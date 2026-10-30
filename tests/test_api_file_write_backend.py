"""Audited owner writes plus real Linux anonymous no-clobber publication.

The native index fixture is explicit and never claims factory acceptance. No
vendor daemon, NAS directory or original user file is modified by these tests.
"""

import copy
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_write_backend as writes
from api_file_operations import FileAccessError
from api_file_provider import audited_owner_write_rule
from api_file_transfers import FileTransfers
import test_api_file_executor as read_fixture
from test_api_file_executor import ROOT, ROW, principal


DATA = b'new isolated object\x00' * 4096


class IndexFixture:
    def __init__(self):
        self.ready = False
        self.notifications = []
        self.verifications = 0

    def preflight(self, row, path, *, current):
        if current() is not True:
            raise FileAccessError('invalid_key')

    def notify_new(self, row, path, descriptor, *, directory, current):
        self.preflight(row, path, current=current)
        value = (row['id'], path, os.fstat(descriptor).st_ino, directory)
        self.notifications.append(value)
        return value

    def verify_new(self, receipt, row, path, descriptor, *, directory, current):
        self.preflight(row, path, current=current)
        self.verifications += 1
        if receipt != (row['id'], path, os.fstat(descriptor).st_ino, directory):
            raise FileAccessError('file_changed')
        return self.ready


class WriteAuthorityFixture:
    def authorize(self, root_id, path, current):
        if current() is not True:
            raise FileAccessError('invalid_key')
        if root_id != ROOT or not audited_owner_write_rule(path):
            raise FileAccessError('unsupported_path_class')
        return copy.deepcopy(ROW)


class ReserveTests(unittest.TestCase):
    def test_capacity_rule_is_stricter_than_factory_and_uses_no_path(self):
        value = SimpleNamespace(f_bsize=4096, f_frsize=4096, f_blocks=10 * 1024 ** 2,
                                f_bfree=9 * 1024 ** 2, f_bavail=8 * 1024 ** 2)
        with mock.patch.object(writes.os, 'fstatvfs', return_value=value, create=True) as query:
            writes._capacity(123, 1024)
        query.assert_called_once_with(123)
        value.f_bavail = 1024
        with mock.patch.object(writes.os, 'fstatvfs', return_value=value, create=True), \
             self.assertRaises(FileAccessError) as caught:
            writes._capacity(123, 0)
        self.assertEqual(caught.exception.code, 'limit_exceeded')
        value.f_frsize = 512
        with mock.patch.object(writes.os, 'fstatvfs', return_value=value, create=True), \
             self.assertRaises(FileAccessError) as caught:
            writes._capacity(123, 0)
        self.assertEqual(caught.exception.code, 'write_backend_unverified')


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_TMPFILE'),
                     'Linux O_TMPFILE and actual anonymous-inode linking required')
class NewFileTests(unittest.TestCase):
    def setUp(self):
        read_fixture.OpenedFileTests.setUp(self)
        self.index = IndexFixture()
        self.manager = FileTransfers(lambda unused: self.valid)
        self.addCleanup(self.manager.close)
        self.writer = writes.FileWriteExecutor(None, root=self.root,
            transfers=self.manager, authorizer=lambda unused: self.valid,
            authority=WriteAuthorityFixture(), index=self.index,
            capacity=lambda descriptor, remaining: None)
        self.addCleanup(self.writer.close)
        self.key = principal()
        self.key.update(role='readwrite', tool_grants=[
            'prepare_file_upload', 'commit_file_upload', 'get_file_operation',
            'get_file_transfer', 'cancel_file_transfer'])
        self.key['file_roots'][0].update(path='', access='readwrite')

    def prepare(self, path='new.bin', **changes):
        args = {'root_id': ROOT, 'path': path, 'size_bytes': len(DATA),
                'sha256': hashlib.sha256(DATA).hexdigest(), 'idempotency_key': 'prepare-1'}
        args.update(changes)
        return self.writer.execute('prepare_file_upload', args, self.key)

    def receive(self, receipt):
        return self.manager.receive_upload(receipt['transfer_id'], self.key,
            (DATA[n:n + 65536] for n in range(0, len(DATA), 65536)), content_length=len(DATA))

    def commit(self, receipt):
        return self.writer.execute('commit_file_upload', {
            'transfer_id': receipt['transfer_id'], 'idempotency_key': 'commit-1'}, self.key)

    def operation(self, receipt):
        return self.writer.execute('get_file_operation', {
            'operation_id': receipt['operation']['operation_id']}, self.key)

    def test_anonymous_receive_and_exact_inode_publish_wait_for_native_index(self):
        receipt = self.prepare()
        self.assertTrue(receipt['ok'], receipt)
        descriptor = self.manager._sessions[receipt['transfer_id']]['fd']
        inode = os.fstat(descriptor).st_ino
        self.assertEqual(os.fstat(descriptor).st_nlink, 0)
        self.assertFalse((self.admin / 'new.bin').exists())
        self.receive(receipt)
        self.assertFalse((self.admin / 'new.bin').exists())
        committed = self.commit(receipt)
        self.assertTrue(committed['ok'], committed)
        self.assertEqual(committed['operation']['state'], 'pending')
        self.assertEqual((self.admin / 'new.bin').stat().st_ino, inode)
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)
        self.assertEqual(self.commit(receipt), committed)
        self.assertEqual(len(self.index.notifications), 1)
        self.assertEqual(self.operation(committed)['operation']['state'], 'pending')
        self.index.ready = True
        done = self.operation(committed)
        self.assertEqual(done['operation']['state'], 'completed')
        self.assertRegex(done['operation']['result_version'], r'^file_[0-9a-f]{48}$')
        self.assertEqual(self.manager.get_transfer(receipt['transfer_id'], self.key)['transfer']['state'], 'completed')
        self.assertEqual(len(self.index.notifications), 1)

    def test_create_conflict_never_overwrites_or_notifies_and_is_not_unconfirmed(self):
        receipt = self.prepare()
        self.receive(receipt)
        destination = self.admin / 'new.bin'
        destination.write_bytes(b'keep concurrent original')
        result = self.commit(receipt)
        self.assertEqual(result['operation']['state'], 'failed', result)
        self.assertEqual(result['operation']['error']['code'], 'target_exists')
        self.assertEqual(destination.read_bytes(), b'keep concurrent original')
        self.assertFalse(self.index.notifications)
        self.assertIsNone(self.manager._sessions[receipt['transfer_id']]['fd'])

    def test_public_user_root_readonly_role_leaf_only_and_overwrite_are_rejected(self):
        for path in ('../public/new.bin', ''):
            self.assertFalse(self.prepare(path)['ok'])
        self.assertFalse(self.prepare(overwrite=True)['ok'])
        self.assertFalse(self.prepare(expected_target_version='file_' + 'a' * 48)['ok'])
        self.key['role'] = 'readonly'
        self.assertFalse(self.prepare()['ok'])
        self.key['role'] = 'readwrite'
        self.key['file_roots'][0]['path'] = 'new.bin'
        self.assertFalse(self.prepare()['ok'])
        self.assertFalse((self.admin / 'new.bin').exists())

    def test_parent_replacement_after_prepare_cannot_receive_or_publish(self):
        folder = self.admin / 'folder'
        folder.mkdir()
        receipt = self.prepare('folder/new.bin')
        folder.rename(self.admin / 'moved')
        folder.mkdir()
        with self.assertRaises(FileAccessError) as caught:
            self.receive(receipt)
        self.assertEqual(caught.exception.code, 'file_changed')
        self.assertFalse((folder / 'new.bin').exists())
        self.assertFalse(self.index.notifications)

    def test_replaced_final_object_or_late_revocation_never_completes_index(self):
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        target = self.admin / 'new.bin'
        target.rename(self.admin / 'moved.bin')
        target.write_bytes(b'concurrent replacement')
        self.index.ready = True
        result = self.operation(committed)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed')
        self.assertEqual(target.read_bytes(), b'concurrent replacement')
        self.assertEqual(self.index.verifications, 0)

    def test_pending_commit_cannot_be_cancelled_or_reported_after_key_revocation(self):
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        with self.assertRaises(FileAccessError) as caught:
            self.manager.cancel_transfer(receipt['transfer_id'], self.key)
        self.assertEqual(caught.exception.code, 'transfer_busy')
        self.valid = False
        result = self.operation(committed)
        self.assertEqual(result['error']['code'], 'invalid_key')
        self.assertEqual(self.index.verifications, 0)
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)

    def test_lost_index_notification_never_republishes_or_claims_rollback(self):
        def lost(*args, **kwargs):
            self.index.notifications.append('sent but result unknown')
            raise FileAccessError('timeout')
        self.index.notify_new = lost
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        self.assertEqual(committed['operation']['state'], 'result_unconfirmed')
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)
        self.assertEqual(self.commit(receipt), committed)
        self.assertEqual(self.operation(committed)['operation']['state'], 'result_unconfirmed')
        self.assertEqual(len(self.index.notifications), 1)
        self.assertEqual(self.index.verifications, 0)

    def test_expired_pending_install_releases_fd_without_replaying_or_removing(self):
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        row = self.manager._sessions[receipt['transfer_id']]
        row['deadline'] = self.manager._clock() - 1
        result = self.operation(committed)
        self.assertEqual(result['operation']['state'], 'result_unconfirmed')
        self.assertIsNone(row['fd'])
        self.assertEqual(self.index.verifications, 0)
        self.assertEqual(len(self.index.notifications), 1)
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)

    def test_new_prepare_cleans_expired_pending_install_without_a_live_pending_receipt(self):
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        row = self.manager._sessions[receipt['transfer_id']]
        descriptor = row['fd']
        row['deadline'] = self.manager._clock() - 1
        deadline = row['deadline']
        # Creating an unrelated staging object used to close the installed fd
        # but leave operation=pending forever, bypassing get_operation's gate.
        new = self.prepare('another.bin', idempotency_key='prepare-2')
        self.assertTrue(new['ok'], new)
        self.assertIsNone(row['fd'])
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertEqual(row['state'], 'expired')
        self.assertEqual(self.operation(committed)['operation']['state'], 'result_unconfirmed')
        self.assertEqual(self.commit(receipt)['operation']['state'], 'result_unconfirmed')
        changed = row['changed']
        third = self.prepare('third.bin', idempotency_key='prepare-3')
        self.assertTrue(third['ok'], third)
        self.assertEqual(row['changed'], changed)
        self.assertEqual(row['deadline'], deadline)
        self.assertEqual(len(self.index.notifications), 1)
        self.assertEqual(self.index.verifications, 0)
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)
        self.assertFalse((self.admin / 'another.bin').exists())

    def test_revocation_during_index_verification_discards_a_successful_match(self):
        receipt = self.prepare()
        self.receive(receipt)
        committed = self.commit(receipt)
        def revoke(*args, **kwargs):
            self.index.verifications += 1
            self.valid = False
            return True
        self.index.verify_new = revoke
        result = self.operation(committed)
        self.assertEqual(result['error']['code'], 'invalid_key')
        row = self.manager._sessions[receipt['transfer_id']]
        self.assertEqual(row['operation']['state'], 'result_unconfirmed')
        self.assertIsNone(row['fd'])
        self.assertEqual(self.index.verifications, 1)
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)

    def test_missing_native_index_bridge_refuses_before_creating_staging(self):
        self.writer._index = SimpleNamespace(preflight=lambda *a, **kw: None)
        with mock.patch.object(writes.os, 'open', side_effect=AssertionError('no staging open')):
            result = self.prepare()
        self.assertFalse(result['ok'])
        self.assertFalse(self.index.notifications)


if __name__ == '__main__':
    unittest.main()
