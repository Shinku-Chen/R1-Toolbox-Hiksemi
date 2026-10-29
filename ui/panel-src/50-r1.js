/* =====================================================================
   R1 TOOLBOX ADDITIONS
   ---------------------------------------------------------------------
   fnmonitor's own modules and styles are used unchanged.  Everything in this
   file only *adds* what the HikSemi R1 can do and fnOS cannot:

     · 双路风扇（CPU / 系统）独立温控的实时状态与编辑
     · 机身灯光（四种原生灯效 / 夜间亮度）
     · 小屏亮度、息屏与 NAS 电源操作
     · 本地告警阈值、历史保留与流量统计范围
     · 登录口令 + 小屏五位配对码

   Reads come from /api/r1, writes from /api/r1/*; both are served by the host
   service in runtime/web_config.py.  No hardware is touched by the browser.
   ===================================================================== */

const R1_MODE_TXT = { manual: '固定输出', temperature: '手动 · 温控曲线' };
const R1_NATIVE_FAN_TXT = { auto: '自动', low: '低档', medium: '中档', high: '高档' };
const R1_LEVEL_TXT = { quiet: '静音', normal: '普通', full: '全速' };
const R1_EFFECT_TXT = { 0: '关闭', 1: '常亮', 2: '彩虹', 3: '呼吸' };

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

/** 三段温控曲线的触摸编辑器：温度节点 + 输出，并实时画出阶梯线。 */
function fanCurveEditor(item, touch) {
  const box = mount(`<div class="r1-curve">
    <svg class="r1-curve-chart" viewBox="0 0 240 92" aria-hidden="true">
      <path class="r1-curve-track" d="M8 82H232M8 8V82"></path>
      <path class="r1-curve-line" data-ref="line"></path>
      <g class="r1-curve-dots" data-ref="dots"></g>
    </svg>
    <div class="r1-curve-rows" data-ref="rows"></div>
  </div>`);
  const line = box.querySelector('[data-ref="line"]'), dots = box.querySelector('[data-ref="dots"]'), rows = box.querySelector('[data-ref="rows"]');
  const x = t => 8 + (clamp(t, 15, 85) - 15) / 70 * 224;
  const y = p => 82 - (clamp(p, 10, 100) - 10) / 90 * 74;
  const paint = () => {
    const qb = clamp(item.quiet_below, 15, 85), na = clamp(item.normal_at, 15, 85), fa = clamp(item.full_at, 15, 85);
    const q = clamp(item.quiet_percent, 10, 100), n = Math.max(q, clamp(item.normal_percent, 10, 100));
    line.setAttribute('d', `M8 ${y(q).toFixed(1)}H${x(qb).toFixed(1)}V${y(n).toFixed(1)}H${x(na).toFixed(1)}V${y(100).toFixed(1)}H232`);
    dots.innerHTML = [[qb, q], [na, n], [fa, 100]].map(([t, p]) => `<circle cx="${x(t).toFixed(1)}" cy="${y(p).toFixed(1)}" r="3.4"></circle>`).join('');
  };
  const addRow = (label, key, low, high, unit) => {
    const row = mount(`<div class="r1-curve-row"><span class="r1-curve-name">${esc(label)}</span><input class="r1-curve-input" type="number" inputmode="numeric" min="${low}" max="${high}" step="1" value="${item[key]}" aria-label="${esc(label + unit)}"><span class="r1-curve-unit">${esc(unit)}</span></div>`);
    row.querySelector('input').addEventListener('change', event => {
      const value = clamp(Math.round(Number(event.target.value) || item[key]), low, high);
      event.target.value = value;
      item[key] = value;
      touch(); paint();
    });
    rows.append(row);
  };
  addRow('静音', 'quiet_below', 15, 85, '℃');
  addRow('普通', 'normal_at', 15, 85, '℃');
  addRow('全速', 'full_at', 15, 85, '℃');
  const duty = mount(`<div class="r1-curve-rows" data-ref="duty"></div>`);
  const dutyInputs = {};
  const addDuty = (label, key, fixed) => {
    const row = mount(`<div class="r1-curve-row"><span class="r1-curve-name">${esc(label)}</span><input class="r1-curve-input" type="number" inputmode="numeric" min="${key === 'normal_percent' ? item.quiet_percent : 10}" max="100" step="1" value="${fixed == null ? item[key] : 100}" aria-label="${esc(label)}" ${fixed == null ? '' : 'disabled'}><span class="r1-curve-unit">%</span></div>`);
    if (fixed == null) {
      dutyInputs[key] = row.querySelector('input');
      dutyInputs[key].addEventListener('change', event => {
        const entered = Number(event.target.value);
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
        form.append(r1Field('温度曲线', fanCurveEditor(item, touch), '输出范围 10–100%，普通不低于静音，全速固定 100%。升温到节点后下一轮升档；降温 3°C 才降档。'));
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
    rows.append(r1Field('登录口令', r1Check(a.enabled ? '访问面板需要登录口令' : '当前无需口令（监控公开）', !!a.enabled,
      v => post({ action: v ? 'enable' : 'disable' })), '开启后访问面板需先输入口令；关闭后监控恢复公开，写操作仍需小屏配对码。'));
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
        // 首次口令需要先解锁一次（小屏配对码），避免任何局域网客户端抢设口令。
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
      const mode = (R1.st && R1.st.web && R1.st.web.auth_mode) || (a.auth_mode) || 'pair';
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
    const passwordMode = (R1.st && R1.st.web && R1.st.web.auth_mode) === 'password' || a.auth_mode === 'password';
    setHTML(x.status, a.password_required ? '当前已启用登录口令；面板读取与写入都需要登录。' : passwordMode ? '当前未启用登录口令；只读监控公开，设备写操作默认开放。' : '当前未启用登录口令；只读监控公开，设备写操作需要小屏五位配对码。');
    x.pairing = a.pairing;
  };
  card._sync();
}

/** API metadata is loaded only when its manager opens, never by the status poll. */
function r1ApiSettings(card) {
  card.innerHTML = `<div class="r1-api-intro"><div><div class="lbl">局域网 AI 与自动化</div><p class="hint">管理 MCP 与 REST 的独立访问密钥、角色、功能和文件范围。默认关闭；密钥仅在创建时显示一次。</p></div><button class="btn" type="button" data-ref="open">打开 API 管理</button></div>`;
  card._r = collect(card);
  card._r.open.onclick = r1OpenApiManager;
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
    <h2 id="r1ApiSecretTitle">保存新密钥</h2><p>密钥仅显示这一次。关闭后无法再次查看；请保存到 AI 客户端的安全配置中。</p>
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
    const opener = document.querySelector('#r1ApiManager [data-ref="create"]'); if (opener) opener.focus();
  };
  const onKey = event => { if (event.key === 'Escape') { event.stopImmediatePropagation(); close(); } };
  x.copyToken.onclick = () => r1ApiCopy(x.token, x.copyStatus);
  x.copyConfig.onclick = () => r1ApiCopy(x.config, x.copyStatus);
  x.close.onclick = close;
  document.addEventListener('keydown', onKey, true);
  document.body.append(el); x.close.focus();
}

function r1OpenApiManager() {
  if ($('#r1ApiManager')) return;
  const el = mount(`<div class="fullmask r1-api-mask" id="r1ApiManager" role="dialog" aria-modal="true" aria-labelledby="r1ApiTitle"><div class="card fm-card r1-api-dialog">
    <div class="r1-api-head"><h2 id="r1ApiTitle">API 管理</h2><button class="btn" type="button" data-ref="close" aria-label="关闭 API 管理">关闭</button></div>
    <p>仅允许持密钥的局域网客户端读取或操作 NAS。角色限定操作等级，功能和文件范围分别授权，可随时撤销。</p>
    <p class="sub" data-ref="status" role="status">正在读取 API 设置…</p><p class="st-crit" data-ref="error" role="alert" hidden></p>
    <div data-ref="content" hidden>
      <div class="r1-api-service"><label class="check"><input type="checkbox" data-ref="enabled">启用局域网 API 服务</label><span class="sub" data-ref="serviceStatus"></span></div>
      <div class="r1-api-endpoints"><label>MCP<input class="textfield mono" data-ref="mcp" readonly aria-label="MCP 地址"></label><label>REST 状态<input class="textfield mono" data-ref="restStatus" readonly aria-label="REST 状态地址"></label><label>REST 操作<input class="textfield mono" data-ref="restAction" readonly aria-label="REST 操作地址"></label><label>功能能力目录<input class="textfield mono" data-ref="restCapabilities" readonly aria-label="功能能力目录地址"></label></div>
      <h3>创建访问密钥</h3><form data-ref="form" class="r1-api-create">
        <label class="r1-api-input">密钥名称<input class="textfield" data-ref="name" aria-label="密钥名称" placeholder="例如：客厅 AI" maxlength="64" required autocomplete="off"></label>
        <label class="r1-api-input">操作角色<select class="textfield" data-ref="role" aria-label="操作角色"></select><span class="sub" data-ref="roleDescription"></span></label>
        <fieldset class="r1-api-scopes"><legend>允许的功能</legend><div data-ref="scopes"></div></fieldset>
        <p class="sub r1-api-permission-note" data-ref="roleStatus" role="status"></p>
        <fieldset class="r1-api-file-roots" data-ref="fileRoots" hidden><legend>允许访问的文件范围</legend><p class="sub">待接入，当前仅保存范围授权；尚不能通过 AI 浏览、上传或下载文件。只会显示宿主已确认的存储根，不会自动授予全盘。</p><p class="sub" data-ref="fileRootsStatus" role="status"></p><div data-ref="fileRootOptions"></div></fieldset>
        <label class="r1-api-input">允许的来源 IP（可选）<textarea class="textfield mono" data-ref="ips" rows="2" aria-label="允许的来源 IP" placeholder="每行一个精确 IPv4 或 IPv6 地址"></textarea><span class="sub">留空允许持密钥的局域网客户端；不接受网段、域名或通配符。</span></label>
        <label class="r1-api-input">有效期<select class="textfield" data-ref="expiry" aria-label="密钥有效期"><option value="7">7 天</option><option value="30">30 天</option><option value="90" selected>90 天</option><option value="365">365 天</option><option value="0">长期</option></select></label>
        <button class="btn primary" type="submit" data-ref="create">创建密钥</button>
      </form>
      <div class="r1-api-head"><h3>访问密钥</h3><button class="btn" type="button" data-ref="refresh">刷新列表</button></div><div class="r1-api-list" data-ref="keys"></div>
      <h3>最近调用记录</h3><p class="sub">仅显示时间、密钥标识、来源、功能和结果，不记录令牌或请求内容。</p><div class="r1-api-list" data-ref="audit"></div>
    </div></div></div>`);
  const x = collect(el), allowedScopes = ['read', 'fan', 'light', 'screen', 'power', 'media', 'files', 'docker', 'apps', 'downloads', 'movie', 'albums', 'sharing', 'backup', 'system'];
  const roleLevel = { readonly: 0, readwrite: 1, admin: 2 }, roleLabels = { readonly: '只读', readwrite: '读写', admin: '管理员' };
  const defaultMinRole = { read: 'readonly', fan: 'readwrite', light: 'readwrite', screen: 'readwrite', power: 'admin', media: 'readwrite' };
  let state = null, busy = false, closed = false;
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
    x.enabled.disabled = value || !state || state.storage_ok !== true;
    x.create.disabled = value || !state || state.storage_ok !== true || (filesSelected() && !fileRoots().length);
    x.refresh.disabled = value;
    x.keys.querySelectorAll('button').forEach(button => { button.disabled = value || !state || state.storage_ok !== true; });
  };
  const names = () => new Map((Array.isArray(state && state.scope_catalog) ? state.scope_catalog : []).map(scope => [scope.id, scope.label || scope.id]));
  const filesSelected = () => !!x.scopes.querySelector('input[value="files"]:checked');
  const fileRoots = () => state && state.file_root_catalog && state.file_root_catalog.available === true && Array.isArray(state.file_root_catalog.roots)
    ? state.file_root_catalog.roots.filter(root => root && /^root_[0-9a-f]{24}$/.test(root.id) && typeof root.label === 'string' && typeof root.mount === 'string' && typeof root.read_only === 'boolean') : [];
  const roles = () => (Array.isArray(state && state.role_catalog) ? state.role_catalog : []).filter(role => Object.hasOwn(roleLevel, role.id));
  const scopeMinimum = scope => Object.hasOwn(roleLevel, scope.min_role) ? scope.min_role : (defaultMinRole[scope.id] || 'readonly');
  const scopeName = (id, labels) => {
    const scope = (Array.isArray(state.scope_catalog) ? state.scope_catalog : []).find(item => item.id === id);
    return (labels.get(id) || id) + (scope && scope.implemented === false ? '（待接入）' : '');
  };
  /** A path grant is relative to an independently discovered storage root. */
  const validSubpath = value => typeof value === 'string' && value.length <= 1024 && !/[\\\u0000-\u001f\u007f]/.test(value) && !/^[A-Za-z]:/.test(value)
    && (!value || value.split('/').every(part => part && part !== '.' && part !== '..'));
  const updatePermissions = () => {
    const role = x.role.value, level = roleLevel[role] ?? -1;
    const selected = new Set([...x.scopes.querySelectorAll('input:checked')].map(input => input.value));
    const limited = [];
    for (const scope of Array.isArray(state && state.scope_catalog) ? state.scope_catalog : []) {
      const input = [...x.scopes.querySelectorAll('input')].find(item => item.value === scope.id);
      if (!input) continue;
      const minRole = scopeMinimum(scope), tooLow = level < roleLevel[minRole];
      const note = input.closest('label').querySelector('[data-scope-note]');
      note.textContent = scope.implemented === false ? '待接入，仅预授权；当前没有可调用工具。'
        : tooLow ? `当前角色不可调用此类工具；需要${roleLabels[minRole]}角色。` : '已接入；仍需同时满足所选功能权限。';
      if (selected.has(scope.id) && scope.implemented !== false && tooLow) limited.push(String(scope.label || scope.id));
    }
    const description = roles().find(item => item.id === role);
    setText(x.roleDescription, description ? String(description.description || '') : '未获得有效角色配置，请刷新后重试。');
    setText(x.roleStatus, limited.length ? `已选择${limited.join('、')}，当前角色不可调用这些工具。提高角色等级后仍只授予已勾选的功能。`
      : '提高角色等级不会自动勾选功能；待接入的功能只保存预授权。');
    x.fileRoots.hidden = !filesSelected();
    for (const item of x.fileRootOptions.querySelectorAll('.r1-api-file-root')) {
      const checked = item.querySelector('[data-root-check]').checked;
      const access = item.querySelector('[data-root-access]');
      const writable = role !== 'readonly' && item.dataset.readOnly !== 'true';
      access.querySelector('option[value="readwrite"]').disabled = !writable;
      if (!writable) access.value = 'readonly';
      access.disabled = !checked;
      item.querySelector('[data-root-path]').disabled = !checked;
      item.querySelector('[data-root-fields]').hidden = !checked;
      setText(item.querySelector('[data-root-note]'), item.dataset.readOnly === 'true' ? '此存储根为只读挂载，只能授权读取。'
        : role === 'readonly' ? '当前角色只能授权读取；切换为读写或管理员后可选择读写。' : '仅对勾选的根与子目录保存授权。');
    }
    pending(busy);
  };
  const drawFileRoots = () => {
    const drafts = new Map([...x.fileRootOptions.querySelectorAll('.r1-api-file-root')].map(item => [item.dataset.rootId,
      { checked: item.querySelector('[data-root-check]').checked, path: item.querySelector('[data-root-path]').value, access: item.querySelector('[data-root-access]').value }]));
    const roots = fileRoots();
    x.fileRootOptions.replaceChildren();
    const reason = state.file_root_catalog && state.file_root_catalog.reason;
    setText(x.fileRootsStatus, roots.length ? '请至少勾选一个存储根；子目录留空表示整个已选根。' : `没有已确认的存储根，不能创建文件权限。${reason ? '原因：' + String(reason) : '请确认磁盘已挂载后刷新。'}`);
    for (const root of roots) {
      const item = mount(`<article class="r1-api-file-root"><label class="r1-api-root-choice"><input type="checkbox" data-root-check><span data-root-label></span></label><p class="sub mono" data-root-mount></p><div class="r1-api-file-fields" data-root-fields hidden><label class="r1-api-input">子目录（可选）<input class="textfield" data-root-path maxlength="1024" placeholder="例如：电影／2026，请使用 / 分隔" autocomplete="off"><span class="sub">相对目录使用 / 分隔，不接受绝对路径、反斜杠、. 或 ..。</span></label><label class="r1-api-input">访问方式<select class="textfield" data-root-access><option value="readonly">只读</option><option value="readwrite">读写</option></select></label></div><p class="sub" data-root-note></p></article>`);
      item.dataset.rootId = root.id; item.dataset.readOnly = String(root.read_only);
      setText(item.querySelector('[data-root-label]'), root.label);
      setText(item.querySelector('[data-root-mount]'), root.mount);
      const checkbox = item.querySelector('[data-root-check]'), path = item.querySelector('[data-root-path]'), access = item.querySelector('[data-root-access]');
      checkbox.value = root.id; path.setAttribute('aria-label', root.label + '的子目录'); access.setAttribute('aria-label', root.label + '的访问方式');
      const draft = drafts.get(root.id);
      if (draft) { checkbox.checked = draft.checked; path.value = draft.path; access.value = draft.access; }
      checkbox.onchange = updatePermissions;
      x.fileRootOptions.append(item);
    }
  };
  const draw = () => {
    const labels = names();
    x.content.hidden = false; x.enabled.checked = state.enabled === true;
    setText(x.status, state.storage_ok === true ? '密钥只在创建时显示。关闭服务后，所有 MCP 与 REST 密钥立即停止访问。' : '访问配置存储不可用，暂不能启用服务或管理密钥。');
    setText(x.serviceStatus, state.enabled === true ? '已开启' : '已关闭');
    x.mcp.value = location.origin + '/mcp'; x.restStatus.value = location.origin + '/api/integration/status'; x.restAction.value = location.origin + '/api/integration/action'; x.restCapabilities.value = location.origin + '/api/integration/capabilities';
    const chosenRole = x.role.value || 'readonly';
    x.role.replaceChildren();
    for (const role of roles()) { const option = document.createElement('option'); option.value = role.id; option.textContent = String(role.label || roleLabels[role.id]); x.role.append(option); }
    x.role.value = roles().some(role => role.id === chosenRole) ? chosenRole : 'readonly';
    const chosenScopes = x.scopes.children.length ? new Set([...x.scopes.querySelectorAll('input:checked')].map(input => input.value)) : new Set(['read']);
    x.scopes.replaceChildren();
    for (const scope of Array.isArray(state.scope_catalog) ? state.scope_catalog : []) {
      if (!allowedScopes.includes(scope.id)) continue;
      const label = mount(`<label class="r1-api-scope"><input type="checkbox"><span><b></b><span class="sub" data-scope-description></span><span class="sub" data-scope-note></span></span></label>`);
      const input = label.querySelector('input'); input.value = scope.id; input.checked = chosenScopes.has(scope.id);
      label.querySelector('b').textContent = String(scope.label || scope.id);
      label.querySelector('[data-scope-description]').textContent = String(scope.description || '');
      input.onchange = updatePermissions;
      x.scopes.append(label);
    }
    const keys = Array.isArray(state.keys) ? state.keys : [];
    x.keys.replaceChildren();
    if (!keys.length) x.keys.append(mount('<p class="sub">尚未创建访问密钥。</p>'));
    for (const key of keys) {
      const item = mount(`<article class="r1-api-item"><div class="r1-api-head"><strong data-ref="name"></strong><span class="pill" data-ref="status"></span></div><dl class="r1-api-meta"><dt>操作角色</dt><dd data-ref="role"></dd><dt>功能权限</dt><dd data-ref="scopes"></dd><dt>文件范围</dt><dd data-ref="files"></dd><dt>允许来源</dt><dd data-ref="ips"></dd><dt>创建时间</dt><dd data-ref="created"></dd><dt>到期时间</dt><dd data-ref="expires"></dd><dt>最后使用</dt><dd data-ref="used"></dd></dl><button class="btn danger" type="button" data-ref="revoke">撤销密钥</button></article>`);
      const k = collect(item);
      setText(k.name, String(key.name || '未命名密钥')); setText(k.status, ({ active: '有效', expired: '已到期', revoked: '已撤销' })[key.status] || '状态未知');
      setText(k.role, !key.role || key.role === 'legacy' ? '原有精确权限' : (roles().find(role => role.id === key.role) || {}).label || roleLabels[key.role] || '角色未知');
      setText(k.scopes, (Array.isArray(key.scopes) ? key.scopes : []).map(id => scopeName(id, labels)).join('、') || '未授予权限');
      const grants = Array.isArray(key.file_roots) ? key.file_roots : [];
      if (!grants.length) setText(k.files, '未授予文件范围');
      for (const grant of grants) {
        const root = fileRoots().find(item => item.id === grant.id), row = document.createElement('p');
        setText(row, `${root ? root.label : '当前未确认的存储根（' + String(grant.id || '未知') + '）'} · ${grant.path ? '子目录：' + String(grant.path) : '整个已选根'} · ${({ readonly: '只读', readwrite: '读写' })[grant.access] || '权限未知'}`);
        k.files.append(row);
      }
      setText(k.ips, Array.isArray(key.allowed_ips) && key.allowed_ips.length ? key.allowed_ips.join('、') : '持密钥的局域网客户端');
      setText(k.created, r1ApiTime(key.created_at)); setText(k.expires, key.expires_at === null ? '长期' : r1ApiTime(key.expires_at)); setText(k.used, r1ApiTime(key.last_used_at));
      if (key.status === 'revoked') k.revoke.hidden = true;
      k.revoke.onclick = async () => {
        if (busy || !(await R1.ask('撤销访问密钥？', `撤销「${esc(key.name || '未命名密钥')}」后，使用此密钥的客户端立即失去访问权限。`, true))) return;
        await mutate('/api/access/keys/revoke', { id: key.id });
      };
      x.keys.append(item);
    }
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
    drawFileRoots(); updatePermissions();
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
      if (url === '/api/access/keys') {
        if (!closed) { r1ShowApiSecret(result.token); x.name.value = ''; x.ips.value = ''; }
        result.token = '';
      }
      await refresh(); return result;
    } catch (error) { fail(error); return null; }
    finally { if (!closed) pending(false); }
  };
  x.enabled.onchange = async () => {
    if (busy || !state) return;
    const enabled = x.enabled.checked; x.enabled.checked = state.enabled === true;
    if (enabled && !(await R1.ask('开启局域网 API 服务？', '已创建的有效密钥将能按授权功能访问 NAS。请将密钥仅交给可信客户端。'))) return;
    await mutate('/api/access/settings', { enabled });
  };
  x.form.onsubmit = async event => {
    event.preventDefault(); if (busy) return;
    const name = x.name.value.trim(), role = x.role.value, scopes = [...x.scopes.querySelectorAll('input:checked')].map(input => input.value);
    const allowed_ips = x.ips.value.split(/[\s,，;；]+/).map(value => value.trim()).filter(Boolean);
    if (!name || !scopes.length) { fail(new Error('请填写名称，并选择至少一项功能权限。')); return; }
    if (!Object.hasOwn(roleLevel, role)) { fail(new Error('请选择有效的操作角色。')); return; }
    if (new Set(allowed_ips).size !== allowed_ips.length || allowed_ips.some(ip => /[/\*]/.test(ip))) { fail(new Error('来源 IP 不可重复，也不接受网段或通配符。')); return; }
    const file_roots = [];
    if (scopes.includes('files')) {
      if (!fileRoots().length) { fail(new Error('没有已确认的存储根，不能创建文件权限。')); return; }
      for (const item of x.fileRootOptions.querySelectorAll('.r1-api-file-root')) {
        if (!item.querySelector('[data-root-check]').checked) continue;
        const path = item.querySelector('[data-root-path]').value, access = item.querySelector('[data-root-access]').value;
        if (!validSubpath(path)) { fail(new Error('文件子目录必须是相对 POSIX 路径，不接受绝对路径、空路径段、反斜杠、. 或 ..，且不超过 1024 个字符。')); return; }
        if (!['readonly', 'readwrite'].includes(access) || (access === 'readwrite' && (role === 'readonly' || item.dataset.readOnly === 'true'))) { fail(new Error('只读角色或只读挂载不能授予文件读写权限。')); return; }
        file_roots.push({ id: item.dataset.rootId, path, access });
      }
      if (!file_roots.length) { fail(new Error('请至少勾选一个已确认的文件存储根。')); return; }
    }
    const labels = names(), grant = scopes.map(id => labels.get(id) || id).join('、');
    const rangeNote = file_roots.length ? `文件范围：${file_roots.map(item => { const root = fileRoots().find(root => root.id === item.id); return esc(root.label) + '／' + esc(item.path || '整个已选根') + '（' + (item.access === 'readwrite' ? '读写' : '只读') + '）'; }).join('、')}。文件功能待接入，当前仅保存范围授权。` : '';
    if (!(await R1.ask('创建访问密钥？', `角色：${roleLabels[role]}。将向「${esc(name)}」授予：${esc(grant)}。${scopes.includes('power') ? '电源权限可安排关机或重启，需要管理员角色。' : ''}${rangeNote}`))) return;
    await mutate('/api/access/keys', { name, role, scopes, file_roots, allowed_ips, expires_days: Number(x.expiry.value) });
  };
  x.refresh.onclick = async () => {
    if (busy) return;
    pending(true); x.error.hidden = true;
    try { await refresh(); } catch (error) { fail(error); } finally { if (!closed) pending(false); }
  };
  x.role.onchange = updatePermissions;
  x.close.onclick = close; document.addEventListener('keydown', onKey);
  document.body.append(el); pending(true);
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
    const passwordMode = web.auth_mode === 'password';
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
  { id: 'r1-auth', title: '登录口令', icon: 'lock', desc: '面板登录口令（可选，默认关闭）。启用后网页写操作需要先登录；也可通过 R1_WEB_AUTH_MODE=password 完全停用配对码。', custom: r1AuthSettings },
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
