"""Real owner HTTP gates around an isolated, memory-only capacity worker."""
import copy
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


ROUTE = '/api/r1/storage/capacity/refresh'
SECRET = 'private-capacity-filesystem-error-sentinel'


class RefreshError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('/private/' + SECRET)


class MemoryMonitor:
    """Model the source lock and final commit guard without touching a mount."""
    def __init__(self):
        self.calls = []
        self.commits = []
        self.hook = None
        self.error = None
        self.check_commit_authorization = True
        self.lock = threading.Lock()
        self.value = {
            'ok': True, 'status': 'complete',
            'started_at': 1793416300.0, 'finished_at': 1793416301.0,
            'results': [{'mount_id': 101, 'status': 'refreshed',
                         'reason': None, 'sampled_at': 1793416300.5}]}

    def snapshot(self):
        return {'disks': [], 'uptime_seconds': 1, 'cpu': {}, 'memory': {},
                'network': {}, 'settings': {}}

    def refresh_capacity(self, *, authorizer=None):
        if not self.lock.acquire(blocking=False):
            raise RefreshError('busy')
        try:
            self.calls.append(authorizer)
            if not callable(authorizer) or authorizer() is not True:
                raise RefreshError('authorization_denied')
            if self.hook is not None:
                self.hook(authorizer)
            if self.error is not None:
                raise self.error
            value = copy.deepcopy(self.value)
            if self.check_commit_authorization and authorizer() is not True:
                raise RefreshError('authorization_denied')
            self.commits.append(value)
            return value
        finally:
            self.lock.release()


class CapacityRefreshHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-capacity-refresh-http-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        controller = mock.Mock(state_dir=root)
        controller.snapshot.return_value = {
            'settings': {}, 'capabilities': {}, 'screen': {}, 'rgb': {}}
        self.monitor = MemoryMonitor()
        self.insights = mock.Mock()
        self.insights.snapshot.return_value = {
            'disks': {'available': True, 'items': []}, 'device': {}}
        with mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'password',
                                         'R1_DEV_NO_PAIRING': '0'}):
            self.server = web_config.WebConfigServer(
                controller, insights=self.insights, monitor=self.monitor,
                host='127.0.0.1', port=0).start()
        self.addCleanup(self.server.stop)
        # Passive GETs must use fixture proc/sys roots, never this host's mounts.
        self.server.panel = web_config.FnmonitorPanel(
            controller, self.insights, None, None, None, self.monitor,
            state_dir=root / 'panel', proc_root=root / 'proc', sys_root=root / 'sys',
            dev_root=root / 'dev', udev_root=root / 'run/udev')
        self.cookie = None

    def request(self, body=None, *, method='POST', path=ROUTE, headers=None):
        host = '127.0.0.1:' + str(self.server.port)
        fields = {'Host': host, 'Origin': 'http://' + host, 'X-FNM-Request': '1',
                  'Content-Type': 'application/json'}
        if self.cookie:
            fields['Cookie'] = self.cookie
        for key, value in (headers or {}).items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=4)
        try:
            connection.request(method, path, encoded if method == 'POST' else None, fields)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data)
        finally:
            connection.close()

    def login(self):
        result = self.server.auth.setup('capacity-refresh-fixture-password')
        self.assertTrue(result['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + result['sid']

    def pair(self):
        self.server.auth_mode = 'pair'
        token = 'p' * 43
        with self.server.lock:
            self.server.sessions[hashlib.sha256(token.encode()).digest()] = {
                'csrf': 'c' * 43, 'until': self.server.clock() + 120}
        self.cookie = web_config.COOKIE + '=' + token

    def revoke(self):
        self.server.auth.revoke(self.server.auth.cookie_sid(self.cookie))

    @staticmethod
    def capacity_receipt(value):
        return (isinstance(value, dict) and value.get('ok') is True
                and 'results' in value and 'started_at' in value and 'force' not in value)

    def assert_no_receipt(self, value):
        self.assertNotIn('results', value)
        self.assertNotIn('sampled_at', value)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_route_constant_and_open_owner_receive_exact_safe_receipt(self):
        self.assertEqual(web_config.CAPACITY_REFRESH_ROUTE, ROUTE)
        status, value = self.request({})
        self.assertEqual(status, 200, value)
        self.assertEqual(value, self.monitor.value)
        self.assertEqual(set(value), {'ok', 'status', 'started_at', 'finished_at', 'results'})
        self.assertEqual(set(value['results'][0]), {'mount_id', 'status', 'reason', 'sampled_at'})
        self.assertTrue(self.monitor.calls[0]())
        self.assertEqual(len(self.monitor.commits), 1)

    def test_password_and_pair_owner_sessions_are_accepted(self):
        self.login()
        self.assertEqual(self.request({})[0], 200)
        self.pair()
        self.assertEqual(self.request({}, headers={'X-FNM-Request': None,
                                                  'X-R1-CSRF': 'c' * 43})[0], 200)
        self.assertEqual(len(self.monitor.calls), 2)

    def test_non_owner_is_403_before_any_worker_call(self):
        self.login()
        self.cookie = None
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.calls, [])

    def test_developer_only_window_cannot_refresh_capacity(self):
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.calls, [])

    def test_bearer_never_inherits_open_password_or_pair_owner(self):
        for owner in ('open', 'password', 'pair'):
            if owner == 'password':
                self.login()
            elif owner == 'pair':
                self.pair()
            with self.subTest(owner=owner):
                status, value = self.request({}, headers={'Authorization': 'Bearer fixture-key'})
                self.assertEqual(status, 403, value)
                self.assert_no_receipt(value)
        self.assertEqual(self.monitor.calls, [])

    def test_body_is_exact_empty_object_without_selectors_or_duplicate_keys(self):
        bodies = (None, [], True, False, 0, 'refresh', {'force': False},
                  {'mount_id': 101}, {'path': '/private/' + SECRET},
                  {'command': SECRET}, {'confirm': True},
                  b'', b'{', b'{"force":false,"force":true}', b'{"value":NaN}',
                  b'{"value":Infinity}', b'{}\n{}')
        for body in bodies:
            with self.subTest(body=body):
                status, value = self.request(body)
                self.assertIn(status, (400, 415), value)
                self.assertNotIn(SECRET, json.dumps(value))
        self.assertEqual(self.monitor.calls, [])

    def test_same_origin_json_and_csrf_proofs_remain_required(self):
        for headers in ({'Origin': 'http://foreign.invalid'}, {'Origin': ''},
                        {'Origin': None}, {'X-FNM-Request': None},
                        {'X-FNM-Request': '0'}, {'Content-Type': 'text/plain'},
                        {'Content-Type': None}):
            with self.subTest(headers=headers):
                status, value = self.request({}, headers=headers)
                self.assertIn(status, (400, 403, 415), value)
        self.assertEqual(self.monitor.calls, [])

    def test_pair_csrf_cannot_replace_origin_or_json_proofs(self):
        self.pair()
        for fields in ({'Origin': 'http://foreign.invalid'},
                       {'Content-Type': 'text/plain'}, {'X-R1-CSRF': 'wrong'}):
            headers = {'X-FNM-Request': None, 'X-R1-CSRF': 'c' * 43, **fields}
            status, value = self.request({}, headers=headers)
            self.assertIn(status, (400, 403, 415), value)
        self.assertEqual(self.monitor.calls, [])

    def test_query_fragment_and_empty_delimiters_are_rejected(self):
        for suffix in ('?mount_id=101', '?force=false', '?', '#fragment', '#'):
            with self.subTest(suffix=suffix):
                status, value = self.request({}, path=ROUTE + suffix)
                self.assertEqual(status, 400, value)
        self.assertEqual(self.monitor.calls, [])

    def test_get_and_passive_cache_routes_never_trigger_refresh(self):
        self.assertIn(self.request(method='GET')[0], (400, 401, 403, 404, 405))
        for path in ('/api/overview', '/api/smart?dev=sda', '/api/r1'):
            status, value = self.request(method='GET', path=path)
            self.assertEqual(status, 200, value)
        self.assertEqual(self.monitor.calls, [])
        self.assertEqual(self.monitor.commits, [])

    def test_partial_receipt_keeps_each_fixed_mount_outcome(self):
        self.monitor.value['status'] = 'partial'
        self.monitor.value['results'].extend([
            {'mount_id': 102, 'status': 'failed', 'reason': 'timeout', 'sampled_at': None},
            {'mount_id': 103, 'status': 'unavailable', 'reason': 'unavailable', 'sampled_at': None},
            {'mount_id': 104, 'status': 'identity_changed',
             'reason': 'identity_changed', 'sampled_at': None}])
        status, value = self.request({})
        self.assertEqual(status, 200, value)
        self.assertEqual(value, self.monitor.value)
        for row in value['results']:
            self.assertIs(type(row['mount_id']), int)
            self.assertGreater(row['mount_id'], 0)
            self.assertEqual(set(row), {'mount_id', 'status', 'reason', 'sampled_at'})

    def test_busy_source_returns_409_without_replay(self):
        self.monitor.lock.acquire()
        try:
            status, value = self.request({})
        finally:
            self.monitor.lock.release()
        self.assertEqual((status, value.get('reason')), (409, 'busy'), value)
        self.assertEqual(self.monitor.calls, [])

    def test_concurrent_http_refresh_does_not_wait_and_replay(self):
        entered, release = threading.Event(), threading.Event()
        first = []
        def hold(authorizer):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture release timeout')
        self.monitor.hook = hold
        def collect():
            try:
                first.append(self.request({}))
            except Exception as error:
                first.append(error)
        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2), 'first request did not reach isolated worker')
            status, value = self.request({})
            self.assertEqual((status, value.get('reason')), (409, 'busy'), value)
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(first), 1)
        self.assertIsInstance(first[0], tuple)
        self.assertEqual(first[0][0], 200, first)
        self.assertEqual(len(self.monitor.calls), 1)
        self.assertEqual(len(self.monitor.commits), 1)

    def test_worker_errors_use_fixed_reasons_without_private_exception_text(self):
        cases = [('busy', 409, 'busy'), ('authorization_denied', 403, 'authorization_denied'),
                 ('timeout', 503, 'timeout'), ('stopping', 503, 'stopping'),
                 ('unavailable', 503, 'unavailable'),
                 ('invalid_measurement', 503, 'unavailable'), (SECRET, 503, 'unavailable')]
        for reason, expected_status, expected_reason in cases:
            with self.subTest(reason=reason):
                self.monitor.error = RefreshError(reason)
                status, value = self.request({})
                self.assertEqual((status, value.get('reason')),
                                 (expected_status, expected_reason), value)
                self.assertNotIn(SECRET, json.dumps(value))
                self.assertNotIn('/private/', json.dumps(value))
                self.assert_no_receipt(value)
        self.monitor.error = RuntimeError('/private/' + SECRET)
        status, value = self.request({})
        self.assertEqual(status, 503, value)
        self.assert_no_receipt(value)

    def test_unavailable_worker_does_not_fall_back_to_snapshot_sampling(self):
        self.server.monitor = object()
        status, value = self.request({})
        self.assertEqual((status, value.get('reason')), (503, 'unavailable'), value)
        self.assertEqual(self.monitor.calls, [])

    def test_revocation_during_collection_prevents_commit_and_response(self):
        self.login()
        self.monitor.hook = lambda authorizer: self.revoke()
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.commits, [])
        self.assertFalse(self.monitor.calls[0]())

    def test_owner_authorizer_latches_real_auth_loss_after_open_mode_returns(self):
        def expire_then_restore(authorizer):
            self.assertTrue(authorizer())
            result = self.server.auth.setup('capacity-refresh-fixture-password')
            self.assertTrue(result['ok'])
            self.assertTrue(self.server.auth.required())
            self.assertFalse(authorizer())
            self.server.auth.set_enabled(False)
            self.assertFalse(self.server.auth.required())
            self.assertEqual(self.server.panel_authorized({}, peer='127.0.0.1'), 'open')
            self.assertFalse(authorizer())
        self.monitor.hook = expire_then_restore
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.commits, [])
        self.assertFalse(self.monitor.calls[0]())
        self.monitor.hook = None
        self.assertEqual(self.request({})[0], 200)
        self.assertTrue(self.monitor.calls[-1]())

    def test_revocation_after_worker_return_discards_receipt(self):
        self.login()
        self.monitor.check_commit_authorization = False
        self.monitor.hook = lambda authorizer: self.revoke()
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(len(self.monitor.calls), 1)

    def test_revocation_before_json_encoding_never_serializes_worker_receipt(self):
        self.login()
        original_post, original_dumps = self.server.panel_post, web_config.json.dumps
        encoded = []
        def dispatch(*args, **kwargs):
            result = original_post(*args, **kwargs)
            self.revoke()
            return result
        def serialize(value, *args, **kwargs):
            if self.capacity_receipt(value):
                encoded.append(value)
            return original_dumps(value, *args, **kwargs)
        with mock.patch.object(self.server, 'panel_post', side_effect=dispatch), \
                mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(encoded, [])

    def test_revocation_during_json_encoding_discards_encoded_receipt(self):
        self.login()
        original = web_config.json.dumps
        encoded = []
        def serialize(value, *args, **kwargs):
            text = original(value, *args, **kwargs)
            if self.capacity_receipt(value):
                encoded.append(text)
                self.revoke()
            return text
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(len(encoded), 1)

    def test_auth_loss_and_restoration_during_encoding_cannot_revive_receipt(self):
        original = web_config.json.dumps
        def serialize(value, *args, **kwargs):
            text = original(value, *args, **kwargs)
            if self.capacity_receipt(value):
                self.assertTrue(self.server.auth.setup('capacity-refresh-fixture-password')['ok'])
                self.assertFalse(self.monitor.calls[0]())
                self.server.auth.set_enabled(False)
            return text
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertFalse(self.monitor.calls[0]())

    def test_restart_during_json_encoding_discards_previous_generation(self):
        original = web_config.json.dumps
        def serialize(value, *args, **kwargs):
            text = original(value, *args, **kwargs)
            if self.capacity_receipt(value):
                self.server.stop()
                self.server.start()
            return text
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertFalse(self.monitor.calls[0]())

    def test_restart_during_collection_cannot_reauthorize_old_request(self):
        def restart(authorizer):
            self.server.stop()
            self.server.start()
            self.assertFalse(authorizer())
        self.monitor.hook = restart
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.commits, [])

    def test_stop_start_never_revives_captured_owner_generation(self):
        self.assertEqual(self.request({})[0], 200)
        current = self.monitor.calls[0]
        self.assertTrue(current())
        # No guard call while stopped: generation binding, not an observed False,
        # must invalidate the old request after restart.
        self.server.stop()
        self.server.start()
        self.assertFalse(current())
        self.assertEqual(self.request({})[0], 200)
        self.assertTrue(self.monitor.calls[-1]())

    def test_stop_during_collection_discards_receipt(self):
        self.monitor.hook = lambda authorizer: self.server.stop()
        status, value = self.request({})
        self.assertEqual(status, 403, value)
        self.assert_no_receipt(value)
        self.assertEqual(self.monitor.commits, [])
        self.assertFalse(self.monitor.calls[0]())


if __name__ == '__main__':
    unittest.main()
