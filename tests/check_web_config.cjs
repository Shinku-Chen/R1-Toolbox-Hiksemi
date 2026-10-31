// Real HTTP + real controller against temporary device fixtures only; never NAS hardware.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-web-browser-'));
const screenshotDir = process.env.R1_UI_SCREENSHOT_DIR;
if (screenshotDir) fs.mkdirSync(screenshotDir, { recursive: true });
const candidates = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ...String(process.env.PATH || '').split(path.delimiter).flatMap(folder => ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'].map(name => path.join(folder, name)))];
const chromeBin = candidates.find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件');

const pythonFixture = String.raw`
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

repo, fixture = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(repo / 'runtime'))
from toolbox import ToolboxController
from web_config import WebConfigServer
from fan_policy import FanPolicyController
from light_policy import LightPolicyController
from monitoring import MonitoringService
from device_info import temperature_display_role
import fnm_compat
import copy

# Only regular TEMP metadata files are read by this fixture. Windows has no
# O_NONBLOCK constant; keep real Monitoring sampling instead of a legacy fallback.
if os.name == 'nt' and not hasattr(os, 'O_NONBLOCK'):
    os.O_NONBLOCK = 0

# This browser suite verifies the explicitly requested legacy pairing workflow.
os.environ['R1_WEB_AUTH_MODE'] = 'pair'

assert fixture.name.startswith('r1-web-browser-') and fixture.is_dir()
registers = {1: 0x55, 0x90: 1, 0x91: 204}
commands = []
def runner(argv):
    # Every command is handled in memory; no subprocess is ever executed.
    assert argv[0] in ('fixture-i2cget', 'fixture-i2cset'), 'unexpected fixture command'
    commands.append(list(argv))
    if argv[0] == 'fixture-i2cget':
        return SimpleNamespace(returncode=0, stdout=hex(registers.get(int(argv[-2], 16), 0)), stderr='')
    registers[int(argv[-3], 16)] = int(argv[-2], 16)
    return SimpleNamespace(returncode=0, stdout='', stderr='')

backlight = fixture / 'sys/class/backlight/intel_backlight'
backlight.mkdir(parents=True)
(backlight / 'max_brightness').write_text('100')
(backlight / 'brightness').write_text('80')
blank = fixture / 'sys/class/graphics/fb0/blank'
blank.parent.mkdir(parents=True)
blank.write_text('0')
(fixture / 'dev').mkdir()
(fixture / 'dev/i2c-4').write_text('fixture, not a device')
(fixture / 'unsupported/sys').mkdir(parents=True)
(fixture / 'unsupported/dev').mkdir()
controllers = []
class BrowserToolboxController(ToolboxController):
    fail_settings = False
    def _save_settings(self, settings):
        if self.fail_settings:
            raise OSError('fixture settings write failed')
        try:
            return super()._save_settings(settings)
        except PermissionError as error:
            # Preserve real fixture JSON persistence, omitting only Windows'
            # unsupported directory-fsync open, as in BrowserFanPolicy below.
            if os.name != 'nt' or not error.filename or Path(error.filename).resolve() != self.state_dir.resolve():
                raise

def controller(folder):
    value = BrowserToolboxController(state_dir=folder / 'data', sys_root=folder / 'sys', dev_root=folder / 'dev', runner=runner)
    value.systemctl = None
    value.i2cget, value.i2cset = 'fixture-i2cget', 'fixture-i2cset'
    controllers.append(value)
    return value

board_stale = False
cpu_stale = False
cpu_temperature = 62
saver_extra_sources = False
missing_sources = set()
class Sources:
    def snapshot(self):
        now = time.monotonic()
        entries = [('cpu', 'CPU Package', 'cpu', cpu_temperature), ('board', '主板温度', 'board', 66), ('disk', '硬盘 A', 'disk', 47), ('disk2', '硬盘 B', 'disk', 39)]
        if saver_extra_sources:
            entries += [('extra:' + str(index), ('<img src=x onerror="window.saverLabelInjected=1">' + '超长温度设备名' * 20) if index == 0 else '扩展温度 ' + str(index), 'disk', 35) for index in range(12)]
        rows = []
        for name, label, kind, temperature in entries:
            if name in missing_sources:
                continue
            row = {'id': name, 'label': label, 'kind': kind, 'temperature_c': temperature, 'status': 'ok',
                   'sampled_monotonic': now - (30 if (name == 'board' and board_stale) or (name == 'cpu' and cpu_stale) else 0),
                   'max_age_seconds': 10, 'critical_celsius': 95 if kind == 'cpu' else None}
            if kind in ('cpu', 'board'):
                # Match TemperatureSources' display-only hwmon metadata while
                # keeping the existing IDs/kinds used by every control test.
                driver, display_label, channel = (('coretemp', 'Package', 'temp1') if kind == 'cpu'
                                                   else ('nct6775', 'SYSTIN', 'temp2'))
                role, evidence = temperature_display_role(driver, display_label)
                row.update(display_driver=driver, display_label=display_label,
                           display_channel=channel, display_source='hwmon')
            else:
                role, evidence = temperature_display_role('smart', '', 'smart')
            row.update(display_role=role, display_evidence=evidence)
            rows.append(row)
        return rows
class Adapter:
    def __init__(self, available):
        self.available, self.channels, self.targets = available, set(), {}
        self.fail_release = False
        self.starts, self.releases = 0, 0
        self.reason = "模拟设备：原厂风扇配置无法解析"
    def capabilities(self):
        return {'available': self.available, 'reason': '' if self.available else self.reason,
                'channels': ['cpu', 'system'] if self.available else [], 'rpm_available': False}
    def start(self, channels):
        assert self.available
        time.sleep(0.15)
        self.starts += 1
        self.channels = set(channels)
    def snapshot(self):
        return {'phase': 'active' if self.channels else 'released', 'active_channels': sorted(self.channels)}
    def write(self, targets):
        assert set(targets) <= self.channels and all(type(value) is int and 26 <= value <= 255 for value in targets.values())
        self.targets = dict(targets)
    def release(self):
        if self.fail_release:
            raise OSError("fixture restore not acknowledged")
        self.releases += 1
        self.channels, self.targets = set(), {}
policies = []
officials = {}

class Histor:
    """In-memory vendor system-fan modes; never opens a device or a socket."""
    def __init__(self):
        self.mode, self.available, self.calls = 'low', True, []
    def snapshot(self):
        return {'available': self.available, 'control_available': self.available, 'mode': self.mode,
                'scope': 'system_fan', 'cpu_policy': 'vendor_auto',
                'reason': '' if self.available else 'fixture: vendor control unavailable'}
    def verified_mode(self):
        return self.mode if self.available else None
    def set_mode(self, mode, confirmed=True):
        assert confirmed is True and self.available and mode in ('auto', 'low', 'medium', 'high')
        self.mode = mode
        self.calls.append(mode)
        return self.snapshot()

class BrowserFanPolicy(FanPolicyController):
    def _save(self, config, active_config, resume):
        try:
            return super()._save(config, active_config, resume)
        except PermissionError as error:
            # Windows cannot open a directory for fsync. The production code
            # already flushed/replaced the actual JSON file; tolerate only
            # that exact fixture-directory operation, never a file failure.
            if os.name != 'nt' or not error.filename or Path(error.filename).resolve() != self.state_dir.resolve():
                raise

def policy(folder, available):
    histor = officials.setdefault(str(folder), Histor())
    value = BrowserFanPolicy(folder / 'data', Sources(), Adapter(available), histor=histor, interval=0.1)
    policies.append(value)
    value.start()
    return value

class Monitor:
    def snapshot(self):
        return {'schema_version':1,'available':True,'hostname':'R1-Fixture','uptime_seconds':201600,
                'cpu':{'usage_percent':24.5,'temp_c':58},'memory':{'usage_percent':37.5,'used_bytes':3e9,'total_bytes':8e9},
                'network':{'rx_bytes_per_sec':1250000,'tx_bytes_per_sec':480000},
                'disks':[{'mount':'/drives/data','used_bytes':1e12,'total_bytes':4e12,'usage_percent':25}]}
class Insights:
    storage_identity_available = True
    def __init__(self):
        self.data={'system':{'model':'HIKSEMI R1','cpu_model':'Intel N100','cpu_logical_cores':4,'uptime_seconds':201600,
                     'cpu_usage_percent':24.5,'load_average':[.2,.3,.5],'memory':{'used_bytes':3e9,'total_bytes':8e9,'usage_percent':37.5},
                     'cpu_cores':[{'id':n,'usage_percent':10+n*5} for n in range(4)]},
                  'device':{'identity':{'hostname':'R1-Fixture','os':{'PRETTY_NAME':'Fixture Linux 12 (R1)','NAME':'Fixture Linux','VERSION_ID':'12'},'kernel':'6.1-fixture','firmware':{'version':'V6.3.3_Build_260907','build':'260907','model':'R1'}},
                    'cpu':{'physical_cores':4,'architecture':'x86_64'},'thermal':[{'driver':'coretemp','label':'Package','celsius':58}],
                    'network':[{'name':'eth0','speed_mbps':2500,'ipv6_addresses':[{'address':'2001:db8:1234:5678:90ab:cdef:1234:5678','prefix_length':64,'scope':'global'}]}],
                    'network_config':{'default_routes':{'ipv4':[{'interface':'eth0','gateway':'192.168.1.1','metric':100,'source':'/proc/net/route'}],'ipv6':[{'interface':'eth0','gateway':None,'metric':200,'source':'/proc/net/ipv6_route'}]},'route_status':{'ipv4':'ok','ipv6':'ok'},'dns':{'servers':['192.168.1.1','2001:4860:4860::8888'],'search':['home.arpa'],'domains':['home.arpa'],'status':'ok'}},'storage':{'raid':[]}},
                  'interfaces':[{'name':'eth0','ipv4':['192.168.1.10'],'mac':'00:11:22:33:44:55','operstate':'up','rx_bytes':400000000,'tx_bytes':100000000,'rx_bytes_per_sec':1250000,'tx_bytes_per_sec':480000}],
                  'disks':{'available':True,'items':[{'id':'sda','device':'/dev/sda','model':'Fixture NAS Drive','size_bytes':4e12,'temp_c':39,'standby':False,'smart_available':True,'health':{'passed':True},'user_name':'自定义 <img src=x onerror="window.diskNameInjected=1"> 数据盘','volume':'FixtureVolume','volume_label':'FixtureLabel','mounts':['/drives/FixtureVolume']},{'id':'sdb','device':'/dev/sdb','model':'Fixture No SMART','size_bytes':2e12,'temp_c':None,'standby':False,'smart_available':False,'health':{'passed':None,'reason':'SMART 未读取'}}]},
                  'containers':{'available':True,'items':[{'id':'a'*64,'name':'fixture-db','image':'fixture:local','state':'running','protected':False,'cpu_percent':3.2,'memory_bytes':134217728,'memory_limit_bytes':1073741824,'ports':'0.0.0.0:8080->8080/tcp, [::]:8080->8080/tcp'},{'id':'b'*64,'name':'r1-toolbox','image':'fixture:local','state':'exited','protected':True}]},
                  'vms':{'available':False,'reason':'fixture: no VM','items':[]},'ports':[{'protocol':'tcp','address':'127.0.0.1','port':18731}],
                  'processes':[{'pid':101,'name':'fixture-monitor','cpu_percent':1.2,'memory_bytes':40000000}],
                  'gpu':{'available':False,'reason':'fixture: no GPU','devices':[]},
                  'history':[{'cpu_usage_percent':v,'memory_usage_percent':37.5} for v in [18,22,28,21,24.5]]}
    def snapshot(self):return copy.deepcopy(self.data)
    def storage_identity_cached(self):
        # Fixed cache-only fixture: no SMART execution or vendor connection.
        if not self.storage_identity_available:
            raise RuntimeError('fixture: storage identity cache unavailable')
        return {'available': True, 'items': [{'id': 'sda', 'serial': 'FIXTURE-WEB-SN',
                'serial_status': 'ok', 'serial_sources': ['smart_identity_cache', 'hiksemi_storage_socket'],
                'matched': True}]}
    def smart_cached(self,name):
        if name!='sda':return None
        return {'smart_data':{'ata_smart_attributes':{'table':[{'id':5,'name':'Reallocated_Sector_Ct','value':100,'worst':100,'thresh':10,'when_failed':'-','raw':{'value':0,'string':'0'}}]},'power_on_time':{'hours':123},'power_cycle_count':4,'rotation_rate':7200}}
    def action(self,name,payload):
        assert name=='container.action' and payload.get('confirmed') is True
        item=next(n for n in self.data['containers']['items'] if n['id']==payload['id'])
        assert not item['protected']
        item['state']='exited' if payload['operation']=='stop' else 'running'
        item['available_actions']=['start'] if item['state']=='exited' else ['stop','restart']
        return {'ok':True}
class Lamp:
    def __init__(self, folder):self.state_dir=folder/'data';self.available=False;self.value={'mode':1,'color':'#4A9170','brightness':60};self.owner=None
    def lighting_probe(self):
        if not self.available:raise ValueError('fixture unavailable')
        return copy.deepcopy(self.value)
    def lighting_claim(self,owner):self.lighting_probe();self.owner=owner;return copy.deepcopy(self.value)
    def lighting_apply(self,owner,target,expected=None):
        assert self.owner is owner and (expected is None or expected==self.value)
        self.value=copy.deepcopy(target);return copy.deepcopy(self.value)
    def lighting_release(self,owner,original=None,expected=None):
        assert self.owner is None or self.owner is owner
        if original is not None and (expected is None or expected==self.value):self.value=copy.deepcopy(original)
        self.owner=None;return True
    def snapshot(self):return {'capabilities':{'rgb':{'available':self.available}}}
monitor,insights=Monitor(),Insights()
lamps=[Lamp(fixture),Lamp(fixture/'unsupported')]
lights=[LightPolicyController(lamp,Sources()) for lamp in lamps]
for light in lights:light.start()
monitoring=MonitoringService(fixture/'data',insights,monitor,Sources(),proc_root=fixture/'proc',sys_root=fixture/'sys')
monitoring.sample_once()  # Initialization failures must fail boot, not be hidden by legacy values.
monitoring.start()
fnm_compat.fetch_weather = lambda city: {'ok': True, 'city': 'Fixture City', 'temp': 21.5, 'feels': 20.0,
    'humidity': 55.0, 'wind': 2.5, 'code': 0, 'desc': '晴', 'is_day': 1}
primary = WebConfigServer(controller(fixture), insights=insights, fan_policy=policy(fixture, True), light_policy=lights[0],monitoring=monitoring,monitor=monitor, host='127.0.0.1', port=0, asset_dir=repo / 'ui/web', dev_root=fixture / 'dev', udev_root=fixture / 'udev').start()
unsupported = WebConfigServer(controller(fixture / 'unsupported'), fan_policy=policy(fixture / 'unsupported', False),light_policy=lights[1],monitor=monitor, host='127.0.0.1', port=0, asset_dir=repo / 'ui/web', dev_root=fixture / 'unsupported/dev', udev_root=fixture / 'unsupported/udev').start()

try:
    print(json.dumps({'ready': True, 'primary': {'origin': 'http://127.0.0.1:' + str(primary.port), 'code': primary.pairing_info(rotate=True)['code']},
                      'unsupported': {'origin': 'http://127.0.0.1:' + str(unsupported.port), 'code': unsupported.pairing_info(rotate=True)['code']}}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request['operation']
        if operation == 'verify_rgb':
            primary.controller._rgb_candidates=lambda: [{"bus":4,"name":"Fixture DesignWare","physical":"fixture-only","device":[0,0,0]}]
            primary.controller._probe_rgb()
            lamps[0].available=True
            lights[0]._probe()
        elif operation == 'reconstruct':
            primary.controller = controller(fixture)
            primary.panel.controller = primary.controller
            primary.fan_policy.stop()
            primary.fan_policy = policy(fixture, True)
            primary.panel.fan_policy = primary.fan_policy
        elif operation == 'saver_legacy_temperature':
            legacy = copy.deepcopy(primary.controller.settings)
            legacy.pop('saver_items', None)
            legacy.update(saver_content='temperature', saver_timeout=60)
            (primary.controller.state_dir / 'settings.json').write_text(json.dumps(legacy), encoding='utf-8')
            primary.controller = controller(fixture)
            primary.panel.controller = primary.controller
        elif operation == 'saver_extra_sources':
            saver_extra_sources = True
            primary.fan_policy.tick()
        elif operation == 'saver_disconnect_disk':
            missing_sources.add('disk')
            primary.fan_policy.tick()
        elif operation == 'storage_identity_unavailable':
            insights.storage_identity_available = False
        elif operation == 'storage_identity_available':
            insights.storage_identity_available = True
        elif operation == 'saver_fail_save':
            primary.controller.fail_settings = True
        elif operation == 'saver_allow_save':
            primary.controller.fail_settings = False
        elif operation == 'stale_board':
            board_stale = True
            primary.fan_policy.tick()
        elif operation == 'fresh_board':
            board_stale = False
            primary.fan_policy.tick()
        elif operation == 'stale_cpu':
            cpu_stale = True
            primary.fan_policy.tick()
        elif operation == 'fresh_cpu':
            cpu_stale = False
            primary.fan_policy.tick()
        elif operation == 'fan_cpu_temperature':
            assert type(request['value']) in (int, float) and 0 <= request['value'] < 80
            cpu_temperature = request['value']
            primary.fan_policy.tick()
        elif operation == 'fail_release':
            primary.fan_policy.adapter.fail_release = True
        elif operation == 'allow_release':
            primary.fan_policy.adapter.fail_release = False
        elif operation == 'fan_legacy_fixed':
            with primary.fan_policy.lock:
                primary.fan_policy.config['channels']['cpu'].update(mode='manual', manual_percent=75)
                primary.fan_policy.config['channels']['system'].update(mode='manual', manual_percent=100)
        elif operation == 'fan_legacy_other':
            with primary.fan_policy.lock:
                primary.fan_policy.config['channels']['cpu'].update(mode='manual', manual_percent=55)
                primary.fan_policy.config['channels']['system'].update(mode='manual', manual_percent=73)
        elif operation == 'fan_legacy_ten':
            with primary.fan_policy.lock:
                primary.fan_policy.config['channels']['cpu'].update(mode='manual', manual_percent=10)
        elif operation == 'unsupported_official_unknown':
            unsupported.fan_policy.histor.mode = None
            with unsupported.fan_policy.lock:
                unsupported.fan_policy.config['channels']['system'].pop('vendor_mode', None)
                unsupported.fan_policy.active_config['channels']['system'].pop('vendor_mode', None)
        elif operation == 'unsupported_official_unavailable':
            unsupported.fan_policy.histor.available = False
        elif operation == 'unsupported_official_auto':
            unsupported.fan_policy.histor.mode = 'auto'
            unsupported.fan_policy.histor.available = True
        elif operation == 'unsupported_recovery_unconfirmed':
            unsupported.fan_policy.adapter.fail_release = True
            result = unsupported.fan_policy.action('fan.policy.release', {'confirmed': True})
            assert result['ok'] is True
            deadline = time.monotonic() + 2
            while unsupported.fan_policy.snapshot()['busy'] and time.monotonic() < deadline:
                time.sleep(0.01)
        elif operation == 'unsupported_fan_ready':
            unsupported.fan_policy.adapter.available = True
            unsupported.fan_policy.tick()
        elif operation == 'unsupported_fan_busy':
            with unsupported.fan_policy.lock:
                unsupported.fan_policy.busy = True
        elif operation == 'unsupported_fan_idle':
            with unsupported.fan_policy.lock:
                unsupported.fan_policy.busy = False
            unsupported.fan_policy.tick()
        elif operation == 'fan_calls':
            pass
        elif operation == 'app_refresh':
            item = insights.data['containers']['items'][0]
            item['cpu_percent'] = round(item.get('cpu_percent', 0) + 1, 1)
        elif operation == 'app_paused':
            insights.data['containers']['items'][0].update(state='paused', available_actions=[])
        elif operation == 'app_running':
            insights.data['containers']['items'][0].update(state='running', available_actions=['stop','restart'])
        elif operation != 'stop':
            raise AssertionError('unknown fixture operation')
        print(json.dumps({'id': request['id'], 'ok': True, 'command_count': len(commands),
                          'fan_calls': {'starts': primary.fan_policy.adapter.starts,
                                        'releases': primary.fan_policy.adapter.releases,
                                        'official': len(primary.fan_policy.histor.calls)}}), flush=True)
        if operation == 'stop':
            break
finally:
    primary.stop()
    unsupported.stop()
    monitoring.stop()
    for light in lights:light.stop()
    for value in policies:
        value.adapter.fail_release = False
        value.stop()
    for value in controllers:
        value.stop()
`;

(async () => {
  const child = spawn(process.env.PYTHON_BIN || 'python3', ['-u', '-c', pythonFixture, root, fixtureDir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = '', bootResolve, bootReject, browser;
  const ready = new Promise((resolve, reject) => { bootResolve = resolve; bootReject = reject; });
  const fixtureReplies = new Map();
  let fixtureSequence = 0;
  const fixtureCall = (operation, value) => new Promise((resolve, reject) => {
    const id = ++fixtureSequence;
    const timer = setTimeout(() => { fixtureReplies.delete(id); reject(new Error('fixture operation timed out: ' + operation)); }, 8000);
    fixtureReplies.set(id, value => { clearTimeout(timer); resolve(value); });
    child.stdin.write(JSON.stringify({ id, operation, value }) + '\n');
  });
  const bootTimer = setTimeout(() => bootReject(new Error('本地测试网页服务启动超时')), 15000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-4096); });
  child.on('error', bootReject);
  child.on('exit', code => { if (code) bootReject(new Error('本地网页 fixture 退出：' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(bootTimer); bootResolve(value); }
    if (fixtureReplies.has(value.id)) { fixtureReplies.get(value.id)(value); fixtureReplies.delete(value.id); }
  });
  const checks = [], sizes = [], pageErrors = [], blocked = [];
  const started = Date.now();
  const check = (name, condition, detail) => {
    checks.push(name);
    assert.ok(condition, name + (detail ? '：' + detail : ''));
  };
  try {
    const fixture = await ready;
    const origin = fixture.primary.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url.startsWith(origin) || url.startsWith('data:')) return route.continue();
      blocked.push(url); return route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    let screenWrites = 0, smartRequests = 0;
    page.on('request', request => {
      if (request.url().endsWith('/api/r1/settings') && request.method() === 'POST') screenWrites += 1;
      if (request.url().includes('/api/smart?')) smartRequests += 1;
    });
    const fanResponses = [];
    page.on('response', async r => {
      if (!r.url().endsWith('/api/r1/fan')) return;
      let body = null; try { body = await r.json(); } catch (e) { body = null; }
      fanResponses.push({ status: r.status(), body, request: r.request().postDataJSON() });
    });
    const shot = async name => {
      if (!screenshotDir) return;
      await page.screenshot({ path: path.join(screenshotDir, name + '.png'), fullPage: false, animations: 'disabled' });
    };
    const api = async (pathname, options) => page.evaluate(async ({ pathname, options }) => {
      const r = await fetch(pathname, Object.assign({ cache: 'no-store', credentials: 'same-origin' }, options || {}));
      let body = null; try { body = await r.json(); } catch (e) { body = null; }
      return { status: r.status, body };
    }, { pathname, options });
    const post = (pathname, body) => api(pathname, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FNM-Request': '1' }, body: JSON.stringify(body || {}) });
    const noOverflow = async label => {
      const value = await page.evaluate(() => ({ width: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth }));
      sizes.push({ label, ...value });
      check('无横向溢出 · ' + label, value.body <= value.width + 1 && value.root <= value.width + 1, JSON.stringify(value));
    };
    const reloadFanSettings = async target => {
      await target.reload();
      await target.waitForSelector('#set-r1-fan .r1-edit[aria-busy="false"] [data-channel="system"] select');
    };

    /* 1. 面板加载 */
    await page.goto(origin + '/#/overview');
    await page.waitForFunction(() => document.getElementById('hostName') && document.getElementById('hostName').textContent === 'R1-Fixture', null, { timeout: 15000 });
    check('面板标题', (await page.title()).includes('R1 Toolbox'), await page.title());
    await page.waitForFunction(() => /Fixture City/.test(document.querySelector('[data-ref="wxCity"]')?.textContent || ''), null, { timeout: 10000 }).catch(() => {});
    check('天气服务已展示', /晴/.test(await page.locator('[data-ref="wxText"]').textContent()), await page.locator('[data-ref="wxText"]').textContent());
    check('天气位置已展示', /Fixture City/.test(await page.locator('[data-ref="wxCity"]').textContent()));
    const navLabels = await page.locator('#pageSeg button').allTextContents();
    check('网页六标签', JSON.stringify(navLabels) === JSON.stringify(['概览', '存储', '网络', '应用', '温度', '设置']), navLabels.join(','));
    check('总览模块已渲染', await page.locator('#layout-overview section[data-mod="metrics"]').count() === 1);
    check('不显示 CPU 功耗卡', await page.locator('#layout-overview [data-m="pwr"]').count() === 0);
    check('不显示显卡内容', !/显卡|GPU/.test(await page.locator('#layout-overview section[data-mod="compute"]').textContent()));
    check('指标卡有真实读数', (await page.locator('[data-m="cpu"] .m-val .dm').first().getAttribute('aria-label')) !== '--');
    check('R1 双路风扇模块在温度页', await page.locator('#layout-thermal section[data-mod="r1cool"]').count() === 1);
    check('概览页不再夹带风扇/灯光磁贴', await page.locator('#layout-overview section[data-mod="r1cool"], #layout-overview section[data-mod="r1light"]').count() === 0);
    check('R1 灯光模块在温度页', await page.locator('#layout-thermal section[data-mod="r1light"]').count() === 1);
    check('R1 小屏与电源模块在概览页', await page.locator('#layout-overview section[data-mod="r1screen"]').count() === 1);
    await page.locator('#pageSeg button[data-page="thermal"]').click();
    await page.waitForSelector('#layout-thermal section[data-mod="r1cool"] [data-ch="cpu"]', { timeout: 10000 });
    check('温度页不显示能耗模块', await page.locator('#layout-thermal section[data-mod="energy"]').count() === 0);
    check('双路风扇名称', /CPU 风扇/.test(await page.locator('#layout-thermal section[data-mod="r1cool"]').textContent()));
    check('系统状态显示实际原厂低档', /原厂 · 低档/.test(await page.locator('#layout-thermal [data-ch="system"]').textContent()));
    await page.locator('#pageSeg button[data-page="overview"]').click();
    await page.locator('section[data-mod="trends"]').scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    check('趋势模块已渲染', await page.locator('section[data-mod="trends"] .chart-card').count() >= 5);
    await shot('overview');

    /* 2. 数据映射 */
    const overview = await api('/api/overview');
    check('/api/overview 200', overview.status === 200);
    check('CPU 使用率映射', !!overview.body && !!overview.body.cpu && overview.body.cpu.percent === 24.5, JSON.stringify(overview.body && overview.body.cpu));
    check('内存映射', !!overview.body && !!overview.body.mem && overview.body.mem.percent === 37.5);
    check('硬盘温度映射', !!overview.body && !!overview.body.disks_detail && !!overview.body.disks_detail[0] && overview.body.disks_detail[0].temp === 39);
    check('温度来自同源 Monitoring hwmon 缓存', !!overview.body && !!overview.body.temp
      && overview.body.temp.cpu === 62 && overview.body.temp.system === 66 && overview.body.temp.mb === 66
      && overview.body.temp.source === 'monitoring_temperature_cache'
      && overview.body.temp.samples.cpu.fresh === true && overview.body.temp.samples.board.fresh === true
      && overview.body.temp.samples.cpu.sampled_at > 0 && overview.body.temp.samples.board.sampled_at > 0,
      JSON.stringify(overview.body && overview.body.temp));
    check('封装功耗缺失时不伪造', !!overview.body && !!overview.body.power && overview.body.power.ok === false && overview.body.power.total === null);
    // The production panel wraps internal const helpers in an IIFE. Exercise
    // their exact source with the real installed R1 provider, without exporting
    // production internals or copying the aggregate implementation into tests.
    const stateSource = fs.readFileSync(path.join(root, 'ui/panel-src/33-state.js'), 'utf8');
    const networkHelpers = ['VIRT', 'nets', 'sumRx', 'sumTx'].map(name => {
      const line = stateSource.split(/\r?\n/).find(value => value.startsWith('const ' + name + ' = '));
      assert.ok(line, '实际面板缺少网络派生函数：' + name); return line;
    }).join('\n');
    const networkMapping = await page.evaluate(helpers => {
      const derive = new Function('o', helpers + '\nreturn {rx: sumRx(o), tx: sumTx(o)};');
      const net = [{ iface: 'bond0', rx_rate: 1000, tx_rate: 80 },
        { iface: 'eth0', rx_rate: 600, tx_rate: 30 }, { iface: 'eth1', rx_rate: 400, tx_rate: 50 }];
      const aggregate = { schema_version: 1, source: 'monitoring_filtered_counters', rx_rate: 1000, tx_rate: 80 };
      return { ...derive({ net, net_aggregate: aggregate }),
        unknown: derive({ net, net_aggregate: { ...aggregate, rx_rate: null } }).rx,
        invalid: derive({ net, net_aggregate: { ...aggregate, tx_rate: -1 } }).tx,
        legacy: derive({ net }).rx, unrelated: derive({ net, net_aggregate: { ...aggregate, source: 'other' } }).rx,
        rows: net.map(row => [row.iface, row.rx_rate, row.tx_rate]) };
    }, networkHelpers);
    check('R1网络汇总不重复相加bond和物理网口', networkMapping.rx === 1000 && networkMapping.tx === 80);
    check('网络未知汇总保持缺测而不是假零', networkMapping.unknown === null && networkMapping.invalid === null);
    check('无R1元数据的上游页面保留原汇总方式', networkMapping.legacy === 2000 && networkMapping.unrelated === 2000);
    check('汇总适配保留真实接口标识和各自读数', JSON.stringify(networkMapping.rows) === '[["bond0",1000,80],["eth0",600,30],["eth1",400,50]]');
    const networkPage = await context.newPage();
    const networkAggregate = { schema_version: 1, source: 'monitoring_filtered_counters', rx_rate: 1048576, tx_rate: 2097152 };
    try {
      await networkPage.route('**/api/overview', route => route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ...overview.body,
          net: [{ iface: 'bond0', rx_rate: 1048576, tx_rate: 2097152 },
                { iface: 'eth0', rx_rate: 524288, tx_rate: 1048576 },
                { iface: 'eth1', rx_rate: 524288, tx_rate: 1048576 }],
          net_aggregate: networkAggregate }) }));
      await networkPage.goto(origin + '/#/overview');
      await networkPage.locator('[data-mod="metrics"] [data-ref="rxV"][aria-label="1.0"]').waitFor();
      await networkPage.locator('[data-mod="metrics"] [data-ref="txV"][aria-label="2.0"]').waitFor();
      check('真实网络指标卡显示独立汇总而不是bond与slave双倍值',
        await networkPage.locator('[data-mod="metrics"] [data-ref="rxU"]').innerText() === 'MB/s'
        && await networkPage.locator('[data-mod="metrics"] [data-ref="txU"]').innerText() === 'MB/s');
      networkAggregate.rx_rate = null; networkAggregate.tx_rate = null;
      await networkPage.reload();
      await networkPage.locator('[data-mod="metrics"] [data-ref="rxV"][aria-label="--"]').waitFor();
      await networkPage.locator('[data-mod="metrics"] [data-ref="txV"][aria-label="--"]').waitFor();
      check('真实网络指标卡缺测显示未知而非假空闲',
        await networkPage.locator('[data-mod="metrics"] [data-ref="netSt"]').innerText() === '未知');
      networkAggregate.rx_rate = 0; networkAggregate.tx_rate = 0;
      await networkPage.reload();
      await networkPage.locator('[data-mod="metrics"] [data-ref="rxV"][aria-label="0"]').waitFor();
      await networkPage.locator('[data-mod="metrics"] [data-ref="txV"][aria-label="0"]').waitFor();
      check('真实零流量仍显示空闲并与缺测分开',
        await networkPage.locator('[data-mod="metrics"] [data-ref="netSt"]').innerText() === '空闲'
        && await networkPage.locator('[data-mod="metrics"] [data-ref="rxU"]').innerText() === 'B/s');
    } finally { await networkPage.close(); }
    const memoryPage = await context.newPage();
    const memorySample = { ...overview.body.mem, available: 5e9, cached: null, buffers: null, free: null,
      breakdown_available: false, swap_total: null, swap_used: null, swap_percent: null };
    try {
      await memoryPage.route('**/api/overview', route => route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ...overview.body, mem: memorySample }) }));
      await memoryPage.goto(origin + '/#/overview');
      const compute = memoryPage.locator('[data-mod="compute"]');
      await compute.scrollIntoViewIfNeeded();
      await compute.locator('[data-memory-breakdown]').waitFor();
      const missingLegend = (await compute.locator('[data-ref="mLeg"]').innerText()).replace(/\s+/g, ' ');
      check('真实概览内存构成缺测不填应用缓存缓冲空闲零值', ['应用', '缓存', '缓冲', '空闲'].every(label => missingLegend.includes(label + ' -- GB'))
        && await compute.locator('[data-ref="dimm"]').isHidden(), missingLegend);
      check('内存构成缺测保留已用可用总量而不宣传零缓存可释放',
        await compute.locator('[data-ref="mUsed"]').getAttribute('aria-label') !== '--'
        && (await compute.locator('.mod-dek').innerText()).includes('可用')
        && !(await compute.locator('.mod-dek').innerText()).includes('缓存可随时释放'));
      check('概览交换分区缺测显示未知而非未启用', await compute.locator('[data-ref="swap"]').innerText() === '未知');
      await compute.locator('[data-sheet="mem"]').click();
      await memoryPage.locator('#sheet.on').waitFor();
      const missingDetailLegend = (await memoryPage.locator('#sheet [data-ref="leg"]').innerText()).replace(/\s+/g, ' ');
      check('真实内存详情隐藏缺测构成图且不把available倒算为空闲',
        await memoryPage.locator('#sheet [data-ref="st"]').isHidden()
        && ['应用', '缓存', '缓冲', '空闲'].every(label => missingDetailLegend.includes(label + ' -- GB'))
        && (await memoryPage.locator('#sheet [data-ref="compositionNote"]').innerText()).includes('不按可用内存倒算'));
      check('内存详情主用量保留且交换分区缺测显示未知',
        !(await memoryPage.locator('#sheet [data-ref="avail"]').innerText()).includes('--')
        && (await memoryPage.locator('#sheet [data-ref="swap"]').innerText()).includes('未知')
        && !(await memoryPage.locator('#sheet [data-ref="swap"]').innerText()).includes('未启用'));
      await memoryPage.keyboard.press('Escape');
      Object.assign(memorySample, { cached: 0, buffers: 0, free: memorySample.total - memorySample.used,
        swap_total: 0, swap_used: 0 });
      delete memorySample.breakdown_available;
      await memoryPage.locator('#btnRefresh').click();
      await compute.scrollIntoViewIfNeeded();
      await compute.locator('[data-ref="dimm"]').waitFor({ state: 'visible' });
      const knownLegend = (await compute.locator('[data-ref="mLeg"]').innerText()).replace(/\s+/g, ' ');
      check('完整上游构成中真实零缓存缓冲不误判缺测', knownLegend.includes('缓存 0.0 GB') && knownLegend.includes('缓冲 0.0 GB')
        && !knownLegend.includes('--') && await compute.locator('[data-memory-breakdown]').count() === 0);
      check('概览交换分区真实零仍显示未启用', await compute.locator('[data-ref="swap"]').innerText() === '未启用');
      await compute.locator('[data-sheet="mem"]').click();
      await memoryPage.locator('#sheet.on [data-ref="st"]').waitFor({ state: 'visible' });
      const knownDetailLegend = (await memoryPage.locator('#sheet [data-ref="leg"]').innerText()).replace(/\s+/g, ' ');
      check('完整上游内存详情保留构成图和真实零值', knownDetailLegend.includes('缓存 0.0 GB') && knownDetailLegend.includes('缓冲 0.0 GB')
        && !knownDetailLegend.includes('--') && (await memoryPage.locator('#sheet [data-ref="swap"]').innerText()).includes('未启用交换分区'));
      Object.assign(memorySample, { cached: null, buffers: null, free: null, breakdown_available: false, swap_total: null });
      await memoryPage.locator('#sheet [data-memory-breakdown]').waitFor();
      check('已知构成转为缺测时清除旧图例和旧段值',
        await memoryPage.locator('#sheet [data-ref="st"]').isHidden()
        && await memoryPage.locator('#sheet [data-ref="st"] > i').evaluateAll(nodes => nodes.every(node => !node.style.flexGrow))
        && !(await memoryPage.locator('#sheet [data-ref="leg"]').innerText()).includes('0.0 GB')
        && await compute.locator('[data-ref="dimm"] .seg').evaluateAll(nodes => nodes.every(node => node.getAttribute('width') === '0')));
      Object.assign(memorySample, { cached: 0, buffers: 0, free: memorySample.total - memorySample.used, swap_total: 0 });
      delete memorySample.breakdown_available;
      await memoryPage.locator('#sheet [data-ref="st"]').waitFor({ state: 'visible' });
      check('缺测后恢复完整构成时图表与真实零值恢复',
        !(await memoryPage.locator('#sheet [data-ref="leg"]').innerText()).includes('--')
        && await memoryPage.locator('#sheet [data-memory-breakdown]').count() === 0
        && await compute.locator('[data-ref="dimm"]').isVisible());
      await memoryPage.keyboard.press('Escape');
      Object.assign(memorySample, { used: null, cached: null, buffers: null, free: null, breakdown_available: false });
      await memoryPage.locator('#btnRefresh').click();
      await compute.scrollIntoViewIfNeeded();
      await compute.locator('[data-ref="mUsed"][aria-label="--"]').waitFor();
      check('已知内存总量但已用缺测时主读数不伪造零', (await compute.locator('[data-ref="mTot"]').innerText()).includes('GB 已用'));
      Object.assign(memorySample, { used: 0, percent: 0, available: memorySample.total,
        cached: 0, buffers: 0, free: memorySample.total });
      delete memorySample.breakdown_available;
      await memoryPage.locator('#btnRefresh').click();
      await compute.locator('[data-ref="mUsed"][aria-label="0.0"]').waitFor();
      check('内存已用真实零值保留并恢复完整构成', await compute.locator('[data-ref="dimm"]').isVisible()
        && (await compute.locator('[data-ref="mLeg"]').innerText()).includes('0.0 GB'));
    } finally { await memoryPage.close(); }
    const system = await api('/api/system');
    check('固件版本来自官方元数据', !!system.body && system.body.fnos_version === 'V6.3.3_Build_260907');
    const sensors = await api('/api/sensors');
    check('温度传感器接口', !!sensors.body && sensors.body.available === true && sensors.body.temps.length >= 1
      && sensors.body.temps.some(row => row.driver === 'coretemp' && row.label === 'Package'
        && row.display_role === 'cpu' && row.value === 62 && row.temp === 62 && row.crit === 95 && row.fresh === true)
      && sensors.body.temps.some(row => row.driver === 'nct6775' && row.label === 'SYSTIN'
        && row.display_role === 'board' && row.value === 66 && row.temp === 66 && row.fresh === true),
      JSON.stringify(sensors.body));
    const docker = await api('/api/docker');
    check('容器列表映射', !!docker.body && docker.body.available === true && docker.body.containers.length === 2);
    const health = await api('/api/disk_health');
    check('盘位分级来自后台 SMART', !!health.body && !!health.body.items && !!health.body.items.sda && health.body.items.sda.grade === 'ok');
    const traffic = await api('/api/traffic');
    check('流量接口', !!traffic.body && !!traffic.body.totals && traffic.body.totals.basis === 'counter_deltas');

    /* 2b. 各详情抽屉与交互不产生脚本错误 */
    for (const sheet of ['cpu', 'mem', 'storage', 'net', 'temp']) {
      await page.locator(`[data-m="${sheet === 'storage' ? 'disk' : sheet}"]`).click();
      await page.waitForSelector('#sheet.on', { timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(150);
      await page.locator('#sheetClose').click();
    }
    await page.locator('.ring-wrap').click();
    await page.waitForTimeout(150);
    await page.locator('#sheetClose').click();
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await page.waitForSelector('#layout-storage section[data-mod="storage"] .bay', { timeout: 8000 });
    check('存储页列出无 SMART 的物理盘', await page.locator('#layout-storage .bay').count() === 2);
    const customDiskName = '自定义 <img src=x onerror="window.diskNameInjected=1"> 数据盘';
    await page.waitForFunction(expected => [...document.querySelectorAll('#layout-storage .bay-top > span:first-child')].some(node => node.textContent === expected), customDiskName, { timeout: 10000 }).catch(() => {});
    const namedDisk = page.locator('#layout-storage .bay[data-sheet="disk/sda"]');
    check('物理盘卡片优先自定义名而非卷标或卷目录', await namedDisk.locator('.bay-top > span').first().textContent() === customDiskName, await namedDisk.textContent());
    check('自定义盘名在卡片中按文本转义', await namedDisk.locator('img').count() === 0 && await page.evaluate(() => window.diskNameInjected) === undefined);
    await page.locator('#layout-storage section[data-mod="storage"] .bay').first().click();
    await page.waitForSelector('#sheetBody', { timeout: 8000 });
    await page.waitForTimeout(1000);
    await page.waitForFunction(() => /Reallocated_Sector_Ct/.test(document.querySelector('#sheetBody')?.textContent || ''), null, { timeout: 8000 }).catch(() => {});
    check('硬盘详情显示完整 SMART 属性', /Reallocated_Sector_Ct/.test(await page.locator('#sheetBody').textContent()));
    check('硬盘详情标题优先自定义名', await page.locator('#sheetTitle').textContent() === customDiskName + ' · sda');
    check('自定义盘名在详情中按文本转义', /自定义 <img src=x onerror="window.diskNameInjected=1"> 数据盘/.test(await page.locator('#sheetBody').textContent()) && await page.locator('#sheetBody img, #sheetTitle img').count() === 0 && await page.evaluate(() => window.diskNameInjected) === undefined);
    await page.locator('#sheetClose').click();
    await page.locator('#pageSeg button[data-page="overview"]').click();
    await page.locator('#btnSearch').click();
    await page.locator('#palInput').fill('cpu');
    await page.waitForTimeout(150);
    await page.keyboard.press('Escape');
    await page.locator('#btnEdit').click();
    await page.locator('#editDone').click();
    check('详情抽屉与搜索无脚本错误', pageErrors.length === 0, pageErrors.join(' | '));

    /* 3. 公开只读 API 与授权边界 */
    const publicStatus = await page.request.get(origin + '/api/v1/status');
    check('公开状态接口无需配对', publicStatus.status() === 200);
    check('公开接口允许第三方读取', publicStatus.headers()['access-control-allow-origin'] === '*');
    check('未配对时会话接口 401', (await page.request.get(origin + '/api/session')).status() === 401);
    const before = await api('/api/r1');
    check('未配对时报告未授权', !!before.body && !!before.body.auth && before.body.auth.authorized === false);
    const denied = await post('/api/r1/fan/spoof', { config: {} });
    check('未知写接口被拒绝', [401, 403, 404].includes(denied.status), String(denied.status));

    /* 4. 主题与界面设置写入 */
    const beforeTheme = await page.locator('html').getAttribute('data-theme');
    await page.locator('#btnTheme').click();
    await page.waitForFunction(value => document.documentElement.dataset.theme !== value, beforeTheme);
    await page.waitForTimeout(1200);
    const savedUI = await api('/api/ui');
    check('外观保存到 NAS', !!savedUI.body && !!savedUI.body.ui && savedUI.body.ui.theme === await page.locator('html').getAttribute('data-theme'));

    /* 5. 响应式 */
    for (const width of [1440, 1120, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await noOverflow('总览 ' + width);
    }
    await page.setViewportSize({ width: 1120, height: 1000 });
    await page.locator('#pageSeg button[data-page="apps"]').click();
    await page.waitForSelector('#page-apps .ctr[data-name="fixture-db"]', { timeout: 15000 });
    check('应用页显示 fixture 容器', /fixture-db/.test(await page.locator('#page-apps').textContent()));
    await page.locator('#page-apps .ctr[data-name="fixture-db"]').click();
    await page.waitForTimeout(200);
    check('容器详情显示资源与端口', /fixture:local/.test(await page.locator('#sheetBody').textContent()));
    await page.locator('#sheetClose').click();
    await noOverflow('应用');
    await page.setViewportSize({ width: 390, height: 844 });
    await noOverflow('应用 390');

    /* 5b. 六个页面均可独立渲染，且不横向溢出 */
    await page.setViewportSize({ width: 1120, height: 1000 });
    for (const [pageName, moduleId] of [['storage', 'storage'], ['network', 'network'], ['thermal', 'thermal']]) {
      await page.locator(`#pageSeg button[data-page="${pageName}"]`).click();
      await page.waitForSelector(`#layout-${pageName} section[data-mod="${moduleId}"]`, { timeout: 10000 });
      check(pageName + ' 页版块已渲染', await page.locator(`#layout-${pageName} section[data-mod="${moduleId}"]`).count() === 1);
      await noOverflow(pageName);
      await shot(pageName);
    }
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await page.setViewportSize({ width: 320, height: 844 });
    await noOverflow('存储 320');
    await shot('storage-320');
    await page.locator('#bottomNav button[data-page="thermal"]').click();
    await noOverflow('温度 320');
    await shot('thermal-320');

    /* 6. 设置页与配对解锁 */
    await page.setViewportSize({ width: 1120, height: 1000 });
    await page.locator('#pageSeg button[data-page="settings"]').click();
    await page.waitForFunction(() => document.getElementById('set-r1-fan'));
    for (const id of ['r1-fan', 'r1-light', 'r1-screen', 'r1-monitor', 'look', 'weather', 'r1-auth', 'r1-conn', 'about']) {
      check('设置分组 ' + id, await page.locator('#set-' + id).count() === 1);
    }
    const lightCard = page.locator('#set-r1-light');
    const lightOptions = await lightCard.locator('.r1-editor select').first().locator('option').allTextContents();
    check('灯光只保留四种原生模式', JSON.stringify(lightOptions) === JSON.stringify(['关闭', '常亮', '彩虹', '呼吸']), lightOptions.join(','));
    check('网页面板不提供温度联动编辑', !/温度联动|温度设备|颜色节点/.test(await lightCard.textContent()));
    const fanCard = page.locator('#set-r1-fan');
    const cpuEditor = fanCard.locator('[data-channel="cpu"]');
    const systemEditor = fanCard.locator('[data-channel="system"]');
    const cpuModes = cpuEditor.locator('select').first();
    const systemModes = systemEditor.locator('select').first();
    check('CPU 固定档上移并保持五档顺序', JSON.stringify(await cpuModes.locator('option').allTextContents()) === JSON.stringify(['自动', '高档', '中档', '低档', '手动']));
    check('系统保留原厂四档与手动顺序', JSON.stringify(await systemModes.locator('option').allTextContents()) === JSON.stringify(['自动', '高档', '中档', '低档', '手动']));
    check('旧原厂配置使用实际低档初始化', await systemModes.inputValue() === 'low');
    await fixtureCall('fan_legacy_fixed');
    await reloadFanSettings(page);
    check('CPU 旧固定 75% 无损映射中档', await cpuModes.inputValue() === 'medium' && await cpuEditor.locator('.r1-fixed').textContent() === '75%');
    check('旧系统固定 100% 明确独立而非原厂高档', await systemModes.inputValue() === 'legacy:100' && /独立固定 100%（旧配置）/.test(await systemModes.textContent()) && await systemEditor.locator('.r1-fixed').textContent() === '100%');
    check('旧固定输出尚未接管时不显示已生效', /固定输出未生效/.test(await cpuEditor.locator('[data-ref="state"]').textContent()));
    check('固定输出不显示设备与曲线', await cpuEditor.locator('.r1-ms, .r1-curve').count() === 0);
    await cpuModes.selectOption('manual');
    check('手动直接进入曲线且没有控制类型选择', await cpuEditor.locator('select').count() === 1);
    check('温控曲线显示设备多选', await cpuEditor.locator('.r1-ms').count() === 1 && await fanCard.locator('.r1-sources').count() === 0);
    check('手动温控显示曲线', await cpuEditor.locator('.r1-curve').count() === 1);
    check('手动温控只显示低温和高温两个阈值', JSON.stringify(await cpuEditor.locator('[data-ref="rows"] input').evaluateAll(inputs => inputs.map(input => input.getAttribute('aria-label')))) === JSON.stringify(['低温℃', '高温℃']));
    check('三档输出仍各自显示且不再描述隐式迟滞', await cpuEditor.locator('[data-ref="duty"] input').count() === 3 && !/迟滞|降温 3/.test(await cpuEditor.textContent()));
    check('设备多选默认收起', await fanCard.locator('.r1-ms-menu').isHidden());
    await fanCard.locator('.r1-ms-toggle').first().click();
    check('设备下拉可展开', await fanCard.locator('.r1-ms-menu').first().isVisible() && await fanCard.locator('.r1-ms-row').count() >= 1);
    await fanCard.locator('.r1-ms-row input').first().check();
    await shot('fan-manual');
    await page.keyboard.press('Escape');
    await cpuModes.selectOption('auto');
    check('CPU 自动不显示设备与曲线', await fanCard.locator('.r1-ms').count() === 0 && await fanCard.locator('.r1-curve').count() === 0);
    await cpuModes.selectOption('manual');
    check('切回手动保留设备多选', await fanCard.locator('.r1-ms').count() === 1);
    await systemModes.selectOption('manual');
    check('系统手动直接显示温控曲线', await systemEditor.locator('select').count() === 1 && await systemEditor.locator('.r1-curve, .r1-ms').count() === 2);
    await systemModes.selectOption('low');
    await systemModes.selectOption('manual');
    check('系统切回手动仍直接编辑曲线', await systemEditor.locator('select').count() === 1 && await systemEditor.locator('.r1-curve').count() === 1 && await systemEditor.locator('.r1-fixed').count() === 0);
    await systemModes.selectOption('medium');
    check('系统原厂档位不显示手动编辑器', await systemEditor.locator('.r1-ms, .r1-curve, input[type="number"]').count() === 0);
    check('风扇只保留保存按钮并移除专门复位', await fanCard.locator('.set-actions button').count() === 1 && await fanCard.locator('[data-ref="save"]').count() === 1 && await fanCard.locator('[data-ref="release"]').count() === 0);
    await fanCard.locator('[data-ref="save"]').click();
    await page.waitForSelector('#r1Pair', { timeout: 8000 });
    check('风扇点击保存直接申请写入且无确认弹窗', await page.locator('#r1AskT').count() === 0);
    check('未配对时写入要求五位配对码', true);
    await page.locator('#r1PairCode').fill(fixture.primary.code);
    await page.locator('#r1Pair [data-go]').click();
    await page.waitForFunction(() => !document.getElementById('r1Pair'), null, { timeout: 8000 });
    await page.waitForTimeout(400);
    check('配对后会话可用', (await page.request.get(origin + '/api/session')).status() === 200);
    // 保存按钮在配对成功后自动重试一次，不再有第二个“保存并应用”。
    await page.waitForTimeout(1500);
    const fanRecord = fanResponses[fanResponses.length - 1];
    check('配对后风扇写入被授权', !!fanRecord && ![401, 403].includes(fanRecord.status), JSON.stringify(fanRecord && fanRecord.status));
    check('风扇写入由真实温控服务处理', !!fanRecord && !!fanRecord.body && !!fanRecord.body.fan_policy,
      JSON.stringify(fanRecord && fanRecord.body && fanRecord.body.error));
    check('风扇设置成功提交到协调控制器', fanRecord.status === 200 && fanRecord.body.ok === true, JSON.stringify(fanRecord.body.error));
    check('直接保存仍携带后端明确应用标志', fanRecord.request.apply === true && fanRecord.request.confirmed === true);
    if (fanRecord && fanRecord.status === 200) {
      check('风扇配置已写入', fanRecord.body.fan_policy.config.channels.cpu.mode === 'temperature' && fanRecord.body.fan_policy.config.channels.cpu.sources.length === 1);
      check('系统保存 vendor_mode 而非固定百分比', fanRecord.body.fan_policy.config.channels.system.mode === 'vendor' && fanRecord.body.fan_policy.config.channels.system.vendor_mode === 'medium');
    }
    const fanState = await api('/api/r1');
    check('温控状态接口可读', !!fanState.body && !!fanState.body.fan && !!fanState.body.fan.capability);
    check('CPU 手动与系统原厂中档同时生效', fanState.body.fan.channels.cpu.active === true && fanState.body.fan.channels.system.active === false && fanState.body.fan.official.mode === 'medium', JSON.stringify({ channels: fanState.body.fan.channels, official: fanState.body.fan.official, last_error: fanState.body.fan.last_error }));
    /* 所有者详情从已核验身份缓存显示 SN；缓存失效不能沿用旧值。 */
    const waitDiskSerial = async expected => {
      await page.waitForFunction(expected => {
        const row = [...document.querySelectorAll('#sheetBody [data-ref="info"] .kv > div')].find(node => node.querySelector('dt')?.textContent === '序列号');
        const value = row?.querySelector('dd')?.textContent.trim() || '';
        return expected === null ? /^(?:--|—|未知|未读取到)$/.test(value) : value === expected;
      }, expected, { timeout: 8000 });
      return (await page.locator('#sheetBody [data-ref="info"] .kv > div').filter({ has: page.locator('dt', { hasText: '序列号' }) }).locator('dd').textContent()).trim();
    };
    const refreshStorage = async () => {
      const refreshed = page.waitForResponse(response => response.url() === origin + '/api/overview' && response.request().method() === 'GET');
      await page.locator('#btnRefresh').click();
      check('存储刷新读取成功', (await refreshed).status() === 200);
    };
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await refreshStorage();
    await namedDisk.click();
    check('所有者硬盘详情实际显示已核验缓存 SN', await waitDiskSerial('FIXTURE-WEB-SN') === 'FIXTURE-WEB-SN');
    await page.locator('#sheetClose').click();
    await fixtureCall('storage_identity_unavailable');
    await refreshStorage();
    await namedDisk.click();
    const missingSerial = await waitDiskSerial(null);
    check('身份缓存不可用时 SN 显示未知且不保留旧值', /^(?:--|—|未知|未读取到)$/.test(missingSerial) && !(await page.locator('#sheetBody').textContent()).includes('FIXTURE-WEB-SN'));
    await page.locator('#sheetClose').click();
    await fixtureCall('storage_identity_available');
    await page.locator('#pageSeg button[data-page="settings"]').click();
    await reloadFanSettings(page);
    check('编辑器显示手动已接管与实际中档', /手动接管中/.test(await cpuEditor.textContent()) && /原厂 · 中档/.test(await systemEditor.textContent()));
    const saveFanDirect = async label => {
      const selectedModes = { cpu: await cpuModes.inputValue(), system: await systemModes.inputValue() };
      const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/r1/fan') && response.request().method() === 'POST');
      await fanCard.locator('[data-ref="save"]').click();
      const response = await responsePromise;
      const body = await response.json();
      const payload = response.request().postDataJSON();
      check(label + '直接保存并应用', response.status() === 200 && body.ok === true && payload.apply === true && payload.confirmed === true && await page.locator('#r1AskT').count() === 0, JSON.stringify(body.error));
      check(label + '提交双阈值第二版配置', payload.config.schema_version === 2 && Object.values(payload.config.channels).every(item => Number.isInteger(item.low_temp) && Number.isInteger(item.high_temp) && item.low_temp < item.high_temp && !['quiet_below', 'normal_at', 'full_at'].some(key => Object.hasOwn(item, key))));
      for (const name of ['cpu', 'system']) {
        const selected = selectedModes[name], submitted = payload.config.channels[name];
        const expected = selected === 'manual' ? 'temperature' : selected.startsWith('legacy:') || (name === 'cpu' && ['high', 'medium', 'low'].includes(selected)) ? 'manual' : 'vendor';
        check(label + name + '提交当前显示的模式', submitted.mode === expected, JSON.stringify({ selected, submitted, body: body.fan_policy }));
      }
      // Poll awaited HTTP snapshots in Node. An async waitForFunction predicate
      // is a truthy Promise in this Playwright version, even when it resolves
      // false, and therefore cannot establish the actual terminal state.
      const deadline = Date.now() + 8000;
      let actual, applied = false;
      while (Date.now() < deadline) {
        actual = (await api('/api/r1')).body.fan;
        applied = !actual.busy && Object.entries(payload.config.channels).every(([name, item]) => {
          const live = actual.channels[name];
          return live.mode === item.mode && live.active === (item.mode !== 'vendor') && (item.mode !== 'manual' || live.target_percent === item.manual_percent);
        });
        if (applied) break;
        await page.waitForTimeout(50);
      }
      check(label + '运行模式与输出实际生效', applied, JSON.stringify({ requested: payload.config.channels, actual }));
      await reloadFanSettings(page);
      return (await api('/api/r1')).body.fan;
    };
    await fixtureCall('fan_legacy_other');
    await reloadFanSettings(page);
    check('CPU 非标准旧值 55% 不偷偷映射低档', await cpuModes.inputValue() === 'legacy:55' && /独立固定 55%（旧配置）/.test(await cpuModes.textContent()) && await cpuEditor.locator('.r1-fixed').textContent() === '55%');
    check('系统旧固定 73% 不转换成原厂档', await systemModes.inputValue() === 'legacy:73' && /独立固定 73%（旧配置）/.test(await systemModes.textContent()));
    const keptLegacy = await saveFanDirect('旧配置');
    check('未选择新模式时保存原样保留旧固定值', keptLegacy.config.channels.cpu.manual_percent === 55 && keptLegacy.config.channels.system.manual_percent === 73 && keptLegacy.config.channels.cpu.mode === 'manual' && keptLegacy.config.channels.system.mode === 'manual');
    check('旧固定配置两路均实际接管', keptLegacy.channels.cpu.active === true && keptLegacy.channels.system.active === true, JSON.stringify({ channels: keptLegacy.channels, last_error: keptLegacy.last_error }));
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await noOverflow('旧固定档 ' + width);
    }
    await page.setViewportSize({ width: 1120, height: 1000 });
    const callsBeforeFixed = (await fixtureCall('fan_calls')).fan_calls;
    for (const [mode, percent] of [['high', 100], ['medium', 75], ['low', 50]]) {
      await cpuModes.selectOption(mode);
      check('CPU ' + mode + '直接显示固定输出', await cpuEditor.locator('.r1-fixed').textContent() === percent + '%' && await cpuEditor.locator('.r1-curve, .r1-ms').count() === 0 && await cpuModes.locator('option[value^="legacy:"]').count() === 0);
      const fixed = await saveFanDirect('CPU ' + mode);
      check('CPU ' + mode + '发送独立固定值且不改系统旧值', fixed.config.channels.cpu.mode === 'manual' && fixed.config.channels.cpu.manual_percent === percent && fixed.channels.cpu.active === true && fixed.config.channels.system.manual_percent === 73 && fixed.config.channels.system.mode === 'manual');
      const callsAfterFixed = (await fixtureCall('fan_calls')).fan_calls;
      check('CPU ' + mode + '调速沿用接管且不复位或重复设原厂档', JSON.stringify(callsAfterFixed) === JSON.stringify(callsBeforeFixed), JSON.stringify({ before: callsBeforeFixed, after: callsAfterFixed }));
    }
    await cpuModes.selectOption('auto');
    await systemModes.selectOption('medium');
    const automatic = await saveFanDirect('自动');
    check('选择自动恢复 CPU 原厂且不需要专门复位', automatic.config.channels.cpu.mode === 'vendor' && automatic.channels.cpu.active === false && automatic.channels.system.active === false && automatic.official.mode === 'medium');
    await cpuModes.selectOption('manual');
    check('从固定档切换手动直接进入已保留的曲线', await cpuEditor.locator('select').count() === 1 && await cpuEditor.locator('.r1-curve').count() === 1 && await cpuEditor.locator('.r1-ms-row input:checked').count() === 1);
    const restoredManual = await saveFanDirect('手动曲线');
    check('曲线重新生效且系统仍用原厂中档', restoredManual.config.channels.cpu.mode === 'temperature' && restoredManual.channels.cpu.active === true && restoredManual.config.channels.system.mode === 'vendor' && restoredManual.official.mode === 'medium', JSON.stringify({ config: restoredManual.config, channels: restoredManual.channels, official: restoredManual.official, last_error: restoredManual.last_error, request: fanResponses[fanResponses.length - 1]?.request }));
    const beforeMinimum = (await fixtureCall('fan_calls')).fan_calls;
    await fixtureCall('fan_legacy_ten');
    await reloadFanSettings(page);
    check('合法旧固定 10% 显示独立兼容项而非 CPU 低档', await cpuModes.inputValue() === 'legacy:10' && await cpuEditor.locator('.r1-fixed').textContent() === '10%' && await cpuEditor.locator('select').count() === 1 && await cpuEditor.locator('input[type="number"]').count() === 0);
    const legacyMinimum = await saveFanDirect('旧固定10%');
    check('旧固定 10% 实际映射 raw26', legacyMinimum.channels.cpu.target_percent === 10 && legacyMinimum.channels.cpu.target_raw === 26);
    await cpuModes.selectOption('manual');
    const lowTemp = cpuEditor.locator('input[aria-label="低温℃"]');
    const highTemp = cpuEditor.locator('input[aria-label="高温℃"]');
    check('两个阈值输入保持严格递增范围', await lowTemp.getAttribute('min') === '15' && Number(await lowTemp.getAttribute('max')) === Number(await highTemp.inputValue()) - 1 && Number(await highTemp.getAttribute('min')) === Number(await lowTemp.inputValue()) + 1 && await highTemp.getAttribute('max') === '85');
    await lowTemp.fill('14');
    await lowTemp.dispatchEvent('change');
    await highTemp.fill('86');
    await highTemp.dispatchEvent('change');
    check('温度阈值约束在 15 至 85℃', await lowTemp.inputValue() === '15' && await highTemp.inputValue() === '85');
    await lowTemp.fill('85');
    await lowTemp.dispatchEvent('change');
    check('低温不能等于或高于高温', await lowTemp.inputValue() === '84' && await highTemp.getAttribute('min') === '85');
    await highTemp.fill('15');
    await highTemp.dispatchEvent('change');
    check('高温不能等于或低于低温', await highTemp.inputValue() === '85' && await lowTemp.getAttribute('max') === '84');
    await lowTemp.fill('15.4');
    await lowTemp.dispatchEvent('change');
    await highTemp.fill('16');
    await highTemp.dispatchEvent('change');
    check('相邻温度阈值合法且输入取整数', await lowTemp.inputValue() === '15' && await highTemp.inputValue() === '16');
    await highTemp.fill('');
    await highTemp.dispatchEvent('change');
    check('清空温度输入保留原值', await highTemp.inputValue() === '16');
    const quietOutput = cpuEditor.locator('input[aria-label="静音输出"]');
    const normalOutput = cpuEditor.locator('input[aria-label="普通输出"]');
    const fullOutput = cpuEditor.locator('input[aria-label="全速输出"]');
    check('曲线静音输出范围为 10 到 100', await quietOutput.getAttribute('min') === '10' && await quietOutput.getAttribute('max') === '100');
    await quietOutput.fill('9');
    await quietOutput.dispatchEvent('change');
    check('静音输出低于 10 时约束为 10', await quietOutput.inputValue() === '10');
    await quietOutput.fill('100');
    await quietOutput.dispatchEvent('change');
    check('100% 合法且普通输出不低于静音', await quietOutput.inputValue() === '100' && await normalOutput.inputValue() === '100' && await normalOutput.getAttribute('min') === '100');
    await quietOutput.fill('101');
    await quietOutput.dispatchEvent('change');
    await normalOutput.fill('101');
    await normalOutput.dispatchEvent('change');
    check('静音与普通输出不能超过 100%', await quietOutput.inputValue() === '100' && await normalOutput.inputValue() === '100');
    check('100% 曲线绘制到纵轴顶端', (await cpuEditor.locator('.r1-curve-line').getAttribute('d')).startsWith('M8 8.0'));
    await quietOutput.fill('10');
    await quietOutput.dispatchEvent('change');
    await normalOutput.fill('9');
    await normalOutput.dispatchEvent('change');
    check('普通输出同时遵守 10% 下限和静音下限', await normalOutput.inputValue() === '10' && await normalOutput.getAttribute('min') === '10');
    await normalOutput.fill('20');
    await normalOutput.dispatchEvent('change');
    check('全速输出固定禁用且保持 100%', await fullOutput.isDisabled() && await fullOutput.inputValue() === '100');
    check('10% 和 20% 按完整 10 到 100 纵轴绘图', (await cpuEditor.locator('.r1-curve-line').getAttribute('d')).startsWith('M8 82.0') && (await cpuEditor.locator('.r1-curve-line').getAttribute('d')).includes('V73.8'));
    for (const [label, value] of [['高温℃', '75'], ['低温℃', '70']]) {
      const input = cpuEditor.locator('input[aria-label="' + label + '"]');
      await input.fill(value);
      await input.dispatchEvent('change');
    }
    check('阶梯图仅在低温和高温两处跳变', await cpuEditor.locator('.r1-curve-line').getAttribute('d') === 'M8 82.0H184.0V73.8H200.0V8.0H232');
    check('两个边界显示四个端点且普通区间两端包含', await cpuEditor.locator('.r1-curve-dots circle').count() === 4 && await cpuEditor.locator('.r1-curve-dots .r1-curve-closed').count() === 2);
    check('区间说明精确显示两个边界与三档输出', await cpuEditor.locator('.r1-curve-ranges').textContent() === 'T < 70℃：静音 10%；70℃ ≤ T ≤ 75℃：普通 20%；T > 75℃：全速 100%。');
    const minimumCurve = await saveFanDirect('10%温控曲线');
    check('曲线保存静音10普通20并实际输出 raw26', minimumCurve.config.channels.cpu.quiet_percent === 10 && minimumCurve.config.channels.cpu.normal_percent === 20 && minimumCurve.channels.cpu.level === 'quiet' && minimumCurve.channels.cpu.target_percent === 10 && minimumCurve.channels.cpu.target_raw === 26, JSON.stringify(minimumCurve.channels.cpu));
    await fixtureCall('stale_cpu');
    const protectedMinimum = (await api('/api/r1')).body.fan;
    check('10% 曲线仍在 CPU 读数过期时保护全速', protectedMinimum.channels.cpu.target_percent === 100 && protectedMinimum.channels.cpu.target_raw === 255, JSON.stringify(protectedMinimum.channels.cpu));
    await fixtureCall('fresh_cpu');
    const freshMinimum = (await api('/api/r1')).body.fan;
    check('读数恢复后按曲线重新输出 10%', freshMinimum.channels.cpu.target_percent === 10 && freshMinimum.channels.cpu.target_raw === 26);
    const afterMinimum = (await fixtureCall('fan_calls')).fan_calls;
    check('10% 调速与保护恢复沿用接管且无需复位', JSON.stringify(afterMinimum) === JSON.stringify(beforeMinimum), JSON.stringify({ before: beforeMinimum, after: afterMinimum }));
    await lowTemp.fill('60');
    await lowTemp.dispatchEvent('change');
    await highTemp.fill('70');
    await highTemp.dispatchEvent('change');
    await saveFanDirect('双阈值边界');
    for (const [temperature, level, percent] of [[59.5, 'quiet', 10], [60, 'normal', 20], [70, 'normal', 20], [70.5, 'full', 100], [70, 'normal', 20], [60, 'normal', 20], [59.5, 'quiet', 10]]) {
      await fixtureCall('fan_cpu_temperature', temperature);
      const live = (await api('/api/r1')).body.fan.channels.cpu;
      check('升降温 ' + temperature + '℃ 使用明确区间 ' + level, live.reference_c === temperature && live.level === level && live.target_percent === percent, JSON.stringify(live));
    }
    await fixtureCall('fan_cpu_temperature', 62);
    await reloadFanSettings(page);
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await noOverflow('风扇设置 ' + width);
    }
    await page.setViewportSize({ width: 1120, height: 1000 });
    await shot('settings');

    /* 原厂档位能力独立于手动接管；未知状态不能默认显示自动。 */
    const nativeContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'zh-CN' });
    const nativePage = await nativeContext.newPage();
    nativePage.on('pageerror', error => pageErrors.push(error.message));
    await nativePage.goto(fixture.unsupported.origin + '/#/settings');
    await nativePage.waitForSelector('#set-r1-fan .r1-edit[aria-busy="false"] [data-channel="system"] select');
    const nativeCard = nativePage.locator('#set-r1-fan');
    const nativeSystem = nativeCard.locator('[data-channel="system"]');
    check('独立接管不可用时原厂档位仍可选', await nativeSystem.locator('select option[value="high"]').isEnabled() && /手动接管暂不可用/.test(await nativeCard.textContent()));
    await nativePage.evaluate(async code => {
      const response = await fetch('/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FNM-Request': '1' }, body: JSON.stringify({ code }) });
      if (!response.ok) throw new Error('fixture native pairing failed');
    }, fixture.unsupported.code);
    await nativeSystem.locator('select').first().selectOption('high');
    const nativeWrite = nativePage.waitForResponse(response => response.url().endsWith('/api/r1/fan') && response.request().method() === 'POST');
    await nativeCard.locator('[data-ref="save"]').click();
    check('纯原厂保存立即发送且无确认弹窗', (await nativeWrite).status() === 200 && await nativePage.locator('#r1AskT').count() === 0);
    await nativePage.waitForFunction(() => /原厂 · 高档/.test(document.querySelector('#set-r1-fan [data-channel="system"] [data-ref="state"]')?.textContent || ''), null, { timeout: 8000 });
    const nativeState = await nativePage.evaluate(async () => (await fetch('/api/r1')).json());
    check('独立预检失败仍成功切换原厂高档', nativeState.fan.official.mode === 'high' && !nativeState.fan.channels.cpu.active && !nativeState.fan.channels.system.active);
    await fixtureCall('unsupported_fan_busy');
    await reloadFanSettings(nativePage);
    check('切换中状态不会误称已生效', /切换中/.test(await nativeSystem.locator('[data-ref="state"]').textContent()) && await nativeCard.locator('[data-ref="save"]').isDisabled());
    await fixtureCall('unsupported_fan_idle');
    await fixtureCall('unsupported_official_unknown');
    await reloadFanSettings(nativePage);
    check('未知原厂档位保持未读取', await nativeSystem.locator('select').first().inputValue() === '' && /档位未读取/.test(await nativeSystem.textContent()));
    await fixtureCall('unsupported_official_unavailable');
    await reloadFanSettings(nativePage);
    check('原厂不可用时禁用四个原厂档位', await nativeSystem.locator('option[value="auto"]').isDisabled() && await nativeSystem.locator('option[value="low"]').isDisabled() && await nativeSystem.locator('option[value="medium"]').isDisabled() && await nativeSystem.locator('option[value="high"]').isDisabled());
    check('原厂不可用原因有显示且不伪装自动', /vendor control unavailable/.test(await nativeSystem.textContent()) && await nativeSystem.locator('select').first().inputValue() === '');
    await fixtureCall('unsupported_official_auto');
    await fixtureCall('unsupported_recovery_unconfirmed');
    await reloadFanSettings(nativePage);
    check('恢复失败单通道状态明确待确认', (await nativeCard.locator('[data-ref="state"]').allTextContents()).every(value => value === '恢复待确认'));
    check('恢复失败当前运行不冒称原厂已恢复', (await nativeCard.locator('.r1-native-current').allTextContents()).every(value => value === '原厂恢复待确认'));
    await nativePage.locator('#bottomNav button[data-page="thermal"]').click();
    await nativePage.waitForSelector('#layout-thermal [data-ch="system"]');
    check('恢复失败状态卡不冒称原厂已恢复', /恢复待确认/.test(await nativePage.locator('#layout-thermal [data-ch="system"]').textContent()) && /原厂恢复待确认/.test(await nativePage.locator('#layout-thermal [data-ch="cpu"]').textContent()));
    const nativeWidth = await nativePage.evaluate(() => ({ width: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth }));
    check('风扇新控件移动端无横向溢出', nativeWidth.body <= nativeWidth.width + 1 && nativeWidth.root <= nativeWidth.width + 1, JSON.stringify(nativeWidth));
    await nativeContext.close();

    /* 屏保混选现有缓存指标：点击即保存，不额外探测温度或读取 SMART。 */
    const screenCard = page.locator('#set-r1-screen');
    const saver = screenCard.locator('.r1-saver-picker');
    const saverInput = id => saver.locator('input[value="' + id + '"]');
    const waitSaver = async expected => {
      await page.waitForFunction(expected => {
        const picker = document.querySelector('#set-r1-screen .r1-saver-picker');
        const actual = picker ? [...picker.querySelectorAll('input:checked')].map(input => input.value) : [];
        return picker?.getAttribute('aria-busy') === 'false' && actual.length === expected.length && expected.every(id => actual.includes(id));
      }, expected, { timeout: 8000 });
    };
    const chooseSaver = async (id, checked = true) => {
      const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/r1/settings') && response.request().method() === 'POST');
      if (checked) await saverInput(id).check(); else await saverInput(id).uncheck();
      const response = await responsePromise, body = await response.json();
      const requested = response.request().postDataJSON().settings.saver_items;
      check('屏保项目点击即保存且保持鉴权写接口', response.status() === 200 && body.ok === true && await page.locator('#r1AskT').count() === 0, JSON.stringify(body));
      await waitSaver(requested);
      const settings = (await api('/api/r1')).body.control.settings;
      check('屏保指标按点击顺序保存稳定 ID', JSON.stringify(settings.saver_items) === JSON.stringify(requested), JSON.stringify(settings.saver_items));
      return requested;
    };
    const screenSmartBefore = smartRequests;
    const screenCommandsBefore = (await fixtureCall('fan_calls')).command_count;
    await waitSaver(['cpu_usage', 'memory_usage']);
    check('屏保关闭时也可预先选择显示项目', (await api('/api/r1')).body.control.settings.saver_timeout === 0 && await saver.isVisible());
    check('旧使用率配置仍显示 CPU 和内存使用率', await saverInput('cpu_usage').isChecked() && await saverInput('memory_usage').isChecked());
    await chooseSaver('memory_usage', false);
    check('仅剩一个项目时禁止清空', await saverInput('cpu_usage').isDisabled());
    const writesBeforeEmpty = screenWrites;
    await saverInput('cpu_usage').evaluate(input => { input.checked = false; input.dispatchEvent(new Event('change', { bubbles: true })); });
    check('UI 空选择恢复原勾选且不提交', screenWrites === writesBeforeEmpty && await saverInput('cpu_usage').isChecked() && /1–12/.test(await saver.textContent()));
    const mixedItems = await chooseSaver('temperature:disk');
    check('屏保可以混选使用率与指定硬盘温度', JSON.stringify(mixedItems) === JSON.stringify(['cpu_usage', 'temperature:disk']));
    await reloadFanSettings(page);
    await waitSaver(mixedItems);
    check('刷新后混选与稳定来源 ID 保持', await saverInput('temperature:disk').isChecked() && /硬盘 A/.test(await saver.textContent()));
    await fixtureCall('saver_disconnect_disk');
    await reloadFanSettings(page);
    await waitSaver(mixedItems);
    const absentDisk = saver.locator('.r1-saver-option:has(input[value="temperature:disk"])');
    check('断开来源不删除勾选且保留原标签', await saverInput('temperature:disk').isChecked() && /硬盘 A/.test(await absentDisk.textContent()) && /不可用.*未连接/.test(await absentDisk.textContent()));
    let releaseScreenWrite;
    const screenWriteGate = new Promise(resolve => { releaseScreenWrite = resolve; });
    const delayedScreenWrite = async route => {
      if (route.request().method() === 'POST') await screenWriteGate;
      await route.continue();
    };
    await page.route(origin + '/api/r1/settings', delayedScreenWrite);
    const delayedScreenResponse = page.waitForResponse(response => response.url().endsWith('/api/r1/settings') && response.request().method() === 'POST');
    await saverInput('memory_usage').check();
    check('屏保写入期间禁用全部选项', await saver.locator('input:enabled').count() === 0 && await saver.getAttribute('aria-busy') === 'true');
    await fixtureCall('stale_board');
    const changedSources = page.waitForResponse(response => response.url().endsWith('/api/r1') && response.request().method() === 'GET');
    await page.locator('#btnRefresh').click();
    const changedSnapshot = await changedSources;
    await changedSnapshot.finished();
    check('保存延迟期间已取得后台来源状态更新', (await changedSnapshot.json()).fan.sources.some(source => source.id === 'board' && source.fresh === false));
    check('来源状态刷新仍保持写入锁与待保存勾选', await saver.locator('input:enabled').count() === 0 && await saver.getAttribute('aria-busy') === 'true' && await saverInput('memory_usage').isChecked());
    releaseScreenWrite();
    check('延迟写入最终成功', (await delayedScreenResponse).status() === 200);
    await waitSaver([...mixedItems, 'memory_usage']);
    await page.unroute(origin + '/api/r1/settings', delayedScreenWrite);
    await chooseSaver('memory_usage', false);
    await fixtureCall('fresh_board');
    await fixtureCall('saver_fail_save');
    const failedSaverResponse = page.waitForResponse(response => response.url().endsWith('/api/r1/settings') && response.request().method() === 'POST');
    await saverInput('memory_usage').check();
    const failedSaver = await failedSaverResponse;
    const failedSaverBody = await failedSaver.json();
    check('设置写入失败明确未保存', failedSaver.status() === 400 && failedSaverBody.saved === false, JSON.stringify(failedSaverBody));
    await waitSaver(mixedItems);
    check('失败后恢复服务端选择而非假勾选', !(await saverInput('memory_usage').isChecked()) && JSON.stringify((await api('/api/r1')).body.control.settings.saver_items) === JSON.stringify(mixedItems));
    check('失败提示不冒称设置已保存', /小屏设置未保存/.test(await page.locator('body').textContent()));
    await fixtureCall('saver_allow_save');
    await fixtureCall('saver_legacy_temperature');
    await reloadFanSettings(page);
    await waitSaver(['cpu_temperature', 'board_temperature']);
    check('旧温度二选一配置由后端迁移到汇总温度项目', JSON.stringify((await api('/api/r1')).body.control.settings.saver_items) === JSON.stringify(['cpu_temperature', 'board_temperature']));
    await fixtureCall('saver_extra_sources');
    await reloadFanSettings(page);
    await waitSaver(['cpu_temperature', 'board_temperature']);
    check('现有来源按 CPU 主板与硬盘分类', /CPU 温度来源/.test(await saver.textContent()) && /主板温度来源/.test(await saver.textContent()) && /硬盘温度来源/.test(await saver.textContent()));
    check('温度来源长标签作为文本转义', /<img src=x onerror="window.saverLabelInjected=1">/.test(await saver.textContent()) && await saver.locator('img').count() === 0 && await page.evaluate(() => window.saverLabelInjected) === undefined);
    await chooseSaver('cpu_usage');
    let twelveItems = await chooseSaver('temperature:extra:0');
    while (twelveItems.length < 12) {
      const next = await saver.locator('input:not(:checked)').first().inputValue();
      twelveItems = await chooseSaver(next);
    }
    check('恰好 12 项时禁止继续勾选', await saver.locator('input:not(:checked):enabled').count() === 0 && /已选 12 项/.test(await saver.textContent()));
    const writesBeforeThirteen = screenWrites;
    await saver.locator('input:not(:checked)').first().evaluate(input => { input.checked = true; input.dispatchEvent(new Event('change', { bubbles: true })); });
    check('UI 超过 12 项恢复原选择且不提交', screenWrites === writesBeforeThirteen && await saver.locator('input:checked').count() === 12);
    const emptySaver = await post('/api/r1/settings', { settings: { saver_items: [] } });
    const oversizedSaver = await post('/api/r1/settings', { settings: { saver_items: Array.from({ length: 13 }, (_, index) => 'temperature:extra:' + index) } });
    check('后端拒绝空选择和超过 12 项', emptySaver.status === 400 && emptySaver.body.saved === false && oversizedSaver.status === 400 && oversizedSaver.body.saved === false);
    check('无效请求不覆盖已保存项目', JSON.stringify((await api('/api/r1')).body.control.settings.saver_items) === JSON.stringify(twelveItems));
    const lastSaverItem = twelveItems[twelveItems.length - 1];
    await chooseSaver(lastSaverItem, false);
    twelveItems = await chooseSaver(lastSaverItem);
    check('边界拒绝后仍可继续保存有效选择', await saver.locator('[data-ref="error"]').isHidden() && twelveItems.length === 12);
    if (screenshotDir) await page.waitForFunction(() => !document.querySelector('.toasts .toast'), null, { timeout: 6000 });
    await page.setViewportSize({ width: 320, height: 844 });
    await saver.scrollIntoViewIfNeeded();
    await noOverflow('屏保项目 320');
    await shot('saver-320');
    await page.setViewportSize({ width: 1120, height: 1000 });
    await saver.scrollIntoViewIfNeeded();
    await shot('saver-desktop');
    check('选择屏保项目不触发 SMART 或硬件探测', smartRequests === screenSmartBefore && (await fixtureCall('fan_calls')).command_count === screenCommandsBefore);

    /* 7. 电源操作受确认保护 */
    await page.locator('#pageSeg button[data-page="overview"]').click();
    await page.waitForSelector('section[data-mod="r1screen"]');
    await page.locator('section[data-mod="r1screen"] [data-pwr="poweroff"]').click();
    check('电源操作先弹确认', await page.locator('.fullmask [data-no]').count() === 1);
    await page.locator('.fullmask [data-no]').click();

    /* 8. 登录口令 */
    const setup = await post('/api/auth/setup', { password: 'fixture-pass-1' });
    check('首次设置口令', setup.status === 200 && setup.body.ok === true, JSON.stringify(setup.body));
    await page.goto(origin + '/');
    await page.waitForFunction(() => document.getElementById('hostName'), null, { timeout: 10000 });
    check('已登录会话可直接打开面板', await page.locator('#layout-overview').count() === 1);
    const fresh = await browser.newContext({ viewport: { width: 1024, height: 900 }, locale: 'zh-CN' });
    const loginPage = await fresh.newPage();
    await loginPage.goto(origin + '/');
    await loginPage.waitForSelector('#login-form:not([hidden])');
    check('启用口令后打开登录页', await loginPage.locator('#pw').count() === 1);
    check('未登录时监控接口 401', (await loginPage.request.get(origin + '/api/overview')).status() === 401);
    await loginPage.locator('#pw').fill('fixture-pass-1');
    await loginPage.locator('#btn-login').click();
    await loginPage.waitForFunction(() => document.getElementById('hostName'), null, { timeout: 10000 });
    check('口令登录进入面板', await loginPage.locator('#layout-overview').count() === 1);
    await fresh.close();

    /* 9. 无页面错误、无外部请求 */
    check('无页面脚本错误', pageErrors.length === 0, pageErrors.join(' | '));
    check('无外部网络请求', blocked.length === 0, blocked.join(' | '));

    await browser.close();
    browser = null;
    console.log(JSON.stringify({ ok: true, checks: checks.length, sizes, elapsed_ms: Date.now() - started }, null, 1));
    process.exit(0);
  } catch (error) {
    if (browser) await browser.close().catch(() => {});
    console.error(JSON.stringify({ ok: false, checks, sizes, pageErrors, blocked, error: String(error && error.stack || error), stderr: stderr.slice(-3000) }, null, 1));
    process.exit(1);
  } finally {
    clearTimeout(bootTimer);
    child.kill('SIGTERM');
  }
})();
