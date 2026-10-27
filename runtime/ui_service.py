#!/usr/bin/env python3
"""Serve local read-only NAS metrics while the official screen app is running.

The one-shot container starts this script through the host service manager.  It
does not install software, change hardware controls, or write host files.
"""

import argparse
import copy
import datetime
import errno
import http.server
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

try:
    import fcntl
except ImportError:  # Allow non-Linux development hosts to import the module.
    fcntl = None


HOST = "127.0.0.1"
PORT = 18731
SAMPLE_INTERVAL = 2.0


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def read_integer(path):
    text = read_text(path)
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None


def read_cpu_ticks(proc_root):
    text = read_text(Path(proc_root) / "stat")
    if text is None:
        return None
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            # guest and guest_nice are already included in user/nice.
            ticks = [int(value) for value in fields[1:9]]
            if len(ticks) < 4 or any(value < 0 for value in ticks):
                return None
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            return sum(ticks), idle
        except ValueError:
            return None
    return None


def read_memory(proc_root):
    text = read_text(Path(proc_root) / "meminfo")
    values = {}
    if text is not None:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] in ("MemTotal:", "MemAvailable:"):
                try:
                    if fields[2] == "kB" and int(fields[1]) >= 0:
                        values[fields[0][:-1]] = int(fields[1]) * 1024
                except ValueError:
                    pass
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    used = None
    if total is not None and total > 0 and available is not None and available <= total:
        used = total - available
    return {
        "total_bytes": total if total and total > 0 else None,
        "used_bytes": used,
        "usage_percent": used / total * 100 if used is not None else None,
    }


def read_uptime(proc_root):
    text = read_text(Path(proc_root) / "uptime")
    try:
        value = float(text.split()[0]) if text else None
        return value if value is not None and math.isfinite(value) and value >= 0 else None
    except (ValueError, IndexError):
        return None


def default_interface(proc_root):
    text = read_text(Path(proc_root) / "net/route")
    candidates = []
    if text is None:
        return None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            if not int(fields[3], 16) & 1:
                continue
            iface, metric = fields[0], int(fields[6])
            if iface in ("lo", ".", "..") or "/" in iface or len(iface.encode()) > 15:
                continue
            candidates.append((metric, iface))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


def interface_ipv4(iface):
    if iface is None or fcntl is None or not sys.platform.startswith("linux"):
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            request = struct.pack("256s", iface.encode("utf-8")[:15])
            result = fcntl.ioctl(connection.fileno(), 0x8915, request)  # SIOCGIFADDR
            return socket.inet_ntoa(result[20:24])
    except (OSError, ValueError, UnicodeError):
        return None


def read_network_counters(sys_root, iface):
    if iface is None:
        return None
    folder = Path(sys_root) / "class/net" / iface / "statistics"
    rx, tx = read_integer(folder / "rx_bytes"), read_integer(folder / "tx_bytes")
    return (rx, tx) if rx is not None and tx is not None and rx >= 0 and tx >= 0 else None


def read_cpu_temperature(sys_root):
    temperatures = []
    for folder in sorted((Path(sys_root) / "class/hwmon").glob("hwmon*")):
        name = read_text(folder / "name")
        if name is None or name.strip() not in ("coretemp", "k10temp"):
            continue
        driver = name.strip()
        preferred, fallback = [], []
        for sensor in folder.glob("temp*_input"):
            value = read_integer(sensor)
            if value is None:
                continue
            label = read_text(sensor.with_name(sensor.name.replace("_input", "_label")))
            label = label.strip() if label is not None else ""
            if (driver == "coretemp" and label.startswith("Package id")) or (
                driver == "k10temp" and label == "Tdie"
            ):
                preferred.append(value / 1000)
            elif sensor.name == "temp1_input" or (driver == "k10temp" and label == "Tctl"):
                fallback.append(value / 1000)
        if preferred or fallback:
            temperatures.append(max(preferred or fallback))
    return max(temperatures) if temperatures else None


def unescape_mount_field(text):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def read_disks(proc_root, statvfs=None):
    statvfs = statvfs or os.statvfs
    text = read_text(Path(proc_root) / "self/mountinfo")
    disks, seen = [], set()
    if text is None:
        return disks
    for line in text.splitlines():
        parts = line.split(" - ", 1)
        if len(parts) != 2:
            continue
        fields, extra = parts[0].split(), parts[1].split()
        if len(fields) < 6 or len(extra) < 2:
            continue
        mount = unescape_mount_field(fields[4])
        if not mount.startswith("/drives/") or fields[2] in seen:
            continue
        if extra[0] in ("tmpfs", "devtmpfs", "proc", "sysfs", "overlay", "squashfs"):
            continue
        seen.add(fields[2])
        entry = {"mount": mount, "total_bytes": None, "used_bytes": None, "usage_percent": None}
        try:
            stats = statvfs(mount)
            block_size = stats.f_frsize or stats.f_bsize
            total = stats.f_blocks * block_size
            used = (stats.f_blocks - stats.f_bfree) * block_size
            if total > 0 and 0 <= used <= total:
                entry.update(total_bytes=total, used_bytes=used, usage_percent=used / total * 100)
        except (OSError, ValueError, OverflowError):
            pass
        disks.append(entry)
    return sorted(disks, key=lambda entry: entry["mount"])


def empty_status():
    return {
        "schema_version": 1,
        "available": False,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hostname": None,
        "uptime_seconds": None,
        "cpu": {"usage_percent": None, "temp_c": None},
        "memory": {"total_bytes": None, "used_bytes": None, "usage_percent": None},
        "network": {"interface": None, "ipv4": None, "rx_bytes_per_sec": None, "tx_bytes_per_sec": None},
        "disks": [],
    }


class StatusSampler:
    """Sampling is separate from serving and accepts fixture proc/sys roots."""

    def __init__(self, proc_root="/proc", sys_root="/sys", clock=None):
        self.proc_root, self.sys_root = Path(proc_root), Path(sys_root)
        self.clock = clock or time.monotonic
        self.previous_cpu = None
        self.previous_network = None

    def sample(self):
        status = empty_status()
        ticks = read_cpu_ticks(self.proc_root)
        if ticks is not None and self.previous_cpu is not None:
            total, idle = ticks[0] - self.previous_cpu[0], ticks[1] - self.previous_cpu[1]
            if total > 0 and 0 <= idle <= total:
                status["cpu"]["usage_percent"] = (total - idle) / total * 100
        self.previous_cpu = ticks
        status["cpu"]["temp_c"] = read_cpu_temperature(self.sys_root)
        status["memory"] = read_memory(self.proc_root)
        status["uptime_seconds"] = read_uptime(self.proc_root)
        try:
            status["hostname"] = socket.gethostname()
        except OSError:
            pass
        iface, now = default_interface(self.proc_root), self.clock()
        counters = read_network_counters(self.sys_root, iface)
        status["network"].update(interface=iface, ipv4=interface_ipv4(iface))
        if counters is not None and self.previous_network is not None:
            old_iface, old_rx, old_tx, old_time = self.previous_network
            elapsed = now - old_time
            if iface == old_iface and elapsed > 0 and counters[0] >= old_rx and counters[1] >= old_tx:
                status["network"].update(
                    rx_bytes_per_sec=(counters[0] - old_rx) / elapsed,
                    tx_bytes_per_sec=(counters[1] - old_tx) / elapsed,
                )
        self.previous_network = (iface, *counters, now) if counters is not None else None
        status["disks"] = read_disks(self.proc_root)
        status["available"] = ticks is not None or status["memory"]["total_bytes"] is not None or status["uptime_seconds"] is not None
        return status


class StatusMonitor:
    def __init__(self, sampler=None, interval=SAMPLE_INTERVAL):
        self.sampler = sampler or StatusSampler()
        self.interval = interval
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.status = empty_status()
        self.updated_at = None
        self.thread = threading.Thread(target=self._run, name="r1-metrics", daemon=True)

    def _run(self):
        while not self.stopped.is_set():
            try:
                status = self.sampler.sample()
            except Exception as error:
                print("[WARN] 数据采样失败：" + str(error), flush=True)
                status = empty_status()
            with self.lock:
                self.status, self.updated_at = status, time.monotonic()
            self.stopped.wait(self.interval)

    def snapshot(self):
        with self.lock:
            if self.updated_at is None or time.monotonic() - self.updated_at > max(6, self.interval * 3):
                return empty_status()
            return copy.deepcopy(self.status)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=0.25)


def allowed_origin(origin, port):
    if origin is None or origin == "null":
        return True
    try:
        parsed = urllib.parse.urlsplit(origin)
        return (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            and parsed.port == port and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


def make_http_server(monitor, port=PORT):
    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            origin = self.headers.get("Origin")
            if not allowed_origin(origin, self.server.server_port):
                self.send_json(403, {"error": "origin not allowed"})
                return
            if urllib.parse.urlsplit(self.path).path != "/api/status":
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200, monitor.snapshot(), origin)

        def send_json(self, code, value, origin=None):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Vary", "Origin")
            if origin is not None and allowed_origin(origin, self.server.server_port):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def reject_method(self):
            self.send_json(405, {"error": "only GET is supported"})

        def do_OPTIONS(self):
            # Newer Chromium versions can preflight file:// -> loopback reads.
            # Grant only this read route, never a control method or extra headers.
            origin = self.headers.get("Origin")
            if (
                origin is None or not allowed_origin(origin, self.server.server_port)
                or urllib.parse.urlsplit(self.path).path != "/api/status"
                or self.headers.get("Access-Control-Request-Method") != "GET"
                or self.headers.get("Access-Control-Request-Headers", "").strip()
            ):
                self.send_json(403, {"error": "preflight not allowed"})
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = reject_method

        def log_message(self, format_string, *args):
            pass

    class LocalServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return LocalServer((HOST, port), StatusHandler)


def bind_server(monitor, port, stopped, timeout=5):
    deadline = time.monotonic() + timeout
    while not stopped.is_set():
        try:
            return make_http_server(monitor, port)
        except OSError as error:
            remaining = deadline - time.monotonic()
            if error.errno != errno.EADDRINUSE or remaining <= 0:
                raise
            stopped.wait(min(0.2, remaining))
    return None


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except ProcessLookupError:
            return
        child.wait(timeout=1)


def run_service(app, port=PORT):
    app = Path(app).resolve()
    executable = app / "AppRun"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        print("[ERROR] AppRun 不存在或不可执行：" + str(executable), file=sys.stderr, flush=True)
        return 1
    stopped, monitor = threading.Event(), StatusMonitor()
    server = server_thread = child = None
    handlers = {}

    def request_stop(signum, frame):
        stopped.set()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, request_stop)
        server = bind_server(monitor, port, stopped)
        if server is None:
            return 0
        server_thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.1), name="r1-status-api", daemon=True
        )
        monitor.start()
        server_thread.start()
        if stopped.is_set():
            return 0
        environment = dict(os.environ, APPDIR=str(app))
        app_library_path = environment.pop("R1_TOOLBOX_APP_LD_LIBRARY_PATH", None)
        if app_library_path is not None:
            environment["LD_LIBRARY_PATH"] = app_library_path
        child = subprocess.Popen([str(executable), "--no-sandbox"], cwd=app, env=environment)
        print("[OK] 只读状态接口：http://" + HOST + ":" + str(server.server_port) + "/api/status", flush=True)
        while not stopped.wait(0.1):
            result = child.poll()
            if result is not None:
                return result if result >= 0 else 128 - result
        return 0
    except (OSError, ValueError) as error:
        print("[ERROR] 小屏服务启动失败：" + str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        stop_child(child)
        if server_thread is not None and server_thread.is_alive():
            server.shutdown()
            server_thread.join(timeout=0.5)
        if server is not None:
            server.server_close()
        if monitor.thread.is_alive():
            monitor.stop()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/screen/squashfs-root"), help="宿主解包版 AppImage 目录")
    parser.add_argument("--port", type=int, default=PORT, help="本机只读状态端口（默认 18731）")
    parser.add_argument("--check", action="store_true", help="只读取一次指标，不启动小屏或 HTTP 服务")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 范围内")
    if args.check:
        print(json.dumps(StatusSampler().sample(), ensure_ascii=False, allow_nan=False))
        return 0
    return run_service(args.app, args.port)


if __name__ == "__main__":
    sys.exit(main())
