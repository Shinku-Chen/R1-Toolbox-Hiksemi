// Permanent real owner HTTP -> sampler publication -> monitor/Fnm -> built UI.
// Only TEMP kernel metadata, SMART input and worker/OS seams are controlled.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-capacity-observation-'));
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, 'Set CHROME_BIN to an installed Chrome/Chromium executable');

(async () => {
  const child = spawn(process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3'),
    ['-B', '-u', path.join(__dirname, 'fixtures/capacity_observation_fixture.py'), root, fixtureDir],
    { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let browser, page, stderr = '', sequence = 0, resolveReady, rejectReady, currentCase = '', phase = 'startup';
  const waiting = new Map(), checks = [], evidence = [], requests = [], pageErrors = [], external = [], blocked = [];
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const startupTimer = setTimeout(() => rejectReady(new Error('capacity fixture startup timeout')), 15000);
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  child.on('error', rejectReady);
  child.on('exit', code => { if (code) rejectReady(new Error('capacity fixture exited ' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(startupTimer); resolveReady(value); }
    if (waiting.has(value.id)) { waiting.get(value.id)(value); waiting.delete(value.id); }
  });
  const operation = name => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { waiting.delete(id); reject(new Error('fixture operation timeout: ' + name)); }, 10000);
    waiting.set(id, result => { clearTimeout(timer); resolve(result); });
    child.stdin.write(JSON.stringify({ id, operation: name }) + '\n');
  });
  const check = (name, condition, detail) => {
    assert.ok(condition, name + (detail ? ': ' + JSON.stringify(detail) : ''));
    checks.push(name);
  };
  const api = (pathname, method = 'GET', headers = {}) => page.evaluate(async ({ pathname, method, headers }) => {
    const response = await fetch(pathname, { method, cache: 'no-store',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-FNM-Request': '1', ...headers } : headers,
      ...(method === 'POST' ? { body: '{}' } : {}) });
    return { status: response.status, body: await response.json() };
  }, { pathname, method, headers });
  const stats = async () => (await operation('stats')).state;
  const currentUnknown = value => value.disk_size === null && value.disk_used === null && value.disk_total === null
    && value.disks_detail.every(row => row.total === null && row.used === null && row.percent === null);
  const historyRows = value => value.disks_detail.filter(row => row.capacity_last_observation !== null);
  const dates = value => [value.sampled_at, value.sampled_at_latest];
  const validate = value => value && value.status === 'historical'
    && value.source === 'last_successful_observation'
    && ['last_successful_measurement', 'mount_continuity_unverified'].includes(value.reason)
    && /^\d+$/.test(value.observation_id) && value.total > 0 && value.used >= 0 && value.used <= value.total
    && value.available === value.total - value.used
    && Math.abs(value.percent - value.used / value.total * 100) < 1e-9
    && value.sampled_at > 0 && value.sampled_at_latest >= value.sampled_at
    && value.coverage.scope === 'measured_filesystem_subset' && value.coverage.complete === false
    && value.coverage.filesystem_count > 0 && value.coverage.disk_count > 0;
  const dom = () => page.locator('#layout-storage [data-mod="storage"]').evaluate(module => ({
    pool: module.querySelector('[data-ref="poolLeg"]').textContent,
    poolHistorical: !!module.querySelector('.r1-pool-hist'),
    bays: [...module.querySelectorAll('.bay')].map(bay => ({
      name: bay.querySelector('.bay-name').textContent,
      text: bay.querySelector('.bay-cap').textContent,
      historical: bay.classList.contains('r1-cap-hist'),
    })),
  }));
  const timeStrings = value => page.evaluate(value => [...new Set([
    new Date(value.sampled_at * 1000).toLocaleString(),
    new Date(value.sampled_at_latest * 1000).toLocaleString(),
  ])], value);
  try {
    const fixture = await ready;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 },
      locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    await context.route('**/*', route => {
      const request = route.request(), raw = request.url();
      if (raw.startsWith('data:')) return route.continue();
      const url = new URL(raw);
      if (url.origin !== fixture.owner) { external.push(raw); return route.abort('blockedbyclient'); }
      const allowed = request.method() === 'GET'
        || request.method() === 'POST' && ['/api/ui', '/api/r1/storage/refresh'].includes(url.pathname);
      if (!allowed) {
        blocked.push({ method: request.method(), pathname: url.pathname });
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    page = await context.newPage();
    await page.clock.install();
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin === fixture.owner && url.pathname.startsWith('/api/'))
        requests.push({ case: currentCase, phase, method: request.method(), pathname: url.pathname });
    });
    const waitPublication = async () => {
      const value = await operation('published');
      check(currentCase + ': real response-write history publisher completed', value.published === true, value.state.calls);
      return value.state;
    };
    const enter = async (scenario, label = scenario) => {
      currentCase = label; phase = 'entry';
      await page.goto('about:blank');
      await operation('case:' + scenario);
      const refresh = page.waitForResponse(response => response.url() === fixture.owner + '/api/r1/storage/refresh'
        && response.request().method() === 'POST');
      await page.goto(fixture.owner + '/#/storage', { waitUntil: 'networkidle' });
      const response = await refresh;
      const body = await response.json();
      check(scenario + ': real page entry performs one ordinary refresh', response.status() === 200 && body.force === false, body);
      await waitPublication();
      await page.waitForSelector('#layout-storage .bay');
      return body;
    };
    const nextFrame = async command => {
      if (command) await operation(command);
      await operation('frame');
      const next = page.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
      await page.clock.fastForward(15100); await next;
      await page.waitForTimeout(50);
      return (await api('/api/overview')).body;
    };
    const explicitRefresh = async headers => {
      await operation('reset_publish');
      const result = await api('/api/r1/storage/refresh', 'POST', headers);
      if (result.status === 200) await waitPublication();
      return result;
    };

    for (const scenario of ['normal', 'multipart', 'zero', 'partial', 'duplicate']) {
      const refresh = await enter(scenario);
      const initial = (await api('/api/overview')).body;
      const before = await stats();
      const measured = historyRows(initial);
      const summary = initial.capacity_last_observation_summary;
      const expectedNames = scenario === 'partial' ? ['sda', 'sdc']
        : scenario === 'duplicate' ? ['sdb', 'sdc'] : ['sda', 'sdb', 'sdc'];
      const expectedFilesystems = scenario === 'multipart' ? 4 : expectedNames.length;
      check(scenario + ': history comes only from production successful publication',
        before.history_capability && before.published_history_count === expectedFilesystems
        && measured.map(row => row.name).sort().join() === expectedNames.join()
        && measured.every(row => validate(row.capacity_last_observation)) && validate(summary),
        { before, initial });
      check(scenario + ': current capacity remains separate and available before invalidation',
        expectedNames.every(name => initial.disks_detail.find(row => row.name === name).total > 0)
        && initial.disk_size === summary.total && initial.disk_used === summary.used, initial);
      check(scenario + ': summary explicitly describes a measured filesystem subset',
        summary.coverage.filesystem_count === expectedFilesystems
        && summary.coverage.disk_count === expectedNames.length && summary.coverage.complete === false, summary);
      check(scenario + ': ordinary page entry does not repeat while visible',
        before.calls.filter(call => call.phase === 'smart').length === 1
        && before.measurements.length === before.specs.length - (scenario === 'duplicate' ? 2 : 0), before);
      await page.waitForFunction(ts => window.__capacityObservationProbe.ts === ts, initial.ts);
      const currentDOM = await dom();
      check(scenario + ': valid current values keep the ordinary display instead of historical mode',
        !currentDOM.poolHistorical && currentDOM.bays.filter(row => expectedNames.includes(row.name))
          .every(row => !row.historical && !row.text.includes('最近实测')), currentDOM);
      const liveBefore = await page.evaluate(() => window.__capacityObservationProbe.liveDisk);
      phase = 'invalidated';
      const invalidated = await nextFrame('event');
      check(scenario + ': event invalidates every current capacity without rewriting its last observation',
        currentUnknown(invalidated)
        && measured.every(row => {
          const old = row.capacity_last_observation;
          const now = invalidated.disks_detail.find(item => item.name === row.name).capacity_last_observation;
          return validate(now) && now.total === old.total && now.used === old.used
            && now.observation_id === old.observation_id && JSON.stringify(dates(now)) === JSON.stringify(dates(old));
        }) && invalidated.capacity_last_observation_summary.total === summary.total, invalidated);
      await page.waitForFunction(expected => expected.every(name =>
        document.querySelector('.bay[data-sheet="disk/' + name + '"]')?.classList.contains('r1-cap-hist')), expectedNames);
      const firstDOM = await dom();
      const poolDates = await timeStrings(summary);
      check(scenario + ': top-level history is visibly labelled with original dates and subset coverage',
        firstDOM.poolHistorical && firstDOM.pool.includes('最近实测快照') && firstDOM.pool.includes('当前容量未知')
        && firstDOM.pool.includes('子集') && poolDates.every(text => firstDOM.pool.includes(text)), firstDOM);
      check(scenario + ': each measured disk visibly labels history and coverage without a hover dependency',
        expectedNames.every(name => {
          const bay = firstDOM.bays.find(row => row.name === name);
          return bay.historical && bay.text.includes('最近实测') && /已测|文件系统|子集/.test(bay.text);
        }), firstDOM);
      if (scenario === 'zero') check('genuine measured zero use is preserved in historical API and disk display',
        summary.used === 0 && summary.percent === 0 && firstDOM.bays.every(row => row.text.includes('0%')), { summary, firstDOM });
      if (scenario === 'multipart') check('two partitions of one physical disk count as two filesystems, one disk',
        invalidated.disks_detail.find(row => row.name === 'sda').capacity_last_observation.coverage.filesystem_count === 2
        && invalidated.disks_detail.find(row => row.name === 'sda').capacity_last_observation.coverage.disk_count === 1, invalidated);
      if (scenario === 'duplicate') check('duplicate filesystem aliases never count as independent capacity observations',
        invalidated.disks_detail.find(row => row.name === 'sda').capacity_last_observation === null
        && summary.coverage.filesystem_count === 2 && summary.coverage.disk_count === 2
        && before.measurements.every(row => row.name !== 'sda'), { invalidated, before });
      const frames = [invalidated];
      for (let index = 0; index < 3; index++) frames.push(await nextFrame());
      const after = await stats();
      check(scenario + ': repeated passive frames never renew dates or start additional collection',
        frames.every(value => currentUnknown(value)
          && JSON.stringify(value.capacity_last_observation_summary) === JSON.stringify(invalidated.capacity_last_observation_summary))
        && after.measurements.length === before.measurements.length
        && after.calls.filter(call => call.phase === 'smart').length === 1, { before, after });
      check(scenario + ': historical display does not append fake current-capacity curve points',
        JSON.stringify(await page.evaluate(() => window.__capacityObservationProbe.liveDisk)) === JSON.stringify(liveBefore), liveBefore);
      if (scenario === 'normal') {
        phase = 'mobile';
        for (const width of [320, 376]) for (const theme of ['light', 'dark']) {
          await page.setViewportSize({ width, height: 1000 });
          if (await page.locator('html').getAttribute('data-theme') !== theme) await page.locator('#btnTheme').click();
          await page.locator('#layout-storage .nas').scrollIntoViewIfNeeded();
          const layout = await page.locator('#layout-storage').evaluate(layout => ({
            overflow: document.documentElement.scrollWidth > innerWidth + 1,
            caps: [...layout.querySelectorAll('.r1-cap-hist .bay-cap')].map(cap => ({
              text: cap.textContent, font: Number.parseFloat(getComputedStyle(cap).fontSize),
              width: cap.clientWidth, overflow: cap.scrollWidth > cap.clientWidth + 1,
              leafFonts: [...cap.querySelectorAll('.r1-hist-value,.r1-hist-meta')]
                .map(node => Number.parseFloat(getComputedStyle(node).fontSize)),
              name: cap.closest('.bay').querySelector('.bay-name').textContent,
            })),
          }));
          const originalDates = Object.fromEntries(await Promise.all(measured.map(async row =>
            [row.name, await timeStrings(row.capacity_last_observation)])));
          await page.screenshot({ path: path.join(fixtureDir, 'capacity-' + width + '-' + theme + '.png'), fullPage: true });
          check('mobile ' + width + '/' + theme + ': history remains readable without horizontal scrolling',
            !layout.overflow && layout.caps.length === 3
            && layout.caps.every(cap => cap.font >= 12 && cap.leafFonts.length === 2
              && cap.leafFonts.every(font => font >= 12) && cap.width > 0 && !cap.overflow
              && cap.text.includes('最近实测') && /已测|文件系统|子集/.test(cap.text)
              && originalDates[cap.name].every(text => cap.text.includes(text))), layout);
        }
        await page.setViewportSize({ width: 1400, height: 1000 });
      }
      await operation('standby_all');
      await operation('reset_publish');
      phase = 'detail';
      const detailPost = page.waitForResponse(response => response.url() === fixture.owner + '/api/r1/storage/refresh'
        && response.request().method() === 'POST');
      await page.locator('.bay[data-sheet="disk/' + expectedNames[0] + '"]').click();
      await detailPost; await waitPublication();
      await page.waitForSelector('#sheet.on [data-ref="leg"]');
      const detail = await page.locator('#sheetBody [data-ref="leg"]').textContent();
      const currentHero = await page.locator('#sheetBody [data-ref="v"]').getAttribute('aria-label');
      check(scenario + ': disk detail shows original historical snapshot while current headline stays unknown',
        detail.includes('最近实测快照') && detail.includes('当前容量未知') && detail.includes('子集')
        && currentHero === '--', { detail, currentHero });
      await page.keyboard.press('Escape');
      await page.waitForSelector('#sheet.on', { state: 'hidden' });
      phase = 'all_disks';
      await page.locator('#layout-storage [data-sheet="storage"]').click();
      await page.waitForSelector('#sheet.on [data-p="sda"]');
      const allDisks = await page.locator('#sheetBody [data-p]').evaluateAll(rows => rows.map(row => ({
        name: row.dataset.p, text: row.textContent,
      })));
      check(scenario + ': all-disks drawer explicitly labels each available historical subset',
        expectedNames.every(name => allDisks.some(row => row.name === name && row.text.includes('最近实测快照')
          && row.text.includes('子集'))), allDisks);
      await page.keyboard.press('Escape');
      await page.waitForSelector('#sheet.on', { state: 'hidden' });
      evidence.push({ scenario, refresh, initial, invalidated, frames, firstDOM, detail, allDisks, before, after });
    }

    await enter('normal', 'separate_batches');
    const old = (await api('/api/overview')).body;
    const oldA = old.disks_detail.find(row => row.name === 'sda').capacity_last_observation;
    await operation('batch_two');
    const secondReceipt = await explicitRefresh();
    check('second real refresh skips standby A but measures active B and C', secondReceipt.status === 200
      && secondReceipt.body.smart.results.find(row => row.device === 'sda').status === 'skipped_standby'
      && secondReceipt.body.capacity.results.filter(row => row.status === 'refreshed').length === 2, secondReceipt);
    const second = (await api('/api/overview')).body;
    const newSummary = second.capacity_last_observation_summary;
    check('latest summary never mixes an older standby sibling into the newer batch',
      second.disks_detail.find(row => row.name === 'sda').capacity_last_observation.observation_id === oldA.observation_id
      && newSummary.observation_id !== oldA.observation_id
      && newSummary.coverage.disk_count === 2 && newSummary.coverage.filesystem_count === 2
      && newSummary.total === second.disks_detail.filter(row => row.name !== 'sda')
        .reduce((total, row) => total + row.capacity_last_observation.total, 0), second);
    const secondInvalid = await nextFrame('event');
    check('independent latest-batch summary survives current invalidation without pool reconstruction',
      currentUnknown(secondInvalid) && secondInvalid.capacity_last_observation_summary.observation_id === newSummary.observation_id
      && secondInvalid.capacity_last_observation_summary.total === newSummary.total, secondInvalid);
    const replacement = await nextFrame('replace_a');
    check('same device name with a different physical lifetime cannot display the old disk history',
      replacement.disks_detail.find(row => row.name === 'sda').capacity_last_observation === null, replacement);
    check('replaced disk clears only its historical DOM marker and visible text',
      await page.locator('.bay[data-sheet="disk/sda"]').evaluate(bay => !bay.classList.contains('r1-cap-hist')
        && !bay.hasAttribute('data-r1-history') && !bay.querySelector('.r1-hist-meta')));
    const incomplete = await nextFrame('inventory_incomplete');
    check('incomplete kernel mount inventory hides every old observation and current capacity',
      currentUnknown(incomplete) && incomplete.capacity_last_observation_summary === null
      && incomplete.disks_detail.every(row => row.capacity_last_observation === null), incomplete);
    check('hidden historical fields clear all pool and disk DOM markers without collecting',
      await page.locator('#layout-storage').evaluate(layout => layout.querySelectorAll('[data-r1-history],.r1-cap-hist,.r1-pool-hist').length === 0));
    await operation('start');
    const restarted = (await api('/api/overview')).body;
    check('start never revives historical state from a prior sampler lifecycle',
      restarted.capacity_last_observation_summary === null
      && restarted.disks_detail.every(row => row.capacity_last_observation === null), restarted);

    await enter('normal', 'owner_boundaries');
    const granted = (await api('/api/overview')).body;
    const rejectedBearer = await api('/api/overview', 'GET', { Authorization: 'Bearer fixture-unprivileged-key' });
    check('Bearer request cannot borrow an open owner historical projection',
      !('capacity_last_observation_summary' in rejectedBearer.body)
      && (!rejectedBearer.body.disks_detail || rejectedBearer.body.disks_detail.every(row => !('capacity_last_observation' in row))), rejectedBearer);
    const pair = (await operation('auth:pair')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...pair, url: fixture.owner }]);
    const paired = await api('/api/overview');
    check('real pair owner can read the same successful observation', paired.status === 200
      && paired.body.capacity_last_observation_summary.observation_id === granted.capacity_last_observation_summary.observation_id, paired);
    const password = (await operation('auth:password')).cookie;
    await context.clearCookies();
    const beforeDenied = await stats();
    const deniedGet = await api('/api/overview');
    const deniedPost = await api('/api/r1/storage/refresh', 'POST');
    check('unauthenticated password mode reveals no observation and cannot collect',
      deniedGet.status === 401 && deniedPost.status === 403
      && !('capacity_last_observation_summary' in deniedGet.body)
      && (await stats()).measurements.length === beforeDenied.measurements.length, { deniedGet, deniedPost });
    await context.addCookies([{ ...password, url: fixture.owner }]);
    const owner = await api('/api/overview');
    check('real password owner retains the original successful history', owner.status === 200
      && owner.body.capacity_last_observation_summary.observation_id === granted.capacity_last_observation_summary.observation_id, owner);
    await operation('hold');
    const pendingRequest = api('/api/r1/storage/refresh', 'POST');
    check('pending real capacity worker reached the controlled probe boundary', (await operation('entered')).entered === true);
    await operation('revoke');
    await operation('release');
    const revoked = await pendingRequest;
    check('revocation before completion rejects the pending ordinary batch', revoked.status === 403, revoked);
    const relogin = (await operation('auth:password')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...relogin, url: fixture.owner }]);
    const afterRevoke = (await api('/api/overview')).body;
    check('unpublished revoked candidate does not replace last successful observations',
      afterRevoke.capacity_last_observation_summary.observation_id === granted.capacity_last_observation_summary.observation_id
      && JSON.stringify(dates(afterRevoke.capacity_last_observation_summary)) === JSON.stringify(dates(granted.capacity_last_observation_summary)), afterRevoke);
    const malformed = await page.evaluate(valid => {
      const wrongPercent = { ...valid, percent: valid.percent + 1 };
      const wrongAvailable = { ...valid, available: valid.available + 1 };
      return [window.__capacityObservationProbe.validateHistory(wrongPercent),
        window.__capacityObservationProbe.validateHistory(wrongAvailable)];
    }, afterRevoke.capacity_last_observation_summary);
    check('production display validator rejects conflicting percent and unused-byte arithmetic',
      malformed.every(value => value === null), malformed);
    await operation('close');
    const closed = (await api('/api/overview')).body;
    check('closing the sampler lifecycle discards all prior history', closed.capacity_last_observation_summary === null
      && closed.disks_detail.every(row => row.capacity_last_observation === null), closed);
    const final = await stats();
    check('no page errors, external access, unexpected writes, real SMART/statvfs/mount or worker commands',
      pageErrors.length === 0 && external.length === 0 && blocked.length === 0 && final.forbidden.length === 0,
      { pageErrors, external, blocked, forbidden: final.forbidden });
    const evidencePath = path.join(fixtureDir, 'capacity-observation-evidence.json');
    fs.writeFileSync(evidencePath, JSON.stringify({ checks, evidence, secondReceipt, second, replacement,
      incomplete, restarted, granted, paired, owner, revoked, afterRevoke, closed, requests, final,
      ui_evidence: fixture.ui_evidence }, null, 2));
    console.log(JSON.stringify({ ok: true, count: checks.length, fixtureDir, evidencePath }, null, 2));
  } catch (error) {
    const snapshot = page && !page.isClosed() ? await page.evaluate(() => ({
      url: location.hash, bays: [...document.querySelectorAll('.bay-cap')].map(row => row.textContent),
      pool: document.querySelector('[data-ref="poolLeg"]')?.textContent,
    })).catch(() => null) : null;
    console.error(JSON.stringify({ ok: false, currentCase, phase, checks, fixtureDir,
      error: String(error?.stack || error), snapshot, pageErrors, external, blocked, stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(startupTimer);
    if (browser) await browser.close();
    child.stdin.end();
    await new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      const timer = setTimeout(() => { child.kill(); resolve(); }, 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
