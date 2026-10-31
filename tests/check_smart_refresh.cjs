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
const NOW = 1_760_000_000;
const OLD = 8, NEW = 12;

function receipt({ force = false, status = 'complete', started = NOW - 2, finished = NOW, results }) {
  return { ok: true, force, status, started_at: started, finished_at: finished,
           results: results || [
             { device: 'sda', status: 'refreshed', reason: null, sampled_at: finished },
             { device: 'sdb', status: 'refreshed', reason: null, sampled_at: finished }] };
}
function smartDetail(raw, { sampledAt = NOW - 3600, fresh = true } = {}) {
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
  const smartMeta = { sda: { sampledAt: NOW - 3600, fresh: true },
                      sdb: { sampledAt: NOW - 3600, fresh: true } };
  let cacheOverride = null;
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
      return route.fulfill({ status: 200, contentType: 'application/json',
                             body: JSON.stringify({ ...smartDetail(smartValue[device] || OLD,
                               cacheOverride || smartMeta[device] || {}), dev: device }) });
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
        smartMeta.sda = { sampledAt: NOW, fresh: true };
        smartMeta.sdb = { sampledAt: NOW, fresh: true };
      }
      return reply(receipt({ force }));
    });

    await page.goto(info.primary.origin + '/', { waitUntil: 'networkidle' });
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await page.waitForSelector('#layout-storage .bay[data-sheet="disk/sda"]', { timeout: 10000 });

    // 先让两块盘都留下旧缓存（8），再从 sda 发起一次批量刷新。
    await openDisk('sda');
    await waitTable(OLD);
    await closeSheet();
    await openDisk('sdb');
    await waitTable(OLD);
    await closeSheet();
    await openDisk('sda');
    check('两盘旧缓存均为 8', (await tableText()).includes(String(OLD)));
    check('打开详情零请求', posts.length === 0);
    check('默认未勾选强制', await page.isChecked('#sheetBody [data-ref="r1SmartForce"]') === false);
    check('提示后台不自动补采', (await page.textContent('#sheetBody [data-ref="r1Smart"]')).includes('后台不会自动补采'));
    const snapshotNote = await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]');
    check('展示被动缓存采样时间并声明非实时',
      snapshotNote.includes('手动快照，非实时') && snapshotNote.includes('采样于')
      && !snapshotNote.includes('采样时间未知') && !snapshotNote.includes('观测已过期'));
    check('打开详情仍是零 POST', posts.length === 0);

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
      (await page.textContent('#sheetBody [data-ref="r1SmartSnapshot"]')).includes('手动快照，非实时'));
    check('缓存更新未追加 POST', posts.length === 1 && detailReads.filter(d => d === 'sda').length >= 2);

    // 另一块盘此前也看过旧缓存：切换过去应拿到新值（批量失效生效），且不追加 POST。
    await closeSheet();
    await openDisk('sdb');
    await waitTable(NEW);
    check('批量失效覆盖其它盘', (await tableText()).includes(String(NEW)));
    check('查看另一盘仍只有一次 POST', posts.length === 1);

    // 强制刷新：默认未勾选，勾选只改提示，刷新后复位。
    await page.check('#sheetBody [data-ref="r1SmartForce"]');
    check('勾选只改提示不发请求', posts.length === 1
      && (await page.textContent('#sheetBody [data-ref="r1SmartState"]')).includes('唤醒'));
    await page.click('#sheetBody [data-ref="r1SmartGo"]');
    await waitIdle();
    check('显式强制 force=true', posts.length === 2 && JSON.parse(posts[1].body).force === true);
    await waitCard();
    check('刷新后强制项复位', await page.isChecked('#sheetBody [data-ref="r1SmartForce"]') === false);

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
    check('切盘不追加请求', posts.length === busyBase + 1);
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
    console.log('PASS: ' + checks.length + ' checks; posts=' + posts.length
      + '; detail GET=' + detailReads.length + '; page errors=' + pageErrors.length);
  } finally {
    if (browser) await browser.close();
    child.kill();
    fs.rmSync(temporary, { recursive: true, force: true });
    fs.rmSync(fixtureDir, { recursive: true, force: true });
  }
})().catch(error => {
  console.error('FAIL: ' + (error && error.stack || error));
  process.exit(1);
});
