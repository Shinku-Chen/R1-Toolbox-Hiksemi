"""Fixed diagnostic stages: synthetic proc metadata, never the NAS or vendor ELF.

Portable cases retain the production bounded comm/stat/status/cmdline readers.
Only namespace/root and executable descriptor operations use an in-memory OS
facade. Linux chain cases use real directory, SQLite and WAL file descriptors.
Diagnostic observations are evidence of rejection, never authorization.
"""

from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import media_sort_plugin as plugin
import vendor_runtime
from test_media_sort_plugin_process import elf_read_hook


PROCESS_STAGES = ('comm', 'stat', 'uid', 'namespace_mnt', 'namespace_net',
                  'namespace_pid', 'root', 'exe_link', 'exe_file', 'elf',
                  'cmdline', 'process_final')
DATABASE_STAGES = ('db_chain', 'sidecars', 'sqlite_fd', 'schema', 'json1',
                   'artifacts', 'publication')
SECRET = 'DIAGNOSTIC_SECRET_PATH_ARGUMENT_SENTINEL'


def write_stat(folder, *, start=123, pid=None, name='scraper', complete=True):
    """Construct the complete Linux field layout without a Linux process."""
    fields = ['S'] + ['0'] * 49
    fields[19] = str(start)
    if not complete:
        fields = fields[:20]
    record = '{} ({}) {}\n'.format(folder.name if pid is None else pid, name,
                                   ' '.join(fields))
    (folder / 'stat').write_bytes(record.encode('ascii'))


def file_stat(**changes):
    values = dict(st_dev=7, st_ino=9001, st_mode=stat.S_IFREG | 0o600,
                  st_uid=0, st_gid=0, st_nlink=1, st_size=4,
                  st_mtime_ns=1, st_ctime_ns=1)
    return SimpleNamespace(**{**values, **changes})


def record_connections(connect, connections):
    """Observe the SQLite API without shadowing its uri keyword argument."""
    def record_connect(database_uri, *args, **kwargs):
        connections.append((database_uri, kwargs.get('uri')))
        return connect(database_uri, *args, **kwargs)
    return record_connect


class _ProcFixture:
    """Use real bounded readers and an explicit fake executable descriptor."""

    def __init__(self, case):
        directory = tempfile.TemporaryDirectory(prefix='r1-media-diag-')
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.proc = self.root / 'proc'
        (self.proc / 'self/ns').mkdir(parents=True)
        self.info = file_stat()
        self.binary = b'TEST'
        self.link = '/audited/scraper'
        self.cwd = '/' + SECRET
        self.namespace_failures = set()
        self.namespace_errors = set()
        self.root_matches = True
        self.exe_error = None
        self.reads = 0
        self.position = 0
        self.stat_sequence = None
        self.guard = mock.Mock()
        self.folder = self.make_process(41)
        self.real_os = os
        facade = SimpleNamespace(**{key: getattr(os, key) for key in dir(os)})
        facade.name = 'posix'
        for key in ('O_CLOEXEC', 'O_NOFOLLOW', 'O_DIRECTORY'):
            setattr(facade, key, getattr(os, key, 0))
        facade.open = mock.Mock(side_effect=self.open)
        facade.close = mock.Mock()
        facade.read = mock.Mock(side_effect=self.read)
        facade.fstat = mock.Mock(side_effect=self.fstat)
        facade.stat = mock.Mock(side_effect=self.stat)
        facade.readlink = mock.Mock(side_effect=self.readlink)
        self.os = facade
        self.stack = ExitStack()
        case.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(plugin, 'os', facade))
        self.stack.enter_context(mock.patch.object(plugin, '_SHA', hashlib.sha256(b'TEST').hexdigest()))
        self.stack.enter_context(mock.patch.object(plugin, '_ELF_SIZE', 4))

    def make_process(self, pid, *, name='scraper', start=123):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'comm').write_bytes(name.encode('ascii') + b'\n')
        write_stat(folder, start=start, name=name)
        (folder / 'status').write_bytes(b'Name:\tscraper\nUid:\t0\t0\t0\t0\n')
        (folder / 'cmdline').write_bytes(
            b'/audited/scraper\0-d\0/' + SECRET.encode('ascii') + b'\0')
        return folder

    @staticmethod
    def _parts(value):
        return str(value).replace('\\', '/')

    def stat(self, path, *args, **kwargs):
        text = self._parts(path)
        if '/ns/' in text:
            kind = text.rsplit('/', 1)[1]
            remote = '/self/ns/' not in text
            if remote and kind in self.namespace_errors:
                raise PermissionError(SECRET)
            return file_stat(st_ino=100 + ('mnt', 'net', 'pid').index(kind)
                             + (1000 if remote and kind in self.namespace_failures else 0))
        if Path(path) == self.root:
            return file_stat(st_ino=1, st_mode=stat.S_IFDIR | 0o700)
        if text.endswith('/root'):
            return file_stat(st_ino=1 if self.root_matches else 2,
                             st_mode=stat.S_IFDIR | 0o700)
        return self.real_os.stat(path, *args, **kwargs)

    def readlink(self, path):
        text = self._parts(path)
        if text.endswith('/exe'):
            return self.link
        if text.endswith('/cwd'):
            return self.cwd
        raise AssertionError('unexpected readlink')

    def open(self, path, *args, **kwargs):
        if self.exe_error:
            raise self.exe_error
        if not self._parts(path).endswith('/exe'):
            raise AssertionError('unexpected executable open')
        self.position = 0
        return 9001

    def fstat(self, descriptor):
        if self.stat_sequence:
            return self.stat_sequence.pop(0)
        return copy.copy(self.info)

    def read(self, descriptor, limit):
        self.reads += 1
        result = self.binary[self.position:self.position + limit]
        self.position += len(result)
        return result

    def run(self, *, pid=41, expected=None):
        diagnostics = plugin._Diagnostics()
        observer = diagnostics.process(pid)
        result, error = None, None
        try:
            result = plugin._process(self.root, pid, self.guard,
                                     expected=expected, observer=observer)
        except (plugin.MediaSortError, OSError, UnicodeError) as caught:
            error = caught
        return result, error, diagnostics.snapshot()['processes'][0]


class MediaSortProcessDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _ProcFixture(self)

    def assert_failed(self, row, stage):
        self.assertEqual(row['stages'][stage]['status'], 'failed', row)
        self.assertTrue(row['stages'][stage]['reason_code'])
        for name in DATABASE_STAGES:
            self.assertEqual(row['stages'][name]['status'], 'not_run', row)
        self.assertNotIn(SECRET, json.dumps(row))

    def test_complete_process_chain_reports_all_fixed_gates_without_argument_values(self):
        result, error, row = self.fixture.run()
        self.assertIsNone(error)
        self.assertEqual(result[0:2], (41, 123))
        self.assertEqual(row['pid'], 41)
        self.assertEqual(row['start_ticks'], 123)
        self.assertEqual(set(row['stages']), set(PROCESS_STAGES + DATABASE_STAGES))
        for stage in PROCESS_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(row['stages'][stage]['status'], 'passed', row)
                self.assertIsInstance(row['stages'][stage]['elapsed_ms'], (int, float))
                self.assertGreaterEqual(row['stages'][stage]['elapsed_ms'], 0)
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn('/audited/scraper', json.dumps(row))

    def test_exact_vendor_0777_runs_the_real_hash_gate_before_arguments(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        result, error, row = self.fixture.run()
        self.assertIsNone(error)
        self.assertIsNotNone(result)
        self.assertGreater(self.fixture.reads, 0)
        self.assertEqual(row['stages']['exe_file']['observations']['mode'], 0o777)
        self.assertTrue(row['stages']['elf']['observations']['hash_matches'])
        for stage in PROCESS_STAGES:
            self.assertEqual(row['stages'][stage]['status'], 'passed', row)
        self.assertNotIn(SECRET, json.dumps(row))

    def test_vendor_0777_bad_hash_stops_arguments_and_all_database_dependencies(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        self.fixture.binary = b'FAIL'
        result, error, row = self.fixture.run()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(row['stages']['exe_file']['status'], 'passed')
        self.assert_failed(row, 'elf')
        self.assertEqual(row['stages']['elf']['reason_code'], 'elf_hash_mismatch')
        self.assertEqual(row['stages']['cmdline']['status'], 'not_run')

    def test_vendor_0777_retains_owner_type_link_and_exact_size_requirements(self):
        cases = (({'st_uid': 1000}, 'exe_file'),
                 ({'st_nlink': 2}, 'exe_file'),
                 ({'st_mode': stat.S_IFDIR | 0o777}, 'exe_file'),
                 ({'st_size': 5}, 'elf'))
        for changes, stage in cases:
            with self.subTest(changes=changes):
                self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                for name, value in changes.items():
                    setattr(self.fixture.info, name, value)
                self.fixture.reads = 0
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, stage)
                self.assertEqual(row['stages']['cmdline']['status'], 'not_run')
                self.assertEqual(self.fixture.reads, 0)

    def test_expected_sources_rehash_both_legacy_and_vendor_modes_with_or_without_observer(self):
        for mode in (0o600, 0o777):
            for observed in (False, True):
                with self.subTest(mode=oct(mode), observer=observed):
                    self.fixture.info = file_stat(st_mode=stat.S_IFREG | mode)
                    expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
                    before = self.fixture.reads
                    observer = plugin._Diagnostics().process(41) if observed else None
                    actual = plugin._process(self.fixture.root, 41, self.fixture.guard,
                                             expected=expected, observer=observer)
                    self.assertEqual(actual, expected)
                    self.assertGreater(self.fixture.reads, before)
                    if observer is not None:
                        self.assertTrue(observer.value['stages']['elf']['observations']['hash_matches'])

    def test_expected_source_same_metadata_cannot_hide_changed_executable_bytes(self):
        # A deliberately fixed fstat facade separates content proof from the
        # metadata-change gate. No real filesystem immutability is assumed.
        for mode in (0o600, 0o777):
            for observed in (False, True):
                with self.subTest(mode=oct(mode), observer=observed):
                    self.fixture.info = file_stat(st_mode=stat.S_IFREG | mode)
                    self.fixture.binary = b'TEST'
                    expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
                    self.fixture.binary = b'FAIL'
                    before = self.fixture.reads
                    observer = plugin._Diagnostics().process(41) if observed else None
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._process(self.fixture.root, 41, self.fixture.guard,
                                        expected=expected, observer=observer)
                    self.assertEqual(caught.exception.reason, 'elf_hash_mismatch')
                    self.assertGreater(self.fixture.reads, before)
                    if observer is not None:
                        self.assert_failed(observer.value, 'elf')
                        self.assertEqual(observer.value['stages']['cmdline']['status'], 'not_run')

    def test_expected_identity_and_link_mismatch_reject_before_reading_elf_bytes(self):
        for change in ('inode', 'link'):
            for observed in (False, True):
                with self.subTest(change=change, observer=observed):
                    self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                    self.fixture.link = '/audited/scraper'
                    expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
                    if change == 'inode':
                        self.fixture.info.st_ino += 1
                    else:
                        self.fixture.link = '/audited/replaced-scraper'
                    before = self.fixture.reads
                    observer = plugin._Diagnostics().process(41) if observed else None
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._process(self.fixture.root, 41, self.fixture.guard,
                                        expected=expected, observer=observer)
                    self.assertEqual(caught.exception.reason, 'identity_changed')
                    self.assertEqual(self.fixture.reads, before)

    def test_expected_pid_or_start_change_stops_hash_and_argument_read_before_revalidation(self):
        self.fixture.make_process(42)
        for change in ('start', 'pid'):
            for observed in (False, True):
                with self.subTest(change=change, observer=observed):
                    write_stat(self.fixture.folder, start=123)
                    self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                    expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
                    if change == 'start':
                        write_stat(self.fixture.folder, start=124)
                    pid = 42 if change == 'pid' else 41
                    observer = plugin._Diagnostics().process(pid) if observed else None
                    before = self.fixture.reads
                    with mock.patch.object(plugin, '_read', wraps=plugin._read) as read:
                        with self.assertRaises(plugin.MediaSortError) as caught:
                            plugin._process(self.fixture.root, pid, self.fixture.guard,
                                            expected=expected, observer=observer)
                    self.assertEqual(caught.exception.reason, 'identity_changed')
                    self.assertEqual(self.fixture.reads, before)
                    self.assertFalse(any(Path(call.args[0]).name == 'cmdline'
                                         for call in read.call_args_list))
                    if observer is not None:
                        self.assertEqual(observer.value['status'], 'identity_changed')
                        self.assertEqual(observer.value['stages']['cmdline']['status'], 'not_run')
                        self.assertTrue(all(observer.value['stages'][stage]['status'] == 'not_run'
                                            for stage in DATABASE_STAGES))

    def test_vendor_0777_hash_metadata_changes_reject_with_or_without_observer(self):
        for field, replacement in (('st_ino', 9002), ('st_mtime_ns', 2),
                                   ('st_ctime_ns', 2), ('st_size', 5)):
            for observed in (False, True):
                with self.subTest(field=field, observer=observed):
                    self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                    observer = plugin._Diagnostics().process(41) if observed else None
                    changed = []

                    def mutate(block):
                        changed.append(block)
                        setattr(self.fixture.info, field, replacement)

                    read_hook = elf_read_hook(self.fixture.read, self.fixture.fstat,
                                              (7, 9001), mutate)
                    with mock.patch.object(self.fixture.os, 'read', side_effect=read_hook):
                        with self.assertRaises(plugin.MediaSortError) as caught:
                            plugin._process(self.fixture.root, 41, self.fixture.guard,
                                            observer=observer)
                    self.assertEqual(changed, [b'TEST'])
                    self.assertEqual(caught.exception.reason, 'identity_changed')
                    if observer is not None:
                        self.assertTrue(all(observer.value['stages'][stage]['status'] == 'not_run'
                                            for stage in DATABASE_STAGES))

    def test_vendor_0777_fresh_proc_exe_descriptor_cannot_replace_unchanged_hashed_fd(self):
        # Both readlink calls retain the same spelling and the hashed FD keeps
        # every field. Only reopening proc/exe exposes the replacement inode.
        for observed in (False, True):
            with self.subTest(observer=observed):
                self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                original = copy.copy(self.fixture.info)
                replacement = file_stat(st_mode=stat.S_IFREG | 0o777, st_ino=9002)
                observer = plugin._Diagnostics().process(41) if observed else None
                self.fixture.position = 0
                with mock.patch.object(self.fixture.os, 'open', side_effect=[9001, 9002]) as opened, \
                        mock.patch.object(self.fixture.os, 'fstat', side_effect=lambda fd:
                                          original if fd == 9001 else replacement):
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._process(self.fixture.root, 41, self.fixture.guard, observer=observer)
                self.assertEqual(opened.call_count, 2)
                self.assertEqual(caught.exception.reason, 'identity_changed')
                if observer is not None:
                    self.assertTrue(all(observer.value['stages'][stage]['status'] == 'not_run'
                                        for stage in DATABASE_STAGES))

    def test_opened_cached_vendor_source_bad_hash_stops_before_chain_and_sidecars(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
        source = plugin._Source(expected[6], (expected,))
        self.fixture.binary = b'FAIL'
        unchanged = plugin._stable(self.fixture.info) + (
            self.fixture.info.st_size, self.fixture.info.st_mtime_ns,
            self.fixture.info.st_ctime_ns)
        self.assertEqual(unchanged, expected[3])
        with mock.patch.object(plugin._Opened, '_open_chain') as chain, \
                mock.patch.object(plugin._Opened, '_sidecars') as sidecars:
            with self.assertRaises(plugin.MediaSortError) as caught:
                plugin._Opened(self.fixture.root, source, self.fixture.guard)
        self.assertEqual(caught.exception.reason, 'elf_hash_mismatch')
        chain.assert_not_called()
        sidecars.assert_not_called()

    def test_apply_and_revert_cached_vendor_source_rehash_before_any_database_or_rule_access(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
        source = plugin._Source(expected[6], (expected,))
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        candidate_id, version = 'a' * 32, 'b' * 64
        value._sources[candidate_id] = source
        self.fixture.binary = b'FAIL'
        for operation in (value.apply, value.revert):
            with self.subTest(operation=operation.__name__):
                with mock.patch.object(plugin._Opened, '_open_chain') as chain, \
                        mock.patch.object(plugin._Opened, '_sidecars') as sidecars, \
                        mock.patch.object(plugin._Opened, 'connect') as opened_connect, \
                        mock.patch.object(plugin.sqlite3, 'connect') as sqlite_connect, \
                        mock.patch.object(value, '_backup') as backup, \
                        mock.patch.object(plugin.rules, 'install') as install, \
                        mock.patch.object(plugin.rules, 'remove') as remove:
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        operation(candidate_id, version, confirmed=True, current=lambda: True)
                self.assertEqual(caught.exception.reason, 'elf_hash_mismatch')
                for dependent in (chain, sidecars, opened_connect, sqlite_connect,
                                  backup, install, remove):
                    dependent.assert_not_called()

    def test_opened_revalidates_every_vendor_process_before_database_chain_then_sidecars(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        self.fixture.make_process(42)
        process = plugin._process
        expected = tuple(process(self.fixture.root, pid, self.fixture.guard) for pid in (41, 42))
        source = plugin._Source(expected[0][6], expected)
        events = []

        def revalidate(root, pid, guard, **kwargs):
            self.assertEqual(kwargs['expected'], expected[pid - 41])
            actual = process(root, pid, guard, **kwargs)
            events.append(('process', pid))
            return actual

        with mock.patch.object(plugin, '_process', side_effect=revalidate), \
                mock.patch.object(plugin._Opened, '_open_chain',
                                  side_effect=lambda *_args: events.append(('chain',))), \
                mock.patch.object(plugin._Opened, '_sidecars',
                                  side_effect=lambda: events.append(('sidecars',)) or {}):
            with plugin._Opened(self.fixture.root, source, self.fixture.guard):
                pass
        self.assertEqual(events, [('process', 41), ('process', 42), ('chain',), ('sidecars',)])

    def test_constructor_source_observer_attributes_hash_uid_and_pid_failures_without_sql(self):
        cases = (('hash', 'elf', 'elf', 'elf_hash_mismatch', 'rejected'),
                 ('uid', 'uid', 'uid', 'uid_not_root', 'rejected'),
                 ('pid', 'elf', 'process_final', 'identity_changed', 'identity_changed'))
        for failure, error_stage, failed_stage, reason, status in cases:
            with self.subTest(failure=failure):
                self.fixture.binary = b'TEST'
                self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                write_stat(self.fixture.folder, start=123)
                (self.fixture.folder / 'status').write_bytes(b'Uid:\t0\t0\t0\t0\n')
                diagnostics = plugin._Diagnostics()
                record = diagnostics.process(41)
                expected = plugin._process(self.fixture.root, 41, self.fixture.guard,
                                           observer=record)
                source = plugin._Source(expected[6], (expected,))
                if failure == 'hash':
                    self.fixture.binary = b'FAIL'
                elif failure == 'uid':
                    (self.fixture.folder / 'status').write_bytes(b'Uid:\t0\t1000\t0\t0\n')
                else:
                    write_stat(self.fixture.folder, start=124)
                # This is the constructor boundary itself: there is no caller
                # db_chain/publication phase to repair missing attribution.
                with plugin._observe_source(diagnostics, source), \
                        mock.patch.object(plugin._Opened, '_open_chain') as chain, \
                        mock.patch.object(plugin._Opened, '_sidecars') as sidecars, \
                        mock.patch.object(plugin.sqlite3, 'connect') as connect:
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._Opened(self.fixture.root, source, self.fixture.guard)
                for dependent in (chain, sidecars, connect):
                    dependent.assert_not_called()
                self.assertEqual(caught.exception.stage, error_stage)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception._media_sort_pid, 41)
                self.assertEqual(record.value['status'], status)
                self.assertEqual(record.value['stages'][failed_stage]['status'], 'failed')
                self.assertEqual(record.value['stages'][failed_stage]['reason_code'], reason)
                self.assertEqual(record.value['stages'][failed_stage]['observations'], {})
                self.assertEqual(diagnostics.first_failure(), (failed_stage, reason))
                for stage in DATABASE_STAGES:
                    self.assertEqual(record.value['stages'][stage]['status'], 'not_run')
                self.assertNotIn(SECRET, json.dumps(diagnostics.snapshot()) + str(caught.exception))

    def test_constructor_shared_source_rejection_changes_only_the_actual_process_record(self):
        second = self.fixture.make_process(42)
        cases = (('hash', 'elf', 'elf_hash_mismatch', 'rejected'),
                 ('uid', 'uid', 'uid_not_root', 'rejected'),
                 ('pid', 'process_final', 'identity_changed', 'identity_changed'))
        for failure, failed_stage, reason, status in cases:
            with self.subTest(failure=failure):
                self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
                self.fixture.binary = b'TEST'
                write_stat(second, start=123)
                (second / 'status').write_bytes(b'Uid:\t0\t0\t0\t0\n')
                diagnostics = plugin._Diagnostics()
                processes = tuple(plugin._process(
                    self.fixture.root, pid, self.fixture.guard, observer=diagnostics.process(pid))
                    for pid in (41, 42))
                source = plugin._Source(processes[0][6], processes)
                before = {row['pid']: row for row in diagnostics.snapshot()['processes']}
                if failure == 'uid':
                    (second / 'status').write_bytes(b'Uid:\t0\t1000\t0\t0\n')
                elif failure == 'pid':
                    write_stat(second, start=124)

                def open_executable(path, *args, **kwargs):
                    self.fixture.binary = (b'FAIL' if failure == 'hash' and Path(path).parent.name == '42'
                                           else b'TEST')
                    return self.fixture.open(path, *args, **kwargs)

                with plugin._observe_source(diagnostics, source), \
                        mock.patch.object(self.fixture.os, 'open', side_effect=open_executable), \
                        mock.patch.object(plugin._Opened, '_open_chain') as chain, \
                        mock.patch.object(plugin._Opened, '_sidecars') as sidecars, \
                        mock.patch.object(plugin.sqlite3, 'connect') as connect:
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._Opened(self.fixture.root, source, self.fixture.guard)
                for dependent in (chain, sidecars, connect):
                    dependent.assert_not_called()
                self.assertEqual(caught.exception._media_sort_pid, 42)
                after = {row['pid']: row for row in diagnostics.snapshot()['processes']}
                self.assertEqual(after[41]['status'], before[41]['status'])
                self.assertEqual(after[41]['start_ticks'], before[41]['start_ticks'])
                for stage in PROCESS_STAGES + DATABASE_STAGES:
                    self.assertEqual(after[41]['stages'][stage]['status'],
                                     before[41]['stages'][stage]['status'])
                    self.assertEqual(after[41]['stages'][stage]['observations'],
                                     before[41]['stages'][stage]['observations'])
                self.assertEqual(after[42]['status'], status)
                self.assertEqual(after[42]['stages'][failed_stage]['status'], 'failed')
                self.assertEqual(after[42]['stages'][failed_stage]['reason_code'], reason)
                self.assertEqual(diagnostics.first_failure(), (failed_stage, reason))
                for stage in DATABASE_STAGES:
                    self.assertEqual(after[42]['stages'][stage]['status'], 'not_run')
                self.assertNotIn(SECRET, json.dumps(diagnostics.snapshot()))

    def test_second_alias_constructor_preserves_prior_db_phases_but_records_new_elf_rejection(self):
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        diagnostics = plugin._Diagnostics()
        record = diagnostics.process(41)
        expected = plugin._process(self.fixture.root, 41, self.fixture.guard, observer=record)
        source = plugin._Source(expected[6], (expected,))
        alias = plugin._Source('synthetic-alias/scraper.db', (expected,))
        entries = []

        def validated_chain(*_args):
            # Synthetic successful production phases model a first alias's
            # already retained proof; no DB or SQL is opened by this fixture.
            with plugin._phase('db_chain'):
                entries.append('chain')
                plugin._note('db_chain', depth=2, main_header=True)

        def validated_sidecars():
            with plugin._phase('sidecars'):
                entries.append('sidecars')
                plugin._note('sidecars', wal_present=False, shm_present=False,
                             journal_present=False)
            return {}

        with mock.patch.object(plugin._Opened, '_open_chain', side_effect=validated_chain) as chain, \
                mock.patch.object(plugin._Opened, '_sidecars', side_effect=validated_sidecars) as sidecars, \
                mock.patch.object(plugin.sqlite3, 'connect') as connect:
            with plugin._observe_source(diagnostics, source):
                with plugin._Opened(self.fixture.root, source, self.fixture.guard):
                    pass
            before = copy.deepcopy(record.value['stages'])
            self.assertEqual(entries, ['chain', 'sidecars'])
            self.fixture.binary = b'FAIL'
            # Exactly the second alias constructor's _observe_source tuple,
            # without an unrelated outer phase to hide the attribution gap.
            with plugin._observe_source(diagnostics, alias):
                with self.assertRaises(plugin.MediaSortError) as caught:
                    plugin._Opened(self.fixture.root, alias, self.fixture.guard)
        chain.assert_called_once()
        sidecars.assert_called_once()
        connect.assert_not_called()
        self.assertEqual(caught.exception.stage, 'elf')
        self.assertEqual(caught.exception.reason, 'elf_hash_mismatch')
        self.assertEqual(record.value['status'], 'rejected')
        self.assertEqual(record.value['stages']['elf']['status'], 'failed')
        self.assertEqual(record.value['stages']['elf']['observations'], {})
        self.assertEqual(diagnostics.first_failure(), ('elf', 'elf_hash_mismatch'))
        for stage in ('db_chain', 'sidecars'):
            self.assertEqual(before[stage]['status'], 'passed')
            self.assertEqual(record.value['stages'][stage], before[stage])
        for stage in ('sqlite_fd', 'schema', 'json1', 'artifacts', 'publication'):
            self.assertEqual(record.value['stages'][stage]['status'], 'not_run')
        self.assertNotIn(SECRET, json.dumps(diagnostics.snapshot()))

    def test_hash_read_generation_race_rejects_with_and_without_observer(self):
        real_identity, real_read, real_fstat = plugin._kernel_identity, os.read, os.fstat
        self.fixture.info = file_stat(st_mode=stat.S_IFREG | 0o777)
        self.assertIs(vendor_runtime.os, os)
        self.assertIs(plugin._read, vendor_runtime._read)
        self.assertIs(real_identity, vendor_runtime._kernel_identity)
        binary_identity = (self.fixture.info.st_dev, self.fixture.info.st_ino)
        for observed in (False, True):
            with self.subTest(observer=observed):
                write_stat(self.fixture.folder, start=123)
                identities, changed_at_read, reads = [], [], []
                observer = plugin._Diagnostics().process(41) if observed else None

                def capture_identity(folder):
                    value = real_identity(folder)
                    identities.append(value)
                    return value

                def read(descriptor, limit):
                    # The vendor reader keeps real open/read/close for proc.
                    # Only the executable FD belongs to the explicit facade.
                    block = (self.fixture.read(descriptor, limit) if descriptor == 9001
                             else real_read(descriptor, limit))
                    if block:
                        reads.append((descriptor == 9001, block, len(identities), bool(changed_at_read)))
                    return block

                def fstat(descriptor):
                    return self.fixture.fstat(descriptor) if descriptor == 9001 else real_fstat(descriptor)

                def change_after_elf_read(block):
                    self.assertEqual(identities[0], (41, b'scraper', 123))
                    changed_at_read.append(block)
                    write_stat(self.fixture.folder, start=124)

                read_hook = elf_read_hook(read, fstat, binary_identity, change_after_elf_read)
                with mock.patch.object(os, 'read', side_effect=read_hook), \
                        mock.patch.object(self.fixture.os, 'read', side_effect=read_hook), \
                        mock.patch.object(plugin, '_kernel_identity', side_effect=capture_identity):
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        plugin._process(self.fixture.root, 41, self.fixture.guard, observer=observer)
                self.assertEqual(changed_at_read, [b'TEST'])
                first_elf = next(i for i, event in enumerate(reads) if event[0])
                before_elf = reads[:first_elf]
                self.assertTrue(any(block == b'scraper\n' and captured == 0
                                    for _, block, captured, _ in before_elf))
                self.assertTrue(any(block.startswith(b'41 (scraper) ') and captured == 0
                                    for _, block, captured, _ in before_elf))
                self.assertTrue(all(not is_elf and not changed
                                    for is_elf, _, _, changed in before_elf))
                self.assertGreater(reads[first_elf][2], 0)
                self.assertEqual((caught.exception.code, caught.exception.stage, caught.exception.reason),
                                 ('unavailable', 'process_final', 'identity_changed'))
                if observer is not None:
                    self.assertEqual(observer.value['status'], 'identity_changed')
                    self.assertIsNone(observer.value['start_ticks'])
                    self.assertTrue(all(not stage['observations']
                                        for stage in observer.value['stages'].values()))
                    self.assertNotIn(SECRET, json.dumps(observer.value))

    def test_comm_rejection_is_visible_and_does_not_hash_or_open_database(self):
        (self.fixture.folder / 'comm').write_bytes(b'other\n')
        result, error, row = self.fixture.run()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assert_failed(row, 'comm')
        self.assertEqual(self.fixture.reads, 0)

    def test_each_incomplete_or_inconsistent_stat_fails_before_other_metadata(self):
        for changes in ({'complete': False}, {'pid': 42}, {'name': 'other'}, {'start': 0}):
            with self.subTest(changes=changes):
                write_stat(self.fixture.folder, **changes)
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'stat')
                self.assertEqual(row['stages']['uid']['status'], 'not_run')
                self.assertEqual(self.fixture.reads, 0)

    def test_each_uid_column_is_independently_checked_and_blocks_hash_and_arguments(self):
        for index in range(4):
            values = ['0'] * 4
            values[index] = '1000'
            with self.subTest(column=index):
                (self.fixture.folder / 'status').write_bytes(
                    ('Uid:\t' + '\t'.join(values) + '\n').encode('ascii'))
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'uid')
                for stage in ('namespace_mnt', 'namespace_net', 'namespace_pid', 'root',
                              'exe_link', 'exe_file'):
                    self.assertEqual(row['stages'][stage]['status'], 'passed')
                for stage in ('elf', 'cmdline'):
                    self.assertEqual(row['stages'][stage]['status'], 'not_run')
                self.assertEqual(self.fixture.reads, 0)

    def test_missing_uid_is_distinguished_from_an_audited_executable(self):
        (self.fixture.folder / 'status').write_bytes(b'Name:\tscraper\n')
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assert_failed(row, 'uid')
        self.assertEqual(row['stages']['elf']['status'], 'not_run')

    def test_namespace_failures_keep_independent_kernel_and_audited_elf_observations(self):
        for kind in ('mnt', 'net', 'pid'):
            with self.subTest(namespace=kind):
                self.fixture.namespace_failures = {kind}
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'namespace_' + kind)
                for other in ('mnt', 'net', 'pid'):
                    if other != kind:
                        self.assertEqual(row['stages']['namespace_' + other]['status'], 'passed')
                self.assertEqual(row['stages']['root']['status'], 'passed')
                self.assertEqual(row['stages']['elf']['status'], 'passed')

    def test_namespace_permission_error_does_not_leak_exception_or_hide_other_metadata(self):
        self.fixture.namespace_errors = {'mnt'}
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assert_failed(row, 'namespace_mnt')
        self.assertEqual(row['stages']['namespace_net']['status'], 'passed')
        self.assertEqual(row['stages']['elf']['status'], 'passed')

    def test_root_mismatch_does_not_hide_elf_hash_but_cannot_create_source(self):
        self.fixture.root_matches = False
        result, error, row = self.fixture.run()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assert_failed(row, 'root')
        self.assertEqual(row['stages']['elf']['status'], 'passed')

    def test_executable_links_reject_deleted_and_relative_names_before_hash(self):
        for link in ('relative/' + SECRET, '/' + SECRET + ' (deleted)'):
            with self.subTest(link_kind='deleted' if link.endswith(' (deleted)') else 'relative'):
                self.fixture.link = link
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'exe_link')
                self.assertEqual(row['stages']['elf']['status'], 'not_run')
                self.assertEqual(row['stages']['cmdline']['status'], 'not_run')
                self.assertEqual(self.fixture.reads, 0)

    def test_executable_file_type_owner_mode_and_links_gate_hash(self):
        cases = ({'st_mode': stat.S_IFDIR | 0o600}, {'st_uid': 1000},
                 {'st_mode': stat.S_IFREG | 0o620}, {'st_mode': stat.S_IFREG | 0o602},
                 {'st_mode': stat.S_IFREG | 0o666}, {'st_mode': stat.S_IFREG | 0o775},
                 {'st_mode': stat.S_IFREG | 0o1777}, {'st_mode': stat.S_IFREG | 0o2777},
                 {'st_mode': stat.S_IFREG | 0o4777},
                 {'st_nlink': 2})
        for changes in cases:
            with self.subTest(changes=changes):
                self.fixture.info = file_stat(**changes)
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'exe_file')
                self.assertEqual(row['stages']['elf']['status'], 'not_run')
                self.assertEqual(row['stages']['cmdline']['status'], 'not_run')
                self.assertEqual(self.fixture.reads, 0)

    def test_executable_size_mismatch_is_reported_before_hash_and_arguments(self):
        self.fixture.info = file_stat(st_size=5)
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assert_failed(row, 'elf')
        self.assertEqual(row['stages']['cmdline']['status'], 'not_run')
        self.assertEqual(self.fixture.reads, 0)

    def test_executable_open_error_is_safe_and_does_not_hide_kernel_observations(self):
        self.fixture.exe_error = PermissionError(SECRET)
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assert_failed(row, 'exe_file')
        self.assertEqual(row['stages']['root']['status'], 'passed')
        self.assertEqual(row['stages']['cmdline']['status'], 'not_run')

    def test_audited_hash_mismatch_blocks_arguments_and_database(self):
        self.fixture.binary = b'FAIL'
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assert_failed(row, 'elf')
        self.assertEqual(row['stages']['cmdline']['status'], 'not_run')

    def test_executable_identity_change_during_hash_cannot_leave_passed_elf(self):
        self.fixture.stat_sequence = [file_stat(), file_stat(st_ino=9002)]
        _, error, row = self.fixture.run()
        self.assertIsNotNone(error)
        self.assertIn(row['stages']['elf']['status'], ('failed', 'not_run'))
        self.assertNotIn('passed', (row['stages']['cmdline']['status'],
                                    row['stages']['process_final']['status']))
        self.assertNotIn(SECRET, json.dumps(row))

    def test_cmdline_rejects_unterminated_invalid_utf8_missing_duplicate_and_empty_d(self):
        cases = (b'/audited/scraper\0-d\0/' + SECRET.encode(),
                 b'/audited/scraper\0-d\0/\xff\0',
                 b'/audited/scraper\0--secret\0' + SECRET.encode() + b'\0',
                 b'/audited/scraper\0-d\0/a\0-d\0/b\0',
                 b'/audited/scraper\0-d\0\0',
                 b'/audited/scraper\0-d\0',
                 b'/audited/scraper\0-d\0/' + b'x' * 4096 + b'\0')
        for raw in cases:
            with self.subTest(length=len(raw)):
                (self.fixture.folder / 'cmdline').write_bytes(raw)
                result, error, row = self.fixture.run()
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assert_failed(row, 'cmdline')

    def test_relative_d_only_reports_kind_and_cwd_validity_not_values(self):
        (self.fixture.folder / 'cmdline').write_bytes(
            b'/audited/scraper\0-d\0relative/' + SECRET.encode() + b'\0')
        result, error, row = self.fixture.run()
        self.assertIsNone(error)
        self.assertIsNotNone(result)
        self.assertEqual(row['stages']['cmdline']['status'], 'passed')
        self.assertNotIn(SECRET, json.dumps(row))
        for cwd in ('relative/' + SECRET, '/' + SECRET + ' (deleted)'):
            with self.subTest(cwd_kind='deleted' if cwd.endswith(' (deleted)') else 'relative'):
                self.fixture.cwd = cwd
                _, error, row = self.fixture.run()
                self.assertIsNotNone(error)
                self.assert_failed(row, 'cmdline')

    def test_pid_generation_change_clears_prior_generation_observations(self):
        original = plugin._kernel_identity
        calls = [0]

        def replace(folder, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                write_stat(self.fixture.folder, start=124)
            return original(folder, *args, **kwargs)

        with mock.patch.object(plugin, '_kernel_identity', side_effect=replace):
            result, error, row = self.fixture.run()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertIn('identity_changed', json.dumps(row))
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in row['stages'].values()), row)

    def test_no_observer_preserves_process_gate_and_result_contract(self):
        expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
        observed, error, _ = self.fixture.run()
        self.assertIsNone(error)
        self.assertEqual(observed, expected)
        self.fixture.namespace_failures = {'net'}
        with self.assertRaises(plugin.MediaSortError):
            plugin._process(self.fixture.root, 41, self.fixture.guard)

    def test_expected_argument_identity_change_cannot_retain_accepted_generation(self):
        expected = plugin._process(self.fixture.root, 41, self.fixture.guard)
        raw = (self.fixture.folder / 'cmdline').read_bytes()
        (self.fixture.folder / 'cmdline').write_bytes(raw + b'--changed\0')
        result, error, row = self.fixture.run(expected=expected)
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertIn('identity_changed', json.dumps(row))
        self.assertEqual(row['status'], 'identity_changed')
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in row['stages'].values()), row)

    def test_first_namespace_io_error_survives_later_pid_replacement_without_mixed_fields(self):
        self.fixture.namespace_errors = {'mnt'}
        original = plugin._kernel_identity
        calls = [0]

        def replace(folder, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                write_stat(self.fixture.folder, start=124)
            return original(folder, *args, **kwargs)

        with mock.patch.object(plugin, '_kernel_identity', side_effect=replace):
            result, error, row = self.fixture.run()
        self.assertIsNone(result)
        self.assertIsInstance(error, PermissionError)
        self.assertEqual(row['status'], 'identity_changed')
        self.assertIsNone(row['start_ticks'])
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in row['stages'].values()), row)
        self.assertNotIn(SECRET, json.dumps(row))

    def test_real_sidecar_primitive_error_keeps_innermost_phase_through_outer_db_scope(self):
        diagnostics = plugin._Diagnostics()
        observer = diagnostics.process(41)
        opened = plugin._Opened.__new__(plugin._Opened)
        opened.directory, opened.mutating = 12, False
        primitive = PermissionError(SECRET)
        token = plugin._OBSERVER.set(observer)
        try:
            with mock.patch.object(plugin.os, 'stat', side_effect=primitive):
                with self.assertRaises(PermissionError) as caught:
                    with plugin._phase('db_chain'):
                        opened._sidecars()
        finally:
            plugin._OBSERVER.reset(token)
        self.assertIs(caught.exception, primitive)
        self.assertEqual(primitive._media_sort_stage, 'sidecars')
        row = diagnostics.snapshot()['processes'][0]
        self.assertEqual(row['stages']['sidecars']['status'], 'failed')
        self.assertNotEqual(row['stages']['db_chain']['status'], 'failed')
        self.assertNotIn(SECRET, json.dumps(row))

    def shared_source(self):
        """Bind two audited synthetic PIDs to the same private DB spelling."""
        folder = self.fixture.make_process(42)
        diagnostics = plugin._Diagnostics()
        processes = tuple(plugin._process(
            self.fixture.root, pid, self.fixture.guard, observer=diagnostics.process(pid))
            for pid in (41, 42))
        opened = plugin._Opened.__new__(plugin._Opened)
        opened.root, opened.guard = self.fixture.root, self.fixture.guard
        opened.source = plugin._Source(processes[0][6], processes)
        return diagnostics, opened, folder

    def test_shared_source_uid_revalidation_failure_marks_only_its_actual_pid(self):
        diagnostics, opened, second_folder = self.shared_source()
        before = {row['pid']: row for row in diagnostics.snapshot()['processes']}
        (second_folder / 'status').write_bytes(b'Uid:\t0\t1000\t0\t0\n')
        with plugin._observe_source(diagnostics, opened.source), \
                mock.patch.object(plugin, '_OpenedChain') as chain:
            with self.assertRaises(plugin.MediaSortError) as caught:
                opened.current()
        chain.assert_not_called()
        self.assertEqual(caught.exception.stage, 'uid')
        self.assertEqual(caught.exception._media_sort_pid, 42)
        after = {row['pid']: row for row in diagnostics.snapshot()['processes']}
        self.assertEqual(after[41], before[41])
        self.assertEqual(after[42]['stages']['uid']['status'], 'failed')
        self.assertEqual(after[42]['stages']['uid']['observations'], {})
        self.assertEqual(after[42]['status'], 'rejected')
        self.assertEqual(after[41]['stages']['uid']['observations']['effective_uid'], 0)
        self.assertEqual(diagnostics.first_failure()[0], 'uid')
        encoded = json.dumps(diagnostics.snapshot())
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn('_media_sort_pid', encoded)

    def test_shared_source_one_pid_reuse_does_not_clear_other_process_observations(self):
        diagnostics, opened, second_folder = self.shared_source()
        before = {row['pid']: row for row in diagnostics.snapshot()['processes']}
        write_stat(second_folder, start=124)
        with plugin._observe_source(diagnostics, opened.source), \
                mock.patch.object(plugin, '_OpenedChain') as chain:
            with self.assertRaises(plugin.MediaSortError) as caught:
                opened.current()
        chain.assert_not_called()
        self.assertEqual(caught.exception._media_sort_pid, 42)
        after = {row['pid']: row for row in diagnostics.snapshot()['processes']}
        self.assertEqual(after[41], before[41])
        self.assertEqual(after[42]['status'], 'identity_changed')
        self.assertIsNone(after[42]['start_ticks'])
        self.assertEqual(after[42]['stages']['process_final']['status'], 'failed')
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in after[42]['stages'].values()), after[42])
        encoded = json.dumps(diagnostics.snapshot())
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn('_media_sort_pid', encoded)


class MediaSortDiscoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _ProcFixture(self)

    def discover(self):
        diagnostics = plugin._Diagnostics()
        result, error = None, None
        try:
            result = plugin._discover(self.fixture.root, self.fixture.guard,
                                      observer=diagnostics)
        except plugin.MediaSortError as caught:
            error = caught
        return result, error, diagnostics.snapshot()

    def test_no_matching_process_is_complete_not_a_fabricated_candidate(self):
        (self.fixture.folder / 'comm').write_bytes(b'other\n')
        result, error, diagnostics = self.discover()
        self.assertIsNone(error)
        self.assertEqual(result, [])
        scan = diagnostics['scan']
        self.assertEqual(scan['status'], 'passed')
        self.assertTrue(scan['observations']['complete'])
        self.assertEqual(scan['observations']['comm_matches'], 0)
        self.assertEqual(diagnostics['processes'], [])

    def test_multiple_scrapers_are_counted_individually_and_preserve_source_identity(self):
        self.fixture.make_process(42)
        self.fixture.make_process(43, name='other')
        result, error, diagnostics = self.discover()
        self.assertIsNone(error)
        self.assertEqual(len(result), 2)
        self.assertEqual({source.processes[0][0] for source in result}, {41, 42})
        counts = diagnostics['scan']['observations']
        self.assertEqual(counts['comm_matches'], 2)
        self.assertEqual(counts['accepted'], 2)
        self.assertEqual(counts['rejected'], 0)
        self.assertEqual({row['pid'] for row in diagnostics['processes']}, {41, 42})

    def test_a_rejected_process_retains_failclosed_discovery_and_diagnostic_reason(self):
        self.fixture.make_process(42)
        self.fixture.namespace_failures = {'mnt'}
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        with mock.patch.object(plugin, '_Opened') as opened:
            result = value.probe(current=lambda: True)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['candidates'], [])
        opened.assert_not_called()
        rows = result['diagnostics']['processes']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['stages']['namespace_mnt']['status'], 'failed')
        counts = result['diagnostics']['scan']['observations']
        self.assertEqual(counts['rejected'], 2)
        self.assertEqual(counts['accepted'], 0)
        self.assertTrue(counts['complete'])
        self.assertIn('namespace_mnt', result['message'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_scan_process_limit_is_rejection_with_at_most_sixteen_diagnostic_records(self):
        for pid in range(42, 58):
            self.fixture.make_process(pid)
        result, error, diagnostics = self.discover()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(diagnostics['scan']['status'], 'failed')
        self.assertLessEqual(len(diagnostics['processes']), 16)
        self.assertFalse(diagnostics['scan']['observations']['complete'])

    def test_scan_entry_limit_is_not_misreported_as_a_complete_empty_scan(self):
        class Entries:
            def __enter__(self):
                return iter(SimpleNamespace(name='not-a-pid', path='not-used')
                            for _ in range(8193))

            def __exit__(self, *_unused):
                return False

        self.fixture.os.scandir = mock.Mock(return_value=Entries())
        result, error, diagnostics = self.discover()
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(diagnostics['scan']['status'], 'failed')
        self.assertFalse(diagnostics['scan']['observations']['complete'])
        self.assertGreaterEqual(diagnostics['scan']['observations']['entries_seen'], 8192)
        self.assertEqual(diagnostics['processes'], [])

    def test_scan_io_failure_never_exports_upstream_exception_text(self):
        self.fixture.os.scandir = mock.Mock(side_effect=PermissionError(SECRET))
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        with self.assertRaises(plugin.MediaSortError) as error:
            value.probe(current=lambda: True)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertNotIn(SECRET, json.dumps(value.snapshot()))

    def test_namespace_permission_error_keeps_503_code_and_publishes_only_safe_cached_diagnostics(self):
        self.fixture.namespace_errors = {'mnt'}
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        with mock.patch.object(plugin, '_Opened') as opened:
            with self.assertRaises(plugin.MediaSortError) as caught:
                value.probe(current=lambda: True)
        # The owner HTTP route maps this existing unavailable exception to 503;
        # diagnostic collection must not turn it into the successful 200 path.
        self.assertEqual(caught.exception.code, 'unavailable')
        opened.assert_not_called()
        state = value.snapshot()
        self.assertEqual(state['candidates'], [])
        self.assertEqual(value._sources, {})
        self.assertEqual(state['diagnostics']['status'], 'complete')
        row = state['diagnostics']['processes'][0]
        self.assertEqual(row['stages']['namespace_mnt']['status'], 'failed')
        self.assertEqual(row['stages']['namespace_net']['status'], 'passed')
        self.assertEqual(row['stages']['elf']['status'], 'passed')
        for stage in DATABASE_STAGES:
            self.assertEqual(row['stages'][stage]['status'], 'not_run')
        self.assertIn('namespace_mnt', state['message'])
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_unicode_decode_error_retains_503_code_and_safe_cmdline_failure_in_cache(self):
        (self.fixture.folder / 'cmdline').write_bytes(
            b'/audited/scraper\0-d\0/' + SECRET.encode() + b'\xff\0')
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        with self.assertRaises(plugin.MediaSortError) as caught:
            value.probe(current=lambda: True)
        self.assertEqual(caught.exception.code, 'unavailable')
        state = value.snapshot()
        self.assertEqual(state['diagnostics']['status'], 'complete')
        self.assertEqual(state['diagnostics']['processes'][0]['stages']['cmdline']['status'], 'failed')
        self.assertFalse(state['diagnostics']['processes'][0]['stages']['cmdline']['observations']['utf8'])
        self.assertEqual(state['candidates'], [])
        self.assertEqual(value._sources, {})
        self.assertNotIn(SECRET, json.dumps(state))

    def test_first_io_error_plus_pid_replacement_remains_503_and_cached_record_is_cleared(self):
        self.fixture.namespace_errors = {'mnt'}
        original = plugin._kernel_identity
        calls = [0]

        def replace(folder, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                write_stat(self.fixture.folder, start=124)
            return original(folder, *args, **kwargs)

        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        with mock.patch.object(plugin, '_kernel_identity', side_effect=replace):
            with self.assertRaises(plugin.MediaSortError) as caught:
                value.probe(current=lambda: True)
        self.assertEqual(caught.exception.code, 'unavailable')
        state = value.snapshot()
        self.assertEqual(state['candidates'], [])
        self.assertEqual(state['diagnostics']['status'], 'complete')
        record = state['diagnostics']['processes'][0]
        self.assertEqual(record['status'], 'identity_changed')
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in record['stages'].values()), record)
        self.assertNotIn(SECRET, json.dumps(state))

    def test_completed_probe_cache_is_deep_copied_without_another_scan_or_read(self):
        (self.fixture.folder / 'comm').write_bytes(b'other\n')
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        first = value.probe(current=lambda: True)
        self.assertEqual(first['diagnostics']['status'], 'complete')
        expected = value.snapshot()
        with mock.patch.object(plugin.os, 'scandir', side_effect=AssertionError('unexpected proc')), \
                mock.patch.object(plugin, '_read', side_effect=AssertionError('unexpected read')), \
                mock.patch.object(plugin.sqlite3, 'connect', side_effect=AssertionError('unexpected DB')):
            cached = value.snapshot()
            cached['diagnostics']['scan']['observations']['comm_matches'] = 999
            self.assertEqual(value.snapshot(), expected)

    def test_timeout_after_previous_probe_keeps_only_previously_published_diagnostics(self):
        (self.fixture.folder / 'comm').write_bytes(b'other\n')
        value = plugin.MediaSortPlugin(self.fixture.root, root=self.fixture.root)
        self.addCleanup(value.close)
        value.probe(current=lambda: True)
        previous = value.snapshot()
        with mock.patch.object(plugin, '_discover', side_effect=plugin.MediaSortError('timeout')):
            with self.assertRaises(plugin.MediaSortError):
                value.probe(current=lambda: True)
        self.assertEqual(value.snapshot(), previous)


class MediaSortDiagnosticCacheTests(unittest.TestCase):
    def setUp(self):
        self.value = plugin.MediaSortPlugin('diagnostic-state-must-not-be-created')
        self.addCleanup(self.value.close)

    def test_constructor_get_and_close_are_cached_only_and_unknown_is_empty(self):
        with mock.patch.object(plugin, '_discover', side_effect=AssertionError('unexpected proc')), \
                mock.patch.object(plugin, '_read', side_effect=AssertionError('unexpected read')), \
                mock.patch.object(plugin.sqlite3, 'connect', side_effect=AssertionError('unexpected DB')):
            for unused in range(3):
                state = self.value.snapshot()
                self.assertEqual(state['diagnostics']['schema_version'], 1)
                self.assertEqual(state['diagnostics']['status'], 'unknown')
                self.assertEqual(state['diagnostics']['processes'], [])
            state['diagnostics']['processes'].append({'untrusted': True})
            self.assertEqual(self.value.snapshot()['diagnostics']['processes'], [])
            self.value.close()
            self.assertEqual(self.value.snapshot()['diagnostics']['status'], 'unknown')

    def test_probe_timeout_discards_new_private_diagnostics_and_retains_old_cache(self):
        before = self.value.snapshot()

        def expire(root, guard, observer=None):
            observer = observer or plugin._DISCOVERY_OBSERVER.get()
            if observer is not None:
                observer.process(41)
            raise plugin.MediaSortError('timeout')

        with mock.patch.object(plugin, '_discover', side_effect=expire):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.value.probe(current=lambda: True)
        self.assertEqual(error.exception.code, 'timeout')
        self.assertEqual(self.value.snapshot(), before)

    def test_revocation_after_sampling_discards_private_process_records(self):
        before = self.value.snapshot()
        authorized = [True]

        def revoke(root, guard, observer=None):
            observer = observer or plugin._DISCOVERY_OBSERVER.get()
            if observer is not None:
                observer.process(41)
            authorized[0] = False
            return []

        with mock.patch.object(plugin, '_discover', side_effect=revoke):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.value.probe(current=lambda: authorized[0])
        self.assertEqual(error.exception.code, 'authorization_expired')
        self.assertEqual(self.value.snapshot(), before)

    def test_close_after_sampling_cannot_revive_old_private_diagnostics(self):
        def stop(root, guard, observer=None):
            observer = observer or plugin._DISCOVERY_OBSERVER.get()
            if observer is not None:
                observer.process(41)
            self.value.close()
            return []

        with mock.patch.object(plugin, '_discover', side_effect=stop):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.value.probe(current=lambda: True)
        self.assertEqual(error.exception.code, 'authorization_expired')
        self.assertEqual(self.value.snapshot()['diagnostics']['status'], 'unknown')
        self.assertEqual(self.value.snapshot()['diagnostics']['processes'], [])

    def test_unexpected_exception_parameters_cannot_reach_cache_or_response(self):
        with mock.patch.object(plugin, '_discover', side_effect=RuntimeError(SECRET)):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.value.probe(current=lambda: True)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertNotIn(SECRET, json.dumps(self.value.snapshot()))

    def cached_value(self, with_previous):
        """Retain distinguishable old cache and sources for rollback assertions."""
        value = plugin.MediaSortPlugin('diagnostic-state-must-not-be-created')
        self.addCleanup(value.close)
        if with_previous:
            with mock.patch.object(plugin, '_discover', return_value=[]):
                value.probe(current=lambda: True)
            # Stand-in for a previously authorized opaque candidate; no path
            # is opened by this test or by the new empty probe.
            value._sources = {'previous': plugin._Source('old/scraper.db', ())}
        return value, value.snapshot(), dict(value._sources)

    def test_revocation_or_timeout_during_private_snapshot_keeps_initial_or_old_cache(self):
        snapshot = plugin._Diagnostics.snapshot
        for with_previous in (False, True):
            for failure in ('authorization_expired', 'timeout'):
                with self.subTest(previous=with_previous, failure=failure):
                    value, before, sources_before = self.cached_value(with_previous)
                    authorized, clock = [True], [100.0]

                    def expire(diagnostics):
                        private = snapshot(diagnostics)
                        if failure == 'timeout':
                            clock[0] = 1000.0
                        else:
                            authorized[0] = False
                        return private

                    with mock.patch.object(plugin, '_discover', return_value=[]), \
                            mock.patch.object(plugin._Diagnostics, 'snapshot', expire), \
                            mock.patch.object(plugin.time, 'monotonic', side_effect=lambda: clock[0]):
                        with self.assertRaises(plugin.MediaSortError) as caught:
                            value.probe(current=lambda: authorized[0])
                    self.assertEqual(caught.exception.code, failure)
                    self.assertEqual(value.snapshot(), before)
                    self.assertEqual(value._sources, sources_before)

    def test_close_during_private_snapshot_cannot_publish_or_restore_old_cache(self):
        snapshot = plugin._Diagnostics.snapshot
        for with_previous in (False, True):
            with self.subTest(previous=with_previous):
                value, before, _ = self.cached_value(with_previous)

                def close(diagnostics):
                    private = snapshot(diagnostics)
                    value.close()
                    return private

                with mock.patch.object(plugin, '_discover', return_value=[]), \
                        mock.patch.object(plugin._Diagnostics, 'snapshot', close):
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        value.probe(current=lambda: True)
                self.assertEqual(caught.exception.code, 'authorization_expired')
                self.assertEqual(value.snapshot()['status'], 'unknown')
                self.assertEqual(value.snapshot()['diagnostics']['status'], 'unknown')
                self.assertEqual(value.snapshot()['diagnostics']['processes'], [])
                self.assertEqual(value._sources, {})

    def test_operation_exit_revocation_or_timeout_rolls_back_new_cache_and_sources(self):
        for with_previous in (False, True):
            for failure in ('authorization_expired', 'timeout'):
                with self.subTest(previous=with_previous, failure=failure):
                    value, before, sources_before = self.cached_value(with_previous)
                    snapshot = value.snapshot
                    authorized, clock = [True], [100.0]
                    deliveries = []

                    def expire_after_publication():
                        private = snapshot()
                        deliveries.append(private)
                        if failure == 'timeout':
                            clock[0] = 1000.0
                        else:
                            authorized[0] = False
                        return private

                    with mock.patch.object(plugin, '_discover', return_value=[]), \
                            mock.patch.object(value, 'snapshot', side_effect=expire_after_publication), \
                            mock.patch.object(plugin.time, 'monotonic', side_effect=lambda: clock[0]):
                        with self.assertRaises(plugin.MediaSortError) as caught:
                            value.probe(current=lambda: authorized[0])
                    self.assertTrue(deliveries)
                    self.assertEqual(caught.exception.code, failure)
                    self.assertEqual(value.snapshot(), before)
                    self.assertEqual(value._sources, sources_before)

    def test_operation_exit_close_never_restores_previous_sources(self):
        for with_previous in (False, True):
            with self.subTest(previous=with_previous):
                value, _, _ = self.cached_value(with_previous)
                snapshot = value.snapshot

                def close_after_publication():
                    private = snapshot()
                    value.close()
                    return private

                with mock.patch.object(plugin, '_discover', return_value=[]), \
                        mock.patch.object(value, 'snapshot', side_effect=close_after_publication):
                    with self.assertRaises(plugin.MediaSortError) as caught:
                        value.probe(current=lambda: True)
                self.assertEqual(caught.exception.code, 'authorization_expired')
                self.assertEqual(value.snapshot()['diagnostics']['status'], 'unknown')
                self.assertEqual(value._sources, {})


class MediaSortDatabaseStageDiagnosticsTests(unittest.TestCase):
    def test_connect_recorder_preserves_uri_keyword_and_read_only_sqlite(self):
        # Exercise the same recorder used by the Linux chain before _operation
        # can redact a test-hook TypeError. Only URI location/FD enumeration are
        # synthetic here; real no-follow and SQLite FD proof remain Linux-only.
        from test_media_sort_plugin import initialize

        directory = tempfile.TemporaryDirectory(prefix='r1-media-diag-uri-')
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / 'scraper.db'
        connect = plugin.sqlite3.connect
        with connect(database) as writer:
            initialize(writer)
        writer.close()
        opened = plugin._Opened.__new__(plugin._Opened)
        opened.directory = 9001
        opened.main_identity = plugin._stable(database.stat())
        opened.guard = mock.Mock()
        opened.current = mock.Mock()
        connections = []
        database_uri = database.as_uri()
        with mock.patch.object(plugin, 'quote', return_value=database_uri[len('file:'):]) as quote, \
                mock.patch.object(plugin, '_fd_states', side_effect=[{}, {9002: opened.main_identity}]), \
                mock.patch.object(plugin.sqlite3, 'connect',
                                  side_effect=record_connections(connect, connections)):
            con = opened.connect()
            try:
                self.assertEqual(con.execute('PRAGMA query_only').fetchone()[0], 1)
                self.assertEqual(con.execute('SELECT count(*) FROM metadata').fetchone()[0], 1)
                with self.assertRaises(plugin.sqlite3.OperationalError):
                    con.execute('UPDATE metadata SET timestamp=123')
            finally:
                con.close()
        quote.assert_called_once_with('/proc/self/fd/9001/scraper.db', safe='/')
        self.assertEqual(connections, [(database_uri + '?mode=ro', True)])
        self.assertEqual(opened.sqlite_fd, 9002)

    """Exercise real current/schema calls with precisely failing dependencies."""

    def database(self, fail_statement):
        from test_media_sort_plugin import initialize

        con = plugin.sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        initialize(con)
        primitive = plugin.sqlite3.OperationalError(SECRET)
        proxy = mock.Mock(spec=plugin.sqlite3.Connection, wraps=con)

        def execute(sql, *args):
            if fail_statement(sql):
                raise primitive
            return con.execute(sql, *args)

        proxy.execute.side_effect = execute
        return proxy, primitive

    def test_current_alias_and_original_chain_errors_remain_db_chain_inside_connect(self):
        for alias in (False, True):
            with self.subTest(alias=alias):
                observer = plugin._Diagnostics().process(41)
                opened = plugin._Opened.__new__(plugin._Opened)
                process = (41, 123, 'not-opened', (), (), None,
                           'alias/scraper.db', 'not-used')
                opened.root = 'not-opened'
                opened.source = plugin._Source('data/scraper.db', (process,) if alias else ())
                opened.guard = mock.Mock()
                opened.nodes, opened.main_identity = [], (7, 9001)
                primitive = PermissionError(SECRET)
                token = plugin._OBSERVER.set(observer)
                try:
                    with mock.patch.object(plugin, '_process') as revalidate, \
                            mock.patch.object(plugin, '_OpenedChain', side_effect=primitive) as chain, \
                            mock.patch.object(plugin.sqlite3, 'connect') as connect:
                        with self.assertRaises(PermissionError) as caught:
                            opened.connect()
                        connect.assert_not_called()
                        chain.assert_called_once_with(
                            'not-opened', 'alias/scraper.db' if alias else 'data/scraper.db')
                        if alias:
                            revalidate.assert_called_once_with(
                                'not-opened', 41, opened.guard, expected=process)
                        else:
                            revalidate.assert_not_called()
                finally:
                    plugin._OBSERVER.reset(token)
                self.assertIs(caught.exception, primitive)
                self.assertEqual(primitive._media_sort_stage, 'db_chain')
                stages = observer.value['stages']
                self.assertEqual(stages['db_chain']['status'], 'failed')
                self.assertNotEqual(stages['sqlite_fd']['status'], 'failed')
                self.assertEqual(stages['schema']['status'], 'not_run')
                self.assertNotIn(SECRET, json.dumps(observer.value))

    def test_sqlite_master_definitions_error_has_schema_failure_after_successful_json1(self):
        con, primitive = self.database(
            lambda sql: sql.startswith('SELECT type,name,tbl_name,sql FROM sqlite_master '))
        observer = plugin._Diagnostics().process(41)
        token = plugin._OBSERVER.set(observer)
        try:
            with self.assertRaises(plugin.sqlite3.OperationalError) as caught:
                plugin._schema(con)
        finally:
            plugin._OBSERVER.reset(token)
        self.assertIs(caught.exception, primitive)
        self.assertEqual(primitive._media_sort_stage, 'schema')
        stages = observer.value['stages']
        self.assertEqual(stages['schema']['status'], 'failed')
        self.assertEqual(stages['json1']['status'], 'passed')
        self.assertEqual(stages['artifacts']['status'], 'not_run')
        self.assertNotIn(SECRET, json.dumps(observer.value))

    def test_metadata_count_error_cannot_leave_all_sql_stages_successful(self):
        con, primitive = self.database(lambda sql: sql == 'SELECT count(*) FROM metadata')
        value = plugin.MediaSortPlugin('diagnostic-state-must-not-be-created')
        self.addCleanup(value.close)
        opened = SimpleNamespace(source=plugin._Source('data/scraper.db', ()),
                                 nodes=[], main_identity=(7, 9001))
        observer = plugin._Diagnostics().process(41)
        token = plugin._OBSERVER.set(observer)
        try:
            with self.assertRaises(plugin.sqlite3.OperationalError) as caught:
                value._describe(opened, con)
        finally:
            plugin._OBSERVER.reset(token)
        self.assertIs(caught.exception, primitive)
        self.assertEqual(primitive._media_sort_stage, 'schema')
        stages = observer.value['stages']
        self.assertEqual(stages['schema']['status'], 'failed')
        self.assertEqual(stages['json1']['status'], 'passed')
        self.assertEqual(stages['artifacts']['status'], 'passed')
        self.assertNotIn(SECRET, json.dumps(observer.value))


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_NOFOLLOW'),
                     'Linux real proc-style links, no-follow and SQLite FD verification required')
class MediaSortDiagnosticLinuxChainTests(unittest.TestCase):
    """Real proc-shaped links and production no-follow/SQLite file handles."""

    def setUp(self):
        # Import only the independent schema initializer, not another TestCase:
        # importing TestCase classes would silently run the original suite twice.
        from test_media_sort_plugin import initialize

        directory = tempfile.TemporaryDirectory(prefix='r1-media-diag-real-fds-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        local_ns = self.root / 'proc/self/ns'
        local_ns.mkdir(parents=True)
        self.folder = self.root / 'proc/41'
        (self.folder / 'ns').mkdir(parents=True)
        for kind in ('mnt', 'net', 'pid'):
            (local_ns / kind).write_bytes(kind.encode('ascii'))
            os.link(local_ns / kind, self.folder / 'ns' / kind)
        (self.folder / 'comm').write_bytes(b'scraper\n')
        write_stat(self.folder)
        (self.folder / 'status').write_bytes(b'Uid:\t0\t0\t0\t0\n')
        self.binary = self.root / 'fixture-executable'
        self.binary.write_bytes(b'TEST')
        self.binary.chmod(0o600)
        (self.folder / 'exe').symlink_to(self.binary)
        (self.folder / 'root').symlink_to(self.root, target_is_directory=True)
        (self.folder / 'cmdline').write_bytes(b'/audited/scraper\0-d\0/data\0')
        self.database = self.root / 'data/scraper.db'
        self.database.parent.mkdir(mode=0o700)
        con = plugin.sqlite3.connect(self.database)
        try:
            initialize(con)
        finally:
            con.close()
        self.database.chmod(0o600)
        uid = os.getuid()

        def fixture_owned(info, mask=0o022):
            if info.st_uid != uid or info.st_mode & mask:
                raise plugin.MediaSortError('unavailable')

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(plugin, '_SHA', hashlib.sha256(b'TEST').hexdigest()))
        self.stack.enter_context(mock.patch.object(plugin, '_ELF_SIZE', 4))
        self.stack.enter_context(mock.patch.object(plugin, '_owned', side_effect=fixture_owned))
        self.value = plugin.MediaSortPlugin(self.root, root=self.root)
        self.addCleanup(self.value.close)

    def probe(self):
        return self.value.probe(current=lambda: True)

    def stages(self, state):
        rows = state['diagnostics']['processes']
        self.assertEqual(len(rows), 1)
        return rows[0]['stages']

    def test_full_production_chain_has_real_database_handles_and_complete_publication(self):
        describe = self.value._describe
        connections = []
        connect = plugin.sqlite3.connect

        def assert_read_only(opened, con):
            self.assertEqual(con.execute('PRAGMA query_only').fetchone()[0], 1)
            self.assertTrue(opened.sqlite_fd >= 0)
            self.assertEqual(plugin._stable(os.fstat(opened.sqlite_fd)), opened.main_identity)
            return describe(opened, con)

        with mock.patch.object(plugin.sqlite3, 'connect',
                               side_effect=record_connections(connect, connections)), \
                mock.patch.object(self.value, '_describe', side_effect=assert_read_only):
            state = self.probe()
        self.assertTrue(connections)
        self.assertTrue(all(uri.endswith('?mode=ro') and is_uri
                            for uri, is_uri in connections))
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['candidates'][0]['state'], 'disabled')
        self.assertEqual(state['diagnostics']['status'], 'complete')
        self.assertEqual(state['diagnostics']['publication']['status'], 'passed')
        stages = self.stages(state)
        for stage in PROCESS_STAGES + DATABASE_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stages[stage]['status'], 'passed', stages)
        self.assertNotIn(str(self.binary), json.dumps(state['diagnostics']))

    def test_real_vendor_0777_probe_completes_hash_database_and_publication(self):
        self.binary.chmod(0o777)
        before = self.database.read_bytes()
        state = self.probe()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['candidates'][0]['state'], 'disabled')
        self.assertEqual(state['diagnostics']['publication']['status'], 'passed')
        stages = self.stages(state)
        self.assertEqual(stages['exe_file']['observations']['mode'], 0o777)
        self.assertTrue(stages['elf']['observations']['hash_matches'])
        for stage in PROCESS_STAGES + DATABASE_STAGES:
            self.assertEqual(stages[stage]['status'], 'passed', stages)
        self.assertEqual(self.database.read_bytes(), before)

    def test_real_vendor_0777_bad_hash_never_opens_database_dependencies(self):
        self.binary.chmod(0o777)
        self.binary.write_bytes(b'FAIL')
        with mock.patch.object(plugin, '_Opened') as opened:
            state = self.probe()
        opened.assert_not_called()
        self.assertEqual(state['status'], 'unavailable')
        self.assertEqual(state['candidates'], [])
        stages = self.stages(state)
        self.assertEqual(stages['elf']['status'], 'failed')
        self.assertEqual(stages['elf']['reason_code'], 'elf_hash_mismatch')
        self.assertEqual(stages['cmdline']['status'], 'not_run')
        for stage in DATABASE_STAGES:
            self.assertEqual(stages[stage]['status'], 'not_run', stages)

    def test_no_follow_main_link_failure_leaves_dependent_database_stages_not_run(self):
        original = self.database.with_name('original.sqlite3')
        self.database.rename(original)
        self.database.symlink_to(original)
        state = self.probe()
        self.assertEqual(state['status'], 'unavailable')
        self.assertEqual(state['candidates'][0]['state'], 'unavailable')
        stages = self.stages(state)
        self.assertEqual(stages['db_chain']['status'], 'failed')
        for stage in ('sidecars', 'sqlite_fd', 'schema', 'json1', 'artifacts'):
            self.assertEqual(stages[stage]['status'], 'not_run', stages)

    def test_sidecar_link_failure_is_not_misreported_as_sqlite_schema_failure(self):
        self.database.with_name('scraper.db-journal').symlink_to(self.database)
        state = self.probe()
        stages = self.stages(state)
        self.assertEqual(stages['db_chain']['status'], 'passed')
        self.assertEqual(stages['sidecars']['status'], 'failed')
        for stage in ('sqlite_fd', 'schema', 'json1', 'artifacts'):
            self.assertEqual(stages[stage]['status'], 'not_run', stages)

    def test_real_sqlite_fd_gate_failure_cannot_report_a_schema_check(self):
        with mock.patch.object(plugin, '_fd_states', return_value={}):
            state = self.probe()
        stages = self.stages(state)
        self.assertEqual(stages['sqlite_fd']['status'], 'failed')
        for stage in ('schema', 'json1', 'artifacts'):
            self.assertEqual(stages[stage]['status'], 'not_run', stages)

    def test_real_schema_rejection_does_not_label_json_or_artifacts_as_failed(self):
        con = plugin.sqlite3.connect(self.database)
        try:
            con.execute('ALTER TABLE metadata ADD COLUMN unrelated TEXT')
            con.commit()
        finally:
            con.close()
        state = self.probe()
        stages = self.stages(state)
        self.assertEqual(stages['schema']['status'], 'failed')
        for stage in ('json1', 'artifacts'):
            self.assertEqual(stages[stage]['status'], 'not_run', stages)

    def test_json1_failure_is_distinguished_after_a_valid_real_schema(self):
        original = plugin._Opened.connect

        class WithoutJson1:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if sql.startswith("SELECT json_valid('[]')"):
                    return SimpleNamespace(fetchone=lambda: (0, None, None))
                return self.connection.execute(sql, *args)

            def __getattr__(self, key):
                return getattr(self.connection, key)

        def connect(opened, *args, **kwargs):
            return WithoutJson1(original(opened, *args, **kwargs))

        with mock.patch.object(plugin._Opened, 'connect', connect):
            state = self.probe()
        stages = self.stages(state)
        self.assertEqual(stages['schema']['status'], 'passed')
        self.assertEqual(stages['json1']['status'], 'failed')
        self.assertEqual(stages['artifacts']['status'], 'not_run')

    def test_unknown_original_artifact_is_reported_as_conflict_without_any_write(self):
        con = plugin.sqlite3.connect(self.database)
        try:
            con.execute('CREATE TABLE metadata_path_guard(arbitrary TEXT)')
            con.commit()
        finally:
            con.close()
        before = self.database.read_bytes()
        state = self.probe()
        self.assertEqual(state['candidates'][0]['state'], 'conflict')
        stages = self.stages(state)
        self.assertEqual(stages['schema']['status'], 'passed')
        self.assertEqual(stages['json1']['status'], 'passed')
        self.assertEqual(stages['artifacts']['status'], 'passed')
        self.assertEqual(stages['artifacts']['observations']['artifact_state'], 'conflict')
        self.assertEqual(self.database.read_bytes(), before)

    def test_real_wal_and_query_only_probe_preserve_media_rows_and_rules(self):
        writer = plugin.sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE metadata SET timestamp=123')
        writer.commit()
        state = self.probe()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(self.stages(state)['sidecars']['status'], 'passed')
        self.assertEqual(self.stages(state)['sqlite_fd']['status'], 'passed')
        self.assertEqual(writer.execute('SELECT timestamp FROM metadata').fetchone()[0], 123)
        self.assertEqual(plugin.rules.owned_artifacts(writer), 'disabled')

    def test_database_replacement_after_validation_cannot_publish_ready(self):
        original = self.value._describe

        def replace(opened, con, *args, **kwargs):
            row = original(opened, con, *args, **kwargs)
            self.database.rename(self.database.with_name('old.sqlite3'))
            self.database.write_bytes(b'not a database')
            return row

        with mock.patch.object(self.value, '_describe', side_effect=replace):
            state = self.probe()
        self.assertEqual(state['status'], 'unavailable')
        self.assertFalse(any(row.get('can_apply') for row in state['candidates']))
        self.assertFalse(any(row.get('can_revert') for row in state['candidates']))

    def test_owner_revocation_after_describe_discards_full_private_chain(self):
        original = self.value._describe
        authorized = [True]

        def revoke(opened, con, *args, **kwargs):
            row = original(opened, con, *args, **kwargs)
            authorized[0] = False
            return row

        with mock.patch.object(self.value, '_describe', side_effect=revoke):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.value.probe(current=lambda: authorized[0])
        self.assertEqual(error.exception.code, 'authorization_expired')
        self.assertEqual(self.value.snapshot()['diagnostics']['status'], 'unknown')
        self.assertEqual(self.value.snapshot()['diagnostics']['processes'], [])

    def test_pid_replacement_after_commit_is_rejected_by_publication_source_recheck(self):
        original = plugin._Opened.connect
        folder = self.folder

        class ReplaceAfterCommit:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                result = self.connection.execute(sql, *args)
                if sql == 'COMMIT':
                    write_stat(folder, start=124)
                return result

            def __getattr__(self, key):
                return getattr(self.connection, key)

        def connect(opened, *args, **kwargs):
            return ReplaceAfterCommit(original(opened, *args, **kwargs))

        with mock.patch.object(plugin._Opened, 'connect', connect):
            state = self.probe()
        self.assertEqual(state['status'], 'unavailable')
        self.assertEqual(state['candidates'], [])
        self.assertEqual(state['diagnostics']['publication']['status'], 'failed')
        record = state['diagnostics']['processes'][0]
        self.assertEqual(record['status'], 'identity_changed')
        self.assertFalse(any(stage['status'] == 'passed' and stage['observations']
                             for stage in record['stages'].values()), record)


if __name__ == '__main__':
    unittest.main()
