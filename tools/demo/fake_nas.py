"""A stateful fake HikSemi R1 for local panel development.

The demo runs the real web server, the real small-screen UI and the real
fnMonitor adapter against these snapshots.  Nothing touches the host: fans,
SMART, Docker, power and the screen are simulated in memory.

The small screen and the web panel share one :class:`FakeNas`, so changing the
brightness on the small screen is visible in the web panel; web and small-screen
fan/light quick changes share the same simulated policy.
"""

from collections import deque
import colorsys
import copy
import datetime
import math
import threading
import time

from fan_policy import validate_config

PAGES = ['overview', 'storage', 'network', 'controls', 'apps', 'system', 'settings']

FAN_DEFAULT = {'schema_version': 2, 'channels': {
    'cpu': {'mode': 'vendor', 'sources': [], 'low_temp': 60, 'high_temp': 75,
            'quiet_percent': 55, 'normal_percent': 75, 'manual_percent': 70},
    'system': {'mode': 'vendor', 'sources': [], 'low_temp': 50, 'high_temp': 60,
               'quiet_percent': 50, 'normal_percent': 70, 'manual_percent': 65},
}}

LIGHT_DEFAULT = {'schema_version': 1, 'enabled': False, 'mode': 'manual', 'sources': [],
                 'stops': [{'temperature': 35, 'color': '#00C832'},
                           {'temperature': 55, 'color': '#FFAA00'},
                           {'temperature': 75, 'color': '#FF3030'}],
                 'brightness': 65, 'manual': {'mode': 1, 'color': '#7CAF98', 'brightness': 60},
                 'schedule': {'enabled': False, 'start': '22:00', 'end': '07:00', 'brightness': 12}}


def _iso(seconds=None):
    moment = datetime.datetime.now(datetime.timezone.utc) if seconds is None else \
        datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.isoformat(timespec='seconds')


class FakeNas:
    def __init__(self, hostname='R1-DEMO'):
        self.hostname = hostname
        self.lock = threading.RLock()
        self.boot_wall = time.time() - 3 * 86400 - 4 * 3600
        self.launched = time.monotonic()
        self.settings = {'theme': 'dark', 'font_scale': 100, 'page_interval': 0, 'sleep_timeout': 0,
                         'brightness': 80, 'night_enabled': False, 'night_start': '22:00',
                         'night_end': '07:00', 'night_brightness': 18, 'enabled_pages': list(PAGES)}
        self.brightness = 80
        self.asleep = False
        self.rgb = {'mode': 1, 'color': '#7CAF98', 'brightness': 60}
        self.official = {'available': True, 'control_available': True,
                         'cpu_rpm': 1350, 'system_rpm': 1080, 'scope': 'system_fan',
                         'cpu_policy': 'vendor_auto', 'mode': 'auto',
                         'reason': '演示模式：原厂档位仅在内存中模拟'}
        self.fan_config = copy.deepcopy(FAN_DEFAULT)
        self.fan_active = False
        self.light_config = copy.deepcopy(LIGHT_DEFAULT)
        self.light_active = False
        self.power = {'pending': None, 'executing': False, 'last_error': None,
                      'deadline': None, 'done_at': None, 'operation': None}
        self.containers = [
            {'id': 'a' * 64, 'name': 'demo-photos', 'image': 'ghcr.io/example/photos:demo', 'state': 'running',
             'status': 'Up 2 hours', 'cpu_percent': 3.4, 'memory_bytes': 268435456, 'memory_percent': 3.3,
             'ports': '0.0.0.0:8088->80/tcp', 'protected': False, 'available_actions': ['stop', 'restart']},
            {'id': 'b' * 64, 'name': 'demo-db', 'image': 'postgres:17', 'state': 'running',
             'status': 'Up 5 hours', 'cpu_percent': 1.2, 'memory_bytes': 134217728, 'memory_percent': 1.6,
             'ports': '127.0.0.1:5432->5432/tcp', 'protected': False, 'available_actions': ['stop', 'restart']},
            {'id': 'c' * 64, 'name': 'r1-toolbox', 'image': 'ghcr.io/shinku-chen/r1-toolbox:preview',
             'state': 'exited', 'status': 'Exited (0) 3 hours ago', 'cpu_percent': None, 'memory_bytes': None,
             'memory_percent': None, 'ports': '', 'protected': True, 'available_actions': []},
        ]
        self.vms = [
            {'id': '7d2f0c9a-1b2c-4d5e-8f90-123456789abc', 'name': 'demo-vm', 'state': 'running'},
        ]
        self.cpu, self.mem = 18.0, 42.0
        self.cpu_temp, self.board_temp = 58.0, 44.0
        self.disk_a_temp, self.disk_b_temp = 38.0, 36.0
        self.rx_rate, self.tx_rate = 1.25e6, 3.4e5
        self.today = [128 * 1024 ** 3, 9 * 1024 ** 3]
        self.month = [2.1 * 1024 ** 4, 180 * 1024 ** 3]
        self.history = deque(maxlen=240)
        self.tick_count = 0
        self.tick_seconds = 1.5
        self.monitor_settings = {'history_enabled': True, 'retention_days': 14, 'traffic_exclude_virtual': True,
                                 'thresholds': {'cpu_warn': 85, 'cpu_critical': 95, 'memory_warn': 85,
                                                'memory_critical': 95, 'cpu_temp_warn': 75, 'cpu_temp_critical': 85,
                                                'disk_temp_warn': 50, 'disk_temp_critical': 58,
                                                'capacity_warn': 85, 'capacity_critical': 95}}
        self.stopped = threading.Event()
        self.thread = None
        self._sample()

    # ------------------------------------------------------------ loop
    def start(self):
        if self.thread:
            return self
        self.thread = threading.Thread(target=self._loop, name='r1-demo-nas', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _loop(self):
        while not self.stopped.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        with self.lock:
            self._sample()
            self._tick_power()
            self._tick_fan()
            self._tick_light()

    def _sample(self):
        self.tick_count += 1
        phase = self.tick_count / 9.0
        self.cpu = max(3.0, min(96.0, 24 + 16 * math.sin(phase) + 6 * math.sin(phase * 2.7)))
        self.mem = max(20.0, min(85.0, 42 + 5 * math.sin(phase / 2)))
        self.cpu_temp = max(35.0, min(86.0, 46 + self.cpu * 0.45 + 2 * math.sin(phase / 1.3)))
        self.board_temp = 40 + self.cpu * 0.12
        self.disk_a_temp = 36 + 3 * math.sin(phase / 2.2)
        self.disk_b_temp = 35 + 2 * math.sin(phase / 3.1)
        self.rx_rate = 8.0e5 + 6.0e5 * (1 + math.sin(phase * 1.7))
        self.tx_rate = 2.0e5 + 2.2e5 * (1 + math.sin(phase * 1.1))
        elapsed = self.tick_seconds
        self.today[0] += self.rx_rate * elapsed
        self.today[1] += self.tx_rate * elapsed
        self.month[0] += self.rx_rate * elapsed
        self.month[1] += self.tx_rate * elapsed
        self.history.append({
            'ts': int(time.time()),
            'cpu': round(self.cpu, 1), 'mem': round(self.mem, 1),
            'temp': round(self.cpu_temp, 1), 'power': round(9 + self.cpu * 0.16, 1),
            'rx': self.rx_rate, 'tx': self.tx_rate,
            'dr': 3.2e6 + 2e6 * math.sin(phase), 'dw': 1.4e6 + 1e6 * math.sin(phase / 1.4),
        })

    def _tick_power(self):
        now = time.time()
        pending = self.power['pending']
        if pending and self.power['deadline'] and now >= self.power['deadline']:
            self.power['pending'] = None
            self.power['executing'] = True
            self.power['done_at'] = now + 2.0
            self.power['last_error'] = None
        elif self.power['executing'] and self.power['done_at'] and now >= self.power['done_at']:
            self.power['executing'] = False
            self.power['done_at'] = None
            self.power['last_error'] = '演示环境未执行真实电源操作'
            if self.power.get('operation') == 'reboot':
                self.boot_wall = time.time() - 12
                self.asleep = False

    # ------------------------------------------------------------ fan
    def fan_sources(self):
        rows = [
            ('cpu', 'CPU Package', 'cpu', self.cpu_temp),
            ('board', '主板温度', 'board', self.board_temp),
            ('disk-a', '硬盘 A', 'disk', self.disk_a_temp),
            ('disk-b', '硬盘 B', 'disk', self.disk_b_temp),
        ]
        return [{'id': key, 'label': label, 'kind': kind, 'temperature_c': round(value, 1),
                 'status': 'ok', 'fresh': True, 'max_age_seconds': 10, 'age_seconds': 0,
                 'sampled_monotonic': time.monotonic(), 'critical_celsius': 95 if kind == 'cpu' else None}
                for key, label, kind, value in rows]

    def _fan_channel(self, name):
        policy = self.fan_config['channels'][name]
        sources = {row['id']: row for row in self.fan_sources()}
        if policy['mode'] == 'vendor' or not self.fan_active:
            return {'mode': policy['mode'], 'active': False, 'level': 'vendor', 'reference_c': None,
                    'source_id': None, 'source_label': None, 'target_percent': None, 'rpm': None,
                    'reason': '原厂固件管理'}
        if policy['mode'] == 'manual':
            return {'mode': 'manual', 'active': True, 'level': 'manual', 'reference_c': None,
                    'source_id': None, 'source_label': '固定输出',
                    'target_percent': policy['manual_percent'], 'rpm': None,
                    'reason': '按固定输出运行'}
        selected = [sources[key] for key in policy['sources'] if key in sources]
        if not selected:
            return {'mode': 'temperature', 'active': True, 'level': 'normal', 'reference_c': None,
                    'source_id': None, 'source_label': None, 'target_percent': policy['normal_percent'],
                    'rpm': None, 'reason': '未选择有效温度设备，使用普通档'}
        best = max(selected, key=lambda row: row['temperature_c'])
        value = best['temperature_c']
        if value < policy['low_temp']:
            level, target = 'quiet', policy['quiet_percent']
        elif value <= policy['high_temp']:
            level, target = 'normal', policy['normal_percent']
        else:
            level, target = 'full', 100
        return {'mode': 'temperature', 'active': True, 'level': level, 'reference_c': value,
                'source_id': best['id'], 'source_label': best['label'], 'target_percent': target,
                'rpm': None, 'reason': '取所选设备最高温度'}

    def _tick_fan(self):
        self._fan_state = {name: self._fan_channel(name) for name in ('cpu', 'system')}

    def fan_snapshot(self):
        with self.lock:
            channels = {name: self._fan_channel(name) for name in ('cpu', 'system')}
            return {'config': copy.deepcopy(self.fan_config),
                    'active': self.fan_active,
                    'busy': False,
                    'pending_changes': self.fan_config['channels']['system']['mode'] == 'vendor'
                        and self.fan_config['channels']['system'].get('vendor_mode', self.official['mode']) != self.official['mode'],
                    'official': copy.deepcopy(self.official),
                    'capability': {'available': True, 'reason': '', 'channels': ['cpu', 'system'],
                                   'rpm_available': False},
                    'channels': channels,
                    'sources': self.fan_sources(),
                    'active_on_boot': self.fan_active,
                    'recovery_confirmed': True,
                    'last_error': None}

    def fan_action(self, name, payload):
        with self.lock:
            apply_official = name == 'fan.policy.update'
            if name == 'fan.policy.release':
                for item in self.fan_config['channels'].values():
                    item['mode'] = 'vendor'
                self.fan_config['channels']['system'].pop('vendor_mode', None)
                self.fan_active = False
                return {'ok': True, 'settings_saved': True, 'pending': False,
                        'fan_policy': self.fan_snapshot()}
            if name == 'fan.policy.quick':
                if not isinstance(payload, dict) or set(payload) != {'channel', 'mode'} or payload.get('channel') not in ('cpu', 'system') or payload.get('mode') not in ('auto', 'low', 'medium', 'high', 'manual'):
                    return {'ok': False, 'settings_saved': False, 'error': '风扇快速调节参数无效'}
                config = copy.deepcopy(self.fan_config)
                item = config['channels'][payload['channel']]
                if payload['channel'] == 'system' and payload['mode'] != 'manual':
                    item.update(mode='vendor', vendor_mode=payload['mode'])
                    apply_official = True
                elif payload['mode'] == 'auto':
                    item['mode'] = 'vendor'
                elif payload['mode'] == 'manual':
                    item['mode'] = 'temperature'
                    item.pop('vendor_mode', None)
                else:
                    item['mode'] = 'manual'
                    item['manual_percent'] = {'low': 50, 'medium': 75, 'high': 100}[payload['mode']]
                payload = {'config': config, 'apply': True}
                name = 'fan.policy.update'
            if name != 'fan.policy.update' or not isinstance(payload, dict) or 'config' not in payload:
                return {'ok': False, 'settings_saved': False, 'error': '风扇配置参数无效'}
            try:
                # Reuse production validation and strict v1→v2 migration so
                # demo saves cannot accept fields or ranges the NAS rejects.
                config = validate_config(payload['config'])
            except ValueError as error:
                return {'ok': False, 'settings_saved': False, 'error': str(error)}
            self.fan_config = copy.deepcopy(config)
            if payload.get('apply') is True:
                self.fan_active = any(item['mode'] != 'vendor' for item in config['channels'].values())
                system = config['channels']['system']
                # Only the editor's explicit apply or a native system shortcut
                # selects a simulated vendor mode; CPU shortcuts preserve it.
                if apply_official and system['mode'] == 'vendor' and 'vendor_mode' in system:
                    self.official['mode'] = system['vendor_mode']
            return {'ok': True, 'settings_saved': True, 'pending': bool(payload.get('apply')),
                    'fan_policy': self.fan_snapshot()}

    # ------------------------------------------------------------ light
    def light_sources(self):
        return self.fan_sources()

    def light_snapshot(self):
        with self.lock:
            config = self.light_config
            active = self.light_active and config.get('enabled') is True
            if config['mode'] == 'temperature' and config.get('sources'):
                rows = {row['id']: row for row in self.fan_sources()}
                chosen = [rows[key] for key in config['sources'] if key in rows]
                reference = max((row['temperature_c'] for row in chosen), default=None)
                source = max(chosen, key=lambda row: row['temperature_c']) if chosen else None
                color = config['stops'][-1]['color'] if reference and reference >= config['stops'][-1]['temperature'] else config['stops'][0]['color']
            else:
                reference, source = None, None
                color = config['manual']['color']
            target = {'mode': config['manual']['mode'] if config['mode'] == 'manual' else 1,
                      'color': color, 'brightness': config['brightness'] if config['mode'] == 'temperature' else config['manual']['brightness']}
            runtime = {'active': active, 'busy': False, 'applied': active,
                       'reason': '演示：温度联动运行中' if active and config['mode'] == 'temperature' else '演示：手动灯效',
                       'fallback': False, 'schedule_active': False,
                       'reference_c': reference, 'source_label': source['label'] if source else None,
                       'target': copy.deepcopy(target)}
            return {'config': copy.deepcopy(config), 'active_config': copy.deepcopy(config),
                    'active_on_boot': active, 'active': active, 'busy': False, 'pending_changes': False,
                    'capability': {'available': True, 'reason': ''}, 'sources': self.light_sources(),
                    'target': target, 'runtime': runtime, 'reference_c': reference,
                    'source_id': source['id'] if source else None,
                    'source_label': source['label'] if source else None,
                    'last_error': None, 'applied': active, 'fallback': False,
                    'reason': runtime['reason']}

    def light_action(self, name, payload):
        with self.lock:
            if name == 'light.policy.quick':
                if not isinstance(payload, dict) or set(payload) != {'hue', 'brightness', 'effect'}:
                    return {'ok': False, 'settings_saved': False, 'error': '灯光快速调节参数无效'}
                valid = all(type(payload[key]) is int for key in ('hue', 'brightness', 'effect'))
                if not valid or not 0 <= payload['hue'] <= 359 or not 0 <= payload['brightness'] <= 100 or not 0 <= payload['effect'] <= 3:
                    return {'ok': False, 'settings_saved': False, 'error': '灯光快速调节参数无效'}
                red, green, blue = colorsys.hsv_to_rgb(payload['hue'] / 360.0, 1.0, 1.0)
                color = '#%02X%02X%02X' % (round(red * 255), round(green * 255), round(blue * 255))
                config = copy.deepcopy(self.light_config)
                config.update(enabled=True, mode='manual', brightness=payload['brightness'])
                config['manual'] = {'mode': payload['effect'], 'color': color, 'brightness': payload['brightness']}
                payload = {'config': config, 'apply': True}
                name = 'light.policy.update'
            if name != 'light.policy.update' or not isinstance(payload, dict) or 'config' not in payload:
                return {'ok': False, 'settings_saved': False, 'error': '灯光配置参数无效'}
            config = payload['config']
            if not isinstance(config, dict) or config.get('mode') not in ('manual', 'temperature'):
                return {'ok': False, 'settings_saved': False, 'error': '灯光配置字段无效'}
            self.light_config = copy.deepcopy(config)
            if payload.get('apply') is True:
                self.light_active = config.get('enabled') is True
            return {'ok': True, 'settings_saved': True, 'pending': bool(payload.get('apply')),
                    'light_policy': self.light_snapshot()}

    def _tick_light(self):
        pass

    # ------------------------------------------------------------ controller
    def toolbox(self):
        with self.lock:
            pending = None
            if self.power['pending']:
                pending = dict(self.power['pending'])
                pending['seconds_remaining'] = max(0, math.ceil((self.power['deadline'] or 0) - time.time()))
            return {
                'settings': copy.deepcopy(self.settings),
                'capabilities': {'screen': {'available': True, 'reason': ''},
                                 'brightness': {'available': True, 'reason': ''},
                                 'fans': {'available': True, 'reason': ''},
                                 'rgb': {'available': True, 'reason': ''},
                                 'power': {'available': True, 'reason': ''}},
                'screen': {'brightness': self.brightness, 'asleep': self.asleep, 'max_brightness': 96000},
                'rgb': {'busy': None, 'mode': self.rgb['mode'], 'color': self.rgb['color'],
                        'brightness': self.rgb['brightness'], 'bus': 8},
                'fans': {'channels': [], 'official': copy.deepcopy(self.official),
                         'last_error': None},
                'power': {'pending': pending, 'executing': self.power['executing'],
                          'last_error': self.power['last_error']},
            }

    def controller_action(self, name, payload):
        with self.lock:
            if name == 'settings.update':
                changes = payload.get('settings') if isinstance(payload, dict) else None
                if not isinstance(changes, dict) or not changes:
                    return {'ok': False, 'settings_saved': False, 'error': '设置无效'}
                for key, value in changes.items():
                    if key in self.settings:
                        self.settings[key] = value
                if 'brightness' in changes and isinstance(changes['brightness'], int):
                    self.brightness = max(5, min(100, changes['brightness']))
                return {'ok': True, 'settings_saved': True, 'state': self.toolbox()}
            if name == 'screen.brightness':
                value = payload.get('value') if isinstance(payload, dict) else None
                if type(value) is not int or not 5 <= value <= 100:
                    return {'ok': False, 'error': '亮度需要 5–100%'}
                self.brightness = value
                self.settings['brightness'] = value
                return {'ok': True, 'state': self.toolbox()}
            if name in ('screen.sleep', 'screen.wake'):
                self.asleep = name == 'screen.sleep'
                return {'ok': True, 'state': self.toolbox()}
            if name == 'screen.activity':
                return {'ok': True, 'state': self.toolbox()}
            if name == 'rgb.set':
                for key in ('mode', 'color', 'brightness'):
                    if key in payload:
                        self.rgb[key] = payload[key]
                return {'ok': True, 'state': self.toolbox()}
            if name == 'rgb.probe':
                return {'ok': True, 'bus': 8, 'state': self.toolbox()}
            if name == 'power.request':
                if payload.get('confirmed') is not True:
                    return {'ok': False, 'error': '电源操作需要确认'}
                operation = payload.get('operation')
                if operation not in ('reboot', 'poweroff'):
                    return {'ok': False, 'error': '电源操作参数无效'}
                if self.power['pending'] or self.power['executing']:
                    return {'ok': False, 'error': '已有电源操作在进行'}
                self.power.update(pending={'operation': operation, 'seconds_remaining': 5},
                                  deadline=time.time() + 5, operation=operation, last_error=None)
                return {'ok': True, 'state': self.toolbox()}
            if name == 'power.cancel':
                if self.power['executing']:
                    return {'ok': False, 'error': '电源操作已开始，无法取消'}
                self.power.update(pending=None, deadline=None)
                return {'ok': True, 'state': self.toolbox()}
            if name == 'fan.official':
                self.official['mode'] = payload.get('mode', 'auto')
                return {'ok': True, 'state': self.toolbox()}
            return {'ok': False, 'error': '演示环境不支持此操作'}

    # ------------------------------------------------------------ monitor / insights
    def monitor(self):
        with self.lock:
            uptime = time.time() - self.boot_wall
            total, used = 16 * 1024 ** 3, int(16 * 1024 ** 3 * self.mem / 100)
            return {'schema_version': 1, 'available': True, 'timestamp': _iso(),
                    'hostname': self.hostname, 'uptime_seconds': round(uptime, 1),
                    'cpu': {'usage_percent': round(self.cpu, 1), 'temp_c': round(self.cpu_temp, 1)},
                    'memory': {'total_bytes': total, 'used_bytes': used,
                               'available_bytes': total - used, 'usage_percent': round(self.mem, 1)},
                    'network': {'interface': 'eth0', 'ipv4': '192.168.1.50',
                                'rx_bytes_per_sec': round(self.rx_rate, 1),
                                'tx_bytes_per_sec': round(self.tx_rate, 1)},
                    'disks': [{'mount': '/drives/data', 'total_bytes': 4 * 10 ** 12,
                               'used_bytes': 1.4 * 10 ** 12, 'usage_percent': 35.0}]}

    def insights(self):
        with self.lock:
            cores = [{'id': index, 'usage_percent': round(max(1, min(99, self.cpu + (index - 1.5) * 4)), 1)}
                     for index in range(4)]
            memory = self.monitor()['memory']
            containers = []
            for item in self.containers:
                row = copy.deepcopy(item)
                if row['state'] == 'running' and row['cpu_percent'] is not None:
                    row['cpu_percent'] = round(max(0.1, row['cpu_percent'] + math.sin(self.tick_count / 7) * 0.6), 1)
                row['available_actions'] = [] if row['protected'] else \
                    (['stop', 'restart'] if row['state'] == 'running' else ['start'])
                containers.append(row)
            return {
                'schema_version': 1, 'timestamp': _iso(),
                'system': {'model': 'HIKSEMI R1 (demo)', 'cpu_model': 'Intel N100', 'cpu_logical_cores': 4,
                           'load_average': [round(self.cpu / 25, 2), round(self.cpu / 30, 2), round(self.cpu / 40, 2)],
                           'process_count': 142, 'uptime_seconds': round(time.time() - self.boot_wall, 1),
                           'cpu_usage_percent': round(self.cpu, 1), 'cpu_cores': cores,
                           'memory': {'total_bytes': memory['total_bytes'], 'used_bytes': memory['used_bytes'],
                                      'available_bytes': memory['available_bytes'],
                                      'usage_percent': memory['usage_percent'], 'swap_total_bytes': 0,
                                      'swap_used_bytes': 0, 'swap_usage_percent': 0.0}},
                'device': {
                    'identity': {'hostname': self.hostname, 'kernel': '6.1.0-demo',
                                 'os': {'PRETTY_NAME': 'Debian GNU/Linux 12 (demo)', 'NAME': 'Debian', 'ID': 'debian'},
                                 'firmware': {'version': 'DEMO-1.0', 'build': 'demo'},
                                 'dmi': {'sys_vendor': 'HikSemi', 'product_name': 'R1 (demo)',
                                         'board_name': 'R1-DEMO', 'bios_version': 'demo'},
                                 'pci': []},
                    'cpu': {'model': 'Intel N100', 'physical_cores': 4, 'logical_cores': 4, 'sockets': 1,
                            'architecture': 'x86_64',
                            'frequencies_mhz': [{'id': index, 'current': 1200 + index * 50} for index in range(4)]},
                    'thermal': [{'id': 'coretemp:0', 'driver': 'coretemp', 'label': 'Package id 0',
                                 'celsius': round(self.cpu_temp, 1), 'critical_celsius': 100, 'source': 'hwmon'}],
                    'fans': [{'id': 'demo:fan1', 'label': 'CPU 风扇', 'rpm': 1350, 'source': 'demo'},
                             {'id': 'demo:fan2', 'label': '系统风扇', 'rpm': 1080, 'source': 'demo'}],
                    'storage': {'devices': [{'name': 'sda', 'model': 'DEMO DISK 4TB', 'vendor': 'Demo',
                                             'serial': 'DEMO-0001', 'bytes': 4 * 10 ** 12, 'rotational': 1}],
                                'raid': []},
                    'network': {'interfaces': [{'name': 'eth0', 'mac': '00:11:22:33:44:55', 'state': 'up',
                                                'carrier': 1, 'speed_mbps': 2500, 'duplex': 'full', 'mtu': 1500,
                                                'ipv6_addresses': [{'address': '2001:db8::50', 'prefix_length': 64, 'scope': 'global'}],
                                                'default_gateways': {'ipv4': [{'gateway': '192.168.1.1', 'metric': 100, 'source': '/proc/net/route'}],
                                                                     'ipv6': [{'gateway': 'fe80::1', 'metric': 200, 'source': '/proc/net/ipv6_route'}]},
                                                'route_status': {'ipv4': 'ok', 'ipv6': 'ok'}}],
                                'network_config': {'default_routes': {'ipv4': [{'interface': 'eth0', 'gateway': '192.168.1.1', 'metric': 100, 'source': '/proc/net/route'}],
                                                                      'ipv6': [{'interface': 'eth0', 'gateway': 'fe80::1', 'metric': 200, 'source': '/proc/net/ipv6_route'}]},
                                                   'dns': {'servers': ['192.168.1.1', '2001:4860:4860::8888'],
                                                           'upstream_servers': ['192.168.1.1'], 'search': ['home.arpa'],
                                                           'domains': ['home.arpa'], 'status': 'ok',
                                                           'sources': [{'source': 'resolv.conf', 'path': '/etc/resolv.conf',
                                                                        'status': 'ok', 'servers': ['192.168.1.1']}]}}},
                },
                'interfaces': [{'name': 'eth0', 'ipv4': ['192.168.1.50'], 'mac': '00:11:22:33:44:55',
                                'operstate': 'up', 'rx_bytes': int(self.today[0]), 'tx_bytes': int(self.today[1]),
                                'rx_bytes_per_sec': round(self.rx_rate, 1), 'tx_bytes_per_sec': round(self.tx_rate, 1)}],
                'disks': {'available': True, 'sampled_at': _iso(), 'reason': '',
                          'items': [{'id': 'sda', 'device': '/dev/sda', 'model': 'DEMO DISK 4TB',
                                     'size_bytes': 4 * 10 ** 12, 'temp_c': round(self.disk_a_temp, 1),
                                     'standby': False, 'smart_available': True,
                                     'health': {'passed': True, 'reason': None},
                                     'temperature_critical_c': 65}]},
                'containers': {'available': True, 'reason': '', 'sampled_at': _iso(), 'items': containers},
                'vms': {'available': True, 'reason': '', 'sampled_at': _iso(), 'items': copy.deepcopy(self.vms)},
                'gpu': {'available': True, 'reason': '', 'devices': [{'name': 'card0', 'vendor': '0x8086',
                                                                      'device': '0x46d1', 'busy_percent': 7,
                                                                      'vram_total_bytes': None, 'vram_used_bytes': None}]},
                'ports': [{'protocol': 'tcp', 'address': '0.0.0.0', 'port': 18731, 'inode': 1},
                          {'protocol': 'tcp', 'address': '0.0.0.0', 'port': 18732, 'inode': 2}],
                'processes': [{'pid': 900, 'name': 'hiksemi-sub', 'cpu_percent': 1.2, 'memory_bytes': 2e8},
                              {'pid': 901, 'name': 'demo-agent', 'cpu_percent': 0.4, 'memory_bytes': 8e7}],
                'device_pci': [],
                'history': [{'cpu_usage_percent': item['cpu'], 'memory_usage_percent': item['mem']}
                            for item in list(self.history)[-30:]],
            }

    def insights_action(self, name, payload):
        with self.lock:
            if name == 'container.action':
                identity, operation = payload.get('id'), payload.get('operation')
                item = next((row for row in self.containers if row['id'] == identity), None)
                if not item or item['protected']:
                    return {'ok': False, 'error': 'protected_container', 'reason': '演示容器受保护'}
                if operation not in item.get('available_actions', []):
                    return {'ok': False, 'error': 'invalid_state', 'reason': '当前状态不支持此操作'}
                item['state'] = 'exited' if operation == 'stop' else 'running'
                item['status'] = 'Exited (0) just now' if operation == 'stop' else 'Up 1 second'
                item['available_actions'] = ['start'] if operation == 'stop' else ['stop', 'restart']
                return {'ok': True, 'refresh_pending': True}
            if name == 'vm.action':
                identity, operation = payload.get('id'), payload.get('operation')
                vm = next((row for row in self.vms if row['id'] == identity), None)
                if not vm:
                    return {'ok': False, 'error': 'not_found', 'reason': '演示虚拟机不存在'}
                vm['state'] = {'start': 'running', 'resume': 'running', 'shutdown': 'shut off',
                               'reboot': 'running', 'suspend': 'paused'}.get(operation, vm['state'])
                return {'ok': True, 'refresh_pending': True}
            return {'ok': False, 'error': 'unsupported', 'reason': '演示环境不支持此操作'}

    # ------------------------------------------------------------ monitoring
    def monitoring_settings(self):
        return copy.deepcopy(self.monitor_settings)

    def monitoring(self):
        settings = self.monitoring_settings()
        with self.lock:
            metrics = [
                {'id': 'cpu_usage_percent', 'label': 'CPU 使用率', 'unit': '%', 'value': round(self.cpu, 1)},
                {'id': 'memory_usage_percent', 'label': '内存使用率', 'unit': '%', 'value': round(self.mem, 1)},
                {'id': 'cpu_temperature_c', 'label': 'CPU 最高温度', 'unit': '°C', 'value': round(self.cpu_temp, 1)},
                {'id': 'cpu_package_w', 'label': 'CPU 封装功耗 · RAPL', 'unit': 'W',
                 'value': round(9 + self.cpu * 0.16, 1)},
            ]
            return {'available': True, 'settings': settings, 'metrics': metrics,
                    'power': {'available': True, 'cpu_package_w': round(9 + self.cpu * 0.16, 1),
                              'scope': 'cpu_package', 'domains': [], 'reason': None},
                    'disk_io': [{'name': 'sda', 'read_bytes_per_sec': self.history[-1]['dr'] if self.history else 0,
                                 'write_bytes_per_sec': self.history[-1]['dw'] if self.history else 0}],
                    'summary': {'status': 'ok', 'label': '演示数据 · 各项指标正常',
                                'known_metrics': len(metrics), 'unknown_metrics': 0},
                    'history': {}}

    def monitoring_history(self, span):
        seconds = {'10m': 600, '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000}.get(span, 3600)
        cutoff = time.time() - seconds
        rows = [row for row in self.history if row['ts'] >= cutoff]
        keys = [('cpu_usage_percent', 'cpu'), ('memory_usage_percent', 'mem'),
                ('cpu_temperature_c', 'temp'), ('cpu_package_w', 'power'),
                ('rx_bytes_per_sec', 'rx'), ('tx_bytes_per_sec', 'tx'),
                ('disk_read_bytes_per_sec', 'dr'), ('disk_write_bytes_per_sec', 'dw')]
        return {'available': bool(rows), 'range': span, 'series': [
            {'id': key, 'points': [[row['ts'], row[field]] for row in rows]} for key, field in keys]}

    def monitoring_traffic(self):
        with self.lock:
            return {'available': True, 'interfaces': [{'name': 'eth0', 'today': {'rx_bytes': int(self.today[0]), 'tx_bytes': int(self.today[1])},
                                                       'month': {'rx_bytes': int(self.month[0]), 'tx_bytes': int(self.month[1])}}],
                    'totals': {'today': {'rx_bytes': int(self.today[0]), 'tx_bytes': int(self.today[1])},
                               'month': {'rx_bytes': int(self.month[0]), 'tx_bytes': int(self.month[1])}},
                    'reason': None}

    def monitoring_report(self):
        with self.lock:
            return {'schema_version': 1, 'generated_at': int(time.time()),
                    'summary': {'cpu': {'avg': round(self.cpu, 1)}, 'memory': {'avg': round(self.mem, 1)}},
                    'note': '演示环境'}

    def monitoring_csv(self, span):
        return 'timestamp,metric,value\r\n' + ''.join(
            '%d,%s,%s\r\n' % (row['ts'], 'cpu_usage_percent', row['cpu']) for row in self.history)

    def monitoring_diag(self):
        return {'available': True, 'rows': len(self.history), 'data_dir': 'demo', 'db_exists': True, 'last_ts': None}

    def monitoring_action(self, name, payload):
        if name != 'monitor.settings.update' or not isinstance(payload.get('settings'), dict):
            return {'ok': False, 'settings_saved': False, 'error': '统计设置无效'}
        with self.lock:
            for key, value in payload['settings'].items():
                if key == 'thresholds' and isinstance(value, dict):
                    self.monitor_settings['thresholds'].update(value)
                elif key in self.monitor_settings:
                    self.monitor_settings[key] = value
            return {'ok': True, 'settings_saved': True}


class DemoController:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.toolbox()

    def action(self, name, payload):
        return self.nas.controller_action(name, payload)


class DemoInsights:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.insights()

    def action(self, name, payload):
        return self.nas.insights_action(name, payload)


class DemoFanPolicy:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.fan_snapshot()

    def action(self, name, payload):
        return self.nas.fan_action(name, payload)


class DemoLightPolicy:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.light_snapshot()

    def action(self, name, payload):
        return self.nas.light_action(name, payload)


class DemoMonitor:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.monitor()


class DemoMonitoring:
    def __init__(self, nas):
        self.nas = nas

    def snapshot(self):
        return self.nas.monitoring()

    def history(self, span):
        return self.nas.monitoring_history(span)

    def history_diag(self):
        return self.nas.monitoring_diag()

    def traffic(self):
        return self.nas.monitoring_traffic()

    def alerts(self):
        return {'available': True, 'events': [], 'active': [], 'reason': '演示环境无告警'}

    def report(self):
        return self.nas.monitoring_report()

    def export_csv(self, span='24h'):
        return self.nas.monitoring_csv(span)

    def action(self, name, payload):
        return self.nas.monitoring_action(name, payload)
