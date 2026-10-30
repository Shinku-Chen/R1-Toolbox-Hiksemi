"""Cached monitoring projections and real MCP/REST permission boundaries."""

import copy
import json
from pathlib import Path
import sys
import time
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_monitoring as api


PRIVATE = 'private-serial-token-or-path'


def monitor():
    source = mock.Mock()
    source.history.return_value = {'available': True, 'range': '1h', 'aggregation': 'last',
        'recording': True, 'sample_interval_seconds': 30, 'reason': PRIVATE,
        'series': [{'id': 'cpu_usage_percent', 'label': PRIVATE, 'unit': PRIVATE,
                    'points': [[100, 0], [130, None], [160, 20]]},
                   {'id': PRIVATE, 'points': [[100, 99]]}]}
    source.alerts.return_value = {'active': [], 'events': [], 'smart_observations': [PRIVATE]}
    source.snapshot.return_value = {'current': {'sampled_at': time.time()},
        'summary': {'status': 'ok', 'known_metrics': 11, 'unknown_metrics': 0}, 'smart': [PRIVATE]}
    source.health_events.side_effect = lambda: copy.deepcopy({
        'alerts': source.alerts.return_value, 'state': source.snapshot.return_value})
    source.traffic.return_value = {'available': True, 'partial': True,
        'periods': {'day': '2026-10-30', 'month': '2026-10'}, 'reason': PRIVATE,
        'interfaces': [{'name': 'eth0', 'today': {'rx_bytes': 0, 'tx_bytes': None}, 'month': None}],
        'totals': {'today': {'rx_bytes': 0, 'tx_bytes': None}, 'month': None}}
    return source


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.source = monitor()

    def call(self, tool='get_monitoring_history', arguments=None):
        return api.execute(self.source, tool, {'metric': 'cpu_usage_percent'} if arguments is None else arguments)

    def test_history_uses_only_one_memory_cache_and_preserves_null_and_real_zero(self):
        value = self.call(arguments={'metric': 'cpu_usage_percent', 'limit': 2})
        self.assertTrue(value['ok'])
        self.assertEqual(value['points'], [[130, None], [160, 20]])
        self.assertEqual((value['known_points'], value['missing_points']), (1, 1))
        self.assertTrue(value['truncated'])
        self.assertEqual(value['sampling_continuity'], 'not_asserted')
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(self.source.mock_calls, [mock.call.history('1h')])
        self.assertEqual(self.call()['points'][0], [100, 0])

    def test_history_empty_missing_and_unknown_values_never_become_zero(self):
        self.source.history.return_value['series'][0]['points'] = [[100, False], [130, -1], [160, 101]]
        value = self.call()
        self.assertEqual(value['points'], [[100, None], [130, None], [160, None]])
        self.assertFalse(value['projection_complete'])
        self.assertEqual(value['known_points'], 0)
        missing = self.call(arguments={'metric': 'cpu_package_w'})
        self.assertFalse(missing['available'])
        self.assertEqual(missing['points'], [])

    def test_history_accepts_only_fixed_ranges_metrics_bounds_and_strict_timestamps(self):
        for arguments in ({'metric': PRIVATE}, {'metric': 'cpu_usage_percent', 'range': '../private'},
                          {'metric': 'cpu_usage_percent', 'limit': True},
                          {'metric': 'cpu_usage_percent', 'limit': 721},
                          {'metric': 'cpu_usage_percent', 'path': PRIVATE}):
            self.assertEqual(self.call(arguments=arguments)['error']['code'], 'invalid_arguments')
        self.source.history.assert_not_called()
        for points in ([[1, 3], [1, 4]], [[True, 1]], [[2, 1], [1, 1]], [[1, 1]] * 721):
            self.source.history.return_value['series'][0]['points'] = points
            self.assertEqual(self.call()['error']['code'], 'invalid_cache')

    def test_history_duplicate_metric_and_bad_cache_fail_closed_without_raw_error(self):
        raw = self.source.history.return_value
        raw['series'].append(copy.deepcopy(raw['series'][0]))
        self.assertEqual(self.call()['error']['code'], 'invalid_cache')
        self.source.history.side_effect = OSError(PRIVATE)
        self.assertNotIn(PRIVATE, json.dumps(self.call()))

    def test_health_requires_fresh_complete_known_metrics_before_ok(self):
        self.assertEqual(self.call('get_health_events', {})['assessment'], 'ok')
        for change in ({'known_metrics': 0}, {'unknown_metrics': 1}, {'unknown_metrics': None}):
            with self.subTest(change=change):
                self.source.snapshot.return_value['summary'] = {'status': 'ok', 'known_metrics': 11, 'unknown_metrics': 0, **change}
                result = self.call('get_health_events', {})
                self.assertEqual(result['assessment'], 'unknown')
                self.assertFalse(result['assessment_complete'])
        self.source.snapshot.return_value['summary'].update(status='critical')
        self.assertEqual(self.call('get_health_events', {})['assessment'], 'critical')

    def test_health_uses_one_atomic_cache_generation_without_separate_accessors(self):
        from monitoring import MonitoringService
        live = object.__new__(MonitoringService)
        live.lock = threading.RLock()
        live.alerts_cache = self.source.alerts.return_value
        live.summary = self.source.snapshot.return_value['summary']
        live.current = self.source.snapshot.return_value['current']
        # The real accessor copies both structures in a single critical section.
        value = live.health_events()
        live.summary['status'] = 'critical'
        live.alerts_cache['active'].append({'private': PRIVATE})
        self.assertEqual(value['state']['summary']['status'], 'ok')
        self.assertEqual(value['alerts']['active'], [])
        self.source.health_events.return_value = value
        self.source.health_events.side_effect = None
        self.assertEqual(self.call('get_health_events', {})['assessment'], 'ok')
        self.source.alerts.assert_not_called()
        self.source.snapshot.assert_not_called()

    def test_health_stale_future_or_missing_sample_never_claims_healthy(self):
        for stamp in (None, time.time() - 91, time.time() + 300, True):
            self.source.snapshot.return_value['current']['sampled_at'] = stamp
            value = self.call('get_health_events', {})
            self.assertEqual(value['assessment'], 'unknown')
            self.assertFalse(value['fresh'])

    def test_health_smart_identity_labels_private_fields_and_raw_observations_stay_private(self):
        raw = {'id': 'smart:' + PRIVATE, 'label': PRIVATE, 'timestamp': time.time(),
               'level': 'critical', 'value': None, 'kind': 'raised', 'path': PRIVATE}
        self.source.alerts.return_value.update(active=[raw], events=[raw])
        value = self.call('get_health_events', {})
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(value['active'][0]['label'], '磁盘 SMART 告警')
        self.assertEqual(value['active'][0]['id'], value['events'][0]['id'])
        self.assertNotIn('smart_observations', value)

    def test_health_filters_events_and_bounds_results_and_does_not_hide_invalid_rows(self):
        row = {'id': 'cpu_load', 'timestamp': 120, 'level': 'warning', 'kind': 'raised', 'value': 90, 'unit': '%'}
        self.source.alerts.return_value.update(active=[{'id': PRIVATE}], events=[row, dict(row, timestamp=140)])
        value = self.call('get_health_events', {'since': 130, 'limit': 1})
        self.assertEqual(len(value['events']), 1)
        self.assertEqual(value['events'][0]['timestamp'], 140)
        self.assertEqual(value['assessment'], 'unknown')
        self.assertFalse(value['complete'])
        for args in ({'limit': True}, {'since': False}, {'since': -1}, {'source': PRIVATE}):
            self.assertEqual(self.call('get_health_events', args)['error']['code'], 'invalid_arguments')

    def test_traffic_keeps_observed_totals_separate_from_interfaces_and_missing_periods(self):
        value = self.call('get_network_traffic_totals', {})
        self.assertEqual(value['totals']['today'], {'rx_bytes': 0, 'tx_bytes': None})
        self.assertIsNone(value['totals']['month'])
        self.assertTrue(value['partial'])
        self.assertEqual(value['coverage'], 'observed_counter_deltas')
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(self.source.mock_calls, [mock.call.traffic()])

    def test_traffic_rejects_duplicates_extra_inputs_and_malformed_counters(self):
        row = self.source.traffic.return_value['interfaces'][0]
        row['today'] = {'rx_bytes': True, 'tx_bytes': 2 ** 64}
        value = self.call('get_network_traffic_totals', {})
        self.assertEqual(value['interfaces'][0]['today'], {'rx_bytes': None, 'tx_bytes': None})
        self.source.traffic.return_value['interfaces'].append(copy.deepcopy(row))
        self.assertEqual(self.call('get_network_traffic_totals', {})['error']['code'], 'invalid_cache')
        self.assertEqual(self.call('get_network_traffic_totals', {'url': PRIVATE})['error']['code'], 'invalid_arguments')

    def test_missing_provider_or_exception_reports_unavailable_and_does_not_sample(self):
        self.assertEqual(api.execute(None, 'get_health_events', {})['error']['code'], 'monitoring_unavailable')
        self.source.traffic.side_effect = RuntimeError(PRIVATE)
        self.assertNotIn(PRIVATE, json.dumps(self.call('get_network_traffic_totals', {})))
        self.source.sample_once.assert_not_called()
        self.source._refresh_history.assert_not_called()
        self.source._open_db.assert_not_called()


def _http_fixture_method(name):
    """Resolve after discovery: the panel tests explicitly reload web_config."""
    def invoke(self, *args, **kwargs):
        from test_integration_http import IntegrationHTTPTests
        return getattr(IntegrationHTTPTests, name)(self, *args, **kwargs)
    return invoke


class MonitoringHTTPTests(unittest.TestCase):
    setUp = _http_fixture_method('setUp')
    _patch = _http_fixture_method('_patch')
    request = _http_fixture_method('request')
    rpc = _http_fixture_method('rpc')
    rest_action = _http_fixture_method('rest_action')
    assert_no_actions = _http_fixture_method('assert_no_actions')

    @staticmethod
    def message(*args, **kwargs):
        from test_integration_http import IntegrationHTTPTests
        return IntegrationHTTPTests.message(*args, **kwargs)

    def key(self, *, tools=None, explicit=True):
        self.server.api_access.configure({'enabled': True})
        body = {'name': 'cached monitoring', 'role': 'readonly', 'scopes': ['read'], 'allowed_ips': [], 'expires_days': 1}
        if explicit:
            body['tool_grants'] = sorted(api.TOOLS) if tools is None else tools
        return self.server.api_access.create_key(body)

    def test_real_mcp_and_rest_list_call_only_selected_tools_and_never_collect(self):
        self.server.monitoring = monitor()
        key = self.key()
        self.assertEqual({row['name'] for row in self.rpc(key)[2]['result']['tools']}, api.TOOLS)
        for tool in sorted(api.TOOLS):
            args = {'metric': 'cpu_usage_percent'} if tool == 'get_monitoring_history' else {}
            for call in (lambda: self.rpc(key, self.message('tools/call', {'name': tool, 'arguments': args}))[2]['result'],
                         lambda: self.rest_action(key, tool, args)[2]):
                value = call()
                self.assertFalse(value['isError'], value)
                self.assertNotIn(PRIVATE, json.dumps(value))
        self.server.monitoring.sample_once.assert_not_called()
        self.server.monitoring._refresh_history.assert_not_called()
        self.assert_no_actions()

    def test_old_keys_and_unselected_tools_do_not_read_any_monitoring_cache(self):
        self.server.monitoring = monitor()
        for key in (self.key(explicit=False), self.key(tools=[])):
            for tool in sorted(api.TOOLS):
                args = {'metric': 'cpu_usage_percent'} if tool == 'get_monitoring_history' else {}
                value = self.rest_action(key, tool, args)[2]
                self.assertTrue(value['isError'])
        self.assertFalse(self.server.monitoring.mock_calls)

    def test_revocation_rotation_disable_and_service_stop_during_read_discard_results(self):
        for transport in ('mcp', 'rest'):
            for change in ('revoke', 'rotate', 'disable', 'stop'):
                with self.subTest(transport=transport, change=change):
                    self.server.integration.open()
                    self.server.monitoring = monitor()
                    key = self.key()
                    raw = self.server.monitoring.history.return_value
                    def read(unused):
                        if change == 'revoke':
                            self.server.api_access.revoke_key(key['id'])
                        elif change == 'rotate':
                            self.server.api_access.rotate_key(key['id'])
                        elif change == 'disable':
                            self.server.api_access.set_key_enabled(key['id'], False)
                        else:
                            self.server.integration.close()
                        return raw
                    self.server.monitoring.history.side_effect = read
                    args = {'metric': 'cpu_usage_percent'}
                    value = (self.rpc(key, self.message('tools/call', {'name': 'get_monitoring_history', 'arguments': args}))[2]['result']
                             if transport == 'mcp' else self.rest_action(key, 'get_monitoring_history', args)[2])
                    self.assertTrue(value['isError'])
                    self.assertEqual(value['structuredContent']['error']['code'], 'invalid_key')
                    self.assertNotIn('points', value['structuredContent'])


if __name__ == '__main__':
    unittest.main()
