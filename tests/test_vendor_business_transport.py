"""Fake TLS sockets verify that credentials never reach an unverified peer."""

import importlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
business = importlib.import_module('vendor_business_transport')
IDENTITY = {'available': True, 'status': 'verified', 'pid': 12, 'start_ticks': 88,
            'socket_inode': '555', 'connect_host': '127.0.0.1'}
TOKEN = 'PRIVATE-EXISTING-OWNER-TOKEN:123:100000'


def response(frame, status=200, headers=None):
    body = json.dumps(frame, separators=(',', ':')).encode('utf8')
    extra = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
    extra.update(headers or {})
    return (f'HTTP/1.1 {status} OK\r\n' + ''.join(k + ': ' + v + '\r\n'
            for k, v in extra.items()) + '\r\n').encode('ascii') + body


class Connection:
    def __init__(self, data=None, certificate=b'CERT', send_error=None):
        self.data = response({'code': 0}) if data is None else data
        self.certificate, self.send_error = certificate, send_error
        self.sent, self.timeouts, self.connected, self.closed = [], [], None, False

    def connect(self, address):
        self.connected = address

    def settimeout(self, value):
        self.timeouts.append(value)

    def getpeercert(self, binary_form=False):
        assert binary_form
        return self.certificate

    def sendall(self, value):
        self.sent.append(value)
        if self.send_error is not None:
            raise self.send_error

    def recv(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.events, self.families = [], []
        self.identity_values = [IDENTITY]
        self.certificates = [b'CERT']
        self.now = 50.0
        self.session = SimpleNamespace(user_id=100000, derive_access_token=lambda: TOKEN)
        self.authorizer = lambda *a: True

    def client(self):
        def listener(**kwargs):
            self.events.append(('identity', kwargs))
            value = self.identity_values[0]
            if len(self.identity_values) > 1:
                self.identity_values.pop(0)
            return value

        def certificate(root):
            self.events.append(('certificate', root))
            value = self.certificates[0]
            if len(self.certificates) > 1:
                self.certificates.pop(0)
            return value

        def session_reader(**kwargs):
            self.events.append(('credential', kwargs))
            return self.session, {'available': True, 'status': 'ready'}

        def factory(family, kind):
            self.families.append((family, kind))
            return self.connection

        context = SimpleNamespace(wrap_socket=lambda raw, server_hostname: raw)
        return business.VendorBusinessClient(root='/FIXTURE', session_reader=session_reader,
                listener_check=listener, certificate_reader=certificate,
                socket_factory=factory, tls_context=context, clock=lambda: self.now)

    def request(self, operation='albums_list', parameters=None, body=None, confirmed=False):
        return self.client().request(operation, parameters, body,
                                     self.authorizer, confirmed=confirmed)

    def assert_error(self, reason, *args, **kwargs):
        with self.assertRaises(business.BusinessTransportError) as raised:
            self.request(*args, **kwargs)
        self.assertEqual(raised.exception.reason, reason)
        self.assertNotIn(TOKEN, str(raised.exception))
        return raised.exception

    def test_fixed_request_is_pinned_and_identity_checked_before_and_after(self):
        self.assertEqual(self.request(), (200, {'code': 0}))
        request = self.connection.sent[0].decode('ascii')
        self.assertTrue(request.startswith('GET /rest/1.1/album?action=get_all_album_list&access_token='))
        self.assertIn('Connection: close\r\n', request)
        self.assertEqual(self.connection.connected, ('127.0.0.1', 20443))
        self.assertEqual(len([e for e in self.events if e[0] == 'identity']), 4)
        first_credential = next(i for i, e in enumerate(self.events) if e[0] == 'credential')
        self.assertGreater(first_credential, 3)
        self.assertTrue(self.connection.closed)
        self.assertTrue(all(0 < t <= business.TIMEOUT for t in self.connection.timeouts))

    def test_invalid_operation_parameters_or_missing_authorization_touch_nothing(self):
        for operation, parameters, body in (('http://evil', {}, {}),
                ('albums_list', {'action': 'format'}, {}), ('albums_list', {}, {'url': 'evil'}),
                ('albums_create', {'album_name': 'x\r\nHost:evil'}, {}),
                ('movie_library_rename', {}, {'lid': 1, 'name': 'a&members=0'})):
            self.assert_error('unsupported_operation' if operation == 'http://evil' else 'invalid_parameters',
                              operation, parameters, body, confirmed=True)
        self.authorizer = None
        self.assert_error('authorization_denied')
        self.assertEqual(self.events, [])
        self.assertFalse(self.connection.sent)

    def test_write_cannot_use_truthy_confirmation_or_truthy_authorizer(self):
        for confirmed in (False, 'true', 1, None):
            self.assert_error('confirmation_required', 'albums_create', {'album_name': 'x'}, {}, confirmed=confirmed)
        self.authorizer = lambda *a: 1
        self.assert_error('authorization_denied', 'albums_create', {'album_name': 'x'}, {}, confirmed=True)
        self.assertEqual(self.events, [])

    def test_identity_certificate_or_owner_failure_never_sends_credentials(self):
        self.identity_values = [dict(IDENTITY, available=False)]
        self.assert_error('identity_unverified')
        self.assertFalse(any(e[0] == 'credential' for e in self.events))
        self.identity_values = [IDENTITY]
        self.connection.certificate = b'WRONG'
        self.assert_error('certificate_mismatch')
        self.assertFalse(any(e[0] == 'credential' for e in self.events))
        self.connection.certificate = b'CERT'
        self.identity_values = [IDENTITY, dict(IDENTITY, start_ticks=89)]
        self.assert_error('identity_changed')
        self.assertFalse(any(e[0] == 'credential' for e in self.events))
        self.identity_values = [IDENTITY]
        self.certificates = [b'CERT', b'CHANGED']
        self.assert_error('certificate_changed')
        self.assertFalse(any(e[0] == 'credential' for e in self.events))
        self.certificates = [b'CERT']
        self.session.user_id = 100001
        self.assert_error('credential_unavailable')
        self.assertFalse(self.connection.sent)

    def test_policy_is_rechecked_after_tls_before_request(self):
        decisions = iter((True, False))
        self.authorizer = lambda *a: next(decisions)
        self.assert_error('authorization_denied', 'albums_create', {'album_name': 'x'}, {}, confirmed=True)
        self.assertFalse(self.connection.sent)

    def test_revocation_during_last_identity_scan_prevents_credential_send(self):
        active, count = [True], [0]
        client = self.client()
        original = client._listener
        def revoke(**kwargs):
            count[0] += 1
            value = original(**kwargs)
            if count[0] == 3:
                active[0] = False
            return value
        client._listener = revoke
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('albums_create', {'album_name': 'x'}, {},
                           lambda *a: active[0], confirmed=True)
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertFalse(raised.exception.request_sent)
        self.assertFalse(self.connection.sent)

    def test_final_authorization_cannot_outlive_absolute_deadline(self):
        count = [0]
        def authorize(*args):
            count[0] += 1
            if count[0] == 3:
                self.now += business.TIMEOUT + 1
            return True
        self.authorizer = authorize
        error = self.assert_error('timeout', 'albums_create', {'album_name': 'x'}, {}, confirmed=True)
        self.assertFalse(error.request_sent)
        self.assertFalse(self.connection.sent)

    def test_callback_cannot_mutate_parameters_or_share_array(self):
        def authorize(operation, parameters, body):
            parameters['action'] = 'evil'
            body.setdefault('share_id_list', []).append('evil')
            return True
        self.authorizer = authorize
        self.request('share_revoke', {}, {'share_id_list': ['real-id']}, confirmed=True)
        request = self.connection.sent[0]
        self.assertIn(b'action=delete_share_link', request)
        self.assertTrue(request.endswith(b'{"share_id_list":["real-id"]}'))
        self.assertNotIn(b'evil', request)

    def test_ipv6_uses_its_verified_family_and_never_assumes_dual_stack(self):
        self.identity_values = [dict(IDENTITY, connect_host='::1')]
        self.request()
        self.assertEqual(self.families, [(socket.AF_INET6, socket.SOCK_STREAM)])
        self.assertEqual(self.connection.connected, ('::1', 20443))

    def test_redirect_http_error_and_malformed_body_never_retry_or_echo(self):
        for data, reason in ((response({'msg': TOKEN}, 302), 'redirect_refused'),
                            (response({'msg': TOKEN}, 403), 'http_error'),
                            (b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n\r\n{}', 'unsupported_content_type'),
                            (response({'code': 0})[:-1], 'incomplete_response')):
            self.connection = Connection(data)
            error = self.assert_error(reason)
            self.assertTrue(error.request_sent)
            self.assertEqual(len(self.connection.sent), 1)

    def test_partial_write_send_failure_is_uncertain_without_retries(self):
        self.connection.send_error = OSError(TOKEN)
        error = self.assert_error('transport_failed', 'albums_create', {'album_name': 'x'}, {}, confirmed=True)
        self.assertTrue(error.request_sent)
        self.assertEqual(len(self.connection.sent), 1)

    def test_identity_change_after_response_discards_the_frame(self):
        self.identity_values = [IDENTITY, IDENTITY, IDENTITY, dict(IDENTITY, socket_inode='556')]
        error = self.assert_error('identity_changed')
        self.assertTrue(error.request_sent)

    def test_elapsed_identity_scan_cannot_reset_absolute_deadline(self):
        client = self.client()
        original = client._listener
        def slow(**kwargs):
            result = original(**kwargs)
            self.now += business.TIMEOUT + 1
            return result
        client._listener = slow
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('albums_list', {}, {}, self.authorizer)
        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertFalse(self.connection.sent)

    def test_bounded_parser_rejects_duplicate_json_keys_and_large_frames(self):
        for body in (b'{"code":0,"code":1}', b'{"code":0,"x":"' + b'a' * 70000 + b'"}'):
            self.connection = Connection(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
            with self.assertRaises(business.BusinessTransportError) as raised:
                self.request()
            self.assertIn(raised.exception.reason, ('invalid_json', 'body_too_large'))


if __name__ == '__main__':
    unittest.main()
