"""Owner-only service controls through HTTP, without a NAS or real GUI."""

import json
import os
import unittest
from unittest import mock

import test_integration_http as fixture
from service_settings import ServiceSettingsStore


class ServiceSettingsHTTPTests(unittest.TestCase):
    request = fixture.IntegrationHTTPTests.request
    browser = fixture.IntegrationHTTPTests.browser
    rpc = fixture.IntegrationHTTPTests.rpc
    message = staticmethod(fixture.IntegrationHTTPTests.message)
    _patch = fixture.IntegrationHTTPTests._patch

    def setUp(self):
        fixture.IntegrationHTTPTests.setUp(self)
        self.wall = 1800000000
        with mock.patch.dict(os.environ, {'R1_DEV_NO_PAIRING': '0', 'R1_SMALL_SCREEN_ENABLED': '1'}):
            self.server.service_settings = ServiceSettingsStore(
                self.server.state_dir, clock=lambda: self.wall)
        self.server._sync_development(force=True)
        self.manager = mock.Mock()
        self.manager.snapshot.return_value = {
            'active': True, 'busy': False, 'available': True, 'last_error': None}
        self.server.attach_screen_manager(self.manager)
        self.manager.set_enabled.reset_mock()

    def state(self):
        status, _, value = self.browser(path='/api/services')
        self.assertEqual(status, 200, value)
        return value

    def developer(self, **changes):
        body = {'enabled': True, 'allowed_ips': ['127.0.0.0/8'],
                'minutes': 10, 'allow_commands': False, **changes}
        return self.browser('POST', '/api/services/developer', body)

    def test_defaults_are_independent_and_key_toggle_preserves_existing_keys(self):
        initial = self.state()
        self.assertIs(initial['screen']['enabled'], True)
        self.assertIs(initial['screen']['active'], True)
        self.assertIs(initial['mcp']['enabled'], False)
        self.assertIs(initial['developer']['enabled'], False)
        key = self.server.api_access.create_key({'name': 'fixture', 'role': 'readonly',
            'scopes': ['read'], 'file_roots': [], 'allowed_ips': [], 'expires_days': 1})
        for enabled in (True, False, True):
            status, _, value = self.browser('POST', '/api/services/mcp', {'enabled': enabled})
            self.assertEqual(status, 200, value)
            self.assertEqual(value['services']['mcp']['enabled'], enabled)
            self.assertEqual(self.rpc(key)[0], 200 if enabled else 403)
            self.assertIs(self.state()['screen']['enabled'], True)
        self.assertEqual(len(self.server.api_access.snapshot()['keys']), 1)
        self.manager.set_enabled.assert_not_called()

    def test_screen_desired_state_is_separate_from_async_actual_state(self):
        self.manager.snapshot.return_value.update(busy=True)
        status, _, value = self.browser('POST', '/api/services/screen', {'enabled': False})
        self.assertEqual(status, 200, value)
        screen = value['services']['screen']
        self.assertIs(screen['enabled'], False)
        self.assertIs(screen['active'], True)
        self.assertIs(screen['busy'], True)
        self.manager.set_enabled.assert_called_once_with(False)
        self.manager.snapshot.return_value.update(active=False, busy=False)
        self.assertIs(self.state()['screen']['active'], False)
        restarted = ServiceSettingsStore(self.server.state_dir, clock=lambda: self.wall)
        self.assertIs(restarted.snapshot()['screen_enabled'], False)
        self.assertIs(self.state()['mcp']['enabled'], False)

    def test_absent_manager_does_not_save_and_enqueue_failure_is_explicit(self):
        self.server.screen_manager = None
        status, _, value = self.browser('POST', '/api/services/screen', {'enabled': False})
        self.assertEqual(status, 503)
        self.assertIs(self.server.service_settings.snapshot()['screen_enabled'], True)
        self.server.screen_manager = self.manager
        self.manager.set_enabled.side_effect = RuntimeError('private fixture path and token')
        status, _, value = self.browser('POST', '/api/services/screen', {'enabled': False})
        self.assertEqual(status, 503)
        self.assertIs(value['saved'], True)
        self.assertIs(value['services']['screen']['active'], True)
        self.assertNotIn('private fixture', json.dumps(value))

    def test_developer_requires_explicit_allowlist_and_persists_expiry(self):
        for ips in ([], ['0.0.0.0/0'], ['example.org'], ['127.0.0.1', '127.0.0.1/32']):
            with self.subTest(ips=ips):
                self.assertEqual(self.developer(allowed_ips=ips)[0], 400)
                self.assertIs(self.state()['developer']['active'], False)
        status, _, value = self.developer(allowed_ips=['127.8.9.10/255.0.0.0'])
        self.assertEqual(status, 200, value)
        dev = value['services']['developer']
        self.assertEqual(dev['allowed_ips'], ['127.0.0.0/8'])
        self.assertEqual(dev['expires_at'], self.wall + 600)
        self.assertIs(self.server.service_settings.authorized('127.1.2.3'), True)
        # Owner password is still required for management, even in the allowlist.
        self.assertEqual(self.browser(path='/api/services', cookie=False)[0], 401)
        self.server.service_settings = ServiceSettingsStore(self.server.state_dir, clock=lambda: self.wall)
        self.assertEqual(self.state()['developer']['expires_at'], dev['expires_at'])
        self.wall += 600
        self.assertIs(self.state()['developer']['active'], False)
        self.assertIs(self.server.service_settings.authorized('127.0.0.1'), False)

    def test_actual_peer_only_and_disable_or_corruption_revokes_debug_immediately(self):
        self.assertEqual(self.developer(allowed_ips=['192.168.31.0/24'])[0], 200)
        headers = {'X-Forwarded-For': '192.168.31.2', 'Forwarded': 'for=192.168.31.2'}
        self.assertEqual(self.browser('POST', '/api/dev/diagnostics',
            {'action': 'snapshot'}, headers=headers)[0], 403)
        self.assertFalse(self.server._dev_authorized('127.0.0.1'))
        self.assertEqual(self.developer()[0], 200)
        with mock.patch('web_config.time.time', return_value=self.wall):
            self.assertTrue(self.server._dev_authorized('127.0.0.1'))
            self.assertEqual(self.developer(enabled=False)[0], 200)
            self.assertFalse(self.server._dev_authorized('127.0.0.1'))
            self.assertEqual(self.developer()[0], 200)
            self.server.service_settings.path.write_text('{broken', encoding='utf-8')
            self.assertFalse(self.server._dev_authorized('127.0.0.1'))
            self.assertIs(self.state()['developer']['active'], False)
            self.assertEqual(self.developer()[0], 503)

    def test_cookie_bearer_origin_csrf_and_url_boundaries(self):
        key = self.server.api_access.create_key({'name': 'fixture', 'role': 'admin',
            'scopes': ['read'], 'file_roots': [], 'allowed_ips': [], 'expires_days': 1})
        for route, body in (('/api/services/screen', {'enabled': False}),
                            ('/api/services/mcp', {'enabled': True}),
                            ('/api/services/developer', {'enabled': False, 'allowed_ips': [],
                             'minutes': 10, 'allow_commands': False})):
            with self.subTest(route=route):
                for changes in ({'cookie': False},
                                {'headers': {'Authorization': 'Bearer ' + key['token']}},
                                {'headers': {'Origin': 'http://foreign.invalid'}},
                                {'remove': ('X-FNM-Request',)},
                                {'headers': {'Content-Type': 'text/plain'}}):
                    self.assertIn(self.browser('POST', route, body, **changes)[0], (400, 401, 403, 415))
                for suffix in ('?enabled=true', '#fragment'):
                    self.assertEqual(self.browser('POST', route + suffix, body)[0], 400)
        for suffix in ('?anything=1', '#fragment'):
            self.assertEqual(self.browser(path='/api/services' + suffix)[0], 400)
        self.assertIs(self.state()['mcp']['enabled'], False)
        self.manager.set_enabled.assert_not_called()

    def test_unknown_fields_and_non_boolean_settings_never_reach_manager(self):
        for route in ('/api/services/screen', '/api/services/mcp'):
            for body in ({'enabled': 1}, {'enabled': 'true'}, {'enabled': True, 'command': 'sh'}, {}):
                with self.subTest(route=route, body=body):
                    self.assertEqual(self.browser('POST', route, body)[0], 400)
        self.manager.set_enabled.assert_not_called()

    def test_unknown_screen_state_is_null_and_never_reported_as_released(self):
        self.manager.snapshot.side_effect = OSError('private status path')
        state = self.state()['screen']
        self.assertIsNone(state['active'])
        self.assertIsNone(state['busy'])
        self.assertIs(state['available'], False)
        self.assertNotIn('private status', json.dumps(state))
        self.manager.snapshot.side_effect = None
        self.manager.snapshot.return_value = {'active': None, 'busy': False, 'available': True}
        self.assertIsNone(self.state()['screen']['active'])

    def test_fresh_default_password_mode_needs_no_screen_and_setup_then_gates_writes(self):
        old_server = self.server
        environment = dict(os.environ)
        for name in ('R1_WEB_AUTH_MODE', 'R1_DEV_NO_PAIRING', 'R1_DEV_ALLOW_IPS'):
            environment.pop(name, None)
        with mock.patch.dict(os.environ, environment, clear=True):
            self.server = fixture.web_config.WebConfigServer(
                self.controller, self.insights, host='127.0.0.1', port=0,
                state_dir=old_server.state_dir / 'fresh').start()
        self.addCleanup(self.server.stop)
        self.host = '127.0.0.1:' + str(self.server.port)
        self.assertEqual(self.server.auth_mode, 'password')
        self.assertFalse(self.server.auth.required())
        self.assertEqual(self.browser(path='/api/services', cookie=False)[0], 200)
        self.assertEqual(self.browser('POST', '/api/services/mcp', {'enabled': True}, cookie=False)[0], 200)
        self.assertEqual(self.developer()[0], 200)
        self.assertEqual(self.browser(path='/api/services', cookie=False)[0], 200)
        status, headers, value = self.browser('POST', '/api/auth/setup',
            {'password': 'fixture-new-owner'}, cookie=False, private=False)
        self.assertEqual(status, 200, value)
        self.assertIn('set-cookie', headers)
        self.assertTrue(self.server.auth.required())
        self.assertEqual(self.browser('POST', '/api/services/mcp', {'enabled': False}, cookie=False)[0], 401)
        self.cookie = headers['set-cookie'].split(';', 1)[0]
        self.assertEqual(self.browser('POST', '/api/services/mcp', {'enabled': False})[0], 200)

    def test_developer_allowlist_does_not_replace_owner_in_explicit_pair_mode(self):
        with mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'pair', 'R1_DEV_NO_PAIRING': '1',
                'R1_DEV_ALLOW_IPS': '127.0.0.0/8', 'R1_DEV_MINUTES': '10'}):
            self.server = fixture.web_config.WebConfigServer(
                self.controller, self.insights, host='127.0.0.1', port=0,
                state_dir=self.server.state_dir / 'legacy-pair').start()
        self.addCleanup(self.server.stop)
        self.host = '127.0.0.1:' + str(self.server.port)
        self.assertTrue(self.server._dev_authorized('127.0.0.1'))
        self.assertEqual(self.server.panel_authorized({}, peer='127.0.0.1'), 'dev')
        for route in ('/api/services', '/api/access'):
            self.assertEqual(self.browser(path=route, cookie=False)[0], 403)
        for route in ('/api/services/mcp', '/api/access/settings'):
            self.assertEqual(self.browser('POST', route, {'enabled': True}, cookie=False)[0], 403)
        self.assertEqual(self.developer(minutes=60)[0], 403)
        self.assertEqual(self.browser('POST', '/api/access/keys', {'name': 'untrusted developer',
            'role': 'admin', 'scopes': ['read'], 'file_roots': [], 'allowed_ips': [],
            'expires_days': 1}, cookie=False)[0], 403)
        self.assertFalse(self.server.api_access.snapshot()['enabled'])
        self.assertEqual(self.browser('POST', '/api/dev/diagnostics',
            {'action': 'catalog'}, cookie=False)[0], 200)
        code = self.server.pairing_info(rotate=True)['code']
        token, status = self.server._pair(code, '127.0.0.1')
        self.assertEqual(status, 200)
        self.cookie = fixture.web_config.COOKIE + '=' + token
        self.assertEqual(self.browser(path='/api/services')[0], 200)
        self.assertEqual(self.browser('POST', '/api/services/mcp', {'enabled': True})[0], 200)


if __name__ == '__main__':
    unittest.main()
