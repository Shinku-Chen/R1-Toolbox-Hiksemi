"""Exercise local diagnostics with fixture proc/sys trees and fake commands.

These tests never execute Docker, virsh, smartctl, or a NAS power operation.
"""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
SPEC = importlib.util.spec_from_file_location("insights_under_test", ROOT / "runtime/insights.py")
insights = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(insights)

CONTAINER = "a" * 64
PROTECTED = "b" * 64
LABEL_PROTECTED = "c" * 64
VM = "12345678-1234-4234-8234-123456789abc"


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="r1-insights-test-"))
        self.proc, self.sys = self.temporary / "proc", self.temporary / "sys"
        self.proc.mkdir()
        self.sys.mkdir()
        self.now = 10.0
        self.calls = []
        self.responses = {}
        self.programs = {"docker", "smartctl", "virsh"}
        self.collector = insights.InsightsCollector(
            self.proc, self.sys, runner=self.runner,
            command_finder=lambda name: "/fixture/bin/" + name if name in self.programs else None,
            clock=lambda: self.now, ipv4_provider=lambda: {"eth0": ["192.0.2.10", "192.0.2.11", "invalid"]},
            page_size=4096, clock_ticks=100, history_limit=3,
        )
        self.addCleanup(self.recycle_fixture)
        self.addCleanup(self.collector.stop)

    def recycle_fixture(self):
        trash = Path.home() / ".Trash"
        if sys.platform == "darwin" and trash.is_dir():
            destination = trash / self.temporary.name
            if not destination.exists():
                shutil.move(str(self.temporary), str(destination))
                return
        print("测试临时目录已保留：", self.temporary)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding="utf-8")

    def runner(self, argv, timeout=3):
        self.calls.append((list(argv), timeout))
        key = (Path(argv[0]).name, *argv[1:])
        response = self.responses.get(key, (0, ""))
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        return types.SimpleNamespace(returncode=response[0], stdout=response[1], stderr="")

    def respond(self, program, arguments, body, code=0):
        self.responses[(program, *arguments)] = (code, body if isinstance(body, str) else json.dumps(body))

    def cpu(self, user=100, idle=900):
        self.write(self.proc / "stat", f"cpu {user} 0 0 {idle} 0 0 0 0\ncpu0 {user} 0 0 {idle} 0 0 0 0\n")

    def network(self, rx=1000, tx=2000):
        root = self.sys / "class/net/eth0"
        self.write(root / "statistics/rx_bytes", rx)
        self.write(root / "statistics/tx_bytes", tx)
        self.write(root / "address", "02:00:00:00:00:01\n")
        self.write(root / "operstate", "up\n")
        self.write(self.proc / "net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 010200C0 0003 0 0 10 00000000\n")

    def block(self, name="sda", temp=None):
        folder = self.sys / "block" / name
        self.write(folder / "size", "1000000")
        self.write(folder / "device/model", "Fixture disk\n")
        self.write(folder / "queue/rotational", 0 if name.startswith("nvme") else 1)
        if temp is not None:
            self.write(folder / "device/hwmon/hwmon8/temp1_input", temp)
        return folder

    def container_list(self):
        rows = [
            {"ID": CONTAINER, "Names": "media", "Image": "media:stable", "State": "running", "Status": "Up 2 hours", "Ports": "127.0.0.1:8080->80/tcp"},
            {"ID": PROTECTED, "Names": "r1-toolbox", "State": "exited"},
            {"ID": LABEL_PROTECTED, "Names": "renamed-toolbox", "Labels": "com.docker.compose.service=r1-toolbox", "State": "exited"},
        ]
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], "\n".join(json.dumps(row) for row in rows))
        self.respond("docker", ["stats", "--all", "--no-stream", "--format", "{{json .}}"],
                     {"ID": CONTAINER[:12], "CPUPerc": "123.45%", "MemUsage": "12.5MiB / 8GiB", "MemPerc": "1.2%"})
        capability = self.collector._containers()
        with self.collector._lock:
            self.collector._state["containers"] = capability
        return capability

    def vm_list(self):
        self.respond("virsh", ["list", "--all", "--uuid"], VM + "\n")
        self.respond("virsh", ["dominfo", VM], f"Name: fixture-vm\nUUID: {VM}\nState: shut off\n")
        capability = self.collector._vms()
        with self.collector._lock:
            self.collector._state["vms"] = capability
        return capability

    def test_cpu_core_deltas_memory_and_uptime_use_real_fixture_values(self):
        self.cpu()
        self.write(self.proc / "cpuinfo", "model name : Fixture CPU\n")
        self.write(self.proc / "meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n")
        self.write(self.proc / "uptime", "123.25 1000\n")
        self.write(self.proc / "loadavg", "0.10 0.20 0.30 1/42 100\n")
        (self.proc / "100").mkdir()
        self.write(self.sys / "class/dmi/id/sys_vendor", "FixtureVendor")
        self.write(self.sys / "class/dmi/id/product_name", "NAS")
        self.collector._collect_fast()
        self.assertIsNone(self.collector.snapshot()["system"]["cpu_usage_percent"])
        self.cpu(user=150, idle=950)
        self.now += 2
        self.collector._collect_fast()
        system = self.collector.snapshot()["system"]
        self.assertEqual(system["cpu_usage_percent"], 50)
        self.assertEqual(system["cpu_cores"], [{"id": 0, "usage_percent": 50}])
        self.assertEqual(system["cpu_model"], "Fixture CPU")
        self.assertEqual(system["model"], "FixtureVendor NAS")
        self.assertEqual(system["process_count"], 1)
        self.assertEqual(system["task_count"], 42)
        self.assertEqual(system["load_average"], [0.1, 0.2, 0.3])
        self.assertEqual(system["uptime_seconds"], 123.25)
        self.assertEqual(system["memory"]["used_bytes"], 600 * 1024)
        self.assertEqual(system["memory"]["swap_usage_percent"], 25)

    def test_counter_reset_and_missing_memory_stay_unknown(self):
        self.cpu()
        self.network()
        self.collector._collect_fast()
        self.cpu(1, 9)
        self.network(10, 20)
        self.now += 2
        self.collector._collect_fast()
        value = self.collector.snapshot()
        self.assertIsNone(value["system"]["cpu_usage_percent"])
        self.assertIsNone(value["system"]["memory"]["usage_percent"])
        self.assertIsNone(value["interfaces"][0]["rx_bytes_per_sec"])

    def test_device_inventory_is_independent_and_does_not_block_basic_metrics(self):
        self.cpu()
        self.network()
        with mock.patch.object(insights, "collect_device_info", side_effect=OSError("fixture inventory failed")) as collect:
            self.collector._collect_fast()
            collect.assert_not_called()
            name, callback = next((name, callback) for name, callback in self.collector._integrations() if name == "device")
            self.collector._publish_integration(name, callback)
        value = self.collector.snapshot()
        self.assertEqual(value["interfaces"][0]["name"], "eth0")
        self.assertFalse(value["device"]["available"])
        self.assertIn("重试", value["device"]["reason"])
        self.assertIsNotNone(value["device"]["sampled_at"])

    def test_interfaces_expose_all_addresses_counters_and_bounded_history(self):
        self.network()
        for index in range(5):
            self.network(1000 + 200 * index, 2000 + 400 * index)
            self.now += 2
            self.collector._collect_fast()
        value = self.collector.snapshot()
        interface = value["interfaces"][0]
        self.assertEqual(interface["ipv4"], ["192.0.2.10", "192.0.2.11"])
        self.assertEqual(interface["mac"], "02:00:00:00:00:01")
        self.assertEqual(interface["rx_bytes"], 1800)
        self.assertEqual(interface["tx_bytes_per_sec"], 200)
        self.assertEqual(len(value["history"]), 3)
        self.assertEqual(value["history"][-1]["rx_bytes_per_sec"], 100)

    def test_process_stat_handles_parentheses_and_pid_reuse(self):
        def process(ticks, started):
            fields = ["S", "1", "1", "1", "0", "0", "0", "0", "0", "0", "0", str(ticks), "0", "0", "0", "20", "0", "1", "0", str(started), "999999", "20"]
            self.write(self.proc / "42/stat", "42 (worker (NAS)) " + " ".join(fields))
        process(100, 200)
        self.collector._collect_fast()
        self.now += 2
        process(150, 200)
        self.collector._collect_fast()
        row = self.collector.snapshot()["processes"][0]
        self.assertEqual(row["name"], "worker (NAS)")
        self.assertEqual(row["memory_bytes"], 20 * 4096)
        self.assertEqual(row["cpu_percent"], 25)
        self.now += 2
        process(999, 300)
        self.collector._collect_fast()
        self.assertIsNone(self.collector.snapshot()["processes"][0]["cpu_percent"])

    def test_listen_ports_exclude_established_connections_and_decode_ipv6(self):
        self.write(self.proc / "net/tcp", "header\n0: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 0 0 123\n1: 0100007F:0016 00000000:0000 01 0:0 0:0 0 0 0 456\n")
        self.write(self.proc / "net/tcp6", "header\n0: 00000000000000000000000001000000:01BB 0:0 0A 0:0 0:0 0 0 0 789\n")
        self.collector._collect_fast()
        ports = self.collector.snapshot()["ports"]
        self.assertEqual([(row["address"], row["port"]) for row in ports], [("::1", 443), ("127.0.0.1", 8080)])

    def test_gpu_missing_metrics_are_null_not_fake_zero(self):
        path = self.sys / "class/drm/card0/device"
        self.write(path / "vendor", "0x8086")
        self.write(path / "device", "0x1234")
        self.collector._collect_fast()
        gpu = self.collector.snapshot()["gpu"]
        self.assertTrue(gpu["available"])
        self.assertIsNone(gpu["devices"][0]["busy_percent"])
        self.assertIsNone(gpu["devices"][0]["vram_total_bytes"])

    def test_snapshot_is_memory_only_and_returns_a_copy(self):
        self.cpu()
        self.collector._collect_fast()
        first = self.collector.snapshot()
        first["system"]["model"] = "changed"
        first["history"].clear()
        self.assertIsNone(self.collector.snapshot()["system"]["model"])
        self.assertEqual(len(self.collector.snapshot()["history"]), 1)
        self.assertEqual(self.calls, [])

    def test_optional_programs_missing_report_reasons_without_commands(self):
        self.programs.clear()
        self.block()
        self.collector._collect_slow()
        value = self.collector.snapshot()
        self.assertIn("docker", value["containers"]["reason"])
        self.assertIn("virsh", value["vms"]["reason"])
        self.assertIn("smartctl", value["disks"]["items"][0]["health"]["reason"])
        self.assertEqual(self.calls, [])

    def test_smart_standby_command_never_wakes_disk_and_is_cached(self):
        self.block()
        args = ["-n", "standby", "-j", "-a", "/dev/sda"]
        self.respond("smartctl", args, {"smartctl": {"messages": [{"string": "Device is in STANDBY mode, exit(2)"}]}}, 2)
        first = self.collector._disks()["items"][0]
        self.assertTrue(first["standby"])
        self.assertIsNone(first["health"]["passed"])
        self.assertFalse(first["smart_available"])
        self.now += 29
        self.collector._disks()
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        self.collector._disks()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], (["/fixture/bin/smartctl", *args], 3))

    def test_smart_failed_health_is_kept_even_with_nonzero_exit_status(self):
        self.block()
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/sda"], {"smart_status": {"passed": False}, "temperature": {"current": 48}}, 8)
        disk = self.collector._disks()["items"][0]
        self.assertFalse(disk["health"]["passed"])
        self.assertTrue(disk["smart_available"])
        self.assertEqual(disk["temp_c"], 48)

    def test_nvme_temperature_prefers_hwmon_and_excludes_pseudo_disks(self):
        self.block("nvme0n1", temp=42000)
        self.block("loop0")
        self.block("dm-0")
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/nvme0n1"], {"nvme_smart_health_information_log": {"temperature": 90, "critical_warning": 0}})
        disks = self.collector._disks()["items"]
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["temp_c"], 42)
        self.assertTrue(disks[0]["health"]["passed"])
        self.assertEqual(disks[0]["size_bytes"], 512000000)

    def test_smart_timeout_and_invalid_output_are_explicit(self):
        self.block()
        key = ("smartctl", "-n", "standby", "-j", "-a", "/dev/sda")
        self.responses[key] = subprocess.TimeoutExpired("smartctl", 3)
        disk = self.collector._disks()["items"][0]
        self.assertIn("超时", disk["health"]["reason"])
        self.assertIsNone(disk["temp_c"])
        self.now += 31
        self.responses[key] = (0, "not JSON")
        disk = self.collector._disks()["items"][0]
        self.assertIn("无效", disk["health"]["reason"])

    def test_docker_status_stats_and_protected_toolbox_are_real_fields(self):
        capability = self.container_list()
        rows = capability["items"]
        self.assertTrue(capability["available"])
        self.assertEqual(rows[0]["cpu_percent"], 123.45)
        self.assertEqual(rows[0]["memory_bytes"], int(12.5 * 1024 ** 2))
        self.assertEqual(rows[0]["memory_percent"], 1.2)
        self.assertTrue(rows[1]["protected"])
        self.assertTrue(rows[2]["protected"])
        self.assertEqual(rows[0]["ports"], "127.0.0.1:8080->80/tcp")
        self.assertEqual(rows[0]["available_actions"], ["stop", "restart"])
        self.assertEqual(rows[1]["available_actions"], [])
        self.assertEqual(rows[2]["available_actions"], [])

    def test_docker_daemon_error_clears_old_list_and_stats_failure_does_not(self):
        self.container_list()
        self.respond("docker", ["stats", "--all", "--no-stream", "--format", "{{json .}}"], "", 1)
        partial = self.collector._containers()
        self.assertTrue(partial["available"])
        self.assertIsNone(partial["items"][0]["cpu_percent"])
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], "", 1)
        failure = self.collector._containers()
        self.assertFalse(failure["available"])
        self.assertEqual(failure["items"], [])

    def test_container_actions_require_confirmation_known_id_and_allowed_operation(self):
        self.container_list()
        self.calls.clear()
        for payload in [None, {}, {"id": CONTAINER, "operation": "stop", "confirmed": 1},
                        {"id": CONTAINER, "operation": "remove", "confirmed": True},
                        {"id": "--all", "operation": "stop", "confirmed": True},
                        {"id": "a" * 11, "operation": "start", "confirmed": True},
                        {"id": "$(reboot)", "operation": "stop", "confirmed": True},
                        {"id": "d" * 64, "operation": "stop", "confirmed": True}]:
            with self.subTest(payload=payload):
                self.assertFalse(self.collector.action("container.action", payload)["ok"])
        self.assertEqual(self.calls, [])
        result = self.collector.action("container.action", {"id": CONTAINER[:12], "operation": "restart", "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls, [(["/fixture/bin/docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"], 2),
                                      (["/fixture/bin/docker", "container", "restart", CONTAINER], 15)])

    def test_container_actions_recheck_current_state_instead_of_stale_running_cache(self):
        self.container_list()
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"],
                     {"ID": CONTAINER, "Names": "media", "State": "exited"})
        self.calls.clear()
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "restart", "confirmed": True})
        self.assertEqual(result["error"], "invalid_state")
        self.assertTrue(self.collector._refresh["containers"].is_set())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][1], "ps")
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "start", "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[-1], (["/fixture/bin/docker", "container", "start", CONTAINER], 3))

    def test_container_transitional_unknown_and_paused_states_expose_no_actions(self):
        self.container_list()
        for state in ("paused", "restarting", "removing", "dead", "", "unknown"):
            with self.subTest(state=state):
                self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"],
                             {"ID": CONTAINER, "Names": "media", "State": state})
                self.assertEqual(self.collector._containers()["items"][0]["available_actions"], [])
                for operation in ("start", "stop", "restart"):
                    self.calls.clear()
                    result = self.collector.action("container.action", {"id": CONTAINER, "operation": operation, "confirmed": True})
                    self.assertEqual(result["error"], "invalid_state")
                    self.assertEqual(len(self.calls), 1)
                    self.assertEqual(self.calls[0][0][1], "ps")

    def test_container_protection_disappearance_and_service_failure_are_rechecked(self):
        self.container_list()
        for live, code, expected in (({"ID": CONTAINER, "Names": "r1-toolbox", "State": "running"}, 0, "protected_container"),
                                     ({"ID": CONTAINER, "Names": "renamed", "State": "running", "Labels": "com.docker.compose.service=r1-toolbox"}, 0, "protected_container"),
                                     ("", 0, "not_found"), ("", 1, "state_unconfirmed")):
            with self.subTest(expected=expected):
                self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], live, code)
                self.calls.clear()
                result = self.collector.action("container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})
                self.assertEqual(result["error"], expected)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.calls[0][0][1], "ps")

    def test_busy_application_actions_are_rejected_without_queuing_or_blocking_snapshot(self):
        self.container_list()
        self.calls.clear()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        completed = []

        def slow_stop():
            entered.set()
            release.wait(timeout=2)
            return 0, ""

        self.responses[("docker", "container", "stop", CONTAINER)] = slow_stop
        thread = threading.Thread(target=lambda: completed.append(self.collector.action(
            "container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            count, started = len(self.calls), time.monotonic()
            result = self.collector.action("container.action", {"id": CONTAINER, "operation": "restart", "confirmed": True})
            self.collector.snapshot()
            self.assertLess(time.monotonic() - started, .1)
            self.assertEqual(result["error"], "busy")
            self.assertEqual(len(self.calls), count)
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(completed[0]["ok"])

    def test_protected_container_actions_never_execute(self):
        self.container_list()
        self.calls.clear()
        for identifier in (PROTECTED, LABEL_PROTECTED):
            result = self.collector.action("container.action", {"id": identifier, "operation": "stop", "confirmed": True})
            self.assertEqual(result["error"], "protected_container")
        self.assertEqual(self.calls, [])

    def test_ambiguous_container_prefix_never_executes(self):
        self.container_list()
        with self.collector._lock:
            self.collector._state["containers"]["items"].append({"id": "a" * 12 + "d" * 52, "protected": False})
        self.calls.clear()
        result = self.collector.action("container.action", {"id": "a" * 12, "operation": "stop", "confirmed": True})
        self.assertEqual(result["error"], "not_found")
        self.assertEqual(self.calls, [])

    def test_invalid_integration_output_does_not_claim_an_empty_valid_list(self):
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], "broken JSON")
        self.assertFalse(self.collector._containers()["available"])
        self.respond("virsh", ["list", "--all", "--uuid"], "not-a-uuid")
        self.assertFalse(self.collector._vms()["available"])

    def test_vm_uuid_mismatch_cannot_authorize_a_different_domain(self):
        self.respond("virsh", ["list", "--all", "--uuid"], VM)
        self.respond("virsh", ["dominfo", VM], "Name: other\nUUID: 87654321-1234-4234-8234-123456789abc\nState: running")
        result = self.collector._vms()
        self.assertEqual(result["items"], [])
        self.assertIn("暂不可用", result["reason"])

    def test_emmc_has_explicit_no_smart_support_and_no_smart_command(self):
        self.block("mmcblk0")
        disk = self.collector._disks()["items"][0]
        self.assertEqual(disk["type"], "eMMC")
        self.assertFalse(disk["smart_available"])
        self.assertIsNone(disk["health"]["passed"])
        self.assertEqual(self.calls, [])

    def test_probe_error_replaces_a_previous_success_instead_of_leaving_stale_data(self):
        self.container_list()
        def failed():
            raise OSError("fixture command disappeared")
        self.collector._publish_integration("containers", failed)
        state = self.collector.snapshot()["containers"]
        self.assertFalse(state["available"])
        self.assertEqual(state["items"], [])
        self.assertIn("重试", state["reason"])

    def test_vm_list_requires_valid_uuid_and_actions_use_fixed_argv(self):
        capability = self.vm_list()
        self.assertEqual(capability["items"], [{"id": VM, "name": "fixture-vm", "state": "shut off"}])
        self.calls.clear()
        for identifier in ("fixture-vm", "--all", "../domain", "12345678", VM + ";reboot"):
            self.assertFalse(self.collector.action("vm.action", {"id": identifier, "operation": "start", "confirmed": True})["ok"])
        for operation in ("destroy", "undefine", "reset", "remove"):
            self.assertFalse(self.collector.action("vm.action", {"id": VM, "operation": operation, "confirmed": True})["ok"])
        self.assertEqual(self.calls, [])
        self.assertTrue(self.collector.action("vm.action", {"id": VM, "operation": "shutdown", "confirmed": True})["ok"])
        self.assertEqual(self.calls, [(["/fixture/bin/virsh", "shutdown", VM], 3)])

    def test_action_timeout_failure_and_unsupported_are_not_reported_successful(self):
        unsupported = self.collector.action("vm.action", {"id": VM, "operation": "start", "confirmed": True})
        self.assertEqual(unsupported["error"], "unsupported")
        self.assertFalse(self.collector.action("shell.run", {"confirmed": True})["ok"])
        self.container_list()
        self.responses[("docker", "container", "stop", CONTAINER)] = subprocess.TimeoutExpired("docker", 3)
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "operation_unconfirmed")
        self.assertIn("超时", result["reason"])
        self.assertIn("结果未确认", result["reason"])
        self.assertTrue(result["refresh_pending"])
        self.assertTrue(self.collector._refresh["containers"].is_set())
        self.assertEqual([call for call in self.calls if call[0][1:3] == ["container", "stop"]],
                         [(["/fixture/bin/docker", "container", "stop", CONTAINER], 15)])
        self.respond("docker", ["container", "stop", CONTAINER], "private engine details", 125)
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 125)
        self.assertNotIn("private", json.dumps(result))

    def test_background_probe_does_not_block_snapshot_or_fast_metrics(self):
        self.cpu()
        self.block()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow():
            entered.set()
            release.wait(timeout=5)
            return 0, json.dumps({"smart_status": {"passed": True}})
        self.responses[("smartctl", "-n", "standby", "-j", "-a", "/dev/sda")] = slow
        self.collector.start()
        self.assertTrue(entered.wait(timeout=1))
        deadline = time.monotonic() + 1
        while not self.collector.snapshot()["timestamp"] and time.monotonic() < deadline:
            time.sleep(0.01)
        start = time.monotonic()
        value = self.collector.snapshot()
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(value["system"]["cpu_logical_cores"], 1)
        release.set()


if __name__ == "__main__":
    unittest.main()
