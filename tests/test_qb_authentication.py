"""Isolated real HTTP fixtures for qB's fixed local authentication contract.

No NAS, production credentials, external listener or filesystem config is used.
Only socket_factory remaps the verified fixed production address to a fixture.
"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from urllib.parse import parse_qs
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_download_diagnostics as download


FAKE_USER = 'fixture +&=用户'
FAKE_PASSWORD = 'test-only +&= 口令'
FAKE_CREDENTIALS = (FAKE_USER, FAKE_PASSWORD)
FAKE_SID = 'fixture-session-A_1234567890abcdef'
SECOND_SID = 'fixture-session-B_1234567890abcdef'
PRIVATE_TASK = 'fixture-private-task-name'
OWNER = {'available': True, 'status': 'verified', 'pid': 42,
         'start_ticks': 100, 'socket_inode': '12345', 'connect_host': '127.0.0.1'}
LOGIN_PATH = '/api/v2/auth/login'
GET_PATHS = ('/api/v2/app/version', '/api/v2/transfer/info',
             '/api/v2/torrents/info?filter=all&sort=hash&limit=129&offset=0')


def response(body=b'', *, status=200, headers=()):
    if not isinstance(body, bytes):
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    return status, list(headers), body


class _Fixture:
    """A fresh local HTTP server with recorded real requests and fixed replies."""

    def __init__(self, test, *, host='127.0.0.1', reply=None):
        self.test, self.host, self.reply = test, host, reply
        self.requests, self.connections = [], []
        self.accepted_sids = {FAKE_SID}
        self.credentials = FAKE_CREDENTIALS
        self.sid = FAKE_SID
        self.public = False
        self.guard = threading.Lock()
        self.cleanup_events = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def handle_query(self):
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 8192:
                    raise AssertionError('unbounded fixture request')
                item = {'method': self.command, 'path': self.path,
                        'headers': {key.lower(): value for key, value in self.headers.items()},
                        'body': self.rfile.read(length)}
                with outer.guard:
                    outer.requests.append(item)
                    number = len(outer.requests)
                value = outer.reply(item, number) if outer.reply is not None else None
                if value is None:
                    value = outer.default_reply(item)
                if isinstance(value, bytes):
                    # Owned fixture bytes permit malformed/duplicate cookie tests.
                    self.wfile.write(value)
                    self.close_connection = True
                    return
                status, headers, body = value
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, content in headers:
                    self.send_header(key, content)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            do_GET = do_POST = handle_query

            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            address_family = socket.AF_INET6 if host == '::1' else socket.AF_INET

            def handle_error(self, *_):
                # Fixture errors must not dump request bodies or cookie material.
                pass

        try:
            self.server = Server((host, 0), Handler)
        except OSError:
            if host == '::1':
                test.skipTest('IPv6 loopback fixture unavailable')
            raise
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        test.addCleanup(self.close)

    def close(self):
        for event in self.cleanup_events:
            event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self, address, timeout):
        # This mapping does not alter the production ownership/listener gate.
        self.test.assertEqual(address, (self.host, 8181))
        self.test.assertGreater(timeout, 0)
        self.test.assertLessEqual(timeout, download.QB_NETWORK_SECONDS)
        self.connections.append((address, timeout))
        return socket.create_connection((self.host, self.server.server_address[1]), timeout=timeout)

    def default_reply(self, item):
        if item['method'] == 'POST':
            if item['path'] != LOGIN_PATH:
                return response(status=404)
            values = parse_qs(item['body'].decode('ascii'), strict_parsing=True)
            if values != {'username': [self.credentials[0]], 'password': [self.credentials[1]]}:
                return response(b'Fails.')
            return response(b'Ok.', headers=[('Set-Cookie', 'SID=' + self.sid + '; HttpOnly; path=/; SameSite=Strict')])
        cookie = item['headers'].get('cookie')
        accepted = any(cookie == 'SID=' + sid for sid in self.accepted_sids)
        if not self.public and not accepted:
            return response(b'Authentication required', status=401)
        if item['path'] == GET_PATHS[0]:
            return response(b'v5.1.4')
        if item['path'] == GET_PATHS[1]:
            return response({'dl_info_speed': 12, 'up_info_speed': 0, 'connection_status': 'connected'})
        if item['path'] == GET_PATHS[2]:
            return response([{'hash': 'a' * 40, 'state': 'downloading', 'size': 100,
                              'completed': 25, 'progress': 0.25, 'dlspeed': 12, 'upspeed': 0,
                              'name': PRIVATE_TASK, 'save_path': '/fixture/private',
                              'magnet_uri': 'magnet:fixture-private', 'token': FAKE_PASSWORD}])
        return response(status=404)

    def posts(self):
        return [item for item in self.requests if item['method'] == 'POST']


class QbAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.state = download.QbAuthState()
        self.now = [1000.0]

    def collect(self, fixture, *, state=None, credentials=FAKE_CREDENTIALS, owner=None):
        return download.collect_qbittorrent(root='/owned-fixture',
            ownership_check=owner or (lambda **_: dict(OWNER, connect_host=fixture.host)),
            socket_factory=fixture.connect, monotonic=lambda: self.now[0],
            auth_state=self.state if state is None else state, credentials=credentials)

    def assert_private(self, value):
        text = json.dumps(value, ensure_ascii=False)
        for secret in (FAKE_USER, FAKE_PASSWORD, FAKE_SID, SECOND_SID,
                       PRIVATE_TASK, 'a' * 40, '/fixture/private', 'magnet:fixture-private'):
            self.assertNotIn(secret, text)

    def assert_failure(self, value, *statuses):
        self.assertFalse(value['available'])
        self.assertIsNone(value['data'])
        if statuses:
            self.assertIn(value['status'], statuses)
        self.assert_private(value)

    def test_401_causes_one_fixed_form_post_then_three_authenticated_gets(self):
        fixture = _Fixture(self)
        with mock.patch.dict('os.environ', {'HTTP_PROXY': 'http://forbidden.invalid',
                                           'ALL_PROXY': 'http://forbidden.invalid'}), \
                mock.patch('builtins.open', side_effect=AssertionError('no credential/config reads')):
            value = self.collect(fixture)
        self.assertTrue(value['available'])
        self.assertEqual([(item['method'], item['path']) for item in fixture.requests],
                         [('GET', GET_PATHS[0]), ('POST', LOGIN_PATH)]
                         + [('GET', path) for path in GET_PATHS])
        login = fixture.posts()[0]
        self.assertEqual(login['headers']['content-type'].split(';')[0], 'application/x-www-form-urlencoded')
        self.assertEqual(int(login['headers']['content-length']), len(login['body']))
        self.assertEqual(parse_qs(login['body'].decode('ascii')),
                         {'username': [FAKE_USER], 'password': [FAKE_PASSWORD]})
        self.assertNotIn('cookie', fixture.requests[0]['headers'])
        for item in fixture.requests[2:]:
            self.assertEqual(item['headers'].get('cookie'), 'SID=' + FAKE_SID)
            self.assertEqual(item['body'], b'')
        self.assertEqual(value['data']['tasks'][0]['progress_percent'], 25.0)
        self.assert_private(value)
        self.assert_private({'state_repr': repr(self.state), 'state_str': str(self.state)})

    def test_same_origin_authority_is_fixed_for_ipv4_and_ipv6_login_and_gets(self):
        for host in ('127.0.0.1', '::1'):
            with self.subTest(host=host):
                fixture = _Fixture(self, host=host)
                value = self.collect(fixture, state=download.QbAuthState())
                self.assertTrue(value['available'])
                authority = ('[::1]' if host == '::1' else host) + ':8181'
                for item in fixture.requests:
                    self.assertEqual(item['headers'].get('host'), authority)
                    self.assertEqual(item['headers'].get('referer'), 'http://' + authority + '/')
                    if item['method'] == 'POST':
                        self.assertEqual(item['headers'].get('origin'), 'http://' + authority)
                    self.assertNotIn('authorization', item['headers'])

    def test_existing_anonymous_access_never_logs_in_or_sends_password(self):
        fixture = _Fixture(self)
        fixture.public = True
        value = self.collect(fixture)
        self.assertTrue(value['available'])
        self.assertEqual([item['path'] for item in fixture.requests], list(GET_PATHS))
        self.assertEqual(fixture.posts(), [])
        self.assertTrue(all('cookie' not in item['headers'] and item['body'] == b'' for item in fixture.requests))

    def test_base64_sid_with_plus_slash_and_padding_is_accepted(self):
        sid = 'fixture/SID+value_1234567890abc='
        fixture = _Fixture(self)
        fixture.sid, fixture.accepted_sids = sid, {sid}
        value = self.collect(fixture)
        self.assertTrue(value['available'])
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(len(fixture.posts()), 1)
        self.assertTrue(all(item['headers'].get('cookie') == 'SID=' + sid
                            for item in fixture.requests[2:]))
        offset = len(fixture.requests)
        self.assertTrue(self.collect(fixture)['available'])
        self.assertEqual(len(fixture.posts()), 1)
        self.assertTrue(all(item['headers'].get('cookie') == 'SID=' + sid
                            for item in fixture.requests[offset:]))
        self.assert_private(value)

    def test_sid_reuse_has_no_forced_expiry_or_repeat_login(self):
        fixture = _Fixture(self)
        self.assertTrue(self.collect(fixture)['available'])
        self.now[0] += 86400
        offset = len(fixture.requests)
        value = self.collect(fixture)
        self.assertTrue(value['available'])
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual([item['path'] for item in fixture.requests[offset:]], list(GET_PATHS))
        self.assertTrue(all(item['headers'].get('cookie') == 'SID=' + FAKE_SID
                            for item in fixture.requests[offset:]))

    def test_rejected_cached_sid_is_replaced_once_with_a_new_session(self):
        fixture = _Fixture(self)
        self.assertTrue(self.collect(fixture)['available'])
        fixture.sid, fixture.accepted_sids = SECOND_SID, {SECOND_SID}
        offset = len(fixture.requests)
        value = self.collect(fixture)
        self.assertTrue(value['available'])
        rows = fixture.requests[offset:]
        self.assertEqual([(item['method'], item['path']) for item in rows],
                         [('GET', GET_PATHS[0]), ('POST', LOGIN_PATH)] + [('GET', path) for path in GET_PATHS])
        self.assertEqual(rows[0]['headers'].get('cookie'), 'SID=' + FAKE_SID)
        self.assertTrue(all(item['headers'].get('cookie') == 'SID=' + SECOND_SID for item in rows[2:]))
        self.assertEqual(len(fixture.posts()), 2)
        self.assert_private(value)

    def test_any_get_rejection_allows_only_one_login_for_that_collection(self):
        for failed_path in GET_PATHS:
            with self.subTest(path=failed_path):
                fixture = _Fixture(self)
                fixture.public = True
                rejected = [False]
                def reply(item, _number):
                    if item['method'] == 'GET' and item['path'] == failed_path and not rejected[0]:
                        rejected[0] = True
                        return response(status=403)
                    return None
                fixture.reply = reply
                value = self.collect(fixture, state=download.QbAuthState())
                self.assertTrue(value['available'])
                self.assertEqual(len(fixture.posts()), 1)
                index = fixture.requests.index(fixture.posts()[0])
                self.assertEqual([item['path'] for item in fixture.requests[index + 1:]], list(GET_PATHS))

    def test_wrong_password_and_banned_login_enter_bounded_backoff(self):
        for login_status, login_body, expected in ((200, b'Fails.', 'auth_rejected'),
                                                    (401, b'private-body', 'auth_rejected'),
                                                    (403, b'private-body', 'auth_banned')):
            with self.subTest(status=login_status):
                fixture = _Fixture(self, reply=lambda item, _number: response(login_body, status=login_status)
                                   if item['method'] == 'POST' else None)
                state = download.QbAuthState()
                value = self.collect(fixture, state=state)
                self.assert_failure(value, expected)
                self.assertEqual(len(fixture.posts()), 1)
                for _ in range(4):
                    self.assert_failure(self.collect(fixture, state=state), 'auth_backoff')
                self.assertEqual(len(fixture.posts()), 1)
                self.now[0] += download.QB_AUTH_BACKOFF_SECONDS - 1
                self.assert_failure(self.collect(fixture, state=state), 'auth_backoff')
                self.assertEqual(len(fixture.posts()), 1)
                self.now[0] += 2
                self.assert_failure(self.collect(fixture, state=state), expected)
                self.assertEqual(len(fixture.posts()), 2)

    def test_successful_login_followed_by_another_auth_rejection_does_not_loop(self):
        fixture = _Fixture(self)
        def reply(item, _number):
            if item['method'] == 'GET' and 'cookie' in item['headers']:
                return response(status=401)
            return None
        fixture.reply = reply
        value = self.collect(fixture)
        self.assert_failure(value, 'auth_rejected')
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual(len(fixture.requests), 3)
        self.assert_failure(self.collect(fixture), 'auth_backoff')
        self.assertEqual(len(fixture.posts()), 1)

    def test_bad_or_duplicate_sid_cookie_is_never_accepted_or_reflected(self):
        variants = [[], [('Set-Cookie', 'another=value; path=/')],
                    [('Set-Cookie', 'SID=; path=/')],
                    [('Set-Cookie', 'SID=bad token; path=/')],
                    [('Set-Cookie', 'SID=' + 'x' * 1025 + '; path=/')],
                    [('Set-Cookie', 'SID=' + FAKE_SID), ('Set-Cookie', 'SID=' + SECOND_SID)],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '; SID=' + SECOND_SID)],
                    [('Set-Cookie', 'SID=' + FAKE_SID + ', SID=' + SECOND_SID)],
                    [('Set-Cookie', 'SID="' + FAKE_SID + '"; path=/')],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '\rInjected: fixture-only')],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '\nInjected: fixture-only')],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '\x00; path=/')],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '; Domain=external.invalid')],
                    [('Set-Cookie', 'SID=' + FAKE_SID + '; Path=/different')]]
        for headers in variants:
            with self.subTest(variant=variants.index(headers)):
                fixture = _Fixture(self, reply=lambda item, _number: response(b'Ok.', headers=headers)
                                   if item['method'] == 'POST' else None)
                state = download.QbAuthState()
                value = self.collect(fixture, state=state)
                self.assert_failure(value, 'invalid_response', 'auth_rejected')
                self.assertEqual(len(fixture.posts()), 1)
                self.assertEqual(len(fixture.requests), 2)
                self.assert_private({'state_repr': repr(state)})
                self.assert_failure(self.collect(fixture, state=state), 'auth_backoff')
                self.assertEqual(len(fixture.posts()), 1)

    def test_auth_backoff_blocks_only_post_and_accepts_new_anonymous_access_immediately(self):
        fixture = _Fixture(self, reply=lambda item, _number: response(b'Fails.')
                           if item['method'] == 'POST' else None)
        self.assert_failure(self.collect(fixture), 'auth_rejected')
        before = len(fixture.requests)
        self.assert_failure(self.collect(fixture), 'auth_backoff')
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual([(item['method'], item['path']) for item in fixture.requests[before:]],
                         [('GET', GET_PATHS[0])])
        fixture.public = True
        before = len(fixture.requests)
        value = self.collect(fixture)
        self.assertTrue(value['available'])
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual([item['path'] for item in fixture.requests[before:]], list(GET_PATHS))
        self.assertTrue(all(item['method'] == 'GET' for item in fixture.requests[before:]))

    def test_login_response_must_be_exact_ok_and_cannot_redirect(self):
        for body in (b'ok.', b'Ok', b'Ok.\n', b'Ok. trailing', b'Fails.', b'<html>Ok.</html>'):
            with self.subTest(body=body):
                fixture = _Fixture(self, reply=lambda item, _number: response(body, headers=[('Set-Cookie', 'SID=' + FAKE_SID)])
                                   if item['method'] == 'POST' else None)
                self.assert_failure(self.collect(fixture, state=download.QbAuthState()), 'auth_rejected', 'invalid_response')
                self.assertEqual(len(fixture.requests), 2)
        fixture = _Fixture(self, reply=lambda item, _number: response(status=302, headers=[
            ('Location', 'http://external.invalid/' + FAKE_SID), ('Set-Cookie', 'SID=' + FAKE_SID)])
                           if item['method'] == 'POST' else None)
        self.assert_failure(self.collect(fixture), 'invalid_response', 'auth_rejected')
        self.assertEqual(len(fixture.requests), 2)

    def test_identity_change_after_rejection_stops_before_credentials_are_sent(self):
        fixture = _Fixture(self)
        def owner(**_):
            return dict(OWNER, socket_inode='54321') if fixture.requests else dict(OWNER)
        value = self.collect(fixture, owner=owner)
        self.assert_failure(value, 'identity_changed')
        self.assertEqual(len(fixture.requests), 1)
        self.assertEqual(fixture.posts(), [])

    def test_identity_change_during_login_discards_sid_before_any_authed_get(self):
        fixture = _Fixture(self)
        def owner(**_):
            return dict(OWNER, start_ticks=101) if fixture.posts() else dict(OWNER)
        value = self.collect(fixture, owner=owner)
        self.assert_failure(value, 'identity_changed')
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual(len(fixture.requests), 2)
        self.now[0] += download.QB_AUTH_BACKOFF_SECONDS + 1
        self.assertTrue(self.collect(fixture)['available'])
        self.assertNotIn('cookie', fixture.requests[2]['headers'])
        self.assertEqual(len(fixture.posts()), 2)

    def test_new_process_or_new_credentials_cannot_receive_old_sid(self):
        for change in ('pid', 'start_ticks', 'socket_inode', 'credentials'):
            with self.subTest(change=change):
                fixture = _Fixture(self)
                state = download.QbAuthState()
                self.assertTrue(self.collect(fixture, state=state)['available'])
                offset = len(fixture.requests)
                owner = dict(OWNER)
                credentials = FAKE_CREDENTIALS
                if change == 'credentials':
                    credentials = ('new-fixture-user', 'new-fixture-password')
                    fixture.credentials = credentials
                else:
                    owner[change] = {'pid': 43, 'start_ticks': 101, 'socket_inode': '99999'}[change]
                value = self.collect(fixture, state=state, credentials=credentials, owner=lambda **_: owner)
                self.assertTrue(value['available'])
                self.assertNotIn('cookie', fixture.requests[offset]['headers'])
                self.assertEqual(len(fixture.posts()), 2)

    def test_missing_or_unverified_listener_cannot_receive_credentials(self):
        fixture = _Fixture(self)
        for status in ('proc_unavailable', 'proc_partial', 'not_listening', 'owner_unverified', 'ambiguous'):
            value = self.collect(fixture, owner=lambda **_: {'available': False, 'status': status})
            self.assert_failure(value, status)
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.connections, [])

    def test_sid_is_bound_to_the_verified_loopback_address_family(self):
        first, second = _Fixture(self), _Fixture(self, host='::1')
        self.assertTrue(self.collect(first)['available'])
        value = self.collect(second)
        self.assertTrue(value['available'])
        self.assertNotIn('cookie', second.requests[0]['headers'])
        self.assertEqual(len(second.posts()), 1)

    def test_invalid_internal_credential_configuration_cannot_be_posted(self):
        fixture = _Fixture(self)
        for invalid in ((), ('one',), ('a', 'b', 'c'), ('user', None),
                        (None, 'password'), (True, False), ('', 'password'),
                        ('user', ''), ('x' * 4097, 'password'), ('fixture\ud800', 'password')):
            with self.subTest(kind=type(invalid).__name__, size=len(invalid)):
                value = self.collect(fixture, state=download.QbAuthState(), credentials=invalid)
                self.assert_failure(value, 'auth_config_invalid')
        self.assertEqual(fixture.posts(), [])

    def test_failed_login_with_temporarily_partial_proc_does_not_reset_backoff(self):
        fixture = _Fixture(self, reply=lambda item, _number: response(b'Fails.')
                           if item['method'] == 'POST' else None)
        def owner(**_):
            return {'available': False, 'status': 'proc_partial'} if fixture.posts() else dict(OWNER)
        value = self.collect(fixture, owner=owner)
        self.assert_failure(value, 'proc_partial')
        self.assertEqual(len(fixture.posts()), 1)
        self.assert_failure(self.collect(fixture), 'auth_backoff')
        self.assertEqual(len(fixture.posts()), 1)

    def test_one_budget_includes_rejection_login_and_all_authenticated_gets(self):
        fixture = _Fixture(self)
        def reply(item, _number):
            self.now[0] = 1000.0 + (download.QB_NETWORK_SECONDS - 0.1 if item['method'] == 'GET'
                                   else download.QB_NETWORK_SECONDS + 0.1)
            return None
        fixture.reply = reply
        self.assert_failure(self.collect(fixture), 'timeout')
        self.assertEqual(len(fixture.requests), 2)
        self.assertEqual(len(fixture.posts()), 1)

    def test_login_body_header_and_encoding_limits_are_enforced(self):
        variants = [b'HTTP/1.1 200 OK\r\nContent-Length: 131073\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nX: ' + b'x' * 9000,
                    b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nOk.',
                    b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Encoding: gzip\r\n\r\nOk.',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nOk.\r\n0\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nOk.']
        for raw in variants:
            with self.subTest(variant=variants.index(raw)):
                fixture = _Fixture(self, reply=lambda item, _number: raw if item['method'] == 'POST' else None)
                value = self.collect(fixture, state=download.QbAuthState())
                self.assert_failure(value, 'limit_exceeded', 'invalid_response', 'auth_rejected')
                self.assertEqual(len(fixture.requests), 2)

    def test_parallel_authentication_shares_state_and_does_not_create_login_storm(self):
        started, release = threading.Event(), threading.Event()
        fixture = _Fixture(self)
        fixture.cleanup_events.append(release)
        def reply(item, _number):
            if item['method'] == 'POST':
                started.set()
                if not release.wait(3):
                    return response(status=503)
            return None
        fixture.reply = reply
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.collect, fixture)
            try:
                self.assertTrue(started.wait(2))
                second = pool.submit(self.collect, fixture)
                self.assert_failure(second.result(timeout=1), 'auth_busy')
            finally:
                release.set()
            self.assertTrue(first.result(timeout=2)['available'])
        self.assertEqual(len(fixture.posts()), 1)

    def test_transport_exceptions_and_public_aggregate_do_not_leak_auth_material(self):
        fixture = _Fixture(self)
        self.assertTrue(self.collect(fixture)['available'])
        with mock.patch.object(fixture, 'connect', side_effect=OSError(FAKE_PASSWORD + FAKE_SID)):
            value = self.collect(fixture)
        self.assert_failure(value, 'transport_error')
        aggregated = download.collect(aria2=lambda **_: {'available': False, 'status': 'not_listening',
                                                       'reason': 'fixture', 'data': None},
                                      qbittorrent=lambda **_: value)
        self.assert_private(aggregated)
        self.assert_private({'state_repr': repr(self.state)})
        before = len(fixture.requests)
        self.assertTrue(self.collect(fixture)['available'])
        self.assertEqual(len(fixture.posts()), 1)
        self.assertEqual([item['path'] for item in fixture.requests[before:]], list(GET_PATHS))
        self.assertTrue(all(item['headers'].get('cookie') == 'SID=' + FAKE_SID
                            for item in fixture.requests[before:]))


if __name__ == '__main__':
    unittest.main()
