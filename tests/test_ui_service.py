#!/usr/bin/env python3
"""Persistent metric/API regressions using fixture proc/sys trees and loopback.

Run: python3 -m unittest discover -s tests -p 'test_ui_service.py'
No NAS, Docker, systemd, or real screen application is used.
"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ui_service_under_test", ROOT / "runtime/ui_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="r1-ui-service-test-"))
        self.addCleanup(self.recycle_fixture)
        self.proc, self.sys = self.temporary / "proc", self.temporary / "sys"
        (self.proc / "net").mkdir(parents=True)
        (self.proc / "self").mkdir()
        self.sys.mkdir()
        self.write_cpu(100, 0, 50, 850)
        (self.proc / "meminfo").write_text(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 400 kB\n"
        )
        (self.proc / "uptime").write_text("123.5 500.0\n")
        (self.proc / "self/mountinfo").write_text("")
        self.write_routes()
        self.write_counters("eth0", 1000, 2000)
        self.write_counters("eth1", 4000, 8000)
        self.write_counters("docker0", 10**9, 10**9)
        self.now = 10
        self.sampler = service.StatusSampler(self.proc, self.sys, clock=lambda: self.now)
        self.address = mock.patch.object(service, "interface_ipv4", return_value="192.0.2.10")
        self.hostname = mock.patch.object(service.socket, "gethostname", return_value="fixture-nas")
        self.address.start()
        self.hostname.start()
        self.addCleanup(self.address.stop)
        self.addCleanup(self.hostname.stop)
        self.real_web_factory = service.create_web_config
        web_factory = mock.patch.object(service, "create_web_config", return_value=None)
        self.web_factory = web_factory.start()
        self.addCleanup(web_factory.stop)

    def recycle_fixture(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            destination = trash / self.temporary.name
            if not destination.exists():
                shutil.move(str(self.temporary), str(destination))
                return
        print("测试临时目录已保留：", self.temporary)

    def write_cpu(self, user, nice, system, idle, *, guest=0):
        (self.proc / "stat").write_text(
            f"cpu {user} {nice} {system} {idle} 0 0 0 0 {guest} 0\n"
            "cpu0 0 0 0 0 0 0 0 0\n"
        )

    def write_routes(self, preferred="eth0"):
        other = "eth1" if preferred == "eth0" else "eth0"
        (self.proc / "net/route").write_text(
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            f"{preferred} 00000000 010200C0 0003 0 0 10 00000000 0 0 0\n"
            f"{other} 00000000 010200C0 0003 0 0 100 00000000 0 0 0\n"
            "docker0 000011AC 00000000 0001 0 0 0 0000FFFF 0 0 0\n"
            "lo 00000000 00000000 0001 0 0 0 00000000 0 0 0\n"
        )

    def write_counters(self, interface, rx, tx):
        folder = self.sys / "class/net" / interface / "statistics"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "rx_bytes").write_text(str(rx))
        (folder / "tx_bytes").write_text(str(tx))

    def test_cpu_first_sample_delta_and_counter_reset(self):
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])
        # Guest ticks are already included in user: adding them again would
        # yield a different utilization here.
        self.write_cpu(150, 0, 100, 950, guest=100)
        self.assertAlmostEqual(self.sampler.sample()["cpu"]["usage_percent"], 50)
        self.write_cpu(10, 0, 10, 80)
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])
        self.write_cpu(20, 0, 20, 160)
        self.assertAlmostEqual(self.sampler.sample()["cpu"]["usage_percent"], 20)

    def test_cpu_unchanged_or_invalid_samples_remain_unknown(self):
        self.sampler.sample()
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])
        self.write_cpu(10, 0, 50, 1000)
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])
        (self.proc / "stat").write_text("cpu invalid 0 0 0\n")
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])
        self.write_cpu(150, 0, 100, 950)
        self.assertIsNone(self.sampler.sample()["cpu"]["usage_percent"])

    def test_memory_uses_available_and_missing_temperature_is_null(self):
        status = self.sampler.sample()
        self.assertEqual(status["memory"]["total_bytes"], 1000 * 1024)
        self.assertEqual(status["memory"]["used_bytes"], 600 * 1024)
        self.assertAlmostEqual(status["memory"]["usage_percent"], 60)
        self.assertIsNone(status["cpu"]["temp_c"])
        for available in ("", "MemAvailable: 1001 kB\n"):
            with self.subTest(available=available):
                (self.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 900 kB\n" + available)
                memory = service.read_memory(self.proc)
                self.assertIsNone(memory["used_bytes"])
                self.assertIsNone(memory["usage_percent"])

    def test_network_uses_default_route_without_counting_docker_twice(self):
        first = self.sampler.sample()["network"]
        self.assertEqual(first["interface"], "eth0")
        self.assertIsNone(first["rx_bytes_per_sec"])
        self.write_counters("eth0", 3000, 6000)
        self.write_counters("docker0", 2 * 10**9, 2 * 10**9)
        self.now += 2
        rates = self.sampler.sample()["network"]
        self.assertEqual(rates["rx_bytes_per_sec"], 1000)
        self.assertEqual(rates["tx_bytes_per_sec"], 2000)
        self.write_counters("eth0", 5, 10)
        self.now += 2
        reset = self.sampler.sample()["network"]
        self.assertIsNone(reset["rx_bytes_per_sec"])
        self.assertIsNone(reset["tx_bytes_per_sec"])
        self.write_counters("eth0", 105, 210)
        self.now += 2
        self.assertEqual(self.sampler.sample()["network"]["rx_bytes_per_sec"], 50)
        self.write_routes(preferred="eth1")
        self.now += 2
        changed = self.sampler.sample()["network"]
        self.assertEqual(changed["interface"], "eth1")
        self.assertIsNone(changed["rx_bytes_per_sec"])

    def test_network_zero_elapsed_time_is_unknown(self):
        self.sampler.sample()
        self.write_counters("eth0", 3000, 6000)
        status = self.sampler.sample()["network"]
        self.assertIsNone(status["rx_bytes_per_sec"])
        self.assertIsNone(status["tx_bytes_per_sec"])

    def test_data_disk_capacity_deduplicates_devices_and_ignores_virtual_mounts(self):
        (self.proc / "self/mountinfo").write_text(
            "10 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "11 1 8:2 / /drives/data rw - ext4 /dev/sdb1 rw\n"
            "12 11 8:2 /share /drives/data-share rw - ext4 /dev/sdb1 rw\n"
            "13 1 8:3 / /drives/archive\\040disk rw - ext4 /dev/sdc1 rw\n"
            "14 1 0:40 / /drives/cache rw - tmpfs tmpfs rw\n"
            "15 1 0:41 / /drives/containers rw - overlay overlay rw\n"
        )
        stats = types.SimpleNamespace(f_frsize=4096, f_bsize=4096,
                                      f_blocks=100, f_bfree=25, f_bavail=10)
        statvfs = mock.Mock(return_value=stats)
        disks = service.read_disks(self.proc, statvfs=statvfs)
        self.assertEqual({disk["mount"] for disk in disks}, {"/drives/data", "/drives/archive disk"})
        self.assertEqual(statvfs.call_count, 2)
        for disk in disks:
            self.assertEqual(disk["total_bytes"], 100 * 4096)
            self.assertEqual(disk["used_bytes"], 75 * 4096)
            self.assertEqual(disk["usage_percent"], 75)

    def start_api(self):
        monitor = types.SimpleNamespace(snapshot=lambda: self.sampler.sample())
        server = service.make_http_server(monitor, port=0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server.server_port

    def request(self, port, method="GET", path="/api/status", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_status_api_schema_and_cors(self):
        port = self.start_api()
        code, headers, body = self.request(port)
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status = json.loads(body, parse_constant=lambda value: self.fail("non-JSON metric: " + value))
        self.assertEqual(status["schema_version"], 1)
        self.assertTrue(status["available"])
        self.assertEqual(status["hostname"], "fixture-nas")
        self.assertEqual(status["uptime_seconds"], 123.5)
        self.assertIsNone(status["cpu"]["usage_percent"])
        self.assertIsNone(status["cpu"]["temp_c"])
        self.assertEqual(status["memory"]["used_bytes"], 600 * 1024)
        self.assertEqual(status["network"]["interface"], "eth0")
        self.assertEqual(status["disks"], [])
        self.assertIsInstance(status["timestamp"], str)
        for origin in ("null", f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            with self.subTest(origin=origin):
                code, headers, _ = self.request(port, headers={"Origin": origin})
                self.assertEqual(code, 200)
                self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
        for origin in ("https://example.invalid", f"http://127.0.0.1:{port}/path"):
            with self.subTest(origin=origin):
                code, headers, _ = self.request(port, headers={"Origin": origin})
                self.assertEqual(code, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_api_supports_read_only_preflight_and_rejects_control_requests(self):
        port = self.start_api()
        headers = {"Origin": "null", "Access-Control-Request-Method": "GET",
                   "Access-Control-Request-Private-Network": "true"}
        code, received, body = self.request(port, "OPTIONS", headers=headers)
        self.assertEqual(code, 204)
        self.assertEqual(body, b"")
        self.assertEqual(received["Access-Control-Allow-Origin"], "null")
        self.assertEqual(received["Access-Control-Allow-Methods"], "GET")
        self.assertEqual(received["Access-Control-Allow-Private-Network"], "true")
        for requested in ("POST", "DELETE"):
            with self.subTest(preflight=requested):
                code, _, _ = self.request(port, "OPTIONS", headers=dict(headers, **{
                    "Access-Control-Request-Method": requested,
                }))
                self.assertEqual(code, 403)
        for method, path, expected in (("GET", "/api/reboot", 404),
                                       ("POST", "/api/status", 405),
                                       ("POST", "/api/reboot", 405),
                                       ("DELETE", "/api/status", 405),
                                       ("HEAD", "/api/status", 405)):
            with self.subTest(method=method, path=path):
                code, _, _ = self.request(port, method, path)
                self.assertEqual(code, expected)

    def test_help_and_check_never_start_apprun_or_http(self):
        with mock.patch.object(service, "run_service") as run_service, \
                mock.patch.object(service.subprocess, "Popen") as spawn, \
                mock.patch.object(service, "make_http_server") as make_server, \
                mock.patch.object(service, "StatusSampler", return_value=self.sampler):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                service.main(["--help"])
            self.assertEqual(caught.exception.code, 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(service.main(["--check", "--app", str(self.temporary / "not-started")]), 0)
            self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)
            run_service.assert_not_called()
            spawn.assert_not_called()
            make_server.assert_not_called()

    def test_api_closes_when_its_apprun_child_exits(self):
        app = self.temporary / "fake-app"
        app.mkdir()
        launcher = app / "AppRun"
        launcher.write_text(
            '#!/bin/sh\nprintf "%s\\n%s\\n" "$APPDIR" "$PWD" > "$APPDIR/launched"\n'
            'printf "%s" "$LD_LIBRARY_PATH" > "$APPDIR/child-library-path"\n'
            'printf "%s" "$R1_TOOLBOX_CONTROL_TOKEN" > "$APPDIR/control-token"\n'
            'while [ ! -f "$APPDIR/release" ]; do sleep 0.02; done\nexit 7\n'
        )
        launcher.chmod(0o755)
        monitor = service.StatusMonitor(self.sampler, interval=0.02)
        original_make_server = service.make_http_server
        bound, observed = threading.Event(), threading.Event()
        servers, errors = [], []

        def make_server(sampler, port, **options):
            server = original_make_server(sampler, port=0, **options)
            servers.append(server)
            bound.set()
            return server

        def observe():
            try:
                if not bound.wait(2):
                    raise AssertionError("API did not bind")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if not (app / "launched").is_file():
                        time.sleep(0.01)
                        continue
                    try:
                        code, _, _ = self.request(servers[0].server_port)
                        if code == 200:
                            observed.set()
                            return
                    except OSError:
                        pass
                    time.sleep(0.01)
                raise AssertionError("API was not available while AppRun was alive")
            except Exception as error:
                errors.append(error)
            finally:
                (app / "release").write_text("finish")

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        controller = types.SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), snapshot=lambda: {}, action=mock.Mock())
        insights = types.SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), snapshot=lambda: {}, action=mock.Mock())
        with mock.patch.object(service, "StatusMonitor", return_value=monitor), \
                mock.patch.object(service, "make_http_server", side_effect=make_server), \
                mock.patch.dict(os.environ, {"R1_TOOLBOX_APP_LD_LIBRARY_PATH": "/electron/lib"}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = service.run_service(app, port=0, controller=controller, insights=insights)
        observer.join(timeout=3)
        self.assertFalse(observer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(observed.is_set())
        self.assertEqual(result, 7, "the service must preserve AppRun's exit status")
        self.assertEqual((app / "launched").read_text().splitlines(), [str(app.resolve())] * 2)
        self.assertEqual((app / "child-library-path").read_text(), "/electron/lib")
        self.assertRegex((app / "control-token").read_text(), r"^[0-9a-f]{64}$")
        controller.start.assert_called_once()
        controller.stop.assert_called_once()
        insights.start.assert_called_once()
        insights.stop.assert_called_once()
        controller.action.assert_not_called()
        insights.action.assert_not_called()
        self.assertEqual(servers[0].socket.fileno(), -1)
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", servers[0].server_port), timeout=0.2):
                pass

    def test_hardware_cleanup_precedes_child_wait_and_survives_child_cleanup_failure(self):
        app = self.temporary / "failing-cleanup-app"
        app.mkdir()
        launcher = app / "AppRun"
        launcher.write_text("#!/bin/sh\nexit 7\n")
        launcher.chmod(0o755)
        events, servers = [], []
        controller = types.SimpleNamespace(start=mock.Mock(), stop=lambda: events.append("hardware"), snapshot=lambda: {})
        insights = types.SimpleNamespace(start=mock.Mock(), stop=lambda: events.append("insights"), snapshot=lambda: {})
        child = mock.Mock()
        child.poll.return_value = 7
        original = service.make_http_server

        def make_server(*args, **kwargs):
            server = original(*args, **kwargs)
            servers.append(server)
            return server

        def fail_child_cleanup(unused):
            events.append("child")
            raise service.subprocess.TimeoutExpired("fixture child", 1)

        with mock.patch.object(service, "make_http_server", side_effect=make_server), \
                mock.patch.object(service.subprocess, "Popen", return_value=child), \
                mock.patch.object(service, "stop_child", side_effect=fail_child_cleanup), \
                mock.patch.object(service, "StatusMonitor", return_value=service.StatusMonitor(self.sampler, interval=0.02)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = service.run_service(app, port=0, controller=controller, insights=insights)
        self.assertEqual(result, 7)
        self.assertEqual(events, ["hardware", "child", "insights"])
        self.assertEqual(servers[0].socket.fileno(), -1)

    def test_optional_web_port_failure_does_not_abort_screen_and_is_cleaned_up(self):
        app = self.temporary / "web-failure-app"
        app.mkdir()
        launcher = app / "AppRun"
        launcher.write_text("#!/bin/sh\nexit 7\n")
        launcher.chmod(0o755)
        web = mock.Mock()
        web.start.side_effect = OSError("fixture occupied port")
        self.web_factory.return_value = web
        controller = types.SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), snapshot=lambda: {})
        insights = types.SimpleNamespace(start=mock.Mock(), stop=mock.Mock(), snapshot=lambda: {})
        child = mock.Mock()
        child.poll.return_value = 7
        with mock.patch.object(service.subprocess, "Popen", return_value=child), \
                mock.patch.object(service, "stop_child"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = service.run_service(app, port=0, controller=controller, insights=insights)
        self.assertEqual(result, 7)
        web.start.assert_called_once()
        web.stop.assert_called_once()
        controller.stop.assert_called_once()
        insights.stop.assert_called_once()

    def test_web_listener_configuration_is_bounded_and_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"R1_WEB_PORT": "0", "R1_WEB_BIND": "0.0.0.0"}):
            self.assertIsNone(self.real_web_factory(None, None))
        for host, port in (("example.invalid", "18732"), ("::", "18732"),
                           ("0.0.0.0", "65536"), ("0.0.0.0", "80"),
                           ("0.0.0.0", "-1"), ("0.0.0.0", "1.5"),
                           ("0.0.0.0", "１８７３２")):
            with self.subTest(host=host, port=port), mock.patch.dict(
                    os.environ, {"R1_WEB_PORT": port, "R1_WEB_BIND": host}):
                with self.assertRaises(ValueError):
                    self.real_web_factory(None, None)


if __name__ == "__main__":
    unittest.main()
