"""Fixed media probing needs an explicit, narrowly granted HTTP request."""
import concurrent.futures
import http.client
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import web_config


_DEFAULT = object()
ROUTE = '/api/dev/media'
ACTIONS = ('probe', 'probe_all')


class MediaProbeHTTPTests(unittest.TestCase):
    """Use a real HTTP server while isolating every vendor or hardware read."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-media-probe-http-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.controller = mock.Mock(state_dir=directory)
        self.policy = mock.Mock()
        self.insights = mock.Mock()
        self.insights.snapshot.return_value = {'device': {'identity': {
            'name': 'fixture-device', 'hostname': 'fixture-host', 'firmware': {}}}}
        self.insights.storage_name_diagnostics.return_value = {
            'available': True, 'items': []}
        self.result = {'available': True, 'status': 'ok',
                       'native': {'http_status': 200, 'code': 0},
                       'data': {'processed': 3, 'total': 8}}
        probe_patch = mock.patch.object(web_config, 'collect_media_probe',
                                        return_value=self.result)
        self.probe = probe_patch.start()
        self.addCleanup(probe_patch.stop)
        self.batch_result = {'schema_version': 1, 'available': True,
                             'results': [{'id': 'face', 'available': True},
                                         {'id': 'ocr', 'available': False}]}
        batch_patch = mock.patch.object(web_config, 'collect_media_batch',
                                        return_value=self.batch_result)
        self.batch = batch_patch.start()
        self.addCleanup(batch_patch.stop)
        business_patch = mock.patch.object(web_config, 'collect_business_diagnostics',
            return_value={'schema_version': 1, 'categories': {}})
        self.business = business_patch.start()
        self.addCleanup(business_patch.stop)
        environment = {'R1_WEB_AUTH_MODE': 'password', 'R1_DEV_NO_PAIRING': '1',
                       'R1_DEV_ALLOW_IPS': '127.0.0.1', 'R1_DEV_MINUTES': '60',
                       'R1_DEV_ALLOW_COMMANDS': '0'}
        with mock.patch.dict(os.environ, environment):
            self.server = web_config.WebConfigServer(
                self.controller, self.insights, fan_policy=self.policy,
                host='127.0.0.1', port=0, state_dir=directory / 'web',
                version='fixture-media').start()
        self.addCleanup(self.server.stop)
        self.fan = mock.Mock()
        self.fan.snapshot.return_value = {'available': True}
        self.server.fan_diagnostics = self.fan
        login = self.server.auth.setup('fixture-media-password')
        self.assertTrue(login['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + login['sid']
        self.host = '127.0.0.1:' + str(self.server.port)

    def request(self, body=_DEFAULT, *, path=ROUTE, method='POST', headers=None,
                remove=(), duplicates=()):
        """Send exact headers so duplicate-field and duplicate-header gates run."""
        if body is _DEFAULT:
            body = {'action': 'probe'} if method == 'POST' else None
        payload = body if isinstance(body, bytes) else (
            json.dumps(body).encode('utf-8') if body is not None else None)
        values = {'Host': self.host, 'Origin': 'http://' + self.host,
                  'Content-Type': 'application/json', 'X-FNM-Request': '1',
                  'Connection': 'close'}
        if payload is not None:
            values['Content-Length'] = str(len(payload))
        values.update(headers or {})
        for key in remove:
            values.pop(key, None)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in list(values.items()) + list(duplicates):
                connection.putheader(key, value)
            # With no length header HTTP/1.1 has no request body. Sending
            # unframed bytes can turn an early 400 into a Windows TCP reset.
            connection.endheaders(payload if 'Content-Length' in values else None)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            raw = response.read()
            self.assertEqual(response_headers.get('cache-control'), 'no-store')
            self.assertIn('application/json', response_headers.get('content-type', ''))
            self.assertNotIn('access-control-allow-origin', response_headers)
            self.assertNotIn('access-control-allow-methods', response_headers)
            self.assertNotIn('set-cookie', response_headers)
            value = json.loads(raw.decode('utf-8')) if method != 'HEAD' else None
            return response.status, response_headers, value
        finally:
            connection.close()

    def assert_no_controls(self):
        self.controller.action.assert_not_called()
        self.policy.action.assert_not_called()
        self.fan.start.assert_not_called()
        self.insights._smart.assert_not_called()
        self.insights._disks.assert_not_called()

    def assert_no_sources(self):
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.insights.snapshot.assert_not_called()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.business.assert_not_called()
        self.fan.snapshot.assert_not_called()
        self.assert_no_controls()

    def test_explicit_probe_works_with_password_set_without_cookie_or_command_flag(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        status, _, value = self.request()
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertEqual(value['probe'], self.result)
        self.assertNotIn('batch', value)
        self.probe.assert_called_once_with()
        self.batch.assert_not_called()
        self.insights.snapshot.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()

    def test_explicit_batch_works_without_cookie_and_dispatches_only_the_batch(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        status, _, value = self.request({'action': 'probe_all'})
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertEqual(value['batch'], self.batch_result)
        self.assertNotIn('probe', value)
        self.batch.assert_called_once_with()
        self.probe.assert_not_called()
        self.insights.snapshot.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()

    def test_http_success_preserves_unavailable_batch_items(self):
        self.batch_result.update(available=False, status='unavailable')
        status, _, value = self.request({'action': 'probe_all'})
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertEqual(value['batch'], self.batch_result)
        self.assertIs(value['batch']['available'], False)
        self.batch.assert_called_once_with()
        self.probe.assert_not_called()
        self.assert_no_controls()

    def test_http_success_preserves_an_unavailable_vendor_result(self):
        self.result.update(available=False, status='unavailable', data=None)
        status, _, value = self.request()
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertIs(value['probe']['available'], False)
        self.assertEqual(value['probe']['status'], 'unavailable')
        self.assertIsNone(value['probe']['data'])
        self.probe.assert_called_once_with()
        self.batch.assert_not_called()
        self.assert_no_controls()

    def test_flag_exact_peer_and_expiry_are_required_before_collection(self):
        cases = (('dev_no_pairing', False), ('dev_allow_ips', set()),
                 ('dev_allow_ips', {ipaddress.ip_address('192.0.2.15')}),
                 ('dev_until', time.time() - 1))
        for attribute, denied in cases:
            with self.subTest(attribute=attribute), \
                    mock.patch.object(self.server, attribute, denied):
                for action in ACTIONS:
                    with self.subTest(action=action):
                        body = {'action': action}
                        self.assertEqual(self.request(body)[0], 403)
                        self.assertEqual(self.request(body,
                            headers={'Cookie': self.cookie})[0], 403)
        self.assert_no_sources()

    def test_forwarded_headers_and_password_cookie_cannot_replace_actual_peer(self):
        self.server.dev_allow_ips = {ipaddress.ip_address('192.0.2.15')}
        headers = {'Cookie': self.cookie, 'Forwarded': 'for=192.0.2.15',
                   'X-Forwarded-For': '192.0.2.15', 'X-Real-IP': '192.0.2.15'}
        for action in ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.request({'action': action}, headers=headers)[0], 403)
        self.assert_no_sources()

    def test_origin_json_and_csrf_guards_precede_collection(self):
        cases = (({'headers': {'Host': 'foreign.invalid'}}, 403),
                 ({'headers': {'Origin': 'http://foreign.invalid'}}, 403),
                 ({'remove': ('Origin',)}, 403),
                 ({'remove': ('X-FNM-Request',)}, 403),
                 ({'headers': {'X-FNM-Request': '0'}}, 403),
                 ({'headers': {'Cookie': self.cookie, 'X-R1-CSRF': 'x' * 43},
                   'remove': ('X-FNM-Request',)}, 403),
                 ({'headers': {'Content-Type': 'text/plain'}}, 400),
                 ({'remove': ('Content-Type',)}, 400),
                 ({'remove': ('Content-Length',)}, 400))
        for action in ACTIONS:
            for kwargs, expected in cases:
                with self.subTest(action=action, kwargs=kwargs):
                    self.assertEqual(self.request({'action': action}, **kwargs)[0], expected)
        self.assert_no_sources()

    def test_duplicate_security_headers_are_rejected_before_collection(self):
        headers = (('Host', self.host), ('Origin', 'http://' + self.host),
                   ('Cookie', self.cookie), ('Content-Length', '19'),
                   ('Connection', 'close'), ('X-R1-CSRF', 'x' * 43),
                   ('X-FNM-Request', '1'))
        for action in ACTIONS:
            for key, value in headers:
                with self.subTest(action=action, header=key):
                    extra = {key: value} if key in ('Cookie', 'X-R1-CSRF') else {}
                    self.assertEqual(self.request({'action': action}, headers=extra,
                        duplicates=((key, value),))[0], 400)
        self.assert_no_sources()

    def test_malformed_duplicate_and_nonfinite_json_is_rejected(self):
        bodies = (b'{', b'null', b'{"action":"probe","action":"probe"}',
                  b'{"action":"probe_all","action":"probe_all"}',
                  b'{"action":"probe","action":"probe_all"}',
                  b'{"action":NaN}', b'{"action":Infinity}', b'\xff',
                  b'{"action":"probe"}\n{}')
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assert_no_sources()

    def test_request_body_is_exactly_one_of_the_two_fixed_probe_actions(self):
        bodies = ({}, [], 'probe', 1, True, {'action': None}, {'action': []},
                  {'action': {}}, {'action': 1}, {'action': 'probe '},
                  {'action': 'snapshot'}, {'action': 'catalog'},
                  {'action': 'retry'}, {'action': 'querynativefaceprogress'},
                  {'action': 'probe_all '}, {'action': 'PROBE_ALL'},
                  {'action': ['probe', 'probe_all']})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        for action in ACTIONS:
            for field, value in (('sql', 'SELECT token FROM users'),
                                 ('path', '/private/vendor.db'), ('root', '/private'),
                                 ('token', 'private-token'), ('user_id', 1), ('source_id', 1),
                                 ('source', 'custom'), ('confirmed', True),
                                 ('url', 'http://127.0.0.1'), ('cmd', 'id'),
                                 ('command', 'id'), ('timeout', 60), ('ids', ['ocr']),
                                 ('action_ids', ['ocr']), ('actions', ['get_ocr_status']),
                                 ('params', {'album_id': 1}), ('account', 'admin'),
                                 ('host', '127.0.0.1'), ('port', 20443)):
                with self.subTest(action=action, field=field):
                    self.assertEqual(self.request({'action': action, field: value})[0], 400)
        self.assert_no_sources()

    def test_nonempty_url_query_or_fragment_cannot_change_the_probe(self):
        for action in ACTIONS:
            for suffix in ('?action=probe', '?action=probe_all', '?token=private',
                           '?confirmed=1', '#probe', '?=ignored', '?action=probe#other'):
                with self.subTest(action=action, suffix=suffix):
                    self.assertEqual(self.request({'action': action},
                        path=ROUTE + suffix)[0], 400)
        self.assert_no_sources()

    def test_route_is_private_post_only_and_has_no_cors_preflight(self):
        self.assertIn(ROUTE, web_config.PANEL_POST_ROUTES)
        self.assertNotIn(ROUTE, web_config.PANEL_GET_ROUTES)
        self.assertNotIn(ROUTE, web_config.PUBLIC_ROUTES)
        self.assertEqual(self.request(method='GET')[0], 401)
        self.assertEqual(self.request(method='GET', headers={'Cookie': self.cookie})[0], 401)
        for method in ('OPTIONS', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
            with self.subTest(method=method):
                self.assertEqual(self.request(method=method)[0], 405)
        self.assert_no_sources()

    def test_single_and_batch_probes_share_one_nonblocking_lock(self):
        for running in ACTIONS:
            for incoming in ACTIONS:
                with self.subTest(running=running, incoming=incoming):
                    entered, release = threading.Event(), threading.Event()
                    collector = self.probe if running == 'probe' else self.batch
                    other = self.batch if running == 'probe' else self.probe
                    result = self.result if running == 'probe' else self.batch_result

                    def collect():
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('fixture was not released')
                        return result

                    collector.side_effect = collect
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        pending = executor.submit(self.request, {'action': running})
                        try:
                            self.assertTrue(entered.wait(3))
                            status, _, value = self.request({'action': incoming})
                            self.assertEqual(status, 409)
                            self.assertIs(value['ok'], False)
                            collector.assert_called_once_with()
                            other.assert_not_called()
                        finally:
                            release.set()
                        self.assertEqual(pending.result(timeout=5)[0], 200)
                    self.assertFalse(self.server.media_probe_lock.locked())
                    self.probe.reset_mock(side_effect=True)
                    self.batch.reset_mock(side_effect=True)
        self.assert_no_controls()

    def test_each_collector_exception_is_private_and_releases_the_shared_lock(self):
        private = '/private/USER_AUTH_DB_101?token=private-credential'
        for action in ACTIONS:
            with self.subTest(action=action):
                collector = self.probe if action == 'probe' else self.batch
                collector.side_effect = OSError(private)
                status, _, value = self.request({'action': action})
                self.assertEqual(status, 503)
                self.assertIs(value['ok'], False)
                self.assertNotIn(private, json.dumps(value))
                self.assertNotIn('private-credential', json.dumps(value))
                self.assertNotIn('USER_AUTH_DB_101', json.dumps(value))
                self.assertFalse(self.server.media_probe_lock.locked())
                collector.side_effect = None
                for next_action in ACTIONS:
                    self.assertEqual(self.request({'action': next_action})[0], 200)
                self.assertEqual(collector.call_count, 2)
                self.probe.reset_mock(side_effect=True)
                self.batch.reset_mock(side_effect=True)
        self.assert_no_controls()

    def test_passive_diagnostics_and_session_reads_never_probe_media(self):
        for action in ('catalog', 'snapshot'):
            status, _, value = self.request({'action': action}, path='/api/dev/diagnostics')
            self.assertEqual(status, 200)
            self.assertIs(value['ok'], True)
            catalog = value.get('catalog', {})
            self.assertNotIn('media', {item['id'] for item in catalog.get('checks', [])})
            self.assertNotIn('media', value.get('snapshot', {}).get('checks', {}))
        status, _, value = self.request(method='GET', path='/api/auth/session')
        self.assertEqual(status, 200)
        self.assertIs(value['authorized'], False)
        self.assertIs(value['dev_mode'], False)
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.assert_no_controls()

    def test_media_grant_does_not_unlock_old_debug_routes_or_normal_writes(self):
        for action in ACTIONS:
            self.assertEqual(self.request({'action': action})[0], 200)
        # Even enabling old diagnostic commands must not bypass their login gate.
        self.server.dev_commands = True
        for path, body, expected in (
                ('/api/dev/storage', {'action': 'snapshot'}, 401),
                ('/api/dev/business', {'action': 'snapshot'}, 401),
                ('/api/dev/fan', {'action': 'snapshot'}, 401),
                ('/api/dev/fan', {'action': 'retry', 'confirmed': True}, 401),
                ('/api/dev/exec', {'name': 'identity'}, 403),
                ('/api/r1/settings', {'brightness': 50}, 401),
                ('/api/r1/fan', {'mode': 'manual', 'duty': 50}, 401),
                ('/api/r1/power', {'action': 'reboot', 'confirmed': True}, 401)):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request(body, path=path)[0], expected)
        self.probe.assert_called_once_with()
        self.batch.assert_called_once_with()
        self.insights.snapshot.assert_not_called()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.business.assert_not_called()
        self.fan.snapshot.assert_not_called()
        self.assert_no_controls()

    def test_old_debug_routes_still_need_command_flag_with_a_password_session(self):
        for path in ('/api/dev/storage', '/api/dev/business', '/api/dev/fan'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request({'action': 'retry', 'confirmed': True},
            path='/api/dev/fan', headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request({'name': 'identity'}, path='/api/dev/exec',
                                     headers={'Cookie': self.cookie})[0], 403)
        self.assert_no_sources()


if __name__ == '__main__':
    unittest.main()
