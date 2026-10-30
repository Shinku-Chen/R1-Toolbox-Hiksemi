"""Real HTTP resource grants with isolated engine GETs and fake credentials.

Alias fixture helpers instead of subclassing its TestCase, so older integration
tests are not collected twice. This module is deliberately named integration:
the shared web fixture must be imported after panel reload tests in discovery.
"""

import concurrent.futures
import copy
import json
import threading
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

import test_integration_http as fixture
from test_api_docker_projects import EngineFixture, SECRET
from api_contracts import contract_catalog
from api_docker_projects import DockerProjectRegistry


DOCKER_TOOLS = ['list_docker_projects', 'get_docker_project_status', 'get_docker_project_summary']
ROOT = 'root_' + '1' * 24
OTHER_ROOT = 'root_' + '2' * 24
CATALOG = {'available': True, 'status': 'authorization_plan', 'boundary_version': 3,
           'reason': 'fixture metadata only', 'roots': [
    {'id': ROOT, 'label': '测试主磁盘', 'mount': '/drives/owner-only-fixture/Users/admin', 'read_only': False},
    {'id': OTHER_ROOT, 'label': '未选择的私有磁盘', 'mount': '/drives/unselected-fixture/Users/admin', 'read_only': True},
]}


class IntegrationResourcesHTTPTests(unittest.TestCase):
    _patch = fixture.IntegrationHTTPTests._patch
    request = fixture.IntegrationHTTPTests.request
    rpc = fixture.IntegrationHTTPTests.rpc
    rest_action = fixture.IntegrationHTTPTests.rest_action
    browser = fixture.IntegrationHTTPTests.browser
    message = staticmethod(fixture.IntegrationHTTPTests.message)
    assert_no_actions = fixture.IntegrationHTTPTests.assert_no_actions
    assert_tool_error = fixture.IntegrationHTTPTests.assert_tool_error

    def setUp(self):
        fixture.IntegrationHTTPTests.setUp(self)
        self.engine = EngineFixture()
        self.registry = DockerProjectRegistry(self.server.state_dir, backend=self.engine.reader)
        self.server.integration.docker_projects = self.registry
        self.roots = self._patch(self.server.integration, 'file_root_catalog', return_value=copy.deepcopy(CATALOG))
        self.schemas = {row['name']: row['outputSchema'] for row in contract_catalog()['tools']}
        self.addCleanup(self.assert_no_actions)

    def register(self, label='测试项目'):
        status, _, catalog = self.browser('GET', '/api/docker/projects/catalog')
        self.assertEqual(status, 200)
        self.assertTrue(catalog['available'])
        status, _, result = self.browser('POST', '/api/docker/projects/register',
            {'candidate_id': catalog['candidates'][0]['id'], 'label': label})
        self.assertEqual(status, 200, result)
        return result['project']['id']

    def key_body(self, project=None, **changes):
        value = {'name': 'resource fixture', 'role': 'readonly', 'scopes': ['docker'],
                 'tool_grants': list(DOCKER_TOOLS), 'file_roots': [],
                 'docker_projects': [{'id': project, 'operations': ['inspect']}] if project else [],
                 'allowed_ips': ['127.0.0.1'], 'expires_days': 1}
        value.update(changes)
        return value

    def key(self, project=None, **changes):
        self.server.api_access.configure({'enabled': True})
        status, _, created = self.browser('POST', '/api/access/keys', self.key_body(project, **changes))
        self.assertEqual(status, 200, created)
        self.assertTrue(created['ok'])
        return {**created['key'], 'token': created['token']}

    def call(self, key, name, arguments=None, *, transport='mcp'):
        arguments = {} if arguments is None else arguments
        response = (self.rpc(key, self.message('tools/call', {'name': name, 'arguments': arguments}))
                    if transport == 'mcp' else self.rest_action(key, name, arguments))
        self.assertEqual(response[0], 200, response[2])
        return response[2]['result'] if transport == 'mcp' else response[2]

    def valid(self, name, result):
        output = result['structuredContent']
        Draft202012Validator(self.schemas[name], format_checker=FormatChecker()).validate(output)
        self.assertFalse(result['isError'], output)
        self.assertNotIn(SECRET, json.dumps(output))
        self.assertNotIn('/drives/', json.dumps(output))
        return output

    def denied(self, name, result, code=None):
        self.assert_tool_error(result, code)
        Draft202012Validator(self.schemas[name], format_checker=FormatChecker()).validate(result['structuredContent'])

    def test_owner_registry_works_without_enabled_machine_service_or_token(self):
        self.assertFalse(self.server.api_access.snapshot()['enabled'])
        project = self.register()
        status, _, state = self.browser('GET', '/api/docker/projects')
        self.assertEqual(status, 200)
        self.assertTrue(state['projects'][0]['available'])
        status, _, edited = self.browser('POST', '/api/docker/projects/update',
            {'project_id': project, 'label': '修改后的名称'})
        self.assertEqual(status, 200)
        self.assertEqual(edited['project']['label'], '修改后的名称')
        status, _, revoked = self.browser('POST', '/api/docker/projects/revoke', {'project_id': project})
        self.assertEqual(status, 200)
        self.assertTrue(revoked['project']['revoked'])
        self.assertFalse(self.server.api_access.snapshot()['enabled'])
        self.assertTrue(all(request.startswith(b'GET ') for request in self.engine.requests))
        self.assertNotIn(b'project_list', b''.join(self.engine.requests))

    def test_bearer_or_developer_window_cannot_manage_owner_registry(self):
        project = self.register()
        key = self.key(project)
        before = len(self.engine.requests)
        cases = [('/api/docker/projects/catalog', None), ('/api/docker/projects', None),
                 ('/api/docker/projects/register', {'candidate_id': 'candidate_' + '1' * 24}),
                 ('/api/docker/projects/update', {'project_id': project, 'label': '不允许'}),
                 ('/api/docker/projects/revoke', {'project_id': project})]
        for path, body in cases:
            for bearer in (False, True):
                with self.subTest(path=path, bearer=bearer):
                    headers = {'Authorization': 'Bearer ' + key['token']} if bearer else {}
                    status, _, result = self.browser('GET' if body is None else 'POST', path, body,
                                                     cookie=False, headers=headers)
                    self.assertIn(status, (401, 403), result)
        # Even a valid owner cookie cannot mix in a machine Authorization header.
        for path in ('/api/docker/projects', '/api/docker/projects/catalog'):
            self.assertEqual(self.browser('GET', path, headers={'Authorization': 'Bearer ' + key['token']})[0], 403)
        self.assertEqual(len(self.engine.requests), before)
        self.assertFalse(self.registry.metadata()['projects'][0]['revoked'])

    def test_registry_metadata_management_does_not_take_hardware_lock(self):
        candidate = self.browser('GET', '/api/docker/projects/catalog')[2]['candidates'][0]['id']
        self.server.operation_lock.acquire()
        try:
            status, _, result = self.browser('POST', '/api/docker/projects/register', {'candidate_id': candidate})
            self.assertEqual(status, 200, result)
            project = result['project']['id']
            self.assertEqual(self.browser('POST', '/api/docker/projects/update', {'project_id': project, 'label': '独立登记'})[0], 200)
            self.assertEqual(self.browser('POST', '/api/docker/projects/revoke', {'project_id': project})[0], 200)
        finally:
            self.server.operation_lock.release()

    def test_policy3_project_reads_work_over_mcp_and_rest_with_exact_schemas(self):
        project = self.register()
        key = self.key(project)
        self.assertEqual(key['policy_version'], 3)
        self.assertEqual(key['docker_projects'], [{'id': project, 'operations': ['inspect']}])
        status, _, listing = self.rpc(key)
        self.assertEqual(status, 200)
        self.assertEqual({row['name'] for row in listing['result']['tools']}, set(DOCKER_TOOLS))
        for transport in ('mcp', 'rest'):
            with self.subTest(transport=transport):
                result = self.valid('list_docker_projects', self.call(key, 'list_docker_projects', transport=transport))
                self.assertEqual(result['projects'][0]['project_id'], project)
                result = self.valid('get_docker_project_status', self.call(key, 'get_docker_project_status', {'project_id': project}, transport=transport))
                self.assertTrue(result['project']['running'])
                self.assertIsNone(result['project']['native_status'])
                self.assertIsNone(result['project']['pop_status'])
                self.assertEqual(result['project']['source'], 'engine')
                result = self.valid('get_docker_project_summary', self.call(key, 'get_docker_project_summary', {'project_id': project}, transport=transport))
                self.assertEqual(result['services'][0]['name'], 'web')
                self.assertIn('unresolved_configuration', result['services'][0]['risk_flags'])
        self.assertTrue(all(request.startswith(b'GET ') for request in self.engine.requests))

    def test_file_root_read_is_selected_grouped_metadata_without_host_paths(self):
        grants = [{'id': ROOT, 'path': '视频', 'access': 'readonly'},
                  {'id': ROOT, 'path': '备份', 'access': 'readonly'}]
        key = self.key(scopes=['files'], tool_grants=['list_file_roots'], file_roots=grants)
        before = len(self.engine.requests)
        for transport in ('mcp', 'rest'):
            result = self.valid('list_file_roots', self.call(key, 'list_file_roots', transport=transport))
            self.assertFalse(result['may_wake_disks'])
            self.assertEqual(len(result['roots']), 1)
            root = result['roots'][0]
            self.assertEqual(root['root_id'], ROOT)
            self.assertEqual(root['grants'], [{'path': '视频', 'access': 'readonly'}, {'path': '备份', 'access': 'readonly'}])
            self.assertEqual(root['verification'], 'metadata_only')
            self.assertEqual(root['backend']['status'], 'backend_unverified')
            self.assertNotIn(OTHER_ROOT, json.dumps(result))
            self.assertNotIn('未选择', json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(self.engine.requests), before)

    def test_policy2_preselected_files_and_docker_gain_none_of_the_four_tools(self):
        self.server.api_access.configure({'enabled': True})
        body = self.key_body(scopes=['read', 'files', 'docker'], file_roots=[{'id': ROOT, 'path': '', 'access': 'readonly'}])
        body.pop('tool_grants')
        status, _, created = self.browser('POST', '/api/access/keys', body)
        self.assertEqual(status, 200)
        key = {**created['key'], 'token': created['token']}
        self.assertEqual(key['policy_version'], 2)
        before = len(self.engine.requests)
        self.assertEqual({row['name'] for row in self.rpc(key)[2]['result']['tools']}, {'get_nas_status'})
        for name in ['list_file_roots'] + DOCKER_TOOLS:
            for transport in ('mcp', 'rest'):
                arguments = {'project_id': 'project_' + 'f' * 24} if name.startswith('get_docker') else {}
                self.denied(name, self.call(key, name, arguments, transport=transport), 'scope_denied')
        self.assertEqual(len(self.engine.requests), before)

    def test_whole_disk_token_stays_editable_until_explicit_admin_regrant(self):
        # Model a persisted key from the old planner through the storage layer;
        # owner creation in this version must reject the same stale root ID.
        old_root = 'root_' + 'f' * 24
        old_grants = [{'id': old_root, 'path': '', 'access': 'readonly'}]
        self.server.api_access.configure({'enabled': True})
        key = self.server.api_access.create_key(self.key_body(
            scopes=['files'], tool_grants=['list_file_roots'], file_roots=old_grants))
        before = len(self.engine.requests)
        status, _, updated = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'name': '旧整盘绑定，只改名称', 'allowed_ips': ['127.0.0.1']})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['key']['file_roots'], old_grants)
        self.assertEqual(updated['key']['tool_grants'], ['list_file_roots'])
        status, _, capabilities = self.request('GET', '/api/integration/capabilities',
            headers={'Authorization': 'Bearer ' + key['token']})
        self.assertEqual(status, 200)
        self.assertFalse(capabilities['file_roots'][0]['available'])
        self.assertEqual(capabilities['file_roots'][0]['id'], old_root)
        self.assertIsNone(capabilities['file_roots'][0]['label'])
        self.assertNotIn('/drives/', json.dumps(capabilities))
        for transport in ('mcp', 'rest'):
            root = self.valid('list_file_roots', self.call(key, 'list_file_roots',
                                                       transport=transport))['roots'][0]
            self.assertEqual(root['root_id'], old_root)
            self.assertEqual(root['binding_status'], 'unavailable')
            self.assertEqual(root['backend']['status'], 'resource_unavailable')
            self.assertIsNone(root['label'])
            arguments = {'root_id': old_root, 'path': ''}
            # Registering file reads must not grant them to an existing key.
            result = self.call(key, 'list_directory', arguments, transport=transport)
            self.denied('list_directory', result, 'scope_denied')
        status, _, _ = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'file_roots': old_grants})
        self.assertEqual(status, 400)
        new_grants = [{'id': ROOT, 'path': '', 'access': 'readonly'}]
        status, _, updated = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'file_roots': new_grants})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['key']['file_roots'], new_grants)
        current = self.valid('list_file_roots', self.call(key, 'list_file_roots'))['roots'][0]
        self.assertEqual(current['root_id'], ROOT)
        self.assertEqual(current['binding_status'], 'present')
        self.assertEqual(current['verification'], 'metadata_only')
        self.assertEqual(current['backend']['status'], 'backend_unverified')
        self.assertEqual(len(self.engine.requests), before)

    def test_users_v2_range_keeps_admin_prefix_until_explicit_admin_regrant(self):
        # A v2 relative path cannot become an admin-relative path by silently
        # dropping its first component when only metadata is being edited.
        old_root = 'root_' + 'e' * 24
        old_grants = [{'id': old_root, 'path': 'admin/reports', 'access': 'readonly'}]
        self.server.api_access.configure({'enabled': True})
        key = self.server.api_access.create_key(self.key_body(
            scopes=['files'], tool_grants=['list_file_roots'], file_roots=old_grants))
        status, _, edited = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'name': '保留旧 Users 相对路径'})
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited['key']['file_roots'], old_grants)
        data = self.valid('list_file_roots', self.call(key, 'list_file_roots'))
        self.assertEqual(data['roots'][0]['binding_status'], 'unavailable')
        status, _, rejected = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'file_roots': old_grants})
        self.assertEqual(status, 400, rejected)
        new_grants = [{'id': ROOT, 'path': 'reports', 'access': 'readonly'}]
        status, _, changed = self.browser('POST', '/api/access/keys/update',
            {'id': key['id'], 'file_roots': new_grants})
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed['key']['file_roots'], new_grants)

    def test_owner_cannot_create_file_grant_from_old_users_v2_catalog(self):
        self.roots.return_value = dict(CATALOG, boundary_version=2,
            roots=[dict(CATALOG['roots'][0], mount='/drives/owner-only-fixture/Users')])
        self.server.api_access.configure({'enabled': True})
        status, _, result = self.browser('POST', '/api/access/keys', self.key_body(
            scopes=['files'], tool_grants=['list_file_roots'],
            file_roots=[{'id': ROOT, 'path': '', 'access': 'readonly'}]))
        self.assertEqual(status, 400, result)

    def test_missing_domain_tool_or_cross_project_never_reads_backend(self):
        project = self.register()
        allowed = self.key(project)
        no_tool = self.key(project, tool_grants=[])
        no_domain = self.key(scopes=['read'], tool_grants=['get_nas_status'])
        # A new live binding receives its own ID; its grant never authorizes the old ID.
        self.engine.config_stamp[3] += 1
        other = self.register('另一个已登记绑定')
        cross = self.key(other, role='admin')
        before = len(self.engine.requests)
        for key, identity in ((no_tool, project), (no_domain, project), (cross, project),
                              (allowed, 'project_' + 'f' * 24)):
            for transport in ('mcp', 'rest'):
                result = self.call(key, 'get_docker_project_status', {'project_id': identity}, transport=transport)
                self.assertTrue(result['isError'])
                self.assertIn(result['structuredContent']['error']['code'], ('scope_denied', 'permission_denied'))
                self.denied('get_docker_project_status', result)
        self.assertEqual(len(self.engine.requests), before)

    def test_owner_rejects_missing_role_pending_tools_and_unsupported_resource_grants(self):
        project = self.register()
        before = len(self.engine.requests)
        no_role = self.key_body(project)
        no_role.pop('role')
        cases = [no_role,
                 self.key_body(project, tool_grants=['plan_docker_project_update'], role='admin'),
                 self.key_body(scopes=['files'], role='readwrite', tool_grants=['unregistered_file_tool'],
                               file_roots=[{'id': ROOT, 'path': '', 'access': 'readwrite'}]),
                 self.key_body('project_' + 'f' * 24),
                 self.key_body(project, role='admin', docker_projects=[{'id': project, 'operations': ['inspect', 'arbitrary_exec']}])]
        for body in cases:
            with self.subTest(tools=body['tool_grants'], role=body.get('role')):
                status, _, result = self.browser('POST', '/api/access/keys', body)
                self.assertEqual(status, 400, result)
                self.assertFalse(result['ok'])
        self.assertEqual(len(self.engine.requests), before)
        key = self.key(project)
        updates = [{'tool_grants': ['plan_docker_project_update'], 'role': 'admin'},
                   {'docker_projects': [{'id': 'project_' + 'e' * 24, 'operations': ['inspect']}]},
                   {'docker_projects': [{'id': project, 'operations': ['inspect', 'arbitrary_exec']}], 'role': 'admin'}]
        for update in updates:
            self.assertEqual(self.browser('POST', '/api/access/keys/update', {'id': key['id'], **update})[0], 400)
        self.assertEqual(len(self.engine.requests), before)

    def test_revoking_project_immediately_removes_machine_access(self):
        project = self.register()
        key = self.key(project)
        self.assertFalse(self.call(key, 'get_docker_project_status', {'project_id': project})['isError'])
        self.assertEqual(self.browser('POST', '/api/docker/projects/revoke', {'project_id': project})[0], 200)
        before = len(self.engine.requests)
        for transport in ('mcp', 'rest'):
            self.denied('get_docker_project_summary', self.call(key, 'get_docker_project_summary', {'project_id': project}, transport=transport), 'permission_denied')
            listing = self.valid('list_docker_projects', self.call(key, 'list_docker_projects', transport=transport))
            self.assertEqual(listing['projects'], [])
        self.assertEqual(len(self.engine.requests), before)

    def test_machine_result_is_discarded_after_disable_rotate_or_natural_expiry(self):
        project = self.register()
        for operation in ('disable', 'rotate', 'expire'):
            for transport in ('mcp', 'rest'):
                with self.subTest(operation=operation, transport=transport):
                    key = self.key(project)
                    entered, release = threading.Event(), threading.Event()
                    def hold(path):
                        if path.startswith('/containers/') and path.endswith('/json'):
                            entered.set()
                            if not release.wait(4):
                                raise TimeoutError('fixture read was not released')
                        return None
                    self.engine.hook = hold
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        pending = executor.submit(self.call, key, 'get_docker_project_summary', {'project_id': project}, transport=transport)
                        try:
                            self.assertTrue(entered.wait(2))
                            if operation == 'expire':
                                self.now += 86400
                            else:
                                route = '/api/access/keys/enable' if operation == 'disable' else '/api/access/keys/rotate'
                                body = {'id': key['id'], 'enabled': False} if operation == 'disable' else {'id': key['id']}
                                self.assertEqual(self.browser('POST', route, body)[0], 200)
                        finally:
                            release.set()
                        result = pending.result(timeout=4)
                    self.engine.hook = None
                    self.denied('get_docker_project_summary', result, 'invalid_key')
                    self.assertNotIn('services', result['structuredContent'])
                    self.assertNotIn(SECRET, json.dumps(result))

    def test_stale_principal_is_checked_before_any_source_read(self):
        project = self.register()
        key = self.key(project)
        before = len(self.engine.requests)
        with mock.patch.object(self.server.api_access, 'is_current', return_value=False) as current:
            for transport in ('mcp', 'rest'):
                result = self.call(key, 'get_docker_project_status', {'project_id': project}, transport=transport)
                self.denied('get_docker_project_status', result, 'invalid_key')
        self.assertEqual(current.call_count, 2)
        self.assertEqual(len(self.engine.requests), before)

    def test_access_snapshot_and_key_metadata_changes_do_not_query_docker(self):
        project = self.register()
        before = len(self.engine.requests)
        # Creation checks registered IDs through metadata, not live source reads.
        key = self.key(project)
        status, _, access = self.browser()
        self.assertEqual(status, 200)
        row = access['docker_project_catalog']['projects'][0]
        self.assertIsNone(row['available'])
        self.assertEqual(row['verification'], 'not_checked')
        status, _, updated = self.browser('POST', '/api/access/keys/update', {'id': key['id'], 'name': '仅改名称'})
        self.assertEqual(status, 200)
        self.assertEqual(updated['key']['name'], '仅改名称')
        self.assertEqual(len(self.engine.requests), before)

    def test_owner_project_routes_reject_extra_query_and_csrf_bypass_before_backend(self):
        project = self.register()
        before = len(self.engine.requests)
        cases = [('GET', '/api/docker/projects/catalog?name=demo', None, {}),
                 ('GET', '/api/docker/projects#fragment', None, {}),
                 ('POST', '/api/docker/projects/register', {'name': 'demo'}, {}),
                 ('POST', '/api/docker/projects/update', {'project_id': project, 'label': 'test'}, {'Origin': 'http://untrusted.invalid'})]
        for method, route, body, headers in cases:
            with self.subTest(route=route):
                self.assertIn(self.browser(method, route, body, headers=headers)[0], (400, 403))
        self.assertEqual(self.browser('POST', '/api/docker/projects/revoke', {'project_id': project}, remove=['X-FNM-Request'])[0], 403)
        self.assertEqual(len(self.engine.requests), before)


if __name__ == '__main__':
    unittest.main()
