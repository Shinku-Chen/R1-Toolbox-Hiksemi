#!/usr/bin/env python3
"""Serve local NAS metrics and authenticated touch controls with the screen app.

The one-shot container starts this script through the host service manager.  It
does not install software or overwrite official screen files. Explicit user
actions can adjust supported hardware, manage workloads and request host power.
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
import secrets
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


def make_http_server(monitor, port=PORT, controller=None, insights=None, control_token=None):
    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def request_origin(self):
            origin = self.headers.get("Origin")
            try:
                host = urllib.parse.urlsplit("http://" + self.headers.get("Host", ""))
                valid_host = (host.hostname in ("127.0.0.1", "localhost", "::1")
                              and host.port == self.server.server_port and not host.username
                              and not host.password and not host.path and not host.query and not host.fragment)
            except ValueError:
                valid_host = False
            if not valid_host or not allowed_origin(origin, self.server.server_port):
                self.send_json(403, {"error": "请求来源不允许"})
                return False, None
            return True, origin

        def do_GET(self):
            valid, origin = self.request_origin()
            if not valid:
                return
            route = urllib.parse.urlsplit(self.path).path
            if route == "/api/status":
                value = monitor.snapshot()
            elif route == "/api/toolbox" and controller is not None:
                value = controller.snapshot()
            elif route == "/api/insights" and insights is not None:
                value = insights.snapshot()
            else:
                self.send_json(404, {"error": "接口不存在"}, origin)
                return
            self.send_json(200, value, origin)

        def do_POST(self):
            if controller is None or not control_token:
                self.reject_method()
                return
            valid, origin = self.request_origin()
            if not valid:
                return
            if urllib.parse.urlsplit(self.path).path != "/api/action":
                self.send_json(404, {"error": "接口不存在"}, origin)
                return
            supplied = self.headers.get("X-R1-Token", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), control_token.encode("utf-8")):
                self.send_json(403, {"ok": False, "error": "控制凭据无效，请在小屏操作"}, origin)
                return
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isascii() or not length.isdigit() or len(length) > 5 or not 0 < int(length) <= 16384:
                self.send_json(413, {"ok": False, "error": "请求内容过大或长度无效"}, origin)
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.send_json(415, {"ok": False, "error": "控制请求需要 JSON"}, origin)
                return
            try:
                def unique_fields(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("重复字段")
                        value[key] = item
                    return value
                payload = json.loads(self.rfile.read(int(length)), object_pairs_hook=unique_fields)
                if not isinstance(payload, dict):
                    raise ValueError("需要对象")
                action = payload.pop("action", None)
                if action in ("power.request", "container.action", "vm.action", "fan.official") and payload.get("confirmed") is not True:
                    raise ValueError("此操作需要在界面明确确认")
                if not isinstance(action, str):
                    raise ValueError("缺少操作名称")
            except (ValueError, UnicodeError):
                self.send_json(400, {"ok": False, "error": "请求格式无效或缺少明确确认"}, origin)
                return
            allowed_controls = {"settings.update", "screen.brightness", "screen.sleep", "screen.wake",
                                "screen.activity", "fan.set", "fan.restore", "fan.official", "rgb.set", "rgb.probe", "power.request", "power.cancel"}
            if action in allowed_controls:
                destination = controller
            elif action in ("container.action", "vm.action") and insights is not None:
                destination = insights
            else:
                self.send_json(400, {"ok": False, "error": "不支持此操作"}, origin)
                return
            try:
                value = destination.action(action, payload)
                if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                    raise ValueError("控制器返回格式错误")
                if action in ("container.action", "vm.action") and value["ok"]:
                    value = {**value, "operation": value.get("state"),
                             "state": controller.snapshot(), "insights": insights.snapshot()}
                self.send_json(200 if value["ok"] else 400, value, origin)
            except Exception:
                # Keep privileged command output and paths out of the browser.
                self.send_json(500, {"ok": False, "error": "操作未完成，请检查宿主服务日志"}, origin)

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
            valid, origin = self.request_origin()
            if not valid:
                return
            route = urllib.parse.urlsplit(self.path).path
            method = self.headers.get("Access-Control-Request-Method")
            headers = {part.strip().lower() for part in self.headers.get("Access-Control-Request-Headers", "").split(",") if part.strip()}
            can_read = route in ("/api/status", "/api/toolbox", "/api/insights") and method == "GET" and not headers
            can_control = (route == "/api/action" and controller is not None and control_token
                           and method == "POST" and headers <= {"content-type", "x-r1-token"})
            if (
                origin is None or not allowed_origin(origin, self.server.server_port)
                or not (can_read or can_control)
            ):
                self.send_json(403, {"error": "preflight not allowed"})
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST" if can_control else "GET")
            if can_control:
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-R1-Token")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        do_PUT = do_PATCH = do_DELETE = do_HEAD = reject_method

        def log_message(self, format_string, *args):
            pass

    class LocalServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

    return LocalServer((HOST, port), StatusHandler)


def bind_server(monitor, port, stopped, timeout=20, **options):
    deadline = time.monotonic() + timeout
    while not stopped.is_set():
        try:
            return make_http_server(monitor, port, **options)
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


def run_service(app, port=PORT, controller=None, insights=None):
    app = Path(app).resolve()
    executable = app / "AppRun"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        print("[ERROR] AppRun 不存在或不可执行：" + str(executable), file=sys.stderr, flush=True)
        return 1
    # Companions live beside this verified script in the RAM bundle, including
    # when tests import ui_service.py without using it as the process entrypoint.
    runtime_dir = str(Path(__file__).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    if controller is None:
        from toolbox import ToolboxController
        from histor import HistorAdapter
        controller = ToolboxController(state_dir=os.environ.get("R1_TOOLBOX_DATA_DIR", "/run/r1-toolbox/settings"),
                                       histor=HistorAdapter(allow_writes=True))
    if insights is None:
        from insights import InsightsCollector
        insights = InsightsCollector()
    stopped, monitor = threading.Event(), StatusMonitor()
    control_token = secrets.token_hex(32)
    server = server_thread = child = None
    handlers = {}

    def request_stop(signum, frame):
        stopped.set()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, request_stop)
        server = bind_server(monitor, port, stopped, controller=controller, insights=insights, control_token=control_token)
        if server is None:
            return 0
        server_thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.1), name="r1-status-api", daemon=True
        )
        monitor.start()
        controller.start()
        insights.start()
        server_thread.start()
        if stopped.is_set():
            return 0
        environment = dict(os.environ, APPDIR=str(app))
        environment["R1_TOOLBOX_CONTROL_TOKEN"] = control_token
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
        # Keep the port bound until hardware is restored, so a replacement
        # service cannot race the previous instance's cleanup.
        def stop_http():
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
                server_thread.join(timeout=0.5)
        cleanup = [("停止接收控制请求", stop_http), ("恢复硬件", controller.stop),
                   ("停止小屏进程", lambda: stop_child(child)), ("停止详细采集", insights.stop)]
        if monitor.thread.is_alive():
            cleanup.append(("停止指标采集", monitor.stop))
        if server is not None:
            cleanup.append(("释放本地接口", server.server_close))
        for description, action in cleanup:
            try:
                action()
            except Exception as error:
                # One failure must not skip fan/screen restoration or leave
                # the remaining service resources alive.
                print("[WARN] " + description + "未完成：" + type(error).__name__, file=sys.stderr, flush=True)
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
