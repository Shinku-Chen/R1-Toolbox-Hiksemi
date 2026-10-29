"""Passive fan evidence tests; no real process, device, RPC or ioctl is used."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
try:
    spec = importlib.util.spec_from_file_location("fan_diagnostics", ROOT / "runtime/fan_diagnostics.py")
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
finally:
    sys.path.pop(0)


class ForbiddenLock:
    def acquire(self, *args, **kwargs):
        raise AssertionError("diagnostics acquired the adapter control lock")


class FanDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # Preserve fixtures for failure inspection, matching the repository's
        # no-permanent-delete convention; CI removes its disposable runner.
        self.root = Path(tempfile.mkdtemp(prefix="r1-fan-diagnostics-"))
        self.program = self.root / "histor/bin/temp_control"
        self.guard = self.root / "runtime/fan_guard.so"
        self.runtime = self.root / "run/fan-control"
        for directory in (self.program.parent, self.guard.parent, self.root / "proc", self.runtime,
                          self.root / "sys/module/hikio/notes", self.root / "dev",
                          self.root / "tmp/nand_config", self.root / "var/tmp"):
            directory.mkdir(parents=True, exist_ok=True)
        self.program.write_bytes(b"fixture vendor binary")
        guard = bytearray(64)
        guard[:6], guard[16:20] = b"\x7fELF\x02\x01", b"\x03\x00\x3e\x00"
        self.guard.write_bytes(guard)
        (self.root / "sys/module/hikio/notes/.note.gnu.build-id").write_bytes(diagnostics.DRIVER_NOTE)
        (self.root / "tmp/nand_config/config.txt").write_bytes(b'{"fan":{"fan_mode":5},"private":"secret"}\0')
        (self.root / "dev/hikio").write_bytes(b"not a device; must never be opened")
        self.links = {}
        self.add_process(20)
        self.folder = self.runtime / "fan-fixture"
        self.folder.mkdir()
        self.write_json(self.folder / "status.json", {"phase": "armed", "reason": None,
                         "watchdog": {"pid": 10, "starttime": 100, "exe": "/python", "env": "secret"},
                         "env": "secret"})
        self.write_json(self.folder / "descriptor.json", {"env": {"PRIVATE_TOKEN": "secret"}})
        (self.folder / "lease.bin").write_bytes(b"secret")
        self.adapter = SimpleNamespace(root=self.root, runtime_dir=self.runtime, guard_path=self.guard,
                                       folder=self.folder, lock=ForbiddenLock(), allow_writes=True)
        self.policy = SimpleNamespace(adapter=self.adapter, lock=threading.RLock(), busy=False,
                                      config={"schema_version": 2, "channels": {
                                          "cpu": {"mode": "manual", "manual_percent": 70,
                                                  "low_temp": 55, "high_temp": 70},
                                          "system": {"mode": "vendor"}}},
                                      channels={"cpu": {"active": False, "mode": "vendor"},
                                                "system": {"active": False, "mode": "vendor"}},
                                      last_error=None, settings_error=None, active_on_boot=False)
        self.collector = diagnostics.FanDiagnostics(self.policy, version="fixture-sha")
        self.addCleanup(self.collector.stop)
        self.platform_patch = patch.object(diagnostics.sys, "platform", "linux")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.link_patch = patch.object(diagnostics.os, "readlink", side_effect=self.readlink)
        self.link_patch.start()
        self.addCleanup(self.link_patch.stop)

    def readlink(self, path):
        try:
            return self.links[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def add_process(self, pid, caught="0000000100000e20", blocked="0000000000000000", exe=None):
        base = self.root / "proc" / str(pid)
        base.mkdir(exist_ok=True)
        (base / "stat").write_text(f"{pid} (temp_control) S " + "0 " * 18 + "200 0", encoding="ascii")
        status = "Name:\ttemp_control\nState:\tS (sleeping)\n"
        if caught is not None:
            status += "SigCgt:\t" + caught + "\n"
        if blocked is not None:
            status += "SigBlk:\t" + blocked + "\n"
        (base / "status").write_text(status, encoding="ascii")
        (base / "comm").write_bytes(b"temp_control\n")
        (base / "maps").write_text("1-2 r-xp 0000 00:00 0 " + str(self.guard.resolve()) + "\n", encoding="utf-8")
        (base / "environ").write_bytes(b"PRIVATE_TOKEN=secret\0")
        self.links[str(base / "exe")] = str(exe or self.program.resolve())
        return base

    def wait_result(self, job, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.collector.result(job["id"])
            if result["state"] != "running":
                return result
            time.sleep(0.01)
        self.fail("finite diagnostics capture did not finish")

    def test_snapshot_is_complete_and_has_no_control_side_effects_or_private_files(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("diagnostics invoked control")
        self.adapter.capabilities = self.adapter.snapshot = self.adapter._backend = forbidden
        self.policy.snapshot = self.policy.action = forbidden
        with patch.object(diagnostics, "_read_file", wraps=diagnostics._read_file) as reads, \
                patch("subprocess.Popen", side_effect=forbidden), patch("os.kill", side_effect=forbidden):
            value = self.collector.snapshot()
        self.assertTrue(value["available"])
        self.assertEqual(value["version"], "fixture-sha")
        self.assertEqual(value["vendor_mode"], {"code": 5, "mode": "auto"})
        process = value["processes"]["items"][0]
        self.assertEqual(process["starttime"], 200)
        self.assertEqual(process["sigcgt"], "0000000100000e20")
        self.assertEqual(process["required"], "0xa00")
        self.assertTrue(process["caught_required"])
        self.assertFalse(process["blocked_required"])
        self.assertTrue(process["guard_exact_match"])
        self.assertTrue(process["maps_complete"])
        self.assertTrue(value["processes"]["unique_verified"])
        self.assertEqual(set(value["preflight"]["checks"]), {"linux_root", "writes_enabled", "systemd_run", "driver",
                         "firmware", "character_device", "guard", "unique_vendor", "vendor_mode", "unix_socket"})
        self.assertEqual(value["preflight"]["checks"]["firmware"]["actual_sha256"],
                         hashlib.sha256(self.program.read_bytes()).hexdigest())
        self.assertFalse(value["preflight"]["checks"]["character_device"]["ok"])
        self.assertNotIn("secret", json.dumps(value))
        opened = [Path(call.args[0]).name for call in reads.call_args_list]
        for name in ("descriptor.json", "lease.bin", "environ", "cmdline", "hikio", "emhistor"):
            self.assertNotIn(name, opened)

    def test_reads_proc_maps_through_multiple_short_reads(self):
        base = self.root / "proc/20"
        original = diagnostics.os.read
        (base / "maps").write_bytes(b"1-2 r-xp 0000 00:00 0 /unrelated\n" * 300
                                    + ("3-4 r-xp 0000 00:00 0 " + str(self.guard.resolve()) + "\n").encode())
        with patch.object(diagnostics.os, "read", side_effect=lambda fd, size: original(fd, min(size, 127))):
            process = self.collector.snapshot()["processes"]["items"][0]
        self.assertTrue(process["guard_exact_match"])
        self.assertTrue(process["maps_complete"])
        self.assertGreater(process["maps_bytes_read"], 8192)

    def test_truncated_maps_do_not_claim_guard_absence(self):
        (self.root / "proc/20/maps").write_bytes(b"x" * (1024 * 1024 + 1))
        process = self.collector.snapshot()["processes"]["items"][0]
        self.assertTrue(process["maps_truncated"])
        self.assertFalse(process["maps_complete"])
        self.assertIsNone(process["guard_exact_match"])

    def test_missing_or_invalid_signals_remain_unknown_and_competitor_is_visible(self):
        self.add_process(20, caught=None, blocked="garbage")
        self.add_process(21, exe=self.root / "unknown/temp_control")
        value = self.collector.snapshot()
        self.assertEqual(value["processes"]["count"], 2)
        self.assertFalse(value["processes"]["unique_verified"])
        process = value["processes"]["items"][0]
        self.assertIsNone(process["sigcgt"])
        self.assertIsNone(process["caught_required"])
        self.assertIsNone(process["blocked_required"])
        self.assertFalse(value["processes"]["items"][1]["verified_exe"])

    def test_process_scheduler_state_change_is_not_identity_change_but_pid_reuse_is(self):
        original = self.collector._identity
        def changing_state(base):
            item = original(base)
            changing_state.count += 1
            item["state"] = "S" if changing_state.count % 2 else "R"
            return item
        changing_state.count = 0
        with patch.object(self.collector, "_identity", side_effect=changing_state):
            value = self.collector.snapshot()
        self.assertTrue(value["processes"]["items"][0]["identity_stable"])
        self.assertTrue(value["processes"]["unique_verified"])
        def changing_identity(base):
            item = original(base)
            changing_identity.count += 1
            item["starttime"] = changing_identity.count
            return item
        changing_identity.count = 0
        with patch.object(self.collector, "_identity", side_effect=changing_identity):
            value = self.collector.snapshot()
        self.assertFalse(value["processes"]["items"][0]["identity_stable"])
        self.assertFalse(value["processes"]["unique_verified"])

    def test_old_zombie_is_visible_but_does_not_invalidate_unique_live_vendor(self):
        base = self.add_process(21)
        (base / "stat").write_text("21 (temp_control) Z " + "0 " * 18 + "300 0", encoding="ascii")
        (base / "status").write_text("State:\tZ (zombie)\n", encoding="ascii")
        value = self.collector.snapshot()
        self.assertEqual(value["processes"]["count"], 2)
        self.assertTrue(value["processes"]["unique_verified"])
        self.assertTrue(value["processes"]["items"][1]["terminated"])

    def test_readiness_actual_journal_contract_is_retained(self):
        event = {"sequence": 8, "attempt": 2, "operation": "wait_ready", "event": "timeout", "result": "timeout",
                 "monotonic": 22.5, "elapsed": 20.0, "stable_seconds": 0.15, "stable_elapsed": 0.1, "remaining": 0,
                 "ready": False, "pid": 20, "starttime": 200, "exe": str(self.program), "alive": True,
                 "identity_ok": True, "sigcgt_raw": "0000000100000e20", "sigcgt_fields": ["0000000100000e20"],
                 "sigcgt": 0x100000e20, "sigcgt_hex": "0x100000e20", "required": 0xa00, "required_hex": "0xa00",
                 "handlers_ready": True, "require_guard": True, "guard_expected": str(self.guard),
                 "guard_paths": [str(self.guard)], "guard_ok": False, "single_process": True,
                 "status_bytes_read": 1024, "maps_bytes_read": 4096, "sigcgt_read_ok": True,
                 "maps_read_ok": True, "sigcgt_parse_ok": True,
                 "error": {"code": "readiness_timeout", "message": "就绪超时"},
                 "fields_truncated": [], "guard_paths_omitted": 0}
        journal = {"schema_version": 1, "first": event, "last": event, "failure": event,
                   "events": [event], "attempt_count": 2, "event_count": 8, "dropped_events": 0}
        self.write_json(self.folder / "readiness.json", journal)
        result = self.collector.snapshot()["runtime"]["directories"][0]["readiness"]
        self.assertEqual(result, journal)

    def test_native_platform_unavailable_is_explicit_and_does_not_import_fcntl(self):
        with patch.object(diagnostics.sys, "platform", "win32"), \
                patch.object(self.collector, "_paths", side_effect=AssertionError("native access on Windows")):
            value = self.collector.snapshot()
        self.assertFalse(value["available"])
        self.assertEqual(value["reason"], "linux_proc_unavailable")

    def test_policy_lock_busy_does_not_block_diagnostics_or_expose_outside_runtime(self):
        held = threading.Lock()
        held.acquire()
        self.policy.lock = held
        outside = self.root / "fan-private-outside"
        outside.mkdir()
        self.write_json(outside / "status.json", {"reason": "secret"})
        self.adapter.folder = outside
        try:
            started = time.monotonic()
            value = self.collector.snapshot()
        finally:
            held.release()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(value["available"])
        self.assertEqual(value["policy"]["reason"], "policy_lock_busy")
        self.assertNotIn("secret", json.dumps(value))

    def test_missing_policy_data_remains_null(self):
        self.policy.config = self.policy.channels = None
        value = self.collector.snapshot()["policy"]
        self.assertIsNone(value["config"])
        self.assertIsNone(value["channels"])
        self.assertIsNone(value["active"])

    def test_policy_evidence_keeps_only_canonical_two_temperature_fields(self):
        self.policy.config["channels"]["cpu"].update(quiet_below=40, normal_at=55, full_at=70,
                                                    unexpected_private_value="secret")
        value = self.collector.snapshot()["policy"]["config"]
        self.assertEqual(value["schema_version"], 2)
        cpu = value["channels"]["cpu"]
        self.assertEqual((cpu["low_temp"], cpu["high_temp"]), (55, 70))
        self.assertFalse({"quiet_below", "normal_at", "full_at", "unexpected_private_value"} & set(cpu))

    def test_capture_initial_state_changes_and_terminal_snapshot_are_retained(self):
        job = self.collector.start(duration_seconds=1, interval_ms=100)
        self.assertEqual(job["state"], "running")
        self.assertEqual(len(job["samples"]), 1)
        self.assertIn("preflight", job["samples"][0]["changes"])
        self.assertFalse(job["samples"][0]["changes"]["policy"]["busy"])
        self.policy.busy = True
        self.write_json(self.folder / "status.json", {"phase": "active"})
        result = self.wait_result(job)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["stop_reason"], "duration_elapsed")
        self.assertTrue(any(sample["changes"].get("policy", {}).get("busy") for sample in result["samples"]))
        self.assertTrue(result["final_snapshot"]["policy"]["busy"])
        self.assertLessEqual(len(result["samples"]), diagnostics.MAX_SAMPLES)
        self.assertFalse(self.collector._thread.is_alive())

    def test_strict_parameters_unknown_task_and_single_capture(self):
        for duration in (0, 61, True, 1.0, "45", None):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.collector.start(duration_seconds=duration)
        for interval in (99, 2001, False, 250.0, "250", None):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.collector.start(interval_ms=interval)
        with self.assertRaises(KeyError):
            self.collector.result("missing")
        job = self.collector.start(1, 100)
        with self.assertRaises(RuntimeError):
            self.collector.start(1, 100)
        result = self.wait_result(job)
        result["samples"].clear()
        self.assertTrue(self.collector.result(job["id"])["samples"])
        newer = self.collector.start(1, 100)
        with self.assertRaises(KeyError):
            self.collector.result(job["id"])
        self.assertNotEqual(newer["id"], job["id"])

    def test_sampling_exception_is_structured_and_does_not_leak_text(self):
        original = self.collector._sample
        def failing(started, previous):
            if previous is not None:
                raise RuntimeError("PRIVATE_TOKEN=secret")
            return original(started, previous)
        with patch.object(self.collector, "_sample", side_effect=failing):
            result = self.wait_result(self.collector.start(1, 100))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["errors"][0]["type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(result))
        self.assertIsNotNone(result["final_snapshot"])

    def test_initial_sampling_exception_returns_failed_job_without_thread(self):
        with patch.object(self.collector, "_sample", side_effect=RuntimeError("PRIVATE_TOKEN=secret")):
            result = self.collector.start(1, 100)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["errors"][0]["stage"], "capture_start")
        self.assertIsNone(self.collector._thread)
        self.assertNotIn("secret", json.dumps(result))

    def test_sample_cap_and_byte_limit_are_explicit(self):
        with patch.object(diagnostics, "MAX_SAMPLES", 2):
            result = self.wait_result(self.collector.start(1, 100))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stop_reason"], "sample_limit")
        self.assertEqual(len(result["samples"]), 2)
        original = self.collector._sample
        def changing_large_sample(started, previous):
            sample, current = original(started, previous)
            sample["changes"]["large_fixture"] = "x" * 2048
            return sample, current
        with patch.object(diagnostics, "MAX_SAMPLE_BYTES", 7000), \
                patch.object(self.collector, "_sample", side_effect=changing_large_sample):
            result = self.wait_result(self.collector.start(1, 100))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stop_reason"], "byte_limit")
        self.assertLessEqual(result["sample_bytes"], 7000)
        self.assertLessEqual(diagnostics.FanDiagnostics._encoded_size(result["final_snapshot"]),
                             diagnostics.MAX_SNAPSHOT_BYTES)

    def test_shutdown_wakes_capture_and_never_alters_control(self):
        job = self.collector.start(60, 2000)
        self.collector.stop()
        result = self.collector.result(job["id"])
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["stop_reason"], "service_stopped")
        self.assertFalse(self.collector._thread.is_alive())
        self.assertEqual(self.policy.config["channels"]["cpu"]["mode"], "manual")
        with self.assertRaises(RuntimeError):
            self.collector.start(1, 100)

    def test_large_changing_journal_obeys_capture_byte_budget(self):
        event = {"sequence": 0, "attempt": 1, "operation": "wait_ready", "result": "waiting",
                 "error": {"code": "fixture", "message": "x" * 700}}
        journal = {"schema_version": 1, "first": event, "last": dict(event), "failure": None,
                   "events": [dict(event) for _ in range(128)], "attempt_count": 1,
                   "event_count": 128, "dropped_events": 0}
        self.write_json(self.folder / "readiness.json", journal)
        baseline, _ = self.collector._sample(time.monotonic(), None)
        budget = self.collector._encoded_size(baseline) + 4000
        original = self.collector._sample
        def advancing_journal(started, previous):
            journal["last"]["sequence"] += 1
            journal["event_count"] += 1
            self.write_json(self.folder / "readiness.json", journal)
            return original(started, previous)
        with patch.object(diagnostics, "MAX_SAMPLE_BYTES", budget), \
                patch.object(self.collector, "_sample", side_effect=advancing_journal):
            result = self.wait_result(self.collector.start(1, 100))
        self.assertEqual(result["stop_reason"], "byte_limit")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["sample_bytes"], budget)
        self.assertLessEqual(self.collector._encoded_size(result), diagnostics.MAX_CAPTURE_BYTES)
        final = result["final_snapshot"]["runtime"]["directories"][0]["readiness"]
        self.assertEqual(len(final["events"]), 128)

    def test_extreme_snapshot_size_is_explicitly_trimmed_within_byte_limit(self):
        value = {"schema_version": 1, "version": "fixture", "captured_at": "now", "available": True,
                 "runtime": {"directories": [{"readiness": {"events": [{"padding": "x" * 20000}] * 128}}]}}
        fitted = self.collector._fit_snapshot(value)
        self.assertTrue(fitted["truncated"])
        self.assertIn("runtime.readiness.events", fitted["diagnostic_omissions"])
        self.assertLessEqual(self.collector._encoded_size(fitted), diagnostics.MAX_SNAPSHOT_BYTES)


if __name__ == "__main__":
    unittest.main()
