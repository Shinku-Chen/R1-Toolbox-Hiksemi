"""Business snapshots distinguish cached metadata, live data and observations."""
import json
import http.client
import ipaddress
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import vendor_business
import web_config


class BusinessSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='r1-business-test-'))
        self.cache = self.root / 'tmp/nand_config/app_center.conf'
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({'count': 8, 'list': [
            {'module_name': 'fixture-app', 'version': '1.0', 'status': 4,
             'token': 'do-not-export', 'path': '/private/user/path'}]}), encoding='utf-8')
        self.observation = {'available': True, 'complete': True, 'processes': [
            {'pid': 101, 'name': 'intelligent', 'exe_name': 'intelligent', 'start_ticks': 123,
             'identity_confirmed': True, 'cmdline': 'do-not-export'},
            {'pid': 102, 'name': 'private-user-process', 'identity_confirmed': True}],
            'listeners': [{'port': 7786, 'address': '127.0.0.1', 'socket_inode': '1234',
                           'owner_pids': [101], 'password': 'do-not-export'}]}

    def snapshot(self, download=None):
        download = download or {'available': False, 'status': 'auth_required',
            'reason': '需要原厂授权', 'source': 'fixture.download', 'data': None}
        return vendor_business.collect_business_diagnostics(
            self.root, downloads=lambda **kw: download, runtime=lambda **kw: self.observation)

    def test_cached_apps_are_explicitly_partial_and_not_live(self):
        result = self.snapshot()
        apps = result['categories']['applications']
        self.assertTrue(apps['available'])
        self.assertEqual(apps['status'], 'cached')
        self.assertEqual(apps['data']['cached_entries'], 1)
        self.assertFalse(apps['data']['live'])
        self.assertFalse(apps['data']['complete'])
        self.assertIsNone(apps['data']['apps'][0]['running'])
        self.assertNotIn('do-not-export', json.dumps(result))
        self.assertEqual(result['readable_sources'], 1)

    def test_media_runtime_is_observed_but_task_progress_remains_unknown(self):
        with mock.patch('socket.create_connection', side_effect=AssertionError('no RPC')), \
                mock.patch('subprocess.run', side_effect=AssertionError('no command')):
            media = self.snapshot()['categories']['media']
        self.assertFalse(media['available'])
        self.assertEqual(media['status'], 'unverified_read_only')
        self.assertIsNone(media['data'])
        self.assertEqual(media['runtime']['processes'][0]['name'], 'intelligent')
        self.assertEqual(media['runtime']['listeners'][0]['port'], 7786)
        self.assertNotIn('private-user-process', str(media))
        self.assertNotIn('do-not-export', str(media))

    def test_no_auth_or_no_cache_is_not_an_empty_success(self):
        self.cache.unlink()
        result = self.snapshot()['categories']
        self.assertEqual(result['applications']['status'], 'cache_missing')
        self.assertIsNone(result['applications']['data'])
        self.assertEqual(result['downloads']['status'], 'auth_required')
        self.assertIsNone(result['downloads']['data'])

    def test_download_projection_excludes_identity_urls_files_and_raw_data(self):
        downloads = {'available': True, 'status': 'ok', 'source': 'fixture', 'reason': None,
            'raw': 'do-not-export', 'data': {'engine': 'aria2', 'version': '1.36.0', 'complete': False,
                'counts': {'active': 1, 'waiting': 0, 'stopped': 0},
                'tasks': [{'status': 'active', 'gid': 'private-gid', 'name': 'private-filename',
                           'total_bytes': 200, 'completed_bytes': 100, 'progress_percent': 50,
                           'url': 'do-not-export', 'files': ['do-not-export']}],
                'token': 'do-not-export'}}
        result = self.snapshot(downloads)
        data = result['categories']['downloads']['data']
        self.assertEqual(data['tasks'][0]['progress_percent'], 50)
        for forbidden in ('gid', 'name', 'url', 'files'):
            self.assertNotIn(forbidden, data['tasks'][0])
        self.assertNotIn('do-not-export', str(result))
        self.assertEqual(result['readable_sources'], 2)

    def test_invalid_provider_data_does_not_leak_errors_or_invent_data(self):
        for value in ({}, {'available': True, 'status': 'ok', 'data': []},
                      {'available': True, 'status': 'ok', 'data': {'total_bytes': float('nan')}},
                      {'available': True, 'status': 'ok', 'data': {'tasks': [{}] * 385}}):
            with self.subTest(value=type(value)):
                result = self.snapshot(value or {'available': 'bad'})['categories']['downloads']
                self.assertFalse(result['available'])
                self.assertIsNone(result['data'])
                self.assertEqual(result['status'], 'diagnostic_error')
        with mock.patch.object(vendor_business, 'collect_downloads', side_effect=OSError('/private/key')):
            result = vendor_business.collect_business_diagnostics(self.root, runtime=lambda **kw: self.observation)
        self.assertNotIn('/private/key', str(result))


class BusinessEndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = web_config.WebConfigServer.__new__(web_config.WebConfigServer)
        self.server.dev_commands = True
        self.server._dev_authorized = lambda ip: ip == '192.0.2.5'
        self.server.auth = types.SimpleNamespace(required=lambda: False)
        self.server.password_session = lambda headers: None
        self.server.business_diagnostic_lock = threading.Lock()

    def request(self, body=None, ip='192.0.2.5'):
        return self.server.panel_post('/api/dev/business',
            {'action': 'snapshot'} if body is None else body, {}, ip)

    def test_requires_developer_ip_command_flag_and_enabled_password_session(self):
        with mock.patch.object(web_config, 'collect_business_diagnostics') as run:
            self.assertEqual(self.request(ip='192.0.2.6')[0], 403)
            self.server.dev_commands = False
            self.assertEqual(self.request()[0], 403)
            self.server.dev_commands = True
            self.server.auth.required = lambda: True
            self.assertEqual(self.request()[0], 401)
            run.assert_not_called()
            self.server.password_session = lambda headers: {'id': 'fixture'}
            self.assertEqual(self.request()[0], 200)

    def test_only_fixed_snapshot_can_execute_without_rpc_url_or_parameters(self):
        with mock.patch.object(web_config, 'collect_business_diagnostics', return_value={'categories': {}}) as run:
            for body in ({}, {'action': 'refresh'}, {'action': 'snapshot', 'url': 'http://remote'},
                         {'action': 'snapshot', 'token': 'override'}, {'action': 'snapshot', 'method': 'addUri'},
                         {'action': 'snapshot', 'path': '/tmp/private'}, [], 'snapshot'):
                self.assertEqual(self.request(body)[0], 400)
            run.assert_not_called()
            status, value, cookie = self.request()
            self.assertEqual(status, 200)
            self.assertTrue(value['ok'])
            self.assertIsNone(cookie)
            run.assert_called_once_with()

    def test_one_capture_at_a_time_and_failures_release_the_lock(self):
        with self.server.business_diagnostic_lock:
            self.assertEqual(self.request()[0], 409)
        with mock.patch.object(web_config, 'collect_business_diagnostics', side_effect=OSError('/private')):
            status, value, _ = self.request()
            self.assertEqual(status, 503)
            self.assertNotIn('/private', str(value))
        self.assertFalse(self.server.business_diagnostic_lock.locked())

    def test_diagnostics_are_not_public_or_get_routes(self):
        self.assertIn('/api/dev/business', web_config.PANEL_POST_ROUTES)
        self.assertNotIn('/api/dev/business', web_config.PUBLIC_ROUTES)
        self.assertNotIn('/api/dev/business', web_config.PANEL_GET_ROUTES)


class BusinessHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = Path(tempfile.mkdtemp(prefix='r1-business-http-test-'))
        controller = mock.Mock(state_dir=directory)
        with mock.patch.dict(os.environ, {'R1_DEV_NO_PAIRING': '1', 'R1_DEV_ALLOW_IPS': '127.0.0.1',
                                         'R1_DEV_ALLOW_COMMANDS': '1', 'R1_DEV_MINUTES': '60'}):
            self.server = web_config.WebConfigServer(controller, host='127.0.0.1', port=0,
                                                       state_dir=directory / 'web').start()
        self.addCleanup(self.server.stop)
        self.host = '127.0.0.1:' + str(self.server.port)
        self.collector = mock.patch.object(web_config, 'collect_business_diagnostics',
            return_value={'categories': {'media': {'available': False, 'data': None}}})
        self.run = self.collector.start()
        self.addCleanup(self.collector.stop)

    def request(self, *, headers=None, remove=(), body=b'{"action":"snapshot"}', method='POST'):
        values = {'Host': self.host, 'Origin': 'http://' + self.host,
                  'Content-Type': 'application/json', 'X-FNM-Request': '1'}
        values.update(headers or {})
        for key in remove:
            values.pop(key, None)
        client = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            client.request(method, '/api/dev/business', body, values)
            response = client.getresponse()
            payload = response.read()
            self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, payload
        finally:
            client.close()

    def test_http_same_origin_json_and_csrf_precede_the_collector(self):
        for kwargs, expected in (({'headers': {'Origin': 'http://foreign.example'}}, 403),
                                 ({'remove': ('Origin',)}, 403),
                                 ({'remove': ('X-FNM-Request',)}, 403),
                                 ({'headers': {'Content-Type': 'text/plain'}}, 400),
                                 ({'body': b'{'}, 400),
                                 ({'body': b'{"action":"snapshot","action":"snapshot"}'}, 400),
                                 ({'body': b'{"action":"snapshot","method":"addUri"}'}, 400)):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(**kwargs)[0], expected)
        self.run.assert_not_called()

    def test_http_developer_controls_and_normal_password_both_apply(self):
        self.server.dev_commands = False
        self.assertEqual(self.request()[0], 403)
        self.server.dev_commands = True
        self.server.dev_allow_ips = {ipaddress.ip_address('192.0.2.1')}
        self.assertEqual(self.request()[0], 403)
        self.server.dev_allow_ips = {ipaddress.ip_address('127.0.0.1')}
        until = self.server.dev_until
        self.server.dev_until = time.time() - 1
        self.assertEqual(self.request()[0], 403)
        self.server.dev_until = until
        auth = self.server.auth.setup('fixture-business-password')
        self.assertTrue(auth['ok'])
        self.assertEqual(self.request()[0], 401)
        self.run.assert_not_called()
        cookie = web_config.AUTH_COOKIE + '=' + auth['sid']
        self.assertEqual(self.request(headers={'Cookie': cookie})[0], 200)
        self.run.assert_called_once_with()

    def test_http_snapshot_is_private_no_store_and_post_only(self):
        for method, expected in (('GET', 401), ('OPTIONS', 405), ('PUT', 405)):
            self.assertEqual(self.request(method=method, body=None)[0], expected)
        self.run.assert_not_called()
        status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)['ok'])
        self.run.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
