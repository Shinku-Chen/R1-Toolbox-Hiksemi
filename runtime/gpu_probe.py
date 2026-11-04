"""Fixed, read-only Intel i915 PMU / RC6 probe for the R1's N100.

The probe is intentionally narrower than a general GPU monitor:

* only ``i915*`` event sources and ``i915`` PMU events are considered;
* event names and configs come from sysfs, never from HTTP input;
* at most four PMU candidates, 64 events, and 16 short samples are used;
* a failed open is reported with its errno name, never a path or exception;
* no GPU reset, frequency write, fan control or other hardware mutation.

The default opener/controller/reader hooks exist so unit tests can exercise
the discovery and failure paths without opening a real perf event.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import time
from pathlib import Path

MAX_CANDIDATES = 4
MAX_EVENTS = 64
MAX_SAMPLED_EVENTS = 16
MAX_EVENT_NAME = 80
MAX_SMALL_FILE = 256
SAMPLE_SECONDS = 0.15
RC6_SAMPLE_SECONDS = 0.20
_PERF_EVENT_OPEN_NR = {
    "x86_64": 298,
    "amd64": 298,
    "aarch64": 241,
    "arm64": 241,
}
_PERF_EVENT_IOC_ENABLE = 0x2400
_PERF_EVENT_IOC_DISABLE = 0x2401
_PERF_EVENT_IOC_RESET = 0x2403
_EVENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,%d}\Z" % MAX_EVENT_NAME)
_CONFIG = re.compile(r"(?:^|\s)config\s*=\s*(0[xX][0-9a-fA-F]+|[0-9]+)(?:\s|\Z)")
_RC6 = "rc6_residency_ms"


class _PerfEventAttr(ctypes.Structure):
    """Linux perf_event_attr fields used by a disabled, counted event."""

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period_or_freq", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events_or_watermark", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
        ("config2", ctypes.c_uint64),
        ("branch_sample_type", ctypes.c_uint64),
        ("sample_regs_user", ctypes.c_uint64),
        ("sample_stack_user", ctypes.c_uint32),
        ("clockid", ctypes.c_int32),
        ("sample_regs_intr", ctypes.c_uint64),
        ("aux_watermark", ctypes.c_uint32),
        ("sample_max_stack", ctypes.c_uint16),
        ("__reserved_2", ctypes.c_uint16),
        ("aux_sample_size", ctypes.c_uint32),
        ("__reserved_3", ctypes.c_uint32),
        ("sig_data", ctypes.c_uint64),
    ]


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def _read_text(path, limit=MAX_SMALL_FILE):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read(limit + 1)
    except OSError:
        return ""


def _read_int(path, *, minimum=0, maximum=2 ** 63 - 1, signed=False):
    text = _read_text(path, 64).strip()
    pattern = r"-?[0-9]{1,19}" if signed else r"[0-9]{1,19}"
    if not re.fullmatch(pattern, text):
        return None
    value = int(text)
    return value if minimum <= value <= maximum else None


def _errno_name(value):
    return errno.errorcode.get(value, "unknown")


def _cpu_from_mask(mask):
    """Return the first CPU in a Linux cpumask such as ``0-3`` or ``0,2``."""
    text = (mask or "").strip()
    first = text.split(",", 1)[0].strip()
    if not first:
        return 0
    first = first.split("-", 1)[0]
    if not re.fullmatch(r"[0-9]{1,4}", first):
        return 0
    return int(first)


def _event_config(path):
    text = _read_text(path)
    match = _CONFIG.search(text)
    if match is None:
        return None
    try:
        value = int(match.group(1), 0)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 2 ** 64 - 1 else None


def _candidates(sys_root):
    base = Path(sys_root) / "bus" / "event_source" / "devices"
    try:
        names = sorted(item.name for item in base.iterdir()
                       if item.is_dir() and (item.name == "i915" or item.name.startswith("i915_")))
    except OSError:
        return []
    result = []
    for name in names[:MAX_CANDIDATES]:
        folder = base / name
        pmu_type = _read_int(folder / "type")
        events = []
        try:
            event_names = sorted(item.name for item in (folder / "events").iterdir()
                                 if item.is_file() and not item.name.endswith(".unit")
                                 and _EVENT_NAME.fullmatch(item.name))
        except OSError:
            event_names = []
        for event_name in event_names[:MAX_EVENTS]:
            events.append({"name": event_name,
                           "config": _event_config(folder / "events" / event_name),
                           "unit": _read_text(folder / "events" / (event_name + ".unit"), 64).strip() or None})
        result.append({"name": name, "type": pmu_type,
                       "cpumask": _read_text(folder / "cpumask", 256).strip() or None,
                       "events": events})
    return result


def _open_perf(pmu_type, config, cpu):
    machine = platform.machine().lower()
    nr = _PERF_EVENT_OPEN_NR.get(machine)
    if nr is None:
        raise OSError(errno.ENOSYS, "unsupported architecture")
    attr = _PerfEventAttr()
    attr.type = int(pmu_type)
    attr.size = ctypes.sizeof(_PerfEventAttr)
    attr.config = int(config)
    # disabled, exclude_hv; no sampling, no write, no inherit.
    attr.flags = (1 << 0) | (1 << 6)
    attr.wakeup_events_or_watermark = 1
    lib = _libc()
    lib.syscall.restype = ctypes.c_long
    fd = lib.syscall(ctypes.c_long(nr), ctypes.byref(attr), ctypes.c_int(-1),
                     ctypes.c_int(int(cpu)), ctypes.c_int(-1), ctypes.c_ulong(0))
    if fd < 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))
    return int(fd)


def _ioctl(fd, request):
    if _libc().ioctl(ctypes.c_int(fd), ctypes.c_ulong(request), 0) != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))


def _read_counter(fd):
    raw = os.read(fd, 8)
    if len(raw) != 8:
        raise OSError(errno.EIO, "short perf counter")
    return int.from_bytes(raw, "little", signed=False)


def _sample_event(candidate, event, *, opener, controller, reader, closer, sleeper, sample=True):
    """Open one fixed event, optionally read a short sample, and always close it."""
    config = event.get("config")
    if candidate.get("type") is None or config is None:
        return {"name": event["name"], "config": None, "unit": event.get("unit"),
                "open": False, "sampled": bool(sample), "errno": "invalid_event",
                "read": False, "counter": None, "sample_utilization_percent": None}
    fd = None
    item = {"name": event["name"], "config": hex(config), "unit": event.get("unit"),
            "open": False, "sampled": bool(sample), "errno": None, "read": False,
            "counter": None, "sample_utilization_percent": None}
    try:
        fd = opener(candidate["type"], config, _cpu_from_mask(candidate.get("cpumask")))
        item["open"] = True
        if sample:
            try:
                controller(fd, _PERF_EVENT_IOC_RESET)
                controller(fd, _PERF_EVENT_IOC_ENABLE)
                sleeper(SAMPLE_SECONDS)
                item["counter"] = reader(fd)
                item["read"] = True
                if event.get("unit") == "ns" and event["name"].endswith("-busy"):
                    item["sample_utilization_percent"] = round(
                        max(0.0, min(100.0, item["counter"] / (SAMPLE_SECONDS * 1e9) * 100.0)), 1)
            except OSError as error:
                item["errno"] = _errno_name(error.errno)
            finally:
                try:
                    controller(fd, _PERF_EVENT_IOC_DISABLE)
                except OSError:
                    pass
    except OSError as error:
        item["errno"] = _errno_name(error.errno)
    except Exception:
        item["errno"] = "probe_failed"
    finally:
        if fd is not None:
            try:
                closer(fd)
            except Exception:
                pass
    return item


def _rc6_paths(sys_root):
    base = Path(sys_root) / "class" / "drm"
    paths = []
    try:
        cards = sorted(item for item in base.iterdir() if item.is_dir() and item.name.startswith("card"))
    except OSError:
        return []
    for card in cards[:8]:
        paths.extend(sorted(card.glob("gt/gt*/" + _RC6)))
        direct = card / "power" / _RC6
        if direct.is_file():
            paths.append(direct)
    return paths


def _rc6_probe(sys_root, *, sleeper, sys_reader):
    paths = _rc6_paths(sys_root)
    if not paths:
        return {"available": False, "reason": "rc6_not_found"}
    path = paths[0]
    try:
        before = int(sys_reader(path).strip())
        sleeper(RC6_SAMPLE_SECONDS)
        after = int(sys_reader(path).strip())
    except (AttributeError, OSError, TypeError, ValueError):
        return {"available": False, "reason": "rc6_unreadable"}
    if before < 0 or after < before:
        return {"available": False, "reason": "rc6_invalid"}
    elapsed_ms = RC6_SAMPLE_SECONDS * 1000.0
    delta = after - before
    utilization = max(0.0, min(100.0, 100.0 * (1.0 - delta / elapsed_ms)))
    return {"available": True, "reason": None,
            "source": path.name, "sampled_ms": round(elapsed_ms, 1),
            "delta_ms": delta, "utilization_percent": round(utilization, 1)}


def probe(*, sys_root="/sys", proc_root="/proc", opener=None, controller=None,
          reader=None, closer=None, sleeper=None, sys_reader=None):
    """Return bounded evidence about i915 PMU event opening and RC6.

    This is a read-only diagnostic.  The injected hooks are test seams only;
    no HTTP caller can select a path, event, CPU, account, or command.
    """
    opener = opener or _open_perf
    controller = controller or _ioctl
    reader = reader or _read_counter
    closer = closer or os.close
    sleeper = sleeper or time.sleep
    sys_reader = sys_reader or (lambda path: _read_text(path))
    machine = platform.machine().lower()
    paranoid = _read_int(Path(proc_root) / "sys" / "kernel" / "perf_event_paranoid",
                         minimum=-1, maximum=4, signed=True)
    value = {
        "schema_version": 1,
        "read_only": True,
        "architecture": machine,
        "perf_event_paranoid": paranoid,
        "pmu": {"available": False, "candidates": []},
        "rc6": {"available": False, "reason": "not_sampled"},
        "reason": None,
    }
    candidates = _candidates(sys_root)
    opened = 0
    read = 0
    sample_budget = MAX_SAMPLED_EVENTS
    for candidate in candidates:
        events = []
        type_valid = candidate.get("type") is not None
        for event in candidate["events"]:
            sample = sample_budget > 0
            if sample:
                sample_budget -= 1
            item = _sample_event(candidate, event, opener=opener, controller=controller,
                                 reader=reader, closer=closer, sleeper=sleeper,
                                 sample=sample)
            opened += int(item["open"])
            read += int(item["read"])
            events.append(item)
        value["pmu"]["candidates"].append({
            "name": candidate["name"], "type": candidate["type"],
            "cpumask": candidate.get("cpumask"),
            "events_seen": len(candidate["events"]), "events": events,
            "type_valid": type_valid,
        })
    value["pmu"]["available"] = bool(candidates)
    value["pmu"]["opened_events"] = opened
    value["pmu"]["readable_events"] = read
    value["rc6"] = _rc6_probe(sys_root, sleeper=sleeper, sys_reader=sys_reader)
    if not candidates:
        value["reason"] = "i915_pmu_not_found"
    elif opened == 0:
        value["reason"] = "no_event_opened"
    elif read == 0:
        value["reason"] = "events_opened_but_not_readable"
    return value
