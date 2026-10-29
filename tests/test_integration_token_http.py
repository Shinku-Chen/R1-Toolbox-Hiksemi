"""Token lifecycle and LAN CIDR rules through a real loopback HTTP server.

Reuse only helper methods from the established fixture, so its old test methods
are not collected twice. All secrets and state are temporary fixtures; no NAS,
SSH, native token, real disk or Docker operation is involved.
"""

import copy
import hashlib
import json
import os
import unittest

import test_integration_http as fixture


UPDATE = '/api/access/keys/update'
ENABLE = '/api/access/keys/enable'
ROTATE = '/api/access/keys/rotate'
CAPABILITIES = '/api/integration/capabilities'
ROOT_ID = 'root_' + 'a' * 24
OTHER_ROOT_ID = 'root_' + 'b' * 24
ROOT_CATALOG = {
    'available': True, 'status': 'authorization_plan', 'boundary_version': 2,
    'reason': '仅用于授权范围选择，当前未提供文件操作工具。',
    'roots': [{'id': ROOT_ID, 'label': '主磁盘', 'mount': '/drives/data/Users', 'read_only': False}],
}


class IntegrationTokenHTTPTests(unittest.TestCase):
    setUp = fixture.IntegrationHTTPTests.setUp
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    rpc = fixture.IntegrationHTTPTests.rpc
    rest_action = fixture.IntegrationHTTPTests.rest_action
    browser = fixture.IntegrationHTTPTests.browser
    message = staticmethod(fixture.IntegrationHTTPTests.message)
    assert_no_actions = fixture.IntegrationHTTPTests.assert_no_actions
    assert_tool_error = fixture.IntegrationHTTPTests.assert_tool_error

    def key(self, *, legacy=False, **updates):
        self.assertEqual(self.browser('POST', '/api/access/settings', {'enabled': True})[0], 200)
        body = {'name': 'Token HTTP fixture', 'scopes': ['read'],
                'allowed_ips': ['127.0.0.1'], 'expires_days': 1,
                **({} if legacy else {'role': 'readonly', 'file_roots': []}), **updates}
        status, _, created = self.browser('POST', '/api/access/keys', body)
        self.assertEqual(status, 200, created)
        self.assertTrue(created['ok'])
        return {**created['key'], 'token': created['token']}

    def capability(self, key, headers=None):
        return self.request('GET', CAPABILITIES,
            headers={'Authorization': 'Bearer ' + key['token'], **(headers or {})})

    def key_row(self, key_id):
        status, _, state = self.browser()
        self.assertEqual(status, 200)
        return next(row for row in state['keys'] if row['id'] == key_id)

    def update(self, key, **fields):
        status, _, value = self.browser('POST', UPDATE, {'id': key['id'], **fields})
        self.assertEqual(status, 200, value)
        self.assertTrue(value['ok'])
        return value

    def test_owner_can_edit_name_cidr_and_expiry_without_changing_secret_or_grants(self):
        key = self.key(scopes=['read', 'fan'])
        before = json.loads(self.server.api_access.path.read_text(encoding='utf-8'))['keys'][0]
        self.now += 100
        self.update(key, name='改名 fixture', allowed_ips=['127.2.3.4/255.0.0.0'], expires_days=7)
        row = self.key_row(key['id'])
        self.assertEqual(row['name'], '改名 fixture')
        self.assertEqual(row['allowed_ips'], ['127.0.0.0/8'])
        self.assertEqual(row['scopes'], ['read', 'fan'])
        self.assertEqual(row['role'], 'readonly')
        self.assertEqual(row['expires_at'], self.now + 7 * 86400)
        self.assertEqual(row['created_at'], key['created_at'])
        after = json.loads(self.server.api_access.path.read_text(encoding='utf-8'))['keys'][0]
        self.assertEqual(after['digest'], before['digest'])
        self.assertEqual(self.capability(key)[0], 200)
        status, _, tools = self.rpc(key)
        self.assertEqual(status, 200)
        self.assertEqual([row['name'] for row in tools['result']['tools']], ['get_nas_status'])
        self.assert_no_actions()

    def test_cidr_uses_actual_socket_peer_and_ignores_forwarded_source_headers(self):
        key = self.key(allowed_ips=['127.2.3.4/8'])
        self.assertEqual(self.key_row(key['id'])['allowed_ips'], ['127.0.0.0/8'])
        self.assertEqual(self.capability(key)[0], 200)
        self.assertEqual(self.rpc(key)[0], 200)
        self.update(key, allowed_ips=['127.0.1.77/24'])
        headers = {'Forwarded': 'for=127.0.1.77', 'X-Forwarded-For': '127.0.1.77',
                   'X-Real-IP': '127.0.1.77'}
        status, _, value = self.capability(key, headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(value['error']['code'], 'peer_not_allowed')
        self.assertEqual(self.rpc(key, headers=headers)[0], 403)
        self.update(key, allowed_ips=['::ffff:127.0.0.1/128'])
        self.assertEqual(self.key_row(key['id'])['allowed_ips'], ['127.0.0.1'])
        self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_empty_key_allowlist_allows_lan_but_unsafe_cidrs_and_equivalent_duplicates_reject(self):
        key = self.key(allowed_ips=[])
        self.assertEqual(self.capability(key)[0], 200)
        for rules in (['0.0.0.0/0'], ['::/0'], ['192.168.0.0/15'], ['*'],
                      ['localhost'], ['127.0.0.1', '127.0.0.1/32']):
            with self.subTest(rules=rules):
                status, _, value = self.browser('POST', UPDATE,
                    {'id': key['id'], 'allowed_ips': rules})
                self.assertEqual(status, 400)
                self.assertNotIn('token', value)
                self.assertEqual(self.key_row(key['id'])['allowed_ips'], [])
                self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_partial_legacy_edits_keep_policy_and_only_explicit_role_changes_it(self):
        key = self.key(legacy=True, scopes=['read', 'fan'])
        status, _, before = self.rpc(key)
        self.assertEqual(status, 200)
        expected = {'get_nas_status', 'set_fan_policy', 'release_fan'}
        self.assertEqual({row['name'] for row in before['result']['tools']}, expected)
        self.update(key, name='旧权限改名', allowed_ips=['127.0.0.0/8'])
        row = self.key_row(key['id'])
        self.assertEqual(row['policy_version'], 1)
        self.assertEqual(row['role'], 'legacy')
        status, _, after = self.rpc(key)
        self.assertEqual({row['name'] for row in after['result']['tools']}, expected)
        status, _, _ = self.browser('POST', UPDATE, {'id': key['id'], 'scopes': ['read', 'docker']})
        self.assertEqual(status, 400)
        self.assertEqual(self.key_row(key['id'])['scopes'], ['read', 'fan'])
        self.update(key, role='admin')
        row = self.key_row(key['id'])
        self.assertEqual(row['policy_version'], 2)
        self.assertEqual(row['role'], 'admin')
        self.assertEqual(row['scopes'], ['read', 'fan'])
        self.assertEqual({item['name'] for item in self.rpc(key)[2]['result']['tools']}, expected)
        self.assert_no_actions()

    def test_pause_restore_and_global_switch_take_effect_for_mcp_and_rest(self):
        key = self.key()
        status, _, value = self.browser('POST', ENABLE, {'id': key['id'], 'enabled': False})
        self.assertEqual(status, 200, value)
        row = self.key_row(key['id'])
        self.assertEqual(row['status'], 'disabled')
        self.assertTrue(row['disabled'])
        self.assertFalse(row['enabled'])
        self.assertEqual(row['expires_at'], key['expires_at'])
        for response in (self.rpc(key), self.capability(key)):
            self.assertEqual(response[0], 403)
            self.assertEqual(response[2]['error']['code'], 'key_disabled')
        self.assertEqual(self.browser('POST', ENABLE, {'id': key['id'], 'enabled': True})[0], 200)
        self.assertEqual(self.key_row(key['id'])['status'], 'active')
        self.assertEqual(self.capability(key)[0], 200)
        self.assertEqual(self.rpc(key)[0], 200)
        self.assertEqual(self.browser('POST', '/api/access/settings', {'enabled': False})[0], 200)
        self.assertEqual(self.capability(key)[0], 403)
        self.assertEqual(self.rpc(key)[2]['error']['code'], 'disabled')
        self.assertTrue(self.key_row(key['id'])['enabled'])
        self.assertEqual(self.browser('POST', '/api/access/settings', {'enabled': True})[0], 200)
        self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_rotation_exposes_new_secret_once_old_token_stops_and_metadata_remains(self):
        key = self.key(role='readwrite', scopes=['read', 'fan'])
        self.assertEqual(self.capability(key)[0], 200)
        status, _, rotated = self.browser('POST', ROTATE, {'id': key['id']})
        self.assertEqual(status, 200, rotated)
        self.assertTrue(rotated['ok'])
        self.assertEqual(set(rotated), {'ok', 'key', 'token'})
        self.assertNotEqual(rotated['token'], key['token'])
        self.assertEqual(rotated['key']['id'], key['id'])
        self.assertEqual(rotated['key']['role'], key['role'])
        self.assertEqual(rotated['key']['scopes'], key['scopes'])
        self.assertEqual(rotated['key']['created_at'], key['created_at'])
        self.assertEqual(rotated['key']['expires_at'], key['expires_at'])
        self.assertIsNone(rotated['key']['last_used_at'])
        self.assertIsNone(self.key_row(key['id'])['last_used_at'])
        self.assertNotIn('token', rotated['key'])
        self.assertNotIn('digest', rotated['key'])
        self.assertEqual(self.rpc(key)[0], 401)
        self.assertEqual(self.capability(key)[0], 401)
        replacement = {**rotated['key'], 'token': rotated['token']}
        self.assertEqual(self.capability(replacement)[0], 200)
        self.assertEqual(self.rpc(replacement)[0], 200)
        status, _, state = self.browser()
        self.assertEqual(status, 200)
        serialized = json.dumps(state)
        persisted = self.server.api_access.path.read_text(encoding='utf-8')
        for token in (key['token'], rotated['token']):
            self.assertNotIn(token, serialized)
            self.assertNotIn(token, persisted)
            self.assertNotIn(hashlib.sha256(token.encode('ascii')).hexdigest(), serialized)
        self.assertNotIn('digest', serialized)
        self.assert_no_actions()

    def test_rotating_a_paused_key_keeps_it_paused_and_revocation_cannot_be_reversed(self):
        key = self.key()
        self.assertEqual(self.browser('POST', ENABLE, {'id': key['id'], 'enabled': False})[0], 200)
        status, _, value = self.browser('POST', ROTATE, {'id': key['id']})
        self.assertEqual(status, 200, value)
        self.assertTrue(value['key']['disabled'])
        replacement = {**value['key'], 'token': value['token']}
        self.assertEqual(self.capability(key)[0], 401)
        self.assertEqual(self.capability(replacement)[0], 403)
        self.assertEqual(self.capability(replacement)[2]['error']['code'], 'key_disabled')
        self.assertEqual(self.browser('POST', '/api/access/keys/revoke', {'id': key['id']})[0], 200)
        for path, body in ((ENABLE, {'id': key['id'], 'enabled': True}),
                           (ROTATE, {'id': key['id']})):
            with self.subTest(path=path):
                status, _, error = self.browser('POST', path, body)
                self.assertEqual(status, 400)
                self.assertNotIn(value['token'], json.dumps(error))
        self.assertEqual(self.key_row(key['id'])['status'], 'revoked')
        self.assertEqual(self.capability(replacement)[0], 401)
        self.assert_no_actions()

    def test_new_management_routes_keep_owner_origin_csrf_json_and_url_guards(self):
        key = self.key()
        requests = ((UPDATE, {'id': key['id'], 'name': 'unsafe change'}),
                    (ENABLE, {'id': key['id'], 'enabled': False}),
                    (ROTATE, {'id': key['id']}))
        cases = ({'cookie': False}, {'headers': {'Origin': 'http://foreign.invalid'}},
                 {'remove': ('Origin',)}, {'remove': ('X-FNM-Request',)},
                 {'headers': {'Content-Type': 'text/plain'}}, {'remove': ('Content-Length',)})
        before = self.key_row(key['id'])
        for path, body in requests:
            for kwargs in cases:
                with self.subTest(path=path, guard=tuple(kwargs)):
                    self.assertIn(self.browser('POST', path, body, **kwargs)[0], (400, 401, 403))
            for suffix in ('?extra=1', '#fragment'):
                with self.subTest(path=path, suffix=suffix):
                    self.assertEqual(self.browser('POST', path + suffix, body)[0], 400)
        self.assertEqual(self.key_row(key['id']), before)
        self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_even_an_admin_api_key_cannot_edit_enable_rotate_or_issue_api_keys(self):
        key = self.key(role='admin', scopes=list(fixture.SCOPES))
        requests = ((UPDATE, {'id': key['id'], 'name': 'unsafe change'}),
                    (ENABLE, {'id': key['id'], 'enabled': False}),
                    (ROTATE, {'id': key['id']}),
                    ('/api/access/keys', {'name': 'unauthorized', 'role': 'admin', 'scopes': ['read']}))
        for path, body in requests:
            for cookie in (False, True):
                with self.subTest(path=path, cookie=cookie):
                    response = self.browser('POST', path, body, cookie=cookie,
                        headers={'Authorization': 'Bearer ' + key['token']})
                    self.assertEqual(response[0], 403)
        self.assertEqual(len(self.server.api_access.snapshot()['keys']), 1)
        self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_invalid_management_bodies_never_echo_token_digest_or_change_the_key(self):
        key = self.key()
        secret = key['token']
        digest = hashlib.sha256(secret.encode('ascii')).hexdigest()
        before = self.key_row(key['id'])
        requests = ((UPDATE, {'id': key['id'], 'token': secret}),
                    (UPDATE, {'id': key['id'], 'digest': digest}),
                    (UPDATE, {'id': key['id'], 'disabled': True}),
                    (ENABLE, {'id': key['id'], 'enabled': False, 'token': secret}),
                    (ENABLE, {'id': key['id'], 'enabled': 'yes'}),
                    (ROTATE, {'id': key['id'], 'token': secret}),
                    (ROTATE, {'id': secret}))
        for path, body in requests:
            with self.subTest(path=path, fields=tuple(body)):
                status, _, error = self.browser('POST', path, body)
                self.assertEqual(status, 400)
                self.assertNotIn(secret, json.dumps(error))
                self.assertNotIn(digest, json.dumps(error))
        self.assertEqual(self.key_row(key['id']), before)
        self.assertEqual(self.capability(key)[0], 200)
        self.assert_no_actions()

    def test_token_mutation_audit_contains_only_safe_operation_metadata(self):
        key = self.key()
        self.update(key, name='改名 audit fixture')
        self.assertEqual(self.browser('POST', ENABLE, {'id': key['id'], 'enabled': False})[0], 200)
        self.assertEqual(self.browser('POST', ENABLE, {'id': key['id'], 'enabled': True})[0], 200)
        status, _, rotated = self.browser('POST', ROTATE, {'id': key['id']})
        self.assertEqual(status, 200)
        status, _, state = self.browser()
        self.assertEqual(status, 200)
        operations = {row['operation'] for row in state['audit']}
        self.assertTrue({'key.update', 'key.enable', 'key.rotate'}.issubset(operations))
        for row in state['audit']:
            self.assertLessEqual(set(row), {'timestamp', 'key_id', 'key_name', 'operation',
                                           'peer', 'outcome', 'duration_ms'})
            if row['operation'] in ('key.update', 'key.enable', 'key.rotate'):
                self.assertEqual(row['key_id'], key['id'])
        self.assertIsNone(self.key_row(key['id'])['last_used_at'])
        serialized = json.dumps(state['audit'])
        for private in (key['token'], rotated['token'], 'Authorization', 'arguments', 'digest'):
            self.assertNotIn(private, serialized)
        self.assert_no_actions()

    def test_v2_http_snapshot_reads_do_not_rewrite_and_first_owner_edit_migrates_to_v4(self):
        key = self.key(role='readwrite', scopes=['read', 'fan'])
        path = self.server.api_access.path
        old = json.loads(path.read_text(encoding='utf-8'))
        old['version'] = 2
        for row in old['keys']:
            for field in ('disabled', 'tool_grants', 'docker_projects', 'grant_revision', 'credential_generation'):
                row.pop(field)
        path.write_text(json.dumps(old, separators=(',', ':')), encoding='utf-8')
        os.chmod(path, 0o600)
        before = path.read_bytes()
        row = self.key_row(key['id'])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(row['disabled'])
        self.assertTrue(row['enabled'])
        self.update(key, name='v2 迁移 fixture')
        migrated = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(migrated['version'], 4)
        actual = migrated['keys'][0]
        expected = {**old['keys'][0], 'name': 'v2 迁移 fixture', 'disabled': False,
                    'tool_grants': [], 'docker_projects': [], 'grant_revision': 1, 'credential_generation': 1}
        self.assertEqual(actual, expected)
        self.assertEqual(self.capability(key)[0], 200)
        self.assertEqual(self.key_row(key['id'])['role'], 'readwrite')
        self.assert_no_actions()

    def test_explicit_file_range_updates_use_current_cold_catalog_without_blocking_metadata_edits(self):
        catalog = self._patch(self.server.integration, 'file_root_catalog', return_value=copy.deepcopy(ROOT_CATALOG))
        roots = [{'id': ROOT_ID, 'path': '视频资源', 'access': 'readonly'}]
        key = self.key(scopes=['read', 'files'], file_roots=roots)
        self.assertEqual(self.key_row(key['id'])['file_roots'], roots)
        status, _, _ = self.browser('POST', UPDATE,
            {'id': key['id'], 'file_roots': [{'id': OTHER_ROOT_ID, 'path': '', 'access': 'readonly'}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.key_row(key['id'])['file_roots'], roots)
        catalog.return_value = {'available': False, 'status': 'unavailable', 'roots': [], 'reason': 'fixture offline'}
        self.update(key, name='只修改名称')
        self.assertEqual(self.key_row(key['id'])['file_roots'], roots)
        self.assert_no_actions()


if __name__ == '__main__':
    unittest.main()
