"""Real Linux custody/rename/anonymous-copy tests, never NAS-device acceptance."""

import copy
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_mutations as mutations
from api_file_mutation_authority import FileMutationAuthority
from api_file_operations import FileAccessError
from api_file_transfers import FileTransfers, _identity, _version
from api_file_write_backend import FileWriteExecutor
import test_api_file_executor as read_fixture
from test_api_file_executor import ROOT, ROW, principal
from test_api_file_write_backend import IndexFixture, WriteAuthorityFixture


DATA = bytes(range(256)) * 600


class MutationIndexFixture(IndexFixture):
    def preflight_mutation(self, row, source, target, *, operation, current):
        self.preflight(row, source, current=current)
        return (row['id'], source, target, operation)

    def notify_mutation(self, row, source, target, descriptor, *, operation, current, prepared):
        if prepared != self.preflight_mutation(row, source, target, operation=operation, current=current):
            raise FileAccessError('file_changed')
        receipt = (row['id'], source, target, operation, os.fstat(descriptor).st_ino)
        self.notifications.append(receipt)
        return receipt

    def verify_mutation(self, receipt, row, source, target, descriptor, *, operation, current):
        self.preflight(row, source, current=current)
        self.verifications += 1
        if receipt != (row['id'], source, target, operation, os.fstat(descriptor).st_ino):
            raise FileAccessError('file_changed')
        return self.ready


class RecycleFixture:
    """Explicit fixture archive; never evidence of factory recycle schema."""
    def __init__(self, users):
        self.users, self.archives = users, 0
        self.destination = users / 'admin/fixture-recycle'
        self.destination.mkdir()
        self.ready = False

    def preflight(self, row, path, *, current):
        if current() is not True:
            raise FileAccessError('invalid_key')
        return (row['id'], path)

    def archive_captured(self, plan, row, path, descriptor, stage, *, current):
        if plan != self.preflight(row, path, current=current):
            raise FileAccessError('file_changed')
        stage.verify(current)
        parent = os.open(self.destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            mutations._rename_new(stage.fd, 'source', parent, 'archived.bin')
        finally:
            os.close(parent)
        self.archives += 1
        return os.fstat(descriptor).st_ino

    def verify(self, receipt, row, path, descriptor, *, current):
        self.preflight(row, path, current=current)
        if (self.destination / 'archived.bin').stat().st_ino != receipt \
                or os.fstat(descriptor).st_ino != receipt or (self.users / path).exists():
            raise FileAccessError('file_changed')
        return self.ready


class ArgumentTests(unittest.TestCase):
    def test_restore_hashes_apply_only_to_the_plan_inode_or_this_restore_attempt(self):
        first = hashlib.sha256(b'original').hexdigest()
        second = hashlib.sha256(b'published').hexdigest()
        attempt = hashlib.sha256(b'wrong captured object').hexdigest()
        manifest = {'source_identity': [1, 2, 7, 0, 0], 'source_sha256': first,
                    'published_identity': [1, 3, 32768], 'published_sha256': second}
        self.assertEqual(FileMutationAuthority._recorded_restore_hashes(
            manifest, SimpleNamespace(st_dev=1, st_ino=2)), [first])
        self.assertEqual(FileMutationAuthority._recorded_restore_hashes(
            manifest, SimpleNamespace(st_dev=1, st_ino=3)), [second])
        self.assertEqual(FileMutationAuthority._recorded_restore_hashes(
            manifest, SimpleNamespace(st_dev=1, st_ino=4)), [])
        manifest['restore_sha256'] = attempt
        self.assertEqual(FileMutationAuthority._recorded_restore_hashes(
            manifest, SimpleNamespace(st_dev=1, st_ino=4)), [attempt])

    def test_owner_recovery_display_only_exposes_valid_relative_paths_and_known_tools(self):
        self.assertEqual(FileMutationAuthority._recovery_display({
            'restore_path': 'admin/folder/file.bin', 'tool': 'rename_file'}),
            {'path': 'admin/folder/file.bin', 'tool': 'rename_file'})
        for path in ('/drives/data/Users/admin/file', '../admin/file', 'public/file',
                     'admin/.DATA_CACHE', 'admin/.DATA_CACHE/Recycle.db', None):
            self.assertEqual(FileMutationAuthority._recovery_display({
                'restore_path': path, 'tool': 'arbitrary tool'})['path'], None)
        self.assertEqual(FileMutationAuthority._recovery_display({
            'restore_path': 'admin/example/.DATA_CACHE/file', 'tool': 'arbitrary tool'}),
            {'path': 'admin/example/.DATA_CACHE/file', 'tool': 'unknown'})

    def test_old_upload_grant_never_grants_overwrite(self):
        manager = FileTransfers(lambda unused: True)
        self.addCleanup(manager.close)
        writer = FileWriteExecutor(None, transfers=manager, authorizer=lambda unused: True,
                                   authority=WriteAuthorityFixture())
        self.addCleanup(writer.close)
        key = principal()
        key.update(role='readwrite', tool_grants=['prepare_file_upload', 'commit_file_upload'])
        key['file_roots'][0].update(path='admin', access='readwrite')
        result = writer.execute('prepare_file_overwrite', {'root_id': ROOT, 'path': 'admin/a',
            'size_bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest(),
            'idempotency_key': 'overwrite', 'expected_target_version': 'file_old',
            'confirmed': True}, key)
        self.assertEqual(result['error']['code'], 'permission_denied')

    def test_machine_cannot_pass_staging_authority_or_owner_bootstrap_arguments(self):
        manager = FileTransfers(lambda unused: True)
        self.addCleanup(manager.close)
        writer = FileWriteExecutor(None, transfers=manager, authorizer=lambda unused: True)
        self.addCleanup(writer.close)
        key = principal()
        key.update(role='readwrite', tool_grants=['create_directory'])
        key['file_roots'][0].update(path='admin', access='readwrite')
        for arguments in ({'root_id': ROOT, 'path': 'admin/a', 'idempotency_key': 'x',
                           'authority_fd': 4},
                          {'root_id': ROOT, 'path': 'admin/a', 'idempotency_key': 'x',
                           'confirmed': True}):
            self.assertEqual(writer.execute('create_directory', arguments, key)['error']['code'],
                             'invalid_arguments')

    def test_factory_cache_is_not_a_generic_mutation_surface(self):
        manager = FileTransfers(lambda unused: True)
        self.addCleanup(manager.close)
        writer = FileWriteExecutor(None, transfers=manager, authorizer=lambda unused: True)
        self.addCleanup(writer.close)
        key = principal()
        key.update(role='readwrite', tool_grants=['create_directory', 'prepare_file_upload'])
        key['file_roots'][0].update(path='admin', access='readwrite')
        for path in ('admin/.DATA_CACHE', 'admin/.DATA_CACHE/Recycle.db'):
            result = writer.execute('create_directory',
                {'root_id': ROOT, 'path': path, 'idempotency_key': 'cache'}, key)
            self.assertEqual(result['error']['code'], 'unsupported_path_class')
        self.assertEqual(writer._grant(key, 'prepare_file_upload', ROOT,
            'admin/example/.DATA_CACHE/new.bin'), ('admin/example/.DATA_CACHE', 'new.bin'))
        self.assertEqual(writer.mutations._grant(key, 'create_directory', ROOT,
            'admin/example/.DATA_CACHE'), ('admin/example', '.DATA_CACHE'))


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_TMPFILE')
                     and hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'Linux root-owned chains, O_TMPFILE and renameat2 required')
class FileMutationTests(unittest.TestCase):
    def setUp(self):
        read_fixture.OpenedFileTests.setUp(self)
        self.state_dir = self.root / 'toolbox-state'
        self.state_dir.mkdir(mode=0o700)
        self.index = MutationIndexFixture()
        self.owner_authority = FileMutationAuthority(None, root=self.root,
            state_dir=self.state_dir, authority=WriteAuthorityFixture())
        self.manager = FileTransfers(lambda unused: self.valid)
        self.writer = FileWriteExecutor(None, root=self.root, transfers=self.manager,
            authorizer=lambda unused: self.valid, authority=WriteAuthorityFixture(),
            mutation_authority=self.owner_authority, index=self.index,
            capacity=lambda descriptor, remaining: None)
        self.addCleanup(self.manager.close)
        self.addCleanup(self.writer.close)
        self.key = principal()
        self.key.update(role='readwrite', tool_grants=list(mutations.TOOLS) + [
            'prepare_file_upload', 'prepare_file_overwrite', 'commit_file_upload',
            'get_file_operation', 'get_file_transfer', 'cancel_file_transfer'])
        self.key['file_roots'][0].update(path='admin', access='readwrite')
        (self.users / 'admin/source.bin').write_bytes(DATA)

    def initialize(self):
        return self.owner_authority.initialize(ROOT, confirmed=True, current=lambda: True)

    def version(self, path='admin/source.bin'):
        descriptor = os.open(self.users / path, os.O_RDONLY)
        try:
            return _version(_identity(descriptor))
        finally:
            os.close(descriptor)

    def copy_args(self, **changes):
        args = {'source_root_id': ROOT, 'source_path': 'admin/source.bin',
                'target_root_id': ROOT, 'target_path': 'admin/copied.bin',
                'expected_source_version': self.version(changes.get('source_path', 'admin/source.bin')),
                'idempotency_key': 'copy-1'}
        args.update(changes)
        return args

    def rename_args(self, **changes):
        args = {'root_id': ROOT, 'path': 'admin/source.bin', 'new_name': 'renamed.bin',
                'expected_source_version': self.version(), 'idempotency_key': 'rename-1'}
        args.update(changes)
        return args

    def poll(self, value):
        return self.writer.execute('get_file_operation',
            {'operation_id': value['operation']['operation_id']}, self.key)

    def captured_with_old_writer(self):
        """Keep an independent pre-capture writable fd for restore races."""
        self.initialize()
        args = self.rename_args()
        descriptor = os.open(self.users / 'admin/source.bin', os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        before = os.fstat(descriptor)
        (self.users / 'admin/renamed.bin').write_bytes(b'keep existing target')
        result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        return result['operation']['operation_id'], descriptor, before

    @staticmethod
    def rewrite_same_length(descriptor, before, marker):
        changed = marker + DATA[1:]
        if os.pwrite(descriptor, changed, 0) != len(changed):
            raise AssertionError('fixture short write')
        os.utime(descriptor, ns=(before.st_atime_ns, before.st_mtime_ns))
        return changed

    def test_owner_bootstrap_requires_confirmation_and_rejects_unknown_or_writable_parents(self):
        with self.assertRaises(FileAccessError):
            self.owner_authority.initialize(ROOT, confirmed=False, current=lambda: True)
        os.chmod(self.users, 0o777)
        with self.assertRaises(FileAccessError):
            self.initialize()
        self.assertFalse((self.users / '.r1-toolbox-ops').exists())
        os.chmod(self.users, 0o755)
        unknown = self.users / '.r1-toolbox-ops'
        unknown.mkdir(mode=0o700)
        (unknown / 'keep.txt').write_bytes(b'unknown custody')
        with self.assertRaises(FileAccessError):
            self.initialize()
        self.assertEqual((unknown / 'keep.txt').read_bytes(), b'unknown custody')

    def test_owner_catalog_does_not_open_users_and_revocation_blocks_new_stages(self):
        self.initialize()
        with mock.patch.object(self.owner_authority, '_handle', side_effect=AssertionError('disk access')):
            catalog = self.owner_authority.catalog(current=lambda: True)
        self.assertEqual(catalog['authorities'][0]['status'], 'registered')
        self.owner_authority.revoke(ROOT, confirmed=True, current=lambda: True)
        with self.assertRaises(FileAccessError):
            self.owner_authority.open(ROW, 'operation_' + 'a' * 32, current=lambda: True)

    def test_owner_preflight_reports_real_uid_mode_acl_and_never_creates(self):
        os.chmod(self.users, 0o777)
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertFalse(evidence['ready'])
        users = [r for r in evidence['checks'] if r['component'] == 'users_root'][0]
        self.assertEqual(users['mode'], '0777')
        self.assertTrue(users['group_other_writable'])
        self.assertIn(users['access_acl'], ('absent', 'present', 'unavailable'))
        self.assertEqual(evidence['source_binding']['native_owner'], 'verified')
        self.assertFalse((self.users / '.r1-toolbox-ops').exists())

    def test_copy_uses_real_bytes_and_no_clobber_then_independent_index(self):
        args = self.copy_args()
        source_inode = (self.users / args['source_path']).stat().st_ino
        result = self.writer.execute('copy_file', args, self.key)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['operation']['state'], 'pending', result)
        copied = self.users / args['target_path']
        self.assertEqual(copied.read_bytes(), DATA)
        self.assertNotEqual(copied.stat().st_ino, source_inode)
        self.assertEqual((self.users / args['source_path']).stat().st_ino, source_inode)
        self.assertEqual(self.writer.execute('copy_file', args, self.key), result)
        self.assertEqual(len(self.index.notifications), 1)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_copy_conflict_keeps_both_files_and_never_notifies(self):
        args = self.copy_args()
        (self.users / args['target_path']).write_bytes(b'existing destination')
        result = self.writer.execute('copy_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'failed', result)
        self.assertEqual((self.users / args['target_path']).read_bytes(), b'existing destination')
        self.assertEqual((self.users / args['source_path']).read_bytes(), DATA)
        self.assertFalse(self.index.notifications)

    def test_copy_readonly_source_and_write_target_can_poll_completed_operation(self):
        (self.users / 'admin/input').mkdir()
        (self.users / 'admin/output').mkdir()
        (self.users / 'admin/source.bin').rename(self.users / 'admin/input/source.bin')
        self.key['file_roots'] = [
            {'id': ROOT, 'path': 'admin/input', 'access': 'readonly'},
            {'id': ROOT, 'path': 'admin/output', 'access': 'readwrite'}]
        args = self.copy_args(source_path='admin/input/source.bin', target_path='admin/output/copy.bin',
                              expected_source_version=self.version('admin/input/source.bin'))
        result = self.writer.execute('copy_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_revocation_during_copy_never_releases_a_published_object(self):
        args = self.copy_args()
        original = os.pread
        def revoke(fd, count, offset):
            block = original(fd, count, offset)
            self.valid = False
            return block
        with mock.patch.object(mutations.os, 'pread', side_effect=revoke):
            result = self.writer.execute('copy_file', args, self.key)
        self.assertEqual(result['error']['code'], 'invalid_key', result)
        self.assertFalse((self.users / args['target_path']).exists())
        self.assertFalse(self.index.notifications)

    def test_mkdir_uses_trusted_birth_inode_and_waits_for_directory_index(self):
        self.initialize()
        args = {'root_id': ROOT, 'path': 'admin/new-directory', 'idempotency_key': 'mkdir-1'}
        result = self.writer.execute('create_directory', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        inode = (self.users / args['path']).stat().st_ino
        self.assertEqual(self.index.notifications[0][2:], (inode, True))
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_uninitialized_authority_rejects_custody_without_touching_source(self):
        result = self.writer.execute('rename_file', self.rename_args(), self.key)
        self.assertEqual(result['operation']['state'], 'failed', result)
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), DATA)
        self.assertFalse((self.users / 'admin/renamed.bin').exists())

    def test_rename_preserves_inode_and_only_reports_after_native_old_and_new_keys(self):
        self.initialize()
        inode = (self.users / 'admin/source.bin').stat().st_ino
        result = self.writer.execute('rename_file', self.rename_args(), self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertFalse((self.users / 'admin/source.bin').exists())
        self.assertEqual((self.users / 'admin/renamed.bin').stat().st_ino, inode)
        self.assertEqual(self.index.notifications[0][1:4],
                         ('admin/source.bin', 'admin/renamed.bin', 'rename'))
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_same_mount_move_uses_custody_and_changed_version_never_captures(self):
        self.initialize()
        (self.users / 'admin/folder').mkdir()
        args = self.copy_args(target_path='admin/folder/moved.bin', idempotency_key='move-1')
        args['expected_source_version'] = 'file_wrong'
        rejected = self.writer.execute('move_file', args, self.key)
        self.assertEqual(rejected['operation']['error']['code'], 'file_changed', rejected)
        self.assertTrue((self.users / 'admin/source.bin').exists())
        args.update(expected_source_version=self.version(), idempotency_key='move-2')
        inode = (self.users / 'admin/source.bin').stat().st_ino
        result = self.writer.execute('move_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertEqual((self.users / 'admin/folder/moved.bin').stat().st_ino, inode)
        self.assertFalse((self.users / 'admin/source.bin').exists())

    def test_owner_recovers_unchanged_published_target_after_lost_native_notification(self):
        self.initialize()
        args = self.rename_args()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        snapshot = self.owner_authority.snapshot(ROOT, current=lambda: True)
        record = next(r for r in snapshot['recoveries'] if r['recovery_id'] == recovery_id)
        self.assertEqual(record['objects'], ['published_target'])
        self.owner_authority.revoke(ROOT, confirmed=True, current=lambda: True)
        restored = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored', restored)
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), DATA)
        self.assertFalse((self.users / 'admin/renamed.bin').exists())
        self.assertEqual(self.owner_authority.restore(ROOT, recovery_id,
            confirmed=True, current=lambda: True), restored)

    def test_owner_refuses_changed_published_target_and_completed_rename_has_no_implicit_undo(self):
        self.initialize()
        args = self.rename_args()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', args, self.key)
        (self.users / 'admin/renamed.bin').write_bytes(b'owner changed it')
        with self.assertRaises(FileAccessError):
            self.owner_authority.restore(ROOT, result['operation']['operation_id'],
                                          confirmed=True, current=lambda: True)
        self.assertEqual((self.users / 'admin/renamed.bin').read_bytes(), b'owner changed it')
        (self.users / 'admin/source.bin').write_bytes(DATA)
        result = self.writer.execute('rename_file', self.rename_args(
            new_name='completed.bin', idempotency_key='rename-completed'), self.key)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')
        recovery_id = result['operation']['operation_id']
        record = next(r for r in self.owner_authority.snapshot(ROOT, current=lambda: True)['recoveries']
                      if r['recovery_id'] == recovery_id)
        self.assertEqual(record['objects'], [])
        with self.assertRaises(FileAccessError):
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual((self.users / 'admin/completed.bin').read_bytes(), DATA)

    def test_owner_published_capture_race_has_separate_recovery_and_never_replays_original(self):
        self.initialize()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', self.rename_args(), self.key)
        recovery_id = result['operation']['operation_id']
        original = mutations._rename_new
        def race(source_parent, source, target_parent, target):
            if source == 'renamed.bin' and target == 'source':
                (self.users / 'admin/renamed.bin').rename(self.users / 'admin/preserved-original.bin')
                (self.users / 'admin/renamed.bin').write_bytes(b'concurrent restore object')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race), \
             self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'result_unconfirmed')
        repeated = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(repeated['recovery']['state'], 'result_unconfirmed')
        records = self.owner_authority.snapshot(ROOT, current=lambda: True)['recoveries']
        conflict = next(r for r in records if r['recovery_id'] != recovery_id and r['objects'] == ['source'])
        restored = self.owner_authority.restore(ROOT, conflict['recovery_id'],
                                               confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored')
        self.assertEqual((self.users / 'admin/renamed.bin').read_bytes(), b'concurrent restore object')
        self.assertEqual((self.users / 'admin/preserved-original.bin').read_bytes(), DATA)

    def test_crash_between_capture_and_first_fstat_retains_a_restorable_fixed_name(self):
        self.initialize()
        original = mutations._rename_new
        def crash(source_parent, source, target_parent, target):
            original(source_parent, source, target_parent, target)
            if target == 'source':
                raise SystemExit('simulated crash after kernel capture')
        with mock.patch.object(mutations, '_rename_new', side_effect=crash), \
             self.assertRaises(SystemExit):
            self.writer.execute('rename_file', self.rename_args(), self.key)
        records = self.owner_authority.snapshot(ROOT, current=lambda: True)['recoveries']
        self.assertEqual(records[0]['objects'], ['source'])
        restored = self.owner_authority.restore(ROOT, records[0]['recovery_id'],
                                               confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored')
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), DATA)

    def test_restore_rejects_old_writer_same_length_rewrite_with_original_mtime(self):
        recovery_id, old_writer, before = self.captured_with_old_writer()
        changed = self.rewrite_same_length(old_writer, before, b'X')
        self.assertEqual(os.fstat(old_writer).st_size, before.st_size)
        self.assertEqual(os.fstat(old_writer).st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertFalse((self.users / 'admin/source.bin').exists())
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), changed)
        self.assertEqual((self.users / 'admin/renamed.bin').read_bytes(), b'keep existing target')

    def test_restore_rename_window_rewrite_cannot_be_hidden_by_rename_ctime(self):
        recovery_id, old_writer, before = self.captured_with_old_writer()
        original = mutations._rename_new
        def race(source_parent, source, target_parent, target):
            if source == 'source' and target == 'source.bin':
                self.rewrite_same_length(old_writer, before, b'Y')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race), \
             mock.patch('api_file_native_index.NativeFileIndex') as native, \
             self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'result_unconfirmed')
        native.assert_not_called()
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), b'Y' + DATA[1:])
        repeated = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(repeated['recovery']['state'], 'result_unconfirmed')

    def test_restore_native_wait_rewrite_never_publishes_a_restored_receipt(self):
        recovery_id, old_writer, before = self.captured_with_old_writer()
        native = mock.Mock()
        def changed_during_notification(*unused, **options):
            self.rewrite_same_length(old_writer, before, b'Z')
            return object()
        native.notify_new.side_effect = changed_during_notification
        native.verify_new.return_value = True
        with mock.patch('api_file_native_index.NativeFileIndex', return_value=native), \
             self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'result_unconfirmed')
        self.assertEqual(native.notify_new.call_count, 1)
        repeated = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(repeated['recovery']['state'], 'result_unconfirmed')
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), b'Z' + DATA[1:])

    def test_published_capture_gap_rewrite_is_checked_again_by_ordinary_restore(self):
        self.initialize()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', self.rename_args(), self.key)
        recovery_id = result['operation']['operation_id']
        old_writer = os.open(self.users / 'admin/renamed.bin', os.O_RDWR)
        self.addCleanup(os.close, old_writer)
        before = os.fstat(old_writer)
        original = self.owner_authority._capture_published
        def after_capture(*args, **options):
            manifest = original(*args, **options)
            self.rewrite_same_length(old_writer, before, b'W')
            return manifest
        with mock.patch.object(self.owner_authority, '_capture_published', side_effect=after_capture), \
             mock.patch('api_file_native_index.NativeFileIndex') as native, \
             self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        native.assert_not_called()
        self.assertFalse((self.users / 'admin/source.bin').exists())
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), b'W' + DATA[1:])
        repeated = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(repeated['recovery']['state'], 'result_unconfirmed')

    def test_previous_overwrite_object_requires_its_original_private_hash(self):
        self.initialize()
        old_writer = os.open(self.users / 'admin/source.bin', os.O_RDWR)
        self.addCleanup(os.close, old_writer)
        before = os.fstat(old_writer)
        prepared = self.writer.execute('prepare_file_overwrite', {
            'root_id': ROOT, 'path': 'admin/source.bin', 'size_bytes': 11,
            'sha256': hashlib.sha256(b'replacement').hexdigest(),
            'idempotency_key': 'prepare-previous', 'confirmed': True,
            'expected_target_version': self.version()}, self.key)
        self.assertTrue(prepared['ok'], prepared)
        self.manager.receive_upload(prepared['transfer_id'], self.key, [b'replacement'], content_length=11)
        result = self.writer.execute('commit_file_upload', {
            'transfer_id': prepared['transfer_id'], 'idempotency_key': 'commit-previous'}, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        recovery_id = result['operation']['operation_id']
        (self.users / 'admin/source.bin').rename(self.users / 'admin/preserved-replacement.bin')
        changed = self.rewrite_same_length(old_writer, before, b'P')
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'previous').read_bytes(), changed)
        self.assertEqual((self.users / 'admin/preserved-replacement.bin').read_bytes(), b'replacement')

    def test_wrong_inode_capture_is_preserved_never_published_and_owner_can_restore(self):
        self.initialize()
        args = self.rename_args()
        original = mutations._rename_new
        calls = []
        def race(source_parent, source, target_parent, target):
            calls.append((source, target))
            if target == 'source':
                (self.users / 'admin/source.bin').rename(self.users / 'admin/original-held.bin')
                (self.users / 'admin/source.bin').write_bytes(b'raced object must survive')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race):
            result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        private = self.users / '.r1-toolbox-ops' / recovery_id / 'source'
        self.assertEqual(private.read_bytes(), b'raced object must survive')
        self.assertEqual((self.users / 'admin/original-held.bin').read_bytes(), DATA)
        self.assertFalse((self.users / 'admin/renamed.bin').exists())
        self.assertFalse(self.index.notifications)
        restored = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored', restored)
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), b'raced object must survive')
        self.assertEqual(self.owner_authority.restore(ROOT, recovery_id,
            confirmed=True, current=lambda: True), restored)

    def test_target_race_keeps_both_objects_and_restore_refuses_to_overwrite(self):
        self.initialize()
        args = self.rename_args()
        original = mutations._rename_new
        def occupy(source_parent, source, target_parent, target):
            if source == 'source':
                (self.users / 'admin/renamed.bin').write_bytes(b'concurrent target')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=occupy):
            result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), DATA)
        (self.users / 'admin/source.bin').write_bytes(b'new occupant')
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'target_exists')
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), b'new occupant')
        self.assertFalse(self.index.notifications)

    def test_overwrite_has_its_own_prepare_tool_and_retains_old_object_for_owner(self):
        self.initialize()
        old = (self.users / 'admin/source.bin').read_bytes()
        args = {'root_id': ROOT, 'path': 'admin/source.bin', 'size_bytes': 11,
                'sha256': hashlib.sha256(b'replacement').hexdigest(),
                'idempotency_key': 'prepare-overwrite', 'confirmed': True,
                'expected_target_version': self.version()}
        self.key['tool_grants'].remove('prepare_file_upload')
        prepared = self.writer.execute('prepare_file_overwrite', args, self.key)
        self.assertTrue(prepared['ok'], prepared)
        self.manager.receive_upload(prepared['transfer_id'], self.key,
                                    [b'replacement'], content_length=11)
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), old)
        result = self.writer.execute('commit_file_upload', {
            'transfer_id': prepared['transfer_id'], 'idempotency_key': 'commit-overwrite'}, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertEqual((self.users / 'admin/source.bin').read_bytes(), b'replacement')
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'previous').read_bytes(), old)
        with self.assertRaises(FileAccessError):
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_trash_captures_exact_inode_before_private_native_archive_and_waits_for_verification(self):
        self.initialize()
        backend = RecycleFixture(self.users)
        self.writer.mutations._recycle = backend
        inode = (self.users / 'admin/source.bin').stat().st_ino
        args = {'root_id': ROOT, 'path': 'admin/source.bin', 'expected_source_version': self.version(),
                'confirmed': True, 'idempotency_key': 'trash-1'}
        result = self.writer.execute('trash_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        archived = self.users / 'admin/fixture-recycle/archived.bin'
        self.assertEqual(archived.stat().st_ino, inode)
        self.assertEqual(archived.read_bytes(), DATA)
        self.assertFalse((self.users / 'admin/source.bin').exists())
        self.assertEqual(self.writer.execute('trash_file', args, self.key), result)
        self.assertEqual(backend.archives, 1)
        backend.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_trash_wrong_capture_preserves_both_objects_and_never_calls_native_archive(self):
        self.initialize()
        backend = RecycleFixture(self.users)
        self.writer.mutations._recycle = backend
        args = {'root_id': ROOT, 'path': 'admin/source.bin', 'expected_source_version': self.version(),
                'confirmed': True, 'idempotency_key': 'trash-race'}
        original = mutations._rename_new
        def race(source_parent, source, target_parent, target):
            if target == 'source':
                (self.users / 'admin/source.bin').rename(self.users / 'admin/original-held.bin')
                (self.users / 'admin/source.bin').write_bytes(b'raced preserved file')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race):
            result = self.writer.execute('trash_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.users / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(),
                         b'raced preserved file')
        self.assertEqual((self.users / 'admin/original-held.bin').read_bytes(), DATA)
        self.assertEqual(backend.archives, 0)

    def test_restart_retains_unknown_receipt_and_never_replays_copy(self):
        args = self.copy_args()
        result = self.writer.execute('copy_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.writer.close()
        replacement = FileWriteExecutor(None, root=self.root, transfers=self.manager,
            authorizer=lambda unused: self.valid, authority=WriteAuthorityFixture(),
            mutation_authority=self.owner_authority, index=self.index,
            capacity=lambda descriptor, remaining: None)
        self.addCleanup(replacement.close)
        repeated = replacement.execute('copy_file', args, self.key)
        self.assertEqual(repeated['operation']['state'], 'result_unconfirmed', repeated)
        self.assertEqual(len(self.index.notifications), 1)
        self.assertEqual((self.users / 'admin/copied.bin').read_bytes(), DATA)
        revised = copy.deepcopy(self.key)
        revised['credential_generation'] += 1
        denied = replacement.execute('get_file_operation',
            {'operation_id': result['operation']['operation_id']}, revised)
        self.assertEqual(denied['error']['code'], 'transfer_unavailable')


if __name__ == '__main__':
    unittest.main()
