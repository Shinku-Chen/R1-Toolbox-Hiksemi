"""Exercise the local demo (fake NAS + real web panel + real small-screen UI)."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'tools' / 'demo'


def load_demo():
    sys.path.insert(0, str(DEMO))
    spec = importlib.util.spec_from_file_location('r1_demo_server', DEMO / 'server.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['r1_demo_server'] = module
    spec.loader.exec_module(module)
    return module


demo_module = load_demo()


def request(url, *, method='GET', body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw
        finally:
            error.close()


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = demo_module.create_demo(screen_port=0, web_port=0, data_dir=tempfile.mkdtemp(prefix='r1-demo-test-'))
        cls.screen = 'http://127.0.0.1:%d' % cls.demo.screen.server_port
        cls.web = 'http://127.0.0.1:%d' % cls.demo.web.port

    @classmethod
    def tearDownClass(cls):
        cls.demo.stop()

    def action(self, body, token=True):
        headers = {'X-R1-Token': self.demo.token} if token else {}
        return request(self.screen + '/api/action', method='POST', body=body, headers=headers)

    def web_request(self, path, *, method='GET', body=None):
        headers = {'Host': '127.0.0.1:%d' % self.demo.web.port,
                   'Origin': 'http://127.0.0.1:%d' % self.demo.web.port}
        return request(self.web + path, method=method, body=body, headers=headers)

    def test_small_screen_assets_and_snapshots(self):
        status, html = request(self.screen + '/')
        self.assertEqual(status, 200)
        self.assertIn('R1_TOOLBOX_CONTROL_TOKEN', html.decode('utf-8'))
        self.assertIn("script-src 'self' 'unsafe-inline'", html.decode('utf-8'))
        self.assertEqual(request(self.screen + '/assets/panel.js')[0], 200)
        for path in ('/api/status', '/api/toolbox', '/api/insights', '/api/fan-policy',
                     '/api/light-policy', '/api/monitoring', '/api/web-config', '/api/history?range=1h'):
            code, value = request(self.screen + path)
            self.assertEqual(code, 200, path)
            self.assertIsInstance(value, dict)
        self.assertEqual(request(self.screen + '/api/history?range=2h')[0], 400)
        self.assertNotEqual(request(self.screen + '/../runtime/web_config.py')[0], 200)

    def test_small_screen_quick_fan_and_light_actions(self):
        code, value = self.action({'action': 'fan.policy.quick', 'channel': 'cpu', 'mode': 'low'})
        self.assertEqual(code, 200)
        self.assertEqual(value['fan_policy']['config']['channels']['cpu']['manual_percent'], 50)
        code, value = self.action({'action': 'light.policy.quick', 'hue': 120, 'brightness': 40, 'effect': 2})
        self.assertEqual(code, 200)
        self.assertEqual(value['light_policy']['config']['manual'], {'mode': 2, 'color': '#00FF00', 'brightness': 40})

    def test_action_requires_token_and_updates_both_panels(self):
        code, _ = self.action({'action': 'screen.brightness', 'value': 66}, token=False)
        self.assertEqual(code, 403)
        code, value = self.action({'action': 'screen.brightness', 'value': 66})
        self.assertEqual(code, 200)
        self.assertEqual(value['state']['screen']['brightness'], 66)
        code, r1 = self.web_request('/api/r1')
        self.assertEqual(code, 200)
        self.assertEqual(r1['control']['screen']['brightness'], 66)
        code, overview = self.web_request('/api/overview')
        self.assertEqual(code, 200)
        self.assertEqual(overview['system']['hostname'], 'R1-DEMO')

    def test_pairing_from_the_small_screen_unlocks_the_web_panel(self):
        code, value = self.action({'action': 'web.pairing', 'rotate': True})
        self.assertEqual(code, 200)
        pairing = value['web']['code']
        self.assertRegex(pairing, r'^\d{5}$')
        status, headers = None, None
        req = urllib.request.Request(self.web + '/api/pair', data=json.dumps({'code': pairing}).encode(),
                                     method='POST', headers={'Host': '127.0.0.1:%d' % self.demo.web.port,
                                                             'Origin': 'http://127.0.0.1:%d' % self.demo.web.port,
                                                             'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            status, headers = response.status, response.headers
        self.assertEqual(status, 200)
        cookie = headers.get('Set-Cookie', '').split(';', 1)[0]
        req = urllib.request.Request(self.web + '/api/session', headers={'Host': '127.0.0.1:%d' % self.demo.web.port,
                                                                         'Cookie': cookie})
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_fan_rules_flow_to_the_read_only_small_screen(self):
        code, snapshot = request(self.screen + '/api/fan-policy')
        config = snapshot['config']
        config['channels']['cpu']['mode'] = 'manual'
        config['channels']['cpu']['manual_percent'] = 75
        config['channels']['system']['mode'] = 'vendor'
        code, value = self.action({'action': 'fan.policy.update',
                                   'config': config, 'apply': True, 'confirmed': True})
        self.assertEqual(code, 200, value)
        self.assertTrue(value['ok'])
        self.assertEqual(value['fan_policy']['channels']['cpu']['target_percent'], 75)
        code, snapshot = request(self.screen + '/api/fan-policy')
        self.assertEqual(snapshot['channels']['cpu']['target_percent'], 75)
        self.assertEqual(snapshot['config']['channels']['cpu']['manual_percent'], 75)


class DemoNativeFanModesTests(unittest.TestCase):
    def test_native_system_mode_applies_without_owning_system_and_preserves_cpu(self):
        nas = demo_module.FakeNas()
        config = nas.fan_snapshot()['config']
        config['channels']['cpu'].update(mode='manual', manual_percent=73)
        config['channels']['system'].update(mode='vendor', vendor_mode='high')
        saved = nas.fan_action('fan.policy.update', {'config': config, 'apply': False})
        self.assertEqual(saved['fan_policy']['official']['mode'], 'auto')
        self.assertTrue(saved['fan_policy']['pending_changes'])
        applied = nas.fan_action('fan.policy.update', {'config': config, 'apply': True})['fan_policy']
        self.assertEqual(applied['official']['mode'], 'high')
        self.assertTrue(applied['channels']['cpu']['active'])
        self.assertEqual(applied['channels']['cpu']['target_percent'], 73)
        self.assertFalse(applied['channels']['system']['active'])
        self.assertFalse(applied['pending_changes'])
        nas.official['mode'] = 'low'
        nas.fan_action('fan.policy.quick', {'channel': 'cpu', 'mode': 'medium'})
        self.assertEqual(nas.official['mode'], 'low')
        nas.fan_action('fan.policy.release', {'confirmed': True})
        self.assertEqual(nas.official['mode'], 'low')
        self.assertNotIn('vendor_mode', nas.fan_config['channels']['system'])

    def test_demo_rejects_native_cpu_modes_and_unknown_system_modes(self):
        for channel, mode in (('cpu', 'high'), ('system', 'stop')):
            with self.subTest(channel=channel, mode=mode):
                nas = demo_module.FakeNas()
                config = nas.fan_snapshot()['config']
                config['channels'][channel]['vendor_mode'] = mode
                self.assertFalse(nas.fan_action('fan.policy.update', {'config': config, 'apply': True})['ok'])
                self.assertEqual(nas.official['mode'], 'auto')


if __name__ == '__main__':
    unittest.main()
