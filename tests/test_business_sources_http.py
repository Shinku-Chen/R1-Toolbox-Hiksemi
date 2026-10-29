"""Real HTTP coverage for passive business state and explicit native refresh."""
import concurrent.futures
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
import vendor_business
import vendor_app_diagnostics
import vendor_app_probe
from vendor_media_snapshot import collect_media_snapshot
from test_vendor_media_probe import SAFE_CREDENTIAL, Session


class BusinessSourcesHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='r1-business-sources-')
        self.addCleanup(directory.cleanup)
        self.state_dir = Path(directory.name) / 'web'
        with mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'password', 'R1_DEV_NO_PAIRING': '0'}):
            self.server = web_config.WebConfigServer(mock.Mock(state_dir=Path(directory.name)),
                host='127.0.0.1', port=0, state_dir=self.state_dir).start()
        self.addCleanup(self.server.stop)
        self.host = '127.0.0.1:' + str(self.server.port)
        self.origin = 'http://' + self.host
        self.snapshot = {'schema_version': 1, 'sampled_at': '2026-10-29T00:00:00+00:00',
                         'categories': {'applications': {'available': False, 'data': None}}}
        self.collector = self.enterContext(mock.patch.object(web_config, 'collect_business_diagnostics',
                                                           return_value=self.snapshot))
        self.apps = self.enterContext(mock.patch.object(web_config, 'collect_application_probe',
            return_value={'schema_version': 1, 'available': True, 'status': 'ok',
                          'source': 'histor.applycenter.get_apply_list',
                          'sampled_at': '2026-10-29T00:00:00+00:00', 'data': {'apps': []}}))
        self.media = self.enterContext(mock.patch.object(web_config, 'collect_media_batch',
            return_value={'schema_version': 1, 'results': [
                {'id': 'ocr', 'available': True, 'query_success': True, 'status': 'ok', 'reason': None,
                 'data': {'totalpic': 10, 'processedpic': 7, 'remainpic': 3,
                          'picture_completion_ratio': 0.7}, 'credential': 'must-not-save'}]}))

    def request(self, path='/api/business', body=None, *, headers=None, remove=()):
        values = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-FNM-Request': '1'}
        values.update(headers or {})
        for key in remove:
            values.pop(key, None)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                json.dumps(body).encode() if body is not None else None, values)
            response = connection.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
            return response.status, data
        finally:
            connection.close()

    def refresh(self, source='media', **kwargs):
        return self.request('/api/r1/business/refresh', {'source': source}, **kwargs)

    def test_passive_reads_are_cached_and_never_refresh_native_queries(self):
        self.assertEqual(self.request()[0], 200)
        status, value = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(value['cached'])
        self.collector.assert_called_once_with(state_dir=self.state_dir)
        self.apps.assert_not_called()
        self.media.assert_not_called()
        self.assertEqual(self.request('/api/business?refresh=1')[0], 400)

    def test_owner_can_explicitly_refresh_without_developer_mode(self):
        self.assertFalse(self.server.services_snapshot()['developer']['enabled'])
        status, value = self.refresh()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertTrue(value['cache']['stored'])
        self.media.assert_called_once_with()
        cached = collect_media_snapshot(self.state_dir)
        self.assertTrue(cached['available'])
        self.assertFalse(cached['data']['live'])
        ocr = next(row for row in cached['data']['results'] if row['id'] == 'ocr')
        self.assertEqual(ocr['data']['remainpic'], 3)
        self.assertNotIn('must-not-save', json.dumps(cached))

    def test_application_refresh_is_fixed_and_saves_a_passive_snapshot(self):
        with mock.patch.object(web_config, 'record_application_snapshot', return_value=True) as record:
            status, value = self.refresh('applications')
        self.assertEqual(status, 200)
        self.assertTrue(value['cache']['stored'])
        self.apps.assert_called_once_with()
        record.assert_called_once_with(self.state_dir, self.apps.return_value)
        self.media.assert_not_called()

    def test_partial_application_refresh_keeps_valid_names_through_snapshot_and_passive_business_http(self):
        transport = mock.Mock(return_value=(200, {'list': [
            {'module_name': '陌生插件 β', 'version': '1.0', 'status': 4},
            {'module_name': ''}, {'module_name': '/private/must-not-leak'}, None]}))
        self.apps.return_value = vendor_app_probe.collect_application_probe(
            session_reader=lambda **_: (Session(), SAFE_CREDENTIAL), transport=transport)
        status, refreshed = self.refresh('applications')
        self.assertEqual(status, 200)
        self.assertTrue(refreshed['result']['available'])
        self.assertEqual(refreshed['result']['status'], 'partial')
        self.assertTrue(refreshed['cache']['stored'])
        empty = lambda **_: {'available': False, 'status': 'unavailable', 'data': None}
        self.collector.side_effect = lambda **_: vendor_business.collect_business_diagnostics(
            applications=lambda **_: vendor_app_diagnostics.collect(self.state_dir, state_dir=self.state_dir),
            downloads=empty, media=empty,
            runtime=lambda **_: {'available': False, 'processes': [], 'listeners': []})
        for _ in range(2):
            status, passive = self.request()
            self.assertEqual(status, 200)
            apps = passive['snapshot']['categories']['applications']
            self.assertEqual(apps['status'], 'cached_partial')
            self.assertTrue(apps['data']['partial'])
            self.assertEqual(apps['data']['rejected_entries'], 3)
            self.assertEqual(apps['data']['rejections'], [
                {'code': 'unsupported_application_label', 'count': 2},
                {'code': 'unsupported_application_entry', 'count': 1}])
            self.assertEqual(apps['data']['apps'][0]['name'], '陌生插件 β')
            self.assertIsNone(apps['data']['apps'][0]['running'])
            self.assertNotIn('must-not-leak', json.dumps(passive))
        self.apps.assert_called_once_with()
        transport.assert_called_once()
        self.media.assert_not_called()
        self.assertEqual(self.collector.call_count, 1)

    def test_native_failure_is_saved_without_claiming_business_success(self):
        self.media.return_value = {'schema_version': 1, 'available': False, 'results': [
            {'id': 'ocr', 'available': False, 'query_success': False,
             'status': 'unavailable', 'reason': 'credential_unavailable', 'data': None}]}
        status, value = self.refresh()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertFalse(value['result']['available'])
        cached = collect_media_snapshot(self.state_dir)
        self.assertFalse(cached['available'])
        self.assertIsInstance(cached['data'], dict)
        self.assertIsNone(next(row for row in cached['data']['results'] if row['id'] == 'ocr')['data'])

    def test_cache_failure_does_not_repeat_or_hide_a_completed_query(self):
        with mock.patch.object(web_config, 'record_media_snapshot', return_value={
                'stored': False, 'status': 'cache_unavailable', 'reason': 'snapshot_not_saved'}):
            status, value = self.refresh()
        self.assertEqual(status, 200)
        self.assertTrue(value['ok'])
        self.assertFalse(value['cache']['stored'])
        self.media.assert_called_once_with()

    def test_only_fixed_sources_and_same_origin_csrf_can_refresh(self):
        route = '/api/r1/business/refresh'
        for body in ({}, {'source': 'downloads'}, {'source': 'media', 'token': 'bad'},
                     {'source': []}, {'source': {'action': 'scan'}}, {'source': True}):
            with self.subTest(body=body):
                self.assertEqual(self.request(route, body)[0], 400)
        for kwargs in ({'remove': ('Origin',)}, {'remove': ('X-FNM-Request',)},
                       {'headers': {'Origin': 'http://foreign.invalid'}}):
            self.assertEqual(self.refresh(**kwargs)[0], 403)
        self.assertEqual(self.request(route + '?action=scan', {'source': 'media'})[0], 400)
        self.apps.assert_not_called()
        self.media.assert_not_called()

    def test_bearer_and_developer_authorization_do_not_replace_web_owner(self):
        self.assertEqual(self.refresh(headers={'Authorization': 'Bearer fixture'})[0], 403)
        with mock.patch.object(self.server, 'panel_authorized', return_value='dev'):
            self.assertEqual(self.refresh()[0], 403)
        self.media.assert_not_called()

    def test_password_gates_reads_and_refreshes(self):
        login = self.server.auth.setup('fixture-business-password')
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.refresh()[0], 401)
        cookie = web_config.AUTH_COOKIE + '=' + login['sid']
        self.assertEqual(self.refresh(headers={'Cookie': cookie})[0], 200)

    def test_shared_lock_rejects_overlap_and_exception_releases_it(self):
        with self.server.media_probe_lock:
            self.assertEqual(self.refresh()[0], 409)
        with self.server.application_probe_lock:
            self.assertEqual(self.refresh('applications')[0], 409)
        self.media.side_effect = RuntimeError('/private/path?token=must-not-leak')
        status, value = self.refresh()
        self.assertEqual(status, 503)
        self.assertNotIn('must-not-leak', json.dumps(value))
        self.assertFalse(self.server.media_probe_lock.locked())

    def test_explicit_refresh_wins_over_an_older_inflight_passive_read(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def collect(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                started.set()
                if not release.wait(4):
                    raise TimeoutError('fixture barrier')
                return {'categories': {'media': {'available': False, 'data': None}}}
            return {'categories': {'media': collect_media_snapshot(self.state_dir)}}
        self.collector.side_effect = collect
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.request)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(self.refresh()[0], 200)
            finally:
                release.set()
            status, value = future.result(timeout=5)
        self.assertEqual(status, 200)
        ocr = next(row for row in value['snapshot']['categories']['media']['data']['results'] if row['id'] == 'ocr')
        self.assertEqual(ocr['data']['remainpic'], 3)
        self.assertEqual(self.collector.call_count, 2)
        self.assertTrue(self.request()[1]['snapshot']['categories']['media']['available'])


class BusinessSourceProjectionTests(unittest.TestCase):
    def test_failed_engine_details_and_media_cache_are_preserved_but_redacted(self):
        no_data = lambda **_: {'available': False, 'status': 'unavailable', 'data': None}
        download = {**no_data(), 'engines': [{'engine': 'qbittorrent', 'available': False,
                    'status': 'auth_required', 'reason': 'requires_auth', 'password': 'must-not-leak'}]}
        media = {**no_data(), 'status': 'cached_empty', 'data': {'captured_at': 'fixture',
                 'results': [{'id': 'ocr', 'available': False, 'data': None,
                              'reason': 'credential_unavailable', 'token': 'must-not-leak'}]}}
        state = vendor_business.collect_business_diagnostics(applications=no_data,
            downloads=lambda **_: download, media=lambda **_: media,
            runtime=lambda **_: {'available': False, 'processes': [], 'listeners': []})
        self.assertEqual(state['readable_sources'], 0)
        self.assertEqual(state['categories']['downloads']['engines'][0]['status'], 'auth_required')
        self.assertIsNotNone(state['categories']['media']['data'])
        self.assertNotIn('must-not-leak', json.dumps(state))


if __name__ == '__main__':
    unittest.main()
