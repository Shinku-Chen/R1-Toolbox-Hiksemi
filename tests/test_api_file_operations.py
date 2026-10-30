"""Fixed context evidence; fixture metadata never becomes file authorization."""

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_operations as files


ROOT = 'root_' + 'a' * 24
PRIVATE = 'private-account-credential-fixture'


class FileContextTests(unittest.TestCase):
    def test_legacy_users_authority_error_is_fixed_and_not_retryable(self):
        self.assertEqual(files.FileAccessError('legacy_users_boundary_v2').result(), {
            'ok': False, 'error': {'code': 'legacy_users_boundary_v2',
                'message': '旧 Users 根的恢复登记尚未迁移，请先使用原镜像处理保管对象。', 'retryable': False}})

    def test_cross_mount_error_is_fixed_and_not_retryable(self):
        self.assertEqual(files.FileAccessError('cross_mount_denied').result(), {
            'ok': False, 'error': {'code': 'cross_mount_denied',
                'message': '此路径进入额外挂载，当前文件授权不允许跨越该挂载。', 'retryable': False}})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-file-context-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('etc', 'tmp/nand_config', 'proc/sys/kernel/random', 'proc/self/ns', 'proc/1/ns'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / 'etc/passwd').write_text('root:x:0:0:fixture:/root:/bin/false\n', encoding='utf-8')
        (self.root / 'etc/group').write_text('root:x:0:\n', encoding='utf-8')
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('fixture-boot\n', encoding='utf-8')
        (self.root / 'proc/self/mountinfo').write_text(
            '100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n', encoding='utf-8')
        (self.root / 'proc/self/ns/mnt').write_text('fixture namespace', encoding='utf-8')
        os.link(self.root / 'proc/self/ns/mnt', self.root / 'proc/1/ns/mnt')
        self.catalog = {'available': True, 'status': 'authorization_plan', 'boundary_version': 3,
                        'roots': [{'id': ROOT, 'mount': '/drives/data/Users/admin',
                                   'label': 'fixture', 'read_only': False}]}

    def collect(self, **options):
        return files.collect_file_backend_context(None, root=self.root,
            root_collector=lambda unused: copy.deepcopy(self.catalog),
            runtime_reader=lambda **unused: {'available': True, 'status': 'verified', 'secret': PRIVATE},
            **options)

    def topology(self, catalog=None):
        catalog = self.catalog if catalog is None else catalog
        return {'schema_version': 1, 'scope': 'file_root_topology', 'boundary_version': 3,
            'available': True, 'complete': True, 'reason_codes': [],
            'catalog_fingerprint': hashlib.sha256(json.dumps(catalog, sort_keys=True,
                ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest(),
            'catalog_root_ids': [row['id'] for row in catalog['roots']],
            'candidates': [{'candidate_id': 'fixture', 'reason_codes': ['source_unsupported']}],
            'inputs': {'mountinfo': {'status': 'ok', 'eof': True, 'bytes_read': 70,
                'parsed_records': 1, 'malformed_records': 0}, 'cached_disks': {
                'status': 'ok', 'records': 1, 'valid_identity_records': 1, 'invalid_identity_records': 0}},
            'effects': {'metadata_only': True, 'opens_data_paths': False,
                'stats_data_paths': False, 'grants_authorization': False}}

    def verified_context(self, **options):
        with mock.patch.object(files, '_admin_context', return_value={
                'root_id': ROOT, 'status': 'verified', 'reason': 'directory_handle_metadata_only',
                'handle_fingerprint': 'a' * 64}):
            return self.collect(owner_reader=lambda **unused: True, **options)

    def owner_database(self):
        path = self.root / 'tmp/nand_config/USER_AUTH_DB_101'
        connection = sqlite3.connect(path)
        connection.executescript('CREATE TABLE tb_user_auth(user_id INTEGER,user_type INTEGER);'
                                 'CREATE TABLE tb_terminal_token(token TEXT);')
        connection.execute('INSERT INTO tb_user_auth VALUES(100000,0)')
        connection.execute('INSERT INTO tb_terminal_token VALUES(?)', (PRIVATE,))
        connection.commit()
        connection.close()
        return path

    def test_owner_metadata_never_reads_token_table_or_returns_account_fields(self):
        self.owner_database()
        result = self.collect()
        self.assertEqual(result['owner']['status'], 'verified')
        self.assertEqual(result['unix_mapping']['status'], 'unverified')
        self.assertFalse(result['file_access_available'])
        self.assertFalse(result['effects']['reads_credentials'])
        self.assertFalse(result['effects']['calls_native_actions'])
        self.assertTrue(result['effects']['may_wake_disks'])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn('tb_terminal_token', json.dumps(result))

    def test_a_numeric_unix_uid_candidate_is_never_an_authority(self):
        (self.root / 'etc/passwd').write_text(
            PRIVATE + ':x:100000:100000:fixture:/private:/bin/false\n', encoding='utf-8')
        result = self.collect(owner_reader=lambda **unused: True)
        self.assertEqual(result['unix_mapping'], {'status': 'unverified',
                         'reason': 'same_numeric_uid_is_only_a_candidate'})
        self.assertFalse(result['file_access_available'])
        self.assertNotIn('nas_subject_to_unix_mapping', result['blockers'])
        self.assertFalse(result['unix_identity_mapping_required'])
        self.assertIn('native_root_read_permission', result['blockers'])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_owner_view_indirection_to_credential_columns_is_refused(self):
        path = self.owner_database()
        connection = sqlite3.connect(path)
        connection.executescript('DROP TABLE tb_user_auth; CREATE VIEW tb_user_auth AS '
                                'SELECT token AS user_id, 0 AS user_type FROM tb_terminal_token;')
        connection.close()
        self.assertEqual(self.collect()['owner']['status'], 'unverified')

    def test_bad_fixed_config_metadata_is_reported_without_raw_error(self):
        (self.root / 'etc/passwd').write_text(PRIVATE + '\n', encoding='utf-8')
        result = self.collect(owner_reader=lambda **unused: False)
        self.assertEqual(result['unix_mapping']['reason'], 'unix_mapping_metadata_unavailable')
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_zero_size_proc_short_reads_keep_later_mounts_and_exact_ids(self):
        rows = ('100 1 8:2 / /drives/system rw - ext4 /dev/sda2 rw\n'
                '605 1 8:17 / /drives/data rw - ext4 /dev/sdb1 rw\n'
                '638 1 8:1 / /drives/another rw - ext4 /dev/sda1 rw\n')
        (self.root / 'proc/self/mountinfo').write_text(rows, encoding='utf-8')
        real_read, real_fstat = os.read, os.fstat

        def proc_stat(descriptor):
            value = real_fstat(descriptor)
            return SimpleNamespace(st_size=0, **{field: getattr(value, field) for field in
                ('st_mode', 'st_dev', 'st_ino', 'st_mtime_ns')})

        with mock.patch.object(files.os, 'read', side_effect=lambda fd, count:
                               real_read(fd, min(count, 7))), \
             mock.patch.object(files.os, 'fstat', side_effect=proc_stat):
            result = files._mounts(self.root)
        self.assertEqual(result, {'/drives/system': [('100', '8:2')],
                                  '/drives/data': [('605', '8:17')],
                                  '/drives/another': [('638', '8:1')]})

    def test_zero_size_proc_short_reads_still_enforce_cap_and_close_handle(self):
        real_open, real_read, real_fstat, real_close = os.open, os.read, os.fstat, os.close
        opened, closed = [], []

        def track_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def track_close(descriptor):
            closed.append(descriptor)
            return real_close(descriptor)

        def proc_stat(descriptor):
            value = real_fstat(descriptor)
            return SimpleNamespace(st_size=0, **{field: getattr(value, field) for field in
                ('st_mode', 'st_dev', 'st_ino', 'st_mtime_ns')})

        with mock.patch.object(files.os, 'open', side_effect=track_open), \
             mock.patch.object(files.os, 'read', side_effect=lambda fd, count:
                               real_read(fd, min(count, 3))), \
             mock.patch.object(files.os, 'fstat', side_effect=proc_stat), \
             mock.patch.object(files.os, 'close', side_effect=track_close), \
             self.assertRaisesRegex(ValueError, 'metadata_changed'):
            files._read_fixed(self.root, 'proc/self/mountinfo', maximum=16)
        self.assertEqual(opened, closed)

    def test_root_limit_and_failures_keep_a_complete_fixed_gap_report(self):
        self.catalog['roots'] *= 9
        with mock.patch.object(files, '_admin_context', return_value={
                'root_id': ROOT, 'status': 'unverified', 'reason': 'fixture'}):
            result = self.collect(owner_reader=lambda **unused: True)
        self.assertEqual(len(result['roots']), 8)
        self.assertFalse(result['complete'])
        self.assertFalse(result['native_contract']['runtime_verified'])
        self.assertFalse(result['file_access_available'])

    def test_lifecycle_change_invalidates_even_successfully_opened_context(self):
        after = copy.deepcopy(self.catalog)
        after['roots'][0]['id'] = 'root_' + 'b' * 24
        collector = mock.Mock(side_effect=[copy.deepcopy(self.catalog), after])
        with mock.patch.object(files, '_admin_context', return_value={
                'root_id': ROOT, 'status': 'verified', 'reason': 'directory_handle_metadata_only',
                'handle_fingerprint': 'a' * 64}):
            result = files.collect_file_backend_context(None, root=self.root,
                root_collector=collector, owner_reader=lambda **unused: True,
                runtime_reader=lambda **unused: {})
        self.assertFalse(result['complete'])
        self.assertEqual(result['roots'][0]['reason'], 'root_lifecycle_changed')
        self.assertIsNone(result['roots'][0]['handle_fingerprint'])

    def test_injected_catalog_does_not_accidentally_collect_real_host_topology(self):
        with mock.patch('api_file_roots.collect_file_root_topology', create=True,
                        side_effect=AssertionError('real host must not be inspected')) as topology:
            result = self.collect()
        topology.assert_not_called()
        self.assertEqual(result['root_topology']['reason_codes'], ['topology_not_collected'])
        self.assertFalse(result['complete'])

    def test_complete_topology_and_known_namespace_do_not_grant_file_access(self):
        topology = self.topology()
        result = self.verified_context(topology_collector=lambda unused: topology)
        self.assertTrue(result['root_topology']['complete'])
        self.assertTrue(result['complete'])
        self.assertTrue(result['namespace']['stable'])
        self.assertTrue(result['namespace']['same_namespace'])
        self.assertFalse(result['file_access_available'])
        self.assertFalse(result['native_contract']['runtime_verified'])
        self.assertIn('native_root_read_permission', result['blockers'])
        self.assertEqual(result['root_topology']['candidates'], topology['candidates'])

    def test_rejected_topology_survives_zero_selected_roots_without_opening_data(self):
        self.catalog.update(available=False, status='unavailable', roots=[])
        topology = self.topology()
        with mock.patch.object(files, '_admin_context', side_effect=AssertionError('no data path')) as users, \
                mock.patch.object(files.os, 'scandir', side_effect=AssertionError('no directory listing')), \
                mock.patch.object(files.os, 'listdir', side_effect=AssertionError('no directory listing')):
            result = self.collect(topology_collector=lambda unused: topology)
        users.assert_not_called()
        self.assertTrue(result['root_topology']['complete'])
        self.assertEqual(result['root_topology']['candidates'], topology['candidates'])
        self.assertEqual(result['root_topology']['catalog_root_ids'], [])
        self.assertFalse(result['complete'])
        self.assertFalse(result['file_access_available'])

    def test_selected_users_failure_does_not_erase_complete_topology(self):
        with mock.patch.object(files, '_admin_context', return_value={
                'root_id': ROOT, 'status': 'unverified', 'reason': 'safe_metadata_open_unavailable'}):
            result = self.collect(topology_collector=lambda unused: self.topology())
        self.assertTrue(result['root_topology']['complete'])
        self.assertFalse(result['complete'])

    def test_topology_fingerprint_and_root_id_order_bind_to_exact_context_catalog(self):
        for field, value in (('catalog_fingerprint', '0' * 64), ('catalog_root_ids', [])):
            with self.subTest(field=field):
                topology = self.topology()
                topology[field] = value
                result = self.verified_context(topology_collector=lambda unused: topology)
                self.assertFalse(result['complete'])
                self.assertFalse(result['root_topology']['complete'])
                self.assertIn('context_catalog_mismatch', result['root_topology']['reason_codes'])
                self.assertTrue(topology['complete'])

    def test_unknown_metadata_counts_or_partial_topology_never_report_complete(self):
        for group, field, value in (('mountinfo', 'parsed_records', None),
                ('mountinfo', 'bytes_read', True), ('mountinfo', 'eof', False),
                ('cached_disks', 'records', None), ('cached_disks', 'invalid_identity_records', False)):
            with self.subTest(group=group, field=field):
                topology = self.topology()
                topology['inputs'][group][field] = value
                result = self.verified_context(topology_collector=lambda unused: topology)
                self.assertFalse(result['complete'])
                self.assertFalse(result['root_topology']['complete'])
                self.assertIn('topology_metadata_incomplete', result['root_topology']['reason_codes'])
        topology = self.topology()
        topology.update(complete=False, reason_codes=['mountinfo_incomplete'])
        result = self.verified_context(topology_collector=lambda unused: topology)
        self.assertFalse(result['complete'])
        self.assertEqual(result['root_topology']['candidates'], topology['candidates'])

    def test_catalog_change_after_topology_invalidates_its_context_binding(self):
        after = copy.deepcopy(self.catalog)
        after['roots'][0]['label'] = 'changed label without changing root ID'
        topology = self.topology()
        with mock.patch.object(files, '_admin_context', return_value={
                'root_id': ROOT, 'status': 'verified', 'handle_fingerprint': 'a' * 64}):
            result = files.collect_file_backend_context(None, root=self.root,
                root_collector=mock.Mock(side_effect=[copy.deepcopy(self.catalog), after]),
                topology_collector=lambda unused: topology,
                owner_reader=lambda **unused: True, runtime_reader=lambda **unused: {})
        self.assertFalse(result['complete'])
        self.assertFalse(result['root_topology']['complete'])
        self.assertIn('context_catalog_mismatch', result['root_topology']['reason_codes'])
        self.assertEqual(result['roots'][0]['reason'], 'root_lifecycle_changed')

    def test_topology_exception_is_fixed_private_and_does_not_stop_other_context(self):
        topology = mock.Mock(side_effect=OSError('/private/' + PRIVATE))
        result = self.verified_context(topology_collector=topology)
        self.assertEqual(result['root_topology']['reason_codes'], ['topology_unavailable'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['roots'][0]['status'], 'verified')
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_missing_real_namespace_metadata_keeps_context_incomplete(self):
        (self.root / 'proc/1/ns/mnt').unlink()
        result = self.verified_context(topology_collector=lambda unused: self.topology())
        self.assertFalse(result['complete'])
        self.assertEqual(result['namespace']['reason'], 'namespace_unavailable')
        self.assertIsNone(result['namespace']['before']['pid1'])
        self.assertTrue(result['root_topology']['complete'])

    def test_different_real_namespace_metadata_keeps_context_incomplete(self):
        (self.root / 'proc/1/ns/mnt').unlink()
        (self.root / 'proc/1/ns/mnt').write_text('different namespace', encoding='utf-8')
        result = self.verified_context(topology_collector=lambda unused: self.topology())
        self.assertFalse(result['complete'])
        self.assertTrue(result['namespace']['stable'])
        self.assertFalse(result['namespace']['same_namespace'])
        self.assertEqual(result['namespace']['reason'], 'namespace_mismatch')

    def test_real_namespace_change_during_collection_keeps_context_incomplete(self):
        calls = []
        def namespace_reader(*, root):
            evidence = files._mount_namespaces(root)
            if not calls:
                alternate = root / 'proc/alternate-mnt'
                alternate.write_text('new namespace', encoding='utf-8')
                (root / 'proc/self/ns/mnt').unlink()
                (root / 'proc/1/ns/mnt').unlink()
                os.link(alternate, root / 'proc/self/ns/mnt')
                os.link(alternate, root / 'proc/1/ns/mnt')
            calls.append(evidence)
            return evidence
        result = self.verified_context(topology_collector=lambda unused: self.topology(),
                                       namespace_reader=namespace_reader)
        self.assertFalse(result['complete'])
        self.assertFalse(result['namespace']['stable'])
        self.assertTrue(result['namespace']['same_namespace'])
        self.assertEqual(result['namespace']['reason'], 'namespace_changed')

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH'), 'Linux O_PATH required')
    def test_linux_directory_handle_refuses_users_symlink_and_reports_only_metadata(self):
        users = self.root / 'drives/data/Users'
        users.mkdir(parents=True)
        admin = users / 'admin'
        admin.mkdir()
        descriptor = os.open(admin, os.O_PATH | os.O_DIRECTORY)
        try:
            expected_id = Path('/proc/self/fdinfo/' + str(descriptor)).read_text().split('mnt_id:\t')[1].splitlines()[0]
            info = os.fstat(descriptor)
            expected = (expected_id, f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}')
        finally:
            os.close(descriptor)
        with mock.patch.object(files, '_descriptor_mount_id', return_value=expected_id):
            value = files._admin_context(self.root, self.catalog['roots'][0], expected)
        self.assertEqual(value['status'], 'verified')
        self.assertEqual(value['native_acl'], 'unverified')
        self.assertNotIn(str(self.root), json.dumps(value))
        admin.rmdir()
        users.rmdir()
        target = self.root / 'outside'
        target.mkdir()
        users.symlink_to(target, target_is_directory=True)
        with mock.patch.object(files, '_descriptor_mount_id', return_value=expected_id):
            value = files._admin_context(self.root, self.catalog['roots'][0], expected)
        self.assertEqual(value['status'], 'unverified')
        self.assertIsNone(value['handle_fingerprint'])

    def test_admin_context_opens_five_fixed_stages_and_checks_each_physical_descendant(self):
        fakeos = mock.Mock(wraps=os)
        fakeos.name = 'posix'
        for name, fallback in (('O_PATH', 0x200000), ('O_DIRECTORY', 0x10000),
                               ('O_NOFOLLOW', 0x20000), ('O_CLOEXEC', 0x80000)):
            setattr(fakeos, name, getattr(os, name, fallback))
        fakeos.open.side_effect = range(10, 15)
        fakeos.fstat.side_effect = lambda fd: SimpleNamespace(st_dev=1, st_ino=fd, st_ctime_ns=123)
        fakeos.major, fakeos.minor = mock.Mock(return_value=8), mock.Mock(return_value=1)
        fakeos.close.return_value = None
        with mock.patch.object(files, 'os', fakeos), \
             mock.patch.object(files, '_descriptor_mount_id', return_value='100') as mount:
            result = files._admin_context(self.root, self.catalog['roots'][0], ('100', '8:1'))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([call.args[0] for call in fakeos.open.call_args_list[1:]],
                         ['drives', 'data', 'Users', 'admin'])
        self.assertEqual([call.args[1] for call in mount.call_args_list], [12, 13, 14])
        self.assertEqual([call.args[0] for call in fakeos.close.call_args_list], [14, 13, 12, 11, 10])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_admin_context_stops_at_users_or_admin_same_device_bind_mount(self):
        for mount_ids, expected_opened in ((['100', '101'], 4), (['100', '100', '101'], 5)):
            with self.subTest(mount_ids=mount_ids):
                fakeos = mock.Mock(wraps=os)
                fakeos.name = 'posix'
                for name, fallback in (('O_PATH', 0x200000), ('O_DIRECTORY', 0x10000),
                                       ('O_NOFOLLOW', 0x20000), ('O_CLOEXEC', 0x80000)):
                    setattr(fakeos, name, getattr(os, name, fallback))
                fakeos.open.side_effect = range(10, 15)
                fakeos.fstat.side_effect = lambda fd: SimpleNamespace(st_dev=1, st_ino=fd, st_ctime_ns=123)
                fakeos.major, fakeos.minor = mock.Mock(return_value=8), mock.Mock(return_value=1)
                fakeos.close.return_value = None
                with mock.patch.object(files, 'os', fakeos), \
                     mock.patch.object(files, '_descriptor_mount_id', side_effect=mount_ids):
                    result = files._admin_context(self.root, self.catalog['roots'][0], ('100', '8:1'))
                self.assertEqual(result['status'], 'unverified')
                self.assertEqual(result['reason'], 'opened_mount_identity_unconfirmed')
                self.assertEqual(fakeos.open.call_count, expected_opened)
                self.assertEqual(fakeos.close.call_count, expected_opened)

    def test_context_rejects_legacy_users_boundary_even_if_root_id_shape_is_valid(self):
        for version, path in ((2, '/drives/data/Users'), (3, '/drives/data/Users'),
                              (3, '/drives/data/Users/public')):
            with self.subTest(version=version, path=path):
                self.catalog['boundary_version'] = version
                self.catalog['roots'][0]['mount'] = path
                with mock.patch.object(files, '_admin_context', side_effect=AssertionError('legacy must not open')):
                    result = self.collect(owner_reader=lambda **unused: True,
                                          topology_collector=lambda unused: self.topology())
                self.assertFalse(result['complete'])
                self.assertEqual(result['roots'], [])


if __name__ == '__main__':
    unittest.main()
