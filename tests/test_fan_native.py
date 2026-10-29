"""Synthetic fan takeover tests. No firmware, processes, devices or ioctls run."""
from contextlib import contextmanager
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fan_native", ROOT / "runtime/fan_native.py")
native = importlib.util.module_from_spec(spec)
sys.modules["fan_native"] = native
spec.loader.exec_module(native)
spec = importlib.util.spec_from_file_location("histor", ROOT / "runtime/histor.py")
histor_module = importlib.util.module_from_spec(spec)
sys.modules["histor"] = histor_module
spec.loader.exec_module(histor_module)
spec = importlib.util.spec_from_file_location("fan_watchdog", ROOT / "runtime/fan_watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class FakeBackend:
    def __init__(self):
        self.owner = {"pid": 10, "starttime": 100, "exe": "/fixture/python3"}
        self.vendor = {"pid": 20, "starttime": 200, "exe": "/histor/bin/temp_control"}
        self.guarded = {"pid": 30, "starttime": 300, "exe": "/histor/bin/temp_control"}
        self.live = {10: dict(self.owner), 20: dict(self.vendor)}
        self.policy, self.verified = "auto", True
        self.events, self.failures = [], set()

    def probe(self):
        self.verify_live()
        return {"guard_available": True}

    def verify_live(self):
        if not self.verified:
            raise ValueError("fixture driver identity changed")

    def owner_identity(self):
        return dict(self.owner)

    def alive(self, identity):
        return bool(identity and self.live.get(identity["pid"]) == identity)

    def capture(self):
        return {"identity": dict(self.vendor), "argv": ["temp_control", "--original-arg"],
                "env": {"DISK_AUTOSLEEP": "preserved", "PRIVATE_TOKEN": "fixture-secret"}, "cwd": "/histor/bin"}

    def mode(self):
        return self.policy

    def sync_mode(self, folder, expected=None, apply_manual=True):
        self.events.append(("sync_mode", expected, apply_manual))
        if expected is not None and self.policy != expected:
            raise RuntimeError("vendor_policy_changed")
        if "sync_mode" in self.failures:
            raise ValueError("fixture GET signal synchronization fails")
        return self.policy

    def write(self, targets):
        self.verify_live()
        self.events.append(("write", dict(targets)))
        if "write" in self.failures:
            raise ValueError("fixture ioctl failed")

    def stop_process(self, identity):
        self.events.append(("stop", identity["pid"]))
        if self.alive(identity):
            self.live.pop(identity["pid"])

    def spawn(self, original, lease=None, mask=None):
        self.events.append(("spawn", bool(lease), mask, dict(original)))
        if "spawn" in self.failures:
            raise RuntimeError("fixture preload load failed")
        identity = self.guarded if lease else self.vendor
        self.live[identity["pid"]] = dict(identity)
        return dict(identity)

    def assert_single(self, identity):
        vendors = [item for item in self.live.values() if item["exe"] == self.vendor["exe"]]
        if vendors != [identity]:
            raise RuntimeError("fixture competing vendor process")

    def assert_ready(self, identity, *, require_guard=False):
        self.assert_single(identity)
        if not self.alive(identity):
            raise RuntimeError("fixture daemon died")
        return identity

    def restore_daemon(self, original, guarded, lease=None):
        self.events.append(("restore_daemon", dict(original)))
        if guarded:
            self.live.pop(guarded["pid"], None)
        else:
            self.live.pop(self.guarded["pid"], None)  # Orphan from this fixture lease.
        self.live[original["identity"]["pid"]] = dict(original["identity"])
        return original["identity"]


class CachedVendorGradeBackend(FakeBackend):
    """Model vendor grade bookkeeping separately from requested ioctl output."""
    sync_mode = watchdog.SystemBackend.sync_mode

    def __init__(self, folder, initialize_at, failure=None):
        super().__init__()
        self.grade_path = folder / "fixture-vendor-fan"
        self.grade_path.write_bytes(struct.pack("<i", 2))
        self.initialize_at, self.failure = initialize_at, failure
        self.system_raw, self.synced = 26, False
        self.initialized = False
        self.clock = lambda: 0.0

    def write(self, targets):
        super().write(targets)
        if self.failure == "retry_write" and self.system_raw == 120:
            raise OSError("fixture safe-high retry failed")
        if "system" in targets:
            self.system_raw = targets["system"]

    def initialize_vendor(self):
        # The matching firmware starts with grade 2/raw 120. This is a fake
        # request recorder, never a hardware output or PWM readback.
        if self.initialized:
            return
        self.initialized = True
        self.grade_path.write_bytes(struct.pack("<i", 2))
        self.system_raw = 120
        self.events.append(("vendor_write", 120))

    def _write(self, targets, minimum):
        self.write(targets)

    def sample_auto(self):
        grade = struct.unpack("<i", self.grade_path.read_bytes())[0]
        # The fixture uses the verified vendor band at 45 C: grade 3/4 drops
        # to 2, while an existing grade 2 does not cause another output request.
        if grade > 2:
            self.grade_path.write_bytes(struct.pack("<i", 2))
            self.system_raw = 120
            self.events.append(("vendor_write", 120))
        else:
            self.events.append(("same_grade", grade))

    def restore_daemon(self, original, guarded, lease=None):
        if self.failure == "spawn":
            raise RuntimeError("fixture vendor spawn failed")
        identity = super().restore_daemon(original, guarded, lease=lease)
        if self.initialize_at == "before_get":
            self.initialize_vendor()
        return identity

    def _vendors(self):
        return [item for item in self.live.values() if item["exe"] == self.vendor["exe"]]

    def _owner_kind(self, identity, lease):
        return "vendor"

    def _histor(self, folder):
        backend = self

        class RPC:
            def _has_socket(self):
                return True

            def _config_mode(self):
                return histor_module.MODES[backend.policy]

            def _get_mode(self):
                if backend.failure in ("sync", "retry_write"):
                    raise ValueError("fixture policy synchronization failed")
                if backend.failure == "sync_policy_changed":
                    backend.policy, backend.system_raw = "medium", 191
                    backend.grade_path.write_bytes(struct.pack("<i", 3))
                    backend.events.append(("external_vendor_write", 191))
                    raise ValueError("fixture external policy changed during synchronization")
                if backend.initialize_at == "during_get":
                    backend.initialize_vendor()
                if backend.policy == "auto":
                    backend.sample_auto()
                return histor_module.MODES[backend.policy]

            def _rpc(self, command, code):
                if command != "set_fan_status" or code not in (2, 3, 4, 5):
                    raise AssertionError("fixture accepts only the fixed vendor SET")
                backend.events.append(("vendor_set", code))
                backend.policy = histor_module._CODES[code]
                if code == 5:
                    backend.sample_auto()
                else:
                    backend.grade_path.write_bytes(struct.pack("<i", code))
                    backend.system_raw = {2: 120, 3: 191, 4: 255}[code]
                    backend.events.append(("vendor_write", backend.system_raw))
                return "OK!"

        return RPC()

    def wait_ready(self, identity, **unused):
        super().assert_ready(identity)
        if self.initialize_at == "during_stability":
            self.initialize_vendor()
        self.synced = True
        return identity

    def assert_ready(self, identity, **options):
        if self.failure == "final_readiness" and self.synced:
            raise RuntimeError("fixture final readiness failed")
        return super().assert_ready(identity, **options)


class NativeFanTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="r1-fan-native-test-"))
        self.backend = FakeBackend()
        self.now = 100_000_000_000
        self.addCleanup(self.preserve)

    def preserve(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            shutil.move(str(self.folder), str(trash / self.folder.name))

    def make(self, channels):
        native.atomic_json(self.folder / "descriptor.json", {"version": 1, "owner": self.backend.owner,
                           "channels": sorted(channels), "folder": str(self.folder)})
        self.renew(channels)
        return watchdog.FanWatchdog(self.folder, self.backend, clock_ns=lambda: self.now)

    def renew(self, channels, targets=None, sequence=1):
        native.atomic_bytes(self.folder / "lease.bin", native.encode_lease(
            set(channels), targets or {name: 255 for name in channels}, sequence, self.now))

    def test_lease_exact_abi_raw_values_and_zero_bool_unknown_channels_rejected(self):
        value = native.encode_lease({"cpu", "system"}, {"cpu": 26, "system": 255}, 7, self.now)
        self.assertEqual(len(value), 32)
        self.assertEqual(struct.unpack("<8sQIIII", value), (native.MAGIC, self.now + 5_000_000_000, 3, 26, 255, 7))
        self.assertEqual(native.decode_lease(value, self.now, 3)["targets"], {"cpu": 26, "system": 255})
        for targets in ({"cpu": 0}, {"cpu": True}, {"cpu": 25}, {"cpu": 256}, {}, {"cpu": 200, "system": 200}):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                native.encode_lease({"cpu"}, targets, 1, self.now)
        for channels in (set(), {"other"}, ["cpu"]):
            with self.assertRaises(ValueError):
                native.valid_channels(channels)

    def test_lease_missing_corrupt_wrong_mask_and_future_are_rejected(self):
        value = native.encode_lease({"cpu"}, {"cpu": 200}, 1, self.now)
        for raw, tick, mask in ((None, self.now, 1), (value[:-1], self.now, 1),
                                (b"badmagic" + value[8:], self.now, 1), (value, self.now, 2),
                                (value, self.now + 5_000_000_000, 1), (value, self.now - 1, 1)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                native.decode_lease(raw, tick, mask)

    def test_cpu_only_restarts_once_preserves_original_environment_and_system_policy(self):
        owner = self.make({"cpu"})
        owner.begin()
        self.assertEqual(self.backend.policy, "auto")
        self.assertFalse(any(item[0] == "mode" for item in self.backend.events))
        spawns = [item for item in self.backend.events if item[0] == "spawn"]
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0][2], 1)
        self.assertEqual(spawns[0][3]["argv"], ["temp_control", "--original-arg"])
        self.assertEqual(spawns[0][3]["env"]["DISK_AUTOSLEEP"], "preserved")
        descriptor = self.folder / "descriptor.json"
        self.assertEqual(descriptor.stat().st_mode & 0o777, 0o600)
        self.assertIn("fixture-secret", descriptor.read_text())
        self.assertNotIn("fixture-secret", (self.folder / "status.json").read_text())

    def test_system_only_shim_mask_never_claims_cpu_or_persists_a_policy_change(self):
        owner = self.make({"system"})
        owner.begin()
        self.assertEqual(self.backend.policy, "auto")
        spawns = [item for item in self.backend.events if item[0] == "spawn"]
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0][2], 2)
        self.assertEqual([item[1] for item in self.backend.events if item[0] == "write"], [{"system": 255}])
        owner.restore("release_requested")
        self.assertEqual(self.backend.policy, "auto")
        self.assertEqual(self.backend.events[-1], ("sync_mode", "auto", True))

    def cached_vendor_recovery(self, initialize_at, failure=None, channels=None, mode="auto"):
        self.backend = CachedVendorGradeBackend(self.folder, initialize_at, failure)
        self.backend.policy = mode
        owner = self.make(channels or {"system"})
        owner.original, owner.guarded, owner.old_mode = self.backend.capture(), self.backend.guarded, mode
        owner.descriptor["daemon_changed"] = True
        self.backend.live.pop(self.backend.vendor["pid"])
        self.backend.live[self.backend.guarded["pid"]] = dict(self.backend.guarded)
        owner.save()
        return owner

    def test_auto_restore_keeps_vendor_initial_output_before_during_and_after_get(self):
        for initialize_at in ("before_get", "during_get", "during_stability"):
            with self.subTest(initialize_at=initialize_at):
                owner = self.cached_vendor_recovery(initialize_at)
                self.assertTrue(owner.restore("release_requested"))
                self.assertEqual(self.backend.events[0], ("write", {"system": 255}))
                self.assertIn(("vendor_write", 120), self.backend.events)
                self.assertIn(("same_grade", 2), self.backend.events)
                self.assertEqual(struct.unpack("<i", self.backend.grade_path.read_bytes())[0], 2)
                self.assertEqual(self.backend.system_raw, 120)
                self.assertEqual([event for event in self.backend.events if event[0] == "write"],
                                 [("write", {"system": 255})])
                status = native.read_json(self.folder / "status.json")
                self.assertEqual(status["phase"], "released")
                self.assertTrue(status["restore_ok"])

    def test_manual_factory_restore_updates_grade_before_later_auto_transition(self):
        for mode, grade, raw in (("medium", 3, 191), ("high", 4, 255)):
            with self.subTest(mode=mode):
                owner = self.cached_vendor_recovery("before_get", mode=mode)
                self.assertTrue(owner.restore("release_requested"))
                self.assertEqual(self.backend.system_raw, raw)
                self.assertEqual(struct.unpack("<i", self.backend.grade_path.read_bytes())[0], grade)
                self.assertIn(("vendor_set", grade), self.backend.events)
                # Match a later explicit vendor auto selection. If restoration
                # only wrote raw, stale grade 2 would suppress this correction.
                rpc = self.backend._histor(self.folder)
                self.assertEqual(rpc._get_mode(), grade)
                self.assertEqual(rpc._rpc("set_fan_status", 5), "OK!")
                self.assertEqual(rpc._get_mode(), 5)
                self.assertEqual(self.backend.system_raw, 120)
                self.assertEqual(struct.unpack("<i", self.backend.grade_path.read_bytes())[0], 2)

    def test_auto_restore_failure_reasserts_safe_high_once_and_keeps_descriptor(self):
        for failure, expected in (("spawn", "vendor_daemon_unconfirmed"),
                                  ("sync", "vendor_policy_unconfirmed"),
                                  ("final_readiness", "vendor_daemon_unconfirmed"),
                                  ("retry_write", "safe_high_unconfirmed")):
            with self.subTest(failure=failure):
                owner = self.cached_vendor_recovery("before_get", failure)
                self.assertFalse(owner.restore("lease_or_identity_failed"))
                writes = [event for event in self.backend.events if event[0] == "write"]
                self.assertEqual(writes, [("write", {"system": 255}), ("write", {"system": 255})])
                if failure != "retry_write":
                    self.assertEqual(self.backend.system_raw, 255)
                status = native.read_json(self.folder / "status.json")
                self.assertEqual(status["phase"], "recovery_failed")
                self.assertFalse(status["restore_ok"])
                self.assertIn(expected, status["recovery_errors"])
                self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def test_failed_sync_preserves_new_external_system_policy_but_keeps_cpu_safe(self):
        owner = self.cached_vendor_recovery("before_get", "sync_policy_changed", {"cpu", "system"})
        self.assertFalse(owner.restore("release_requested"))
        self.assertEqual([event for event in self.backend.events if event[0] == "write"],
                         [("write", {"cpu": 255, "system": 255}), ("write", {"cpu": 255})])
        self.assertEqual(self.backend.policy, "medium")
        self.assertEqual(self.backend.system_raw, 191)
        self.assertEqual(struct.unpack("<i", self.backend.grade_path.read_bytes())[0], 3)
        status = native.read_json(self.folder / "status.json")
        self.assertEqual(status["phase"], "recovery_failed")
        self.assertFalse(status["restore_ok"])
        self.assertIn("vendor_policy_unconfirmed", status["recovery_errors"])
        self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def test_both_channels_reassert_same_target_bounded_without_restarting(self):
        owner = self.make({"cpu", "system"})
        owner.begin()
        self.renew({"cpu", "system"}, {"cpu": 150, "system": 210}, 2)
        owner.tick()
        writes = len([item for item in self.backend.events if item[0] == "write"])
        self.renew({"cpu", "system"}, {"cpu": 150, "system": 210}, 3)
        owner.tick()
        self.assertEqual(len([item for item in self.backend.events if item[0] == "write"]), writes)
        self.now += 800_000_000
        self.renew({"cpu", "system"}, {"cpu": 150, "system": 210}, 4)
        owner.tick()
        self.assertEqual(len([item for item in self.backend.events if item[0] == "write"]), writes + 1)
        self.assertEqual(len([item for item in self.backend.events if item[0] == "spawn"]), 1)
        self.assertEqual(native.read_json(self.folder / "status.json")["applied_sequence"], 4)

    def test_ten_percent_lease_reaches_watchdog_then_expiry_recovers_full(self):
        owner = self.make({"cpu", "system"})
        owner.begin()
        self.backend.events.clear()
        self.renew({"cpu", "system"}, {"cpu": 26, "system": 26}, 2)
        owner.tick()
        self.assertEqual(self.backend.events, [("write", {"cpu": 26, "system": 26})])
        self.assertEqual(native.read_json(self.folder / "status.json")["applied_sequence"], 2)
        self.now += 5_000_000_001
        with self.assertRaises(ValueError):
            owner.tick()
        self.backend.events.clear()
        self.assertTrue(owner.restore("lease_or_identity_failed"))
        self.assertEqual(self.backend.events[0], ("write", {"cpu": 255, "system": 255}))

    def test_expired_lease_recovers_high_before_daemon_and_policy(self):
        owner = self.make({"cpu", "system"})
        owner.begin()
        self.now += 5_000_000_001
        with self.assertRaises(ValueError):
            owner.tick()
        self.backend.events.clear()
        self.assertTrue(owner.restore("lease_or_identity_failed"))
        self.assertEqual([event[0] for event in self.backend.events], ["write", "restore_daemon", "sync_mode"])
        self.assertEqual(self.backend.events[0][1], {"cpu": 255, "system": 255})
        self.assertEqual(native.read_json(self.folder / "descriptor.json"), {"version": 1, "recovered": True})

    def test_owner_pid_reuse_and_release_request_trigger_recovery(self):
        owner = self.make({"system"})
        owner.begin()
        self.backend.live[10]["starttime"] += 1
        with self.assertRaisesRegex(RuntimeError, "owner_stopped"):
            owner.tick()
        self.backend.live[10] = dict(self.backend.owner)
        native.atomic_json(self.folder / "release.json", {"release": True})
        with self.assertRaisesRegex(RuntimeError, "release_requested"):
            owner.tick()

    def test_external_vendor_policy_wins_without_fighting_or_restoring_old_policy(self):
        owner = self.make({"cpu", "system"})
        owner.begin()
        self.backend.policy = "low"
        self.now += 800_000_000  # Competition wins even when reassertion is due.
        count = len([item for item in self.backend.events if item[0] == "write"])
        with self.assertRaisesRegex(RuntimeError, "vendor_policy_changed"):
            owner.tick()
        self.assertEqual(len([item for item in self.backend.events if item[0] == "write"]), count)
        self.backend.events.clear()
        owner.restore("vendor_policy_changed")
        self.assertEqual(self.backend.policy, "low")
        self.assertEqual(self.backend.events[0], ("write", {"cpu": 255}))
        self.assertFalse(any(event[0] == "mode" for event in self.backend.events))
        self.assertEqual(native.read_json(self.folder / "status.json")["release_cause"], "vendor_policy_changed")

    def test_sync_failure_still_has_original_executor_and_policy_recovery_record(self):
        owner = self.make({"system"})
        self.backend.failures.add("sync_mode")
        with self.assertRaises(ValueError):
            owner.begin()
        self.assertTrue(native.read_json(self.folder / "descriptor.json")["daemon_changed"])
        self.assertEqual(native.read_json(self.folder / "descriptor.json")["old_mode"], "auto")
        self.backend.failures.clear()
        self.assertTrue(owner.restore("lease_or_identity_failed"))
        self.assertEqual(self.backend.policy, "auto")

    def test_preload_spawn_failure_restores_original_executor(self):
        owner = self.make({"cpu"})
        self.backend.failures.add("spawn")
        with self.assertRaises(RuntimeError):
            owner.begin()
        self.assertFalse(self.backend.alive(self.backend.vendor))
        self.assertTrue(owner.restore("lease_or_identity_failed"))
        self.assertTrue(self.backend.alive(self.backend.vendor))

    def test_identity_change_prevents_custom_write_but_still_restores_daemon(self):
        owner = self.make({"cpu"})
        owner.begin()
        self.backend.verified = False
        with self.assertRaises(ValueError):
            owner.tick()
        self.assertFalse(owner.restore("lease_or_identity_failed"))
        self.assertTrue(self.backend.alive(self.backend.vendor))
        self.assertFalse(native.read_json(self.folder / "status.json")["restore_ok"])

    def test_unknown_original_system_mode_refuses_before_any_mutation(self):
        owner = self.make({"system"})
        self.backend.policy = None
        with self.assertRaises(ValueError):
            owner.begin()
        self.assertEqual(self.backend.events, [])

    def test_default_adapter_capability_is_passive_and_does_not_expose_private_state(self):
        adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram", backend=self.backend)
        with patch("subprocess.Popen", side_effect=AssertionError("不能启动程序")), \
                patch("os.kill", side_effect=AssertionError("不能停止程序")), \
                patch("fcntl.ioctl", side_effect=AssertionError("不能访问硬件")):
            self.assertFalse(adapter.capabilities()["available"])
            self.assertEqual(adapter.snapshot()["phase"], "idle")
            adapter.release()
        self.assertEqual(self.backend.events, [])
        self.assertNotIn("fixture-secret", json.dumps(adapter.capabilities()))

    def test_failed_probe_hides_capability_even_when_writes_enabled(self):
        self.backend.verified = False
        adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram",
                                          backend=self.backend, allow_writes=True)
        self.assertFalse(adapter.capabilities()["available"])
        with self.assertRaises(ValueError):
            adapter.start({"cpu"})
        self.assertEqual(self.backend.events, [])

    def test_capability_reasons_allow_only_static_preflight_messages(self):
        adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram",
                                          backend=self.backend, allow_writes=True)
        for message in native.PREFLIGHT_REASONS:
            with self.subTest(message=message), patch.object(self.backend, "probe", side_effect=ValueError(message)):
                state = adapter.capabilities()
                self.assertFalse(state["available"])
                self.assertEqual(state["reason"], message)
        private = "/private/config-path token=fixture-secret environment=private"
        for error in (ValueError(private), RuntimeError(private), OSError(private),
                      FileNotFoundError(2, private, "/private/secret")):
            with self.subTest(error=type(error).__name__), patch.object(self.backend, "probe", side_effect=error):
                state = adapter.capabilities()
                self.assertFalse(state["available"])
                self.assertNotIn("/private", json.dumps(state))
                self.assertNotIn("fixture-secret", json.dumps(state))
                self.assertIn("身份尚未验证", state["reason"])

    def test_proc_identity_parses_parentheses_and_rejects_zombie_or_reused_pid(self):
        proc = self.folder / "proc/123"
        proc.mkdir(parents=True)
        (proc / "exe").symlink_to("/fixture/python")
        (proc / "stat").write_text("123 (odd) name) S " + " ".join(["0"] * 18 + ["777"] + ["0"] * 4))
        self.assertEqual(native.process_identity(123, proc.parent), {"pid": 123, "starttime": 777, "exe": "/fixture/python"})
        (proc / "stat").write_text("123 (dead) Z " + " ".join(["0"] * 18 + ["777"] + ["0"] * 4))
        self.assertIsNone(native.process_identity(123, proc.parent))

    def test_state_reader_rejects_oversize_symlink_and_nonregular_without_blocking(self):
        path = self.folder / "state"
        path.write_bytes(b"x" * 33)
        self.assertIsNone(native.read_bytes(path, 32))
        (self.folder / "link").symlink_to(path)
        self.assertIsNone(native.read_bytes(self.folder / "link", 64))
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.folder / "fifo")
            self.assertIsNone(native.read_bytes(self.folder / "fifo", 64))

    def fake_child_factory(self, command, **options):
        folder = Path(command[command.index("--descriptor") + 1]).parent
        test = self

        class Child:
            def __init__(child):
                child.owner = watchdog.FanWatchdog(folder, test.backend, clock_ns=lambda: test.now)
                child.started = child.done = False

            def poll(child):
                if child.done:
                    return 0
                try:
                    if not child.started:
                        child.owner.begin()
                        child.started = True
                    elif native.read_json(folder / "release.json") is not None:
                        child.owner.restore("release_requested")
                        child.done = True
                        return 0
                    else:
                        child.owner.tick()
                except (ValueError, RuntimeError):
                    child.owner.restore("lease_or_identity_failed")
                    child.done = True
                    return 1
                return None

            def wait(child, timeout):
                return child.poll()

        return Child()

    def fake_sleep(self, seconds):
        self.now += int(seconds * 1_000_000_000)

    def adapter(self):
        return native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram", backend=self.backend,
            allow_writes=True, popen=self.fake_child_factory, clock=lambda: self.now / 1_000_000_000, sleep=self.fake_sleep)

    def test_adapter_start_write_sequence_ack_and_release_are_fake_only_and_idempotent(self):
        adapter = self.adapter()
        adapter.start({"cpu", "system"})
        self.assertEqual(adapter.snapshot()["owner"], "custom")
        adapter.write({"cpu": 190, "system": 220})
        self.assertEqual(adapter.snapshot()["targets"], {"cpu": 190, "system": 220})
        before = (adapter.folder / "lease.bin").read_bytes()
        with self.assertRaises(ValueError):
            adapter.write({"cpu": 190})
        self.assertEqual((adapter.folder / "lease.bin").read_bytes(), before)
        adapter.release()
        events = list(self.backend.events)
        adapter.release()
        self.assertEqual(self.backend.events, events)
        self.assertEqual(adapter.snapshot()["active_channels"], [])
        self.assertEqual(adapter.channels, set())

    def test_adapter_driver_change_releases_instead_of_renewing_an_unsafe_lease(self):
        adapter = self.adapter()
        adapter.start({"cpu"})
        self.backend.verified = False
        with self.assertRaises(RuntimeError):
            adapter.write({"cpu": 200})
        self.assertEqual(adapter.channels, set())
        self.assertTrue(self.backend.alive(self.backend.vendor))

    def test_independent_systemd_handle_tracks_watchdog_not_launcher_or_control_owner(self):
        identity = {"pid": 99, "starttime": 990, "exe": "/fixture/python3"}
        self.backend.live[99] = identity
        native.atomic_json(self.folder / "status.json", {"watchdog": identity, "phase": "active"})
        launcher = type("Launcher", (), {"poll": lambda _: 0})()
        handle = native.SystemdSupervisor(launcher, self.folder, self.backend)
        self.assertIsNone(handle.poll())
        self.backend.live.pop(10)  # Killing the control owner does not kill WDT.
        self.assertIsNone(handle.poll())
        self.backend.live[99] = {**identity, "starttime": 991}  # PID reuse is exit.
        self.assertEqual(handle.poll(), 0)

    def test_production_launch_uses_independent_transient_unit_and_no_restart_job(self):
        backend = watchdog.SystemBackend(root="/")
        calls = []
        launcher = type("Launcher", (), {"poll": lambda _: None})()

        def fake_popen(argv, **options):
            calls.append((argv, options))
            return launcher

        backend.launch_watchdog(["/usr/bin/python3", "-B", "/run/r1-toolbox/fan_watchdog.py"],
                                "/run/r1-toolbox/fan-abc123", fake_popen)
        argv = calls[0][0]
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn("--collect", argv)
        self.assertIn("--property=KillMode=process", argv)
        self.assertIn("--property=Restart=no", argv)
        self.assertNotIn("enable", argv)
        with self.assertRaises(ValueError):
            backend.launch_watchdog(["python3"], "/tmp/persistent-descriptor", fake_popen)

    def test_backend_integer_ioctl_abi_uses_only_fake_opened_file_and_mocked_ioctl(self):
        device = self.folder / "dev/hikio"
        device.parent.mkdir()
        device.write_bytes(b"fixture-only")
        info = device.stat()
        backend = watchdog.SystemBackend(root=self.folder)
        backend._device = (info.st_dev, info.st_ino, info.st_rdev)
        with patch.object(backend, "verify_live"), patch.object(watchdog.stat, "S_ISCHR", return_value=True), \
                patch.object(watchdog.fcntl, "ioctl", return_value=0) as ioctl:
            backend.write({"cpu": 26, "system": 240})
        self.assertEqual([(call.args[1], call.args[2]) for call in ioctl.call_args_list], [(0x5798, 26), (0x5799, 240)])
        with patch.object(backend, "verify_live"), patch.object(watchdog.stat, "S_ISCHR", return_value=True), \
                patch.object(watchdog.fcntl, "ioctl", return_value=-1):
            with self.assertRaises(ValueError):
                backend.write({"cpu": 180})

    def test_backend_rejects_raw_25_zero_and_256_before_any_ioctl(self):
        device = self.folder / "dev/hikio"
        device.parent.mkdir()
        device.write_bytes(b"fixture-only")
        info = device.stat()
        backend = watchdog.SystemBackend(root=self.folder)
        backend._device = (info.st_dev, info.st_ino, info.st_rdev)
        with patch.object(backend, "verify_live"), patch.object(watchdog.stat, "S_ISCHR", return_value=True), \
                patch.object(watchdog.fcntl, "ioctl", return_value=0) as ioctl:
            for value in (0, 25, 256, True):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    backend.write({"cpu": value})
            ioctl.assert_not_called()

    def test_backend_immutable_firmware_and_exact_loaded_note_gate(self):
        program = self.folder / "histor/bin/temp_control"
        program.parent.mkdir(parents=True)
        program.write_bytes(b"fake-firmware-not-executable")
        note = self.folder / "sys/module/hikio/notes/.note.gnu.build-id"
        note.parent.mkdir(parents=True)
        note.write_bytes(native.DRIVER_NOTE)
        device = self.folder / "dev/hikio"
        device.parent.mkdir()
        device.write_bytes(b"fake-char-identity")
        digest = hashlib.sha256(program.read_bytes()).hexdigest()
        backend = watchdog.SystemBackend(root=self.folder)
        with patch.object(watchdog, "FIRMWARE_SHA256", digest), \
                patch.object(watchdog.stat, "S_ISCHR", return_value=True):
            backend.verify_live()
            note.write_bytes(native.DRIVER_NOTE[:-1] + b"\0")
            with self.assertRaises(ValueError):
                backend.verify_live()
            note.write_bytes(native.DRIVER_NOTE)
            program.write_bytes(b"replaced-firmware-not-executable")
            with self.assertRaises(ValueError):
                backend.verify_live()

    def test_vendor_mode_accepts_only_trailing_nuls_on_known_bounded_config(self):
        config = self.folder / "tmp/nand_config/config.txt"
        config.parent.mkdir(parents=True)
        backend = watchdog.SystemBackend(root=self.folder)
        for code, expected in ((2, "low"), (3, "medium"), (4, "high"), (5, "auto")):
            for suffix in (b"", b"\0", b"\0\0\0"):
                with self.subTest(mode=code, nuls=len(suffix)):
                    raw = json.dumps({"fan": {"fan_mode": code}}).encode() + suffix
                    config.write_bytes(raw)
                    self.assertEqual(backend.mode(), expected)
                    self.assertEqual(config.read_bytes(), raw)
                    if suffix:
                        self.assertIsNone(native.read_json(config))
        for raw in (b"", b"\0", b"not-json\0", b'{"fan":\0{"fan_mode":5}}\0',
                    b'{"fan":{"fan_mode":5}}\0 {}', b'{"fan":{"fan_mode":5}}\0\n',
                    b"[]\0", b"{}\0", b'{"fan":5}\0', b'{"fan":{"fan_mode":true}}\0',
                    b'{"fan":{"fan_mode":"5"}}\0', b'{"fan":{"fan_mode":5.0}}\0',
                    b'{"fan":{"fan_mode":null}}\0', b'{"fan":{"fan_mode":1}}\0',
                    b'{"fan":{"fan_mode":6}}\0'):
            with self.subTest(raw=raw):
                config.write_bytes(raw)
                self.assertIsNone(backend.mode())
        config.write_bytes(b'{"fan":{"fan_mode":5}}' + b"\0" * (1024 * 1024))
        self.assertIsNone(backend.mode())
        config.rename(self.folder / "oversize-vendor-config")
        target = self.folder / "valid-vendor-config"
        target.write_bytes(b'{"fan":{"fan_mode":5}}\0')
        config.symlink_to(target)
        self.assertIsNone(backend.mode())

    def test_single_nul_vendor_config_passes_probe_with_real_socket_and_safe_diagnostics(self):
        config = self.folder / "tmp/nand_config/config.txt"
        config.parent.mkdir(parents=True)
        original = b'{"fan":{"fan_mode":5}}\0'
        config.write_bytes(original)
        endpoint = self.folder / "var/tmp/emhistor"
        endpoint.parent.mkdir(parents=True)
        guard = self.folder / "fan_guard.so"
        elf = bytearray(64)
        elf[:6], elf[16:20] = b"\x7fELF\x02\x01", b"\x03\x00\x3e\x00"
        guard.write_bytes(elf)
        guard.chmod(0o644)
        backend = watchdog.SystemBackend(root=self.folder, guard_path=guard)
        adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram",
                                          backend=backend, allow_writes=True)
        with patch.object(watchdog.sys, "platform", "linux"), patch.object(watchdog.os, "geteuid", return_value=0), \
                patch.object(watchdog.shutil, "which", return_value="/fixture/systemd-run"), \
                patch.object(backend, "verify_live"), patch.object(backend, "_vendors", return_value=[self.backend.vendor]), \
                patch("subprocess.Popen", side_effect=AssertionError("预检不能启动程序")), \
                patch("fcntl.ioctl", side_effect=AssertionError("预检不能写硬件")):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(endpoint))
                self.assertTrue(adapter.capabilities()["available"])
                self.assertEqual(config.read_bytes(), original)
                config.write_bytes(b'{"fan":{"fan_mode":"5"}}\0')
                self.assertEqual(adapter.capabilities()["reason"], "原厂系统风扇策略配置无效或不可读")
                config.write_bytes(original)
            endpoint.rename(self.folder / "closed-socket-fixture")
            self.assertEqual(adapter.capabilities()["reason"], "原厂系统风扇同步接口不可用")
            endpoint.write_bytes(b"ordinary file, not a socket")
            self.assertEqual(adapter.capabilities()["reason"], "原厂系统风扇同步接口不可用")
        self.assertEqual(config.read_bytes(), original)

    def test_recovery_and_status_json_remain_strict_about_trailing_nuls(self):
        for name in ("descriptor.json", "status.json", "release.json"):
            path = self.folder / name
            path.write_bytes(b'{"version":1}\0')
            self.assertIsNone(native.read_json(path))

    @contextmanager
    def real_proc_backend(self):
        program = self.folder / "histor/bin/temp_control"
        program.parent.mkdir(parents=True)
        program.write_bytes(b"fixture program, never executed")
        note = self.folder / "sys/module/hikio/notes/.note.gnu.build-id"
        note.parent.mkdir(parents=True)
        note.write_bytes(native.DRIVER_NOTE)
        device = self.folder / "dev/hikio"
        device.parent.mkdir()
        device.write_bytes(b"fixture character device substitute")
        config = self.folder / "tmp/nand_config/config.txt"
        config.parent.mkdir(parents=True)
        config.write_bytes(b'{"fan":{"fan_mode":5}}\0')
        guard = self.folder / "fan_guard.so"
        elf = bytearray(64)
        elf[:6], elf[16:20] = b"\x7fELF\x02\x01", b"\x03\x00\x3e\x00"
        guard.write_bytes(elf)
        guard.chmod(0o644)
        (self.folder / "proc").mkdir()
        endpoint = self.folder / "var/tmp/emhistor"
        endpoint.parent.mkdir(parents=True)
        backend = watchdog.SystemBackend(root=self.folder, guard_path=guard)
        with patch.object(watchdog.sys, "platform", "linux"), patch.object(watchdog.os, "geteuid", return_value=0), \
                patch.object(watchdog.signal, "SIGUSR1", 10), patch.object(watchdog.signal, "SIGUSR2", 12), \
                patch.object(watchdog.shutil, "which", return_value="/fixture/systemd-run"), \
                patch.object(watchdog, "FIRMWARE_SHA256", hashlib.sha256(program.read_bytes()).hexdigest()), \
                patch.object(watchdog.stat, "S_ISCHR", return_value=True), \
                patch("subprocess.Popen", side_effect=AssertionError("不能执行假固件")), \
                patch("os.kill", side_effect=AssertionError("不能发送信号")), \
                patch("fcntl.ioctl", side_effect=AssertionError("不能访问硬件")), \
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            yield backend

    def write_proc_identity(self, backend, pid, starttime, state="S", exe=None):
        path = backend.proc / str(pid)
        path.mkdir(exist_ok=True)
        (path / "stat").write_text(f"{pid} (temp_control) {state} "
                                   + " ".join(["0"] * 18 + [str(starttime)] + ["0"] * 4))
        if not (path / "exe").is_symlink():
            (path / "exe").symlink_to(exe or backend.program)
        (path / "status").write_text("Name:\ttemp_control\nSigCgt:\t0000000000000a00\n")
        return {"pid": pid, "starttime": starttime, "exe": str(exe or backend.program)}

    def test_real_proc_vendor_to_guarded_replacement_keeps_repeated_capabilities_available(self):
        with self.real_proc_backend() as backend:
            adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram",
                                              backend=backend, allow_writes=True)
            original = self.write_proc_identity(backend, 20, 200)
            self.assertEqual(backend._vendors(), [original])
            self.assertTrue(adapter.capabilities()["available"])
            # Replaced parent remains a zombie; the sole live replacement is
            # our exact guarded daemon. No _vendors/probe/identity mocks here.
            self.write_proc_identity(backend, 20, 200, state="Z")
            guarded = self.write_proc_identity(backend, 30, 300)
            lease = self.folder / "ram/lease.bin"
            (backend.proc / "30/maps").write_text(f"0000-1000 r-xp 0000 00:01 123 {backend.guard_path.resolve()}\n")
            (backend.proc / "30/environ").write_bytes(b"R1_FAN_LEASE=" + os.fsencode(lease) + b"\0")
            self.assertEqual(backend._owner_kind(guarded, lease), "ours")
            adapter.channels = {"cpu"}
            for _ in range(3):
                self.assertEqual(backend._vendors(), [guarded])
                state = adapter.capabilities()
                self.assertTrue(state["available"], state)
                self.assertEqual(state["owner"], "custom")

    def test_real_proc_cardinality_unknown_exe_zombies_and_nested_threads_keep_identity_gate(self):
        with self.real_proc_backend() as backend:
            adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram",
                                              backend=backend, allow_writes=True)
            unique_reason = "必须存在唯一身份已验证的原厂温控进程"
            self.assertEqual(adapter.capabilities()["reason"], unique_reason)
            self.write_proc_identity(backend, 99, 990, state="Z")
            self.assertEqual(backend._vendors(), [])
            self.assertEqual(adapter.capabilities()["reason"], unique_reason)
            original = self.write_proc_identity(backend, 20, 200)
            task = backend.proc / "20/task/21"
            task.mkdir(parents=True)
            (task / "stat").write_text("21 (temp_control) S " + " ".join(["0"] * 18 + ["210"] + ["0"] * 4))
            (task / "exe").symlink_to(backend.program)
            self.assertEqual(backend._vendors(), [original])
            self.assertTrue(adapter.capabilities()["available"])
            second = self.write_proc_identity(backend, 30, 300)
            self.assertEqual({row["pid"] for row in backend._vendors()}, {20, 30})
            self.assertEqual(adapter.capabilities()["reason"], unique_reason)
            self.write_proc_identity(backend, 30, 300, state="Z")
            self.assertTrue(adapter.capabilities()["available"])
            (backend.proc / "30/exe").rename(self.folder / "retired-exe-symlink")
            self.write_proc_identity(backend, second["pid"], 301, exe=self.folder / "unknown/temp_control")
            self.assertIn("来源或版本无法确认", adapter.capabilities()["reason"])

    def test_backend_sync_verifies_manual_ioctl_then_reasserts_same_vendor_grade(self):
        backend = watchdog.SystemBackend(root=self.folder)
        calls = []
        rpc = type("RPC", (), {"_has_socket": lambda _: True, "_config_mode": lambda _: 2,
                                "_get_mode": lambda _: calls.append(("get",)) or 2,
                                "_rpc": lambda _, command, value: calls.append((command, value)) or "OK!"})()
        with patch.object(backend, "verify_live"), patch.object(backend, "_histor", return_value=rpc), \
                patch.object(backend, "mode", return_value="low"), patch.object(backend, "_write") as write, \
                patch.object(backend, "_vendors", return_value=[self.backend.vendor]), \
                patch.object(backend, "_owner_kind", return_value="vendor"), \
                patch.object(backend, "assert_ready"), patch.object(backend, "wait_ready"):
            backend.sync_mode(self.folder, expected="low")
            write.assert_called_once_with({"system": 120}, minimum=120)
        self.assertEqual(calls, [("get",), ("set_fan_status", 2), ("get",)])

    def test_manual_restore_ioctl_failure_never_uses_vendor_ack_as_write_proof(self):
        owner = self.cached_vendor_recovery("before_get", mode="high")
        with patch.object(self.backend, "_write", side_effect=OSError("fixture manual ioctl failed")):
            self.assertFalse(owner.restore("release_requested"))
        self.assertFalse(any(event[0] == "vendor_set" for event in self.backend.events))
        self.assertEqual(self.backend.system_raw, 255)
        status = native.read_json(self.folder / "status.json")
        self.assertFalse(status["restore_ok"])
        self.assertIn("vendor_policy_unconfirmed", status["recovery_errors"])
        self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def test_manual_restore_ack_and_readback_failures_remain_unconfirmed(self):
        for failure in ("ack", "readback"):
            with self.subTest(failure=failure):
                owner = self.cached_vendor_recovery("before_get", mode="medium")
                rpc = self.backend._histor(self.folder)
                if failure == "ack":
                    failing = patch.object(rpc, "_rpc", return_value="OK")
                else:
                    failing = patch.object(rpc, "_get_mode", side_effect=[3, 4])
                with patch.object(self.backend, "_histor", return_value=rpc), failing:
                    self.assertFalse(owner.restore("release_requested"))
                self.assertEqual(self.backend.system_raw, 255)
                status = native.read_json(self.folder / "status.json")
                self.assertFalse(status["restore_ok"])
                self.assertIn("vendor_policy_unconfirmed", status["recovery_errors"])
                self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def test_manual_sync_rechecks_driver_and_external_mode_before_vendor_set(self):
        for change in ("driver", "mode"):
            with self.subTest(change=change):
                owner = self.cached_vendor_recovery("before_get", mode="high")
                write = self.backend._write

                def change_after_write(targets, minimum):
                    write(targets, minimum)
                    if change == "driver":
                        self.backend.verified = False
                    else:
                        self.backend.policy, self.backend.system_raw = "medium", 191
                        self.backend.grade_path.write_bytes(struct.pack("<i", 3))

                with patch.object(self.backend, "_write", side_effect=change_after_write):
                    self.assertFalse(owner.restore("release_requested"))
                self.assertFalse(any(event[0] == "vendor_set" for event in self.backend.events))
                if change == "mode":
                    self.assertEqual(self.backend.policy, "medium")
                    self.assertEqual(self.backend.system_raw, 191)
                    self.assertEqual([event for event in self.backend.events if event[0] == "write"],
                                     [("write", {"system": 255}), ("write", {"system": 255})])
                status = native.read_json(self.folder / "status.json")
                self.assertFalse(status["restore_ok"])
                self.assertIn("vendor_policy_unconfirmed", status["recovery_errors"])
                self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def fake_startup_clock(self, backend, on_sleep=lambda seconds: None):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            on_sleep(now[0])

        backend.clock, backend.sleep = lambda: now[0], sleep
        return now

    def test_spawn_waits_for_both_handlers_and_exact_guard_then_stability(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            original = {"argv": [str(backend.program)], "env": {"DISK_AUTOSLEEP": "preserved"},
                        "cwd": str(backend.program.parent)}
            for guarded in (False, True):
                with self.subTest(guarded=guarded):
                    (base / "status").write_text("SigCgt:\t0000000000000000\n")
                    (base / "maps").write_bytes(b"")

                    def advance(seconds):
                        if seconds >= 0.10:
                            (base / "status").write_text("SigCgt:\t0000000000000200\n")
                        if seconds >= 0.25:
                            (base / "status").write_text("SigCgt:\t0000000000000a00\n")
                        if seconds >= 0.35:
                            (base / "maps").write_text(f"0000-1000 r-xp 0000 00:01 123 {backend.guard_path.resolve()}\n")

                    now = self.fake_startup_clock(backend, advance)
                    child = type("Child", (), {"pid": 30, "poll": lambda _: None})()
                    with patch("subprocess.Popen", return_value=child) as popen:
                        result = backend.spawn(original, lease=self.folder / "lease.bin" if guarded else None,
                                               mask=1 if guarded else None)
                    self.assertEqual(result, identity)
                    self.assertGreaterEqual(now[0], (0.35 if guarded else 0.25) + 0.15 - 1e-9)
                    self.assertLess(now[0], 1)
                    self.assertEqual(popen.call_args.kwargs["env"]["DISK_AUTOSLEEP"], "preserved")

    def test_spawn_unready_timeout_exit_and_pid_reuse_never_cleanup_another_identity(self):
        with self.real_proc_backend() as backend:
            base = backend.proc / "30"
            original = {"argv": [str(backend.program)], "env": {}, "cwd": str(backend.program.parent)}
            for failure in ("timeout", "exit", "pid_reuse"):
                with self.subTest(failure=failure):
                    identity = self.write_proc_identity(backend, 30, 300)
                    (base / "status").write_text("SigCgt:\t0000000000000000\n")

                    def advance(seconds):
                        if seconds >= 0.1 and failure != "timeout":
                            self.write_proc_identity(backend, 30, 301 if failure == "pid_reuse" else 300,
                                                     state="Z" if failure == "exit" else "S")

                    now = self.fake_startup_clock(backend, advance)
                    child = type("Child", (), {"pid": 30, "poll": lambda _: None})()
                    with patch("subprocess.Popen", return_value=child), patch.object(backend, "stop_process") as stop:
                        with self.assertRaisesRegex(RuntimeError, "信号处理器就绪未确认"):
                            backend.spawn(original)
                    self.assertLessEqual(now[0], 20)
                    if failure == "timeout":
                        stop.assert_called_once_with(identity)
                    else:
                        stop.assert_not_called()

    def test_readiness_journal_keeps_full_flicker_timeout_after_successful_recovery(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            maps = f"0000-1000 r-xp 0000 00:01 123 {backend.guard_path.resolve()}\n".encode()
            (base / "maps").write_bytes(maps)
            path = self.folder / "readiness.json"
            backend.set_readiness_path(path)

            def advance(seconds):
                if seconds >= 0.025:
                    (base / "status").write_text("SigCgt:\t0\n")

            now = self.fake_startup_clock(backend, advance)
            with self.assertRaisesRegex(RuntimeError, "信号处理器就绪未确认"):
                backend.wait_ready(identity, require_guard=True, deadline=0.2)
            value = native.read_json(path)
            self.assertEqual(value["schema_version"], 1)
            self.assertEqual(value["first"]["event"], "start")
            failed = value["failure"]
            self.assertEqual(failed["event"], "timeout")
            self.assertEqual(failed["remaining"], 0)
            self.assertEqual(failed["pid"], 30)
            self.assertEqual(failed["starttime"], 300)
            self.assertEqual(failed["exe"], str(backend.program))
            self.assertTrue(failed["alive"])
            self.assertTrue(failed["identity_ok"])
            self.assertTrue(failed["single_process"])
            self.assertEqual(failed["sigcgt_raw"], "0")
            self.assertEqual(failed["sigcgt"], 0)
            self.assertEqual(failed["required"], 0xa00)
            self.assertFalse(failed["handlers_ready"])
            self.assertTrue(failed["require_guard"])
            self.assertTrue(failed["guard_ok"])
            self.assertEqual(failed["guard_expected"], str(backend.guard_path.resolve()))
            self.assertEqual(failed["guard_paths"], [str(backend.guard_path.resolve())])
            self.assertEqual(failed["maps_bytes_read"], len(maps))
            self.assertTrue(failed["maps_read_ok"])
            self.assertEqual([item["ready"] for item in value["events"] if item["event"] == "state"], [True, False])
            (base / "status").write_text("SigCgt:\ta00\n")
            backend.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
            self.assertEqual(backend.wait_ready(identity, deadline=now[0] + 0.5), identity)
            recovered = native.read_json(path)
            self.assertEqual(recovered["failure"], failed)
            self.assertEqual(recovered["first"], value["first"])
            self.assertEqual(recovered["last"]["event"], "success")
            self.assertEqual(recovered["attempt_count"], 2)
            self.assertGreater(recovered["last"]["attempt"], failed["attempt"])
            self.assertIn(failed, recovered["events"])

    def test_readiness_journal_records_flicker_then_stability_without_relaxing_gate(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            backend.set_readiness_path(self.folder / "readiness.json")

            def advance(seconds):
                (base / "status").write_text("SigCgt:\t%s\n" % ("0" if 0.025 <= seconds < 0.075 else "a00"))

            now = self.fake_startup_clock(backend, advance)
            backend.wait_ready(identity, deadline=0.5)
            self.assertGreaterEqual(now[0], 0.075 + 0.15 - 1e-9)
            value = native.read_json(self.folder / "readiness.json")
            self.assertIsNone(value["failure"])
            self.assertEqual([item["ready"] for item in value["events"] if item["event"] == "state"], [True, False, True])
            self.assertGreaterEqual(value["last"]["stable_elapsed"], 0.15)

    def test_readiness_journal_reports_identity_and_competition_at_actual_gate(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            self.fake_startup_clock(backend)
            for gate in ("identity", "single_process"):
                with self.subTest(gate=gate):
                    path = self.folder / (gate + "-readiness.json")
                    backend.set_readiness_path(path)
                    candidate = {**identity, "starttime": 301} if gate == "identity" else identity
                    with patch.object(backend, "assert_single", side_effect=RuntimeError("fixture competing process")):
                        with self.assertRaises(RuntimeError):
                            backend.wait_ready(candidate)
                    failure = native.read_json(path)["failure"]
                    self.assertEqual(failure["error"]["code"], gate)
                    self.assertEqual(failure["event"], "error")
                    self.assertEqual(failure["identity_ok"], gate != "identity")
                    self.assertEqual(failure["single_process"], False if gate == "single_process" else None)
                    self.assertIsNone(failure["sigcgt"])
                    self.assertEqual(failure["remaining"], 20)

    def test_real_ready_reads_guard_after_multiple_short_maps_reads(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            prefix = b"0000-1000 r-xp 0000 00:01 123 /fixture/unrelated.so\n" * 120
            self.assertGreater(len(prefix), 4096)
            complete = prefix + f"1000-2000 r-xp 0000 00:01 124 {backend.guard_path.resolve()}\n".encode()
            maps = base / "maps"
            maps.write_bytes(complete)
            journal = self.folder / "readiness.json"
            backend.set_readiness_path(journal)
            now = self.fake_startup_clock(backend)
            actual_read = os.read

            # Model seq_file short reads below the first page, while retaining
            # the real identity, uniqueness, signal and guard readiness gates.
            with patch.object(native.os, "read", side_effect=lambda fd, size: actual_read(fd, min(size, 2048))):
                self.assertTrue(backend._ready(identity, require_guard=True))
                self.assertEqual(backend._last_readiness["maps_bytes_read"], len(complete))
                self.assertTrue(backend._last_readiness["guard_ok"])
                self.assertEqual(backend.wait_ready(identity, require_guard=True, deadline=0.5), identity)
                success = native.read_json(journal)["last"]
                self.assertEqual(success["event"], "success")
                self.assertEqual(success["maps_bytes_read"], len(complete))
                maps.write_bytes(prefix)
                self.assertFalse(backend._ready(identity, require_guard=True))
                self.assertTrue(backend._last_readiness["identity_ok"])
                self.assertTrue(backend._last_readiness["single_process"])
                self.assertTrue(backend._last_readiness["handlers_ready"])
                self.assertFalse(backend._last_readiness["guard_ok"])
                self.assertEqual(backend._last_readiness["maps_bytes_read"], len(prefix))
                with self.assertRaises(RuntimeError):
                    backend.wait_ready(identity, require_guard=True, deadline=now[0] + 0.1)
            failure = native.read_json(journal)["failure"]
            self.assertTrue(failure["handlers_ready"])
            self.assertFalse(failure["guard_ok"])
            self.assertEqual(failure["maps_bytes_read"], len(prefix))
            self.assertTrue(failure["maps_read_ok"])
            self.assertEqual(failure["guard_paths"], [])

    def test_readiness_journal_io_failure_never_changes_ready_or_restore_gates(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            (backend.proc / "30/maps").write_bytes(b"")
            (backend.proc / "30/environ").write_bytes(b"PATH=/fixture\0")
            backend.set_readiness_path(self.folder / "readiness.json")
            self.fake_startup_clock(backend)
            with patch.object(watchdog, "atomic_json", side_effect=OSError("fixture ENOSPC")):
                self.assertEqual(backend.wait_ready(identity, deadline=0.5), identity)
                self.assertEqual(backend.restore_daemon({"identity": identity}, None), identity)
                (backend.proc / "30/status").write_text("SigCgt:\t0\n")
                with self.assertRaisesRegex(RuntimeError, "信号处理器就绪未确认"):
                    backend.wait_ready(identity, deadline=backend.clock() + 0.05)

    def test_readiness_journal_is_bounded_and_rate_limited_without_private_fields(self):
        journal = watchdog.ReadinessJournal(self.folder / "readiness.json")
        base = {"attempt": 1, "operation": "wait_ready", "event": "state", "result": "waiting",
                "pid": 30, "starttime": 300, "exe": "/histor/bin/temp_control", "guard_expected": "/run/fan_guard.so",
                "sigcgt_fields": ["a00"], "guard_paths": ["/run/" + "x" * 6000 + "/fan_guard.so"] * 40}
        with patch.object(watchdog, "atomic_json", wraps=native.atomic_json) as write:
            for index in range(400):
                journal.record({**base, "monotonic": index * 0.001})
            journal.record({**base, "monotonic": 0.4, "event": "timeout", "result": "timeout"}, force=True)
            self.assertLessEqual(write.call_count, 6)  # Never write/fsync at the 25ms probe cadence.
        value = native.read_json(journal.path)
        self.assertLessEqual(journal.path.stat().st_size, 131072)
        self.assertLessEqual(len(value["events"]), 128)
        self.assertGreater(value["dropped_events"], 0)
        self.assertEqual(value["first"]["sequence"], 1)
        self.assertEqual(value["last"]["event"], "timeout")
        self.assertEqual(value["failure"], value["last"])
        self.assertIn("guard_paths", value["last"]["fields_truncated"])
        self.assertGreater(value["last"]["guard_paths_omitted"], 0)
        encoded = journal.path.read_text()
        for forbidden in ("PRIVATE_TOKEN", "DISK_AUTOSLEEP", "lease.bin", "descriptor", "environ"):
            self.assertNotIn(forbidden, encoded)
        resumed = watchdog.ReadinessJournal(journal.path)
        resumed.record({**base, "monotonic": 0.5, "event": "success", "result": "ready"}, force=True)
        recovered = native.read_json(journal.path)
        self.assertEqual(recovered["failure"], value["failure"])
        self.assertEqual(recovered["first"], value["first"])
        self.assertEqual(recovered["last"]["result"], "ready")

    def test_sync_never_sends_get_until_both_handlers_are_ready(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            (base / "maps").write_bytes(b"")
            (base / "environ").write_bytes(b"PATH=/fixture\0")
            calls = []
            rpc = type("RPC", (), {"_has_socket": lambda _: True, "_config_mode": lambda _: 5,
                                    "_get_mode": lambda _: calls.append("GET") or 5})()
            now = self.fake_startup_clock(backend)
            for raw in (b"", b"SigCgt:\t0\n", b"SigCgt:\t200\n", b"SigCgt:\t800\n",
                        b"SigCgt:\t-a00\n", b"SigCgt:\ta00\nSigCgt:\ta00\n", b"SigCgt:\tinvalid\n"):
                (base / "status").write_bytes(raw)
                with patch.object(backend, "_histor", return_value=rpc), \
                        self.assertRaisesRegex(RuntimeError, "信号处理器就绪未确认"):
                    backend.sync_mode(self.folder)
            self.assertEqual(calls, [])
            self.assertEqual(now[0], 0)  # No second five-second startup wait before GET.
            self.assertTrue(backend.alive(identity))

    def test_sync_get_exit_and_guard_disappearance_cannot_return_success(self):
        with self.real_proc_backend() as backend:
            base = backend.proc / "30"
            (base).mkdir()
            (base / "environ").write_bytes(b"R1_FAN_LEASE=" + os.fsencode(self.folder / "lease.bin") + b"\0")
            calls = []
            for failure in ("exit", "guard_lost", "late_exit"):
                self.write_proc_identity(backend, 30, 300)
                (base / "maps").write_text(f"0000-1000 r-xp 0000 00:01 123 {backend.guard_path.resolve()}\n")

                def advance(seconds):
                    if failure == "late_exit" and seconds >= 0.05:
                        self.write_proc_identity(backend, 30, 300, state="Z")

                self.fake_startup_clock(backend, advance)

                def get_mode(_):
                    calls.append("GET")
                    if failure == "exit":
                        self.write_proc_identity(backend, 30, 300, state="Z")
                    elif failure == "guard_lost":
                        (base / "maps").write_bytes(b"")
                    return 5

                rpc = type("RPC", (), {"_has_socket": lambda _: True, "_config_mode": lambda _: 5,
                                        "_get_mode": get_mode})()
                with patch.object(backend, "_histor", return_value=rpc), \
                        patch.object(backend, "_write") as write, self.assertRaises(RuntimeError):
                    backend.sync_mode(self.folder)
                write.assert_not_called()
            self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_restore_get_followed_by_daemon_death_keeps_recovery_descriptor(self):
        owner = self.make({"cpu"})
        owner.begin()

        def sync_then_die(*args, **kwargs):
            self.backend.live.pop(self.backend.vendor["pid"], None)
            return "auto"

        with patch.object(self.backend, "sync_mode", side_effect=sync_then_die):
            self.assertFalse(owner.restore("lease_or_identity_failed"))
        status = native.read_json(self.folder / "status.json")
        self.assertEqual(status["phase"], "recovery_failed")
        self.assertFalse(status["restore_ok"])
        self.assertIn("vendor_daemon_unconfirmed", status["recovery_errors"])
        self.assertIn("original", native.read_json(self.folder / "descriptor.json"))

    def test_capture_failure_with_no_vendor_never_claims_recovery_or_redacts(self):
        owner = self.make({"cpu"})
        self.backend.live.pop(self.backend.vendor["pid"])
        with patch.object(self.backend, "capture", side_effect=ValueError("fixture preflight failed")):
            with self.assertRaises(ValueError):
                owner.begin()
        self.assertFalse(owner.restore("lease_or_identity_failed"))
        self.assertEqual(self.backend.events, [])
        status = native.read_json(self.folder / "status.json")
        self.assertEqual(status["phase"], "recovery_failed")
        self.assertFalse(status["restore_ok"])
        self.assertNotIn("recovered", native.read_json(self.folder / "descriptor.json"))

    def test_restored_vendor_is_waited_ready_before_reuse_and_never_spawned_twice(self):
        with self.real_proc_backend() as backend:
            identity = self.write_proc_identity(backend, 30, 300)
            base = backend.proc / "30"
            (base / "maps").write_bytes(b"")
            (base / "environ").write_bytes(b"PATH=/fixture\0")
            (base / "status").write_text("SigCgt:\t0\n")

            def advance(seconds):
                if seconds >= 0.25:
                    (base / "status").write_text("SigCgt:\ta00\n")

            now = self.fake_startup_clock(backend, advance)
            with patch.object(backend, "spawn", side_effect=AssertionError("不可另起副本")):
                self.assertEqual(backend.restore_daemon({"identity": identity}, None), identity)
            self.assertGreaterEqual(now[0], 0.4 - 1e-9)

    def test_oversized_recovery_descriptor_is_refused_before_writing(self):
        with self.assertRaises(ValueError):
            native.atomic_json(self.folder / "descriptor-too-large.json", {"environment": "x" * 140000})
        self.assertFalse((self.folder / "descriptor-too-large.json").exists())

    def test_recovery_hardware_does_not_depend_on_writable_status_or_redaction(self):
        owner = self.make({"cpu", "system"})
        owner.begin()
        self.backend.events.clear()
        with patch.object(watchdog, "atomic_json", side_effect=OSError("fixture ENOSPC")):
            self.assertTrue(owner.restore("lease_or_identity_failed"))
        self.assertEqual([event[0] for event in self.backend.events], ["write", "restore_daemon", "sync_mode"])
        self.assertEqual(self.backend.events[0][1], {"cpu": 255, "system": 255})
        self.assertEqual(owner.phase, "released")
        events = list(self.backend.events)
        self.assertTrue(owner.restore("release_requested"))
        self.assertEqual(events, self.backend.events)

    def test_watchdog_owner_death_full_loop_restores_independently(self):
        owner = self.make({"system"})

        def owner_dies(seconds):
            self.now += int(seconds * 1_000_000_000)
            self.backend.live.pop(10, None)

        owner.run(sleep=owner_dies)
        status = native.read_json(self.folder / "status.json")
        self.assertEqual(status["phase"], "released")
        self.assertEqual(status["release_cause"], "owner_stopped")
        self.assertTrue(self.backend.alive(self.backend.vendor))

    def test_recovery_handles_spawn_success_before_guarded_pid_journal_commit(self):
        owner = self.make({"cpu"})
        owner.begin()
        descriptor = native.read_json(self.folder / "descriptor.json")
        descriptor["guarded"] = None
        native.atomic_json(self.folder / "descriptor.json", descriptor)
        recovery = watchdog.FanWatchdog(self.folder, self.backend, clock_ns=lambda: self.now)
        self.assertTrue(recovery.restore("supervisor_stopped"))
        self.assertTrue(self.backend.alive(self.backend.vendor))
        self.assertFalse(self.backend.alive(self.backend.guarded))

    def test_system_backend_recognizes_only_this_exact_guard_and_lease(self):
        backend = watchdog.SystemBackend(root=self.folder, guard_path=self.folder / "fan_guard.so")
        proc = self.folder / f"proc/{self.backend.guarded['pid']}"
        proc.mkdir(parents=True)
        maps = proc / "maps"
        env = proc / "environ"
        guard = str(backend.guard_path.resolve())
        lease = self.folder / "lease.bin"
        maps.write_text(f"0000-1000 r-xp 0000 00:01 123 {guard}\n")
        env.write_bytes(b"R1_FAN_LEASE=" + os.fsencode(lease) + b"\0LD_PRELOAD=" + os.fsencode(guard) + b"\0")
        with patch.object(backend, "alive", return_value=True):
            self.assertEqual(backend._owner_kind(self.backend.guarded, lease), "ours")
            self.assertEqual(backend._owner_kind(self.backend.guarded, self.folder / "other-lease"), "unknown")
            maps.write_text(f"0000-1000 r-xp 0000 00:01 123 {guard}.backup\n")
            self.assertEqual(backend._owner_kind(self.backend.guarded, lease), "unknown")
            env.write_bytes(b"PATH=/fixture\0")
            maps.write_text("0000-1000 r-xp 0000 00:01 123 /fixture/libc.so\n")
            self.assertEqual(backend._owner_kind(self.backend.guarded, lease), "vendor")

    def test_system_backend_stops_journalless_own_guard_but_reuses_verified_vendor(self):
        backend = watchdog.SystemBackend(root=self.folder)
        original = self.backend.capture()
        with patch.object(backend, "_firmware_signature"), \
                patch.object(backend, "_vendors", side_effect=[[self.backend.guarded], []]), \
                patch.object(backend, "_owner_kind", return_value="ours"), \
                patch.object(backend, "stop_process") as stop, \
                patch.object(backend, "spawn", return_value=self.backend.vendor) as spawn:
            self.assertEqual(backend.restore_daemon(original, None, lease=self.folder / "lease.bin"), self.backend.vendor)
            stop.assert_called_once_with(self.backend.guarded)
            spawn.assert_called_once_with(original)
        with patch.object(backend, "_firmware_signature"), \
                patch.object(backend, "_vendors", return_value=[self.backend.vendor]), \
                patch.object(backend, "_owner_kind", return_value="vendor"), \
                patch.object(backend, "wait_ready", side_effect=lambda identity: identity), \
                patch.object(backend, "stop_process") as stop, patch.object(backend, "spawn") as spawn:
            self.assertEqual(backend.restore_daemon(original, None, lease=self.folder / "lease.bin"), self.backend.vendor)
            stop.assert_not_called()
            spawn.assert_not_called()

    def recovery_adapter(self, phase, child, events):
        native.atomic_json(self.folder / "descriptor.json", {"version": 1, "original": self.backend.capture(),
                           "channels": ["cpu"], "owner": self.backend.owner})
        native.atomic_json(self.folder / "status.json", {"phase": phase, "restore_ok": False})

        def recover(command, **options):
            events.append("recover")
            self.assertIn("--recover", command)
            native.atomic_json(self.folder / "status.json", {"phase": "released", "restore_ok": True})
            native.atomic_json(self.folder / "descriptor.json", {"version": 1, "recovered": True})
            return type("Completed", (), {"wait": lambda _, timeout: 0, "poll": lambda _: 0})()

        adapter = native.FanNativeAdapter(self.folder / "persistent", self.folder / "ram", backend=self.backend,
                                          allow_writes=True, popen=recover)
        adapter.folder, adapter.channels, adapter.child = self.folder, {"cpu"}, child
        return adapter

    def test_explicit_release_retries_recovery_failed_once_and_then_is_idempotent(self):
        events = []
        completed = type("Completed", (), {"wait": lambda _, timeout: 0, "poll": lambda _: 0})()
        adapter = self.recovery_adapter("recovery_failed", completed, events)
        adapter.release()
        self.assertEqual(events, ["recover"])
        self.assertEqual(adapter.channels, set())
        adapter.release()
        self.assertEqual(events, ["recover"])

    def test_hung_live_supervisor_is_terminated_before_one_bounded_recovery(self):
        events = []

        class Hung:
            terminated = False

            def wait(child, timeout):
                if not child.terminated:
                    raise subprocess.TimeoutExpired("fixture-watchdog", timeout)
                return 0

            def terminate_verified(child):
                events.append("terminate-own-watchdog")
                child.terminated = True

        adapter = self.recovery_adapter("active", Hung(), events)
        adapter.release()
        self.assertEqual(events, ["terminate-own-watchdog", "recover"])
        self.assertTrue(native.read_json(self.folder / "status.json")["restore_ok"])

    def test_hung_watchdog_identity_replacement_is_never_signalled(self):
        identity = {"pid": 99, "starttime": 990, "exe": "/fixture/python3"}
        self.backend.live[99] = {**identity, "starttime": 991}
        launcher = type("Launcher", (), {"poll": lambda _: 0})()
        handle = native.SystemdSupervisor(launcher, self.folder, self.backend)
        handle.identity = identity
        with self.assertRaises(RuntimeError):
            handle.terminate_verified()
        self.assertFalse(any(event[0] == "stop" for event in self.backend.events))
        self.backend.live[99] = identity
        handle.terminate_verified()
        self.assertIn(("stop", 99), self.backend.events)
        self.assertTrue(self.backend.alive(self.backend.vendor))

    def test_each_explicit_recovery_gets_a_new_transient_unit_name(self):
        backend = watchdog.SystemBackend(root="/")
        calls = []

        def popen(command, **options):
            calls.append(command)
            return type("Launcher", (), {"poll": lambda _: None})()

        for _ in range(2):
            backend.launch_watchdog(["/usr/bin/python3", "--recover"], "/run/r1-toolbox/fan-abc123", popen)
        units = [next(arg for arg in command if arg.startswith("--unit=")) for command in calls]
        self.assertNotEqual(units[0], units[1])

    def test_unknown_guard_is_not_signalled_or_replaced_during_recovery(self):
        backend = watchdog.SystemBackend(root=self.folder)
        original = self.backend.capture()
        with patch.object(backend, "_firmware_signature"), \
                patch.object(backend, "_vendors", return_value=[self.backend.guarded]), \
                patch.object(backend, "_owner_kind", return_value="unknown"), \
                patch.object(backend, "stop_process") as stop, patch.object(backend, "spawn") as spawn:
            with self.assertRaises(RuntimeError):
                backend.restore_daemon(original, None, lease=self.folder / "lease.bin")
            stop.assert_not_called()
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
