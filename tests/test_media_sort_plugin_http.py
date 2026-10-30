"""Owner plugin HTTP gates with a memory-only backend, never a NAS database."""
import copy
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


BASE = '/api/plugins/media-sort'
PRIVATE = '/drives/fixture/scraper/owner-only.db'
CHANGE = {'candidate_id': 'candidate_fixture', 'version': 'version_fixture', 'confirmed': True}


class MemoryPlugin:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.error = None
        self.closed = False
        self.value = {'schema_version': 1, 'id': 'media_sort', 'status': 'unknown',
                      'busy': False, 'checked_at': None, 'message': None,
                      'candidates': [{'id': CHANGE['candidate_id'], 'version': CHANGE['version'],
                                      'database_path': PRIVATE, 'state': 'disabled'}]}

    def snapshot(self):
        self.calls.append(('snapshot',))
        if self.hook:
            self.hook()
        return copy.deepcopy(self.value)

    def probe(self, *, current):
        return self.change('probe', current=current)

    def change(self, action, *args, current, **kwargs):
        assert current() is True
        self.calls.append((action, args, kwargs, current))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return copy.deepcopy(self.value)

    def apply(self, *args, **kwargs):
        return self.change('apply', *args, **kwargs)

    def revert(self, *args, **kwargs):
        return self.change('revert', *args, **kwargs)

    def close(self):
        self.closed = True


class MediaSortHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-media-plugin-http-')
        self.addCleanup(self.temp.cleanup)
        self.plugin = MemoryPlugin()
        controller = mock.Mock(state_dir=Path(self.temp.name))
        controller.snapshot.return_value = {'settings': {}, 'capabilities': {}, 'screen': {}, 'rgb': {}}
        with mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'password', 'R1_DEV_NO_PAIRING': '0'}):
            self.server = web_config.WebConfigServer(controller, host='127.0.0.1', port=0,
                media_sort_plugin=self.plugin).start()
        self.addCleanup(self.server.stop)
        self.cookie = None

    def request(self, method='GET', path=BASE, body=None, *, headers=None):
        host = '127.0.0.1:' + str(self.server.port)
        fields = {'Host': host, 'Origin': 'http://' + host, 'X-FNM-Request': '1'}
        if self.cookie:
            fields['Cookie'] = self.cookie
        if body is not None:
            fields['Content-Type'] = 'application/json'
        fields.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=4)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, fields)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def login(self):
        result = self.server.auth.setup('media-plugin-fixture-password')
        self.assertTrue(result['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + result['sid']

    def revoke(self):
        self.server.auth.revoke(self.server.auth.cookie_sid(self.cookie))

    def test_start_and_cached_get_never_probe_or_apply(self):
        self.assertEqual(self.plugin.calls, [])
        status, value = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(value['may_wake_disks'])
        self.assertEqual(self.plugin.calls, [('snapshot',)])
        self.server.stop()
        self.assertTrue(self.plugin.closed)
        self.assertEqual(self.plugin.calls, [('snapshot',)])

    def test_open_owner_and_logged_in_owner_fixed_actions(self):
        for authenticated in (False, True):
            if authenticated:
                self.login()
            for action, body in (('probe', {}), ('apply', CHANGE), ('revert', CHANGE)):
                with self.subTest(authenticated=authenticated, action=action):
                    status, result = self.request('POST', BASE + '/' + action, body)
                    self.assertEqual(status, 200, result)
                    self.assertTrue(result['may_wake_disks'])
                    call = self.plugin.calls[-1]
                    self.assertEqual(call[0], action)
                    self.assertTrue(call[3]())
                    if action != 'probe':
                        self.assertEqual(call[1], (CHANGE['candidate_id'], CHANGE['version']))
                        self.assertEqual(call[2], {'confirmed': True})

    def test_strict_shapes_no_paths_sql_or_implicit_confirmation(self):
        for action in ('probe', 'apply', 'revert'):
            bodies = ([{'path': PRIVATE}, [], None] if action == 'probe' else
                      [{}, {**CHANGE, 'path': PRIVATE}, {**CHANGE, 'sql': 'SELECT PRIVATE'},
                       {**CHANGE, 'confirmed': False}, {**CHANGE, 'confirmed': 1},
                       {**CHANGE, 'version': '../db'}, {**CHANGE, 'candidate_id': 'x' * 129},
                       {**CHANGE, 'candidate_id': 5}])
            for body in bodies:
                with self.subTest(action=action, body=body):
                    self.assertIn(self.request('POST', BASE + '/' + action, body)[0], (400, 415))
        self.assertEqual(self.plugin.calls, [])

    def test_bearer_never_inherits_owner_or_developer_authority(self):
        self.login()
        for method, path, body in [('GET', BASE, None), ('POST', BASE + '/probe', {}),
                                   ('POST', BASE + '/apply', CHANGE), ('POST', BASE + '/revert', CHANGE)]:
            self.assertEqual(self.request(method, path, body,
                headers={'Authorization': 'Bearer fixture-unrelated'})[0], 403)
        self.assertEqual(self.plugin.calls, [])
        self.revoke()
        self.server.auth.set_enabled(False)
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            self.assertEqual(self.request()[0], 403)
            self.assertEqual(self.request('POST', BASE + '/probe', {})[0], 403)
        self.assertEqual(self.plugin.calls, [])

    def test_password_required_and_transport_guards(self):
        self.login()
        self.cookie = None
        self.assertIn(self.request()[0], (401, 403))
        self.assertIn(self.request('POST', BASE + '/probe', {})[0], (401, 403))
        # A password is not required to test same-origin and JSON gates.
        self.server.auth.set_enabled(False)
        for headers in ({'Origin': 'http://foreign.invalid'}, {'Origin': ''},
                        {'X-FNM-Request': ''}, {'Content-Type': 'text/plain'}):
            self.assertIn(self.request('POST', BASE + '/probe', {}, headers=headers)[0], (400, 403, 415))
        for suffix in ('?path=PRIVATE', '#PRIVATE'):
            self.assertEqual(self.request('POST', BASE + '/probe' + suffix, {})[0], 400)
        self.assertEqual(self.request(path=BASE + '?path=PRIVATE')[0], 400)
        self.assertEqual(self.plugin.calls, [])

    def test_revocation_during_read_and_write_discards_private_result(self):
        self.login()
        self.plugin.hook = self.revoke
        status, result = self.request()
        self.assertEqual(status, 403)
        self.assertNotIn(PRIVATE, json.dumps(result))
        session = self.server.auth.login('media-plugin-fixture-password')
        self.cookie = web_config.AUTH_COOKIE + '=' + session['sid']
        status, result = self.request('POST', BASE + '/apply', CHANGE)
        self.assertEqual(status, 403)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertFalse(self.plugin.calls[-1][3]())

    def test_revocation_during_json_serialization_discards_encoded_result(self):
        self.login()
        original = web_config.json.dumps

        def serialize(value, *args, **kwargs):
            raw = original(value, *args, **kwargs)
            if type(value) is dict and 'plugin' in value:
                self.revoke()
            return raw

        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, result = self.request()
        self.assertEqual(status, 403)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_fixed_error_projection_and_closed_current(self):
        class BackendError(RuntimeError):
            code = 'conflict'
        self.plugin.error = BackendError('SECRET-SQL-token ' + PRIVATE)
        status, result = self.request('POST', BASE + '/probe', {})
        self.assertEqual((status, result['code']), (409, 'conflict'))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.plugin.error = RuntimeError('SECRET ' + PRIVATE)
        status, result = self.request('POST', BASE + '/probe', {})
        self.assertEqual((status, result['code']), (503, 'operation_failed'))
        current = self.plugin.calls[-1][3]
        self.assertTrue(current())
        self.server.stop()
        self.assertFalse(current())
        self.assertTrue(self.plugin.closed)
        self.server.start()
        self.assertFalse(current())

    def test_restart_cannot_deliver_old_get_or_post_result(self):
        for method, path, body in (('GET', BASE, None), ('POST', BASE + '/probe', {})):
            with self.subTest(method=method):
                entered, release = threading.Event(), threading.Event()
                results, failures = [], []

                def wait():
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError('fixture did not release old request')

                def request():
                    try:
                        results.append(self.request(method, path, body))
                    except Exception as error:
                        failures.append(error)

                self.plugin.hook = wait
                thread = threading.Thread(target=request, daemon=True)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    self.server.stop()
                    self.server.start()
                finally:
                    release.set()
                    thread.join(4)
                    self.plugin.hook = None
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(results[0][0], 403)
                self.assertNotIn(PRIVATE, json.dumps(results[0][1]))


if __name__ == '__main__':
    unittest.main()
