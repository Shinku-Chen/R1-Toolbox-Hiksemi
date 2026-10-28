"""Short-lease adapter for the exact statically verified R1 vendor fan ABI.

Construction/capabilities are passive. Only an explicit start launches our own
independent watchdog; the watchdog owns all process/RPC/ioctl mutations. No RPM
or previous PWM value is invented. Never invoke this adapter on a developer Mac.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import struct
import subprocess
import sys
import threading
import time

FIRMWARE_SHA256 = "745cd85d96fdcd1655455c652850abc49290dba5326b7f93ef2c5710e0de1719"
DRIVER_NOTE = bytes.fromhex("040000001400000003000000474e5500c909da338be2c4700bb00b233cb189e7a32655b7")
LEASE = struct.Struct("<8sQIIII")
MAGIC = b"R1FAN01\0"
CHANNEL_BITS = {"cpu": 1, "system": 2}
LEASE_NS = 5_000_000_000
PREFLIGHT_REASONS = frozenset((
    "独立风扇接管尚未启用", "独立风扇控制仅在 Linux 宿主 root 服务中启用",
    "独立恢复需要宿主 systemd-run", "已加载的风扇驱动身份已经改变或未验证",
    "原厂程序路径不在设备根目录内", "原厂温控程序格式不受支持", "原厂温控程序不可读",
    "原厂温控程序版本尚未验证", "原厂温控程序身份已经改变", "风扇字符设备身份无法确认",
    "缺少已编译的 Linux amd64 风扇保护模块", "进程数量超出有界身份检测范围",
    "发现来源或版本无法确认的同名温控执行者", "原厂温控进程身份无法读取",
    "必须存在唯一身份已验证的原厂温控进程", "原厂系统风扇策略配置无效或不可读",
    "原厂系统风扇同步接口不可用",
))


def read_bytes(path, limit):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        value = os.read(descriptor, limit + 1)
        return value if len(value) <= limit else None
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("风扇运行状态路径不能是符号链接")
    temporary = path.parent / (".write-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
        temporary.replace(path)
    finally:
        # Keep failed temporary artifacts private; no recursive cleanup occurs.
        pass


def atomic_json(path, value):
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > 131072:
        raise ValueError("风扇恢复描述符超出安全大小，未执行交接")
    atomic_bytes(path, encoded)


def read_json(path, limit=131072):
    raw = read_bytes(path, limit)
    try:
        value = json.loads(raw) if raw is not None else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def process_identity(pid, proc_root="/proc"):
    """PID reuse is never sufficient authority to signal another process."""
    path = Path(proc_root) / str(pid)
    try:
        raw = read_bytes(path / "stat", 8192)
        fields = raw.decode().rsplit(")", 1)[1].split() if raw else []
        if len(fields) < 20 or fields[0] in ("Z", "X"):
            return None
        return {"pid": int(pid), "starttime": int(fields[19]), "exe": os.readlink(path / "exe")}
    except (OSError, ValueError, UnicodeError, IndexError):
        return None


def valid_channels(channels):
    if not isinstance(channels, (set, frozenset)) or not channels or channels - CHANNEL_BITS.keys():
        raise ValueError("接管通道必须为 CPU、系统或两者")
    return sum(CHANNEL_BITS[name] for name in channels)


def encode_lease(channels, targets, sequence, now_ns):
    mask = valid_channels(channels)
    if (not isinstance(targets, dict) or set(targets) != set(channels)
            or any(type(value) is not int or not 128 <= value <= 255 for value in targets.values())
            or type(sequence) is not int or not 1 <= sequence <= 0xffffffff):
        raise ValueError("风扇目标必须完整提供接管通道且为 128–255 的整数")
    return LEASE.pack(MAGIC, now_ns + LEASE_NS, mask, targets.get("cpu", 0), targets.get("system", 0), sequence)


def decode_lease(raw, now_ns, expected_mask):
    if raw is None or len(raw) != LEASE.size:
        raise ValueError("风扇租约缺失或长度错误")
    magic, until, mask, cpu, system, sequence = LEASE.unpack(raw)
    if magic != MAGIC or mask != expected_mask or not sequence or not now_ns < until <= now_ns + LEASE_NS:
        raise ValueError("风扇租约已过期或身份错误")
    values = {name: value for name, value in (("cpu", cpu), ("system", system)) if mask & CHANNEL_BITS[name]}
    if any(not 128 <= value <= 255 for value in values.values()):
        raise ValueError("风扇租约目标超出安全范围")
    return {"until_ns": until, "sequence": sequence, "targets": values}


class SystemdSupervisor:
    """Handle the real supervisor identity, not systemd-run's short-lived PID."""
    def __init__(self, launcher, folder, backend, clock=None, sleep=None):
        self.launcher, self.folder, self.backend = launcher, Path(folder), backend
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.born, self.identity = self.clock(), None

    def poll(self):
        launched = self.launcher.poll()
        if launched not in (None, 0):
            return launched
        if self.identity is None:
            status = read_json(self.folder / "status.json") or {}
            candidate = status.get("watchdog")
            if isinstance(candidate, dict) and self.backend.alive(candidate):
                self.identity = candidate
            elif launched == 0 and self.clock() - self.born > 3:
                return 1
            else:
                return None
        return None if self.backend.alive(self.identity) else 0

    def wait(self, timeout):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            result = self.poll()
            if result is not None:
                return result
            self.sleep(0.05)
        raise subprocess.TimeoutExpired("r1-fan-watchdog", timeout)

    def terminate_verified(self):
        # Never signal the systemd-run launcher or an entire cgroup. Its vendor
        # children must remain available for identity-aware restoration.
        if self.identity is None:
            self.poll()
        if self.identity is None or not self.backend.alive(self.identity):
            raise RuntimeError("风扇监督身份已改变，未向未知进程发送信号")
        self.backend.stop_process(self.identity)
        if self.backend.alive(self.identity):
            raise RuntimeError("独立风扇监督仍未停止，恢复尚未确认")


class FanNativeAdapter:
    def __init__(self, state_dir, runtime_dir, *, root="/", histor=None, allow_writes=False,
                 guard_path=None, backend=None, popen=None, clock=None, sleep=None):
        self.state_dir, self.runtime_dir = Path(state_dir), Path(runtime_dir)
        self.root = Path(root).resolve()
        self.guard_path = Path(guard_path) if guard_path else Path(__file__).with_name("fan_guard.so")
        self.histor, self.allow_writes = histor, allow_writes is True
        self.backend = backend
        self.popen = popen or subprocess.Popen
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.RLock()
        self.channels, self.targets = set(), {}
        self.sequence, self.child, self.folder = 0, None, None
        self.last_error = None

    def _backend(self):
        if self.backend is None:
            from fan_watchdog import SystemBackend
            self.backend = SystemBackend(root=self.root, guard_path=self.guard_path)
        return self.backend

    def capabilities(self):
        with self.lock:
            try:
                info = self._backend().probe()
                if not self.allow_writes:
                    raise ValueError("独立风扇接管尚未启用")
                return {"available": True, "reason": None, "channels": ["cpu", "system"],
                        "active_channels": sorted(self.channels), "owner": "custom" if self.channels else "vendor",
                        "driver_build_id": DRIVER_NOTE[16:].hex(), "firmware_sha256": FIRMWARE_SHA256,
                        "rpm_available": False, **info}
            except (OSError, ValueError, RuntimeError) as error:
                # Only these static checks may reach either UI. OS errors and
                # arbitrary backend exception text may include private paths.
                message = str(error)
                reason = message if type(error) in (ValueError, RuntimeError) and message in PREFLIGHT_REASONS else (
                    "当前固件、驱动、设备或守护程序身份尚未验证；独立控制已隐藏")
                return {"available": False, "reason": reason,
                        "channels": [], "active_channels": sorted(self.channels), "rpm_available": False}

    def _status(self):
        return read_json(self.folder / "status.json") if self.folder else None

    def snapshot(self):
        with self.lock:
            status = self._status() or {}
            return {"active_channels": sorted(self.channels) if status.get("phase") == "active" else [],
                    "targets": dict(self.targets), "owner": "custom" if status.get("phase") == "active" else "vendor",
                    "phase": status.get("phase", "idle"), "reason": status.get("reason") or self.last_error,
                    "rpm_available": False, "restore_ok": status.get("restore_ok"),
                    "lease_seconds": 5, "applied_sequence": status.get("applied_sequence")}

    def _renew(self, targets):
        self.sequence = self.sequence % 0xffffffff + 1
        value = encode_lease(self.channels, targets, self.sequence, int(self.clock() * 1_000_000_000))
        atomic_bytes(self.folder / "lease.bin", value)

    def start(self, channels):
        valid_channels(channels)
        with self.lock:
            if self.channels:
                if self.channels == channels and self.snapshot()["phase"] == "active":
                    return
                self.release()
            if not self.capabilities()["available"]:
                raise ValueError("独立风扇控制预检未通过")
            self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.runtime_dir.is_symlink():
                raise ValueError("风扇 RAM 目录不能是符号链接")
            self.folder = self.runtime_dir / ("fan-" + secrets.token_hex(8))
            self.folder.mkdir(mode=0o700)
            self.channels, self.targets = set(channels), {name: 255 for name in channels}
            self._renew(self.targets)
            owner = self._backend().owner_identity()
            if owner is None:
                self.channels.clear()
                raise ValueError("无法确认控制服务身份")
            descriptor = {"version": 1, "root": str(self.root), "owner": owner, "channels": sorted(channels),
                          "guard_path": str(self.guard_path.resolve()), "folder": str(self.folder.resolve())}
            atomic_json(self.folder / "descriptor.json", descriptor)
            command = [sys.executable, "-B", str(Path(__file__).with_name("fan_watchdog.py")),
                       "--descriptor", str(self.folder / "descriptor.json")]
            try:
                if hasattr(self._backend(), "launch_watchdog"):
                    self.child = self._backend().launch_watchdog(command, self.folder, self.popen)
                else:
                    self.child = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                deadline = self.clock() + 15
                while self.clock() < deadline:
                    if self.child.poll() is not None:
                        status = self._status() or {}
                        reason = str(status.get("reason") or "监督进程提前退出")[:160]
                        raise RuntimeError("独立风扇监督进程未能就绪：" + reason)
                    status = self._status() or {}
                    if status.get("phase") == "active":
                        return
                    if status.get("phase") in ("released", "failed", "recovery_failed"):
                        raise RuntimeError("独立风扇交接未完成，已请求恢复原厂控制")
                    self._renew(self.targets)
                    self.sleep(0.1)
                raise RuntimeError("独立风扇交接超时")
            except (OSError, ValueError, RuntimeError):
                self.last_error = "独立风扇接管未完成"
                self.release()
                raise

    def write(self, targets):
        with self.lock:
            if not self.channels or not self.folder:
                raise ValueError("尚未接管风扇通道")
            # Full validation occurs before the lease can be replaced.
            encode_lease(self.channels, targets, 1, 0)
            try:
                self._backend().verify_live()
            except (OSError, ValueError, RuntimeError):
                self.last_error = "风扇固件、设备或驱动身份改变，已请求恢复原厂控制"
                self.release()
                raise RuntimeError(self.last_error) from None
            if self.child is None or self.child.poll() is not None:
                self.last_error = "风扇监督进程已停止，正在恢复原厂控制"
                self.release()
                raise RuntimeError(self.last_error)
            self._renew(targets)
            wanted = self.sequence
            deadline = self.clock() + 3
            while self.clock() < deadline:
                status = self._status() or {}
                if status.get("phase") != "active":
                    self.last_error = status.get("reason") or "独立风扇已释放，请检查原厂策略竞争"
                    self.release()
                    raise RuntimeError(self.last_error)
                if status.get("applied_sequence") == wanted:
                    self.targets = dict(targets)
                    return
                if self.child.poll() is not None:
                    break
                self.sleep(0.05)
            self.last_error = "风扇应用结果未确认，已请求恢复原厂控制"
            self.release()
            raise RuntimeError(self.last_error)

    def release(self):
        with self.lock:
            if self.folder is None:
                self.channels.clear()
                return
            atomic_json(self.folder / "release.json", {"release": True})
            if self.child is not None:
                try:
                    self.child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    if not hasattr(self.child, "terminate_verified"):
                        raise RuntimeError("风扇监督卡住且身份无法确认，未终止未知进程") from None
                    self.child.terminate_verified()
                    self.child.wait(timeout=4)
            status = self._status() or {}
            if status.get("phase") != "released":
                # If our own watchdog was killed, the still-live controller
                # launches one bounded recovery supervisor; never signals a
                # recycled PID or leaves a low target indefinitely in place.
                descriptor = read_json(self.folder / "descriptor.json")
                if descriptor and descriptor.get("original"):
                    command = [sys.executable, "-B", str(Path(__file__).with_name("fan_watchdog.py")),
                               "--descriptor", str(self.folder / "descriptor.json"), "--recover"]
                    if hasattr(self._backend(), "launch_watchdog"):
                        recovery = self._backend().launch_watchdog(command, self.folder, self.popen)
                    else:
                        recovery = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                    self.child = recovery
                    try:
                        recovery.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        if hasattr(recovery, "terminate_verified"):
                            recovery.terminate_verified()
                        raise RuntimeError("独立风扇恢复监督仍未完成") from None
                    status = self._status() or {}
            self.channels.clear()
            self.targets.clear()
            if status.get("restore_ok") is False or status.get("phase") not in ("released", "recovery_failed"):
                self.last_error = "原厂风扇恢复未能全部确认"
                raise RuntimeError(self.last_error)
