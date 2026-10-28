"""Vendor adapter fixtures: no actual hardware, factory binaries, or services."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("histor_under_test", ROOT / "runtime/histor.py")
histor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(histor)


class FakeSocket:
    def __init__(self, owner, chunks):
        self.owner, self.chunks = owner, list(chunks)
        self.closed = False

    def settimeout(self, seconds):
        self.owner.timeouts.append(seconds)

    def bind(self, address):
        self.address = Path(address)
        self.address.touch()
        self.owner.bound.append(self.address)

    def connect(self, address):
        self.owner.targets.append(address)
        if self.owner.connect_error:
            raise self.owner.connect_error

    def sendall(self, value):
        self.owner.sent.append(value)
        self.owner.permissions.append(self.address.stat().st_mode & 0o777)

    def recv(self, length):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, Exception):
            raise value
        if len(value) > length:
            self.chunks.insert(0, value[length:])
        return value[:length]

    def close(self):
        self.closed = True
        self.owner.closed += 1


class SocketFactory:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.sent, self.targets, self.bound = [], [], []
        self.permissions, self.timeouts, self.types = [], [], []
        self.closed = 0
        self.connect_error = None

    def __call__(self, family, kind):
        self.types.append((family, kind))
        if not self.replies:
            raise AssertionError("未安排的接口调用")
        reply = self.replies.pop(0)
        return FakeSocket(self, reply if isinstance(reply, list) else [reply])


class HistorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Path(tempfile.mkdtemp(prefix="r1-histor-test-", dir="/tmp"))
        self.root = (self.fixture / "host").resolve()
        self.root.mkdir()
        self.calls = []
        self.now = 100.0
        self.output, self.code = "cpu[1300]-sys[850]", 0
        self.failure = None
        self.sockets = SocketFactory()
        self.addCleanup(self.recycle)

    def recycle(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir() and not (trash / self.fixture.name).exists():
            shutil.move(str(self.fixture), str(trash / self.fixture.name))
        else:
            print("测试临时目录已保留：", self.fixture)

    def write(self, path, contents):
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
        return target

    def runner(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.failure:
            raise self.failure
        return SimpleNamespace(stdout=self.output, stderr="fixture", returncode=self.code)

    def adapter(self, enabled=False, config=True):
        self.write("/histor/bin/product_test", "fixture executable, never run")
        if config:
            self.write("/tmp/nand_config/config.txt", json.dumps({"fan": {"fan_mode": 5}, "password": "must-not-return"}))
        adapter = histor.HistorAdapter(root=self.root, runtime_dir=self.fixture / "ipc", runner=self.runner,
                                       clock=lambda: self.now, socket_factory=self.sockets,
                                       allow_writes=enabled, hardware_supported=True)
        adapter._has_device = lambda: True
        adapter._has_socket = lambda: True
        return adapter

    def activate(self):
        adapter = self.adapter(True)
        self.assertTrue(adapter.poll_once()["control_available"])
        return adapter

    def test_constructor_snapshot_and_disabled_probe_never_connect_or_execute(self):
        adapter = self.adapter()
        self.assertFalse(adapter.snapshot()["available"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sockets.sent, [])
        self.assertFalse((self.fixture / "ipc").exists())
        state = adapter.poll_once()
        self.assertEqual((state["cpu_rpm"], state["system_rpm"]), (1300, 850))
        self.assertEqual(state["mode"], "auto")
        self.assertFalse(state["control_available"])
        self.assertEqual(self.sockets.sent, [])
        self.assertNotIn("must-not-return", json.dumps(state))
        self.assertEqual(self.calls, [([str(self.root / "histor/bin/product_test"), "-o"], 5)])

    def test_exact_get_packet_bound_socket_permissions_and_padded_response(self):
        adapter = self.adapter(True)
        self.sockets.replies = [b"5" + b"\0" * 131071]
        state = adapter.poll_once()
        self.assertTrue(state["control_available"])
        self.assertEqual(adapter._get_mode(), 5)
        self.assertEqual(self.sockets.sent, [b"emhistor;get_fan_status;0\0"])
        self.assertEqual(self.sockets.targets, [str(self.root / "var/tmp/emhistor")])
        self.assertEqual(self.sockets.permissions, [0o700])
        self.assertEqual(self.sockets.types, [(socket.AF_UNIX, socket.SOCK_STREAM)])
        self.assertEqual(self.sockets.closed, 1)

    def test_set_requires_confirmation_and_valid_mode_before_any_io(self):
        adapter = self.activate()
        original = list(self.sockets.sent)
        for bad in (None, 1, 2, True, "stop", "manual", "low;reboot", []):
            with self.assertRaises(ValueError):
                adapter.set_mode(bad, confirmed=True)
        for confirmed in (False, None, 1, "true"):
            with self.assertRaises(ValueError):
                adapter.set_mode("low", confirmed=confirmed)
        self.assertEqual(self.sockets.sent, original)

    def test_success_requires_precise_ack_and_mode_readback(self):
        adapter = self.activate()
        self.sockets.replies = [b"5\0", b"OK!\0", b"3\0"]
        state = adapter.set_mode("medium", confirmed=True)
        self.assertEqual(state["mode"], "medium")
        self.assertEqual(state["mode_code"], 3)
        self.assertEqual(self.sockets.sent[-3:], [b"emhistor;get_fan_status;0\0",
                                                b"emhistor;set_fan_status;3\0",
                                                b"emhistor;get_fan_status;0\0"])
        self.assertTrue(state["control_available"])

    def test_every_supported_policy_uses_exact_vendor_code_and_never_stop_mode(self):
        adapter = self.activate()
        for name, code in (("low", 2), ("medium", 3), ("high", 4), ("auto", 5)):
            self.sockets.replies = [b"5\0", b"OK!\0", str(code).encode() + b"\0"]
            self.assertEqual(adapter.set_mode(name, confirmed=True)["mode"], name)
            self.assertEqual(self.sockets.sent[-2], f"emhistor;set_fan_status;{code}\0".encode())

    def test_unknown_ack_and_wrong_readback_disable_control_without_retry(self):
        for replies, expected_count in (([b"5\0", b"success\0"], 2),
                                        ([b"5\0", b"OK!\0", b"4\0"], 3),
                                        ([b"5\0", b"xx\0"], 2),
                                        ([b"1\0"], 1)):
            with self.subTest(replies=replies):
                adapter = self.activate()
                count = len(self.sockets.sent)
                self.sockets.replies = replies
                with self.assertRaises(ValueError):
                    adapter.set_mode("low", confirmed=True)
                self.assertEqual(len(self.sockets.sent) - count, expected_count)
                self.assertFalse(adapter.snapshot()["control_available"])
                self.assertEqual(adapter.snapshot()["mode"], "auto")

    def test_rpc_handles_chunk_boundaries_short_valid_eof_and_bad_padding(self):
        adapter = self.adapter(True)
        self.sockets.replies = [[b"O", b"K", b"!", b"\0" * 100], b"OK!"]
        self.assertEqual(adapter._rpc("set_fan_status", 4), "OK!")
        self.assertEqual(adapter._rpc("set_fan_status", 4), "OK!")
        for reply in (b"", b"5\0bad", b"\xff\0", b"5" * 131072):
            self.sockets.replies = [reply]
            with self.assertRaises(ValueError):
                adapter._get_mode()

    def test_timeout_or_connect_failure_closes_socket_and_hides_control(self):
        for error in (TimeoutError(), OSError("fixture")):
            adapter = self.adapter(True)
            self.sockets.replies = [[error]]
            state = adapter.poll_once()
            self.assertTrue(state["control_available"])
            with self.assertRaises(ValueError):
                adapter.set_mode("low", confirmed=True)
            self.assertFalse(adapter.snapshot()["control_available"])
            self.assertEqual(state["cpu_rpm"], 1300)
            self.assertEqual(self.sockets.closed, len(self.sockets.sent))

    def test_missing_or_malformed_config_never_invokes_getter_or_initialises_it(self):
        adapter = self.adapter(True, config=False)
        bad_values = (b"", b"not-json", b"{}", b'{"fan":5}', b'{"fan":{"fan_mode":true}}',
                      b'{"fan":{"fan_mode":1}}', b'{"fan":{"fan_mode":"5"}}', b'{"fan":{"fan_mode":6}}')
        self.assertFalse(adapter.poll_once()["control_available"])
        for value in bad_values:
            self.write("/tmp/nand_config/config.txt", value)
            self.assertFalse(adapter.poll_once()["control_available"])
        self.assertEqual(self.sockets.sent, [])

    def test_rpm_capability_is_independent_of_native_policy_transport(self):
        self.output = "cpu[-1]-sys[-1]"
        adapter = self.adapter(True)
        state = adapter.poll_once()
        self.assertTrue(state["control_available"])
        self.assertEqual(self.sockets.sent, [])
        self.assertIsNone(state["cpu_rpm"])
        self.assertIsNone(state["system_rpm"])
        self.assertIsNotNone(state["telemetry_reason"])

    def test_failed_or_invalid_rpm_never_fabricates_zero(self):
        adapter = self.adapter()
        for value in ("nonsense", "cpu[100001]-sys[0]", "cpu[-2]-sys[200]", "cpu[1]-sys[2]\nextra",
                      "cpu[1]-sys[2]\ncpu[3]-sys[4]", "cpu[-1]-sys[-1]"):
            self.output = value
            self.now += 61
            state = adapter.poll_once()
            self.assertIsNone(state["cpu_rpm"])
            self.assertIsNone(state["system_rpm"])
        self.output = "cpu[0]-sys[-1]"
        self.now += 61
        self.assertEqual(adapter.poll_once()["cpu_rpm"], 0)
        self.code = 1
        self.now += 61
        self.assertIsNone(adapter.poll_once()["cpu_rpm"])
        self.failure = subprocess.TimeoutExpired("fixture", 5)
        self.now += 61
        self.assertIsNone(adapter.poll_once()["cpu_rpm"])

    def test_grade_is_exact_little_endian_binary_not_guessed_ascii(self):
        adapter = self.adapter()
        self.write("/tmp/fan", b"\x03\0\0\0")
        self.assertEqual(adapter.poll_once()["grade"], 3)
        for invalid in (b"3", b"\x03\0\0\0more", b"\xff\xff\xff\xff"):
            self.write("/tmp/fan", invalid)
            self.assertIsNone(adapter.poll_once()["grade"])

    def test_read_uses_nofollow_and_oversize_config_stays_unknown(self):
        adapter = self.adapter(True, config=False)
        outside = self.fixture / "secret"
        outside.write_text('{"fan":{"fan_mode":5},"password":"must-not-read"}')
        target = self.root / "tmp/nand_config/config.txt"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        self.assertIsNone(adapter.poll_once()["mode"])
        self.assertEqual(self.sockets.sent, [])

    def test_cached_snapshots_do_not_probe_and_stale_controls_hide(self):
        adapter = self.activate()
        call_count, sent_count = len(self.calls), len(self.sockets.sent)
        for _ in range(20):
            adapter.snapshot()
        self.assertEqual(len(self.calls), call_count)
        self.assertEqual(len(self.sockets.sent), sent_count)
        self.now += 16
        state = adapter.snapshot()
        self.assertFalse(state["control_available"])
        self.assertIsNone(state["cpu_rpm"])
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_reads_current_native_config_even_when_cache_is_stale_without_commands(self):
        adapter = self.activate()
        self.assertEqual(adapter.snapshot()["mode"], "auto")
        original_calls = list(self.calls)
        self.now += 16
        self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":2},"password":"must-not-return"}')
        self.assertFalse(adapter.snapshot()["control_available"])
        self.assertEqual(adapter.verified_mode(), "low")
        self.assertEqual(adapter.snapshot()["mode"], "low")
        self.assertFalse(adapter.snapshot()["control_available"])
        self.assertEqual(self.calls, original_calls)
        self.assertEqual(self.sockets.sent, [])
        self.assertNotIn("must-not-return", json.dumps(adapter.snapshot()))

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_keeps_writes_disabled_failed_rpc_and_stopped_gates(self):
        for state in ("writes_disabled", "rpc_disabled", "stopped", "device_missing", "socket_missing"):
            with self.subTest(state=state):
                adapter = self.adapter(True)
                if state == "writes_disabled":
                    adapter.allow_writes = False
                elif state == "rpc_disabled":
                    adapter._control_disabled = True
                elif state == "stopped":
                    adapter.stopped.set()
                elif state == "device_missing":
                    adapter._has_device = lambda: False
                else:
                    adapter._has_socket = lambda: False
                self.assertIsNone(adapter.verified_mode())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sockets.sent, [])

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_requires_known_current_driver_and_rechecks_after_read(self):
        adapter = self.native_adapter(histor._SUPPORTED_DRIVER_NOTE)
        self.assertEqual(adapter.verified_mode(), "auto")
        self.write("/sys/module/hikio/notes/.note.gnu.build-id", histor._UNSUPPORTED_DRIVER_NOTE)
        self.assertIsNone(adapter.verified_mode())
        self.write("/sys/module/hikio/notes/.note.gnu.build-id", histor._SUPPORTED_DRIVER_NOTE)
        original_mode = adapter._config_mode
        def replaced_during_mode_read():
            value = original_mode()
            self.write("/sys/module/hikio/notes/.note.gnu.build-id", histor._UNSUPPORTED_DRIVER_NOTE)
            return value
        adapter._config_mode = replaced_during_mode_read
        self.assertIsNone(adapter.verified_mode())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sockets.sent, [])

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_never_waits_behind_factory_rpm_poll_or_connects_rpc(self):
        adapter = self.adapter(True)
        entered, finish = threading.Event(), threading.Event()
        failures = []
        def slow_runner(argv, timeout):
            self.calls.append((argv, timeout))
            entered.set()
            if not finish.wait(3):
                raise TimeoutError("fixture RPM poll did not finish")
            return SimpleNamespace(stdout="cpu[1300]-sys[850]", stderr="", returncode=0)
        def poll():
            try:
                adapter.poll_once()
            except Exception as error:
                failures.append(error)
        adapter.runner = slow_runner
        worker = threading.Thread(target=poll)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":2}}')
            self.assertEqual(adapter.verified_mode(), "low")
            self.assertFalse(finish.is_set())
            self.assertEqual(adapter._mode_generation, 0)
        finally:
            finish.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sockets.sent, [])

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_rejects_read_during_real_set_rpc_without_waiting(self):
        adapter = self.activate()
        entered, finish = threading.Event(), threading.Event()
        results, failures, commands = [], [], []
        def rpc(command, value):
            commands.append((command, value))
            if command == "set_fan_status":
                self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":4}}')
                return "OK!"
            if len(commands) == 1:
                entered.set()
                self.assertTrue(finish.wait(3))
                return "5"
            return "4"
        def change():
            try:
                results.append(adapter.set_mode("high", confirmed=True))
            except Exception as error:
                failures.append(error)
        adapter._rpc = rpc
        worker = threading.Thread(target=change)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertIsNone(adapter.verified_mode())
            self.assertTrue(adapter._mode_mutating)
            self.assertFalse(finish.is_set())
        finally:
            finish.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]["mode"], "high")
        self.assertFalse(adapter._mode_mutating)
        self.assertEqual(adapter._mode_generation, 2)
        self.assertEqual(adapter.verified_mode(), "high")

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_generation_rejects_complete_rpc_overlapping_passive_read(self):
        adapter = self.activate()
        entered, finish = threading.Event(), threading.Event()
        results, failures = [], []
        original_mode = adapter._config_mode
        reader = None
        def config_read():
            value = original_mode()
            if threading.current_thread() is reader:
                entered.set()
                self.assertTrue(finish.wait(3))
            return value
        def rpc(command, value):
            if command == "set_fan_status":
                self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":4}}')
                return "OK!"
            return str(original_mode())
        def read():
            try:
                results.append(adapter.verified_mode())
            except Exception as error:
                failures.append(error)
        adapter._config_mode, adapter._rpc = config_read, rpc
        reader = threading.Thread(target=read)
        reader.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertEqual(adapter.set_mode("high", confirmed=True)["mode"], "high")
        finally:
            finish.set()
            reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [None])
        self.assertFalse(adapter._mode_mutating)
        self.assertEqual(adapter._mode_generation, 2)
        self.assertEqual(adapter.snapshot()["mode"], "high")

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_fence_closes_after_failed_set_and_keeps_control_disabled(self):
        adapter = self.activate()
        with patch.object(adapter, "_rpc", return_value="invalid"):
            with self.assertRaises(ValueError):
                adapter.set_mode("high", confirmed=True)
        self.assertFalse(adapter._mode_mutating)
        self.assertEqual(adapter._mode_generation, 2)
        self.assertIsNone(adapter.verified_mode())

    @patch.object(histor.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
    def test_verified_mode_rejects_unknown_native_mode_without_initializing_defaults(self):
        adapter = self.adapter(True)
        for data in ('{}', '{"fan":{"fan_mode":1}}', '{"fan":{"fan_mode":true}}', 'bad json'):
            with self.subTest(data=data):
                self.write("/tmp/nand_config/config.txt", data)
                self.assertIsNone(adapter.verified_mode())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sockets.sent, [])

    def test_stop_never_restores_guessed_vendor_policy_or_restarts_worker(self):
        adapter = self.activate()
        count = len(self.sockets.sent)
        adapter.stop()
        adapter.start()
        self.assertIsNone(adapter.thread)
        self.assertFalse(adapter.snapshot()["control_available"])
        self.assertEqual(len(self.sockets.sent), count)
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)

    def test_failed_rpc_remains_disabled_after_passive_policy_refresh(self):
        adapter = self.activate()
        self.sockets.replies = [b"5\0", b"xx\0"]
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)
        count = len(self.sockets.sent)
        self.now += 10
        state = adapter.poll_once()
        self.assertFalse(state["control_available"])
        self.assertEqual(state["mode"], "auto")
        self.assertEqual(len(self.sockets.sent), count)
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)
        self.assertEqual(len(self.sockets.sent), count)

    def test_unsupported_rpm_backs_off_while_policy_remains_fresh(self):
        self.output = "cpu[-1]-sys[-1]"
        adapter = self.adapter(True)
        adapter.poll_once()
        self.assertEqual(len(self.calls), 1)
        self.now += 10
        self.assertTrue(adapter.poll_once()["control_available"])
        self.assertEqual(len(self.calls), 1)
        self.now += 51
        adapter.poll_once()
        self.assertEqual(len(self.calls), 2)

    def test_policy_ack_or_presence_cannot_enable_unverified_physical_fan_control(self):
        self.write("/histor/bin/product_test", "fixture never run")
        self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":5}}')
        adapter = histor.HistorAdapter(root=self.root, runtime_dir=self.fixture / "ipc",
                                       runner=self.runner, clock=lambda: self.now,
                                       socket_factory=self.sockets, allow_writes=True)
        adapter._has_device = adapter._has_socket = lambda: True
        state = adapter.poll_once()
        self.assertTrue(state["policy_available"])
        self.assertTrue(state["policy_only"])
        self.assertFalse(state["control_available"])
        self.assertEqual(self.sockets.sent, [])
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)
        self.assertEqual(self.sockets.sent, [])

    def native_adapter(self, note):
        self.write("/histor/bin/product_test", "fixture never run")
        self.write("/tmp/nand_config/config.txt", '{"fan":{"fan_mode":5}}')
        if note is not None:
            self.write("/sys/module/hikio/notes/.note.gnu.build-id", note)
        adapter = histor.HistorAdapter(root=self.root, runtime_dir=self.fixture / "ipc",
                                       runner=self.runner, clock=lambda: self.now,
                                       socket_factory=self.sockets, allow_writes=True)
        adapter._has_device = adapter._has_socket = lambda: True
        return adapter

    def test_exact_loaded_initrd_note_enables_native_policy_without_rpm(self):
        self.output = "cpu[-1]-sys[-1]"
        adapter = self.native_adapter(histor._SUPPORTED_DRIVER_NOTE)
        self.assertFalse(adapter.snapshot()["hardware_supported"])
        state = adapter.poll_once()
        self.assertTrue(state["hardware_supported"])
        self.assertEqual(state["driver_build_id"], "c909da338be2c4700bb00b233cb189e7a32655b7")
        self.assertTrue(state["control_available"])
        self.assertFalse(state["policy_only"])
        self.assertFalse(state["rpm_available"])
        self.assertIsNone(state["cpu_rpm"])
        self.assertIsNone(state["system_rpm"])
        self.assertEqual(self.sockets.sent, [])
        self.sockets.replies = [b"5\0", b"OK!\0", b"4\0"]
        self.assertEqual(adapter.set_mode("high", confirmed=True)["mode"], "high")

    def test_rootfs_unknown_truncated_missing_and_malformed_notes_hide_control(self):
        unknown = histor._GNU_BUILD_NOTE_HEADER + bytes.fromhex("11" * 20)
        for note in (histor._UNSUPPORTED_DRIVER_NOTE, unknown,
                     histor._SUPPORTED_DRIVER_NOTE[:-1], histor._SUPPORTED_DRIVER_NOTE + b"\0",
                     b"BAD!" + histor._SUPPORTED_DRIVER_NOTE[4:], None):
            with self.subTest(note=note):
                # Each iteration uses a separate root; None never sees a note
                # left by a preceding case, and never falls back to real /sys.
                prior_root = self.root
                self.root = prior_root / "nested"
                self.root.mkdir()
                adapter = self.native_adapter(note)
                state = adapter.poll_once()
                self.assertFalse(state["hardware_supported"])
                self.assertFalse(state["control_available"])
                self.assertTrue(state["policy_only"])
                with self.assertRaises(ValueError):
                    adapter.set_mode("high", confirmed=True)
                self.assertEqual(self.sockets.sent, [])

    def test_runtime_loaded_module_replacement_blocks_set_before_any_rpc(self):
        adapter = self.native_adapter(histor._SUPPORTED_DRIVER_NOTE)
        self.assertTrue(adapter.poll_once()["control_available"])
        self.write("/sys/module/hikio/notes/.note.gnu.build-id", histor._UNSUPPORTED_DRIVER_NOTE)
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)
        self.assertFalse(adapter.snapshot()["hardware_supported"])
        self.assertFalse(adapter.snapshot()["control_available"])
        self.assertTrue(adapter.snapshot()["policy_only"])
        self.assertEqual(self.sockets.sent, [])

    def test_driver_replacement_during_get_blocks_subsequent_write(self):
        adapter = self.native_adapter(histor._SUPPORTED_DRIVER_NOTE)
        adapter.poll_once()
        original_get = adapter._get_mode
        self.sockets.replies = [b"5\0"]
        def replaced_after_get():
            value = original_get()
            self.write("/sys/module/hikio/notes/.note.gnu.build-id", histor._UNSUPPORTED_DRIVER_NOTE)
            return value
        adapter._get_mode = replaced_after_get
        with self.assertRaises(ValueError):
            adapter.set_mode("high", confirmed=True)
        self.assertEqual(self.sockets.sent, [b"emhistor;get_fan_status;0\0"])
        self.assertFalse(adapter.snapshot()["control_available"])

    def test_loaded_note_symlink_cannot_escape_fixture_root(self):
        adapter = self.native_adapter(None)
        secret = self.fixture / "outside-note"
        secret.write_bytes(histor._SUPPORTED_DRIVER_NOTE)
        link = self.root / "sys/module/hikio/notes/.note.gnu.build-id"
        link.parent.mkdir(parents=True)
        link.symlink_to(secret)
        self.assertFalse(adapter.poll_once()["hardware_supported"])
        self.assertFalse(adapter.snapshot()["control_available"])

    def test_intermediate_symlink_cannot_execute_binary_outside_fixture_root(self):
        outside = self.fixture / "outside"
        (outside / "bin").mkdir(parents=True)
        (outside / "bin/product_test").write_text("never run")
        (self.root / "histor").symlink_to(outside)
        adapter = histor.HistorAdapter(root=self.root, runtime_dir=self.fixture / "ipc", runner=self.runner)
        adapter._has_device = lambda: True
        state = adapter.poll_once()
        self.assertFalse(state["control_available"])
        self.assertEqual(self.calls, [])

    def test_missing_device_never_queries_factory_or_socket(self):
        adapter = self.adapter(True)
        adapter._has_device = lambda: False
        state = adapter.poll_once()
        self.assertFalse(state["control_available"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sockets.sent, [])


if __name__ == "__main__":
    unittest.main()
