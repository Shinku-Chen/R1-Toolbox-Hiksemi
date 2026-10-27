"""Exercise the real paired HTTP server without host hardware or credentials."""
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('web_config', ROOT / 'runtime/web_config.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
spec = importlib.util.spec_from_file_location('web_toolbox', ROOT / 'runtime/toolbox.py')
toolbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolbox)
spec = importlib.util.spec_from_file_location('web_device_info', ROOT / 'runtime/device_info.py')
device_info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device_info)
spec = importlib.util.spec_from_file_location('web_insights', ROOT / 'runtime/insights.py')
insights_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'device_info': device_info}):
    spec.loader.exec_module(insights_module)


class Controller:
    def __init__(self):
        self.settings = copy.deepcopy(toolbox.DEFAULTS)
        self.calls = []
        self.fail_apply = False
        self.fail_save = False
        self.fail_snapshot = False
        self.verified = True

    def snapshot(self):
        if self.fail_snapshot:
            raise RuntimeError('/secret/private-location')
        return {'settings': copy.deepcopy(self.settings), 'settings_error': None,
                'capabilities': {'screen': {'available': self.verified}, 'rgb': {'available': self.verified}},
                'screen': {'max_brightness': 100 if self.verified else None, 'brightness': 80, 'asleep': False},
                'rgb': {'mode': 1, 'color': '#112233', 'brightness': 70, 'bus': 8},
                'fans': {'private': 'must not be exposed'}, 'power': {'secret': 'must not be exposed'}}

    def action(self, action, payload):
        self.calls.append((action, payload))
        if self.fail_save:
            return {'ok': False, 'settings_saved': False, 'error': '/secret/data-volume write failed'}
        if action == 'settings.update':
            self.settings = toolbox.validated_settings(payload['settings'], self.settings)
        return {'ok': not self.fail_apply, 'settings_saved': action == 'settings.update', 'error': '/secret/command output'}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.now = 1000.0
        self.web = web.WebConfigServer(self.controller, host='127.0.0.1', port=0,
                                       asset_dir=ROOT / 'ui/web', clock=lambda: self.now).start()
        self.addCleanup(self.web.stop)
        self.host = f'127.0.0.1:{self.web.port}'
        self.origin = 'http://' + self.host
        self.cookie = self.csrf = None

    def request(self, method, path, body=None, extra=None, origin=True):
        headers = {'Host': self.host}
        if self.cookie:
            headers['Cookie'] = self.cookie
        if origin:
            headers['Origin'] = self.origin
        if body is not None:
            body = json.dumps(body).encode() if not isinstance(body, bytes) else body
            headers['Content-Type'] = 'application/json'
        if extra:
            headers.update(extra)
        connection = http.client.HTTPConnection('127.0.0.1', self.web.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            values = dict(response.getheaders())
            return response.status, values, json.loads(raw) if 'application/json' in values.get('Content-Type', '') else raw
        finally:
            connection.close()

    def login(self):
        pairing = self.web.pairing_info(rotate=True)
        status, headers, _ = self.request('POST', '/api/pair', {'code': pairing['code']})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        status, _, value = self.request('GET', '/api/session')
        self.assertEqual(status, 200)
        self.csrf = value['csrf']
        return pairing['code'], headers

    def action(self, action='settings.update', payload=None, extra=None):
        headers = {'X-R1-CSRF': self.csrf or ''}
        if extra:
            headers.update(extra)
        return self.request('POST', '/api/action', {'action': action, 'payload': payload or {'settings': {'font_scale': 120}}}, headers)

    def test_only_local_explicit_pairing_returns_a_code(self):
        self.assertNotIn('code', self.web.snapshot())
        self.assertNotIn('code', self.web.pairing_info())
        pairing = self.web.pairing_info(rotate=True)
        self.assertRegex(pairing['code'], r'^[0-9]{10}$')
        self.assertEqual(pairing['expires_in'], 180)
        self.assertNotIn(pairing['code'], json.dumps(self.web.snapshot()))
        for route in ('/api/session', '/api/pair', '/api/web-config', '/api/toolbox'):
            self.assertEqual(self.request('GET', route)[0], 401)

    def test_single_use_code_cookie_csrf_logout_and_filtered_state(self):
        code, headers = self.login()
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn('Secure', cookie)  # Explicit HTTP LAN edition.
        self.assertEqual(self.request('POST', '/api/pair', {'code': code})[0], 403)
        state = self.request('GET', '/api/session')[2]['state']
        self.assertEqual(set(state), {'settings', 'settings_error', 'capabilities', 'screen', 'rgb'})
        self.assertNotIn('bus', state['rgb'])
        self.assertEqual(self.action()[0], 200)
        self.assertEqual(self.request('POST', '/api/logout', {}, {'X-R1-CSRF': self.csrf})[0], 200)
        self.assertEqual(self.request('GET', '/api/session')[0], 401)

    def test_expired_rotated_codes_and_expired_sessions(self):
        old = self.web.pairing_info(rotate=True)['code']
        new = self.web.pairing_info(rotate=True)['code']
        self.assertNotEqual(old, new)
        self.assertEqual(self.request('POST', '/api/pair', {'code': old})[0], 403)
        self.now += 181
        self.assertEqual(self.request('POST', '/api/pair', {'code': new})[0], 403)
        self.login()
        self.now += web.SESSION_TTL + 1
        self.assertEqual(self.action()[0], 401)

    def test_pairing_attempts_are_limited_and_window_expires(self):
        code = self.web.pairing_info(rotate=True)['code']
        for _ in range(5):
            self.assertEqual(self.request('POST', '/api/pair', {'code': 'incorrect'})[0], 403)
        self.assertEqual(self.request('POST', '/api/pair', {'code': code})[0], 429)
        self.now += 61
        self.assertEqual(self.request('POST', '/api/pair', {'code': code})[0], 200)

    def test_host_origin_and_csrf_are_independent_guards(self):
        code = self.web.pairing_info(rotate=True)['code']
        self.assertEqual(self.request('POST', '/api/pair', {'code': code}, origin=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/pair', {'code': code}, {'Origin': 'http://evil.invalid'})[0], 403)
        self.login()
        for headers in ({'Host': f'evil.invalid:{self.web.port}'}, {'Origin': 'null'}, {'Origin': 'http://evil.invalid'},
                        {'X-R1-CSRF': ''}, {'X-R1-CSRF': 'wrong'}, {'X-R1-CSRF': 'é'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.action(extra=headers)[0], 403)
        self.assertEqual(self.controller.calls, [])

    def test_forbidden_commands_payloads_and_unverified_devices_never_reach_controller(self):
        self.login()
        for action in ('fan.set', 'fan.official', 'power.request', 'container.action', 'vm.action', 'rgb.probe', 'web.pairing'):
            self.assertEqual(self.action(action, {'confirmed': True})[0], 400)
        self.assertEqual(self.action(payload={'settings': {'command': 'anything'}})[0], 400)
        self.assertEqual(self.action('rgb.set', {'bus': 4, 'mode': 1, 'color': '#112233'})[0], 400)
        self.controller.verified = False
        for name, payload in (('screen.brightness', {'value': 70}), ('screen.sleep', {}), ('rgb.set', {'mode': 1, 'color': '#112233'}),
                              ('settings.update', {'settings': {'night_enabled': True}})):
            self.assertEqual(self.action(name, payload)[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_json_limits_duplicates_and_no_arbitrary_file_route(self):
        self.login()
        for body in (b'{}' * 5000, b'{"code":"1","code":"2"}', b'{"x":NaN}', b'[]'):
            self.assertEqual(self.request('POST', '/api/pair', body)[0], 400)
        for route in ('/../runtime/web_config.py', '/config.js/../../settings.json', '/%2e%2e/settings.json', '/api/session?code=123'):
            self.assertIn(self.request('GET', route)[0], (400, 404))
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('GET', '/config.js')[0], 200)
        self.assertEqual(self.request('OPTIONS', '/api/action')[0], 405)

    def test_duplicate_security_headers_are_rejected(self):
        self.login()
        for name, value in (('Host', self.host), ('Origin', self.origin), ('Cookie', self.cookie),
                            ('Content-Length', '2'), ('Connection', 'close'), ('X-R1-CSRF', self.csrf)):
            with self.subTest(name=name):
                connection = http.client.HTTPConnection('127.0.0.1', self.web.port, timeout=3)
                try:
                    connection.putrequest('POST', '/api/logout', skip_host=True)
                    for key, item in [('Host', self.host), ('Origin', self.origin), ('Cookie', self.cookie),
                                      ('Content-Length', '2'), ('Connection', 'close'), ('X-R1-CSRF', self.csrf), (name, value)]:
                        connection.putheader(key, item)
                    connection.putheader('Content-Type', 'application/json')
                    connection.endheaders(b'{}')
                    self.assertEqual(connection.getresponse().status, 400)
                finally:
                    connection.close()

    def test_saved_but_not_applied_and_internal_errors_are_distinct(self):
        self.login()
        self.controller.fail_apply = True
        status, _, value = self.action()
        self.assertEqual(status, 400)
        self.assertTrue(value['saved'])
        self.assertFalse(value['applied'])
        self.assertNotIn('/secret', json.dumps(value))

    def test_same_value_does_not_claim_persistence_when_storage_write_fails(self):
        self.login()
        self.controller.fail_save = True
        status, _, value = self.action(payload={'settings': {'theme': self.controller.settings['theme']}})
        self.assertEqual(status, 400)
        self.assertFalse(value['saved'])
        self.assertFalse(value['applied'])
        self.assertNotIn('/secret', json.dumps(value))
        self.controller.fail_snapshot = True
        status, _, value = self.request('GET', '/api/session')
        self.assertEqual(status, 503)
        self.assertNotIn('/secret', json.dumps(value))

    def test_busy_action_is_rejected_without_queueing_or_controller_calls(self):
        self.login()
        with self.web.operation_lock:
            status, _, value = self.action()
        self.assertEqual(status, 409)
        self.assertIn('重新读取', value['error'])
        self.assertEqual(self.controller.calls, [])

    def test_concurrent_pairing_consumes_code_once_and_sessions_are_bounded(self):
        code = self.web.pairing_info(rotate=True)['code']
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.web._pair(code, '127.0.0.1')[1])) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=2)
        self.assertEqual(sorted(results), [200, 403])
        for index in range(12):
            code = self.web.pairing_info(rotate=True)['code']
            self.assertEqual(self.web._pair(code, f'192.0.2.{index}')[1], 200)
        self.assertEqual(self.web.snapshot()['session_count'], 8)

    def test_thread_limit_and_absolute_read_deadline(self):
        slots = self.web.server.slots
        for _ in range(8): self.assertTrue(slots.acquire(blocking=False))
        connection = socket.create_connection(('127.0.0.1', self.web.port), timeout=2)
        try:
            connection.sendall(b'GET / HTTP/1.0\r\n\r\n')
            try:
                received = connection.recv(100)
            except ConnectionResetError:
                received = b''
            self.assertEqual(received, b'')
        finally:
            connection.close()
            for _ in range(8): slots.release()
        handler = type('Handler', (), {'deadline': time.monotonic() - 1, 'connection': None})()
        with self.assertRaises(TimeoutError):
            web._Reader(None, handler).readline()

    def test_bind_or_thread_failure_is_inert_and_stop_revokes_sessions(self):
        occupied = web.WebConfigServer(Controller(), host='127.0.0.1', port=self.web.port)
        with self.assertRaises(OSError): occupied.start()
        self.assertFalse(occupied.snapshot()['enabled'])
        self.assertTrue(occupied.snapshot()['reason'])
        occupied.stop()
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('fixture failure')):
            failed = web.WebConfigServer(Controller(), host='127.0.0.1', port=0)
            with self.assertRaises(OSError): failed.start()
        self.assertFalse(failed.snapshot()['enabled'])
        failed.stop()
        self.login()
        old_cookie = self.cookie
        self.web.stop()
        self.web.start()
        self.assertIsNone(self.web._session(old_cookie)[1])

    def test_ipv4_addresses_use_link_state_and_actual_bound_port(self):
        folder = Path(tempfile.mkdtemp(prefix='r1-web-addresses-'))
        def preserve():
            trash = Path.home() / '.Trash'
            if sys.platform == 'darwin' and trash.is_dir():
                shutil.move(str(folder), str(trash / folder.name))
            else:
                print('测试临时目录已保留：', folder)
        self.addCleanup(preserve)
        proc, sysroot, etc, run = [folder / name for name in ('proc', 'sys', 'etc', 'run')]
        for path in (proc, sysroot, etc, run): path.mkdir()
        for name, state, carrier in (('eth0', 'up', '1'), ('eth1', 'down', '0')):
            network = sysroot / 'class/net' / name
            network.mkdir(parents=True)
            (network / 'operstate').write_text(state)
            (network / 'carrier').write_text(carrier)
        insights = insights_module.InsightsCollector(
            proc_root=proc, sys_root=sysroot, etc_root=etc, run_root=run,
            ipv4_provider=lambda: {'eth0': ['192.168.1.20', '127.0.0.1', '169.254.0.1', '::1'], 'eth1': ['192.168.2.20']},
            command_finder=lambda _: None, clock=lambda: self.now)
        insights._collect_fast()
        insights._state['device'] = device_info.collect_device_info(proc, sysroot, etc, run)
        actual = insights.snapshot()
        self.assertIn('interfaces', actual)
        self.assertNotIn('network', actual)
        self.assertEqual(actual['interfaces'][0]['operstate'], 'up')
        self.assertTrue(all('ipv4' not in interface for interface in actual['device']['network']))
        server = web.WebConfigServer(Controller(), insights=insights, host='0.0.0.0', port=0).start()
        try:
            self.assertEqual(server.snapshot()['addresses'], [f'http://192.168.1.20:{server.port}'])
            # A stale legacy shape must not override the real interfaces key.
            insights._state['network'] = [{'name': 'eth0', 'operstate': 'up', 'ipv4': ['192.168.99.20']}]
            self.assertEqual(server.snapshot()['addresses'], [f'http://192.168.1.20:{server.port}'])
        finally:
            server.stop()

    def test_real_controller_settings_survive_reconstruction_from_same_data_directory(self):
        folder = Path(tempfile.mkdtemp(prefix='r1-web-settings-'))
        def preserve():
            trash = Path.home() / '.Trash'
            if sys.platform == 'darwin' and trash.is_dir():
                shutil.move(str(folder), str(trash / folder.name))
            else:
                print('测试临时目录已保留：', folder)
        self.addCleanup(preserve)
        backlight = folder / 'sys/class/backlight/intel_backlight'
        backlight.mkdir(parents=True)
        (backlight / 'max_brightness').write_text('100')
        (backlight / 'brightness').write_text('80')
        controller = toolbox.ToolboxController(state_dir=folder / 'data', sys_root=folder / 'sys', dev_root=folder / 'dev')
        self.web.controller = controller
        self.login()
        self.assertEqual(self.action(payload={'settings': {'theme': 'jade', 'font_scale': 120, 'page_interval': 15}})[0], 200)
        self.assertEqual(self.action('screen.brightness', {'value': 65})[0], 200)
        recreated = toolbox.ToolboxController(state_dir=folder / 'data', sys_root=folder / 'sys', dev_root=folder / 'dev')
        self.web.controller = recreated
        self.assertEqual(recreated.settings['theme'], 'jade')
        self.assertEqual(recreated.settings['font_scale'], 120)
        self.assertEqual(recreated.settings['page_interval'], 15)
        self.assertEqual(recreated.settings['brightness'], 65)
        self.assertEqual((backlight / 'brightness').read_text(), '65')
        self.assertEqual(self.request('GET', '/api/session')[2]['state']['settings']['brightness'], 65)


if __name__ == '__main__':
    unittest.main()
