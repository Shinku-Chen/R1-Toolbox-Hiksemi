"""Exact native-index proof; real Linux fd/WAL cases are not NAS acceptance."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_native_index_reader as reader
from api_file_operations import FileAccessError


ROW = {'id': 'root_' + 'a' * 24, 'mount': '/drives/data/Users', 'read_only': False}
PATH = 'admin/uploads/中文.txt'
CREATE = ('CREATE TABLE tb_file_db_all(mkey INTEGER PRIMARY KEY AUTOINCREMENT, '
          'path VARCHAR(4096) NOT NULL UNIQUE, name VARCHAR(256), ctime int64, '
          'mtime int64, atime int64, isdir int64, size int64, deep int64, '
          'sname VARCHAR(4096), disk_mnt VARCHAR(4096), '
          + ', '.join('RESERVE%d VARCHAR(4096)' % i for i in range(1, 7)) + ')')


class SchemaAndBoundaryTests(unittest.TestCase):
    def database(self, schema=CREATE):
        connection = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute(schema)
        connection.set_authorizer(reader._schema_authorizer)
        return connection

    def error(self, code, callback):
        with self.assertRaises(FileAccessError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_exact_native_schema_and_unique_path_index_are_accepted(self):
        reader._schema(self.database())

    def test_view_trigger_generated_column_and_schema_changes_are_refused(self):
        variants = [CREATE.replace('ctime int64', 'ctime TEXT'),
                    CREATE.replace(' NOT NULL UNIQUE', ' NOT NULL'),
                    CREATE.replace('RESERVE6 VARCHAR(4096)', 'RESERVE6 VARCHAR(4096) DEFAULT 1'),
                    CREATE.replace('RESERVE6 VARCHAR(4096)', 'RESERVE6 VARCHAR(4096), hidden TEXT '
                                   'GENERATED ALWAYS AS (path) VIRTUAL')]
        for schema in variants:
            self.error('write_backend_unverified', lambda: reader._schema(self.database(schema)))
        connection = self.database()
        connection.set_authorizer(None)
        connection.execute('CREATE TRIGGER extra AFTER INSERT ON tb_file_db_all BEGIN SELECT 1; END')
        connection.set_authorizer(reader._schema_authorizer)
        self.error('write_backend_unverified', lambda: reader._schema(connection))
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE VIEW tb_file_db_all AS SELECT 1 AS path')
        connection.set_authorizer(reader._schema_authorizer)
        self.error('write_backend_unverified', lambda: reader._schema(connection))

    def test_reader_authorizer_cannot_read_tokens_other_columns_or_attach(self):
        connection = self.database()
        connection.set_authorizer(None)
        connection.execute('CREATE TABLE tb_user_auth(access_token TEXT)')
        connection.execute('INSERT INTO tb_user_auth VALUES(?)', ('never disclosed',))
        connection.set_authorizer(reader._data_authorizer)
        for sql in ('SELECT access_token FROM tb_user_auth', 'SELECT RESERVE1 FROM tb_file_db_all',
                    'SELECT upper(path) FROM tb_file_db_all', 'ATTACH DATABASE ":memory:" AS other',
                    'DELETE FROM tb_file_db_all', 'PRAGMA user_version=1'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                connection.execute(sql).fetchall()
        self.assertEqual(connection.execute(reader._SELECT, ('/Users/' + PATH,)).fetchall(), [])

    def test_schema_authorizer_only_accepts_fixed_metadata(self):
        connection = self.database()
        for sql in ('PRAGMA table_xinfo(tb_user_auth)', 'PRAGMA index_info(other)',
                    'PRAGMA journal_mode=WAL', 'SELECT sqlite_version()',
                    'CREATE TABLE extra(x)', 'SELECT path FROM tb_file_db_all'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                connection.execute(sql).fetchall()

    def test_only_fixed_admin_descendant_and_complete_untruncated_key_is_valid(self):
        self.assertEqual(reader._arguments(ROW, PATH), ('/drives/data', '/Users/' + PATH))
        for path in ('admin', 'public/new', 'another-user/private', '../admin/new',
                     'admin/.hidden', 'admin/folder/.nested', 'admin/a.ecloud.download',
                     'admin/a|b.txt',
                     'admin/' + '字' * 800):
            self.error('write_backend_unverified', lambda: reader._arguments(ROW, path))
        for mount in ('/drives/data', '/drives/data/Users/admin', '/other/data/Users'):
            self.error('write_backend_unverified',
                       lambda: reader._arguments(dict(ROW, mount=mount), PATH))

    def test_authorization_and_busy_precede_any_index_open(self):
        instance = reader.NativeFileIndexReader()
        with mock.patch.object(reader, '_OpenedIndex') as opened:
            self.error('invalid_key', lambda: instance.preflight(ROW, PATH, current=lambda: 1))
            self.assertFalse(opened.called)
            reader._LOCK.acquire()
            try:
                self.error('resource_busy', lambda: instance.preflight(ROW, PATH, current=lambda: True))
            finally:
                reader._LOCK.release()
            self.assertFalse(opened.called)

    def test_preflight_error_never_returns_an_upstream_filename(self):
        with mock.patch.object(reader, '_OpenedIndex', side_effect=RuntimeError('/secret/admin/token')):
            instance = reader.NativeFileIndexReader()
            self.error('write_backend_unverified',
                       lambda: instance.preflight(ROW, PATH, current=lambda: True))

    def test_other_reader_or_different_path_cannot_reuse_private_receipt(self):
        instance = reader.NativeFileIndexReader()
        receipt = reader._Receipt('different reader', '/drives/data', '/Users/' + PATH,
                                   ROW['id'], '1', (), (), True)
        self.error('write_backend_unverified', lambda: instance.verify_new(
            receipt, ROW, PATH, -1, directory=False, current=lambda: True))

    def test_unrelated_new_sqlite_fd_or_writable_sidecar_is_refused(self):
        opened = object.__new__(reader._OpenedIndex)
        opened.root, opened.mount_id = '/', '10'
        main, unexpected = (1, 2, stat.S_IFREG, 1), (1, 9, stat.S_IFREG, 1)
        opened.file_states = {'': main, '-wal': None, '-shm': None, '-journal': None}
        opened.check = lambda: None
        with mock.patch.object(reader, '_fd_snapshot', return_value={20: main, 21: unexpected}), \
                mock.patch.object(reader, '_readonly_fd', return_value=True), \
                mock.patch.object(reader, '_descriptor_mount_id', return_value='10'):
            self.error('write_backend_unverified', lambda: opened.sqlite_proof({}))
        with mock.patch.object(reader, '_fd_snapshot', return_value={20: main}), \
                mock.patch.object(reader, '_readonly_fd', return_value=False):
            self.error('write_backend_unverified', lambda: opened.sqlite_proof({}))


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH') and hasattr(os, 'pread'),
                     'Linux O_PATH, proc fd identity and Unix SQLite VFS required')
class NativeIndexLinuxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-native-index-reader-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'drives/data/Users'
        self.parent = self.users / 'admin/uploads'
        self.parent.mkdir(parents=True)
        self.cache = self.root / 'drives/data/.DATA_CACHE/Users/admin/file_db'
        self.cache.mkdir(parents=True)
        self.database = self.cache / 'FILE_DB_V102'
        connection = sqlite3.connect(self.database)
        connection.execute(CREATE)
        connection.commit()
        connection.close()
        proc = self.root / 'proc/self'
        proc.mkdir(parents=True)
        (proc / 'fdinfo').symlink_to('/proc/self/fdinfo', target_is_directory=True)
        descriptor = os.open(self.users, os.O_PATH | os.O_DIRECTORY)
        try:
            raw = Path('/proc/self/fdinfo/' + str(descriptor)).read_text()
            self.mount_id = raw.split('mnt_id:\t', 1)[1].splitlines()[0]
            info = os.fstat(descriptor)
            self.device = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
        finally:
            os.close(descriptor)
        (proc / 'mountinfo').write_text(
            f'{self.mount_id} 1 {self.device} / /drives/data rw - ext4 /dev/sda1 rw\n')
        self.current = True
        self.reader = reader.NativeFileIndexReader(self.root)

    def error(self, code, callback):
        with self.assertRaises(FileAccessError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def preflight(self, **kwargs):
        return self.reader.preflight(ROW, PATH, current=lambda: self.current, **kwargs)

    def file(self):
        path = self.users / PATH
        path.write_bytes(b'new native-index test')
        descriptor = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def values(self, descriptor):
        info = os.fstat(descriptor)
        return ('/Users/' + PATH, info.st_ctime_ns // 10 ** 9,
                info.st_mtime_ns // 10 ** 9, 0, info.st_size, '/drives/data')

    def insert(self, values):
        with sqlite3.connect(self.database) as connection:
            connection.execute('INSERT OR REPLACE INTO tb_file_db_all(path,ctime,mtime,isdir,size,disk_mnt) '
                               'VALUES(?,?,?,?,?,?)', values)

    def verify(self, receipt, descriptor):
        return self.reader.verify_new(receipt, ROW, PATH, descriptor, directory=False,
                                      current=lambda: self.current)

    def test_absent_then_real_indexed_metadata_is_required(self):
        receipt = self.preflight()
        descriptor = self.file()
        self.assertFalse(self.verify(receipt, descriptor))
        self.insert(self.values(descriptor))
        self.assertTrue(self.verify(receipt, descriptor))
        self.error('write_backend_unverified', self.preflight)
        observed = self.preflight(require_absent=False)
        self.assertTrue(self.verify(observed, descriptor))

    def test_stale_or_other_mount_or_noninteger_index_rows_never_confirm(self):
        receipt, descriptor = self.preflight(), self.file()
        expected = self.values(descriptor)
        for index, value in ((1, expected[1] - 1), (2, expected[2] - 1), (3, 1),
                             (4, expected[4] + 1), (5, '/drives/another')):
            row = list(expected)
            row[index] = value
            self.insert(row)
            self.assertFalse(self.verify(receipt, descriptor), row)
        self.insert(list(expected[:4]) + ['not a size', expected[5]])
        self.error('write_backend_unverified', lambda: self.verify(receipt, descriptor))

    def test_replaced_index_database_and_wrong_submitted_fd_are_refused(self):
        receipt, descriptor = self.preflight(), self.file()
        self.insert(self.values(descriptor))
        other = self.parent / 'other.txt'
        other.write_bytes(b'new native-index test')
        other_fd = os.open(other, os.O_RDONLY)
        self.addCleanup(os.close, other_fd)
        self.error('file_changed', lambda: self.verify(receipt, other_fd))
        replacement = self.cache / 'fixture-replacement'
        shutil.copyfile(self.database, replacement)
        os.replace(replacement, self.database)
        self.error('write_backend_unverified', lambda: self.verify(receipt, descriptor))

    def test_symlink_hardlink_and_hot_journal_are_refused_before_sqlite(self):
        hardlink = self.cache / 'fixture-hardlink'
        os.link(self.database, hardlink)
        self.error('write_backend_unverified', self.preflight)
        hardlink.unlink()
        saved = self.cache / 'fixture-saved'
        self.database.rename(saved)
        self.database.symlink_to(saved)
        self.error('write_backend_unverified', self.preflight)
        self.database.unlink()
        saved.rename(self.database)
        (self.cache / 'FILE_DB_V102-journal').write_bytes(b'not recovered')
        self.error('write_backend_unverified', self.preflight)

    def test_reader_never_creates_missing_database_or_schema(self):
        self.database.rename(self.cache / 'fixture-saved')
        self.error('write_backend_unverified', self.preflight)
        self.assertFalse(self.database.exists())
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE other(x)')
        self.error('write_backend_unverified', self.preflight)
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()),
                         ['FILE_DB_V102', 'fixture-saved'])

    def test_revocation_after_query_discards_rows_and_replaced_file_is_refused(self):
        receipt, descriptor = self.preflight(), self.file()
        self.insert(self.values(descriptor))
        original = reader._query

        def revoke(*args):
            rows = original(*args)
            self.current = False
            return rows

        with mock.patch.object(reader, '_query', side_effect=revoke):
            self.error('invalid_key', lambda: self.verify(receipt, descriptor))
        self.current = True

        def change(*args):
            rows = original(*args)
            replacement = self.parent / 'fixture-new'
            replacement.write_bytes(b'replacement')
            os.replace(replacement, self.users / PATH)
            return rows

        with mock.patch.object(reader, '_query', side_effect=change):
            self.error('file_changed', lambda: self.verify(receipt, descriptor))

    def test_database_actual_sqlite_fd_proof_is_mandatory(self):
        with mock.patch.object(reader._OpenedIndex, 'sqlite_proof',
                               side_effect=FileAccessError('write_backend_unverified')):
            self.error('write_backend_unverified', self.preflight)

    def wal_writer(self):
        # A distinct process models the native daemon; a same-process SQLite
        # connection could reuse its writable unixShmNode and invalidate proof.
        script = ('import sqlite3,sys,json\n'
                  'c=sqlite3.connect(sys.argv[1]);c.execute("PRAGMA journal_mode=WAL");'
                  'c.execute("PRAGMA wal_autocheckpoint=0");c.execute("UPDATE tb_file_db_all SET size=1");'
                  'c.commit();print("ready",flush=True)\n'
                  'for line in sys.stdin:\n'
                  ' value=json.loads(line)\n'
                  ' if value is None: break\n'
                  ' c.execute("INSERT OR REPLACE INTO tb_file_db_all'
                  '(path,ctime,mtime,isdir,size,disk_mnt) VALUES(?,?,?,?,?,?)",value);'
                  'c.commit();print("inserted",flush=True)\n'
                  'c.close()\n')
        process = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def close():
            if process.poll() is None:
                process.communicate('null\n', timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(close)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        # Ensure a real committed WAL frame exists even when the fixture table
        # is empty: the writer's first INSERT is deliberately another key.
        process.stdin.write(json.dumps(['/Users/admin/unselected', 1, 1, 0, 1, '/drives/data']) + '\n')
        process.stdin.flush()
        self.assertEqual(process.stdout.readline().strip(), 'inserted')
        return process

    def test_live_wal_visibility_without_shm_creation_or_content_writes(self):
        writer = self.wal_writer()
        receipt, descriptor = self.preflight(), self.file()
        writer.stdin.write(json.dumps(self.values(descriptor)) + '\n')
        writer.stdin.flush()
        self.assertEqual(writer.stdout.readline().strip(), 'inserted')
        before = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in self.cache.iterdir()}
        self.assertTrue(self.verify(receipt, descriptor))
        after = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in self.cache.iterdir()}
        self.assertEqual(before, after)
        immutable = sqlite3.connect(self.database.as_uri() + '?mode=ro&immutable=1', uri=True)
        self.addCleanup(immutable.close)
        self.assertEqual(immutable.execute('SELECT path FROM tb_file_db_all WHERE path=?',
                                          ('/Users/' + PATH,)).fetchall(), [])

    def test_missing_live_shm_is_rejected_without_recreating_it(self):
        self.wal_writer()
        shm = self.cache / 'FILE_DB_V102-shm'
        saved = self.cache / 'fixture-shm'
        shm.rename(saved)
        try:
            self.error('write_backend_unverified', self.preflight)
            self.assertFalse(shm.exists())
        finally:
            saved.rename(shm)


if __name__ == '__main__':
    unittest.main()
