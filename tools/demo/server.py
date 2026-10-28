"""Local demo: run the real web panel and small-screen UI against a fake NAS.

Nothing here is shipped in the image.  It exists so the panels can be opened,
clicked and debugged on a normal computer (including over the LAN) without the
NAS, Docker or any hardware access.
"""

import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'runtime'
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))
if str(ROOT / 'tools' / 'demo') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tools' / 'demo'))

from fake_nas import FakeNas, DemoController, DemoInsights, DemoFanPolicy, DemoLightPolicy, DemoMonitor, DemoMonitoring  # noqa: E402
from web_config import WebConfigServer  # noqa: E402


ASSETS = ROOT / 'ui' / 'assets'
INDEX = ROOT / 'ui' / 'index.html'
PANEL = ROOT / 'ui' / 'web' / 'index.html'
MIME = {'.css': 'text/css; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
        '.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png'}


def _token_script(token):
    return ('<script>window.require=function(name){if(name==="process")'
            'return {env:{R1_TOOLBOX_CONTROL_TOKEN:"%s"}};'
            'return {ipcRenderer:{once:function(){},send:function(){}}};};</script>' % token)


class _ScreenHandler(BaseHTTPRequestHandler):
    server_version = 'R1Demo'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    @property
    def demo(self):
        return self.server.demo

    def log_message(self, *args):
        pass

    def _send(self, status, data, mime='application/json; charset=utf-8', extra=None):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-R1-Token')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def do_OPTIONS(self):
        self._send(204, b'')

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        nas = self.demo.nas
        if path in ('/', '/index.html'):
            try:
                html = INDEX.read_text(encoding='utf-8')
            except OSError:
                self._json(503, {'error': '缺少 ui/index.html'})
                return
            html = html.replace('</head>', _token_script(self.demo.token) + '</head>')
            self._send(200, html, 'text/html; charset=utf-8')
            return
        if path.startswith('/assets/'):
            name = path[len('/assets/'):]
            if '/' in name or '\\' in name or not name:
                self._json(404, {'error': 'not found'})
                return
            target = ASSETS / name
            if not target.is_file() or target.is_symlink():
                self._json(404, {'error': 'not found'})
                return
            self._send(200, target.read_bytes(), MIME.get(target.suffix, 'application/octet-stream'))
            return
        if path == '/api/status':
            self._json(200, nas.monitor())
            return
        if path == '/api/toolbox':
            self._json(200, nas.toolbox())
            return
        if path == '/api/insights':
            self._json(200, nas.insights())
            return
        if path == '/api/fan-policy':
            self._json(200, nas.fan_snapshot())
            return
        if path == '/api/light-policy':
            self._json(200, nas.light_snapshot())
            return
        if path == '/api/monitoring':
            self._json(200, nas.monitoring())
            return
        if path == '/api/web-config':
            self._json(200, self.demo.web.snapshot())
            return
        if path == '/favicon.ico':
            self._send(204, b'')
            return
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path != '/api/action':
            self._json(404, {'error': 'not found'})
            return
        if self.headers.get('X-R1-Token') != self.demo.token:
            self._json(403, {'ok': False, 'error': '演示控制令牌无效'})
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            self._json(415, {'ok': False, 'error': '需要 JSON'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 65536:
            self._json(413, {'ok': False, 'error': '请求长度无效'})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeError):
            self._json(400, {'ok': False, 'error': '请求格式无效'})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get('action'), str):
            self._json(400, {'ok': False, 'error': '缺少操作名称'})
            return
        action = payload.pop('action')
        nas = self.demo.nas
        if action == 'web.pairing':
            try:
                value = self.demo.web.pairing_info(rotate=payload.get('rotate') is True)
            except Exception:
                self._json(400, {'ok': False, 'error': '演示网页服务未启动'})
                return
            self._json(200, {'ok': True, 'web': value})
            return
        if action in ('fan.policy.update', 'fan.policy.release'):
            result = self.demo.fan_policy.action(action, payload)
        elif action == 'light.policy.update':
            result = self.demo.light_policy.action(action, payload)
        elif action == 'monitor.settings.update':
            result = self.demo.monitoring.action(action, payload)
        elif action in ('container.action', 'vm.action'):
            result = self.demo.insights.action(action, payload)
        else:
            result = self.demo.controller.action(action, payload)
        result = dict(result or {})
        result.setdefault('ok', True)
        result.setdefault('state', nas.toolbox())
        result.setdefault('insights', nas.insights())
        self._json(200 if result.get('ok') is not False else 400, result)


class _ScreenServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, demo, token):
        self.demo = demo
        self.token = token
        super().__init__(address, _ScreenHandler)


class Demo:
    def __init__(self, nas, web, screen, token):
        self.nas = nas
        self.web = web
        self.screen = screen
        self.token = token
        self.controller = web.controller
        self.insights = web.insights
        self.fan_policy = web.fan_policy
        self.light_policy = web.light_policy
        self.monitoring = web.monitoring

    @property
    def screen_url(self):
        return 'http://127.0.0.1:%d/' % self.screen.server_port

    @property
    def web_url(self):
        return 'http://127.0.0.1:%d/' % self.web.port

    def stop(self):
        try:
            self.screen.shutdown()
            self.screen.server_close()
        except Exception:
            pass
        try:
            self.web.stop()
        except Exception:
            pass
        self.nas.stop()


def _lan_addresses():
    values = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith('127.') and not address.startswith('169.254.'):
                values.add(address)
    except OSError:
        pass
    return sorted(values)


def create_demo(host='127.0.0.1', screen_port=18731, web_port=18732, data_dir=None, token=None):
    """Build both servers and the shared fake NAS.  Ports may be 0 for tests."""
    if not PANEL.is_file():
        raise RuntimeError('缺少 ui/web/index.html，请先运行 python scripts/build-panel.py')
    token = token or secrets.token_hex(16)
    state_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix='r1-demo-state-'))
    nas = FakeNas().start()
    controller = DemoController(nas)
    insights = DemoInsights(nas)
    fan = DemoFanPolicy(nas)
    light = DemoLightPolicy(nas)
    monitor = DemoMonitor(nas)
    monitoring = DemoMonitoring(nas)
    web = WebConfigServer(controller, insights, fan_policy=fan, light_policy=light, monitoring=monitoring,
                          monitor=monitor, host=host, port=web_port, asset_dir=ROOT / 'ui' / 'web',
                          state_dir=state_dir, version='demo')
    web.start()
    demo = Demo(nas, web, None, token)
    screen = _ScreenServer((host, screen_port), demo, token)
    demo.screen = screen
    thread = threading.Thread(target=screen.serve_forever, kwargs={'poll_interval': 0.1},
                              name='r1-demo-screen', daemon=True)
    thread.start()
    demo.screen_thread = thread
    return demo
