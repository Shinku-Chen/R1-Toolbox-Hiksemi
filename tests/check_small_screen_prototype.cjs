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
  apps: '应用', containers: 'Docker 容器', container: '容器详情', vms: '虚拟机', thermal: '温度与风扇',
  trends: '历史趋势', ports: '监听端口', controls: '设置', web: '网页设置',
};
const galleryViews = ['overview', 'metrics', 'trends', 'processor', 'storage', 'thermal', 'network', 'containers', 'vms', 'ports', 'controls', 'web'];

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
          const rect = el.getBoundingClientRect();
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

    await check('nested application details restore their parent page and scroll offset', async () => {
      // A shorter viewport makes these sample lists scroll without injecting extra fixture cards.
      await page.setViewportSize({ width: 376, height: 720 });
      try {
        await open('apps');
        const allContainers = inside('#screen-body [data-view="containers"]');
        await allContainers.scrollIntoViewIfNeeded();
        const appsOffset = await scrollTop();
        await allContainers.tap();
        const container = inside('[data-view="container"]').last();
        await container.scrollIntoViewIfNeeded();
        const containersOffset = await scrollTop();
        assert.ok(containersOffset > 0, 'the container list must actually scroll before testing restoration');
        const appName = await container.getAttribute('data-app');
        await container.tap();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.container);
        assert.equal(await inside('.app-heading h3').textContent(), appName);
        await back();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.containers);
        assert.ok(Math.abs(await scrollTop() - containersOffset) <= 1, 'container list scroll is restored');
        await back();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.apps);
        assert.ok(Math.abs(await scrollTop() - appsOffset) <= 1, 'applications scroll is restored');

        const vmEntry = inside('[data-view="vms"]');
        await vmEntry.scrollIntoViewIfNeeded();
        const vmParentOffset = await scrollTop();
        assert.ok(vmParentOffset > 0, 'the applications page must actually scroll before opening virtual machines');
        await vmEntry.tap();
        assert.equal(await inside('.screen-title').textContent(), viewTitles.vms);
        await back();
        assert.ok(Math.abs(await scrollTop() - vmParentOffset) <= 1, 'applications scroll is restored after virtual machine details');
      } finally {
        await page.setViewportSize({ width: 376, height: 960 });
      }
    });

    await check('direct detail query back returns to its parent tab', async () => {
      for (const [view, parent] of [['processor', 'overview'], ['containers', 'apps'], ['ports', 'network'], ['web', 'controls']]) {
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
      assert.match(await inside('.fan-quick-row').first().locator('.fan-quick-head span').textContent(), /低/);
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
      await inside('[data-fan-channel="system"][data-fan-mode="manual"]').tap();
      assert.equal(await inside('[data-fan-channel="system"][data-fan-mode="manual"]').getAttribute('aria-pressed'), 'true');
      await open('controls');
      assert.match(await inside('.screen-body').textContent(), /电源管理/);
      assert.equal(await inside('[data-action="reboot"]').count(), 1);
      assert.equal(await inside('[data-action="shutdown"]').count(), 1);
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
      assert.equal(await inside('#light-effect').inputValue(), '2');
      await inside('#sleep-timeout').selectOption('300');
      assert.equal(await inside('#sleep-timeout').inputValue(), '300');
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
      const fanValues = await inside('.fan-value').allTextContents();
      assert.equal(fanValues.length, 2);
      assert.ok(fanValues.every(value => /^--\s*RPM$/.test(value)), JSON.stringify(fanValues));
    });

    await check('power dialog confirms first, defaults to Cancel, traps focus, and supports Escape', async () => {
      await open('overview');
      assert.equal(await inside('.screen-header').count(), 0, 'main pages have no top header');
      await open('processor');
      assert.equal(await inside('.screen-header').count(), 1, 'detail pages keep a back header');
      await open('controls');
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

    if (screenshotDir) {
      await check('optional 376 × 960 screenshots', async () => {
        for (const view of ['overview', 'storage', 'network', 'apps', 'controls']) {
          await open(view);
          await device.screenshot({ path: path.join(screenshotDir, `${view}.png`) });
        }
      });
    }

    await check('desktop gallery lays out twelve pages in a grid and each entry opens an interactive page', async () => {
      await page.setViewportSize({ width: 1600, height: 1200 });
      await open('overview', 'gallery');
      assert.equal(await page.locator('#gallery-container').isVisible(), true);
      assert.equal(await page.locator('#stage').isHidden(), true);
      assert.equal(await page.locator('#gallery .gallery-item').count(), 12);
      assert.equal(await page.locator('#gallery .device[data-preview-device]').count(), 12);
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
        assert.equal(await inside('.screen-title').textContent(), viewTitles[view]);
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
