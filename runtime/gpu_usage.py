"""Persistent Intel i915 PMU usage sampler for the R1 N100.

Only the four i915 engine ``*-busy`` counters are opened.  CPU-side reads are
cheap and non-blocking; the caller samples on its normal slow refresh.  RC6
residency is used only when the PMU is unavailable or unreadable.  No clock,
frequency, fan, reset, or display control is written here.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import gpu_probe

RETRY_SECONDS = 300.0
REREAD_RETRY_SECONDS = 30.0
MIN_SAMPLE_SECONDS = 0.20


def _card_folder(sys_root, pci):
    base = Path(sys_root) / "class" / "drm"
    try:
        cards = sorted(item for item in base.iterdir() if item.is_dir() and item.name.startswith("card"))
    except OSError:
        return None
    for card in cards[:8]:
        try:
            if os.path.basename(os.path.realpath(card / "device")) == pci:
                return card
        except OSError:
            continue
    for card in cards[:8]:
        try:
            if os.path.basename(os.readlink(card / "device" / "driver")) in ("i915", "xe"):
                return card
        except OSError:
            continue
    return None


def _read_int(path):
    try:
        return int(gpu_probe._read_text(path, 64).strip())
    except (TypeError, ValueError):
        return None


class IntelGpuSampler:
    """Open once, read counters on each sample, and expose max engine busy %.

    The object is process-local and never creates a CPU/GPU workload.  The
    injected hooks are fixture/test seams; they are not reachable over HTTP.
    """

    def __init__(self, sys_root="/sys", *, opener=None, reader=None, closer=None,
                 clock=None, sys_reader=None):
        self.sys_root = str(sys_root)
        self._opener = opener or (lambda pmu_type, config, cpu: gpu_probe._open_perf(
            pmu_type, config, cpu, disabled=False))
        self._reader = reader or gpu_probe._read_counter
        self._closer = closer or os.close
        self._clock = clock or time.monotonic
        self._sys_reader = sys_reader or (lambda path: gpu_probe._read_text(path))
        self._lock = threading.Lock()
        self._fds = {}
        self._events = {}
        self._prev = None
        self._last = None
        self._next_init = 0.0
        self._rc6_prev = None
        self._rc6_path = None

    def _close(self):
        for fd in self._fds.values():
            try:
                self._closer(fd)
            except Exception:
                pass
        self._fds = {}
        self._events = {}
        self._prev = None
        self._rc6_prev = None
        self._last = None

    def _rc6_candidate(self, pci):
        card = _card_folder(self.sys_root, pci)
        if card is None:
            return None
        for rel in ("gt/gt0/rc6_residency_ms", "power/rc6_residency_ms",
                    "device/tile0/gt0/gtidle/idle_residency_ms"):
            path = card / rel
            if path.is_file():
                return path
        return None

    def _init(self, pci):
        self._close()
        self._rc6_path = self._rc6_candidate(pci)
        candidates = gpu_probe._candidates(self.sys_root)
        if not candidates:
            self._next_init = self._clock() + RETRY_SECONDS
            return
        candidate = candidates[0]
        if candidate.get("type") is None:
            self._next_init = self._clock() + RETRY_SECONDS
            return
        for event in candidate.get("events", []):
            name = event.get("name", "")
            if not name.endswith("-busy") or event.get("config") is None:
                continue
            try:
                fd = self._opener(candidate["type"], event["config"],
                                  gpu_probe._cpu_from_mask(candidate.get("cpumask")))
            except OSError:
                continue
            self._fds[name] = fd
            self._events[name] = name[:-5]
        if not self._fds:
            self._next_init = self._clock() + RETRY_SECONDS
            self._close()

    def _frequency(self, pci):
        card = _card_folder(self.sys_root, pci)
        if card is None:
            return None
        for rel in ("gt/gt0/rps/act_freq_mhz", "gt/gt0/rps_cur_freq_mhz",
                    "gt_act_freq_mhz", "gt_cur_freq_mhz",
                    "device/tile0/gt0/freq0/act_freq"):
            value = _read_int(card / rel)
            if value is not None and 0 < value <= 100000:
                return value
        return None

    def _rc6_sample(self, pci):
        path = self._rc6_path or self._rc6_candidate(pci)
        self._rc6_path = path
        if path is None:
            return None
        try:
            value = int(self._sys_reader(path).strip())
        except (AttributeError, OSError, TypeError, ValueError):
            self._rc6_prev = None
            return None
        now = self._clock()
        prev, self._rc6_prev = self._rc6_prev, (now, value)
        if prev is None:
            return {"warming": True, "util": None, "util_source": None,
                    "engines": None, "freq_mhz": self._frequency(pci)}
        dt = now - prev[0]
        if dt < MIN_SAMPLE_SECONDS:
            return self._last or {"warming": True, "util": None, "util_source": None,
                                  "engines": None, "freq_mhz": self._frequency(pci)}
        delta = value - prev[1]
        if delta < 0:
            return {"warming": True, "util": None, "util_source": None,
                    "engines": None, "freq_mhz": self._frequency(pci)}
        idle = max(0.0, min(100.0, delta / (dt * 1000.0) * 100.0))
        return {"warming": False, "util": round(max(0.0, 100.0 - idle), 1),
                "util_source": "rc6", "engines": None,
                "freq_mhz": self._frequency(pci)}

    def sample(self, pci=None):
        with self._lock:
            now = self._clock()
            if not self._fds and now >= self._next_init:
                self._init(pci)
            if self._fds:
                try:
                    values = {name: int(self._reader(fd)) for name, fd in self._fds.items()}
                except Exception:
                    self._close()
                    self._next_init = self._clock() + REREAD_RETRY_SECONDS
                    return self._rc6_sample(pci)
                prev, self._prev = self._prev, (now, values)
                if prev is None:
                    return {"warming": True, "util": None, "util_source": None,
                            "engines": None, "freq_mhz": self._frequency(pci)}
                dt = now - prev[0]
                if dt < MIN_SAMPLE_SECONDS:
                    return self._last or {"warming": True, "util": None, "util_source": None,
                                          "engines": None, "freq_mhz": self._frequency(pci)}
                engines = {}
                for name, value in values.items():
                    if name not in prev[1]:
                        continue
                    delta = value - prev[1][name]
                    if delta < 0:
                        self._prev = (now, values)
                        self._last = {"warming": True, "util": None, "util_source": None,
                                      "engines": None, "freq_mhz": self._frequency(pci)}
                        return self._last
                    engine = self._events.get(name)
                    if engine:
                        pct = max(0.0, min(100.0, delta / (dt * 1e9) * 100.0))
                        engines[engine] = max(engines.get(engine, 0.0), pct)
                self._last = {"warming": False,
                              "util": round(max(engines.values()), 1) if engines else None,
                              "util_source": "pmu",
                              "engines": {key: round(value, 1) for key, value in engines.items()} or None,
                              "freq_mhz": self._frequency(pci)}
                return self._last
            return self._rc6_sample(pci)

    def close(self):
        with self._lock:
            self._close()
            self._rc6_prev = None
            self._last = None
