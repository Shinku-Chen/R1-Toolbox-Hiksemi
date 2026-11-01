"""Separate real cache loss from normal Monitor/Fnm projection failures.

The portable fixture substitutes only external SMART, worker and namespace
seams. The production collectors, sampler, Monitor, Fnm and HTTP handler remain
in the path. A diagnostic request must never replay any of those projections.
"""
import concurrent.futures
import contextlib
import copy
import hashlib
import json
import math
import threading
import unittest
from unittest import mock

import test_dev_diagnostics_http as diagnostics_http
import test_smart_power_compat as power_fixture


web_config = diagnostics_http.web_config
BODY = {'action': 'snapshot', 'checks': ['capacity_cache']}
PRIVATE = 'PRIVATE_PROJECTION_PATH_TOKEN_CANARY'


class CapacityProjectionHTTPTests(unittest.TestCase):
    request = diagnostics_http.DeveloperDiagnosticsHTTPTests.request
    _patch = diagnostics_http.DeveloperDiagnosticsHTTPTests._patch

    def setUp(self):
        diagnostics_http.DeveloperDiagnosticsHTTPTests.setUp(self)
        self.case = power_fixture.SmartPowerCompatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.ordinary_mix()
        smart, capacity, _, self.initial = self.case.refresh_visible()
        self.assertEqual(next(row['status'] for row in smart['results'] if row['device'] == 'sda'),
                         'skipped_standby')
        self.assertEqual(sum(row['status'] == 'refreshed' for row in capacity['results']), 3)
        self.server.monitor, self.server.panel = self.case.monitor, self.case.panel
        self.case.panel.web = self.server

    def overview(self):
        status, _, value = self.request(method='GET', path='/api/overview',
                                        headers={'Cookie': self.cookie})
        self.assertEqual(status, 200)
        return value

    def diagnostic_without_replay(self):
        """Trap every collection/projection seam while reading the RAM ledger."""
        sampler, panel, monitor = self.case.sampler, self.case.panel, self.case.monitor
        before = sampler.capacity_cache_diagnostics()
        fnm_before = panel.capacity_projection_diagnostics()
        smart_calls, worker_calls = copy.deepcopy(self.case.calls), list(self.case.capacity_calls)
        with contextlib.ExitStack() as stack:
            for target, name in (
                    (monitor, 'snapshot'), (sampler, 'capacity_snapshot'),
                    (sampler, 'sample'), (sampler, '_capacity_probe'),
                    (sampler, '_capacity_reap_exited'),
                    (self.case.collector, 'snapshot'), (self.case.collector, 'runner'),
                    (self.case.watch, 'changed'),
                    (panel, '_safe'), (panel, '_basic'), (panel, '_mounts'),
                    (panel, '_disks'), (panel, '_disk_summary'),
                    (power_fixture.service, '_capacity_inventory'),
                    (power_fixture.service, '_capacity_read'),
                    (power_fixture.service.subprocess, 'Popen')):
                stack.enter_context(mock.patch.object(target, name,
                    side_effect=AssertionError('diagnostic replayed ' + name)))
            stack.enter_context(mock.patch.object(power_fixture.service.os, 'statvfs', create=True,
                side_effect=AssertionError('diagnostic statvfs')))
            status, _, value = self.request(BODY)
        self.assertEqual(status, 200)
        check = value['snapshot']['checks']['capacity_cache']
        self.assertEqual(check['http_status'], 200)
        self.assertTrue(check['ok'])
        ledger = check['data']['capacity_cache']
        self.assertEqual(ledger['seq'], before['seq'])
        self.assertEqual(ledger['state'], before['state'])
        self.assertEqual(ledger['first_invalidation'], before['first_invalidation'])
        self.assertEqual(ledger['fnm'], fnm_before)
        self.assertEqual(self.case.calls, smart_calls)
        self.assertEqual(self.case.capacity_calls, worker_calls)
        encoded = json.dumps(value)
        for marker in (PRIVATE, '/drives/fixture-', '/dev/sdb1', power_fixture.smart_fixture.PRIVATE):
            self.assertNotIn(marker, encoded)
        return ledger

    def test_real_http_chain_records_measured_rows_dates_and_separate_summary_source(self):
        overview = self.overview()
        self.assertEqual(overview['disk_total'], 25.0)
        dates = {row['name']: row['capacity_sampled_at'] for row in overview['disks_detail']}
        self.assertTrue(all(dates[name] is not None for name in ('sdb', 'sdc', 'sdd')))
        ledger = self.diagnostic_without_replay()
        self.assertEqual(ledger['status'], 'complete')
        self.assertIs(ledger['evidence_returned'], True)
        self.assertIs(ledger['truncated'], False)
        self.assertEqual(ledger['response_limit_bytes'], 4 * 1024 * 1024)
        self.assertGreater(ledger['evidence_encoded_bytes'], 0)
        self.assertLessEqual(ledger['evidence_encoded_bytes'],
                             ledger['response_limit_bytes'] - 16384)
        self.assertEqual(ledger['state']['positive'], 3)
        self.assertIsNone(ledger['first_invalidation'])
        latest = ledger['fnm']['latest']
        self.assertEqual(latest['disks']['result'], 'returned')
        self.assertEqual(latest['disks']['measured_count'], 3)
        self.assertEqual(latest['disks']['sample_date_count'], 3)
        self.assertEqual(latest['disks']['sampled_at_oldest'], min(
            dates[name] for name in ('sdb', 'sdc', 'sdd')))
        self.assertTrue(latest['summary']['summary_has_value'])
        self.assertTrue(latest['overview']['summary_has_value'])
        disk_source, summary_source = (latest[stage]['monitor_source']
                                       for stage in ('disks', 'summary'))
        self.assertEqual(disk_source['status'], 'observed')
        self.assertEqual(summary_source['status'], 'observed')
        self.assertNotEqual(disk_source['sequence'], summary_source['sequence'])
        self.assertEqual(disk_source['measured_count'], 3)
        self.assertEqual(summary_source['measured_count'], 3)

    def test_monitor_projection_error_keeps_cache_and_original_fnm_fallback(self):
        error = RuntimeError(PRIVATE)
        with mock.patch.object(self.case.sampler, 'capacity_snapshot', side_effect=error):
            overview = self.overview()
        self.assertTrue(all(row['total'] is None for row in overview['disks_detail']))
        self.assertIsNone(overview['disk_total'])
        ledger = self.diagnostic_without_replay()
        self.assertEqual(ledger['state']['positive'], 3)
        self.assertIsNone(ledger['first_invalidation'])
        projection = ledger['monitor']['projection']
        self.assertEqual(projection['latest']['result'], 'projection_error')
        self.assertEqual(ledger['fnm']['latest']['safe']['result'], 'projection_error')
        self.assertEqual(ledger['fnm']['latest']['disks']['measured_count'], 0)
        self.assertFalse(ledger['fnm']['latest']['summary']['summary_has_value'])

    def test_mount_matching_failure_is_not_reported_as_sampler_cache_loss(self):
        # The production Fnm cache is the controlled external metadata seam.
        # Real matching/aggregation still consume it; no output JSON is forged.
        panel = self.case.panel
        panel._mount_cache = (self.case.now, {
            name: ['/drives/' + PRIVATE + '-' + name] for name in self.case.mounts})
        overview = self.overview()
        self.assertTrue(all(row['total'] is None for row in overview['disks_detail']))
        self.assertIsNone(overview['disk_total'])
        ledger = self.diagnostic_without_replay()
        self.assertEqual(ledger['state']['positive'], 3)
        self.assertIsNone(ledger['first_invalidation'])
        latest = ledger['fnm']['latest']
        self.assertEqual(latest['disks']['monitor_source']['result'], 'returned')
        self.assertEqual(latest['disks']['monitor_source']['measured_count'], 3)
        self.assertEqual(latest['disks']['counts'].get('matched', 0), 0)
        self.assertEqual(latest['disks']['counts']['unknown'], 4)
        self.assertEqual(latest['summary']['counts']['out_of_scope'], 4)
        self.assertFalse(latest['summary']['summary_has_value'])

    def test_summary_early_empty_path_does_not_borrow_an_earlier_basic_observation(self):
        self.overview()
        self.case.panel._mount_cache = (self.case.now, {})
        overview = self.overview()
        self.assertIsNone(overview['disk_total'])
        ledger = self.diagnostic_without_replay()
        summary = ledger['fnm']['latest']['summary']
        self.assertEqual(ledger['state']['positive'], 3)
        self.assertFalse(summary['summary_has_value'])
        self.assertNotIn('basic', summary['children'])
        self.assertEqual(summary['monitor_source']['status'], 'not_observed')

    def test_real_cache_invalidation_is_distinct_from_projection_only_failures(self):
        self.overview()
        mountinfo = self.case.proc / 'self/mountinfo'
        text = mountinfo.read_text(encoding='utf-8')
        mountinfo.write_text(text.replace('/drives/fixture-sdb rw -',
                                         '/drives/fixture-sdb ro -'), encoding='utf-8')
        overview = self.overview()
        self.assertTrue(all(row['total'] is None for row in overview['disks_detail']))
        ledger = self.diagnostic_without_replay()
        self.assertEqual(ledger['state']['positive'], 0)
        self.assertEqual(ledger['first_invalidation']['reason'], 'active_keys_changed')
        self.assertEqual(ledger['first_invalidation']['positive_before'], 3)
        self.assertEqual(ledger['monitor']['projection']['latest']['result'], 'returned')
        self.assertEqual(ledger['fnm']['latest']['disks']['monitor_source']['measured_count'], 0)

    def test_observer_exceptions_cannot_change_success_or_original_failure(self):
        expected = self.overview()
        panel = self.case.panel
        for method in ('_capacity_projection_begin', '_capacity_projection_hint',
                       '_capacity_projection_end'):
            with self.subTest(observer=method), mock.patch.object(panel, method,
                    side_effect=RuntimeError(PRIVATE)):
                self.assertEqual(self.overview(), expected)
        with mock.patch.object(self.case.monitor, 'capacity_projection_observation',
                               side_effect=RuntimeError(PRIVATE)):
            self.assertEqual(self.overview(), expected)
        original_error = RuntimeError('original-projection-error')
        with mock.patch.object(panel, '_mounts', side_effect=original_error), \
                mock.patch.object(panel, '_capacity_projection_end', side_effect=RuntimeError(PRIVATE)):
            with self.assertRaises(RuntimeError) as caught:
                panel._disks()
        self.assertIs(caught.exception, original_error)

    def test_concurrent_basic_calls_keep_their_exact_return_monitor_sequences(self):
        monitor, panel = self.case.monitor, self.case.panel
        first_captured, second_finished = threading.Event(), threading.Event()
        original = monitor.snapshot
        expected = {}
        expected_lock = threading.Lock()

        def snapshot():
            is_first = threading.current_thread().name.endswith('_0')
            if not is_first:
                self.assertTrue(first_captured.wait(5))
            value = original()
            source = monitor.capacity_projection_observation(value)
            frame = next(frame for frame in panel._capacity_projection_local.stack
                         if frame['stage'] == 'basic')
            with expected_lock:
                expected[frame['operation_id']] = source['sequence']
            if is_first:
                first_captured.set()
                self.assertTrue(second_finished.wait(5))
            else:
                second_finished.set()
            return value

        before = panel.capacity_projection_diagnostics()['sequence']
        with mock.patch.object(monitor, 'snapshot', side_effect=snapshot), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2,
                    thread_name_prefix='capacity_projection_http') as pool:
            futures = [pool.submit(panel._basic) for _ in range(2)]
            values = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(len(value['disks']) == 4 for value in values))
        events = [event for event in panel.capacity_projection_diagnostics()['ring']
                  if event['stage'] == 'basic' and event['sequence'] > before]
        self.assertEqual(len(events), 2)
        self.assertEqual(len(set(expected.values())), 2)
        for event in events:
            self.assertEqual(event['monitor_source']['status'], 'observed')
            self.assertEqual(event['monitor_source']['sequence'], expected[event['operation_id']])
        self.diagnostic_without_replay()

    def test_http_size_and_nonfinite_fallbacks_keep_private_evidence_in_memory(self):
        limit = 4 * 1024 * 1024
        first = {'reason': 'watch_mount_event', 'private': PRIVATE + ('x' * limit)}
        source = {'schema_version': 1, 'first_invalidation': first,
                  'state': {'positive': 0}, 'events': []}
        original_first = copy.deepcopy(first)
        provided = []

        def read():
            value = copy.deepcopy(source)
            provided.append(value)
            return value

        with mock.patch.object(self.case.monitor, 'capacity_cache_diagnostics', side_effect=read):
            status, _, value = self.request(BODY)
        self.assertEqual(status, 200)
        check = value['snapshot']['checks']['capacity_cache']
        self.assertTrue(check['ok'])
        limited = check['data']['capacity_cache']
        self.assertEqual(limited['status'], 'size_exceeded')
        self.assertEqual(limited['reason'], 'diagnostic_size_exceeded')
        self.assertIs(limited['evidence_returned'], False)
        self.assertIs(limited['truncated'], True)
        self.assertIs(limited['first_invalidation_retained_in_memory'], True)
        self.assertEqual(limited['response_limit_bytes'], limit)
        encoded = json.dumps(provided[0], ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.assertEqual(limited['evidence_encoded_bytes'], len(encoded))
        self.assertGreater(limited['evidence_encoded_bytes'], limit)
        self.assertEqual(limited['evidence_sha256'], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(source['first_invalidation'], original_first)
        self.assertEqual(provided[0]['first_invalidation'], original_first)
        self.assertIn('fnm', provided[0])
        self.assertNotIn('fnm', source)
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertLess(len(json.dumps(value).encode('utf-8')), limit)

        # Unsupported numeric values must produce a fixed envelope, not Python
        # JSON's nonstandard NaN/Infinity or exception text from the provider.
        for number in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(number=str(number)):
                source['floating'] = number
                with mock.patch.object(self.case.monitor, 'capacity_cache_diagnostics', side_effect=read):
                    status, _, value = self.request(BODY)
                self.assertEqual(status, 200)
                limited = value['snapshot']['checks']['capacity_cache']['data']['capacity_cache']
                self.assertEqual(limited['status'], 'encoding_unavailable')
                self.assertEqual(limited['reason'], 'diagnostic_encoding_unavailable')
                self.assertIs(limited['evidence_returned'], False)
                self.assertIs(limited['truncated'], False)
                self.assertIs(limited['first_invalidation_retained_in_memory'], True)
                self.assertEqual(limited['response_limit_bytes'], limit)
                self.assertNotIn('evidence_sha256', limited)
                self.assertNotIn(PRIVATE, json.dumps(value, allow_nan=False))
                self.assertFalse(math.isfinite(source['floating']))
                self.assertEqual(source['first_invalidation'], original_first)
                self.assertEqual(provided[-1]['first_invalidation'], original_first)

    def test_serialization_revocation_discards_even_the_oversize_envelope(self):
        source = {'schema_version': 1, 'first_invalidation': {
            'reason': 'watch_mount_event', 'private': PRIVATE + ('x' * (4 * 1024 * 1024))}}
        original_first = copy.deepcopy(source['first_invalidation'])
        original_dump = web_config.json.dumps
        encoded = []

        def expire(value, *args, **kwargs):
            result = original_dump(value, *args, **kwargs)
            if isinstance(value, dict) and 'snapshot' in value:
                envelope = value['snapshot']['checks']['capacity_cache']['data']['capacity_cache']
                self.assertEqual(envelope['status'], 'size_exceeded')
                self.assertIs(envelope['evidence_returned'], False)
                encoded.append(True)
                self.server.dev_until = web_config.time.time() - 1
            return result

        with mock.patch.object(self.case.monitor, 'capacity_cache_diagnostics',
                               side_effect=lambda: copy.deepcopy(source)), \
                mock.patch.object(web_config.json, 'dumps', side_effect=expire):
            status, _, value = self.request(BODY)
        self.assertEqual(encoded, [True])
        self.assertEqual(status, 403)
        self.assertEqual(value['code'], 'development_authorization_expired')
        self.assertNotIn('snapshot', value)
        self.assertNotIn('first_invalidation', json.dumps(value))
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(source['first_invalidation'], original_first)


if __name__ == '__main__':
    unittest.main()
