"""Control temperature catalog: one-second sysfs reads and timed SMART cache.

No commands run here. In particular, reading this catalog never probes SMART,
wakes disks, or treats the fast dashboard timestamp as a disk measurement.
"""
import copy
import hashlib
import math
from pathlib import Path
import re
import threading
import time

CPU_DRIVERS = frozenset(('coretemp', 'k10temp', 'zenpower', 'cpu_thermal', 'cpu-thermal'))
CPU_ZONE_TYPES = frozenset(('x86_pkg_temp', 'cpu-thermal', 'cpu_thermal', 'cpu', 'soc_thermal'))
DISK_DRIVERS = frozenset(('nvme', 'drivetemp'))
SYSFS_MAX_AGE = 3
SMART_MAX_AGE = 90


def _number(value, minimum=-40, maximum=150):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if minimum <= value <= maximum and math.isfinite(value) else None


class TemperatureSources:
    def __init__(self, insights=None, sys_root='/sys', clock=None):
        self.insights = insights
        self.sys_root = Path(sys_root).resolve()
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.last_sysfs = None
        self.sysfs = []
        self.known_disks = {}

    def _inside(self, path):
        try:
            path = path.resolve()
            return path == self.sys_root or self.sys_root in path.parents
        except (OSError, ValueError, RuntimeError):
            return False

    def _text(self, path):
        if not self._inside(path):
            return None
        try:
            with path.open('r', encoding='utf-8') as source:
                data = source.read(257)
            return data.strip() if len(data) <= 256 else None
        except (OSError, ValueError, UnicodeError):
            return None

    def _temperature(self, path, critical=False):
        raw = self._text(path)
        if raw is None or not re.fullmatch(r'-?[0-9]{1,9}', raw):
            return None
        return _number(int(raw) / 1000, minimum=40 if critical else -40)

    def _physical(self, folder):
        """Keep physical topology, never hwmonN or thermal_zoneN numbering."""
        try:
            physical = folder / 'device'
            resolved = physical.resolve() if physical.exists() else folder.resolve()
            relative = resolved.relative_to(self.sys_root)
            parts = [part for part in relative.parts
                     if part not in ('hwmon', 'thermal')
                     and not re.fullmatch(r'(?:hwmon|thermal_zone)[0-9]+', part)]
            return '/'.join(parts)
        except (OSError, ValueError, RuntimeError):
            return ''

    @staticmethod
    def _id(prefix, physical, driver, channel):
        value = '\0'.join((physical, driver, channel))
        return prefix + ':' + hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def _row(identity, label, kind, temperature, sampled, max_age, critical=None, status=None):
        return {'id': identity, 'label': label, 'kind': kind, 'temperature_c': temperature,
                'status': status or ('ok' if temperature is not None else 'unavailable'),
                'sampled_monotonic': sampled, 'max_age_seconds': max_age,
                'critical_celsius': critical}

    def _read_sysfs(self, now):
        rows = []
        for folder in sorted((self.sys_root / 'class/hwmon').glob('hwmon*'))[:64]:
            if not self._inside(folder):
                continue
            driver = self._text(folder / 'name') or 'unknown'
            if driver.lower() in DISK_DRIVERS:
                # Storage uses the existing slow, non-waking SMART worker.
                continue
            kind = 'cpu' if driver.lower() in CPU_DRIVERS else 'board'
            physical = self._physical(folder)
            for path in sorted(folder.glob('temp*_input'))[:32]:
                match = re.fullmatch(r'(temp[0-9]+)_input', path.name)
                if not match:
                    continue
                channel = match[1]
                label = self._text(folder / (channel + '_label')) or channel
                temperature = self._temperature(path)
                identity = self._id('sysfs', physical, driver, channel)
                rows.append(self._row(identity, driver + ' · ' + label, kind, temperature,
                                      now if temperature is not None else None, SYSFS_MAX_AGE,
                                      self._temperature(folder / (channel + '_crit'), critical=True)))
        for folder in sorted((self.sys_root / 'class/thermal').glob('thermal_zone*'))[:64]:
            if not self._inside(folder):
                continue
            driver = self._text(folder / 'type') or 'unknown'
            kind = 'cpu' if driver.lower() in CPU_ZONE_TYPES else 'board'
            temperature = self._temperature(folder / 'temp')
            critical = []
            for path in sorted(folder.glob('trip_point_*_type'))[:32]:
                if self._text(path) == 'critical':
                    value = self._temperature(path.with_name(path.name[:-4] + 'temp'), critical=True)
                    if value is not None:
                        critical.append(value)
            rows.append(self._row(self._id('thermal', self._physical(folder), driver, 'temp'),
                                  driver, kind, temperature, now if temperature is not None else None,
                                  SYSFS_MAX_AGE, min(critical) if critical else None))
        return rows

    @staticmethod
    def _fresh(sampled, now, max_age):
        return (type(sampled) in (int, float) and 0 <= sampled <= now
                and math.isfinite(sampled) and now - sampled <= max_age)

    def _disks(self, now):
        try:
            state = self.insights.snapshot() if self.insights is not None else {}
            disks = state.get('disks', {})
            items = disks.get('items', []) if disks.get('available') is True else []
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        active = set()
        rows = []
        for item in items[:128]:
            if not isinstance(item, dict):
                continue
            stable = item.get('stable_identity')
            identified = isinstance(stable, str) and bool(re.fullmatch(r'(?:wwn|serial|physical):[0-9a-f]{24}', stable))
            model = item.get('model') if isinstance(item.get('model'), str) else '存储设备'
            identity = 'disk:' + stable if identified else 'disk:unidentified:' + hashlib.sha256(model.encode()).hexdigest()[:24]
            sampled = item.get('smart_sampled_monotonic')
            fresh = identified and self._fresh(sampled, now, SMART_MAX_AGE)
            temperature = _number(item.get('smart_temperature_c')) if fresh else None
            status = 'unavailable'
            if fresh and item.get('smart_status') == 'standby' and item.get('standby') is True:
                status, temperature = 'standby', None
            elif fresh and item.get('smart_status') == 'ok' and temperature is not None:
                status = 'ok'
            else:
                temperature = None
            device = item.get('device', '')
            suffix = ' · ' + device if isinstance(device, str) and re.fullmatch(r'/dev/(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)', device) else ''
            user_name = item.get('user_name')
            if not isinstance(user_name, str) or not 0 < len(user_name) <= 128 or any(
                    ord(char) < 32 or ord(char) == 127 for char in user_name):
                user_name = None
            row = self._row(identity, '磁盘 · ' + (user_name or model or '存储设备')[:128] + suffix, 'disk', temperature,
                            sampled if type(sampled) in (int, float) and 0 <= sampled <= now and math.isfinite(sampled) else None,
                            SMART_MAX_AGE, _number(item.get('temperature_critical_c'), minimum=40), status)
            rows.append(row)
            active.add(identity)
            if identity not in self.known_disks and len(self.known_disks) >= 256:
                self.known_disks.pop(next(iter(self.known_disks)))
            self.known_disks[identity] = copy.deepcopy(row)
        # Disappearance is explicit unavailability, not a silent removal of a
        # previously selected source. A stale standby never remains excluded.
        for identity, previous in self.known_disks.items():
            if identity not in active:
                row = copy.deepcopy(previous)
                row.update(status='unavailable', temperature_c=None)
                rows.append(row)
        return rows

    def snapshot(self):
        with self.lock:
            now = self.clock()
            if self.last_sysfs is None or not 0 <= now - self.last_sysfs < 1:
                self.sysfs = self._read_sysfs(now)
                self.last_sysfs = now
            rows = copy.deepcopy(self.sysfs) + self._disks(now)
            unique = {}
            for row in rows:
                if row['id'] in unique:
                    # Identical numbering without physical topology is
                    # ambiguous. Never choose a device merely by scan order.
                    unique[row['id']].update(status='unavailable', temperature_c=None)
                else:
                    unique[row['id']] = row
            return sorted(unique.values(), key=lambda row: (row['kind'], row['label'], row['id']))

    def cpu_guard(self):
        values = [row['temperature_c'] for row in self.snapshot()
                  if row['kind'] == 'cpu' and row['status'] == 'ok']
        return max(values) if values else None
