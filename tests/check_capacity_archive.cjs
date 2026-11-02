// Owner HTTP -> real core candidate -> Handler JSON/write/finalizer -> publish ->
// independent "past observation archive" regression on the real built page.
//
// Real: WebConfigServer route, owner auth, ordinary POST /api/r1/storage/refresh,
// sampler candidate + cache commit, publish_capacity_history, Fnm projection,
// HTTP JSON response and rendered DOM.
// Controlled (TEMP only): kernel mountinfo/sysfs metadata, mount-watch event,
// sysfs link identities, capacity worker probe seam, SMART/statvfs/subprocess
// blocked by the fixture. No archive/history field is ever injected.
//
// Contract under test (fixed by the shared product decision):
//   GET /api/overview (real password/pair/open owner only) top-level
//   capacity_observation_archive: null | {
//     schema_version:1, status:"historical", source:"past_successful_observation",
//     reason:"current_binding_unverified", observation_id:"<1..2^63-1 decimal string>",
//     total, used, available=total-used, percent=used/total*100,
//     sampled_at, sampled_at_latest, coverage:{scope:"measured_filesystem_subset",
//     filesystem_count, disk_count, complete:false}, retention:"memory_until_restart" }
//   No per-disk archive fields, no automatic bay/pool binding, no current-capacity use.
//
// Scenario groups (unique control flow, counted separately from assertions):
//   S1 normal success archive (fixed copy + served display validator)
//   S2 unrelated mountinfo change     S3 mount alias change
//   S4 disk replacement/lifetime      S5 genuinely incomplete inventory
//   S6 full recovery (no bay revival) S7 partial/multipart/zero/dedupe,
//   failed/cached-only, two batches   S8 owner boundaries + revocation
//   S9 start/close lifecycle clear
//
// Termination: success is printed only after the browser closed cleanly and the
// fixture process exited with code 0 and no forced kill; a failing cleanup turns
// the whole gate red even when every assertion passed. The success serializer
// never runs before cleanup, so a fixture crash cannot be reported as ok.
//
// Display validator: the served production r1CapacityArchive is invoked through
// the TEMP read-only __r1ArchiveProbe getter; Node does not re-implement it.
// Wire-level malformed payloads remain delegated to the backend HTTP tests.
//
// Usage: node tests/check_capacity_archive.cjs
//   --baseline is retired: it prints NOT_RUN and exits 2 before creating any
//   TEMP directory, fixture child or browser. The archived baseline evidence is
//   final; this gate only runs the full owner HTTP/DOM contract.
//   R1_ARCHIVE_TEST_FAULT=exit1|hang|browser-close is a test-only negative
//   control proving the terminal gate fails; it must never be set in CI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

// Retired mode: the archived baseline evidence is final and this gate only runs
// the full owner HTTP/DOM contract. Reject before any TEMP directory, fixture
// child process or browser exists, so nothing can be left running.
if (process.argv.includes('--baseline')) {
  console.log(JSON.stringify({
    ok: false,
    status: 'NOT_RUN',
    mode: 'retired_baseline',
    reason: 'baseline mode retired: archived baseline evidence is final; run the gate without --baseline',
    usage: 'node tests/check_capacity_archive.cjs',
    exit: 2,
  }, null, 2));
  process.exit(2);
}

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-capacity-archive-'));
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));

const OBSERVATION_ID = /^[1-9]\d{0,18}$/;
const ARCHIVE_KEYS = ['schema_version', 'status', 'source', 'reason', 'observation_id', 'total', 'used',
  'available', 'percent', 'sampled_at', 'sampled_at_latest', 'coverage', 'retention'];

function validateArchive(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'not-an-object';
  const keys = Object.keys(value).sort().join();
  if (keys !== [...ARCHIVE_KEYS].sort().join()) return 'key-set:' + keys;
  if (value.schema_version !== 1) return 'schema_version';
  if (value.status !== 'historical') return 'status';
  if (value.source !== 'past_successful_observation') return 'source';
  if (value.reason !== 'current_binding_unverified') return 'reason';
  if (typeof value.observation_id !== 'string' || !OBSERVATION_ID.test(value.observation_id)) return 'observation_id';
  if (BigInt(value.observation_id) > 2n ** 63n - 1n) return 'observation_id-range';
  const { total, used, available, percent, sampled_at: oldest, sampled_at_latest: latest } = value;
  if (!Number.isSafeInteger(total) || total <= 0) return 'total';
  if (!Number.isSafeInteger(used) || used < 0 || used > total) return 'used';
  if (!Number.isSafeInteger(available) || available !== total - used) return 'available';
  if (typeof percent !== 'number' || Math.abs(percent - (used / total) * 100) > 1e-9) return 'percent';
  if (!Number.isFinite(oldest) || oldest <= 0) return 'sampled_at';
  if (!Number.isFinite(latest) || latest < oldest) return 'sampled_at_latest';
  const coverage = value.coverage;
  if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage)) return 'coverage';
  if (Object.keys(coverage).sort().join() !== 'complete,disk_count,filesystem_count,scope') return 'coverage-keys';
  if (coverage.scope !== 'measured_filesystem_subset' || coverage.complete !== false) return 'coverage-scope';
  for (const key of ['filesystem_count', 'disk_count'])
    if (!Number.isInteger(coverage[key]) || coverage[key] < 1) return 'coverage-' + key;
  if (value.retention !== 'memory_until_restart') return 'retention';
  return null;
}

// Genuinely bounded font/layout readiness: Node wall-clock deadline, Playwright
// real waits, and only synchronous page reads (document.fonts.status). A pending
// font promise can never block this loop; the deadline is the real upper bound.
async function settleBounded(page, { deadlineMs = 6000, pollMs = 120, stableFrames = 2 } = {}) {
  const startedAt = Date.now();
  let last = null, stable = 0;
  while (Date.now() - startedAt < deadlineMs) {
    let probe;
    try {
      probe = await page.evaluate(() => ({
        sw: document.documentElement.scrollWidth,
        iw: window.innerWidth,
        cw: document.documentElement.clientWidth,
        fonts: document.fonts ? document.fonts.status : 'unavailable',
      }));
    } catch (error) {
      return { ok: false, reason: 'probe_failed', error: String(error && error.message || error),
        elapsed_ms: Date.now() - startedAt, deadline_ms: deadlineMs };
    }
    if (probe.fonts === 'loaded' && last && probe.sw === last.sw && probe.iw === last.iw) stable += 1;
    else stable = 0;
    last = probe;
    if (stable >= stableFrames) {
      return { ok: true, probe, stable_frames: stable, elapsed_ms: Date.now() - startedAt, deadline_ms: deadlineMs };
    }
    await page.waitForTimeout(pollMs);
  }
  return { ok: false, reason: 'font_or_layout_not_settled', probe: last,
    elapsed_ms: Date.now() - startedAt, deadline_ms: deadlineMs };
}

async function main() {
  const child = spawn(process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3'),
    ['-B', '-u', path.join(__dirname, 'fixtures/capacity_archive_fixture.py'), root, fixtureDir],
    { stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' } });
  let browser, page, stderr = '', sequence = 0, resolveReady, rejectReady, currentCase = 'startup', phase = 'startup';
  const waiting = new Map(), checks = [], newChecks = [], scenarios = new Set(), evidence = [], requests = [];
  const pageErrors = [], consoleErrors = [], external = [], blocked = [];
  // Terminal-reporting state: success may only be printed after the browser and
  // the fixture process have provably finished (exit code 0, no forced kill).
  const childExit = { exited: false, code: null, signal: null };
  const browserClose = { attempted: false, ok: false, error: null };
  let forcedKill = false, exitTimeout = false, cleanupFailure = null, readySettled = false;
  let failure = null, successPayload = null, snapshot = null, getterEvidence = null;
  const fault = process.env.R1_ARCHIVE_TEST_FAULT || '';
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const startupTimer = setTimeout(() => { readySettled = true; rejectReady(new Error('capacity archive fixture startup timeout')); }, 20000);
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  child.on('error', error => { if (!readySettled) { readySettled = true; rejectReady(error); } });
  child.on('exit', (code, signal) => {
    childExit.exited = true; childExit.code = code; childExit.signal = signal;
    if (!readySettled) {
      readySettled = true;
      rejectReady(new Error('capacity archive fixture exited ' + code + ' ' + signal + ' ' + stderr));
    }
  });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(startupTimer); readySettled = true; resolveReady(value); }
    if (waiting.has(value.id)) { waiting.get(value.id)(value); waiting.delete(value.id); }
  });
  const operation = name => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { waiting.delete(id); reject(new Error('fixture operation timeout: ' + name)); }, 12000);
    waiting.set(id, result => { clearTimeout(timer); resolve(result); });
    child.stdin.write(JSON.stringify({ id, operation: name }) + '\n');
  });
  const check = (name, condition, detail) => {
    assert.ok(condition, currentCase + ' / ' + name + (detail !== undefined ? ': ' + JSON.stringify(detail) : ''));
    checks.push(name);
  };
  // Assertions added when the cleanup/validator gap was closed; counted apart
  // from the pre-existing scenario assertions.
  const checkNew = (name, condition, detail) => {
    assert.ok(condition, currentCase + ' / ' + name + (detail !== undefined ? ': ' + JSON.stringify(detail) : ''));
    checks.push(name);
    newChecks.push(name);
  };
  const api = (pathname, method = 'GET', headers = {}) => page.evaluate(async ({ pathname, method, headers }) => {
    const response = await fetch(pathname, { method, cache: 'no-store',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-FNM-Request': '1', ...headers } : headers,
      ...(method === 'POST' ? { body: '{}' } : {}) });
    const text = await response.text();
    let body = null;
    try { body = JSON.parse(text); } catch (_) { body = { raw: text.slice(0, 200) }; }
    return { status: response.status, body };
  }, { pathname, method, headers });
  const stats = async () => (await operation('stats')).state;
  const archive = body => body ? body.capacity_observation_archive : undefined;
  const numbers = value => value && ({ total: value.total, used: value.used, available: value.available,
    percent: value.percent, sampled_at: value.sampled_at, sampled_at_latest: value.sampled_at_latest,
    observation_id: value.observation_id });
  const currentUnknown = value => value.disk_size === null && value.disk_used === null && value.disk_total === null
    && value.disks_detail.every(row => row.total === null && row.used === null && row.percent === null);
  const measuredRows = value => value.disks_detail.filter(row => row.capacity_last_observation !== null);
  const localDates = async value => page.evaluate(value => [...new Set([
    new Date(value.sampled_at * 1000).toLocaleString(),
    new Date(value.sampled_at_latest * 1000).toLocaleString(),
  ])], value);
  // Stable UI markers published by the panel author:
  //   [data-r1-archive="overview"|"storage"] > .r1-cap-archive
  //   .r1-arc-title/.r1-arc-value/.r1-arc-date/.r1-arc-coverage/.r1-arc-note
  const readCard = selector => page.evaluate(sel => {
    const node = document.querySelector(sel);
    if (!node) return null;
    const part = cls => node.querySelector(cls)?.textContent.replace(/\s+/g, ' ').trim() || null;
    const fonts = {};
    for (const cls of ['r1-arc-title', 'r1-arc-value', 'r1-arc-date', 'r1-arc-coverage', 'r1-arc-note']) {
      const leaf = node.querySelector('.' + cls);
      fonts[cls] = leaf ? Number.parseFloat(getComputedStyle(leaf).fontSize) : null;
    }
    return {
      className: node.className,
      text: node.textContent.replace(/\s+/g, ' ').trim(),
      attribute: node.getAttribute('data-r1-archive'),
      title: part('.r1-arc-title'),
      value: part('.r1-arc-value'),
      date: part('.r1-arc-date'),
      coverage: part('.r1-arc-coverage'),
      note: part('.r1-arc-note'),
      fonts,
      insideBay: !!node.closest('.bay'),
      overflow: node.scrollWidth > node.clientWidth + 1,
    };
  }, selector);
  const readOverviewCard = () => readCard('#layout-overview [data-r1-archive="overview"]');
  const readStorageCard = () => readCard('#layout-storage [data-r1-archive="storage"]');
  const readPrimaryCard = kind => page.evaluate(([selector, kindName]) => {
    const card = document.querySelector(selector);
    if (!card) return null;
    const node = card.querySelector(':scope > .r1-primary-measured');
    const part = cls => node?.querySelector(cls)?.textContent.replace(/\s+/g, ' ').trim() || null;
    const fonts = {};
    for (const cls of ['.r1-primary-title', '.r1-primary-value', '.r1-primary-value span',
      '.r1-primary-date', '.r1-primary-coverage', '.r1-primary-note']) {
      const leaf = node?.querySelector(cls);
      fonts[cls] = leaf ? Number.parseFloat(getComputedStyle(leaf).fontSize) : null;
    }
    let sig = null;
    try { sig = JSON.parse(card.getAttribute('data-r1-primary-sig') || 'null'); } catch (e) { sig = null; }
    return {
      kind: card.getAttribute('data-r1-primary-measured'),
      expectedKind: kindName,
      title: part('.r1-primary-title'),
      value: part('.r1-primary-value'),
      date: part('.r1-primary-date'),
      coverage: part('.r1-primary-coverage'),
      note: part('.r1-primary-note'),
      freeText: part('.r1-primary-current-free-value'),
      freeLabel: part('.r1-primary-current-free'),
      freeVisible: (() => { const n = node?.querySelector('.r1-primary-current-free');
        if (!n) return false; const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !n.closest('[hidden]'); })(),
      text: (node?.textContent || '').replace(/\s+/g, ' ').trim(),
      sig,
      hiddenLegacy: card.querySelectorAll('[data-r1-primary-hidden="1"]').length,
      fonts,
      insideBay: !!card.closest('.bay'),
      overflow: card.scrollWidth > card.clientWidth + 1,
    };
  }, [kind === 'overview' ? '#layout-overview [data-r1-primary-measured="overview"]' : '#layout-storage [data-r1-primary-measured="storage"]', kind]);
  // The panel re-renders modules independently of the 1 s archive watch; under the
  // faked clock a fresh render can drop the promoted node for one tick. Settle the
  // real public marker before asserting, without ever skipping an assertion.
  const settlePrimary = async kind => {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const card = await readPrimaryCard(kind);
      if (card && card.kind === kind && card.sig) return card;
      await page.clock.fastForward(1100);
      await page.waitForTimeout(60);
    }
    return readPrimaryCard(kind);
  };
  const settlePrimaryExit = async () => {
    let scan = await readStorageScan();
    for (let attempt = 0; attempt < 12 && (scan.primaryStorage || !scan.archiveStorage); attempt += 1) {
      await page.clock.fastForward(1100);
      await page.waitForTimeout(80);
      scan = await readStorageScan();
    }
    return scan;
  };
  // Bounded public-DOM geometry for the mobile home assertion: a CI-only overflow
  // must name its offender instead of only reporting a boolean.
  const mobileGeometry = () => page.evaluate(() => {
    const inner = window.innerWidth;
    const describe = el => {
      const rect = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(), id: el.id || null,
        cls: typeof el.className === 'string' ? el.className.slice(0, 120) : null,
        rect: { left: +rect.left.toFixed(1), right: +rect.right.toFixed(1), width: +rect.width.toFixed(1), height: +rect.height.toFixed(1) },
        client: { w: el.clientWidth, h: el.clientHeight }, scroll: { w: el.scrollWidth, h: el.scrollHeight },
        style: { display: s.display, position: s.position, minWidth: s.minWidth, whiteSpace: s.whiteSpace,
          grid: s.gridTemplateColumns, overflowX: s.overflowX },
      };
    };
    const offenders = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.right > inner + 0.5 || r.left < -0.5) {
        offenders.push(describe(el));
        if (offenders.length >= 24) break;
      }
    }
    const card = document.querySelector('#layout-overview [data-r1-primary-measured="overview"]');
    const chain = [];
    for (let n = card; n && n !== document.body && chain.length < 8; n = n.parentElement) chain.push(describe(n));
    return { innerWidth: inner, clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
      devicePixelRatio: window.devicePixelRatio, fonts: document.fonts ? document.fonts.status : null,
      overflow: document.documentElement.scrollWidth > inner + 1, offenders, cardChain: chain };
  });
  // Bounded readiness: fonts + two consecutive stable frames, never an open sleep.
  const settleMobileLayout = () => settleBounded(page, { deadlineMs: 6000, pollMs: 120, stableFrames: 2 });
  const settleArchiveGone = async () => {
    let scan = await readStorageScan();
    for (let attempt = 0; attempt < 12 && (scan.primary > 0 || scan.cards > 0); attempt += 1) {
      await page.clock.fastForward(1100);
      await page.waitForTimeout(80);
      scan = await readStorageScan();
    }
    return scan;
  };
  const readStorageScan = () => page.evaluate(() => ({
    pool: document.querySelector('#layout-storage [data-ref="poolLeg"]')?.textContent || '',
    bays: [...document.querySelectorAll('#layout-storage .bay')].map(bay => ({
      name: bay.querySelector('.bay-name')?.textContent || '',
      text: bay.querySelector('.bay-cap')?.textContent || '',
      history: bay.classList.contains('r1-cap-hist'),
      archive: bay.hasAttribute('data-r1-archive') || !!bay.querySelector('[data-r1-archive]'),
    })),
    docOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    cards: document.querySelectorAll('[data-r1-archive]').length,
    primary: document.querySelectorAll('[data-r1-primary-measured]').length,
    primaryStorage: !!document.querySelector('#layout-storage [data-r1-primary-measured="storage"]'),
    archiveStorage: !!document.querySelector('#layout-storage [data-r1-archive="storage"]'),
    theme: document.documentElement.getAttribute('data-theme'),
  }));

  try {
    const fixture = await ready;
    const nodeGet = async (pathname, cookie) => {
    const response = await fetch(fixture.owner + pathname, { cache: 'no-store',
      headers: { Cookie: cookie.name + '=' + cookie.value } });
    const text = await response.text();
    let body = null;
    try { body = JSON.parse(text); } catch (_) { body = { raw: text.slice(0, 200) }; }
    return { status: response.status, body };
  };

    assert.ok(chromeBin, 'Set CHROME_BIN to an installed Chrome/Chromium executable');
    const { chromium } = require('playwright-core');
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 },
      locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    await context.route('**/*', route => {
      const request = route.request(), raw = request.url();
      if (raw.startsWith('data:')) return route.continue();
      const url = new URL(raw);
      if (url.origin !== fixture.owner) { external.push(raw); return route.abort('blockedbyclient'); }
      const allowed = request.method() === 'GET'
        || request.method() === 'POST' && ['/api/ui', '/api/r1/storage/refresh', '/api/r1/storage/fill'].includes(url.pathname);
      if (!allowed) { blocked.push({ method: request.method(), pathname: url.pathname }); return route.abort('blockedbyclient'); }
      return route.continue();
    });
    page = await context.newPage();
    await page.clock.install();
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin === fixture.owner && url.pathname.startsWith('/api/'))
        requests.push({ case: currentCase, phase, method: request.method(), pathname: url.pathname });
    });

    const waitPublication = async () => {
      const value = await operation('published');
      check('real response-write archive/history publisher completed', value.published === true, value.state.calls);
      return value.state;
    };
    const enter = async (scenario, label = scenario) => {
      currentCase = label; phase = 'entry';
      scenarios.add(label);
      await page.goto('about:blank');
      await operation('case:' + scenario);
      const refresh = page.waitForResponse(response => response.url() === fixture.owner + '/api/r1/storage/refresh'
        && response.request().method() === 'POST');
      await page.goto(fixture.owner + '/#/storage', { waitUntil: 'networkidle' });
      const response = await refresh;
      const body = await response.json();
      check('ordinary page entry performs one non-forced owner refresh',
        response.status() === 200 && body.force === false, body);
      await waitPublication();
      await page.waitForSelector('#layout-storage .bay');
      // Fake clock: the archive card is synced by a 1s Poll job, so tick it once.
      await page.clock.fastForward(1100);
      await page.waitForSelector('#layout-storage [data-r1-primary-measured="storage"], #layout-storage [data-r1-archive="storage"]', { timeout: 8000 });
      return body;
    };
    const nextFrame = async command => {
      if (command) await operation(command);
      await operation('frame');
      const next = page.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
      await page.clock.fastForward(15100); await next;
      await page.waitForTimeout(80);
      return (await api('/api/overview')).body;
    };
    const explicitRefresh = async headers => {
      await operation('reset_publish');
      const result = await api('/api/r1/storage/refresh', 'POST', headers);
      if (result.status === 200) await waitPublication();
      return result;
    };
    const openOverview = async () => {
      await page.goto(fixture.owner + '/#/', { waitUntil: 'networkidle' });
      await page.clock.fastForward(1100);
      await page.waitForSelector('#layout-overview [data-r1-primary-measured="overview"], #layout-overview [data-r1-archive="overview"]', { timeout: 8000 });
    };
    const openStorage = async () => {
      await page.goto(fixture.owner + '/#/storage', { waitUntil: 'networkidle' });
      await page.waitForSelector('#layout-storage .bay');
      await page.clock.fastForward(1100);
      await page.waitForSelector('#layout-storage [data-r1-archive="storage"]', { timeout: 8000 });
    };

    // ---------------- S1: normal success archive ----------------
    await enter('normal', 'S1_normal');
    const firstBody = (await api('/api/overview')).body;
    const archiveA = archive(firstBody);
    check('archive is disclosed only as a fixed contract object',
      validateArchive(archiveA) === null, { archive: archiveA, error: validateArchive(archiveA) });
    const rowsA = measuredRows(firstBody);
    check('archive covers exactly the measured subset of the successful batch',
      archiveA.coverage.filesystem_count === 3 && archiveA.coverage.disk_count === 3
      && rowsA.length === 3
      && archiveA.total === rowsA.reduce((sum, row) => sum + row.capacity_last_observation.total, 0)
      && archiveA.used === rowsA.reduce((sum, row) => sum + row.capacity_last_observation.used, 0), { archiveA, rowsA });
    check('archive carries the published batch identity and the original sample window',
      archiveA.observation_id === firstBody.capacity_last_observation_summary.observation_id
      && archiveA.sampled_at === Math.min(...rowsA.map(row => row.capacity_last_observation.sampled_at))
      && archiveA.sampled_at_latest === Math.max(...rowsA.map(row => row.capacity_last_observation.sampled_at_latest)),
      { archiveA, summary: firstBody.capacity_last_observation_summary });
    check('no per-disk archive field and no bay-level archive binding',
      firstBody.disks_detail.every(row => !('capacity_observation_archive' in row))
      && Object.keys(firstBody).filter(key => key.includes('archive')).join() === 'capacity_observation_archive', firstBody);

    // Real page tabs switch through the stock hash router; entering the storage
    // view triggers its ordinary owner refresh, so every DOM assertion is bound
    // to the archive batch that the same read returned (never to a guessed one).
    const switchTo = async hash => {
      await page.evaluate(value => { location.hash = value; }, hash);
      await page.clock.fastForward(1100);
      await page.waitForTimeout(120);
    };
    const settleCard = async (readCard, label) => {
      for (let attempt = 0; attempt < 6; attempt += 1) {
        const body = (await api('/api/overview')).body;
        const current = archive(body);
        const card = await readCard();
        if (card && current) {
          const dates = await localDates(current);
          if (dates.every(text => card.date.includes(text))) return { body, current, card };
        }
        if (attempt === 5) return { body, current, card };
        await page.clock.fastForward(1100);
        await page.waitForTimeout(100);
      }
      return { body: null, current: null, card: null };
    };

    await switchTo('#/storage');
    const storagePair = await settleCard(readStorageCard, 'storage');
    const storageCardA = storagePair.card;
    check('storage archive card is bound to the batch the API currently returns',
      storagePair.current && storageCardA
      && validateArchive(storagePair.current) === null
      && storageCardA.attribute === 'storage' && /\br1-cap-archive\b/.test(storageCardA.className)
      && !storageCardA.insideBay && storageCardA.title === '过去测量档案'
      && storageCardA.text.includes('当前归属未确认')
      && storageCardA.date.includes('原日期区间')
      && storageCardA.coverage === '已测 3 个文件系统 / 3 块盘（子集）'
      && storageCardA.note.includes('重启清空') && storageCardA.note.includes('仅过去观测')
      && storageCardA.note.includes('不代表当前容量或可用空间'), storageCardA);
    await switchTo('#/');
    const overviewPair = await settleCard(readOverviewCard, 'overview');
    const overviewCardA = overviewPair.card;
    check('overview archive card mirrors the same contract on the home page',
      overviewPair.current && overviewCardA
      && overviewCardA.attribute === 'overview' && /\br1-cap-archive\b/.test(overviewCardA.className)
      && !overviewCardA.insideBay && overviewCardA.title === '过去测量档案'
      && overviewCardA.text.includes('当前归属未确认')
      && overviewCardA.coverage === '已测 3 个文件系统 / 3 块盘（子集）'
      && overviewCardA.note.includes('重启清空'), overviewCardA);
    check('both cards agree on the archived numbers while current capacity stays untouched',
      storagePair.current && overviewPair.current
      && ['total', 'used', 'available', 'percent'].every(key => storagePair.current[key] === overviewPair.current[key])
      && ['total', 'used', 'available'].every(key => storagePair.current[key] === archiveA[key])
      && (overviewPair.body.disk_size > 0 || storagePair.body.disk_size > 0), { storage: storagePair.current, overview: overviewPair.current });
    const knownPool = await readStorageScan();
    check('archive never replaces known current values or enters a bay',
      /\d/.test(knownPool.pool) && knownPool.bays.length === 3
      && knownPool.bays.every(bay => !bay.archive && !bay.text.includes('过去测量档案')), knownPool);

    // The production display validator is the real served r1CapacityArchive,
    // reached through the TEMP read-only getter; Node never re-implements it.
    const inPage = await page.evaluate(archive => {
      const probe = window.__r1ArchiveProbe;
      if (!probe || typeof probe.validate !== 'function') return { present: false };
      const variants = {
        extra_key: { ...archive, unexpected: 1 },
        bad_source: { ...archive, source: 'last_successful_observation' },
        bad_reason: { ...archive, reason: 'mount_continuity_unverified' },
        bad_percent: { ...archive, percent: archive.percent + 1 },
        bad_available: { ...archive, available: archive.available + 1 },
        bad_id_zero: { ...archive, observation_id: '0' },
        bad_retention: { ...archive, retention: 'persistent' },
        bad_coverage: { ...archive, coverage: { ...archive.coverage, complete: true } },
      };
      const rejected = {};
      for (const [key, value] of Object.entries(variants)) rejected[key] = probe.validate(value) === null;
      const accepted = probe.validate(archive);
      return { present: true, accepted, rejected, acceptedKeys: accepted ? Object.keys(accepted).sort() : [] };
    }, archiveA);
    getterEvidence = inPage.present ? {
      source: 'TEMP read-only __r1ArchiveProbe -> served r1CapacityArchive',
      accepted_keys: inPage.acceptedKeys, rejected: inPage.rejected,
    } : { present: false };
    checkNew('served production display validator accepts the real owner archive object',
      inPage.present === true && !!inPage.accepted
      && inPage.acceptedKeys.join() === 'available,disks,filesystems,observation_id,percent,sampled_at,sampled_at_latest,total,used'
      && inPage.accepted.observation_id === archiveA.observation_id
      && inPage.accepted.total === archiveA.total && inPage.accepted.used === archiveA.used
      && inPage.accepted.filesystems === archiveA.coverage.filesystem_count
      && inPage.accepted.disks === archiveA.coverage.disk_count, inPage);
    checkNew('served production display validator rejects all eight malformed variants',
      inPage.present === true
      && ['extra_key', 'bad_source', 'bad_reason', 'bad_percent', 'bad_available', 'bad_id_zero',
        'bad_retention', 'bad_coverage'].every(key => inPage.rejected[key] === true), inPage.rejected);
    // Wire-level malformed payloads stay delegated to backend HTTP tests (no
    // route.fulfill here); this gate covers the served display validator plus
    // the null-field DOM cleanup below.

    // ---------------- S2: unrelated mountinfo change ----------------
    currentCase = 'S2_mount_noise';
    scenarios.add('S2_mount_noise');
    phase = 'mount_noise';
    await switchTo('#/storage');
    const beforeNoise = (await api('/api/overview')).body;
    const noisy = await nextFrame('mount_noise');
    check('unrelated mountinfo change invalidates current capacity', currentUnknown(noisy), noisy);
    check('mount invalidation keeps the existing per-disk observation, unchanged and unrenewed',
      noisy.disks_detail.every(row => {
        const before = beforeNoise.disks_detail.find(item => item.name === row.name).capacity_last_observation;
        const now = row.capacity_last_observation;
        return now && now.observation_id === before.observation_id
          && JSON.stringify([now.sampled_at, now.sampled_at_latest]) === JSON.stringify([before.sampled_at, before.sampled_at_latest]);
      }), noisy.disks_detail);
    check('archive survives the invalidation with the original numbers, dates and identity',
      validateArchive(archive(noisy)) === null
      && JSON.stringify(numbers(archive(noisy))) === JSON.stringify(numbers(archive(beforeNoise))), archive(noisy));
    const noisePrimary = await settlePrimary('storage');
    const noiseScan = await readStorageScan();
    const noiseDates = await localDates(archive(noisy));
    check('invalidated storage promotes the archived snapshot into the primary card',
      noisePrimary && noisePrimary.kind === 'storage' && noisePrimary.title === '最近实测'
      && noiseDates.every(text => noisePrimary.date.includes(text))
      && noisePrimary.freeVisible === true && noisePrimary.freeText === '--'
      && !noiseScan.archiveStorage && noiseScan.primaryStorage === true
      && noiseScan.bays.every(bay => !bay.archive), { primary: noisePrimary, scan: noiseScan });
    check('per-disk history remains a separate disclosure and never becomes a bay archive',
      noiseScan.bays.filter(bay => ['sda', 'sdb', 'sdc'].includes(bay.name)).every(bay => bay.history)
      && noiseScan.bays.every(bay => !bay.text.includes('过去测量档案')), noiseScan.bays);
    phase = 'responsive';
    for (const width of [320, 376, 1400]) for (const theme of ['light', 'dark']) {
      await page.setViewportSize({ width, height: 1000 });
      if (await page.locator('html').getAttribute('data-theme') !== theme) await page.locator('#btnTheme').click();
      await page.locator('#layout-storage [data-r1-primary-measured="storage"]').scrollIntoViewIfNeeded();
      const card = await readPrimaryCard('storage');
      const scan = await readStorageScan();
      await page.screenshot({ path: path.join(fixtureDir, 'archive-' + width + '-' + theme + '.png'), fullPage: true });
      check('archive card is readable at ' + width + 'px/' + theme + ' without horizontal overflow',
        card && scan.theme === theme && !scan.docOverflow && !card.overflow
        && ['.r1-primary-title', '.r1-primary-value', '.r1-primary-date', '.r1-primary-coverage', '.r1-primary-note']
          .every(cls => card.fonts[cls] !== null && card.fonts[cls] >= 12)
        && (await localDates(archive(noisy))).every(text => card.date.includes(text)),
        { width, theme, card, docOverflow: scan.docOverflow, themeNow: scan.theme });
    }
    await page.setViewportSize({ width: 1400, height: 1000 });

    // ---------------- S3: mount alias change ----------------
    currentCase = 'S3_alias';
    scenarios.add('S3_alias');
    phase = 'alias';
    const aliased = await nextFrame('alias_b');
    check('mount alias change drops only that disk binding and leaves the archive unbound',
      currentUnknown(aliased)
      && aliased.disks_detail.find(row => row.name === 'sdb').capacity_last_observation === null
      && aliased.disks_detail.filter(row => row.name !== 'sdb')
        .every(row => row.capacity_last_observation !== null)
      && JSON.stringify(numbers(archive(aliased))) === JSON.stringify(numbers(archive(noisy))), aliased);

    // ---------------- S4: disk replacement / lifetime change ----------------
    currentCase = 'S4_replace';
    scenarios.add('S4_replace');
    phase = 'replace';
    const replaced = await nextFrame('replace_a');
    check('disk replacement drops that device lifetime only and never rebinds the archive',
      replaced.disks_detail.find(row => row.name === 'sda').capacity_last_observation === null
      && replaced.disks_detail.find(row => row.name === 'sdc').capacity_last_observation !== null
      && JSON.stringify(numbers(archive(replaced))) === JSON.stringify(numbers(archive(noisy))), replaced);

    // ---------------- S5: genuinely incomplete inventory ----------------
    currentCase = 'S5_incomplete_inventory';
    scenarios.add('S5_incomplete_inventory');
    phase = 'incomplete';
    const incomplete = await nextFrame('inventory_incomplete');
    check('incomplete kernel inventory clears the bound per-disk history but not the independent archive',
      currentUnknown(incomplete) && incomplete.capacity_last_observation_summary === null
      && incomplete.disks_detail.every(row => row.capacity_last_observation === null)
      && JSON.stringify(numbers(archive(incomplete))) === JSON.stringify(numbers(archive(noisy))), incomplete);

    // ---------------- S6: full recovery ----------------
    currentCase = 'S6_recovery';
    scenarios.add('S6_recovery');
    phase = 'recovery';
    const recovered = await nextFrame('restore_mounts');
    check('full mount recovery never revives per-disk history and keeps the archive unbound',
      recovered.disks_detail.every(row => row.capacity_last_observation === null)
      && JSON.stringify(numbers(archive(recovered))) === JSON.stringify(numbers(archive(noisy))), recovered);
    const recoveredStorage = await readStorageScan();
    check('recovered page shows no revived per-disk history or bay archive markers',
      recoveredStorage.bays.every(bay => !bay.history && !bay.archive), recoveredStorage.bays);

    // ---------------- S7: subset shapes + two batches ----------------
    const partial = await enter('partial', 'S7_partial');
    const archivePartial = archive((await api('/api/overview')).body);
    const partialRows = measuredRows((await api('/api/overview')).body);
    check('partial batch archives only the measured filesystems (no standby/failed sibling)',
      validateArchive(archivePartial) === null && archivePartial.coverage.filesystem_count === 2
      && archivePartial.coverage.disk_count === 2
      && partial.capacity.results.filter(row => row.status === 'refreshed').length === 2, { archivePartial, partial });
    check('partial archive numbers equal the two measured rows only',
      archivePartial.total === partialRows.reduce((sum, row) => sum + row.capacity_last_observation.total, 0)
      && archivePartial.used === partialRows.reduce((sum, row) => sum + row.capacity_last_observation.used, 0)
      && partialRows.map(row => row.name).sort().join() === 'sda,sdc', { archivePartial, partialRows });
    const partialCard = await readStorageCard();
    check('partial batch card declares the measured 2/2 subset instead of the whole pool',
      partialCard && /^已测\s*2\s*个文件系统\s*\/\s*2\s*块盘（子集）$/.test(partialCard.coverage),
      partialCard);

    currentCase = 'S7_failed_no_renew';
    scenarios.add('S7_failed_no_renew');
    phase = 'failed';
    await operation('fail_all');
    const failed = await explicitRefresh();
    const afterFailed = (await api('/api/overview')).body;
    check('a batch where every probe fails never renews or replaces the archive',
      failed.status === 200 && failed.body.capacity.results.every(row => row.status !== 'refreshed')
      && JSON.stringify(numbers(archive(afterFailed))) === JSON.stringify(numbers(archivePartial)),
      { failed: failed.body.capacity, archive: archive(afterFailed) });
    phase = 'cached_only';
    await operation('standby_all');
    const cachedOnly = await explicitRefresh();
    const afterCached = (await api('/api/overview')).body;
    check('cached-only/standby batch never renews the archive dates',
      cachedOnly.status === 200
      && JSON.stringify(numbers(archive(afterCached))) === JSON.stringify(numbers(archivePartial))
      && archive(afterCached).sampled_at === archivePartial.sampled_at, { cachedOnly: cachedOnly.body.capacity, afterCached });

    const multipart = await enter('multipart', 'S7_multipart');
    const multipartBody = (await api('/api/overview')).body;
    const archiveMultipart = archive(multipartBody);
    const multipartRows = measuredRows(multipartBody);
    check('two partitions of one physical disk count as two filesystems but one disk',
      archiveMultipart.coverage.filesystem_count === 4 && archiveMultipart.coverage.disk_count === 3
      && multipart.capacity.results.filter(row => row.status === 'refreshed').length === 4, archiveMultipart);
    check('multipart archive total aggregates every measured filesystem of the batch',
      archiveMultipart.used === multipartRows.reduce((sum, row) => sum + row.capacity_last_observation.used, 0)
      && archiveMultipart.total === multipartRows.reduce((sum, row) => sum + row.capacity_last_observation.total, 0),
      { archiveMultipart, multipartRows });

    await enter('zero', 'S7_zero');
    const archiveZero = archive((await api('/api/overview')).body);
    check('genuine measured zero usage stays zero in the archive', archiveZero.used === 0
      && archiveZero.percent === 0 && archiveZero.available === archiveZero.total, archiveZero);
    const zeroCard = await readStorageCard();
    check('zero-usage archive card renders 0% and full subset coverage', zeroCard
      && zeroCard.value.includes('0%')
      && /^已测\s*3\s*个文件系统\s*\/\s*3\s*块盘（子集）$/.test(zeroCard.coverage), zeroCard);

    await enter('duplicate', 'S7_duplicate');
    const archiveDuplicate = archive((await api('/api/overview')).body);
    check('duplicate filesystem aliases are deduplicated in the archive coverage',
      archiveDuplicate.coverage.filesystem_count === 2 && archiveDuplicate.coverage.disk_count === 2
      && (await api('/api/overview')).body.disks_detail.find(row => row.name === 'sda').capacity_last_observation === null,
      archiveDuplicate);

    await enter('normal', 'S7_two_batches');
    const batchOne = archive((await api('/api/overview')).body);
    await operation('batch_two');
    const second = await explicitRefresh();
    const afterSecond = (await api('/api/overview')).body;
    const batchTwo = archive(afterSecond);
    const freshRows = afterSecond.disks_detail.filter(row => row.capacity_last_observation).filter(row => row.name !== 'sda');
    check('the second real batch replaces the archive without mixing the older standby sibling',
      second.status === 200 && validateArchive(batchTwo) === null
      && batchTwo.observation_id !== batchOne.observation_id
      && batchTwo.coverage.disk_count === 2 && batchTwo.coverage.filesystem_count === 2
      && batchTwo.total === freshRows.reduce((sum, row) => sum + row.capacity_last_observation.total, 0)
      && batchTwo.sampled_at >= batchOne.sampled_at, { batchOne, batchTwo, freshRows });

    // ---------------- S8: owner boundaries + revocation ----------------
    await enter('normal', 'S8_owner');
    const granted = (await api('/api/overview')).body;
    const grantedArchive = archive(granted);
    const bearer = await api('/api/overview', 'GET', { Authorization: 'Bearer fixture-unprivileged-key' });
    check('an invalid Bearer-shaped key is not treated as owner and receives no archive',
      !Object.prototype.hasOwnProperty.call(bearer.body, 'capacity_observation_archive')
      && (!Array.isArray(bearer.body.disks_detail)
        || bearer.body.disks_detail.every(row => !('capacity_last_observation' in row))), bearer);
    const pair = (await operation('auth:pair')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...pair, url: fixture.owner }]);
    const paired = await api('/api/overview');
    check('real pair owner reads the same archive batch as the password owner',
      paired.status === 200 && archive(paired.body) && archive(paired.body).observation_id === grantedArchive.observation_id, paired.body);
    const password = (await operation('auth:password')).cookie;
    await context.clearCookies();
    const anonymous = await api('/api/overview');
    check('anonymous password mode receives no archive field', anonymous.status === 401
      && !Object.prototype.hasOwnProperty.call(anonymous.body, 'capacity_observation_archive'), anonymous);
    await context.addCookies([{ ...password, url: fixture.owner }]);
    const ownerAgain = await api('/api/overview');
    check('real password owner retains the archive batch', ownerAgain.status === 200
      && archive(ownerAgain.body).observation_id === grantedArchive.observation_id, ownerAgain.body);
    await operation('hold');
    const pending = api('/api/r1/storage/refresh', 'POST');
    check('pending batch reached the controlled probe boundary', (await operation('entered')).entered === true);
    await operation('revoke');
    await operation('release');
    const revoked = await pending;
    // The revocation invalidates the current session, so a fresh real owner
    // login is required to read what survived; the revoked POST itself is 403.
    const revokedRelogin = (await operation('auth:password')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...revokedRelogin, url: fixture.owner }]);
    const afterRevoke = (await api('/api/overview')).body;
    check('owner revocation rejects the pending batch and never publishes a new archive',
      revoked.status === 403 && validateArchive(archive(afterRevoke)) === null
      && JSON.stringify(numbers(archive(afterRevoke))) === JSON.stringify(numbers(grantedArchive)), { revoked, afterRevoke });
    // DOM must not keep a stale archive once owner access is gone. A hash-only
    // URL change is a same-document navigation, so reload the document for real.
    await context.clearCookies();
    await page.goto(fixture.owner + '/', { waitUntil: 'networkidle' });
    await page.waitForTimeout(200);
    const unauthenticatedDom = await page.evaluate(() => ({
      cards: document.querySelectorAll('[data-r1-archive]').length,
      login: !!document.querySelector('#login-form, input[type="password"]'),
      hash: location.hash,
    }));
    check('revoked/unauthenticated page exposes no archive DOM',
      unauthenticatedDom.cards === 0 && unauthenticatedDom.login, unauthenticatedDom);

    // ---------------- S10: primary card promoted to last measured ----------------
    // Values recomputed from the API archive in the test process (never the page).
    const primaryDsize = b => {
      if (b >= 1e12) { const v = b / 1e12; return (v >= 100 ? String(Math.round(v)) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + ' TB'; }
      if (b >= 1e9) { const v = b / 1e9; return (v >= 100 ? String(Math.round(v)) : v.toFixed(1)) + ' GB'; }
      return String(Math.round(b / 1e6)) + ' MB';
    };
    const primaryTextExpectations = (a, dates) => ({
      percent: String(Math.round(a.percent)) + '%',
      bytes: primaryDsize(a.used) + ' / ' + primaryDsize(a.total),
      availableBytes: primaryDsize(a.available),
      coverage: '已测 ' + a.coverage.filesystem_count + ' 个文件系统 / ' + a.coverage.disk_count + ' 块盘（子集）',
      dates,
    });
    // Unified contract: promotion only when current is genuinely invalid and a
    // valid owner archive exists. Display sig reuses the canonical nine keys
    // (available included for contract checking only, never shown as free space).
    currentCase = 'S10_primary_measured';
    scenarios.add('S10_primary_measured');
    phase = 'primary_current';
    // S8 leaves the browser unauthenticated on purpose; restore a real owner
    // session (same fixture auth path, no cookie copying) before the new page.
    const primaryOwner = (await operation('auth:password')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...primaryOwner, url: fixture.owner }]);
    await enter('normal', 'S10_primary_measured');
    await switchTo('#/storage');
    const validScan = await readStorageScan();
    check('P0 valid current keeps the native primary without any measured marker',
      !validScan.primaryStorage && validScan.archiveStorage
      && /\d/.test(validScan.pool), validScan);

    phase = 'primary_promote';
    const beforePromote = (await api('/api/overview')).body;
    const promotedBody = await nextFrame('mount_noise');
    const promotedArchive = archive(promotedBody);
    const promotedExpect = primaryTextExpectations(promotedArchive, await localDates(promotedArchive));
    check('P1 invalidated current still keeps a legal archive for promotion',
      currentUnknown(promotedBody) && validateArchive(promotedArchive) === null
      && JSON.stringify(archive(beforePromote)) === JSON.stringify(promotedArchive), promotedBody);
    check('P2 promotion never writes measured numbers back into the native API/current',
      promotedBody.disk_size === null && promotedBody.disk_used === null && promotedBody.disk_total === null
      && promotedBody.disks_detail.every(row => row.total === null && row.used === null && row.percent === null)
      && promotedBody.disks_detail.every(row => !('capacity_observation_archive' in row)), promotedBody);
    await page.waitForSelector('#layout-storage [data-r1-primary-measured="storage"]', { timeout: 8000 });
    await page.clock.fastForward(1100);
    const promotedStorage = await settlePrimary('storage');
    const promotedScan = await readStorageScan();
    check('P3 storage primary shows title, archived percent/bytes, original date range and subset coverage',
      promotedStorage && promotedStorage.kind === 'storage' && promotedStorage.title === '最近实测'
      && promotedStorage.value.includes(promotedExpect.percent)
      && promotedStorage.value.includes(promotedExpect.bytes)
      && promotedStorage.date.includes(promotedExpect.dates[0])
      && (promotedExpect.dates.length < 2 || promotedStorage.date.includes(promotedExpect.dates[1]))
      && /^已测\s*3\s*个文件系统\s*\/\s*3\s*块盘（子集）· 当前归属未确认$/.test(promotedStorage.coverage)
      && promotedStorage.note.includes('重启清空') && promotedStorage.note.includes('不代表当前可用空间'),
      { promotedStorage, promotedExpect });
    check('P4 storage promotion shows a visible current-free node whose value is exactly --',
      promotedStorage.freeVisible === true && promotedStorage.freeText === '--'
      && /当前可用空间/.test(promotedStorage.freeLabel || '')
      && !promotedStorage.text.includes(promotedExpect.availableBytes)
      && !String(promotedStorage.value).includes(promotedExpect.availableBytes),
      { freeText: promotedStorage.freeText, freeVisible: promotedStorage.freeVisible,
        archiveAvailableText: promotedExpect.availableBytes });
    const pageCards = await page.evaluate(() => ({
      overviewPrimary: document.querySelectorAll('#layout-overview [data-r1-primary-measured="overview"]').length,
      storagePrimary: document.querySelectorAll('#layout-storage [data-r1-primary-measured="storage"]').length,
      overviewArchive: document.querySelectorAll('#layout-overview [data-r1-archive]').length,
      storageArchive: document.querySelectorAll('#layout-storage [data-r1-archive]').length,
    }));
    check('P5 promotion dedupes the standalone archive card on both pages (one marker each)',
      pageCards.overviewPrimary === 1 && pageCards.storagePrimary === 1
      && pageCards.overviewArchive === 0 && pageCards.storageArchive === 0
      && promotedScan.archiveStorage === false && promotedScan.cards === 0,
      { pageCards, promotedScan });
    check('P6 per-bay cells keep their own disclosure and never host archive numbers',
      promotedScan.bays.length === 3
      && promotedScan.bays.every(bay => !bay.archive && !bay.text.includes(promotedExpect.percent))
      && !promotedStorage.insideBay, { bays: promotedScan.bays });
    check('P7 promoted primary carries the canonical nine-key display signature',
      promotedStorage.sig
      && Object.keys(promotedStorage.sig).sort().join() === 'available,disks,filesystems,observation_id,percent,sampled_at,sampled_at_latest,total,used'
      && promotedStorage.sig.observation_id === promotedArchive.observation_id
      && promotedStorage.sig.total === promotedArchive.total && promotedStorage.sig.used === promotedArchive.used
      && promotedStorage.sig.available === promotedArchive.available
      && Math.abs(promotedStorage.sig.percent - promotedArchive.percent) < 1e-9
      && promotedStorage.sig.filesystems === promotedArchive.coverage.filesystem_count
      && promotedStorage.sig.disks === promotedArchive.coverage.disk_count
      && !('reason' in promotedStorage.sig), { sig: promotedStorage.sig });

    phase = 'primary_home';
    // Do not navigate: entering the overview view triggers its own ordinary fill
    // entry (allowed write), which would restore current and end the promotion.
    // The overview card lives in the same document, so it is asserted in place.
    const promotedHome = await settlePrimary('overview');
    const homeArchiveCount = await page.evaluate(() => document.querySelectorAll('#layout-overview [data-r1-archive="overview"]').length);
    check('P8 home primary mirrors the same archived numbers, dates and dedupe',
      promotedHome && promotedHome.kind === 'overview' && promotedHome.title === '最近实测'
      && promotedHome.value.includes(promotedExpect.percent)
      && promotedHome.value.includes(promotedExpect.bytes)
      && promotedHome.coverage === promotedStorage.coverage
      && promotedHome.freeVisible === false && homeArchiveCount === 0,
      { promotedHome, homeArchiveCount });

    phase = 'primary_responsive';
    for (const width of [320, 376, 1400]) for (const theme of ['light', 'dark']) {
      await page.setViewportSize({ width, height: 1000 });
      if (await page.locator('html').getAttribute('data-theme') !== theme) await page.locator('#btnTheme').click();
      await page.locator('#layout-storage [data-r1-primary-measured="storage"]').scrollIntoViewIfNeeded();
      const card = await settlePrimary('storage');
      const doc = await page.evaluate(() => ({ scroll_width: document.documentElement.scrollWidth, width: window.innerWidth,
        theme: document.documentElement.getAttribute('data-theme') }));
      await page.screenshot({ path: path.join(fixtureDir, 'primary-' + width + '-' + theme + '.png'), fullPage: true });
      check('R' + width + '-' + theme + ' primary measured card is readable and unclipped',
        card && doc.theme === theme && doc.scroll_width <= doc.width + 1 && !card.overflow
        && Object.values(card.fonts).every(font => font !== null && font >= 12)
        && promotedExpect.dates.every(text => card.date.includes(text)),
        { width, theme, card: card && { value: card.value, date: card.date, fonts: card.fonts }, doc });
    }
    await page.setViewportSize({ width: 1400, height: 1000 });

    phase = 'primary_restore';
    await switchTo('#/storage');
    await operation('restore_mounts');
    const restored = await explicitRefresh();
    await nextFrame();
    // Wait for the real public DOM to leave the promoted state (native primary
    // restored, standalone archive card back) instead of sampling one frame.
    const restoredScan = await settlePrimaryExit();
    const restoredPrimary = await readPrimaryCard('storage');
    const restoredNative = await page.evaluate(() => {
      const pool = document.querySelector('#layout-storage .card.pool');
      return { hiddenLegacy: pool ? pool.querySelectorAll('[data-r1-primary-hidden="1"]').length : -1,
        primaryNode: pool ? pool.querySelectorAll(':scope > .r1-primary-measured').length : -1,
        marker: pool ? pool.hasAttribute('data-r1-primary-measured') : null,
        poolV: document.querySelector('#layout-storage [data-ref="poolV"]')?.closest('[hidden]') ? 'hidden' : 'visible' };
    });
    check('P9 restored current returns the native primary, restores hidden refs and re-exposes the archive card',
      restored.status === 200 && !restoredScan.primaryStorage && restoredScan.archiveStorage
      && /\d/.test(restoredScan.pool) && !restoredScan.pool.includes('最近实测')
      && restoredPrimary === null && restoredNative.hiddenLegacy === 0
      && restoredNative.primaryNode === 0 && restoredNative.marker === false,
      { restored: restored.status, restoredScan, restoredNative });

    phase = 'primary_round_trip';
    const roundTrip = await nextFrame('mount_noise');
    await page.waitForSelector('#layout-storage [data-r1-primary-measured="storage"]', { timeout: 8000 });
    await page.clock.fastForward(1100);
    const roundTripCard = await settlePrimary('storage');
    const roundTripArchive = archive(roundTrip);
    check('P10 current->unknown round trip re-promotes exactly the current archive batch',
      validateArchive(roundTripArchive) === null && roundTripCard
      && roundTripCard.value.includes(String(Math.round(roundTripArchive.percent)) + '%')
      && roundTripCard.sig && roundTripCard.sig.observation_id === roundTripArchive.observation_id,
      { roundTripCard: roundTripCard && { value: roundTripCard.value, sig: roundTripCard.sig }, roundTripArchive });

    phase = 'primary_archive_removed';
    await operation('close');
    await page.clock.fastForward(15100);
    const closedScan = await settleArchiveGone();
    const closedPrimary = await readPrimaryCard('storage');
    check('P11 cleared archive removes the promoted numbers and the primary marker',
      closedScan.primary === 0 && closedScan.cards === 0 && closedPrimary === null
      && !closedScan.pool.includes('最近实测') && !/\d+%/.test(closedScan.pool), closedScan);
    await operation('start');

    phase = 'primary_zero_used_current';
    // 0 used is a valid current value: it must never be mistaken for "missing".
    await enter('zero', 'S10_zero_used_current');
    await switchTo('#/storage');
    const zeroBody = (await api('/api/overview')).body;
    const zeroScan = await settlePrimaryExit();
    check('P12 genuine 0%-used current is valid and is never promoted to the measured card',
      Number.isFinite(zeroBody.disk_size) && zeroBody.disk_size > 0
      && zeroBody.disk_used === 0 && zeroBody.disk_used <= zeroBody.disk_size
      && Number.isFinite(zeroBody.disk_total) && zeroBody.disk_total === 0
      && !zeroScan.primaryStorage && zeroScan.archiveStorage
      && zeroScan.bays.length === 3 && zeroScan.bays.every(bay => /0%/.test(bay.text))
      && !zeroScan.pool.includes('最近实测'), { zeroBody, zeroScan });

    // ---------------- S11: visible home promotion (real navigation) ----------------
    // The home card must promote while the overview page is genuinely visible, not
    // merely read from the hidden layout. Navigation uses the stock tab button; the
    // overview entry may run its own allowed fill, which is allowed to finish first.
    currentCase = 'S11_home_visible';
    scenarios.add('S11_home_visible');
    phase = 'home_visible_enter';
    await enter('normal', 'S11_home_visible');
    await switchTo('#/storage');
    await page.locator('#pageSeg button[data-page="overview"]').click({ timeout: 8000 });
    await page.locator('#page-overview:not([hidden])').waitFor({ state: 'visible', timeout: 8000 });
    // Let the overview entry's own fill / native polls finish normally.
    await page.clock.fastForward(15100);
    await page.waitForTimeout(120);
    const afterEntry = (await api('/api/overview')).body;

    phase = 'home_visible_promote';
    const homeNoise = await nextFrame('mount_noise');
    const homeArchive = archive(homeNoise);
    const homeExpect = primaryTextExpectations(homeArchive, await localDates(homeArchive));
    const homePrimary = await settlePrimary('overview');
    const homeVisible = await page.evaluate(() => {
      const card = document.querySelector('#layout-overview [data-r1-primary-measured="overview"]');
      const layout = document.querySelector('#layout-overview');
      const pageEl = document.querySelector('#page-overview');
      const rect = card ? card.getBoundingClientRect() : null;
      const layoutRect = layout ? layout.getBoundingClientRect() : null;
      return {
        hash: location.hash,
        selected: document.querySelector('#pageSeg button[aria-selected="true"]')?.getAttribute('data-page') || null,
        pageHidden: pageEl ? pageEl.hasAttribute('hidden') : null,
        layoutVisible: !!layoutRect && layoutRect.width > 0 && layoutRect.height > 0,
        cardVisible: !!rect && rect.width > 0 && rect.height > 0 && !card.closest('[hidden]'),
        cardRect: rect ? { width: +rect.width.toFixed(1), height: +rect.height.toFixed(1) } : null,
        archiveOverviewCards: document.querySelectorAll('#layout-overview [data-r1-archive]').length,
      };
    });
    const homeRendered = await readPrimaryCard('overview');
    check('P13 genuinely visible home promotes the latest archive with full original values and dedupe',
      homeVisible.selected === 'overview' && homeVisible.pageHidden === false
      && homeVisible.layoutVisible === true && homeVisible.cardVisible === true
      && homeVisible.archiveOverviewCards === 0
      && homePrimary && homePrimary.kind === 'overview' && homePrimary.title === '最近实测'
      && homeRendered.value.includes(homeExpect.percent) && homeRendered.value.includes(homeExpect.bytes)
      && homeRendered.date.includes(homeExpect.dates[0])
      && (homeExpect.dates.length < 2 || homeRendered.date.includes(homeExpect.dates[1]))
      && /^已测\s*3\s*个文件系统\s*\/\s*3\s*块盘（子集）· 当前归属未确认$/.test(homeRendered.coverage)
      && homeRendered.freeVisible === false
      && homeRendered.sig
      && Object.keys(homeRendered.sig).sort().join() === 'available,disks,filesystems,observation_id,percent,sampled_at,sampled_at_latest,total,used'
      && homeRendered.sig.observation_id === homeArchive.observation_id
      && (!archive(afterEntry) || archive(afterEntry).observation_id === homeArchive.observation_id)
      && homeVisible.hash.startsWith('#/'),
      { homeVisible, homeRendered: homeRendered && { value: homeRendered.value, coverage: homeRendered.coverage, sig: homeRendered.sig },
        homeExpect, afterEntryArchive: archive(afterEntry) });
    check('P14 visible home promotion never writes measured numbers into the native API current',
      homeNoise.disk_size === null && homeNoise.disk_used === null && homeNoise.disk_total === null
      && homeNoise.disks_detail.every(row => row.total === null && row.used === null && row.percent === null)
      && homeNoise.disks_detail.every(row => !('capacity_observation_archive' in row)), homeNoise);
    await page.screenshot({ path: path.join(fixtureDir, 'home-visible-1440.png'), fullPage: true });

    phase = 'home_visible_mobile';
    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      const settleAfterResize = await settleMobileLayout();
      // Re-invalidate at this width: the overview view may run its own allowed
      // capacity fill after a resize, so the promoted card can be short-lived.
      await nextFrame('mount_noise');
      const mobileHome = await settlePrimary('overview');
      const settleAfterPromotion = await settleMobileLayout();
      const view = await page.evaluate(() => {
        const card = document.querySelector('#layout-overview [data-r1-primary-measured="overview"]');
        const rect = card ? card.getBoundingClientRect() : null;
        return { cardVisible: !!rect && rect.width > 0 && rect.height > 0,
          markerCount: document.querySelectorAll('#layout-overview [data-r1-primary-measured]').length,
          archiveCount: document.querySelectorAll('#layout-overview [data-r1-archive]').length,
          diskModule: !!document.querySelector('#layout-overview [data-m="disk"]'),
          layoutWidth: document.querySelector('#layout-overview')?.getBoundingClientRect().width || 0,
          docOverflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
      });
      const mobileApi = (await api('/api/overview')).body;
      const mobileArchive = archive(mobileApi);
      const mobileExpect = mobileArchive
        ? primaryTextExpectations(mobileArchive, await localDates(mobileArchive)) : null;
      await page.screenshot({ path: path.join(fixtureDir, 'home-visible-' + width + '.png'), fullPage: true });
      const geometry = await mobileGeometry();
      fs.writeFileSync(path.join(fixtureDir, 'home-mobile-geometry-' + width + '.json'),
        JSON.stringify({ settleAfterResize, settleAfterPromotion, geometry }, null, 2), 'utf8');
      if (!settleAfterResize.ok || !settleAfterPromotion.ok) {
        console.log(JSON.stringify({ bounded_readiness_failed: { width, settleAfterResize,
          settleAfterPromotion, geometry } }));
      }
      check('R-home-' + width + '-settle bounded font/layout readiness completes before the overflow assertion',
        settleAfterResize.ok === true && settleAfterPromotion.ok === true,
        { settleAfterResize, settleAfterPromotion, offenders: geometry.offenders, cardChain: geometry.cardChain });
      // Either the promoted card is still on screen (assert readability), or the
      // product's own overview fill already restored a valid current (assert that
      // real restored state instead of claiming a promotion that is gone).
      const promotedHere = !!(mobileHome && view.cardVisible);
      check('R-home-' + width + ' visible home is readable without horizontal overflow',
        !view.docOverflow && (promotedHere
          ? !!mobileExpect
            && Object.values(mobileHome.fonts).every(font => font !== null && font >= 12)
            && mobileExpect.dates.every(text => mobileHome.date.includes(text))
            && mobileHome.freeVisible === false
          : view.markerCount === 0 && view.archiveCount === 1
            && Number.isFinite(mobileApi.disk_size) && mobileApi.disk_size > 0),
        { width, view, promotedHere, apiCurrent: mobileApi.disk_size,
          archiveId: archive(mobileApi)?.observation_id, fonts: mobileHome && mobileHome.fonts,
          geometry: { innerWidth: geometry.innerWidth, clientWidth: geometry.clientWidth,
            scrollWidth: geometry.scrollWidth, dpr: geometry.devicePixelRatio, fonts: geometry.fonts,
            offenders: geometry.offenders.slice(0, 6), cardChain: geometry.cardChain.slice(0, 4) } });
    }
    await page.setViewportSize({ width: 1400, height: 1000 });

    phase = 'home_visible_back_to_storage';
    // Returning to storage runs the existing entry refresh; accept whatever batch it
    // publishes and compare against that newest real API archive only.
    await page.locator('#pageSeg button[data-page="storage"]').click({ timeout: 8000 });
    await page.locator('#page-storage:not([hidden])').waitFor({ state: 'visible', timeout: 8000 });
    await page.clock.fastForward(15100);
    await page.waitForTimeout(120);
    const backNoise = await nextFrame('mount_noise');
    const backArchive = archive(backNoise);
    const backExpect = primaryTextExpectations(backArchive, await localDates(backArchive));
    const backStorage = await settlePrimary('storage');
    check('P15 storage after the entry refresh re-promotes the newest batch using fresh API values',
      backArchive && validateArchive(backArchive) === null
      && backStorage && backStorage.kind === 'storage'
      && backStorage.value.includes(backExpect.percent) && backStorage.value.includes(backExpect.bytes)
      && backExpect.dates.every(text => backStorage.date.includes(text))
      && backStorage.sig && backStorage.sig.observation_id === backArchive.observation_id
      && backStorage.freeText === '--' && backStorage.freeVisible === true
      && backNoise.disk_size === null,
      { backStorage: backStorage && { value: backStorage.value, date: backStorage.date, sig: backStorage.sig },
        backExpect });

    // ---------------- S9: start/close clear ----------------
    currentCase = 'S9_lifecycle_clear';
    scenarios.add('S9_lifecycle_clear');
    const relogin = (await operation('auth:password')).cookie;
    await context.clearCookies();
    await context.addCookies([{ ...relogin, url: fixture.owner }]);
    // Real document reload so the fresh owner session is picked up by the page
    // (a hash-only goto is a same-document navigation and keeps the login SPA).
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(200);
    const preCloseRefresh = await explicitRefresh();
    const publishedBeforeClose = (await api('/api/overview')).body;
    check('a real owner refresh publishes the archive before lifecycle close',
      preCloseRefresh.status === 200 && validateArchive(archive(publishedBeforeClose)) === null,
      { status: preCloseRefresh.status, archive: archive(publishedBeforeClose) });
    await operation('close');
    const closed = (await api('/api/overview')).body;
    check('closing the sampler lifecycle clears the archive field', archive(closed) === null, closed);
    await page.clock.fastForward(15100);
    const closedDom = await page.waitForFunction(
      () => document.querySelectorAll('[data-r1-archive]').length === 0, null, { timeout: 8000 })
      .then(() => ({ cards: 0 })).catch(async () => ({
        cards: await page.evaluate(() => document.querySelectorAll('[data-r1-archive]').length),
      }));
    check('a deleted archive field leaves no stale archive card in the live DOM',
      closedDom.cards === 0, closedDom);
    // Park the page so its own visible-period auto entries cannot publish a
    // batch between start/close and the next owner read.
    await page.goto('about:blank');
    await operation('start');
    const restarted = await nodeGet('/api/overview', relogin);
    check('start never revives an archive from a prior lifecycle',
      restarted.status === 200 && archive(restarted.body) === null, restarted);
    await operation('reset_publish');
    await page.goto(fixture.owner + '/#/storage', { waitUntil: 'networkidle' });
    await waitPublication();
    const rebuilt = (await page.evaluate(async () => {
      const response = await fetch('/api/overview', { cache: 'no-store' });
      return response.json();
    }));
    check('a genuinely new successful batch after start creates a new archive batch',
      validateArchive(archive(rebuilt)) === null
      && archive(rebuilt).sampled_at > archive(publishedBeforeClose).sampled_at,
      { rebuilt: archive(rebuilt), before: archive(publishedBeforeClose) });
    await page.goto('about:blank');
    await operation('close');
    const finalOverview = await nodeGet('/api/overview', relogin);
    check('final close leaves no archive behind', archive(finalOverview.body) === null, finalOverview);

    // ---------------- hygiene ----------------
    const finalStats = await stats();
    check('no page errors, external requests, blocked writes or real hardware/outbound IO',
      pageErrors.length === 0 && external.length === 0 && blocked.length === 0 && finalStats.forbidden.length === 0,
      { pageErrors, external, blocked, forbidden: finalStats.forbidden });
    // Deliberate negative cases (anonymous 401, revoked 403, closed-service 503)
    // legitimately log a failed resource load; anything else is a defect.
    const expectedStatus = /Failed to load resource: the server responded with a status of (401|403|503)(?: |$)/;
    const unexpectedConsole = consoleErrors.filter(text => !expectedStatus.test(text)
      && !/favicon|net::ERR_/i.test(text));
    check('no unexpected production console errors during the whole archive run',
      unexpectedConsole.length === 0
      && consoleErrors.every(text => expectedStatus.test(text) || /favicon|net::ERR_/i.test(text)),
      { unexpected: unexpectedConsole, consoleErrors });

    successPayload = {
      assertions: checks.length,
      new_assertions: newChecks.length,
      prior_assertions: checks.length - newChecks.length,
      scenarios: [...scenarios],
      evidence, requests,
      fills: requests.filter(request => request.method === 'POST' && request.pathname === '/api/r1/storage/fill'),
      pageErrors, consoleErrors, finalStats,
      ui_evidence: fixture.ui_evidence,
      getter_evidence: getterEvidence,
      fixtureDir,
    };
    // Fault injection is opt-in and only exists to prove the gate can fail.
    if (fault === 'exit1') {
      try { child.kill('SIGKILL'); } catch (_) { /* ignored */ }
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  } catch (error) {
    failure = error;
    snapshot = page && !page.isClosed() ? await page.evaluate(() => ({
      hash: location.hash,
      archiveCards: [...document.querySelectorAll('[data-r1-archive]')].map(node => ({
        attribute: node.getAttribute('data-r1-archive'),
        text: node.textContent.replace(/\s+/g, ' ').trim().slice(0, 200),
      })),
      pool: document.querySelector('#layout-storage [data-ref="poolLeg"]')?.textContent || null,
    })).catch(() => null) : null;
  }

  // ---- cleanup: browser first (its failure keeps its own error), then fixture ----
  clearTimeout(startupTimer);
  if (browser) {
    browserClose.attempted = true;
    try {
      await browser.close();
      if (fault === 'browser-close') throw new Error('injected browser close failure');
      browserClose.ok = true;
    } catch (error) {
      browserClose.error = String(error && error.message || error);
    }
  }
  try {
    if (fault !== 'hang' && !child.stdin.destroyed) child.stdin.end();
  } catch (error) {
    cleanupFailure = 'stdin:' + String(error && error.message || error);
  }
  const waitExit = ms => new Promise(resolve => {
    if (childExit.exited) return resolve();
    const timer = setTimeout(resolve, ms);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
  });
  if (!childExit.exited) await waitExit(fault === 'hang' ? 500 : 10000);
  if (!childExit.exited) {
    exitTimeout = true;
    forcedKill = true;
    try { child.kill('SIGKILL'); } catch (error) { cleanupFailure = 'kill:' + String(error && error.message || error); }
    await waitExit(5000);
  }
  const cleanup = {
    browser_close: browserClose,
    fixture: { exited: childExit.exited, exit_code: childExit.code, signal: childExit.signal,
      forced_kill: forcedKill, timeout_before_kill: exitTimeout },
    failure: cleanupFailure,
  };
  const cleanupOk = browserClose.ok && childExit.exited && childExit.code === 0
    && childExit.signal === null && !forcedKill && !exitTimeout && !cleanupFailure;

  if (!failure && successPayload && cleanupOk) {
    const evidencePath = path.join(fixtureDir, 'capacity-archive-evidence.json');
    fs.writeFileSync(evidencePath, JSON.stringify({ ...successPayload, cleanup }, null, 2));
    console.log(JSON.stringify({ ok: true, assertions: successPayload.assertions,
      new_assertions: successPayload.new_assertions, prior_assertions: successPayload.prior_assertions,
      scenarios: successPayload.scenarios.length, scenario_names: successPayload.scenarios,
      fixtureDir, evidencePath, cleanup }, null, 2));
    process.exitCode = 0;
  } else {
    console.error(JSON.stringify({ ok: false,
      error: failure ? String(failure.stack || failure)
        : 'harness cleanup failed after all assertions passed (fixture exit/browser close not clean)',
      currentCase, phase,
      assertions: checks.length, new_assertions: newChecks.length,
      prior_assertions: checks.length - newChecks.length,
      scenarios: [...scenarios], fixtureDir, snapshot, cleanup,
      getter_evidence: getterEvidence,
      pageErrors, consoleErrors, external, blocked, stderr: stderr.slice(-4000) }, null, 2));
    process.exitCode = 1;
  }
}

if (require.main === module) {
  main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
}
module.exports = { settleBounded };
