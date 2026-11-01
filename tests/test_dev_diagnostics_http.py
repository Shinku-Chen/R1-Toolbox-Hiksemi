"""The passive developer grant is confined to a fixed, private HTTP route."""
import copy
import http.client
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import web_config


_DEFAULT = object()
CHECKS = {'device', 'storage', 'business', 'fan'}
ROUTE = '/api/dev/diagnostics'


class DeveloperDiagnosticsHTTPTests(unittest.TestCase):
    """Real HTTP requests; every device/provider read is an isolated mock."""

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='r1-dev-diagnostics-http-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.controller = mock.Mock(state_dir=directory)
        self.policy = mock.Mock()
        self.insights = mock.Mock()
        self.cached = {'device': {'identity': {
            'name': '测试设备昵称', 'name_source': 'histor.dev_info.dev_name',
            'name_reason': None, 'hostname': 'R1-fixture', 'kernel': 'fixture-kernel',
            'serial': 'private-device-serial', 'credentials': 'private-credentials',
            'firmware': {'version': 'fixture-version', 'build': 'fixture-build',
                         'model': 'HikSemi R1', 'token': 'private-firmware-token'},
        }}, 'containers': {'secret': 'private-unrelated-data'}}
        self.insights.snapshot.side_effect = lambda: copy.deepcopy(self.cached)
        self.insights.storage_name_diagnostics.return_value = {
            'available': True, 'source': 'fixture-storage', 'items': [], 'vendor': {}}
        self.insights.disk_activity_diagnostics.return_value = {
            'available': True, 'status': 'baseline', 'source': 'procfs_sysfs_counters',
            'disks': [], 'processes': [], 'power_state': None}
        self.insights.storage_identity_diagnostics.return_value = {
            'available': True, 'source': 'insights_cache+hiksemi_storage_socket', 'items': [
                {'id': 'sda', 'device': '/dev/sda', 'user_name': '测试盘名', 'space_names': [],
                 'name_source': 'hiksemi_storage_socket', 'serial': 'fixture-full-disk-serial',
                 'serial_status': 'ok', 'serial_sources': ['smart_cache', 'hiksemi_storage_socket'],
                 'matched': True}]}
        environment = {'R1_WEB_AUTH_MODE': 'password', 'R1_DEV_NO_PAIRING': '1',
                       'R1_DEV_ALLOW_IPS': '127.0.0.1', 'R1_DEV_MINUTES': '60',
                       'R1_DEV_ALLOW_COMMANDS': '0'}
        with mock.patch.dict(os.environ, environment):
            self.server = web_config.WebConfigServer(
                self.controller, self.insights, fan_policy=self.policy,
                host='127.0.0.1', port=0, state_dir=directory / 'web',
                version='fixture-diagnostics').start()
        self.addCleanup(self.server.stop)
        self.fan = mock.Mock()
        self.fan.snapshot.return_value = {'available': True, 'source': 'fixture-fan'}
        self.server.fan_diagnostics = self.fan
        business_patch = mock.patch.object(web_config, 'collect_business_diagnostics',
            return_value={'schema_version': 1, 'categories': {}})
        self.business = business_patch.start()
        self.addCleanup(business_patch.stop)
        self.login = self.server.auth.setup('fixture-diagnostics-password')
        self.assertTrue(self.login['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + self.login['sid']
        self.host = '127.0.0.1:' + str(self.server.port)

    def request(self, body=_DEFAULT, *, path=ROUTE, method='POST', headers=None,
                remove=(), duplicates=()):
        """Build exact headers, including duplicates browsers normally suppress."""
        if body is _DEFAULT:
            body = {'action': 'snapshot'} if method == 'POST' else None
        payload = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None)
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
            connection.endheaders(payload)
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            response_body = json.loads(response.read().decode('utf-8'))
            self.assertEqual(response_headers.get('cache-control'), 'no-store')
            self.assertNotIn('access-control-allow-origin', response_headers)
            return response.status, response_headers, response_body
        finally:
            connection.close()

    def assert_no_sources(self):
        self.insights.snapshot.assert_not_called()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.insights.storage_identity_diagnostics.assert_not_called()
        self.insights.disk_activity_diagnostics.assert_not_called()
        self.business.assert_not_called()
        self.fan.snapshot.assert_not_called()
        self.assert_no_controls()

    def assert_no_controls(self):
        self.insights._smart.assert_not_called()
        self.insights._disks.assert_not_called()
        self.controller.action.assert_not_called()
        self.policy.action.assert_not_called()
        self.fan.start.assert_not_called()

    def test_catalog_needs_no_password_cookie_or_command_flag_and_does_not_collect(self):
        self.assertTrue(self.server.auth.required())
        self.assertFalse(self.server.dev_commands)
        status, headers, value = self.request({'action': 'catalog'})
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertEqual(value['version'], 'fixture-diagnostics')
        self.assertEqual(value['access']['client_ip'], '127.0.0.1')
        self.assertGreater(value['access']['expires_in'], 0)
        self.assertLessEqual(value['access']['expires_in'], 3600)
        self.assertEqual(value['catalog']['schema_version'], 1)
        self.assertIs(value['catalog']['read_only'], True)
        self.assertEqual({row['id'] for row in value['catalog']['checks']},
                         CHECKS | {'storage_identity', 'file_access_readiness', 'docker_project_readiness', 'disk_activity', 'file_backend_context', 'file_protocol', 'capacity_cache'})
        self.assertNotIn('set-cookie', headers)
        self.assert_no_sources()

    def test_default_snapshot_runs_all_fixed_sources_without_enabling_controls(self):
        status, headers, value = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        snapshot = value['snapshot']
        self.assertEqual(snapshot['schema_version'], 1)
        self.assertIs(snapshot['read_only'], True)
        self.assertEqual(set(snapshot['checks']), CHECKS)
        self.assertIs(snapshot['all_checks_completed'], True)
        for result in snapshot['checks'].values():
            self.assertEqual(result['http_status'], 200)
            self.assertIs(result['ok'], True)
            self.assertIsInstance(result['data'], dict)
            self.assertIsNone(result['error'])
        self.insights.storage_name_diagnostics.assert_called_once_with()
        self.insights.storage_identity_diagnostics.assert_not_called()
        self.assertNotIn('fixture-full-disk-serial', json.dumps(value))
        self.insights.disk_activity_diagnostics.assert_not_called()
        self.business.assert_called_once_with(state_dir=self.server.state_dir)
        self.fan.snapshot.assert_called_once_with()
        self.assertNotIn('set-cookie', headers)
        self.assert_no_controls()

    def test_disk_activity_is_explicit_and_does_not_query_storage_or_business(self):
        self.server.monitoring = mock.Mock(state_dir='/drives/fixture/Users/admin/private-volume')
        self.server.monitoring.snapshot.return_value = {
            'settings': {'history_enabled': True},
            'history': {'recording': True, 'last_write_at': 1234567890, 'private': 'not-exported'}}
        status, _, value = self.request({'action': 'snapshot', 'checks': ['disk_activity']})
        self.assertEqual(status, 200)
        activity = value['snapshot']['checks']['disk_activity']['data']['activity']
        self.assertEqual(activity['toolbox_history']['enabled'], True)
        self.assertEqual(activity['toolbox_history']['write_interval_seconds'], 30)
        self.assertIsNone(activity['power_state'])
        self.insights.disk_activity_diagnostics.assert_called_once_with(
            history_directory='/drives/fixture/Users/admin/private-volume')
        self.insights.storage_name_diagnostics.assert_not_called()
        self.insights.storage_identity_diagnostics.assert_not_called()
        self.business.assert_not_called()
        self.assertNotIn('not-exported', json.dumps(value))
        self.assertNotIn('private-volume', json.dumps(value))
        self.assert_no_controls()

    def test_disk_activity_result_is_discarded_after_window_expires(self):
        def expire(**kwargs):
            self.server.dev_until = time.time() - 1
            return {'disks': [{'private-marker': 'should-not-leave'}]}
        self.insights.disk_activity_diagnostics.side_effect = expire
        status, _, value = self.request({'action': 'snapshot', 'checks': ['disk_activity']})
        self.assertEqual(status, 403)
        self.assertNotIn('should-not-leave', json.dumps(value))

    def test_file_readiness_is_explicit_metadata_and_shares_the_storage_lock(self):
        provider = self._patch(self.server.integration, 'file_readiness', return_value={
            'ok': True, 'diagnostic_available': True, 'file_access_available': False,
            'status': 'metadata_only', 'blockers': ['acl_unverified', 'secure_open_unavailable']})
        body = {'action': 'snapshot', 'checks': ['file_access_readiness']}
        status, _, value = self.request(body)
        self.assertEqual(200, status)
        data = value['snapshot']['checks']['file_access_readiness']['data']
        self.assertIs(data['file_access_available'], False)
        self.assertEqual(data['status'], 'metadata_only')
        provider.assert_called_once_with()
        provider.reset_mock()
        with self.server.storage_diagnostic_lock:
            _, _, value = self.request(body)
            self.assertEqual(409, value['snapshot']['checks']['file_access_readiness']['http_status'])
        provider.assert_not_called()
        self.assertEqual(400, self.request({**body, 'path': '/private'})[0])
        self.assert_no_controls()

    def test_docker_readiness_only_discovers_and_reads_registered_metadata(self):
        registry = self.server.integration.docker_projects
        discover = self._patch(registry, 'catalog', return_value={
            'ok': True, 'available': False, 'reason': 'fixture unavailable', 'candidates': []})
        metadata = self._patch(registry, 'metadata', return_value={'ok': True, 'projects': []})
        live = self._patch(registry, 'snapshot', side_effect=AssertionError('duplicate engine read'))
        status, _, value = self.request({'action': 'snapshot', 'checks': ['docker_project_readiness']})
        self.assertEqual(200, status)
        data = value['snapshot']['checks']['docker_project_readiness']['data']
        self.assertIs(data['discovery']['available'], False)
        self.assertEqual([], data['registry']['projects'])
        discover.assert_called_once_with()
        metadata.assert_called_once_with()
        live.assert_not_called()
        self.assert_no_controls()

    def test_expired_window_during_collection_discards_the_diagnostic_result(self):
        self._patch(self.server, '_dev_authorized', side_effect=[True, False])
        self._patch(self.server.integration, 'file_readiness', return_value={
            'ok': True, 'status': 'private-fixture-proof'})
        status, _, value = self.request({'action': 'snapshot', 'checks': ['file_access_readiness']})
        self.assertEqual(403, status)
        self.assertNotIn('snapshot', value)
        self.assertNotIn('private-fixture-proof', json.dumps(value))
        self.assert_no_controls()

    def test_selected_check_executes_only_its_fixed_source(self):
        for selected in sorted(CHECKS):
            with self.subTest(check=selected):
                self.insights.reset_mock()
                self.business.reset_mock()
                self.fan.reset_mock()
                status, _, value = self.request({'action': 'snapshot', 'checks': [selected]})
                self.assertEqual(status, 200)
                self.assertEqual(set(value['snapshot']['checks']), {selected})
                self.assertTrue(value['snapshot']['checks'][selected]['ok'])
                self.assertEqual(self.insights.snapshot.call_count,
                                 1 if selected in ('device', 'storage') else 0)
                self.assertEqual(self.insights.storage_name_diagnostics.call_count,
                                 int(selected == 'storage'))
                self.assertEqual(self.business.call_count, int(selected == 'business'))
                self.assertEqual(self.fan.snapshot.call_count, int(selected == 'fan'))
                self.assert_no_controls()

    def test_explicit_storage_identity_uses_its_fixed_cache_projection_only(self):
        body = {'action': 'snapshot', 'checks': ['storage_identity']}
        status, _, value = self.request(body)
        self.assertEqual(status, 200)
        self.assertEqual(set(value['snapshot']['checks']), {'storage_identity'})
        result = value['snapshot']['checks']['storage_identity']
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['storage']['items'][0]['serial'], 'fixture-full-disk-serial')
        self.assertEqual(result['data']['device']['name'], '测试设备昵称')
        self.assertNotIn('private-', json.dumps(value))
        self.insights.storage_identity_diagnostics.assert_called_once_with()
        self.insights.storage_name_diagnostics.assert_not_called()
        self.business.assert_not_called()
        self.fan.snapshot.assert_not_called()
        self.assert_no_controls()
        self.insights.reset_mock()
        default = self.request()[2]
        self.assertNotIn('fixture-full-disk-serial', json.dumps(default))
        self.assertNotIn('storage_identity', default['snapshot']['checks'])
        self.insights.storage_identity_diagnostics.assert_not_called()

    def test_storage_identity_retains_actual_peer_window_and_fixed_request_security(self):
        body = {'action': 'snapshot', 'checks': ['storage_identity']}
        with mock.patch.object(self.server, 'dev_allow_ips', {ipaddress.ip_address('192.168.31.9')}):
            self.assertEqual(self.request(body, headers={
                'Cookie': self.cookie, 'X-Forwarded-For': '192.168.31.9',
                'Authorization': 'Bearer fixture-token'})[0], 403)
        with mock.patch.object(self.server, 'dev_until', time.time() - 1):
            self.assertEqual(self.request(body)[0], 403)
        with mock.patch.object(self.server, 'dev_no_pairing', False):
            self.assertEqual(self.request(body)[0], 403)
        for remove in (('Origin',), ('X-FNM-Request',)):
            self.assertEqual(self.request(body, remove=remove)[0], 403)
        for field, argument in (('serial', 'override'), ('path', '/dev/sda'),
                                ('cmd', 'smartctl'), ('refresh', True)):
            self.assertEqual(self.request({**body, field: argument})[0], 400)
        self.assertEqual(self.request(body, path=ROUTE + '?checks=storage_identity')[0], 400)
        self.assert_no_sources()

    def test_storage_identity_http_reuses_real_collector_cache_and_never_runs_another_probe(self):
        from insights import InsightsCollector

        temporary = tempfile.TemporaryDirectory(prefix='r1-diagnostic-identity-cache-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        folder = root / 'sys/block/sda'
        (folder / 'device').mkdir(parents=True)
        (folder / 'queue').mkdir()
        (folder / 'dev').write_text('8:0\n', encoding='utf8')
        (folder / 'wwid').write_text('fixture-physical-lifetime-a\n', encoding='utf8')
        (folder / 'size').write_text('1000000', encoding='utf8')
        (folder / 'device/model').write_text('Fixture drive', encoding='utf8')
        (folder / 'queue/rotational').write_text('1', encoding='utf8')
        serial = 'fixture-full-cached-identity'
        vendor = mock.Mock()
        vendor.snapshot.return_value = {
            'available': True, 'source': 'hiksemi_storage_socket',
            'disks': [{'disk_name': 'sda', 'disk_port': 1, 'disk_serial': serial}],
            'spaces': [{'space_name': '测试盘名', 'is_raid': 0, 'port': 1,
                        'member': [{'root_name': 'sda', 'disk_name': 'sda2', 'port': 1, 'disk_id': serial}]}]}
        runner = mock.Mock(return_value=mock.Mock(returncode=0, stderr='', stdout=json.dumps({
            'serial_number': serial, 'smart_status': {'passed': True}})))
        collector = InsightsCollector(root / 'proc', root / 'sys', runner=runner,
            command_finder=lambda name: '/fixture/' + name, clock=lambda: 10,
            page_size=4096, clock_ticks=100, storage_client=vendor)
        self.addCleanup(collector.stop)
        collector._state['disks'] = collector._disks()
        runner.assert_not_called()
        receipt = collector.refresh_smart(force=False, authorizer=lambda: True)
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(receipt['results'][0]['device'], 'sda')
        self.assertEqual(receipt['results'][0]['status'], 'refreshed')
        runner.assert_called_once()
        collector._state['disks'] = collector._disks()
        collector._state['device'] = copy.deepcopy(self.cached['device'])
        runner.assert_called_once()
        self.server.insights = collector
        with mock.patch.object(collector, '_run', side_effect=AssertionError('no extra command')), \
                mock.patch.object(collector, '_disks', side_effect=AssertionError('no disk refresh')):
            explicit = self.request({'action': 'snapshot', 'checks': ['storage_identity']})
            self.assertEqual(explicit[0], 200)
            row = explicit[2]['snapshot']['checks']['storage_identity']['data']['storage']['items'][0]
            self.assertEqual(row['serial'], serial)
            self.assertEqual(row['user_name'], '测试盘名')
            self.assertTrue(row['matched'])
            default = self.request()[2]
            self.assertNotIn(serial, json.dumps(default))
            self.assertNotIn('storage_identity', default['snapshot']['checks'])
        runner.assert_called_once()

    def test_storage_identity_shares_storage_lock_and_sanitizes_failures(self):
        body = {'action': 'snapshot', 'checks': ['storage_identity']}
        with self.server.storage_diagnostic_lock:
            result = self.request(body)[2]['snapshot']['checks']['storage_identity']
        self.assertEqual(result['http_status'], 409)
        self.assertIsNone(result['data'])
        self.insights.storage_identity_diagnostics.assert_not_called()
        self.insights.storage_identity_diagnostics.side_effect = OSError('fixture-full-disk-serial /private/path')
        value = self.request(body)[2]
        result = value['snapshot']['checks']['storage_identity']
        self.assertEqual(result['http_status'], 503)
        self.assertIsNone(result['data'])
        self.assertNotIn('fixture-full-disk-serial', json.dumps(value))
        self.assertNotIn('/private', json.dumps(value))
        self.assertFalse(self.server.storage_diagnostic_lock.locked())
        self.assertFalse(self.server.diagnostic_lock.locked())

    def test_flag_ip_and_expiry_are_checked_before_catalog_or_snapshot(self):
        for attribute, denied in (('dev_no_pairing', False), ('dev_allow_ips', set()),
                                 ('dev_allow_ips', {ipaddress.ip_address('192.0.2.5')}),
                                 ('dev_until', time.time() - 1)):
            for action in ('catalog', 'snapshot'):
                with self.subTest(attribute=attribute, action=action), \
                        mock.patch.object(self.server, attribute, denied):
                    self.assertEqual(self.request({'action': action})[0], 403)
        self.assert_no_sources()

    def test_nonallowlisted_peer_cannot_use_password_or_spoof_forwarded_headers(self):
        self.server.dev_allow_ips = {ipaddress.ip_address('192.0.2.5')}
        headers = {'Cookie': self.cookie, 'Forwarded': 'for=192.0.2.5',
                   'X-Forwarded-For': '192.0.2.5', 'X-Real-IP': '192.0.2.5'}
        for action in ('catalog', 'snapshot'):
            with self.subTest(action=action):
                self.assertEqual(self.request({'action': action}, headers=headers)[0], 403)
        self.assert_no_sources()

    def test_same_origin_json_marker_and_unique_fields_precede_sources(self):
        cases = (({'headers': {'Host': 'foreign.invalid'}}, 403),
                 ({'headers': {'Origin': 'http://foreign.invalid'}}, 403),
                 ({'remove': ('Origin',)}, 403),
                 ({'remove': ('X-FNM-Request',)}, 403),
                 ({'headers': {'X-FNM-Request': '0'}}, 403),
                 ({'headers': {'Content-Type': 'text/plain'}}, 400),
                 ({'body': b'{'}, 400),
                 ({'body': b'{"action":"snapshot","action":"snapshot"}'}, 400),
                 ({'body': b'{"action":"snapshot","checks":[NaN]}'}, 400))
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(**kwargs)[0], expected)
        self.assert_no_sources()

    def test_duplicate_security_headers_are_rejected_before_sources(self):
        headers = (('Host', self.host), ('Origin', 'http://' + self.host),
                   ('Cookie', self.cookie), ('Content-Length', '21'),
                   ('Connection', 'close'), ('X-R1-CSRF', 'x' * 43),
                   ('X-FNM-Request', '1'))
        for key, value in headers:
            with self.subTest(header=key):
                extra = {key: value} if key in ('Cookie', 'X-R1-CSRF') else {}
                self.assertEqual(self.request(headers=extra, duplicates=((key, value),))[0], 400)
        self.assert_no_sources()

    def test_fixed_schema_rejects_empty_unknown_duplicate_checks_and_extra_fields(self):
        bodies = ({}, [], 'snapshot', {'action': []}, {'action': 'retry'},
                  {'action': 'snapshot', 'checks': []},
                  {'action': 'snapshot', 'checks': 'device'},
                  {'action': 'snapshot', 'checks': ['device', 'device']},
                  {'action': 'snapshot', 'checks': ['unknown']},
                  {'action': 'snapshot', 'checks': [1]},
                  {'action': 'snapshot', 'checks': [None]},
                  {'action': 'snapshot', 'checks': [['device']]},
                  {'action': 'catalog', 'checks': ['device']})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        for field, value in (('path', '/etc/shadow'), ('cmd', 'id'), ('command', 'id'),
                             ('url', 'http://127.0.0.1'), ('port', 7788),
                             ('confirmed', True), ('duration_seconds', 60)):
            with self.subTest(field=field):
                self.assertEqual(self.request({'action': 'snapshot', field: value})[0], 400)
        self.assert_no_sources()

    def test_route_has_no_get_cors_or_write_methods_and_rejects_url_arguments(self):
        for method in ('GET', 'OPTIONS', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
            with self.subTest(method=method):
                if method == 'HEAD':
                    # HEAD has no response body by HTTP definition.
                    connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
                    try:
                        connection.request('HEAD', ROUTE, headers={'Host': self.host})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 405)
                        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
                        response.read()
                    finally:
                        connection.close()
                else:
                    self.assertEqual(self.request(method=method)[0], 401 if method == 'GET' else 405)
        for suffix in ('?checks=device', '?cmd=id', '#device'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(path=ROUTE + suffix)[0], 400)
        self.assertIn(ROUTE, web_config.PANEL_POST_ROUTES)
        self.assertNotIn(ROUTE, web_config.PANEL_GET_ROUTES)
        self.assertNotIn(ROUTE, web_config.PUBLIC_ROUTES)
        self.assert_no_sources()

    def test_new_grant_does_not_unlock_old_debug_routes_or_normal_writes(self):
        self.server.dev_commands = True
        for path, body, expected in (
                ('/api/dev/storage', {'action': 'snapshot'}, 401),
                ('/api/dev/business', {'action': 'snapshot'}, 401),
                ('/api/dev/fan', {'action': 'snapshot'}, 401),
                ('/api/dev/fan', {'action': 'retry', 'confirmed': True}, 401),
                ('/api/dev/exec', {'name': 'identity'}, 403),
                ('/api/r1/settings', {'brightness': 50}, 401),
                ('/api/r1/power', {'action': 'reboot', 'confirmed': True}, 401)):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.request(body, path=path)[0], expected)
        status, _, session = self.request(path='/api/auth/session', method='GET')
        self.assertEqual(status, 200)
        self.assertFalse(session['authorized'])
        self.assertFalse(session['dev_mode'])
        self.assert_no_sources()

    def test_old_debug_routes_still_need_command_flag_with_valid_password_cookie(self):
        for path in ('/api/dev/storage', '/api/dev/business', '/api/dev/fan'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request({'name': 'identity'}, path='/api/dev/exec',
                                     headers={'Cookie': self.cookie})[0], 403)
        self.assert_no_sources()

    def test_device_check_projects_only_fixed_cached_identity_fields(self):
        status, _, value = self.request({'action': 'snapshot', 'checks': ['device']})
        self.assertEqual(status, 200)
        data = value['snapshot']['checks']['device']['data']
        self.assertEqual(data['source'], 'insights_cache')
        device = data['device']
        self.assertEqual(set(device), {'name', 'name_source', 'name_reason', 'hostname', 'kernel', 'firmware'})
        self.assertEqual(device['name'], '测试设备昵称')
        self.assertIsNone(device['name_reason'])
        self.assertEqual(set(device['firmware']), {
            'version', 'firmware_version', 'build', 'build_date', 'build_time', 'model'})
        self.assertNotIn('private-', json.dumps(value))
        self.insights.snapshot.assert_called_once_with()
        self.assert_no_controls()

    def test_shared_source_locks_report_busy_without_calling_either_provider(self):
        with self.server.storage_diagnostic_lock, self.server.business_diagnostic_lock:
            status, _, value = self.request({'action': 'snapshot', 'checks': ['storage', 'business']})
        self.assertEqual(status, 200)
        self.assertFalse(value['snapshot']['all_checks_completed'])
        for result in value['snapshot']['checks'].values():
            self.assertEqual(result['http_status'], 409)
            self.assertFalse(result['ok'])
            self.assertIsNone(result['data'])
        self.assert_no_sources()
        self.assertEqual(self.request({'action': 'snapshot', 'checks': ['storage', 'business']})[0], 200)
        self.insights.storage_name_diagnostics.assert_called_once_with()
        self.business.assert_called_once_with(state_dir=self.server.state_dir)

    def test_global_busy_does_not_collect_and_catalog_stays_available(self):
        with self.server.diagnostic_lock:
            self.assertEqual(self.request()[0], 409)
            self.assertEqual(self.request({'action': 'catalog'})[0], 200)
        self.assert_no_sources()

    def test_source_errors_stay_private_release_locks_and_allow_other_checks(self):
        private = '/private/credentials-token'
        self.insights.storage_name_diagnostics.side_effect = OSError(private)
        self.business.side_effect = OSError(private)
        self.fan.snapshot.side_effect = OSError(private)
        status, _, value = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(value['snapshot']['all_checks_completed'])
        self.assertTrue(value['snapshot']['checks']['device']['ok'])
        for name in ('storage', 'business', 'fan'):
            result = value['snapshot']['checks'][name]
            self.assertEqual(result['http_status'], 503)
            self.assertFalse(result['ok'])
            self.assertIsNone(result['data'])
        self.assertNotIn(private, json.dumps(value))
        self.assertFalse(self.server.diagnostic_lock.locked())
        self.assertFalse(self.server.storage_diagnostic_lock.locked())
        self.assertFalse(self.server.business_diagnostic_lock.locked())
        self.insights.storage_name_diagnostics.side_effect = None
        self.business.side_effect = None
        self.fan.snapshot.side_effect = None
        self.assertTrue(self.request()[2]['snapshot']['all_checks_completed'])
        self.assert_no_controls()


    def test_file_backend_context_is_explicit_locked_and_expiry_fenced(self):
        import api_file_operations
        provider = self._patch(api_file_operations, 'collect_file_backend_context', return_value={
            'available': False, 'may_wake_disks': True, 'file_access_available': False})
        self.request()
        provider.assert_not_called()
        body = {'action': 'snapshot', 'checks': ['file_backend_context']}
        status, _, value = self.request(body)
        self.assertEqual(200, status)
        self.assertTrue(value['snapshot']['checks']['file_backend_context']['data']['context']['may_wake_disks'])
        provider.assert_called_once_with(self.server.panel)
        provider.reset_mock()
        self.server.storage_diagnostic_lock.acquire()
        try:
            result = self.request(body)[2]['snapshot']['checks']['file_backend_context']
            self.assertEqual(result['http_status'], 409)
            provider.assert_not_called()
        finally:
            self.server.storage_diagnostic_lock.release()
        def expire(*args, **kwargs):
            self.server.dev_until = time.time() - 1
            return {'private-marker': 'never-disclose'}
        provider.side_effect = expire
        status, _, value = self.request(body)
        self.assertEqual(403, status)
        self.assertNotIn('never-disclose', json.dumps(value))
        self.assert_no_controls()

    def test_root_topology_is_only_collected_for_explicit_authorized_context(self):
        import hashlib
        import api_file_operations
        import api_file_roots
        fixture = self.server.state_dir / 'context-proc-fixture'
        (fixture / 'proc/self').mkdir(parents=True)
        (fixture / 'proc/sys/kernel/random').mkdir(parents=True)
        (fixture / 'proc/self/mountinfo').write_text('', encoding='utf-8')
        (fixture / 'proc/sys/kernel/random/boot_id').write_text('fixture-boot', encoding='utf-8')
        catalog = {'available': False, 'status': 'unavailable', 'boundary_version': 3, 'roots': []}
        topology = {'schema_version': 1, 'scope': 'file_root_topology', 'boundary_version': 3,
            'available': True, 'complete': True, 'reason_codes': [], 'catalog_root_ids': [],
            'catalog_fingerprint': hashlib.sha256(json.dumps(catalog, sort_keys=True,
                ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest(),
            'inputs': {'mountinfo': {'status': 'ok', 'eof': True, 'bytes_read': 0,
                'parsed_records': 0, 'malformed_records': 0}, 'cached_disks': {'status': 'ok',
                'records': 1, 'valid_identity_records': 0, 'invalid_identity_records': 1}},
            'candidates': [{'candidate_id': 'candidate_fixture', 'reason_codes': ['source_unsupported']}],
            'effects': {'metadata_only': True, 'opens_data_paths': False, 'stats_data_paths': False,
                'grants_authorization': False}}
        self._patch(api_file_operations, 'collect_file_roots', return_value=catalog)
        producer = self._patch(api_file_roots, 'collect_file_root_topology', return_value=topology)
        actual_context = api_file_operations.collect_file_backend_context
        provider = self._patch(api_file_operations, 'collect_file_backend_context',
            side_effect=lambda panel: actual_context(panel, root=fixture,
                owner_reader=lambda **unused: False, runtime_reader=lambda **unused: {}))
        users = self._patch(api_file_operations, '_admin_context',
                            side_effect=AssertionError('no eligible admin path may open'))
        self.request()
        self.request({'action': 'catalog'})
        self.request(method='GET')
        provider.assert_not_called()
        producer.assert_not_called()
        body = {'action': 'snapshot', 'checks': ['file_backend_context']}
        allowed = self.server.dev_allow_ips
        self.server.dev_allow_ips = {'192.168.31.193'}
        status, _, value = self.request(body, headers={'Cookie': self.cookie,
            'X-Forwarded-For': '192.168.31.193', 'X-Real-IP': '192.168.31.193'})
        self.assertEqual(status, 403)
        self.assertNotIn('root_topology', json.dumps(value))
        provider.assert_not_called()
        producer.assert_not_called()
        self.server.dev_allow_ips = allowed
        status, _, value = self.request(body)
        self.assertEqual(status, 200)
        context = value['snapshot']['checks']['file_backend_context']['data']['context']
        self.assertEqual(context['root_topology'], topology)
        self.assertTrue(context['root_topology']['complete'])
        self.assertFalse(context['complete'])
        self.assertFalse(context['file_access_available'])
        provider.assert_called_once_with(self.server.panel)
        producer.assert_called_once_with(self.server.panel)
        users.assert_not_called()

        def expire(unused):
            self.server.dev_until = time.time() - 1
            return dict(topology, candidates=[{'private_marker': 'topology-must-not-leave'}])
        producer.side_effect = expire
        status, _, value = self.request(body)
        self.assertEqual(status, 403)
        self.assertNotIn('topology-must-not-leave', json.dumps(value))
        self.assertNotIn('root_topology', json.dumps(value))
        self.assertEqual(producer.call_count, 2)
        self.assert_no_controls()


if __name__ == '__main__':
    unittest.main()
