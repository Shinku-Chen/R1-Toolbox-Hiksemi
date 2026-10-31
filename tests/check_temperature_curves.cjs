// Real TemperatureSources -> Monitoring/SQLite -> Fnm/WebConfigServer -> built
// homepage and SVG. Business responses are never route.fulfill substitutions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-temperature-curves-'));
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, 'Set CHROME_BIN to an installed Chrome/Chromium executable');

(async () => {
  const child = spawn(process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3'),
    ['-B', '-u', path.join(__dirname, 'fixtures/temperature_curves_fixture.py'), root, fixtureDir],
    { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let stderr = '', browser, page, resolveReady, rejectReady, sequence = 0;
  let currentCase = '', phase = 'startup';
  const pending = new Map(), checks = [], requests = [], errors = [], external = [], writes = [];
  const drawInputs = [], evidence = [];
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const startupTimer = setTimeout(() => rejectReady(new Error('temperature fixture startup timeout')), 15000);
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  child.on('error', rejectReady);
  child.on('exit', code => { if (code) rejectReady(new Error('temperature fixture exited: ' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(startupTimer); resolveReady(value); }
    if (pending.has(value.id)) { pending.get(value.id)(value); pending.delete(value.id); }
  });
  const fixtureCall = operation => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('fixture operation timeout: ' + operation)); }, 10000);
    pending.set(id, value => { clearTimeout(timeout); resolve(value); });
    child.stdin.write(JSON.stringify({ id, operation }) + '\n');
  });
  const check = (name, condition, detail) => {
    assert.ok(condition, name + (detail ? ': ' + JSON.stringify(detail) : ''));
    checks.push(name);
  };
  const api = pathname => page.evaluate(async pathname => {
    const response = await fetch(pathname, { cache: 'no-store' });
    return { status: response.status, body: await response.json() };
  }, pathname);
  const metric = (state, id) => state.current.metrics.find(row => row.id === id);
  const temperatureDOM = async () => page.locator('#layout-overview [data-m="temp"]').evaluate(card => ({
    value: card.querySelector('[data-ref="tempV"]').getAttribute('aria-label'),
    note: card.querySelector('[data-ref="tempSub"]').textContent,
  }));
  const chartSelector = '#layout-overview [data-ref="ch-temp"]';
  const detailButton = '#layout-overview .chart-card:has([data-ref="ch-temp"]) [data-sheet="temp"]';
  const noteSelector = '#layout-overview [data-ref="ch-temp"] + [data-r1-temperature-note]';
  const noteText = '主板温度暂无有效读数；CPU 或未确认的热区不会代替主板测点。其他测点可在“全部传感器”查看。';
  const notes = selector => page.locator(selector).evaluateAll(items => items.map(note => ({ hidden: note.hidden, text: note.textContent })));
  const noteMatches = (items, missing) => missing
    ? items.length === 1 && items[0].hidden === false && items[0].text === noteText
    : items.every(item => item.hidden === true);
  const chartDOM = () => page.locator(chartSelector).evaluate(host => ({
    empty: !host.querySelector('.chart-empty').hidden,
    visible: host.querySelector('svg').style.visibility,
    paths: [...host.querySelectorAll('.gs path.ln')].map(item => item.getAttribute('d')),
    point: host.querySelector('.live-pt .c')?.getAttribute('cy'),
  }));
  const awaitDraw = async predicate => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      const records = await page.evaluate(() => window.__r1TemperatureCurveInputs.splice(0));
      records.forEach(record => drawInputs.push({ case: currentCase, phase, ...record }));
      const result = [...drawInputs].reverse().find(predicate);
      if (result) return result;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw Error('no production temperature chart input: ' + currentCase + '/' + phase);
  };
  try {
    const fixture = await ready;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, locale: 'zh-CN' });
    await context.route('**/*', route => {
      const request = route.request(), url = request.url();
      if (url.startsWith('data:')) return route.continue();
      if (new URL(url).origin !== fixture.owner) {
        external.push(url); return route.abort('blockedbyclient');
      }
      const preference = request.method() === 'POST' && new URL(url).pathname === '/api/ui';
      if (request.method() !== 'GET' && !preference) {
        writes.push({ method: request.method(), pathname: new URL(url).pathname });
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    page = await context.newPage();
    await page.clock.install();
    await page.addInitScript(() => { window.__r1TemperatureCurveInputs = []; });
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin === fixture.owner && url.pathname.startsWith('/api/'))
        requests.push({ case: currentCase, pathname: url.pathname, method: request.method() });
    });
    check('the served build has exactly one TEMP read-only input observer and no replaced renderer',
      fixture.ui_evidence?.replacement_count === 1
      && fixture.ui_evidence?.source_sha256 !== fixture.ui_evidence?.observed_sha256,
      fixture.ui_evidence);

    for (const scenario of ['board', 'no_board', 'cpu_only', 'history_empty', 'history_off', 'cache_hit', 'zero', 'negative', 'stale', 'future', 'cache_expired', 'slow_device', 'wall_jump']) {
      currentCase = scenario; phase = 'history';
      await page.goto('about:blank');
      const before = (await fixtureCall('case:' + scenario)).state;
      await page.goto(fixture.owner + '/#/overview', { waitUntil: 'networkidle' });
      await page.waitForSelector(chartSelector);
      await page.locator(chartSelector).scrollIntoViewIfNeeded();
      await page.locator('#layout-overview [data-mod="trends"] button[data-v="24h"]').click();
      const day = await awaitDraw(row => row.case === scenario && row.ref === 'ch-temp' && row.span === 86400);
      const overview = (await api('/api/overview')).body;
      const sensors = (await api('/api/sensors')).body;
      const cpuHistory = (await api('/api/history?metric=temp&range=24h')).body;
      const boardHistory = (await api('/api/history?metric=temp_mb&range=24h')).body;
      const currentCpu = metric(before, 'cpu_temperature_c');
      const currentBoard = metric(before, 'board_temperature_c');
      const missing = ['stale', 'future', 'cache_expired'].includes(scenario);
      const expectCpu = missing ? null : before.expected_cpu;
      const expectBoard = missing ? null : before.expected_board;
      check(scenario + ': live CPU uses canonical Monitoring rather than legacy 99 degrees',
        currentCpu && currentCpu.value === (scenario === 'cache_expired' ? before.expected_cpu : expectCpu)
        && overview.temp.cpu === expectCpu,
        { currentCpu, actual: overview.temp, expected: expectCpu });
      check(scenario + ': motherboard uses only a positively identified board source',
        currentBoard && currentBoard.value === (scenario === 'cache_expired' ? before.expected_board : expectBoard)
        && overview.temp.system === expectBoard,
        { currentBoard, actual: overview.temp, expected: expectBoard });
      check(scenario + ': temperature source and mb alias preserve canonical meaning',
        overview.temp.source === 'monitoring_temperature_cache' && overview.temp.mb === overview.temp.system,
        overview.temp);
      check(scenario + ': original measurement dates and lifetimes are projected without GET renewal',
        [['cpu', currentCpu, expectCpu], ['board', currentBoard, expectBoard]].every(([role, original, value]) => {
          const sample = overview.temp.samples?.[role];
          return sample && sample.sampled_at === original.sampled_at
            && sample.sampled_monotonic === original.sampled_monotonic
            && sample.max_age_seconds === original.max_age_seconds
            && sample.fresh === (value !== null)
            && (value === null ? typeof sample.reason === 'string' : sample.reason === null);
        }), { samples: overview.temp.samples, currentCpu, currentBoard });
      check(scenario + ': default 24h chart consumes the actual two history endpoints',
        JSON.stringify(day.cols) === JSON.stringify([cpuHistory.points, boardHistory.points]),
        { day, cpuHistory, boardHistory });
      if (['no_board', 'cpu_only'].includes(scenario)) {
        check(scenario + ': no independent motherboard reading is invented from CPU or ACPI',
          boardHistory.points.every(point => point[1] === null) && day.cols[1].every(point => point[1] === null),
          { boardHistory, day, overview: overview.temp });
      }
      if (scenario === 'board') {
        check('new source reads with a strictly advancing clock never create spurious CPU nulls',
          cpuHistory.points.length >= 2 && cpuHistory.points.every(point => point[1] === 47), cpuHistory);
        check('positively identified board temperature has persisted real 24h history',
          boardHistory.points.length >= 2 && boardHistory.points.every(point => point[1] === 33), boardHistory);
        check('unclassified ACPI/PCH/INT3400 and CPUTIN/AUXTIN do not win the board maximum',
          overview.temp.system === 33 && before.saved_sources.some(row => row.temperature_c >= 88), before.saved_sources);
      }
      if (scenario === 'cache_hit') check('source one-second cache hit does not read the TEMP sensor again',
        before.cache_read_count === 0, before);
      if (['stale', 'future'].includes(scenario)) {
        check(scenario + ': genuine invalid observation remains null in persisted history',
          cpuHistory.points.at(-1)?.[1] === null && boardHistory.points.at(-1)?.[1] === null,
          { cpuHistory, boardHistory, currentCpu, currentBoard });
      }
      if (scenario === 'future') check('future source time does not invent a wall measurement date',
        overview.temp.samples.cpu.reason === 'future_sample' && overview.temp.samples.cpu.sampled_at === null
        && overview.temp.samples.board.sampled_at === null, overview.temp.samples);
      if (scenario === 'cache_expired') check('worker-stopped cache expires on GET while preserving its successful date',
        overview.temp.samples.cpu.reason === 'stale_sample'
        && overview.temp.samples.cpu.sampled_at === currentCpu.sampled_at
        && currentCpu.value === 47 && overview.temp.cpu === null, overview.temp);
      if (scenario === 'slow_device') {
        check('fresh source cache keeps independent sensors current despite old slow device inventory',
          before.wall - Date.parse(before.device_sampled_at) / 1000 >= 4
          && sensors.temps.some(row => row.name.includes('coretemp') && row.value === 55 && row.fresh === true)
          && sensors.temps.some(row => row.name.includes('SYSTIN') && row.value === 42 && row.fresh === true),
          { deviceSampled: before.device_sampled_at, sensors, currentCpu, currentBoard });
      }
      if (scenario === 'wall_jump') {
        const oldCpu = before.wall_anchor_before.metrics.find(row => row.id === 'cpu_temperature_c');
        const oldBoard = before.wall_anchor_before.metrics.find(row => row.id === 'board_temperature_c');
        check('same source sample keeps its original wall measurement date after a consumer wall-clock jump',
          currentCpu.sampled_monotonic === oldCpu.sampled_monotonic && currentCpu.sampled_at === oldCpu.sampled_at
          && currentBoard.sampled_monotonic === oldBoard.sampled_monotonic && currentBoard.sampled_at === oldBoard.sampled_at,
          { oldCpu, currentCpu, oldBoard, currentBoard });
      }
      if (['history_empty', 'history_off'].includes(scenario)) {
        const graph = await chartDOM();
        check(scenario + ': empty/disabled long-range history is not fabricated from live temperature',
          cpuHistory.points.length === 0 && boardHistory.points.length === 0 && graph.empty,
          { cpuHistory, boardHistory, graph });
      } else {
        const graph = await chartDOM();
        const known = day.cols.some(col => col.some(point => typeof point[1] === 'number'));
        check(scenario + ': actual history SVG draws known data and keeps unsupported board empty',
          graph.empty === !known && graph.paths.length === (known ? 2 : 0)
          && (!known || graph.paths.every((d, index) => day.cols[index].some(point => typeof point[1] === 'number') ? d?.startsWith('M') : d === '')),
          { graph, day });
      }
      phase = 'short';
      await page.locator('#layout-overview [data-mod="trends"] button[data-v="1h"]').click();
      const short = await awaitDraw(row => row.case === scenario && row.ref === 'ch-temp' && row.span === 3600);
      const shortCpu = (await api('/api/history?metric=temp&range=1h')).body;
      const shortBoard = (await api('/api/history?metric=temp_mb&range=1h')).body;
      check(scenario + ': short chart preserves genuine historical values rather than a live-only substitute',
        [shortCpu.points, shortBoard.points].every((points, index) => points.every(point =>
          short.cols[index].some(actual => actual[0] === point[0] && actual[1] === point[1]))),
        { short, shortCpu, shortBoard });
      const homepageNotes = await notes(noteSelector);
      check(scenario + ': homepage accurately explains unavailable motherboard data',
        noteMatches(homepageNotes, expectBoard === null), homepageNotes);
      if (['no_board', 'cpu_only'].includes(scenario)) {
        const graph = await chartDOM();
        check(scenario + ': CPU curve remains visible while the absent motherboard curve stays empty',
          graph.paths[0]?.startsWith('M') && graph.paths[1] === '' && !graph.empty,
          { graph, short });
      }
      await page.locator('#layout-overview [data-m="temp"]').scrollIntoViewIfNeeded();
      const readout = await temperatureDOM();
      check(scenario + ': homepage readout preserves measured zero/negative and missing values',
        readout.value === (expectCpu === null ? '--' : String(expectCpu)), { readout, expectCpu });
      await page.locator(detailButton).click();
      await page.waitForSelector('#sheet.on [data-ref="c2"]');
      const detailNotes = await notes('#sheetBody > [data-r1-temperature-note]');
      const detail = await page.locator('#sheetBody').evaluate(body => ({
        cpu: body.querySelector('[data-ref="c1"]').textContent,
        board: body.querySelector('[data-ref="c2"]').textContent,
      }));
      check(scenario + ': temperature drawer preserves unknown board semantics and explanation',
        noteMatches(detailNotes, expectBoard === null)
        && (expectBoard === null ? detail.board === '—' : Number.parseFloat(detail.board) === expectBoard)
        && (expectCpu === null ? detail.cpu === '—' : Number.parseFloat(detail.cpu) === expectCpu),
        { detailNotes, detail, expectCpu, expectBoard });
      await page.keyboard.press('Escape');
      await page.waitForSelector('#sheet.on', { state: 'hidden' });
      const after = (await fixtureCall('stats')).state;
      check(scenario + ': all HTTP cache/history/sensor reads perform zero sensor collection',
        after.sensor_reads === before.sensor_reads && after.sensor_opens === before.sensor_opens
        && after.current.sampled_at === before.current.sampled_at,
        { readsBefore: before.sensor_reads, readsAfter: after.sensor_reads,
          opensBefore: before.sensor_opens, opensAfter: after.sensor_opens });
      const repeated = (await api('/api/overview')).body;
      check(scenario + ': repeated passive overview keeps the same source sample time',
        JSON.stringify(repeated.temp.samples) === JSON.stringify(overview.temp.samples),
        { original: overview.temp.samples, repeated: repeated.temp.samples });
      if (scenario === 'wall_jump') {
        const stopped = (await fixtureCall('expire_only')).state;
        const expired = (await api('/api/overview')).body;
        check('wall-clock-jump sample still expires on GET without another source read or date renewal',
          expired.temp.cpu === null && expired.temp.system === null
          && expired.temp.samples.cpu.sampled_at === currentCpu.sampled_at
          && expired.temp.samples.board.sampled_at === currentBoard.sampled_at
          && expired.temp.samples.cpu.reason === 'stale_sample'
          && stopped.sensor_opens === after.sensor_opens
          && (await fixtureCall('stats')).state.sensor_opens === stopped.sensor_opens,
          { expired: expired.temp, currentCpu, currentBoard });
      }
      evidence.push({ scenario, before, overview, sensors, cpuHistory, boardHistory, day, short, readout, detailNotes, detail, after });
    }

    currentCase = 'same_dom_transition'; phase = 'numeric';
    await page.goto('about:blank');
    await fixtureCall('case:board');
    await page.goto(fixture.owner + '/#/overview', { waitUntil: 'networkidle' });
    await page.locator('#layout-overview [data-mod="trends"] button[data-v="1h"]').click();
    await page.locator(chartSelector).scrollIntoViewIfNeeded();
    const initial = await awaitDraw(row => row.case === currentCase && row.ref === 'ch-temp' && row.span === 3600);
    const cpuNode = await page.locator('#layout-overview [data-m="temp"] [data-ref="tempV"]').elementHandle();
    const publish = async operation => {
      const stats = (await fixtureCall(operation)).state;
      const response = page.waitForResponse(value => value.url() === fixture.owner + '/api/overview');
      await page.clock.fastForward(5100); await response;
      await page.waitForTimeout(50);
      return stats;
    };
    phase = 'missing';
    const missingState = await publish('missing');
    await page.locator('#layout-overview [data-m="temp"]').scrollIntoViewIfNeeded();
    const missingReadout = await temperatureDOM();
    const missingHistory = (await api('/api/history?metric=temp&range=1h')).body;
    check('same DOM numeric-to-missing does not resurrect slow device or legacy CPU temperature',
      missingReadout.value === '--' && metric(missingState, 'cpu_temperature_c').value === null
      && missingHistory.points.at(-1)?.[1] === null
      && await cpuNode.evaluate(node => node === document.querySelector('#layout-overview [data-m="temp"] [data-ref="tempV"]')),
      { missingReadout, missingHistory });
    await page.locator(chartSelector).scrollIntoViewIfNeeded();
    const missingNote = page.locator(noteSelector);
    await missingNote.waitFor({ state: 'visible' });
    const noteNode = await missingNote.elementHandle();
    check('same homepage node explains missing board without requiring reopen',
      noteMatches(await notes(noteSelector), true), await notes(noteSelector));
    phase = 'restore';
    const restoredState = await publish('restore');
    await page.locator(chartSelector).scrollIntoViewIfNeeded();
    const restoredDraw = await awaitDraw(row => row.case === currentCase && row.phase === 'restore' && row.ref === 'ch-temp'
      && row.cols[0].at(-1)?.[1] === 51 && row.cols[1].at(-1)?.[1] === 35);
    await page.locator('#layout-overview [data-m="temp"]').scrollIntoViewIfNeeded();
    const restoredReadout = await temperatureDOM();
    check('same DOM recovered measurement returns the real CPU and board values',
      restoredReadout.value === '51' && metric(restoredState, 'board_temperature_c').value === 35
      && await cpuNode.evaluate(node => node === document.querySelector('#layout-overview [data-m="temp"] [data-ref="tempV"]')),
      { restoredReadout, restoredDraw });
    await page.locator(chartSelector).scrollIntoViewIfNeeded();
    check('recovered board measurement hides the same homepage explanation node',
      noteMatches(await notes(noteSelector), false)
      && await noteNode.evaluate(node => node === document.querySelector('#layout-overview [data-ref="ch-temp"] + [data-r1-temperature-note]')),
      await notes(noteSelector));
    phase = 'drawer_transition';
    await page.locator(detailButton).click();
    await page.waitForSelector('#sheet.on [data-ref="c2"]');
    await publish('missing');
    const drawerNote = page.locator('#sheetBody > [data-r1-temperature-note]');
    await drawerNote.waitFor({ state: 'visible' });
    const drawerNoteNode = await drawerNote.elementHandle();
    check('open drawer follows numeric-to-missing board state without rebuild',
      (await page.locator('#sheetBody [data-ref="c2"]').textContent()) === '—'
      && noteMatches(await notes('#sheetBody > [data-r1-temperature-note]'), true));
    await publish('restore');
    check('open drawer follows restored board value and hides the same explanation node',
      Number.parseFloat(await page.locator('#sheetBody [data-ref="c2"]').textContent()) === 35
      && noteMatches(await notes('#sheetBody > [data-r1-temperature-note]'), false)
      && await drawerNoteNode.evaluate(node => node === document.querySelector('#sheetBody > [data-r1-temperature-note]')));
    const final = await fixtureCall('stats');
    check('no browser errors, external requests, collection writes, SMART, capacity or commands',
      errors.length === 0 && external.length === 0 && writes.length === 0 && final.forbidden.length === 0,
      { errors, external, writes, forbidden: final.forbidden });
    evidence.push({ scenario: currentCase, initial, missingState, missingReadout, restoredState, restoredReadout, restoredDraw });
    const evidencePath = path.join(fixtureDir, 'temperature-evidence.json');
    fs.writeFileSync(evidencePath, JSON.stringify({ checks, evidence, drawInputs, requests, final,
      ui_evidence: fixture.ui_evidence }, null, 2));
    console.log(JSON.stringify({ ok: true, count: checks.length, scenarios: evidence.length,
      checks, fixtureDir, evidencePath }, null, 2));
  } catch (error) {
    const dom = page && !page.isClosed() ? await page.evaluate(() => ({
      chart: !!document.querySelector('#layout-overview [data-ref="ch-temp"]'),
      temperature: document.querySelector('#layout-overview [data-m="temp"]')?.textContent,
    })).catch(() => null) : null;
    console.error(JSON.stringify({ ok: false, currentCase, phase, checks, fixtureDir,
      error: String(error?.stack || error), dom, errors, external, writes, stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(startupTimer);
    if (browser) await browser.close();
    child.stdin.end();
    await new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      const timeout = setTimeout(() => { child.kill(); resolve(); }, 10000);
      child.once('exit', () => { clearTimeout(timeout); resolve(); });
    });
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
