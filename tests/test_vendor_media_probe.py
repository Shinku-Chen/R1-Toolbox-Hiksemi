"""固定原厂媒体探测的投影、首帧、TLS pin 和失败边界。"""

from contextlib import ExitStack
import importlib.util
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('vendor_media_probe', ROOT / 'runtime/vendor_media_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
SECRET = 'private-session-value'
CERT = b'test-owned-peer-certificate'
FRAME = {'code': 0, 'msg': SECRET, 'total': 12, 'totalpic': 10, 'totalvid': 2,
         'processed': 6, 'processedpic': 5, 'processedvid': 1,
         'left': 6, 'status': 1, 'range_status': 0, 'percent': 100,
         'access_token': SECRET, 'path': '/private/photos', 'user_name': 'private-user'}
SAFE_CREDENTIAL = {'available': True, 'status': 'ready', 'reason': None, 'user_id': 100000,
                   'records_checked': 2, 'candidate_count': 1,
                   'selection': 'newest_in_vendor_window'}

# 公开的测试专用证书与私钥，不属于 NAS，不需要 openssl 或第三方测试依赖。
_OWNED_CERT = '''-----BEGIN CERTIFICATE-----
MIIBXjCCAQSgAwIBAgIDAYagMAoGCCqGSM49BAMCMCwxKjAoBgNVBAMMIVIxIFRv
b2xib3ggb3duZWQgVExTIHRlc3QgZml4dHVyZTAgFw0yMDAxMDEwMDAwMDBaGA8y
MTAwMDEwMTAwMDAwMFowLDEqMCgGA1UEAwwhUjEgVG9vbGJveCBvd25lZCBUTFMg
dGVzdCBmaXh0dXJlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEPlMKp/dMOfqg
t6dNUCpjcjC3vT9PPD/eIqQlzoOdTK6CoHhuSVHdZBFYx8EQc8+JxZ1BPlQDOC9s
U3Hqt7eKdaMTMBEwDwYDVR0RBAgwBocEfwAAATAKBggqhkjOPQQDAgNIADBFAiEA
+JA8NcPDRHLUtsFgoE2yPH3XJnSajecZ5FdM0+YlqMcCIF3ioWSmt5c7n3cruh+r
KLHjgBGPOr+Nh6B+xqqv9KiP
-----END CERTIFICATE-----
'''
_OWNED_KEY = '''-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgy+WxJCRZCM9k/snf
3q51DiJOZ5i3mbbWBuRZIfx/V9qhRANCAAQ+Uwqn90w5+qC3p01QKmNyMLe9P088
P94ipCXOg51MroKgeG5JUd1kEVjHwRBzz4nFnUE+VAM4L2xTceq3t4p1
-----END PRIVATE KEY-----
'''


class Session:
    user_id = 100000
    source_id = SECRET
    login_time = 123456
    base_token = SECRET

    def derive_access_token(self):
        return SECRET


def multipart(frame=FRAME, *, raw=None, boundary=b'--myboundary', declared_boundary='--myboundary'):
    payload = json.dumps(frame).encode('utf-8') if raw is None else raw
    body = (boundary + b'\r\nContent-type: application/json\r\nContent-Length: '
            + str(len(payload)).encode('ascii') + b'\r\n\r\n' + payload)
    headers = ('HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary='
               + declared_boundary + '\r\n\r\n').encode('ascii')
    return headers, body


class FakeSocket:
    """recv 只交付当前已到达片段；不会伪装成等满读取。"""

    def __init__(self, chunks=(), events=None):
        self.chunks = list(chunks)
        self.events = events if events is not None else []
        self.sent = []
        self.targets = []
        self.timeouts = []
        self.closed = False
        self.receives = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, target):
        self.targets.append(target)

    def getpeercert(self, binary_form=False):
        self.events.append('pin')
        assert binary_form
        return CERT

    def sendall(self, value):
        self.events.append('request')
        self.sent.append(value)

    def recv(self, count):
        self.receives += 1
        if not self.chunks:
            raise AssertionError('read past first complete frame')
        value = self.chunks.pop(0)
        if isinstance(value, Exception):
            raise value
        if len(value) > count:
            self.chunks.insert(0, value[count:])
        return value[:count]

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def run_transport(self, chunks, *, cert=CERT, token=SECRET, wire=None, query_id=None,
                      parameters=None):
        raw = FakeSocket()
        wire = wire or FakeSocket(chunks)
        context = Mock()
        context.wrap_socket.return_value = wire
        with ExitStack() as stack:
            stack.enter_context(patch.object(probe, '_certificate', return_value=cert))
            socket_factory = stack.enter_context(patch.object(probe.socket, 'socket', return_value=raw))
            stack.enter_context(patch.object(probe.ssl, 'SSLContext', return_value=context))
            stack.enter_context(patch.object(probe.socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')))
            try:
                if query_id is None:
                    result = probe.query_first_face_frame(token, root='/fixture-root')
                else:
                    result = probe.query_first_media_frame(token, query_id, root='/fixture-root',
                                                           parameters=parameters)
            finally:
                self.assertTrue(raw.closed)
                self.assertTrue(wire.closed)
                socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
                self.assertEqual(raw.targets, [('127.0.0.1', 20443)])
                context.wrap_socket.assert_called_once_with(raw, server_hostname=None)
                self.assertTrue(all(0 < value <= probe.TOTAL_TIMEOUT for value in raw.timeouts + wire.timeouts))
        return result, wire, context

    def test_audited_multipart_delimiter_returns_then_closes_before_next_eight_second_frame(self):
        header, body = multipart()
        result, wire, _ = self.run_transport([header, body, AssertionError('second frame must not be read')])
        self.assertEqual(result, (200, FRAME))
        self.assertEqual(wire.events, ['pin', 'request'])
        self.assertEqual(wire.receives, 2)
        self.assertEqual(len(wire.chunks), 1)
        request = wire.sent[0].decode('ascii')
        self.assertTrue(request.startswith('GET /rest/1.1/fcabm?action=get_face_ai_status&access_token='))
        self.assertIn('Host: 127.0.0.1:20443\r\n', request)
        self.assertNotIn('Authorization:', request)

    def test_each_media_query_sends_only_its_fixed_native_route_and_action(self):
        expected = {
            'face': '/rest/1.1/fcabm?action=get_face_ai_status',
            'ocr': '/rest/1.1/file?action=get_ocr_status',
            'thumbnail': '/rest/1.1/file?action=get_thumb_convert_process',
            'similar': '/rest/1.1/file?action=get_similar_status',
            'ai': '/rest/1.1/fcabm?action=get_ai_status',
            'scraper': '/rest/1.1/scraper?action=lib_scan_status',
            'shared_album': '/rest/1.1/album?action=share_album_progress&album_id=share_5000&type=1',
            'shared_album_catalog': '/rest/1.1/album?action=get_share_album_list',
        }
        header, body = multipart()
        for query_id, target in expected.items():
            with self.subTest(query_id=query_id):
                parameters = {'album_id': 'share_5000', 'type': '1'} if query_id == 'shared_album' else None
                result, wire, _ = self.run_transport(
                    [header + body, AssertionError('later frame forbidden')], query_id=query_id,
                    parameters=parameters)
                self.assertEqual(result, (200, FRAME))
                self.assertEqual(wire.events, ['pin', 'request'])
                self.assertEqual(len(wire.sent), 1)
                method = 'POST' if query_id == 'scraper' else 'GET'
                self.assertEqual(wire.sent[0].split(b'\r\n', 1)[0],
                                 (method + ' ' + target + '&access_token=' + SECRET
                                  + ' HTTP/1.1').encode('ascii'))
                headers, payload = wire.sent[0].split(b'\r\n\r\n', 1)
                if query_id == 'scraper':
                    self.assertIn(b'Content-Type: application/json', headers)
                    self.assertIn(b'Content-Length: 2', headers)
                    self.assertEqual(payload, b'{}')
                else:
                    self.assertNotIn(b'Content-Length:', headers)
                    self.assertEqual(payload, b'')
                self.assertEqual(len(wire.chunks), 1)

    def test_unknown_query_is_rejected_before_certificate_tls_network_or_token_use(self):
        class UntouchableToken:
            def __str__(self):
                raise AssertionError('unsupported query touched its token')

        unknown = ('', 'Face', 'face&action=erase', '/rest/1.1/login',
                   'http://private.invalid', None, False, 1, [], {}, b'face')
        for query_id in unknown:
            with self.subTest(query_id=query_id), \
                    patch.object(probe, '_certificate') as certificate, \
                    patch.object(probe.socket, 'socket') as factory, \
                    patch.object(probe, 'ssl', None), \
                    patch.object(probe.time, 'monotonic') as clock:
                with self.assertRaises(probe.MediaProbeError) as raised:
                    probe.query_first_media_frame(UntouchableToken(), query_id)
                self.assertEqual(raised.exception.reason, 'unsupported_query')
                certificate.assert_not_called()
                factory.assert_not_called()
                clock.assert_not_called()

    def test_shared_parameters_are_quoted_without_changing_the_fixed_action(self):
        header, body = multipart()
        _, wire, _ = self.run_transport([header + body], query_id='shared_album', parameters={
            'album_id': 'share_5000', 'type': '1&access_token=other',
        })
        target = (b'GET /rest/1.1/album?action=share_album_progress'
                  b'&album_id=share_5000'
                  b'&type=1%26access_token%3Dother&access_token=' + SECRET.encode('ascii')
                  + b' HTTP/1.1')
        self.assertEqual(wire.sent[0].split(b'\r\n', 1)[0], target)

    def test_shared_missing_extra_or_unsafe_parameters_fail_before_certificate_and_network(self):
        valid = {'album_id': 'share_5000', 'type': '1'}
        cases = [None, {}, [], 'share_owned', {'album_id': 'share_owned'},
                 {'type': '1'}, dict(valid, action='erase'), dict(valid, path='/private'),
                 dict(valid, album_id=''), dict(valid, album_id='share_'),
                 dict(valid, album_id='other_owner'), dict(valid, album_id='share_' + 'a' * 251),
                 dict(valid, album_id='share_owned'), dict(valid, album_id='share_12345678901'),
                 dict(valid, type='中文'), dict(valid, type='1' * 32),
                 dict(valid, album_id=None), dict(valid, album_id=1), dict(valid, album_id=[]),
                 dict(valid, type=''), dict(valid, type='1' * 33),
                 dict(valid, type=None), dict(valid, type=1), dict(valid, type=[])]
        for key in ('album_id', 'type'):
            for control in ('\x00', '\t', '\n', '\r', '\x7f', '\x85', '\u2028', '\u200b'):
                cases.append(dict(valid, **{key: valid[key] + control}))
        for parameters in cases:
            with self.subTest(parameters=parameters), \
                    patch.object(probe, '_certificate') as certificate, \
                    patch.object(probe.socket, 'socket') as factory, \
                    patch.object(probe, 'ssl', None), \
                    patch.object(probe.time, 'monotonic') as clock:
                with self.assertRaises(probe.MediaProbeError) as raised:
                    probe.query_first_media_frame(object(), 'shared_album', parameters=parameters)
                self.assertEqual(raised.exception.reason, 'invalid_parameters')
                certificate.assert_not_called()
                factory.assert_not_called()
                clock.assert_not_called()

    def test_non_shared_queries_reject_all_extra_parameters_before_certificate_and_network(self):
        for query_id in ('face', 'ocr', 'thumbnail', 'similar', 'ai', 'scraper', 'shared_album_catalog'):
            for parameters in ({}, {'album_id': 'share_owned', 'type': '1'}, {'action': 'erase'}, False):
                with self.subTest(query_id=query_id, parameters=parameters), \
                        patch.object(probe, '_certificate') as certificate, \
                        patch.object(probe.socket, 'socket') as factory:
                    with self.assertRaises(probe.MediaProbeError) as raised:
                        probe.query_first_media_frame(object(), query_id, parameters=parameters)
                    self.assertEqual(raised.exception.reason, 'invalid_parameters')
                    certificate.assert_not_called()
                    factory.assert_not_called()

    def test_fragmented_and_http_chunked_first_part(self):
        _, body = multipart()
        pieces = [body[index:index + 7] for index in range(0, len(body), 7)]
        chunked = b''.join(format(len(part), 'x').encode('ascii') + b'\r\n' + part + b'\r\n'
                           for part in pieces)
        header = (b'HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary=--myboundary\r\n'
                  b'Transfer-Encoding: chunked\r\n\r\n')
        complete = header + chunked
        fragments = [complete[index:index + 3] for index in range(0, len(complete), 3)]
        result, wire, _ = self.run_transport(fragments + [AssertionError('stream continued')])
        self.assertEqual(result, (200, FRAME))
        self.assertTrue(wire.chunks)

    def test_standard_boundary_and_quoted_boundary_are_also_bounded(self):
        for delimiter, advertised in [(b'----myboundary', '"--myboundary"'),
                                      (b'--myboundary', 'myboundary')]:
            with self.subTest(delimiter=delimiter):
                header, body = multipart(boundary=delimiter, declared_boundary=advertised)
                self.assertEqual(self.run_transport([header + body])[0], (200, FRAME))

    def test_peer_pin_mismatch_prevents_any_signed_request(self):
        wire = FakeSocket()
        with self.assertRaises(probe.MediaProbeError) as raised:
            self.run_transport([], cert=b'different-certificate', wire=wire)
        self.assertEqual(str(raised.exception), 'certificate_mismatch')
        self.assertEqual(wire.events, ['pin'])
        self.assertFalse(wire.sent)

    def test_proxy_environment_is_ignored_and_token_is_url_escaped(self):
        header, body = multipart()
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://private-proxy.invalid',
                                      'ALL_PROXY': 'http://private-proxy.invalid'}):
            _, wire, context = self.run_transport([header + body], token='private&action=erase\r\nInjected: x')
        self.assertIn(b'private%26action%3Derase%0D%0AInjected%3A%20x HTTP/1.1', wire.sent[0])
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, probe.ssl.CERT_NONE)

    def test_redirect_and_non200_do_not_read_error_body_or_retry(self):
        for status in (301, 302, 307, 401, 403, 500):
            with self.subTest(status=status):
                wire = FakeSocket([(f'HTTP/1.1 {status} Rejected\r\nLocation: https://private.invalid\r\n'
                                    'Content-Length: 100\r\n\r\n').encode('ascii'),
                                   AssertionError('must not read error body')])
                with self.assertRaises(probe.MediaProbeError) as raised:
                    self.run_transport([], wire=wire)
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(raised.exception.reason, 'redirect_refused' if status < 400 else 'http_error')
                self.assertEqual(wire.receives, 1)
                self.assertEqual(len(wire.sent), 1)
                self.assertNotIn('private', str(raised.exception))

    def test_invalid_json_duplicate_keys_nonfinite_depth_and_utf8_are_rejected(self):
        cases = [b'{"code":0,"code":1}', b'{"code":0,"x":{"a":1,"a":2}}',
                 b'{"code":0,"total":NaN}', b'{"code":0,"total":Infinity}',
                 b'{"code":0,"percent":1e999}', b'[]', b'{"code":0,}', b'\xff',
                 b'{"x":' * 20 + b'0' + b'}' * 20]
        for value in cases:
            with self.subTest(value=value):
                header, body = multipart(raw=value)
                with self.assertRaises(probe.MediaProbeError) as raised:
                    self.run_transport([header + body])
                self.assertEqual(raised.exception.reason, 'invalid_json')

    def test_plain_json_auth_failure_and_chunked_json(self):
        value = b'{"code":401,"msg":"private"}'
        plain = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(value)).encode() + b'\r\n\r\n'
        self.assertEqual(self.run_transport([plain + value])[0], (200, {'code': 401, 'msg': 'private'}))
        chunked = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n'
        chunks = [chunked] + [b'1\r\n' + bytes([char]) + b'\r\n' for char in value]
        self.assertEqual(self.run_transport(chunks)[0], (200, {'code': 401, 'msg': 'private'}))

    def test_header_body_transfer_and_multipart_size_limits(self):
        header, _ = multipart()
        cases = [
            (b'HTTP/1.1 200 OK\r\nX: ' + b'a' * probe.MAX_HEADERS, 'headers_too_large'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 65537\r\n\r\n', 'body_too_large'),
            (header + b'--myboundary\r\nContent-Type: application/json\r\nContent-Length: 65537\r\n\r\n', 'body_too_large'),
            (header + b'--myboundary\r\nContent-Type: application/json\r\n\r\n{}', 'invalid_multipart'),
            (header + b'--other\r\n', 'invalid_multipart'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n', 'unsupported_content_type'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n\r\n', 'unsupported_encoding'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nContent-Length: 10\r\n\r\n', 'invalid_http'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n10001\r\n', 'body_too_large'),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n', 'invalid_http'),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(probe.MediaProbeError) as raised:
                    self.run_transport([value])
                self.assertEqual(raised.exception.reason, reason)

    def test_incomplete_frame_is_never_projected_as_success(self):
        header, body = multipart()
        with self.assertRaises(probe.MediaProbeError) as raised:
            self.run_transport([header + body[:-5], b''])
        self.assertEqual(raised.exception.reason, 'incomplete_response')

    def test_socket_timeout_is_fixed_safe_error(self):
        with self.assertRaises(probe.MediaProbeError) as raised:
            self.run_transport([socket.timeout(SECRET)])
        self.assertEqual(str(raised.exception), 'timeout')

    def test_total_budget_is_recomputed_after_connect_before_request(self):
        wire = FakeSocket()
        with patch.object(probe.time, 'monotonic', side_effect=[0.0, 4.0, 8.0, 12.0]):
            with self.assertRaises(probe.MediaProbeError) as raised:
                self.run_transport([], wire=wire)
        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertFalse(wire.sent)

    def test_missing_host_ssl_is_an_unavailable_probe_not_import_failure(self):
        with patch.object(probe, 'ssl', None):
            with self.assertRaises(probe.MediaProbeError) as raised:
                probe.query_first_face_frame(SECRET)
        self.assertEqual(raised.exception.reason, 'tls_unavailable')


class CertificateTests(unittest.TestCase):
    def test_only_fixed_public_certificate_and_first_pem_leaf_are_read(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'tmp'
            target.mkdir()
            first = probe.ssl.DER_cert_to_PEM_cert(CERT)
            second = probe.ssl.DER_cert_to_PEM_cert(b'other-certificate')
            (target / 'Cert.pem').write_text(first + second, encoding='ascii')
            self.assertEqual(probe._certificate(folder), CERT)
            self.assertFalse((target / 'key.pem').exists())
            (target / 'Cert.pem').write_bytes(b'x' * (probe.MAX_CERT + 1))
            with self.assertRaises(probe.MediaProbeError) as raised:
                probe._certificate(folder)
            self.assertEqual(raised.exception.reason, 'certificate_invalid')
            (target / 'Cert.pem').write_bytes(b'not a certificate')
            with self.assertRaises(probe.MediaProbeError) as raised:
                probe._certificate(folder)
            self.assertEqual(raised.exception.reason, 'certificate_invalid')

    def test_missing_certificate_prevents_connection(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(probe.socket, 'socket') as factory:
            with self.assertRaises(probe.MediaProbeError) as raised:
                probe.query_first_face_frame(SECRET, root=folder)
            self.assertEqual(raised.exception.reason, 'certificate_unavailable')
            factory.assert_not_called()


@unittest.skipUnless(probe.ssl is not None, '宿主 Python 未提供 ssl，无法运行本机 TLS fixture。')
class RealLoopbackTLSTests(unittest.TestCase):
    def test_real_tls_public_pem_pin_and_first_frame_close_before_second_frame(self):
        """真实 SSL socket，第二帧由事件阻挡，首帧返回后立即验证连接关闭。"""
        first_sent = threading.Event()
        client_finished = threading.Event()
        server_finished = threading.Event()
        release_server = threading.Event()
        second_sent = threading.Event()
        peer_closed = threading.Event()
        observed, server_errors, outcome = [], [], {}
        timestamp = '1700000000'
        signed_token = (hashlib.md5(('owned-test-token' + timestamp).encode('ascii')).hexdigest()
                        + ':' + timestamp + ':100000')
        header, first_part = multipart()

        with tempfile.TemporaryDirectory(prefix='r1-media-owned-tls-') as folder:
            certificate = Path(folder) / 'tmp' / 'Cert.pem'
            certificate.parent.mkdir()
            certificate.write_text(_OWNED_CERT, encoding='ascii')
            # 只有测试服务端读取测试私钥；被测客户端只读固定 tmp/Cert.pem。
            server_key = Path(folder) / 'owned-test-server-key.pem'
            server_key.write_text(_OWNED_KEY, encoding='ascii')
            server_context = probe.ssl.SSLContext(probe.ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(certificate), str(server_key))

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(3)
                fixture_port = listener.getsockname()[1]

                def serve():
                    try:
                        raw, _ = listener.accept()
                        raw.settimeout(3)
                        with raw, server_context.wrap_socket(raw, server_side=True) as connection:
                            request = bytearray()
                            while b'\r\n\r\n' not in request:
                                chunk = connection.recv(2048)
                                if not chunk or len(request) + len(chunk) > probe.MAX_HEADERS:
                                    raise AssertionError('fixture expected one bounded signed request')
                                request.extend(chunk)
                            observed.append(bytes(request))
                            connection.sendall(header + first_part)
                            first_sent.set()
                            # 成功路径由主线程立即释放；没有等待原厂约八秒间隔。
                            if not release_server.wait(3):
                                second_sent.set()
                                connection.sendall(b'\r\n' + first_part)
                            if connection.recv(1) == b'':
                                peer_closed.set()
                    except Exception as error:
                        server_errors.append(error)
                    finally:
                        server_finished.set()

                def collect():
                    try:
                        outcome['value'] = probe.query_first_face_frame(signed_token, root=folder)
                    except Exception as error:
                        outcome['error'] = error
                    finally:
                        client_finished.set()

                server_thread = threading.Thread(target=serve, daemon=True)
                client_thread = threading.Thread(target=collect, daemon=True)
                server_thread.start()
                try:
                    # 仅测试连接的临时端口可变；默认固定目标仍由 TransportTests 验证。
                    with patch.object(probe, 'PORT', fixture_port):
                        client_thread.start()
                        self.assertTrue(client_finished.wait(2), 'first frame waited for a later stream frame')
                    self.assertTrue(first_sent.is_set())
                    self.assertFalse(second_sent.is_set())
                    self.assertNotIn('error', outcome)
                    self.assertEqual(outcome['value'], (200, FRAME))
                    self.assertEqual(len(observed), 1)
                    expected = ('GET /rest/1.1/fcabm?action=get_face_ai_status&access_token='
                                + probe.quote(signed_token, safe='') + ' HTTP/1.1\r\n').encode('ascii')
                    self.assertTrue(observed[0].startswith(expected))
                finally:
                    release_server.set()
                    self.assertTrue(server_finished.wait(3), 'owned fixture server did not close')
                    server_thread.join(timeout=1)
                    client_thread.join(timeout=1)
                self.assertFalse(server_errors)
                self.assertTrue(peer_closed.is_set(), 'client retained the native stream after its first frame')
                self.assertFalse(second_sent.is_set())


class ProjectionTests(unittest.TestCase):
    def collect(self, frame=FRAME, status=200, credential=None):
        reader = Mock(return_value=(Session(), credential or SAFE_CREDENTIAL))
        transport = Mock(return_value=(status, frame))
        value = probe.collect_media_probe('/fixture', session_reader=reader, transport=transport)
        reader.assert_called_once_with(root='/fixture')
        transport.assert_called_once_with(SECRET, root='/fixture')
        return value

    def test_success_projects_only_verified_fields_and_keeps_side_effect_semantics(self):
        value = self.collect()
        self.assertTrue(value['available'])
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['scope'], {'user_id': 100000})
        self.assertTrue(value['credential_read_only'])
        self.assertTrue(value['native_query_may_update_state'])
        self.assertNotIn('read_only', value)
        self.assertEqual(value['native'], {'action': 'get_face_ai_status', 'http_status': 200,
                                           'code': 0, 'first_frame_only': True})
        self.assertEqual(value['data']['picture_completion_ratio'], 0.5)
        self.assertEqual(value['data']['video_completion_ratio'], 0.5)
        self.assertEqual(value['data']['sync_percent'], 100)
        self.assertEqual(value['data']['estimated_remaining'], {'value': 6, 'unit': None})
        public = json.dumps(value, allow_nan=False)
        for secret in (SECRET, '/private/photos', 'private-user', 'base_token', 'login_time', 'source_id'):
            self.assertNotIn(secret, public)

    def test_native_auth_denied_preserves_code_without_message_or_any_retry(self):
        value = self.collect({'code': -401, 'msg': SECRET, 'total': 20})
        self.assertFalse(value['available'])
        self.assertEqual(value['status'], 'native_error')
        self.assertEqual(value['native']['code'], -401)
        self.assertIsNone(value['data'])
        self.assertNotIn(SECRET, json.dumps(value))

    def test_invalid_counters_and_status_remain_unknown(self):
        invalid = (None, True, False, -1, 1.2, '2', [], {}, 2 ** 53, float('inf'), float('nan'))
        for counter in invalid:
            with self.subTest(counter=counter):
                frame = dict(FRAME, **{key: counter for key in probe._COUNTERS})
                frame.update(left=counter, percent=counter, status=counter, range_status=counter)
                value = self.collect(frame)
                self.assertEqual(value['status'], 'partial')
                self.assertEqual(value['reason'], 'counts_unavailable')
                for key in probe._COUNTERS:
                    self.assertIsNone(value['data'][key])
                self.assertIsNone(value['data']['picture_completion_ratio'])
                self.assertIsNone(value['data']['video_completion_ratio'])
                self.assertIsNone(value['data']['estimated_remaining']['value'])
                json.dumps(value, allow_nan=False)

    def test_zero_totals_and_overflowing_processed_do_not_imply_completion(self):
        for total, processed in ((0, 0), (0, 1), (2, 3)):
            frame = dict(FRAME, totalpic=total, processedpic=processed, totalvid=total, processedvid=processed)
            value = self.collect(frame)
            self.assertIsNone(value['data']['picture_completion_ratio'])
            self.assertIsNone(value['data']['video_completion_ratio'])
            self.assertNotIn('completed', value['data'])

    def test_percent_status_and_range_are_not_invented_task_states(self):
        frame = dict(FRAME, percent=101, status=2, range_status=1)
        value = self.collect(frame)
        self.assertIsNone(value['data']['sync_percent'])
        self.assertIsNone(value['data']['upgrade_status'])
        self.assertIsNone(value['data']['range_status'])
        self.assertEqual(value['data']['picture_completion_ratio'], 0.5)

    def test_missing_session_wrong_owner_and_reader_failure_never_query(self):
        wrong = Session()
        wrong.user_id = 100001
        cases = [Mock(return_value=(None, {'status': 'db_missing', 'available': False})),
                 Mock(return_value=(wrong, SAFE_CREDENTIAL)), Mock(side_effect=RuntimeError(SECRET)),
                 Mock(return_value=(Session(), {'status': 'no_session', 'available': False})),
                 Mock(return_value=(Session(), dict(SAFE_CREDENTIAL, user_id=None)))]
        for reader in cases:
            with self.subTest(reader=reader):
                transport = Mock()
                value = probe.collect_media_probe(session_reader=reader, transport=transport)
                transport.assert_not_called()
                self.assertFalse(value['available'])
                self.assertIsNone(value['native']['http_status'])
                self.assertNotIn(SECRET, json.dumps(value))

    def test_credential_and_transport_exception_text_never_enter_public_result(self):
        credential = dict(SAFE_CREDENTIAL, reason=SECRET, token=SECRET)
        value = self.collect(credential=credential)
        self.assertNotIn(SECRET, json.dumps(value))
        transport = Mock()
        value = probe.collect_media_probe(
            session_reader=lambda **_: (Session(), dict(credential, status=SECRET)), transport=transport)
        transport.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(value))
        for error in (RuntimeError(SECRET), probe.MediaProbeError('http_error', 401)):
            transport = Mock(side_effect=error)
            value = probe.collect_media_probe(session_reader=lambda **_: (Session(), SAFE_CREDENTIAL),
                                              transport=transport)
            self.assertFalse(value['available'])
            self.assertNotIn(SECRET, json.dumps(value))
            self.assertIsNone(value['data'])
            transport.assert_called_once()

    def test_missing_invalid_native_code_and_non200_are_not_accepted(self):
        for frame in ({}, {'code': True}, {'code': '0'}, {'code': float('nan')}):
            value = self.collect(frame)
            self.assertEqual(value['status'], 'invalid_response')
            self.assertIsNone(value['data'])
            json.dumps(value, allow_nan=False)
        value = self.collect(status=302)
        self.assertFalse(value['available'])
        self.assertEqual(value['reason'], 'redirect_refused')
        self.assertEqual(value['native']['http_status'], 302)


if __name__ == '__main__':
    unittest.main()
