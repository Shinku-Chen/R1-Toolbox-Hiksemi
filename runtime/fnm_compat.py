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
* GET and SMART detail only project dated cache. Page entry may separately POST
  an owner-authorized ordinary refresh; idle monitoring never triggers SMART.
  Ordinary refresh requests standby skip, with no absolute no-wake guarantee.
* fnOS-only integrations (iDRAC, RAID cards, the app centre, the self-updater)
  report themselves unavailable instead of pretending to exist.  Weather uses
  Open-Meteo instead of the fnOS weather provider.
"""

import copy
import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request


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
    'temp_mb': 'board_temperature_c',
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


def _serial(value):
    """A full identity is indivisible: reject an invalid value, never truncate."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not 0 < len(value) <= 512
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        return None
    return value


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
            if key == 'weather_city':
                value = _weather_place(value)
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


_WMO_TEXT = {
    0: '晴', 1: '大部晴朗', 2: '局部多云', 3: '阴', 45: '雾', 48: '雾凇',
    51: '毛毛雨', 53: '毛毛雨', 55: '毛毛雨', 56: '冻雨', 57: '冻雨',
    61: '小雨', 63: '中雨', 65: '大雨', 66: '冻雨', 67: '冻雨',
    71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '阵雨', 81: '阵雨', 82: '强阵雨', 85: '阵雪', 86: '阵雪',
    95: '雷暴', 96: '雷暴伴冰雹', 99: '雷暴伴冰雹',
}


def _weather_coords(value):
    """Parse the user's ``lat,lon`` weather location without accepting junk."""
    parts = [part.strip() for part in str(value or '').split(',')]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _weather_place(value):
    """Return a display-safe place string; never echo markup or control bytes."""
    text = re.sub(r'[\x00-\x1f<>\u007f]', '', str(value or ''))
    return re.sub(r'\s+', ' ', text).strip()[:64]


def _weather_get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'R1-Toolbox/1.0'})
    with urllib.request.urlopen(request, timeout=7) as response:
        if response.status != 200:
            raise ValueError('天气服务返回 HTTP %s' % response.status)
        raw = response.read(262144)
    return json.loads(raw.decode('utf-8', 'replace'))


def fetch_weather(city_override=''):
    """Fetch current weather from Open-Meteo; no API key or bundled secret.

    An empty location uses the NAS public-IP location.  A user-supplied city is
    geocoded by Open-Meteo; ``lat,lon`` is accepted directly.  Only fixed
    provider URLs are contacted, and the city is never interpolated as a URL
    path without quoting.
    """
    try:
        override = _weather_place(city_override)
        lat = lon = None
        city = ''
        if override:
            coords = _weather_coords(override)
            if coords:
                lat, lon = coords
                city = '%.4f,%.4f' % (lat, lon)
            else:
                try:
                    geo = _weather_get('https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh' %
                                       urllib.parse.quote(override))
                    rows = geo.get('results') if isinstance(geo, dict) else None
                    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
                    lat, lon = _weather_coords('%s,%s' % (row.get('latitude'), row.get('longitude')))
                    city = _weather_place(row.get('name') or override)
                except Exception:
                    pass
                if lat is None:
                    return {'ok': False, 'error': '未找到城市「%s」，可改用 纬度,经度 格式' % override}
        else:
            try:
                loc = _weather_get('https://ipwho.is/?lang=zh-CN&fields=success,latitude,longitude,city,region,country')
                if isinstance(loc, dict) and loc.get('success') is not False:
                    lat, lon = _weather_coords('%s,%s' % (loc.get('latitude'), loc.get('longitude')))
                    city = _weather_place(loc.get('city') or loc.get('region') or loc.get('country') or '')
            except Exception:
                pass
            if lat is None:
                try:
                    loc = _weather_get('http://ip-api.com/json/?lang=zh-CN&fields=status,lat,lon,city,regionName,country')
                    if isinstance(loc, dict) and loc.get('status') == 'success':
                        lat, lon = _weather_coords('%s,%s' % (loc.get('lat'), loc.get('lon')))
                        city = _weather_place(loc.get('city') or loc.get('regionName') or loc.get('country') or '')
                except Exception:
                    pass
            if lat is None:
                return {'ok': False, 'error': '无法定位当前网络位置，可在设置中填写城市'}
            city = city or '未知地区'
        data = _weather_get('https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f'
                            '&current=temperature_2m,relative_humidity_2m,apparent_temperature,'
                            'weather_code,wind_speed_10m,is_day&timezone=auto' % (lat, lon))
        current = data.get('current') if isinstance(data, dict) else None
        if not isinstance(current, dict) or _number(current.get('temperature_2m')) is None:
            return {'ok': False, 'error': '天气接口没有返回当前温度'}
        code = current.get('weather_code')
        code = int(code) if type(code) is int else None
        return {'ok': True, 'city': city,
                'temp': round(float(current['temperature_2m']), 1),
                'feels': round(float(_number(current.get('apparent_temperature'), -100, 100) or current['temperature_2m']), 1),
                'humidity': round(float(_number(current.get('relative_humidity_2m'), 0, 100) or 0), 1),
                'wind': round(float(_number(current.get('wind_speed_10m'), 0, 1000) or 0), 1),
                'code': code, 'desc': _WMO_TEXT.get(code, '未知'),
                'is_day': 1 if current.get('is_day') == 1 else 0}
    except Exception as exc:
        return {'ok': False, 'error': '天气服务暂时不可用：%s' % str(exc)[:120]}


_NVME_LABELS = {
    'critical_warning': '严重警告', 'temperature': '温度 (K)', 'available_spare': '可用备用块',
    'available_spare_threshold': '备用块阈值', 'percentage_used': '已用寿命', 'data_units_read': '读取数据单元',
    'data_units_written': '写入数据单元', 'host_read_commands': '主机读取命令', 'host_write_commands': '主机写入命令',
    'controller_busy_time': '控制器忙碌时间', 'power_cycles': '通电次数', 'power_on_hours': '通电时间',
    'unsafe_shutdowns': '异常断电', 'media_errors': '介质错误', 'num_err_log_entries': '错误日志条数',
}

# Only measured health/counter fields belong in the shareable SMART projection.
# Vendor identity strings and messages may contain a full serial or WWN; even an
# owner request must obtain its serial from the independently verified cache.
_SMART_OBSERVATION_FIELDS = frozenset((
    'ata_smart_attributes', 'nvme_smart_health_information_log', 'smart_status',
    'temperature', 'power_on_time', 'power_cycle_count', 'rotation_rate',
    'scsi_grown_defect_list', 'scsi_pending_defects', 'scsi_error_counter_log',
    'manufactured', 'ata_smart_data', 'ata_smart_error_log',
    'ata_smart_selective_self_test_log', 'ata_smart_self_test_log',
))


def _smart_observations(raw):
    """Keep SMART observations, stripping identity even inside nested tables."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if isinstance(key, str) and 'serial' not in key.lower()
                    and key.lower() not in ('wwn', 'logical_unit_id', 'nvme_eui64', 'nvme_nguid', 'scsi_device_id')}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return copy.deepcopy(value)
    return {key: clean(value) for key, value in _dict(raw).items() if key in _SMART_OBSERVATION_FIELDS}


def _smart_rows(raw):
    """Normalize the cached smartctl JSON for the detail table.

    No command is executed here.  The raw JSON remains available for clients
    that need a field the table does not label yet.
    """
    raw = _dict(raw)
    attributes = []
    table = _list(_dict(raw.get('ata_smart_attributes')).get('table'), 256)
    for item in table:
        if not isinstance(item, dict):
            continue
        value = item.get('raw') if isinstance(item.get('raw'), dict) else {}
        attributes.append({
            'id': _number(item.get('id'), 0, 999999),
            'name': _text(item.get('name'), 120) or '—',
            'value': _number(item.get('value'), 0, 1000),
            'worst': _number(item.get('worst'), 0, 1000),
            'threshold': _number(item.get('thresh'), 0, 1000),
            'raw_value': _number(value.get('value'), 0, 2 ** 63 - 1),
            'raw_text': _text(value.get('string'), 160),
            'when_failed': _text(item.get('when_failed'), 80),
        })
    nvme = _dict(raw.get('nvme_smart_health_information_log'))
    nvme_rows = []
    for key, value in sorted(nvme.items()):
        if type(value) is not int and not (isinstance(value, str) and value.strip()):
            continue
        nvme_rows.append({'key': key, 'label': _NVME_LABELS.get(key, key), 'value': value if type(value) is int else _text(value, 160)})
    power = _dict(raw.get('power_on_time'))
    scsi_log = _dict(raw.get('scsi_error_counter_log'))
    return {
        'attributes': attributes,
        'nvme_rows': nvme_rows,
        'power_on_hours': _number(power.get('hours'), 0),
        'power_cycle_count': _number(raw.get('power_cycle_count'), 0),
        'rotation_rate': _number(raw.get('rotation_rate'), 0),
        'grown_defects': _number(raw.get('scsi_grown_defect_list'), 0),
        'scsi_pending_defects': _number(raw.get('scsi_pending_defects'), 0),
        'scsi_uncorrected': _number(_dict(scsi_log.get('read')).get('total_uncorrected_errors'), 0),
        'scsi_ecc': _number(_dict(scsi_log.get('read')).get('total_corrected_errors'), 0),
        'scsi_rereads': _number(_dict(scsi_log.get('read')).get('total_rereads'), 0),
        'scsi': bool(scsi_log),
        'manufactured': _text(raw.get('manufactured'), 80),
        'raw': raw,
    }


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
                 proc_root='/proc', sys_root='/sys', dev_root='/dev', udev_root='/run/udev',
                 weather_fetcher=None):
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
        self.dev_root = Path(dev_root)
        self.udev_root = Path(udev_root)
        self._mount_cache = (0.0, {})
        self._mount_devices = {}
        self._label_cache = (0.0, {})
        self._weather_fetcher = weather_fetcher or fetch_weather
        self._weather_cache = None
        self._weather_at = 0.0
        self._weather_key = None
        self._temperature_read_clock = None
        self._temperature_clock_lock = threading.Lock()

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

    def _volume_labels(self):
        """Read filesystem labels from udev links without opening a disk.

        ``/dev/disk/by-label`` is populated from metadata already seen by udev;
        resolving these symlinks does not issue a SMART command or wake a
        sleeping disk.  Missing links simply mean the label is unknown.
        """
        now = self.clock()
        stamp, cached = self._label_cache
        if now - stamp < 30:
            return cached
        labels = {}
        for folder_name, priority in (('disk/by-label', 2), ('disk/by-partlabel', 1)):
            folder = self.dev_root / folder_name
            try:
                entries = sorted(folder.iterdir(), key=lambda path: path.name)[:256]
            except OSError:
                continue
            for path in entries:
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                match = re.fullmatch(r'(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)(?:p?\d+)?', target.name)
                if not match:
                    continue
                label = _text(path.name, 120)
                disk = match.group(1)
                if label and (disk not in labels or priority > labels[disk][0]):
                    labels[disk] = (priority, label)
        # The by-label links may be absent for a sleeping disk because udev
        # has not scanned it since boot.  The udev database is metadata only;
        # reading it does not open the block device or wake the disk.
        for folder in sorted((self.sys_root / 'block').glob('*'))[:128]:
            name = folder.name
            if name in labels or not re.fullmatch(r'(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)', name):
                continue
            # A filesystem label normally lives on a partition, not on the
            # whole-disk device.  Check the disk and its child partitions in
            # the udev database; these are metadata reads only.
            blocks = [folder] + sorted(folder.glob(name + '*'))[:64]
            for block in blocks:
                try:
                    dev_id = (block / 'dev').read_text(encoding='ascii').strip()
                    lines = (self.udev_root / 'data' / ('b' + dev_id)).read_text(
                        encoding='utf-8', errors='replace').splitlines()[:512]
                except OSError:
                    continue
                value = None
                for line in lines:
                    if line.startswith('E:ID_FS_LABEL=') or line.startswith('E:ID_PART_ENTRY_NAME='):
                        value = line.split('=', 1)[1].strip()
                        if value:
                            break
                label = _text(value, 120)
                if label:
                    labels[name] = (0, label)
                    break
        result = {name: value[1] for name, value in labels.items()}
        self._label_cache = (now, result)
        return result

    def _volume_name(self, disk_mounts, basic):
        """Pick the largest /drives mount as the disk's user-visible volume."""
        candidates = [mount for mount in disk_mounts if mount.startswith('/drives/')]
        if not candidates:
            return None
        def weight(mount):
            entry = _dict(basic.get(mount))
            return _number(entry.get('total_bytes'), 0) or 0
        primary = max(candidates, key=weight)
        return _text(Path(primary).name, 120)

    # ------------------------------------------------------------------
    # mounts (read-only, cached): /dev/sda1 -> ["/drives/data"]
    # ------------------------------------------------------------------
    def _mounts(self):
        now = self.clock()
        stamp, cached = self._mount_cache
        if now - stamp < 30:
            return cached
        result, devices = {}, {}
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
            # NVMe uses a pN partition suffix; treating it like SATA's numeric
            # suffix loses mounted SSD roots from the authorization catalog.
            match = (re.fullmatch(r'/dev/(sd[a-z]+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)\d*', source)
                     or re.fullmatch(r'/dev/(nvme\d+n\d+)(?:p\d+)?', source))
            if not match or extra[0] in ('tmpfs', 'devtmpfs', 'overlay', 'squashfs', 'proc', 'sysfs'):
                continue
            result.setdefault(match[1], []).append(mount)
            if re.fullmatch(r'[0-9]+:[0-9]+', fields[2]):
                devices[mount] = fields[2]
        self._mount_cache = (now, result)
        self._mount_devices = devices
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
            'total': total, 'used': used, 'available': available,
            'free': _number(memory.get('free_bytes'), 0),
            'percent': percent, 'cached': None, 'buffers': None,
            'breakdown_available': False,
            'breakdown_reason': '未采集应用／缓存／缓冲／MemFree 构成，不由可用内存倒算。',
            'swap_total': swap_total, 'swap_used': swap_used,
            'swap_free': swap_total - swap_used if swap_total is not None and swap_used is not None else None,
            'swap_percent': _number(memory.get('swap_usage_percent'), 0, 100),
        }

    def _disks(self, *, include_identity=False):
        insight, device = self._insights(), self._device()
        storage = _dict(device.get('storage'))
        inventory = {row.get('name'): row for row in _list(storage.get('devices'), 128)
                     if isinstance(row, dict) and isinstance(row.get('name'), str)}
        mounts = self._mounts()
        basic = {entry.get('mount'): entry for entry in _list(self._basic().get('disks'), 128)
                 if isinstance(entry, dict)}
        identities = {}
        if include_identity:
            getter = getattr(self.insights, 'storage_identity_cached', None)
            if callable(getter):
                try:
                    evidence = _dict(getter())
                    identities = {row['id']: row for row in _list(evidence.get('items'), 128)
                                  if isinstance(row, dict) and isinstance(row.get('id'), str)}
                except Exception:
                    pass
        rows = []
        for item in _list(_dict(insight.get('disks')).get('items'), 128):
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            # The R1 storage view lists physical HDD/SSD/NVMe devices, even
            # when the background SMART probe is unavailable.  SMART-less disks
            # are still real hardware and must not disappear from the inventory;
            # their health/temperature fields remain unknown (never zero).
            # eMMC is boot storage, not a user data disk.
            if item.get('type') == 'eMMC':
                continue
            name = item['id']
            physical = _dict(inventory.get(name))
            identity = _dict(identities.get(name))
            # No sysfs fallback: missing/conflicting cache evidence remains null.
            serial = (_serial(identity.get('serial'))
                      if identity.get('serial_status') == 'ok' and identity.get('matched') is True else None)
            serial_status = identity.get('serial_status', 'missing')
            if serial_status == 'ok' and serial is None:
                serial_status = 'invalid'
            health = _dict(item.get('health'))
            passed = health.get('passed')
            disk_mounts = mounts.get(name, [])
            volume = self._volume_name(disk_mounts, basic) or _text(item.get('volume'), 120)
            volume_label = self._volume_labels().get(name) or _text(item.get('volume_label'), 120)
            total = used = 0
            measured = False
            measured_devices = set()
            capacity_entries = []
            for mount in disk_mounts:
                entry = _dict(basic.get(mount))
                filesystem = self._mount_devices.get(mount)
                if filesystem is not None and filesystem in measured_devices:
                    continue
                capacity_entries.append(entry)
                if _number(entry.get('total_bytes')) is not None and _number(entry.get('used_bytes')) is not None:
                    total += entry['total_bytes']
                    used += entry['used_bytes']
                    measured = True
                    if filesystem is not None:
                        measured_devices.add(filesystem)
            # This is a dated manual measurement, independent of the fast
            # overview timestamp. Multiple partitions retain the oldest sample
            # and explicitly mark incomplete coverage rather than imply live IO.
            capacity_stamps = [_number(entry.get('capacity_sampled_at'), 0)
                               for entry in capacity_entries]
            capacity_stamps = [value for value in capacity_stamps if value is not None and value > 0]
            capacity_complete = (bool(capacity_entries) and all(
                entry.get('capacity_status') == 'cached'
                and (_number(entry.get('capacity_sampled_at'), 0) or 0) > 0
                and _number(entry.get('total_bytes'), 1) is not None
                and _number(entry.get('used_bytes'), 0) is not None
                for entry in capacity_entries))
            capacity_status = ('cached' if measured and capacity_complete else 'partial' if measured
                               else 'identity_changed' if any(entry.get('capacity_status') == 'identity_changed'
                                                              for entry in capacity_entries)
                               else 'unavailable' if any(entry.get('capacity_status') == 'unavailable'
                                                        for entry in capacity_entries) else 'unknown')
            size = _number(item.get('size_bytes'), 0) or _number(physical.get('bytes'), 0)
            rpm = physical.get('rotational')
            media = ('HDD' if rpm == 1 else 'SSD' if rpm == 0 else '')
            rows.append({
                'name': name, 'device': item.get('device') or '/dev/' + name,
                'user_name': _text(item.get('user_name'), 128),
                'space_names': [_text(value, 128) for value in _list(item.get('space_names'), 256)
                                if isinstance(value, str) and value.strip()],
                'name_source': _text(item.get('name_source'), 64),
                'model': _text(item.get('model') or physical.get('model'), 120),
                'vendor': _text(physical.get('vendor'), 80),
                'serial': serial,
                **({'serial_status': serial_status,
                    'serial_sources': _list(identity.get('serial_sources'), 16) if serial else []}
                   if include_identity else {}),
                'size': size, 'total': total if measured else None,
                'used': used if measured else None,
                'percent': used / total * 100 if measured and total > 0 else None,
                'capacity_sampled_at': min(capacity_stamps) if capacity_stamps else None,
                'capacity_source': 'manual_statvfs_cache', 'capacity_status': capacity_status,
                'mounts': disk_mounts, 'volume': volume, 'volume_label': volume_label, 'slot': None,
                'rpm': media, 'kind': 'nvme' if name.startswith('nvme') else media.lower(),
                'temp': _number(item.get('temp_c'), -40, 200),
                'health': 'PASS' if passed is True else 'FAIL' if passed is False else None,
                'health_reason': _text(health.get('reason'), 160),
                'smart_available': bool(item.get('smart_available')),
                'standby': item.get('standby') if type(item.get('standby')) is bool else None,
                'smart_status': item.get('smart_status'),
                'smart_sampled_at': _number(item.get('smart_sampled_at'), 0),
                'smart_fresh': item.get('smart_fresh') is True,
                'temperature_critical_c': _number(item.get('temperature_critical_c'), 0, 200),
            })
        return rows

    def _disk_summary(self):
        """Count each confirmed block filesystem once, excluding FUSE aliases.

        The sampler deduplicates mount major/minor values, but the factory's
        public/private FUSE views have distinct values for the same backing
        storage. Match its cached usage only to actual block-backed mounts;
        never guess aliases from matching sizes or names and never stat a disk.
        """
        total = used = 0
        measured = False
        mounts = {mount for rows in self._mounts().values() for mount in rows}
        if not mounts:
            return None, None, None
        seen = set()
        for entry in _list(self._basic().get('disks'), 128):
            if not isinstance(entry, dict):
                continue
            mount = entry.get('mount')
            if mount not in mounts:
                continue
            identity = self._mount_devices.get(mount) or mount
            if not isinstance(identity, str) or identity in seen:
                continue
            size = _number(entry.get('total_bytes'), 1)
            taken = _number(entry.get('used_bytes'), 0)
            if size is not None and taken is not None and taken <= size:
                total += size
                used += taken
                measured = True
                seen.add(identity)
        if not measured or total <= 0:
            return None, None, None
        return total, used, used / total * 100

    def _net(self):
        insight, device = self._insights(), self._device()
        network = device.get('network')
        # DeviceInfo uses a list; retain the older embedded mapping shape.
        network = network if isinstance(network, list) else _dict(network).get('interfaces')
        links = {row.get('name'): row for row in _list(network, 128)
                 if isinstance(row, dict) and isinstance(row.get('name'), str)}
        rows = []
        for item in _list(insight.get('interfaces'), 128):
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                continue
            name = item['name']
            # sysfs exposes this bonding control file beside interface dirs.
            if name == 'bonding_masters':
                continue
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

    def _network_aggregate(self, snapshot=None):
        """Cached accounting totals, separate from the real interface inventory."""
        monitoring = self._monitoring() if snapshot is None else snapshot
        current = _dict(monitoring.get('current'))
        if not isinstance(current.get('metrics'), list):
            return None
        result = {'schema_version': 1, 'source': 'monitoring_filtered_counters',
                  'rx_rate': None, 'tx_rate': None,
                  'sampled_at': _number(current.get('sampled_at'), 0),
                  'exclude_virtual': _dict(monitoring.get('settings')).get('traffic_exclude_virtual')}
        for identifier, field in (('rx_bytes_per_sec', 'rx_rate'), ('tx_bytes_per_sec', 'tx_rate')):
            values = [row.get('value') for row in _list(current['metrics'], 512)
                      if isinstance(row, dict) and row.get('id') == identifier]
            result[field] = _number(values[0], 0) if len(values) == 1 else None
        if type(result['exclude_virtual']) is not bool:
            result['exclude_virtual'] = None
        return result

    def _temperature_cache(self):
        """Take one memory snapshot and atomically advance the display clock."""
        current = _dict(self._monitoring().get('current'))
        with self._temperature_clock_lock:
            now = _number(self.clock(), 0)
            previous = self._temperature_read_clock
            regressed = now is None or previous is not None and now < previous
            if now is not None:
                self._temperature_read_clock = max(now, previous) if previous is not None else now
        return current, now, regressed

    @staticmethod
    def _temperature_observation(row, now, evaluated, regressed):
        """Both temperature cards and sensor rows obey the original lifetime."""
        sampled = _number(row.get('sampled_monotonic'), 0)
        maximum = _number(row.get('max_age_seconds'), 0, 120)
        sampled_wall = _number(row.get('sampled_at'), 0)
        value = _number(row.get('value'), -40, 150)
        reason = row.get('reason')
        reasons = {'missing_sample', 'unavailable', 'invalid_sample', 'future_sample',
                   'stale_sample', 'clock_regressed', 'display_role_unverified'}
        if regressed:
            reason = 'clock_regressed'
        elif evaluated is not None and evaluated > now:
            reason = 'future_sample'
        elif reason is not None:
            reason = reason if isinstance(reason, str) and reason in reasons else 'invalid_sample'
        elif sampled is None or maximum is None or sampled_wall is None:
            reason = 'missing_sample'
        elif sampled > now:
            reason = 'future_sample'
        elif now - sampled > maximum:
            reason = 'stale_sample'
        elif value is None:
            reason = 'unavailable'
        sample = {'sampled_at': sampled_wall, 'sampled_monotonic': sampled,
                  'max_age_seconds': maximum, 'fresh': reason is None, 'reason': reason}
        return value if reason is None else None, sample

    def _sensors(self):
        device = self._device()
        temps, fans = [], []
        if self.monitoring is not None:
            current, now, regressed = self._temperature_cache()
            evaluated = _number(current.get('sampled_monotonic'), 0)
            rows = [row for row in _list(current.get('metrics'), 512) if isinstance(row, dict)
                    and isinstance(row.get('id'), str) and row['id'].startswith('temperature.')]
            for row in rows:
                role = row.get('display_role')
                if role == 'disk' or row.get('display_source') == 'smart':
                    continue  # Disk display remains the existing dated disk projection.
                value, sample = self._temperature_observation(row, now, evaluated, regressed)
                if sum(other.get('id') == row['id'] for other in rows) != 1:
                    value, sample = None, {**sample, 'fresh': False, 'reason': 'invalid_sample'}
                driver = _text(row.get('display_driver'), 80) or ''
                label = _text(row.get('display_label'), 80) or _text(row.get('label'), 120) or '温度'
                evidence = _text(row.get('display_evidence'), 40)
                accepted = {'cpu': ('cpu_driver', 'cpu_label'), 'board': ('board_label',),
                            'unknown': ('unclassified', 'invalid_metadata', 'display_role_unverified')}
                if not isinstance(role, str) or evidence not in accepted.get(role, ()):
                    role, evidence = 'unknown', 'invalid_metadata'
                temps.append({'name': label + ' · ' + driver if driver else label,
                              'label': label, 'value': value, 'temp': value,
                              'max': None, 'crit': _number(row.get('critical_celsius'), 40, 150),
                              'source': _text(row.get('display_source'), 16), 'driver': driver,
                              'channel': _text(row.get('display_channel'), 40),
                              'type': role if role in ('cpu', 'board') else 'other',
                              'display_role': role, 'display_evidence': evidence, **sample})
        else:
            sampled = _stamp(device.get('sampled_at'))
            sample_age = self.wall_clock() - sampled if sampled is not None else None
            sensor_fresh = sample_age is not None and 0 <= sample_age <= 3
            for row in _list(device.get('thermal'), 256):
                if not isinstance(row, dict):
                    continue
                celsius = _number(row.get('celsius'), -100, 250)
                if celsius is None:
                    continue
                label = _text(row.get('label'), 80) or _text(row.get('channel'), 40) or '温度'
                driver = str(row.get('driver') or '')
                try:
                    from device_info import temperature_display_role
                    role, evidence = temperature_display_role(driver, label, row.get('source', 'hwmon'))
                except ImportError:
                    role, evidence = 'unknown', 'display_role_unverified'
                temps.append({'name': label + ' · ' + driver if driver else label, 'label': label,
                              'value': celsius if sensor_fresh else None, 'temp': celsius if sensor_fresh else None,
                              'max': _number(row.get('max_celsius'), -100, 250),
                              'crit': _number(row.get('critical_celsius'), -100, 250),
                              'source': row.get('source'), 'driver': driver, 'channel': row.get('channel'),
                              'type': role if role in ('cpu', 'board', 'disk') else 'other',
                              'display_role': role, 'display_evidence': evidence,
                              'sampled_at': sampled, 'max_age_seconds': 3, 'fresh': sensor_fresh,
                              'reason': None if sensor_fresh else 'missing_sample' if sampled is None
                              else 'future_sample' if sample_age < 0 else 'stale_sample'})
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
            if any(row.get('rpm') is not None and (row.get('channel') == channel
                   or row.get('name') == name) for row in fans):
                continue
            fans.append({'name': name, 'label': name, 'channel': channel,
                         'rpm': state['rpm'], 'source': 'fan-policy'})
        return {'temps': temps, 'fans': fans, 'volts': [],
                'available': bool(temps or fans),
                'fans_source': 'hwmon / fan-policy' if fans else ''}

    def _power(self):
        monitoring = self._monitoring()
        power = monitoring.get('power')
        if not isinstance(power, dict):
            power = _dict(monitoring.get('current')).get('power')
        power = _dict(power)
        package = _number(power.get('cpu_package_w'), 0, 2000)
        ok = bool(power.get('available')) and package is not None
        return {'ok': ok, 'total': package if ok else None,
                'source': 'rapl' if ok else 'unavailable',
                'package': package, 'core': None, 'uncore': None, 'dram': None, 'base': None,
                'disks_w': None, 'disks_active': None,
                'reason': None if ok else _text(power.get('reason'), 160) or '未取得 RAPL 实测功耗；不估算整机功耗'}

    def _temperature(self, disks):
        samples, values = {}, {}
        if self.monitoring is not None:
            current, now, regressed = self._temperature_cache()
            metrics = _list(current.get('metrics'), 512)
            evaluated = _number(current.get('sampled_monotonic'), 0)
            for role, identity in (('cpu', 'cpu_temperature_c'), ('board', 'board_temperature_c')):
                matches = [row for row in metrics if isinstance(row, dict) and row.get('id') == identity]
                row = matches[0] if len(matches) == 1 else {
                    'reason': 'missing_sample' if not matches else 'invalid_sample'}
                values[role], samples[role] = self._temperature_observation(row, now, evaluated, regressed)
            source = 'monitoring_temperature_cache'
        else:
            # Old embedding callers have no control-source catalog. Only a
            # genuinely dated, still-fresh cache can supply a display value.
            basic, sensors = self._basic(), self._sensors()
            sampled = _stamp(basic.get('sampled_at') or basic.get('timestamp'))
            value = _number(_dict(basic.get('cpu')).get('temp_c'), -40, 150)
            now = self.wall_clock()
            for role in ('cpu', 'board'):
                candidates = [row for row in sensors['temps']
                              if row.get('display_role') == role and row.get('fresh') is True
                              and _number(row.get('value'), -40, 150) is not None]
                if role == 'cpu' and value is not None and sampled is not None and 0 <= now - sampled <= 3:
                    selected = {'value': value, 'sampled_at': sampled, 'fresh': True, 'reason': None}
                else:
                    selected = max(candidates, key=lambda row: row['value'], default={})
                if not selected:
                    old = [row for row in sensors['temps'] if row.get('display_role') == role]
                    selected = dict(old[0]) if old else {}
                    if selected:
                        selected.update(value=None, fresh=False,
                                        reason=selected.get('reason') or 'invalid_sample')
                    if role == 'cpu' and sampled is not None:
                        selected = {'value': None, 'sampled_at': sampled, 'fresh': False,
                                    'reason': 'future_sample' if sampled > now else
                                    'stale_sample' if now - sampled > 3 else 'unavailable'}
                values[role] = selected.get('value')
                samples[role] = {'sampled_at': selected.get('sampled_at'), 'sampled_monotonic': None,
                                 'max_age_seconds': 3, 'fresh': selected.get('fresh') is True,
                                 'reason': selected.get('reason') if selected else 'missing_sample'}
            source = 'legacy_temperature_cache'
        disk_temps = [{'name': disk['name'], 'temp': disk['temp']} for disk in disks
                      if _number(disk.get('temp'), -40, 200) is not None]
        return {'cpu': values['cpu'], 'system': values['board'], 'mb': values['board'],
                'disks': disk_temps, 'samples': samples, 'source': source}

    def _disk_io(self):
        rows = []
        monitoring = self._monitoring()
        # MonitoringService.snapshot() nests the live sample under ``current``;
        # older/embedding callers may still provide the flat compatibility shape.
        source = monitoring.get('disk_io')
        if not isinstance(source, list):
            source = _dict(monitoring.get('current')).get('disk_io')
        for row in _list(source, 128):
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
        pci_rows = [row for row in _list(self._identity().get('pci'), 256) if isinstance(row, dict)]
        for item in _list(_dict(self._insights().get('gpu')).get('devices'), 32):
            if not isinstance(item, dict):
                continue
            vendor = str(item.get('vendor') or '').lower().replace('0x', '')
            device_id = str(item.get('device') or '').lower().replace('0x', '')
            matches = [row for row in pci_rows
                       if str(row.get('class', '')).lower().startswith('0x03')
                       and str(row.get('vendor') or '').lower().replace('0x', '') == vendor
                       and str(row.get('device') or '').lower().replace('0x', '') == device_id]
            # sysfs DRM and PCI inventory are cached independently. Only an
            # unambiguous vendor/device match supplies missing display names.
            pci = matches[0] if vendor and device_id and len(matches) == 1 else {}
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
                'pci': _text(item.get('pci') or pci.get('address'), 64),
                'driver': _text(item.get('driver') or pci.get('driver'), 64),
                'model': _text(item.get('model') or pci.get('device_name'), 120),
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
            'gpu': [row.get('model') or row.get('name') for row in self._gpu()],
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
            'device_name': _text(identity.get('name'), 128),
            'device_name_source': _text(identity.get('name_source'), 64),
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
    def overview(self, *, include_identity=False):
        """Shared overview; only an authorized owner Web call opts into identity."""
        disks = self._disks(include_identity=include_identity)
        sensors = self._sensors()
        total_size, used, percent = self._disk_summary()
        fan = self._fan()
        light = self._light()
        return {
            'cpu': self._cpu(),
            'mem': self._mem(),
            # R1 exposes physical SMART disks and aggregate usage only; do
            # not feed fnMonitor's partition table with mount rows.
            'disks': [],
            'disk_total': percent,
            'disk_used': used,
            'disk_size': total_size,
            'disk_pools': 0,
            'disks_detail': disks,
            'idrac_slot_state': 'off',
            'net': self._net(),
            'net_aggregate': self._network_aggregate(),
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
                             'base': None, 'disks_w': None, 'disks_active': None},
                'energy': {'today_kwh': None, 'month_kwh': None, 'boot_kwh': None, 'basis': 'rapl_only'},
                'cost': {'today_yuan': None, 'month_yuan': None,
                         'rate_yuan_per_kwh': self.store.config().get('power_rate_yuan')},
                'disks': [], 'standby_protect': True,
                'estimation_params': None,
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
        aggregate = self._network_aggregate(snapshot)
        # The shared sampler already applies the chosen bridge/bond/VLAN
        # accounting policy. Do not add its logical interfaces a second time.
        selected = [row for row in net if not (settings.get('traffic_exclude_virtual') and
                    re.match(r'^(?:lo$|docker|veth|br-|br[0-9]+$|virbr|bond[0-9]+$|team[0-9]+$|tun|tap|vnet|lxc|kube|wg|vxlan)', row['name']))]
        def rate(field):
            if aggregate is not None:
                return aggregate[field]
            values = [row.get(field) for row in selected]
            return sum(values) if values and all(value is not None for value in values) else None
        return {
            'realtime': {'rx_rate': rate('rx_rate'), 'tx_rate': rate('tx_rate')},
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

    def smart(self, device, *, include_identity=False):
        """Dated manual-cache detail; opening this page never runs smartctl."""
        if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', device):
            raise ValueError('设备标识无效')
        for disk in self._disks(include_identity=include_identity):
            if disk['name'] == device:
                cached = None
                getter = getattr(self.insights, 'smart_cached', None)
                if callable(getter):
                    try:
                        cached = getter(device)
                    except Exception:
                        cached = None
                raw = _dict(_dict(cached).get('smart_data'))
                observations = _smart_observations(raw)
                if include_identity and disk.get('serial'):
                    observations['serial_number'] = disk['serial']
                detail = _smart_rows(observations)
                return {'ok': True, 'dev': device, 'model': disk.get('model'),
                        'serial': disk.get('serial'), 'size': disk.get('size'),
                        'temp': disk.get('temp'), 'health': disk.get('health'),
                        'standby': disk.get('standby'),
                        'reason': disk.get('health_reason'),
                        'source': 'manual_smartctl_cache',
                        'sampled_at': _number(_dict(cached).get('smart_sampled_at'), 0),
                        'fresh': _dict(cached).get('smart_fresh') is True,
                        'attributes': detail['attributes'],
                        'nvme_rows': detail['nvme_rows'],
                        'power_on_hours': detail['power_on_hours'],
                        'power_cycle_count': detail['power_cycle_count'],
                        'rotation_rate': detail['rotation_rate'],
                        'grown_defects': detail['grown_defects'],
                        'scsi_pending_defects': detail['scsi_pending_defects'],
                        'scsi_uncorrected': detail['scsi_uncorrected'],
                        'scsi_ecc': detail['scsi_ecc'],
                        'scsi_rereads': detail['scsi_rereads'],
                        'scsi': detail['scsi'],
                        'manufactured': detail['manufactured'],
                        'raw': detail['raw'],
                        'note': '页面仅显示有日期的手动 SMART 缓存；过期温度与健康为未知，打开详情不会查询硬盘'}
        return {'ok': False, 'dev': device, 'error': '未找到该硬盘的 SMART 观测'}

    def disk_health(self):
        """Grades from fresh explicit SMART cache; no background SMART activity."""
        items, summary = {}, {'ok': 0, 'warn': 0, 'crit': 0, 'unknown': 0}
        stamp = None
        for disk in self._disks():
            grade, detail = 'unknown', ''
            if disk.get('standby'):
                grade, detail = 'unknown', '上次查询因休眠跳过，健康未知'
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
            sampled = _number(disk.get('smart_sampled_at'), 0)
            if sampled is not None:
                stamp = max(stamp or 0, sampled)
        return {'ok': True, 'enabled': True, 'interval_h': 0, 'presets': [],
                'last_ts': stamp, 'next_ts': None, 'scanning': False,
                'summary': summary, 'error': '', 'items': items, 'now': self.wall_clock(),
                'source': 'manual_smart_cache'}

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
        """Cached current weather; no hardware sampling and no API key.

        Failures are cached briefly so a dead provider cannot stall every
        panel refresh.  Successful responses live for 15 minutes.
        """
        city = _weather_place(self.store.config().get('weather_city'))
        now = self.clock()
        age = now - self._weather_at
        ttl = 900 if self._weather_cache and self._weather_cache.get('ok') else 60
        if self._weather_cache is not None and self._weather_key == city and 0 <= age < ttl:
            return copy.deepcopy(self._weather_cache)
        try:
            result = self._weather_fetcher(city)
        except Exception as exc:
            result = {'ok': False, 'error': '天气服务暂时不可用：%s' % str(exc)[:120]}
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            result = {'ok': False, 'error': '天气服务返回了无效数据'}
        self._weather_cache = copy.deepcopy(result)
        self._weather_at = now
        self._weather_key = city
        return result

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
        if 'weather_city' in changes:
            self._weather_cache = None
            self._weather_key = None
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
        if 'weather_city' in changes:
            notes.append('天气位置已保存，下一次天气刷新生效')
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
                'auth_mode': getattr(self.web, 'auth_mode', 'pair') if self.web is not None else 'pair',
            },
            'web': {'port': getattr(self.web, 'port', None), 'host': getattr(self.web, 'host', None),
                    'auth_mode': getattr(self.web, 'auth_mode', 'pair') if self.web is not None else 'pair',
                    'addresses': self.web._addresses() if self.web is not None else []},
            'fan': fan,
            'light': light,
            'control': {'capabilities': capabilities,
                        'screen': {key: _dict(control.get('screen')).get(key) for key in ('brightness', 'asleep', 'saver')},
                        'rgb': {key: _dict(control.get('rgb')).get(key) for key in ('mode', 'color', 'brightness')},
                        'fans': _dict(control.get('fans')),
                        'power': _dict(control.get('power')),
                        'settings': settings},
            'monitoring': {'settings': monitoring_settings,
                           'available': bool(_dict(self._monitoring()).get('available'))},
            'device': {'name': self._system().get('device_name'),
                       'name_source': self._system().get('device_name_source'),
                       'hostname': self._system().get('hostname'),
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
