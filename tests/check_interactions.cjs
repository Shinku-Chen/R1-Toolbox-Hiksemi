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
  let offline = false, failNextAction = false, failNextCancel = false, delayAction = false, lightDelay = false, historyFail = false, pairingExpires = 180;
  const apiReads = [];
  const failedAPIs = new Set();
  let freezeStatusStamp = false, freezeDeviceStamp = false;
  let delayInsightsOnce = 0;
  let statusAgeSeconds = 0;
  const diskSource = 'disk:serial:' + 'a'.repeat(24);
  const standbySource = 'disk:serial:' + 'b'.repeat(24);
  const staleSource = 'disk:serial:' + 'c'.repeat(24);
  const missingSource = 'disk:serial:' + 'd'.repeat(24);
  const saverSources = [
    { id: diskSource, label: '家庭照片与文档备份硬盘温度', kind: 'disk', temperature_c: 33, status: 'ok', fresh: true },
    { id: standbySource, label: '休眠归档盘', kind: 'disk', temperature_c: 90, status: 'standby', fresh: true },
    { id: staleSource, label: '已过期硬盘', kind: 'disk', temperature_c: 99, status: 'ok', fresh: false },
    ...Array.from({ length: 12 }, (_, i) => ({ id: 'hwmon:' + String(i).padStart(24, '0'), label: '主板测点 ' + i, kind: 'board', temperature_c: i, status: 'ok', fresh: true })),
  ];
  const fanConfig = { schema_version: 2, channels: {
    cpu: { mode: 'vendor', sources: [], low_temp: 55, high_temp: 70, quiet_percent: 55, normal_percent: 75, manual_percent: 70 },
    system: { mode: 'vendor', sources: [], low_temp: 45, high_temp: 55, quiet_percent: 55, normal_percent: 75, manual_percent: 70 },
  } };
  let independentPolicy = { config: fanConfig, active: false, busy: false, capability: { available: true, reason: '' }, channels: {
    cpu: { active: false, target_percent: null, reference_c: null }, system: { active: false, target_percent: null, reference_c: null },
  }, official: { mode: 'high', control_available: true }, recovery_confirmed: true, sources: saverSources };
  const lightConfig = { schema_version: 1, enabled: false, mode: 'manual', sources: [], stops: [], brightness: 70,
    manual: { mode: 1, color: '#00C832', brightness: 70 }, schedule: { enabled: false, start: '22:00', end: '07:00', brightness: 15 } };
  let lightPolicy = { config: lightConfig, active: false, busy: false, capability: { available: true, reason: '' },
    target: { mode: 1, color: '#00C832', brightness: 70 }, runtime: {} };
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
    capabilities: { screen: { available: true, reason: '' }, fans: { available: true, reason: '' }, rgb: { available: true, reason: '' }, power: { available: true, reason: '' } },
    settings, screen: { brightness: 55, asleep: false, max_brightness: 96000 },
    fans: { channels: [], official: { control_available: false, cpu_rpm: null, system_rpm: null, mode: 'auto' } },
    rgb: { bus: null, mode: 1, color: '#82ddc2', brightness: 60, availableBuses: [0, 4] }, power: { pending: null, executing: false, last_error: null },
  };
  const insights = {
    schema_version: 1, timestamp: new Date().toISOString(),
    system: { model: 'HIKSEMI R1', cpu_model: 'Intel N100', cpu_logical_cores: 4, load_average: [.34, .52, .6], process_count: 142, memory: { used_bytes: 3e9, available_bytes: 13e9, total_bytes: 16e9, usage_percent: 18.75, swap_used_bytes: 0, swap_total_bytes: 0 }, cpu_cores: [0, 1, 2, 3].map(id => ({ id, usage_percent: 16 + id })) },
    device: { identity: { name: '\u6d4b\u8bd5\u8bbe\u5907\u6635\u79f0', hostname: 'R1-NAS', os: 'Linux fixture', firmware: 'fixture-firmware', kernel: '6.1-fixture', dmi: { model: 'R1' } }, cpu: { architecture: 'x86_64', physical_cores: 4, sockets: 1 }, thermal: [{ id: 'coretemp:0', driver: 'coretemp', label: 'Package', celsius: 46, critical_celsius: 100 }], fans: [{ id: 'hwmon0:fan1', label: 'fan1', rpm: 1200 }], network: [{ name: 'eth0', mtu: 1500, speed_mbps: 2500, duplex: 'full', ipv6_addresses: [{ address: '2001:db8:1234:5678:90ab:cdef:1234:5678', prefix_length: 64, scope: 'global' }], address_status: { ipv6: 'ok' }, default_gateways: { ipv4: [{ gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' } }], network_config: { default_routes: { ipv4: [{ interface: 'eth0', gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ interface: 'eth0', gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' }, dns: { servers: ['127.0.0.53', '2001:4860:4860::8888'], upstream_servers: ['192.168.1.1', '2001:4860:4860::8888'], stub_servers: ['127.0.0.53'], search: ['home.arpa', 'fixture.local'], domains: ['home.arpa'], status: 'partial', reason: '部分解析器状态文件不可读取' } }, storage: { devices: [{ name: 'sda', model: 'Test disk', serial: 'FIXTURE-ONLY', vendor: 'Fixture', state: 'running' }], raid: [] }, capabilities: {} },
    interfaces: [{ name: 'eth0', ipv4: ['192.168.1.10'], mac: '00:11:22:33:44:55', operstate: 'up', rx_bytes: 3e9, tx_bytes: 1e9, rx_bytes_per_sec: 4096, tx_bytes_per_sec: 1024 }],
    disks: { available: true, reason: '', items: [{ id: 'sda', user_name: '家庭照片与备份盘', device: '/dev/sda', model: 'Test disk', size_bytes: 4e12, temp_c: 38, health: { passed: true }, standby: false, smart_available: true }, { id: 'sdb', device: '/dev/sdb', model: 'No SMART disk', size_bytes: 1e12, temp_c: null, health: { passed: null }, standby: false, smart_available: false }, { id: 'sdc', display_name: '休眠盘暂无温度', temp_c: null, standby: true, smart_available: false }, { id: 'sdd', user_name: '超长用户自定义硬盘名称家庭影音文档备份共享数据资料归档', temp_c: 34, standby: true, smart_available: false }] },
    containers: { available: true, reason: '', items: [{ id: 'abcdef123456', name: 'test-db', image: 'postgres:17', state: 'running', status: 'Up 2 hours', cpu_percent: 125, memory_bytes: 125829120, ports: '0.0.0.0:5432->5432/tcp', protected: false, available_actions: ['stop', 'restart'] }, { id: '123456abcdef', name: 'r1-toolbox', image: 'ghcr.io/shinku-chen/r1-toolbox:preview', state: 'exited', protected: true, available_actions: [] }, ...Array.from({ length: 6 }, (_, i) => ({ id: 'fixture-extra-' + i, name: 'extra-container-' + i, image: 'fixture/image:latest', state: 'running', cpu_percent: null, memory_bytes: null, protected: false, available_actions: [] }))] },
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
      const nativeNow = Date.now.bind(Date);
      window._fixtureTimeOffset = 0;
      Date.now = () => nativeNow() + window._fixtureTimeOffset;
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
      if (offline || failedAPIs.has(new URL(route.request().url()).pathname)) return route.abort('connectionrefused');
      const req = route.request(), endpoint = new URL(req.url()).pathname;
      if (req.method() === 'GET') apiReads.push(endpoint);
      if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST', 'Access-Control-Allow-Headers': 'Content-Type, X-R1-Token' } });
      if (endpoint.endsWith('/insights') && delayInsightsOnce) {
        const delay = delayInsightsOnce; delayInsightsOnce = 0;
        await new Promise(resolve => setTimeout(resolve, delay));
      }
      if (endpoint.endsWith('/status') && !freezeStatusStamp) status.timestamp = new Date(Date.now() - statusAgeSeconds * 1000).toISOString();
      if (endpoint.endsWith('/insights')) {
        insights.timestamp = new Date().toISOString();
        if (!freezeDeviceStamp) insights.device.sampled_at = new Date().toISOString();
      }
      let data;
      if (endpoint.endsWith('/action')) {
        assert.equal(req.headers()['x-r1-token'], 'fixture-only-not-real');
        const body = req.postDataJSON(); posts.push(body);
        if (failNextAction && body.action === 'screen.brightness') { failNextAction = false; return route.fulfill({ status: 409, headers, body: JSON.stringify({ ok: false, reason: '模拟亮度调整被拒绝' }) }); }
        if (failNextCancel && body.action === 'power.cancel') { failNextCancel = false; return route.fulfill({ status: 409, headers, body: JSON.stringify({ ok: false, reason: '模拟取消失败' }) }); }
        if (body.action === 'screen.brightness') { if (delayAction) { delayAction = false; await new Promise(r => setTimeout(r, 600)); } state.screen.brightness = body.value; }
        if (body.action === 'screen.sleep') state.screen.asleep = true;
        if (body.action === 'screen.wake') state.screen.asleep = false;
        if (body.action === 'screen.activity') state.screen.saver = false;
        if (body.action === 'settings.update') Object.assign(settings, body.settings);
        if (body.action === 'power.request') { assert.equal(body.confirmed, true); state.power.pending = { operation: body.operation, seconds_remaining: 5 }; }
        if (body.action === 'power.cancel') state.power.pending = null;
        if (body.action === 'container.action') { assert.equal(body.confirmed, true); insights.containers.items[0].state = body.operation === 'stop' ? 'exited' : 'running'; insights.containers.items[0].available_actions = body.operation === 'stop' ? ['start'] : ['stop', 'restart']; }
        if (body.action === 'vm.action') { assert.equal(body.confirmed, true); insights.vms.items[0].state = body.operation === 'shutdown' ? 'shut off' : 'running'; }
        if (body.action === 'web.pairing') { assert.equal(body.rotate, true); webConfig.pairing = { active: true, expires_in: pairingExpires }; data = { ok: true, web: { code: '12345', expires_in: pairingExpires, port: webConfig.port, addresses: webConfig.addresses, http: true } }; }
        else if (body.action === 'fan.policy.quick') {
          assert.ok(['cpu', 'system'].includes(body.channel));
          const channel = fanConfig.channels[body.channel];
          if (body.channel === 'system' && ['auto', 'low', 'medium', 'high'].includes(body.mode)) {
            channel.mode = 'vendor'; channel.vendor_mode = body.mode;
            independentPolicy.official.mode = body.mode;
          } else if (body.mode === 'auto') channel.mode = 'vendor';
          else if (body.mode === 'manual') { assert.ok(channel.sources.length); channel.mode = 'temperature'; delete channel.vendor_mode; }
          else { channel.mode = 'manual'; channel.manual_percent = { low: 50, medium: 75, high: 100 }[body.mode]; }
          independentPolicy = { ...independentPolicy, config: fanConfig, channels: {
            ...independentPolicy.channels, [body.channel]: { active: channel.mode !== 'vendor', target_percent: channel.mode === 'vendor' ? null : channel.manual_percent, reference_c: null },
          } };
          independentPolicy.active = Object.values(independentPolicy.channels).some(item => item.active);
          data = { ok: true, fan_policy: independentPolicy };
        } else if (body.action === 'light.policy.quick') {
          assert.ok(Number.isInteger(body.hue) && Number.isInteger(body.brightness) && Number.isInteger(body.effect));
          if (lightDelay) { lightDelay = false; await new Promise(resolve => setTimeout(resolve, 500)); }
          lightConfig.enabled = true; lightConfig.mode = 'manual'; lightConfig.brightness = body.brightness;
          lightConfig.manual = { mode: body.effect, color: '#00FF00', brightness: body.brightness };
          lightPolicy = { ...lightPolicy, active: true, config: lightConfig, target: lightConfig.manual };
          data = { ok: true, light_policy: lightPolicy };
        }
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
    await page.waitForFunction(() => document.querySelector('.clock-live')?.textContent.includes('LOCAL HOST') && document.querySelector('.status-title')?.textContent.includes('正常'));
    assert.match(await page.locator('.clock-digits svg').getAttribute('viewBox'), /^0 0 /);
    assert.ok(await page.locator('.clock-digits circle').count() > 0);
    assert.match(await page.locator('.clock-device').textContent(), /测试设备昵称/, 'show the vendor display name');
    assert.match(await page.locator('.metric-row').first().textContent(), /CPU/);
    assert.match(await page.locator('.inline-summary').textContent(), /25/);
    assert.equal(await page.locator('.screen-header').count(), 0, 'main pages have no top header');
    await shot('overview');
    await page.locator('.status-card').click();
    await page.waitForTimeout(200);
    assert.ok(posts.some(item => item.action === 'screen.activity'), 'a touch must reset the host idle timer');
    checks.push('real status feeds dot clock, status card and CPU/memory/storage/network summary without a top header');

    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const name of ['overview', 'storage', 'network', 'apps', 'thermal', 'controls']) {
        await root(name); await measure(name);
        if (width === 320 && name === 'thermal') {
          for (const channel of ['cpu', 'system']) {
            const buttons = await page.locator(`[data-action="fan-quick"][data-channel="${channel}"]`).evaluateAll(nodes =>
              nodes.map(node => ({ mode: node.dataset.mode, width: node.getBoundingClientRect().width })));
            assert.equal(buttons.length, 5, `${channel} must expose all five quick modes at 320px`);
            assert.ok(buttons.every(button => button.width >= 56),
              `${channel} fan buttons must be at least 56px wide at 320px: ${JSON.stringify(buttons)}`);
          }
        }
      }
    }
    checks.push('CPU and system fan five-mode buttons are each at least 56px wide at 320px, without rounding tolerance');
    await page.setViewportSize({ width: 376, height: 960 });
    const style = await page.evaluate(() => {
      const nav = Array.from(document.querySelectorAll('.nav-button')).map(node => ({ width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height, font: parseFloat(getComputedStyle(node).fontSize) }));
      return { nav, body: parseFloat(getComputedStyle(document.body).fontSize), title: parseFloat(getComputedStyle(document.querySelector('.screen-title')).fontSize) };
    });
    assert.equal(style.nav.length, 6);
    assert.ok(style.nav.every(item => item.height >= 56 && item.width >= 52 && item.font >= 17), JSON.stringify(style.nav));
    assert.ok(style.body >= 22 && style.title >= 20, JSON.stringify(style));
    const webSymbols = fs.readFileSync(path.resolve(__dirname, '../ui/panel-src/20-icons.svg'), 'utf8');
    for (const [tab, name] of Object.entries({ overview: 'home', storage: 'storage', network: 'net', apps: 'box', thermal: 'temp', controls: 'gear' })) {
      const expected = webSymbols.match(new RegExp('<symbol id="i-' + name + '"[^>]*>(.*?)</symbol>'))[1];
      const actual = await page.locator(`[data-tab="${tab}"] svg`).evaluate(svg => svg.innerHTML);
      const canonical = html => html.replace(/\s*\/?>/g, '>').replace(/<\/(path|circle|ellipse)>/g, '');
      assert.equal(canonical(actual), canonical(expected), `${tab} must use the web navigation icon`);
    }
    checks.push('six touch tabs at 376/320 without overflow; 56px+ targets and larger touch typography');

    await root('overview');
    await open('metrics');
    assert.equal(await title(), '核心指标');
    assert.equal(await page.locator('.screen-body .card-button').count(), 5, 'CPU, memory, storage, network and temperature');
    await open('processor');
    assert.equal(await title(), '处理器与内存');
    assert.ok(await page.locator('.core-row').count() >= 4);
    await back();
    await back();
    await root('overview');
    await open('trends');
    await page.waitForFunction(() => document.querySelectorAll('.chart-card').length === 5, null, { timeout: 8000 });
    assert.equal(await page.locator('.chart-card').count(), 5);
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
    await root('thermal');
    assert.equal(await title(), '温度与风扇');
    assert.match(await page.locator('.screen-body').textContent(), /Package/);
    assert.equal(await page.locator('.fan-quick-row').count(), 2);
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="high"]').getAttribute('aria-pressed'), 'true', 'legacy vendor config selects the actual native high mode');
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]').getAttribute('aria-pressed'), 'false', 'native high must never appear as automatic');
    assert.match(await page.locator('.fan-quick-row').nth(1).textContent(), /原厂高档/);
    assert.equal(await page.locator('.disk-temperature-row').count(), 4, 'every physical disk has a temperature row');
    assert.match(await page.locator('[data-disk="sda"]').textContent(), /家庭照片与备份盘.*38°/);
    assert.match(await page.locator('[data-disk="sdb"]').textContent(), /sdb.*--.*温度暂不可读/);
    assert.match(await page.locator('[data-disk="sdc"]').textContent(), /--.*休眠中 · 暂无缓存读数/);
    assert.match(await page.locator('[data-disk="sdd"]').textContent(), /34°.*休眠中 · 已有缓存读数/);
    assert.equal(await page.locator('[data-disk="sdc"] .bar, [data-disk="sdd"] .bar').count(), 0, 'sleeping disk caches are not graphed as current readings');
    assert.equal(apiReads.filter(endpoint => /smart|disk-temperature/.test(endpoint)).length, 0, 'opening temperature uses insights cache only');
    await shot('thermal');
    await root('storage');
    assert.equal(await page.locator('.drive-row').count(), 4, 'all physical disks are listed, including SMART-less ones');
    const storageText = await page.locator('.screen-body').textContent();
    assert.match(storageText, /SMART/);
    assert.doesNotMatch(storageText, /\/drives\/data/, 'filesystem mounts are not shown as physical disks');
    const measuredDisks = status.disks;
    status.disks = [{ mount: '/drives/data', total_bytes: null, used_bytes: null, usage_percent: null }];
    await page.waitForTimeout(2200);
    await root('storage');
    const unknownCapacity = await page.locator('.screen-body').textContent();
    assert.match(unknownCapacity, /-- \/ --/);
    assert.doesNotMatch(unknownCapacity, /0 B \/ 0 B/);
    status.disks = measuredDisks;
    await root('network');
    assert.match(await page.locator('.screen-body').textContent(), /192\.168\.1\.10/);
    assert.match(await page.locator('.screen-body').textContent(), /2500 Mbps/);
    await open('ports');
    assert.match(await page.locator('.screen-body').textContent(), /18731/);
    await back();
    checks.push('metrics, processor, history charts, temperature tab, SMART physical disks and network/ports details');

    await root('apps');
    assert.equal(await page.locator('.app-summary .total').textContent(), '8');
    assert.match(await page.locator('.app-count-meta').textContent(), /1 个已停止/);
    assert.equal(await page.locator('.app-card').count(), 8, 'the application page directly lists every container');
    assert.equal(await page.locator('[data-view="containers"]').count(), 0, 'there is no second-level all-containers entry');
    await shot('apps');
    await page.locator('.app-card').last().scrollIntoViewIfNeeded();
    const appsOffset = await page.locator('#screen-body').evaluate(node => node.scrollTop);
    assert.ok(appsOffset > 0, 'the complete list scrolls vertically');
    assert.match(await page.locator('.app-card').last().textContent(), /extra-container-5/);
    await page.locator('.app-card').last().click();
    assert.equal(await title(), '容器详情');
    await back();
    assert.equal(await title(), '应用', 'container details return directly to applications');
    assert.ok(Math.abs(await page.locator('#screen-body').evaluate(node => node.scrollTop) - appsOffset) <= 1, 'returning restores application list scroll');
    assert.equal(await page.locator('.menu-row').filter({ hasText: '虚拟机' }).count(), 0, 'no VM entry on a NAS without VMs');
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
    checks.push('complete container list with restored parent scroll, confirmed stop and protected container; no VM module');

    await root('thermal');
    await page.locator('[data-action="fan-quick"][data-channel="cpu"][data-mode="low"]').click();
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="cpu"][data-mode="low"]')?.getAttribute('aria-pressed') === 'true');
    assert.equal(fanConfig.channels.cpu.mode, 'manual');
    assert.equal(fanConfig.channels.cpu.manual_percent, 50);
    assert.equal(fanConfig.channels.system.mode, 'vendor', 'CPU shortcut must not apply the system draft');
    await page.locator('[data-action="fan-quick"][data-channel="cpu"][data-mode="auto"]').click();
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="cpu"][data-mode="auto"]')?.getAttribute('aria-pressed') === 'true');
    assert.equal(fanConfig.channels.cpu.mode, 'vendor');
    for (const [mode, label] of [['low', '低档'], ['medium', '中档'], ['high', '高档'], ['auto', '自动']]) {
      await page.locator(`[data-action="fan-quick"][data-channel="system"][data-mode="${mode}"]`).click();
      await page.waitForFunction(mode => document.querySelector(`[data-action="fan-quick"][data-channel="system"][data-mode="${mode}"]`)?.getAttribute('aria-pressed') === 'true', mode);
      assert.equal(fanConfig.channels.system.mode, 'vendor');
      assert.equal(fanConfig.channels.system.vendor_mode, mode, 'system shortcuts select the same native mode as the web');
      assert.equal(independentPolicy.channels.system.active, false);
      assert.match(await page.locator('.fan-quick-row').nth(1).textContent(), new RegExp('原厂' + label));
    }
    fanConfig.channels.system.mode = 'manual';
    fanConfig.channels.system.manual_percent = 50;
    independentPolicy.channels.system = { active: true, target_percent: 50 };
    independentPolicy.active = true;
    await page.waitForFunction(() => document.querySelectorAll('.fan-quick-row')[1]?.textContent.includes('旧固定目标 50%'));
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][aria-pressed="true"]').count(), 0, 'legacy independent 50% is neither native low nor a temperature curve');
    fanConfig.channels.system.mode = 'vendor';
    delete fanConfig.channels.system.vendor_mode;
    independentPolicy.channels.system = { active: false, target_percent: null };
    independentPolicy.active = false;
    independentPolicy.official.mode = null;
    await page.waitForFunction(() => document.querySelectorAll('.fan-quick-row')[1]?.textContent.includes('原厂档位未读取'));
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][aria-pressed="true"]').count(), 0, 'unknown native mode must stay unknown');
    independentPolicy.official.mode = 'low';
    fanConfig.channels.system.vendor_mode = 'high';
    await page.waitForFunction(() => document.querySelectorAll('.fan-quick-row')[1]?.textContent.includes('原厂低档'));
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="high"]').getAttribute('aria-pressed'), 'true', 'saved selection and actual running native mode remain distinct');
    independentPolicy.capability.available = false;
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="cpu"][data-mode="low"]')?.disabled);
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="cpu"][data-mode="auto"]').isEnabled(), true, 'CPU release does not need independent takeover capability');
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="low"]').isEnabled(), true, 'native system capability is independent from custom takeover');
    await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="low"]').click();
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="system"][data-mode="low"]')?.getAttribute('aria-pressed') === 'true');
    independentPolicy.capability.available = true;
    independentPolicy.official.control_available = false;
    independentPolicy.official.reason = '模拟原厂接口未通过验证';
    fanConfig.channels.system.sources = ['cpu'];
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]')?.disabled);
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="manual"]').isEnabled(), true, 'custom temperature control uses its own preflight');
    assert.match(await page.locator('.fan-control').textContent(), /模拟原厂接口未通过验证/);
    await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="manual"]').click();
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="system"][data-mode="manual"]')?.getAttribute('aria-pressed') === 'true');
    assert.equal(fanConfig.channels.system.mode, 'temperature');
    assert.equal('vendor_mode' in fanConfig.channels.system, false);
    independentPolicy.official.control_available = true;
    independentPolicy.recovery_confirmed = false;
    await page.waitForFunction(() => document.querySelectorAll('.fan-quick-row')[1]?.textContent.includes('原厂恢复待确认'));
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]').isDisabled(), true, 'native selection cannot bypass recovery confirmation');
    independentPolicy.recovery_confirmed = true;
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]')?.disabled === false);
    await page.locator('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]').click();
    await page.waitForFunction(() => document.querySelector('[data-action="fan-quick"][data-channel="system"][data-mode="auto"]')?.getAttribute('aria-pressed') === 'true');
    checks.push('system shortcuts use native modes, distinguish old fixed/unknown/draft states, and preserve separate takeover/recovery gates');
    independentPolicy = { ...independentPolicy, busy: true, last_error: null };
    await page.waitForTimeout(2100);
    assert.match(await page.locator('.fan-control').textContent(), /正在应用/);
    assert.equal(await page.locator('[data-action="fan-quick"][data-channel="cpu"][data-mode="low"]').isDisabled(), true);
    independentPolicy = { ...independentPolicy, busy: false, last_error: '模拟风扇应用失败' };
    await page.waitForTimeout(2100);
    assert.match(await page.locator('.fan-control').textContent(), /应用失败/);
    assert.match(await page.locator('.fan-control').textContent(), /模拟风扇应用失败/);
    independentPolicy = { ...independentPolicy, last_error: null };
    checks.push('temperature page owns five fan modes per channel with pending/error status');

    await root('controls');
    assert.equal(await page.locator('.fan-quick-row').count(), 0, 'settings no longer owns fan controls');
    assert.equal(await page.locator('[data-view="power"]').count(), 1, 'settings exposes power through a second-level entry');
    assert.equal(await page.locator('[data-action="reboot"]').count(), 0, 'settings must not expose power actions directly');
    assert.equal(await page.locator('[data-action="shutdown"]').count(), 0);
    assert.deepEqual(await page.locator('#light-effect option').allTextContents(), ['关闭', '常亮', '彩虹', '呼吸']);
    await page.locator('#light-hue').evaluate(node => { node.value = '120'; node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.waitForFunction(() => document.getElementById('toast').textContent === '灯光已提交，等待设备确认');
    assert.deepEqual(lightConfig.manual, { mode: 1, color: '#00FF00', brightness: 70 });
    lightDelay = true;
    await page.locator('#light-hue').evaluate(node => { node.value = '130'; node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true })); });
    await page.waitForTimeout(50);
    assert.equal(await page.locator('#light-hue').isDisabled(), true, 'controls disable while a light request is pending');
    await page.waitForFunction(() => document.getElementById('toast').textContent === '灯光已提交，等待设备确认');
    lightPolicy = { ...lightPolicy, busy: true, active: false, last_error: null };
    await page.waitForTimeout(2100);
    assert.match(await page.locator('.light-control').textContent(), /正在应用/);
    assert.equal(await page.locator('#light-hue').isDisabled(), true);
    lightPolicy = { ...lightPolicy, busy: false, active: false, last_error: '模拟应用失败' };
    await page.waitForTimeout(2100);
    assert.match(await page.locator('.light-control').textContent(), /应用失败/);
    assert.match(await page.locator('.light-control').textContent(), /模拟应用失败/);
    lightPolicy = { ...lightPolicy, active: true, last_error: null };
    await page.locator('#light-effect').selectOption('2');
    await page.waitForTimeout(250);
    assert.ok(posts.some(item => item.action === 'light.policy.quick' && item.effect === 2));
    await page.locator('#sleep-timeout').selectOption('300');
    await page.waitForTimeout(250);
    assert.equal(settings.sleep_timeout, 300);
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
    checks.push('settings light uses four native modes, sleep timeout, brightness failure and sleep/wake');

    const saverInput = key => page.locator(`input[data-saver-item="${key}"]`);
    const saverValue = key => page.locator(`#saver [data-saver-key="${key}"] [data-saver-value]`).textContent();
    const waitSaverItems = async expected => page.waitForFunction(items =>
      JSON.stringify(Array.from(document.querySelectorAll('#saver [data-saver-key]'), node => node.dataset.saverKey)) === JSON.stringify(items), expected);
    const saverMeasure = async label => {
      const dimensions = await page.locator('#saver').evaluate(node => ({
        width: innerWidth, height: innerHeight, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth,
        scrollHeight: node.scrollHeight, clientHeight: node.clientHeight,
        items: Array.from(node.querySelectorAll('.saver-metric'), item => ({
          top: item.getBoundingClientRect().top, bottom: item.getBoundingClientRect().bottom,
          font: parseFloat(getComputedStyle(item.querySelector('strong')).fontSize),
        })),
      }));
      assert.ok(dimensions.scrollWidth <= dimensions.clientWidth && dimensions.scrollHeight <= dimensions.clientHeight + 1,
        label + ' overlay overflow ' + JSON.stringify(dimensions));
      assert.ok(dimensions.items.every(item => item.top >= 0 && item.bottom <= dimensions.height && item.font >= 22), label);
      sizes.push({ name: label, ...dimensions });
    };
    await page.locator('#saver-timeout').selectOption('60');
    await page.waitForFunction(() => !!document.querySelector('.saver-picker'));
    assert.equal(await saverInput('cpu_usage').isChecked(), true);
    assert.equal(await saverInput('memory_usage').isChecked(), true);
    await saverInput('cpu_temperature').check();
    await page.waitForFunction(() => document.querySelectorAll('[data-saver-item]:checked').length === 3);
    assert.deepEqual(settings.saver_items, ['cpu_usage', 'memory_usage', 'cpu_temperature']);
    await saverInput('memory_usage').uncheck();
    await page.waitForFunction(() => document.querySelectorAll('[data-saver-item]:checked').length === 2);
    await saverInput('cpu_usage').uncheck();
    await page.waitForFunction(() => document.querySelectorAll('[data-saver-item]:checked').length === 1);
    assert.equal(await saverInput('cpu_temperature').isDisabled(), true, 'the last chosen metric cannot be removed');
    for (const source of saverSources.slice(0, 11)) {
      const expectedCount = settings.saver_items.length + 1;
      await saverInput('temperature:' + source.id).check();
      await page.waitForFunction(count => document.querySelectorAll('[data-saver-item]:checked').length === count, expectedCount);
    }
    assert.equal(settings.saver_items.length, 12);
    assert.equal(settings.saver_items[0], 'cpu_temperature');
    assert.equal(settings.saver_items[1], 'temperature:' + diskSource);
    assert.equal(await saverInput('memory_usage').isDisabled(), true, 'a thirteenth item is disabled');
    assert.equal(await page.locator('#modal').isHidden(), true, 'saver selections save immediately without confirmation');
    await saverInput('temperature:' + saverSources[10].id).uncheck();
    await page.waitForFunction(() => document.querySelectorAll('[data-saver-item]:checked').length === 11);
    assert.equal(await saverInput('memory_usage').isDisabled(), false);
    await page.setViewportSize({ width: 320, height: 960 });
    await measure('saver-picker-320');
    await saverInput('temperature:' + diskSource).scrollIntoViewIfNeeded();
    const choiceMetrics = await page.locator('.saver-choice').evaluateAll(nodes => nodes.map(node => ({
      height: node.getBoundingClientRect().height, font: parseFloat(getComputedStyle(node).fontSize),
    })));
    assert.ok(choiceMetrics.every(item => item.height >= 56 && item.font >= 22));
    await shot('saver-picker-320');
    settings.saver_items = ['temperature:offline_usage'];
    await page.waitForTimeout(2200);
    await root('controls');
    assert.equal(await saverInput('temperature:offline_usage').isChecked(), true, 'a saved source remains selected when absent from the catalog');
    assert.match(await page.locator('.saver-choice').first().textContent(), /设备离线，保留原选择/);
    state.screen.saver = true;
    await waitSaverItems(['temperature:offline_usage']);
    assert.equal(await saverValue('temperature:offline_usage'), '--');
    assert.equal(await page.locator('#saver [data-saver-key="temperature:offline_usage"] small').textContent(), '℃', 'offline source IDs ending in usage are still temperatures');

    const originalThermals = insights.device.thermal.slice();
    insights.device.thermal.push({ id: 'board:fixture', driver: 'acpitz', label: '主板环境', celsius: 0 },
      { id: 'thermal_zone0', driver: 'thermal', label: 'x86_pkg_temp', celsius: 90 });
    const originalCPU = status.cpu.usage_percent, originalMemory = status.memory.usage_percent;
    status.cpu.usage_percent = 0; status.memory.usage_percent = null;
    delete settings.saver_items;
    settings.saver_content = 'usage'; state.screen.saver = true;
    await waitSaverItems(['cpu_usage', 'memory_usage']);
    assert.equal(await saverValue('cpu_usage'), '0');
    assert.equal(await saverValue('memory_usage'), '--');
    settings.saver_content = 'temperature';
    await waitSaverItems(['cpu_temperature', 'board_temperature']);
    assert.equal(await saverValue('cpu_temperature'), '46');
    assert.equal(await saverValue('board_temperature'), '0', 'CPU thermal zones must not masquerade as mainboard temperature');
    insights.device.thermal = originalThermals.slice();
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="board_temperature"] [data-saver-value]')?.textContent === '--');
    insights.device.thermal.push({ id: 'board:fixture', driver: 'acpitz', label: '主板环境', celsius: 0 });

    const mixedSaverItems = ['cpu_usage', 'cpu_temperature', 'temperature:' + diskSource,
      'temperature:' + standbySource, 'temperature:' + staleSource, 'temperature:' + missingSource];
    settings.saver_items = mixedSaverItems;
    await waitSaverItems(mixedSaverItems.slice(0, 4));
    assert.equal(await saverValue('cpu_usage'), '0');
    assert.equal(await saverValue('temperature:' + diskSource), '33');
    assert.equal(await saverValue('temperature:' + standbySource), '--');
    assert.match(await page.locator(`#saver [data-saver-key="temperature:${standbySource}"]`).textContent(), /休眠中/);
    assert.equal(await page.locator('#saver .saver-page').textContent(), '1 / 2');
    await saverMeasure('saver-mixed-320');
    await shot('saver-mixed-320');
    await page.setViewportSize({ width: 376, height: 960 });
    await saverMeasure('saver-mixed-376');
    await shot('saver-mixed-376');
    await page.evaluate(() => { window._fixtureTimeOffset += 10000; });
    await waitSaverItems(mixedSaverItems.slice(4));
    assert.equal(await saverValue('temperature:' + staleSource), '--', 'stale 99°C is never displayed');
    assert.equal(await saverValue('temperature:' + missingSource), '--', 'a missing stable source is not replaced');
    assert.match(await page.locator(`#saver [data-saver-key="temperature:${staleSource}"]`).textContent(), /读数已过期/);
    assert.equal(await page.locator('#saver .saver-page').textContent(), '2 / 2');
    await shot('saver-second-page-376');
    await page.evaluate(() => { window._fixtureTimeOffset = 0; });
    settings.saver_items = mixedSaverItems.slice(0, 4);
    await waitSaverItems(mixedSaverItems.slice(0, 4));
    await page.waitForFunction(() => document.querySelector('#saver .saver-page')?.textContent === '1 / 1');

    failedAPIs.add('/api/fan-policy');
    await page.waitForFunction(key => document.querySelector(`#saver [data-saver-key="${key}"] [data-saver-value]`)?.textContent === '--', 'temperature:' + diskSource);
    assert.equal(await saverValue('cpu_temperature'), '46', 'other live APIs still supply their own values');
    failedAPIs.clear();
    await page.waitForFunction(key => document.querySelector(`#saver [data-saver-key="${key}"] [data-saver-value]`)?.textContent === '33', 'temperature:' + diskSource);
    saverSources[1].fresh = false;
    await page.waitForFunction(key => document.querySelector(`#saver [data-saver-key="${key}"]`)?.textContent.includes('读数已过期'), 'temperature:' + standbySource);
    assert.equal(await saverValue('temperature:' + standbySource), '--', 'an old standby observation stays unknown');
    saverSources[1].fresh = true;
    failedAPIs.add('/api/status');
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="cpu_usage"] [data-saver-value]')?.textContent === '--');
    assert.equal(await saverValue('cpu_temperature'), '--', 'failed status cannot keep a cached CPU temperature visible');
    assert.equal(await saverValue('temperature:' + diskSource), '33');
    failedAPIs.clear();
    settings.saver_items = ['board_temperature'];
    await waitSaverItems(['board_temperature']);
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="board_temperature"] [data-saver-value]')?.textContent === '0');
    failedAPIs.add('/api/insights');
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="board_temperature"] [data-saver-value]')?.textContent === '--');
    failedAPIs.clear();
    freezeDeviceStamp = true;
    insights.device.sampled_at = '2000-01-01T00:00:00Z';
    insights.device.thermal.find(row => row.id === 'board:fixture').celsius = 95;
    status.cpu.temp_c = null;
    settings.saver_items = ['board_temperature', 'cpu_temperature'];
    await waitSaverItems(settings.saver_items);
    await page.waitForFunction(() => Array.from(document.querySelectorAll('#saver [data-saver-value]')).every(node => node.textContent === '--'));
    assert.equal(await saverValue('board_temperature'), '--', 'a fresh insights envelope cannot refresh its old device block');
    assert.equal(await saverValue('cpu_temperature'), '--', 'CPU fallback must not borrow an old device sample');
    freezeDeviceStamp = false;
    insights.device.thermal.find(row => row.id === 'board:fixture').celsius = 0;
    status.cpu.temp_c = 46;
    freezeStatusStamp = true;
    status.timestamp = '2000-01-01T00:00:00Z';
    settings.saver_items = ['cpu_usage', 'memory_usage', 'cpu_temperature'];
    await waitSaverItems(settings.saver_items);
    await page.waitForFunction(() => Array.from(document.querySelectorAll('#saver [data-saver-value]')).every(node => node.textContent === '--'));
    assert.equal(await saverValue('cpu_usage'), '--', 'HTTP 200 cannot make an old status sample current');
    freezeStatusStamp = false;
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="cpu_usage"] [data-saver-value]')?.textContent === '0');
    const earlySource = 'hwmon:' + 'f'.repeat(24);
    independentPolicy.sources = [{ id: earlySource, label: '先返回的 3 秒温度样本', kind: 'board',
      temperature_c: 77, status: 'ok', fresh: true, age_seconds: 0, max_age_seconds: 3 }];
    settings.saver_items = ['temperature:' + earlySource, 'cpu_usage'];
    statusAgeSeconds = 4;
    delayInsightsOnce = 3500;
    await waitSaverItems(settings.saver_items);
    assert.equal(await saverValue('temperature:' + earlySource), '--', 'waiting for another API cannot extend an early 3-second source TTL');
    assert.equal(await saverValue('cpu_usage'), '0', 'fixed timestamp age does not count the request wait twice');
    statusAgeSeconds = 0;

    const longSources = Array.from({ length: 4 }, (_, i) => ({
      id: 'hwmon:' + 'a'.repeat(249) + i, label: '超长用户自定义硬盘名称与主板温度测点'.repeat(16).slice(0, 256),
      kind: 'disk', temperature_c: i, status: 'ok', fresh: true,
    }));
    independentPolicy.sources = longSources;
    settings.saver_items = longSources.map(source => 'temperature:' + source.id);
    await waitSaverItems(settings.saver_items);
    assert.ok((await page.locator('#saver .saver-metric').first().getAttribute('aria-label')).length === 256);
    await saverMeasure('saver-long-names-376');
    await shot('saver-long-names-376');
    await page.setViewportSize({ width: 320, height: 960 });
    await saverMeasure('saver-long-names-320');
    await shot('saver-long-names-320');
    await page.setViewportSize({ width: 320, height: 540 });
    await waitSaverItems(settings.saver_items.slice(0, 2));
    await saverMeasure('saver-short-screen');
    await page.setViewportSize({ width: 376, height: 960 });
    settings.saver_items = ['cpu_usage', 'memory_usage', 'cpu_temperature', 'board_temperature'];
    await waitSaverItems(settings.saver_items);
    offline = true;
    await page.waitForFunction(() => Array.from(document.querySelectorAll('#saver [data-saver-value]')).every(node => node.textContent === '--'));
    assert.match(await page.locator('#saver').textContent(), /本机服务离线/);
    offline = false;
    await page.waitForFunction(() => document.querySelector('#saver [data-saver-key="cpu_usage"] [data-saver-value]')?.textContent === '0');

    const writesBeforeWake = posts.filter(item => item.action !== 'screen.activity').length;
    await page.locator('#saver').click();
    await page.waitForFunction(() => document.getElementById('saver').hidden);
    assert.equal(await saverInput('cpu_usage').isChecked(), true, 'wake refreshes settings changed while the saver was visible');
    await saverInput('cpu_usage').dispatchEvent('click');
    assert.equal(await saverInput('cpu_usage').isChecked(), true, 'wake click-through cannot toggle a checkbox');
    await page.locator('[data-tab="thermal"]').dispatchEvent('click');
    assert.equal(await title(), '设置', 'the wake gesture does not activate controls underneath');
    assert.equal(posts.filter(item => item.action !== 'screen.activity').length, writesBeforeWake);
    await page.waitForTimeout(700);
    await root('thermal');
    assert.equal(await title(), '温度与风扇');
    state.screen.saver = true; state.screen.asleep = true;
    await page.waitForFunction(() => !document.getElementById('sleep').hidden);
    assert.equal(await page.locator('#saver').isHidden(), true, 'screen-off takes precedence over the saver');
    state.screen.saver = false; state.screen.asleep = false;
    settings.saver_timeout = 0;
    delete settings.saver_items;
    independentPolicy.sources = saverSources;
    status.cpu.usage_percent = originalCPU; status.memory.usage_percent = originalMemory;
    insights.device.thermal = originalThermals;
    await page.waitForFunction(() => document.getElementById('sleep').hidden);
    await root('controls');
    assert.equal(apiReads.filter(endpoint => /smart|disk-temperature/.test(endpoint)).length, 0, 'saver selection and rotation only use existing caches');
    checks.push('mixed ordered saver items, 1..12 immediate selections, legacy fallback, 10s pages, long-label layouts, stale/standby/offline isolation and guarded wake');

    await page.locator('[data-view="power"]').click();
    assert.equal(await title(), '电源管理');
    const powerTitle = await page.locator('.screen-title').textContent();
    assert.equal(powerTitle, '电源管理');
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
    checks.push('power is a second-level settings page with explicit confirmation and cancellable countdown');

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
    await root('overview');
    await page.waitForFunction(() => document.querySelector('.status-card .badge')?.textContent === '本机服务离线');
    await root('controls');
    assert.equal(await page.locator('#brightness').isDisabled(), true);
    await page.locator('[data-view="power"]').click();
    assert.equal(await page.locator('[data-action="reboot"]').isDisabled(), true);
    offline = false;
    await root('overview');
    await page.waitForFunction(() => document.querySelector('.clock-live')?.textContent.includes('LOCAL HOST'));
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
    await rp.waitForFunction(() => document.querySelector('.clock-live')?.textContent.includes('LOCAL HOST'));
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
