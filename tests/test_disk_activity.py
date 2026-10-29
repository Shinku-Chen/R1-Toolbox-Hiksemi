"""Passive sleep investigation uses only bounded proc/sys fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from insights import InsightsCollector


class DiskActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-disk-activity-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'self').mkdir()
        (self.sys / 'block/sda').mkdir(parents=True)
        (self.sys / 'block/sda/dev').write_text('8:0\n')
        self.boot = self.proc / 'sys/kernel/random/boot_id'
        self.boot.write_text('12345678-1234-1234-1234-123456789abc')
        (self.proc / 'self/mountinfo').write_text(
            '1 0 179:6 / / rw - ext4 /dev/mmcblk0p6 rw\n'
            '2 1 8:1 / /drives/disk rw - ext4 /dev/sda1 rw\n')
        self.now = 10.0
        self.collector = InsightsCollector.__new__(InsightsCollector)
        self.collector.proc_root, self.collector.sys_root = self.proc, self.sys
        self.collector.clock = lambda: self.now
        self.collector._disk_activity_lock = threading.Lock()
        self.collector._disk_activity_previous = None
        self.disk()
        self.process()

    def disk(self, reads=10, writes=20, flushes=4, sectors=100, name='sda'):
        # Partition rows are deliberately present and must not be summed twice.
        fields = [8, 0, name, reads, 0, sectors, 1, writes, 0, sectors, 2, 0, 3, 4, 0, 0, 0, 0, flushes, 0]
        (self.proc / 'diskstats').write_text(' '.join(map(str, fields)) + '\n8 1 sda1 999 0 999 0 999 0 999 0 0 0 0\n')

    def process(self, pid=123, start=100, read=4096, written=8192, cancelled=0, name='fixture worker'):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        fields = ['S'] + ['0'] * 21
        fields[19] = str(start)
        (folder / 'stat').write_text(str(pid) + ' (' + name + ') ' + ' '.join(fields))
        (folder / 'io').write_text('rchar: 9999999\nwchar: 9999999\nread_bytes: ' + str(read)
                                  + '\nwrite_bytes: ' + str(written) + '\ncancelled_write_bytes: ' + str(cancelled) + '\n')

    def sample(self, **kwargs):
        return self.collector.disk_activity_diagnostics(**kwargs)

    def test_baseline_has_no_invented_activity_or_power_state(self):
        result = self.sample()
        self.assertEqual(result['status'], 'baseline')
        self.assertIsNone(result['interval_seconds'])
        self.assertIsNone(result['disks'][0]['delta'])
        self.assertIsNone(result['disks'][0]['activity_observed'])
        self.assertIsNone(result['power_state'])
        self.assertIsNone(result['processes'][0]['delta'])
        self.assertEqual(result['disks'][0]['counters']['read_bytes'], 51200)
        self.assertEqual(len(result['disks']), 1)

    def test_two_samples_include_flushes_even_without_byte_changes(self):
        self.sample()
        self.now += 5
        self.disk(flushes=5)
        self.process(read=8192, written=12288, cancelled=1024)
        result = self.sample()
        self.assertEqual(result['interval_seconds'], 5)
        disk = result['disks'][0]
        self.assertEqual(disk['delta']['write_bytes'], 0)
        self.assertEqual(disk['delta']['flush_ops'], 1)
        self.assertTrue(disk['activity_observed'])
        self.assertEqual(result['processes'][0]['delta'], {
            'read_bytes': 4096, 'write_bytes': 4096, 'cancelled_write_bytes': 1024})
        self.assertEqual(result['attribution'], 'process_totals_not_per_disk')

    def test_cached_character_io_is_not_disk_io_and_zero_is_not_standby(self):
        self.sample()
        self.now += 5
        with (self.proc / '123/io').open('a') as stream:
            stream.write('syscr: 999\nsyscw: 999\n')
        result = self.sample()
        self.assertEqual(result['processes'][0]['delta']['write_bytes'], 0)
        self.assertFalse(result['disks'][0]['activity_observed'])
        self.assertIsNone(result['power_state'])

    def test_returned_data_cannot_mutate_the_next_counter_baseline(self):
        first = self.sample()
        first['disks'][0]['counters']['read_bytes'] = 0
        first['processes'][0]['counters']['write_bytes'] = 0
        self.now += 5
        second = self.sample()
        self.assertEqual(second['disks'][0]['delta']['read_bytes'], 0)
        self.assertEqual(second['processes'][0]['delta']['write_bytes'], 0)

    def test_reboot_or_long_gap_resets_baseline(self):
        for kind in ('boot', 'gap', 'clock_reversed'):
            with self.subTest(kind=kind):
                self.sample()
                if kind == 'boot':
                    self.boot.write_text('87654321-1234-1234-1234-123456789abc')
                    self.now += 5
                else:
                    self.now += 301 if kind == 'gap' else -1
                self.assertEqual(self.sample()['status'], 'baseline')

    def test_missing_boot_identity_never_produces_deltas(self):
        self.boot.unlink()
        self.sample()
        self.now += 5
        self.assertIsNone(self.sample()['disks'][0]['delta'])

    def test_first_sample_reports_known_in_flight_io_without_claiming_power_state(self):
        path = self.proc / 'diskstats'
        lines = path.read_text().splitlines()
        fields = lines[0].split()
        fields[11] = '1'
        lines[0] = ' '.join(fields)
        path.write_text('\n'.join(lines))
        result = self.sample()
        self.assertIs(result['disks'][0]['activity_observed'], True)
        self.assertIsNone(result['power_state'])

    def test_pid_reuse_and_counter_rollback_are_unknown(self):
        self.sample()
        self.now += 5
        self.process(start=101, read=100000)
        self.assertIsNone(self.sample()['processes'][0]['delta'])
        self.now += 5
        self.process(start=101, read=0)
        self.assertIsNone(self.sample()['processes'][0]['delta'])

    def test_same_disk_name_after_replacement_has_no_delta(self):
        self.sample()
        self.now += 5
        (self.sys / 'block/sda').rename(self.sys / 'block/retired')
        (self.sys / 'block/sda').mkdir()
        (self.sys / 'block/sda/dev').write_text('8:0')
        self.disk(reads=10000)
        self.assertIsNone(self.sample()['disks'][0]['delta'])

    def test_missing_or_wrong_device_identity_does_not_assert_current_activity(self):
        self.sample()
        self.now += 5
        (self.sys / 'block/sda/dev').write_text('8:16')
        self.disk(writes=999)
        self.assertIsNone(self.sample()['disks'][0]['delta'])

    def test_duplicate_disks_and_invalid_counters_are_not_zero_filled(self):
        path = self.proc / 'diskstats'
        raw = path.read_text()
        path.write_text(raw + raw)
        result = self.sample()
        self.assertEqual(result['disks'], [])
        self.assertIn('duplicate_disk', result['partial_reasons'])
        path.write_text('8 0 sda invalid 0 1 0 0 0 0 0 0 0 0')
        self.assertFalse(self.sample()['available'])

    def test_oversized_or_missing_proc_files_are_reported(self):
        (self.proc / '123/io').write_text('x' * 4097)
        (self.proc / 'diskstats').unlink()
        result = self.sample()
        self.assertFalse(result['complete'])
        self.assertEqual(result['processes'], [])
        self.assertIn('diskstats_unavailable', result['partial_reasons'])
        self.assertIn('process_io_unavailable', result['partial_reasons'])

    def test_process_changes_between_stat_reads_are_omitted(self):
        original = Path.open
        calls = []
        def changing(path, *args, **kwargs):
            if path == self.proc / '123/stat':
                calls.append(path)
                if len(calls) == 2:
                    self.process(start=200)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', changing):
            result = self.sample()
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['changed_processes'], 1)

    def test_history_mount_match_is_metadata_only_and_does_not_open_directory(self):
        result = self.sample(history_directory='/drives/disk/Docker/private-volume')
        self.assertEqual(result['history_storage'], {'status': 'mount_metadata_only',
            'device_number': '8:1', 'source_device': '/dev/sda1', 'filesystem': 'ext4'})
        encoded = json.dumps(result)
        self.assertNotIn('private-volume', encoded)
        self.assertNotIn('/drives/disk', encoded)

    def test_stacked_history_mount_is_unverified(self):
        with (self.proc / 'self/mountinfo').open('a') as stream:
            stream.write('3 1 8:17 / /drives/disk rw - ext4 /dev/sdb1 rw\n')
        result = self.sample(history_directory='/drives/disk/private')
        self.assertEqual(result['history_storage']['status'], 'unverified')

    def test_no_commands_block_devices_credentials_or_user_files_are_read(self):
        (self.proc / '123/cmdline').write_text('secret-commandline')
        (self.proc / '123/environ').write_text('secret-environment')
        with mock.patch('insights.subprocess.run', side_effect=AssertionError('no commands')):
            result = self.sample()
        encoded = json.dumps(result)
        self.assertNotIn('secret-', encoded)
        self.assertNotIn('rchar', encoded)


if __name__ == '__main__':
    unittest.main()
