"""Read-only inventory tests using synthetic roots, never the host's hardware."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("device_info_under_test", ROOT / "runtime/device_info.py")
device_info = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(device_info)


class DeviceInfoTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r1-device-info-test-"))
        self.proc, self.sys, self.etc, self.run = (self.root / name for name in ("proc", "sys", "etc", "run"))
        for path in (self.proc, self.sys, self.etc, self.run):
            path.mkdir()
        self.addCleanup(self.recycle_fixture)

    def recycle_fixture(self):
        trash = Path.home() / ".Trash"
        destination = trash / self.root.name
        if sys.platform == "darwin" and trash.is_dir() and not destination.exists():
            shutil.move(str(self.root), str(destination))
        else:
            print("测试临时目录已保留：", self.root)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="utf-8")

    def collect(self):
        return device_info.collect_device_info(self.proc, self.sys, self.etc, self.run)

    def write_routes4(self, *rows):
        self.write(self.proc / "net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n" + "\n".join(rows))

    @staticmethod
    def route6(gateway="fe800000000000000000000000000001", metric="00000400", interface="eth0",
               flags="00000003", destination="0" * 32, prefix="00", source="0" * 32, source_prefix="00"):
        return " ".join((destination, prefix, source, source_prefix, gateway, metric,
                         "00000000", "00000000", flags, interface))

    def test_missing_files_remain_unknown_and_do_not_use_host_defaults(self):
        with patch("subprocess.run", side_effect=AssertionError("不能运行命令")), \
                patch("socket.socket", side_effect=AssertionError("不能建立连接")):
            data = self.collect()
        self.assertIsNone(data["identity"]["hostname"])
        self.assertIsNone(data["identity"]["kernel"])
        self.assertEqual(data["identity"]["os"], {})
        self.assertIsNone(data["cpu"]["model"])
        self.assertIsNone(data["cpu"]["logical_cores"])
        self.assertEqual(data["fans"], [])
        self.assertFalse(data["capabilities"]["fan_rpm"])
        json.dumps(data, allow_nan=False)

    def test_release_identity_uses_only_whitelisted_fields_and_dmi(self):
        self.write(self.proc / "sys/kernel/hostname", "HikSemi-fixture\n")
        self.write(self.proc / "sys/kernel/osrelease", "6.1.66-fixture\n")
        self.write(self.etc / "os-release", 'PRETTY_NAME="Fixture NAS"\nVERSION_ID=1.2\nPASSWORD=never-return-this\n')
        self.write(self.etc / "firmware_version", "R1-1.2.3-20261027")
        self.write(self.etc / "histor-release", "version=1\npassword=never-return-this")
        self.write(self.sys / "class/dmi/id/product_name", "Fixture R1")
        self.write(self.sys / "class/dmi/id/product_serial", "LOCAL-ONLY-123")
        self.write(self.sys / "class/dmi/id/credentials", "never-read-this")
        data = self.collect()["identity"]
        self.assertEqual(data["hostname"], "HikSemi-fixture")
        self.assertEqual(data["kernel"], "6.1.66-fixture")
        self.assertEqual(data["os"], {"PRETTY_NAME": "Fixture NAS", "VERSION_ID": "1.2"})
        self.assertEqual(data["firmware"], {"firmware_version": "R1-1.2.3-20261027"})
        self.assertEqual(data["dmi"]["product_serial"], "LOCAL-ONLY-123")
        self.assertNotIn("never", json.dumps(data))

    def test_vendor_display_name_is_separate_from_hostname_and_projects_no_secrets(self):
        self.write(self.proc / "sys/kernel/hostname", "HikSemi-fixture")
        self.write(self.root / "tmp/nand_config/config.txt", json.dumps({
            "dev_info": {"dev_name": "测试设备昵称", "serial_number": "never-return-this"},
            "account": {"password": "never-return-this"}}, ensure_ascii=False))
        with patch("subprocess.run", side_effect=AssertionError("不能运行命令")), \
                patch("socket.socket", side_effect=AssertionError("不能建立连接")), \
                patch.object(device_info.os, "O_NONBLOCK", 0, create=True):
            data = self.collect()["identity"]
        self.assertEqual(data["name"], "测试设备昵称")
        self.assertEqual(data["hostname"], "HikSemi-fixture")
        self.assertEqual(data["name_source"], "histor.dev_info.dev_name")
        self.assertNotIn("never-return-this", json.dumps(data))

    def test_release_symlink_and_sysfs_class_links_inside_fixture_work(self):
        target = self.root / "usr/lib/os-release"
        self.write(target, 'PRETTY_NAME="Linked Fixture"\nID=fixture')
        (self.etc / "os-release").symlink_to("../usr/lib/os-release")
        device = self.sys / "devices/platform/coretemp.0/hwmon/hwmon7"
        self.write(device / "name", "coretemp")
        self.write(device / "temp1_input", "49000")
        folder = self.sys / "class/hwmon"
        folder.mkdir(parents=True)
        (folder / "hwmon7").symlink_to(device, target_is_directory=True)
        data = self.collect()
        self.assertEqual(data["identity"]["os"]["ID"], "fixture")
        self.assertEqual(data["thermal"][0]["celsius"], 49)

    def test_factory_firmware_metadata_matches_real_structure_and_fallback_file(self):
        first = self.root / "histor/bin/temp/sim_version.json"
        self.write(first, json.dumps({"version": "V6.3.3_Build_260907",
                                    "versionID": "00000001000000010000080000000e00ffffffff00000004ffffffff"}))
        second = self.root / "histor/basis/temp/sim_version_R1.json"
        self.write(second, json.dumps({"version": "V6.3.2_Build_260801", "build": 260907, "model": "R1"}))
        data = self.collect()["identity"]["firmware"]
        self.assertEqual(data, {"version": "V6.3.3_Build_260907", "build": "260907", "model": "R1"})
        # A missing/unreadable first document can use the second exact filename.
        self.write(first, "invalid-json")
        self.assertEqual(self.collect()["identity"]["firmware"]["version"], "V6.3.2_Build_260801")

    def test_factory_firmware_unknown_secret_nested_fields_never_leave_inventory(self):
        path = self.root / "histor/bin/temp/sim_version.json"
        self.write(path, json.dumps({"version": "V6.3.3_Build_260907", "password": "private-secret",
                                   "token": "private-secret", "versionID": "private-secret",
                                   "extra": {"secret": "private-secret"}, "build_time": ["private-secret"],
                                   "model": {"password": "private-secret"}, "product_name": "R1\nprivate-secret"}))
        self.write(self.root / "histor/bin/temp/credentials.json", '{"password":"private-secret"}')
        firmware = self.collect()["identity"]["firmware"]
        self.assertEqual(firmware, {"version": "V6.3.3_Build_260907"})
        self.assertNotIn("private-secret", json.dumps(firmware))
        reader = device_info._Reader((*device_info._firmware_metadata_paths(self.etc),))
        self.assertFalse(reader.allowed(self.root / "histor/bin/temp/credentials.json"))
        self.assertFalse(reader.allowed(self.root / "histor/bin/temp"))

    def test_symlink_to_secret_outside_allowed_roots_is_never_followed(self):
        secret = self.root / "secret"
        self.write(secret, "fixture-private-key")
        (self.etc / "hostname").symlink_to(secret)
        self.assertIsNone(self.collect()["identity"]["hostname"])

    def test_cpu_topology_frequency_and_architecture_are_derived_from_fixture(self):
        self.write(self.proc / "cpuinfo", "model name : Fixture N100\nvendor_id : GenuineIntel\nflags : fpu lm sse\n\n")
        for number, core in ((0, 0), (1, 0), (2, 1), (3, 1)):
            root = self.sys / f"devices/system/cpu/cpu{number}"
            self.write(root / "topology/core_id", core)
            self.write(root / "topology/physical_package_id", 0)
            self.write(root / "cpufreq/scaling_cur_freq", 1200000 + number * 1000)
            self.write(root / "cpufreq/cpuinfo_min_freq", 800000)
            self.write(root / "cpufreq/cpuinfo_max_freq", 3400000)
        data = self.collect()["cpu"]
        self.assertEqual(data["model"], "Fixture N100")
        self.assertEqual(data["logical_cores"], 4)
        self.assertEqual(data["physical_cores"], 2)
        self.assertEqual(data["sockets"], 1)
        self.assertEqual(data["architecture"], "x86_64")
        self.assertEqual(data["frequencies_mhz"][0], {"id": 0, "current": 1200, "minimum": 800, "maximum": 3400})

    def test_cpuinfo_fallback_uses_per_core_records_and_rejects_nonfinite_frequency(self):
        self.write(self.proc / "cpuinfo", "processor : 0\nphysical id : 0\ncore id : 0\ncpu MHz : 1234.5\n\n"
                   "processor : 1\nphysical id : 0\ncore id : 1\ncpu MHz : inf\n")
        data = self.collect()["cpu"]
        self.assertEqual(data["logical_cores"], 2)
        self.assertEqual(data["physical_cores"], 2)
        self.assertEqual(data["frequencies_mhz"][0]["current"], 1234.5)
        self.assertIsNone(data["frequencies_mhz"][1]["current"])
        json.dumps(data, allow_nan=False)

    def test_temperatures_keep_labels_and_convert_millidegrees_only(self):
        root = self.sys / "class/hwmon/hwmon0"
        self.write(root / "name", "coretemp")
        self.write(root / "temp1_label", "Package id 0")
        self.write(root / "temp1_input", "47375")
        self.write(root / "temp1_max", "90000")
        self.write(root / "temp1_crit", "105000")
        self.write(root / "temp2_label", "Core 0")
        self.write(root / "temp2_input", "-1000")
        self.write(root / "temp3_input", "999999999")
        self.write(self.sys / "class/thermal/thermal_zone0/type", "acpitz")
        self.write(self.sys / "class/thermal/thermal_zone0/temp", "39000")
        data = self.collect()["thermal"]
        self.assertEqual(data[0]["label"], "Package id 0")
        self.assertEqual(data[0]["celsius"], 47.375)
        self.assertEqual(data[0]["max_celsius"], 90)
        self.assertEqual(data[0]["critical_celsius"], 105)
        self.assertEqual(data[1]["celsius"], -1)
        self.assertIsNone(data[2]["celsius"])
        self.assertEqual(data[-1]["celsius"], 39)

    def test_fan_rpm_works_without_pwm_and_zero_is_not_unknown(self):
        root = self.sys / "class/hwmon/hwmon9"
        self.write(root / "name", "unknown-controller")
        self.write(root / "fan2_input", "0")
        self.write(root / "fan3_input", "1475")
        self.write(root / "fan3_label", "Header B")
        data = self.collect()
        self.assertEqual(data["fans"][0]["rpm"], 0)
        self.assertEqual(data["fans"][1]["rpm"], 1475)
        self.assertEqual(data["fans"][1]["label"], "Header B")
        self.assertIsNone(data["fans"][1]["physical_role"])
        self.assertFalse(data["fans"][1]["role_verified"])
        self.assertIsNone(data["fans"][0]["pwm"])
        self.assertTrue(data["capabilities"]["fan_rpm"])
        self.assertFalse(data["capabilities"]["fan_pwm"])

    def test_fan_invalid_readings_do_not_become_fake_zero_or_control_capability(self):
        root = self.sys / "class/hwmon/hwmon1"
        self.write(root / "fan1_input", "unavailable")
        self.write(root / "pwm1", 256)
        self.write(root / "fan2_input", -1)
        data = self.collect()
        self.assertTrue(all(item["rpm"] is None for item in data["fans"]))
        self.assertFalse(data["capabilities"]["fan_rpm"])
        self.assertFalse(data["capabilities"]["fan_pwm"])

    def test_backlight_zero_does_not_claim_panel_is_sleeping(self):
        root = self.sys / "class/backlight/intel_backlight"
        self.write(root / "brightness", 0)
        self.write(root / "actual_brightness", 48000)
        self.write(root / "max_brightness", 96000)
        self.write(root / "bl_power", 0)
        data = self.collect()
        self.assertEqual(data["backlight"][0]["percent"], 0)
        self.assertEqual(data["backlight"][0]["actual_brightness"], 48000)
        self.assertEqual(data["backlight"][0]["sleep_state"], "unknown")
        self.assertTrue(data["capabilities"]["backlight"])

    def test_block_capacity_uses_512_byte_sectors_not_logical_sector_size(self):
        root = self.sys / "block/sda"
        sectors = 18014398509481985
        self.write(root / "size", sectors)
        self.write(root / "queue/logical_block_size", 4096)
        self.write(root / "queue/rotational", 1)
        self.write(root / "device/model", "Fixture disk")
        self.write(root / "device/serial", "LOCAL-SERIAL")
        self.write(root / "device/state", "running")
        self.write(self.sys / "block/loop0/size", "9999")
        data = self.collect()["storage"]["devices"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["sectors"], sectors)
        self.assertEqual(data[0]["bytes"], sectors * 512)
        self.assertEqual(data[0]["serial"], "LOCAL-SERIAL")

    def test_raid_reads_existing_status_without_issuing_rescan_or_assembly(self):
        root = self.sys / "block/md0/md"
        self.write(root / "level", "raid1")
        self.write(root / "array_state", "clean")
        self.write(root / "raid_disks", 2)
        self.write(root / "degraded", 1)
        self.write(root / "sync_action", "recover")
        self.write(root / "sync_completed", "100 / 1000")
        (root / "dev-sda1").mkdir()
        self.write(self.proc / "mdstat", "md0 : active raid1 sda1[0]\n")
        data = self.collect()["storage"]
        self.assertEqual(data["raid"][0]["level"], "raid1")
        self.assertEqual(data["raid"][0]["degraded"], 1)
        self.assertEqual(data["raid"][0]["members"], ["dev-sda1"])
        self.assertIn("active raid1", data["mdstat"])

    def test_network_link_information_ipv6_and_unknown_speed(self):
        root = self.sys / "class/net/eth0"
        for field, value in {"address": "02:00:00:00:00:01", "operstate": "up", "carrier": 1,
                             "speed": 2500, "duplex": "full", "mtu": 9000}.items():
            self.write(root / field, value)
        self.write(self.sys / "class/net/eth1/speed", -1)
        self.write(self.proc / "net/if_inet6", "20010db8000000000000000000000001 01 40 00 80 eth0\n"
                   "fe800000000000000000000000000001 01 40 20 80 eth0\ninvalid 00 40 00 00 eth0\n")
        data = self.collect()["network"]
        self.assertEqual(data[0]["speed_mbps"], 2500)
        self.assertEqual(data[0]["duplex"], "full")
        self.assertEqual(data[0]["mtu"], 9000)
        self.assertEqual(data[0]["ipv6"], ["2001:db8::1/64", "fe80::1/64"])
        self.assertIsNone(data[1]["speed_mbps"])

    def test_ipv6_addresses_preserve_multiple_prefixes_scopes_and_zero_flags(self):
        self.write(self.sys / "class/net/eth0/operstate", "up")
        self.write(self.sys / "class/net/lo/operstate", "unknown")
        self.write(self.proc / "net/if_inet6", "20010db8000000000000000000000001 02 40 00 00 eth0\n"
                   "fe800000000000000000000000000001 02 40 20 80 eth0\n"
                   "00000000000000000000000000000001 01 80 10 80 lo\n")
        data = self.collect()["network"]
        self.assertEqual(data[0]["ipv6_addresses"], [
            {"address": "2001:db8::1", "prefix_length": 64, "scope": "global", "scope_id": 0, "flags": 0},
            {"address": "fe80::1", "prefix_length": 64, "scope": "link", "scope_id": 32, "flags": 128}])
        self.assertEqual(data[1]["ipv6_addresses"][0]["scope"], "host")
        self.assertEqual(data[1]["ipv6_addresses"][0]["prefix_length"], 128)
        self.assertEqual(data[0]["address_status"]["ipv6"], "ok")

    def test_ipv6_missing_empty_and_malformed_tables_are_distinct(self):
        self.write(self.sys / "class/net/eth0/operstate", "up")
        self.assertEqual(self.collect()["network"][0]["address_status"]["ipv6"], "unavailable")
        self.write(self.proc / "net/if_inet6", "")
        self.assertEqual(self.collect()["network"][0]["address_status"]["ipv6"], "ok")
        self.write(self.proc / "net/if_inet6", "invalid address row\n")
        self.assertEqual(self.collect()["network"][0]["address_status"]["ipv6"], "malformed")
        self.write(self.proc / "net/if_inet6", "20010db8000000000000000000000001 02 81 00 80 eth0\n"
                   "fe800000000000000000000000000001 02 40 20 00 eth0\n")
        data = self.collect()["network"][0]
        self.assertEqual(data["address_status"]["ipv6"], "partial")
        self.assertEqual(data["ipv6"], ["fe80::1/64"])

    def test_ipv6_address_output_is_bounded_and_reports_partial(self):
        self.write(self.sys / "class/net/eth0/operstate", "up")
        rows = [f"{index:032x} 02 40 00 00 eth0" for index in range(1, 20)]
        self.write(self.proc / "net/if_inet6", "\n".join(rows))
        data = self.collect()["network"][0]
        self.assertEqual(len(data["ipv6_addresses"]), 16)
        self.assertEqual(data["address_status"]["ipv6"], "partial")

    def test_ipv4_defaults_decode_little_endian_keep_metrics_and_all_interfaces(self):
        self.write(self.sys / "class/net/eth0/operstate", "up")
        self.write(self.sys / "class/net/eth1/operstate", "up")
        self.write_routes4("eth0 00000000 010200C0 0003 0 0 100 00000000 0 0 0",
                           "eth1 00000000 0100000A 0003 0 0 0 00000000 0 0 0",
                           "eth0 00000000 020200C0 0003 0 0 200 00000000 0 0 0")
        data = self.collect()
        routes = data["network_config"]["default_routes"]["ipv4"]
        self.assertEqual([(row["gateway"], row["metric"]) for row in routes],
                         [("10.0.0.1", 0), ("192.0.2.1", 100), ("192.0.2.2", 200)])
        self.assertEqual(data["network"][0]["default_gateways"]["ipv4"], [routes[1], routes[2]])
        self.assertEqual(data["network"][1]["default_gateways"]["ipv4"], [routes[0]])
        self.assertEqual(data["network_config"]["route_status"]["ipv4"], "ok")

    def test_ipv4_routes_do_not_treat_reject_down_or_specific_routes_as_gateways(self):
        self.write_routes4("eth0 00000000 00000000 0001 0 0 0 00000000",
                           "eth0 000200C0 00000000 0001 0 0 10 00FFFFFF",
                           "eth0 00000000 010200C0 0002 0 0 20 00000000",
                           "eth0 00000000 020200C0 0201 0 0 30 00000000")
        rows = self.collect()["network_config"]["default_routes"]["ipv4"]
        self.assertEqual(rows, [{"interface": "eth0", "gateway": None, "metric": 0, "source": "/proc/net/route"}])

    def test_ipv6_defaults_decode_gateway_metric_and_source_specific_prefix(self):
        self.write(self.sys / "class/net/eth0/operstate", "up")
        self.write(self.proc / "net/ipv6_route", "\n".join((self.route6(),
                   self.route6(metric="00000000", source="20010db8000100000000000000000000", source_prefix="40"),
                   self.route6(gateway="0" * 32, metric="00000800"))))
        data = self.collect()
        routes = data["network_config"]["default_routes"]["ipv6"]
        self.assertEqual(routes[0]["source_prefix"], "2001:db8:1::/64")
        self.assertEqual([(row["gateway"], row["metric"]) for row in routes],
                         [("fe80::1", 0), ("fe80::1", 1024), (None, 2048)])
        self.assertEqual(data["network"][0]["default_gateways"]["ipv6"], routes)
        self.assertEqual(data["network_config"]["route_status"]["ipv6"], "ok")

    def test_ipv6_reject_down_and_nondefault_routes_do_not_invent_gateways(self):
        self.write(self.proc / "net/ipv6_route", "\n".join((self.route6(flags="00000201"),
                   self.route6(flags="00000002"), self.route6(destination="20010db8" + "0" * 24, prefix="40"))))
        data = self.collect()["network_config"]
        self.assertEqual(data["default_routes"]["ipv6"], [])
        self.assertEqual(data["route_status"]["ipv6"], "ok")

    def test_route_missing_empty_malformed_and_partial_are_explicit(self):
        data = self.collect()["network_config"]
        self.assertEqual(data["route_status"], {"ipv4": "unavailable", "ipv6": "unavailable"})
        self.write_routes4()
        self.write(self.proc / "net/ipv6_route", "")
        self.assertEqual(self.collect()["network_config"]["route_status"], {"ipv4": "ok", "ipv6": "ok"})
        self.write_routes4("broken row")
        self.write(self.proc / "net/ipv6_route", self.route6() + "\nmalformed\n")
        self.assertEqual(self.collect()["network_config"]["route_status"], {"ipv4": "malformed", "ipv6": "partial"})
        self.write(self.proc / "net/route", "not a kernel route header")
        self.assertEqual(self.collect()["network_config"]["route_status"]["ipv4"], "malformed")

    def test_route_parser_rejects_bad_ranges_and_limits_output(self):
        self.write_routes4("eth0 00000000 010200C0 0003 0 0 4294967296 00000000",
                           "../secret 00000000 010200C0 0003 0 0 1 00000000",
                           "eth0 00000000 BADGATEWAY 0003 0 0 1 00000000")
        self.assertEqual(self.collect()["network_config"]["route_status"]["ipv4"], "malformed")
        self.write(self.proc / "net/ipv6_route", self.route6(prefix="81"))
        self.assertEqual(self.collect()["network_config"]["route_status"]["ipv6"], "malformed")
        self.write_routes4(*(f"eth0 00000000 010200C0 0003 0 0 {metric} 00000000" for metric in range(300)))
        data = self.collect()["network_config"]
        self.assertEqual(len(data["default_routes"]["ipv4"]), 256)
        self.assertEqual(data["route_status"]["ipv4"], "partial")

    def test_dns_preserves_dual_stack_values_search_domains_and_discards_other_fields(self):
        self.write(self.etc / "resolv.conf", "# password=private-secret\nnameserver 192.0.2.53\n"
                   "nameserver 2001:0db8::53 ; comment\nnameserver fe80::53%eth0\n"
                   "nameserver 192.0.2.53\nsearch NAS.Example local.\ndomain example.org\n"
                   "options timeout:2\npassword private-secret\n")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["servers"], ["192.0.2.53", "2001:db8::53", "fe80::53%eth0"])
        self.assertEqual(data["upstream_servers"], data["servers"])
        self.assertEqual(data["stub_servers"], [])
        self.assertEqual(data["search"], ["nas.example", "local."])
        self.assertEqual(data["domains"], ["example.org"])
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["sources"][0]["path"], "/etc/resolv.conf")
        self.assertNotIn("private-secret", json.dumps(data))

    def test_systemd_stub_symlink_reports_actual_upstream_without_other_resolvers(self):
        stub = self.run / "systemd/resolve/stub-resolv.conf"
        self.write(stub, "nameserver 127.0.0.53\nsearch fixture.local\n")
        self.write(self.run / "systemd/resolve/resolv.conf", "nameserver 192.0.2.53\nnameserver 2001:db8::53\n")
        self.write(self.run / "NetworkManager/no-stub-resolv.conf", "nameserver 198.51.100.99\n")
        (self.etc / "resolv.conf").symlink_to(stub)
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["servers"], ["127.0.0.53"])
        self.assertEqual(data["stub_servers"], ["127.0.0.53"])
        self.assertEqual(data["upstream_servers"], ["192.0.2.53", "2001:db8::53"])
        self.assertEqual(data["search"], ["fixture.local"])
        self.assertEqual(data["status"], "ok")

    def test_direct_dns_does_not_merge_stale_runtime_upstreams(self):
        self.write(self.etc / "resolv.conf", "nameserver 192.0.2.53")
        self.write(self.run / "systemd/resolve/resolv.conf", "nameserver 203.0.113.8")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["upstream_servers"], ["192.0.2.53"])
        self.assertEqual(data["sources"][1]["servers"], ["203.0.113.8"])

    def test_dns_only_associated_upstream_damage_changes_aggregate_status(self):
        upstream = self.run / "systemd/resolve/resolv.conf"
        self.write(upstream, "nameserver 192.0.2.53\nnameserver invalid")
        self.write(self.run / "NetworkManager/no-stub-resolv.conf", "nameserver invalid")
        self.write(self.etc / "resolv.conf", "nameserver 127.0.0.53")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["sources"][1]["status"], "partial")
        self.assertEqual(data["upstream_servers"], ["192.0.2.53"])
        self.assertEqual(data["status"], "partial")
        self.assertIn("关联上游", data["reason"])
        # Unrelated stale resolver files remain visible in sources, but cannot
        # make a directly configured healthy resolver appear broken.
        self.write(self.etc / "resolv.conf", "nameserver 198.51.100.53")
        self.assertEqual(self.collect()["network_config"]["dns"]["status"], "ok")

    def test_network_manager_known_stub_link_reports_upstream_and_ignores_profiles(self):
        path = self.run / "NetworkManager/resolv.conf"
        self.write(path, "nameserver 127.0.0.1\n")
        self.write(self.run / "NetworkManager/no-stub-resolv.conf", "nameserver 198.51.100.53\nnameserver 2001:db8::54\n")
        self.write(self.run / "NetworkManager/system-connections/secret.nmconnection", "password=do-not-read")
        (self.etc / "resolv.conf").symlink_to(path)
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["stub_servers"], ["127.0.0.1"])
        self.assertEqual(data["upstream_servers"], ["198.51.100.53", "2001:db8::54"])
        self.assertNotIn("do-not-read", json.dumps(data))

    def test_dns_missing_or_unknown_upstream_is_not_reported_as_unconfigured(self):
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["servers"], [])
        self.assertIn("不能据此判断", data["reason"])
        self.write(self.etc / "resolv.conf", "nameserver ::1")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["stub_servers"], ["::1"])
        self.assertEqual(data["upstream_servers"], [])
        self.assertIn("上游", data["reason"])

    def test_dns_runtime_fallback_is_labelled_unconfirmed(self):
        self.write(self.run / "systemd/resolve/resolv.conf", "nameserver 2001:db8::53")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["servers"], ["2001:db8::53"])
        self.assertEqual(data["status"], "partial")
        self.assertIn("未确认系统实际选择", data["reason"])

    def test_dns_invalid_fields_are_rejected_without_losing_valid_values(self):
        self.write(self.etc / "resolv.conf", "nameserver 999.0.0.1\nnameserver 192.0.2.53:53\n"
                   "nameserver 2001:db8::53%../secret\nnameserver 192.0.2.53\n"
                   "search good.example bad/label\ndomain one.example two.example\nsearch valid.example\n")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["servers"], ["192.0.2.53"])
        self.assertEqual(data["search"], ["valid.example"])
        self.assertEqual(data["domains"], [])
        self.assertEqual(data["status"], "partial")
        self.write(self.etc / "resolv.conf", "nameserver invalid\ndomain invalid/label")
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["status"], "malformed")
        self.assertEqual(data["servers"], [])

    def test_dns_bounded_values_and_oversized_file_keep_uncertainty(self):
        self.write(self.etc / "resolv.conf", "\n".join(f"nameserver 192.0.2.{index}" for index in range(1, 30)))
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(len(data["servers"]), 16)
        self.assertEqual(data["status"], "partial")
        self.write(self.etc / "resolv.conf", "#\n" * 1025 + "nameserver 192.0.2.53")
        self.assertEqual(self.collect()["network_config"]["dns"]["status"], "partial")
        self.write(self.etc / "resolv.conf", "x" * 65537)
        data = self.collect()["network_config"]["dns"]
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["servers"], [])

    def test_dns_symlinks_cannot_read_credentials_inside_or_outside_injected_roots(self):
        secret = self.etc / "credentials"
        self.write(secret, "nameserver 192.0.2.199\npassword private-secret")
        (self.etc / "resolv.conf").symlink_to(secret)
        original = device_info._Reader.text
        read_paths = []

        def record_read(reader, path, *args):
            read_paths.append(Path(path))
            return original(reader, path, *args)

        with patch.object(device_info._Reader, "text", record_read):
            data = self.collect()["network_config"]["dns"]
        self.assertNotIn(secret, read_paths)
        self.assertEqual(data["sources"][0]["status"], "unavailable")
        self.assertIn("不在已知", data["sources"][0]["reason"])
        self.assertNotIn("192.0.2.199", json.dumps(data))
        # Even an absolute link to the real host's standard resolver path is
        # rejected: fixture collection cannot fall back to host /etc or /run.
        runtime = self.run / "systemd/resolve"
        runtime.mkdir(parents=True)
        (runtime / "resolv.conf").symlink_to("/etc/resolv.conf")
        read_paths.clear()
        with patch.object(device_info._Reader, "text", record_read):
            data = self.collect()["network_config"]["dns"]
        self.assertNotIn(Path("/etc/resolv.conf"), read_paths)
        self.assertEqual(data["sources"][1]["status"], "unavailable")

    def test_pci_resolves_names_from_local_database_and_falls_back_to_numeric_ids(self):
        self.write(self.etc / "pci.ids", "8086  Fixture Intel\n\t1234  Fixture GPU\n\t\t0000 0001 Subsystem\n")
        root = self.sys / "bus/pci/devices/0000:00:02.0"
        self.write(root / "vendor", "0x8086")
        self.write(root / "device", "0x1234")
        self.write(root / "class", "0x030000")
        self.write(self.sys / "bus/pci/devices/0000:00:03.0/vendor", "0x9999")
        data = self.collect()["identity"]["pci"]
        self.assertEqual(data[0]["vendor_name"], "Fixture Intel")
        self.assertEqual(data[0]["device_name"], "Fixture GPU")
        self.assertEqual(data[1]["vendor"], "0x9999")
        self.assertIsNone(data[1]["vendor_name"])

    def test_scan_output_and_file_reads_are_bounded(self):
        root = self.sys / "class/hwmon/hwmon0"
        for index in range(300):
            self.write(root / f"fan{index}_input", index)
        self.write(self.etc / "hostname", "x" * 10000)
        data = self.collect()
        self.assertEqual(len(data["fans"]), 64)
        self.assertIsNone(data["identity"]["hostname"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "platform has no fifo")
    def test_fifo_in_place_of_release_metadata_does_not_block(self):
        os.mkfifo(self.etc / "hostname")
        self.assertIsNone(self.collect()["identity"]["hostname"])

    def test_collection_does_not_write_or_chmod_any_fixture_file(self):
        root = self.sys / "class/hwmon/hwmon0"
        self.write(root / "fan2_input", 1200)
        self.write(root / "pwm2", 125)
        self.write(root / "pwm2_enable", 2)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.collect()
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
