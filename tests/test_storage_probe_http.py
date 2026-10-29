"""Explicit storage probing stays behind the fixed developer HTTP boundary.

The real Web server uses a projected backend substitute. No NAS, SMART command,
native session database or upstream HTTP connection is touched by these tests.
"""
import copy
import ipaddress
import json
import time
import unittest
from unittest import mock

import test_dev_diagnostics_http as diagnostic_fixture


web_config = diagnostic_fixture.web_config
ROUTE = '/api/dev/storage/probe'
_DEFAULT = object()
PROBE = {
    'schema_version': 1, 'source': 'histor.space_manage.loopback_http',
    'available': True, 'status': 'ok', 'reason': None, 'attempted': True,
    'http_status': 200, 'native_code': None, 'elapsed_ms': 1,
    'credential_read_only': True, 'native_query_may_update_state': True,
    'data': {'disks': [{'disk_sn': 'fixture-full-serial', 'port': 1,
                        'disk_name': 'sda', 'space_nickname': '测试盘名',
                        'space_name': 'fixture-space', 'location': None, 'conflict': False}],
             'spaces': [{'space_nickname': '测试盘名', 'space_name': 'fixture-space'}]},
}


class StorageProbeHTTPTests(unittest.TestCase):
    def setUp(self):
        # Reuse only fixture setup/request methods, not its test collection.
        diagnostic_fixture.DeveloperDiagnosticsHTTPTests.setUp(self)
        self.real_probe = web_config.collect_storage_probe
        patcher = mock.patch.object(web_config, 'collect_storage_probe',
                                    return_value=copy.deepcopy(PROBE))
        self.probe = patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, body=_DEFAULT, **kwargs):
        kwargs.setdefault('path', ROUTE)
        if body is _DEFAULT:
            body = {'action': 'probe', 'confirm': True}
        return diagnostic_fixture.DeveloperDiagnosticsHTTPTests.request(self, body, **kwargs)

    def test_explicit_confirmed_probe_runs_once_without_password_or_commands(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        before = {str(path.relative_to(self.server.state_dir)): path.read_bytes()
                  for path in self.server.state_dir.rglob('*') if path.is_file()}
        status, headers, value = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(value['probe'], PROBE)
        self.assertNotIn('set-cookie', headers)
        self.probe.assert_called_once_with()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.insights.storage_identity_diagnostics.assert_not_called()
        self.insights._smart.assert_not_called()
        self.insights._disks.assert_not_called()
        self.controller.action.assert_not_called()
        self.assertEqual(before, {str(path.relative_to(self.server.state_dir)): path.read_bytes()
                                 for path in self.server.state_dir.rglob('*') if path.is_file()})
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.probe.call_args_list, [mock.call(), mock.call()])

    def test_real_backend_uses_one_existing_session_substitute_and_returns_serial_without_native_code(self):
        session = mock.Mock(user_id=100000)
        session.derive_access_token.return_value = 'fixture-storage-session'
        reader = mock.Mock(return_value=(session, {
            'available': True, 'status': 'ready', 'user_id': 100000}))
        transport = mock.Mock(return_value=(200, {
            'space_list': [{'space_nickname': '测试盘名', 'space_name': 'fixture-space',
                            'disk_info': [{'disk_sn': 'fixture-full-serial', 'disk_name': 'sda', 'port': 1}]}],
            'unused_disk_list': []}))
        self.probe.side_effect = lambda: self.real_probe(session_reader=reader, transport=transport)
        status, _, value = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertTrue(value['probe']['available'])
        self.assertIsNone(value['probe']['native_code'])
        self.assertEqual(value['probe']['data']['disks'][0]['disk_sn'], 'fixture-full-serial')
        self.assertNotIn('fixture-storage-session', json.dumps(value))
        reader.assert_called_once_with(root='/')
        transport.assert_called_once_with('fixture-storage-session', root='/')
        session.derive_access_token.assert_called_once_with()
        self.probe.assert_called_once_with()

    def test_native_failure_is_a_processed_probe_not_outer_http_success_claim(self):
        value = {**PROBE, 'available': False, 'status': 'native_error',
                 'reason': 'native_request_rejected', 'native_code': -7, 'data': None}
        self.probe.return_value = value
        status, _, response = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(response['ok'])
        self.assertFalse(response['probe']['available'])
        self.assertEqual(response['probe']['native_code'], -7)
        self.assertIsNone(response['probe']['data'])
        self.probe.assert_called_once_with()

    def test_default_catalog_snapshot_and_identity_read_never_run_active_probe(self):
        for body in ({'action': 'catalog'}, {'action': 'snapshot'},
                     {'action': 'snapshot', 'checks': ['storage']},
                     {'action': 'snapshot', 'checks': ['storage_identity']}):
            with self.subTest(body=body):
                status, _, value = self.request(body, path='/api/dev/diagnostics')
                self.assertEqual(status, 200)
                self.assertNotIn(ROUTE, json.dumps(value))
        self.assertEqual(self.request({'action': 'snapshot', 'checks': ['storage_probe']},
                                     path='/api/dev/diagnostics')[0], 400)
        self.probe.assert_not_called()

    def test_confirmation_and_only_exact_fixed_payload_are_required(self):
        invalid = [None, [], 'probe', {}, {'action': 'probe'},
                   {'action': 'snapshot', 'confirm': True},
                   *({'action': 'probe', 'confirm': value}
                     for value in (False, None, 0, 1, 'true', [], {}))]
        for field, argument in (('url', 'http://foreign.invalid'), ('path', '/dev/sda'),
                                ('uid', 100000), ('token', 'private-token'),
                                ('serial', 'override'), ('command', 'smartctl'),
                                ('action_override', 'delete')):
            invalid.append({'action': 'probe', 'confirm': True, field: argument})
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.probe.assert_not_called()

    def test_disabled_expired_and_empty_whitelist_cannot_be_replaced_by_browser_login(self):
        for attribute, denied in (('dev_no_pairing', False), ('dev_allow_ips', set()),
                                 ('dev_until', time.time() - 1)):
            with self.subTest(attribute=attribute), mock.patch.object(self.server, attribute, denied):
                self.assertEqual(self.request(headers={'Cookie': self.cookie})[0], 403)
        self.probe.assert_not_called()

    def test_wrong_actual_peer_cannot_be_replaced_by_forwarded_headers_or_bearer(self):
        with mock.patch.object(self.server, 'dev_allow_ips', {ipaddress.ip_address('192.168.31.9')}):
            status, _, value = self.request(headers={
                'Cookie': self.cookie, 'Authorization': 'Bearer fixture-api-key',
                'X-Forwarded-For': '192.168.31.9', 'X-Real-IP': '192.168.31.9',
                'Forwarded': 'for=192.168.31.9'})
        self.assertEqual(status, 403)
        self.assertNotIn('fixture-api-key', json.dumps(value))
        self.probe.assert_not_called()

    def test_same_origin_json_csrf_and_unique_fields_precede_probe(self):
        cases = [({'headers': {'Origin': 'http://foreign.invalid'}}, 403),
                 ({'headers': {'Host': 'foreign.invalid'}}, 403),
                 ({'remove': ('Origin',)}, 403), ({'remove': ('X-FNM-Request',)}, 403),
                 ({'headers': {'Content-Type': 'text/plain'}}, 400),
                 ({'body': b'{"action":"probe","confirm":true,"confirm":true}'}, 400),
                 ({'body': b'{"action":"probe","confirm":NaN}'}, 400),
                 ({'body': b'{'}, 400)]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(**kwargs)[0], expected)
        self.assertEqual(self.request(duplicates=(('Origin', 'http://' + self.host),))[0], 400)
        self.probe.assert_not_called()

    def test_route_accepts_no_get_url_arguments_fragment_or_cors(self):
        for headers in ({}, {'Cookie': self.cookie}):
            self.assertIn(self.request(None, method='GET', headers=headers)[0], (401, 403, 404, 405))
        self.assertEqual(self.request(None, method='OPTIONS')[0], 405)
        for suffix in ('?action=probe', '?url=http://foreign.invalid', '#probe'):
            self.assertEqual(self.request(path=ROUTE + suffix)[0], 400)
        self.assertIn(ROUTE, web_config.PANEL_POST_ROUTES)
        self.assertNotIn(ROUTE, web_config.PANEL_GET_ROUTES)
        self.assertNotIn(ROUTE, web_config.PUBLIC_ROUTES)
        self.probe.assert_not_called()

    def test_shared_storage_lock_rejects_busy_without_backend_call(self):
        with self.server.storage_diagnostic_lock:
            self.assertEqual(self.request()[0], 409)
        self.probe.assert_not_called()

        def check_lock():
            self.assertTrue(self.server.storage_diagnostic_lock.locked())
            self.assertEqual(self.server._storage_debug_snapshot()[0], 409)
            return copy.deepcopy(PROBE)

        self.probe.side_effect = check_lock
        self.assertEqual(self.request()[0], 200)
        self.insights.storage_name_diagnostics.assert_not_called()
        self.assertFalse(self.server.storage_diagnostic_lock.locked())

    def test_developer_window_is_checked_again_after_acquiring_source_lock(self):
        lock = mock.Mock()

        def expire(**kwargs):
            self.server.dev_until = time.time() - 1
            return True

        lock.acquire.side_effect = expire
        with mock.patch.object(self.server, 'storage_diagnostic_lock', lock):
            self.assertEqual(self.request()[0], 403)
        self.probe.assert_not_called()
        lock.acquire.assert_called_once_with(blocking=False)
        lock.release.assert_called_once_with()

    def test_uninitialized_malformed_and_exceptional_sources_fail_closed(self):
        with mock.patch.object(web_config, 'collect_storage_probe', None):
            self.assertEqual(self.request()[0], 503)
        self.probe.assert_not_called()
        for malformed in (None, [], {}, {'available': 1}):
            with self.subTest(value=malformed):
                self.probe.return_value = malformed
                self.assertEqual(self.request()[0], 503)
                self.assertFalse(self.server.storage_diagnostic_lock.locked())
        self.probe.side_effect = OSError('/private/database token=private-secret')
        status, _, value = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn('/private', json.dumps(value))
        self.assertNotIn('private-secret', json.dumps(value))
        self.assertFalse(self.server.storage_diagnostic_lock.locked())


if __name__ == '__main__':
    unittest.main()
