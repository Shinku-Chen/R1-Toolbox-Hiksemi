"""The explicit identity-only view must never replay a vendor media getter."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_business_transport as transport
import vendor_transcode_probe as probe

SECRET = 'private-listener-sentinel-not-public'
IDENTITY = {'available': True, 'status': 'verified', 'pid': 7506,
            'start_ticks': 6298, 'socket_inode': '123456', 'connect_host': '127.0.0.1'}


class ListenerPreflightTests(unittest.TestCase):
    def client(self, listener):
        def forbidden(*args, **kwargs):
            self.fail('identity-only preflight accessed credentials/certificate/network')
        return transport.VendorBusinessClient(
            listener_check=listener, session_reader=forbidden,
            certificate_reader=forbidden, socket_factory=forbidden,
            tls_context=mock.Mock(wrap_socket=forbidden))

    def test_success_runs_only_the_production_listener_gate(self):
        listener = mock.Mock(return_value=dict(IDENTITY))
        seen = []
        result = probe.collect_listener_preflight(
            client=self.client(listener),
            authorizer=lambda *args: seen.append(args) or True)
        self.assertEqual(listener.call_count, 1)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(result['available'])
        self.assertTrue(result['proc_metadata_only'])
        for key in ('native_request_performed', 'credentials_read', 'certificate_read', 'media_accessed'):
            self.assertIs(result[key], False)
        phases = result['diagnostics']['listener_checks']
        self.assertEqual(phases['initial']['status'], 'passed')
        for key in ('connected', 'before_send', 'after_response'):
            self.assertEqual(phases[key]['status'], 'not_run')
        self.assertFalse(result['diagnostics']['request_sent'])
        self.assertTrue(all(args == ('probe_listener', {}, {}) for args in seen))

    def test_unverified_scan_is_visible_and_never_enters_network(self):
        result = probe.collect_listener_preflight(
            client=self.client(lambda **kw: {'available': False, 'status': 'proc_partial'}),
            authorizer=lambda *args: True)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'identity_unverified')
        self.assertEqual(result['diagnostics']['listener_checks']['initial']['status'], 'failed')
        self.assertFalse(result['diagnostics']['send_attempted'])
        self.assertFalse(result['diagnostics']['response_received'])

    def test_denied_or_missing_authorizer_never_reads_proc(self):
        for authorizer in (None, lambda *args: False, lambda *args: 1):
            listener = mock.Mock(side_effect=AssertionError('must not sample'))
            value = probe.collect_listener_preflight(client=self.client(listener),
                                                      authorizer=authorizer)
            self.assertEqual(value['status'], 'denied')
            self.assertIsNone(value['diagnostics'])
            listener.assert_not_called()

    def test_revoke_during_scan_discards_every_observation(self):
        allowed = {'value': True}
        def listener(**kw):
            allowed['value'] = False
            return dict(IDENTITY)
        result = probe.collect_listener_preflight(
            client=self.client(listener), authorizer=lambda *args: allowed['value'])
        self.assertEqual(result['status'], 'denied')
        self.assertIsNone(result['diagnostics'])
        self.assertNotIn('7506', json.dumps(result))

    def test_revoke_during_public_projection_discards_result(self):
        allowed = {'value': True}
        original = probe.public_trace
        def project(value):
            projected = original(value)
            allowed['value'] = False
            return projected
        with mock.patch.object(probe, 'public_trace', side_effect=project):
            result = probe.collect_listener_preflight(
                client=self.client(lambda **kw: dict(IDENTITY)),
                authorizer=lambda *args: allowed['value'])
        self.assertEqual(result['status'], 'denied')
        self.assertIsNone(result['diagnostics'])

    def test_listener_exception_never_exports_sensitive_text(self):
        def listener(**kw):
            raise RuntimeError('/private/path/' + SECRET)
        value = probe.collect_listener_preflight(
            client=self.client(listener), authorizer=lambda *args: True)
        self.assertEqual(value['reason'], 'transport_failed')
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertNotIn('/private', json.dumps(value))

    def test_injected_unknown_observer_fields_never_enter_public_result(self):
        class Client:
            def probe_listener(self, *, authorizer, observer):
                observer.update({
                    'schema': 1, 'error_reason': SECRET, 'discarded_at': SECRET,
                    'token': SECRET, 'argv': ['/secret/' + SECRET],
                    'listener_checks': {'initial': {
                        'status': 'passed', 'reason_code': SECRET,
                        'identity': dict(IDENTITY, path=SECRET),
                        'runtime': {'raw': SECRET, 'scan': {
                            'reason_codes': [SECRET], 'counters': {SECRET: 33}}},
                    }}})
                return dict(IDENTITY)
        value = probe.collect_listener_preflight(
            client=Client(), authorizer=lambda *args: True)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_authorization_denial_inside_client_is_not_revivable(self):
        class Client:
            def probe_listener(self, *, authorizer, observer):
                observer['secret'] = SECRET
                raise transport.BusinessTransportError('authorization_denied')
        result = probe.collect_listener_preflight(
            client=Client(), authorizer=lambda *args: True)
        self.assertEqual(result['status'], 'denied')
        self.assertIsNone(result['diagnostics'])


class GetterTransportProjectionTests(unittest.TestCase):
    def test_step_preserves_possible_send_and_discards_private_exception(self):
        class Client:
            def request(self, *args, observer, **kwargs):
                observer.update({'schema': 1, 'request_sent': True,
                                 'send_attempted': True, 'send_completed': True,
                                 'response_received': True,
                                 'discarded_at': 'after_response',
                                 'error_reason': 'identity_changed', 'raw': SECRET})
                raise transport.BusinessTransportError('identity_changed', request_sent=True)
        step = probe._public_step(probe._step(
            Client(), 'video_capacity', {}, lambda *args: True, lambda value: True))
        self.assertTrue(step['request_sent'])
        self.assertTrue(step['transport']['response_received'])
        self.assertEqual(step['transport']['discarded_at'], 'after_response')
        self.assertNotIn(SECRET, json.dumps(step))

    def test_initial_failure_does_not_claim_request_sent(self):
        class Client:
            def request(self, *args, observer, **kwargs):
                observer.update({'schema': 1, 'request_started': True,
                                 'discarded_at': 'initial',
                                 'error_reason': 'identity_unverified'})
                raise transport.BusinessTransportError('identity_unverified', request_sent=False)
        step = probe._public_step(probe._step(
            Client(), 'offline_video_path', {}, lambda *args: True, lambda value: True))
        self.assertFalse(step['request_sent'])
        self.assertFalse(step['transport']['send_attempted'])
        self.assertEqual(step['transport']['discarded_at'], 'initial')


if __name__ == '__main__':
    unittest.main()
