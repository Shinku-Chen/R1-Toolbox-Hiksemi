"""Fixed native metadata protocol and full positive logical ACL subset evidence."""

from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_provider as files
from api_file_operations import FileAccessError


ROOT = 'root_' + 'a' * 24
MOUNT = '/drives/data/Users'
IDENTITY = {'available': True, 'status': 'verified', 'pid': 12, 'start_ticks': 88,
            'socket_inode': '555', 'connect_host': '127.0.0.1'}
PRIVATE = 'PRIVATE-NATIVE-SESSION-FIXTURE'
CATALOG = {'available': True, 'boundary_version': 2, 'roots': [
    {'id': ROOT, 'mount': MOUNT, 'label': 'fixture', 'read_only': False}]}


def frame(path=MOUNT, **changes):
    value = {'path': path, 'isdir': 1, 'size': 0, 'ctime': 123, 'mtime': 456}
    value.update(changes)
    return value


def listing(*names, **changes):
    value = {'mtime': 456, 'file_attr': 0, 'list': [
        {'name': name, 'isdir': 1, 'size': 0, 'ctime': 123, 'mtime': 456} for name in names]}
    value.update(changes)
    return value


class Connection:
    def __init__(self, value=None, status=200, certificate=b'CERT'):
        raw = json.dumps(frame() if value is None else value).encode('utf-8')
        self.data = (f'HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n'
                     f'Content-Length: {len(raw)}\r\n\r\n').encode() + raw
        self.sent, self.closed, self.certificate = [], False, certificate

    def connect(self, address):
        self.address = address

    def settimeout(self, value):
        self.timeout = value

    def getpeercert(self, binary_form=False):
        return self.certificate

    def sendall(self, value):
        self.sent.append(value)

    def recv(self, size):
        result, self.data = self.data[:size], self.data[size:]
        return result

    def close(self):
        self.closed = True


class RuleTests(unittest.TestCase):
    def test_positive_subset_is_exact_native_owner_public_and_no_uid_claim(self):
        for path in ('', 'admin', 'public', 'admin/docs/a', 'public/照片.jpg'):
            self.assertTrue(files.audited_owner_read_rule(path))
        for path in ('other', 'Admin', 'admin-private', '/admin', 'admin/../other', 'admin//a'):
            self.assertFalse(files.audited_owner_read_rule(path))

    def test_exact_info_success_has_no_assumed_code_zero_and_echoes_path(self):
        value = files.project_protocol_frame(frame(), MOUNT, '', 'get_info')
        self.assertTrue(value['native_success'])
        self.assertIsNone(value['native_code'])
        self.assertEqual(value['success_contract'], 'get_info_structural_v260907')
        self.assertFalse(value['acl_proven'])
        for changes in ({'path': MOUNT + '/other'}, {'isdir': True}, {'size': 12},
                        {'mtime': '456'}, {'code': -1110}, {'isdir': 0}):
            self.assertFalse(files.project_protocol_frame(frame(**changes), MOUNT, '',
                                                          'get_info')['native_success'])

    def test_projection_never_returns_native_name_content_path_message_or_token(self):
        value = frame(name=PRIVATE, message=PRIVATE, token=PRIVATE,
                      list=[{'path': MOUNT + '/' + PRIVATE, 'name': PRIVATE}])
        result = files.project_protocol_frame(value, MOUNT, '', 'get_list')
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn(MOUNT, json.dumps(result))
        self.assertTrue(result['all_list_paths_contained'])
        self.assertEqual(result['field_shapes']['name'], 'string')
        result = files.project_protocol_frame(frame(list=[{'path': MOUNT + '/../secret'}]),
                                             MOUNT, '', 'get_list')
        self.assertFalse(result['all_list_paths_contained'])

    def test_native_list_uses_literal_names_and_needs_no_path_or_code(self):
        result = files.project_protocol_frame(listing('admin', '照片'), MOUNT, '', 'get_list')
        self.assertTrue(result['native_success'])
        self.assertEqual(result['success_contract'], 'get_list_name_children_v260907')
        self.assertIsNone(result['native_code'])
        self.assertIsNone(result['path_matches_requested'])
        self.assertTrue(result['all_list_paths_contained'])
        self.assertEqual(files._native_list_paths([{'name': '%2e%2e'}], MOUNT, 'admin'),
                         [MOUNT + '/admin/%2e%2e'])
        value = listing('报告.txt')
        value['list'][0]['path'] = MOUNT + '/admin/报告.txt'
        self.assertTrue(files.project_protocol_frame(value, MOUNT, 'admin', 'get_list')['native_success'])
        self.assertNotIn('报告', json.dumps(result, ensure_ascii=False))
        self.assertFalse(result['acl_proven'])
        self.assertFalse(result['opened_object_proven'])

    def test_native_list_refuses_traversal_separators_duplicates_and_ambiguous_paths(self):
        bad_rows = [None, {}, {'path': MOUNT + '/admin'}, {'name': ''}, {'name': '.'},
                    {'name': '..'}, {'name': '../other'}, {'name': 'a/b'}, {'name': 'a\\b'},
                    {'name': 'a\n'}, {'name': '\ud800'}, {'name': '照' * 86},
                    {'name': 'admin', 'path': MOUNT + '/other'},
                    {'name': 'admin', 'path': 'admin'}, {'name': 'admin', 'path': None}]
        for row in bad_rows:
            with self.subTest(row=row):
                value = listing()
                value['list'] = [row]
                result = files.project_protocol_frame(value, MOUNT, '', 'get_list')
                self.assertFalse(result['native_success'])
                self.assertFalse(result['all_list_paths_contained'])
        self.assertFalse(files.project_protocol_frame(listing('admin', 'admin'), MOUNT, '',
                                                      'get_list')['native_success'])

    def test_list_success_requires_metadata_and_rejects_error_or_bad_pagination(self):
        self.assertTrue(files.project_protocol_frame(listing(), MOUNT, '', 'get_list')['native_success'])
        for changes in ({'mtime': True}, {'file_attr': -1}, {'count': 2}, {'count': True},
                        {'totalpage': -1}, {'totalpage': True}, {'code': True}, {'code': -1110},
                        {'path': MOUNT + '/other'}, {'list': [{'name': 'x'}] * 257}):
            with self.subTest(changes=changes):
                result = files.project_protocol_frame(listing('admin', **changes), MOUNT, '', 'get_list')
                self.assertFalse(result['native_success'])
        self.assertFalse(files.project_protocol_frame({'code': 0}, MOUNT, '', 'get_list')['native_success'])
        self.assertFalse(files.project_protocol_frame({'code': 0, 'data': listing('admin', code=-1)},
                                                      MOUNT, '', 'get_list')['native_success'])

    def test_owner_read_is_fixed_columns_no_token_or_other_account(self):
        with tempfile.TemporaryDirectory(prefix='r1-file-owner-') as directory:
            root = Path(directory)
            path = root / 'tmp/nand_config/USER_AUTH_DB_101'
            path.parent.mkdir(parents=True)
            db = sqlite3.connect(path)
            db.executescript('CREATE TABLE tb_user_auth(user_id INTEGER,user_type INTEGER,user_name TEXT);'
                             'CREATE TABLE tb_terminal_token(token TEXT);')
            db.execute('INSERT INTO tb_user_auth VALUES(100000,0,?)', ('admin',))
            db.execute('INSERT INTO tb_terminal_token VALUES(?)', (PRIVATE,))
            db.commit()
            self.assertTrue(files._fixed_owner(root))
            db.execute("UPDATE tb_user_auth SET user_name='other'")
            db.commit()
            self.assertFalse(files._fixed_owner(root))
            db.executescript('DROP TABLE tb_user_auth; CREATE VIEW tb_user_auth AS '
                             'SELECT 100000 AS user_id,0 AS user_type,token AS user_name '
                             'FROM tb_terminal_token;')
            self.assertFalse(files._fixed_owner(root))
            db.close()

    def test_missing_sqlite_and_binary_fail_closed(self):
        with mock.patch.object(files, 'sqlite3', None):
            self.assertFalse(files._fixed_owner('/'))
        self.assertFalse(files._firmware_identity('/no-such-fixture-root'))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.session = SimpleNamespace(user_id=100000, derive_access_token=lambda: PRIVATE)
        self.listener = mock.Mock(return_value=IDENTITY)
        self.cert = mock.Mock(return_value=b'CERT')
        self.binary = mock.Mock(return_value=True)
        self.auth = lambda: True
        self.now = 1.0

    def client(self):
        return files.VendorFileProtocolClient(root='/FIXTURE',
            session_reader=lambda **kw: (self.session, {'available': True, 'status': 'ready'}),
            listener_check=self.listener, certificate_reader=self.cert,
            socket_factory=lambda *a: self.connection,
            tls_context=SimpleNamespace(wrap_socket=lambda raw, server_hostname: raw),
            clock=lambda: self.now, binary_check=self.binary)

    def request(self, **options):
        return self.client().request(MOUNT, '', 'get_info', deadline=13, authorizer=self.auth, **options)

    def test_real_fixed_framing_uses_existing_owner_tls_and_no_upload_repair_parameters(self):
        self.assertEqual(self.request(), frame())
        request = self.connection.sent[0].decode()
        self.assertTrue(request.startswith('GET /rest/1.0/file?action=get_info&path='))
        self.assertNotIn('size=', request)
        self.assertNotIn('append=', request)
        self.assertNotIn('temp_suffix=', request)
        self.assertIn('Connection: close', request)
        self.assertEqual(self.connection.address, ('127.0.0.1', 20443))
        self.assertTrue(self.connection.closed)
        self.assertGreaterEqual(self.binary.call_count, 3)

    def test_native_list_request_uses_fixed_one_based_page_and_page_size(self):
        self.connection = Connection(listing('admin'))
        value = self.client().request(MOUNT, '', 'get_list', deadline=13, authorizer=self.auth)
        self.assertTrue(files.project_protocol_frame(value, MOUNT, '', 'get_list')['native_success'])
        request = self.connection.sent[0].decode('ascii')
        self.assertIn('&page=1&pagesize=2&', request)
        self.assertNotIn('limit=', request)
        self.assertNotIn('offset=', request)

    def test_route_component_action_and_authorization_rejection_touch_nothing(self):
        for mount, component, action in (('/etc', '', 'get_info'), (MOUNT, '../other', 'get_info'),
                                          (MOUNT, '', 'delete'), (MOUNT, 'other', 'get_list')):
            with self.assertRaises(files.BusinessTransportError):
                self.client().request(mount, component, action, deadline=13,
                                      authorizer=self.auth)
        self.auth = lambda: 1
        with self.assertRaises(files.BusinessTransportError):
            self.request()
        self.assertFalse(self.listener.called)
        self.assertFalse(self.connection.sent)

    def test_pin_listener_binary_and_current_grant_are_all_required_before_token_send(self):
        for field in ('certificate', 'binary', 'listener'):
            with self.subTest(field=field):
                self.connection = Connection(certificate=b'BAD' if field == 'certificate' else b'CERT')
                self.binary.return_value = field != 'binary'
                self.listener.return_value = (dict(IDENTITY, start_ticks=None)
                                              if field == 'listener' else IDENTITY)
                with self.assertRaises(files.BusinessTransportError):
                    self.request()
                self.assertFalse(self.connection.sent)
        self.binary.return_value = True
        self.listener.return_value = IDENTITY
        self.connection = Connection()
        values = iter((True, True, False))
        self.auth = lambda: next(values)
        with self.assertRaises(files.BusinessTransportError):
            self.request()
        self.assertFalse(self.connection.sent)

    def test_default_binary_gate_uses_listener_tuple_pid_and_exact_running_inode(self):
        disk = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_dev=8, st_ino=123,
                               st_size=files.FIRMWARE_SIZE, st_mtime_ns=11, st_ctime_ns=12)
        opened = []
        disk_path = Path('/FIXTURE').absolute() / 'histor/bin/emhistor'
        running_path = Path('/FIXTURE').absolute() / 'proc/12/exe'

        def binary_stat(path, **options):
            value = Path(path)
            opened.append(value)
            self.assertIn(value, (disk_path, running_path))
            return disk

        client = self.client()
        client._binary = files._runtime_binary_matches
        with mock.patch.object(files.os, 'stat', side_effect=binary_stat), \
             mock.patch.object(files, '_firmware_identity', return_value=True):
            self.assertEqual(client.request(MOUNT, '', 'get_info', deadline=13,
                                           authorizer=self.auth), frame())
        self.assertGreaterEqual(opened.count(running_path), 3)
        self.assertEqual(len(self.connection.sent), 1)

        self.connection = Connection()
        running = SimpleNamespace(**vars(disk))
        running.st_ino += 1
        with mock.patch.object(files.os, 'stat', side_effect=(disk, running)), \
             mock.patch.object(files, '_firmware_identity', return_value=True), \
             self.assertRaises(files.BusinessTransportError) as caught:
            self.request_with_default_binary_gate()
        self.assertEqual(caught.exception.reason, 'identity_unverified')
        self.assertFalse(self.connection.sent)

    def request_with_default_binary_gate(self):
        client = self.client()
        client._binary = files._runtime_binary_matches
        return client.request(MOUNT, '', 'get_info', deadline=13, authorizer=self.auth)

    def test_last_identity_scan_cannot_mask_revocation_and_absolute_deadline(self):
        checks = 0
        def listener(**kw):
            nonlocal checks
            checks += 1
            if checks == 3:
                self.now = 14
            return IDENTITY
        self.listener.side_effect = listener
        with self.assertRaises(files.BusinessTransportError) as caught:
            self.request()
        self.assertEqual(caught.exception.reason, 'timeout')
        self.assertFalse(self.connection.sent)

    def test_rejected_or_redirect_response_is_fixed_error_without_native_body(self):
        self.connection = Connection({'error': PRIVATE}, status=302)
        with self.assertRaises(files.BusinessTransportError) as caught:
            self.request()
        self.assertEqual(caught.exception.reason, 'redirect_refused')
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_policy_revoked_after_response_discards_all_native_fields(self):
        def listener(**kw):
            if len(self.connection.sent):
                self.auth = lambda: False
            return IDENTITY
        self.listener.side_effect = listener
        self.auth = lambda: len(self.connection.sent) == 0
        with self.assertRaises(files.BusinessTransportError) as caught:
            self.request()
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(len(self.connection.sent), 1)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls = []
        self.catalog = copy.deepcopy(CATALOG)
        self.valid = True
        def request(mount, component, action, **options):
            self.assertTrue(options['authorizer']())
            self.calls.append((mount, component, action))
            return frame(path=mount + ('/' + component if component else ''), name=PRIVATE)
        self.client = SimpleNamespace(clock=lambda: self.now, request=request)

    def collect(self, **options):
        return files.collect_file_protocol_probe(None, authorizer=lambda: self.valid,
            root_collector=lambda unused: copy.deepcopy(self.catalog), client=self.client,
            firmware_reader=lambda **kw: True, root_guard=lambda *a: nullcontext(), **options)

    def test_complete_fixed_probe_all_roots_is_bounded_and_strips_native_values(self):
        result = self.collect()
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(result['complete'])
        self.assertTrue(result['effects']['enumerates_user_directories'])
        self.assertFalse(result['effects']['opens_user_files'])
        self.assertFalse(result['file_access_available'])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn(MOUNT, json.dumps(result))
        self.assertNotIn('access_token', json.dumps(result))

    def test_firmware_mismatch_never_sends_unreviewed_native_action(self):
        result = files.collect_file_protocol_probe(None, authorizer=lambda: True,
            root_collector=lambda unused: CATALOG, client=self.client,
            firmware_reader=lambda **kw: False, root_guard=lambda *a: nullcontext())
        self.assertFalse(result['available'])
        self.assertIn('runtime_firmware_mismatch', result['blockers'])
        self.assertFalse(self.calls)

    def test_missing_peer_authorization_cannot_acquire_lock_or_read_firmware(self):
        reader = mock.Mock()
        with self.assertRaises(files.BusinessTransportError):
            files.collect_file_protocol_probe(None, authorizer=None, firmware_reader=reader)
        reader.assert_not_called()

    def test_catalog_change_or_window_expiry_discards_whole_result(self):
        original = self.client.request
        def change(*args, **kw):
            result = original(*args, **kw)
            self.valid = False
            return result
        self.client.request = change
        with self.assertRaises(files.BusinessTransportError):
            self.collect()
        self.assertEqual(len(self.calls), 1)

    def test_missing_users_or_mount_refusal_is_not_a_listener_identity_failure(self):
        for code in ('not_found', 'root_unavailable'):
            with self.subTest(code=code):
                guard = mock.Mock(side_effect=FileAccessError(code))
                result = files.collect_file_protocol_probe(None, authorizer=lambda: True,
                    root_collector=lambda unused: CATALOG, client=self.client,
                    firmware_reader=lambda **kw: True, root_guard=guard)
                self.assertTrue(result['complete'])
                self.assertEqual(result['requests_completed'], 0)
                self.assertEqual(len(result['roots'][0]['queries']), 6)
                self.assertTrue(all(row['reason'] == 'root_open_unverified'
                                    and row['open_error_code'] == code
                                    and row['http_status'] is None
                                    for row in result['roots'][0]['queries']))
        self.assertFalse(self.calls)


class NativeAuthorityTests(unittest.TestCase):
    def test_transport_failures_use_existing_availability_codes_without_retry_or_acl_claim(self):
        for reason, code in (('authorization_denied', 'invalid_key'), ('timeout', 'timeout'),
                             ('busy', 'resource_busy'), ('identity_unverified', 'native_unavailable'),
                             ('credential_unavailable', 'native_unavailable'),
                             ('transport_failed', 'native_unavailable')):
            with self.subTest(reason=reason):
                request = mock.Mock(side_effect=files.BusinessTransportError(reason))
                client = SimpleNamespace(clock=lambda: 0, request=request)
                authority = files.NativeOwnerReadAuthority(None, client=client,
                                   root_collector=lambda unused: copy.deepcopy(CATALOG),
                                   root_guard=lambda *a: nullcontext())
                with mock.patch.object(files, '_fixed_owner', return_value=True), \
                     mock.patch.object(files, '_firmware_identity', return_value=True), \
                     self.assertRaises(FileAccessError) as caught:
                    authority.authorize(ROOT, 'admin/sample', lambda: True)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(PRIVATE, json.dumps(caught.exception.result()))
                request.assert_called_once()

    def test_positive_native_root_plus_exact_owner_and_binary_maps_only_audited_subtree(self):
        client = SimpleNamespace(clock=lambda: 0, request=mock.Mock(return_value=frame()))
        authority = files.NativeOwnerReadAuthority(None, root='/fixture', client=client,
                           root_collector=lambda unused: copy.deepcopy(CATALOG),
                           root_guard=lambda *a: nullcontext())
        with mock.patch.object(files, '_fixed_owner', return_value=True), \
             mock.patch.object(files, '_firmware_identity', return_value=True):
            self.assertEqual(authority.authorize(ROOT, 'admin/sample', lambda: True)['mount'], MOUNT)
            with self.assertRaises(FileAccessError) as caught:
                authority.authorize(ROOT, 'other/sample', lambda: True)
            self.assertEqual(caught.exception.code, 'unsupported_path_class')
            client.request.assert_called_once()

    def test_native_denial_unknown_shape_or_missing_owner_never_grants_local_access(self):
        for value in ({'code': 0}, frame(code=-1110), frame(path='/other'), frame(isdir=0)):
            client = SimpleNamespace(clock=lambda: 0, request=mock.Mock(return_value=value))
            authority = files.NativeOwnerReadAuthority(None, client=client,
                               root_collector=lambda unused: copy.deepcopy(CATALOG),
                           root_guard=lambda *a: nullcontext())
            with mock.patch.object(files, '_fixed_owner', return_value=True), \
                 mock.patch.object(files, '_firmware_identity', return_value=True), \
                 self.assertRaises(FileAccessError):
                authority.authorize(ROOT, 'public/a', lambda: True)
