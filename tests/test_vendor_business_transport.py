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

    def request(self, operation='albums_list', parameters=None, body=None, confirmed=False,
                observer=None):
        return self.client().request(operation, parameters, body,
                                     self.authorizer, confirmed=confirmed, observer=observer)

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

    def test_getter_trace_runs_all_real_identity_checks_with_one_deadline(self):
        observed = {}
        client = self.client()
        original = client._listener

        def checked(**kwargs):
            self.assertEqual(kwargs['deadline'], 60.0)
            kwargs['observer'].update(schema=1, budget={'scan_limit_ms': 1000},
                scan={'status': 'passed', 'complete': True, 'elapsed_ms': 2,
                      'counters': {'seen': 3}, 'reason_codes': []},
                listener={'status': 'verified', 'port': 20443, 'listener_count': 1,
                          'owner_count': 1, 'matching_owner_count': 1, 'owner_matches': True,
                          'identity': IDENTITY,
                          'final': {'status': 'passed', 'executable_path_matches': True,
                                    'generation_matches': True}},
                tables={'initial': 'passed', 'final': 'passed'},
                processes=[{'pid': 12, 'start_ticks': 88, 'executable_name_matches': True,
                    'final_start_ticks': 88,
                    'after_scan_identity': 'passed', 'final_identity': 'passed',
                    'final_fd': 'passed'}],
                listener_snapshots={'initial': [{'socket_inode': '555',
                    'address': '0.0.0.0', 'owner_count': 1}],
                    'final': [{'socket_inode': '555', 'address': '0.0.0.0', 'owner_count': None}]})
            return original(**kwargs)

        client._listener = checked
        self.assertEqual(client.request('video_capacity', {}, {}, self.authorizer,
                                        observer=observed), (200, {'code': 0}))
        self.assertEqual(observed['schema'], 1)
        for flag in business._TRACE_FLAGS:
            self.assertTrue(observed[flag], flag)
        self.assertIsNone(observed['discarded_at'])
        self.assertIsNone(observed['error_reason'])
        for index, phase in enumerate(business._LISTENER_PHASES):
            item = observed['listener_checks'][phase]
            self.assertEqual(item['status'], 'passed')
            self.assertEqual(item['identity'], {'pid': 12, 'start_ticks': 88, 'socket_inode': '555'})
            self.assertEqual(item['expected_matches'], None if index == 0 else True)
            self.assertTrue(item['runtime']['scan']['complete'])
            self.assertEqual(item['runtime']['processes'][0]['final_fd'], 'passed')
            self.assertEqual(item['runtime']['processes'][0]['final_start_ticks'], 88)
            self.assertEqual(item['runtime']['listener_snapshots']['initial'][0]['owner_count'], 1)
            self.assertIsNone(item['runtime']['listener_snapshots']['final'][0]['owner_count'])
        self.assertNotIn(TOKEN, json.dumps(observed))

    def test_each_listener_rejection_has_its_real_phase_and_dependency_not_run(self):
        for failure_index, phase in enumerate(business._LISTENER_PHASES):
            with self.subTest(phase=phase):
                self.setUp()
                observed = {}
                self.identity_values = [IDENTITY] * failure_index + [dict(IDENTITY, available=False)]
                error = self.assert_error('identity_unverified', 'video_capacity', observer=observed)
                self.assertEqual(observed['discarded_at'], phase)
                self.assertEqual(observed['error_reason'], 'identity_unverified')
                for index, name in enumerate(business._LISTENER_PHASES):
                    self.assertEqual(observed['listener_checks'][name]['status'],
                        'passed' if index < failure_index else 'failed' if index == failure_index else 'not_run')
                self.assertEqual(observed['request_sent'], failure_index == 3)
                self.assertEqual(error.request_sent, failure_index == 3)
                self.assertEqual(observed['response_received'], failure_index == 3)
                self.assertEqual(len(self.connection.sent), 1 if failure_index == 3 else 0)

    def test_each_identity_competition_compares_current_generation_and_socket(self):
        for index, changed in ((1, {'start_ticks': 89}), (2, {'pid': 13}),
                               (3, {'socket_inode': '556'})):
            with self.subTest(index=index):
                self.setUp()
                observed = {}
                self.identity_values = [IDENTITY] * index + [dict(IDENTITY, **changed)]
                self.assert_error('identity_changed', 'video_capacity', observer=observed)
                phase = business._LISTENER_PHASES[index]
                item = observed['listener_checks'][phase]
                self.assertEqual(item['status'], 'failed')
                self.assertFalse(item['expected_matches'])
                self.assertEqual(item['identity'], {key: changed.get(key, IDENTITY[key])
                    for key in ('pid', 'start_ticks', 'socket_inode')})
                self.assertEqual(observed['discarded_at'], phase)

    def test_partial_send_trace_is_uncertain_and_never_retries(self):
        observed = {}
        self.connection.send_error = OSError(TOKEN)
        self.assert_error('transport_failed', 'transcode_switch', observer=observed)
        self.assertTrue(observed['send_attempted'])
        self.assertTrue(observed['request_sent'])
        self.assertFalse(observed['send_completed'])
        self.assertFalse(observed['response_started'])
        self.assertFalse(observed['response_received'])
        self.assertEqual(observed['discarded_at'], 'send')
        self.assertEqual(len(self.connection.sent), 1)
        self.assertEqual(observed['listener_checks']['after_response']['status'], 'not_run')
        self.assertNotIn(TOKEN, json.dumps(observed))

    def test_bad_received_frame_is_distinct_from_no_response(self):
        observed = {}
        self.connection = Connection(response({'code': 0})[:-1])
        self.assert_error('incomplete_response', 'video_capacity', observer=observed)
        self.assertTrue(observed['send_completed'])
        self.assertTrue(observed['response_started'])
        self.assertFalse(observed['response_received'])
        self.assertEqual(observed['discarded_at'], 'response')
        self.assertEqual(observed['listener_checks']['after_response']['status'], 'not_run')

    def test_response_identity_failure_records_complete_but_discarded_frame(self):
        observed = {}
        self.identity_values = [IDENTITY, IDENTITY, IDENTITY, dict(IDENTITY, available=False)]
        self.assert_error('identity_unverified', 'offline_video_path',
            {'video_capability': 1, 'path': '/drives/disk/Users/admin/PRIVATE-MEDIA',
             'offset': 0, 'sharpness': 'raw', 'terminal': 'LOCAL'}, observer=observed)
        self.assertTrue(observed['response_started'])
        self.assertTrue(observed['response_received'])
        self.assertEqual(observed['discarded_at'], 'after_response')
        self.assertNotIn('PRIVATE-MEDIA', json.dumps(observed))

    def test_revocation_during_final_listener_check_discards_received_frame(self):
        observed = {}
        client = self.client()
        original = client._listener
        active, count = [True], [0]

        def revoke(**kwargs):
            count[0] += 1
            value = original(**kwargs)
            if count[0] == 4:
                active[0] = False
            return value

        client._listener = revoke
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('video_capacity', {}, {}, lambda *a: active[0], observer=observed)
        self.assertEqual(raised.exception.reason, 'authorization_denied')
        self.assertTrue(raised.exception.request_sent)
        self.assertTrue(observed['response_received'])
        self.assertEqual(observed['discarded_at'], 'authorization')
        self.assertEqual(observed['listener_checks']['after_response']['status'], 'passed')

    def test_trace_timeout_retains_passed_observation_but_never_sends(self):
        observed = {}
        client = self.client()
        original = client._listener

        def slow(**kwargs):
            value = original(**kwargs)
            self.now += business.TIMEOUT + 1
            return value

        client._listener = slow
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('video_capacity', {}, {}, self.authorizer, observer=observed)
        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertEqual(observed['listener_checks']['initial']['status'], 'passed')
        self.assertEqual(observed['listener_checks']['initial']['elapsed_ms'], 11000)
        self.assertFalse(observed['request_sent'])
        self.assertEqual(observed['listener_checks']['connected']['status'], 'not_run')
        self.assertFalse(self.connection.sent)

    def test_other_business_operations_do_not_opt_into_observer_contract(self):
        observed = {'sentinel': TOKEN}
        self.request(observer=observed)
        self.assertEqual(observed, {'sentinel': TOKEN})
        self.assertTrue(all(set(kwargs) == {'root', 'port'}
            for event, kwargs in self.events if event == 'identity'))

    def test_fixed_preflight_never_reads_credentials_certificate_or_network(self):
        observed = {}
        client = self.client()
        for attribute in ('_session', '_certificate', '_socket'):
            setattr(client, attribute, mock.Mock(side_effect=AssertionError(TOKEN)))
        authorizer = mock.Mock(return_value=True)
        with mock.patch.object(business, 'ssl', None):
            result = client.probe_listener(authorizer, observer=observed)
        self.assertEqual(result, IDENTITY)
        authorizer.assert_called_with('probe_listener', {}, {})
        self.assertEqual(len([event for event in self.events if event[0] == 'identity']), 1)
        self.assertTrue(observed['request_started'])
        self.assertFalse(any(observed[flag] for flag in business._TRACE_FLAGS if flag != 'request_started'))
        self.assertEqual(observed['listener_checks']['initial']['status'], 'passed')
        self.assertTrue(all(observed['listener_checks'][phase]['status'] == 'not_run'
                            for phase in business._LISTENER_PHASES[1:]))
        for attribute in ('_session', '_certificate', '_socket'):
            getattr(client, attribute).assert_not_called()

    def test_fixed_preflight_uses_same_lock_and_absolute_deadline(self):
        client = self.client()
        observed = {}
        lock = mock.Mock()
        lock.acquire.return_value = False
        with mock.patch.object(business, '_LOCK', lock):
            with self.assertRaises(business.BusinessTransportError) as raised:
                client.probe_listener(self.authorizer, observer=observed)
        self.assertEqual(raised.exception.reason, 'busy')
        lock.acquire.assert_called_once_with(timeout=10.0)
        lock.release.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertEqual(observed['discarded_at'], 'lock')

    def test_preflight_rechecks_authorization_after_scan_and_stops_on_timeout(self):
        for failure in ('authorization', 'timeout'):
            with self.subTest(failure=failure):
                self.setUp()
                client = self.client()
                observed = {}
                active = [True]
                original = client._listener

                def changed(**kwargs):
                    value = original(**kwargs)
                    if failure == 'authorization':
                        active[0] = False
                    else:
                        self.now += business.TIMEOUT + 1
                    return value

                client._listener = changed
                with self.assertRaises(business.BusinessTransportError) as raised:
                    client.probe_listener(lambda *a: active[0], observer=observed)
                self.assertEqual(raised.exception.reason,
                    'authorization_denied' if failure == 'authorization' else 'timeout')
                self.assertFalse(raised.exception.request_sent)
                self.assertFalse(self.connection.sent)
                self.assertEqual(observed['listener_checks']['initial']['status'], 'passed')
                self.assertEqual(observed['discarded_at'],
                    'authorization' if failure == 'authorization' else 'deadline')

    def test_public_trace_deep_whitelist_rejects_sensitive_or_malformed_hook_fields(self):
        private = 'PRIVATE-PATH-TOKEN-ERROR-SENTINEL'
        value = {'schema': 1, 'secret': private, 'discarded_at': {}, 'error_reason': [private],
            'request_started': 1, 'listener_checks': {'initial': {'status': private,
                'reason_code': {private: private}, 'elapsed_ms': True,
                'identity': {'pid': 1, 'start_ticks': 2, 'socket_inode': '3', 'path': private},
                'runtime': {'schema': 1, 'secret': private,
                    'budget': {'scan_limit_ms': True, 'max_processes': 4096, 'path': private},
                    'scan': {'status': private, 'reason_codes': [private, 'fd_limit', {}],
                             'counters': {'seen': 1, 'path': private}},
                    'listener': {'status': private, 'port': private, 'path': private},
                    'processes': [{'pid': 1, 'start_ticks': 2, 'argv': private,
                                   'final_fd': private}]}}}}
        projected = business.public_trace(value)
        self.assertNotIn(private, json.dumps(projected))
        self.assertFalse(projected['request_started'])
        self.assertEqual(projected['listener_checks']['initial']['status'], 'not_run')
        self.assertIsNone(projected['listener_checks']['initial']['elapsed_ms'])
        self.assertEqual(projected['listener_checks']['initial']['runtime']['scan']['reason_codes'], ['fd_limit'])
        self.assertEqual(projected['listener_checks']['initial']['runtime']['listener']['port'], 20443)
        self.assertEqual(business.public_trace({})['listener_checks']['initial']['status'], 'not_run')

    def test_listener_exception_does_not_echo_text_or_trigger_later_dependencies(self):
        observed = {}
        client = self.client()
        client._listener = mock.Mock(side_effect=OSError(TOKEN))
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('video_capacity', {}, {}, self.authorizer, observer=observed)
        self.assertEqual(raised.exception.reason, 'transport_failed')
        self.assertFalse(raised.exception.request_sent)
        self.assertEqual(observed['listener_checks']['initial']['reason_code'], 'transport_failed')
        self.assertEqual(observed['listener_checks']['connected']['status'], 'not_run')
        self.assertFalse(any(event[0] in ('credential', 'certificate') for event in self.events))
        self.assertNotIn(TOKEN, json.dumps(observed))

    def test_failed_production_scan_preserves_safe_reason_and_real_budget_counters(self):
        observed = {}
        client = self.client()

        def partial(**kwargs):
            kwargs['observer'].update(schema=1,
                budget={'scan_limit_ms': 1000, 'external_remaining_ms': 3000,
                    'effective_limit_ms': 1000, 'max_processes': 4096,
                    'max_fds': 32768, 'max_bytes': 1048576},
                scan={'status': 'failed', 'complete': False, 'elapsed_ms': 1000,
                    'reason_codes': ['attribute_permission_denied', 'deadline', TOKEN],
                    'counters': {'seen': 412, 'read_failed': 2, 'identity_invalid': 1,
                                 'deadline': 1, 'fd_seen': 2364}},
                listener={'status': 'proc_partial', 'listener_count': 1,
                    'owner_count': 2, 'matching_owner_count': 1, 'owner_matches': False},
                listener_snapshots={'initial': [{'socket_inode': '555',
                    'address': '0.0.0.0', 'owner_count': 2}], 'final': []})
            return {'available': False, 'status': 'proc_partial'}

        client._listener = partial
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('video_capacity', {}, {}, self.authorizer, observer=observed)
        self.assertEqual(raised.exception.reason, 'identity_unverified')
        runtime = observed['listener_checks']['initial']['runtime']
        self.assertEqual(runtime['scan']['reason_codes'], ['attribute_permission_denied', 'deadline'])
        self.assertEqual(runtime['scan']['counters']['identity_invalid'], 1)
        self.assertEqual(runtime['scan']['counters']['seen'], 412)
        self.assertEqual(runtime['budget']['effective_limit_ms'], 1000)
        self.assertEqual(runtime['listener']['owner_count'], 2)
        self.assertEqual(runtime['listener']['status'], 'proc_partial')
        self.assertEqual(runtime['listener_snapshots']['initial'][0]['owner_count'], 2)
        self.assertFalse(self.events)
        self.assertFalse(observed['request_sent'])
        self.assertNotIn(TOKEN, json.dumps(observed))

    def test_trace_snapshots_are_bounded_detached_and_do_not_expose_other_addresses(self):
        source = {'schema': 1, 'listener_checks': {'initial': {'runtime': {'schema': 1,
            'listener_snapshots': {'initial': [{'socket_inode': str(index + 1),
                'address': '127.0.0.1', 'owner_count': 1, 'secret': TOKEN}
                for index in range(20)],
                'final': [{'socket_inode': '1', 'address': '192.0.2.1', 'owner_count': 1}]},
            'processes': [{'pid': 12, 'start_ticks': 88, 'final_start_ticks': 89,
                          'final_identity': 'failed', 'argv': TOKEN}]}}}}
        result = business.public_trace(source)
        runtime = result['listener_checks']['initial']['runtime']
        self.assertEqual(len(runtime['listener_snapshots']['initial']), 16)
        self.assertEqual(runtime['listener_snapshots']['final'], [])
        self.assertEqual(runtime['processes'][0]['start_ticks'], 88)
        self.assertEqual(runtime['processes'][0]['final_start_ticks'], 89)
        self.assertEqual(runtime['processes'][0]['final_identity'], 'failed')
        source['listener_checks']['initial']['runtime']['processes'][0]['start_ticks'] = 99
        self.assertEqual(runtime['processes'][0]['start_ticks'], 88)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_validation_or_authorization_rejection_clears_stale_trace_without_collecting(self):
        for parameters, authorize in (({'unexpected': TOKEN}, lambda *a: True),
                                      ({}, lambda *a: False)):
            with self.subTest(parameters=bool(parameters)):
                self.setUp()
                observed = {'secret': TOKEN, 'request_sent': True}
                with self.assertRaises(business.BusinessTransportError) as raised:
                    self.client().request('video_capacity', parameters, {}, authorize,
                                          observer=observed)
                self.assertIn(raised.exception.reason, ('invalid_parameters', 'authorization_denied'))
                self.assertFalse(observed['request_started'])
                self.assertFalse(observed['request_sent'])
                self.assertEqual(observed['listener_checks']['initial']['status'], 'not_run')
                self.assertFalse(self.events)
                self.assertNotIn(TOKEN, json.dumps(observed))

    def test_listener_timeout_has_matching_phase_reason_and_no_later_dependencies(self):
        observed = {}
        client = self.client()
        client._listener = mock.Mock(side_effect=TimeoutError(TOKEN))
        with self.assertRaises(business.BusinessTransportError) as raised:
            client.request('video_capacity', {}, {}, self.authorizer, observer=observed)
        self.assertEqual(raised.exception.reason, 'timeout')
        self.assertEqual(observed['listener_checks']['initial']['reason_code'], 'timeout')
        self.assertEqual(observed['discarded_at'], 'initial')
        self.assertFalse(self.connection.sent)
        self.assertFalse(self.events)


if __name__ == '__main__':
    unittest.main()
