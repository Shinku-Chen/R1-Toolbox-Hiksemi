"""Fixed diagnostic markers observe production gates without granting writes."""

from contextlib import ExitStack
import ast
import errno
import itertools
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_native_index as index
import api_file_native_index_reader as reader
from api_file_operations import FileAccessError


ROW = {'id': 'root_' + 'a' * 24, 'mount': '/drives/data/Users/admin'}
PATH = 'parent/secret-user-name.txt'
NATIVE = ((123, 40, '99', '127.0.0.1'), '100', (1, 2), (3, 4))
CREATE = ('CREATE TABLE tb_file_db_all(mkey INTEGER PRIMARY KEY AUTOINCREMENT, '
          'path VARCHAR(4096) NOT NULL UNIQUE, name VARCHAR(256), ctime int64, '
          'mtime int64, atime int64, isdir int64, size int64, deep int64, '
          'sname VARCHAR(4096), disk_mnt VARCHAR(4096), '
          + ', '.join('RESERVE%d VARCHAR(4096)' % i for i in range(1, 7)) + ')')


class FixedObserverTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.observer = lambda stage, state: self.events.append((stage, state))

    def assert_fixed(self, allowed):
        self.assertTrue(self.events)
        self.assertTrue(all(stage in allowed and state in ('start', 'pass')
                            for stage, state in self.events))
        self.assertNotIn('secret', repr(self.events))
        self.assertEqual(len(allowed), len(set(allowed)))

    def test_all_stage_literals_belong_to_unique_exported_vocabularies(self):
        for module, prefix in ((index, 'index.'), (reader, 'reader.')):
            with self.subTest(module=module.__name__):
                source = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
                literals = {node.value for node in ast.walk(source)
                            if isinstance(node, ast.Constant) and type(node.value) is str
                            and node.value.startswith(prefix)}
                self.assertEqual(literals, set(module.PREFLIGHT_STAGES))
                self.assertEqual(len(module.PREFLIGHT_STAGES), len(literals))

    def bridge(self, **kwargs):
        backend = mock.Mock()
        backend.preflight.return_value = 'private-receipt'
        values = dict(reader=backend, disk_check=lambda row: True,
                      listener_check=lambda deadline: NATIVE, observer=self.observer)
        values.update(kwargs)
        return index.NativeFileIndex(**values), backend

    def test_bridge_success_emits_only_fixed_markers_and_never_connects(self):
        connection = mock.Mock(side_effect=AssertionError('no notification'))
        bridge, backend = self.bridge(socket_factory=connection)
        self.assertIsNone(bridge.preflight(ROW, PATH, current=lambda: True))
        self.assertEqual(self.events[0], ('index.authorization', 'start'))
        self.assertEqual(self.events[-1], ('index.final_current', 'pass'))
        backend.preflight.assert_called_once_with(ROW, PATH, current=mock.ANY,
                                                  require_absent=True)
        connection.assert_not_called()
        self.assert_fixed(index.PREFLIGHT_STAGES)

    def test_failed_gate_has_no_pass_and_stops_dependent_work(self):
        for gate, code in (('authorization', 'invalid_key'), ('path', 'unsupported_path_class'),
                           ('sata', 'write_backend_unverified'), ('listener', 'native_unavailable'),
                           ('reader', 'write_backend_unverified')):
            with self.subTest(gate=gate):
                self.events.clear()
                listener = mock.Mock(return_value=NATIVE)
                disk = mock.Mock(return_value=gate != 'sata')
                bridge, backend = self.bridge(disk_check=disk, listener_check=listener)
                if gate == 'listener':
                    listener.side_effect = FileAccessError(code)
                if gate == 'reader':
                    backend.preflight.side_effect = FileAccessError(code)
                with self.assertRaises(FileAccessError) as error:
                    bridge.preflight(ROW, '' if gate == 'path' else PATH,
                                     current=lambda: gate != 'authorization')
                self.assertEqual(error.exception.code, code)
                self.assertEqual(self.events[-1], ('index.' + gate, 'start'))
                if gate in ('authorization', 'path', 'sata'):
                    listener.assert_not_called()
                if gate != 'reader':
                    backend.preflight.assert_not_called()
                self.assert_fixed(index.PREFLIGHT_STAGES)

    def test_observer_exception_preserves_success_original_error_and_timeout(self):
        def broken(*unused):
            raise RuntimeError('private path or Token must never become output')
        for observer in (None, broken):
            with self.subTest(observer=observer):
                bridge, backend = self.bridge(observer=observer)
                self.assertIsNone(bridge.preflight(ROW, PATH, current=lambda: True))
                backend.preflight.side_effect = FileAccessError('file_changed')
                with self.assertRaises(FileAccessError) as error:
                    bridge.preflight(ROW, PATH, current=lambda: True)
                self.assertEqual(error.exception.code, 'file_changed')
                clock = iter((0, 9)).__next__
                bridge, backend = self.bridge(observer=observer, clock=clock)
                with self.assertRaises(FileAccessError) as error:
                    bridge.preflight(ROW, PATH, current=lambda: True)
                self.assertEqual(error.exception.code, 'timeout')

    def test_default_reader_receives_observer_but_trusted_seams_keep_old_signature(self):
        with mock.patch.object(reader, 'NativeFileIndexReader') as factory:
            bridge = index.NativeFileIndex('/fixed-root', observer=self.observer)
            self.assertIs(bridge._backend(), factory.return_value)
            factory.assert_called_once_with('/fixed-root', observer=self.observer)

    def sata(self, *, source='/dev/sda1', root='/', dev='8:1', read_error=False,
             node_dev=None, transport='pci0000/ata1/host0'):
        base = Path('observer-fixture').absolute()
        line = f'605 1 8:1 {root} /drives/data rw - ext4 {source} rw\n'
        def read(_root, path, *unused):
            if path == 'proc/self/mountinfo':
                return line
            if read_error:
                raise OSError(errno.EACCES, 'secret upstream path')
            return dev
        node = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600,
                               st_rdev=(8, 1) if node_dev is None else node_dev)
        with mock.patch.object(index, '_read_fixed', side_effect=read), \
                mock.patch.object(index, '_read_sysfs_partition_dev',
                    side_effect=lambda root, disk, partition: read(
                        root, 'sys/block/' + disk + '/' + partition + '/dev')), \
                mock.patch.object(index.os, 'stat', return_value=node), \
                mock.patch.object(index.os, 'major', side_effect=lambda value: value[0], create=True), \
                mock.patch.object(index.os, 'minor', side_effect=lambda value: value[1], create=True), \
                mock.patch.object(index.Path, 'resolve', return_value=base / 'sys/devices' / transport):
            return index._internal_sata(base, ROW, observer=self.observer)

    def test_sata_subgates_have_exact_failure_stage_without_metadata_payload(self):
        for kwargs, stage in (({'root': '/sub'}, 'canonical_mount'),
                              ({'source': '/dev/sda'}, 'source'),
                              ({'read_error': True}, 'sysfs_device'),
                              ({'dev': '8:2'}, 'device_match'),
                              ({'transport': 'virtual/ata1'}, 'transport')):
            with self.subTest(stage=stage):
                self.events.clear()
                self.assertFalse(self.sata(**kwargs))
                self.assertEqual(self.events[-1], ('index.sata.' + stage, 'start'))
                self.assert_fixed(index.PREFLIGHT_STAGES)
        self.events.clear()
        self.assertTrue(self.sata())
        self.assertEqual(self.events[-1], ('index.sata.transport', 'pass'))

    def listener(self, *, binary=True, table=None, owned=True, socket_uid=0):
        identity = (123, 40, '99', '127.0.0.1')
        namespace = SimpleNamespace(st_dev=1, st_ino=2)
        node = SimpleNamespace(st_dev=1, st_ino=3, st_mode=stat.S_IFSOCK | 0o600,
            st_uid=socket_uid, st_gid=0, st_mtime_ns=4, st_ctime_ns=5)
        text = ('Num RefCount Protocol Flags Type St Inode Path\n'
                '0: 2 0 00010000 0001 01 100 /tmp/UNIX.domain\n')
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(index, 'require_emhistor_listener'))
            stack.enter_context(mock.patch.object(index, '_service_identity', return_value=identity))
            stack.enter_context(mock.patch.object(index, '_runtime_binary_matches', return_value=binary))
            stack.enter_context(mock.patch.object(index.os, 'stat', return_value=namespace))
            stack.enter_context(mock.patch.object(index.Path, 'lstat', return_value=node))
            stack.enter_context(mock.patch.object(index, '_read_fixed', return_value=text if table is None else table))
            scan = stack.enter_context(mock.patch.object(index, '_socket_inodes',
                return_value=({'100'} if owned else set(), True)))
            result = index._listener_signature('.', time.monotonic() + 8,
                                               observer=self.observer)
            self.assertTrue(scan.called)
            return result

    def test_listener_subgates_preserve_fixed_identity_rejections(self):
        for kwargs, stage in (({'binary': False}, 'binary'), ({'socket_uid': 1046}, 'socket_path'),
                              ({'table': 'header\n'}, 'unix_table'), ({'owned': False}, 'socket_owner')):
            with self.subTest(stage=stage):
                self.events.clear()
                with self.assertRaises(FileAccessError) as error:
                    self.listener(**kwargs)
                self.assertEqual(error.exception.code, 'native_unavailable')
                self.assertEqual(self.events[-1], ('index.listener.' + stage, 'start'))
                self.assert_fixed(index.PREFLIGHT_STAGES)
        self.events.clear()
        self.assertEqual(self.listener()[1], '100')
        self.assertEqual(self.events[-1], ('index.listener.socket_stable', 'pass'))

    def test_listener_allows_only_normal_pending_and_accepted_stream_children(self):
        header = 'Num RefCount Protocol Flags Type St Inode Path\n'
        listening = '0: 2 0 00010000 0001 01 100 /tmp/UNIX.domain\n'
        pending = '1: 3 0 00000000 0001 02 0 /tmp/UNIX.domain\n'
        accepted = '2: 3 0 00000000 0001 03 200 /tmp/UNIX.domain\n'
        arrangements = list(itertools.permutations((listening, pending, accepted)))
        arrangements += [(listening, child) for child in (pending, accepted)]
        arrangements += [(child, listening) for child in (pending, accepted)]
        for rows in arrangements:
            with self.subTest(rows=rows):
                self.events.clear()
                self.assertEqual(self.listener(table=header + ''.join(rows))[1], '100')
                self.assertEqual(self.events[-1], ('index.listener.socket_stable', 'pass'))

    def test_stream_children_never_replace_or_hide_duplicate_listeners(self):
        header = 'Num RefCount Protocol Flags Type St Inode Path\n'
        listening = '0: 2 0 00010000 0001 01 100 /tmp/UNIX.domain\n'
        pending = '1: 3 0 00000000 0001 02 0 /tmp/UNIX.domain\n'
        accepted = '2: 3 0 00000000 0001 03 200 /tmp/UNIX.domain\n'
        for rows in (pending, accepted, pending + accepted,
                     listening + pending + accepted + listening):
            with self.subTest(rows=rows):
                self.events.clear()
                with self.assertRaises(FileAccessError) as error:
                    self.listener(table=header + rows)
                self.assertEqual(error.exception.code, 'native_unavailable')
                self.assertEqual(self.events[-1], ('index.listener.unix_table', 'start'))

    def test_same_path_nonlistener_malformed_or_unconfirmed_states_are_refused(self):
        header = ('Num RefCount Protocol Flags Type St Inode Path\n'
                  '0: 2 0 00010000 0001 01 100 /tmp/UNIX.domain\n')
        # Only kernel pending inode 0 and accepted positive inode are admitted.
        # Other transient states remain unconfirmed, not listener candidates.
        for flags, kind, state, inode in (
                ('00000000', '0001', '02', '200'),
                ('00000000', '0001', '03', '0'),
                ('00000000', '0001', '03', '-1'),
                ('00000000', '0001', '03', '12x'),
                ('00000000', '0001', '03', '1' * 25),
                ('00000000', '0001', '04', '200'),
                ('00000000', '0002', '03', '200'),
                ('00010000', '0001', '03', '200'),
                ('00020000', '0001', '03', '200'),
                ('00010000', '0001', '01', '0'),
                ('00010000', '0001', '01', '12x'),
                ('00010000', '0002', '01', '200')):
            with self.subTest(flags=flags, kind=kind, state=state, inode=inode):
                self.events.clear()
                row = f'1: 3 0 {flags} {kind} {state} {inode} /tmp/UNIX.domain\n'
                with self.assertRaises(FileAccessError) as error:
                    self.listener(table=header + row)
                self.assertEqual(error.exception.code, 'native_unavailable')
                self.assertEqual(self.events[-1], ('index.listener.unix_table', 'start'))

    def connection(self, create=CREATE):
        connection = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute(create)
        return connection

    def test_schema_stages_identify_tables_columns_and_index_without_schema_output(self):
        for create, stage in (('CREATE VIEW tb_file_db_all AS SELECT 1 AS path', 'tables'),
                              (CREATE.replace('ctime int64', 'ctime TEXT'), 'columns'),
                              (CREATE.replace(' NOT NULL UNIQUE', ' NOT NULL'), 'index')):
            with self.subTest(stage=stage):
                self.events.clear()
                connection = self.connection(create)
                connection.set_authorizer(reader._schema_authorizer)
                with self.assertRaises(FileAccessError) as error:
                    reader._schema(connection, observer=self.observer)
                self.assertEqual(error.exception.code, 'write_backend_unverified')
                self.assertEqual(self.events[-1], ('reader.schema.' + stage, 'start'))
                self.assert_fixed(reader.PREFLIGHT_STAGES)

    def query(self, *, proof=None, create=CREATE, connect_error=None):
        connection = self.connection(create)
        opened = SimpleNamespace(_observer=self.observer, directory=7,
            deadline=time.monotonic() + 10, current=lambda: True, wal=False,
            sqlite_proof=mock.Mock(side_effect=proof))
        with mock.patch.object(reader, '_fd_snapshot', return_value={}), \
                mock.patch.object(reader.sqlite3, 'connect', return_value=connection,
                                  side_effect=connect_error):
            return reader._query(opened, '/Users/admin/' + PATH, versioned=True)

    def test_query_success_and_open_fd_schema_failures_preserve_stop_stage(self):
        self.assertEqual(self.query(), [])
        self.assertEqual(self.events[-1], ('reader.sqlite.final_fd', 'pass'))
        self.assert_fixed(reader.PREFLIGHT_STAGES)
        for kwargs, stage, error in (({'connect_error': sqlite3.OperationalError('secret db path')},
                                      'reader.sqlite.open', sqlite3.OperationalError),
                                     ({'proof': FileAccessError('write_backend_unverified')},
                                      'reader.sqlite.main_fd', FileAccessError),
                                     ({'proof': [None, FileAccessError('write_backend_unverified')]},
                                      'reader.sqlite.sidecars_fd', FileAccessError),
                                     ({'proof': [None, None, FileAccessError('write_backend_unverified')]},
                                      'reader.sqlite.final_fd', FileAccessError),
                                     ({'create': CREATE.replace('ctime int64', 'ctime TEXT')},
                                      'reader.schema.columns', FileAccessError)):
            with self.subTest(stage=stage):
                self.events.clear()
                with self.assertRaises(error):
                    self.query(**kwargs)
                self.assertEqual(self.events[-1], (stage, 'start'))
                self.assert_fixed(reader.PREFLIGHT_STAGES)

    def test_reader_authorization_busy_and_stale_row_do_not_gain_later_pass(self):
        instance = reader.NativeFileIndexReader(observer=self.observer)
        with self.assertRaises(FileAccessError) as error:
            instance.preflight(ROW, PATH, current=lambda: False)
        self.assertEqual(error.exception.code, 'invalid_key')
        self.assertEqual(self.events[-1], ('reader.authorization', 'start'))
        self.events.clear()
        reader._LOCK.acquire()
        try:
            with self.assertRaises(FileAccessError) as error:
                instance.preflight(ROW, PATH, current=lambda: True)
        finally:
            reader._LOCK.release()
        self.assertEqual(error.exception.code, 'resource_busy')
        self.assertEqual(self.events[-1], ('reader.lock', 'start'))
        self.events.clear()
        with mock.patch.object(reader, '_OpenedIndex'), \
                mock.patch.object(reader, '_query', return_value=[('private row',)]):
            with self.assertRaises(FileAccessError) as error:
                instance.preflight(ROW, PATH, current=lambda: True)
        self.assertEqual(error.exception.code, 'write_backend_unverified')
        self.assertEqual(self.events[-1], ('reader.absence', 'start'))
        self.assert_fixed(reader.PREFLIGHT_STAGES)

    def test_reader_observer_exceptions_do_not_mask_original_error(self):
        def broken(*unused):
            raise RuntimeError('observer failed')
        instance = reader.NativeFileIndexReader(observer=broken)
        with mock.patch.object(reader, '_OpenedIndex', side_effect=FileAccessError('file_changed')):
            with self.assertRaises(FileAccessError) as error:
                instance.preflight(ROW, PATH, current=lambda: True)
        self.assertEqual(error.exception.code, 'file_changed')
        connection = self.connection()
        connection.set_authorizer(reader._schema_authorizer)
        self.assertIsNone(reader._schema(connection, observer=broken))


if __name__ == '__main__':
    unittest.main()
