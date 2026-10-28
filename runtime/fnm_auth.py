"""Password login for the web panel, ported from fnmonitor (MIT).

fnmonitor protects its panel with a PBKDF2-HMAC-SHA256 password stored in
``config.json`` and persistent ``sessions.json`` entries that only keep the
SHA-256 of each session id.  This module keeps that behaviour for the R1 panel
while writing to the Toolbox data volume instead of a fnOS app directory:

* ``panel-auth.json``      – login password hash and session lifetime
* ``panel-sessions.json`` – hashed session ids and expiry
* ``panel.json``          – panel settings (weather city, retention hint, ...)

Nothing here touches hardware, Docker or the small screen.  The optional
five-digit pairing from ``web_config`` stays independent: a password session and
a pairing session are both accepted by write endpoints, and neither can create
the other.
"""

import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time


PBKDF2_ITERATIONS = 240000
PASSWORD_MIN_LEN = 6
COOKIE = 'fnmp_sid'
SID_PATTERN = r'[A-Za-z0-9_-]{43}'
SESSION_DAYS_DEFAULT = 7
MAX_SESSIONS = 16
CONFIG_KEYS = ('auth_enabled', 'auth_password_hash', 'auth_session_days')


def hash_password(password, iterations=PBKDF2_ITERATIONS, salt=None):
    salt = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt, int(iterations))
    return 'pbkdf2_sha256$%d$%s$%s' % (
        int(iterations), base64.b64encode(salt).decode('ascii'), base64.b64encode(derived).decode('ascii'))


def verify_password(password, stored):
    """Constant-time compare; any malformed stored value is a mismatch."""
    try:
        algorithm, iterations, salt_b64, hash_b64 = str(stored or '').split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        iterations = int(iterations)
        if not 1000 <= iterations <= 10000000:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        derived = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt, iterations)
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def write_json_secure(path, value):
    """Atomic 0600 JSON write: temporary file, fsync, chmod, replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp.%d.%d' % (os.getpid(), threading.get_ident()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            pass


class LoginLimiter:
    """Per-address and global failure lockout, shared by login and password change.

    fnmonitor's numbers are kept: 5 failures inside 10 minutes lock the address
    for 60 s (doubling up to 15 min); 30 failures in 60 s from anywhere lock
    every source for 30 s.  Two PBKDF2 verifications may run at once so a slow
    NAS CPU cannot be saturated by parallel guesses.
    """

    WINDOW = 600
    MAX_FAILS = 5
    BASE_LOCK = 60
    MAX_LOCK = 900
    GLOBAL_WINDOW = 60
    GLOBAL_MAX = 30
    GLOBAL_LOCK = 30

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.addresses = {}
        self.global_failures = []
        self.global_until = 0.0
        self.verify_semaphore = threading.BoundedSemaphore(2)

    def retry_after(self, address):
        now = self.clock()
        with self.lock:
            wait = max(0.0, self.global_until - now)
            state = self.addresses.get(address)
            if state and state['until'] > now:
                wait = max(wait, state['until'] - now)
        return int(math.ceil(wait)) if wait > 0 else 0

    def fail(self, address):
        now = self.clock()
        with self.lock:
            self.global_failures = [stamp for stamp in self.global_failures if now - stamp < self.GLOBAL_WINDOW]
            self.global_failures.append(now)
            if len(self.global_failures) >= self.GLOBAL_MAX:
                self.global_until = now + self.GLOBAL_LOCK
                self.global_failures = []
            state = self.addresses.setdefault(address, {'fails': [], 'until': 0.0, 'level': 0, 'last': now})
            if now - state['last'] > 3600:
                state['level'] = 0
            state['last'] = now
            state['fails'] = [stamp for stamp in state['fails'] if now - stamp < self.WINDOW] + [now]
            if len(state['fails']) >= self.MAX_FAILS:
                state['level'] += 1
                state['until'] = now + min(self.MAX_LOCK, self.BASE_LOCK * (2 ** (state['level'] - 1)))
                state['fails'] = []
            if len(self.addresses) > 4096:
                for key in sorted(self.addresses, key=lambda item: self.addresses[item]['last'])[:2048]:
                    self.addresses.pop(key, None)

    def success(self, address):
        with self.lock:
            self.addresses.pop(address, None)


class AuthStore:
    """Password state and persistent sessions for the panel."""

    def __init__(self, state_dir, clock=time.monotonic, wall_clock=time.time):
        self.state_dir = Path(state_dir)
        self.config_path = self.state_dir / 'panel-auth.json'
        self.sessions_path = self.state_dir / 'panel-sessions.json'
        self.clock = clock
        self.wall_clock = wall_clock
        self.lock = threading.RLock()
        self.limiter = LoginLimiter(clock)
        self.sessions = {}
        self.load_sessions()

    # ---- configuration ----
    def _read_config(self):
        try:
            data = json.loads(self.config_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_config(self, updates):
        with self.lock:
            current = self._read_config()
            current.update(updates)
            write_json_secure(self.config_path, current)
            return current

    def enabled(self):
        config = self._read_config()
        return str(config.get('auth_enabled') or '0') in ('1', 'true', 'on')

    def password_set(self):
        return bool(str(self._read_config().get('auth_password_hash') or '').strip())

    def session_days(self):
        try:
            days = int(self._read_config().get('auth_session_days') or SESSION_DAYS_DEFAULT)
        except (TypeError, ValueError):
            days = SESSION_DAYS_DEFAULT
        return days if 1 <= days <= 30 else SESSION_DAYS_DEFAULT

    def required(self):
        """The panel is only gated once a password exists and protection is on."""
        return self.enabled() and self.password_set()

    def public_config(self):
        return {'auth_enabled': 1 if self.enabled() else 0, 'auth_password_set': self.password_set(),
                'auth_session_days': self.session_days(), 'password_min_len': PASSWORD_MIN_LEN}

    def auth_config(self):
        has = self.password_set()
        return {'auth_enabled': 1 if self.enabled() else 0, 'requires_password': has,
                'setup_mode': not has, 'password_min_len': PASSWORD_MIN_LEN}

    # ---- sessions ----
    @staticmethod
    def _sid_key(sid):
        return hashlib.sha256(str(sid).encode('utf-8')).hexdigest()

    def load_sessions(self):
        try:
            if self.sessions_path.stat().st_size > 262144:
                raise ValueError('session file too large')
            stored = json.loads(self.sessions_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            stored = {}
        now, sessions = self.wall_clock(), {}
        if isinstance(stored, dict):
            for key, value in list(stored.items())[:MAX_SESSIONS * 4]:
                if not isinstance(value, dict):
                    continue
                expires = value.get('expires_wall')
                label = value.get('label')
                if isinstance(expires, (int, float)) and now < expires and isinstance(label, str):
                    sessions[str(key)] = {'expires_wall': float(expires), 'label': label[:64]}
        with self.lock:
            self.sessions = sessions

    def _save_sessions(self):
        write_json_secure(self.sessions_path, self.sessions)

    def create_session(self, label='password'):
        sid = secrets.token_urlsafe(32)
        key = self._sid_key(sid)
        now = self.wall_clock()
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda item: self.sessions[item]['expires_wall'])
                self.sessions.pop(oldest, None)
            self.sessions[key] = {'expires_wall': now + self.session_days() * 86400, 'label': str(label)[:64]}
            self._save_sessions()
        return sid

    def revoke(self, sid):
        key = self._sid_key(sid)
        with self.lock:
            if self.sessions.pop(key, None) is not None:
                self._save_sessions()

    def revoke_others(self, keep=None):
        keep_key = self._sid_key(keep) if keep else None
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items() if key == keep_key}
            self._save_sessions()

    def set_session_days(self, days, keep=None):
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items()
                             if key == (self._sid_key(keep) if keep else None)}
            self._save_sessions()
        self._write_config({'auth_session_days': days})

    def cookie_sid(self, cookie_header):
        if not cookie_header or len(cookie_header) > 2048:
            return None
        values = [value for name, _, value in (part.strip().partition('=') for part in cookie_header.split(';'))
                  if name == COOKIE and _]
        if len(values) != 1 or len(values[0]) != 43:
            return None
        return values[0]

    def session(self, cookie_header):
        """Return the session for this request, expiring it when needed."""
        sid = self.cookie_sid(cookie_header)
        if sid is None:
            return None
        key = self._sid_key(sid)
        now = self.wall_clock()
        with self.lock:
            entry = self.sessions.get(key)
            if entry is None:
                return None
            if entry['expires_wall'] <= now:
                self.sessions.pop(key, None)
                self._save_sessions()
                return None
            return entry

    # ---- operations (called by the web server with validated input) ----
    def login(self, password, address=''):
        if not self.password_set():
            return {'ok': False, 'error': '未配置登录口令'}
        if not isinstance(password, str) or not password or len(password) > 256:
            self.limiter.fail(address)
            return {'ok': False, 'error': '口令错误'}
        with self.limiter.verify_semaphore:
            matched = verify_password(password, self._read_config().get('auth_password_hash'))
        if not matched:
            self.limiter.fail(address)
            return {'ok': False, 'error': '口令错误', 'retry_after': self.limiter.retry_after(address) or None}
        self.limiter.success(address)
        return {'ok': True, 'sid': self.create_session('password')}

    def setup(self, password, address=''):
        """First-time password: only while no password exists."""
        if self.password_set():
            return {'ok': False, 'error': '已设置口令，请使用登录修改'}
        if not isinstance(password, str) or not PASSWORD_MIN_LEN <= len(password) <= 256:
            return {'ok': False, 'error': '口令至少 %d 位' % PASSWORD_MIN_LEN}
        self._write_config({'auth_password_hash': hash_password(password), 'auth_enabled': 1})
        return {'ok': True, 'sid': self.create_session('setup')}

    def change(self, old_password, new_password, address=''):
        """Change or clear the password; old password required and rate-limited."""
        if not self.password_set():
            return {'ok': False, 'error': '尚未设置口令'}
        if not isinstance(old_password, str) or not old_password:
            return {'ok': False, 'error': '当前口令不正确'}
        with self.limiter.verify_semaphore:
            matched = verify_password(old_password, self._read_config().get('auth_password_hash'))
        if not matched:
            self.limiter.fail(address)
            return {'ok': False, 'error': '当前口令不正确', 'retry_after': self.limiter.retry_after(address) or None}
        self.limiter.success(address)
        if new_password == '':
            self._write_config({'auth_password_hash': '', 'auth_enabled': 0})
            self.revoke_others()
            return {'ok': True, 'cleared': True}
        if not isinstance(new_password, str) or not PASSWORD_MIN_LEN <= len(new_password) <= 256:
            return {'ok': False, 'error': '新口令至少 %d 位' % PASSWORD_MIN_LEN}
        self._write_config({'auth_password_hash': hash_password(new_password), 'auth_enabled': 1})
        self.revoke_others()
        return {'ok': True, 'sid': self.create_session('password')}

    def set_enabled(self, enabled):
        if enabled and not self.password_set():
            raise ValueError('请先设置登录口令')
        self._write_config({'auth_enabled': 1 if enabled else 0})
        if not enabled:
            with self.lock:
                self.sessions = {}
                self._save_sessions()
        return {'ok': True}
