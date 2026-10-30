"""Fixed context evidence; fixture metadata never becomes file authorization."""

import copy
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
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-file-context-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('etc', 'tmp/nand_config', 'proc/sys/kernel/random', 'proc/self'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / 'etc/passwd').write_text('root:x:0:0:fixture:/root:/bin/false\n', encoding='utf-8')
        (self.root / 'etc/group').write_text('root:x:0:\n', encoding='utf-8')
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('fixture-boot\n', encoding='utf-8')
        (self.root / 'proc/self/mountinfo').write_text(
            '100 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n', encoding='utf-8')
        self.catalog = {'available': True, 'status': 'authorization_plan', 'boundary_version': 2,
                        'roots': [{'id': ROOT, 'mount': '/drives/data/Users',
                                   'label': 'fixture', 'read_only': False}]}

    def collect(self, **options):
        return files.collect_file_backend_context(None, root=self.root,
            root_collector=lambda unused: copy.deepcopy(self.catalog),
            runtime_reader=lambda **unused: {'available': True, 'status': 'verified', 'secret': PRIVATE},
            **options)

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
        with mock.patch.object(files, '_users_context', return_value={
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
        with mock.patch.object(files, '_users_context', return_value={
                'root_id': ROOT, 'status': 'verified', 'reason': 'directory_handle_metadata_only',
                'handle_fingerprint': 'a' * 64}):
            result = files.collect_file_backend_context(None, root=self.root,
                root_collector=collector, owner_reader=lambda **unused: True,
                runtime_reader=lambda **unused: {})
        self.assertFalse(result['complete'])
        self.assertEqual(result['roots'][0]['reason'], 'root_lifecycle_changed')
        self.assertIsNone(result['roots'][0]['handle_fingerprint'])

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH'), 'Linux O_PATH required')
    def test_linux_directory_handle_refuses_users_symlink_and_reports_only_metadata(self):
        users = self.root / 'drives/data/Users'
        users.mkdir(parents=True)
        descriptor = os.open(users, os.O_PATH | os.O_DIRECTORY)
        try:
            expected_id = Path('/proc/self/fdinfo/' + str(descriptor)).read_text().split('mnt_id:\t')[1].splitlines()[0]
            info = os.fstat(descriptor)
            expected = (expected_id, f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}')
        finally:
            os.close(descriptor)
        with mock.patch.object(files, '_descriptor_mount_id', return_value=expected_id):
            value = files._users_context(self.root, self.catalog['roots'][0], expected)
        self.assertEqual(value['status'], 'verified')
        self.assertEqual(value['native_acl'], 'unverified')
        self.assertNotIn(str(self.root), json.dumps(value))
        users.rmdir()
        target = self.root / 'outside'
        target.mkdir()
        users.symlink_to(target, target_is_directory=True)
        with mock.patch.object(files, '_descriptor_mount_id', return_value=expected_id):
            value = files._users_context(self.root, self.catalog['roots'][0], expected)
        self.assertEqual(value['status'], 'unverified')
        self.assertIsNone(value['handle_fingerprint'])


if __name__ == '__main__':
    unittest.main()
