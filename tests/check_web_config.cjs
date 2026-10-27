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
import copy

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
def controller(folder):
    value = ToolboxController(state_dir=folder / 'data', sys_root=folder / 'sys', dev_root=folder / 'dev', runner=runner)
    value.systemctl = None
    value.i2cget, value.i2cset = 'fixture-i2cget', 'fixture-i2cset'
    controllers.append(value)
    return value

board_stale = False
class Sources:
    def snapshot(self):
        now = time.monotonic()
        return [{'id': name, 'label': label, 'kind': kind, 'temperature_c': temperature, 'status': 'ok',
                 'sampled_monotonic': now - (30 if name == 'board' and board_stale else 0),
                 'max_age_seconds': 10, 'critical_celsius': 95 if kind == 'cpu' else None}
                for name, label, kind, temperature in [('cpu', 'CPU Package', 'cpu', 62), ('board', '主板温度', 'board', 66), ('disk', '硬盘 A', 'disk', 47), ('disk2', '硬盘 B', 'disk', 39)]]
class Adapter:
    def __init__(self, available):
        self.available, self.channels, self.targets = available, set(), {}
        self.fail_release = False
        self.reason = "模拟设备：原厂风扇配置无法解析"
    def capabilities(self):
        return {'available': self.available, 'reason': '' if self.available else self.reason,
                'channels': ['cpu', 'system'] if self.available else [], 'rpm_available': False}
    def start(self, channels):
        assert self.available
        time.sleep(0.15)
        self.channels = set(channels)
    def write(self, targets):
        assert set(targets) <= self.channels and all(type(value) is int and 128 <= value <= 255 for value in targets.values())
        self.targets = dict(targets)
    def release(self):
        if self.fail_release:
            raise OSError("fixture restore not acknowledged")
        self.channels, self.targets = set(), {}
policies = []
def policy(folder, available):
    value = FanPolicyController(folder / 'data', Sources(), Adapter(available), interval=0.1)
    policies.append(value)
    value.start()
    return value

class Monitor:
    def snapshot(self):
        return {'schema_version':1,'available':True,'hostname':'R1-Fixture','uptime_seconds':201600,
                'cpu':{'usage_percent':24.5,'temp_c':62},'memory':{'usage_percent':37.5,'used_bytes':3e9,'total_bytes':8e9},
                'network':{'rx_bytes_per_sec':1250000,'tx_bytes_per_sec':480000},
                'disks':[{'mount':'/drives/data','used_bytes':1e12,'total_bytes':4e12,'usage_percent':25}]}
class Insights:
    def __init__(self):
        self.data={'system':{'model':'HIKSEMI R1','cpu_model':'Intel N100','cpu_logical_cores':4,'uptime_seconds':201600,
                     'cpu_usage_percent':24.5,'load_average':[.2,.3,.5],'memory':{'used_bytes':3e9,'total_bytes':8e9,'usage_percent':37.5},
                     'cpu_cores':[{'id':n,'usage_percent':10+n*5} for n in range(4)]},
                  'device':{'identity':{'hostname':'R1-Fixture','os':{'PRETTY_NAME':'Fixture Linux 12 (R1)','NAME':'Fixture Linux','VERSION_ID':'12'},'kernel':'6.1-fixture','firmware':{'version':'V6.3.3_Build_260907','build':'260907','model':'R1'}},
                    'cpu':{'physical_cores':4,'architecture':'x86_64'},'thermal':[{'driver':'coretemp','label':'Package','celsius':62}],
                    'network':[{'name':'eth0','speed_mbps':2500,'ipv6_addresses':[{'address':'2001:db8:1234:5678:90ab:cdef:1234:5678','prefix_length':64,'scope':'global'}]}],
                    'network_config':{'default_routes':{'ipv4':[{'interface':'eth0','gateway':'192.168.1.1','metric':100,'source':'/proc/net/route'}],'ipv6':[{'interface':'eth0','gateway':None,'metric':200,'source':'/proc/net/ipv6_route'}]},'route_status':{'ipv4':'ok','ipv6':'ok'},'dns':{'servers':['192.168.1.1','2001:4860:4860::8888'],'search':['home.arpa'],'domains':['home.arpa'],'status':'ok'}},'storage':{'raid':[]}},
                  'interfaces':[{'name':'eth0','ipv4':['192.168.1.10'],'mac':'00:11:22:33:44:55','operstate':'up','rx_bytes':400000000,'tx_bytes':100000000,'rx_bytes_per_sec':1250000,'tx_bytes_per_sec':480000}],
                  'disks':{'available':True,'items':[{'id':'sda','device':'/dev/sda','model':'Fixture NAS Drive','size_bytes':4e12,'temp_c':39,'standby':False,'health':{'passed':True}}]},
                  'containers':{'available':True,'items':[{'id':'a'*64,'name':'fixture-db','image':'fixture:local','state':'running','protected':False,'cpu_percent':3.2,'memory_bytes':134217728,'memory_limit_bytes':1073741824,'ports':'0.0.0.0:8080->8080/tcp, [::]:8080->8080/tcp'},{'id':'b'*64,'name':'r1-toolbox','image':'fixture:local','state':'exited','protected':True}]},
                  'vms':{'available':False,'reason':'fixture: no VM','items':[]},'ports':[{'protocol':'tcp','address':'127.0.0.1','port':18731}],
                  'processes':[{'pid':101,'name':'fixture-monitor','cpu_percent':1.2,'memory_bytes':40000000}],
                  'gpu':{'available':False,'reason':'fixture: no GPU','devices':[]},
                  'history':[{'cpu_usage_percent':v,'memory_usage_percent':37.5} for v in [18,22,28,21,24.5]]}
    def snapshot(self):return copy.deepcopy(self.data)
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
monitoring=MonitoringService(fixture/'data',insights,monitor,Sources(),proc_root=fixture/'proc',sys_root=fixture/'sys').start()
primary = WebConfigServer(controller(fixture), insights=insights, fan_policy=policy(fixture, True), light_policy=lights[0],monitoring=monitoring,monitor=monitor, host='127.0.0.1', port=0, asset_dir=repo / 'ui/web').start()
unsupported = WebConfigServer(controller(fixture / 'unsupported'), fan_policy=policy(fixture / 'unsupported', False),light_policy=lights[1],monitor=monitor, host='127.0.0.1', port=0, asset_dir=repo / 'ui/web').start()

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
            primary.fan_policy.stop()
            primary.fan_policy = policy(fixture, True)
        elif operation == 'stale_board':
            board_stale = True
        elif operation == 'fresh_board':
            board_stale = False
        elif operation == 'fail_release':
            primary.fan_policy.adapter.fail_release = True
        elif operation == 'allow_release':
            primary.fan_policy.adapter.fail_release = False
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
        elif operation == 'app_refresh':
            item = insights.data['containers']['items'][0]
            item['cpu_percent'] = round(item.get('cpu_percent', 0) + 1, 1)
        elif operation == 'app_paused':
            insights.data['containers']['items'][0].update(state='paused', available_actions=[])
        elif operation == 'app_running':
            insights.data['containers']['items'][0].update(state='running', available_actions=['stop','restart'])
        elif operation != 'stop':
            raise AssertionError('unknown fixture operation')
        print(json.dumps({'id': request['id'], 'ok': True, 'command_count': len(commands)}), flush=True)
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
  let stderr = '', sequence = 0, bootResolve, bootReject, browser;
  const pending = new Map(), checks = [], sizes = [], pageErrors = [], blocked = [], actionHeaders = [], actionBodies = [];
  const ready = new Promise((resolve, reject) => { bootResolve = resolve; bootReject = reject; });
  const bootTimer = setTimeout(() => bootReject(new Error('本地测试网页服务启动超时')), 10000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-4096); });
  child.on('error', bootReject);
  child.on('exit', code => {
    if (code) bootReject(new Error('本地网页 fixture 退出：' + code + '\n' + stderr));
    for (const request of pending.values()) request.reject(new Error('本地网页 fixture 已退出\n' + stderr));
    pending.clear();
  });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try {
      const value = JSON.parse(line);
      if (value.ready) { clearTimeout(bootTimer); bootResolve(value); }
      else if (pending.has(value.id)) { const request = pending.get(value.id); pending.delete(value.id); request.resolve(value); }
    } catch (_) { bootReject(new Error('网页 fixture 启动响应格式错误')); }
  });
  const command = operation => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('网页 fixture 操作超时')); }, 5000);
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
    child.stdin.write(JSON.stringify({ id, operation }) + '\n');
  });
  try {
    const fixture = await ready;
    const allowed = new Set([fixture.primary.origin, fixture.unsupported.origin]);
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1120, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', route => {
      if (allowed.has(new URL(route.request().url()).origin)) return route.continue();
      blocked.push(route.request().url()); return route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/action')) { actionHeaders.push(request.headers()); actionBodies.push(request.postDataJSON()); } });
    const screenshot = async name => {
      if (!screenshotDir) return;
      const dialogOpen = await page.locator('#app-detail[open]').count();
      const caption = page.locator(dialogOpen ? '#app-detail-kind' : '.topbar .eyebrow');
      const original = await caption.textContent();
      await caption.evaluate(node => { node.textContent += ' · 模拟设备数据'; });
      await page.screenshot({ path: path.join(screenshotDir, name + '.png'), fullPage: !dialogOpen });
      await caption.evaluate((node, value) => { node.textContent = value; }, original);
    };
    const measure = async name => {
      const value = await page.evaluate(() => ({ width: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth }));
      sizes.push({ name, ...value }); assert.ok(value.body <= value.width && value.root <= value.width, '网页横向溢出：' + JSON.stringify(value));
    };
    const login = async pair => {
      const initialSession = page.waitForResponse(response => response.url() === pair.origin + '/api/session');
      await page.goto(pair.origin + '/#settings'); await page.reload(); await initialSession;
      await page.locator('#code').fill(pair.code);
      await page.locator('#pair-form button').click();
      await page.waitForFunction(() => !document.getElementById('editor').hidden);
      assert.equal(await page.locator('#code').inputValue(), '');
    };
    const persisted = () => JSON.parse(fs.readFileSync(path.join(fixtureDir, 'data/settings.json'), 'utf8'));
    const save = async section => {
      const form = page.locator(`[data-section="${section}"]`);
      await form.locator('button[type="submit"]').click();
      await page.waitForFunction(section => document.querySelector(`[data-section="${section}"] .result`).textContent.includes('已保存'), section);
    };
    const setting = async name => {
      await page.locator('[data-web-page="settings"]').click();
      await page.locator('[data-settings-tab="'+name+'"]').click();
      assert.equal(await page.locator('[data-settings-tab="'+name+'"]').getAttribute('aria-selected'),'true'); assert.equal(await page.locator('.settings-panel[data-settings-view="'+name+'"]').isVisible(),true);
    };
    const reload = async () => {
      const tab = await page.evaluate(() => document.documentElement.dataset.settingsView || 'panel');
      await page.reload(); await page.waitForFunction(() => !document.getElementById('editor').hidden);
      await setting(tab);
    };

    await page.goto(fixture.primary.origin + '/#overview');
    await page.waitForFunction(() => document.getElementById('host-name').textContent === 'R1-Fixture');
    const publicStatus = await page.request.get(fixture.primary.origin + '/api/v1/status');
    assert.equal(publicStatus.status(),200); assert.equal(publicStatus.headers()['access-control-allow-origin'],'*');
    const baselineStatus = await publicStatus.json();
    assert.equal(typeof baselineStatus.insights.device.identity.os,'object','public API must preserve structured OS metadata');
    assert.equal(typeof baselineStatus.insights.device.identity.firmware,'object','public API must preserve structured firmware metadata');
    assert.equal((await page.request.get(fixture.primary.origin + '/api/session')).status(),401);
    assert.equal(await page.locator('#editor').isVisible(),false);
    assert.match(await page.locator('#overview-fans').textContent(),/RPM 未知/);
    const publicPosts=actionBodies.length;
    for(const width of [1440,1120,760,376,320]) {
      await page.setViewportSize({width,height:1000});
      for(const view of ['overview','trends','storage','network','apps','device','controls']) {
        await page.locator('[data-web-page="'+view+'"]').click();
        if(view==='trends') await page.waitForFunction(()=>!document.getElementById('history-note').textContent.includes('正在读取'));
        await measure('public-'+width+'-'+view);
        if(['overview','network','apps','controls'].includes(view)&&[1440,376,320].includes(width)) await screenshot('preview-web-'+width+'-'+view+'-fixture');
      }
    }
    assert.equal(actionBodies.length,publicPosts,'public navigation must never emit device writes');
    assert.equal(await page.locator('#web-power-card').isVisible(),false);
    await page.locator('[data-web-page="network"]').click();
    assert.match(await page.locator('#network-interfaces').textContent(),/2001:db8:1234:5678:90ab:cdef:1234:5678\/64/);
    assert.match(await page.locator('#network-routes').textContent(),/直连路由（无下一跳网关）/);
    assert.match(await page.locator('#network-dns').textContent(),/默认域名/);
    await page.locator('[data-web-page="apps"]').click();assert.equal(await page.locator('#web-containers [data-device-action]').count(),0);
    assert.match(await page.locator('#docker-running').textContent(),/1\s*\/\s*2/);
    assert.equal(await page.locator('#web-containers .port-badge').count(),2);
    await page.locator('[data-container-filter="running"]').click();assert.equal(await page.locator('#web-containers .application-row').count(),1);
    await page.locator('[data-container-filter="all"]').click();await page.locator('#container-search').fill('r1-toolbox');assert.equal(await page.locator('#web-containers .application-row').count(),1);
    assert.match(await page.locator('#web-containers').textContent(),/受保护，仅查看/);assert.ok(!(await page.locator('#web-containers').textContent()).includes('解锁后管理'));
    await page.locator('#web-containers .app-title').click();assert.match(await page.locator('#app-detail-note').textContent(),/安装任务完成后停止属正常/);assert.equal(await page.locator('#app-detail [data-device-action]').count(),0);await page.keyboard.press('Escape');
    await page.locator('#container-search').fill('');await page.locator('#web-containers .application-row').first().getByRole('button',{name:'查看详情 ↗',exact:true}).click();
    assert.match(await page.locator('#app-detail-fields').textContent(),/134|128 MB/);assert.match(await page.locator('#app-detail-fields').textContent(),new RegExp('a'.repeat(64)));
    for(const width of [1440,376,320]){await page.setViewportSize({width,height:1000});await measure('public-docker-detail-'+width);await screenshot('preview-web-'+width+'-docker-detail-fixture');}
    await page.locator('#app-detail-close').click();assert.equal(await page.locator('#app-detail').isVisible(),false);
    await page.locator('[data-web-page="controls"]').click();await page.waitForFunction(()=>document.getElementById('control-brightness').textContent==='80%');
    checks.push('Docker running/total, search, status filters and raw port badges use real fixture fields; anonymous and protected details are read-only, Toolbox stopped-after-install is normal; drawer fits desktop/376/320; control center reads actual public brightness');
    checks.push('public real HTTP dashboard requires no pairing; CORS read-only; six monitoring sections at five widths; IPv6/source wrapping and on-link routes; no unauthorized controls or POST');

    // Inventory returns structured identity metadata. Exercise the real browser
    // renderer rather than replacing those public API objects with test strings.
    const identityFields = () => page.locator('#web-identity .reading-row').evaluateAll(nodes => Object.fromEntries(nodes.map(node => [node.querySelector('span').textContent,node.querySelector('strong').textContent])));
    await page.locator('[data-web-page="device"]').click();
    const actualIdentity = await identityFields();
    assert.equal(actualIdentity['系统'],'Fixture Linux 12 (R1)');
    assert.match(actualIdentity['固件'],/V6\.3\.3_Build_260907/);
    assert.match(actualIdentity['固件'],/R1/);
    assert.match(actualIdentity['固件'],/260907/);
    assert.ok(!(await page.locator('#web-identity').textContent()).includes('[object Object]'));
    const xssName = '<img src=x onerror="window.identityXss=1"> NAS ' + 'W'.repeat(100);
    const xssFirmware = '<svg onload="window.identityXss=2"></svg> Legacy 1.0';
    const identityCases = [
      {name:'empty',os:{},firmware:{},osValues:['--'],firmwareValues:['--'],exact:true},
      {name:'null',os:null,firmware:null,osValues:['--'],firmwareValues:['--'],exact:true},
      {name:'legacy',os:'  Legacy NAS OS 1.0  ',firmware:'  Legacy R1 firmware 2.0  ',osValues:['Legacy NAS OS 1.0'],firmwareValues:['Legacy R1 firmware 2.0'],exact:true},
      {name:'name-version',os:{NAME:'Fixture Linux',VERSION:'13 (R1)',VERSION_ID:'13',BUILD_ID:'20261028'},firmware:{'hiksemi-release':'Hiksemi release 6.3.0'},osValues:['Fixture Linux','13 (R1)','20261028'],firmwareValues:['Hiksemi release 6.3.0']},
      {name:'name-version-id',os:{NAME:'Hiksemi Fixture',VERSION_ID:'6.3.3'},firmware:{version:'V6.3.3_Build_260907',firmware_version:'ignored-secondary-version','hiksemi-release':'ignored-tertiary-version',model:'R1',build:260907,build_date:'2026-09-07',build_time:'12:30:00',unknown:'never-display-unknown',extra:{secret:'never-display-nested'}},osValues:['Hiksemi Fixture','6.3.3'],firmwareValues:['V6.3.3_Build_260907','R1','260907','2026-09-07','12:30:00'],absent:['ignored-secondary-version','ignored-tertiary-version','never-display-unknown','never-display-nested']},
      {name:'version-only-nested',os:{PRETTY_NAME:{secret:'never-display-nested'},NAME:['never-display-array'],VERSION_ID:'2026.10',BUILD_ID:true,unknown:'never-display-unknown'},firmware:{version:{secret:'never-display-nested'},firmware_version:['never-display-array'],model:{name:'never-display-nested'},build:true,unknown:'never-display-unknown'},osValues:['2026.10'],firmwareValues:['--'],exact:true,absent:['never-display-nested','never-display-array','never-display-unknown','true']},
      {name:'text-only-xss',os:{PRETTY_NAME:xssName},firmware:{'histor-release':xssFirmware},osValues:[xssName],firmwareValues:[xssFirmware],exact:true},
    ];
    let renderedIdentity;
    const identityRoute = fixture.primary.origin + '/api/v1/status';
    await page.route(identityRoute,route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...baselineStatus,insights:{...baselineStatus.insights,device:{...baselineStatus.insights.device,identity:renderedIdentity}}})}));
    for (const sample of identityCases) {
      renderedIdentity = {...baselineStatus.insights.device.identity,os:sample.os,firmware:sample.firmware};
      await page.reload();
      await page.waitForFunction(() => document.getElementById('live-status').textContent==='实时连接');
      const fields = await identityFields();
      for (const [label,values] of [['系统',sample.osValues],['固件',sample.firmwareValues]]) {
        if (sample.exact) assert.equal(fields[label],values[0],sample.name+' '+label);
        else for (const value of values) assert.ok(fields[label].includes(value),sample.name+' '+label+' missing '+value+': '+fields[label]);
      }
      const text = await page.locator('#web-identity').textContent();
      assert.ok(!text.includes('[object Object]'),sample.name+' must not stringify metadata objects');
      for (const value of sample.absent || []) assert.ok(!text.includes(value),sample.name+' unexpectedly rendered '+value);
      assert.equal(await page.locator('#web-identity img, #web-identity svg, #web-identity script').count(),0,sample.name+' metadata must remain text nodes');
      assert.equal(await page.evaluate(() => window.identityXss),undefined);
      for (const width of [1120,320]) { await page.setViewportSize({width,height:1000}); await measure('identity-'+sample.name+'-'+width); }
    }
    await page.unroute(identityRoute);
    await page.goto(fixture.primary.origin+'/#overview');
    await page.waitForFunction(() => document.getElementById('host-name').textContent==='R1-Fixture');
    checks.push('device identity consumes real structured public metadata; PRETTY_NAME, NAME+VERSION/VERSION_ID, version-only, build and firmware metadata stay readable; empty/null and legacy strings work, nested/unknown fields are ignored, XSS stays literal text, and desktop/320 never overflow');
    const historyRoute='**/api/v1/history?range=*';
    await page.route(historyRoute,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({available:true,range:'1h',series:[{id:'temperature.cpu-fixture',label:'CPU fixture sensor',unit:'°C',points:[[100,40],[130,50],[160,null],[190,60]]},{id:'fan_cpu_target_percent',label:'CPU 风扇请求输出',unit:'%',points:[[100,55],[130,75],[160,null],[190,100]]},{id:'fan_rpm.fixture',label:'实测传感器 fixture',unit:'rpm',points:[[100,1200],[130,1400],[160,null],[190,1600]]}]})}));
    await page.locator('[data-web-page="trends"]').click();await page.waitForFunction(()=>document.querySelectorAll('#history-charts .card').length===3);
    for(const line of await page.locator('#history-charts .chart-line').all())assert.equal(((await line.getAttribute('d')).match(/M/g)||[]).length,2,'missing history sample must break the SVG line');
    assert.match(await page.locator('#history-charts').textContent(),/CPU 风扇请求输出/);await page.unroute(historyRoute);
    await page.locator('#load-report').click();await page.waitForFunction(()=>document.getElementById('report-content').textContent.includes('有效 / 缺测指标'));assert.match(await page.locator('#report-content').textContent(),/不是 SMART 自检/);
    const [download]=await Promise.all([page.waitForEvent('download'),page.locator('#export-csv').click()]);assert.match(download.suggestedFilename(),/\.csv$/);
    checks.push('history renders separate sensor/real-RPM/request-output series with null gaps, never zero-filled; report labels read-only limits and CSV downloads only after explicit click');

    await page.setViewportSize({width:1120,height:1000});
    await page.goto(fixture.primary.origin + '/#settings');
    await page.waitForFunction(() => !document.getElementById('login').hidden);
    assert.equal((await page.request.get(fixture.primary.origin + '/api/session')).status(), 401);
    await measure('desktop-login'); await screenshot('preview-web-login-fixture');
    await login(fixture.primary);
    const cookies = await context.cookies(fixture.primary.origin);
    const cookie = cookies.find(item => item.name === 'r1_web_session');
    assert.ok(cookie && cookie.httpOnly && cookie.sameSite === 'Strict');
    assert.equal(await page.evaluate(() => document.cookie.includes('r1_web_session')), false);
    const noCsrf = await page.evaluate(async () => (await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'settings.update', payload: { settings: { theme: 'light' } } }) })).status);
    assert.equal(noCsrf, 403);
    assert.equal(await page.locator('#rgb-card').isHidden(), true);
    assert.equal(await page.locator('#screen-card').isVisible(), true);
    checks.push('real pairing login; HttpOnly/SameSite cookie hidden from JavaScript; missing CSRF rejected; unverified RGB hidden');

    await setting('lighting');await page.locator('#light-mode').selectOption('temperature');await page.locator('#light-brightness').fill('17');
    await setting('monitoring');await page.locator('#history-retention').fill('11');
    await setting('fans');await page.locator('#fan-cpu-manual_percent').evaluate(node=>{node.value='83';node.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.locator('#fan-policy-save').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('尚未应用'));
    await setting('lighting');assert.equal(await page.locator('#light-brightness').inputValue(),'17');
    await setting('monitoring');assert.equal(await page.locator('#history-retention').inputValue(),'11');
    for(const width of [1440,760,376,320]){await page.setViewportSize({width,height:1000});for(const tab of ['panel','fans','lighting','monitoring','connection']){await setting(tab);await measure('settings-'+tab+'-'+width);if([1440,320].includes(width)&&['fans','lighting','connection'].includes(tab))await screenshot('preview-web-'+width+'-settings-'+tab+'-fixture');}}
    await setting('panel');
    checks.push('all five settings subpages are truly separate and readable at four widths; fan save plus forced state poll preserves independent light/history drafts without hiding any existing controls');

    await page.locator('#theme').selectOption('jade'); await page.locator('#font_scale').fill('125'); await save('appearance');
    assert.equal(persisted().theme, 'jade'); assert.equal(persisted().font_scale, 125); await reload(); assert.equal(await page.locator('#theme').inputValue(), 'jade'); assert.equal(await page.locator('#font_scale').inputValue(), '125');
    for (const node of await page.locator('#pages input').all()) await node.setChecked(['overview', 'network', 'settings'].includes(await node.inputValue()));
    await page.locator('#page_interval').fill('15'); await save('pages'); assert.deepEqual(persisted().enabled_pages, ['overview', 'network', 'settings']); await reload(); assert.equal(await page.locator('#pages input:checked').count(), 3); assert.equal(await page.locator('#page_interval').inputValue(), '15');
    await page.locator('#brightness').fill('55'); await save('brightness'); assert.equal(persisted().brightness, 55); assert.equal(fs.readFileSync(path.join(fixtureDir, 'sys/class/backlight/intel_backlight/brightness'), 'utf8'), '55'); await reload(); assert.equal(await page.locator('#brightness').inputValue(), '55');
    await page.locator('[data-web-page="controls"]').click();await page.waitForFunction(()=>document.getElementById('control-brightness').textContent==='55%');await setting('panel');
    await page.locator('#night_enabled').check(); await page.locator('#night_start').fill('22:30'); await page.locator('#night_end').fill('06:45'); await page.locator('#night_brightness').fill('15'); await save('night'); await reload(); assert.equal(await page.locator('#night_enabled').isChecked(), true); assert.equal(await page.locator('#night_start').inputValue(), '22:30'); assert.equal(await page.locator('#night_end').inputValue(), '06:45'); assert.equal(await page.locator('#night_brightness').inputValue(), '15');
    await page.locator('#sleep_timeout').fill('90'); await save('sleep'); await reload(); assert.equal(persisted().sleep_timeout, 90); assert.equal(await page.locator('#sleep_timeout').inputValue(), '90');
    checks.push('appearance/pages/brightness/night/sleep save through real API, persist to actual fixture settings.json, and survive page reload');

    await command('verify_rgb'); await reload(); await setting('lighting'); assert.equal(await page.locator('#rgb-card').isVisible(), true);
    await page.locator('#rgb-mode').selectOption('3'); await page.locator('#rgb-color').fill('#123456'); await page.locator('#rgb-brightness').fill('35'); await page.locator('[data-section="rgb"] button').click();
    await page.waitForFunction(() => document.querySelector('[data-section="rgb"] .result').textContent.includes('即时应用'));
    assert.ok(!Object.keys(persisted()).some(key => key.startsWith('rgb')));
    await reload(); assert.equal(await page.locator('#rgb-mode').inputValue(), '3'); assert.equal(await page.locator('#rgb-brightness').inputValue(), '35');
    await page.locator('[data-web-page="controls"]').click();await page.waitForFunction(()=>document.getElementById('control-light-note').textContent.includes('当前亮度 35%'));assert.match(await page.locator('#control-light-title').textContent(),/呼吸/);assert.equal(await page.locator('#control-light-color').evaluate(node=>node.style.backgroundColor),'rgb(18, 52, 86)');await setting('lighting');
    await setting('fans'); assert.equal(await page.locator('.fan-policy-channel').count(), 2);
    await page.locator('#fan-cpu-mode').selectOption('temperature'); await page.locator('#fan-system-mode').selectOption('temperature');
    await page.locator('#fan-cpu-sources input[value="cpu"]').check(); await page.locator('#fan-cpu-sources input[value="board"]').check();
    await page.locator('#fan-system-sources input[value="disk"]').check(); await page.locator('#fan-system-sources input[value="disk2"]').check();
    const channelValues = { cpu: { quiet_below:42,normal_at:54,full_at:71,quiet_percent:58,normal_percent:78,manual_percent:76 }, system: { quiet_below:33,normal_at:44,full_at:56,quiet_percent:52,normal_percent:68,manual_percent:66 } };
    for (const [channel, values] of Object.entries(channelValues)) for (const [key,value] of Object.entries(values)) {
      const input = page.locator('#fan-' + channel + '-' + key); await input.evaluate((node, value) => { node.value = value; node.dispatchEvent(new Event('input', {bubbles:true})); }, String(value));
    }
    await page.locator('#fan-policy-save').click(); await page.waitForFunction(() => document.getElementById('fan-policy-result').textContent.includes('尚未应用'));
    const fanPersisted = () => JSON.parse(fs.readFileSync(path.join(fixtureDir,'data/fan-policy.json'),'utf8'));
    assert.deepEqual(fanPersisted().config.channels.cpu.sources,['cpu','board']); assert.deepEqual(fanPersisted().config.channels.system.sources,['disk','disk2']); assert.equal(fanPersisted().config.channels.cpu.full_at,71); assert.equal(fanPersisted().config.channels.system.full_at,56); assert.equal(fanPersisted().resume_on_boot,false);
    await reload(); assert.equal(await page.locator('#fan-cpu-normal_at').inputValue(),'54'); assert.equal(await page.locator('#fan-system-normal_at').inputValue(),'44'); assert.equal(await page.locator('#fan-cpu-sources input:checked').count(),2);
    await page.locator('#fan-cpu-quiet_below').fill('80'); const beforeInvalid=actionBodies.length; await page.locator('#fan-policy-save').click(); assert.match(await page.locator('#fan-policy-result').textContent(),/回到静音温度/); assert.equal(actionBodies.length,beforeInvalid); await page.locator('#fan-cpu-quiet_below').fill('42');
    const beforeCancel=actionBodies.length; page.once('dialog',dialog=>dialog.dismiss()); await page.locator('#fan-policy-apply').click(); assert.equal(actionBodies.length,beforeCancel);
    page.once('dialog',dialog=>dialog.accept()); await page.locator('#fan-policy-apply').click();
    await page.waitForFunction(() => document.getElementById('fan-cpu-live').textContent.includes('66 °C') && document.getElementById('fan-cpu-live').textContent.includes('78%') && document.getElementById('fan-system-live').textContent.includes('47 °C') && document.getElementById('fan-system-live').textContent.includes('68%'));
    assert.match(await page.locator('#fan-cpu-live').textContent(),/主板温度/); assert.match(await page.locator('#fan-system-live').textContent(),/硬盘 A/); assert.match(await page.locator('#fan-cpu-live').textContent(),/有效/); assert.ok(!(await page.locator('#fan-policy-result').textContent()).includes('正在切换'));
    assert.equal(fanPersisted().resume_on_boot,true); assert.ok(actionBodies.some(body=>body.action==='fan.policy.update'&&body.payload.apply===true&&body.payload.confirmed===true));
    checks.push('independent CPU/system drafts preserve separate thresholds/sources; save-only does not take ownership; invalid order and cancelled confirmation emit no POST; async apply confirms both independent max winners and actual target percentages');
    await command('stale_board'); await page.waitForFunction(() => document.getElementById('fan-cpu-live').textContent.includes('保护全速') && document.getElementById('fan-cpu-live').textContent.includes('100%'));
    assert.match(await page.locator('#fan-system-live').textContent(),/68%/); assert.match(await page.locator('#fan-cpu-sources').textContent(),/已过期/);
    await command('fresh_board'); await page.waitForFunction(() => document.getElementById('fan-cpu-live').textContent.includes('78%'));
    checks.push('stale selected board sensor makes only its CPU policy fail safe at 100%; system policy stays independent and source freshness is visible');
    await measure('desktop-editor'); await screenshot('preview-web-desktop-fixture');
    await page.setViewportSize({ width: 320, height: 960 }); await measure('320-editor'); await screenshot('preview-web-320-fixture');
    checks.push('verified RGB uses only in-memory I2C fixture commands, stays an immediate operation, and is not saved as boot configuration; desktop/320 have no horizontal overflow');

    await page.locator('#fan-cpu-mode').selectOption('manual'); await page.locator('#fan-cpu-manual_percent').fill('82'); page.once('dialog',dialog=>dialog.accept()); await page.locator('#fan-policy-apply').click();
    await page.waitForFunction(() => document.getElementById('fan-cpu-live').textContent.includes('82%') && document.getElementById('fan-system-live').textContent.includes('68%'));
    await page.locator('#fan-cpu-manual_percent').fill('84'); await page.locator('#fan-policy-save').click(); await page.waitForFunction(() => document.getElementById('fan-policy-result').textContent.includes('尚未应用'));
    assert.equal(fanPersisted().config.channels.cpu.manual_percent,84); assert.match(await page.locator('#fan-cpu-live').textContent(),/82%/); assert.match(await page.locator('#fan-policy-save-state').textContent(),/待生效/);
    page.once('dialog',dialog=>dialog.accept()); await page.locator('#fan-policy-release').click(); await page.waitForFunction(() => document.getElementById('fan-policy-result').textContent.includes('两路已恢复原厂管理'));
    assert.equal(fanPersisted().resume_on_boot,false); assert.equal(fanPersisted().config.channels.cpu.mode,'vendor'); assert.equal(fanPersisted().config.channels.system.mode,'vendor');
    checks.push('CPU can switch to manual 82% while system remains on its own temperature rule; saving 84% stays pending without changing 82%; confirmed release restores both vendor policies');
    // Begin the next independent scenario from persisted vendor settings, after
    // the previous submission's final forced refresh has left the old page.
    await reload(); await setting('fans');
    assert.equal(await page.locator('#fan-cpu-mode').inputValue(), 'vendor');
    await page.locator('#fan-cpu-mode').selectOption('manual');await page.locator('#fan-cpu-manual_percent').fill('82');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-apply').click();await page.waitForFunction(()=>document.getElementById('fan-cpu-live').textContent.includes('82%'));
    await command('fail_release');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-release').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('未生效'));
    assert.match(await page.locator('#fan-cpu-live').textContent(),/恢复未确认/);assert.match(await page.locator('#fan-policy-save-state').textContent(),/控制异常/);assert.equal(await page.locator('#fan-policy-release').isEnabled(),true);
    await page.locator('[data-web-page="overview"]').click();await page.waitForFunction(()=>document.getElementById('overview-fans').textContent.includes('控制 / 恢复未确认'));assert.ok(!(await page.locator('#overview-fans').textContent()).includes('原厂管理'));await page.locator('[data-web-page="settings"]').click();
    await command('allow_release');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-release').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('两路已恢复原厂管理'));
    checks.push('an unacknowledged vendor restoration never becomes a success label; both channel cards flag restore-unconfirmed and allow an explicit retry');
    await command('reconstruct'); await reload(); await setting('panel');
    assert.equal(await page.locator('#theme').inputValue(), 'jade'); assert.equal(await page.locator('#font_scale').inputValue(), '125'); assert.equal(await page.locator('#page_interval').inputValue(), '15'); assert.equal(await page.locator('#sleep_timeout').inputValue(), '90'); assert.equal(await page.locator('#rgb-card').isHidden(), true);
    assert.ok(actionHeaders.filter(headers => headers['x-r1-csrf']).length >= 6); assert.ok(actionHeaders.every(headers => !headers['x-r1-token']));
    const storage = await page.evaluate(() => [...Object.keys(localStorage), ...Object.keys(sessionStorage)]);
    assert.deepEqual(storage, []);
    checks.push('new real controller reconstructs settings from the same data directory; RGB verification is not persisted; UI uses CSRF without any Electron token or browser storage');

    await page.locator('[data-web-page="apps"]').click();await page.waitForFunction(()=>document.querySelectorAll('#web-containers [data-device-action]').length>0);
    const stopFocused=await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).elementHandle();await stopFocused.focus();await command('app_refresh');await page.waitForFunction(()=>document.querySelector('#web-containers .app-resources').textContent.includes('4.2%'));assert.equal(await stopFocused.evaluate(node=>node===document.activeElement&&node.isConnected),true);
    const appStopBox=await stopFocused.boundingBox();await page.mouse.move(appStopBox.x+appStopBox.width/2,appStopBox.y+appStopBox.height/2);await page.mouse.down();await command('app_refresh');await page.waitForFunction(()=>document.querySelector('#web-containers .app-resources').textContent.includes('5.2%'));await page.mouse.up();await page.waitForFunction(()=>document.getElementById('web-confirm').open);await page.locator('#web-confirm button[value="cancel"]').click();
    await page.locator('#web-containers .application-row').first().getByRole('button',{name:'查看详情 ↗',exact:true}).click();const drawerStop=await page.locator('#app-detail-actions').getByRole('button',{name:'停止',exact:true}).elementHandle();await drawerStop.focus();await command('app_refresh');await page.waitForFunction(()=>document.getElementById('app-detail-fields').textContent.includes('6.2%'));assert.equal(await drawerStop.evaluate(node=>node===document.activeElement&&node.isConnected),true);await screenshot('preview-web-paired-docker-detail-fixture');await page.keyboard.press('Escape');
    const beforeChangedState=actionBodies.length;await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).click();await command('app_paused');await page.waitForFunction(()=>document.querySelector('#web-containers .status-pill').dataset.state==='paused');await page.locator('#web-confirm-accept').click();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('状态已改变'));assert.equal(actionBodies.length,beforeChangedState);assert.equal(await page.locator('#web-containers .application-row').first().locator('[data-device-action]').count(),0);await command('app_running');await page.waitForFunction(()=>document.querySelectorAll('#web-containers .application-row:first-child [data-device-action]').length===2);
    checks.push('keyed container cards and drawer retain keyboard focus and pointerdown through real metric refreshes; paused state exposes no start; confirmation rechecks latest state and emits no write after state changes');
    const beforeAppCancel=actionBodies.length;await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).click();await page.locator('#web-confirm button[value="cancel"]').click();assert.equal(actionBodies.length,beforeAppCancel);
    await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).click();await page.locator('#web-confirm-accept').click();await page.waitForFunction(()=>document.querySelector('#web-containers .application-row').textContent.includes('exited'));
    assert.ok(actionBodies.some(body=>body.action==='container.action'&&body.payload.confirmed===true));
    assert.equal(await page.locator('#web-containers .application-row').nth(1).locator('[data-device-action]').count(),0); assert.match(await page.locator('#web-containers .application-row').nth(1).textContent(),/Toolbox 受保护/);
    checks.push('paired container changes use explicit confirm, cancellation emits no POST, protected toolbox stays disabled, actual fixture state updates');
    await page.locator('#web-containers .application-row').first().getByRole('button',{name:'启动',exact:true}).click();
    const beforeOfflineConfirm=actionBodies.length;
    await page.route('**/api/v1/status',route=>route.abort('connectionrefused'));await page.waitForFunction(()=>document.getElementById('live-status').textContent==='连接中断');
    for(const button of await page.locator('[data-device-action]').all())assert.equal(await button.isDisabled(),true);
    assert.match(await page.locator('#monitor-error').textContent(),/此前读数仅供参考/);
    assert.equal(await page.locator('#web-power-cancel').isDisabled(),true);assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true);assert.equal(await page.locator('#fan-policy-release').isDisabled(),true);assert.equal(await page.locator('#fan-policy-save').isEnabled(),true);assert.equal(await page.locator('[data-section="brightness"] button').isDisabled(),true);assert.equal(await page.locator('#screen-sleep').isDisabled(),true);assert.equal(await page.locator('#light-policy-apply').isDisabled(),true);assert.equal(await page.locator('#light-policy-save').isEnabled(),true);
    await page.locator('#web-confirm-accept').click();assert.equal(actionBodies.length,beforeOfflineConfirm,'confirm after sampling loss must not emit POST');
    await page.unroute('**/api/v1/status');await page.waitForFunction(()=>document.getElementById('live-status').textContent==='实时连接');
    checks.push('lost public sampling disables paired device actions immediately and labels cached readings unconfirmed; reconnect restores capability-based controls');

    await page.locator('[data-web-page="settings"]').click();
    await page.waitForFunction(()=>!document.getElementById('analytics-settings-section').hidden);
    await setting('monitoring'); await page.locator('#history-retention').fill('14');await page.locator('#monitor-cpu-warn').fill('82');await page.locator('#monitor-cpu-critical').fill('96');await page.locator('#analytics-form button[type="submit"]').click();await page.waitForFunction(()=>document.getElementById('analytics-result').textContent.includes('已保存'));
    const analyticsSaved=JSON.parse(fs.readFileSync(path.join(fixtureDir,'data/monitoring.json'),'utf8')).settings;assert.equal(analyticsSaved.retention_days,14);assert.equal(analyticsSaved.thresholds.cpu_warn,82);await reload();assert.equal(await page.locator('#history-retention').inputValue(),'14');
    await setting('lighting'); await page.locator('#light-mode').selectOption('temperature');await page.locator('#light-enabled').check();await page.locator('#light-sources input[value="cpu"]').check();await page.locator('#light-sources input[value="board"]').check();await page.locator('#light-brightness').fill('44');await page.locator('#light-policy-save').click();await page.waitForFunction(()=>document.getElementById('light-policy-result').textContent.includes('尚未应用'));
    const lightSaved=()=>JSON.parse(fs.readFileSync(path.join(fixtureDir,'data/light-policy.json'),'utf8'));assert.deepEqual(lightSaved().config.sources,['cpu','board']);assert.equal(lightSaved().resume_on_boot,false);
    await command('verify_rgb');await reload();page.once('dialog',dialog=>dialog.accept());await page.locator('#light-policy-apply').click();await page.waitForFunction(()=>document.getElementById('light-policy-state').textContent==='运行中');assert.match(await page.locator('#light-policy-live').textContent(),/66 °C/);assert.match(await page.locator('#light-policy-live').textContent(),/主板温度/);assert.equal(lightSaved().resume_on_boot,true);assert.equal(await page.locator('#rgb-card').isVisible(),false,'active light policy must hide immediate RGB overrides');
    await page.locator('#light-night-enabled').check();await page.locator('#light-night-brightness').fill('12');await page.locator('#light-policy-save').click();await page.waitForFunction(()=>document.getElementById('light-policy-result').textContent.includes('尚未应用'));assert.equal(lightSaved().config.schedule.brightness,12);
    await reload();assert.equal(await page.locator('#light-brightness').inputValue(),'44');assert.equal(await page.locator('#light-night-brightness').inputValue(),'12');
    for(const width of [1440,320]){await page.setViewportSize({width,height:1000});await measure('policy-settings-'+width);await screenshot('preview-web-'+width+'-all-settings-fixture');}
    checks.push('real monitoring settings persist and reload independently of safety thresholds; real LightPolicyController saves drafts, applies asynchronously using only an in-memory lamp, max-temperature source and nighttime values survive reload');
    await page.locator('#logout').click(); await page.waitForFunction(() => !document.getElementById('login').hidden);
    assert.equal((await page.request.get(fixture.primary.origin + '/api/session')).status(), 401); assert.ok(!(await context.cookies(fixture.primary.origin)).some(item => item.name === 'r1_web_session'));
    checks.push('real logout revokes the session and clears its cookie');
    await login(fixture.unsupported);
    assert.equal(await page.locator('#screen-card').isHidden(), true); assert.equal(await page.locator('#night-card').isHidden(), true); assert.equal(await page.locator('#rgb-card').isHidden(), true);
    assert.equal(await page.locator('[data-section="appearance"]').isVisible(), true); assert.equal(await page.locator('[data-section="pages"]').isVisible(), true); await measure('320-unsupported-device');
    checks.push('real controller with empty temporary sys/dev hides all unsupported screen/night/RGB modules while keeping appearance/pages usable');
    assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true); assert.equal(await page.locator('#fan-policy-save').isEnabled(),true);
    await setting('fans'); await page.locator('#fan-cpu-mode').selectOption('temperature'); await page.locator('#fan-cpu-sources input[value="cpu"]').check(); await page.locator('#fan-cpu-normal_at').fill('58'); await page.locator('#fan-policy-save').click(); await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('尚未应用'));
    const unsupportedFan=JSON.parse(fs.readFileSync(path.join(fixtureDir,'unsupported/data/fan-policy.json'),'utf8')); assert.equal(unsupportedFan.config.channels.cpu.normal_at,58); assert.equal(unsupportedFan.resume_on_boot,false); assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true);
    checks.push('unverified hardware still permits independent configuration persistence but keeps apply disabled and never claims hardware success');
    assert.equal(await page.locator('#fan-policy-apply').getAttribute('aria-describedby'),'fan-policy-apply-reason');
    await page.waitForFunction(()=>document.getElementById('fan-policy-apply-reason').textContent.includes('模拟设备：原厂风扇配置无法解析'));
    assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true);await page.locator('#fan-cpu-normal_at').fill('59');await page.locator('#fan-system-mode').selectOption('manual');await page.locator('#fan-system-manual_percent').fill('83');
    await screenshot('preview-fan-preflight-reason-fixture');
    const beforePreflightPosts=actionBodies.length;
    await command('unsupported_fan_ready');await page.waitForFunction(()=>!document.getElementById('fan-policy-apply').disabled&&document.getElementById('fan-policy-apply-reason').textContent.startsWith('可应用'));
    assert.equal(await page.locator('#fan-cpu-normal_at').inputValue(),'59');assert.equal(await page.locator('#fan-system-manual_percent').inputValue(),'83');
    await command('unsupported_fan_busy');await page.waitForFunction(()=>document.getElementById('fan-policy-apply').disabled&&document.getElementById('fan-policy-apply-reason').textContent.includes('正在切换控制方式'));
    await command('unsupported_fan_idle');await page.waitForFunction(()=>!document.getElementById('fan-policy-apply').disabled);
    await page.route(fixture.unsupported.origin+'/api/v1/status',route=>route.abort('connectionrefused'));
    await page.waitForFunction(()=>document.getElementById('fan-policy-apply-reason').textContent.includes('监控连接已中断'));
    assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true);assert.equal(await page.locator('#fan-policy-save').isEnabled(),true);
    await page.unroute(fixture.unsupported.origin+'/api/v1/status');await page.waitForFunction(()=>!document.getElementById('fan-policy-apply').disabled&&document.getElementById('fan-policy-apply-reason').textContent.startsWith('可应用'));
    await page.route(fixture.unsupported.origin+'/api/session',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'fixture session state unavailable'})}));
    await page.waitForFunction(()=>document.getElementById('fan-policy-apply-reason').textContent.includes('实时风扇状态暂不可读取'));
    const nextPublic=page.waitForResponse(response=>response.url()===fixture.unsupported.origin+'/api/v1/status');await nextPublic;
    assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true,'cached public snapshot must not bypass failed fan state reading');assert.match(await page.locator('#fan-cpu-live').textContent(),/设置状态未确认/);assert.ok(!(await page.locator('#fan-cpu-live').textContent()).includes('请求输出 0%'));assert.ok(!(await page.locator('#fan-system-live').textContent()).includes('参考 max：'));
    await page.unroute(fixture.unsupported.origin+'/api/session');await page.waitForFunction(()=>!document.getElementById('fan-policy-apply').disabled&&document.getElementById('fan-policy-apply-reason').textContent.startsWith('可应用'));
    assert.equal(await page.locator('#fan-cpu-normal_at').inputValue(),'59');assert.equal(await page.locator('#fan-system-manual_percent').inputValue(),'83');assert.equal(await page.locator('#fan-cpu-sources input[value="cpu"]').isChecked(),true);
    assert.equal(actionBodies.length,beforePreflightPosts);assert.equal(JSON.parse(fs.readFileSync(path.join(fixtureDir,'unsupported/data/fan-policy.json'),'utf8')).config.channels.cpu.normal_at,58);
    checks.push('fan apply reason names exact failed capability, busy switch, offline and failed session read; real capability/session recovery re-enables without applying or overwriting unsaved CPU/system drafts');


    assert.equal(blocked.length, 0, 'unexpected external requests'); assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({ passed: true, checks, sizes, pageErrors, externalRequests: blocked.length, fixtureDirectory: fixtureDir, fixtureFilesRetained: true }, null, 2));
  } finally {
    clearTimeout(bootTimer);
    if (browser) await browser.close();
    if (child.exitCode === null && !child.killed) {
      try { await command('stop'); } catch (_) { child.kill('SIGTERM'); }
      child.stdin.end();
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
