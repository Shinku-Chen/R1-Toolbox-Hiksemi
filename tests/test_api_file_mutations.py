"""Real Linux custody/rename/anonymous-copy tests, never NAS-device acceptance."""

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import struct
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
                or os.fstat(descriptor).st_ino != receipt or (self.users / 'admin' / path).exists():
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
            'restore_path': 'folder/file.bin', 'tool': 'rename_file'}),
            {'path': 'folder/file.bin', 'tool': 'rename_file'})
        for path in ('/drives/data/Users/admin/file', '../admin/file', '',
                     '.DATA_CACHE', '.DATA_CACHE/Recycle.db', '.r1-toolbox-ops',
                     '.r1-toolbox-ops/private/source', None):
            self.assertEqual(FileMutationAuthority._recovery_display({
                'restore_path': path, 'tool': 'arbitrary tool'})['path'], None)
        self.assertEqual(FileMutationAuthority._recovery_display({
            'restore_path': 'example/.DATA_CACHE/file', 'tool': 'arbitrary tool'}),
            {'path': 'example/.DATA_CACHE/file', 'tool': 'unknown'})

    def test_old_upload_grant_never_grants_overwrite(self):
        manager = FileTransfers(lambda unused: True)
        self.addCleanup(manager.close)
        writer = FileWriteExecutor(None, transfers=manager, authorizer=lambda unused: True,
                                   authority=WriteAuthorityFixture())
        self.addCleanup(writer.close)
        key = principal()
        key.update(role='readwrite', tool_grants=['prepare_file_upload', 'commit_file_upload'])
        key['file_roots'][0].update(path='', access='readwrite')
        result = writer.execute('prepare_file_overwrite', {'root_id': ROOT, 'path': 'a',
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
        key['file_roots'][0].update(path='', access='readwrite')
        for arguments in ({'root_id': ROOT, 'path': 'a', 'idempotency_key': 'x',
                           'authority_fd': 4},
                          {'root_id': ROOT, 'path': 'a', 'idempotency_key': 'x',
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
        key['file_roots'][0].update(path='', access='readwrite')
        for path in ('.DATA_CACHE', '.DATA_CACHE/Recycle.db'):
            result = writer.execute('create_directory',
                {'root_id': ROOT, 'path': path, 'idempotency_key': 'cache'}, key)
            self.assertEqual(result['error']['code'], 'unsupported_path_class')
        self.assertEqual(writer._grant(key, 'prepare_file_upload', ROOT,
            'example/.DATA_CACHE/new.bin'), ('example/.DATA_CACHE', 'new.bin'))
        self.assertEqual(writer.mutations._grant(key, 'create_directory', ROOT,
            'example/.DATA_CACHE'), ('example', '.DATA_CACHE'))

    def test_machine_mutations_cannot_reach_root_custody_even_with_full_root_grant(self):
        manager = FileTransfers(lambda unused: True)
        self.addCleanup(manager.close)
        writer = FileWriteExecutor(None, transfers=manager, authorizer=lambda unused: True)
        self.addCleanup(writer.close)
        key = principal()
        key.update(role='readwrite', tool_grants=list(mutations.TOOLS) + ['prepare_file_upload'])
        key['file_roots'][0].update(path='', access='readwrite')
        for path in ('.r1-toolbox-ops', '.r1-toolbox-ops/operation_private/source',
                     '.R1-TOOLBOX-OPS/source', 'folder/.r1-toolbox-ops/file'):
            with self.assertRaises(FileAccessError) as caught:
                writer._grant(key, 'prepare_file_upload', ROOT, path)
            self.assertEqual(caught.exception.code, 'unsupported_path_class')
            for write in (True, False):
                with self.assertRaises(FileAccessError) as caught:
                    writer.mutations._grant(key, 'copy_file', ROOT, path, write=write)
                self.assertEqual(caught.exception.code, 'unsupported_path_class')
        for path in ('.r1-toolbox-ops2/file', 'folder/.r1-toolbox-ops2/file'):
            self.assertEqual(writer._grant(key, 'prepare_file_upload', ROOT, path)[1], 'file')
            self.assertEqual(writer.mutations._grant(key, 'copy_file', ROOT, path, write=False)[1], 'file')


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
        self.key['file_roots'][0].update(path='', access='readwrite')
        (self.admin / 'source.bin').write_bytes(DATA)

    def initialize(self):
        return self.owner_authority.initialize(ROOT, confirmed=True, current=lambda: True)

    def version(self, path='source.bin'):
        descriptor = os.open(self.admin / path, os.O_RDONLY)
        try:
            return _version(_identity(descriptor))
        finally:
            os.close(descriptor)

    def copy_args(self, **changes):
        args = {'source_root_id': ROOT, 'source_path': 'source.bin',
                'target_root_id': ROOT, 'target_path': 'copied.bin',
                'expected_source_version': self.version(changes.get('source_path', 'source.bin')),
                'idempotency_key': 'copy-1'}
        args.update(changes)
        return args

    def rename_args(self, **changes):
        args = {'root_id': ROOT, 'path': 'source.bin', 'new_name': 'renamed.bin',
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
        descriptor = os.open(self.admin / 'source.bin', os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        before = os.fstat(descriptor)
        (self.admin / 'renamed.bin').write_bytes(b'keep existing target')
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

    def test_owner_bootstrap_requires_confirmation_and_rejects_unknown_custody(self):
        with self.assertRaises(FileAccessError):
            self.owner_authority.initialize(ROOT, confirmed=False, current=lambda: True)
        unknown = self.admin / '.r1-toolbox-ops'
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

    def test_authority_persists_admin_inode_and_private_location(self):
        import api_file_mutation_authority as custody
        self.initialize()
        self.assertTrue((self.admin / '.r1-toolbox-ops').is_dir())
        self.assertFalse((self.users / '.r1-toolbox-ops').exists())
        registry = self.state_dir / custody._REGISTRY
        saved = json.loads(registry.read_bytes())
        self.assertEqual(saved['version'], 3)
        self.assertEqual(saved['entries'][ROOT]['layout'], 'admin_custody_v1')
        self.assertEqual(saved['entries'][ROOT]['trust_model'], 'admin_anchor_v1')
        self.assertEqual(saved['entries'][ROOT]['admin_identity'],
                         [self.admin.stat().st_dev, self.admin.stat().st_ino, stat.S_IFDIR])
        self.assertEqual(stat.S_IMODE((self.admin / '.r1-toolbox-ops').stat().st_mode), 0o700)
        self.assertFalse((self.state_dir / custody._LEGACY_REGISTRY).exists())
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertTrue(evidence['ready'], evidence)
        self.assertEqual(evidence['existing_directory'], 'registered')
        self.assertEqual(evidence['recovery_location'], {
            'boundary': 'admin', 'relative_path': '.r1-toolbox-ops',
            'layout': 'admin_custody_v1', 'anchor_component': 'admin_root'})
        self.assertEqual([check['component'] for check in evidence['checks']],
            ['namespace_root', 'drives_directory', 'physical_mount', 'users_root', 'admin_root'])
        self.assertEqual(evidence['recovery_anchor']['component'], 'admin_root')
        self.assertTrue(evidence['recovery_anchor']['same_mount'])
        self.assertTrue(evidence['recovery_anchor']['trusted'])
        self.assertEqual(evidence['trust_boundary'], 'admin')
        self.assertEqual(evidence['registration_trust_boundary'], 'admin')
        self.assertFalse(evidence['owner_revalidation_required'])
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        self.assertEqual(reloaded.preflight(ROOT, current=lambda: True)['existing_directory'], 'registered')

    def test_admin_mode_or_real_posix_acl_remains_a_permission_gate(self):
        os.chmod(self.admin, 0o777)
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertFalse(evidence['ready'])
        self.assertEqual(evidence['recovery_anchor']['mode'], '0777')
        self.assertIn('admin_root_untrusted_mode_or_acl', evidence['blockers'])
        with self.assertRaises(FileAccessError):
            self.initialize()
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())
        os.chmod(self.admin, 0o755)
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in (
            (1, 7, 0xffffffff), (2, 5, 1001), (4, 5, 0xffffffff),
            (16, 5, 0xffffffff), (32, 5, 0xffffffff)))
        try:
            os.setxattr(self.admin, 'system.posix_acl_access', acl)
        except OSError as error:
            import errno
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest('fixture filesystem does not support actual POSIX ACLs')
            raise
        try:
            evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
            self.assertFalse(evidence['ready'])
            self.assertEqual(evidence['recovery_anchor']['access_acl'], 'present')
            with self.assertRaises(FileAccessError):
                self.initialize()
            self.assertFalse((self.admin / '.r1-toolbox-ops').exists())
        finally:
            os.removexattr(self.admin, 'system.posix_acl_access')

    def test_admin_anchor_mount_refusal_cannot_initialize_custody(self):
        with (self.root / 'proc/self/mountinfo').open('a') as stream:
            stream.write(f'88 1 {self.device} / /drives/data/Users/admin rw - ext4 /dev/sda1 rw\n')
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertFalse(evidence['ready'])
        self.assertIsNone(evidence['recovery_anchor'])
        self.assertIn('root_unavailable', evidence['blockers'])
        with self.assertRaises(FileAccessError):
            self.initialize()
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())

    def test_old_admin_v3_registry_cannot_bind_the_new_location_or_change_bytes(self):
        import api_file_mutation_authority as custody
        private = self.users / '.r1-toolbox-ops'
        private.mkdir(mode=0o700)
        (private / 'previous').write_bytes(b'old Users custody object')
        info = private.stat()
        entry = {'row': dict(ROW), 'identity': [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)],
                 'mount_id': self.mount_id, 'enabled': True, 'revision': 4}
        with self.owner_authority._state() as descriptor:
            custody._write_at(descriptor, custody._LEGACY_REGISTRY,
                             {'version': 1, 'entries': {ROOT: entry}})
        legacy_file = self.state_dir / custody._LEGACY_REGISTRY
        original = legacy_file.read_bytes()
        result = self.owner_authority.catalog(current=lambda: True)['authorities'][0]
        self.assertEqual((result['status'], result['reason'], result['enabled']),
                         ('legacy_layout', 'legacy_recovery_layout', False))
        self.assertEqual(result['recovery_location']['boundary'], 'users')
        with mock.patch.object(self.owner_authority, '_handle', side_effect=AssertionError('wrong location')):
            self.assertEqual(self.owner_authority.preflight(ROOT, current=lambda: True)['blockers'],
                             ['legacy_recovery_layout'])
            for action in (lambda: self.initialize(),
                    lambda: self.owner_authority.snapshot(ROOT, current=lambda: True),
                    lambda: self.owner_authority.restore(ROOT, 'operation_' + 'd' * 32,
                                                        confirmed=True, current=lambda: True),
                    lambda: self.owner_authority.revoke(ROOT, confirmed=True, current=lambda: True),
                    lambda: self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)):
                with self.assertRaises(FileAccessError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'legacy_recovery_layout')
        self.assertEqual(legacy_file.read_bytes(), original)
        self.assertEqual((private / 'previous').read_bytes(), b'old Users custody object')
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())
        self.assertFalse((self.state_dir / custody._REGISTRY).exists())

    def test_new_registry_save_never_rewrites_unrelated_legacy_bytes(self):
        import api_file_mutation_authority as custody
        old_id = 'root_' + 'b' * 24
        old = {'row': dict(ROW, id=old_id, mount='/drives/old-disk/Users/admin'),
               'identity': [1, 2, stat.S_IFDIR], 'mount_id': '99', 'enabled': True, 'revision': 8}
        with self.owner_authority._state() as descriptor:
            custody._write_at(descriptor, custody._LEGACY_REGISTRY,
                             {'version': 1, 'entries': {old_id: old}})
        legacy_file = self.state_dir / custody._LEGACY_REGISTRY
        original = legacy_file.read_bytes()
        self.initialize()
        self.assertEqual(legacy_file.read_bytes(), original)
        saved = json.loads((self.state_dir / custody._REGISTRY).read_bytes())
        self.assertEqual(set(saved['entries']), {ROOT})
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        catalog = reloaded.catalog(current=lambda: True)['authorities']
        self.assertEqual({entry['root_id']: entry['status'] for entry in catalog},
                         {old_id: 'legacy_layout', ROOT: 'registered'})
        self.assertEqual(legacy_file.read_bytes(), original)

    def test_legacy_registry_and_objects_are_unchanged_and_never_adopted(self):
        import api_file_mutation_authority as custody
        legacy_id = 'root_' + 'b' * 24
        operation_id = 'operation_' + 'c' * 32
        private = self.users / '.r1-toolbox-ops'
        private.mkdir(mode=0o700)
        operation = private / operation_id
        operation.mkdir(mode=0o700)
        (operation / 'source').write_bytes(b'legacy preserved bytes')
        info = private.stat()
        entry = {'row': dict(ROW, id=legacy_id, mount='/drives/data/Users'),
                 'identity': [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)],
                 'mount_id': self.mount_id, 'enabled': True, 'revision': 7}
        with self.owner_authority._state() as descriptor:
            custody._write_at(descriptor, 'file-mutation-authorities.json',
                             {'version': 1, 'entries': {legacy_id: entry}})
        state_file = self.state_dir / 'file-mutation-authorities.json'
        before = state_file.read_bytes()
        catalog = self.owner_authority.catalog(current=lambda: True)
        self.assertEqual(catalog['authorities'], [{'root_id': legacy_id, 'enabled': False,
            'revision': 7, 'status': 'legacy_boundary', 'legacy': True,
            'reason': 'legacy_users_boundary_v2',
            'recovery_location': custody.FileMutationAuthority._location(legacy=True)}])
        with mock.patch.object(self.owner_authority, '_handle',
                               side_effect=AssertionError('no custody disk access')):
            preflight = self.owner_authority.preflight(legacy_id, current=lambda: True)
            self.assertEqual(preflight['blockers'], ['legacy_users_boundary_v2'])
            actions = (
                lambda: self.owner_authority.initialize(legacy_id, confirmed=True, current=lambda: True),
                lambda: self.owner_authority.snapshot(legacy_id, current=lambda: True),
                lambda: self.owner_authority.restore(legacy_id, operation_id, confirmed=True, current=lambda: True),
                lambda: self.owner_authority.revoke(legacy_id, confirmed=True, current=lambda: True),
                lambda: self.owner_authority.open(entry['row'], operation_id, current=lambda: True),
                lambda: self.initialize(),
            )
            for action in actions:
                with self.assertRaises(FileAccessError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'legacy_users_boundary_v2')
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(json.loads(before)['entries'][legacy_id], entry)
        self.assertEqual((operation / 'source').read_bytes(), b'legacy preserved bytes')
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())

    def test_owner_preflight_reports_real_uid_mode_acl_and_never_creates(self):
        os.chmod(self.users, 0o777)
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertTrue(evidence['ready'], evidence)
        users = [r for r in evidence['checks'] if r['component'] == 'users_root'][0]
        self.assertEqual(users['mode'], '0777')
        self.assertTrue(users['group_other_writable'])
        self.assertFalse(users['required_for_trust'])
        self.assertEqual(users['gate'], 'location_only')
        self.assertIsNone(users['trusted'])
        self.assertIn(users['access_acl'], ('absent', 'present', 'unavailable'))
        self.assertEqual(evidence['source_binding']['native_owner'], 'verified')
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())

    def test_real_writable_nonroot_parents_allow_admin_custody_and_owner_restore(self):
        drives = self.root / 'drives'
        os.chown(drives, 1046, 1046)
        os.chmod(drives, 0o775)
        os.chmod(self.users, 0o777)
        evidence = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertTrue(evidence['ready'], evidence)
        self.assertEqual(evidence['blockers'], [])
        self.assertIsNone(evidence['registration_trust_boundary'])
        self.assertFalse(evidence['owner_revalidation_required'])
        self.assertEqual(evidence['checks'][1]['uid'], 1046)
        for check in evidence['checks'][:-1]:
            self.assertFalse(check['required_for_trust'])
            self.assertEqual(check['gate'], 'location_only')
            self.assertIsNone(check['trusted'])
        self.assertTrue(evidence['recovery_anchor']['required_for_trust'])
        self.assertTrue(evidence['recovery_anchor']['trusted'])
        self.initialize()
        args = self.rename_args()
        (self.admin / 'renamed.bin').write_bytes(b'preserve target')
        result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        self.assertFalse((self.admin / 'source.bin').exists())
        operation_id = result['operation']['operation_id']
        self.assertEqual((self.admin / '.r1-toolbox-ops' / operation_id / 'source').read_bytes(), DATA)
        self.index.ready = True
        with mock.patch('api_file_native_index.NativeFileIndex', return_value=self.index):
            restored = self.owner_authority.restore(ROOT, operation_id, confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored', restored)
        self.assertTrue(restored['recovery']['index_verified'])
        self.assertEqual((self.admin / 'source.bin').read_bytes(), DATA)
        self.assertEqual((self.admin / 'renamed.bin').read_bytes(), b'preserve target')

    def test_nonroot_admin_is_rejected_even_when_parent_permissions_are_ignored(self):
        os.chmod(self.users, 0o777)
        os.chown(self.admin, 1046, 1046)
        result = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertFalse(result['ready'])
        self.assertEqual(result['recovery_anchor']['uid'], 1046)
        self.assertIn('admin_root_untrusted_mode_or_acl', result['blockers'])
        with self.assertRaises(FileAccessError):
            self.initialize()
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())

    def test_outer_real_posix_acl_is_evidence_but_private_acl_remains_forbidden(self):
        import errno
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in (
            (1, 7, 0xffffffff), (2, 7, 1001), (4, 5, 0xffffffff),
            (16, 7, 0xffffffff), (32, 7, 0xffffffff)))
        try:
            os.setxattr(self.users, 'system.posix_acl_access', acl)
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest('fixture filesystem does not support actual POSIX ACLs')
            raise
        result = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertTrue(result['ready'], result)
        users = next(item for item in result['checks'] if item['component'] == 'users_root')
        self.assertEqual(users['access_acl'], 'present')
        self.assertIsNone(users['trusted'])
        self.initialize()
        stage = self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.addCleanup(stage.close)
        private = self.admin / '.r1-toolbox-ops'
        private_acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in (
            (1, 7, 0xffffffff), (2, 0, 1001), (4, 0, 0xffffffff),
            (16, 0, 0xffffffff), (32, 0, 0xffffffff)))
        os.setxattr(private, 'system.posix_acl_access', private_acl)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        with self.assertRaises(FileAccessError):
            stage.verify(lambda: True)
        self.assertFalse(self.owner_authority.preflight(ROOT, current=lambda: True)['ready'])

    def test_registered_admin_replaced_by_empty_directory_is_not_preflight_ready(self):
        self.initialize()
        self.admin.rename(self.users / 'preserved-admin')
        self.admin.mkdir(mode=0o755)
        result = self.owner_authority.preflight(ROOT, current=lambda: True)
        self.assertFalse(result['ready'], result)
        self.assertIn('file_changed', result['blockers'])
        self.assertEqual(result['existing_directory'], 'absent')
        self.assertTrue((self.users / 'preserved-admin/.r1-toolbox-ops').is_dir())

    def test_admin_inode_replacement_cannot_adopt_even_the_original_ops_inode(self):
        import api_file_mutation_authority as custody
        self.initialize()
        stage = self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.addCleanup(stage.close)
        registry = self.state_dir / custody._REGISTRY
        saved = registry.read_bytes()
        original = self.users / 'original-admin'
        self.admin.rename(original)
        self.admin.mkdir(mode=0o755)
        (original / '.r1-toolbox-ops').rename(self.admin / '.r1-toolbox-ops')
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        for authority in (self.owner_authority, reloaded):
            for action in (lambda: authority.open(ROW, 'operation_' + 'e' * 32, current=lambda: True),
                    lambda: authority.snapshot(ROOT, current=lambda: True),
                    lambda: authority.initialize(ROOT, confirmed=True, current=lambda: True)):
                with self.assertRaises(FileAccessError) as caught:
                    action()
                self.assertEqual(caught.exception.code, 'file_changed')
            self.assertFalse(authority.preflight(ROOT, current=lambda: True)['ready'])
        with self.assertRaises(FileAccessError) as caught:
            stage.verify(lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertEqual(registry.read_bytes(), saved)
        (self.admin / '.r1-toolbox-ops').rename(original / '.r1-toolbox-ops')
        self.admin.rmdir()
        original.rename(self.admin)
        stage.verify(lambda: True)
        self.assertTrue(reloaded.snapshot(ROOT, current=lambda: True)['ok'])

    def test_registered_admin_mount_change_invalidates_held_stage_and_inventory(self):
        self.initialize()
        stage = self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.addCleanup(stage.close)
        with (self.root / 'proc/self/mountinfo').open('a') as stream:
            stream.write(f'88 1 {self.device} / /drives/data/Users/admin rw - ext4 /dev/sda1 rw\n')
        for action in (lambda: stage.verify(lambda: True),
                lambda: self.owner_authority.snapshot(ROOT, current=lambda: True)):
            with self.assertRaises(FileAccessError) as caught:
                action()
            self.assertEqual(caught.exception.code, 'root_unavailable')
        self.assertFalse(self.owner_authority.preflight(ROOT, current=lambda: True)['ready'])

    def test_ops_replacement_and_weakened_permissions_invalidate_every_held_stage(self):
        self.initialize()
        stage = self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.addCleanup(stage.close)
        private = self.admin / '.r1-toolbox-ops'
        os.chmod(private, 0o750)
        for action in (lambda: stage.verify(lambda: True),
                lambda: self.owner_authority.snapshot(ROOT, current=lambda: True)):
            with self.assertRaises(FileAccessError):
                action()
        os.chmod(private, 0o700)
        private.rename(self.admin / 'preserved-private')
        private.mkdir(mode=0o700)
        with self.assertRaises(FileAccessError) as caught:
            stage.verify(lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertFalse(self.owner_authority.preflight(ROOT, current=lambda: True)['ready'])
        self.assertTrue((self.admin / 'preserved-private' / stage.operation_id).exists())

    def test_initialize_detects_admin_replacement_after_mkdir_without_adopting_or_deleting(self):
        import api_file_mutation_authority as custody
        original = self.users / 'preserved-admin'
        real_mkdir = os.mkdir
        replaced = False
        def swap_after_mkdir(path, *args, **options):
            nonlocal replaced
            result = real_mkdir(path, *args, **options)
            if path == '.r1-toolbox-ops' and not replaced:
                replaced = True
                self.admin.rename(original)
                real_mkdir(self.admin, 0o755)
            return result
        with mock.patch.object(custody.os, 'mkdir', side_effect=swap_after_mkdir):
            with self.assertRaises(FileAccessError) as caught:
                self.initialize()
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertTrue((original / '.r1-toolbox-ops').is_dir())
        self.assertFalse((self.admin / '.r1-toolbox-ops').exists())
        self.assertNotIn(ROOT, self.owner_authority._load())
        self.assertFalse((self.state_dir / custody._REGISTRY).exists())

    def test_initialize_save_failure_preserves_unregistered_object_and_old_memory(self):
        with mock.patch.object(self.owner_authority, '_save', side_effect=FileAccessError('write_backend_unverified')):
            with self.assertRaises(FileAccessError):
                self.initialize()
        self.assertNotIn(ROOT, self.owner_authority._load())
        self.assertTrue((self.admin / '.r1-toolbox-ops').is_dir())
        with self.assertRaises(FileAccessError):
            self.initialize()

    def test_initialize_rechecks_canonical_admin_after_durable_save(self):
        import api_file_mutation_authority as custody
        save = self.owner_authority._save
        original = self.users / 'preserved-admin'
        def swap_after_save():
            save()
            self.admin.rename(original)
            self.admin.mkdir(mode=0o755)
        with mock.patch.object(self.owner_authority, '_save', side_effect=swap_after_save):
            with self.assertRaises(FileAccessError) as caught:
                self.initialize()
        self.assertEqual(caught.exception.code, 'file_changed')
        saved = json.loads((self.state_dir / custody._REGISTRY).read_bytes())['entries'][ROOT]
        self.assertEqual(saved['admin_identity'][1], original.stat().st_ino)
        with self.assertRaises(FileAccessError):
            self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.assertTrue((original / '.r1-toolbox-ops').is_dir())

    def test_snapshot_discards_inventory_if_admin_is_moved_during_manifest_read(self):
        import api_file_mutation_authority as custody
        self.initialize()
        with self.owner_authority.open(ROW, 'operation_' + 'd' * 32, current=lambda: True) as stage:
            stage.record({'restore_path': 'source.bin', 'tool': 'rename_file'}, current=lambda: True)
        read = custody._read_at
        original = self.users / 'preserved-admin'
        def swap_after_read(parent, name, *args):
            result = read(parent, name, *args)
            if name == 'manifest.json':
                self.admin.rename(original)
                self.admin.mkdir(mode=0o755)
            return result
        with mock.patch.object(custody, '_read_at', side_effect=swap_after_read):
            with self.assertRaises(FileAccessError) as caught:
                self.owner_authority.snapshot(ROOT, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')

    def test_old_admin_v2_keeps_full_chain_policy_until_explicit_owner_revalidation(self):
        import api_file_mutation_authority as custody
        self.initialize()
        registry = self.state_dir / custody._REGISTRY
        old = json.loads(registry.read_bytes())['entries'][ROOT]
        del old['trust_model'], old['admin_identity']
        with self.owner_authority._state() as parent:
            custody._write_at(parent, custody._REGISTRY, {'version': 2, 'entries': {ROOT: old}})
        before = registry.read_bytes()
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        self.assertEqual(reloaded.catalog(current=lambda: True)['authorities'][0]['trust_boundary'],
                         'complete_ancestors_legacy')
        with reloaded.open(ROW, 'operation_' + 'd' * 32, current=lambda: True) as stage:
            stage.verify(lambda: True)
        self.assertEqual(registry.read_bytes(), before)
        os.chmod(self.users, 0o777)
        with self.assertRaises(FileAccessError):
            reloaded.open(ROW, 'operation_' + 'e' * 32, current=lambda: True)
        with self.assertRaises(FileAccessError):
            reloaded.snapshot(ROOT, current=lambda: True)
        preflight = reloaded.preflight(ROOT, current=lambda: True)
        self.assertTrue(preflight['ready'], preflight)
        self.assertTrue(preflight['owner_revalidation_required'])
        self.assertEqual(preflight['registration_trust_boundary'], 'complete_ancestors_legacy')
        self.assertEqual(registry.read_bytes(), before)
        upgraded = reloaded.initialize(ROOT, confirmed=True, current=lambda: True)
        self.assertEqual(upgraded['authority']['trust_boundary'], 'admin')
        self.assertEqual(upgraded['authority']['revision'], old['revision'] + 1)
        saved = json.loads(registry.read_bytes())
        self.assertEqual(saved['version'], 3)
        self.assertEqual(saved['entries'][ROOT]['identity'], old['identity'])
        with reloaded.open(ROW, 'operation_' + 'e' * 32, current=lambda: True) as stage:
            stage.verify(lambda: True)

    def test_mixed_v3_records_keep_unbound_entries_and_current_mount_id_is_not_persistent_identity(self):
        import api_file_mutation_authority as custody
        self.initialize()
        registry = self.state_dir / custody._REGISTRY
        data = json.loads(registry.read_bytes())
        data['entries'][ROOT]['mount_id'] = '123456789'
        other = copy.deepcopy(data['entries'][ROOT])
        other['row']['id'] = 'root_' + 'f' * 24
        other['row']['mount'] = '/drives/another/Users/admin'
        del other['trust_model'], other['admin_identity']
        data['entries'][other['row']['id']] = other
        with self.owner_authority._state() as parent:
            custody._write_at(parent, custody._REGISTRY, data)
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        with reloaded.open(ROW, 'operation_' + 'e' * 32, current=lambda: True) as stage:
            stage.verify(lambda: True)
        reloaded.revoke(ROOT, confirmed=True, current=lambda: True)
        self.assertEqual(json.loads(registry.read_bytes())['entries'][other['row']['id']], other)

    def test_failed_explicit_upgrade_never_enables_admin_only_policy_in_memory(self):
        import api_file_mutation_authority as custody
        self.initialize()
        registry = self.state_dir / custody._REGISTRY
        old = json.loads(registry.read_bytes())['entries'][ROOT]
        del old['trust_model'], old['admin_identity']
        with self.owner_authority._state() as parent:
            custody._write_at(parent, custody._REGISTRY, {'version': 2, 'entries': {ROOT: old}})
        original = registry.read_bytes()
        os.chmod(self.users, 0o777)
        reloaded = FileMutationAuthority(None, root=self.root, state_dir=self.state_dir,
                                        authority=WriteAuthorityFixture())
        with mock.patch.object(reloaded, '_save', side_effect=FileAccessError('write_backend_unverified')):
            with self.assertRaises(FileAccessError):
                reloaded.initialize(ROOT, confirmed=True, current=lambda: True)
        self.assertEqual(reloaded._load()[ROOT], old)
        self.assertEqual(registry.read_bytes(), original)
        with self.assertRaises(FileAccessError):
            reloaded.open(ROW, 'operation_' + 'd' * 32, current=lambda: True)
        self.assertEqual(reloaded.catalog(current=lambda: True)['authorities'][0]['trust_boundary'],
                         'complete_ancestors_legacy')

    def test_same_inode_private_alias_cannot_be_a_copy_source(self):
        self.initialize()
        private = self.admin / '.r1-toolbox-ops'
        secret = private / 'preserved.bin'
        secret.write_bytes(b'private owner recovery object')
        (self.admin / 'ops-alias').mkdir()
        expected = os.open(secret, os.O_RDONLY)
        try:
            version = _version(_identity(expected))
        finally:
            os.close(expected)
        real_open = os.open
        admin_identity = self.admin.stat().st_dev, self.admin.stat().st_ino
        def alias_open(path, flags, *args, **options):
            parent = options.get('dir_fd')
            if os.fspath(path) == 'ops-alias' and parent is not None:
                info = os.fstat(parent)
                if (info.st_dev, info.st_ino) == admin_identity:
                    path = '.r1-toolbox-ops'
            return real_open(path, flags, *args, **options)
        arguments = self.copy_args()
        arguments.update(source_path='ops-alias/preserved.bin', expected_source_version=version)
        with mock.patch('api_file_executor.os.open', side_effect=alias_open):
            result = self.writer.execute('copy_file', arguments, self.key)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'permission_denied')
        self.assertFalse((self.admin / 'copied.bin').exists())
        self.assertEqual(secret.read_bytes(), b'private owner recovery object')

    def test_owner_restore_never_publishes_a_manifest_target_inside_private_custody(self):
        import api_file_mutation_authority as custody
        recovery_id, descriptor, before = self.captured_with_old_writer()
        with self.owner_authority.open(ROW, recovery_id, current=lambda: True, create=False) as stage:
            manifest = custody._read_at(stage.fd, 'manifest.json')
            manifest['restore_path'] = '.r1-toolbox-ops/owner-injected.bin'
            stage.record(manifest, current=lambda: True)
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'write_backend_unverified')
        self.assertFalse((self.admin / '.r1-toolbox-ops/owner-injected.bin').exists())
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), DATA)

    def test_copy_uses_real_bytes_and_no_clobber_then_independent_index(self):
        args = self.copy_args()
        source_inode = (self.admin / args['source_path']).stat().st_ino
        result = self.writer.execute('copy_file', args, self.key)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['operation']['state'], 'pending', result)
        copied = self.admin / args['target_path']
        self.assertEqual(copied.read_bytes(), DATA)
        self.assertNotEqual(copied.stat().st_ino, source_inode)
        self.assertEqual((self.admin / args['source_path']).stat().st_ino, source_inode)
        self.assertEqual(self.writer.execute('copy_file', args, self.key), result)
        self.assertEqual(len(self.index.notifications), 1)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_copy_conflict_keeps_both_files_and_never_notifies(self):
        args = self.copy_args()
        (self.admin / args['target_path']).write_bytes(b'existing destination')
        result = self.writer.execute('copy_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'failed', result)
        self.assertEqual((self.admin / args['target_path']).read_bytes(), b'existing destination')
        self.assertEqual((self.admin / args['source_path']).read_bytes(), DATA)
        self.assertFalse(self.index.notifications)

    def test_copy_readonly_source_and_write_target_can_poll_completed_operation(self):
        (self.admin / 'input').mkdir()
        (self.admin / 'output').mkdir()
        (self.admin / 'source.bin').rename(self.admin / 'input/source.bin')
        self.key['file_roots'] = [
            {'id': ROOT, 'path': 'input', 'access': 'readonly'},
            {'id': ROOT, 'path': 'output', 'access': 'readwrite'}]
        args = self.copy_args(source_path='input/source.bin', target_path='output/copy.bin',
                              expected_source_version=self.version('input/source.bin'))
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
        self.assertFalse((self.admin / args['target_path']).exists())
        self.assertFalse(self.index.notifications)

    def test_mkdir_uses_trusted_birth_inode_and_waits_for_directory_index(self):
        self.initialize()
        args = {'root_id': ROOT, 'path': 'new-directory', 'idempotency_key': 'mkdir-1'}
        result = self.writer.execute('create_directory', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        inode = (self.admin / args['path']).stat().st_ino
        self.assertEqual(self.index.notifications[0][2:], (inode, True))
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_uninitialized_authority_rejects_custody_without_touching_source(self):
        result = self.writer.execute('rename_file', self.rename_args(), self.key)
        self.assertEqual(result['operation']['state'], 'failed', result)
        self.assertEqual((self.admin / 'source.bin').read_bytes(), DATA)
        self.assertFalse((self.admin / 'renamed.bin').exists())

    def test_rename_preserves_inode_and_only_reports_after_native_old_and_new_keys(self):
        self.initialize()
        inode = (self.admin / 'source.bin').stat().st_ino
        result = self.writer.execute('rename_file', self.rename_args(), self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertFalse((self.admin / 'source.bin').exists())
        self.assertEqual((self.admin / 'renamed.bin').stat().st_ino, inode)
        self.assertEqual(self.index.notifications[0][1:4],
                         ('source.bin', 'renamed.bin', 'rename'))
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_same_mount_move_uses_custody_and_changed_version_never_captures(self):
        self.initialize()
        (self.admin / 'folder').mkdir()
        args = self.copy_args(target_path='folder/moved.bin', idempotency_key='move-1')
        args['expected_source_version'] = 'file_wrong'
        rejected = self.writer.execute('move_file', args, self.key)
        self.assertEqual(rejected['operation']['error']['code'], 'file_changed', rejected)
        self.assertTrue((self.admin / 'source.bin').exists())
        args.update(expected_source_version=self.version(), idempotency_key='move-2')
        inode = (self.admin / 'source.bin').stat().st_ino
        result = self.writer.execute('move_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertEqual((self.admin / 'folder/moved.bin').stat().st_ino, inode)
        self.assertFalse((self.admin / 'source.bin').exists())

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
        self.assertEqual((self.admin / 'source.bin').read_bytes(), DATA)
        self.assertFalse((self.admin / 'renamed.bin').exists())
        self.assertEqual(self.owner_authority.restore(ROOT, recovery_id,
            confirmed=True, current=lambda: True), restored)

    def test_owner_refuses_changed_published_target_and_completed_rename_has_no_implicit_undo(self):
        self.initialize()
        args = self.rename_args()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', args, self.key)
        (self.admin / 'renamed.bin').write_bytes(b'owner changed it')
        with self.assertRaises(FileAccessError):
            self.owner_authority.restore(ROOT, result['operation']['operation_id'],
                                          confirmed=True, current=lambda: True)
        self.assertEqual((self.admin / 'renamed.bin').read_bytes(), b'owner changed it')
        (self.admin / 'source.bin').write_bytes(DATA)
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
        self.assertEqual((self.admin / 'completed.bin').read_bytes(), DATA)

    def test_owner_published_capture_race_has_separate_recovery_and_never_replays_original(self):
        self.initialize()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', self.rename_args(), self.key)
        recovery_id = result['operation']['operation_id']
        original = mutations._rename_new
        def race(source_parent, source, target_parent, target):
            if source == 'renamed.bin' and target == 'source':
                (self.admin / 'renamed.bin').rename(self.admin / 'preserved-original.bin')
                (self.admin / 'renamed.bin').write_bytes(b'concurrent restore object')
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
        self.assertEqual((self.admin / 'renamed.bin').read_bytes(), b'concurrent restore object')
        self.assertEqual((self.admin / 'preserved-original.bin').read_bytes(), DATA)

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
        self.assertEqual((self.admin / 'source.bin').read_bytes(), DATA)

    def test_restore_rejects_old_writer_same_length_rewrite_with_original_mtime(self):
        recovery_id, old_writer, before = self.captured_with_old_writer()
        changed = self.rewrite_same_length(old_writer, before, b'X')
        self.assertEqual(os.fstat(old_writer).st_size, before.st_size)
        self.assertEqual(os.fstat(old_writer).st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertFalse((self.admin / 'source.bin').exists())
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), changed)
        self.assertEqual((self.admin / 'renamed.bin').read_bytes(), b'keep existing target')

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
        self.assertEqual((self.admin / 'source.bin').read_bytes(), b'Y' + DATA[1:])
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
        self.assertEqual((self.admin / 'source.bin').read_bytes(), b'Z' + DATA[1:])

    def test_published_capture_gap_rewrite_is_checked_again_by_ordinary_restore(self):
        self.initialize()
        with mock.patch.object(self.index, 'notify_mutation',
                               side_effect=FileAccessError('result_unconfirmed')):
            result = self.writer.execute('rename_file', self.rename_args(), self.key)
        recovery_id = result['operation']['operation_id']
        old_writer = os.open(self.admin / 'renamed.bin', os.O_RDWR)
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
        self.assertFalse((self.admin / 'source.bin').exists())
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), b'W' + DATA[1:])
        repeated = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(repeated['recovery']['state'], 'result_unconfirmed')

    def test_previous_overwrite_object_requires_its_original_private_hash(self):
        self.initialize()
        old_writer = os.open(self.admin / 'source.bin', os.O_RDWR)
        self.addCleanup(os.close, old_writer)
        before = os.fstat(old_writer)
        prepared = self.writer.execute('prepare_file_overwrite', {
            'root_id': ROOT, 'path': 'source.bin', 'size_bytes': 11,
            'sha256': hashlib.sha256(b'replacement').hexdigest(),
            'idempotency_key': 'prepare-previous', 'confirmed': True,
            'expected_target_version': self.version()}, self.key)
        self.assertTrue(prepared['ok'], prepared)
        self.manager.receive_upload(prepared['transfer_id'], self.key, [b'replacement'], content_length=11)
        result = self.writer.execute('commit_file_upload', {
            'transfer_id': prepared['transfer_id'], 'idempotency_key': 'commit-previous'}, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        recovery_id = result['operation']['operation_id']
        (self.admin / 'source.bin').rename(self.admin / 'preserved-replacement.bin')
        changed = self.rewrite_same_length(old_writer, before, b'P')
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'previous').read_bytes(), changed)
        self.assertEqual((self.admin / 'preserved-replacement.bin').read_bytes(), b'replacement')

    def test_wrong_inode_capture_is_preserved_never_published_and_owner_can_restore(self):
        self.initialize()
        args = self.rename_args()
        original = mutations._rename_new
        calls = []
        def race(source_parent, source, target_parent, target):
            calls.append((source, target))
            if target == 'source':
                (self.admin / 'source.bin').rename(self.admin / 'original-held.bin')
                (self.admin / 'source.bin').write_bytes(b'raced object must survive')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race):
            result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        private = self.admin / '.r1-toolbox-ops' / recovery_id / 'source'
        self.assertEqual(private.read_bytes(), b'raced object must survive')
        self.assertEqual((self.admin / 'original-held.bin').read_bytes(), DATA)
        self.assertFalse((self.admin / 'renamed.bin').exists())
        self.assertFalse(self.index.notifications)
        restored = self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(restored['recovery']['state'], 'restored', restored)
        self.assertEqual((self.admin / 'source.bin').read_bytes(), b'raced object must survive')
        self.assertEqual(self.owner_authority.restore(ROOT, recovery_id,
            confirmed=True, current=lambda: True), restored)

    def test_target_race_keeps_both_objects_and_restore_refuses_to_overwrite(self):
        self.initialize()
        args = self.rename_args()
        original = mutations._rename_new
        def occupy(source_parent, source, target_parent, target):
            if source == 'source':
                (self.admin / 'renamed.bin').write_bytes(b'concurrent target')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=occupy):
            result = self.writer.execute('rename_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(), DATA)
        (self.admin / 'source.bin').write_bytes(b'new occupant')
        with self.assertRaises(FileAccessError) as caught:
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.assertEqual(caught.exception.code, 'target_exists')
        self.assertEqual((self.admin / 'source.bin').read_bytes(), b'new occupant')
        self.assertFalse(self.index.notifications)

    def test_overwrite_has_its_own_prepare_tool_and_retains_old_object_for_owner(self):
        self.initialize()
        old = (self.admin / 'source.bin').read_bytes()
        args = {'root_id': ROOT, 'path': 'source.bin', 'size_bytes': 11,
                'sha256': hashlib.sha256(b'replacement').hexdigest(),
                'idempotency_key': 'prepare-overwrite', 'confirmed': True,
                'expected_target_version': self.version()}
        self.key['tool_grants'].remove('prepare_file_upload')
        prepared = self.writer.execute('prepare_file_overwrite', args, self.key)
        self.assertTrue(prepared['ok'], prepared)
        self.manager.receive_upload(prepared['transfer_id'], self.key,
                                    [b'replacement'], content_length=11)
        self.assertEqual((self.admin / 'source.bin').read_bytes(), old)
        result = self.writer.execute('commit_file_upload', {
            'transfer_id': prepared['transfer_id'], 'idempotency_key': 'commit-overwrite'}, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        self.assertEqual((self.admin / 'source.bin').read_bytes(), b'replacement')
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'previous').read_bytes(), old)
        with self.assertRaises(FileAccessError):
            self.owner_authority.restore(ROOT, recovery_id, confirmed=True, current=lambda: True)
        self.index.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_trash_captures_exact_inode_before_private_native_archive_and_waits_for_verification(self):
        self.initialize()
        backend = RecycleFixture(self.users)
        self.writer.mutations._recycle = backend
        inode = (self.admin / 'source.bin').stat().st_ino
        args = {'root_id': ROOT, 'path': 'source.bin', 'expected_source_version': self.version(),
                'confirmed': True, 'idempotency_key': 'trash-1'}
        result = self.writer.execute('trash_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'pending', result)
        archived = self.admin / 'fixture-recycle/archived.bin'
        self.assertEqual(archived.stat().st_ino, inode)
        self.assertEqual(archived.read_bytes(), DATA)
        self.assertFalse((self.admin / 'source.bin').exists())
        self.assertEqual(self.writer.execute('trash_file', args, self.key), result)
        self.assertEqual(backend.archives, 1)
        backend.ready = True
        self.assertEqual(self.poll(result)['operation']['state'], 'completed')

    def test_trash_wrong_capture_preserves_both_objects_and_never_calls_native_archive(self):
        self.initialize()
        backend = RecycleFixture(self.users)
        self.writer.mutations._recycle = backend
        args = {'root_id': ROOT, 'path': 'source.bin', 'expected_source_version': self.version(),
                'confirmed': True, 'idempotency_key': 'trash-race'}
        original = mutations._rename_new
        def race(source_parent, source, target_parent, target):
            if target == 'source':
                (self.admin / 'source.bin').rename(self.admin / 'original-held.bin')
                (self.admin / 'source.bin').write_bytes(b'raced preserved file')
            return original(source_parent, source, target_parent, target)
        with mock.patch.object(mutations, '_rename_new', side_effect=race):
            result = self.writer.execute('trash_file', args, self.key)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed', result)
        recovery_id = result['operation']['operation_id']
        self.assertEqual((self.admin / '.r1-toolbox-ops' / recovery_id / 'source').read_bytes(),
                         b'raced preserved file')
        self.assertEqual((self.admin / 'original-held.bin').read_bytes(), DATA)
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
        self.assertEqual((self.admin / 'copied.bin').read_bytes(), DATA)
        revised = copy.deepcopy(self.key)
        revised['credential_generation'] += 1
        denied = replacement.execute('get_file_operation',
            {'operation_id': result['operation']['operation_id']}, revised)
        self.assertEqual(denied['error']['code'], 'transfer_unavailable')


if __name__ == '__main__':
    unittest.main()
