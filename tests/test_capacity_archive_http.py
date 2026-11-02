"""Owner HTTP archives use real publication, with TEMP-only metadata seams.

These are portable lifecycle/transport fixtures, not NAS or kernel syscall
evidence. Shared setup is reused without inheriting its pre-existing tests.
"""
import http.client
import json
import socketserver
import threading
import unittest
from unittest import mock

import test_capacity_history_http as history_http


FIELD = 'capacity_observation_archive'
service = history_http.history_core.service


class CapacityArchiveHTTPTests(unittest.TestCase):
    request = history_http.CapacityHistoryHTTPTests.request
    overview = history_http.CapacityHistoryHTTPTests.overview
    history = history_http.CapacityHistoryHTTPTests.history

    def setUp(self):
        history_http.CapacityHistoryHTTPTests.setUp(self)
        # Eight distinct trusted partition identities across two physical disks.
        # Only the existing OS identity and measurement seams are substituted.
        self.core.mounts = []
        for name, first_dev, first_mount in (('sda', 1, 11), ('sdb', 17, 21)):
            for offset in range(4):
                dev = '8:' + str(first_dev + offset)
                self.core.devices[dev] = name
                self.core.generations[dev] = 1
                self.core.mounts.append((first_mount + offset, dev,
                                         '/drives/archive-' + str(first_mount + offset)))
        self.core.write_mounts()

    def refresh(self, path=history_http.capacity_http.ROUTE):
        self.published.clear()
        code, value = self.request({}, path=path)
        self.assertEqual(code, 200, value)
        self.assertTrue(self.published.wait(2), 'response did not reach real finalizer')
        return value

    def archive(self):
        code, value = self.overview()
        self.assertEqual(code, 200, value)
        self.assertIn(FIELD, value)
        return value[FIELD]

    def assert_archive_absent(self, value):
        self.assertNotIn(FIELD, json.dumps(value))
        self.assertNotIn('past_successful_observation', json.dumps(value))

    def test_owner_http_publishes_exact_eight_filesystem_archive_contract(self):
        receipt = self.refresh()
        archive = self.archive()
        self.assertEqual(set(archive), {
            'schema_version', 'status', 'source', 'reason', 'observation_id',
            'total', 'used', 'available', 'percent', 'sampled_at',
            'sampled_at_latest', 'coverage', 'retention'})
        self.assertEqual(archive['schema_version'], 1)
        self.assertEqual(archive['status'], 'historical')
        self.assertEqual(archive['source'], 'past_successful_observation')
        self.assertEqual(archive['reason'], 'current_binding_unverified')
        self.assertEqual(archive['retention'], 'memory_until_restart')
        self.assertRegex(archive['observation_id'], r'^[1-9][0-9]*$')
        self.assertLessEqual(int(archive['observation_id']), 2 ** 63 - 1)
        self.assertEqual((archive['total'], archive['used'], archive['available'],
                          archive['percent']), (140000, 35000, 105000, 25.0))
        self.assertEqual(archive['coverage'], {
            'scope': 'measured_filesystem_subset', 'filesystem_count': 8,
            'disk_count': 2, 'complete': False})
        dates = [row['sampled_at'] for row in receipt['results']
                 if row['status'] == 'refreshed']
        self.assertEqual((archive['sampled_at'], archive['sampled_at_latest']),
                         (min(dates), max(dates)))
        text = json.dumps(archive)
        for private in ('/sys/', '/drives/', 'physical-', 'identity', 'mount_id',
                        'device_name', 'serial', 'records'):
            self.assertNotIn(private, text)

    def test_watch_then_incomplete_preserves_archive_without_history_revival(self):
        self.refresh()
        expected = self.archive()
        self.assertEqual(len(self.core.sampler._capacity_history), 8)
        current, observed = self.core.event()
        self.assertTrue(all(row['total_bytes'] is None for row in current))
        self.assertEqual(len(self.core.sampler._capacity_history), 8)
        self.assertIsNotNone(observed['summary'])
        self.assertEqual(self.archive(), expected)
        (self.core.proc / 'self/mountinfo').write_text('malformed\n', encoding='utf-8')
        code, value = self.overview()
        self.assertEqual(code, 200)
        self.assertIsNone(value['disk_size'])
        self.assertIsNone(value['capacity_last_observation_summary'])
        self.assertEqual(value[FIELD], expected)
        self.assertEqual(self.core.sampler._capacity_history, {})
        self.core.write_mounts()
        code, restored = self.overview()
        self.assertEqual(code, 200)
        self.assertIsNone(restored['disk_size'])
        self.assertIsNone(restored['capacity_last_observation_summary'])
        self.assertTrue(all(row['total'] is None and row['capacity_last_observation'] is None
                            and FIELD not in row for row in restored['disks_detail']))
        self.assertEqual(restored[FIELD], expected)

    def test_nested_storage_refresh_finalizes_same_archive_receipt_once(self):
        self.fixture.insights.refresh_smart_visible.return_value = {
            'ok': True, 'status': 'complete', 'results': []}
        self.fixture.insights.capacity_visible_eligibility.return_value = lambda _: None
        value = self.refresh('/api/r1/storage/refresh')
        self.assertIs(value['force'], False)
        self.assertEqual(self.publish.call_count, 1)
        self.assertEqual(self.archive()['coverage']['filesystem_count'], 8)

    def test_password_pair_and_open_owner_publications_are_accepted(self):
        self.refresh()
        self.assertEqual(self.archive()['observation_id'], '1')
        self.fixture.login()
        self.refresh()
        self.assertEqual(self.archive()['observation_id'], '2')
        self.server.auth.set_enabled(False)
        self.fixture.pair()
        self.refresh()
        self.assertEqual(self.archive()['observation_id'], '3')
        self.assertEqual(self.publish.call_count, 3)

    def test_partial_second_http_batch_archives_only_its_real_successful_subset(self):
        self.refresh()
        original = self.core.probe.side_effect
        def partial(entry, *args):
            if entry['mount_id'] != 24:
                raise service.CapacityRefreshError('probe_failed')
            return original(entry, *args)
        self.core.probe.side_effect = partial
        value = self.refresh()
        self.assertEqual(value['status'], 'partial')
        archive = self.archive()
        self.assertEqual(archive['observation_id'], '2')
        self.assertEqual((archive['total'], archive['used'], archive['available']),
                         (24000, 6000, 18000))
        self.assertEqual(archive['coverage'], {
            'scope': 'measured_filesystem_subset', 'filesystem_count': 1,
            'disk_count': 1, 'complete': False})
        measured = [row['sampled_at'] for row in value['results']
                    if row['status'] == 'refreshed']
        self.assertEqual(measured, [archive['sampled_at']])
        self.assertEqual(archive['sampled_at_latest'], measured[0])

    def test_developer_only_and_bearer_even_with_owner_cookie_do_not_read_archive(self):
        self.refresh()
        self.fixture.login()
        code, value = self.overview(headers={'Authorization': 'Bearer not-an-owner'})
        self.assertEqual(code, 200)
        self.assert_archive_absent(value)
        self.server.auth.set_enabled(False)
        self.fixture.cookie = None
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            code, value = self.overview()
        self.assertEqual(code, 200)
        self.assert_archive_absent(value)

    def test_public_and_authenticated_machine_routes_never_serialize_archive(self):
        self.refresh()
        expected = self.archive()
        code, value = self.request(method='GET', path='/api/v1/status')
        self.assertEqual(code, 200)
        self.assert_archive_absent(value)
        self.server.api_access.configure({'enabled': True})
        key = self.server.api_access.create_key({
            'name': 'archive HTTP isolation fixture', 'scopes': ['read'],
            'allowed_ips': [], 'expires_days': 1})
        headers = {'Authorization': 'Bearer ' + key['token']}
        code, value = self.request(method='GET', path='/api/integration/status?section=overview',
                                   headers=headers)
        self.assertEqual(code, 200, value)
        self.assertFalse(value.get('isError'), value)
        self.assert_archive_absent(value)
        code, value = self.request({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'get_nas_status', 'arguments': {'section': 'overview'}}},
            path='/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream',
                                  'MCP-Protocol-Version': '2025-03-26'})
        self.assertEqual(code, 200, value)
        self.assertNotIn('error', value)
        self.assertFalse(value['result'].get('isError'), value)
        self.assert_archive_absent(value)
        self.assertEqual(self.archive(), expected)

    def test_owner_without_any_success_gets_explicit_null_archive(self):
        self.assertIsNone(self.archive())
        self.publish.assert_not_called()
        self.assertEqual(self.core.calls, [])

    def test_passive_gets_do_not_collect_publish_or_renew_archive_dates(self):
        self.refresh()
        expected = self.archive()
        self.core.event()
        calls, publications = list(self.core.calls), self.publish.call_count
        with mock.patch.object(self.core.sampler, '_capacity_probe',
                               side_effect=AssertionError('passive measurement')), \
                mock.patch.object(service.os, 'statvfs', create=True,
                                  side_effect=AssertionError('passive statvfs')), \
                mock.patch.object(self.fixture.insights, 'refresh_smart',
                                  side_effect=AssertionError('passive SMART')), \
                mock.patch.object(service.time, 'time',
                                  return_value=expected['sampled_at_latest'] + 31536000):
            for _ in range(3):
                for path in ('/api/overview', '/api/smart?dev=sda', '/api/r1', '/api/v1/status'):
                    code, value = self.request(method='GET', path=path)
                    self.assertEqual(code, 200)
                    if path != '/api/overview':
                        self.assert_archive_absent(value)
                self.assertEqual(self.archive(), expected)
        self.assertEqual(self.core.calls, calls)
        self.assertEqual(self.publish.call_count, publications)

    def test_archive_uses_same_overview_snapshot_with_no_extra_inventory(self):
        with mock.patch.object(self.monitor, 'snapshot', wraps=self.monitor.snapshot) as snapshot, \
                mock.patch.object(service, '_capacity_inventory',
                                  wraps=service._capacity_inventory) as inventory:
            self.assertIsNone(self.archive())
        before = snapshot.call_count, inventory.call_count
        self.assertGreater(before[0], 0)
        self.assertGreater(before[1], 0)
        self.refresh()
        expected = self.archive()
        with mock.patch.object(self.monitor, 'snapshot', wraps=self.monitor.snapshot) as snapshot, \
                mock.patch.object(service, '_capacity_inventory',
                                  wraps=service._capacity_inventory) as inventory:
            self.assertEqual(self.archive(), expected)
        # Preserve all original current-binding checks; archive adds none.
        self.assertEqual((snapshot.call_count, inventory.call_count), before)

    def test_core_commit_followed_by_owner_revocation_does_not_publish_archive(self):
        original = self.server.panel_post
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.server.auth_mode = 'pair'
            return result
        with mock.patch.object(self.server, 'panel_post', side_effect=revoke):
            code, value = self.request({})
        self.assertEqual(code, 403)
        self.assert_archive_absent(value)
        self.assertTrue(self.core.sampler._capacity_cache)
        self.publish.assert_not_called()
        self.server.auth_mode = 'password'
        self.assertIsNone(self.archive())

    def test_json_revocation_does_not_publish_archive(self):
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if type(value) is service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request({})
        self.assertEqual(code, 403)
        self.assert_archive_absent(value)
        self.publish.assert_not_called()
        self.server.auth_mode = 'password'
        self.assertIsNone(self.archive())

    def test_json_revoke_regrant_cannot_revive_archive_candidate(self):
        guards = []
        original_guard, original_dumps = self.server._capacity_authorizer, self.web.json.dumps
        def guard(*args, **kwargs):
            value = original_guard(*args, **kwargs)
            guards.append(value)
            return value
        def dumps(value, *args, **kwargs):
            if type(value) is service._CapacityHistoryReceipt:
                self.server.auth_mode = 'pair'
                self.assertFalse(guards[-1]())
                self.server.auth_mode = 'password'
            return original_dumps(value, *args, **kwargs)
        with mock.patch.object(self.server, '_capacity_authorizer', side_effect=guard), \
                mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.request({})
        self.assertEqual(code, 403)
        self.assert_archive_absent(value)
        self.assertIsNone(self.archive())
        self.publish.assert_not_called()

    def test_json_encoding_failure_cannot_publish_or_replace_archive(self):
        self.refresh()
        expected = self.archive()
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if type(value) is service._CapacityHistoryReceipt:
                raise ValueError('controlled archive response encoding failure')
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request({})
        self.assertEqual(self.archive(), expected)
        self.assertEqual(self.publish.call_count, 1)

    def test_owner_get_json_revocation_withholds_archive_without_deleting_it(self):
        self.refresh()
        expected = self.archive()
        original = self.web.json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and FIELD in value:
                self.server.auth_mode = 'pair'
            return original(value, *args, **kwargs)
        with mock.patch.object(self.web.json, 'dumps', side_effect=dumps):
            code, value = self.overview()
        self.assertEqual(code, 403)
        self.assert_archive_absent(value)
        self.server.auth_mode = 'password'
        self.assertEqual(self.archive(), expected)

    def test_developer_to_owner_mid_get_does_not_upgrade_archive_disclosure(self):
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
        self.assert_archive_absent(value)

    def test_transport_write_failure_does_not_publish_archive(self):
        original = socketserver._SocketWriter.write
        failed = threading.Event()
        def write(writer, data):
            if b'"results"' in data and b'"refreshed"' in data:
                failed.set()
                raise OSError('controlled archive transport failure')
            return original(writer, data)
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            with self.assertRaises(http.client.IncompleteRead):
                self.request({})
        self.assertTrue(failed.is_set())
        self.assertTrue(self.core.sampler._capacity_cache)
        self.assertIsNone(self.archive())
        self.publish.assert_not_called()

    def test_later_write_failure_keeps_previous_archive_and_dates(self):
        self.refresh()
        expected = self.archive()
        original = socketserver._SocketWriter.write
        def write(writer, data):
            if b'"results"' in data and b'"refreshed"' in data:
                raise OSError('controlled later archive transport failure')
            return original(writer, data)
        with mock.patch.object(socketserver._SocketWriter, 'write', new=write):
            with self.assertRaises(http.client.IncompleteRead):
                self.request({})
        self.assertEqual(self.archive(), expected)
        self.assertEqual(self.publish.call_count, 1)

    def test_finalizer_fault_preserves_response_and_previous_archive(self):
        self.refresh()
        expected = self.archive()
        with mock.patch.object(self.monitor, 'publish_capacity_history',
                               side_effect=ValueError('controlled archive finalizer failure')):
            code, value = self.request({})
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(self.archive(), expected)
        self.assertFalse(self.core.sampler._capacity_refresh_lock.locked())

    def test_final_authorizer_exception_rolls_back_archive_after_assignment(self):
        self.refresh()
        expected = self.archive()
        assigned = []
        def finalize(receipt, *, authorizer):
            calls = 0
            def guard():
                nonlocal calls
                calls += 1
                if calls == 3:
                    assigned.append(getattr(self.core.sampler.capacity_snapshot(),
                                            'capacity_observation_archive', None))
                    raise ValueError('controlled final archive authorization failure')
                return authorizer()
            try:
                return self.core.sampler.publish_capacity_history(receipt, authorizer=guard)
            finally:
                self.published.set()
        self.publish.side_effect = finalize
        self.refresh()
        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0]['observation_id'], '2')
        self.assertEqual(self.archive(), expected)

    def test_stop_start_during_finalizer_clears_archive_without_rollback_revival(self):
        self.refresh()
        def finalize(receipt, *, authorizer):
            calls = 0
            def guard():
                nonlocal calls
                calls += 1
                if calls == 3:
                    self.core.sampler.close_capacity()
                    self.core.sampler.start_capacity()
                return authorizer()
            try:
                return self.core.sampler.publish_capacity_history(receipt, authorizer=guard)
            finally:
                self.published.set()
        self.publish.side_effect = finalize
        self.refresh()
        self.assertIsNone(self.archive())
        self.assertEqual(self.history()['disks'], {})

    def test_late_http_a_cannot_replace_newer_b_after_original_history_pruned(self):
        entered, release, completed, newer = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)
        calls, late = 0, []
        def finalize(receipt, *, authorizer):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not release.wait(8):
                    raise AssertionError('late HTTP finalizer was not released')
                try:
                    result = self.core.sampler.publish_capacity_history(receipt, authorizer=authorizer)
                    late.append(result)
                    return result
                finally:
                    completed.set()
            try:
                return self.core.sampler.publish_capacity_history(receipt, authorizer=authorizer)
            finally:
                newer.set()
        self.publish.side_effect = finalize
        # Equal values and wall dates make archive business ordering decisive.
        with mock.patch.object(service.time, 'time', return_value=1793522657.0):
            self.assertEqual(self.request({})[0], 200)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request({})[0], 200)
            self.assertTrue(newer.wait(2))
            expected = self.archive()
            self.assertEqual(expected['observation_id'], '2')
            # This invokes only the original history pruning helper. A real
            # incomplete snapshot also invalidates current/epoch, and would
            # still reject A at those original lifecycle gates. Here A stays
            # eligible for its original per-disk publication semantics.
            with self.core.sampler._capacity_lock:
                self.core.sampler._capacity_history_prune([], False)
            self.assertEqual(self.history()['disks'], {})
            release.set()
            self.assertTrue(completed.wait(2))
        self.assertEqual(late, [True])
        self.assertEqual(self.archive(), expected)
        self.assertEqual(len(self.core.sampler._capacity_history), 8)
        self.assertEqual(set(self.history()['disks']), {'sda', 'sdb'})
        self.assertTrue(all(row['observation_id'] == '1'
                            for row in self.history()['disks'].values()))


if __name__ == '__main__':
    unittest.main()
