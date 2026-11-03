"""Persistent, display-only disk snapshot cache.

The cache keeps the last successfully observed physical-disk and capacity
values so a Web or small-screen page can paint immediately after a restart or
a host mount event.  It is never an authorization, identity, or measurement
proof: writes, file access, fan decisions and capacity refreshes continue to
use their existing live checks.  Only bounded scalar fields are stored; no
serial numbers, paths outside the fixed /drives namespace, command output,
tokens, or request data are retained.
"""

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time

SCHEMA_VERSION = 1
MAX_BYTES = 512 * 1024
MAX_DISKS = 64
MAX_CAPACITY_ROWS = 256
_SMART_FIELDS = (
    'model', 'size_bytes', 'type', 'temp_c', 'temperature_source', 'health',
    'smart_available', 'standby', 'smart_status', 'smart_sampled_at',
    'smart_fresh', 'temperature_critical_c',
)


def _number(value, minimum=None, maximum=None):
    if type(value) is not int and type(value) is not float:
        return None
    if not isinstance(value, (int, float)) or not value == value:
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _capacity_row(value):
    if not isinstance(value, dict):
        return None
    mount = _text(value.get('mount'), 256)
    device = _text(value.get('device'), 32)
    total = _number(value.get('total_bytes'), 1)
    used = _number(value.get('used_bytes'), 0)
    if not mount or not mount.startswith('/drives/') or used is None or total is None or used > total:
        return None
    return {
        'mount': mount,
        'device': device,
        'total_bytes': total,
        'used_bytes': used,
        'capacity_sampled_at': _number(value.get('capacity_sampled_at'), 1),
        'capacity_status': 'cached',
        'capacity_source': 'persistent_display_cache',
    }


def _disk_row(value):
    if not isinstance(value, dict):
        return None
    name = _text(value.get('id'), 64)
    if not name or any(char in name for char in ('/', chr(92))):
        return None
    row = {'id': name}
    for key in _SMART_FIELDS:
        if key in ('model', 'type', 'temperature_source', 'smart_status'):
            row[key] = _text(value.get(key), 160)
        elif key == 'health':
            health = value.get(key)
            row[key] = {'passed': health.get('passed') if isinstance(health, dict)
                        and type(health.get('passed')) is bool else None}
        elif key in ('smart_available', 'standby', 'smart_fresh'):
            row[key] = value.get(key) if type(value.get(key)) is bool else None
        else:
            row[key] = _number(value.get(key), -40 if key == 'temp_c' else 0,
                               200 if key == 'temp_c' else 9223372036854775807)
    return row


class DiskDisplayCache:
    """Thread-safe last-good display values loaded from one bounded JSON file."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.RLock()
        self.capacity = {}
        self.disks = {}
        self._last_write = 0.0
        self._last_payload = None
        self.load()

    def load(self):
        if self.path is None:
            return
        try:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > MAX_BYTES:
                return
            raw = self.path.read_text(encoding='utf-8')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION:
                return
            capacity = {}
            for item in value.get('capacity', []):
                row = _capacity_row(item)
                if row is not None and len(capacity) < MAX_CAPACITY_ROWS:
                    capacity[row['mount']] = row
            disks = {}
            for item in value.get('disks', []):
                row = _disk_row(item)
                if row is not None and len(disks) < MAX_DISKS:
                    disks[row['id']] = row
            with self.lock:
                self.capacity, self.disks = capacity, disks
        except (OSError, ValueError, TypeError, UnicodeError):
            # A corrupt or inaccessible display cache is simply unavailable;
            # it never changes a live measurement or authorization result.
            return

    def overlay_status(self, value):
        """Fill missing capacity fields from the display cache, without guessing."""
        if not isinstance(value, dict):
            return value
        rows = value.get('disks')
        if not isinstance(rows, list):
            return value
        with self.lock:
            capacity = copy.deepcopy(self.capacity)
        now = time.time()
        if not rows and capacity:
            # The live inventory is temporarily empty (for example during a
            # host mount event).  Keep the last display rows visible.
            value['disks'] = sorted(capacity.values(), key=lambda row: row['mount'])
            return value
        for row in rows:
            if not isinstance(row, dict):
                continue
            total = _number(row.get('total_bytes'), 1)
            used = _number(row.get('used_bytes'), 0)
            if total is not None and used is not None and used <= total:
                continue
            cached = capacity.get(row.get('mount'))
            if cached is None:
                continue
            current_device = row.get('device')
            if current_device and cached.get('device') and current_device != cached['device']:
                continue
            row.update({
                'total_bytes': cached['total_bytes'],
                'used_bytes': cached['used_bytes'],
                'capacity_sampled_at': cached.get('capacity_sampled_at'),
                'capacity_status': 'cached',
                'capacity_source': 'persistent_display_cache',
            })
        return value

    def overlay_insights(self, value):
        """Fill missing SMART/temperature fields for the same physical disk id."""
        if not isinstance(value, dict):
            return value
        disks = value.get('disks')
        if not isinstance(disks, dict) or not isinstance(disks.get('items'), list):
            return value
        items = disks['items']
        with self.lock:
            cached = copy.deepcopy(self.disks)
        by_id = {row.get('id'): row for row in items if isinstance(row, dict)}
        for row in items:
            if not isinstance(row, dict):
                continue
            known = cached.get(row.get('id'))
            if not known:
                continue
            for key, cached_value in known.items():
                if key == 'id':
                    continue
                if key == 'health':
                    health = row.get('health')
                    if isinstance(health, dict) and health.get('passed') is not None:
                        continue
                    row['health'] = {'passed': cached_value.get('passed')}
                elif row.get(key) is None:
                    row[key] = copy.deepcopy(cached_value)
        if not items:
            value['disks']['items'] = [copy.deepcopy(row) for row in cached.values()]
        return value

    def record(self, status, insights):
        """Update from live snapshots and atomically persist changed values."""
        changed = False
        capacity = {}
        if isinstance(status, dict):
            for row in status.get('disks', []) if isinstance(status.get('disks'), list) else []:
                item = _capacity_row(row)
                if item is not None and len(capacity) < MAX_CAPACITY_ROWS:
                    capacity[item['mount']] = item
        disks = {}
        disks = {}
        if isinstance(insights, dict):
            items = insights.get('disks', {}).get('items') if isinstance(insights.get('disks'), dict) else None
            for row in items if isinstance(items, list) else []:
                item = _disk_row(row)
                if item is not None and len(disks) < MAX_DISKS:
                    disks[item['id']] = item
        with self.lock:
            if capacity and capacity != self.capacity:
                self.capacity = capacity
                changed = True
            if disks and disks != self.disks:
                self.disks = disks
                changed = True
            if not changed or self.path is None or self.clock() - self._last_write < 5:
                return
            payload = {'schema_version': SCHEMA_VERSION, 'saved_at': self.clock(),
                       'capacity': sorted(capacity.values(), key=lambda row: row['mount']),
                       'disks': sorted(disks.values(), key=lambda row: row['id'])}
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if len(encoded.encode('utf-8')) > MAX_BYTES:
                return
            if encoded == self._last_payload:
                return
            self._last_write = self.clock()
            self._last_payload = encoded
        self._write(encoded)

    def _write(self, encoded):
        path = self.path
        if path is None:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
            temporary = Path(name)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, path)
            temporary = None
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
