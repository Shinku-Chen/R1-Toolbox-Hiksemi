"""Display roles add metadata without changing temperature control semantics.

All sensor inputs are ordinary temporary directories/files, not host hardware
or symlink-dependent sysfs fixtures. SMART rows are existing in-memory samples.
"""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'runtime') not in sys.path:
    sys.path.insert(0, str(ROOT / 'runtime'))
import device_info
import temperature_sources


class Snapshot:
    def __init__(self):
        self.value = {'disks': {'available': True, 'items': []}}

    def snapshot(self):
        return copy.deepcopy(self.value)


class TemperatureDisplayRoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-display-role-')
        self.addCleanup(temporary.cleanup)
        self.sys = Path(temporary.name) / 'sys'
        self.sys.mkdir()
        self.now = 100.0
        self.insights = Snapshot()
        self.sources = temperature_sources.TemperatureSources(
            self.insights, self.sys, clock=lambda: self.now)
        nonblock = mock.patch.object(device_info.os, 'O_NONBLOCK',
                                     getattr(os, 'O_NONBLOCK', 0), create=True)
        nonblock.start()
        self.addCleanup(nonblock.stop)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding='utf-8')

    def hwmon(self, index, driver, labels):
        folder = self.sys / 'class/hwmon' / ('hwmon' + str(index))
        self.write(folder / 'name', driver)
        for number, label in enumerate(labels, 1):
            self.write(folder / ('temp%d_label' % number), label)
            self.write(folder / ('temp%d_input' % number), 40000 + number * 1000)
            self.write(folder / ('temp%d_crit' % number), 95000)
        return folder

    def zone(self, index, driver):
        folder = self.sys / 'class/thermal' / ('thermal_zone' + str(index))
        self.write(folder / 'type', driver)
        self.write(folder / 'temp', 50000)
        self.write(folder / 'trip_point_0_type', 'critical')
        self.write(folder / 'trip_point_0_temp', 96000)
        return folder

    def device_rows(self):
        reader = device_info._Reader([self.sys])
        rows, fans = device_info._sensors(reader, self.sys)
        self.assertEqual(fans, [])
        return rows

    def disk(self, **changes):
        row = {'stable_identity': 'serial:' + 'a' * 24, 'model': 'Fixture HDD',
               'device': '/dev/sda', 'smart_sampled_monotonic': 100.0,
               'smart_temperature_c': 42.0, 'smart_status': 'ok', 'standby': False}
        row.update(changes)
        self.insights.value['disks']['items'] = [row]
        return row

    def test_shared_helper_is_pure_and_returns_only_fixed_metadata(self):
        self.assertIs(temperature_sources.temperature_display_role,
                      device_info.temperature_display_role)
        with mock.patch.object(Path, 'open', side_effect=AssertionError('file read')), \
                mock.patch.object(os, 'open', side_effect=AssertionError('file open')):
            self.assertEqual(device_info.temperature_display_role('it87', 'SYSTIN'),
                             ('board', 'board_label'))
            self.assertEqual(device_info.temperature_display_role('nvme', 'System'),
                             ('disk', 'disk_driver'))

    def test_cpu_drivers_and_package_core_labels_are_never_board(self):
        for driver in ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal',
                       'cpu-thermal', 'x86_pkg_temp', 'TCPU_PCI'):
            with self.subTest(driver=driver):
                self.assertEqual(device_info.temperature_display_role(driver, 'System'),
                                 ('cpu', 'cpu_driver'))
        for label in ('Package id 0', 'Package', 'Core 0', 'Core 17', 'TCPU_PCI', 'CPU'):
            with self.subTest(label=label):
                self.assertEqual(device_info.temperature_display_role('it87', label),
                                 ('cpu', 'cpu_label'))

    def test_disk_and_smart_contexts_cannot_be_board(self):
        for driver in ('disk', 'nvme', 'drivetemp'):
            with self.subTest(driver=driver):
                self.assertEqual(device_info.temperature_display_role(driver, 'Motherboard'),
                                 ('disk', 'disk_driver'))
        self.assertEqual(device_info.temperature_display_role('smart', '', 'smart'),
                         ('disk', 'smart_cache'))

    def test_generic_acpi_and_chipset_zones_cannot_be_board(self):
        for source in ('hwmon', 'thermal'):
            for driver in ('acpitz', 'INT3400 Thermal', 'INT3403', 'pch_cannonlake', 'PCH'):
                with self.subTest(source=source, driver=driver):
                    self.assertEqual(device_info.temperature_display_role(driver, 'System', source),
                                     ('unknown', 'unclassified'))
        self.assertEqual(device_info.temperature_display_role('it87', 'System', 'thermal'),
                         ('unknown', 'unclassified'))

    def test_board_requires_explicit_label_and_hwmon_driver_context(self):
        for driver in ('it87', 'it8728', 'nct6775', 'nct6779', 'w83627ehf'):
            for label in ('System', 'Motherboard', 'SYSTIN', ' systin '):
                with self.subTest(driver=driver, label=label):
                    self.assertEqual(device_info.temperature_display_role(driver, label),
                                     ('board', 'board_label'))
        for driver, label in (('it87', 'temp1'), ('it87', 'temp2'), ('it87', 'AUXTIN'),
                              ('it87', 'System 1'), ('unknown', 'System'), ('hwmon0', 'Motherboard')):
            with self.subTest(driver=driver, label=label):
                self.assertEqual(device_info.temperature_display_role(driver, label),
                                 ('unknown', 'unclassified'))

    def test_invalid_or_unknown_metadata_is_not_positive_display_evidence(self):
        for driver, label, source in ((None, 'System', 'hwmon'), ('it87', [], 'hwmon'),
                                       ('it87', 'System\n', 'hwmon'), ('it87', 'System', 'other'),
                                       ('it87', 'System', None), ('x' * 257, 'System', 'hwmon')):
            with self.subTest(driver=driver, label=label, source=source):
                self.assertEqual(device_info.temperature_display_role(driver, label, source),
                                 ('unknown', 'invalid_metadata'))

    def test_all_independent_hwmon_measurements_are_preserved(self):
        self.hwmon(0, 'coretemp', ('Package id 0', 'Core 0', 'Core 1'))
        self.hwmon(1, 'it87', ('SYSTIN', 'temp2'))
        self.hwmon(2, 'acpitz', ('System',))
        control = self.sources.snapshot()
        device = self.device_rows()
        self.assertEqual(len(control), 6)
        self.assertEqual(len(device), 6)
        self.assertEqual(len({row['id'] for row in control}), 6)
        self.assertEqual(len({row['id'] for row in device}), 6)
        roles = {row['label']: (row['display_role'], row['display_evidence']) for row in control}
        self.assertEqual(roles['coretemp · Package id 0'], ('cpu', 'cpu_driver'))
        self.assertEqual(roles['coretemp · Core 0'], ('cpu', 'cpu_driver'))
        self.assertEqual(roles['coretemp · Core 1'], ('cpu', 'cpu_driver'))
        self.assertEqual(roles['it87 · SYSTIN'], ('board', 'board_label'))
        self.assertEqual(roles['it87 · temp2'], ('unknown', 'unclassified'))
        self.assertEqual(roles['acpitz · System'], ('unknown', 'unclassified'))
        for row in device:
            self.assertEqual((row['display_role'], row['display_evidence']),
                             roles[row['driver'] + ' · ' + row['label']])
            source_row = next(source for source in control
                              if source['display_driver'] == row['driver']
                              and source['display_channel'] == row['channel'])
            self.assertEqual(source_row['display_label'], row['label'])
            self.assertEqual(source_row['display_source'], row['source'])
            self.assertEqual(source_row['display_label'] + ' · ' + source_row['display_driver'],
                             row['label'] + ' · ' + row['driver'])
        self.assertEqual(self.sources.cpu_guard(), 43.0)

    def test_thermal_cpu_display_metadata_does_not_change_control_kind(self):
        for index, driver in enumerate(('x86_pkg_temp', 'TCPU_PCI', 'acpitz', 'INT3400 Thermal', 'pch')):
            self.zone(index, driver)
        control = {row['label']: row for row in self.sources.snapshot()}
        device = {row['label']: row for row in self.device_rows()}
        self.assertEqual(len(control), 5)
        self.assertEqual(len(device), 5)
        self.assertEqual(control['x86_pkg_temp']['kind'], 'cpu')
        # This old kind is deliberately preserved: display metadata is not a
        # new fan source, selector migration or CPU guard authorization.
        self.assertEqual(control['TCPU_PCI']['kind'], 'board')
        for label in ('x86_pkg_temp', 'TCPU_PCI'):
            self.assertEqual(control[label]['display_role'], 'cpu')
            self.assertEqual(device[label]['display_role'], 'cpu')
        for label in ('acpitz', 'INT3400 Thermal', 'pch'):
            self.assertEqual(control[label]['kind'], 'board')
            self.assertEqual(control[label]['display_role'], 'unknown')
            self.assertEqual(device[label]['display_role'], 'unknown')
        for row in control.values():
            self.assertEqual(row['max_age_seconds'], 3)
            self.assertEqual(row['critical_celsius'], 96.0)
            self.assertEqual(row['sampled_monotonic'], 100.0)
            self.assertEqual(row['display_driver'], 'thermal')
            self.assertEqual(row['display_label'], row['label'])
            self.assertEqual(row['display_source'], 'thermal')
            self.assertEqual(row['display_channel'], device[row['label']]['channel'])

    def test_existing_hwmon_ids_and_control_fields_are_unchanged(self):
        self.hwmon(0, 'it87', ('System',))
        row = self.sources.snapshot()[0]
        expected = 'sysfs:' + hashlib.sha256('class\0it87\0temp1'.encode()).hexdigest()[:24]
        self.assertEqual(row['id'], expected)
        self.assertEqual({key: value for key, value in row.items()
                          if key not in ('display_role', 'display_evidence', 'display_driver',
                                         'display_label', 'display_channel', 'display_source')},
                         {'id': expected, 'label': 'it87 · System', 'kind': 'board',
                          'temperature_c': 41.0, 'status': 'ok', 'sampled_monotonic': 100.0,
                          'max_age_seconds': 3, 'critical_celsius': 95.0})
        self.assertEqual(self.device_rows()[0]['id'], 'hwmon0:temp1')

    def test_display_fields_reuse_existing_reads_exactly_once(self):
        self.hwmon(0, 'coretemp', ('Package id 0',))
        self.zone(0, 'x86_pkg_temp')
        reads, original = [], self.sources._text

        def read(path):
            reads.append(Path(path).relative_to(self.sys).as_posix())
            return original(path)

        with mock.patch.object(self.sources, '_text', side_effect=read):
            rows = self.sources.snapshot()
        self.assertEqual(len(rows), 2)
        expected = ['class/hwmon/hwmon0/' + name for name in
                    ('name', 'temp1_label', 'temp1_input', 'temp1_crit')]
        expected += ['class/thermal/thermal_zone0/' + name for name in
                     ('type', 'temp', 'trip_point_0_type', 'trip_point_0_temp')]
        self.assertEqual(reads, expected)
        self.assertTrue(all(set(('display_driver', 'display_label', 'display_channel', 'display_source'))
                            <= row.keys() for row in rows))

    def test_missing_hwmon_label_preserves_channel_fallback_and_name(self):
        folder = self.hwmon(0, 'it87', ('System',))
        (folder / 'temp1_label').unlink()
        row = self.sources.snapshot()[0]
        self.assertEqual(row['label'], 'it87 · temp1')
        self.assertEqual(row['display_label'], 'temp1')
        self.assertEqual(row['display_driver'], 'it87')
        self.assertEqual(row['display_channel'], 'temp1')
        self.assertEqual(row['display_source'], 'hwmon')
        self.assertEqual(row['display_role'], 'unknown')
        self.assertEqual(row['display_label'] + ' · ' + row['display_driver'], 'temp1 · it87')

    def test_existing_one_second_cache_and_three_second_contract_are_unchanged(self):
        folder = self.hwmon(0, 'coretemp', ('Package id 0',))
        first = self.sources.snapshot()[0]
        self.write(folder / 'temp1_input', 80000)
        self.now = 100.999
        cached = self.sources.snapshot()[0]
        self.assertEqual(cached, first)
        self.now = 101.0
        fresh = self.sources.snapshot()[0]
        self.assertEqual(fresh['id'], first['id'])
        self.assertEqual(fresh['kind'], 'cpu')
        self.assertEqual(fresh['temperature_c'], 80.0)
        self.assertEqual(fresh['sampled_monotonic'], 101.0)
        self.assertEqual(fresh['max_age_seconds'], 3)
        self.assertEqual(fresh['display_role'], 'cpu')

    def test_smart_metadata_keeps_original_source_id_and_exact_expiration(self):
        self.disk()
        row = self.sources.snapshot()[0]
        self.assertEqual(row['id'], 'disk:serial:' + 'a' * 24)
        self.assertEqual(row['kind'], 'disk')
        self.assertEqual((row['display_role'], row['display_evidence']), ('disk', 'smart_cache'))
        self.assertFalse(any(key in row for key in
                             ('display_driver', 'display_label', 'display_channel', 'display_source')))
        self.assertEqual(row['max_age_seconds'], 30)
        self.now = 129.999
        self.assertEqual(self.sources.snapshot()[0]['temperature_c'], 42.0)
        self.now = 130.0
        expired = self.sources.snapshot()[0]
        self.assertEqual(expired['status'], 'unavailable')
        self.assertIsNone(expired['temperature_c'])
        self.assertEqual(expired['sampled_monotonic'], 100.0)
        self.assertEqual(expired['max_age_seconds'], 30)
        self.assertEqual(expired['id'], row['id'])
        self.assertEqual(expired['display_role'], 'disk')
        self.assertIsNone(self.sources.cpu_guard())

    def test_standby_and_disappearance_do_not_fabricate_control_measurements(self):
        self.disk(smart_status='standby', standby=True, smart_temperature_c=None)
        row = self.sources.snapshot()[0]
        self.assertEqual(row['status'], 'standby')
        self.assertEqual(row['display_role'], 'disk')
        self.assertIsNone(row['temperature_c'])
        self.insights.value['disks']['items'] = []
        gone = self.sources.snapshot()[0]
        self.assertEqual(gone['id'], row['id'])
        self.assertEqual(gone['status'], 'unavailable')
        self.assertEqual(gone['display_role'], 'disk')
        self.assertIsNone(gone['temperature_c'])

    def test_display_role_does_not_make_disk_sysfs_readable(self):
        self.hwmon(0, 'nvme', ('System',))
        self.hwmon(1, 'drivetemp', ('Motherboard',))
        original_source = self.sources._text
        original_reader = device_info._Reader.text

        def guarded_source(path):
            self.assertFalse(Path(path).name.startswith('temp'))
            return original_source(path)

        def guarded_reader(reader, path, *args, **kwargs):
            self.assertFalse(Path(path).name.startswith('temp'))
            return original_reader(reader, path, *args, **kwargs)

        with mock.patch.object(self.sources, '_text', side_effect=guarded_source), \
                mock.patch.object(device_info._Reader, 'text', new=guarded_reader):
            self.assertEqual(self.sources.snapshot(), [])
            self.assertEqual(self.device_rows(), [])


if __name__ == '__main__':
    unittest.main()
