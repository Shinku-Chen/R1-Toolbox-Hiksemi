"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {overview:"概览",storage:"存储",network:"网络",controls:"控制",apps:"应用",system:"系统",settings:"设置"};
  let csrf = "", busy = false;
  Object.entries(labels).forEach(([value, text]) => {
    const label = document.createElement("label"), input = document.createElement("input");
    label.className = "check"; input.type = "checkbox"; input.value = value;
    label.append(input, document.createTextNode(text)); $("pages").append(label);
  });
  function message(text) { $("message").textContent = text; }
  function signedOut() {
    csrf = ""; $("login").hidden = false; $("editor").hidden = true; $("logout").hidden = true;
    $("code").value = "";
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
      if (!response.ok && !(value.saved === true)) throw new Error(value.error || "请求未完成，请稍后重试");
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
    csrf = value.csrf; render(value.state); $("login").hidden = true; $("editor").hidden = false; $("logout").hidden = false;
  }
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
    finally { busy = false; }
  }
  $("pair-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    const code = $("code").value.replace(/\s/g, "");
    if (!/^[0-9]{10}$/.test(code)) { message("请输入小屏显示的 10 位数字配对码。"); return; }
    busy = true;
    try { await request("/api/pair", {code}, false); $("code").value = ""; await refresh(); message("已连接。请按区域保存设置。"); }
    catch (error) { message(error.name === "AbortError" ? "连接超时，请检查 NAS 地址后重试。" : error.message); }
    finally { busy = false; }
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
