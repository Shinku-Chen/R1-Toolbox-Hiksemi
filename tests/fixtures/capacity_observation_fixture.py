"""Owner HTTP capacity observations from the real sampler publication path.

Mountinfo and block identity are private TEMP metadata. Only the OS mount-watch,
sysfs-link and capacity worker seams are controlled. No history/API field is
injected: the real StatusSampler/StatusMonitor/Fnm/Web server must create it.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import types

repo, fixture = (Path(value).resolve() for value in sys.argv[1:3])
assert fixture.name.startswith('r1-capacity-observation-') and fixture.is_dir()
sys.path.insert(0, str(repo / 'runtime'))
import ui_service as service
from fnm_compat import FnmonitorPanel
from web_config import WebConfigServer, AUTH_COOKIE, COOKIE

forbidden = []


def reject_io(*args, **kwargs):
    forbidden.append('hardware_or_outbound_io')
    raise AssertionError('capacity fixture forbids subprocess/statvfs/outbound connections')


subprocess.Popen = subprocess.run = reject_io
socket.socket.connect = socket.create_connection = reject_io
os.statvfs = reject_io
os.fstatvfs = reject_io
if not hasattr(os, 'O_NONBLOCK'):
    os.O_NONBLOCK = 0  # Only regular private TEMP files use this portable seam.

GIB = 1024 ** 3
real_mono = time.monotonic


class Controller:
    state_dir = fixture / 'data'

    def snapshot(self):
        return {'settings': {}, 'capabilities': {}, 'screen': {}, 'rgb': {}}


class Watch:
    """Fixed namespace event seam; production invalidation remains unchanged."""
    def __init__(self, state):
        self.state, self.closed = state, False
        self.observation = {'reason': 'unchanged', 'namespace_read': True, 'polled': True,
                            'namespace': 'mnt:[1234]', 'namespace_current': 'mnt:[1234]'}

    def changed(self):
        changed = self.state.mount_event
        if changed:
            self.state.mount_event = False
            self.observation['reason'] = 'mount_event'
        return changed

    def close(self):
        self.closed = True


class Insights:
    def __init__(self, state):
        self.state = state
        self.receipt = None

    def snapshot(self):
        rows = []
        for name in self.state.names:
            sleeping = name in self.state.standby
            rows.append({'id': name, 'device': '/dev/' + name,
                'model': 'Fixture-' + name, 'user_name': 'Drive-' + name,
                'size_bytes': 2 * 1024 ** 4, 'temp_c': None if sleeping else 34,
                'smart_available': True, 'smart_fresh': True,
                'smart_sampled_at': self.state.smart_stamp,
                'smart_status': 'standby' if sleeping else 'ok', 'standby': sleeping,
                'health': {'passed': None if sleeping else True}})
        return {'sampled_at': self.state.iso(), 'system': {'memory': {}, 'model': 'R1'},
                'interfaces': [], 'disks': {'available': True, 'items': rows},
                'device': {'identity': {'hostname': 'Capacity-Observation-Fixture'},
                    'storage': {'devices': [{'name': name, 'rotational': 1,
                                            'model': 'Fixture-' + name} for name in self.state.names]}}}

    def storage_identity_cached(self):
        return {'items': []}

    def smart_cached(self, name):
        assert name in self.state.names
        row = next(row for row in self.snapshot()['disks']['items'] if row['id'] == name)
        return {**row, 'smart_data': {}, 'smart_fresh': True}

    def _smart(self, *, authorizer, mode):
        assert authorizer() is True
        self.state.calls.append({'phase': 'smart', 'mode': mode})
        self.state.smart_stamp = self.state.wall
        value = {'ok': True, 'force': False, 'status': 'complete',
            'started_at': self.state.wall, 'finished_at': self.state.wall,
            'results': [{'device': name,
                         'status': 'skipped_standby' if name in self.state.standby else 'refreshed',
                         'reason': 'standby' if name in self.state.standby else None,
                         'sampled_at': None if name in self.state.standby else self.state.wall}
                        for name in self.state.names]}
        assert authorizer() is True
        self.receipt = value
        return value

    def refresh_smart_visible(self, *, authorizer):
        return self._smart(authorizer=authorizer, mode='refresh')

    def fill_smart_missing(self, *, authorizer):
        return self._smart(authorizer=authorizer, mode='fill')

    def capacity_visible_eligibility(self, receipt):
        assert receipt is self.receipt
        def eligible(name):
            assert receipt is self.receipt
            return 'standby' if name in self.state.standby else None
        return eligible

    def capacity_fill_eligibility(self, name):
        return 'standby' if name in self.state.standby else None


class MonitorProxy:
    def __init__(self, state):
        self.state = state

    def __getattr__(self, name):
        return getattr(self.state.monitor, name)

    def publish_capacity_history(self, receipt, *, authorizer):
        # Observe completion after calling the real production publisher. This
        # event synchronizes the browser with socket-write publication only;
        # it never creates or modifies any historical object itself.
        published = self.state.monitor.publish_capacity_history(receipt, authorizer=authorizer)
        self.state.calls.append({'phase': 'publish', 'published': published is True})
        self.state.published.set()
        return published


class State:
    def __init__(self):
        self.sequence, self.sampler, self.monitor = 0, None, None
        self.entered, self.release = threading.Event(), threading.Event()
        self.published = threading.Event()
        self.release.set()
        self.wall = time.time() - 600
        self.names = ('sda', 'sdb', 'sdc')
        self.configure('normal')

    def iso(self):
        return datetime.datetime.fromtimestamp(self.wall, datetime.timezone.utc).isoformat()

    def write(self, relative, value):
        target = self.folder / relative
        assert self.folder in target.parents
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(str(value), encoding='ascii')

    def configure(self, case):
        assert case in ('normal', 'multipart', 'zero', 'partial', 'duplicate')
        if self.sampler is not None:
            self.sampler.close_capacity()
        self.sequence += 1
        self.case = case
        self.folder = fixture / ('case_' + str(self.sequence) + '_' + case)
        self.folder.mkdir(mode=0o700)
        self.calls, self.measurements, self.descriptors = [], [], {}
        self.mount_event, self.hold_next = False, False
        self.standby = set()
        self.failed = {'sdb'} if case == 'partial' else set()
        self.release.set(); self.entered.clear()
        self.published.clear()
        self.wall += 60
        self.smart_stamp = self.wall
        self.specs = [dict(name='sda', partition=1, mount_id=101, device='8:1', total=100 * GIB, used=40 * GIB),
                      dict(name='sdb', partition=1, mount_id=201, device='8:17', total=400 * GIB, used=100 * GIB),
                      dict(name='sdc', partition=1, mount_id=301, device='8:33', total=800 * GIB, used=400 * GIB)]
        if case == 'multipart':
            self.specs.insert(1, dict(name='sda', partition=2, mount_id=102, device='8:2', total=200 * GIB, used=0))
        if case == 'duplicate':
            self.specs.insert(1, {**self.specs[0], 'mount_id': 102})
        if case == 'zero':
            for row in self.specs:
                row['used'] = 0
        for folder in ('proc/self', 'proc/net', 'sys/block', 'sys/class/hwmon', 'dev', 'run/udev', 'data', 'anchors'):
            (self.folder / folder).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write('proc/stat', 'cpu  10 0 10 80 0 0 0 0\n')
        self.write('proc/meminfo', 'MemTotal: 4096000 kB\nMemAvailable: 2048000 kB\n')
        self.write('proc/uptime', '10 10\n')
        self.write('proc/net/route', 'Iface Destination Gateway Flags RefCnt Use Metric Mask\n')
        for index, name in enumerate(self.names):
            self.write('sys/block/' + name + '/dev', '8:' + str(index * 16))
            self.write('sys/block/' + name + '/diskseq', '1')
            self.write('sys/block/' + name + '/queue/rotational', '1')
        for row in self.specs:
            name, partition = row['name'], str(row['partition'])
            self.write('sys/block/' + name + '/' + name + partition + '/dev', row['device'])
            self.write('sys/block/' + name + '/' + name + partition + '/partition', partition)
            self.write('anchors/link_' + row['device'].replace(':', '_'), 'metadata-only fixture link')
            self.write('anchors/mount_' + str(row['mount_id']), 'capacity worker handle fixture')
        self.write_mounts()
        self.sampler = service.StatusSampler(self.folder / 'proc', self.folder / 'sys')
        self.sampler._capacity_probe = self.probe
        self.monitor = service.StatusMonitor(self.sampler)
        self.tick()

    def write_mounts(self):
        self.write('proc/self/mountinfo', ''.join(
            f"{row['mount_id']} 1 {row['device']} / /drives/{row['name']}_{row['mount_id']} rw - ext4 /dev/{row['name']}{row['partition']} rw\n"
            for row in self.specs))

    def device(self, sys_root, device):
        assert Path(sys_root) == self.folder / 'sys'
        row = next((item for item in self.specs if item['device'] == device), None)
        if row is None:
            return None
        physical = self.folder / 'sys/block' / row['name']
        target = physical / (row['name'] + str(row['partition']))
        link = self.folder / 'anchors' / ('link_' + device.replace(':', '_'))
        def generation(path):
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_ctime_ns
        return (str(target), str(physical), generation(link), generation(target), generation(physical),
                (physical / 'dev').read_text().strip(), (physical / 'diskseq').read_text().strip(), row['name'])

    def probe(self, entry, deadline, authorized):
        authorized()
        row = next(row for row in self.specs if row['mount_id'] == entry['mount_id'])
        self.measurements.append({'mount_id': row['mount_id'], 'name': row['name'], 'wall': self.wall})
        if self.hold_next:
            self.hold_next = False
            self.entered.set()
            if not self.release.wait(4):
                raise AssertionError('pending capacity fixture was not released')
            authorized()
        if row['name'] in self.failed:
            raise service.CapacityRefreshError('probe_failed')
        descriptor = os.open(self.folder / 'anchors' / ('mount_' + str(row['mount_id'])), os.O_RDONLY)
        self.descriptors[descriptor] = row['mount_id']
        self.wall += 5
        return {'total_bytes': row['total'], 'used_bytes': row['used'],
                'usage_percent': row['used'] / row['total'] * 100}, descriptor

    def tick(self):
        value = self.sampler.sample()
        with self.monitor.lock:
            self.monitor.status, self.monitor.updated_at = value, real_mono()

    def stats(self):
        return {'case': self.case, 'wall': self.wall, 'calls': copy.deepcopy(self.calls),
                'measurements': copy.deepcopy(self.measurements), 'specs': self.specs,
                'capacity_cache_count': len(self.sampler._capacity_cache),
                'published_history_count': len(getattr(self.sampler, '_capacity_history', {})),
                'history_capability': callable(getattr(self.sampler, 'publish_capacity_history', None)),
                'forbidden': forbidden}


state = State()
# Fixed sysfs link/namespace seams; all normal inventory, cache, batch commit,
# history publication, monitor projection and HTTP owner checks remain real.
service._capacity_device = state.device
service._capacity_fd_mount = lambda descriptor, proc_root=None: state.descriptors.get(descriptor)
service._CapacityMountWatch = lambda proc_root: Watch(state)
service.time = types.SimpleNamespace(time=lambda: state.wall, monotonic=real_mono)
proxy, insights = MonitorProxy(state), Insights(state)
controller = Controller()
controller.state_dir.mkdir(mode=0o700, exist_ok=True)
os.environ['R1_WEB_AUTH_MODE'] = 'password'
os.environ['R1_DEV_NO_PAIRING'] = '0'

# This TEMP-only getter observes the actual live capacity buffer. It cannot set
# state, feed data, call an API or change a renderer, and is never in repo UI.
assets = fixture / 'assets'
assets.mkdir(mode=0o700)
built = (repo / 'ui/web/index.html').read_bytes().decode('utf-8')
needle = 'const sheetIs = '
assert built.count(needle) == 1
probe = ("Object.defineProperty(window,'__capacityObservationProbe',{get:()=>"
         "({liveDisk:JSON.parse(JSON.stringify(S.live.disk)),ts:S.o&&S.o.ts,"
         "validateHistory:value=>r1CapacityHistory(value)})});\n")
observed = built.replace(needle, probe + needle)
assert observed.replace(probe + needle, needle) == built
(assets / 'index.html').write_bytes(observed.encode('utf-8'))
(assets / 'login.html').write_bytes((repo / 'ui/web/login.html').read_bytes())
ui_evidence = {'source_sha256': hashlib.sha256(built.encode()).hexdigest(),
               'observed_sha256': hashlib.sha256(observed.encode()).hexdigest(),
               'replacement_count': 1, 'only_change': 'TEMP read-only capacity live-buffer getter'}
server = WebConfigServer(controller, insights=insights, monitor=proxy,
                         host='127.0.0.1', port=0, asset_dir=assets)
server.panel = FnmonitorPanel(controller, insights, None, None, None, proxy,
    state_dir=server.state_dir, web=server, proc_root=state.folder / 'proc',
    sys_root=state.folder / 'sys', dev_root=state.folder / 'dev', udev_root=state.folder / 'run/udev',
    wall_clock=lambda: state.wall,
    weather_fetcher=lambda city: {'ok': False, 'error': 'fixture_weather_unavailable'})


def auth_operation(mode):
    if mode == 'password':
        result = server.auth.setup('capacity-observation-fixture-password')
        if not result.get('ok'):
            result = server.auth.login('capacity-observation-fixture-password', '127.0.0.1')
        assert result.get('ok')
        server.auth_mode = 'password'
        return {'name': AUTH_COOKIE, 'value': result['sid']}
    if mode == 'pair':
        server.auth_mode = 'pair'
        token = 'p' * 43
        with server.lock:
            server.sessions[hashlib.sha256(token.encode()).digest()] = {'csrf': 'c' * 43, 'until': server.clock() + 120}
        return {'name': COOKIE, 'value': token}
    raise ValueError('unknown auth fixture mode')


try:
    for attempt in range(20):
        server.port = 20000 + secrets.randbelow(40000)
        try:
            server.start()
            break
        except OSError:
            if server.server is not None or attempt == 19:
                raise
    print(json.dumps({'ready': True, 'owner': 'http://127.0.0.1:' + str(server.port),
                      'ui_evidence': ui_evidence}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request['operation']
        reply = {}
        if operation.startswith('case:'):
            state.configure(operation.split(':', 1)[1])
            server.panel.proc_root, server.panel.sys_root = state.folder / 'proc', state.folder / 'sys'
            server.panel.dev_root, server.panel.udev_root = state.folder / 'dev', state.folder / 'run/udev'
            server.panel._mount_cache = (float('-inf'), {})
        elif operation == 'event':
            state.mount_event = True
        elif operation == 'frame':
            state.wall += 30
            state.tick()
        elif operation == 'standby_all':
            state.standby = set(state.names)
        elif operation == 'batch_two':
            state.standby = {'sda'}
            state.wall += 60
            for row in state.specs:
                if row['name'] in ('sdb', 'sdc'):
                    row['used'] += 20 * GIB
        elif operation == 'replace_a':
            state.write('sys/block/sda/diskseq', '2')
        elif operation == 'inventory_incomplete':
            state.write('proc/self/mountinfo', 'incomplete kernel fixture\n')
        elif operation in ('start', 'close'):
            getattr(state.sampler, operation + '_capacity')()
        elif operation.startswith('auth:'):
            reply['cookie'] = auth_operation(operation.split(':', 1)[1])
        elif operation == 'revoke':
            server.auth.revoke_others()
        elif operation == 'hold':
            state.hold_next = True
            state.entered.clear(); state.release.clear()
        elif operation == 'entered':
            reply['entered'] = state.entered.wait(3)
        elif operation == 'published':
            reply['published'] = state.published.wait(3)
        elif operation == 'reset_publish':
            state.published.clear()
        elif operation == 'release':
            state.release.set()
        elif operation != 'stats':
            raise ValueError('unknown fixed capacity observation operation')
        print(json.dumps({'id': request['id'], 'state': state.stats(), **reply}), flush=True)
finally:
    state.release.set()
    state.sampler.close_capacity()
    server.stop()
