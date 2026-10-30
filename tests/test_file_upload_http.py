"""Actual HTTP framing and POSIX staging; injected targets are not native proof.

The byte-only POSIX fixture supplies a private backend target without touching
NAS. A separate Linux class follows the real owner/MCP callback and production
writer through no-clobber publication, with explicit native proof fixtures.
"""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import test_integration_http as fixture
import test_api_file_executor as read_fixture
import test_api_file_write_backend as write_fixture
import test_mcp_completion_http as completion_fixture
from api_contracts import IMPLEMENTED_TOOLS, contract_catalog
from api_file_operations import FileAccessError
from api_file_transfers import UploadTarget
from api_file_executor import FileReadExecutor
from api_file_write_backend import FileWriteExecutor
from mcp_service import tool_catalog
from web_config import _Reader


ROOT = 'root_' + 'a' * 24
CONTENT = '/api/integration/files/transfers/transfer_' + 'a' * 32 + '/content'
DATA = b'PRIVATE-FILE-BYTES\x00\xff' * 8192
UPLOAD_TOOLS = ['prepare_file_upload', 'commit_file_upload', 'get_file_operation',
                'get_file_transfer', 'cancel_file_transfer']


class UploadBodyReaderTests(unittest.TestCase):
    def reader(self, data):
        connection = socket.socket()
        self.addCleanup(connection.close)
        base = io.BufferedReader(io.BytesIO(data))
        self.addCleanup(base.close)
        return _Reader(base, SimpleNamespace(connection=connection, deadline=time.monotonic() + 2))

    def test_large_body_is_streamed_in_bounded_blocks_and_zero_body_is_valid(self):
        blocks = list(self.reader(DATA).file_blocks(len(DATA)))
        self.assertEqual(b''.join(blocks), DATA)
        self.assertTrue(all(0 < len(block) <= 65536 for block in blocks))
        self.assertEqual(list(self.reader(b'').file_blocks(0)), [])

    def test_short_and_buffered_excess_body_never_finish_a_valid_iterator(self):
        for data, length in ((b'short', 10), (b'expectedEXCESS', 8), (b'x', 0)):
            with self.subTest(length=length), self.assertRaises(FileAccessError) as error:
                list(self.reader(data).file_blocks(length))
            self.assertEqual(error.exception.code, 'invalid_arguments')

    def test_absolute_deadline_is_checked_before_each_body_read(self):
        reader = self.reader(b'expected')
        reader.handler.deadline = time.monotonic() - 1
        with self.assertRaises(FileAccessError) as error:
            list(reader.file_blocks(8))
        self.assertEqual(error.exception.code, 'timeout')


class UploadHTTPBase(unittest.TestCase):
    setUp = fixture.IntegrationHTTPTests.setUp
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    browser = fixture.IntegrationHTTPTests.browser

    def machine_key(self, *, write=False, legacy=False):
        self.server.api_access.configure({'enabled': True})
        body = {'name': 'upload HTTP fixture', 'role': 'readwrite' if write else 'readonly',
                'scopes': ['files'], 'allowed_ips': ['127.0.0.0/8'], 'expires_days': 1,
                'file_roots': [{'id': ROOT, 'path': '', 'access': 'readwrite' if write else 'readonly'}]}
        if not legacy:
            # Store-only transport fixture. The production E2E class separately
            # validates owner grants against a current root catalog.
            body['tool_grants'] = UPLOAD_TOOLS if write else []
        value = self.server.api_access.create_key(body)
        return value

    def binary(self, path=CONTENT, *, key=None, method='PUT', data=b'', headers=None,
               remove=(), duplicates=(), peer='127.0.0.1', half_close=False):
        values = {'Host': self.host, 'Connection': 'close',
                  'Content-Type': 'application/octet-stream', 'Content-Length': str(len(data))}
        if key is not None:
            values['Authorization'] = 'Bearer ' + key['token']
        values.update(headers or {})
        for name in remove:
            values.pop(name, None)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5,
                                                source_address=(peer, 0))
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in list(values.items()) + list(duplicates):
                connection.putheader(name, value)
            connection.endheaders(data)
            if half_close:
                connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            result = response.read()
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            self.assertEqual(response_headers.get('connection'), 'close')
            self.assertEqual(response_headers.get('cache-control'), 'no-store')
            self.assertNotIn('set-cookie', response_headers)
            self.assertNotIn('access-control-allow-origin', response_headers)
            return response.status, json.loads(result.decode()) if result else None
        finally:
            connection.close()


class FileUploadHTTPRules(UploadHTTPBase):
    def test_bearer_required_and_cookie_csrf_or_developer_never_substitutes(self):
        key = self.machine_key()
        self.assertEqual(self.binary()[0], 401)
        self.assertEqual(self.binary(headers={'Cookie': self.cookie,
            'Origin': 'http://' + self.host, 'X-FNM-Request': '1'})[0], 401)
        self.assertEqual(self.binary(key=key, headers={'Origin': 'http://foreign.invalid'})[0], 403)
        self.assertEqual(self.binary(key=key)[0], 404)

    def test_fixed_route_and_methods_have_no_path_query_fragment_or_offsets(self):
        key = self.machine_key()
        for path in (CONTENT + '?path=public/file', CONTENT + '#fragment'):
            self.assertEqual(self.binary(path, key=key)[0], 400)
        for path in (CONTENT.replace('transfer_' + 'a' * 32, '../file'),
                     CONTENT.replace('transfer_', 'TRANSFER_')):
            self.assertEqual(self.binary(path, key=key)[0], 404)
        for method in ('POST', 'PATCH', 'DELETE', 'OPTIONS'):
            self.assertEqual(self.binary(key=key, method=method)[0], 405)
        for name in ('Range', 'Content-Range', 'Upload-Offset', 'X-Upload-Offset',
                     'If-Match', 'If-None-Match', 'Content-Encoding', 'Tus-Resumable'):
            with self.subTest(header=name):
                self.assertEqual(self.binary(key=key, headers={name: '1'})[0], 400)

    def test_strict_framing_length_limits_duplicates_and_no_encoding(self):
        key = self.machine_key()
        for length in ('', '-1', '+1', '01', '1.0', '8589934593', '99999999999'):
            with self.subTest(length=length):
                self.assertIn(self.binary(key=key, headers={'Content-Length': length})[0], (400, 413))
        self.assertEqual(self.binary(key=key, remove=('Content-Length',))[0], 400)
        self.assertEqual(self.binary(key=key, remove=('Content-Type',))[0], 400)
        for mime in ('application/json', 'text/plain', 'application/octet-stream; charset=utf-8'):
            self.assertEqual(self.binary(key=key, headers={'Content-Type': mime})[0], 400)
        for name, value in (('Content-Length', '0'), ('Content-Type', 'application/octet-stream'),
                            ('Authorization', 'Bearer ' + key['token'])):
            self.assertEqual(self.binary(key=key, duplicates=[(name, value)])[0], 400)
        for name in ('Transfer-Encoding', 'Expect'):
            self.assertEqual(self.binary(key=key, headers={name: 'chunked'})[0], 400)

    def test_concrete_mutations_register_without_expanding_old_upload_grants(self):
        self.assertTrue({'prepare_file_upload', 'commit_file_upload', 'get_file_operation'} <= IMPLEMENTED_TOOLS)
        names = {row['name'] for row in tool_catalog()}
        self.assertIn('prepare_file_upload', names)
        self.assertTrue({'prepare_file_overwrite', 'create_directory', 'rename_file', 'copy_file', 'move_file', 'trash_file'} <= names)
        self._patch(self.server.integration, 'file_root_catalog', return_value={
            'boundary_version': 3, 'status': 'authorization_plan', 'available': True,
            'roots': [{'id': ROOT, 'mount': '/drives/fixture/Users/admin', 'read_only': False}]})
        status, _, value = self.browser('POST', '/api/access/keys', {
            'name': 'explicit concrete writer', 'role': 'readwrite', 'scopes': ['files'],
            'file_roots': [{'id': ROOT, 'path': '', 'access': 'readwrite'}],
            'tool_grants': ['rename_file'], 'expires_days': 1})
        self.assertEqual(status, 200)
        self.assertEqual(value['key']['tool_grants'], ['rename_file'])
        old = self.machine_key(write=True)
        principal, reason = self.server.api_access.authorize('Bearer ' + old['token'], '127.0.0.1')
        self.assertEqual(reason, 'ok')
        self.assertNotIn('prepare_file_overwrite', {item['name'] for item in tool_catalog(principal)})
        upload = next(row for row in contract_catalog()['tools'] if row['name'] == 'prepare_file_upload')
        self.assertTrue(upload['implemented'])


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'pread') and hasattr(os, 'pwrite'),
                     'Linux real unnamed staging and descriptor byte transport required')
class FileUploadHTTPStaging(UploadHTTPBase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix='r1-upload-http-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manager = self.server.integration.file_operations.transfers
        self.key = self.machine_key(write=True)
        self.installs, self.counter, self.hook = [], 0, None
        self.acl = True

    def principal(self, key=None, *, peer='127.0.0.1'):
        key = self.key if key is None else key
        principal, reason = self.server.api_access.authorize('Bearer ' + key['token'], peer,
                                                            required_scope='files')
        self.assertEqual(reason, 'ok')
        principal['_peer'] = peer
        principal['_service_generation'] = self.server.integration._service_generation
        return principal

    def prepare(self, data=DATA, **options):
        self.counter += 1
        def staging(unused):
            with tempfile.TemporaryFile(dir=self.directory) as stream:
                return os.dup(stream.fileno())
        def authorize(principal, descriptor, **unused):
            if self.hook is not None:
                self.hook(principal, descriptor)
            return self.acl
        def install(principal, descriptor, **conditions):
            self.installs.append((os.pread(descriptor, len(data), 0), conditions))
            return 'file_http_fixture'
        target = UploadTarget(ROOT, 'public/new.bin', 'new.bin', staging, authorize, install)
        return self.manager.prepare_upload(self.principal(), target, size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(), idempotency_key='prepare:' + str(self.counter), **options)

    def test_exact_bytes_then_explicit_commit_and_same_key_idempotency(self):
        receipt = self.prepare()
        status, result = self.binary(receipt['content_path'], key=self.key, data=DATA)
        self.assertEqual(status, 200)
        self.assertEqual(result['transfer']['state'], 'verified')
        self.assertEqual(result['transfer']['received_bytes'], len(DATA))
        self.assertEqual(result['transfer']['verified_sha256'], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(self.installs, [])
        principal = self.principal()
        committed = self.manager.commit_upload(receipt['transfer_id'], principal, idempotency_key='commit:1')
        repeated = self.manager.commit_upload(receipt['transfer_id'], principal, idempotency_key='commit:1')
        self.assertTrue(committed['ok'])
        self.assertEqual(committed, repeated)
        self.assertEqual(self.installs, [(DATA, {'overwrite': False, 'expected_target_version': None})])
        raw = json.dumps(self.server.api_access.snapshot()['audit'])
        self.assertNotIn('PRIVATE-FILE-BYTES', raw)
        self.assertNotIn(self.key['token'], raw)

    def test_zero_file_and_sha_mismatch_never_auto_install(self):
        receipt = self.prepare(b'')
        self.assertEqual(self.binary(receipt['content_path'], key=self.key)[0], 200)
        receipt = self.prepare(b'expected')
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'not-that')[0], 422)
        self.assertEqual(self.manager.get_transfer(receipt['transfer_id'], self.principal())['transfer']['state'], 'failed')
        self.assertEqual(self.installs, [])

    def test_short_excess_mismatched_length_and_resend_are_not_committable(self):
        for data, length, half_close, expected in ((b'shor', 8, True, 400),
                (b'expectedEXCESS', 8, False, 400), (b'expected', 7, False, 404)):
            receipt = self.prepare(b'expected')
            status, _ = self.binary(receipt['content_path'], key=self.key, data=data,
                                    headers={'Content-Length': str(length)}, half_close=half_close)
            self.assertEqual(status, expected)
            with self.assertRaises(FileAccessError):
                self.manager.commit_upload(receipt['transfer_id'], self.principal(), idempotency_key='no-commit')
            self.manager.cancel_transfer(receipt['transfer_id'], self.principal())
        receipt = self.prepare(b'expected')
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 200)
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 404)
        self.assertEqual(self.installs, [])

    def test_current_peer_generation_revision_and_original_write_grant_are_required(self):
        receipt = self.prepare(b'expected')
        other = self.machine_key(write=True)
        self.assertEqual(self.binary(receipt['content_path'], key=other, data=b'expected')[0], 404)
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected', peer='127.0.0.2')[0], 404)
        rotated = self.server.api_access.rotate_key(self.key['id'])
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 401)
        self.key = rotated
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 404)
        self.assertEqual(self.installs, [])

    def test_permission_revision_expiry_and_shutdown_never_revive_a_transfer(self):
        receipt = self.prepare(b'expected')
        self.server.api_access.update_key({'id': self.key['id'], 'role': 'readonly',
            'file_roots': [{'id': ROOT, 'path': '', 'access': 'readonly'}], 'tool_grants': []})
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 404)
        self.key = self.machine_key(write=True)
        receipt = self.prepare(b'expected')
        self.now += 86401
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 401)
        self.key = self.machine_key(write=True)
        receipt = self.prepare(b'expected')
        self.server.integration.close()
        self.server.integration.open()
        self.assertEqual(self.binary(receipt['content_path'], key=self.key, data=b'expected')[0], 404)
        self.assertEqual(self.installs, [])

    def test_revocation_acl_and_cancel_during_stream_discard_results(self):
        for action in ('revoke', 'acl', 'cancel'):
            if action != 'revoke':
                self.key = self.machine_key(write=True)
            receipt = self.prepare(DATA)
            fired = []
            def hook(principal, descriptor):
                if not fired and os.fstat(descriptor).st_size:
                    fired.append(True)
                    if action == 'revoke':
                        self.server.api_access.revoke_key(self.key['id'])
                    elif action == 'acl':
                        self.acl = False
                    else:
                        self.manager.cancel_transfer(receipt['transfer_id'], principal)
            self.hook = hook
            self.assertIn(self.binary(receipt['content_path'], key=self.key, data=DATA)[0], (401, 403, 404))
            self.assertTrue(fired)
            self.hook, self.acl = None, True
        self.assertEqual(self.installs, [])

    def test_old_role_or_no_exact_tool_cannot_prepare_internal_upload(self):
        for key in (self.machine_key(write=True, legacy=True), self.machine_key()):
            with self.assertRaises(FileAccessError):
                target = UploadTarget(ROOT, 'public/new.bin', 'new.bin',
                    lambda unused: self.fail('must reject before creating staging'), lambda *a, **kw: True,
                    lambda *a, **kw: self.fail('must not install'))
                self.manager.prepare_upload(self.principal(key), target, size_bytes=0,
                    sha256=hashlib.sha256(b'').hexdigest(), idempotency_key='no-old-key')


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_TMPFILE')
                     and { 'prepare_file_upload', 'commit_file_upload', 'get_file_operation' }
                         <= IMPLEMENTED_TOOLS,
                     'Registered production writer and Linux real O_TMPFILE publication required')
class ProductionFileUploadHTTPTests(completion_fixture.CompletionHTTPBase):
    """Owner grant → MCP/REST → real writer/fd PUT → no-clobber publish/index.

    Only the native subject and index replies are constructor fixtures. The
    production writer, descriptor traversal, atomic link, transport and schemas
    execute without replacing their filesystem primitives or central callback.
    """

    binary = UploadHTTPBase.binary

    def setUp(self):
        super().setUp()
        read_fixture.OpenedFileTests.setUp(self)
        del self.key
        integration = self.server.integration
        integration.file_operations.close()
        executor = FileReadExecutor(None, root=self.root,
            authorizer=integration._is_current, authority=self.authority)
        executor.write_operations.close()
        self.index = write_fixture.IndexFixture()
        executor.write_operations = FileWriteExecutor(None, root=self.root,
            authorizer=integration._is_current, transfers=executor.transfers,
            authority=write_fixture.WriteAuthorityFixture(), index=self.index,
            capacity=lambda descriptor, remaining: None)
        integration.file_operations = executor
        self._patch(integration, 'file_root_catalog', return_value={
            'available': True, 'status': 'authorization_plan', 'boundary_version': 3,
            'roots': [dict(read_fixture.ROW)]})
        self.file_key = self.key('readwrite', ['files'], UPLOAD_TOOLS + ['get_file_info'],
            roots=[{'id': read_fixture.ROOT, 'path': '', 'access': 'readwrite'}])

    def prepare(self, *, key=None, **changes):
        arguments = {'root_id': read_fixture.ROOT, 'path': 'new.bin',
            'size_bytes': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest(),
            'idempotency_key': 'http:production:prepare', **changes}
        return self.good(self.call(key or self.file_key, 'prepare_file_upload', arguments))

    def commit(self, receipt, *, rest=False):
        return self.call(self.file_key, 'commit_file_upload', {
            'transfer_id': receipt['transfer_id'], 'idempotency_key': 'http:production:commit'}, rest=rest)

    def test_real_production_prepare_put_commit_and_index_confirmation(self):
        prepared = self.prepare()
        self.assertFalse((self.admin / 'new.bin').exists())
        status, uploaded = self.binary(prepared['content_path'], key=self.file_key, data=DATA)
        self.assertEqual(status, 200)
        self.assertEqual(uploaded['transfer']['state'], 'verified')
        self.assertFalse((self.admin / 'new.bin').exists())
        committed = self.good(self.commit(prepared, rest=True))
        self.assertEqual(committed['operation']['state'], 'pending')
        self.assertEqual((self.admin / 'new.bin').read_bytes(), DATA)
        self.assertEqual(len(self.index.notifications), 1)
        replay = self.good(self.commit(prepared))
        self.assertEqual(replay['operation']['operation_id'], committed['operation']['operation_id'])
        self.assertEqual(len(self.index.notifications), 1)
        arguments = {'operation_id': committed['operation']['operation_id']}
        pending = self.good(self.call(self.file_key, 'get_file_operation', arguments))
        self.assertTrue(pending['may_wake_disks'])
        self.assertEqual(pending['operation']['state'], 'pending')
        self.index.ready = True
        completed = self.good(self.call(self.file_key, 'get_file_operation', arguments, rest=True))
        self.assertEqual(completed['operation']['state'], 'completed')
        self.assertRegex(completed['operation']['result_version'], '^file_[0-9a-f]{48}$')

    def test_file_range_parent_access_and_schema_cannot_authorize_public_or_overwrite(self):
        for changes in ({'path': '../public/new.bin'}, {'path': ''},
                        {'overwrite': True}, {'expected_target_version': 'file_version'},
                        {'confirmed': True}, {'size_bytes': 8 * 1024 ** 3 + 1}):
            arguments = {'root_id': read_fixture.ROOT, 'path': 'new.bin',
                'size_bytes': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest(),
                'idempotency_key': 'reject:write', **changes}
            self.bad(self.call(self.file_key, 'prepare_file_upload', arguments))
        leaf = self.key('readwrite', ['files'], UPLOAD_TOOLS,
            roots=[{'id': read_fixture.ROOT, 'path': 'new.bin', 'access': 'readwrite'}])
        arguments = {'root_id': read_fixture.ROOT, 'path': 'new.bin',
            'size_bytes': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest(),
            'idempotency_key': 'reject:parent'}
        self.bad(self.call(leaf, 'prepare_file_upload', arguments))
        self.assertFalse((self.admin / 'new.bin').exists())
        self.assertEqual(self.index.notifications, [])

    def test_concurrent_target_is_preserved_and_failed_operation_is_not_completion(self):
        prepared = self.prepare()
        self.assertEqual(self.binary(prepared['content_path'], key=self.file_key, data=DATA)[0], 200)
        target = self.admin / 'new.bin'
        target.write_bytes(b'keep existing user file')
        result = self.commit(prepared)
        value = result['structuredContent']
        self.assertEqual(value['operation']['state'], 'failed')
        self.assertEqual(value['operation']['error']['code'], 'target_exists')
        self.assertEqual(target.read_bytes(), b'keep existing user file')
        self.assertEqual(self.index.notifications, [])

    def test_revocation_after_byte_receive_prevents_publication(self):
        prepared = self.prepare()
        self.assertEqual(self.binary(prepared['content_path'], key=self.file_key, data=DATA)[0], 200)
        self.server.api_access.revoke_key(self.file_key['id'])
        status, _, _ = self.rest_action(self.file_key, 'commit_file_upload', {
            'transfer_id': prepared['transfer_id'], 'idempotency_key': 'revoked:commit'})
        self.assertEqual(status, 401)
        self.assertFalse((self.admin / 'new.bin').exists())
        self.assertEqual(self.index.notifications, [])


if __name__ == '__main__':
    unittest.main()
