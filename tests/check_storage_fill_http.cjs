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

OLD, STEP = time.time() - 5, 1.0
class MemoryState:
    def __init__(self, valid):
        self.lock = threading.RLock()
        self.smart_valid = self.capacity_valid = valid
        self.stamp = OLD
        self.round = 0
        self.scenario = None
        self.review_stamp = self.review_now = None
        self.review_age = 0
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
            fresh = valid and 0 <= time.time() - stamp < 30
            disk = {'id': 'sda', 'device': '/dev/sda', 'model': 'Memory fixture drive',
                    'user_name': '内存测试盘', 'size_bytes': 4e12,
                    'standby': False if fresh else None, 'smart_available': valid, 'smart_fresh': fresh,
                    'smart_sampled_at': stamp if valid else None,
                    'temp_c': (39 + self.state.round * 2) if fresh else None,
                    'health': {'passed': True if fresh else None}}
            return {'system': {'model': 'HIKSEMI R1', 'memory': {}},
                    'device': {'identity': {'hostname': 'Storage-Fill-Fixture'}, 'storage': {}},
                    'disks': {'available': True, 'items': [disk]}}
    def smart_cached(self, device):
        assert device == 'sda'
        with self.state.lock:
            valid, stamp = self.state.smart_valid, self.state.stamp
            fresh = valid and 0 <= time.time() - stamp < 30
            return {'smart_sampled_at': stamp if valid else None, 'smart_fresh': fresh,
                    'smart_data': {'power_on_time': {'hours': 100 + self.state.round * 50}} if fresh else {}}
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


# The added DOM regressions keep the production server and projection. Only
# hardware inputs, device identity and cache clocks are fixed memory values.
from insights import InsightsCollector

class ScenarioInsights(Insights):
    def _measurement(self, name):
        state = self.state
        identity = ('fixture-generation', name)
        collector = object.__new__(InsightsCollector)
        collector._lock = threading.RLock()
        collector.clock = lambda: 1000.0 + state.review_age
        collector._smart_context = lambda device: identity if device == name else None
        sleeping = name == 'sda' and state.scenario in ('missing', 'old')
        temp = None if sleeping or state.scenario == 'no_temp' else 36 if name == 'sda' else 34
        value = {'standby': sleeping, 'temp_c': temp, 'smart_available': True,
                 'smart_sampled_at': state.review_stamp,
                 'health': {'passed': None if sleeping else not (state.scenario == 'failed' and name == 'sdb')}}
        collector._smart_cache = {name: (1000.0, value, identity)}
        collector._smart_measurements = {}
        collector._smart_visible_observations = {}
        result = collector._smart(name, identity)
        # Deliberately inconsistent legacy dates must not appear fresh in UI.
        if state.scenario == 'future':
            result['smart_sampled_at'] = state.review_now + 1
        elif state.scenario == 'boundary':
            result['smart_sampled_at'] = state.review_now - 30
        return result
    def snapshot(self):
        if not self.state.scenario:
            return super().snapshot()
        with self.state.lock:
            rows = [{'id': name, 'device': '/dev/' + name, 'model': 'Memory review ' + name,
                     'user_name': 'Review-' + name, 'size_bytes': size * 1e12,
                     **self._measurement(name)} for name, size in [('sda', 2), ('sdb', 4), ('sdc', 8)]]
            return {'system': {'model': 'HIKSEMI R1', 'memory': {}},
                    'device': {'identity': {'hostname': 'Storage-Review'}, 'storage': {}},
                    'disks': {'available': True, 'items': rows}}
    def smart_cached(self, device):
        if not self.state.scenario:
            return super().smart_cached(device)
        return {**self._measurement(device), 'smart_data': {}}
    def refresh_smart_visible(self, *, authorizer):
        if not self.state.scenario:
            return super().refresh_smart_visible(authorizer=authorizer)
        assert authorizer() is True
        with self.state.lock:
            self.state.calls.append({'phase': 'smart', 'mode': 'review', 'scenario': self.state.scenario})
            self.visible_receipt = {'ok': True, 'force': False, 'status': 'partial',
                'started_at': self.state.review_now, 'finished_at': self.state.review_now,
                'results': [{'device': name, 'status': 'skipped_standby' if name == 'sda' else 'refreshed',
                             'reason': 'standby' if name == 'sda' else None,
                             'sampled_at': None if name == 'sda' else self.state.review_stamp}
                            for name in ('sda', 'sdb', 'sdc')]}
            return self.visible_receipt
    def capacity_visible_eligibility(self, receipt):
        if not self.state.scenario:
            return super().capacity_visible_eligibility(receipt)
        assert receipt is self.visible_receipt
        return lambda device: 'standby' if device == 'sda' else None

class ScenarioMonitor(MemoryMonitor):
    def snapshot(self):
        if not self.state.scenario:
            return super().snapshot()
        with self.state.lock:
            scenario, stamp = self.state.scenario, self.state.review_stamp
            rows = []
            for index, name in enumerate(('sda', 'sdb', 'sdc')):
                valid = scenario not in ('fresh_missing', 'stale') and (name != 'sda' or scenario == 'old')
                rows.append({'mount': '/drives/fixture' if name == 'sda' else '/drives/' + name,
                             'total_bytes': (2, 4, 8)[index] * 1e12 if valid else None,
                             'used_bytes': (1, 1, 4)[index] * 1e12 if valid else None,
                             'capacity_status': 'cached' if valid else 'skipped_standby',
                             'capacity_sampled_at': stamp - (86400 if name == 'sda' else 0) if valid else None})
            return {'cpu': {}, 'memory': {}, 'network': {}, 'uptime_seconds': 10, 'disks': rows}
    def refresh_capacity_visible(self, *, authorizer, eligibility):
        if not self.state.scenario:
            return super().refresh_capacity_visible(authorizer=authorizer, eligibility=eligibility)
        assert authorizer() is True
        assert eligibility('sda') == 'standby' and eligibility('sdb') is None and eligibility('sdc') is None
        with self.state.lock:
            self.state.calls.append({'phase': 'capacity', 'mode': 'review', 'scenario': self.state.scenario})
            return {'ok': True, 'status': 'partial', 'started_at': self.state.review_now,
                    'finished_at': self.state.review_now,
                    'results': [{'mount_id': 101 + i, 'status': 'skipped_standby' if i == 0 else 'refreshed',
                                 'reason': 'standby' if i == 0 else None,
                                 'sampled_at': None if i == 0 else self.state.review_stamp}
                                for i in range(3)]}

class ScenarioPanel(FnmonitorPanel):
    def _disks(self, **kwargs):
        rows = super()._disks(**kwargs)
        scenario = self.insights.state.scenario
        # Simulate an older server lacking mount metadata, not an empty list.
        if scenario == 'mount_unknown':
            for row in rows:
                if row['name'] == 'sda':
                    row.pop('mounts', None)
        return rows


servers, states = [], []
try:
    for label, auth_mode, valid in [('owner', 'password', True), ('nonowner', 'pair', False)]:
        folder = fixture / label
        for suffix in ['proc/self', 'sys', 'dev', 'run/udev', 'data']:
            (folder / suffix).mkdir(parents=True, exist_ok=True)
        (folder / 'proc/self/mountinfo').write_text(
            '101 1 8:1 / /drives/fixture rw - ext4 /dev/sda1 rw\n', encoding='ascii')
        state = MemoryState(valid)
        insights = ScenarioInsights(state)
        monitor = ScenarioMonitor(state, insights)
        controller = Controller(folder / 'data')
        os.environ['R1_WEB_AUTH_MODE'] = auth_mode
        os.environ['R1_DEV_NO_PAIRING'] = '0'
        server = WebConfigServer(controller, insights=insights, monitor=monitor,
                                 host='127.0.0.1', port=0, asset_dir=repo / 'ui/web')
        # Replace only FnmonitorPanel's filesystem roots, keeping its real projection.
        server.panel = ScenarioPanel(controller, insights, None, None, None, monitor,
            state_dir=server.state_dir, web=server, proc_root=folder / 'proc',
            sys_root=folder / 'sys', dev_root=folder / 'dev', udev_root=folder / 'run/udev',
            weather_fetcher=lambda city: {'ok': False, 'error': 'fixture_weather_unavailable'},
            wall_clock=lambda state=state: state.review_now if state.scenario else time.time())
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
        if operation.startswith('scenario_'):
            with states[0].lock:
                state = states[0]
                state.scenario = operation[len('scenario_'):]
                if state.scenario == 'stale':
                    state.review_age = 30.0
                    state.review_now = state.review_stamp + state.review_age
                else:
                    state.review_age = 0.0
                    state.review_stamp = state.review_now = time.time()
                folder = fixture / 'owner'
                mounts = ('101 1 8:1 / /drives/fixture rw - ext4 /dev/sda1 rw\n'
                          if state.scenario != 'unmounted' else '')
                mounts += ('102 1 8:17 / /drives/sdb rw - ext4 /dev/sdb1 rw\n'
                           '103 1 8:33 / /drives/sdc rw - ext4 /dev/sdc1 rw\n')
                (folder / 'proc/self/mountinfo').write_text(mounts, encoding='ascii')
                servers[0].panel._mount_cache = (float('-inf'), {})
        elif operation in ('missing', 'hold'):
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
    await page.waitForFunction(() => document.querySelector('#sheetBody [data-ref="r1SmartSnapshot"]')?.textContent === 'SMART 数据');
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
    await page.waitForFunction(() => /200 小时/.test(document.querySelector('#sheetBody [data-ref="smart"]')?.textContent || ''),
      null, { timeout: 8000 });
    const filled = await api(page, '/api/overview');
    check('响应后同一抽屉无需重开即显示新值且不显示采样时间',
      filled.body.disks_detail[0].used === 2e12 && filled.body.disks_detail[0].temp === 43
      && filled.body.disks_detail[0].capacity_sampled_at === fixture.old + 2 * fixture.step
      && filled.body.disks_detail[0].smart_sampled_at === fixture.old + 2 * fixture.step
      && await page.evaluate(() => document.querySelector('#sheetBody') === window.__refreshDrawer
        && document.querySelector('[data-ref="r1CapNote"]')?.textContent === '容量数据'
        && document.querySelector('[data-ref="r1SmartSnapshot"]')?.textContent === 'SMART 数据'));
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

    // Permanent screenshot regressions use the actual HTTP server, projection,
    // original module renderer and real click/hash navigation, not route.fulfill.
    await page.context().close();
    const review = await makePage('owner'); activePage = review;
    await review.clock.install();
    const dom = () => review.evaluate(() => ({
      pool: document.querySelector('#layout-storage [data-ref="poolV"]')?.getAttribute('aria-label'),
      free: document.querySelector('#layout-storage [data-ref="poolFree"]')?.getAttribute('aria-label'),
      legend: document.querySelector('#layout-storage [data-ref="poolLeg"]')?.textContent,
      observed: document.querySelector('#layout-storage [data-ref="scanAgo"]')?.textContent,
      bays: [...document.querySelectorAll('#layout-storage .bay')].map(bay => ({
        name: bay.querySelector('.bay-name').textContent, capacity: bay.querySelector('[data-ref="cap"]').textContent,
        temp: bay.querySelector('[data-ref="t"]').textContent, unit: bay.querySelector('.bay-temp small').textContent,
        state: bay.dataset.st, title: bay.title, led: getComputedStyle(bay.querySelector('.bay-led')).backgroundColor,
      })),
    }));
    const enter = async scenario => {
      await review.goto('about:blank');
      await fixtureCall('scenario_' + scenario);
      const before = calls(await fixtureCall('stats'))[0].length;
      const pending = foregroundResponse(review, fixture.owner, 'refresh');
      await review.goto(fixture.owner + '/#/storage', { waitUntil: 'domcontentloaded' });
      const receipt = await (await pending).json();
      await review.waitForFunction(() => document.querySelectorAll('#layout-storage .bay').length === 3);
      await review.clock.fastForward(2100);
      check(scenario + '：一次进入恰好两阶段，无轮询追加采集', calls(await fixtureCall('stats'))[0].length === before + 2);
      return { receipt, overview: (await api(review, '/api/overview')).body, ui: await dom() };
    };
    const closeToStorage = async () => {
      const pending = foregroundResponse(review, fixture.owner, 'refresh');
      await review.keyboard.press('Escape');
      await pending;
      await review.waitForSelector('#sheet.on', { state: 'hidden' });
    };
    for (const scenario of ['missing', 'old']) {
      const { receipt, overview, ui } = await enter(scenario);
      const a = overview.disks_detail.find(d => d.name === 'sda');
      check(scenario + '：生产 partial/standby 回执不清除 B/C 独立容量',
        receipt.smart.results[0].status === 'skipped_standby' && receipt.capacity.status === 'partial'
        && receipt.capacity.results[0].status === 'skipped_standby'
        && overview.disks_detail.find(d => d.name === 'sdb').percent === 25
        && overview.disks_detail.find(d => d.name === 'sdc').percent === 50
        && ui.bays.find(d => d.name === 'sdb').capacity.startsWith('25%')
        && ui.bays.find(d => d.name === 'sdc').capacity.startsWith('50%'), { receipt, ui });
      check(scenario + '：池只聚合有效容量且不显示覆盖／采样状态',
        overview.disk_size === (scenario === 'old' ? 14e12 : 12e12)
        && overview.disk_used === (scenario === 'old' ? 6e12 : 5e12)
        && ui.pool === (scenario === 'old' ? '43' : '42') && ui.free === (scenario === 'old' ? '8.00' : '7.00')
        && !/fnOS|\/vol\*|已覆盖|采样|快照/.test(ui.legend), ui);
      check(scenario + '：休眠 A 单独缺测或保留同代历史容量', a.standby === true
        && a.total === (scenario === 'old' ? 2e12 : null) && a.mounts.length === 1
        && ui.bays.find(d => d.name === 'sda').state === 'standby'
        && (scenario === 'old' ? ui.bays[0].capacity.startsWith('50%')
          : ui.bays[0].capacity.includes('已挂载 1 个分区') && !ui.bays[0].capacity.includes('未挂载')), ui);
      for (const [name, expected] of [['sda', scenario === 'old' ? '50' : '--'], ['sdb', '25'], ['sdc', '50']]) {
        const pending = foregroundResponse(review, fixture.owner, 'refresh');
        await review.locator('#layout-storage .bay[data-sheet="disk/' + name + '"]').click();
        await pending;
        await review.waitForFunction(value => document.querySelector('#sheet.on .sheet-hero [data-ref="v"]')?.getAttribute('aria-label') === value, expected);
        const note = await review.locator('#sheetBody [data-ref="r1CapNote"]').textContent();
        const row = overview.disks_detail.find(d => d.name === name);
        check(scenario + '：磁盘详情 ' + name + ' 显示独立用量且不显示采样时间',
          await review.locator('#sheetBody .sheet-hero [data-ref="v"]').getAttribute('aria-label') === expected
          && note === '容量数据', { note });
        if (name === 'sda' && scenario === 'missing') check('已挂载缺测 A 的详情不再显示未挂载',
          (await review.locator('#sheetBody [data-ref="leg"]').textContent()).includes('已挂载 1 个分区'));
        await closeToStorage();
      }
      await review.locator('#layout-storage [data-sheet="storage"]').first().click();
      await review.waitForSelector('#sheet.on [data-p="sdb"]');
      const rowB = await review.locator('#sheetBody [data-p="sdb"]').textContent();
      const rowC = await review.locator('#sheetBody [data-p="sdc"]').textContent();
      const rowA = await review.locator('#sheetBody [data-p="sda"]').textContent();
      check(scenario + '：全部硬盘抽屉保留 B25/C50，并区别 A 容量缺测和未挂载',
        rowB.includes('25%') && rowC.includes('50%')
        && (scenario === 'old' ? rowA.includes('50%') : rowA.includes('容量未采集') && !rowA.includes('未挂载')), { rowA, rowB, rowC });
    }
    for (const [scenario, expected] of [['unmounted', '未挂载'], ['mount_unknown', '挂载状态未知']]) {
      const { overview, ui } = await enter(scenario);
      const a = overview.disks_detail.find(d => d.name === 'sda');
      check(scenario + '：缺测容量与挂载事实分开显示', ui.bays.find(d => d.name === 'sda').capacity.includes(expected)
        && (scenario === 'unmounted' ? a.mounts.length === 0 : !Object.hasOwn(a, 'mounts')), { a, ui });
      const pending = foregroundResponse(review, fixture.owner, 'refresh');
      await review.locator('#layout-storage .bay[data-sheet="disk/sda"]').click(); await pending;
      check(scenario + '：磁盘详情保留挂载状态的明确性', (await review.locator('#sheetBody [data-ref="leg"]').textContent()).includes(expected));
      await closeToStorage();
      await review.locator('#layout-storage [data-sheet="storage"]').first().click();
      check(scenario + '：全部硬盘抽屉不把未知挂载误推为空', (await review.locator('#sheetBody [data-p="sda"]').textContent()).includes(expected));
    }
    const healthy = await enter('no_temp');
    check('新鲜健康且温度缺失仍正常，数值 -- 且单位 ℃ 不被替换', healthy.ui.bays.every(d => d.state === 'ok' && d.temp === '--' && d.unit === '℃'), healthy.ui);
    const noTempPending = foregroundResponse(review, fixture.owner, 'refresh');
    await review.locator('#layout-storage .bay[data-sheet="disk/sdb"]').click(); await noTempPending;
    await review.waitForFunction(() => document.querySelector('#sheetBody [data-ref="pill"]')?.textContent === '健康');
    check('新鲜健康但缺温度的详情不误判未知，也不借旧温度填零',
      await review.locator('#sheetBody [data-ref="pill"]').textContent() === '健康'
      && await review.locator('#sheetBody [data-ref="smart"] tr').filter({ has: review.locator('td:first-child', { hasText: '温度' }) }).locator('td').nth(1).textContent() === '—');
    await closeToStorage();
    // Keep the same live page and its old /api/disk_health PASS cache.
    await fixtureCall('scenario_failed');
    const failedGET = review.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
    await review.clock.fastForward(15010); await failedGET;
    await review.waitForFunction(() => document.querySelector('#layout-storage .bay[data-sheet="disk/sdb"]')?.dataset.st === 'crit');
    check('当前新鲜 FAIL 立即覆盖独立旧 PASS 分级与标题', (await dom()).bays.find(d => d.name === 'sdb').title.includes('SMART 未通过'));
    await enter('missing');
    await fixtureCall('scenario_active');
    const activeGET = review.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
    await review.clock.fastForward(15010); await activeGET;
    await review.waitForFunction(() => document.querySelector('#layout-storage .bay[data-sheet="disk/sda"]')?.dataset.st === 'ok');
    check('休眠→活动不锁住旧 standby DOM', (await dom()).bays.find(d => d.name === 'sda').temp === '36');
    await fixtureCall('scenario_stale');
    const activeExpiredGET = review.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
    await review.clock.fastForward(15010); await activeExpiredGET;
    await review.waitForTimeout(200);
    check('同一 DOM 不再渲染采样时间或缓存状态',
      !/采样|快照|观测于/.test(await review.textContent('#layout-storage')));
    for (const scenario of ['boundary', 'future']) {
      const { ui } = await enter(scenario);
      check(scenario + '：仍展示缓存数据且不渲染采样状态', ui.bays.every(d => d.temp !== '--' && d.state !== 'unknown'), ui);
    }
    const fresh = await enter('fresh_missing');
    check('容量全缺测显示不可用说明，不编造未挂载或厂商路径', fresh.ui.pool === '--' && fresh.ui.free === '--'
      && fresh.ui.legend.includes('容量数据暂不可用') && !/fnOS|\/vol\*/.test(fresh.ui.legend)
      && fresh.ui.bays.every(d => d.capacity.includes('已挂载') && !d.capacity.includes('未挂载')), fresh.ui);
    const beforeAge = calls(await fixtureCall('stats'))[0].length;
    const originalDates = fresh.overview.disks_detail.map(d => d.smart_sampled_at);
    await fixtureCall('scenario_stale');
    const staleGET = review.waitForResponse(response => response.url() === fixture.owner + '/api/overview');
    await review.clock.fastForward(31000); await staleGET;
    await review.waitForFunction(() => [...document.querySelectorAll('#layout-storage .bay')].every(b => b.dataset.st === 'unknown' && b.querySelector('[data-ref="t"]').textContent === '--'));
    await review.waitForTimeout(100);
    const stale = await dom(), expired = (await api(review, '/api/overview')).body;
    check('真实三十秒投影已 false/null，31 秒页面立即 unknown 灰灯，单位不丢',
      expired.disks_detail.every(d => d.smart_fresh === false && d.temp === null && d.health === null)
      && stale.bays.every(d => d.state === 'unknown' && d.temp === '--' && d.unit === '℃' && d.led !== 'rgb(18, 138, 78)'), stale);
    check('过期不渲染采样时间或缓存状态且不因 GET 续期', !/采样|过期|观测/.test(stale.observed)
      && JSON.stringify(expired.disks_detail.map(d => d.smart_sampled_at)) === JSON.stringify(originalDates), stale);
    await review.clock.fastForward(31000);
    check('62 秒独立健康轮询后仍不返绿，停留未增加任何采集', (await dom()).bays.every(d => d.state === 'unknown')
      && calls(await fixtureCall('stats'))[0].length === beforeAge);


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
