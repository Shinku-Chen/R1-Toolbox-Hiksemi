"""Independent fail-high/recovery supervisor for the opt-in native fan owner.

Only our own Python supervisor and compiled shim are launched. Installed vendor
temp_control is restarted only after exact identities and a recoverable RAM
descriptor have been saved. Tests inject a backend; never run this on a NAS as
part of tests, or execute a firmware binary on a development machine.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import secrets
import stat
import subprocess
import sys
import time

from fan_native import (CHANNEL_BITS, DRIVER_NOTE, FIRMWARE_SHA256, atomic_json,
                        decode_lease, process_identity, read_bytes, read_json, valid_channels,
                        SystemdSupervisor)


class SystemBackend:
    def __init__(self, root="/", guard_path=None, *, clock=None, sleep=None):
        self.root = Path(root).resolve()
        self.proc = self.root / "proc"
        self.program_link = self.root / "histor/bin/temp_control"
        self.program = self.program_link.resolve()
        self.guard_path = Path(guard_path) if guard_path else Path(__file__).with_name("fan_guard.so")
        self._signature = self._device = None
        self._last_ready = {}
        self.children = []
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep

    def _inside(self, path):
        path = Path(path).resolve()
        return path == self.root or self.root in path.parents

    def _firmware_signature(self):
        if not self._inside(self.program) or self.program_link.resolve() != self.program:
            raise ValueError("原厂程序路径不在设备根目录内")
        try:
            info = self.program.stat()
        except OSError:
            raise ValueError("原厂温控程序不可读") from None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise ValueError("原厂温控程序格式不受支持")
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._signature is None:
            raw = read_bytes(self.program, 4 * 1024 * 1024)
            if raw is None or hashlib.sha256(raw).hexdigest() != FIRMWARE_SHA256:
                raise ValueError("原厂温控程序版本尚未验证")
            self._signature = signature
        elif signature != self._signature:
            raise ValueError("原厂温控程序身份已经改变")
        return signature

    def verify_live(self):
        note_path = self.root / "sys/module/hikio/notes/.note.gnu.build-id"
        if not self._inside(note_path) or read_bytes(note_path, 36) != DRIVER_NOTE:
            raise ValueError("已加载的风扇驱动身份已经改变或未验证")
        self._firmware_signature()
        device = self.root / "dev/hikio"
        try:
            info = device.lstat()
        except OSError:
            raise ValueError("风扇字符设备身份无法确认") from None
        current = (info.st_dev, info.st_ino, info.st_rdev)
        if not stat.S_ISCHR(info.st_mode) or self._device is not None and current != self._device:
            raise ValueError("风扇字符设备身份无法确认")
        self._device = current

    def _vendors(self):
        result = []
        with os.scandir(self.proc) as entries:
            for index, entry in enumerate(entries):
                if index >= 4096:
                    raise ValueError("进程数量超出有界身份检测范围")
                if not entry.name.isascii() or not entry.name.isdecimal():
                    continue
                identity = process_identity(int(entry.name), self.proc)
                if identity is not None and identity["exe"] == str(self.program):
                    result.append(identity)
                elif identity is not None and Path(identity["exe"].removesuffix(" (deleted)")).name == "temp_control":
                    raise ValueError("发现来源或版本无法确认的同名温控执行者")
        return result

    def probe(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ValueError("独立风扇控制仅在 Linux 宿主 root 服务中启用")
        if not shutil.which("systemd-run"):
            raise ValueError("独立恢复需要宿主 systemd-run")
        self.verify_live()
        guard = self.guard_path.resolve()
        try:
            info = guard.stat()
        except OSError:
            raise ValueError("缺少已编译的 Linux amd64 风扇保护模块") from None
        header = read_bytes(guard, 2 * 1024 * 1024)
        if (self.guard_path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or header is None or len(header) < 64 or header[:6] != b"\x7fELF\x02\x01"
                or header[16:20] != b"\x03\x00\x3e\x00"):
            raise ValueError("缺少已编译的 Linux amd64 风扇保护模块")
        try:
            vendors = self._vendors()
        except OSError:
            raise ValueError("原厂温控进程身份无法读取") from None
        if len(vendors) != 1:
            raise ValueError("必须存在唯一身份已验证的原厂温控进程")
        endpoint = self.root / "var/tmp/emhistor"
        if self.mode() is None:
            raise ValueError("原厂系统风扇策略配置无效或不可读")
        try:
            socket_available = stat.S_ISSOCK(endpoint.stat().st_mode)
        except OSError:
            socket_available = False
        if not socket_available:
            raise ValueError("原厂系统风扇同步接口不可用")
        return {"guard_available": True}

    def owner_identity(self):
        return process_identity(os.getpid(), self.proc)

    def launch_watchdog(self, command, folder, popen):
        folder = Path(folder).resolve()
        if self.root != Path("/") or Path("/run/r1-toolbox") not in folder.parents:
            raise ValueError("风扇恢复描述符必须位于宿主 RAM 目录内")
        unit = "r1-fan-watch-" + folder.name.removeprefix("fan-") + (
            "-recover-" + secrets.token_hex(4) if "--recover" in command else "")
        launcher = popen(["systemd-run", "--quiet", "--collect", "--service-type=exec", "--unit=" + unit,
                          "--property=KillMode=process", "--property=Restart=no", "--property=TimeoutStopSec=12s",
                          "--property=StandardOutput=null", "--property=StandardError=null", *command],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        return SystemdSupervisor(launcher, folder, self)

    def alive(self, identity):
        return identity is not None and process_identity(identity["pid"], self.proc) == identity

    def capture(self):
        self.probe()
        identity = self._vendors()[0]
        base = self.proc / str(identity["pid"])
        args, environment = read_bytes(base / "cmdline", 16384), read_bytes(base / "environ", 65536)
        if args is None or environment is None:
            raise ValueError("无法保存原厂温控启动信息")
        parts = args.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        argv = [os.fsdecode(item) for item in parts]
        env = {}
        for item in environment.split(b"\0"):
            if item:
                key, equal, value = item.partition(b"=")
                if not equal or not key or os.fsdecode(key) in env:
                    raise ValueError("原厂进程环境格式无法确认")
                env[os.fsdecode(key)] = os.fsdecode(value)
        if not argv or not argv[0] or env.get("LD_PRELOAD") or any(key.startswith("R1_FAN_") for key in env):
            raise ValueError("原厂温控存在未知注入，不能安全接管")
        cwd = os.readlink(base / "cwd")
        if not os.path.isabs(cwd) or not Path(cwd).is_dir() or not self.alive(identity):
            raise ValueError("原厂温控身份在交接前发生改变")
        return {"identity": identity, "argv": argv, "env": env, "cwd": cwd}

    def _histor(self, folder):
        from histor import HistorAdapter
        return HistorAdapter(root=self.root, runtime_dir=folder, allow_writes=True)

    def mode(self):
        # This vendor-owned JSON can end in NUL bytes. Only this known file
        # accepts that encoding; leases and recovery/status JSON remain strict.
        raw = read_bytes(self.root / "tmp/nand_config/config.txt", 1024 * 1024)
        try:
            value = json.loads(raw.rstrip(b"\0")) if raw is not None else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        fan = value.get("fan") if isinstance(value, dict) else None
        code = fan.get("fan_mode") if isinstance(fan, dict) else None
        return {2: "low", 3: "medium", 4: "high", 5: "auto"}.get(code) if type(code) is int else None

    def sync_mode(self, folder, *, expected=None, safe_auto=False, apply_manual=True):
        """Synchronize existing policy without writing another fan_mode value.

        Vendor GET signals its existing manual/auto flag. Manual PWM is known
        from the exact binary; auto is never guessed from a nonexistent readback.
        """
        self.verify_live()
        from histor import MODES
        adapter = self._histor(folder)
        before = self.mode()
        if before not in MODES or expected is not None and before != expected:
            raise RuntimeError("vendor_policy_changed")
        if not adapter._has_socket() or adapter._config_mode() is None:
            raise ValueError("原厂系统风扇策略接口不可用")
        vendors = self._vendors()
        if len(vendors) != 1:
            raise RuntimeError("必须存在唯一身份已验证的原厂温控进程")
        identity = vendors[0]
        kind = self._owner_kind(identity, Path(folder) / "lease.bin")
        if kind not in ("vendor", "ours"):
            raise RuntimeError("原厂温控信号就绪状态无法确认")
        guarded = kind == "ours"
        # get_fan_status sends USR1/USR2; exe/maps alone do not prove that
        # temp_control has installed its handlers. Never send GET while starting.
        self.assert_ready(identity, require_guard=guarded)
        actual = adapter._get_mode()
        self.assert_ready(identity, require_guard=guarded)
        self.wait_ready(identity, require_guard=guarded, deadline=self.clock() + 0.3)
        self.verify_live()
        after = self.mode()
        if after != before or actual != MODES[before]:
            raise ValueError("原厂系统策略读回不一致")
        if apply_manual and after in ("low", "medium", "high"):
            self._write({"system": {"low": 120, "medium": 191, "high": 255}[after]}, minimum=120)
        elif safe_auto:
            self.write({"system": 255})
        return after

    def write(self, targets):
        return self._write(targets, minimum=128)

    def _write(self, targets, minimum):
        self.verify_live()
        device = self.root / "dev/hikio"
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or (info.st_dev, info.st_ino, info.st_rdev) != self._device:
                raise ValueError("风扇设备在打开时发生改变")
            for name, value in targets.items():
                if name not in CHANNEL_BITS or type(value) is not int or not minimum <= value <= 255:
                    raise ValueError("风扇控制值超出安全范围")
                # Direct integer ABI. This is not a packed pointer or RPM value.
                result = fcntl.ioctl(fd, 0x5798 if name == "cpu" else 0x5799, value)
                if result != 0:
                    raise ValueError("原厂风扇写入未返回可确认状态")
        finally:
            os.close(fd)

    def stop_process(self, identity):
        if not self.alive(identity):
            return
        os.kill(identity["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 2
        while self.alive(identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.alive(identity):
            os.kill(identity["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 1
            while self.alive(identity) and time.monotonic() < deadline:
                time.sleep(0.05)
        if self.alive(identity):
            raise RuntimeError("原厂温控进程未能停止")

    def spawn(self, original, *, lease=None, mask=None):
        if lease is None:
            self._firmware_signature()  # Restoring the original owner is still
            # allowed if an external actor replaced the loaded driver.
        else:
            self.verify_live()
        env = dict(original["env"])
        if lease is not None:
            env.update(LD_PRELOAD=str(self.guard_path.resolve()), R1_FAN_LEASE=str(lease), R1_FAN_MASK=str(mask))
        child = subprocess.Popen(original["argv"], executable=str(self.program), cwd=original["cwd"], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
        self.children.append(child)
        deadline = self.clock() + 20
        expected = None
        last_error = None
        try:
            while self.clock() < deadline:
                identity = process_identity(child.pid, self.proc)
                if child.poll() is not None:
                    break
                if identity and identity["exe"] == str(self.program):
                    expected = identity
                    return self.wait_ready(identity, require_guard=lease is not None, deadline=deadline)
                self.sleep(0.05)
        except (OSError, ValueError, RuntimeError) as error:
            # Preserve only a readiness detail; identity/exit failures keep the
            # established generic spawn error and its safety semantics.
            if "信号处理器就绪未确认" in str(error) or "执行者竞争" in str(error):
                last_error = error
        if expected is not None and self.alive(expected):
            self.stop_process(expected)
        if last_error is not None:
            raise RuntimeError(str(last_error)) from None
        raise RuntimeError("原厂温控信号处理器就绪未确认")

    def assert_single(self, identity):
        vendors = self._vendors()
        if vendors != [identity]:
            raise RuntimeError("检测到原厂温控执行者竞争，已释放自定义控制")

    def _ready(self, identity, require_guard):
        if identity is None or identity.get("exe") != str(self.program) or not self.alive(identity):
            raise RuntimeError("原厂温控启动身份无法确认")
        self.assert_single(identity)
        base = self.proc / str(identity["pid"])
        raw = read_bytes(base / "status", 65536)
        fields = [line.split(b":", 1)[1].strip() for line in (raw or b"").splitlines()
                  if line.startswith(b"SigCgt:")]
        required = (1 << (signal.SIGUSR1 - 1)) | (1 << (signal.SIGUSR2 - 1))
        try:
            caught = (int(fields[0], 16) if len(fields) == 1 and 1 <= len(fields[0]) <= 16
                      and all(char in b"0123456789abcdefABCDEF" for char in fields[0]) else 0)
        except ValueError:
            caught = 0
        ready = caught & required == required
        if require_guard:
            maps = read_bytes(base / "maps", 1024 * 1024)
            paths = [line.split(None, 5)[-1] for line in (maps or b"").splitlines() if len(line.split(None, 5)) == 6]
            expected = os.fsencode(self.guard_path.resolve())
            guard_name = os.fsencode(self.guard_path.name)
            self._last_ready = {'sigcgt': hex(caught), 'required': hex(required),
                                'guard_expected': os.fsdecode(expected),
                                'guard_paths': [os.fsdecode(path) for path in paths if b'fan_guard.so' in path]}
            # A mapped file may show ``(deleted)`` after a RAM bundle refresh, or
            # resolve through a different absolute path.  The process identity
            # and the fixed guard filename still prove this is our shim.
            guard_ok = any(path == expected or path == expected + b" (deleted)"
                           or path.rsplit(b"/", 1)[-1].removesuffix(b" (deleted)") == guard_name
                           for path in paths)
            ready = ready and guard_ok
            self._last_ready['guard_ok'] = guard_ok
        else:
            self._last_ready = {'sigcgt': hex(caught), 'required': hex(required)}
        if not self.alive(identity):
            raise RuntimeError("原厂温控启动身份无法确认")
        return ready

    def assert_ready(self, identity, *, require_guard=False):
        """Immediate identity/uniqueness/USR-handler gate before a signaling GET."""
        if not self._ready(identity, require_guard):
            raise RuntimeError("原厂温控信号处理器就绪未确认：%s" % json.dumps(self._last_ready, ensure_ascii=True)[:300])
        return identity

    def wait_ready(self, identity, *, require_guard=False, deadline=None, stable_seconds=0.15):
        """Bounded startup gate, also used by harmless real-/proc Linux fixtures."""
        deadline = min(self.clock() + 20, deadline) if deadline is not None else self.clock() + 20
        stable_since = None
        while self.clock() < deadline:
            ready = self._ready(identity, require_guard)
            now = self.clock()
            if ready:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= stable_seconds:
                    return identity
            elif stable_since is not None:
                raise RuntimeError("原厂温控信号处理器就绪未确认：%s" % json.dumps(self._last_ready, ensure_ascii=True)[:300])
            self.sleep(min(0.025, max(0, deadline - now)))
        raise RuntimeError("原厂温控信号处理器就绪未确认：%s" % json.dumps(self._last_ready, ensure_ascii=True)[:300])

    def _owner_kind(self, identity, lease):
        """Recognize an orphan from this exact lease, not any injected daemon."""
        base = self.proc / str(identity["pid"])
        maps, env = read_bytes(base / "maps", 1024 * 1024), read_bytes(base / "environ", 65536)
        if maps is None or env is None or not self.alive(identity):
            return "unknown"
        paths = [line.split(None, 5)[-1] for line in maps.splitlines() if len(line.split(None, 5)) == 6]
        pairs = [item.partition(b"=") for item in env.split(b"\0") if item]
        values = {key: value for key, equal, value in pairs if equal}
        injected = os.fsencode(self.guard_path.resolve()) in paths
        if injected:
            return "ours" if lease is not None and values.get(b"R1_FAN_LEASE") == os.fsencode(lease) else "unknown"
        if values.get(b"LD_PRELOAD") or any(key.startswith(b"R1_FAN_") for key in values):
            return "unknown"
        return "vendor"

    def restore_daemon(self, original, guarded, *, lease=None):
        self._firmware_signature()
        if guarded is not None and self.alive(guarded):
            kind = self._owner_kind(guarded, lease)
            if kind == "ours":
                self.stop_process(guarded)
            elif kind != "vendor":
                raise RuntimeError("恢复时无法确认温控注入来源")
        vendors = self._vendors()
        if len(vendors) == 1 and (guarded is None or vendors[0] != guarded):
            kind = self._owner_kind(vendors[0], lease)
            if kind == "vendor":
                return self.wait_ready(vendors[0])
            if kind == "ours":
                self.stop_process(vendors[0])
                vendors = self._vendors()
                if len(vendors) == 1 and self._owner_kind(vendors[0], lease) == "vendor":
                    return self.wait_ready(vendors[0])
            else:
                raise RuntimeError("恢复时发现未知温控注入，未启动另一个副本")
        elif len(vendors) == 1 and guarded is not None and vendors[0] == guarded:
            if self._owner_kind(guarded, lease) == "vendor":
                return self.wait_ready(guarded)
        if vendors:
            raise RuntimeError("原厂温控存在多个执行者，不能再启动副本")
        return self.spawn(original)


class FanWatchdog:
    """The small deterministic ownership state machine; tests use fake backend."""
    def __init__(self, folder, backend, *, clock_ns=None):
        self.folder, self.backend = Path(folder), backend
        self.clock_ns = clock_ns or time.monotonic_ns
        self.descriptor = read_json(self.folder / "descriptor.json")
        if not self.descriptor or self.descriptor.get("version") != 1:
            raise ValueError("风扇恢复描述符格式错误")
        self.channels = set(self.descriptor.get("channels", []))
        self.mask = valid_channels(self.channels)
        self.original = self.descriptor.get("original")
        self.guarded = self.descriptor.get("guarded")
        self.old_mode = self.descriptor.get("old_mode")
        self.applied, self.sequence, self.phase = {}, None, "armed"
        self.last_write_ns = None
        self.stop_requested = False
        self._restored = False

    def status(self, phase, reason=None, **values):
        self.phase = phase
        atomic_json(self.folder / "status.json", {"phase": phase, "reason": reason,
                    "applied_sequence": self.sequence, "watchdog": self.backend.owner_identity(), **values})

    def save(self):
        self.descriptor.update(original=self.original, guarded=self.guarded, old_mode=self.old_mode)
        atomic_json(self.folder / "descriptor.json", self.descriptor)

    def lease(self):
        return decode_lease(read_bytes(self.folder / "lease.bin", 32), self.clock_ns(), self.mask)

    def checkpoint(self):
        if self.stop_requested or read_json(self.folder / "release.json") is not None:
            raise RuntimeError("release_requested")
        if not self.backend.alive(self.descriptor.get("owner")):
            raise RuntimeError("owner_stopped")
        return self.lease()

    def begin(self):
        self.status("armed")  # Independent supervisor is running before mutation.
        self.checkpoint()
        self.original = self.backend.capture()
        self.old_mode = self.backend.mode()
        if self.old_mode not in ("low", "medium", "high", "auto"):
            raise ValueError("无法记录原厂系统风扇策略，未接管")
        self.save()  # Includes complete original argv/env, mode and identity at 0600.
        self.checkpoint()
        self.descriptor["daemon_changed"] = True
        self.save()  # Recovery remains possible if killed between TERM/spawn.
        self.backend.stop_process(self.original["identity"])
        self.checkpoint()
        self.guarded = self.backend.spawn(self.original, lease=self.folder / "lease.bin", mask=self.mask)
        self.save()
        self.backend.sync_mode(self.folder, expected=self.old_mode if "system" in self.channels else None,
                               apply_manual="system" not in self.channels)
        self.checkpoint()
        self.backend.assert_single(self.guarded or self.original["identity"])
        self.tick()

    def tick(self):
        lease = self.checkpoint()
        self.backend.verify_live()
        self.backend.assert_single(self.guarded or self.original["identity"])
        if "system" in self.channels and self.backend.mode() != self.old_mode:
            raise RuntimeError("vendor_policy_changed")
        # The vendor periodic controller is scoped by the shim. emhistor can
        # still make a sparse direct write without changing fan_mode, so renew
        # the requested output at a bounded sub-second cadence. This is not PWM
        # readback: a same-mode external write can race for one short interval.
        now = self.clock_ns()
        if lease["targets"] != self.applied or self.last_write_ns is None or now - self.last_write_ns >= 750_000_000:
            self.backend.write(lease["targets"])
            self.applied = dict(lease["targets"])
            self.last_write_ns = now
        self.sequence = lease["sequence"]
        self.status("active")

    def restore(self, reason, detail=None):
        if self._restored:
            return True
        try:
            self.status("restoring", "检测到策略竞争，正在恢复原厂控制" if reason == "vendor_policy_changed" else "正在恢复原厂控制")
        except (OSError, ValueError):
            self.phase = "restoring"  # Recovery is never gated on writable RAM.
        errors = []
        restored_identity = self.original["identity"] if self.original is not None else None
        if self.original is None:
            errors.append("vendor_daemon_unconfirmed")
        competing_policy = reason == "vendor_policy_changed" and self.backend.mode() in ("low", "medium", "high", "auto")
        # Fail high first, before touching a daemon or waiting on an RPC.
        try:
            if self.original is not None:
                emergency = {name: 255 for name in self.channels if not (name == "system" and competing_policy)}
                if emergency:
                    self.backend.write(emergency)
        except (OSError, ValueError, RuntimeError):
            errors.append("safe_high_unconfirmed")
        if self.original is not None and (self.descriptor.get("daemon_changed")
                                         or not self.backend.alive(self.original["identity"])):
            try:
                restored_identity = self.backend.restore_daemon(self.original, self.guarded, lease=self.folder / "lease.bin")
            except (OSError, ValueError, RuntimeError):
                restored_identity = None
                errors.append("vendor_daemon_unconfirmed")
        if restored_identity is not None and self.descriptor.get("daemon_changed"):
            try:
                # Never persist another fan_mode value. An explicit official-app
                # choice wins; otherwise synchronize the policy we inherited.
                self.backend.sync_mode(self.folder,
                    expected=None if competing_policy or "system" not in self.channels else self.old_mode,
                    safe_auto="system" in self.channels and not competing_policy)
            except (OSError, ValueError, RuntimeError):
                errors.append("vendor_policy_unconfirmed")
        if restored_identity is not None:
            try:
                # Do not redact the only recovery descriptor merely because a
                # spawn or signaling RPC returned before the daemon then died.
                self.backend.assert_ready(restored_identity)
            except (OSError, ValueError, RuntimeError):
                errors.append("vendor_daemon_unconfirmed")
        final_phase = "recovery_failed" if errors else "released"
        try:
            self.status(final_phase,
                        "原厂恢复未能全部确认" if errors else "已恢复原厂温控" if reason != "vendor_policy_changed" else "原厂应用已改变系统风扇策略，自定义控制已释放",
                        restore_ok=not errors, recovery_errors=errors, release_cause=reason,
                        error_detail=(str(detail)[:160] if detail else None))
        except (OSError, ValueError):
            self.phase = final_phase
        # Redact the saved environment after recovery. Keep only safe status.
        if not errors:
            self._restored = True
            try:
                atomic_json(self.folder / "descriptor.json", {"version": 1, "recovered": True})
            except (OSError, ValueError):
                pass  # Retained original descriptor stays 0600, RAM-only.
        return not errors

    def run(self, sleep=time.sleep):
        reason = "release_requested"
        detail = None
        try:
            self.begin()
            while True:
                sleep(0.2)
                self.tick()
        except (OSError, ValueError, RuntimeError) as error:
            detail = str(error)[:160]
            reason = detail if detail in ("vendor_policy_changed", "release_requested", "owner_stopped") else "lease_or_identity_failed"
        finally:
            self.restore(reason, detail)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--recover", action="store_true")
    options = parser.parse_args()
    path = Path(options.descriptor)
    info = path.lstat()
    if (sys.platform != "linux" or os.geteuid() != 0 or path.name != "descriptor.json"
            or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()):
        return 2
    value = read_json(path)
    if value and value.get("recovered") is True:
        return 0
    directory = path.parent.resolve()
    directory_info = directory.stat()
    if (not value or value.get("folder") != str(directory) or value.get("root") != "/"
            or Path("/run/r1-toolbox") not in directory.parents
            or directory_info.st_uid != os.geteuid() or directory_info.st_mode & 0o077):
        return 2
    backend = SystemBackend(root=value["root"], guard_path=value.get("guard_path"))
    watchdog = FanWatchdog(path.parent, backend)
    for item in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(item, lambda signum, frame: setattr(watchdog, "stop_requested", True))
    if options.recover:
        watchdog.restore("supervisor_stopped")
    else:
        watchdog.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
