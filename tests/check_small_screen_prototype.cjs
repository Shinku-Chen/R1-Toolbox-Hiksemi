// Offline checks for the standalone design prototype. No NAS service is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright-core');

const prototypeURL = pathToFileURL(path.resolve(__dirname, '../docs/prototypes/r1-small-screen.html')).href;
const screenshotDir = process.env.R1_PROTOTYPE_SCREENSHOT_DIR;
const playwrightCache = process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'ms-playwright');
const cachedChromium = playwrightCache && fs.existsSync(playwrightCache)
  ? fs.readdirSync(playwrightCache, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && /^chromium-\d+$/.test(entry.name))
    .sort((a, b) => Number(b.name.slice('chromium-'.length)) - Number(a.name.slice('chromium-'.length)))
    .flatMap(entry => ['chrome-win64/chrome.exe', 'chrome-win/chrome.exe'].map(executable => path.join(playwrightCache, entry.name, executable)))
  : [];
const chromeCandidates = [
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ...cachedChromium,
  process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, 'Google/Chrome/Application/chrome.exe'),
  process.env['PROGRAMFILES(X86)'] && path.join(process.env['PROGRAMFILES(X86)'], 'Microsoft/Edge/Application/msedge.exe'),
  ...String(process.env.PATH || '').split(path.delimiter).flatMap(folder =>
    ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'].map(name => path.join(folder, name))),
];
const chromeBin = chromeCandidates.find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件。');
if (screenshotDir) fs.mkdirSync(screenshotDir, { recursive: true });

const mainTabs = ['overview', 'storage', 'network', 'apps', 'thermal', 'controls'];
const viewTitles = {
  overview: '概览', metrics: '核心指标', processor: '处理器与内存', storage: '存储', network: '网络',
  apps: '应用', container: '容器详情', thermal: '温度与风扇',
  trends: '历史趋势', ports: '监听端口', controls: '设置', web: '网页设置', power: '电源管理',
};
const galleryViews = ['overview', 'metrics', 'trends', 'processor', 'storage', 'thermal', 'network', 'apps', 'ports', 'controls', 'web', 'saver'];

(async () => {
  const browser = await chromium.launch({ executablePath: chromeBin, headless: true });
  const failures = [], pageErrors = [], externalRequests = [], checks = [];
  const check = async (name, fn) => {
    try {
      await fn();
      checks.push(name);
      console.log(`PASS ${name}`);
    } catch (error) {
      failures.push(name);
      console.error(`FAIL ${name}\n${error.stack || error}`);
    }
  };

  try {
    const context = await browser.newContext({
      viewport: { width: 376, height: 960 }, deviceScaleFactor: 1, locale: 'zh-CN', hasTouch: true,
      reducedMotion: 'reduce', serviceWorkers: 'block',
    });
    // Block and record unexpected connections rather than replacing them with a fixture API.
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url.startsWith('file:') || url.startsWith('data:') || url === 'about:blank') return route.continue();
      externalRequests.push({ url, method: route.request().method() });
      return route.abort('blockedbyclient');
    });
    context.on('page', opened => opened.on('pageerror', error => pageErrors.push({ url: opened.url(), error: error.message })));
    const page = await context.newPage();
    page.setDefaultTimeout(6000);
    const device = page.locator('#device');
    const inside = selector => device.locator(selector);
    const open = async (view = 'overview', mode = 'interactive') => {
      await page.goto(`${prototypeURL}?view=${view}&mode=${mode}`, { waitUntil: 'load' });
      assert.equal(new URL(page.url()).protocol, 'file:');
      assert.equal(await inside('.screen-title').textContent(), viewTitles[view]);
    };
    const tab = async view => inside(`[data-tab="${view}"]`).tap();
    const scrollTop = () => inside('#screen-body').evaluate(el => el.scrollTop);
    const back = () => inside('[data-action="back"]').tap();
    const choosePower = async operation => {
      await tab('controls');
      await inside('[data-view="power"]').tap();
      await inside(`[data-action="${operation === 'reboot' ? 'reboot' : 'shutdown'}"]`).tap();
    };

    const measure = async label => {
      const dimensions = await page.evaluate(() => {
        const device = document.querySelector('#device');
        const body = device.querySelector('.screen-body');
        const section = body.querySelector('section');
        return {
          viewport: innerWidth, document: document.documentElement.scrollWidth, page: document.body.scrollWidth,
          device: [device.scrollWidth, device.clientWidth], body: [body.scrollWidth, body.clientWidth],
          section: [section.scrollWidth, section.clientWidth],
        };
      });
      assert.ok(dimensions.document <= dimensions.viewport + 1 && dimensions.page <= dimensions.viewport + 1,
        `${label} document overflow: ${JSON.stringify(dimensions)}`);
      for (const name of ['device', 'body', 'section']) {
        assert.ok(dimensions[name][0] <= dimensions[name][1] + 1, `${label} ${name} overflow: ${JSON.stringify(dimensions)}`);
      }
      const smallTargets = await page.evaluate(() => Array.from(document.querySelectorAll(
        'button, a[href], input:not([type="hidden"]), select, textarea, [role="button"], [tabindex]:not([tabindex="-1"])',
      )).filter(el => !el.closest('[inert]') && el.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true }))
        .map(el => {
          // A native checkbox uses its full 64 px label as the touch target, as on the device.
          const target = el.matches('input[type="checkbox"]') ? el.closest('label') || el : el;
          const rect = target.getBoundingClientRect();
          return { element: el.tagName.toLowerCase(), id: el.id, text: (el.getAttribute('aria-label') || el.textContent || el.getAttribute('type') || '').trim(), width: rect.width, height: rect.height };
        }).filter(rect => rect.width < 55.99 || rect.height < 55.99));
      assert.deepEqual(smallTargets, [], `${label} touch targets must be at least 56 × 56 px, including range inputs`);
      const clippedControls = await device.evaluate(device => {
        const bounds = device.getBoundingClientRect();
        return Array.from(device.querySelectorAll('button, input')).filter(el => el.checkVisibility())
          .map(el => ({ label: el.getAttribute('aria-label') || el.id || el.textContent.trim(), left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right }))
          .filter(rect => rect.left < bounds.left - 1 || rect.right > bounds.right + 1);
      });
      assert.deepEqual(clippedControls, [], `${label} controls must fit within the device, including header buttons`);
    };

    for (const width of [376, 320]) {
      await page.setViewportSize({ width, height: 960 });
      for (const view of Object.keys(viewTitles)) {
        await check(`${width} × 960 / ${view}: query view, overflow and touch targets`, async () => {
          await open(view);
          assert.equal(await inside('.screen-nav [data-tab]').count(), 6);
          assert.equal(await inside('[data-tab="thermal"] span').textContent(), '温度');
          assert.equal(await inside('[data-tab="controls"] span').textContent(), '设置');
          await measure(`${width}/${view}`);
        });
      }
    }

    await page.setViewportSize({ width: 376, height: 960 });
    await check('six bottom tabs navigate and mark the current page', async () => {
      await open();
      for (const view of mainTabs) {
        await tab(view);
        assert.equal(await inside('.screen-title').textContent(), viewTitles[view]);
        assert.equal(await inside('[aria-current="page"]').getAttribute('data-tab'), view);
        assert.equal(await inside('[data-action="back"]').count(), 0);
      }
    });

    await check('complete application list and container details restore their parent scroll offset', async () => {
      // A shorter viewport makes these sample lists scroll without injecting extra fixture cards.
      await page.setViewportSize({ width: 376, height: 720 });
      try {
        await open('apps');
        assert.equal(await inside('#screen-body [data-view="containers"]').count(), 0);
        assert.equal(await inside('.app-card').count(), 4);
        const container = inside('[data-view="container"]').last();
        await container.scrollIntoViewIfNeeded();
        const appsOffset = await scrollTop();
        assert.ok(appsOffset > 0, 'the application list must actually scroll before testing restoration');
        const appName = await container.getAttribute('data-app');
        await container.tap();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.container);
        assert.equal(await inside('.app-heading h3').textContent(), appName);
        await back();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.apps);
        assert.ok(Math.abs(await scrollTop() - appsOffset) <= 1, 'applications scroll is restored');

      } finally {
        await page.setViewportSize({ width: 376, height: 960 });
      }
    });

    await check('all navigation icons match the web panel and temperature includes cached disk rows', async () => {
      await open('thermal');
      const symbols = fs.readFileSync(path.resolve(__dirname, '../ui/panel-src/20-icons.svg'), 'utf8');
      const canonical = html => html.replace(/\s*\/?>/g, '>').replace(/<\/(path|circle|ellipse)>/g, '');
      for (const [tabName, iconName] of Object.entries({ overview: 'home', storage: 'storage', network: 'net', apps: 'box', thermal: 'temp', controls: 'gear' })) {
        const expected = symbols.match(new RegExp('<symbol id="i-' + iconName + '"[^>]*>(.*?)</symbol>'))[1];
        const actual = await inside(`[data-tab="${tabName}"] svg`).evaluate(svg => svg.innerHTML);
        assert.equal(canonical(actual), canonical(expected), tabName);
      }
      assert.equal(await inside('.disk-temperature-row').count(), 4);
      assert.match(await inside('.disk-temperature-row').last().textContent(), /HDD 2.*--.*休眠中/);
    });

    await check('direct detail query back returns to its parent tab', async () => {
      for (const [view, parent] of [['processor', 'overview'], ['container', 'apps'], ['ports', 'network'], ['web', 'controls'], ['power', 'controls']]) {
        await open(view);
        await back();
        assert.equal(await inside('.screen-title').textContent(), viewTitles[parent]);
        assert.equal(await inside('[aria-current="page"]').getAttribute('data-tab'), parent);
      }
    });

    await check('brightness range responds to keyboard and touch, persists through navigation, and sleeps/wakes', async () => {
      await open('controls');
      const brightness = inside('#brightness');
      await brightness.focus();
      await brightness.press('Home');
      assert.equal(await brightness.inputValue(), '5');
      assert.match(await inside('[data-brightness-value]').textContent(), /^5\s*%$/);
      await brightness.press('ArrowRight');
      assert.equal(await brightness.inputValue(), '6');
      assert.match(await inside('[data-brightness-value]').textContent(), /^6\s*%$/);
      const bounds = await brightness.boundingBox();
      await page.touchscreen.tap(bounds.x + bounds.width * .75, bounds.y + bounds.height / 2);
      const touchValue = Number(await brightness.inputValue());
      assert.ok(touchValue >= 70 && touchValue <= 85, `touch range value ${touchValue}`);
      assert.match(await inside('[data-brightness-value]').textContent(), new RegExp(`^${touchValue}\\s*%$`));
      await tab('overview');
      await tab('controls');
      assert.equal(Number(await inside('#brightness').inputValue()), touchValue);
      await inside('[data-action="sleep"]').tap();
      assert.equal(await inside('#sleep').isVisible(), true);
      assert.equal(await inside('#sleep').evaluate(el => document.activeElement === el), true);
      assert.match(await inside('#sleep .dot-number').first().getAttribute('aria-label'), /^\d{2}:\d{2}$/);
      await inside('#sleep').tap();
      assert.equal(await inside('#sleep').isHidden(), true);
      assert.equal(Number(await inside('#brightness').inputValue()), touchValue);
    });

    await check('storage lists only SMART physical disks with usage', async () => {
      await open('storage');
      assert.equal(await inside('.drive-row').count(), 3);
      assert.equal(await inside('.drive-row .caption').first().textContent(), 'SMART 正常 · 缓存读数');
      const text = await inside('.screen-body').textContent();
      assert.match(text, /已用 73%/);
      assert.doesNotMatch(text, /HDD 2/);
    });

    await check('temperature is an independent tab with per-channel fan modes', async () => {
      await open('thermal');
      assert.equal(await inside('.fan-quick-row').count(), 2);
      await inside('[data-fan-channel="cpu"][data-fan-mode="low"]').tap();
      assert.equal(await inside('[data-fan-channel="cpu"][data-fan-mode="low"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await inside('.fan-quick-row').first().locator('.fan-quick-head span').textContent(), '目标 50%');
      assert.equal(await inside('.fan-quick-row').first().locator('.fan-mode').count(), 5);
      const fanBoxes = await inside('.fan-quick-row').first().locator('.fan-mode').evaluateAll(nodes => nodes.map(node => {
        const rect = node.getBoundingClientRect();
        return { x: Math.round(rect.x), y: Math.round(rect.y), width: rect.width };
      }));
      assert.equal(new Set(fanBoxes.map(box => box.y)).size, 1, 'five fan modes share one row');
      assert.ok(fanBoxes.every(box => box.width >= 56), JSON.stringify(fanBoxes));
      await tab('overview');
      await tab('thermal');
      assert.equal(await inside('[data-fan-channel="cpu"][data-fan-mode="low"]').getAttribute('aria-pressed'), 'true',
        'fan shortcut state survives main-tab navigation');
      await inside('[data-fan-channel="cpu"][data-fan-mode="auto"]').tap();
      await inside('[data-fan-channel="system"][data-fan-mode="high"]').tap();
      assert.equal(await inside('.fan-quick-row').last().locator('.fan-quick-head span').textContent(), '原厂管理',
        'a native system preset must not fabricate a target percentage');
      assert.equal(await inside('.fan-control .badge').textContent(), '原厂管理');
      await inside('[data-fan-channel="system"][data-fan-mode="manual"]').tap();
      assert.equal(await inside('[data-fan-channel="system"][data-fan-mode="manual"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await inside('.fan-control .badge').textContent(), '独立温控');
      await open('controls');
      assert.equal(await inside('[data-view="power"]').count(), 1, 'settings exposes power through a second-level entry');
      assert.equal(await inside('[data-action="reboot"]').count(), 0, 'settings must not expose shutdown actions directly');
      assert.equal(await inside('[data-action="shutdown"]').count(), 0);
      const hue = inside('#light-hue');
      assert.equal(await hue.evaluate(el => getComputedStyle(el).webkitAppearance), 'none',
        'custom hue thumb owns the track geometry');
      await hue.evaluate(el => { el.value = '120'; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); });
      assert.equal(await inside('[data-light-hue-label]').textContent(), '色相 120°');
      assert.match(await inside('.light-swatch').getAttribute('style'), /hsl\(120/);
      const brightness = inside('#light-brightness');
      await brightness.evaluate(el => { el.value = '55'; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); });
      assert.equal(await inside('[data-light-brightness]').textContent(), '55%');
      await inside('#light-effect').selectOption('2');
      assert.deepEqual(await inside('#light-effect option').allTextContents(), ['关闭', '常亮', '彩虹', '呼吸'],
        'prototype exposes only the four native light modes');
      assert.equal(await inside('#light-effect').inputValue(), '2');
      await inside('#sleep-timeout').selectOption('300');
      assert.equal(await inside('#sleep-timeout').inputValue(), '300');
    });

    await check('screen brightness, light brightness and hue sliders share geometry', async () => {
      await open('controls');
      const sliders = await inside('input.brightness').evaluateAll(nodes => nodes.map(node => {
        const rect = node.getBoundingClientRect();
        return { id: node.id, width: rect.width, height: rect.height, appearance: getComputedStyle(node).appearance };
      }));
      assert.equal(sliders.length, 3);
      assert.ok(sliders.every(slider => Math.abs(slider.width - sliders[0].width) <= 1 && Math.abs(slider.height - sliders[0].height) <= 1), JSON.stringify(sliders));
      assert.ok(sliders.every(slider => slider.height === 56 && slider.appearance === 'none'), JSON.stringify(sliders));
    });

    await check('trend selectors switch 1h / 6h / 24h curves and time labels', async () => {
      await open('trends');
      let firstCurve;
      for (const [range, start] of [['1h', '08:24'], ['6h', '03:24'], ['24h', '昨日 09:24'], ['1h', '08:24']]) {
        await inside(`[data-range="${range}"]`).tap();
        assert.equal(await inside('[data-range][aria-pressed="true"]').count(), 1);
        assert.equal(await inside('[data-range][aria-pressed="true"]').getAttribute('data-range'), range);
        assert.equal(await inside('.chart-times').first().locator('span').first().textContent(), start);
        const curve = await inside('.chart-card').first().locator('.spark path[stroke]').getAttribute('d');
        if (!firstCurve) firstCurve = curve;
        else if (range === '1h') assert.equal(curve, firstCurve);
        else assert.notEqual(curve, firstCurve, `${range} changes the demonstration curve`);
        assert.equal(await inside('.chart-card').count(), 6);
      }
    });

    await check('unknown temperatures and fan RPM stay unknown and use neutral styling', async () => {
      await open('thermal');
      const sleepingDisk = inside('.temperature-row').last();
      assert.equal(await sleepingDisk.locator('b').textContent(), '--');
      assert.equal(await sleepingDisk.locator('b').evaluate(el => el.classList.contains('muted')), true);
      const fanValues = await inside('.fan-value').filter({ has: page.locator('small') }).allTextContents();
      assert.equal(fanValues.length, 2);
      assert.ok(fanValues.every(value => /^--\s*RPM$/.test(value)), JSON.stringify(fanValues));
      assert.match(await inside('.screen-body').textContent(), /系统风扇/);
      assert.match(await inside('.screen-body').textContent(), /CPU温控/);
      assert.match(await inside('.screen-body').textContent(), /系统温控/);
      assert.doesNotMatch(await inside('.screen-body').textContent(), /机箱风扇|自动温控/);
    });

    await check('power dialog confirms first, defaults to Cancel, traps focus, and supports Escape', async () => {
      await open('overview');
      assert.equal(await inside('.screen-header').count(), 0, 'main pages have no top header');
      await open('processor');
      assert.equal(await inside('.screen-header').count(), 1, 'detail pages keep a back header');
      await open('controls');
      assert.equal(await inside('[data-action="reboot"]').count(), 0);
      await inside('[data-view="power"]').tap();
      assert.equal(await inside('.screen-title').textContent(), viewTitles.power);
      await inside('[data-action="reboot"]').tap();
      assert.equal(await inside('[role="dialog"]').isVisible(), true);
      assert.match(await inside('[role="dialog"]').textContent(), /请确认/);
      assert.equal(await inside('#power-status').isHidden(), true, 'opening confirmation cannot start the countdown');
      assert.equal(await inside('[data-action="cancel-modal"]').evaluate(el => document.activeElement === el), true);
      await measure('power confirmation');
      await page.keyboard.press('Tab');
      assert.equal(await inside('[data-action="confirm-power"]').evaluate(el => document.activeElement === el), true);
      await page.keyboard.press('Shift+Tab');
      assert.equal(await inside('[data-action="cancel-modal"]').evaluate(el => document.activeElement === el), true);
      await page.keyboard.press('Escape');
      assert.equal(await inside('#modal').isHidden(), true);
      assert.equal(await inside('[data-action="reboot"]').evaluate(el => document.activeElement === el), true);
      await inside('[data-action="shutdown"]').tap();
      await inside('[data-action="cancel-modal"]').tap();
      assert.equal(await inside('#modal').isHidden(), true);
      assert.equal(await inside('#power-status').isHidden(), true);
    });

    await check('five-second power countdown remains cancellable after switching tabs', async () => {
      await open('controls');
      await choosePower('reboot');
      await inside('[data-action="confirm-power"]').tap();
      assert.match(await inside('#power-status').textContent(), /5 秒/);
      await page.waitForFunction(() => document.querySelector('#device #power-status').textContent.includes('4 秒'));
      await tab('storage');
      assert.equal(await inside('.screen-title').textContent(), viewTitles.storage);
      assert.equal(await inside('#power-status').isVisible(), true);
      await measure('pending power operation');
      await inside('[data-action="cancel-power"]').focus();
      await page.waitForFunction(() => document.querySelector('#device #power-status').textContent.includes('3 秒'));
      assert.equal(await inside('[data-action="cancel-power"]').evaluate(el => document.activeElement === el), true,
        'countdown updates must retain keyboard focus on Cancel');
      await inside('[data-action="cancel-power"]').tap();
      assert.equal(await inside('#power-status').isHidden(), true);
      assert.equal(await inside('#toast').textContent(), '已取消电源操作。');
      await page.waitForTimeout(1100);
      assert.equal(await inside('#power-status').isHidden(), true, 'a cancelled countdown must stay cancelled');
    });

    await check('power countdown expires as a local simulation without navigating or contacting a device', async () => {
      await open('controls');
      const startingURL = page.url();
      await choosePower('shutdown');
      await inside('[data-action="confirm-power"]').tap();
      assert.match(await inside('#power-status').textContent(), /5 秒/);
      await inside('#power-status').waitFor({ state: 'hidden', timeout: 7500 });
      assert.equal(await inside('#toast').textContent(), '演示结束，未执行真实电源操作。');
      assert.equal(page.url(), startingURL);
      assert.deepEqual(externalRequests, []);
    });

    await check('screen saver settings match production labels, save selection order, and enforce one to twelve items', async () => {
      await open('controls');
      assert.deepEqual(await inside('#saver-timeout option').allTextContents(), ['关闭', '1 分钟', '5 分钟', '10 分钟', '30 分钟', '1 小时']);
      assert.equal(await inside('label[for="saver-timeout"]').textContent(), '屏幕保护');
      assert.equal(await inside('label[for="saver-brightness"]').textContent(), '保护亮度');
      assert.equal(await inside('label[for="sleep-timeout"]').textContent(), '关闭屏幕');
      assert.equal(await inside('#saver-brightness').inputValue(), '30');
      await inside('#saver-brightness').selectOption('20');
      const item = key => inside(`[data-saver-item="${key}"]`);
      const choose = key => item(key).locator('..').tap();
      const selected = () => inside('[data-saver-item]').evaluateAll(nodes => nodes.filter(node => node.checked).map(node => node.dataset.saverItem));
      await choose('memory_usage');
      assert.deepEqual(await selected(), ['cpu_usage']);
      assert.equal(await item('cpu_usage').isDisabled(), true, 'the last selected item cannot be removed');
      await choose('board_temperature');
      await choose('cpu_temperature');
      await choose('memory_usage');
      assert.deepEqual(await selected(), ['cpu_usage', 'board_temperature', 'cpu_temperature', 'memory_usage']);
      assert.deepEqual(await inside('.saver-choice b').allTextContents(), ['1', '2', '3', '4']);
      for (const key of await inside('[data-saver-item]:not(:checked)').evaluateAll(nodes => nodes.slice(0, 8).map(node => node.dataset.saverItem))) await choose(key);
      assert.equal((await selected()).length, 12);
      assert.equal(await inside('[data-saver-item]:not(:checked):disabled').count(), 2);
      assert.equal(await inside('.saver-picker legend').textContent(), '屏保展示内容 · 12 / 12');
      await inside('#saver-timeout').selectOption('0');
      assert.equal(await inside('.saver-picker').count(), 0);
      assert.equal(await inside('#saver-brightness').count(), 0);
      await inside('#saver-timeout').selectOption('300');
      assert.equal((await selected()).length, 12, 'disabling protection preserves chosen content');
      assert.equal(await inside('#saver-brightness').inputValue(), '20');
      await tab('thermal');
      await tab('controls');
      assert.equal((await selected()).length, 12);
      assert.equal(await inside('#saver-timeout').inputValue(), '300');
      assert.equal(await inside('#saver-brightness').inputValue(), '20');
    });

    await check('mixed screen saver matches production layout, rotates every ten seconds, and preserves unknown and standby values', async () => {
      await page.clock.install();
      await page.clock.resume();
      for (const width of [376, 320]) {
        await page.setViewportSize({ width, height: 960 });
        await page.goto(`${prototypeURL}?view=saver`, { waitUntil: 'load' });
        assert.equal(await inside('#saver').isVisible(), true);
        assert.equal(await inside('#saver .saver-metric').count(), 4);
        assert.deepEqual(await inside('#saver [data-saver-key]').evaluateAll(nodes => nodes.map(node => node.dataset.saverKey)), ['cpu_usage', 'memory_usage', 'cpu_temperature', 'board_temperature']);
        assert.deepEqual(await inside('#saver .value small').allTextContents(), ['%', '%', '℃', '℃']);
        assert.equal(await inside('#saver .saver-page').textContent(), '1 / 2');
        await measure(`${width}/saver`);
        if (screenshotDir) await device.screenshot({ path: path.join(screenshotDir, `saver-mixed-${width}.png`) });
        await page.clock.fastForward(10000);
        assert.equal(await inside('#saver .saver-page').textContent(), '2 / 2');
        const standby = inside('#saver [data-saver-key="temperature:disk:hdd_2"]');
        assert.equal(await standby.locator('[data-saver-value]').textContent(), '--');
        assert.equal(await standby.locator('.saver-metric-note').textContent(), '休眠中');
        await inside('#saver').tap();
        assert.equal(await inside('#saver').isHidden(), true);
        assert.equal(await inside('.screen-title').textContent(), '设置', 'wake does not activate underlying controls');
        await page.clock.fastForward(700);
        const unknown = inside('[data-saver-item="temperature:sysfs:core_3"]');
        await unknown.locator('..').tap();
        await page.clock.fastForward(60000);
        assert.equal(await inside('#saver').isVisible(), true);
        await page.clock.fastForward(10000);
        const unknownMetric = inside('#saver [data-saver-key="temperature:sysfs:core_3"]');
        assert.equal(await unknownMetric.locator('[data-saver-value]').textContent(), '--');
        assert.equal(await unknownMetric.locator('.saver-metric-note').textContent(), '读数暂不可用');
        if (screenshotDir) await device.screenshot({ path: path.join(screenshotDir, `saver-standby-${width}.png`) });
      }
      await page.clock.resume();
    });

    await check('short screen saver uses two items and screen-off takes priority over protection', async () => {
      await page.setViewportSize({ width: 320, height: 600 });
      await page.goto(`${prototypeURL}?view=saver`, { waitUntil: 'load' });
      assert.equal(await inside('#saver .saver-metric').count(), 2);
      assert.equal(await inside('#saver .saver-page').textContent(), '1 / 3');
      await measure('320/short saver');
      if (screenshotDir) await device.screenshot({ path: path.join(screenshotDir, 'saver-short-320.png') });
      await inside('#saver').tap();
      await page.clock.fastForward(700);
      await inside('#sleep-timeout').selectOption('60');
      await page.clock.fastForward(60000);
      assert.equal(await inside('#sleep').isVisible(), true);
      assert.equal(await inside('#saver').isHidden(), true);
      await inside('#sleep').tap();
      assert.equal(await inside('#sleep').isHidden(), true);
      assert.equal(await inside('#saver').isHidden(), true);
      assert.equal(await inside('#sleep-timeout').inputValue(), '60');
      await page.clock.resume();
      await page.setViewportSize({ width: 376, height: 960 });
    });

    await check('saved missing temperature devices stay selected and measured zero is not treated as unknown', async () => {
      await open('controls');
      // Simulate a previously saved stable ID whose device has disappeared; no network fixture is involved.
      await page.evaluate(() => {
        state.saverItems = ['temperature:disk:retired'];
        demoSaverSources.find(source => source.id === 'disk:nvme_0').value = 0;
        render();
      });
      const missing = inside('[data-saver-item="temperature:disk:retired"]');
      assert.equal(await missing.isChecked(), true);
      assert.equal(await missing.isDisabled(), true);
      assert.match(await missing.locator('..').textContent(), /设备离线，保留原选择/);
      await inside('[data-saver-item="temperature:disk:nvme_0"]').locator('..').tap();
      await page.clock.fastForward(61000);
      assert.equal(await inside('#saver').isVisible(), true);
      assert.equal(await inside('#saver [data-saver-key="temperature:disk:nvme_0"] [data-saver-value]').textContent(), '0');
      assert.equal(await inside('#saver [data-saver-key="temperature:disk:retired"] [data-saver-value]').textContent(), '--');
      assert.equal(await inside('#saver [data-saver-key="temperature:disk:retired"] .saver-metric-note').textContent(), '设备离线');
      await page.clock.resume();
    });

    await check('web-access prototype reflects the default password mode and preserves sample-address semantics', async () => {
      await open('web');
      const text = await inside('.screen-body').textContent();
      assert.match(text, /示例地址/);
      assert.match(text, /WEB ACCESS/);
      assert.match(text, /网页写操作不要求配对码/);
      assert.match(text, /登录口令默认关闭/);
      assert.match(text, /登录口令可选保护/);
      assert.doesNotMatch(text, /登录与配对|生成一次性配对码/);
    });

    if (screenshotDir) {
      await check('optional 376 × 960 screenshots', async () => {
        for (const view of ['overview', 'storage', 'network', 'apps', 'thermal', 'controls', 'web']) {
          await open(view);
          await device.screenshot({ path: path.join(screenshotDir, `${view}.png`) });
        }
      });
    }

    await check('desktop gallery lays out twelve pages including screen protection and each entry opens an interactive page', async () => {
      await page.setViewportSize({ width: 1600, height: 1200 });
      await open('overview', 'gallery');
      assert.equal(await page.locator('#gallery-container').isVisible(), true);
      assert.equal(await page.locator('#stage').isHidden(), true);
      assert.equal(await page.locator('#gallery .gallery-item').count(), 12);
      assert.equal(await page.locator('#gallery .device[data-preview-device]').count(), 12);
      assert.equal(await page.locator('#gallery [aria-label="屏幕保护页面展示"] .saver-layer').isVisible(), true);
      assert.equal(await page.locator('#gallery [aria-label="屏幕保护页面展示"] .saver-metric').count(), 4);
      assert.equal(await page.evaluate(() => {
        const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
        return ids.length === new Set(ids).size;
      }), true, 'gallery controls keep unique IDs and matching labels');
      assert.equal(await page.locator('#gallery .device[inert]').count(), 0, 'inert would prevent scrolling long previews');
      const gallery = await page.locator('#gallery .device').evaluateAll(devices => devices.map(el => {
        const rect = el.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      }));
      assert.ok(new Set(gallery.map(rect => Math.round(rect.x))).size >= 2, 'desktop gallery must have multiple columns');
      assert.ok(gallery.every(rect => Math.abs(rect.width - 376) <= 1 && Math.abs(rect.height - 960) <= 1), JSON.stringify(gallery));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const trendPreview = page.locator('#gallery .gallery-item').nth(galleryViews.indexOf('trends'));
      const previewBody = trendPreview.locator('.screen-body');
      await previewBody.scrollIntoViewIfNeeded();
      await previewBody.hover();
      await page.mouse.wheel(0, 500);
      await page.waitForFunction(() => document.querySelectorAll('#gallery .screen-body')[2].scrollTop > 0);
      assert.ok(await previewBody.evaluate(el => el.scrollTop) > 0, 'long previews scroll with the wheel');
      const previewView = trendPreview.locator('[data-tab="network"]');
      await previewView.scrollIntoViewIfNeeded();
      const previewBounds = await previewView.boundingBox();
      // Real pointer input can hit an aria-disabled preview; Playwright's locator.tap intentionally cannot.
      await page.touchscreen.tap(previewBounds.x + previewBounds.width / 2, previewBounds.y + previewBounds.height / 2);
      assert.equal(await page.locator('#gallery-container').isVisible(), true, 'preview controls cannot navigate the interactive device');
      assert.equal(await inside('.screen-title').textContent(), viewTitles.overview);
      await previewBody.evaluate(el => { el.scrollTop = 0; });
      if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'desktop.png'), fullPage: true });
      for (const view of galleryViews) {
        await page.locator(`#gallery [data-preview="${view}"]`).tap();
        assert.equal(await page.locator('#stage').isVisible(), true);
        assert.equal(await page.locator('#gallery-container').isHidden(), true);
        assert.equal(await inside('.screen-title').textContent(), view === 'saver' ? viewTitles.controls : viewTitles[view]);
        if (view === 'saver') {
          assert.equal(await inside('#saver').isVisible(), true);
          await inside('#saver').tap();
          await page.clock.fastForward(700);
        }
        await tab('network');
        assert.equal(await inside('.screen-title').textContent(), viewTitles.network);
        await page.locator('[data-mode="gallery"]').tap();
        assert.equal(await page.locator('#gallery-container').isVisible(), true);
      }
    });

    await check('desktop gallery cannot conceal a power confirmation or pending cancellation', async () => {
      await open('overview');
      await choosePower('reboot');
      await page.locator('[data-mode="gallery"]').tap();
      assert.equal(await inside('[role="dialog"]').isVisible(), true);
      assert.equal(await page.locator('#stage').isVisible(), true);
      assert.equal(await page.locator('#gallery-container').isHidden(), true);
      await page.keyboard.press('Tab');
      assert.equal(await inside('[role="dialog"]').evaluate(el => el.contains(document.activeElement)), true,
        'an attempted gallery switch must keep keyboard navigation in the confirmation');
      await page.keyboard.press('Escape');
      await choosePower('reboot');
      await inside('[data-action="confirm-power"]').tap();
      await page.locator('[data-mode="gallery"]').tap();
      assert.equal(await page.locator('#stage').isVisible(), true);
      assert.equal(await page.locator('#gallery-container').isHidden(), true);
      assert.equal(await inside('#power-status').isVisible(), true);
      assert.equal(await inside('[data-action="cancel-power"]').evaluate(el => {
        const bounds = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2));
      }), true, 'a warning must leave the power cancellation button immediately tappable');
      await inside('[data-action="cancel-power"]').tap();
      assert.equal(await inside('#power-status').isHidden(), true);
    });

    await check('no JavaScript page errors and no external or NAS requests', async () => {
      assert.deepEqual(pageErrors, []);
      assert.deepEqual(externalRequests, []);
      assert.equal(context.pages().length, 1, 'prototype navigation must stay in the current page');
    });
  } finally {
    await browser.close();
  }
  console.log(`\n${failures.length ? 'FAIL' : 'PASS'}: ${checks.length} passed, ${failures.length} failed; ${pageErrors.length} page errors; ${externalRequests.length} external requests.`);
  if (screenshotDir) console.log(`Screenshots: ${path.resolve(screenshotDir)}`);
  if (failures.length) process.exitCode = 1;
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
