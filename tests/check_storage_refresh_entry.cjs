// Execute the real enclosed R1 controller, stock routing and sheet lifecycle in a bounded VM.
// The DOM and transport are memory-only; no browser internals, NAS or hardware are exposed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, 'ui/panel-src', name), 'utf8');
const r1 = read('50-r1.js'), core = read('33-state.js'), sheets = read('45-sheets.js'), layout = read('48-layout.js');
function part(source, start, end) {
  const first = source.indexOf(start), last = end ? source.indexOf(end, first) : source.length;
  assert.ok(first >= 0 && last > first, '真实源码边界必须存在：' + start);
  return source.slice(first, last);
}
const checks = [];
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const receipt = (id, standby = false) => ({ ok: true, force: false, status: 'complete', started_at: 100, finished_at: 100 + id,
  smart: { ok: true, force: false, status: 'complete', started_at: 100, finished_at: 100 + id,
    results: ['sda', 'sdb'].map(device => ({ device, status: standby ? 'skipped_standby' : 'refreshed',
      reason: standby ? 'standby' : null, sampled_at: standby ? null : 100 + id })) },
  capacity: { ok: true, status: 'complete', started_at: 100, finished_at: 100 + id,
    results: [{ mount_id: 1, status: standby ? 'skipped_standby' : 'refreshed', reason: standby ? 'standby' : null,
      sampled_at: standby ? null : 100 + id }] } });

function model() {
  const state = { posts: [], gets: [], renders: [], builds: [], observers: [], changed: false,
    gates: new Map(), active: 0, maxActive: 0, clock: 100000, mode: 'normal', missing: false,
    owner: true, action: false };
  const events = { document: new Map(), window: new Map() }, nodes = new Map();
  const listen = (kind, name, fn) => {
    if (!events[kind].has(name)) events[kind].set(name, []);
    events[kind].get(name).push(fn);
  };
  const dispatch = (kind, name) => (events[kind].get(name) || []).forEach(fn => fn({ type: name }));
  const node = id => {
    if (nodes.has(id)) return nodes.get(id);
    let hidden = false, html = '';
    const classes = new Set();
    const value = { id, style: {}, dataset: {}, offsetWidth: 0, scrollTop: 0, setAttribute() {},
      focus() {}, classList: {
        add(name) { if (!classes.has(name)) state.changed = true; classes.add(name); },
        remove(name) { if (classes.delete(name)) state.changed = true; },
        contains: name => classes.has(name),
      } };
    Object.defineProperties(value, {
      hidden: { get: () => hidden, set(value) { if (hidden !== value) state.changed = true; hidden = value; } },
      innerHTML: { get: () => html, set(value) { html = value; state.changed = true; } },
    });
    nodes.set(id, value); return value;
  };
  const overview = () => ({ marker: state.posts.length, config: { must_not_survive: true }, disks_detail: ['sda', 'sdb'].map(name => ({
    name, total: state.missing ? null : 1000, used: state.missing ? null : 100,
    capacity_sampled_at: state.missing ? null : 100, capacity_status: state.missing ? 'unavailable' : 'cached',
    smart_fresh: !state.missing, smart_available: !state.missing, temp: state.missing ? null : 39,
  })) });
  const transport = (stage, value) => {
    const gate = state.gates.get(stage);
    if (!gate) return Promise.resolve(value);
    state.gates.delete(stage); gate.entered = true;
    return gate.promise.then(() => value);
  };
  const context = {
    S: { page: 'network', o: overview(), smartDetail: {}, sheet: null, editing: false },
    document: { hidden: false, title: '', activeElement: node('focus'), documentElement: { style: {} },
      addEventListener: (name, fn) => listen('document', name, fn),
      getElementById: node, querySelectorAll: () => [], contains: () => true },
    window: { addEventListener: (name, fn) => listen('window', name, fn) },
    addEventListener: (name, fn) => listen('window', name, fn),
    location: { hash: '#/network' },
    history: { replaceState(_state, _title, hash) { context.location.hash = hash; },
      pushState(_state, _title, hash) { context.location.hash = hash; }, back() {} },
    MutationObserver: class { constructor(fn) { state.observers.push(fn); } observe() {} },
    Date: class extends Date { static now() { return state.clock; } },
    setTimeout() { return 0; }, clearTimeout() {},
    $: selector => node(selector.replace(/^#/, '')), $$: () => [],
    placeThumb() {}, scrollTo() {}, setEditing() {}, buildSettings() {}, syncSettings() {}, onPageChange() {},
    setHTML(target, html) { target.innerHTML = html; }, setText() {}, ic() {}, dmScan() {},
    renderNow() { state.renders.push(context.S.o.marker); },
    disksOf: value => value.disks_detail,
    r1CapIsStamp: value => typeof value === 'number' && Number.isFinite(value) && value > 0,
    r1SmartUpdateSnapshot() {}, r1CapSync() {},
    API: {
      get(url) {
        state.gets.push(url);
        if (url === '/api/auth/session') return transport('auth', { owner_authorized: state.owner });
        if (url === '/api/overview') return transport('overview', overview());
        if (url.startsWith('/api/smart?')) return transport('smart', { marker: state.posts.length });
        throw new Error('unexpected GET ' + url);
      },
      post(url, body) {
        assert.ok(['/api/r1/storage/refresh', '/api/r1/storage/fill'].includes(url));
        state.posts.push({ url, body: JSON.stringify(body) });
        state.active++; state.maxActive = Math.max(state.maxActive, state.active);
        const response = state.mode === 'busy' ? Promise.reject(Object.assign(new Error('busy'), { status: 409 }))
          : transport('post', state.mode === 'invalid' ? { ...receipt(state.posts.length), force: true }
            : receipt(state.posts.length, state.mode === 'standby'));
        return response.finally(() => { state.active--; });
      },
    },
  };
  context.SHEETS = Object.fromEntries(['disk', 'cpu', 'mem', 'net', 'keys'].map(type => [type, {
    title: value => value || type, icon: 'disk', build(_body, arg) {
      state.builds.push({ type, arg, post: state.posts.length, action: state.action }); return () => {};
    },
  }]));
  const script = part(core, 'const SMART_DETAIL_TTL', '/** 轮询调度')
    + part(core, 'const Poll =', '/** 历史缓存')
    + part(sheets, 'const ICO_OF', "$('#sheetClose').addEventListener")
    + part(layout, 'const PAGES =', "$$('#pageSeg button, #bottomNav button').forEach(b => b.addEventListener")
    + part(r1, 'function r1SmartVisibleDisk()', 'async function r1SmartRun(')
    + part(r1, 'const R1_FILL =', '/* R1 展示兼容：')
    + '\nglobalThis.controller={go,openSheet,closeSheet,r1StorageWatch,r1FillReceipt,R1_FILL,R1_REFRESH,Poll,getContext:()=>sheetCtx};';
  vm.runInNewContext(script, context, { filename: 'real-storage-refresh-controller.js', timeout: 1000 });
  const controller = context.controller;
  const settle = async () => {
    for (let i = 0; i < 12; i++) {
      if (state.changed) { state.changed = false; state.observers.forEach(fn => fn()); }
      await new Promise(resolve => setImmediate(resolve));
    }
  };
  const action = async (fn, event = 'click', kind = 'document') => {
    state.action = true; fn(); dispatch(kind, event); state.action = false; await settle();
  };
  return { state, context, controller, settle,
    page: (name, event = 'click') => action(() => controller.go(name), event),
    disk: name => action(() => controller.openSheet('disk/' + name)),
    sheet: name => action(() => controller.openSheet(name)),
    close: () => action(() => controller.closeSheet(true)),
    async visible(value) { context.document.hidden = !value; dispatch('document', 'visibilitychange'); await settle(); },
    async route(hash, event) { await action(() => { context.location.hash = hash; }, event, 'window'); },
    async ticks(count = 3) { for (let i = 0; i < count; i++) { state.clock += 1100; controller.Poll.tick(); await settle(); } },
    gate(stage) {
      let release;
      const gate = { entered: false, promise: new Promise(resolve => { release = resolve; }), release: () => release() };
      state.gates.set(stage, gate); return gate;
    },
  };
}

(async () => {
  {
    const m = model(); await m.page('overview');
    check('有效缓存的概览零采集', m.state.posts.length === 0);
    await m.page('storage');
    check('有效缓存进入 storage 立即一次 refresh', m.state.posts.length === 1 && m.state.posts[0].url.endsWith('/refresh'));
    await m.ticks(5); check('停留不周期采集且没有内部重建循环', m.state.posts.length === 1);
    await m.disk('sda'); check('storage 上的 disk A 独立刷新一次', m.state.posts.length === 2);
    const epoch = m.controller.R1_REFRESH.epoch;
    await m.ticks(3); check('真实 helper 同 key 重建不产生新进入', m.state.posts.length === 2 && m.controller.R1_REFRESH.epoch === epoch);
    await m.disk('sdb'); check('直接切 disk B 再刷新一次', m.state.posts.length === 3);
    await m.close(); check('关闭 disk 返回 storage 刷新一次', m.state.posts.length === 4);
    await m.visible(false); await m.ticks(); check('hidden 不刷新', m.state.posts.length === 4);
    await m.visible(true); check('返回同一 storage 刷新一次', m.state.posts.length === 5);
    check('请求均严格空对象且没有 force', m.state.posts.every(row => row.body === '{}') && m.state.maxActive === 1);
  }
  {
    const m = model(); await m.page('overview');
    for (const name of ['cpu', 'mem', 'net/eth0', 'keys']) { await m.sheet(name); await m.ticks(); }
    check('非磁盘抽屉不主动刷新也不借 overview fill', m.state.posts.length === 0);
    await m.page('storage'); const before = m.state.posts.length;
    await m.sheet('cpu'); await m.ticks(); check('storage 被 CPU 抽屉覆盖时不刷新', m.state.posts.length === before);
    await m.page('network'); await m.page('storage', 'keydown');
    check('无一秒 tick 的真实键盘进入也刷新', m.state.posts.length === before + 1);
    await m.route('#/network', 'hashchange'); await m.route('#/storage', 'popstate');
    check('hash 和历史返回走真实路由并刷新', m.state.posts.length === before + 2);
  }
  {
    const m = model(); m.state.missing = true; m.context.S.o.disks_detail.forEach(row => {
      row.total = null; row.used = null; row.capacity_sampled_at = null;
    });
    await m.route('#/overview/disk/sda', 'hashchange'); await m.ticks();
    check('overview 上 disk 抽屉仅 refresh，不额外 fill', m.state.posts.length === 1 && m.state.posts.every(row => row.url.endsWith('/refresh')));
  }
  for (const mode of ['busy', 'standby', 'invalid']) {
    const m = model(); m.state.mode = mode; await m.page('storage'); await m.ticks(5);
    check('负结果本次进入不重试：' + mode, m.state.posts.length === 1 && !m.controller.R1_REFRESH.inflight);
    await m.page('network'); await m.page('storage');
    check('负结果下次进入仍可尝试：' + mode, m.state.posts.length === 2);
  }
  for (const stage of ['auth', 'post', 'overview', 'smart']) {
    const m = model(); const gate = m.gate(stage);
    await m.route('#/overview/disk/sda', 'hashchange');
    check('真实异步阶段确实在途：' + stage, gate.entered && m.controller.R1_REFRESH.inflight);
    const before = m.state.posts.length;
    await m.disk('sdb'); await m.disk('sda');
    check('ABA 期间不并发新采集：' + stage, m.state.posts.length === before && m.state.maxActive <= 1);
    gate.release(); await m.settle(); await m.ticks();
    check('旧阶段不投影新进入：' + stage,
      (stage !== 'overview' || !m.state.renders.includes(1))
      && (stage === 'auth' || !m.state.builds.some(row => !row.action && row.post === 1 && row.type === 'disk'))
      && m.state.maxActive === 1 && !m.controller.R1_REFRESH.inflight);
    check('释放后新进入恰好一次采集：' + stage, m.state.posts.length === (stage === 'auth' ? 1 : 2));
  }
  {
    const m = model(); m.state.owner = false; await m.page('storage'); await m.ticks();
    check('非 owner 不发主动 POST', m.state.posts.length === 0);
  }
  console.log('PASS: ' + checks.length + ' checks; real routing/helper/controller, memory-only transport');
})().catch(error => { console.error('FAIL: ' + (error.stack || error)); process.exitCode = 1; });
