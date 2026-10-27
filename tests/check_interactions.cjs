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
const blockedRequests=[];
async function fixtureOnly(context){
 await context.route('**/*',route=>{
  const url=route.request().url();
  if(url.startsWith('file:'))return route.continue();
  // Page-level fixture API handlers run first; everything else is denied.
  blockedRequests.push(url);return route.abort('blockedbyclient');
 });
}
(async () => {
  const browser = await chromium.launch({ executablePath: chromeBin, headless: true });
  const checks = [], sizes = [], pageErrors = [], posts = [];
  let offline = false, failNextAction = false, delayAction = false, pairingExpires = 180;
  let independentPolicy = null, lightPolicy = null;
  const monitoring = {available:true,summary:{status:'ok',label:'已采集指标未见告警',known_metrics:12,unknown_metrics:2}};
  const webConfig = { enabled: true, host: '0.0.0.0', port: 18732, scheme: 'http', addresses: ['http://192.168.1.10:18732'], pairing: { active: false, expires_in: 0 } };
  const settings = { theme: 'dark', font_scale: 100, page_interval: 0, sleep_timeout: 0, brightness: 80, night_enabled: false, night_start: '22:00', night_end: '07:00', night_brightness: 20, enabled_pages: ['overview', 'storage', 'network', 'controls', 'apps', 'system', 'settings'] };
  const state = {
    capabilities: { screen: { available: true, reason: '' }, fans: { available: false, reason: '缺少可控制的风扇通道' }, rgb: { available: false, reason: '接口尚未验证' }, power: { available: true, reason: '' } },
    settings, screen: { brightness: 55, asleep: false, max_brightness: 96000 },
    fans: { channels: [], official: { control_available: false, cpu_rpm: null, system_rpm: null, mode: 'auto' } },
    rgb: { bus: null, mode: null, color: null, availableBuses: [0, 4] }, power: { pending: null, executing: false, last_error: null },
  };
  const insights = {
    schema_version: 1, timestamp: new Date().toISOString(),
    system: { model: 'HIKSEMI R1', cpu_model: 'Intel N100', cpu_logical_cores: 4, load_average: [.34, .52, .6], process_count: 142, memory: { used_bytes: 3e9, available_bytes: 13e9, total_bytes: 16e9, usage_percent: 18.75, swap_used_bytes: 0, swap_total_bytes: 0 }, cpu_cores: [0, 1, 2, 3].map(id => ({ id, usage_percent: 16 + id })) },
    device: { identity: { hostname: 'R1-NAS', os: 'Linux fixture', firmware: 'fixture-firmware', kernel: '6.1-fixture', dmi: { model: 'R1' } }, cpu: { architecture: 'x86_64', physical_cores: 4, sockets: 1 }, thermal: [{ id: 'coretemp:0', driver: 'coretemp', label: 'Package', celsius: 46, critical_celsius: 100 }], fans: [], network: [{ name: 'eth0', mtu: 1500, speed_mbps: 2500, duplex: 'full', ipv6: ['fe80::10'], ipv6_addresses: [{ address: '2001:db8:1234:5678:90ab:cdef:1234:5678', prefix_length: 64, scope: 'global' }, { address: 'fe80::10', prefix_length: 64, scope: 'link' }], address_status: { ipv6: 'ok' }, default_gateways: { ipv4: [{ gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' } }], network_config: { default_routes: { ipv4: [{ interface: 'eth0', gateway: '192.168.1.1', metric: 100, source: '/proc/net/route' }], ipv6: [{ interface: 'eth0', gateway: 'fe80::1', metric: 200, source: '/proc/net/ipv6_route' }, { interface: 'eth0', gateway: null, metric: 300, source: '/proc/net/ipv6_route' }] }, route_status: { ipv4: 'ok', ipv6: 'ok' }, dns: { servers: ['127.0.0.53', '2001:4860:4860::8888'], upstream_servers: ['192.168.1.1', '2001:4860:4860::8888'], stub_servers: ['127.0.0.53'], search: ['home.arpa', 'fixture.local'], domains: ['home.arpa'], status: 'partial', reason: '部分解析器状态文件不可读取', sources: [{ source: 'resolv.conf', path: '/etc/resolv.conf', status: 'ok', servers: ['127.0.0.53'], search: ['home.arpa'], domains: ['home.arpa'] }, { source: 'systemd-resolved', path: '/run/systemd/resolve/resolv.conf', status: 'ok', servers: ['2001:4860:4860::8888'], search: ['fixture.local'] }] } }, storage: { devices: [{ name: 'sda', model: 'Test disk', serial: 'FIXTURE-ONLY', vendor: 'Fixture', state: 'running' }], raid: [] }, capabilities: {} },
    interfaces: [{ name: 'eth0', ipv4: ['192.168.1.10'], mac: '00:11:22:33:44:55', operstate: 'up', rx_bytes: 3e9, tx_bytes: 1e9, rx_bytes_per_sec: 4096, tx_bytes_per_sec: 1024 }],
    disks: { available: true, reason: '', items: [{ id: 'sda', device: '/dev/sda', model: 'Test disk', size_bytes: 4e12, temp_c: 38, health: { passed: true }, standby: false }] },
    containers: { available: true, reason: '', items: [{ id: 'abcdef123456', name: 'test-db', image: 'postgres:17', state: 'running', status: 'Up 2 hours', cpu_percent: 125, memory_bytes: 125829120, ports: '0.0.0.0:5432->5432/tcp', protected: false }, { id: '123456abcdef', name: 'r1-toolbox', image: 'ghcr.io/shinku-chen/r1-toolbox:preview', state: 'exited', protected: true, available_actions: [] }] },
    vms: { available: false, reason: '宿主未提供 libvirt 服务', items: [] }, gpu: { available: false, reason: '没有可用 GPU 采样', devices: [] },
    ports: [{ protocol: 'tcp', address: '127.0.0.1', port: 18731 }], processes: [{ pid: 900, name: 'hiksemi-sub', cpu_percent: 120, memory_bytes: 2e8 }],
    history: [10, 20, 15, 40, 28].map(cpu => ({ cpu_usage_percent: cpu, memory_usage_percent: 35 })),
  };
  const status = { schema_version: 1, available: true, timestamp: new Date().toISOString(), hostname: 'R1-NAS', uptime_seconds: 281940, cpu: { usage_percent: 18, temp_c: 46 }, memory: { used_bytes: 3e9, total_bytes: 16e9, usage_percent: 18.75 }, network: { interface: 'eth0', ipv4: '192.168.1.10', rx_bytes_per_sec: 4096, tx_bytes_per_sec: 1024 }, disks: [{ mount: '/drives/data', total_bytes: 4e12, used_bytes: 1e12, usage_percent: 25 }] };
  const headers = { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' };
  try {
    const context = await browser.newContext({ viewport: { width: 376, height: 960 }, deviceScaleFactor: 1, locale: 'zh-CN', hasTouch: true });
    await context.addInitScript(() => {
      const setTimeout = window.setTimeout;
      window._fixtureContainerTimeouts = [];
      window.setTimeout = function(callback, delay, ...args) { if (delay === 60000) window._fixtureContainerTimeouts.push(delay); return setTimeout.call(this, callback, delay, ...args); };
      window.require = name => name === 'process' ? { env: { R1_TOOLBOX_CONTROL_TOKEN: 'fixture-only-not-real' } } : { ipcRenderer: { once: (_name, callback) => { window._fixtureAck = callback; }, send: name => { if (name === 'showWin' && window._fixtureAck) window._fixtureAck(); } } };
    });
    await fixtureOnly(context);
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
        if (failNextAction && body.action === 'screen.brightness') { failNextAction = false; return route.fulfill({ status: 409, headers, body: JSON.stringify({ ok: false, error: 'fixture_failure', reason: '模拟亮度调整被拒绝' }) }); }
        if (body.action === 'fan.set') Object.assign(state.fans.channels[0], { owned: true, mode: body.mode, duty_percent: body.value, curve: body.curve });
        if (body.action === 'fan.restore') Object.assign(state.fans.channels[0], { owned: false, mode: 'native' });
        if (body.action === 'fan.official') { assert.equal(body.confirmed, true); assert.ok(['low', 'medium', 'high', 'auto'].includes(body.mode)); state.fans.official.mode = body.mode; }
        if (body.action === 'screen.brightness') { if (delayAction) { delayAction = false; await new Promise(resolve => setTimeout(resolve, 600)); } state.screen.brightness = body.value; }
        if (body.action === 'screen.sleep') state.screen.asleep = true;
        if (body.action === 'screen.wake') state.screen.asleep = false;
        if (body.action === 'settings.update') Object.assign(settings, body.settings);
        if (body.action === 'power.request') { assert.equal(body.confirmed, true); state.power.pending = { operation: body.operation, seconds_remaining: 5 }; }
        if (body.action === 'power.cancel') state.power.pending = null;
        if (body.action === 'container.action') { assert.equal(body.confirmed, true); insights.containers.items[0].state = body.operation === 'stop' ? 'exited' : 'running'; insights.containers.items[0].available_actions = body.operation === 'stop' ? ['start'] : ['stop','restart']; }
        if (body.action === 'rgb.probe') { assert.deepEqual(Object.keys(body),['action']); state.rgb.bus = 4; state.rgb.brightness = 75; state.capabilities.rgb.available = true; }
        if (body.action === 'rgb.set') { assert.equal(Object.hasOwn(body,'bus'),false,'RGB action must match host schema without legacy bus'); assert.equal(state.capabilities.rgb.available, true); Object.assign(state.rgb, { mode: body.mode, color: body.color, brightness: body.brightness === undefined ? state.rgb.brightness : body.brightness }); }
        if (body.action === 'web.pairing') { assert.equal(body.rotate, true); webConfig.pairing = { active: true, expires_in: pairingExpires }; data = { ok: true, web: { code: '12345', expires_in: pairingExpires, port: webConfig.port, addresses: webConfig.addresses, http: true } }; }
        else data = { ok: true, state, insights };
      } else data = endpoint.endsWith('/status') ? status : endpoint.endsWith('/toolbox') ? state : endpoint.endsWith('/web-config') ? webConfig : endpoint.endsWith('/fan-policy') ? independentPolicy : endpoint.endsWith('/light-policy') ? lightPolicy : endpoint.endsWith('/monitoring') ? monitoring : insights;
      return route.fulfill({ status: 200, headers, body: JSON.stringify(data) });
    });
    const root = async name => page.locator(`[data-page="${name}"]`).click();
    const enter = async name => page.locator(`#page-${name === 'screen' || name === 'power' || name === 'fans' ? 'controls' : name} [data-open-view="${name}"]`).click();
    const back = async () => page.locator('#detail-back').click();
    const title = async () => page.locator('#detail-title').textContent();
    const shot = async name => { if (screenshotDir) { await page.locator('#toast').waitFor({ state: 'hidden', timeout: 7500 }); const label = page.locator('#connection-label'), original = await label.textContent(); await label.evaluate(node => { node.textContent = '模拟设备数据'; }); await page.screenshot({ path: path.join(screenshotDir, name + '.png') }); await label.evaluate((node, text) => { node.textContent = text; }, original); } };
    const measure = async (name, detail = false) => {
      const metrics = await page.evaluate(detail => { const area = detail ? document.getElementById('detail-body') : document.querySelector('.page:not([hidden])'); return { width: innerWidth, body: document.body.scrollWidth, scroll: area.scrollWidth, client: area.clientWidth, height: area.scrollHeight, visible: area.clientHeight }; }, detail);
      sizes.push({ name, ...metrics }); assert.ok(metrics.body <= metrics.width && metrics.scroll <= metrics.client + 1, 'horizontal overflow ' + JSON.stringify({ name, ...metrics }));
    };
    await page.goto(uiURL);
    await page.waitForFunction(() => !document.getElementById('brightness-save').disabled);
    assert.equal(posts.filter(item => item.action === 'power.request').length, 0);
    await root('network');await page.locator('#control-center-toggle').click();assert.equal(await page.locator('#title-controls').textContent(),'控制中心');assert.equal(await page.locator('#control-center-toggle').getAttribute('aria-expanded'),'true');await enter('screen');await back();assert.equal(await page.locator('#page-controls').isVisible(),true);await page.locator('#control-center-close').click();assert.equal(await page.locator('#page-network').isVisible(),true);
    await page.locator('#interfaces-entry').click();const oldDetail=await title();await page.locator('#control-center-toggle').click();await page.locator('#control-center-close').click();assert.equal(await title(),oldDetail);await back();
    await page.locator('.masthead').dispatchEvent('pointerdown',{pointerId:11,isPrimary:true,clientX:40,clientY:30});await page.locator('.masthead').dispatchEvent('pointerup',{pointerId:11,isPrimary:true,clientX:44,clientY:130});assert.equal(await page.locator('#title-controls').textContent(),'控制中心');await page.locator('#control-center-close').dispatchEvent('pointerdown',{pointerId:12,isPrimary:true});await page.locator('#control-center-close').click();assert.equal(await page.locator('#page-network').isVisible(),true);
    await page.locator('#interfaces-entry').click();await page.locator('#control-center-toggle').click();settings.enabled_pages=settings.enabled_pages.filter(name=>name!=='network');await page.waitForFunction(()=>document.querySelector('[data-page="network"]').hidden);await page.locator('#control-center-close').click();assert.equal(await page.locator('#page-overview').isVisible(),true);assert.equal(await page.locator('#subpage').isHidden(),true);
    settings.enabled_pages=['overview','storage','network','controls','apps','system','settings'];await page.waitForFunction(()=>!document.querySelector('[data-page="network"]').hidden);await page.locator('#control-center-toggle').click();settings.enabled_pages=settings.enabled_pages.filter(name=>name!=='controls');await page.waitForFunction(()=>document.querySelector('#control-center-toggle').hidden);assert.equal(await page.locator('#page-overview').isVisible(),true);assert.equal(await page.locator('#control-center-toggle').getAttribute('aria-expanded'),'false');settings.enabled_pages=['overview','storage','network','controls','apps','system','settings'];await page.waitForFunction(()=>!document.querySelector('#control-center-toggle').hidden);
    checks.push('top shortcut and header-only downward pull reuse existing control page; detail back and close restore origin; disabling origin or control page while open falls back safely; no copied hardware controls');
    await root('overview');
    // Glyphs such as 11:11 use fewer dots than 08:08. Check a visible clock
    // and its matching accessible time, independent of the runner's minute.
    await page.locator('#clock-matrix svg').waitFor({state:'visible'});
    const clock = await page.evaluate(() => ({dots:document.querySelectorAll('#clock-matrix circle').length,header:document.getElementById('header-clock').textContent,time:document.getElementById('clock').textContent}));
    assert.ok(clock.dots>0);assert.match(clock.header,/^\d{2}:\d{2}$/);assert.equal(clock.header,clock.time);
    await page.waitForFunction(()=>document.getElementById('overview-state-headline').textContent==='已采集指标未见告警');

    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const name of settings.enabled_pages) { await root(name); await measure(name); if (['overview', 'controls', 'apps', 'settings'].includes(name)) await shot(`preview-${width}-${name}`); }
    }
    checks.push('seven root pages at 320/376 without horizontal overflow; short first-level menus');
    await page.setViewportSize({width:376,height:960});await root('overview');await shot('preview-376-reference-overview');await page.locator('#control-center-toggle').click();await measure('control-center');await shot('preview-376-control-center');await page.locator('#control-center-close').click();
    const readability = await page.evaluate(() => {
      const font = selector => parseFloat(getComputedStyle(document.querySelector(selector)).fontSize);
      const cards = Array.from(document.querySelectorAll('.resource-card')).map(node => node.getBoundingClientRect());
      const nav = Array.from(document.querySelectorAll('[data-page]')).map(node => ({ font: parseFloat(getComputedStyle(node).fontSize), width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height, bottom: node.getBoundingClientRect().bottom }));
      return { body: font('body'), primary: font('.menu-row strong'), secondary: font('.menu-row small'), detail: font('.detail-row > span'), nav, singleColumn: cards.every(card => Math.abs(card.left - cards[0].left) < 1) };
    });
    assert.ok(readability.body >= 20 && readability.primary >= 20 && readability.secondary >= 18 && readability.detail >= 20, JSON.stringify(readability));
    assert.equal(readability.singleColumn, true);
    assert.ok(readability.nav.every(item => item.font >= 20 && item.width >= 52 && item.height >= 52 && item.bottom <= 960), JSON.stringify(readability.nav));
    checks.push('default 20px body/detail, 22px entry titles, 18px descriptions; single-column metrics; all seven 52px-plus navigation targets visible');

    await root('overview');
    const cpuCard = page.locator('#page-overview [data-open-view="cpu"]');
    await cpuCard.dispatchEvent('pointerdown', { pointerId: 7, isPrimary: true, clientX: 290, clientY: 300 });
    await cpuCard.dispatchEvent('pointerup', { pointerId: 7, isPrimary: true, clientX: 50, clientY: 305 });
    assert.equal(await page.locator('#page-storage').isVisible(), true);
    await cpuCard.dispatchEvent('click');
    assert.equal(await page.locator('#subpage').isHidden(), true, 'swipe compatibility click must not open CPU detail');
    assert.equal(await page.locator('#page-storage').isVisible(), true);
    await root('overview');
    await cpuCard.dispatchEvent('pointerdown', { pointerId: 8, isPrimary: true, clientX: 290, clientY: 300 });
    await cpuCard.dispatchEvent('pointerup', { pointerId: 8, isPrimary: true, clientX: 50, clientY: 305 });
    await root('overview'); await cpuCard.click();
    assert.equal(await title(), '处理器', 'new pointer sequence must allow normal card activation');
    await back();
    checks.push('CPU-card horizontal swipe suppresses only its compatibility click; fresh pointerdown restores normal activation');
    await page.setViewportSize({ width: 376, height: 960 }); await root('controls');
    assert.equal(await page.locator('[data-control-entry="fans"]').isHidden(), true);
    await root('apps'); assert.equal(await page.locator('#vm-entry').isHidden(), true);
    await root('system'); assert.equal(await page.locator('#gpu-entry').isHidden(), true); assert.equal(await page.locator('#raid-entry').isHidden(), true); assert.equal(await page.locator('#fan-sensor-entry').isHidden(), true);
    checks.push('unavailable controls/VM/GPU/RAID/sensors are hidden');

    await root('controls'); await enter('screen'); assert.equal(await title(), '屏幕');
    assert.equal(await page.locator('#page-controls').isHidden(), true);
    await page.locator('#brightness').dispatchEvent('pointerdown', { pointerId: 9, isPrimary: true, clientX: 40, clientY: 400 });
    await page.locator('#brightness').dispatchEvent('pointerup', { pointerId: 9, isPrimary: true, clientX: 280, clientY: 400 });
    assert.equal(await title(), '屏幕', 'slider drag must not trigger back gesture');
    failNextAction = true; await page.locator('#brightness-save').click(); await page.waitForFunction(() => document.getElementById('toast').textContent === '模拟亮度调整被拒绝'); assert.equal(state.screen.brightness, 55);
    await page.waitForFunction(() => !document.getElementById('brightness-save').disabled);
    delayAction = true; await page.locator('#brightness').fill('65'); await page.locator('#brightness').dispatchEvent('input'); await page.locator('#brightness-save').click(); assert.equal(await page.locator('#action-status').isVisible(), true); await page.waitForFunction(() => document.getElementById('toast').textContent === '亮度已调整'); assert.equal(state.screen.brightness, 65);
    await measure('screen-detail', true); await shot('preview-376-screen-detail');
    checks.push('focused brightness controls; slider gesture isolation; Chinese failure reason; real action payload');

    state.capabilities.fans = { available: true, reason: '' }; state.fans.channels = [{ id: 'hwmon2:pwm3', driver: 'it87', duty_percent: 80, owned: false, mode: 'native', rpm: null, rpm_verified: false }];
    await back(); await page.waitForFunction(() => !document.querySelector('[data-control-entry="fans"]').hidden); await enter('fans'); await page.locator('#fan-list button').click();
    assert.equal(await title(), 'hwmon2:pwm3'); assert.equal(await page.locator('.fan-channel input[type=range]').getAttribute('min'), '50');
    await page.getByRole('button', { name: '自动温控', exact: true }).click(); assert.match(await page.locator('#toast').textContent(), /请先勾选/); assert.equal(posts.filter(item => item.action === 'fan.set').length, 0);
    await page.locator('.fan-channel input[type=checkbox]').check(); await page.getByRole('button', { name: '自动温控', exact: true }).click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => document.querySelector('.fan-channel').textContent.includes('已由 Toolbox 接管'));
    await page.locator('.curve-editor summary').click(); await page.getByRole('button', { name: '应用曲线', exact: true }).click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => document.getElementById('toast').textContent === '风扇控制已提交');
    const curve = posts.find(item => item.action === 'fan.set' && item.mode === 'curve'); assert.equal(curve.curve.length, 5); assert.ok(curve.curve.every(point => point.duty >= 50));
    await page.getByRole('button', { name: '恢复原控制', exact: true }).click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => document.querySelector('.fan-channel').textContent.includes('尚未接管'));
    checks.push('third-level fan detail, explicit takeover, safe curve, restore action');

    state.fans.channels = []; state.fans.official = { control_available: true, rpm_available: false, cpu_rpm: null, system_rpm: null, mode: 'auto', raw_grade: 1, reason: '转速读数不可用' };
    await back(); await page.waitForFunction(() => document.getElementById('fan-list').textContent.includes('原厂系统风扇策略')); await page.getByRole('button', { name: /原厂系统风扇策略/ }).click();
    assert.equal(await page.locator('#official-fan-modes button').count(), 4); assert.equal(await page.locator('[data-detail-view="official-fan"] input').count(), 0); assert.ok(!(await page.locator('#official-fan-readings').textContent()).includes('0 RPM'));
    await page.locator('#official-fan-modes').getByRole('button', { name: '低速', exact: true }).click(); assert.match(await page.locator('#confirm-message').textContent(), /原厂系统风扇策略.*海康配置/); assert.match(await page.locator('#confirm-message').textContent(), /CPU 风扇继续由原厂温控管理/); assert.match(await page.locator('#confirm-message').textContent(), /物理风扇对应需实机确认/); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => document.querySelector('#official-fan-modes .is-selected').textContent === '低速');
    await shot('preview-376-official-fan-detail'); checks.push('official system-fan policy only; CPU remains factory-managed; missing RPM is not zero; persistent-setting confirmation');

    await root('controls'); await enter('power'); await page.locator('#power-off').click(); assert.equal(posts.filter(item => item.action === 'power.request').length, 0); await page.locator('#confirm-cancel').click();
    await page.locator('#power-reboot').click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => !document.getElementById('global-power-notice').hidden); assert.equal(state.power.pending.operation, 'reboot');
    await back(); await page.locator('#global-power-cancel').click(); await page.waitForFunction(() => document.getElementById('global-power-notice').hidden); assert.equal(state.power.pending, null);
    checks.push('power opens only by explicit choice; countdown cancellable after leaving detail');

    await enter('screen'); await page.locator('#screen-sleep').click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => !document.getElementById('sleep-overlay').hidden);
    const previousPower = posts.filter(item => item.action === 'power.request').length;
    await page.locator('#sleep-overlay').dispatchEvent('pointerdown', { pointerId: 2, isPrimary: true, clientX: 100, clientY: 300 }); await page.waitForFunction(() => document.getElementById('sleep-overlay').hidden);
    await page.locator('#power-off').dispatchEvent('click'); assert.equal(await page.locator('#confirm-backdrop').isHidden(), true); assert.equal(posts.filter(item => item.action === 'power.request').length, previousPower); await page.waitForTimeout(700); await back();
    checks.push('first wake touch swallowed, never leaks into power confirmation');

    assert.equal(await page.locator('#page-controls [data-control-entry="rgb"]').isVisible(), false); state.rgb.bus = 4; state.rgb.brightness = 75; state.capabilities.rgb.available = true; await page.waitForFunction(() => !document.querySelector('#page-controls [data-control-entry="rgb"]').hidden); await page.locator('#page-controls [data-control-entry="rgb"]').click(); await page.waitForFunction(() => document.getElementById('detail-title').textContent === '灯光');
    assert.equal(posts.filter(item => item.action === 'rgb.set').length, 0, 'probe must not write colors'); assert.equal(await page.locator('#rgb-mode option[value="2"]').textContent(), '彩虹'); assert.equal(await page.locator('#rgb-mode option[value="3"]').textContent(), '呼吸');
    await page.locator('#rgb-mode').selectOption('3'); await page.locator('#rgb-brightness').fill('35'); await page.locator('#rgb-brightness').dispatchEvent('input'); await page.locator('#rgb-save').click(); await page.waitForFunction(() => document.getElementById('toast').textContent === '灯光设置已提交'); assert.equal(state.rgb.mode, 3); assert.equal(state.rgb.brightness, 35);
    lightPolicy = {config:{enabled:true},active:true,busy:false,runtime:{reference_c:62,source_label:'CPU Package',reason:'温度联动运行中',target:{brightness:44}}};await page.waitForFunction(()=>!document.getElementById('small-light-policy').hidden);assert.equal(await page.locator('#rgb-save').isDisabled(),true);assert.match(await page.locator('#small-light-policy-state').textContent(),/62 °C/);lightPolicy=null;await page.waitForFunction(()=>document.getElementById('small-light-policy').hidden);
    checks.push('local light-policy live state exposes reference/source/target and disables immediate RGB overrides while owned');
    checks.push('RGB controls appear only after automatic verification; protocol labels/payload match');

    await root('storage'); await page.locator('#disk-list button').first().click(); assert.match(await page.locator('#volume-detail').textContent(), /\/drives\/data/); await back(); await page.locator('#physical-disks-entry').click(); await page.locator('#physical-disks button').first().click(); assert.match(await page.locator('#disk-detail').textContent(), /FIXTURE-ONLY/); await measure('disk-detail', true); await shot('preview-376-disk-detail');
    await page.locator('#detail-body').dispatchEvent('pointerdown', { pointerId: 4, isPrimary: true, clientX: 30, clientY: 400 }); await page.locator('#detail-body').dispatchEvent('pointerup', { pointerId: 4, isPrimary: true, clientX: 280, clientY: 405 }); assert.equal(await title(), '物理磁盘'); await back(); assert.equal(await page.locator('#page-storage').isVisible(), true);
    await page.locator('#viewport').dispatchEvent('pointerdown', { pointerId: 5, isPrimary: true, clientX: 290, clientY: 400 }); await page.locator('#viewport').dispatchEvent('pointerup', { pointerId: 5, isPrimary: true, clientX: 50, clientY: 405 }); assert.equal(await page.locator('#page-network').isVisible(), true);
    await page.locator('#viewport').dispatchEvent('pointerdown', { pointerId: 6, isPrimary: true, clientX: 180, clientY: 500 }); await page.locator('#viewport').dispatchEvent('pointerup', { pointerId: 6, isPrimary: true, clientX: 185, clientY: 250 }); assert.equal(await page.locator('#page-network').isVisible(), true);
    assert.match(await page.locator('#network-ipv4-summary').textContent(), /192\.168\.1\.10/); assert.match(await page.locator('#network-ipv6-summary').textContent(), /2001:db8:/);
    await page.locator('#interfaces-entry').click(); await page.locator('#interface-list button').click();
    const interfaceText = await page.locator('#interface-detail').textContent();
    assert.match(interfaceText, /2500 Mbps/); assert.match(interfaceText, /2001:db8:1234:5678:90ab:cdef:1234:5678\/64/); assert.match(interfaceText, /scope：global/); assert.match(interfaceText, /scope：link/); assert.match(interfaceText, /IPv4 默认网关.*192\.168\.1\.1/s); assert.match(interfaceText, /IPv6 默认网关.*fe80::1/s); assert.match(interfaceText, /来源\n\/proc\/net\/route/);
    await page.locator('#interface-detail .detail-row').filter({ hasText: 'IPv6 · 地址 1' }).scrollIntoViewIfNeeded(); await measure('ipv6-interface-detail', true); await shot('preview-376-network-interface');
    await page.keyboard.press('Escape'); assert.equal(await title(), '网络接口'); await back();
    await page.locator('[data-open-view="network-routes"]').click(); assert.match(await page.locator('#network-routes-detail').textContent(), /eth0/); assert.match(await page.locator('#network-routes-detail').textContent(), /fe80::1/); assert.match(await page.locator('#network-routes-detail').textContent(), /直连路由（无下一跳网关）/); await measure('network-routes-detail', true); await back();
    await page.locator('[data-open-view="network-dns"]').click(); const dnsText = await page.locator('#network-dns-detail').textContent(); assert.match(dnsText, /2001:4860:4860::8888/); assert.match(dnsText, /home\.arpa/); assert.match(dnsText, /fixture\.local/); assert.match(dnsText, /systemd-resolved/); assert.match(dnsText, /默认域名.*home\.arpa/s); assert.ok(!dnsText.includes('路由域')); assert.match(dnsText, /部分解析器状态文件不可读取/); assert.ok(!dnsText.includes('未配置'));
    await measure('network-dns-detail', true); await shot('preview-376-network-dns'); await back();
    checks.push('IPv4/IPv6 overview and per-interface prefix/scope; dual-stack default gateways; DNS/upstream/stub/search/source and partial-read reason');
    checks.push('filesystem/disk/interface third-level details; right-swipe/back; root horizontal swipe; vertical gesture unchanged');

    await root('apps');assert.equal(await page.locator('#docker-running').textContent(),'1');assert.equal(await page.locator('#docker-total').textContent(),'2');assert.match(await page.locator('#apps-container-preview').textContent(),/5432->5432/);await measure('docker-root-cards');await shot('preview-376-docker-list');
    const pressed=page.locator('#apps-container-preview .application-card').first();await pressed.dispatchEvent('pointerdown',{pointerId:18,isPrimary:true});await pressed.evaluate(node=>{window._pressedCard=node;});const heldPoll=page.waitForResponse(response=>response.url().endsWith('/api/insights'));insights.containers.items[0].cpu_percent=126;await (await heldPoll).finished();await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));assert.equal(await pressed.evaluate(node=>window._pressedCard===node&&node.isConnected),true);await pressed.dispatchEvent('pointerup',{pointerId:18,isPrimary:true});await pressed.click();assert.match(await page.locator('#app-detail').textContent(),/test-db/);const detailOrder=await page.locator('#app-detail .detail-content').evaluate(node=>Array.from(node.children).map(n=>n.className));assert.ok(detailOrder[2].includes('app-primary-actions'),'operations must precede long resource and image details');await shot('preview-376-docker-detail');await back();
    await page.locator('#container-entry').click();await page.waitForFunction(()=>document.getElementById('container-list').textContent.includes('126.0%'));assert.match(await page.locator('#container-list').textContent(), /126.0%/); await page.getByRole('button', { name: /r1-toolbox/ }).click(); assert.equal(await page.locator('#app-detail button').count(), 0);assert.match(await page.locator('#app-detail').textContent(),/启动任务完成后停止属正常/); await back(); await page.getByRole('button', { name: /test-db/ }).click(); await page.locator('#app-detail').getByRole('button', { name: '停止', exact: true }).click(); await page.locator('#confirm-accept').click(); await page.waitForFunction(() => Array.from(document.querySelectorAll('#app-detail button')).some(button => button.textContent === '启动' && !button.disabled));
    const beforeCancelledStart=posts.filter(item=>item.action==='container.action').length;await page.locator('#app-detail').getByRole('button',{name:'启动',exact:true}).click();await page.locator('#confirm-cancel').click();assert.equal(posts.filter(item=>item.action==='container.action').length,beforeCancelledStart);
    await page.locator('#app-detail').getByRole('button',{name:'启动',exact:true}).click();await page.locator('#confirm-accept').click();await page.waitForFunction(()=>Array.from(document.querySelectorAll('#app-detail button')).some(n=>n.textContent==='重启'));
    await page.locator('#app-detail').getByRole('button',{name:'重启',exact:true}).click();await page.locator('#confirm-accept').click();await page.waitForFunction(()=>document.getElementById('action-status').hidden);
    insights.containers.items[0].available_actions=[];await page.waitForFunction(()=>document.querySelectorAll('#app-detail button').length===0);insights.containers.items[0].available_actions=['stop','restart'];await page.waitForFunction(()=>document.querySelectorAll('#app-detail button').length===2);
    await root('apps');const delayedCard=page.locator('#apps-container-preview .application-card').first();await delayedCard.dispatchEvent('pointerdown',{pointerId:29,isPrimary:true});const pendingPoll=page.waitForResponse(response=>response.url().endsWith('/api/insights'));insights.containers.items[0].cpu_percent=127;await pendingPoll;offline=true;await page.waitForFunction(()=>document.getElementById('brightness-save').disabled&&document.getElementById('apps-container-preview').hidden);await page.locator('body').dispatchEvent('pointerup',{pointerId:29,isPrimary:true});await page.waitForTimeout(120);assert.equal(await page.locator('#apps-container-preview').isHidden(),true);assert.equal(await page.locator('#apps-container-preview .application-card').count(),0);offline=false;await page.waitForFunction(()=>!document.getElementById('apps-container-preview').hidden);
    checks.push('Docker cards show status/resources/observed ports and counts; polling preserves pressed card and never replays it after offline; protected one-shot completion is normal; explicit available_actions empty means no operations; cancelled start emits no POST and start/stop/restart require confirmation');

    assert.equal(await page.evaluate(() => window._fixtureContainerTimeouts.length), posts.filter(item => item.action === 'container.action').length, 'each container operation receives one 60-second request budget without automatic retries');
    const readableContainers = insights.containers, previousVMs = insights.vms, beforeUnavailablePosts = posts.filter(item => item.action === 'container.action').length;
    await page.locator('#apps-container-preview .application-card').first().click();
    insights.containers = { ...readableContainers, available: false, reason: '模拟：读取 Docker 容器列表超时，请等待重新采集' };
    await page.waitForFunction(() => document.getElementById('app-detail').textContent.includes('读取 Docker 容器列表超时'));
    assert.equal(await page.locator('#app-detail button').count(), 0, 'unreadable containers cannot retain management buttons from stale items');
    await root('apps');
    await page.locator('#container-read-reason').waitFor({ state: 'visible' });
    assert.match(await page.locator('#container-read-reason').textContent(), /读取 Docker 容器列表超时/);
    assert.equal(await page.locator('#container-entry').isHidden(), true);
    assert.equal(await page.locator('#docker-summary').isHidden(), true);
    assert.equal(await page.locator('#docker-running').textContent(), '--');
    assert.equal(await page.locator('#docker-total').textContent(), '--');
    assert.equal(await page.locator('#container-count').textContent(), '--');
    assert.equal(await page.locator('#apps-container-preview .application-card').count(), 0);
    assert.equal(await page.locator('#container-list .application-card').count(), 0);
    await measure('docker-unreadable-reason'); await shot('preview-376-docker-unreadable-fixture');
    insights.vms = { available: true, reason: '', items: [] };
    await page.waitForFunction(() => !document.getElementById('vm-entry').hidden && document.getElementById('apps-empty').hidden);
    assert.equal(await page.locator('#container-read-reason').isVisible(), true, 'a readable VM service must not hide the Docker failure');
    await page.setViewportSize({ width: 320, height: 960 }); await measure('docker-unreadable-reason-320'); await shot('preview-320-docker-unreadable-fixture'); await page.setViewportSize({ width: 376, height: 960 });
    insights.containers = { available: true, reason: '', items: [] }; insights.vms = previousVMs;
    await page.waitForFunction(() => !document.getElementById('docker-summary').hidden && document.getElementById('docker-total').textContent === '0');
    assert.equal(await page.locator('#docker-running').textContent(), '0');
    assert.equal(await page.locator('#container-read-reason').isHidden(), true);
    assert.match(await page.locator('#docker-summary-note').textContent(), /已读取服务，尚未发现容器/);
    insights.containers = readableContainers;
    await page.waitForFunction(() => document.querySelectorAll('#apps-container-preview .application-card').length === 2);
    const heldUnavailableCard = page.locator('#apps-container-preview .application-card').first();
    await heldUnavailableCard.dispatchEvent('pointerdown', { pointerId: 39, isPrimary: true });
    const deferredResponse = page.waitForResponse(response => response.url().endsWith('/api/insights'));
    insights.containers.items[0].cpu_percent = 128;
    await (await deferredResponse).finished(); await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    insights.containers = { ...readableContainers, available: false, reason: '模拟：Docker 状态暂不可读取' };
    await page.waitForFunction(() => !document.getElementById('container-read-reason').hidden && document.getElementById('container-read-reason').textContent.includes('Docker 状态暂不可读取'));
    await page.locator('body').dispatchEvent('pointerup', { pointerId: 39, isPrimary: true });
    const rereadResponse = await page.waitForResponse(response => response.url().endsWith('/api/insights')); await rereadResponse.finished();
    assert.equal(await page.locator('#apps-container-preview .application-card').count(), 0, 'an unavailable snapshot must discard deferred readable cards');
    assert.equal(posts.filter(item => item.action === 'container.action').length, beforeUnavailablePosts, 'failed or recovering reads must not submit container actions');
    insights.containers = readableContainers;
    await page.waitForFunction(() => !document.getElementById('container-entry').hidden && document.getElementById('container-read-reason').hidden && document.querySelectorAll('#apps-container-preview .application-card').length === 2);
    checks.push('Docker unavailable reason stays visible even when VMs are readable; stale cards/actions and deferred pointer snapshots are cleared; unknown counts differ from a confirmed zero; recovery rereads without POST; each container action waits up to 60 seconds without retry');

    await root('settings'); await page.locator('#page-settings [data-open-view="web-config"]').click(); await page.waitForFunction(() => !document.getElementById('web-pairing-generate').disabled);
    assert.match(await page.locator('#web-config-addresses').textContent(), /http:\/\/192\.168\.1\.10:18732/); assert.match(await page.locator('#web-config-security').textContent(), /HTTP 未加密/); assert.equal(await page.locator('#web-config-addresses .detail-row').count(), 1); assert.ok(!(await page.locator('#web-config-addresses').textContent()).includes('http://['));
    await page.locator('#web-pairing-generate').click(); await page.waitForFunction(() => document.getElementById('web-pairing-code').textContent === '12345'); await measure('web-pairing', true); await shot('preview-376-web-pairing');
    assert.ok(!(await page.locator('#web-config-addresses').textContent()).includes('fixture-only-not-real'));
    await back(); assert.equal(await page.locator('#web-pairing-code').textContent(), '');
    pairingExpires = 5; await page.locator('#page-settings [data-open-view="web-config"]').click(); await page.waitForFunction(() => !document.getElementById('web-pairing-generate').disabled); await page.locator('#web-pairing-generate').click(); await page.waitForFunction(() => !document.getElementById('web-pairing').hidden); await page.waitForFunction(() => document.getElementById('web-pairing').hidden); assert.equal(await page.locator('#web-pairing-code').textContent(), ''); await back();
    checks.push('web configuration address and unencrypted-HTTP notice; protected explicit pairing POST; 5-digit code cleared on leave/expiry');

    await root('settings'); await page.locator('[data-section="appearance"]').click(); await page.locator('#setting-theme').selectOption('jade'); await page.locator('#setting-theme').dispatchEvent('input'); await page.locator('#setting-font').fill('120'); await page.locator('#setting-font').dispatchEvent('input'); await page.locator('#settings-save').click(); await page.waitForFunction(() => document.documentElement.dataset.theme === 'jade'); assert.equal(settings.font_scale, 120);
    await page.locator('#setting-font').fill('140'); await page.locator('#setting-font').dispatchEvent('input'); await page.locator('#settings-save').click(); await page.waitForFunction(() => document.documentElement.style.getPropertyValue('--ui-scale') === '1.4');
    await page.setViewportSize({ width: 320, height: 960 });
    for (const name of settings.enabled_pages) { await root(name); await measure(name + '-font140'); }
    await root('overview');await shot('preview-320-reference-overview-font140');await root('apps');await shot('preview-320-docker-font140');await page.locator('#control-center-toggle').click();await measure('control-center-font140');await shot('preview-320-control-center-font140');await page.locator('#control-center-close').click();
    await root('network'); await page.locator('#interfaces-entry').click(); await page.locator('#interface-list button').click(); await page.locator('#interface-detail .detail-row').filter({ hasText: 'IPv6 · 地址 1' }).scrollIntoViewIfNeeded(); await measure('ipv6-font140', true); await shot('preview-320-ipv6-font140'); await back(); await back();
    await page.locator('[data-open-view="network-dns"]').click(); await measure('dns-font140', true); await shot('preview-320-dns-font140'); await back();
    await root('controls'); await enter('screen'); await measure('screen-font140', true); await back(); await enter('fans'); await page.getByRole('button', { name: /原厂系统风扇策略/ }).click(); await measure('official-fan-font140', true); await shot('preview-320-official-fan-font140');
    await root('settings'); await page.locator('[data-section="pages"]').click(); await measure('settings-pages-font140', true);
    for (const checkbox of await page.locator('[data-page-setting]').all()) await checkbox.uncheck(); await page.locator('#settings-save').click(); assert.match(await page.locator('#toast').textContent(), /至少启用一个页面/);
    await page.locator('[data-page-setting="overview"]').check(); await page.locator('#settings-save').click(); await page.waitForFunction(() => document.getElementById('settings-result').textContent.includes('已保存'));
    assert.equal(await page.locator('[data-page="settings"]').isHidden(), false, 'settings must remain reachable');
    await root('settings'); await page.locator('[data-section="schedule"]').click(); await page.locator('#setting-night').check(); await page.locator('#setting-night-start').fill('22:30'); await page.locator('#setting-night-end').fill('06:45'); await page.locator('#setting-night-brightness').fill('15'); await page.locator('#setting-night-brightness').dispatchEvent('input'); await page.locator('#settings-save').click(); await page.waitForFunction(() => document.getElementById('settings-result').textContent.includes('已保存')); assert.equal(settings.night_brightness, 15);
    checks.push('settings grouped into focused views; server-authoritative theme/font; at least one page; settings stays reachable; night schedule');

    // Restore root visibility in the fixture, then verify theme variants without network/hardware effects.
    settings.enabled_pages = ['overview', 'storage', 'network', 'controls', 'apps', 'system', 'settings'];
    for (const theme of ['dark', 'light', 'ink', 'jade']) { settings.theme = theme; await page.waitForFunction(theme => document.documentElement.dataset.theme === theme, theme); await root('overview'); await measure(theme + '-font140'); }
    checks.push('four themes and 140% text at 320 pixels remain within width');
    state.capabilities.fans.available = false; state.fans.official.control_available = false; state.fans.official.policy_only = true; state.fans.official.grade = 2; state.fans.official.reason = '策略可读取；硬件调速尚未验证';
    await page.waitForFunction(() => document.querySelector('[data-control-entry="fans"]').hidden);
    await root('system'); await page.locator('#page-system [data-open-view="identity"]').click(); assert.match(await page.locator('#device-identity-extra').textContent(), /硬件调速尚未验证/); assert.match(await page.locator('#device-identity-extra').textContent(), /低速/); assert.match(await page.locator('#device-identity-extra').textContent(), /2（档位，不是转速）/);
    checks.push('policy-only official fan stays read-only and disappears from control menu');
    independentPolicy = {config:{schema_version:1,channels:{cpu:{mode:'temperature',sources:['cpu','board'],quiet_below:42,normal_at:54,full_at:71,quiet_percent:58,normal_percent:78,manual_percent:76},system:{mode:'temperature',sources:['disk','disk2'],quiet_below:33,normal_at:44,full_at:56,quiet_percent:52,normal_percent:68,manual_percent:66}}},active_on_boot:true,pending_changes:false,active:true,busy:false,capability:{available:true,channels:['cpu','system'],rpm_available:false},channels:{cpu:{mode:'temperature',active:true,reference_c:66,source_id:'board',source_label:'主板温度',level:'normal',target_percent:78,rpm:null,reason:'所选有效温度取最高'},system:{mode:'temperature',active:true,reference_c:47,source_id:'disk',source_label:'硬盘 A',level:'normal',target_percent:68,rpm:null,reason:'所选硬盘温度取最高'}},sources:[{id:'cpu',label:'CPU Package',kind:'cpu',temperature_c:62,status:'ok',age_seconds:1,fresh:true},{id:'board',label:'主板温度',kind:'board',temperature_c:66,status:'ok',age_seconds:2,fresh:true},{id:'disk',label:'硬盘 A',kind:'disk',temperature_c:47,status:'ok',age_seconds:20,fresh:true},{id:'disk2',label:'硬盘 B',kind:'disk',temperature_c:39,status:'ok',age_seconds:20,fresh:true}]};
    settings.font_scale=100;settings.theme='dark';await page.setViewportSize({width:376,height:960});await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--ui-scale')==='1' && !document.getElementById('independent-fans').hidden);
    await root('controls');await enter('fans');assert.equal(await page.locator('#independent-fan-list button').count(),2);const separateText=await page.locator('#independent-fan-list').textContent();assert.match(separateText,/CPU 风扇/);assert.match(separateText,/系统风扇/);assert.match(separateText,/66 °C/);assert.match(separateText,/47 °C/);assert.match(separateText,/78%/);assert.match(separateText,/68%/);assert.match(separateText,/RPM 未知/);assert.ok(!separateText.includes('0 RPM'));
    await measure('independent-fans',true);await shot('preview-376-independent-fans');await page.locator('#independent-fan-list button').first().click();assert.match(await page.locator('#fan-policy-channel-detail').textContent(),/主板温度/);assert.match(await page.locator('#fan-policy-channel-detail').textContent(),/2 秒前/);assert.match(await page.locator('#fan-policy-channel-detail').textContent(),/不是 RPM/);await back();
    settings.font_scale=140;await page.setViewportSize({width:320,height:960});await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--ui-scale')==='1.4');await measure('independent-fans-320-font140',true);await shot('preview-320-independent-fans-font140');
    checks.push('large-screen CPU/system cards show independent max winners, freshness and requested outputs; missing RPM stays unknown; per-channel detail and 320/140% layout remain usable');
    independentPolicy.active=false;independentPolicy.active_on_boot=false;independentPolicy.last_error='接管失败，原厂恢复未确认';for(const state of Object.values(independentPolicy.channels))Object.assign(state,{mode:'vendor',active:false,level:'vendor',reference_c:null,source_id:null,source_label:null,target_percent:null,reason:'接管失败，原厂恢复未确认；请检查监督服务'});
    await page.waitForFunction(()=>document.querySelectorAll('#independent-fan-list strong').length===2&&Array.from(document.querySelectorAll('#independent-fan-list strong')).every(node=>node.textContent.includes('恢复未确认')));assert.ok(!(await page.locator('#independent-fan-list').textContent()).includes('输出由原厂管理'));assert.equal(await page.locator('#fan-list button').count(),0);
    await page.locator('#independent-fan-list button').first().click();assert.match(await page.locator('#fan-policy-channel-detail').textContent(),/原厂恢复未确认/);
    offline = true; await page.waitForFunction(() => document.getElementById('brightness-save').disabled && document.getElementById('connection-label').textContent === '数据暂不可用'); assert.equal(await page.locator('#settings-save').isDisabled(), true); assert.equal(await page.locator('#power-off').isDisabled(), true); assert.equal(await page.locator('[data-control-entry="fans"]').isHidden(), true);
    assert.match(await page.locator('#fan-policy-channel-detail').textContent(), /实时风扇状态暂不可用/); checks.push('offline removes stale controls and fan reference values and disables all actions');

    const ro = await browser.newContext({ viewport: { width: 376, height: 960 } }); await fixtureOnly(ro); const rp = await ro.newPage(); let readOnlyPosts = 0;
    rp.on('pageerror', error => pageErrors.push(error.message));
    await rp.route('http://127.0.0.1:18731/api/**', route => { if (route.request().method() === 'POST') readOnlyPosts++; return route.fulfill({ status: 200, headers, body: JSON.stringify(route.request().url().endsWith('/toolbox') ? state : route.request().url().endsWith('/insights') ? insights : status) }); });
    await rp.goto(uiURL); await rp.waitForTimeout(300); assert.equal(await rp.locator('#brightness-save').isDisabled(), true); assert.match(await rp.locator('#control-access').textContent(), /只读/); assert.equal(readOnlyPosts, 0);
    checks.push('ordinary browser is read-only, never emits action POSTs');
    const storageValues = await page.evaluate(() => [...Object.keys(sessionStorage).map(key => sessionStorage.getItem(key)), ...Object.keys(localStorage).map(key => localStorage.getItem(key))].join('|'));
    assert.ok(!storageValues.includes('fixture-only-not-real')); assert.ok(!(await page.content()).includes('fixture-only-not-real')); assert.equal(blockedRequests.length, 0, 'unexpected network ' + blockedRequests.join(';')); assert.equal(pageErrors.length, 0, pageErrors.join(';'));
    console.log(JSON.stringify({ passed: true, checks, sizes, pageErrors, externalRequests: 0, postActions: posts.map(item => item.action) }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
