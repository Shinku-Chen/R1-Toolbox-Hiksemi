"""Bounded, local NAS diagnostics and explicitly confirmed application actions.

Fast proc/sys sampling and slow optional commands run in separate workers.  The
HTTP-facing snapshot only copies memory; SMART probing never blocks a request.
All commands have fixed arguments and run without a shell.
"""

import array
from collections import deque
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

try:
    import fcntl
except ImportError:
    fcntl = None


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
                 page_size=None, clock_ticks=None, etc_root=None, run_root=None):
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
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._stop = threading.Event()
        self._refresh = {name: threading.Event() for name in ("disks", "containers", "vms", "device")}
        self._threads = []
        self._history = deque(maxlen=max(1, min(int(history_limit), 120)))
        self._previous_cpu = {}
        self._previous_network = {}
        self._previous_process = {}
        self._smart_cache = {}
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
        for event in self._refresh.values():
            event.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

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

    def _disk_temperature(self, name, folder):
        candidates = list((folder / "device").glob("hwmon/hwmon*/temp1_input"))
        candidates += list((folder / "device").glob("hwmon*/temp1_input"))
        if name.startswith("nvme"):
            controller = re.match(r"nvme\d+", name)[0]
            base = self.sys_root / "class/nvme" / controller
            candidates += list(base.glob("device/hwmon/hwmon*/temp1_input"))
            candidates += list(base.glob("hwmon*/temp1_input"))
        values = [_integer(candidate) for candidate in candidates]
        valid = [value / 1000 for value in values if value is not None and 0 < value <= 200000]
        return max(valid) if valid else None

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

    def _smart(self, name, identity=None):
        now = self.clock()
        cached = self._smart_cache.get(name)
        if cached and 0 <= now - cached[0] < 30 and len(cached) == 3 and cached[2] == identity:
            return copy.deepcopy(cached[1])
        result = {"smart_available": False, "standby": None, "temp_c": None,
                  "health": {"passed": None, "reason": None},
                  "smart_temperature_c": None, "smart_sampled_monotonic": None,
                  "smart_status": "unavailable", "smart_identity": None,
                  "temperature_critical_c": None}
        command, error = self._run("smartctl", ["-n", "standby", "-j", "-a", "/dev/" + name])
        if error:
            result["health"]["reason"] = error
        else:
            code, output = command
            try:
                data = json.loads(output)
                if not isinstance(data, dict):
                    raise ValueError("invalid SMART response")
                messages = data.get("smartctl", {}).get("messages", [])
                notes = " ".join(str(entry.get("string", "")) for entry in messages if isinstance(entry, dict))
                power = str(data.get("power_mode", {}).get("string", ""))
                standby = bool(re.fullmatch(r'(?:standby|sleep)(?:\s+mode)?', power.strip(), re.I)
                               or re.search(r'\b(?:device|disk)\s+is\s+in\s+(?:standby|sleep)\s+mode\b', notes, re.I))
                result["standby"] = True if standby else None
                if standby:
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
                        candidate = temperature.get('current') if isinstance(temperature, dict) else None
                        measured = _finite(candidate, minimum=-40, maximum=150)
                        if measured is None and isinstance(nvme, dict):
                            measured = _finite(nvme.get('temperature'), minimum=-40, maximum=150)
                        result['smart_temperature_c'] = measured
                        if measured is not None:
                            result['smart_status'] = 'ok'
                    critical = temperature.get('op_limit_max') if isinstance(temperature, dict) else None
                    result['temperature_critical_c'] = _finite(critical, minimum=40, maximum=150)
                serial = data.get('serial_number')
                wwn = data.get('wwn')
                if isinstance(wwn, dict) and all(type(wwn.get(key)) is int for key in ('naa', 'oui', 'id')):
                    raw = json.dumps({key: wwn[key] for key in ('naa', 'oui', 'id')}, sort_keys=True)
                    result['smart_identity'] = 'wwn:' + hashlib.sha256(raw.encode()).hexdigest()[:24]
                elif isinstance(serial, str) and 0 < len(serial.strip()) <= 512:
                    result['smart_identity'] = 'serial:' + hashlib.sha256(serial.strip().encode()).hexdigest()[:24]
            except (ValueError, TypeError, AttributeError):
                result["health"]["reason"] = "SMART 返回了无效数据"
        result['smart_sampled_monotonic'] = self.clock()
        self._smart_cache[name] = (result['smart_sampled_monotonic'], copy.deepcopy(result), identity)
        return result

    def _disks(self):
        rows, active = [], set()
        for folder in sorted((self.sys_root / "block").glob("*"))[:128]:
            if self._stop.is_set():
                break
            name = folder.name
            if not re.fullmatch(r"(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)", name):
                continue
            active.add(name)
            sectors = _integer(folder / "size")
            rotational = _integer(folder / "queue/rotational")
            media = "NVMe SSD" if name.startswith("nvme") else "eMMC" if name.startswith("mmcblk") else "HDD" if rotational == 1 else "SSD" if rotational == 0 else None
            temperature = self._disk_temperature(name, folder)
            identity = self._disk_identity(name, folder)
            smart = self._smart(name, identity) if not name.startswith("mmcblk") else {
                "smart_available": False, "standby": None, "temp_c": None,
                "health": {"passed": None, "reason": "eMMC 不提供通用 SMART 状态"}}
            entry = {"id": name, "device": "/dev/" + name,
                     "model": (_text(folder / "device/model", 512) or "").strip() or None,
                     "size_bytes": sectors * 512 if sectors is not None else None, "type": media,
                     "temp_c": temperature if temperature is not None else smart["temp_c"],
                     "health": smart["health"], "smart_available": smart["smart_available"], "standby": smart["standby"],
                     "stable_identity": self._disk_identity(name, folder, smart.get('smart_identity')),
                     "smart_temperature_c": smart.get('smart_temperature_c'),
                     "smart_sampled_monotonic": smart.get('smart_sampled_monotonic'),
                     "smart_status": smart.get('smart_status', 'unavailable'),
                     "temperature_critical_c": smart.get('temperature_critical_c')}
            rows.append(entry)
        self._smart_cache = {key: value for key, value in self._smart_cache.items() if key in active}
        return {"available": bool(rows), "reason": None if rows else "未发现可查询的底层块设备", "items": rows}

    def _container_listing(self, timeout=3):
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
        stats_command, stats_error = self._run("docker", ["stats", "--all", "--no-stream", "--format", "{{json .}}"])
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
                rows, failure = self._container_listing(timeout=2)
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
            timeout = 15 if container and operation in ("stop", "restart") else 3
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
