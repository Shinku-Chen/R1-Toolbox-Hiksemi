"""Real owner HTTP, machine policies, native/Engine dispatch and Linux fd bytes.

Only fixed native replies, isolated Engine sockets and constructor-only native
file authority are fixtures. This never calls a NAS or its Docker daemon.
"""

import copy
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import test_integration_http as http_fixture
import test_api_docker_deployment as engine_fixture
import test_api_file_executor as file_fixture
from api_docker_deployment import DockerDeploymentRegistry, DockerDeploymentOperations
from api_file_executor import FileReadExecutor
from vendor_native_capabilities import NativeCapabilities
from vendor_media_operations import IdempotencyStore
from native_capability_specs import TOOL_SPECS


SECRET = 'PRIVATE-COMPLETION-FIXTURE'


class NativeFixture:
    def __init__(self):
        self.calls, self.hook = [], None

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        if authorizer(operation, parameters, body) is not True:
            from vendor_business_transport import BusinessTransportError
            raise BusinessTransportError('authorization_denied')
        self.calls.append((operation, copy.deepcopy(parameters), copy.deepcopy(body), confirmed))
        if self.hook is not None:
            self.hook(operation)
        album = {'album_id': '8', 'type': 'custom', 'name': '普通相册', 'count': 0}
        task = {'task_id': 19, 'task_status': 0, 'user': 100000,
                'from': '/PRIVATE-SOURCE', 'to': '/PRIVATE-TARGET', 'total_size': 10}
        frames = {
            'apply_list': {'list': [{'apply_id': 5, 'module_name': '应用', 'version': '1.0',
                                     'status': 4, 'source': SECRET}]},
            'module_inventory': {'code': 0, 'available_list': [{'id': 5, 'module_name': 'download_engine',
                                 'curr_ver': '1.0', 'latest_ver': '1.1', 'status': 4,
                                 'inst_path': '/PRIVATE-MODULE', 'paramList': SECRET}],
                                 'installed_list': [{'id': 5, 'module_name': 'download_engine',
                                 'curr_ver': '1.0', 'status': 0}]},
            'application_progress': {'code': 0, 'apply_id': 5, 'size': 3, 'total_size': 10, 'status': 3},
            'backup_tasks_list': {'run_list': [task], 'wait_list': [], 'done_list': []},
            'auto_backup_get': {'code': 0, 'udisk_a_status': 1, 'sd_status': 0,
                                'udisk_a_type': 1, 'sd_type': 0},
            'nas_users_list': {'code': 0, 'user_list': [{'user_id': 100000, 'user_name': 'admin',
                               'nickname': '所有者', 'account_email': SECRET}]},
            'device_name_get': {'code': 0, 'name': '小可爱NAS', 'serial_number': SECRET},
            'nas_ai_get': {'code': 0, 'status': 1},
            'movie_scan_status': {'code': 0, 'status': 1, 'scaned': 4, 'total': 12, 'timestamp': 100},
            'albums_list': {'code': 0, 'all_album_list': [album]},
        }
        return 200, copy.deepcopy(frames.get(operation, {'code': 0, 'msg': SECRET}))


class CompletionHTTPBase(unittest.TestCase):
    setUp = http_fixture.IntegrationHTTPTests.setUp
    _patch = http_fixture.IntegrationHTTPTests._patch
    message = staticmethod(http_fixture.IntegrationHTTPTests.message)
    request = http_fixture.IntegrationHTTPTests.request
    rpc = http_fixture.IntegrationHTTPTests.rpc
    rest_action = http_fixture.IntegrationHTTPTests.rest_action
    browser = http_fixture.IntegrationHTTPTests.browser

    def key(self, role, scopes, tools=None, *, deployments=None, roots=None, ips=None):
        status, _, enabled = self.browser('POST', '/api/access/settings', {'enabled': True})
        self.assertEqual(status, 200)
        self.assertTrue(enabled['ok'])
        body = {'name': 'completion HTTP fixture', 'role': role, 'scopes': scopes,
                'file_roots': roots or [], 'docker_projects': [],
                'docker_deployments': deployments or [],
                'allowed_ips': ['127.0.0.1'] if ips is None else ips, 'expires_days': 1}
        if tools is not None:
            body['tool_grants'] = tools
        status, _, value = self.browser('POST', '/api/access/keys', body)
        self.assertEqual(status, 200, {k: v for k, v in value.items() if k != 'token'})
        self.assertTrue(value['ok'])
        return {**value['key'], 'token': value['token']}

    def call(self, key, tool, args=None, *, rest=False):
        arguments = {} if args is None else args
        status, _, value = (self.rest_action(key, tool, arguments) if rest else
                            self.rpc(key, self.message('tools/call', {'name': tool, 'arguments': arguments})))
        self.assertEqual(status, 200)
        self.assertNotIn(key['token'], json.dumps(value, ensure_ascii=False))
        return value if rest else value['result']

    def good(self, value):
        self.assertFalse(value.get('isError'), value)
        self.assertTrue(value['structuredContent']['ok'], value)
        raw = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn('/PRIVATE-', raw)
        return value['structuredContent']

    def bad(self, value, code=None):
        self.assertTrue(value['isError'], value)
        self.assertFalse(value['structuredContent']['ok'], value)
        if code:
            self.assertEqual(value['structuredContent']['error']['code'], code)

    def native(self):
        client = NativeFixture()
        self.server.integration.native_operations = NativeCapabilities(client=client, idempotency_store=IdempotencyStore())
        return client

    def write(self, **values):
        return {'confirmed': True, 'idempotency_key': 'http:completion:1', **values}


class NativeCompletionHTTPTests(CompletionHTTPBase):
    def test_all_native_tools_have_real_catalog_roles_and_strict_schema(self):
        client = self.native()
        tools = [row['name'] for row in TOOL_SPECS]
        key = self.key('admin', ['apps', 'backup', 'system', 'movie', 'albums'], tools)
        status, _, listed = self.rpc(key)
        self.assertEqual(status, 200)
        entries = {row['name']: row for row in listed['result']['tools']}
        self.assertEqual(set(entries), set(tools))
        for name in tools:
            self.assertFalse(entries[name]['annotations']['readOnlyHint'])
            self.assertFalse(entries[name]['inputSchema']['additionalProperties'])
        self.bad(self.call(key, 'set_nas_name', {'name': 'missing confirmation'}), 'invalid_arguments')
        self.bad(self.call(key, 'set_nas_ai_enabled', self.write(enabled=1)), 'invalid_arguments')
        self.assertEqual(client.calls, [])

    def test_native_module_inventory_is_explicit_scoped_and_never_an_install_grant(self):
        client = self.native()
        key = self.key('readwrite', ['apps'], ['list_native_modules'])
        for rest in (False, True):
            result = self.good(self.call(key, 'list_native_modules', rest=rest))
            groups = result['data']['groups']
            self.assertEqual(groups['available']['items'][0]['available_version'], '1.1')
            self.assertEqual(groups['installed']['items'][0]['module_id'], 5)
            self.assertIsNone(groups['installed']['items'][0]['running'])
            self.assertFalse(result['data']['catalog_confers_write_identity'])
            self.assertTrue(result['native_query_may_update_state'])
        self.assertEqual(client.calls, [('module_inventory', {}, {}, False)] * 2)
        self.bad(self.call(key, 'list_native_modules', {'action': 'install'}), 'invalid_arguments')
        self.bad(self.call(key, 'get_application_install_progress', self.write(application_id=5)))
        readonly = self.key('readonly', ['apps'], ['get_app_status'])
        self.bad(self.call(readonly, 'list_native_modules'))
        self.assertEqual(len(client.calls), 2)
        client.hook = lambda _operation: self.browser('POST', '/api/access/keys/revoke', {'id': key['id']})
        self.bad(self.call(key, 'list_native_modules'), 'invalid_key')

    def test_native_name_ai_and_album_delete_across_mcp_rest_use_one_guarded_write(self):
        client = self.native()
        key = self.key('admin', ['system', 'albums'], ['set_nas_name', 'set_nas_ai_enabled', 'delete_album'])
        args = self.write(name='新NAS')
        first = self.good(self.call(key, 'set_nas_name', args))
        repeated = self.good(self.call(key, 'set_nas_name', args, rest=True))
        self.assertTrue(repeated['idempotent_replay'])
        self.assertEqual(first['data'], repeated['data'])
        self.assertEqual(client.calls, [('device_name_set', {'name': '新NAS'}, {}, True)])
        self.good(self.call(key, 'set_nas_ai_enabled', self.write(enabled=False, idempotency_key='http:ai')))
        self.assertEqual(client.calls[-1], ('nas_ai_set', {'mode': 0}, {}, True))
        self.good(self.call(key, 'delete_album', self.write(album_id='8', idempotency_key='http:delete')))
        self.assertEqual([row[0] for row in client.calls[-2:]], ['albums_list', 'album_delete'])
        self.bad(self.call(key, 'set_nas_name', self.write(name='different')), 'idempotency_conflict')

    def test_backup_opaque_version_controls_are_not_unchecked_task_ids(self):
        client = self.native()
        tools = ['list_backup_tasks', 'pause_backup_task', 'cancel_backup_task', 'disable_auto_backup']
        key = self.key('admin', ['backup'], tools)
        data = self.good(self.call(key, 'list_backup_tasks'))
        task = data['data']['groups']['running'][0]
        self.assertRegex(task['version'], r'^backup_[0-9a-f]{32}$')
        self.good(self.call(key, 'pause_backup_task', self.write(task_id=19, expected_version=task['version'])))
        self.assertEqual(client.calls[-1], ('backup_task_halt', {'task_id': 19, 'task_status': 2}, {}, True))
        self.bad(self.call(key, 'cancel_backup_task', self.write(task_id=99,
                 expected_version=task['version'], idempotency_key='http:foreign')), 'object_not_in_owner_catalog')
        self.good(self.call(key, 'disable_auto_backup', self.write(source='usb', idempotency_key='http:disable')))
        self.assertEqual(client.calls[-1], ('auto_backup_disable', {'disk_type': 'udisk_a', 'status': 0}, {}, True))

    def test_admin_exact_tool_scope_old_token_and_readonly_do_not_gain_authority(self):
        client = self.native()
        limited = self.key('admin', ['albums'], ['delete_album'])
        self.bad(self.call(limited, 'set_nas_name', self.write(name='denied')))
        old = self.key('admin', ['system', 'albums'], tools=None)
        self.assertLess(old['policy_version'], 3)
        self.bad(self.call(old, 'delete_album', self.write(album_id='8')))
        read = self.key('readonly', ['albums'], ['get_album_status'])
        self.bad(self.call(read, 'delete_album', self.write(album_id='8')))
        self.assertEqual(client.calls, [])

    def test_native_result_discarded_after_owner_revocation_and_rotation(self):
        client = self.native()
        key = self.key('admin', ['system'], ['get_nas_name', 'set_nas_name'])
        client.hook = lambda _operation: self.browser('POST', '/api/access/keys/revoke', {'id': key['id']})
        result = self.call(key, 'get_nas_name')
        self.bad(result, 'invalid_key')
        self.assertNotIn('小可爱NAS', json.dumps(result, ensure_ascii=False))
        client.hook = None
        replacement = self.key('admin', ['system'], ['get_nas_name'])
        status, _, rotated = self.browser('POST', '/api/access/keys/rotate', {'id': replacement['id']})
        self.assertEqual(status, 200)
        self.assertEqual(self.rpc(replacement)[0], 401)
        self.good(self.call({**rotated['key'], 'token': rotated['token']}, 'get_nas_name'))

    def test_native_inflight_role_edit_and_service_epoch_discard_result(self):
        client = self.native()
        key = self.key('admin', ['system'], ['get_nas_name'])
        edits = []

        def downgrade(_operation):
            edits.append(self.browser('POST', '/api/access/keys/update', {'id': key['id'],
                'role': 'readonly', 'scopes': ['read'], 'tool_grants': ['get_nas_status']})[0])

        client.hook = downgrade
        result = self.call(key, 'get_nas_name')
        self.assertEqual(edits, [200])
        self.bad(result, 'invalid_key')
        self.assertNotIn('小可爱NAS', json.dumps(result, ensure_ascii=False))
        client = self.native()
        key = self.key('admin', ['system'], ['get_nas_name'])
        integration = self.server.integration
        client.hook = lambda _operation: (integration.close(), integration.open())
        self.bad(self.call(key, 'get_nas_name'), 'invalid_key')
        current = self.native()
        self.good(self.call(key, 'get_nas_name'))
        self.assertEqual(len(current.calls), 1)


class DockerCompletionHTTPTests(CompletionHTTPBase):
    def setUp(self):
        super().setUp()
        self.engine = engine_fixture.Engine(self.server.state_dir)
        integration = self.server.integration
        integration.docker_deployments = DockerDeploymentRegistry(self.server.state_dir,
            backend=self.engine.writer, volume_identity=lambda path: [101, 102, 103])
        integration.deployment_operations.close()
        # Recreate the shared existing-project executor after closing its event.
        from api_docker_operations import DockerProjectOperations
        integration.docker_operations = DockerProjectOperations(integration.docker_projects,
            authorizer=integration._is_current)
        integration.deployment_operations = DockerDeploymentOperations(integration.docker_deployments,
            authorizer=integration._is_current, shared_operations=integration.docker_operations)
        self.addCleanup(self.engine.close)

    def register(self):
        status, _, catalog = self.browser('GET', '/api/docker/deployments/catalog')
        self.assertEqual(status, 200)
        self.assertTrue(catalog['ok'])
        status, _, created = self.browser('POST', '/api/docker/deployments/register',
                                          {'label': 'HTTP delegated future resource', 'spec': engine_fixture.spec()})
        self.assertEqual(status, 200, created)
        self.assertTrue(created['ok'])
        return created['deployment']['id']

    def deployment_key(self, identity, *, tools=None, operations=None, role='admin'):
        return self.key(role, ['docker'], sorted(engine_fixture.deployment._TOOLS) if tools is None else tools,
                        deployments=[{'id': identity, 'operations': operations or ['inspect', 'deploy', 'control', 'remove']}])

    def test_owner_register_is_csrf_guarded_bearer_cannot_self_register(self):
        body = {'label': 'invalid origin', 'spec': engine_fixture.spec()}
        response = self.browser('POST', '/api/docker/deployments/register', body,
                                headers={'Origin': 'http://evil.invalid'})
        self.assertEqual(response[0], 403)
        identity = self.register()
        key = self.deployment_key(identity)
        self.assertEqual(key['docker_projects'], [])
        self.assertEqual(key['docker_deployments'][0]['id'], identity)
        status, _, denied = self.request('POST', '/api/docker/deployments/revoke',
            {'deployment_id': identity}, headers={'Authorization': 'Bearer ' + key['token']})
        self.assertEqual(status, 403, denied)
        state = self.browser('GET', '/api/docker/deployments')[2]
        self.assertFalse(state['deployments'][0]['revoked'])

    def test_future_deployment_mcp_rest_idempotency_and_exact_resource(self):
        identity = self.register()
        key = self.deployment_key(identity)
        self.good(self.call(key, 'list_docker_deployments'))
        wrong = 'deployment_' + 'f' * 24
        self.bad(self.call(key, 'plan_docker_deployment', {'deployment_id': wrong,
                 'operation': 'deploy', 'image_reference': engine_fixture.IMAGE}))
        plan = self.good(self.call(key, 'plan_docker_deployment', {'deployment_id': identity,
                 'operation': 'deploy', 'image_reference': engine_fixture.IMAGE}))['plan']
        args = self.write(plan_id=plan['plan_id'])
        first = self.good(self.call(key, 'execute_docker_deployment', args))
        repeated = self.good(self.call(key, 'execute_docker_deployment', args, rest=True))
        self.assertEqual(first['task']['task_id'], repeated['task']['task_id'])
        with self.server.integration.deployment_operations._state_lock:
            workers = list(self.server.integration.deployment_operations._threads)
        for worker in workers:
            worker.join(4)
        final = self.good(self.call(key, 'get_docker_deployment_task', {'deployment_id': identity,
                          'task_id': first['task']['task_id']}))
        self.assertEqual(final['task']['state'], 'completed', final)
        self.assertEqual(sum(method == 'POST' and path.startswith('/containers/create')
                             for method, path, _body in self.engine.requests), 1)
        self.assertEqual(set(self.engine.volumes), {'test-data'})

    def test_deployment_role_downgrade_and_owner_revoke_invalidate_plan(self):
        identity = self.register()
        key = self.deployment_key(identity)
        plan = self.good(self.call(key, 'plan_docker_deployment', {'deployment_id': identity,
                     'operation': 'deploy', 'image_reference': engine_fixture.IMAGE}))['plan']
        status, _, edited = self.browser('POST', '/api/access/keys/update', {'id': key['id'],
            'role': 'readonly', 'tool_grants': ['list_docker_deployments'],
            'docker_deployments': [{'id': identity, 'operations': ['inspect']}]})
        self.assertEqual(status, 200, edited)
        self.bad(self.call(key, 'execute_docker_deployment', self.write(plan_id=plan['plan_id'])))
        self.good(self.call(key, 'list_docker_deployments'))
        status, _, revoked = self.browser('POST', '/api/docker/deployments/revoke', {'deployment_id': identity})
        self.assertEqual(status, 200)
        self.assertTrue(revoked['ok'])
        self.bad(self.call(key, 'list_docker_deployments'))
        self.assertFalse(any(method != 'GET' for method, _path, _body in self.engine.requests))


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_PATH') and hasattr(os, 'pread'),
                     'Linux real fd, O_PATH and mount identities required')
class FileCompletionHTTPTests(CompletionHTTPBase):
    def setUp(self):
        super().setUp()
        file_fixture.OpenedFileTests.setUp(self)
        del self.key  # File fixture's principal is not the HTTP key helper.
        self.server.integration.file_operations.close()
        self.server.integration.file_operations = FileReadExecutor(None, root=self.root,
            authorizer=self.server.integration._is_current, authority=self.authority)
        self._patch(self.server.integration, 'file_root_catalog', return_value={
            'available': True, 'status': 'authorization_plan', 'boundary_version': 3,
            'roots': [dict(file_fixture.ROW)]})
        self.tools = sorted(['list_directory', 'get_file_info', 'prepare_file_download',
                             'get_file_transfer', 'cancel_file_transfer'])
        self.file_key = self.key('readonly', ['files'], self.tools,
            roots=[{'id': file_fixture.ROOT, 'path': '', 'access': 'readonly'}], ips=['127.0.0.0/8'])

    def prepare(self):
        data = self.good(self.call(self.file_key, 'prepare_file_download',
                                  {'root_id': file_fixture.ROOT, 'path': 'payload.bin'}))
        return data['content_path']

    def binary(self, path, *, key=None, method='GET', headers=None, duplicates=(), body=None,
               peer='127.0.0.1'):
        values = {'Host': self.host, 'Connection': 'close'}
        if key is not None:
            values['Authorization'] = 'Bearer ' + key['token']
        if body is not None:
            values['Content-Length'] = str(len(body))
        values.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5,
                                                 source_address=(peer, 0))
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in list(values.items()) + list(duplicates):
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
        finally:
            connection.close()

    def test_real_prepare_head_range_416_and_full_bytes(self):
        path = self.prepare()
        status, headers, data = self.binary(path, key=self.file_key, method='HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(data, b'')
        self.assertEqual(int(headers['content-length']), len(file_fixture.DATA))
        status, headers, data = self.binary(path, key=self.file_key, headers={'Range': 'bytes=5-13'})
        self.assertEqual(status, 206)
        self.assertEqual(data, file_fixture.DATA[5:14])
        status, headers, data = self.binary(path, key=self.file_key, headers={'Range': 'bytes=99999999-'})
        self.assertEqual(status, 416)
        self.assertEqual(data, b'')
        self.assertEqual(headers['content-range'], 'bytes */' + str(len(file_fixture.DATA)))
        status, headers, data = self.binary(path, key=self.file_key)
        self.assertEqual(status, 200)
        self.assertEqual(data, file_fixture.DATA)
        self.assertEqual(headers['cache-control'], 'no-store')

    def test_no_cookie_or_bearer_substitution_duplicate_range_condition_or_body(self):
        path = self.prepare()
        self.assertEqual(self.binary(path)[0], 401)
        self.assertIn(self.binary(path, headers={'Cookie': self.cookie})[0], (401, 403))
        for options in ({'headers': {'If-Range': 'anything'}}, {'body': b'x'},
                        {'headers': {'Range': 'bytes=1-2'}, 'duplicates': [('Range', 'bytes=4-5')]},
                        {'headers': {'Range': 'bytes=0-1,4-5'}}):
            status, _headers, _body = self.binary(path, key=self.file_key, **options)
            self.assertEqual(status, 400, options)
        self.assertEqual(self.binary(path, key=self.file_key, headers={'Range': 'bytes=0-3'})[2], file_fixture.DATA[:4])

    def test_key_peer_rotation_and_revocation_never_reuse_existing_handle(self):
        path = self.prepare()
        other = self.key('readonly', ['files'], self.tools,
            roots=[{'id': file_fixture.ROOT, 'path': '', 'access': 'readonly'}], ips=['127.0.0.0/8'])
        self.assertEqual(self.binary(path, key=other)[0], 404)
        self.assertEqual(self.binary(path, key=self.file_key, peer='127.0.0.2')[0], 404)
        status, _, rotated = self.browser('POST', '/api/access/keys/rotate', {'id': self.file_key['id']})
        self.assertEqual(status, 200)
        self.assertEqual(self.binary(path, key=self.file_key)[0], 401)
        replacement = {**rotated['key'], 'token': rotated['token']}
        self.assertEqual(self.binary(path, key=replacement)[0], 404)
        self.browser('POST', '/api/access/keys/revoke', {'id': replacement['id']})
        self.assertEqual(self.binary(path, key=replacement)[0], 401)

    def test_close_reopen_invalidates_old_transfer_without_revoking_token(self):
        path = self.prepare()
        integration = self.server.integration
        integration.close()
        integration.open()
        self.assertEqual(self.binary(path, key=self.file_key)[0], 404)
        integration.file_operations.close()
        integration.file_operations = FileReadExecutor(None, root=self.root,
            authorizer=integration._is_current, authority=self.authority)
        current = self.prepare()
        self.assertNotEqual(current, path)
        self.assertEqual(self.binary(current, key=self.file_key, headers={'Range': 'bytes=0-9'})[2], file_fixture.DATA[:10])


if __name__ == '__main__':
    unittest.main()
