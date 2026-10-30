"""Fixed native archive protocol tests; Linux fixtures are not NAS acceptance."""

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_native_recycle as recycle
from api_file_operations import FileAccessError


ROW = {'id': 'root_' + 'a' * 24, 'mount': '/drives/data/Users', 'read_only': False}
PATH = 'admin/folder/sample.bin'
NATIVE = ((123, 40, '99', '127.0.0.1'), '100', (1, 2), (3, 4))
CREATE = ('CREATE TABLE tb_recycle(' + ','.join('"%s" %s%s' % (name, kind,
          ' PRIMARY KEY AUTOINCREMENT' if i == 0 else '')
          for i, (name, kind) in enumerate(zip(recycle._COLUMNS, recycle._TYPES))) + ')')
INDEX = 'CREATE INDEX deltime_index ON tb_recycle(deltime DESC)'
RECORD = ('a' * 32, '/Users/' + PATH, 'sample.bin', 0, 1700000000,
          recycle.OWNER_ID, 'b' * 32 + '.bin', 8193, 1700000000, 1700000000)


class RecycleSchemaAndContractTests(unittest.TestCase):
    def connection(self):
        connection = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute(CREATE)
        connection.execute(INDEX)
        return connection

    def error(self, code, callback):
        with self.assertRaises(FileAccessError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_exact_seventeen_column_factory_schema_accepts_only_fixed_record(self):
        connection = self.connection()
        connection.set_authorizer(recycle._schema_authorizer)
        recycle._schema(connection)
        connection.set_authorizer(recycle._data_authorizer(write=True))
        connection.execute(recycle._INSERT, RECORD)
        self.assertEqual(connection.execute(recycle._SELECT, (RECORD[6], recycle.OWNER_ID)).fetchall(), [RECORD])

    def test_extra_trigger_other_index_or_default_cannot_be_adopted(self):
        for sql in ('CREATE TRIGGER extra AFTER INSERT ON tb_recycle BEGIN SELECT 1; END',
                    'CREATE INDEX extra ON tb_recycle(src)'):
            connection = self.connection()
            connection.execute(sql)
            connection.set_authorizer(recycle._schema_authorizer)
            self.error('write_backend_unverified', lambda: recycle._schema(connection))
        connection = self.connection()
        connection.execute('ALTER TABLE tb_recycle ADD COLUMN hidden TEXT DEFAULT "x"')
        connection.set_authorizer(recycle._schema_authorizer)
        self.error('write_backend_unverified', lambda: recycle._schema(connection))

    def test_fixed_authorizer_cannot_access_credentials_attach_or_change_schema(self):
        connection = self.connection()
        connection.execute('CREATE TABLE tb_user_auth(access_token TEXT)')
        connection.set_authorizer(recycle._data_authorizer(write=True))
        for sql in ('SELECT access_token FROM tb_user_auth', 'SELECT tbl_name FROM sqlite_master',
                    'SELECT upper(src) FROM tb_recycle', 'ATTACH DATABASE ":memory:" AS other',
                    'CREATE TABLE extra(x)', 'PRAGMA journal_mode=WAL'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                connection.execute(sql).fetchall()
        connection.execute(recycle._INSERT, RECORD)
        connection.set_authorizer(recycle._data_authorizer())
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute(recycle._INSERT, RECORD)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute(recycle._DELETE, RECORD + (1,))

    def test_exact_owner_row_delete_never_deletes_another_archive(self):
        connection = self.connection()
        identifier = connection.execute(recycle._INSERT, RECORD).lastrowid
        other = list(RECORD)
        other[6] = 'c' * 32 + '.bin'
        connection.execute(recycle._INSERT, other)
        connection.set_authorizer(recycle._data_authorizer(deleting=True))
        self.assertEqual(connection.execute(recycle._DELETE, RECORD + (identifier,)).rowcount, 1)
        self.assertEqual(connection.execute(recycle._SELECT, (other[6], recycle.OWNER_ID)).fetchall(), [tuple(other)])

    def test_scope_is_original_admin_descendant_and_not_internal_or_public(self):
        recycle._arguments(ROW, PATH)
        for path in ('admin', 'public/file', 'another/private', 'admin/.DATA_CACHE/Recycle',
                     '../admin/file', 'admin/a|b', 'admin/' + 'x' * 1020):
            with self.assertRaises(FileAccessError, msg=path):
                recycle._arguments(ROW, path)

    def test_archive_name_bound_precedes_capture_and_supports_trailing_dot(self):
        recycle._arguments(ROW, 'admin/folder/report.')
        recycle._arguments(ROW, 'admin/folder/a.' + 'x' * 222)
        self.error('limit_exceeded', lambda: recycle._arguments(ROW, 'admin/folder/a.' + 'x' * 223))
        self.error('limit_exceeded', lambda: recycle._arguments(ROW, 'admin/folder/a.' + '字' * 75))

    def test_uuid_comes_from_audited_script_newline_hash_and_device_bound_udev(self):
        fs_uuid = '12345678-1234-1234-1234-123456789abc'
        script = 'audited script fixture\n'
        metadata = {'histor/bin/fs/get_zonelist_info.sh': script,
            'proc/self/mountinfo': '11 1 8:1 / /drives/data rw - ext4 /dev/sda1 rw\n',
            'run/udev/data/b8:1': 'E:DEVNAME=/dev/sda1\nE:ID_FS_TYPE=ext4\nE:ID_FS_UUID=' + fs_uuid + '\n',
            'sys/class/block/sda1/uevent': 'MAJOR=8\nMINOR=1\nDEVNAME=sda1\n'}
        with mock.patch.object(recycle, '_read_fixed', side_effect=lambda root, path, *args: metadata[path]), \
                mock.patch.object(recycle, '_SCRIPT_SHA256', hashlib.sha256(script.encode()).hexdigest()), \
                mock.patch.object(recycle, '_internal_sata', return_value=True):
            self.assertEqual(recycle._native_uuid('/', ROW),
                             hashlib.md5((fs_uuid + '\n').encode(), usedforsecurity=False).hexdigest())
            metadata['run/udev/data/b8:1'] = metadata['run/udev/data/b8:1'].replace('/dev/sda1', '/dev/sdb1')
            self.error('write_backend_unverified', lambda: recycle._native_uuid('/', ROW))
            metadata['histor/bin/fs/get_zonelist_info.sh'] += 'modified'
            self.error('write_backend_unverified', lambda: recycle._native_uuid('/', ROW))

    def test_row_identity_and_reserved_flags_prevent_foreign_delete(self):
        connection = self.connection()
        identifier = connection.execute(recycle._INSERT, RECORD).lastrowid
        self.assertEqual(recycle._record_identity(connection, RECORD, expected_id=identifier), identifier)
        self.error('result_unconfirmed', lambda: recycle._record_identity(connection, RECORD, expected_id=identifier+1))
        connection.execute('UPDATE tb_recycle SET i32flag1=1 WHERE id=?', (identifier,))
        self.error('result_unconfirmed', lambda: recycle._record_identity(connection, RECORD, expected_id=identifier))
        connection.set_authorizer(recycle._data_authorizer(deleting=True))
        self.assertEqual(connection.execute(recycle._DELETE, RECORD + (identifier,)).rowcount, 0)

    def test_sqlite_proof_checks_real_journal_fd_identity_and_open_flags(self):
        import stat
        opened = object.__new__(recycle._OpenedRecycle)
        main, journal, wrong = [(1, number, stat.S_IFREG, 1) for number in (2, 3, 4)]
        opened.main_state, opened.directory = main, 10
        opened.check = mock.Mock()
        opened._mount = mock.Mock()
        journal_stat = mock.Mock(st_dev=1, st_ino=3, st_mode=stat.S_IFREG, st_nlink=1)
        with mock.patch.object(recycle.os, 'stat', return_value=journal_stat), \
                mock.patch.object(recycle.os, 'O_ACCMODE', 3, create=True), \
                mock.patch.object(recycle, '_fd_snapshot', return_value={20:main, 21:journal}), \
                mock.patch.object(recycle, '_fd_flags', return_value=os.O_RDWR):
            opened.sqlite_proof({}, write=True, journal=True)
            with mock.patch.object(recycle, '_fd_snapshot', return_value={20:main, 21:wrong}):
                self.error('write_backend_unverified', lambda: opened.sqlite_proof({}, write=True, journal=True))
            with mock.patch.object(recycle, '_fd_flags', return_value=os.O_RDONLY):
                self.error('write_backend_unverified', lambda: opened.sqlite_proof({}, write=True, journal=True))

    def test_no_actual_delete_or_general_sql_action_or_path_proxy_exists(self):
        instance = recycle.NativeFileRecycle()
        self.assertFalse(hasattr(instance, 'delete'))
        self.assertFalse(hasattr(instance, 'request'))
        self.assertFalse(hasattr(instance, 'execute_sql'))
        with mock.patch.object(recycle, '_listener_signature') as listener:
            self.error('invalid_key', lambda: instance.preflight(ROW, PATH, current=lambda: False))
            listener.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_PATH') and os.geteuid() == 0,
                     'Linux root, real O_PATH, exact fd/mount metadata and SQLite Unix VFS required')
class NativeRecycleLinuxTests(unittest.TestCase):
    def setUp(self):
        from api_file_mutation_authority import _write_at
        temporary = tempfile.TemporaryDirectory(prefix='r1-native-recycle-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'drives/data/Users'
        self.archive = self.users / recycle._ARCHIVE
        self.archive.mkdir(parents=True)
        self.parent = self.users / 'admin/folder'
        self.parent.mkdir()
        self.db = self.root / 'drives/data/.DATA_CACHE/Recycle/Recycle.db'
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as connection:
            connection.execute(CREATE)
            connection.execute(INDEX)
        self.stage_path = self.users / '.r1-toolbox-ops/operation_fixture'
        self.stage_path.mkdir(parents=True)
        self.stage_fd = os.open(self.stage_path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.stage_fd)
        self.stage = mock.Mock(fd=self.stage_fd, operation_id='operation_fixture')
        self.stage.record_recovery.side_effect = lambda value: _write_at(self.stage_fd, 'manifest.json', value)
        source = self.stage_path / 'source'
        source.write_bytes(b'owner isolated fixture')
        _write_at(self.stage_fd, 'manifest.json', {'state': 'captured',
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        self.fd = os.open(source, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)
        proc = self.root / 'proc/self'
        proc.mkdir(parents=True)
        (proc / 'fdinfo').symlink_to('/proc/self/fdinfo', target_is_directory=True)
        info = os.fstat(self.stage_fd)
        self.mount_id = Path('/proc/self/fdinfo/' + str(self.stage_fd)).read_text().split('mnt_id:\t', 1)[1].splitlines()[0]
        (proc / 'mountinfo').write_text(f'{self.mount_id} 1 {os.major(info.st_dev)}:{os.minor(info.st_dev)} '
                                      '/ /drives/data rw - ext4 /dev/sda1 rw\n')
        self.reader = mock.Mock()
        self.reader.preflight.return_value.absent = False
        self.reader._verify_absent.return_value = True
        self.instance = recycle.NativeFileRecycle(self.root, reader=self.reader)
        self.current = True
        for name, value in (('_native_uuid', 'a' * 32), ('_listener_signature', NATIVE)):
            patcher = mock.patch.object(recycle, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.notify = mock.patch.object(self.instance, '_notify')
        self.notification = self.notify.start()
        self.addCleanup(self.notify.stop)

    def test_capture_same_inode_real_native_row_and_independent_completion(self):
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        receipt = self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                 current=lambda: self.current)
        self.assertFalse((self.stage_path / 'source').exists())
        archived = self.archive / receipt.record[6]
        self.assertEqual(archived.stat().st_ino, os.fstat(self.fd).st_ino)
        self.assertEqual(archived.read_bytes(), b'owner isolated fixture')
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute(recycle._SELECT, (receipt.record[6], recycle.OWNER_ID)).fetchall(),
                             [receipt.record])
        self.assertTrue(self.instance.verify(receipt, ROW, PATH, self.fd, current=lambda: self.current))
        self.notification.assert_called_once()
        self.reader._verify_absent.return_value = False
        self.assertFalse(self.instance.verify(receipt, ROW, PATH, self.fd, current=lambda: self.current))
        self.notification.assert_called_once()

    def test_database_insert_failure_keeps_real_archive_and_durable_recovery(self):
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        with mock.patch.object(recycle, '_INSERT', 'INVALID FIXTURE SQL'):
            with self.assertRaises(FileAccessError) as raised:
                self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                current=lambda: self.current)
            self.assertEqual(raised.exception.code, 'result_unconfirmed')
        manifest = recycle._read_at(self.stage_fd, 'manifest.json')
        self.assertEqual(manifest['state'], 'native_recycle_archived')
        archived = self.archive / manifest['native_recycle']['record'][6]
        self.assertEqual(archived.stat().st_ino, os.fstat(self.fd).st_ino)
        self.notification.assert_not_called()
        with mock.patch('api_file_native_index.NativeFileIndex') as bridge:
            bridge.return_value.verify_new.return_value = True
            result = self.instance.restore_manifest(ROW, manifest, self.stage, current=lambda: self.current)
        self.assertEqual(result['recovery']['state'], 'restored')
        self.assertEqual((self.users / PATH).stat().st_ino, os.fstat(self.fd).st_ino)

    def test_restore_no_clobber_and_only_its_exact_native_record_is_removed(self):
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        receipt = self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                 current=lambda: self.current)
        manifest = recycle._read_at(self.stage_fd, 'manifest.json')
        destination = self.users / PATH
        destination.write_bytes(b'existing owner file')
        with self.assertRaises(FileAccessError) as raised:
            self.instance.restore_manifest(ROW, manifest, self.stage, current=lambda: self.current)
        self.assertEqual(raised.exception.code, 'target_exists')
        self.assertEqual(destination.read_bytes(), b'existing owner file')
        self.assertTrue((self.archive / receipt.record[6]).exists())

    def test_pre_move_collision_recovers_only_staged_source_and_preserves_old_archive_row(self):
        deltime = 1700000000
        name = hashlib.md5(('/Users/' + PATH + str(deltime)).encode(), usedforsecurity=False).hexdigest() + '.bin'
        previous = self.archive / name
        previous.write_bytes(b'previous independent archive')
        foreign = ('a' * 32, '/Users/admin/older.bin', 'older.bin', 0, deltime,
                   recycle.OWNER_ID, name, previous.stat().st_size, deltime, deltime)
        with sqlite3.connect(self.db) as connection:
            connection.execute(recycle._INSERT, foreign)
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        with mock.patch.object(recycle.time, 'time', return_value=deltime):
            with self.assertRaises(FileAccessError) as raised:
                self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                current=lambda: self.current)
        self.assertEqual(raised.exception.code, 'target_exists')
        manifest = recycle._read_at(self.stage_fd, 'manifest.json')
        saved_db = self.db.with_name('fixture-offline-native-db')
        self.db.rename(saved_db)
        with mock.patch('api_file_native_index.NativeFileIndex') as bridge, \
                mock.patch.object(recycle, '_OpenedRecycle', side_effect=AssertionError('factory DB not needed')):
            bridge.return_value.verify_new.return_value = True
            result = self.instance.restore_manifest(ROW, manifest, self.stage, current=lambda: self.current)
        self.assertEqual(result['recovery']['state'], 'restored')
        self.assertEqual((self.users / PATH).stat().st_ino, os.fstat(self.fd).st_ino)
        self.assertEqual(previous.read_bytes(), b'previous independent archive')
        with sqlite3.connect(saved_db) as connection:
            self.assertEqual(connection.execute(recycle._SELECT, (name, recycle.OWNER_ID)).fetchall(), [foreign])

    def test_restore_rejects_old_writable_fd_byte_change_even_if_mtime_is_restored(self):
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        receipt = self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                 current=lambda: self.current)
        manifest = recycle._read_at(self.stage_fd, 'manifest.json')
        archive = self.archive / receipt.record[6]
        info = archive.stat()
        archive.write_bytes(b'x' * info.st_size)
        os.utime(archive, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(FileAccessError) as raised:
            self.instance.restore_manifest(ROW, manifest, self.stage, current=lambda: self.current)
        self.assertEqual(raised.exception.code, 'file_changed')
        self.assertFalse((self.users / PATH).exists())
        self.assertTrue(archive.exists())

    def test_restore_failure_after_rename_has_durable_unknown_receipt_and_never_replays(self):
        plan = self.instance.preflight(ROW, PATH, current=lambda: self.current)
        receipt = self.instance.archive_captured(plan, ROW, PATH, self.fd, self.stage,
                                                 current=lambda: self.current)
        manifest = recycle._read_at(self.stage_fd, 'manifest.json')
        with mock.patch.object(recycle, '_DELETE', 'INVALID FIXTURE SQL'):
            with self.assertRaises(FileAccessError) as raised:
                self.instance.restore_manifest(ROW, manifest, self.stage, current=lambda: self.current)
        self.assertEqual(raised.exception.code, 'result_unconfirmed')
        self.assertEqual((self.users / PATH).stat().st_ino, os.fstat(self.fd).st_ino)
        saved = recycle._read_at(self.stage_fd, 'manifest.json')
        self.assertEqual(saved['restore_receipt']['state'], 'result_unconfirmed')
        with mock.patch('api_file_mutations._rename_new') as rename:
            result = self.instance.restore_manifest(ROW, saved, self.stage, current=lambda: self.current)
            rename.assert_not_called()
        self.assertEqual(result['recovery']['state'], 'result_unconfirmed')
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute(recycle._SELECT, (receipt.record[6], recycle.OWNER_ID)).fetchall(),
                             [receipt.record])

    def test_untrusted_factory_directory_symlink_and_existing_hot_journal_reject_before_move(self):
        for mode in (0o777, 0o775):
            self.archive.chmod(mode)
            with self.assertRaises(FileAccessError):
                self.instance.preflight(ROW, PATH, current=lambda: self.current)
            self.assertTrue((self.stage_path / 'source').exists())
        self.archive.chmod(0o755)
        saved = self.db.with_name('saved-db')
        self.db.rename(saved)
        self.db.symlink_to(saved)
        with self.assertRaises(FileAccessError):
            self.instance.preflight(ROW, PATH, current=lambda: self.current)
        self.db.rename(self.db.with_name('fixture-db-symlink'))
        saved.rename(self.db)
        (self.db.parent / 'Recycle.db-journal').write_bytes(b'not recovered')
        with self.assertRaises(FileAccessError):
            self.instance.preflight(ROW, PATH, current=lambda: self.current)


if __name__ == '__main__':
    unittest.main()
