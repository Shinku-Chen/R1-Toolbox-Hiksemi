"""Monitoring exercises fixture counters only; no hardware, commands or network."""
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("monitoring_under_test", ROOT / "runtime/monitoring.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class Snapshot:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return copy.deepcopy(self.value)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="r1-monitor-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.proc, self.sys, self.state = self.root / "proc", self.root / "sys", self.root / "data"
        self.proc.mkdir(); self.sys.mkdir(); self.state.mkdir(mode=0o700)
        self.now, self.wall = 100.0, dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc).timestamp()
        self.write(self.proc / "sys/kernel/random/boot_id", "01234567-89ab-cdef-0123-456789abcdef")
        self.insights = Snapshot({"system": {"cpu_usage_percent": 23, "cpu_model": "Fixture CPU", "uptime_seconds": 10,
            "memory": {"usage_percent": 42, "total_bytes": 1024}}, "interfaces": [self.interface()],
            "device": {"fans": []}, "disks": {"items": []},
            "processes": [{"cmdline": "SECRET_PASSWORD=hunter2"}], "credentials": "do not export"})
        self.basic = Snapshot({"cpu": {"usage_percent": 23}, "memory": {"usage_percent": 42},
                               "disks": [{"mount": "/drives/fixture", "usage_percent": 30}]})
        self.sources = Snapshot([self.source("cpu:package", 40), self.source("disk:fixture", 32, "disk")])
        self.fan = Snapshot({"channels": {"cpu": {"active": True, "target_percent": 65},
                                          "system": {"active": False, "target_percent": 80}}})
        self.service = self.create()
        self.addCleanup(self.service.stop)

    def create(self, **kwargs):
        return module.MonitoringService(self.state, self.insights, self.basic, self.sources,
            fan_policy=self.fan, proc_root=self.proc, sys_root=self.sys, clock=lambda: self.now,
            wall_clock=lambda: self.wall, timezone=dt.timezone.utc, **kwargs)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def interface(self, name="eth0", rx=1000, tx=2000):
        return {"name": name, "rx_bytes": rx, "tx_bytes": tx, "rx_bytes_per_sec": 10, "tx_bytes_per_sec": 20}

    def source(self, identity, value, kind="cpu"):
        return {"id": identity, "label": identity, "kind": kind, "temperature_c": value,
                "sampled_monotonic": self.now, "max_age_seconds": 3 if kind != "disk" else 90, "status": "ok"}

    def sample(self, advance=0, refresh=True):
        self.now += advance; self.wall += advance
        if refresh:
            stamp = dt.datetime.fromtimestamp(self.wall, dt.timezone.utc).isoformat()
            self.insights.value["timestamp"] = stamp
            self.insights.value.setdefault("device", {})["sampled_at"] = stamp
            self.basic.value["timestamp"] = stamp
            for row in self.sources.value:
                row["sampled_monotonic"] = self.now
        self.service.sample_once()
        return {row["id"]: row["value"] for row in self.service.snapshot()["current"]["metrics"]}

    def test_true_zero_negative_temperature_and_requested_output_remain_distinct(self):
        self.insights.value["system"]["cpu_usage_percent"] = 0
        self.sources.value = [self.source("cpu:package", -4), self.source("disk:fixture", 0, "disk")]
        values = self.sample()
        self.assertEqual(values["cpu_usage_percent"], 0)
        self.assertEqual(values["cpu_temperature_c"], -4)
        self.assertEqual(values["disk_temperature_c"], 0)
        self.assertEqual(values["fan_cpu_target_percent"], 65)
        self.assertIsNone(values["fan_system_target_percent"])
        self.assertFalse(any(key.startswith("fan_rpm.") for key in values))

    def test_partial_settings_persist_without_changing_other_fields(self):
        result = self.service.action("monitor.settings.update", {"settings": {"retention_days": 7}})
        self.assertTrue(result["settings_saved"])
        other = self.create(); self.addCleanup(other.stop)
        self.assertEqual(other.snapshot()["settings"]["retention_days"], 7)
        self.assertEqual(other.snapshot()["settings"]["thresholds"], module.DEFAULTS["thresholds"])
        self.assertEqual((self.state / "monitoring.json").stat().st_mode & 0o777, 0o600)

    def test_settings_strict_thresholds_types_and_unknown_fields(self):
        for changes in ({"retention_days": True}, {"retention_days": 31}, {"history_enabled": 1},
                        {"shell": "anything"}, {"thresholds": {"cpu_warn": 95}},
                        {"thresholds": {"disk_temp_critical": 200}}, {"thresholds": {"temp": 30}}):
            with self.subTest(changes=changes):
                self.assertFalse(self.service.action("monitor.settings.update", {"settings": changes})["settings_saved"])
        self.assertFalse(self.service.action("monitor.settings.update", {"settings": {"retention_days": 7}, "extra": 1})["ok"])

    def test_save_failure_same_value_has_no_false_ack(self):
        with mock.patch.object(self.service, "_prepare_directory", side_effect=OSError("fixture")):
            result = self.service.action("monitor.settings.update", {"settings": {"retention_days": 30}})
        self.assertFalse(result["settings_saved"])

    def test_bad_settings_and_symlink_are_not_loaded(self):
        self.write(self.state / "monitoring.json", "{broken")
        other = self.create(); self.addCleanup(other.stop)
        self.assertIsNotNone(other.snapshot()["settings_error"])
        (self.state / "monitoring.json").unlink()
        external = self.root / "external.json"
        self.write(external, json.dumps({"version": 1, "settings": {"retention_days": 7}}))
        (self.state / "monitoring.json").symlink_to(external)
        other = self.create(); self.addCleanup(other.stop)
        self.assertEqual(other.snapshot()["settings"]["retention_days"], 30)
        self.assertFalse(other.action("monitor.settings.update", {"settings": {"retention_days": 7}})["ok"])

    def test_missing_sqlite_import_keeps_live_monitoring(self):
        isolated = importlib.util.module_from_spec(SPEC)
        with mock.patch.dict("sys.modules", {"sqlite3": None}):
            SPEC.loader.exec_module(isolated)
        self.assertIsNone(isolated.sqlite3)
        service = isolated.MonitoringService(self.state, self.insights, self.basic, self.sources,
            proc_root=self.proc, sys_root=self.sys, clock=lambda: self.now, wall_clock=lambda: self.wall)
        self.addCleanup(service.stop)
        stamp = dt.datetime.fromtimestamp(self.wall, dt.timezone.utc).isoformat()
        self.insights.value["timestamp"] = self.basic.value["timestamp"] = stamp
        service.sample_once()
        self.assertTrue(service.snapshot()["available"])
        self.assertIn("sqlite3", service.snapshot()["history"]["reason"])
        self.assertFalse(service.history()["available"])
        self.assertFalse((self.state / "monitoring.sqlite3").exists())

    def test_corrupt_database_does_not_hide_live_cpu(self):
        self.write(self.state / "monitoring.sqlite3", "not a database")
        values = self.sample()
        self.assertEqual(values["cpu_usage_percent"], 23)
        self.assertFalse(self.service.snapshot()["history"]["recording"])
        self.assertIsNotNone(self.service.history()["reason"])

    def test_database_retries_are_low_frequency(self):
        with mock.patch.object(self.service, "_open_db", side_effect=OSError("no volume")) as opener:
            self.sample(); self.sample(2); self.sample(2)
            self.assertEqual(opener.call_count, 1)
            self.sample(30)
            self.assertEqual(opener.call_count, 2)

    def test_no_database_when_history_disabled(self):
        self.assertTrue(self.service.action("monitor.settings.update", {"settings": {"history_enabled": False}})["ok"])
        self.sample()
        self.assertFalse((self.state / "monitoring.sqlite3").exists())
        self.assertFalse(self.service.history()["available"])
        self.assertFalse(self.service.snapshot()["history"]["recording"])

    def test_rapl_measures_package_only_with_wrap_zero_and_no_estimates(self):
        domain = self.sys / "class/powercap/intel-rapl:0"
        self.write(domain / "name", "package-0")
        self.write(domain / "max_energy_range_uj", 100000000)
        self.write(domain / "energy_uj", 99000000)
        self.write(self.sys / "class/powercap/intel-rapl:0:0/name", "core")
        self.write(self.sys / "class/powercap/intel-rapl:0:0/energy_uj", 500000000)
        self.assertIsNone(self.sample()["cpu_package_w"])
        self.write(domain / "energy_uj", 1000000)
        self.assertEqual(self.sample(2)["cpu_package_w"], 1)
        self.assertEqual(self.sample(2)["cpu_package_w"], 0)
        self.assertEqual(self.service.snapshot()["current"]["power"]["scope"], "cpu_package")
        self.assertEqual(len(self.service.snapshot()["current"]["power"]["domains"]), 1)
        (domain / "energy_uj").unlink()
        self.assertIsNone(self.sample(2)["cpu_package_w"])

    def test_rapl_symlink_escape_and_gap_are_unavailable(self):
        external = self.root / "outside-rapl"
        self.write(external / "name", "package-0")
        self.write(external / "energy_uj", 1)
        self.write(external / "max_energy_range_uj", 1000)
        (self.sys / "class/powercap").mkdir(parents=True)
        (self.sys / "class/powercap/intel-rapl:0").symlink_to(external, target_is_directory=True)
        self.assertIsNone(self.sample()["cpu_package_w"])
        self.assertEqual(self.service.snapshot()["current"]["power"]["domains"], [])

    def test_disk_io_uses_kernel_512_byte_sectors_and_no_partitions(self):
        (self.sys / "block/sda").mkdir(parents=True)
        self.write(self.proc / "diskstats", "8 0 sda 1 0 100 0 1 0 200 0 0 0 0\n8 1 sda1 1 0 500 0 1 0 800 0 0 0 0")
        self.assertIsNone(self.sample()["disk_read_bytes_per_sec"])
        self.write(self.proc / "diskstats", "8 0 sda 1 0 110 0 1 0 220 0 0 0 0")
        values = self.sample(2)
        self.assertEqual(values["disk_read_bytes_per_sec"], 2560)
        self.assertEqual(values["disk_write_bytes_per_sec"], 5120)
        self.write(self.proc / "diskstats", "8 0 sda 1 0 1 0 1 0 2 0 0 0 0")
        self.assertIsNone(self.sample(2)["disk_read_bytes_per_sec"])

    def test_component_failure_does_not_suppress_other_metrics(self):
        with mock.patch.object(self.service, "_power", side_effect=OSError("fixture")), mock.patch.object(self.service, "_disk_io", side_effect=ValueError("fixture")):
            values = self.sample()
        self.assertEqual(values["cpu_usage_percent"], 23)
        self.assertEqual(values["cpu_temperature_c"], 40)
        self.assertIsNone(values["cpu_package_w"])
        self.assertIsNone(values["disk_read_bytes_per_sec"])

    def test_traffic_observed_delta_zero_and_virtual_filter(self):
        self.insights.value["interfaces"].append(self.interface("veth123", 4000, 8000))
        self.sample()
        self.assertFalse(self.service.traffic()["available"])
        self.insights.value["interfaces"][0].update(rx_bytes=1100, tx_bytes=2200)
        self.insights.value["interfaces"][1].update(rx_bytes=9000, tx_bytes=9000)
        self.sample(2)
        traffic = self.service.traffic()
        self.assertEqual(traffic["totals"]["today"], {"rx_bytes": 100, "tx_bytes": 200})
        self.assertEqual([row["name"] for row in traffic["interfaces"]], ["eth0"])
        self.assertTrue(traffic["partial"])
        self.sample(2)
        self.assertEqual(self.service.traffic()["totals"]["today"]["rx_bytes"], 100)
        self.service.action("monitor.settings.update", {"settings": {"traffic_exclude_virtual": False}})
        values = self.sample(2)
        self.assertEqual(values["rx_bytes_per_sec"], 20)
        self.assertEqual(self.service.traffic()["totals"]["today"]["rx_bytes"], 5100)

    def test_traffic_reset_boot_change_and_missing_boot_never_fabricate(self):
        self.sample()
        self.insights.value["interfaces"][0].update(rx_bytes=100, tx_bytes=200)
        self.sample(2)
        self.assertIsNone(self.service.traffic()["totals"]["today"])
        self.write(self.proc / "sys/kernel/random/boot_id", "ffffffff-ffff-ffff-ffff-ffffffffffff")
        self.insights.value["interfaces"][0].update(rx_bytes=500, tx_bytes=800)
        self.sample(2)
        self.assertIsNone(self.service.traffic()["totals"]["today"])
        (self.proc / "sys/kernel/random/boot_id").unlink()
        self.insights.value["interfaces"][0].update(rx_bytes=1000, tx_bytes=2000)
        self.sample(2)
        self.assertIsNone(self.service.traffic()["totals"]["today"])

    def test_traffic_midnight_gap_is_not_allocated_to_new_day(self):
        self.wall = dt.datetime(2026, 10, 15, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()
        self.sample()
        self.insights.value["interfaces"][0].update(rx_bytes=1100, tx_bytes=2200)
        self.sample(2)
        traffic = self.service.traffic()
        self.assertIsNone(traffic["totals"]["today"])
        self.assertEqual(traffic["totals"]["month"], {"rx_bytes": 100, "tx_bytes": 200})

    def test_persisted_counter_baseline_and_total_resume_same_boot(self):
        self.sample()
        self.insights.value["interfaces"][0].update(rx_bytes=1100, tx_bytes=2200)
        self.sample(30)
        self.service.stop()
        other = self.create(); self.addCleanup(other.stop)
        self.service = other
        self.insights.value["interfaces"][0].update(rx_bytes=1150, tx_bytes=2250)
        self.sample(2)
        self.assertEqual(other.traffic()["totals"]["today"], {"rx_bytes": 150, "tx_bytes": 250})

    def test_freshness_does_not_extend_cpu_temperature_smart_or_rpm(self):
        self.insights.value["device"]["fans"] = [{"id": "hwmon0:fan1", "driver": "fixture", "channel": "fan1", "rpm": 0}]
        self.insights.value["disks"]["items"] = [{"id": "sda", "stable_identity": "disk:test", "model": "Fixture SSD", "health": {"passed": True}, "smart_sampled_monotonic": self.now, "smart_temperature_c": 32}]
        first = self.sample()
        self.assertTrue(any(key.startswith("fan_rpm.") and value == 0 for key, value in first.items()))
        self.assertIs(self.service.snapshot()["smart"][0]["passed"], True)
        stale = self.sample(100, refresh=False)
        self.assertIsNone(stale["cpu_usage_percent"])
        self.assertIsNone(stale["cpu_temperature_c"])
        self.assertFalse(any(key.startswith("fan_rpm.") for key in stale))
        self.assertIsNone(self.service.snapshot()["smart"][0]["passed"])
        self.assertIsNone(self.service.snapshot()["smart"][0]["temperature_c"])

    def test_smart_observations_are_cached_no_self_test_and_standby_is_unknown(self):
        self.insights.value["disks"]["items"] = [{"id": "sda", "health": {"passed": True}, "standby": True,
            "smart_sampled_monotonic": self.now, "smart_temperature_c": 40}]
        self.sample(); self.sample(2)
        data = self.service.alerts()
        self.assertEqual(len(data["smart_observations"]), 1)
        self.assertIsNone(self.service.snapshot()["smart"][0]["passed"])
        self.assertIn("不主动唤醒", self.service.snapshot()["smart"][0]["reason"])

    def test_alert_raise_change_recover_and_missing_is_stale_not_recovered(self):
        self.insights.value["system"]["cpu_usage_percent"] = 90
        self.sample()
        self.assertEqual(self.service.alerts()["active"][0]["level"], "warning")
        self.insights.value["system"]["cpu_usage_percent"] = 99
        self.sample(2)
        self.assertEqual(self.service.alerts()["active"][0]["level"], "critical")
        self.sample(20, refresh=False)
        self.assertTrue(self.service.alerts()["active"][0]["stale"])
        self.assertNotIn("recovered", [row["kind"] for row in self.service.alerts()["events"]])
        self.insights.value["system"]["cpu_usage_percent"] = 10
        self.sample(2)
        self.assertEqual(self.service.alerts()["active"], [])
        self.assertEqual([row["kind"] for row in self.service.alerts()["events"]], ["raised", "changed", "recovered"])

    def test_missing_capacity_and_smart_are_not_recovery(self):
        self.basic.value["disks"][0]["usage_percent"] = 99
        self.insights.value["disks"]["items"] = [{"id": "sda", "health": {"passed": False}, "smart_sampled_monotonic": self.now}]
        self.sample()
        self.assertEqual(len(self.service.alerts()["active"]), 2)
        self.basic.value["disks"] = []
        self.insights.value["disks"]["items"] = []
        self.sample(2)
        self.assertTrue(all(row.get("stale") for row in self.service.alerts()["active"]))
        self.assertNotIn("recovered", [row["kind"] for row in self.service.alerts()["events"]])

    def test_history_batched_30_seconds_and_public_reads_do_not_query_db(self):
        self.sample(); self.sample(2); self.sample(2)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM samples").fetchone()[0], 1)
        self.sample(26)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM samples").fetchone()[0], 2)
        db = self.service.db
        self.service.db = object()
        try:
            for method in (self.service.snapshot, self.service.history, self.service.traffic, self.service.alerts, self.service.report, self.service.export_csv):
                method()
        finally:
            self.service.db = db

    def test_history_ranges_bounded_points_and_missing_values_break_lines(self):
        self.sample()
        first = int(self.wall) - 2592000
        encoded = json.dumps({"cpu_usage_percent": {"value": 30, "label": "CPU", "unit": "%"}})
        with self.service.db:
            self.service.db.executemany("INSERT OR REPLACE INTO samples VALUES(?,?)", ((first + index * 1800, encoded) for index in range(1441)))
            self.service.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?)", (int(self.wall) - 300, "{}"))
        self.service._refresh_history(int(self.wall))
        for selected_range in module.RANGES:
            history = self.service.history(selected_range)
            self.assertTrue(all(len(row["points"]) <= 720 for row in history["series"]))
        cpu = next(row for row in self.service.history("10m")["series"] if row["id"] == "cpu_usage_percent")
        self.assertTrue(any(value is None for _, value in cpu["points"]))
        with self.assertRaises(ValueError):
            self.service.history("all")

    def test_retention_prunes_and_history_disable_returns_no_old_data(self):
        self.sample()
        with self.service.db:
            self.service.db.execute("INSERT INTO samples VALUES(?,?)", (int(self.wall) - 31 * 86400, "{}"))
        self.sample(30)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM samples WHERE ts<?", (int(self.wall) - 30 * 86400,)).fetchone()[0], 0)
        self.service.action("monitor.settings.update", {"settings": {"history_enabled": False}})
        self.assertEqual(self.service.history()["series"], [])
        count = self.service.db.execute("SELECT count(*) FROM samples").fetchone()[0]
        self.sample(30)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM samples").fetchone()[0], count)

    def test_write_failure_does_not_block_cached_snapshot_lock(self):
        self.sample()
        entered, finish = threading.Event(), threading.Event()
        original = self.service._flush
        def slow(stamp):
            entered.set(); finish.wait(2)
            original(stamp)
        self.service._flush = slow
        self.now += 30; self.wall += 30
        thread = threading.Thread(target=self.service.sample_once)
        thread.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.service.snapshot(); self.service.history(); self.service.traffic(); self.service.alerts()
        self.assertLess(time.monotonic() - started, 0.2)
        finish.set(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_report_and_csv_do_not_export_arbitrary_insights_or_credentials(self):
        self.sample()
        raw = json.dumps(self.service.report())
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("credentials", raw)
        self.assertNotIn("cmdline", raw)
        self.assertIn("Fixture CPU", raw)
        csv = self.service.export_csv("24h")
        self.assertIn("timestamp_utc,metric,value,unit", csv)
        self.assertIn("cpu_usage_percent,23,%", csv)
        self.assertNotIn("hunter2", csv)

    def test_fixture_path_reader_rejects_fifo_oversize_and_outside_symlink(self):
        external = self.root / "secret"
        self.write(external, "not allowed")
        (self.proc / "link").symlink_to(external)
        self.assertIsNone(self.service._read(self.proc / "link", root=self.proc))
        self.write(self.proc / "huge", "x" * 30)
        self.assertIsNone(self.service._read(self.proc / "huge", 10, self.proc))
        os.mkfifo(self.proc / "fifo")
        self.assertIsNone(self.service._read(self.proc / "fifo", root=self.proc))

    def test_cached_public_returns_are_independent_copies(self):
        self.sample()
        one = self.service.snapshot(); one["settings"]["retention_days"] = 1
        history = self.service.history(); history["series"].clear()
        self.assertEqual(self.service.snapshot()["settings"]["retention_days"], 30)
        self.assertTrue(self.service.history()["series"])


if __name__ == "__main__":
    unittest.main()
