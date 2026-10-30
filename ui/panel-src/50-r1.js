/* =====================================================================
   R1 TOOLBOX ADDITIONS
   ---------------------------------------------------------------------
   fnmonitor's own modules and styles are used unchanged.  Everything in this
   file only *adds* what the HikSemi R1 can do and fnOS cannot:

     · 双路风扇（CPU / 系统）独立温控的实时状态与编辑
     · 机身灯光（四种原生灯效 / 夜间亮度）
     · 小屏亮度、息屏与 NAS 电源操作
     · 本地告警阈值、历史保留与流量统计范围
     · 可选登录口令、服务开关与独立 API 密钥
     · 可选影视追更排序的所有者检测、应用与撤销

   Controls use /api/r1/*; optional media rules use /api/plugins/media-sort.
   Both are served by the host
   service in runtime/web_config.py.  No hardware is touched by the browser.
   ===================================================================== */

const R1_MODE_TXT = { manual: '固定输出', temperature: '手动 · 温控曲线' };
const R1_NATIVE_FAN_TXT = { auto: '自动', low: '低档', medium: '中档', high: '高档' };
const R1_LEVEL_TXT = { quiet: '静音', normal: '普通', full: '全速' };
const R1_EFFECT_TXT = { 0: '关闭', 1: '常亮', 2: '彩虹', 3: '呼吸' };

/** Keep bond/slave interface rows intact; only trusted R1 totals replace sums. */
window.panelNetworkAggregate = (overview, field) => {
  const aggregate = overview && overview.net_aggregate;
  if (!aggregate || aggregate.schema_version !== 1 || aggregate.source !== 'monitoring_filtered_counters'
      || !['rx_rate', 'tx_rate'].includes(field)) return undefined;
  const value = aggregate[field];
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
};

const R1 = {
  st: null, loading: false,
  async load() {
    if (this.loading) return this.st;
    this.loading = true;
    try {
      const j = await API.get('/api/r1');
      this.st = j; S.r1 = j;
      return j;
    } finally {
      this.loading = false;
    }
  },
  /** POST a write; opens the pairing dialog when the host asks for it. */
  async post(path, body) {
    try {
      return await API.post(path, body || {});
    } catch (e) {
      const needPair = e.status === 403 && /need_pairing/.test(String(e.message || ''));
      if (needPair) {
        // 配对成功后自动重试一次，避免用户还要再点一遍原来的保存按钮。
        const unlocked = await R1.openPair('修改设备设置需要授权');
        if (unlocked) return API.post(path, body || {});
        const err = new Error('需要在小屏生成五位配对码后解锁');
        err.status = 403; err.handled = true; throw err;
      }
      throw e;
    }
  },
  async refresh() {
    try { await this.load(); } catch (e) { /* keep the last known state */ }
    renderNow(); syncSettings();
  },
  fan: () => (R1.st && R1.st.fan) || {},
  light: () => (R1.st && R1.st.light) || {},
  control: () => (R1.st && R1.st.control) || {},
  auth: () => (R1.st && R1.st.auth) || {},
  sources: () => ((R1.st && R1.st.fan && R1.st.fan.sources) || []),
  /** 五位配对码：只能在本机小屏生成，网页只负责输入。 */
  openPair(reason) {
    if ($('#r1Pair')) return Promise.resolve(false);
    return new Promise(resolve => {
    const el = mount(`<div class="fullmask" id="r1Pair" role="dialog" aria-modal="true" aria-labelledby="r1PairT">
      <div class="card fm-card">
        <div class="fm-ico">${ic('lock', 'i-lg')}</div>
        <h2 id="r1PairT">解锁设置</h2>
        <p>${esc(reason || '修改设置需要授权')}：在 NAS 小屏「设置 → 网页配置」点击生成五位配对码。配对码 3 分钟有效、只能使用一次。</p>
        <label class="field" style="margin:18px 0 10px"><input id="r1PairCode" inputmode="numeric" maxlength="5" placeholder="00000" autocomplete="one-time-code" aria-label="五位配对码"></label>
        <p class="st-crit" id="r1PairErr" hidden></p>
        <div class="fm-btns"><button class="btn" type="button" data-close="1">取消</button><button class="btn primary" type="button" data-go="1">解锁</button></div>
        <p class="faint" style="font-size:12px;margin-top:12px">网页不会保存配对码；服务重启后需要重新生成。若已开启登录口令，直接登录即可。</p>
      </div></div>`);
    const input = el.querySelector('#r1PairCode'), err = el.querySelector('#r1PairErr');
    const onKey = event => { if (event.key === 'Escape') finish(false); };
    const finish = value => { document.removeEventListener('keydown', onKey); el.remove(); resolve(value); };
    document.addEventListener('keydown', onKey);
    el.querySelector('[data-close]').onclick = () => finish(false);
    const submit = async () => {
      const code = input.value.trim();
      if (!/^[0-9]{5}$/.test(code)) { err.hidden = false; setText(err, '请输入 5 位数字，前导零也算'); return; }
      try {
        await API.post('/api/pair', { code });
        finish(true);
        toast('已解锁设置', { icon: 'check' });
        await R1.refresh();
      } catch (e) {
        err.hidden = false;
        setText(err, String(e.message || e).slice(0, 80));
      }
    };
    el.querySelector('[data-go]').onclick = submit;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
    document.body.append(el);
    setTimeout(() => input.focus(), 30);
    });
  },
  /** Promise-based confirmation; resolves true/false. */
  ask(title, text, danger) {
    return new Promise(resolve => {
      const el = mount(`<div class="fullmask" role="dialog" aria-modal="true" aria-labelledby="r1AskT">
        <div class="card fm-card"><div class="fm-ico">${ic(danger ? 'alert' : 'info', 'i-lg')}</div>
        <h2 id="r1AskT">${esc(title)}</h2><p>${text || ''}</p>
        <div class="fm-btns"><button class="btn" type="button" data-no="1">取消</button><button class="btn ${danger ? 'danger' : 'primary'}" type="button" data-yes="1">确认执行</button></div>
        </div></div>`);
      const onKey = event => { if (event.key === 'Escape') done(false); };
      const done = value => { document.removeEventListener('keydown', onKey); el.remove(); resolve(value); };
      document.addEventListener('keydown', onKey);
      el.querySelector('[data-no]').onclick = () => done(false);
      el.querySelector('[data-yes]').onclick = () => done(true);
      document.body.append(el);
    });
  },
  /** Shared save flow for fan/light editors. */
  async save(path, body, label) {
    try {
      const j = await R1.post(path, body);
      if (!j || j.ok !== true) throw new Error((j && j.error) || '未确认应用结果');
      toast(j.pending ? `已提交 · ${label}，等待运行状态确认` : `已保存 · ${label}`, { key: 'save' });
      await R1.refresh();
      return j;
    } catch (e) {
      if (!e.handled) toast(`${label}失败 · ${String(e.message).slice(0, 90)}`, { icon: 'alert', ms: 5200 });
      return null;
    }
  },
  chanName: name => (name === 'cpu' ? 'CPU 风扇' : '系统风扇'),
};

/** 原厂快照只读缓存；兼容旧版本放在 control 下的同一份状态。 */
function fanOfficial() {
  return R1.fan().official || ((R1.control().fans || {}).official) || {};
}

function fanNativeLabel(name) {
  if (name === 'cpu') return '原厂自动';
  const label = R1_NATIVE_FAN_TXT[fanOfficial().mode];
  return label ? '原厂 · ' + label : '原厂 · 档位未读取';
}

function fanRunningMode(name) {
  const live = (R1.fan().channels || {})[name] || {};
  if (!live.mode) return '运行状态未读取';
  if (!live.active && R1.fan().recovery_confirmed === false) return '原厂恢复待确认';
  return live.active ? (R1_MODE_TXT[live.mode] || '手动') : fanNativeLabel(name);
}

/** 运行状态与保存的配置分别显示，不能把尚未成功的手动请求当作接管。 */
function fanLiveState(name) {
  const fan = R1.fan(), live = (fan.channels || {})[name] || {};
  const configured = ((fan.config || {}).channels || {})[name] || {};
  if (fan.busy) return '切换中';
  if (fan.recovery_confirmed === false) return '恢复待确认';
  if (!live.mode) return '运行状态未读取';
  if (live.active) return live.mode === 'manual' ? '固定输出接管中' : '手动接管中';
  if (configured.mode && configured.mode !== 'vendor') return (configured.mode === 'manual' ? '固定输出未生效 · ' : '手动未生效 · ') + fanNativeLabel(name);
  if (name === 'system' && configured.vendor_mode && configured.vendor_mode !== fanOfficial().mode) return '档位未生效 · ' + fanNativeLabel(name);
  return fanNativeLabel(name);
}

/* 状态轮询：10 秒；概览、温度与设置页可见时更新 */
Poll.add('r1', async () => { await R1.load(); renderNow(); syncSettings(); }, 10000,
  () => ['overview', 'thermal', 'settings'].includes(S.page));

/* ---------------------------------------------------------------------
   模块 01 · 双路温控
   --------------------------------------------------------------------- */
defineModule({
  id: 'r1cool', page: 'thermal', kicker: 'R1 COOLING', title: '双路风扇',
  dek: 'CPU 与系统风扇各自选择模式、温度来源与输出',
  actions: () => {
    const b = mount(`<button class="link-btn" type="button">配置温控${ic('chev', 'i-sm')}</button>`);
    b.onclick = () => focusGroup('r1-fan');
    return b;
  },
  build(body, m) {
    body.innerHTML = `<div class="card pad r1-chans" data-ref="chans"></div>
      <p class="sub r1-note" data-ref="note"></p>`;
    m.x = collect(body);
  },
  update(o, m) {
    const f = R1.fan(), channels = f.channels || {};
    const stable = Object.values(channels).map(c => [c.mode, c.active, c.target_percent, c.reference_c,
      c.level, c.reason, c.source_label, c.rpm]);
    const sig = JSON.stringify([stable, f.config, fanOfficial(), f.busy, f.capability, f.last_error, f.active, f.recovery_confirmed]);
    if (m.sig !== sig) {
      m.sig = sig;
      m.x.chans.innerHTML = ['cpu', 'system'].map(name => `<div class="r1-chan" data-ch="${name}">
          <div class="r1-chan-head"><b>${R1.chanName(name)}</b>
            <span class="pill" data-ref="st-${name}"><span class="dot"></span><span data-ref="stt-${name}"></span></span></div>
          <div class="r1-chan-grid">
            <div><div class="eyebrow">参考温度</div><div class="num"><span data-ref="t-${name}">--</span><span class="unit">℃</span></div></div>
            <div><div class="eyebrow">请求输出</div><div class="num"><span data-ref="p-${name}">--</span><span class="unit">%</span></div></div>
            <div><div class="eyebrow">模式</div><div class="num" style="font-size:15px"><span data-ref="m-${name}">--</span></div></div>
          </div>
          <div class="r1-chan-foot"><span data-ref="s-${name}"></span><span class="faint" data-ref="l-${name}"></span></div>
          <div class="bar" style="height:6px;margin-top:10px"><i data-ref="b-${name}"></i></div>
        </div>`).join('');
      const refs = collect(m.x.chans);
      Object.assign(m.x, refs);
    }
    ['cpu', 'system'].forEach(name => {
      const c = channels[name] || {};
      const lv = c.level && c.level !== 'off' ? (R1_LEVEL_TXT[c.level] || '') : '';
      setText(m.x['t-' + name], ok(c.reference_c) ? F.int(c.reference_c) : '--');
      setText(m.x['p-' + name], ok(c.target_percent) ? F.int(c.target_percent) : '--');
      setText(m.x['m-' + name], fanRunningMode(name));
      setText(m.x['s-' + name], c.source_label ? '来源 · ' + c.source_label : (c.active ? '等待温度来源' : f.recovery_confirmed === false ? '原厂管理恢复待确认' : '由原厂固件管理'));
      setText(m.x['l-' + name], [lv, c.reason].filter(Boolean).join(' · '));
      setW(m.x['b-' + name], ok(c.target_percent) ? c.target_percent : 0);
      if (m.x['st-' + name]) m.x['st-' + name].className = 'pill' + (c.active ? ' ok' : '');
      if (m.x['stt-' + name]) setText(m.x['stt-' + name], fanLiveState(name));
    });
    const cap = f.capability || {};
    setHTML(m.x.note, f.last_error
      ? `<span class="st-crit">${ic('alert', 'i-sm')}${esc(f.last_error)}</span>`
      : cap.available === true
        ? '百分比是请求输出，不是实测 RPM；温度取所选来源的最高值，保护规则优先于用户阈值。'
        : `手动接管暂不可用：${esc(cap.reason || '设备尚未满足独立调速条件')}。系统原厂档位可用性单独检查。`);
    const active = Object.values(channels).some(c => c.active);
    setHTML(m.r.dek, f.recovery_confirmed === false ? '原厂管理恢复尚未确认，请检查风扇运行状态。' : active
      ? `独立调速运行中${f.busy ? ' · 正在切换' : ''}；系统原厂档位与独立控制分别生效。`
      : 'CPU 由原厂自动管理；系统风扇可切换原厂档位，手动模式独立接管。');
  },
});

/* ---------------------------------------------------------------------
   模块 02 · 机身灯光
   --------------------------------------------------------------------- */
defineModule({
  id: 'r1light', page: 'thermal', kicker: 'R1 LIGHT', title: '机身灯光',
  dek: '四种原生灯效：关闭、常亮、彩虹、呼吸',
  actions: () => {
    const b = mount(`<button class="link-btn" type="button">灯光设置${ic('chev', 'i-sm')}</button>`);
    b.onclick = () => focusGroup('r1-light');
    return b;
  },
  build(body, m) {
    body.innerHTML = `<div class="card pad r1-light">
      <div class="r1-light-main"><i class="r1-swatch" data-ref="sw"></i><div style="min-width:0">
        <div class="eyebrow">当前目标</div><div class="r1-light-state" data-ref="state">--</div>
        <div class="sub" data-ref="sub"></div></div></div>
      <div class="r1-light-rows" data-ref="rows"></div>
      <p class="sub" data-ref="note" style="margin-top:12px"></p></div>`;
    m.x = collect(body);
  },
  update(o, m) {
    const l = R1.light(), config = l.config || {}, target = l.target || {}, runtime = l.runtime || {};
    const active = !!l.active;
    const color = target.color || (config.manual && config.manual.color) || '#666666';
    const effect = target.mode != null ? R1_EFFECT_TXT[target.mode] : null;
    const stateText = active ? `灯光已接管${effect ? ' · ' + effect : ''}`
      : config.enabled ? '规则已保存，尚未接管' : '未启用灯光策略';
    setText(m.x.state, stateText);
    m.x.sw.style.background = color;
    m.x.sw.style.opacity = active && target.mode === 0 ? .25 : 1;
    setText(m.x.sub, active && ok(target.brightness) ? `亮度 ${F.int(target.brightness)}%` : '由原厂主题管理机身灯');
    const rows = [['灯效', effect || '--']];
    if (runtime.schedule_active) rows.push(['夜间计划', '生效中']);
    setHTML(m.x.rows, rows.map(([k, v]) => `<div class="row-i" style="grid-template-columns:auto 1fr"><span class="faint">${esc(k)}</span><span style="text-align:right">${esc(String(v))}</span></div>`).join(''));
    setHTML(m.x.note, l.last_error ? `<span class="st-crit">${esc(l.last_error)}</span>`
      : (l.capability && l.capability.available === false ? esc(l.capability.reason || '尚未验证唯一灯控设备') : '保存会立即应用；成功接管后才启用开机恢复。'));
  },
});

/* ---------------------------------------------------------------------
   模块 03 · 小屏与电源
   --------------------------------------------------------------------- */
defineModule({
  id: 'r1screen', page: 'overview', kicker: 'R1 SCREEN & POWER', title: '小屏与电源',
  dek: '亮度、息屏与整机电源操作',
  build(body, m) {
    body.innerHTML = `<div class="card pad">
      <div class="r1-light-main"><div style="flex:1"><div class="eyebrow">小屏亮度</div>
        <div class="dnum"><span class="dm" data-ref="br"></span><span class="unit">%</span></div>
        <div class="sub" data-ref="sub"></div></div>
        <div class="r1-stop"><button class="btn" type="button" data-act="sleep">立即息屏</button>
        <button class="btn" type="button" data-act="wake">唤醒小屏</button></div></div>
      <div class="r1-pwr"><button class="btn" type="button" data-pwr="reboot">${ic('restart')}重启 NAS</button>
        <button class="btn danger" type="button" data-pwr="poweroff">${ic('power')}关闭 NAS</button>
        <span class="faint">电源操作会二次确认，提交后 5 秒内可取消。</span></div>
      <p class="sub" data-ref="note" style="margin-top:12px"></p></div>`;
    m.x = collect(body);
    body.addEventListener('click', async e => {
      const act = e.target.closest('[data-act]'), pwr = e.target.closest('[data-pwr]');
      if (act) {
        const sleep = act.dataset.act === 'sleep';
        if (sleep && !(await R1.ask('立即让小屏息屏？', '触摸屏幕即可唤醒；网页与监控服务继续运行。'))) return;
        try {
          const j = await R1.post('/api/r1/screen', { action: act.dataset.act });
          toast(j.ok ? (sleep ? '小屏已请求息屏' : '已请求唤醒') : '操作未完成，请在小屏检查', j.ok ? {} : { icon: 'alert' });
          await R1.refresh();
        } catch (err) { if (!err.handled && err.status !== 401) toast('屏幕操作失败 · ' + String(err.message).slice(0, 70), { icon: 'alert' }); }
        return;
      }
      if (pwr) {
        const off = pwr.dataset.pwr === 'poweroff';
        if (!(await R1.ask(off ? '关闭这台 NAS？' : '重启这台 NAS？',
          off ? '所有服务与网页都会停止，且不会自动开机。确认后仍可在大约 5 秒内取消。'
              : '所有服务与网页会短暂中断。确认后仍可在大约 5 秒内取消。', true))) return;
        try {
          const j = await R1.post('/api/r1/power', { operation: pwr.dataset.pwr, confirmed: true });
          if (!j.ok) throw new Error(j.error || '未开始');
          toast(`${off ? '关机' : '重启'}请求已提交`, { action: '取消', onAction: async () => {
            try { const c = await R1.post('/api/r1/power/cancel', {}); toast(c.ok ? '已取消' : '取消失败，操作可能已开始', c.ok ? {} : { icon: 'alert' }); }
            catch (err) { toast('取消失败，操作可能已开始', { icon: 'alert' }); }
          }, ms: 5000 });
          await R1.refresh();
        } catch (err) { if (!err.handled && err.status !== 401) toast('电源操作未开始 · ' + String(err.message).slice(0, 70), { icon: 'alert' }); }
      }
    });
  },
  update(o, m) {
    const c = R1.control(), screen = c.screen || {}, caps = c.capabilities || {};
    const available = !!(caps.screen && caps.screen.available === true);
    dnum(m.x.br, available && ok(screen.brightness) ? screen.brightness : null);
    setText(m.x.sub, !available ? '未验证背光能力' : screen.asleep ? '小屏已息屏 · 触摸可唤醒' : '无操作后可按设置自动息屏');
    setHTML(m.x.note, available ? '亮度调整会同时写入小屏设置，重启后保持。' : '背光能力尚未验证，亮度与息屏操作不可用。');
    $$('[data-act]', m.el).forEach(b => { b.disabled = !available; });
  },
});

/* ---------------------------------------------------------------------
   SETTINGS — R1 keeps fnmonitor's look, thresholds, row controls and
   save-on-change behaviour, and replaces the fnOS-only groups.
   --------------------------------------------------------------------- */
const R1_LOOK = SET_SCHEMA.find(g => g.id === 'look');
const R1_THRESHOLDS = SET_SCHEMA.find(g => g.id === 'thresholds');

function r1Field(label, node, hint) {
  const row = mount(`<div class="set-row wide"><div><div class="lbl">${esc(label)}</div><div class="hint" data-ref="hint"></div></div><div class="ctl" data-ref="ctl"></div></div>`);
  row._r.ctl.append(node);
  if (hint) setHTML(row._r.hint, hint); else row._r.hint.hidden = true;
  return row;
}

function r1Select(options, value, onchange, aria) {
  const el = mount(`<select class="textfield" aria-label="${esc(aria || '')}">${options.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select>`);
  el.value = String(value);
  el.addEventListener('change', () => onchange(el.value));
  return el;
}

function r1Number(value, min, max, unit, onchange, aria) {
  const el = mount(`<span class="numfield"><button class="st" type="button" aria-label="减少">−</button><input type="number" inputmode="decimal" min="${min}" max="${max}" step="1" aria-label="${esc(aria || '')}"><span class="u">${esc(unit || '')}</span><button class="st" type="button" aria-label="增加">+</button></span>`);
  const input = el.querySelector('input');
  const [dec, inc] = el.querySelectorAll('.st');
  input.value = ok(value) ? value : '';
  const commit = () => {
    const n = parseFloat(input.value);
    if (!isFinite(n)) return;
    input.value = clamp(Math.round(n), min, max);
    onchange(+input.value);
  };
  input.addEventListener('change', commit);
  dec.onclick = () => { input.value = clamp((parseFloat(input.value) || min) - 1, min, max); commit(); };
  inc.onclick = () => { input.value = clamp((parseFloat(input.value) || min) + 1, min, max); commit(); };
  return el;
}

function r1Check(label, checked, onchange) {
  const el = mount(`<label class="check"><input type="checkbox">${esc(label)}</label>`);
  const input = el.querySelector('input');
  input.checked = !!checked;
  input.addEventListener('change', () => onchange(input.checked));
  return el;
}

/** 下拉多选：设备较多时避免把每个来源平铺在界面上。 */
function r1MultiSelect(options, selected, onchange, aria) {
  const el = mount(`<details class="r1-ms"><summary class="r1-ms-toggle" aria-label="${esc(aria || '选择设备')}"><span data-ref="label"></span>${ic('chev', 'i-sm')}</summary><div class="r1-ms-menu" role="listbox" aria-multiselectable="true" data-ref="menu"></div></details>`);
  const picked = new Set((selected || []).map(String));
  const label = el.querySelector('[data-ref="label"]'), menu = el.querySelector('[data-ref="menu"]'), toggle = el.querySelector('summary');
  const rows = (options || []).map(o => ({
    id: String(o.id), label: String(o.label || o.id),
    temp: ok(o.temperature_c) ? F.int(o.temperature_c) + '℃' : '',
    note: o.fresh === false ? '已过期' : o.status === 'standby' ? '休眠' : '',
  }));
  const refresh = () => {
    const chosen = rows.filter(r => picked.has(r.id));
    label.textContent = chosen.length ? (chosen.length <= 2 ? chosen.map(r => r.label).join('、') : '已选 ' + chosen.length + ' 个设备') : '选择设备';
    toggle.classList.toggle('has-value', chosen.length > 0);
  };
  if (!rows.length) {
    menu.innerHTML = '<p class="r1-ms-empty">尚未发现可用设备</p>';
    label.textContent = '暂无设备';
    toggle.setAttribute('aria-disabled', 'true');
  } else {
    menu.innerHTML = rows.map(r => `<label class="r1-ms-row" role="option" aria-selected="${picked.has(r.id)}"><input type="checkbox" value="${esc(r.id)}" ${picked.has(r.id) ? 'checked' : ''}><span class="r1-ms-name">${esc(r.label)}</span><span class="r1-ms-temp">${esc(r.temp)}${r.note ? ' · ' + esc(r.note) : ''}</span></label>`).join('');
    menu.addEventListener('change', event => {
      const input = event.target.closest('input[type=checkbox]');
      if (!input) return;
      if (input.checked) picked.add(input.value); else picked.delete(input.value);
      const row = input.closest('.r1-ms-row');
      if (row) row.setAttribute('aria-selected', String(input.checked));
      refresh();
      onchange([...picked]);
    });
  }
  el.addEventListener('keydown', event => { if (event.key === 'Escape') { el.open = false; toggle.focus(); } });
  el.addEventListener('toggle', () => toggle.setAttribute('aria-expanded', String(el.open)));
  refresh();
  return el;
}

function r1Time(value, onchange) {
  const el = mount(`<input class="textfield" type="time" value="${esc(value || '22:00')}">`);
  el.addEventListener('change', () => onchange(el.value));
  return el;
}

function r1Color(value, onchange) {
  const el = mount(`<input type="color" value="${esc(value || '#00C832')}">`);
  el.addEventListener('change', () => onchange(el.value.toUpperCase()));
  return el;
}

const FAN_SYSTEM_MODES = [['auto', '自动'], ['high', '高档'], ['medium', '中档'], ['low', '低档'], ['manual', '手动']];
const FAN_CPU_MODES = [['auto', '自动'], ['high', '高档'], ['medium', '中档'], ['low', '低档'], ['manual', '手动']];
const FAN_CPU_FIXED = { high: 100, medium: 75, low: 50 };

/** CPU 固定档沿用现有比例；系统旧固定值保留为兼容项，不能冒充原厂档。 */
function fanUiMode(item, name) {
  if (item && item.mode === 'manual') {
    if (name === 'cpu') {
      const fixed = Object.keys(FAN_CPU_FIXED).find(key => FAN_CPU_FIXED[key] === item.manual_percent);
      if (fixed) return fixed;
    }
    return 'legacy:' + item.manual_percent;
  }
  if (item && item.mode === 'temperature') return 'manual';
  return name === 'cpu' ? 'auto' : (R1_NATIVE_FAN_TXT[item && item.vendor_mode] ? item.vendor_mode : '');
}

/** 两个温度阈值划分三个输出区间，边界点与说明同步反映实际控制规则。 */
function fanCurveEditor(item, touch) {
  const box = mount(`<div class="r1-curve">
    <svg class="r1-curve-chart" viewBox="0 0 240 92" aria-hidden="true">
      <path class="r1-curve-track" d="M8 82H232M8 8V82"></path>
      <path class="r1-curve-line" data-ref="line"></path>
      <g class="r1-curve-dots" data-ref="dots"></g>
    </svg>
    <div class="r1-curve-rows" data-ref="rows"></div>
    <p class="sub r1-curve-ranges" data-ref="ranges"></p>
  </div>`);
  const line = box.querySelector('[data-ref="line"]'), dots = box.querySelector('[data-ref="dots"]'), rows = box.querySelector('[data-ref="rows"]'), ranges = box.querySelector('[data-ref="ranges"]');
  const x = t => 8 + (clamp(t, 15, 85) - 15) / 70 * 224;
  const y = p => 82 - (clamp(p, 10, 100) - 10) / 90 * 74;
  const paint = () => {
    const low = item.low_temp, high = item.high_temp;
    const q = clamp(item.quiet_percent, 10, 100), n = Math.max(q, clamp(item.normal_percent, 10, 100));
    line.setAttribute('d', `M8 ${y(q).toFixed(1)}H${x(low).toFixed(1)}V${y(n).toFixed(1)}H${x(high).toFixed(1)}V${y(100).toFixed(1)}H232`);
    dots.innerHTML = [[low, q, false], [low, n, true], [high, 100, false], [high, n, true]].map(([t, p, closed]) => `<circle class="${closed ? 'r1-curve-closed' : ''}" cx="${x(t).toFixed(1)}" cy="${y(p).toFixed(1)}" r="3.4"></circle>`).join('');
    ranges.textContent = `T < ${low}℃：静音 ${q}%；${low}℃ ≤ T ≤ ${high}℃：普通 ${n}%；T > ${high}℃：全速 100%。`;
  };
  const tempInputs = {};
  const tempBounds = key => key === 'low_temp' ? [15, item.high_temp - 1] : [item.low_temp + 1, 85];
  const syncTempBounds = () => {
    for (const [key, input] of Object.entries(tempInputs)) [input.min, input.max] = tempBounds(key);
  };
  const addRow = (label, key) => {
    const row = mount(`<div class="r1-curve-row"><span class="r1-curve-name">${esc(label)}</span><input class="r1-curve-input" type="number" inputmode="numeric" step="1" value="${item[key]}" aria-label="${esc(label + '℃')}"><span class="r1-curve-unit">℃</span></div>`);
    const input = row.querySelector('input');
    tempInputs[key] = input;
    input.addEventListener('change', event => {
      const entered = event.target.valueAsNumber;
      const [low, high] = tempBounds(key);
      const value = clamp(Math.round(Number.isFinite(entered) ? entered : item[key]), low, high);
      event.target.value = value;
      item[key] = value;
      syncTempBounds();
      touch(); paint();
    });
    rows.append(row);
  };
  addRow('低温', 'low_temp');
  addRow('高温', 'high_temp');
  syncTempBounds();
  const duty = mount(`<div class="r1-curve-rows" data-ref="duty"></div>`);
  const dutyInputs = {};
  const addDuty = (label, key, fixed) => {
    const row = mount(`<div class="r1-curve-row"><span class="r1-curve-name">${esc(label)}</span><input class="r1-curve-input" type="number" inputmode="numeric" min="${key === 'normal_percent' ? item.quiet_percent : 10}" max="100" step="1" value="${fixed == null ? item[key] : 100}" aria-label="${esc(label)}" ${fixed == null ? '' : 'disabled'}><span class="r1-curve-unit">%</span></div>`);
    if (fixed == null) {
      dutyInputs[key] = row.querySelector('input');
      dutyInputs[key].addEventListener('change', event => {
        const entered = event.target.valueAsNumber;
        const low = key === 'normal_percent' ? item.quiet_percent : 10;
        const value = clamp(Math.round(Number.isFinite(entered) ? entered : item[key]), low, 100);
        event.target.value = value;
        item[key] = value;
        if (key === 'quiet_percent') {
          item.normal_percent = Math.max(value, item.normal_percent);
          dutyInputs.normal_percent.value = item.normal_percent;
          dutyInputs.normal_percent.min = value;
        }
        touch(); paint();
      });
    }
    duty.append(row);
  };
  addDuty('静音输出', 'quiet_percent');
  addDuty('普通输出', 'normal_percent');
  addDuty('全速输出', null, 100);
  rows.after(duty);
  paint();
  return box;
}

function r1FanSettings(card) {
  const shell = () => {
    const config = JSON.parse(JSON.stringify(R1.fan().config || {}));
    const system = (config.channels || {}).system;
    // 旧配置没有原厂档位时，用实际快照补足；未知时省略字段以保留原厂现状。
    if (system && system.mode === 'vendor' && !system.vendor_mode && R1_NATIVE_FAN_TXT[fanOfficial().mode]) system.vendor_mode = fanOfficial().mode;
    return config;
  };
  let draft = shell(), dirty = false;
  let renderedCapability = '';
  card.innerHTML = `<div class="r1-edit" data-ref="wrap"></div>
    <div class="set-actions"><button class="btn primary" type="button" data-ref="save">保存</button></div>
    <p class="sub" data-ref="status"></p>`;
  const x = collect(card);
  const touch = () => { dirty = true; };
  const render = () => {
    const f = R1.fan(), config = draft.channels || {};
    const loaded = !!(config.cpu && config.system);
    x.wrap.setAttribute('aria-busy', String(!loaded));
    x.save.disabled = !!f.busy || !loaded;
    const sources = f.sources || [], cap = f.capability || {};
    renderedCapability = JSON.stringify([cap, fanOfficial()]);
    const wrap = mount(`<div></div>`);
    ['cpu', 'system'].forEach(name => {
      const item = config[name] || {};
      const ch = mount(`<div class="card set-card r1-editor" data-channel="${name}">
        <div class="r1-chan-head"><b>${R1.chanName(name)}</b><span class="pill" data-ref="state"></span></div>
        <div class="r1-form" data-ref="form"></div></div>`);
      const form = ch._r.form;
      const modeOptions = (name === 'cpu' ? FAN_CPU_MODES : FAN_SYSTEM_MODES).slice();
      const mode = fanUiMode(item, name);
      if (!mode) modeOptions.unshift(['', '原厂档位未读取']);
      if (mode.startsWith('legacy:')) modeOptions.push([mode, '独立固定 ' + item.manual_percent + '%（旧配置）']);
      const selector = r1Select(modeOptions, mode, value => {
        if (value === 'manual') {
          item.mode = 'temperature';
          delete item.vendor_mode;
          if (!Array.isArray(item.sources)) item.sources = [];
        } else if (name === 'cpu' && FAN_CPU_FIXED[value]) {
          item.mode = 'manual';
          item.manual_percent = FAN_CPU_FIXED[value];
        } else if (value.startsWith('legacy:')) {
          return;
        } else {
          item.mode = 'vendor';
          if (name === 'system') item.vendor_mode = value;
        }
        touch(); render();
      }, name + '转速模式');
      selector.disabled = !config[name];
      for (const option of selector.options) {
        option.disabled = !option.value || option.value.startsWith('legacy:') || (name === 'system' && option.value !== 'manual' && fanOfficial().control_available !== true);
      }
      form.append(r1Field('转速模式', selector, name === 'cpu'
        ? '自动：原厂温控。高／中／低：固定输出 100／75／50%。手动：按温度曲线调速。'
        : '自动／高／中／低：原厂系统档位。手动：按温度曲线独立调速。'));
      if (item.mode === 'temperature') {
        form.append(r1Field('温度设备 · 多选', r1MultiSelect(sources, item.sources, value => { item.sources = value; touch(); }), '点开下拉多选；只使用新鲜读数，取所选设备的最高温度。'));
        form.append(r1Field('温度曲线', fanCurveEditor(item, touch), '低温必须小于高温。输出范围 10–100%，普通不低于静音，全速固定 100%。升温与降温均按当前温度在下一轮采样切换，温度保护优先。'));
      }
      if (item.mode === 'manual') {
        const legacy = mode.startsWith('legacy:');
        form.append(r1Field(legacy ? '兼容配置' : '固定输出', mount(`<span class="r1-fixed">${item.manual_percent}%</span>`), legacy
          ? '保留原有固定输出；选择新档位后才会替换此配置。'
          : '请求输出不是实测转速，温度保护优先。'));
      }
      if (item.mode === 'vendor') {
        form.append(r1Field('当前运行', mount(`<span class="sub r1-native-current">${esc(fanRunningMode(name))}</span>`), name === 'system'
          ? (fanOfficial().control_available === true ? '保存后切换所选原厂档位，并释放该通道的手动接管。' : esc('原厂档位暂不可切换：' + (fanOfficial().reason || '尚未确认原厂控制能力')))
          : '保存后释放 CPU 手动接管，恢复原厂自动温控。'));
      }
      const live = (R1.fan().channels || {})[name] || {};
      setText(ch._r.state, fanLiveState(name));
      ch._r.state.className = 'pill' + (live.active ? ' ok' : '');
      wrap.append(ch);
    });
    wrap.append(mount(`<p class="sub">${cap.available === true ? '手动接管预检通过。' : esc('手动接管暂不可用：' + (cap.reason || '设备尚未满足独立调速条件')) + '。'}${fanOfficial().control_available === true ? '系统原厂档位可切换。' : '系统原厂档位暂不可切换。'}保存后以当前运行状态确认为准。</p>`));
    x.wrap.replaceChildren(wrap);
  };
  x.save.onclick = async () => {
    if (await R1.save('/api/r1/fan', { config: draft, apply: true, confirmed: true }, '风扇设置')) { dirty = false; draft = shell(); render(); }
  };
  card._sync = () => {
    const f = R1.fan();
    setHTML(x.status, f.last_error ? `<span class="st-crit">${esc(f.last_error)}</span>`
      : `保存：${f.pending_changes ? '有未应用的更改' : '与当前运行一致'} · 恢复确认：${f.recovery_confirmed === false ? '待确认' : '正常'}`);
    if (!dirty) { const fresh = shell(); if (JSON.stringify(fresh) !== JSON.stringify(draft) || renderedCapability !== JSON.stringify([f.capability || {}, fanOfficial()])) { draft = fresh; render(); } }
    x.save.disabled = !!f.busy || !draft.channels || !draft.channels.cpu || !draft.channels.system;
    for (const name of ['cpu', 'system']) {
      const state = x.wrap.querySelector(`[data-channel="${name}"] [data-ref="state"]`);
      if (state) { setText(state, fanLiveState(name)); state.className = 'pill' + (((f.channels || {})[name] || {}).active ? ' ok' : ''); }
      const current = x.wrap.querySelector(`[data-channel="${name}"] .r1-native-current`);
      if (current) setText(current, fanRunningMode(name));
      if (name === 'system') {
        const selector = x.wrap.querySelector('[data-channel="system"] select');
        if (selector) for (const option of selector.options) option.disabled = !option.value || option.value.startsWith('legacy:') || (option.value !== 'manual' && fanOfficial().control_available !== true);
      }
    }
  };
  render();
}

function r1LightSettings(card) {
  const shell = () => JSON.parse(JSON.stringify(R1.light().config || {}));
  let draft = shell(), dirty = false;
  card.innerHTML = `<div class="r1-edit" data-ref="wrap"></div>
    <div class="set-actions"><button class="btn primary" type="button" data-ref="save">保存</button></div>
    <p class="sub" data-ref="status"></p>
    <h4 class="r1-sub">即时灯效（不写入规则）</h4>
    <div class="r1-inline" data-ref="rgb"></div>`;
  const x = collect(card);
  const touch = () => { dirty = true; };
  const render = () => {
    const l = R1.light(), config = draft;
    // The UI now exposes only the four native effects.  Existing temperature
    // drafts are converted to manual when the user opens and saves the card.
    config.mode = 'manual';
    const wrap = mount(`<div class="card set-card r1-editor"><div class="r1-form" data-ref="form"></div></div>`);
    const form = wrap._r.form;
    form.append(r1Field('启用灯光策略', r1Check('保存后允许接管机身灯光', !!config.enabled, v => { config.enabled = v; touch(); }), '关闭并应用会释放接管，恢复原厂主题。'));
    const manual = config.manual || (config.manual = { mode: 1, color: '#00C832', brightness: 70 });
    form.append(r1Field('灯效', r1Select([[0, '关闭'], [1, '常亮'], [2, '彩虹'], [3, '呼吸']], manual.mode, v => { manual.mode = +v; touch(); })));
    form.append(r1Field('颜色', r1Color(manual.color, v => { manual.color = v; touch(); })));
    form.append(r1Field('亮度', r1Number(manual.brightness, 0, 100, '%', v => { manual.brightness = v; touch(); })));
    const schedule = config.schedule || (config.schedule = { enabled: false, start: '22:00', end: '07:00', brightness: 15 });
    form.append(r1Field('夜间亮度上限', r1Check('夜间限制灯光亮度', !!schedule.enabled, v => { schedule.enabled = v; touch(); render(); }), '支持跨午夜时段。'));
    if (schedule.enabled) {
      form.append(r1Field('夜间开始', r1Time(schedule.start, v => { schedule.start = v; touch(); })));
      form.append(r1Field('夜间结束', r1Time(schedule.end, v => { schedule.end = v; touch(); })));
      form.append(r1Field('夜间亮度', r1Number(schedule.brightness, 0, 100, '%', v => { schedule.brightness = v; touch(); })));
    }
    wrap.append(mount(`<p class="sub">${l.capability && l.capability.available === false ? esc('暂不可应用：' + (l.capability.reason || '尚未验证唯一灯控设备')) : '保存会立即应用；成功接管后才启用开机恢复。'}</p>`));
    x.wrap.replaceChildren(wrap);
  };
  x.save.onclick = async () => {
    if (!(await R1.ask('保存并应用灯光规则？', '保存后会立即应用；成功接管后按配置持续运行。'))) return;
    if (await R1.save('/api/r1/light', { config: draft, apply: true }, '灯光规则')) { dirty = false; draft = shell(); render(); }
  };
  const rgb = mount(`<div class="r1-form" data-ref="form"></div>`);
  const rgbState = { mode: 1, color: '#00C832', brightness: 70 };
  rgb._r.form.append(r1Field('即时灯效', r1Select([[0, '关闭'], [1, '常亮'], [2, '彩虹'], [3, '呼吸']], rgbState.mode, v => { rgbState.mode = +v; })));
  rgb._r.form.append(r1Field('颜色', r1Color(rgbState.color, v => { rgbState.color = v; })));
  rgb._r.form.append(r1Field('亮度', r1Number(rgbState.brightness, 0, 100, '%', v => { rgbState.brightness = v; })));
  const go = mount(`<button class="btn" type="button">立即应用灯光</button>`);
  go.onclick = async () => {
    try {
      const j = await R1.post('/api/r1/rgb', rgbState);
      toast(j.ok ? '灯光已应用' : '灯光未应用，请在小屏检查', j.ok ? {} : { icon: 'alert' });
    } catch (e) { if (!e.handled && e.status !== 401) toast('灯光操作失败 · ' + String(e.message).slice(0, 70), { icon: 'alert' }); }
  };
  rgb._r.form.append(go);
  x.rgb.replaceWith(rgb);
  card._sync = () => {
    setHTML(x.status, R1.light().last_error ? `<span class="st-crit">${esc(R1.light().last_error)}</span>` : '');
    if (!dirty) { const fresh = shell(); if (JSON.stringify(fresh) !== JSON.stringify(draft)) { draft = fresh; render(); } }
  };
  render();
}

function r1ScreenSettings(card) {
  let sig = '';
  let saverPending = false;
  const labelKey = 'r1SaverSourceLabels';
  const sourceLabels = new Map();
  try {
    const cached = JSON.parse(localStorage.getItem(labelKey) || '{}');
    Object.entries(cached).slice(0, 12).forEach(([id, label]) => {
      if (/^temperature:[A-Za-z0-9:_.-]{1,256}$/.test(id) && typeof label === 'string') sourceLabels.set(id, label.slice(0, 256));
    });
  } catch (_) { /* Storage is optional; unknown sources still retain their IDs. */ }
  card.innerHTML = `<div data-ref="rows"></div><div class="r1-inline" data-ref="acts">
      <button class="btn" type="button" data-ref="sleep">立即息屏</button>
      <button class="btn" type="button" data-ref="wake">唤醒小屏</button></div>
    <p class="sub" data-ref="status"></p>`;
  const x = collect(card);
  const apply = async settings => {
    try {
      const j = await R1.post('/api/r1/settings', { settings });
      const saved = j.ok === true || j.saved === true || j.settings_saved === true;
      toast(j.ok ? '小屏设置已保存' : saved ? '小屏设置已保存，但设备应用未完成' : '小屏设置未保存，请重试', { key: j.ok ? 'screen-save' : 'screen-save-error', icon: j.ok ? 'check' : 'alert' });
      await R1.refresh();
      return saved;
    } catch (e) {
      const saved = e.body && (e.body.saved === true || e.body.settings_saved === true);
      if (!e.handled && e.status !== 401) toast(saved ? '小屏设置已保存，但设备应用未完成' : '小屏设置未保存，请重试', { key: 'screen-save-error', icon: 'alert' });
      await R1.refresh();
      return !!saved;
    }
  };
  const saverPicker = settings => {
    const loaded = typeof settings.saver_timeout === 'number';
    let picked = Array.isArray(settings.saver_items) && settings.saver_items.length ? settings.saver_items.slice()
      : settings.saver_content === 'temperature' ? ['cpu_temperature', 'board_temperature'] : ['cpu_usage', 'memory_usage'];
    const groups = new Map([
      ['使用率', [{ id: 'cpu_usage', label: 'CPU 使用率' }, { id: 'memory_usage', label: '内存使用率' }]],
      ['汇总温度', [{ id: 'cpu_temperature', label: 'CPU 温度（最高值）' }, { id: 'board_temperature', label: '主板温度（最高值）' }]],
    ]);
    const options = new Map([...groups.values()].flat().map(item => [item.id, item]));
    for (const source of R1.sources()) {
      const sourceId = String(source.id || '');
      if (!/^[A-Za-z0-9:_.-]{1,256}$/.test(sourceId)) continue;
      const id = 'temperature:' + sourceId;
      if (options.has(id)) continue;
      const label = String(source.label || sourceId).slice(0, 256);
      sourceLabels.set(id, label);
      const group = source.kind === 'cpu' ? 'CPU 温度来源' : source.kind === 'board' ? '主板温度来源' : source.kind === 'disk' ? '硬盘温度来源' : '其他温度来源';
      if (!groups.has(group)) groups.set(group, []);
      const available = source.fresh !== false && source.status === 'ok' && ok(source.temperature_c);
      const item = { id, label, note: available ? '' : source.status === 'standby' ? '不可用 · 休眠' : '不可用' };
      groups.get(group).push(item); options.set(id, item);
    }
    for (const id of picked) {
      if (options.has(id)) continue;
      const group = '未连接的温度来源';
      if (!groups.has(group)) groups.set(group, []);
      const item = { id, label: sourceLabels.get(id) || id.replace(/^temperature:/, ''), note: '不可用 · 未连接' };
      groups.get(group).push(item); options.set(id, item);
    }
    const remember = () => {
      const labels = Object.fromEntries(picked.filter(id => sourceLabels.has(id)).map(id => [id, sourceLabels.get(id)]));
      try { localStorage.setItem(labelKey, JSON.stringify(labels)); } catch (_) { /* Keep selection usable when browser storage is disabled. */ }
    };
    if (loaded) remember();
    const picker = mount(`<div class="r1-saver-picker" role="group" aria-label="屏幕保护项目" aria-busy="false">
      <div class="r1-saver-count" data-ref="count"></div><ol class="r1-saver-selected" aria-label="显示顺序" data-ref="order"></ol>
      <div class="r1-saver-groups" data-ref="groups"></div><p class="sub st-crit" data-ref="error" role="status" hidden></p></div>`);
    const x = collect(picker);
    for (const [title, items] of groups) {
      x.groups.append(mount(`<fieldset class="r1-saver-group"><legend>${esc(title)}</legend>${items.map(item => `<label class="r1-saver-option"><input type="checkbox" value="${esc(item.id)}" ${picked.includes(item.id) ? 'checked' : ''}><span class="r1-saver-name">${esc(item.label)}</span>${item.note ? `<span class="r1-saver-note">${esc(item.note)}</span>` : ''}</label>`).join('')}</fieldset>`));
    }
    const refresh = () => {
      picker.setAttribute('aria-busy', String(saverPending || !loaded));
      setText(x.count, `已选 ${picked.length} 项，最多 12 项`);
      x.order.replaceChildren(...picked.map(id => {
        const li = document.createElement('li');
        li.textContent = (options.get(id) || {}).label || id;
        return li;
      }));
      picker.querySelectorAll('input').forEach(input => {
        input.checked = picked.includes(input.value);
        input.disabled = !loaded || saverPending || (input.checked ? picked.length === 1 : picked.length >= 12);
      });
    };
    picker.addEventListener('change', async event => {
      const input = event.target.closest('input[type="checkbox"]');
      if (!input) return;
      const next = input.checked ? [...picked, input.value] : picked.filter(id => id !== input.value);
      if (!loaded || saverPending || !options.has(input.value) || next.length < 1 || next.length > 12 || new Set(next).size !== next.length) {
        setText(x.error, '请选择 1–12 个不重复的项目。'); x.error.hidden = false; refresh(); return;
      }
      const before = picked;
      picked = next; saverPending = true; x.error.hidden = true; remember(); refresh();
      if (!(await apply({ saver_items: picked }))) {
        const savedItems = (R1.control().settings || {}).saver_items;
        picked = Array.isArray(savedItems) && savedItems.length ? savedItems.slice() : before;
        remember();
      }
      saverPending = false; card._sync(); refresh();
    });
    refresh();
    return picker;
  };
  x.sleep.onclick = async () => { try { await R1.post('/api/r1/screen', { action: 'sleep' }); toast('已请求息屏'); } catch (e) { if (!e.handled && e.status !== 401) toast('操作失败', { icon: 'alert' }); } };
  x.wake.onclick = async () => { try { await R1.post('/api/r1/screen', { action: 'wake' }); toast('已请求唤醒'); } catch (e) { if (!e.handled && e.status !== 401) toast('操作失败', { icon: 'alert' }); } };
  card._sync = () => {
    if (saverPending) return;
    const control = R1.control(), s = control.settings || {}, caps = control.capabilities || {};
    const sources = R1.sources().map(source => [source.id, source.label, source.kind, source.fresh, source.status]);
    const key = JSON.stringify([s, caps.screen, caps.brightness, sources]);
    if (key === sig) return;
    sig = key;
    const rows = mount(`<div></div>`);
    if (caps.brightness && caps.brightness.available === true) {
      rows.append(r1Field('屏幕亮度', r1Number(s.brightness, 5, 100, '%', v => apply({ brightness: v })), '5–100%，同时保存在小屏设置中'));
    }
    rows.append(r1Field('屏幕保护', r1Number(s.saver_timeout || 0, 0, 86400, '秒', v => apply({ saver_timeout: v })), '无操作达到该时间后进入屏幕保护；0 为关闭'));
    if (s.saver_timeout > 0) {
      rows.append(r1Field('屏幕保护亮度', r1Number(s.saver_brightness || 30, 5, 100, '%', v => apply({ saver_brightness: v })), '屏幕保护时使用的背光亮度'));
    }
    rows.append(r1Field('屏幕保护项目', saverPicker(s), '使用率与温度可混选，至少 1 项、最多 12 项；按勾选顺序显示。断开的来源保留为不可用。'));
    rows.append(r1Field('关闭屏幕', r1Number(s.sleep_timeout || 0, 0, 86400, '秒', v => apply({ sleep_timeout: v })), '无操作达到该时间后关闭背光；0 为关闭。建议大于屏幕保护时间'));
    rows.append(r1Field('夜间自动调暗', r1Check('启用夜间亮度', !!s.night_enabled, v => apply({ night_enabled: v })), '按 NAS 本地时间，支持跨午夜'));
    if (s.night_enabled) {
      rows.append(r1Field('夜间开始', r1Time(s.night_start, v => apply({ night_start: v }))));
      rows.append(r1Field('夜间结束', r1Time(s.night_end, v => apply({ night_end: v }))));
      rows.append(r1Field('夜间亮度', r1Number(s.night_brightness, 5, 100, '%', v => apply({ night_brightness: v }))));
    }
    x.rows.replaceChildren(rows);
    setHTML(x.status, (caps.brightness && caps.brightness.available === true) ? '' : '背光能力尚未验证：亮度与夜间调暗在小屏上不可用。');
    x.sleep.disabled = !(caps.screen && caps.screen.available === true);
    x.wake.disabled = x.sleep.disabled;
  };
  card._sync();
}

function r1MonitorSettings(card) {
  const spec = [
    ['cpu_warn', 'CPU 警告', 1, 100, '%'], ['cpu_critical', 'CPU 严重', 1, 100, '%'],
    ['memory_warn', '内存警告', 1, 100, '%'], ['memory_critical', '内存严重', 1, 100, '%'],
    ['cpu_temp_warn', 'CPU 温度警告', 10, 110, '℃'], ['cpu_temp_critical', 'CPU 温度严重', 10, 110, '℃'],
    ['disk_temp_warn', '硬盘温度警告', 10, 90, '℃'], ['disk_temp_critical', '硬盘温度严重', 10, 90, '℃'],
    ['capacity_warn', '容量警告', 1, 100, '%'], ['capacity_critical', '容量严重', 1, 100, '%'],
  ];
  let sig = '';
  card.innerHTML = `<div data-ref="rows"></div><p class="sub">这里的阈值决定本地告警与历史标记；面板配色使用「提醒阈值」分组。RAPL 不可用时不估算整机功耗。</p>`;
  const x = collect(card);
  const save = async changes => {
    try {
      const j = await R1.post('/api/r1/monitor', { settings: changes });
      toast(j.ok ? '统计设置已保存' : '统计设置未能保存', j.ok ? {} : { icon: 'alert' });
      await R1.refresh();
    } catch (e) { if (!e.handled && e.status !== 401) toast('保存失败 · ' + String(e.message).slice(0, 70), { icon: 'alert' }); }
  };
  card._sync = () => {
    const s = (R1.st && R1.st.monitoring && R1.st.monitoring.settings) || {};
    const key = JSON.stringify(s);
    if (key === sig) return;
    sig = key;
    const thresholds = s.thresholds || {};
    const rows = mount(`<div></div>`);
    rows.append(r1Field('记录历史采样', r1Check('启用 SQLite 历史', s.history_enabled !== false, v => save({ history_enabled: v })), '历史写入数据卷，关闭后不再记录'));
    rows.append(r1Field('历史保留', r1Number(s.retention_days || 30, 1, 30, '天', v => save({ retention_days: v })), '1–30 天，超期自动清理'));
    rows.append(r1Field('流量汇总', r1Check('排除虚拟网卡', s.traffic_exclude_virtual !== false, v => save({ traffic_exclude_virtual: v })), 'docker / veth / br- 等不计入今日与本月流量'));
    spec.forEach(([key, label, min, max, unit]) => rows.append(r1Field(label,
      r1Number(thresholds[key], min, max, unit, v => save({ thresholds: { [key]: v } })), '')));
    x.rows.replaceChildren(rows);
  };
  card._sync();
}

function r1AuthSettings(card) {
  let sig = '';
  card.innerHTML = `<div data-ref="rows"></div><p class="sub" data-ref="status"></p>`;
  const x = collect(card);
  const post = async body => {
    try {
      const j = await R1.post('/api/r1/auth', body);
      if (!j || j.ok !== true) throw new Error((j && j.error) || '操作未完成');
      toast('登录设置已保存');
      await R1.refresh();
      card._sync();
    } catch (e) { if (!e.handled && e.status !== 401) toast('登录设置失败 · ' + String(e.message).slice(0, 80), { icon: 'alert', ms: 5000 }); }
  };
  card._sync = () => {
    const a = R1.auth();
    const key = JSON.stringify([a.enabled, a.password_set, a.session_days, a.password_required]);
    if (key === sig) return;
    sig = key;
    const rows = mount(`<div></div>`);
    const passwordMode = ((R1.st && R1.st.web && R1.st.web.auth_mode) || a.auth_mode || 'password') === 'password';
    rows.append(r1Field('登录口令', r1Check(a.enabled ? '访问面板需要登录口令' : '当前无需口令（监控公开）', !!a.enabled,
      v => post({ action: v ? 'enable' : 'disable' })), passwordMode
      ? '首次使用默认不设口令，可直接管理设备；设置并开启口令后，访问网页需要登录。'
      : '开启后访问面板需先输入口令；当前兼容配对模式下，关闭口令后的写操作需要小屏配对码。'));
    rows.append(r1Field('会话有效期', r1Number(a.session_days || 7, 1, 30, '天', v => post({ action: 'session_days', days: v })), '登录后保持天数；修改口令会注销其他会话'));
    const pw = mount(`<div class="r1-inline">
      <input class="textfield" type="password" placeholder="当前口令" aria-label="当前口令" data-ref="old" autocomplete="current-password">
      <input class="textfield" type="password" placeholder="新口令（至少 6 位）" aria-label="新口令" data-ref="pw" autocomplete="new-password">
      <button class="btn" type="button" data-ref="go">${a.password_set ? '修改口令' : '设置口令'}</button>
      ${a.password_set ? '<button class="btn ghost" type="button" data-ref="clear">取消口令</button>' : ''}
    </div>`);
    pw._r.go.onclick = async () => {
      const oldValue = pw._r.old.value, newValue = pw._r.pw.value;
      if (newValue.length < 6) { toast('新口令至少 6 位', { icon: 'alert' }); return; }
      if (!a.password_set) {
        // 默认无口令时允许首次设置；兼容配对模式仍由服务端核验授权。
        try {
          const j = await R1.post('/api/auth/setup', { password: newValue });
          if (!j || j.ok !== true) throw new Error((j && j.error) || '设置失败');
          toast('登录口令已设置');
          await R1.refresh();
          await loadConfig().catch(() => null);
          card._sync();
        } catch (e) { if (!e.handled) toast('设置口令失败 · ' + String(e.message).slice(0, 80), { icon: 'alert', ms: 5000 }); }
        return;
      }
      post({ action: 'change', old_password: oldValue, new_password: newValue });
    };
    if (pw._r.clear) pw._r.clear.onclick = async () => {
      const mode = (R1.st && R1.st.web && R1.st.web.auth_mode) || (a.auth_mode) || 'password';
      const message = mode === 'password'
        ? '取消后面板恢复公开监控，设备写操作将不再需要登录。'
        : '取消后面板恢复公开监控，设备写操作仍需要小屏配对码。';
      if (!(await R1.ask('取消登录口令？', message, true))) return;
      post({ action: 'change', old_password: pw._r.old.value, new_password: '' });
    };
    rows.append(r1Field('修改口令', pw, '只保存 PBKDF2 哈希；忘记口令可在 NAS 上删除数据卷中的 panel-auth.json 后重启服务。'));
    if (a.password_set) {
      const out = mount(`<button class="btn" type="button">退出登录</button>`);
      out.onclick = async () => { try { await API.post('/api/auth/logout', {}); } catch (e) { /* ignore */ } location.reload(); };
      rows.append(r1Field('当前会话', out, ''));
    }
    x.rows.replaceChildren(rows);
    setHTML(x.status, a.password_required ? '当前已启用登录口令；面板读取与写入都需要登录。' : passwordMode ? '当前未启用登录口令；只读监控公开，设备写操作默认开放。' : '当前未启用登录口令；只读监控公开，设备写操作需要小屏五位配对码。');
    x.pairing = a.pairing;
  };
  card._sync();
}

/** Accept literal LAN address/network syntax; the host validates ranges strictly. */
function r1ApiRules(value) {
  const rules = String(value || '').split(/[\s,，;；]+/).map(item => item.trim()).filter(Boolean);
  if (rules.length > 32 || new Set(rules).size !== rules.length || rules.some(rule =>
    !(rule.includes(':') ? /^[0-9a-fA-F:.]+(?:\/\d{1,3})?$/.test(rule)
      : /^\d+(?:\.\d+){3}(?:\/(?:\d{1,2}|\d+(?:\.\d+){3}))?$/.test(rule)))) {
    throw new Error('来源须为不重复的 IPv4／IPv6 地址、CIDR 或 IPv4 点分掩码，最多 32 项；不接受域名或通配符。');
  }
  return rules;
}

/** Services are read only while this explicit manager is open. */
function r1ServiceSettings(card) {
  card.innerHTML = `<div class="r1-api-intro"><div><div class="lbl">常驻服务与开发模式</div><p class="hint">容器持续提供网页监控与管理。小屏默认开启，可关闭并恢复官方界面；MCP 默认关闭。首次使用无需网页口令，可在登录口令中设置。</p></div><button class="btn" type="button" data-ref="open">管理服务</button></div>`;
  card._r = collect(card); card._r.open.onclick = r1OpenServiceManager;
}

/** Owner-only optional rule; mounting this card reads cached state, never a DB. */
function r1MediaSortSettings(card) {
  card.innerHTML = `<div class="r1-media-sort">
    <p class="sub">可选插件 · v2 仅在已识别电视剧新增路径时调整追更排序，资料刷新不置前。电视端“最近添加”的实际效果仍需验证。</p>
    <p class="sub">仅网页所有者可操作。点击检测才自动查找影视数据库，可能唤醒磁盘；不需要填写路径，不会开机重装规则。</p>
    <div class="r1-api-actions"><button class="btn primary" type="button" data-ref="probe">检测数据库</button><button class="btn" type="button" data-ref="refresh">读取缓存</button></div>
    <p data-ref="summary" role="status">未检测。</p><p class="sub" data-ref="checked">上次检测：未检测。</p>
    <p class="sub" data-ref="activity" role="status"></p><p class="st-crit" data-ref="error" role="alert" hidden></p>
    <label class="r1-api-input">自动找到的数据库<select class="textfield" data-ref="choice" aria-label="选择影视数据库"></select></label>
    <p class="sub" data-ref="selection"></p>
    <dl class="r1-api-meta"><dt>数据库位置</dt><dd data-ref="path">未选择。</dd><dt>上次检测状态</dt><dd data-ref="state">未知。</dd><dt>媒体记录数</dt><dd data-ref="count">--</dd><dt>规则版本</dt><dd data-ref="rule">--</dd><dt>Toolbox 备份位置</dt><dd data-ref="backup">尚无备份记录。</dd></dl>
    <p class="sub" data-ref="reason"></p>
    <div class="r1-api-actions"><button class="btn primary" type="button" data-ref="apply" disabled>应用追更排序</button><button class="btn" type="button" data-ref="revert" disabled>撤销增强</button></div>
    <p class="sub">应用或升级前先备份，再改变排序规则。撤销只停止增强，已调整的时间保留；不会用旧备份覆盖新增媒体。</p>
  </div>`;
  const x = collect(card), endpoint = '/api/plugins/media-sort';
  const labels = { disabled: '未启用', enabled: '已启用', legacy: '旧规则，需确认升级', conflict: '规则冲突，禁止写入', unavailable: '不可用' };
  let snapshot = null, selected = null, pending = '', error = '', uncertain = false;
  const failure = value => /[\u3400-\u9fff]/u.test(String(value && value.message || '')) ? String(value.message) : '连接中断、请求超时或返回结果异常。';
  const candidates = () => snapshot && Array.isArray(snapshot.candidates) ? snapshot.candidates : [];
  const candidate = () => selected && candidates().find(row => row.id === selected.id && row.version === selected.version);
  const bindingCurrent = (binding, action) => {
    const row = candidate();
    return row && row.id === binding.id && row.version === binding.version && snapshot.status === 'ready'
      && snapshot.busy === false && !uncertain && row[action === 'apply' ? 'can_apply' : 'can_revert'] === true
      && (action === 'apply' ? ['disabled', 'legacy'].includes(row.state) : ['enabled', 'legacy'].includes(row.state));
  };
  const accept = result => {
    const value = result && result.plugin;
    if (!result || result.ok !== true || !value || value.schema_version !== 1 || value.id !== 'media_sort'
        || !['unknown', 'ready', 'unavailable'].includes(value.status) || typeof value.busy !== 'boolean'
        || !Array.isArray(value.candidates) || value.candidates.some(row => !row || typeof row.id !== 'string'
          || !row.id || typeof row.version !== 'string' || !row.version)
        || new Set(value.candidates.map(row => row.id)).size !== value.candidates.length) {
      throw new Error('未收到可核验的插件状态，请手动重新检测。');
    }
    snapshot = value;
    // Preserve an explicit database choice, binding it to this fresh snapshot.
    // Never choose the first newly discovered database on the owner's behalf.
    const row = selected && candidates().find(item => item.id === selected.id);
    selected = row ? { id: row.id, version: row.version } : null;
  };
  const draw = () => {
    const row = candidate(), locked = !!pending;
    setText(x.summary, snapshot ? snapshot.status === 'unknown' ? '未检测。' : snapshot.message || (snapshot.status === 'ready' ? '检测结果已保存。' : '当前不可用。') : '未检测。');
    const at = snapshot && snapshot.checked_at;
    setText(x.checked, typeof at === 'number' && Number.isFinite(at) && at > 0 ? '上次检测：' + new Date(at * 1000).toLocaleString('zh-CN') : '上次检测：未检测。');
    setText(x.activity, pending ? pending === 'confirm' ? '等待确认，尚未发送写入。' : '当前操作进行中…' : snapshot && snapshot.busy ? '服务端正在操作；可手动读取缓存核对，不能重复应用。' : '当前无进行中的操作。');
    setText(x.error, error); x.error.hidden = !error;
    x.choice.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = candidates().length ? '请选择数据库（不自动选择）' : '尚未检测到可选数据库'; x.choice.append(empty);
    for (const item of candidates()) {
      const option = document.createElement('option'); option.value = item.id;
      option.textContent = `${labels[item.state] || '状态未知'} · ${item.database_path || '位置未知'}`;
      x.choice.append(option);
    }
    x.choice.value = row ? row.id : ''; x.choice.disabled = locked || !candidates().length;
    setText(x.selection, candidates().length > 1 ? '找到多个数据库，请核对位置并明确选择；每次操作绑定所选数据库及检测版本。' : '操作绑定所选数据库及检测版本，位置仅展示。');
    setText(x.path, row && row.database_path || '未选择。');
    setText(x.state, row ? labels[row.state] || '未知。' : '未知。');
    setText(x.count, row && Number.isInteger(row.metadata_count) && row.metadata_count >= 0 ? String(row.metadata_count) : '--');
    setText(x.rule, row && row.rule_version != null ? String(row.rule_version) : '--');
    setText(x.backup, row && row.last_backup_path || '尚无备份记录。');
    setText(x.reason, row && row.reason || '');
    x.apply.textContent = row && row.state === 'legacy' ? '确认升级追更排序' : '应用追更排序';
    x.probe.disabled = locked; x.refresh.disabled = locked;
    x.apply.disabled = locked || !row || !bindingCurrent(row, 'apply');
    x.revert.disabled = locked || !row || !bindingCurrent(row, 'revert');
  };
  const read = async probe => {
    if (pending) return;
    pending = probe ? 'probe' : 'read'; draw();
    try {
      // API.post intentionally has no pairing retry: an uncertain write is never replayed.
      accept(probe ? await API.post(endpoint + '/probe', {}) : await API.req(endpoint, { method: 'GET' }));
      if (probe) uncertain = false;
      if (!uncertain) error = '';
    } catch (e) {
      error = failure(e);
    } finally { pending = ''; draw(); }
  };
  const mutate = async action => {
    if (pending) return;
    const row = candidate();
    if (!row || !bindingCurrent(row, action)) return;
    const binding = { id: row.id, version: row.version };
    pending = 'confirm'; draw();
    try {
      const applying = action === 'apply', upgrade = row.state === 'legacy';
      const title = applying ? upgrade ? '确认升级影视追更排序？' : '应用影视追更排序？' : upgrade ? '撤销已识别的旧版影视排序增强？' : '撤销影视追更排序增强？';
      const note = applying ? '先创建 Toolbox 备份，再改变所选数据库的排序规则。仅已识别电视剧新增路径置前，资料刷新不置前；电视端实际效果仍需验证。'
        : (upgrade ? '只移除已识别的旧版增强规则。' : '') + '只停止增强，已调整的时间保留；不会使用旧备份覆盖新增媒体。';
      if (!(await R1.ask(title, `数据库：<span class="r1-media-sort-path">${esc(row.database_path || '位置未知')}</span>。${note}可能唤醒磁盘。`, !applying))) return;
      if (!bindingCurrent(binding, action)) throw new Error('数据库选择或检测版本已变化，请重新检测。');
      pending = action; error = ''; draw();
      accept(await API.post(endpoint + '/' + action, { candidate_id: binding.id, version: binding.version, confirmed: true }));
      const observed = candidates().find(item => item.id === binding.id);
      if (observed) selected = { id: observed.id, version: observed.version };
      if (!observed || observed.state !== (applying ? 'enabled' : 'disabled')) {
        throw new Error('返回状态未确认操作结果。');
      }
    } catch (e) {
      uncertain = true;
      error = failure(e) + ' 操作结果未确认，未自动重试；请手动重新检测后再操作。';
    } finally { pending = ''; draw(); }
  };
  x.choice.onchange = () => {
    if (pending) return;
    const row = candidates().find(item => item.id === x.choice.value);
    selected = row ? { id: row.id, version: row.version } : null; draw();
  };
  x.probe.onclick = () => read(true); x.refresh.onclick = () => read(false);
  x.apply.onclick = () => mutate('apply'); x.revert.onclick = () => mutate('revert');
  draw(); read(false);
}

function r1OpenServiceManager() {
  if ($('#r1ServiceManager')) return;
  const el = mount(`<div class="fullmask r1-api-mask" id="r1ServiceManager" role="dialog" aria-modal="true" aria-labelledby="r1ServiceTitle"><div class="card fm-card r1-api-dialog">
    <div class="r1-api-head"><h2 id="r1ServiceTitle">服务与开发</h2><button class="btn" type="button" data-ref="close">关闭</button></div>
    <p>网页服务随容器常驻。小屏与 MCP 可分别开关，关闭 MCP 不会删除访问密钥。</p>
    <p class="sub" data-ref="loading" role="status">正在读取服务状态…</p><p class="st-crit" data-ref="error" role="alert" hidden></p><button class="btn" type="button" data-ref="retry" hidden>重新读取服务状态</button>
    <div class="r1-services" data-ref="content" hidden>
      <section class="r1-service-item"><h3>小屏界面</h3><label class="check"><input type="checkbox" data-ref="screenEnabled">启用 Toolbox 小屏</label><p class="sub" data-ref="screenState" role="status"></p><p class="sub">关闭后恢复官方显示，网页和其他 Toolbox 服务继续运行。</p></section>
      <section class="r1-service-item"><h3>局域网 AI 服务</h3><label class="check"><input type="checkbox" data-ref="mcpEnabled">启用 MCP 与 REST API</label><p class="sub" data-ref="mcpState" role="status"></p><button class="btn" type="button" data-ref="api">管理访问密钥</button></section>
      <section class="r1-service-item"><h3>开发模式</h3><p class="sub">仅对白名单来源开放固定诊断接口，不提供任意命令或原厂动作代理。到期后自动停止授权。</p>
        <form class="r1-api-create" data-ref="developerForm"><label class="check"><input type="checkbox" data-ref="developerEnabled">启用开发模式</label>
          <p class="sub" data-ref="developerState" role="status"></p>
          <label class="r1-api-input">允许来源（开启时必填）<textarea class="textfield mono" data-ref="developerIps" rows="3" aria-label="开发白名单" placeholder="192.168.31.0/24&#10;fd00::/64"></textarea><span class="sub">支持局域网 IPv4／IPv6、CIDR 与 IPv4 点分掩码。真实来源必须命中，不使用转发头；不允许公网全放行。</span></label>
          <label class="r1-api-input">授权时长（分钟）<input class="textfield" type="number" min="1" max="1440" step="1" value="60" data-ref="developerMinutes" aria-label="开发授权分钟"></label>
          <label class="check"><input type="checkbox" data-ref="developerCommands">允许受限命令诊断</label><p class="sub">仅增加服务端固定项目的诊断权限，不等于开放 Shell，也不授予 MCP 密钥。</p>
          <button class="btn primary" type="submit" data-ref="saveDeveloper">保存开发设置</button>
        </form>
      </section><div class="r1-api-actions"><button class="btn" type="button" data-ref="refresh">刷新实际状态</button></div>
    </div></div></div>`);
  const x = collect(el);
  let state = null, busy = false, closed = false, developerDirty = false, pollTimer = null, polls = 0, revision = 0;
  const stopPoll = () => { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; };
  const close = () => { closed = true; stopPoll(); document.removeEventListener('keydown', onKey); el.remove(); };
  const onKey = event => { if (event.key === 'Escape' && [...document.querySelectorAll('.fullmask')].at(-1) === el) close(); };
  const fail = error => { if (!closed) { setText(x.error, '服务设置操作失败：' + String(error && error.message || '请求未完成').slice(0, 160)); x.error.hidden = false; x.retry.hidden = !!state; } };
  const pending = value => {
    busy = value; el.setAttribute('aria-busy', String(value));
    // An unavailable GUI must still allow opting out or an explicit retry;
    // the host rechecks availability and all display preflight conditions.
    x.screenEnabled.disabled = value || !state || state.screen.busy === true;
    x.mcpEnabled.disabled = value || !state; x.saveDeveloper.disabled = value || !state; x.refresh.disabled = value; x.retry.disabled = value;
  };
  const draw = () => {
    if (closed) return;
    const screen = state.screen, developer = state.developer;
    x.content.hidden = false; x.loading.hidden = true; x.retry.hidden = true;
    x.screenEnabled.checked = screen.enabled === true; x.mcpEnabled.checked = state.mcp.enabled === true;
    setText(x.screenState, screen.last_error ? '切换未完成：' + String(screen.last_error)
      : screen.available !== true ? '小屏切换不可用。'
      : screen.busy === true ? `正在切换；当前实际显示${screen.active === true ? ' Toolbox' : screen.active === false ? '官方界面' : '尚未确认'}。${polls >= 30 ? '自动核对已结束，请点击刷新实际状态。' : ''}`
      : screen.active === true ? '实际状态：Toolbox 小屏已接管。'
      : screen.active === false ? '实际状态：官方界面；Toolbox 小屏未接管。' : '实际显示状态尚未确认，请刷新核对。');
    setText(x.mcpState, state.mcp.enabled === true ? '已开启，仍需要独立 Token 与对应权限。' : '已关闭，已有密钥保留。');
    r1SyncMcpService(state.mcp.enabled);
    setText(x.developerState, developer.active === true
      ? '开发窗口有效，截止时间：' + r1ApiTime(developer.expires_at) + '。每次请求仍须命中来源白名单。'
      : (developer.enabled === true ? '已保存开启意图，开发窗口尚未生效。' : '当前已关闭。') + (developer.reason ? ' ' + String(developer.reason) : ''));
    if (!developerDirty) {
      x.developerEnabled.checked = developer.enabled === true;
      x.developerIps.value = Array.isArray(developer.allowed_ips) ? developer.allowed_ips.join('\n') : '';
      x.developerMinutes.value = Number.isInteger(developer.minutes) ? String(developer.minutes) : '60';
      x.developerCommands.checked = developer.allow_commands === true;
    }
    pending(busy); stopPoll();
    if (screen.busy === true && polls < 30) {
      pollTimer = setTimeout(() => { pollTimer = null; if (!closed && !busy) { polls += 1; refresh().catch(fail); } }, 1000);
    }
  };
  const refresh = async () => {
    const requestedRevision = revision;
    const result = await API.get('/api/services');
    if (!result || result.ok !== true || !result.screen || !result.mcp || !result.developer) throw new Error(result && result.error || '未读取到服务状态');
    if (!closed && requestedRevision === revision) { state = result; draw(); }
  };
  const mutate = async (path, body) => {
    if (busy) return;
    revision += 1; pending(true); x.error.hidden = true; stopPoll();
    try {
      const result = await R1.post(path, body);
      if (!result || result.ok !== true || !result.services) throw new Error(result && result.error || '服务设置未保存');
      if (closed) return;
      if (path === '/api/services/developer') developerDirty = false;
      polls = 0; state = { ok: true, ...result.services }; draw();
    } catch (error) {
      fail(error);
      // A saved preference may precede a failed display transition. Read the
      // authoritative state once; never replay a possibly completed write.
      try { if (!closed) await refresh(); } catch (_) { if (!closed && state) draw(); }
    }
    finally { if (!closed) pending(false); }
  };
  x.screenEnabled.onchange = () => { const enabled = x.screenEnabled.checked; x.screenEnabled.checked = state.screen.enabled === true; mutate('/api/services/screen', { enabled }); };
  x.mcpEnabled.onchange = () => { const enabled = x.mcpEnabled.checked; x.mcpEnabled.checked = state.mcp.enabled === true; mutate('/api/services/mcp', { enabled }); };
  for (const input of [x.developerEnabled, x.developerIps, x.developerMinutes, x.developerCommands]) input.oninput = () => { developerDirty = true; };
  x.developerForm.onsubmit = event => {
    event.preventDefault(); if (busy) return;
    try {
      const allowed_ips = r1ApiRules(x.developerIps.value), minutes = Number(x.developerMinutes.value), enabled = x.developerEnabled.checked;
      if (enabled && !allowed_ips.length) throw new Error('开启开发模式必须填写至少一个来源 IP 或 CIDR 白名单。');
      if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) throw new Error('开发授权时长须为 1 至 1440 分钟的整数。');
      mutate('/api/services/developer', { enabled, allowed_ips, minutes, allow_commands: x.developerCommands.checked });
    } catch (error) { fail(error); }
  };
  x.refresh.onclick = async () => { if (busy) return; polls = 0; pending(true); x.error.hidden = true; try { await refresh(); } catch (error) { fail(error); } finally { if (!closed) pending(false); } };
  x.retry.onclick = () => x.refresh.onclick();
  x.api.onclick = () => { close(); r1OpenApiManager(); };
  x.close.onclick = close; document.addEventListener('keydown', onKey); document.body.append(el); pending(true);
  refresh().catch(fail).finally(() => { if (!closed) pending(false); });
}

/** Share verified service results across the two owner settings entrances. */
function r1SyncMcpService(enabled) {
  const card = document.querySelector('#set-r1-api > .card');
  if (card && card._setMcpEnabled && typeof enabled === 'boolean') card._setMcpEnabled(enabled);
}

/** The service switch reads no keys; Token metadata is loaded only on demand. */
function r1ApiSettings(card) {
  card.innerHTML = `<div class="r1-api-home"><div class="r1-api-intro"><div><div class="lbl">局域网 AI 与自动化</div><p class="hint">独立 Token 控制 MCP 与 REST 的角色、具体工具、文件和项目范围。Token 仅在创建或轮换时显示一次。</p></div><label class="r1-api-inline-switch"><span>启用 MCP／API</span><input type="checkbox" role="switch" data-ref="enabled" aria-label="启用 MCP 与 REST API" disabled></label></div><p class="sub" data-ref="serviceStatus" role="status">正在读取服务状态…</p><p class="st-crit" data-ref="error" role="alert" hidden></p><button class="btn" type="button" data-ref="retry" hidden>重试读取服务状态</button><div class="r1-api-entrypoints"><button class="btn primary" type="button" data-ref="add">添加 Token</button><button class="btn" type="button" data-ref="manage">Token 管理</button><button class="btn" type="button" data-ref="operations">Token 操作</button></div><p class="sub">开关即时保存；关闭后已有 Token 保留并停止访问。Token 操作用于查看连接地址、工具能力与调用记录。</p></div>`;
  const x = collect(card); card._r = x;
  let enabled = null, busy = false, revision = 0;
  const draw = () => {
    x.enabled.checked = enabled === true; x.enabled.disabled = busy || enabled === null;
    x.enabled.setAttribute('aria-busy', String(busy));
    setText(x.serviceStatus, busy ? '正在保存服务开关…' : enabled === null ? '服务状态尚未确认，请重试。' : enabled ? '已开启，客户端仍须使用独立 Token 和已授予的权限。' : '已关闭，已有 Token 保留。');
  };
  card._setMcpEnabled = value => { if (!busy) { revision += 1; enabled = value; draw(); } };
  const read = async () => {
    const requestedRevision = revision;
    // A missing owner session is local to this optional control. Explicit GET
    // keeps the upstream API wrapper from treating it as a domain-wide block.
    const result = await API.req('/api/services', { method: 'GET' });
    if (!result || result.ok !== true || !result.mcp || typeof result.mcp.enabled !== 'boolean') throw new Error('未读取到 MCP 服务状态。');
    if (requestedRevision === revision) { enabled = result.mcp.enabled; draw(); }
  };
  const fail = error => {
    const message = String(error && error.message || '请求未完成');
    setText(x.error, 'MCP 服务设置失败：' + (/need_pairing/.test(message) ? '需要先解锁网页管理，请完成配对后重试。' : message.slice(0, 160)));
    x.error.hidden = false; x.retry.hidden = false;
  };
  x.enabled.onchange = async () => {
    if (busy || enabled === null) return;
    const requested = x.enabled.checked; revision += 1; busy = true; x.error.hidden = true; x.retry.hidden = true; draw();
    try {
      const result = await R1.post('/api/services/mcp', { enabled: requested });
      if (!result || result.ok !== true || !result.services || !result.services.mcp || typeof result.services.mcp.enabled !== 'boolean') throw new Error(result && result.error || '服务开关未保存。');
      enabled = result.services.mcp.enabled;
    } catch (error) { fail(error); try { await read(); } catch (_) { /* Retain the last verified state, never replay a write. */ } }
    finally { busy = false; draw(); }
  };
  x.retry.onclick = async () => { if (busy) return; busy = true; x.retry.disabled = true; try { await read(); x.error.hidden = true; x.retry.hidden = true; } catch (error) { fail(error); } finally { busy = false; x.retry.disabled = false; draw(); } };
  x.add.onclick = () => r1OpenApiManager('create'); x.manage.onclick = () => r1OpenApiManager('manage'); x.operations.onclick = () => r1OpenApiManager('operations');
  read().catch(error => { fail(error); draw(); });
}

function r1ApiTime(value) {
  if (value === null || value === undefined || value === '') return '尚无记录';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : '时间未知';
}

/** Copy only after an explicit click; select the visible field on HTTP fallback. */
async function r1ApiCopy(field, status) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(field.value);
    else {
      field.focus(); field.select();
      if (!document.execCommand('copy')) throw new Error('manual_copy');
    }
    setText(status, '已复制。');
  } catch (_) {
    field.focus(); field.select();
    setText(status, '已选中文本，请手动复制。');
  }
}

/** The secret belongs to this temporary dialog only and is cleared on close. */
function r1ShowApiSecret(value) {
  let secret = String(value || '');
  if (!secret) throw new Error('服务端未返回新密钥，请撤销后重新创建。');
  const el = mount(`<div class="fullmask r1-api-secret" id="r1ApiSecret" role="dialog" aria-modal="true" aria-labelledby="r1ApiSecretTitle"><div class="card fm-card r1-api-dialog">
    <h2 id="r1ApiSecretTitle">保存新密钥</h2><p>Token 仅显示这一次。关闭后无法再次查看；请保存到 AI 客户端的安全配置中。轮换后旧 Token 已立即失效。</p>
    <label class="r1-api-input">访问密钥<textarea class="textfield mono" data-ref="token" rows="3" readonly spellcheck="false" autocomplete="off" aria-label="新访问密钥"></textarea></label>
    <button class="btn" type="button" data-ref="copyToken">复制密钥</button>
    <label class="r1-api-input">MCP 客户端配置<textarea class="textfield mono" data-ref="config" rows="10" readonly spellcheck="false" autocomplete="off" aria-label="MCP 客户端配置"></textarea></label>
    <p class="sub">支持远程 Streamable HTTP 的客户端可使用此配置；若客户端字段不同，填写相同 URL 和 Authorization 请求头。</p>
    <button class="btn" type="button" data-ref="copyConfig">复制 MCP 配置</button><p class="sub" data-ref="copyStatus" role="status"></p>
    <div class="fm-btns"><button class="btn primary" type="button" data-ref="close">已保存，关闭</button></div></div></div>`);
  const x = collect(el);
  x.token.value = secret;
  x.config.value = JSON.stringify({ mcpServers: { toolbox: { url: location.origin + '/mcp', headers: { Authorization: 'Bearer ' + secret } } } }, null, 2);
  const close = () => {
    secret = ''; x.token.value = ''; x.config.value = '';
    x.copyToken.onclick = null; x.copyConfig.onclick = null; x.close.onclick = null;
    document.removeEventListener('keydown', onKey, true); el.remove();
    const opener = document.querySelector('#r1ApiManager [data-api-nav][aria-current="page"]'); if (opener) opener.focus();
  };
  const onKey = event => { if (event.key === 'Escape') { event.stopImmediatePropagation(); close(); } };
  x.copyToken.onclick = () => r1ApiCopy(x.token, x.copyStatus);
  x.copyConfig.onclick = () => r1ApiCopy(x.config, x.copyStatus);
  x.close.onclick = close;
  document.addEventListener('keydown', onKey, true);
  document.body.append(el); x.close.focus();
}

function r1OpenApiManager(initialPage = 'manage') {
  if ($('#r1ApiManager')) return;
  const el = mount(`<div class="fullmask r1-api-mask" id="r1ApiManager" role="dialog" aria-modal="true" aria-labelledby="r1ApiTitle"><div class="card fm-card r1-api-dialog">
    <div class="r1-api-head"><h2 id="r1ApiTitle" data-ref="title">Token 管理</h2><button class="btn" type="button" data-ref="close" aria-label="关闭 API 管理">关闭</button></div>
    <p class="sub" data-ref="pageDescription"></p>
    <nav class="r1-api-pages" aria-label="Token 页面"><button class="btn" type="button" data-api-nav="create">添加 Token</button><button class="btn" type="button" data-api-nav="manage">Token 管理</button><button class="btn" type="button" data-api-nav="operations">Token 操作</button></nav>
    <p class="sub" data-ref="status" role="status">正在读取 API 设置…</p><p class="st-crit" data-ref="error" role="alert" hidden></p>
    <div data-ref="content" hidden>
      <div class="r1-api-state-line"><span class="pill" data-ref="serviceStatus"></span><button class="btn" type="button" data-ref="refresh">刷新当前数据</button></div>
      <section data-api-page="operations" hidden><h3>连接地址</h3><p class="sub">使用创建或轮换时保存的 Token 连接客户端。此页不代替 Token 发起 NAS 操作，也不接收旧 Token。</p>
      <div class="r1-api-endpoints"><label>MCP<input class="textfield mono" data-ref="mcp" readonly aria-label="MCP 地址"></label><label>REST 状态<input class="textfield mono" data-ref="restStatus" readonly aria-label="REST 状态地址"></label><label>REST 操作<input class="textfield mono" data-ref="restAction" readonly aria-label="REST 操作地址"></label><label>功能能力目录<input class="textfield mono" data-ref="restCapabilities" readonly aria-label="功能能力目录地址"></label></div>
      <details data-ref="deploymentManager"><summary>部署资源管理</summary><p class="sub">所有者先登记未来服务的固定资源，Token 再单独获权。这里不拉取镜像或创建、启停、删除容器；登记只委托固定仓库、卷、网络与端口。</p>
        <button class="btn" type="button" data-ref="loadDeployments">读取可用卷、网络与部署登记</button><p class="sub" data-ref="deploymentStatus" role="status">尚未探测本机资源；打开页面不会自动查询 Docker。</p>
        <form class="r1-api-deployment-create" data-ref="deploymentForm">
          <label class="r1-api-input">登记标签<input class="textfield" data-ref="deploymentLabel" maxlength="128" required autocomplete="off" placeholder="例如：局域网测试服务"></label>
          <label class="r1-api-input">固定服务名称<input class="textfield mono" data-ref="deploymentName" maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}" required autocomplete="off" placeholder="test-service"></label>
          <label class="r1-api-input">允许的镜像仓库<input class="textfield mono" data-ref="deploymentRepository" maxlength="256" required autocomplete="off" placeholder="registry.example/service"><span class="sub">只填写仓库，不含 tag 或 digest。部署由已授权 AI 另行规划并指定同仓库的 sha256 digest。</span></label>
          <label class="r1-api-input">已有网络<select class="textfield" data-ref="deploymentNetwork" required aria-label="部署网络"><option value="">请先读取可用网络</option></select><span class="sub">仅已有受限 bridge 网络或 none；none 不能发布端口。</span></label>
          <label class="r1-api-input">容器用户 UID:GID<input class="textfield mono" data-ref="deploymentUser" value="1000:1000" required pattern="[1-9][0-9]{0,9}:[1-9][0-9]{0,9}" aria-label="部署容器用户"><span class="sub">两个值均须非零，容器固定使用非 root 和只读根文件系统。</span></label>
          <div class="r1-api-deployment-limits"><label class="r1-api-input">内存上限（MiB）<input class="textfield" type="number" min="16" max="16384" step="1" value="64" data-ref="deploymentMemory" required></label><label class="r1-api-input">CPU 上限（核）<input class="textfield" type="number" min="0.01" max="8" step="0.01" value="0.5" data-ref="deploymentCpu" required></label></div>
          <fieldset class="r1-api-file-roots"><legend>已有具名卷（可选）</legend><p class="sub">不会创建卷，不接受宿主路径。卷是独立容器授权，请只委托服务确实需要的数据。</p><div data-ref="deploymentVolumes"></div><button class="btn" type="button" data-ref="addDeploymentVolume" disabled>增加具名卷</button></fieldset>
          <fieldset class="r1-api-file-roots"><legend>固定端口（可选）</legend><p class="sub">只允许具体局域网或回环 IP；不接受 0.0.0.0、:: 或公网地址。</p><div data-ref="deploymentPorts"></div><button class="btn" type="button" data-ref="addDeploymentPort">增加端口</button></fieldset>
          <button class="btn primary" type="submit" data-ref="registerDeployment" disabled>登记部署资源</button>
        </form><h3>已登记的部署资源</h3><div class="r1-api-list" data-ref="registeredDeployments"></div>
      </details>
      <details data-ref="fileAuthorityManager"><summary>文件变更与恢复</summary>
        <p class="sub">覆盖、改名、移动和新建目录使用所选磁盘的 Users/admin/.r1-toolbox-ops 专用恢复区，仅由所有者恢复界面管理。首次初始化需要所有者确认，核验 admin 及私有恢复区权限与实际挂载身份；外层目录仅提供定位信息。此操作可能唤醒磁盘。打开页面只读 Toolbox 登记，不扫描文件。撤销后停止新操作，已保留对象仍可检查和恢复。</p>
        <div data-ref="fileAuthorityRoots" class="r1-api-list"></div><p class="sub" data-ref="fileAuthorityStatus" role="status"></p><div data-ref="fileRecoveries" class="r1-api-list"></div>
      </details>
      <details data-ref="contracts"><summary>文件与 Docker 接口定义</summary>
        <p class="sub">只读、读写、管理员按工具和资源分别授权。定义分别标明已接入与待接入；查看定义不会启用工具或修改密钥。</p>
        <button class="btn" type="button" data-ref="loadContracts">读取接口定义</button>
        <p class="sub" data-ref="contractStatus" role="status"></p><div data-ref="contractList"></div>
        <label class="r1-api-input" data-ref="contractJson" hidden>接口定义 JSON<textarea class="textfield mono" data-ref="contractDefinition" rows="10" readonly spellcheck="false" aria-label="接口定义 JSON"></textarea></label>
        <button class="btn" type="button" data-ref="copyContracts" hidden>复制接口定义</button>
      </details>
      <h3>已接入的工具能力</h3><p class="sub">下列能力来自当前服务；客户端只能看到其 Token 实际获准的工具。角色、功能、具体工具与资源范围共同限制调用。</p><div class="r1-api-capabilities" data-ref="capabilities"></div>
      <h3>最近调用记录</h3><p class="sub">仅显示时间、密钥标识、来源、功能和结果，不记录令牌或请求内容。</p><div class="r1-api-list" data-ref="audit"></div></section>
      <section data-api-page="create" hidden>
      <h3 data-ref="formTitle">创建访问密钥</h3><form data-ref="form" class="r1-api-create">
        <label class="r1-api-input">密钥名称<input class="textfield" data-ref="name" aria-label="密钥名称" placeholder="例如：客厅 AI" maxlength="64" required autocomplete="off"></label>
        <label class="r1-api-input">操作角色<select class="textfield" data-ref="role" aria-label="操作角色"></select><span class="sub" data-ref="roleDescription"></span></label>
        <label class="r1-api-root-choice" data-ref="upgradePolicyLabel" hidden><input type="checkbox" data-ref="upgradePolicy"><span>启用逐工具授权<span class="sub">旧密钥默认保持原策略。明确启用后只以原十工具中符合当前角色和功能的权限作为初值，不自动增加新工具。</span></span></label>
        <fieldset class="r1-api-scopes"><legend>允许的功能</legend><div data-ref="scopes"></div></fieldset>
        <p class="sub r1-api-permission-note" data-ref="roleStatus" role="status"></p>
        <fieldset class="r1-api-scopes r1-api-tools"><legend>允许的具体工具</legend><p class="sub" data-ref="toolsStatus"></p><div data-ref="toolOptions"></div></fieldset>
        <fieldset class="r1-api-file-roots" data-ref="fileRoots" hidden><legend>允许访问的文件范围</legend><p class="sub">文件边界固定为 /drives/*/Users/admin；子目录相对 admin 根填写，无需添加 admin/ 前缀。根下有其他挂载不影响选择，实际操作仍拒绝进入子挂载。任意层级的 .r1-toolbox-ops 保留目录及其后代仅由所有者恢复界面管理，Token 不可访问或单独授权。旧 Users 根授权须重新选择，不自动换根或改写路径。各项工具是否可调用以当前能力目录为准。多个范围按并集生效；下级只读范围不会收窄上级读写授权。</p><p class="sub" data-ref="fileRootsStatus" role="status"></p><div data-ref="fileRootOptions"></div></fieldset>
        <fieldset class="r1-api-file-roots" data-ref="applicationInstalls" hidden><legend>允许新装的原厂应用</legend><p class="sub">安装需要管理员、应用功能和安装工具，以及这里明确选择的应用。只支持未安装的审计模块，不会自动卸载或更新已有应用；新应用默认不授权。原厂安装可能启动后台服务并访问其自身数据目录。</p><div data-ref="applicationInstallOptions"></div></fieldset>
        <fieldset class="r1-api-file-roots" data-ref="dockerProjects" hidden><legend>允许访问的已登记 Docker 项目</legend><p class="sub">现有项目按读取、启停重启、镜像更新分别授权；写操作需要管理员及对应工具。未来新服务使用下面独立的部署资源。</p><p class="sub" data-ref="dockerProjectsStatus"></p><div data-ref="dockerProjectOptions"></div></fieldset>
        <fieldset class="r1-api-file-roots" data-ref="dockerDeployments" hidden><legend>允许访问的未来部署资源</legend><p class="sub">先在「Token 操作 → 部署资源管理」登记固定资源。默认只选读取，部署／更新、启停重启和删除须管理员明确勾选，且仍须对应工具授权。</p><p class="sub" data-ref="dockerDeploymentsStatus"></p><div data-ref="dockerDeploymentOptions"></div></fieldset>
        <label class="r1-api-input">允许的来源 IP／网段（可选）<textarea class="textfield mono" data-ref="ips" rows="2" aria-label="允许的来源 IP" placeholder="192.168.31.0/24&#10;fd00::/64"></textarea><span class="sub">支持 IPv4／IPv6、CIDR 与 IPv4 点分掩码。留空允许持密钥的局域网客户端；不接受域名、通配符或公网全放行。</span></label>
        <label class="r1-api-input">有效期<select class="textfield" data-ref="expiry" aria-label="密钥有效期"><option value="keep" data-ref="keepExpiry" hidden>保持当前有效期</option><option value="7">7 天</option><option value="30">30 天</option><option value="90" selected>90 天</option><option value="365">365 天</option><option value="0">长期</option></select></label>
        <div class="r1-api-actions"><button class="btn primary" type="submit" data-ref="create">创建密钥</button><button class="btn" type="button" data-ref="cancelEdit" hidden>取消编辑</button></div>
      </form>
      <details data-ref="projectManager"><summary>登记 Docker 项目（仅管理元数据）</summary><p class="sub">登记、改标签或撤销只修改 Toolbox 的授权登记，不创建、更新、停止或删除 Docker 项目。</p><button class="btn" type="button" data-ref="loadProjects">读取项目候选与登记</button><p class="sub" data-ref="projectStatus" role="status"></p><div class="r1-api-list" data-ref="projectCandidates"></div><div class="r1-api-list" data-ref="registeredProjects"></div></details>
      </section><section data-api-page="manage" hidden><h3>访问密钥</h3><p class="sub" data-ref="keySummary"></p><div class="r1-api-list" data-ref="keys"></div></section>
    </div></div></div>`);
  const x = collect(el), allowedScopes = ['read', 'fan', 'light', 'screen', 'power', 'media', 'files', 'docker', 'apps', 'downloads', 'movie', 'albums', 'sharing', 'backup', 'system'];
  const roleLevel = { readonly: 0, readwrite: 1, admin: 2 }, roleLabels = { readonly: '只读', readwrite: '读写', admin: '管理员' };
  const defaultMinRole = { read: 'readonly', fan: 'readwrite', light: 'readwrite', screen: 'readwrite', power: 'admin', media: 'readwrite' };
  const legacyTools = new Set(['get_nas_status', 'get_media_status', 'set_fan_policy', 'release_fan', 'set_light_policy', 'set_rgb', 'set_screen', 'update_screen_settings', 'request_power', 'cancel_power']);
  let state = null, busy = false, closed = false, editing = null, contractsBusy = false, projectsBusy = false, deploymentsBusy = false, deploymentCandidates = null, activePage = '';
  const showPage = page => {
    if (!['create', 'manage', 'operations'].includes(page)) page = 'manage';
    activePage = page;
    el.querySelectorAll('[data-api-page]').forEach(section => { section.hidden = section.dataset.apiPage !== page; });
    el.querySelectorAll('[data-api-nav]').forEach(button => { button.setAttribute('aria-current', button.dataset.apiNav === page ? 'page' : 'false'); });
    setText(x.title, page === 'create' ? editing ? '编辑 Token' : '添加 Token' : page === 'manage' ? 'Token 管理' : 'Token 操作');
    setText(x.pageDescription, ({ create: '选择角色、功能、具体工具与资源范围，再创建独立访问密钥。', manage: '查看授权与使用情况，编辑、停用、启用、轮换或撤销已有 Token。', operations: '查看客户端连接方式、真实工具能力、接口定义和调用结果。' })[page]);
    x.error.hidden = true;
  };
  el.querySelectorAll('[data-api-nav]').forEach(button => { button.onclick = () => { if (!busy) showPage(button.dataset.apiNav); }; });
  let dirty = { role: false, scopes: false, tools: false, files: false, projects: false, deployments: false, applications: false };
  const policyDirty = () => Object.values(dirty).some(Boolean);
  const exactPolicy = () => !editing || editing.policy_version === 3 || x.upgradePolicy.checked;
  const selectedScopes = () => [...x.scopes.querySelectorAll('input:checked')].map(input => input.value);
  const projectRows = () => Array.isArray(state && state.docker_project_catalog && state.docker_project_catalog.projects)
    ? state.docker_project_catalog.projects.filter(row => row && /^project_[0-9a-f]{24}$/.test(row.id) && typeof row.label === 'string') : [];
  const deploymentRows = () => Array.isArray(state && state.docker_deployment_catalog && state.docker_deployment_catalog.deployments)
    ? state.docker_deployment_catalog.deployments.filter(row => row && /^deployment_[0-9a-f]{24}$/.test(row.id) && typeof row.label === 'string') : [];
  const applicationRows = () => Array.isArray(state && state.application_install_catalog)
    ? state.application_install_catalog.filter(row => row && Number.isSafeInteger(row.application_id) && typeof row.label === 'string' && typeof row.effect_summary === 'string') : [];
  const currentTools = () => (Array.isArray(state && state.tools) ? state.tools : []).map(tool => ({ ...tool,
    scope: tool.scope || (tool._meta || {})['io.github.shinku-chen.r1-toolbox/requiredScope'],
    min_role: tool.min_role || (tool._meta || {})['io.github.shinku-chen.r1-toolbox/minimumRole'],
  })).filter(tool => tool && /^[a-z_]{1,64}$/.test(tool.name) && allowedScopes.includes(tool.scope) && Object.hasOwn(roleLevel, tool.min_role) && tool.implemented !== false);
  const close = () => {
    if ($('#r1ApiSecret')) return;
    closed = true; document.removeEventListener('keydown', onKey); el.remove();
  };
  const onKey = event => { if (event.key === 'Escape' && [...document.querySelectorAll('.fullmask')].at(-1) === el) close(); };
  const fail = error => {
    if (closed) return;
    const message = String((error && error.message) || '请求未完成').slice(0, 160);
    setText(x.error, 'API 管理操作失败：' + message); x.error.hidden = false;
  };
  const pending = value => {
    busy = value; el.setAttribute('aria-busy', String(value));
    el.querySelectorAll('[data-api-nav]').forEach(button => { button.disabled = value; });
    x.create.disabled = value || !state || state.storage_ok !== true || (filesSelected() && !fileRoots().length && (!editing || dirty.files));
    x.cancelEdit.disabled = value;
    x.refresh.disabled = value;
    x.keys.querySelectorAll('button').forEach(button => { button.disabled = value || !state || state.storage_ok !== true; });
    x.loadDeployments.disabled = value || deploymentsBusy;
    x.deploymentForm.querySelectorAll('input,select,button').forEach(input => { input.disabled = value || deploymentsBusy || !state || state.storage_ok !== true; });
    x.registerDeployment.disabled = value || deploymentsBusy || !deploymentCandidates || !state || state.storage_ok !== true;
    x.addDeploymentVolume.disabled = value || deploymentsBusy || !deploymentCandidates || !deploymentCandidates.volumes.length;
    x.registeredDeployments.querySelectorAll('button').forEach(button => { button.disabled = value || deploymentsBusy || button.dataset.revoked === 'true'; });
    for (const row of x.fileAuthorityRoots.querySelectorAll('[data-authority-root]')) {
      row.querySelector('[data-authority-preflight]').disabled = value;
      row.querySelector('[data-authority-init]').disabled = value || row.dataset.readOnly === 'true' || (row.dataset.enabled === 'true' && row.dataset.legacyTrust !== 'true');
      row.querySelector('[data-authority-inspect]').disabled = value || row.dataset.registered !== 'true';
      row.querySelector('[data-authority-revoke]').disabled = value || row.dataset.enabled !== 'true';
    }
    x.fileRecoveries.querySelectorAll('button').forEach(button => { button.disabled = value || button.dataset.available !== 'true' || button.dataset.completed === 'true'; });
  };
  const names = () => new Map((Array.isArray(state && state.scope_catalog) ? state.scope_catalog : []).map(scope => [scope.id, scope.label || scope.id]));
  const filesSelected = () => !!x.scopes.querySelector('input[value="files"]:checked');
  const adminBoundaryReady = () => !!(state && state.file_root_catalog && state.file_root_catalog.boundary_version === 3);
  const fileRoots = () => adminBoundaryReady() && state.file_root_catalog.available === true && Array.isArray(state.file_root_catalog.roots)
    ? state.file_root_catalog.roots.filter(root => root && /^root_[0-9a-f]{24}$/.test(root.id) && typeof root.label === 'string' && typeof root.mount === 'string'
      && /^\/drives\/[^/\\]+\/Users\/admin$/.test(root.mount) && !['.', '..'].includes(root.mount.split('/')[2])
      && !/[\u0000-\u001f\u007f-\u009f]/.test(root.mount) && typeof root.read_only === 'boolean') : [];
  const roles = () => (Array.isArray(state && state.role_catalog) ? state.role_catalog : []).filter(role => Object.hasOwn(roleLevel, role.id));
  const scopeMinimum = scope => Object.hasOwn(roleLevel, scope.min_role) ? scope.min_role : (defaultMinRole[scope.id] || 'readonly');
  const scopeName = (id, labels) => {
    const scope = (Array.isArray(state.scope_catalog) ? state.scope_catalog : []).find(item => item.id === id);
    return (labels.get(id) || id) + (scope && scope.implemented === false ? '（待接入）' : '');
  };
  /** A path grant is relative to an independently discovered storage root. */
  const validSubpath = value => typeof value === 'string' && [...value].length <= 1024 && new TextEncoder().encode(value).length <= 4096 && !/[\\\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value) && !/^[A-Za-z]:/.test(value)
    && (!value || value.split('/').every(part => part && part !== '.' && part !== '..' && !/^\.r1-toolbox-ops$/iu.test(part)));
  const updatePermissions = () => {
    const role = x.role.value, legacy = role === 'legacy' && editing, level = legacy ? Infinity : roleLevel[role] ?? -1;
    const selected = new Set([...x.scopes.querySelectorAll('input:checked')].map(input => input.value));
    const limited = [];
    for (const scope of Array.isArray(state && state.scope_catalog) ? state.scope_catalog : []) {
      const input = [...x.scopes.querySelectorAll('input')].find(item => item.value === scope.id);
      if (!input) continue;
      input.disabled = !!legacy;
      const minRole = scopeMinimum(scope), tooLow = level < roleLevel[minRole];
      const note = input.closest('label').querySelector('[data-scope-note]');
      note.textContent = scope.implemented === false ? '待接入，仅预授权；当前没有可调用工具。'
        : tooLow ? `当前角色不可调用此类工具；需要${roleLabels[minRole]}角色。` : '已接入；仍需同时满足所选功能权限。';
      if (selected.has(scope.id) && scope.implemented !== false && tooLow) limited.push(String(scope.label || scope.id));
    }
    const description = roles().find(item => item.id === role);
    setText(x.roleDescription, legacy ? '保留旧密钥原有精确权限，不会自动升级成管理员。明确选择新角色后才更新权限策略。' : description ? String(description.description || '') : '未获得有效角色配置，请刷新后重试。');
    setText(x.roleStatus, legacy ? '当前仅编辑名称、来源和有效期；原有功能权限保持。选择新角色后可重新配置。' : limited.length ? `已选择${limited.join('、')}，当前角色不可调用这些工具。提高角色等级后仍只授予已勾选的功能。`
      : '提高角色等级不会自动勾选功能或工具；角色、具体工具、文件和项目范围须同时满足。');
    setText(x.toolsStatus, exactPolicy() ? '只显示当前真正接入的工具；新增工具默认不勾选，缺少功能或角色等级不足时不能选择。'
      : '保留旧策略的原十工具权限；明确勾选“启用逐工具授权”后才能修改具体工具。');
    for (const input of x.toolOptions.querySelectorAll('input')) {
      const tool = currentTools().find(item => item.name === input.value);
      const allowed = tool && selected.has(tool.scope) && level >= roleLevel[tool.min_role];
      input.disabled = !exactPolicy() || !allowed;
      if (exactPolicy() && !allowed && input.checked) { input.checked = false; if (editing) dirty.tools = true; }
    }
    x.fileRoots.hidden = !filesSelected();
    x.applicationInstalls.hidden = !selected.has('apps');
    for (const input of x.applicationInstallOptions.querySelectorAll('input')) {
      input.disabled = !exactPolicy() || !selected.has('apps') || role !== 'admin';
      if (input.checked && (role !== 'admin' || !selected.has('apps'))) { input.checked = false; if (editing) dirty.applications = true; }
    }
    for (const item of x.fileRootOptions.querySelectorAll('.r1-api-file-root')) {
      const checked = item.querySelector('[data-root-check]').checked;
      const writable = role !== 'readonly' && item.dataset.readOnly !== 'true' && item.dataset.available !== 'false';
      for (const range of item.querySelectorAll('[data-root-range]')) {
        const access = range.querySelector('[data-root-access]');
        access.querySelector('option[value="readwrite"]').disabled = !writable;
        if (!writable && access.value !== 'readonly' && (!editing || (dirty.role && role === 'readonly'))) {
          access.value = 'readonly'; if (editing) dirty.files = true;
        }
        access.disabled = !checked;
        range.querySelector('[data-root-path]').disabled = !checked;
      }
      item.querySelector('[data-root-fields]').hidden = !checked;
      setText(item.querySelector('[data-root-note]'), item.dataset.readOnly === 'true' ? '此存储根为只读挂载，只能授权读取。'
        : item.dataset.available === 'false' ? '原范围当前失效，须明确重新选择 admin 根。旧 Users 授权和路径不自动转换；仅编辑元数据仍保留原绑定。' : role === 'readonly' ? '当前角色只能授权读取；切换为读写或管理员后可选择读写。' : '授权仅限 admin 根内已勾选的相对子目录；实际操作不进入子挂载，仍须选择已接入的具体工具。');
    }
    x.dockerProjects.hidden = !selected.has('docker');
    for (const row of x.dockerProjectOptions.querySelectorAll('[data-project-row]')) {
      const selectedProject = row.querySelector('[data-project-check]');
      selectedProject.disabled = !exactPolicy() || selectedProject.dataset.available !== 'true';
      for (const input of row.querySelectorAll('[data-project-operation]')) {
        const writable = input.value !== 'inspect';
        input.disabled = selectedProject.disabled || !selectedProject.checked || (writable && role !== 'admin');
        if (writable && role !== 'admin' && input.checked) { input.checked = false; if (editing) dirty.projects = true; }
      }
    }
    setText(x.dockerProjectsStatus, projectRows().some(row => row.available === true && row.revoked !== true)
      ? '逐项目选择读取、启停重启和镜像更新；后两项需要管理员及对应具体工具，默认不选。' : '暂无可用的已登记项目；请在下方读取候选并登记，不能凭名称创建授权。');
    x.dockerDeployments.hidden = !selected.has('docker');
    for (const row of x.dockerDeploymentOptions.querySelectorAll('[data-deployment-row]')) {
      const selectedDeployment = row.querySelector('[data-deployment-check]');
      selectedDeployment.disabled = !exactPolicy() || selectedDeployment.dataset.available !== 'true';
      for (const input of row.querySelectorAll('[data-deployment-operation]')) {
        const writable = input.value !== 'inspect';
        input.disabled = selectedDeployment.disabled || !selectedDeployment.checked || (writable && role !== 'admin');
        if (writable && role !== 'admin' && input.checked) { input.checked = false; if (editing) dirty.deployments = true; }
      }
    }
    setText(x.dockerDeploymentsStatus, deploymentRows().some(row => row.revoked !== true)
      ? '登记不代表当前已通过实机核验；每次执行还会检查实际 Engine、资源和容器身份。写范围默认不选。'
      : '暂无未来部署资源；可先只授权现有项目，或在「Token 操作」中登记资源。');
    pending(busy);
  };
  const appendRange = (item, draft = {}) => {
    const range = mount('<div class="r1-api-file-range" data-root-range><label class="r1-api-input">admin 内子目录（可选）<input class="textfield" data-root-path maxlength="2048" autocomplete="off" placeholder="例如：电影/2026（相对 admin 根）"><span class="sub">相对 admin 根填写，无需添加 admin/ 前缀；空表示整个已选 admin 根。最多 1024 个 Unicode 字符，相对目录以 / 分隔。</span></label><label class="r1-api-input">访问方式<select class="textfield" data-root-access><option value="readonly">只读</option><option value="readwrite">读写</option></select></label><button class="btn" type="button" data-remove-range>移除此范围</button></div>');
    const path = range.querySelector('[data-root-path]'), access = range.querySelector('[data-root-access]');
    path.value = typeof draft.path === 'string' ? draft.path : ''; access.value = draft.access === 'readwrite' ? 'readwrite' : 'readonly';
    path.setAttribute('aria-label', '文件子目录'); access.setAttribute('aria-label', '文件访问方式');
    path.oninput = () => { dirty.files = true; };
    access.onchange = () => { dirty.files = true; };
    range.querySelector('[data-remove-range]').onclick = () => {
      range.remove(); dirty.files = true;
      if (!item.querySelector('[data-root-range]')) { item.querySelector('[data-root-check]').checked = false; appendRange(item); }
      updatePermissions();
    };
    item.querySelector('[data-root-ranges]').append(range);
  };
  const drawFileRoots = (prefill = null) => {
    const drafts = new Map([...x.fileRootOptions.querySelectorAll('.r1-api-file-root')].map(item => [item.dataset.rootId,
      { checked: item.querySelector('[data-root-check]').checked, ranges: [...item.querySelectorAll('[data-root-range]')].map(range => ({ path: range.querySelector('[data-root-path]').value, access: range.querySelector('[data-root-access]').value })) }]));
    const roots = [...fileRoots()];
    if (editing) for (const grant of prefill || editing.file_roots || []) if (!roots.some(root => root.id === grant.id)) roots.push({ id: grant.id, label: '失效的原文件范围；需重选 admin 根', mount: '', read_only: false, unavailable: true });
    x.fileRootOptions.replaceChildren();
    const reason = state.file_root_catalog && state.file_root_catalog.reason;
    setText(x.fileRootsStatus, !adminBoundaryReady() ? '尚未取得 admin 专用 v3 根目录，请刷新后重新选择文件范围；旧 Users 授权不会自动转换。'
      : roots.length ? '请至少勾选一个 admin 存储根；子目录留空表示整个已选 admin 根。' : `没有已确认的存储根，不能创建文件权限。${reason ? '原因：' + String(reason) : '请确认磁盘已挂载后刷新。'}`);
    for (const root of roots) {
      const item = mount('<article class="r1-api-file-root"><label class="r1-api-root-choice"><input type="checkbox" data-root-check><span data-root-label></span></label><p class="sub mono" data-root-mount></p><div class="r1-api-file-fields" data-root-fields hidden><div data-root-ranges></div><button class="btn" type="button" data-add-range>增加同根子目录范围</button></div><p class="sub" data-root-note></p></article>');
      item.dataset.rootId = root.id; item.dataset.readOnly = String(root.read_only); item.dataset.available = String(!root.unavailable);
      setText(item.querySelector('[data-root-label]'), root.label);
      setText(item.querySelector('[data-root-mount]'), root.mount);
      const checkbox = item.querySelector('[data-root-check]'); checkbox.value = root.id;
      const saved = prefill ? prefill.filter(grant => grant.id === root.id) : null;
      const draft = saved ? { checked: saved.length > 0, ranges: saved } : drafts.get(root.id);
      checkbox.checked = !!(draft && draft.checked);
      for (const range of draft && draft.ranges.length ? draft.ranges : [{}]) appendRange(item, range);
      checkbox.onchange = () => { dirty.files = true; updatePermissions(); };
      item.querySelector('[data-add-range]').onclick = () => { appendRange(item); dirty.files = true; updatePermissions(); };
      x.fileRootOptions.append(item);
    }
  };
  const drawTools = (chosen = null) => {
    const selected = chosen || new Set(x.toolOptions.children.length ? [...x.toolOptions.querySelectorAll('input:checked')].map(input => input.value) : ['get_nas_status']);
    x.toolOptions.replaceChildren();
    for (const tool of currentTools()) {
      const row = mount('<label class="r1-api-scope"><input type="checkbox"><span><b></b><span class="sub mono" data-tool-name></span><span class="sub" data-tool-note></span></span></label>');
      const input = row.querySelector('input'); input.value = tool.name; input.checked = selected.has(tool.name);
      setText(row.querySelector('b'), tool.title || tool.name); setText(row.querySelector('[data-tool-name]'), tool.name);
      setText(row.querySelector('[data-tool-note]'), `最低角色：${roleLabels[tool.min_role]}；功能：${names().get(tool.scope) || tool.scope}。`);
      input.onchange = () => { dirty.tools = true; };
      x.toolOptions.append(row);
    }
  };
  const drawProjects = (chosen = null) => {
    const previous = new Map([...x.dockerProjectOptions.querySelectorAll('[data-project-row]')].map(row =>
      [row.querySelector('[data-project-check]').value, [...row.querySelectorAll('[data-project-operation]:checked')].map(input => input.value)]));
    const selected = chosen || new Set([...x.dockerProjectOptions.querySelectorAll('[data-project-check]:checked')].map(input => input.value));
    x.dockerProjectOptions.replaceChildren();
    for (const project of projectRows()) {
      const row = mount('<div data-project-row><label class="r1-api-scope"><input type="checkbox" data-project-check><span><b></b><span class="sub" data-project-note></span></span></label><div class="r1-api-project-operations" data-project-operations></div></div>');
      const input = row.querySelector('[data-project-check]'); input.value = project.id; input.checked = selected.has(project.id);
      input.dataset.available = String(project.available === true && project.revoked !== true);
      setText(row.querySelector('b'), project.label); setText(row.querySelector('[data-project-note]'), input.dataset.available === 'true' ? '只授权当前登记绑定；新工具与写操作须另行勾选。' : project.reason || '登记已撤销或项目绑定暂不可用。');
      const saved = chosen !== null ? (editing && (editing.docker_projects || []).find(grant => grant.id === project.id) || {}).operations
        : previous.get(project.id);
      for (const [operation, label] of [['inspect', '读取状态与摘要'], ['control', '启停／重启（管理员）'], ['update_image', '镜像更新（管理员）']]) {
        const option = mount('<label class="r1-api-scope"><input type="checkbox" data-project-operation><span></span></label>');
        const check = option.querySelector('input'); check.value = operation;
        check.checked = Array.isArray(saved) ? saved.includes(operation) : operation === 'inspect';
        option.querySelector('span').textContent = label;
        check.onchange = () => { dirty.projects = true; };
        row.querySelector('[data-project-operations]').append(option);
      }
      input.onchange = () => { dirty.projects = true; updatePermissions(); };
      x.dockerProjectOptions.append(row);
    }
  };
  const drawDeployments = (chosen = null) => {
    const drafts = new Map([...x.dockerDeploymentOptions.querySelectorAll('[data-deployment-row]')].map(row =>
      [row.querySelector('[data-deployment-check]').value, [...row.querySelectorAll('[data-deployment-operation]:checked')].map(input => input.value)]));
    const selected = chosen || new Set([...x.dockerDeploymentOptions.querySelectorAll('[data-deployment-check]:checked')].map(input => input.value));
    const resources = [...deploymentRows()];
    if (editing) for (const grant of editing.docker_deployments || []) if (!resources.some(row => row.id === grant.id)) resources.push({ id: grant.id, label: '当前未确认的原部署资源', revoked: true });
    x.dockerDeploymentOptions.replaceChildren();
    for (const resource of resources) {
      const row = mount('<div data-deployment-row><label class="r1-api-scope"><input type="checkbox" data-deployment-check><span><b></b><span class="sub" data-deployment-note></span></span></label><div class="r1-api-project-operations" data-deployment-operations></div></div>');
      const input = row.querySelector('[data-deployment-check]'); input.value = resource.id; input.checked = selected.has(resource.id);
      input.dataset.available = String(resource.revoked !== true);
      setText(row.querySelector('b'), resource.label);
      setText(row.querySelector('[data-deployment-note]'), resource.revoked ? '登记已撤销或当前无法确认；只改密钥元数据仍保留原授权。' : '固定资源合同；不会把同名现有容器自动收编。');
      const saved = chosen !== null ? (editing && (editing.docker_deployments || []).find(grant => grant.id === resource.id) || {}).operations : drafts.get(resource.id);
      for (const [operation, label] of [['inspect', '读取登记与本键任务'], ['deploy', '新建／同仓库镜像更新（管理员）'], ['control', '启停／重启（管理员）'], ['remove', '删除已停止容器，保留卷（管理员）']]) {
        const option = mount('<label class="r1-api-scope"><input type="checkbox" data-deployment-operation><span></span></label>');
        const check = option.querySelector('input'); check.value = operation;
        check.checked = Array.isArray(saved) ? saved.includes(operation) : operation === 'inspect';
        option.querySelector('span').textContent = label;
        check.onchange = () => { dirty.deployments = true; };
        row.querySelector('[data-deployment-operations]').append(option);
      }
      input.onchange = () => { dirty.deployments = true; updatePermissions(); };
      x.dockerDeploymentOptions.append(row);
    }
  };
  const drawApplications = (chosen = null) => {
    const selected = chosen || new Set([...x.applicationInstallOptions.querySelectorAll('input:checked')].map(input => Number(input.value)));
    x.applicationInstallOptions.replaceChildren();
    for (const app of applicationRows()) {
      const row = mount('<label class="r1-api-scope"><input type="checkbox" data-application-id><span><b></b><span class="sub"></span></span></label>');
      const input = row.querySelector('input'); input.value = String(app.application_id); input.checked = selected.has(app.application_id);
      setText(row.querySelector('b'), `${app.label}（${app.application_id}）`); setText(row.querySelector('.sub'), app.effect_summary);
      input.onchange = () => { dirty.applications = true; }; x.applicationInstallOptions.append(row);
    }
    if (!x.applicationInstallOptions.children.length) x.applicationInstallOptions.append(mount('<p class="sub">当前版本没有可授权的新装应用。</p>'));
  };
  const drawFileAuthorities = () => {
    x.fileAuthorityRoots.replaceChildren();
    // Inspection cards belong to one registry snapshot. A refresh must never
    // keep an old restore action alive after its custody layout has changed.
    x.fileRecoveries.replaceChildren();
    const registry = state.file_mutation_authority || {}, entries = Array.isArray(registry.authorities) ? registry.authorities : [];
    const legacy = entry => entry && (['legacy_boundary', 'legacy_layout'].includes(entry.status)
      || ['legacy_users_boundary_v2', 'legacy_recovery_layout'].includes(entry.reason));
    setText(x.fileAuthorityStatus, registry.ok === false ? '文件恢复登记暂不可用。初始化必须在 NAS 上通过权限检查。' : '尚未读取磁盘恢复清单。');
    for (const root of fileRoots()) {
      // An old custody layout can share the current admin root ID. Never offer
      // its disabled registration as a revoked new-layout authority to enable.
      if (entries.some(entry => legacy(entry) && entry.root_id === root.id)) continue;
      const registration = entries.find(entry => entry.root_id === root.id);
      const legacyTrust = !!registration && registration.trust_boundary === 'complete_ancestors_legacy';
      const recoveryBindingCurrent = () => {
        const currentRegistry = state && state.file_mutation_authority;
        const currentEntries = currentRegistry && Array.isArray(currentRegistry.authorities) ? currentRegistry.authorities : [];
        const currentRoots = fileRoots().filter(candidate => candidate.id === root.id);
        const currentRegistrations = currentEntries.filter(entry => entry && entry.root_id === root.id);
        if (closed || !currentRegistry || currentRegistry.ok !== true || !registration
            || !Number.isSafeInteger(registration.revision) || currentRoots.length !== 1
            || currentRoots[0].mount !== root.mount || currentRegistrations.length !== 1) return false;
        const currentRegistration = currentRegistrations[0], location = currentRegistration.recovery_location;
        // Revocation stops new mutations, but a current-layout owner may still
        // restore retained objects. Bind the action to layout and revision.
        return !legacy(currentRegistration) && currentRegistration.trust_boundary === 'admin'
          && currentRegistration.revision === registration.revision
          && !!location && location.boundary === 'admin' && location.relative_path === '.r1-toolbox-ops'
          && location.layout === 'admin_custody_v1' && location.anchor_component === 'admin_root';
      };
      const row = mount('<article class="r1-api-audit r1-api-authority"><strong></strong><p class="sub" data-authority-state></p><div class="r1-api-actions"><button class="btn" type="button" data-authority-preflight>检查初始化条件</button><button class="btn" type="button" data-authority-init>初始化恢复区</button><button class="btn" type="button" data-authority-inspect>检查保留对象</button><button class="btn" type="button" data-authority-revoke>停止新变更</button></div></article>');
      setText(row.querySelector('strong'), root.label); setText(row.querySelector('[data-authority-state]'), legacyTrust
        ? '旧完整祖先信任策略登记；刷新不会升级。须由所有者核验并升级后，才能按 admin 信任边界恢复保留对象。'
        : registration ? registration.enabled ? '已登记；每次操作仍核验实际身份与权限。' : '已撤销；保留对象未删除。' : '未初始化。');
      row.dataset.authorityRoot = root.id; row.dataset.readOnly = String(root.read_only); row.dataset.registered = String(!!registration); row.dataset.enabled = String(!!(registration && registration.enabled)); row.dataset.legacyTrust = String(legacyTrust);
      const init = row.querySelector('[data-authority-init]'), inspect = row.querySelector('[data-authority-inspect]'), revoke = row.querySelector('[data-authority-revoke]');
      init.disabled = busy || root.read_only || !!(registration && registration.enabled && !legacyTrust); inspect.disabled = busy || !registration; revoke.disabled = busy || !registration || !registration.enabled;
      if (legacyTrust) setText(init, '核验并升级信任策略');
      else if (registration && !registration.enabled) setText(init, '重新启用恢复区');
      const perform = async action => {
        if (busy) return;
        const reading = ['inspect', 'preflight'].includes(action);
        if (!reading && !(await R1.ask(action === 'initialize' ? legacyTrust ? '核验并升级恢复区信任策略？' : '初始化文件恢复区？' : '停止新的文件变更？', action === 'initialize'
          ? legacyTrust ? `将核验「${esc(root.label)}」现有 Users/admin/.r1-toolbox-ops 的原 inode，绑定当前 admin 并切换信任策略；不搬动、不重建已有恢复区。只有 admin 及私有恢复区权限与实际挂载身份检查通过，才启用新变更授权。外层目录仅提供定位信息，可能唤醒磁盘。`
            : `将在「${esc(root.label)}」的 Users/admin/.r1-toolbox-ops 创建专用恢复区，可能唤醒磁盘。只有 admin 及私有恢复区权限与实际挂载身份检查通过才启用；外层目录仅提供定位信息。已有同名目录不会被接管，旧 Users 恢复区不会自动搬动或复用。`
          : '停止新的文件变更授权。保留的对象和恢复记录不会删除。'))) return;
        pending(true);
        try {
          const result = await R1.post('/api/files/authority/' + action, { root_id: root.id, ...(reading ? {} : { confirmed: true }) });
          if (!result || result.ok !== true) throw new Error(result && result.error && result.error.code || '操作结果未确认');
          if (action === 'preflight') {
            setText(x.fileAuthorityStatus, result.ready === true ? '初始化条件已通过；权限信任锚为 admin 及私有恢复区，外层目录仅提供定位信息。仍需明确初始化后才能执行文件变更。'
              : `初始化条件未通过：${(result.blockers || []).join('、') || '身份或权限无法核验'}。`);
            x.fileRecoveries.replaceChildren();
            const evidence = document.createElement('pre'); evidence.className = 'sub mono'; evidence.style.whiteSpace = 'pre-wrap'; evidence.style.overflowWrap = 'anywhere';
            evidence.textContent = JSON.stringify({ trust_boundary: result.trust_boundary, registration_trust_boundary: result.registration_trust_boundary,
              owner_revalidation_required: result.owner_revalidation_required, recovery_location: result.recovery_location, recovery_anchor: result.recovery_anchor,
              checks: result.checks, source_binding: result.source_binding, existing_directory: result.existing_directory }, null, 2);
            x.fileRecoveries.append(evidence);
          } else if (action === 'inspect') {
            x.fileRecoveries.replaceChildren();
            for (const recovery of Array.isArray(result.recoveries) ? result.recoveries : []) {
              if (!/^operation_[0-9a-f]{32}$/.test(recovery.recovery_id)) continue;
              const item = mount('<article class="r1-api-audit r1-api-recovery"><strong class="mono"></strong><p class="sub"></p><button class="btn" type="button">恢复到原路径</button></article>');
              const restorePath = validSubpath(recovery.path) && recovery.path ? recovery.path : null;
              setText(item.querySelector('strong'), restorePath || recovery.recovery_id);
              setText(item.querySelector('.sub'), `${recovery.recovery_id}；${recovery.state || 'result_unconfirmed'}；保留对象：${(recovery.objects || []).join('、') || '无'}${restorePath ? '' : '；原路径无法核验，暂不能恢复。'}`);
              const canRestore = !!(recovery.objects || []).length && !!restorePath && recovery.state !== 'restored' && recoveryBindingCurrent();
              const button = item.querySelector('button'); button.disabled = !canRestore;
              button.dataset.available = String(canRestore);
              button.onclick = async () => {
                if (busy || !restorePath) return;
                const bindingChanged = () => {
                  x.fileRecoveries.replaceChildren();
                  setText(x.fileAuthorityStatus, '恢复登记或根绑定已变化，请重新检查保留对象。');
                };
                if (!recoveryBindingCurrent()) { bindingChanged(); return; }
                if (!(await R1.ask('恢复保留对象？', `恢复「${esc(root.label)}／${esc(restorePath)}」。仅使用该记录绑定的原路径；目标存在时保留双方并拒绝覆盖。可能唤醒磁盘。`))) return;
                if (busy || !recoveryBindingCurrent()) { bindingChanged(); return; }
                pending(true);
                try {
                  const restored = await R1.post('/api/files/authority/restore', { root_id: root.id, recovery_id: recovery.recovery_id, confirmed: true });
                  if (!restored || restored.ok !== true) throw new Error(restored && restored.error && restored.error.code || '恢复结果未确认');
                  setText(x.fileAuthorityStatus, restored.recovery && restored.recovery.state === 'restored'
                    ? restored.recovery.index_verified === true ? '对象已恢复；原厂索引已核对。' : '对象已恢复；原厂索引尚未确认，请检查原厂文件列表。'
                    : '恢复结果尚未确认，请重新检查保留对象。');
                  button.dataset.completed = 'true'; button.disabled = true;
                } catch (error) { fail(error); } finally { pending(false); }
              };
              x.fileRecoveries.append(item);
            }
            setText(x.fileAuthorityStatus, result.recoveries && result.recoveries.length ? '已读取此根的保留记录；恢复只使用记录中的固定原路径。' : '此根没有保留记录。');
          } else await refresh();
        } catch (error) { fail(error); } finally { pending(false); }
      };
      row.querySelector('[data-authority-preflight]').onclick = () => perform('preflight');
      init.onclick = () => perform('initialize'); inspect.onclick = () => perform('inspect'); revoke.onclick = () => perform('revoke'); x.fileAuthorityRoots.append(row);
    }
    for (const registration of entries.filter(legacy)) {
      const oldBoundary = registration.status === 'legacy_boundary' || registration.reason === 'legacy_users_boundary_v2';
      const row = mount('<article class="r1-api-audit r1-api-authority" data-legacy-authority><strong></strong><p class="sub" data-legacy-description></p><p class="sub mono" data-legacy-root></p><div class="r1-api-actions"><button class="btn" type="button" disabled>初始化恢复区</button><button class="btn" type="button" disabled>检查保留对象</button><button class="btn" type="button" disabled>恢复保留对象</button><button class="btn" type="button" disabled>停止新变更</button></div></article>');
      row.dataset.legacyKind = oldBoundary ? 'boundary' : 'layout';
      setText(row.querySelector('strong'), oldBoundary ? '旧 Users 边界恢复登记' : '旧恢复区位置登记');
      setText(row.querySelector('[data-legacy-description]'), `${oldBoundary ? '旧 Users 边界登记' : '旧 admin 根仍使用 Users/.r1-toolbox-ops 恢复区'}，需先使用原镜像处理保管对象。本版不会迁移、删除、接管或恢复旧位置，也不会自动转换为 Users/admin/.r1-toolbox-ops。`);
      setText(row.querySelector('[data-legacy-root]'), String(registration.root_id || '未知根') + '；修订：' + String(registration.revision ?? '--'));
      x.fileAuthorityRoots.append(row);
    }
  };
  const draw = () => {
    const labels = names();
    x.content.hidden = false; r1SyncMcpService(state.enabled);
    setText(x.status, state.storage_ok === true ? 'Token 仅在创建或轮换时显示。关闭服务后，所有 MCP 与 REST 密钥立即停止访问；已有密钥保留。' : '访问配置存储不可用，暂不能启用服务或管理密钥。');
    setText(x.serviceStatus, state.enabled === true ? '已开启' : '已关闭');
    x.mcp.value = location.origin + '/mcp'; x.restStatus.value = location.origin + '/api/integration/status'; x.restAction.value = location.origin + '/api/integration/action'; x.restCapabilities.value = location.origin + '/api/integration/capabilities';
    const chosenRole = x.role.value || 'readonly';
    x.role.replaceChildren();
    if (editing && (!editing.role || editing.role === 'legacy')) {
      const option = document.createElement('option'); option.value = 'legacy'; option.textContent = '保留原有精确权限'; x.role.append(option);
    }
    for (const role of roles()) { const option = document.createElement('option'); option.value = role.id; option.textContent = String(role.label || roleLabels[role.id]); x.role.append(option); }
    x.role.value = chosenRole === 'legacy' && editing ? 'legacy' : roles().some(role => role.id === chosenRole) ? chosenRole : 'readonly';
    const chosenScopes = x.scopes.children.length ? new Set([...x.scopes.querySelectorAll('input:checked')].map(input => input.value)) : new Set(['read']);
    x.scopes.replaceChildren();
    for (const scope of Array.isArray(state.scope_catalog) ? state.scope_catalog : []) {
      if (!allowedScopes.includes(scope.id)) continue;
      const label = mount(`<label class="r1-api-scope"><input type="checkbox"><span><b></b><span class="sub" data-scope-description></span><span class="sub" data-scope-note></span></span></label>`);
      const input = label.querySelector('input'); input.value = scope.id; input.checked = chosenScopes.has(scope.id);
      label.querySelector('b').textContent = String(scope.label || scope.id);
      label.querySelector('[data-scope-description]').textContent = String(scope.description || '');
      input.onchange = () => {
        dirty.scopes = true;
        if (scope.id === 'files') dirty.files = true;
        if (scope.id === 'docker') { dirty.projects = true; dirty.deployments = true; }
        if (!input.checked && scope.id === 'files') { dirty.files = true; x.fileRootOptions.querySelectorAll('[data-root-check]').forEach(check => { check.checked = false; }); }
        if (!input.checked && scope.id === 'docker') { dirty.projects = true; dirty.deployments = true; x.dockerProjectOptions.querySelectorAll('input').forEach(check => { check.checked = false; }); x.dockerDeploymentOptions.querySelectorAll('input').forEach(check => { check.checked = false; }); }
        updatePermissions();
      };
      x.scopes.append(label);
    }
    const keys = Array.isArray(state.keys) ? state.keys : [];
    setText(x.keySummary, `${keys.length} 个 Token；${keys.filter(key => key.status === 'active').length} 个状态有效。服务关闭时所有 Token 均不能访问。`);
    x.keys.replaceChildren();
    if (!keys.length) x.keys.append(mount('<p class="sub">尚未创建访问密钥。</p>'));
    for (const key of keys) {
      const item = mount(`<article class="r1-api-item"><div class="r1-api-head"><strong data-ref="name"></strong><span class="pill" data-ref="status"></span></div><dl class="r1-api-meta"><dt>操作角色</dt><dd data-ref="role"></dd><dt>功能权限</dt><dd data-ref="scopes"></dd><dt>具体工具</dt><dd data-ref="tools"></dd><dt>文件范围</dt><dd data-ref="files"></dd><dt>Docker 项目</dt><dd data-ref="projects"></dd><dt>未来部署资源</dt><dd data-ref="deployments"></dd><dt>允许来源</dt><dd data-ref="ips"></dd><dt>创建时间</dt><dd data-ref="created"></dd><dt>到期时间</dt><dd data-ref="expires"></dd><dt>最后使用</dt><dd data-ref="used"></dd></dl><div class="r1-api-actions"><button class="btn" type="button" data-ref="edit">编辑权限</button><button class="btn" type="button" data-ref="toggle"></button><button class="btn" type="button" data-ref="rotate">轮换 Token</button><button class="btn danger" type="button" data-ref="revoke">撤销密钥</button></div></article>`);
      const k = collect(item);
      setText(k.name, String(key.name || '未命名密钥')); setText(k.status, ({ active: '有效', disabled: '已停用', expired: '已到期', revoked: '已撤销' })[key.status] || '状态未知');
      setText(k.role, !key.role || key.role === 'legacy' ? '原有精确权限' : (roles().find(role => role.id === key.role) || {}).label || roleLabels[key.role] || '角色未知');
      setText(k.scopes, (Array.isArray(key.scopes) ? key.scopes : []).map(id => scopeName(id, labels)).join('、') || '未授予权限');
      setText(k.tools, key.policy_version === 3 ? (Array.isArray(key.tool_grants) ? key.tool_grants : []).map(name => (currentTools().find(tool => tool.name === name) || {}).title || name).join('、') || '未选择具体工具' : '原有十工具按原角色和功能授权；新增工具未启用。');
      setText(k.projects, (Array.isArray(key.docker_projects) ? key.docker_projects : []).map(grant => `${(projectRows().find(project => project.id === grant.id) || {}).label || grant.id}（${(grant.operations || []).join('、')}）`).join('；') || '未授予项目范围');
      setText(k.deployments, (Array.isArray(key.docker_deployments) ? key.docker_deployments : []).map(grant => `${(deploymentRows().find(resource => resource.id === grant.id) || {}).label || grant.id}（${(grant.operations || []).join('、')}）`).join('；') || '未授予未来部署资源');
      const applications = document.createElement('dd');
      setText(applications, (key.application_ids || []).map(id => (applicationRows().find(app => app.application_id === id) || {}).label || String(id)).join('、') || '未授予应用新装资源');
      k.deployments.after(mount('<dt>允许新装应用</dt>'), applications);
      const grants = Array.isArray(key.file_roots) ? key.file_roots : [];
      if (!grants.length) setText(k.files, '未授予文件范围');
      else k.files.append(mount('<p class="sub">当前边界仅限已核验的 admin 专用 v3 根；旧 Users 根失效，须明确重选，原路径不会自动转换。具体操作以已授权的接入工具为准。</p>'));
      for (const grant of grants) {
        const root = fileRoots().find(item => item.id === grant.id), row = document.createElement('p');
        setText(row, `${root ? root.label : '原文件范围已失效，需重选 admin 根（' + String(grant.id || '未知') + '）'} · ${grant.path ? '子目录：' + String(grant.path) : '整个已选根'} · ${({ readonly: '只读', readwrite: '读写' })[grant.access] || '权限未知'}`);
        k.files.append(row);
      }
      setText(k.ips, Array.isArray(key.allowed_ips) && key.allowed_ips.length ? key.allowed_ips.join('、') : '持密钥的局域网客户端');
      setText(k.created, r1ApiTime(key.created_at)); setText(k.expires, key.expires_at === null ? '长期' : r1ApiTime(key.expires_at)); setText(k.used, r1ApiTime(key.last_used_at));
      const disabled = key.disabled === true || key.enabled === false || key.status === 'disabled';
      setText(k.toggle, disabled ? '启用' : '停用');
      for (const button of [k.edit, k.toggle, k.rotate, k.revoke]) button.hidden = key.status === 'revoked';
      k.edit.onclick = () => { if (!busy) beginEdit(key); };
      k.toggle.onclick = () => { if (!busy) mutate('/api/access/keys/enable', { id: key.id, enabled: disabled }); };
      k.rotate.onclick = async () => {
        if (busy || !(await R1.ask('轮换访问 Token？', `「${esc(key.name || '未命名密钥')}」的旧 Token 将立即失效，新 Token 只显示一次；客户端需要更新配置。权限、停用状态和到期时间保持不变。`, true))) return;
        await mutate('/api/access/keys/rotate', { id: key.id });
      };
      k.revoke.onclick = async () => {
        if (busy || !(await R1.ask('撤销访问密钥？', `撤销「${esc(key.name || '未命名密钥')}」后，使用此密钥的客户端立即失去访问权限。`, true))) return;
        await mutate('/api/access/keys/revoke', { id: key.id });
      };
      x.keys.append(item);
    }
    x.capabilities.replaceChildren();
    for (const tool of currentTools()) {
      const row = mount('<article class="r1-api-capability"><strong></strong><span class="mono" data-capability-name></span><span class="sub" data-capability-role></span><p class="sub" data-capability-description></p></article>');
      setText(row.querySelector('strong'), tool.title || tool.name);
      setText(row.querySelector('[data-capability-name]'), tool.name);
      setText(row.querySelector('[data-capability-role]'), `${names().get(tool.scope) || tool.scope} · 最低角色：${roleLabels[tool.min_role]}`);
      setText(row.querySelector('[data-capability-description]'), tool.description || '调用仍须满足当前 Token 的具体工具与资源授权。');
      x.capabilities.append(row);
    }
    if (!x.capabilities.children.length) x.capabilities.append(mount('<p class="sub">暂未获得可调用能力，请刷新重试。</p>'));
    x.audit.replaceChildren();
    const audit = Array.isArray(state.audit) ? state.audit : [];
    if (!audit.length) x.audit.append(mount('<p class="sub">尚无调用记录。</p>'));
    for (const record of audit.slice(0, 100)) {
      const item = mount('<article class="r1-api-audit"><span data-ref="time"></span><strong data-ref="action"></strong><span data-ref="source"></span><span data-ref="result"></span></article>');
      const a = collect(item);
      setText(a.time, r1ApiTime(record.timestamp ?? record.created_at)); setText(a.action, String(record.operation || record.tool || record.action || record.scope || '功能未知'));
      setText(a.source, [record.key_name || record.key_id, record.peer || record.source_ip || record.ip].filter(Boolean).join(' · ') || '来源未知');
      setText(a.result, String(record.outcome || record.result || record.status || '结果未知'));
      x.audit.append(item);
    }
    drawFileRoots(); drawTools(); drawProjects(); drawDeployments(); drawApplications(); drawFileAuthorities(); drawDeploymentRegistry(); updatePermissions();
  };
  const endEdit = () => {
    editing = null; dirty = { role: false, scopes: false, tools: false, files: false, projects: false, deployments: false, applications: false };
    x.upgradePolicy.checked = false; x.upgradePolicyLabel.hidden = true;
    setText(x.formTitle, '创建访问密钥'); setText(x.create, '创建密钥');
    x.cancelEdit.hidden = true; x.keepExpiry.hidden = true; x.expiry.value = '90';
    x.name.value = ''; x.ips.value = ''; x.role.value = 'readonly';
    x.scopes.querySelectorAll('input').forEach(input => { input.checked = input.value === 'read'; });
    x.fileRootOptions.querySelectorAll('[data-root-check]').forEach(input => { input.checked = false; });
    drawFileRoots([]);
    drawTools(new Set(['get_nas_status'])); drawProjects(new Set()); drawDeployments(new Set()); drawApplications(new Set());
    if (state) draw();
  };
  const beginEdit = key => {
    editing = key; x.upgradePolicy.checked = false; x.upgradePolicyLabel.hidden = key.policy_version === 3; draw();
    showPage('create');
    setText(x.formTitle, '编辑访问密钥'); setText(x.create, '保存修改');
    x.cancelEdit.hidden = false; x.keepExpiry.hidden = false; x.expiry.value = 'keep';
    x.name.value = String(key.name || ''); x.ips.value = Array.isArray(key.allowed_ips) ? key.allowed_ips.join('\n') : '';
    x.role.value = !key.role || key.role === 'legacy' ? 'legacy' : key.role;
    const chosen = new Set(Array.isArray(key.scopes) ? key.scopes : []);
    x.scopes.querySelectorAll('input').forEach(input => { input.checked = chosen.has(input.value); });
    drawFileRoots(Array.isArray(key.file_roots) ? key.file_roots : []);
    const keyLevel = x.role.value === 'legacy' ? Infinity : roleLevel[x.role.value];
    drawTools(new Set(key.policy_version === 3 ? key.tool_grants || [] : currentTools().filter(tool => legacyTools.has(tool.name) && chosen.has(tool.scope) && keyLevel >= roleLevel[tool.min_role]).map(tool => tool.name)));
    drawProjects(new Set((key.docker_projects || []).map(project => project.id)));
    drawDeployments(new Set((key.docker_deployments || []).map(resource => resource.id)));
    drawApplications(new Set(key.application_ids || []));
    updatePermissions(); dirty = { role: false, scopes: false, tools: false, files: false, projects: false, deployments: false, applications: false };
    x.formTitle.scrollIntoView({ block: 'start', behavior: 'smooth' }); x.name.focus();
  };
  const refresh = async () => {
    const result = await API.get('/api/access');
    if (!result || result.ok !== true) throw new Error((result && result.error) || '未读取到访问配置');
    if (closed) return;
    state = result; draw();
  };
  const mutate = async (url, body) => {
    if (busy) return null;
    pending(true); x.error.hidden = true;
    try {
      const result = await R1.post(url, body);
      if (!result || result.ok !== true) throw new Error((result && result.error) || '操作未完成');
      if (url === '/api/access/keys' || url === '/api/access/keys/rotate') {
        if (!closed) {
          r1ShowApiSecret(result.token);
          if (url === '/api/access/keys') { x.name.value = ''; x.ips.value = ''; }
        }
        result.token = '';
      }
      if (url === '/api/access/keys/update' || (url === '/api/access/keys/revoke' && editing && editing.id === body.id)) endEdit();
      await refresh();
      if (!closed && ['/api/access/keys', '/api/access/keys/update'].includes(url)) showPage('manage');
      return result;
    } catch (error) { fail(error); return null; }
    finally { if (!closed) pending(false); }
  };
  const projectError = value => typeof (value && value.error) === 'string' ? value.error
    : value && value.error && typeof value.error.message === 'string' ? value.error.message : '项目登记读取失败，请重试。';
  const deploymentError = value => typeof (value && value.error) === 'string' ? value.error
    : value && value.error && typeof value.error.message === 'string' ? value.error.message : '部署资源操作未完成，请重试。';
  const deploymentMutation = async (path, body) => {
    if (closed || deploymentsBusy || busy) return;
    deploymentsBusy = true; pending(busy); setText(x.deploymentStatus, '正在保存部署资源登记…');
    try {
      const result = await R1.post(path, body);
      if (!result || result.ok !== true) throw new Error(deploymentError(result));
      await refresh();
      if (!closed) setText(x.deploymentStatus, path.endsWith('/refresh') ? '已重新核验生命周期，旧计划已失效；Token 权限保持不变。'
        : path.endsWith('/revoke') ? '已撤销登记；已有容器和卷保留，后续操作会检查授权。' : '已登记固定资源，未拉取镜像或创建容器。现在可在添加／编辑 Token 中明确授权。');
    } catch (error) { if (!closed) setText(x.deploymentStatus, error && error.body ? deploymentError(error.body) : String(error && error.message || '部署资源操作未完成。')); }
    finally { deploymentsBusy = false; if (!closed) pending(busy); }
  };
  const drawDeploymentRegistry = () => {
    x.registeredDeployments.replaceChildren();
    for (const resource of deploymentRows()) {
      const item = mount('<article class="r1-api-project"><strong></strong><p class="sub mono" data-deployment-name></p><p class="sub" data-deployment-state></p><div class="r1-api-actions"><button class="btn" type="button" data-refresh-deployment>重新核验</button><button class="btn danger" type="button" data-revoke-deployment>撤销登记</button></div></article>');
      setText(item.querySelector('strong'), resource.label);
      setText(item.querySelector('[data-deployment-name]'), [resource.name, resource.image_repository].filter(value => typeof value === 'string').join(' · '));
      setText(item.querySelector('[data-deployment-state]'), resource.revoked ? '登记已撤销；已有容器和卷保留。'
        : resource.deployed ? '已有记录容器；此处不证明服务正在运行，实际状态以执行任务核对为准。'
          : '已登记未来资源；尚未记录部署容器。调用前仍会核验实际资源。');
      for (const button of item.querySelectorAll('button')) { button.dataset.revoked = String(resource.revoked === true); button.disabled = resource.revoked === true; }
      item.querySelector('[data-refresh-deployment]').onclick = async () => {
        if (busy || deploymentsBusy || !(await R1.ask('重新核验部署资源？', `核对「${esc(resource.label)}」原有固定资源和既有容器。更新重启后的生命周期证据，使旧计划失效；不扩大 Token 权限，不启动或重建容器。`))) return;
        await deploymentMutation('/api/docker/deployments/refresh', { deployment_id: resource.id, confirm: true });
      };
      item.querySelector('[data-revoke-deployment]').onclick = async () => {
        if (busy || deploymentsBusy || !(await R1.ask('撤销部署资源登记？', `「${esc(resource.label)}」将停止接受后续授权操作。已有容器和具名卷保留，不停止或删除服务。`, true))) return;
        await deploymentMutation('/api/docker/deployments/revoke', { deployment_id: resource.id });
      };
      x.registeredDeployments.append(item);
    }
    if (!x.registeredDeployments.children.length) x.registeredDeployments.append(mount('<p class="sub">尚未登记未来部署资源。</p>'));
  };
  const volumeOptions = (select, previous = '') => {
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择已有具名卷'; select.append(empty);
    for (const volume of deploymentCandidates ? deploymentCandidates.volumes : []) {
      const option = document.createElement('option'); option.value = volume.name; option.textContent = volume.name; select.append(option);
    }
    select.value = previous;
  };
  x.loadDeployments.onclick = async () => {
    if (closed || deploymentsBusy || busy) return;
    deploymentsBusy = true; deploymentCandidates = null; pending(busy); setText(x.deploymentStatus, '正在显式读取本机卷、网络与登记元数据…');
    try {
      const [catalog, registered] = await Promise.all([API.req('/api/docker/deployments/catalog', { method: 'GET' }), API.req('/api/docker/deployments', { method: 'GET' })]);
      if (closed) return;
      if (!catalog || catalog.ok !== true || !Array.isArray(catalog.volumes) || !Array.isArray(catalog.networks)) throw new Error(deploymentError(catalog));
      if (!registered || registered.ok !== true || !Array.isArray(registered.deployments)) throw new Error(deploymentError(registered));
      const validName = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value);
      deploymentCandidates = { volumes: catalog.volumes.filter(row => row && validName(row.name) && row.name !== 'r1-toolbox-data').slice(0, 128),
        networks: catalog.networks.filter(row => row && validName(row.name) && !['host', 'container'].includes(row.name)).slice(0, 128) };
      const selected = x.deploymentNetwork.value; x.deploymentNetwork.replaceChildren();
      const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择已有网络'; x.deploymentNetwork.append(empty);
      for (const network of deploymentCandidates.networks) { const option = document.createElement('option'); option.value = network.name; option.textContent = network.name; x.deploymentNetwork.append(option); }
      x.deploymentNetwork.value = selected;
      for (const select of x.deploymentVolumes.querySelectorAll('[data-volume-name]')) volumeOptions(select, select.value);
      state.docker_deployment_catalog = registered; drawDeployments(); drawDeploymentRegistry(); updatePermissions();
      setText(x.deploymentStatus, `已读取 ${deploymentCandidates.volumes.length} 个可用卷、${deploymentCandidates.networks.length} 个网络候选。登记时会重新核验，不会创建资源或容器。`);
    } catch (error) { if (!closed) setText(x.deploymentStatus, error && error.body ? deploymentError(error.body) : String(error && error.message || '本机资源读取失败，请重试。')); }
    finally { deploymentsBusy = false; if (!closed) pending(busy); }
  };
  x.addDeploymentVolume.onclick = () => {
    if (busy || deploymentsBusy || !deploymentCandidates || x.deploymentVolumes.children.length >= 8) return;
    const row = mount('<div class="r1-api-file-range" data-deployment-volume><label class="r1-api-input">已有具名卷<select class="textfield" data-volume-name required></select></label><label class="r1-api-input">容器内目录<input class="textfield mono" data-volume-target placeholder="/data" maxlength="256" required></label><label class="r1-api-scope"><input type="checkbox" data-volume-readonly checked><span>只读挂载</span></label><button class="btn" type="button" data-remove-volume>移除此卷</button></div>');
    volumeOptions(row.querySelector('[data-volume-name]'));
    row.querySelector('[data-remove-volume]').onclick = () => row.remove(); x.deploymentVolumes.append(row);
  };
  x.addDeploymentPort.onclick = () => {
    if (busy || deploymentsBusy || x.deploymentPorts.children.length >= 16) return;
    const row = mount('<div class="r1-api-file-range" data-deployment-port><label class="r1-api-input">宿主具体 IP<input class="textfield mono" data-port-ip value="127.0.0.1" maxlength="64" required></label><label class="r1-api-input">宿主端口<input class="textfield" type="number" data-port-published min="1" max="65535" step="1" required></label><label class="r1-api-input">容器端口<input class="textfield" type="number" data-port-target min="1" max="65535" step="1" required></label><label class="r1-api-input">协议<select class="textfield" data-port-protocol><option value="tcp">TCP</option><option value="udp">UDP</option></select></label><button class="btn" type="button" data-remove-port>移除此端口</button></div>');
    row.querySelector('[data-remove-port]').onclick = () => row.remove(); x.deploymentPorts.append(row);
  };
  x.deploymentForm.onsubmit = async event => {
    event.preventDefault(); if (busy || deploymentsBusy || !deploymentCandidates) return;
    const spec = { name: x.deploymentName.value.trim(), image_repository: x.deploymentRepository.value.trim(),
      network: x.deploymentNetwork.value, volumes: [], ports: [], user: x.deploymentUser.value.trim(),
      memory_bytes: Number(x.deploymentMemory.value) * 1024 * 1024, nano_cpus: Math.round(Number(x.deploymentCpu.value) * 1e9) };
    const label = x.deploymentLabel.value.trim();
    for (const row of x.deploymentVolumes.querySelectorAll('[data-deployment-volume]')) spec.volumes.push({ name: row.querySelector('[data-volume-name]').value, target: row.querySelector('[data-volume-target]').value.trim(), readonly: row.querySelector('[data-volume-readonly]').checked });
    for (const row of x.deploymentPorts.querySelectorAll('[data-deployment-port]')) spec.ports.push({ host_ip: row.querySelector('[data-port-ip]').value.trim(), published: Number(row.querySelector('[data-port-published]').value), target: Number(row.querySelector('[data-port-target]').value), protocol: row.querySelector('[data-port-protocol]').value });
    if (!label || !spec.network || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(spec.name)
        || /[@\s]/.test(spec.image_repository) || !spec.image_repository || spec.image_repository.split('/').at(-1).includes(':')
        || !/^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$/.test(spec.user)
        || !Number.isInteger(spec.memory_bytes) || !Number.isInteger(spec.nano_cpus)) { setText(x.deploymentStatus, '请填写固定名称、无 tag 的镜像仓库、已有网络及有效的非 root 用户和资源上限。'); return; }
    if (spec.network === 'none' && spec.ports.length) { setText(x.deploymentStatus, 'none 网络不能发布端口，请移除端口或选择已核验的 bridge 网络。'); return; }
    const volumeNote = spec.volumes.map(row => `${row.name} → ${row.target}（${row.readonly ? '只读' : '读写'}）`).join('；') || '无卷';
    const portNote = spec.ports.map(row => `${row.host_ip}:${row.published} → ${row.target}/${row.protocol}`).join('；') || '无发布端口';
    if (!(await R1.ask('登记未来部署资源？', `标签：${esc(label)}；服务：${esc(spec.name)}；允许仓库：${esc(spec.image_repository)}；网络：${esc(spec.network)}；用户：${esc(spec.user)}；内存：${spec.memory_bytes / 1048576} MiB；CPU：${spec.nano_cpus / 1e9} 核。卷：${esc(volumeNote)}。端口：${esc(portNote)}。固定只读根文件系统与最低权限；这里只保存授权合同，不部署容器。`))) return;
    await deploymentMutation('/api/docker/deployments/register', { label, spec });
  };
  const projectMutation = async (path, body) => {
    if (closed || projectsBusy || busy) return;
    projectsBusy = true; x.loadProjects.disabled = true;
    try {
      const result = await R1.post(path, body);
      if (!result || result.ok !== true) throw new Error(projectError(result));
      await refresh();
      projectsBusy = false;
      await loadProjects();
    } catch (error) { if (!closed) setText(x.projectStatus, String(error && error.message || '项目登记操作失败。')); }
    finally { projectsBusy = false; if (!closed) x.loadProjects.disabled = false; }
  };
  const loadProjects = async () => {
    if (closed || projectsBusy || busy) return;
    projectsBusy = true; x.loadProjects.disabled = true;
    setText(x.projectStatus, '正在读取固定本机项目元数据…');
    x.projectCandidates.replaceChildren(); x.registeredProjects.replaceChildren();
    try {
      const [candidates, registered] = await Promise.all([API.get('/api/docker/projects/catalog'), API.get('/api/docker/projects')]);
      if (closed) return;
      if (!registered || registered.ok !== true || !Array.isArray(registered.projects)) throw new Error(projectError(registered));
      state.docker_project_catalog = registered; drawProjects(); updatePermissions();
      setText(x.projectStatus, candidates && candidates.ok === true ? '候选只定位项目；登记时后端重新核验身份，仅保存 inspect 读取登记。' : projectError(candidates));
      for (const candidate of candidates && Array.isArray(candidates.candidates) ? candidates.candidates.slice(0, 128) : []) {
        if (!candidate || !/^candidate_[0-9a-f]{24}$/.test(candidate.id)) continue;
        const item = mount('<article class="r1-api-project"><strong></strong><label class="r1-api-input">登记标签<input class="textfield" maxlength="128" data-project-label autocomplete="off"></label><p class="sub" data-project-reason></p><button class="btn" type="button" data-register>登记读取项目</button></article>');
        setText(item.querySelector('strong'), candidate.label || '未命名候选');
        item.querySelector('[data-project-label]').value = String(candidate.label || '');
        setText(item.querySelector('[data-project-reason]'), candidate.available === true ? '仅登记元数据，不启动或更新容器。' : candidate.reason || '候选身份当前不可用。');
        const button = item.querySelector('[data-register]'); button.disabled = candidate.available !== true;
        button.onclick = async () => {
          const label = item.querySelector('[data-project-label]').value.trim();
          if (!label) { setText(x.projectStatus, '请填写项目登记标签。'); return; }
          if (!(await R1.ask('登记项目读取范围？', `将「${esc(label)}」加入可授权项目，不部署、更新或启停 Docker。`))) return;
          await projectMutation('/api/docker/projects/register', { candidate_id: candidate.id, label });
        };
        x.projectCandidates.append(item);
      }
      for (const project of projectRows()) {
        const item = mount('<article class="r1-api-project"><strong></strong><p class="sub" data-project-reason></p><label class="r1-api-input">登记标签<input class="textfield" maxlength="128" data-project-label autocomplete="off"></label><div class="r1-api-actions"><button class="btn" type="button" data-update>保存标签</button><button class="btn danger" type="button" data-revoke>撤销登记</button></div></article>');
        setText(item.querySelector('strong'), project.label);
        setText(item.querySelector('[data-project-reason]'), project.revoked ? '已撤销登记；原 Docker 项目不受影响。' : project.available ? '已登记 inspect：仅可授权读取。' : project.reason || '当前绑定不可用。');
        item.querySelector('[data-project-label]').value = project.label;
        item.querySelector('[data-update]').disabled = project.revoked === true;
        item.querySelector('[data-revoke]').disabled = project.revoked === true;
        item.querySelector('[data-update]').onclick = async () => {
          const label = item.querySelector('[data-project-label]').value.trim();
          if (!label) { setText(x.projectStatus, '请填写项目登记标签。'); return; }
          if (!(await R1.ask('保存项目标签？', '只更新 Toolbox 登记标签，项目身份和原 Docker 设置保持不变。'))) return;
          await projectMutation('/api/docker/projects/update', { project_id: project.id, label });
        };
        item.querySelector('[data-revoke]').onclick = async () => {
          if (!(await R1.ask('撤销项目登记？', `「${esc(project.label)}」不再作为可授权项目；不会停止或删除原 Docker 项目。`, true))) return;
          await projectMutation('/api/docker/projects/revoke', { project_id: project.id });
        };
        x.registeredProjects.append(item);
      }
      if (!x.projectCandidates.children.length) x.projectCandidates.append(mount('<p class="sub">暂无可登记的安全候选，不会按名称猜测项目。</p>'));
      if (!x.registeredProjects.children.length) x.registeredProjects.append(mount('<p class="sub">尚未登记项目。</p>'));
    } catch (error) { if (!closed) setText(x.projectStatus, String(error && error.message || '项目登记读取失败。')); }
    finally { projectsBusy = false; if (!closed) x.loadProjects.disabled = false; }
  };
  x.loadProjects.onclick = loadProjects;
  // Definitions are owner metadata, loaded separately from callable tools and keys.
  x.loadContracts.onclick = async () => {
    if (closed || contractsBusy) return;
    contractsBusy = true; x.loadContracts.disabled = true;
    x.contractList.replaceChildren(); x.contractDefinition.value = '';
    x.contractJson.hidden = true; x.copyContracts.hidden = true;
    setText(x.contractStatus, '正在读取接口定义…');
    try {
      const result = await API.get('/api/access/contracts');
      if (closed) return;
      if (!result || result.ok !== true) throw new Error(typeof (result && result.error) === 'string' ? result.error : '接口定义读取失败，请重试。');
      if (result.schema_version !== 1 || result.status !== 'definitions_only'
          || !Array.isArray(result.tools) || !result.tools.length || result.tools.length > 64
          || !result.tools.every(tool => tool && typeof tool.implemented === 'boolean' && Object.hasOwn(roleLevel, tool.min_role)
            && ['files', 'docker'].includes(tool.scope) && typeof tool.name === 'string'
            && tool.inputSchema && tool.outputSchema)) throw new Error('接口定义暂不可用，请确认服务版本。');
      for (const tool of result.tools) {
        const row = mount('<article class="r1-api-audit r1-api-contract"><strong></strong><span class="mono"></span><span data-contract-role></span></article>');
        setText(row.querySelector('strong'), String(tool.title || tool.name));
        setText(row.querySelector('.mono'), tool.name);
        setText(row.querySelector('[data-contract-role]'), `最低角色：${roleLabels[tool.min_role]}；功能：${tool.scope === 'files' ? '文件管理' : 'Docker 管理'}；${tool.implemented ? '已接入，仍需明确授权。' : '待接入。'}`);
        x.contractList.append(row);
      }
      x.contractDefinition.value = JSON.stringify(result, null, 2);
      x.contractJson.hidden = false; x.copyContracts.hidden = false;
      setText(x.contractStatus, `已读取 ${result.tools.length} 项接口定义，已接入 ${result.tools.filter(tool => tool.implemented).length} 项；其余待接入，定义本身不授予执行权限。`);
    } catch (error) { if (!closed) setText(x.contractStatus, String(error && error.message || '接口定义读取失败，请重试。')); }
    finally { contractsBusy = false; if (!closed) x.loadContracts.disabled = false; }
  };
  x.copyContracts.onclick = () => r1ApiCopy(x.contractDefinition, x.contractStatus);
  x.form.onsubmit = async event => {
    event.preventDefault(); if (busy) return;
    const name = x.name.value.trim(), role = x.role.value, scopes = [...x.scopes.querySelectorAll('input:checked')].map(input => input.value);
    let allowed_ips;
    try { allowed_ips = r1ApiRules(x.ips.value); } catch (error) { fail(error); return; }
    const changePolicy = !editing || policyDirty();
    const upgrade = !!editing && editing.policy_version !== 3 && x.upgradePolicy.checked;
    if (!name || (changePolicy && !scopes.length)) { fail(new Error('请填写名称，并选择至少一项功能权限。')); return; }
    if (changePolicy && !Object.hasOwn(roleLevel, role)) { fail(new Error('请选择有效的操作角色。')); return; }
    const changeFiles = !editing || dirty.files;
    const file_roots = changeFiles ? [] : (Array.isArray(editing.file_roots) ? editing.file_roots : []);
    if (changeFiles && scopes.includes('files')) {
      if (!fileRoots().length) { fail(new Error('没有已确认的存储根，不能创建文件权限。')); return; }
      const identities = new Set();
      for (const item of x.fileRootOptions.querySelectorAll('.r1-api-file-root')) {
        if (!item.querySelector('[data-root-check]').checked) continue;
        if (item.dataset.available === 'false') { fail(new Error('原文件范围当前失效，请明确取消失效范围或保持原文件配置。')); return; }
        for (const range of item.querySelectorAll('[data-root-range]')) {
          const path = range.querySelector('[data-root-path]').value, access = range.querySelector('[data-root-access]').value;
          if (!validSubpath(path)) { fail(new Error('文件子目录必须是相对 POSIX 路径，不接受绝对路径、空路径段、反斜杠、. 或 ..，且不超过 1024 个字符和 4096 个 UTF-8 字节；任意层级的 .r1-toolbox-ops 及其后代仅由所有者恢复界面管理。')); return; }
          if (!['readonly', 'readwrite'].includes(access) || (access === 'readwrite' && (role === 'readonly' || item.dataset.readOnly === 'true'))) { fail(new Error('只读角色或只读挂载不能授予文件读写权限。')); return; }
          const identity = item.dataset.rootId + '\n' + path;
          if (identities.has(identity)) { fail(new Error('同一存储根的相同子目录不能重复授权。')); return; }
          identities.add(identity); file_roots.push({ id: item.dataset.rootId, path, access });
        }
      }
      if (!file_roots.length) { fail(new Error('请至少勾选一个已确认的文件存储根。')); return; }
      if (file_roots.length > 32) { fail(new Error('文件范围最多 32 条。')); return; }
    }
    const changeProjects = !editing || dirty.projects || upgrade;
    const changeDeployments = !editing || dirty.deployments || upgrade;
    const docker_projects = changeProjects ? [] : (Array.isArray(editing.docker_projects) ? editing.docker_projects : []);
    if (exactPolicy() && changeProjects && scopes.includes('docker')) {
      for (const input of x.dockerProjectOptions.querySelectorAll('[data-project-check]:checked')) {
        if (input.dataset.available !== 'true') { fail(new Error('所选项目登记已失效，请明确移除后重新选择已登记项目。')); return; }
        const operations = [...input.closest('[data-project-row]').querySelectorAll('[data-project-operation]:checked')].map(option => option.value);
        if (!operations.length || (role !== 'admin' && operations.some(operation => operation !== 'inspect'))) { fail(new Error('请为项目选择当前角色允许的具体操作。')); return; }
        docker_projects.push({ id: input.value, operations });
      }
    }
    const docker_deployments = changeDeployments ? [] : (Array.isArray(editing.docker_deployments) ? editing.docker_deployments : []);
    if (exactPolicy() && changeDeployments && scopes.includes('docker')) {
      for (const input of x.dockerDeploymentOptions.querySelectorAll('[data-deployment-check]:checked')) {
        if (input.dataset.available !== 'true') { fail(new Error('所选部署资源已失效，请明确移除后重新选择所有者登记的资源。')); return; }
        const operations = [...input.closest('[data-deployment-row]').querySelectorAll('[data-deployment-operation]:checked')].map(option => option.value);
        if (!operations.length || (role !== 'admin' && operations.some(operation => operation !== 'inspect'))) { fail(new Error('请为部署资源选择当前角色允许的具体操作。')); return; }
        docker_deployments.push({ id: input.value, operations });
      }
    }
    if (exactPolicy() && (changeProjects || changeDeployments) && scopes.includes('docker') && !docker_projects.length && !docker_deployments.length) { fail(new Error('请至少选择一个可用的已登记 Docker 项目或未来部署资源。')); return; }
    const tool_grants = [...x.toolOptions.querySelectorAll('input:checked')].map(input => input.value);
    if (exactPolicy()) for (const name of tool_grants) {
      const tool = currentTools().find(item => item.name === name);
      if (!tool || !scopes.includes(tool.scope) || roleLevel[role] < roleLevel[tool.min_role]) { fail(new Error('具体工具不满足当前角色和功能权限，请重新选择。')); return; }
    }
    const labels = names(), grant = scopes.map(id => labels.get(id) || id).join('、');
    const rangeNote = file_roots.length ? `文件范围：${file_roots.map(item => { const root = fileRoots().find(root => root.id === item.id); return esc(root ? root.label : item.id) + '／' + esc(item.path || '整个已选 admin 根') + '（' + (item.access === 'readwrite' ? '读写' : '只读') + '）'; }).join('、')}。边界仅限已核验的 admin 专用 v3 根；旧根失效，须重选后方可使用，不自动转换原路径。具体操作还须有对应工具授权。` : '';
    const body = { name, allowed_ips };
    if (!editing || dirty.role || upgrade) body.role = role;
    if (!editing || dirty.scopes || upgrade) body.scopes = scopes;
    if (changeFiles) body.file_roots = scopes.includes('files') ? file_roots : [];
    if (exactPolicy() && (!editing || dirty.tools || upgrade)) body.tool_grants = tool_grants;
    if (exactPolicy() && changeProjects) body.docker_projects = scopes.includes('docker') ? docker_projects : [];
    if (exactPolicy() && changeDeployments) body.docker_deployments = scopes.includes('docker') ? docker_deployments : [];
    if (exactPolicy() && (!editing || dirty.applications || upgrade)) body.application_ids = scopes.includes('apps') && role === 'admin'
      ? [...x.applicationInstallOptions.querySelectorAll('input:checked')].map(input => Number(input.value)) : [];
    const applicationNote = Array.isArray(body.application_ids) && body.application_ids.length
      ? `允许新装应用：${body.application_ids.map(id => esc((applicationRows().find(app => app.application_id === id) || {}).label || String(id))).join('、')}。原生应用安装影响不受文件工具的 admin 范围授权限制，执行前还须预览实际影响。` : '';
    if (!editing || x.expiry.value !== 'keep') body.expires_days = Number(x.expiry.value);
    if (editing) {
      body.id = editing.id;
      const note = changePolicy ? `角色：${roleLabels[role]}；功能：${esc(grant)}。${rangeNote}${applicationNote}${exactPolicy() ? '具体工具：' + esc(tool_grants.join('、') || '未授予') + '。' : '旧策略保持原有十工具范围。'}` : '原有角色、功能、具体工具、文件与项目范围保持不变。';
      if (!(await R1.ask('保存访问密钥修改？', `保存「${esc(name)}」的设置。${note}${x.expiry.value === 'keep' ? '到期时间保持不变。' : '有效期从本次保存重新计算。'}Token 不会变化。`))) return;
      await mutate('/api/access/keys/update', body);
    } else {
      if (!(await R1.ask('创建访问密钥？', `角色：${roleLabels[role]}。将向「${esc(name)}」授予：${esc(grant)}。具体工具：${esc(tool_grants.join('、') || '未授予')}。${scopes.includes('power') ? '电源权限可安排关机或重启，需要管理员角色。' : ''}${rangeNote}${applicationNote}`))) return;
      await mutate('/api/access/keys', body);
    }
  };
  x.refresh.onclick = async () => {
    if (busy) return;
    pending(true); x.error.hidden = true;
    try { await refresh(); } catch (error) { fail(error); } finally { if (!closed) pending(false); }
  };
  x.role.onchange = () => { dirty.role = true; updatePermissions(); };
  x.upgradePolicy.onchange = () => {
    if (!editing || editing.policy_version === 3) return;
    dirty.tools = x.upgradePolicy.checked;
    if (x.upgradePolicy.checked) {
      if (x.role.value === 'legacy') { x.role.value = 'readonly'; dirty.role = true; }
      const scopes = new Set(selectedScopes());
      drawTools(new Set(currentTools().filter(tool => legacyTools.has(tool.name) && scopes.has(tool.scope) && roleLevel[x.role.value] >= roleLevel[tool.min_role]).map(tool => tool.name)));
    }
    updatePermissions();
  };
  x.cancelEdit.onclick = () => { endEdit(); showPage('manage'); };
  x.close.onclick = close; document.addEventListener('keydown', onKey);
  document.body.append(el); showPage(initialPage); pending(true);
  refresh().catch(fail).finally(() => { if (!closed) pending(false); });
}

function r1ConnSettings(card) {
  let sig = '';
  card.innerHTML = `<div data-ref="rows"></div>`;
  const x = collect(card);
  card._sync = () => {
    const st = R1.st || {}, web = st.web || {}, auth = st.auth || {}, pairing = auth.pairing || {};
    const key = JSON.stringify([web.addresses, web.port, pairing, st.version]);
    if (key === sig) return;
    sig = key;
    const rows = mount(`<div></div>`);
    const passwordMode = web.auth_mode !== 'pair';
    rows.append(r1Field('网页地址', mount(`<span class="mono">${esc((web.addresses && web.addresses[0]) || ('http://NAS_IP:' + (web.port || 18732)))}</span>`),
      passwordMode ? '登录口令模式：写操作只使用可选登录口令，不再生成配对码。' : '配对码在小屏「设置 → 网页配置」生成，180 秒有效、只能使用一次。'));
    if (!passwordMode) {
      rows.append(r1Field('配对状态', mount(`<span>${pairing.active ? '已生成，剩余 ' + Math.max(0, pairing.expires_in || 0) + ' 秒' : '当前没有有效配对码'}</span>`),
        '配对码只在本机小屏显示，不经网页或第三方接口。'));
      const b = mount(`<button class="btn" type="button">输入配对码解锁</button>`);
      b.onclick = () => R1.openPair('修改设备设置需要授权');
      rows.append(r1Field('解锁设置', b, '登录口令开启后可直接登录，无需配对码。'));
    }
    rows.append(r1Field('数据卷', mount(`<span class="mono">r1-toolbox-data</span>`),
      '升级或重建 Compose 时必须复用原数据卷，否则配对会话与服务设置会丢失。'));
    rows.append(r1Field('版本', mount(`<span class="mono">${esc(st.version || '--')}</span>`), '镜像按提交标签发布；更新请复用原项目与数据卷。'));
    x.rows.replaceChildren(rows);
  };
  card._sync();
}

function r1About(card) {
  card.innerHTML = `<div class="about"><div class="brand-mark">${ic('logo')}</div><h4>R1 Toolbox</h4>
    <p>海康 HikSemi R1 NAS 的本地监控与控制面板 · <span data-ref="v">v--</span></p>
    <p>设备名称：<span data-ref="deviceName">--</span> · 主机名：<span data-ref="hostname">--</span></p>
    <p class="faint" style="margin-top:2px">面板框架与视觉来自 fnMonitor（MIT）· 本机采集、鉴权与控制代码独立实现</p>
    <p class="faint">监控读数只来自本机真实来源；没有可信来源的模块会隐藏或标注原因。</p>
    <div style="display:flex;gap:8px;justify-content:center;margin-top:20px;flex-wrap:wrap">
      <a class="btn" href="https://github.com/MisiteQ/fnmonitor" target="_blank" rel="noopener noreferrer">${ic('ext')}fnMonitor（面板框架）</a>
      <button class="btn" type="button" data-sheet="keys">${ic('key')}快捷键</button></div></div>`;
  const x = collect(card);
  card._sync = () => {
    const device = (R1.st && R1.st.device) || {};
    setText(x.v, (S.cfg && S.cfg.version) || (R1.st && R1.st.version) || '--');
    setText(x.deviceName, device.name || '--');
    setText(x.hostname, device.hostname || '--');
  };
  card._sync();
}

SET_SCHEMA.length = 0;
SET_SCHEMA.push(
  { id: 'r1-fan', title: '双路风扇', icon: 'fan', desc: 'CPU 高／中／低为独立固定输出，系统高／中／低为原厂档位；手动直接编辑温控曲线。点击保存立即应用，选择自动恢复原厂管理。', custom: r1FanSettings },
  { id: 'r1-light', title: '机身灯光', icon: 'palette', desc: '手动灯效或按温度连续混色，可设置夜间亮度上限。默认只读探测；保存并确认后接管。', custom: r1LightSettings },
  { id: 'r1-screen', title: '小屏与显示', icon: 'layout', desc: '亮度、息屏与夜间调暗；屏幕能力未验证时不提供操作。', custom: r1ScreenSettings },
  { id: 'r1-monitor', title: '历史与告警', icon: 'pulse', desc: '历史采样、保留天数、流量统计范围与本地告警阈值。', custom: r1MonitorSettings },
  ...(R1_THRESHOLDS ? [R1_THRESHOLDS] : []),
  { id: 'look', title: '外观', icon: 'palette', desc: R1_LOOK.desc, items: R1_LOOK.items },
  { id: 'r1-auth', title: '登录口令', icon: 'lock', desc: '首次默认无需口令，可在此设置；开启后网页监控与管理需要登录。', custom: r1AuthSettings },
  { id: 'r1-services', title: '服务与开发', icon: 'server', desc: '容器与网页持续运行，小屏默认开启可切回官方，MCP 默认关闭；开发诊断必须设置来源白名单和有效时长。', custom: r1ServiceSettings },
  { id: 'r1-media-sort', title: '影视追更排序', icon: 'trend', desc: '可选插件。显式检测自动发现数据库；确认后应用或撤销追更排序规则。', custom: r1MediaSortSettings },
  { id: 'r1-api', title: 'API 管理', icon: 'key', desc: '为局域网 AI 与自动化管理 MCP／REST 密钥、操作角色、功能与文件范围、来源限制和调用记录。', custom: r1ApiSettings },
  { id: 'r1-conn', title: '连接与数据卷', icon: 'server', desc: '网页地址、配对与持久化位置。', custom: r1ConnSettings },
  { id: 'weather', title: '天气', icon: 'cloud', desc: '从 Open-Meteo 读取当前天气；自动定位按 NAS 公网 IP 估算城市，也可填写城市或坐标。需要外网访问。', items: [
    { k: 'weatherAuto', label: '自动定位', type: 'toggle', hint: '按 NAS 公网 IP 估算城市；需要外网。关闭后可手动填写下方位置。',
      get: () => !(S.cfg && S.cfg.weather_city) && !S.weatherManual,
      set: (v, row) => { if (v) { S.weatherManual = false; saveCfg({ weather_city: '' }, '已切换为自动定位天气', row).then(() => { syncSettings(); setTimeout(() => Poll.run('weather'), 800); }); } else { S.weatherManual = true; syncSettings(); setTimeout(() => { const i = $('.set-row[data-k="weather_city"] input'); i && i.focus(); }, 60); } } },
    cfgItem('weather_city', '天气位置', 'text', { placeholder: '城市名（如 徐州）或 纬度,经度', showIf: () => !!(S.cfg && S.cfg.weather_city) || !!S.weatherManual, hint: '城市名或纬度,经度（如 34.26,117.18）；留空则由上方自动定位',
      validate: v => v && v.includes(',') && !/^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$/.test(v) ? '坐标格式应为 纬度,经度（如 34.26,117.18）' : v.length > 100 ? '位置太长了' : '',
      set: (v, row) => saveCfg({ weather_city: v }, '天气位置', row).then(() => setTimeout(() => Poll.run('weather'), 800)) }),
  ] },
  { id: 'about', title: '关于', icon: 'info', custom: r1About },
);

/* R1 模块的默认归属已在 33-state.js 的 LAYOUT_DEFAULT 中定义：
   r1cool / r1light 在温度页，r1screen 在概览页；这里不再修改默认顺序。 */

/* 首次进入面板时读一次 R1 状态 */
Poll.run('r1');

/* 原厂业务：GET 只采集被动来源；签名查询仅由显式刷新触发。 */
// fnOS 的相册统计在 R1 没有来源，由下面的原厂业务模块提供真实数据。
if (MODS.apps) MODS.apps.el.hidden = true;
const R1_BUSINESS = {
  state: null, error: null, pending: null,
  async load(force = false) {
    if (this.pending) {
      if (!force) return this.pending;
      await this.pending;
      return this.load(true);
    }
    const task = (async () => {
      try { this.state = await API.get('/api/business'); this.error = null; }
      catch (error) { this.error = String(error.message || error); }
    })();
    this.pending = task;
    try { return await task; } finally { if (this.pending === task) this.pending = null; }
  },
};
const R1_MEDIA_LABELS = { face: '相册识别', ocr: '文字识别', thumbnail: '缩略图', similar: '相似图片', ai: 'AI 配置', scraper: '影视刮削', shared_album: '共享相册' };
const r1BusinessNumber = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString() : '--';
const r1BusinessTime = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '尚未采集';
function r1MediaSummary(row) {
  if (row.status === 'not_applicable') return '没有可查询的已有共享相册';
  if (!row.available || !row.data) return row.reason || '数据暂不可用';
  const d = row.data, n = r1BusinessNumber;
  if (row.id === 'face') return `图片 ${n(d.processedpic)} / ${n(d.totalpic)}，视频 ${n(d.processedvid)} / ${n(d.totalvid)}`;
  if (row.id === 'ocr') return `已处理 ${n(d.processedpic)} / ${n(d.totalpic)}，剩余 ${n(d.remainpic)}`;
  if (row.id === 'similar') return `待计算哈希 ${n(d.pending_hashes)}；原厂计数不用于推断完成率`;
  if (row.id === 'ai') return `识别范围数量 ${n(d.recognition_range_count)}，配置状态 ${n(d.configuration_status)}`;
  if (row.id === 'scraper') return `scaned / total：${n(d.scaned)} / ${n(d.total)}，num：${n(d.num)}，状态码 ${n(d.native_status)}`;
  if (row.id === 'thumbnail') return ['b', 'st', 'pt', 'sc', 'cc'].map(key => `${key}：${n(d[key])}`).join('，') + '；单位及状态含义待确认';
  return `状态码 ${n(d.native_status)}，扫描 ${n(d.scan_num)}，失败 ${n(d.failed_num)}，等待 ${n(d.await_num)}`;
}
function r1BusinessApplications(source) {
  const data = source.data;
  const failed = source.error_code ? `（${source.error_code}）` : '';
  const attempted = source.latest_attempt_at || source.sampled_at;
  const attemptNote = attempted ? `<p class="sub">最近尝试：${esc(r1BusinessTime(attempted))}</p>` : '';
  if (!source.available || !data) return `<p class="sub">${esc((source.reason || '尚未读取应用列表，请按需采集。') + failed)}</p>` + attemptNote;
  const rows = Array.isArray(data.apps) ? data.apps : [];
  const partial = data.partial === true || source.status === 'cached_partial';
  const partialNote = partial ? `<p class="st-warn" role="status">部分结果：已隔离 ${esc(r1BusinessNumber(data.rejected_entries))} 条无效条目。当前仅展示 ${rows.length} 条有效缓存，不代表全部已安装应用。</p>` : '';
  return partialNote + `<p class="sub">采集时间：${esc(r1BusinessTime(data.cache_modified_at))}。${esc((source.reason || '列表不代表应用实时运行状态。') + failed)}</p>` + attemptNote
    + (rows.length ? rows.map(row => `<div class="r1-business-row"><strong>${esc(row.name || '名称未知')}</strong><span>版本 ${esc(row.version || '--')} · 安装状态码 ${esc(r1BusinessNumber(row.installation_status_code))}</span></div>`).join('') : '<p class="sub">本次返回的列表为空。</p>');
}
function r1BusinessDownloads(source) {
  const engines = source.data && Array.isArray(source.data.engines) ? source.data.engines : source.engines || [];
  if (!engines.length) return `<p class="sub">${esc(source.reason || '下载数据暂不可用。')}</p>`;
  return engines.map(row => {
    const label = row.engine === 'qbittorrent' ? 'qBittorrent' : 'aria2', d = row.data;
    if (!row.available || !d) return `<div class="r1-business-row"><strong>${label}</strong><span>${esc(row.reason || '该引擎暂不可用')}</span></div>`;
    const counts = d.counts || {}, count = row.engine === 'qbittorrent'
      ? `当前窗口 ${r1BusinessNumber(counts.tasks)} 个任务`
      : `活动 ${r1BusinessNumber(counts.active)}，等待 ${r1BusinessNumber(counts.waiting)}，停止 ${r1BusinessNumber(counts.stopped)}`;
    const speed = value => typeof value === 'number' && Number.isFinite(value) ? F.bytesStr(value) + '/s' : '--';
    return `<div class="r1-business-row"><strong>${label} ${esc(d.version || '')}</strong><span>${esc(count)}</span><span>下载 ${esc(speed(d.download_bytes_per_second))} · 上传 ${esc(speed(d.upload_bytes_per_second))}</span><span class="sub">有界快照，不代表全部历史任务。</span></div>`;
  }).join('');
}
if (!LAYOUT_DEFAULT.apps.includes('r1business')) LAYOUT_DEFAULT.apps.push('r1business');
defineModule({
  id: 'r1business', page: 'apps', kicker: 'NAS SERVICES', title: '原厂业务状态',
  dek: '应用列表、下载队列与媒体处理状态；各来源独立报告采集时间与可用性',
  build(body, m) {
    body.innerHTML = `<div class="r1-business-grid">
      <section class="card pad"><h3>应用</h3><div data-ref="applications"></div><button class="btn" type="button" data-ref="refreshApplications">采集应用状态</button></section>
      <section class="card pad"><h3>下载</h3><div data-ref="downloads"></div><p class="sub" data-ref="downloadTime"></p></section>
      <section class="card pad"><h3>媒体</h3><div data-ref="media"></div><button class="btn" type="button" data-ref="refreshMedia">采集媒体状态</button></section>
      </div><p class="sub r1-note" data-ref="notice">应用与媒体按需采集。原厂查询可能初始化用户资源或更新完成标记；页面轮询只读取已保存结果，不自动发起这些查询。</p>`;
    m.x = collect(body);
    for (const [button, source] of [[m.x.refreshApplications, 'applications'], [m.x.refreshMedia, 'media']]) {
      button.onclick = async () => {
        button.disabled = true;
        const oldText = button.textContent; setText(button, '正在采集…');
        try {
          const result = await R1.post('/api/r1/business/refresh', { source });
          if (!result || result.ok !== true) throw new Error(result && result.error || '采集未完成');
          const saved = result.cache && result.cache.stored === true;
          const partial = result.result && (result.result.status === 'partial' || result.result.data && result.result.data.partial === true);
          toast(!saved ? '查询已结束，但快照未保存，请核对来源状态' : partial ? '已保存部分结果，无效条目已隔离' : result.result && result.result.available ? '采集结果已保存' : '已保存本次结果，来源暂不可用', { icon: saved && !partial && result.result && result.result.available ? 'check' : 'alert' });
          await R1_BUSINESS.load(true); renderNow();
        } catch (error) { toast('采集未确认：' + String(error.message || error), { icon: 'alert' }); }
        finally { button.disabled = false; setText(button, oldText); }
      };
    }
  },
  update(o, m) {
    const state = R1_BUSINESS.state, categories = state && state.snapshot && state.snapshot.categories;
    if (!categories) {
      for (const key of ['applications', 'downloads', 'media']) setText(m.x[key], R1_BUSINESS.error || '正在读取业务来源…');
      return;
    }
    setHTML(m.x.applications, r1BusinessApplications(categories.applications || {}));
    setHTML(m.x.downloads, r1BusinessDownloads(categories.downloads || {}));
    setText(m.x.downloadTime, '采集时间：' + r1BusinessTime(state.snapshot.sampled_at));
    const media = categories.media || {}, data = media.data;
    setHTML(m.x.media, data && Array.isArray(data.results)
      ? data.results.map(row => `<div class="r1-business-row"><strong>${esc(R1_MEDIA_LABELS[row.id] || '媒体状态')}</strong><span>${esc(r1MediaSummary(row))}</span><span class="sub">采集时间：${esc(r1BusinessTime(row.captured_at))}</span></div>`).join('')
      : `<p class="sub">${esc(media.reason || '尚未采集媒体状态。')}</p>`);
    setText(m.x.notice, (R1_BUSINESS.error ? '状态更新未完成：' + R1_BUSINESS.error + '。显示上次结果。' : '') + '应用与媒体按需采集。原厂查询可能初始化用户资源或更新完成标记；页面轮询只读取已保存结果，不自动发起这些查询。');
  },
});
Poll.add('r1-business', async () => { await R1_BUSINESS.load(); renderNow(); }, 30000, () => S.page === 'apps');
