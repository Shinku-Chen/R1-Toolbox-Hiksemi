// Service switches use a real local panel fixture and fixed service responses.
// No NAS, display manager, Docker socket or developer command is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-service-browser-'));
const fixtureDir = path.join(temporary, 'r1-web-browser-services'), buildDir = path.join(temporary, 'panel');
fs.mkdirSync(fixtureDir);
const python = process.env.PYTHON_BIN || 'python3';
const chromeBin = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请设置 CHROME_BIN');
const build = spawnSync(python, ['scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(build.status, 0, '临时面板构建失败：' + (build.stderr || build.stdout));
const source = fs.readFileSync(path.join(__dirname, 'check_web_config.cjs'), 'utf8');
const fixtureMatch = source.match(/const pythonFixture = String\.raw`([\s\S]*?)`;/);
assert.ok(fixtureMatch, '无法找到真实网页 fixture');
const pythonFixture = fixtureMatch[1].replaceAll("asset_dir=repo / 'ui/web'", 'asset_dir=Path(sys.argv[3])');

(async () => {
  const child = spawn(python, ['-u', '-c', pythonFixture, root, fixtureDir, buildDir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, stderr = '', readyResolve, readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const timer = setTimeout(() => readyReject(new Error('本地服务设置 fixture 启动超时')), 20000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-6000); });
  child.on('error', readyReject);
  child.on('exit', code => { if (code) readyReject(new Error('网页 fixture 退出：' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try { const value = JSON.parse(line); if (value.ready) { clearTimeout(timer); readyResolve(value); } } catch (_) { /* fixture log */ }
  });
  const checks = [], pageErrors = [], calls = [];
  const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
  let serviceReads = 0, accessReads = 0, failRead = false, failScreen = false, screenError = false;
  let finishScreenOnRead = false;
  const state = {
    screen: { enabled: true, active: true, busy: false, available: true, last_error: null },
    mcp: { enabled: false },
    developer: { enabled: false, active: false, allowed_ips: [], minutes: 60,
      expires_at: null, allow_commands: false, reason: '开发模式未开启。' },
  };
  try {
    const fixture = await ready, origin = fixture.primary.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin && url.protocol !== 'data:') return route.abort('blockedbyclient');
      const reply = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/access' && request.method() === 'GET') {
        accessReads += 1;
        return reply(200, { ok: true, storage_ok: true, enabled: state.mcp.enabled, keys: [], audit: [],
          role_catalog: [{ id: 'readonly', label: '只读' }, { id: 'readwrite', label: '读写' }, { id: 'admin', label: '管理员' }],
          scope_catalog: [{ id: 'read', label: '读取状态', min_role: 'readonly', implemented: true }],
          file_root_catalog: { available: false, roots: [], reason: 'fixture：没有挂载' } });
      }
      if (url.pathname !== '/api/services' && !url.pathname.startsWith('/api/services/')) return route.continue();
      if (request.method() === 'GET') {
        serviceReads += 1;
        if (failRead) return reply(503, { error: 'fixture：服务状态不可读' });
        if (finishScreenOnRead && state.screen.busy) {
          state.screen.active = state.screen.enabled; state.screen.busy = false; finishScreenOnRead = false;
        }
        return reply(200, { ok: true, ...state });
      }
      const body = request.postDataJSON(); calls.push({ path: url.pathname, body, headers: request.headers() });
      if (url.pathname === '/api/services/screen') {
        if (failScreen) return reply(503, { error: 'fixture：屏幕切换请求失败' });
        state.screen.enabled = body.enabled;
        if (screenError) {
          state.screen.busy = false; state.screen.last_error = 'fixture：官方窗口恢复未确认';
          return reply(503, { ok: false, saved: true, error: 'fixture：小屏偏好已保存，切换未完成', services: state });
        }
        else { state.screen.busy = true; state.screen.last_error = null; finishScreenOnRead = true; }
      } else if (url.pathname === '/api/services/mcp') state.mcp.enabled = body.enabled;
      else if (url.pathname === '/api/services/developer') {
        if (body.allowed_ips.includes('0.0.0.0/0')) return reply(400, { error: 'fixture：白名单网段不得覆盖公网' });
        state.developer = { ...body, active: body.enabled && body.allowed_ips.includes('127.0.0.1/32'),
          expires_at: body.enabled ? 1893456000 : null,
          reason: !body.enabled ? '开发模式未开启。' : body.allowed_ips.includes('127.0.0.1/32') ? null : '当前来源不在白名单。' };
      } else return reply(404, { error: '未知服务设置' });
      return reply(200, { ok: true, services: state });
    });
    const page = await context.newPage(); page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin + '/#/settings'); await page.waitForSelector('#set-r1-services');
    check('设置具有独立服务与开发入口', await page.locator('#setNav [data-g="r1-services"]').count() === 1);
    check('入口明确常驻容器小屏默认开MCP默认关首次免口令', (await page.locator('#set-r1-services').innerText()).includes('容器持续')
      && (await page.locator('#set-r1-services').innerText()).includes('小屏默认开启')
      && (await page.locator('#set-r1-services').innerText()).includes('MCP 默认关闭')
      && (await page.locator('#set-r1-services').innerText()).includes('首次使用无需网页口令'));
    check('普通设置渲染不读取服务管理或密钥', serviceReads === 0 && accessReads === 0);
    await page.locator('#set-r1-services [data-ref="open"]').click();
    const manager = page.locator('#r1ServiceManager');
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('默认小屏开而MCP和开发关闭', await manager.locator('[data-ref="screenEnabled"]').isChecked()
      && !(await manager.locator('[data-ref="mcpEnabled"]').isChecked())
      && !(await manager.locator('[data-ref="developerEnabled"]').isChecked()));
    check('实际小屏接管状态由服务端显示', (await manager.locator('[data-ref="screenState"]').innerText()).includes('Toolbox 小屏已接管'));
    check('开发默认时窗60分钟且无命令授权', await manager.locator('[data-ref="developerMinutes"]').inputValue() === '60'
      && !(await manager.locator('[data-ref="developerCommands"]').isChecked()));
    check('打开服务管理仍不读取密钥', accessReads === 0);

    await manager.locator('[data-ref="screenEnabled"]').click();
    await manager.locator('[data-ref="screenState"]').filter({ hasText: '正在切换' }).waitFor();
    check('保存关闭意图后不会误报已恢复官方', !(await manager.locator('[data-ref="screenEnabled"]').isChecked())
      && (await manager.locator('[data-ref="screenState"]').innerText()).includes('当前实际显示 Toolbox')
      && await manager.locator('[data-ref="screenEnabled"]').isDisabled());
    await manager.locator('[data-ref="screenState"]').filter({ hasText: '实际状态：官方界面' }).waitFor();
    check('短期轮询核对真实状态再显示恢复完成', serviceReads >= 2 && !(await manager.locator('[data-ref="screenEnabled"]').isDisabled()));
    const stableReads = serviceReads;
    await page.waitForTimeout(2200);
    check('稳定后停止服务状态轮询且不读取密钥', serviceReads === stableReads && accessReads === 0);

    failScreen = true;
    await manager.locator('[data-ref="screenEnabled"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '屏幕切换请求失败' }).waitFor();
    check('请求失败保留实际官方状态和关闭开关', !(await manager.locator('[data-ref="screenEnabled"]').isChecked())
      && (await manager.locator('[data-ref="screenState"]').innerText()).includes('官方界面'));
    failScreen = false; screenError = true;
    await manager.locator('[data-ref="screenEnabled"]').click();
    await manager.locator('[data-ref="screenState"]').filter({ hasText: '切换未完成' }).waitFor();
    check('503保存意图但执行失败时只读核对后如实显示', await manager.locator('[data-ref="screenEnabled"]').isChecked()
      && (await manager.locator('[data-ref="screenState"]').innerText()).includes('恢复未确认'));
    screenError = false;
    state.screen = { enabled: true, active: false, busy: false, available: false, last_error: 'fixture：screen_manager 未初始化' };
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="screenState"]').filter({ hasText: 'screen_manager' }).waitFor();
    check('缺少screen_manager不能伪造切换能力', await manager.locator('[data-ref="screenEnabled"]').isDisabled());

    await manager.locator('[data-ref="mcpEnabled"]').click();
    await manager.locator('[data-ref="mcpState"]').filter({ hasText: '已开启' }).waitFor();
    check('MCP独立开启且仍说明Token授权', state.mcp.enabled === true && (await manager.locator('[data-ref="mcpState"]').innerText()).includes('独立 Token'));
    await manager.locator('[data-ref="mcpEnabled"]').click();
    await manager.locator('[data-ref="mcpState"]').filter({ hasText: '已有密钥保留' }).waitFor();
    check('MCP关闭使用单独服务接口且不删除密钥', calls.filter(call => call.path === '/api/services/mcp').length === 2 && accessReads === 0);

    await manager.locator('[data-ref="developerEnabled"]').check();
    const writesBeforeRules = calls.length;
    await manager.locator('[data-ref="saveDeveloper"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '必须填写至少一个' }).waitFor();
    check('开发模式强制非空来源白名单不提交空规则', calls.length === writesBeforeRules && state.developer.enabled === false);
    for (const invalid of ['*', 'nas.example.local', '192.168.31.2\n192.168.31.2']) {
      await manager.locator('[data-ref="developerIps"]').fill(invalid);
      await manager.locator('[data-ref="saveDeveloper"]').click();
      await manager.locator('[data-ref="error"]').filter({ hasText: '来源须为' }).waitFor();
    }
    check('域名通配符重复规则均在本地拒绝', calls.length === writesBeforeRules);
    await manager.locator('[data-ref="developerIps"]').fill('0.0.0.0/0');
    await manager.locator('[data-ref="saveDeveloper"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '白名单网段不得覆盖公网' }).waitFor();
    check('后端拒绝公网网段不误报开发授权', state.developer.active === false
      && !(await manager.locator('[data-ref="developerState"]').innerText()).includes('开发窗口有效'));
    await manager.locator('[data-ref="developerIps"]').fill('192.168.31.0/24\nfd00::/64\n192.168.32.0/255.255.255.0');
    await manager.locator('[data-ref="developerMinutes"]').fill('120');
    await manager.locator('[data-ref="saveDeveloper"]').click();
    await manager.locator('[data-ref="developerState"]').filter({ hasText: '当前来源不在白名单' }).waitFor();
    const networkRequest = calls.filter(call => call.path === '/api/services/developer').at(-1).body;
    check('开发白名单提交CIDR点分掩码时窗和显式命令开关', networkRequest.enabled === true && networkRequest.minutes === 120
      && networkRequest.allow_commands === false && networkRequest.allowed_ips.length === 3);
    check('保存开启意图不等于窗口已生效', (await manager.locator('[data-ref="developerState"]').innerText()).includes('开发窗口尚未生效'));
    await manager.locator('[data-ref="developerIps"]').fill('127.0.0.1/32');
    await manager.locator('[data-ref="developerCommands"]').check();
    await manager.locator('[data-ref="saveDeveloper"]').click();
    await manager.locator('[data-ref="developerState"]').filter({ hasText: '开发窗口有效' }).waitFor();
    check('有效窗口显示截止时间且说明仍需命中白名单', state.developer.active === true && state.developer.allow_commands === true
      && (await manager.locator('[data-ref="developerState"]').innerText()).includes('截止时间')
      && (await manager.locator('[data-ref="developerState"]').innerText()).includes('仍须命中来源白名单')
      && await manager.locator('[data-ref="developerIps"]').inputValue() === '127.0.0.1/32');
    check('所有服务写请求保留JSON与CSRF头', calls.every(call => call.headers['x-fnm-request'] === '1'
      && call.headers['content-type'] === 'application/json'));

    await page.setViewportSize({ width: 320, height: 740 });
    check('320px服务表单和状态不横向溢出', await manager.evaluate(element => [...element.querySelectorAll('.r1-api-dialog, input, textarea, .r1-service-item')]
      .every(node => node.getBoundingClientRect().right <= 321 && node.getBoundingClientRect().left >= -1)));
    failRead = true;
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '服务状态不可读' }).waitFor();
    check('服务读取失败可重试且保留上次真实状态', !(await manager.locator('[data-ref="refresh"]').isDisabled())
      && (await manager.locator('[data-ref="developerState"]').innerText()).includes('开发窗口有效'));
    failRead = false;
    state.screen = { enabled: false, active: true, busy: true, available: true, last_error: null };
    finishScreenOnRead = false;
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="screenState"]').filter({ hasText: '正在切换' }).waitFor();
    await manager.locator('[data-ref="close"]').click();
    const readsAtClose = serviceReads;
    await page.waitForTimeout(2200);
    check('关闭管理窗口立即停止切换轮询', serviceReads === readsAtClose && await page.locator('#r1ServiceManager').count() === 0);
    failRead = true;
    await page.locator('#set-r1-services [data-ref="open"]').click();
    await manager.locator('[data-ref="retry"]:not([hidden])').waitFor();
    check('首次读取失败有可直接重试入口', (await manager.locator('[data-ref="error"]').innerText()).includes('服务状态不可读'));
    failRead = false;
    await manager.locator('[data-ref="retry"]').click();
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    await manager.locator('[data-ref="api"]').click();
    await page.locator('#r1ApiManager [data-ref="content"]:not([hidden])').waitFor();
    check('跳转API管理才按需读取密钥元数据', await page.locator('#r1ServiceManager').count() === 0 && accessReads === 1);
    check('页面无脚本异常', pageErrors.length === 0);
    console.log(JSON.stringify({ ok: true, checks, fixture: fixtureDir, serviceReads, accessReads, writes: calls.length }, null, 2));
  } catch (error) {
    console.error(JSON.stringify({ ok: false, checks, pageErrors, error: String(error && error.stack || error), stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
    if (browser) await browser.close().catch(() => {});
    child.kill('SIGTERM');
  }
})();
