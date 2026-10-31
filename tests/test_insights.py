"""Exercise local diagnostics with fixture proc/sys trees and fake commands.

These tests never execute Docker, virsh, smartctl, or a NAS power operation.
"""

import importlib.util
import copy
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
DOCKER = ["/fixture/bin/docker", "--host", "unix:///var/run/docker.sock"]


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

    @staticmethod
    def response_key(program, arguments):
        if program == "docker":
            arguments = [*DOCKER[1:], *arguments]
        return (program, *arguments)

    def respond(self, program, arguments, body, code=0):
        self.responses[self.response_key(program, arguments)] = (code, body if isinstance(body, str) else json.dumps(body))
        if program == 'smartctl':
            # Seed only through the real explicit entry; passive disk reads
            # below must never be responsible for launching this fake command.
            self.collector.refresh_smart(authorizer=lambda: True)

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
        self.write(folder / 'dev', '259:0' if name.startswith('nvme') else '8:0')
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
        self.assertEqual(value["disks"]["items"][0]["health"]["reason"], "尚无有效的 SMART 观测缓存")
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
        self.assertEqual(len(self.calls), 1)
        self.collector.refresh_smart(authorizer=lambda: True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], (["/fixture/bin/smartctl", *args], 3))

    def test_smart_cached_keeps_full_json_for_detail_endpoint(self):
        self.block()
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/sda"], {
            'smart_status': {'passed': True},
            'ata_smart_attributes': {'table': [{
                'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 100,
                'worst': 100, 'thresh': 10, 'when_failed': '-',
                'raw': {'value': 0, 'string': '0'},
            }]},
            'power_on_time': {'hours': 123},
        })
        self.collector._disks()
        cached = self.collector.smart_cached('sda')
        self.assertIn('ata_smart_attributes', cached['smart_data'])
        self.assertEqual(cached['smart_data']['power_on_time']['hours'], 123)
        cached['smart_data']['power_on_time']['hours'] = 999
        self.assertEqual(self.collector.smart_cached('sda')['smart_data']['power_on_time']['hours'], 123)

    def storage_names_fixture(self, serial="SERIAL-A"):
        data = {"available": True, "reason": None, "source": "hiksemi_storage_socket",
                "disks": [{"disk_name": "sda", "disk_port": 1, "disk_serial": serial}],
                "spaces": [{"space_name": "家庭照片", "space_disk_id": serial,
                            "is_raid": 0, "port": 1,
                            "member": [{"port": 1, "disk_name": "sda2", "root_name": "sda", "disk_id": serial}]}]}
        client = mock.Mock()
        client.snapshot.return_value = data
        self.collector.storage_client = client
        return client

    def identity_smart_fixture(self, serial="SERIAL-A", standby=False):
        data = {"serial_number": serial, "smart_status": {"passed": True},
                "wwn": {"naa": 5, "oui": 123, "id": 456}, "temperature": {"current": 38}}
        if standby:
            data = {"power_mode": {"string": "STANDBY"}}
            if serial is not None:
                data["serial_number"] = serial
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/sda"], data, 2 if standby else 0)

    def test_disk_names_reuse_current_smart_serial_with_no_sysfs_serial_or_new_probe(self):
        self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        row = self.collector._disks()["items"][0]
        self.assertEqual(row["user_name"], "家庭照片")
        self.assertEqual(row["name_source"], "hiksemi_storage_socket")
        self.assertNotIn("serial_number", row)
        self.assertNotIn("smart_data", row)
        self.assertNotIn("serial", row)
        before = list(self.calls)
        diagnostic = self.collector.storage_name_diagnostics()
        self.assertEqual(self.calls, before, "诊断不能新增 SMART 或硬件命令")
        client.snapshot.assert_has_calls([mock.call(), mock.call()])
        self.assertEqual(diagnostic["items"][0]["user_name"], "家庭照片")
        self.assertEqual(diagnostic["items"][0]["serial_status"], "ok")
        self.assertIn("smart_cache", diagnostic["items"][0]["serial_sources"])
        self.assertNotIn("SERIAL-A", json.dumps(diagnostic))
        self.assertNotIn("disk_serial", json.dumps(diagnostic))
        self.assertEqual(diagnostic["items"][0]["serial_hash"], diagnostic["vendor"]["disks"][0]["serial_hash"])

    def test_smart_serial_never_overrides_sysfs_udev_conflicts(self):
        folder = self.block()
        self.write(folder / "device/serial", "SERIAL-A")
        self.write(folder / "dev", "8:0")
        self.write(self.collector.run_root / "udev/data/b8:0", "E:ID_SERIAL_SHORT=OTHER\n")
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.assertIsNone(self.collector._disks()["items"][0]["user_name"])
        result = self.collector.storage_name_diagnostics()["items"][0]
        self.assertEqual(result["serial_status"], "conflict")
        self.assertFalse(result["matched"])
        self.assertEqual(self.collector._disk_serial_cache, {})

    def test_explicit_storage_identity_returns_only_verified_cached_serial_without_smart(self):
        self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        overview = self.collector._disks()
        before = list(self.calls)
        with mock.patch.object(self.collector, '_run', side_effect=AssertionError('no command allowed')):
            value = self.collector.storage_identity_diagnostics()
            default = self.collector.storage_name_diagnostics()
        self.assertTrue(value['available'])
        self.assertEqual(value['source'], 'insights_cache+hiksemi_storage_socket')
        self.assertEqual(value['commands'], ['get_disk_list;', 'get_space_list;'])
        row = value['items'][0]
        self.assertEqual(set(row), {'id', 'device', 'user_name', 'space_names', 'name_source',
                                    'serial', 'serial_status', 'serial_sources', 'matched'})
        self.assertEqual(row['device'], '/dev/sda')
        self.assertEqual(row['user_name'], '家庭照片')
        self.assertEqual(row['serial'], 'SERIAL-A')
        self.assertEqual(row['serial_status'], 'ok')
        self.assertTrue(row['matched'])
        self.assertEqual(row['serial_sources'], ['smart_cache', 'smart_identity_cache', 'hiksemi_storage_socket'])
        self.assertNotIn('SERIAL-A', json.dumps(overview))
        self.assertNotIn('SERIAL-A', json.dumps(default))
        self.assertNotIn('smart_data', json.dumps(value))
        self.assertNotIn('vendor', value)
        self.assertEqual(self.calls, before)
        self.assertEqual(client.snapshot.call_count, 3)

    def test_storage_identity_keeps_verified_standby_serial_without_refresh(self):
        self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        self.now += 31
        self.identity_smart_fixture(None, standby=True)
        self.collector._disks()
        before = list(self.calls)
        row = self.collector.storage_identity_diagnostics()['items'][0]
        self.assertEqual(row['serial'], 'SERIAL-A')
        self.assertEqual(row['serial_sources'], ['smart_identity_cache', 'hiksemi_storage_socket'])
        self.assertTrue(row['matched'])
        self.assertEqual(self.calls, before)

    def test_storage_identity_conflicting_sources_never_disclose_either_serial(self):
        folder = self.block()
        self.write(folder / 'device/serial', 'SERIAL-A')
        self.write(folder / 'dev', '8:0')
        self.write(self.collector.run_root / 'udev/data/b8:0', 'E:ID_SERIAL_SHORT=OTHER\n')
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        before = list(self.calls)
        value = self.collector.storage_identity_diagnostics()
        row = value['items'][0]
        self.assertFalse(value['available'])
        self.assertEqual(row['serial_status'], 'conflict')
        self.assertIsNone(row['serial'])
        self.assertIsNone(row['user_name'])
        self.assertFalse(row['matched'])
        self.assertNotIn('SERIAL-A', json.dumps(value))
        self.assertNotIn('OTHER', json.dumps(value))
        self.assertEqual(self.calls, before)

    def test_storage_identity_requires_unique_matching_vendor_serial_and_port(self):
        self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        original = copy.deepcopy(client.snapshot.return_value)
        cases = [
            [{'disk_name': 'sda', 'disk_port': 1, 'disk_serial': 'OTHER'}],
            [*original['disks'], {'disk_name': 'sdb', 'disk_port': 2, 'disk_serial': 'SERIAL-A'}],
            [*original['disks'], {'disk_name': 'sdb', 'disk_port': 1, 'disk_serial': 'OTHER'}],
            [*original['disks'], *original['disks']],
        ]
        before = list(self.calls)
        for rows in cases:
            with self.subTest(rows=rows):
                client.snapshot.return_value = {**original, 'disks': rows}
                value = self.collector.storage_identity_diagnostics()
                row = value['items'][0]
                self.assertEqual(row['serial_status'], 'conflict')
                self.assertIsNone(row['serial'])
                self.assertFalse(row['matched'])
                self.assertIsNone(row['user_name'])
                self.assertNotIn('SERIAL-A', json.dumps(value))
                self.assertNotIn('OTHER', json.dumps(value))
        self.assertEqual(self.calls, before)

    def test_storage_identity_does_not_fill_missing_or_unconfirmed_sources(self):
        self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        before = list(self.calls)
        original = copy.deepcopy(client.snapshot.return_value)
        for vendor, status in (({'available': False, 'error_code': 'timeout'}, 'vendor_unavailable'),
                               ({**original, 'disks': []}, 'vendor_missing')):
            with self.subTest(status=status):
                client.snapshot.return_value = vendor
                row = self.collector.storage_identity_diagnostics()['items'][0]
                self.assertIsNone(row['serial'])
                self.assertEqual(row['serial_status'], status)
                self.assertFalse(row['matched'])
        client.snapshot.return_value = original
        cached = self.collector._smart_cache['sda']
        self.collector._smart_cache['sda'] = (cached[0], cached[1], ('foreign-lifetime', None))
        self.collector._disk_serial_cache.clear()
        row = self.collector.storage_identity_diagnostics()['items'][0]
        self.assertIsNone(row['serial'])
        self.assertEqual(row['serial_status'], 'missing')
        self.assertEqual(self.calls, before)

    def test_storage_identity_replacement_before_or_during_getters_clears_serial(self):
        folder = self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        vendor = copy.deepcopy(client.snapshot.return_value)
        before = list(self.calls)

        def replace_during_getter():
            folder.rename(self.temporary / 'removed-identity-getter')
            self.block()
            return vendor

        client.snapshot.side_effect = replace_during_getter
        value = self.collector.storage_identity_diagnostics()
        row = value['items'][0]
        self.assertEqual(row['serial_status'], 'identity_changed')
        self.assertIsNone(row['serial'])
        self.assertIsNone(row['user_name'])
        self.assertNotIn('SERIAL-A', json.dumps(value))
        client.snapshot.side_effect = None
        row = self.collector.storage_identity_diagnostics()['items'][0]
        self.assertEqual(row['serial_status'], 'identity_changed')
        self.assertIsNone(row['serial'])
        self.assertEqual(self.calls, before)

    def test_storage_identity_final_lifetime_check_after_serial_read_is_required(self):
        folder = self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        original = self.collector._disk_serial_info
        before = list(self.calls)

        def replace_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            folder.rename(self.temporary / 'removed-full-serial')
            self.block()
            return result

        with mock.patch.object(self.collector, '_disk_serial_info', side_effect=replace_after_read):
            row = self.collector.storage_identity_diagnostics()['items'][0]
        self.assertEqual(row['serial_status'], 'identity_changed')
        self.assertIsNone(row['serial'])
        self.assertIsNone(row['user_name'])
        self.assertFalse(row['matched'])
        self.assertEqual(self.calls, before)

    def test_storage_identity_can_verify_a_disk_without_an_assigned_user_name(self):
        self.block()
        client = self.storage_names_fixture()
        client.snapshot.return_value['spaces'] = []
        self.identity_smart_fixture()
        self.collector._disks()
        row = self.collector.storage_identity_diagnostics()['items'][0]
        self.assertEqual(row['serial'], 'SERIAL-A')
        self.assertTrue(row['matched'])
        self.assertIsNone(row['user_name'])
        self.assertEqual(row['space_names'], [])

    def test_owner_web_identity_uses_private_background_cache_without_any_query(self):
        self.block()
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        overview = self.collector._disks()
        self.collector._state['disks'] = overview
        self.collector._state['device'] = {'storage': {'devices': [{'name': 'sda', 'serial': 'SERIAL-A'}]}}
        before = list(self.calls)
        with mock.patch.object(self.collector, '_run', side_effect=AssertionError('no command allowed')):
            with mock.patch.object(client, 'snapshot', side_effect=AssertionError('no vendor IPC allowed')):
                value = self.collector.storage_identity_cached()
        self.assertTrue(value['available'])
        self.assertEqual(value['items'][0]['serial'], 'SERIAL-A')
        self.assertEqual(value['items'][0]['serial_status'], 'ok')
        self.assertTrue(value['items'][0]['matched'])
        self.assertIn('hiksemi_storage_socket', value['items'][0]['serial_sources'])
        self.assertNotIn('SERIAL-A', json.dumps(overview))
        self.assertNotIn('SERIAL-A', json.dumps(self.collector.snapshot()))
        self.assertIsNone(self.collector.snapshot()['device']['storage']['devices'][0]['serial'])
        self.assertEqual(self.collector._state['device']['storage']['devices'][0]['serial'], 'SERIAL-A')
        self.assertEqual(self.calls, before)
        value['items'][0]['serial'] = 'mutated-copy'
        self.assertEqual(self.collector.storage_identity_cached()['items'][0]['serial'], 'SERIAL-A')

    def test_owner_web_identity_keeps_standby_serial_without_refreshing_smart(self):
        self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        self.now += 31
        self.identity_smart_fixture(None, standby=True)
        self.collector._disks()
        before = list(self.calls)
        row = self.collector.storage_identity_cached()['items'][0]
        self.assertEqual(row['serial'], 'SERIAL-A')
        self.assertTrue(row['matched'])
        self.assertEqual(self.calls, before)

    def test_owner_web_identity_never_fills_vendor_only_or_conflicting_cache(self):
        self.block()
        client = self.storage_names_fixture()
        self.collector._disks()
        self.assertIsNone(self.collector.storage_identity_cached()['items'][0]['serial'])
        self.now += 31
        self.identity_smart_fixture()
        client.snapshot.return_value['disks'].append({'disk_name': 'sdb', 'disk_port': 2, 'disk_serial': 'SERIAL-A'})
        self.collector._disks()
        row = self.collector.storage_identity_cached()['items'][0]
        self.assertIsNone(row['serial'])
        self.assertEqual(row['serial_status'], 'conflict')
        self.assertFalse(row['matched'])
        client.snapshot.return_value = {'available': False, 'reason': 'fixture unavailable'}
        self.collector._disks()
        row = self.collector.storage_identity_cached()['items'][0]
        self.assertIsNone(row['serial'])
        self.assertEqual(row['serial_status'], 'vendor_unavailable')

    def test_owner_web_identity_rechecks_sources_and_invalidates_conflict(self):
        folder = self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        self.write(folder / 'device/serial', 'OTHER')
        row = self.collector.storage_identity_cached()['items'][0]
        self.assertIsNone(row['serial'])
        self.assertIn(row['serial_status'], ('conflict', 'identity_changed'))
        self.assertEqual(self.collector._disk_verified_serials, {})
        (folder / 'device/serial').unlink()
        self.assertIsNone(self.collector.storage_identity_cached()['items'][0]['serial'])

    def test_owner_web_identity_replacement_during_read_is_not_disclosed(self):
        folder = self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        original = self.collector._disk_serial_info

        def replace_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            folder.rename(self.temporary / 'removed-owner-serial')
            self.block()
            return result

        before = list(self.calls)
        with mock.patch.object(self.collector, '_disk_serial_info', side_effect=replace_after_read):
            row = self.collector.storage_identity_cached()['items'][0]
        self.assertEqual(row['serial_status'], 'identity_changed')
        self.assertIsNone(row['serial'])
        self.assertFalse(row['matched'])
        self.assertEqual(self.calls, before)

    def test_standby_without_serial_keeps_only_verified_identity_not_temperature(self):
        self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.assertEqual(self.collector._disks()["items"][0]["user_name"], "家庭照片")
        self.now += 31
        self.identity_smart_fixture(None, standby=True)
        row = self.collector._disks()["items"][0]
        self.assertEqual(row["user_name"], "家庭照片")
        self.assertTrue(row["standby"])
        self.assertIsNone(row["temp_c"])
        self.assertIsNone(row["smart_temperature_c"])
        self.assertIsNone(row["health"]["passed"])
        self.assertNotIn("serial_number", self.collector.smart_cached("sda")["smart_data"])
        before = len(self.calls)
        item = self.collector.storage_name_diagnostics()["items"][0]
        self.assertEqual(item["serial_sources"], ["smart_identity_cache"])
        self.assertEqual(item["user_name"], "家庭照片")
        self.assertEqual(len(self.calls), before)

    def test_hot_replacement_invalidates_names_and_serial_cache_even_at_same_sd_name(self):
        folder = self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        folder.rename(self.temporary / "removed-disk")
        self.block()
        calls = list(self.calls)
        item = self.collector.storage_name_diagnostics()["items"][0]
        self.assertEqual(item["serial_status"], "identity_changed")
        self.assertIsNone(item["user_name"])
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.collector._disk_serial_cache, {})
        self.identity_smart_fixture(None, standby=True)
        row = self.collector._disks()["items"][0]
        self.assertIsNone(row["user_name"])
        self.assertNotIn("sda", self.collector._disk_serial_cache)
        self.assertEqual(len(self.calls), len(calls) + 1, "原有后台采集因新 sysfs lifetime 失效，名称不额外探测")

    def test_identity_change_during_getters_discards_otherwise_matching_names(self):
        folder = self.block()
        client = self.storage_names_fixture()
        data = client.snapshot.return_value
        self.identity_smart_fixture()

        def replace_during_getter():
            folder.rename(self.temporary / "removed-during-getter")
            self.block()
            return data

        client.snapshot.side_effect = replace_during_getter
        before = list(self.calls)
        result = self.collector._disks()
        self.assertEqual(result['items'], [], 'a changed disk invalidates the complete sampled row')
        self.assertNotIn('家庭照片', json.dumps(result))
        self.assertEqual(self.collector._disk_serial_cache, {})
        self.assertEqual(self.collector._disk_name_context, {})
        self.assertEqual(self.collector._disk_verified_serials, {})
        client.snapshot.side_effect = None
        replacement = self.collector._disks()['items'][0]
        self.assertIsNone(replacement['user_name'])
        self.assertIsNone(self.collector._disk_verified_serials['sda']['serial'])
        self.assertEqual(self.calls, before, 'replacement metadata cannot trigger SMART')

    def test_diagnostic_final_identity_check_rejects_change_after_serial_read(self):
        folder = self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        original = self.collector._disk_serial_info

        def replace_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            folder.rename(self.temporary / "removed-after-serial")
            self.block()
            return result

        with mock.patch.object(self.collector, "_disk_serial_info", side_effect=replace_after_read):
            result = self.collector.storage_name_diagnostics()["items"][0]
        self.assertEqual(result["serial_status"], "identity_changed")
        self.assertIsNone(result["user_name"])
        self.assertIsNone(result["serial_hash"])

    def test_stable_wwid_change_and_new_smart_serial_cannot_reuse_old_identity(self):
        folder = self.block()
        self.write(folder / "wwid", "wwn-OLD")
        client = self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        self.write(folder / "wwid", "wwn-NEW")
        self.identity_smart_fixture("SERIAL-B")
        client.snapshot.return_value = {
            "available": True, "disks": [{"disk_name": "sda", "disk_port": 1, "disk_serial": "SERIAL-B"}],
            "spaces": [{"space_name": "新盘", "is_raid": 0, "port": 1,
                        "member": [{"disk_name": "sda2", "root_name": "sda", "port": 1, "disk_id": "SERIAL-B"}]}]}
        self.assertEqual(self.collector._disks()["items"][0]["user_name"], "新盘")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.collector._disk_serial_cache["sda"]["serial"], "SERIAL-B")

    def test_smart_identity_conflict_invalidates_retained_standby_serial(self):
        self.block()
        self.storage_names_fixture()
        self.identity_smart_fixture()
        self.collector._disks()
        self.now += 31
        self.identity_smart_fixture("OTHER")
        self.assertIsNone(self.collector._disks()["items"][0]["user_name"])
        self.assertEqual(self.collector._disk_serial_cache, {})

    def test_nvme_controller_and_usb_bridge_disagreement_remain_unknown(self):
        folder = self.block("nvme0n1")
        self.write(self.sys / "class/nvme/nvme0/serial", "NVME-SERIAL")
        self.assertEqual(self.collector._disk_serial("nvme0n1", folder), "NVME-SERIAL")
        self.write(folder / "dev", "259:0")
        self.write(self.collector.run_root / "udev/data/b259:0", "E:ID_SERIAL_SHORT=OTHER\n")
        self.assertIsNone(self.collector._disk_serial("nvme0n1", folder))
        usb = self.block("sdb")
        self.write(usb / "device/serial", "BRIDGE-SERIAL")
        self.assertIsNone(self.collector._disk_serial("sdb", usb, {"smart_data": {"serial_number": "DRIVE-SERIAL"}}))

    def test_smart_failed_health_is_kept_even_with_nonzero_exit_status(self):
        self.block()
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/sda"], {"smart_status": {"passed": False}, "temperature": {"current": 48}}, 8)
        disk = self.collector._disks()["items"][0]
        self.assertFalse(disk["health"]["passed"])
        self.assertTrue(disk["smart_available"])
        self.assertEqual(disk["temp_c"], 48)

    def test_nvme_temperature_uses_only_manual_smart_cache_and_excludes_pseudo_disks(self):
        self.block("nvme0n1", temp=42000)
        self.block("loop0")
        self.block("dm-0")
        self.respond("smartctl", ["-n", "standby", "-j", "-a", "/dev/nvme0n1"], {"nvme_smart_health_information_log": {"temperature": 90, "critical_warning": 0}})
        disks = self.collector._disks()["items"]
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["temp_c"], 90)
        self.assertEqual(disks[0]['temperature_source'], 'smart')
        self.assertTrue(disks[0]["health"]["passed"])
        self.assertEqual(disks[0]["size_bytes"], 512000000)

    def test_smart_timeout_and_invalid_output_are_explicit(self):
        self.block()
        key = ("smartctl", "-n", "standby", "-j", "-a", "/dev/sda")
        self.responses[key] = subprocess.TimeoutExpired("smartctl", 3)
        receipt = self.collector.refresh_smart(authorizer=lambda: True)
        disk = self.collector._disks()["items"][0]
        self.assertEqual(receipt['results'][0]['reason'], 'command_timeout')
        self.assertIsNone(disk["temp_c"])
        self.now += 31
        self.responses[key] = (0, "not JSON")
        receipt = self.collector.refresh_smart(authorizer=lambda: True)
        self.assertEqual(receipt['results'][0]['reason'], 'invalid_response')

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

    def test_docker_realistic_thirteen_second_latency_fits_read_and_action_budgets(self):
        self.container_list()
        runner = self.collector.runner

        def delayed(argv, timeout=3):
            # Model the measured NAS's 13.26-second response without sleeping
            # or running a real daemon. Old 3/2-second budgets must fail here.
            if Path(argv[0]).name == "docker" and timeout < 13.26:
                raise subprocess.TimeoutExpired(argv, timeout)
            return runner(argv, timeout=timeout)

        self.collector.runner = delayed
        self.calls.clear()
        capability = self.collector._containers()
        self.assertTrue(capability["available"])
        self.assertEqual(len(capability["items"]), 3)
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "restart", "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertEqual([timeout for _, timeout in self.calls], [20, 20, 20, 30])
        self.assertTrue(all(argv[:3] == DOCKER for argv, _ in self.calls))

    def test_stats_timeout_keeps_seventeen_containers_and_unknown_resource_values(self):
        rows = [{"ID": f"{index + 1:064x}", "Names": f"fixture-{index}", "State": "running"}
                for index in range(17)]
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"],
                     "\n".join(json.dumps(row) for row in rows))
        key = self.response_key("docker", ["stats", "--all", "--no-stream", "--format", "{{json .}}"])
        self.responses[key] = subprocess.TimeoutExpired("docker", 20)
        capability = self.collector._containers()
        self.assertTrue(capability["available"])
        self.assertEqual(len(capability["items"]), 17)
        self.assertIn("超时", capability["stats_reason"])
        self.assertTrue(all(row["cpu_percent"] is None and row["memory_bytes"] is None
                            for row in capability["items"]))
        self.assertEqual([timeout for _, timeout in self.calls], [20, 20])
        self.assertTrue(all(argv[:3] == DOCKER for argv, _ in self.calls))

    def test_live_docker_timeout_refuses_write_and_requests_refresh_without_retry(self):
        self.container_list()
        self.calls.clear()
        self.collector._refresh["containers"].clear()
        key = self.response_key("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"])
        self.responses[key] = subprocess.TimeoutExpired("docker", 20)
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "state_unconfirmed")
        self.assertTrue(self.collector._refresh["containers"].is_set())
        self.assertEqual(self.calls, [(DOCKER + ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], 20)])

    def test_default_command_clears_only_docker_connection_environment(self):
        environment = {name: "fixture-private-connection" for name in (
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_API_VERSION", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")}
        environment.update(HOME="/fixture/home", DOCKER_CONFIG="/fixture/docker-config", DOCKER_BUILDKIT="1",
                           KEEP_OTHER="present", LC_ALL="fixture-locale")
        with mock.patch.dict(insights.os.environ, environment, clear=True), \
                mock.patch.object(insights.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            insights._command(DOCKER + ["ps"], timeout=20)
            docker_environment = run.call_args.kwargs["env"]
            self.assertEqual(docker_environment, {"HOME": "/fixture/home", "DOCKER_CONFIG": "/fixture/docker-config",
                                                   "DOCKER_BUILDKIT": "1", "KEEP_OTHER": "present", "LC_ALL": "C"})
            for program in ("smartctl", "virsh"):
                insights._command(["/fixture/bin/" + program, "--fixture-read"])
                self.assertEqual(run.call_args.kwargs["env"], {**environment, "LC_ALL": "C"})
                self.assertEqual(run.call_args.kwargs["timeout"], 3)

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
        self.assertEqual(self.calls, [(DOCKER + ["ps", "--all", "--no-trunc", "--format", "{{json .}}"], 20),
                                      (DOCKER + ["container", "restart", CONTAINER], 30)])

    def test_container_actions_recheck_current_state_instead_of_stale_running_cache(self):
        self.container_list()
        self.respond("docker", ["ps", "--all", "--no-trunc", "--format", "{{json .}}"],
                     {"ID": CONTAINER, "Names": "media", "State": "exited"})
        self.calls.clear()
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "restart", "confirmed": True})
        self.assertEqual(result["error"], "invalid_state")
        self.assertTrue(self.collector._refresh["containers"].is_set())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][3], "ps")
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "start", "confirmed": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[-1], (DOCKER + ["container", "start", CONTAINER], 20))

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
                    self.assertEqual(self.calls[0][0][3], "ps")

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
                self.assertEqual(self.calls[0][0][3], "ps")

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

        self.responses[self.response_key("docker", ["container", "stop", CONTAINER])] = slow_stop
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
        self.responses[self.response_key("docker", ["container", "stop", CONTAINER])] = subprocess.TimeoutExpired("docker", 30)
        result = self.collector.action("container.action", {"id": CONTAINER, "operation": "stop", "confirmed": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "operation_unconfirmed")
        self.assertIn("超时", result["reason"])
        self.assertIn("结果未确认", result["reason"])
        self.assertTrue(result["refresh_pending"])
        self.assertTrue(self.collector._refresh["containers"].is_set())
        self.assertEqual([call for call in self.calls if call[0][3:5] == ["container", "stop"]],
                         [(DOCKER + ["container", "stop", CONTAINER], 30)])
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
        refresh = threading.Thread(target=lambda: self.collector.refresh_smart(authorizer=lambda: True), daemon=True)
        refresh.start()
        self.assertTrue(entered.wait(timeout=1))
        deadline = time.monotonic() + 1
        while not self.collector.snapshot()["timestamp"] and time.monotonic() < deadline:
            time.sleep(0.01)
        start = time.monotonic()
        value = self.collector.snapshot()
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(value["system"]["cpu_logical_cores"], 1)
        release.set()
        refresh.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
