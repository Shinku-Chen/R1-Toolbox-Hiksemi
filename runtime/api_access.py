"""Scoped LAN integration keys, with no reusable secret in persistent state.

The browser may receive a newly created key once. Every later response is a
projection of metadata only; authorization never trusts proxy forwarding headers.
This store is shared by the HTTP integration routes and MCP transport.
"""

import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time


SCOPES = ('read', 'fan', 'light', 'screen', 'power', 'media')
SCOPE_CATALOG = (
    {'id': 'read', 'label': '读取状态', 'description': '读取设备、磁盘、温度与策略状态。'},
    {'id': 'fan', 'label': '风扇控制', 'description': '修改风扇策略或恢复原厂管理。'},
    {'id': 'light', 'label': '灯光控制', 'description': '修改灯效与 RGB 设置。'},
    {'id': 'screen', 'label': '屏幕控制', 'description': '控制小屏与屏幕保护设置。'},
    {'id': 'power', 'label': '电源操作', 'description': '请求或取消带延时的重启与关机。'},
    {'id': 'media', 'label': '媒体查询', 'description': '显式查询原厂媒体进度，可能写原厂完成状态。'},
)
AUDIT_OPERATIONS = frozenset((
    'get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan',
    'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings',
    'request_power', 'cancel_power', 'initialize', 'server/discover', 'ping',
    'tools/list', 'tools/call', 'integration/status', 'integration/action',
    'config', 'key.create', 'key.revoke', 'authenticate',
))
AUDIT_OUTCOMES = frozenset(('success', 'error', 'denied', 'busy', 'unavailable'))
MAX_ACTIVE_KEYS, MAX_KEYS, MAX_AUDIT = 32, 64, 100
_MAX_FILE_BYTES = 262144
_TOKEN_RE = re.compile(r'r1_[A-Za-z0-9_-]{43}\Z', re.ASCII)
_KEY_ID_RE = re.compile(r'[0-9a-f]{24}\Z', re.ASCII)
_DIGEST_RE = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))
_PRIVATE_V6 = ipaddress.ip_network('fc00::/7')
_KEY_FIELDS = frozenset((
    'id', 'name', 'scopes', 'allowed_ips', 'created_at', 'expires_at',
    'revoked_at', 'last_used_at', 'digest',
))
_AUDIT_FIELDS = frozenset((
    'timestamp', 'key_id', 'operation', 'peer', 'outcome', 'duration_ms',
))


def normalize_peer(peer):
    """Return a canonical allowed LAN address, never a hostname or network range."""
    if not isinstance(peer, str) or peer != peer.strip() or '%' in peer or len(peer) > 64:
        return None
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        return str(address) if any(address in net for net in _PRIVATE_V4) else None
    return str(address) if address == ipaddress.IPv6Address('::1') or address in _PRIVATE_V6 else None


def _timestamp(value, nullable=False):
    return (value is None and nullable) or (type(value) is int and 0 <= value <= 253402300799)


def _duration(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value <= 3600000 and math.isfinite(value))


def _label(value):
    return (isinstance(value, str) and value == value.strip() and 1 <= len(value) <= 64
            and all(character.isprintable() for character in value))


def _scopes(value):
    return (isinstance(value, list) and 1 <= len(value) <= len(SCOPES)
            and all(isinstance(item, str) and item in SCOPES for item in value)
            and len(set(value)) == len(value))


def _ips(value):
    return (isinstance(value, list) and len(value) <= 32
            and all(normalize_peer(item) == item and item is not None for item in value)
            and len(set(value)) == len(value))


def _reject_constants(_value):
    raise ValueError('invalid numeric constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


class ApiAccessStore:
    """Persistent scoped bearer keys, disabled until explicitly enabled.

    Every operation reloads the small state file so expiry, revocation, file
    removal and malformed state take effect immediately. A malformed file is
    never silently replaced by an empty store, which could hide a lost revoke.
    """

    def __init__(self, state_dir, *, clock=None):
        self.path = Path(state_dir) / 'api-access.json'
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self._write_failed = False

    def _now(self):
        value = self.clock()
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 253402300799 or not math.isfinite(value)):
            raise ValueError('系统时间不可用')
        now = int(value)
        if not _timestamp(now):
            raise ValueError('系统时间不可用')
        return now

    @staticmethod
    def _empty():
        return {'version': 1, 'enabled': False, 'keys': [], 'audit': []}

    @staticmethod
    def _valid_key(row):
        return (isinstance(row, dict) and set(row) == _KEY_FIELDS
                and isinstance(row['id'], str) and _KEY_ID_RE.fullmatch(row['id'])
                and _label(row['name']) and _scopes(row['scopes']) and _ips(row['allowed_ips'])
                and all(_timestamp(row[field], nullable=True) for field in (
                    'expires_at', 'revoked_at', 'last_used_at'))
                and _timestamp(row['created_at'])
                and isinstance(row['digest'], str) and _DIGEST_RE.fullmatch(row['digest']))

    @staticmethod
    def _valid_audit(row):
        return (isinstance(row, dict) and set(row) == _AUDIT_FIELDS
                and _timestamp(row['timestamp'])
                and (row['key_id'] is None or isinstance(row['key_id'], str)
                     and _KEY_ID_RE.fullmatch(row['key_id']))
                and isinstance(row['operation'], str) and row['operation'] in AUDIT_OPERATIONS
                and (row['peer'] is None or normalize_peer(row['peer']) == row['peer'])
                and isinstance(row['outcome'], str) and row['outcome'] in AUDIT_OUTCOMES
                and type(row['duration_ms']) is int and 0 <= row['duration_ms'] <= 3600000)

    def _read(self):
        if self._write_failed:
            return self._empty(), False
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return self._empty(), True
        except OSError:
            return self._empty(), False
        try:
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if (self.path.is_symlink() or not stat.S_ISREG(info.st_mode)
                        or info.st_size > _MAX_FILE_BYTES
                        or os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o600):
                    return self._empty(), False
                text = stream.read(_MAX_FILE_BYTES + 1)
                if len(text) > _MAX_FILE_BYTES:
                    return self._empty(), False
                data = json.loads(text, parse_constant=_reject_constants, object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {'version', 'enabled', 'keys', 'audit'}
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['enabled']) is not bool
                    or not isinstance(data['keys'], list) or len(data['keys']) > MAX_KEYS
                    or not all(self._valid_key(row) for row in data['keys'])
                    or len({row['id'] for row in data['keys']}) != len(data['keys'])
                    or len({row['digest'] for row in data['keys']}) != len(data['keys'])
                    or not isinstance(data['audit'], list) or len(data['audit']) > MAX_AUDIT
                    or not all(self._valid_audit(row) for row in data['audit'])):
                return self._empty(), False
            return data, True
        except (OSError, ValueError, UnicodeError, RecursionError):
            return self._empty(), False

    def _write(self, state):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError('访问配置不可写')
            descriptor, temporary = tempfile.mkstemp(prefix='.api-access-', dir=str(self.path.parent))
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(state, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name != 'nt':
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError:
            # Persistence errors cannot leave an enabled in-memory copy usable.
            self._write_failed = True
            raise OSError('访问配置保存失败') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _mutable(self):
        data, valid = self._read()
        if not valid:
            raise OSError('访问配置不可用，请检查持久存储')
        return data

    @staticmethod
    def _key_status(row, now):
        if row['revoked_at'] is not None:
            return 'revoked'
        if row['expires_at'] is not None and row['expires_at'] <= now:
            return 'expired'
        return 'active'

    @classmethod
    def _public_key(cls, row, now):
        result = {field: value for field, value in row.items() if field != 'digest'}
        result['scopes'] = list(row['scopes'])
        result['allowed_ips'] = list(row['allowed_ips'])
        result['status'] = cls._key_status(row, now)
        return result

    def snapshot(self):
        with self.lock:
            data, valid = self._read()
            now = self._now()
            return {
                'enabled': bool(valid and data['enabled']), 'storage_ok': valid,
                'keys': [self._public_key(row, now) for row in data['keys']],
                'scope_catalog': [dict(item) for item in SCOPE_CATALOG],
                'audit': [dict(row) for row in reversed(data['audit'])],
            }

    def configure(self, body):
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise ValueError('仅接受 enabled 布尔值')
        with self.lock:
            data = self._mutable()
            data['enabled'] = body['enabled']
            self._write(data)
            return self.snapshot()

    def create_key(self, body):
        if (not isinstance(body, dict) or set(body) - {'name', 'scopes', 'allowed_ips', 'expires_days'}
                or not _label(body.get('name'))):
            raise ValueError('密钥名称或字段无效')
        scopes = body.get('scopes', ['read'])
        days = body.get('expires_days', 90)
        supplied_ips = body.get('allowed_ips', [])
        if not _scopes(scopes):
            raise ValueError('密钥权限无效')
        if type(days) is not int or not 0 <= days <= 365:
            raise ValueError('有效天数须为 0 至 365 的整数')
        if not isinstance(supplied_ips, list) or len(supplied_ips) > 32:
            raise ValueError('客户端须为精确的局域网 IP')
        ips = [normalize_peer(item) for item in supplied_ips]
        if any(item is None for item in ips) or len(set(ips)) != len(ips):
            raise ValueError('客户端须为不重复的精确局域网 IP')
        with self.lock:
            data = self._mutable()
            now = self._now()
            if sum(self._key_status(row, now) == 'active' for row in data['keys']) >= MAX_ACTIVE_KEYS:
                raise ValueError('最多保留 32 个有效密钥')
            expires_at = now + days * 86400 if days else None
            if not _timestamp(expires_at, nullable=True):
                raise ValueError('有效期超出支持范围')
            # Preserve active grants; discard only oldest inactive history at the cap.
            while len(data['keys']) >= MAX_KEYS:
                inactive = [row for row in data['keys'] if self._key_status(row, now) != 'active']
                if not inactive:
                    raise ValueError('密钥记录已达上限')
                oldest = min(inactive, key=lambda row: row['created_at'])
                data['keys'].remove(oldest)
            token = 'r1_' + secrets.token_urlsafe(32)
            key_id = secrets.token_hex(12)
            while any(row['id'] == key_id for row in data['keys']):
                key_id = secrets.token_hex(12)
            row = {
                'id': key_id, 'name': body['name'], 'scopes': list(scopes),
                'allowed_ips': ips, 'created_at': now,
                'expires_at': expires_at,
                'revoked_at': None, 'last_used_at': None,
                'digest': hashlib.sha256(token.encode('ascii')).hexdigest(),
            }
            data['keys'].append(row)
            self._write(data)
            result = self._public_key(row, now)
            result['token'] = token
            return result

    def revoke_key(self, key_id):
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise ValueError('密钥标识无效')
        with self.lock:
            data = self._mutable()
            now = self._now()
            row = next((row for row in data['keys'] if row['id'] == key_id), None)
            if row is None:
                raise ValueError('密钥不存在')
            if row['revoked_at'] is None:
                row['revoked_at'] = now
                self._write(data)
            return self._public_key(row, now)

    def authorize(self, authorization_header, peer, required_scope=None):
        canonical = normalize_peer(peer)
        if canonical is None:
            return None, 'peer_not_lan'
        if required_scope is not None and (not isinstance(required_scope, str) or required_scope not in SCOPES):
            return None, 'invalid_scope'
        with self.lock:
            data, valid = self._read()
            if not valid:
                return None, 'storage_unavailable'
            if not data['enabled']:
                return None, 'disabled'
            if (not isinstance(authorization_header, str) or len(authorization_header) != 53
                    or authorization_header[:7].lower() != 'bearer '
                    or not _TOKEN_RE.fullmatch(authorization_header[7:])):
                return None, 'invalid_key'
            digest = hashlib.sha256(authorization_header[7:].encode('ascii')).hexdigest()
            matched = None
            for row in data['keys']:
                if hmac.compare_digest(digest, row['digest']):
                    matched = row
            if matched is None:
                return None, 'invalid_key'
            status = self._key_status(matched, self._now())
            if status != 'active':
                return None, 'key_' + status
            if matched['allowed_ips'] and canonical not in matched['allowed_ips']:
                return None, 'peer_not_allowed'
            if required_scope is not None and required_scope not in matched['scopes']:
                return None, 'scope_denied'
            return {'key_id': matched['id'], 'name': matched['name'], 'scopes': list(matched['scopes'])}, 'ok'

    def record(self, principal, operation, peer, outcome, duration_ms=0):
        """Persist only a known operation, outcome and address, never its arguments."""
        if (not isinstance(operation, str) or operation not in AUDIT_OPERATIONS
                or not isinstance(outcome, str) or outcome not in AUDIT_OUTCOMES
                or not _duration(duration_ms)):
            raise ValueError('审计项目无效')
        with self.lock:
            data = self._mutable()
            now = self._now()
            key_id = principal.get('key_id') if isinstance(principal, dict) else None
            row = next((row for row in data['keys'] if row['id'] == key_id), None)
            key_id = row['id'] if row is not None else None
            if row is not None:
                row['last_used_at'] = now
            data['audit'].append({
                'timestamp': now, 'key_id': key_id, 'operation': operation,
                'peer': normalize_peer(peer), 'outcome': outcome, 'duration_ms': int(duration_ms),
            })
            data['audit'] = data['audit'][-MAX_AUDIT:]
            self._write(data)
