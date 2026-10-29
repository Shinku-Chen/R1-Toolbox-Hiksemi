import copy
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import download_operations as ops
import vendor_download_diagnostics as wire
from tests.test_vendor_download_diagnostics import FakeSocket, frame, OWNER


TASK = 'a' * 40
ARIA = 'b' * 16
IDENTITY = (42, 100, '12345', '127.0.0.1')
ROW = {'hash': TASK, 'name': '示例视频', 'state': 'downloading', 'size': 100,
       'total_size': 100, 'completed': 20, 'added_on': 123, 'save_path': '/secret/private',
       'dlspeed': 8, 'upspeed': 2, 'tracker': 'https://secret.invalid/passkey'}
ARIA_ROW = {'gid': ARIA, 'status': 'active', 'totalLength': '100', 'completedLength': '20',
            'downloadSpeed': '8', 'uploadSpeed': '2', 'dir': '/secret/private', 'files': []}


def principal(role='readwrite'):
    return {'key_id': 'key_' + '1' * 24, 'policy_version': 3, 'role': role,
            'scopes': ['downloads'], 'tool_grants': sorted(ops.TOOLS if role != 'readonly' else ops.READ_TOOLS),
            'file_roots': [], 'docker_projects': [], 'grant_revision': 1, 'credential_generation': 1}


def mutation(tool='pause_download_task', engine='qbittorrent'):
    row = ROW if engine == 'qbittorrent' else ARIA_ROW
    return {'engine': engine, 'task_id': TASK if engine == 'qbittorrent' else ARIA,
            'expected_version': ops._project(engine, IDENTITY, row)['version'],
            'confirmed': True, 'idempotency_key': 'request-1'}


class Factory:
    def __init__(self, responses):
        self.responses, self.sockets = list(responses), []

    def __call__(self, address, timeout):
        if address not in (('127.0.0.1', 8181), ('127.0.0.1', 6800), ('::1', 8181)):
            raise AssertionError('unexpected endpoint')
        if not self.responses:
            raise AssertionError('unexpected request')
        value = self.responses.pop(0)
        sock = value if isinstance(value, FakeSocket) else FakeSocket(value)
        self.sockets.append(sock)
        return sock


class DownloadOperationTests(unittest.TestCase):
    def client(self, responses, *, authorizer=None, owner=None):
        factory = Factory(responses)
        value = ops.DownloadOperations(authorizer=authorizer or (lambda unused: True),
            ownership_check=owner or (lambda **unused: copy.deepcopy(OWNER)),
            socket_factory=factory, auth_state=wire.QbAuthState())
        return value, factory

    def test_live_read_has_identity_version_without_private_paths(self):
        client, factory = self.client([frame(b'v4.6.7'), frame([ROW])])
        result = client.execute('get_download_task', {'engine': 'qbittorrent', 'task_id': TASK}, principal('readonly'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['task']['name'], '示例视频')
        self.assertEqual(result['task']['completed_bytes'], 20)
        self.assertRegex(result['task']['version'], r'^download_[0-9a-f]{32}$')
        serialized = json.dumps(result)
        self.assertNotIn('/secret', serialized)
        self.assertNotIn('passkey', serialized)
        self.assertFalse(result['task']['location_available'])
        self.assertIsNone(result['task']['configured_download_limit_bytes_per_second'])
        self.assertIsNone(result['task']['configured_upload_limit_bytes_per_second'])
        self.assertEqual(len(factory.sockets), 2)

    def test_qb_configured_limits_distinguish_unlimited_from_unknown(self):
        for native, expected in ((-1, 0), (0, 0), (4096, 4096), (None, None),
                                 (True, None), (-2, None)):
            with self.subTest(native=native):
                row = {**ROW, 'dl_limit': native, 'up_limit': native}
                client, unused = self.client([frame(b'v5.1.0'), frame([row])])
                result = client.execute('get_download_task',
                    {'engine': 'qbittorrent', 'task_id': TASK}, principal('readonly'))
                self.assertTrue(result['ok'])
                self.assertEqual(result['task']['configured_download_limit_bytes_per_second'], expected)
                self.assertEqual(result['task']['configured_upload_limit_bytes_per_second'], expected)

    def test_qb4_and_qb5_use_exact_single_task_pause_endpoints(self):
        for version, endpoint in ((b'v4.6.7', b'/pause '), (b'v5.1.0', b'/stop ')):
            with self.subTest(version=version):
                client, factory = self.client([frame(version), frame([ROW]), frame(b'')])
                result = client.execute('pause_download_task', mutation(), principal())
                self.assertTrue(result['ok'])
                self.assertTrue(result['accepted'])
                self.assertFalse(result['effect_confirmed'])
                request = factory.sockets[-1].sent
                self.assertIn(endpoint, request)
                self.assertIn(('hashes=' + TASK).encode(), request)
                self.assertNotIn(b'hashes=all', request)

    def test_qb5_resume_uses_start(self):
        client, factory = self.client([frame(b'v5.0.5'), frame([ROW]), frame(b'')])
        self.assertTrue(client.execute('resume_download_task', mutation(), principal())['ok'])
        self.assertIn(b'/torrents/start ', factory.sockets[-1].sent)

    def test_delete_always_retains_data_and_cannot_accept_delete_files_flag(self):
        client, factory = self.client([frame(b'v4.6.7'), frame([ROW]), frame(b'')])
        result = client.execute('remove_download_task', mutation(), principal())
        self.assertTrue(result['ok'])
        self.assertFalse(result['delete_files'])
        self.assertIn(b'deleteFiles=false', factory.sockets[-1].sent)
        rejected = mutation()
        rejected['delete_files'] = True
        self.assertFalse(client.execute('remove_download_task', rejected, principal())['ok'])
        self.assertEqual(len(factory.sockets), 3)

    def test_changed_task_or_daemon_rejects_old_version_before_post(self):
        for row in ({**ROW, 'added_on': 999}, {**ROW, 'save_path': '/changed'}):
            with self.subTest(row=row['added_on']):
                client, factory = self.client([frame(b'v4.6.7'), frame([row])])
                result = client.execute('pause_download_task', mutation(), principal())
                self.assertEqual(result['error']['code'], 'task_changed')
                self.assertFalse(any(s.sent.startswith(b'POST ') for s in factory.sockets))

    def test_idempotent_repetition_does_not_send_again_and_conflict_is_rejected(self):
        client, factory = self.client([frame(b'v4.6.7'), frame([ROW]), frame(b'')])
        first = client.execute('pause_download_task', mutation(), principal())
        self.assertEqual(client.execute('pause_download_task', mutation(), principal()), first)
        self.assertEqual(len(factory.sockets), 3)
        result = client.execute('resume_download_task', mutation(), principal())
        self.assertEqual(result['error']['code'], 'idempotency_conflict')
        self.assertEqual(len(factory.sockets), 3)

    def test_uncertain_mutation_is_remembered_without_login_or_replay(self):
        client, factory = self.client([frame(b'v4.6.7'), frame([ROW]), frame(b'', status=403)])
        result = client.execute('pause_download_task', mutation(), principal())
        self.assertEqual(result['error']['code'], 'result_unconfirmed')
        self.assertEqual(client.execute('pause_download_task', mutation(), principal()), result)
        self.assertEqual(len(factory.sockets), 3)
        self.assertFalse(any(b'/auth/login' in s.sent for s in factory.sockets))

    def test_revocation_between_preflight_and_task_prevents_mutation_and_result_disclosure(self):
        active = [True]
        class RevokeSocket(FakeSocket):
            def recv(self, count):
                result = super().recv(count)
                active[0] = False
                return result
        client, factory = self.client([RevokeSocket(frame(b'v4.6.7'))], authorizer=lambda unused: active[0])
        result = client.execute('pause_download_task', mutation(), principal())
        self.assertEqual(result['error']['code'], 'invalid_key')
        self.assertEqual(len(factory.sockets), 1)

    def test_readonly_and_legacy_keys_cannot_run_writes(self):
        client, factory = self.client([])
        for key in (principal('readonly'), {'scopes': ['read']}, {**principal(), 'tool_grants': ['get_download_status']}):
            self.assertEqual(client.execute('pause_download_task', mutation(), key)['error']['code'], 'permission_denied')
        self.assertEqual(factory.sockets, [])

    def test_invalid_arguments_cannot_form_selector_or_arbitrary_daemon_requests(self):
        client, factory = self.client([])
        for extra in ({'task_id': 'all'}, {'task_id': TASK + '|all'}, {'confirmed': False},
                      {'engine': 'other'}, {'host': 'evil.invalid'}, {'expected_version': 'stale'},
                      {'idempotency_key': '\r\n'}, {'delete_data': True}):
            with self.subTest(extra=extra):
                self.assertEqual(client.execute('pause_download_task', {**mutation(), **extra}, principal())['error']['code'], 'invalid_arguments')
        self.assertEqual(factory.sockets, [])

    def test_only_fixed_limits_are_accepted(self):
        for extra in ({}, {'download_limit_bps': -1}, {'download_limit_bps': True}, {'option': 'dir'}):
            self.assertFalse(ops.valid_arguments('set_download_task_limits', {**mutation(), **extra}))
        client, factory = self.client([frame(b'v4.6.7'), frame([ROW]), frame(b'')])
        self.assertTrue(client.execute('set_download_task_limits', {**mutation(), 'download_limit_bps': 0}, principal())['ok'])
        self.assertIn(b'/torrents/setDownloadLimit ', factory.sockets[-1].sent)
        self.assertIn(b'limit=0', factory.sockets[-1].sent)

    def test_qb_explicit_list_paginates_and_marks_more(self):
        rows = [ROW, {**ROW, 'hash': 'c' * 40}]
        client, factory = self.client([frame(b'v4.6.7'), frame(rows)])
        result = client.execute('list_download_tasks', {'engine': 'qbittorrent', 'limit': 1}, principal('readonly'))
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['tasks']), 1)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_offset'], 1)

    def test_aria2_remove_does_not_delete_files(self):
        def rpc(value):
            return frame({'jsonrpc': '2.0', 'id': 'r1-task-operation', 'result': value})
        client, factory = self.client([rpc(ARIA_ROW), rpc(ARIA)])
        result = client.execute('remove_download_task', mutation(engine='aria2'), principal())
        self.assertTrue(result['ok'])
        body = json.loads(factory.sockets[-1].sent.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(body['method'], 'aria2.remove')
        self.assertEqual(body['params'], [ARIA])

    def test_identity_change_discards_result(self):
        owners = [copy.deepcopy(OWNER) for _ in range(20)]
        owners[2]['start_ticks'] += 1
        client, factory = self.client([frame(b'v4.6.7')], owner=lambda **unused: owners.pop(0))
        result = client.execute('get_download_task', {'engine': 'qbittorrent', 'task_id': TASK}, principal('readonly'))
        self.assertEqual(result['error']['code'], 'identity_changed')
        self.assertNotIn('task', result)

    def test_real_http_fixture_accepts_one_version_bound_pause(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append(('GET', self.path, b''))
                value = b'v5.0.5' if self.path == '/api/v2/app/version' else json.dumps([ROW]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(value)))
                self.end_headers()
                self.wfile.write(value)
            def do_POST(self):
                calls.append(('POST', self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def connect(address, timeout):
                self.assertEqual(address, ('127.0.0.1', 8181))
                return socket.create_connection(server.server_address, timeout=timeout)
            client = ops.DownloadOperations(authorizer=lambda unused: True,
                ownership_check=lambda **unused: OWNER, socket_factory=connect, auth_state=wire.QbAuthState())
            result = client.execute('pause_download_task', mutation(), principal())
            self.assertTrue(result['ok'], result)
            self.assertEqual(calls[-1], ('POST', '/api/v2/torrents/stop', ('hashes=' + TASK).encode()))
            self.assertEqual(client.execute('pause_download_task', mutation(), principal()), result)
            self.assertEqual(len(calls), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
