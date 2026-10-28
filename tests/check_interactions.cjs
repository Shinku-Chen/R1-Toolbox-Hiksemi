// Fresh headless browser + intercepted fixture APIs only. No NAS service is contacted.
const { chromium } = require('playwright-core');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {pathToFileURL}=require('node:url');
const uiURL=pathToFileURL(path.resolve(__dirname,'../ui/index.html')).href;
const screenshotDir=process.env.R1_UI_SCREENSHOT_DIR;
if(screenshotDir)fs.mkdirSync(screenshotDir,{recursive:true});
const chromeCandidates=[process.env.CHROME_BIN,
 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
 '/usr/bin/google-chrome','/usr/bin/google-chrome-stable','/usr/bin/chromium','/usr/bin/chromium-browser',
 ...String(process.env.PATH||'').split(path.delimiter).flatMap(folder=>['google-chrome','google-chrome-stable','chromium','chromium-browser'].map(name=>path.join(folder,name)))];
const chromeBin=chromeCandidates.find(candidate=>candidate&&fs.existsSync(candidate));
assert.ok(chromeBin,'未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件');
(async () => {
  const browser = await chromium.launch({ executablePath: chromeBin, headless: true });
  const checks = [], sizes = [], pageErrors = [], posts = [], external = [];
  let offline = false, failNextAction = false, failNextCancel = false, delayAction = false, historyFail = false, pairingExpires = 180;
  let independentPolicy = null, lightPolicy = null;
  const monitoring = { available: true, settings: { thresholds: { cpu_temp_warn: 70, cpu_temp_critical: 80 } }, power: { available: true, cpu_package_w: 12.5 }, disk_io: [{ name: 'sda', read_bytes_per_sec: 1048576, write_bytes_per_sec: 0 }], summary: { status: 'ok', label: '已采集指标未见告警', known_metrics: 12, unknown_metrics: 2 } };
  const historyFixture = { available: true, range: '1h', series: [
    { id: 'cpu_usage_percent', points: [[Date.now() / 1000 - 1800, 20], [Date.now() / 1000 - 900, 35], [Date.now() / 1000, 18]] },
    { id: 'memory_usage_percent', points: [[Date.now() / 1000 - 1800, 18], [Date.now() / 1000 - 900, 19], [Date.now() / 1000, 18.75]] },
    { id: 'cpu_temperature_c', points: [[Date.now() / 1000 - 1800, 43], [Date.now() / 1000, 46]] },
    { id: 'rx_bytes_per_sec', points: [[Date.now() / 1000 - 1800, 2048], [Date.now() / 1000, 4096]] },
    { id: 'disk_read_bytes_per_sec', points: [[Date.now() / 1000 - 1800, 1048576], [Date.now() / 1000, 2097152]] },
    { id: 'cpu_package_w', points: [[Date.now() / 1000 - 1800, 11], [Date.now() / 1000, 12.5]] },
  ] };
  const webConfig = { enabled: true, host: '0.0.0.0', port: 18732, scheme: 'http', addresses: ['http://192.168.1.10:18732'], pairing: { active: false, expires_in: 0 } };
  const settings = { theme: 'dark', font_scale: 100, page_interval: 0, sleep_timeout: 0, brightness: 80, night_enabled: false, night_start: '22:00', night_end: '07:00', night_brightness: 20, enabled_pages: ['overview', 'storage', 'network', 'controls', 'apps', 'system', 'settings'] };
  const state = {
    capabilities: { screen: { available: true, reason: '' }, fans: { available: false, reason: '缺少可控制的风扇通道' }, rgb: { available: false, reason: '接口尚未验证' }, power: { available: true, reason: '' } },
    settings, screen: { brightness: 55, asleep: false, max_brightness: 96000 },
    fans: { channels: [], official: { control_available: false, cpu_rpm: null, system_rpm: null, mode: 'auto' } },
    rgb: { bus: null, mode: 1, color: '#82ddc2', brightness: 60, availableBuses: [0, 4] }, power: { pending: null, executing: false, last_error: null },
  };
  const insights = {
    schema_version: 1, timestamp: new Date().toISOString(),
    system: { model: 'HIKSEMI R1', cpu_model: 'Intel N100', cpu_logical_cores: 4, load_average: [.34, .52, .6], process_count: 142, memory: { used_bytes: 3e9, available_bytes: 13e9, total_bytes: 16e9, usage_percent: 18.75, swap_used_bytes: 0, swap_total_bytes: 0 }, cpu_cores: [0, 1, 2, 3].map(id => ({ id, usage_percent: 16 + id })) },
    device: { identity: { hostname: 'R1-NAS', os: 'Linux fixture', firmware: 'fixture-firmware', kernel: '6.1-fixture', dmi: { model: 'R1' } }, cpu: { architecture: 'x86_64', physical_cores: 4, sockets: 1 }, thermal: [{ id: 'coretemp:0', driver: 'coretemp', label: 'Package', celsius: 46, critical_celsius: 100 }], fans: [{ id: 'hwmon0:fan1', label: 'fan1', rpm: 1200 }], network: [{ name: 'eth0', mtu: 1500, speed_mbps: 2500, duplex: 'full', ipv6_addresses: [{ address: '2001:db8:1234:5678:90ab:cdef:1234:5678', prefix_length: 64, scope: 'global' }], address_status: { ipv6: 'ok' }, default_gateways: { ipv4: [{ gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' } }], network_config: { default_routes: { ipv4: [{ interface: 'eth0', gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ interface: 'eth0', gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' }, dns: { servers: ['127.0.0.53', '2001:4860:4860::8888'], upstream_servers: ['192.168.1.1', '2001:4860:4860::8888'], stub_servers: ['127.0.0.53'], search: ['home.arpa', 'fixture.local'], domains: ['home.arpa'], status: 'partial', reason: '部分解析器状态文件不可读取' } }, storage: { devices: [{ name: 'sda', model: 'Test disk', serial: 'FIXTURE-ONLY', vendor: 'Fixture', state: 'running' }], raid: [] }, capabilities: {} },
    interfaces: [{ name: 'eth0', ipv4: ['192.168.1.10'], mac: '00:11:22:33:44:55', operstate: 'up', rx_bytes: 3e9, tx_bytes: 1e9, rx_bytes_per_sec: 4096, tx_bytes_per_sec: 1024 }],
    disks: { available: true, reason: '', items: [{ id: 'sda', device: '/dev/sda', model: 'Test disk', size_bytes: 4e12, temp_c: 38, health: { passed: true }, standby: false }] },
    containers: { available: true, reason: '', items: [{ id: 'abcdef123456', name: 'test-db', image: 'postgres:17', state: 'running', status: 'Up 2 hours', cpu_percent: 125, memory_bytes: 125829120, ports: '0.0.0.0:5432->5432/tcp', protected: false, available_actions: ['stop', 'restart'] }, { id: '123456abcdef', name: 'r1-toolbox', image: 'ghcr.io/shinku-chen/r1-toolbox:preview', state: 'exited', protected: true, available_actions: [] }] },
    vms: { available: false, reason: '宿主未提供 libvirt 服务', items: [] }, gpu: { available: false, reason: '没有可用 GPU 采样', devices: [] },
    ports: [{ protocol: 'tcp', address: '127.0.0.1', port: 18731 }], processes: [{ pid: 900, name: 'hiksemi-sub', cpu_percent: 120, memory_bytes: 2e8 }],
    history: [10, 20, 15, 40, 28].map(cpu => ({ cpu_usage_percent: cpu, memory_usage_percent: 35 })),
  };
  const status = { schema_version: 1, available: true, timestamp: new Date().toISOString(), hostname: 'R1-NAS', uptime_seconds: 281940, cpu: { usage_percent: 18, temp_c: 46 }, memory: { used_bytes: 3e9, total_bytes: 16e9, usage_percent: 18.75 }, network: { interface: 'eth0', ipv4: '192.168.1.10', rx_bytes_per_sec: 4096, tx_bytes_per_sec: 1024 }, disks: [{ mount: '/drives/data', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25 }] };
  const headers = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };
  try {
    const context = await browser.newContext({ viewport: { width: 376, height: 960 }, deviceScaleFactor: 1, locale: 'zh-CN', hasTouch: true });
    await context.addInitScript(() => {
      const nativeTimeout = window.setTimeout;
      window._fixtureContainerTimeouts = [];
      window.setTimeout = function (callback, delay, ...args) { if (delay === 60000) window._fixtureContainerTimeouts.push(delay); return nativeTimeout.call(this, callback, delay, ...args); };
      window.require = name => name === 'process' ? { env: { R1_TOOLBOX_CONTROL_TOKEN: 'fixture-only-not-real' } } : { ipcRenderer: { once: (_n, cb) => { window._fixtureAck = cb; }, send: n => { if (n === 'showWin' && window._fixtureAck) window._fixtureAck(); } } };
    });
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url.startsWith('file:')) return route.continue();
      external.push(url); return route.abort('blockedbyclient');
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.route('http://127.0.0.1:18731/api/**', async route => {
      if (offline) return route.abort('connectionrefused');
      const req = route.request(), endpoint = new URL(req.url()).pathname;
      if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST', 'Access-Control-Allow-Headers': 'Content-Type, X-R1-Token' } });
      let data;
      if (endpoint.endsWith('/action')) {
        assert.equal(req.headers()['x-r1-token'], 'fixture-only-not-real');
        const body = req.postDataJSON(); posts.push(body);
        if (failNextAction && body.action === 'screen.brightness') { failNextAction = false; return route.fulfill({ status: 409, headers, body: JSON.stringify({ ok: false, reason: '模拟亮度调整被拒绝' }) }); }
        if (failNextCancel && body.action === 'power.cancel') { failNextCancel = false; return route.fulfill({ status: 409, headers, body: JSON.stringify({ ok: false, reason: '模拟取消失败' }) }); }
        if (body.action === 'screen.brightness') { if (delayAction) { delayAction = false; await new Promise(r => setTimeout(r, 600)); } state.screen.brightness = body.value; }
        if (body.action === 'screen.sleep') state.screen.asleep = true;
        if (body.action === 'screen.wake') state.screen.asleep = false;
        if (body.action === 'settings.update') Object.assign(settings, body.settings);
        if (body.action === 'power.request') { assert.equal(body.confirmed, true); state.power.pending = { operation: body.operation, seconds_remaining: 5 }; }
        if (body.action === 'power.cancel') state.power.pending = null;
        if (body.action === 'container.action') { assert.equal(body.confirmed, true); insights.containers.items[0].state = body.operation === 'stop' ? 'exited' : 'running'; insights.containers.items[0].available_actions = body.operation === 'stop' ? ['start'] : ['stop', 'restart']; }
        if (body.action === 'vm.action') { assert.equal(body.confirmed, true); insights.vms.items[0].state = body.operation === 'shutdown' ? 'shut off' : 'running'; }
        if (body.action === 'web.pairing') { assert.equal(body.rotate, true); webConfig.pairing = { active: true, expires_in: pairingExpires }; data = { ok: true, web: { code: '12345', expires_in: pairingExpires, port: webConfig.port, addresses: webConfig.addresses, http: true } }; }
        else data = { ok: true, state, insights };
      } else if (endpoint.endsWith('/history') && historyFail) return route.fulfill({ status: 503, headers, body: JSON.stringify({ error: '历史服务不可用' }) });
      else data = endpoint.endsWith('/history') ? historyFixture : endpoint.endsWith('/status') ? status : endpoint.endsWith('/toolbox') ? state : endpoint.endsWith('/web-config') ? webConfig : endpoint.endsWith('/fan-policy') ? independentPolicy : endpoint.endsWith('/light-policy') ? lightPolicy : endpoint.endsWith('/monitoring') ? monitoring : insights;
      return route.fulfill({ status: 200, headers, body: JSON.stringify(data) });
    });

    const root = async name => { await page.locator(`[data-tab="${name}"]`).click(); await page.waitForTimeout(160); };
    const title = async () => page.locator('.screen-title').textContent();
    const open = async view => { await page.locator(`[data-view="${view}"]`).first().click(); await page.waitForTimeout(220); };
    const back = async () => page.locator('[data-action="back"]').click();
    const shot = async name => {
      if (!screenshotDir) return;
      await page.locator('#toast').waitFor({ state: 'hidden', timeout: 7500 }).catch(() => {});
      await page.screenshot({ path: path.join(screenshotDir, name + '.png') });
    };
    const measure = async name => {
      const value = await page.evaluate(() => {
        const area = document.getElementById('screen-body');
        return { width: innerWidth, body: document.body.scrollWidth, scroll: area.scrollWidth, client: area.clientWidth };
      });
      sizes.push({ name, ...value });
      assert.ok(value.body <= value.width && value.scroll <= value.client + 1, 'horizontal overflow ' + JSON.stringify({ name, ...value }));
    };

    await page.goto(uiURL);
    await page.waitForFunction(() => document.querySelector('.badge')?.textContent === '本机在线' && document.querySelector('.status-title')?.textContent.includes('正常'));
    assert.match(await page.locator('.clock-digits svg').getAttribute('viewBox'), /^0 0 /);
    assert.ok(await page.locator('.clock-digits circle').count() > 0);
    assert.match(await page.locator('.metric-row').first().textContent(), /CPU/);
    assert.match(await page.locator('.inline-summary').textContent(), /25/);
    await shot('overview');
    checks.push('real status feeds dot clock, status card and CPU/memory/storage/network summary');

    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const name of ['overview', 'storage', 'network', 'apps', 'controls']) { await root(name); await measure(name); }
    }
    const style = await page.evaluate(() => {
      const nav = Array.from(document.querySelectorAll('.nav-button')).map(node => ({ width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height, font: parseFloat(getComputedStyle(node).fontSize) }));
      return { nav, body: parseFloat(getComputedStyle(document.body).fontSize), title: parseFloat(getComputedStyle(document.querySelector('.screen-title')).fontSize) };
    });
    assert.equal(style.nav.length, 5);
    assert.ok(style.nav.every(item => item.height >= 56 && item.width >= 52 && item.font >= 14), JSON.stringify(style.nav));
    assert.ok(style.body >= 20 && style.title >= 20, JSON.stringify(style));
    checks.push('five touch tabs at 376/320 without overflow; 56px+ targets and 20px body text');

    await root('overview');
    await open('metrics');
    assert.equal(await title(), '核心指标');
    assert.equal(await page.locator('.screen-body .card-button').count(), 6, 'CPU, memory, storage, network, temperature and RAPL power');
    await page.locator('.screen-header [data-view="controls"]').click();
    assert.equal(await title(), '控制中心');
    await page.locator('.screen-header [data-action="back"]').click();
    assert.equal(await title(), '核心指标', 'control centre opened from a detail returns to it');
    await open('processor');
    assert.equal(await title(), '处理器与内存');
    assert.ok(await page.locator('.core-row').count() >= 4);
    await back();
    await back();
    await root('overview');
    await open('trends');
    await page.waitForFunction(() => document.querySelectorAll('.chart-card').length === 6, null, { timeout: 8000 });
    assert.equal(await page.locator('.chart-card').count(), 6);
    assert.equal(await page.locator('[data-range="6h"]').getAttribute('aria-pressed'), 'false');
    await page.locator('[data-range="6h"]').click();
    await page.waitForTimeout(300);
    assert.equal(await page.locator('[data-range="6h"]').getAttribute('aria-pressed'), 'true');
    historyFail = true;
    await page.locator('[data-range="24h"]').click();
    await page.waitForFunction(() => document.querySelector('.detail-lead')?.textContent.includes('历史数据暂不可用'));
    historyFail = false;
    await shot('trends');
    await back();
    await open('thermal');
    assert.equal(await title(), '温度与风扇');
    assert.match(await page.locator('.screen-body').textContent(), /Package/);
    await back();
    await root('storage');
    assert.equal(await page.locator('.drive-row').count(), 1);
    assert.match(await page.locator('.screen-body').textContent(), /\/drives\/data/);
    await root('network');
    assert.match(await page.locator('.screen-body').textContent(), /192\.168\.1\.10/);
    assert.match(await page.locator('.screen-body').textContent(), /2500 Mbps/);
    await open('ports');
    assert.match(await page.locator('.screen-body').textContent(), /18731/);
    await back();
    checks.push('metrics, processor, real history charts, thermal sensors, storage mounts and network/ports details');

    await root('apps');
    assert.equal(await page.locator('.app-summary .total').textContent(), '2');
    assert.match(await page.locator('.app-count-meta').textContent(), /1 个已停止/);
    assert.equal(await page.locator('.app-card').count(), 2);
    await page.locator('.app-card').first().click();
    assert.equal(await title(), '容器详情');
    assert.equal(await page.locator('[data-action="container-act"]').count(), 2);
    await page.locator('[data-action="container-act"][data-operation="stop"]').click();
    await page.locator('#modal [data-action="modal-accept"]').click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('[data-action="container-act"]')).some(node => node.textContent === '启动'));
    assert.ok(posts.some(item => item.action === 'container.action' && item.operation === 'stop' && item.confirmed === true));
    await back();
    await page.locator('.app-card').nth(1).click();
    assert.equal(await page.locator('[data-action="container-act"]').count(), 0, 'protected container has no lifecycle buttons');
    await back();
    insights.containers.items[0].available_actions = ['stop', 'restart'];
    insights.containers.items[0].state = 'running';
    insights.vms = { available: true, reason: '', items: [{ id: 'vm-1', name: 'fixture-vm', state: 'running' }] };
    await page.waitForFunction(() => Array.from(document.querySelectorAll('.menu-row')).some(node => node.textContent.includes('虚拟机')));
    await page.locator('.menu-row').filter({ hasText: '虚拟机' }).click();
    assert.equal(await title(), '虚拟机');
    assert.equal(await page.locator('[data-action="vm-act"]').count(), 2);
    await page.locator('[data-action="vm-act"][data-operation="shutdown"]').click();
    await page.locator('#modal [data-action="modal-accept"]').click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('[data-action="vm-act"]')).some(node => node.textContent === '启动'));
    assert.ok(posts.some(item => item.action === 'vm.action' && item.operation === 'shutdown' && item.confirmed === true));
    await back();
    checks.push('container list/detail, confirmed stop, protected container, VM start/shutdown from real insights');

    await root('controls');
    await page.locator('#brightness').evaluate(node => { node.value = '66'; node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.waitForFunction(() => document.getElementById('toast').textContent === '亮度已调整');
    assert.equal(state.screen.brightness, 66);
    failNextAction = true;
    await page.locator('#brightness').evaluate(node => { node.value = '70'; node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.waitForFunction(() => document.getElementById('toast').textContent === '模拟亮度调整被拒绝');
    assert.equal(state.screen.brightness, 66);
    await page.locator('[data-action="sleep"]').click();
    await page.waitForFunction(() => !document.getElementById('sleep').hidden);
    await shot('sleep');
    await page.locator('#sleep').dispatchEvent('click');
    await page.waitForFunction(() => document.getElementById('sleep').hidden);
    await page.waitForTimeout(700);
    state.screen.asleep = true;
    await page.waitForFunction(() => !document.getElementById('sleep').hidden);
    state.screen.asleep = false;
    await page.waitForFunction(() => document.getElementById('sleep').hidden);
    checks.push('brightness writes real value, failure keeps server value, sleep/wake with first-touch wake and external state');

    await page.locator('[data-action="reboot"]').click();
    assert.equal(posts.filter(item => item.action === 'power.request').length, 0);
    await page.locator('#modal [data-action="modal-cancel"]').click();
    await page.locator('[data-action="reboot"]').click();
    await page.locator('#modal [data-action="modal-accept"]').click();
    await page.waitForFunction(() => !document.getElementById('power-status').hidden);
    assert.equal(state.power.pending.operation, 'reboot');
    await root('overview');
    assert.equal(await page.locator('#power-status').isVisible(), true);
    failNextCancel = true;
    await page.locator('#power-status [data-action="cancel-power"]').click();
    await page.waitForFunction(() => document.getElementById('toast').textContent === '模拟取消失败');
    assert.equal(await page.locator('#power-status').isVisible(), true, 'failed cancellation keeps the countdown cancellable');
    await page.locator('#power-status [data-action="cancel-power"]').click();
    await page.waitForFunction(() => document.getElementById('power-status').hidden);
    assert.equal(state.power.pending, null);
    checks.push('power needs explicit confirmation, survives navigation, and stays cancellable');

    await root('controls');
    await page.locator('.menu-row').filter({ hasText: '网页设置' }).click();
    assert.equal(await title(), '网页设置');
    assert.match(await page.locator('.web-address').textContent(), /192\.168\.1\.10/);
    await page.locator('[data-action="pairing"]').click();
    await page.waitForFunction(() => document.querySelector('.pairing-code')?.textContent === '12345');
    await back();
    assert.equal(await page.locator('.pairing-code').count(), 0, 'leaving the web view clears the code');
    await page.locator('.menu-row').filter({ hasText: '网页设置' }).click();
    assert.equal(await page.locator('.pairing-code').count(), 0);
    await back();
    checks.push('web address and protected pairing code; code is cleared on leave');

    offline = true;
    await page.waitForFunction(() => document.querySelector('.badge')?.textContent === '离线');
    assert.equal(await page.locator('#brightness').isDisabled(), true);
    assert.equal(await page.locator('[data-action="reboot"]').isDisabled(), true);
    offline = false;
    await page.waitForFunction(() => document.querySelector('.badge')?.textContent === '本机在线');
    checks.push('offline disables writes and recovers');

    const ro = await browser.newContext({ viewport: { width: 376, height: 960 } });
    const rp = await ro.newPage(); let readOnlyPosts = 0;
    rp.on('pageerror', error => pageErrors.push(error.message));
    await rp.route('http://127.0.0.1:18731/api/**', route => {
      if (route.request().method() === 'POST') readOnlyPosts++;
      const path = new URL(route.request().url()).pathname;
      return route.fulfill({ status: 200, headers, body: JSON.stringify(path.endsWith('/toolbox') ? state : path.endsWith('/insights') ? insights : path.endsWith('/history') ? historyFixture : path.endsWith('/monitoring') ? monitoring : status) });
    });
    await rp.goto(uiURL);
    await rp.waitForFunction(() => document.querySelector('.badge')?.textContent === '本机在线');
    await rp.locator('[data-tab="controls"]').click();
    assert.equal(await rp.locator('#brightness').isDisabled(), true);
    assert.equal(readOnlyPosts, 0);
    await ro.close();
    checks.push('ordinary browser is read-only and never emits action POSTs');

    assert.equal(pageErrors.length, 0, pageErrors.join(';'));
    assert.equal(external.length, 0, external.join(';'));
    console.log(JSON.stringify({ passed: true, checks, sizes, pageErrors, externalRequests: 0, postActions: posts.map(item => item.action) }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
