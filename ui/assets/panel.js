"use strict";

(() => {
  const API = "http://127.0.0.1:18731/api";
  const POLL_INTERVAL = 2000;
  const REQUEST_TIMEOUT = 3500;
  const pages = ["overview", "storage", "network", "controls", "apps", "system", "settings"];
  const pageLabels = { overview: "概览", storage: "存储", network: "网络", controls: "控制", apps: "应用", system: "系统", settings: "设置" };
  const $ = (id) => document.getElementById(id);
  const controlToken = (() => {
    try {
      if (typeof window.require !== "function") return "";
      const value = window.require("process").env.R1_TOOLBOX_CONTROL_TOKEN;
      return typeof value === "string" ? value : "";
    } catch (_) { return ""; }
  })();
  const fieldNodes = new Map();
  document.querySelectorAll("[data-field]").forEach((node) => {
    const name = node.dataset.field;
    if (!fieldNodes.has(name)) fieldNodes.set(name, []);
    fieldNodes.get(name).push(node);
  });

  let currentPage = 0;
  let requestRunning = false;
  let pointerStart = null;
  let diskSignature = null;
  let toolbox = null;
  let insights = null;
  let toolboxOnline = false;
  let actionBusy = false;
  let settingsDirty = false;
  let brightnessDirty = false;
  let rgbDirty = false;
  let rgbBusesSignature = null;
  let fanSignature = null;
  let appsSignature = null;
  let enabledPages = pages.slice();
  let settings = {};
  let lastInteraction = Date.now();
  let lastIdleWake = 0;
  let lastCarousel = Date.now();
  let lastInsightsPoll = 0;
  let sleeping = false;
  let waking = false;
  let swallowUntil = 0;
  let toastTimer = null;
  let resolveConfirmation = null;
  let powerDeadline = null;
  let powerOperation = null;

  function field(name, value) {
    (fieldNodes.get(name) || []).forEach((node) => { node.textContent = value; });
  }

  function number(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  }

  function percent(value) {
    const result = number(value);
    return result !== null && result <= 100 ? result : null;
  }

  function percentage(value) {
    const result = percent(value);
    return result === null ? "--" : Math.round(result).toString();
  }

  function bytes(value, decimals = 1) {
    const result = number(value);
    if (result === null) return "--";
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    const power = result === 0 ? 0 : Math.max(0, Math.min(Math.floor(Math.log(result) / Math.log(1024)), units.length - 1));
    const amount = result / (1024 ** power);
    return `${amount.toFixed(power === 0 ? 0 : decimals)} ${units[power]}`;
  }

  function speed(value) {
    const result = number(value);
    if (result === null) return { value: "--", unit: "/s" };
    const units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"];
    const power = result === 0 ? 0 : Math.max(0, Math.min(Math.floor(Math.log(result) / Math.log(1024)), units.length - 1));
    const amount = result / (1024 ** power);
    return { value: amount.toFixed(power === 0 || amount >= 100 ? 0 : 1), unit: units[power] };
  }

  function uptime(value) {
    const result = number(value);
    if (result === null) return "--";
    const minutes = Math.floor(result / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    if (days > 0) return `${days} 天 ${hours % 24} 小时`;
    if (hours > 0) return `${hours} 小时 ${minutes % 60} 分钟`;
    return `${minutes} 分钟`;
  }

  function meter(id, value) {
    const bar = document.getElementById(id);
    const result = percent(value);
    bar.style.width = `${result === null ? 0 : result}%`;
    bar.parentElement.setAttribute("aria-label", `${bar.parentElement.getAttribute("aria-label").split("：")[0]}：${result === null ? "数据暂不可用" : `${Math.round(result)}%`}`);
  }

  function updateClock() {
    const now = new Date();
    document.getElementById("clock").textContent = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    document.getElementById("seconds").textContent = String(now.getSeconds()).padStart(2, "0");
    const weekdays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
    document.getElementById("date").textContent = `${now.getMonth() + 1} 月 ${now.getDate()} 日 · ${weekdays[now.getDay()]}`;
  }

  function setConnection(state, timestamp) {
    document.getElementById("connection").dataset.state = state;
    document.getElementById("connection-label").textContent = state === "online" ? "实时监测" : state === "connecting" ? "连接中" : "数据暂不可用";
    const updated = typeof timestamp === "string" ? new Date(timestamp) : null;
    document.getElementById("freshness-label").textContent = state === "online"
      ? updated && Number.isFinite(updated.getTime()) ? `更新于 ${updated.toLocaleTimeString("zh-CN", { hour12: false })}` : "已连接 · 每 2 秒读取"
      : state === "connecting" ? "正在连接本机只读服务" : "未连接 · 正在自动重试";
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderDisks(disks, connected) {
    const signature = JSON.stringify([connected, disks]);
    if (signature === diskSignature) return;
    diskSignature = signature;
    const list = document.getElementById("disk-list");
    const previousScroll = list.scrollTop;
    list.replaceChildren();
    if (!disks.length) {
      const empty = element("div", "empty-state");
      empty.append(element("p", "", connected ? "暂未发现数据盘" : "数据暂不可用"));
      empty.append(element("span", "", connected ? "这里显示宿主机的数据盘挂载" : "连接后显示真实挂载信息"));
      list.append(empty);
      return;
    }
    disks.forEach((disk, index) => {
      const card = element("article", "card disk-card");
      const heading = element("div", "disk-card-heading");
      const parts = disk.mount.split("/").filter(Boolean);
      heading.append(element("h3", "disk-name", parts[parts.length - 1] || `数据盘 ${index + 1}`));
      heading.append(element("span", "disk-percent", `${percentage(disk.usage_percent)}%`));
      const track = element("div", "meter meter-neutral");
      const fill = element("span", "");
      const usedPercent = percent(disk.usage_percent);
      fill.style.width = `${usedPercent === null ? 0 : usedPercent}%`;
      track.append(fill);
      card.append(heading, track);
      card.append(element("div", "disk-capacity", `${bytes(disk.used_bytes)} / ${bytes(disk.total_bytes)}`));
      card.append(element("div", "disk-mount", disk.mount));
      list.append(card);
    });
    list.scrollTop = previousScroll;
  }

  function render(data) {
    const connected = data !== null;
    const cpu = data && data.cpu && typeof data.cpu === "object" ? data.cpu : {};
    const memory = data && data.memory && typeof data.memory === "object" ? data.memory : {};
    const network = data && data.network && typeof data.network === "object" ? data.network : {};
    const disks = data && Array.isArray(data.disks) ? data.disks.filter((disk) => disk && typeof disk.mount === "string") : [];
    field("hostname", data && typeof data.hostname === "string" && data.hostname ? data.hostname : "本机面板");
    field("cpu", percentage(cpu.usage_percent));
    field("cpu-status", connected ? percent(cpu.usage_percent) === null ? "采集中" : "使用率" : "等待读取");
    const temp = number(cpu.temp_c);
    field("temperature", temp === null ? "--" : temp.toFixed(0));
    field("memory", percentage(memory.usage_percent));
    field("memory-usage", number(memory.used_bytes) !== null && number(memory.total_bytes) !== null ? `${bytes(memory.used_bytes)} / ${bytes(memory.total_bytes)}` : "等待读取");
    meter("cpu-meter", cpu.usage_percent);
    meter("memory-meter", memory.usage_percent);
    const rx = speed(network.rx_bytes_per_sec);
    const tx = speed(network.tx_bytes_per_sec);
    field("rx", rx.value);
    field("rx-unit", rx.unit);
    field("tx", tx.value);
    field("tx-unit", tx.unit);
    field("interface", typeof network.interface === "string" && network.interface ? network.interface : "--");
    field("ipv4", typeof network.ipv4 === "string" && network.ipv4 ? network.ipv4 : "--");
    field("network-status", !connected ? "等待连接本机只读服务" : number(network.rx_bytes_per_sec) === null || number(network.tx_bytes_per_sec) === null ? "流量采集中 · 首次采样后显示速率" : "当前接口流量 · 每 2 秒读取");
    field("uptime", uptime(data && data.uptime_seconds));
    field("disk-count", connected ? `${disks.length} 个挂载` : "--");
    const completeDisks = disks.length > 0 && disks.every((disk) => number(disk.used_bytes) !== null && number(disk.total_bytes) !== null);
    const total = completeDisks ? disks.reduce((sum, disk) => sum + disk.total_bytes, 0) : null;
    const used = completeDisks ? disks.reduce((sum, disk) => sum + disk.used_bytes, 0) : null;
    const usedPercent = total > 0 ? 100 * used / total : null;
    field("disk-used", bytes(used));
    field("disk-total", total !== null ? `/ ${bytes(total)}` : connected && disks.length === 0 ? "暂未发现数据盘" : "等待读取容量");
    meter("storage-meter", usedPercent);
    meter("storage-detail-meter", usedPercent);
    renderDisks(disks, connected);
  }

  function text(id, value) { $(id).textContent = value === null || value === undefined || value === "" ? "--" : String(value); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function cap(name) {
    const value = toolbox && toolbox.capabilities && toolbox.capabilities[name];
    return value && typeof value === "object" ? value : { available: false, reason: "宿主尚未提供此控制能力" };
  }
  function canWrite() { return Boolean(controlToken && toolboxOnline && toolbox); }
  function errorMessage(error) {
    if (error && error.name === "AbortError") return "请求超时，请稍后重试";
    return error && typeof error.message === "string" ? error.message : "连接失败，请稍后重试";
  }
  function toast(message, error = false) {
    window.clearTimeout(toastTimer);
    text("toast", message);
    $("toast").dataset.error = String(error);
    $("toast").hidden = false;
    toastTimer = window.setTimeout(() => { $("toast").hidden = true; }, error ? 6500 : 3500);
  }
  async function request(path, payload) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), payload ? 10000 : REQUEST_TIMEOUT);
    try {
      const options = { method: payload ? "POST" : "GET", cache: "no-store", credentials: "omit", signal: controller.signal };
      if (payload) {
        options.headers = { "Content-Type": "application/json", "X-R1-Token": controlToken };
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(API + path, options);
      let data;
      try { data = await response.json(); } catch (_) { throw new Error("本机服务返回了无法读取的数据"); }
      if (!response.ok || data.ok === false) throw new Error(typeof data.reason === "string" ? data.reason : typeof data.error === "string" ? data.error : `本机服务请求失败（${response.status}）`);
      return data;
    } finally { window.clearTimeout(timeout); }
  }
  function setAllowed(id, allowed) { $(id).dataset.allowed = String(Boolean(allowed)); }
  function syncControls() {
    const writable = canWrite() && !actionBusy;
    const screenAvailable = cap("screen").available === true;
    const rgbAvailable = cap("rgb").available === true || Boolean(toolbox && toolbox.rgb && list(toolbox.rgb.availableBuses).length);
    const powerAvailable = cap("power").available === true;
    const pending = Boolean(toolbox && toolbox.power && toolbox.power.pending);
    const executing = Boolean(toolbox && toolbox.power && toolbox.power.executing);
    setAllowed("brightness-save", screenAvailable);
    setAllowed("screen-sleep", screenAvailable && !pending);
    setAllowed("rgb-save", rgbAvailable);
    setAllowed("power-reboot", powerAvailable && !pending && !executing);
    setAllowed("power-off", powerAvailable && !pending && !executing);
    document.querySelectorAll(".requires-write").forEach((node) => {
      node.disabled = !writable || node.dataset.allowed === "false";
    });
    $("brightness").disabled = !writable || !screenAvailable;
    $("rgb-color").disabled = $("rgb-mode").disabled = $("rgb-bus").disabled = !writable || !rgbAvailable;
    document.querySelectorAll("#settings-form input, #settings-form select").forEach((node) => { node.disabled = !writable; });
    document.querySelectorAll(".fan-input").forEach((node) => { node.disabled = !writable || !cap("fans").available; });
    $("power-cancel").disabled = !writable || !pending || executing;
    const notice = !controlToken ? "只读预览 · 控制仅限本机小屏授权会话" : !toolboxOnline ? "服务未连接 · 所有设备操作已禁用" : actionBusy ? "正在执行操作，请稍候" : "本机授权会话 · 所有高影响操作均需确认";
    text("control-access", notice);
    text("settings-access", !controlToken ? "只读预览 · 请在 NAS 小屏修改设置" : !toolboxOnline ? "服务未连接 · 暂时无法保存设置" : "设置会保存并立即生效");
  }
  async function action(name, payload = {}, successMessage = "操作已完成", options = {}) {
    if (!canWrite()) { if (!options.quiet) toast("当前为只读或离线状态，无法执行操作", true); return false; }
    if (actionBusy) return false;
    actionBusy = true;
    syncControls();
    try {
      const result = await request("/action", Object.assign({ action: name }, payload));
      if (result.state) renderToolbox(result.state);
      if (result.insights) { insights = result.insights; renderInsights(insights); }
      if (successMessage && !options.quiet) toast(successMessage);
      return true;
    } catch (error) {
      if (error && (error.name === "AbortError" || error instanceof TypeError)) {
        toolboxOnline = false;
        renderToolbox(null);
      }
      if (!options.quiet) toast(errorMessage(error), true);
      return false;
    } finally { actionBusy = false; syncControls(); }
  }
  function confirmOperation(title, message, accept = "确认操作", danger = false) {
    if (resolveConfirmation) resolveConfirmation(false);
    text("confirm-title", title);
    text("confirm-message", message);
    text("confirm-accept", accept);
    $("confirm-accept").classList.toggle("button-danger", danger);
    $("confirm-backdrop").hidden = false;
    const previousFocus = document.activeElement;
    $("confirm-cancel").focus();
    return new Promise((resolve) => {
      resolveConfirmation = (confirmed) => {
        $("confirm-backdrop").hidden = true;
        resolveConfirmation = null;
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        resolve(confirmed);
      };
    });
  }
  $("confirm-cancel").addEventListener("click", () => { if (resolveConfirmation) resolveConfirmation(false); });
  $("confirm-accept").addEventListener("click", () => { if (resolveConfirmation) resolveConfirmation(true); });
  $("confirm-backdrop").addEventListener("click", (event) => { if (event.target === $("confirm-backdrop") && resolveConfirmation) resolveConfirmation(false); });

  function applySettings(value) {
    settings = value && typeof value === "object" ? value : {};
    const themes = ["dark", "light", "ink", "jade"];
    document.documentElement.dataset.theme = themes.includes(settings.theme) ? settings.theme : "dark";
    const scale = number(settings.font_scale);
    document.documentElement.style.setProperty("--ui-scale", String(scale !== null && scale >= 80 && scale <= 140 ? scale / 100 : 1));
    const configured = list(settings.enabled_pages).filter((name) => pages.includes(name));
    enabledPages = configured.length ? configured : pages.slice();
    // Settings remains reachable even if every business page is hidden.
    document.querySelectorAll("[data-page]").forEach((button) => { button.hidden = !enabledPages.includes(button.dataset.page) && button.dataset.page !== "settings"; });
    if (!enabledPages.includes(pages[currentPage]) && pages[currentPage] !== "settings") showPage(enabledPages[0]);
    if (settingsDirty) return;
    $("setting-theme").value = themes.includes(settings.theme) ? settings.theme : "dark";
    $("setting-font").value = scale !== null ? scale : 100;
    text("font-value", $("setting-font").value + "%");
    $("setting-carousel").value = number(settings.page_interval) || 0;
    $("setting-sleep").value = number(settings.sleep_timeout) || 0;
    $("setting-night").checked = settings.night_enabled === true;
    $("setting-night-start").value = settings.night_start || "22:00";
    $("setting-night-end").value = settings.night_end || "07:00";
    $("setting-night-brightness").value = number(settings.night_brightness) || 20;
    text("night-brightness-value", $("setting-night-brightness").value + "%");
    document.querySelectorAll("[data-page-setting]").forEach((input) => { input.checked = enabledPages.includes(input.dataset.pageSetting); });
  }
  pages.forEach((name) => {
    const row = element("label", "check-row");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.pageSetting = name;
    input.checked = true;
    input.disabled = true;
    row.append(input, element("span", "", pageLabels[name]));
    $("page-checks").append(row);
  });
  $("settings-form").addEventListener("input", () => { settingsDirty = true; text("settings-result", "尚未保存"); });
  $("setting-font").addEventListener("input", () => text("font-value", $("setting-font").value + "%"));
  $("setting-night-brightness").addEventListener("input", () => text("night-brightness-value", $("setting-night-brightness").value + "%"));
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const chosen = Array.from(document.querySelectorAll("[data-page-setting]:checked")).map((node) => node.dataset.pageSetting);
    if (!chosen.length) { toast("请至少启用一个页面", true); return; }
    const pageInterval = Number($("setting-carousel").value);
    const sleepTimeout = Number($("setting-sleep").value);
    if (!Number.isInteger(pageInterval) || pageInterval < 0 || pageInterval > 3600 || !Number.isInteger(sleepTimeout) || sleepTimeout < 0 || sleepTimeout > 86400) {
      toast("轮播须为 0–3600 秒，息屏须为 0–86400 秒", true); return;
    }
    const next = { theme: $("setting-theme").value, font_scale: Number($("setting-font").value), page_interval: pageInterval,
      sleep_timeout: sleepTimeout, night_enabled: $("setting-night").checked, night_start: $("setting-night-start").value,
      night_end: $("setting-night-end").value, night_brightness: Number($("setting-night-brightness").value), enabled_pages: chosen };
    if (!next.night_start || !next.night_end) { toast("请设置完整的夜间起止时间", true); return; }
    if (await action("settings.update", { settings: next }, "设置已保存")) {
      settingsDirty = false;
      applySettings(toolbox && toolbox.settings);
      lastCarousel = Date.now();
      text("settings-result", "已保存 · 以宿主服务返回配置为准");
    }
  });

  function renderFans(data) {
    const channels = list(data && data.channels);
    const signature = JSON.stringify([cap("fans"), data && data.last_error, channels.map((item) => [item.id, item.driver, item.owned, item.mode])]);
    if (signature === fanSignature) {
      channels.forEach((channel) => {
        const value = document.querySelector('[data-fan-duty="' + channel.id + '"]');
        if (value) value.textContent = percentage(channel.duty_percent) + "%";
      });
      return;
    }
    fanSignature = signature;
    clear($("fan-controls"));
    if (data && data.last_error) $("fan-controls").append(element("p", "capability-note fan-error", data.last_error));
    if (!cap("fans").available || !channels.length) {
      $("fan-controls").append(element("p", "capability-note", cap("fans").reason || "宿主未提供可控 PWM 通道；不会模拟风扇控制"));
      return;
    }
    channels.forEach((channel) => {
      const card = element("div", "fan-channel");
      const heading = element("div", "fan-heading");
      const duty = element("span", "fan-rpm", percentage(channel.duty_percent) + "%"); duty.dataset.fanDuty = channel.id;
      heading.append(element("strong", "", channel.driver || channel.id), duty);
      const rpm = channel.rpm_verified && number(channel.rpm) !== null ? " · " + channel.rpm + " RPM" : " · 转速传感器未确认";
      const note = element("p", "capability-note", channel.id + (channel.owned ? " · 已由 Toolbox 接管" : " · 尚未接管") + rpm);
      if (channel.rpm_reason) note.append(document.createTextNode(" · " + channel.rpm_reason));
      const consent = element("label", "check-row");
      const takeover = document.createElement("input");
      takeover.type = "checkbox";
      takeover.className = "fan-input";
      takeover.checked = channel.owned === true;
      consent.append(takeover, element("span", "", "允许 Toolbox 临时接管此风扇"));
      const output = element("output", "", (percent(channel.duty_percent) === null ? 60 : Math.max(50, Math.round(channel.duty_percent))) + "%");
      const label = element("label", "control-label", "自定义 PWM 占空比");
      label.append(output);
      const slider = document.createElement("input");
      slider.type = "range"; slider.min = "50"; slider.max = "100"; slider.step = "5";
      slider.value = percent(channel.duty_percent) === null ? "60" : String(Math.max(50, Math.round(channel.duty_percent)));
      slider.className = "touch-range fan-input";
      slider.addEventListener("input", () => { output.textContent = slider.value + "%"; });
      const buttons = element("div", "item-actions");
      [["低速", "low"], ["均衡", "medium"], ["高速", "high"], ["应用自定义", "manual"], ["自动温控", "auto"], ["恢复原控制", "restore"]].forEach(([title, mode]) => {
        const button = element("button", "button button-secondary requires-write", title);
        button.type = "button";
        button.dataset.allowed = String(mode !== "restore" || channel.owned === true);
        button.addEventListener("click", async () => {
          if (!takeover.checked && mode !== "restore") { toast("请先勾选允许接管风扇", true); return; }
          const message = mode === "restore" ? "将恢复 " + channel.id + " 接管前的控制模式和 PWM 设置。" : mode === "auto" ? "Toolbox 将按 CPU 温度调整此通道；温度不可读时使用满速。确认允许接管？" : "将调整 " + channel.id + "。确认允许 Toolbox 接管该通道？";
          if (!(await confirmOperation(title, message, "确认调整"))) return;
          if (mode === "restore") await action("fan.restore", { channel: channel.id }, "已请求恢复原风扇控制");
          else await action("fan.set", { channel: channel.id, mode, value: Number(slider.value), takeover: true }, "风扇控制已提交");
        });
        buttons.append(button);
      });
      card.append(heading, note, consent, label, slider, buttons);
      $("fan-controls").append(card);
    });
  }
  function renderPower(power) {
    const pending = power && power.pending;
    powerOperation = pending && pending.operation;
    powerDeadline = pending && number(pending.seconds_remaining) !== null ? Date.now() + pending.seconds_remaining * 1000 : null;
    $("power-pending").hidden = !pending && !(power && power.executing);
    if (power && power.executing) text("power-countdown", "电源操作已交给宿主系统");
    else updatePowerCountdown();
    text("power-reason", power && power.last_error ? power.last_error : cap("power").available ? "确认后有 5 秒可取消倒计时" : cap("power").reason);
  }
  function updatePowerCountdown() {
    if (powerDeadline === null) return;
    const seconds = Math.max(0, Math.ceil((powerDeadline - Date.now()) / 1000));
    text("power-countdown", (powerOperation === "poweroff" ? "关闭 NAS" : "重启 NAS") + (seconds ? ` · ${seconds} 秒后执行` : " · 等待宿主确认"));
  }
  function setSleeping(value) {
    sleeping = value === true;
    $("sleep-overlay").hidden = !sleeping;
    if (sleeping && resolveConfirmation) resolveConfirmation(false);
  }
  function renderToolbox(data) {
    toolbox = data && typeof data === "object" ? data : null;
    toolboxOnline = Boolean(toolbox);
    if (!toolbox) {
      fanSignature = null;
      clear($("fan-controls"));
      $("fan-controls").append(element("p", "capability-note", "服务未连接，风扇状态不可用"));
      text("screen-state", "离线"); text("screen-reason", "服务未连接，亮度与息屏操作已禁用");
      text("rgb-reason", "服务未连接，灯光状态不可用"); text("rgb-state", "离线");
      text("power-reason", "服务未连接，电源操作已禁用");
      if (powerDeadline !== null) text("power-countdown", "连接中断 · 电源操作状态未知");
      powerDeadline = null;
      if (resolveConfirmation) resolveConfirmation(false);
      syncControls(); return;
    }
    const screen = toolbox.screen || {};
    text("screen-state", screen.asleep ? "已息屏" : percent(screen.brightness) !== null ? percentage(screen.brightness) + "%" : "状态未知");
    if (!brightnessDirty && percent(screen.brightness) !== null) $("brightness").value = Math.max(5, screen.brightness);
    text("brightness-value", brightnessDirty ? $("brightness").value + "%" : percentage(screen.brightness) + "%");
    text("screen-reason", cap("screen").available ? "轻触屏幕可唤醒；本页操作不会执行电源关机" : cap("screen").reason);
    setSleeping(screen.asleep);
    const rgb = toolbox.rgb || {};
    text("rgb-state", cap("rgb").available ? "可控制" : "不可用");
    text("rgb-reason", cap("rgb").available ? "仅使用宿主已验证的 I²C 灯光接口" : cap("rgb").reason);
    const buses = list(rgb.availableBuses).filter((bus) => Number.isInteger(bus));
    if (JSON.stringify(buses) !== rgbBusesSignature) {
      rgbBusesSignature = JSON.stringify(buses);
      const previousBus = $("rgb-bus").value;
      clear($("rgb-bus"));
      const placeholder = element("option", "", "请选择已检测到的总线"); placeholder.value = ""; $("rgb-bus").append(placeholder);
      buses.forEach((bus) => { const option = element("option", "", "i2c-" + bus); option.value = String(bus); $("rgb-bus").append(option); });
      $("rgb-bus").value = previousBus;
    }
    if (!rgbDirty && Number.isInteger(rgb.bus)) $("rgb-bus").value = String(rgb.bus);
    text("rgb-save", cap("rgb").available ? "应用灯光" : "验证总线并应用灯光");
    if (!rgbDirty) { if (/^#[0-9a-f]{6}$/i.test(rgb.color || "")) $("rgb-color").value = rgb.color; if (number(rgb.mode) !== null) $("rgb-mode").value = String(rgb.mode); }
    applySettings(toolbox.settings);
    renderFans(toolbox.fans);
    renderPower(toolbox.power);
    syncControls();
  }
  $("brightness").min = "5";
  $("setting-night-brightness").min = "5";
  $("brightness").addEventListener("input", () => { brightnessDirty = true; text("brightness-value", $("brightness").value + "%"); });
  $("brightness-save").addEventListener("click", async () => { if (await action("screen.brightness", { value: Number($("brightness").value) }, "亮度已调整")) brightnessDirty = false; });
  $("screen-sleep").addEventListener("click", async () => {
    if (await confirmOperation("立即息屏", "只关闭屏幕显示，NAS 和应用继续运行。再次轻触屏幕即可唤醒。", "确认息屏")) {
      if (await action("screen.sleep", {}, null)) setSleeping(true);
    }
  });
  ["rgb-color", "rgb-mode", "rgb-bus"].forEach((id) => $(id).addEventListener("input", () => { rgbDirty = true; }));
  $("rgb-save").addEventListener("click", async () => {
    const rgb = toolbox && toolbox.rgb;
    const selected = $("rgb-bus").value;
    const bus = selected === "" ? null : Number(selected);
    if (!rgb || !Number.isInteger(bus) || !list(rgb.availableBuses).includes(bus)) { toast("请明确选择宿主检测到的 I²C 总线", true); return; }
    if ((!cap("rgb").available || rgb.bus !== bus) && !(await confirmOperation("验证灯光接口", "将在 i2c-" + bus + " 的设备地址 0x26 验证接口并应用灯光；验证失败时不会执行灯光写入。", "验证并应用"))) return;
    if (await action("rgb.set", { bus, mode: Number($("rgb-mode").value), color: $("rgb-color").value }, "灯光设置已提交")) rgbDirty = false;
  });
  async function requestPower(operation) {
    const label = operation === "poweroff" ? "关闭 NAS" : "重启 NAS";
    const message = "此操作将影响所有用户、容器和文件访问。请先保存正在进行的工作。\n确认后进入 5 秒可取消倒计时。";
    if (await confirmOperation(label, message, "确认" + label, true)) await action("power.request", { operation, confirmed: true }, "电源操作已安排，可在倒计时结束前取消");
  }
  $("power-reboot").addEventListener("click", () => requestPower("reboot"));
  $("power-off").addEventListener("click", () => requestPower("poweroff"));
  $("power-cancel").addEventListener("click", () => action("power.cancel", {}, "已取消电源操作"));

  function emptyList(id, message) {
    clear($(id)); $(id).append(element("p", "capability-note", message));
  }
  function detailRow(title, description, health) {
    const row = element("div", "detail-row");
    row.append(element("strong", "", title), element("span", health || "", description));
    return row;
  }
  function renderApps(data) {
    const containers = data && data.containers;
    const vms = data && data.vms;
    const signature = JSON.stringify([containers, vms, toolboxOnline]);
    if (signature === appsSignature) { syncControls(); return; }
    appsSignature = signature;
    const groups = [
      { name: "container", value: containers, target: "container-list", count: "container-count", label: "容器" },
      { name: "vm", value: vms, target: "vm-list", count: "vm-count", label: "虚拟机" },
    ];
    groups.forEach((group) => {
      const values = list(group.value && group.value.items);
      text(group.count, group.value && group.value.available ? values.length + " 个" : "不可用");
      if (!group.value || !group.value.available) { emptyList(group.target, group.value && group.value.reason || "服务未连接，状态不可用"); return; }
      if (!values.length) { emptyList(group.target, "未发现" + group.label); return; }
      clear($(group.target));
      if (group.value.reason) $(group.target).append(element("p", "capability-note", group.value.reason));
      values.forEach((item) => {
        const card = element("article", "card");
        const heading = element("div", "item-heading");
        const running = item.state === "running";
        const stateLabels = { running: "运行中", exited: "已停止", created: "未启动", paused: "已暂停", restarting: "重启中", dead: "异常", shutoff: "已关机", "shut off": "已关机", suspended: "已挂起" };
        heading.append(element("h2", "item-title", item.name || String(item.id).slice(0, 12)), element("span", "item-status" + (running ? " is-running" : ""), stateLabels[item.state] || item.state || "未知"));
        card.append(heading);
        if (group.name === "container") {
          card.append(element("p", "item-image", item.image || "镜像信息不可用"));
          card.append(element("p", "item-metrics", `CPU ${number(item.cpu_percent) === null ? "--" : item.cpu_percent.toFixed(1)}% · 内存 ${bytes(item.memory_bytes)}`));
          if (item.status) card.append(element("p", "item-image", item.status));
        }
        if (item.protected) card.append(element("p", "capability-note", "Toolbox 容器受到保护，请从 NAS 管理页管理"));
        const operations = group.name === "container"
          ? running ? [["停止", "stop"], ["重启", "restart"]] : item.state === "exited" || item.state === "created" ? [["启动", "start"]] : []
          : running ? [["关机", "shutdown"], ["重启", "reboot"], ["挂起", "suspend"]] : item.state === "paused" || item.state === "suspended" ? [["恢复", "resume"]] : item.state === "shut off" || item.state === "shutoff" ? [["启动", "start"]] : [];
        const buttons = element("div", "item-actions");
        operations.forEach(([label, operation]) => {
          const button = element("button", "button button-secondary requires-write", label);
          button.type = "button";
          button.dataset.allowed = String(!item.protected && Boolean(insights));
          button.addEventListener("click", async () => {
            if (!(await confirmOperation(label + group.label, "确定要" + label + "「" + (item.name || item.id) + "」吗？正在进行的任务可能受到影响。", "确认" + label, operation !== "start" && operation !== "resume"))) return;
            if (await action(group.name + ".action", { id: item.id, operation, confirmed: true }, group.label + "操作已提交")) {
              await pollInsights(true);
            }
          });
          buttons.append(button);
        });
        if (operations.length) card.append(buttons);
        $(group.target).append(card);
      });
    });
    syncControls();
  }
  function renderInsights(data) {
    insights = data && typeof data === "object" ? data : null;
    renderApps(insights);
    const system = insights && insights.system || {};
    text("system-model", system.model || "系统型号不可用");
    text("cpu-model", system.cpu_model || "CPU 型号不可用");
    text("load-average", list(system.load_average).length ? system.load_average.map((value) => typeof value === "number" ? value.toFixed(2) : "--").join(" / ") : "--");
    text("system-counts", (number(system.process_count) === null ? "--" : system.process_count) + " / " + (number(system.cpu_logical_cores) === null ? "--" : system.cpu_logical_cores));
    const memory = system.memory || {};
    text("swap-usage", number(memory.swap_total_bytes) === 0 ? "未启用" : bytes(memory.swap_used_bytes) + " / " + bytes(memory.swap_total_bytes));
    clear($("cpu-cores"));
    const cores = list(system.cpu_cores);
    if (!cores.length) $("cpu-cores").append(element("p", "capability-note", "分核数据暂不可用"));
    cores.forEach((core) => {
      const cell = element("div", "core-cell");
      cell.append(element("span", "", "CPU " + core.id), document.createTextNode(percentage(core.usage_percent) + "%"));
      $("cpu-cores").append(cell);
    });
    const history = list(insights && insights.history).slice(-60);
    [["cpu-history", "cpu_usage_percent"], ["memory-history", "memory_usage_percent"]].forEach(([id, key]) => {
      const commands = []; let pen = false;
      history.forEach((point, index) => {
        const value = percent(point[key]);
        if (value === null) { pen = false; return; }
        const x = history.length <= 1 ? 150 : 4 + index / (history.length - 1) * 292;
        commands.push((pen ? "L" : "M") + x.toFixed(1) + " " + (66 - value * .62).toFixed(1)); pen = true;
      });
      $(id).setAttribute("d", commands.join(" "));
    });
    const disks = insights && insights.disks;
    if (!disks || !disks.available) emptyList("physical-disks", disks && disks.reason || "物理磁盘信息暂不可用");
    else {
      clear($("physical-disks"));
      if (disks.reason) $("physical-disks").append(element("p", "capability-note", disks.reason));
      const values = list(disks.items);
      if (!values.length) emptyList("physical-disks", "未发现物理磁盘");
      values.forEach((disk) => {
        const health = disk.health || {};
        const state = disk.standby ? "待机 · 不主动唤醒" : health.passed === true ? "SMART 正常" : health.passed === false ? "SMART 检测异常" : health.reason || "SMART 状态不可用";
        const temperature = number(disk.temp_c);
        const description = [disk.device || disk.id, bytes(disk.size_bytes), temperature === null ? "温度 --" : temperature.toFixed(0) + " °C", state].join(" · ");
        $("physical-disks").append(detailRow(disk.model || disk.id || "磁盘", description, health.passed === false ? "unhealthy" : health.passed === true ? "healthy" : ""));
      });
    }
    const interfaces = list(insights && insights.interfaces);
    clear($("interface-list"));
    if (!interfaces.length) emptyList("interface-list", "网络接口信息暂不可用");
    interfaces.forEach((network) => {
      const rx = speed(network.rx_bytes_per_sec), tx = speed(network.tx_bytes_per_sec);
      $("interface-list").append(detailRow(network.name + " · " + (network.operstate || "未知"), [list(network.ipv4).join(", ") || "无 IPv4", network.mac || "MAC --", "↓ " + rx.value + " " + rx.unit + " / ↑ " + tx.value + " " + tx.unit].join(" · ")));
    });
    const gpu = insights && insights.gpu;
    if (!gpu || !gpu.available) emptyList("gpu-list", gpu && gpu.reason || "GPU 信息暂不可用");
    else {
      clear($("gpu-list"));
      const devices = list(gpu.devices);
      if (!devices.length) emptyList("gpu-list", "未发现 GPU");
      devices.forEach((device) => $("gpu-list").append(detailRow(device.name || device.device || "GPU", "使用率 " + percentage(device.busy_percent) + "% · 显存 " + bytes(device.vram_used_bytes) + " / " + bytes(device.vram_total_bytes))));
    }
    clear($("process-list"));
    const processes = list(insights && insights.processes);
    if (!processes.length) emptyList("process-list", "进程信息暂不可用");
    processes.slice(0, 30).forEach((process) => $("process-list").append(detailRow(process.name || "进程", "PID " + process.pid + " · CPU " + (number(process.cpu_percent) === null ? "--" : process.cpu_percent.toFixed(1)) + "% · 内存 " + bytes(process.memory_bytes))));
    clear($("port-list"));
    const ports = list(insights && insights.ports);
    if (!ports.length) emptyList("port-list", insights ? "未发现监听端口" : "监听端口信息暂不可用");
    ports.slice(0, 60).forEach((port) => $("port-list").append(detailRow(String(port.protocol || "TCP").toUpperCase() + " :" + port.port, port.address || "地址不可用")));
  }
  let insightsRunning = false;
  async function pollInsights(force = false) {
    if (insightsRunning || (!force && Date.now() - lastInsightsPoll < 5000)) return;
    insightsRunning = true; lastInsightsPoll = Date.now();
    try { renderInsights(await request("/insights")); }
    catch (_) { renderInsights(null); }
    finally { insightsRunning = false; }
  }
  async function poll() {
    if (requestRunning) return;
    requestRunning = true;
    const tasks = [
      request("/status").then((data) => {
        if (!data || data.schema_version !== 1 || data.available !== true) throw new Error("status-unavailable");
        render(data); setConnection("online", data.timestamp);
      }).catch(() => { render(null); setConnection("offline"); }),
      request("/toolbox").then(renderToolbox).catch(() => renderToolbox(null)),
      pollInsights(),
    ];
    try { await Promise.all(tasks); }
    finally { requestRunning = false; }
  }
  function showPage(value) {
    const allowed = enabledPages.filter((name) => name !== "settings");
    if (enabledPages.includes("settings")) allowed.push("settings");
    if (!allowed.length) allowed.push("settings");
    let name;
    if (typeof value === "string") name = value;
    else {
      const current = Math.max(0, allowed.indexOf(pages[currentPage]));
      name = allowed[(current + value + allowed.length) % allowed.length];
    }
    if (!pages.includes(name) || (!enabledPages.includes(name) && name !== "settings")) return;
    currentPage = pages.indexOf(name);
    pages.forEach((pageName) => {
      const active = pageName === name;
      const page = $("page-" + pageName);
      page.hidden = !active;
      page.classList.toggle("is-active", active);
      const button = document.querySelector(`[data-page="${pageName}"]`);
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    const activeButton = document.querySelector(`[data-page="${name}"]`);
    if (activeButton && activeButton.scrollIntoView) activeButton.scrollIntoView({ block: "nearest", inline: "nearest" });
    lastCarousel = Date.now();
  }
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
  const viewport = $("viewport");
  viewport.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.target.closest("input, select, button, summary, .button, .disk-list")) return;
    pointerStart = { x: event.clientX, y: event.clientY, id: event.pointerId };
  });
  viewport.addEventListener("pointerup", (event) => {
    if (!pointerStart || event.pointerId !== pointerStart.id) return;
    const dx = event.clientX - pointerStart.x, dy = event.clientY - pointerStart.y;
    pointerStart = null;
    if (Math.abs(dx) >= 65 && Math.abs(dx) > Math.abs(dy) * 1.6) showPage(dx < 0 ? 1 : -1);
  });
  viewport.addEventListener("pointercancel", () => { pointerStart = null; });
  async function wakeScreen() {
    if (waking) return;
    waking = true;
    try { if (await action("screen.wake", {}, null)) { swallowUntil = Date.now() + 650; setSleeping(false); } }
    finally { waking = false; }
  }
  ["pointerdown", "pointerup", "click"].forEach((name) => {
    document.addEventListener(name, (event) => {
      if (sleeping || Date.now() < swallowUntil) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (name === "pointerdown" && sleeping) { swallowUntil = Date.now() + 650; wakeScreen(); }
        return;
      }
      if (name === "pointerdown") {
        lastInteraction = lastCarousel = Date.now();
        const activityInterval = settings.sleep_timeout ? Math.max(500, Math.min(10000, settings.sleep_timeout * 400)) : 10000;
        if (canWrite() && !actionBusy && Date.now() - lastIdleWake > activityInterval) {
          lastIdleWake = Date.now();
          request("/action", { action: "screen.activity" }).catch(() => {});
        }
      }
    }, true);
  });
  document.addEventListener("keydown", (event) => {
    lastInteraction = lastCarousel = Date.now();
    if (resolveConfirmation) {
      if (event.key === "Escape") { event.preventDefault(); resolveConfirmation(false); }
      if (event.key === "Tab") { event.preventDefault(); ($(document.activeElement === $("confirm-cancel") ? "confirm-accept" : "confirm-cancel")).focus(); }
      return;
    }
    if (event.target.closest("input, select, textarea")) return;
    if (event.key === "ArrowRight") showPage(1);
    if (event.key === "ArrowLeft") showPage(-1);
  });
  window.addEventListener("offline", () => { render(null); setConnection("offline"); renderToolbox(null); renderInsights(null); });
  window.addEventListener("online", poll);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { updateClock(); poll(); } });
  updateClock(); render(null); renderToolbox(null); renderInsights(null); setConnection("connecting");
  poll();
  window.setInterval(() => {
    updateClock(); updatePowerCountdown();
    const interval = number(settings.page_interval);
    if (interval && !sleeping && !resolveConfirmation && !actionBusy && !settingsDirty && Date.now() - lastInteraction >= interval * 1000 && Date.now() - lastCarousel >= interval * 1000) showPage(1);
  }, 1000);
  window.setInterval(poll, POLL_INTERVAL);
})();
