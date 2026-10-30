// Real panel + owner HTTP with an in-memory plugin; never NAS or actual media DBs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-media-sort-browser-'));
const buildDir = path.join(temporary, 'panel');
const python = process.env.PYTHON_BIN || 'python3';
const chromeBin = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(value => value && fs.existsSync(value));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请设置 CHROME_BIN');
const build = spawnSync(python, ['-B', 'scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(build.status, 0, '临时面板构建失败：' + (build.stderr || build.stdout));

(async () => {
  const child = spawn(python, ['-B', '-u', 'tests/fixtures/media_sort_plugin_fixture.py', root, temporary, buildDir],
    { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let browser, stderr = '', readyResolve, readyReject, requestId = 0;
  const waiting = new Map(), checks = [], pageErrors = [], calls = [];
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const timer = setTimeout(() => readyReject(new Error('影视插件 fixture 启动超时：' + stderr)), 20000);
  child.stderr.on('data', value => { stderr = (stderr + value).slice(-8000); });
  child.on('error', readyReject);
  child.on('exit', code => { if (code) readyReject(new Error('影视插件 fixture 退出：' + code + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try {
      const value = JSON.parse(line);
      if (value.ready) { clearTimeout(timer); readyResolve(value); }
      else if (waiting.has(value.request_id)) { waiting.get(value.request_id)(value); waiting.delete(value.request_id); }
    } catch (_) { /* fixture output */ }
  });
  const command = (operation, fields = {}) => new Promise((resolve, reject) => {
    const id = ++requestId, deadline = setTimeout(() => { waiting.delete(id); reject(new Error('fixture command timeout')); }, 5000);
    waiting.set(id, value => { clearTimeout(deadline); resolve(value); });
    child.stdin.write(JSON.stringify({ operation, request_id: id, ...fields }) + '\n');
  });
  const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
  const item = (id, state = 'disabled') => ({ id, version: 'fixture_version_' + id,
    database_path: '/fixture/media/' + id + '/scraper.db', state, reason: null,
    can_apply: state === 'disabled', can_revert: state === 'enabled',
    last_backup_path: null, metadata_count: 384, rule_version: state === 'enabled' ? 1 : null });
  let failRead = false, failWrite = false, timeoutWrite = false, loseReply = false, unconfirmed = false, holdWrite = null, releaseWrite;
  try {
    const fixture = await ready, origin = fixture.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin && url.protocol !== 'data:') return route.abort('blockedbyclient');
      if (!url.pathname.startsWith('/api/plugins/media-sort')) return route.continue();
      calls.push({ path: url.pathname, method: request.method(), body: request.method() === 'POST' ? request.postDataJSON() : null, headers: request.headers() });
      const reply = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (request.method() === 'GET' && failRead) return reply(503, { ok: false, error: '缓存读取失败 <img src=x onerror="window.mediaInjected=1">', code: 'unavailable' });
      if (url.pathname.endsWith('/apply')) {
        if (holdWrite) await holdWrite;
        if (failWrite) return reply(503, { ok: false, error: '备份失败 <svg onload="window.mediaInjected=2">', code: 'backup_failed' });
        if (timeoutWrite) return reply(408, { ok: false, error: '操作超时，结果未确认。', code: 'timeout' });
        if (unconfirmed) return reply(200, { ok: true, plugin: (await command('stats')).plugin });
        if (loseReply) { loseReply = false; await route.fetch(); return route.abort('failed'); }
      }
      return route.continue();
    });
    const page = await context.newPage(); page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin + '/#/settings');
    const card = page.locator('#set-r1-media-sort'), select = card.locator('[data-ref="choice"]');
    const apply = card.locator('[data-ref="apply"]'), revert = card.locator('[data-ref="revert"]');
    const probe = card.locator('[data-ref="probe"]'), refresh = card.locator('[data-ref="refresh"]');
    const error = card.locator('[data-ref="error"]');
    const writes = action => calls.filter(call => call.path === '/api/plugins/media-sort/' + action);
    await card.locator('[data-ref="activity"]').filter({ hasText: '当前无进行' }).waitFor();
    check('具有独立可选插件设置入口', await page.locator('#setNav [data-g="r1-media-sort"]').count() === 1);
    check('初始未知明确未检测且不显示已关闭', (await card.innerText()).includes('未检测') && !(await card.locator('[data-ref="state"]').innerText()).includes('未启用'));
    check('打开页面只GET缓存且不自动检测', calls.length === 1 && calls[0].method === 'GET' && (await command('stats')).calls.length === 0);
    check('没有手工路径SQL输入或默认可写操作', await card.locator('input,textarea').count() === 0 && await apply.isDisabled() && await revert.isDisabled());

    const unsafe = item('db_b', 'disabled');
    unsafe.database_path = '/fixture/' + 'long-folder-'.repeat(36) + '<img src=x onerror="window.mediaInjected=3">/scraper.db';
    unsafe.reason = '未启用说明 <svg onload="window.mediaInjected=4">';
    await command('configure', { candidates: [item('db_a'), unsafe] });
    await probe.click(); await select.locator('option[value="db_b"]').waitFor({ state: 'attached' });
    check('检测提交严格空对象且真实fixture仅显式probe', JSON.stringify(writes('probe')[0].body) === '{}' && (await command('stats')).calls[0].action === 'probe');
    check('多库不默认选择第一项', await select.inputValue() === '' && await apply.isDisabled() && (await card.innerText()).includes('多个数据库'));
    check('检测显示时间与当前操作分离', !(await card.locator('[data-ref="checked"]').innerText()).includes('未检测') && (await card.locator('[data-ref="activity"]').innerText()).includes('当前无进行'));
    await select.selectOption('db_b');
    check('路径和原因按文本转义', (await card.locator('[data-ref="path"]').innerText()).includes('<img') && (await card.locator('[data-ref="reason"]').innerText()).includes('<svg') && await card.locator('img,svg[onload]').count() === 0 && !await page.evaluate(() => window.mediaInjected));
    check('未启用显示应用而非升级', (await apply.innerText()).includes('应用') && !(await apply.innerText()).includes('升级') && (await card.locator('[data-ref="state"]').innerText()).includes('未启用'));
    await apply.click();
    let dialog = page.getByRole('dialog').filter({ has: page.locator('#r1AskT') });
    check('应用确认说明先备份和原始规则并转义路径', (await dialog.innerText()).includes('先创建 Toolbox 备份') && (await dialog.innerText()).includes('沿用原始 INSERT 触发器') && await dialog.locator('img').count() === 0);
    await dialog.locator('[data-no]').click();
    check('取消应用没有apply请求', writes('apply').length === 0 && !(await apply.isDisabled()));

    holdWrite = new Promise(resolve => { releaseWrite = resolve; });
    await apply.click(); await page.locator('[data-yes]').click();
    await apply.evaluate(button => { button.click(); button.click(); });
    check('在途操作禁用双击和改库', await apply.isDisabled() && await probe.isDisabled() && await select.isDisabled());
    releaseWrite(); holdWrite = null;
    await card.locator('[data-ref="state"]').filter({ hasText: '已启用' }).waitFor();
    check('应用仅一次并精确绑定选中ID版本确认', writes('apply').length === 1 && JSON.stringify(writes('apply')[0].body) === JSON.stringify({ candidate_id: 'db_b', version: unsafe.version, confirmed: true }));
    check('实际enabled回执才显示启用且展示备份规则', (await card.locator('[data-ref="backup"]').innerText()).includes('scraper.sqlite.backup') && await card.locator('[data-ref="rule"]').innerText() === '1' && !(await revert.isDisabled()));
    const savedBackup = await card.locator('[data-ref="backup"]').innerText();
    check('写入使用JSON同源头而无Bearer', writes('apply')[0].headers['x-fnm-request'] === '1' && !writes('apply')[0].headers.authorization);
    await revert.click();
    check('撤销确认保留时间和新增媒体', (await page.locator('.fullmask').filter({ has: page.locator('#r1AskT') }).innerText()).includes('已调整的时间保留') && (await page.locator('.fullmask').filter({ has: page.locator('#r1AskT') }).innerText()).includes('不会使用旧备份覆盖新增媒体'));
    await page.locator('[data-no]').click();
    check('取消撤销不请求', writes('revert').length === 0);
    await revert.click(); await page.locator('[data-yes]').click();
    await card.locator('[data-ref="state"]').filter({ hasText: '未启用' }).waitFor();
    check('撤销精确当前版本且保留备份位置', writes('revert').length === 1 && JSON.stringify(writes('revert')[0].body) === JSON.stringify({ candidate_id: 'db_b', version: 'revision_1', confirmed: true }) && await card.locator('[data-ref="backup"]').innerText() === savedBackup);

    failWrite = true;
    await apply.click(); await page.locator('[data-yes]').click(); await error.filter({ hasText: '备份失败' }).waitFor();
    check('错误中文按文本展示且保留备份与上次状态', (await error.innerText()).includes('<svg') && await error.locator('svg').count() === 0 && await card.locator('[data-ref="backup"]').innerText() === savedBackup && (await card.locator('[data-ref="state"]').innerText()).includes('未启用'));
    check('失败后不直接重放且保留手工检测', await apply.isDisabled() && !(await probe.isDisabled()));
    await refresh.click(); await card.locator('[data-ref="activity"]').filter({ hasText: '当前无进行' }).waitFor();
    check('读取缓存不会清除未知写入结果保护', await apply.isDisabled() && (await error.innerText()).includes('未自动重试'));
    failWrite = false;
    await probe.click(); await error.waitFor({ state: 'hidden' }); await select.selectOption('db_b');
    check('显式重新检测后才恢复操作', !(await apply.isDisabled()));

    unconfirmed = true;
    await apply.click(); await page.locator('[data-yes]').click(); await error.filter({ hasText: '返回状态未确认' }).waitFor();
    check('HTTP200但未enabled不宣称成功', (await card.locator('[data-ref="state"]').innerText()).includes('未启用') && await apply.isDisabled());
    unconfirmed = false; await probe.click(); await error.waitFor({ state: 'hidden' });
    timeoutWrite = true;
    const beforeTimeout = writes('apply').length;
    await apply.click(); await page.locator('[data-yes]').click(); await error.filter({ hasText: '操作超时' }).waitFor();
    await page.waitForTimeout(150);
    check('超时不重发且要求显式重新检测', writes('apply').length === beforeTimeout + 1 && await apply.isDisabled() && !(await probe.isDisabled()));
    timeoutWrite = false; await probe.click(); await error.waitFor({ state: 'hidden' });
    loseReply = true;
    const beforeUnknown = writes('apply').length;
    await apply.click(); await page.locator('[data-yes]').click(); await error.filter({ hasText: '未自动重试' }).waitFor();
    await page.waitForTimeout(250);
    check('已执行但丢失响应不重试并保留旧状态', writes('apply').length === beforeUnknown + 1 && await apply.isDisabled() && (await card.locator('[data-ref="state"]').innerText()).includes('未启用'));
    await probe.click(); await card.locator('[data-ref="state"]').filter({ hasText: '已启用' }).waitFor();
    check('手工检测可确认未知结果实际已启用', !(await revert.isDisabled()));

    failRead = true; await refresh.click(); await error.filter({ hasText: '缓存读取失败' }).waitFor();
    check('缓存读取失败保留上次已启用和备份', (await card.locator('[data-ref="state"]').innerText()).includes('已启用') && !(await card.locator('[data-ref="backup"]').innerText()).includes('尚无') && await error.locator('img').count() === 0);
    failRead = false;
    await command('configure', { state: { busy: true } }); await refresh.click();
    await card.locator('[data-ref="activity"]').filter({ hasText: '服务端正在操作' }).waitFor();
    check('后端busy独立于上次启用状态且禁止写', (await card.locator('[data-ref="state"]').innerText()).includes('已启用') && await revert.isDisabled());
    await command('configure', { state: { busy: false }, candidates: [item('db_conflict', 'conflict'), item('db_unavailable', 'unavailable')] });
    await probe.click(); await select.selectOption('db_conflict');
    check('冲突候选不可应用撤销', await apply.isDisabled() && await revert.isDisabled());
    await select.selectOption('db_unavailable');
    check('不可用候选不可写', await apply.isDisabled() && await revert.isDisabled());

    const enabled = { ...item('db_enabled', 'enabled'), last_backup_path: '/fixture/enabled-backup/scraper.sqlite3' };
    await command('configure', { candidates: [enabled] }); await probe.click(); await select.selectOption('db_enabled');
    check('已有原规则直接已启用且不能重复安装', !(await revert.isDisabled()) && await apply.isDisabled() && (await card.locator('[data-ref="state"]').innerText()).includes('已启用') && await card.locator('[data-ref="rule"]').innerText() === '1');
    const beforeEnabledRevert = writes('revert').length, beforeEnabledApply = writes('apply').length;
    await revert.click();
    check('撤销确认说明只移除规则并保留时间', (await page.locator('.fullmask').filter({ has: page.locator('#r1AskT') }).innerText()).includes('只移除已识别的追更排序规则') && (await page.locator('.fullmask').filter({ has: page.locator('#r1AskT') }).innerText()).includes('已调整的时间保留'));
    await page.locator('[data-no]').click();
    check('取消撤销不发送请求', writes('revert').length === beforeEnabledRevert);
    await revert.click(); await page.locator('[data-yes]').click();
    await card.locator('[data-ref="state"]').filter({ hasText: '未启用' }).waitFor();
    check('确认撤销精确绑定并保留备份', writes('revert').length === beforeEnabledRevert + 1
      && JSON.stringify(writes('revert').at(-1).body) === JSON.stringify({ candidate_id: enabled.id, version: enabled.version, confirmed: true })
      && writes('apply').length === beforeEnabledApply && await card.locator('[data-ref="backup"]').innerText() === enabled.last_backup_path);

    await command('configure', { candidates: [unsafe, item('db_a')] }); await probe.click(); await select.selectOption('db_b');
    await page.setViewportSize({ width: 320, height: 900 });
    await card.scrollIntoViewIfNeeded();
    check('320px长路径原因不横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
      && await card.evaluate(element => element.scrollWidth <= element.clientWidth + 1));
    check('窄屏操作可用且无脚本注入', !(await apply.isDisabled()) && !await page.evaluate(() => window.mediaInjected));
    await apply.click();
    check('320px确认窗长路径换行且无横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
      && await page.locator('.fullmask').filter({ has: page.locator('#r1AskT') }).evaluate(element => element.scrollWidth <= element.clientWidth + 1));
    await page.locator('[data-no]').click();
    const stable = calls.length; await page.waitForTimeout(1100);
    check('稳定页面不自动检测或轮询插件', calls.length === stable);
    check('没有页面脚本错误', pageErrors.length === 0);
    console.log(JSON.stringify({ ok: true, checks, fixture: temporary, requests: calls.length, backendCalls: (await command('stats')).calls.length }, null, 2));
  } finally {
    clearTimeout(timer); if (releaseWrite) releaseWrite();
    if (browser) await browser.close().catch(() => {});
    child.stdin.end(); child.kill('SIGTERM');
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
