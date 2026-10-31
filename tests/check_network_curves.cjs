// Production producer -> canonical monitoring -> HTTP panel -> real SVG curves.
// Kernel metadata comes from tests/fixtures/network_curves_fixture.py; no NAS,
// external network, disk collection, route.fulfill or replacement renderer.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-network-curves-'));
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, 'Set CHROME_BIN to an installed Chrome/Chromium executable');

(async () => {
  const child = spawn(process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3'),
    ['-B', '-u', path.join(__dirname, 'fixtures/network_curves_fixture.py'), root, fixtureDir],
    { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let stderr = '', browser, activePage, resolveReady, rejectReady, sequence = 0;
  let currentCase = '', phase = 'startup';
  const replies = new Map(), checks = [], requests = [], errors = [], external = [], blockedWrites = [];
  const drawInputs = [], evidence = [];
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const timer = setTimeout(() => rejectReady(new Error('fixture startup timeout')), 15000);
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  child.on('error', rejectReady);
  child.on('exit', code => {
    if (code) rejectReady(new Error('fixture exited: ' + code + '\n' + stderr));
  });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch (_) { return; }
    if (value.ready) { clearTimeout(timer); resolveReady(value); }
    if (replies.has(value.id)) { replies.get(value.id)(value); replies.delete(value.id); }
  });
  const fixtureCall = operation => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { replies.delete(id); reject(new Error('fixture operation timeout: ' + operation)); }, 10000);
    replies.set(id, value => { clearTimeout(timeout); resolve(value); });
    child.stdin.write(JSON.stringify({ id, operation }) + '\n');
  });
  const check = (name, condition, detail) => {
    assert.ok(condition, name + (detail ? ': ' + JSON.stringify(detail) : ''));
    checks.push(name);
  };
  const api = (page, pathname) => page.evaluate(async pathname => {
    const response = await fetch(pathname, { cache: 'no-store' });
    return { status: response.status, body: await response.json() };
  }, pathname);
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
      // Range/tab changes use the actual /api/ui presentation-only store in
      // this temporary server. Every collection/control write is forbidden.
      const presentation = request.method() === 'POST' && new URL(url).pathname === '/api/ui';
      if (request.method() !== 'GET' && !presentation) {
        blockedWrites.push({ method: request.method(), pathname: new URL(url).pathname });
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    const page = await context.newPage(); activePage = page;
    await page.clock.install();
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin === fixture.owner && url.pathname.startsWith('/api/')) {
        requests.push({ case: currentCase, pathname: url.pathname, method: request.method() });
      }
    });

    // Observe the actual production LineChart.setData without changing code or
    // exporting private state. A numeric assertion plus real SVG path/point
    // assertions avoids mistaking a renderer invocation for visible data.
    const cdp = await context.newCDPSession(page);
    await cdp.send('Debugger.enable');
    const html = fs.readFileSync(path.join(root, 'ui/web/index.html'), 'utf8');
    const lineNumber = html.split('\n').findIndex(line => line.includes('const clean = (cols || [])'));
    assert.ok(lineNumber >= 0, 'production chart setData observation point exists');
    await cdp.send('Debugger.setBreakpointByUrl', { lineNumber, urlRegex: '^http://127\\.0\\.0\\.1:' });
    cdp.on('Debugger.paused', async event => {
      try {
        const value = await cdp.send('Debugger.evaluateOnCallFrame', {
          callFrameId: event.callFrames[0].callFrameId,
          expression: '({ref:this.host.dataset.ref,module:this.host.closest("[data-mod]")?.dataset.mod,sheet:!!this.host.closest("#sheetBody"),labels:this.opt.series.map(s=>s.label),cols,span})',
          returnByValue: true,
        });
        const result = value.result.value;
        if (result?.labels?.join('/') === '\u4e0b\u884c/\u4e0a\u884c') {
          drawInputs.push({ case: currentCase, phase, ...result });
        }
      } catch (error) { errors.push('debugger: ' + error.message); }
      finally { await cdp.send('Debugger.resume').catch(() => {}); }
    });
    const chartDOM = selector => page.locator(selector).evaluate(host => ({
      empty: !host.querySelector('.chart-empty').hidden,
      visible: host.querySelector('svg').style.visibility,
      paths: [...host.querySelectorAll('.gs path.ln')].map(path => path.getAttribute('d')),
      point: host.querySelector('.live-pt .c')?.getAttribute('cy'),
    }));
    const chartSelector = '#layout-overview [data-ref="ch-net"]';
    const shownRate = value => ({ 0: '0 B/s', 300000: '293 KB/s', 480000: '469 KB/s',
      600000: '586 KB/s', 900000: '879 KB/s', 1250000: '1.2 MB/s', 1800000: '1.7 MB/s' })[value] || '--';
    const rateNumbers = rates => rates.map(value => shownRate(value).split(' ')[0]);
    const metricDOM = () => page.locator('#layout-overview [data-m="net"]').evaluate(card => ({
      values: ['rxV', 'txV'].map(ref => card.querySelector('[data-ref="' + ref + '"]').getAttribute('aria-label')),
      status: card.querySelector('[data-ref="netSt"]').textContent,
    }));
    const awaitRecord = async predicate => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        const result = [...drawInputs].reverse().find(predicate);
        if (result) return result;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      throw Error('no real chart call: ' + currentCase + '/' + phase);
    };
    const nextOverview = async () => {
      await fixtureCall('advance');
      const pending = page.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
      await page.clock.fastForward(5100); await pending;
      await page.waitForTimeout(50);
    };

    for (const scenario of ['numeric', 'control_file', 'one_unknown', 'bridge_filtered', 'bridge_included',
      'bond_filtered', 'bond_included', 'first', 'restart', 'rollback', 'misaligned', 'only_virtual',
      'only_virtual_filtered', 'mixed_rx_unknown', 'mixed_tx_unknown', 'only_virtual_mixed', 'zero', 'only_virtual_zero']) {
      currentCase = scenario; phase = 'overview';
      await page.goto('about:blank');
      const stats = (await fixtureCall('case:' + scenario)).state;
      await page.goto(fixture.owner + '/#/overview', { waitUntil: 'networkidle' });
      await page.waitForSelector('#layout-overview [data-mod="trends"]');
      await page.locator(chartSelector).scrollIntoViewIfNeeded();
      if (scenario === 'numeric') {
        await page.locator('#layout-overview [data-mod="trends"] button[data-v="24h"]').click();
        await page.locator(chartSelector).scrollIntoViewIfNeeded();
        await awaitRecord(record => record.case === scenario && record.ref === 'ch-net' && record.span === 86400);
        const day = await chartDOM(chartSelector);
        check('empty disabled 24h history does not synthesize live history', day.empty, day);
      }
      await page.locator('#layout-overview [data-mod="trends"] button[data-v="1h"]').click();
      await page.locator(chartSelector).scrollIntoViewIfNeeded();
      const initial = await awaitRecord(record => record.case === scenario && record.ref === 'ch-net' && record.span === 3600);
      const response = await api(page, '/api/overview');
      assert.equal(response.status, 200);
      const overview = response.body, aggregate = overview.net_aggregate;
      const recover = ['first', 'restart', 'rollback'].includes(scenario);
      const unknown = ['one_unknown', 'first', 'restart', 'rollback', 'only_virtual_filtered'].includes(scenario);
      const mixed = ['mixed_rx_unknown', 'mixed_tx_unknown', 'only_virtual_mixed'].includes(scenario);
      const included = ['bridge_included', 'bond_included'].includes(scenario);
      const expected = ['zero', 'only_virtual_zero'].includes(scenario) ? 0 : scenario === 'control_file' ? 1250000 : included ? 1800000 : 900000;
      const rates = [aggregate.rx_rate, aggregate.tx_rate];
      if (unknown) {
        check(scenario + ': initial curve contains no invented zero for unknown rates', initial.cols.every(col => col.length === 0), initial);
      } else if (mixed) {
        check(scenario + ': each canonical direction independently keeps measured or unknown data',
          initial.cols.every((col, index) => rates[index] === null ? !col.length : col.length === 1 && col[0][1] === rates[index]), { initial, aggregate });
      } else {
        check(scenario + ': real overview chart consumes canonical aggregate',
          initial.cols[0].length > 0 && initial.cols[0].at(-1)[1] === expected, { initial, aggregate });
      }
      if (scenario === 'control_file') {
        check('ordinary bonding_masters control file is absent from real producer rows',
          !stats.rows.some(row => row.name === 'bonding_masters')
          && stats.rows.find(row => row.name === 'eth0').rx_bytes_per_sec === 1250000, stats.rows);
        check('two real frames recover canonical aggregate and Fnm preserves its exact source',
          aggregate.schema_version === 1 && aggregate.source === 'monitoring_filtered_counters'
          && aggregate.rx_rate === 1250000 && aggregate.tx_rate === 480000
          && overview.net.length === 1 && overview.net[0].rx_rate === 1250000, overview);
      }
      if (scenario === 'one_unknown') {
        check('real eth1 missing counters remains visible and forces unknown aggregate',
          stats.rows.some(row => row.name === 'eth1' && row.rx_bytes_per_sec === null)
          && overview.net.some(row => row.iface === 'eth1' && row.rx_rate === null)
          && aggregate.rx_rate === null && aggregate.tx_rate === null, overview);
      }
      if (['bridge_filtered', 'bond_filtered'].includes(scenario)) {
        check(scenario + ': canonical virtual exclusion avoids the double-counted visible-row sum',
          aggregate.exclude_virtual === true && aggregate.rx_rate === 900000
          && overview.net.reduce((total, row) => total + row.rx_rate, 0) === 1800000, overview);
      }
      if (included) {
        check(scenario + ': explicit virtual inclusion preserves original accounting policy',
          aggregate.exclude_virtual === false && aggregate.rx_rate === 1800000, aggregate);
      }
      if (scenario === 'misaligned') {
        check('independent cached snapshots do not replace canonical rate with a row fallback',
          aggregate.rx_rate === 900000 && overview.net[0].rx_rate === 1250000
          && aggregate.sampled_at < overview.ts, overview);
      }
      if (scenario === 'only_virtual') {
        // Changed contract: a trusted measured total is not gated by the
        // upstream physical-inventory presentation filter anymore.
        check('explicitly included VPN-only canonical data reaches the real curve',
          aggregate.rx_rate === 900000 && overview.net[0].iface === 'wg0'
          && initial.cols[0].length === 1 && initial.cols[0][0][1] === 900000, overview);
      }
      if (scenario === 'only_virtual_filtered') {
        check('excluded VPN-only inventory stays unknown rather than inventing idle zero',
          aggregate.exclude_virtual === true && rates.every(value => value === null), aggregate);
      }
      if (mixed) check(scenario + ': one unavailable direction does not suppress or zero the other direction',
        scenario === 'mixed_tx_unknown' ? rates[0] === 900000 && rates[1] === null : rates[0] === null && rates[1] === 300000, aggregate);
      if (['zero', 'only_virtual_zero'].includes(scenario)) check(scenario + ': genuine counter-stable rates are measured zeros',
        rates.every(value => value === 0) && overview.net.every(row => row.rx_rate === 0 && row.tx_rate === 0), overview);
      if (recover) {
        check(scenario + ': Insights and StatusSampler both preserve first/reset unknown',
          stats.rows[0].rx_bytes_per_sec === null && stats.primary.rx_bytes_per_sec === null
          && stats.first_primary.rx_bytes_per_sec === null, stats);
      }
      check(scenario + ': one initial sample per measured direction, never duplicate feed',
        initial.cols.every((col, index) => col.length === (rates[index] === null ? 0 : 1)), initial);
      await page.locator('#layout-overview [data-m="net"]').scrollIntoViewIfNeeded();
      const metrics = await metricDOM();
      check(scenario + ': overview readouts use the same canonical directions, including zero',
        JSON.stringify(metrics.values) === JSON.stringify(rateNumbers(rates))
        && metrics.status === (rates.some(value => value === null) ? '未知' : rates[0] + rates[1] < 10240 ? '空闲' : '活跃'), { metrics, rates });
      await page.locator(chartSelector).scrollIntoViewIfNeeded();
      await nextOverview();
      const next = (await api(page, '/api/overview')).body;
      const nextRates = [next.net_aggregate.rx_rate, next.net_aggregate.tx_rate];
      const counts = nextRates.map((value, index) => initial.cols[index].length + (value === null ? 0 : 1));
      const after = await awaitRecord(record => record.case === scenario && record.ref === 'ch-net' && record.span === 3600
        && record.cols.every((col, index) => col.length >= counts[index]));
      check(scenario + ': repeated GETs do not append duplicates or backfill unknown history',
        after.cols.every((col, index) => col.length === counts[index]
          && (nextRates[index] === null || col.at(-1)[1] === nextRates[index])), { after, counts, nextRates });
      const graph = await chartDOM(chartSelector);
      if (unknown && !recover) {
        check(scenario + ': unknown real SVG stays empty rather than drawing zero', graph.empty && graph.visible === 'hidden', graph);
      } else {
        check(scenario + ': actual SVG draws measured paths and a live point',
          !graph.empty && graph.paths.length === 2
          && graph.paths.every((d, index) => nextRates[index] === null ? !d : d && d.startsWith('M'))
          && (nextRates[0] === null || graph.point != null), { graph, after });
      }
      phase = 'network';
      await page.locator('#pageSeg button[data-page="network"]').click();
      await page.waitForSelector('#layout-network [data-ref="ifs"]');
      await page.clock.fastForward(1000); // Old numeric tween must not restore a false zero.
      const networkRates = await page.locator('#layout-network [data-ref="r1NetworkRates"]').textContent();
      check(scenario + ': network page summary preserves the same measured/unknown total',
        networkRates.includes('↓ ' + shownRate(nextRates[0])) && networkRates.includes('↑ ' + shownRate(nextRates[1])), { networkRates, nextRates });
      const interfaceDOM = await page.locator('#layout-network .if-row').evaluateAll(rows => rows.map(row => ({
        name: row.querySelector('.label').textContent.trim().split(/\s/)[0],
        rx: row.querySelector('[data-ref^="rx"]').textContent,
        tx: row.querySelector('[data-ref^="tx"]').textContent,
      })));
      check(scenario + ': actual interface readouts never coerce unknown rate to zero', interfaceDOM.every(row => {
        const source = next.net.find(item => row.name.startsWith(item.iface));
        return source && row.rx === shownRate(source.rx_rate) && row.tx === shownRate(source.tx_rate);
      }), { interfaceDOM, net: next.net });
      phase = 'detail';
      await page.locator('#layout-network [data-sheet="net"]').first().click();
      await page.waitForSelector('#sheet.on .chart');
      const detail = await awaitRecord(record => record.case === scenario && record.phase === 'detail' && record.sheet && record.span === 3600);
      const detailDOM = await chartDOM('#sheetBody .chart-card .chart >> nth=0');
      check(scenario + ': real network detail uses the same measured or unknown curve',
        JSON.stringify(detail.cols) === JSON.stringify(after.cols)
        && detailDOM.empty === (unknown && !recover), { detail, after, detailDOM });
      const detailValues = await page.locator('#sheetBody .sheet-hero').evaluate(hero => ['rx', 'tx'].map(ref => hero.querySelector('[data-ref="' + ref + '"]').getAttribute('aria-label')));
      const detailInterfaces = await page.locator('#sheetBody [data-ref="ifs"] .kv > div').evaluateAll(rows => rows.map(row => ({ name: row.querySelector('dt').textContent, text: row.querySelector('dd').textContent })));
      check(scenario + ': detail totals and real interface rates use the current overview, not cumulative-only rows',
        JSON.stringify(detailValues) === JSON.stringify(rateNumbers(nextRates))
        && next.net.filter(row => row.iface !== 'lo').every(row => detailInterfaces.some(item => item.name.startsWith(row.iface)
          && item.text.startsWith('↓ ' + shownRate(row.rx_rate) + ' · ↑ ' + shownRate(row.tx_rate)))),
        { detailValues, detailInterfaces, nextRates, net: next.net });
      evidence.push({ scenario, stats, overview, initial, metrics, after, graph, networkRates, interfaceDOM, detail, detailDOM, detailValues, detailInterfaces });
    }
    currentCase = 'same_dom_transition'; phase = 'network';
    await fixtureCall('case:numeric');
    await page.goto(fixture.owner + '/#/network', { waitUntil: 'networkidle' });
    const rowSelector = '#layout-network .if-row';
    await page.waitForSelector(rowSelector);
    await page.clock.fastForward(1000);
    const rxNode = await page.locator(rowSelector + ' [data-ref^="rx"]').elementHandle();
    const transitionDOM = () => page.locator(rowSelector).evaluate(row => ({
      rx: row.querySelector('[data-ref^="rx"]').textContent,
      tx: row.querySelector('[data-ref^="tx"]').textContent,
      utilizationHidden: row.querySelector('[data-ref^="uw"]').hidden,
    }));
    const beforeTransition = await transitionDOM();
    check('same DOM transition starts with measured real interface rates',
      beforeTransition.rx === shownRate(900000), beforeTransition);
    const publishTransition = async operation => {
      await fixtureCall(operation);
      const response = page.waitForResponse(value => value.url() === fixture.owner + '/api/overview');
      await page.clock.fastForward(15100); await response;
      await page.waitForTimeout(50);
      await page.clock.fastForward(1000);
    };
    await publishTransition('rx_unknown');
    const missing = await transitionDOM();
    check('same DOM numeric-to-unknown cancels the native zero tween permanently',
      missing.rx === '--' && missing.tx === shownRate(300000) && missing.utilizationHidden
      && await rxNode.evaluate(node => node === document.querySelector('#layout-network .if-row [data-ref^="rx"]')), missing);
    await page.clock.fastForward(1000);
    check('unknown interface remains unknown after the old numeric animation deadline',
      (await transitionDOM()).rx === '--', await transitionDOM());
    await publishTransition('rx_restore');
    const restored = await transitionDOM();
    check('same DOM recovered measurement restores the real rate and native link-availability state',
      restored.rx === shownRate(900000) && restored.tx === shownRate(300000)
      && restored.utilizationHidden === beforeTransition.utilizationHidden
      && await rxNode.evaluate(node => node === document.querySelector('#layout-network .if-row [data-ref^="rx"]')), restored);
    evidence.push({ scenario: currentCase, beforeTransition, missing, restored });
    const final = await fixtureCall('stats');
    check('no page errors, external requests, collection/control writes, hardware/statvfs or subprocess I/O',
      errors.length === 0 && external.length === 0 && blockedWrites.length === 0 && final.forbidden.length === 0,
      { errors, external, blockedWrites, forbidden: final.forbidden });
    fs.writeFileSync(path.join(fixtureDir, 'network-evidence.json'),
      JSON.stringify({ checks, evidence, drawInputs, requests, final }, null, 2));
    console.log(JSON.stringify({ ok: true, count: checks.length, checks, fixtureDir, evidencePath: path.join(fixtureDir, 'network-evidence.json') }, null, 2));
  } catch (error) {
    const dom = activePage && !activePage.isClosed() ? await activePage.evaluate(() => ({
      overview: !!document.querySelector('#layout-overview [data-mod="trends"]'),
      sheet: !!document.querySelector('#sheet.on'),
      chart: !!document.querySelector('#layout-overview [data-ref="ch-net"]'),
    })).catch(() => null) : null;
    console.error(JSON.stringify({ ok: false, checks, fixtureDir, currentCase, phase,
      error: String(error && error.stack || error), dom, errors, external, blockedWrites, stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
    if (browser) await browser.close();
    child.stdin.end();
    await new Promise(resolve => {
      if (child.exitCode !== null) return resolve();
      const deadline = setTimeout(() => { child.kill(); resolve(); }, 10000);
      child.once('exit', () => { clearTimeout(deadline); resolve(); });
    });
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
