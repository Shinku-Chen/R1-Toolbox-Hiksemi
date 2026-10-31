"""Real owner HTTP and CSRF gates around an isolated memory-only SMART worker."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import web_config
import insights as insights_module

ROUTE = '/api/r1/storage/smart/refresh'
SECRET = 'private-smart-command-stderr-sentinel'


class RefreshError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(SECRET)


class MemoryInsights:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.error = None
        self.lock = threading.Lock()
        self.value = {'ok': True, 'force': False, 'status': 'complete',
                      'started_at': 1793416300.0, 'finished_at': 1793416301.0,
                      'results': [{'device': 'sda', 'status': 'skipped_standby',
                                   'reason': 'standby', 'sampled_at': None}]}

    def snapshot(self):
        return {'disks': {'available': False, 'items': []}}

    def refresh_smart(self, force=False, authorizer=None):
        if not self.lock.acquire(blocking=False):
            raise RefreshError('busy')
        try:
            self.calls.append((force, authorizer))
            assert authorizer() is True
            if self.hook:
                self.hook()
            if self.error:
                raise self.error
            value = copy.deepcopy(self.value)
            value['force'] = force
            return value
        finally:
            self.lock.release()


class SmartRefreshHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-smart-refresh-http-')
        self.addCleanup(self.temp.cleanup)
        controller = mock.Mock(state_dir=Path(self.temp.name))
        controller.snapshot.return_value = {
            'settings': {}, 'capabilities': {}, 'screen': {}, 'rgb': {}}
        self.insights = MemoryInsights()
        with mock.patch.dict(os.environ, {'R1_WEB_AUTH_MODE': 'password',
                                         'R1_DEV_NO_PAIRING': '0'}):
            self.server = web_config.WebConfigServer(
                controller, insights=self.insights, host='127.0.0.1', port=0).start()
        self.addCleanup(self.server.stop)
        self.cookie = None

    def request(self, body=None, *, method='POST', path=ROUTE, headers=None):
        host = '127.0.0.1:' + str(self.server.port)
        fields = {'Host': host, 'Origin': 'http://' + host, 'X-FNM-Request': '1',
                  'Content-Type': 'application/json'}
        if self.cookie:
            fields['Cookie'] = self.cookie
        fields.update(headers or {})
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=4)
        try:
            conn.request(method, path, encoded if method == 'POST' else None, fields)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data)
        finally:
            conn.close()

    def login(self):
        result = self.server.auth.setup('smart-refresh-fixture-password')
        self.assertTrue(result['ok'])
        self.cookie = web_config.AUTH_COOKIE + '=' + result['sid']

    def revoke(self):
        self.server.auth.revoke(self.server.auth.cookie_sid(self.cookie))

    def test_open_and_password_owners_receive_actual_batch_receipt(self):
        for password in (False, True):
            if password:
                self.login()
            for force in (False, True):
                with self.subTest(password=password, force=force):
                    status, value = self.request({'force': force})
                    self.assertEqual(status, 200, value)
                    self.assertIs(value['force'], force)
                    self.assertEqual(value['results'][0]['status'], 'skipped_standby')
                    self.assertEqual(self.insights.calls[-1][0], force)
                    self.assertTrue(self.insights.calls[-1][1]())

    def test_pair_owner_uses_existing_session_without_new_machine_authority(self):
        self.server.auth_mode = 'pair'
        token = 'p' * 43
        with self.server.lock:
            self.server.sessions[hashlib.sha256(token.encode()).digest()] = {
                'csrf': 'c' * 43, 'until': self.server.clock() + 120}
        self.cookie = web_config.COOKIE + '=' + token
        self.assertEqual(self.request({'force': False})[0], 200)

    def test_password_session_required_and_bearer_never_inherits_owner(self):
        self.login()
        self.cookie = None
        self.assertIn(self.request({'force': False})[0], (401, 403))
        self.cookie = web_config.AUTH_COOKIE + '=' + self.server.auth.login(
            'smart-refresh-fixture-password')['sid']
        self.assertEqual(self.request({'force': False},
            headers={'Authorization': 'Bearer fixture-key'})[0], 403)
        self.assertEqual(self.insights.calls, [])

    def test_developer_window_cannot_refresh_smart(self):
        self.server.auth_mode = 'pair'
        with mock.patch.object(self.server, '_dev_authorized', return_value=True):
            self.assertEqual(self.request({'force': False})[0], 403)
        self.assertEqual(self.insights.calls, [])

    def test_exact_boolean_only_no_path_or_command_selectors(self):
        bodies = ({}, [], None, True, {'force': 0}, {'force': 1},
                  {'force': None}, {'force': 'false'}, {'force': []},
                  {'force': False, 'device': 'sda'}, {'force': True, 'path': '/dev/sda'},
                  {'force': False, 'command': 'smartctl'}, {'force': False, 'confirm': True},
                  b'{"force":false,"force":true}', b'{"force":NaN}',
                  b'{"force":false}\n{}')
        for body in bodies:
            with self.subTest(body=body):
                self.assertIn(self.request(body)[0], (400, 415))
        self.assertEqual(self.insights.calls, [])

    def test_origin_json_marker_and_url_query_guards(self):
        for headers in ({'Origin': 'http://foreign.invalid'}, {'Origin': ''},
                        {'X-FNM-Request': ''}, {'Content-Type': 'text/plain'}):
            self.assertIn(self.request({'force': False}, headers=headers)[0], (400, 403, 415))
        for suffix in ('?force=true', '?device=sda', '#force=true'):
            self.assertEqual(self.request({'force': False}, path=ROUTE + suffix)[0], 400)
        self.assertEqual(self.insights.calls, [])

    def test_get_cannot_trigger_a_refresh(self):
        self.assertIn(self.request(method='GET')[0], (400, 401, 403, 404, 405))
        self.assertEqual(self.insights.calls, [])

    def test_busy_is_409_and_never_replays(self):
        self.insights.lock.acquire()
        try:
            status, value = self.request({'force': True})
        finally:
            self.insights.lock.release()
        self.assertEqual((status, value['reason']), (409, 'busy'))
        self.assertEqual(self.insights.calls, [])

    def test_failure_is_fixed_and_does_not_echo_command_stderr(self):
        self.insights.error = RuntimeError('/private/' + SECRET)
        status, value = self.request({'force': True})
        self.assertEqual(status, 503)
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertNotIn('/private', json.dumps(value))

    def test_revocation_during_collection_discards_batch(self):
        self.login()
        self.insights.hook = self.revoke
        status, value = self.request({'force': False})
        self.assertEqual(status, 403)
        self.assertNotIn('results', value)
        self.assertFalse(self.insights.calls[0][1]())

    def test_revocation_during_serialization_discards_encoded_receipt(self):
        self.login()
        original = web_config.json.dumps
        def serialize(value, *args, **kwargs):
            text = original(value, *args, **kwargs)
            if isinstance(value, dict) and 'results' in value and 'force' in value:
                self.revoke()
            return text
        with mock.patch.object(web_config.json, 'dumps', side_effect=serialize):
            status, value = self.request({'force': True})
        self.assertEqual(status, 403)
        self.assertNotIn('results', value)

    def test_stop_start_invalidates_captured_owner_lifetime(self):
        status, _ = self.request({'force': False})
        self.assertEqual(status, 200)
        current = self.insights.calls[-1][1]
        self.assertTrue(current())
        self.server.stop()
        self.assertFalse(current())
        self.server.start()
        self.assertFalse(current())

    def test_real_collector_http_refresh_and_dated_detail_share_one_cache(self):
        root = Path(self.temp.name) / 'collector'
        proc, sysfs = root / 'proc', root / 'sys'
        proc.mkdir(parents=True)
        folder = sysfs / 'block/sda'
        for name, value in {'dev': '8:0', 'size': '1000000',
                            'queue/rotational': '1', 'wwid': 'fixture-disk-a',
                            'device/model': 'Fixture HDD'}.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + '\n', encoding='ascii')
        now, calls = [100.0], []
        def command(argv, timeout):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stderr=SECRET, stdout=json.dumps({
                'smart_status': {'passed': True}, 'temperature': {'current': 37},
                'ata_smart_attributes': {'table': [
                    {'id': 194, 'name': 'Temperature_Celsius', 'raw': {'value': 37}}]}}))
        storage = mock.Mock()
        storage.snapshot.return_value = {
            'available': False, 'disks': [], 'spaces': [], 'reason': 'fixture'}
        collector = insights_module.InsightsCollector(
            proc, sysfs, runner=command, command_finder=lambda name: (
                '/fixture/smartctl' if name == 'smartctl' else None),
            clock=lambda: now[0], page_size=4096, clock_ticks=100,
            ipv4_provider=lambda: {}, storage_client=storage)
        self.addCleanup(collector.stop)
        collector._publish_integration('disks', collector._disks)
        self.assertEqual(calls, [])
        storage.snapshot.reset_mock()
        self.server.insights = collector
        self.server.panel = web_config.FnmonitorPanel(
            self.server.controller, collector, None, None, None, None,
            state_dir=root / 'panel', proc_root=proc, sys_root=sysfs,
            dev_root=root / 'dev', udev_root=root / 'run/udev', clock=lambda: now[0])

        status, receipt = self.request({'force': False})
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt['results'][0]['status'], 'refreshed')
        self.assertEqual(calls, [['/fixture/smartctl', '-n', 'standby', '-j', '-a', '/dev/sda']])
        storage.snapshot.assert_not_called()
        status, detail = self.request(method='GET', path='/api/smart?dev=sda')
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail['health'], 'PASS')
        self.assertTrue(detail['fresh'])
        self.assertEqual(detail['source'], 'manual_smartctl_cache')
        now[0] += 31
        status, stale = self.request(method='GET', path='/api/smart?dev=sda')
        self.assertEqual(status, 200, stale)
        self.assertFalse(stale['fresh'])
        self.assertIsNone(stale['health'])
        self.assertIsNone(stale['temp'])
        self.assertEqual(stale['sampled_at'], detail['sampled_at'])
        self.assertEqual(stale['attributes'], [])
        self.assertEqual(len(calls), 1)
        storage.snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
