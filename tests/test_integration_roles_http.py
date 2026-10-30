"""Role and capability boundaries through the real integration HTTP fixture.

The fixture contains no NAS, Docker socket or real files. Reuse only its helper
methods, rather than subclassing its test class and collecting old tests twice.
"""

import copy
import json
import os
import unittest

import test_integration_http as fixture
from api_permissions import MIN_ROLE_BY_TOOL as DECLARED_MIN_ROLE_BY_TOOL, LEGACY_TOOLS
MIN_ROLE_BY_TOOL = {name: DECLARED_MIN_ROLE_BY_TOOL[name] for name in LEGACY_TOOLS}


CAPABILITIES = '/api/integration/capabilities'
ROOT_ID = 'root_' + '1' * 24
OTHER_ROOT_ID = 'root_' + '2' * 24
MISSING_ROOT_ID = 'root_' + '3' * 24
ROOT_CATALOG = {
    'available': True, 'status': 'authorization_plan', 'boundary_version': 2,
    'reason': '仅用于授权范围选择，当前未提供文件操作工具。',
    'roots': [
        {'id': ROOT_ID, 'label': '主磁盘', 'mount': '/drives/data/Users', 'read_only': False},
        {'id': OTHER_ROOT_ID, 'label': '其它私有空间',
         'mount': '/drives/private-unselected/Users', 'read_only': True},
    ],
}


class IntegrationRoleHTTPTests(unittest.TestCase):
    setUp = fixture.IntegrationHTTPTests.setUp
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    rpc = fixture.IntegrationHTTPTests.rpc
    rest_action = fixture.IntegrationHTTPTests.rest_action
    browser = fixture.IntegrationHTTPTests.browser
    message = staticmethod(fixture.IntegrationHTTPTests.message)
    assert_no_actions = fixture.IntegrationHTTPTests.assert_no_actions
    assert_denied = fixture.IntegrationHTTPTests.assert_denied
    assert_tool_error = fixture.IntegrationHTTPTests.assert_tool_error
    legacy_key = fixture.IntegrationHTTPTests.key

    def create_body(self, role='readonly', scopes=('read',), **updates):
        return {'name': '角色 fixture', 'role': role, 'scopes': list(scopes),
                'file_roots': [], 'allowed_ips': ['127.0.0.1'],
                'expires_days': 1, **updates}

    def key(self, role='readonly', scopes=('read',), *, enable=True, **updates):
        if enable:
            self.server.api_access.configure({'enabled': True})
        status, _, created = self.browser('POST', '/api/access/keys',
            self.create_body(role, scopes, **updates))
        self.assertEqual(200, status, created)
        self.assertTrue(created['ok'])
        return {**created['key'], 'token': created['token']}

    def capabilities(self, key=None, *, path=CAPABILITIES, headers=None, method='GET',
                     body=None, **kwargs):
        values = {'Authorization': 'Bearer ' + key['token']} if key else {}
        values.update(headers or {})
        return self.request(method, path, body, headers=values, **kwargs)

    def root_catalog(self, value=None):
        return self._patch(self.server.integration, 'file_root_catalog',
            return_value=copy.deepcopy(ROOT_CATALOG if value is None else value))

    def test_capabilities_require_bearer_not_cookie_or_developer_whitelist(self):
        self.server.api_access.configure({'enabled': True})
        for headers in ({}, {'Cookie': self.cookie},
                        {'Cookie': self.cookie, 'X-FNM-Request': '1'},
                        {'Authorization': 'Bearer r1_' + 'x' * 43}):
            with self.subTest(headers=tuple(headers)):
                response = self.capabilities(headers=headers)
                self.assertEqual(401, response[0])
                self.assertIn('Bearer', response[1].get('www-authenticate', ''))
                self.assertNotIn('domains', response[2])
        self.assert_no_actions()

    def test_capabilities_accept_a_valid_key_without_the_read_domain(self):
        key = self.key('admin', ['apps'])
        status, _, value = self.capabilities(key)
        self.assertEqual(200, status)
        self.assertEqual({'ok', 'role', 'policy_version', 'scopes', 'file_roots',
                          'tools', 'domains', 'tool_grants', 'docker_projects', 'docker_deployments'}, set(value))
        self.assertIs(value['ok'], True)
        self.assertEqual('admin', value['role'])
        self.assertEqual(2, value['policy_version'])
        self.assertEqual(['apps'], value['scopes'])
        self.assertEqual([], value['tools'])
        self.assertEqual([], value['file_roots'])
        self.assertEqual(['apps'], [row['id'] for row in value['domains']])
        self.assertIs(value['domains'][0]['implemented'], True)
        self.assertEqual(value['domains'][0]['status'], 'pending_enable')
        self.assertEqual([], value['domains'][0]['available_tools'])
        self.assertTrue(value['domains'][0]['reason'])
        denied = self.request('GET', '/api/integration/status',
            headers={'Authorization': 'Bearer ' + key['token']})
        self.assertEqual(403, denied[0])
        self.assert_no_actions()

    def test_capability_tools_match_mcp_for_all_three_roles(self):
        for role in ('readonly', 'readwrite', 'admin'):
            with self.subTest(role=role):
                key = self.key(role, fixture.SCOPES)
                status, _, capabilities = self.capabilities(key)
                self.assertEqual(200, status)
                status, _, rpc = self.rpc(key)
                self.assertEqual(200, status)
                self.assertEqual(rpc['result']['tools'], capabilities['tools'])
                actual = {tool['name'] for tool in capabilities['tools']}
                expected = ({'get_nas_status'} if role == 'readonly' else
                    set(MIN_ROLE_BY_TOOL) - {'request_power', 'cancel_power'}
                    if role == 'readwrite' else set(MIN_ROLE_BY_TOOL))
                self.assertEqual(expected, actual)
                for row in capabilities['domains']:
                    tool_names = {tool['name'] for tool in capabilities['tools']
                        if tool['_meta']['io.github.shinku-chen.r1-toolbox/requiredScope']
                        == row['id']}
                    self.assertEqual(tool_names, set(row['available_tools']))
        self.assert_no_actions()

    def test_readonly_never_reaches_controllers_or_native_media(self):
        key = self.key('readonly', fixture.SCOPES)
        calls = {
            'get_media_status': {'action': 'all'},
            'set_fan_policy': {'config': {}, 'confirmed': True},
            'release_fan': {'confirmed': True},
            'set_light_policy': {'config': {}},
            'set_rgb': {'mode': 1, 'color': '#123456'},
            'set_screen': {'action': 'wake'},
            'update_screen_settings': {'settings': {}},
            'request_power': {'operation': 'reboot', 'confirmed': True},
            'cancel_power': {},
        }
        for tool, arguments in calls.items():
            with self.subTest(tool=tool):
                status, _, value = self.rpc(key, self.message('tools/call',
                    {'name': tool, 'arguments': arguments}))
                self.assertEqual(200, status)
                self.assert_tool_error(value['result'], 'role_denied')
                status, _, value = self.rest_action(key, tool, arguments)
                self.assertEqual(200, status)
                self.assert_tool_error(value, 'role_denied')
        self.assert_no_actions()

    def test_readwrite_permits_explicit_screen_domain_but_not_power(self):
        key = self.key('readwrite', ['screen', 'power'])
        status, _, result = self.rest_action(key, 'set_screen', {'action': 'wake'})
        self.assertEqual(200, status)
        self.assertFalse(result['isError'])
        self.controller.action.assert_called_once_with('screen.wake', {})
        self.controller.action.reset_mock()
        for tool, args in (('request_power', {'operation': 'reboot', 'confirmed': True}),
                           ('cancel_power', {})):
            with self.subTest(tool=tool):
                status, _, result = self.rest_action(key, tool, args)
                self.assertEqual(200, status)
                self.assert_tool_error(result, 'role_denied')
        self.assert_no_actions()

    def test_admin_does_not_grant_unselected_domains(self):
        key = self.key('admin', ['read'])
        for tool, arguments in (('set_screen', {'action': 'wake'}),
                                ('get_media_status', {'action': 'face'}),
                                ('request_power', {'operation': 'poweroff', 'confirmed': True})):
            with self.subTest(tool=tool):
                status, _, result = self.rest_action(key, tool, arguments)
                self.assertEqual(200, status)
                self.assert_tool_error(result, 'scope_denied')
        self.assert_no_actions()

    def test_role_denials_are_audited_without_arguments_credentials_or_results(self):
        key = self.key('readonly', ['screen'])
        marker = 'PRIVATE_PARAMETER_MUST_NOT_BE_STORED'
        status, _, result = self.rest_action(key, 'set_screen', {'invalid': marker})
        self.assertEqual(200, status)
        self.assert_tool_error(result, 'role_denied')
        status, _, state = self.browser()
        self.assertEqual(200, status)
        denied = [row for row in state['audit']
                  if row['key_id'] == key['id'] and row['operation'] == 'set_screen']
        self.assertTrue(denied)
        self.assertEqual('denied', denied[0]['outcome'])
        public = json.dumps(state, ensure_ascii=False)
        self.assertNotIn(marker, public)
        self.assertNotIn(key['token'], public)
        for item in state['keys']:
            self.assertNotIn('digest', item)
        stored = json.loads(self.server.api_access.path.read_text(encoding='utf-8'))
        for item in stored['keys']:
            self.assertNotIn(item['digest'], public)
        self.assert_no_actions()

    def test_v1_persistent_keys_keep_their_original_scope_grants(self):
        # Reproduce an actual pre-upgrade state, not a request that asks the new
        # create API for an old role. The same key digest must survive migration.
        key = self.legacy_key(fixture.SCOPES)
        path = self.server.api_access.path
        state = json.loads(path.read_text(encoding='utf-8'))
        state['version'] = 1
        for row in state['keys']:
            for field in ('role', 'policy_version', 'file_roots', 'disabled',
                          'tool_grants', 'docker_projects', 'docker_deployments', 'grant_revision', 'credential_generation'):
                row.pop(field, None)
        digest = state['keys'][0]['digest']
        path.write_text(json.dumps(state), encoding='utf-8')
        os.chmod(path, 0o600)
        status, _, capabilities = self.capabilities(key)
        self.assertEqual(200, status)
        self.assertEqual('legacy', capabilities['role'])
        self.assertEqual(1, capabilities['policy_version'])
        self.assertEqual(set(MIN_ROLE_BY_TOOL),
                         {tool['name'] for tool in capabilities['tools']})
        status, _, result = self.rest_action(key, 'cancel_power', {})
        self.assertEqual(200, status)
        self.assertFalse(result['isError'])
        self.controller.action.assert_called_once_with('power.cancel', {})
        persisted = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(5, persisted['version'])
        self.assertEqual(digest, persisted['keys'][0]['digest'])
        self.assertEqual('legacy', persisted['keys'][0]['role'])
        self.assertEqual(1, persisted['keys'][0]['policy_version'])

    def test_capabilities_disabled_revoked_and_expired_keys_fail_immediately(self):
        disabled = self.key(enable=False)
        self.assertEqual(403, self.capabilities(disabled)[0])
        self.server.api_access.configure({'enabled': True})
        self.assertEqual(200, self.capabilities(disabled)[0])
        revoked = self.key()
        self.server.api_access.revoke_key(revoked['id'])
        self.assertEqual(401, self.capabilities(revoked)[0])
        self.now += 86400
        self.assertEqual(401, self.capabilities(disabled)[0])
        self.assert_no_actions()

    def test_capabilities_use_the_actual_peer_and_ignore_forwarding_headers(self):
        key = self.key(allowed_ips=['192.168.31.22'])
        forwarded = {'Forwarded': 'for=192.168.31.22',
                     'X-Forwarded-For': '192.168.31.22', 'X-Real-IP': '192.168.31.22'}
        self.assertEqual(403, self.capabilities(key, headers=forwarded)[0])
        allowed = self.key()
        self.assertEqual(200, self.capabilities(allowed, headers=forwarded)[0])
        self.assert_no_actions()

    def test_capabilities_keep_strict_host_origin_and_reject_url_parameters(self):
        key = self.key()
        self.assertEqual(200, self.capabilities(key)[0])
        self.assertEqual(200, self.capabilities(key,
            headers={'Origin': 'http://' + self.host})[0])
        for headers in ({'Origin': 'http://foreign.invalid'},
                        {'Origin': 'null'}, {'Host': 'foreign.invalid'},
                        {'Host': '127.0.0.1'},
                        {'Host': 'foreign.invalid', 'X-Forwarded-Host': self.host}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.capabilities(key, headers=headers)[0])
        for suffix in ('?section=storage', '?token=PRIVATE', '#fragment', '?x=1#fragment'):
            with self.subTest(suffix=suffix):
                status, _, value = self.capabilities(key, path=CAPABILITIES + suffix)
                self.assertEqual(400, status)
                self.assertNotIn('domains', value)
                self.assertNotIn('PRIVATE', json.dumps(value))
        self.assert_no_actions()

    def test_capabilities_are_get_only_and_never_enable_cors(self):
        key = self.key()
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                status, headers, value = self.capabilities(key, method=method,
                    body={} if method == 'POST' else None)
                self.assertEqual(405, status)
                self.assertEqual('GET', headers.get('allow'))
                self.assertNotIn('domains', value)
        self.assert_no_actions()

    def test_duplicate_machine_authorization_headers_cannot_read_capabilities(self):
        key = self.key()
        status, _, value = self.capabilities(key,
            duplicates=[('Authorization', 'Bearer ' + key['token'])])
        self.assertEqual(400, status)
        self.assertNotIn('domains', value)
        self.assert_no_actions()

    def test_capabilities_show_only_selected_roots_without_touching_user_paths(self):
        catalog = self.root_catalog()
        grant = {'id': ROOT_ID, 'path': '视频资源/电影', 'access': 'readonly'}
        key = self.key('readonly', ['files', 'docker'], file_roots=[grant])
        status, _, capabilities = self.capabilities(key)
        self.assertEqual(200, status)
        self.assertEqual([], capabilities['tools'])
        self.assertEqual({'files', 'docker'}, {row['id'] for row in capabilities['domains']})
        self.assertEqual(1, len(capabilities['file_roots']))
        root = capabilities['file_roots'][0]
        for name, expected in grant.items():
            self.assertEqual(expected, root[name])
        self.assertIs(root['available'], True)
        self.assertEqual('主磁盘', root['label'])
        self.assertNotIn('mount', root)
        serialized = json.dumps(capabilities, ensure_ascii=False)
        self.assertNotIn(OTHER_ROOT_ID, serialized)
        self.assertNotIn('其它私有空间', serialized)
        self.assertNotIn('/drives/private-unselected', serialized)
        for domain in capabilities['domains']:
            self.assertIs(domain['implemented'], True)
            self.assertEqual(domain['status'], 'pending_enable')
            self.assertEqual([], domain['available_tools'])
            self.assertTrue(domain['reason'])
        self.assertGreaterEqual(catalog.call_count, 1)
        self.assert_no_actions()

    def test_unmounted_file_root_stays_unknown_without_substitution(self):
        catalog = self.root_catalog()
        grant = {'id': ROOT_ID, 'path': '', 'access': 'readwrite'}
        key = self.key('admin', ['files'], file_roots=[grant])
        catalog.return_value = dict(ROOT_CATALOG,
            roots=[ROOT_CATALOG['roots'][1]])
        status, _, capabilities = self.capabilities(key)
        self.assertEqual(200, status)
        self.assertEqual(1, len(capabilities['file_roots']))
        unavailable = capabilities['file_roots'][0]
        self.assertEqual(ROOT_ID, unavailable['id'])
        self.assertIs(unavailable['available'], False)
        self.assertIsNone(unavailable.get('mount'))
        serialized = json.dumps(capabilities, ensure_ascii=False)
        self.assertNotIn(OTHER_ROOT_ID, serialized)
        self.assertNotIn('/drives/private-unselected', serialized)
        self.assert_no_actions()

    def test_invalid_root_grants_are_rejected_before_any_key_is_created(self):
        self.root_catalog()
        good = {'id': ROOT_ID, 'path': '文件', 'access': 'readonly'}
        cases = [
            ('readonly', ['files'], []),
            ('readonly', ['read'], [good]),
            ('readonly', ['files'], [dict(good, access='readwrite')]),
            ('admin', ['files'], [dict(good, id=MISSING_ROOT_ID)]),
            ('admin', ['files'], [dict(good, id=OTHER_ROOT_ID, access='readwrite')]),
            ('admin', ['files'], [dict(good, id='/drives/data')]),
            ('admin', ['files'], [dict(good, path='../private')]),
            ('admin', ['files'], [dict(good, path='/etc')]),
            ('admin', ['files'], [dict(good, path='safe/../private')]),
            ('admin', ['files'], [dict(good, path='safe//files')]),
            ('admin', ['files'], [dict(good, path='safe\\files')]),
            ('admin', ['files'], [dict(good, path='safe\nfiles')]),
            ('admin', ['files'], [dict(good, extra='PRIVATE_FIELD')]),
            ('admin', ['files'], [good, good]),
        ]
        for role, scopes, roots in cases:
            with self.subTest(role=role, roots=roots):
                status, _, value = self.browser('POST', '/api/access/keys',
                    self.create_body(role, scopes, file_roots=roots))
                self.assertEqual(400, status)
                self.assertNotIn('token', value)
                self.assertNotIn('PRIVATE_FIELD', json.dumps(value))
                self.assertEqual([], self.server.api_access.snapshot()['keys'])
        self.assert_no_actions()

    def test_capabilities_do_not_reveal_file_catalog_to_unselected_keys(self):
        catalog = self.root_catalog()
        key = self.key('readonly', ['read'])
        catalog.reset_mock()
        status, _, capabilities = self.capabilities(key)
        self.assertEqual(200, status)
        self.assertEqual([], capabilities['file_roots'])
        self.assertEqual(['read'], [row['id'] for row in capabilities['domains']])
        serialized = json.dumps(capabilities, ensure_ascii=False)
        for value in (ROOT_ID, OTHER_ROOT_ID, '/drives/data', '主磁盘'):
            self.assertNotIn(value, serialized)
        self.assert_no_actions()


if __name__ == '__main__':
    unittest.main()
