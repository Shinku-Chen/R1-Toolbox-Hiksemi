"""Pure MCP protocol checks; fixtures never contact a NAS or change hardware."""

import base64
import copy
import json
from email.message import Message
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

from mcp_service import (LEGACY_VERSIONS, META_CLIENT_CAPABILITIES,
                         META_CLIENT_INFO, META_MINIMUM_ROLE, META_REQUIRED_SCOPE, META_SERVER_INFO,
                         META_VERSION, MODERN_VERSION, READ_SECTIONS,
                         SUPPORTED_VERSIONS, McpService, tool_catalog,
                         validate_http_metadata)
from api_permissions import MIN_ROLE_BY_TOOL as DECLARED_MIN_ROLE_BY_TOOL, LEGACY_TOOLS
MIN_ROLE_BY_TOOL = {name: DECLARED_MIN_ROLE_BY_TOOL[name] for name in LEGACY_TOOLS}


def modern_meta(version=MODERN_VERSION):
    return {META_VERSION: version, META_CLIENT_CAPABILITIES: {},
            META_CLIENT_INFO: {'name': 'fixture-ai', 'version': '1.0'}}


def request(method, params=None, *, modern=False, request_id=1):
    body = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None or modern:
        body['params'] = copy.deepcopy(params or {})
    if modern:
        body['params']['_meta'] = modern_meta()
    return body


class McpServiceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.principal = {'key_id': 'api-test', 'name': 'fixture',
                          'scopes': ['read', 'media', 'fan', 'light', 'screen', 'power']}

        def execute(name, arguments, principal):
            self.calls.append((name, arguments, principal))
            return {'ok': True, 'tool': name, 'arguments': arguments,
                    'unknown': None}

        self.service = McpService(execute, version='fixture-build')

    def handle(self, body, version=None, principal=None):
        return self.service.handle(body, self.principal if principal is None else principal,
                                   protocol_version=version)

    def assert_rpc_error(self, result, code, status=None):
        http_status, payload = result
        if status is not None:
            self.assertEqual(status, http_status)
        self.assertEqual(code, payload['error']['code'])
        self.assertNotIn('result', payload)
        self.assertEqual([], self.calls)

    def test_catalog_is_detached_public_metadata_and_has_no_source_calls(self):
        public = tool_catalog()
        self.assertEqual(69, len(public))
        self.assertEqual(69, len({item['name'] for item in public}))
        self.assertTrue(all(item['name'].replace('_', '').isalnum() for item in public))
        self.assertTrue(all(item['inputSchema']['additionalProperties'] is False
                            for item in public))
        self.assertEqual('read', public[0]['_meta'][META_REQUIRED_SCOPE])
        self.assertEqual(['get_nas_status'], [item['name'] for item in
                                            tool_catalog({'scopes': ['read']})])
        self.assertEqual([], tool_catalog({'scopes': 'read'}))
        self.assertEqual([], tool_catalog({}))
        public[0]['inputSchema']['properties']['section']['enum'].clear()
        self.assertEqual(list(READ_SECTIONS), tool_catalog()[0]['inputSchema']['properties']['section']['enum'])
        self.assertEqual([], self.calls)

    def test_side_effect_and_power_descriptions_are_truthful(self):
        catalog = {item['name']: item for item in tool_catalog()}
        self.assertTrue(catalog['get_nas_status']['annotations']['readOnlyHint'])
        self.assertFalse(catalog['get_media_status']['annotations']['readOnlyHint'])
        self.assertFalse(catalog['get_media_status']['annotations']['idempotentHint'])
        self.assertIn('完成标记', catalog['get_media_status']['description'])
        self.assertTrue(catalog['request_power']['annotations']['destructiveHint'])
        self.assertIn('5 秒', catalog['request_power']['description'])
        self.assertNotIn('exec', catalog)
        self.assertNotIn('execute_sql', catalog)

    def test_fan_schema_advertises_two_thresholds_and_strict_legacy_input_shape(self):
        catalog = {item['name']: item for item in tool_catalog()}
        schema = catalog['set_fan_policy']['inputSchema']['properties']['config']
        self.assertIn('无迟滞', schema['description'])
        self.assertIn('响应与保存统一为 v2', schema['description'])
        self.assertEqual([2, 1], [item['properties']['schema_version']['const'] for item in schema['oneOf']])
        for version in schema['oneOf']:
            number = version['properties']['schema_version']['const']
            channels = version['properties']['channels']['properties']
            self.assertEqual({'cpu', 'system'}, set(channels))
            for channel, definition in channels.items():
                fields = definition['properties']
                self.assertFalse(definition['additionalProperties'])
                temperatures = ('low_temp', 'high_temp') if number == 2 else ('quiet_below', 'normal_at', 'full_at')
                excluded = ('quiet_below', 'normal_at', 'full_at') if number == 2 else ('low_temp', 'high_temp')
                self.assertFalse(set(excluded) & set(fields))
                self.assertTrue(set(temperatures) <= set(definition['required']))
                for name in temperatures:
                    self.assertEqual({'type': 'integer', 'minimum': 15, 'maximum': 85}, fields[name])
                self.assertEqual(10, fields['quiet_percent']['minimum'])
                self.assertEqual(channel == 'system', 'vendor_mode' in fields)
                self.assertNotIn('vendor_mode', definition['required'])
        schema['oneOf'][0]['properties']['channels']['properties']['cpu']['properties'].clear()
        fresh = next(item for item in tool_catalog() if item['name'] == 'set_fan_policy')
        self.assertIn('low_temp', fresh['inputSchema']['properties']['config']['oneOf'][0]
                      ['properties']['channels']['properties']['cpu']['properties'])
        self.assertEqual([], self.calls)

    def test_legacy_initialize_echoes_all_supported_legacy_versions(self):
        for version in LEGACY_VERSIONS:
            with self.subTest(version=version):
                status, payload = self.handle(request('initialize', {
                    'protocolVersion': version, 'capabilities': {},
                    'clientInfo': {'name': 'fixture', 'version': '1.0'}}))
                self.assertEqual(200, status)
                self.assertEqual(version, payload['result']['protocolVersion'])
                self.assertEqual('fixture-build', payload['result']['serverInfo']['version'])
                self.assertEqual({'tools': {'listChanged': False}}, payload['result']['capabilities'])
                self.assertNotIn('resultType', payload['result'])
        self.assertEqual([], self.calls)

    def test_legacy_initialize_unknown_version_offers_supported_legacy(self):
        status, payload = self.handle(request('initialize', {
            'protocolVersion': '1900-01-01', 'capabilities': {},
            'clientInfo': {'name': 'fixture', 'version': '1.0'}}))
        self.assertEqual(200, status)
        self.assertEqual(LEGACY_VERSIONS[0], payload['result']['protocolVersion'])

    def test_initialize_rejects_extra_fields_and_wrong_types(self):
        valid = {'protocolVersion': LEGACY_VERSIONS[0], 'capabilities': {},
                 'clientInfo': {'name': 'fixture', 'version': '1.0'}}
        bad = [dict(valid, unused=True), dict(valid, capabilities=[]),
               dict(valid, protocolVersion=1), dict(valid, clientInfo={'name': 'x'}),
               dict(valid, clientInfo={'name': 'x', 'version': '1', 'secret': 'x'})]
        for params in bad:
            with self.subTest(params=params):
                self.assert_rpc_error(self.handle(request('initialize', params)), -32602, 400)

    def test_modern_discovery_is_stateless_and_has_required_result_type(self):
        body = request('server/discover', modern=True, request_id='discover-1')
        status, payload = self.handle(body, MODERN_VERSION)
        self.assertEqual(200, status)
        self.assertEqual('discover-1', payload['id'])
        result = payload['result']
        self.assertEqual('complete', result['resultType'])
        self.assertEqual(list(SUPPORTED_VERSIONS), result['supportedVersions'])
        self.assertEqual('r1-toolbox', result['_meta'][META_SERVER_INFO]['name'])
        self.assertNotIn('resources', result['capabilities'])
        self.assertEqual([], self.calls)

    def test_modern_client_info_is_optional_but_capabilities_are_required(self):
        body = request('ping', modern=True)
        del body['params']['_meta'][META_CLIENT_INFO]
        self.assertEqual(200, self.handle(body, MODERN_VERSION)[0])
        del body['params']['_meta'][META_CLIENT_CAPABILITIES]
        self.assert_rpc_error(self.handle(body, MODERN_VERSION), -32602, 400)

    def test_modern_missing_metadata_and_mismatched_version_are_rejected(self):
        self.assert_rpc_error(self.handle(request('tools/list'), MODERN_VERSION), -32602, 400)
        body = request('tools/list', modern=True)
        self.assert_rpc_error(self.handle(body), -32020, 400)
        self.assert_rpc_error(self.handle(body, LEGACY_VERSIONS[0]), -32020, 400)
        body['params']['_meta'][META_VERSION] = LEGACY_VERSIONS[0]
        self.assert_rpc_error(self.handle(body, MODERN_VERSION), -32020, 400)

    def test_version_metadata_rejects_wrong_type_in_either_era(self):
        for version in (None, 1, [], {}):
            body = request('tools/list', {'_meta': {META_VERSION: version}})
            with self.subTest(version=version):
                self.assert_rpc_error(self.handle(body), -32602, 400)

    def test_unsupported_header_version_has_negotiation_data(self):
        status, body = self.handle(request('tools/list'), '1900-01-01')
        self.assertEqual(400, status)
        self.assertEqual(-32022, body['error']['code'])
        self.assertEqual(list(SUPPORTED_VERSIONS), body['error']['data']['supported'])
        self.assertEqual('1900-01-01', body['error']['data']['requested'])

    def test_unknown_method_differs_by_protocol_era(self):
        self.assert_rpc_error(self.handle(request('arbitrary/run')), -32601, 200)
        self.assert_rpc_error(self.handle(request('arbitrary/run', modern=True), MODERN_VERSION),
                              -32601, 404)
        self.assert_rpc_error(self.handle(request('initialize', modern=True), MODERN_VERSION),
                              -32601, 404)

    def test_jsonrpc_envelope_and_identifier_validation(self):
        bad = [[], [request('ping')], None, 'json', {},
               {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'},
               dict(request('ping'), extra=True),
               dict(request('ping'), result={}),
               dict(request('ping'), method=1),
               dict(request('ping'), method='ping\n'),
               dict(request('ping'), id=None), dict(request('ping'), id=True),
               dict(request('ping'), id=1.5), dict(request('ping'), id=[]),
               dict(request('ping'), id=2 ** 53), dict(request('ping'), id='\ud800')]
        for value in bad:
            with self.subTest(value=value):
                self.assert_rpc_error(self.handle(value), -32600, 400)
        for value in (0, -1, 2 ** 53 - 1, '', 'id'):
            with self.subTest(value=value):
                self.assertEqual(value, self.handle(request('ping', request_id=value))[1]['id'])

    def test_params_must_be_objects_and_bounded_json(self):
        for params in (None, [], 'x', False, {'_meta': []}, {'_meta': {'x': float('nan')}},
                       {'_meta': {'x': '\ud800'}}, {'_meta': {'\ud800': 'x'}}):
            body = dict(request('ping'), params=params)
            with self.subTest(params=params):
                self.assert_rpc_error(self.handle(body), -32602, 400)
        deep = {}
        deep['cycle'] = deep
        self.assert_rpc_error(self.handle(dict(request('ping'), params=deep)), -32602, 400)

    def test_notifications_have_no_body_and_do_not_execute_even_tools_call(self):
        for method, params in (('notifications/initialized', {}),
                               ('notifications/cancelled', {'requestId': 8}),
                               ('unknown/notification', {}),
                               ('tools/call', {'name': 'request_power',
                                               'arguments': {'operation': 'reboot', 'confirmed': True}})):
            with self.subTest(method=method):
                self.assertEqual((202, None), self.handle({'jsonrpc': '2.0', 'method': method,
                                                          'params': params}))
        self.assertEqual([], self.calls)
        self.assert_rpc_error(self.handle(request('notifications/initialized')), -32600, 400)
        self.assert_rpc_error(self.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized',
                                          'params': {'unknown': True}}), -32602, 400)

    def test_tools_list_filters_scope_on_every_request(self):
        principal = {'key_id': 'ro', 'scopes': ['read']}
        for modern in (False, True):
            with self.subTest(modern=modern):
                status, payload = self.handle(request('tools/list', modern=modern),
                                               MODERN_VERSION if modern else None, principal)
                self.assertEqual(200, status)
                self.assertEqual(['get_nas_status'], [tool['name'] for tool in payload['result']['tools']])
        principal['scopes'] = []
        self.assertEqual([], self.handle(request('tools/list'), principal=principal)[1]['result']['tools'])
        self.assertEqual([], self.calls)

    def test_tools_call_structure_and_unknown_tool_are_protocol_errors(self):
        bad = [{'name': 'get_nas_status', 'arguments': {}, 'unexpected': True},
               {'name': 1}, {'name': 'get_nas_status', 'arguments': []},
               {'name': 'get_nas_status', 'arguments': None}, {},
               {'name': 'shell', 'arguments': {'command': 'reboot'}}]
        for params in bad:
            with self.subTest(params=params):
                self.assert_rpc_error(self.handle(request('tools/call', params)), -32602, 400)

    def test_tools_list_and_ping_reject_unexpected_params(self):
        for method in ('tools/list', 'ping'):
            self.assert_rpc_error(self.handle(request(method, {'cursor': 'not-issued'})), -32602, 400)
        self.assert_rpc_error(self.handle(request('server/discover', {'extra': True}, modern=True),
                                         MODERN_VERSION), -32602, 400)

    def test_call_tool_enforces_scope_before_validating_or_executing(self):
        principals = [None, {}, {'scopes': 'power'}, {'scopes': ['read']},
                      {'scopes': ['*']}, {'scopes': ['POWER']}]
        for principal in principals:
            with self.subTest(principal=principal):
                result = self.service.call_tool('request_power', {'operation': 'reboot',
                                                                 'confirmed': True}, principal)
                self.assertTrue(result['isError'])
                self.assertEqual('scope_denied', result['structuredContent']['error']['code'])
        self.assertEqual([], self.calls)

    @staticmethod
    def role_principal(role, scopes=None):
        return {'key_id': 'role-fixture', 'name': 'fixture', 'policy_version': 2,
                'role': role, 'scopes': list(scopes if scopes is not None else
                    ('read', 'media', 'fan', 'light', 'screen', 'power')),
                'file_roots': []}

    def test_catalog_announces_exact_minimum_roles_for_registered_tools(self):
        metadata = {item['name']: item['_meta'][META_MINIMUM_ROLE]
                    for item in tool_catalog()}
        self.assertEqual({name: DECLARED_MIN_ROLE_BY_TOOL[name] for name in metadata}, metadata)
        self.assertEqual('readonly', metadata['get_nas_status'])
        self.assertEqual('readwrite', metadata['get_media_status'])
        self.assertEqual('admin', metadata['request_power'])
        self.assertEqual('admin', metadata['cancel_power'])
        self.assertEqual(69, len(metadata))

    def test_catalog_combines_roles_with_explicit_domains(self):
        expected = {
            'readonly': {'get_nas_status'},
            'readwrite': set(MIN_ROLE_BY_TOOL) - {'request_power', 'cancel_power'},
            'admin': set(MIN_ROLE_BY_TOOL),
        }
        for role, names in expected.items():
            with self.subTest(role=role):
                principal = self.role_principal(role)
                self.assertEqual(names, {item['name'] for item in tool_catalog(principal)})
                for modern in (False, True):
                    status, body = self.handle(request('tools/list', modern=modern),
                        MODERN_VERSION if modern else None, principal)
                    self.assertEqual(200, status)
                    self.assertEqual(names, {item['name'] for item in body['result']['tools']})
        self.assertEqual([], tool_catalog(self.role_principal('admin', ['files', 'docker'])))
        self.assertEqual([], tool_catalog(self.role_principal('readonly', ['media', 'power'])))
        self.assertEqual({'set_screen', 'update_screen_settings'}, {
            item['name'] for item in tool_catalog(self.role_principal('admin', ['screen']))})
        self.assertEqual([], self.calls)

    def test_all_tool_calls_follow_same_role_matrix_as_catalog(self):
        fixtures = {
            'get_nas_status': {}, 'get_media_status': {},
            'set_fan_policy': {'config': {}, 'confirmed': True},
            'release_fan': {'confirmed': True},
            'set_light_policy': {'config': {}},
            'set_rgb': {'mode': 0, 'color': '#123456'},
            'set_screen': {'action': 'wake'}, 'update_screen_settings': {'settings': {}},
            'request_power': {'operation': 'reboot', 'confirmed': True}, 'cancel_power': {},
        }
        for role in ('readonly', 'readwrite', 'admin'):
            principal = self.role_principal(role)
            listed = {item['name'] for item in tool_catalog(principal)}
            for name, arguments in fixtures.items():
                with self.subTest(role=role, tool=name):
                    self.calls.clear()
                    result = self.service.call_tool(name, arguments, principal)
                    if name in listed:
                        self.assertFalse(result['isError'])
                        self.assertEqual(1, len(self.calls))
                        self.assertEqual(name, self.calls[0][0])
                    else:
                        self.assertTrue(result['isError'])
                        self.assertEqual('role_denied', result['structuredContent']['error']['code'])
                        self.assertEqual([], self.calls)
        self.calls.clear()

    def test_role_and_scope_denials_precede_argument_validation_without_echo(self):
        secret = 'PRIVATE_INPUT_MUST_NOT_BE_REFLECTED'
        cases = [
            (self.role_principal('readonly', ['screen']), 'set_screen', 'role_denied'),
            (self.role_principal('readwrite', ['power']), 'request_power', 'role_denied'),
            (self.role_principal('admin', ['read']), 'request_power', 'scope_denied'),
            (self.role_principal('admin', ['files']), 'get_nas_status', 'scope_denied'),
        ]
        for principal, tool, expected in cases:
            with self.subTest(tool=tool, role=principal['role']):
                result = self.service.call_tool(tool, {'invalid': secret}, principal)
                self.assertTrue(result['isError'])
                self.assertEqual(expected, result['structuredContent']['error']['code'])
                self.assertNotIn(secret, json.dumps(result))
        self.assertEqual([], self.calls)

    def test_malformed_policy_principals_fail_closed(self):
        valid = self.role_principal('admin')
        malformed = [None, 'admin', [], {}, {'scopes': 'read'},
                     dict(valid, role='ADMIN'), dict(valid, role=None),
                     dict(valid, role='legacy'), dict(valid, policy_version=1),
                     dict(valid, policy_version=True), dict(valid, scopes=['read', '*']),
                     dict(valid, scopes=['read', 'read']), dict(valid, file_roots='all')]
        for principal in malformed:
            with self.subTest(principal=principal):
                # None without an argument means the browser metadata catalog;
                # an explicitly unauthenticated machine caller passes {}.
                self.assertEqual([], tool_catalog(principal if principal is not None else {}))
                result = self.service.call_tool('get_nas_status', {}, principal)
                self.assertTrue(result['isError'])
                self.assertEqual('scope_denied', result['structuredContent']['error']['code'])
        self.assertEqual([], self.calls)

    def test_legacy_keys_keep_original_scope_grants_without_implicit_upgrade(self):
        variants = [self.principal,
                    dict(self.principal, policy_version=1, role='legacy', file_roots=[])]
        for principal in variants:
            with self.subTest(explicit_legacy='role' in principal):
                self.assertEqual(set(MIN_ROLE_BY_TOOL),
                    {item['name'] for item in tool_catalog(principal)})
                # New keys need admin for power, but existing flat power grants
                # remain usable without rewriting an old key into an admin key.
                result = self.service.call_tool('request_power',
                    {'operation': 'reboot', 'confirmed': True}, principal)
                self.assertFalse(result['isError'])
                self.assertEqual('request_power', self.calls[-1][0])
        self.calls.clear()
        media_only = {'scopes': ['media']}
        self.assertEqual(['get_media_status'], [item['name'] for item in tool_catalog(media_only)])
        self.assertFalse(self.service.call_tool('get_media_status', {}, media_only)['isError'])
        self.calls.clear()

    def test_unknown_tool_rest_result_is_safe(self):
        result = self.service.call_tool('shell\nSECRET', {}, self.principal)
        self.assertTrue(result['isError'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual('unknown_tool', result['structuredContent']['error']['code'])

    def test_all_tools_accept_valid_fixtures_with_defaults(self):
        fixtures = [('get_nas_status', {}, {'section': 'overview'}),
                    ('get_media_status', {}, {'action': 'face'}),
                    ('set_fan_policy', {'config': {'cpu': {}}, 'confirmed': True},
                     {'config': {'cpu': {}}, 'confirmed': True, 'apply': True}),
                    ('release_fan', {'confirmed': True}, {'confirmed': True}),
                    ('set_light_policy', {'config': {'enabled': True}},
                     {'config': {'enabled': True}, 'apply': True}),
                    ('set_rgb', {'mode': 3, 'color': '#abcdef', 'brightness': 0},
                     {'mode': 3, 'color': '#abcdef', 'brightness': 0}),
                    ('set_screen', {'action': 'brightness', 'value': 5},
                     {'action': 'brightness', 'value': 5}),
                    ('update_screen_settings', {'settings': {'timeout': 60}},
                     {'settings': {'timeout': 60}}),
                    ('request_power', {'operation': 'poweroff', 'confirmed': True},
                     {'operation': 'poweroff', 'confirmed': True}),
                    ('cancel_power', {}, {})]
        for name, arguments, expected in fixtures:
            with self.subTest(name=name):
                result = self.service.call_tool(name, arguments, self.principal)
                self.assertFalse(result['isError'])
                self.assertEqual(expected, self.calls[-1][1])
                self.assertEqual(result['structuredContent'], json.loads(result['content'][0]['text']))
                self.assertIs(self.principal, self.calls[-1][2])
        self.assertEqual(10, len(self.calls))

    def test_tool_validation_rejects_extra_keys_types_ranges_and_missing_confirmation(self):
        invalid = [('get_nas_status', {'section': 'shell'}),
                   ('get_nas_status', {'section': True}),
                   ('get_nas_status', {'path': '/etc/passwd'}),
                   ('get_media_status', {'action': 'login'}),
                   ('get_media_status', {'action': 'all', 'token': 'secret'}),
                   ('set_fan_policy', {'config': {}, 'confirmed': False}),
                   ('set_fan_policy', {'config': {}, 'confirmed': 1}),
                   ('set_fan_policy', {'config': {}}),
                   ('set_fan_policy', {'config': [], 'confirmed': True}),
                   ('set_fan_policy', {'config': {}, 'confirmed': True, 'apply': 1}),
                   ('release_fan', {}), ('release_fan', {'confirmed': 'true'}),
                   ('set_light_policy', {'config': {}, 'apply': None}),
                   ('set_light_policy', {'config': {str(index): 1 for index in range(65)}}),
                   ('set_rgb', {'mode': True, 'color': '#123456'}),
                   ('set_rgb', {'mode': 4, 'color': '#123456'}),
                   ('set_rgb', {'mode': 1, 'color': '123456'}),
                   ('set_rgb', {'mode': 1, 'color': '#123456\n'}),
                   ('set_rgb', {'mode': 1, 'color': '#123456', 'brightness': 101}),
                   ('set_screen', {'action': 'brightness'}),
                   ('set_screen', {'action': 'brightness', 'value': 4}),
                   ('set_screen', {'action': 'brightness', 'value': 5.0}),
                   ('set_screen', {'action': 'wake', 'value': 5}),
                   ('set_screen', {'action': 'sleep', 'value': 100}),
                   ('update_screen_settings', {'settings': []}),
                   ('update_screen_settings', {'settings': {'bad': float('inf')}}),
                   ('request_power', {'operation': 'halt', 'confirmed': True}),
                   ('request_power', {'operation': 'reboot', 'confirmed': False}),
                   ('request_power', {'operation': 'reboot'}),
                   ('cancel_power', {'confirmed': True})]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                result = self.service.call_tool(name, arguments, self.principal)
                self.assertTrue(result['isError'])
                self.assertEqual('invalid_arguments', result['structuredContent']['error']['code'])
        self.assertEqual([], self.calls)

    def test_valid_boundaries_and_screen_actions(self):
        valid = [('set_rgb', {'mode': 0, 'color': '#123456', 'brightness': 100}),
                 ('set_screen', {'action': 'brightness', 'value': 100}),
                 ('set_screen', {'action': 'wake'}), ('set_screen', {'action': 'sleep'}),
                 ('get_media_status', {'action': 'all'}),
                 ('set_light_policy', {'config': {}, 'apply': False})]
        for name, args in valid:
            self.assertFalse(self.service.call_tool(name, args, self.principal)['isError'])

    def test_tool_input_errors_are_actionable_result_errors_not_rpc_errors(self):
        status, payload = self.handle(request('tools/call', {
            'name': 'request_power', 'arguments': {'operation': 'reboot'}}))
        self.assertEqual(200, status)
        self.assertTrue(payload['result']['isError'])
        self.assertNotIn('error', payload)
        self.assertEqual([], self.calls)

    def test_modern_tool_result_and_legacy_text_are_consistent(self):
        status, payload = self.handle(request('tools/call', {'name': 'get_nas_status'}, modern=True),
                                       MODERN_VERSION)
        self.assertEqual(200, status)
        result = payload['result']
        self.assertEqual('complete', result['resultType'])
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent'], json.loads(result['content'][0]['text']))

    def test_callback_failure_and_exception_never_leak_exception_text(self):
        def execute(*unused):
            raise ValueError('private-token-and-filesystem-path')

        service = McpService(execute)
        result = service.call_tool('get_nas_status', {}, self.principal)
        self.assertTrue(result['isError'])
        self.assertEqual('execution_failed', result['structuredContent']['error']['code'])
        self.assertNotIn('private-token', json.dumps(result))
        service.execute = lambda *unused: {'ok': False, 'reason': 'preflight_failed'}
        self.assertTrue(service.call_tool('get_nas_status', {}, self.principal)['isError'])

    def test_invalid_callback_results_are_rejected_without_stringification(self):
        for value in (None, [], object(), {'ok': 0}, {'ok': True, 'value': float('nan')},
                      {'ok': True, 'bytes': b'secret'}, {'ok': True, 'bad': object()}):
            with self.subTest(value=type(value).__name__):
                self.service.execute = lambda *unused: value
                result = self.service.call_tool('get_nas_status', {}, self.principal)
                self.assertTrue(result['isError'])
                self.assertEqual('invalid_result', result['structuredContent']['error']['code'])
                self.assertNotIn('secret', json.dumps(result))

    def test_arguments_and_results_are_detached_from_mutable_inputs(self):
        arguments = {'settings': {'temperature_sources': ['cpu']}}
        result = self.service.call_tool('update_screen_settings', arguments, self.principal)
        self.calls[0][1]['settings']['temperature_sources'].append('disk')
        self.assertEqual(['cpu'], arguments['settings']['temperature_sources'])
        self.assertEqual(['cpu'], result['structuredContent']['arguments']['settings']['temperature_sources'])


class McpHttpMetadataTests(unittest.TestCase):
    def setUp(self):
        self.body = request('tools/call', {'name': 'get_nas_status', 'arguments': {}}, modern=True)
        self.headers = {'MCP-Protocol-Version': MODERN_VERSION,
                        'Mcp-Method': 'tools/call', 'Mcp-Name': 'get_nas_status'}

    def assert_error(self, code, body=None, headers=None, **kwargs):
        result = validate_http_metadata(self.body if body is None else body,
                                        self.headers if headers is None else headers, **kwargs)
        self.assertIsNotNone(result)
        status, payload = result
        self.assertEqual(400, status)
        self.assertEqual(code, payload['error']['code'])
        return payload

    def test_valid_modern_and_case_insensitive_headers(self):
        self.assertIsNone(validate_http_metadata(self.body, self.headers))
        self.assertIsNone(validate_http_metadata(self.body,
                         {key.lower(): value for key, value in self.headers.items()}))
        message_headers = Message()
        for key, value in self.headers.items():
            message_headers[key] = value
        self.assertIsNone(validate_http_metadata(self.body, message_headers))

    def test_legacy_headerless_and_supported_headers_need_no_mirrors(self):
        for version in (None,) + LEGACY_VERSIONS:
            with self.subTest(version=version):
                headers = {} if version is None else {'MCP-Protocol-Version': version}
                self.assertIsNone(validate_http_metadata(request('tools/list'), headers))

    def test_missing_or_mismatched_protocol_header(self):
        self.assert_error(-32020, headers={'Mcp-Method': 'tools/call', 'Mcp-Name': 'get_nas_status'})
        self.assert_error(-32020, headers=dict(self.headers, **{'MCP-Protocol-Version': LEGACY_VERSIONS[0]}))
        self.assert_error(-32020, protocol_version=LEGACY_VERSIONS[0])

    def test_explicit_protocol_version_works_when_caller_has_extracted_header(self):
        headers = {key: value for key, value in self.headers.items() if key != 'MCP-Protocol-Version'}
        self.assertIsNone(validate_http_metadata(self.body, headers, protocol_version=MODERN_VERSION))

    def test_missing_and_invalid_modern_meta(self):
        for meta in (None, {}, {META_VERSION: MODERN_VERSION},
                     {META_VERSION: MODERN_VERSION, META_CLIENT_CAPABILITIES: []},
                     {META_VERSION: MODERN_VERSION, META_CLIENT_CAPABILITIES: {},
                      META_CLIENT_INFO: {'name': 'x'}}):
            body = copy.deepcopy(self.body)
            if meta is None:
                del body['params']['_meta']
            else:
                body['params']['_meta'] = meta
            with self.subTest(meta=meta):
                self.assert_error(-32602, body=body)

    def test_unknown_version_reports_supported_versions(self):
        body = copy.deepcopy(self.body)
        body['params']['_meta'][META_VERSION] = '1900-01-01'
        payload = self.assert_error(-32022, body=body,
                                   headers=dict(self.headers, **{'MCP-Protocol-Version': '1900-01-01'}))
        self.assertEqual(list(SUPPORTED_VERSIONS), payload['error']['data']['supported'])

    def test_method_mirror_is_required_case_sensitive_and_ascii(self):
        for method in (None, 'tools/list', 'TOOLS/CALL', 'tools/call\r', 'tools/call\t', '工具'):
            headers = dict(self.headers)
            if method is None:
                del headers['Mcp-Method']
            else:
                headers['Mcp-Method'] = method
            with self.subTest(method=method):
                self.assert_error(-32020, headers=headers)

    def test_name_mirror_is_required_and_decodes_official_base64_sentinel(self):
        for name in ('get_nas_status', '工具', ' padded ', '=?base64?literal?='):
            body = copy.deepcopy(self.body)
            body['params']['name'] = name
            encoded = '=?base64?' + base64.b64encode(name.encode('utf-8')).decode('ascii') + '?='
            self.assertIsNone(validate_http_metadata(body, dict(self.headers, **{'Mcp-Name': encoded})))
        for name in (None, 'get_media_status', 'get_nas_status\n', ' 工具 ', '=?base64?%%%?=',
                     '=?base64?/w==?='):
            headers = dict(self.headers)
            if name is None:
                del headers['Mcp-Name']
            else:
                headers['Mcp-Name'] = name
            with self.subTest(name=name):
                self.assert_error(-32020, headers=headers)

    def test_other_modern_methods_do_not_require_tool_name(self):
        body = request('server/discover', modern=True)
        self.assertIsNone(validate_http_metadata(body, {'MCP-Protocol-Version': MODERN_VERSION,
                                                       'Mcp-Method': 'server/discover'}))

    def test_header_errors_do_not_reflect_bad_header_or_tool_name(self):
        payload = self.assert_error(-32020, headers=dict(self.headers, **{'Mcp-Name': 'SECRET'}))
        self.assertNotIn('SECRET', json.dumps(payload))
        payload = self.assert_error(-32022, headers={'MCP-Protocol-Version': 'bad\nversion'})
        self.assertIsNone(payload['error']['data']['requested'])


if __name__ == '__main__':
    unittest.main()
