'use strict';

/*
 * R1 Toolbox small screen.
 *
 * Touch-first UI for the 376x960 panel; the layout comes from the approved
 * prototype in docs/prototypes.  All values are read from the host service on
 * loopback (or the current origin when the demo serves this file over HTTP).
 * Writes use the local control token and are confirmed by the host.
 */
(() => {
  const API = /^https?:$/.test(location.protocol) ? location.origin + '/api' : 'http://127.0.0.1:18731/api';
  const POLL_INTERVAL = 2000;
  const REQUEST_TIMEOUT = 4500;
  const ACTION_TIMEOUT = 60000;
  const controlToken = (() => {
    try {
      if (typeof window.require !== 'function') return '';
      const value = window.require('process').env.R1_TOOLBOX_CONTROL_TOKEN;
      return typeof value === 'string' ? value : '';
    } catch (_) { return ''; }
  })();

  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const list = value => Array.isArray(value) ? value : [];
  const obj = value => value && typeof value === 'object' ? value : {};
  const num = (value, low = 0, high = null) => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return null;
    if (value < low || (high !== null && value > high)) return null;
    return value;
  };
  const pct = value => num(value, 0, 100);

  const state = {
    view: 'overview',
    history: [],
    data: {},
    online: false,
    brightness: null,
    brightnessDirty: false,
    trendRange: '1h',
    trendCache: {},
    trendPending: {},
    modal: null,
    powerDeadline: null,
    powerOperation: null,
    pairing: null,
    pairingDeadline: null,
    containerId: null,
    toastTimer: null,
    countdownTimer: null,
    sleeping: false,
    swallowUntil: 0,
    swipeUntil: 0,
    actionBusy: false,
    live: { cpu: [], mem: [], temp: [], power: [], rx: [], tx: [], disk: [] },
    lastTs: null,
  };

  const mainTabs = ['overview', 'storage', 'network', 'apps', 'controls'];
  const titles = {
    overview: '概览', storage: '存储', network: '网络', apps: '应用', controls: '控制中心',
    metrics: '核心指标', processor: '处理器与内存', trends: '历史趋势', thermal: '温度与风扇',
    containers: 'Docker 容器', container: '容器详情', vms: '虚拟机', ports: '监听端口', web: '网页设置',
  };
  const parentTabs = {
    metrics: 'overview', processor: 'overview', trends: 'overview', thermal: 'overview',
    containers: 'apps', container: 'apps', vms: 'apps', ports: 'network', web: 'controls',
  };

  const icons = {
    overview: '<path d="M3 10 12 3l9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
    storage: '<rect x="4" y="3" width="16" height="18" rx="4"/><path d="M4 14h16M8 18h1m3 0h1"/>',
    network: '<path d="M7 3v18m-4-4 4 4 4-4M17 21V3m-4 4 4-4 4 4"/>',
    apps: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    controls: '<path d="M3 6h18M3 18h18M3 12h18"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="9" cy="18" r="2"/>',
    back: '<path d="m15 5-7 7 7 7"/>',
    fan: '<circle cx="12" cy="12" r="2"/><path d="M10 10C2 11 3 2 9 3c4 0 5 4 3 7m2 1c3-8 10-2 6 3-2 3-6 2-7 0m-2 0c6 6-3 10-5 5-2-4 1-7 4-7"/>',
  };
  const icon = (name, extra = '') => `<svg ${extra} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.apps}</svg>`;

  const glyphs = {
    '0': ['01110', '11011', '11011', '11011', '11011', '11011', '01110'],
    '1': ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
    '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'],
    '3': ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
    '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'],
    '5': ['11111', '10000', '10000', '11110', '00001', '00001', '11110'],
    '6': ['01110', '10000', '10000', '11110', '10001', '10001', '01110'],
    '7': ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
    '8': ['01110', '10001', '10001', '01110', '10001', '10001', '01110'],
    '9': ['01110', '10001', '10001', '01111', '00001', '00001', '01110'],
    ':': ['0', '1', '1', '0', '1', '1', '0'],
    '.': ['0', '0', '0', '0', '0', '1', '1'],
    '-': ['000', '000', '000', '111', '000', '000', '000'],
  };

  /** Draw a number with local SVG dots; never depends on fonts or the network. */
  function dotNumber(value, cls = '') {
    let x = 0;
    let circles = '';
    for (const char of String(value)) {
      const glyph = glyphs[char] || glyphs['-'];
      for (let row = 0; row < 7; row += 1) {
        for (let col = 0; col < glyph[row].length; col += 1) {
          circles += `<circle cx="${x + col * 4 + 2}" cy="${row * 4 + 2}" r="1.37" opacity="${glyph[row][col] === '1' ? 1 : 0.055}"/>`;
        }
      }
      x += glyph[0].length * 4 + 5;
    }
    return `<span class="dot-number ${cls}" role="img" aria-label="${esc(value)}"><svg viewBox="0 0 ${Math.max(1, x - 5)} 28" fill="currentColor" aria-hidden="true">${circles}</svg></span>`;
  }

  /** A tiny trend from real samples; a flat line when there is not enough data. */
  function spark(values, cls = '', fill = false) {
    const points = list(values).filter(value => num(value) !== null);
    const data = points.length >= 2 ? points.slice(-48) : [];
    if (!data.length) return `<svg class="spark ${cls}" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="暂无足够采样"></svg>`;
    const max = Math.max(1, ...data) * 1.08;
    const min = Math.min(0, ...data);
    const span = max - min || 1;
    const xy = data.map((value, index) => `${(index * 300 / (data.length - 1)).toFixed(1)},${(95 - (value - min) / span * 80).toFixed(1)}`);
    const d = 'M' + xy.join(' L');
    const last = data[data.length - 1];
    return `<svg class="spark ${cls}" viewBox="0 0 300 100" preserveAspectRatio="none" role="img" aria-label="最近采样趋势">${fill ? `<path d="${d} L300,100 L0,100Z" fill="currentColor" opacity=".07"/>` : ''}<path d="${d}" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><circle cx="300" cy="${(95 - (last - min) / span * 80).toFixed(1)}" r="2.8" fill="currentColor"/></svg>`;
  }

  const bar = (percent, color = 'var(--lime)', cls = '') =>
    `<div class="bar ${cls}" aria-hidden="true"><span style="--value:${Math.max(0, Math.min(100, Number(percent) || 0))}%;--fill:${color}"></span></div>`;

  const menu = (view, title, subtitle) =>
    `<button type="button" class="menu-row" data-view="${view}"><span><strong>${esc(title)}</strong>${subtitle ? `<small>${esc(subtitle)}</small>` : ''}</span><span class="chevron" aria-hidden="true">›</span></button>`;

  function bytes(value, decimals = 1) {
    if (num(value, 0) === null) return '--';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let amount = value;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(index === 0 || amount >= 100 ? 0 : decimals)} ${units[index]}`;
  }

  function speed(value) {
    if (num(value, 0) === null) return ['--', ''];
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s'];
    let amount = value;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return [amount.toFixed(amount >= 100 || index === 0 ? 0 : 1), units[index]];
  }

  function duration(seconds) {
    if (num(seconds, 0) === null) return '--';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds % 86400 / 3600);
    if (days) return `${days} 天 ${hours} 小时`;
    if (hours) return `${hours} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
    return `${Math.floor(seconds / 60)} 分钟`;
  }

  const tempText = value => num(value, -40, 200) === null ? '--' : `${Math.round(value)}°`;
  const percentText = value => pct(value) === null ? '--' : String(Math.round(value));

  function cpuTemp() {
    const status = obj(state.data.status);
    const direct = num(obj(status.cpu).temp_c, -40, 200);
    if (direct !== null) return direct;
    const device = obj(obj(state.data.insights).device);
    const rows = list(device.thermal).filter(row => row && /coretemp|k10temp/.test(String(row.driver || '')) && num(row.celsius, -40, 200) !== null);
    return rows.length ? Math.max(...rows.map(row => row.celsius)) : null;
  }

  function cpuModel() {
    const insights = obj(state.data.insights);
    return String(obj(obj(insights.device).cpu).model || obj(insights.system).cpu_model || '处理器');
  }

  function cpuCores() {
    const insights = obj(state.data.insights);
    return num(obj(insights.system).cpu_logical_cores, 1) || num(obj(obj(insights.device).cpu).logical_cores, 1);
  }

  function memory() {
    const status = obj(state.data.status);
    const insights = obj(state.data.insights);
    const basic = obj(status.memory);
    const full = obj(obj(insights.system).memory);
    return {
      total: num(full.total_bytes, 1) || num(basic.total_bytes, 1),
      used: num(full.used_bytes, 0) || num(basic.used_bytes, 0),
      percent: pct(basic.usage_percent) ?? pct(full.usage_percent),
      available: num(full.available_bytes, 0),
      swapTotal: num(full.swap_total_bytes, 0),
      swapUsed: num(full.swap_used_bytes, 0),
    };
  }

  function pool() {
    const disks = list(obj(state.data.status).disks);
    let total = 0;
    let used = 0;
    for (const disk of disks) {
      if (num(disk.total_bytes, 1) !== null && num(disk.used_bytes, 0) !== null) {
        total += disk.total_bytes;
        used += disk.used_bytes;
      }
    }
    return { total, used, percent: total > 0 ? used / total * 100 : null, count: disks.length };
  }

  const containers = () => list(obj(obj(state.data.insights).containers).items);
  const dstate = () => obj(obj(state.data.insights).containers);
  const vms = () => list(obj(obj(state.data.insights).vms).items);
  const vmState = () => obj(obj(state.data.insights).vms);
  const fanPolicy = () => obj(state.data.fan);
  const lightPolicy = () => obj(state.data.light);
  const monitoring = () => obj(state.data.monitoring);
  const canWrite = () => Boolean(controlToken && state.online && !state.actionBusy);

  const statusLevel = temp => {
    const thresholds = obj(monitoring().settings).thresholds || {};
    const warn = num(thresholds.cpu_temp_warn, 0, 120) ?? 70;
    const critical = num(thresholds.cpu_temp_critical, 0, 120) ?? 80;
    if (temp === null) return '';
    if (temp >= critical) return 'crit';
    if (temp >= warn) return 'warn';
    return 'ok';
  };

  // ------------------------------------------------------------------ data
  async function request(path, payload = null) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), payload ? ACTION_TIMEOUT : REQUEST_TIMEOUT);
    try {
      const options = { method: payload ? 'POST' : 'GET', cache: 'no-store', credentials: 'omit', signal: controller.signal };
      if (payload) {
        options.headers = { 'Content-Type': 'application/json', 'X-R1-Token': controlToken };
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(API + path, options);
      let data;
      try { data = await response.json(); } catch (_) { throw new Error('本机服务返回了无法读取的数据'); }
      if (!response.ok || data.ok === false) {
        throw new Error(typeof data.reason === 'string' ? data.reason : typeof data.error === 'string' ? data.error : `请求失败（${response.status}）`);
      }
      return data;
    } catch (error) {
      if (error && error.name === 'AbortError') throw new Error('本机服务响应超时');
      if (error instanceof TypeError) throw new Error('无法连接本机服务');
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function poll() {
    if (document.hidden) return schedulePoll();
    const tasks = [
      ['status', '/status'], ['toolbox', '/toolbox'], ['insights', '/insights'],
      ['fan', '/fan-policy'], ['light', '/light-policy'], ['monitoring', '/monitoring'], ['web', '/web-config'],
    ];
    const results = await Promise.allSettled(tasks.map(([, path]) => request(path)));
    let success = 0;
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        state.data[tasks[index][0]] = result.value;
        success += 1;
      }
    });
    const wasOnline = state.online;
    state.online = success > 0;
    if (!state.online && wasOnline) toast('与 NAS 的连接已中断，正在重试', true);
    const toolbox = obj(state.data.toolbox);
    if (!state.brightnessDirty && pct(obj(toolbox.screen).brightness) !== null) {
      state.brightness = Math.round(toolbox.screen.brightness);
    }
    state.sleeping = obj(toolbox.screen).asleep === true;
    feed();
    syncPower();
    if (state.view === 'trends') loadHistory(state.trendRange).catch(() => {});
    softRender();
    schedulePoll();
  }

  function schedulePoll() {
    window.clearTimeout(schedulePoll.timer);
    schedulePoll.timer = window.setTimeout(poll, POLL_INTERVAL);
  }

  function feed() {
    const status = obj(state.data.status);
    const ts = Date.parse(status.timestamp || '') || Date.now();
    if (state.lastTs === ts) return;
    state.lastTs = ts;
    const push = (key, value) => {
      if (num(value, 0) === null) return;
      const buffer = state.live[key];
      buffer.push(Math.round(value * 10) / 10);
      if (buffer.length > 60) buffer.shift();
    };
    push('cpu', obj(status.cpu).usage_percent);
    push('mem', obj(status.memory).usage_percent);
    push('temp', cpuTemp());
    push('power', obj(monitoring().power).cpu_package_w);
    push('rx', obj(status.network).rx_bytes_per_sec);
    push('tx', obj(status.network).tx_bytes_per_sec);
    const poolValue = pool().percent;
    push('disk', poolValue);
    const insightHistory = list(obj(state.data.insights).history);
    if (insightHistory.length && state.live.cpu.length < 3) {
      state.live.cpu = insightHistory.map(row => num(row.cpu_usage_percent, 0, 100)).filter(value => value !== null).slice(-40);
      state.live.mem = insightHistory.map(row => num(row.memory_usage_percent, 0, 100)).filter(value => value !== null).slice(-40);
    }
  }

  function syncPower() {
    const power = obj(obj(state.data.toolbox).power);
    const pending = obj(power.pending);
    if (pending && num(pending.seconds_remaining, 0) !== null) {
      state.powerOperation = pending.operation || state.powerOperation;
      state.powerDeadline = Date.now() + pending.seconds_remaining * 1000;
    } else if (!power.executing) {
      state.powerDeadline = null;
      state.powerOperation = null;
    }
  }

  // ------------------------------------------------------------------ render
  function softRender() {
    if (state.modal || state.sleeping) { renderPower(); syncSleep(); return; }
    if (document.activeElement && document.activeElement.id === 'brightness') return;
    const body = $('screen-body');
    render(body ? body.scrollTop : 0);
  }

  function deviceMarkup(view) {
    const main = mainTabs.includes(view);
    const tab = main ? view : parentTabs[view];
    const online = state.online;
    const canBack = !main || (view === 'controls' && state.history.length > 0);
    return `<header class="screen-header">${canBack ? `<button type="button" class="icon-button header-back" data-action="back" aria-label="返回上一页">${icon('back')}</button>` : '<span class="header-mark" aria-hidden="true">R1</span>'}<h1 class="screen-title">${esc(titles[view])}</h1><span class="badge ${online ? '' : 'warn'}">${online ? '本机在线' : '离线'}</span><button type="button" class="icon-button" data-view="${view === 'controls' ? 'web' : 'controls'}" aria-label="${view === 'controls' ? '打开网页设置' : '打开控制中心'}">${icon('controls')}</button></header><div class="screen-body" id="screen-body"><section aria-label="${esc(titles[view])}">${views[view]()}</section></div><nav class="screen-nav" aria-label="小屏页面">${mainTabs.map(name => `<button type="button" class="nav-button" data-tab="${name}" ${tab === name ? 'aria-current="page"' : ''}>${icon(name)}<span>${name === 'controls' ? '控制' : titles[name]}</span></button>`).join('')}</nav>`;
  }

  function render(scrollTop = 0) {
    $('device').innerHTML = deviceMarkup(state.view);
    const body = $('screen-body');
    if (body) body.scrollTop = scrollTop;
    tickClock();
    syncBrightness();
    renderPower();
    syncSleep();
  }

  function tickClock() {
    const now = new Date();
    const time = now.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
    document.querySelectorAll('[data-clock-digits]').forEach(node => {
      if (node.dataset.time !== time) {
        node.innerHTML = dotNumber(time, 'clock-digits');
        node.dataset.time = time;
      }
    });
    document.querySelectorAll('[data-clock-seconds]').forEach(node => { node.textContent = String(now.getSeconds()).padStart(2, '0'); });
    document.querySelectorAll('[data-clock-date]').forEach(node => { node.textContent = now.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }); });
  }

  function toast(message, error = false) {
    const node = $('toast');
    if (!node) return;
    window.clearTimeout(state.toastTimer);
    node.textContent = message;
    node.dataset.error = String(error);
    node.hidden = false;
    state.toastTimer = window.setTimeout(() => { node.hidden = true; }, error ? 5000 : 3200);
  }

  function syncSleep() {
    const overlay = $('sleep');
    if (!overlay) return;
    if (!state.sleeping) { overlay.hidden = true; return; }
    overlay.innerHTML = `<span data-clock-digits>${dotNumber(new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' }))}</span><span>轻触唤醒</span>`;
    overlay.hidden = false;
  }

  function renderPower() {
    const node = $('power-status');
    if (!node) return;
    const toolbox = obj(state.data.toolbox);
    const power = obj(toolbox.power);
    if (power.executing) {
      node.hidden = false;
      node.innerHTML = '<span>电源操作已交给宿主系统</span>';
      return;
    }
    if (state.powerDeadline === null) { node.hidden = true; node.innerHTML = ''; return; }
    const seconds = Math.max(0, Math.ceil((state.powerDeadline - Date.now()) / 1000));
    node.hidden = false;
    node.innerHTML = `<span>${state.powerOperation === 'poweroff' ? '关闭' : '重新启动'} · ${seconds} 秒后执行</span><button type="button" data-action="cancel-power">取消电源操作</button>`;
    window.clearTimeout(renderPower.timer);
    renderPower.timer = window.setTimeout(renderPower, 500);
  }

  function syncBrightness() {
    const value = state.brightness;
    const input = $('brightness');
    if (!input) return;
    if (value !== null && !state.brightnessDirty) input.value = String(value);
    const output = document.querySelector('[data-brightness-value]');
    if (output) output.innerHTML = value === null ? '--<small style="font-size:15px;color:var(--dim)"> %</small>' : `${input.value}<small style="font-size:15px;color:var(--dim)"> %</small>`;
  }

  // ------------------------------------------------------------------ views
  function clockBlock() {
    const status = obj(state.data.status);
    const host = status.hostname || 'R1-NAS';
    return `<div class="clock-block"><div class="clock-topline"><span class="clock-date" data-clock-date></span><span class="clock-live"><span class="dot"></span>${state.online ? 'LOCAL HOST' : 'OFFLINE'}</span></div><div class="clock-line"><span data-clock-digits>${dotNumber('--:--', 'clock-digits')}</span><span class="clock-seconds" data-clock-seconds>--</span></div><p class="clock-device">${esc(host)} <span style="padding:0 5px">/</span> ${status.uptime_seconds != null ? '已运行 ' + esc(duration(status.uptime_seconds)) : '等待采样'}</p></div>`;
  }

  function overview() {
    const status = obj(state.data.status);
    const cpu = obj(status.cpu);
    const mem = memory();
    const disk = pool();
    const temp = cpuTemp();
    const level = statusLevel(temp);
    const net = obj(status.network);
    const down = speed(net.rx_bytes_per_sec);
    const up = speed(net.tx_bytes_per_sec);
    const badge = !state.online ? '本机服务离线' : status.available === false ? '数据不可用' : level === 'crit' ? '温度过高' : level === 'warn' ? '一项需要关注' : temp === null ? '部分指标未知' : '运行正常';
    const headline = !state.online ? '暂时无法连接 NAS' : temp === null ? '温度暂不可读' : level === 'crit' ? 'CPU 温度过高' : level === 'warn' ? 'CPU 温度稍高' : '各项指标正常';
    const copy = !state.online ? '每几秒自动重试；保留上次可用读数。' : temp === null ? 'CPU 温度传感器暂未返回读数。' : '其余已采集指标正常';
    return `${clockBlock()}<article class="card status-card"><div class="thermal-ring"><svg viewBox="0 0 84 84" aria-hidden="true"><circle cx="42" cy="42" r="35" fill="none" stroke="#ffffff0d" stroke-width="5"/><circle cx="42" cy="42" r="35" fill="none" stroke="${level === 'crit' ? 'var(--red)' : level === 'warn' ? 'var(--orange)' : 'var(--green)'}" stroke-width="5" stroke-linecap="round" stroke-dasharray="${temp === null ? 0 : Math.min(220, Math.max(0, temp / 100 * 220)).toFixed(0)} 220"/></svg><span class="ring-value ${level === 'crit' ? 'orange' : ''}">${tempText(temp)}</span></div><div><span class="badge ${level === 'warn' || level === 'crit' || !state.online ? 'warn' : ''}">${esc(badge)}</span><h2 class="status-title">${esc(headline)}</h2><p class="status-copy">${esc(copy)}</p></div></article><div class="section-label tight"><h2>AT A GLANCE / 核心指标</h2><button type="button" data-view="metrics" aria-label="查看全部核心指标">全部 ›</button></div><article class="card metric-stack"><button type="button" class="metric-row" data-view="processor" aria-label="CPU ${percentText(cpu.usage_percent)}%，查看处理器详情"><div class="metric-info"><div class="metric-name">CPU</div><div class="metric-meta">${esc(cpuModel())}${cpuCores() ? ' · ' + cpuCores() + ' 核' : ''}</div></div>${spark(state.live.cpu, 'small', false)}<span class="metric-number lime">${dotNumber(percentText(cpu.usage_percent))}<small>%</small></span></button><button type="button" class="metric-row" data-view="processor" aria-label="内存 ${percentText(mem.percent)}%，查看内存详情"><div class="metric-info"><div class="metric-name">内存</div><div class="metric-meta">${bytes(mem.used)} / ${bytes(mem.total)}</div></div>${spark(state.live.mem, 'small purple', false)}<span class="metric-number purple">${dotNumber(percentText(mem.percent))}<small>%</small></span></button></article><button type="button" class="card card-button inline-summary" data-view="storage"><div><div class="metric-name">存储空间</div><span class="caption">${disk.count ? bytes(disk.used) + ' / ' + bytes(disk.total) : '-- / --'}</span></div><div><span class="summary-number orange">${percentText(disk.percent)}<small>%</small></span>${bar(disk.percent, statusLevel(disk.percent) === 'warn' ? 'var(--orange)' : 'var(--green)', 'mini-bar')}</div><span class="chevron" aria-hidden="true">›</span></button><button type="button" class="card card-button flow-summary" data-view="network"><div class="flow-cell"><p class="flow-label">↓ 下载</p><p class="flow-value green">${esc(down[0])} <small>${esc(down[1])}</small></p></div><div class="flow-separator"></div><div class="flow-cell"><p class="flow-label">↑ 上传</p><p class="flow-value">${esc(up[0])} <small>${esc(up[1])}</small></p></div><span class="chevron" aria-hidden="true">›</span></button><div class="quick-links"><button type="button" data-view="trends">历史趋势</button><button type="button" data-view="thermal">温度与风扇</button></div>`;
  }

  function metrics() {
    const status = obj(state.data.status);
    const disk = pool();
    const net = obj(status.network);
    const down = speed(net.rx_bytes_per_sec);
    const temp = cpuTemp();
    const power = obj(monitoring().power);
    const load = num(list(obj(obj(state.data.insights).system).load_average)[0], 0);
    const cards = [
      ['CPU', percentText(obj(status.cpu).usage_percent), '%', `${cpuCores() || '--'} 核 · 负载 ${load === null ? '--' : load.toFixed(2)}`, 'cpu', 'lime', 'processor'],
      ['内存', percentText(memory().percent), '%', `${bytes(memory().used)} / ${bytes(memory().total)}`, 'mem', 'purple', 'processor'],
      ['存储', percentText(disk.percent), '%', disk.count ? `已用 ${bytes(disk.used)}` : '未发现数据盘', 'disk', 'orange', 'storage'],
      ['下载', down[0], down[1], `${esc(net.interface || '--')}`, 'rx', 'green', 'network'],
      ['CPU 温度', tempText(temp), '', 'CPU 封装温度', 'temp', 'orange', 'thermal'],
    ];
    if (power.available === true && num(power.cpu_package_w, 0) !== null) {
      cards.push(['CPU 封装功率', String(Math.round(power.cpu_package_w * 10) / 10), 'W', 'RAPL 实测 · 非整机功耗', 'power', '', 'trends']);
    }
    const stamp = status.timestamp ? new Date(status.timestamp).toLocaleTimeString('zh-CN', { hour12: false }) : '--';
    return `<div class="detail-lead"><p class="kicker">AT A GLANCE</p><h2>${cards.length} 项核心读数</h2><p>${state.online ? '真实快照 · ' + esc(stamp) : '等待连接本机服务'}</p></div>${cards.map(([label, value, unit, sub, chart, color, target]) => `<button type="button" class="card card-button" data-view="${target}"><div class="card-top"><h2>${esc(label)}</h2><span class="chevron" aria-hidden="true">›</span></div><div class="big-value ${color}">${dotNumber(value)}<small>${esc(unit)}</small></div><p class="caption" style="margin:12px 0">${esc(sub)}</p>${spark(state.live[chart] || [], color)}</button>`).join('')}`;
  }

  function processor() {
    const insights = obj(state.data.insights);
    const system = obj(insights.system);
    const cores = list(system.cpu_cores);
    const mem = memory();
    const load = num(list(system.load_average)[0], 0);
    return `<article class="card"><div class="card-top"><p class="kicker">PROCESSOR</p><span class="badge">负载 ${load === null ? '--' : esc(load.toFixed(2))}</span></div><div class="big-value lime">${dotNumber(percentText(system.cpu_usage_percent ?? obj(state.data.status).cpu?.usage_percent))}<small>%</small></div><p class="body-copy" style="margin-top:16px">${esc(cpuModel())}</p><p class="caption">${cpuCores() || '--'} 逻辑核心${num(obj(obj(insights.device).cpu).physical_cores, 1) ? ' · ' + obj(obj(insights.device).cpu).physical_cores + ' 物理核心' : ''} · ${esc(obj(obj(insights.device).cpu).architecture || '')}</p><div class="divider"></div>${cores.slice(0, 8).map((core, index) => `<div class="core-row"><span>核心 ${index + 1}</span>${bar(core.usage_percent, statusLevel(core.usage_percent) === 'warn' ? 'var(--orange)' : 'var(--lime)')}<span class="mono">${percentText(core.usage_percent)}%</span></div>`).join('')}${cores.length ? '' : '<p class="caption">分核读数暂不可用。</p>'}${spark(state.live.cpu, 'lime')}</article><article class="card"><div class="card-top"><p class="kicker">MEMORY</p><span class="badge neutral">${bytes(mem.total)}</span></div><div class="big-value purple">${dotNumber(percentText(mem.percent))}<small>%</small></div><p class="body-copy" style="margin:16px 0">${bytes(mem.used)} / ${bytes(mem.total)}</p>${bar(mem.percent, 'var(--purple)')}<div class="stat-row"><span class="muted">可用内存</span><span class="value">${bytes(mem.available)}</span></div><div class="stat-row"><span class="muted">交换空间</span><span class="value">${mem.swapTotal ? bytes(mem.swapUsed) + ' / ' + bytes(mem.swapTotal) : '--'}</span></div></article>${menu('trends', '查看历史趋势', 'CPU、内存与温度曲线')}`;
  }

  function driveCard(disk, index) {
    const temp = num(disk.temp_c, -40, 200);
    const standby = disk.standby === true;
    const size = bytes(disk.size_bytes);
    const health = obj(disk.health);
    const smart = health.passed === false ? 'SMART 异常' : health.passed === true ? 'SMART 正常' : 'SMART 未读取';
    return `<article class="card"><div class="drive-row"><div class="drive-symbol" style="color:${standby ? 'var(--dim)' : temp !== null && temp >= 50 ? 'var(--orange)' : 'var(--green)'}"><svg viewBox="0 0 28 58" aria-hidden="true" fill="currentColor">${Array.from({ length: 10 }, (_, row) => `<circle cx="8" cy="${6 + row * 5}" r="1.7"/><circle cx="14" cy="${6 + row * 5}" r="1.7"/><circle cx="20" cy="${6 + row * 5}" r="1.7"/>`).join('')}</svg></div><div class="drive-content"><div class="drive-title"><h3>${esc(disk.id || 'disk ' + (index + 1))}</h3><span class="drive-temperature ${standby ? 'muted' : temp !== null && temp >= 50 ? 'orange' : 'green'}">${standby ? '--' : tempText(temp)}</span></div><p class="drive-meta">${esc(disk.model || '未知型号')} · ${esc(size)}</p>${standby ? '<p class="drive-standby">休眠中 · 保留休眠，不主动唤醒</p>' : `<p class="caption">${esc(smart)}${num(disk.temperature_critical_c, 0) ? ' · 临界 ' + disk.temperature_critical_c + '°C' : ''}</p>`}</div></div></article>`;
  }

  function storage() {
    const disk = pool();
    const drives = list(obj(obj(state.data.insights).disks).items);
    const mounts = list(obj(state.data.status).disks);
    return `<article class="card storage-total"><div class="card-top"><p class="kicker">STORAGE POOL</p><span class="badge ${disk.percent !== null && disk.percent >= 85 ? 'warn' : ''}">${disk.count ? disk.count + ' 个挂载' : '未发现数据盘'}</span></div><div class="big-value orange">${dotNumber(percentText(disk.percent))}<small>% 已用</small></div>${bar(disk.percent, 'linear-gradient(90deg,#8ed7b2,#c7ed89,#f2a275)')}<div class="capacity-label"><span class="mono">${disk.count ? bytes(disk.used) + ' / ' + bytes(disk.total) : '-- / --'}</span><span>数据盘</span></div></article>${mounts.map((mount, index) => `<article class="card"><div class="card-top"><h2>${esc(mount.mount)}</h2><span class="badge neutral">${percentText(mount.usage_percent)}%</span></div>${bar(mount.usage_percent, 'var(--green)')}<p class="caption" style="margin-top:12px">${bytes(mount.used_bytes)} / ${bytes(mount.total_bytes)}</p></article>`).join('')}<div class="section-label"><h2>PHYSICAL DRIVES / 物理磁盘</h2><span>${drives.length} 块</span></div>${drives.map(driveCard).join('')}${drives.length ? '' : '<p class="info-note">未读取到物理磁盘；SMART 只使用后台缓存，不主动唤醒休眠盘。</p>'}<p class="info-note">SMART 使用缓存读数。休眠盘温度未知，不因查看页面唤醒。</p>`;
  }

  function network() {
    const status = obj(state.data.status);
    const net = obj(status.network);
    const insights = obj(state.data.insights);
    const deviceNet = obj(obj(insights.device).network);
    const links = list(deviceNet.interfaces || deviceNet);
    const interfaces = list(insights.interfaces);
    const down = speed(net.rx_bytes_per_sec);
    const up = speed(net.tx_bytes_per_sec);
    const primary = links.find(row => row.name === net.interface) || links[0] || {};
    return `<article class="card"><div class="card-top"><p class="kicker">ACTIVE INTERFACE</p><span class="badge ${state.online ? '' : 'warn'}"><span class="dot"></span>${state.online ? '已连接' : '离线'}</span></div><div class="stat-row" style="padding-top:0"><span>${esc(net.interface || '--')}</span><span class="value" style="font-size:19px">${esc(net.ipv4 || '--')}</span></div><div class="network-interface"><span>${num(primary.speed_mbps, 1) ? primary.speed_mbps + ' Mbps' : '速率未知'}${primary.duplex ? ' · ' + esc(primary.duplex) : ''}</span><span>IPv4</span></div></article><article class="card network-card"><div class="card-top"><h2>↓ 下载速率</h2><span class="label">实时</span></div><div class="big-value green">${dotNumber(down[0])}<small>${esc(down[1])}</small></div>${spark(state.live.rx, 'green', true)}</article><article class="card network-card"><div class="card-top"><h2>↑ 上传速率</h2><span class="label">实时</span></div><div class="big-value purple">${dotNumber(up[0])}<small>${esc(up[1])}</small></div>${spark(state.live.tx, 'purple', true)}</article>${interfaces.map(item => `<article class="card"><div class="card-top"><h2>${esc(item.name)}</h2><span class="badge neutral">${esc(item.operstate || '--')}</span></div><div class="stat-row"><span class="muted">IPv4</span><span class="value" style="font-size:16px">${esc(list(item.ipv4).join('、') || '--')}</span></div><div class="stat-row"><span class="muted">MAC</span><span class="value" style="font-size:16px">${esc(item.mac || '--')}</span></div></article>`).join('')}${menu('ports', '监听端口', '宿主监听与容器端口映射')}`;
  }

  function appCard(item, clickable = true) {
    const running = item.state === 'running';
    return `<${clickable ? 'button type="button" data-action="container" data-id="' + esc(item.id) + '"' : 'article'} class="card app-card ${clickable ? 'card-button' : ''}"><div class="app-line"><div class="app-icon" ${running ? '' : 'style="color:var(--dim)"'}>${esc(String(item.name || '?').slice(0, 1).toUpperCase())}</div><div class="app-heading"><h3>${esc(item.name || String(item.id).slice(0, 12))}</h3><p class="app-image">${esc(item.image || '')}</p></div><span class="${running ? 'green' : 'muted'}" aria-label="${running ? '运行中' : '已停止'}"><span class="dot"></span></span></div><div class="app-resources"><span>CPU ${item.cpu_percent != null ? Number(item.cpu_percent).toFixed(1) + '%' : '--'}</span><span>内存 ${item.memory_bytes != null ? bytes(item.memory_bytes) : '--'}</span></div><p class="app-port">${esc(item.ports || '无端口映射')}</p></${clickable ? 'button' : 'article'}>`;
  }

  function apps() {
    const docker = dstate();
    const listRows = containers();
    const running = listRows.filter(item => item.state === 'running').length;
    const vmInfo = vmState();
    return `<article class="card"><div class="card-top"><p class="kicker">CONTAINERS</p><span class="badge ${docker.available ? '' : 'warn'}"><span class="dot"></span>${docker.available ? 'Docker' : '不可用'}</span></div><div class="app-summary lime">${dotNumber(String(running))}<span class="slash">/</span><span class="total">${listRows.length}</span></div><div class="app-count-meta"><span>运行中</span><span>${listRows.length - running} 个已停止</span></div>${docker.available ? '' : `<p class="caption" style="margin-top:14px">${esc(docker.reason || 'Docker 状态暂不可读')}</p>`}</article><div class="section-label"><h2>RUNNING / 容器预览</h2><button type="button" data-view="containers">全部 ›</button></div>${listRows.slice(0, 3).map(item => appCard(item)).join('')}${listRows.length ? '' : '<p class="info-note">没有可显示的容器。</p>'}${vmInfo.available ? menu('vms', '虚拟机', `${vms().filter(vm => vm.state === 'running').length} / ${vms().length} 运行中`) : ''}`;
  }

  function containersView() {
    const docker = dstate();
    const rows = containers();
    return `<div class="detail-lead"><p class="kicker">DOCKER CONTAINERS</p><h2>${rows.filter(item => item.state === 'running').length} 个容器运行中</h2><p>${rows.length} 个已发现 · 点击查看详情与操作</p></div>${rows.map(item => appCard(item)).join('')}${rows.length ? '' : `<p class="info-note">${esc(docker.reason || '没有可显示的容器')}</p>`}<p class="info-note">容器 CPU 可超过 100%。未知统计显示为 --。</p>`;
  }

  function containerView() {
    const item = containers().find(row => row.id === state.containerId);
    if (!item) return '<p class="info-note">容器不存在或列表已刷新，请返回应用页重新选择。</p>';
    const actions = list(item.available_actions);
    const label = { start: '启动', stop: '停止', restart: '重启' };
    return `${appCard(item, false)}<article class="card"><div class="card-top"><h2>容器状态</h2><span class="badge ${item.state === 'running' ? '' : 'neutral'}">${esc(item.state || '未知')}</span></div><div class="stat-row"><span class="muted">CPU 使用率</span><span class="value">${item.cpu_percent != null ? Number(item.cpu_percent).toFixed(1) + '%' : '--'}</span></div><div class="stat-row"><span class="muted">内存使用</span><span class="value">${item.memory_bytes != null ? bytes(item.memory_bytes) : '--'}</span></div><div class="stat-row"><span class="muted">端口文本</span><span class="value mono-wrap" style="font-size:16px">${esc(item.ports || 'Docker 未提供端口文本')}</span></div>${item.protected ? '<p class="info-note">这是 Toolbox 的保护容器，不在此提供启停操作。</p>' : ''}</article>${actions.length && !item.protected ? `<div class="action-stack">${actions.map(action => `<button type="button" class="action-button ${action === 'stop' ? 'danger' : ''}" data-action="container-act" data-id="${esc(item.id)}" data-operation="${esc(action)}">${label[action] || action}</button>`).join('')}</div>` : ''}<p class="info-note">操作前会重新核验容器 ID、状态和保护标记，并要求确认。</p>`;
  }

  function vmsView() {
    const info = vmState();
    if (!info.available) return `<p class="info-note">${esc(info.reason || '宿主未提供 libvirt 服务，虚拟机模块已隐藏。')}</p>`;
    return `<article class="card"><p class="kicker">VIRTUAL MACHINES</p><div class="app-summary lime" style="margin-top:20px">${dotNumber(String(vms().filter(vm => vm.state === 'running').length))}<span class="slash">/</span><span class="total">${vms().length}</span></div><p class="caption" style="margin-top:16px">运行中 / 已发现</p></article>${vms().map(vm => {
      const running = ['running', 'blocked'].includes(vm.state);
      return `<article class="card"><div class="card-top"><h2>${esc(vm.name || vm.id)}</h2><span class="badge ${running ? '' : 'neutral'}">${esc(vm.state || '未知')}</span></div><p class="caption mono-wrap">${esc(vm.id)}</p><div class="action-stack">${running ? `<button type="button" class="action-button" data-action="vm-act" data-id="${esc(vm.id)}" data-operation="reboot">重启</button><button type="button" class="action-button danger" data-action="vm-act" data-id="${esc(vm.id)}" data-operation="shutdown">关机</button>` : `<button type="button" class="action-button" data-action="vm-act" data-id="${esc(vm.id)}" data-operation="start">启动</button>`}</div></article>`;
    }).join('')}<p class="info-note">虚拟机操作需要确认，结果以刷新后的状态为准。</p>`;
  }

  function thermal() {
    const insights = obj(state.data.insights);
    const device = obj(insights.device);
    const temps = list(device.thermal).filter(row => num(row.celsius, -40, 200) !== null);
    const fans = list(device.fans);
    const policy = fanPolicy();
    const channels = obj(policy.channels);
    return `<article class="card"><div class="card-top"><p class="kicker">THERMAL SENSORS</p><span class="badge ${statusLevel(cpuTemp()) === 'warn' || statusLevel(cpuTemp()) === 'crit' ? 'warn' : 'neutral'}">${temps.length} 个测点</span></div>${temps.map(row => {
      const value = row.celsius;
      return `<div class="temperature-row"><div class="temperature-top"><span>${esc(row.label || row.channel || '传感器')}${row.driver ? ' · ' + esc(row.driver) : ''}</span><b class="${value >= 55 ? 'orange' : 'green'}">${tempText(value)}</b></div>${bar(Math.max(0, Math.min(100, (value - 20) / 60 * 100)), value >= 55 ? 'var(--orange)' : 'var(--green)')}</div>`;
    }).join('')}${temps.length ? '' : '<p class="caption">暂未读取到温度传感器。</p>'}</article><article class="card"><div class="card-top"><p class="kicker">FANS</p><span class="badge neutral">${policy.active ? '独立温控' : '原厂管理'}</span></div>${fans.map(fan => `<div class="fan-row"><span class="fan-label">${esc(fan.label || fan.channel || '风扇')}</span><span class="fan-value">${num(fan.rpm, 0) === null ? '--' : fan.rpm} <small>RPM</small></span></div>`).join('')}${channels.cpu ? `<div class="divider"></div>${['cpu', 'system'].map(name => {
      const channel = obj(channels[name]);
      return `<div class="fan-row"><span class="fan-label">${name === 'cpu' ? 'CPU' : '系统'}温控</span><span class="fan-value" style="font-size:17px">${channel.active ? tempText(channel.reference_c) + ' · ' + percentText(channel.target_percent) + '%' : '原厂管理'}</span></div>`;
    }).join('')}` : ''}${!fans.length && !channels.cpu ? '<p class="caption">未获得转速反馈。控制目标百分比不代表实测转速。</p>' : ''}${menu('web', '在网页配置温控', '曲线、阈值与温度来源')}</article>`;
  }

  function chartCard(name, unit, series, color, label) {
    const points = series || [];
    const last = points.length ? points[points.length - 1] : null;
    const shown = last === null ? ['--', unit] : unit === 'B/s' ? speed(last) : [formatSeries(last), unit];
    return `<article class="card chart-card"><div class="card-top"><h2 class="chart-name">${esc(name)}</h2><span class="chart-value ${color}">${esc(shown[0])}<small> ${esc(shown[1])}</small></span></div><div class="chart"><div class="chart-grid"></div>${spark(points, color, true)}</div><div class="chart-times"><span>${esc(label)}</span><span>现在</span></div></article>`;
  }

  function formatSeries(value) {
    if (value === null || value === undefined) return '--';
    return Math.abs(value) >= 100 ? String(Math.round(value)) : (Math.round(value * 10) / 10).toString();
  }

  function trends() {
    const cache = state.trendCache[state.trendRange];
    const rangeLabel = { '1h': '过去 1 小时', '6h': '过去 6 小时', '24h': '过去 24 小时' }[state.trendRange];
    const at = state.trendRange === '24h' ? '昨日' : state.trendRange === '6h' ? '6 小时前' : '1 小时前';
    if (!cache) return `<div class="detail-lead"><p class="kicker">TRENDS / 历史趋势</p><p style="margin-top:8px">${esc(rangeLabel)} · 正在读取历史</p></div><div class="tabs" aria-label="趋势时间范围">${['1h', '6h', '24h'].map(range => `<button type="button" data-range="${range}" aria-pressed="${state.trendRange === range}">${range}</button>`).join('')}</div><p class="info-note">历史不足时保留断点，不补算未监控时段。</p>`;
    const seriesOf = id => list(cache.series).find(row => row.id === id)?.points?.map(point => point[1]).filter(value => num(value) !== null) || [];
    const unavailable = cache.available === false;
    return `<div class="detail-lead"><p class="kicker">TRENDS / 历史趋势</p><p style="margin-top:8px">${esc(rangeLabel)} · ${unavailable ? '历史数据暂不可用' : '采样缺失时保留断点'}</p></div><div class="tabs" aria-label="趋势时间范围">${['1h', '6h', '24h'].map(range => `<button type="button" data-range="${range}" aria-pressed="${state.trendRange === range}">${range}</button>`).join('')}</div>${unavailable ? `<p class="info-note">${esc(cache.reason || '宿主历史接口不可用，未补算或伪造采样。')}</p>` : ''}${chartCard('CPU', '%', seriesOf('cpu_usage_percent'), 'lime', at)}${chartCard('内存', '%', seriesOf('memory_usage_percent'), 'purple', at)}${chartCard('网络下载', 'B/s', seriesOf('rx_bytes_per_sec'), 'green', at)}${chartCard('磁盘读取', 'B/s', seriesOf('disk_read_bytes_per_sec'), 'purple', at)}${chartCard('CPU 温度', '°C', seriesOf('cpu_temperature_c'), 'orange', at)}${chartCard('CPU 封装功率', 'W', seriesOf('cpu_package_w'), 'green', at)}`;
  }

  async function loadHistory(range, force = false) {
    if (state.trendPending[range]) return;
    if (!force && state.trendCache[range]) return;
    if (force) delete state.trendCache[range];
    state.trendPending[range] = true;
    try {
      const value = await request('/history?range=' + encodeURIComponent(range));
      state.trendCache[range] = value;
      if (state.view === 'trends' && state.trendRange === range) softRender();
    } catch (error) {
      state.trendCache[range] = { available: false, series: [], reason: error && error.message ? error.message : '历史接口不可用' };
      if (state.view === 'trends' && state.trendRange === range) softRender();
    } finally {
      delete state.trendPending[range];
      if (state.view === 'trends' && state.trendRange !== range) loadHistory(state.trendRange).catch(() => {});
    }
  }

  function ports() {
    const listeners = list(obj(state.data.insights).ports);
    const docker = containers().filter(item => item.ports);
    return `<div class="detail-lead"><p class="kicker">HOST LISTENING PORTS</p><h2>宿主监听</h2><p>读取 /proc/net · 不推测服务归属</p></div>${listeners.map(row => `<article class="card port-row"><span class="port-number">${esc(row.port)}</span><div><p class="port-name">${esc(String(row.protocol || 'tcp').toUpperCase())}</p><p class="port-note mono">${esc(row.address || '')}</p></div></article>`).join('')}${listeners.length ? '' : '<p class="info-note">未读取到监听端口。</p>'}<div class="section-label"><h2>DOCKER / 端口映射</h2></div>${docker.map(item => `<article class="card"><div class="card-top"><h3>${esc(item.name)}</h3><span class="${item.state === 'running' ? 'green' : 'muted'}"><span class="dot"></span></span></div><p class="mono mono-wrap" style="font-size:18px">${esc(item.ports)}</p></article>`).join('')}${docker.length ? '' : '<p class="info-note">没有带端口映射的容器。</p>'}<p class="info-note">Docker 映射与宿主监听来自不同数据源，仅展示各自的原始语义。</p>`;
  }

  function controls() {
    const toolbox = obj(state.data.toolbox);
    const screen = obj(toolbox.screen);
    const caps = obj(toolbox.capabilities);
    const available = obj(caps.screen).available === true;
    const brightness = state.brightness ?? pct(screen.brightness);
    const policy = fanPolicy();
    const light = lightPolicy();
    const lightConfig = obj(light.config);
    const target = obj(light.target);
    const powerPending = state.powerDeadline !== null;
    return `<article class="card"><div class="control-heading"><h2 style="font-size:20px;font-weight:400">屏幕亮度</h2><output data-brightness-value for="brightness">${brightness ?? '--'}<small style="font-size:15px;color:var(--dim)"> %</small></output></div><label class="sr-only" for="brightness">屏幕亮度</label><input class="brightness" id="brightness" type="range" min="5" max="100" value="${brightness ?? 50}" ${available && canWrite() ? '' : 'disabled'}><div class="control-hint"><span>暗</span><span>亮</span></div><button type="button" class="action-button" data-action="sleep" style="margin-top:18px" ${available && canWrite() ? '' : 'disabled'}>立即息屏</button>${available ? '' : `<p class="caption" style="margin-top:10px">${esc(obj(caps.screen).reason || '背光能力尚未验证')}</p>`}</article>${menu('thermal', '风扇状态', policy.active ? '独立温控运行中 · 只读' : '原厂管理 · 只读')}<article class="card"><div class="card-top"><h2>机身灯光</h2><span class="badge neutral"><span class="control-color" style="background:${esc(target.color || lightConfig.manual?.color || '#7caf98')}"></span>${light.active ? '联动中' : '原厂主题'}</span></div><p class="caption">${light.active ? '目标亮度 ' + percentText(target.brightness) + '%' : '由原厂主题管理机身灯'}</p><button type="button" class="action-button" data-view="web" style="margin-top:18px">在网页调整灯光</button></article>${menu('web', '网页设置', '温控、灯光、登录与配对')}<div class="section-label"><h2>POWER / 电源</h2><span>${powerPending ? '倒计时中' : '需确认'}</span></div><div class="action-stack"><button type="button" class="action-button" data-action="reboot" ${canWrite() && !powerPending ? '' : 'disabled'}>重新启动 NAS</button><button type="button" class="action-button danger" data-action="shutdown" ${canWrite() && !powerPending ? '' : 'disabled'}>关闭 NAS</button></div><p class="info-note">确认后有 5 秒可取消。关机后不会自动开机。</p>`;
  }

  function web() {
    const web = obj(state.data.web);
    const addresses = list(web.addresses);
    const ipv4 = obj(obj(state.data.status).network).ipv4;
    const address = addresses[0] || (ipv4 ? `http://${ipv4}:18732` : '');
    const pairing = state.pairing;
    const remaining = pairing ? Math.max(0, Math.ceil((state.pairingDeadline - Date.now()) / 1000)) : 0;
    return `<div class="detail-lead"><p class="kicker">CONTINUE ON THE WEB</p><h2>大一点的屏幕，<br>更完整的设置。</h2></div><article class="card"><div class="card-top"><h2>网页访问地址</h2><span class="badge neutral">${web.enabled ? '已启用' : '未启用'}</span></div><p class="web-address">${esc(address || '--')}</p><p class="body-copy">${address ? '在同一局域网的手机或电脑上打开。' : '网页服务暂未返回可访问地址。'}</p><div class="divider"></div><p class="caption">网页默认公开只读；修改设置需要一次性配对码或登录口令。</p></article><article class="card"><p class="kicker">PAIRING CODE</p>${pairing ? `<p class="pairing-code">${esc(pairing.code)}</p><p class="caption" style="text-align:center">${remaining} 秒内有效 · 仅可使用一次</p>` : '<p class="body-copy" style="margin:16px 0">需要修改网页设置时，生成五位配对码并在网页输入。</p>'}<button type="button" class="action-button primary" data-action="pairing" ${canWrite() && web.enabled ? '' : 'disabled'}>${pairing ? '重新生成配对码' : '生成一次性配对码'}</button><p class="caption" style="margin-top:12px">代码只显示在本机小屏，不会发送到网络。</p></article><article class="card"><p class="kicker">AVAILABLE ON THE WEB</p><div class="stat-row"><span>风扇温控</span><span class="caption">曲线与来源</span></div><div class="stat-row"><span>机身灯光</span><span class="caption">颜色与联动</span></div><div class="stat-row"><span>登录与配对</span><span class="caption">保护写操作</span></div></article><p class="info-note">局域网 HTTP 未加密，请只在可信网络使用；公开监控与配对共用同一端口。</p>`;
  }

  const views = { overview, metrics, processor, storage, network, apps, containers: containersView, container: containerView, vms: vmsView, thermal, trends, ports, controls, web };

  // ------------------------------------------------------------------ actions
  function openConfirm({ title, message, accept, danger = false, onAccept }) {
    if (state.modal) return;
    state.modal = { title, message, accept, danger, onAccept, trigger: document.activeElement };
    const modal = $('modal');
    if (!modal) return;
    modal.innerHTML = `<section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-description"><p class="kicker">PLEASE CONFIRM</p><h2 id="confirm-title">${esc(title)}</h2><p id="confirm-description">${esc(message)}</p><div class="action-stack"><button type="button" class="action-button ${danger ? 'danger' : ''}" data-action="modal-accept">${esc(accept)}</button><button type="button" class="action-button primary" data-action="modal-cancel">取消</button></div></section>`;
    modal.hidden = false;
    const cancel = modal.querySelector('[data-action="modal-cancel"]');
    if (cancel) cancel.focus();
  }

  function closeConfirm() {
    const trigger = state.modal?.trigger;
    state.modal = null;
    const modal = $('modal');
    if (modal) { modal.hidden = true; modal.innerHTML = ''; }
    if (trigger && trigger.focus) trigger.focus();
  }

  async function action(name, payload = {}, success = '操作已完成') {
    if (!canWrite()) { toast('当前为只读或离线状态，无法执行操作', true); return false; }
    if (state.actionBusy) return false;
    state.actionBusy = true;
    try {
      const result = await request('/action', Object.assign({ action: name }, payload));
      if (result.state) state.data.toolbox = result.state;
      if (result.insights) state.data.insights = result.insights;
      if (result.web) state.data.web = result.web;
      if (success) toast(success);
      syncPower();
      return result;
    } catch (error) {
      toast(error && error.message ? error.message : '操作未完成，请稍后重试', true);
      return false;
    } finally {
      state.actionBusy = false;
      softRender();
    }
  }

  async function setBrightness() {
    const input = $('brightness');
    if (!input) return;
    const value = Number(input.value);
    state.brightnessDirty = true;
    state.brightness = value;
    const ok = await action('screen.brightness', { value }, '亮度已调整');
    state.brightnessDirty = false;
    if (!ok && pct(obj(obj(state.data.toolbox).screen).brightness) !== null) state.brightness = Math.round(obj(obj(state.data.toolbox).screen).brightness);
    softRender();
  }

  function requestPower(operation) {
    if (state.powerDeadline !== null) { toast('请先取消当前电源倒计时。', true); return; }
    const label = operation === 'poweroff' ? '关闭 NAS' : '重新启动 NAS';
    openConfirm({
      title: label + '？',
      message: '此操作会影响所有用户、容器和文件访问。确认后进入 5 秒可取消倒计时。',
      accept: '确认' + (operation === 'poweroff' ? '关闭' : '重启'),
      danger: operation === 'poweroff',
      onAccept: async () => {
        const result = await action('power.request', { operation, confirmed: true }, '电源操作已安排，可在倒计时结束前取消');
        if (result) { syncPower(); softRender(); }
      },
    });
  }

  function cancelPower() {
    action('power.cancel', {}, '已取消电源操作').then(() => { state.powerDeadline = null; state.powerOperation = null; softRender(); });
  }

  function containerAction(id, operation) {
    const item = containers().find(row => row.id === id);
    if (!item) return;
    const label = { start: '启动', stop: '停止', restart: '重启' }[operation] || operation;
    openConfirm({
      title: label + ' ' + (item.name || '容器') + '？',
      message: '操作前会重新核验容器 ID、当前状态与保护标记；结果以刷新后的列表为准。',
      accept: '确认' + label,
      danger: operation === 'stop',
      onAccept: async () => {
        await action('container.action', { id, operation, confirmed: true }, '已提交容器操作，等待状态刷新');
      },
    });
  }

  function vmAction(id, operation) {
    const item = vms().find(row => row.id === id);
    if (!item) return;
    const label = { start: '启动', shutdown: '关机', reboot: '重启' }[operation] || operation;
    openConfirm({
      title: label + ' ' + (item.name || '虚拟机') + '？',
      message: '虚拟机操作需要确认，结果以刷新后的状态为准。',
      accept: '确认' + label,
      danger: operation === 'shutdown',
      onAccept: async () => {
        await action('vm.action', { id, operation, confirmed: true }, '已提交虚拟机操作');
      },
    });
  }

  async function generatePairing() {
    const result = await action('web.pairing', { rotate: true }, null);
    if (!result || !result.web) return;
    const code = String(result.web.code || '');
    if (!/^\d{5}$/.test(code)) { toast('配对码生成失败，请重试', true); return; }
    state.pairing = { code };
    state.pairingDeadline = Date.now() + Math.max(1, num(result.web.expires_in, 1) ?? 180) * 1000;
    softRender();
    window.clearInterval(state.pairingTimer);
    state.pairingTimer = window.setInterval(() => {
      if (!state.pairing) return;
      if (Date.now() >= state.pairingDeadline) {
        state.pairing = null;
        window.clearInterval(state.pairingTimer);
        softRender();
      } else if (state.view === 'web') softRender();
    }, 1000);
  }

  // ------------------------------------------------------------------ navigation
  function navigate(view, root = false) {
    if (!views[view]) return;
    closeConfirm();
    if (state.pairing && view !== 'web') { state.pairing = null; window.clearInterval(state.pairingTimer); }
    if (root) state.history = [];
    else if (view !== state.view) {
      const body = $('screen-body');
      state.history.push({ view: state.view, scroll: body ? body.scrollTop : 0 });
    }
    state.view = view;
    render();
    if (view === 'trends') loadHistory(state.trendRange).catch(() => {});
  }

  function back() {
    const previous = state.history.pop();
    if (state.pairing && (!previous || previous.view !== 'web')) { state.pairing = null; window.clearInterval(state.pairingTimer); }
    if (previous) { state.view = previous.view; render(previous.scroll); }
    else navigate(parentTabs[state.view] || 'overview', true);
  }

  document.addEventListener('click', event => {
    if (Date.now() < state.swipeUntil) { event.preventDefault(); event.stopPropagation(); return; }
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.tab) { navigate(button.dataset.tab, true); return; }
    if (button.dataset.view) {
      if (button.dataset.action === 'container') state.containerId = button.dataset.id;
      navigate(button.dataset.view);
      return;
    }
    if (button.dataset.range) {
      const body = $('screen-body');
      state.trendRange = button.dataset.range;
      render(body ? body.scrollTop : 0);
      loadHistory(state.trendRange, true).catch(() => {});
      return;
    }
    const act = button.dataset.action;
    if (act === 'back') back();
    if (act === 'sleep') { if (state.powerDeadline !== null) { toast('电源倒计时期间请先取消操作。', true); return; } action('screen.sleep', {}, null).then(result => { if (result) { state.sleeping = true; syncSleep(); } }); }
    if (act === 'wake') {
      if (Date.now() < state.swallowUntil) return;
      state.swallowUntil = Date.now() + 650;
      action('screen.wake', {}, null).then(result => { if (result) { state.sleeping = false; syncSleep(); } });
    }
    if (act === 'reboot' || act === 'shutdown') requestPower(act === 'shutdown' ? 'poweroff' : 'reboot');
    if (act === 'cancel-power') cancelPower();
    if (act === 'container') { state.containerId = button.dataset.id; navigate('container'); }
    if (act === 'container-act') containerAction(button.dataset.id, button.dataset.operation);
    if (act === 'vm-act') vmAction(button.dataset.id, button.dataset.operation);
    if (act === 'pairing') generatePairing();
    if (act === 'modal-cancel') closeConfirm();
    if (act === 'modal-accept') {
      const submit = state.modal?.onAccept;
      closeConfirm();
      if (submit) submit();
    }
  });

  document.addEventListener('input', event => {
    if (event.target.id === 'brightness') {
      state.brightness = Number(event.target.value);
      state.brightnessDirty = true;
      const output = document.querySelector('[data-brightness-value]');
      if (output) output.innerHTML = `${state.brightness}<small style="font-size:15px;color:var(--dim)"> %</small>`;
    }
  });
  document.addEventListener('change', event => {
    if (event.target.id === 'brightness') setBrightness();
  });

  document.addEventListener('keydown', event => {
    if (state.modal) {
      const modal = $('modal');
      const buttons = modal ? [...modal.querySelectorAll('button')] : [];
      if (event.key === 'Escape') { event.preventDefault(); closeConfirm(); }
      if (event.key === 'Tab') {
        const current = buttons.indexOf(document.activeElement);
        if (event.shiftKey && current <= 0) { event.preventDefault(); buttons.at(-1)?.focus(); }
        else if (!event.shiftKey && (current === -1 || current === buttons.length - 1)) { event.preventDefault(); buttons[0]?.focus(); }
      }
      return;
    }
    if (event.key === 'Escape' && state.view !== 'overview') { event.preventDefault(); back(); }
    if (event.key === 'ArrowLeft' && !mainTabs.includes(state.view)) { event.preventDefault(); back(); }
  });

  let touchStart = null;
  document.addEventListener('touchstart', event => {
    if (event.touches.length !== 1 || state.modal || event.target.closest('input,#modal,#sleep')) { touchStart = null; return; }
    const touch = event.touches[0];
    touchStart = { x: touch.clientX, y: touch.clientY };
  }, { passive: true });
  document.addEventListener('touchend', event => {
    if (!touchStart || state.modal) return;
    const touch = event.changedTouches[0];
    const dx = touch.clientX - touchStart.x;
    const dy = touch.clientY - touchStart.y;
    touchStart = null;
    if (Math.abs(dx) < 72 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    state.swipeUntil = Date.now() + 450;
    if (!mainTabs.includes(state.view)) { if (dx > 0) back(); return; }
    const next = mainTabs[(mainTabs.indexOf(state.view) + (dx < 0 ? 1 : -1) + mainTabs.length) % mainTabs.length];
    navigate(next, true);
  }, { passive: true });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  window.setInterval(tickClock, 1000);
  render();
  poll();
})();
