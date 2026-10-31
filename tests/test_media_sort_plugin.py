"""Owner lifecycle and real synthetic SQLite fixtures, never the NAS sample.

Linux filesystem tests normalize the fixture UID only. File identities,
no-follow opens, SQLite descriptors, WAL and transactions remain real; this
is not proof of the factory ELF or live NAS process identity.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import media_sort_plugin as plugin


METADATA = '''CREATE TABLE metadata (
id INTEGER,name TEXT,initials TEXT,path TEXT,lid INTEGER,poster TEXT,type INTEGER,
vote REAL,timestamp INTEGER,genres TEXT,country TEXT,year TEXT,resolution INTEGER,
dynamic INTEGER,ids TEXT,tag TEXT,season INTEGER,crew TEXT,num INTEGER,sname TEXT,
backup1 TEXT,backup2 TEXT,backup3 TEXT,backup4 TEXT,PRIMARY KEY(id,lid,type))'''
LIBRARY = '''CREATE TABLE library (
lid INTEGER PRIMARY KEY,name TEXT,language TEXT,posters TEXT,sources TEXT,members TEXT,
auto_subtitle INTEGER,tv_available INTEGER,timestamp INTEGER,ctime INTEGER,
backup1 TEXT,backup2 TEXT,backup3 TEXT,backup4 TEXT)'''


def initialize(con):
    con.execute(METADATA)
    con.execute(LIBRARY)
    con.execute("INSERT INTO metadata(id,lid,type,path,timestamp) VALUES(1,1,2,'[\"synthetic-a\"]',1)")
    con.commit()


class MediaSortCacheAndSchemaTests(unittest.TestCase):
    def test_constructor_and_snapshot_do_not_probe_or_open_database(self):
        with mock.patch.object(plugin, '_discover', side_effect=AssertionError('unexpected discovery')), \
                mock.patch.object(plugin.sqlite3, 'connect', side_effect=AssertionError('unexpected SQLite')):
            value = plugin.MediaSortPlugin('not-created')
            cached = value.snapshot()
            self.assertEqual(cached['status'], 'unknown')
            self.assertEqual(cached['candidates'], [])
            cached['candidates'].append({'arbitrary': True})
            self.assertEqual(value.snapshot()['candidates'], [])

    def test_close_and_revocation_never_discover(self):
        value = plugin.MediaSortPlugin('not-created')
        with mock.patch.object(plugin, '_discover') as discovery:
            for close in (False, True):
                if close:
                    value.close()
                with self.assertRaises(plugin.MediaSortError) as error:
                    value.probe(current=lambda: False if not close else True)
                self.assertEqual(error.exception.code, 'authorization_expired')
            discovery.assert_not_called()

    def test_busy_and_confirmation_are_explicit(self):
        value = plugin.MediaSortPlugin('not-created')
        with self.assertRaises(plugin.MediaSortError) as error:
            value.apply('a' * 32, 'b' * 64, confirmed=False, current=lambda: True)
        self.assertEqual(error.exception.code, 'confirmation_required')
        value._operation_lock.acquire()
        try:
            with self.assertRaises(plugin.MediaSortError) as error:
                value.probe(current=lambda: True)
            self.assertEqual(error.exception.code, 'busy')
        finally:
            value._operation_lock.release()

    def test_operation_error_does_not_leak_upstream_text(self):
        value = plugin.MediaSortPlugin('not-created')
        with mock.patch.object(plugin, '_discover', side_effect=OSError('secret-native-path')):
            with self.assertRaises(plugin.MediaSortError) as error:
                value.probe(current=lambda: True)
        self.assertNotIn('secret', str(error.exception))
        self.assertFalse(value.snapshot()['busy'])

    def test_revocation_during_probe_drops_new_results(self):
        value = plugin.MediaSortPlugin('not-created')
        authorized = [True]
        def revoke(*unused):
            authorized[0] = False
            return []
        with mock.patch.object(plugin, '_discover', side_effect=revoke):
            with self.assertRaises(plugin.MediaSortError):
                value.probe(current=lambda: authorized[0])
        self.assertEqual(value.snapshot()['status'], 'unknown')

    def test_exact_schema_accepts_factory_contract_and_rejects_pk_and_affinity_changes(self):
        for metadata in (METADATA, METADATA.replace('PRIMARY KEY(id,lid,type)', 'PRIMARY KEY(id,lid)'),
                         METADATA.replace('timestamp INTEGER', 'timestamp TEXT')):
            with self.subTest(metadata=metadata):
                con = sqlite3.connect(':memory:')
                self.addCleanup(con.close)
                con.execute(metadata)
                con.execute(LIBRARY)
                if metadata == METADATA:
                    self.assertTrue(plugin._schema(con))
                else:
                    with self.assertRaises(plugin.MediaSortError):
                        plugin._schema(con)

    def test_schema_rejects_extra_constraints_and_unique_indexes(self):
        for extra in ('CHECK(timestamp>=0)', 'UNIQUE(tag)'):
            with self.subTest(extra=extra), closing(sqlite3.connect(':memory:')) as con, con:
                con.execute(METADATA[:-1] + ',' + extra + ')')
                con.execute(LIBRARY)
                with self.assertRaises(plugin.MediaSortError):
                    plugin._schema(con)
        with closing(sqlite3.connect(':memory:')) as con, con:
            initialize(con)
            con.execute('CREATE UNIQUE INDEX unrelated_unique ON metadata(tag)')
            with self.assertRaises(plugin.MediaSortError):
                plugin._schema(con)

    def test_regular_file_permission_contract_is_fail_closed(self):
        from types import SimpleNamespace
        good = dict(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=0)
        plugin._regular(SimpleNamespace(**good))
        for change in ({'st_uid': 1000}, {'st_nlink': 2},
                       {'st_mode': stat.S_IFREG | 0o666},
                       {'st_mode': stat.S_IFREG | 0o777},
                       {'st_mode': stat.S_IFREG | 0o775}, {'st_mode': stat.S_IFLNK | 0o600}):
            with self.subTest(change=change), self.assertRaises(plugin.MediaSortError):
                plugin._regular(SimpleNamespace(**{**good, **change}))

    def test_private_backup_permission_contract_has_no_scraper_exception(self):
        from types import SimpleNamespace
        for mode in (0o755, 0o775, 0o777):
            with self.subTest(mode=oct(mode)), self.assertRaises(plugin.MediaSortError):
                plugin._owned(SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=0), 0o077)


class MediaSortPortableTransactionTests(unittest.TestCase):
    """Real SQLite transactions/WAL; Linux opening and backup ownership are seams."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-media-transaction-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'data/scraper.db'
        self.database.parent.mkdir()
        con = sqlite3.connect(self.database)
        initialize(con)
        con.close()
        self.source = plugin._Source('data/scraper.db', ())
        self.value = plugin.MediaSortPlugin(self.root, root=self.root)
        self.addCleanup(self.value.close)
        self.owner = [True]
        fixture = self

        class OpenedFixture:
            def __init__(self, root, source, guard):
                self.root, self.source, self.guard = root, source, guard
                self.path = Path(root) / source.relative
                self.main_identity = plugin._stable(self.path.stat())
                self.nodes = []

            def current(self):
                self.guard()
                if self.path.is_symlink() or plugin._stable(self.path.stat()) != self.main_identity:
                    raise plugin.MediaSortError('unavailable')

            def connect(self, write=False):
                self.current()
                return sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                       uri=True, isolation_level=None, timeout=0.15)

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                pass

        def backup(opened, guard):
            guard()
            destination = fixture.root / ('synthetic-backup-' + str(len(list(fixture.root.glob('*.sqlite3'))))
                                          + '.sqlite3')
            source = opened.connect()
            target = sqlite3.connect(destination)
            try:
                source.backup(target, pages=64, progress=lambda *a: guard())
            finally:
                target.close()
                source.close()
            return str(destination)

        for patcher in (mock.patch.object(plugin, '_Opened', OpenedFixture),
                        mock.patch.object(plugin, '_discover', return_value=[self.source]),
                        mock.patch.object(self.value, '_backup', side_effect=backup)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def candidate(self):
        return self.value.probe(current=lambda: self.owner[0])['candidates'][0]

    def apply(self, row):
        return self.value.apply(row['id'], row['version'], confirmed=True,
                                current=lambda: self.owner[0])

    def test_real_wal_backup_and_revert_preserve_new_rows(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE metadata SET timestamp=123')
        writer.commit()
        row = self.apply(self.candidate())['candidates'][0]
        backup = sqlite3.connect(row['last_backup_path'])
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute('SELECT timestamp FROM metadata').fetchone()[0], 123)
        writer.execute("INSERT INTO metadata(id,lid,type,path,timestamp) VALUES(2,1,2,'[]',2)")
        writer.commit()
        result = self.value.revert(row['id'], row['version'], confirmed=True,
                                    current=lambda: self.owner[0])
        self.assertEqual(result['candidates'][0]['state'], 'disabled')
        self.assertEqual(writer.execute('SELECT count(*) FROM metadata').fetchone()[0], 2)

    def test_install_failure_rolls_back_and_does_not_report_enabled(self):
        row = self.candidate()
        original = plugin.rules.install
        def fail(con, **kwargs):
            original(con, **kwargs)
            raise RuntimeError('synthetic-failure')
        with mock.patch.object(plugin.rules, 'install', side_effect=fail):
            with self.assertRaises(plugin.MediaSortError):
                self.apply(row)
        self.assertEqual(self.candidate()['state'], 'disabled')

    def test_close_during_install_revokes_and_rolls_back(self):
        row = self.candidate()
        original = plugin.rules.install
        def revoke(con, **kwargs):
            original(con, **kwargs)
            self.value.close()
        with mock.patch.object(plugin.rules, 'install', side_effect=revoke):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.apply(row)
        self.assertEqual(error.exception.code, 'authorization_expired')
        con = sqlite3.connect(self.database)
        self.addCleanup(con.close)
        self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')
        self.assertEqual(self.value.snapshot()['candidates'], [])

    def install_original_rule(self):
        con = sqlite3.connect(self.database)
        con.execute(plugin.rules.CREATE_GUARD_SQL)
        con.execute(plugin.rules.SEED_SQL)
        con.execute(plugin.rules.INSERT_TRIGGER_SQL)
        con.commit()
        con.close()

    def test_existing_original_rule_is_enabled_idempotent_and_revertible(self):
        self.install_original_rule()
        row = self.candidate()
        self.assertEqual(row['state'], 'enabled')
        self.assertEqual(row['rule_version'], 1)
        self.assertFalse(row['can_apply'])
        self.assertTrue(row['can_revert'])
        before = sqlite3.connect(self.database)
        try:
            guard = before.execute(
                'SELECT path FROM metadata_path_guard').fetchone()[0]
        finally:
            before.close()
        after = self.apply(row)['candidates'][0]
        self.assertEqual(after['state'], 'enabled')
        self.assertIsNone(after['last_backup_path'])
        with closing(sqlite3.connect(self.database)) as con, con:
            self.assertEqual(con.execute(
                'SELECT path FROM metadata_path_guard').fetchone()[0], guard)

    def test_existing_original_rule_can_be_removed_without_restoring_media(self):
        self.install_original_rule()
        con = sqlite3.connect(self.database)
        con.execute("INSERT INTO metadata(id,lid,type,path,timestamp) VALUES(2,1,2,'[]',2)")
        con.commit()
        con.close()
        row = self.candidate()
        result = self.value.revert(row['id'], row['version'], confirmed=True,
                                   current=lambda: self.owner[0])
        self.assertEqual(result['candidates'][0]['state'], 'disabled')
        with closing(sqlite3.connect(self.database)) as con, con:
            self.assertEqual(con.execute('SELECT count(*) FROM metadata').fetchone()[0], 2)

    def test_changed_schema_version_and_unknown_trigger_are_not_modified(self):
        row = self.candidate()
        con = sqlite3.connect(self.database)
        con.execute('CREATE TRIGGER unknown_hook AFTER INSERT ON metadata BEGIN SELECT 1; END')
        con.close()
        with self.assertRaises(plugin.MediaSortError) as error:
            self.apply(row)
        self.assertEqual(error.exception.code, 'stale_candidate')
        self.assertEqual(self.candidate()['state'], 'conflict')


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_NOFOLLOW'),
                     'Linux actual directory/main/SQLite descriptors required')
class MediaSortFilesystemTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='r1-media-plugin-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.database = self.root / 'data/scraper.db'
        self.database.parent.mkdir(mode=0o700)
        con = sqlite3.connect(self.database)
        initialize(con)
        con.close()
        self.database.chmod(0o600)
        self.source = self.source_for('data/scraper.db')
        self.owner = [True]
        self.current = lambda: self.owner[0]
        self.value = plugin.MediaSortPlugin(self.state, root=self.root)
        self.addCleanup(self.value.close)
        def owned(value, mask=0o022):
            if value.st_uid != os.getuid() or value.st_mode & mask:
                raise plugin.MediaSortError('unavailable')
        patcher = mock.patch.object(plugin, '_owned', side_effect=owned)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(plugin, '_process', side_effect=lambda *a, **kw: kw['expected'])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(plugin, '_discover', return_value=[self.source])
        self.discovery = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def source_for(relative, pid=123):
        return plugin._Source(relative, ((pid, 40, '/audited/scraper', ('binary-fixture',),
                                          ('namespace-fixture',), None, relative, 'argv-fixture'),))

    def candidate(self):
        result = self.value.probe(current=self.current)
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['busy'])
        self.assertEqual(len(result['candidates']), 1)
        return result['candidates'][0]

    def apply(self, row):
        return self.value.apply(row['id'], row['version'], confirmed=True, current=self.current)

    def test_apply_real_delete_journal_and_revert_preserve_subsequent_rows(self):
        before = self.candidate()
        self.assertEqual(before['state'], 'disabled')
        after = self.apply(before)['candidates'][0]
        self.assertEqual(after['state'], 'enabled')
        backup = Path(after['last_backup_path'])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        con = sqlite3.connect(self.database)
        con.execute("INSERT INTO metadata(id,lid,type,path,timestamp) VALUES(2,1,2,'[]',2)")
        con.commit()
        con.close()
        result = self.value.revert(after['id'], after['version'], confirmed=True, current=self.current)
        self.assertEqual(result['candidates'][0]['state'], 'disabled')
        con = sqlite3.connect(self.database)
        self.addCleanup(con.close)
        self.assertEqual(con.execute('SELECT count(*) FROM metadata').fetchone()[0], 2)

    def test_online_backup_includes_live_committed_wal(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE metadata SET path='[\"synthetic-a\",\"synthetic-b\"]'")
        writer.commit()
        before = self.candidate()
        after = self.apply(before)['candidates'][0]
        backup = sqlite3.connect(after['last_backup_path'])
        self.addCleanup(backup.close)
        self.assertEqual(backup.execute('SELECT path FROM metadata').fetchone()[0],
                         '["synthetic-a","synthetic-b"]')

    def test_writable_database_and_sidecars_do_not_inherit_executable_exception(self):
        for mode in (0o777, 0o775):
            with self.subTest(kind='database', mode=oct(mode)):
                self.database.chmod(mode)
                result = self.value.probe(current=self.current)
                self.assertEqual(result['status'], 'unavailable')
                self.assertFalse(any(row['can_apply'] for row in result['candidates']))
                self.database.chmod(0o600)
            for suffix in ('-wal', '-shm', '-journal'):
                with self.subTest(kind=suffix, mode=oct(mode)):
                    sidecar = Path(str(self.database) + suffix)
                    sidecar.write_bytes(b'')
                    sidecar.chmod(mode)
                    try:
                        result = self.value.probe(current=self.current)
                        self.assertEqual(result['status'], 'unavailable')
                        self.assertFalse(any(row['can_apply'] for row in result['candidates']))
                    finally:
                        sidecar.unlink()

    def test_writable_private_backup_directory_aborts_before_rule_install(self):
        row = self.candidate()
        directory = self.state / 'media-sort-backups'
        directory.mkdir(mode=0o700)
        directory.chmod(0o777)
        with mock.patch.object(plugin.rules, 'install') as install:
            with self.assertRaises(plugin.MediaSortError) as error:
                self.apply(row)
            self.assertEqual(error.exception.code, 'backup_failed')
            install.assert_not_called()
        self.assertEqual(list(directory.iterdir()), [])
        with closing(sqlite3.connect(self.database)) as con:
            self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')

    def test_failed_install_rolls_back_all_plugin_objects(self):
        row = self.candidate()
        original = plugin.rules.install
        def fail(con, **kwargs):
            original(con, **kwargs)
            raise RuntimeError('injected-after-install')
        with mock.patch.object(plugin.rules, 'install', side_effect=fail):
            with self.assertRaises(plugin.MediaSortError):
                self.apply(row)
        con = sqlite3.connect(self.database)
        self.addCleanup(con.close)
        self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')

    def test_revoked_owner_rolls_back_and_close_does_not_wait_for_operation_lock(self):
        row = self.candidate()
        original = plugin.rules.install
        def revoke(con, **kwargs):
            original(con, **kwargs)
            self.value.close()
        with mock.patch.object(plugin.rules, 'install', side_effect=revoke):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.apply(row)
        self.assertEqual(error.exception.code, 'authorization_expired')
        con = sqlite3.connect(self.database)
        self.addCleanup(con.close)
        self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')
        self.assertEqual(self.value.snapshot()['candidates'], [])

    def test_stale_inode_or_symlink_replacement_does_not_modify_replacement(self):
        row = self.candidate()
        original = self.database.with_suffix('.original')
        self.database.rename(original)
        self.database.symlink_to(original)
        with self.assertRaises(plugin.MediaSortError):
            self.apply(row)
        con = sqlite3.connect(original)
        self.addCleanup(con.close)
        self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')

    def test_multiple_database_candidates_are_not_selected_implicitly(self):
        other = self.root / 'second/scraper.db'
        other.parent.mkdir(mode=0o700)
        con = sqlite3.connect(other)
        initialize(con)
        con.close()
        other.chmod(0o600)
        self.discovery.return_value = [self.source, self.source_for('second/scraper.db', 124)]
        result = self.value.probe(current=self.current)
        self.assertEqual(len(result['candidates']), 2)
        for row in result['candidates']:
            self.assertEqual(row['state'], 'disabled')

    def test_same_database_from_multiple_verified_processes_is_inode_deduplicated(self):
        self.discovery.return_value = [self.source, self.source_for('data/scraper.db', 124)]
        result = self.value.probe(current=self.current)
        self.assertEqual(len(result['candidates']), 1)
        row = result['candidates'][0]
        self.assertEqual(len(self.value._sources[row['id']].processes), 2)

    def test_backup_directory_replacement_aborts_before_install(self):
        row = self.candidate()
        actual_connect = sqlite3.connect
        changed = []

        def swap_directory(database, *args, **kwargs):
            if str(database).startswith('file:/proc/self/fd/') and '/scraper-' in str(database):
                directory = self.state / 'media-sort-backups'
                directory.rename(self.state / 'detached-private-backups')
                directory.mkdir(mode=0o700)
                changed.append(True)
            return actual_connect(database, *args, **kwargs)

        with mock.patch.object(plugin.sqlite3, 'connect', side_effect=swap_directory):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.apply(row)
        self.assertEqual(error.exception.code, 'backup_failed')
        self.assertEqual(changed, [True])
        with closing(actual_connect(self.database)) as con, con:
            self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')

    def test_backup_target_symlink_replacement_is_not_written(self):
        row = self.candidate()
        decoy = self.root / 'decoy.sqlite3'
        with closing(sqlite3.connect(decoy)) as con, con:
            con.execute('CREATE TABLE untouched(value TEXT)')
            con.execute("INSERT INTO untouched VALUES('synthetic')")
        before = decoy.read_bytes()
        actual_connect = sqlite3.connect
        changed = []

        def swap_target(database, *args, **kwargs):
            if str(database).startswith('file:/proc/self/fd/') and '/scraper-' in str(database):
                directory = self.state / 'media-sort-backups'
                files = list(directory.iterdir())
                self.assertEqual(len(files), 1)
                target = files[0]
                target.rename(directory / 'detached-exclusive.sqlite3')
                target.symlink_to(decoy)
                changed.append(True)
            return actual_connect(database, *args, **kwargs)

        with mock.patch.object(plugin.sqlite3, 'connect', side_effect=swap_target):
            with self.assertRaises(plugin.MediaSortError) as error:
                self.apply(row)
        self.assertEqual(error.exception.code, 'backup_failed')
        self.assertEqual(changed, [True])
        self.assertEqual(decoy.read_bytes(), before)
        with closing(actual_connect(self.database)) as con, con:
            self.assertEqual(plugin.rules.owned_artifacts(con), 'disabled')

    def test_missing_source_is_not_created_and_unknown_trigger_is_preserved(self):
        self.discovery.return_value = [self.source_for('absent/scraper.db')]
        self.assertEqual(self.value.probe(current=self.current)['status'], 'unavailable')
        self.assertFalse((self.root / 'absent').exists())
        self.discovery.return_value = [self.source]
        con = sqlite3.connect(self.database)
        con.execute('CREATE TRIGGER unknown_hook AFTER INSERT ON metadata BEGIN SELECT 1; END')
        con.close()
        row = self.candidate()
        self.assertEqual(row['state'], 'conflict')
        with self.assertRaises(plugin.MediaSortError) as error:
            self.apply(row)
        self.assertEqual(error.exception.code, 'conflict')


if __name__ == '__main__':
    unittest.main()
