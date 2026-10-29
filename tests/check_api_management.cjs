// API management UI against the real WebConfig fixture and fixed API responses.
// All production panel assets are built into a temporary folder, never ui/web.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-api-browser-'));
const fixtureDir = path.join(temporary, 'r1-web-browser-api');
const buildDir = path.join(temporary, 'panel');
fs.mkdirSync(fixtureDir);
const python = process.env.PYTHON_BIN || 'python3';
const candidates = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
const chromeBin = candidates.find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件');
const build = spawnSync(python, ['scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(build.status, 0, '临时面板构建失败：' + (build.stderr || build.stdout));

// Share the complete hardware-free real fixture without changing the larger
// regression test. Its actual controllers and HTTP server remain unmocked.
const source = fs.readFileSync(path.join(__dirname, 'check_web_config.cjs'), 'utf8');
const fixtureMatch = source.match(/const pythonFixture = String\.raw`([\s\S]*?)`;/);
assert.ok(fixtureMatch, '无法找到现有真实网页 fixture');
const pythonFixture = fixtureMatch[1].replaceAll("asset_dir=repo / 'ui/web'", 'asset_dir=Path(sys.argv[3])');

(async () => {
  const child = spawn(python, ['-u', '-c', pythonFixture, root, fixtureDir, buildDir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = '', browser, readyResolve, readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const timer = setTimeout(() => readyReject(new Error('本地网页 fixture 启动超时')), 20000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-6000); });
  child.on('error', readyReject);
  child.on('exit', code => { if (code) readyReject(new Error('网页 fixture 退出：' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try { const value = JSON.parse(line); if (value.ready) { clearTimeout(timer); readyResolve(value); } } catch (_) { /* fixture log */ }
  });
  const checks = [], pageErrors = [];
  const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
  const secret = 'r1-fixture-secret-ONLY-ONCE-<img src=x>';
  const unsafeName = '<img src=x onerror="window.apiNameInjected=1">客厅 AI';
  const calls = [];
  let reads = 0, failCreate = false, failRead = false;
  const state = {
    ok: true, enabled: false, storage_ok: true, keys: [],
    scope_catalog: [
      { id: 'read', label: '读取状态', description: '读取监控与设备信息' },
      { id: 'fan', label: '风扇策略', description: '设置风扇策略' },
      { id: 'light', label: '机身灯光', description: '修改灯光' },
      { id: 'screen', label: '小屏显示', description: '屏幕与亮度' },
      { id: 'power', label: '电源操作', description: '关机或重启' },
      { id: 'media', label: '媒体状态', description: '查询原厂媒体任务' },
    ],
    audit: [{ timestamp: 1793240000, key_id: 'fixture-key-1', key_name: unsafeName,
      operation: 'get_status', peer: '192.168.31.100', outcome: 'ok', duration_ms: 12 }],
    endpoints: { mcp: '/mcp', status: '/api/integration/status', action: '/api/integration/action' },
    protocol_versions: ['2025-03-26'],
  };
  try {
    const fixture = await ready, origin = fixture.primary.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.addInitScript(() => {
      window.__apiStorageWrites = []; window.__apiCopied = [];
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) { window.__apiStorageWrites.push([String(key), String(value)]); return set.call(this, key, value); };
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__apiCopied.push(text); } } });
    });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin && url.protocol !== 'data:') return route.abort('blockedbyclient');
      if (url.pathname !== '/api/access' && !url.pathname.startsWith('/api/access/')) return route.continue();
      const reply = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (request.method() === 'GET') {
        reads += 1;
        if (failRead) return reply(503, { error: 'fixture：配置读取失败' });
        return reply(200, state);
      }
      const body = request.postDataJSON();
      calls.push({ path: url.pathname, body, headers: request.headers() });
      if (url.pathname === '/api/access/settings') { state.enabled = body.enabled; return reply(200, { ok: true }); }
      if (url.pathname === '/api/access/keys') {
        if (failCreate) return reply(429, { error: 'fixture：密钥创建失败' });
        const key = { id: 'fixture-key-' + (state.keys.length + 1), ...body, created_at: 1793240000,
          expires_at: body.expires_days === 0 ? null : 1801016000, revoked_at: null, last_used_at: null, status: 'active' };
        state.keys.push(key); return reply(200, { ok: true, key, token: secret });
      }
      if (url.pathname === '/api/access/keys/revoke') {
        const key = state.keys.find(item => item.id === body.id);
        assert.ok(key, '撤销已有 key'); key.status = 'revoked'; key.revoked_at = 1793240100;
        return reply(200, { ok: true });
      }
      return reply(404, { error: 'unknown fixture route' });
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin + '/#/settings');
    await page.waitForSelector('#set-r1-api');
    check('设置中有 API 管理入口', await page.locator('#setNav [data-g="r1-api"]').count() === 1);
    check('构建设置不自动获取密钥列表', reads === 0);
    await page.locator('#set-r1-api [data-ref="open"]').click();
    const manager = page.locator('#r1ApiManager');
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('服务默认关闭', !(await manager.locator('[data-ref="enabled"]').isChecked()));
    check('默认仅勾选读取状态', JSON.stringify(await manager.locator('[data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    check('默认有效期 90 天', await manager.locator('[data-ref="expiry"]').inputValue() === '90');
    check('MCP 地址与当前设备一致', await manager.locator('[data-ref="mcp"]').inputValue() === origin + '/mcp');
    check('安全调用记录可见', (await manager.locator('[data-ref="audit"]').innerText()).includes('192.168.31.100') && (await manager.locator('[data-ref="audit"]').innerText()).includes('get_status'));

    await manager.locator('[data-ref="enabled"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="serviceStatus"]').textContent === '已开启');
    check('启用服务保持 JSON 与 CSRF 请求头', calls[0].body.enabled === true && calls[0].headers['x-fnm-request'] === '1' && calls[0].headers['content-type'] === 'application/json');

    await manager.locator('input[data-ref="name"]').fill(unsafeName);
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();
    await manager.locator('[data-ref="ips"]').fill('192.168.31.100\nfd00::2');
    await manager.locator('[data-ref="create"]').click();
    await page.locator('.fullmask [data-yes]').click();
    const secretDialog = page.locator('#r1ApiSecret');
    await secretDialog.waitFor();
    const secretField = await secretDialog.locator('[data-ref="token"]').elementHandle();
    check('密钥只在创建对话框中显示', await secretDialog.locator('[data-ref="token"]').inputValue() === secret);
    const config = JSON.parse(await secretDialog.locator('[data-ref="config"]').inputValue());
    check('MCP 配置包含对应 URL 和 Bearer 请求头', config.mcpServers.toolbox.url === origin + '/mcp' && config.mcpServers.toolbox.headers.Authorization === 'Bearer ' + secret);
    const created = calls.find(call => call.path === '/api/access/keys');
    check('创建提交明确功能、来源与有效期', JSON.stringify(created.body.scopes) === '["read","fan"]' && created.body.allowed_ips.length === 2 && created.body.expires_days === 90);
    await secretDialog.locator('[data-ref="copyConfig"]').click();
    check('复制 MCP 配置', await page.evaluate(() => window.__apiCopied.some(text => JSON.parse(text).mcpServers.toolbox.url.endsWith('/mcp'))));
    await page.evaluate(() => { Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }); document.execCommand = () => false; });
    await secretDialog.locator('[data-ref="copyToken"]').click();
    check('HTTP 复制失败时保留可手动复制文本', (await secretDialog.locator('[data-ref="copyStatus"]').innerText()).includes('请手动复制') && await secretDialog.locator('[data-ref="token"]').evaluate(input => input.selectionStart === 0 && input.selectionEnd === input.value.length));
    await secretDialog.locator('[data-ref="close"]').click();
    check('关闭后清除临时密钥字段', await secretField.inputValue() === '' && await page.locator('#r1ApiSecret').count() === 0);
    await manager.locator('.r1-api-item').waitFor();
    check('名称作为文本展示且不注入 HTML', await manager.locator('.r1-api-item img').count() === 0 && (await manager.locator('.r1-api-item').innerText()).includes(unsafeName));
    check('未知最后使用时间不显示假零值', (await manager.locator('.r1-api-item [data-ref="used"]').innerText()) === '尚无记录');
    const beforePoll = reads;
    await page.waitForTimeout(11000);
    check('定时状态轮询不重复获取访问密钥', reads === beforePoll);
    await manager.locator('[data-ref="refresh"]').click();
    await page.waitForFunction(expected => window.document.querySelector('#r1ApiManager').getAttribute('aria-busy') === 'false', null);
    check('按需刷新不再次显示密钥', reads === beforePoll + 1 && await page.locator('#r1ApiSecret').count() === 0);
    check('密钥未写入浏览器存储', !(await page.evaluate(() => JSON.stringify(window.__apiStorageWrites))).includes(secret));
    check('安全元数据列表不包含密钥', !(await manager.innerText()).includes(secret));

    await manager.locator('.r1-api-item [data-ref="revoke"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await manager.locator('.r1-api-item [data-ref="status"]').filter({ hasText: '已撤销' }).waitFor();
    check('撤销后刷新安全元数据', calls.some(call => call.path === '/api/access/keys/revoke') && await manager.locator('.r1-api-item [data-ref="revoke"]').isHidden());

    failCreate = true;
    await manager.locator('input[data-ref="name"]').fill('失败测试');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '密钥创建失败' }).waitFor();
    check('创建失败可见且不展示旧密钥', await page.locator('#r1ApiSecret').count() === 0 && !(await manager.locator('[data-ref="create"]').isDisabled()));
    failCreate = false; failRead = true;
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '配置读取失败' }).waitFor();
    check('读取失败反馈具体原因', !(await manager.locator('[data-ref="refresh"]').isDisabled()));
    failRead = false;

    await manager.locator('input[data-ref="name"]').fill('长期 AI');
    await manager.locator('[data-ref="expiry"]').selectOption('0');
    await manager.locator('[data-ref="scopes"] input[value="power"]').check();
    await manager.locator('[data-ref="create"]').click();
    check('授予电源权限时明确说明', (await page.locator('.fullmask').last().innerText()).includes('电源权限可安排关机或重启'));
    await page.locator('.fullmask [data-yes]').click();
    await page.locator('#r1ApiSecret').waitFor();
    const escapedField = await page.locator('#r1ApiSecret [data-ref="config"]').elementHandle();
    await page.keyboard.press('Escape');
    check('Escape 清理密钥而保留管理页', await escapedField.inputValue() === '' && await manager.count() === 1 && await page.locator('#r1ApiSecret').count() === 0);
    await manager.locator('.r1-api-item').last().waitFor();
    check('长期有效期明确提交零值', calls.filter(call => call.path === '/api/access/keys').at(-1).body.expires_days === 0 && (await manager.locator('.r1-api-item').last().innerText()).includes('长期'));

    await page.setViewportSize({ width: 320, height: 740 });
    check('320px 管理页面不横向溢出', await manager.evaluate(element => [...element.querySelectorAll('.r1-api-dialog, .r1-api-item, input, textarea')].every(node => node.getBoundingClientRect().right <= 321 && node.getBoundingClientRect().left >= -1)));
    await manager.locator('[data-ref="enabled"]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="serviceStatus"]').textContent === '已关闭');
    check('关闭服务可立即提交', calls.at(-1).path === '/api/access/settings' && calls.at(-1).body.enabled === false);
    await manager.locator('[data-ref="close"]').click();
    await page.locator('#set-r1-api [data-ref="open"]').click();
    await page.locator('#r1ApiManager [data-ref="content"]:not([hidden])').waitFor();
    check('重新打开只返回元数据', await page.locator('#r1ApiSecret').count() === 0 && !(await page.locator('#r1ApiManager').innerText()).includes(secret));
    check('名称未执行脚本', await page.evaluate(() => !window.apiNameInjected));
    check('页面无脚本异常', pageErrors.length === 0);
    console.log(JSON.stringify({ ok: true, checks, fixture: fixtureDir, reads, writes: calls.length }, null, 2));
  } catch (error) {
    console.error(JSON.stringify({ ok: false, checks, pageErrors, error: String(error && error.stack || error), stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
    if (browser) await browser.close().catch(() => {});
    child.kill('SIGTERM');
  }
})();
