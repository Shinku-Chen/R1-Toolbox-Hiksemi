// Real panel + production HTTP/auth/fill/refresh orchestration; hardware stays in memory.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-storage-fill-http-'));
const chromeBin = [process.env.CHROME_BIN,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '请用 CHROME_BIN 指定 Chrome／Chromium');

const pythonFixture = String.raw`
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

repo, fixture = map(lambda value: Path(value).resolve(), sys.argv[1:3])
assert fixture.name.startswith('r1-storage-fill-http-') and fixture.is_dir()
sys.path.insert(0, str(repo / 'runtime'))
from web_config import WebConfigServer
from fnm_compat import FnmonitorPanel

# Fail closed if this small fixture ever grows an actual hardware or network read.
# No production collector, sampler, vendor client or subprocess is constructed.
forbidden = []
def reject_io(*args, **kwargs):
    forbidden.append('hardware_or_outbound_io')
    raise AssertionError('fixture forbids subprocess/statvfs/outbound connections')
subprocess.Popen = reject_io
subprocess.run = reject_io
socket.socket.connect = reject_io
socket.create_connection = reject_io
if hasattr(os, 'statvfs'):
    os.statvfs = reject_io

OLD, STEP = 1793416300.0, 60.0
class MemoryState:
    def __init__(self, valid):
        self.lock = threading.RLock()
        self.smart_valid = self.capacity_valid = valid
        self.stamp = OLD
        self.round = 0
        self.released = threading.Event()
        self.entered = threading.Event()
        self.released.set()
        self.calls = []
    def stats(self):
        with self.lock:
            return {'calls': copy.deepcopy(self.calls), 'smart_valid': self.smart_valid,
                    'capacity_valid': self.capacity_valid}

class Controller:
    def __init__(self, folder):
        self.state_dir = folder
    def snapshot(self):
        return {'settings': {}, 'capabilities': {}, 'screen': {}, 'rgb': {}}

class Insights:
    def __init__(self, state):
        self.state = state
    def snapshot(self):
        with self.state.lock:
            valid, stamp = self.state.smart_valid, self.state.stamp
            disk = {'id': 'sda', 'device': '/dev/sda', 'model': 'Memory fixture drive',
                    'user_name': '内存测试盘', 'size_bytes': 4e12,
                    'standby': False, 'smart_available': valid, 'smart_fresh': valid,
                    'smart_sampled_at': stamp if valid else None,
                    'temp_c': (39 + self.state.round * 2) if valid else None,
                    'health': {'passed': True if valid else None}}
            return {'system': {'model': 'HIKSEMI R1', 'memory': {}},
                    'device': {'identity': {'hostname': 'Storage-Fill-Fixture'}, 'storage': {}},
                    'disks': {'available': True, 'items': [disk]}}
    def smart_cached(self, device):
        assert device == 'sda'
        with self.state.lock:
            valid, stamp = self.state.smart_valid, self.state.stamp
            return {'smart_sampled_at': stamp if valid else None, 'smart_fresh': valid,
                    'smart_data': {'power_on_time': {'hours': 100 + self.state.round * 50}} if valid else {}}
    def storage_identity_cached(self):
        return {'items': []}
    def fill_smart_missing(self, *, authorizer):
        return self._smart(authorizer, 'fill')
    def refresh_smart_visible(self, *, authorizer):
        receipt = self._smart(authorizer, 'refresh')
        self.visible_receipt = receipt
        return receipt
    def _smart(self, authorizer, mode):
        assert authorizer() is True
        with self.state.lock:
            self.state.calls.append({'phase': 'smart', 'mode': mode,
                                     'cached': self.state.smart_valid, 'owner': authorizer() is True})
            self.state.entered.set()
        assert self.state.released.wait(8), 'fixture foreground request was not released'
        assert authorizer() is True
        with self.state.lock:
            self.state.round += 1
            self.state.smart_valid, self.state.stamp = True, OLD + self.state.round * STEP
            sampled = self.state.stamp
        return {'ok': True, 'force': False, 'status': 'complete',
                'started_at': sampled, 'finished_at': sampled + 1,
                'results': [{'device': 'sda', 'status': 'refreshed', 'reason': None, 'sampled_at': sampled}]}
    def capacity_fill_eligibility(self, device):
        assert device == 'sda'
        with self.state.lock:
            return None if self.state.smart_valid and self.state.round > 0 else 'power_state_unverified'
    def capacity_visible_eligibility(self, receipt):
        assert receipt is self.visible_receipt, 'production must bind capacity to the exact SMART receipt'
        def eligible(device):
            assert receipt is self.visible_receipt, 'capacity must use this refresh round'
            return self.capacity_fill_eligibility(device)
        return eligible

class MemoryMonitor:
    def __init__(self, state, insights):
        self.state, self.insights = state, insights
    def snapshot(self):
        with self.state.lock:
            valid, stamp = self.state.capacity_valid, self.state.stamp
            return {'cpu': {}, 'memory': {}, 'network': {}, 'uptime_seconds': 10,
                    'disks': [{'mount': '/drives/fixture', 'total_bytes': 4e12 if valid else None,
                               'used_bytes': (1e12 + self.state.round * 5e11) if valid else None,
                               'capacity_status': 'cached' if valid else 'unknown',
                               'capacity_sampled_at': stamp if valid else None}]}
    def refresh_capacity_missing(self, *, authorizer, eligibility):
        assert eligibility == self.insights.capacity_fill_eligibility
        return self._capacity(authorizer, eligibility, 'fill')
    def refresh_capacity_visible(self, *, authorizer, eligibility):
        assert eligibility != self.insights.capacity_fill_eligibility
        return self._capacity(authorizer, eligibility, 'refresh')
    def _capacity(self, authorizer, eligibility, mode):
        assert authorizer() is True
        assert eligibility('sda') is None, 'production must run SMART before capacity'
        with self.state.lock:
            self.state.calls.append({'phase': 'capacity', 'mode': mode,
                                     'cached': self.state.capacity_valid, 'owner': authorizer() is True})
            self.state.capacity_valid = True
            sampled = self.state.stamp
        assert authorizer() is True
        return {'ok': True, 'status': 'complete', 'started_at': sampled + 1, 'finished_at': sampled + 2,
                'results': [{'mount_id': 101, 'status': 'refreshed', 'reason': None, 'sampled_at': sampled}]}

servers, states = [], []
try:
    for label, auth_mode, valid in [('owner', 'password', True), ('nonowner', 'pair', False)]:
        folder = fixture / label
        for suffix in ['proc/self', 'sys', 'dev', 'run/udev', 'data']:
            (folder / suffix).mkdir(parents=True, exist_ok=True)
        (folder / 'proc/self/mountinfo').write_text(
            '101 1 8:1 / /drives/fixture rw - ext4 /dev/sda1 rw\n', encoding='ascii')
        state = MemoryState(valid)
        insights = Insights(state)
        monitor = MemoryMonitor(state, insights)
        controller = Controller(folder / 'data')
        os.environ['R1_WEB_AUTH_MODE'] = auth_mode
        os.environ['R1_DEV_NO_PAIRING'] = '0'
        server = WebConfigServer(controller, insights=insights, monitor=monitor,
                                 host='127.0.0.1', port=0, asset_dir=repo / 'ui/web')
        # Replace only FnmonitorPanel's filesystem roots, keeping its real projection.
        server.panel = FnmonitorPanel(controller, insights, None, None, None, monitor,
            state_dir=server.state_dir, web=server, proc_root=folder / 'proc',
            sys_root=folder / 'sys', dev_root=folder / 'dev', udev_root=folder / 'run/udev',
            weather_fetcher=lambda city: {'ok': False, 'error': 'fixture_weather_unavailable'})
        # Some Windows hosts allocate unsafe low ports for port=0. Bind a high
        # loopback port through the real server, retrying only unavailable ports.
        for attempt in range(20):
            server.port = 20000 + secrets.randbelow(40000)
            try:
                server.start()
                break
            except OSError:
                if server.server is not None or attempt == 19:
                    raise
        servers.append(server)
        states.append(state)
    print(json.dumps({'ready': True, 'owner': 'http://127.0.0.1:' + str(servers[0].port),
                      'nonowner': 'http://127.0.0.1:' + str(servers[1].port),
                      'old': OLD, 'step': STEP}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request['operation']
        if operation in ('missing', 'hold'):
            with states[0].lock:
                if operation == 'missing':
                    states[0].smart_valid = states[0].capacity_valid = False
                states[0].entered.clear()
                states[0].released.clear()
        elif operation == 'release':
            states[0].released.set()
        elif operation == 'entered':
            assert states[0].entered.wait(4), 'HTTP request did not enter production foreground orchestration'
        elif operation != 'stats':
            raise ValueError('unknown fixed fixture operation')
        print(json.dumps({'id': request['id'], 'states': [state.stats() for state in states],
                          'forbidden': forbidden}), flush=True)
finally:
    for state in states:
        state.released.set()
    for server in reversed(servers):
        server.stop()
`;

(async () => {
  const child = spawn(process.env.PYTHON_BIN || 'python3', ['-B', '-u', '-c', pythonFixture, root, fixtureDir],
    { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  let stderr = '', browser, activePage, resolveReady, rejectReady, sequence = 0;
  const replies = new Map(), checks = [], requests = [], errors = [], external = [];
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
    assert.ok(condition, name + (detail ? '：' + JSON.stringify(detail) : ''));
    checks.push(name);
  };
  const calls = stats => stats.states.map(state => state.calls);
  const api = (page, pathname, body) => page.evaluate(async ({ pathname, body }) => {
    const response = await fetch(pathname, body === undefined ? { cache: 'no-store' } : {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FNM-Request': '1' },
      body: JSON.stringify(body), credentials: 'same-origin' });
    return { status: response.status, body: await response.json() };
  }, { pathname, body });
  const fillRequests = label => requests.filter(request => request.label === label && request.pathname === '/api/r1/storage/fill');
  const refreshRequests = label => requests.filter(request => request.label === label && request.pathname === '/api/r1/storage/refresh');
  const foregroundResponse = (page, origin, operation) => {
    const pending = page.waitForResponse(response => response.url() === origin + '/api/r1/storage/' + operation
      && response.request().method() === 'POST');
    pending.catch(() => {}); // Keep an earlier assertion failure from leaving an unhandled browser waiter.
    return pending;
  };
  const expectedRound = (mode, cached) => [
    { phase: 'smart', mode, cached, owner: true }, { phase: 'capacity', mode, cached, owner: true }];
  const completeReceipt = receipt => receipt.ok === true && receipt.force === false
    && receipt.smart.results[0].status === 'refreshed' && receipt.capacity.results[0].status === 'refreshed';
  try {
    const fixture = await ready;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const makePage = async label => {
      const context = await browser.newContext({ viewport: { width: 1120, height: 1000 }, locale: 'zh-CN' });
      await context.route('**/*', route => {
        const url = route.request().url();
        if (new URL(url).origin === fixture[label] || url.startsWith('data:')) return route.continue();
        external.push(url);
        return route.abort('blockedbyclient');
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(label + ': ' + error.message));
      page.on('request', request => {
        const url = new URL(request.url());
        if (url.origin === fixture[label] && url.pathname.startsWith('/api/')) {
          requests.push({ label, pathname: url.pathname, method: request.method(), body: request.postData() });
        }
      });
      return page;
    };
    const nonowner = await makePage('nonowner');
    await nonowner.goto(fixture.nonowner + '/#/storage');
    await nonowner.waitForSelector('#layout-storage .bay[data-sheet="disk/sda"]');
    const deniedSession = await api(nonowner, '/api/auth/session');
    await nonowner.waitForTimeout(2100);
    const deniedBefore = await fixtureCall('stats');
    const deniedRefresh = await api(nonowner, '/api/r1/storage/refresh', {});
    const deniedFill = await api(nonowner, '/api/r1/storage/fill', {});
    check('未配对非 owner 页面不自动 POST，直接 refresh／fill 均 403 且零采集',
      deniedSession.status === 200 && deniedSession.body.owner_authorized === false
      && deniedSession.body.auth_mode === 'pair' && fillRequests('nonowner').length === 1
      && refreshRequests('nonowner').length === 1 && deniedRefresh.status === 403 && deniedRefresh.body.ok === false
      && deniedFill.status === 403 && deniedFill.body.ok === false
      && calls(deniedBefore)[1].length === 0 && calls(await fixtureCall('stats'))[1].length === 0,
      { session: deniedSession, refresh: deniedRefresh, fill: deniedFill });
    await nonowner.context().close();

    const page = await makePage('owner');
    activePage = page;
    await page.goto(fixture.owner + '/#/overview');
    await page.waitForSelector('#layout-overview section');
    const session = await api(page, '/api/auth/session');
    const initial = await api(page, '/api/overview');
    await page.waitForTimeout(2100); // Cover two actual foreground-watch ticks, not just startup.
    check('真实开放口令会话给出 owner_authorized，生产投影使用 total／used',
      session.status === 200 && session.body.owner_authorized === true && session.body.auth_mode === 'password'
      && session.body.auth_required === false && initial.body.disks_detail[0].total === 4e12
      && initial.body.disks_detail[0].used === 1e12 && initial.body.disks_detail[0].capacity_status === 'cached', session);
    check('有效缓存的概览仅被动 GET，SMART／容量零采集',
      requests.filter(request => request.label === 'owner').every(request => request.method === 'GET')
      && calls(await fixtureCall('stats')).every(value => value.length === 0), requests);

    // Entering storage must refresh even though the production projection is valid.
    const storageResponse = foregroundResponse(page, fixture.owner, 'refresh');
    await page.locator('#pageSeg button[data-page="storage"]').click();
    const storageHTTP = await storageResponse, storageReceipt = await storageHTTP.json();
    await page.waitForTimeout(2100);
    const afterStorage = await fixtureCall('stats');
    check('进入存储页即使缓存有效仍只发一次严格 {} refresh，真实两阶段各采集一次',
      storageHTTP.status() === 200 && completeReceipt(storageReceipt)
      && refreshRequests('owner').length === 1 && refreshRequests('owner')[0].body === '{}'
      && fillRequests('owner').length === 0
      && JSON.stringify(calls(afterStorage)[0]) === JSON.stringify(expectedRound('refresh', true)),
      { receipt: storageReceipt, calls: calls(afterStorage)[0] });

    // Hold only the next memory SMART operation, leaving both real cache projections valid.
    await fixtureCall('hold');
    const detailResponse = foregroundResponse(page, fixture.owner, 'refresh');
    await page.locator('#layout-storage .bay[data-sheet="disk/sda"]').click();
    await page.waitForSelector('#sheet.on [data-ref="r1CapNote"]');
    await page.waitForFunction(() => document.querySelector('#sheetBody [data-ref="r1SmartSnapshot"]')?.textContent.includes('采样于'));
    const oldCapacityNote = await page.locator('[data-ref="r1CapNote"]').textContent();
    const oldSmartNote = await page.locator('[data-ref="r1SmartSnapshot"]').textContent();
    await page.locator('#sheetBody').evaluate(node => { window.__refreshDrawer = node; });
    const during = await fixtureCall('entered');
    check('进入已有缓存的磁盘详情仅再发一次严格 {} refresh，在真实 SMART 在途时保留当前抽屉',
      refreshRequests('owner').length === 2 && refreshRequests('owner')[1].body === '{}'
      && fillRequests('owner').length === 0
      && JSON.stringify(calls(during)[0]) === JSON.stringify([
        ...expectedRound('refresh', true), { phase: 'smart', mode: 'refresh', cached: true, owner: true }]), during);
    await fixtureCall('release');
    const response = await detailResponse, receipt = await response.json();
    check('真实 HTTP 回执保留 SMART／容量两阶段，生产编排各调用一次',
      response.status() === 200 && completeReceipt(receipt)
      && JSON.stringify(calls(await fixtureCall('stats'))[0]) === JSON.stringify([
        ...expectedRound('refresh', true), ...expectedRound('refresh', true)]), receipt);
    await page.waitForFunction(() => {
      const body = document.querySelector('#sheetBody');
      return body === window.__refreshDrawer && document.querySelector('#sheet.on')
        && body.querySelector('.sheet-hero [data-ref="v"]')?.getAttribute('aria-label') === '50'
        && /43℃/.test(body.querySelector('[data-ref="smart"]')?.textContent || '');
    }, null, { timeout: 8000 });
    await page.waitForFunction(({ oldCapacityNote, oldSmartNote }) => {
      const capacity = document.querySelector('[data-ref="r1CapNote"]')?.textContent || '';
      const smart = document.querySelector('[data-ref="r1SmartSnapshot"]')?.textContent || '';
      return capacity !== oldCapacityNote && capacity.includes('采样于')
        && smart !== oldSmartNote && smart.includes('采样于')
        && /200 小时/.test(document.querySelector('#sheetBody [data-ref="smart"]')?.textContent || '');
    }, { oldCapacityNote, oldSmartNote }, { timeout: 8000 });
    const filled = await api(page, '/api/overview');
    check('响应后同一抽屉无需重开即显示新值及新容量／SMART 时间',
      filled.body.disks_detail[0].used === 2e12 && filled.body.disks_detail[0].temp === 43
      && filled.body.disks_detail[0].capacity_sampled_at === fixture.old + 2 * fixture.step
      && filled.body.disks_detail[0].smart_sampled_at === fixture.old + 2 * fixture.step
      && await page.evaluate(() => document.querySelector('#sheetBody') === window.__refreshDrawer));
    for (const pathname of ['/api/overview', '/api/smart?dev=sda', '/api/r1']) {
      assert.equal((await api(page, pathname)).status, 200);
    }
    await page.waitForTimeout(2100);
    check('磁盘详情停留与后续被动 GET 不追加 refresh／fill 或采集', refreshRequests('owner').length === 2
      && fillRequests('owner').length === 0 && calls(await fixtureCall('stats'))[0].length === 4);

    // Overview retains the separate missing-only path; do not route its fill through refresh.
    // One real hash-route transition avoids a separate intervening storage entry.
    await page.goto(fixture.owner + '/#/overview');
    await page.waitForSelector('#sheet.on', { state: 'hidden' });
    await page.waitForSelector('#layout-overview section');
    await page.waitForTimeout(1100);
    const fillResponse = foregroundResponse(page, fixture.owner, 'fill');
    // Only the real passive overview timer observes this miss; it never collects itself.
    const fillRequest = page.waitForRequest(request => request.url() === fixture.owner + '/api/r1/storage/fill'
      && request.method() === 'POST');
    fillRequest.catch(() => {});
    await fixtureCall('missing');
    await fillRequest;
    const duringFill = await fixtureCall('entered');
    check('概览缺测沿用严格 {} fill，真实编排先 SMART 且不转成主动 refresh',
      fillRequests('owner').length === 1 && fillRequests('owner')[0].body === '{}'
      && refreshRequests('owner').length === 2
      && JSON.stringify(calls(duringFill)[0].slice(4)) === JSON.stringify([
        { phase: 'smart', mode: 'fill', cached: false, owner: true }]), duringFill);
    await fixtureCall('release');
    const fillHTTP = await fillResponse, fillReceipt = await fillHTTP.json();
    await page.waitForTimeout(2100);
    check('概览 fill 回执与两阶段真实编排完成，停留与轮询不重试',
      fillHTTP.status() === 200 && completeReceipt(fillReceipt)
      && fillRequests('owner').length === 1 && refreshRequests('owner').length === 2
      && JSON.stringify(calls(await fixtureCall('stats'))[0].slice(4)) === JSON.stringify(expectedRound('fill', false)), fillReceipt);

    const final = await fixtureCall('stats');
    check('无页面异常、外网请求、真实硬件／statvfs／子进程访问',
      errors.length === 0 && external.length === 0 && final.forbidden.length === 0,
      { errors, external, forbidden: final.forbidden });
    console.log(JSON.stringify({ ok: true, checks, fixtureDir, calls: calls(final), requests }, null, 2));
  } catch (error) {
    const drawer = activePage && !activePage.isClosed() ? await activePage.evaluate(() => ({
      shown: !!document.querySelector('#sheet.on'),
      percent: document.querySelector('#sheetBody .sheet-hero [data-ref="v"]')?.getAttribute('aria-label'),
      capacityNote: document.querySelector('[data-ref="r1CapNote"]')?.textContent,
      smartNote: document.querySelector('[data-ref="r1SmartSnapshot"]')?.textContent,
      smartRows: document.querySelector('#sheetBody [data-ref="smart"]')?.textContent,
    })).catch(() => null) : null;
    console.error(JSON.stringify({ ok: false, checks, fixtureDir, requests, errors, external,
      error: String(error && error.stack || error), drawer, stderr }, null, 2));
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
})();
