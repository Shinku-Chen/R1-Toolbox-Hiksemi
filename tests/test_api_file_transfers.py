"""Real temporary-file byte I/O; injected ACL fixtures are not NAS acceptance."""

import copy
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_contracts import contract_catalog
from api_file_operations import FileAccessError, assert_file_grant
from api_file_transfers import DownloadSource, FileTransfers, UploadTarget, _byte_range


ROOT = 'root_' + 'a' * 24
DATA = bytes(range(256)) * 600


def principal(write=False):
    tools = ['prepare_file_download', 'get_file_transfer', 'cancel_file_transfer']
    if write:
        tools += ['prepare_file_upload', 'commit_file_upload', 'get_file_operation']
    return {'key_id': '1' * 24, 'grant_revision': 1, 'credential_generation': 1,
            '_peer': '127.0.0.1', 'role': 'readwrite' if write else 'readonly', 'policy_version': 3,
            'scopes': ['files'], 'tool_grants': tools, 'docker_projects': [],
            'file_roots': [{'id': ROOT, 'path': '', 'access': 'readwrite' if write else 'readonly'}]}


class FileTransferRuleTests(unittest.TestCase):
    def test_range_parser_covers_whole_open_suffix_empty_and_unsatisfiable(self):
        for header, size, expected in ((None, 10, (200, 0, 10)), ('bytes=1-4', 10, (206, 1, 4)),
                                     ('bytes=8-99', 10, (206, 8, 2)), ('bytes=8-', 10, (206, 8, 2)),
                                     ('bytes=-3', 10, (206, 7, 3)), ('bytes=-99', 10, (206, 0, 10)),
                                     ('bytes=10-', 10, (416, 0, 0)), ('bytes=4-1', 10, (416, 0, 0)),
                                     ('bytes=-0', 10, (416, 0, 0)), ('bytes=0-', 0, (416, 0, 0)),
                                     (None, 0, (200, 0, 0))):
            with self.subTest(header=header, size=size):
                self.assertEqual(_byte_range(header, size), expected)

    def test_range_parser_never_accepts_headers_or_multiple_ranges(self):
        for value in ('bytes=0-2,4-6', 'bytes=0-1\r\nCookie: secret', 'bytes=-',
                      'bytes= -1', 'bytes=+1-2', 'bytes=1.0-2', [], 1, 'a' * 100):
            with self.subTest(value=value), self.assertRaises(FileAccessError):
                _byte_range(value, 10)

    def test_real_peer_and_current_authorization_are_required_before_opener(self):
        opened = mock.Mock(side_effect=AssertionError('no backend open'))
        source = DownloadSource(ROOT, 'owner/data.bin', 'data.bin', opened, lambda *a, **kw: True)
        manager = FileTransfers(lambda unused: True)
        for peer in (None, '', '8.8.8.8', '127.0.0.1\r\n', 'localhost'):
            with self.subTest(peer=peer), self.assertRaises(FileAccessError):
                manager.prepare_download(dict(principal(), _peer=peer), source)
        denied = FileTransfers(lambda unused: False)
        with self.assertRaises(FileAccessError):
            denied.prepare_download(principal(), source)
        opened.assert_not_called()

    def test_path_range_role_and_exact_tool_are_intersected(self):
        allowed = principal(write=True)
        allowed['file_roots'][0]['path'] = 'owner'
        assert_file_grant(allowed, 'prepare_file_download', ROOT, 'owner/资料')
        for path in ('owner-private/secret', '../owner/secret', '/owner', 'owner/../other'):
            with self.subTest(path=path), self.assertRaises(FileAccessError):
                assert_file_grant(allowed, 'prepare_file_download', ROOT, path)
        readonly = principal()
        with self.assertRaises(FileAccessError):
            assert_file_grant(readonly, 'prepare_file_upload', ROOT, 'owner/new', write=True)
        allowed['tool_grants'].remove('prepare_file_download')
        with self.assertRaises(FileAccessError):
            assert_file_grant(allowed, 'prepare_file_download', ROOT, 'owner/资料')

    def test_no_backend_or_upload_argument_can_activate_a_fake_executor(self):
        manager = FileTransfers(lambda unused: True)
        with self.assertRaises(FileAccessError) as caught:
            manager.prepare_download(principal(), {'root': '/', 'acl_verified': True})
        self.assertEqual(caught.exception.code, 'secure_open_unavailable')
        with self.assertRaises(FileAccessError) as caught:
            manager.prepare_upload(principal(write=True), None, size_bytes=0,
                                   sha256='0' * 64, idempotency_key='test')
        self.assertEqual(caught.exception.code, 'write_backend_unverified')


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'pread') and hasattr(os, 'pwrite'),
                     'Linux/POSIX descriptor transport required')
class FileTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-file-transfers-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'payload.bin'
        self.file.write_bytes(DATA)
        self.now = 0.0
        self.valid = self.acl = True
        self.manager = FileTransfers(lambda unused: self.valid, clock=lambda: self.now,
                                     wall_clock=lambda: 1793232000 + self.now)
        self.addCleanup(self.manager.close)
        self.p = principal()
        self.opened = []
        self.source = DownloadSource(ROOT, 'owner/payload.bin', '视频.bin', self.open_reader,
                                     lambda *args, **kw: self.acl)
        self.schemas = {row['name']: row['outputSchema'] for row in contract_catalog()['tools']}
        self.installs = []

    def open_reader(self, unused):
        descriptor = os.open(self.file, os.O_RDONLY)
        self.opened.append(descriptor)
        return descriptor

    def valid_result(self, tool, value):
        Draft202012Validator(self.schemas[tool]).validate(value)
        return value

    def prepare(self):
        result = self.manager.prepare_download(self.p, self.source)
        self.valid_result('prepare_file_download', result)
        return result['transfer_id']

    def status(self, identifier):
        return self.valid_result('get_file_transfer', self.manager.get_transfer(identifier, self.p))['transfer']

    def assert_code(self, code, action):
        with self.assertRaises(FileAccessError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def target(self):
        def staging(unused):
            with tempfile.TemporaryFile(dir=self.root) as stream:
                return os.dup(stream.fileno())
        def install(unused, descriptor, **conditions):
            # This fixture verifies byte/protocol dispatch, not native indexing
            # or quota. Production targets use a separate native-index gate.
            self.installs.append((os.pread(descriptor, len(DATA), 0), conditions))
            return 'file_installed_fixture'
        return UploadTarget(ROOT, 'owner/new.bin', 'new.bin', staging,
                            lambda *args, **kw: self.acl, install)

    def upload(self, data=DATA, **changes):
        self.p = principal(write=True)
        return self.manager.prepare_upload(self.p, self.target(), size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(), idempotency_key='prepare-1', **changes)

    def test_complete_download_matches_bytes_and_satisfies_declared_contract(self):
        identifier = self.prepare()
        output = io.BytesIO()
        response = self.manager.open_download(identifier, self.p)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Length'], str(len(DATA)))
        self.assertIn('filename*=UTF-8', response.headers['Content-Disposition'])
        self.assertEqual(response.send(output.write), len(DATA))
        self.assertEqual(output.getvalue(), DATA)
        self.assertEqual(self.status(identifier)['state'], 'completed')
        self.assertEqual(self.status(identifier)['sent_bytes'], len(DATA))

    def test_head_never_consumes_or_extends_first_get_window(self):
        identifier = self.prepare()
        initial = self.status(identifier)
        self.now += 100
        with mock.patch('os.pread', side_effect=AssertionError('HEAD must not read bytes')):
            response = self.manager.open_download(identifier, self.p, head=True, range_header='invalid')
            response.close()
        current = self.status(identifier)
        self.assertEqual(response.status, 200)
        self.assertEqual(current['state'], 'prepared')
        self.assertEqual(current['first_get_deadline'], initial['first_get_deadline'])
        self.assertIsNone(current['transfer_deadline'])

    def test_single_ranges_are_exact_and_cumulative_bytes_do_not_fake_completion(self):
        identifier = self.prepare()
        for header, expected in [('bytes=0-9', DATA[:10]), ('bytes=10-', DATA[10:]),
                                 ('bytes=-17', DATA[-17:])]:
            output = io.BytesIO()
            response = self.manager.open_download(identifier, self.p, range_header=header)
            self.assertEqual(response.status, 206)
            response.send(output.write)
            self.assertEqual(output.getvalue(), expected)
            self.assertEqual(self.status(identifier)['state'], 'ready')
        self.assertGreater(self.status(identifier)['sent_bytes'], len(DATA))

    def test_unsatisfiable_or_malformed_range_leaks_no_fd_or_starts_deadline(self):
        identifier = self.prepare()
        response = self.manager.open_download(identifier, self.p, range_header='bytes=999999-')
        self.assertEqual(response.status, 416)
        self.assertEqual(response.headers['Content-Range'], 'bytes */' + str(len(DATA)))
        response.close()
        for header in ('bytes=0-1,4-5', 'bytes=-', 'Bytes=0-1', 'bytes=a-b', 'bytes= 0-1'):
            self.assert_code('unsupported_range', lambda: self.manager.open_download(identifier, self.p,
                              range_header=header))
            with self.assertRaises(OSError):
                os.fstat(self.opened[-1])
        self.assertEqual(self.status(identifier)['state'], 'prepared')
        self.assertIsNone(self.status(identifier)['transfer_deadline'])

    def test_missing_peer_and_wrong_key_revision_generation_or_peer_are_rejected(self):
        missing = copy.deepcopy(self.p)
        missing.pop('_peer')
        self.assert_code('invalid_key', lambda: self.manager.prepare_download(missing, self.source))
        self.assertEqual(self.opened, [])
        identifier = self.prepare()
        for field, value in (('key_id', '2' * 24), ('grant_revision', 2),
                             ('credential_generation', 2), ('_peer', '127.0.0.2')):
            different = dict(self.p, **{field: value})
            self.assert_code('transfer_unavailable', lambda: self.manager.open_download(identifier, different))

    def test_acl_and_token_are_rechecked_before_the_next_real_byte_chunk(self):
        for change in ('revoke', 'acl'):
            self.valid = self.acl = True
            identifier = self.prepare()
            output = io.BytesIO()
            response = self.manager.open_download(identifier, self.p)
            def writer(block):
                output.write(block)
                if change == 'revoke':
                    self.valid = False
                else:
                    self.acl = False
                return len(block)
            self.assert_code('invalid_key' if change == 'revoke' else 'acl_unverified',
                             lambda: response.send(writer))
            self.assertEqual(output.getvalue(), DATA[:65536])
            with self.assertRaises(OSError):
                os.fstat(self.opened[-1])

    def test_replaced_or_changed_file_never_splices_new_bytes(self):
        identifier = self.prepare()
        output = io.BytesIO()
        response = self.manager.open_download(identifier, self.p)
        def writer(block):
            output.write(block)
            with self.file.open('ab') as stream:
                stream.write(b'changed')
            return len(block)
        self.assert_code('file_changed', lambda: response.send(writer))
        self.assertEqual(output.getvalue(), DATA[:65536])

    def test_cancel_single_active_get_disconnect_and_shutdown_release_fds(self):
        identifier = self.prepare()
        response = self.manager.open_download(identifier, self.p)
        self.assert_code('transfer_busy', lambda: self.manager.open_download(identifier, self.p))
        response.close()
        self.assertEqual(self.status(identifier)['state'], 'interrupted')
        response = self.manager.open_download(identifier, self.p)
        self.valid_result('cancel_file_transfer', self.manager.cancel_transfer(identifier, self.p))
        self.assert_code('transfer_unavailable', lambda: response.send(lambda unused: None))
        self.assertEqual(self.status(identifier)['state'], 'cancelled')
        another = self.prepare()
        response = self.manager.open_download(another, self.p)
        fd = self.opened[-1]
        self.manager.close()
        with self.assertRaises(OSError):
            os.fstat(fd)
        response.close()  # No double-close of an fd number that may be reused.

    def test_limits_and_monotonic_deadlines_do_not_extend_on_range_retries(self):
        first, second = self.prepare(), self.prepare()
        self.assert_code('transfer_busy', self.prepare)
        self.now = 301
        self.assertEqual(self.status(second)['state'], 'expired')
        self.assert_code('transfer_expired', lambda: self.manager.open_download(first, self.p))
        identifier = self.prepare()
        response = self.manager.open_download(identifier, self.p, range_header='bytes=0-0')
        response.send(lambda unused: None)
        deadline = self.status(identifier)['transfer_deadline']
        self.now += 100
        response = self.manager.open_download(identifier, self.p, range_header='bytes=1-1')
        response.send(lambda unused: None)
        self.assertEqual(self.status(identifier)['transfer_deadline'], deadline)
        self.now += 1801
        self.assertEqual(self.status(identifier)['state'], 'expired')

    def test_empty_file_completes_without_fabricated_bytes(self):
        self.file.write_bytes(b'')
        identifier = self.prepare()
        response = self.manager.open_download(identifier, self.p)
        self.assertEqual(response.send(lambda unused: self.fail('no data expected')), 0)
        self.assertEqual(self.status(identifier)['state'], 'completed')

    def test_upload_verifies_real_unnamed_staging_bytes_before_native_install_callback(self):
        receipt = self.upload()
        self.valid_result('prepare_file_upload', receipt)
        identifier = receipt['transfer_id']
        result = self.manager.receive_upload(identifier, self.p,
            [DATA[index:index + 65536] for index in range(0, len(DATA), 65536)], content_length=len(DATA))
        self.valid_result('get_file_transfer', result)
        self.assertEqual(result['transfer']['state'], 'verified')
        self.assertEqual(self.installs, [])
        result = self.manager.commit_upload(identifier, self.p, idempotency_key='commit-1')
        self.valid_result('commit_file_upload', result)
        self.assertEqual(result['operation']['state'], 'completed')
        self.assertEqual(self.installs[0][0], DATA)
        replay = self.manager.commit_upload(identifier, self.p, idempotency_key='commit-1')
        self.assertEqual(replay, result)
        self.assertEqual(len(self.installs), 1)

    def test_upload_hash_size_permissions_and_idempotency_conflicts_prevent_install(self):
        receipt = self.upload(data=b'expected')
        identifier = receipt['transfer_id']
        self.assert_code('checksum_mismatch', lambda: self.manager.receive_upload(identifier, self.p,
                         [b'bad'], content_length=8))
        self.assertEqual(self.installs, [])
        self.assertEqual(self.status(identifier)['state'], 'failed')
        self.assert_code('permission_denied', lambda: self.manager.prepare_upload(principal(), self.target(),
            size_bytes=0, sha256=hashlib.sha256(b'').hexdigest(), idempotency_key='new'))

    def test_upload_revocation_destroys_only_staging_and_never_installs(self):
        receipt = self.upload()
        identifier = receipt['transfer_id']
        def chunks():
            yield DATA[:65536]
            self.valid = False
            yield DATA[65536:131072]
        self.assert_code('invalid_key', lambda: self.manager.receive_upload(identifier, self.p,
                         chunks(), content_length=len(DATA)))
        self.assertEqual(self.installs, [])
        self.assertIsNone(self.manager._sessions[identifier]['fd'])

    def test_upload_prepare_and_commit_never_accept_mutated_targets_or_changed_bytes(self):
        receipt = self.upload(data=b'content')
        self.assert_code('idempotency_conflict', lambda: self.manager.prepare_upload(self.p, self.target(),
            size_bytes=0, sha256=hashlib.sha256(b'').hexdigest(), idempotency_key='prepare-1'))
        identifier = receipt['transfer_id']
        self.manager.receive_upload(identifier, self.p, [b'content'], content_length=7)
        descriptor = self.manager._sessions[identifier]['fd']
        os.pwrite(descriptor, b'changed-longer', 0)
        self.assert_code('file_changed', lambda: self.manager.commit_upload(identifier, self.p,
                         idempotency_key='commit-1'))
        self.assertEqual(self.installs, [])

    def test_slow_acl_cannot_release_download_bytes_after_revocation(self):
        identifier = self.prepare()
        response = self.manager.open_download(identifier, self.p)
        original = self.source
        def revoke(*args, **kwargs):
            self.valid = False
            return True
        self.manager._sessions[identifier]['resource'] = DownloadSource(
            original.root_id, original.path, original.name, original.open_reader, revoke)
        output = io.BytesIO()
        self.assert_code('invalid_key', lambda: response.send(output.write))
        self.assertEqual(output.getvalue(), b'')
        with self.assertRaises(OSError):
            os.fstat(self.opened[-1])

    def test_fsync_cannot_publish_verified_upload_after_revocation(self):
        identifier = self.upload(data=b'payload')['transfer_id']
        original = os.fsync
        def revoke(descriptor):
            original(descriptor)
            self.valid = False
        with mock.patch('api_file_transfers.os.fsync', side_effect=revoke):
            self.assert_code('invalid_key', lambda: self.manager.receive_upload(
                identifier, self.p, [b'payload'], content_length=7))
        self.assertEqual(self.manager._sessions[identifier]['state'], 'failed')
        self.assertIsNone(self.manager._sessions[identifier]['fd'])
        self.assertEqual(self.installs, [])

    def finish_during_install(self, expire):
        self.p = principal(write=True)
        original = self.target()
        def install(*args, **kwargs):
            version = original.install(*args, **kwargs)
            if expire:
                self.now = 1801
            else:
                self.valid = False
            return version
        target = UploadTarget(original.root_id, original.path, original.name,
                              original.create_staging, original.authorize_opened, install)
        receipt = self.manager.prepare_upload(self.p, target, size_bytes=7,
            sha256=hashlib.sha256(b'payload').hexdigest(), idempotency_key='prepare-1')
        identifier = receipt['transfer_id']
        self.manager.receive_upload(identifier, self.p, [b'payload'], content_length=7)
        self.assert_code('transfer_expired' if expire else 'invalid_key', lambda:
            self.manager.commit_upload(identifier, self.p, idempotency_key='commit-1'))
        row = self.manager._sessions[identifier]
        self.assertEqual(row['operation']['state'], 'result_unconfirmed')
        self.assertIsNone(row['operation']['result_version'])
        self.assertIsNone(row['fd'])
        self.assertEqual(len(self.installs), 1)

    def test_native_install_cannot_publish_success_after_revocation(self):
        self.finish_during_install(False)

    def test_native_install_cannot_publish_success_after_transfer_expiry(self):
        self.finish_during_install(True)


if __name__ == '__main__':
    unittest.main()
