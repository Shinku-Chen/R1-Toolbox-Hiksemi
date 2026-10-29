"""Scoped LAN integration keys, with no reusable secret in persistent state.

The browser receives a new or rotated key once. Every later response is a
projection of metadata only; authorization never trusts proxy forwarding headers.
This store is shared by the HTTP integration routes and MCP transport.
"""

import copy
import hashlib
import hmac
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

from api_permissions import (LEGACY_SCOPES, ROLE_CATALOG, SCOPES, SCOPE_BY_TOOL,
                             SCOPE_CATALOG, valid_file_roots as _file_roots,
                             valid_tool_grants as _tool_grants)
from network_access import normalize_peer, normalize_rule, normalize_rules, peer_matches

AUDIT_OPERATIONS = frozenset((
    'get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan',
    'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings',
    'request_power', 'cancel_power', 'initialize', 'server/discover', 'ping',
    'tools/list', 'tools/call', 'integration/status', 'integration/action',
    'integration/capabilities',
    'config', 'key.create', 'key.revoke', 'key.update', 'key.enable', 'key.rotate',
    'authenticate',
)) | frozenset(SCOPE_BY_TOOL)
AUDIT_OUTCOMES = frozenset(('success', 'error', 'denied', 'busy', 'unavailable'))
MAX_ACTIVE_KEYS, MAX_KEYS, MAX_AUDIT = 32, 64, 100
_MAX_FILE_BYTES = 262144
_TOKEN_RE = re.compile(r'r1_[A-Za-z0-9_-]{43}\Z', re.ASCII)
_KEY_ID_RE = re.compile(r'[0-9a-f]{24}\Z', re.ASCII)
_DIGEST_RE = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_ROLES = frozenset(item['id'] for item in ROLE_CATALOG)
_KEY_FIELDS = frozenset((
    'id', 'name', 'scopes', 'allowed_ips', 'created_at', 'expires_at',
    'revoked_at', 'last_used_at', 'digest',
))
_POLICY_FIELDS = frozenset(('policy_version', 'role', 'file_roots'))
_V3_FIELDS = frozenset(('disabled',))
_V4_FIELDS = frozenset(('tool_grants', 'docker_projects', 'grant_revision', 'credential_generation'))
_REVISION_FIELDS = ('policy_version', 'role', 'scopes', 'file_roots',
                    'tool_grants', 'docker_projects', 'allowed_ips', 'expires_at', 'disabled', 'revoked_at')
_MAX_REVISION = 2 ** 53 - 1
_STORE_LOCKS = {}
_STORE_LOCKS_GUARD = threading.Lock()
_AUDIT_FIELDS = frozenset((
    'timestamp', 'key_id', 'operation', 'peer', 'outcome', 'duration_ms',
))


def _timestamp(value, nullable=False):
    return (value is None and nullable) or (type(value) is int and 0 <= value <= 253402300799)


def _duration(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value <= 3600000 and math.isfinite(value))


def _label(value):
    return (isinstance(value, str) and value == value.strip() and 1 <= len(value) <= 64
            and all(character.isprintable() for character in value))


def _scopes(value, allowed=SCOPES):
    return (isinstance(value, list) and 1 <= len(value) <= len(allowed)
            and all(isinstance(item, str) and item in allowed for item in value)
            and len(set(value)) == len(value))


def _ips(value):
    return (isinstance(value, list) and len(value) <= 32
            and all(normalize_rule(item) == item and item is not None for item in value)
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
        # Reopened adapters in this process must serialize the same pathname,
        # otherwise a rotation could overwrite a concurrently saved revoke.
        lock_path = os.path.normcase(os.path.abspath(os.fspath(self.path)))
        with _STORE_LOCKS_GUARD:
            self.lock = _STORE_LOCKS.setdefault(lock_path, threading.RLock())
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
        return {'version': 4, 'enabled': False, 'keys': [], 'audit': []}

    @staticmethod
    def _valid_key(row, version=4):
        fields = _KEY_FIELDS if version == 1 else _KEY_FIELDS | _POLICY_FIELDS
        if version >= 3:
            fields |= _V3_FIELDS
        if version == 4:
            fields |= _V4_FIELDS
        valid = (isinstance(row, dict) and set(row) == fields
                and isinstance(row['id'], str) and _KEY_ID_RE.fullmatch(row['id'])
                and _label(row['name']) and _scopes(row['scopes']) and _ips(row['allowed_ips'])
                and all(_timestamp(row[field], nullable=True) for field in (
                    'expires_at', 'revoked_at', 'last_used_at'))
                and _timestamp(row['created_at'])
                and isinstance(row['digest'], str) and _DIGEST_RE.fullmatch(row['digest']))
        if not valid:
            return False
        if version >= 3 and type(row['disabled']) is not bool:
            return False
        if version == 4 and any(type(row[name]) is not int or not 1 <= row[name] <= _MAX_REVISION
                                for name in ('grant_revision', 'credential_generation')):
            return False
        if version == 1:
            return _scopes(row['scopes'], LEGACY_SCOPES)
        if type(row['policy_version']) is not int:
            return False
        if row['policy_version'] == 1:
            return (row['role'] == 'legacy' and row['file_roots'] == []
                    and _scopes(row['scopes'], LEGACY_SCOPES)
                    and (version < 4 or row['tool_grants'] == [] and row['docker_projects'] == []))
        if row['policy_version'] == 2:
            return (isinstance(row['role'], str) and row['role'] in _ROLES
                    and _file_roots(row['file_roots'], row['role'], row['scopes'])
                    and (version < 4 or row['tool_grants'] == [] and row['docker_projects'] == []))
        return (version == 4 and row['policy_version'] == 3
                and _tool_grants(row['tool_grants'], row['role'], row['scopes'],
                                 row['file_roots'], row['docker_projects']))

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
            return self._empty(), not self.path.is_symlink()
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
                    or type(data['version']) is not int or data['version'] not in (1, 2, 3, 4)
                    or type(data['enabled']) is not bool
                    or not isinstance(data['keys'], list) or len(data['keys']) > MAX_KEYS
                    or not all(self._valid_key(row, data['version']) for row in data['keys'])
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
            # Pure reads retain v1/v2/v3 bytes. A real write upgrades the complete
            # state in the same fsync/replace transaction as the requested change.
            state = dict(state, version=4, keys=[self._policy_row(row) for row in state['keys']])
            if not all(self._valid_key(row) for row in state['keys']):
                raise OSError('访问配置不可写')
            serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            if len(serialized.encode('utf-8')) > _MAX_FILE_BYTES:
                raise OSError('访问配置超出存储上限')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError('访问配置不可写')
            descriptor, temporary = tempfile.mkstemp(prefix='.api-access-', dir=str(self.path.parent))
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                stream.write(serialized)
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
        if row.get('disabled', False):
            return 'disabled'
        return 'active'

    @classmethod
    def _public_key(cls, row, now):
        row = cls._policy_row(row)
        result = {field: value for field, value in row.items() if field != 'digest'}
        result['scopes'] = list(row['scopes'])
        result['allowed_ips'] = list(row['allowed_ips'])
        result['file_roots'] = [dict(root) for root in row['file_roots']]
        result['tool_grants'] = list(row['tool_grants'])
        result['docker_projects'] = copy.deepcopy(row['docker_projects'])
        result['status'] = cls._key_status(row, now)
        result['enabled'] = not row['disabled']
        return result

    @staticmethod
    def _policy_row(row):
        """Project validated old records without changing effective grants."""
        policy = copy.deepcopy(row)
        if 'policy_version' not in policy:
            policy.update(policy_version=1, role='legacy', file_roots=[])
        for field, default in (('disabled', False), ('tool_grants', []), ('docker_projects', []),
                               ('grant_revision', 1), ('credential_generation', 1)):
            policy.setdefault(field, default)
        return policy

    @staticmethod
    def _bump(row, field):
        if row[field] >= _MAX_REVISION:
            raise ValueError('访问授权修订已达上限')
        row[field] += 1

    def snapshot(self):
        with self.lock:
            data, valid = self._read()
            now = self._now()
            return {
                'enabled': bool(valid and data['enabled']), 'storage_ok': valid,
                'keys': [self._public_key(row, now) for row in data['keys']],
                'scope_catalog': [dict(item) for item in SCOPE_CATALOG],
                'role_catalog': [dict(item) for item in ROLE_CATALOG],
                'audit': [dict(row) for row in reversed(data['audit'])],
            }

    def configure(self, body):
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise ValueError('仅接受 enabled 布尔值')
        with self.lock:
            data = self._mutable()
            if data['enabled'] != body['enabled']:
                data['keys'] = [self._policy_row(row) for row in data['keys']]
                for row in data['keys']:
                    self._bump(row, 'grant_revision')
            data['enabled'] = body['enabled']
            self._write(data)
            return self.snapshot()

    def create_key(self, body):
        if (not isinstance(body, dict) or set(body) - {
                'name', 'scopes', 'allowed_ips', 'expires_days', 'role', 'file_roots',
                'tool_grants', 'docker_projects'}
                or not _label(body.get('name'))):
            raise ValueError('密钥名称或字段无效')
        scopes = body.get('scopes', ['read'])
        days = body.get('expires_days', 90)
        supplied_ips = body.get('allowed_ips', [])
        modern = 'role' in body
        explicit = 'tool_grants' in body
        role = body.get('role') if modern else 'legacy'
        roots = body.get('file_roots', [])
        tools = body.get('tool_grants', [])
        projects = body.get('docker_projects', [])
        if (modern and (not isinstance(role, str) or role not in _ROLES)
                or not modern and (roots != [] or explicit)
                or not explicit and projects != []):
            raise ValueError('密钥角色或文件范围无效')
        if not _scopes(scopes, SCOPES if modern else LEGACY_SCOPES):
            raise ValueError('密钥权限无效')
        if modern and not _file_roots(roots, role, scopes):
            raise ValueError('文件范围或访问权限无效')
        if explicit and not _tool_grants(tools, role, scopes, roots, projects):
            raise ValueError('具体工具或项目范围无效')
        if type(days) is not int or not 0 <= days <= 365:
            raise ValueError('有效天数须为 0 至 365 的整数')
        ips = normalize_rules(supplied_ips)
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
                # Disabled keys remain recoverable. Only irreversibly revoked
                # or expired history may be discarded when the record cap fills.
                inactive = [row for row in data['keys']
                            if self._key_status(row, now) in ('expired', 'revoked')]
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
                'policy_version': 3 if explicit else 2 if modern else 1, 'role': role,
                'file_roots': [dict(root) for root in roots],
                'tool_grants': list(tools), 'docker_projects': copy.deepcopy(projects),
                'grant_revision': 1, 'credential_generation': 1,
                'disabled': False,
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

    @staticmethod
    def _key_id(key_id):
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise ValueError('密钥标识无效')

    @classmethod
    def _selected_key(cls, data, key_id):
        cls._key_id(key_id)
        row = next((row for row in data['keys'] if row['id'] == key_id), None)
        if row is None:
            raise ValueError('密钥不存在')
        return row

    @classmethod
    def _check_active_limit(cls, data, candidate, now):
        if (cls._key_status(candidate, now) == 'active'
                and sum(cls._key_status(row, now) == 'active'
                        for row in data['keys'] if row['id'] != candidate['id']) >= MAX_ACTIVE_KEYS):
            raise ValueError('最多保留 32 个有效密钥')

    def update_key(self, body):
        """Edit the complete merged grant; omitted role preserves legacy policy.

        Expiry changes are relative to this edit, and never reset creation time,
        revocation or the secret. Dropping files requires explicitly clearing its
        ranges; lowering a role must also lower any writable range in that edit.
        """
        if (not isinstance(body, dict) or not 2 <= len(body)
                or set(body) - {'id', 'name', 'role', 'scopes', 'file_roots',
                                'tool_grants', 'docker_projects',
                                'allowed_ips', 'expires_days'} or 'id' not in body):
            raise ValueError('密钥更新字段无效')
        self._key_id(body['id'])
        with self.lock:
            data = self._mutable()
            row = self._selected_key(data, body['id'])
            candidate = self._policy_row(row)
            previous = copy.deepcopy(candidate)
            for field in ('name', 'role', 'scopes', 'file_roots', 'tool_grants', 'docker_projects'):
                if field in body:
                    candidate[field] = body[field]
            if 'tool_grants' in body:
                candidate['policy_version'] = 3
            elif 'role' in body and candidate['policy_version'] < 3:
                candidate['policy_version'] = 2
            if 'allowed_ips' in body:
                candidate['allowed_ips'] = normalize_rules(body['allowed_ips'])
            now = self._now()
            if 'expires_days' in body:
                days = body['expires_days']
                if type(days) is not int or not 0 <= days <= 365:
                    raise ValueError('有效天数须为 0 至 365 的整数')
                candidate['expires_at'] = now + days * 86400 if days else None
            if not self._valid_key(candidate):
                raise ValueError('密钥角色、权限或文件范围无效')
            if any(candidate[field] != previous[field] for field in _REVISION_FIELDS):
                self._bump(candidate, 'grant_revision')
            self._check_active_limit(data, candidate, now)
            data['keys'][data['keys'].index(row)] = candidate
            self._write(data)
            return self._public_key(candidate, now)

    def set_key_enabled(self, key_id, enabled):
        """Suspend or resume a key, retaining expiry and irreversible revocation."""
        self._key_id(key_id)
        if type(enabled) is not bool:
            raise ValueError('密钥启用状态须为布尔值')
        with self.lock:
            data = self._mutable()
            row = self._selected_key(data, key_id)
            if enabled and row['revoked_at'] is not None:
                raise ValueError('已撤销的密钥不能恢复')
            candidate = dict(self._policy_row(row), disabled=not enabled)
            if candidate['disabled'] != row.get('disabled', False):
                self._bump(candidate, 'grant_revision')
            now = self._now()
            self._check_active_limit(data, candidate, now)
            data['keys'][data['keys'].index(row)] = candidate
            self._write(data)
            return self._public_key(candidate, now)

    def rotate_key(self, key_id):
        """Replace one secret atomically; the old digest is removed immediately."""
        self._key_id(key_id)
        with self.lock:
            data = self._mutable()
            row = self._selected_key(data, key_id)
            if row['revoked_at'] is not None:
                raise ValueError('已撤销的密钥不能轮换')
            now = self._now()
            candidate = self._policy_row(row)
            self._bump(candidate, 'credential_generation')
            token = 'r1_' + secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
            if any(hmac.compare_digest(digest, other['digest']) for other in data['keys']):
                raise OSError('未能生成新的访问密钥')
            candidate.update(digest=digest, last_used_at=None)
            data['keys'][data['keys'].index(row)] = candidate
            self._write(data)
            result = self._public_key(candidate, now)
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
                candidate = self._policy_row(row)
                self._bump(candidate, 'grant_revision')
                data['keys'][data['keys'].index(row)] = candidate
                row = candidate
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
            try:
                status = self._key_status(matched, self._now())
            except ValueError:
                return None, 'storage_unavailable'
            if status != 'active':
                return None, 'key_' + status
            if matched['allowed_ips'] and not peer_matches(canonical, matched['allowed_ips']):
                return None, 'peer_not_allowed'
            if required_scope is not None and required_scope not in matched['scopes']:
                return None, 'scope_denied'
            policy = self._policy_row(matched)
            return {'key_id': matched['id'], 'name': matched['name'],
                    'scopes': list(matched['scopes']), 'role': policy['role'],
                    'policy_version': policy['policy_version'],
                    'file_roots': [dict(root) for root in policy['file_roots']],
                    'tool_grants': list(policy['tool_grants']),
                    'docker_projects': copy.deepcopy(policy['docker_projects']),
                    'grant_revision': policy['grant_revision'],
                    'credential_generation': policy['credential_generation']}, 'ok'

    def is_current(self, principal):
        """Revalidate an already authenticated principal around a bounded read.

        This is not initial authentication and must never accept a principal
        supplied over HTTP. The original authorize() checked the real peer;
        changes to IP policy advance the revision checked here. No secret is
        needed to discard a result after rotation, revocation or natural expiry.
        """
        if (not isinstance(principal, dict)
                or not isinstance(principal.get('key_id'), str)
                or not _KEY_ID_RE.fullmatch(principal['key_id'])
                or any(type(principal.get(field)) is not int
                       or not 1 <= principal[field] <= _MAX_REVISION
                       for field in ('grant_revision', 'credential_generation'))):
            return False
        with self.lock:
            try:
                data, valid = self._read()
                if not valid or not data['enabled']:
                    return False
                row = next((row for row in data['keys'] if row['id'] == principal['key_id']), None)
                if row is None or self._key_status(row, self._now()) != 'active':
                    return False
                policy = self._policy_row(row)
                return all(principal[field] == policy[field]
                           for field in ('grant_revision', 'credential_generation'))
            except (OSError, ValueError, TypeError):
                return False

    def record(self, principal, operation, peer, outcome, duration_ms=0, *, touch_key=True):
        """Persist only a known operation, outcome and address, never its arguments."""
        if (not isinstance(operation, str) or operation not in AUDIT_OPERATIONS
                or not isinstance(outcome, str) or outcome not in AUDIT_OUTCOMES
                or not _duration(duration_ms) or type(touch_key) is not bool):
            raise ValueError('审计项目无效')
        with self.lock:
            data = self._mutable()
            now = self._now()
            key_id = principal.get('key_id') if isinstance(principal, dict) else None
            row = next((row for row in data['keys'] if row['id'] == key_id), None)
            key_id = row['id'] if row is not None else None
            if (row is not None and touch_key and
                    (not isinstance(principal, dict) or principal.get('credential_generation',
                     row.get('credential_generation', 1)) == row.get('credential_generation', 1))):
                row['last_used_at'] = now
            data['audit'].append({
                'timestamp': now, 'key_id': key_id, 'operation': operation,
                'peer': normalize_peer(peer), 'outcome': outcome, 'duration_ms': int(duration_ms),
            })
            data['audit'] = data['audit'][-MAX_AUDIT:]
            self._write(data)
