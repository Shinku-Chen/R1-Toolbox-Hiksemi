"""Explicit new developer routes over real HTTP, without NAS provider I/O."""

import http.client
import ipaddress
import json
import time
import unittest
from unittest import mock

import test_dev_diagnostics_http as fixture
import api_file_provider
import vendor_native_capabilities


NATIVE = '/api/dev/native'
DIAGNOSTICS = '/api/dev/diagnostics'
MARKER = 'private-inflight-diagnostic-must-not-leave'


class CompletionDiagnosticsHTTPTests(unittest.TestCase):
    setUp = fixture.DeveloperDiagnosticsHTTPTests.setUp
    _patch = fixture.DeveloperDiagnosticsHTTPTests._patch
    request = fixture.DeveloperDiagnosticsHTTPTests.request
    assert_no_controls = fixture.DeveloperDiagnosticsHTTPTests.assert_no_controls

    def setUp(self):
        fixture.DeveloperDiagnosticsHTTPTests.setUp(self)
        self.native = self._patch(vendor_native_capabilities, 'collect_native_capabilities_probe',
            return_value={'schema_version': 1, 'explicit_only': True, 'items': []})
        self.packages = self._patch(vendor_native_capabilities, 'collect_application_package_context',
            return_value={'schema_version': 1, 'available': True, 'items': [],
                          'trusted_package_manifest_verified': False})
        self.protocol = self._patch(api_file_provider, 'collect_file_protocol_probe',
            return_value={'schema_version': 1, 'available': True,
                          'file_access_available': False, 'roots': []})

    def native_request(self, action='probe', **options):
        return self.request({'action': action, 'confirm': True}, path=NATIVE, **options)

    def protocol_request(self, **options):
        return self.request({'action': 'snapshot', 'checks': ['file_protocol']},
                            path=DIAGNOSTICS, **options)

    def no_new_sources(self):
        self.native.assert_not_called()
        self.packages.assert_not_called()
        self.protocol.assert_not_called()
        self.assert_no_controls()

    def test_default_four_checks_and_catalog_never_start_new_native_queries(self):
        status, _, value = self.request({'action': 'catalog'})
        self.assertEqual(status, 200)
        self.assertIn('file_protocol', [item['id'] for item in value['catalog']['checks']])
        status, _, value = self.request({'action': 'snapshot'})
        self.assertEqual(status, 200)
        self.assertEqual(set(value['snapshot']['checks']), fixture.CHECKS)
        self.no_new_sources()

    def test_native_probe_and_packages_are_separate_fixed_collectors_without_login(self):
        for action, collector in (('probe', self.native), ('packages', self.packages)):
            with self.subTest(action=action):
                status, headers, value = self.native_request(action)
                self.assertEqual(status, 200)
                self.assertTrue(value['ok'])
                self.assertNotIn('set-cookie', headers)
                self.assertNotIn('access_token', json.dumps(value))
                collector.assert_called_once()
                self.assertEqual(set(collector.call_args.kwargs), {'authorizer'})
                current = collector.call_args.kwargs['authorizer']
                self.assertIs(current('fixed-operation', {}, {}), True)
        self.protocol.assert_not_called()
        self.assert_no_controls()

    def test_native_body_is_strict_and_confirmation_is_boolean_true(self):
        cases = ({}, [], {'action': 'probe'}, {'action': 'probe', 'confirm': 1},
                 {'action': 'probe', 'confirm': False}, {'action': 'packages', 'confirm': 'true'},
                 {'action': 'install', 'confirm': True}, {'action': [], 'confirm': True},
                 {'action': 'probe', 'confirm': True, 'url': 'https://example.invalid'},
                 {'action': 'packages', 'confirm': True, 'path': '/etc/shadow'},
                 b'{"action":"probe","action":"packages","confirm":true}')
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.request(body, path=NATIVE)[0], 400)
        self.no_new_sources()

    def test_actual_peer_whitelist_window_and_explicit_developer_mode_are_required(self):
        for route, body in ((NATIVE, {'action': 'probe', 'confirm': True}),
                            (DIAGNOSTICS, {'action': 'snapshot', 'checks': ['file_protocol']})):
            with self.subTest(route=route):
                with mock.patch.object(self.server, 'dev_allow_ips', {ipaddress.ip_address('192.168.31.9')}):
                    self.assertEqual(self.request(body, path=route, headers={
                        'Cookie': self.cookie, 'Authorization': 'Bearer not-an-authority',
                        'X-Forwarded-For': '192.168.31.9'})[0], 403)
                with mock.patch.object(self.server, 'dev_allow_ips', set()):
                    self.assertEqual(self.request(body, path=route)[0], 403)
                with mock.patch.object(self.server, 'dev_until', time.time() - 1):
                    self.assertEqual(self.request(body, path=route)[0], 403)
                with mock.patch.object(self.server, 'dev_no_pairing', False):
                    self.assertEqual(self.request(body, path=route, headers={'Cookie': self.cookie})[0], 403)
        self.no_new_sources()

    def test_origin_json_and_csrf_headers_are_enforced_before_collectors(self):
        for route, body in ((NATIVE, {'action': 'packages', 'confirm': True}),
                            (DIAGNOSTICS, {'action': 'snapshot', 'checks': ['file_protocol']})):
            for changes, expected in (({'remove': ('Origin',)}, 403),
                                      ({'remove': ('X-FNM-Request',)}, 403),
                                      ({'headers': {'Origin': 'http://evil.invalid'}}, 403),
                                      ({'headers': {'Content-Type': 'text/plain'}}, 400)):
                with self.subTest(route=route, changes=changes):
                    self.assertEqual(self.request(body, path=route, **changes)[0], expected)
        self.no_new_sources()

    def test_duplicate_security_headers_queries_and_fragments_are_rejected(self):
        body = {'action': 'probe', 'confirm': True}
        for field, value in (('Host', self.host), ('Origin', 'http://' + self.host),
                             ('X-FNM-Request', '1'), ('Cookie', self.cookie),
                             ('Content-Length', '34'), ('X-R1-CSRF', 'x' * 43)):
            headers = {field: value} if field in ('Cookie', 'X-R1-CSRF') else {}
            self.assertEqual(self.request(body, path=NATIVE, headers=headers,
                                          duplicates=((field, value),))[0], 400)
        for suffix in ('?action=packages', '?path=/etc', '#probe'):
            self.assertEqual(self.request(body, path=NATIVE + suffix)[0], 400)
            self.assertEqual(self.request({'action': 'snapshot', 'checks': ['file_protocol']},
                                          path=DIAGNOSTICS + suffix)[0], 400)
        self.no_new_sources()

    def test_native_lock_shared_with_media_and_released_after_collector_error(self):
        with self.server.media_probe_lock:
            self.assertEqual(self.native_request()[0], 409)
            self.assertEqual(self.native_request('packages')[0], 409)
        self.no_new_sources()
        self.native.side_effect = RuntimeError(MARKER)
        status, _, value = self.native_request()
        self.assertEqual(status, 503)
        self.assertNotIn(MARKER, json.dumps(value))
        self.native.side_effect = None
        self.assertEqual(self.native_request()[0], 200)

    def test_file_protocol_is_explicit_fixed_check_and_shares_storage_lock(self):
        status, _, value = self.protocol_request()
        self.assertEqual(status, 200)
        self.assertEqual(set(value['snapshot']['checks']), {'file_protocol'})
        result = value['snapshot']['checks']['file_protocol']
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['protocol']['file_access_available'])
        self.protocol.assert_called_once()
        self.assertEqual(self.protocol.call_args.args, (self.server.panel,))
        self.assertIs(self.protocol.call_args.kwargs['authorizer'](), True)
        self.protocol.reset_mock()
        with self.server.storage_diagnostic_lock:
            status, _, value = self.protocol_request()
        self.assertEqual(status, 200)
        self.assertEqual(value['snapshot']['checks']['file_protocol']['http_status'], 409)
        self.protocol.assert_not_called()
        self.native.assert_not_called()
        self.packages.assert_not_called()
        self.assert_no_controls()

    def test_expiration_or_whitelist_change_mid_native_call_discards_result(self):
        for action, collector, change in (
                ('probe', self.native, lambda: setattr(self.server, 'dev_until', time.time() - 1)),
                ('packages', self.packages, lambda: setattr(self.server, 'dev_allow_ips', set()))):
            self.server.dev_until = time.time() + 3600
            self.server.dev_allow_ips = {ipaddress.ip_address('127.0.0.1')}
            callbacks = []
            def collect(*, authorizer):
                callbacks.append(authorizer('fixed', {}, {}))
                change()
                callbacks.append(authorizer('fixed', {}, {}))
                return {'private': MARKER}
            collector.side_effect = collect
            status, _, value = self.native_request(action)
            self.assertEqual(status, 403)
            self.assertEqual(callbacks, [True, False])
            self.assertNotIn(MARKER, json.dumps(value))
            self.assertNotIn('probe', value)

    def test_file_protocol_result_is_discarded_after_absolute_window_changes(self):
        def expire(panel, *, authorizer):
            self.assertTrue(authorizer())
            self.server.dev_until = time.time() - 1
            self.assertFalse(authorizer())
            return {'private': MARKER}
        self.protocol.side_effect = expire
        status, _, value = self.protocol_request()
        self.assertEqual(status, 403)
        self.assertNotIn(MARKER, json.dumps(value))
        self.assertNotIn('snapshot', value)
        self.assert_no_controls()

    def test_native_route_has_no_get_cors_or_other_write_verb(self):
        for method in ('GET', 'OPTIONS', 'PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request(None, path=NATIVE, method=method)[0],
                             401 if method == 'GET' else 405)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            connection.request('HEAD', NATIVE, headers={'Host': self.host})
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
            response.read()
        finally:
            connection.close()
        self.no_new_sources()


if __name__ == '__main__':
    unittest.main()
