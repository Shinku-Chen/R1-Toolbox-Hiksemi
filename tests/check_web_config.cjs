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
                  'disks':{'available':True,'items':[{'id':'sda','device':'/dev/sda','model':'Fixture NAS Drive','size_bytes':4e12,'temp_c':39,'standby':False,'smart_available':True,'health':{'passed':True}}]},
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
  let stderr = '', bootResolve, bootReject, browser;
  const ready = new Promise((resolve, reject) => { bootResolve = resolve; bootReject = reject; });
  const bootTimer = setTimeout(() => bootReject(new Error('本地测试网页服务启动超时')), 15000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-4096); });
  child.on('error', bootReject);
  child.on('exit', code => { if (code) bootReject(new Error('本地网页 fixture 退出：' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(bootTimer); bootResolve(value); }
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
    const fanResponses = [];
    page.on('response', async r => {
      if (!r.url().endsWith('/api/r1/fan')) return;
      let body = null; try { body = await r.json(); } catch (e) { body = null; }
      fanResponses.push({ status: r.status(), body });
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

    /* 1. 面板加载 */
    await page.goto(origin + '/#/overview');
    await page.waitForFunction(() => document.getElementById('hostName') && document.getElementById('hostName').textContent === 'R1-Fixture', null, { timeout: 15000 });
    check('面板标题', (await page.title()).includes('R1 Toolbox'), await page.title());
    check('总览模块已渲染', await page.locator('#layout-overview section[data-mod="metrics"]').count() === 1);
    check('指标卡有真实读数', (await page.locator('[data-m="cpu"] .m-val .dm').first().getAttribute('aria-label')) !== '--');
    check('R1 双路风扇模块', await page.locator('#layout-overview section[data-mod="r1cool"]').count() === 1);
    check('R1 灯光模块', await page.locator('#layout-overview section[data-mod="r1light"]').count() === 1);
    check('R1 小屏与电源模块', await page.locator('#layout-overview section[data-mod="r1screen"]').count() === 1);
    await page.locator('section[data-mod="r1cool"]').scrollIntoViewIfNeeded();
    await page.waitForSelector('section[data-mod="r1cool"] [data-ch="cpu"]', { timeout: 10000 });
    check('双路风扇名称', /CPU 风扇/.test(await page.locator('section[data-mod="r1cool"]').textContent()));
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
    check('温度来自 hwmon', !!overview.body && !!overview.body.temp && overview.body.temp.cpu === 62);
    check('封装功耗缺失时不伪造', !!overview.body && !!overview.body.power && overview.body.power.ok === false && overview.body.power.total === null);
    const system = await api('/api/system');
    check('固件版本来自官方元数据', !!system.body && system.body.fnos_version === 'V6.3.3_Build_260907');
    const sensors = await api('/api/sensors');
    check('温度传感器接口', !!sensors.body && sensors.body.available === true && sensors.body.temps.length >= 1);
    const docker = await api('/api/docker');
    check('容器列表映射', !!docker.body && docker.body.available === true && docker.body.containers.length === 2);
    const health = await api('/api/disk_health');
    check('盘位分级来自后台 SMART', !!health.body && !!health.body.items && !!health.body.items.sda && health.body.items.sda.grade === 'ok');
    const traffic = await api('/api/traffic');
    check('流量接口', !!traffic.body && !!traffic.body.totals && traffic.body.totals.basis === 'counter_deltas');

    /* 2b. 各详情抽屉与交互不产生脚本错误 */
    for (const sheet of ['cpu', 'mem', 'storage', 'net', 'temp', 'power']) {
      await page.locator(`[data-m="${sheet === 'storage' ? 'disk' : sheet === 'power' ? 'pwr' : sheet}"]`).click();
      await page.waitForSelector('#sheet.on', { timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(150);
      await page.locator('#sheetClose').click();
    }
    await page.locator('.ring-wrap').click();
    await page.waitForTimeout(150);
    await page.locator('#sheetClose').click();
    await page.locator('section[data-mod="storage"]').scrollIntoViewIfNeeded();
    await page.waitForSelector('section[data-mod="storage"] .bay', { timeout: 8000 });
    await page.locator('section[data-mod="storage"] .bay').first().click();
    await page.waitForTimeout(200);
    await page.locator('#sheetClose').click();
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
    await page.locator('#pageSeg button[data-page="docker"]').click();
    await page.waitForSelector('#page-docker .ctr[data-name="fixture-db"]', { timeout: 15000 });
    check('容器页显示 fixture 容器', /fixture-db/.test(await page.locator('#page-docker').textContent()));
    await page.locator('#page-docker .ctr[data-name="fixture-db"]').click();
    await page.waitForTimeout(200);
    check('容器详情显示资源与端口', /fixture:local/.test(await page.locator('#sheetBody').textContent()));
    await page.locator('#sheetClose').click();
    await noOverflow('容器');
    await page.setViewportSize({ width: 390, height: 844 });
    await noOverflow('容器 390');

    /* 6. 设置页与配对解锁 */
    await page.setViewportSize({ width: 1120, height: 1000 });
    await page.locator('#pageSeg button[data-page="settings"]').click();
    await page.waitForFunction(() => document.getElementById('set-r1-fan'));
    for (const id of ['r1-fan', 'r1-light', 'r1-screen', 'r1-monitor', 'look', 'r1-auth', 'r1-conn', 'about']) {
      check('设置分组 ' + id, await page.locator('#set-' + id).count() === 1);
    }
    const lightCard = page.locator('#set-r1-light');
    const lightOptions = await lightCard.locator('.r1-editor select').first().locator('option').allTextContents();
    check('灯光只保留四种原生模式', JSON.stringify(lightOptions) === JSON.stringify(['关闭', '常亮', '彩虹', '呼吸']), lightOptions.join(','));
    check('网页面板不提供温度联动编辑', !/温度联动|温度设备|颜色节点/.test(await lightCard.textContent()));
    const fanCard = page.locator('#set-r1-fan');
    const modeOptions = await fanCard.locator('.r1-editor select').first().locator('option').allTextContents();
    check('风扇转速五档', ['自动', '低档', '中档', '高档', '手动'].every(label => modeOptions.includes(label)), modeOptions.join(','));
    await fanCard.locator('.r1-editor select').first().selectOption('manual');
    check('手动模式才显示设备多选', await fanCard.locator('.r1-ms').count() === 1 && await fanCard.locator('.r1-sources').count() === 0);
    check('手动模式显示温控曲线', await fanCard.locator('.r1-curve').count() === 1);
    check('设备多选默认收起', await fanCard.locator('.r1-ms-menu').isHidden());
    await fanCard.locator('.r1-ms-toggle').first().click();
    check('设备下拉可展开', await fanCard.locator('.r1-ms-menu').first().isVisible() && await fanCard.locator('.r1-ms-row').count() >= 1);
    await fanCard.locator('.r1-ms-row input').first().check();
    await shot('fan-manual');
    await page.keyboard.press('Escape');
    await fanCard.locator('.r1-editor select').first().selectOption('low');
    check('低/中/高档不显示设备与曲线', await fanCard.locator('.r1-ms').count() === 0 && await fanCard.locator('.r1-curve').count() === 0);
    await fanCard.locator('.r1-editor select').first().selectOption('manual');
    check('切回手动保留设备多选', await fanCard.locator('.r1-ms').count() === 1);
    check('配置只保留保存按钮', await fanCard.locator('[data-ref="save"]').count() === 1 && await fanCard.locator('[data-ref="apply"]').count() === 0);
    await fanCard.locator('[data-ref="save"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await page.waitForSelector('#r1Pair', { timeout: 8000 });
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
    if (fanRecord && fanRecord.status === 200) {
      check('风扇配置已写入', fanRecord.body.fan_policy.config.channels.cpu.mode === 'temperature' && fanRecord.body.fan_policy.config.channels.cpu.sources.length === 1);
    }
    const fanState = await api('/api/r1');
    check('温控状态接口可读', !!fanState.body && !!fanState.body.fan && !!fanState.body.fan.capability);
    await shot('settings');

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
