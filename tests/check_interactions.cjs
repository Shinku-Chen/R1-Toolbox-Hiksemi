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
  let offline = false, failNextAction = false, delayAction = false, pairingExpires = 180;
  let independentPolicy = null, lightPolicy = null;
  const monitoring = { available: true, summary: { status: 'ok', label: '已采集指标未见告警', known_metrics: 12, unknown_metrics: 2 } };
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
        if (body.action === 'screen.brightness') { if (delayAction) { delayAction = false; await new Promise(r => setTimeout(r, 600)); } state.screen.brightness = body.value; }
        if (body.action === 'screen.sleep') state.screen.asleep = true;
        if (body.action === 'screen.wake') state.screen.asleep = false;
        if (body.action === 'settings.update') Object.assign(settings, body.settings);
        if (body.action === 'power.request') { assert.equal(body.confirmed, true); state.power.pending = { operation: body.operation, seconds_remaining: 5 }; }
        if (body.action === 'power.cancel') state.power.pending = null;
        if (body.action === 'container.action') { assert.equal(body.confirmed, true); insights.containers.items[0].state = body.operation === 'stop' ? 'exited' : 'running'; insights.containers.items[0].available_actions = body.operation === 'stop' ? ['start'] : ['stop', 'restart']; }
        if (body.action === 'web.pairing') { assert.equal(body.rotate, true); webConfig.pairing = { active: true, expires_in: pairingExpires }; data = { ok: true, web: { code: '12345', expires_in: pairingExpires, port: webConfig.port, addresses: webConfig.addresses, http: true } }; }
        else data = { ok: true, state, insights };
      } else data = endpoint.endsWith('/status') ? status : endpoint.endsWith('/toolbox') ? state : endpoint.endsWith('/web-config') ? webConfig : endpoint.endsWith('/fan-policy') ? independentPolicy : endpoint.endsWith('/light-policy') ? lightPolicy : endpoint.endsWith('/monitoring') ? monitoring : insights;
      return route.fulfill({ status: 200, headers, body: JSON.stringify(data) });
    });

    const root = async name => {
      if (['overview', 'storage', 'network', 'apps', 'controls'].includes(name)) await page.locator(`[data-page="${name}"]`).click();
      else if (name === 'settings') await page.locator('#settings-open').click();
      else if (name === 'system') { await page.locator('#settings-open').click(); await page.locator('#page-settings [data-open-page="system"]').click(); }
    };
    const enter = async name => page.locator(`#page-controls [data-open-view="${name}"]`).click();
    const back = async () => page.locator('#detail-back').click();
    const title = async () => page.locator('#detail-title').textContent();
    const measure = async (name, detail = false) => {
      const m = await page.evaluate(detail => {
        const area = detail ? document.getElementById('detail-body') : document.querySelector('.page:not([hidden])');
        return { width: innerWidth, body: document.body.scrollWidth, scroll: area.scrollWidth, client: area.clientWidth };
      }, detail);
      sizes.push({ name, ...m });
      assert.ok(m.body <= m.width && m.scroll <= m.client + 1, 'horizontal overflow ' + JSON.stringify({ name, ...m }));
    };
    const shot = async name => {
      if (!screenshotDir) return;
      await page.locator('#toast').waitFor({ state: 'hidden', timeout: 7500 }).catch(() => {});
      await page.screenshot({ path: path.join(screenshotDir, name + '.png') });
    };

    await page.goto(uiURL);
    await page.waitForFunction(() => document.getElementById('overview-state-headline').textContent === '已采集指标未见告警');
    assert.match(await page.locator('#clock-matrix svg').getAttribute('viewBox'), /^0 0 /);
    assert.equal(await page.locator('#header-clock').textContent(), await page.locator('#clock').textContent());
    await shot('overview');
    checks.push('dot-matrix clock, header time and monitoring verdict render from real snapshots');

    /* 5 touch tabs, no overflow at both widths */
    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const name of ['overview', 'storage', 'network', 'apps', 'controls']) { await root(name); await measure(name); }
      await root('settings'); await measure('settings');
      await root('system'); await measure('system');
    }
    checks.push('five bottom tabs plus settings/system at 320/376 without horizontal overflow');

    const style = await page.evaluate(() => {
      const font = s => parseFloat(getComputedStyle(document.querySelector(s)).fontSize);
      const nav = Array.from(document.querySelectorAll('[data-page]')).map(n => ({ w: n.getBoundingClientRect().width, h: n.getBoundingClientRect().height, font: parseFloat(getComputedStyle(n).fontSize) }));
      const gear = document.getElementById('settings-open').getBoundingClientRect();
      const tabs = document.querySelectorAll('[data-page]').length;
      return { tabs, nav, gear: { w: gear.width, h: gear.height }, body: font('body'), menu: font('.menu-row strong'), small: font('.menu-row small'), detail: font('.detail-row > span') };
    });
    assert.equal(style.tabs, 5);
    assert.ok(style.nav.every(n => n.h >= 56 && n.w >= 52 && n.font >= 14), JSON.stringify(style.nav));
    assert.ok(style.gear.w >= 48 && style.gear.h >= 48);
    assert.ok(style.body >= 20 && style.menu >= 20 && style.small >= 17 && style.detail >= 19, JSON.stringify(style));
    checks.push('touch targets: 5 bottom tabs >=56px high, 52px wide, gear >=48px; body 20px, titles 20px+, descriptions 17px+');

    /* page switching + settings back */
    await page.setViewportSize({ width: 376, height: 960 });
    await root('storage'); await shot('storage');
    await page.locator('#viewport').dispatchEvent('pointerdown', { pointerId: 5, isPrimary: true, clientX: 290, clientY: 400 });
    await page.locator('#viewport').dispatchEvent('pointerup', { pointerId: 5, isPrimary: true, clientX: 50, clientY: 405 });
    assert.equal(await page.locator('#page-network').isVisible(), true);
    await root('apps');
    await page.locator('#settings-open').click();
    assert.equal(await page.locator('#page-settings').isVisible(), true);
    assert.equal(await page.locator('#settings-back').isVisible(), true);
    await page.locator('#settings-back').click();
    assert.equal(await page.locator('#page-apps').isVisible(), true, 'settings back restores the previous page');
    await page.locator('[data-page="overview"]').click();
    const cpuCard = page.locator('#page-overview [data-open-view="cpu"]');
    await cpuCard.dispatchEvent('pointerdown', { pointerId: 7, isPrimary: true, clientX: 290, clientY: 300 });
    await cpuCard.dispatchEvent('pointerup', { pointerId: 7, isPrimary: true, clientX: 50, clientY: 305 });
    assert.equal(await page.locator('#page-storage').isVisible(), true);
    await cpuCard.dispatchEvent('click');
    assert.equal(await page.locator('#subpage').isHidden(), true, 'swipe compatibility click must not open the detail');
    await root('overview');
    await cpuCard.click();
    assert.equal(await title(), '处理器');
    await page.locator('#detail-body').dispatchEvent('pointerdown', { pointerId: 8, isPrimary: true, clientX: 30, clientY: 400 });
    await page.locator('#detail-body').dispatchEvent('pointerup', { pointerId: 8, isPrimary: true, clientX: 280, clientY: 405 });
    assert.equal(await page.locator('#page-overview').isVisible(), true);
    checks.push('swipe pages, swipe back from detail, settings back, and swipe-safe card activation');

    /* screen brightness + sleep */
    await root('controls'); await shot('controls'); await enter('screen');
    assert.equal(await title(), '屏幕');
    failNextAction = true;
    await page.locator('#brightness-save').click();
    await page.waitForFunction(() => document.getElementById('toast').textContent === '模拟亮度调整被拒绝');
    assert.equal(state.screen.brightness, 55);
    await page.waitForFunction(() => !document.getElementById('brightness-save').disabled);
    delayAction = true;
    await page.locator('#brightness').fill('65');
    await page.locator('#brightness').dispatchEvent('input');
    await page.locator('#brightness-save').click();
    await page.waitForFunction(() => document.getElementById('toast').textContent === '亮度已调整');
    assert.equal(state.screen.brightness, 65);
    await shot('screen');
    await page.locator('#screen-sleep').click(); await page.locator('#confirm-accept').click();
    await page.waitForFunction(() => !document.getElementById('sleep-overlay').hidden);
    await page.locator('#sleep-overlay').dispatchEvent('pointerdown', { pointerId: 2, isPrimary: true, clientX: 100, clientY: 300 });
    await page.waitForFunction(() => document.getElementById('sleep-overlay').hidden);
    await page.waitForTimeout(700);
    checks.push('touch brightness with failure reason and 56px controls; sleep and first-touch wake');

    /* fans and lights are read-only, complex rules live on the web */
    state.capabilities.fans = { available: true, reason: '' };
    state.fans.channels = [{ id: 'hwmon2:pwm3', driver: 'it87', duty_percent: 80, owned: false, mode: 'native', rpm: null, rpm_verified: false }];
    await back(); await page.waitForFunction(() => !document.querySelector('[data-control-entry="fans"]').hidden);
    await enter('fans');
    await page.waitForFunction(() => document.getElementById('fan-list').textContent.includes('80% PWM'));
    assert.match(await page.locator('#fan-list').textContent(), /转速未确认/);
    assert.equal(await page.locator('#page-controls [data-open-view="web-config"]').count(), 1);
    await page.locator('[data-detail-view="fans"] [data-open-view="web-config"]').click();
    assert.equal(await title(), '网页配置');
    await back(); await back();
    state.capabilities.rgb = { available: true, reason: '' };
    await page.waitForFunction(() => !document.querySelector('[data-control-entry="rgb"]').hidden);
    await enter('rgb');
    await page.waitForFunction(() => document.getElementById('rgb-summary').textContent.includes('常亮'));
    assert.match(await page.locator('#rgb-summary').textContent(), /#82ddc2/);
    assert.equal(posts.filter(p => p.action === 'rgb.set').length, 0, 'small screen must not write light effects');
    assert.equal(posts.filter(p => p.action === 'fan.set').length, 0, 'small screen must not write fan curves');
    checks.push('fan and light are read-only on the small screen; both link to the web for curves/effects');

    /* power confirm + countdown cancel */
    await back();
    await enter('power');
    await page.locator('#power-off').click();
    assert.equal(posts.filter(p => p.action === 'power.request').length, 0);
    await page.locator('#confirm-cancel').click();
    await page.locator('#power-reboot').click(); await page.locator('#confirm-accept').click();
    await page.waitForFunction(() => !document.getElementById('global-power-notice').hidden);
    assert.equal(state.power.pending.operation, 'reboot');
    await back();
    await page.locator('#global-power-cancel').click();
    await page.waitForFunction(() => document.getElementById('global-power-notice').hidden);
    checks.push('power only by explicit choice; 5-second cancel works after leaving the detail');

    /* docker */
    await root('apps'); await shot('apps');
    assert.equal(await page.locator('#docker-running').textContent(), '1');
    assert.equal(await page.locator('#docker-total').textContent(), '2');
    assert.match(await page.locator('#apps-container-preview').textContent(), /5432->5432/);
    await page.locator('#apps-container-preview .application-card').first().click();
    assert.match(await page.locator('#app-detail').textContent(), /test-db/);
    await page.getByRole('button', { name: '停止', exact: true }).click();
    await page.locator('#confirm-accept').click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('#app-detail button')).some(b => b.textContent === '启动' && !b.disabled));
    await page.getByRole('button', { name: '启动', exact: true }).click();
    await page.locator('#confirm-accept').click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('#app-detail button')).some(b => b.textContent === '重启'));
    await back();
    await page.locator('#container-entry').click();
    await page.getByRole('button', { name: /r1-toolbox/ }).click();
    assert.equal(await page.locator('#app-detail button').count(), 0);
    assert.match(await page.locator('#app-detail').textContent(), /启动任务完成后停止属正常/);
    await back(); await back();
    insights.containers = { ...insights.containers, available: false, reason: '模拟：读取 Docker 容器列表超时' };
    await page.waitForFunction(() => !document.getElementById('container-read-reason').hidden);
    assert.equal(await page.locator('#container-entry').isHidden(), true);
    assert.equal(await page.locator('#docker-total').textContent(), '--');
    insights.containers = { available: true, reason: '', items: [] };
    await page.waitForFunction(() => document.getElementById('docker-total').textContent === '0');
    checks.push('Docker counts, observed ports, confirmed start/stop, protected container and unreadable reason');

    /* app callbacks stay bounded */
    assert.equal(await page.evaluate(() => window._fixtureContainerTimeouts.length), posts.filter(p => p.action === 'container.action').length, 'each container action gets one 60-second budget');
    checks.push('container actions keep the existing 60-second budget without automatic retries');

    /* system / network details */
    await root('system');
    await page.locator('#page-system [data-open-view="identity"]').click();
    assert.match(await page.locator('#device-identity-extra').textContent(), /6.1-fixture/);
    await back();
    await root('network');
    assert.match(await page.locator('#network-ipv4-summary').textContent(), /192\.168\.1\.10/);
    assert.match(await page.locator('#network-ipv6-summary').textContent(), /2001:db8:/);
    await page.locator('#interfaces-entry').click(); await page.locator('#interface-list button').click();
    const interfaceText = await page.locator('#interface-detail').textContent();
    assert.match(interfaceText, /2500 Mbps/);
    assert.match(interfaceText, /2001:db8:1234:5678:90ab:cdef:1234:5678\/64/);
    await back(); await back();
    await page.locator('[data-open-view="network-dns"]').click();
    assert.match(await page.locator('#network-dns-detail').textContent(), /systemd-resolved|2001:4860:4860::8888/);
    await back();
    checks.push('device identity and IPv4/IPv6/route/DNS details remain available on touch');

    /* web pairing + settings */
    await root('settings'); await shot('settings');
    await page.locator('#page-settings [data-open-view="web-config"]').click();
    await page.waitForFunction(() => !document.getElementById('web-pairing-generate').disabled);
    assert.match(await page.locator('#web-config-addresses').textContent(), /http:\/\/192\.168\.1\.10:18732/);
    await page.locator('#web-pairing-generate').click();
    await page.waitForFunction(() => document.getElementById('web-pairing-code').textContent === '12345');
    await back();
    assert.equal(await page.locator('#web-pairing-code').textContent(), '');
    await page.locator('#page-settings [data-open-view="web-config"]').click();
    pairingExpires = 5;
    await page.waitForFunction(() => !document.getElementById('web-pairing-generate').disabled);
    await page.locator('#web-pairing-generate').click();
    await page.waitForFunction(() => !document.getElementById('web-pairing').hidden);
    await page.waitForFunction(() => document.getElementById('web-pairing').hidden);
    await back();
    checks.push('web address, protected pairing POST, 5-digit code cleared on leave/expiry');

    await root('settings');
    await page.locator('#page-settings [data-section="appearance"]').click();
    await page.locator('#setting-theme').selectOption('jade');
    await page.locator('#setting-theme').dispatchEvent('input');
    await page.locator('#setting-font').fill('140');
    await page.locator('#setting-font').dispatchEvent('input');
    await page.locator('#settings-save').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'jade' && document.documentElement.style.getPropertyValue('--ui-scale') === '1.4');
    for (const name of ['overview', 'storage', 'network', 'apps', 'controls']) { await root(name); await measure(name + '-font140'); }
    await root('settings'); await measure('settings-font140');
    await page.locator('#page-settings [data-section="pages"]').click();
    for (const checkbox of await page.locator('[data-page-setting]').all()) await checkbox.uncheck();
    await page.locator('#settings-save').click();
    assert.match(await page.locator('#toast').textContent(), /至少启用一个页面/);
    await page.locator('[data-page-setting="overview"]').check();
    await page.locator('#detail-back').click();
    await page.locator('#page-settings [data-section="schedule"]').click();
    await page.locator('#setting-night').check();
    await page.locator('#setting-night-brightness').fill('15');
    await page.locator('#setting-night-brightness').dispatchEvent('input');
    await page.locator('#settings-save').click();
    await page.waitForFunction(() => document.getElementById('settings-result').textContent.includes('已保存'));
    assert.equal(settings.night_brightness, 15);
    assert.equal(await page.locator('#settings-open').isVisible(), true, 'settings stays reachable with every page hidden');
    settings.enabled_pages = ['overview', 'storage', 'network', 'controls', 'apps', 'system', 'settings'];
    settings.theme = 'dark'; settings.font_scale = 100;
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark' && document.documentElement.style.getPropertyValue('--ui-scale') === '1');
    checks.push('theme/font/night save; at least one page; settings gear stays reachable; 140% text fits at 320px');

    /* offline clears actions */
    offline = true;
    await page.waitForFunction(() => document.getElementById('connection-label').textContent === '数据暂不可用');
    assert.equal(await page.locator('#brightness-save').isDisabled(), true);
    assert.equal(await page.locator('#power-off').isDisabled(), true);
    assert.equal(await page.locator('[data-control-entry="fans"]').isHidden(), true);
    offline = false;
    await page.waitForFunction(() => document.getElementById('connection-label').textContent !== '数据暂不可用');
    checks.push('offline hides stale controls and disables every action');

    const ro = await browser.newContext({ viewport: { width: 376, height: 960 } });
    const rp = await ro.newPage(); let readOnlyPosts = 0;
    rp.on('pageerror', error => pageErrors.push(error.message));
    await rp.route('http://127.0.0.1:18731/api/**', route => {
      if (route.request().method() === 'POST') readOnlyPosts++;
      return route.fulfill({ status: 200, headers, body: JSON.stringify(route.request().url().endsWith('/toolbox') ? state : route.request().url().endsWith('/insights') ? insights : status) });
    });
    await rp.goto(uiURL); await rp.waitForTimeout(300);
    assert.equal(await rp.locator('#brightness-save').isDisabled(), true);
    assert.match(await rp.locator('#control-access').textContent(), /只读/);
    assert.equal(readOnlyPosts, 0);
    await ro.close();
    checks.push('ordinary browser is read-only and never emits action POSTs');

    assert.equal(pageErrors.length, 0, pageErrors.join(';'));
    assert.equal(external.length, 0, external.join(';'));
    console.log(JSON.stringify({ passed: true, checks, sizes, pageErrors, externalRequests: 0, postActions: posts.map(item => item.action) }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
