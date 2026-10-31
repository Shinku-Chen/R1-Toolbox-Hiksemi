"""Foreground fills retain independent owner authorization and passive GETs."""
import copy
import json
import threading
import unittest
from unittest import mock

import test_capacity_refresh_http as capacity_http
from test_capacity_refresh_http import RefreshError, SECRET

# Panel discovery can replace sys.modules["web_config"] with an isolated module.
# Use the same server module and exact exception classes as the shared fixture.
web_config = capacity_http.web_config


class StorageFillHTTPTests(unittest.TestCase):
    login = capacity_http.CapacityRefreshHTTPTests.login
    pair = capacity_http.CapacityRefreshHTTPTests.pair
    revoke = capacity_http.CapacityRefreshHTTPTests.revoke

    def setUp(self):
        capacity_http.CapacityRefreshHTTPTests.setUp(self)
        self.smart = {'ok': True, 'force': False, 'status': 'complete',
                      'started_at': 100.0, 'finished_at': 101.0,
                      'results': [{'device': 'sda', 'status': 'skipped_standby',
                                   'reason': 'standby', 'sampled_at': 100.5}]}
        self.capacity = {'ok': True, 'status': 'complete',
                         'started_at': 101.0, 'finished_at': 102.0,
                         'results': [{'mount_id': 101, 'status': 'skipped_standby',
                                      'reason': 'standby', 'sampled_at': None}]}
        self.insights.fill_smart_missing.return_value = self.smart
        self.insights.capacity_fill_eligibility.return_value = 'standby'
        self.monitor.refresh_capacity_missing = mock.Mock(return_value=self.capacity)

    def request(self, body=None, *, path='/api/r1/storage/fill', **kwargs):
        return capacity_http.CapacityRefreshHTTPTests.request(self, body, path=path, **kwargs)

    def developer_window(self, enabled=True):
        status, value = self.request({'enabled': enabled, 'allowed_ips': ['127.0.0.0/8'],
                                      'minutes': 10, 'allow_commands': False},
                                     path='/api/services/developer')
        self.assertEqual(status, 200, value)
        self.assertIs(self.server._dev_authorized('127.0.0.1'), enabled)

    def test_fixed_route_and_receipt_preserve_both_actual_results(self):
        self.assertEqual(web_config.STORAGE_FILL_ROUTE, '/api/r1/storage/fill')
        status, value = self.request({})
        self.assertEqual(status, 200, value)
        self.assertEqual(set(value), {'ok', 'force', 'status', 'started_at',
                                     'finished_at', 'smart', 'capacity'})
        self.assertIs(value['force'], False)
        self.assertEqual(value['status'], 'complete')
        self.assertEqual(value['smart'], self.smart)
        self.assertEqual(value['capacity'], self.capacity)
        self.assertEqual(value['capacity']['status'], 'complete')
        self.insights.fill_smart_missing.assert_called_once()
        args = self.insights.fill_smart_missing.call_args.kwargs
        self.assertEqual(set(args), {'authorizer'})
        self.assertIs(args['authorizer'](), True)
        cap_args = self.monitor.refresh_capacity_missing.call_args.kwargs
        self.assertIs(cap_args['eligibility'], self.insights.capacity_fill_eligibility)
        self.assertIs(cap_args['authorizer'], args['authorizer'])

    def test_strict_empty_body_and_no_force_device_or_url_selectors(self):
        for body in (None, [], False, {'force': False}, {'device': 'sda'},
                     {'path': SECRET}, {'capacity': True}, b'{', b'{}{}'):
            with self.subTest(body=body):
                status, value = self.request(body)
                self.assertIn(status, (400, 415), value)
                self.assertNotIn(SECRET, json.dumps(value))
        for suffix in ('?', '?force=false', '#', '#private'):
            self.assertEqual(self.request({}, path=web_config.STORAGE_FILL_ROUTE + suffix)[0], 400)
        self.insights.fill_smart_missing.assert_not_called()
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_no_owner_bearer_and_developer_only_cannot_fill(self):
        self.login()
        self.cookie = None
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            for headers in ({}, {'Authorization': 'Bearer test-key'}, {'X-R1-Token': 'local-key'}):
                self.assertEqual(self.request({}, headers=headers)[0], 403)
        self.insights.fill_smart_missing.assert_not_called()

    def test_open_password_and_pair_owner_are_accepted(self):
        self.assertEqual(self.request({})[0], 200)
        self.login()
        self.assertEqual(self.request({})[0], 200)
        self.pair()
        self.assertEqual(self.request({}, headers={'X-FNM-Request': None,
                                                   'X-R1-CSRF': 'c' * 43})[0], 200)

    def test_same_origin_json_csrf_gates_precede_collectors(self):
        for headers in ({'Origin': 'http://foreign.invalid'}, {'Origin': None},
                        {'Content-Type': 'text/plain'}, {'X-FNM-Request': None}):
            self.assertIn(self.request({}, headers=headers)[0], (400, 403, 415))
        self.insights.fill_smart_missing.assert_not_called()

    def test_gets_and_construction_do_not_fill(self):
        self.insights.fill_smart_missing.assert_not_called()
        for path in ('/api/overview', '/api/smart?dev=sda', '/api/r1'):
            self.assertEqual(self.request(method='GET', path=path)[0], 200)
        self.assertIn(self.request(method='GET')[0], (400, 401, 403, 404, 405))
        self.insights.fill_smart_missing.assert_not_called()
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_partial_one_phase_stays_partial_without_discarding_outcomes(self):
        self.capacity['status'] = 'partial'
        self.capacity['results'][0].update(status='unavailable', reason='power_state_unverified')
        status, value = self.request({})
        self.assertEqual((status, value['status']), (200, 'partial'))
        self.assertEqual(value['smart'], self.smart)

    def test_busy_smart_does_not_start_capacity_or_retry(self):
        self.insights.fill_smart_missing.side_effect = RefreshError('busy')
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (409, 'busy'))
        self.insights.fill_smart_missing.assert_called_once()
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_busy_capacity_is_not_replayed(self):
        self.monitor.refresh_capacity_missing.side_effect = RefreshError('busy')
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (409, 'busy'))
        self.insights.fill_smart_missing.assert_called_once()
        self.monitor.refresh_capacity_missing.assert_called_once()

    def test_concurrent_fill_returns_busy_before_second_smart(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def hold(**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError(SECRET)
            return self.smart
        self.insights.fill_smart_missing.side_effect = hold
        thread = threading.Thread(target=lambda: result.append(self.request({})), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            status, value = self.request({})
            self.assertEqual((status, value['reason']), (409, 'busy'))
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.insights.fill_smart_missing.assert_called_once()

    def test_smart_revocation_prevents_capacity_and_response(self):
        self.login()
        def revoke(**kwargs):
            self.revoke()
            return self.smart
        self.insights.fill_smart_missing.side_effect = revoke
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assertNotIn('smart', value)
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_serialization_revocation_discards_entire_projection(self):
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

    def test_first_denial_is_latched_across_regrant(self):
        allowed = [True]
        observed = []
        def revoke_then_regrant(**kwargs):
            allowed[0] = False
            observed.append(kwargs['authorizer']())
            allowed[0] = True
            observed.append(kwargs['authorizer']())
            return copy.deepcopy(self.smart)
        self.insights.fill_smart_missing.side_effect = revoke_then_regrant
        with self.assertRaises(web_config.StorageFillError) as caught:
            self.server.storage_fill(authorizer=lambda: allowed[0])
        self.assertEqual(caught.exception.reason, 'authorization_denied')
        self.assertEqual(observed, [False, False])
        self.monitor.refresh_capacity_missing.assert_not_called()

    def test_unexpected_exception_never_leaks_source_text(self):
        self.insights.fill_smart_missing.side_effect = RuntimeError(SECRET)
        status, value = self.request({})
        self.assertEqual((status, value['reason']), (503, 'unavailable'))
        self.assertNotIn(SECRET, json.dumps(value))

    def test_session_open_owner_remains_owner_with_development_enabled(self):
        self.developer_window()
        status, value = self.request(method='GET', path='/api/auth/session')
        self.assertEqual(status, 200, value)
        self.assertIs(value['authorized'], True)
        self.assertIs(value['dev_mode'], True)
        self.assertIs(value['logged_in'], False)
        self.assertIs(value['owner_authorized'], True)
        self.assertEqual(self.request({})[0], 200)

    def test_session_logged_in_password_and_developer_window_keep_owner(self):
        self.login()
        self.developer_window()
        status, value = self.request(method='GET', path='/api/auth/session')
        self.assertEqual(status, 200, value)
        self.assertIs(value['logged_in'], True)
        self.assertIs(value['auth_required'], True)
        self.assertIs(value['owner_authorized'], True)
        # Legacy dev_mode is suppressed when password protection is on.
        self.assertIs(value['dev_mode'], False)
        # An actual password session can also coexist with open mode and
        # dev_mode=true. Keep the original auth store semantics unchanged.
        self.server.auth.set_enabled(False)
        login = self.server.auth.login('capacity-refresh-fixture-password')
        self.assertTrue(login['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + login['sid']
        status, value = self.request(method='GET', path='/api/auth/session')
        self.assertEqual(status, 200, value)
        self.assertIs(value['logged_in'], True)
        self.assertIs(value['dev_mode'], True)
        self.assertIs(value['owner_authorized'], True)
        self.assertEqual(self.request({})[0], 200)

    def test_session_pair_owner_is_explicit_without_changing_legacy_authorized(self):
        self.pair()
        for developer in (False, True):
            self.developer_window(developer)
            with self.subTest(developer=developer):
                status, value = self.request(method='GET', path='/api/auth/session')
                self.assertEqual(status, 200, value)
                self.assertIs(value['logged_in'], False)
                self.assertIs(value['authorized'], developer)
                self.assertIs(value['dev_mode'], developer)
                self.assertIs(value['owner_authorized'], True)
                self.assertEqual(self.request({})[0], 200)

    def test_session_developer_bearer_and_local_token_alone_are_not_owner(self):
        self.developer_window()
        self.server.auth_mode = 'pair'
        for headers in ({}, {'Authorization': 'Bearer fixture-key'},
                        {'X-R1-Token': 'fixture-local-screen-key'}):
            with self.subTest(headers=headers):
                status, value = self.request(method='GET', path='/api/auth/session', headers=headers)
                self.assertEqual(status, 200, value)
                self.assertIs(value['authorized'], True)
                self.assertIs(value['dev_mode'], True)
                self.assertIs(value['owner_authorized'], False)
                self.assertEqual(self.request({}, headers=headers)[0], 403)
        self.insights.fill_smart_missing.assert_not_called()

    def test_session_password_missing_or_invalid_cookie_is_not_owner(self):
        self.login()
        self.developer_window()
        for cookie in (None, web_config.AUTH_COOKIE + '=' + 'x' * 43,
                       web_config.COOKIE + '=' + 'y' * 43):
            self.cookie = cookie
            with self.subTest(cookie_present=cookie is not None):
                status, value = self.request(method='GET', path='/api/auth/session')
                self.assertEqual(status, 200, value)
                self.assertIs(value['auth_required'], True)
                self.assertIs(value['owner_authorized'], False)
                self.assertEqual(self.request({})[0], 403)
        self.insights.fill_smart_missing.assert_not_called()

    def test_session_hint_cannot_bypass_bearer_or_revoked_owner_post_gates(self):
        # Open-mode owner evidence comes from open mode, never from Bearer.
        fields = {'Authorization': 'Bearer fixture-key'}
        status, value = self.request(method='GET', path='/api/auth/session', headers=fields)
        self.assertEqual(status, 200, value)
        self.assertIs(value['owner_authorized'], True)
        self.assertEqual(self.request({}, headers=fields)[0], 403)
        self.login()
        status, value = self.request(method='GET', path='/api/auth/session')
        self.assertEqual(status, 200, value)
        self.assertIs(value['owner_authorized'], True)
        self.revoke()
        self.assertEqual(self.request({})[0], 403)
        self.insights.fill_smart_missing.assert_not_called()

    def test_auth_status_owner_boolean_delegates_to_the_real_panel_predicate(self):
        peer = '127.0.0.1'
        for grant, expected in (('password', True), ('pair', True), ('open', True),
                                ('dev', False), (None, False)):
            with self.subTest(grant=grant), mock.patch.object(
                    self.server, 'panel_authorized', return_value=grant) as authorize:
                value = self.server.auth_status({}, peer=peer)
                self.assertIs(value['owner_authorized'], expected)
                authorize.assert_called_once_with({}, peer=peer)


if __name__ == '__main__':
    unittest.main()
