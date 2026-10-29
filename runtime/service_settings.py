"""Persistent optional services and a mandatory, bounded developer allowlist.

Web monitoring remains the core service. Screen ownership is desired state for
the independent screen manager; this store never controls a window or hardware.
Developer grants expire at a persisted wall-clock timestamp across restarts.
"""

import copy
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

from network_access import normalize_rules, peer_matches


_MAX_BYTES = 32768
_DEVELOPER_FIELDS = {'enabled', 'allowed_ips', 'minutes', 'expires_at', 'allow_commands'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _invalid(_value):
    raise ValueError('nonfinite JSON')


class ServiceSettingsStore:
    """Use explicit Web settings after the first save, without rewriting reads."""

    def __init__(self, state_dir, *, clock=None):
        self.path = Path(state_dir) / 'service-settings.json'
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self._write_failed = False
        self.bootstrap_error = None
        self._bootstrap = self._defaults()
        self._bootstrap['screen_enabled'] = os.environ.get('R1_SMALL_SCREEN_ENABLED', '1') != '0'
        if os.environ.get('R1_DEV_NO_PAIRING') == '1':
            try:
                rules = normalize_rules([item.strip() for item in os.environ.get(
                    'R1_DEV_ALLOW_IPS', '').split(',') if item.strip()], required=True)
                minutes = int(os.environ.get('R1_DEV_MINUTES', '60'))
                commands = os.environ.get('R1_DEV_ALLOW_COMMANDS', '0')
                if not 1 <= minutes <= 1440 or commands not in ('', '0', '1'):
                    raise ValueError('invalid developer configuration')
                self._bootstrap['developer'].update(enabled=True, allowed_ips=rules,
                    minutes=minutes, expires_at=self._now() + minutes * 60,
                    allow_commands=commands == '1')
            except (TypeError, ValueError, OverflowError):
                self.bootstrap_error = '环境中的开发者白名单或时限无效，开发者模式保持关闭。'

    @staticmethod
    def _defaults():
        return {'version': 1, 'screen_enabled': True,
                'developer': {'enabled': False, 'allowed_ips': [], 'minutes': 60,
                              'expires_at': None, 'allow_commands': False}}

    def _now(self):
        value = self.clock()
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 253402214399):
            raise ValueError('系统时间不可用')
        return int(value)

    @staticmethod
    def _valid(state):
        if (not isinstance(state, dict) or set(state) != {'version', 'screen_enabled', 'developer'}
                or type(state['version']) is not int or state['version'] != 1
                or type(state['screen_enabled']) is not bool):
            return False
        dev = state['developer']
        if (not isinstance(dev, dict) or set(dev) != _DEVELOPER_FIELDS
                or type(dev['enabled']) is not bool or type(dev['allow_commands']) is not bool
                or type(dev['minutes']) is not int or not 1 <= dev['minutes'] <= 1440
                or (dev['expires_at'] is not None and (type(dev['expires_at']) is not int
                    or not 0 <= dev['expires_at'] <= 253402300799))
                or dev['enabled'] and dev['expires_at'] is None
                or not dev['enabled'] and dev['expires_at'] is not None):
            return False
        try:
            return normalize_rules(dev['allowed_ips'], required=dev['enabled']) == dev['allowed_ips']
        except (TypeError, ValueError):
            return False

    def _read(self):
        if self._write_failed:
            return None
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return None if self.path.is_symlink() else copy.deepcopy(self._bootstrap)
        except OSError:
            return None
        try:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (self.path.is_symlink() or not stat.S_ISREG(info.st_mode)
                        or info.st_size > _MAX_BYTES
                        or os.name != 'nt' and stat.S_IMODE(info.st_mode) != 0o600):
                    return None
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return None
            state = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_invalid)
            return state if self._valid(state) else None
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None

    def _write(self, state):
        temporary = None
        try:
            serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(serialized) > _MAX_BYTES:
                raise ValueError('服务设置超过存储上限')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise OSError('unsafe settings target')
            descriptor, temporary = tempfile.mkstemp(prefix='.service-settings-', dir=str(self.path.parent))
            with os.fdopen(descriptor, 'wb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name != 'nt':
                descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            self._write_failed = True
            raise OSError('服务设置未能保存，请检查数据卷') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self):
        with self.lock:
            state = self._read()
            valid = state is not None
            if not valid:
                state = self._defaults()
                state['screen_enabled'] = False
            now = self._now()
            dev = copy.deepcopy(state['developer'])
            active = valid and dev['enabled'] and dev['expires_at'] > now
            dev.update(active=active, reason=(
                '服务配置不可用，开发者访问已关闭。' if not valid else
                '开发者窗口已过期。' if dev['enabled'] and not active else
                self.bootstrap_error if not self.path.exists() and self.bootstrap_error else
                '开发者模式未开启。' if not dev['enabled'] else None))
            return {'storage_ok': valid, 'screen_enabled': state['screen_enabled'],
                    'developer': dev, 'error': None if valid else '服务配置存储不可用'}

    def authorized(self, peer):
        state = self.snapshot()
        return state['storage_ok'] and state['developer']['active'] and peer_matches(
            peer, state['developer']['allowed_ips'])

    def _mutable(self):
        state = self._read()
        if state is None:
            raise OSError('服务配置存储不可用，请检查数据卷')
        return state

    def configure_screen(self, body):
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            raise ValueError('小屏开关仅接受 enabled 布尔值')
        with self.lock:
            state = self._mutable()
            state['screen_enabled'] = body['enabled']
            self._write(state)
            return self.snapshot()

    def configure_developer(self, body):
        if (not isinstance(body, dict) or set(body) != {'enabled', 'allowed_ips', 'minutes', 'allow_commands'}
                or type(body['enabled']) is not bool or type(body['allow_commands']) is not bool
                or type(body['minutes']) is not int or not 1 <= body['minutes'] <= 1440):
            raise ValueError('开发者设置需要开关、白名单、1～1440 分钟时限与受限命令开关')
        rules = normalize_rules(body['allowed_ips'], required=body['enabled'])
        with self.lock:
            state = self._mutable()
            state['developer'] = {**body, 'allowed_ips': rules,
                                 'expires_at': self._now() + body['minutes'] * 60 if body['enabled'] else None}
            self._write(state)
            return self.snapshot()
