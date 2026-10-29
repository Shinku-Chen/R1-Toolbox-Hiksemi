"""Storage authorization planning never opens the mounted user filesystem."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_file_roots import collect_file_roots, validate_file_grants


class ApiFileRootsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        (self.proc / 'self').mkdir()
        self.mountinfo = self.proc / 'self/mountinfo'
        self.panel = mock.Mock(proc_root=self.proc)
        self.disk = {'name': 'sda', 'serial': 'SERIAL-A', 'user_name': '主磁盘',
                     'mounts': ['/drives/data']}
        self.panel.overview.return_value = {'disks_detail': [self.disk]}
        self.set_mounts('100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n')

    def set_mounts(self, text):
        self.mountinfo.write_text(text, encoding='utf-8')

    def catalog(self):
        return collect_file_roots(self.panel)

    def test_only_reads_kernel_metadata_and_cached_overview(self):
        with mock.patch('os.stat', side_effect=AssertionError('no data path stat')):
            result = self.catalog()
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'authorization_plan')
        root = result['roots'][0]
        self.assertEqual(root['label'], '主磁盘')
        self.assertEqual(root['mount'], '/drives/data')
        self.assertRegex(root['id'], r'^root_[0-9a-f]{24}$')
        self.assertFalse(root['read_only'])
        self.assertNotIn('SERIAL-A', repr(result))
        self.assertEqual(self.panel.mock_calls, [mock.call.overview()])

    def test_rename_keeps_id_but_identity_and_mount_changes_revoke_binding(self):
        original = self.catalog()['roots'][0]['id']
        self.disk['user_name'] = '视频资源'
        self.assertEqual(self.catalog()['roots'][0]['id'], original)
        self.disk['serial'] = 'SERIAL-B'
        self.assertNotEqual(self.catalog()['roots'][0]['id'], original)
        self.disk['serial'] = 'SERIAL-A'
        self.set_mounts('100 1 8:2 / /drives/data rw - ext4 /dev/sda2 rw\n')
        self.assertNotEqual(self.catalog()['roots'][0]['id'], original)

    def test_reboot_mount_id_does_not_change_grant_id(self):
        original = self.catalog()['roots'][0]['id']
        self.set_mounts('999 2 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n')
        self.assertEqual(self.catalog()['roots'][0]['id'], original)

    def test_missing_or_ambiguous_identity_never_creates_roots(self):
        for rows in ([dict(self.disk, serial=None)], [self.disk, dict(self.disk)], [], None):
            with self.subTest(rows=rows):
                self.panel.overview.return_value = {'disks_detail': rows}
                self.assertEqual(self.catalog()['roots'], [])

    def test_mount_must_match_cache_and_single_visible_source(self):
        self.disk['mounts'] = ['/drives/old']
        self.assertFalse(self.catalog()['available'])
        self.disk['mounts'] = ['/drives/data']
        self.set_mounts(self.mountinfo.read_text() + '101 1 8:2 / /drives/data rw - ext4 /dev/sda2 rw\n')
        self.assertFalse(self.catalog()['available'])

    def test_boot_kernel_virtual_bind_and_unsupported_sources_excluded(self):
        lines = (
            '100 1 8:1 / / rw - ext4 /dev/sda1 rw\n',
            '100 1 8:1 / /proc rw - ext4 /dev/sda1 rw\n',
            '100 1 0:1 / /drives/data rw - tmpfs tmpfs rw\n',
            '100 1 8:1 /subdir /drives/data rw - ext4 /dev/sda1 rw\n',
            '100 1 179:1 / /drives/data rw - ext4 /dev/mmcblk0p1 rw\n',
            '100 1 253:1 / /drives/data rw - ext4 /dev/mapper/unknown rw\n',
            '100 1 8:1 / /drives/../etc rw - ext4 /dev/sda1 rw\n',
        )
        for line in lines:
            with self.subTest(line=line):
                self.set_mounts(line)
                self.assertFalse(self.catalog()['available'])

    def test_nvme_partition_and_readonly_superblock(self):
        self.panel.overview.return_value = {'disks_detail': [
            {'name': 'nvme0n1', 'serial': 'NVME-SERIAL', 'mounts': ['/drives/ssd']}]}
        self.set_mounts('100 1 259:1 / /drives/ssd rw - ext4 /dev/nvme0n1p1 ro\n')
        row = self.catalog()['roots'][0]
        self.assertTrue(row['read_only'])
        self.assertEqual(row['label'], 'ssd')

    def test_space_escapes_decode_without_treating_backslash_as_a_path(self):
        self.disk['mounts'] = [r'/drives/video\040files']
        self.set_mounts('100 1 8:1 / /drives/video\\040files rw - ext4 /dev/sda1 rw\n')
        self.assertEqual(self.catalog()['roots'][0]['mount'], '/drives/video files')
        self.disk['mounts'] = [r'/drives/video\134files']
        self.set_mounts('100 1 8:1 / /drives/video\\134files rw - ext4 /dev/sda1 rw\n')
        self.assertFalse(self.catalog()['available'])

    def test_missing_invalid_or_oversized_mountinfo_is_explicitly_unavailable(self):
        self.mountinfo.unlink()
        self.assertFalse(self.catalog()['available'])
        self.mountinfo.write_bytes(b'\xff')
        self.assertFalse(self.catalog()['available'])
        self.mountinfo.write_bytes(b'x' * 262145)
        self.assertEqual(self.catalog()['status'], 'limit_exceeded')

    def test_grants_reject_missing_roots_and_readonly_write(self):
        catalog = self.catalog()
        root = catalog['roots'][0]
        grant = {'id': root['id'], 'path': '', 'access': 'readonly'}
        validate_file_grants([grant], catalog)
        with self.assertRaises(ValueError):
            validate_file_grants([dict(grant, id='root_' + '0' * 24)], catalog)
        root['read_only'] = True
        with self.assertRaises(ValueError):
            validate_file_grants([dict(grant, access='readwrite')], catalog)
        for value in (None, {}, ['x'], [dict(grant, extra='value')]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_file_grants(value, catalog)


if __name__ == '__main__':
    unittest.main()
