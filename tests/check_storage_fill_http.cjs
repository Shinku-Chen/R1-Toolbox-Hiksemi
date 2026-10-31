// Real panel + production HTTP/auth/fill orchestration; all hardware is memory-only.
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

OLD, NEW = 1793416300.0, 1793416360.0
class MemoryState:
    def __init__(self, valid):
        self.lock = threading.RLock()
        self.smart_valid = self.capacity_valid = valid
        self.stamp = OLD
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
                    'temp_c': (39 if stamp == OLD else 43) if valid else None,
                    'health': {'passed': True if valid else None}}
            return {'system': {'model': 'HIKSEMI R1', 'memory': {}},
                    'device': {'identity': {'hostname': 'Storage-Fill-Fixture'}, 'storage': {}},
                    'disks': {'available': True, 'items': [disk]}}
    def smart_cached(self, device):
        assert device == 'sda'
        with self.state.lock:
            valid, stamp = self.state.smart_valid, self.state.stamp
            return {'smart_sampled_at': stamp if valid else None, 'smart_fresh': valid,
                    'smart_data': {'power_on_time': {'hours': 100 if stamp == OLD else 200}} if valid else {}}
    def storage_identity_cached(self):
        return {'items': []}
    def fill_smart_missing(self, *, authorizer):
        assert authorizer() is True
        with self.state.lock:
            self.state.calls.append({'phase': 'smart', 'owner': authorizer() is True})
            self.state.entered.set()
        assert self.state.released.wait(8), 'fixture fill was not released'
        assert authorizer() is True
        with self.state.lock:
            self.state.smart_valid, self.state.stamp = True, NEW
        return {'ok': True, 'force': False, 'status': 'complete',
                'started_at': NEW, 'finished_at': NEW + 1,
                'results': [{'device': 'sda', 'status': 'refreshed', 'reason': None, 'sampled_at': NEW}]}
    def capacity_fill_eligibility(self, device):
        assert device == 'sda'
        with self.state.lock:
            return None if self.state.smart_valid and self.state.stamp == NEW else 'power_state_unverified'

class MemoryMonitor:
    def __init__(self, state, insights):
        self.state, self.insights = state, insights
    def snapshot(self):
        with self.state.lock:
            valid, stamp = self.state.capacity_valid, self.state.stamp
            return {'cpu': {}, 'memory': {}, 'network': {}, 'uptime_seconds': 10,
                    'disks': [{'mount': '/drives/fixture', 'total_bytes': 4e12 if valid else None,
                               'used_bytes': (1e12 if stamp == OLD else 2e12) if valid else None,
                               'capacity_status': 'cached' if valid else 'unknown',
                               'capacity_sampled_at': stamp if valid else None}]}
    def refresh_capacity_missing(self, *, authorizer, eligibility):
        assert authorizer() is True
        assert eligibility == self.insights.capacity_fill_eligibility
        assert eligibility('sda') is None, 'production must run SMART before capacity'
        with self.state.lock:
            self.state.calls.append({'phase': 'capacity', 'owner': authorizer() is True})
            self.state.capacity_valid = True
        assert authorizer() is True
        return {'ok': True, 'status': 'complete', 'started_at': NEW + 1, 'finished_at': NEW + 2,
                'results': [{'mount_id': 101, 'status': 'refreshed', 'reason': None, 'sampled_at': NEW}]}

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
                      'old': OLD, 'new': NEW}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        operation = request['operation']
        if operation == 'missing':
            with states[0].lock:
                states[0].smart_valid = states[0].capacity_valid = False
                states[0].released.clear()
        elif operation == 'release':
            states[0].released.set()
        elif operation == 'entered':
            assert states[0].entered.wait(4), 'HTTP request did not enter production fill'
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
    const deniedPost = await api(nonowner, '/api/r1/storage/fill', {});
    check('未配对非 owner 页面不自动 POST，直接 POST 403 且零采集',
      deniedSession.status === 200 && deniedSession.body.owner_authorized === false
      && deniedSession.body.auth_mode === 'pair' && fillRequests('nonowner').length === 1
      && deniedPost.status === 403 && deniedPost.body.ok === false
      && calls(deniedBefore)[1].length === 0 && calls(await fixtureCall('stats'))[1].length === 0,
      { session: deniedSession, post: deniedPost });
    await nonowner.context().close();

    const page = await makePage('owner');
    activePage = page;
    await page.goto(fixture.owner + '/#/overview');
    await page.waitForSelector('#layout-overview section');
    await page.locator('#pageSeg button[data-page="storage"]').click();
    await page.locator('#layout-storage .bay[data-sheet="disk/sda"]').click();
    await page.waitForSelector('#sheet.on [data-ref="r1CapNote"]');
    await page.waitForFunction(() => document.querySelector('#sheetBody [data-ref="r1SmartSnapshot"]')?.textContent.includes('采样于'));
    const oldCapacityNote = await page.locator('[data-ref="r1CapNote"]').textContent();
    const oldSmartNote = await page.locator('[data-ref="r1SmartSnapshot"]').textContent();
    const session = await api(page, '/api/auth/session');
    const initial = await api(page, '/api/overview');
    await page.waitForTimeout(2100); // Cover two actual foreground-watch ticks, not just startup.
    check('真实开放口令会话给出 owner_authorized，生产投影使用 total／used',
      session.status === 200 && session.body.owner_authorized === true && session.body.auth_mode === 'password'
      && session.body.auth_required === false && initial.body.disks_detail[0].total === 4e12
      && initial.body.disks_detail[0].used === 1e12 && initial.body.disks_detail[0].capacity_status === 'cached', session);
    check('有效缓存的总览／磁盘页／抽屉只有 GET，SMART／容量零采集',
      requests.filter(request => request.label === 'owner').every(request => request.method === 'GET')
      && calls(await fixtureCall('stats')).every(value => value.length === 0), requests);

    // Keep the exact current drawer alive through missing data and the real POST.
    await page.locator('#sheetBody').evaluate(node => { window.__fillDrawer = node; });
    const fillResponse = page.waitForResponse(response => response.url() === fixture.owner + '/api/r1/storage/fill' && response.request().method() === 'POST');
    // A later failed assertion must not leave this browser waiter unhandled.
    fillResponse.catch(() => {});
    const fillRequest = page.waitForRequest(request => request.url() === fixture.owner + '/api/r1/storage/fill' && request.method() === 'POST');
    await fixtureCall('missing');
    // The real 15-second overview poll observes the miss while the drawer stays open.
    await fillRequest;
    const during = await fixtureCall('entered');
    check('缺测可见时仅一次严格 {} POST，真实编排先启动受控 SMART',
      fillRequests('owner').length === 1 && fillRequests('owner')[0].method === 'POST'
      && fillRequests('owner')[0].body === '{}' && JSON.stringify(calls(during)[0]) === JSON.stringify([{ phase: 'smart', owner: true }]), during);
    await fixtureCall('release');
    const response = await fillResponse, receipt = await response.json();
    check('真实 HTTP 回执保留 SMART／容量两阶段，生产编排各调用一次',
      response.status() === 200 && receipt.ok === true && receipt.force === false
      && receipt.smart.results[0].status === 'refreshed' && receipt.capacity.results[0].status === 'refreshed'
      && JSON.stringify(calls(await fixtureCall('stats'))[0]) === JSON.stringify([
        { phase: 'smart', owner: true }, { phase: 'capacity', owner: true }]), receipt);
    await page.waitForFunction(() => {
      const body = document.querySelector('#sheetBody');
      return body === window.__fillDrawer && document.querySelector('#sheet.on')
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
      && filled.body.disks_detail[0].capacity_sampled_at === fixture.new
      && filled.body.disks_detail[0].smart_sampled_at === fixture.new
      && await page.evaluate(() => document.querySelector('#sheetBody') === window.__fillDrawer));
    for (const pathname of ['/api/overview', '/api/smart?dev=sda', '/api/r1']) {
      assert.equal((await api(page, pathname)).status, 200);
    }
    await page.waitForTimeout(2100);
    check('后续被动 GET 与前台轮询不重复 POST 或采集', fillRequests('owner').length === 1
      && calls(await fixtureCall('stats'))[0].length === 2);

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
