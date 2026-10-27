"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {overview:"概览",storage:"存储",network:"网络",controls:"控制",apps:"应用",system:"系统",settings:"设置"};
  let csrf = "", busy = false, rgbCapability = false, monitorOnline = document.documentElement.dataset.monitorOnline === "true", controlCaps = {};
  let lightSnapshot = null, lightDirty = false, analyticsDirty = false, lightAwaiting = false;
  let fanSnapshot = null, fanDirty = false, fanPollRunning = false, fanAwaitingTransition = null, fanReadError = null;
  const fanSourceSignatures = {};
  const fanNames = { cpu: "CPU 风扇", system: "系统风扇" };
  const fanModes = { vendor: "原厂管理", manual: "手动固定输出", temperature: "自定义温控" };
  const fanLevels = { vendor: "原厂管理", quiet: "静音档", normal: "普通档", full: "全速档", manual: "手动固定", failsafe: "保护全速" };
  Object.entries(labels).forEach(([value, text]) => {
    const label = document.createElement("label"), input = document.createElement("input");
    label.className = "check"; input.type = "checkbox"; input.value = value;
    label.append(input, document.createTextNode(text)); $("pages").append(label);
  });
  let settingsTab = "panel";
  function selectSettings(name) {
    if (!["panel","fans","lighting","monitoring","connection"].includes(name)) return;
    settingsTab = name;
    document.querySelectorAll(".settings-panel[data-settings-view]").forEach(node => { node.hidden = node.dataset.settingsView !== name; });
    document.querySelectorAll("[data-settings-tab]").forEach(node => node.setAttribute("aria-selected", String(node.dataset.settingsTab === name)));
    document.documentElement.dataset.settingsView = name;
  }
  document.querySelectorAll("[data-settings-tab]").forEach(node => node.addEventListener("click", () => selectSettings(node.dataset.settingsTab)));
  document.addEventListener("r1:settings-open", event => selectSettings(event.detail));
  selectSettings(settingsTab);
  function message(text) { $("message").textContent = text; }
  function signedOut() {
    csrf = ""; document.dispatchEvent(new CustomEvent("r1:authorized", {detail:false})); $("login").hidden = false; $("editor").hidden = true; $("logout").hidden = true;
    $("code").value = ""; fanSnapshot = null; fanDirty = false; fanAwaitingTransition = null;
    $("fan-policy-section").hidden = true;
  }
  async function request(path, body, auth = true) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), body && body.action === "container.action" ? 25000 : 18000);
    const options = {credentials:"same-origin",cache:"no-store",signal:controller.signal};
    if (body !== undefined) {
      options.method = "POST"; options.headers = {"Content-Type":"application/json"};
      if (auth) options.headers["X-R1-CSRF"] = csrf;
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options), value = await response.json();
      if (response.status === 401) signedOut();
      if ((!response.ok || value.ok === false) && !(value.saved === true || value.settings_saved === true)) throw new Error(value.reason || value.error || "请求未完成，请稍后重试");
      return value;
    } finally { clearTimeout(timeout); }
  }
  function disable(section, disabled) {
    document.querySelector(`[data-section="${section}"]`).querySelectorAll("input,select,button").forEach((node) => { node.disabled = disabled; });
  }
  function render(state) {
    const settings = state.settings || {}, caps = state.capabilities || {}; controlCaps = caps;
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
    const rgb = caps.rgb && caps.rgb.available === true; rgbCapability = rgb;
    $("rgb-card").hidden = !rgb;
    disable("rgb", !rgb); $("rgb-hint").textContent = rgb ? "灯控芯片已自动识别并验证。本次仅立即应用。" : "灯光接口尚未自动验证，相关操作暂不可用。";
    if (state.rgb && Number.isInteger(state.rgb.mode)) $("rgb-mode").value = state.rgb.mode;
    if (state.rgb && /^#[a-fA-F0-9]{6}$/.test(state.rgb.color || "")) $("rgb-color").value = state.rgb.color;
    if (state.rgb && Number.isInteger(state.rgb.brightness)) $("rgb-brightness").value = state.rgb.brightness;
    if (state.settings_error) message(state.settings_error); syncHardwareAvailability();
  }
  async function refresh() {
    const value = await request("/api/session");
    csrf = value.csrf; fanReadError = null; document.dispatchEvent(new CustomEvent("r1:authorized", {detail:true})); render(value.state); renderFanPolicy(value.fan_policy || value.state.fan_policy, true); renderOtherPolicies(value, true); $("login").hidden = true; $("editor").hidden = false; $("logout").hidden = false; selectSettings(settingsTab);
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
    const sources = (Array.isArray(snapshot.sources) ? snapshot.sources : []).map(source => fanReadError ? {...source,temperature_c:null,status:"unavailable",fresh:false,age_seconds:null} : source), capability = fanReadError ? {available:false,reason:fanReadError} : snapshot.capability || {};
    Object.keys(fanNames).forEach(channel => {
      const config = snapshot.config.channels[channel], runtime = snapshot.channels && snapshot.channels[channel] || {};
      if (force || !fanDirty) ["mode", "quiet_below", "normal_at", "full_at", "quiet_percent", "normal_percent", "manual_percent"].forEach(key => { if (config[key] !== undefined) $("fan-" + channel + "-" + key).value = config[key]; });
      renderFanSources(channel, config, sources, force);
      const source = sources.find(item => item.id === runtime.source_id);
      const reference = typeof runtime.reference_c === "number" ? runtime.reference_c + " °C" : "-- °C";
      const target = typeof runtime.target_percent === "number" ? "请求输出 " + runtime.target_percent + "%" : "输出未提供";
      const rpm = typeof runtime.rpm === "number" ? runtime.rpm + " RPM" : "RPM 未知";
      $("fan-" + channel + "-live").textContent = fanReadError ? "设置状态未确认，当前参考温度与请求输出暂不可读取。" : (runtime.active ? "运行中 · " : "当前 · ") + fanRuntimeLabel(runtime) + "\n参考 max：" + reference + "\n来源：" + (runtime.source_label || "未提供") + " · " + fanFreshness(source) + "\n" + target + " · " + rpm + (runtime.reason ? "\n" + runtime.reason : "");
    });
    const state = snapshot.busy && !fanReadError ? "正在切换控制方式" : fanReadError ? "设置状态未确认" : snapshot.last_error ? "控制异常 · 请核对恢复状态" : fanDirty ? "有尚未保存的修改" : snapshot.pending_changes ? "已保存 · 有待生效的配置" : snapshot.active ? "两路分别执行中" : "当前由原厂管理";
    $("fan-policy-save-state").textContent = state;
    $("fan-policy-capability").textContent = capability.available ? "独立控制接口已通过检查。" : "独立控制前置检查未通过；仍可编辑并保存配置。";
    $("fan-policy-runtime-note").textContent = snapshot.last_error || snapshot.settings_error || (snapshot.active_on_boot ? "已启用开机恢复；每次恢复仍须通过硬件与温度源检查。" : "开机接管尚未启用。纯保存只保存草稿，不改变当前控制方式。");
    const locked = busy || snapshot.busy === true;
    $("fan-policy-form").querySelectorAll("input,select,button").forEach(node => { node.disabled = locked; });
    $("fan-policy-apply").disabled = locked || !monitorOnline || capability.available !== true;
    $("fan-policy-apply-reason").textContent = snapshot.busy && !fanReadError ? "正在切换控制方式，请等待完成后再应用。"
      : busy ? "正在提交操作，请等待完成后再应用。"
      : !monitorOnline ? "监控连接已中断，暂不能应用；连接恢复后会重新检查。"
      : capability.available !== true ? "暂不能应用：" + (capability.reason || "独立控制能力尚未验证")
      : "可应用；保存并应用后需确认接管，仅保存配置不会改变当前控制。";
    $("fan-policy-release").disabled = locked || !monitorOnline || !(snapshot.active || snapshot.active_on_boot || snapshot.last_error || Object.values(snapshot.config.channels).some(item => item.mode !== "vendor"));
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
      const value = await request("/api/session"); csrf = value.csrf; fanReadError = null;
      renderFanPolicy(value.fan_policy || value.state.fan_policy, force); renderOtherPolicies(value, false);
    } catch (_) {
      fanReadError = "设置状态未确认：实时风扇状态暂不可读取，请恢复连接后重试。";
      if (!$("editor").hidden) { $("fan-policy-capability").textContent = "实时状态暂不可读取。请重新读取后再提交，当前温度与输出尚未确认。"; $("fan-policy-apply-reason").textContent = "暂不能应用：" + fanReadError; $("fan-policy-form").querySelectorAll("button").forEach(node => { node.disabled = true; }); Object.keys(fanNames).forEach(id => { $("fan-" + id + "-live").textContent = "设置状态未确认，当前参考温度与请求输出暂不可读取。"; }); }
    } finally { fanPollRunning = false; }
  }
  async function submitFanPolicy(apply, release = false) {
    if (busy || !fanSnapshot || fanSnapshot.busy) return;
    if ((apply || release) && !monitorOnline) { $("fan-policy-result").textContent="实时连接未确认，未提交设备操作；仍可保存配置草稿。"; return; }
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
    const hardware = name !== "settings.update" || Object.keys(payload.settings || {}).some(key => key === "brightness" || key.startsWith("night_"));
    if (hardware && !monitorOnline) { output.textContent="实时连接未确认，设备操作未提交。"; return; }
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
    if (!/^[0-9]{5}$/.test(code)) { message("请输入小屏显示的 5 位数字配对码。"); return; }
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

  const monitorThresholds = [["cpu","CPU 使用率", "%"],["memory","内存使用率","%"],["cpu_temp","CPU 温度","°C"],["disk_temp","硬盘温度","°C"],["capacity","空间使用率","%"]];
  monitorThresholds.forEach(([key,label,unit]) => {
    const box=fanElement("fieldset",""); box.append(fanElement("legend","",label));
    for(const [level,name] of [["warn","提醒"],["critical","严重"]]) { const row=fanElement("label","",name+"（"+unit+"）"), input=document.createElement("input");input.type="number";input.min="0";input.max="150";input.id="monitor-"+key+"-"+level;input.required=true;row.append(input);box.append(row); } $("monitor-thresholds").append(box);
  });
  function lightModes() { const temperature=$("light-mode").value==="temperature";$("light-manual-fields").hidden=temperature;$("light-temperature-fields").hidden=!temperature; }
  function addLightStop(value={temperature:70,color:"#e0a170"}) { if($("light-stops").children.length>=8)return;const row=fanElement("div","light-stop"),label=fanElement("label","","温度 °C"),temp=document.createElement("input"),colorLabel=fanElement("label","","颜色"),color=document.createElement("input"),remove=fanElement("button","quiet-button","移除");temp.type="number";temp.min="-40";temp.max="150";temp.required=true;temp.value=value.temperature;temp.dataset.stopTemperature="true";color.type="color";color.value=value.color;color.dataset.stopColor="true";remove.type="button";remove.addEventListener("click",()=>{if($("light-stops").children.length<=2){$("light-policy-result").textContent="至少保留 2 个颜色节点。";return;}row.remove();lightDirty=true;});label.append(temp);colorLabel.append(color);row.append(label,colorLabel,remove);$("light-stops").append(row); }
  function renderOtherPolicies(value,force=false) {
    const light=value.light_policy;
    if(light && light.config) {
      lightSnapshot=light;$("light-policy-section").hidden=false;$("rgb-card").hidden=!rgbCapability||light.active||light.busy;const c=light.config;
      if(force||!lightDirty) {
        $("light-enabled").checked=c.enabled;$("light-mode").value=c.mode;
        $("light-brightness").value=c.brightness;$("light-manual-mode").value=c.manual.mode;$("light-manual-color").value=c.manual.color;$("light-manual-brightness").value=c.manual.brightness;
        $("light-night-enabled").checked=c.schedule.enabled;$("light-night-start").value=c.schedule.start;$("light-night-end").value=c.schedule.end;$("light-night-brightness").value=c.schedule.brightness;
        $("light-stops").replaceChildren();(c.stops||[]).forEach(addLightStop);
        const box=$("light-sources");box.querySelectorAll("label").forEach(n=>n.remove());const sources=new Map((light.sources||[]).map(s=>[s.id,s]));c.sources.forEach(id=>{if(!sources.has(id))sources.set(id,{id,label:"已保存来源暂缺 · "+id});});sources.forEach(source=>{const label=fanElement("label","fan-source"),input=document.createElement("input");input.type="checkbox";input.value=source.id;input.checked=c.sources.includes(source.id);label.append(input,fanElement("span","",source.label||source.id));box.append(label);});
      }
      const target=light.target||{};$("light-policy-state").textContent=light.busy?"正在切换":light.last_error?"应用异常":light.pending_changes?"有待生效配置":light.active?"运行中":"未接管";
      $("light-policy-live").textContent=(typeof light.reference_c==="number"?"参考 max "+light.reference_c+" °C · "+(light.source_label||"--")+"\n":"")+(light.reason||"尚未取得运行状态")+(target.color?" · 目标颜色 "+target.color:"")+(Number.isInteger(target.brightness)?" · 目标亮度 "+target.brightness+"%":"")+(light.schedule_active?" · 夜间上限生效":"");
      $("light-policy-capability").textContent=light.last_error||light.settings_error||(light.capability && light.capability.available?"灯控设备已自动识别。保存与应用分开；应用成功后服务持续管理灯光。":"尚未验证唯一灯控设备，仍可保存草稿，不能启用。 "+(light.capability && light.capability.reason||""));
      $("light-policy-form").querySelectorAll("input,select,button").forEach(n=>n.disabled=busy||light.busy===true);$("light-policy-apply").disabled=busy||!monitorOnline||light.busy===true||!(light.capability && light.capability.available);
      if(light.busy)$("light-policy-result").textContent="配置已保存，正在切换灯光控制。请等待运行状态确认。";
      else if(lightAwaiting){$("light-policy-result").textContent=light.last_error?"已保存，但应用未完成："+light.last_error:light.applied?"灯光策略切换完成，请核对当前状态。":"已保存，应用结果尚未确认，请核对运行状态。";lightAwaiting=false;}
      lightModes();
    }
    const settings=value.analytics_settings || value.analytics && value.analytics.settings;
    if(settings) { $("analytics-settings-section").hidden=false;if(force||!analyticsDirty){$("history-enabled").checked=settings.history_enabled;$("history-retention").value=settings.retention_days;$("traffic-exclude-virtual").checked=settings.traffic_exclude_virtual;monitorThresholds.forEach(([key])=>["warn","critical"].forEach(level=>{$("monitor-"+key+"-"+level).value=settings.thresholds && settings.thresholds[key+"_"+level];}));} }
  }
  function lightConfig() { const n=id=>Number($(id).value), sources=Array.from($("light-sources").querySelectorAll("input:checked"),n=>n.value), stops=Array.from($("light-stops").children,n=>({temperature:Number(n.querySelector("[data-stop-temperature]").value),color:n.querySelector("[data-stop-color]").value}));if(stops.length<2||stops.length>8||stops.some((n,i)=>!Number.isInteger(n.temperature)||n.temperature< -40||n.temperature>150||i&&n.temperature<=stops[i-1].temperature))throw new Error("请填写 2–8 个温度递增的颜色节点。");if($("light-enabled").checked&&$("light-mode").value==="temperature"&&!sources.length)throw new Error("温度联动至少选择一个来源。");return{schema_version:1,enabled:$("light-enabled").checked,mode:$("light-mode").value,sources,stops,brightness:n("light-brightness"),manual:{mode:n("light-manual-mode"),color:$("light-manual-color").value,brightness:n("light-manual-brightness")},schedule:{enabled:$("light-night-enabled").checked,start:$("light-night-start").value,end:$("light-night-end").value,brightness:n("light-night-brightness")}}; }
  async function saveLight(apply) { if(busy||!lightSnapshot||lightSnapshot.busy)return;if(apply&&!monitorOnline){$("light-policy-result").textContent="实时连接未确认，未提交灯光操作；仍可保存配置草稿。";return;}let config;try{config=lightConfig();}catch(error){$("light-policy-result").textContent=error.message;return;}if(apply&&!window.confirm(config.enabled?"将持续应用当前灯光策略，并在以后启动时恢复。温度联动取所选来源最高温；缺测使用低亮警示色。确认继续？":"将关闭持续灯光策略并释放接管。确认继续？"))return;busy=true;$("light-policy-result").textContent="正在保存灯光配置…";try{const result=await request("/api/action",{action:"light.policy.update",payload:{config,apply}});if(result.ok||result.saved)lightDirty=false;lightAwaiting=result.pending===true;$("light-policy-result").textContent=result.pending?"灯光配置已保存，正在切换，请核对实时状态。":apply?(result.applied?"灯光配置已保存并应用。":"已保存，应用结果尚未确认。"):"灯光配置已保存，尚未应用。";if(result.light_policy)renderOtherPolicies({light_policy:result.light_policy},true);}catch(error){$("light-policy-result").textContent=error.message;}finally{busy=false;await pollFanPolicy();} }
  $("light-policy-form").addEventListener("input",()=>{lightDirty=true;lightModes();});$("light-policy-form").addEventListener("change",()=>{lightDirty=true;lightModes();});$("light-policy-form").addEventListener("submit",event=>{event.preventDefault();saveLight(false);});$("light-policy-apply").addEventListener("click",()=>saveLight(true));$("light-add-stop").addEventListener("click",()=>{addLightStop();lightDirty=true;});
  $("analytics-form").addEventListener("input",()=>{analyticsDirty=true;});$("analytics-form").addEventListener("submit",async event=>{event.preventDefault();if(busy)return;const settings={history_enabled:$("history-enabled").checked,retention_days:Number($("history-retention").value),traffic_exclude_virtual:$("traffic-exclude-virtual").checked,thresholds:{}};for(const[key]of monitorThresholds){const warn=Number($("monitor-"+key+"-warn").value),critical=Number($("monitor-"+key+"-critical").value);if(!(warn<critical)){$("analytics-result").textContent="各项提醒阈值必须小于严重阈值。";return;}settings.thresholds[key+"_warn"]=warn;settings.thresholds[key+"_critical"]=critical;}busy=true;$("analytics-result").textContent="正在保存…";try{const result=await request("/api/action",{action:"monitor.settings.update",payload:{settings}});if(result.ok||result.saved){analyticsDirty=false;$("analytics-result").textContent="统计与提醒设置已保存。";}}catch(error){$("analytics-result").textContent=error.message;}finally{busy=false;}});
  function syncHardwareAvailability() {
    const brightness=controlCaps.brightness && controlCaps.brightness.available===true;
    const screen=controlCaps.screen && controlCaps.screen.available===true;
    disable("brightness", !monitorOnline || !brightness); disable("night", !monitorOnline || !brightness);
    disable("rgb", !monitorOnline || !rgbCapability || Boolean(lightSnapshot && (lightSnapshot.active || lightSnapshot.busy)));
    $("screen-sleep").disabled=!monitorOnline||!screen;$("screen-wake").disabled=!monitorOnline||!screen;
    if(fanSnapshot)renderFanPolicy(fanSnapshot);
    if(lightSnapshot)$("light-policy-apply").disabled=!monitorOnline||busy||lightSnapshot.busy||!(lightSnapshot.capability && lightSnapshot.capability.available);
  }
  document.addEventListener("r1:monitor-online", event => {monitorOnline=event.detail===true;syncHardwareAvailability();});
  document.addEventListener("r1:web-action", async event => {
    const command = event.detail || {};
    if (!csrf) { command.reject && command.reject(new Error("请先使用小屏配对码解锁设置。")); return; }
    if (busy) { command.reject && command.reject(new Error("已有操作正在进行，请稍候。")); return; }
    busy = true;
    try { command.resolve(await request("/api/action", {action:command.action,payload:command.payload || {}})); }
    catch (error) { command.reject(error); }
    finally { busy = false; }
  });
  refresh().catch((error) => { signedOut(); if (error.message !== "请在小屏生成新的配对码后登录") message(error.message); });
})();
