"""Bounded, passive fan evidence collection; never signals or opens a device.

This module deliberately does not import fan_watchdog (and hence fcntl), call
the adapter backend, or acquire its control lock. Vendor GET is not a read-only
operation: it sends USR signals, so all evidence comes from fixed local files.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import threading
import time

from fan_native import DRIVER_NOTE, FIRMWARE_SHA256


REQUIRED_SIGNALS = 0xa00  # Linux SIGUSR1/SIGUSR2; Windows numbers differ.
MAX_PROCESSES = 64
MAX_PROC_ENTRIES = 4096
MAX_SAMPLES = 600
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SAMPLE_BYTES = MAX_CAPTURE_BYTES - MAX_SNAPSHOT_BYTES - 65536  # Job metadata and encoding margin.
MAX_RUNTIME_ENTRIES = 256
MAX_RUNTIME_FOLDERS = 8
CONFIG_FIELDS = ("mode", "sources", "quiet_below", "normal_at", "full_at",
                 "quiet_percent", "normal_percent", "manual_percent")
CHANNEL_FIELDS = ("active", "mode", "level", "target_percent", "target_raw",
                  "reference_c", "source_id", "source_label", "reason", "applied_monotonic")
STATUS_FIELDS = ("phase", "reason", "applied_sequence", "watchdog", "restore_ok",
                 "recovery_errors", "release_cause", "error_detail")


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _error(error, stage):
    # Exception text may contain file contents or environment values.
    return {"stage": stage, "type": type(error).__name__, "errno": getattr(error, "errno", None)}


def _bounded(value, depth=0):
    """JSON-compatible public data only; all callers select allowed fields first."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:4096]
    if depth >= 6:
        return None
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in value[:128]]
    if isinstance(value, dict):
        return {str(key)[:128]: _bounded(item, depth + 1)
                for key, item in list(value.items())[:64]}
    return None


def _select(value, fields):
    return {key: _bounded(value.get(key)) for key in fields} if isinstance(value, dict) else None


def _inside(path, root):
    return path == root or root in path.parents


def _read_file(path, limit):
    """Read proc seq_files through EOF; a single read may be only one page."""
    descriptor, chunks, size = None, [], 0
    evidence = {"bytes_read": 0, "complete": False, "truncated": False, "error": None}
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            evidence["error"] = {"code": "not_regular_file"}
            return None, evidence
        if info.st_size > limit:
            evidence["truncated"] = True
            return None, evidence
        while size <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                evidence.update(bytes_read=size, complete=True)
                return b"".join(chunks), evidence
            size += len(chunk)
            chunks.append(chunk)
        evidence.update(bytes_read=size, truncated=True)
        return b"".join(chunks)[:limit], evidence
    except (OSError, ValueError) as error:
        evidence.update(bytes_read=size, error=_error(error, "file_read"))
        return b"".join(chunks) if chunks else None, evidence
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_bytes(path, limit):
    raw, evidence = _read_file(path, limit)
    return raw if evidence["complete"] else None


def _read_json(path, limit=131072, trailing_nul=False):
    raw = read_bytes(path, limit)
    if raw is None:
        return None
    try:
        value = json.loads(raw.rstrip(b"\0") if trailing_nul else raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


class FanDiagnostics:
    """One in-memory, time-limited capture per service; no resident sampler.

    snapshot() includes expensive static preflight evidence. Capture samples
    omit binary hashing and contain only changed top-level evidence groups.
    Sampling observes a non-atomic system; every process reports identity
    stability across its own file reads rather than claiming a global lock.
    """

    def __init__(self, policy, version="dev"):
        self.policy = policy
        self.adapter = getattr(policy, "adapter", None)
        self.version = str(version)[:256]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._job = None
        self._closed = False

    def _paths(self):
        adapter = self.adapter
        if adapter is None:
            return None
        return (Path(adapter.root).resolve(), Path(adapter.runtime_dir).resolve(),
                Path(adapter.guard_path).resolve())

    def _policy(self):
        """Do not block a pending policy update or touch adapter.lock."""
        lock = getattr(self.policy, "lock", None)
        acquired = lock.acquire(blocking=False) if lock is not None else True
        if not acquired:
            return {"available": False, "reason": "policy_lock_busy"}
        try:
            config = getattr(self.policy, "config", None)
            channels = getattr(self.policy, "channels", None)
            safe_config = None
            if isinstance(config, dict):
                values = config.get("channels")
                safe_config = {"schema_version": _bounded(config.get("schema_version")),
                               "channels": {name: _select(values.get(name), CONFIG_FIELDS)
                                            for name in ("cpu", "system")} if isinstance(values, dict) else None}
            safe_channels = {name: _select(channels.get(name), CHANNEL_FIELDS)
                             for name in ("cpu", "system")} if isinstance(channels, dict) else None
            return {"available": True, "config": safe_config, "channels": safe_channels,
                    "busy": _bounded(getattr(self.policy, "busy", None)),
                    "active": any(item.get("active") is True for item in safe_channels.values()
                                  if isinstance(item, dict)) if safe_channels is not None else None,
                    "active_on_boot": _bounded(getattr(self.policy, "active_on_boot", None)),
                    "last_error": _bounded(getattr(self.policy, "last_error", None)),
                    "settings_error": _bounded(getattr(self.policy, "settings_error", None))}
        finally:
            if lock is not None:
                lock.release()

    @staticmethod
    def _identity(base):
        raw = read_bytes(base / "stat", 8192)
        try:
            fields = raw.decode("ascii").rsplit(")", 1)[1].split() if raw else []
            return {"starttime": int(fields[19]), "state": fields[0],
                    "exe": os.readlink(base / "exe")} if len(fields) >= 20 else None
        except (OSError, ValueError, UnicodeError, IndexError):
            return None

    @staticmethod
    def _mask(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 16:
            return None
        try:
            return int(value, 16) if all(char in "0123456789abcdefABCDEF" for char in value) else None
        except ValueError:
            return None

    def _process(self, pid, proc, program, guard, executable):
        base = proc / str(pid)
        before = self._identity(base)
        status_raw = read_bytes(base / "status", 65536)
        status = {}
        for line in (status_raw or b"").splitlines():
            key, colon, value = line.partition(b":")
            if colon and key in (b"Name", b"State", b"SigCgt", b"SigBlk"):
                key = key.decode("ascii")
                # Duplicate masks must not turn corrupt data into a ready claim.
                status[key] = None if key in status else value.strip().decode("ascii", "replace")[:256]
        maps_raw, maps_evidence = _read_file(base / "maps", 1024 * 1024)
        paths, omitted = [], 0
        exact_match, name_match = False, False
        expected = str(guard)
        for line in (maps_raw or b"").splitlines():
            fields = line.split(None, 5)
            if len(fields) == 6 and b"fan_guard.so" in fields[-1]:
                path = os.fsdecode(fields[-1])[:4096]
                exact_match = exact_match or path in (expected, expected + " (deleted)")
                name_match = name_match or Path(path.removesuffix(" (deleted)")).name == guard.name
                if path not in paths and len(paths) < 16:
                    paths.append(path)
                elif path not in paths:
                    omitted += 1
        after = self._identity(base)
        caught, blocked = self._mask(status.get("SigCgt")), self._mask(status.get("SigBlk"))
        stable = (before is not None and after is not None
                  and (before["starttime"], before["exe"]) == (after["starttime"], after["exe"]))
        identity = before or after
        return {"pid": pid, "starttime": identity.get("starttime") if identity else None,
                "exe": identity.get("exe") if identity else executable,
                "state": status.get("State") or (identity.get("state") if identity else None),
                "identity_stable": stable,
                "alive": stable and before["state"] not in ("Z", "X") and after["state"] not in ("Z", "X"),
                "terminated": bool(after and after["state"] in ("Z", "X")),
                "verified_exe": executable == str(program) and bool(identity and identity["exe"] == str(program)),
                "sigcgt": status.get("SigCgt"), "sigblk": status.get("SigBlk"),
                "required": hex(REQUIRED_SIGNALS),
                "caught_required": caught & REQUIRED_SIGNALS == REQUIRED_SIGNALS if caught is not None else None,
                "blocked_required": bool(blocked & REQUIRED_SIGNALS) if blocked is not None else None,
                "guard_expected": expected, "guard_paths": paths if maps_raw is not None else None,
                "guard_exact_match": (True if exact_match
                                      else False if maps_evidence["complete"] else None),
                "guard_name_match": (True if name_match
                                     else False if maps_evidence["complete"] else None),
                "guard_paths_omitted": omitted,
                "status_available": status_raw is not None, "maps_available": maps_raw is not None,
                "maps_bytes_read": maps_evidence["bytes_read"], "maps_complete": maps_evidence["complete"],
                "maps_truncated": maps_evidence["truncated"], "maps_error": maps_evidence["error"]}

    def _processes(self, root, guard):
        proc = root / "proc"
        program = (root / "histor/bin/temp_control").resolve()
        rows, errors, truncated = [], [], False
        try:
            with os.scandir(proc) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_PROC_ENTRIES:
                        truncated = True
                        break
                    if not entry.name.isascii() or not entry.name.isdecimal():
                        continue
                    base = proc / entry.name
                    try:
                        executable = os.readlink(base / "exe")
                    except OSError:
                        comm = read_bytes(base / "comm", 256)
                        if (comm or b"").strip() != b"temp_control":
                            continue
                        executable = None
                    if executable is not None and Path(executable.removesuffix(" (deleted)")).name != "temp_control":
                        continue
                    if len(rows) >= MAX_PROCESSES:
                        truncated = True
                        break
                    rows.append(self._process(int(entry.name), proc, program, guard, executable))
        except OSError as error:
            errors.append(_error(error, "proc_scan"))
        rows.sort(key=lambda value: value["pid"])
        living = [row for row in rows if not row["terminated"]]
        unique = len(living) == 1 and living[0]["verified_exe"] and living[0]["alive"]
        return {"items": rows, "count": len(rows), "unique_verified": unique and not truncated and not errors,
                "truncated": truncated, "errors": errors, "required": hex(REQUIRED_SIGNALS)}

    @staticmethod
    def _readiness(value):
        if not isinstance(value, dict):
            return None
        # Journals are produced by our supervisor, but do not let an extra
        # private field accidentally become a downloadable recovery descriptor.
        event_fields = ("sequence", "attempt", "operation", "event", "result", "monotonic", "elapsed",
                        "stable_seconds", "stable_elapsed", "remaining", "ready", "pid", "starttime", "exe",
                        "alive", "identity_ok", "sigcgt_raw", "sigcgt_fields", "sigcgt", "sigcgt_hex", "required",
                        "required_hex", "handlers_ready", "require_guard", "guard_expected", "guard_paths",
                        "guard_ok", "single_process", "status_bytes_read", "maps_bytes_read", "sigcgt_read_ok", "sigcgt_parse_ok",
                        "maps_read_ok", "error", "fields_truncated", "guard_paths_omitted")
        result = {"schema_version": _bounded(value.get("schema_version"))}
        def event(item):
            selected = _select(item, event_fields)
            if selected is not None:
                selected["error"] = _select(selected.get("error"), ("code", "message"))
            return selected
        for key in ("first", "last", "failure"):
            result[key] = event(value.get(key))
        events = value.get("events")
        result["events"] = [event(item) for item in events[:128]] if isinstance(events, list) else []
        for key in ("attempt_count", "event_count", "dropped_events"):
            if key in value:
                result[key] = _bounded(value[key])
        return result

    def _runtime(self, runtime, full=False):
        folders, errors, truncated = [], [], False
        current = getattr(self.adapter, "folder", None)
        selected = None
        if current is not None:
            candidate = Path(current).resolve()
            if _inside(candidate, runtime) and candidate.parent == runtime and candidate.name.startswith("fan-"):
                selected = candidate
        try:
            candidates = []
            with os.scandir(runtime) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_RUNTIME_ENTRIES:
                        truncated = True
                        break
                    if not entry.name.startswith("fan-") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        candidates.append((entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path)))
                    except OSError:
                        continue
            candidates.sort(key=lambda item: item[0], reverse=True)
            paths = ([selected] if selected else []) + [item[1] for item in candidates if item[1] != selected]
            truncated = truncated or len(paths) > MAX_RUNTIME_FOLDERS
            for path in paths[:MAX_RUNTIME_FOLDERS]:
                # Never open descriptor.json, lease.bin, cmdline or environ.
                status = _select(_read_json(path / "status.json"), STATUS_FIELDS)
                if status is not None:
                    status["watchdog"] = _select(status.get("watchdog"), ("pid", "starttime", "exe"))
                readiness = self._readiness(_read_json(path / "readiness.json"))
                if readiness is not None and not full:
                    # Full journal is retained in the final snapshot; frequent
                    # samples need only transitions and stable evidence anchors.
                    readiness["events_in_final_snapshot"] = True
                    readiness.pop("events", None)
                folders.append({"name": path.name, "current": path == selected, "status": status,
                                "readiness": readiness})
        except FileNotFoundError:
            pass  # No handover has started yet; not a diagnostic failure.
        except OSError as error:
            errors.append(_error(error, "runtime_scan"))
        return {"directories": folders, "truncated": truncated, "errors": errors}

    @staticmethod
    def _mode(root):
        value = _read_json(root / "tmp/nand_config/config.txt", 1024 * 1024, trailing_nul=True)
        fan = value.get("fan") if value else None
        code = fan.get("fan_mode") if isinstance(fan, dict) else None
        return {"code": code if type(code) is int else None,
                "mode": {2: "low", 3: "medium", 4: "high", 5: "auto"}.get(code) if type(code) is int else None}

    def _preflight(self, root, guard, processes, mode):
        checks = {}
        checks["linux_root"] = {"ok": sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0}
        checks["writes_enabled"] = {"ok": getattr(self.adapter, "allow_writes", None) is True}
        checks["systemd_run"] = {"ok": shutil.which("systemd-run") is not None}
        note_path = root / "sys/module/hikio/notes/.note.gnu.build-id"
        note = read_bytes(note_path, 36) if _inside(note_path.resolve(), root) else None
        checks["driver"] = {"ok": note == DRIVER_NOTE, "expected_note": DRIVER_NOTE.hex(),
                            "actual_note": note.hex() if note is not None else None}
        program = (root / "histor/bin/temp_control").resolve()
        inside = _inside(program, root)
        binary = read_bytes(program, 4 * 1024 * 1024) if inside else None
        actual_hash = hashlib.sha256(binary).hexdigest() if binary is not None else None
        backend = getattr(self.adapter, "backend", None)  # Observe only; never initialize or call it.
        cached_signature = getattr(backend, "_signature", None)
        try:
            info = program.stat()
            signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            signature = None
        signature_matches = signature == cached_signature if cached_signature is not None else None
        checks["firmware"] = {"ok": inside and actual_hash == FIRMWARE_SHA256, "path": str(program),
                              "inside_root": inside, "expected_sha256": FIRMWARE_SHA256, "actual_sha256": actual_hash,
                              "matches_cached_identity": signature_matches}
        checks["firmware"]["ok"] = checks["firmware"]["ok"] and signature_matches is not False
        try:
            device = (root / "dev/hikio").lstat()
            current = (device.st_dev, device.st_ino, getattr(device, "st_rdev", None))
            cached_device = getattr(backend, "_device", None)
            matches_device = current == cached_device if cached_device is not None else None
            checks["character_device"] = {"ok": stat.S_ISCHR(device.st_mode), "mode": oct(device.st_mode),
                                          "device": device.st_dev, "inode": device.st_ino,
                                          "rdev": getattr(device, "st_rdev", None),
                                          "matches_cached_identity": matches_device}
            checks["character_device"]["ok"] = checks["character_device"]["ok"] and matches_device is not False
        except OSError as error:
            checks["character_device"] = {"ok": False, "error": _error(error, "device_stat")}
        raw_guard = read_bytes(guard, 2 * 1024 * 1024)
        try:
            info = Path(self.adapter.guard_path).lstat()
            guard_valid = (stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and raw_guard is not None
                           and len(raw_guard) >= 64 and raw_guard[:6] == b"\x7fELF\x02\x01"
                           and raw_guard[16:20] == b"\x03\x00\x3e\x00")
        except OSError:
            guard_valid = False
        checks["guard"] = {"ok": guard_valid, "expected": str(guard)}
        checks["unique_vendor"] = {"ok": processes["unique_verified"], "count": processes["count"],
                                   "truncated": processes["truncated"]}
        checks["vendor_mode"] = {"ok": mode["mode"] is not None, **mode}
        try:
            endpoint = (root / "var/tmp/emhistor").stat()
            socket_ok = stat.S_ISSOCK(endpoint.st_mode)
        except OSError:
            socket_ok = False
        checks["unix_socket"] = {"ok": socket_ok}
        return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}

    def _collect(self, full=False):
        value = {"schema_version": 1, "version": self.version, "captured_at": _stamp(),
                 "available": False, "reason": None, "policy": self._policy(), "errors": []}
        if sys.platform != "linux":
            value["reason"] = "linux_proc_unavailable"
            return value
        paths = self._paths()
        if paths is None:
            value["reason"] = "native_adapter_unavailable"
            return value
        root, runtime, guard = paths
        if not (root / "proc").is_dir():
            value["reason"] = "linux_proc_unavailable"
            return value
        value["available"] = True
        value["processes"] = self._processes(root, guard)
        value["vendor_mode"] = self._mode(root)
        value["runtime"] = self._runtime(runtime, full=full)
        if full:
            value["preflight"] = self._preflight(root, guard, value["processes"], value["vendor_mode"])
        return value

    def snapshot(self):
        """Return full passive evidence even if control is busy or unsupported."""
        try:
            return self._fit_snapshot(self._collect(full=True))
        except Exception as error:
            return {"schema_version": 1, "version": self.version, "captured_at": _stamp(),
                    "available": False, "reason": "collection_failed", "errors": [_error(error, "snapshot")]}

    def _sample(self, started, previous):
        current = self._fit_snapshot(self._collect(full=previous is None))
        captured_at = current.pop("captured_at")
        changes = {key: item for key, item in current.items() if previous is None or previous.get(key) != item}
        return ({"captured_at": captured_at, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                 "changes": changes}, current)

    @staticmethod
    def _encoded_size(value):
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))

    def _fit_snapshot(self, value):
        """Bound final evidence separately from the sample byte reservation."""
        if self._encoded_size(value) <= MAX_SNAPSHOT_BYTES:
            return value
        value["truncated"] = True
        omissions = value["diagnostic_omissions"] = []
        directories = value.get("runtime", {}).get("directories", [])
        for folder in directories:
            journal = folder.get("readiness")
            if journal and journal.get("events"):
                journal["events_omitted"] = len(journal["events"])
                journal["events"] = []
        omissions.append("runtime.readiness.events")
        if self._encoded_size(value) <= MAX_SNAPSHOT_BYTES:
            return value
        for process in value.get("processes", {}).get("items", []):
            paths = process.get("guard_paths")
            if paths:
                process["guard_paths_omitted"] = process.get("guard_paths_omitted", 0) + len(paths) - 1
                process["guard_paths"] = paths[:1]
        for folder in directories:
            journal = folder.get("readiness") or {}
            for key in ("first", "last", "failure"):
                event = journal.get(key)
                if not event:
                    continue
                for field, item in list(event.items()):
                    if isinstance(item, list) and len(item) > 1:
                        event[field] = item[:1]
                        omissions.append("runtime.readiness." + key + "." + field)
        omissions.append("processes.guard_paths")
        if self._encoded_size(value) <= MAX_SNAPSHOT_BYTES:
            return value
        # Only hostile or corrupt local files should reach this fallback.
        return {"schema_version": 1, "version": self.version, "captured_at": value["captured_at"],
                "available": False, "reason": "snapshot_byte_limit", "truncated": True,
                "errors": [], "diagnostic_omissions": ["snapshot exceeds byte limit"]}

    def _append_sample(self, sample):
        size = self._encoded_size(sample) + 1
        if self._job["sample_bytes"] + size > MAX_SAMPLE_BYTES:
            self._job.update(truncated=True, stop_reason="byte_limit")
            return False
        self._job["samples"].append(sample)
        self._job["sample_bytes"] += size
        return True

    def start(self, duration_seconds=45, interval_ms=250):
        """Start one finite observation; first sample precedes the return.

        Arguments are strict integers, including rejecting booleans. At most
        600 samples are retained; a 60s/100ms request may finish at this cap,
        reporting truncated=true and stop_reason=sample_limit explicitly.
        """
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 60:
            raise ValueError("duration_seconds 必须为 1–60 的整数")
        if type(interval_ms) is not int or not 100 <= interval_ms <= 2000:
            raise ValueError("interval_ms 必须为 100–2000 的整数")
        with self._lock:
            if self._closed:
                raise RuntimeError("风扇诊断服务已停止")
            if self._job and self._job["state"] == "running":
                raise RuntimeError("已有风扇诊断采集正在运行")
            started = time.monotonic()
            self._stop_event.clear()
            self._job = {"id": secrets.token_hex(12), "state": "running", "started_at": _stamp(),
                         "duration_seconds": duration_seconds, "interval_ms": interval_ms,
                         "max_samples": MAX_SAMPLES, "max_capture_bytes": MAX_CAPTURE_BYTES,
                         "sample_bytes": 0, "truncated": False, "samples": [], "errors": [],
                         "final_snapshot": None, "finished_at": None, "stop_reason": None}
            try:
                sample, previous = self._sample(started, None)
                self._append_sample(sample)
                self._thread = threading.Thread(target=self._run, args=(self._job["id"], started, previous),
                                                daemon=True, name="r1-fan-diagnostics")
                self._thread.start()
            except Exception as error:
                self._job.update(state="failed", finished_at=_stamp(), stop_reason="collection_failed")
                self._job["errors"].append(_error(error, "capture_start"))
            return copy.deepcopy(self._job)

    def _run(self, job_id, started, previous):
        reason, failed = "duration_elapsed", False
        try:
            with self._lock:
                duration, interval = self._job["duration_seconds"], self._job["interval_ms"] / 1000
            deadline, next_sample = started + duration, started + interval
            while True:
                now = time.monotonic()
                if now >= deadline:
                    break
                if self._stop_event.wait(max(0, min(next_sample, deadline) - now)):
                    reason = "service_stopped"
                    break
                if time.monotonic() >= deadline:
                    break
                sample, previous = self._sample(started, previous)
                with self._lock:
                    if self._job["id"] != job_id:
                        return
                    if not self._append_sample(sample):
                        reason = "byte_limit"
                        break
                    if len(self._job["samples"]) >= MAX_SAMPLES:
                        self._job["truncated"] = True
                        reason = "sample_limit"
                        break
                next_sample = max(next_sample + interval, time.monotonic())
        except Exception as error:
            failed, reason = True, "collection_failed"
            with self._lock:
                self._job["errors"].append(_error(error, "capture_sample"))
        finally:
            final = self.snapshot()
            with self._lock:
                if self._job["id"] == job_id:
                    self._job.update(state="failed" if failed else "stopped" if self._stop_event.is_set() else "completed",
                                     finished_at=_stamp(), final_snapshot=final, stop_reason=reason)

    def result(self, job_id):
        with self._lock:
            if not isinstance(job_id, str) or not self._job or self._job["id"] != job_id:
                raise KeyError("风扇诊断任务不存在或已被新任务替换")
            return copy.deepcopy(self._job)

    def stop(self):
        """Wake a finite capture on shutdown; never alter fan ownership."""
        with self._lock:
            self._closed = True
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
