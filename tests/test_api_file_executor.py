"""Production descriptor code with real Linux files; policy fixtures are explicit."""

import copy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from api_contracts import contract_catalog
from api_file_operations import FileAccessError
from api_file_executor import FileReadExecutor, _Handle
from api_file_provider import audited_owner_read_rule


ROOT = 'root_' + 'a' * 24
ROW = {'id': ROOT, 'mount': '/drives/data/Users', 'label': 'fixture', 'read_only': False}
DATA = bytes(range(256)) * 600


def principal():
    return {'key_id': '1' * 24, 'grant_revision': 1, 'credential_generation': 1,
            '_peer': '127.0.0.1', 'role': 'readonly', 'policy_version': 3,
            'scopes': ['files'], 'tool_grants': ['list_directory', 'get_file_info',
                'prepare_file_download', 'get_file_transfer', 'cancel_file_transfer'],
            'file_roots': [{'id': ROOT, 'path': '', 'access': 'readonly'}],
            'docker_projects': [], 'docker_deployments': []}


class FixtureAuthority:
    """Constructor-only fixture; never claims to be native NAS ACL acceptance."""
    def __init__(self):
        self.valid = True
        self.calls = []
        self.after = None

    def authorize(self, root_id, path, current):
        self.calls.append((root_id, path))
        if not self.valid or current() is not True:
            raise FileAccessError('acl_unverified')
        if root_id != ROOT:
            raise FileAccessError('root_unavailable')
        if not audited_owner_read_rule(path):
            raise FileAccessError('unsupported_path_class')
        if self.after is not None:
            self.after()
        return dict(ROW)


class ArgumentTests(unittest.TestCase):
    def test_unimplemented_mutations_unknown_parameters_and_forged_acl_touch_no_backend(self):
        authority = FixtureAuthority()
        executor = FileReadExecutor(None, authorizer=lambda unused: True, authority=authority)
        for tool, args in (('create_directory', {'root_id': ROOT, 'path': 'public/new'}),
                ('get_file_info', {'root_id': ROOT, 'path': 'public/a', 'acl_verified': True}),
                ('list_directory', {'path': ''}),
                ('get_file_transfer', {'transfer_id': 'transfer_' + 'a' * 32, 'url': 'http://x'})):
            result = executor.execute(tool, args, principal())
            self.assertFalse(result['ok'])
        self.assertFalse(authority.calls)
        executor.close()

    def test_legacy_missing_tool_scope_or_live_authorization_cannot_open_a_handle(self):
        authority = FixtureAuthority()
        executor = FileReadExecutor(None, authorizer=lambda unused: True, authority=authority)
        variants = [dict(principal(), policy_version=2, tool_grants=[]),
                    dict(principal(), tool_grants=['list_directory']),
                    dict(principal(), scopes=[])]
        for key in variants:
            result = executor.execute('get_file_info', {'root_id': ROOT, 'path': 'public/a'}, key)
            self.assertFalse(result['ok'])
        self.assertFalse(authority.calls)
        executor.close()
        result = executor.execute('get_file_info', {'root_id': ROOT, 'path': 'public/a'}, principal())
        self.assertEqual(result['error']['code'], 'invalid_key')

    def test_other_user_path_is_not_misreported_as_empty_or_not_found(self):
        executor = FileReadExecutor(None, authorizer=lambda unused: True,
                                    authority=FixtureAuthority())
        result = executor.execute('list_directory', {'root_id': ROOT, 'path': 'another-user'}, principal())
        self.assertEqual(result['error']['code'], 'unsupported_path_class')
        self.assertIn('尚不支持', result['error']['message'])
        executor.close()


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH') and hasattr(os, 'pread'),
                     'Linux real O_PATH, mount IDs and fd byte transport required')
class OpenedFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-native-file-read-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'drives/data/Users'
        for name in ('admin', 'public', 'another-user'):
            (self.users / name).mkdir(parents=True)
        (self.users / 'public/payload.bin').write_bytes(DATA)
        (self.users / 'admin/中文.txt').write_bytes(b'owner')
        (self.users / 'another-user/secret.txt').write_text('not selected native subject')
        proc = self.root / 'proc/self'
        proc.mkdir(parents=True)
        (proc / 'fdinfo').symlink_to('/proc/self/fdinfo', target_is_directory=True)
        fd = os.open(self.users, os.O_PATH | os.O_DIRECTORY)
        try:
            raw = Path('/proc/self/fdinfo/' + str(fd)).read_text()
            self.mount_id = raw.split('mnt_id:\t', 1)[1].splitlines()[0]
            info = os.fstat(fd)
            self.device = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
        finally:
            os.close(fd)
        (proc / 'mountinfo').write_text(
            f'{self.mount_id} 1 {self.device} / /drives/data rw - ext4 /dev/sda1 rw\n')
        self.valid = True
        self.authority = FixtureAuthority()
        self.executor = FileReadExecutor(None, root=self.root,
            authorizer=lambda unused: self.valid, authority=self.authority)
        self.addCleanup(self.executor.close)
        self.key = principal()
        self.schemas = {tool['name']: tool['outputSchema'] for tool in contract_catalog()['tools']}

    def call(self, tool, path='public', **args):
        result = self.executor.execute(tool, {'root_id': ROOT, 'path': path, **args}, self.key)
        Draft202012Validator(self.schemas[tool]).validate(result)
        return result

    def prepare(self):
        value = self.call('prepare_file_download', 'public/payload.bin')
        self.assertTrue(value['ok'], value)
        return value['transfer_id']

    def test_real_directory_filters_other_users_and_unsafe_objects(self):
        result = self.call('list_directory', '')
        self.assertTrue(result['ok'], result)
        self.assertEqual([row['name'] for row in result['entries']], ['admin', 'public'])
        self.assertIsNone(result['page']['total'])
        self.assertNotIn('another-user', json.dumps(result))
        value = self.call('get_file_info', 'admin/中文.txt')
        self.assertEqual(value['entry']['size_bytes'], 5)
        self.assertTrue(value['entry']['downloadable'])
        self.assertNotIn(str(self.root), json.dumps(value))

    def test_real_pagination_is_bound_to_key_revision_path_and_directory_identity(self):
        for name in ('a', 'b', 'c'):
            (self.users / 'public' / name).write_bytes(b'x')
        first = self.call('list_directory', 'public', limit=2)
        self.assertTrue(first['ok'], first)
        cursor = first['page']['next_cursor']
        self.assertIsNotNone(cursor)
        second = self.call('list_directory', 'public', limit=2, cursor=cursor)
        self.assertEqual([r['name'] for r in first['entries'] + second['entries']],
                         ['a', 'b', 'c', 'payload.bin'])
        self.key['grant_revision'] = 2
        denied = self.call('list_directory', 'public', limit=2, cursor=cursor)
        self.assertEqual(denied['error']['code'], 'directory_changed')
        self.key['grant_revision'] = 1
        (self.users / 'public/new').write_bytes(b'x')
        changed = self.call('list_directory', 'public', limit=2, cursor=cursor)
        self.assertEqual(changed['error']['code'], 'directory_changed')

    def test_symlink_file_directory_hardlink_fifo_and_socket_are_not_followed_or_read(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'secret').write_bytes(b'private-outside')
        (self.users / 'public/link').symlink_to(target, target_is_directory=True)
        (self.users / 'public/symlink').symlink_to(target / 'secret')
        os.link(target / 'secret', self.users / 'public/hardlink')
        os.mkfifo(self.users / 'public/fifo')
        channel = socket.socket(socket.AF_UNIX)
        channel.bind(str(self.users / 'public/socket'))
        self.addCleanup(channel.close)
        for path in ('public/link', 'public/link/secret', 'public/symlink',
                     'public/hardlink', 'public/fifo', 'public/socket'):
            result = self.call('prepare_file_download', path)
            self.assertFalse(result['ok'], path)
        listed = self.call('list_directory', 'public')
        self.assertEqual([r['name'] for r in listed['entries']], ['payload.bin'])

    def test_users_symlink_or_same_device_submount_is_rejected(self):
        with mock.patch('api_file_executor._descriptor_mount_id', return_value='99999999'):
            result = self.call('get_file_info', 'public/payload.bin')
        self.assertEqual(result['error']['code'], 'root_unavailable')
        with (self.root / 'proc/self/mountinfo').open('a') as stream:
            stream.write(f'88 1 {self.device} / /drives/data/Users/public rw - ext4 /dev/sda1 rw\n')
        result = self.call('get_file_info', 'public/payload.bin')
        self.assertEqual(result['error']['code'], 'root_unavailable')

    def test_live_authority_or_token_revocation_never_returns_metadata(self):
        self.authority.after = lambda: setattr(self, 'valid', False)
        result = self.call('get_file_info', 'public/payload.bin')
        self.assertEqual(result['error']['code'], 'invalid_key')
        self.valid = True
        self.authority.after = None
        self.authority.valid = False
        result = self.call('get_file_info', 'public/payload.bin')
        self.assertEqual(result['error']['code'], 'acl_unverified')

    def test_real_fd_get_head_suffix_range_and_cancel_have_contract_results(self):
        identifier = self.prepare()
        manager = self.executor.transfers
        response = manager.open_download(identifier, self.key, head=True)
        self.assertEqual(response.headers['Content-Length'], str(len(DATA)))
        response.close()
        response = manager.open_download(identifier, self.key, range_header='bytes=-16')
        sink = io.BytesIO()
        self.assertEqual(response.send(sink.write), 16)
        self.assertEqual(sink.getvalue(), DATA[-16:])
        response.close()
        result = self.executor.execute('get_file_transfer', {'transfer_id': identifier}, self.key)
        Draft202012Validator(self.schemas['get_file_transfer']).validate(result)
        self.assertEqual(result['transfer']['state'], 'ready')
        result = self.executor.execute('cancel_file_transfer', {'transfer_id': identifier}, self.key)
        self.assertEqual(result['transfer']['state'], 'cancelled')
        with self.assertRaises(FileAccessError):
            manager.open_download(identifier, self.key)

    def test_path_replacement_or_an_ancestor_moved_outside_root_invalidates_prepared_bytes(self):
        identifier = self.prepare()
        payload = self.users / 'public/payload.bin'
        payload.rename(self.users / 'public/old.bin')
        payload.write_bytes(DATA)
        with self.assertRaises(FileAccessError):
            self.executor.transfers.open_download(identifier, self.key)
        self.executor.close()
        self.executor = FileReadExecutor(None, root=self.root,
            authorizer=lambda unused: self.valid, authority=self.authority)
        self.addCleanup(self.executor.close)
        identifier = self.prepare()
        (self.users / 'public').rename(self.root / 'moved-public')
        (self.users / 'public').mkdir()
        with self.assertRaises(FileAccessError):
            self.executor.transfers.open_download(identifier, self.key)

    def test_each_byte_chunk_revalidates_native_acl_and_key_before_sending(self):
        identifier = self.prepare()
        response = self.executor.transfers.open_download(identifier, self.key)
        sink = io.BytesIO()
        def write(value):
            count = sink.write(value)
            self.valid = False
            return count
        with self.assertRaises(FileAccessError):
            response.send(write)
        response.close()
        self.assertLessEqual(len(sink.getvalue()), 65536)
        self.assertEqual(sink.getvalue(), DATA[:len(sink.getvalue())])

    def test_read_scope_cannot_access_neighbor_prefix_or_other_root(self):
        self.key['file_roots'][0]['path'] = 'public/sub'
        result = self.call('get_file_info', 'public/payload.bin')
        self.assertEqual(result['error']['code'], 'permission_denied')
        self.key['file_roots'][0]['path'] = ''
        result = self.executor.execute('get_file_info',
            {'root_id': 'root_' + 'b' * 24, 'path': 'public/payload.bin'}, self.key)
        self.assertEqual(result['error']['code'], 'permission_denied')

    def test_stale_or_different_peer_cannot_reuse_an_existing_transfer(self):
        identifier = self.prepare()
        for key in (dict(self.key, grant_revision=2), dict(self.key, credential_generation=2),
                    dict(self.key, _peer='127.0.0.2')):
            result = self.executor.execute('get_file_transfer', {'transfer_id': identifier}, key)
            self.assertEqual(result['error']['code'], 'transfer_unavailable')

    def test_reader_device_rejection_happens_before_ordinary_file_open(self):
        os.mkfifo(self.users / 'public/fifo')
        real_open = os.open
        seen = []
        def track(path, flags, *args, **kw):
            seen.append((path, flags))
            return real_open(path, flags, *args, **kw)
        with mock.patch('api_file_executor.os.open', side_effect=track):
            result = self.call('prepare_file_download', 'public/fifo')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'unsupported_file_type')
        fifo_opens = [flags for path, flags in seen if os.fspath(path) == 'fifo']
        self.assertTrue(fifo_opens, 'The final FIFO component must be inspected.')
        self.assertTrue(all(flags & os.O_PATH and flags & os.O_NOFOLLOW
                            for flags in fifo_opens))
        self.assertFalse(any(os.fspath(path).startswith('/proc/self/fd/')
                             for path, unused_flags in seen))
        # Both modules share os: this mock also sees the fixed proc metadata reads
        # needed to verify every O_PATH handle's mount identity.
        mountinfo = self.root / 'proc/self/mountinfo'
        fdinfo = self.root / 'proc/self/fdinfo'
        metadata_opens = [(Path(path), flags) for path, flags in seen
                          if not flags & os.O_PATH]
        self.assertTrue(any(path == mountinfo for path, unused_flags in metadata_opens))
        for path, flags in metadata_opens:
            self.assertTrue(path == mountinfo or
                            path.parent == fdinfo and path.name.isascii() and path.name.isdigit(),
                            f'Unexpected ordinary open before FIFO rejection: {path}')
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NONBLOCK)

    def test_native_authority_pipeline_runs_fixed_owner_sql_tls_parser_and_real_fd_reads(self):
        import api_file_provider as provider
        from test_api_file_provider import Connection, IDENTITY, PRIVATE, frame
        path = self.root / 'tmp/nand_config/USER_AUTH_DB_101'
        path.parent.mkdir(parents=True)
        database = sqlite3.connect(path)
        self.addCleanup(database.close)
        database.executescript('CREATE TABLE tb_user_auth(user_id INTEGER,user_type INTEGER,user_name TEXT);'
                               "INSERT INTO tb_user_auth VALUES(100000,0,'admin');")
        database.commit()
        connections = []
        def socket_factory(*unused):
            connection = Connection(frame(ROW['mount']))
            connections.append(connection)
            return connection
        client = provider.VendorFileProtocolClient(root=self.root,
            session_reader=lambda **unused: (SimpleNamespace(user_id=100000,
                derive_access_token=lambda: PRIVATE), {'available': True, 'status': 'ready'}),
            listener_check=lambda **unused: IDENTITY,
            certificate_reader=lambda unused: b'CERT', socket_factory=socket_factory,
            tls_context=SimpleNamespace(wrap_socket=lambda raw, server_hostname: raw),
            binary_check=lambda *unused: True)
        catalog = {'available': True, 'boundary_version': 2, 'roots': [dict(ROW)]}
        authority = provider.NativeOwnerReadAuthority(None, root=self.root, client=client,
            root_collector=lambda unused: copy.deepcopy(catalog))
        executor = FileReadExecutor(None, root=self.root, authorizer=lambda unused: self.valid,
                                    authority=authority)
        self.addCleanup(executor.close)
        with mock.patch.object(provider, '_firmware_identity', return_value=True):
            prepared = executor.execute('prepare_file_download',
                {'root_id': ROOT, 'path': 'public/payload.bin'}, self.key)
            self.assertTrue(prepared['ok'], prepared)
            response = executor.transfers.open_download(prepared['transfer_id'], self.key)
            sink = io.BytesIO()
            self.assertEqual(response.send(sink.write), len(DATA))
            response.close()
            self.assertEqual(sink.getvalue(), DATA)
            self.assertGreater(len(connections), 4)
            for connection in connections:
                request = connection.sent[0].decode()
                self.assertTrue(request.startswith('GET /rest/1.0/file?action=get_info&path='))
                self.assertNotIn('size=', request)
                self.assertNotIn('payload.bin', request)
                self.assertNotIn('public', request)
            count = len(connections)
            database.execute("UPDATE tb_user_auth SET user_name='other'")
            database.commit()
            result = executor.execute('get_file_info',
                {'root_id': ROOT, 'path': 'public/payload.bin'}, self.key)
            self.assertEqual(result['error']['code'], 'acl_unverified')
            self.assertEqual(len(connections), count)

    def test_fixed_root_evidence_reports_exact_mounts_and_open_stages_without_names(self):
        from api_file_provider import collect_opened_root_evidence
        result = collect_opened_root_evidence(self.root, ROW)
        self.assertTrue(result['same_mount'], result)
        self.assertEqual(result['expected_mount_id'], self.mount_id)
        self.assertEqual(result['expected_device'], self.device)
        self.assertEqual([r['stage'] for r in result['directory_stages']],
                         ['host_root', 'drives_directory', 'physical_mount', 'users_directory'])
        self.assertEqual(result['directory_stages'][-1]['mount_id'], self.mount_id)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn('payload.bin', json.dumps(result))
        self.assertFalse(result['native_acl_proven'])

    def test_short_proc_reads_preserve_real_mount_gate_and_file_execution(self):
        from api_file_provider import collect_opened_root_evidence
        real_read = os.read
        with mock.patch('api_file_operations.os.read', side_effect=lambda fd, count:
                        real_read(fd, min(count, 7))):
            evidence = collect_opened_root_evidence(self.root, ROW)
            result = self.call('list_directory', 'public')
        self.assertTrue(evidence['same_mount'], evidence)
        self.assertEqual(evidence['expected_mount_id'], self.mount_id)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['entries'][0]['name'], 'payload.bin')

    def test_mount_mismatch_probe_keeps_complete_topology_but_never_sends_native_request(self):
        import api_file_provider as provider
        (self.root / 'proc/self/mountinfo').write_text(
            f'99999999 1 {self.device} / /drives/data rw - ext4 /dev/sda1 rw\n')
        client = SimpleNamespace(clock=lambda: 0.0, request=mock.Mock())
        catalog = {'available': True, 'boundary_version': 2, 'roots': [dict(ROW)]}
        result = provider.collect_file_protocol_probe(None, root=self.root,
            authorizer=lambda: True, client=client, firmware_reader=lambda **unused: True,
            root_collector=lambda unused: copy.deepcopy(catalog))
        client.request.assert_not_called()
        self.assertTrue(result['complete'])
        self.assertFalse(result['file_access_available'])
        evidence = result['roots'][0]['opened_root']
        self.assertFalse(evidence['same_mount'])
        self.assertEqual(evidence['expected_mount_id'], '99999999')
        self.assertEqual(evidence['directory_stages'][-1]['mount_id'], self.mount_id)
        self.assertTrue(result['roots'][0]['root_topology_stable'])
        self.assertEqual(len(result['roots'][0]['queries']), 6)
        self.assertTrue(all(q['reason'] == 'root_open_unverified'
                            for q in result['roots'][0]['queries']))
        self.assertTrue(all(q['open_error_code'] == 'root_unavailable'
                            for q in result['roots'][0]['queries']))
