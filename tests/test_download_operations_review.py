"""Independent revocation and cross-engine concurrency review fixtures."""

import copy
import json
from pathlib import Path
import sys
import threading
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import download_operations as ops
import vendor_download_diagnostics as wire
from tests.test_download_operations import (ARIA, ARIA_ROW, IDENTITY, ROW, TASK,
                                            mutation, principal)
from tests.test_vendor_download_diagnostics import FakeSocket, OWNER, frame


def rpc(value):
    return frame({'jsonrpc': '2.0', 'id': 'r1-task-operation', 'result': value})


class DownloadReviewTests(unittest.TestCase):
    def client(self, factory, authorizer=None, ownership_check=None):
        return ops.DownloadOperations(authorizer=authorizer or (lambda _: True),
            ownership_check=ownership_check or (lambda **_: copy.deepcopy(OWNER)),
            socket_factory=factory, auth_state=wire.QbAuthState())

    def test_revocation_during_mutation_connect_prevents_post(self):
        active, sockets = [True], []
        replies = [frame(b'v4.6.7'), frame([ROW]), frame(b'')]
        def connect(address, timeout):
            if len(sockets) == 2:
                active[0] = False
            value = FakeSocket(replies[len(sockets)])
            sockets.append(value)
            return value
        client = self.client(connect, authorizer=lambda _: active[0])
        result = client.execute('pause_download_task', mutation(), principal())
        self.assertFalse(result['ok'])
        self.assertFalse(any(s.sent.startswith(b'POST ') for s in sockets),
                         '撤权发生在连接完成时，随后仍然发送了控制 POST')

    def test_identity_change_during_mutation_connect_prevents_post(self):
        owner, sockets = [copy.deepcopy(OWNER)], []
        replies = [frame(b'v5.0.5'), frame([ROW]), frame(b'')]
        def connect(address, timeout):
            if len(sockets) == 2:
                owner[0] = dict(owner[0], start_ticks=101)
            value = FakeSocket(replies[len(sockets)])
            sockets.append(value)
            return value
        client = self.client(connect, ownership_check=lambda **_: copy.deepcopy(owner[0]))
        result = client.execute('pause_download_task', mutation(), principal())
        self.assertFalse(result['ok'])
        self.assertFalse(any(s.sent.startswith(b'POST ') for s in sockets),
                         '监听身份在连接完成时变化，随后仍然发送了控制 POST')

    def test_revocation_during_login_connect_prevents_credential_send(self):
        active, sockets = [True], []
        replies = [frame(b'', status=401), frame(b'Ok.', headers=b'Set-Cookie: SID=' + b'a' * 32 + b'; path=/\r\n')]
        def connect(address, timeout):
            if len(sockets) == 1:
                active[0] = False
            value = FakeSocket(replies[len(sockets)])
            sockets.append(value)
            return value
        client = self.client(connect, authorizer=lambda _: active[0])
        result = client.execute('get_download_task', {'engine': 'qbittorrent', 'task_id': TASK}, principal('readonly'))
        self.assertFalse(result['ok'])
        self.assertFalse(any(b'/auth/login' in s.sent for s in sockets),
                         '撤权后仍发送登录凭据')

    def test_before_send_callback_cannot_reset_or_outlive_absolute_window(self):
        now, channel = [10.0], FakeSocket(frame(b''))
        def before_send():
            now[0] = 12.0
        with self.assertRaises(wire._Failure) as raised:
            wire._http_exchange(b'POST /fixture HTTP/1.1\r\n\r\n', 8181, 1024,
                11.0, lambda: now[0], lambda *a, **kw: channel,
                before_send=before_send)
        self.assertEqual(raised.exception.status, 'timeout')
        self.assertEqual(channel.sent, b'', '截止时间后仍发送了请求')

    def test_auth_rejection_performs_at_most_one_fixed_login_then_backoff(self):
        replies = [frame(b'', status=401),
                   frame(b'Ok.', headers=b'Set-Cookie: SID=' + b'a' * 32 + b'; path=/\r\n'),
                   frame(b'', status=401)]
        sockets = []
        def connect(address, timeout):
            value = FakeSocket(replies[len(sockets)])
            sockets.append(value)
            return value
        client = self.client(connect)
        result = client.execute('get_download_task', {'engine': 'qbittorrent', 'task_id': TASK}, principal('readonly'))
        self.assertFalse(result['ok'])
        self.assertEqual(len([s for s in sockets if b'/auth/login' in s.sent]), 1)
        self.assertGreater(client.auth_state._retry_at, client.clock())
        self.assertIsNone(client.auth_state._sid)
        self.assertNotIn('SID', json.dumps(result))

    def test_cross_engine_same_key_conflicts_before_second_engine_network(self):
        entered, release = threading.Event(), threading.Event()
        calls, replies = [], [frame(b'v4.6.7'), frame([ROW]), frame(b'')]
        def connect(address, timeout):
            calls.append(address)
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('fixture release timed out')
            return FakeSocket(replies[len(calls) - 1])
        client = self.client(connect)
        results = []
        thread = threading.Thread(target=lambda: results.append(client.execute(
            'pause_download_task', mutation(), principal())))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            other = client.execute('pause_download_task', mutation(engine='aria2'), principal())
            self.assertEqual(other['error']['code'], 'idempotency_conflict')
            self.assertEqual(calls, [('127.0.0.1', 8181)])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['ok'], results)

    def test_distinct_keys_on_two_engines_can_run_concurrently(self):
        entered, release = threading.Event(), threading.Event()
        calls, calls_lock = [], threading.Lock()
        qb_replies = [frame(b'v4.6.7'), frame([ROW]), frame(b'')]
        aria_replies = [rpc(ARIA_ROW), rpc(ARIA)]
        def connect(address, timeout):
            with calls_lock:
                calls.append(address)
                value = (qb_replies if address[1] == 8181 else aria_replies).pop(0)
                first = len(calls) == 1
            if first:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('fixture release timed out')
            return FakeSocket(value)
        client = self.client(connect)
        results = []
        thread = threading.Thread(target=lambda: results.append(client.execute(
            'pause_download_task', mutation(), principal())))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            args = dict(mutation(engine='aria2'), idempotency_key='request-2')
            other = client.execute('pause_download_task', args, principal())
            self.assertTrue(other['ok'], other)
            self.assertIn(('127.0.0.1', 6800), calls)
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(results[0]['ok'], results)

    def test_aria2_completed_result_removal_keeps_data_and_uses_fixed_method(self):
        row = dict(ARIA_ROW, status='complete')
        replies, sockets = [rpc(row), rpc('OK')], []
        def connect(address, timeout):
            value = FakeSocket(replies[len(sockets)])
            sockets.append(value)
            return value
        client = self.client(connect)
        args = mutation(engine='aria2')
        args['expected_version'] = ops._project('aria2', IDENTITY, row)['version']
        result = client.execute('remove_download_task', args, principal())
        self.assertTrue(result['ok'], result)
        request = json.loads(sockets[-1].sent.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(request['method'], 'aria2.removeDownloadResult')
        self.assertEqual(request['params'], [ARIA])
        self.assertFalse(result['delete_files'])
        self.assertNotIn('/secret', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
