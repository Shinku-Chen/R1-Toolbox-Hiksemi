// Business panels use the real panel with isolated HTTP responses, never a NAS.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-business-browser-'));
const fixtureDir = path.join(temporary, 'r1-web-browser-business'), buildDir = path.join(temporary, 'panel');
fs.mkdirSync(fixtureDir);
const python = process.env.PYTHON_BIN || 'python3';
const chromeBin = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'].find(x => x && fs.existsSync(x));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请设置 CHROME_BIN');
const built = spawnSync(python, ['scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(built.status, 0, built.stderr || built.stdout);
const fixtureSource = fs.readFileSync(path.join(__dirname, 'check_web_config.cjs'), 'utf8');
const match = fixtureSource.match(/const pythonFixture = String.raw`([\s\S]*?)`;/);
assert.ok(match, '无法找到网页 fixture');
const fixtureCode = match[1].replaceAll("asset_dir=repo / 'ui/web'", 'asset_dir=Path(sys.argv[3])');

(async () => {
  const child = spawn(python, ['-u', '-c', fixtureCode, root, fixtureDir, buildDir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, stderr = '', resolveReady, rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const timer = setTimeout(() => rejectReady(new Error('业务 fixture 启动超时')), 20000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-6000); });
  child.on('error', rejectReady);
  child.on('exit', code => { if (code) rejectReady(new Error('fixture 退出：' + code + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try { const value = JSON.parse(line); if (value.ready) { clearTimeout(timer); resolveReady(value); } } catch (_) { /* fixture log */ }
  });
  const checks = [], errors = [], writes = [];
  const check = (name, value) => { assert.ok(value, name); checks.push(name); };
  const stamp = '2026-10-29T01:02:03.000+00:00';
  const state = { ok: true, snapshot: { sampled_at: stamp, categories: {
    applications: { available: true, status: 'cached_partial', reason: '历史快照，不代表运行状态', data: { cache_modified_at: stamp,
      partial: true, rejected_entries: 2, rejections: [{ code: 'unsupported_application_label', count: 2 }],
      apps: [{ name: '<img src=x onerror=alert(1)>', version: '1.0', installation_status_code: null }] } },
    downloads: { available: true, data: { engines: [
      { engine: 'aria2', available: false, reason: '固定监听未就绪' },
      { engine: 'qbittorrent', available: true, data: { version: 'v5.1.4', counts: { tasks: 3 },
        download_bytes_per_second: 0, upload_bytes_per_second: null, complete: false } },
    ] } },
    media: { available: false, reason: '尚未主动采集媒体状态。', data: null },
  } } };
  let reads = 0, failRefresh = false, holdRefresh;
  try {
    const fixture = await ready, origin = fixture.primary.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin && url.protocol !== 'data:') return route.abort('blockedbyclient');
      const reply = (status, value) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (url.pathname === '/api/business') { reads += 1; return reply(200, state); }
      if (url.pathname !== '/api/r1/business/refresh') return route.continue();
      writes.push({ body: request.postDataJSON(), headers: request.headers() });
      if (failRefresh) return reply(503, { ok: false, error: 'fixture：原厂来源暂不可用' });
      if (holdRefresh) await holdRefresh;
      if (request.postDataJSON().source === 'media') state.snapshot.categories.media = {
        available: true, data: { captured_at: stamp, results: [
          { id: 'face', available: true, captured_at: stamp, data: { processedpic: 7, totalpic: 10, processedvid: 2, totalvid: 3 } },
          { id: 'similar', available: true, captured_at: stamp, data: { pending_hashes: 8, completion_ratio: null } },
          { id: 'shared_album', status: 'not_applicable', available: false, captured_at: stamp, data: null },
        ] },
      };
      return reply(200, { ok: true, result: { available: true }, cache: { stored: true } });
    });
    const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/#/apps');
    const panel = page.locator('section[data-mod="r1business"]');
    await panel.scrollIntoViewIfNeeded();
    await panel.locator('[data-ref="downloads"]').filter({ hasText: 'v5.1.4' }).waitFor();
    check('应用页自动显示原厂业务模块', await panel.count() === 1 && reads >= 1);
    check('没有原厂来源的上游统计模块隐藏', await page.locator('[data-mod="apps"]').isHidden());
    check('打开页面不执行签名查询', writes.length === 0);
    check('应用名称作为文字安全显示', (await panel.locator('[data-ref="applications"]').innerText()).includes('<img src=x')
      && await panel.locator('[data-ref="applications"] img').count() === 0);
    check('未知安装状态显示缺测', (await panel.locator('[data-ref="applications"]').innerText()).includes('状态码 --'));
    check('应用部分结果明确隔离数量且不冒充总安装量', (await panel.locator('[data-ref="applications"] .st-warn').innerText()).includes('已隔离 2 条')
      && (await panel.locator('[data-ref="applications"] .st-warn').innerText()).includes('1 条有效缓存')
      && (await panel.locator('[data-ref="applications"] .st-warn').innerText()).includes('不代表全部已安装应用'));
    const downloadText = await panel.locator('[data-ref="downloads"]').innerText();
    check('两个下载来源独立报告成功与失败', downloadText.includes('固定监听未就绪') && downloadText.includes('3 个任务'));
    check('真零速率与未知速率分开', downloadText.includes('下载 0') && downloadText.includes('上传 --'));
    check('媒体无缓存不会伪造计数', (await panel.locator('[data-ref="media"]').innerText()).includes('尚未主动采集'));
    let releaseRefresh;
    holdRefresh = new Promise(resolve => { releaseRefresh = resolve; });
    const mediaButton = panel.locator('[data-ref="refreshMedia"]');
    await mediaButton.click();
    await mediaButton.filter({ hasText: '正在采集' }).waitFor();
    check('采集中禁用重复点击', await mediaButton.isDisabled());
    releaseRefresh(); holdRefresh = null;
    await panel.locator('[data-ref="media"]').filter({ hasText: '图片 7 / 10' }).waitFor();
    check('显式采集媒体后显示计数和时间', (await panel.locator('[data-ref="media"]').innerText()).includes('采集时间'));
    check('相似图片不从原厂错误计数推断完成率', (await panel.locator('[data-ref="media"]').innerText()).includes('不用于推断完成率'));
    check('无共享相册标注不适用', (await panel.locator('[data-ref="media"]').innerText()).includes('没有可查询的已有共享相册'));
    check('刷新请求只有固定来源及CSRF头', JSON.stringify(writes[0].body) === '{"source":"media"}' && writes[0].headers['x-fnm-request'] === '1');
    failRefresh = true;
    const appButton = panel.locator('[data-ref="refreshApplications"]');
    await appButton.click();
    await page.waitForFunction(() => !document.querySelector('[data-mod="r1business"] [data-ref="refreshApplications"]').disabled);
    check('刷新失败保留原结果并恢复按钮', !(await appButton.isDisabled()) && (await panel.innerText()).includes('v5.1.4'));
    await page.setViewportSize({ width: 320, height: 900 });
    await panel.scrollIntoViewIfNeeded();
    check('手机宽度业务卡片单列且不横向溢出', await panel.evaluate(element =>
      element.scrollWidth <= element.clientWidth + 1 && getComputedStyle(element.querySelector('.r1-business-grid')).gridTemplateColumns.split(' ').length === 1));
    check('页面无脚本错误', errors.length === 0);
    console.log(JSON.stringify({ ok: true, checks, reads, writes: writes.length }, null, 2));
  } catch (error) {
    console.error(JSON.stringify({ ok: false, checks, errors, error: String(error.stack || error), stderr }, null, 2));
    process.exitCode = 1;
  } finally { clearTimeout(timer); if (browser) await browser.close().catch(() => {}); child.kill('SIGTERM'); }
})();
