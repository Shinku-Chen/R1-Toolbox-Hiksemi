"""Real network producer/accounting/panel chain over temporary proc/sys files.

No host interface, command, disk, or network request is used.  Regular fixture
files exercise production readers on both Windows and Linux; they do not claim
to emulate Linux sysfs lifetimes or kernel permissions.
"""
import copy
import datetime as dt
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))


def load_runtime(name):
    # Do not replace shared loader names used by HTTP fixture modules.
    spec = importlib.util.spec_from_file_location("network_control_" + name,
                                                ROOT / "runtime" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


insights = load_runtime("insights")
monitoring = load_runtime("monitoring")
compat = load_runtime("fnm_compat")


class Snapshot:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return copy.deepcopy(self.value)


class NetworkControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r1-network-control-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc, self.sys = self.root / "proc", self.root / "sys"
        self.proc.mkdir()
        self.sys.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.now, self.wall = 100.0, 1_790_000_000.0
        self.write(self.proc / "sys/kernel/random/boot_id",
                   "01234567-89ab-cdef-0123-456789abcdef")
        self.write(self.proc / "self/mountinfo", "")
        self.write(self.proc / "diskstats", "")
        self.collector = self.new_collector()
        self.basic = Snapshot({"disks": []})
        self.service = monitoring.MonitoringService(
            self.state, self.collector, self.basic, Snapshot([]),
            proc_root=self.proc, sys_root=self.sys, clock=lambda: self.now,
            wall_clock=lambda: self.wall, timezone=dt.timezone.utc)
        self.service.settings["history_enabled"] = False
        self.addCleanup(self.service.stop)
        # Windows lacks this Linux flag.  Only regular temporary metadata files
        # are opened here; Linux retains its real O_NONBLOCK value.
        patch = mock.patch.object(monitoring.os, "O_NONBLOCK",
                                  getattr(os, "O_NONBLOCK", 0), create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.panel = compat.FnmonitorPanel(
            Snapshot({}), self.collector, self.service, None, None, self.basic,
            state_dir=self.state, proc_root=self.proc, sys_root=self.sys,
            dev_root=self.root / "dev", udev_root=self.root / "run/udev",
            clock=lambda: self.now, wall_clock=lambda: self.wall,
            weather_fetcher=lambda _: self.fail("unexpected weather fetch"))

    def new_collector(self):
        return insights.InsightsCollector(
            self.proc, self.sys, clock=lambda: self.now,
            ipv4_provider=lambda: {}, page_size=4096, clock_ticks=100,
            command_finder=lambda _: self.fail("unexpected executable lookup"),
            runner=lambda *a, **kw: self.fail("unexpected command"))

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding="ascii")

    def interface(self, name, rx=1000, tx=2000):
        path = self.sys / "class/net" / name
        path.mkdir(parents=True, exist_ok=True)
        self.write(path / "operstate", "up")
        self.write(path / "address", "02:00:00:00:00:01")
        if rx is not None:
            self.write(path / "statistics/rx_bytes", rx)
        if tx is not None:
            self.write(path / "statistics/tx_bytes", tx)
        return path

    def counters(self, name, rx, tx):
        self.write(self.sys / "class/net" / name / "statistics/rx_bytes", rx)
        self.write(self.sys / "class/net" / name / "statistics/tx_bytes", tx)

    def sample(self, advance=0):
        self.now += advance
        self.wall += advance
        rows = self.collector._interfaces(self.now)
        stamp = dt.datetime.fromtimestamp(self.wall, dt.timezone.utc).isoformat()
        with self.collector._lock:
            self.collector._state.update(timestamp=stamp, interfaces=rows)
        self.basic.value["timestamp"] = stamp
        self.service.sample_once()
        return rows, self.panel.overview()

    def assert_rates(self, overview, rx, tx):
        values = {row["id"]: row["value"]
                  for row in self.service.snapshot()["current"]["metrics"]}
        self.assertEqual(values["rx_bytes_per_sec"], rx)
        self.assertEqual(values["tx_bytes_per_sec"], tx)
        self.assertEqual(overview["net_aggregate"]["rx_rate"], rx)
        self.assertEqual(overview["net_aggregate"]["tx_rate"], tx)

    def test_control_file_is_not_an_interface_and_full_chain_recovers(self):
        self.interface("eth0")
        self.write(self.sys / "class/net/bonding_masters", "bond0\n")
        first, overview = self.sample()
        self.assertEqual([row["name"] for row in first], ["eth0"])
        self.assert_rates(overview, None, None)
        self.counters("eth0", 2_501_000, 962_000)
        rows, overview = self.sample(2)
        self.assertEqual([row["name"] for row in rows], ["eth0"])
        self.assertNotIn("bonding_masters", self.collector._previous_network)
        self.assert_rates(overview, 1_250_000.0, 480_000.0)
        self.assertEqual(overview["net"][0]["rx_rate"], 1_250_000.0)
        self.assertEqual(overview["net"][0]["tx_rate"], 480_000.0)
        self.assertEqual(self.service.traffic()["totals"]["today"],
                         {"rx_bytes": 2_500_000, "tx_bytes": 960_000})

    def test_real_interface_missing_counter_keeps_canonical_unknown(self):
        self.interface("eth0")
        self.interface("eth1", rx=None, tx=None)
        self.sample()
        self.counters("eth0", 3000, 6000)
        rows, overview = self.sample(2)
        self.assertEqual([row["name"] for row in rows], ["eth0", "eth1"])
        self.assertIsNone(rows[1]["rx_bytes"])
        self.assertEqual(overview["net"][0]["rx_rate"], 1000.0)
        self.assert_rates(overview, None, None)

    def test_actual_counter_read_permission_failure_is_not_an_omitted_interface(self):
        self.interface("eth0")
        self.interface("eth1")
        self.sample()
        self.counters("eth0", 3000, 6000)
        original = Path.open

        def fail_counter(path, *args, **kwargs):
            if path.parent == self.sys / "class/net/eth1/statistics":
                raise PermissionError("fixture counter access denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=fail_counter):
            rows, overview = self.sample(2)
        self.assertEqual([row["name"] for row in rows], ["eth0", "eth1"])
        self.assertIsNone(rows[1]["rx_bytes_per_sec"])
        self.assert_rates(overview, None, None)

    def test_disappearing_real_interface_counters_are_not_treated_as_control(self):
        path = self.interface("eth1")
        self.interface("eth0")
        self.sample()
        self.counters("eth0", 3000, 6000)
        (path / "statistics/rx_bytes").unlink()
        (path / "statistics/tx_bytes").unlink()
        rows, overview = self.sample(2)
        self.assertEqual(len(rows), 2)
        self.assert_rates(overview, None, None)

    def test_newly_readable_real_interface_still_needs_two_valid_frames(self):
        self.interface("eth0")
        self.interface("eth1", rx=None, tx=None)
        self.sample()
        self.counters("eth0", 3000, 6000)
        self.counters("eth1", 1000, 2000)
        rows, overview = self.sample(2)
        self.assertIsNone(rows[1]["rx_bytes_per_sec"])
        self.assert_rates(overview, None, None)
        self.counters("eth0", 5000, 10000)
        self.counters("eth1", 3000, 6000)
        _, overview = self.sample(2)
        self.assert_rates(overview, 2000.0, 4000.0)

    def test_other_regular_file_is_not_silently_filtered(self):
        self.interface("eth0")
        self.write(self.sys / "class/net/mystery0", "not verified as control")
        self.sample()
        self.counters("eth0", 3000, 6000)
        rows, overview = self.sample(2)
        self.assertEqual([row["name"] for row in rows], ["eth0", "mystery0"])
        self.assert_rates(overview, None, None)

    def test_control_metadata_failure_cannot_make_aggregate_falsely_known(self):
        self.interface("eth0")
        control = self.sys / "class/net/bonding_masters"
        self.write(control, "bond0")
        self.sample()
        self.counters("eth0", 3000, 6000)
        original = Path.lstat
        for error in (PermissionError("fixture"), FileNotFoundError("fixture")):
            with self.subTest(error=type(error).__name__):
                def fail_control(path, *args, **kwargs):
                    if path == control:
                        raise error
                    return original(path, *args, **kwargs)
                with mock.patch.object(Path, "lstat", new=fail_control):
                    rows, overview = self.sample(2)
                self.assertIn("bonding_masters", [row["name"] for row in rows])
                self.assert_rates(overview, None, None)

    def test_same_name_directory_is_not_proven_to_be_control_file(self):
        self.interface("eth0")
        self.interface("bonding_masters", rx=None, tx=None)
        self.sample()
        self.counters("eth0", 3000, 6000)
        rows, overview = self.sample(2)
        self.assertIn("bonding_masters", [row["name"] for row in rows])
        self.assert_rates(overview, None, None)

    def test_symlink_metadata_is_not_followed_to_exclude_control(self):
        self.interface("eth0")
        control = self.sys / "class/net/bonding_masters"
        self.write(control, "bond0")
        self.sample()
        self.counters("eth0", 3000, 6000)
        original = Path.lstat

        def symlink_metadata(path, *args, **kwargs):
            if path == control:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
            return original(path, *args, **kwargs)

        # This is a portable metadata seam, not a claimed Linux symlink test.
        with mock.patch.object(Path, "lstat", new=symlink_metadata):
            rows, overview = self.sample(2)
        self.assertIn("bonding_masters", [row["name"] for row in rows])
        self.assert_rates(overview, None, None)

    def test_collector_restart_requires_new_pair_including_panel_projection(self):
        self.interface("eth0")
        self.sample()
        self.counters("eth0", 3000, 6000)
        _, overview = self.sample(2)
        self.assert_rates(overview, 1000.0, 2000.0)
        self.collector = self.new_collector()
        self.service.insights = self.panel.insights = self.collector
        self.counters("eth0", 5000, 10000)
        _, overview = self.sample(2)
        self.assert_rates(overview, None, None)

    def test_counter_rollback_and_recovery_require_real_pair(self):
        self.interface("eth0")
        self.sample()
        self.counters("eth0", 10, 20)
        _, overview = self.sample(2)
        self.assert_rates(overview, None, None)
        self.counters("eth0", 210, 420)
        _, overview = self.sample(2)
        self.assert_rates(overview, 100.0, 200.0)

    def test_nonpositive_sample_interval_remains_unknown(self):
        self.interface("eth0")
        self.sample()
        self.counters("eth0", 3000, 6000)
        _, overview = self.sample()
        self.assert_rates(overview, None, None)
        self.counters("eth0", 4000, 8000)
        _, overview = self.sample(-1)
        self.assert_rates(overview, None, None)

    def test_boot_change_blocks_counter_totals_without_inventing_zero(self):
        self.interface("eth0")
        self.sample()
        self.write(self.proc / "sys/kernel/random/boot_id",
                   "ffffffff-ffff-ffff-ffff-ffffffffffff")
        self.counters("eth0", 3000, 6000)
        self.sample(2)
        self.assertIsNone(self.service.traffic()["totals"]["today"])
        self.assertTrue(self.service.traffic()["partial"])

    def test_bridge_bond_and_physical_policy_is_preserved_through_panel(self):
        for name in ("eth0", "bond0", "br0"):
            self.interface(name)
        self.write(self.sys / "class/net/bonding_masters", "bond0")
        self.sample()
        for name in ("eth0", "bond0", "br0"):
            self.counters(name, 1_801_000, 602_000)
        rows, overview = self.sample(2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(overview["net"]), 3)
        self.assert_rates(overview, 900_000.0, 300_000.0)
        self.assertTrue(overview["net_aggregate"]["exclude_virtual"])
        # Explicitly including virtual counters retains the existing policy;
        # it is not a new UI fallback which silently adds bridge traffic twice.
        self.service.settings["traffic_exclude_virtual"] = False
        _, overview = self.sample()
        self.assert_rates(overview, None, None)
        for name in ("eth0", "bond0", "br0"):
            self.counters(name, 3_601_000, 1_202_000)
        _, overview = self.sample(2)
        self.assert_rates(overview, 2_700_000.0, 900_000.0)
        self.assertFalse(overview["net_aggregate"]["exclude_virtual"])

    def test_previous_control_row_is_replaced_by_next_real_producer_sample(self):
        self.interface("eth0")
        self.write(self.sys / "class/net/bonding_masters", "bond0")
        self.sample()
        old_control = {"name": "bonding_masters", "rx_bytes": None,
                       "tx_bytes": None, "rx_bytes_per_sec": None,
                       "tx_bytes_per_sec": None}
        with self.collector._lock:
            self.collector._state["interfaces"].append(old_control)
        self.counters("eth0", 3000, 6000)
        rows, overview = self.sample(2)
        self.assertNotIn("bonding_masters", [row["name"] for row in rows])
        self.assert_rates(overview, 1000.0, 2000.0)

    def test_inventory_budget_and_name_validation_are_not_expanded(self):
        for index in range(130):
            self.interface("eth%03d" % index)
        self.write(self.sys / "class/net/bonding_masters", "bond0")
        rows, overview = self.sample()
        self.assertLessEqual(len(rows), 128)
        self.assertNotIn("bonding_masters", [row["name"] for row in rows])
        self.assert_rates(overview, None, None)


if __name__ == "__main__":
    unittest.main()
