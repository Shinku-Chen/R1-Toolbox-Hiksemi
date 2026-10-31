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
FIXED_ACTIONS = ACTIONS + ('probe_listener',)


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
        self.transcode_result = {'schema_version': 1, 'available': True, 'status': 'ok',
                                 'reason': 'definitions_available', 'steps': {},
                                 'tv_player': {'playback': 'hls', 'labels': ['原画', '高清']}}
        transcode_patch = mock.patch.object(web_config, 'collect_transcode_probe',
                                            return_value=self.transcode_result)
        self.transcode = transcode_patch.start()
        self.addCleanup(transcode_patch.stop)
        self.listener_result = {'schema_version': 1, 'available': True, 'status': 'ok',
                                'reason': 'listener_verified', 'diagnostics': {}}
        listener_patch = mock.patch.object(web_config, 'collect_listener_preflight',
                                           return_value=self.listener_result)
        self.listener = listener_patch.start()
        self.addCleanup(listener_patch.stop)
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
        self.transcode.assert_not_called()
        self.listener.assert_not_called()
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
                for action in FIXED_ACTIONS:
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
        for action in FIXED_ACTIONS:
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
        for action in FIXED_ACTIONS:
            for kwargs, expected in cases:
                with self.subTest(action=action, kwargs=kwargs):
                    self.assertEqual(self.request({'action': action}, **kwargs)[0], expected)
        self.assert_no_sources()

    def test_duplicate_security_headers_are_rejected_before_collection(self):
        headers = (('Host', self.host), ('Origin', 'http://' + self.host),
                   ('Cookie', self.cookie), ('Content-Length', '19'),
                   ('Connection', 'close'), ('X-R1-CSRF', 'x' * 43),
                   ('X-FNM-Request', '1'))
        for action in FIXED_ACTIONS:
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

    def test_request_body_is_one_of_the_strict_fixed_probe_actions(self):
        bodies = ({}, [], 'probe', 1, True, {'action': None}, {'action': []},
                  {'action': {}}, {'action': 1}, {'action': 'probe '},
                  {'action': 'snapshot'}, {'action': 'catalog'},
                  {'action': 'retry'}, {'action': 'querynativefaceprogress'},
                  {'action': 'probe_all '}, {'action': 'PROBE_ALL'},
                  {'action': ['probe', 'probe_all']})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        for action in FIXED_ACTIONS:
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
        for action in FIXED_ACTIONS:
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
        self.listener.assert_not_called()
        self.transcode.assert_not_called()
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


    def test_explicit_business_probe_has_fixed_action_and_confirmation(self):
        with mock.patch('vendor_media_operations.collect_media_operations_probe', return_value={
                'items': [], 'may_wake_disks': True, 'native_query_may_update_state': True}) as provider:
            for body in ({'action': 'probe_business'}, {'action': 'probe_business', 'confirm': 1},
                         {'action': 'probe_business', 'confirm': True, 'album_id': '8'}):
                self.assertEqual(400, self.request(body)[0])
            provider.assert_not_called()
            status, _, value = self.request({'action': 'probe_business', 'confirm': True})
            self.assertEqual(200, status)
            self.assertTrue(value['probe']['may_wake_disks'])
            self.assertEqual(set(provider.call_args.kwargs), {'authorizer'})
            self.assertTrue(provider.call_args.kwargs['authorizer']('albums_list', {}, {}))
        self.assert_no_sources()

    def test_business_probe_discards_results_when_window_expires(self):
        def expire(**kwargs):
            self.server.dev_until = time.time() - 1
            self.assertFalse(kwargs['authorizer']('albums_list', {}, {}))
            return {'private-marker': 'never-disclose'}
        with mock.patch('vendor_media_operations.collect_media_operations_probe', side_effect=expire):
            status, _, value = self.request({'action': 'probe_business', 'confirm': True})
        self.assertEqual(403, status)
        self.assertNotIn('never-disclose', json.dumps(value))
        self.assert_no_sources()

    def test_transcode_probe_passes_one_admin_file_path_to_one_fixed_collector(self):
        path = '/drives/sda/Users/admin/Movies/fixture.mkv'
        status, _, value = self.request({'action': 'probe_transcode', 'path': path,
                                         'confirm': True})
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertEqual(value['transcode'], self.transcode_result)
        self.assertEqual(self.transcode.call_count, 1)
        called = self.transcode.call_args
        self.assertEqual(called.args, (path,))
        self.assertEqual(set(called.kwargs), {'authorizer'})
        self.assertTrue(called.kwargs['authorizer']('video_capacity', {}, {}))
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()

    def test_transcode_probe_rejects_every_other_path_or_body(self):
        bodies = ({'action': 'probe_transcode'},
                  {'action': 'probe_transcode', 'path': ''},
                  {'action': 'probe_transcode', 'path': 1},
                  {'action': 'probe_transcode', 'path': None},
                  {'action': 'probe_transcode', 'path': '/etc/passwd'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/public/x.mkv'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/../x.mkv'},
                  {'action': 'probe_transcode', 'path': 'relative/x.mkv'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'sharpness': 'high'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'offset': 0},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'terminal': 'private-terminal'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv'},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'confirm': False},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'confirm': 1},
                  {'action': 'probe_transcode', 'path': '/drives/sda/Users/admin/x.mkv',
                   'confirm': True, 'force': True})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assert_no_sources()

    def test_transcode_probe_shares_the_media_lock_with_the_other_probes(self):
        path = '/drives/sda/Users/admin/Movies/fixture.mkv'
        self.assertTrue(self.server.media_probe_lock.acquire(blocking=False))
        try:
            status, _, value = self.request({'action': 'probe_transcode', 'path': path,
                                             'confirm': True})
            self.assertEqual(status, 409)
            self.assertIs(value['ok'], False)
        finally:
            self.server.media_probe_lock.release()
        self.assertFalse(self.server.media_probe_lock.locked())
        self.assertEqual(self.request({'action': 'probe_transcode', 'path': path,
                                       'confirm': True})[0], 200)
        self.transcode.assert_called_once()

    def test_transcode_probe_discards_result_when_window_expires(self):
        def expire(*args, **kwargs):
            self.server.dev_until = time.time() - 1
            self.assertFalse(kwargs['authorizer']('video_capacity', {}, {}))
            return {'private-marker': 'never-disclose', 'available': True}
        with mock.patch.object(web_config, 'collect_transcode_probe', side_effect=expire):
            status, _, value = self.request({'action': 'probe_transcode',
                'path': '/drives/sda/Users/admin/Movies/fixture.mkv', 'confirm': True})
        self.assertEqual(status, 403)
        self.assertNotIn('never-disclose', json.dumps(value))

    def test_listener_probe_is_one_fixed_collection_without_native_sources(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        status, _, value = self.request({'action': 'probe_listener'})
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        self.assertEqual(value['listener'], self.listener_result)
        self.assertNotIn('transcode', value)
        self.listener.assert_called_once()
        called = self.listener.call_args
        self.assertEqual(called.args, ())
        self.assertEqual(set(called.kwargs), {'authorizer'})
        self.assertIs(called.kwargs['authorizer']('probe_listener', {}, {}), True)
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.transcode.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()

    def test_listener_probe_rejects_all_callsite_target_and_confirmation_fields(self):
        for key, value in (('pid', 7506), ('port', 20443), ('path', '/private/media'),
                           ('root', '/tmp/proc'), ('command', 'id'), ('token', 'secret'),
                           ('action_id', 'get_offlinevideo_path'), ('confirm', True),
                           ('timeout', 60), ('observer', {}), ('client', {})):
            with self.subTest(field=key):
                self.assertEqual(self.request({'action': 'probe_listener', key: value})[0], 400)
        self.assertEqual(self.request(b'{"action":"probe_listener","action":"probe_listener"}')[0], 400)
        self.assert_no_sources()

    def test_listener_probe_rejects_bearer_even_when_developer_window_is_valid(self):
        for authorization in ('Bearer private-listener-key', 'Basic private-listener-key', ''):
            with self.subTest(authorization=authorization):
                self.assertEqual(self.request({'action': 'probe_listener'},
                    headers={'Authorization': authorization})[0], 403)
        self.assert_no_sources()

    def test_listener_probe_shares_the_media_lock_and_preserves_unavailable_results(self):
        self.assertTrue(self.server.media_probe_lock.acquire(blocking=False))
        try:
            self.assertEqual(self.request({'action': 'probe_listener'})[0], 409)
        finally:
            self.server.media_probe_lock.release()
        self.listener.assert_not_called()
        self.listener_result.update(available=False, status='unavailable')
        status, _, value = self.request({'action': 'probe_listener'})
        self.assertEqual(status, 200)
        self.assertIs(value['listener']['available'], False)
        self.assertEqual(value['listener']['status'], 'unavailable')
        self.listener.assert_called_once()

    def test_listener_probe_rechecks_developer_window_after_lock_acquisition(self):
        def acquire(*args, **kwargs):
            self.server.dev_until = time.time() - 1
            return True
        lock = mock.Mock()
        lock.acquire.side_effect = acquire
        with mock.patch.object(self.server, 'media_probe_lock', lock):
            status, _, value = self.request({'action': 'probe_listener'})
        self.assertEqual(status, 403)
        self.assertIs(value['ok'], False)
        lock.release.assert_called_once_with()
        self.assert_no_sources()

    def test_listener_probe_drops_complete_result_after_window_revocation(self):
        def expire(**kwargs):
            self.server.dev_until = time.time() - 1
            self.assertIs(kwargs['authorizer']('probe_listener', {}, {}), False)
            return {'private-marker': 'listener-private-sentinel', 'available': True}
        self.listener.side_effect = expire
        status, _, value = self.request({'action': 'probe_listener'})
        self.assertEqual(status, 403)
        self.assertNotIn('listener-private-sentinel', json.dumps(value))
        self.assertNotIn('listener', value)
        self.assertFalse(self.server.media_probe_lock.locked())

    def test_listener_probe_does_not_expose_exceptions_and_releases_the_lock(self):
        self.listener.side_effect = OSError('/private/secret-listener-path?token=private-key')
        status, _, value = self.request({'action': 'probe_listener'})
        self.assertEqual(status, 503)
        self.assertNotIn('secret-listener-path', json.dumps(value))
        self.assertNotIn('private-key', json.dumps(value))
        self.assertFalse(self.server.media_probe_lock.locked())
        self.listener.side_effect = None
        self.assertEqual(self.request({'action': 'probe_listener'})[0], 200)

    def guarded_media_cases(self):
        return (({'action': 'probe_listener'}, self.listener, 'listener'),
                ({'action': 'probe_transcode',
                  'path': '/drives/sda/Users/admin/Movies/fixture.mkv',
                  'confirm': True}, self.transcode, 'transcode'))

    def test_fixed_media_response_rechecks_window_after_real_json_serialization(self):
        original = web_config.json.dumps
        for body, provider, field in self.guarded_media_cases():
            with self.subTest(action=body['action']):
                self.server.dev_until = time.time() + 60
                provider.return_value = {'private-marker': 'serialized-media-must-not-escape'}
                serialized = []

                def expire(value, *args, **kwargs):
                    encoded = original(value, *args, **kwargs)
                    if isinstance(value, dict) and field in value:
                        serialized.append(True)
                        self.server.dev_until = time.time() - 1
                    return encoded

                with mock.patch.object(web_config.json, 'dumps', side_effect=expire):
                    status, _, value = self.request(body)
                self.assertEqual(serialized, [True])
                self.assertEqual(status, 403)
                self.assertEqual(value['code'], 'development_authorization_expired')
                self.assertNotIn(field, value)
                self.assertNotIn('serialized-media-must-not-escape', json.dumps(value))
                self.assertNotIn('网页登录', value['error'])
                self.assertFalse(self.server.media_probe_lock.locked())
        self.assertEqual(self.listener.call_count, 1)
        self.assertEqual(self.transcode.call_count, 1)
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()

    def test_response_guard_latches_first_false_even_if_window_recovers_during_encoding(self):
        original_dump = web_config.json.dumps
        original_factory = self.server._media_debug_authorizer
        for body, provider, field in self.guarded_media_cases():
            with self.subTest(action=body['action']):
                self.server.dev_until = time.time() + 60
                provider.return_value = {'private-marker': 'restored-window-must-not-revive-data'}
                guards, checked = [], []

                def capture(peer, *, generation=None):
                    guard = original_factory(peer, generation=generation)
                    guards.append(guard)
                    return guard

                def revoke_restore(value, *args, **kwargs):
                    encoded = original_dump(value, *args, **kwargs)
                    if isinstance(value, dict) and field in value:
                        self.assertEqual(len(guards), 1)
                        self.server.dev_until = time.time() - 1
                        self.assertFalse(guards[0]())
                        self.server.dev_until = time.time() + 60
                        self.assertTrue(self.server._dev_authorized('127.0.0.1'))
                        self.assertFalse(guards[0]())
                        checked.append(True)
                    return encoded

                with mock.patch.object(self.server, '_media_debug_authorizer', side_effect=capture), \
                        mock.patch.object(web_config.json, 'dumps', side_effect=revoke_restore):
                    status, _, value = self.request(body)
                self.assertEqual(checked, [True])
                self.assertEqual(status, 403)
                self.assertEqual(value['code'], 'development_authorization_expired')
                self.assertNotIn(field, value)
                self.assertNotIn('restored-window-must-not-revive-data', json.dumps(value))
                self.assertFalse(guards[0]())
        self.assertEqual(self.listener.call_count, 1)
        self.assertEqual(self.transcode.call_count, 1)
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.assert_no_controls()

    def test_collector_authorization_false_is_sticky_after_window_is_restored(self):
        for body, provider, field in self.guarded_media_cases():
            with self.subTest(action=body['action']):
                self.server.dev_until = time.time() + 60
                checked = []

                def revoke_restore(*args, **kwargs):
                    current = kwargs['authorizer']
                    self.assertTrue(current('probe_listener', {}, {}))
                    self.server.dev_until = time.time() - 1
                    self.assertFalse(current('probe_listener', {}, {}))
                    self.server.dev_until = time.time() + 60
                    self.assertTrue(self.server._dev_authorized('127.0.0.1'))
                    self.assertFalse(current('probe_listener', {}, {}))
                    checked.append(True)
                    return {'private-marker': 'collected-under-revoked-window'}

                provider.side_effect = revoke_restore
                status, _, value = self.request(body)
                provider.side_effect = None
                self.assertEqual(checked, [True])
                self.assertEqual(status, 403)
                self.assertNotIn(field, value)
                self.assertNotIn('collected-under-revoked-window', json.dumps(value))
                self.assertFalse(self.server.media_probe_lock.locked())
        self.assertEqual(self.listener.call_count, 1)
        self.assertEqual(self.transcode.call_count, 1)
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.assert_no_controls()

    def test_fixed_media_guard_uses_real_peer_even_with_owner_cookie_and_forwarded_headers(self):
        self.server.dev_allow_ips = {ipaddress.ip_address('192.0.2.15')}
        headers = {'Cookie': self.cookie, 'Forwarded': 'for=192.0.2.15',
                   'X-Forwarded-For': '192.0.2.15', 'X-Real-IP': '192.0.2.15'}
        for body, _, field in self.guarded_media_cases():
            with self.subTest(action=body['action']):
                status, _, value = self.request(body, headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(field, value)
        self.assert_no_sources()

    def test_transcode_probe_rejects_bearer_instead_of_inheriting_developer_grant(self):
        body = {'action': 'probe_transcode',
                'path': '/drives/sda/Users/admin/Movies/fixture.mkv', 'confirm': True}
        for authorization in ('Bearer private-transcode-key', 'Basic private-transcode-key', ''):
            with self.subTest(kind=authorization.split(' ')[0]):
                status, _, value = self.request(body, headers={'Authorization': authorization})
                self.assertEqual(status, 403)
                self.assertNotIn('transcode', value)
        self.assert_no_sources()

    def test_transcode_probe_rechecks_developer_window_after_lock_acquisition(self):
        def acquire(*args, **kwargs):
            self.server.dev_until = time.time() - 1
            return True

        lock = mock.Mock()
        lock.acquire.side_effect = acquire
        with mock.patch.object(self.server, 'media_probe_lock', lock):
            status, _, value = self.request({'action': 'probe_transcode',
                'path': '/drives/sda/Users/admin/Movies/fixture.mkv', 'confirm': True})
        self.assertEqual(status, 403)
        self.assertIs(value['ok'], False)
        lock.release.assert_called_once_with()
        self.assert_no_sources()

    def test_fixed_media_guard_cannot_outlive_its_original_absolute_developer_window(self):
        now = [time.time()]
        with mock.patch.object(web_config.time, 'time', side_effect=lambda: now[0]):
            self.server.dev_until = now[0] + 1
            guard = self.server._media_debug_authorizer('127.0.0.1')
            self.assertTrue(guard())
            self.server.dev_until = now[0] + 60
            now[0] += 2
            self.assertTrue(self.server._dev_authorized('127.0.0.1'))
            self.assertFalse(guard(), 'an in-flight request must not inherit an extended grant')
            self.assertFalse(guard())
        self.assert_no_sources()

    def test_pending_true_authorization_cannot_clear_a_concurrent_false_latch(self):
        guard = self.server._media_debug_authorizer('127.0.0.1')
        self.assertTrue(guard())
        entered, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()

        def grant(peer):
            self.assertEqual(peer, '127.0.0.1')
            if threading.get_ident() != main_thread:
                entered.set()
                release.wait(timeout=2)
                return True
            return False

        with mock.patch.object(self.server, '_dev_authorized', side_effect=grant), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(guard)
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertFalse(guard())
            finally:
                release.set()
            self.assertFalse(pending.result(timeout=2))
        self.assertTrue(self.server._dev_authorized('127.0.0.1'))
        self.assertFalse(guard())
        self.assert_no_sources()

    def test_stop_start_never_revives_fixed_media_request_authorizers(self):
        for body, provider, _ in self.guarded_media_cases():
            with self.subTest(action=body['action']):
                self.host = '127.0.0.1:' + str(self.server.port)
                status, _, _ = self.request(body)
                self.assertEqual(status, 200)
                current = provider.call_args.kwargs['authorizer']
                self.assertTrue(current('probe_listener', {}, {}))
                self.server.stop()
                self.assertFalse(current('probe_listener', {}, {}))
                self.server.start()
                self.assertTrue(self.server._dev_authorized('127.0.0.1'))
                self.assertFalse(current('probe_listener', {}, {}))
        self.probe.assert_not_called()
        self.batch.assert_not_called()
        self.business.assert_not_called()
        self.assert_no_controls()


if __name__ == '__main__':
    unittest.main()
