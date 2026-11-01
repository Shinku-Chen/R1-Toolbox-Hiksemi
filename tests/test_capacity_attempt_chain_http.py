"""Actual sampler, monitor and Web handlers retain one capacity attempt chain."""
import copy
import http.client
import ipaddress
import json
import socketserver
import sys
import threading
import time
import unittest
from unittest import mock

import test_capacity_history_core as history_core
import test_capacity_refresh_http as capacity_http


class CapacityAttemptChainHTTPTests(unittest.TestCase):
    def setUp(self):
        self.core = history_core.CapacityHistoryCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.fixture = capacity_http.CapacityRefreshHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.web, self.service = capacity_http.web_config, history_core.service
        self.server, self.sampler = self.fixture.server, self.core.sampler
        self.monitor = self.service.StatusMonitor(self.sampler)
        self.server.monitor = self.server.panel.monitor = self.monitor
        self.server.panel.proc_root, self.server.panel.sys_root = self.core.proc, self.core.sys
        self.fixture.insights.snapshot.return_value = {
            'disks': {'items': [{'id': name, 'type': 'HDD', 'size_bytes': 100000,
                                 'health': {}, 'smart_available': True} for name in ('sda', 'sdb')]},
            'device': {'storage': {'devices': []}}}
        self.fixture.insights.refresh_smart_visible.return_value = {
            'ok': True, 'status': 'complete', 'results': []}
        self.fixture.insights.fill_smart_missing.return_value = {
            'ok': True, 'status': 'complete', 'results': []}
        self.fixture.insights.capacity_visible_eligibility.return_value = lambda _: None
        self.fixture.insights.capacity_fill_eligibility = lambda _: None
        self.done = threading.Event()
        original = self.monitor.capacity_attempt_response
        def observe(receipt, stage, status, reason=None, observations=None):
            result = original(receipt, stage, status, reason, observations)
            if stage == 'finalize' or stage == 'web_return' and status == 'failed':
                self.done.set()
            return result
        patch = mock.patch.object(self.monitor, 'capacity_attempt_response', side_effect=observe)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, *, route=None):
        self.done.clear()
        result = self.fixture.request({}, path=route or self.web.STORAGE_REFRESH_ROUTE)
        self.assertTrue(self.done.wait(2), 'real response did not finish its attempt observation')
        return result

    def evidence(self):
        return self.monitor.capacity_cache_diagnostics()['attempt_diagnostics']

    @staticmethod
    def phases(evidence, attempt, field):
        """Decode the public compact phase contract without private store reads."""
        logical = history_core.service._capacity_attempt_decode(evidence)
        attempt = logical['attempts'][evidence['attempts'].index(attempt)]
        result = {}
        for stage, subphase, status, reason, observations, observer_error in attempt[field]:
            label = logical['stage_values'][stage]
            if subphase is not None:
                label += '/' + logical['worker_phase_order'][subphase]
            if type(observations) is not dict:
                raise AssertionError('strict decoder did not restore fixed observation fields')
            result[label] = {'status': logical['status_values'][status],
                             'reason': logical['reason_values'][reason],
                             'observation_error': observer_error,
                             'observations': observations}
        return result

    def test_real_visible_refresh_candidate_json_write_and_finalizer_share_attempt(self):
        code, receipt = self.request()
        self.assertEqual(code, 200)
        self.assertEqual([row['status'] for row in receipt['capacity']['results']], ['refreshed'] * 3)
        evidence = self.evidence()
        self.assertEqual(evidence['observation_status'], 'complete')
        self.assertEqual(len(evidence['attempts']), 1)
        attempt = evidence['attempts'][0]
        self.assertEqual(attempt['origin'], 'web_owner_refresh')
        self.assertEqual(attempt['source'], 'visible_refresh')
        self.assertEqual(attempt['status'], 'complete')
        self.assertEqual(set(attempt['rows']), {'11', '12', '21'})
        phases = self.phases(evidence, attempt, 'history')
        for stage in ('candidate', 'web_return', 'json_before', 'json_after', 'write', 'finalize'):
            self.assertEqual(phases[stage]['status'], 'passed', (stage, phases))
            self.assertFalse(phases[stage]['observation_error'])
        self.assertTrue(self.sampler._capacity_cache)
        self.assertTrue(self.core.view()[1]['summary'])
        serialized = json.dumps(receipt)
        for marker in ('capacity_attempt_id', '/sys/physical-', '/drives/'):
            self.assertNotIn(marker, serialized)

    def test_real_owner_manual_and_fill_origins_remain_distinct(self):
        for route, origin, source in ((self.web.CAPACITY_REFRESH_ROUTE, 'web_owner_capacity', 'explicit_refresh'),
                                      (self.web.STORAGE_FILL_ROUTE, 'web_owner_fill', 'missing_fill')):
            with self.subTest(route=route):
                self.assertEqual(self.request(route=route)[0], 200)
                evidence = self.evidence()
                attempt = evidence['attempts'][-1]
                self.assertEqual(attempt['origin'], origin)
                self.assertEqual(attempt['source'], source)
                self.assertEqual(evidence['observation_status'], 'complete')

    def test_json_revocation_retains_core_candidate_and_failed_response_same_id(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and type(value.get('capacity')) is self.service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request()
        self.assertEqual(code, 403)
        self.assertNotIn('capacity', value)
        evidence = self.evidence()
        self.assertEqual(len(evidence['attempts']), 1)
        phases = self.phases(evidence, evidence['attempts'][0], 'history')
        self.assertEqual(phases['candidate']['status'], 'passed')
        self.assertEqual(phases['json_after']['reason'], 'authorization_denied')
        self.assertEqual(phases['write']['status'], 'passed')
        self.assertEqual(phases['finalize']['status'], 'not_run')
        self.assertTrue(self.sampler._capacity_cache)
        self.assertFalse(self.core.view()[1]['summary'])

    def test_first_owner_json_failure_survives_later_nonowner_cache_fill_successes(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and type(value.get('capacity')) is self.service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            self.assertEqual(self.request()[0], 403)
        first = self.evidence()['attempts'][0]['id']
        for _ in range(7):
            # A distinct direct/local-origin fill may successfully consume the
            # original current cache without acquiring owner publication rights.
            result = self.server.storage_fill(authorizer=lambda: True)
            self.assertTrue(result['ok'])
        evidence = self.evidence()
        self.assertEqual(evidence['first_rejected_id'], first)
        self.assertLessEqual(len(evidence['attempts']), 4)
        attempt = next(item for item in evidence['attempts'] if item['id'] == first)
        self.assertEqual(attempt['status'], 'complete', 'do not relabel successful core work')
        phases = self.phases(evidence, attempt, 'history')
        self.assertEqual(phases['json_after']['reason'], 'authorization_denied')
        self.assertIsNone(self.core.view()[1]['summary'])

    def test_internal_finalizer_rejection_is_not_overwritten_by_web_generic_result(self):
        original = socketserver._SocketWriter.write
        def write(writer, data):
            result = original(writer, data)
            if b'"capacity"' in data and b'"refreshed"' in data:
                with self.sampler._capacity_lock:
                    self.sampler._capacity_replace_cache({})
            return result
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        evidence = self.evidence()
        phases = self.phases(evidence, evidence['attempts'][0], 'history')
        self.assertEqual(phases['finalize/cache_match']['reason'], 'cache_changed')
        self.assertEqual(phases['finalize']['reason'], 'publication_rejected')
        self.assertFalse(self.core.view()[1]['summary'])

    def test_delayed_json_attempt_cannot_overwrite_later_http_publication(self):
        original = self.web.json.dumps
        paused, release, first_lock = threading.Event(), threading.Event(), threading.Lock()
        first, responses, errors = [], [], []
        def dumps(value, *args, **kwargs):
            candidate = value.get('capacity') if isinstance(value, dict) else None
            if type(candidate) is self.service._CapacityHistoryReceipt:
                with first_lock:
                    delayed = not first
                    if delayed:
                        first.append(candidate)
                if delayed:
                    paused.set()
                    if not release.wait(3):
                        raise AssertionError('controlled first JSON response was not released')
            return original(value, *args, **kwargs)
        def request_first():
            try:
                responses.append(self.fixture.request({}, path=self.web.STORAGE_REFRESH_ROUTE))
            except Exception as error:
                errors.append(error)
        # Equal real commit dates and values ensure only the business sequence,
        # rather than a changed cache/date, decides the late finalizer outcome.
        with mock.patch.object(self.service.time, 'time', return_value=1793522657.0), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            thread = threading.Thread(target=request_first)
            thread.start()
            try:
                self.assertTrue(paused.wait(2))
                self.assertEqual(self.request()[0], 200)
                published = copy.deepcopy(self.core.view()[1])
                self.assertTrue(published['summary'])
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0][0], 200)
        evidence = self.evidence()
        self.assertEqual(len(evidence['attempts']), 2)
        attempts = sorted(evidence['attempts'], key=lambda item: item['id'])
        old, new = [self.phases(evidence, attempt, 'history') for attempt in attempts]
        self.assertEqual(old['json_after']['status'], 'passed')
        self.assertEqual(old['write']['status'], 'passed')
        self.assertEqual(old['finalize/ordering']['reason'], 'superseded')
        self.assertEqual(old['finalize']['reason'], 'publication_rejected')
        self.assertEqual(new['finalize/published']['status'], 'passed')
        self.assertEqual(new['finalize']['status'], 'passed')
        self.assertEqual(self.core.view()[1], published)
        self.assertEqual(published['summary']['observation_id'], str(first[0].batch + 1))
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())

    def test_actual_ram_diagnostic_preserves_attempt_without_any_new_source_read(self):
        self.assertEqual(self.request()[0], 200)
        expected = self.evidence()
        # Model only the persisted dev-config read seam. Real peer matching,
        # expiry, request generation and JSON guards still execute normally.
        self.server.dev_no_pairing = True
        self.server.dev_allow_ips = {ipaddress.ip_address('127.0.0.1')}
        self.server.dev_until = time.time() + 60
        with mock.patch.object(self.server, '_sync_development'), \
                mock.patch.object(self.sampler, '_capacity_inventory_observe', side_effect=AssertionError('inventory')), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError('projection')), \
                mock.patch.object(self.sampler, 'sample', side_effect=AssertionError('sample')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(self.core.watch, 'changed', side_effect=AssertionError('poll')):
            code, value = self.fixture.request({'action': 'snapshot', 'checks': ['capacity_cache']},
                                               path='/api/dev/diagnostics')
        self.assertEqual(code, 200)
        check = value['snapshot']['checks']['capacity_cache']
        self.assertTrue(check['ok'])
        evidence = check['data']['capacity_cache']['attempt_diagnostics']
        self.assertEqual(evidence, expected)
        serialized = json.dumps(value)
        for marker in ('/sys/physical-', '/drives/', 'capacity_attempt_id'):
            self.assertNotIn(marker, serialized)

    def test_real_observer_double_fault_does_not_change_commit_history_or_lock(self):
        with mock.patch.object(self.sampler._capacity_attempts, 'step', side_effect=ValueError('step-canary')), \
                mock.patch.object(self.sampler._capacity_attempts, 'history', side_effect=ValueError('history-canary')):
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(len(self.sampler._capacity_cache), 3)
        self.assertFalse(self.sampler._capacity_refresh_lock.locked())
        self.assertTrue(self.core.view()[1]['summary'])
        self.assertNotIn('canary', json.dumps(value))

    def test_ram_codec_and_handler_json_double_fault_preserve_original_exception(self):
        self.assertEqual(self.request()[0], 200)
        cache, history = copy.deepcopy(self.sampler._capacity_cache), copy.deepcopy(self.sampler._capacity_history)
        measurements = list(self.core.calls)
        self.server.dev_no_pairing = True
        self.server.dev_allow_ips = {ipaddress.ip_address('127.0.0.1')}
        self.server.dev_until = time.time() + 60
        error = ValueError('private-original-diagnostic-json-canary')
        original = self.web.json.dumps
        caught, states, failure = [], [], threading.Event()
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and isinstance(value.get('snapshot'), dict):
                checks = value['snapshot'].get('checks', {})
                if 'capacity_cache' in checks:
                    states.append(checks['capacity_cache']['data']['capacity_cache']
                                  ['attempt_diagnostics']['observation_status'])
                    raise error
            return original(value, *args, **kwargs)
        def handle_error(*args):
            caught.append(sys.exc_info()[1])
            failure.set()
        with mock.patch.object(self.server, '_sync_development'), \
                mock.patch.object(self.service, '_capacity_attempt_encode', side_effect=ValueError('codec-canary')) as encoded, \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps), \
                mock.patch.object(self.server.server, 'handle_error', side_effect=handle_error), \
                mock.patch.object(self.sampler, '_capacity_inventory_observe', side_effect=AssertionError('inventory')), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError('projection')), \
                mock.patch.object(self.sampler, 'sample', side_effect=AssertionError('sample')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(self.core.watch, 'changed', side_effect=AssertionError('poll')):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.fixture.request({'action': 'snapshot', 'checks': ['capacity_cache']},
                                     path='/api/dev/diagnostics')
            self.assertTrue(failure.wait(1))
            encoded.assert_called_once()
        self.assertIs(caught[0], error)
        self.assertEqual(states, ['observer_error'], 'codec failure must not claim complete evidence')
        self.assertEqual(self.sampler._capacity_cache, cache)
        self.assertEqual(self.sampler._capacity_history, history)
        self.assertEqual(self.core.calls, measurements)
        for lock in (self.sampler._capacity_refresh_lock, self.server.diagnostic_lock,
                     self.server.storage_diagnostic_lock):
            self.assertFalse(lock.locked())

    def test_ram_codec_fault_cannot_regrant_a_revoked_development_response(self):
        self.assertEqual(self.request()[0], 200)
        cache, history = copy.deepcopy(self.sampler._capacity_cache), copy.deepcopy(self.sampler._capacity_history)
        measurements = list(self.core.calls)
        self.server.dev_no_pairing = True
        self.server.dev_allow_ips = {ipaddress.ip_address('127.0.0.1')}
        self.server.dev_until = time.time() + 60
        original_dumps, original_authorizer = self.web.json.dumps, self.server._media_debug_authorizer
        grants, states = [], []
        def authorize(*args, **kwargs):
            grant = original_authorizer(*args, **kwargs)
            grants.append(grant)
            return grant
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and isinstance(value.get('snapshot'), dict):
                checks = value['snapshot'].get('checks', {})
                if 'capacity_cache' in checks:
                    states.append(checks['capacity_cache']['data']['capacity_cache']
                                  ['attempt_diagnostics']['observation_status'])
                    expiry = self.server.dev_until
                    self.server.dev_until = 0
                    self.assertFalse(grants[-1]())
                    self.server.dev_until = expiry
            return original_dumps(value, *args, **kwargs)
        with mock.patch.object(self.server, '_sync_development'), \
                mock.patch.object(self.service, '_capacity_attempt_encode', side_effect=ValueError('codec-canary')) as encoded, \
                mock.patch.object(self.server, '_media_debug_authorizer', side_effect=authorize), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps), \
                mock.patch.object(self.sampler, '_capacity_inventory_observe', side_effect=AssertionError('inventory')), \
                mock.patch.object(self.sampler, 'capacity_snapshot', side_effect=AssertionError('projection')), \
                mock.patch.object(self.sampler, 'sample', side_effect=AssertionError('sample')), \
                mock.patch.object(self.sampler, '_capacity_probe', side_effect=AssertionError('worker')), \
                mock.patch.object(self.core.watch, 'changed', side_effect=AssertionError('poll')):
            code, value = self.fixture.request({'action': 'snapshot', 'checks': ['capacity_cache']},
                                               path='/api/dev/diagnostics')
            encoded.assert_called_once()
        self.assertEqual(code, 403)
        self.assertEqual(value['code'], 'development_authorization_expired')
        self.assertNotIn('snapshot', value)
        self.assertEqual(states, ['observer_error'])
        self.assertEqual(len(grants), 1)
        self.assertFalse(grants[0](), 'restoring the window must not revive the old response')
        self.assertEqual(self.sampler._capacity_cache, cache)
        self.assertEqual(self.sampler._capacity_history, history)
        self.assertEqual(self.core.calls, measurements)
        for lock in (self.sampler._capacity_refresh_lock, self.server.diagnostic_lock,
                     self.server.storage_diagnostic_lock):
            self.assertFalse(lock.locked())

    def test_private_attempt_id_cannot_supply_history_authority_to_public_copy(self):
        receipt = self.sampler.refresh_capacity(authorizer=lambda: True)
        clone = self.service._CapacityAttemptResult(copy.deepcopy(dict(receipt)),
                                                    getattr(receipt, 'capacity_attempt_id', None))
        self.assertFalse(self.monitor.publish_capacity_history(clone, authorizer=lambda: True))
        self.assertIsNone(self.core.view()[1]['summary'])
        self.assertTrue(self.monitor.publish_capacity_history(receipt, authorizer=lambda: True))
        self.assertTrue(self.core.view()[1]['summary'])

    def test_real_failed_core_receipt_keeps_row_cause_and_response_association(self):
        with mock.patch.object(self.sampler, '_capacity_probe',
                               side_effect=self.service.CapacityRefreshError('identity_changed')):
            code, value = self.request()
        self.assertEqual(code, 200)
        self.assertEqual([row['status'] for row in value['capacity']['results']],
                         ['identity_changed'] * 3)
        evidence = self.evidence()
        self.assertEqual(len(evidence['attempts']), 1)
        attempt = evidence['attempts'][0]
        self.assertEqual(attempt['origin'], 'web_owner_refresh')
        self.assertEqual(set(attempt['rows']), {'11', '12', '21'})
        self.assertIsNotNone(attempt['first_rejection'])
        phases = self.phases(evidence, attempt, 'history')
        self.assertEqual(phases['candidate']['status'], 'not_run')
        self.assertEqual(phases['candidate']['reason'], 'no_pending')
        self.assertEqual(phases['json_after']['status'], 'passed')
        self.assertEqual(phases['write']['status'], 'passed')
        self.assertEqual(phases['finalize']['reason'], 'publication_rejected')
        self.assertFalse(self.sampler._capacity_cache)
        self.assertIsNone(self.core.view()[1]['summary'])

    def test_real_history_prune_keeps_original_attempt_and_first_actual_removal(self):
        self.assertEqual(self.request()[0], 200)
        first_id = self.evidence()['attempts'][0]['id']
        self.core.generations['8:1'] += 1
        self.core.view()
        evidence = self.evidence()
        removal = self.service._capacity_attempt_decode(evidence)['first_historical_removal']
        self.assertIsNotNone(removal)
        self.assertEqual(removal['attempt_id'], first_id)
        self.assertEqual(removal['event'], 'prune')
        self.assertEqual(removal['status'], 'failed')
        self.assertGreater(removal['observations']['removed_count'], 0)
        before = copy.deepcopy(removal)
        for _ in range(35):
            self.core.view()
        self.assertEqual(self.service._capacity_attempt_decode(
            self.evidence())['first_historical_removal'], before)

    def test_real_local_screen_handler_records_local_origin_without_owner_publication(self):
        local = self.service.make_http_server(self.monitor, port=0, control_token='fixture-screen-token',
                                               web_config=self.server)
        thread = threading.Thread(target=lambda: local.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', local.server_port, timeout=3)
            try:
                connection.request('POST', '/api/storage/fill', b'{}',
                    {'Origin': 'null', 'X-R1-Token': 'fixture-screen-token',
                     'Content-Type': 'application/json'})
                response = connection.getresponse()
                value = json.loads(response.read())
                self.assertEqual(response.status, 200)
            finally:
                connection.close()
            self.assertTrue(value['ok'])
            evidence = self.evidence()
            self.assertEqual(len(evidence['attempts']), 1)
            attempt = evidence['attempts'][0]
            self.assertEqual(attempt['origin'], 'local_screen_fill')
            self.assertEqual(attempt['source'], 'missing_fill')
            phases = self.phases(evidence, attempt, 'history')
            self.assertEqual(phases['candidate']['status'], 'passed')
            self.assertNotIn('finalize', phases)
            self.assertIsNone(self.core.view()[1]['summary'])
            self.assertTrue(self.sampler._capacity_cache)
        finally:
            local.shutdown()
            local.server_close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_direct_storage_call_has_unknown_origin_and_no_web_publish(self):
        result = self.server.storage_fill(authorizer=lambda: True)
        self.assertTrue(result['ok'])
        evidence = self.evidence()
        attempt = evidence['attempts'][0]
        self.assertEqual(attempt['origin'], 'unknown')
        self.assertEqual(attempt['source'], 'missing_fill')
        phases = self.phases(evidence, attempt, 'history')
        self.assertEqual(phases['candidate']['status'], 'passed')
        self.assertNotIn('write', phases)
        self.assertNotIn('finalize', phases)
        self.assertIsNone(self.core.view()[1]['summary'])


if __name__ == '__main__':
    unittest.main()
