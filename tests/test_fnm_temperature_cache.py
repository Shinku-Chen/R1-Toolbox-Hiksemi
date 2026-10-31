"""Temperature display consumes the same dated cache as recorded history.

Unit cases control only cache metadata. The integration cases use real
TemperatureSources, MonitoringService, SQLite history and FnmonitorPanel over
temporary sys/proc files; they never access a host sensor, SMART or network.
"""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))


def load_source(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "runtime" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = load_source("fnm_temperature_cache_compat", "fnm_compat.py")
monitoring = load_source("fnm_temperature_cache_monitoring", "monitoring.py")
source_module = load_source("fnm_temperature_cache_sources", "temperature_sources.py")


class Snapshot:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return copy.deepcopy(self.value)


class CachedMonitoring(Snapshot):
    def __init__(self, value):
        super().__init__(value)
        self.recorded = {"available": False, "series": []}

    def history(self, _span):
        return copy.deepcopy(self.recorded)


class FnmTemperatureCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r1-fnm-temperature-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc, self.sys = self.root / "proc", self.root / "sys"
        for path in (self.proc, self.sys, self.root / "dev", self.root / "udev"):
            path.mkdir()
        self.now, self.wall_base = 100.0, 1_700_000_000.0
        self.controller = Snapshot({"settings": {}, "capabilities": {}, "screen": {}, "rgb": {}})
        self.basic = Snapshot({"sampled_at": self.iso(), "cpu": {"temp_c": 99}, "disks": []})
        self.insights = Snapshot({
            "sampled_at": self.iso(), "disks": {"items": []}, "interfaces": [],
            "system": {"memory": {}},
            "device": {"sampled_at": self.iso(), "thermal": [
                {"driver": "acpitz", "label": "System", "celsius": 98,
                 "channel": "thermal_zone0", "source": "thermal"}]}})
        self.cached = CachedMonitoring({"current": {"sampled_at": self.wall(),
            "sampled_monotonic": self.now, "metrics": [self.metric("cpu", 55), self.metric("board", 42)]}})
        # With Monitoring present, independent points are dated dynamic
        # metrics too. A device-only row must never become a hidden fallback.
        independent = self.metric("board", 98)
        independent.update(id="temperature.thermal:fixture-acpi", label="acpitz", display_driver="thermal",
                           display_label="acpitz", display_channel="thermal_zone0", display_source="thermal",
                           display_role="unknown", display_evidence="unclassified")
        self.cached.value["current"]["metrics"].append(independent)
        self.panel = self.make_panel(self.cached)
        self.write(self.proc / "self/mountinfo", "")
        self.write(self.proc / "diskstats", "")
        self.write(self.proc / "sys/kernel/random/boot_id", "01234567-89ab-cdef-0123-456789abcdef")

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="ascii")

    def wall(self):
        return self.wall_base + self.now

    def iso(self, age=0):
        return dt.datetime.fromtimestamp(self.wall() - age, dt.timezone.utc).isoformat()

    def metric(self, kind, value, *, sampled=None, maximum=3, reason=None):
        sampled = self.now if sampled is None else sampled
        return {"id": "cpu_temperature_c" if kind == "cpu" else "board_temperature_c",
                "value": value, "sampled_monotonic": sampled,
                "max_age_seconds": maximum, "sampled_at": self.wall_base + sampled,
                "reason": reason}

    def make_panel(self, service):
        return compat.FnmonitorPanel(
            self.controller, self.insights, service, None, None, self.basic,
            state_dir=self.root / "panel", clock=lambda: self.now,
            wall_clock=self.wall, proc_root=self.proc, sys_root=self.sys,
            dev_root=self.root / "dev", udev_root=self.root / "udev",
            weather_fetcher=lambda _: {"ok": False})

    def temperatures(self):
        return self.panel.overview()["temp"]

    def assert_unknown(self, temp, kind):
        field = "cpu" if kind == "cpu" else "system"
        self.assertIsNone(temp[field])
        self.assertFalse(temp["samples"][kind]["fresh"])
        self.assertIsNotNone(temp["samples"][kind]["reason"])

    def test_monitoring_is_the_only_realtime_source_and_keeps_legacy_fields(self):
        temp = self.temperatures()
        self.assertEqual(temp["source"], "monitoring_temperature_cache")
        self.assertEqual((temp["cpu"], temp["system"], temp["mb"], temp["disks"]), (55, 42, 42, []))
        for kind in ("cpu", "board"):
            self.assertEqual(temp["samples"][kind], {
                "sampled_at": self.wall(), "sampled_monotonic": self.now,
                "max_age_seconds": 3, "fresh": True, "reason": None})

    def test_explicit_unknown_does_not_fall_back_to_basic_or_acpi(self):
        for row in self.cached.value["current"]["metrics"]:
            row.update(value=None, reason="unavailable")
        temp = self.temperatures()
        self.assert_unknown(temp, "cpu")
        self.assert_unknown(temp, "board")
        self.assertEqual(self.basic.value["cpu"]["temp_c"], 99)
        self.assertEqual(self.insights.value["device"]["thermal"][0]["celsius"], 98)

    def test_missing_and_duplicate_metrics_are_unknown_independently(self):
        original = copy.deepcopy(self.cached.value["current"]["metrics"])
        for rows in (original[1:], original + [copy.deepcopy(original[0])]):
            with self.subTest(count=len(rows)):
                self.cached.value["current"]["metrics"] = rows
                temp = self.temperatures()
                self.assert_unknown(temp, "cpu")
                self.assertEqual(temp["system"], 42)

    def test_repeated_gets_keep_original_measurement_date_and_expire(self):
        before = self.temperatures()
        for elapsed in (0.2, 0.9, 2.9, 3.0):
            self.now = 100 + elapsed
            temp = self.temperatures()
            self.assertEqual((temp["cpu"], temp["system"]), (55, 42))
            self.assertEqual(temp["samples"], before["samples"])
        self.now = 103.01
        expired = self.temperatures()
        self.assert_unknown(expired, "cpu")
        self.assert_unknown(expired, "board")
        self.assertEqual(expired["samples"]["cpu"]["sampled_at"], before["samples"]["cpu"]["sampled_at"])
        self.assertEqual(expired["samples"]["cpu"]["sampled_monotonic"], 100)

    def test_future_source_or_evaluation_clock_never_becomes_current(self):
        for field, value in (("sampled_monotonic", 100.01),):
            with self.subTest(field=field):
                self.cached.value["current"]["metrics"][0][field] = value
                self.assert_unknown(self.temperatures(), "cpu")
        self.cached.value["current"]["metrics"][0] = self.metric("cpu", 55)
        self.cached.value["current"]["sampled_monotonic"] = 101
        temp = self.temperatures()
        self.assert_unknown(temp, "cpu")
        self.assert_unknown(temp, "board")

    def test_clock_rollback_is_not_a_new_temperature_sample(self):
        original = self.temperatures()["samples"]
        self.now = 99
        temp = self.temperatures()
        self.assert_unknown(temp, "cpu")
        self.assert_unknown(temp, "board")
        self.assertEqual(temp["samples"]["cpu"]["sampled_at"], original["cpu"]["sampled_at"])

    def test_concurrent_gets_cannot_lower_the_temperature_clock_high_water(self):
        entered, release_earlier = threading.Event(), threading.Event()
        later_phase, later_done = threading.Event(), threading.Event()
        cache = {"current": {"sampled_monotonic": 99.5, "metrics": [
            self.metric("cpu", 55, sampled=99), self.metric("board", 42, sampled=99)]}}

        class ObservedLock:
            def __init__(self):
                self.lock, self.owner = threading.Lock(), None

            def __enter__(self):
                if threading.current_thread().name == "fnm-later":
                    later_phase.set()  # The later GET has attempted acquisition.
                self.lock.acquire()
                self.owner = threading.get_ident()
                return self

            def __exit__(self, *_):
                self.owner = None
                self.lock.release()

        class InterleavedPanel(compat.FnmonitorPanel):
            def __setattr__(self, name, value):
                if name == "_temperature_read_clock" and threading.current_thread().name == "fnm-earlier":
                    entered.set()  # Earlier GET has read previous, before store.
                    if not release_earlier.wait(2):
                        raise AssertionError("bounded earlier GET release timed out")
                super().__setattr__(name, value)

            def _monitoring(self):
                return copy.deepcopy(cache)

        # Only snapshots and the scheduling boundary are controlled. Both
        # threads execute the actual production _temperature method. Without
        # its lock the later GET completes first, then the old store loses 101.
        panel = object.__new__(InterleavedPanel)
        panel.monitoring = object()
        panel._temperature_read_clock = None
        panel._temperature_clock_lock = observed = ObservedLock()
        clock_reads = []

        def clock():
            name = threading.current_thread().name
            clock_reads.append(observed.owner == threading.get_ident())
            return {"fnm-earlier": 100.0, "fnm-later": 101.0}.get(name, 100.5)

        panel.clock = clock
        results, errors = {}, []

        def call(name):
            try:
                results[name] = panel._temperature([])
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                if name == "later":
                    later_done.set()
                    later_phase.set()

        threads = []
        try:
            earlier = threading.Thread(target=call, args=("earlier",), name="fnm-earlier")
            earlier.start()
            threads.append(earlier)
            self.assertTrue(entered.wait(2), "earlier GET reached its actual high-water store")
            later = threading.Thread(target=call, args=("later",), name="fnm-later")
            later.start()
            threads.append(later)
            self.assertTrue(later_phase.wait(2), "later GET attempted the shared critical section")
            self.assertFalse(later_done.is_set(), "later GET waits while the earlier GET holds the lock")
        finally:
            release_earlier.set()
            for thread in threads:
                thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual((results["earlier"]["cpu"], results["later"]["cpu"]), (55, 55))
        self.assertEqual(panel._temperature_read_clock, 101)
        rollback = panel._temperature([])
        self.assert_unknown(rollback, "cpu")
        self.assert_unknown(rollback, "board")
        self.assertEqual(rollback["samples"]["cpu"]["reason"], "clock_regressed")
        self.assertEqual(panel._temperature_read_clock, 101)
        self.assertTrue(all(clock_reads), "clock read and high-water update share the same lock")

    def test_invalid_value_or_missing_measurement_metadata_never_renews(self):
        metric = self.metric("cpu", 55)
        mutations = [("value", value) for value in (True, "55", float("nan"), float("inf"), -41, 151)]
        mutations += [(key, value) for key in ("sampled_monotonic", "sampled_at", "max_age_seconds")
                      for value in (None, True, "100", float("nan"), float("inf"))]
        mutations += [("max_age_seconds", -1)]
        for key, value in mutations:
            with self.subTest(field=key, value=value):
                candidate = copy.deepcopy(metric)
                candidate[key] = value
                self.cached.value["current"]["metrics"][0] = candidate
                self.assert_unknown(self.temperatures(), "cpu")
        for key in ("sampled_monotonic", "sampled_at", "max_age_seconds"):
            with self.subTest(missing=key):
                candidate = copy.deepcopy(metric)
                candidate.pop(key)
                self.cached.value["current"]["metrics"][0] = candidate
                self.assert_unknown(self.temperatures(), "cpu")

    def test_missing_or_failed_monitoring_snapshot_never_activates_legacy_fallback(self):
        self.cached.value = {}
        self.assert_unknown(self.temperatures(), "cpu")
        with mock.patch.object(self.cached, "snapshot", side_effect=RuntimeError("fixture unavailable")):
            self.assert_unknown(self.temperatures(), "cpu")

    def test_history_mapping_uses_board_metric_and_preserves_unknown_points(self):
        self.cached.recorded = {"series": [
            {"id": "cpu_temperature_c", "points": [[100, 55], [130, None]]},
            {"id": "board_temperature_c", "points": [[100, 42], [130, None]]},
            {"id": "temperature.unknown_acpi", "points": [[100, 98]]}]}
        self.assertEqual(self.panel.history("temp", "1h")["points"], [[100, 55], [130, None]])
        self.assertEqual(self.panel.history("temp_mb", "1h")["points"], [[100, 42], [130, None]])

    def test_without_monitoring_only_dated_legacy_measurements_are_visible(self):
        self.panel = self.make_panel(None)
        self.basic.value["cpu"]["temp_c"] = 55
        self.insights.value["device"]["thermal"] = [
            {"driver": "nct6775", "label": "System", "celsius": 42,
             "source": "hwmon", "channel": "temp1"},
            {"driver": "acpitz", "label": "System", "celsius": 98,
             "source": "thermal", "channel": "thermal_zone0"}]
        for age in (0, 2.9, 3):
            with self.subTest(age=age):
                self.basic.value["sampled_at"] = self.iso(age)
                self.insights.value["device"]["sampled_at"] = self.iso(age)
                temp = self.temperatures()
                self.assertEqual((temp["cpu"], temp["system"], temp["mb"]), (55, 42, 42))
                self.assertAlmostEqual(temp["samples"]["cpu"]["sampled_at"], self.wall() - age)

    def test_legacy_missing_stale_and_future_dates_are_not_get_timestamps(self):
        self.panel = self.make_panel(None)
        self.insights.value["device"]["thermal"] = []
        for timestamp in (None, "not-a-date", self.iso(3.01), self.iso(-0.1)):
            with self.subTest(timestamp=timestamp):
                self.basic.value["sampled_at"] = timestamp
                temp = self.temperatures()
                self.assert_unknown(temp, "cpu")
                if timestamp in (self.iso(3.01), self.iso(-0.1)):
                    self.assertEqual(temp["samples"]["cpu"]["sampled_at"], compat._stamp(timestamp))
        self.basic.value.pop("sampled_at", None)
        self.assert_unknown(self.temperatures(), "cpu")

    def test_legacy_rejected_out_of_range_sensor_keeps_date_without_reusing_value(self):
        self.panel = self.make_panel(None)
        original = self.iso(1)
        self.basic.value.update(sampled_at=original, cpu={"temp_c": None})
        self.insights.value["device"]["sampled_at"] = original
        for role, driver, label in (("board", "nct6775", "System"),
                                    ("cpu", "coretemp", "Package id 0")):
            for value in (200, -50):
                with self.subTest(role=role, celsius=value):
                    # The independent sensor catalog's broader display range
                    # must not re-enter the rejected legacy aggregate through
                    # its old-row/date explanation path.
                    self.insights.value["device"]["thermal"] = [
                        {"driver": driver, "label": label, "celsius": value,
                         "source": "hwmon", "channel": "temp1"}]
                    temp = self.temperatures()
                    self.assert_unknown(temp, role)
                    self.assertEqual(temp["samples"][role]["sampled_at"], compat._stamp(original))
                    if role == "board":
                        self.assertIsNone(temp["mb"])

    def test_sensor_catalog_retains_cpu_thermal_and_unknown_zones_without_board_inference(self):
        device = self.insights.value["device"]
        device["thermal"] = [
            {"driver": "coretemp", "label": "Package id 0", "celsius": 55, "channel": "temp1", "source": "hwmon"},
            {"driver": "cpu_thermal", "label": "cpu_thermal", "celsius": 56, "channel": "thermal_zone0", "source": "thermal"},
            {"driver": "acpitz", "label": "System", "celsius": 97, "channel": "thermal_zone1", "source": "thermal"},
            {"driver": "INT3400 Thermal", "label": "System", "celsius": 96, "channel": "thermal_zone2", "source": "thermal"},
            {"driver": "pch_cannonlake", "label": "System", "celsius": 95, "channel": "temp1", "source": "hwmon"}]
        self.hwmon(0, "coretemp", "Package id 0", 55000)
        self.hwmon(1, "pch_cannonlake", "System", 95000)
        for index, driver, value in ((0, "cpu_thermal", 56000), (1, "acpitz", 97000),
                                     (2, "INT3400 Thermal", 96000)):
            self.write(self.sys / "class/thermal" / ("thermal_zone" + str(index)) / "type", driver)
            self.write(self.sys / "class/thermal" / ("thermal_zone" + str(index)) / "temp", value)
        service, _sources = self.real_service()
        service.sample_once()
        # Keep the original independent aggregate fixture (CPU55/board42),
        # while obtaining classification metadata from real production
        # sources/monitoring rather than hand-authoring their role evidence.
        dynamic = [row for row in service.snapshot()["current"]["metrics"]
                   if row["id"].startswith("temperature.")]
        self.cached.value["current"]["metrics"] = [self.metric("cpu", 55), self.metric("board", 42), *dynamic]
        self.panel = self.make_panel(self.cached)
        rows = self.panel.sensors()["temps"]
        self.assertEqual(len(rows), 5)
        roles = {row["label"]: row["display_role"] for row in rows}
        self.assertEqual(roles["Package id 0"], "cpu")
        self.assertEqual(roles["cpu_thermal"], "cpu")
        self.assertTrue(all(roles[name] == "unknown" for name in ("acpitz", "INT3400 Thermal", "System")))
        self.assertTrue(all(row["driver"] == "thermal" for row in rows
                            if row["label"] in ("cpu_thermal", "acpitz", "INT3400 Thermal")))
        self.assertTrue(all(row["display_evidence"] for row in rows))
        self.assertEqual(self.temperatures()["system"], 42)

    def test_independent_sensor_rows_keep_dates_when_the_catalog_expires(self):
        before = self.panel.sensors()["temps"][0]
        self.now += 3.01
        after = self.panel.sensors()["temps"][0]
        self.assertIsNone(after["value"])
        self.assertIsNone(after["temp"])
        self.assertFalse(after["fresh"])
        self.assertEqual(after["sampled_at"], before["sampled_at"])

    def hwmon(self, index, driver, label, value):
        folder = self.sys / "class/hwmon" / ("hwmon" + str(index))
        self.write(folder / "name", driver)
        self.write(folder / "temp1_input", value)
        self.write(folder / "temp1_label", label)
        return folder

    def real_service(self, *, history=False):
        sources = source_module.TemperatureSources(self.insights, self.sys, clock=lambda: self.now)
        service = monitoring.MonitoringService(
            self.root / "monitoring", self.insights, self.basic, sources,
            proc_root=self.proc, sys_root=self.sys, clock=lambda: self.now,
            wall_clock=self.wall, timezone=dt.timezone.utc)
        service.settings["history_enabled"] = history
        self.addCleanup(service.stop)
        if sys.platform == "win32":
            # Windows reports inherited write bits for a private TEMP folder.
            # Only this POSIX mode proof is a platform seam; the production
            # SQLite open/schema/flush/history chain still runs unchanged.
            service._prepare_directory = lambda: service.state_dir.mkdir(
                mode=0o700, parents=True, exist_ok=True)
        patch = mock.patch.object(monitoring.os, "O_NONBLOCK", getattr(monitoring.os, "O_NONBLOCK", 0), create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.panel = self.make_panel(service)
        return service, sources

    def test_real_sources_monitoring_overview_and_sqlite_history_use_identical_values(self):
        if monitoring.sqlite3 is None:
            self.skipTest("fixture Python has no sqlite3")
        cpu = self.hwmon(0, "coretemp", "Package id 0", 55000)
        board = self.hwmon(1, "nct6775", "System", 42000)
        self.hwmon(2, "opaque_sensor", "temp1", 99000)
        service, _sources = self.real_service(history=True)
        service.sample_once()
        first = self.temperatures()
        self.assertEqual((first["cpu"], first["system"]), (55, 42))
        self.assertEqual(self.panel.history("temp", "1h")["points"][-1], [int(self.wall()), 55])
        self.assertEqual(self.panel.history("temp_mb", "1h")["points"][-1], [int(self.wall()), 42])
        self.now += 35
        self.write(cpu / "temp1_input", 64000)
        self.write(board / "temp1_input", 46000)
        service.sample_once()
        current = self.temperatures()
        metrics = {row["id"]: row for row in service.snapshot()["current"]["metrics"]}
        for kind, identifier, display, history in (("cpu", "cpu_temperature_c", "cpu", "temp"),
                                                  ("board", "board_temperature_c", "system", "temp_mb")):
            self.assertEqual(current[display], metrics[identifier]["value"])
            self.assertEqual(current["samples"][kind]["sampled_at"], metrics[identifier]["sampled_at"])
            self.assertEqual(self.panel.history(history, "1h")["points"][-1], [int(self.wall()), current[display]])
        self.assertTrue((self.root / "monitoring/monitoring.sqlite3").exists())

    def test_real_one_second_source_cache_has_no_null_holes_and_gets_never_sample(self):
        self.hwmon(0, "coretemp", "Package id 0", 55000)
        self.hwmon(1, "nct6775", "System", 42000)
        service, sources = self.real_service()
        observed = []
        with mock.patch.object(sources, "_read_sysfs", wraps=sources._read_sysfs) as reader:
            for _ in range(9):
                service.sample_once()
                observed.append(self.temperatures())
                self.now += 0.25
            self.assertGreaterEqual(reader.call_count, 2)
            self.assertLess(reader.call_count, 9)
        self.assertTrue(all((row["cpu"], row["system"]) == (55, 42) for row in observed))
        before = self.temperatures()["samples"]
        with mock.patch.object(sources, "snapshot", side_effect=AssertionError("GET must not sample")):
            for _ in range(3):
                self.assertEqual(self.temperatures()["samples"], before)

    def test_fast_dynamic_catalog_stays_current_when_device_inventory_is_slow(self):
        cpu = self.hwmon(0, "coretemp", "Package id 0", 55000)
        board = self.hwmon(1, "nct6775", "System", 42000)
        self.write(cpu / "temp1_crit", 100000)
        self.write(board / "temp1_crit", 90000)
        self.hwmon(2, "opaque_sensor", "temp1", 97000)
        service, sources = self.real_service()
        inventory_stamp = self.insights.value["device"]["sampled_at"]
        for sampled in (100, 102, 104):
            self.now = sampled
            service.sample_once()
        self.assertEqual(self.insights.value["device"]["sampled_at"], inventory_stamp)
        temp = self.temperatures()
        rows = self.panel.sensors()["temps"]
        by_driver = {row["driver"]: row for row in rows}
        self.assertEqual((temp["cpu"], temp["system"]), (55, 42))
        self.assertTrue(all(temp["samples"][kind]["fresh"] for kind in ("cpu", "board")))
        for driver, role, value, label in (("coretemp", "cpu", 55, "Package id 0"),
                                           ("nct6775", "board", 42, "System")):
            row = by_driver[driver]
            self.assertEqual((row["value"], row["temp"], row["display_role"]), (value, value, role))
            self.assertEqual(row["name"], label + " · " + driver)
            self.assertTrue(row["fresh"])
            self.assertEqual(row["sampled_at"], temp["samples"][role]["sampled_at"])
            self.assertEqual(row["crit"], 100 if role == "cpu" else 90)
        self.assertEqual((by_driver["opaque_sensor"]["value"], by_driver["opaque_sensor"]["display_role"]),
                         (97, "unknown"))
        before_dates = {row["driver"]: row["sampled_at"] for row in rows}
        with mock.patch.object(sources, "snapshot", side_effect=AssertionError("GET must not sample TS")):
            self.assertEqual(self.panel.sensors()["temps"], rows)
            self.assertEqual(self.temperatures()["samples"], temp["samples"])
            # Fresh slow inventory cannot revive an expired fast dynamic row.
            self.now = 107.01
            self.insights.value["device"].update(sampled_at=self.iso(), thermal=[
                {"driver": "coretemp", "label": "Package id 0", "celsius": 99, "source": "hwmon"},
                {"driver": "nct6775", "label": "System", "celsius": 98, "source": "hwmon"}])
            expired = self.temperatures()
            self.assert_unknown(expired, "cpu")
            self.assert_unknown(expired, "board")
            expired_rows = self.panel.sensors()["temps"]
            self.assertEqual(len(expired_rows), 3)
            self.assertTrue(all(row["value"] is None and row["temp"] is None and not row["fresh"]
                                for row in expired_rows))
            self.assertEqual({row["driver"]: row["sampled_at"] for row in expired_rows}, before_dates)

    def test_same_source_sample_keeps_first_wall_anchor_across_wall_clock_jumps(self):
        self.hwmon(0, "coretemp", "Package id 0", 55000)
        self.hwmon(1, "nct6775", "System", 42000)
        service, sources = self.real_service()
        with mock.patch.object(sources, "_read_sysfs", wraps=sources._read_sysfs) as reader:
            service.sample_once()
            original = {row["id"]: row for row in service.snapshot()["current"]["metrics"]
                        if row["id"] in ("cpu_temperature_c", "board_temperature_c")}
            for elapsed, wall_jump in ((0.25, 60), (0.5, -120)):
                self.now = 100 + elapsed
                self.wall_base += wall_jump
                service.sample_once()
                current = {row["id"]: row for row in service.snapshot()["current"]["metrics"]}
                projected = self.temperatures()
                for kind, identifier, field in (("cpu", "cpu_temperature_c", "cpu"),
                                                ("board", "board_temperature_c", "system")):
                    self.assertEqual(current[identifier]["sampled_monotonic"], 100)
                    self.assertEqual(current[identifier]["sampled_at"], original[identifier]["sampled_at"])
                    self.assertEqual(projected["samples"][kind]["sampled_at"], original[identifier]["sampled_at"])
                    self.assertEqual(projected[field], original[identifier]["value"])
            self.assertEqual(reader.call_count, 1, "wall changes do not create a new source sample")

    def test_real_no_sensor_and_history_disabled_remain_empty(self):
        self.insights.value["device"]["thermal"] = []
        service, _sources = self.real_service()
        service.sample_once()
        temp = self.temperatures()
        self.assert_unknown(temp, "cpu")
        self.assert_unknown(temp, "board")
        self.assertEqual(self.panel.sensors()["temps"], [])
        for field in ("temp", "temp_mb"):
            self.assertEqual(self.panel.history(field, "1h")["points"], [])
        self.assertFalse((self.root / "monitoring/monitoring.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
