// 磁盘页手动刷新 SMART：真实本地面板 + 固定回执 fixture（Playwright 路由桩）。
// 未连接 NAS，也没有真实后端；路由桩只验证前端合同与交互。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-smart-refresh-'));
const buildDir = path.join(temporary, 'panel');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-web-browser-smart-'));
const python = process.env.PYTHON_BIN || 'python3';
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请设置 CHROME_BIN');

const build = spawnSync(python, ['scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(build.status, 0, '临时面板构建失败：' + (build.stderr || build.stdout));

const source = fs.readFileSync(path.join(__dirname, 'check_web_config.cjs'), 'utf8');
const fixtureMatch = source.match(/const pythonFixture = String\.raw`([\s\S]*?)`;/);
assert.ok(fixtureMatch, '无法找到真实网页 fixture');
const pythonFixture = fixtureMatch[1].replaceAll("asset_dir=repo / 'ui/web'", 'asset_dir=Path(sys.argv[3])');

const checks = [];
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const NOW = Date.now() / 1000;
const OLD = 8, NEW = 12;

function receipt({ force = false, status = 'complete', started = Date.now() / 1000 - 2, finished = Date.now() / 1000, results }) {
  return { ok: true, force, status, started_at: started, finished_at: finished,
           results: results || [
             { device: 'sda', status: 'refreshed', reason: null, sampled_at: finished },
             { device: 'sdb', status: 'refreshed', reason: null, sampled_at: finished }] };
}
function smartDetail(raw, { sampledAt = NOW - 5, fresh = true } = {}) {
  return { ok: true, dev: 'sda', model: 'Fixture NAS Drive', size: 4e12, temp: 39,
           sampled_at: sampledAt, fresh,
           health: { passed: true }, standby: null, reason: null, source: 'fixture',
           attributes: [{ id: 5, name: 'Reallocated_Sector_Ct', value: 100, worst: 100,
                          threshold: 10, raw_value: raw, raw_text: String(raw), when_failed: '' }],
           nvme_rows: [], power_on_hours: 100, power_cycle_count: 3, rotation_rate: 7200,
           grown_defects: null, scsi_pending_defects: null, scsi_uncorrected: null,
           scsi_ecc: null, scsi_rereads: null };
}

(async () => {
  const child = spawn(python, ['-u', '-c', pythonFixture, root, fixtureDir, buildDir],
    { stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, stderr = '';
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('网页 fixture 启动超时')), 20000);
    child.stderr.on('data', data => { stderr = (stderr + data).slice(-6000); });
    child.on('error', reject);
    child.on('exit', code => { if (code) reject(new Error('网页 fixture 退出：' + code + '\n' + stderr)); });
    readline.createInterface({ input: child.stdout }).on('line', line => {
      try { const value = JSON.parse(line); if (value.ready) { clearTimeout(timer); resolve(value); } } catch (_) { /* log */ }
    });
  });
  let mode = 'normal';
  const posts = [];
  const detailReads = [];
  const smartValue = { sda: OLD, sdb: OLD };
  const smartMeta = { sda: { sampledAt: NOW - 5, fresh: true },
                      sdb: { sampledAt: NOW - 5, fresh: true } };
  let cacheOverride = null;
  let releaseFillGate = null;
  let releaseAuthGate = null;
  try {
    const info = await ready;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(String(error)));
    const waitCard = () => page.waitForSelector('#sheetBody [data-ref="r1SmartForce"]', { state: 'visible', timeout: 10000 });
    const waitIdle = () => page.waitForFunction(() => {
      const button = document.querySelector('#sheetBody [data-ref="r1SmartGo"]');
      return button && !button.disabled;
    }, null, { timeout: 8000 });
    const openDisk = async device => {
      await page.locator(`#layout-storage .bay[data-sheet="disk/${device}"]`).click();
      await waitCard();
    };
    const closeSheet = async () => {
      await page.locator('#sheetClose').click();
      await page.waitForFunction(() => !document.querySelector('#sheet').classList.contains('on'));
    };
    const tableText = () => page.textContent('#sheetBody [data-ref="smartAll"]');
    const waitTable = value => page.waitForFunction(expected => {
      const table = document.querySelector('#sheetBody [data-ref="smartAll"]');
      return table && table.textContent.includes(expected);
    }, String(value), { timeout: 8000 });

    await page.route('**/api/smart?**', async route => {
      const url = new URL(route.request().url());
      const device = (url.searchParams.get('dev') || 'sda').replace(/[^A-Za-z0-9_.:-]/g, '');
      detailReads.push(device);
      const meta = cacheOverride || smartMeta[device] || {};
      const age = Date.now() / 1000 - meta.sampledAt;
      return route.fulfill({ status: 200, contentType: 'application/json',
                             body: JSON.stringify({ ...smartDetail(smartValue[device] || OLD,
                               cacheOverride || { ...meta, fresh: meta.fresh && age >= 0 && age < 30 }), dev: device }) });
    });
    const capPosts = [];
    let capMode = 'normal';
    const capState = { sda: { stamp: NOW - 3600, used: 1000, total: 4000, status: 'ok' },
                       sdb: { stamp: null, used: null, total: null, status: 'unavailable' } };
    let capUpdated = false;
    let overviewRows = [];
    await page.route('**/api/overview', async route => {
      const response = await route.fetch();
      const body = await response.json();
      for (const row of (body.disks_detail || [])) {
        const meta = capState[row.name];
        if (!meta) continue;
        row.total = meta.total == null ? meta.total : meta.total * 1e9;
        row.used = meta.used == null ? meta.used : meta.used * 1e9;
        row.capacity_sampled_at = meta.stamp; row.capacity_status = meta.status;
        // Each declared fixture phase starts one dated cache sample. GETs reuse
        // that same timestamp, rather than declaring an hour-old sample fresh.
        if (meta.smartStamp === undefined) meta.smartStamp = Date.now() / 1000;
        row.smart_sampled_at = meta.smartStamp;
        const age = Date.now() / 1000 - meta.smartStamp;
        row.smart_fresh = (meta.smartFresh === undefined ? true : meta.smartFresh) && age >= 0 && age < 30;
        row.smart_available = meta.smartAvailable === undefined ? true : meta.smartAvailable;
        row.temp = meta.temp === undefined ? 39 : meta.temp;
        row.temp_c = row.temp;
        row.capacity_source = 'manual_statvfs_cache';
      }
      overviewRows = body.disks_detail || [];
      return route.fulfill({ status: response.status(), contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.route('**/api/r1/storage/capacity/refresh', async route => {
      const request = route.request();
      capPosts.push({ method: request.method(), headers: request.headers(), body: request.postData() || '' });
      const reply = payload => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
      const base = { ok: true, status: 'complete', started_at: NOW - 2, finished_at: NOW,
                     results: [{ mount_id: 1, status: 'refreshed', reason: null, sampled_at: NOW }] };
      if (capMode === 'busy') return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ ok: false, reason: 'busy' }) });
      if (capMode === 'partial') return reply({ ...base, status: 'partial', results: [
        { mount_id: 1, status: 'refreshed', reason: null, sampled_at: NOW },
        { mount_id: 2, status: 'failed', reason: 'statvfs_failed', sampled_at: null }] });
      if (capMode === 'bad_stamp') return reply({ ...base, finished_at: '1760000000' });
      if (capMode === 'bad_mount') return reply({ ...base, results: [{ mount_id: 0, status: 'refreshed', reason: null, sampled_at: NOW }] });
      if (capMode === 'missing_key') return reply({ ...base, results: [{ mount_id: 1, status: 'refreshed', reason: null }] });
      if (capMode === 'slow') await new Promise(resolve => setTimeout(resolve, 700));
      return reply(base);
    });
    await page.route('**/api/r1/storage/smart/refresh', async route => {
      const request = route.request();
      const body = request.postData() || '';
      posts.push({ method: request.method(), headers: request.headers(), body });
      let force = false;
      try { force = JSON.parse(body).force === true; } catch (_) { /* ignore */ }
      const reply = payload => route.fulfill({ status: 200, contentType: 'application/json',
                                               body: JSON.stringify(payload) });
      if (mode === 'busy') {
        return route.fulfill({ status: 409, contentType: 'application/json',
                               body: JSON.stringify({ ok: false, error: 'busy', reason: 'busy' }) });
      }
      if (mode === 'force_string') return reply({ ...receipt({ force }), force: 'yes' });
      if (mode === 'results_object') return reply({ ok: true, force, status: 'complete',
                                                    started_at: NOW - 1, finished_at: NOW, results: {} });
      if (mode === 'time_string') return reply({ ...receipt({ force }), finished_at: '1760000000' });
      if (mode === 'time_order') return reply(receipt({ force, started: NOW, finished: NOW - 5 }));
      if (mode === 'force_mismatch') return reply(receipt({ force: !force }));
      if (mode === 'duplicate_device') {
        return reply(receipt({ force, results: [
          { device: 'sda', status: 'refreshed', reason: null, sampled_at: NOW },
          { device: 'sda', status: 'refreshed', reason: null, sampled_at: NOW }] }));
      }
      if (mode === 'missing_keys') {
        return reply(receipt({ force, results: [{ device: 'sda', status: 'refreshed' }] }));
      }
      if (mode === 'slow') await new Promise(resolve => setTimeout(resolve, 700));
      smartValue.sda = NEW; smartValue.sdb = NEW;   // 桩：批处理已更新服务端被动缓存
      if (!cacheOverride) {
        smartMeta.sda = { sampledAt: Date.now() / 1000, fresh: true };
        smartMeta.sdb = { sampledAt: Date.now() / 1000, fresh: true };
      }
      return reply(receipt({ force }));
    });

    const fillPosts = [];
    let fillMode = 'normal';
    let fillActive = 0, maxFillActive = 0;
    let authMode = 'owner';
    let gateNextAuth = false, authActive = 0, maxAuthActive = 0;
    await page.route('**/api/auth/session', async route => {
      const base = { auth_mode: 'password', client_ip: '127.0.0.1', session: null, config: {} };
      const modes = {
        owner: { logged_in: true, auth_required: true, authorized: true, dev_mode: false, owner_authorized: true },
        open_dev: { logged_in: false, auth_required: false, authorized: true, dev_mode: true, owner_authorized: true },
        password_dev: { logged_in: true, auth_required: true, authorized: true, dev_mode: true, owner_authorized: true },
        pair: { logged_in: false, auth_required: true, authorized: true, dev_mode: false, owner_authorized: true },
        need_login: { logged_in: false, auth_required: true, authorized: false, dev_mode: false, owner_authorized: false },
        dev_only_old: { logged_in: false, auth_required: true, authorized: true, dev_mode: true },
      };
      authActive += 1;
      maxAuthActive = Math.max(maxAuthActive, authActive);
      try {
        if (gateNextAuth) {
          gateNextAuth = false;
          await new Promise(resolve => {
            releaseAuthGate = () => { releaseAuthGate = null; resolve(); };
          });
        }
        return await route.fulfill({ status: 200, contentType: 'application/json',
          body: JSON.stringify({ ...base, ...(modes[authMode] || modes.owner) }) });
      } finally { authActive -= 1; }
    });
    const refreshPosts = [];
    let refreshMode = 'normal';
    await page.route('**/api/r1/storage/refresh', async route => {
      const request = route.request();
      refreshPosts.push({ method: request.method(), headers: request.headers(), body: request.postData() || '' });
      if (refreshMode === 'busy') return await route.fulfill({ status: 409, contentType: 'application/json',
        body: JSON.stringify({ ok: false, reason: 'busy' }) });
      if (refreshMode === 'standby') return await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
          smart: { ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
                   results: [{ device: 'sda', status: 'skipped_standby', reason: 'standby', sampled_at: null }] },
          capacity: { ok: true, status: 'complete', started_at: NOW - 2, finished_at: NOW,
                      results: [{ mount_id: 101, status: 'skipped_standby', reason: 'standby', sampled_at: null }] } }) });
      const sampled = Date.now() / 1000;
      for (const device of ['sda', 'sdb']) {
        smartMeta[device] = { sampledAt: sampled, fresh: true };
        capState[device].smartStamp = sampled;
      }
      return await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, force: false, status: 'complete', started_at: sampled - 2, finished_at: sampled,
          smart: { ok: true, force: false, status: 'complete', started_at: sampled - 2, finished_at: sampled,
                   results: [{ device: 'sda', status: 'refreshed', reason: null, sampled_at: sampled }] },
          capacity: { ok: true, status: 'complete', started_at: sampled - 2, finished_at: sampled,
                      results: [{ mount_id: 101, status: 'refreshed', reason: null, sampled_at: sampled }] } }) });
    });
    await page.route('**/api/r1/storage/fill', async route => {
      const request = route.request();
      fillActive += 1;
      maxFillActive = Math.max(maxFillActive, fillActive);
      try {
        fillPosts.push({ method: request.method(), headers: request.headers(), body: request.postData() || '' });
        const okBody = { ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW,
          smart: { ok: true, force: false, status: 'complete', started_at: NOW - 2, finished_at: NOW, results: [] },
          capacity: { ok: true, status: 'complete', started_at: NOW - 2, finished_at: NOW, results: [] } };
        if (fillMode === 'badForce') { const body = JSON.parse(JSON.stringify(okBody)); body.force = true; return await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }); }
        if (fillMode === 'badSmart') { const body = JSON.parse(JSON.stringify(okBody)); delete body.smart; return await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }); }
        if (fillMode === 'unknownStatus') { const body = JSON.parse(JSON.stringify(okBody)); body.smart.results = [{ device: 'sda', status: 'weird', reason: null, sampled_at: NOW }]; return await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }); }
        if (fillMode === 'dupDevice') { const body = JSON.parse(JSON.stringify(okBody)); body.smart.results = [{ device: 'sda', status: 'refreshed', reason: null, sampled_at: NOW }, { device: 'sda', status: 'refreshed', reason: null, sampled_at: NOW }]; return await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }); }
        if (fillMode === 'gated') {
          fillMode = 'normal'; // One-shot gate: later serial requests must never acquire a second waiter.
          await new Promise(resolve => {
            releaseFillGate = () => { fillMode = 'normal'; releaseFillGate = null; resolve(); };
          });
        }
        if (fillMode === 'busy') {
          return await route.fulfill({ status: 409, contentType: 'application/json',
                                 body: JSON.stringify({ ok: false, reason: 'busy', error: '已有补齐正在进行' }) });
        }
        if (fillMode === 'standby') {
          okBody.smart.results = ['sda', 'sdb'].map(device =>
            ({ device, status: 'skipped_standby', reason: 'standby', sampled_at: null }));
          okBody.capacity.results = [{ mount_id: 1, status: 'skipped_standby', reason: 'standby', sampled_at: null }];
        }
        return await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(okBody) });
      } finally { fillActive -= 1; }
    });
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    // 可见且缺测（sdb 无容量样本）→ 自动补齐一次；随后不重复。
    await page.waitForTimeout(1800);
    check('可见缺测自动补齐一次', fillPosts.length === 1
      && fillPosts[0].method === 'POST' && fillPosts[0].body === '{}'
      && /application\/json/.test(fillPosts[0].headers['content-type'] || '')
      && fillPosts[0].headers['x-fnm-request'] === '1');
    await page.waitForTimeout(1600);
    check('补齐后不重复请求', fillPosts.length === 1);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(1500);
    check('hidden 不再补齐', fillPosts.length === 1);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const refreshBeforeStorage = refreshPosts.length;
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await page.waitForSelector('#layout-storage .bay[data-sheet="disk/sda"]', { timeout: 10000 });
    // 进入 storage 页本身即一次主动 refresh（即使缓存有效），并用严格 {}。
    await page.waitForTimeout(1200);
    const refreshAtStorage = refreshPosts.length;
    check('进入 storage 页触发一次 refresh', refreshAtStorage === refreshBeforeStorage + 1
      && refreshPosts.slice(-1).every(row => row.method === 'POST' && row.body === '{}'));

    // 每次进入不同 disk 详情各自一次 refresh；停留不重复；hidden 返回再形成新周期。
    let baseRefresh = refreshPosts.length;
    await openDisk('sda');
    await page.waitForTimeout(900);
    check('进入 disk/sda 详情刷新一次', refreshPosts.length === baseRefresh + 1);
    await page.waitForTimeout(2200);
    check('停留 disk/sda 不重复刷新', refreshPosts.length === baseRefresh + 1);
    baseRefresh = refreshPosts.length;
    await closeSheet();
    await page.waitForTimeout(900);
    check('关闭磁盘详情返回 storage 再刷新一次', refreshPosts.length === baseRefresh + 1);
    baseRefresh = refreshPosts.length;
    await openDisk('sdb');
    await page.waitForTimeout(900);
    check('换到 disk/sdb 再刷新一次', refreshPosts.length === baseRefresh + 1);
    baseRefresh = refreshPosts.length;
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(900);
    check('hidden 返回同一 disk 再次刷新', refreshPosts.length === baseRefresh + 1);
    await closeSheet();
    await page.waitForTimeout(900);
    refreshMode = 'busy';
    baseRefresh = refreshPosts.length;
    await openDisk('sda');
    await page.waitForTimeout(900);
    check('busy 仍只尝试一次', refreshPosts.length === baseRefresh + 1);
    await page.waitForTimeout(2200);
    check('busy 不自动重试', refreshPosts.length === baseRefresh + 1);
    refreshMode = 'normal';
    await closeSheet();

    // 先让两块盘都留下旧缓存（8），再从 sda 发起一次批量刷新。
    await openDisk('sda');
    await waitTable(OLD);
    await closeSheet();
    await openDisk('sdb');
    await page.evaluate(() => { if (typeof Poll !== 'undefined' && Poll.run) Poll.run('overview'); });
    await page.waitForTimeout(2600);
    // 容量卡由 MutationObserver 在 SMART 卡之后异步挂载，先等挂载再读文本，避免时序竞态。
    await page.waitForSelector('#sheetBody [data-ref="r1CapNote"]', { timeout: 10000 });
    check('无容量样本盘显示尚未采集', (await page.textContent('#sheetBody [data-ref="r1CapNote"]')).includes('尚未采集容量'));
    capState.sdb = { stamp: NOW - 3600, used: 500, total: 2000, status: 'ok' };
    await waitTable(OLD);

    // 正式回归：同一开着的抽屉，旧时间 → 更新 /api/overview → 不重开即得到新时间。
    const capBase = capPosts.length;
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => {
      const note = document.querySelector('#sheetBody [data-ref="r1CapNote"]');
      return note && note.textContent.includes('采样于');
    }, null, { timeout: 8000 });
    const capOldNote = await page.textContent('#sheetBody [data-ref="r1CapNote"]');
    capState.sdb = { stamp: NOW + 60, used: 640, total: 2000, status: 'ok' };
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(previous => {
      const note = document.querySelector('#sheetBody [data-ref="r1CapNote"]');
      return note && note.textContent !== previous && note.textContent.includes('采样于');
    }, capOldNote, { timeout: 8000 });
    check('当前抽屉不重开也能看到新采样时间',
      (await page.textContent('#sheetBody [data-ref="r1CapNote"]')) !== capOldNote);
    check('缓存投影更新不追加容量 POST', capPosts.length === capBase);
    await closeSheet();
    await openDisk('sda');
    await waitTable(OLD); // Entry refresh invalidates the detail cache; wait for its passive GET as well.
    check('两盘旧缓存均为 8', (await tableText()).includes(String(OLD)));
    check('打开详情不追加手动 SMART 请求', posts.length === 0);
    check('默认未勾选强制', await page.isChecked('#sheetBody [data-ref="r1SmartForce"]') === false);
    check('提示后台不自动补采', (await page.textContent('#sheetBody [data-ref="r1Smart"]')).includes('后台不会自动补采'));
    const snapshotNote = await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]');
    check('展示被动缓存采样时间并声明非实时',
      snapshotNote.includes('SMART 快照，非实时') && snapshotNote.includes('采样于')
      && !snapshotNote.includes('采样时间未知') && !snapshotNote.includes('观测已过期'));
    check('打开详情仍无手动 SMART POST', posts.length === 0);

    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    check('点击发出一次请求', posts.length === 1);
    check('请求为 POST + JSON + CSRF 头', posts[0].method === 'POST'
      && /application\/json/.test(posts[0].headers['content-type'] || '')
      && posts[0].headers['x-fnm-request'] === '1');
    check('默认 force=false', JSON.parse(posts[0].body).force === false);
    check('仍在当前磁盘详情且未跳走', await page.evaluate(() =>
      document.querySelector('#sheetBody [data-ref="r1Smart"]')?.dataset.device === 'sda'));
    await waitTable(NEW);
    check('当前盘属性从旧值更新为新值', (await tableText()).includes(String(NEW)));
    check('刷新后快照时间随被动缓存更新',
      (await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]')).includes('SMART 快照，非实时'));
    check('缓存更新未追加 POST', posts.length === 1 && detailReads.filter(d => d === 'sda').length >= 2);

    // 另一块盘此前也看过旧缓存：切换过去应拿到新值（批量失效生效），且不追加 POST。
    await closeSheet();
    await openDisk('sdb');
    await waitTable(NEW);
    check('批量失效覆盖其它盘', (await tableText()).includes(String(NEW)));
    check('查看另一盘仍只有一次手动 SMART POST', posts.length === 1);

    // 强制刷新：默认未勾选，勾选只改提示，刷新后复位。
    await page.check('#sheetBody [data-ref="r1SmartForce"]');
    check('勾选只改提示不发请求', posts.length === 1
      && (await page.textContent('#sheetBody [data-ref="r1SmartState"]')).includes('唤醒'));
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    check('显式强制 force=true', posts.length === 2 && JSON.parse(posts[1].body).force === true);
    await waitCard();
    check('刷新后强制项复位', await page.isChecked('#sheetBody [data-ref="r1SmartForce"]') === false);

    // —— 容量手动刷新（与 SMART 卡分离）——
    const capIdle = () => page.waitForFunction(() => {
      const button = document.querySelector('#sheetBody [data-ref="r1CapGo"]');
      return button && !button.disabled;
    }, null, { timeout: 8000 });
    await waitCard();
    check('容量按钮存在且无手动容量 POST', capPosts.length === 0
      && await page.isVisible('#sheetBody [data-ref="r1CapGo"]'));
    await page.evaluate(async () => {
      if (typeof Poll === 'undefined' || !Poll.run) return;
      await Poll.run('overview');
      await Poll.run('r1CapacitySync');
    });
    await page.waitForTimeout(300);
    const capNoteInitial = await page.textContent('#sheetBody [data-ref="r1CapNote"]');
    check('容量提示为按需快照而非实时',
      capNoteInitial.includes('容量快照') && !capNoteInitial.includes('实时'));
    await page.waitForTimeout(900);
    check('等待不触发容量 POST', capPosts.length === 0);
    await page.click('#sheetBody [data-ref="r1CapGo"]');
    await capIdle();
    check('点击容量刷新只发一次空对象', capPosts.length === 1
      && capPosts[0].method === 'POST' && (capPosts[0].body === '{}' || capPosts[0].body === '')
      && /application\/json/.test(capPosts[0].headers['content-type'] || '')
      && capPosts[0].headers['x-fnm-request'] === '1');
    check('容量回执显示完成时间', (await page.textContent('#sheetBody [data-ref="r1CapState"]')).includes('完成于'));
    // 让被动 /api/overview 也随之更新，验证当前抽屉无需重开即可看到新采样时间。
    capState.sda = { stamp: NOW, used: 1200, total: 4000, status: 'ok' };
    capState.sdb = { stamp: NOW, used: 600, total: 2000, status: 'ok' };
    await page.evaluate(() => { if (typeof Poll !== 'undefined' && Poll.run) Poll.run('overview'); });
    const capNoteAfter = await page.textContent('#sheetBody [data-ref="r1CapNote"]');
    check('同步后提示不含原文或实时承诺', capNoteAfter.includes('容量快照'));
    check('同步不追加容量 POST', capPosts.length === 1);
    await page.waitForTimeout(600);
    check('轮询不追加容量 POST', capPosts.length === 1
      && (await page.textContent('#sheetBody [data-ref="r1CapNote"]')) === capNoteAfter);
    capMode = 'busy';
    await page.click('#sheetBody [data-ref="r1CapGo"]');
    await capIdle();
    check('容量 409 显示进行中', (await page.textContent('#sheetBody [data-ref="r1CapState"]')).includes('已有刷新正在进行'));
    capMode = 'partial';
    await page.click('#sheetBody [data-ref="r1CapGo"]');
    await capIdle();
    check('容量 partial 逐项呈现', (await page.textContent('#sheetBody [data-ref="r1CapRows"]')).includes('失败'));
    for (const bad of ['bad_stamp', 'bad_mount', 'missing_key']) {
      capMode = bad;
      await page.click('#sheetBody [data-ref="r1CapGo"]');
      await capIdle();
      const text = await page.textContent('#sheetBody [data-ref="r1CapState"]');
      check('容量畸形回执不冒充成功：' + bad, text.includes('回执无法确认') && !text.includes('完成于'));
    }
    capMode = 'slow';
    const capBefore = capPosts.length;
    await page.click('#sheetBody [data-ref="r1CapGo"]');
    await page.waitForTimeout(120);
    check('容量 busy 禁用按钮', await page.isDisabled('#sheetBody [data-ref="r1CapGo"]'));
    await page.locator('#sheetClose').click();
    await page.waitForTimeout(900);
    check('容量在途关闭不抢回抽屉',
      await page.evaluate(() => !document.querySelector('#sheet').classList.contains('on'))
      && capPosts.length === capBefore + 1);
    capMode = 'normal';
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await openDisk('sda');
    await waitCard();
    await capIdle();

    // 在途切到另一块磁盘：完成后当前盘（sda）看到结果，不跳回发起盘（sdb）。
    mode = 'slow';
    const busyBase = posts.length;
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await page.waitForTimeout(120);
    check('busy 时按钮与强制项都被禁用',
      await page.isDisabled('#sheetBody [data-ref="r1SmartGo"]')
      && await page.isDisabled('#sheetBody [data-ref="r1SmartForce"]'));
    await closeSheet();
    await openDisk('sda');
    check('切盘不追加手动 SMART 请求', posts.length === busyBase + 1);
    await waitIdle();
    const currentDevice = await page.evaluate(() =>
      document.querySelector('#sheetBody [data-ref="r1Smart"]')?.dataset.device || '');
    check('完成结果留在当前可见盘', currentDevice === 'sda'
      && (await page.textContent('#sheetBody [data-ref="r1SmartState"]')).includes('完成于'));

    // 在途关闭抽屉：完成后不得自动重新打开。
    mode = 'slow';
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await page.waitForTimeout(100);
    await closeSheet();
    await page.waitForTimeout(900);
    check('在途关闭后不被自动重开',
      await page.evaluate(() => !document.querySelector('#sheet').classList.contains('on')));

    // 在途切到非磁盘详情（CPU）：完成后不得替换成磁盘抽屉。
    await openDisk('sda');
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await page.waitForTimeout(100);
    await closeSheet();
    await page.locator('#pageSeg button[data-page="overview"]').click();
    await page.locator('.ring-wrap').click();
    await page.waitForTimeout(900);
    check('在途切到其它详情不被抢回磁盘',
      await page.evaluate(() => !document.querySelector('#sheetBody [data-ref="r1Smart"]')));
    await closeSheet();
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await openDisk('sda');
    mode = 'busy';
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    check('409 显示进行中而不是成功',
      (await page.textContent('#sheetBody [data-ref="r1SmartState"]')).includes('已有刷新正在进行'));

    const invalidModes = ['force_string', 'results_object', 'time_string', 'time_order',
                          'force_mismatch', 'duplicate_device', 'missing_keys'];
    const beforeInvalid = posts.length;
    for (const invalid of invalidModes) {
      mode = invalid;
      await waitCard();
      await page.click('#sheetBody [data-ref="r1SmartGo"]');
      await waitIdle();
      const text = await page.textContent('#sheetBody [data-ref="r1SmartState"]');
      check('畸形回执不冒充成功：' + invalid, text.includes('回执无法确认') && !text.includes('完成于'));
    }
    check('畸形回执每个只发一次请求', posts.length === beforeInvalid + invalidModes.length);

    // fresh=false：显示过期未知说明；sampled_at=0：显示采样时间未知且不造时间。
    mode = 'normal';
    cacheOverride = { sampledAt: NOW - 7200, fresh: false };
    await waitCard();
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    const expiredNote = await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]');
    check('过期观测显示未知健康提示',
      expiredNote.includes('观测已过期，当前健康未知，请手动刷新')
      && !expiredNote.includes('采样时间未知'));
    mode = 'normal';
    cacheOverride = { sampledAt: 0, fresh: true };
    smartValue.sdb = OLD;
    await closeSheet();
    await openDisk('sdb');
    await page.waitForTimeout(200);
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    await page.waitForTimeout(200);
    const unknownTime = await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]');
    check('缺采样时间显示未知且不造时间',
      unknownTime.includes('采样时间未知') && !/采样于\s*\d{4}/.test(unknownTime));
    cacheOverride = null;

    await page.setViewportSize({ width: 320, height: 760 });
    await page.waitForTimeout(200);
    check('320px 无横向滚动', await page.evaluate(() => document.documentElement.scrollWidth <= 321));
    check('320px 按钮可见', await page.isVisible('#sheetBody [data-ref="r1SmartGo"]'));
    check('无页面脚本错误', pageErrors.length === 0);

    // —— 补齐规则的剩余分支：有有效样本 0 次；busy / 畸形回执只一次不重试 ——
    capState.sda = { stamp: NOW, used: 1000, total: 4000, status: 'ok' };
    capState.sdb = { stamp: NOW, used: 500, total: 2000, status: 'ok' };
    let fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('有有效缓存时 0 次补齐', fillPosts.length === fillBase);

    capState.sda.stamp = null;
    capState.sdb.stamp = null;
    fillMode = 'busy';
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('busy 只尝试一次', fillPosts.length === fillBase + 1);
    await page.waitForTimeout(1600);
    check('busy 后不循环重试', fillPosts.length === fillBase + 1);
    fillMode = 'normal';

    // SMART/温度缺测也能触发补齐（容量样本已存在）
    capState.sda = { stamp: NOW, used: 1000, total: 4000, status: 'ok', smartStamp: null, smartFresh: false, smartAvailable: false, temp: null };
    capState.sdb = { stamp: NOW, used: 500, total: 2000, status: 'ok', smartStamp: null, smartFresh: false, smartAvailable: false, temp: null };
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('仅 SMART/温度缺测也补齐一次', fillPosts.length === fillBase + 1);
    await page.waitForTimeout(1200);
    check('SMART 补齐后不重复', fillPosts.length === fillBase + 1);

    // owner 语义按生产：open+dev、password 会话+dev、pair 都是 owner；需登录与 dev-only 旧服务不是。
    authMode = 'need_login';
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('需密码未登录 0 次补齐', fillPosts.length === fillBase);
    authMode = 'dev_only_old';
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('旧服务 dev-only 不能当 owner', fillPosts.length === fillBase);
    capState.sda = { stamp: null, used: null, total: null, status: 'unavailable' };
    capState.sdb = { stamp: null, used: null, total: null, status: 'unavailable' };
    for (const mode of ['open_dev', 'password_dev', 'pair']) {
      authMode = mode;
      fillBase = fillPosts.length;
      await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
      await page.waitForTimeout(1800);
      check('owner 证明可用：' + mode, fillPosts.length === fillBase + 1);
    }
    authMode = 'owner';

    // Hold the real owner GET across visibility epochs; its eventual POST consumes the new epoch.
    for (const negative of ['busy', 'standby']) {
      gateNextAuth = true;
      maxAuthActive = 0;
      fillMode = negative;
      fillBase = fillPosts.length;
      await page.goto(info.primary.origin + '/', { waitUntil: 'domcontentloaded' });
      const authDeadline = Date.now() + 6000;
      while (releaseAuthGate === null && Date.now() < authDeadline) {
        await new Promise(resolve => setTimeout(resolve, 30));
      }
      check('认证 GET 确实在途且未发补齐：' + negative,
        typeof releaseAuthGate === 'function' && authActive === 1 && fillPosts.length === fillBase);
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
        document.dispatchEvent(new Event('visibilitychange'));
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await page.waitForTimeout(200);
      check('认证跨 hide/show 仍仅一个 GET 且无提前 POST：' + negative,
        authActive === 1 && maxAuthActive === 1 && fillPosts.length === fillBase);
      releaseAuthGate();
      const postDeadline = Date.now() + 6000;
      while (fillPosts.length === fillBase && Date.now() < postDeadline) {
        await new Promise(resolve => setTimeout(resolve, 30));
      }
      check('认证释放后当前周期仅一个 POST：' + negative, fillPosts.length === fillBase + 1);
      await page.waitForTimeout(2600);
      check('认证跨代负结果经过两个 ticker 仍严格一次：' + negative,
        fillPosts.length === fillBase + 1 && authActive === 0 && fillActive === 0 && releaseAuthGate === null);
    }
    fillMode = 'normal';

    // B: 容量有效但 SMART 过期/无温度 → 补齐一次；全有效 → 0
    capState.sda = { stamp: NOW, used: 1000, total: 4000, status: 'ok', smartFresh: false, temp: null };
    capState.sdb = { stamp: NOW, used: 500, total: 2000, status: 'ok', smartFresh: false, temp: null };
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('SMART 过期/无温度也算缺测', fillPosts.length === fillBase + 1);
    await page.waitForTimeout(1200);
    check('SMART 过期补齐不重复', fillPosts.length === fillBase + 1);
    capState.sda = { stamp: NOW, used: 1000, total: 4000, status: 'ok', smartFresh: true, temp: 39 };
    capState.sdb = { stamp: NOW, used: 500, total: 2000, status: 'ok', smartFresh: true, temp: 38 };
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('容量与 SMART 全有效 0 次', fillPosts.length === fillBase);

    // Null remains unknown in the real overview fields, never numeric zero.
    capState.sda = { stamp: NOW, used: null, total: 4000, status: 'cached', smartFresh: true, temp: 39 };
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('容量有采样时间但 used 为 null 仍补齐一次', fillPosts.length === fillBase + 1
      && overviewRows.find(row => row.name === 'sda').used === null);
    await page.waitForTimeout(1200);
    check('容量 used 缺测补齐不循环', fillPosts.length === fillBase + 1);

    capState.sda = { stamp: NOW, used: 1000, total: 4000, status: 'cached',
      smartStamp: Date.now() / 1000, smartFresh: true, smartAvailable: false, temp: null };
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    const noSmartRow = overviewRows.find(row => row.name === 'sda');
    check('fresh 但温度与 SMART 均缺测仍补齐一次', fillPosts.length === fillBase + 1
      && noSmartRow.smart_fresh === true && noSmartRow.smart_available === false
      && noSmartRow.temp === null && noSmartRow.temp_c === null);
    await page.waitForTimeout(1200);
    check('fresh 的温度与 SMART 缺测补齐不循环', fillPosts.length === fillBase + 1);

    capState.sda.smartAvailable = true;
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(1800);
    check('fresh SMART 健康数据无温度仍为有效缓存', fillPosts.length === fillBase);

    // 畸形回执：各只一次、不投影、不重试
    capState.sda = { stamp: null, used: null, total: null, status: 'unavailable' };
    capState.sdb = { stamp: null, used: null, total: null, status: 'unavailable' };
    for (const bad of ['badForce', 'badSmart', 'unknownStatus', 'dupDevice']) {
      fillMode = bad;
      fillBase = fillPosts.length;
      await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
      const deadline = Date.now() + 5000;
      while (fillPosts.length === fillBase && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      check('畸形 fill 回执只一次且不重试：' + bad, fillPosts.length === fillBase + 1);
      await page.waitForTimeout(1200);
      check('畸形 fill 回执无循环：' + bad, fillPosts.length === fillBase + 1);
    }
    fillMode = 'normal';

    // 在途 hide/show：最多一个在途，旧响应不抢导航也不永久锁 busy
    capState.sda = { stamp: null, used: null, total: null, status: 'unavailable' };
    capState.sdb = { stamp: null, used: null, total: null, status: 'unavailable' };
    fillMode = 'gated';
    maxFillActive = 0;
    fillBase = fillPosts.length;
    await page.goto(info.primary.origin + '/', { waitUntil: 'domcontentloaded' });
    const inflightDeadline = Date.now() + 6000;
    while (releaseFillGate === null && Date.now() < inflightDeadline) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    check('慢响应期间只有一个在途', fillPosts.length === fillBase + 1
      && fillActive === 1 && maxFillActive === 1 && typeof releaseFillGate === 'function');
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(400);
    check('在途时 hide/show 不发第二个请求', fillPosts.length === fillBase + 1
      && fillActive === 1 && maxFillActive === 1);
    releaseFillGate();
    await page.waitForTimeout(1500);
    check('旧响应释放后在途不重复并发（新周期最多再一次）',
      fillPosts.length >= fillBase + 1 && fillPosts.length <= fillBase + 2 && maxFillActive === 1);
    check('deferred 门释放后无悬挂请求', fillActive === 0 && releaseFillGate === null && fillMode === 'normal');
    fillMode = 'normal';

    check('补齐自动请求均为 force 无关空对象', fillPosts.every(row => row.body === '{}'));
    console.log('PASS: ' + checks.length + ' checks; smart POST=' + posts.length
      + '; fill POST=' + fillPosts.length + '; capacity POST=' + capPosts.length
      + '; detail GET=' + detailReads.length
      + '; page errors=' + pageErrors.length);
  } finally {
    if (releaseAuthGate) releaseAuthGate();
    if (releaseFillGate) releaseFillGate();
    if (browser) await browser.close();
    child.kill();
    fs.rmSync(temporary, { recursive: true, force: true });
    fs.rmSync(fixtureDir, { recursive: true, force: true });
  }
})().catch(error => {
  console.error('FAIL: ' + (error && error.stack || error));
  process.exit(1);
});
