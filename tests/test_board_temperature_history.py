"""Temperature display/history clocks over real source and Monitoring code.

Only controlled proc/sys metadata files and snapshot inputs are used. No SMART,
network, host sensor, fan action, or live NAS operation is performed.
"""
import copy
import datetime as dt
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))
import temperature_sources

SPEC = importlib.util.spec_from_file_location("board_temperature_monitoring", ROOT / "runtime/monitoring.py")
monitoring = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitoring)


class Clock:
    def __init__(self):
        self.current = 100.0
        self.last = 100.0
        self.calls = 0

    def __call__(self):
        self.last = self.current
        self.current += 0.1
        self.calls += 1
        return self.last


class Snapshot:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return copy.deepcopy(self.value)


class BoardTemperatureHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r1-board-temperature-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys, self.proc, self.state = self.root / "sys", self.root / "proc", self.root / "state"
        self.sys.mkdir()
        self.proc.mkdir()
        self.state.mkdir()
        self.clock = Clock()
        self.wall_base = 1_700_000_000.0
        self.insights = Snapshot({"disks": {"available": False, "items": []}, "interfaces": [],
                                  "system": {"cpu_usage_percent": 25, "memory": {"usage_percent": 30}}})
        self.basic = Snapshot({"disks": []})
        self.sources = temperature_sources.TemperatureSources(
            self.insights, self.sys, clock=self.clock)
        self.service = monitoring.MonitoringService(
            self.state, self.insights, self.basic, self.sources,
            proc_root=self.proc, sys_root=self.sys, clock=self.clock,
            wall_clock=lambda: self.wall_base + self.clock.last,
            timezone=dt.timezone.utc)
        self.service.settings["history_enabled"] = False
        self.addCleanup(self.service.stop)
        patch = mock.patch.object(monitoring.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.write(self.proc / "self/mountinfo", "")
        self.write(self.proc / "diskstats", "")
        self.write(self.proc / "sys/kernel/random/boot_id", "01234567-89ab-cdef-0123-456789abcdef")

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="ascii")

    def hwmon(self, index=0, driver="coretemp", label="Package id 0", value=55000):
        folder = self.sys / "class/hwmon" / ("hwmon" + str(index))
        self.write(folder / "name", driver)
        self.write(folder / "temp1_input", value)
        self.write(folder / "temp1_label", label)
        return folder

    def sample(self):
        stamp = dt.datetime.fromtimestamp(self.wall_base + self.clock.current, dt.timezone.utc).isoformat()
        self.insights.value["timestamp"] = self.basic.value["timestamp"] = stamp
        self.service.sample_once()
        return {row["id"]: row for row in self.service.snapshot()["current"]["metrics"]}

    def row(self, identity="fixture", *, value=42, sampled=100, maximum=3,
            kind="board", role="board", status="ok"):
        result = {"id": identity, "label": identity, "kind": kind, "temperature_c": value,
                  "sampled_monotonic": sampled, "max_age_seconds": maximum, "status": status}
        if role is not None:
            result.update(display_role=role, display_evidence="fixture_explicit_metadata")
        return result

    def controlled_sources(self, rows):
        self.service.sources = Snapshot(rows)

    def test_real_new_sysfs_read_is_evaluated_after_sources_and_never_future(self):
        self.hwmon()
        metrics = self.sample()
        cpu = metrics["cpu_temperature_c"]
        self.assertEqual(cpu["value"], 55)
        independent = next(row for key, row in metrics.items() if key.startswith("temperature.sysfs:"))
        self.assertIsNone(cpu["reason"])
        self.assertGreater(cpu["sampled_monotonic"], 100.0)
        self.assertLessEqual(cpu["sampled_monotonic"], self.service.snapshot()["current"]["sampled_monotonic"])
        self.assertEqual(cpu["sampled_monotonic"], independent["sampled_monotonic"])
        # Unix-second floats at this epoch have a ~0.24us representable step.
        self.assertAlmostEqual(cpu["sampled_at"], self.wall_base + cpu["sampled_monotonic"], delta=1e-6)
        self.assertEqual(cpu["max_age_seconds"], 3)

    def test_real_one_second_cache_and_rereads_have_no_alternating_null_holes(self):
        self.hwmon()
        readings = []
        with mock.patch.object(self.sources, "_read_sysfs", wraps=self.sources._read_sysfs) as reader:
            for _ in range(12):
                readings.append(self.sample()["cpu_temperature_c"])
        self.assertGreaterEqual(reader.call_count, 3)
        self.assertLess(reader.call_count, 12)
        self.assertTrue(all(row["value"] == 55 and row["reason"] is None for row in readings))
        self.assertGreater(len({row["sampled_monotonic"] for row in readings}), 2)

    def test_cached_real_read_keeps_original_source_monotonic_and_wall_time(self):
        folder = self.hwmon()
        first = self.sample()["cpu_temperature_c"]
        self.write(folder / "temp1_input", 66000)
        second = self.sample()["cpu_temperature_c"]
        self.assertEqual(second["value"], 55)
        self.assertEqual(second["sampled_monotonic"], first["sampled_monotonic"])
        self.assertAlmostEqual(second["sampled_at"], first["sampled_at"], delta=1e-6)
        self.assertEqual(second["max_age_seconds"], first["max_age_seconds"])
        self.clock.current += 1
        third = self.sample()["cpu_temperature_c"]
        self.assertEqual(third["value"], 66)
        self.assertGreater(third["sampled_monotonic"], second["sampled_monotonic"])

    def test_real_explicit_board_is_distinct_from_unknown_chip_and_cpu(self):
        self.hwmon(0, "coretemp", "Package id 0", 55000)
        self.hwmon(1, "nct6775", "System", 42000)
        self.hwmon(2, "opaque_sensor", "temp1", 99000)
        metrics = self.sample()
        self.assertEqual(metrics["cpu_temperature_c"]["value"], 55)
        self.assertEqual(metrics["board_temperature_c"]["value"], 42)
        independent = [row["value"] for key, row in metrics.items() if key.startswith("temperature.")]
        self.assertEqual(sorted(independent), [42, 55, 99])

    def test_real_unproven_board_kind_does_not_become_display_board(self):
        self.hwmon(0, "opaque_sensor", "temp1", 99000)
        metrics = self.sample()
        board = metrics["board_temperature_c"]
        self.assertIsNone(board["value"])
        self.assertEqual(board["reason"], "display_role_unverified")
        self.assertIsNotNone(board["sampled_monotonic"])
        self.assertTrue(any(row["value"] == 99 for key, row in metrics.items()
                            if key.startswith("temperature.")))

    def test_display_role_overrides_control_kind_without_reclassifying_it(self):
        rows = [self.row("cpu-label", value=60, kind="board", role="cpu"),
                self.row("board-label", value=45, kind="board", role="board"),
                self.row("unknown-cpu", value=90, kind="cpu", role="unknown")]
        self.controlled_sources(rows)
        metrics = self.sample()
        self.assertEqual(metrics["cpu_temperature_c"]["value"], 60)
        self.assertEqual(metrics["board_temperature_c"]["value"], 45)
        self.assertEqual(metrics["temperature.unknown-cpu"]["value"], 90)
        self.assertEqual(rows[0]["kind"], "board")

    def test_legacy_cpu_and_disk_rows_remain_compatible_but_kind_board_is_unproven(self):
        self.controlled_sources([self.row("old-cpu", value=55, kind="cpu", role=None),
                                 self.row("old-disk", value=35, kind="disk", role=None, maximum=30),
                                 self.row("old-board", value=95, kind="board", role=None)])
        metrics = self.sample()
        self.assertEqual(metrics["cpu_temperature_c"]["value"], 55)
        self.assertEqual(metrics["disk_temperature_c"]["value"], 35)
        self.assertIsNone(metrics["board_temperature_c"]["value"])
        self.assertEqual(metrics["temperature.old-board"]["value"], 95)

    def test_genuine_future_preserves_raw_clock_without_inventing_wall_date(self):
        self.controlled_sources([self.row(sampled=105)])
        metrics = self.sample()
        for key in ("board_temperature_c", "temperature.fixture"):
            self.assertIsNone(metrics[key]["value"])
            self.assertEqual(metrics[key]["reason"], "future_sample")
            self.assertEqual(metrics[key]["sampled_monotonic"], 105)
            self.assertEqual(metrics[key]["max_age_seconds"], 3)
            self.assertIsNone(metrics[key]["sampled_at"])

    def test_stale_source_remains_null_and_retains_original_date_not_sample_cycle(self):
        self.controlled_sources([self.row(sampled=95)])
        self.clock.current = 95
        original = self.sample()["board_temperature_c"]
        self.clock.current = 100
        metrics = self.sample()
        for key in ("board_temperature_c", "temperature.fixture"):
            row = metrics[key]
            self.assertIsNone(row["value"])
            self.assertEqual(row["reason"], "stale_sample")
            self.assertEqual(row["sampled_monotonic"], 95)
            self.assertEqual(row["max_age_seconds"], 3)
            self.assertAlmostEqual(row["sampled_at"], self.wall_base + 95)
            self.assertEqual(row["sampled_at"], original["sampled_at"])

    def test_real_clock_regression_cannot_revive_until_high_water_is_reached(self):
        self.hwmon()
        self.assertEqual(self.sample()["cpu_temperature_c"]["value"], 55)
        self.clock.current = 20
        regressed = self.sample()["cpu_temperature_c"]
        self.assertIsNone(regressed["value"])
        self.assertEqual(regressed["reason"], "clock_regressed")
        self.assertIsNotNone(regressed["sampled_monotonic"])
        self.assertIsNone(regressed["sampled_at"])
        self.clock.current = 99
        self.assertEqual(self.sample()["cpu_temperature_c"]["reason"], "clock_regressed")
        self.clock.current = 110
        self.assertEqual(self.sample()["cpu_temperature_c"]["value"], 55)

    def test_clock_recoil_during_actual_source_snapshot_is_rejected(self):
        self.hwmon()
        original = self.sources.snapshot

        def recoil():
            rows = original()
            self.clock.current = 99
            return rows

        with mock.patch.object(self.sources, "snapshot", side_effect=recoil):
            row = self.sample()["cpu_temperature_c"]
        self.assertIsNone(row["value"])
        self.assertEqual(row["reason"], "clock_regressed")
        self.assertIsNotNone(row["sampled_monotonic"])
        self.assertIsNone(row["sampled_at"])

    def test_aggregate_maximum_uses_that_exact_source_time_and_lifetime(self):
        self.controlled_sources([self.row("older", value=42, sampled=98, maximum=5),
                                 self.row("newer-hotter", value=60, sampled=99, maximum=2)])
        row = self.sample()["board_temperature_c"]
        self.assertEqual((row["value"], row["sampled_monotonic"], row["max_age_seconds"]), (60, 99, 2))
        self.assertAlmostEqual(row["sampled_at"], self.wall_base + 99)

    def test_equal_maxima_conservatively_choose_the_earliest_original_sample(self):
        self.controlled_sources([self.row("newer", value=42, sampled=99, maximum=3),
                                 self.row("older", value=42, sampled=98, maximum=5)])
        row = self.sample()["board_temperature_c"]
        self.assertEqual((row["value"], row["sampled_monotonic"], row["max_age_seconds"]), (42, 98, 5))

    def test_aggregate_without_valid_values_keeps_a_real_rejected_source_time(self):
        self.controlled_sources([self.row("newer", sampled=100, maximum=0.01),
                                 self.row("older", sampled=98, maximum=1)])
        self.clock.current = 98
        self.sample()
        self.clock.current = 100
        row = self.sample()["board_temperature_c"]
        self.assertIsNone(row["value"])
        self.assertEqual((row["reason"], row["sampled_monotonic"], row["max_age_seconds"]),
                         ("stale_sample", 98, 1))
        self.assertAlmostEqual(row["sampled_at"], self.wall_base + 98)

    def test_invalid_or_missing_sample_fields_do_not_become_zero(self):
        for sampled, maximum in ((None, 3), (True, 3), ("100", 3), (float("nan"), 3),
                                 (100, None), (100, True), (100, -1), (100, "3")):
            with self.subTest(sampled=sampled, maximum=maximum):
                self.controlled_sources([self.row(sampled=sampled, maximum=maximum)])
                row = self.sample()["temperature.fixture"]
                self.assertIsNone(row["value"])
                self.assertIsNone(row["sampled_at"])
                self.assertIn(row["reason"], ("missing_sample", "invalid_sample"))

    def test_no_temperature_source_has_null_measurement_fields(self):
        self.controlled_sources([])
        metrics = self.sample()
        for key in ("cpu_temperature_c", "board_temperature_c", "disk_temperature_c"):
            row = metrics[key]
            self.assertEqual(row["reason"], "missing_sample")
            for field in ("value", "sampled_monotonic", "sampled_at", "max_age_seconds"):
                self.assertIsNone(row[field])

    def test_malformed_display_role_does_not_fall_back_or_crash(self):
        for role in ([], {}, True, "unexpected"):
            with self.subTest(role=role):
                self.controlled_sources([self.row(kind="cpu", role=role)])
                metrics = self.sample()
                self.assertIsNone(metrics["cpu_temperature_c"]["value"])
                self.assertEqual(metrics["temperature.fixture"]["value"], 42)

    def test_rejected_points_do_not_displace_existing_valid_independent_points(self):
        rows = [self.row("missing-%03d" % i, value=None, status="unavailable", role="unknown")
                for i in range(127)]
        rows.append(self.row("valid-cpu", value=55, kind="cpu", role="cpu"))
        self.controlled_sources(rows)
        metrics = self.sample()
        self.assertLessEqual(len(metrics), monitoring.MAX_METRICS)
        self.assertEqual(metrics["temperature.valid-cpu"]["value"], 55)
        self.assertEqual(metrics["cpu_temperature_c"]["value"], 55)

    def test_memory_gets_never_recollect_or_renew_temperature_sample(self):
        self.hwmon()
        self.sample()
        before = self.service.snapshot()
        calls = self.clock.calls
        with mock.patch.object(self.sources, "snapshot", side_effect=AssertionError("GET must be memory only")):
            for _ in range(3):
                self.assertEqual(self.service.snapshot(), before)
                self.assertFalse(self.service.history("1h")["available"])
        self.assertEqual(self.clock.calls, calls)

    def test_history_disabled_is_empty_but_pending_metric_metadata_is_original(self):
        self.hwmon(0, "nct6775", "System", 42000)
        metric = self.sample()["board_temperature_c"]
        history = self.service.history("1h")
        self.assertFalse(history["available"])
        self.assertFalse(history["recording"])
        self.assertEqual(history["series"], [])
        self.assertFalse((self.state / "monitoring.sqlite3").exists())
        pending = self.service.last_metrics["board_temperature_c"]
        for key in ("sampled_monotonic", "sampled_at", "max_age_seconds", "reason", "value"):
            self.assertEqual(pending[key], metric[key])
        non_temperature = self.service.last_metrics["cpu_usage_percent"]
        self.assertEqual(set(non_temperature), {"value", "label", "unit"})

    def captured_real_source(self):
        self.hwmon()
        first = self.sample()
        rows = copy.deepcopy(self.sources.sysfs)
        self.controlled_sources(rows)
        return first, rows

    def test_real_same_cached_sample_keeps_first_date_across_both_wall_jumps(self):
        self.hwmon()
        first = self.sample()["cpu_temperature_c"]
        self.wall_base += 60
        forward = self.sample()["cpu_temperature_c"]
        self.wall_base -= 120
        backward = self.sample()["cpu_temperature_c"]
        for row in (forward, backward):
            self.assertEqual(row["value"], 55)
            self.assertEqual(row["sampled_monotonic"], first["sampled_monotonic"])
            self.assertEqual(row["sampled_at"], first["sampled_at"])
            self.assertEqual(row["max_age_seconds"], first["max_age_seconds"])
        self.clock.current += 1
        renewed = self.sample()["cpu_temperature_c"]
        self.assertGreater(renewed["sampled_monotonic"], first["sampled_monotonic"])
        self.assertAlmostEqual(renewed["sampled_at"], self.wall_base + renewed["sampled_monotonic"], delta=1e-6)

    def test_first_seen_stale_sample_does_not_invent_a_historical_date(self):
        self.controlled_sources([self.row(sampled=95)])
        row = self.sample()["board_temperature_c"]
        self.assertIsNone(row["value"])
        self.assertEqual(row["reason"], "stale_sample")
        self.assertEqual(row["sampled_monotonic"], 95)
        self.assertIsNone(row["sampled_at"])
        self.assertEqual(self.service._temperature_anchors, {})

    def test_clock_recovery_reuses_existing_anchor_after_wall_correction(self):
        first, rows = self.captured_real_source()
        expected = first["cpu_temperature_c"]["sampled_at"]
        self.clock.current = 90
        rejected = self.sample()["cpu_temperature_c"]
        self.assertEqual(rejected["reason"], "clock_regressed")
        self.assertIsNone(rejected["sampled_at"])
        self.assertIn(rows[0]["id"], self.service._temperature_anchors)
        self.wall_base += 60
        self.clock.current = 100.3
        recovered = self.sample()["cpu_temperature_c"]
        self.assertEqual(recovered["value"], 55)
        self.assertEqual(recovered["sampled_at"], expected)

    def test_future_rejection_does_not_erase_an_existing_legitimate_anchor(self):
        first, rows = self.captured_real_source()
        future = copy.deepcopy(rows)
        future[0]["sampled_monotonic"] += 5
        self.controlled_sources(future)
        rejected = self.sample()["cpu_temperature_c"]
        self.assertEqual(rejected["reason"], "future_sample")
        self.assertIsNone(rejected["sampled_at"])
        self.wall_base += 60
        self.controlled_sources(rows)
        recovered = self.sample()["cpu_temperature_c"]
        self.assertEqual(recovered["value"], 55)
        self.assertEqual(recovered["sampled_at"], first["cpu_temperature_c"]["sampled_at"])

    def test_disappearance_discards_anchor_without_redating_an_old_returned_sample(self):
        _, rows = self.captured_real_source()
        self.controlled_sources([])
        self.sample()
        self.assertEqual(self.service._temperature_anchors, {})
        self.wall_base += 60
        self.controlled_sources(rows)
        returned = self.sample()["cpu_temperature_c"]
        self.assertEqual(returned["value"], 55)
        self.assertIsNone(returned["sampled_at"])
        rows[0]["sampled_monotonic"] = self.clock.current
        self.controlled_sources(rows)
        fresh = self.sample()["cpu_temperature_c"]
        self.assertIsNotNone(fresh["sampled_at"])
        self.assertAlmostEqual(fresh["sampled_at"], self.wall_base + fresh["sampled_monotonic"], delta=1e-6)

    def test_changed_context_does_not_splice_old_source_date_into_same_id(self):
        _, rows = self.captured_real_source()
        rows[0]["display_label"] = "Replacement Package"
        self.wall_base += 60
        self.controlled_sources(rows)
        changed = self.sample()["cpu_temperature_c"]
        self.assertEqual(changed["value"], 55)
        self.assertIsNone(changed["sampled_at"])
        rows[0]["sampled_monotonic"] = self.clock.current
        self.controlled_sources(rows)
        fresh = self.sample()["cpu_temperature_c"]
        self.assertIsNotNone(fresh["sampled_at"])
        self.assertAlmostEqual(fresh["sampled_at"], self.wall_base + fresh["sampled_monotonic"], delta=1e-6)

    def test_missing_stale_threshold_is_not_a_new_source_generation(self):
        source = self.row(kind="disk", role="disk", maximum=30)
        source["critical_celsius"] = 65
        self.controlled_sources([source])
        original = self.sample()["disk_temperature_c"]
        source.update(temperature_c=None, status="unavailable", critical_celsius=None)
        self.controlled_sources([source])
        self.clock.current = 131
        self.wall_base += 60
        stale = self.sample()["disk_temperature_c"]
        self.assertIsNone(stale["value"])
        self.assertEqual(stale["reason"], "stale_sample")
        self.assertEqual(stale["sampled_at"], original["sampled_at"])

    def test_stop_start_clears_private_anchors_without_reanchoring_cached_old_sample(self):
        self.captured_real_source()
        self.assertTrue(self.service._temperature_anchors)
        self.service.stop()
        self.assertEqual(self.service._temperature_anchors, {})
        # Exercise start's actual lifecycle code without creating a sampler
        # thread in this controlled metadata-only fixture.
        thread = mock.Mock()
        thread.is_alive.return_value = False
        with mock.patch.object(monitoring.threading, "Thread", return_value=thread):
            self.service.start()
        self.assertEqual(self.service._temperature_anchors, {})
        self.wall_base += 60
        row = self.sample()["cpu_temperature_c"]
        self.assertEqual(row["value"], 55)
        self.assertIsNone(row["sampled_at"])

    def test_inflight_old_epoch_cannot_repopulate_anchors_after_stop_start(self):
        self.hwmon()
        original = self.sources.snapshot
        thread = mock.Mock()
        thread.is_alive.return_value = False

        def restart():
            rows = original()
            self.service.stop()
            self.service.start()
            return rows

        with mock.patch.object(monitoring.threading, "Thread", return_value=thread), \
                mock.patch.object(self.sources, "snapshot", side_effect=restart):
            metrics = self.sample()
        # Revocation now discards the entire round, a stronger result than
        # publishing a null temperature row for the obsolete generation.
        self.assertEqual(metrics, {})
        self.assertEqual(self.service.last_metrics, {})
        self.assertEqual(self.service._temperature_anchors, {})

    def test_source_anchor_cache_is_bounded_to_current_inventory(self):
        for batch in range(3):
            rows = [self.row("source-%d-%03d" % (batch, i), kind="cpu", role="cpu",
                             sampled=self.clock.current) for i in range(128)]
            self.controlled_sources(rows)
            self.sample()
            self.assertEqual(len(self.service._temperature_anchors), 128)
            self.assertEqual(set(self.service._temperature_anchors), {row["id"] for row in rows})

    def test_source_id_above_interface_name_limit_keeps_original_temperature_contract(self):
        identity = "sensor:" + "a" * 80
        self.controlled_sources([self.row(identity, kind="cpu", role="cpu")])
        metrics = self.sample()
        self.assertIsNotNone(metrics["temperature." + identity]["sampled_at"])
        self.assertIn(identity, self.service._temperature_anchors)

    def test_real_display_metadata_and_critical_are_whitelisted_on_independent_metric(self):
        folder = self.hwmon()
        self.write(folder / "temp1_crit", 100000)
        metrics = self.sample()
        source = self.sources.sysfs[0]
        dynamic = metrics["temperature." + source["id"]]
        for key in ("display_driver", "display_label", "display_channel", "display_source",
                    "display_role", "display_evidence", "critical_celsius"):
            self.assertEqual(dynamic[key], source[key])
        self.assertEqual(dynamic["display_driver"], "coretemp")
        self.assertEqual(dynamic["display_label"], "Package id 0")
        self.assertEqual(dynamic["display_channel"], "temp1")
        self.assertEqual(dynamic["display_source"], "hwmon")
        self.assertEqual(dynamic["critical_celsius"], 100)
        self.assertNotIn("display_driver", metrics["cpu_temperature_c"])

    def test_display_projection_does_not_copy_paths_serials_raw_or_unbounded_critical(self):
        source = self.row(kind="cpu", role="cpu")
        source.update(display_driver="/dev/private", display_label="/drives/private",
                      display_channel="temp1/path", display_source="private/raw", display_evidence="bad\nvalue",
                      private_path="PRIVATE-PATH-MARKER", serial_number="PRIVATE-SERIAL-MARKER",
                      raw={"secret": "PRIVATE-RAW-MARKER"})
        for critical in (True, 39, 151, "100"):
            with self.subTest(critical=critical):
                source["critical_celsius"] = critical
                self.controlled_sources([source])
                dynamic = self.sample()["temperature.fixture"]
                for key in ("display_driver", "display_label", "display_channel", "display_source",
                            "display_evidence", "critical_celsius"):
                    self.assertIsNone(dynamic[key])
                for key in ("private_path", "serial_number", "raw", "kind"):
                    self.assertNotIn(key, dynamic)

    def test_thermal_source_preserves_existing_display_label_channel_and_driver(self):
        folder = self.sys / "class/thermal/thermal_zone0"
        self.write(folder / "type", "x86_pkg_temp")
        self.write(folder / "temp", 61000)
        metrics = self.sample()
        dynamic = next(row for key, row in metrics.items() if key.startswith("temperature.thermal:"))
        self.assertEqual((dynamic["display_driver"], dynamic["display_label"],
                          dynamic["display_channel"], dynamic["display_source"]),
                         ("thermal", "x86_pkg_temp", "thermal_zone0", "thermal"))
        self.assertEqual(dynamic["display_role"], "cpu")

    def late_lifecycle_change(self, stage, restart):
        self.hwmon(value=99000)
        before = self.service.snapshot()
        original = getattr(self.service, stage)
        thread = mock.Mock()
        thread.is_alive.return_value = False

        def revoke_after_source_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.service.stop()
            if restart:
                self.service.start()
            return result

        with mock.patch.object(monitoring.threading, "Thread", return_value=thread), \
                mock.patch.object(self.service, stage, side_effect=revoke_after_source_validation), \
                mock.patch.object(self.service, "_alerts", wraps=self.service._alerts) as alerts:
            self.sample()
        self.assertEqual(self.service.snapshot(), before)
        self.assertEqual(self.service.last_metrics, {})
        self.assertIsNone(self.service.last_wall)
        self.assertEqual(self.service.active, {})
        self.assertEqual(list(self.service.events), [])
        self.assertEqual(list(self.service.pending_events), [])
        self.assertEqual(self.service._temperature_anchors, {})
        alerts.assert_not_called()

    def test_restart_after_power_discards_metrics_alerts_and_history_input(self):
        self.late_lifecycle_change("_power", restart=True)

    def test_restart_after_disk_io_discards_metrics_alerts_and_history_input(self):
        self.late_lifecycle_change("_disk_io", restart=True)

    def test_stop_without_restart_after_power_discards_the_old_sample(self):
        self.late_lifecycle_change("_power", restart=False)

    def test_final_alerts_and_publication_are_atomic_with_concurrent_stop(self):
        self.hwmon(value=99000)
        entered, release, stop_attempted = threading.Event(), threading.Event(), threading.Event()
        stopped = threading.Event()
        real_lock = self.service.lock
        errors = []

        class LockProbe:
            # Preserve the real RLock; expose the exact competing acquisition
            # instead of relying on sleeps to guess whether stop has run.
            def __enter__(inner):
                if threading.current_thread().name == "temperature-stop-test":
                    stop_attempted.set()
                real_lock.acquire()
                return inner

            def __exit__(inner, *unused):
                real_lock.release()

        self.service.lock = LockProbe()
        original = self.service._alerts

        def paused_alerts(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release publication gate")
            return original(*args)

        def run_sample():
            try:
                self.sample()
            except BaseException as error:
                errors.append(error)

        def run_stop():
            try:
                self.service.stop()
            except BaseException as error:
                errors.append(error)
            finally:
                stopped.set()

        worker = threading.Thread(target=run_sample, name="temperature-sample-test")
        stopper = threading.Thread(target=run_stop, name="temperature-stop-test")
        with mock.patch.object(self.service, "_alerts", side_effect=paused_alerts):
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                stopper.start()
                self.assertTrue(stop_attempted.wait(3))
                # stop cannot change either Event or epoch between the final
                # check and the alert/current/last_metrics publication.
                self.assertFalse(self.service.stopped.is_set())
                self.assertFalse(stopped.is_set())
            finally:
                release.set()
                worker.join(3)
                if stopper.ident is not None:
                    stopper.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.service.stopped.is_set())
        self.assertEqual(self.service._temperature_anchors, {})
        # This round linearized before stop acquired the publication lock;
        # its original date is legitimate, rather than a post-revocation write.
        metric = next(row for row in self.service.snapshot()["current"]["metrics"]
                      if row["id"] == "cpu_temperature_c")
        self.assertEqual(metric["value"], 99)
        self.assertIsNotNone(metric["sampled_at"])


if __name__ == "__main__":
    unittest.main()
