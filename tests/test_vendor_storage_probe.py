"""Isolated storage protocol/credential fixtures; never call a NAS or real DB."""
import copy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_storage_probe as probe


TOKEN = 'f' * 32 + ':123456:100000'


class Session:
    user_id = 100000

    def derive_access_token(self):
        return TOKEN


def reader(**_):
    return Session(), {'available': True, 'status': 'ready', 'user_id': 100000,
                       'token': 'PRIVATE-BASE-TOKEN', 'source_id': 'PRIVATE-ACCOUNT'}


def native():
    return {'code': 0, 'msg': 'PRIVATE-MESSAGE', 'user_id': 'PRIVATE-USER',
            'space_list': [{'space_nickname': '视频资源', 'space_name': '', 'space_id': 'PRIVATE-UUID',
                            'path': '/private/path', 'disk_info': [
                                {'disk_sn': 'SERIAL-A-012345', 'port': 1, 'disk_name': 'sda',
                                 'disk_serial': 'WRONG-ALIAS', 'user': 'PRIVATE-USER'}]},
                           {'space_name': '备份磁盘', 'disk_info': [
                               {'disk_sn': 'SERIAL-B-678901', 'port': 2, 'disk_name': 'sdb'}]}],
            'unused_disk_list': [{'disk_sn': 'SERIAL-C-234567', 'port': 3}]}


def response(body=None, status=200, extra=b'', *, length=True):
    body = native() if body is None else body
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf8')
    return (f'HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n'.encode()
            + (f'Content-Length: {len(raw)}\r\n'.encode() if length else b'') + extra + b'\r\n' + raw)


class MemorySocket:
    def __init__(self, raw, *, short=False, receive=None):
        self.raw, self.sent, self.closed, self.timeouts = raw, b'', False, []
        self.short, self.receive = short, receive

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, value):
        self.sent += value

    def recv(self, count):
        if self.receive:
            self.receive()
        count = min(count, 7) if self.short else count
        part, self.raw = self.raw[:count], self.raw[count:]
        return part

    def close(self):
        self.closed = True


class StorageProbeTests(unittest.TestCase):
    def wire(self, raw=None, **kwargs):
        connection = MemorySocket(response() if raw is None else raw, **kwargs)
        with mock.patch.object(probe.socket, 'create_connection', return_value=connection) as connect:
            value = probe.collect_storage_probe(root='/fixture', session_reader=reader)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 80))
        self.assertTrue(connection.closed)
        return value, connection

    def assert_private(self, result):
        text = json.dumps(result, ensure_ascii=False)
        for secret in (TOKEN, 'PRIVATE-BASE-TOKEN', 'PRIVATE-ACCOUNT', 'PRIVATE-MESSAGE',
                       'PRIVATE-USER', 'PRIVATE-UUID', '/private/path', 'WRONG-ALIAS',
                       'access_token', 'source_id', 'user_id'):
            self.assertNotIn(secret, text)

    def test_fixed_signed_get_and_native_serial_names_are_safely_projected(self):
        with mock.patch.dict('os.environ', {'HTTP_PROXY': 'http://forbidden', 'ALL_PROXY': 'http://forbidden'}):
            value, connection = self.wire(short=True)
        self.assertTrue(value['available'], value)
        self.assertTrue(value['attempted'])
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['http_status'], 200)
        self.assertEqual(value['native_code'], 0)
        self.assertTrue(value['credential_read_only'])
        self.assertTrue(value['native_query_may_update_state'])
        self.assertGreaterEqual(value['elapsed_ms'], 0)
        first = connection.sent.split(b'\r\n', 1)[0].decode('ascii').split(' ')
        self.assertEqual(first[0], 'GET')
        target = urlsplit(first[1])
        self.assertEqual(target.path, '/rest/1.2/config')
        self.assertEqual(parse_qs(target.query), {
            'action': ['space_manage'], 'check_warning': ['1'], 'access_token': [TOKEN]})
        self.assertIn(b'Host: 127.0.0.1:80\r\n', connection.sent)
        self.assertIn(b'Accept-Encoding: identity\r\n', connection.sent)
        self.assertEqual(connection.sent.count(b'GET '), 1)
        rows = value['data']['disks']
        self.assertEqual([row['disk_sn'] for row in rows],
                         ['SERIAL-A-012345', 'SERIAL-B-678901', 'SERIAL-C-234567'])
        self.assertEqual(rows[0]['space_nickname'], '视频资源')
        self.assertIsNone(rows[0]['space_name'])
        self.assertEqual(rows[1]['space_name'], '备份磁盘')
        self.assertEqual(rows[0]['port'], 1)
        self.assertEqual(rows[0]['disk_name'], 'sda')
        self.assertIsNone(rows[2]['disk_name'])
        self.assert_private(value)

    def test_missing_failed_or_nonowner_session_never_contacts_transport(self):
        cases = [(None, {'available': False, 'status': 'no_session', 'reason': TOKEN}),
                 (Session(), {'available': True, 'status': 'ready', 'user_id': 99999}),
                 (Session(), {'available': True, 'status': 'ready', 'user_id': 100000.0}),
                 (Session(), {'available': 1, 'status': 'ready', 'user_id': 100000})]
        for session in cases:
            transport = mock.Mock(side_effect=AssertionError('No request'))
            value = probe.collect_storage_probe(session_reader=lambda **_: session, transport=transport)
            self.assertFalse(value['available'])
            self.assertFalse(value['attempted'])
            self.assertEqual(value['reason'], 'credential_unavailable')
            transport.assert_not_called()
            self.assert_private(value)

    def test_session_and_signing_failures_are_fixed_private_classifications(self):
        def broken_reader(**_):
            raise OSError(TOKEN)
        value = probe.collect_storage_probe(session_reader=broken_reader)
        self.assertEqual(value['reason'], 'credential_read_failed')
        self.assertFalse(value['attempted'])

        class BadSession(Session):
            def derive_access_token(self):
                raise ValueError(TOKEN)

        value = probe.collect_storage_probe(session_reader=lambda **_: (
            BadSession(), {'available': True, 'status': 'ready', 'user_id': 100000}))
        self.assertEqual(value['reason'], 'token_unavailable')
        self.assertFalse(value['attempted'])
        self.assert_private(value)

    def test_token_header_injection_rejected_and_query_characters_quoted(self):
        class BadSession(Session):
            def derive_access_token(self):
                return 'signature\r\nInjected: bad'

        transport = mock.Mock(side_effect=AssertionError('No request'))
        value = probe.collect_storage_probe(session_reader=lambda **_: (
            BadSession(), {'available': True, 'status': 'ready', 'user_id': 100000}), transport=transport)
        self.assertEqual(value['reason'], 'token_unavailable')
        self.assertFalse(value['attempted'])
        transport.assert_not_called()
        connection = MemorySocket(response())
        with mock.patch.object(probe.socket, 'create_connection', return_value=connection):
            probe.query_storage_frame('opaque&PROXY_TARGET=elsewhere?x=1')
        target = connection.sent.split(b'\r\n', 1)[0].decode().split(' ')[1]
        self.assertEqual(set(parse_qs(urlsplit(target).query)), {'action', 'check_warning', 'access_token'})

    def test_native_failure_and_bad_code_do_not_expose_raw_body(self):
        for code in (2, -400):
            value, _ = self.wire(response({'code': code, 'message': TOKEN, 'space_list': native()['space_list']}))
            self.assertEqual(value['status'], 'native_error')
            self.assertEqual(value['native_code'], code)
            self.assertIsNone(value['data'])
            self.assert_private(value)
        for frame in ({'code': False}, {'code': '0'}, {'code': 2 ** 60}, {'code': None}):
            value, _ = self.wire(response(frame))
            self.assertEqual(value['reason'], 'native_code_unavailable')
            self.assertIsNone(value['data'])

    def test_native_success_without_code_preserves_null_and_ancestor_space_names(self):
        frame = {'space_list': [{'space_nickname': 'SSD', 'space_name': '',
                                'disk_info': [{'disk_sn': 'SERIAL-A', 'port': 1}],
                                'expanding_disk_info': {'disk_sn': 'SERIAL-B', 'port': 2},
                                'member': {'disk_sn': 'NOT-AN-AUDITED-RECORD'}}], 'unused_disk_list': []}
        value, _ = self.wire(response(frame))
        self.assertTrue(value['available'], value)
        self.assertIsNone(value['native_code'])
        self.assertEqual(value['data']['disks'][0]['disk_sn'], 'SERIAL-A')
        self.assertEqual(value['data']['disks'][0]['space_nickname'], 'SSD')
        self.assertIsNone(value['data']['disks'][0]['space_name'])
        self.assertEqual(value['data']['disks'][1]['disk_sn'], 'SERIAL-B')
        self.assertEqual(value['data']['disks'][1]['space_nickname'], 'SSD')
        self.assertNotIn('NOT-AN-AUDITED-RECORD', json.dumps(value))

    def test_redirect_http_error_duplicate_headers_and_injection_are_not_followed(self):
        for raw, reason in (
                (response(TOKEN.encode(), status=302, extra=b'Location: http://forbidden\r\n'), 'redirect_refused'),
                (response(TOKEN.encode(), status=403), 'http_error'),
                (response(extra=b'Content-Length: 1\r\n'), 'invalid_http'),
                (response(extra=b'X-Unsafe: value\rbroken\r\n'), 'invalid_http'),
                (response(extra=b'X-Unsafe: value\x00broken\r\n'), 'invalid_http')):
            value, _ = self.wire(raw)
            self.assertFalse(value['available'])
            self.assertEqual(value['reason'], reason)
            self.assertIsNone(value['data'])
            self.assert_private(value)

    def test_strict_json_depth_duplicates_nonfinite_and_shape(self):
        deep = '[]'
        for _ in range(probe.MAX_JSON_DEPTH + 1):
            deep = '[' + deep + ']'
        for raw in (b'{"code":0,"code":0}', b'{"code":0,"extra":NaN}', b'[]',
                    ('{"code":0,"deep":' + deep + '}').encode(), b'\xff', b'{"code":'):
            value, _ = self.wire(response(raw))
            self.assertEqual(value['reason'], 'invalid_json')
            self.assertIsNone(value['data'])

    def test_body_header_and_json_item_limits(self):
        variants = [(b'HTTP/1.1 200 OK\r\nX: ' + b'x' * probe.MAX_HEADERS, 'headers_too_large'),
                    (f'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {probe.MAX_BODY + 1}\r\n\r\n'.encode(), 'body_too_large'),
                    (response({'code': 0, 'extra': [None] * probe.MAX_JSON_ITEMS}), 'invalid_json')]
        for raw, reason in variants:
            value, _ = self.wire(raw)
            self.assertEqual(value['reason'], reason)

    def test_identity_chunked_gzip_and_connection_close_framing(self):
        raw = json.dumps(native()).encode()
        chunks = b''.join(f'{len(part):x}\r\n'.encode() + part + b'\r\n'
                          for part in (raw[:17], raw[17:])) + b'0\r\n\r\n'
        for encoded in (
                response(raw, length=False),
                response(chunks, extra=b'Transfer-Encoding: chunked\r\n', length=False),
                response(gzip.compress(raw), extra=b'Content-Encoding: gzip\r\n'),
                response(gzip.compress(raw), extra=b'Content-Encoding: gzip\r\n', length=False)):
            value, _ = self.wire(encoded, short=True)
            self.assertTrue(value['available'], value)

    def test_malformed_chunk_gzip_bomb_and_transfer_conflicts_fail_closed(self):
        for encoded, reason in (
                (response(b'1\r\nxBAD0\r\n\r\n', extra=b'Transfer-Encoding: chunked\r\n', length=False), 'invalid_http'),
                (response(b'0\r\nX-Trailer: bad\r\n\r\n', extra=b'Transfer-Encoding: chunked\r\n', length=False), 'invalid_http'),
                (response(b'0\r\n\r\n', extra=b'Transfer-Encoding: chunked\r\n'), 'invalid_http'),
                (response(b'bad', extra=b'Content-Encoding: gzip\r\n'), 'unsupported_encoding'),
                (response(gzip.compress(b'x' * (probe.MAX_BODY + 1)), extra=b'Content-Encoding: gzip\r\n'), 'body_too_large'),
                (response(b'{}', extra=b'Content-Encoding: br\r\n'), 'unsupported_encoding')):
            value, _ = self.wire(encoded)
            self.assertEqual(value['reason'], reason)
            self.assertIsNone(value['data'])

    def test_absolute_deadline_and_network_failure_are_private(self):
        now = [0.0]
        connection = MemorySocket(response(), receive=lambda: now.__setitem__(0, probe.TOTAL_TIMEOUT + 1))
        with mock.patch.object(probe.time, 'monotonic', side_effect=lambda: now[0]), \
                mock.patch.object(probe.socket, 'create_connection', return_value=connection):
            value = probe.collect_storage_probe(session_reader=reader)
        self.assertEqual(value['reason'], 'timeout')
        self.assertTrue(connection.closed)
        for failure, expected in ((OSError(TOKEN), 'transport_failed'), (TimeoutError(TOKEN), 'timeout')):
            with mock.patch.object(probe.socket, 'create_connection', side_effect=failure):
                value = probe.collect_storage_probe(session_reader=reader)
            self.assertEqual(value['reason'], expected)
            self.assert_private(value)

    def test_missing_and_conflicting_fields_stay_null_without_alias_guessing(self):
        frame = {'code': 0, 'space_list': [{'space_name': 'SSD', 'disk_info': [
            {'disk_sn': None, 'disk_serial': 'NOT-A-FALLBACK', 'port': True, 'disk_name': '/dev/sda'},
            {'disk_sn': 'SERIAL-A', 'port': 2}, {'disk_sn': 'SERIAL-B', 'port': 2}]}],
            'unused_disk_list': []}
        value, _ = self.wire(response(frame))
        self.assertTrue(value['available'])
        self.assertEqual(value['status'], 'partial')
        rows = value['data']['disks']
        self.assertTrue(all(row['disk_sn'] is None for row in rows))
        self.assertIsNone(rows[0]['port'])
        self.assertIsNone(rows[0]['disk_name'])
        self.assertTrue(any(row['conflict'] for row in rows))
        self.assertNotIn('NOT-A-FALLBACK', json.dumps(value))

    def test_same_serial_on_different_explicit_devices_without_ports_is_conflicted(self):
        frame = {'space_list': [{'space_nickname': 'SSD', 'disk_info': [
            {'disk_sn': 'SERIAL-A', 'disk_name': 'sda'},
            {'disk_sn': 'SERIAL-A', 'disk_name': 'sdb'}]}], 'unused_disk_list': []}
        value, _ = self.wire(response(frame))
        self.assertEqual(value['status'], 'partial')
        self.assertTrue(all(row['conflict'] and row['disk_sn'] is None
                            and row['disk_name'] is None for row in value['data']['disks']))

    def test_reflected_signature_and_private_labels_never_escape(self):
        frame = native()
        frame['space_list'][0]['space_nickname'] = 'prefix-' + TOKEN
        frame['space_list'][0]['space_name'] = '/private/path'
        frame['space_list'][0]['disk_info'][0]['disk_sn'] = TOKEN
        value, _ = self.wire(response(frame))
        self.assertEqual(value['status'], 'partial')
        self.assertIsNone(value['data']['disks'][0]['disk_sn'])
        self.assertIsNone(value['data']['spaces'][0]['space_nickname'])
        self.assertIsNone(value['data']['spaces'][0]['space_name'])
        self.assert_private(value)

    def test_real_empty_lists_and_unsupported_schema_have_distinct_results(self):
        value, _ = self.wire(response({'code': 0, 'space_list': [], 'unused_disk_list': []}))
        self.assertTrue(value['available'])
        self.assertEqual(value['data'], {'disks': [], 'spaces': []})
        value, _ = self.wire(response({'code': 0, 'not_storage': []}))
        self.assertFalse(value['available'])
        self.assertEqual(value['reason'], 'schema_unavailable')

    def test_transport_injection_has_fixed_signature_and_cannot_return_arbitrary_payload(self):
        calls = []

        def transport(token, *, root):
            calls.append((token, root))
            return 200, native()

        value = probe.collect_storage_probe(root='/fixture', session_reader=reader, transport=transport)
        self.assertTrue(value['available'])
        self.assertEqual(calls, [(TOKEN, '/fixture')])
        for bad in (None, (200,), ('200', native()), (200, [])):
            value = probe.collect_storage_probe(session_reader=reader, transport=lambda *_a, **_k: bad)
            self.assertFalse(value['available'])
            self.assertIsNone(value['data'])
            self.assert_private(value)

    def test_real_loopback_http_fixture_issues_one_fixed_request(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Host')))
                frame = native()
                frame.pop('code')
                raw = json.dumps(frame, ensure_ascii=False).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = socket.create_connection
        try:
            def connect(address, timeout):
                self.assertEqual(address, ('127.0.0.1', 80))
                return original(server.server_address, timeout=timeout)
            with mock.patch.object(probe.socket, 'create_connection', side_effect=connect):
                value = probe.collect_storage_probe(session_reader=reader)
            self.assertTrue(value['available'], value)
            self.assertIsNone(value['native_code'])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][1], '127.0.0.1:80')
            self.assertEqual(parse_qs(urlsplit(requests[0][0]).query)['access_token'], [TOKEN])
            self.assert_private(value)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
