"""Real owner HTTP, Token policy, MCP schema and business dispatch integration.

Only native media replies and fixed downloader sockets are fixture data. This
does not contact a NAS, mount user files or operate the host Docker daemon.
"""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from download_operations import DownloadOperations
from vendor_business_transport import BusinessTransportError
from vendor_media_operations import IdempotencyStore, MediaOperations
import vendor_download_diagnostics as wire
from tests.test_api_docker_projects import EngineFixture
from tests.test_download_operations import Factory, ROW, TASK, mutation
from tests import test_integration_http as http_fixture
from tests.test_vendor_download_diagnostics import FakeSocket, OWNER, frame


SECRET = 'fixture-native-private-credential'
PRIVATE_PATH = '/drives/private/Users/fixture/private-file.jpg'


class NativeMediaFixture:
    def __init__(self):
        self.calls, self.rejected, self.hook = [], set(), None

    def request(self, operation, parameters, body, authorizer, *, confirmed=False):
        self.calls.append((operation, copy.deepcopy(parameters), copy.deepcopy(body), confirmed))
        if self.hook is not None:
            self.hook(operation)
        if operation in self.rejected:
            return 200, {'code': -2003, 'msg': SECRET, 'path': PRIVATE_PATH}
        album = {'album_id': '8', 'name': '旅行', 'type': 'custom', 'count': 0,
                 'pic_url_list': [{'pic_url': PRIVATE_PATH}], 'token': SECRET}
        if operation == 'albums_list':
            return 200, {'code': 0, 'all_album_list': [album], 'msg': SECRET}
        if operation == 'shared_albums_list':
            return 200, {'code': 0, 'share_album_list': [dict(album, album_id='share_9', creater=1)]}
        if operation == 'albums_create':
            return 200, dict(album, code=0, name=parameters['album_name'])
        if operation == 'movie_libraries_list':
            return 200, {'code': 0, 'list': [{'lid': 7, 'name': '影视',
                                           'sources': [PRIVATE_PATH], 'members': [SECRET]}]}
        if operation == 'movie_library_rename':
            return 200, {'code': 0, 'msg': SECRET, 'raw_config': {'path': PRIVATE_PATH}}
        raise AssertionError('unexpected fixture operation')


class McpBusinessHTTPTests(unittest.TestCase):
    # Reuse server setup/helpers without inheriting the older test methods.
    setUp = http_fixture.IntegrationHTTPTests.setUp
    _patch = http_fixture.IntegrationHTTPTests._patch
    message = staticmethod(http_fixture.IntegrationHTTPTests.message)
    request = http_fixture.IntegrationHTTPTests.request
    rpc = http_fixture.IntegrationHTTPTests.rpc
    rest_action = http_fixture.IntegrationHTTPTests.rest_action
    browser = http_fixture.IntegrationHTTPTests.browser

    def owner_key(self, role, scopes, tools=None, projects=None):
        status, _, enabled = self.browser('POST', '/api/access/settings', {'enabled': True})
        self.assertEqual(status, 200)
        self.assertTrue(enabled['ok'])
        body = {'name': 'HTTP business fixture', 'role': role, 'scopes': list(scopes),
                'file_roots': [], 'docker_projects': projects or [],
                'allowed_ips': ['127.0.0.1'], 'expires_days': 1}
        if tools is not None:
            body['tool_grants'] = list(tools)
        status, _, created = self.browser('POST', '/api/access/keys', body)
        self.assertEqual(status, 200, {k: v for k, v in created.items() if k != 'token'})
        self.assertTrue(created['ok'])
        if tools is not None:
            self.assertEqual(created['key']['policy_version'], 3)
            self.assertEqual(set(created['key']['tool_grants']), set(tools))
        return {**created['key'], 'token': created['token']}

    def tool_call(self, key, tool, arguments=None):
        status, _, value = self.rpc(key, self.message('tools/call',
            {'name': tool, 'arguments': {} if arguments is None else arguments}))
        self.assertEqual(status, 200)
        self.assertIn('result', value)
        return value['result']

    def assert_safe(self, value, key=None):
        raw = json.dumps(value, ensure_ascii=False)
        for private in (SECRET, PRIVATE_PATH, '/secret/private', 'passkey', 'raw_config'):
            self.assertNotIn(private, raw)
        if key is not None:
            self.assertNotIn(key['token'], raw)

    def native_media(self):
        native = NativeMediaFixture()
        self.server.integration.media_operations = MediaOperations(
            client=native, idempotency_store=IdempotencyStore())
        return native

    def downloads(self, replies):
        factory = Factory(replies)
        self.server.integration.download_operations = DownloadOperations(
            authorizer=self.server.integration._is_current,
            ownership_check=lambda **_: copy.deepcopy(OWNER), socket_factory=factory,
            auth_state=wire.QbAuthState())
        return factory

    def test_readonly_download_key_reads_native_metadata_but_cannot_write(self):
        factory = self.downloads([frame(b'v5.0.5'), frame([ROW])])
        key = self.owner_key('readonly', ['downloads'], ['list_download_tasks'])
        result = self.tool_call(key, 'list_download_tasks', {'engine': 'qbittorrent'})
        self.assertFalse(result.get('isError', False), result)
        data = result['structuredContent']
        self.assertTrue(data['ok'])
        self.assertEqual(data['tasks'][0]['task_id'], TASK)
        self.assertEqual(data['tasks'][0]['name'], ROW['name'])
        self.assertFalse(data['tasks'][0]['location_available'])
        self.assert_safe(result, key)
        denied = self.tool_call(key, 'pause_download_task', mutation())
        self.assertTrue(denied['isError'])
        self.assertFalse(denied['structuredContent']['ok'])
        self.assertEqual(len(factory.sockets), 2)
        self.assertFalse(any(s.sent.startswith(b'POST ') for s in factory.sockets))

    def test_old_role_key_does_not_implicitly_gain_new_download_or_media_tools(self):
        native = self.native_media()
        factory = self.downloads([])
        key = self.owner_key('admin', ['downloads', 'albums'], tools=None)
        self.assertEqual(key['policy_version'], 2)
        status, _, listed = self.rpc(key)
        self.assertEqual(status, 200)
        names = {item['name'] for item in listed['result']['tools']}
        self.assertTrue(names.isdisjoint({'list_download_tasks', 'create_album', 'list_albums'}))
        for tool, args in (('list_download_tasks', {'engine': 'qbittorrent'}),
                           ('list_albums', {}),
                           ('create_album', {'name': '旅行', 'confirmed': True, 'idempotency_key': 'old-key'})):
            self.assertTrue(self.tool_call(key, tool, args)['isError'])
        self.assertEqual(native.calls, [])
        self.assertEqual(factory.sockets, [])

    def test_readonly_album_key_keeps_cache_only_and_cannot_run_native_lists(self):
        native = self.native_media()
        key = self.owner_key('readonly', ['albums'], ['get_album_status'])
        result = self.tool_call(key, 'list_albums')
        self.assertTrue(result['isError'])
        self.assertEqual(native.calls, [])
        status, _, bad = self.browser('POST', '/api/access/keys', {
            'name': 'invalid readonly native list', 'role': 'readonly', 'scopes': ['albums'],
            'tool_grants': ['list_albums'], 'file_roots': [], 'docker_projects': []})
        self.assertEqual(status, 400)
        self.assertFalse(bad['ok'])

    def test_readwrite_media_lists_create_and_integer_movie_id_use_real_schema(self):
        native = self.native_media()
        tools = ['list_albums', 'list_shared_albums', 'create_album',
                 'list_movie_libraries', 'rename_movie_library']
        key = self.owner_key('readwrite', ['albums', 'movie'], tools)
        for tool in ('list_albums', 'list_shared_albums', 'list_movie_libraries'):
            result = self.tool_call(key, tool)
            self.assertFalse(result.get('isError', False), result)
            self.assertTrue(result['structuredContent']['available'])
            self.assert_safe(result, key)
        created = self.tool_call(key, 'create_album',
            {'name': '新旅行', 'confirmed': True, 'idempotency_key': 'http:create:1'})
        self.assertFalse(created.get('isError', False), created)
        self.assertEqual(created['structuredContent']['data']['name'], '新旅行')
        renamed = self.tool_call(key, 'rename_movie_library',
            {'library_id': 7, 'name': '新影视', 'confirmed': True, 'idempotency_key': 'http:movie:1'})
        self.assertFalse(renamed.get('isError', False), renamed)
        self.assertEqual(native.calls[-1], ('movie_library_rename', {}, {'lid': 7, 'name': '新影视'}, True))
        before = len(native.calls)
        rejected = self.tool_call(key, 'rename_movie_library',
            {'library_id': '7', 'name': '错误类型', 'confirmed': True, 'idempotency_key': 'http:movie:bad'})
        self.assertTrue(rejected['isError'])
        self.assertEqual(rejected['structuredContent']['error']['code'], 'invalid_arguments')
        self.assertEqual(len(native.calls), before)
        self.assert_safe(created, key)
        self.assert_safe(renamed, key)

    def test_album_create_same_token_and_key_runs_native_client_once_across_mcp_and_rest(self):
        native = self.native_media()
        key = self.owner_key('readwrite', ['albums'], ['create_album'])
        args = {'name': '只创建一次', 'confirmed': True, 'idempotency_key': 'http:single:create'}
        first = self.tool_call(key, 'create_album', args)
        status, _, repeated = self.rest_action(key, 'create_album', args)
        self.assertEqual(status, 200)
        self.assertFalse(repeated.get('isError', False), repeated)
        self.assertTrue(repeated['structuredContent']['idempotent_replay'])
        self.assertEqual(repeated['structuredContent']['data'], first['structuredContent']['data'])
        self.assertEqual(len(native.calls), 1)
        self.assert_safe(repeated, key)

    def test_native_failure_is_mcp_tool_error_without_original_message_or_paths(self):
        native = self.native_media()
        native.rejected.update(('albums_list', 'albums_create'))
        key = self.owner_key('readwrite', ['albums'], ['list_albums', 'create_album'])
        for tool, args in (('list_albums', {}), ('create_album', {
                'name': '不会成功', 'confirmed': True, 'idempotency_key': 'native:reject'})):
            result = self.tool_call(key, tool, args)
            self.assertTrue(result['isError'])
            self.assertFalse(result['structuredContent']['ok'])
            self.assertEqual(result['structuredContent']['error']['code'], 'native_request_rejected')
            self.assertIsNone(result['structuredContent']['data'])
            self.assert_safe(result, key)

    def test_owner_revocation_during_native_request_prevents_result_disclosure(self):
        native = self.native_media()
        key = self.owner_key('readwrite', ['albums'], ['list_albums'])
        revoked = []
        def revoke(operation):
            status, _, value = self.browser('POST', '/api/access/keys/revoke', {'id': key['id']})
            revoked.append((status, value['ok']))
        native.hook = revoke
        result = self.tool_call(key, 'list_albums')
        self.assertEqual(revoked, [(200, True)])
        self.assertTrue(result['isError'])
        data = result['structuredContent']
        self.assertEqual(data['error']['code'], 'invalid_key')
        self.assertNotIn('data', data)
        self.assertNotIn('旅行', json.dumps(result, ensure_ascii=False))
        self.assert_safe(result, key)

    def test_owner_revocation_during_download_response_prevents_task_disclosure(self):
        key = self.owner_key('readonly', ['downloads'], ['get_download_task'])
        parent, revoked = self, []
        class RevokeSocket(FakeSocket):
            def recv(self, count):
                block = super().recv(count)
                if not revoked:
                    status, _, value = parent.browser('POST', '/api/access/keys/revoke', {'id': key['id']})
                    revoked.append((status, value['ok']))
                return block
        factory = self.downloads([frame(b'v4.6.7'), RevokeSocket(frame([ROW]))])
        result = self.tool_call(key, 'get_download_task', {'engine': 'qbittorrent', 'task_id': TASK})
        self.assertEqual(revoked, [(200, True)])
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['error']['code'], 'invalid_key')
        self.assertNotIn('task', result['structuredContent'])
        self.assertEqual(len(factory.sockets), 2)
        self.assertNotIn(ROW['name'], json.dumps(result, ensure_ascii=False))
        self.assert_safe(result, key)

    def test_close_open_during_download_connect_invalidates_old_generation_and_allows_new_http_call(self):
        integration = self.server.integration
        key = self.owner_key('readwrite', ['downloads'], ['get_download_task', 'pause_download_task'])
        principals, sockets = [], []
        replies = [frame(b'v4.6.7'), frame([ROW]), frame(b'')]

        class CaptureDownloads(DownloadOperations):
            def execute(self, tool, arguments, principal):
                principals.append(copy.deepcopy(principal))
                return super().execute(tool, arguments, principal)

        def restart_at_mutation_connect(address, timeout):
            if len(sockets) == 2:
                integration.close()
                integration.open()
            channel = FakeSocket(replies[len(sockets)])
            sockets.append(channel)
            return channel

        integration.download_operations = CaptureDownloads(authorizer=integration._is_current,
            ownership_check=lambda **_: copy.deepcopy(OWNER), socket_factory=restart_at_mutation_connect,
            auth_state=wire.QbAuthState())
        old = self.tool_call(key, 'pause_download_task', mutation())
        self.assertTrue(old['isError'])
        self.assertEqual(old['structuredContent']['error']['code'], 'invalid_key')
        self.assertEqual(len(sockets), 3)
        self.assertEqual(sockets[-1].sent, b'', '服务重开后旧调用仍发送了控制请求')
        old_principal = principals[0]
        self.assertTrue(integration.access.is_current(old_principal),
                        '此回归须由服务代际失效触发，不能依赖撤销 Token')
        self.assertFalse(integration._is_current(old_principal))

        new_factory = Factory([frame(b'v4.6.7'), frame([ROW])])
        integration.download_operations = CaptureDownloads(authorizer=integration._is_current,
            ownership_check=lambda **_: copy.deepcopy(OWNER), socket_factory=new_factory,
            auth_state=wire.QbAuthState())
        current = self.tool_call(key, 'get_download_task', {'engine': 'qbittorrent', 'task_id': TASK})
        self.assertFalse(current.get('isError', False), current)
        self.assertTrue(current['structuredContent']['ok'])
        self.assertEqual(len(new_factory.sockets), 2)
        new_principal = principals[-1]
        self.assertNotEqual(old_principal['_service_generation'], new_principal['_service_generation'])
        self.assertTrue(integration._is_current(new_principal))
        denied = integration.execute('get_download_task',
            {'engine': 'qbittorrent', 'task_id': TASK}, old_principal)
        self.assertFalse(denied['ok'])
        self.assertEqual(denied['error']['code'], 'invalid_key')
        self.assertEqual(len(new_factory.sockets), 2)
        self.assert_safe(old, key)
        self.assert_safe(current, key)

    def test_close_open_before_media_send_invalidates_old_callback_and_allows_new_http_call(self):
        integration = self.server.integration
        key = self.owner_key('readwrite', ['albums'], ['create_album'])
        principals, native_sends, callback_results = [], [], []

        class CaptureMedia(MediaOperations):
            def execute(self, tool, arguments=None, *, authorizer=None, principal=None):
                principals.append(copy.deepcopy(principal))
                return super().execute(tool, arguments, authorizer=authorizer, principal=principal)

        class RestartMedia(NativeMediaFixture):
            def request(self, operation, parameters, body, authorizer, *, confirmed=False):
                integration.close()
                integration.open()
                allowed = authorizer(operation, parameters, body)
                callback_results.append(allowed)
                if allowed is not True:
                    raise BusinessTransportError('authorization_denied')
                native_sends.append(operation)
                return super().request(operation, parameters, body, authorizer, confirmed=confirmed)

        integration.media_operations = CaptureMedia(client=RestartMedia(), idempotency_store=IdempotencyStore())
        arguments = {'name': '代际测试', 'confirmed': True, 'idempotency_key': 'lifecycle:album'}
        old = self.tool_call(key, 'create_album', arguments)
        self.assertTrue(old['isError'])
        self.assertEqual(old['structuredContent']['error']['code'], 'invalid_key')
        self.assertEqual(callback_results, [False])
        self.assertEqual(native_sends, [])
        old_principal = principals[0]
        self.assertTrue(integration.access.is_current(old_principal))
        self.assertFalse(integration._is_current(old_principal))

        native = NativeMediaFixture()
        integration.media_operations = CaptureMedia(client=native, idempotency_store=IdempotencyStore())
        current = self.tool_call(key, 'create_album', arguments)
        self.assertFalse(current.get('isError', False), current)
        self.assertTrue(current['structuredContent']['available'])
        self.assertEqual(len(native.calls), 1)
        new_principal = principals[-1]
        self.assertNotEqual(old_principal['_service_generation'], new_principal['_service_generation'])
        self.assertTrue(integration._is_current(new_principal))
        denied = integration.execute('create_album', dict(arguments, idempotency_key='lifecycle:old'), old_principal)
        self.assertFalse(denied['ok'])
        self.assertEqual(denied['error']['code'], 'invalid_key')
        self.assertEqual(len(native.calls), 1)
        self.assert_safe(old, key)
        self.assert_safe(current, key)

    def test_owner_admin_saves_three_project_operations_and_readwrite_cannot_control(self):
        engine = EngineFixture()
        registry = self.server.integration.docker_projects
        registry.backend = engine.reader
        status, _, catalog = self.browser(path='/api/docker/projects/catalog')
        self.assertEqual(status, 200)
        self.assertTrue(catalog['ok'])
        candidate = catalog['candidates'][0]['id']
        status, _, registered = self.browser('POST', '/api/docker/projects/register',
                                             {'candidate_id': candidate, 'label': 'HTTP Docker fixture'})
        self.assertEqual(status, 200)
        self.assertTrue(registered['ok'])
        project_id = registered['project']['id']
        grant = {'id': project_id, 'operations': ['inspect', 'control', 'update_image']}
        admin = self.owner_key('admin', ['docker'], ['list_docker_projects', 'control_docker_project',
            'plan_docker_project_update', 'execute_docker_project_update'], [grant])
        self.assertEqual(admin['docker_projects'], [grant])
        status, _, state = self.browser()
        self.assertEqual(status, 200)
        stored = next(item for item in state['keys'] if item['id'] == admin['id'])
        self.assertEqual(stored['docker_projects'], [grant])
        key = self.owner_key('readwrite', ['docker'], ['list_docker_projects'],
                             [{'id': project_id, 'operations': ['inspect']}])
        action = mock.Mock(side_effect=AssertionError('readwrite must not execute Docker control'))
        self.server.integration.docker_operations.control = action
        denied = self.tool_call(key, 'control_docker_project', {
            'project_id': project_id, 'operation': 'restart', 'confirmed': True,
            'idempotency_key': 'http:docker:denied'})
        self.assertTrue(denied['isError'])
        action.assert_not_called()
        status, _, invalid = self.browser('POST', '/api/access/keys', {
            'name': 'invalid readwrite control', 'role': 'readwrite', 'scopes': ['docker'],
            'tool_grants': ['control_docker_project'], 'file_roots': [],
            'docker_projects': [grant]})
        self.assertEqual(status, 400)
        self.assertFalse(invalid['ok'])
        action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
