"""Fixed opt-in write-preflight HTTP gates, with no NAS or issued API Token.

Only the collector is substituted. Real sockets are isolated local Web fixture
requests; no native listener, storage database or user directory is opened.
"""
import copy
import ipaddress
import json
import time
import unittest
from unittest import mock

import test_dev_diagnostics_http as diagnostic_fixture


web_config = diagnostic_fixture.web_config
ROUTE = '/api/dev/files/write-preflight'
ROOT = 'root_' + 'a' * 24
_DEFAULT = object()
BODY = {'action': 'preflight', 'confirm': True, 'root_id': ROOT}
REPORT = {
    'schema_version': 1, 'scope': 'native_index_preflight', 'root_id': ROOT,
    'ready': False, 'error_code': 'write_backend_unverified',
    'failure_stage': 'index.reader', 'stages': [], 'trace_schema_valid': True,
    'write_acceptance_proven': False, 'file_published': False,
    'native_notification_sent': False, 'may_wake_disks': True,
    'sqlite_sidecar_metadata_may_change': True,
}


class FileWritePreflightHTTPTests(unittest.TestCase):
    def setUp(self):
        diagnostic_fixture.DeveloperDiagnosticsHTTPTests.setUp(self)
        patcher = mock.patch.object(web_config, 'collect_file_write_preflight',
                                    return_value=copy.deepcopy(REPORT))
        self.collector = patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, body=_DEFAULT, **kwargs):
        kwargs.setdefault('path', ROUTE)
        if body is _DEFAULT:
            body = copy.deepcopy(BODY)
        return diagnostic_fixture.DeveloperDiagnosticsHTTPTests.request(self, body, **kwargs)

    def test_explicit_report_uses_live_peer_authorizer_without_session_or_command_permission(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        before = {str(path.relative_to(self.server.state_dir)): path.read_bytes()
                  for path in self.server.state_dir.rglob('*') if path.is_file()}
        status, headers, value = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(value, {'ok': True, 'preflight': REPORT})
        self.assertFalse(value['preflight']['ready'])
        self.assertNotIn('set-cookie', headers)
        call = self.collector.call_args
        self.assertEqual(call.args, (self.server.panel, ROOT))
        self.assertEqual(set(call.kwargs), {'authorizer'})
        current = call.kwargs['authorizer']
        self.assertIs(current(), True)
        with mock.patch.object(self.server, 'dev_allow_ips', {ipaddress.ip_address('192.168.31.9')}):
            self.assertIs(current(), False)
        with mock.patch.object(self.server, 'dev_until', time.time() - 1):
            self.assertIs(current(), False)
        self.assertEqual(before, {str(path.relative_to(self.server.state_dir)): path.read_bytes()
                                 for path in self.server.state_dir.rglob('*') if path.is_file()})
        self.assertEqual(self.collector.call_count, 1)
        self.insights._smart.assert_not_called()
        self.insights._disks.assert_not_called()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.controller.action.assert_not_called()

    def test_ready_false_and_ready_true_are_processed_reports_not_implicit_execution(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                report = {**REPORT, 'ready': ready,
                          'error_code': None if ready else REPORT['error_code']}
                self.collector.return_value = report
                status, headers, value = self.request()
                self.assertEqual(status, 200)
                self.assertEqual(value, {'ok': True, 'preflight': report})
                self.assertNotIn('set-cookie', headers)
        self.assertEqual(self.collector.call_count, 2)
        self.controller.action.assert_not_called()

    def test_only_exact_payload_and_true_boolean_confirmation_are_allowed(self):
        invalid = [None, [], 'preflight', {}, {'action': 'preflight'},
                   {key: value for key, value in BODY.items() if key != 'root_id'},
                   {**BODY, 'action': 'snapshot'}, {**BODY, 'action': 'initialize'},
                   *({**BODY, 'confirm': value} for value in (False, None, 0, 1, 'true', [], {})),
                   *({**BODY, 'root_id': value} for value in (
                       None, True, 1, [], {}, '', '/drives/data/Users/admin',
                       'root_' + 'a' * 23, 'root_' + 'a' * 25, 'root_' + 'A' * 24,
                       ROOT + '\n', ROOT + '/file'))]
        for name, value in (('path', 'relative/file.bin'), ('op_id', 'operation_' + 'b' * 32),
                            ('operation_id', 'operation_' + 'b' * 32), ('sql', 'SELECT private'),
                            ('url', 'http://foreign.invalid'), ('token', 'private-fixture'),
                            ('command', 'arbitrary'), ('root', '/'), ('action_override', 'mkdir')):
            invalid.append({**BODY, name: value})
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.collector.assert_not_called()

    def test_browser_login_does_not_replace_enabled_nonempty_unexpired_developer_grant(self):
        for attribute, denied in (('dev_no_pairing', False), ('dev_allow_ips', set()),
                                 ('dev_until', time.time() - 1)):
            with self.subTest(attribute=attribute), mock.patch.object(self.server, attribute, denied):
                status, _, value = self.request(headers={'Cookie': self.cookie})
                self.assertEqual(status, 403)
                self.assertNotIn('preflight', value)
        self.collector.assert_not_called()

    def test_real_peer_cannot_be_substituted_by_proxy_headers_or_browser_cookie(self):
        with mock.patch.object(self.server, 'dev_allow_ips', {ipaddress.ip_address('192.168.31.9')}):
            status, _, value = self.request(headers={
                'Cookie': self.cookie, 'X-Forwarded-For': '192.168.31.9',
                'X-Real-IP': '192.168.31.9', 'Forwarded': 'for=192.168.31.9'})
        self.assertEqual(status, 403)
        self.assertNotIn('preflight', value)
        self.collector.assert_not_called()

    def test_authorization_header_is_rejected_even_for_a_live_allowed_developer_peer(self):
        self.assertTrue(self.server._dev_authorized('127.0.0.1'))
        with mock.patch.object(self.server.api_access, 'authorize') as machine_authorize:
            for authorization in ('Bearer fixture-not-issued', 'Basic fixture-not-issued', ''):
                with self.subTest(authorization=authorization):
                    status, _, value = self.request(headers={
                        'Cookie': self.cookie, 'Authorization': authorization})
                    self.assertEqual(status, 403)
                    self.assertNotIn('fixture-not-issued', json.dumps(value))
            machine_authorize.assert_not_called()
        self.collector.assert_not_called()

    def test_same_origin_json_csrf_and_duplicate_json_fields_precede_collection(self):
        duplicate = ('{"action":"preflight","confirm":true,"root_id":"' + ROOT
                     + '","root_id":"' + ROOT + '"}').encode('ascii')
        cases = [({'headers': {'Origin': 'http://foreign.invalid'}}, 403),
                 ({'headers': {'Host': 'foreign.invalid'}}, 403),
                 ({'remove': ('Origin',)}, 403), ({'remove': ('X-FNM-Request',)}, 403),
                 ({'headers': {'Content-Type': 'text/plain'}}, 400),
                 ({'body': duplicate}, 400),
                 ({'body': b'{"action":"preflight","confirm":NaN}'}, 400),
                 ({'body': b'{'}, 400)]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(**kwargs)[0], expected)
        self.assertEqual(self.request(duplicates=(('Origin', 'http://' + self.host),))[0], 400)
        self.collector.assert_not_called()

    def test_route_rejects_get_options_query_and_fragment_without_cors_or_cookies(self):
        for headers in ({}, {'Cookie': self.cookie}):
            self.assertIn(self.request(None, method='GET', headers=headers)[0], (401, 403, 404, 405))
        self.assertEqual(self.request(None, method='OPTIONS')[0], 405)
        for suffix in ('?action=preflight', '?path=relative/file.bin', '#preflight'):
            with self.subTest(suffix=suffix):
                status, headers, _ = self.request(path=ROUTE + suffix)
                self.assertEqual(status, 400)
                self.assertNotIn('set-cookie', headers)
        self.assertIn(ROUTE, web_config.PANEL_POST_ROUTES)
        self.assertNotIn(ROUTE, web_config.PANEL_GET_ROUTES)
        self.assertNotIn(ROUTE, web_config.PUBLIC_ROUTES)
        self.assertNotIn(ROUTE, web_config.MACHINE_ROUTES)
        self.collector.assert_not_called()

    def test_shared_storage_lock_is_nonblocking_and_held_through_backend(self):
        with self.server.storage_diagnostic_lock:
            self.assertEqual(self.request()[0], 409)
        self.collector.assert_not_called()

        def collect(panel, root_id, *, authorizer):
            self.assertIs(panel, self.server.panel)
            self.assertEqual(root_id, ROOT)
            self.assertTrue(self.server.storage_diagnostic_lock.locked())
            self.assertIs(authorizer(), True)
            self.assertEqual(self.server._storage_debug_snapshot()[0], 409)
            return copy.deepcopy(REPORT)
        self.collector.side_effect = collect
        self.assertEqual(self.request()[0], 200)
        self.assertFalse(self.server.storage_diagnostic_lock.locked())
        self.insights.storage_name_diagnostics.assert_not_called()

    def test_developer_window_is_checked_after_acquiring_storage_lock(self):
        lock = mock.Mock()
        def expire(**kwargs):
            self.server.dev_until = time.time() - 1
            return True
        lock.acquire.side_effect = expire
        with mock.patch.object(self.server, 'storage_diagnostic_lock', lock):
            self.assertEqual(self.request()[0], 403)
        self.collector.assert_not_called()
        lock.acquire.assert_called_once_with(blocking=False)
        lock.release.assert_called_once_with()

    def test_returned_report_is_discarded_when_developer_window_expires_in_backend(self):
        def expire(panel, root_id, *, authorizer):
            self.assertIs(authorizer(), True)
            self.server.dev_until = time.time() - 1
            self.assertIs(authorizer(), False)
            return copy.deepcopy(REPORT)
        self.collector.side_effect = expire
        status, headers, value = self.request()
        self.assertEqual(status, 403)
        self.assertNotIn('preflight', value)
        self.assertNotIn('write_backend_unverified', json.dumps(value))
        self.assertNotIn('set-cookie', headers)
        self.assertFalse(self.server.storage_diagnostic_lock.locked())

    def test_unavailable_or_exceptional_backend_returns_fixed_chinese_503_and_releases_lock(self):
        with mock.patch.object(web_config, 'collect_file_write_preflight', None):
            status, _, value = self.request()
            self.assertEqual(status, 503)
            self.assertIs(value['ok'], False)
            self.assertFalse(self.server.storage_diagnostic_lock.locked())
        self.collector.assert_not_called()
        self.collector.side_effect = OSError('/private/database token=private-secret')
        status, headers, value = self.request()
        self.assertEqual(status, 503)
        self.assertIs(value['ok'], False)
        self.assertNotIn('/private', json.dumps(value))
        self.assertNotIn('private-secret', json.dumps(value))
        self.assertRegex(json.dumps(value, ensure_ascii=False), '[\u4e00-\u9fff]')
        self.assertNotIn('set-cookie', headers)
        self.assertFalse(self.server.storage_diagnostic_lock.locked())

    def test_default_diagnostics_owner_api_and_machine_catalog_never_run_write_preflight(self):
        for body in ({'action': 'catalog'}, {'action': 'snapshot'},
                     {'action': 'snapshot', 'checks': ['storage']}):
            self.assertEqual(self.request(body, path='/api/dev/diagnostics')[0], 200)
        self.assertEqual(self.request({'action': 'snapshot', 'checks': ['file_write_preflight']},
                                     path='/api/dev/diagnostics')[0], 400)
        for path in ('/api/r1', '/api/access/contracts'):
            self.assertEqual(self.request(None, method='GET', path=path,
                headers={'Cookie': self.cookie})[0], 200)
        # Constructor-only authentication substitute: no key/token is minted.
        principal = {'key_id': 'b' * 24, 'grant_revision': 1, 'credential_generation': 1,
            'role': 'readonly', 'policy_version': 3, 'scopes': ['read'],
            'tool_grants': ['get_nas_status'], 'file_roots': [],
            'docker_projects': [], 'docker_deployments': [], 'application_ids': []}
        with mock.patch.object(self.server.api_access, 'authorize', return_value=(principal, None)), \
             mock.patch.object(self.server.api_access, 'is_current', return_value=True), \
             mock.patch.object(self.server.integration, 'record'):
            status, _, value = self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}},
                path='/mcp', headers={'Authorization': 'Bearer constructor-only-substitute',
                    'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-03-26'})
        self.assertEqual(status, 200)
        self.assertEqual({tool['name'] for tool in value['result']['tools']}, {'get_nas_status'})
        self.collector.assert_not_called()


if __name__ == '__main__':
    unittest.main()
