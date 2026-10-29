"""Public LAN monitoring and paired controls with independent credentials."""
import copy
import datetime
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

# Companion modules live beside this file, including when tests import it by path.
_RUNTIME_DIR = str(Path(__file__).resolve().parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

from fnm_auth import AuthStore, COOKIE as AUTH_COOKIE
from fnm_compat import FnmonitorPanel
from fan_diagnostics import FanDiagnostics
from vendor_business import collect_business_diagnostics
from vendor_media_probe import collect_media_probe
from vendor_media_batch import collect_media_batch
from vendor_media_snapshot import record_media_snapshot
from vendor_app_probe import collect_application_probe
from vendor_app_diagnostics import record_application_snapshot
from vendor_storage_probe import collect_storage_probe
from integration_api import IntegrationApi
from service_settings import ServiceSettingsStore
from network_access import peer_matches
from mcp_service import validate_http_metadata
from dev_diagnostics import catalog as diagnostic_catalog, parse_request as parse_diagnostic_request, collect as collect_diagnostics

SETTINGS = frozenset(('theme', 'font_scale', 'page_interval', 'sleep_timeout', 'saver_timeout',
                      'saver_brightness', 'saver_content', 'saver_items', 'brightness',
                      'night_enabled', 'night_start', 'night_end', 'night_brightness', 'enabled_pages'))
COOKIE = 'r1_web_session'
PAIR_TTL, SESSION_TTL = 180, 8 * 3600
PUBLIC_ROUTES = frozenset(('/api/v1', '/api/v1/status', '/api/v1/history', '/api/v1/traffic',
                           '/api/v1/alerts', '/api/v1/report', '/api/v1/export.csv', '/api/v1/openapi.json'))

# Static files the panel needs.  Everything else stays unserved.
PANEL_ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
                '/index.html': ('index.html', 'text/html; charset=utf-8'),
                '/login.html': ('login.html', 'text/html; charset=utf-8')}

# fnmonitor GET routes that accept the query parameters its UI sends.
PANEL_QUERY_ROUTES = frozenset(('/api/history', '/api/smart', '/api/vms', '/api/apps',
                                '/api/report', '/api/export', '/api/update/check', '/api/daily'))

# Every fnmonitor route the vendored panel calls (plus the R1 extension).
PANEL_GET_ROUTES = frozenset((
    '/api/auth/config', '/api/auth/session',
    '/api/overview', '/api/system', '/api/sensors', '/api/fans', '/api/docker', '/api/ports',
    '/api/hardware', '/api/memory', '/api/power', '/api/power_stats', '/api/traffic', '/api/gpu',
    '/api/raidcard', '/api/processes', '/api/apps', '/api/modules', '/api/vms', '/api/history',
    '/api/history/diag', '/api/smart', '/api/disk_health', '/api/scan_log', '/api/notify',
    '/api/idrac', '/api/daily', '/api/monthly_summary', '/api/weather', '/api/update/check',
    '/api/config', '/api/ui', '/api/report', '/api/export', '/api/r1', '/api/access', '/api/access/contracts', '/api/services',
    '/api/access/file-readiness', '/api/docker/projects', '/api/docker/projects/catalog',
    '/api/business',
)) | PANEL_QUERY_ROUTES

PANEL_POST_ROUTES = frozenset((
    '/api/auth/password', '/api/auth/setup', '/api/auth/logout',
    '/api/config/save', '/api/ui', '/api/docker/action', '/api/vm/action',
    '/api/notify/save', '/api/notify/test', '/api/idrac/save', '/api/idrac/test',
    '/api/idrac/refresh', '/api/disk_scan/now', '/api/scan_log/clear', '/api/update/install',
    '/api/r1/fan', '/api/r1/fan/release', '/api/r1/light', '/api/r1/rgb', '/api/r1/screen',
    '/api/r1/settings', '/api/r1/monitor', '/api/r1/power', '/api/r1/power/cancel', '/api/r1/auth',
    '/api/dev/exec', '/api/dev/fan', '/api/dev/storage', '/api/dev/business', '/api/dev/diagnostics',
    '/api/dev/media', '/api/dev/storage/probe', '/api/access/settings', '/api/access/keys', '/api/access/keys/revoke',
    '/api/access/keys/update', '/api/access/keys/enable', '/api/access/keys/rotate',
    '/api/services/screen', '/api/services/mcp', '/api/services/developer', '/api/r1/business/refresh',
    '/api/docker/projects/register', '/api/docker/projects/update', '/api/docker/projects/revoke',
))
MACHINE_ROUTES = frozenset(('/mcp', '/api/integration/status', '/api/integration/action',
                            '/api/integration/capabilities'))
ACCESS_POST_ROUTES = frozenset(('/api/access/settings', '/api/access/keys', '/api/access/keys/revoke',
                              '/api/access/keys/update', '/api/access/keys/enable', '/api/access/keys/rotate'))
SERVICE_POST_ROUTES = frozenset(('/api/services/screen', '/api/services/mcp', '/api/services/developer'))
PROJECT_POST_ROUTES = frozenset(('/api/docker/projects/register', '/api/docker/projects/update', '/api/docker/projects/revoke'))
MANAGEMENT_POST_ROUTES = ACCESS_POST_ROUTES | SERVICE_POST_ROUTES | PROJECT_POST_ROUTES
OWNER_READ_ROUTES = frozenset(('/api/access', '/api/access/contracts', '/api/access/file-readiness',
                               '/api/services', '/api/docker/projects', '/api/docker/projects/catalog'))


def _mcp_accepts(values):
    """Require both advertised MCP response types with nonzero HTTP quality."""
    accepted = set()
    for item in ','.join(values).split(','):
        parts = [part.strip() for part in item.split(';')]
        quality, seen, valid = '1', False, True
        for parameter in parts[1:]:
            name, separator, value = parameter.partition('=')
            if not separator:
                valid = False
            elif name.strip().lower() == 'q':
                if seen or not re.fullmatch(r'(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', value.strip()):
                    valid = False
                quality, seen = value.strip(), True
        if valid and float(quality) > 0:
            accepted.add(parts[0].lower())
    return {'application/json', 'text/event-stream'} <= accepted


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


_DEV_COMMANDS = {
    'identity': ['id'],
    'uname': ['uname', '-a'],
    'processes': ['ps', '-eo', 'pid,ppid,comm,args'],
    'blocks': ['lsblk', '-J', '-o', 'NAME,MODEL,SERIAL,SIZE,ROTA,MOUNTPOINTS,LABEL,UUID'],
    'failed_units': ['systemctl', '--failed', '--no-pager', '--no-legend'],
    'fan_units': ['systemctl', 'list-units', '--all', '--type=service', '--no-pager', '--no-legend', 'r1-fan-*'],
    'boot_log': ['journalctl', '-b', '-n', '300', '--no-pager'],
    'fan_files': ['ls', '-la', '/run/r1-toolbox/fan-control'],
    'fan_status': ['find', '/run/r1-toolbox/fan-control', '-maxdepth', '2', '-type', 'f',
                   '-name', 'status.json', '-exec', 'cat', '{}', ';'],
}


def _dev_fan_process():
    """Bounded read-only snapshot of the vendor temp_control readiness fields."""
    import glob
    rows = []
    for folder in glob.glob('/proc/[0-9]*')[:4096]:
        try:
            raw = Path(folder, 'cmdline').read_bytes()[:16384]
        except OSError:
            continue
        if b'temp_control' not in raw:
            continue
        def read(name, limit=1048576):
            try:
                return Path(folder, name).read_bytes()[:limit]
            except OSError:
                return b''
        status = read('status')
        fields = {}
        for line in status.splitlines():
            key, sep, value = line.partition(b':')
            if sep:
                fields[key.decode('ascii', 'replace')] = value.strip().decode('ascii', 'replace')
        maps = read('maps')
        guard_paths = [line.split(None, 5)[-1].decode('utf-8', 'replace')
                       for line in maps.splitlines() if len(line.split(None, 5)) == 6
                       and b'fan_guard.so' in line.split(None, 5)[-1]]
        environ = read('environ', 65536)
        env_names = sorted({item.split(b'=', 1)[0].decode('ascii', 'replace') for item in environ.split(b'\0') if b'=' in item})
        rows.append({
            'pid': int(folder.rsplit('/', 1)[-1]),
            'state': fields.get('State', ''),
            'exe': (lambda: (os.readlink(folder + '/exe') if os.path.exists(folder + '/exe') else None))(),
            'sigcgt': fields.get('SigCgt', ''),
            'sigblk': fields.get('SigBlk', ''),
            'guard_loaded': bool(guard_paths),
            'guard_paths': guard_paths,
            'ld_preload': 'LD_PRELOAD' in env_names,
            'r1_fan_env': [name for name in env_names if name.startswith('R1_FAN_')],
            'cmdline': [item.decode('utf-8', 'replace')[:200] for item in raw.split(b'\0') if item][:8],
        })
    return {'ok': True, 'name': 'fan_process', 'processes': rows, 'count': len(rows)}


def _run_dev_command(name):
    """Run one fixed, read-only diagnostic command; never a shell string."""
    if name == 'fan_process':
        return _dev_fan_process()
    command = _DEV_COMMANDS.get(name)
    if command is None:
        raise ValueError('未知的开发者诊断命令')
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'})
    except (OSError, subprocess.SubprocessError) as error:
        return {'ok': False, 'name': name, 'error': type(error).__name__}
    def bounded(value):
        text = value or ''
        return text[:65536] + (chr(10) + '… truncated' if len(text) > 65536 else '')
    return {'ok': True, 'name': name, 'code': result.returncode,
            'stdout': bounded(result.stdout), 'stderr': bounded(result.stderr),
            'command': command}


class WebConfigServer:
    def __init__(self, controller, insights=None, *, fan_policy=None, light_policy=None, monitoring=None,
                 monitor=None, host='0.0.0.0', port=18732,
                 asset_dir=None, clock=None, state_dir=None, version='dev', dev_root=None, udev_root=None,
                 screen_manager=None):
        if host not in ('127.0.0.1', '0.0.0.0') or type(port) is not int or not (port == 0 or 1024 <= port <= 65535):
            raise ValueError('网页监听只允许 127.0.0.1/0.0.0.0 和 1024–65535 端口')
        self.controller, self.insights = controller, insights
        self.fan_policy = fan_policy
        self.light_policy, self.monitoring, self.monitor = light_policy, monitoring, monitor
        self.host, self.port = host, port
        self.asset_dir = Path(asset_dir) if asset_dir is not None else Path(__file__).resolve().parent.parent / 'frontend/web'
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.storage_diagnostic_lock = threading.Lock()
        self.business_diagnostic_lock = threading.Lock()
        self.business_state_lock = threading.Lock()
        self.diagnostic_lock = threading.Lock()
        self.media_probe_lock = threading.Lock()
        self.application_probe_lock = threading.Lock()
        self._business_cached, self._business_cached_at = None, 0
        self._business_epoch = self._business_cached_epoch = 0
        self.server = self.thread = None
        self.reason = None
        self.pair_hash, self.pair_until = None, 0
        self.sessions, self.attempts = {}, {}
        self.global_attempts = []
        self.version = version
        if state_dir is not None:
            self.state_dir = Path(state_dir)
        elif getattr(controller, 'state_dir', None) is not None:
            self.state_dir = Path(controller.state_dir) / 'web'
        else:
            # Tests and embedding callers without a writable volume still need a
            # private directory; never fall back to the current working tree.
            import tempfile
            self.state_dir = Path(tempfile.mkdtemp(prefix='r1-panel-'))
        self.auth = AuthStore(self.state_dir)
        self.panel = FnmonitorPanel(controller, insights, monitoring, fan_policy, light_policy, monitor,
                                    state_dir=self.state_dir, version=version, web=self,
                                    dev_root=dev_root or '/dev', udev_root=udev_root or '/run/udev')
        self.auth_mode = os.environ.get('R1_WEB_AUTH_MODE', 'password')
        if self.auth_mode not in ('pair', 'password'):
            self.auth_mode = 'password'
        self.service_settings = ServiceSettingsStore(self.state_dir)
        self.screen_manager = screen_manager
        self._screen_error = None
        self._dev_fingerprint = None
        self.dev_no_pairing, self.dev_allow_ips = False, set()
        self.dev_until, self.dev_commands = 0, False
        self._sync_development(force=True)
        self.fan_diagnostics = FanDiagnostics(fan_policy, version=version)
        self.integration = IntegrationApi(self, state_dir=self.state_dir, version=version)
        self.api_access = self.integration.access

    def _sync_development(self, state=None, *, force=False):
        """Apply persisted changes while retaining legacy embedding attributes.

        Existing embedders may shorten the in-memory window for diagnostics.
        A real Web edit or changed persisted configuration replaces that cache;
        malformed storage always closes access, regardless of cached attributes.
        """
        state = state if state is not None else self.service_settings.snapshot()
        if not state['storage_ok']:
            self.dev_no_pairing, self.dev_allow_ips = False, set()
            self.dev_until, self.dev_commands, self._dev_fingerprint = 0, False, None
            return state
        dev = state['developer']
        fingerprint = (dev['enabled'], tuple(dev['allowed_ips']), dev['minutes'],
                       dev['expires_at'], dev['allow_commands'])
        if force or fingerprint != self._dev_fingerprint:
            self.dev_no_pairing = dev['enabled']
            self.dev_allow_ips = set(dev['allowed_ips'])
            self.dev_until = dev['expires_at'] or 0
            self.dev_commands = dev['allow_commands']
            self._dev_fingerprint = fingerprint
        return state

    def attach_screen_manager(self, manager):
        """Attach the independent GUI lifecycle after core services are ready."""
        self.screen_manager = manager
        try:
            manager.set_enabled(self.service_settings.snapshot()['screen_enabled'])
            self._screen_error = None
        except Exception:
            self._screen_error = '小屏接管请求未完成；网页与 MCP 保持可用。'

    def services_snapshot(self):
        settings = self._sync_development()
        screen = {'enabled': settings['screen_enabled'], 'active': None, 'busy': None,
                  'available': False, 'last_error': '小屏接管管理器未就绪。'}
        if self.screen_manager is not None:
            try:
                live = self.screen_manager.snapshot()
                if not isinstance(live, dict):
                    raise ValueError('invalid screen lifecycle snapshot')
                for key in ('active', 'busy', 'available'):
                    value = live.get(key)
                    if type(value) is not bool:
                        raise ValueError('unknown screen lifecycle field')
                    screen[key] = value
                # Only the lifecycle adapter supplies this bounded product error.
                error = live.get('last_error')
                screen['last_error'] = error if isinstance(error, str) and len(error) <= 256 else None
            except Exception:
                screen.update(active=None, busy=None, available=False)
                screen['last_error'] = '小屏接管状态暂不可确认。'
        if self._screen_error:
            screen['last_error'] = self._screen_error
        dev = settings['developer']
        return {'ok': True, 'screen': screen,
                'mcp': {'enabled': self.api_access.snapshot()['enabled']},
                'developer': {key: dev[key] for key in (
                    'enabled', 'active', 'allowed_ips', 'minutes', 'expires_at', 'allow_commands', 'reason')}}

    def _services_write(self, route, body):
        """Browser-owner settings only; no API token or developer bypass here."""
        if route == '/api/services/mcp':
            self.api_access.configure(body)
        elif route == '/api/services/developer':
            self.service_settings.configure_developer(body)
            self._sync_development(force=True)
        elif route == '/api/services/screen':
            if self.screen_manager is None:
                return 503, {'ok': False, 'error': '小屏接管管理器未就绪，未改变显示状态。'}
            self.service_settings.configure_screen(body)
            try:
                self.screen_manager.set_enabled(body['enabled'])
                self._screen_error = None
            except Exception:
                self._screen_error = '小屏偏好已保存，但切换未完成，请核对实际状态后重试。'
                state = self.services_snapshot()
                return 503, {'ok': False, 'saved': True, 'error': self._screen_error,
                             'services': {key: value for key, value in state.items() if key != 'ok'}}
        else:
            raise ValueError('不支持的服务设置')
        state = self.services_snapshot()
        return 200, {'ok': True, 'services': {key: value for key, value in state.items() if key != 'ok'}}

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
                    'reason': self.reason, 'session_count': len(self.sessions), 'auth_mode': self.auth_mode,
                    'pairing': {'active': self.pair_hash is not None,
                                'expires_in': max(0, int(self.pair_until - self.clock())) if self.pair_hash else 0}}

    def pairing_info(self, rotate=False):
        """Call only from the local authenticated screen action, never a web GET."""
        with self.lock:
            if not self.server:
                raise ValueError('网页配置服务未启用')
            result = self.snapshot()
            if rotate is True:
                for attempt in range(4):
                    number = secrets.randbelow(10 ** 5)
                    code = f'{number:05d}'
                    if self.pair_hash is None or not secrets.compare_digest(hashlib.sha256(code.encode()).digest(), self.pair_hash):
                        break
                else:
                    # Bounded even if the random source repeats; the immediately
                    # preceding live code must never survive a rotation.
                    code = f'{(number + 1) % 100000:05d}'
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
            digest = hashlib.sha256(code.encode()).digest() if isinstance(code, str) and re.fullmatch(r'[0-9]{5}', code) else b''
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

    @staticmethod
    def _read(component):
        """Read component state without commands, SMART probes or I²C writes."""
        if component is None:
            return {}
        try:
            value = component.snapshot()
            return copy.deepcopy(value) if isinstance(value, dict) else {}
        except Exception:
            return {'available': False, 'reason': '监控来源暂不可用'}

    def public_status(self):
        raw = self._read(self.controller)
        controls = {key: raw.get(key) for key in ('capabilities', 'screen', 'rgb', 'fans', 'power')}
        fan = self._read(self.fan_policy)
        # Runtime is public; drafts and settings write acknowledgements stay in
        # the paired session. No pairing code, CSRF, token or environment here.
        fan = {key: fan[key] for key in ('active', 'busy', 'capability', 'channels', 'sources',
                                        'last_error', 'recovery_confirmed') if key in fan}
        light = self._read(self.light_policy)
        light = {key: light[key] for key in ('active', 'busy', 'capability', 'runtime', 'sources', 'target', 'last_requested_target',
                    'reference_c', 'source_id', 'source_label', 'last_error', 'fallback', 'reason', 'schedule_active') if key in light}
        analytics = self._read(self.monitoring)
        analytics = {key: analytics[key] for key in ('available', 'current', 'summary', 'smart', 'history') if key in analytics}
        status, insights = self._read(self.monitor), self._read(self.insights)
        return {'schema_version': 1, 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                'available': status.get('available', bool(insights)), 'status': status,
                'insights': insights, 'controls': controls, 'fan_policy': fan,
                'light_policy': light, 'analytics': analytics}

    def _public(self, route, query):
        parameters = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=4)
        if set(parameters) - {'range'} or any(len(values) != 1 for values in parameters.values()):
            raise ValueError('查询参数无效')
        span = parameters.get('range', ['1h'])[0]
        if span not in ('10m', '1h', '6h', '24h', '7d', '30d'):
            raise ValueError('时间范围无效')
        if parameters and route not in ('/api/v1/history', '/api/v1/export.csv'):
            raise ValueError('此接口不接受查询参数')
        if route == '/api/v1':
            return {'schema_version': 1, 'read_only': True, 'endpoints': sorted(PUBLIC_ROUTES),
                    'settings': 'POST /api/action 需要五位配对码建立的会话与 CSRF'}, 'application/json; charset=utf-8'
        if route == '/api/v1/status':
            return self.public_status(), 'application/json; charset=utf-8'
        if route == '/api/v1/openapi.json':
            paths = {}
            for path in sorted(PUBLIC_ROUTES):
                if path == '/api/v1/openapi.json':
                    continue
                csv = path.endswith('.csv')
                operation = {'summary': '公开只读监控；写操作另需配对', 'security': [],
                    'responses': {'200': {'description': '无读数使用null；保留每项实际采样时间',
                        'content': {'text/csv' if csv else 'application/json': {
                            'schema': {'type': 'string'} if csv else {'type': 'object', 'additionalProperties': True}}}},
                        '400': {'description': '查询参数无效'}, '503': {'description': '来源暂不可用'}}}
                if path in ('/api/v1/history', '/api/v1/export.csv'):
                    operation['parameters'] = [{'name': 'range', 'in': 'query', 'required': False,
                        'schema': {'type': 'string', 'enum': ['10m', '1h', '6h', '24h', '7d', '30d'], 'default': '1h'}}]
                paths[path] = {'get': operation}
            return {'openapi': '3.1.0', 'info': {'title': 'R1 Toolbox 只读监控 API', 'version': '1.0.0'},
                    'servers': [{'url': '/'}], 'paths': paths}, 'application/json; charset=utf-8'
        if self.monitoring is None:
            raise RuntimeError('统计服务尚不可用')
        method = {'/api/v1/history': lambda: self.monitoring.history(span),
                  '/api/v1/traffic': self.monitoring.traffic,
                  '/api/v1/alerts': self.monitoring.alerts,
                  '/api/v1/report': self.monitoring.report,
                  '/api/v1/export.csv': lambda: self.monitoring.export_csv(span)}[route]
        value = method()
        if route.endswith('.csv'):
            return value.encode('utf-8-sig') if isinstance(value, str) else value, 'text/csv; charset=utf-8'
        return value, 'application/json; charset=utf-8'

    def _action(self, body):
        if not isinstance(body, dict) or set(body) != {'action', 'payload'} or not isinstance(body['payload'], dict):
            raise ValueError('操作格式错误')
        action, payload = body['action'], body['payload']
        if action in ('light.policy.update', 'monitor.settings.update'):
            service = self.light_policy if action == 'light.policy.update' else self.monitoring
            if service is None:
                raise ValueError('配置服务未启用')
            result = service.action(action, copy.deepcopy(payload))
            return {**result, 'saved': result.get('settings_saved') is True, 'state': self._state(),
                    **({'light_policy': service.snapshot()} if action == 'light.policy.update' else {'analytics': service.snapshot()})}
        if action in ('fan.policy.update', 'fan.policy.release'):
            if self.fan_policy is None:
                raise ValueError('独立温控服务未启用')
            result = self.fan_policy.action(action, copy.deepcopy(payload))
            return {**result, 'saved': result.get('settings_saved') is True,
                    'state': self._state(), 'fan_policy': self.fan_policy.snapshot()}
        state = self._state()
        if action in ('container.action', 'vm.action', 'power.request', 'power.cancel'):
            if action != 'power.cancel' and payload.get('confirmed') is not True:
                raise ValueError('设备操作需要明确确认')
            destination = self.insights if action in ('container.action', 'vm.action') else self.controller
            if destination is None:
                raise ValueError('设备控制服务不可用')
            result = destination.action(action, copy.deepcopy(payload))
            ok = result.get('ok') is True
            refresh = (result.get('refresh_pending') is True
                       or isinstance(result.get('state'), dict) and result['state'].get('refresh_pending') is True)
            workload_errors = {
                'busy': '已有应用操作正在执行，请先刷新状态，勿连续提交',
                'stopping': '应用控制服务正在停止，请稍后重新读取状态',
                'protected_container': '保留的 Toolbox 容器不允许在此操作',
                'invalid_state': '容器状态已改变或不支持此操作，请刷新列表后再决定',
                'state_unconfirmed': 'Docker 当前状态未确认，请刷新列表后再决定',
                'not_found': '设备已不存在或标识不唯一，请刷新列表',
                'operation_unconfirmed': '操作超时或响应异常，结果未确认；请先刷新列表核对，勿连续重试',
                'operation_failed': '宿主机未完成操作，请刷新列表并查看服务状态',
                'unsupported': '应用服务当前不可用，请刷新列表并检查宿主权限',
            }
            code = result.get('error') if action in ('container.action', 'vm.action') else None
            reason = workload_errors.get(code, '操作未完成，请检查设备状态')
            response = {'ok': ok, 'applied': ok and not refresh, 'pending': ok and refresh,
                        'refresh_pending': refresh, 'state': self._state(), 'error': None if ok else reason}
            if code in workload_errors:
                response['error_code'] = code
            if action in ('container.action', 'vm.action') and type(result.get('exit_code')) is int:
                response['exit_code'] = result['exit_code']
            return response
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

    # ------------------------------------------------------------------
    # fnmonitor panel: authentication, read routes and write routes
    # ------------------------------------------------------------------
    def password_session(self, headers):
        """``None`` when no valid password session cookie was sent."""
        return self.auth.session(headers.get('Cookie') or '')

    def _dev_authorized(self, peer):
        self._sync_development()
        if not self.dev_no_pairing or not self.dev_allow_ips or time.time() >= self.dev_until:
            return False
        return peer_matches(str(peer), [str(rule) for rule in self.dev_allow_ips]) if peer else False

    def panel_authorized(self, headers, *, network_csrf=True, peer=None):
        """Password session, or the small-screen pairing session, may write.

        The pairing session normally proves itself with ``X-R1-CSRF`` on
        ``/api/action``.  fnmonitor's front-end has no such token, so panel
        writes accept a same-origin POST carrying ``X-FNM-Request: 1`` instead:
        that custom header forces a CORS preflight this server never grants, and
        the handler already rejects foreign origins and non-JSON bodies.
        """
        if self.password_session(headers) is not None:
            return 'password'
        if self.auth_mode == 'password' and not self.auth.required():
            return 'open'
        _, session = self._session(headers.get('Cookie') or '')
        if session is not None and network_csrf:
            return 'pair'
        if self._dev_authorized(peer) and not self.auth.required():
            return 'dev'
        return None

    def panel_readable(self, headers, peer=None):
        if self.auth.required():
            return self.password_session(headers) is not None
        return True

    def auth_status(self, headers, peer=None):
        session = self.password_session(headers)
        dev = self._dev_authorized(peer) and not self.auth.required()
        return {'logged_in': session is not None, 'auth_required': self.auth.required(),
                'authorized': session is not None or dev or (self.auth_mode == 'password' and not self.auth.required()),
                'auth_mode': self.auth_mode, 'dev_mode': dev,
                'client_ip': str(peer) if peer else None,
                'session': {'label': session.get('label'),
                            'expires_in': max(0, int(session['expires_wall'] - time.time()))} if session else None,
                'config': self.auth.auth_config()}

    @staticmethod
    def _panel_query(query, allowed):
        try:
            parsed = urllib.parse.parse_qs(query, keep_blank_values=False, max_num_fields=6)
        except ValueError:
            raise ValueError('查询参数无效')
        if set(parsed) - set(allowed) or any(len(values) != 1 for values in parsed.values()):
            raise ValueError('查询参数无效')
        return {key: values[0] for key, values in parsed.items()}

    def panel_get(self, route, query, headers, peer=None):
        """Return ``(status, payload, content_type)`` for a panel GET route."""
        panel = self.panel
        if route in OWNER_READ_ROUTES:
            if (headers.get('Authorization') is not None
                    or self.panel_authorized(headers, peer=peer) not in ('password', 'pair', 'open')):
                return 403, {'ok': False, 'error': 'API 管理需要网页登录授权'}, 'application/json; charset=utf-8'
            if query:
                raise ValueError('API 管理不接受查询参数')
            if route == '/api/access/contracts':
                return 200, self.integration.contracts(), 'application/json; charset=utf-8'
            if route == '/api/access/file-readiness':
                return 200, self.integration.file_readiness(), 'application/json; charset=utf-8'
            if route in ('/api/docker/projects', '/api/docker/projects/catalog'):
                value = (self.integration.docker_projects.catalog() if route.endswith('/catalog')
                         else self.integration.docker_projects.snapshot())
                return (200 if value.get('ok') is True else 503), value, 'application/json; charset=utf-8'
            return 200, (self.integration.snapshot() if route == '/api/access' else self.services_snapshot()), 'application/json; charset=utf-8'
        if route == '/api/auth/config':
            return 200, self.auth.auth_config(), 'application/json; charset=utf-8'
        if route == '/api/auth/session':
            return 200, self.auth_status(headers, peer=peer), 'application/json; charset=utf-8'
        if route == '/api/r1':
            authorized = self.panel_authorized(headers, peer=peer) is not None
            return 200, panel.r1_state(authorized=authorized), 'application/json; charset=utf-8'
        if route == '/api/business':
            if query:
                raise ValueError('业务状态不接受查询参数')
            status, value, _ = self._business_state()
            return status, value, 'application/json; charset=utf-8'
        params = self._panel_query(query, {
            '/api/history': ('metric', 'range'), '/api/smart': ('dev',),
            '/api/vms': ('probe',), '/api/apps': ('refresh',), '/api/gpu': ('diag',),
            '/api/report': ('format',), '/api/export': ('type', 'range'),
            '/api/update/check': ('force',), '/api/daily': ('days',), '/api/scan_log': ('limit',),
        }.get(route, ()))
        if route == '/api/overview':
            return 200, panel.overview(), 'application/json; charset=utf-8'
        if route == '/api/system':
            return 200, panel.system(), 'application/json; charset=utf-8'
        if route == '/api/sensors':
            return 200, panel.sensors(), 'application/json; charset=utf-8'
        if route == '/api/fans':
            return 200, panel.fans(), 'application/json; charset=utf-8'
        if route == '/api/docker':
            return 200, panel.docker(), 'application/json; charset=utf-8'
        if route == '/api/ports':
            return 200, panel.ports(), 'application/json; charset=utf-8'
        if route == '/api/hardware':
            return 200, panel.hardware(), 'application/json; charset=utf-8'
        if route == '/api/memory':
            return 200, panel.memory(), 'application/json; charset=utf-8'
        if route == '/api/power':
            return 200, panel.power(), 'application/json; charset=utf-8'
        if route == '/api/power_stats':
            return 200, panel.power_stats(), 'application/json; charset=utf-8'
        if route == '/api/traffic':
            return 200, panel.traffic(), 'application/json; charset=utf-8'
        if route == '/api/gpu':
            return 200, panel.gpu(diag=params.get('diag') in ('1', 'true')), 'application/json; charset=utf-8'
        if route == '/api/raidcard':
            return 200, panel.raidcard(), 'application/json; charset=utf-8'
        if route == '/api/processes':
            return 200, panel.processes(), 'application/json; charset=utf-8'
        if route == '/api/apps':
            return 200, panel.apps(), 'application/json; charset=utf-8'
        if route == '/api/modules':
            return 200, panel.modules(), 'application/json; charset=utf-8'
        if route == '/api/vms':
            if params.get('probe') in ('1', 'true', 'yes'):
                return 200, panel.vm_probe(), 'application/json; charset=utf-8'
            return 200, panel.vms(), 'application/json; charset=utf-8'
        if route == '/api/history':
            return 200, panel.history(params.get('metric', 'cpu'), params.get('range', '1h')), 'application/json; charset=utf-8'
        if route == '/api/history/diag':
            return 200, panel.history_diag(), 'application/json; charset=utf-8'
        if route == '/api/smart':
            return 200, panel.smart(params.get('dev', '')), 'application/json; charset=utf-8'
        if route == '/api/disk_health':
            return 200, panel.disk_health(), 'application/json; charset=utf-8'
        if route == '/api/scan_log':
            return 200, panel.scan_log(), 'application/json; charset=utf-8'
        if route == '/api/notify':
            return 200, panel.notify(), 'application/json; charset=utf-8'
        if route == '/api/idrac':
            return 200, panel.idrac(), 'application/json; charset=utf-8'
        if route == '/api/daily':
            return 200, panel.daily(params.get('days', '30')), 'application/json; charset=utf-8'
        if route == '/api/monthly_summary':
            return 200, panel.monthly_summary(), 'application/json; charset=utf-8'
        if route == '/api/weather':
            return 200, panel.weather(), 'application/json; charset=utf-8'
        if route == '/api/update/check':
            return 200, {'ok': True, 'has_update': False, 'latest': self.version,
                         'note': 'R1 Toolbox 通过镜像标签更新，不在面板内自更新'}, 'application/json; charset=utf-8'
        if route == '/api/config':
            return 200, {'config': panel.config()}, 'application/json; charset=utf-8'
        if route == '/api/ui':
            return 200, {'ui': panel.store.ui()}, 'application/json; charset=utf-8'
        if route == '/api/report':
            fmt = params.get('format', 'json')
            if fmt == 'html':
                return 200, panel.report('html').encode('utf-8'), 'text/html; charset=utf-8'
            return 200, panel.report(), 'application/json; charset=utf-8'
        if route == '/api/export':
            kind = params.get('type', 'history')
            if kind != 'history':
                raise ValueError('仅支持历史 CSV')
            return 200, panel.export_csv(params.get('range', '7d')).encode('utf-8-sig'), 'text/csv; charset=utf-8'
        return 404, {'ok': False, 'error': 'not_found'}, 'application/json; charset=utf-8'

    def panel_post(self, route, body, headers, ip=''):
        """Return ``(status, payload, cookie)`` for a panel POST route."""
        if route in MANAGEMENT_POST_ROUTES and headers.get('Authorization') is not None:
            return 403, {'ok': False, 'error': '访问密钥不能管理 API 授权，请使用网页登录'}, None
        if route == '/api/r1/business/refresh':
            return self._business_refresh_post(body, headers, ip)
        if route == '/api/dev/diagnostics':
            return self._diagnostic_post(body, ip)
        if route == '/api/dev/media':
            return self._media_debug_post(body, ip)
        if route == '/api/dev/storage/probe':
            return self._storage_probe_post(body, ip)
        if route == '/api/dev/business':
            return self._business_debug_post(body, headers, ip)
        if route == '/api/dev/storage':
            return self._storage_debug_post(body, headers, ip)
        if route == '/api/dev/fan':
            return self._fan_debug_post(body, headers, ip)
        if route == '/api/dev/exec':
            if not self._dev_authorized(ip) or self.auth.required():
                return 403, {'ok': False, 'error': '开发者命令接口未授权'}, None
            if not self.dev_commands:
                return 403, {'ok': False, 'error': '开发者命令接口未启用'}, None
            if not isinstance(body, dict) or set(body) != {'name'} or not isinstance(body['name'], str):
                return 400, {'ok': False, 'error': '只需要 name 字段'}, None
            try:
                return 200, _run_dev_command(body['name']), None
            except ValueError as error:
                return 400, {'ok': False, 'error': str(error)}, None
        if route == '/api/auth/password':
            wait = self.auth.limiter.retry_after(ip)
            if wait:
                return 429, {'ok': False, 'error': '尝试次数过多，请 %d 秒后再试' % wait, 'retry_after': wait}, None
            result = self.auth.login(str(body.get('password') or ''), ip)
            if not result.get('ok'):
                return 401, result, None
            return 200, {'ok': True}, self._auth_cookie(result['sid'])
        if route == '/api/auth/setup':
            # Password mode permits initial setup without screen ownership.
            # Explicit legacy pairing mode retains its original unlock rule.
            if self.auth.password_set():
                return 400, {'ok': False, 'error': '已设置口令，请使用登录修改'}, None
            if self.panel_authorized(headers, peer=ip) is None:
                return 403, {'ok': False, 'error': 'need_pairing',
                             'message': '设置登录口令需要先在小屏生成五位配对码'}, None
            result = self.auth.setup(str(body.get('password') or ''), ip)
            if not result.get('ok'):
                return 400, result, None
            return 200, {'ok': True}, self._auth_cookie(result['sid'])
        if route == '/api/auth/logout':
            sid = self.auth.cookie_sid(headers.get('Cookie') or '')
            if sid:
                self.auth.revoke(sid)
            return 200, {'ok': True}, self._auth_cookie('', clear=True)
        authorization = self.panel_authorized(headers, peer=ip)
        authorized = authorization is not None
        if route in MANAGEMENT_POST_ROUTES and authorization == 'dev':
            return 403, {'ok': False, 'error': '授权管理需要网页登录，开发者白名单不能修改授权'}, None
        if not authorized and not (route == '/api/ui' and not self.auth.required()):
            # With a login password the panel is already gated, so a 401 sends the
            # browser to the login page.  Without one, keep public monitoring and
            # ask for the small screen's one-time pairing code instead.
            if self.auth.required():
                return 401, {'ok': False, 'error': '未登录', 'auth_required': True}, None
            return 403, {'ok': False, 'error': 'need_pairing',
                         'message': '需要在小屏「设置 → 网页配置」生成五位配对码后解锁设置'}, None
        if route in PROJECT_POST_ROUTES:
            # Registry has its own bounded lock; daemon reads must not hold the
            # hardware-policy lock while an owner registers metadata.
            started = time.monotonic()
            try:
                value = self.integration.manage(route, body)
            except (ValueError, TypeError):
                return 400, {'ok': False, 'error': '项目登记参数无效'}, None
            except Exception:
                return 503, {'ok': False, 'error': '项目登记暂不可用'}, None
            code = value.get('error', {}).get('code') if isinstance(value.get('error'), dict) else None
            status = 200 if value.get('ok') is True else 400 if code == 'invalid_arguments' else 409 if code == 'resource_busy' else 503
            self.integration.record(None, 'config', ip, value, started=started, touch_key=False)
            return status, value, None
        # Serialise writes: settings files are read-modify-write, and a fan or
        # light apply must not interleave with another configuration save.
        if not self.operation_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有设置正在保存或应用，请稍后重试'}, None
        started = time.monotonic()
        try:
            try:
                status, payload = self._panel_write(route, body, headers)
            except ValueError as error:
                return 400, {'ok': False, 'error': str(error)}, None
            except Exception:
                return 503, {'ok': False, 'error': '服务暂不可用，未确认操作结果'}, None
            if route in ACCESS_POST_ROUTES and 200 <= status < 300:
                operation = {'/api/access/settings': 'config', '/api/access/keys': 'key.create',
                             '/api/access/keys/revoke': 'key.revoke', '/api/access/keys/update': 'key.update',
                             '/api/access/keys/enable': 'key.enable', '/api/access/keys/rotate': 'key.rotate'}[route]
                target = payload.get('key', {}).get('id') if isinstance(payload.get('key'), dict) else body.get('id')
                self.integration.record({'key_id': target} if target else None, operation, ip,
                                        payload, started=started, touch_key=False)
            return status, payload, None
        finally:
            self.operation_lock.release()

    def _media_debug_post(self, body, ip):
        """Explicit media probe; credentials stay on the host, never in the API.

        Unlike passive diagnostics this invokes native status queries, whose
        completion branch may update vendor state. It is never auto-polled.
        """
        if not self._dev_authorized(ip):
            return 403, {'ok': False, 'error': '媒体测试未启用、已过期或客户端不在白名单'}, None
        if not isinstance(body, dict) or body not in ({'action': 'probe'}, {'action': 'probe_all'}):
            return 400, {'ok': False, 'error': '仅支持固定媒体 probe 或 probe_all 测试，不接受账号、令牌或路径参数'}, None
        if not self.media_probe_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有媒体测试正在进行'}, None
        try:
            if body['action'] == 'probe_all':
                value = collect_media_batch()
                receipt = self._save_media_snapshot(value, 'batch')
                return 200, {'ok': True, 'batch': value, 'cache': receipt}, None
            value = collect_media_probe()
            receipt = self._save_media_snapshot(value, 'face')
            return 200, {'ok': True, 'probe': value, 'cache': receipt}, None
        except Exception:
            return 503, {'ok': False, 'error': '媒体测试暂不可确认'}, None
        finally:
            self.media_probe_lock.release()

    def _save_media_snapshot(self, value, kind):
        """Record an already completed explicit query; never retry on write failure."""
        receipt = record_media_snapshot(self.state_dir, value, kind=kind)
        self._invalidate_business_cache()
        return receipt

    def _storage_probe_post(self, body, ip):
        """Explicit fixed native query; never part of a passive storage read.

        This may cause vendor device/SMART work. The developer grant and exact
        confirmation apply only to this fixed call, with no forwarded arguments.
        """
        if not self._dev_authorized(ip):
            return 403, {'ok': False, 'error': '存储主动测试未启用、已过期或客户端不在白名单'}, None
        if (not isinstance(body, dict) or set(body) != {'action', 'confirm'}
                or body.get('action') != 'probe' or body.get('confirm') is not True):
            return 400, {'ok': False, 'error': '仅支持明确确认的固定存储 probe，不接受账号、令牌、路径或动作参数'}, None
        if not callable(collect_storage_probe):
            return 503, {'ok': False, 'error': '存储主动测试来源暂不可用'}, None
        if not self.storage_diagnostic_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有存储查询正在进行'}, None
        try:
            # Revalidate at the point of use; acquiring the shared source lock
            # must not let an expired/revoked developer window reach the host.
            if not self._dev_authorized(ip):
                return 403, {'ok': False, 'error': '存储主动测试未启用、已过期或客户端不在白名单'}, None
            value = collect_storage_probe()
            if not isinstance(value, dict) or type(value.get('available')) is not bool:
                return 503, {'ok': False, 'error': '存储主动测试暂不可确认'}, None
            return 200, {'ok': True, 'probe': value}, None
        except Exception:
            return 503, {'ok': False, 'error': '存储主动测试暂不可确认'}, None
        finally:
            self.storage_diagnostic_lock.release()

    def _invalidate_business_cache(self):
        """Prevent a slow pre-refresh collection from publishing an older result."""
        with self.business_state_lock:
            self._business_epoch += 1
            self._business_cached, self._business_cached_at = None, 0

    def _business_refresh_post(self, body, headers, ip):
        """Owner-initiated fixed getters; native middleware may update its state."""
        authorization = self.panel_authorized(headers, peer=ip)
        if headers.get('Authorization') is not None or authorization not in ('password', 'pair', 'open'):
            if self.auth.required() and authorization is None:
                return 401, {'ok': False, 'error': '未登录', 'auth_required': True}, None
            return 403, {'ok': False, 'error': '刷新原厂业务需要网页登录授权'}, None
        if (not isinstance(body, dict) or set(body) != {'source'}
                or body['source'] not in ('applications', 'media')):
            return 400, {'ok': False, 'error': '请选择 applications 或 media 固定来源'}, None
        source = body['source']
        lock = self.media_probe_lock if source == 'media' else self.application_probe_lock
        if not lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '该业务来源已有刷新正在进行'}, None
        try:
            if source == 'media':
                value = collect_media_batch()
                receipt = self._save_media_snapshot(value, 'batch')
            else:
                value = collect_application_probe()
                stored = record_application_snapshot(self.state_dir, value)
                receipt = {'stored': stored, 'reason': None if stored else 'snapshot_not_saved'}
                self._invalidate_business_cache()
            # The result reports native success separately from HTTP handling.
            return 200, {'ok': True, 'source': source, 'result': value, 'cache': receipt}, None
        except Exception:
            return 503, {'ok': False, 'error': '业务刷新未能完成，请先核对当前状态'}, None
        finally:
            lock.release()

    def _business_state(self):
        """Short-lived passive snapshot; no signed native query runs from GET."""
        with self.business_state_lock:
            cached = self._business_cached
            if (cached is not None and self._business_cached_epoch == self._business_epoch
                    and time.monotonic() - self._business_cached_at < 30):
                return 200, {'ok': True, 'snapshot': copy.deepcopy(cached), 'cached': True}, None
        status, value, cookie = self._business_debug_snapshot()
        if status == 409 and value.get('error_code') == 'business_changed':
            # Retry only the passive read, never a native signed refresh.
            return self._business_debug_snapshot()
        if status == 409:
            with self.business_state_lock:
                if self._business_cached is not None and self._business_cached_epoch == self._business_epoch:
                    return 200, {'ok': True, 'snapshot': copy.deepcopy(self._business_cached),
                                 'cached': True, 'busy': True}, None
        return status, value, cookie

    def _diagnostic_post(self, body, ip):
        """Explicit developer IP access grants passive checks without a login.

        This grant is local to this fixed route. Control, retry and command
        routes retain their existing password and command permissions.
        """
        if not self._dev_authorized(ip):
            return 403, {'ok': False, 'error': '开发者检测未启用、已过期或客户端不在白名单'}, None
        try:
            action, checks = parse_diagnostic_request(body)
        except ValueError:
            return 400, {'ok': False, 'error': '仅支持固定检测目录或不重复的检测项目列表'}, None
        access = {'client_ip': str(ip), 'expires_in': max(0, int(self.dev_until - time.time()))}
        if action == 'catalog':
            return 200, {'ok': True, 'version': self.version, 'access': access,
                         'catalog': diagnostic_catalog()}, None
        if not self.diagnostic_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有开发者检测正在进行'}, None
        try:
            collectors = {
                'device': self._device_diagnostic_snapshot,
                'storage': lambda: self._storage_debug_snapshot()[:2],
                'storage_identity': lambda: self._storage_debug_snapshot(include_serial=True)[:2],
                'business': lambda: self._business_debug_snapshot()[:2],
                'fan': lambda: (200, {'ok': True, 'snapshot': self.fan_diagnostics.snapshot()}),
                'file_access_readiness': self._file_readiness_snapshot,
                'docker_project_readiness': lambda: (200, {'ok': True,
                    'discovery': self.integration.docker_projects.catalog(),
                    'registry': self.integration.docker_projects.metadata()}),
            }
            snapshot = collect_diagnostics(checks, collectors)
            if not self._dev_authorized(ip):
                return 403, {'ok': False, 'error': '开发窗口已失效，未返回检测结果'}, None
            return 200, {'ok': True, 'version': self.version, 'access': access,
                         'snapshot': snapshot}, None
        finally:
            self.diagnostic_lock.release()

    def _file_readiness_snapshot(self):
        """Share storage pressure control without opening user files."""
        if not self.storage_diagnostic_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有存储查询正在进行'}
        try:
            return 200, self.integration.file_readiness()
        finally:
            self.storage_diagnostic_lock.release()

    def _device_diagnostic_snapshot(self):
        """Project identity fields from the existing cache; never refresh hardware."""
        if self.insights is None:
            return 503, {'ok': False}
        cached = self.insights.snapshot()
        identity = cached.get('device', {}).get('identity', {})
        if not isinstance(identity, dict) or not identity:
            return 503, {'ok': False}

        def text(value):
            return value[:256] if isinstance(value, str) and value else None

        device = {key: text(identity.get(key)) for key in ('name', 'name_source', 'name_reason', 'hostname', 'kernel')}
        firmware = identity.get('firmware', {})
        if not isinstance(firmware, dict):
            firmware = {}
        device['firmware'] = {key: text(firmware.get(key)) for key in
                              ('version', 'firmware_version', 'build', 'build_date', 'build_time', 'model')}
        return 200, {'ok': True, 'source': 'insights_cache', 'device': device}

    def _business_debug_post(self, body, headers, ip):
        """Fixed passive business snapshot; never a general factory API bridge."""
        if not self._dev_authorized(ip) or not self.dev_commands:
            return 403, {'ok': False, 'error': '业务诊断接口未启用或客户端未授权'}, None
        if self.auth.required() and self.password_session(headers) is None:
            return 401, {'ok': False, 'error': '未登录', 'auth_required': True}, None
        if not isinstance(body, dict) or body != {'action': 'snapshot'}:
            return 400, {'ok': False, 'error': '仅支持固定只读 snapshot 查询'}, None
        return self._business_debug_snapshot()

    def _business_debug_snapshot(self):
        """Shared bounded collector; authorization belongs to each HTTP route."""
        if not self.business_diagnostic_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有业务诊断正在进行'}, None
        try:
            # Compatibility for embedding fixtures created without __init__.
            if not hasattr(self, 'business_state_lock'):
                self.business_state_lock = threading.Lock()
                self._business_epoch = self._business_cached_epoch = 0
            with self.business_state_lock:
                epoch = self._business_epoch
            state_dir = getattr(self, 'state_dir', None)
            snapshot = (collect_business_diagnostics(state_dir=state_dir) if state_dir is not None
                        else collect_business_diagnostics())
            with self.business_state_lock:
                if epoch != self._business_epoch:
                    return 409, {'ok': False, 'error': '业务来源已刷新，请读取新快照',
                                 'error_code': 'business_changed'}, None
                self._business_cached, self._business_cached_at = copy.deepcopy(snapshot), time.monotonic()
                self._business_cached_epoch = epoch
            return 200, {'ok': True, 'snapshot': snapshot, 'cached': False}, None
        except Exception:
            return 503, {'ok': False, 'error': '业务诊断暂不可确认'}, None
        finally:
            self.business_diagnostic_lock.release()

    def _storage_debug_post(self, body, headers, ip):
        """Fixed name diagnostics; the collector reuses existing SMART cache only.

        The HTTP handler enforces same-origin JSON/CSRF. No command, path,
        serial override, configuration dump or disk refresh can be submitted.
        """
        if not self._dev_authorized(ip) or not self.dev_commands:
            return 403, {'ok': False, 'error': '存储调试接口未启用或客户端未授权'}, None
        if self.auth.required() and self.password_session(headers) is None:
            return 401, {'ok': False, 'error': '未登录', 'auth_required': True}, None
        if not isinstance(body, dict) or body != {'action': 'snapshot'}:
            return 400, {'ok': False, 'error': '仅支持固定只读 snapshot 查询'}, None
        return self._storage_debug_snapshot()

    def _storage_debug_snapshot(self, *, include_serial=False):
        """Share the IPC lock; only an explicitly selected check exports serials."""
        method = 'storage_identity_diagnostics' if include_serial else 'storage_name_diagnostics'
        collector = getattr(self.insights, method, None)
        if not callable(collector):
            return 503, {'ok': False, 'error': '存储名称采集器暂不可用'}, None
        # A single in-flight request bounds IPC pressure without touching the
        # hardware-control lock or changing any saved policy.
        if not self.storage_diagnostic_lock.acquire(blocking=False):
            return 409, {'ok': False, 'error': '已有存储名称查询正在进行'}, None
        try:
            identity = self.insights.snapshot().get('device', {}).get('identity', {})
            device = {key: identity.get(key) for key in ('name', 'hostname', 'name_source', 'name_reason')}
            snapshot = collector()
            return 200, {'ok': True, 'device': device, 'storage': snapshot}, None
        except Exception:
            return 503, {'ok': False, 'error': '存储名称暂不可只读确认'}, None
        finally:
            self.storage_diagnostic_lock.release()

    def _fan_debug_post(self, body, headers, ip):
        """Private diagnostics; retry alone may apply the existing saved policy.

        All actions use the HTTP handler's same-origin JSON/CSRF gate. Neither
        password-only open writes nor a pairing cookie opens this debug API:
        explicit, unexpired developer IP access and the command flag are needed.
        """
        if not self._dev_authorized(ip) or not self.dev_commands:
            return 403, {'ok': False, 'error': '风扇调试接口未启用或客户端未授权'}, None
        if self.auth.required() and self.password_session(headers) is None:
            return 401, {'ok': False, 'error': '未登录', 'auth_required': True}, None
        if not isinstance(body, dict):
            return 400, {'ok': False, 'error': '请求体必须是 JSON 对象'}, None
        action = body.get('action')
        fields = {'snapshot': {'action'}, 'result': {'action', 'id'},
                  'capture': {'action', 'duration_seconds', 'interval_ms'},
                  'retry': {'action', 'duration_seconds', 'interval_ms', 'confirmed'}}
        if not isinstance(action, str) or action not in fields or set(body) - fields[action]:
            return 400, {'ok': False, 'error': '风扇调试操作或参数无效'}, None
        try:
            if action == 'snapshot':
                return 200, {'ok': True, 'snapshot': self.fan_diagnostics.snapshot()}, None
            if action == 'result':
                identity = body.get('id')
                if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identity):
                    raise ValueError('诊断任务 id 无效')
                return 200, {'ok': True, 'job': self.fan_diagnostics.result(identity)}, None
            if action == 'retry' and body.get('confirmed') is not True:
                raise ValueError('重试风扇接管需要明确确认')
            duration, interval = body.get('duration_seconds', 45), body.get('interval_ms', 250)
            if action == 'capture':
                job = self.fan_diagnostics.start(duration_seconds=duration, interval_ms=interval)
                running = job.get('state') == 'running'
                return (202 if running else 503), {'ok': running, 'job': job}, None
            # Capture starts before the existing async apply worker, preserving
            # both the original owner and a failure followed by vendor recovery.
            if not self.operation_lock.acquire(blocking=False):
                return 409, {'ok': False, 'error': '已有设置正在保存或应用，请稍后重试'}, None
            try:
                if self.fan_policy is None:
                    raise ValueError('独立温控未启用')
                policy = self.fan_policy.snapshot()
                if policy.get('busy'):
                    return 409, {'ok': False, 'error': '风扇控制正在切换，请先采集或查询结果'}, None
                config = policy.get('config')
                channels = config.get('channels') if isinstance(config, dict) else None
                if not isinstance(channels, dict) or not any(
                        isinstance(item, dict) and item.get('mode') in ('manual', 'temperature')
                        for item in channels.values()):
                    raise ValueError('已保存的配置没有手动或自定义温控通道，请先保存配置')
                job = self.fan_diagnostics.start(duration_seconds=duration, interval_ms=interval)
                if job.get('state') != 'running':
                    return 503, {'ok': False, 'error': '诊断采集未启动，未重试接管', 'job': job}, None
                attempt = self.fan_policy.action('fan.policy.update',
                    {'config': copy.deepcopy(config), 'apply': True, 'confirmed': True})
                return (202 if attempt.get('ok') else 400), {
                    'ok': attempt.get('ok') is True, 'job': job,
                    'attempt': self._policy_result(attempt)}, None
            finally:
                self.operation_lock.release()
        except KeyError:
            return 404, {'ok': False, 'error': '诊断任务不存在或已被新任务替换'}, None
        except ValueError as error:
            return 400, {'ok': False, 'error': str(error)}, None
        except RuntimeError as error:
            return 409, {'ok': False, 'error': str(error)}, None

    def _auth_cookie(self, sid, clear=False):
        if clear:
            return '%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0' % AUTH_COOKIE
        return '%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d' % (
            AUTH_COOKIE, sid, self.auth.session_days() * 86400)

    def _panel_write(self, route, body, headers=None):
        if not isinstance(body, dict):
            raise ValueError('请求体必须是 JSON 对象')
        panel = self.panel
        if route in ACCESS_POST_ROUTES:
            return 200, self.integration.manage(route, body)
        if route in SERVICE_POST_ROUTES:
            return self._services_write(route, body)
        if route == '/api/ui':
            clean = panel.store.save_ui(body)
            return 200, {'ok': True, 'ui': clean}
        if route == '/api/config/save':
            return 200, panel.save_config(dict(body))
        if route == '/api/docker/action':
            identity, action = body.get('id'), body.get('action')
            if action not in ('start', 'stop', 'restart') or not isinstance(identity, str):
                raise ValueError('容器操作参数无效')
            if self.insights is None:
                raise ValueError('应用控制服务不可用')
            result = self.insights.action('container.action',
                                          {'id': identity, 'operation': action, 'confirmed': True})
            return 200, {'ok': result.get('ok') is True, 'error': result.get('reason') or result.get('error'),
                         'refresh_pending': bool(result.get('refresh_pending'))}
        if route == '/api/vm/action':
            identity, action = body.get('uuid'), body.get('action')
            if action not in ('start', 'shutdown', 'reboot', 'suspend', 'resume') or not isinstance(identity, str):
                raise ValueError('虚拟机操作参数无效')
            if self.insights is None:
                raise ValueError('虚拟机控制服务不可用')
            result = self.insights.action('vm.action', {'id': identity, 'operation': action, 'confirmed': True})
            return 200, {'ok': result.get('ok') is True, 'error': result.get('reason') or result.get('error'),
                         'refresh_pending': bool(result.get('refresh_pending'))}
        if route == '/api/r1/fan':
            if set(body) - {'config', 'apply', 'confirmed'} or not isinstance(body.get('config'), dict):
                raise ValueError('风扇配置参数无效')
            if self.fan_policy is None:
                raise ValueError('独立温控未启用')
            result = self.fan_policy.action('fan.policy.update', {
                'config': body['config'], 'apply': body.get('apply') is True,
                'confirmed': body.get('confirmed') is True})
            return (200 if result.get('ok') else 400), self._policy_result(result)
        if route == '/api/r1/fan/release':
            if self.fan_policy is None:
                raise ValueError('独立温控未启用')
            result = self.fan_policy.action('fan.policy.release', {'confirmed': body.get('confirmed') is True})
            return (200 if result.get('ok') else 400), self._policy_result(result)
        if route == '/api/r1/light':
            if set(body) - {'config', 'apply'} or not isinstance(body.get('config'), dict):
                raise ValueError('灯光配置参数无效')
            if self.light_policy is None:
                raise ValueError('灯光联动未启用')
            result = self.light_policy.action('light.policy.update',
                                              {'config': body['config'], 'apply': body.get('apply') is True})
            return (200 if result.get('ok') else 400), self._policy_result(result)
        if route == '/api/r1/rgb':
            if self.controller is None or set(body) - {'mode', 'color', 'brightness'} or not {'mode', 'color'} <= set(body):
                raise ValueError('灯光操作参数无效')
            result = self.controller.action('rgb.set', body)
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'error': None if result.get('ok') else '灯光操作未完成',
                                                        'state': self._state()}
        if route == '/api/r1/screen':
            action = body.get('action')
            if action == 'brightness':
                if type(body.get('value')) is not int or not 5 <= body['value'] <= 100:
                    raise ValueError('亮度需要 5–100%')
                payload = {'value': body['value']}
                name = 'screen.brightness'
            elif action in ('sleep', 'wake'):
                payload, name = {}, 'screen.' + action
            else:
                raise ValueError('不支持的屏幕操作')
            if self.controller is None:
                raise ValueError('屏幕控制不可用')
            result = self.controller.action(name, payload)
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'error': None if result.get('ok') else '屏幕操作未完成，请在小屏检查',
                                                        'state': self._state()}
        if route == '/api/r1/settings':
            changes = body.get('settings')
            if (set(body) != {'settings'} or not isinstance(changes, dict)
                    or not changes or set(changes) - SETTINGS):
                raise ValueError('屏幕与外观设置无效')
            if self.controller is None:
                raise ValueError('设置服务不可用')
            result = self.controller.action('settings.update', {'settings': changes})
            saved = result.get('settings_saved') is True
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'saved': saved,
                                                        'error': None if result.get('ok') else (
                                                            '设置已保存但未应用到设备，请在小屏检查' if saved else
                                                            '设置未保存，请检查显示项目或设置数据卷'),
                                                        'state': self._state()}
        if route == '/api/r1/monitor':
            changes = body.get('settings')
            if not isinstance(changes, dict) or not changes or self.monitoring is None:
                raise ValueError('统计设置无效')
            result = self.monitoring.action('monitor.settings.update', {'settings': changes})
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'saved': result.get('settings_saved') is True,
                                                        'error': None if result.get('ok') else '统计设置未能保存',
                                                        'settings': self.monitoring.snapshot().get('settings')}
        if route == '/api/r1/power':
            operation = body.get('operation')
            if operation not in ('reboot', 'poweroff'):
                raise ValueError('电源操作参数无效')
            if body.get('confirmed') is not True:
                raise ValueError('电源操作需要明确确认')
            if self.controller is None:
                raise ValueError('电源控制不可用')
            result = self.controller.action('power.request', {'operation': operation, 'confirmed': True})
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'error': None if result.get('ok') else '电源操作未开始，请在小屏检查',
                                                        'state': self._state()}
        if route == '/api/r1/power/cancel':
            if self.controller is None:
                raise ValueError('电源控制不可用')
            result = self.controller.action('power.cancel', {})
            return (200 if result.get('ok') else 400), {'ok': result.get('ok') is True,
                                                        'error': None if result.get('ok') else '取消失败，操作可能已开始',
                                                        'state': self._state()}
        if route == '/api/r1/auth':
            action = body.get('action')
            if action == 'change':
                result = self.auth.change(str(body.get('old_password') or ''),
                                          str(body.get('new_password') or ''))
                if not result.get('ok'):
                    return 400, result
                cookie = self._auth_cookie(result['sid']) if result.get('sid') else None
                return 200, {'ok': True, 'cleared': bool(result.get('cleared')),
                             'config': self.auth.public_config()}, cookie
            if action == 'enable':
                self.auth.set_enabled(True)
                return 200, {'ok': True, 'config': self.auth.public_config()}
            if action == 'disable':
                self.auth.set_enabled(False)
                return 200, {'ok': True, 'config': self.auth.public_config()}
            if action == 'session_days':
                days = body.get('days')
                if type(days) is not int or not 1 <= days <= 30:
                    raise ValueError('会话天数需要 1–30')
                cookie = (headers or {}).get('Cookie') or ''
                self.auth.set_session_days(days, keep=self.auth.cookie_sid(cookie))
                return 200, {'ok': True, 'config': self.auth.public_config()}
            raise ValueError('不支持的登录设置')
        if route in ('/api/notify/save', '/api/notify/test'):
            raise ValueError('R1 Toolbox 不发送外部通知')
        if route in ('/api/idrac/save', '/api/idrac/test', '/api/idrac/refresh'):
            raise ValueError('R1 不是戴尔 iDRAC 平台')
        if route in ('/api/disk_scan/now', '/api/scan_log/clear'):
            raise ValueError('R1 Toolbox 只做只读 SMART 观测，不执行巡检自检')
        if route == '/api/update/install':
            raise ValueError('请通过更新 Compose 镜像升级，面板内不自更新')
        return 404, {'ok': False, 'error': 'not_found'}

    @staticmethod
    def _policy_result(result):
        return {'ok': result.get('ok') is True, 'saved': result.get('settings_saved') is True,
                'pending': bool(result.get('pending')),
                'error': None if result.get('ok') else result.get('error') or '配置未应用',
                'fan_policy': result.get('fan_policy'), 'light_policy': result.get('light_policy')}

    def start(self):
        with self.lock:
            if self.server:
                return self
            outer = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.0'
                server_version = 'R1Web'
                sys_version = ''
                # _Reader sets a 2 s socket timeout while parsing a request.
                # That timeout also governs writes; a large panel response over
                # a slow link would otherwise be truncated while Content-Length
                # still advertises the full body (Chrome: ERR_CONTENT_LENGTH_MISMATCH).
                RESPONSE_TIMEOUT = 60

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

                def send(self, code, value, cookie=None, mime='application/json; charset=utf-8', public=False, extra_headers=None):
                    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                    try:
                        self.connection.settimeout(self.RESPONSE_TIMEOUT)
                    except OSError:
                        pass
                    self.send_response(code)
                    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                           "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
                    for key, content in {'Content-Type': mime, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store',
                                         'Connection': 'close', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                                         'Content-Security-Policy': csp}.items():
                        self.send_header(key, content)
                    if cookie:
                        self.send_header('Set-Cookie', cookie)
                    for key, content in (extra_headers or {}).items():
                        self.send_header(key, content)
                    if public:
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                    if mime.startswith('text/csv'):
                        self.send_header('Content-Disposition', 'attachment; filename="r1-monitoring.csv"')
                    elif mime.startswith('text/html') and code == 200 and self.path.startswith('/api/'):
                        self.send_header('Content-Disposition', 'attachment; filename="r1-report.html"')
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass
                    self.close_connection = True

                def valid(self, post=False, public=False, panel=False):
                    for name in ('Host', 'Origin', 'Cookie', 'Content-Length', 'Connection', 'X-R1-CSRF', 'X-FNM-Request'):
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
                    if host not in expected or (not public and self.headers.get('Origin') is not None and self.headers.get('Origin') != 'http://' + host):
                        self.send(403, {'error': '请求来源不允许'})
                        return False
                    if post and self.headers.get('Origin') != 'http://' + host:
                        self.send(403, {'error': '写入请求需要同源网页'})
                        return False
                    if urllib.parse.urlsplit(self.path).query and not (public or panel):
                        self.send(400, {'error': '接口不接受查询参数'})
                        return False
                    return True

                def body(self, limit=8192):
                    length = self.headers.get('Content-Length', '')
                    if not re.fullmatch(r'[0-9]{1,6}', length) or not 0 < int(length) <= limit:
                        raise ValueError('请求长度无效')
                    if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
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

                def machine(self, parsed):
                    """Bearer-only machine routes; browser cookies grant nothing here."""
                    duplicates = {'authorization', 'content-type', 'mcp-protocol-version',
                                  'mcp-method', 'mcp-name'}
                    for name in self.headers:
                        if (name.lower() in duplicates or name.lower().startswith('mcp-')) and len(self.headers.get_all(name, [])) > 1:
                            self.send(400, {'error': '重复机器接口请求头'})
                            return
                    # No Origin is valid for native clients. A supplied Origin
                    # must still match the actual local Host, as on the panel.
                    if not self.valid(panel=True):
                        return
                    route, peer = parsed.path, self.client_address[0]
                    if parsed.fragment or (parsed.query and route != '/api/integration/status'):
                        self.send(400, {'error': '该机器接口不接受 URL 参数'})
                        return
                    principal, reason = outer.api_access.authorize(
                        self.headers.get('Authorization'), peer,
                        required_scope='read' if route == '/api/integration/status' else None)
                    if principal is None:
                        status = 503 if reason == 'storage_unavailable' else 403 if reason in (
                            'disabled', 'key_disabled', 'peer_not_lan', 'peer_not_allowed', 'scope_denied') else 401
                        self.send(status, {'ok': False, 'error': {'code': reason, 'message': 'API 访问未授权'}},
                                  extra_headers={'WWW-Authenticate': 'Bearer realm="r1-toolbox"'} if status == 401 else None)
                        return
                    allowed = 'GET' if route in ('/api/integration/status', '/api/integration/capabilities') else 'POST'
                    if self.command != allowed:
                        self.send(405, {'error': '请求方法不支持'}, extra_headers={'Allow': allowed})
                        return
                    started = time.monotonic()
                    if route == '/api/integration/capabilities':
                        try:
                            payload = outer.integration.capabilities(principal)
                            status = 200
                        except Exception:
                            status, payload = 503, {'ok': False, 'error': '能力目录暂不可用'}
                        outer.integration.record(principal, 'integration/capabilities', peer, payload, started=started)
                        self.send(status, payload)
                        return
                    if route == '/api/integration/status':
                        try:
                            params = outer._panel_query(parsed.query, {'section'})
                            result = outer.integration.mcp.call_tool('get_nas_status', params, principal)
                            payload = result.get('structuredContent') or {'ok': False, 'error': '状态不可用'}
                            status = 400 if result.get('isError') else 200
                        except (ValueError, TypeError):
                            status, payload = 400, {'ok': False, 'error': '状态参数无效'}
                        except Exception:
                            status, payload = 503, {'ok': False, 'error': '状态来源暂不可用'}
                        outer.integration.record(principal, 'get_nas_status', peer, payload, started=started)
                        self.send(status, payload)
                        return
                    if route == '/mcp':
                        if not _mcp_accepts(self.headers.get_all('Accept', [])):
                            self.send(406, {'error': 'MCP Accept 需要包含 application/json 和 text/event-stream'})
                            return
                    try:
                        body = self.body(limit=65536)
                    except (ValueError, UnicodeError, RecursionError, OverflowError):
                        self.send(400, {'jsonrpc': '2.0', 'id': None,
                                       'error': {'code': -32700, 'message': '请求 JSON 或长度无效'}} if route == '/mcp'
                                  else {'ok': False, 'error': '请求 JSON 或长度无效'})
                        return
                    operation = 'tools/call'
                    if route == '/mcp':
                        try:
                            invalid = validate_http_metadata(body, self.headers)
                            status, payload = invalid if invalid is not None else outer.integration.mcp.handle(
                                body, principal, protocol_version=self.headers.get('MCP-Protocol-Version'))
                        except Exception:
                            status, payload = 503, {'jsonrpc': '2.0', 'id': None,
                                'error': {'code': -32603, 'message': 'MCP 服务暂不可用'}}
                        if isinstance(body, dict):
                            operation = body.get('method') if isinstance(body.get('method'), str) else 'tools/call'
                            params = body.get('params')
                            if operation == 'tools/call' and isinstance(params, dict) and type(params.get('name')) is str:
                                operation = params['name']
                        outcome = payload.get('result', payload) if isinstance(payload, dict) else {}
                        outer.integration.record(principal, operation, peer, outcome, started=started)
                        self.send(status, b'' if payload is None else payload)
                        return
                    if (not isinstance(body, dict) or set(body) != {'tool', 'arguments'}
                            or type(body['tool']) is not str or not isinstance(body['arguments'], dict)):
                        self.send(400, {'ok': False, 'error': '仅接受 tool 和 arguments 字段'})
                        return
                    try:
                        payload = outer.integration.mcp.call_tool(body['tool'], body['arguments'], principal)
                        status = 200
                    except Exception:
                        status, payload = 503, {'ok': False, 'error': '集成操作暂不可用'}
                    outer.integration.record(principal, body['tool'], peer, payload, started=started)
                    self.send(status, payload)

                def do_GET(self):
                    parsed = urllib.parse.urlsplit(self.path)
                    route = parsed.path
                    if route in MACHINE_ROUTES:
                        self.machine(parsed)
                        return
                    if route in OWNER_READ_ROUTES and parsed.fragment:
                        self.send(400, {'error': 'API 管理不接受 URL 参数'})
                        return
                    public = route in PUBLIC_ROUTES
                    panel = route in PANEL_GET_ROUTES
                    if not self.valid(public=public, panel=panel):
                        return
                    if public:
                        try:
                            value, mime = outer._public(route, parsed.query)
                            self.send(200, value, mime=mime, public=True)
                        except ValueError:
                            self.send(400, {'error': '查询参数无效'}, public=True)
                        except Exception:
                            self.send(503, {'error': '监控来源暂不可用'}, public=True)
                        return
                    if route in PANEL_ASSETS:
                        name, mime = PANEL_ASSETS[route]
                        if route != '/login.html' and outer.auth.required() and outer.password_session(self.headers) is None:
                            name, mime = 'login.html', 'text/html; charset=utf-8'
                        elif route == '/login.html' and not outer.auth.required():
                            # No password protection: the panel is public, so the
                            # login page would only offer a setup form that now
                            # requires pairing.  Send the visitor to the panel.
                            name, mime = 'index.html', 'text/html; charset=utf-8'
                        path = outer.asset_dir / name
                        if not path.is_symlink() and path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                            self.send(200, path.read_bytes(), mime=mime)
                        else:
                            self.send(503, {'error': '网页资源不可用'})
                        return
                    if panel:
                        if route not in ('/api/auth/config', '/api/auth/session') and not outer.panel_readable(self.headers, peer=self.client_address[0]):
                            self.send(401, {'ok': False, 'error': '未登录', 'auth_required': True})
                            return
                        try:
                            status, value, mime = outer.panel_get(route, parsed.query, self.headers, peer=self.client_address[0])
                        except ValueError:
                            status, value, mime = 400, {'ok': False, 'error': '查询参数无效'}, 'application/json; charset=utf-8'
                        except Exception:
                            status, value, mime = 503, {'ok': False, 'error': '数据来源暂不可用'}, 'application/json; charset=utf-8'
                        self.send(status, value, mime=mime)
                        return
                    _, session = self.authenticated()
                    if session is None:
                        return
                    if route == '/api/session':
                        try:
                            self.send(200, {'csrf': session['csrf'], 'state': outer._state(), 'http': True,
                                **({'fan_policy': outer.fan_policy.snapshot()} if outer.fan_policy is not None else {}),
                                **({'light_policy': outer.light_policy.snapshot()} if outer.light_policy is not None else {}),
                                **({'analytics': outer.monitoring.snapshot()} if outer.monitoring is not None else {})})
                        except Exception:
                            self.send(503, {'error': '设置服务暂不可用'})
                    else:
                        self.send(404, {'error': '接口不存在'})

                def do_POST(self):
                    parsed = urllib.parse.urlsplit(self.path)
                    route = parsed.path
                    if route in MACHINE_ROUTES:
                        self.machine(parsed)
                        return
                    panel = route in PANEL_POST_ROUTES
                    if not self.valid(post=True, panel=panel):
                        return
                    if (route in ('/api/dev/diagnostics', '/api/dev/media', '/api/dev/storage/probe', '/api/r1/business/refresh') or route in MANAGEMENT_POST_ROUTES) and (parsed.query or parsed.fragment):
                        self.send(400, {'error': '该接口不接受 URL 参数'})
                        return
                    try:
                        body = self.body(limit=65536 if panel else 8192)
                    except (ValueError, UnicodeError, RecursionError):
                        self.send(400, {'error': '请求格式或长度无效'})
                        return
                    if panel:
                        # fnmonitor's cross-site gate: JSON plus a custom header the
                        # server never allows through CORS preflight.  A pairing
                        # session's CSRF token is accepted as an equivalent proof
                        # for callers that already hold one.
                        marker = (self.headers.get('X-FNM-Request') or '').strip() == '1'
                        supplied = self.headers.get('X-R1-CSRF', '')
                        _, pair = outer._session(self.headers.get('Cookie') or '')
                        csrf_ok = bool(pair) and re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied) and secrets.compare_digest(supplied, pair['csrf'])
                        if not marker and not csrf_ok:
                            self.send(403, {'ok': False, 'error': 'csrf_rejected',
                                            'message': '请求被拒绝：缺少 X-FNM-Request 头或会话校验失败'})
                            return
                        try:
                            status, value, cookie = outer.panel_post(route, body, self.headers,
                                                                     self.client_address[0])
                        except Exception:
                            status, value, cookie = 503, {'ok': False, 'error': '服务暂不可用'}, None
                        self.send(status, value, cookie=cookie)
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
                            status = 200 if result['ok'] else 400
                        except ValueError:
                            status, result = 400, {'error': '操作或参数不受网页支持，设备能力须先在小屏验证'}
                        except Exception:
                            status, result = 503, {'error': '设置服务暂不可用，未确认应用结果'}
                        finally:
                            outer.operation_lock.release()
                        # A client can submit again as soon as it reads this
                        # response. Release before publishing that completion,
                        # rather than spuriously rejecting the next request.
                        self.send(status, result)
                    else:
                        self.send(404, {'error': '接口不存在'})

                def unsupported(self):
                    parsed = urllib.parse.urlsplit(self.path)
                    if parsed.path in MACHINE_ROUTES:
                        self.machine(parsed)
                        return
                    self.send(405, {'error': '请求方法不支持'})

                def do_OPTIONS(self):
                    if urllib.parse.urlsplit(self.path).path not in PUBLIC_ROUTES:
                        self.unsupported()
                    elif self.valid(public=True):
                        self.send(200, {'read_only': True}, public=True)

                do_PUT = do_PATCH = do_DELETE = do_HEAD = unsupported

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
        self.fan_diagnostics.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
