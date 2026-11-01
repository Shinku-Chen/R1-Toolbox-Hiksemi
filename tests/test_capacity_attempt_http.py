"""Real Web gates expose only already-executed capacity attempt decisions."""
import copy
import http.client
import json
import socketserver
import sys
import threading
import unittest
from unittest import mock

import test_capacity_refresh_http as capacity_http


class AttemptReceipt(dict):
    """Private production-shaped carrier; JSON must not reveal its identifier."""
    capacity_attempt_id = 71


class CapacityAttemptHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capacity_http.CapacityRefreshHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server, self.monitor = self.fixture.server, self.fixture.monitor
        # The fixture's loader may be replaced by other discovery modules.
        self.web = capacity_http.web_config
        self.events, self.origins, self.receipts = [], [], []
        self.local = threading.local()
        self.done = threading.Event()
        self.published = threading.Event()
        self.origin_fault = self.response_fault = False
        self.publish_result = True
        self.publisher = mock.Mock(side_effect=self.publish)
        self.monitor.capacity_attempt_origin_set = self.origin_set
        self.monitor.capacity_attempt_response = self.observe
        self.monitor.publish_capacity_history = self.publisher
        original = self.monitor.refresh_capacity

        def refresh(**kwargs):
            self.origins.append(getattr(self.local, 'origin', 'unknown'))
            kwargs.pop('eligibility', None)
            value = AttemptReceipt(original(**kwargs))
            self.receipts.append(value)
            return value

        self.monitor.refresh_capacity = refresh
        self.monitor.refresh_capacity_missing = refresh
        self.monitor.refresh_capacity_visible = refresh
        self.fixture.insights.fill_smart_missing.return_value = {'ok': True, 'status': 'complete'}
        self.fixture.insights.refresh_smart_visible.return_value = {'ok': True, 'status': 'complete'}
        self.fixture.insights.capacity_fill_eligibility = lambda _: None
        self.fixture.insights.capacity_visible_eligibility.return_value = lambda _: None

    def origin_set(self, origin):
        if self.origin_fault:
            raise ValueError('private-origin-canary')
        previous = getattr(self.local, 'origin', 'unknown')
        self.local.origin = origin
        return previous

    def observe(self, receipt, stage, status, *, reason=None, observations=None):
        if self.response_fault:
            raise ValueError('private-observer-canary')
        self.events.append({'receipt': receipt, 'stage': stage, 'status': status,
                            'reason': reason, 'observations': copy.deepcopy(observations)})
        if stage == 'finalize':
            self.done.set()

    def publish(self, receipt, *, authorizer):
        self.published.set()
        return self.publish_result

    def request(self, **kwargs):
        result = self.fixture.request({}, **kwargs)
        self.done.wait(1)
        return result

    def stage(self, name):
        return [event for event in self.events if event['stage'] == name]

    def test_owner_routes_bind_distinct_origin_and_restore_after_business(self):
        for route, origin in ((self.web.CAPACITY_REFRESH_ROUTE, 'web_owner_capacity'),
                              (self.web.STORAGE_FILL_ROUTE, 'web_owner_fill'),
                              (self.web.STORAGE_REFRESH_ROUTE, 'web_owner_refresh')):
            with self.subTest(route=route):
                self.done.clear()
                code, value = self.request(path=route)
                self.assertEqual(code, 200)
                self.assertEqual(self.origins[-1], origin)
                self.assertEqual([event['stage'] for event in self.events[-5:]],
                                 ['web_return', 'json_before', 'json_after', 'write', 'finalize'])
                self.assertTrue(all(event['status'] == 'passed' for event in self.events[-5:]))
                self.assertTrue(all(event['receipt'] is self.receipts[-1]
                                    for event in self.events[-5:]))
                self.assertNotIn('capacity_attempt_id', json.dumps(value))
        # The actual server worker thread restored its own origin, not a global
        # owner flag that a direct/local invocation could accidentally inherit.
        self.server.storage_fill(authorizer=lambda: True)
        self.assertEqual(self.origins[-1], 'unknown')

    def test_origin_finally_restores_even_when_original_business_raises(self):
        self.monitor.error = capacity_http.RefreshError('timeout')
        code, _ = self.request()
        self.assertEqual(code, 503)
        self.assertEqual(self.origins, ['web_owner_capacity'])
        self.assertEqual(self.events, [])
        self.monitor.error = None
        self.monitor.refresh_capacity(authorizer=lambda: True)
        self.assertEqual(self.origins[-1], 'unknown')

    def test_original_receipt_survives_guard_replacement_after_core_success(self):
        original = self.server.panel_post
        def revoke(*args, **kwargs):
            value = original(*args, **kwargs)
            self.server.auth_mode = 'pair'
            return value
        with mock.patch.object(self.server, 'panel_post', side_effect=revoke):
            code, value = self.request()
        self.assertEqual(code, 403)
        self.assertNotIn('results', value)
        self.assertEqual(self.stage('json_before')[0]['reason'], 'authorization_denied')
        self.assertIs(self.stage('json_before')[0]['receipt'], self.receipts[0])
        self.assertEqual(self.stage('finalize')[0]['status'], 'not_run')
        self.publisher.assert_not_called()

    def test_json_revoke_then_regrant_keeps_original_false_latch_and_receipt(self):
        original_authorizer, original_dumps = self.server._capacity_authorizer, self.web.json.dumps
        grants = []
        def authorize(*args, **kwargs):
            grant = original_authorizer(*args, **kwargs)
            grants.append(grant)
            return grant
        def dumps(value, *args, **kwargs):
            if isinstance(value, AttemptReceipt):
                self.server.auth_mode = 'pair'
                self.assertFalse(grants[-1]())
                self.server.auth_mode = 'password'
            return original_dumps(value, *args, **kwargs)
        with mock.patch.object(self.server, '_capacity_authorizer', side_effect=authorize), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request()
        self.assertEqual(code, 403)
        self.assertNotIn('results', value)
        self.assertEqual(self.stage('json_after')[0]['reason'], 'authorization_denied')
        self.assertTrue(self.stage('json_after')[0]['observations']['serialized'])
        self.assertIs(self.stage('finalize')[0]['receipt'], self.receipts[0])
        self.publisher.assert_not_called()

    def test_json_generation_change_does_not_revive_old_handler(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, AttemptReceipt):
                self.server._media_sort_generation = object()
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request()
        self.assertEqual(code, 403)
        self.assertNotIn('results', value)
        self.assertEqual(self.stage('json_after')[0]['reason'], 'authorization_denied')
        self.publisher.assert_not_called()

    def test_web_return_revocation_records_existing_guard_without_publishing(self):
        original = self.monitor.refresh_capacity
        def refresh(**kwargs):
            value = original(**kwargs)
            self.server.auth_mode = 'pair'
            return value
        with mock.patch.object(self.monitor, 'refresh_capacity', side_effect=refresh):
            code, _ = self.request()
        self.assertEqual(code, 403)
        self.assertEqual(self.stage('web_return')[0]['reason'], 'authorization_denied')
        self.publisher.assert_not_called()

    def test_transport_body_failure_is_distinct_from_successful_core_and_json(self):
        original = socketserver._SocketWriter.write
        failed = threading.Event()
        def write(writer, data):
            if b'"results"' in data and b'"refreshed"' in data:
                failed.set()
                raise OSError('private-transport-canary')
            return original(writer, data)
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            with self.assertRaises(http.client.IncompleteRead):
                self.request()
            self.assertTrue(self.done.wait(1))
        self.assertTrue(failed.is_set())
        self.assertEqual(self.stage('json_after')[0]['status'], 'passed')
        self.assertEqual(self.stage('write')[0]['reason'], 'transport_error')
        self.assertEqual(self.stage('finalize')[0]['status'], 'not_run')
        self.assertEqual(len(self.monitor.commits), 1)
        self.publisher.assert_not_called()

    def test_json_exception_keeps_original_exception_and_reports_unexecuted_write(self):
        error = ValueError('private-json-canary')
        original = self.web.json.dumps
        caught = []
        failure = threading.Event()
        def dumps(value, *args, **kwargs):
            if isinstance(value, AttemptReceipt):
                raise error
            return original(value, *args, **kwargs)
        def handle_error(*args):
            caught.append(sys.exc_info()[1])
            failure.set()
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps), \
                mock.patch.object(self.server.server, 'handle_error', side_effect=handle_error):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request()
            self.assertTrue(failure.wait(1))
        self.assertIs(caught[0], error)
        self.assertEqual(self.stage('json_after')[0]['reason'], 'encoding_unavailable')
        self.assertEqual(self.stage('write')[0]['status'], 'not_run')
        self.assertEqual(self.stage('finalize')[0]['status'], 'not_run')
        self.publisher.assert_not_called()

    def test_header_transport_failure_preserves_original_exception_without_finalize(self):
        error = OSError('private-header-canary')
        original = socketserver._SocketWriter.write
        caught, failure = [], threading.Event()
        def write(writer, data):
            if data.startswith(b'HTTP/1.0 200'):
                raise error
            return original(writer, data)
        def handle_error(*args):
            caught.append(sys.exc_info()[1])
            failure.set()
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write), \
                mock.patch.object(self.server.server, 'handle_error', side_effect=handle_error):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request()
            self.assertTrue(failure.wait(1))
        self.assertIs(caught[0], error)
        self.assertEqual(self.stage('write')[0]['reason'], 'response_error')
        self.assertEqual(self.stage('finalize')[0]['status'], 'not_run')
        self.publisher.assert_not_called()

    def test_after_write_revocation_records_actual_pre_finalizer_guard(self):
        original = socketserver._SocketWriter.write
        def write(writer, data):
            result = original(writer, data)
            if b'"results"' in data and b'"refreshed"' in data:
                self.server.auth_mode = 'pair'
            return result
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(self.stage('write')[0]['status'], 'passed')
        self.assertEqual(self.stage('finalize')[0]['reason'], 'authorization_denied')
        self.assertFalse(self.stage('finalize')[0]['observations']['guard_passed'])
        self.publisher.assert_not_called()

    def test_observer_and_origin_double_fault_do_not_change_response_or_lock(self):
        self.origin_fault = self.response_fault = True
        code, value = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(len(self.monitor.commits), 1)
        self.assertFalse(self.monitor.lock.locked())
        self.publisher.assert_called_once()
        self.assertNotIn('canary', json.dumps(value))

    def test_observation_wrapper_entry_double_fault_cannot_change_business(self):
        with mock.patch.object(self.server, '_capacity_attempt_origin_set', side_effect=ValueError('origin-entry')), \
                mock.patch.object(self.server, '_capacity_attempt_response', side_effect=ValueError('response-entry')):
            code, value = self.fixture.request({})
            self.assertTrue(self.published.wait(1))
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(self.origins, ['unknown'])
        self.assertEqual(len(self.monitor.commits), 1)
        self.assertFalse(self.monitor.lock.locked())
        self.publisher.assert_called_once()

    def test_observer_entry_double_fault_preserves_original_json_exception(self):
        error = ValueError('private-original-json-canary')
        original = self.web.json.dumps
        caught, failure = [], threading.Event()
        def dumps(value, *args, **kwargs):
            if isinstance(value, AttemptReceipt):
                raise error
            return original(value, *args, **kwargs)
        def handle_error(*args):
            caught.append(sys.exc_info()[1])
            failure.set()
        with mock.patch.object(self.server, '_capacity_attempt_origin_set', side_effect=ValueError('origin-entry')), \
                mock.patch.object(self.server, '_capacity_attempt_response', side_effect=ValueError('response-entry')), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps), \
                mock.patch.object(self.server.server, 'handle_error', side_effect=handle_error):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.fixture.request({})
            self.assertTrue(failure.wait(1))
        self.assertIs(caught[0], error, 'pure observation must not replace the business exception')
        self.assertEqual(len(self.monitor.commits), 1)
        self.assertFalse(self.monitor.lock.locked())
        self.publisher.assert_not_called()

    def test_observation_adds_no_authorizer_calls(self):
        original = self.server._capacity_authorizer
        calls = []
        def authorizer(*args, **kwargs):
            grant = original(*args, **kwargs)
            def current():
                calls.append(None)
                return grant()
            return current
        with mock.patch.object(self.server, '_capacity_authorizer', side_effect=authorizer):
            code, _ = self.request()
        self.assertEqual(code, 200)
        # Existing owner handler (1), core fixture (2), handler return (1),
        # JSON before/after (2), and pre-publication guard (1), without a
        # second call for any diagnostic stage.
        self.assertEqual(len(calls), 7)

    def test_publisher_false_none_and_exception_are_separate_observations(self):
        for value, status, reason in ((False, 'failed', 'publication_rejected'),
                                      (None, 'not_run', 'publication_result_unobserved')):
            with self.subTest(value=value):
                self.done.clear()
                self.events.clear()
                self.publish_result = value
                self.assertEqual(self.request()[0], 200)
                self.assertEqual(self.stage('finalize')[0]['status'], status)
                self.assertEqual(self.stage('finalize')[0]['reason'], reason)
        self.events.clear()
        self.done.clear()
        with mock.patch.object(self.monitor, 'publish_capacity_history', side_effect=ValueError('private-publish-canary')):
            self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.stage('finalize')[0]['reason'], 'publication_error')

    def test_no_owner_bearer_and_invalid_body_never_create_owner_attempt(self):
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            self.assertEqual(self.request()[0], 403)
        self.server.auth_mode = 'password'
        self.assertEqual(self.request(headers={'Authorization': 'Bearer private-canary'})[0], 403)
        self.assertEqual(self.fixture.request({'path': '/private/canary'})[0], 400)
        self.assertEqual(self.monitor.calls, [])
        self.assertEqual(self.origins, [])
        self.assertEqual(self.events, [])

    def test_passive_and_public_responses_do_not_emit_attempt_hooks(self):
        for route in ('/api/overview', '/api/r1', '/api/v1/status'):
            code, value = self.fixture.request(method='GET', path=route)
            self.assertIn(code, (200, 404))
            self.assertNotIn('capacity_attempt_id', json.dumps(value))
        self.assertEqual(self.origins, [])
        self.assertEqual(self.events, [])
        self.publisher.assert_not_called()


if __name__ == '__main__':
    unittest.main()
