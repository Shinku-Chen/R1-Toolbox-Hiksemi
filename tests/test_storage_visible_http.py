"""Explicit Web page-entry refresh, with exact owner and source-round boundaries."""
import json
import threading
import unittest
from unittest import mock

import test_storage_fill_http as fill_http
from test_capacity_refresh_http import RefreshError, SECRET

# Use the exact module that creates the shared fixture server even when panel
# discovery replaces sys.modules with an isolated web_config instance.
web_config = fill_http.web_config
ROUTE = '/api/r1/storage/refresh'


class StorageVisibleHTTPTests(unittest.TestCase):
    login = fill_http.StorageFillHTTPTests.login
    pair = fill_http.StorageFillHTTPTests.pair
    revoke = fill_http.StorageFillHTTPTests.revoke

    def setUp(self):
        fill_http.StorageFillHTTPTests.setUp(self)
        self.smart['results'][0].update(status='refreshed', reason=None)
        self.capacity['results'][0].update(status='refreshed', reason=None, sampled_at=101.5)
        self.insights.refresh_smart_visible = mock.Mock(return_value=self.smart)
        self.eligible = mock.Mock(return_value=None)
        self.insights.capacity_visible_eligibility = mock.Mock(return_value=self.eligible)
        self.monitor.refresh_capacity_visible = mock.Mock(return_value=self.capacity)

    def request(self, body=None, *, path=ROUTE, **kwargs):
        return fill_http.capacity_http.CapacityRefreshHTTPTests.request(self, body, path=path, **kwargs)

    def assert_not_sampled(self):
        self.insights.refresh_smart_visible.assert_not_called()
        self.monitor.refresh_capacity_visible.assert_not_called()

    def test_exact_receipt_and_same_round_private_eligibility(self):
        self.assertEqual(web_config.STORAGE_REFRESH_ROUTE, ROUTE)
        status, value = self.request({})
        self.assertEqual(status, 200, value)
        self.assertEqual(set(value), {'ok', 'force', 'status', 'started_at', 'finished_at', 'smart', 'capacity'})
        self.assertIs(value['force'], False)
        self.assertEqual(value['smart'], self.smart)
        self.assertEqual(value['capacity'], self.capacity)
        self.insights.refresh_smart_visible.assert_called_once()
        self.insights.capacity_visible_eligibility.assert_called_once()
        self.assertIs(self.insights.capacity_visible_eligibility.call_args.args[0], self.smart)
        self.assertIs(self.monitor.refresh_capacity_visible.call_args.kwargs['eligibility'], self.eligible)
        self.insights.fill_smart_missing.assert_not_called()
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_fresh_positive_receipt_does_not_swallow_a_second_page_entry(self):
        for _ in range(2):
            self.assertEqual(self.request({})[0], 200)
        self.assertEqual(self.insights.refresh_smart_visible.call_count, 2)
        self.assertEqual(self.monitor.refresh_capacity_visible.call_count, 2)

    def test_strict_empty_json_and_no_caller_force_or_resources(self):
        for body in (None, {'force': False}, {'force': True}, {'view': 'storage'}, {'device': 'sda'},
                     {'path': '/dev/sda'}, {'extra': None}, [], False, 1, 'refresh'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assert_not_sampled()

    def test_malformed_or_duplicate_json_never_reaches_sources(self):
        for body in (b'', b'{', b'{"force":false,"force":true}', b'NaN'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assert_not_sampled()

    def test_no_query_or_fragment_even_empty(self):
        for suffix in ('?', '?force=false', '#', '#private'):
            self.assertEqual(self.request({}, path=ROUTE + suffix)[0], 400)
        self.assert_not_sampled()

    def test_password_pair_and_open_owner_modes_stay_exact(self):
        self.assertEqual(self.request({})[0], 200)
        self.login()
        self.assertEqual(self.request({})[0], 200)
        self.pair()
        self.assertEqual(self.request({}, headers={'X-FNM-Request': None, 'X-R1-CSRF': 'c' * 43})[0], 200)

    def test_developer_bearer_and_screen_tokens_cannot_grant_owner(self):
        self.login()
        self.cookie = None
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            for headers in ({}, {'Authorization': 'Bearer fixture-key'}, {'X-R1-Token': 'local-token'}):
                self.assertEqual(self.request({}, headers=headers)[0], 403)
        self.assert_not_sampled()

    def test_any_authorization_header_is_rejected_even_open_owner(self):
        for value in ('Bearer fixture-key', 'Basic invalid', ''):
            self.assertEqual(self.request({}, headers={'Authorization': value})[0], 403)
        self.assert_not_sampled()

    def test_origin_json_and_csrf_checks_precede_sources(self):
        for headers in ({'Origin': None}, {'Origin': 'http://foreign.invalid'},
                        {'Content-Type': 'text/plain'}, {'X-FNM-Request': None}):
            self.assertIn(self.request({}, headers=headers)[0], (400, 403, 415))
        self.assert_not_sampled()

    def test_get_and_passive_existing_routes_never_start_refresh(self):
        for path in ('/api/r1', '/api/overview', '/api/smart?dev=sda'):
            self.assertEqual(self.request(method='GET', path=path)[0], 200)
        self.assertIn(self.request(method='GET')[0], (400, 401, 403, 404, 405))
        self.assert_not_sampled()

    def test_original_fill_uses_only_original_collectors(self):
        self.assertEqual(self.request({}, path='/api/r1/storage/fill')[0], 200)
        self.insights.fill_smart_missing.assert_called_once()
        self.monitor.refresh_capacity_missing.assert_called_once()
        self.assert_not_sampled()

    def test_standby_and_unknown_preserve_actual_capacity_receipt_dates(self):
        for status, reason in (('skipped_standby', 'standby'), ('unavailable', 'power_state_unverified')):
            self.capacity['results'][0].update(status=status, reason=reason, sampled_at=77.5)
            self.capacity['status'] = 'complete' if status == 'skipped_standby' else 'partial'
            result, value = self.request({})
            self.assertEqual(result, 200)
            self.assertEqual(value['capacity']['results'][0]['sampled_at'], 77.5)
            self.assertEqual(value['capacity']['results'][0]['reason'], reason)
            self.assertEqual(value['status'], self.capacity['status'])

    def test_busy_smart_or_capacity_never_replays(self):
        self.insights.refresh_smart_visible.side_effect = RefreshError('busy')
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (409, 'busy'))
        self.monitor.refresh_capacity_visible.assert_not_called()
        self.insights.refresh_smart_visible.assert_called_once()
        self.insights.refresh_smart_visible.side_effect = None
        self.monitor.refresh_capacity_visible.side_effect = RefreshError('busy')
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (409, 'busy'))
        self.monitor.refresh_capacity_visible.assert_called_once()

    def test_new_refresh_and_original_fill_share_nonqueueing_lock(self):
        entered, release, result = threading.Event(), threading.Event(), []
        def hold(**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError(SECRET)
            return self.smart
        self.insights.refresh_smart_visible.side_effect = hold
        thread = threading.Thread(target=lambda: result.append(self.request({})), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for route in (ROUTE, '/api/r1/storage/fill'):
                status, value = self.request({}, path=route)
                self.assertEqual((status, value['reason']), (409, 'busy'))
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 200)
        self.insights.refresh_smart_visible.assert_called_once()
        self.insights.fill_smart_missing.assert_not_called()

    def test_owner_revoked_after_smart_never_measures_capacity(self):
        self.login()
        def revoke(**kwargs):
            self.revoke()
            return self.smart
        self.insights.refresh_smart_visible.side_effect = revoke
        status, value = self.request({})
        # The final owner send guard uses the existing standard 403 envelope.
        self.assertEqual(status, 403, value)
        self.assertNotIn('smart', value)
        self.monitor.refresh_capacity_visible.assert_not_called()

    def test_first_denial_latches_even_after_regrant(self):
        allowed, observed = [True], []
        def revoke_then_regrant(**kwargs):
            allowed[0] = False
            observed.append(kwargs['authorizer']())
            allowed[0] = True
            observed.append(kwargs['authorizer']())
            return self.smart
        self.insights.refresh_smart_visible.side_effect = revoke_then_regrant
        with self.assertRaises(web_config.StorageFillError) as caught:
            self.server.storage_refresh(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(observed, [False, False])
        self.monitor.refresh_capacity_visible.assert_not_called()

    def test_serialization_revocation_discards_complete_nested_result(self):
        self.login()
        original = web_config.json.dumps
        def serialize(value, *args, **kwargs):
            if isinstance(value, dict) and 'smart' in value and 'capacity' in value:
                self.revoke()
            return original(value, *args, **kwargs)
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assertNotIn('smart', value)
        self.assertNotIn('capacity', value)

    def test_real_restart_during_smart_cannot_revive_captured_owner(self):
        observed = []
        def restart(**kwargs):
            self.server.stop()
            self.server.start()
            observed.append(kwargs['authorizer']())
            return self.smart
        self.insights.refresh_smart_visible.side_effect = restart
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assertEqual(observed, [False])
        self.assertNotIn('smart', value)
        self.assertNotIn('capacity', value)
        self.monitor.refresh_capacity_visible.assert_not_called()

    def test_real_restart_after_json_encoding_discards_both_results(self):
        original = web_config.json.dumps
        observed = []
        def serialize(value, *args, **kwargs):
            encoded = original(value, *args, **kwargs)
            if isinstance(value, dict) and 'smart' in value and 'capacity' in value:
                self.server.stop()
                self.server.start()
                observed.append(True)
            return encoded
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assertEqual(observed, [True])
        self.assertNotIn('smart', value)
        self.assertNotIn('capacity', value)
        self.assertFalse(self.monitor.refresh_capacity_visible.call_args.kwargs['authorizer']())

    def test_owner_revoked_after_capacity_discards_complete_batch(self):
        self.login()
        def revoke(**kwargs):
            self.revoke()
            return self.capacity
        self.monitor.refresh_capacity_visible.side_effect = revoke
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assertNotIn('smart', value)
        self.assertNotIn('capacity', value)
        self.assertFalse(self.monitor.refresh_capacity_visible.call_args.kwargs['authorizer']())

    def test_unexpected_source_exception_never_exposes_free_text(self):
        self.insights.refresh_smart_visible.side_effect = RuntimeError(SECRET)
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (503, 'unavailable'))
        self.assertNotIn(SECRET, json.dumps(value))
        self.monitor.refresh_capacity_visible.assert_not_called()

    def test_invalid_private_eligibility_cannot_fall_back_to_old_cache(self):
        self.insights.capacity_visible_eligibility.return_value = None
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (503, 'unavailable'))
        self.monitor.refresh_capacity_visible.assert_not_called()
        self.monitor.refresh_capacity_missing.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
