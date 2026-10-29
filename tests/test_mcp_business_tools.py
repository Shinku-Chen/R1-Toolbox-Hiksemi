"""Real cache projections and machine authorization; no live NAS is contacted."""

import concurrent.futures
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import integration_api
from mcp_service import META_REQUIRED_SCOPE, tool_catalog
from vendor_app_diagnostics import record_application_snapshot
from vendor_media_snapshot import record_media_snapshot
import vendor_media_snapshot
import test_integration_http as http_fixture
import test_vendor_media_snapshot as media_fixture


TOOLS = {'get_app_status': 'apps', 'get_download_status': 'downloads',
         'get_movie_status': 'movie', 'get_album_status': 'albums',
         'get_share_status': 'sharing'}
STAMP = '2026-10-29T00:00:00.000+00:00'
PRIVATE = 'private-token-cookie-path-fixture'


def app_snapshot():
    return {'schema_version': 1, 'source': 'histor.applycenter.get_apply_list',
            'sampled_at': STAMP, 'available': True, 'status': 'ok',
            'data': {'apps': [{'name': 'demo-app', 'version': '1.0',
                              'installation_status_code': 4, 'running': None,
                              'token': PRIVATE, 'path': PRIVATE}]}}


def downloads_snapshot():
    return {'sampled_at': STAMP, 'categories': {'downloads': {
        'available': True, 'status': 'ok', 'source': 'verified_local_download_engines',
        'reason': None, 'token': PRIVATE,
        'engines': [{'engine': 'aria2', 'available': False, 'status': 'not_listening',
                     'source': 'aria2.fixed_rpc', 'reason': '下载来源尚未监听。'},
                    {'engine': 'qbittorrent', 'available': True, 'status': 'ok',
                     'source': 'qbittorrent.fixed_webapi', 'reason': None}],
        'data': {'complete': False, 'engines': [
            {'engine': 'aria2', 'available': False, 'status': 'not_listening', 'data': None},
            {'engine': 'qbittorrent', 'available': True, 'status': 'ok', 'sid': PRIVATE,
             'data': {'counts': {'active': None, 'waiting': 2},
                      'tasks': [{'status': 'downloading', 'total_bytes': None,
                                 'completed_bytes': 2, 'progress_percent': None,
                                 'name': PRIVATE, 'path': PRIVATE, 'hash': PRIVATE}]}}]}}}}


class BusinessToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='r1-mcp-business-')
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.web = SimpleNamespace(state_dir=self.directory, business_state_lock=threading.Lock(),
                                   _business_cached=None, _business_epoch=0, _business_cached_epoch=0)
        self.api = integration_api.IntegrationApi(self.web, state_dir=self.directory)
        self.api.access.configure({'enabled': True})
        self.key = self.api.access.create_key({'name': 'business read', 'role': 'readonly',
            'scopes': list(TOOLS.values()), 'tool_grants': list(TOOLS),
            'allowed_ips': [], 'expires_days': 1})
        self.principal, _ = self.api.access.authorize('Bearer ' + self.key['token'], '127.0.0.1')

    def result(self, name):
        result = self.api.mcp.call_tool(name, {}, self.principal)
        self.assertFalse(result['isError'], result)
        return result['structuredContent']

    def record_media(self, value=None):
        with mock.patch.object(vendor_media_snapshot, '_stamp', return_value=STAMP):
            receipt = record_media_snapshot(self.directory, value or media_fixture.batch_result())
        self.assertTrue(receipt['stored'])

    def test_all_five_tools_have_readonly_schema_and_exact_domain_gates(self):
        catalog = {row['name']: row for row in tool_catalog(self.principal)}
        self.assertEqual(set(catalog), set(TOOLS))
        for name, scope in TOOLS.items():
            self.assertEqual(catalog[name]['_meta'][META_REQUIRED_SCOPE], scope)
            self.assertTrue(catalog[name]['annotations']['readOnlyHint'])
            self.assertEqual(catalog[name]['inputSchema']['properties'], {})
            self.assertFalse(catalog[name]['inputSchema']['additionalProperties'])
            for principal in (dict(self.principal, scopes=['read']),
                              dict(self.principal, role='legacy'),
                              dict(self.principal, role='unknown'),
                              dict(self.principal, tool_grants=[]),
                              dict(self.principal, policy_version=2, tool_grants=[])):
                with self.subTest(name=name, principal=principal):
                    self.assertNotIn(name, {row['name'] for row in tool_catalog(principal)})
                    result = self.api.mcp.call_tool(name, {'path': PRIVATE}, principal)
                    self.assertTrue(result['isError'])
                    self.assertEqual(result['structuredContent']['error']['code'], 'scope_denied')
                    self.assertNotIn(PRIVATE, json.dumps(result))
            result = self.api.mcp.call_tool(name, {'refresh': True}, self.principal)
            self.assertEqual(result['structuredContent']['error']['code'], 'invalid_arguments')

    def test_missing_cache_is_unavailable_without_task_zeros_or_native_queries(self):
        for name in ('get_download_status', 'get_movie_status', 'get_album_status', 'get_share_status'):
            result = self.result(name)
            self.assertFalse(result['available'])
            self.assertEqual(result['status'], 'not_collected')
            self.assertIsNone(result['data'])
            self.assertIsNone(result['freshness']['observed_at'])
            self.assertIsNone(result['freshness']['age_seconds'])

    def test_invalid_media_cache_cannot_become_an_empty_success(self):
        (self.directory / 'media-snapshot.json').write_text('{}', encoding='utf-8')
        for name in ('get_movie_status', 'get_album_status', 'get_share_status'):
            result = self.result(name)
            self.assertFalse(result['available'])
            self.assertEqual(result['status'], 'cache_invalid')
            self.assertIsNone(result['data'])

    def test_application_cache_preserves_partial_inventory_unknown_running_and_age(self):
        self.assertTrue(record_application_snapshot(self.directory, app_snapshot()))
        now = datetime.fromisoformat(STAMP).timestamp() + 90
        with mock.patch.object(integration_api.time, 'time', return_value=now):
            result = self.result('get_app_status')
        self.assertTrue(result['available'])
        self.assertEqual(result['source'], 'toolbox.application_snapshot')
        self.assertFalse(result['data']['complete'])
        self.assertFalse(result['data']['live'])
        self.assertIsNone(result['data']['apps'][0]['running'])
        self.assertEqual(result['freshness']['age_seconds'], 90)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_failed_application_refresh_retains_attempt_time_without_a_successful_inventory(self):
        value = app_snapshot()
        value.update(available=False, error_code='timeout', data=None)
        self.assertTrue(record_application_snapshot(self.directory, value))
        result = self.result('get_app_status')
        self.assertFalse(result['available'])
        self.assertIsNone(result['data'])
        self.assertEqual(result['last_failure']['error_code'], 'timeout')
        self.assertEqual(result['last_failure']['observed_at'], '2026-10-29T00:00:00+00:00')

    def test_future_or_unknown_cache_age_remains_unknown(self):
        self.web._business_cached = downloads_snapshot()
        for stamp in ('9999-12-31T00:00:00+00:00', None, '2026-10-29T00:00:00'):
            with self.subTest(stamp=stamp):
                self.web._business_cached['sampled_at'] = stamp
                self.assertIsNone(self.result('get_download_status')['freshness']['age_seconds'])

    def test_downloads_only_reuses_existing_projection_and_keeps_null_and_engine_failures(self):
        self.web._business_cached = downloads_snapshot()
        result = self.result('get_download_status')
        self.assertTrue(result['available'])
        self.assertFalse(result['data']['complete'])
        task = result['data']['engines'][1]['data']['tasks'][0]
        self.assertIsNone(task['total_bytes'])
        self.assertIsNone(task['progress_percent'])
        self.assertEqual(result['last_failure'][0]['status'], 'not_listening')
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.web._business_epoch += 1
        self.assertIsNone(self.result('get_download_status')['data'])

    def test_media_domains_are_disjoint_and_do_not_disclose_whole_cache_coverage(self):
        self.record_media()
        expected = {'get_movie_status': {'scraper'},
                    'get_album_status': {'face', 'ocr', 'thumbnail', 'similar', 'ai'},
                    'get_share_status': {'shared_album'}}
        for name, ids in expected.items():
            result = self.result(name)
            self.assertEqual({row['id'] for row in result['data']['results']}, ids)
            self.assertEqual(result['status'], 'cached')
            self.assertTrue(result['data']['complete'])
            self.assertNotIn('coverage', result['data'])
            self.assertTrue(all(row['freshness']['observed_at'] is not None
                                for row in result['data']['results']))
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_partial_media_keeps_failed_item_null_and_latest_failure_after_recovery(self):
        value = media_fixture.batch_result()
        value['results'][1].update(available=False, query_success=False,
                                   status='unavailable', reason='timeout', data=None)
        self.record_media(value)
        result = self.result('get_album_status')
        self.assertEqual(result['status'], 'partial')
        failed = next(row for row in result['data']['results'] if row['id'] == 'ocr')
        self.assertIsNone(failed['data'])
        self.assertEqual(result['last_failure'][0]['id'], 'ocr')
        self.assertEqual(result['last_failure'][0]['reason'], 'timeout')
        self.record_media()
        self.assertEqual(self.result('get_album_status')['last_failure'][0]['reason'], 'timeout')

    def test_shared_album_absence_is_not_applicable_not_empty_or_idle(self):
        value = media_fixture.batch_result()
        value['results'][-1].update(available=False, query_success=False,
            status='not_applicable', reason='no_existing_owner_album', data=None)
        self.record_media(value)
        result = self.result('get_share_status')
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'not_applicable')
        self.assertIsNone(result['data']['results'][0]['data'])

    def test_current_key_is_rechecked_before_and_after_every_business_read(self):
        for name in TOOLS:
            with self.subTest(tool=name), mock.patch.object(self.api, '_read_business') as read:
                with mock.patch.object(self.api.access, 'is_current', side_effect=[False]):
                    result = self.api.mcp.call_tool(name, {}, self.principal)
                self.assertEqual(result['structuredContent']['error']['code'], 'invalid_key')
                read.assert_not_called()
                read.return_value = {'ok': True, 'data': PRIVATE}
                with mock.patch.object(self.api.access, 'is_current', side_effect=[True, False]):
                    result = self.api.mcp.call_tool(name, {}, self.principal)
                self.assertEqual(result['structuredContent']['error']['code'], 'invalid_key')
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_five_legacy_business_domains_do_not_upgrade_without_explicit_tools(self):
        legacy = self.api.access.create_key({'name': 'old domain key', 'role': 'admin',
            'scopes': list(TOOLS.values()), 'allowed_ips': [], 'expires_days': 0})
        principal, _ = self.api.access.authorize('Bearer ' + legacy['token'], '127.0.0.1')
        self.assertEqual(tool_catalog(principal), [])
        capabilities = self.api.capabilities(principal)
        self.assertTrue(all(row['status'] == 'pending_enable' for row in capabilities['domains']))
        self.api.access.update_key({'id': legacy['id'], 'name': 'renamed old domain key'})
        principal, _ = self.api.access.authorize('Bearer ' + legacy['token'], '127.0.0.1')
        self.assertEqual(tool_catalog(principal), [])


class BusinessToolsHTTPTests(unittest.TestCase):
    # Reuse the real HTTP fixture without collecting its legacy tests twice.
    setUp = http_fixture.IntegrationHTTPTests.setUp
    _patch = http_fixture.IntegrationHTTPTests._patch
    request = http_fixture.IntegrationHTTPTests.request
    rpc = http_fixture.IntegrationHTTPTests.rpc
    message = staticmethod(http_fixture.IntegrationHTTPTests.message)
    rest_action = http_fixture.IntegrationHTTPTests.rest_action
    browser = http_fixture.IntegrationHTTPTests.browser
    assert_no_actions = http_fixture.IntegrationHTTPTests.assert_no_actions

    def key(self, *, explicit=True):
        self.server.api_access.configure({'enabled': True})
        body = {'name': 'business HTTP', 'role': 'readonly', 'scopes': list(TOOLS.values()),
                'allowed_ips': [], 'expires_days': 1}
        if explicit:
            body['tool_grants'] = list(TOOLS)
        return self.server.api_access.create_key(body)

    def test_real_http_list_rest_calls_capabilities_and_old_key_isolation(self):
        key = self.key()
        self.assertTrue(record_application_snapshot(self.server.state_dir, app_snapshot()))
        self.server._business_cached = downloads_snapshot()
        self.server._business_cached_epoch = self.server._business_epoch
        with mock.patch.object(vendor_media_snapshot, '_stamp', return_value=STAMP):
            self.assertTrue(record_media_snapshot(self.server.state_dir, media_fixture.batch_result())['stored'])
        forbidden = self._patch(http_fixture.web_config, 'collect_business_diagnostics',
                                side_effect=AssertionError('must not refresh business sources'))
        application = self._patch(http_fixture.web_config, 'collect_application_probe',
                                  side_effect=AssertionError('must not query native applications'))
        listed = self.rpc(key)[2]['result']['tools']
        self.assertEqual({tool['name'] for tool in listed}, set(TOOLS))
        for name in TOOLS:
            status, _, value = self.rpc(key, self.message('tools/call', {'name': name, 'arguments': {}}))
            self.assertEqual(status, 200)
            self.assertFalse(value['result']['isError'], value)
            self.assertTrue(value['result']['structuredContent']['available'])
            status, _, value = self.rest_action(key, name, {})
            self.assertEqual(status, 200)
            self.assertFalse(value['isError'], value)
            self.assertNotIn(PRIVATE, json.dumps(value))
        headers = {'Authorization': 'Bearer ' + key['token']}
        caps = self.request('GET', '/api/integration/capabilities', headers=headers)[2]
        self.assertTrue(all(row['status'] == 'available' for row in caps['domains']))
        old = self.key(explicit=False)
        self.assertEqual(self.rpc(old)[2]['result']['tools'], [])
        self.assertTrue(self.rest_action(old, 'get_app_status', {})[2]['isError'])
        self.assert_no_actions()
        forbidden.assert_not_called()
        application.assert_not_called()
        audit = self.server.api_access.snapshot()['audit']
        self.assertTrue(set(TOOLS) <= {row['operation'] for row in audit})
        self.assertNotIn(PRIVATE, json.dumps(audit))
        self.assertNotIn(key['token'], json.dumps(audit))

    def test_owner_explicit_upgrade_and_single_domain_grants_match_lists_and_calls(self):
        old = self.key(explicit=False)
        status, _, upgraded = self.browser('POST', '/api/access/keys/update',
            {'id': old['id'], 'scopes': ['apps'], 'tool_grants': ['get_app_status']})
        self.assertEqual(status, 200, upgraded)
        self.assertEqual(upgraded['key']['policy_version'], 3)
        self.assertEqual([row['name'] for row in self.rpc(old)[2]['result']['tools']], ['get_app_status'])
        for name, scope in TOOLS.items():
            status, _, created = self.browser('POST', '/api/access/keys', {
                'name': 'single ' + scope, 'role': 'readonly', 'scopes': [scope],
                'tool_grants': [name], 'allowed_ips': [], 'expires_days': 1})
            self.assertEqual(status, 200, created)
            key = dict(created['key'], token=created['token'])
            self.assertEqual([row['name'] for row in self.rpc(key)[2]['result']['tools']], [name])
            for other in TOOLS:
                result = self.rest_action(key, other, {})[2]
                self.assertEqual(result['isError'], other != name, (name, other, result))
            self.assertTrue(self.rest_action(key, 'get_media_status', {})[2]['isError'])
        self.assert_no_actions()

    def test_http_read_discarded_after_concurrent_revoke_rotation_disable_scope_edit_or_expiry(self):
        for change in ('revoke', 'rotate', 'disable', 'scope_edit', 'expire', 'service_off'):
            key = self.key()
            entered, release = threading.Event(), threading.Event()
            def read(unused):
                entered.set()
                self.assertTrue(release.wait(3))
                return {'ok': True, 'data': PRIVATE}
            with self.subTest(change=change), mock.patch.object(self.server.integration, '_read_business', side_effect=read):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.rest_action, key, 'get_download_status', {})
                    self.assertTrue(entered.wait(3))
                    if change == 'revoke':
                        self.server.api_access.revoke_key(key['id'])
                    elif change == 'rotate':
                        self.server.api_access.rotate_key(key['id'])
                    elif change == 'disable':
                        self.server.api_access.set_key_enabled(key['id'], False)
                    elif change == 'scope_edit':
                        self.server.api_access.update_key({'id': key['id'], 'scopes': ['apps'],
                                                          'tool_grants': ['get_app_status']})
                    elif change == 'expire':
                        self.now += 86401
                    else:
                        self.server.api_access.configure({'enabled': False})
                    release.set()
                    status, _, value = future.result(timeout=5)
                self.assertEqual(status, 200)
                self.assertTrue(value['isError'])
                self.assertEqual(value['structuredContent']['error']['code'], 'invalid_key')
                self.assertNotIn(PRIVATE, json.dumps(value))


if __name__ == '__main__':
    unittest.main()
