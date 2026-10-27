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
    def capabilities(self):
        return {'available': self.available, 'reason': '' if self.available else '模拟设备：独立驱动未验证',
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
                  'device':{'identity':{'hostname':'R1-Fixture','os':'Fixture Linux','kernel':'6.1-fixture','firmware':'fixture'},
                    'cpu':{'physical_cores':4,'architecture':'x86_64'},'thermal':[{'driver':'coretemp','label':'Package','celsius':62}],
                    'network':[{'name':'eth0','speed_mbps':2500,'ipv6_addresses':[{'address':'2001:db8:1234:5678:90ab:cdef:1234:5678','prefix_length':64,'scope':'global'}]}],
                    'network_config':{'default_routes':{'ipv4':[{'interface':'eth0','gateway':'192.168.1.1','metric':100,'source':'/proc/net/route'}],'ipv6':[{'interface':'eth0','gateway':None,'metric':200,'source':'/proc/net/ipv6_route'}]},'route_status':{'ipv4':'ok','ipv6':'ok'},'dns':{'servers':['192.168.1.1','2001:4860:4860::8888'],'search':['home.arpa'],'domains':['home.arpa'],'status':'ok'}},'storage':{'raid':[]}},
                  'interfaces':[{'name':'eth0','ipv4':['192.168.1.10'],'mac':'00:11:22:33:44:55','operstate':'up','rx_bytes':400000000,'tx_bytes':100000000,'rx_bytes_per_sec':1250000,'tx_bytes_per_sec':480000}],
                  'disks':{'available':True,'items':[{'id':'sda','device':'/dev/sda','model':'Fixture NAS Drive','size_bytes':4e12,'temp_c':39,'standby':False,'health':{'passed':True}}]},
                  'containers':{'available':True,'items':[{'id':'a'*64,'name':'fixture-db','image':'fixture:local','state':'running','protected':False},{'id':'b'*64,'name':'r1-toolbox','image':'fixture:local','state':'exited','protected':True}]},
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
unsupported = WebConfigServer(controller(fixture / 'unsupported'), fan_policy=policy(fixture / 'unsupported', False),light_policy=lights[1], host='127.0.0.1', port=0, asset_dir=repo / 'ui/web').start()

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
      const original = await page.locator('header .eyebrow').textContent();
      await page.locator('header .eyebrow').evaluate(node => { node.textContent += ' · 模拟设备数据'; });
      await page.screenshot({ path: path.join(screenshotDir, name + '.png'), fullPage: true });
      await page.locator('header .eyebrow').evaluate((node, value) => { node.textContent = value; }, original);
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
    const reload = async () => { await page.reload(); await page.waitForFunction(() => !document.getElementById('editor').hidden); };

    await page.goto(fixture.primary.origin + '/#overview');
    await page.waitForFunction(() => document.getElementById('host-name').textContent === 'R1-Fixture');
    const publicStatus = await page.request.get(fixture.primary.origin + '/api/v1/status');
    assert.equal(publicStatus.status(),200); assert.equal(publicStatus.headers()['access-control-allow-origin'],'*');
    assert.equal((await page.request.get(fixture.primary.origin + '/api/session')).status(),401);
    assert.equal(await page.locator('#editor').isVisible(),false);
    assert.match(await page.locator('#overview-fans').textContent(),/RPM 未知/);
    const publicPosts=actionBodies.length;
    for(const width of [1440,1120,760,376,320]) {
      await page.setViewportSize({width,height:1000});
      for(const view of ['overview','trends','storage','network','apps','device']) {
        await page.locator('[data-web-page="'+view+'"]').click();
        if(view==='trends') await page.waitForFunction(()=>!document.getElementById('history-note').textContent.includes('正在读取'));
        await measure('public-'+width+'-'+view);
        if(['overview','network'].includes(view)&&[1440,376,320].includes(width)) await screenshot('preview-web-'+width+'-'+view+'-fixture');
      }
    }
    assert.equal(actionBodies.length,publicPosts,'public navigation must never emit device writes');
    assert.equal(await page.locator('#web-power-card').isVisible(),false);
    await page.locator('[data-web-page="network"]').click();
    assert.match(await page.locator('#network-interfaces').textContent(),/2001:db8:1234:5678:90ab:cdef:1234:5678\/64/);
    assert.match(await page.locator('#network-routes').textContent(),/直连路由（无下一跳网关）/);
    assert.match(await page.locator('#network-dns').textContent(),/默认域名/);
    await page.locator('[data-web-page="apps"]').click();assert.equal(await page.locator('#web-containers [data-device-action]').count(),0);
    checks.push('public real HTTP dashboard requires no pairing; CORS read-only; six monitoring sections at five widths; IPv6/source wrapping and on-link routes; no unauthorized controls or POST');
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

    await page.locator('#theme').selectOption('jade'); await page.locator('#font_scale').fill('125'); await save('appearance');
    assert.equal(persisted().theme, 'jade'); assert.equal(persisted().font_scale, 125); await reload(); assert.equal(await page.locator('#theme').inputValue(), 'jade'); assert.equal(await page.locator('#font_scale').inputValue(), '125');
    for (const node of await page.locator('#pages input').all()) await node.setChecked(['overview', 'network', 'settings'].includes(await node.inputValue()));
    await page.locator('#page_interval').fill('15'); await save('pages'); assert.deepEqual(persisted().enabled_pages, ['overview', 'network', 'settings']); await reload(); assert.equal(await page.locator('#pages input:checked').count(), 3); assert.equal(await page.locator('#page_interval').inputValue(), '15');
    await page.locator('#brightness').fill('65'); await save('brightness'); assert.equal(persisted().brightness, 65); assert.equal(fs.readFileSync(path.join(fixtureDir, 'sys/class/backlight/intel_backlight/brightness'), 'utf8'), '65'); await reload(); assert.equal(await page.locator('#brightness').inputValue(), '65');
    await page.locator('#night_enabled').check(); await page.locator('#night_start').fill('22:30'); await page.locator('#night_end').fill('06:45'); await page.locator('#night_brightness').fill('15'); await save('night'); await reload(); assert.equal(await page.locator('#night_enabled').isChecked(), true); assert.equal(await page.locator('#night_start').inputValue(), '22:30'); assert.equal(await page.locator('#night_end').inputValue(), '06:45'); assert.equal(await page.locator('#night_brightness').inputValue(), '15');
    await page.locator('#sleep_timeout').fill('90'); await save('sleep'); await reload(); assert.equal(persisted().sleep_timeout, 90); assert.equal(await page.locator('#sleep_timeout').inputValue(), '90');
    checks.push('appearance/pages/brightness/night/sleep save through real API, persist to actual fixture settings.json, and survive page reload');

    await command('verify_rgb'); await reload(); assert.equal(await page.locator('#rgb-card').isVisible(), true);
    await page.locator('#rgb-mode').selectOption('3'); await page.locator('#rgb-color').fill('#123456'); await page.locator('#rgb-brightness').fill('35'); await page.locator('[data-section="rgb"] button').click();
    await page.waitForFunction(() => document.querySelector('[data-section="rgb"] .result').textContent.includes('即时应用'));
    assert.ok(!Object.keys(persisted()).some(key => key.startsWith('rgb')));
    await reload(); assert.equal(await page.locator('#rgb-mode').inputValue(), '3'); assert.equal(await page.locator('#rgb-brightness').inputValue(), '35');
    assert.equal(await page.locator('.fan-policy-channel').count(), 2);
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
    await page.locator('#fan-cpu-mode').selectOption('manual');await page.locator('#fan-cpu-manual_percent').fill('82');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-apply').click();await page.waitForFunction(()=>document.getElementById('fan-cpu-live').textContent.includes('82%'));
    await command('fail_release');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-release').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('未生效'));
    assert.match(await page.locator('#fan-cpu-live').textContent(),/恢复未确认/);assert.match(await page.locator('#fan-policy-save-state').textContent(),/控制异常/);assert.equal(await page.locator('#fan-policy-release').isEnabled(),true);
    await page.locator('[data-web-page="overview"]').click();await page.waitForFunction(()=>document.getElementById('overview-fans').textContent.includes('控制 / 恢复未确认'));assert.ok(!(await page.locator('#overview-fans').textContent()).includes('原厂管理'));await page.locator('[data-web-page="settings"]').click();
    await command('allow_release');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-release').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('两路已恢复原厂管理'));
    checks.push('an unacknowledged vendor restoration never becomes a success label; both channel cards flag restore-unconfirmed and allow an explicit retry');
    await command('reconstruct'); await reload();
    assert.equal(await page.locator('#theme').inputValue(), 'jade'); assert.equal(await page.locator('#font_scale').inputValue(), '125'); assert.equal(await page.locator('#page_interval').inputValue(), '15'); assert.equal(await page.locator('#sleep_timeout').inputValue(), '90'); assert.equal(await page.locator('#rgb-card').isHidden(), true);
    assert.ok(actionHeaders.filter(headers => headers['x-r1-csrf']).length >= 6); assert.ok(actionHeaders.every(headers => !headers['x-r1-token']));
    const storage = await page.evaluate(() => [...Object.keys(localStorage), ...Object.keys(sessionStorage)]);
    assert.deepEqual(storage, []);
    checks.push('new real controller reconstructs settings from the same data directory; RGB verification is not persisted; UI uses CSRF without any Electron token or browser storage');

    await page.locator('[data-web-page="apps"]').click();await page.waitForFunction(()=>document.querySelectorAll('#web-containers [data-device-action]').length>0);
    const beforeAppCancel=actionBodies.length;await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).click();await page.locator('#web-confirm button[value="cancel"]').click();assert.equal(actionBodies.length,beforeAppCancel);
    await page.locator('#web-containers .application-row').first().getByRole('button',{name:'停止',exact:true}).click();await page.locator('#web-confirm-accept').click();await page.waitForFunction(()=>document.querySelector('#web-containers .application-row').textContent.includes('exited'));
    assert.ok(actionBodies.some(body=>body.action==='container.action'&&body.payload.confirmed===true));
    assert.equal(await page.locator('#web-containers .application-row').nth(1).getByRole('button',{name:'启动',exact:true}).isDisabled(),true);
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
    await page.locator('#history-retention').fill('14');await page.locator('#monitor-cpu-warn').fill('82');await page.locator('#monitor-cpu-critical').fill('96');await page.locator('#analytics-form button[type="submit"]').click();await page.waitForFunction(()=>document.getElementById('analytics-result').textContent.includes('已保存'));
    const analyticsSaved=JSON.parse(fs.readFileSync(path.join(fixtureDir,'data/monitoring.json'),'utf8')).settings;assert.equal(analyticsSaved.retention_days,14);assert.equal(analyticsSaved.thresholds.cpu_warn,82);await reload();assert.equal(await page.locator('#history-retention').inputValue(),'14');
    await page.locator('#light-mode').selectOption('temperature');await page.locator('#light-enabled').check();await page.locator('#light-sources input[value="cpu"]').check();await page.locator('#light-sources input[value="board"]').check();await page.locator('#light-brightness').fill('44');await page.locator('#light-policy-save').click();await page.waitForFunction(()=>document.getElementById('light-policy-result').textContent.includes('尚未应用'));
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
    await page.locator('#fan-cpu-mode').selectOption('temperature'); await page.locator('#fan-cpu-sources input[value="cpu"]').check(); await page.locator('#fan-cpu-normal_at').fill('58'); await page.locator('#fan-policy-save').click(); await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('尚未应用'));
    const unsupportedFan=JSON.parse(fs.readFileSync(path.join(fixtureDir,'unsupported/data/fan-policy.json'),'utf8')); assert.equal(unsupportedFan.config.channels.cpu.normal_at,58); assert.equal(unsupportedFan.resume_on_boot,false); assert.equal(await page.locator('#fan-policy-apply').isDisabled(),true);
    checks.push('unverified hardware still permits independent configuration persistence but keeps apply disabled and never claims hardware success');

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
