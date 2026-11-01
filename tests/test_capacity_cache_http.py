"""The explicit capacity ledger grant never becomes a disk-query capability."""

import copy
import contextlib
import ipaddress
import json
import time
import unittest
from unittest import mock

# Reuse the existing real HTTP fixture's module as well as its setup: isolated
# loaders elsewhere may replace sys.modules['web_config'] during discovery.
import test_dev_diagnostics_http as diagnostics_http

web_config = diagnostics_http.web_config
BODY = {'action': 'snapshot', 'checks': ['capacity_cache']}


class CapacityCacheHTTPTests(unittest.TestCase):
    request = diagnostics_http.DeveloperDiagnosticsHTTPTests.request
    _patch = diagnostics_http.DeveloperDiagnosticsHTTPTests._patch
    assert_no_sources = diagnostics_http.DeveloperDiagnosticsHTTPTests.assert_no_sources
    assert_no_controls = diagnostics_http.DeveloperDiagnosticsHTTPTests.assert_no_controls

    def setUp(self):
        diagnostics_http.DeveloperDiagnosticsHTTPTests.setUp(self)
        self.monitor = mock.Mock()
        self.record = {'schema_version': 1, 'source': 'memory_decision_ledger',
                       'instance': 'fixture', 'sequence': 7,
                       'first_invalidation': {'reason': 'mount_event',
                                              'ctime_ns': '1793503350123456789'}}
        self.monitor.capacity_cache_diagnostics.side_effect = lambda: copy.deepcopy(self.record)
        self.monitor.snapshot.side_effect = AssertionError('snapshot would rescan mounts')
        self.monitor.sampler.capacity_snapshot.side_effect = AssertionError('disk rescan')
        self.monitor.sampler.sample.side_effect = AssertionError('sampler read')
        self.server.monitor = self.monitor

    def assert_only_ledger_read(self, count=1):
        self.assertEqual(self.monitor.capacity_cache_diagnostics.call_count, count)
        self.monitor.snapshot.assert_not_called()
        self.monitor.sampler.capacity_snapshot.assert_not_called()
        self.monitor.sampler.sample.assert_not_called()
        self.assert_no_sources()

    def assert_evidence_envelope(self, actual, original):
        # The projection extension adds fixed RAM metadata; every original
        # ledger field must still be identical, including the first loss.
        self.assertEqual({key: actual[key] for key in original}, original)
        self.assertEqual(set(actual) - set(original), {'fnm', 'status', 'evidence_returned',
            'truncated', 'response_limit_bytes', 'evidence_encoded_bytes'})
        self.assertEqual(actual['status'], 'complete')
        self.assertTrue(actual['evidence_returned'])
        self.assertFalse(actual['truncated'])
        self.assertEqual(actual['response_limit_bytes'], 4 * 1024 * 1024)
        self.assertLess(actual['evidence_encoded_bytes'], actual['response_limit_bytes'])
        self.assertEqual(actual['fnm']['schema_version'], 1)
        self.assertEqual(actual['fnm']['source'], 'fnm_normal_consumption')

    def test_explicit_selection_only_reads_memory_without_owner_or_command_permission(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        status, headers, value = self.request(BODY)
        self.assertEqual(status, 200)
        row = value['snapshot']['checks']['capacity_cache']
        self.assertEqual(row['http_status'], 200)
        self.assertTrue(row['ok'])
        self.assert_evidence_envelope(row['data']['capacity_cache'], self.record)
        self.assertIsInstance(row['data']['capacity_cache']['first_invalidation']['ctime_ns'], str)
        self.assertNotIn('set-cookie', headers)
        self.assert_only_ledger_read()

    def test_catalog_adds_opt_in_check_without_expanding_original_default_four(self):
        _, _, catalog = self.request({'action': 'catalog'})
        self.assertIn('capacity_cache', {row['id'] for row in catalog['catalog']['checks']})
        _, _, value = self.request({'action': 'snapshot'})
        self.assertEqual(set(value['snapshot']['checks']), diagnostics_http.CHECKS)
        self.monitor.capacity_cache_diagnostics.assert_not_called()

    def test_owner_cookie_or_forwarded_address_never_replaces_actual_peer_development_grant(self):
        self.server.dev_allow_ips = {ipaddress.ip_address('192.0.2.55')}
        for headers in ({}, {'Cookie': self.cookie},
                        {'Cookie': self.cookie, 'X-Forwarded-For': '192.0.2.55',
                         'Forwarded': 'for=192.0.2.55'}):
            with self.subTest(headers=tuple(headers)):
                self.assertEqual(self.request(BODY, headers=headers)[0], 403)
        self.assert_only_ledger_read(0)

    def test_disabled_expired_empty_whitelist_or_password_owner_alone_cannot_read(self):
        cases = (('dev_no_pairing', False), ('dev_until', time.time() - 1),
                 ('dev_allow_ips', set()))
        for field, value in cases:
            with self.subTest(field=field):
                original = getattr(self.server, field)
                setattr(self.server, field, value)
                try:
                    self.assertEqual(self.request(BODY, headers={'Cookie': self.cookie})[0], 403)
                finally:
                    setattr(self.server, field, original)
        self.assert_only_ledger_read(0)

    def test_small_screen_token_and_owner_session_do_not_grant_development_access(self):
        self.server.dev_no_pairing = False
        for headers in ({'X-R1-Control': 'fixture-local-token'},
                        {'Cookie': self.cookie, 'X-R1-Control': 'fixture-local-token'}):
            with self.subTest(headers=tuple(headers)):
                self.assertEqual(self.request(BODY, headers=headers)[0], 403)
        self.assert_only_ledger_read(0)

    def test_bearer_even_with_owner_or_development_permission_is_explicitly_rejected(self):
        for authorization in ('Bearer fixture-token', '', 'Basic fixture-token'):
            with self.subTest(scheme=authorization.split(' ')[0]):
                self.assertEqual(self.request(BODY, headers={
                    'Authorization': authorization, 'Cookie': self.cookie})[0], 403)
        self.assert_only_ledger_read(0)

    def test_json_origin_csrf_and_strict_fixed_body_are_required(self):
        for kwargs, expected in (({'remove': ('Origin',)}, 403),
                                 ({'remove': ('X-FNM-Request',)}, 403),
                                 ({'headers': {'Origin': 'http://foreign.invalid'}}, 403),
                                 ({'headers': {'Content-Type': 'text/plain'}}, 400)):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(BODY, **kwargs)[0], expected)
        invalid = [None, [], {**BODY, 'force': False}, {**BODY, 'path': '/private/canary'},
                   {**BODY, 'device': 'sda'}, {**BODY, 'pid': 1},
                   {**BODY, 'checks': ['capacity_cache', 'capacity_cache']},
                   {**BODY, 'action': 'refresh'},
                   b'{"action":"snapshot","checks":["capacity_cache"],"checks":["capacity_cache"]}']
        for body in invalid:
            with self.subTest(body=body):
                status, _, value = self.request(body)
                self.assertEqual(status, 400)
                self.assertNotIn('/private/canary', json.dumps(value))
        for suffix in ('?force=1', '#capacity_cache', '?', '#'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(BODY, path=diagnostics_http.ROUTE + suffix)[0], 400)
        self.assert_only_ledger_read(0)

    def test_get_is_not_a_diagnostic_entry_and_password_gate_remains(self):
        self.assertEqual(self.request(method='GET')[0], 401)
        self.assertEqual(self.request(method='GET', headers={'Cookie': self.cookie})[0], 401)
        self.assert_only_ledger_read(0)

    def test_global_busy_is_not_queued_or_retried_and_releases_after_use(self):
        with self.server.diagnostic_lock:
            self.assertEqual(self.request(BODY)[0], 409)
        self.assert_only_ledger_read(0)
        self.assertEqual(self.request(BODY)[0], 200)
        self.assert_only_ledger_read()
        self.assertFalse(self.server.diagnostic_lock.locked())

    def test_storage_source_busy_is_reported_per_check_without_read_or_retry(self):
        with self.server.storage_diagnostic_lock:
            status, _, value = self.request(BODY)
        self.assertEqual(status, 200)
        self.assertFalse(value['snapshot']['all_checks_completed'])
        self.assertEqual(value['snapshot']['checks']['capacity_cache']['http_status'], 409)
        self.assertIsNone(value['snapshot']['checks']['capacity_cache']['data'])
        self.assert_only_ledger_read(0)

    def test_accessor_errors_and_absence_are_safe_and_do_not_keep_locks(self):
        for result in (RuntimeError('/private/path token=secret-canary'), None, []):
            with self.subTest(kind=type(result).__name__):
                self.monitor.capacity_cache_diagnostics.side_effect = (
                    result if isinstance(result, Exception) else None)
                self.monitor.capacity_cache_diagnostics.return_value = result
                status, _, value = self.request(BODY)
                self.assertEqual(status, 200)
                self.assertEqual(value['snapshot']['checks']['capacity_cache']['http_status'], 503)
                self.assertNotIn('secret-canary', json.dumps(value))
                self.assertNotIn('/private/path', json.dumps(value))
                self.assertFalse(self.server.storage_diagnostic_lock.locked())
                self.assertFalse(self.server.diagnostic_lock.locked())
        self.server.monitor = None
        self.assertEqual(self.request(BODY)[2]['snapshot']['checks']['capacity_cache']['http_status'], 503)

    def test_expiry_after_memory_copy_discards_entire_round_and_stops_later_sources(self):
        def expire():
            self.server.dev_until = time.time() - 1
            return {'schema_version': 1, 'marker': 'discarded-ledger-canary'}
        self.monitor.capacity_cache_diagnostics.side_effect = expire
        status, _, value = self.request({'action': 'snapshot', 'checks': ['capacity_cache', 'device']})
        self.assertEqual(status, 403)
        self.assertNotIn('discarded-ledger-canary', json.dumps(value))
        self.assertNotIn('snapshot', value)
        self.assert_only_ledger_read()

    def test_serialization_expiry_discards_encoded_private_round(self):
        original = web_config.json.dumps
        encoded = []

        def expire(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(value, dict) and 'snapshot' in value:
                encoded.append(True)
                self.server.dev_until = time.time() - 1
            return result

        with mock.patch.object(web_config.json, 'dumps', side_effect=expire):
            status, _, value = self.request(BODY)
        self.assertEqual(encoded, [True])
        self.assertEqual(status, 403)
        self.assertEqual(value['code'], 'development_authorization_expired')
        self.assertNotIn('snapshot', value)
        self.assertNotIn('first_invalidation', json.dumps(value))
        self.assert_only_ledger_read()

    def test_revocation_is_latched_even_when_window_is_restored_during_encoding(self):
        original_dump, original_factory = web_config.json.dumps, self.server._media_debug_authorizer
        guards = []

        def capture(peer, *, generation=None):
            current = original_factory(peer, generation=generation)
            guards.append(current)
            return current

        def revoke_restore(value, *args, **kwargs):
            result = original_dump(value, *args, **kwargs)
            if isinstance(value, dict) and 'snapshot' in value:
                self.server.dev_until = time.time() - 1
                self.assertFalse(guards[0]())
                self.server.dev_until = time.time() + 60
                self.assertTrue(self.server._dev_authorized('127.0.0.1'))
                self.assertFalse(guards[0]())
            return result

        with mock.patch.object(self.server, '_media_debug_authorizer', side_effect=capture), \
                mock.patch.object(web_config.json, 'dumps', side_effect=revoke_restore):
            status, _, value = self.request(BODY)
        self.assertEqual(status, 403)
        self.assertEqual(value['code'], 'development_authorization_expired')
        self.assertNotIn('snapshot', value)
        self.assertEqual(len(guards), 1)
        self.assertFalse(guards[0]())
        self.assert_only_ledger_read()

    def test_stop_start_during_encoding_cannot_revive_old_generation(self):
        original = web_config.json.dumps
        restarted = []

        def restart(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(value, dict) and 'snapshot' in value:
                self.server.stop()
                self.server.start()
                restarted.append(True)
            return result

        with mock.patch.object(web_config.json, 'dumps', side_effect=restart):
            status, _, value = self.request(BODY)
        self.assertEqual(restarted, [True])
        self.assertEqual(status, 403)
        self.assertNotIn('snapshot', value)
        self.assertEqual(value['code'], 'development_authorization_expired')
        self.assert_only_ledger_read()

    def test_original_absolute_window_is_not_extended_mid_response(self):
        self.server.dev_until = time.time() + 30
        original = web_config.json.dumps
        expiry = self.server.dev_until

        def extend(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(value, dict) and 'snapshot' in value:
                self.server.dev_until = expiry + 300
            return result

        # Only the guard's wall-clock calls move past its captured window; do not
        # change collection data, sample clocks, or the HTTP client's timeout.
        with mock.patch.object(web_config.json, 'dumps', side_effect=extend):
            def project():
                self.server.dev_until = expiry + 300
                with mock.patch.object(web_config.time, 'time', return_value=expiry + 1):
                    self.assertFalse(self._current_guard())
                return copy.deepcopy(self.record)
            factory = self.server._media_debug_authorizer
            def capture(peer, *, generation=None):
                guard = factory(peer, generation=generation)
                self._current_guard = guard
                return guard
            self.monitor.capacity_cache_diagnostics.side_effect = project
            with mock.patch.object(self.server, '_media_debug_authorizer', side_effect=capture):
                status, _, value = self.request(BODY)
        self.assertEqual(status, 403)
        self.assertNotIn('snapshot', value)

    def test_real_smart_sampler_monitor_fnm_http_keeps_first_loss_without_replaying_checks(self):
        import test_smart_power_compat as power_fixture

        case = power_fixture.SmartPowerCompatTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.ordinary_mix()
        smart, capacity, _, initial = case.refresh_visible()
        self.assertEqual(next(row['status'] for row in smart['results'] if row['device'] == 'sda'),
                         'skipped_standby')
        self.assertEqual(sum(row['status'] == 'refreshed' for row in capacity['results']), 3)
        sampled = {row['name']: row['capacity_sampled_at'] for row in initial['disks_detail']}

        # Both HTTP routes below use the actual StatusMonitor and actual Fnm
        # panel. Only the fixture's external SMART runner, namespace watch and
        # capacity worker seams are substituted; no panel JSON is manufactured.
        self.server.monitor, self.server.panel = case.monitor, case.panel
        case.panel.web = self.server
        status, _, baseline = self.request(method='GET', path='/api/overview',
                                           headers={'Cookie': self.cookie})
        self.assertEqual(status, 200)
        for name in ('sdb', 'sdc', 'sdd'):
            disk = next(row for row in baseline['disks_detail'] if row['name'] == name)
            self.assertEqual(disk['percent'], 25)
            self.assertEqual(disk['capacity_sampled_at'], sampled[name])
        baseline_ledger = case.monitor.capacity_cache_diagnostics()
        self.assertEqual(baseline_ledger['state']['positive'], 3)
        self.assertIsNone(baseline_ledger['first_invalidation'])

        # Keep IDs, target counts and device generations unchanged. Changing a
        # real parsed mount option still changes the complete production key;
        # the first passive overview must retain that exact loss provenance.
        mountinfo = case.proc / 'self/mountinfo'
        before = mountinfo.read_text(encoding='utf-8')
        self.assertIn('/drives/fixture-sdb rw -', before)
        mountinfo.write_text(before.replace('/drives/fixture-sdb rw -',
                                           '/drives/fixture-sdb ro -'), encoding='utf-8')
        status, _, empty = self.request(method='GET', path='/api/overview',
                                        headers={'Cookie': self.cookie})
        self.assertEqual(status, 200)
        self.assertTrue(all(row['total'] is None for row in empty['disks_detail']))
        lost = case.monitor.capacity_cache_diagnostics()
        first = copy.deepcopy(lost['first_invalidation'])
        self.assertEqual(first['reason'], 'active_keys_changed')
        self.assertEqual(first['positive_before'], 3)
        self.assertEqual(first['counts']['positive'], 0)
        self.assertEqual(first['changed_mount_ids'], [12, 13, 14])
        self.assertEqual({row['sampled_at'] for row in first['before']['records']},
                         {sampled[name] for name in ('sdb', 'sdc', 'sdd')})
        self.assertTrue(first['component_changes']['complete'])
        changed = next(row for row in first['component_changes']['mounts'] if row['mount_id'] == 12)
        self.assertTrue(changed['changed']['mount_options'])
        self.assertTrue(changed['changed']['mountline'])

        for _ in range(40):
            self.assertEqual(self.request(method='GET', path='/api/overview',
                                          headers={'Cookie': self.cookie})[0], 200)
        before_read = case.monitor.capacity_cache_diagnostics()
        self.assertGreater(before_read['dropped_events'], 0)
        self.assertEqual(before_read['first_invalidation'], first)
        self.assertEqual(before_read['generation'], baseline_ledger['generation'])
        smart_calls, capacity_calls = copy.deepcopy(case.calls), list(case.capacity_calls)

        # A read of past decisions must succeed with every production collection
        # seam booby-trapped, including the poll already consumed by overview.
        with contextlib.ExitStack() as stack:
            for target, name in ((case.monitor, 'snapshot'), (case.sampler, 'capacity_snapshot'),
                                 (case.sampler, 'sample'), (case.sampler, '_capacity_probe'),
                                 (case.collector, 'snapshot'), (case.collector, 'runner'),
                                 (power_fixture.service, '_capacity_inventory'),
                                 (power_fixture.service, '_capacity_read'),
                                 (case.watch, 'changed'), (power_fixture.service.subprocess, 'Popen')):
                stack.enter_context(mock.patch.object(target, name,
                    side_effect=AssertionError('diagnostic must not collect: ' + name)))
            stack.enter_context(mock.patch.object(power_fixture.service.os, 'statvfs', create=True,
                side_effect=AssertionError('diagnostic must not statvfs')))
            status, _, value = self.request(BODY)
        self.assertEqual(status, 200)
        check = value['snapshot']['checks']['capacity_cache']
        self.assertTrue(check['ok'])
        ledger = check['data']['capacity_cache']
        self.assert_evidence_envelope(ledger, before_read)
        self.assertEqual(ledger['first_invalidation'], first)
        self.assertFalse(ledger['monitor']['atomic_with_capacity'])
        self.assertEqual(ledger['monitor']['read_order'], ['monitor', 'capacity'])
        self.assertEqual(case.calls, smart_calls)
        self.assertEqual(case.capacity_calls, capacity_calls)
        serialized = json.dumps(value)
        for marker in ('/drives/fixture-', '/dev/sdb1', power_fixture.smart_fixture.PRIVATE):
            self.assertNotIn(marker, serialized)


if __name__ == '__main__':
    unittest.main()
