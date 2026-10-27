"""Paired LAN HTTP settings. Never accepts the Electron control credential."""
import copy
import hashlib
import http.client
import http.server
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import urllib.parse

SETTINGS = frozenset(('theme', 'font_scale', 'page_interval', 'sleep_timeout', 'brightness',
                      'night_enabled', 'night_start', 'night_end', 'night_brightness', 'enabled_pages'))
COOKIE = 'r1_web_session'
PAIR_TTL, SESSION_TTL = 180, 8 * 3600


class _Reader:
    """An absolute header/body deadline prevents byte-at-a-time slow readers."""
    def __init__(self, base, handler):
        self.base, self.handler, self.remaining = base, handler, 8192

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _timeout(self):
        left = self.handler.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('request deadline')
        self.handler.connection.settimeout(min(2, left))

    def readline(self, limit=-1):
        data = bytearray()
        while limit < 0 or len(data) < limit:
            if self.remaining <= 0:
                raise http.client.LineTooLong('headers')
            self._timeout()
            value = self.base.read(1)
            if not value:
                break
            self.remaining -= 1
            data.extend(value)
            if value == b'\n':
                break
        return bytes(data)

    def body(self, length):
        chunks = bytearray()
        while len(chunks) < length:
            self._timeout()
            data = self.base.read1(length - len(chunks))
            if not data:
                raise ValueError('incomplete body')
            chunks.extend(data)
        return bytes(chunks)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not log pairing material, cookies, request bodies or privileged errors.
        pass


class WebConfigServer:
    def __init__(self, controller, insights=None, *, fan_policy=None, host='0.0.0.0', port=18732,
                 asset_dir=None, clock=None):
        if host not in ('127.0.0.1', '0.0.0.0') or type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
            raise ValueError('网页监听只允许 127.0.0.1/0.0.0.0 和 1024–65535 端口')
        self.controller, self.insights = controller, insights
        self.fan_policy = fan_policy
        self.host, self.port = host, port
        self.asset_dir = Path(asset_dir) if asset_dir is not None else Path(__file__).resolve().parent.parent / 'frontend/web'
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.server = self.thread = None
        self.reason = None
        self.pair_hash, self.pair_until = None, 0
        self.sessions, self.attempts = {}, {}
        self.global_attempts = []

    def _expire(self):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items() if value['until'] > now}
        if self.pair_until <= now:
            self.pair_hash = None
        self.attempts = {key: [item for item in value if now - item < 60]
                         for key, value in self.attempts.items() if value and now - value[-1] < 60}
        self.global_attempts = [item for item in self.global_attempts if now - item < 60]

    def _addresses(self):
        if self.host == '127.0.0.1':
            return [f'http://127.0.0.1:{self.port}']
        values = set()
        if self.insights is not None:
            try:
                state = self.insights.snapshot()
                device = state.get('device', {})
                network = device.get('network', [])
                if isinstance(network, dict):
                    network = network.get('interfaces', [])
                links = {item.get('name'): item for item in network if isinstance(item, dict)}
                # InsightsCollector publishes fast IPv4 samples as interfaces;
                # device.network supplies slower link metadata and IPv6 only.
                interfaces = state.get('interfaces', state.get('network', []))
                network = list(network) + list(interfaces)
                for item in network:
                    if not isinstance(item, dict):
                        continue
                    link = links.get(item.get('name'), item)
                    if link.get('operstate', link.get('state')) != 'up' or link.get('carrier') == 0:
                        continue
                    for raw in item.get('ipv4', []):
                        raw = raw.get('address') if isinstance(raw, dict) else raw
                        try:
                            address = ipaddress.IPv4Interface(raw).ip
                        except (ValueError, TypeError):
                            continue
                        if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                            values.add(f'http://{address}:{self.port}')
            except Exception:
                pass
        return sorted(values)

    def snapshot(self):
        with self.lock:
            self._expire()
            return {'enabled': self.server is not None, 'host': self.host, 'port': self.port,
                    'scheme': 'http', 'addresses': self._addresses() if self.server else [],
                    'reason': self.reason, 'session_count': len(self.sessions),
                    'pairing': {'active': self.pair_hash is not None,
                                'expires_in': max(0, int(self.pair_until - self.clock())) if self.pair_hash else 0}}

    def pairing_info(self, rotate=False):
        """Call only from the local authenticated screen action, never a web GET."""
        with self.lock:
            if not self.server:
                raise ValueError('网页配置服务未启用')
            result = self.snapshot()
            if rotate is True:
                code = f'{secrets.randbelow(10 ** 10):010d}'
                self.pair_hash = hashlib.sha256(code.encode()).digest()
                self.pair_until = self.clock() + PAIR_TTL
                result = self.snapshot()
                result.update(code=code, expires_in=PAIR_TTL)
            return result

    def _pair(self, code, peer):
        with self.lock:
            self._expire()
            if self.server is None:
                return None, 403
            attempts = self.attempts.get(peer, [])
            if len(attempts) >= 5 or len(self.global_attempts) >= 30 or (peer not in self.attempts and len(self.attempts) >= 128):
                return None, 429
            self.attempts[peer] = attempts + [self.clock()]
            self.global_attempts.append(self.clock())
            digest = hashlib.sha256(code.encode()).digest() if isinstance(code, str) and re.fullmatch(r'[0-9]{10}', code) else b''
            if self.pair_hash is None or not secrets.compare_digest(digest, self.pair_hash):
                return None, 403
            self.pair_hash = None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(self.sessions) >= 8:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[hashlib.sha256(token.encode()).digest()] = {'csrf': csrf, 'until': self.clock() + SESSION_TTL}
            return token, 200

    def _session(self, raw):
        if not raw or len(raw) > 2048:
            return None, None
        pairs = [item.strip().partition('=') for item in raw.split(';')]
        tokens = [value for name, equal, value in pairs if name == COOKIE and equal]
        if len(tokens) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', tokens[0]):
            return None, None
        key = hashlib.sha256(tokens[0].encode()).digest()
        with self.lock:
            self._expire()
            return key, self.sessions.get(key) if self.server else None

    def _state(self):
        raw = self.controller.snapshot()
        caps, screen = raw.get('capabilities', {}), raw.get('screen', {})
        bright = (caps.get('screen', {}).get('available') is True
                  and type(screen.get('max_brightness')) is int and screen['max_brightness'] > 0
                  and type(screen.get('brightness')) is int and 0 <= screen['brightness'] <= 100)
        return {'settings': {key: copy.deepcopy(value) for key, value in raw.get('settings', {}).items() if key in SETTINGS},
                'settings_error': '设置状态异常，请在小屏查看' if raw.get('settings_error') else None,
                'capabilities': {'screen': {'available': caps.get('screen', {}).get('available') is True},
                                 'brightness': {'available': bright},
                                 'rgb': {'available': caps.get('rgb', {}).get('available') is True}},
                'screen': {key: screen.get(key) for key in ('brightness', 'asleep')},
                'rgb': {key: raw.get('rgb', {}).get(key) for key in ('mode', 'color', 'brightness')}}

    def _action(self, body):
        if not isinstance(body, dict) or set(body) != {'action', 'payload'} or not isinstance(body['payload'], dict):
            raise ValueError('操作格式错误')
        action, payload = body['action'], body['payload']
        if action in ('fan.policy.update', 'fan.policy.release'):
            if self.fan_policy is None:
                raise ValueError('独立温控服务未启用')
            result = self.fan_policy.action(action, copy.deepcopy(payload))
            return {**result, 'saved': result.get('settings_saved') is True,
                    'state': self._state(), 'fan_policy': self.fan_policy.snapshot()}
        state = self._state()
        if action == 'settings.update':
            changes = payload.get('settings')
            if set(payload) != {'settings'} or not isinstance(changes, dict) or not changes or set(changes) - SETTINGS:
                raise ValueError('设置包含不支持的字段')
            needs_brightness = bool(set(changes) & {'brightness', 'night_enabled', 'night_start', 'night_end', 'night_brightness'})
            if needs_brightness and not state['capabilities']['brightness']['available']:
                raise ValueError('背光能力尚未验证，请在小屏检查')
            if changes.get('sleep_timeout') and not state['capabilities']['screen']['available']:
                raise ValueError('息屏能力尚未验证')
        elif action in ('screen.brightness', 'screen.sleep', 'screen.wake'):
            allowed = {'value'} if action == 'screen.brightness' else set()
            if set(payload) != allowed:
                raise ValueError('屏幕操作参数无效')
            capability = 'brightness' if action == 'screen.brightness' else 'screen'
            if not state['capabilities'][capability]['available']:
                raise ValueError('屏幕能力尚未验证')
        elif action == 'rgb.set':
            if set(payload) - {'mode', 'color', 'brightness'} or not {'mode', 'color'} <= set(payload):
                raise ValueError('灯光操作参数无效')
            if not state['capabilities']['rgb']['available']:
                raise ValueError('请先在小屏验证灯控芯片')
        else:
            raise ValueError('网页仅支持面板、屏幕及已验证灯光设置')
        result = self.controller.action(action, copy.deepcopy(payload))
        fresh = self._state()
        ok = isinstance(result, dict) and result.get('ok') is True
        saved = result.get('settings_saved') is True if isinstance(result, dict) and action in ('settings.update', 'screen.brightness') else None
        return {'ok': ok, 'saved': saved, 'applied': ok, 'state': fresh,
                'error': None if ok else ('设置已保存，但设备应用未完成，请在小屏检查' if saved else '操作未完成，请在小屏检查')}

    def start(self):
        with self.lock:
            if self.server:
                return self
            outer = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.0'
                server_version = 'R1Web'
                sys_version = ''

                def setup(self):
                    super().setup()
                    self.rfile = _Reader(self.rfile, self)

                def handle_one_request(self):
                    self.deadline = time.monotonic() + 5
                    try:
                        super().handle_one_request()
                    except http.client.LineTooLong:
                        self.close_connection = True

                def log_message(self, *args):
                    pass

                def send(self, code, value, cookie=None, mime='application/json; charset=utf-8'):
                    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                    self.send_response(code)
                    for key, content in {'Content-Type': mime, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store',
                                         'Connection': 'close', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                                         'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"}.items():
                        self.send_header(key, content)
                    if cookie:
                        self.send_header('Set-Cookie', cookie)
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass
                    self.close_connection = True

                def valid(self, post=False):
                    for name in ('Host', 'Origin', 'Cookie', 'Content-Length', 'Connection', 'X-R1-CSRF'):
                        if len(self.headers.get_all(name, [])) > 1:
                            self.send(400, {'error': '重复请求头'})
                            return False
                    if len(self.raw_requestline) > 1024 or len(self.headers) > 32 or self.headers.get('Transfer-Encoding') or self.headers.get('Expect'):
                        self.send(400, {'error': '请求格式不支持'})
                        return False
                    host = self.headers.get('Host', '')
                    local = self.connection.getsockname()[0]
                    expected = {f'{local}:{outer.port}'}
                    if local == '127.0.0.1':
                        expected.add(f'localhost:{outer.port}')
                    if host not in expected or (self.headers.get('Origin') is not None and self.headers.get('Origin') != 'http://' + host):
                        self.send(403, {'error': '请求来源不允许'})
                        return False
                    if post and self.headers.get('Origin') != 'http://' + host:
                        self.send(403, {'error': '写入请求需要同源网页'})
                        return False
                    if urllib.parse.urlsplit(self.path).query:
                        self.send(400, {'error': '接口不接受查询参数'})
                        return False
                    return True

                def body(self):
                    length = self.headers.get('Content-Length', '')
                    if not re.fullmatch(r'[0-9]{1,4}', length) or not 0 < int(length) <= 8192:
                        raise ValueError('请求长度无效')
                    if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                        raise ValueError('请求必须为 JSON')
                    def unique(pairs):
                        obj = {}
                        for key, value in pairs:
                            if key in obj:
                                raise ValueError('重复字段')
                            obj[key] = value
                        return obj
                    def invalid(value):
                        raise ValueError('无效数值')
                    return json.loads(self.rfile.body(int(length)), object_pairs_hook=unique, parse_constant=invalid)

                def authenticated(self, csrf=False):
                    key, session = outer._session(self.headers.get('Cookie'))
                    if session is None:
                        self.send(401, {'error': '请在小屏生成新的配对码后登录'})
                        return None, None
                    supplied = self.headers.get('X-R1-CSRF', '')
                    if csrf and (not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied) or not secrets.compare_digest(supplied, session['csrf'])):
                        self.send(403, {'error': '会话校验失败，请刷新网页'})
                        return None, None
                    return key, session

                def do_GET(self):
                    if not self.valid():
                        return
                    route = urllib.parse.urlsplit(self.path).path
                    static = {'/': ('index.html', 'text/html; charset=utf-8'), '/config.css': ('config.css', 'text/css; charset=utf-8'),
                              '/config.js': ('config.js', 'application/javascript; charset=utf-8')}
                    if route in static:
                        name, mime = static[route]
                        path = outer.asset_dir / name
                        if not path.is_symlink() and path.is_file() and path.stat().st_size <= 256 * 1024:
                            self.send(200, path.read_bytes(), mime=mime)
                        else:
                            self.send(503, {'error': '网页资源不可用'})
                        return
                    _, session = self.authenticated()
                    if session is None:
                        return
                    if route == '/api/session':
                        try:
                            self.send(200, {'csrf': session['csrf'], 'state': outer._state(), 'http': True,
                                **({'fan_policy': outer.fan_policy.snapshot()} if outer.fan_policy is not None else {})})
                        except Exception:
                            self.send(503, {'error': '设置服务暂不可用'})
                    else:
                        self.send(404, {'error': '接口不存在'})

                def do_POST(self):
                    if not self.valid(post=True):
                        return
                    route = urllib.parse.urlsplit(self.path).path
                    try:
                        body = self.body()
                    except (ValueError, UnicodeError, RecursionError):
                        self.send(400, {'error': '请求格式或长度无效'})
                        return
                    if route == '/api/pair':
                        if not isinstance(body, dict) or set(body) != {'code'}:
                            self.send(400, {'error': '配对请求格式无效'})
                            return
                        token, status = outer._pair(body['code'], self.client_address[0])
                        if not token:
                            self.send(status, {'error': '尝试过于频繁，请稍后重试' if status == 429 else '配对码无效、已使用或已过期'})
                        else:
                            self.send(200, {'ok': True}, cookie=f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}')
                        return
                    key, session = self.authenticated(csrf=True)
                    if session is None:
                        return
                    if route == '/api/logout' and body == {}:
                        with outer.lock:
                            outer.sessions.pop(key, None)
                        self.send(200, {'ok': True}, cookie=f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                    elif route == '/api/action':
                        if not outer.operation_lock.acquire(blocking=False):
                            self.send(409, {'error': '已有设置正在应用，请稍后重新读取后提交'})
                            return
                        try:
                            if outer.server is None:
                                raise ValueError('服务正在停止')
                            result = outer._action(body)
                            self.send(200 if result['ok'] else 400, result)
                        except ValueError:
                            self.send(400, {'error': '操作或参数不受网页支持，设备能力须先在小屏验证'})
                        except Exception:
                            self.send(503, {'error': '设置服务暂不可用，未确认应用结果'})
                        finally:
                            outer.operation_lock.release()
                    else:
                        self.send(404, {'error': '接口不存在'})

                def unsupported(self):
                    self.send(405, {'error': '请求方法不支持'})

                do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = unsupported

            try:
                server = _Server((self.host, self.port), Handler)
            except OSError:
                self.reason = '网页端口不可用，请检查监听配置或端口占用'
                raise
            self.server, self.port, self.reason = server, server.server_port, None
            self.thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, name='r1-web-config', daemon=True)
            try:
                self.thread.start()
            except RuntimeError as error:
                server.server_close()
                self.server = self.thread = None
                self.reason = '网页服务线程启动失败'
                raise OSError(self.reason) from error
            return self

    def stop(self):
        with self.operation_lock:
            with self.lock:
                server, thread = self.server, self.thread
                self.server = self.thread = None
                self.sessions.clear()
                self.pair_hash, self.pair_until = None, 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
