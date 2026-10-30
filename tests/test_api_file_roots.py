"""Storage authorization planning never opens the mounted user filesystem."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_file_roots import (collect_file_roots, is_users_root_path, is_admin_root_path,
                            physical_mount_path, is_private_recovery_path,
                            validate_file_grants, valid_relative_file_path)


class ApiFileRootsTests(unittest.TestCase):
    def test_private_recovery_cannot_be_new_file_grant_but_root_and_siblings_remain_valid(self):
        catalog = self.catalog()
        root = catalog['roots'][0]['id']
        for path in ('.r1-toolbox-ops', '.r1-toolbox-ops/operation_fixture/source',
                     '.R1-TOOLBOX-OPS/source', '.r1-toolbox-opſ/source',
                     'folder/.r1-toolbox-ops', 'folder/.R1-TOOLBOX-OPS/source'):
            self.assertTrue(is_private_recovery_path(path))
            for access in ('readonly', 'readwrite'):
                with self.assertRaisesRegex(ValueError, '内部恢复区'):
                    validate_file_grants([{'id': root, 'path': path, 'access': access}], catalog)
        for path in ('', '.r1-toolbox-ops2', 'folder/.r1-toolbox-ops2'):
            self.assertFalse(is_private_recovery_path(path))
            validate_file_grants([{'id': root, 'path': path, 'access': 'readwrite'}], catalog)

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
        self.assertEqual(root['mount'], '/drives/data/Users/admin')
        self.assertEqual(result['boundary_version'], 3)
        self.assertRegex(root['id'], r'^root_[0-9a-f]{24}$')
        self.assertFalse(root['read_only'])
        self.assertNotIn('SERIAL-A', repr(result))
        self.assertEqual(self.panel.mock_calls, [mock.call.overview(include_identity=True)])

    def test_rename_keeps_id_but_identity_and_mount_changes_revoke_binding(self):
        original = self.catalog()['roots'][0]['id']
        self.disk['user_name'] = '视频资源'
        self.assertEqual(self.catalog()['roots'][0]['id'], original)
        self.disk['serial'] = 'SERIAL-B'
        self.assertNotEqual(self.catalog()['roots'][0]['id'], original)
        self.disk['serial'] = 'SERIAL-A'
        self.set_mounts('100 1 8:2 / /drives/data rw - ext4 /dev/sda2 rw\n')
        self.assertNotEqual(self.catalog()['roots'][0]['id'], original)

    def test_private_cached_identity_is_used_without_serial_projection(self):
        def cached_overview(*, include_identity=False):
            return {'disks_detail': [dict(self.disk,
                serial=self.disk['serial'] if include_identity else None)]}
        self.panel.overview.side_effect = cached_overview
        result = self.catalog()
        self.assertTrue(result['available'])
        self.assertNotIn(self.disk['serial'], json.dumps(result))
        self.assertEqual(result['roots'][0]['mount'], '/drives/data/Users/admin')

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
        self.assertEqual(self.catalog()['roots'][0]['mount'], '/drives/video files/Users/admin')
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

    def test_users_boundary_is_exact_and_case_sensitive(self):
        valid = ('/drives/data/Users', '/drives/视频 资源/Users')
        invalid = ('/drives/data', '/drives/data/Users/', '/drives/data/users',
                   '/drives/data/Users2', '/drives/data/Users/alice',
                   '/drives/data/nested/Users', '/drives//Users', '/drives/../Users',
                   '/drives/./Users', '/drives/data\\other/Users', '/DRIVES/data/Users',
                   '/drives/data/Users\x00', None)
        for path in valid:
            with self.subTest(path=path):
                self.assertTrue(is_users_root_path(path))
        for path in invalid:
            with self.subTest(path=path):
                self.assertFalse(is_users_root_path(path))

    def test_public_admin_boundary_and_physical_mount_helper_are_exact(self):
        for path in ('/drives/data/Users/admin', '/drives/视频 资源/Users/admin'):
            self.assertTrue(is_admin_root_path(path))
            self.assertEqual(physical_mount_path(path), path[:-12])
        for path in ('/drives/data', '/drives/data/Users', '/drives/data/Users/public',
                     '/drives/data/Users/Admin', '/drives/data/users/admin',
                     '/drives/data/Users/admin/', '/drives/data/Users/admin/child',
                     '/drives/data/nested/Users/admin', '/drives/../Users/admin',
                     '/drives//Users/admin', '/drives/data\\other/Users/admin',
                     '/DRIVES/data/Users/admin', '/drives/data/Users/admin\x00', None):
            with self.subTest(path=path):
                self.assertFalse(is_admin_root_path(path))
                with self.assertRaises(ValueError):
                    physical_mount_path(path)

    def test_nested_data_mounts_never_become_extra_roots(self):
        for path in ('/drives/data/Users', '/drives/data/users', '/drives/data/Users2',
                     '/drives/data/other', '/drives/data/../another', '/drives/data/'):
            with self.subTest(path=path):
                self.disk['mounts'] = [path]
                self.set_mounts(f'100 1 8:1 / {path} rw - ext4 /dev/sda1 rw\n')
                self.assertFalse(self.catalog()['available'])

    def test_old_whole_disk_id_is_not_reused_or_silently_narrowed(self):
        identity = ['r1-file-root-v1', self.disk['serial'], '/dev/sda1',
                    '8:1', 'ext4', '/drives/data']
        old_id = 'root_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                     separators=(',', ':')).encode('ascii')).hexdigest()[:24]
        catalog = self.catalog()
        self.assertNotEqual(catalog['roots'][0]['id'], old_id)
        with self.assertRaises(ValueError):
            validate_file_grants([{'id': old_id, 'path': '', 'access': 'readonly'}], catalog)

    def test_old_users_id_and_paths_are_never_automatically_converted_to_admin(self):
        identity = ['r1-file-users-root-v2', self.disk['serial'], '/dev/sda1',
                    '8:1', 'ext4', '/drives/data/Users']
        legacy_id = 'root_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                     separators=(',', ':')).encode('ascii')).hexdigest()[:24]
        catalog = self.catalog()
        self.assertNotEqual(catalog['roots'][0]['id'], legacy_id)
        legacy = {'id': legacy_id, 'path': 'admin/folder', 'access': 'readonly'}
        with self.assertRaises(ValueError):
            validate_file_grants([legacy], catalog)
        self.assertEqual(legacy['path'], 'admin/folder')
        old_catalog = dict(catalog, boundary_version=2, roots=[dict(catalog['roots'][0],
            id=legacy_id, mount='/drives/data/Users')])
        with self.assertRaises(ValueError):
            validate_file_grants([legacy], old_catalog)
        with self.assertRaises(ValueError):
            validate_file_grants([legacy], dict(old_catalog, boundary_version=3))
        # A literal new path beginning with "admin" means a child named admin;
        # it is not silently stripped or normalized to the old boundary.
        new = dict(legacy, id=catalog['roots'][0]['id'])
        validate_file_grants([new], catalog)
        self.assertEqual(new['path'], 'admin/folder')

    def test_children_or_exact_users_admin_mounts_do_not_reject_whole_root(self):
        base = '100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n'
        children = (
            '101 100 0:4 / /drives/data/Users rw - tmpfs tmpfs rw\n',
            '101 100 0:4 / /drives/data/Users/admin rw - tmpfs tmpfs rw\n',
            '101 100 0:4 / /drives/data/Users/admin/app rw - tmpfs tmpfs rw\n',
            '101 100 8:2 / /drives/data/Users/alice ro - ext4 /dev/sdb1 ro\n',
            '101 100 8:1 /private /drives/data/Users/alice rw - ext4 /dev/sda1 rw\n',
            '101 100 0:4 / /drives/data/Users/a\\134b rw - tmpfs tmpfs rw\n',
            '101 100 0:4 / /drives/data/Users/a\\012b rw - tmpfs tmpfs rw\n',
        )
        original = self.catalog()['roots'][0]
        for child in children:
            with self.subTest(child=child):
                self.set_mounts(base + child)
                self.assertEqual(self.catalog()['roots'], [original])

    def test_sibling_mounts_do_not_extend_users_boundary(self):
        self.set_mounts(self.mountinfo.read_text() +
                       '101 100 0:4 / /drives/data/Users2 rw - tmpfs tmpfs rw\n' +
                       '102 100 0:5 / /drives/data/Applications rw - tmpfs tmpfs rw\n')
        self.assertEqual([row['mount'] for row in self.catalog()['roots']], ['/drives/data/Users/admin'])

    def test_canonical_alias_mounts_with_same_source_or_device_are_ambiguous(self):
        base = self.mountinfo.read_text()
        for child in (
            '101 1 8:1 / /drives/alias rw - ext4 /dev/sda1 rw\n',
            '101 1 8:1 / /drives/alias rw - ext4 /dev/disk/by-uuid/alias rw\n',
            '101 1 8:2 / /drives/alias rw - ext4 /dev/sda1 rw\n',
        ):
            with self.subTest(child=child):
                self.set_mounts(base + child)
                self.assertFalse(self.catalog()['available'])

    def test_external_full_filesystem_alias_keeps_only_canonical_grant_and_id(self):
        original = self.catalog()['roots'][0]['id']
        base = self.mountinfo.read_text()
        self.disk['mounts'].extend(['/home/openclaw', '/mnt/alias'])
        for child in (
            '101 1 8:1 / /home/openclaw rw - ext4 /dev/sda1 rw\n',
            '101 1 8:1 / /mnt/alias rw - ext4 /dev/disk/by-uuid/alias rw\n',
        ):
            with self.subTest(child=child):
                self.set_mounts(base + child)
                rows = self.catalog()['roots']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['id'], original)
                self.assertEqual(rows[0]['mount'], '/drives/data/Users/admin')

    def test_external_full_alias_cannot_replace_absent_or_stacked_canonical(self):
        external = '101 1 8:1 / /home/openclaw rw - ext4 /dev/sda1 rw\n'
        self.disk['mounts'].append('/home/openclaw')
        self.set_mounts(external)
        self.assertFalse(self.catalog()['available'])
        self.set_mounts(external +
                        '100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n' +
                        '102 1 8:17 / /drives/data rw - ext4 /dev/sdb1 rw\n')
        self.assertFalse(self.catalog()['available'])

    def test_external_full_alias_and_users_mounts_do_not_reject_admin_planning(self):
        base = self.mountinfo.read_text()
        for target in ('/drives/data/Users', '/drives/data/Users/admin/app'):
            with self.subTest(target=target):
                self.set_mounts(base +
                    '101 1 8:1 / /home/openclaw rw - ext4 /dev/sda1 rw\n' +
                    '102 100 0:4 / ' + target + ' rw - tmpfs tmpfs rw\n')
                self.assertEqual(self.catalog()['roots'][0]['mount'], '/drives/data/Users/admin')

    def test_two_data_partitions_on_same_disk_have_independent_users_roots(self):
        original = self.catalog()['roots'][0]['id']
        self.disk['mounts'] = ['/drives/data', '/drives/other']
        self.set_mounts(self.mountinfo.read_text() +
                       '101 1 8:2 / /drives/other rw - ext4 /dev/sda2 rw\n')
        rows = self.catalog()['roots']
        self.assertEqual({row['mount'] for row in rows},
                         {'/drives/data/Users/admin', '/drives/other/Users/admin'})
        self.assertEqual(len({row['id'] for row in rows}), 2)
        self.assertEqual(next(row['id'] for row in rows
                              if row['mount'] == '/drives/data/Users/admin'), original)

    def test_external_subdirectory_bind_does_not_revoke_canonical_users_root(self):
        original = self.catalog()['roots'][0]['id']
        self.disk['mounts'].append('/home/openclaw')
        self.set_mounts(self.mountinfo.read_text() +
                       '101 1 8:1 /Users/fixture-user/openclaw /home/openclaw rw - ext4 /dev/sda1 rw\n')
        rows = self.catalog()['roots']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], original)
        self.assertEqual(rows[0]['mount'], '/drives/data/Users/admin')

    def test_external_bind_is_not_a_second_selectable_drive_root(self):
        original = self.catalog()['roots'][0]['id']
        self.disk['mounts'].append('/drives/alias')
        self.set_mounts(self.mountinfo.read_text() +
                       '101 1 8:1 /Users/fixture-user /drives/alias rw - ext4 /dev/sda1 rw\n')
        self.assertEqual([(row['id'], row['mount']) for row in self.catalog()['roots']],
                         [(original, '/drives/data/Users/admin')])

    def test_four_disk_dual_partition_layout_with_external_ssd_bind_is_plannable(self):
        lines, disks, expected = [], [], set()
        for index, name in enumerate(('sda', 'sdb', 'sdc', 'sdd')):
            mounts = [f'/drives/fixture_{name}_{partition}' for partition in (1, 2)]
            for partition, mount in enumerate(mounts, 1):
                minor = index * 16 + partition
                mount_id = 100 + index * 2 + partition
                lines.append(f'{mount_id} 1 8:{minor} / {mount} rw - ext4 /dev/{name}{partition} rw\n')
                expected.add(mount + '/Users/admin')
            disks.append({'name': name, 'serial': 'FIXTURE-' + name,
                          'user_name': 'fixture-' + name, 'mounts': mounts})
        disks[-1]['mounts'].append('/home/openclaw')
        lines.append('200 1 8:50 /Users/fixture-user/openclaw /home/openclaw rw - ext4 /dev/sdd2 rw\n')
        self.panel.overview.return_value = {'disks_detail': disks}
        self.set_mounts(''.join(lines))
        with mock.patch('os.stat', side_effect=AssertionError('no data path stat')), \
                mock.patch('os.scandir', side_effect=AssertionError('no user enumeration')):
            catalog = self.catalog()
        self.assertTrue(catalog['available'])
        self.assertEqual(catalog['status'], 'authorization_plan')
        self.assertEqual({row['mount'] for row in catalog['roots']}, expected)
        self.assertEqual(len({row['id'] for row in catalog['roots']}), 8)
        self.assertNotIn('/home/openclaw', repr(catalog))
        for disk in disks:
            self.assertNotIn(disk['serial'], repr(catalog))

    def test_same_serial_under_multiple_device_names_is_ambiguous(self):
        self.panel.overview.return_value = {'disks_detail': [self.disk,
            dict(self.disk, name='sdb', mounts=['/drives/other'])]}
        self.set_mounts(self.mountinfo.read_text() +
                       '101 1 8:17 / /drives/other rw - ext4 /dev/sdb1 rw\n')
        self.assertFalse(self.catalog()['available'])

    def test_independent_disks_have_distinct_users_roots(self):
        self.panel.overview.return_value = {'disks_detail': [self.disk,
            dict(self.disk, name='sdb', serial='SERIAL-B', mounts=['/drives/backup'])]}
        self.set_mounts(self.mountinfo.read_text() +
                       '101 1 8:17 / /drives/backup rw - ext4 /dev/sdb1 rw\n')
        rows = self.catalog()['roots']
        self.assertEqual({row['mount'] for row in rows},
                         {'/drives/data/Users/admin', '/drives/backup/Users/admin'})
        self.assertEqual(len({row['id'] for row in rows}), 2)

    def test_stacked_ancestor_cannot_approve_hidden_data_root(self):
        for ancestor in ('/', '/drives'):
            with self.subTest(ancestor=ancestor):
                self.set_mounts('100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n' +
                    f'101 1 0:1 / {ancestor} rw - tmpfs first rw\n' +
                    f'102 1 0:2 / {ancestor} rw - tmpfs second rw\n')
                self.assertFalse(self.catalog()['available'])

    def test_mount_readonly_is_checked_separately_from_identity(self):
        original = self.catalog()['roots'][0]['id']
        self.set_mounts('100 1 8:1 / /drives/data ro - ext4 /dev/sda1 rw\n')
        current = self.catalog()
        self.assertEqual(current['roots'][0]['id'], original)
        self.assertTrue(current['roots'][0]['read_only'])
        with self.assertRaises(ValueError):
            validate_file_grants([{'id': original, 'path': '', 'access': 'readwrite'}], current)

    def test_unknown_or_conflicting_mount_write_flags_are_unavailable(self):
        for mount_flags, super_flags in (('noexec', 'rw'), ('rw', 'noexec'),
                                         ('rw,ro', 'rw'), ('rw', 'rw,ro')):
            with self.subTest(mount_flags=mount_flags, super_flags=super_flags):
                self.set_mounts('100 1 8:1 / /drives/data ' + mount_flags +
                                ' - ext4 /dev/sda1 ' + super_flags + '\n')
                self.assertFalse(self.catalog()['available'])

    def test_malformed_mountinfo_never_hides_a_subtree_mount(self):
        base = self.mountinfo.read_text()
        for malformed in ('broken mount row\n',
                          '101 1 0:4 / /drives/data/Users rw - incomplete\n',
                          '101 1 invalid / /drives/data/Users rw - tmpfs tmpfs rw\n'):
            with self.subTest(malformed=malformed):
                self.set_mounts(base + malformed)
                self.assertFalse(self.catalog()['available'])

    def test_relative_paths_never_escape_or_reinterpret_users_root(self):
        invalid = ('/drives/data/Users', '../Applications', 'alice/../../Applications',
                   'alice/../bob', 'alice/./file', 'alice//file', 'alice/',
                   'alice\\file', 'a\x00b', '\ud800', 'a' * 1025)
        catalog = self.catalog()
        identifier = catalog['roots'][0]['id']
        for path in invalid:
            with self.subTest(path=path):
                self.assertFalse(valid_relative_file_path(path))
                with self.assertRaises(ValueError):
                    validate_file_grants([{'id': identifier, 'path': path, 'access': 'readonly'}], catalog)
        # Paths are literals. Never URL-decode a granted name into traversal.
        for path in ('', 'alice', 'alice/视频', 'alice/%2e%2e'):
            self.assertTrue(valid_relative_file_path(path))
            validate_file_grants([{'id': identifier, 'path': path, 'access': 'readonly'}], catalog)

    def test_invalid_catalog_or_access_cannot_validate_grants(self):
        catalog = self.catalog()
        grant = {'id': catalog['roots'][0]['id'], 'path': '', 'access': 'readonly'}
        for changed in (None, {}, dict(catalog, boundary_version=1), dict(catalog, boundary_version=2),
                        dict(catalog, boundary_version=3.0), dict(catalog, available=False),
                        dict(catalog, roots=[None]), dict(catalog, roots=[]),
                        dict(catalog, roots=catalog['roots'] * 2)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_file_grants([grant], changed)
        whole_disk = dict(catalog, roots=[dict(catalog['roots'][0], mount='/drives/data')])
        with self.assertRaises(ValueError):
            validate_file_grants([grant], whole_disk)
        for changed in (dict(grant, access='admin'), dict(grant, access=True)):
            with self.assertRaises(ValueError):
                validate_file_grants([changed], catalog)
        with self.assertRaises(ValueError):
            validate_file_grants([grant, dict(grant)], catalog)


if __name__ == '__main__':
    unittest.main()
