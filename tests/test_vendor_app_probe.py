"""Fixed application GET, existing owner auth, redaction and native failures."""

from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_app_probe as probe
from test_vendor_media_probe import CERT, FakeSocket, SAFE_CREDENTIAL, SECRET, Session


FRAME = {'list': [{'module_name': '相册', 'version': '1.0', 'status': 0,
                    'apply_id': 1234, 'vid': SECRET, 'path': '/private/apps',
                    'url': 'https://private.invalid/' + SECRET}],
         'count': 1, 'support_ecloud': 1, 'msg': SECRET, 'access_token': SECRET}


class VendorAppProbeTests(unittest.TestCase):
    def collect(self, *, frame=None, status=200, credential=None, session=None):
        reader = mock.Mock(return_value=(session or Session(), credential or SAFE_CREDENTIAL))
        transport = mock.Mock(return_value=(status, FRAME if frame is None else frame))
        result = probe.collect_application_probe('/fixture-root', session_reader=reader, transport=transport)
        return result, reader, transport

    def test_audited_list_needs_no_invented_code_and_projects_no_credentials_or_paths(self):
        result, reader, transport = self.collect()
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['attempted'])
        self.assertEqual(result['native']['action'], 'get_apply_list')
        self.assertEqual(result['native']['opttype'], 0)
        self.assertFalse(result['native']['code_present'])
        self.assertIsNone(result['native']['code'])
        self.assertEqual(result['data']['apps'], [{'name': '相册', 'version': '1.0',
                                                  'installation_status_code': 0, 'running': None}])
        self.assertFalse(result['data']['live'])
        self.assertFalse(result['data']['complete'])
        self.assertTrue(result['native_query_may_update_state'])
        reader.assert_called_once_with(root='/fixture-root')
        transport.assert_called_once_with(SECRET, root='/fixture-root')
        public = json.dumps(result, ensure_ascii=False)
        for private in (SECRET, '/private/', 'private.invalid', 'apply_id', 'source_id'):
            self.assertNotIn(private, public)

    def test_valid_zero_row_list_is_observed_not_a_fabricated_missing_fallback(self):
        result, _, transport = self.collect(frame={'list': [], 'count': 0})
        self.assertTrue(result['available'])
        self.assertEqual(result['data']['apps'], [])
        transport.assert_called_once()

    def test_one_invalid_catalog_label_does_not_fail_valid_entries_or_relax_label_validation(self):
        frame = {'list': [{'module_name': '新插件 β', 'version': '2.0', 'status': 2},
                          {'module_name': ''}, {'module_name': '/private/' + SECRET},
                          {'module_name': 'bad\nlabel'}, None], 'token': SECRET}
        result, _, transport = self.collect(frame=frame)
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['data']['apps'], [{'name': '新插件 β', 'version': '2.0',
            'installation_status_code': None, 'running': None}])
        self.assertEqual(result['data']['rejected_entries'], 4)
        self.assertEqual(result['data']['rejections'], [
            {'code': 'unsupported_application_label', 'count': 3},
            {'code': 'unsupported_application_entry', 'count': 1}])
        self.assertTrue(result['data']['partial'])
        self.assertFalse(result['data']['complete'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('bad\nlabel', json.dumps(result))
        transport.assert_called_once()

    def test_all_invalid_labels_still_fail_instead_of_a_fake_zero_entry_success(self):
        result, _, transport = self.collect(frame={'list': [{'module_name': ''}, {'module_name': '/private/' + SECRET}]})
        self.assertFalse(result['available'])
        self.assertEqual(result['error_code'], 'unsupported_application_label')
        self.assertIsNone(result['data'])
        self.assertNotIn(SECRET, json.dumps(result))
        transport.assert_called_once()

    def test_auth_unavailable_wrong_owner_and_reader_error_never_invoke_transport(self):
        for session, credential in ((None, {'available': False, 'status': 'no_session'}),
                                    (mock.Mock(user_id=123), SAFE_CREDENTIAL),
                                    (Session(), {**SAFE_CREDENTIAL, 'user_id': 123}),
                                    (Session(), {**SAFE_CREDENTIAL, 'available': False})):
            with self.subTest(credential=credential):
                reader = mock.Mock(return_value=(session, credential))
                transport = mock.Mock()
                result = probe.collect_application_probe(session_reader=reader, transport=transport)
                self.assertFalse(result['available'])
                self.assertEqual(result['error_code'], 'credential_unavailable')
                self.assertFalse(result['attempted'])
                self.assertIsNone(result['data'])
                transport.assert_not_called()
        transport = mock.Mock()
        result = probe.collect_application_probe(session_reader=mock.Mock(side_effect=OSError(SECRET)),
                                                 transport=transport)
        self.assertEqual(result['error_code'], 'credential_read_failed')
        self.assertNotIn(SECRET, str(result))
        transport.assert_not_called()

    def test_bad_token_and_signing_exception_are_not_retried(self):
        for value in (None, '', False, 'x' * 4097):
            session = mock.Mock(user_id=100000)
            session.derive_access_token.return_value = value
            result, _, transport = self.collect(session=session)
            self.assertEqual(result['error_code'], 'token_unavailable')
            session.derive_access_token.assert_called_once_with()
            transport.assert_not_called()

    def test_http_native_code_and_schema_failures_never_expose_error_body(self):
        for status, frame, error in ((302, FRAME, 'redirect_refused'),
                                     (401, {'msg': SECRET}, 'http_error'),
                                     (200, {'code': 99, 'list': [], 'msg': SECRET}, 'native_request_rejected'),
                                     (200, {'code': False, 'list': []}, 'native_code_unavailable'),
                                     (200, {'code': '0', 'list': []}, 'native_code_unavailable'),
                                     (200, {'list': None, 'msg': SECRET}, 'unsupported_application_list'),
                                     (200, {'list': [None]}, 'unsupported_application_entry'),
                                     (200, {'list': [{'module_name': '/private/' + SECRET}]},
                                      'unsupported_application_label'),
                                     (True, FRAME, 'invalid_transport_result')):
            with self.subTest(error=error, status=status):
                result, _, transport = self.collect(frame=frame, status=status)
                self.assertFalse(result['available'])
                self.assertIsNone(result['data'])
                self.assertEqual(result['error_code'], error)
                self.assertNotIn(SECRET, str(result))
                transport.assert_called_once()
        result, _, _ = self.collect(frame={'code': 0, 'list': []})
        self.assertTrue(result['available'])

    def test_transport_failure_records_fixed_code_only_and_calls_once(self):
        for failure, expected in ((probe.wire.MediaProbeError('timeout'), 'timeout'),
                                   (OSError('/private/' + SECRET), 'transport_failed')):
            reader = mock.Mock(return_value=(Session(), SAFE_CREDENTIAL))
            transport = mock.Mock(side_effect=failure)
            result = probe.collect_application_probe(session_reader=reader, transport=transport)
            self.assertEqual(result['error_code'], expected)
            self.assertIsNone(result['data'])
            self.assertNotIn(SECRET, str(result))
            transport.assert_called_once()

    def run_wire(self, chunks, *, pin=CERT):
        raw, connection = FakeSocket(), FakeSocket(chunks)
        context = mock.Mock()
        context.wrap_socket.return_value = connection
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(probe.wire, '_certificate', return_value=pin))
            stack.enter_context(mock.patch.object(probe.wire.ssl, 'SSLContext', return_value=context))
            factory = stack.enter_context(mock.patch.object(probe.socket, 'socket', return_value=raw))
            stack.enter_context(mock.patch.object(probe.socket, 'getaddrinfo', side_effect=AssertionError('no DNS')))
            try:
                result = probe.query_application_frame(SECRET, root='/fixture-root')
            finally:
                self.assertTrue(raw.closed)
                self.assertTrue(connection.closed)
                self.assertEqual(raw.targets, [('127.0.0.1', 20443)])
                factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
                context.wrap_socket.assert_called_once_with(raw, server_hostname=None)
                self.connection = connection
        return result

    def test_wire_is_exact_opttype_zero_get_with_pin_before_signature_and_bounded_single_frame(self):
        body = json.dumps(FRAME).encode()
        header = ('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                  + str(len(body)) + '\r\n\r\n').encode()
        result = self.run_wire([header, body, AssertionError('no extra response')])
        self.assertEqual(result, (200, FRAME))
        self.assertEqual(self.connection.events, ['pin', 'request'])
        request = self.connection.sent[0]
        self.assertEqual(request.split(b'\r\n')[0],
                         b'GET /rest/1.1/applycenter?action=get_apply_list&opttype=0&access_token='
                         + SECRET.encode() + b' HTTP/1.1')
        self.assertEqual(request.split(b'\r\n\r\n')[1], b'')
        for forbidden in (b'opttype=1', b'get_install_progress', b'install_apply', b'uid=', b'Authorization:'):
            self.assertNotIn(forbidden, request)
        self.assertEqual(len(self.connection.sent), 1)
        self.assertEqual(len(self.connection.chunks), 1)

    def test_certificate_mismatch_sends_nothing(self):
        with self.assertRaises(probe.wire.MediaProbeError) as raised:
            self.run_wire([], pin=b'wrong-certificate')
        self.assertEqual(raised.exception.reason, 'certificate_mismatch')
        self.assertEqual(self.connection.sent, [])

    def test_redirect_and_error_bodies_are_not_read_or_retried(self):
        for status, reason in ((302, 'redirect_refused'), (401, 'http_error')):
            with self.subTest(status=status):
                with self.assertRaises(probe.wire.MediaProbeError) as raised:
                    self.run_wire([f'HTTP/1.1 {status} Error\r\nContent-Length: 999\r\n\r\n'.encode(),
                                   AssertionError('error body must not be read')])
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(len(self.connection.chunks), 1)

    def test_invalid_json_bounds_duplicate_keys_and_unsupported_encoding_fail(self):
        for body, header, error in ((b'{"list":[],"list":[]}', '', 'invalid_json'),
                                    (b'{"list":[],"n":NaN}', '', 'invalid_json'),
                                    (b'not-json', '', 'invalid_json'),
                                    (b'{"list":[]}', 'Content-Encoding: gzip\r\n', 'unsupported_encoding')):
            with self.subTest(error=error):
                response = ('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                            + header + 'Content-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body
                with self.assertRaises(probe.wire.MediaProbeError) as raised:
                    self.run_wire([response])
                self.assertEqual(raised.exception.reason, error)


if __name__ == '__main__':
    unittest.main()
