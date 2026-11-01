"""Actual HTTP publication/owner guards with the production capacity sampler."""
import http.client
import json
import socketserver
import threading
import unittest
from unittest import mock

import test_capacity_history_core as history_core
import test_capacity_refresh_http as capacity_http


class CapacityHistoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.core = history_core.CapacityHistoryCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.fixture = capacity_http.CapacityRefreshHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.web = capacity_http.web_config  # Use the module that created this server.
        self.server = self.fixture.server
        self.monitor = history_core.service.StatusMonitor(self.core.sampler)
        self.fixture.monitor = self.server.monitor = self.server.panel.monitor = self.monitor
        self.server.panel.proc_root, self.server.panel.sys_root = self.core.proc, self.core.sys
        self.fixture.insights.snapshot.return_value = {
            'disks': {'items': [{'id': name, 'type': 'HDD', 'size_bytes': 100000,
                                 'health': {}, 'smart_available': True} for name in ('sda', 'sdb')]},
            'device': {'storage': {'devices': []}}}
        self.published = threading.Event()
        publisher = self.monitor.publish_capacity_history
        def publish(*args, **kwargs):
            try:
                return publisher(*args, **kwargs)
            finally:
                self.published.set()
        patch = mock.patch.object(self.monitor, 'publish_capacity_history', side_effect=publish)
        self.publish = patch.start()
        self.addCleanup(patch.stop)

    def request(self, body=None, *, method='POST', path=capacity_http.ROUTE, headers=None):
        return self.fixture.request(body, method=method, path=path, headers=headers)

    def refresh(self, path=capacity_http.ROUTE):
        code, value = self.request({}, path=path)
        self.assertEqual(code, 200)
        self.assertTrue(self.published.wait(2), 'real send did not reach publication')
        return value

    def overview(self, **kwargs):
        return self.request(method='GET', path='/api/overview', **kwargs)

    def history(self):
        return self.core.view()[1]

    def test_owner_real_http_publication_survives_event_with_current_null(self):
        self.refresh()
        code, before = self.overview()
        self.assertEqual(code, 200)
        self.assertIsNotNone(before['capacity_last_observation_summary'])
        self.core.event()
        code, after = self.overview()
        self.assertEqual(code, 200)
        self.assertIsNone(after['disk_size'])
        self.assertEqual(after['capacity_last_observation_summary']['sampled_at'],
                         before['capacity_last_observation_summary']['sampled_at'])
        self.assertTrue(all(row['total'] is None and row['capacity_last_observation'] is not None
                            for row in after['disks_detail']))
        self.assertNotIn('physical-', json.dumps(after))

    def test_nested_storage_refresh_finalizes_original_private_capacity_receipt(self):
        self.fixture.insights.refresh_smart_visible.return_value = {
            'ok': True, 'status': 'complete', 'results': []}
        self.fixture.insights.capacity_visible_eligibility.return_value = lambda _: None
        value = self.refresh('/api/r1/storage/refresh')
        self.assertIs(value['force'], False)
        self.assertEqual(self.publish.call_count, 1)
        self.assertIsNotNone(self.history()['summary'])

    def test_gets_never_publish_or_collect_history(self):
        self.refresh()
        calls = list(self.core.calls)
        count = self.publish.call_count
        for path in ('/api/overview', '/api/smart?dev=sda', '/api/r1'):
            self.assertEqual(self.request(method='GET', path=path)[0], 200)
        self.assertEqual(self.core.calls, calls)
        self.assertEqual(self.publish.call_count, count)

    def test_no_owner_developer_and_bearer_cannot_obtain_history(self):
        self.refresh()
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            code, value = self.overview()
            self.assertEqual(code, 200)
            self.assertNotIn('capacity_last_observation_summary', value)
            self.assertTrue(all('capacity_last_observation' not in row for row in value['disks_detail']))
        self.server.auth_mode = 'password'
        code, value = self.overview(headers={'Authorization': 'Bearer historical-not-owner'})
        self.assertEqual(code, 200)
        self.assertNotIn('capacity_last_observation_summary', value)

    def test_password_and_pair_owner_can_read_history(self):
        self.fixture.login()
        self.refresh()
        self.assertIsNotNone(self.overview()[1]['capacity_last_observation_summary'])
        # Pair-only reads require the fixture's password protection disabled;
        # retain the real panel_readable/password precedence rather than mock it.
        self.server.auth.set_enabled(False)
        self.fixture.pair()
        self.assertIsNotNone(self.overview()[1]['capacity_last_observation_summary'])

    def test_core_success_followed_by_web_revocation_publishes_no_history(self):
        original = self.server.panel_post
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.server.auth_mode = 'pair'
            return result
        with mock.patch.object(self.server, 'panel_post', side_effect=revoke):
            code, value = self.request({})
        self.assertEqual(code, 403)
        self.assertTrue(self.core.sampler._capacity_cache, 'original core commit semantics changed')
        self.assertEqual(self.history()['disks'], {})
        self.publish.assert_not_called()

    def test_json_revocation_after_core_success_never_publishes(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if type(value) is history_core.service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request({})
        self.assertEqual(code, 403)
        self.assertNotIn('results', value)
        self.assertEqual(self.history()['disks'], {})
        self.publish.assert_not_called()

    def test_json_revoke_then_regrant_does_not_revive_history_receipt(self):
        current = []
        original_guard, original_dumps = self.server._capacity_authorizer, self.web.json.dumps
        def guard(*args, **kwargs):
            value = original_guard(*args, **kwargs)
            current.append(value)
            return value
        def dumps(value, *args, **kwargs):
            if type(value) is history_core.service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
                self.assertFalse(current[-1]())
                self.server.auth_mode = 'password'
            return original_dumps(value, *args, **kwargs)
        with mock.patch.object(self.server, '_capacity_authorizer', side_effect=guard), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, _ = self.request({})
        self.assertEqual(code, 403)
        self.assertEqual(self.history()['disks'], {})

    def test_owner_get_json_revocation_discards_all_owner_history(self):
        self.refresh()
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'capacity_last_observation_summary' in value:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.overview()
        self.assertEqual(code, 403)
        self.assertNotIn('capacity_last_observation_summary', value)

    def test_read_cannot_upgrade_from_developer_to_owner_mid_request(self):
        self.refresh()
        self.server.auth_mode = 'pair'
        original = self.server.panel_get
        def grant(*args, **kwargs):
            self.server.auth_mode = 'password'
            return original(*args, **kwargs)
        with mock.patch.object(self.server, '_dev_authorized', return_value=True), \
                mock.patch.object(self.server, 'panel_get', side_effect=grant):
            code, value = self.overview()
        self.assertEqual(code, 200)
        self.assertNotIn('capacity_last_observation_summary', value)

    def test_transport_write_failure_keeps_core_commit_but_not_history(self):
        original = socketserver._SocketWriter.write
        failed = threading.Event()
        def write(writer, data):
            if b'"results"' in data and b'"refreshed"' in data:
                failed.set()
                raise OSError('controlled response transport failure')
            return original(writer, data)
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            with self.assertRaises(http.client.IncompleteRead):
                self.request({})
        self.assertTrue(failed.is_set())
        self.assertTrue(self.core.sampler._capacity_cache)
        self.assertEqual(self.history()['disks'], {})
        self.publish.assert_not_called()

    def test_failed_later_transport_does_not_roll_back_already_published_history(self):
        self.refresh()
        previous = self.history()
        original = socketserver._SocketWriter.write
        def write(writer, data):
            if b'"results"' in data and b'"refreshed"' in data:
                raise OSError('controlled later transport failure')
            return original(writer, data)
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            with self.assertRaises(http.client.IncompleteRead):
                self.request({})
        self.assertEqual(self.history(), previous)
        self.assertEqual(self.publish.call_count, 1)

    def test_observer_fault_and_history_publisher_fault_do_not_break_response(self):
        with mock.patch.object(self.core.sampler, '_capacity_record', side_effect=ValueError('observer')), \
                mock.patch.object(self.monitor, 'publish_capacity_history', side_effect=ValueError('history')):
            code, value = self.request({})
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertTrue(self.core.sampler._capacity_cache)
        self.assertFalse(self.core.sampler._capacity_refresh_lock.locked())


if __name__ == '__main__':
    unittest.main()
