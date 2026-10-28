"""Bounded adapter for existing Hiksemi R1 control services.

Protocol evidence comes from emhistor V6.3.3 build 260907. No bundled factory
program or firmware binary is redistributed. Only the installed product_test -o
query is executed; no shell, raw port I/O, ioctl write, or kernel loading occurs.
Official mode changes affect the system-fan policy and persist in the vendor's
configuration. The separate CPU-fan thread continues vendor automatic control.
"""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import subprocess
import threading
import time

MODES = {"low": 2, "medium": 3, "high": 4, "auto": 5}
_CODES = {value: key for key, value in MODES.items()}
_MAX_RESPONSE = 131072
_MAX_OUTPUT = 4096
_GNU_BUILD_NOTE_HEADER = bytes.fromhex("040000001400000003000000474e5500")
# The initrd and rootfs ship different modules despite sharing vermagic.
# Only the exact loaded initrd note below has statically verified 0x5798/0x5799
# implementations. Old 0x5793/0x5794 RPM reads remain unsupported independently.
_SUPPORTED_DRIVER_NOTE = _GNU_BUILD_NOTE_HEADER + bytes.fromhex("c909da338be2c4700bb00b233cb189e7a32655b7")
_UNSUPPORTED_DRIVER_NOTE = _GNU_BUILD_NOTE_HEADER + bytes.fromhex("7a0ac48f741ff349b12b56cff270d4e1f9b1bc07")


def _read_file(path, limit):
    """Read only a regular nofollow file and never return credentials/config dumps."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, limit + 1)
        return raw if len(raw) <= limit else None
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parse_rpm(text):
    """Parse precisely the installed factory query's cpu[N]-sys[N] response."""
    if not isinstance(text, str) or len(text) > _MAX_OUTPUT:
        raise ValueError("原厂转速响应格式不正确")
    # Factory logging can precede this one final measurement. Ambiguous or extra
    # payload after the measurement is rejected rather than guessed.
    matches = list(re.finditer(r"(?m)^cpu\[(-?[0-9]{1,6})\]-sys\[(-?[0-9]{1,6})\]\s*$", text))
    if len(matches) != 1 or text[matches[0].end():].strip():
        raise ValueError("原厂转速响应格式不正确")
    values = [int(value) for value in matches[0].groups()]
    if any(value < -1 or value > 100000 for value in values):
        raise ValueError("原厂转速响应超出范围")
    return tuple(value if value >= 0 else None for value in values)


class HistorAdapter:
    """Constructor/snapshot are inert; workers cache read-only telemetry.

    allow_writes defaults false. Even when enabled, controls additionally require
    the installed factory program, /dev/hikio, a live official socket, a pre-existing
    valid fan configuration, an exact supported loaded-driver identity, and an
    explicit confirmed user action before any mode RPC. Missing support is hidden
    by the caller; factory mode 1 (stop fan) is never exposed.
    """
    def __init__(self, root="/", runtime_dir="/run/r1-toolbox", runner=None,
                 clock=None, socket_factory=None, allow_writes=False, poll_interval=5,
                 hardware_supported=None):
        self.root = Path(root).resolve()
        self.runtime_dir = Path(runtime_dir)
        self.runner = runner or self._run
        self.clock = clock or time.monotonic
        self.socket_factory = socket_factory or socket.socket
        self.allow_writes = allow_writes is True
        if hardware_supported is not None and type(hardware_supported) is not bool:
            raise ValueError("硬件支持注入必须是布尔值或 None")
        # Explicit bool is a fixture injection only; production supplies None
        # and passively identifies the currently loaded module's complete note.
        self._hardware_override = hardware_supported
        self.hardware_supported = False
        self.poll_interval = max(1.0, float(poll_interval))
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self._mode_generation = 0
        self._mode_mutating = False
        self.stopped = threading.Event()
        self.thread = None
        self._control_disabled = False
        self._rpm_next_poll = 0
        self._rpm_values = (None, None)
        self._rpm_error = None
        self._state = {"available": False, "rpm_available": False, "control_available": False,
                       "policy_available": False, "policy_only": False,
                       "driver_build_id": None, "hardware_supported": False,
                       "hardware_reason": "等待原厂驱动身份检测",
                       "cpu_rpm": None, "system_rpm": None, "mode": None,
                       "mode_code": None, "grade": None, "sampled_at": None,
                       "reason": "等待原厂接口检测", "telemetry_reason": None,
                       "source": "histor", "scope": "system_fan", "cpu_policy": "vendor_auto"}

    def _path(self, absolute):
        return self.root / absolute.lstrip("/")

    def _inside(self, path):
        try:
            resolved = Path(path).resolve()
            return resolved == self.root or self.root in resolved.parents
        except (OSError, RuntimeError, ValueError):
            return False

    def _driver_support(self):
        path = self._path("/sys/module/hikio/notes/.note.gnu.build-id")
        raw = _read_file(path, 36) if self._inside(path) else None
        valid_note = raw is not None and len(raw) == 36 and raw[:16] == _GNU_BUILD_NOTE_HEADER
        build_id = raw[16:].hex() if valid_note else None
        if self._hardware_override is not None:
            return self._hardware_override, build_id, "测试注入的驱动支持状态"
        if raw == _SUPPORTED_DRIVER_NOTE:
            return True, build_id, "已确认原厂驱动支持调速；实际效果需设备验证"
        if raw == _UNSUPPORTED_DRIVER_NOTE:
            return False, build_id, "当前加载的原厂驱动未实现风扇调速接口"
        if valid_note:
            return False, build_id, "当前原厂驱动版本尚未验证，已隐藏控制"
        return False, None, "无法确认已加载的原厂驱动，已隐藏控制"

    def _refresh_driver(self):
        supported, build_id, reason = self._driver_support()
        self.hardware_supported = supported
        with self.lock:
            self._state.update(driver_build_id=build_id, hardware_supported=supported,
                               hardware_reason=reason)
            if not supported:
                self._state["control_available"] = False
                self._state["policy_only"] = self._state["policy_available"]
                self._state["reason"] = reason
        return supported

    @staticmethod
    def _run(argv, timeout=5):
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout, check=False)

    def _config_mode(self):
        # json_conf_load("fan") reads precisely this JSON document and key.
        path = self._path("/tmp/nand_config/config.txt")
        raw = _read_file(path, 1024 * 1024) if self._inside(path) else None
        try:
            obj = json.loads(raw.rstrip(b"\0")) if raw is not None else None
            fan = obj.get("fan") if isinstance(obj, dict) else None
            mode = fan.get("fan_mode") if isinstance(fan, dict) else None
            return mode if type(mode) is int and mode in _CODES else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def _has_socket(self):
        try:
            path = self._path("/var/tmp/emhistor")
            return self._inside(path) and stat.S_ISSOCK(path.stat().st_mode)
        except OSError:
            return False

    def _has_device(self):
        try:
            path = self._path("/dev/hikio")
            return self._inside(path) and stat.S_ISCHR(path.stat().st_mode)
        except OSError:
            return False

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self._state)
            fresh = result["sampled_at"] is not None and self.clock() - result["sampled_at"] <= max(15, 3 * self.poll_interval)
            if not fresh:
                result["control_available"] = False
                if result["sampled_at"] is not None:
                    result["cpu_rpm"] = result["system_rpm"] = None
                    result["available"] = result["rpm_available"] = False
                    result["stale"] = True
                    result["reason"] = "原厂接口数据已过期"
            if self.stopped.is_set():
                result["control_available"] = False
            return result

    def verified_mode(self):
        """Read the current native selection through the existing control gates.

        This is passive evidence for avoiding an unnecessary mode RPC, never a
        vendor GET or RPM query. RPM polling and this fixed-path read are
        independent. A generation change across the read, or an actual mode
        RPC in progress, leaves the mode unconfirmed rather than trusting a
        partial update. Neither path waits behind a slow factory command.
        """
        with self.lock:
            if self._mode_mutating:
                return None
            generation = self._mode_generation
        try:
            command = self._path("/histor/bin/product_test")
            if (self.stopped.is_set() or not self.allow_writes or self._control_disabled
                    or not self._refresh_driver() or not self._inside(command) or not command.is_file()
                    or not self._has_device() or not self._has_socket()):
                return None
            mode = self._config_mode()
            # A replacement during the read must not turn stale driver proof
            # into permission to omit the normal verified set_mode path.
            if not self._refresh_driver() or self.stopped.is_set() or self._control_disabled:
                return None
            selected = _CODES.get(mode)
            with self.lock:
                if (self._mode_mutating or self._mode_generation != generation
                        or self.stopped.is_set() or self._control_disabled):
                    return None
                if selected is not None:
                    # Keep the policy label current without refreshing RPM or
                    # granting a stale cached control_available capability.
                    self._state.update(mode=selected, mode_code=mode)
                return selected
        except (OSError, ValueError, RuntimeError):
            return None

    @contextmanager
    def _mode_change(self):
        """Fence passive readers around a real set, separately from RPM polls."""
        with self.operation_lock:
            with self.lock:
                self._mode_generation += 1
                self._mode_mutating = True
            try:
                yield
            finally:
                with self.lock:
                    self._mode_generation += 1
                    self._mode_mutating = False

    def poll_once(self):
        with self.operation_lock:
            if self.stopped.is_set():
                return self.snapshot()
            self._refresh_driver()
            command = self._path("/histor/bin/product_test")
            if self.clock() >= self._rpm_next_poll:
                cpu = system = None
                error = None
                try:
                    if not self._inside(command) or not command.is_file() or not self._has_device():
                        raise ValueError("当前固件未提供可用的原厂转速查询")
                    result = self.runner([str(command), "-o"], timeout=5)
                    if result.returncode != 0:
                        raise ValueError("原厂转速查询失败")
                    cpu, system = parse_rpm(result.stdout)
                    if cpu is None and system is None:
                        raise ValueError("原厂驱动没有返回有效风扇转速")
                except (OSError, ValueError, subprocess.TimeoutExpired, TimeoutError):
                    error = "原厂转速接口未返回有效数据"
                self._rpm_values, self._rpm_error = (cpu, system), error
                self._rpm_next_poll = self.clock() + (60 if error else self.poll_interval)
            cpu, system = self._rpm_values
            error = self._rpm_error
            mode = self._config_mode()
            grade_path = self._path("/tmp/fan")
            grade_raw = _read_file(grade_path, 4) if self._inside(grade_path) else None
            grade = struct.unpack("<i", grade_raw)[0] if grade_raw is not None and len(grade_raw) == 4 else None
            grade = grade if grade in (1, 2, 3, 4) else None
            telemetry = cpu is not None or system is not None
            # Reading this existing policy does not invoke the vendor getter:
            # that getter signals temp_control and can create missing defaults.
            # Only a confirmed set_mode action is permitted to invoke it.
            supported = bool(self.hardware_supported and self.allow_writes and not self._control_disabled and self._has_device() and self._inside(command) and command.is_file()
                             and mode is not None and self._has_socket())
            reason = None if supported else "原厂风扇控制接口不可用"
            if not self.hardware_supported:
                reason = self._state["hardware_reason"]
            elif not self.allow_writes:
                reason = "原厂风扇控制尚未启用"
            elif self._control_disabled:
                reason = "原厂接口校验失败，当前服务已停用控制"
            elif mode is None:
                reason = "原厂风扇配置不存在或无法确认"
            with self.lock:
                self._state.update(available=telemetry or mode is not None,
                                   rpm_available=telemetry, stale=False,
                                   policy_available=mode is not None,
                                   policy_only=mode is not None and not supported,
                                   control_available=supported,
                                   cpu_rpm=cpu, system_rpm=system, mode=_CODES.get(mode),
                                   mode_code=mode, grade=grade, sampled_at=self.clock(),
                                   reason=reason, telemetry_reason=error)
            return self.snapshot()

    def _rpc(self, command, value):
        if command not in ("get_fan_status", "set_fan_status"):
            raise ValueError("不支持的原厂命令")
        if (command == "get_fan_status" and value != 0) or (command == "set_fan_status" and value not in _CODES):
            raise ValueError("不支持的原厂风扇模式")
        # Both the kernel socket and named client must stay in bounded trusted
        # directories. This is ephemeral IPC, never a persistent NAS config file.
        target = self._path("/var/tmp/emhistor")
        if not self._inside(target):
            raise ValueError("原厂接口路径超出设备根目录")
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.runtime_dir.resolve()
        if not directory.is_dir():
            raise ValueError("原厂接口暂存目录无效")
        endpoint = directory / ("histor-" + secrets.token_hex(8) + ".sock")
        if len(os.fsencode(endpoint)) >= 104:
            raise ValueError("原厂接口暂存路径过长")
        identity = None
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic() + 2
            client.settimeout(2)
            client.bind(str(endpoint))
            os.chmod(endpoint, 0o700)
            info = endpoint.stat()
            identity = (info.st_dev, info.st_ino)
            client.connect(str(self._path("/var/tmp/emhistor")))
            client.sendall(("emhistor;" + command + ";" + str(value)).encode("ascii") + b"\0")
            result = bytearray()
            while len(result) < _MAX_RESPONSE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("原厂接口响应超时")
                client.settimeout(remaining)
                part = client.recv(min(8192, _MAX_RESPONSE - len(result)))
                if not part:
                    break
                result.extend(part)
            if not result or len(result) >= _MAX_RESPONSE and b"\0" not in result:
                raise ValueError("原厂接口响应为空或过长")
            head, separator, tail = bytes(result).partition(b"\0")
            if separator and any(tail):
                raise ValueError("原厂接口响应包含额外数据")
            if not separator and len(result) >= _MAX_RESPONSE:
                raise ValueError("原厂接口响应过长")
            return head.decode("ascii", "strict")
        finally:
            client.close()
            # The official accept() normally unlinks this private socket. On a
            # failed connection remove only our unchanged ephemeral IPC inode.
            try:
                info = endpoint.lstat()
                if identity == (info.st_dev, info.st_ino) and stat.S_ISSOCK(info.st_mode):
                    endpoint.unlink()
            except OSError:
                pass

    def _get_mode(self):
        reply = self._rpc("get_fan_status", 0)
        if reply not in ("2", "3", "4", "5"):
            raise ValueError("原厂风扇模式响应无法确认")
        return int(reply)

    def set_mode(self, mode, confirmed=False):
        if confirmed is not True:
            raise ValueError("更改原厂风扇设置需要确认")
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("仅支持原厂低、中、高、自动四种模式")
        with self._mode_change():
            if self.stopped.is_set():
                raise ValueError("原厂风扇服务已停止")
            if not self._refresh_driver() or not self.allow_writes or not self.snapshot()["control_available"]:
                raise ValueError("原厂风扇控制接口未通过验证")
            if not self._has_device() or not self._has_socket() or self._config_mode() is None:
                raise ValueError("原厂风扇接口已不可用")
            try:
                self._get_mode()
                # A module replacement after the initial policy read must not
                # turn a known command into a write against an unknown driver.
                if not self._refresh_driver():
                    raise ValueError("原厂风扇驱动已发生变化")
                if self._rpc("set_fan_status", MODES[mode]) != "OK!":
                    raise ValueError("原厂风扇设置未返回成功")
                actual = self._get_mode()
                if not self._refresh_driver() or actual != MODES[mode]:
                    raise ValueError("原厂风扇设置结果未能确认")
            except (OSError, ValueError, TimeoutError) as error:
                self._control_disabled = True
                with self.lock:
                    self._state["control_available"] = False
                    self._state["reason"] = "原厂风扇设置结果未能确认，请检查设备"
                raise ValueError("原厂风扇设置结果未能确认，请检查设备") from error
            with self.lock:
                self._state.update(mode=mode, mode_code=actual, reason=None)
            return self.snapshot()

    def _loop(self):
        while not self.stopped.is_set():
            self.poll_once()
            self.stopped.wait(self.poll_interval)

    def start(self):
        with self.lock:
            if not self.stopped.is_set() and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._loop, name="r1-histor", daemon=True)
                self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=8)
        # Official mode belongs to the original daemon and remains selected.
        # No guessed restore or hardware writes occur during service cleanup.
