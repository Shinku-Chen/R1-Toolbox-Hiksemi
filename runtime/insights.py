"""Bounded, local NAS diagnostics and explicitly confirmed application actions.

Fast proc/sys sampling and slow optional commands run in separate workers. The
HTTP-facing snapshot copies memory and validates cached disk lifetimes in sysfs;
SMART runs only for an explicit Web fill/refresh or a live fan-policy demand;
inventory workers and GETs never initiate it.
All commands have fixed arguments and run without a shell.
"""

import array
from collections import deque
from contextlib import nullcontext
import copy
import datetime
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

from device_info import collect_device_info
from disk_names import VendorStorageClient, read_disk_names

try:
    import fcntl
except ImportError:
    fcntl = None

# Use the installer's local daemon. On the NAS, ps took 13.26 seconds;
# allow bounded reads and a normal graceful stop without retrying writes.
DOCKER_LOCAL_HOST = "unix:///var/run/docker.sock"
DOCKER_READ_TIMEOUT = 20
DOCKER_START_TIMEOUT = 20
DOCKER_STOP_TIMEOUT = 30
SMART_CACHE_SECONDS = 30.0
SMART_REFRESH_TIMEOUT = 30.0
_DOCKER_CONNECTION_ENV = frozenset(("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_API_VERSION",
                                     "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"))


class SmartRefreshError(ValueError):
    """Fixed explicit-refresh failure; no command output or paths are exposed."""
    def __init__(self, reason):
        self.reason = reason if reason in ('invalid_parameters', 'authorization_denied', 'busy', 'stopping') else 'invalid_parameters'
        super().__init__(self.reason)


_SMART_POWER_ATA_VALUES = {
    'SLEEP': -1, 'STANDBY': 0, 'STANDBY_Y': 1,
    'IDLE': 128, 'IDLE_A': 129, 'IDLE_B': 130, 'IDLE_C': 131,
    'ACTIVE_NV_DOWN': 64, 'ACTIVE_NV_UP': 65, 'ACTIVE OR IDLE': 255,
}
_SMART_NON_STANDBY = frozenset(('ACTIVE', 'IDLE', 'IDLE_A', 'IDLE_B', 'IDLE_C', 'ACTIVE OR IDLE'))
_SMART_STANDBY = frozenset(('SLEEP', 'STANDBY', 'STANDBY_Y'))


def _smart_power_mode(value):
    """Read official name/ATA fields or legacy string, rejecting contradictions.

    smartmontools 7.5 ataprint.cpp emits name and ata_value. Missing power
    evidence does not invalidate an otherwise valid temperature, but never
    authorizes capacity. Obsolete NV modes stay outside the capacity allowlist.
    """
    if not isinstance(value, dict):
        return None, True
    # A lone empty legacy field was the old representation of no evidence.
    # It must not become a fallback that conceals another field or ATA value.
    if (set(value) == {'string'} and isinstance(value['string'], str)
            and len(value['string']) <= 64 and not value['string'].strip()):
        return None, False
    modes = []
    for key in ('name', 'string'):
        if key not in value:
            continue
        text = value[key]
        if (not isinstance(text, str) or not 0 < len(text) <= 64
                or any(not 32 <= ord(char) <= 126 and char not in '\t\n\r\f\v' for char in text)):
            return None, True
        mode = ' '.join(text.upper().split())
        if mode.endswith(' MODE'):
            mode = mode[:-5]
        modes.append(mode)
    if not modes:
        return None, bool(value)
    if len(set(modes)) != 1:
        return None, True
    mode = modes[0]
    if mode != 'ACTIVE' and mode not in _SMART_POWER_ATA_VALUES:
        # An unrecognized label never proves power state, but does not itself
        # invalidate an independently valid temperature/health measurement.
        # Explicit check failures retain the existing unsafe -n rejection.
        unsafe = re.search(r'\b(?:unknown|unsupported|not[\s_-]+supported|not[\s_-]+implemented|failed|unavailable)\b', mode, re.I)
        return None, bool(unsafe or 'ata_value' in value)
    if 'ata_value' in value:
        number = value['ata_value']
        if (type(number) is not int or mode not in _SMART_POWER_ATA_VALUES
                or number != _SMART_POWER_ATA_VALUES[mode]):
            return None, True
    return mode, False


def _text(path, limit=262144):
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit)
    except (OSError, ValueError):
        return None


def _integer(path):
    try:
        value = int((_text(path, 128) or "").strip())
        return value if value >= 0 else None
    except ValueError:
        return None


def _finite(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            return None
    except OverflowError:
        return None
    return value


def _timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _capability(reason="正在探测"):
    return {"available": False, "reason": reason, "items": []}


def _ipv4_addresses():
    """Enumerate local interface addresses, including IPv4 aliases, on Linux."""
    if fcntl is None or not sys.platform.startswith("linux"):
        return {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            storage = array.array("B", bytes(65536))
            request = struct.pack("iP", len(storage), storage.buffer_info()[0])
            answer = fcntl.ioctl(connection.fileno(), 0x8912, request)  # SIOCGIFCONF
            length = struct.unpack("iP", answer)[0]
            stride = 40 if struct.calcsize("P") == 8 else 32
            addresses = {}
            raw = storage.tobytes()
            for offset in range(0, min(length, len(raw)) - stride + 1, stride):
                name = raw[offset:offset + 16].split(b"\0", 1)[0].decode("utf-8", "replace")
                name = name.split(":", 1)[0]
                address = socket.inet_ntoa(raw[offset + 20:offset + 24])
                if address not in addresses.setdefault(name, []):
                    addresses[name].append(address)
            return addresses
    except (OSError, ValueError, struct.error):
        return {}


def _command(argv, timeout=3):
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    if Path(argv[0]).name == "docker":
        # Ignore inherited remote contexts, TLS and forced API negotiation.
        for name in _DOCKER_CONNECTION_ENV:
            environment.pop(name, None)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          check=False, env=environment)


def _memory(proc_root):
    values = {}
    for line in (_text(proc_root / "meminfo") or "").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB":
            try:
                value = int(parts[1]) * 1024
                if value >= 0:
                    values[parts[0].rstrip(":")] = value
            except ValueError:
                pass
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    used = total - available if total and available is not None and available <= total else None
    swap_total, swap_free = values.get("SwapTotal"), values.get("SwapFree")
    swap_used = swap_total - swap_free if swap_total is not None and swap_free is not None and swap_free <= swap_total else None
    return {
        "total_bytes": total, "available_bytes": available, "used_bytes": used,
        "usage_percent": used / total * 100 if used is not None else None,
        "swap_total_bytes": swap_total, "swap_used_bytes": swap_used,
        "swap_usage_percent": swap_used / swap_total * 100 if swap_total and swap_used is not None else None,
    }


def _cpu_ticks(proc_root):
    ticks = {}
    for line in (_text(proc_root / "stat") or "").splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"cpu\d*", fields[0]):
            continue
        try:
            values = [int(value) for value in fields[1:9]]
            if len(values) < 4 or any(value < 0 for value in values):
                continue
            ticks[fields[0]] = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
        except ValueError:
            pass
    return ticks


def _percent_delta(previous, current):
    if previous is None or current is None:
        return None
    total, idle = current[0] - previous[0], current[1] - previous[1]
    return (total - idle) / total * 100 if total > 0 and 0 <= idle <= total else None


def _size_string(text):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", str(text))
    if not match:
        return None
    units = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
             "TB": 1000 ** 4, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    factor = units.get(match[2])
    value = _finite(float(match[1]))
    return int(value * factor) if factor is not None and value is not None else None


def _percent_string(value, maximum=None):
    try:
        return _finite(float(str(value).rstrip("%")), maximum=maximum)
    except (ValueError, TypeError):
        return None


def _json_lines(text):
    rows = []
    for line in str(text or "").splitlines()[:512]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except (ValueError, TypeError):
            pass
    return rows


class InsightsCollector:
    """Collect local diagnostics; optional integrations report explicit reasons."""

    def __init__(self, proc_root="/proc", sys_root="/sys", runner=None,
                 command_finder=None, clock=None, sample_interval=2.0,
                 slow_interval=30.0, history_limit=120, ipv4_provider=None,
                 page_size=None, clock_ticks=None, etc_root=None, run_root=None,
                 disk_config_root=None, storage_client=None):
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self.runner = runner or _command
        self.command_finder = command_finder or shutil.which
        self.clock = clock or time.monotonic
        self.ipv4_provider = ipv4_provider or _ipv4_addresses
        self.sample_interval = max(0.1, float(sample_interval))
        self.slow_interval = max(1.0, float(slow_interval))
        self.page_size = page_size or os.sysconf("SC_PAGE_SIZE")
        self.clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        # Fixture proc trees must never fall through to this computer's /etc.
        self.etc_root = Path(etc_root) if etc_root is not None else (
            Path("/etc") if self.proc_root == Path("/proc") else self.proc_root.parent / "etc")
        self.run_root = Path(run_root) if run_root is not None else (
            Path("/run") if self.proc_root == Path("/proc") else self.proc_root.parent / "run")
        self.disk_config_root = Path(disk_config_root) if disk_config_root is not None else (
            Path("/tmp/nand_config") if self.proc_root == Path("/proc") and self.sys_root == Path("/sys")
            else self.proc_root.parent / "tmp/nand_config")
        # The old config-root argument remains accepted for fixture callers;
        # migration databases are no longer a source of current user names.
        host_root = Path("/") if self.proc_root == Path("/proc") and self.sys_root == Path("/sys") else self.proc_root.parent
        self.storage_client = storage_client or VendorStorageClient(
            root=host_root, runtime_dir=self.run_root / "r1-toolbox/storage")
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._stop = threading.Event()
        self._refresh = {name: threading.Event() for name in ("disks", "containers", "vms", "device")}
        self._threads = []
        self._history = deque(maxlen=max(1, min(int(history_limit), 120)))
        self._previous_cpu = {}
        self._previous_network = {}
        self._previous_process = {}
        self._disk_activity_lock = threading.Lock()
        self._disk_activity_previous = None
        self._smart_cache = {}
        # Automatic fan demand gets measurements only, never serial/raw detail
        # authority. Attempts also bind short-lived negative results to a disk
        # lifetime, so visible-page fills do not repeatedly wake/query failures.
        self._smart_measurements = {}
        self._smart_attempts = {}
        self._smart_batch_sequence = 0
        # Explicit visible refreshes supersede old measurement verdicts without
        # adding serial/raw authority or changing their historical cache bytes.
        self._smart_visible_observations = {}
        self._smart_visible_batch = None
        self._smart_refresh_lock = threading.Lock()
        self._smart_policy_worker = None
        self._smart_epoch = 0
        # Control-catalog aliases are grants for one applied policy only. They
        # never change public disk identities or grant serial/raw-data access.
        self._smart_policy_aliases = {}
        self._disk_name_context = {}
        # Serial identity survives a standby-only SMART response, independently
        # of all temperature/health/raw-data expiry. It never survives a changed
        # sysfs lifetime or disagreement with a current identity source.
        self._disk_serial_cache = {}
        # Owner Web identity is a separate projection, never a public snapshot.
        # Each record is tied to the same sampled lifetime and fixed vendor list.
        self._disk_verified_serials = {}
        self._state = {
            "schema_version": 1, "timestamp": None, "system": {}, "interfaces": [],
            "disks": _capability(), "containers": _capability(), "vms": _capability(),
            "ports": [], "processes": [], "gpu": {"available": False, "reason": "正在探测", "devices": []},
            "history": [], "device": {},
        }

    def start(self):
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return self
            self._stop.clear()
            self._threads = [threading.Thread(target=self._fast_loop, daemon=True, name="r1-insights-fast")]
            for name, collect in self._integrations():
                self._threads.append(threading.Thread(target=self._slow_loop, args=(name, collect),
                                                      daemon=True, name="r1-insights-" + name))
            for thread in self._threads:
                thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            self._smart_epoch += 1
            self._smart_policy_aliases = {}
            self._smart_visible_batch = None
        for event in self._refresh.values():
            event.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)

    def snapshot(self):
        with self._lock:
            state = copy.deepcopy(self._state)
        # A stopped/slow sampler cannot keep a manual SMART temperature or
        # health verdict fresh. This projection reads only sysfs/cache.
        current_disks = []
        for disk in state.get('disks', {}).get('items', []):
            name = disk.get('id') if isinstance(disk, dict) else None
            if isinstance(name, str) and re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)', name):
                # The row's model/names were collected for
                # this exact lifetime. Never rebind those old values to a new
                # disk just because it reuses the same /dev name.
                lifetime = disk.pop('_lifecycle', None)
                if lifetime is None or self._smart_context(name) != lifetime:
                    continue
                smart = self._smart(name, lifetime)
                for key in ('health', 'smart_available', 'standby', 'smart_temperature_c',
                            'smart_sampled_monotonic', 'smart_sampled_at', 'smart_status',
                            'smart_fresh', 'temperature_critical_c'):
                    disk[key] = copy.deepcopy(smart.get(key))
                disk['stable_identity'] = self._disk_identity(name, self.sys_root / 'block' / name,
                                                             smart.get('smart_identity'))
                disk['temp_c'] = smart.get('temp_c')
                disk['temperature_source'] = 'smart'
                if self._smart_context(name) != lifetime:
                    continue
            else:
                continue
            current_disks.append(disk)
        if isinstance(state.get('disks'), dict):
            state['disks']['items'] = current_disks
        # Local inventory may read sysfs serials, but its shared snapshot serves
        # public v1, the small screen and machine integrations as well as Web.
        for device in state.get("device", {}).get("storage", {}).get("devices", []):
            if isinstance(device, dict) and "serial" in device:
                device["serial"] = None
        return state

    def _fast_loop(self):
        while not self._stop.is_set():
            try:
                self._collect_fast()
            except (OSError, ValueError, TypeError, OverflowError):
                # A disappearing proc/sys entry must not stop the sampler.
                pass
            self._stop.wait(self.sample_interval)

    def _integrations(self):
        return (("disks", self._disks), ("containers", self._containers), ("vms", self._vms),
                ("device", lambda: collect_device_info(self.proc_root, self.sys_root, self.etc_root, self.run_root)))

    def _slow_loop(self, name, collect):
        while not self._stop.is_set():
            self._publish_integration(name, collect)
            self._refresh[name].wait(self.slow_interval)
            self._refresh[name].clear()

    def _run(self, program, arguments, timeout=3):
        executable = self.command_finder(program)
        if not executable:
            return None, "未安装 " + program
        if program == "docker":
            arguments = ["--host", DOCKER_LOCAL_HOST, *arguments]
        try:
            result = self.runner([executable, *arguments], timeout=timeout)
            output = result.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            return (result.returncode, str(output or "")), None
        except subprocess.TimeoutExpired:
            return None, program + " 响应超时"
        except (OSError, ValueError, TypeError):
            return None, program + " 不可访问"

    def _collect_fast(self):
        now = self.clock()
        stamp = _timestamp()
        ticks = _cpu_ticks(self.proc_root)
        cpu_usage = _percent_delta(self._previous_cpu.get("cpu"), ticks.get("cpu"))
        cores = [{"id": int(name[3:]), "usage_percent": _percent_delta(self._previous_cpu.get(name), value)}
                 for name, value in ticks.items() if name != "cpu"]
        self._previous_cpu = ticks
        cpu_info = _text(self.proc_root / "cpuinfo") or ""
        cpu_model = None
        for line in cpu_info.splitlines():
            if ":" in line and line.split(":", 1)[0].strip() in ("model name", "Hardware", "Processor"):
                cpu_model = line.split(":", 1)[1].strip() or None
                break
        load, task_count = None, None
        try:
            fields = (_text(self.proc_root / "loadavg", 256) or "").split()
            values = [float(value) for value in fields[:3]]
            if len(values) == 3 and all(_finite(value) is not None for value in values):
                load = values
            if len(fields) > 3:
                task_count = int(fields[3].split("/", 1)[1])
        except (ValueError, IndexError):
            pass
        uptime = None
        try:
            uptime = _finite(float((_text(self.proc_root / "uptime", 256) or "").split()[0]))
        except (ValueError, IndexError):
            pass
        vendor = (_text(self.sys_root / "class/dmi/id/sys_vendor", 512) or "").strip()
        product = (_text(self.sys_root / "class/dmi/id/product_name", 512) or "").strip()
        memory = _memory(self.proc_root)
        interfaces = self._interfaces(now)
        processes = self._processes(now)
        process_count = None
        try:
            # /proc/loadavg counts runnable/total tasks, including threads;
            # numeric proc directories are the actual process count.
            process_count = sum(1 for path in self.proc_root.iterdir() if path.name.isdigit())
        except OSError:
            pass
        primary = self._primary_interface()
        main_net = next((entry for entry in interfaces if entry["name"] == primary), {})
        history_entry = {"timestamp": stamp, "cpu_usage_percent": cpu_usage,
                         "memory_usage_percent": memory["usage_percent"],
                         "rx_bytes_per_sec": main_net.get("rx_bytes_per_sec"),
                         "tx_bytes_per_sec": main_net.get("tx_bytes_per_sec")}
        system = {"model": " ".join(filter(None, (vendor, product))) or None,
                  "cpu_model": cpu_model, "cpu_logical_cores": len(cores) or None,
                  "load_average": load, "cpu_usage_percent": cpu_usage,
                  "cpu_cores": sorted(cores, key=lambda entry: entry["id"]),
                  "process_count": process_count, "task_count": task_count,
                  "uptime_seconds": uptime, "memory": memory}
        ports = self._ports()
        gpu = self._gpu()
        with self._lock:
            self._history.append(history_entry)
            self._state.update(timestamp=stamp, system=system, interfaces=interfaces,
                               processes=processes, ports=ports, gpu=gpu, history=list(self._history))

    def _primary_interface(self):
        candidates = []
        for line in (_text(self.proc_root / "net/route") or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 8 or parts[1] != "00000000" or parts[7] != "00000000":
                continue
            try:
                if int(parts[3], 16) & 1:
                    candidates.append((int(parts[6]), parts[0]))
            except ValueError:
                pass
        return min(candidates)[1] if candidates else None

    def _interfaces(self, now):
        try:
            addresses = self.ipv4_provider()
        except (OSError, ValueError, TypeError):
            addresses = {}
        result, previous = [], {}
        for path in sorted((self.sys_root / "class/net").glob("*"))[:128]:
            if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", path.name):
                continue
            rx, tx = _integer(path / "statistics/rx_bytes"), _integer(path / "statistics/tx_bytes")
            entry = {"name": path.name, "ipv4": [],
                     "mac": (_text(path / "address", 128) or "").strip() or None,
                     "operstate": (_text(path / "operstate", 128) or "").strip() or None,
                     "rx_bytes": rx, "tx_bytes": tx, "rx_bytes_per_sec": None, "tx_bytes_per_sec": None}
            for value in addresses.get(path.name, []) if isinstance(addresses, dict) else []:
                try:
                    parsed = str(ipaddress.IPv4Address(value))
                    if parsed not in entry["ipv4"]:
                        entry["ipv4"].append(parsed)
                except (ValueError, TypeError):
                    pass
            old = self._previous_network.get(path.name)
            if rx is not None and tx is not None:
                if old and now > old[2] and rx >= old[0] and tx >= old[1]:
                    entry["rx_bytes_per_sec"] = (rx - old[0]) / (now - old[2])
                    entry["tx_bytes_per_sec"] = (tx - old[1]) / (now - old[2])
                previous[path.name] = (rx, tx, now)
            result.append(entry)
        self._previous_network = previous
        return result

    def _processes(self, now):
        rows, previous = [], {}
        try:
            paths = sorted((path for path in self.proc_root.iterdir() if path.name.isdigit()), key=lambda path: int(path.name))[:4096]
        except OSError:
            paths = []
        for path in paths:
            text = _text(path / "stat", 8192)
            if not text or "(" not in text or ")" not in text:
                continue
            start, end = text.find("("), text.rfind(")")
            parts = text[end + 1:].split()
            try:
                if len(parts) < 22:
                    continue
                ticks = int(parts[11]) + int(parts[12])
                identity = (int(path.name), int(parts[19]))
                resident = int(parts[21])
                if min(ticks, resident, identity[1]) < 0:
                    continue
                cpu = None
                old = self._previous_process.get(identity)
                if old and now > old[1] and ticks >= old[0]:
                    cpu = (ticks - old[0]) / self.clock_ticks / (now - old[1]) * 100
                previous[identity] = (ticks, now)
                rows.append({"pid": identity[0], "name": text[start + 1:end][:128],
                             "cpu_percent": cpu, "memory_bytes": resident * self.page_size})
            except (ValueError, IndexError):
                continue
        self._previous_process = previous
        return sorted(rows, key=lambda row: (row["cpu_percent"] or 0, row["memory_bytes"]), reverse=True)[:12]

    def disk_activity_diagnostics(self, *, history_directory=None):
        """Explicit proc/sys counter sample; never open a disk or a user file.

        Deltas require two calls in the same boot and device/process lifetime.
        Process accounting is not per-disk attribution or proof of spindle state.
        No command lines, fd targets, container environment or file names leave
        this collector. Sampling itself neither changes power policy nor SMART.
        """
        with self._disk_activity_lock:
            deadline = time.monotonic() + 3
            now = self.clock()
            previous = self._disk_activity_previous
            partial = []

            def read(path, limit):
                try:
                    with path.open('r', encoding='utf-8', errors='strict') as stream:
                        value = stream.read(limit + 1)
                    return value if len(value) <= limit else None
                except (OSError, ValueError, UnicodeError):
                    return None

            boot = (read(self.proc_root / 'sys/kernel/random/boot_id', 128) or '').strip()
            if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', boot):
                boot = None
                partial.append('boot_identity_unavailable')
            interval = (now - previous['monotonic'] if previous and boot is not None
                        and boot == previous['boot'] else None)
            if interval is None or not 0 < interval <= 300:
                interval, previous = None, None

            def delta(current, old, identity):
                if previous is None or old is None or identity is None or old['identity'] != identity:
                    return None
                before = old['counters']
                if set(current) != set(before) or any(current[key] < before[key] for key in current):
                    return None
                return {key: current[key] - before[key] for key in current}

            disks, disk_state, seen, lifetimes = [], {}, set(), {}
            try:
                with os.scandir(self.sys_root / 'block') as entries:
                    for entry in entries:
                        if len(lifetimes) == 128:
                            partial.append('disk_limit')
                            break
                        lifetimes[entry.name] = self._disk_generation(self.sys_root / 'block' / entry.name)
            except OSError:
                partial.append('disk_identity_unavailable')
            raw = read(self.proc_root / 'diskstats', 262144)
            if raw is None:
                partial.append('diskstats_unavailable')
            for line in (raw or '').splitlines():
                fields = line.split()
                if len(fields) < 3 or not re.fullmatch(r'(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)', fields[2]):
                    continue
                name = fields[2]
                if name in seen:
                    partial.append('duplicate_disk')
                    disks = [row for row in disks if row['name'] != name]
                    disk_state.pop(name, None)
                    continue
                seen.add(name)
                if len(disks) >= 128:
                    partial.append('disk_limit')
                    break
                try:
                    if len(fields) < 14 or any(not re.fullmatch(r'[0-9]{1,20}', v) or int(v) > 2**64 - 1 for v in fields[:2] + fields[3:]):
                        raise ValueError('invalid diskstats')
                    counters = {'read_ops': int(fields[3]), 'write_ops': int(fields[7]),
                                'read_bytes': int(fields[5]) * 512, 'write_bytes': int(fields[9]) * 512}
                    if len(fields) >= 18:
                        counters['discard_ops'] = int(fields[14])
                    if len(fields) >= 20:
                        counters['flush_ops'] = int(fields[18])
                    folder = self.sys_root / 'block' / name
                    generation = self._disk_generation(folder)
                    dev = ':'.join(fields[:2])
                    identity = ((dev, generation) if generation and generation == lifetimes.get(name)
                                and (read(folder / 'dev', 64) or '').strip() == dev else None)
                    if identity is None:
                        partial.append('disk_identity_unverified')
                    old = previous['disks'].get(name) if previous else None
                    change = delta(counters, old, identity)
                    disk_state[name] = {'identity': identity, 'counters': counters}
                    disks.append({'name': name, 'device_number': dev, 'counters': counters,
                                  'delta': change, 'in_flight': int(fields[11]),
                                  'activity_observed': (True if int(fields[11]) > 0 else
                                                        any(value > 0 for value in change.values())
                                                        if change is not None else None)})
                except (ValueError, OverflowError):
                    partial.append('invalid_diskstats')

            def process_stat(folder):
                text = read(folder / 'stat', 8192)
                if not text or '(' not in text or ')' not in text:
                    return None
                start, end = text.find('('), text.rfind(')')
                if text[:start].strip() != folder.name:
                    return None
                fields = text[end + 1:].split()
                try:
                    ticks = int(fields[19])
                    comm = text[start + 1:end]
                    if not 0 <= ticks <= 2**64 - 1 or len(comm) > 128 or not comm or not all(c.isprintable() for c in comm):
                        return None
                    return ticks, comm
                except (ValueError, IndexError):
                    return None

            processes, process_state = [], {}
            try:
                folders = []
                with os.scandir(self.proc_root) as entries:
                    for entry in entries:
                        if entry.name.isascii() and entry.name.isdecimal():
                            if len(folders) == 4096:
                                partial.append('process_limit')
                                break
                            folders.append(self.proc_root / entry.name)
            except OSError:
                folders = []
                partial.append('process_list_unavailable')
            unreadable, changed = 0, 0
            for folder in folders:
                if time.monotonic() >= deadline:
                    partial.append('time_limit')
                    break
                before = process_stat(folder)
                if before is None:
                    unreadable += 1
                    continue
                text = read(folder / 'io', 4096)
                if text is None:
                    unreadable += 1
                    continue
                counters = {}
                for line in text.splitlines():
                    key, separator, value = line.partition(':')
                    if key in ('read_bytes', 'write_bytes', 'cancelled_write_bytes') and separator:
                        if key in counters or not re.fullmatch(r'\s*[0-9]{1,20}\s*', value) or int(value) > 2**64 - 1:
                            counters = {}
                            break
                        counters[key] = int(value)
                after = process_stat(folder)
                if after != before:
                    changed += 1
                    continue
                if len(counters) != 3:
                    unreadable += 1
                    continue
                pid = int(folder.name)
                identity = (pid, before[0], before[1])
                old = previous['processes'].get(pid) if previous else None
                change = delta(counters, old, identity)
                process_state[pid] = {'identity': identity, 'counters': counters}
                processes.append({'pid': pid, 'name': before[1], 'start_ticks': before[0],
                                  'toolbox_process': pid == os.getpid(), 'counters': counters,
                                  'delta': change})
            if unreadable:
                partial.append('process_io_unavailable')
            if changed:
                partial.append('process_changed')
            processes.sort(key=lambda row: sum((row['delta'] or {}).get(key, 0)
                                               for key in ('read_bytes', 'write_bytes')), reverse=True)
            history_storage = {'status': 'unverified', 'device_number': None,
                               'source_device': None, 'filesystem': None}
            if isinstance(history_directory, (str, Path)) and str(history_directory).startswith('/'):
                # The installer supplies the canonical directory. Match only
                # mount metadata; do not resolve/stat/open this storage path.
                directory = str(history_directory).rstrip('/') or '/'
                mountinfo = read(self.proc_root / 'self/mountinfo', 262144)
                matches = []
                for line in (mountinfo or '').splitlines():
                    before, separator, after = line.partition(' - ')
                    fields, tail = before.split(), after.split()
                    if not separator or len(fields) < 6 or len(tail) < 2:
                        continue
                    mount = re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), fields[4])
                    if mount == '/' or directory == mount or directory.startswith(mount + '/'):
                        matches.append((len(mount), fields, tail))
                if matches:
                    matches.sort(key=lambda item: item[0], reverse=True)
                    if len(matches) == 1 or matches[0][0] > matches[1][0]:
                        _, fields, tail = matches[0]
                        source = tail[1] if re.fullmatch(r'/dev/(?:sd[a-z]+[0-9]*|vd[a-z]+[0-9]*|xvd[a-z]+[0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?|mmcblk[0-9]+(?:p[0-9]+)?)', tail[1]) else None
                        if re.fullmatch(r'[0-9]+:[0-9]+', fields[2]) and re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', tail[0]):
                            history_storage = {'status': 'mount_metadata_only', 'device_number': fields[2],
                                               'source_device': source, 'filesystem': tail[0]}
            self._disk_activity_previous = {'monotonic': now, 'boot': boot,
                                           'disks': copy.deepcopy(disk_state),
                                           'processes': copy.deepcopy(process_state)}
            return {'available': bool(disks), 'status': 'sampled' if interval is not None else 'baseline',
                    'sampled_at': _timestamp(), 'interval_seconds': interval,
                    'source': 'procfs_sysfs_counters', 'complete': not partial,
                    'partial_reasons': sorted(set(partial)), 'disks': disks,
                    'processes': processes[:32], 'processes_observed': len(process_state),
                    'processes_omitted': max(0, len(processes) - 32),
                    'unreadable_processes': unreadable, 'changed_processes': changed,
                    'history_storage': history_storage,
                    'attribution': 'process_totals_not_per_disk', 'power_state': None,
                    'reason': '相隔至少五秒再次采样可比较增量；零 I/O 不证明休眠，进程总计不能直接归因到某块盘。'}

    def _ports(self):
        rows = []
        for protocol, filename in (("tcp", "tcp"), ("tcp6", "tcp6")):
            for line in (_text(self.proc_root / "net" / filename) or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) < 10 or parts[3] != "0A":
                    continue
                try:
                    address, raw_port = parts[1].split(":", 1)
                    if protocol == "tcp":
                        packed = bytes.fromhex(address)[::-1]
                        display = socket.inet_ntop(socket.AF_INET, packed)
                    else:
                        packed = bytes.fromhex(address)
                        packed = b"".join(packed[index:index + 4][::-1] for index in range(0, 16, 4))
                        display = socket.inet_ntop(socket.AF_INET6, packed)
                    port = int(raw_port, 16)
                    inode = int(parts[9])
                    if not 0 <= port <= 65535 or inode < 0:
                        continue
                    rows.append({"protocol": protocol, "address": display, "port": port, "inode": inode})
                except (ValueError, OSError, IndexError):
                    pass
        return sorted(rows, key=lambda row: (row["port"], row["protocol"], row["address"]))[:256]

    def _gpu(self):
        devices = []
        for card in sorted((self.sys_root / "class/drm").glob("card*"))[:32]:
            if not re.fullmatch(r"card\d+", card.name) or not (card / "device").is_dir():
                continue
            path = card / "device"
            devices.append({"name": card.name,
                            "vendor": (_text(path / "vendor", 128) or "").strip() or None,
                            "device": (_text(path / "device", 128) or "").strip() or None,
                            "busy_percent": _finite(_integer(path / "gpu_busy_percent"), maximum=100),
                            "vram_total_bytes": _integer(path / "mem_info_vram_total"),
                            "vram_used_bytes": _integer(path / "mem_info_vram_used")})
        return {"available": bool(devices), "reason": None if devices else "未发现支持的 GPU sysfs 设备", "devices": devices}

    def _collect_slow(self):
        # Publish each integration independently; a slow/missing hypervisor does
        # not hide completed disk/container probes.
        for name, collect in self._integrations():
            if self._stop.is_set():
                return
            self._publish_integration(name, collect)

    def _publish_integration(self, name, collect):
        try:
            result = collect()
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            result = _capability("采集暂不可用，正在自动重试")
        result["sampled_at"] = _timestamp()
        with self._lock:
            self._state[name] = result

    def _disk_identity(self, name, folder, smart_identity=None):
        """Stable identity; never hash a transient /dev/sdX or hwmon number."""
        def digest(kind, value):
            return kind + ':' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]
        for kind, paths in (
            ('wwn', (folder / 'wwid', folder / 'device/wwid')),
            ('serial', (folder / 'device/serial', folder / 'serial')),
        ):
            for path in paths:
                value = (_text(path, 512) or '').strip()
                if value:
                    return digest(kind, value)
        if name.startswith('nvme'):
            controller = re.match(r'nvme\d+', name)[0]
            serial = (_text(self.sys_root / 'class/nvme' / controller / 'serial', 512) or '').strip()
            if serial:
                return digest('serial', serial)
        if smart_identity:
            return smart_identity
        try:
            relative = folder.resolve().relative_to(self.sys_root.resolve())
            parts = [part for part in relative.parts if part != 'block' and part != 'hwmon'
                     and not re.fullmatch(r'(?:sd[a-z]+|nvme\d+(?:n\d+)?|mmcblk\d+|vd[a-z]+|xvd[a-z]+|hwmon\d+)', part)]
            if parts and parts[0] == 'devices':
                return digest('physical', '/'.join(parts))
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    @staticmethod
    def _unknown_smart():
        return {"smart_available": False, "standby": None, "temp_c": None,
                  "health": {"passed": None, "reason": None},
                  "smart_temperature_c": None, "smart_sampled_monotonic": None,
                  "smart_status": "unavailable", "smart_identity": None,
                  "temperature_critical_c": None}

    def _smart_context(self, name):
        if type(name) is not str or re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)', name) is None:
            return None
        folder = self.sys_root / 'block' / name
        generation = self._disk_generation(folder)
        dev = (_text(folder / 'dev', 32) or '').strip()
        if generation is None or re.fullmatch(r'[0-9]{1,6}:[0-9]{1,6}', dev) is None:
            return None
        identity = self._disk_identity(name, folder)
        if (self._disk_generation(folder) != generation
                or (_text(folder / 'dev', 32) or '').strip() != dev
                or self._disk_identity(name, folder) != identity):
            return None
        return (identity, generation, dev)

    def _smart(self, name, identity=None):
        """Project existing current-lifetime measurements; never initiate a probe."""
        with self._lock:
            manual = copy.deepcopy(self._smart_cache.get(name))
            automatic = copy.deepcopy(self._smart_measurements.get(name))
            visible = copy.deepcopy(self._smart_visible_observations.get(name))
        records = [row for row in (manual, automatic) if row and len(row) == 3 and row[2] == identity]
        fresh = [row for row in records if self._smart_record_fresh(row, identity)]
        cached = (manual if manual in fresh else max(fresh or records,
                  key=lambda row: _finite(row[0]) if _finite(row[0]) is not None else -math.inf, default=None))
        if (not cached or len(cached) != 3 or identity is None or cached[2] != identity
                or self._smart_context(name) != identity):
            result = self._unknown_smart()
            result['health']['reason'] = '尚无有效的 SMART 观测缓存'
            return self._visible_smart_projection(name, result, visible, identity)
        result = cached[1]
        if manual and len(manual) == 3 and manual[2] == identity and cached is not manual:
            # An already owner-authorized identity may survive an expired
            # measurement. Automatic reads never introduce a new serial ID.
            result['smart_identity'] = manual[1].get('smart_identity')
        sampled = _finite(cached[0])
        fresh = sampled is not None and 0 <= self.clock() - sampled < SMART_CACHE_SECONDS
        result['smart_fresh'] = fresh
        result['fresh'] = fresh
        if not fresh:
            # History remains dated, but stale health, standby and temperatures
            # cannot masquerade as current measurements or drive fan policy.
            result.update(standby=None, temp_c=None, smart_temperature_c=None,
                          smart_status='stale', temperature_critical_c=None,
                          health={'passed': None, 'reason': 'SMART 观测缓存已过期'})
            # Attribute rows contain measurements too. Keep raw data only in
            # the private identity cache, never in a stale public detail view.
            result.pop('smart_data', None)
        return self._visible_smart_projection(name, result, visible, identity)

    def _visible_smart_projection(self, name, result, visible, identity):
        """A later explicit failure cannot leave an old fresh temperature live."""
        with self._lock:
            visible = copy.deepcopy(self._smart_visible_observations.get(name))
        if not visible or identity is None or visible['identity'] != identity:
            return result
        if self._smart_context(name) != identity:
            return self._unknown_smart()
        value = visible['value']
        if value is not None:
            result.update(copy.deepcopy(value))
            stamp = _finite(value.get('smart_sampled_monotonic'))
            fresh = stamp is not None and 0 <= self.clock() - stamp < SMART_CACHE_SECONDS
            result.update(smart_fresh=fresh, fresh=fresh)
            with self._lock:
                unchanged = self._smart_visible_observations.get(name) == visible
            if fresh and unchanged and self._smart_context(name) == identity:
                with self._lock:
                    unchanged = self._smart_visible_observations.get(name) == visible
            else:
                unchanged = False
            if unchanged:
                result.pop('smart_data', None)
                return result
        # Keep the original sample date on a failed attempt. Its status and
        # readings are unknown now; an attempt date is not a new measurement.
        result.update(smart_available=False, standby=None, temp_c=None, smart_temperature_c=None,
                      temperature_critical_c=None, smart_fresh=False, fresh=False,
                      smart_status='unavailable' if value is None else 'stale',
                      health={'passed': None, 'reason': '本次 SMART 刷新未取得有效测量'})
        if value is None:
            result.update(visible.get('history', {}))
        result.pop('smart_data', None)
        return result

    def _probe_smart(self, name, *, force=False, timeout=3, measurement_only=False, require_power_state=False):
        """The sole SMART command path, behind the shared batch grant and lock."""
        result = self._unknown_smart()
        arguments = ([] if force else ['-n', 'standby']) + ['-j', '-a', '/dev/' + name]
        command, error = self._run('smartctl', arguments, timeout=timeout)
        status, reason = 'failed', 'command_failed'
        if error:
            result["health"]["reason"] = error
            status = 'unavailable' if error.startswith('未安装') else 'failed'
            reason = 'command_unavailable' if status == 'unavailable' else 'command_timeout' if '超时' in error else 'command_failed'
        else:
            code, output = command
            try:
                data = json.loads(output)
                if not isinstance(data, dict):
                    raise ValueError("invalid SMART response")
                # Keep the complete smartctl JSON in the cache for the on-demand
                # disk detail endpoint.  It is deliberately not copied into the
                # frequent overview snapshot; smart() reads this cached copy and
                # never launches another probe.
                if not measurement_only:
                    result["smart_data"] = copy.deepcopy(data)
                messages = data.get("smartctl", {}).get("messages", [])
                notes = " ".join(str(entry.get("string", "")) for entry in messages if isinstance(entry, dict))
                power, invalid_power = _smart_power_mode(data.get("power_mode", {}))
                standby = bool(power in _SMART_STANDBY
                               or re.search(r'\b(?:device|disk)\s+is\s+in\s+(?:standby|sleep)\s+mode\b', notes, re.I))
                unsafe_power = not force and bool(invalid_power or re.search(
                    r'\b(?:unknown|unsupported|not[\s_-]+supported|not[\s_-]+implemented|failed|unavailable)\b', power or '', re.I) or re.search(
                    r'(?:failed|unable|unsupported|unknown|not[\s_-]+supported|not[\s_-]+implemented).*?(?:power|standby|sleep)'
                    r'|(?:power|standby|sleep).*?(?:unknown|unsupported|not[\s_-]+supported|not[\s_-]+implemented|cannot|failed|ignoring|ignored)'
                    r'|\bignor(?:ing|ed)\s+(?:the\s+)?-n\b',
                    notes + ' ' + (power or ''), re.I))
                result["standby"] = True if standby else None
                if standby:
                    status, reason = 'skipped_standby', 'standby'
                    result["smart_status"] = "standby"
                    result["health"]["reason"] = "磁盘处于休眠，已跳过 SMART 读取"
                else:
                    smart = data.get("smart_status", {})
                    nvme = data.get("nvme_smart_health_information_log", {})
                    passed = smart.get("passed") if isinstance(smart, dict) else None
                    if not isinstance(passed, bool) and isinstance(nvme, dict):
                        warning = nvme.get("critical_warning")
                        if isinstance(warning, int) and not isinstance(warning, bool):
                            passed = warning == 0
                    result["smart_available"] = isinstance(passed, bool) or bool(nvme) or isinstance(data.get("ata_smart_attributes"), dict)
                    result["health"] = {"passed": passed if isinstance(passed, bool) else None,
                                        "reason": None if isinstance(passed, bool) else "设备未返回 SMART 健康结论" if code == 0 else "SMART 不支持或读取失败"}
                    temperature = data.get("temperature", {})
                    if isinstance(temperature, dict):
                        result["temp_c"] = _finite(temperature.get("current"), maximum=200)
                    if result["temp_c"] is None and isinstance(nvme, dict):
                        result["temp_c"] = _finite(nvme.get("temperature"), maximum=200)
                    # The control catalog uses bounded physical readings and
                    # this probe's time, independently of the legacy UI field.
                    if code & 7 == 0:
                        status, reason = 'refreshed', None
                        candidate = temperature.get('current') if isinstance(temperature, dict) else None
                        measured = _finite(candidate, minimum=-40, maximum=150)
                        if measured is None and isinstance(nvme, dict):
                            measured = _finite(nvme.get('temperature'), minimum=-40, maximum=150)
                        result['smart_temperature_c'] = measured
                        if measured is not None:
                            result['smart_status'] = 'ok'
                    critical = temperature.get('op_limit_max') if isinstance(temperature, dict) else None
                    result['temperature_critical_c'] = _finite(critical, minimum=40, maximum=150)
                    if code & 7:
                        status, reason = 'unavailable', 'smart_unavailable'
                        result.update(smart_available=False, temp_c=None, smart_temperature_c=None,
                                      smart_status='unavailable')
                        result['health'] = {'passed': None, 'reason': 'SMART 不支持或读取失败'}
                    elif not result['smart_available'] and result['smart_temperature_c'] is None:
                        status, reason = 'unavailable', 'smart_unavailable'
                if not measurement_only:
                    serial = data.get('serial_number')
                    wwn = data.get('wwn')
                    if isinstance(wwn, dict) and all(type(wwn.get(key)) is int for key in ('naa', 'oui', 'id')):
                        raw = json.dumps({key: wwn[key] for key in ('naa', 'oui', 'id')}, sort_keys=True)
                        result['smart_identity'] = 'wwn:' + hashlib.sha256(raw.encode()).hexdigest()[:24]
                    elif isinstance(serial, str) and 0 < len(serial.strip()) <= 512:
                        result['smart_identity'] = 'serial:' + hashlib.sha256(serial.strip().encode()).hexdigest()[:24]
                if unsafe_power:
                    status, reason = 'unavailable', 'power_mode_unverified'
                    result = self._unknown_smart()
                if require_power_state and not standby:
                    if power in _SMART_NON_STANDBY and status == 'refreshed':
                        # This explicit parser proof, never bool(None), is the
                        # ordinary batch's non-standby capacity prerequisite.
                        result['standby'] = False
            except (ValueError, TypeError, AttributeError):
                result["health"]["reason"] = "SMART 返回了无效数据"
                status, reason = 'failed', 'invalid_response'
        result['smart_sampled_monotonic'] = self.clock()
        result['smart_sampled_at'] = time.time()
        return result, status, reason

    def smart_cached(self, name):
        """Return dated manual cache for the current device lifetime, without probing."""
        if type(name) is not str or re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+)', name) is None:
            return None
        identity = self._smart_context(name)
        if identity is None:
            return None
        with self._lock:
            valid = any(cached and len(cached) == 3 and cached[2] == identity for cached in (
                self._smart_cache.get(name), self._smart_measurements.get(name)))
        return self._smart(name, identity) if valid else None

    def refresh_smart(self, force=False, authorizer=None):
        """Owner-explicit full refresh; force applies to this batch only."""
        return self._smart_batch(force, authorizer)

    def fill_smart_missing(self, authorizer=None):
        """Owner-explicit fixed fill: ordinary standby-safe reads on cache misses."""
        return self._smart_batch(False, authorizer, missing_only=True)

    def refresh_smart_visible(self, *, authorizer=None):
        """Reread ordinary SMART even with positive cache; no force/raw/serial."""
        return self._smart_batch(False, authorizer, missing_only=True, visible=True)

    def capacity_visible_eligibility(self, receipt):
        """Return a private grant bound to this exact returned visible batch.

        Copies, prior receipts, later attempts and stop/start cannot renew it.
        The callback is an extra denial gate, not a mount/device authority.
        """
        with self._lock:
            batch = self._smart_visible_batch
            captured = batch if batch is not None and batch['receipt'] is receipt else None

        def eligible(name):
            if captured is None:
                return 'power_state_unverified'
            with self._lock:
                current = self._smart_visible_batch
                proof = copy.deepcopy(captured['proofs'].get(name))
                attempt = copy.deepcopy(self._smart_attempts.get(name))
                observation = copy.deepcopy(self._smart_visible_observations.get(name))
                valid = (current is captured and captured['epoch'] == self._smart_epoch and not self._stop.is_set())
            if not valid or not proof or not attempt or not observation:
                return 'power_state_unverified'
            identity = self._smart_context(name)
            if (identity is None or proof['identity'] != identity or attempt['identity'] != identity
                    or observation['identity'] != identity or observation['batch'] != captured['sequence']
                    or attempt.get('sequence') != proof['sequence']
                    or attempt['stamp'] != proof['stamp']
                    or not 0 <= self.clock() - proof['stamp'] < SMART_CACHE_SECONDS):
                return 'power_state_unverified'
            reason = proof['reason']
            with self._lock:
                unchanged = (self._smart_visible_batch is captured and self._smart_epoch == captured['epoch']
                             and self._smart_attempts.get(name) == attempt
                             and self._smart_visible_observations.get(name) == observation)
            if not unchanged or self._stop.is_set() or self._smart_context(name) != identity:
                return 'power_state_unverified'
            return reason
        return eligible

    def _smart_record_fresh(self, record, identity):
        return bool(record and len(record) == 3 and record[2] == identity
                    and _finite(record[0]) is not None and 0 <= self.clock() - record[0] < SMART_CACHE_SECONDS)

    def capacity_fill_eligibility(self, device_name):
        """Only a recent ordinary SMART success proves a non-standby fill grant.

        A forced refresh is not evidence that the default standby guard worked.
        This check is metadata/cache only and never performs a SMART command.
        """
        identity = self._smart_context(device_name)
        if identity is None:
            return 'power_state_unverified'
        value = self._smart(device_name, identity)
        if (value.get('smart_fresh') is True and value.get('standby') is True
                and self._smart_context(device_name) == identity):
            return 'standby'
        with self._lock:
            attempt = copy.deepcopy(self._smart_attempts.get(device_name))
        if (attempt and attempt['identity'] == identity
                and 0 <= self.clock() - attempt['stamp'] < SMART_CACHE_SECONDS
                and attempt['row']['status'] == 'refreshed' and attempt['nonstandby'] is True
                and self._smart_context(device_name) == identity):
            return None
        return 'power_state_unverified'

    def request_policy_smart(self, demand):
        """Schedule one bounded measurement-only batch for a live policy token.

        The fan loop never waits for a command or for another SMART batch. The
        token belongs to FanPolicyController and rechecks applied policy/lease,
        rather than borrowing an owner Web authorization callback.
        """
        if not demand.authorized():
            return False
        with self._lock:
            if self._stop.is_set() or self._smart_policy_worker and self._smart_policy_worker.is_alive():
                return False
            epoch = self._smart_epoch
            worker = threading.Thread(target=self._policy_smart_batch, args=(demand, epoch),
                                      daemon=True, name='r1-fan-smart')
            self._smart_policy_worker = worker
            try:
                worker.start()
            except RuntimeError:
                self._smart_policy_worker = None
                return False
        return True

    def _policy_smart_batch(self, demand, epoch):
        with self._lock:
            if self._stop.is_set() or self._smart_epoch != epoch:
                return
        try:
            self._smart_batch(False, demand.authorized, missing_only=True, policy_demand=demand,
                              collector_epoch=epoch)
        except SmartRefreshError:
            # Busy/revoked/stopped are not retry loops. A subsequent live fan
            # tick may schedule a new token, still subject to cache freshness.
            pass

    def _policy_disk_names(self, demand):
        """Resolve selected stable source IDs only from current inventory rows."""
        bindings, _ = self._policy_disk_bindings(demand)
        return list(dict.fromkeys(bindings.values()))

    def _policy_alias_inventory_complete(self, rows):
        """Alias uniqueness needs the entire fixed, bounded kernel inventory.

        Global inventory intentionally caps its published rows. An alias must
        not mistake that cap (or an omitted/unreadable row) for a complete scan.
        Only fixed sysfs directory metadata is inspected; no SMART is run.
        """
        pattern = r'(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)'
        published = [row.get('id') for row in rows if isinstance(row, dict)]
        if (len(published) != len(rows) or len(published) > 128
                or any(type(name) is not str or re.fullmatch(pattern, name) is None for name in published)
                or len(set(published)) != len(published)):
            return False
        names = set()
        try:
            with os.scandir(self.sys_root / 'block') as entries:
                for index, entry in enumerate(entries):
                    if index >= 256:  # Also bound irrelevant kernel entries.
                        return False
                    if re.fullmatch(pattern, entry.name):
                        names.add(entry.name)
                        if len(names) >= 129:
                            return False
        except OSError:
            return False
        return names == set(published)

    def _policy_disk_bindings(self, demand):
        """Unique current disk bindings, including policy-only old serial IDs.

        A cold process may have no owner SMART serial cache. Existing consistent
        sysfs/udev serial metadata can prove the selected serial hash without
        promoting it to a global stable ID or probing disks to find a match.
        """
        with self._lock:
            rows = copy.deepcopy(self._state.get('disks', {}).get('items', []))
        # The temperature catalog marks duplicate stable IDs unavailable. Do
        # not turn that ambiguous source into permission to probe both disks,
        # or pick one merely because another row failed its lifetime check.
        candidates, complete = {}, len(rows) <= 128
        selected_serials = {source for source in demand.sources if source.startswith('disk:serial:')}
        if selected_serials:
            complete = complete and self._policy_alias_inventory_complete(rows)
        for row in rows[:128]:
            if not isinstance(row, dict):
                complete = False
                continue
            name, identity = row.get('id'), row.get('_lifecycle')
            source = 'disk:' + str(row.get('stable_identity'))
            matches = {source: None} if source in demand.sources else {}
            if identity is not None and self._smart_context(name) == identity:
                current = self._smart(name, identity)
                stable = self._disk_identity(name, self.sys_root / 'block' / name, current.get('smart_identity'))
                if 'disk:' + str(stable) == source and self._smart_context(name) == identity:
                    if source in matches:
                        matches[source] = (name, identity, False)
                if selected_serials:
                    with self._lock:
                        manual = self._smart_cache.get(name)
                        known = manual[1] if manual and len(manual) == 3 and manual[2] == identity else None
                        prior = self._disk_serial_cache.get(name, {})
                        prior_serial = prior.get('serial') if prior.get('cache_key') == identity else None
                    evidence = self._disk_serial_info(name, self.sys_root / 'block' / name, known, prior_serial,
                                                      policy_sources=selected_serials)
                    valid = (evidence['policy_metadata_status'] == 'ok' and evidence['status'] == 'ok'
                             and self._smart_context(name) == identity)
                    # Conflicting metadata can neither restore an alias nor
                    # override an already owner-known serial identity.
                    if source in selected_serials and (evidence['policy_metadata_status'] != 'missing'
                                                       or evidence['status'] in ('conflict', 'invalid')):
                        matches[source] = None
                    for match in evidence['policy_matches']:
                        matches[match] = ((name, identity, match != 'disk:' + str(stable)) if valid else None)
            else:
                complete = False
            for match, value in matches.items():
                candidates.setdefault(match, []).append(value)
        bindings, aliases = {}, {}
        for source, values in candidates.items():
            if len(values) != 1 or values[0] is None:
                continue
            name, identity, alias = values[0]
            if alias and not complete:
                continue
            bindings[source] = (name, identity)
            if alias:
                aliases[source] = (name, identity)
        return bindings, aliases

    def policy_temperature_aliases(self):
        """Internal control-catalog projection; no public identity/cache mutation."""
        empty = {'by_device': {}, 'unavailable_sources': [], 'controlled_sources': [], 'items': []}
        with self._lock:
            record = self._smart_policy_aliases
        if not record:
            return empty
        demand = record['demand']
        selected = {source for source in demand.sources if source.startswith('disk:serial:')}
        unavailable = {'by_device': {}, 'unavailable_sources': sorted(selected),
                       'controlled_sources': sorted(selected), 'items': []}
        with self._lock, demand.publication_guard():
            if record is not self._smart_policy_aliases:
                return unavailable
            if (self._stop.is_set() or record['epoch'] != self._smart_epoch or not demand.authorized()):
                self._smart_policy_aliases = {}
                return empty
            bindings, current = self._policy_disk_bindings(demand)
            if (not demand.authorized() or self._stop.is_set() or record['epoch'] != self._smart_epoch):
                self._smart_policy_aliases = {}
                return empty
            blocked = selected - bindings.keys()
            result = {'by_device': {}, 'unavailable_sources': [], 'controlled_sources': sorted(selected), 'items': []}
            for source, pair in record['bindings'].items():
                if bindings.get(source) != pair:
                    blocked.add(source)
                elif current.get(source) == pair:
                    result['by_device'].setdefault(pair[0], []).append(source)
                # A normal base row may now provide the same source after an
                # owner refresh. Do not duplicate it with an alias row.
            inventory = {row['id']: row for row in self._state.get('disks', {}).get('items', [])[:128]
                         if isinstance(row, dict) and isinstance(row.get('id'), str)}
            for source in sorted(selected - blocked):
                pair = bindings.get(source)
                if pair is None:
                    continue
                name, identity = pair
                # A new alias requires this batch's binding grant, not merely
                # metadata that appeared after it finished.
                if source in current and record['bindings'].get(source) != pair:
                    blocked.add(source)
                    continue
                row = inventory.get(name)
                if row is None or row.get('_lifecycle') != identity or self._smart_context(name) != identity:
                    blocked.add(source)
                    continue
                smart = self._smart(name, identity)
                item = {key: copy.deepcopy(row.get(key)) for key in ('id', 'device', 'model', 'user_name')}
                item.update({key: copy.deepcopy(smart.get(key)) for key in (
                    'smart_temperature_c', 'smart_sampled_monotonic', 'smart_status', 'standby',
                    'temperature_critical_c')})
                item['policy_source_id'] = source
                if self._smart_context(name) != identity:
                    blocked.add(source)
                    continue
                result['items'].append(item)
            # Binding and measurement above come from one private lifecycle.
            # Never join this grant to a separately copied public sdX row.
            final_bindings, final_aliases = self._policy_disk_bindings(demand)
            if final_bindings != bindings or final_aliases != current:
                return unavailable
            if (not demand.authorized() or self._stop.is_set() or record['epoch'] != self._smart_epoch):
                self._smart_policy_aliases = {}
                return empty
            result['unavailable_sources'] = sorted(blocked)
            result['items'] = [item for item in result['items'] if item['policy_source_id'] not in blocked]
            return result

    def _smart_batch(self, force, authorizer, *, missing_only=False, policy_demand=None,
                     collector_epoch=None, visible=False):
        """Explicit bounded batch, fixed devices only; force is never persisted.

        All results/cache writes are private until the final grant and device
        lifetime checks. Revocation discards the entire batch, without retries.
        """
        if type(force) is not bool:
            raise SmartRefreshError('invalid_parameters')
        revoked = False
        with self._lock:
            epoch = self._smart_epoch if collector_epoch is None else collector_epoch

        def authorized():
            nonlocal revoked
            try:
                allowed = not revoked and callable(authorizer) and authorizer() is True
            except Exception:
                allowed = False
            if not allowed:
                revoked = True
                raise SmartRefreshError('authorization_denied')
            if self._stop.is_set() or self._smart_epoch != epoch:
                raise SmartRefreshError('stopping')

        authorized()
        if not self._smart_refresh_lock.acquire(blocking=False):
            raise SmartRefreshError('busy')
        started, deadline, pending, results = time.time(), self.clock() + SMART_REFRESH_TIMEOUT, {}, []
        attempts, identities, visible_values = {}, {}, {}
        try:
            authorized()
            with self._lock:
                self._smart_batch_sequence += 1
                sequence = self._smart_batch_sequence
                if visible:
                    self._smart_visible_batch = None
            bindings, aliases = self._policy_disk_bindings(policy_demand) if policy_demand is not None else ({}, {})
            targets = (list(dict.fromkeys(bindings.values())) if policy_demand is not None else
                       [(folder.name, None) for folder in sorted((self.sys_root / 'block').glob('*'))[:128]])
            for name, expected in targets:
                if re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)', name) is None:
                    continue
                authorized()
                row = {'device': name, 'status': 'unavailable', 'reason': None, 'sampled_at': None}
                results.append(row)
                identity = self._smart_context(name)
                if identity is None or expected is not None and expected != identity:
                    row['reason'] = 'identity_unverified'
                    continue
                identities[name] = identity
                if missing_only:
                    with self._lock:
                        cached = any(self._smart_record_fresh(record, identity) for record in (
                            self._smart_cache.get(name), self._smart_measurements.get(name)))
                        previous = copy.deepcopy(self._smart_attempts.get(name))
                    if cached and not visible:
                        value = self._smart(name, identity)
                        row.update(status='skipped_standby' if value.get('standby') is True else 'cached',
                                   reason='standby' if value.get('standby') is True else None,
                                   sampled_at=value.get('smart_sampled_at'))
                        continue
                    if (previous and previous['identity'] == identity
                            and 0 <= self.clock() - previous['stamp'] < SMART_CACHE_SECONDS
                            and (not visible or previous['row']['status'] not in ('refreshed', 'cached'))):
                        row.update(previous['row'])
                        if visible and row['status'] == 'skipped_standby':
                            value = self._unknown_smart()
                            value.update(standby=True, smart_status='standby',
                                         smart_sampled_monotonic=previous['stamp'], smart_sampled_at=row['sampled_at'])
                            visible_values[name] = value
                        continue
                if name.startswith('mmcblk'):
                    row['reason'] = 'smart_unsupported'
                    continue
                remaining = deadline - self.clock()
                if remaining <= 0:
                    row.update(status='failed', reason='timeout')
                    continue
                value, status, reason = self._probe_smart(name, force=force, timeout=min(3.0, remaining),
                                                        measurement_only=missing_only, require_power_state=not force)
                authorized()
                if self._smart_context(name) != identity:
                    row.update(status='identity_changed', reason='identity_changed')
                    continue
                if self.clock() >= deadline:
                    row.update(status='failed', reason='timeout')
                    continue
                row.update(status=status, reason=reason, sampled_at=value['smart_sampled_at'])
                if not force:
                    attempts[name] = {'identity': identity, 'stamp': value['smart_sampled_monotonic'],
                                      'row': copy.deepcopy(row),
                                      'nonstandby': status == 'refreshed' and value.get('standby') is False,
                                      'sequence': sequence}
                if status in ('refreshed', 'skipped_standby'):
                    if missing_only:
                        value = {key: copy.deepcopy(value.get(key)) for key in (
                            'smart_available', 'standby', 'temp_c', 'health', 'smart_temperature_c',
                            'smart_sampled_monotonic', 'smart_sampled_at', 'smart_status',
                            'temperature_critical_c')}
                    pending[name] = (value['smart_sampled_monotonic'], value, identity)
                    if visible:
                        visible_values[name] = copy.deepcopy(value)
            authorized()
            publication = policy_demand.publication_guard() if policy_demand is not None else nullcontext()
            with self._lock, publication:
                authorized()
                if policy_demand is not None:
                    current_bindings, current_aliases = self._policy_disk_bindings(policy_demand)
                    if current_bindings != bindings or current_aliases != aliases:
                        raise SmartRefreshError('identity_changed')
                if self.clock() >= deadline:
                    for row in results:
                        if row['device'] in pending:
                            row.update(status='failed', reason='timeout', sampled_at=None)
                    pending.clear()
                for row in results:
                    name = row['device']
                    identity = identities.get(name)
                    if identity is not None and self._smart_context(name) != identity:
                        row.update(status='identity_changed', reason='identity_changed', sampled_at=None)
                        pending.pop(name, None)
                        attempts.pop(name, None)
                authorized()
                # Prune against current lifetimes at the commit point, never
                # an older background scan's list of active names. New manual
                # records take precedence when enforcing the fixed cache cap.
                cache = self._smart_measurements if missing_only else self._smart_cache
                retained = {name: record for name, record in cache.items()
                            if len(record) == 3 and self._smart_context(name) == record[2]}
                retained.update(copy.deepcopy(pending))
                keys = list(pending) + [name for name in retained if name not in pending]
                kept_attempts = {name: record for name, record in self._smart_attempts.items()
                                 if self._smart_context(name) == record['identity']}
                kept_attempts.update({name: record for name, record in attempts.items()
                                     if self._smart_context(name) == record['identity']})
                authorized()
                if self.clock() >= deadline:
                    for row in results:
                        if row['device'] in pending:
                            row.update(status='failed', reason='timeout', sampled_at=None)
                # A successful newer actual command supersedes a visible
                # verdict; cache-only fill/policy hits do not revive old data.
                observations = {name: record for name, record in self._smart_visible_observations.items()
                                if self._smart_context(name) == record['identity']}
                for name in pending:
                    if not visible and self.clock() < deadline:
                        observations.pop(name, None)
                proofs = {}
                if visible:
                    for row in results:
                        name, identity = row['device'], identities.get(row['device'])
                        if identity is None or self._smart_context(name) != identity:
                            observations.pop(name, None)
                            continue
                        value = visible_values.get(name) if row['status'] in ('refreshed', 'skipped_standby') else None
                        attempt = kept_attempts.get(name)
                        history = [record for record in (self._smart_cache.get(name), self._smart_measurements.get(name))
                                   if record and len(record) == 3 and record[2] == identity and _finite(record[0]) is not None]
                        historical = max(history, key=lambda record: record[0], default=None)
                        dates = ({key: historical[1].get(key) for key in ('smart_sampled_at', 'smart_sampled_monotonic')}
                                 if historical is not None else {})
                        observations[name] = {'identity': identity, 'batch': sequence,
                                              'value': copy.deepcopy(value), 'history': dates}
                        if attempt and attempt['identity'] == identity:
                            reason = ('standby' if row['status'] == 'skipped_standby' else
                                      None if (row['status'] == 'refreshed' and value is not None
                                               and value.get('standby') is False and attempt.get('sequence') == sequence)
                                      else 'power_state_unverified')
                            proofs[name] = {'identity': identity, 'sequence': attempt.get('sequence'),
                                            'stamp': attempt['stamp'], 'reason': reason}
                authorized()
                if self.clock() >= deadline:
                    for row in results:
                        if row['device'] in pending:
                            row.update(status='failed', reason='timeout', sampled_at=None)
                        if visible and row['device'] in observations:
                            observations[row['device']]['value'] = None
                        if row['device'] in proofs:
                            proofs[row['device']]['reason'] = 'power_state_unverified'
                else:
                    cache = {name: retained[name] for name in keys[:128]}
                    if missing_only:
                        self._smart_measurements = cache
                    else:
                        self._smart_cache = cache
                    self._smart_attempts = dict(list(kept_attempts.items())[-128:])
                    if policy_demand is not None:
                        self._smart_policy_aliases = ({'demand': policy_demand, 'epoch': epoch, 'bindings': aliases}
                            if any(source.startswith('disk:serial:') for source in policy_demand.sources) else {})
                self._smart_visible_observations = dict(list(observations.items())[-128:])
                result = {'ok': True, 'force': force, 'status': 'complete' if all(
                    row['status'] in ('refreshed', 'skipped_standby', 'cached') for row in results) else 'partial',
                    'started_at': started, 'finished_at': time.time(), 'results': results}
                if visible:
                    self._smart_visible_batch = {'receipt': result, 'sequence': sequence, 'epoch': epoch, 'proofs': proofs}
            # snapshot() reprojects this cache immediately. Do not wake the
            # names worker: its normal storage snapshot is a separate source.
            return result
        finally:
            self._smart_refresh_lock.release()

    @staticmethod
    def _disk_generation(folder):
        """Bind cached identity to this sysfs device lifetime, not its sdX name."""
        try:
            link, target = folder.lstat(), folder.stat()
            return (str(folder.resolve()), link.st_dev, link.st_ino, link.st_ctime_ns,
                    target.st_dev, target.st_ino, target.st_ctime_ns)
        except (OSError, ValueError, RuntimeError):
            return None

    def _disk_serial_info(self, name, folder, smart=None, identity_serial=None, *, policy_sources=None):
        """Merge bounded existing metadata, preserving disagreement as unknown."""
        candidates = {}
        invalid = False

        def candidate(source, value):
            nonlocal invalid
            if value is None or value == "":
                return
            if (not isinstance(value, str) or not 0 < len(value.strip()) <= 512
                    or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value.strip())):
                invalid = True
                return
            candidates[source] = value.strip()

        paths = [folder / "device/serial", folder / "serial"]
        if name.startswith("nvme"):
            controller = re.match(r"nvme\d+", name)[0]
            paths.append(self.sys_root / "class/nvme" / controller / "serial")
        for index, path in enumerate(paths):
            candidate("sysfs:" + str(index), _text(path, 513))
        dev = (_text(folder / "dev", 32) or "").strip()
        if re.fullmatch(r"\d{1,6}:\d{1,6}", dev):
            metadata = _text(self.run_root / "udev/data" / ("b" + dev), 65537) or ""
            if len(metadata) <= 65536:
                for line in metadata.splitlines():
                    if line.startswith("E:ID_SERIAL_SHORT="):
                        candidate("udev:" + str(len(candidates)), line.partition("=")[2])
        metadata_values = set(candidates.values()) if policy_sources is not None else set()
        metadata_status = ('invalid' if invalid else 'conflict' if len(metadata_values) > 1
                           else 'ok' if metadata_values else 'missing')
        data = smart.get("smart_data") if isinstance(smart, dict) else None
        if isinstance(data, dict):
            candidate("smart_cache", data.get("serial_number"))
        candidate("smart_identity_cache", identity_serial)
        values = set(candidates.values())
        status = "invalid" if invalid else "conflict" if len(values) > 1 else "ok" if values else "missing"
        result = {"serial": next(iter(values)) if status == "ok" else None,
                  "status": status, "sources": sorted(candidates)}
        if policy_sources is not None:
            # Only current sysfs/udev can establish an alias. Existing owner
            # identity caches participate solely as additional conflict vetoes.
            result['policy_metadata_status'] = metadata_status
            result['policy_matches'] = {source for value in metadata_values
                if (source := 'disk:serial:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]) in policy_sources}
        return result

    def _disk_serial(self, name, folder, smart=None):
        """Use existing sysfs/udev and a caller-verified SMART cache; no probes."""
        return self._disk_serial_info(name, folder, smart)["serial"]

    def _disks(self):
        rows, active, serials, contexts, details = [], set(), {}, {}, {}
        with self._lock:
            identity_cache = copy.deepcopy(self._disk_serial_cache)
        for folder in sorted((self.sys_root / "block").glob("*"))[:128]:
            if self._stop.is_set():
                break
            name = folder.name
            if not re.fullmatch(r"(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)", name):
                continue
            active.add(name)
            # Bind the entire row before its first sysfs size/model read.
            cache_key = self._smart_context(name)
            if cache_key is None:
                identity_cache.pop(name, None)
                continue
            identity, generation, _ = cache_key
            sectors = _integer(folder / "size")
            rotational = _integer(folder / "queue/rotational")
            media = "NVMe SSD" if name.startswith("nvme") else "eMMC" if name.startswith("mmcblk") else "HDD" if rotational == 1 else "SSD" if rotational == 0 else None
            smart = self._smart(name, cache_key) if not name.startswith("mmcblk") else {
                "smart_available": False, "standby": None, "temp_c": None,
                "health": {"passed": None, "reason": "eMMC 不提供通用 SMART 状态"}}
            entry = {"id": name, "device": "/dev/" + name,
                     "_lifecycle": cache_key,
                     "model": (_text(folder / "device/model", 512) or "").strip() or None,
                     "size_bytes": sectors * 512 if sectors is not None else None, "type": media,
                     "temp_c": smart["temp_c"],
                     "temperature_source": 'smart',
                     "health": smart["health"], "smart_available": smart["smart_available"], "standby": smart["standby"],
                     "stable_identity": self._disk_identity(name, folder, smart.get('smart_identity')),
                     "smart_temperature_c": smart.get('smart_temperature_c'),
                     "smart_sampled_monotonic": smart.get('smart_sampled_monotonic'),
                     "smart_sampled_at": smart.get('smart_sampled_at'),
                     "smart_fresh": smart.get('smart_fresh'),
                     "smart_status": smart.get('smart_status', 'unavailable'),
                     "temperature_critical_c": smart.get('temperature_critical_c')}
            if self._smart_context(name) != cache_key:
                identity_cache.pop(name, None)
                continue
            prior = identity_cache.get(name, {})
            prior_serial = prior.get("serial") if prior.get("cache_key") == cache_key else None
            detail = self._disk_serial_info(name, folder, smart, prior_serial)
            if self._smart_context(name) != cache_key:
                identity_cache.pop(name, None)
                continue
            rows.append(entry)
            serials[name] = detail["serial"]
            details[name] = detail
            if detail["status"] == "ok" and any(source in detail["sources"] for source in ("smart_cache", "smart_identity_cache")):
                identity_cache[name] = {"cache_key": cache_key, "serial": detail["serial"]}
            else:
                identity_cache.pop(name, None)
            contexts[name] = {"generation": generation, "identity": identity,
                              "cache_key": cache_key, "row": copy.deepcopy(entry), "consistent": True}
        storage = self.storage_client.snapshot()
        names = read_disk_names(serials, storage)
        verified_serials = {}
        # The two vendor getters may overlap a hot unplug. Check the same sysfs
        # lifetime again before publishing an otherwise valid serial/name join.
        retained = []
        for row in rows:
            name, context = row["id"], contexts[row["id"]]
            if self._smart_context(name) != context['cache_key']:
                identity_cache.pop(name, None)
                contexts.pop(name, None)
                continue
            row.update(names["items"][name])
            detail = details[name]
            serial, status = self._storage_serial_match(name, detail, serials, storage, names["available"])
            verified_serials[name] = {"cache_key": context["cache_key"], "serial": serial,
                                      "serial_status": status,
                                      "serial_sources": detail["sources"] + (["hiksemi_storage_socket"] if serial else [])}
            retained.append(row)
        with self._lock:
            rows = []
            for row in retained:
                name = row['id']
                if self._smart_context(name) != contexts[name]['cache_key']:
                    contexts.pop(name, None)
                    identity_cache.pop(name, None)
                    verified_serials.pop(name, None)
                    continue
                rows.append(row)
            self._disk_name_context = contexts
            self._disk_serial_cache = {name: value for name, value in identity_cache.items() if name in active}
            self._disk_verified_serials = verified_serials
        return {"available": bool(rows), "reason": None if rows else "未发现可查询的底层块设备", "items": rows,
                "names": {"available": names["available"], "reason": names["reason"]}}

    def storage_identity_cached(self):
        """Owner Web projection of background identity; no SMART or vendor IPC.

        Recheck bounded sysfs/udev metadata against the sampled generation and
        current identity caches. A replaced, ambiguous or conflicting disk stays
        unknown until a later background sample verifies the fixed vendor list.
        """
        with self._lock:
            contexts = copy.deepcopy(self._disk_name_context)
            identities = copy.deepcopy(self._disk_serial_cache)
            verified = copy.deepcopy(self._disk_verified_serials)
            smart_cache = copy.deepcopy(self._smart_cache)
        details = {}
        for name, context in contexts.items():
            folder = self.sys_root / "block" / name
            current = (context["consistent"] and context["generation"] == self._disk_generation(folder)
                       and context["identity"] == self._disk_identity(name, folder))
            cached = smart_cache.get(name)
            smart = cached[1] if current and cached and len(cached) == 3 and cached[2] == context["cache_key"] else None
            prior = identities.get(name, {})
            prior_serial = prior.get("serial") if prior.get("cache_key") == context["cache_key"] else None
            details[name] = (self._disk_serial_info(name, folder, smart, prior_serial) if current else
                             {"serial": None, "status": "identity_changed", "sources": []})
        items = []
        for name, context in contexts.items():
            detail, evidence = details[name], verified.get(name, {})
            folder = self.sys_root / "block" / name
            current = (context["consistent"] and context["generation"] == self._disk_generation(folder)
                       and context["identity"] == self._disk_identity(name, folder))
            serial, status = None, detail["status"]
            if not current:
                status = "identity_changed"
            elif status == "ok":
                if sum(row["serial"] == detail["serial"] for row in details.values()) != 1:
                    status = "conflict"
                elif evidence.get("cache_key") != context["cache_key"]:
                    status = "missing"
                elif evidence.get("serial_status") != "ok":
                    status = evidence.get("serial_status") or "missing"
                elif evidence.get("serial") != detail["serial"]:
                    status = "conflict"
                else:
                    serial = detail["serial"]
            if status in ("conflict", "invalid", "identity_changed"):
                with self._lock:
                    for cache in (self._disk_serial_cache, self._disk_verified_serials):
                        if cache.get(name, {}).get("cache_key") == context["cache_key"]:
                            cache.pop(name, None)
            items.append({"id": name, "serial": serial, "serial_status": status,
                          "serial_sources": evidence.get("serial_sources", []) if serial else [],
                          "matched": serial is not None})
        available = any(row["serial"] is not None for row in items)
        return {"available": available, "items": items,
                "source": "insights_cache+hiksemi_storage_socket",
                "reason": None if available else "未取得缓存与原厂列表唯一一致的当前硬盘序列号"}

    def storage_name_diagnostics(self):
        """Sample fixed vendor lists with existing identity caches, never SMART.

        Only names, device IDs, consistency and one-way serial hashes leave this
        method. A cache from a different sysfs lifetime cannot name a new disk.
        """
        return self._storage_diagnostics()

    def storage_identity_diagnostics(self):
        """Explicit developer-only identity projection, never a public overview.

        Full serials require a current cache and unique agreement with the fixed
        vendor disk getter. Nothing here probes SMART or opens a block device.
        """
        return self._storage_diagnostics(include_serial=True)

    @staticmethod
    def _storage_serial_match(name, detail, serials, snapshot, vendor_available):
        """Join existing serial evidence to one unique physical vendor record."""
        if detail['status'] != 'ok':
            return None, detail['status']
        if not vendor_available:
            return None, 'vendor_unavailable'
        rows = snapshot.get('disks', [])
        matches = [row for row in rows if row.get('disk_name') == name]
        if not matches:
            return None, 'vendor_missing'
        serial = detail['serial']
        if len(matches) != 1 or sum(value == serial for value in serials.values()) != 1:
            return None, 'conflict'
        row = matches[0]
        port, vendor_serial = row.get('disk_port'), row.get('disk_serial')
        if (not isinstance(vendor_serial, str) or not vendor_serial
                or type(port) is not int or not 0 <= port <= 65535):
            return None, 'vendor_missing'
        if (vendor_serial != serial or sum(other.get('disk_serial') == serial for other in rows) != 1
                or sum(type(other.get('disk_port')) is int and other['disk_port'] == port for other in rows) != 1):
            return None, 'conflict'
        return serial, 'ok'

    def _storage_diagnostics(self, *, include_serial=False):
        """Reuse fixed getters and generation-bound caches for both projections."""
        with self._lock:
            contexts = copy.deepcopy(self._disk_name_context)
            identity_cache = copy.deepcopy(self._disk_serial_cache)
        # Freeze the sampled disk lifetimes before either getter can overlap a
        # hot replacement or a background publication for the same sdX name.
        snapshot = self.storage_client.snapshot()
        serials, details = {}, {}
        for name, context in contexts.items():
            folder = self.sys_root / "block" / name
            current = (context["consistent"] and context["generation"] == self._disk_generation(folder)
                       and context["identity"] == self._disk_identity(name, folder))
            cached = self._smart_cache.get(name)
            # Validate and copy the same tuple. A second dictionary lookup could
            # race a background sample for a newly inserted disk at this name.
            smart = copy.deepcopy(cached[1]) if current and cached and len(cached) == 3 and cached[2] == context["cache_key"] else None
            prior = identity_cache.get(name, {})
            prior_serial = prior.get("serial") if prior.get("cache_key") == context["cache_key"] else None
            detail = self._disk_serial_info(name, folder, smart, prior_serial) if current else {"serial": None, "status": "identity_changed", "sources": []}
            serials[name], details[name] = detail["serial"], detail
        names = read_disk_names(serials, snapshot)

        def serial_hash(value):
            return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24] if isinstance(value, str) and value else None

        items = []
        for name, context in contexts.items():
            detail, result = details[name], names["items"][name]
            folder = self.sys_root / "block" / name
            unchanged = (context["consistent"] and context["generation"] == self._disk_generation(folder)
                         and context["identity"] == self._disk_identity(name, folder))
            if not unchanged:
                detail = {"serial": None, "status": "identity_changed", "sources": []}
                result = {"user_name": None, "space_names": [], "name_source": None}
            if detail["status"] in ("conflict", "invalid", "identity_changed"):
                with self._lock:
                    for cache in (self._disk_serial_cache, self._disk_verified_serials):
                        if cache.get(name, {}).get("cache_key") == context["cache_key"]:
                            cache.pop(name, None)
            if include_serial:
                serial, status = self._storage_serial_match(name, detail, serials, snapshot, names['available'])
                if status != 'ok':
                    result = {"user_name": None, "space_names": [], "name_source": None}
                items.append({"id": name, "device": context["row"].get("device"), **result,
                              "serial": serial, "serial_status": status,
                              "serial_sources": detail['sources'] + (["hiksemi_storage_socket"] if serial else []),
                              "matched": serial is not None})
                continue
            items.append({"id": name, "device": context["row"].get("device"),
                          "model": context["row"].get("model"), **result,
                          "matched": bool(result["user_name"] or result["space_names"]),
                          "serial_status": detail["status"], "serial_sources": detail["sources"],
                          "serial_hash": serial_hash(detail["serial"])})
        if include_serial:
            available = any(row['serial'] is not None for row in items)
            return {"available": available,
                    "reason": None if available else "未取得缓存与原厂列表唯一一致的当前硬盘序列号",
                    "source": "insights_cache+hiksemi_storage_socket", "items": items,
                    "commands": ["get_disk_list;", "get_space_list;"]}
        vendor = {"disks": [{"disk_name": row.get("disk_name"), "disk_port": row.get("disk_port"),
                              "serial_hash": serial_hash(row.get("disk_serial"))} for row in snapshot.get("disks", [])],
                  "spaces": [{"space_name": row.get("space_name"), "is_raid": row.get("is_raid"), "port": row.get("port"),
                               "member": [{"disk_name": member.get("disk_name"), "root_name": member.get("root_name"),
                                           "port": member.get("port"),
                                           "serial_hash": serial_hash(member.get("disk_id"))} for member in row.get("member") or []]}
                              for row in snapshot.get("spaces", [])]}
        return {"available": names["available"], "reason": names["reason"],
                "error_code": snapshot.get("error_code"),
                "failed_getter": snapshot.get("failed_getter"),
                "source": snapshot.get("source"), "items": items, "vendor": vendor,
                "commands": ["get_disk_list;", "get_space_list;"]}

    def _container_listing(self, timeout=DOCKER_READ_TIMEOUT):
        command, error = self._run("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], timeout=timeout)
        if error:
            return None, error
        if command[0] != 0:
            return None, "Docker 未运行或当前进程没有访问权限"
        listed = _json_lines(command[1])
        if command[1].strip() and not listed:
            return None, "Docker 返回了无效的容器列表"
        rows = []
        for row in listed:
            identity = row.get("ID")
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{12,64}", identity):
                continue
            name = str(row.get("Names") or "").lstrip("/")
            labels = [value.strip() for value in str(row.get("Labels") or "").split(",")]
            protected = name.lower() in {"r1-toolbox", "r1-boot-once"} or "com.docker.compose.service=r1-toolbox" in labels
            state = str(row.get("State") or "")
            available_actions = ([] if protected else ["stop", "restart"] if state == "running"
                                 else ["start"] if state in ("created", "exited") else [])
            rows.append({"id": identity, "name": name, "image": str(row.get("Image") or ""),
                         "state": state, "status": str(row.get("Status") or ""),
                         "ports": str(row.get("Ports") or ""), "protected": protected,
                         "available_actions": available_actions})
        if command[1].strip() and not rows:
            return None, "Docker 返回了无效的容器标识"
        return rows, None

    def _containers(self):
        rows, error = self._container_listing()
        if error:
            return _capability(error)
        stats = {}
        stats_command, stats_error = self._run("docker", ["stats", "--all", "--no-stream", "--format", "{{json .}}"],
                                                timeout=DOCKER_READ_TIMEOUT)
        if not stats_error and stats_command[0] == 0:
            for row in _json_lines(stats_command[1]):
                identity = row.get("ID") or row.get("Container")
                if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{12,64}", identity):
                    stats[identity] = row
        for row in rows:
            identity = row["id"]
            matches = [value for key, value in stats.items() if identity.startswith(key) or key.startswith(identity)]
            detail = matches[0] if len(matches) == 1 else {}
            memory = str(detail.get("MemUsage") or "").split("/", 1)[0]
            row.update(cpu_percent=_percent_string(detail.get("CPUPerc")), memory_bytes=_size_string(memory),
                       memory_percent=_percent_string(detail.get("MemPerc"), maximum=100))
        return {"available": True, "reason": None, "items": rows,
                "stats_reason": stats_error or ("容器统计暂不可用" if stats_command and stats_command[0] != 0 else None)}

    def _vms(self):
        command, error = self._run("virsh", ["list", "--all", "--uuid"])
        if error:
            return _capability(error)
        if command[0] != 0:
            return _capability("libvirt 未运行或当前进程没有访问权限")
        identifiers = [line.strip().lower() for line in command[1].splitlines()[:64] if line.strip()]
        if identifiers and not any(self._valid_uuid(value) for value in identifiers):
            return _capability("libvirt 返回了无效的虚拟机列表")
        rows, unreadable = [], False
        for identity in identifiers:
            if self._stop.is_set():
                break
            if not self._valid_uuid(identity):
                continue
            detail, failure = self._run("virsh", ["dominfo", identity])
            if failure or detail[0] != 0:
                unreadable = True
                continue
            fields = dict(line.split(":", 1) for line in detail[1].splitlines() if ":" in line)
            fields = {key.strip(): value.strip() for key, value in fields.items()}
            if fields.get("UUID", "").lower() != identity:
                unreadable = True
                continue
            rows.append({"id": identity, "name": fields.get("Name") or identity, "state": fields.get("State") or None})
        return {"available": True, "reason": "部分虚拟机状态暂不可用" if unreadable else None, "items": rows}

    @staticmethod
    def _valid_uuid(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
            return False
        try:
            return str(uuid.UUID(value)) == value.lower()
        except ValueError:
            return False

    def action(self, name, payload):
        if name not in ("container.action", "vm.action"):
            return {"ok": False, "error": "unsupported_action", "reason": "不支持此操作"}
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            return {"ok": False, "error": "confirmation_required", "reason": "请先确认操作"}
        identifier, operation = payload.get("id"), payload.get("operation")
        container = name == "container.action"
        allowed = ("start", "stop", "restart") if container else ("start", "shutdown", "reboot", "suspend", "resume")
        valid_id = isinstance(identifier, str) and (bool(re.fullmatch(r"[0-9a-f]{12,64}", identifier)) if container else self._valid_uuid(identifier))
        if not valid_id or operation not in allowed:
            return {"ok": False, "error": "invalid_request", "reason": "操作或设备标识不合法"}
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "error": "busy", "reason": "已有应用操作正在执行，请先刷新状态，勿连续提交"}
        try:
            if self._stop.is_set():
                return {"ok": False, "error": "stopping", "reason": "应用控制服务正在停止，请稍后重新读取状态"}
            with self._lock:
                capability = copy.deepcopy(self._state["containers" if container else "vms"])
            if not capability["available"]:
                return {"ok": False, "error": "unsupported", "reason": capability["reason"]}
            if container:
                matches = [row for row in capability["items"] if row["id"].startswith(identifier)]
            else:
                matches = [row for row in capability["items"] if row["id"] == identifier.lower()]
            if len(matches) != 1:
                return {"ok": False, "error": "not_found", "reason": "设备不存在或标识不唯一，请刷新列表"}
            selected = matches[0]
            if container and selected["protected"]:
                return {"ok": False, "error": "protected_container", "reason": "保留的 Toolbox 容器不允许在此操作"}
            if container:
                # Re-read the non-sensitive ps view immediately before a write.
                # A stale cached running state must never restart a now-stopped
                # container, and a changed protection label must take effect.
                rows, failure = self._container_listing()
                self._refresh["containers"].set()
                if failure:
                    return {"ok": False, "error": "state_unconfirmed", "reason": "Docker 当前状态未确认，请刷新列表后再决定"}
                current = next((row for row in rows if row["id"] == selected["id"]), None)
                if current is None:
                    return {"ok": False, "error": "not_found", "reason": "容器已不存在，请刷新列表"}
                if current["protected"]:
                    return {"ok": False, "error": "protected_container", "reason": "保留的 Toolbox 容器不允许在此操作"}
                if operation not in current["available_actions"]:
                    return {"ok": False, "error": "invalid_state", "reason": "容器状态已改变或不支持此操作，请刷新列表后再决定"}
            if self._stop.is_set():
                return {"ok": False, "error": "stopping", "reason": "应用控制服务正在停止，请稍后重新读取状态"}
            arguments = ["container", operation, selected["id"]] if container else [operation, selected["id"]]
            # Docker may need its normal graceful-stop period. No new flags or
            # forced stop are added, and uncertain results are never retried.
            timeout = (DOCKER_STOP_TIMEOUT if operation in ("stop", "restart") else DOCKER_START_TIMEOUT) if container else 3
            command, error = self._run("docker" if container else "virsh", arguments, timeout=timeout)
            self._refresh["containers" if container else "vms"].set()
            if error:
                return {"ok": False, "error": "operation_unconfirmed", "refresh_pending": True,
                        "reason": ("操作超时，" if "响应超时" in error else "操作响应异常，")
                                  + "结果未确认；请先刷新列表核对，勿连续重试"}
            if command[0] != 0:
                return {"ok": False, "error": "operation_failed", "exit_code": command[0],
                        "refresh_pending": True, "reason": "宿主机未完成操作，请刷新列表并查看服务状态"}
            return {"ok": True, "state": {"id": selected["id"], "operation": operation, "refresh_pending": True}}
        finally:
            self._action_lock.release()
