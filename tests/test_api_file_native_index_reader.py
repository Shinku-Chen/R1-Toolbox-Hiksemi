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


ROW = {'id': 'root_' + 'a' * 24, 'mount': '/drives/data/Users/admin', 'read_only': False}
PATH = 'uploads/中文.txt'
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
        self.assertEqual(connection.execute(reader._SELECT, ('/Users/admin/' + PATH,)).fetchall(), [])

    def test_schema_authorizer_only_accepts_fixed_metadata(self):
        connection = self.database()
        for sql in ('PRAGMA table_xinfo(tb_user_auth)', 'PRAGMA index_info(other)',
                    'PRAGMA journal_mode=WAL', 'SELECT sqlite_version()',
                    'CREATE TABLE extra(x)', 'SELECT path FROM tb_file_db_all'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                connection.execute(sql).fetchall()

    def test_only_fixed_admin_descendant_and_complete_untruncated_key_is_valid(self):
        self.assertEqual(reader._arguments(ROW, PATH), ('/drives/data', '/Users/admin/' + PATH))
        for path in ('', '/public/new', '../new', '.hidden', 'folder/.nested',
                     'a.ecloud.download', 'a|b.txt', '字' * 800):
            self.error('write_backend_unverified', lambda: reader._arguments(ROW, path))
        for mount in ('/drives/data', '/drives/data/Users', '/drives/data/Users/public', '/other/data/Users/admin'):
            self.error('write_backend_unverified',
                       lambda: reader._arguments(dict(ROW, mount=mount), PATH))
        for path in ('public/new', 'another-user/private', 'admin/new'):
            self.assertEqual(reader._arguments(ROW, path), ('/drives/data', '/Users/admin/' + path))

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
        receipt = reader._Receipt('different reader', '/drives/data', '/Users/admin/' + PATH,
                                   ROW['id'], '1', (), (), True)
        self.error('write_backend_unverified', lambda: instance.verify_new(
            receipt, ROW, PATH, -1, directory=False, current=lambda: True))

    def test_old_root_receipt_is_not_rebound_to_new_admin_root(self):
        instance = reader.NativeFileIndexReader()
        receipt = reader._Receipt(instance._identity, '/drives/data', '/Users/admin/' + PATH,
                                  'root_' + 'b' * 24, '1', (), (), True)
        with mock.patch.object(reader, '_OpenedIndex', side_effect=AssertionError('no database open')):
            self.error('write_backend_unverified', lambda: instance.verify_new(
                receipt, ROW, PATH, -1, directory=False, current=lambda: True))

    def test_mutation_preflight_requires_an_existing_generation_and_absent_target(self):
        instance = reader.NativeFileIndexReader()
        source = reader._Receipt(instance._identity, '/drives/data', '/Users/admin/' + PATH,
                                  ROW['id'], '1', (), (1, 2), False, 7)
        target = reader._Receipt(instance._identity, '/drives/data', '/Users/admin/target',
                                  ROW['id'], '1', (), (1, 2), True, None)
        with mock.patch.object(instance, 'preflight', side_effect=[source, target]) as preflight:
            result = instance.preflight_mutation(ROW, PATH, 'target',
                                                  operation='move', current=lambda: True)
            self.assertIs(result.source, source)
            self.assertIs(result.target, target)
            self.assertFalse(preflight.call_args_list[0].kwargs['require_absent'])
            self.assertTrue(preflight.call_args_list[1].kwargs['require_absent'])
        with mock.patch.object(instance, 'preflight', return_value=target):
            self.error('write_backend_unverified', lambda: instance.preflight_mutation(
                ROW, PATH, 'target', operation='move', current=lambda: True))

    def test_mutation_requires_both_target_generation_and_source_disappearance(self):
        instance = reader.NativeFileIndexReader()
        source = reader._Receipt(instance._identity, '/drives/data', '/Users/admin/' + PATH,
                                  ROW['id'], '1', (), (1, 2), False, 7)
        target = reader._Receipt(instance._identity, '/drives/data', '/Users/admin/target',
                                  ROW['id'], '1', (), (1, 2), True, None)
        receipt = reader._MutationReceipt('move', source, target)
        current = lambda: True
        with mock.patch.object(instance, 'verify_new', return_value=True) as verify, \
                mock.patch.object(instance, '_verify_absent', return_value=False) as absent:
            self.assertFalse(instance.verify_mutation(receipt, ROW, PATH, 'target', 1,
                                                      operation='move', current=current))
            self.assertEqual(verify.call_args.kwargs['after_generation'], 7)
            absent.assert_called_once()
            absent.return_value = True
            self.assertTrue(instance.verify_mutation(receipt, ROW, PATH, 'target', 1,
                                                     operation='move', current=current))
            verify.return_value = False
            self.assertFalse(instance.verify_mutation(receipt, ROW, PATH, 'target', 1,
                                                      operation='move', current=current))

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

    def test_only_exact_wal_may_use_sqlite_internal_readwrite_descriptor(self):
        opened = object.__new__(reader._OpenedIndex)
        opened.root, opened.mount_id, opened.wal = '/', '10', True
        main, wal, shm = [(1, i, stat.S_IFREG, 1) for i in (2, 3, 4)]
        opened.file_states = {'': main, '-wal': wal, '-shm': shm, '-journal': None}
        opened.check = lambda: None
        with mock.patch.object(reader, '_fd_snapshot', return_value={20: main, 21: wal, 22: shm}), \
                mock.patch.object(reader, '_readonly_fd', side_effect=lambda fd: fd != 21), \
                mock.patch.object(reader, '_wal_fd_allowed', return_value=True), \
                mock.patch.object(reader, '_descriptor_mount_id', return_value='10'):
            opened.sqlite_proof({}, sidecars=True)
            with mock.patch.object(reader, '_readonly_fd', side_effect=lambda fd: fd == 20):
                self.error('write_backend_unverified', lambda: opened.sqlite_proof({}, sidecars=True))
            with mock.patch.object(reader, '_wal_fd_allowed', return_value=False):
                self.error('write_backend_unverified', lambda: opened.sqlite_proof({}, sidecars=True))


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH') and hasattr(os, 'pread'),
                     'Linux O_PATH, proc fd identity and Unix SQLite VFS required')
class NativeIndexLinuxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-native-index-reader-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'drives/data/Users'
        self.admin = self.users / 'admin'
        self.parent = self.admin / 'uploads'
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

    def test_observer_covers_real_cache_chain_sqlite_fds_and_exact_query_without_data(self):
        events = []
        self.reader = reader.NativeFileIndexReader(self.root,
            observer=lambda stage, state: events.append((stage, state)))
        before = hashlib.sha256(self.database.read_bytes()).digest()
        self.assertTrue(self.preflight().absent)
        required = (*reader._CACHE_STAGES, *reader._SIDECAR_STAGES.values(),
                    'reader.sqlite.main_fd', 'reader.sqlite.sidecars_fd',
                    'reader.schema.tables', 'reader.schema.columns', 'reader.schema.index',
                    'reader.sqlite.exact_query', 'reader.sqlite.final_fd', 'reader.final_check')
        for stage in required:
            self.assertIn((stage, 'pass'), events)
        self.assertTrue(all(stage in reader.PREFLIGHT_STAGES and state in ('start', 'pass')
                            for stage, state in events))
        self.assertNotIn(PATH, repr(events))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())

    def test_missing_fixed_cache_component_stops_at_exact_stage_before_sqlite(self):
        events = []
        self.reader = reader.NativeFileIndexReader(self.root,
            observer=lambda stage, state: events.append((stage, state)))
        physical = self.root / 'drives/data'
        for count, stage in enumerate(reader._CACHE_STAGES, 1):
            with self.subTest(stage=stage):
                path = physical.joinpath(*reader._CACHE[:count])
                saved = path.with_name(path.name + '-saved')
                path.rename(saved)
                try:
                    events.clear()
                    with mock.patch.object(reader.sqlite3, 'connect',
                                           side_effect=AssertionError('must not open SQLite')) as connect:
                        self.error('write_backend_unverified', self.preflight)
                    connect.assert_not_called()
                    self.assertEqual(events[-1], (stage, 'start'))
                finally:
                    saved.rename(path)

    def test_hot_journal_and_wal_header_failures_remain_unpassed_without_sqlite_open(self):
        events = []
        self.reader = reader.NativeFileIndexReader(self.root,
            observer=lambda stage, state: events.append((stage, state)))
        journal = self.database.with_name(self.database.name + '-journal')
        journal.write_bytes(b'not safe to recover')
        try:
            with mock.patch.object(reader.sqlite3, 'connect') as connect:
                self.error('write_backend_unverified', self.preflight)
            connect.assert_not_called()
            self.assertEqual(events[-1], ('reader.sidecar.journal', 'start'))
        finally:
            journal.unlink()
        before = self.database.read_bytes()
        damaged = bytearray(before)
        damaged[18:20] = b'\x02\x02'
        self.database.write_bytes(damaged)
        try:
            events.clear()
            with mock.patch.object(reader.sqlite3, 'connect') as connect:
                self.error('write_backend_unverified', self.preflight)
            connect.assert_not_called()
            self.assertEqual(events[-1], ('reader.wal_header', 'start'))
        finally:
            self.database.write_bytes(before)

    def test_raising_observer_does_not_change_real_descriptor_or_index_proof(self):
        def broken(*unused):
            raise RuntimeError('observer is not an authorization gate')
        self.reader = reader.NativeFileIndexReader(self.root, observer=broken)
        receipt = self.preflight()
        descriptor = self.file()
        self.insert(self.values(descriptor))
        self.assertTrue(self.verify(receipt, descriptor))

    def file(self):
        path = self.admin / PATH
        path.write_bytes(b'new native-index test')
        descriptor = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def values(self, descriptor):
        info = os.fstat(descriptor)
        return ('/Users/admin/' + PATH, info.st_ctime_ns // 10 ** 9,
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

    def test_mkdir_requires_real_directory_metadata_and_native_index_row(self):
        receipt = self.preflight()
        path = self.admin / PATH
        path.mkdir()
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        self.assertFalse(self.reader.verify_new(receipt, ROW, PATH, descriptor,
                                               directory=True, current=lambda: True))
        values = list(self.values(descriptor))
        values[3] = 1
        self.insert(values)
        self.assertTrue(self.reader.verify_new(receipt, ROW, PATH, descriptor,
                                              directory=True, current=lambda: True))

    def test_replace_must_advance_native_generation_even_with_identical_stat_tuple(self):
        descriptor = self.file()
        self.insert(self.values(descriptor))
        prepared = self.reader.preflight_mutation(ROW, PATH, PATH,
                                                   operation='replace', current=lambda: True)
        self.assertFalse(self.reader.verify_mutation(prepared, ROW, PATH, PATH, descriptor,
                                                    operation='replace', current=lambda: True))
        self.insert(self.values(descriptor))
        self.assertTrue(self.reader.verify_mutation(prepared, ROW, PATH, PATH, descriptor,
                                                   operation='replace', current=lambda: True))

    def test_move_needs_new_generation_and_no_old_key_in_same_database(self):
        descriptor = self.file()
        self.insert(self.values(descriptor))
        target = 'uploads/moved.txt'
        prepared = self.reader.preflight_mutation(ROW, PATH, target,
                                                   operation='move', current=lambda: True)
        os.rename(self.admin / PATH, self.admin / target)
        info = os.fstat(descriptor)
        values = ('/Users/admin/' + target, info.st_ctime_ns // 10**9,
                  info.st_mtime_ns // 10**9, 0, info.st_size, '/drives/data')
        self.insert(values)
        self.assertFalse(self.reader.verify_mutation(prepared, ROW, PATH, target, descriptor,
                                                    operation='move', current=lambda: True))
        with sqlite3.connect(self.database) as connection:
            connection.execute('DELETE FROM tb_file_db_all WHERE path=?', ('/Users/admin/' + PATH,))
            connection.execute('UPDATE tb_file_db_all SET mkey=? WHERE path=?',
                               (prepared.source.generation, '/Users/admin/' + target))
        self.assertFalse(self.reader.verify_mutation(prepared, ROW, PATH, target, descriptor,
                                                    operation='move', current=lambda: True))
        self.insert(values)
        self.assertTrue(self.reader.verify_mutation(prepared, ROW, PATH, target, descriptor,
                                                   operation='move', current=lambda: True))

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

        def revoke(*args, **kwargs):
            rows = original(*args, **kwargs)
            self.current = False
            return rows

        with mock.patch.object(reader, '_query', side_effect=revoke):
            self.error('invalid_key', lambda: self.verify(receipt, descriptor))
        self.current = True

        def change(*args, **kwargs):
            rows = original(*args, **kwargs)
            replacement = self.parent / 'fixture-new'
            replacement.write_bytes(b'replacement')
            os.replace(replacement, self.admin / PATH)
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
                                          ('/Users/admin/' + PATH,)).fetchall(), [])

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

    def test_wal_rotation_race_never_confirms_or_writes_database_content(self):
        self.wal_writer()
        wal = self.cache / 'FILE_DB_V102-wal'
        saved = self.cache / 'fixture-rotated-wal'
        before = {p.name: hashlib.sha256(p.read_bytes()).digest()
                  for p in (self.database, wal)}
        rotated = []

        class RotatingConnection(sqlite3.Connection):
            def execute(connection, sql, *args):
                if sql == 'PRAGMA schema_version' and not rotated:
                    wal.rename(saved)
                    rotated.append(True)
                return super().execute(sql, *args)

        connect = sqlite3.connect
        with mock.patch.object(reader.sqlite3, 'connect',
                               side_effect=lambda *a, **kw: connect(*a, factory=RotatingConnection, **kw)):
            self.error('write_backend_unverified', self.preflight)
        self.assertTrue(rotated)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before[self.database.name])
        self.assertEqual(hashlib.sha256(saved.read_bytes()).digest(), before[wal.name])
        # SQLite's internal READWRITE|CREATE open can create an empty new WAL
        # after the precheck. Identity failure must reject it, not claim a row.
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
