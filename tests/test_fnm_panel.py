"""Unit and HTTP checks for the fnmonitor panel adapter and password auth.

Everything runs against fixture snapshots: no /proc, /sys, Docker, SMART or I2C
access, and no browser.
"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / 'runtime' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auth_module = load('fnm_auth')
compat = load('fnm_compat')
web = load('web_config')


class Controller:
    def __init__(self):
        self.state_dir = tempfile.mkdtemp(prefix='r1-panel-ctl-')
        self.calls = []
        self.settings = {'brightness': 80, 'sleep_timeout': 0, 'night_enabled': False,
                         'night_start': '22:00', 'night_end': '07:00', 'night_brightness': 20}

    def snapshot(self):
        return {'settings': copy.deepcopy(self.settings),
                'capabilities': {'screen': {'available': True}, 'brightness': {'available': True},
                                 'rgb': {'available': True}, 'power': {'available': True}},
                'screen': {'brightness': 80, 'asleep': False},
                'rgb': {'mode': 1, 'color': '#00C832', 'brightness': 60},
                'fans': {}, 'power': {'pending': None}}

    def action(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        return {'ok': True, 'settings_saved': name == 'settings.update', 'state': self.snapshot()}


class FanPolicy:
    def __init__(self, available=True):
        self.calls = []

    def snapshot(self):
        return {'config': {'schema_version': 1, 'channels': {
                    'cpu': {'mode': 'vendor', 'sources': [], 'quiet_below': 40, 'normal_at': 55,
                            'full_at': 70, 'quiet_percent': 55, 'normal_percent': 75, 'manual_percent': 70},
                    'system': {'mode': 'vendor', 'sources': [], 'quiet_below': 35, 'normal_at': 45,
                               'full_at': 55, 'quiet_percent': 55, 'normal_percent': 75, 'manual_percent': 70}}},
                'active': False, 'busy': False, 'pending_changes': False, 'recovery_confirmed': True,
                'capability': {'available': True, 'reason': ''},
                'channels': {'cpu': {'mode': 'vendor', 'active': False, 'reference_c': None,
                                     'target_percent': None, 'level': None, 'source_label': None, 'reason': None},
                             'system': {'mode': 'vendor', 'active': False, 'reference_c': None,
                                        'target_percent': None, 'level': None, 'source_label': None, 'reason': None}},
                'sources': [{'id': 'cpu', 'label': 'CPU Package', 'kind': 'cpu', 'temperature_c': 62.0,
                             'status': 'ok', 'fresh': True, 'max_age_seconds': 5}],
                'last_error': None}

    def action(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        return {'ok': True, 'settings_saved': True, 'pending': payload.get('apply') is True,
                'fan_policy': self.snapshot()}


class LightPolicy:
    def snapshot(self):
        return {'config': {'schema_version': 1, 'enabled': False, 'mode': 'manual', 'sources': [],
                           'stops': [{'temperature': 35, 'color': '#00C832'}],
                           'brightness': 70, 'manual': {'mode': 1, 'color': '#00C832', 'brightness': 70},
                           'schedule': {'enabled': False, 'start': '22:00', 'end': '07:00', 'brightness': 15}},
                'active': False, 'busy': False, 'pending_changes': False,
                'capability': {'available': False, 'reason': '未验证唯一灯控设备'},
                'channels': {}, 'runtime': {}, 'target': None, 'sources': [], 'last_error': None}

    def action(self, name, payload):
        return {'ok': True, 'settings_saved': True, 'light_policy': self.snapshot()}


class Monitoring:
    def __init__(self):
        self.settings = {'history_enabled': True, 'retention_days': 30, 'traffic_exclude_virtual': True,
                         'thresholds': {'cpu_warn': 85, 'cpu_critical': 95}}
        self.calls = []

    def snapshot(self):
        return {'available': True, 'settings': copy.deepcopy(self.settings),
                'power': {'available': True, 'cpu_package_w': 12.5, 'domains': [], 'scope': 'cpu_package'},
                'disk_io': [{'name': 'sda', 'read_bytes_per_sec': 1048576.0, 'write_bytes_per_sec': 0.0}],
                'summary': {}}

    def history(self, span):
        return {'range': span, 'available': True, 'series': [
            {'id': 'cpu_usage_percent', 'points': [[100, 10.0], [130, 20.0], [160, None]]},
            {'id': 'memory_usage_percent', 'points': [[100, 40.0], [130, 41.0]]},
            {'id': 'disk_read_bytes_per_sec', 'points': [[100, 2097152.0]]},
            {'id': 'temperature.cpu', 'points': [[100, 60.0]]},
        ]}

    def traffic(self):
        return {'totals': {'today': {'rx_bytes': 100, 'tx_bytes': 50},
                           'month': {'rx_bytes': 1000, 'tx_bytes': 500}},
                'interfaces': [{'name': 'eth0', 'today': {'rx_bytes': 100, 'tx_bytes': 50},
                                'month': {'rx_bytes': 1000, 'tx_bytes': 500}}],
                'reason': None}

    def report(self):
        return {'summary': {'cpu': {'avg': 10}}}

    def export_csv(self, span):
        return 'timestamp_utc,metric,value\r\n'

    def history_diag(self):
        return {'available': True, 'rows': 3}

    def action(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        return {'ok': True, 'settings_saved': True}


class Insights:
    def __init__(self):
        self.container_calls = []

    def snapshot(self):
        return {
            'system': {'cpu_usage_percent': 24.5, 'cpu_logical_cores': 4,
                       'load_average': [0.2, 0.3, 0.5],
                       'memory': {'total_bytes': 8 * 1024 ** 3, 'used_bytes': 3 * 1024 ** 3,
                                  'available_bytes': 5 * 1024 ** 3, 'usage_percent': 37.5,
                                  'swap_total_bytes': 0, 'swap_used_bytes': 0},
                       'uptime_seconds': 201600,
                       'cpu_cores': [{'id': 0, 'usage_percent': 10.0}, {'id': 1, 'usage_percent': 15.0}]},
            'interfaces': [{'name': 'eth0', 'ipv4': ['192.168.1.10'], 'mac': '00:11:22:33:44:55',
                            'operstate': 'up', 'rx_bytes': 400, 'tx_bytes': 100,
                            'rx_bytes_per_sec': 1250.0, 'tx_bytes_per_sec': 480.0}],
            'disks': {'available': True, 'sampled_at': '2026-10-28T00:00:00+00:00', 'items': [
                {'id': 'sda', 'device': '/dev/sda', 'model': 'Fixture Drive',
                 'size_bytes': 4 * 10 ** 12, 'temp_c': 39, 'standby': False,
                 'smart_available': True, 'health': {'passed': True, 'reason': None},
                 'temperature_critical_c': 65}, {'id': 'sdb', 'device': '/dev/sdb',
                 'size_bytes': 1 * 10 ** 12, 'standby': False, 'smart_available': False,
                 'health': {'passed': None, 'reason': 'SMART 未读取'}}]},
            'containers': {'available': True, 'sampled_at': '2026-10-28T00:00:00+00:00', 'items': [
                {'id': 'a' * 64, 'name': 'fixture-db', 'image': 'fixture:local', 'state': 'running',
                 'status': 'Up 1 hour', 'ports': '0.0.0.0:8080->8080/tcp', 'protected': False,
                 'available_actions': ['stop', 'restart'], 'cpu_percent': 3.2,
                 'memory_bytes': 134217728, 'memory_percent': 1.6}]},
            'vms': {'available': False, 'reason': '未安装 virsh', 'items': []},
            'ports': [{'protocol': 'tcp', 'address': '127.0.0.1', 'port': 18731, 'inode': 1}],
            'processes': [{'pid': 101, 'name': 'fixture-monitor', 'cpu_percent': 1.2,
                           'memory_bytes': 40000000}],
            'gpu': {'available': False, 'reason': '无 GPU', 'devices': []},
            'device': {'identity': {'hostname': 'R1-Fixture', 'kernel': '6.1-fixture',
                                    'os': {'PRETTY_NAME': 'Fixture Linux', 'ID': 'debian'},
                                    'firmware': {'version': 'V6.3.3_Build_260907'}, 'pci': [],
                                    'dmi': {'product_name': 'HikSemi R1', 'sys_vendor': 'HikSemi'}},
                       'cpu': {'model': 'Intel N100', 'physical_cores': 4, 'logical_cores': 4,
                               'sockets': 1, 'architecture': 'x86_64',
                               'frequencies_mhz': [{'current': 800}, {'current': 1200}]},
                       'thermal': [{'label': 'Package id 0', 'celsius': 62.0, 'driver': 'coretemp',
                                    'channel': 'temp1', 'source': 'hwmon', 'max_celsius': 90}],
                       'fans': [{'label': 'fan1', 'rpm': 1200, 'channel': 'fan1', 'source': 'hwmon'}],
                       'storage': {'devices': [{'name': 'sda', 'model': 'Fixture Drive', 'vendor': 'Fixture',
                                                'serial': 'SN1', 'bytes': 4 * 10 ** 12, 'rotational': 1}],
                                   'raid': []},
                       'network': {'interfaces': [{'name': 'eth0', 'mac': '00:11:22:33:44:55', 'state': 'up',
                                                   'carrier': 1, 'speed_mbps': 2500, 'mtu': 1500,
                                                   'ipv6_addresses': [{'address': '2001:db8::1'}],
                                                   'default_gateways': {'ipv4': [{'gateway': '192.168.1.1'}]}}]}},
        }

    def action(self, name, payload):
        self.container_calls.append((name, copy.deepcopy(payload)))
        return {'ok': True, 'refresh_pending': True}


class Monitor:
    def snapshot(self):
        return {'available': True, 'hostname': 'R1-Fixture', 'uptime_seconds': 201600,
                'cpu': {'usage_percent': 24.5, 'temp_c': 62.0},
                'memory': {'usage_percent': 37.5}, 'network': {},
                'disks': [{'mount': '/drives/data', 'total_bytes': 4 * 10 ** 12,
                           'used_bytes': 10 ** 12, 'usage_percent': 25.0}]}


def panel_for(state_dir, *, fan=None, light=None, web_ref=None, weather_fetcher=None, insights=None, proc_root=None, dev_root=None, sys_root=None, udev_root=None):
    return compat.FnmonitorPanel(Controller(), insights or Insights(), Monitoring(), fan or FanPolicy(),
                                 light or LightPolicy(), Monitor(), state_dir=state_dir,
                                 version='test', web=web_ref,
                                 proc_root=Path(proc_root or tempfile.mkdtemp(prefix='r1-panel-proc-')),
                                 sys_root=Path(sys_root or tempfile.mkdtemp(prefix='r1-panel-sys-')),
                                 dev_root=Path(dev_root or tempfile.mkdtemp(prefix='r1-panel-dev-')),
                                 udev_root=Path(udev_root or tempfile.mkdtemp(prefix='r1-panel-udev-')),
                                 weather_fetcher=weather_fetcher or (lambda city: {'ok': False, 'error': 'fixture: no weather'}))


class AuthTests(unittest.TestCase):
    def test_password_hash_round_trip_and_rejects_malformed(self):
        digest = auth_module.hash_password('correct horse', iterations=1000)
        self.assertTrue(digest.startswith('pbkdf2_sha256$1000$'))
        self.assertTrue(auth_module.verify_password('correct horse', digest))
        self.assertFalse(auth_module.verify_password('wrong', digest))
        for broken in ('', 'plain', 'pbkdf2_sha256$x$y$z', 'md5$1$a$b', None):
            self.assertFalse(auth_module.verify_password('correct horse', broken))

    def test_sessions_are_hashed_persisted_and_expire(self):
        directory = Path(tempfile.mkdtemp(prefix='r1-auth-'))
        now = [1000.0]
        store = auth_module.AuthStore(directory, wall_clock=lambda: now[0])
        store._write_config({'auth_password_hash': auth_module.hash_password('secret12'), 'auth_enabled': 1,
                             'auth_session_days': 2})
        self.assertTrue(store.required())
        sid = store.create_session('password')
        self.assertEqual(len(sid), 43)
        raw = json.loads((directory / 'panel-sessions.json').read_text(encoding='utf-8'))
        self.assertNotIn(sid, raw)
        again = auth_module.AuthStore(directory, wall_clock=lambda: now[0])
        self.assertIsNotNone(again.session('fnmp_sid=' + sid))
        now[0] += 3 * 86400
        self.assertIsNone(again.session('fnmp_sid=' + sid))
        self.assertEqual(again.sessions, {})

    def test_login_limiter_locks_then_clears_on_success(self):
        limiter = auth_module.LoginLimiter()
        for _ in range(auth_module.LoginLimiter.MAX_FAILS):
            limiter.fail('192.168.1.9')
        self.assertGreater(limiter.retry_after('192.168.1.9'), 0)
        limiter.success('192.168.1.9')
        self.assertEqual(limiter.retry_after('192.168.1.9'), 0)

    def test_change_password_requires_old_and_revokes_others(self):
        directory = Path(tempfile.mkdtemp(prefix='r1-auth-'))
        store = auth_module.AuthStore(directory)
        self.assertEqual(store.setup('initial-1', 'x')['ok'], True)
        other = store.create_session('second')
        self.assertFalse(store.change('wrong', 'next-pass')['ok'])
        result = store.change('initial-1', 'next-pass')
        self.assertTrue(result['ok'])
        self.assertIsNotNone(store.session('fnmp_sid=' + result['sid']))
        self.assertIsNone(store.session('fnmp_sid=' + other))


class StoreTests(unittest.TestCase):
    def test_config_ranges_and_ui_sanitising(self):
        directory = Path(tempfile.mkdtemp(prefix='r1-store-'))
        store = compat.PanelStore(directory)
        with self.assertRaises(ValueError):
            store.save_config({'retention_days': 99})
        with self.assertRaises(ValueError):
            store.save_config({'retention_days': '30'})
        with self.assertRaises(ValueError):
            store.save_config({'unknown': 1})
        saved = store.save_config({'retention_days': 7})
        self.assertEqual(saved['retention_days'], 7)
        self.assertEqual(compat.PanelStore(directory).config()['retention_days'], 7)

        clean = store.save_ui({'theme': 'dark', 'fontScale': 99, 'accent': '<script>',
                               'thresholds': {'cpuWarn': 50, 'cpuCrit': 40},
                               'ignored': [{'key': 'cpu', 'fp': '1', 'label': 'CPU'}],
                               'unknown': 'drop me'})
        self.assertEqual(clean['theme'], 'dark')
        self.assertEqual(clean['fontScale'], 1.5)
        self.assertNotIn('accent', clean)
        self.assertNotIn('unknown', clean)
        self.assertEqual(clean['thresholds']['cpuCrit'], 50)
        self.assertEqual(len(clean['ignored']), 1)
        with self.assertRaises(ValueError):
            store.save_ui({'layout': {'overview': ['x'] * 200000}})


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='r1-panel-map-'))
        self.panel = panel_for(self.directory)

    def test_overview_uses_real_snapshots_and_keeps_unknowns_null(self):
        data = self.panel.overview()
        self.assertEqual(data['cpu']['percent'], 24.5)
        self.assertEqual(data['cpu']['frequency_mhz'], 1200)
        self.assertEqual(data['cpu']['per_core'], [10.0, 15.0])
        self.assertEqual(data['mem']['percent'], 37.5)
        self.assertEqual(data['disk_total'], 25.0)
        self.assertEqual([row['name'] for row in data['disks_detail']], ['sda', 'sdb'])
        self.assertEqual(data['disks_detail'][0]['temp'], 39)
        self.assertEqual(data['disks_detail'][0]['health'], 'PASS')
        self.assertEqual(data['disks'], [])
        self.assertEqual(data['temp']['cpu'], 62.0)
        self.assertEqual(data['power']['total'], 12.5)
        self.assertEqual(data['power']['source'], 'rapl')
        self.assertEqual(data['net'][0]['rx_rate'], 1250.0)
        self.assertEqual(data['sensors']['fans'][0]['rpm'], 1200)
        self.assertEqual(data['docker']['containers'][0]['mem_usage'], '128 MiB')
        self.assertEqual(data['ports']['listeners'][0]['port'], 18731)
        self.assertEqual(data['system']['hostname'], 'R1-Fixture')
        self.assertEqual(data['system']['fnos_version'], 'V6.3.3_Build_260907')
        self.assertEqual(data['memory'], [])
        self.assertEqual(data['raidcard']['type'], 'none')

        empty = compat.FnmonitorPanel(Controller(), None, None, None, None, None,
                                      state_dir=self.directory, version='test')
        overview = empty.overview()
        self.assertIsNone(overview['cpu']['percent'])
        self.assertEqual(overview['disks_detail'], [])
        self.assertFalse(overview['docker']['available'])
        self.assertIsNone(overview['power']['total'])

    def test_disk_volume_comes_from_drives_mount(self):
        proc = Path(tempfile.mkdtemp(prefix='r1-panel-proc-'))
        (proc / 'self').mkdir()
        (proc / 'self/mountinfo').write_text(
            '123 1 8:1 / /drives/VOL1 rw - ext4 /dev/sda1 rw\n'
            '124 1 8:2 / /drives/SYSTEM rw - ext4 /dev/sda2 rw\n', encoding='utf-8')
        panel = panel_for(tempfile.mkdtemp(prefix='r1-panel-vol-'), proc_root=proc)
        row = panel.overview()['disks_detail'][0]
        self.assertEqual(row['volume'], 'VOL1')
        self.assertIsNone(row['volume_label'])

    def test_volume_label_falls_back_to_udev_database(self):
        sys_root = Path(tempfile.mkdtemp(prefix='r1-panel-sys-'))
        dev = sys_root / 'block' / 'sda'
        dev.mkdir(parents=True)
        (dev / 'dev').write_text('8:0', encoding='ascii')
        udev = Path(tempfile.mkdtemp(prefix='r1-panel-udev-'))
        (udev / 'data').mkdir()
        (udev / 'data' / 'b8:0').write_text('E:ID_FS_LABEL=NAS-DISK4\n', encoding='utf-8')
        panel = panel_for(tempfile.mkdtemp(prefix='r1-panel-udev-test-'), sys_root=sys_root, udev_root=udev)
        self.assertEqual(panel._volume_labels().get('sda'), 'NAS-DISK4')

    def test_sleeping_disk_stays_visible_without_smart_or_wakeup(self):
        class SleepingInsights(Insights):
            def snapshot(self):
                data = super().snapshot()
                data['disks']['items'].append({
                    'id': 'sdz', 'device': '/dev/sdz', 'model': 'Sleeping fixture',
                    'size_bytes': 3 * 10 ** 12, 'temp_c': 31, 'standby': True,
                    'smart_available': False,
                    'health': {'passed': None, 'reason': '磁盘处于休眠，已跳过 SMART 读取'},
                })
                return data

        panel = panel_for(tempfile.mkdtemp(prefix='r1-panel-sleep-'), insights=SleepingInsights())
        disks = panel.overview()['disks_detail']
        row = next(item for item in disks if item['name'] == 'sdz')
        self.assertTrue(row['standby'])
        self.assertFalse(row['smart_available'])
        self.assertIsNone(row['health'])
        self.assertEqual(row['size'], 3 * 10 ** 12)
        self.assertEqual(row['temp'], 31)
        health = panel.disk_health()['items']['sdz']
        self.assertEqual(health['grade'], 'ok')
        self.assertIn('休眠', health['detail'])

    def test_history_maps_two_series_metrics_and_rejects_bad_ranges(self):
        cpu = self.panel.history('cpu', '1h')
        self.assertEqual(cpu['points'], [[100, 10.0], [130, 20.0], [160, None]])
        net = self.panel.history('net', '24h')
        self.assertEqual(net['rx'], [])
        diskio = self.panel.history('diskio', '7d')
        self.assertEqual(diskio['r'], [[100, 2.0]])
        self.assertEqual(self.panel.history('cpu', 'all')['range'], 'all')
        for metric, span in (('nope', '1h'), ('cpu', '2h')):
            with self.assertRaises(ValueError):
                self.panel.history(metric, span)

    def test_disk_health_and_smart_detail_use_cached_snapshot_only(self):
        health = self.panel.disk_health()
        self.assertEqual(health['items']['sda']['grade'], 'ok')
        self.assertEqual(health['source'], 'background_smart')
        detail = self.panel.smart('sda')
        self.assertEqual(detail['source'], 'background_smartctl')
        self.assertEqual(detail['attributes'], [])
        self.assertEqual(self.panel.smart('missing')['ok'], False)
        with self.assertRaises(ValueError):
            self.panel.smart('../etc/passwd')

    def test_disk_io_reads_nested_monitoring_sample(self):
        self.panel.monitoring.snapshot = lambda: {
            'current': {'disk_io': [{'name': 'sdc', 'read_bytes_per_sec': 2048.0, 'write_bytes_per_sec': 512.0}]},
        }
        row = self.panel.overview()['diskio'][0]
        self.assertEqual(row['name'], 'sdc')
        self.assertEqual(row['read_rate'], 2048.0)
        self.assertEqual(row['write_rate'], 512.0)

    def test_smart_detail_returns_cached_full_attributes(self):
        self.panel.insights.smart_cached = lambda name: {
            'smart_data': {
                'ata_smart_attributes': {'table': [{
                    'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 100, 'worst': 100,
                    'thresh': 10, 'when_failed': '-', 'raw': {'value': 0, 'string': '0'},
                }]},
                'power_on_time': {'hours': 123},
                'power_cycle_count': 4,
                'rotation_rate': 7200,
            },
        } if name == 'sda' else None
        detail = self.panel.smart('sda')
        self.assertEqual(detail['attributes'][0]['name'], 'Reallocated_Sector_Ct')
        self.assertEqual(detail['attributes'][0]['raw_text'], '0')
        self.assertEqual(detail['power_on_hours'], 123)
        self.assertEqual(detail['power_cycle_count'], 4)
        self.assertEqual(detail['rotation_rate'], 7200)
        self.assertIn('ata_smart_attributes', detail['raw'])

    def test_unsupported_integrations_report_unavailable(self):
        self.assertFalse(self.panel.weather()['ok'])
        self.assertEqual(self.panel.notify()['enabled'], 0)
        self.assertFalse(self.panel.idrac()['ok'])
        self.assertFalse(self.panel.monthly_summary()['available'])
        self.assertEqual(self.panel.modules(), {'modules': []})
        self.assertEqual(self.panel.apps(), {'apps': []})

    def test_weather_uses_saved_city_and_caches(self):
        calls = []

        def fetch(city):
            calls.append(city)
            return {'ok': True, 'city': '徐州', 'temp': 20.0, 'feels': 20.0,
                    'humidity': 50.0, 'wind': 1.0, 'code': 0, 'desc': '晴', 'is_day': 1}

        panel = panel_for(tempfile.mkdtemp(prefix='r1-panel-weather-'), weather_fetcher=fetch)
        saved = panel.save_config({'weather_city': '34.26,117.18'})
        self.assertTrue(saved['ok'])
        first = panel.weather()
        second = panel.weather()
        self.assertTrue(first['ok'])
        self.assertEqual(first['temp'], 20.0)
        self.assertEqual(calls, ['34.26,117.18'])
        self.assertIsNot(first, second)

    def test_weather_rejects_bad_coordinates_without_network(self):
        self.assertEqual(compat._weather_coords('34.26,117.18'), (34.26, 117.18))
        self.assertIsNone(compat._weather_coords('91,0'))
        self.assertIsNone(compat._weather_coords('abc'))

    def test_r1_state_exposes_runtime_and_authorization(self):
        state = self.panel.r1_state(authorized=True)
        self.assertEqual(state['fan']['capability']['available'], True)
        self.assertEqual(state['control']['screen']['brightness'], 80)
        self.assertTrue(state['auth']['authorized'])
        self.assertIn('pairing', state['auth'])
        self.assertEqual(state['device']['model'], 'HikSemi R1')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='r1-panel-http-'))
        self.now = 1000.0
        self.fan = FanPolicy()
        self.controller = Controller()
        self.insights = Insights()
        self.monitoring = Monitoring()
        self.light = LightPolicy()
        self.web = web.WebConfigServer(self.controller, self.insights, fan_policy=self.fan,
                                       light_policy=self.light, monitoring=self.monitoring,
                                       monitor=Monitor(), host='127.0.0.1', port=0,
                                       asset_dir=ROOT / 'ui/web', clock=lambda: self.now,
                                       state_dir=self.directory, version='test').start()
        self.addCleanup(self.web.stop)
        self.host = '127.0.0.1:' + str(self.web.port)
        self.origin = 'http://' + self.host
        self.cookie = None

    def request(self, method, path, body=None, extra=None, origin=True, form=False):
        connection = socket.create_connection(('127.0.0.1', self.web.port), timeout=5)
        try:
            payload = b'' if body is None else json.dumps(body).encode()
            headers = ['%s %s HTTP/1.0' % (method, path), 'Host: ' + self.host]
            if origin:
                headers.append('Origin: ' + self.origin)
            if self.cookie:
                headers.append('Cookie: ' + self.cookie)
            if body is not None:
                headers += ['Content-Type: application/json', 'Content-Length: %d' % len(payload)]
            for key, value in (extra or {}).items():
                headers.append('%s: %s' % (key, value))
            if form:
                headers = [line for line in headers if not line.lower().startswith('content-type:')]
                headers.append('Content-Type: application/x-www-form-urlencoded')
            connection.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode() + payload)
            raw = b''
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            connection.close()
        head, _, body_bytes = raw.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        status = int(lines[0].split()[1])
        response_headers = {}
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                response_headers[key.strip().lower()] = value.strip()
        try:
            value = json.loads(body_bytes.decode('utf-8')) if body_bytes else None
        except ValueError:
            value = body_bytes
        return status, response_headers, value

    def pair(self):
        code = self.web.pairing_info(rotate=True)['code']
        status, headers, _ = self.request('POST', '/api/pair', {'code': code})
        self.assertEqual(status, 200)
        self.cookie = headers['set-cookie'].split(';', 1)[0]
        return code

    def test_response_write_does_not_reuse_the_request_reader_timeout(self):
        # A request is parsed with a 2 s socket timeout.  That timeout must not
        # govern the response write: a large panel page on a slow link was being
        # truncated while Content-Length still advertised the full body.
        class Connection:
            timeout = 2

            def settimeout(self, value):
                self.timeout = value

        handler = object.__new__(self.web.server.RequestHandlerClass)
        handler.connection = Connection()
        handler.wfile = io.BytesIO()
        handler.path = '/'
        handler.close_connection = False
        handler.send_response = lambda code: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None
        handler.send(200, b'x' * 4096, mime='text/html; charset=utf-8')
        self.assertGreaterEqual(handler.connection.timeout, 30)
        self.assertEqual(len(handler.wfile.getvalue()), 4096)

    def test_read_routes_and_unknown_query_rejection(self):
        self.assertEqual(self.request('GET', '/api/overview')[0], 200)
        self.assertEqual(self.request('GET', '/api/history?metric=cpu&range=1h')[0], 200)
        self.assertEqual(self.request('GET', '/api/history?metric=cpu&range=2h')[0], 400)
        self.assertEqual(self.request('GET', '/api/history?range=1h&x=1')[0], 400)
        self.assertEqual(self.request('GET', '/api/docker')[2]['containers'][0]['name'], 'fixture-db')
        self.assertEqual(self.request('GET', '/api/v1/status')[0], 200)
        self.assertEqual(self.request('GET', '/api/ui')[0], 200)

    def test_writes_need_pairing_and_csrf_marker(self):
        status, _, value = self.request('POST', '/api/r1/fan', {'config': {}})
        self.assertEqual(status, 403)
        self.assertEqual(value['error'], 'csrf_rejected')
        status, _, value = self.request('POST', '/api/r1/fan', {'config': {}}, {'X-FNM-Request': '1'})
        self.assertEqual(status, 403)
        self.assertEqual(value['error'], 'need_pairing')
        self.pair()
        config = self.fan.snapshot()['config']
        status, _, value = self.request('POST', '/api/r1/fan',
                                        {'config': config, 'apply': False},
                                        {'X-FNM-Request': '1'})
        self.assertEqual(status, 200, value)
        self.assertEqual(self.fan.calls[-1][0], 'fan.policy.update')

    def test_container_actions_require_pairing_and_confirm_flag(self):
        identity = 'a' * 64
        self.assertEqual(self.request('POST', '/api/docker/action', {'id': identity, 'action': 'stop'},
                                      {'X-FNM-Request': '1'})[0], 403)
        self.pair()
        status, _, value = self.request('POST', '/api/docker/action', {'id': identity, 'action': 'stop'},
                                        {'X-FNM-Request': '1'})
        self.assertEqual(status, 200, value)
        self.assertEqual(self.insights.container_calls[-1][1], {'id': identity, 'operation': 'stop', 'confirmed': True})

    def test_r1_screen_light_and_power_actions_reach_the_controller(self):
        self.pair()
        marker = {'X-FNM-Request': '1'}
        status, _, _ = self.request('POST', '/api/r1/screen', {'action': 'brightness', 'value': 55}, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls[-1], ('screen.brightness', {'value': 55}))
        self.assertEqual(self.request('POST', '/api/r1/screen', {'action': 'brightness', 'value': 101}, marker)[0], 400)
        status, _, _ = self.request('POST', '/api/r1/rgb',
                                    {'mode': 1, 'color': '#112233', 'brightness': 40}, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls[-1][0], 'rgb.set')
        status, _, _ = self.request('POST', '/api/r1/power', {'operation': 'reboot', 'confirmed': True}, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls[-1], ('power.request', {'operation': 'reboot', 'confirmed': True}))
        self.assertEqual(self.request('POST', '/api/r1/power', {'operation': 'reboot'}, marker)[0], 400)
        status, _, _ = self.request('POST', '/api/r1/settings',
                                    {'settings': {'sleep_timeout': 120}}, marker)
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls[-1][0], 'settings.update')

    def test_light_policy_and_retention_writes_use_the_policy_services(self):
        self.pair()
        marker = {'X-FNM-Request': '1'}
        config = self.web.light_policy.snapshot()['config']
        status, _, value = self.request('POST', '/api/r1/light', {'config': config, 'apply': False}, marker)
        self.assertEqual(status, 200, value)
        status, _, value = self.request('POST', '/api/config/save', {'retention_days': 7}, marker)
        self.assertEqual(status, 200, value)
        self.assertTrue(any(call[1].get('settings', {}).get('retention_days') == 7
                            for call in self.monitoring.calls))

    def test_ui_write_is_allowed_without_pairing_but_never_credentials(self):
        status, _, value = self.request('POST', '/api/ui', {'theme': 'dark', 'layoutVersion': 200},
                                        {'X-FNM-Request': '1'})
        self.assertEqual(status, 200, value)
        self.assertEqual(self.request('GET', '/api/ui')[2]['ui']['theme'], 'dark')

    def test_password_enable_gates_pages_and_reads(self):
        self.pair()
        status, _, _ = self.request('POST', '/api/auth/setup', {'password': 'fixture-pass-1'},
                                    {'X-FNM-Request': '1'})
        self.assertEqual(status, 200)
        # Drop the pairing cookie: the panel must now serve the login page.
        self.cookie = None
        status, _, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'login', body.lower() if isinstance(body, bytes) else str(body).lower())
        self.assertEqual(self.request('GET', '/api/overview')[0], 401)
        status, headers, _ = self.request('POST', '/api/auth/password', {'password': 'fixture-pass-1'},
                                         {'X-FNM-Request': '1'})
        self.assertEqual(status, 200)
        self.cookie = headers['set-cookie'].split(';', 1)[0]
        self.assertIn(auth_module.COOKIE, self.cookie)
        self.assertEqual(self.request('GET', '/api/overview')[0], 200)
        self.assertEqual(self.request('GET', '/')[0], 200)

    def test_login_rate_limit_and_wrong_password(self):
        self.pair()
        self.request('POST', '/api/auth/setup', {'password': 'fixture-pass-1'}, {'X-FNM-Request': '1'})
        self.cookie = None
        status, _, value = self.request('POST', '/api/auth/password', {'password': 'wrong'},
                                        {'X-FNM-Request': '1'})
        self.assertEqual(status, 401)
        self.assertEqual(value['error'], '口令错误')

    def test_first_password_cannot_be_set_without_pairing(self):
        status, _, value = self.request('POST', '/api/auth/setup', {'password': 'fixture-pass-1'},
                                        {'X-FNM-Request': '1'})
        self.assertEqual(status, 403)
        self.assertEqual(value['error'], 'need_pairing')
        self.assertFalse(self.web.auth.password_set())

    def test_session_days_change_keeps_current_session(self):
        self.pair()
        status, _, _ = self.request('POST', '/api/r1/auth', {'action': 'session_days', 'days': 3},
                                    {'X-FNM-Request': '1'})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('GET', '/api/session')[0], 200)


if __name__ == '__main__':
    unittest.main()
