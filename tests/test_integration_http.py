"""Exercise LAN API keys, MCP and browser management through a real HTTP server.

All credentials live in a temporary fixture directory. Hardware and native media
collectors are mocks; these tests never contact a NAS or execute host commands.
"""

import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import web_config


_DEFAULT = object()
PROTOCOL = '2025-11-25'
SCOPES = ('read', 'fan', 'light', 'screen', 'power', 'media')
TOOL_NAMES = {
    'get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan',
    'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings',
    'request_power', 'cancel_power',
}


class IntegrationHTTPTests(unittest.TestCase):
    def setUp(self):
        # Panel discovery reloads web_config; patch the same current instance
        # that IntegrationApi imports when executing a media request.
        global web_config
        web_config = sys.modules['web_config']
        temporary = tempfile.TemporaryDirectory(prefix='r1-integration-http-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.controller = mock.Mock(state_dir=directory)
        self.controller.snapshot.return_value = {
            'settings': {'brightness': 80, 'font_scale': 100},
            'capabilities': {'screen': {'available': True},
                             'rgb': {'available': True}},
            'screen': {'max_brightness': 100, 'brightness': 80, 'asleep': False},
            'rgb': {'mode': 1, 'color': '#123456', 'brightness': 70},
        }
        self.controller.action.return_value = {'ok': True}
        self.insights = mock.Mock()
        self.insights.snapshot.return_value = {
            'device': {'identity': {'name': 'fixture-device',
                                    'hostname': 'fixture-host', 'firmware': {}}},
            'cpu': {'usage_percent': 7, 'temperature_c': None},
            'memory': {}, 'disks': [], 'interfaces': [], 'containers': [],
        }
        self.insights.storage_name_diagnostics.return_value = {
            'available': True, 'items': []}
        self.fan_policy = mock.Mock()
        self.fan_policy.snapshot.return_value = {'active': False, 'channels': {}}
        self.fan_policy.action.return_value = {'ok': True}
        self.light_policy = mock.Mock()
        self.light_policy.snapshot.return_value = {'active': False}
        self.light_policy.action.return_value = {'ok': True}
        self.face_result = {'schema_version': 1, 'available': True,
                            'data': {'processed': 3, 'total': 8}}
        self.batch_result = {'schema_version': 1, 'available': True,
                             'results': [{'id': 'face', 'available': True}]}
        self.face = self._patch(web_config, 'collect_media_probe',
                                return_value=self.face_result)
        self.batch = self._patch(web_config, 'collect_media_batch',
                                 return_value=self.batch_result)
        environment = {'R1_WEB_AUTH_MODE': 'password', 'R1_DEV_NO_PAIRING': '1',
                       'R1_DEV_ALLOW_IPS': '127.0.0.1', 'R1_DEV_MINUTES': '60',
                       'R1_DEV_ALLOW_COMMANDS': '0'}
        with mock.patch.dict(os.environ, environment):
            self.server = web_config.WebConfigServer(
                self.controller, self.insights, fan_policy=self.fan_policy,
                light_policy=self.light_policy, host='127.0.0.1', port=0,
                state_dir=directory / 'web', version='fixture-integration').start()
        self.addCleanup(self.server.stop)
        login = self.server.auth.setup('fixture-integration-password')
        self.assertTrue(login['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + login['sid']
        self.host = '127.0.0.1:' + str(self.server.port)
        self.now = 1800000000
        self.server.api_access.clock = lambda: self.now

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def key(self, scopes=('read',), *, enable=True, ips=(), days=90):
        if enable:
            self.server.api_access.configure({'enabled': True})
        return self.server.api_access.create_key({
            'name': 'fixture key', 'scopes': list(scopes),
            'allowed_ips': list(ips), 'expires_days': days})

    @staticmethod
    def message(method='tools/list', params=_DEFAULT, request_id=1):
        value = {'jsonrpc': '2.0', 'method': method}
        if request_id is not _DEFAULT:
            value['id'] = request_id
        if params is not _DEFAULT:
            value['params'] = params
        return value

    def request(self, method, path, body=None, *, headers=None, remove=(),
                duplicates=(), private=True):
        payload = body if isinstance(body, bytes) else (
            json.dumps(body).encode('utf-8') if body is not None else None)
        values = {'Host': self.host, 'Connection': 'close'}
        if payload is not None:
            values.update({'Content-Type': 'application/json',
                           'Content-Length': str(len(payload))})
        values.update(headers or {})
        for name in remove:
            values.pop(name, None)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in list(values.items()) + list(duplicates):
                connection.putheader(name, value)
            connection.endheaders(payload)
            response = connection.getresponse()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            raw = response.read()
            self.assertEqual(response_headers.get('cache-control'), 'no-store')
            if private:
                self.assertNotIn('access-control-allow-origin', response_headers)
                self.assertNotIn('access-control-allow-methods', response_headers)
                self.assertNotIn('set-cookie', response_headers)
            if raw and 'application/json' in response_headers.get('content-type', ''):
                value = json.loads(raw.decode('utf-8'))
            else:
                value = raw
            return response.status, response_headers, value
        finally:
            connection.close()

    def rpc(self, key=None, message=_DEFAULT, **kwargs):
        if message is _DEFAULT:
            message = self.message()
        headers = {'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': PROTOCOL}
        if key is not None:
            headers['Authorization'] = 'Bearer ' + key['token']
        headers.update(kwargs.pop('headers', {}))
        return self.request('POST', kwargs.pop('path', '/mcp'), message,
                            headers=headers, **kwargs)

    def rest_action(self, key, tool='set_screen', arguments=None, **kwargs):
        headers = {'Authorization': 'Bearer ' + key['token']}
        headers.update(kwargs.pop('headers', {}))
        body = {'tool': tool, 'arguments': arguments if arguments is not None
                else {'action': 'wake'}}
        return self.request('POST', '/api/integration/action', body,
                            headers=headers, **kwargs)

    def browser(self, method='GET', path='/api/access', body=None, *,
                cookie=True, headers=None, **kwargs):
        values = {'Origin': 'http://' + self.host, 'X-FNM-Request': '1'}
        if cookie:
            values['Cookie'] = self.cookie
        values.update(headers or {})
        return self.request(method, path, body, headers=values, **kwargs)

    def assert_no_actions(self):
        self.controller.action.assert_not_called()
        self.fan_policy.action.assert_not_called()
        self.light_policy.action.assert_not_called()
        self.insights.action.assert_not_called()
        self.face.assert_not_called()
        self.batch.assert_not_called()

    def assert_denied(self, response):
        self.assertIn(response[0], (401, 403))
        self.assert_no_actions()

    def assert_tool_error(self, value, code=None):
        self.assertIs(value['isError'], True)
        self.assertIs(value['structuredContent']['ok'], False)
        if code is not None:
            error = value['structuredContent']['error']
            self.assertEqual(error['code'] if isinstance(error, dict) else error, code)

    def test_disabled_missing_and_unknown_keys_are_denied(self):
        key = self.key(enable=False)
        response = self.rpc(key)
        self.assertEqual(response[0], 403)
        self.assert_denied(response)
        self.server.api_access.configure({'enabled': True})
        response = self.rpc()
        self.assertEqual(response[0], 401)
        self.assertIn('Bearer', response[1].get('www-authenticate', ''))
        self.assert_denied(response)
        invalid = {'token': 'r1_' + 'z' * 43}
        self.assert_denied(self.rpc(invalid))
        self.assert_denied(self.request('GET', '/api/integration/status?section=overview'))
        self.assert_denied(self.request('POST', '/api/integration/action',
            {'tool': 'set_screen', 'arguments': {'action': 'wake'}}))

    def test_unavailable_key_storage_fails_closed_with_a_safe_503(self):
        key = self.key(SCOPES)
        self.server.api_access.path.write_text('{invalid fixture state', encoding='utf-8')
        status, _, value = self.rpc(key)
        self.assertEqual(status, 503)
        self.assertEqual(value['error']['code'], 'storage_unavailable')
        self.assertNotIn(str(self.server.api_access.path), json.dumps(value))
        self.assertNotIn(key['token'], json.dumps(value))
        status, _, state = self.browser()
        self.assertEqual(status, 200)
        self.assertIs(state['storage_ok'], False)
        self.assertIs(state['enabled'], False)
        self.assert_no_actions()

    def test_expired_and_revoked_keys_are_denied_immediately(self):
        expiring = self.key(days=1)
        revoked = self.key()
        self.server.api_access.revoke_key(revoked['id'])
        self.assert_denied(self.rpc(revoked))
        self.now += 86400
        self.assert_denied(self.rpc(expiring))
        self.assert_denied(self.request('GET', '/api/integration/status?section=overview',
            headers={'Authorization': 'Bearer ' + expiring['token']}))

    def test_exact_actual_peer_cannot_be_replaced_by_forwarded_headers(self):
        key = self.key(ips=['192.168.31.22'])
        headers = {'Forwarded': 'for=192.168.31.22',
                   'X-Forwarded-For': '192.168.31.22', 'X-Real-IP': '192.168.31.22'}
        self.assert_denied(self.rpc(key, headers=headers))
        self.assert_denied(self.rest_action(key, headers=headers))
        allowed = self.key(ips=['127.0.0.1'])
        self.assertEqual(self.rpc(allowed)[0], 200)

    def test_cookie_and_developer_whitelist_do_not_replace_a_machine_key(self):
        self.server.api_access.configure({'enabled': True})
        self.assert_denied(self.rpc(headers={'Cookie': self.cookie}))
        self.assert_denied(self.request('GET', '/api/integration/status?section=overview',
            headers={'Cookie': self.cookie, 'X-FNM-Request': '1'}))

    def test_legacy_initialize_notification_and_scope_filtered_tools(self):
        key = self.key()
        initialize = self.message('initialize', {
            'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'fixture-client', 'version': '1'}})
        status, _, value = self.rpc(key, initialize)
        self.assertEqual(status, 200)
        self.assertEqual(value['result']['protocolVersion'], PROTOCOL)
        self.assertEqual(value['result']['serverInfo']['name'], 'r1-toolbox')
        status, headers, value = self.rpc(key,
            self.message('notifications/initialized', request_id=_DEFAULT))
        self.assertEqual(status, 202)
        self.assertEqual(value, b'')
        self.assertEqual(headers.get('content-length'), '0')
        status, _, value = self.rpc(key)
        self.assertEqual(status, 200)
        self.assertEqual({tool['name'] for tool in value['result']['tools']}, {'get_nas_status'})
        self.assert_no_actions()

    def test_full_scope_catalog_has_only_the_ten_fixed_tools(self):
        key = self.key(SCOPES)
        status, _, value = self.rpc(key)
        self.assertEqual(status, 200)
        tools = value['result']['tools']
        self.assertEqual(len(tools), 10)
        self.assertEqual({tool['name'] for tool in tools}, TOOL_NAMES)
        for tool in tools:
            self.assertIs(tool['inputSchema']['additionalProperties'], False)
        self.assert_no_actions()

    def test_read_key_cannot_write_or_query_media_over_either_transport(self):
        key = self.key()
        for name, arguments in (('set_screen', {'action': 'wake'}),
                                ('get_media_status', {'action': 'face'})):
            with self.subTest(tool=name):
                status, _, value = self.rpc(key,
                    self.message('tools/call', {'name': name, 'arguments': arguments}))
                self.assertEqual(status, 200)
                self.assert_tool_error(value['result'], 'scope_denied')
                status, _, value = self.rest_action(key, name, arguments)
                self.assertEqual(status, 200)
                self.assert_tool_error(value, 'scope_denied')
        self.assert_no_actions()

    def test_permitted_mcp_and_rest_screen_tools_use_the_original_controller(self):
        key = self.key(('screen',))
        status, _, value = self.rpc(key, self.message('tools/call',
            {'name': 'set_screen', 'arguments': {'action': 'wake'}}))
        self.assertEqual(status, 200)
        self.assertIs(value['result']['isError'], False)
        self.controller.action.assert_called_once_with('screen.wake', {})
        status, _, value = self.rest_action(key, arguments={'action': 'brightness', 'value': 35})
        self.assertEqual(status, 200)
        self.assertIs(value['isError'], False)
        self.assertEqual(self.controller.action.call_args, mock.call('screen.brightness', {'value': 35}))
        self.assertEqual(self.controller.action.call_count, 2)
        self.face.assert_not_called()
        self.batch.assert_not_called()

    def test_rest_and_mcp_reads_use_cached_overview_without_hardware_actions(self):
        key = self.key()
        overview = {'hostname': 'fixture-host', 'temp': None,
                    'disks_detail': [{'name': 'sda', 'temp': None, 'standby': True}]}
        with mock.patch.object(self.server.panel, 'overview', return_value=overview) as source:
            status, _, value = self.request('GET', '/api/integration/status?section=overview',
                headers={'Authorization': 'Bearer ' + key['token']})
            self.assertEqual(status, 200)
            self.assertIs(value['ok'], True)
            self.assertEqual(value['data'], overview)
            status, _, value = self.rpc(key, self.message('tools/call',
                {'name': 'get_nas_status', 'arguments': {'section': 'overview'}}))
            self.assertEqual(status, 200)
            self.assertEqual(value['result']['structuredContent']['data'], overview)
            self.assertEqual(source.call_count, 2)
        self.assert_no_actions()

    def test_all_status_sections_preserve_cached_unknowns_and_disk_names(self):
        key = self.key()
        disk = {'name': 'sda', 'user_name': 'SSD', 'space_names': ['SSD'],
                'temp': None, 'standby': True}
        overview = {'disks_detail': [disk], 'disk_used': None, 'disk_size': None,
                    'temp': None, 'net': None}
        with mock.patch.object(self.server.panel, 'overview', return_value=overview), \
                mock.patch('integration_api.collect_applications', return_value={
                    'available': False, 'status': 'cache_missing', 'data': None}) as applications:
            for section in ('overview', 'system', 'storage', 'temperatures', 'network',
                            'containers', 'apps', 'fans', 'lights', 'screen'):
                with self.subTest(section=section):
                    status, _, value = self.request('GET', '/api/integration/status?section=' + section,
                        headers={'Authorization': 'Bearer ' + key['token']})
                    self.assertEqual(status, 200, value)
                    self.assertIs(value['ok'], True)
                    self.assertEqual(value['section'], section)
                    if section == 'temperatures':
                        self.assertEqual(value['data']['disks'][0]['user_name'], 'SSD')
                        self.assertIsNone(value['data']['disks'][0]['temp'])
                    if section == 'storage':
                        self.assertEqual(value['data']['disks'], [disk])
                        self.assertIsNone(value['data']['used_bytes'])
                    if section == 'apps':
                        self.assertIsNone(value['data']['data'])
                    if section == 'system':
                        self.assertNotIn('data_dir', value['data'])
            applications.assert_called_once_with(state_dir=self.server.state_dir)
        self.assert_no_actions()

    def test_screen_only_key_cannot_read_rest_status(self):
        key = self.key(('screen',))
        with mock.patch.object(self.server.panel, 'overview') as source:
            response = self.request('GET', '/api/integration/status?section=overview',
                headers={'Authorization': 'Bearer ' + key['token']})
            self.assertEqual(response[0], 403)
            source.assert_not_called()
        self.assert_no_actions()

    def test_permitted_media_queries_use_the_same_collectors_and_lock_as_dev_media(self):
        key = self.key(('media',))
        status, _, value = self.rpc(key, self.message('tools/call',
            {'name': 'get_media_status', 'arguments': {'action': 'face'}}))
        self.assertEqual(status, 200)
        self.assertIs(value['result']['isError'], False)
        self.assertEqual(value['result']['structuredContent']['data'], self.face_result)
        status, _, value = self.rest_action(key, 'get_media_status', {'action': 'all'})
        self.assertEqual(status, 200)
        self.assertEqual(value['structuredContent']['data'], self.batch_result)
        self.face.assert_called_once_with()
        self.batch.assert_called_once_with()
        self.server.media_probe_lock.acquire()
        try:
            status, _, value = self.rest_action(key, 'get_media_status', {'action': 'all'})
            self.assertEqual(status, 200)
            self.assert_tool_error(value)
            self.assertEqual(value['structuredContent']['status'], 409)
            self.assertEqual(self.browser('POST', '/api/dev/media',
                {'action': 'probe'}, cookie=False)[0], 409)
        finally:
            self.server.media_probe_lock.release()
        self.assertEqual(self.face.call_count, 1)
        self.assertEqual(self.batch.call_count, 1)
        self.controller.action.assert_not_called()

    def test_machine_media_in_flight_blocks_developer_probe_without_double_collection(self):
        key = self.key(('media',))
        entered, release = threading.Event(), threading.Event()

        def collect():
            entered.set()
            if not release.wait(4):
                raise TimeoutError('fixture was not released')
            return self.face_result

        self.face.side_effect = collect
        message = self.message('tools/call',
            {'name': 'get_media_status', 'arguments': {'action': 'face'}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.rpc, key, message)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.browser('POST', '/api/dev/media',
                    {'action': 'probe_all'}, cookie=False)[0], 409)
                self.batch.assert_not_called()
                self.face.assert_called_once_with()
            finally:
                release.set()
            status, _, value = pending.result(timeout=5)
            self.assertEqual(status, 200)
            self.assertIs(value['result']['isError'], False)
        self.assertFalse(self.server.media_probe_lock.locked())

    def test_fixed_policy_and_power_tools_keep_original_controller_operations(self):
        key = self.key(SCOPES)
        cases = (
            ('set_fan_policy', {'config': {'enabled': True}, 'confirmed': True},
             self.fan_policy.action, 'fan.policy.update',
             {'config': {'enabled': True}, 'apply': True, 'confirmed': True}),
            ('release_fan', {'confirmed': True}, self.fan_policy.action,
             'fan.policy.release', {'confirmed': True}),
            ('set_light_policy', {'config': {'enabled': True}}, self.light_policy.action,
             'light.policy.update', {'config': {'enabled': True}, 'apply': True}),
            ('set_rgb', {'mode': 1, 'color': '#112233'}, self.controller.action,
             'rgb.set', {'mode': 1, 'color': '#112233'}),
            ('update_screen_settings', {'settings': {'saver_timeout': 60}},
             self.controller.action, 'settings.update', {'settings': {'saver_timeout': 60}}),
            ('request_power', {'operation': 'reboot', 'confirmed': True},
             self.controller.action, 'power.request', {'operation': 'reboot', 'confirmed': True}),
            ('cancel_power', {}, self.controller.action, 'power.cancel', {}),
        )
        for tool, arguments, target, operation, payload in cases:
            with self.subTest(tool=tool):
                target.reset_mock()
                status, _, value = self.rest_action(key, tool, arguments)
                self.assertEqual(status, 200)
                self.assertIs(value['isError'], False)
                target.assert_called_once_with(operation, payload)
        self.face.assert_not_called()
        self.batch.assert_not_called()

    def test_media_collector_exception_is_private_and_releases_the_original_lock(self):
        key = self.key(('media',))
        private = '/private/USER_AUTH_DB_101?access_token=secret-fixture'
        self.face.side_effect = OSError(private)
        status, _, value = self.rest_action(key, 'get_media_status', {'action': 'face'})
        self.assertEqual(status, 200)
        self.assert_tool_error(value, 'execution_failed')
        self.assertNotIn(private, json.dumps(value))
        self.assertNotIn('secret-fixture', json.dumps(value))
        self.assertFalse(self.server.media_probe_lock.locked())
        self.face.side_effect = None
        status, _, value = self.rest_action(key, 'get_media_status', {'action': 'face'})
        self.assertEqual(status, 200)
        self.assertIs(value['isError'], False)

    def test_write_lock_busy_prevents_both_machine_transports(self):
        key = self.key(('screen',))
        self.server.operation_lock.acquire()
        try:
            status, _, value = self.rpc(key, self.message('tools/call',
                {'name': 'set_screen', 'arguments': {'action': 'wake'}}))
            self.assertEqual(status, 200)
            self.assert_tool_error(value['result'])
            status, _, value = self.rest_action(key)
            self.assertEqual(status, 200)
            self.assert_tool_error(value)
        finally:
            self.server.operation_lock.release()
        self.assert_no_actions()

    def test_invalid_tool_arguments_and_unknown_actions_cannot_reach_hardware(self):
        key = self.key(SCOPES)
        cases = (('set_screen', {'action': 'wake', 'command': 'id'}),
                 ('set_screen', {'action': 'brightness', 'value': True}),
                 ('set_screen', {'action': 'brightness', 'value': 0}),
                 ('set_fan_policy', {'config': {}, 'confirmed': False}),
                 ('request_power', {'operation': 'reboot', 'confirmed': False}),
                 ('get_media_status', {'action': 'all', 'url': 'http://private/'}),
                 ('exec', {'command': 'id'}))
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=arguments):
                status, _, value = self.rest_action(key, tool, arguments)
                self.assertEqual(status, 200)
                self.assert_tool_error(value)
        self.assert_no_actions()

    def test_tool_notifications_are_acknowledged_without_executing_writes(self):
        key = self.key(('screen',))
        status, _, value = self.rpc(key, self.message('tools/call',
            {'name': 'set_screen', 'arguments': {'action': 'wake'}}, request_id=_DEFAULT))
        self.assertEqual(status, 202)
        self.assertEqual(value, b'')
        self.assert_no_actions()

    def test_machine_origin_optional_but_foreign_origin_and_bad_host_are_rejected(self):
        key = self.key()
        self.assertEqual(self.rpc(key)[0], 200)
        self.assertEqual(self.rpc(key, headers={'Origin': 'http://' + self.host})[0], 200)
        for headers in ({'Origin': 'http://foreign.invalid'}, {'Origin': 'null'},
                        {'Host': 'foreign.invalid'}, {'Host': 'localhost.invalid:' + str(self.server.port)}):
            with self.subTest(headers=headers):
                self.assertEqual(self.rpc(key, headers=headers)[0], 403)
                self.assertEqual(self.rest_action(key, headers=headers)[0], 403)
        self.assert_no_actions()

    def test_duplicate_security_and_mcp_headers_are_rejected(self):
        key = self.key()
        headers = (('Host', self.host), ('Origin', 'http://' + self.host),
                   ('Authorization', 'Bearer ' + key['token']),
                   ('MCP-Protocol-Version', PROTOCOL), ('MCP-Session-Id', 'fixture'),
                   ('Mcp-Method', 'tools/list'), ('Mcp-Name', 'get_nas_status'))
        for name, value in headers:
            with self.subTest(header=name):
                supplied = {} if name in ('Host', 'Authorization', 'MCP-Protocol-Version') else {name: value}
                self.assertEqual(self.rpc(key, headers=supplied,
                    duplicates=((name, value),))[0], 400)
        self.assert_no_actions()

    def test_mcp_query_fragments_framing_and_accept_are_strict(self):
        key = self.key()
        for suffix in ('?action=tools/list', '?token=private', '#fragment'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.rpc(key, path='/mcp' + suffix)[0], 400)
        for kwargs in ({'headers': {'Transfer-Encoding': 'chunked'}},
                       {'headers': {'Expect': '100-continue'}},
                       {'headers': {'Content-Type': 'text/plain'}},
                       {'remove': ('Content-Length',)}, {'remove': ('Content-Type',)}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.rpc(key, **kwargs)[0], 400)
        for accept in ('application/json', 'text/event-stream', '*/*', 'text/plain'):
            with self.subTest(accept=accept):
                self.assertIn(self.rpc(key, headers={'Accept': accept})[0], (400, 406))
        self.assert_no_actions()

    def test_mcp_unacceptable_or_invalid_q_values_cannot_execute_a_tool(self):
        key = self.key(('screen',))
        message = self.message('tools/call',
            {'name': 'set_screen', 'arguments': {'action': 'wake'}})
        accepts = (
            'application/json;q=0, text/event-stream;q=0',
            'application/json;q=0, text/event-stream;q=1',
            'application/json;q=1, text/event-stream;q=0',
            'application/json;q=abc, text/event-stream;q=1',
            'application/json;q=1.1, text/event-stream;q=1',
            'application/json;q=-0.1, text/event-stream;q=1',
            'application/json;q=, text/event-stream;q=1',
            'application/json;q=nan, text/event-stream;q=1',
            'application/json;q=0.1234, text/event-stream;q=1',
        )
        for accept in accepts:
            with self.subTest(accept=accept):
                self.assertEqual(self.rpc(key, message,
                    headers={'Accept': accept})[0], 406)
        self.assert_no_actions()

    def test_mcp_valid_q_values_and_combined_accept_fields_allow_ping(self):
        key = self.key()
        message = self.message('ping')
        status, _, value = self.rpc(key, message, headers={
            'Accept': 'application/json;q=0.5, text/event-stream;q=0.8'})
        self.assertEqual(status, 200)
        self.assertEqual(value['result'], {})
        status, _, value = self.rpc(key, message,
            headers={'Accept': 'application/json;q=0.5'},
            duplicates=(('Accept', 'text/event-stream;q=0.8'),))
        self.assertEqual(status, 200)
        self.assertEqual(value['result'], {})
        self.assert_no_actions()

    def test_malformed_rpc_json_duplicates_and_nonfinite_never_execute_tools(self):
        key = self.key(SCOPES)
        for body in (b'{', b'null', b'[]', b'1', b'false', b'\xff',
                     b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call"}',
                     b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}',
                     b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n{}'):
            with self.subTest(body=body):
                self.assertEqual(self.rpc(key, body)[0], 400)
        for message in ({'method': 'tools/list', 'id': 1},
                        {'jsonrpc': '2.0', 'method': 'tools/list', 'id': True},
                        self.message('tools/list', [])):
            with self.subTest(message=message):
                self.assertEqual(self.rpc(key, message)[0], 400)
        self.assert_no_actions()

    def test_rest_rejects_arbitrary_query_fields_and_nonfixed_action_shapes(self):
        key = self.key(SCOPES)
        headers = {'Authorization': 'Bearer ' + key['token']}
        for suffix in ('?section=overview&section=system', '?section=overview&url=http://private/',
                       '?section=../../private', '?section=overview#fragment'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request('GET', '/api/integration/status' + suffix,
                    headers=headers)[0], 400)
        for body in ({'tool': 'set_screen', 'arguments': {'action': 'wake'}, 'url': 'private'},
                     {'tool': 'set_screen', 'arguments': {'action': 'wake'}, 'confirmed': True},
                     {'tool': 'set_screen', 'arguments': [], 'path': '/private'},
                     {'name': 'set_screen', 'arguments': {'action': 'wake'}}):
            with self.subTest(body=body):
                self.assertEqual(self.request('POST', '/api/integration/action', body,
                    headers=headers)[0], 400)
        self.assert_no_actions()

    def test_mcp_is_post_only_without_cors_preflight(self):
        key = self.key()
        for method in ('GET', 'DELETE', 'OPTIONS', 'PUT', 'PATCH', 'HEAD'):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, '/mcp',
                    headers={'Authorization': 'Bearer ' + key['token']})[0], 405)
        self.assert_no_actions()

    def test_browser_management_requires_current_web_authorization(self):
        key = self.key(SCOPES)
        self.assert_denied(self.browser(cookie=False))
        self.assert_denied(self.browser(cookie=False,
            headers={'Authorization': 'Bearer ' + key['token']}))
        self.assertEqual(self.browser(
            headers={'Authorization': 'Bearer ' + key['token']})[0], 403)
        for path, body in (('/api/access/settings', {'enabled': False}),
                           ('/api/access/keys', {'name': 'unauthorized'}),
                           ('/api/access/keys/revoke', {'id': key['id']})):
            with self.subTest(path=path):
                self.assert_denied(self.browser('POST', path, body, cookie=False,
                    headers={'Authorization': 'Bearer ' + key['token']}))
        self.assertIs(self.server.api_access.snapshot()['enabled'], True)
        self.assertEqual(self.server.api_access.snapshot()['keys'][0]['status'], 'active')

    def test_management_creation_exposes_secret_once_then_only_metadata(self):
        self.assertEqual(self.browser('POST', '/api/access/settings', {'enabled': True})[0], 200)
        status, _, value = self.browser('POST', '/api/access/keys', {
            'name': 'fixture browser key', 'scopes': ['read', 'screen'],
            'allowed_ips': ['127.0.0.1'], 'expires_days': 90})
        self.assertEqual(status, 200)
        self.assertIs(value['ok'], True)
        token, metadata = value['token'], value['key']
        self.assertRegex(token, r'^r1_[A-Za-z0-9_-]{43}$')
        self.assertEqual(metadata['name'], 'fixture browser key')
        self.assertNotIn('token', metadata)
        self.assertNotIn('digest', metadata)
        status, _, later = self.browser()
        self.assertEqual(status, 200)
        serialized = json.dumps(later)
        self.assertIn(metadata['id'], serialized)
        self.assertNotIn(token, serialized)
        self.assertNotIn(hashlib.sha256(token.encode('ascii')).hexdigest(), serialized)
        for row in later['keys']:
            self.assertNotIn('digest', row)
        self.assertNotIn('"digest":', json.dumps(later['audit']))
        self.assertEqual(self.browser('POST', '/api/access/keys/revoke', {'id': metadata['id']})[0], 200)
        self.assert_denied(self.rpc({'token': token}))

    def test_management_get_fragment_is_rejected_without_returning_key_metadata(self):
        key = self.key(SCOPES)
        status, _, value = self.browser(path='/api/access#fragment')
        self.assertEqual(status, 400)
        serialized = json.dumps(value)
        self.assertNotIn(key['id'], serialized)
        self.assertNotIn(key['token'], serialized)
        self.assertNotIn('keys', value)
        self.assert_no_actions()

    def test_browser_management_writes_keep_origin_json_and_csrf_guards(self):
        cases = ({'headers': {'Origin': 'http://foreign.invalid'}},
                 {'remove': ('Origin',)}, {'remove': ('X-FNM-Request',)},
                 {'headers': {'Content-Type': 'text/plain'}},
                 {'remove': ('Content-Length',)})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                status, _, _ = self.browser('POST', '/api/access/settings',
                    {'enabled': True}, **kwargs)
                self.assertIn(status, (400, 403))
        self.assertFalse(self.server.api_access.snapshot()['enabled'])
        self.assert_no_actions()

    def test_api_keys_do_not_unlock_normal_web_writes_or_developer_commands(self):
        key = self.key(SCOPES)
        headers = {'Authorization': 'Bearer ' + key['token']}
        for path, body in (('/api/r1/screen', {'action': 'wake'}),
                           ('/api/r1/settings', {'settings': {'brightness': 30}}),
                           ('/api/dev/exec', {'name': 'identity'})):
            with self.subTest(path=path):
                self.assert_denied(self.browser('POST', path, body, cookie=False, headers=headers))
        self.assert_no_actions()

    def test_public_monitoring_and_session_endpoints_never_expose_keys_or_mcp_state(self):
        key = self.key(SCOPES)
        for path in ('/api/v1/status', '/api/v1/openapi.json', '/api/auth/session'):
            with self.subTest(path=path):
                status, _, value = self.request('GET', path, private=not path.startswith('/api/v1'))
                self.assertEqual(status, 200)
                serialized = json.dumps(value)
                for secret in (key['token'], key['id'], 'api-access.json', 'digest'):
                    self.assertNotIn(secret, serialized)
                self.assertNotIn('/mcp', serialized)
        self.assert_no_actions()

    def test_http_audit_contains_only_safe_bounded_operation_metadata(self):
        key = self.key(('screen',))
        self.assertEqual(self.rest_action(key)[0], 200)
        status, _, value = self.rest_action(key, arguments={
            'action': 'wake', 'command': 'secret-fixture-body'})
        self.assertEqual(status, 200)
        self.assert_tool_error(value)
        status, _, management = self.browser()
        self.assertEqual(status, 200)
        rows = management['audit']
        self.assertGreaterEqual(len(rows), 2)
        self.assertLessEqual(len(rows), 100)
        fields = {'timestamp', 'key_id', 'key_name', 'operation', 'peer',
                  'outcome', 'duration_ms'}
        for row in rows:
            self.assertLessEqual(set(row), fields)
            self.assertIn(row['operation'], ('set_screen', 'integration/action', 'tools/call'))
            self.assertEqual(row['key_id'], key['id'])
            self.assertEqual(row['peer'], '127.0.0.1')
        serialized = json.dumps(rows)
        for private in (key['token'], 'secret-fixture-body', 'Authorization',
                        'arguments', 'digest'):
            self.assertNotIn(private, serialized)
        self.controller.action.assert_called_once_with('screen.wake', {})

    def test_existing_password_write_and_developer_media_authorization_still_work(self):
        self.assertEqual(self.browser('POST', '/api/r1/screen', {'action': 'wake'})[0], 200)
        self.controller.action.assert_called_once_with('screen.wake', {})
        for action, field in (('probe', 'probe'), ('probe_all', 'batch')):
            with self.subTest(action=action):
                status, _, value = self.browser('POST', '/api/dev/media',
                    {'action': action}, cookie=False)
                self.assertEqual(status, 200)
                self.assertIs(value['ok'], True)
                self.assertIn(field, value)
        self.face.assert_called_once_with()
        self.batch.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
