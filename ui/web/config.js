"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {overview:"概览",storage:"存储",network:"网络",controls:"控制",apps:"应用",system:"系统",settings:"设置"};
  let csrf = "", busy = false;
  let fanSnapshot = null, fanDirty = false, fanPollRunning = false, fanAwaitingTransition = null;
  const fanSourceSignatures = {};
  const fanNames = { cpu: "CPU 风扇", system: "系统风扇" };
  const fanModes = { vendor: "原厂管理", manual: "手动固定输出", temperature: "自定义温控" };
  const fanLevels = { vendor: "原厂管理", quiet: "静音档", normal: "普通档", full: "全速档", manual: "手动固定", failsafe: "保护全速" };
  Object.entries(labels).forEach(([value, text]) => {
    const label = document.createElement("label"), input = document.createElement("input");
    label.className = "check"; input.type = "checkbox"; input.value = value;
    label.append(input, document.createTextNode(text)); $("pages").append(label);
  });
  function message(text) { $("message").textContent = text; }
  function signedOut() {
    csrf = ""; $("login").hidden = false; $("editor").hidden = true; $("logout").hidden = true;
    $("code").value = ""; fanSnapshot = null; fanDirty = false; fanAwaitingTransition = null;
    $("fan-policy-section").hidden = true;
  }
  async function request(path, body, auth = true) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 8000);
    const options = {credentials:"same-origin",cache:"no-store",signal:controller.signal};
    if (body !== undefined) {
      options.method = "POST"; options.headers = {"Content-Type":"application/json"};
      if (auth) options.headers["X-R1-CSRF"] = csrf;
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options), value = await response.json();
      if (response.status === 401) signedOut();
      if (!response.ok && !(value.saved === true || value.settings_saved === true)) throw new Error(value.error || "请求未完成，请稍后重试");
      return value;
    } finally { clearTimeout(timeout); }
  }
  function disable(section, disabled) {
    document.querySelector(`[data-section="${section}"]`).querySelectorAll("input,select,button").forEach((node) => { node.disabled = disabled; });
  }
  function render(state) {
    const settings = state.settings || {}, caps = state.capabilities || {};
    ["theme","font_scale","page_interval","brightness","night_start","night_end","night_brightness","sleep_timeout"].forEach((key) => {
      if (settings[key] !== undefined) $(key).value = settings[key];
    });
    $("night_enabled").checked = settings.night_enabled === true;
    document.querySelectorAll("#pages input").forEach((node) => { node.checked = (settings.enabled_pages || []).includes(node.value); });
    const brightness = caps.brightness && caps.brightness.available === true, screen = caps.screen && caps.screen.available === true;
    $("screen-card").hidden = !screen && !brightness;
    document.querySelector('[data-section="brightness"]').hidden = !brightness;
    $("screen-buttons").hidden = !screen;
    $("night-card").hidden = !screen && !brightness;
    document.querySelector('[data-section="night"]').hidden = !brightness;
    document.querySelector('[data-section="sleep"]').hidden = !screen;
    disable("brightness", !brightness); disable("night", !brightness); disable("sleep", !screen);
    $("screen-sleep").disabled = !screen; $("screen-wake").disabled = !screen;
    $("screen-hint").textContent = brightness ? `当前实际亮度：${state.screen.brightness}%${state.screen.asleep ? " · 已息屏" : ""}` : "背光能力尚未验证，相关控制暂不可用。";
    const rgb = caps.rgb && caps.rgb.available === true;
    $("rgb-card").hidden = !rgb;
    disable("rgb", !rgb); $("rgb-hint").textContent = rgb ? "灯控芯片已在小屏验证。本次仅立即应用。" : "请先在小屏明确选择总线并验证灯控芯片，网页不会自行探测。";
    if (state.rgb && Number.isInteger(state.rgb.mode)) $("rgb-mode").value = state.rgb.mode;
    if (state.rgb && /^#[a-fA-F0-9]{6}$/.test(state.rgb.color || "")) $("rgb-color").value = state.rgb.color;
    if (state.rgb && Number.isInteger(state.rgb.brightness)) $("rgb-brightness").value = state.rgb.brightness;
    if (state.settings_error) message(state.settings_error);
  }
  async function refresh() {
    const value = await request("/api/session");
    csrf = value.csrf; render(value.state); renderFanPolicy(value.fan_policy || value.state.fan_policy, true); $("login").hidden = true; $("editor").hidden = false; $("logout").hidden = false;
  }
  function fanElement(tag, className, text) {
    const node = document.createElement(tag); node.className = className;
    if (text !== undefined) node.textContent = text; return node;
  }
  function createFanForms() {
    Object.entries(fanNames).forEach(([channel, title]) => {
      const card = fanElement("section", "fan-policy-channel"); card.dataset.fanChannel = channel;
      const heading = fanElement("h3", "", title), live = fanElement("p", "fan-policy-live", "等待读取实时状态"); live.id = "fan-" + channel + "-live";
      const fields = fanElement("div", "fan-policy-fields");
      const modeLabel = fanElement("label", "", "控制模式"), mode = document.createElement("select"); mode.id = "fan-" + channel + "-mode";
      Object.entries(fanModes).forEach(([value, label]) => { const option = document.createElement("option"); option.value = value; option.textContent = label; mode.append(option); });
      modeLabel.append(mode); fields.append(modeLabel);
      const sourceBox = fanElement("fieldset", "fan-sources"); sourceBox.id = "fan-" + channel + "-sources";
      sourceBox.append(fanElement("legend", "", "温度来源 · 可多选，取最高温")); fields.append(sourceBox);
      const thresholds = fanElement("div", "fan-policy-numbers"); thresholds.dataset.fanMode = "temperature";
      [["quiet_below", "回到静音 ≤（°C）", 15, 85], ["normal_at", "进入普通 ≥（°C）", 15, 85], ["full_at", "进入全速 ≥（°C）", 15, 85], ["quiet_percent", "静音输出（50–100%）", 50, 100], ["normal_percent", "普通输出（50–100%）", 50, 100]].forEach(([key, label, min, max]) => {
        const row = fanElement("label", "", label), input = document.createElement("input"); input.id = "fan-" + channel + "-" + key; input.type = "number"; input.min = min; input.max = max; input.step = "1"; input.inputMode = "numeric"; row.append(input); thresholds.append(row);
      }); fields.append(thresholds);
      const manual = fanElement("label", "", "固定输出（50–100%）"); manual.dataset.fanMode = "manual";
      const input = document.createElement("input"); input.id = "fan-" + channel + "-manual_percent"; input.type = "number"; input.min = "50"; input.max = "100"; input.step = "1"; input.inputMode = "numeric"; manual.append(input); fields.append(manual);
      const rule = fanElement("p", "small", "仅本路使用这些来源与阈值。CPU 和系统风扇互不共用配置；缺少 RPM 时只显示请求输出。");
      card.append(heading, live, fields, rule); $("fan-policy-channels").append(card);
    });
  }
  function fanFreshness(source) {
    if (!source) return "来源暂不可用";
    const age = typeof source.age_seconds === "number" ? Math.max(0, Math.round(source.age_seconds)) + " 秒前" : "采样时间未提供";
    const state = { ok: source.fresh ? "有效" : "已过期", standby: "休眠", stale: "已过期", unavailable: "不可读取" }[source.status] || "状态未知";
    return state + " · " + age;
  }
  function fanRuntimeLabel(runtime) { return String(runtime.reason || "").includes("恢复未确认") ? "恢复未确认" : fanLevels[runtime.level] || fanModes[runtime.mode] || "状态未提供"; }
  function fanSourceLine(source) {
    const temperature = typeof source.temperature_c === "number" ? source.temperature_c + " °C" : "温度 --";
    return temperature + " · " + fanFreshness(source);
  }
  function updateFanModes() {
    Object.keys(fanNames).forEach(channel => {
      const card = document.querySelector('[data-fan-channel="' + channel + '"]'), mode = $("fan-" + channel + "-mode").value;
      card.querySelectorAll("[data-fan-mode]").forEach(node => { node.hidden = node.dataset.fanMode !== mode; });
      $("fan-" + channel + "-sources").hidden = mode === "vendor";
    });
  }
  function renderFanSources(channel, config, sources, force) {
    const box = $("fan-" + channel + "-sources"), previous = Array.from(box.querySelectorAll("input:checked"), node => node.value);
    const selected = force || !fanDirty ? (config.sources || []) : previous;
    const values = new Map(sources.map(source => [source.id, source]));
    selected.forEach(id => { if (!values.has(id)) values.set(id, { id, label: "已保存的来源暂缺 · " + id, status: "unavailable", fresh: false }); });
    const signature = JSON.stringify(Array.from(values.keys()));
    if (force || fanSourceSignatures[channel] !== signature) {
      box.querySelectorAll("label,p").forEach(node => node.remove());
      values.forEach((source, id) => {
        const label = fanElement("label", "fan-source"), input = document.createElement("input"); input.type = "checkbox"; input.value = id; input.checked = selected.includes(id);
        const text = fanElement("span", "", source.label || id), reading = fanElement("span", "fan-source-reading", fanSourceLine(source)); reading.dataset.sourceId = id;
        text.append(reading); label.append(input, text); box.append(label);
      });
      if (!values.size) box.append(fanElement("p", "small", "尚未发现温度源。已保存的来源会保留；温控应用受后端能力与保护检查约束。"));
      fanSourceSignatures[channel] = signature;
    } else {
      if (!fanDirty) box.querySelectorAll("input").forEach(node => { node.checked = selected.includes(node.value); });
      box.querySelectorAll(".fan-source-reading").forEach(node => { const source = values.get(node.dataset.sourceId); node.textContent = fanSourceLine(source); });
    }
  }
  function renderFanPolicy(snapshot, force = false) {
    if (!snapshot || !snapshot.config || !snapshot.config.channels || !snapshot.config.channels.cpu || !snapshot.config.channels.system) { fanSnapshot = null; $("fan-policy-section").hidden = true; return; }
    fanSnapshot = snapshot; $("fan-policy-section").hidden = false;
    const sources = Array.isArray(snapshot.sources) ? snapshot.sources : [], capability = snapshot.capability || {};
    Object.keys(fanNames).forEach(channel => {
      const config = snapshot.config.channels[channel], runtime = snapshot.channels && snapshot.channels[channel] || {};
      if (force || !fanDirty) ["mode", "quiet_below", "normal_at", "full_at", "quiet_percent", "normal_percent", "manual_percent"].forEach(key => { if (config[key] !== undefined) $("fan-" + channel + "-" + key).value = config[key]; });
      renderFanSources(channel, config, sources, force);
      const source = sources.find(item => item.id === runtime.source_id);
      const reference = typeof runtime.reference_c === "number" ? runtime.reference_c + " °C" : "-- °C";
      const target = typeof runtime.target_percent === "number" ? "请求输出 " + runtime.target_percent + "%" : "输出未提供";
      const rpm = typeof runtime.rpm === "number" ? runtime.rpm + " RPM" : "RPM 未知";
      $("fan-" + channel + "-live").textContent = (runtime.active ? "运行中 · " : "当前 · ") + fanRuntimeLabel(runtime) + "\n参考 max：" + reference + "\n来源：" + (runtime.source_label || "未提供") + " · " + fanFreshness(source) + "\n" + target + " · " + rpm + (runtime.reason ? "\n" + runtime.reason : "");
    });
    const state = snapshot.busy ? "正在切换控制方式" : snapshot.last_error ? "控制异常 · 请核对恢复状态" : fanDirty ? "有尚未保存的修改" : snapshot.pending_changes ? "已保存 · 有待生效的配置" : snapshot.active ? "两路分别执行中" : "当前由原厂管理";
    $("fan-policy-save-state").textContent = state;
    $("fan-policy-capability").textContent = capability.available ? "已验证独立控制接口。应用前需要确认接管；保存配置本身不会修改当前风扇输出。" : "当前不能接管：" + (capability.reason || "独立控制能力未验证") + "。仍可编辑并保存配置，保存不代表已应用。";
    $("fan-policy-runtime-note").textContent = snapshot.last_error || snapshot.settings_error || (snapshot.active_on_boot ? "已启用开机恢复；每次恢复仍须通过硬件与温度源检查。" : "开机接管尚未启用。纯保存只保存草稿，不改变当前控制方式。");
    const locked = busy || snapshot.busy === true;
    $("fan-policy-form").querySelectorAll("input,select,button").forEach(node => { node.disabled = locked; });
    $("fan-policy-apply").disabled = locked || capability.available !== true;
    $("fan-policy-release").disabled = locked || !(snapshot.active || snapshot.active_on_boot || snapshot.last_error || Object.values(snapshot.config.channels).some(item => item.mode !== "vendor"));
    $("fan-policy-release").textContent = Object.values(snapshot.channels || {}).some(item => String(item.reason || "").includes("恢复未确认")) ? "重试两路恢复原厂管理" : "两路恢复原厂管理";
    if (snapshot.busy) $("fan-policy-result").textContent = "配置已保存，正在切换控制方式。请等待实时状态确认，勿重复提交。";
    else if (fanAwaitingTransition) {
      $("fan-policy-result").textContent = snapshot.last_error ? "配置已保存，但未生效：" + snapshot.last_error : fanAwaitingTransition === "release" ? "两路已恢复原厂管理。" : snapshot.pending_changes ? "配置已保存，仍有待生效的修改，请核对实时状态。" : "独立控制切换完成，请核对两路当前状态。";
      fanAwaitingTransition = null;
    }
    updateFanModes();
  }
  function fanConfigFromInputs() {
    const config = { schema_version: 1, channels: {} };
    Object.keys(fanNames).forEach(channel => {
      const fields = { mode: $("fan-" + channel + "-mode").value, sources: Array.from($("fan-" + channel + "-sources").querySelectorAll("input:checked"), node => node.value) };
      ["quiet_below", "normal_at", "full_at", "quiet_percent", "normal_percent", "manual_percent"].forEach(key => {
        const value = Number($("fan-" + channel + "-" + key).value), threshold = key.endsWith("_at") || key === "quiet_below";
        if (!Number.isInteger(value) || value < (threshold ? 15 : 50) || value > (threshold ? 85 : 100)) throw new Error(fanNames[channel] + "：温度须为 15–85°C 整数，输出须为 50–100% 整数。");
        fields[key] = value;
      });
      if (!(fields.quiet_below < fields.normal_at && fields.normal_at < fields.full_at)) throw new Error(fanNames[channel] + "：回到静音温度 < 进入普通温度 < 进入全速温度。");
      if (fields.quiet_percent > fields.normal_percent) throw new Error(fanNames[channel] + "：静音输出不能高于普通输出。");
      if (fields.sources.length > 32 || (fields.mode === "temperature" && !fields.sources.length)) throw new Error(fanNames[channel] + "：温控模式至少选择 1 个、最多 32 个温度源。");
      config.channels[channel] = fields;
    });
    return config;
  }
  async function pollFanPolicy(force = false) {
    if (fanPollRunning || busy || $("editor").hidden) return;
    fanPollRunning = true;
    try {
      const value = await request("/api/session"); csrf = value.csrf;
      renderFanPolicy(value.fan_policy || value.state.fan_policy, force);
    } catch (_) {
      if (!$("editor").hidden) { $("fan-policy-capability").textContent = "实时状态暂不可读取。请重新读取后再提交，当前温度与输出尚未确认。"; $("fan-policy-form").querySelectorAll("button").forEach(node => { node.disabled = true; }); Object.keys(fanNames).forEach(id => { $("fan-" + id + "-live").textContent = "实时数据暂不可读取，参考温度与输出尚未确认。"; }); }
    } finally { fanPollRunning = false; }
  }
  async function submitFanPolicy(apply, release = false) {
    if (busy || !fanSnapshot || fanSnapshot.busy) return;
    let config;
    try { config = release ? null : fanConfigFromInputs(); } catch (error) { $("fan-policy-result").textContent = error.message; return; }
    if (apply && fanSnapshot.capability.available !== true) { $("fan-policy-result").textContent = "独立控制能力未验证。可以先保存配置，但不能启用。"; return; }
    if ((apply || release) && !window.confirm(release ? "将释放 CPU 与系统风扇的独立控制，两路恢复原厂管理。确认继续？" : "将分别应用 CPU 与系统风扇的配置：\n" + Object.entries(config.channels).map(([id, value]) => fanNames[id] + "：" + fanModes[value.mode]).join("\n") + "\n自定义或手动模式会接管对应风扇；缺测、过期或过热会触发保护输出。接管失败由服务执行安全回退，请核对完成状态。确认继续？")) return;
    busy = true; renderFanPolicy(fanSnapshot); $("fan-policy-result").textContent = release ? "正在请求恢复原厂管理…" : "正在保存配置…";
    try {
      const payload = release ? { confirmed: true } : { config, apply, ...(apply ? { confirmed: true } : {}) };
      const result = await request("/api/action", { action: release ? "fan.policy.release" : "fan.policy.update", payload });
      if (result.saved === true || result.settings_saved === true || result.ok === true) fanDirty = false;
      fanAwaitingTransition = result.pending ? release ? "release" : "apply" : null;
      const snapshot = result.fan_policy || result.state && result.state.fan_policy;
      $("fan-policy-result").textContent = result.pending ? "配置已保存，正在切换控制方式。请等待实时状态确认。" : release && result.applied ? "两路已恢复原厂管理。" : apply ? result.applied ? "配置已保存，独立控制已应用。" : "配置已保存，但尚未确认应用成功。请核对实时状态。" : "配置已保存，尚未应用；当前风扇保持原控制方式。";
      if (snapshot) renderFanPolicy(snapshot, true);
    } catch (error) { $("fan-policy-result").textContent = error.name === "AbortError" ? "请求超时，结果尚未确认。请重新读取实时状态，勿连续提交。" : error.message; }
    finally { busy = false; if (fanSnapshot) renderFanPolicy(fanSnapshot); await pollFanPolicy(true); }
  }
  createFanForms();
  $("fan-policy-form").addEventListener("input", () => { fanDirty = true; updateFanModes(); $("fan-policy-save-state").textContent = "有尚未保存的修改"; });
  $("fan-policy-form").addEventListener("change", () => { fanDirty = true; updateFanModes(); });
  $("fan-policy-form").addEventListener("submit", event => { event.preventDefault(); submitFanPolicy(false); });
  $("fan-policy-apply").addEventListener("click", () => submitFanPolicy(true));
  $("fan-policy-release").addEventListener("click", () => submitFanPolicy(false, true));
  window.setInterval(() => { if (!document.hidden) pollFanPolicy(); }, 3000);
  function number(id) { return Number($(id).value); }
  async function action(name, payload, output) {
    if (busy) return;
    busy = true; message(""); output.textContent = "正在提交…";
    try {
      const result = await request("/api/action", {action:name,payload});
      const adjustsBacklight = name === "screen.brightness" || (name === "settings.update" && Object.keys(payload.settings).some((key) => key === "brightness" || key.startsWith("night_")));
      // Refresh only this section's outcome. Other unsaved forms stay untouched.
      output.textContent = result.saved === true ? (result.applied ? (adjustsBacklight ? "已保存到 NAS，本次背光设置已应用。" : "已保存到 NAS，小屏会自动读取。") : "已保存，但设备应用未完成，请在小屏检查。")
        : result.ok ? (name === "rgb.set" ? "灯光已即时应用；未保存为开机配置。" : "设备操作已应用。") : "操作未完成。";
    } catch (error) { output.textContent = error.name === "AbortError" ? "请求超时，结果尚未确认。请重新读取设置核对，勿连续提交。" : error.message; }
    finally { busy = false; if (fanSnapshot) renderFanPolicy(fanSnapshot); }
  }
  $("pair-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    const code = $("code").value.replace(/\s/g, "");
    if (!/^[0-9]{10}$/.test(code)) { message("请输入小屏显示的 10 位数字配对码。"); return; }
    busy = true;
    try { await request("/api/pair", {code}, false); $("code").value = ""; await refresh(); message("已连接。请按区域保存设置。"); }
    catch (error) { message(error.name === "AbortError" ? "连接超时，请检查 NAS 地址后重试。" : error.message); }
    finally { busy = false; if (fanSnapshot) renderFanPolicy(fanSnapshot); }
  });
  document.querySelectorAll("form[data-section]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault(); const section = form.dataset.section, output = form.querySelector(".result");
      let changes;
      if (section === "appearance") changes = {theme:$("theme").value,font_scale:number("font_scale")};
      if (section === "pages") changes = {enabled_pages:Array.from(document.querySelectorAll("#pages input:checked"), (node) => node.value),page_interval:number("page_interval")};
      if (section === "brightness") changes = {brightness:number("brightness")};
      if (section === "night") changes = {night_enabled:$("night_enabled").checked,night_start:$("night_start").value,night_end:$("night_end").value,night_brightness:number("night_brightness")};
      if (section === "sleep") changes = {sleep_timeout:number("sleep_timeout")};
      if (section === "rgb") { action("rgb.set", {mode:number("rgb-mode"),color:$("rgb-color").value,brightness:number("rgb-brightness")}, output); return; }
      action("settings.update", {settings:changes}, output);
    });
  });
  $("screen-sleep").addEventListener("click", () => action("screen.sleep", {}, $("message")));
  $("screen-wake").addEventListener("click", () => action("screen.wake", {}, $("message")));
  $("refresh").addEventListener("click", async () => {
    if (busy || !window.confirm("重新读取会覆盖本页尚未保存的输入，继续吗？")) return;
    try { await refresh(); message("已重新读取 NAS 保存的设置。"); } catch (error) { message(error.message); }
  });
  $("logout").addEventListener("click", async () => {
    if (busy) return;
    try { await request("/api/logout", {}); signedOut(); message("已退出。再次登录需要在小屏生成新的配对码。"); }
    catch (error) { message(error.message); }
  });
  refresh().catch((error) => { signedOut(); if (error.message !== "请在小屏生成新的配对码后登录") message(error.message); });
})();
