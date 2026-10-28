"""fnmonitor-compatible panel data built from the Toolbox's existing collectors.

The web panel is fnmonitor's own UI (vendored under ``ui/panel-src``).  Instead of
rewriting its rendering code, this module speaks its HTTP contract and fills it
with readings the Toolbox already produced.  Nothing here samples hardware on a
request: every value comes from cached snapshots held by the collectors that run
in the host service.

Deliberate limits, matching the project's rules:

* Unknown readings are ``None`` (the panel prints ``--``), never zero.
* Power is only reported when RAPL provides a real CPU package reading; there is
  no whole-machine estimate.
* SMART detail is not probed per request.  The disk list uses the background
  probe (``smartctl -n standby``) and never wakes a sleeping disk.
* fnOS-only integrations (iDRAC, RAID cards, the app centre, weather, the
  self-updater) report themselves unavailable instead of pretending to exist.
"""

import copy
import datetime
import json
import math
import os
from pathlib import Path
import re
import time


PANEL_SCHEMA = 1
SAMPLE_INTERVAL = 5

# fnmonitor's config.json keys kept for the panel's settings page.  Only a few
# map onto real Toolbox services; ``PANEL_NOTES`` explains the rest so a saved
# value never implies an effect it does not have.
PANEL_DEFAULTS = {
    'interval': SAMPLE_INTERVAL,
    'retention_days': 30,
    'history_interval': 60,
    'weather_city': '',
    'data_dir': '',
    'update_autocheck': 0,
    'update_autodownload': 0,
    'update_autoupdate': 0,
    'traffic_exclude_bridge': 0,
    'power_tdp_w': 0,
    'power_rate_yuan': 0.6,
    'power_disk_typical_w': 8,
    'power_nic_fixed_w': 5,
    'power_base_w': 8,
    'disk_standby_protect': 1,
    'disk_scan_enabled': 0,
    'disk_scan_interval_h': 24,
    'open_mode': 'url',
    'monthly_summary_enabled': 0,
    'ec_fan_probe': 0,
}

CONFIG_LIMITS = {
    'retention_days': (1, 30),
    'history_interval': (10, 3600),
    'interval': (1, 60),
    'power_rate_yuan': (0.0, 100.0),
    'disk_scan_interval_h': (1, 168),
}

RANGES = {'10m': 600, '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000, 'all': 0}

# fnmonitor metric -> Toolbox monitoring metric.  ``net`` and ``diskio`` are
# handled separately because they carry two series.
HISTORY_MAP = {
    'cpu': 'cpu_usage_percent',
    'mem': 'memory_usage_percent',
    'temp': 'cpu_temperature_c',
    'power': 'cpu_package_w',
    'load1': None,
    'temp_mb': None,
    'fan_avg': None,
    'disk_total': None,
    'net_bytes': None,
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value, limit=512):
    return value[:limit] if isinstance(value, list) else []


def _number(value, low=None, high=None):
    if type(value) not in (int, float):
        return None
    if not math.isfinite(value):
        return None
    if low is not None and value < low:
        return None
    if high is not None and value > high:
        return None
    return value


def _text(value, limit=256):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _stamp(value):
    """Epoch seconds from an ISO timestamp; None when unreadable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
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


class PanelStore:
    """Panel settings and layout persisted in the Toolbox data volume."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.config_path = self.state_dir / 'panel.json'
        self.ui_path = self.state_dir / 'panel-ui.json'

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def config(self):
        stored = self._read(self.config_path)
        result = copy.deepcopy(PANEL_DEFAULTS)
        for key in PANEL_DEFAULTS:
            if key in stored and type(stored[key]) is type(PANEL_DEFAULTS[key]):
                result[key] = stored[key]
        return result

    def save_config(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValueError('配置内容为空')
        unknown = set(changes) - set(PANEL_DEFAULTS) - {'auth_enabled', 'auth_password_hash', 'auth_session_days'}
        if unknown:
            raise ValueError('不支持的配置字段')
        current = self.config()
        for key, value in changes.items():
            if key not in PANEL_DEFAULTS:
                continue
            default = PANEL_DEFAULTS[key]
            if type(value) is not type(default):
                raise ValueError('配置类型无效：' + key)
            limit = CONFIG_LIMITS.get(key)
            if limit and not limit[0] <= value <= limit[1]:
                raise ValueError('配置超出范围：' + key)
            current[key] = value
        _write_json(self.config_path, current)
        return current

    def ui(self):
        return self._read(self.ui_path)

    def save_ui(self, data):
        """Keep only presentation fields; never store credentials here."""
        if not isinstance(data, dict):
            raise ValueError('界面配置必须是对象')
        allowed = ('theme', 'layoutVersion', 'range', 'tab', 'fontScale', 'accent', 'dotMatrix',
                   'motion', 'layout', 'hidden', 'thresholds', 'ignored')
        clean = {key: copy.deepcopy(data[key]) for key in allowed if key in data}
        thresholds = clean.get('thresholds')
        if thresholds is not None:
            clean['thresholds'] = _sanitize_thresholds(thresholds)
        if 'accent' in clean and not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', str(clean['accent'])):
            clean.pop('accent')
        if 'theme' in clean and clean['theme'] not in ('light', 'dark', 'auto'):
            clean.pop('theme')
        if 'fontScale' in clean:
            try:
                scale = float(clean['fontScale'])
            except (TypeError, ValueError):
                scale = 1.0
            clean['fontScale'] = round(min(1.5, max(0.8, scale)), 2)
        if 'dotMatrix' in clean and type(clean['dotMatrix']) is not bool:
            clean.pop('dotMatrix')
        if 'motion' in clean and type(clean['motion']) is not bool:
            clean.pop('motion')
        if 'ignored' in clean:
            clean['ignored'] = _sanitize_ignored(clean['ignored'])
        for key in ('layout', 'hidden'):
            if key in clean and not isinstance(clean[key], dict):
                clean.pop(key)
        if len(json.dumps(clean, ensure_ascii=False)) > 256 * 1024:
            raise ValueError('界面配置过大')
        _write_json(self.ui_path, clean)
        return clean


THRESHOLD_SPEC = {
    'cpuWarn': (85, 30, 100), 'cpuCrit': (95, 30, 100),
    'memWarn': (85, 30, 100), 'memCrit': (95, 30, 100),
    'capWarn': (85, 30, 100), 'capCrit': (95, 30, 100),
    'tempWarn': (80, 40, 110), 'tempCrit': (90, 40, 115),
    'diskTempWarn': (50, 30, 80), 'diskTempCrit': (58, 30, 85),
}


def _sanitize_thresholds(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, (default, low, high) in THRESHOLD_SPEC.items():
        value = _number(raw.get(key), low, high)
        out[key] = round(value, 1) if value is not None else default
    for warn, critical in (('cpuWarn', 'cpuCrit'), ('memWarn', 'memCrit'), ('capWarn', 'capCrit'),
                           ('tempWarn', 'tempCrit'), ('diskTempWarn', 'diskTempCrit')):
        if out[critical] < out[warn]:
            out[critical] = out[warn]
    return out


def _sanitize_ignored(raw):
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw[-100:]:
        if not isinstance(item, dict):
            continue
        key, fingerprint = item.get('key'), item.get('fp')
        if not isinstance(key, str) or not isinstance(fingerprint, str):
            continue
        try:
            at = int(item.get('at') or 0)
        except (TypeError, ValueError, OverflowError):
            at = 0
        result.append({'key': key[:120], 'fp': fingerprint[:400],
                       'label': str(item.get('label') or key)[:160],
                       'level': item.get('level') if item.get('level') in ('warn', 'crit', 'info') else 'warn',
                       'at': at})
    return result


class FnmonitorPanel:
    """Build fnmonitor's API payloads from Toolbox snapshots."""

    def __init__(self, controller, insights, monitoring, fan_policy, light_policy, monitor, *,
                 state_dir, version='dev', web=None, clock=time.monotonic, wall_clock=time.time,
                 proc_root='/proc', sys_root='/sys'):
        self.controller = controller
        self.insights = insights
        self.monitoring = monitoring
        self.fan_policy = fan_policy
        self.light_policy = light_policy
        self.monitor = monitor
        self.store = PanelStore(state_dir)
        self.version = version
        # ``web`` is the WebConfigServer, used for address/pairing status only.
        self.web = web
        self.clock = clock
        self.wall_clock = wall_clock
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self._mount_cache = (0.0, {})

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def _safe(self, component, default=None):
        if component is None:
            return default if default is not None else {}
        try:
            value = component.snapshot()
            return copy.deepcopy(value) if isinstance(value, dict) else (default if default is not None else {})
        except Exception:
            return default if default is not None else {}

    def _insights(self):
        return self._safe(self.insights)

    def _device(self):
        return _dict(self._insights().get('device'))

    def _basic(self):
        return self._safe(self.monitor)

    def _monitoring(self):
        return self._safe(self.monitoring)

    def _fan(self):
        return self._safe(self.fan_policy)

    def _light(self):
        return self._safe(self.light_policy)

    def _control(self):
        return self._safe(self.controller)

    def _identity(self):
        return _dict(self._device().get('identity'))

    # ------------------------------------------------------------------
    # mounts (read-only, cached): /dev/sda1 -> ["/drives/data"]
    # ------------------------------------------------------------------
    def _mounts(self):
        now = self.clock()
        stamp, cached = self._mount_cache
        if now - stamp < 30:
            return cached
        result = {}
        try:
            text = (self.proc_root / 'self/mountinfo').read_text(encoding='utf-8', errors='replace')[:262144]
        except OSError:
            text = ''
        for line in text.splitlines():
            parts = line.split(' - ', 1)
            if len(parts) != 2:
                continue
            fields, extra = parts[0].split(), parts[1].split()
            if len(fields) < 6 or len(extra) < 2:
                continue
            source, mount = extra[1], fields[4]
            match = re.fullmatch(r'/dev/((?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+))\d*', source)
            if not match or extra[0] in ('tmpfs', 'devtmpfs', 'overlay', 'squashfs', 'proc', 'sysfs'):
                continue
            result.setdefault(match[1], []).append(mount)
        self._mount_cache = (now, result)
        return result

    # ------------------------------------------------------------------
    # shared data builders
    # ------------------------------------------------------------------
    def _cpu(self):
        system = _dict(self._insights().get('system'))
        device = _dict(self._device().get('cpu'))
        cores = _list(system.get('cpu_cores'), 256)
        frequencies = [row.get('current') for row in _list(device.get('frequencies_mhz'), 256)
                       if _number(row.get('current')) is not None]
        per_core = [_number(row.get('usage_percent'), 0, 100)
                    for row in sorted(cores, key=lambda item: item.get('id')) if type(row.get('id')) is int]
        return {
            'percent': _number(system.get('cpu_usage_percent'), 0, 100),
            'per_core': per_core,
            'load': [value for value in _list(system.get('load_average'), 3) if _number(value) is not None],
            'cores': _number(device.get('physical_cores'), 1, 4096),
            'threads': _number(device.get('logical_cores'), 1, 8192) or _number(system.get('cpu_logical_cores'), 1, 8192),
            'logical_cpus': _number(device.get('logical_cores'), 1, 8192) or _number(system.get('cpu_logical_cores'), 1, 8192),
            'sockets': _number(device.get('sockets'), 1, 64),
            'model': _text(device.get('model') or system.get('cpu_model'), 160),
            'frequency_mhz': max(frequencies) if frequencies else None,
        }

    def _mem(self):
        memory = _dict(_dict(self._insights().get('system')).get('memory'))
        total = _number(memory.get('total_bytes'), 1)
        used = _number(memory.get('used_bytes'), 0)
        available = _number(memory.get('available_bytes'), 0)
        percent = _number(memory.get('usage_percent'), 0, 100)
        swap_total = _number(memory.get('swap_total_bytes'), 0)
        swap_used = _number(memory.get('swap_used_bytes'), 0)
        return {
            'total': total, 'used': used, 'available': available, 'free': available,
            'percent': percent, 'cached': None, 'buffers': None,
            'swap_total': swap_total, 'swap_used': swap_used,
            'swap_free': swap_total - swap_used if swap_total is not None and swap_used is not None else None,
            'swap_percent': _number(memory.get('swap_usage_percent'), 0, 100),
        }

    def _disks(self):
        insight, device = self._insights(), self._device()
        storage = _dict(device.get('storage'))
        inventory = {row.get('name'): row for row in _list(storage.get('devices'), 128)
                     if isinstance(row, dict) and isinstance(row.get('name'), str)}
        mounts = self._mounts()
        basic = {entry.get('mount'): entry for entry in _list(self._basic().get('disks'), 128)
                 if isinstance(entry, dict)}
        rows = []
        for item in _list(_dict(insight.get('disks')).get('items'), 128):
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            name = item['id']
            physical = _dict(inventory.get(name))
            health = _dict(item.get('health'))
            passed = health.get('passed')
            disk_mounts = mounts.get(name, [])
            total = used = 0
            measured = False
            for mount in disk_mounts:
                entry = _dict(basic.get(mount))
                if _number(entry.get('total_bytes')) is not None and _number(entry.get('used_bytes')) is not None:
                    total += entry['total_bytes']
                    used += entry['used_bytes']
                    measured = True
            size = _number(item.get('size_bytes'), 0) or _number(physical.get('bytes'), 0)
            rpm = physical.get('rotational')
            media = ('HDD' if rpm == 1 else 'SSD' if rpm == 0 else '')
            rows.append({
                'name': name, 'device': item.get('device') or '/dev/' + name,
                'model': _text(item.get('model') or physical.get('model'), 120),
                'vendor': _text(physical.get('vendor'), 80),
                'serial': _text(physical.get('serial'), 120),
                'size': size, 'total': total if measured else None,
                'used': used if measured else None,
                'percent': used / total * 100 if measured and total > 0 else None,
                'mounts': disk_mounts, 'slot': None,
                'rpm': media, 'kind': 'nvme' if name.startswith('nvme') else media.lower(),
                'temp': _number(item.get('temp_c'), -40, 200),
                'health': 'PASS' if passed is True else 'FAIL' if passed is False else None,
                'health_reason': _text(health.get('reason'), 160),
                'smart_available': bool(item.get('smart_available')),
                'standby': bool(item.get('standby')),
                'smart_status': item.get('smart_status'),
                'temperature_critical_c': _number(item.get('temperature_critical_c'), 0, 200),
            })
        return rows

    def _disk_summary(self):
        """Total data-disk usage straight from the sampler's deduplicated mounts."""
        total = used = 0
        measured = False
        for entry in _list(self._basic().get('disks'), 128):
            if not isinstance(entry, dict):
                continue
            size = _number(entry.get('total_bytes'), 1)
            taken = _number(entry.get('used_bytes'), 0)
            if size is not None and taken is not None:
                total += size
                used += taken
                measured = True
        if not measured or total <= 0:
            return None, None, None
        return total, used, used / total * 100

    def _net(self):
        insight, device = self._insights(), self._device()
        links = {row.get('name'): row for row in _list(_dict(device.get('network')).get('interfaces'), 128)
                 if isinstance(row, dict) and isinstance(row.get('name'), str)}
        rows = []
        for item in _list(insight.get('interfaces'), 128):
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                continue
            name = item['name']
            link = _dict(links.get(name))
            ipv6 = [row.get('address') for row in _list(link.get('ipv6_addresses'), 32)
                    if isinstance(row, dict) and isinstance(row.get('address'), str)]
            gateways = _dict(link.get('default_gateways'))
            gateway_rows = [row for row in _list(gateways.get('ipv4'), 4) if isinstance(row, dict)]
            rows.append({
                'iface': name, 'name': name,
                'ipv4': [value for value in _list(item.get('ipv4'), 32) if isinstance(value, str)],
                'ipv6': ipv6,
                'mac': _text(item.get('mac') or link.get('mac'), 64),
                'state': _text(item.get('operstate') or link.get('state'), 32),
                'operstate': _text(item.get('operstate') or link.get('state'), 32),
                'carrier': _number(link.get('carrier'), 0, 1),
                'speed_mbps': _number(link.get('speed_mbps'), 1),
                'duplex': _text(link.get('duplex'), 32),
                'mtu': _number(link.get('mtu'), 1),
                'rx_bytes': _number(item.get('rx_bytes'), 0),
                'tx_bytes': _number(item.get('tx_bytes'), 0),
                'rx_rate': _number(item.get('rx_bytes_per_sec'), 0),
                'tx_rate': _number(item.get('tx_bytes_per_sec'), 0),
                'gateway': gateway_rows[0].get('gateway') if gateway_rows else None,
            })
        return rows

    def _sensors(self):
        device = self._device()
        temps, fans = [], []
        for row in _list(device.get('thermal'), 256):
            if not isinstance(row, dict):
                continue
            celsius = _number(row.get('celsius'), -100, 250)
            if celsius is None:
                continue
            label = _text(row.get('label'), 80) or _text(row.get('channel'), 40) or '温度'
            driver = str(row.get('driver') or '')
            name = label
            if driver:
                name = label + ' · ' + driver
            temps.append({'name': name, 'label': label,
                          'value': celsius, 'temp': celsius,
                          'max': _number(row.get('max_celsius'), -100, 250),
                          'crit': _number(row.get('critical_celsius'), -100, 250),
                          'source': row.get('source'), 'driver': driver,
                          'channel': row.get('channel'),
                          'type': 'cpu' if driver in ('coretemp', 'k10temp') else 'other'})
        for row in _list(device.get('fans'), 128):
            if not isinstance(row, dict):
                continue
            rpm = _number(row.get('rpm'), 0, 1000000)
            label = _text(row.get('label'), 80) or _text(row.get('channel'), 40) or '风扇'
            fans.append({'name': label, 'label': label, 'rpm': rpm,
                         'pwm': _number(row.get('pwm'), 0, 255),
                         'pwm_enable': _number(row.get('pwm_enable'), 0, 255),
                         'channel': row.get('channel'), 'source': row.get('source')})
        for name, channel in (('CPU 风扇', 'cpu'), ('系统风扇', 'system')):
            state = _dict(self._fan().get('channels', {}).get(channel))
            if _number(state.get('rpm'), 0, 1000000) is None:
                continue
            if any(row.get('rpm') is not None for row in fans):
                continue
            fans.append({'name': name, 'label': name, 'rpm': state['rpm'], 'source': 'fan-policy'})
        return {'temps': temps, 'fans': fans, 'volts': [],
                'available': bool(temps or fans),
                'fans_source': 'hwmon / fan-policy' if fans else ''}

    def _power(self):
        power = _dict(self._monitoring().get('power'))
        package = _number(power.get('cpu_package_w'), 0, 2000)
        ok = bool(power.get('available')) and package is not None
        return {'ok': ok, 'total': package if ok else None,
                'source': 'rapl' if ok else 'unavailable',
                'package': package, 'core': None, 'uncore': None, 'dram': None, 'base': None,
                'disks_w': 0, 'disks_active': 0,
                'reason': None if ok else _text(power.get('reason'), 160) or '未取得 RAPL 实测功耗；不估算整机功耗'}

    def _temperature(self, disks):
        basic = self._basic()
        cpu = _number(_dict(basic.get('cpu')).get('temp_c'), -40, 200)
        sensors = self._sensors()
        if cpu is None:
            cpu = max([row['value'] for row in sensors['temps'] if row.get('type') == 'cpu'], default=None)
        board = [row['value'] for row in sensors['temps']
                 if row.get('type') != 'cpu' and row.get('driver') not in ('coretemp', 'k10temp')]
        disk_temps = [{'name': disk['name'], 'temp': disk['temp']} for disk in disks
                      if _number(disk.get('temp'), -40, 200) is not None]
        return {'cpu': cpu, 'system': max(board) if board else None, 'disks': disk_temps}

    def _disk_io(self):
        rows = []
        for row in _list(self._monitoring().get('disk_io'), 128):
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                continue
            rows.append({'name': row['name'],
                         'read_rate': _number(row.get('read_bytes_per_sec'), 0),
                         'write_rate': _number(row.get('write_bytes_per_sec'), 0),
                         'read_bytes_per_sec': _number(row.get('read_bytes_per_sec'), 0),
                         'write_bytes_per_sec': _number(row.get('write_bytes_per_sec'), 0)})
        return rows

    def _gpu(self):
        rows = []
        pci_names = {row.get('address'): row for row in _list(self._identity().get('pci'), 256)
                     if isinstance(row, dict)}
        for item in _list(_dict(self._insights().get('gpu')).get('devices'), 32):
            if not isinstance(item, dict):
                continue
            vendor = str(item.get('vendor') or '').lower().replace('0x', '')
            used = _number(item.get('vram_used_bytes'), 0)
            total = _number(item.get('vram_total_bytes'), 0)
            name = _text(item.get('name'), 80) or 'GPU'
            rows.append({
                'name': name, 'vendor': vendor, 'type': 'intel' if vendor == '8086' else 'drv',
                'util': _number(item.get('busy_percent'), 0, 100),
                'util_source': 'sysfs' if _number(item.get('busy_percent'), 0, 100) is not None else None,
                'temp': _number(item.get('temp_c'), -40, 200),
                'freq_mhz': _number(item.get('freq_mhz'), 0, 100000),
                'mem_total': total, 'mem_used': used,
                'mem_pct': used / total * 100 if used is not None and total else None,
                'shared_memory': bool(total in (None, 0)),
                'pci': item.get('pci'), 'driver': item.get('driver'),
                'model': _text(item.get('model'), 120),
            })
        return rows

    def _hardware(self):
        identity, device = self._identity(), self._device()
        dmi = _dict(identity.get('dmi'))
        cpu = _dict(device.get('cpu'))
        board = {
            'sys_vendor': _text(dmi.get('sys_vendor'), 120),
            'board_vendor': _text(dmi.get('board_vendor'), 120),
            'board_name': _text(dmi.get('board_name'), 120),
            'board_version': _text(dmi.get('board_version'), 120),
            'bios_vendor': _text(dmi.get('bios_vendor'), 120),
            'bios_version': _text(dmi.get('bios_version'), 120),
            'bios_date': _text(dmi.get('bios_date'), 64),
            'product_name': _text(dmi.get('product_name'), 160),
            'product_version': _text(dmi.get('product_version'), 120),
            'chassis_type': _text(dmi.get('chassis_type'), 32),
        }
        return {
            'cpu': {'model': _text(cpu.get('model'), 160), 'cores': _number(cpu.get('physical_cores'), 1),
                    'threads': _number(cpu.get('logical_cores'), 1, 8192),
                    'sockets': _number(cpu.get('sockets'), 1), 'arch': _text(cpu.get('architecture'), 32)},
            'board': board,
            'gpu': [row.get('name') for row in self._gpu()],
            'net': [row.get('iface') for row in self._net() if row.get('mac')],
            'pci': _list(identity.get('pci'), 256),
        }

    def _raid(self):
        rows = []
        for item in _list(_dict(self._device().get('storage')).get('raid'), 32):
            if not isinstance(item, dict):
                continue
            members = _list(item.get('members'), 64)
            degraded = _number(item.get('degraded'), 0)
            state = _text(item.get('state'), 64) or ''
            status = state
            if degraded:
                status = (state + ' · 降级 ' + str(degraded)).strip()
            rows.append({'device': item.get('name') or 'md', 'level': _text(item.get('level'), 32),
                         'status': status, 'members': members, 'degraded': degraded,
                         'sync_action': _text(item.get('sync_action'), 32),
                         'sync_completed': _text(item.get('sync_completed'), 64)})
        return rows

    def _system(self):
        basic, identity, device = self._basic(), self._identity(), self._device()
        os_release = _dict(identity.get('os'))
        firmware = _dict(identity.get('firmware'))
        # device_info records the version under the file that supplied it; accept
        # any of the known keys rather than only the JSON one.
        firmware_version = next((_text(firmware[key], 80) for key in
                                 ('version', 'firmware_version', 'hiksemi-release', 'histor-release')
                                 if _text(firmware.get(key), 80)), None)
        # Prefer the interface that carries the default route, then physical
        # ports, so the hero never shows docker0 or 127.0.0.1 instead of the LAN.
        virtual = re.compile(r'^(?:lo|docker|veth|br-|virbr|tun|tap|vnet|lxc|kube|wg|vxlan)')
        rows = sorted(self._net(), key=lambda row: (
            0 if row.get('gateway') else 1,
            1 if virtual.match(str(row.get('iface') or '')) else 0,
            str(row.get('iface') or '')))
        ips = [{'name': row.get('iface'), 'ipv4': row.get('ipv4') or [],
                'ipv6': row.get('ipv6') or []} for row in rows]
        uptime = _number(basic.get('uptime_seconds'), 0) or _number(_dict(self._insights().get('system')).get('uptime_seconds'), 0)
        return {
            'hostname': _text(basic.get('hostname') or identity.get('hostname'), 120),
            'kernel': _text(identity.get('kernel'), 120),
            'kernel_version': _text(identity.get('kernel'), 120),
            'arch': _text(_dict(device.get('cpu')).get('architecture'), 32),
            'uptime': uptime,
            'boot_time': (self.wall_clock() - uptime) if uptime else None,
            'cpu_model': _text(_dict(device.get('cpu')).get('model'), 160),
            # R1's official firmware metadata replaces fnOS version fields.
            'fnos_version': firmware_version,
            'os_name': _text(os_release.get('PRETTY_NAME') or os_release.get('NAME'), 120),
            'os_id': _text(os_release.get('ID'), 32),
            'firmware': {key: _text(firmware.get(key), 128) for key in ('version', 'firmware_version', 'build', 'build_date', 'build_time', 'model') if firmware.get(key)},
            'product_name': _text(_dict(identity.get('dmi')).get('product_name'), 160),
            'ips': ips,
            'version': self.version,
        }

    def _docker(self):
        containers = _dict(self._insights().get('containers'))
        rows = []
        for item in _list(containers.get('items'), 256):
            if not isinstance(item, dict):
                continue
            cpu = _number(item.get('cpu_percent'), 0, 100000)
            memory = _number(item.get('memory_bytes'), 0)
            rows.append({
                'id': item.get('id'), 'name': item.get('name'), 'image': item.get('image'),
                'state': item.get('state'), 'status': item.get('status'),
                'ports': item.get('ports') or '', 'protected': bool(item.get('protected')),
                'available_actions': _list(item.get('available_actions'), 4),
                'cpu': cpu, 'cpu_percent': cpu,
                'mem_usage': _bytes_text(memory), 'memory_bytes': memory,
                'mem_percent': _number(item.get('memory_percent'), 0, 100),
                'memory_percent': _number(item.get('memory_percent'), 0, 100),
            })
        return {'available': bool(containers.get('available')),
                'error': None if containers.get('available') else (_text(containers.get('reason'), 200) or 'Docker 不可用'),
                'containers': rows, 'images': -1,
                'sampled_at': _stamp(containers.get('sampled_at'))}

    def _ports(self):
        listeners = []
        for row in _list(self._insights().get('ports'), 256):
            if not isinstance(row, dict):
                continue
            protocol = str(row.get('protocol') or 'tcp')
            listeners.append({'port': row.get('port'), 'proto': protocol,
                              'addr': str(row.get('address') or ''), 'process': None, 'pid': None})
        docker_rows = [{'name': row['name'], 'state': row['state'],
                        'ports': [part.strip() for part in str(row['ports'] or '').split(',') if part.strip()]}
                       for row in self._docker()['containers']]
        return {'apps': [], 'docker': docker_rows, 'listeners': listeners, 'ts': self.wall_clock()}

    def _modules(self):
        # fnOS app modules do not exist on the R1; return an explicitly empty list
        # instead of inventing service states.
        return []

    def _apps(self):
        return []

    def _memory_modules(self):
        return []

    def _vms(self):
        capability = _dict(self._insights().get('vms'))
        rows = []
        for item in _list(capability.get('items'), 64):
            if not isinstance(item, dict):
                continue
            state = str(item.get('state') or '')
            rows.append({'uuid': item.get('id'), 'name': item.get('name'),
                         'title': item.get('name'), 'state': state,
                         'running': state in ('running', 'blocked'),
                         'state_zh': _vm_state_zh(state), 'vcpus': None, 'mem_max': None,
                         'os': None, 'ips': []})
        return {'available': bool(capability.get('available')),
                'installed': bool(capability.get('available')),
                'kvm': os.path.exists('/dev/kvm'),
                'error': None if capability.get('available') else _text(capability.get('reason'), 200),
                'vms': rows, 'sampled_at': _stamp(capability.get('sampled_at'))}

    # ------------------------------------------------------------------
    # fnmonitor endpoints
    # ------------------------------------------------------------------
    def overview(self):
        disks = self._disks()
        sensors = self._sensors()
        total_size, used, percent = self._disk_summary()
        fan = self._fan()
        light = self._light()
        return {
            'cpu': self._cpu(),
            'mem': self._mem(),
            'disks': [{'mount': mount, 'name': disk['name']} for disk in disks for mount in disk.get('mounts', [])],
            'disk_total': percent,
            'disk_used': used,
            'disk_size': total_size,
            'disk_pools': len({mount for disk in disks for mount in disk.get('mounts', [])}),
            'disks_detail': disks,
            'idrac_slot_state': 'off',
            'net': self._net(),
            'diskio': self._disk_io(),
            'temp': self._temperature(disks),
            'power': self._power(),
            'gpu': self._gpu(),
            'memory': self._memory_modules(),
            'uptime': _number(self._basic().get('uptime_seconds'), 0),
            'ts': self.wall_clock(),
            'interval': self.store.config().get('interval', SAMPLE_INTERVAL),
            'docker': self._docker(),
            'modules': self._modules(),
            'ports': self._ports(),
            'hardware': self._hardware(),
            'raid': self._raid(),
            'raidcard': {'type': 'none', 'label': '', 'detail': '', 'tool': '', 'controllers': [], 'error': ''},
            'sensors': sensors,
            'fans': sensors['fans'],
            'apps': self._apps(),
            'system': self._system(),
            'config': self.config(),
            'r1': {'fan': self.r1_summary(fan), 'light': self.r1_summary(light),
                   'panel_version': self.version},
        }

    def system(self):
        data = self._system()
        data['db_size'] = None
        data['data_dir'] = str(self.store.state_dir)
        return data

    def sensors(self):
        return self._sensors()

    def fans(self):
        return {'fans': self._sensors()['fans']}

    def docker(self):
        return self._docker()

    def ports(self):
        return {'ports': self._ports()}

    def hardware(self):
        return {'hardware': self._hardware(), 'raid': self._raid()}

    def memory(self):
        return self._memory_modules()

    def power(self):
        return self._power()

    def power_stats(self):
        power = self._power()
        return {'realtime': {'power_w': power['total'], 'source': power['source'],
                             'package': power['package'], 'core': None, 'dram': None,
                             'base': None, 'disks_w': 0, 'disks_active': 0},
                'energy': {'today_kwh': None, 'month_kwh': None, 'boot_kwh': None, 'basis': 'rapl_only'},
                'cost': {'today_yuan': None, 'month_yuan': None,
                         'rate_yuan_per_kwh': self.store.config().get('power_rate_yuan')},
                'disks': [], 'standby_protect': True,
                'estimation_params': {'tdp_w': 0, 'disk_typical_w': 0, 'nic_fixed_w': 0, 'base_w': 0},
                'reason': power.get('reason')}

    def traffic(self):
        settings = _dict(self._monitoring().get('settings'))
        snapshot = self._safe(self.monitoring)
        try:
            traffic = self.monitoring.traffic() if self.monitoring is not None else {}
        except Exception:
            traffic = {}
        rows, totals_from = [], _dict(traffic.get('totals'))
        for item in _list(traffic.get('interfaces'), 128):
            if not isinstance(item, dict):
                continue
            today, month = _dict(item.get('today')), _dict(item.get('month'))
            rows.append({'name': item.get('name'),
                         'today_rx': _number(today.get('rx_bytes'), 0),
                         'today_tx': _number(today.get('tx_bytes'), 0),
                         'month_rx': _number(month.get('rx_bytes'), 0),
                         'month_tx': _number(month.get('tx_bytes'), 0)})
        today = _dict(totals_from.get('today'))
        month = _dict(totals_from.get('month'))
        net = self._net()
        return {
            'realtime': {'rx_rate': sum(row.get('rx_rate') or 0 for row in net),
                         'tx_rate': sum(row.get('tx_rate') or 0 for row in net)},
            'ifaces': rows,
            'totals': {'boot_rx': None, 'boot_tx': None,
                       'today_rx': _number(today.get('rx_bytes'), 0),
                       'today_tx': _number(today.get('tx_bytes'), 0),
                       'month_rx': _number(month.get('rx_bytes'), 0),
                       'month_tx': _number(month.get('tx_bytes'), 0),
                       'basis': 'counter_deltas'},
            'containers': [],
            'config': {'exclude_bridge': bool(settings.get('traffic_exclude_virtual'))},
            'reason': _text(traffic.get('reason'), 200),
        }

    def gpu(self, diag=False):
        rows = self._gpu()
        payload = {'gpu': rows}
        if diag:
            payload['diag'] = _dict(self._insights().get('gpu')).get('reason')
        return payload

    def raidcard(self):
        return {'type': 'none', 'label': '', 'detail': '', 'tool': '', 'controllers': [], 'error': ''}

    def processes(self):
        rows = []
        for item in _list(self._insights().get('processes'), 64):
            if not isinstance(item, dict):
                continue
            rows.append({'pid': item.get('pid'), 'name': item.get('name'),
                         'comm': item.get('name'), 'cpu': _number(item.get('cpu_percent'), 0),
                         'rss': _number(item.get('memory_bytes'), 0), 'args': ''})
        return {'processes': rows}

    def apps(self):
        return {'apps': []}

    def vms(self):
        return self._vms()

    def vm_probe(self):
        capability = _dict(self._insights().get('vms'))
        return {'installed': bool(capability.get('available')),
                'kvm': os.path.exists('/dev/kvm')}

    def modules(self):
        return {'modules': []}

    def history(self, metric, range_name):
        if range_name not in RANGES:
            raise ValueError('时间范围无效')
        if metric not in HISTORY_MAP and metric not in ('net', 'diskio', 'net_bytes', 'disk_total', 'fan_avg'):
            raise ValueError('未知指标')
        span = range_name if range_name in ('10m', '1h', '6h', '24h', '7d', '30d') else '30d' if range_name == 'all' else '24h'
        payload = self._history_payload(span)
        if metric == 'net':
            return {'metric': 'net', 'range': range_name,
                    'rx': payload.get('rx_bytes_per_sec', []), 'tx': payload.get('tx_bytes_per_sec', [])}
        if metric == 'net_bytes':
            return {'metric': 'net_bytes', 'range': range_name, 'rx': [], 'tx': []}
        if metric == 'diskio':
            return {'metric': 'diskio', 'range': range_name,
                    'r': _mb_series(payload.get('disk_read_bytes_per_sec', [])),
                    'w': _mb_series(payload.get('disk_write_bytes_per_sec', []))}
        identifier = HISTORY_MAP.get(metric)
        points = payload.get(identifier, []) if identifier else []
        return {'metric': metric, 'range': range_name, 'points': points}

    def _history_payload(self, span):
        result = {}
        if self.monitoring is None:
            return result
        try:
            raw = self.monitoring.history(span)
        except Exception:
            return result
        for series in _list(_dict(raw).get('series'), 128):
            if not isinstance(series, dict):
                continue
            identifier = series.get('id')
            if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', identifier):
                continue
            points = []
            for point in _list(series.get('points'), 720):
                if isinstance(point, list) and len(point) == 2 and _number(point[0], 0) is not None:
                    points.append([int(point[0]), _number(point[1])])
            if isinstance(identifier, str) and identifier.startswith('temperature.'):
                continue
            result[identifier] = points
        return result

    def history_diag(self):
        if self.monitoring is None:
            return {'available': False, 'reason': '统计服务未启用'}
        try:
            return self.monitoring.history_diag()
        except Exception as error:
            return {'available': False, 'reason': type(error).__name__}

    def smart(self, device):
        """Disk detail from the background probe; never runs smartctl here."""
        if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', device):
            raise ValueError('设备标识无效')
        for disk in self._disks():
            if disk['name'] == device:
                return {'ok': True, 'dev': device, 'model': disk.get('model'),
                        'serial': disk.get('serial'), 'size': disk.get('size'),
                        'temp': disk.get('temp'), 'health': disk.get('health'),
                        'standby': disk.get('standby'),
                        'reason': disk.get('health_reason'),
                        'source': 'background_smartctl', 'attributes': [],
                        'note': '页面读取后台 SMART 缓存，不会为打开详情而启动磁盘或自检'}
        return {'ok': False, 'dev': device, 'error': '未找到该硬盘的 SMART 观测'}

    def disk_health(self):
        """Grades for the bay cards, derived from the background SMART probe."""
        items, summary = {}, {'ok': 0, 'warn': 0, 'crit': 0, 'unknown': 0}
        stamp = None
        for disk in self._disks():
            grade, detail = 'unknown', ''
            if disk.get('standby'):
                grade, detail = 'ok', '休眠中'
            elif disk.get('health') == 'FAIL':
                grade, detail = 'crit', disk.get('health_reason') or 'SMART 未通过'
            elif disk.get('health') == 'PASS':
                critical = _number(disk.get('temperature_critical_c'), 0, 200)
                temp = _number(disk.get('temp'), -40, 200)
                if temp is not None and critical is not None and temp >= critical - 5:
                    grade, detail = 'warn', '温度接近临界值 %.0f°C' % temp
                else:
                    grade = 'ok'
            elif disk.get('health_reason'):
                grade, detail = 'unknown', disk['health_reason']
            key = grade if grade in summary else 'unknown'
            summary[key] += 1
            items[disk['name']] = {'grade': grade, 'detail': detail}
        sampled = _dict(self._insights().get('disks')).get('sampled_at')
        stamp = _stamp(sampled) or int(self.wall_clock())
        return {'ok': True, 'enabled': True, 'interval_h': 0, 'presets': [],
                'last_ts': stamp, 'next_ts': None, 'scanning': False,
                'summary': summary, 'error': '', 'items': items, 'now': self.wall_clock(),
                'source': 'background_smart'}

    def scan_log(self, query=None):
        return {'ok': True, 'items': [], 'keep_ok_days': 0,
                'note': 'R1 Toolbox 只做只读 SMART 观测，不执行巡检自检'}

    def notify(self):
        return {'enabled': 0, 'provider': 'custom', 'host': '', 'port': 0, 'secure': 'ssl',
                'user': '', 'sender': '', 'to': '', 'sct_enabled': 0, 'hw_enabled': 0,
                'hw_time': '08:00', 'password_set': False, 'sct_key_set': False, 'ok': True,
                'providers': [], 'last': {}, 'notified': {},
                'reason': 'R1 Toolbox 不发送外部通知；提醒只在面板与本地告警中显示'}

    def idrac(self):
        return {'ok': False, 'configured': False, 'error': '', 'reason': 'R1 不是戴尔 iDRAC 平台',
                'config': {'enabled': 0, 'host': '', 'user': '', 'verify_tls': 0,
                           'sel_alert': 1, 'password_set': False},
                'fans': [], 'temps': [], 'psu': [], 'disks': [], 'dimms': [],
                'volumes': [], 'controllers': [], 'cpus': []}

    def daily(self, days=30):
        try:
            count = max(1, min(90, int(days)))
        except (TypeError, ValueError):
            count = 30
        rows = []
        today = datetime.date.today()
        for offset in range(count - 1, -1, -1):
            day = today - datetime.timedelta(days=offset)
            rows.append({'day': day.isoformat(), 'has_data': False, 'kwh': None, 'cost': None,
                         'power_h': None, 'avg_w': None, 'rx': None, 'tx': None,
                         'cpu_avg': None, 'cpu_max': None})
        return {'days': rows, 'today': today.isoformat(), 'archived_until': None,
                'missing': '', 'rate': self.store.config().get('power_rate_yuan'),
                'exclude_bridge': False, 'power_source': 'rapl_only'}

    def monthly_summary(self):
        return {'available': False, 'enabled': False, 'retention_days': self.store.config().get('retention_days'),
                'note': 'R1 Toolbox 不提供月度归档；历史曲线保留 1–30 天'}

    def weather(self):
        return {'ok': False, 'error': 'R1 Toolbox 不访问外网天气服务'}

    def report(self, fmt='json'):
        if fmt == 'html':
            return self._report_html()
        if self.monitoring is None:
            return {'available': False, 'reason': '统计服务未启用'}
        try:
            report = self.monitoring.report()
        except Exception:
            return {'available': False, 'reason': '报告生成失败'}
        report = copy.deepcopy(_dict(report))
        report['panel'] = 'R1 Toolbox ' + self.version
        return report

    def _report_html(self):
        overview = self.overview()
        rows = ''.join('<tr><th>%s</th><td>%s</td></tr>' % (label, value) for label, value in (
            ('主机名', _esc(overview['system'].get('hostname') or '--')),
            ('CPU', _esc(overview['cpu'].get('model') or '--')),
            ('CPU 使用率', _fmt(overview['cpu'].get('percent'), '%')),
            ('内存使用率', _fmt(overview['mem'].get('percent'), '%')),
            ('存储使用率', _fmt(overview['disk_total'], '%')),
            ('CPU 温度', _fmt((overview['temp'] or {}).get('cpu'), ' ℃')),
            ('CPU 封装功耗', _fmt(overview['power'].get('total'), ' W')),
            ('运行时长', _duration(overview.get('uptime'))),
        ))
        return ('<!doctype html><meta charset="utf-8"><title>R1 Toolbox 健康报告</title>'
                '<style>body{font:15px/1.6 system-ui,sans-serif;margin:32px;color:#111}'
                'h1{font-size:20px}table{border-collapse:collapse;margin-top:16px}'
                'th,td{border-bottom:1px solid #ddd;padding:8px 16px;text-align:left}'
                'th{color:#555;font-weight:500}</style><h1>R1 Toolbox 健康报告</h1>'
                '<p>生成时间 %s · 仅包含本机可读到的真实采样</p><table>%s</table>'
                % (_esc(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')), rows))

    def export_csv(self, range_name='7d'):
        if self.monitoring is None:
            raise RuntimeError('统计服务未启用')
        span = range_name if range_name in RANGES else '7d'
        if span == 'all':
            span = '7d'
        return self.monitoring.export_csv(span)

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def config(self):
        stored = self.store.config()
        monitoring_settings = _dict(self._monitoring().get('settings'))
        stored['retention_days'] = int(_number(monitoring_settings.get('retention_days'), 1, 30)
                                       or stored.get('retention_days') or 30)
        stored['traffic_exclude_bridge'] = 1 if monitoring_settings.get('traffic_exclude_virtual') else 0
        auth = self.web.auth.public_config() if self.web is not None and getattr(self.web, 'auth', None) else {
            'auth_enabled': 0, 'auth_password_set': False, 'auth_session_days': 7}
        stored.update(auth)
        stored.update({'version': self.version, 'arch': _dict(self._device().get('cpu')).get('architecture') or 'x86_64',
                       'listen_port': getattr(self.web, 'port', None), 'data_dir': '',
                       'data_dir_actual': str(self.store.state_dir), 'ui_file': str(self.store.ui_path),
                       'auth_has_password': auth.get('auth_password_set', False),
                       'password_min_len': 6,
                       'update_status': {'last_check': None, 'latest': self.version, 'has_update': False,
                                         'downloading': False, 'installing': False,
                                         'downloaded_file': '', 'download_dir': '', 'error': ''}})
        return stored

    def save_config(self, data):
        """Apply the settings that map onto real services; report the rest."""
        if not isinstance(data, dict):
            raise ValueError('配置必须是对象')
        auth = data.pop('auth_password_hash', None) if 'auth_password_hash' in data else None
        changes = {key: value for key, value in data.items() if key in PANEL_DEFAULTS}
        notes = []
        stored = self.store.save_config(changes) if changes else self.store.config()
        if 'retention_days' in changes and self.monitoring is not None:
            try:
                self.monitoring.action('monitor.settings.update',
                                       {'settings': {'retention_days': changes['retention_days']}})
                notes.append('历史保留天数已应用')
            except Exception:
                notes.append('历史保留天数未能应用，请检查统计服务')
        if 'traffic_exclude_bridge' in changes and self.monitoring is not None:
            try:
                self.monitoring.action('monitor.settings.update',
                                       {'settings': {'traffic_exclude_virtual': bool(changes['traffic_exclude_bridge'])}})
                notes.append('流量统计网卡范围已应用')
            except Exception:
                notes.append('流量统计网卡范围未能应用')
        ignored = sorted(set(changes) - {'retention_days', 'traffic_exclude_bridge'})
        if ignored:
            notes.append('以下项仅保存在面板：' + '、'.join(ignored))
        if auth is not None:
            notes.append('登录口令请在「登录口令」分组修改，不随此接口保存')
        if self.web is not None and 'weather_city' in data:
            notes.append('R1 Toolbox 不访问外网天气')
        config = self.config()
        return {'ok': True, 'config': config, 'note': '；'.join(notes) if notes else None}

    # ------------------------------------------------------------------
    # R1-specific runtime state and actions
    # ------------------------------------------------------------------
    @staticmethod
    def r1_summary(snapshot):
        """Trim a policy snapshot so the overview payload stays small."""
        keys = ('active', 'busy', 'capability', 'pending_changes', 'last_error',
                'recovery_confirmed', 'settings_error', 'reason', 'fallback', 'target',
                'reference_c', 'source_id', 'source_label')
        return {key: snapshot[key] for key in keys if key in snapshot}

    def r1_state(self, *, authorized=False):
        fan = self._fan()
        light = self._light()
        control = self._control()
        settings = _dict(control.get('settings'))
        capabilities = _dict(control.get('capabilities'))
        auth = self.web.auth if self.web is not None and getattr(self.web, 'auth', None) else None
        monitoring_settings = _dict(self._monitoring().get('settings'))
        pairing = self.web.snapshot()['pairing'] if self.web is not None else {'active': False, 'expires_in': 0}
        return {
            'ok': True,
            'version': self.version,
            'auth': {
                'authorized': bool(authorized),
                'password_required': bool(auth.required()) if auth else False,
                'password_set': bool(auth.password_set()) if auth else False,
                'enabled': bool(auth.enabled()) if auth else False,
                'session_days': auth.session_days() if auth else 7,
                'password_min_len': 6,
                'pairing': pairing,
            },
            'web': {'port': getattr(self.web, 'port', None), 'host': getattr(self.web, 'host', None),
                    'addresses': self.web._addresses() if self.web is not None else []},
            'fan': fan,
            'light': light,
            'control': {'capabilities': capabilities,
                        'screen': {key: _dict(control.get('screen')).get(key) for key in ('brightness', 'asleep')},
                        'rgb': {key: _dict(control.get('rgb')).get(key) for key in ('mode', 'color', 'brightness')},
                        'fans': _dict(control.get('fans')),
                        'power': _dict(control.get('power')),
                        'settings': settings},
            'monitoring': {'settings': monitoring_settings,
                           'available': bool(_dict(self._monitoring()).get('available'))},
            'device': {'hostname': self._system().get('hostname'),
                       'model': self._hardware()['board'].get('product_name'),
                       'firmware': self._system().get('fnos_version'),
                       'kernel': self._system().get('kernel')},
        }


def _bytes_text(value):
    value = _number(value, 0)
    if value is None:
        return ''
    units = ('KiB', 'MiB', 'GiB', 'TiB')
    scaled = float(value)
    unit = 'B'
    for name in units:
        if scaled < 1024:
            break
        scaled /= 1024.0
        unit = name
    text = str(int(scaled)) if scaled >= 100 or unit == 'B' else '%.1f' % scaled
    return text + ' ' + unit


def _mb_series(points):
    result = []
    for point in points:
        if isinstance(point, list) and len(point) == 2:
            value = _number(point[1], 0)
            result.append([point[0], value / 1048576 if value is not None else None])
    return result


def _vm_state_zh(state):
    return {'running': '运行中', 'shut off': '已关机', 'paused': '已暂停',
            'blocked': '阻塞', 'crashed': '已崩溃'}.get(state, state or '未知')


def _fmt(value, unit=''):
    if _number(value) is None:
        return '--'
    text = ('%.1f' % value).rstrip('0').rstrip('.')
    return text + unit


def _duration(value):
    if _number(value, 0) is None:
        return '--'
    seconds = int(value)
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return '%d 天 %d 小时' % (days, hours)
    if hours:
        return '%d 小时 %d 分' % (hours, minutes)
    return '%d 分钟' % minutes


def _esc(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))
