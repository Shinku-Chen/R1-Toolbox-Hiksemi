"""The private indexing bridge sends a fixed pair, never arbitrary RPC data."""
import importlib
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
index = importlib.import_module('api_file_native_index')
from api_file_operations import FileAccessError

ROOT = {'id': 'root_' + '1' * 24, 'mount': '/drives/BSJQZGLBMYA/Users/admin'}
PATH = '临时共享/accept.bin'
IDENTITY = (8, 42, 8193, 123000000000, 124000000000)
NATIVE = ((123, 40, '99', '127.0.0.1'), '100', (1, 2), (3, 4))


class NativeFileIndexTests(unittest.TestCase):
    def make(self):
        self.current = True
        self.reader = mock.Mock()
        self.reader.preflight.return_value = 'private-reader-receipt'
        self.reader.verify_new.return_value = True
        self.reader.verify_mutation.return_value = True
        self.connection = mock.Mock()
        self.connection.getsockopt.return_value = struct.pack('3i', 123, 0, 0)
        self.listener = mock.Mock(return_value=NATIVE)
        self.object_check = mock.Mock(return_value=IDENTITY)
        self.factory = mock.Mock(return_value=self.connection)
        self.bridge = index.NativeFileIndex(reader=self.reader, listener_check=self.listener,
            disk_check=lambda row: True, object_check=self.object_check,
            socket_factory=self.factory)
        for name, value in (('SO_PEERCRED', 17), ('AF_UNIX', 1)):
            patcher = mock.patch.object(socket, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        return lambda: self.current

    def test_literal_wire_records_and_only_create_then_close(self):
        payload = index._new_file_records(ROOT, PATH)
        self.assertEqual(len(payload), 0x834 * 2)
        rows = [index._RECORD.unpack_from(payload, offset) for offset in (0, 0x834)]
        absolute = (ROOT['mount'] + '/' + PATH).encode('utf-8')
        self.assertEqual([row[2] for row in rows], [0x14, 0x15])
        for row in rows:
            self.assertEqual(row[0].rstrip(b'\0'), b'admin')
            self.assertEqual(row[1].rstrip(b'\0'), b'/drives/BSJQZGLBMYA')
            self.assertEqual(row[3].rstrip(b'\0'), absolute)
        self.assertEqual(rows[0][4].rstrip(b'\0'), b'file|create|' + absolute)
        self.assertEqual(rows[1][4].rstrip(b'\0'), b'')

    def test_wire_never_truncates_or_accepts_outside_admin_boundary(self):
        for path in ('', '../file', '/public/file',
                     'a\\file', 'file|create|other', 'parent|dir/file',
                     '界' * 400):
            with self.subTest(path=path), self.assertRaises(FileAccessError):
                index._new_file_records(ROOT, path)
        with self.assertRaises(FileAccessError):
            index._new_file_records({**ROOT, 'mount': '/drives/' + 'a' * 40 + '/Users/admin'}, PATH)

    def test_public_or_admin_named_children_are_literal_and_old_users_rows_are_rejected(self):
        for path in ('public/file', 'guest0/file', 'admin/file'):
            record = index._RECORD.unpack_from(index._new_file_records(ROOT, path))
            self.assertEqual(record[3].rstrip(b'\0'), (ROOT['mount'] + '/' + path).encode())
        with self.assertRaises(FileAccessError):
            index._new_file_records({**ROOT, 'mount': '/drives/BSJQZGLBMYA/Users'}, PATH)

    def test_preflight_checks_absence_and_refuses_unverified_transport_before_send(self):
        current = self.make()
        self.bridge.preflight(ROOT, PATH, current=current)
        self.assertTrue(self.reader.preflight.call_args.kwargs['require_absent'])
        self.factory.assert_not_called()
        self.bridge._disk = lambda row: False
        with self.assertRaises(FileAccessError) as error:
            self.bridge.preflight(ROOT, PATH, current=current)
        self.assertEqual(error.exception.code, 'write_backend_unverified')
        self.factory.assert_not_called()

    def test_stale_index_preflight_rejects_without_notify(self):
        current = self.make()
        self.reader.preflight.side_effect = FileAccessError('target_exists')
        with self.assertRaises(FileAccessError):
            self.bridge.preflight(ROOT, PATH, current=current)
        self.factory.assert_not_called()

    def test_notify_is_not_completion_and_verify_never_resends(self):
        current = self.make()
        receipt = self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
        self.assertFalse(self.reader.preflight.call_args.kwargs['require_absent'])
        self.connection.sendall.assert_called_once_with(index._new_file_records(ROOT, PATH))
        self.reader.verify_new.assert_not_called()
        self.connection.close.assert_called_once()
        self.reader.verify_new.return_value = False
        self.assertFalse(self.bridge.verify_new(receipt, ROOT, PATH, 7, directory=False, current=current))
        self.reader.verify_new.return_value = True
        self.assertTrue(self.bridge.verify_new(receipt, ROOT, PATH, 7, directory=False, current=current))
        self.connection.sendall.assert_called_once()

    def test_wrong_peer_or_changed_listener_is_refused_before_send(self):
        current = self.make()
        for peer in ((124, 0, 0), (123, 1000, 1000)):
            self.connection.getsockopt.return_value = struct.pack('3i', *peer)
            with self.assertRaises(FileAccessError):
                self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
            self.connection.sendall.assert_not_called()
        self.connection.getsockopt.return_value = struct.pack('3i', 123, 0, 0)
        self.listener.side_effect = [NATIVE, ('different',)]
        with self.assertRaises(FileAccessError):
            self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
        self.connection.sendall.assert_not_called()

    def test_mid_send_revoke_and_timeout_do_not_return_a_receipt(self):
        current = self.make()
        self.connection.sendall.side_effect = lambda payload: setattr(self, 'current', False)
        with self.assertRaises(FileAccessError) as error:
            self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
        self.assertEqual(error.exception.code, 'invalid_key')
        self.connection.sendall.assert_called_once()
        self.current = True
        self.connection.sendall.side_effect = TimeoutError()
        with self.assertRaises(FileAccessError) as error:
            self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
        self.assertEqual(error.exception.code, 'result_unconfirmed')

    def test_result_is_bound_to_opened_file_path_root_and_service_lifecycle(self):
        current = self.make()
        receipt = self.bridge.notify_new(ROOT, PATH, 7, directory=False, current=current)
        for root, path in (({**ROOT, 'id': 'root_' + '2' * 24}, PATH), (ROOT, 'other')):
            with self.assertRaises(FileAccessError):
                self.bridge.verify_new(receipt, root, path, 7, directory=False, current=current)
        self.object_check.return_value = (8, 43, *IDENTITY[2:])
        with self.assertRaises(FileAccessError):
            self.bridge.verify_new(receipt, ROOT, PATH, 7, directory=False, current=current)
        self.object_check.return_value = IDENTITY
        self.listener.return_value = ('different',)
        with self.assertRaises(FileAccessError):
            self.bridge.verify_new(receipt, ROOT, PATH, 7, directory=False, current=current)
        self.connection.sendall.assert_called_once()

    def test_directory_uses_one_mkdir_record_and_receipt_binds_kind(self):
        current = self.make()
        receipt = self.bridge.notify_new(ROOT, PATH, 7, directory=True, current=current)
        payload = self.connection.sendall.call_args.args[0]
        self.assertEqual(len(payload), index._RECORD.size)
        row = index._RECORD.unpack(payload)
        self.assertEqual(row[2], 0x10)
        self.assertEqual(row[3].rstrip(b'\0'), (ROOT['mount'] + '/' + PATH).encode())
        self.assertEqual(row[4].rstrip(b'\0'), b'')
        self.assertTrue(self.bridge.verify_new(receipt, ROOT, PATH, 7,
                                               directory=True, current=current))
        with self.assertRaises(FileAccessError):
            self.bridge.verify_new(receipt, ROOT, PATH, 7, directory=False, current=current)

    def test_invalid_kind_and_revoked_preflight_cannot_send(self):
        current = self.make()
        with self.assertRaises(FileAccessError):
            self.bridge.notify_new(ROOT, PATH, 7, directory=1, current=current)
        self.current = False
        with self.assertRaises(FileAccessError):
            self.bridge.preflight(ROOT, PATH, current=current)
        self.factory.assert_not_called()

    def test_rename_and_move_preserve_literal_real_paths_and_never_resend(self):
        current = self.make()
        target = '临时共享/renamed.bin'
        for operation in ('rename', 'move'):
            with self.subTest(operation=operation):
                self.connection.reset_mock()
                prepared = self.bridge.preflight_mutation(ROOT, PATH, target,
                                                         operation=operation, current=current)
                receipt = self.bridge.notify_mutation(ROOT, PATH, target, 7,
                    operation=operation, prepared=prepared, current=current)
                row = index._RECORD.unpack(self.connection.sendall.call_args.args[0])
                self.assertEqual(row[2], 0x21)
                self.assertEqual(row[3].rstrip(b'\0'), (ROOT['mount'] + '/' + PATH).encode())
                self.assertEqual(row[4].rstrip(b'\0'), (ROOT['mount'] + '/' + target).encode())
                self.reader.verify_mutation.return_value = False
                self.assertFalse(self.bridge.verify_mutation(receipt, ROOT, PATH, target, 7,
                    operation=operation, current=current))
                self.reader.verify_mutation.return_value = True
                self.assertTrue(self.bridge.verify_mutation(receipt, ROOT, PATH, target, 7,
                    operation=operation, current=current))
                self.connection.sendall.assert_called_once()

    def test_replace_uses_create_close_but_private_prepublication_generation(self):
        current = self.make()
        prepared = self.bridge.preflight_mutation(ROOT, PATH, PATH,
                                                  operation='replace', current=current)
        receipt = self.bridge.notify_mutation(ROOT, PATH, PATH, 7,
            operation='replace', prepared=prepared, current=current)
        self.connection.sendall.assert_called_once_with(index._new_file_records(ROOT, PATH))
        self.assertTrue(self.bridge.verify_mutation(receipt, ROOT, PATH, PATH, 7,
                                                   operation='replace', current=current))
        self.reader.verify_mutation.assert_called_once_with(self.reader.preflight_mutation.return_value,
            ROOT, PATH, PATH, 7, operation='replace', current=current)

    def test_prepared_receipt_cannot_change_root_paths_operation_or_service(self):
        current = self.make()
        target = '临时共享/renamed.bin'
        prepared = self.bridge.preflight_mutation(ROOT, PATH, target,
                                                  operation='rename', current=current)
        for root, source, destination, operation in (
                ({**ROOT, 'id': 'root_' + '2' * 24}, PATH, target, 'rename'),
                (ROOT, 'other', target, 'rename'), (ROOT, PATH, 'other', 'rename'),
                (ROOT, PATH, target, 'move')):
            with self.assertRaises(FileAccessError):
                self.bridge.notify_mutation(root, source, destination, 7,
                    operation=operation, prepared=prepared, current=current)
        self.listener.return_value = ('changed',)
        with self.assertRaises(FileAccessError):
            self.bridge.notify_mutation(ROOT, PATH, target, 7,
                operation='rename', prepared=prepared, current=current)
        self.connection.sendall.assert_not_called()

    def test_no_arbitrary_event_or_cross_parent_rename_or_same_path_move(self):
        for operation, target in (('delete', PATH), ('move', PATH), ('replace', 'other'),
                                  ('rename', 'elsewhere/name')):
            with self.assertRaises(FileAccessError):
                index._mutation_records(ROOT, PATH, target, operation=operation)


if __name__ == '__main__':
    unittest.main()
