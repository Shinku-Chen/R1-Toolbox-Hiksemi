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
primary = WebConfigServer(controller(fixture), fan_policy=policy(fixture, True), host='127.0.0.1', port=0, asset_dir=repo / 'ui/web').start()
unsupported = WebConfigServer(controller(fixture / 'unsupported'), fan_policy=policy(fixture / 'unsupported', False), host='127.0.0.1', port=0, asset_dir=repo / 'ui/web').start()
try:
    print(json.dumps({'ready': True, 'primary': {'origin': 'http://127.0.0.1:' + str(primary.port), 'code': primary.pairing_info(rotate=True)['code']},
                      'unsupported': {'origin': 'http://127.0.0.1:' + str(unsupported.port), 'code': unsupported.pairing_info(rotate=True)['code']}}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request['operation']
        if operation == 'verify_rgb':
            primary.controller._probe_rgb(4)
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
      await page.goto(pair.origin); await initialSession;
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

    await page.goto(fixture.primary.origin);
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
    await command('allow_release');page.once('dialog',dialog=>dialog.accept());await page.locator('#fan-policy-release').click();await page.waitForFunction(()=>document.getElementById('fan-policy-result').textContent.includes('两路已恢复原厂管理'));
    checks.push('an unacknowledged vendor restoration never becomes a success label; both channel cards flag restore-unconfirmed and allow an explicit retry');
    await command('reconstruct'); await reload();
    assert.equal(await page.locator('#theme').inputValue(), 'jade'); assert.equal(await page.locator('#font_scale').inputValue(), '125'); assert.equal(await page.locator('#page_interval').inputValue(), '15'); assert.equal(await page.locator('#sleep_timeout').inputValue(), '90'); assert.equal(await page.locator('#rgb-card').isHidden(), true);
    assert.ok(actionHeaders.filter(headers => headers['x-r1-csrf']).length >= 6); assert.ok(actionHeaders.every(headers => !headers['x-r1-token']));
    const storage = await page.evaluate(() => [...Object.keys(localStorage), ...Object.keys(sessionStorage)]);
    assert.deepEqual(storage, []);
    checks.push('new real controller reconstructs settings from the same data directory; RGB verification is not persisted; UI uses CSRF without any Electron token or browser storage');

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
