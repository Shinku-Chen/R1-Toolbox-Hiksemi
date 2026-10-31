"""Control temperature tests use real collector snapshots and fixture sysfs only."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import insights
from temperature_sources import TemperatureSources


class TemperatureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='r1-temperature-test-'))
        self.sys, self.proc = self.root / 'sys', self.root / 'proc'
        self.sys.mkdir()
        self.proc.mkdir()
        self.now = 100.0
        self.calls = []
        self.responses = {}
        self.collector = insights.InsightsCollector(
            proc_root=self.proc, sys_root=self.sys, clock=lambda: self.now,
            ipv4_provider=lambda: {}, command_finder=lambda name: '/fixture/' + name if name == 'smartctl' else None,
            runner=self.runner, page_size=4096, clock_ticks=100)
        self.sources = TemperatureSources(self.collector, self.sys, clock=lambda: self.now)
        self.addCleanup(self.preserve)

    def preserve(self):
        trash = Path.home() / '.Trash'
        if sys.platform == 'darwin' and trash.is_dir():
            shutil.move(str(self.root), str(trash / self.root.name))
        else:
            print('测试临时目录已保留：', self.root)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def runner(self, argv, timeout=3):
        self.calls.append(list(argv))
        response = self.responses.get(argv[-1], (0, {'temperature': {'current': 40}}))
        if isinstance(response, Exception):
            raise response
        code, body = response
        return types.SimpleNamespace(returncode=code, stdout=body if isinstance(body, str) else json.dumps(body), stderr='')

    def hwmon(self, number=0, driver='coretemp', physical='platform/coretemp.0', temperature=55000):
        folder = self.sys / 'devices' / physical / 'hwmon' / ('hwmon' + str(number))
        self.write(folder / 'name', driver)
        self.write(folder / 'temp1_input', temperature)
        self.write(folder / 'temp1_label', 'Package id 0')
        link = self.sys / 'class/hwmon' / ('hwmon' + str(number))
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(folder, target_is_directory=True)
        return folder, link

    def disk(self, name='sda', serial='serial-a', physical='pci0000:00/0000:00:17.0/ata2/host0/target0:0:0/0:0:0:0'):
        folder = self.sys / 'devices' / physical / 'block' / name
        self.write(folder / 'size', 1000000)
        self.write(folder / 'dev', '8:0')
        self.write(folder / 'queue/rotational', 1)
        self.write(folder / 'device/model', 'Fixture HDD')
        if serial is not None:
            self.write(folder / 'device/serial', serial)
        link = self.sys / 'block' / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(folder, target_is_directory=True)
        return folder, link

    def collect_disks(self, *, refresh=True):
        if refresh:
            self.collector.refresh_smart(authorizer=lambda: True)
        self.collector._publish_integration('disks', self.collector._disks)
        return [row for row in self.sources.snapshot() if row['kind'] == 'disk']

    def test_cpu_guard_uses_all_fresh_packages_and_core_sources(self):
        cpu, _ = self.hwmon()
        self.write(cpu / 'temp2_input', 78000)
        self.write(cpu / 'temp2_label', 'Core 0')
        self.write(cpu / 'temp1_crit', 100000)
        self.hwmon(5, 'k10temp', 'pci0000:00/0000:00:18.3', 91000)
        rows = self.sources.snapshot()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['kind'] == 'cpu' and row['status'] == 'ok' for row in rows))
        self.assertEqual(self.sources.cpu_guard(), 91)
        self.assertIn(100, [row['critical_celsius'] for row in rows])
        self.assertTrue(all(row['sampled_monotonic'] == 100 for row in rows))
        self.assertEqual(self.calls, [])

    def test_sysfs_one_second_cache_is_not_resampled_by_each_snapshot(self):
        folder, _ = self.hwmon(temperature=0)
        self.assertEqual(self.sources.cpu_guard(), 0)
        self.write(folder / 'temp1_input', 70000)
        self.now += 0.5
        row = self.sources.snapshot()[0]
        self.assertEqual(row['temperature_c'], 0)
        self.assertEqual(row['sampled_monotonic'], 100)
        self.now += 0.5
        self.assertEqual(self.sources.cpu_guard(), 70)

    def test_malformed_missing_and_unphysical_are_not_fake_zero(self):
        folder, _ = self.hwmon()
        for text, expected in (('0', 0), ('-5000', -5), ('-41000', None), ('151000', None),
                               ('NaN', None), ('40000x', None), ('', None)):
            with self.subTest(text=text):
                self.write(folder / 'temp1_input', text)
                self.now += 2
                row = self.sources.snapshot()[0]
                self.assertEqual(row['temperature_c'], expected)
                self.assertEqual(row['status'], 'ok' if expected is not None else 'unavailable')
        (folder / 'temp1_input').rename(self.root / 'removed-input')
        self.now += 2
        self.assertIsNone(self.sources.cpu_guard())

    def test_hwmon_renumber_preserves_physical_id(self):
        folder, link = self.hwmon(2)
        first = self.sources.snapshot()[0]['id']
        link.rename(self.root / 'previous-class-link')
        renamed = folder.with_name('hwmon9')
        folder.rename(renamed)
        (link.parent / 'hwmon9').symlink_to(renamed, target_is_directory=True)
        self.now += 2
        self.assertEqual(self.sources.snapshot()[0]['id'], first)
        self.assertNotIn('hwmon', first)

    def test_thermal_cpu_fallback_and_board_are_distinct(self):
        zone = self.sys / 'devices/virtual/thermal/thermal_zone2'
        self.write(zone / 'type', 'x86_pkg_temp')
        self.write(zone / 'temp', 62000)
        self.write(zone / 'trip_point_0_type', 'critical')
        self.write(zone / 'trip_point_0_temp', 95000)
        link = self.sys / 'class/thermal/thermal_zone2'
        link.parent.mkdir(parents=True)
        link.symlink_to(zone, target_is_directory=True)
        self.hwmon(8, 'acpitz', 'platform/acpi_thermal', 35000)
        rows = self.sources.snapshot()
        self.assertEqual({row['kind'] for row in rows}, {'cpu', 'board'})
        self.assertEqual(self.sources.cpu_guard(), 62)
        self.assertEqual(next(row for row in rows if row['kind'] == 'cpu')['critical_celsius'], 95)

    def test_smart_uses_actual_probe_time_not_fast_snapshot_freshness(self):
        self.disk()
        first = self.collect_disks()[0]
        self.assertEqual(first['temperature_c'], 40)
        self.assertEqual(first['sampled_monotonic'], 100)
        self.now += 29
        second = self.collect_disks(refresh=False)[0]
        self.assertEqual(second['sampled_monotonic'], 100)
        self.assertEqual(len(self.calls), 1)
        self.now += 62
        self.collector._collect_fast()
        stale = next(row for row in self.sources.snapshot() if row['kind'] == 'disk')
        self.assertEqual(stale['status'], 'unavailable')
        self.assertIsNone(stale['temperature_c'])
        self.assertEqual(stale['sampled_monotonic'], 100)
        self.assertEqual(len(self.calls), 1)

    def test_standby_has_its_own_time_and_expires_to_unavailable(self):
        self.disk()
        self.responses['/dev/sda'] = (2, {'smartctl': {'messages': [{'string': 'Device is in STANDBY mode, exit(2)'}]}})
        row = self.collect_disks()[0]
        self.assertEqual(row['status'], 'standby')
        self.assertIsNone(row['temperature_c'])
        self.now += 91
        row = next(row for row in self.sources.snapshot() if row['kind'] == 'disk')
        self.assertEqual(row['status'], 'unavailable')
        self.assertEqual(row['sampled_monotonic'], 100)
        self.assertEqual(self.calls[0][1:], ['-n', 'standby', '-j', '-a', '/dev/sda'])
        self.assertEqual(len(self.calls), 1)

    def test_standby_check_error_is_not_a_confirmed_sleep_state(self):
        self.disk()
        self.responses['/dev/sda'] = (2, {'smartctl': {'messages': [{'string': 'failed to check standby power mode'}]}})
        row = self.collect_disks()[0]
        self.assertEqual(row['status'], 'unavailable')
        self.assertIsNone(self.collector.snapshot()['disks']['items'][0]['standby'])

    def test_smart_zero_and_physical_negative_are_valid(self):
        self.disk()
        for value in (0, -5, 45):
            with self.subTest(value=value):
                self.responses['/dev/sda'] = (0, {'temperature': {'current': value}})
                self.now += 31
                row = self.collect_disks()[0]
                self.assertEqual(row['status'], 'ok')
                self.assertEqual(row['temperature_c'], value)

    def test_failed_smart_command_cannot_control_but_health_warning_can(self):
        self.disk()
        for code, expected in ((1, 'unavailable'), (2, 'unavailable'),
                               (4, 'unavailable'), (8, 'ok')):
            with self.subTest(code=code):
                self.responses['/dev/sda'] = (code, {'temperature': {'current': 40}})
                self.now += 31
                row = self.collect_disks()[0]
                self.assertEqual(row['status'], expected)
                self.assertEqual(row['temperature_c'], 40 if expected == 'ok' else None)

    def test_smart_timestamp_is_probe_completion_not_start(self):
        self.disk()
        original = self.collector.runner

        def delayed_runner(*args, **kwargs):
            self.now += 2.5
            return original(*args, **kwargs)

        self.collector.runner = delayed_runner
        row = self.collect_disks()[0]
        self.assertEqual(row['sampled_monotonic'], 102.5)
        self.now += 29
        self.assertEqual(self.collect_disks(refresh=False)[0]['sampled_monotonic'], 102.5)
        self.assertEqual(len(self.calls), 1)

    def test_bad_smart_values_or_missing_sample_time_cannot_control(self):
        self.disk()
        for value in (True, 200, -41, '45', None):
            with self.subTest(value=value):
                self.responses['/dev/sda'] = (0, {'temperature': {'current': value}})
                self.now += 31
                row = self.collect_disks()[0]
                self.assertEqual(row['status'], 'unavailable')
        self.responses['/dev/sda'] = (0, {'temperature': {'current': 40}})
        self.now += 31
        self.collect_disks()
        for timestamp in (None, float('nan'), self.now + 10, True):
            cached = self.collector._smart_cache['sda']
            self.collector._smart_cache['sda'] = (timestamp, cached[1], cached[2])
            self.assertEqual(self.sources.snapshot()[0]['status'], 'unavailable')

    def test_disk_serial_and_physical_fallback_survive_sdx_renumbering(self):
        for serial in ('stable-serial', None):
            with self.subTest(serial=serial):
                root = self.sys / ('serial-test' if serial else 'physical-test')
                root.mkdir()
                old_sys, self.sys = self.sys, root
                folder, link = self.disk(serial=serial)
                collector = insights.InsightsCollector(self.proc, root, clock=lambda: self.now, runner=self.runner,
                                                      command_finder=lambda _: '/fixture/smartctl', ipv4_provider=lambda: {})
                catalog = TemperatureSources(collector, root, lambda: self.now)
                collector._publish_integration('disks', collector._disks)
                identity = catalog.snapshot()[0]['id']
                link.rename(self.root / ('old-link-' + str(serial)))
                renamed = folder.with_name('sdb')
                folder.rename(renamed)
                (link.parent / 'sdb').symlink_to(renamed, target_is_directory=True)
                self.now += 31
                collector._publish_integration('disks', collector._disks)
                self.assertEqual(catalog.snapshot()[0]['id'], identity)
                self.assertNotIn('sda', identity)
                self.assertNotIn('sdb', identity)
                self.sys = old_sys

    def test_replaced_identity_invalidates_smart_cache_immediately(self):
        folder, _ = self.disk()
        old = self.collect_disks()[0]['id']
        self.write(folder / 'device/serial', 'replacement-disk')
        self.responses['/dev/sda'] = (0, {'temperature': {'current': 72}})
        rows = self.collect_disks()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(next(row for row in rows if row['id'] == old)['status'], 'unavailable')
        self.assertEqual(next(row for row in rows if row['id'] != old)['temperature_c'], 72)

    def test_disk_disappearance_and_missing_stable_identity_fail_closed(self):
        self.disk()
        row = self.collect_disks()[0]
        self.collector._state['disks'] = {'available': False, 'items': []}
        gone = self.sources.snapshot()[0]
        self.assertEqual(gone['id'], row['id'])
        self.assertEqual(gone['status'], 'unavailable')
        self.assertIsNone(gone['temperature_c'])
        self.collector._state['disks'] = {'available': True, 'items': [{'model': 'unknown', 'smart_temperature_c': 30,
            'smart_sampled_monotonic': self.now, 'smart_status': 'ok'}]}
        self.assertTrue(all(row['status'] == 'unavailable' for row in self.sources.snapshot()))

    def test_smart_command_timeout_remains_unavailable(self):
        self.disk()
        self.responses['/dev/sda'] = subprocess.TimeoutExpired('smartctl', 3)
        row = self.collect_disks()[0]
        self.assertEqual(row['status'], 'unavailable')
        self.assertIsNone(row['temperature_c'])
        self.assertIsNone(row['sampled_monotonic'])

    def test_disk_user_name_updates_label_without_changing_source_or_probing(self):
        item = {'stable_identity': 'serial:' + 'a' * 24, 'model': 'Fixture HDD',
                'device': '/dev/sda', 'user_name': '测试自定义磁盘',
                'smart_temperature_c': 35, 'smart_sampled_monotonic': self.now,
                'smart_status': 'ok'}
        # This exercises catalog formatting with an already validated source;
        # real InsightsCollector rows separately require a captured lifetime.
        self.sources.insights = types.SimpleNamespace(snapshot=lambda: {
            'disks': {'available': True, 'items': [item]}})
        first = self.sources.snapshot()[0]
        self.assertEqual(first['label'], '磁盘 · 测试自定义磁盘 · /dev/sda')
        item['user_name'] = '重命名测试盘'
        second = self.sources.snapshot()[0]
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['temperature_c'], 35)
        self.assertIn('重命名测试盘', second['label'])
        item['user_name'] = None
        self.assertIn('Fixture HDD', self.sources.snapshot()[0]['label'])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
