// 小屏 local 按需补齐：静态真实 ui/ + 本机 API fixture。未连接 NAS。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const ui = path.join(root, 'ui');
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium');

const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = http.createServer((request, response) => {
  const target = request.url === '/' ? '/index.html' : request.url.split('?')[0];
  const file = path.join(ui, target);
  if (!file.startsWith(ui) || !fs.existsSync(file)) { response.writeHead(404); return response.end(); }
  response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(response);
});
const checks = [];
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const NOW = 1_760_000_000;

/** Run the real enclosed model without its timers or initial HTTP/render calls. */
function screenModel() {
  const filename = path.join(ui, 'assets', 'panel.js');
  const source = fs.readFileSync(filename, 'utf8').replace(/\r\n/g, '\n');
  const startup = '  window.setInterval(tickClock, 1000);\n  render();\n  poll();';
  assert.equal(source.split(startup).length, 2, '真实小屏启动入口必须唯一，不能静默跳过模型检查');
  const context = {
    location: { protocol: 'http:', origin: 'http://127.0.0.1' },
    window: { addEventListener() {} },
    document: { addEventListener() {} },
  };
  vm.runInNewContext(source.replace(startup,
    '  globalThis.screenModel = { state, pool, feed };'), context, { filename, timeout: 1000 });
  return context.screenModel;
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ executablePath: chromeBin, headless: true });
  const page = await browser.newPage({ viewport: { width: 376, height: 960 } });
  const fills = [];
  let mode = 'normal';
  let active = 0, maxActive = 0, releaseGate = null;
  const gate = () => new Promise(resolve => {
    releaseGate = () => { mode = 'normal'; releaseGate = null; resolve(); };
  });
  let disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
                 capacity_sampled_at: null, capacity_status: 'unavailable' }];
  let insightDisks = [];
  let statusSequence = 0;
  const status = () => ({ schema_version: 1, available: true,
    timestamp: new Date((NOW + statusSequence++) * 1000).toISOString(),
    hostname: 'fixture', uptime_seconds: 100, cpu: { usage_percent: 5, temp_c: 40 },
    memory: { total_bytes: 8e9, used_bytes: 2e9, usage_percent: 25 },
    network: { interface: 'eth0', ipv4: '192.168.1.2', rx_bytes_per_sec: 0, tx_bytes_per_sec: 0 },
    disks });
  await page.addInitScript(() => {
    window.require = () => ({ env: { R1_TOOLBOX_CONTROL_TOKEN: 'fixture-local-token' } });
  });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const reply = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname === '/api/status') return reply(status());
    if (url.pathname === '/api/storage/fill') {
      active += 1;
      maxActive = Math.max(maxActive, active);
      try {
        fills.push({ method: route.request().method(), headers: route.request().headers(),
                     body: route.request().postData() || '' });
        if (mode === 'busy') return await route.fulfill({ status: 409, contentType: 'application/json',
          body: JSON.stringify({ ok: false, reason: 'busy' }) });
        if (mode === 'forbidden') return await route.fulfill({ status: 403, contentType: 'application/json',
          body: JSON.stringify({ ok: false, reason: 'forbidden' }) });
        if (mode === 'standby') return await route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
            smart: { ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
                     results: [{ device: 'sda', status: 'skipped_standby', reason: 'standby', sampled_at: null }] },
            capacity: { ok: true, status: 'complete', started_at: NOW - 2, finished_at: NOW,
                        results: [{ mount_id: 1, status: 'skipped_standby', reason: 'standby', sampled_at: null }] } }) });
        if (mode === 'invalid') return await route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ ok: true, force: true, status: 'complete' }) });
        if (mode === 'gated') {
          mode = 'normal'; // One-shot gate: any unexpected concurrent request cannot strand another waiter.
          await gate();
        }
        return await reply({ ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
          smart: { ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW, results: [] },
          capacity: { ok: true, status: 'complete', started_at: NOW - 2, finished_at: NOW, results: [] } });
      } finally { active -= 1; }
    }
    if (url.pathname === '/api/insights' || url.pathname === '/api/overview') {
      return reply({ ok: true, system: {}, device: {}, history: [], disks: { items: insightDisks } });
    }
    return reply({ ok: true });
  });
  try {
    await page.goto(origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(2500);
    check('小屏可见缺测自动补齐一次', fills.length === 1 && fills[0].body === '{}'
      && fills[0].headers['x-r1-token'] === 'fixture-local-token');
    await page.waitForTimeout(2200);
    check('补齐后不重复', fills.length === 1);

    disks = [{ name: 'sda', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25,
               capacity_sampled_at: NOW, capacity_status: 'ok' }];
    const before = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2500);
    check('有效缓存 0 次', fills.length === before);

    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    mode = 'busy';
    const busyBase = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2500);
    check('busy 只一次', fills.length === busyBase + 1);
    await page.waitForTimeout(2000);
    check('busy 后不循环', fills.length === busyBase + 1);
    mode = 'normal';

    // 采样成功：POST 后被动 GET 展示新值/时间，不追加 POST
    const fillBase = fills.length;
    disks = [{ name: 'sda', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25,
               capacity_sampled_at: NOW, capacity_status: 'ok' }];
    let served = 0;
    let sampling = true;
    await page.route('**/api/status', async route => {
      if (!sampling) return route.fallback();
      served += 1;
      const row = served <= 2
        ? { name: 'sda', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25,
            capacity_sampled_at: NOW - 3600, capacity_status: 'ok' }
        : { name: 'sda', total_bytes: 4.2e12, used_bytes: 1.2e12, usage_percent: 28,
            capacity_sampled_at: NOW, capacity_status: 'ok' };
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        ...status(), disks: [row] }) });
    });
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(5200);
    sampling = false;
    const caption = await page.textContent('#device');
    console.log('CAPTION', JSON.stringify(caption.slice(0, 240)));
    check('被动刷新后展示新值与快照说明', caption.includes('3.8 TB') && caption.includes('容量快照'));
    check('被动刷新不追加补齐', fills.length === fillBase);

    // hidden 与无 token
    insightDisks = [];
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const hiddenBase = fills.length;
    await page.waitForTimeout(2600);
    check('hidden 不补齐', fills.length === hiddenBase);

    const clean = await browser.newPage({ viewport: { width: 376, height: 960 } });
    const cleanFills = [];
    await clean.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/storage/fill') cleanFills.push(route.request().headers());
      if (url.pathname === '/api/status') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
          ...status(), disks: [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
                                 capacity_sampled_at: null, capacity_status: 'unavailable' }] }) });
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });
    await clean.goto(origin + '/', { waitUntil: 'networkidle' });
    await clean.waitForTimeout(2600);
    check('无 local token 时不补齐', cleanFills.length === 0);
    await clean.close();

    // 容量有效但 SMART 过期 → 一次；恢复后 0
    disks = [{ name: 'sda', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25,
               capacity_sampled_at: NOW, capacity_status: 'ok' }];
    insightDisks = [{ id: 'sda', smart_fresh: false, smart_sampled_at: NOW - 9999, temp_c: null }];
    let base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2600);
    check('容量有效但 SMART 过期也补齐一次', fills.length === base + 1);
    await page.waitForTimeout(2000);
    check('SMART 过期补齐不重复', fills.length === base + 1);
    insightDisks = [{ id: 'sda', smart_fresh: true, smart_sampled_at: NOW, temp_c: -5 }];
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2600);
    check('SMART 有效（含负温度）0 次', fills.length === base);

    // Production's never-sampled projection uses null freshness, not false.
    disks[0].capacity_status = 'cached';
    insightDisks = [{ id: 'sda', smart_available: false, smart_fresh: null,
      smart_sampled_at: null, temp_c: null, smart_status: 'unavailable' }];
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2600);
    check('容量有缓存但 SMART 从未采集时补齐一次', fills.length === base + 1);
    await page.waitForTimeout(2200);
    check('SMART 初始缺测补齐不循环', fills.length === base + 1);

    // standby / 403 / 畸形：各一次、不循环
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    insightDisks = [];
    for (const variant of ['busy', 'forbidden', 'invalid']) {
      mode = variant;
      base = fills.length;
      await page.reload({ waitUntil: 'networkidle' });
      await page.waitForTimeout(2600);
      check('负结果严格一次：' + variant, fills.length === base + 1);
      await page.waitForTimeout(2000);
      check('负结果不循环：' + variant, fills.length === base + 1);
    }
    mode = 'normal';

    // 在途 hide/show：用 deferred 门确认真实在途后切换，最多 1 个并发
    mode = 'gated';
    maxActive = 0;
    base = fills.length;
    await page.goto(origin + '/', { waitUntil: 'domcontentloaded' });
    const gateDeadline = Date.now() + 6000;
    while (active === 0 && Date.now() < gateDeadline) await new Promise(r => setTimeout(r, 30));
    check('确认补齐请求真实在途', active === 1 && fills.length === base + 1);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(300);
    check('在途 hide/show 不追加并发请求', maxActive === 1 && fills.length === base + 1);
    releaseGate();
    await page.waitForTimeout(2600);
    check('释放后新周期最多再判断一次', fills.length >= base + 1 && fills.length <= base + 2
      && maxActive === 1 && active === 0 && releaseGate === null);
    mode = 'normal';

    // 非磁盘消费页（网络）不补齐
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2600);
    await page.click('[data-view="network"]');
    await page.waitForTimeout(2600);
    check('非磁盘消费页 0 次', fills.length === base + 1); // 主页 overview 已允许一次

    // 请求头只带本地 token，不借 Cookie/Bearer
    const head = fills[fills.length - 1].headers;
    check('补齐仅本地 token 头', head['x-r1-token'] === 'fixture-local-token'
      && !head['cookie'] && !head['authorization'] && fills.every(row => row.body === '{}'));

    // standby 真实回执：一次且不循环
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    mode = 'standby';
    base = fills.length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2600);
    check('standby 回执只一次', fills.length === base + 1);
    await page.waitForTimeout(2000);
    check('standby 不循环', fills.length === base + 1);
    mode = 'normal';

    // sleeping：toolbox.screen.asleep=true → 0
    await page.route('**/api/toolbox', async route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, screen: { asleep: true } }) }));
    base = fills.length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2600);
    check('sleeping 不补齐', fills.length === base);

    await page.unroute('**/api/toolbox');
    await page.route('**/api/toolbox', async route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, screen: { saver: true, asleep: false } }) }));
    base = fills.length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2600);
    check('初始屏保显示时不补齐', await page.isVisible('#saver') && fills.length === base);
    await page.waitForTimeout(2200);
    check('屏保期间重复 GET 仍不补齐', fills.length === base);

    const early = NOW - 7200, late = NOW - 60, older = NOW - 86400;
    disks = [
      { name: 'sda', total_bytes: 1e12, used_bytes: 1e11, usage_percent: 10,
        capacity_sampled_at: early, capacity_status: 'ok' },
      { name: 'sdb', total_bytes: 2e12, used_bytes: 5e11, usage_percent: 25,
        capacity_sampled_at: late, capacity_status: 'ok' },
      { name: 'sdc', total_bytes: null, used_bytes: null, usage_percent: null,
        capacity_sampled_at: older, capacity_status: 'partial' },
      { name: 'sdd', total_bytes: 5e11, used_bytes: 5e10, usage_percent: 10,
        capacity_sampled_at: late, capacity_status: 'failed' },
    ];
    await page.unroute('**/api/toolbox');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2600);
    const poolText = await page.textContent('.inline-summary .caption');
    const [earlyText, lateText, olderText] = await page.evaluate(stamps =>
      stamps.map(stamp => new Date(stamp * 1000).toLocaleString()), [early, late, older]);
    check('混合时刻显示最早→最新范围', poolText.includes(earlyText + ' – ' + lateText)
      && !poolText.includes(olderText));
    check('覆盖 3/4 且失败行计入未完成', poolText.includes('覆盖 3/4 块盘') && poolText.includes('1 块上次刷新未完成'));
    check('容量快照完整说明与具体覆盖准确', poolText.endsWith('容量快照 · ' + earlyText + ' – ' + lateText
      + ' · 覆盖 3/4 块盘 · 1 块上次刷新未完成'));
    const aggregate = screenModel();
    aggregate.state.data.status = { disks };
    const measured = aggregate.pool();
    check('真实 pool 仅汇总贡献行及其时间', measured.total === 3.5e12 && measured.used === 6.5e11
      && measured.measured === 3 && measured.count === 4 && measured.earliest === early
      && measured.latest === late && measured.stale === 1);
    const firstCaption = poolText;
    await page.waitForTimeout(2200);
    check('同一采样时刻重复 GET 不改变文案', (await page.textContent('.inline-summary .caption')) === firstCaption);
    await page.evaluate(() => { document.querySelector('[data-view="metrics"]')?.click(); });
    await page.waitForTimeout(600);
    check('metrics 页也展示容量快照语义', (await page.textContent('#device')).includes('容量快照'));

    // Inspect real closed-over feed state in a VM; each passive poll has a new timestamp.
    const curve = screenModel();
    const curveDisks = [
      { name: 'sda', total_bytes: 1000, used_bytes: 100, capacity_sampled_at: 100, capacity_status: 'cached' },
      { name: 'sdb', total_bytes: 1000, used_bytes: 200, capacity_sampled_at: 200, capacity_status: 'cached' },
      { name: 'sdc', total_bytes: null, used_bytes: null, capacity_sampled_at: 900, capacity_status: 'unavailable' },
    ];
    let curveSequence = 0;
    const feedCurve = () => {
      curve.state.data.status = { timestamp: new Date((NOW + curveSequence++) * 1000).toISOString(), disks: curveDisks };
      curve.feed();
      return Array.from(curve.state.live.disk);
    };
    check('真实容量曲线首次采样为一个 15% 点', JSON.stringify(feedCurve()) === '[15]');
    check('同一容量缓存的新 GET 不追加曲线点', JSON.stringify(feedCurve()) === '[15]');
    curveDisks[0].capacity_sampled_at = 150;
    curveDisks[0].used_bytes = 300;
    check('较旧贡献行更新也追加一个 25% 点', JSON.stringify(feedCurve()) === '[15,25]');
    check('更新后的同一容量缓存不重复追加', JSON.stringify(feedCurve()) === '[15,25]');
    const signature = curve.state.lastCapacitySample;
    curveDisks[2].capacity_sampled_at = 1000;
    check('未贡献行更新更晚时间不改变曲线签名或点数', JSON.stringify(feedCurve()) === '[15,25]'
      && curve.state.lastCapacitySample === signature);
    const curvePool = curve.pool();
    check('未贡献行不影响总量、时间及覆盖', curvePool.total === 2000 && curvePool.used === 500
      && curvePool.percent === 25 && curvePool.earliest === 150 && curvePool.latest === 200
      && curvePool.measured === 2 && curvePool.count === 3 && curvePool.stale === 0);

    // Enter metrics while caches are valid, then make data missing in that view.
    disks = [{ name: 'sda', total_bytes: 1000, used_bytes: 100,
      capacity_sampled_at: NOW, capacity_status: 'cached' }];
    insightDisks = [];
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.click('[data-view="metrics"]');
    check('metrics 有有效缓存时不补齐', fills.length === base);
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null,
      capacity_sampled_at: null, capacity_status: 'unavailable' }];
    await page.waitForTimeout(2600);
    check('metrics 当前可见时缺测补齐一次', fills.length === base + 1);
    await page.waitForTimeout(2200);
    check('metrics 缺测补齐不循环', fills.length === base + 1);

    // ================= 新增：每次真实相关 view entry 独立一次 =================
    // 已确认语义：overview / storage / metrics 的每一次真实进入都获得
    // 独立的一次缺测 fill 机会；同一 view 内 poll/render/重复点击不重试；离开相关
    // 视图（hidden/saver/sleep/非相关页）结束本次机会，回来可再一次。原 45 项全部
    // 保留，本节只追加，不改写任何既有断言。
    insightDisks = [];
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    mode = 'normal';
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });          // overview entry
    await page.waitForTimeout(2600);
    check('每页进入独立：overview 首次一次', fills.length === base + 1);
    await page.waitForTimeout(2000);
    check('overview 同页轮询不重试', fills.length === base + 1);

    await page.click('[data-tab="storage"]');                  // storage entry
    await page.waitForTimeout(2600);
    check('每页进入独立：storage 新一次', fills.length === base + 2);
    await page.waitForTimeout(2000);
    check('storage 同页轮询不重试', fills.length === base + 2);

    await page.click('[data-tab="storage"]');                  // same view: no new
    await page.waitForTimeout(1200);
    check('同 view 重复点击不追加', fills.length === base + 2);

    await page.click('[data-tab="overview"]');                 // overview re-entry
    await page.waitForTimeout(2600);
    check('相关页之间来回：overview 再入一次', fills.length === base + 3);
    await page.click('[data-view="metrics"]');                 // metrics entry
    await page.waitForTimeout(2600);
    check('metrics 进入独立一次', fills.length === base + 4);
    await page.waitForTimeout(1800);
    check('metrics 同页轮询不重试', fills.length === base + 4);

    // 有效 current：任何相关 view 都不需要读
    disks = [{ name: 'sda', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25,
               capacity_sampled_at: NOW, capacity_status: 'ok' }];
    base = fills.length;
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1400);
    await page.click('[data-tab="storage"]');
    await page.waitForTimeout(1400);
    await page.click('[data-tab="overview"]');
    await page.waitForTimeout(1400);
    await page.click('[data-view="metrics"]');
    await page.waitForTimeout(1400);
    check('有效 current 各 view 0 次', fills.length === base);

    // 在途 A→B：单并发、旧回执不能占 B 的进入机会、释放后 B 自己一次
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    mode = 'gated';
    maxActive = 0;
    base = fills.length;
    await page.goto(origin + '/', { waitUntil: 'domcontentloaded' });   // A: overview fill in flight
    const inflightDeadline = Date.now() + 6000;
    while (active === 0 && Date.now() < inflightDeadline) await new Promise(r => setTimeout(r, 30));
    check('在途确认：A 单请求', active === 1 && fills.length === base + 1 && maxActive === 1);
    await page.click('[data-tab="storage"]');                  // B entry while A is inflight
    await page.waitForTimeout(400);
    check('A 在途时 B 不并发', maxActive === 1);
    releaseGate();
    await page.waitForTimeout(2600);
    check('旧回执不占 B：释放后 B 自己一次', fills.length === base + 2 && maxActive === 1);
    await page.waitForTimeout(1800);
    check('B 之后同页不追加', fills.length === base + 2);
    mode = 'normal';

    // hidden 进入不补；恢复可见后该 view 补一次（边界保留）
    disks = [{ name: 'sda', total_bytes: null, used_bytes: null, usage_percent: null,
               capacity_sampled_at: null, capacity_status: 'unavailable' }];
    base = fills.length;
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.click('[data-tab="overview"]');
    await page.waitForTimeout(1600);
    check('hidden 进入不补齐', fills.length === base);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(2600);
    check('恢复可见后该 view 补一次', fills.length === base + 1);

    // 非相关页结束本次机会；下一次相关 view entry 重新允许一次
    base = fills.length;
    await page.click('[data-tab="network"]');
    await page.waitForTimeout(1600);
    check('非相关页 0 次', fills.length === base);
    await page.click('[data-tab="storage"]');
    await page.waitForTimeout(2600);
    check('非相关页之后 storage 新一次', fills.length === base + 1);

    console.log('PASS: ' + checks.length + ' checks; fill POST=' + fills.length);
  } finally {
    if (releaseGate) releaseGate();
    await browser.close();
    server.close();
  }
})().catch(error => { console.error('FAIL: ' + (error && error.stack || error)); process.exit(1); });
