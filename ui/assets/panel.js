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
  let currentDetail = null;
  let navigationStack = [];
  let lastStatus = null;
  let fanDetailSignature = null;
  let independentFanPolicy = null;
  let lightPolicy = null;
  let requestRunning = false;
  let pointerStart = null;
  let suppressGestureClick = false;
  let diskSignature = null;
  let toolbox = null;
  let insights = null;
  let toolboxOnline = false;
  let actionBusy = false;
  let settingsDirty = false;
  let brightnessDirty = false;
  let rgbDirty = false;
  let rgbBrightnessDirty = false;
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
  let webConfig = null;
  let webConfigRunning = false;
  let lastWebConfigPoll = 0;
  let pairingDeadline = null;

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
    if (!bar) return;
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
    clear($("disk-list"));
    if (!disks.length) { $("disk-list").append(element("p", "capability-note", connected ? "暂未发现挂载的数据盘" : "等待连接本机服务")); return; }
    disks.forEach((disk, index) => {
      const parts = disk.mount.split("/").filter(Boolean);
      const name = parts[parts.length - 1] || "存储空间 " + (index + 1);
      $("disk-list").append(menuButton(name, bytes(disk.used_bytes) + " / " + bytes(disk.total_bytes) + " · " + percentage(disk.usage_percent) + "%", () => openView("volume-detail", { mount: disk.mount }, name)));
    });
    renderFocusedDetail();
  }

  function render(data) {
    lastStatus = data;
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
    const timeout = window.setTimeout(() => controller.abort(), payload ? 18000 : REQUEST_TIMEOUT);
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
  const detailTitles = { screen: "屏幕", fans: "风扇", "fan-detail": "风扇通道", "fan-policy-detail": "独立风扇状态", "official-fan": "原厂系统风扇策略", power: "电源", rgb: "灯光", "rgb-probe": "验证灯光接口", containers: "Docker 容器", vms: "虚拟机", "app-detail": "应用详情", identity: "设备信息", cpu: "处理器", memory: "内存", "physical-disks": "物理磁盘", "volume-detail": "存储空间", "disk-detail": "磁盘详情", interfaces: "网络接口", "interface-detail": "接口详情", gpu: "GPU", processes: "进程", ports: "监听端口", thermal: "温度传感器", "fan-sensors": "风扇传感器", raid: "阵列信息", capabilities: "设备能力", "settings-editor": "面板设置", "network-routes": "默认网关", "network-dns": "DNS 与搜索域", "web-config": "网页配置" };
  function activeScroller() { return currentDetail ? $("detail-body") : $("page-" + pages[currentPage]); }
  function drawNavigation() {
    pages.forEach((name, index) => { $("page-" + name).hidden = Boolean(currentDetail) || index !== currentPage; });
    $("subpage").hidden = !currentDetail;
    document.querySelector(".swipe-hint").textContent = currentDetail ? "右滑返回上一级" : "左右滑动切换";
    document.querySelectorAll("[data-detail-view]").forEach((view) => { view.hidden = !currentDetail || view.dataset.detailView !== currentDetail.name; });
    if (currentDetail) {
      const parent = navigationStack.length ? navigationStack[navigationStack.length - 1] : null;
      text("detail-title", currentDetail.title || detailTitles[currentDetail.name]);
      text("detail-breadcrumb", parent && parent.detail ? parent.detail.title || detailTitles[parent.detail.name] : pageLabels[pages[currentPage]]);
      $("detail-depth").textContent = navigationStack.length > 1 ? "详情" : "";
      document.querySelectorAll("[data-settings-section]").forEach((section) => { section.hidden = currentDetail.name !== "settings-editor" || section.dataset.settingsSection !== currentDetail.params.section; });
    }
  }
  function openView(name, params = {}, title, replace = false) {
    if (!document.querySelector('[data-detail-view="' + name + '"]')) return;
    if (currentDetail && currentDetail.name === "web-config" && name !== "web-config") clearWebPairing();
    if (!replace) navigationStack.push({ detail: currentDetail, scroll: activeScroller().scrollTop });
    currentDetail = { name, params, title: title || detailTitles[name] };
    drawNavigation(); renderFocusedDetail(); $("detail-body").scrollTop = 0;
    lastCarousel = lastInteraction = Date.now();
    if (name === "web-config") pollWebConfig(true);
  }
  function backView() {
    if (!currentDetail) return;
    if (currentDetail.name === "web-config") clearWebPairing();
    const previous = navigationStack.pop();
    currentDetail = previous ? previous.detail : null;
    drawNavigation(); renderFocusedDetail();
    activeScroller().scrollTop = previous ? previous.scroll : 0;
    lastCarousel = lastInteraction = Date.now();
  }
  $("detail-back").addEventListener("click", backView);
  document.addEventListener("click", (event) => {
    const view = event.target.closest("[data-open-view]");
    const page = event.target.closest("[data-open-page]");
    if (view) openView(view.dataset.openView, view.dataset.section ? { section: view.dataset.section } : {});
    else if (page) showPage(page.dataset.openPage);
  });
  function menuButton(title, description, onClick, className = "") {
    const button = element("button", "menu-row " + className);
    button.type = "button";
    const labels = element("span", "");
    labels.append(element("strong", "", title), element("small", "", description));
    button.append(labels, element("span", "menu-chevron", "›"));
    button.addEventListener("click", onClick);
    return button;
  }
  function familyFanControl() {
    const official = toolbox && toolbox.fans && toolbox.fans.official;
    return Boolean(independentFanPolicy) || Boolean(cap("fans").available === true && list(toolbox && toolbox.fans && toolbox.fans.channels).length) || Boolean(official && official.control_available === true);
  }
  function showControlModules() {
    const official = toolbox && toolbox.fans && toolbox.fans.official;
    const available = { screen: cap("screen").available === true, fans: familyFanControl(), power: cap("power").available === true,
      rgb: cap("rgb").available === true };
    let count = 0;
    document.querySelectorAll("[data-control-entry]").forEach((button) => {
      const name = button.dataset.controlEntry;
      button.hidden = !toolboxOnline || !available[name];
      if (!button.hidden) count++;
      if (name === "rgb") {
        button.dataset.openView = cap("rgb").available ? "rgb" : "rgb-probe";
        button.querySelector("strong").textContent = cap("rgb").available ? "灯光" : "验证灯光接口";
        button.querySelector("small").textContent = cap("rgb").available ? "颜色与灯效" : "验证通过后开放调色";
      }
      if (name === "fans") button.querySelector("small").textContent = independentFanPolicy ? "CPU 与系统风扇 · 各自独立状态" : official && official.control_available ? "原厂系统风扇策略 · 仅系统风扇" : "可用风扇通道";
    });
    $("control-empty").hidden = count > 0;
    if (currentDetail && ["screen", "power", "rgb", "rgb-probe", "fans", "fan-detail", "official-fan"].includes(currentDetail.name)) {
      const type = currentDetail.name.startsWith("rgb") ? "rgb" : currentDetail.name.includes("fan") ? "fans" : currentDetail.name;
      if (!toolboxOnline || !available[type]) { currentDetail = null; navigationStack = []; drawNavigation(); }
    }
  }
  function fillDetails(id, rows, fallback = "暂无可用信息") {
    clear($(id));
    const present = rows.filter((row) => row[1] !== undefined && row[1] !== null && row[1] !== "");
    if (!present.length) { $(id).append(element("p", "capability-note", fallback)); return; }
    present.forEach(([label, value, health]) => $(id).append(detailRow(label, String(value), health)));
  }
  function readable(value) {
    if (typeof value === "string" || typeof value === "number") return String(value);
    if (Array.isArray(value)) return value.map(readable).filter(Boolean).join(" · ");
    return value && typeof value === "object" ? Object.entries(value).map(([key, item]) => key + ": " + readable(item)).join(" · ") : null;
  }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function renderFocusedDetail() {
    if (!currentDetail) return;
    const name = currentDetail.name, params = currentDetail.params;
    const device = insights && insights.device || {}, system = insights && insights.system || {};
    if (name === "fan-detail") renderFanDetail(list(toolbox && toolbox.fans && toolbox.fans.channels).find((channel) => channel.id === params.id));
    if (name === "official-fan") renderOfficialFan();
    if (name === "fan-policy-detail") renderIndependentFanDetail(params.id);
    if (name === "app-detail") renderAppDetail(params.kind, params.id);
    if (name === "volume-detail") {
      const volume = list(lastStatus && lastStatus.disks).find((item) => item.mount === params.mount);
      fillDetails("volume-detail", volume ? [["挂载路径", volume.mount], ["已用空间", bytes(volume.used_bytes)], ["总容量", bytes(volume.total_bytes)], ["使用率", percentage(volume.usage_percent) + "%"]] : [], "此存储空间当前不可用");
    }
    if (name === "disk-detail") {
      const disk = list(insights && insights.disks && insights.disks.items).find((item) => (item.id || item.device) === params.id);
      const raw = list(device.storage && device.storage.devices).find((item) => disk && (item.name === disk.id || "/dev/" + item.name === disk.device));
      const health = disk && disk.health || {};
      fillDetails("disk-detail", disk ? [["型号", disk.model], ["设备", disk.device || disk.id], ["容量", bytes(disk.size_bytes)], ["类型", disk.type], ["温度", finite(disk.temp_c) ? disk.temp_c + " °C" : "暂无读数"], ["状态", disk.standby ? "待机 · 未主动唤醒" : "可读取"], ["SMART", health.passed === true ? "检测正常" : health.passed === false ? "检测异常" : health.reason || "暂不可用", health.passed === false ? "unhealthy" : ""], ["序列号", raw && raw.serial], ["厂商", raw && raw.vendor], ["介质状态", raw && raw.state]] : [], "此磁盘当前不可用");
    }
    if (name === "interface-detail") {
      const network = networkInterfaces().find((item) => item.name === params.id);
      const raw = network && network.raw || {};
      const rx = speed(network && network.rx_bytes_per_sec), tx = speed(network && network.tx_bytes_per_sec);
      const rows = [];
      if (network) {
        ["ipv4", "ipv6"].forEach((family) => {
          const addresses = interfaceAddresses(network, family);
          addresses.forEach((entry, index) => rows.push([ipFamilyLabel(family) + " · 地址 " + (index + 1), formatAddress(entry)]));
          if (!addresses.length) {
            const status = raw.address_status && raw.address_status[family];
            rows.push([ipFamilyLabel(family), status && status !== "ok" ? networkStatus(status) : "当前未报告地址"]);
          } else if (raw.address_status && raw.address_status[family] && raw.address_status[family] !== "ok") rows.push([ipFamilyLabel(family) + " 地址状态", networkStatus(raw.address_status[family])]);
          const routes = list(raw.default_gateways && raw.default_gateways[family]);
          const status = raw.route_status && raw.route_status[family];
          rows.push([ipFamilyLabel(family) + " 默认网关", routes.length ? routes.map(formatGateway).join("\n\n") : status === "ok" ? "未发现默认网关" : "默认网关暂不可读取"]);
        });
        rows.push(["状态", network.operstate || "未提供"], ["MAC", network.mac || raw.mac], ["接收速度", rx.value + " " + rx.unit], ["发送速度", tx.value + " " + tx.unit], ["累计接收", bytes(network.rx_bytes)], ["累计发送", bytes(network.tx_bytes)], ["连接速率", finite(raw.speed_mbps) && raw.speed_mbps > 0 ? raw.speed_mbps + " Mbps" : null], ["双工", raw.duplex], ["MTU", raw.mtu]);
      }
      fillDetails("interface-detail", rows, "此网络接口当前不可用");
    }
    if (name === "network-routes") renderNetworkRoutes();
    if (name === "network-dns") renderNetworkDNS();
    if (name === "web-config") renderWebConfig();
    if (name === "memory") {
      const memory = system.memory || lastStatus && lastStatus.memory || {};
      fillDetails("memory-detail", [["内存使用率", percentage(memory.usage_percent) + "%"], ["已用", bytes(memory.used_bytes)], ["可用", bytes(memory.available_bytes)], ["总量", bytes(memory.total_bytes)], ["交换空间", number(memory.swap_total_bytes) === 0 ? "未启用" : bytes(memory.swap_used_bytes) + " / " + bytes(memory.swap_total_bytes)]]);
    }
    if (name === "identity") {
      const identity = device.identity || {}, cpu = device.cpu || {};
      const official = toolbox && toolbox.fans && toolbox.fans.official;
      const modes = { low: "低速", medium: "中速", high: "高速", auto: "自动" };
      fillDetails("device-identity-extra", [["持续运行", lastStatus ? uptime(lastStatus.uptime_seconds) : null], ["操作系统", readable(identity.os)], ["固件", readable(identity.firmware)], ["内核", readable(identity.kernel)], ["架构", cpu.architecture], ["物理核心", cpu.physical_cores], ["处理器插槽", cpu.sockets], ["DMI", readable(identity.dmi)], ["PCI 设备", readable(identity.pci)], ["配置系统风扇策略", official && modes[official.mode]], ["当前原厂档位", official && finite(official.grade) ? official.grade + "（档位，不是转速）" : null], ["原厂风扇接口", official && official.reason], ["风扇驱动 Build-ID", official && official.driver_build_id], ["硬件能力说明", official && official.hardware_reason]]);
    }
    if (name === "cpu") {
      const cpu = device.cpu || {};
      const frequency = list(cpu.frequencies_mhz).filter((item) => finite(item.current)).map((item) => "CPU " + item.id + " · " + Math.round(item.current) + " MHz").join(" / ");
      fillDetails("cpu-hardware-detail", [["型号", cpu.model || system.cpu_model], ["核心", (cpu.physical_cores || "--") + " 物理 / " + (cpu.logical_cores || system.cpu_logical_cores || "--") + " 逻辑"], ["架构", cpu.architecture], ["报告频率", frequency]], "");
    }
    if (name === "thermal") fillDetails("thermal-detail", list(device.thermal).filter((item) => finite(item.celsius)).map((item) => [item.driver + " · " + (item.label || item.channel || item.id), item.celsius + " °C" + (finite(item.critical_celsius) ? " · 临界 " + item.critical_celsius + " °C" : "")]), "暂无可读取的温度传感器");
    if (name === "fan-sensors") {
      const rows = list(device.fans).filter((item) => finite(item.rpm)).map((item) => [item.driver + " · " + (item.label || item.channel || item.id), item.rpm + " RPM" + (item.role_verified && item.physical_role ? " · " + item.physical_role : " · 未确认物理位置")]);
      const official = toolbox && toolbox.fans && toolbox.fans.official;
      if (official && finite(official.cpu_rpm)) rows.push(["CPU 风扇 · 原厂接口", official.cpu_rpm + " RPM"]);
      if (official && finite(official.system_rpm)) rows.push(["系统风扇 · 原厂接口", official.system_rpm + " RPM"]);
      fillDetails("fan-sensor-detail", rows, "当前接口未提供转速读数");
    }
    if (name === "raid") fillDetails("raid-detail", list(device.storage && device.storage.raid).map((item) => [item.name || item.device || "阵列", readable(item)]), "宿主未报告软件阵列");
    if (name === "capabilities") {
      const labels = { screen: "屏幕控制", fans: "风扇控制", rgb: "灯光控制", power: "电源控制" };
      const rows = Object.entries(toolbox && toolbox.capabilities || {}).map(([key, value]) => [labels[key] || key, value.available ? "可用" : value.reason || "当前不可用"]);
      const official = toolbox && toolbox.fans && toolbox.fans.official;
      if (official && official.policy_only) rows.push(["原厂系统风扇策略接口", official.reason || "策略可读取；硬件调速尚未验证，不开放控制"]);
      Object.entries(device.capabilities || {}).forEach(([key, value]) => rows.push([key, typeof value === "object" ? value.reason || (value.available ? "可用" : "当前不可用") : readable(value)]));
      fillDetails("capabilities-detail", rows, "服务未连接，无法读取设备能力");
    }
  }
  function ipFamilyLabel(family) { return family === "ipv4" ? "IPv4" : "IPv6"; }
  function networkInterfaces() {
    const items = new Map();
    list(insights && insights.interfaces).forEach((item) => { if (item && item.name) items.set(item.name, Object.assign({}, item, { raw: {} })); });
    list(insights && insights.device && insights.device.network).forEach((raw) => {
      if (!raw || !raw.name) return;
      const network = items.get(raw.name) || { name: raw.name, ipv4: [], operstate: raw.state };
      network.raw = raw; items.set(raw.name, network);
    });
    return Array.from(items.values());
  }
  function interfaceAddresses(network, family) {
    const raw = network.raw || {};
    const structured = list(raw[family + "_addresses"]).length ? raw[family + "_addresses"] : list(network[family + "_addresses"]);
    if (structured.length) return structured.filter((entry) => entry && typeof entry.address === "string");
    return list(family === "ipv4" ? network.ipv4 : raw.ipv6).filter((entry) => typeof entry === "string").map((entry) => {
      const parts = entry.split("/"); return { address: parts[0], prefix_length: parts.length > 1 && /^\d+$/.test(parts[1]) ? Number(parts[1]) : null, scope: null };
    });
  }
  function formatAddress(entry) {
    const prefix = Number.isInteger(entry.prefix_length) ? "/" + entry.prefix_length : "前缀未提供";
    const scopeNames = { global: "全局", link: "链路本地", host: "本机", site: "站点本地" };
    const scope = entry.scope === undefined || entry.scope === null ? "未提供" : String(entry.scope) + (scopeNames[entry.scope] ? "（" + scopeNames[entry.scope] + "）" : "");
    if (prefix[0] !== "/" && scope === "未提供") return entry.address + "\n前缀与 scope 暂未提供";
    return entry.address + (prefix[0] === "/" ? prefix : "\n" + prefix) + "\nscope：" + scope;
  }
  function formatGateway(route) {
    return (route.gateway === null ? "直连路由（无下一跳网关）" : route.gateway || "网关地址未提供") + (finite(route.metric) ? "\nmetric：" + route.metric : "") + (route.source ? "\n来源\n" + route.source : "");
  }
  function networkStatus(value) {
    return { ok: "读取正常", partial: "部分来源可用", unavailable: "暂不可读取", malformed: "来源格式异常" }[value] || "状态未提供";
  }
  function renderNetworkOverview() {
    const interfaces = networkInterfaces();
    ["ipv4", "ipv6"].forEach((family) => {
      const addresses = interfaces.flatMap((item) => interfaceAddresses(item, family));
      text("network-" + family + "-summary", ipFamilyLabel(family) + " · " + (addresses.length ? addresses[0].address + (addresses.length > 1 ? " · 共 " + addresses.length + " 个" : "") : "当前未报告地址"));
    });
  }
  function renderNetworkRoutes() {
    const config = insights && insights.device && insights.device.network_config || {};
    const rows = [];
    ["ipv4", "ipv6"].forEach((family) => {
      const routes = list(config.default_routes && config.default_routes[family]);
      const status = config.route_status && config.route_status[family];
      if (!routes.length) rows.push([ipFamilyLabel(family) + " 默认网关", status === "ok" ? "未发现默认网关" : networkStatus(status)]);
      routes.forEach((route, index) => rows.push([ipFamilyLabel(family) + " · " + (route.interface || "接口未提供") + (routes.length > 1 ? " · " + (index + 1) : ""), formatGateway(route)]));
      if (routes.length && status && status !== "ok") rows.push([ipFamilyLabel(family) + " 读取状态", networkStatus(status)]);
    });
    fillDetails("network-routes-detail", rows);
  }
  function renderNetworkDNS() {
    const dns = insights && insights.device && insights.device.network_config && insights.device.network_config.dns || {};
    const rows = [["读取状态", networkStatus(dns.status) + (dns.reason ? "\n" + dns.reason : "")], ["DNS 服务器", list(dns.servers).join("\n") || "当前来源未报告可用地址"], ["上游 DNS", list(dns.upstream_servers).join("\n")], ["本地 stub", list(dns.stub_servers).join("\n")], ["搜索域", list(dns.search).join("\n") || "当前来源未报告搜索域"], ["默认域名", list(dns.domains).join("\n")]];
    list(dns.sources).forEach((source, index) => {
      const details = [source.path, "状态：" + networkStatus(source.status)];
      if (list(source.servers).length) details.push("服务器：\n" + source.servers.join("\n"));
      if (list(source.search).length) details.push("搜索域：\n" + source.search.join("\n"));
      if (list(source.domains).length) details.push("默认域名：\n" + source.domains.join("\n"));
      rows.push(["配置来源 · " + (source.source || index + 1), details.filter(Boolean).join("\n")]);
    });
    fillDetails("network-dns-detail", rows);
  }
  function clearWebPairing() {
    pairingDeadline = null; clear($("web-pairing-code")); $("web-pairing").hidden = true;
    text("web-pairing-expiry", "");
  }
  function updatePairingCountdown() {
    if (pairingDeadline === null) return;
    const remaining = Math.max(0, Math.ceil((pairingDeadline - Date.now()) / 1000));
    if (!remaining) { clearWebPairing(); text("web-config-status", "配对码已过期，请重新生成"); return; }
    text("web-pairing-expiry", remaining + " 秒后失效 · 仅可使用一次");
  }
  function renderWebConfig() {
    const config = webConfig || {};
    text("web-config-status", !webConfig ? "正在读取网页服务状态" : config.enabled ? "在同一局域网的电脑或手机打开以下地址" : config.reason || "网页服务未启用，可继续使用小屏本地设置");
    clear($("web-config-addresses"));
    list(config.addresses).forEach((address) => {
      const row = detailRow("网页地址", String(address)); row.classList.add("web-address"); $("web-config-addresses").append(row);
    });
    text("web-config-security", config.enabled ? (config.scheme === "https" ? "通过配对码登录网页。请仅向你信任的设备提供配对码。" : "HTTP 未加密，仅在可信局域网使用。配对码是临时登录凭据，请勿转发。") : "服务未启用时无法生成配对码。");
    if (!config.enabled || !canWrite()) clearWebPairing();
    setAllowed("web-pairing-generate", Boolean(config.enabled));
    $("web-pairing-generate").disabled = !config.enabled || !canWrite() || actionBusy;
    updatePairingCountdown();
  }
  async function pollWebConfig(force = false) {
    if (webConfigRunning || (!force && Date.now() - lastWebConfigPoll < 5000)) return;
    webConfigRunning = true; lastWebConfigPoll = Date.now();
    try { webConfig = await request("/web-config"); }
    catch (_) { webConfig = { enabled: false, reason: "网页服务暂不可用，可继续使用小屏本地设置", addresses: [] }; }
    finally { webConfigRunning = false; if (currentDetail && currentDetail.name === "web-config") renderWebConfig(); }
  }
  $("web-pairing-generate").addEventListener("click", async () => {
    await action("web.pairing", { rotate: true }, null, { onResult(result) {
      const pairing = result.web || result.pairing || result;
      if (!currentDetail || currentDetail.name !== "web-config" || !webConfig || !webConfig.enabled || !/^\d{5}$/.test(pairing.code || "") || !finite(pairing.expires_in) || pairing.expires_in <= 0) return;
      clearWebPairing();
      pairingDeadline = Date.now() + Math.min(180, pairing.expires_in) * 1000;
      $("web-pairing-code").append(element("span", "", pairing.code));
      $("web-pairing").hidden = false; updatePairingCountdown();
      $("web-pairing").scrollIntoView({ block: "center" });
    } });
  });
  function updateInformationModules() {
    const device = insights && insights.device || {};
    const gpu = insights && insights.gpu;
    $("thermal-entry").hidden = !list(device.thermal).some((item) => finite(item.celsius));
    const official = toolbox && toolbox.fans && toolbox.fans.official;
    $("fan-sensor-entry").hidden = !list(device.fans).some((item) => finite(item.rpm)) && !(official && (finite(official.cpu_rpm) || finite(official.system_rpm)));
    $("gpu-entry").hidden = !(gpu && gpu.available && list(gpu.devices).length);
    $("process-entry").hidden = !list(insights && insights.processes).length;
    $("ports-entry").hidden = !list(insights && insights.ports).length;
    $("raid-entry").hidden = !list(device.storage && device.storage.raid).length;
    $("physical-disks-entry").hidden = !(insights && insights.disks && insights.disks.available);
    $("interfaces-entry").hidden = false;
    renderNetworkOverview();
    const system = insights && insights.system || {};
    text("system-summary", system.model ? system.model + (number(system.cpu_logical_cores) !== null ? " · " + system.cpu_logical_cores + " 核" : "") : "设备报告的真实信息与采样");
    renderFocusedDetail();
  }

  function setAllowed(id, allowed) { $(id).dataset.allowed = String(Boolean(allowed)); }
  function syncControls() {
    const writable = canWrite() && !actionBusy;
    const screenAvailable = cap("screen").available === true;
    const rgbAvailable = cap("rgb").available === true;
    const rgbProbeAvailable = Boolean(toolbox && toolbox.rgb && list(toolbox.rgb.availableBuses).length);
    const powerAvailable = cap("power").available === true;
    const pending = Boolean(toolbox && toolbox.power && toolbox.power.pending);
    const executing = Boolean(toolbox && toolbox.power && toolbox.power.executing);
    setAllowed("brightness-save", screenAvailable);
    setAllowed("screen-sleep", screenAvailable && !pending);
    setAllowed("rgb-save", rgbAvailable && !(lightPolicy && (lightPolicy.active || lightPolicy.busy)));
    setAllowed("rgb-probe", rgbProbeAvailable);
    setAllowed("web-pairing-generate", Boolean(webConfig && webConfig.enabled));
    setAllowed("power-reboot", powerAvailable && !pending && !executing);
    setAllowed("power-off", powerAvailable && !pending && !executing);
    document.querySelectorAll(".requires-write").forEach((node) => {
      node.disabled = !writable || node.dataset.allowed === "false";
    });
    $("brightness").disabled = !writable || !screenAvailable;
    $("rgb-color").disabled = $("rgb-mode").disabled = $("rgb-brightness").disabled = !writable || !rgbAvailable;
    $("rgb-bus").disabled = !writable || !rgbProbeAvailable;
    document.querySelectorAll("#settings-form input, #settings-form select").forEach((node) => { node.disabled = !writable; });
    document.querySelectorAll(".fan-input").forEach((node) => { node.disabled = !writable || !cap("fans").available; });
    $("power-cancel").disabled = $("global-power-cancel").disabled = !writable || !pending || executing;
    if (!toolboxOnline) clearWebPairing();
    showControlModules();
    const notice = !controlToken ? "只读预览 · 控制仅限本机小屏授权会话" : !toolboxOnline ? "服务未连接 · 所有设备操作已禁用" : actionBusy ? "正在执行操作，请稍候" : "本机授权会话 · 所有高影响操作均需确认";
    text("control-access", notice);
    $("control-access").hidden = canWrite();
    $("action-status").hidden = !actionBusy;
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
      if (typeof options.onResult === "function") options.onResult(result);
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
    text("settings-summary", "当前主题：" + ({dark:"深空",light:"晨光",ink:"水墨",jade:"青玉"}[settings.theme] || "深空") + " · 字号 " + (scale || 100) + "%");
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

  function independentFanFreshness(source) {
    if (!source) return "来源暂不可用";
    const age = finite(source.age_seconds) ? Math.max(0, Math.round(source.age_seconds)) + " 秒前" : "采样时间未知";
    return (source.status === "standby" ? "休眠" : source.fresh && source.status === "ok" ? "有效" : "缺测或过期") + " · " + age;
  }
  const independentFanNames = { cpu: "CPU 风扇", system: "系统风扇" };
  const independentFanLevels = { vendor: "原厂管理", quiet: "静音", normal: "普通", full: "全速", manual: "手动固定", failsafe: "保护全速" };
  function independentFanStateLabel(state) { return String(state.reason || "").includes("恢复未确认") ? "恢复未确认" : independentFanLevels[state.level] || "状态未知"; }
  function independentFanBlocksLegacy() { return Boolean(independentFanPolicy && (independentFanPolicy.active || independentFanPolicy.busy || Object.values(independentFanPolicy.channels || {}).some(state => String(state.reason || "").includes("恢复未确认")))); }
  function renderIndependentFanPolicy(snapshot) {
    independentFanPolicy = snapshot && snapshot.config && snapshot.channels && snapshot.channels.cpu && snapshot.channels.system ? snapshot : null;
    $("independent-fans").hidden = !independentFanPolicy;
    clear($("independent-fan-list"));
    if (independentFanPolicy) {
      const data = independentFanPolicy;
      text("independent-fan-note", data.busy ? "配置已保存，正在切换控制方式" : data.last_error || (data.pending_changes ? "已保存 · 有待生效的配置" : data.active ? "CPU 与系统风扇分别执行配置" : "当前原厂管理；规则可在网页分别配置"));
      Object.entries(independentFanNames).forEach(([id, title]) => {
        const state = data.channels[id], source = list(data.sources).find(item => item.id === state.source_id);
        const button = element("button", "menu-row fan-policy-summary"); button.type = "button";
        const content = element("span", "");
        content.append(element("strong", "", title + " · " + independentFanStateLabel(state)), element("span", "fan-reference", finite(state.reference_c) ? state.reference_c + " °C" : "参考温度 --"), element("small", "", (state.source_label || "触发来源未提供") + "\n" + independentFanFreshness(source)), element("small", "", (finite(state.target_percent) ? "请求输出 " + state.target_percent + "%" : String(state.reason || "").includes("恢复未确认") ? "输出未确认" : "输出由原厂管理") + " · " + (finite(state.rpm) ? state.rpm + " RPM" : "RPM 未知")));
        button.append(content, element("span", "menu-chevron", "›")); button.addEventListener("click", () => openView("fan-policy-detail", { id }, title)); $("independent-fan-list").append(button);
      });
      if (currentDetail && currentDetail.name === "fan-policy-detail") renderIndependentFanDetail(currentDetail.params.id);
    }
    if (!independentFanPolicy && currentDetail && currentDetail.name === "fan-policy-detail") renderIndependentFanDetail(currentDetail.params.id);
    fanSignature = null;
    if (toolbox) renderFans(toolbox.fans);
    showControlModules();
  }
  function renderIndependentFanDetail(id) {
    const policy = independentFanPolicy, state = policy && policy.channels && policy.channels[id];
    if (!state) { fillDetails("fan-policy-channel-detail", [], "实时风扇状态暂不可用，请等待重新连接"); return; }
    const source = list(policy.sources).find(item => item.id === state.source_id), config = policy.config.channels[id];
    const modes = { vendor: "原厂管理", manual: "手动固定输出", temperature: "自定义温控" };
    fillDetails("fan-policy-channel-detail", [["当前运行", independentFanStateLabel(state)], ["当前控制模式", String(state.reason || "").includes("恢复未确认") ? "原厂恢复未确认" : modes[state.mode]], ["已保存模式", modes[config.mode]], ["参考温度 · 所选有效来源 max", finite(state.reference_c) ? state.reference_c + " °C" : "暂不可读"], ["触发来源", state.source_label || "未提供"], ["采样新鲜度", independentFanFreshness(source)], ["请求目标输出", finite(state.target_percent) ? state.target_percent + "%（不是 RPM）" : String(state.reason || "").includes("恢复未确认") ? "输出未确认" : "原厂管理"], ["实际转速", finite(state.rpm) ? state.rpm + " RPM" : "接口未提供，保持未知"], ["当前原因", state.reason], ["独立控制能力", policy.capability && policy.capability.available ? "已验证" : policy.capability && policy.capability.reason || "尚未验证"], ["开机恢复", policy.active_on_boot ? "已启用；启动时仍需通过验证" : "未启用"]]);
  }
  function renderFans(data) {
    const channels = list(data && data.channels), official = data && data.official;
    const policyOwns = independentFanBlocksLegacy();
    const signature = JSON.stringify([cap("fans"), data && data.last_error, policyOwns, official, channels.map((item) => [item.id, item.driver, item.owned, item.mode, item.duty_percent])]);
    if (signature !== fanSignature) {
      fanSignature = signature; clear($("fan-list"));
      if (data && data.last_error) $("fan-list").append(element("p", "capability-note fan-error", data.last_error));
      if (official && official.control_available && !policyOwns) {
        const readings = [];
        if (finite(official.cpu_rpm)) readings.push("CPU " + official.cpu_rpm + " RPM");
        if (finite(official.system_rpm)) readings.push("系统 " + official.system_rpm + " RPM");
        $("fan-list").append(menuButton("原厂系统风扇策略", readings.length ? "仅系统风扇 · " + readings.join(" · ") : "仅原厂系统风扇 · 转速读数不可用", () => openView("official-fan")));
      }
      if (!policyOwns) channels.forEach((channel) => $("fan-list").append(menuButton(channel.label || channel.driver + " · " + channel.id, percentage(channel.duty_percent) + "% PWM · " + (channel.owned ? "Toolbox 接管" : "原控制模式"), () => openView("fan-detail", { id: channel.id }, channel.label || channel.id))));
      if (!channels.length && !(official && official.control_available && !policyOwns) && !independentFanPolicy) $("fan-list").append(element("p", "capability-note", "此设备当前没有可控制的风扇接口"));
    }
    if (currentDetail && currentDetail.name === "fan-detail") renderFanDetail(channels.find((channel) => channel.id === currentDetail.params.id));
    if (currentDetail && currentDetail.name === "official-fan") renderOfficialFan();
  }
  function renderOfficialFan() {
    const official = toolbox && toolbox.fans && toolbox.fans.official;
    const modes = { low: "低速", medium: "中速", high: "高速", auto: "自动" };
    fillDetails("official-fan-readings", official ? [["CPU 风扇", finite(official.cpu_rpm) ? official.cpu_rpm + " RPM" : null], ["系统风扇", finite(official.system_rpm) ? official.system_rpm + " RPM" : null], ["当前系统风扇策略", modes[official.mode] || "未读取到策略"]] : [], "接口当前不可用");
    text("official-fan-reason", official && !finite(official.cpu_rpm) && !finite(official.system_rpm) ? "系统风扇策略可设，转速读数不可用" : official && official.reason || "该策略会保存到海康配置");
    clear($("official-fan-modes"));
    const policyOwns = independentFanBlocksLegacy();
    text("official-fan-scope", policyOwns ? "此入口仅对应原厂系统风扇。独立温控运行中，请先在网页恢复原厂管理，再设置原厂档位。" : "此入口仅设置原厂系统风扇。CPU 风扇继续由原厂温控管理；物理风扇对应需实机确认。");
    if (!official || !official.control_available || policyOwns) return;
    Object.entries(modes).forEach(([mode, label]) => {
      const button = element("button", "button button-secondary requires-write" + (official.mode === mode ? " is-selected" : ""), label);
      button.type = "button";
      button.addEventListener("click", async () => {
        if (!(await confirmOperation("切换至" + label, "将修改原厂系统风扇策略，并保存到海康配置。\nCPU 风扇继续由原厂温控管理；物理风扇对应需实机确认。", "确认切换"))) return;
        await action("fan.official", { mode, confirmed: true }, "原厂系统风扇策略已提交");
      });
      $("official-fan-modes").append(button);
    });
    syncControls();
  }
  function renderFanDetail(channel) {
    if (independentFanBlocksLegacy()) { fanDetailSignature = null; clear($("fan-controls")); $("fan-controls").append(element("p", "capability-note", "独立温控运行中或原厂恢复尚未确认。请先在网页确认两路恢复原厂管理，再使用其他风扇控制。")); return; }
    const signature = JSON.stringify(channel ? [channel.id, channel.driver, channel.owned, channel.mode, channel.curve, toolbox && toolbox.fans && toolbox.fans.last_error] : null);
    if (signature === fanDetailSignature) return;
    fanDetailSignature = signature; clear($("fan-controls"));
    if (!channel) { $("fan-controls").append(element("p", "capability-note", "此通道当前不可用")); return; }
    const card = element("article", "card control-card fan-channel");
    const heading = element("div", "fan-heading");
    heading.append(element("strong", "", channel.label || channel.driver || channel.id), element("span", "fan-rpm", percentage(channel.duty_percent) + "% PWM"));
    const note = element("p", "capability-note", channel.id + (channel.owned ? " · 已由 Toolbox 接管" : " · 尚未接管") + (channel.rpm_verified && finite(channel.rpm) ? " · " + channel.rpm + " RPM" : " · 未确认转速对应关系"));
    if (channel.rpm_reason) note.append(document.createTextNode(" · " + channel.rpm_reason));
    const consent = element("label", "check-row");
    const takeover = document.createElement("input"); takeover.type = "checkbox"; takeover.className = "fan-input"; takeover.checked = channel.owned === true;
    consent.append(takeover, element("span", "", "允许 Toolbox 临时接管此通道"));
    const slider = document.createElement("input"); slider.type = "range"; slider.min = "50"; slider.max = "100"; slider.step = "5"; slider.value = String(percent(channel.duty_percent) === null ? 80 : Math.max(50, Math.round(channel.duty_percent))); slider.className = "touch-range fan-input";
    const output = element("output", "", slider.value + "%");
    const label = element("label", "control-label", "手动 PWM 占空比"); label.append(output);
    slider.addEventListener("input", () => { output.textContent = slider.value + "%"; });
    async function setMode(mode, title, curve) {
      if (!takeover.checked && mode !== "restore") { toast("请先勾选允许接管风扇", true); return; }
      const message = mode === "restore" ? "恢复此通道接管前的控制模式和 PWM 设置。" : mode === "auto" || mode === "curve" ? "Toolbox 将按温度调整该通道；传感器缺失或过热时使用满速。确认允许接管？" : "将调整 " + channel.id + "，确认允许 Toolbox 接管此通道？";
      if (!(await confirmOperation(title, message, "确认调整"))) return;
      if (mode === "restore") await action("fan.restore", { channel: channel.id }, "已请求恢复原风扇控制");
      else {
        const payload = { channel: channel.id, mode, value: Number(slider.value), takeover: true };
        if (curve) payload.curve = curve;
        await action("fan.set", payload, "风扇控制已提交");
      }
    }
    const buttons = element("div", "mode-grid");
    [["低速", "low"], ["中速", "medium"], ["高速", "high"], ["自动温控", "auto"], ["应用手动", "manual"], ["恢复原控制", "restore"]].forEach(([title, mode]) => {
      const button = element("button", "button button-secondary requires-write", title); button.type = "button"; button.dataset.allowed = String(mode !== "restore" || channel.owned === true);
      button.addEventListener("click", () => setMode(mode, title)); buttons.append(button);
    });
    const curveEditor = element("details", "curve-editor");
    const summary = element("summary", "check-row", "自定义温控曲线"); curveEditor.append(summary);
    curveEditor.append(element("p", "capability-note", "5–8 个递增温度点；PWM 最低 50%。超温或温度缺测时使用满速。"));
    const curveHead = element("div", "curve-heading"); curveHead.append(element("span", "", "温度 °C"), element("span", "", "PWM %"), element("span", "")); curveEditor.append(curveHead);
    const pointList = element("div", "curve-points");
    function addPoint(point) {
      const row = element("div", "curve-row");
      const temperature = document.createElement("input"); temperature.type = "number"; temperature.min = "0"; temperature.max = "100"; temperature.step = "1"; temperature.value = String(point.temperature); temperature.className = "fan-input"; temperature.setAttribute("aria-label", "曲线温度");
      const duty = document.createElement("input"); duty.type = "number"; duty.min = "50"; duty.max = "100"; duty.step = "1"; duty.value = String(point.duty); duty.className = "fan-input"; duty.setAttribute("aria-label", "曲线 PWM");
      const remove = element("button", "button button-secondary curve-remove requires-write", "−"); remove.type = "button"; remove.setAttribute("aria-label", "移除此温度点");
      remove.addEventListener("click", () => { if (pointList.children.length <= 5) toast("至少保留 5 个温度点", true); else row.remove(); });
      row.append(temperature, duty, remove); pointList.append(row);
    }
    const points = list(channel.curve).length >= 5 ? channel.curve : [{ temperature: 30, duty: 60 }, { temperature: 40, duty: 60 }, { temperature: 50, duty: 75 }, { temperature: 60, duty: 90 }, { temperature: 75, duty: 100 }]; points.forEach(addPoint); curveEditor.append(pointList);
    const curveButtons = element("div", "button-row");
    const add = element("button", "button button-secondary requires-write", "添加温度点"); add.type = "button";
    add.addEventListener("click", () => { if (pointList.children.length >= 8) toast("最多设置 8 个温度点", true); else { const inputs = pointList.lastElementChild.querySelectorAll("input"); addPoint({ temperature: Math.min(100, Number(inputs[0].value) + 5), duty: 100 }); syncControls(); } });
    const apply = element("button", "button requires-write", "应用曲线"); apply.type = "button";
    apply.addEventListener("click", () => {
      const curve = Array.from(pointList.children).map((row) => { const inputs = row.querySelectorAll("input"); return { temperature: Number(inputs[0].value), duty: Number(inputs[1].value) }; });
      if (curve.length < 5 || curve.length > 8 || curve.some((point, index) => !Number.isInteger(point.temperature) || point.temperature < 0 || point.temperature > 100 || !Number.isInteger(point.duty) || point.duty < 50 || point.duty > 100 || index && point.temperature <= curve[index - 1].temperature)) { toast("请填写 5–8 个递增温度点，PWM 为 50–100%", true); return; }
      setMode("curve", "应用温控曲线", curve);
    });
    curveButtons.append(add, apply); curveEditor.append(curveButtons);
    card.append(heading, note, consent, label, slider, buttons, curveEditor); $("fan-controls").append(card);
    if (toolbox && toolbox.fans && toolbox.fans.last_error) $("fan-controls").append(element("p", "capability-note fan-error", toolbox.fans.last_error));
    syncControls();
  }

  function renderPower(power) {
    const pending = power && power.pending;
    powerOperation = pending && pending.operation;
    powerDeadline = pending && number(pending.seconds_remaining) !== null ? Date.now() + pending.seconds_remaining * 1000 : null;
    $("power-pending").hidden = $("global-power-notice").hidden = !pending && !(power && power.executing);
    if (power && power.executing) { text("power-countdown", "电源操作已交给宿主系统"); text("global-power-label", "电源操作正在执行"); }
    else updatePowerCountdown();
    text("power-reason", power && power.last_error ? power.last_error : cap("power").available ? "确认后有 5 秒可取消倒计时" : cap("power").reason);
  }
  function updatePowerCountdown() {
    if (powerDeadline === null) return;
    const seconds = Math.max(0, Math.ceil((powerDeadline - Date.now()) / 1000));
    const message = (powerOperation === "poweroff" ? "关闭 NAS" : "重启 NAS") + (seconds ? ` · ${seconds} 秒后执行` : " · 等待宿主确认");
    text("power-countdown", message); text("global-power-label", message);
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
      if (powerDeadline !== null) { text("power-countdown", "连接中断 · 电源操作状态未知"); text("global-power-label", "连接中断 · 电源状态未知"); }
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
    text("rgb-reason", cap("rgb").available ? "当前使用已验证的灯光接口" : cap("rgb").reason);
    text("rgb-probe-reason", cap("rgb").reason || "已验证，可进入灯光设置");
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
    text("rgb-save", "应用灯光");
    if (!rgbBrightnessDirty) {
      if (percent(rgb.brightness) !== null) $("rgb-brightness").value = rgb.brightness;
      text("rgb-brightness-value", percentage(rgb.brightness) + "%");
    }
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
  $("rgb-brightness").addEventListener("input", () => { rgbBrightnessDirty = true; text("rgb-brightness-value", $("rgb-brightness").value + "%"); });
  $("rgb-probe").addEventListener("click", async () => {
    const value = $("rgb-bus").value, bus = value === "" ? null : Number(value);
    if (!Number.isInteger(bus) || !list(toolbox && toolbox.rgb && toolbox.rgb.availableBuses).includes(bus)) { toast("请明确选择宿主检测到的 I²C 总线", true); return; }
    if (!(await confirmOperation("验证灯光接口", "只在 i2c-" + bus + " 的设备地址 0x26 验证接口。验证通过后才开放调色。", "开始验证"))) return;
    if (await action("rgb.probe", { bus }, "灯光接口验证通过")) { if (cap("rgb").available) openView("rgb", {}, "灯光", true); }
  });
  $("rgb-save").addEventListener("click", async () => {
    const rgb = toolbox && toolbox.rgb;
    if (!cap("rgb").available || !rgb || !Number.isInteger(rgb.bus)) { toast("灯光接口尚未验证", true); return; }
    const payload = { bus: rgb.bus, mode: Number($("rgb-mode").value), color: $("rgb-color").value };
    if (rgbBrightnessDirty) payload.brightness = Number($("rgb-brightness").value);
    if (await action("rgb.set", payload, "灯光设置已提交")) { rgbDirty = false; rgbBrightnessDirty = false; }
  });
  async function requestPower(operation) {
    const label = operation === "poweroff" ? "关闭 NAS" : "重启 NAS";
    const message = "此操作将影响所有用户、容器和文件访问。请先保存正在进行的工作。\n确认后进入 5 秒可取消倒计时。";
    if (await confirmOperation(label, message, "确认" + label, true)) await action("power.request", { operation, confirmed: true }, "电源操作已安排，可在倒计时结束前取消");
  }
  $("power-reboot").addEventListener("click", () => requestPower("reboot"));
  $("power-off").addEventListener("click", () => requestPower("poweroff"));
  $("power-cancel").addEventListener("click", () => action("power.cancel", {}, "已取消电源操作"));
  $("global-power-cancel").addEventListener("click", () => action("power.cancel", {}, "已取消电源操作"));

  function emptyList(id, message) {
    clear($(id)); $(id).append(element("p", "capability-note", message));
  }
  function detailRow(title, description, health) {
    const row = element("div", "detail-row");
    row.append(element("strong", "", title), element("span", health || "", description));
    return row;
  }
  function renderApps(data) {
    const containers = data && data.containers, vms = data && data.vms;
    const signature = JSON.stringify([containers, vms, toolboxOnline]);
    if (signature === appsSignature) { syncControls(); return; }
    appsSignature = signature;
    $("container-entry").hidden = !(containers && containers.available);
    $("vm-entry").hidden = !(vms && vms.available);
    $("apps-empty").hidden = Boolean(containers && containers.available || vms && vms.available);
    [{ name: "container", value: containers, target: "container-list", count: "container-count", entry: "container-entry" }, { name: "vm", value: vms, target: "vm-list", count: "vm-count", entry: "vm-entry" }].forEach((group) => {
      const values = list(group.value && group.value.items);
      text(group.count, values.length + " 个");
      $(group.entry).querySelector("small").textContent = values.length + " 个 · " + values.filter((item) => item.state === "running").length + " 个运行中";
      clear($(group.target));
      if (!group.value || !group.value.available) { $(group.target).append(element("p", "capability-note", "服务当前不可用")); return; }
      if (!values.length) { $(group.target).append(element("p", "capability-note", "尚未发现应用")); return; }
      values.forEach((item) => {
        const labels = { running: "运行中", exited: "已停止", created: "未启动", paused: "已暂停", restarting: "重启中", dead: "异常", shutoff: "已关机", "shut off": "已关机", suspended: "已挂起" };
        const name = item.name || String(item.id).slice(0, 12);
        const description = [labels[item.state] || item.state || "未知状态", group.name === "container" ? "CPU " + (number(item.cpu_percent) === null ? "--" : item.cpu_percent.toFixed(1)) + "% · " + bytes(item.memory_bytes) : "", item.protected ? "Toolbox 保护" : ""].filter(Boolean).join(" · ");
        $(group.target).append(menuButton(name, description, () => openView("app-detail", { kind: group.name, id: item.id }, name), "app-summary"));
      });
    });
    if (currentDetail && currentDetail.name === "app-detail") renderAppDetail(currentDetail.params.kind, currentDetail.params.id);
    syncControls();
  }
  function renderAppDetail(kind, id) {
    const group = insights && insights[kind === "container" ? "containers" : "vms"];
    const item = list(group && group.items).find((entry) => entry.id === id);
    clear($("app-detail"));
    if (!item) { $("app-detail").append(element("p", "capability-note", "此应用当前不可用")); return; }
    const card = element("article", "card detail-content");
    card.append(detailRow("名称", item.name || item.id), detailRow("状态", item.status || item.state || "未知"));
    if (kind === "container") {
      card.append(detailRow("镜像", item.image || "未提供"), detailRow("CPU", (number(item.cpu_percent) === null ? "--" : item.cpu_percent.toFixed(1)) + "%"), detailRow("内存", bytes(item.memory_bytes)));
      if (item.ports) card.append(detailRow("端口映射", readable(item.ports)));
    }
    card.append(detailRow("标识", item.id));
    if (item.protected) card.append(element("p", "capability-note", "Toolbox 容器受保护，请在 NAS 管理页管理"));
    const running = item.state === "running";
    const operations = kind === "container" ? running ? [["停止", "stop"], ["重启", "restart"]] : ["exited", "created"].includes(item.state) ? [["启动", "start"]] : [] : running ? [["关机", "shutdown"], ["重启", "reboot"], ["挂起", "suspend"]] : ["paused", "suspended"].includes(item.state) ? [["恢复", "resume"]] : ["shut off", "shutoff"].includes(item.state) ? [["启动", "start"]] : [];
    const buttons = element("div", "item-actions");
    operations.forEach(([label, operation]) => {
      const button = element("button", "button button-secondary requires-write", label); button.type = "button"; button.dataset.allowed = String(!item.protected && Boolean(group && group.available));
      button.addEventListener("click", async () => {
        if (!(await confirmOperation(label + (kind === "container" ? "容器" : "虚拟机"), "确定" + label + "「" + (item.name || item.id) + "」？正在运行的任务可能受到影响。", "确认" + label, !["start", "resume"].includes(operation)))) return;
        if (await action(kind + ".action", { id: item.id, operation, confirmed: true }, "应用操作已提交")) await pollInsights(true);
      }); buttons.append(button);
    });
    if (operations.length) card.append(buttons);
    $("app-detail").append(card); syncControls();
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
        $("physical-disks").append(menuButton(disk.model || disk.id || "磁盘", description, () => openView("disk-detail", { id: disk.id || disk.device }, disk.model || disk.id || "磁盘")));
      });
    }
    const interfaces = networkInterfaces();
    clear($("interface-list"));
    if (!interfaces.length) emptyList("interface-list", "网络接口信息暂不可用");
    interfaces.forEach((network) => {
      const description = "IPv4 " + interfaceAddresses(network, "ipv4").length + " 个 · IPv6 " + interfaceAddresses(network, "ipv6").length + " 个地址";
      $("interface-list").append(menuButton(network.name + " · " + (network.operstate || "未知"), description, () => openView("interface-detail", { id: network.name }, network.name)));
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
    updateInformationModules();
  }
  let insightsRunning = false;
  async function pollInsights(force = false) {
    if (insightsRunning || (!force && Date.now() - lastInsightsPoll < 5000)) return;
    insightsRunning = true; lastInsightsPoll = Date.now();
    try { renderInsights(await request("/insights")); }
    catch (_) { renderInsights(null); }
    finally { insightsRunning = false; }
  }
  function renderLightPolicy(data) {
    lightPolicy = data && data.config ? data : null;
    $("small-light-policy").hidden = !lightPolicy;
    if (lightPolicy) {
      const runtime = lightPolicy.runtime || lightPolicy;
      const line = lightPolicy.busy ? "灯光控制正在切换" : lightPolicy.last_error ? "灯光应用未确认：" + lightPolicy.last_error : lightPolicy.active ? "灯光策略运行中 · " + (runtime.reason || "") : "持续灯光策略未接管";
      text("small-light-policy-state", line + (finite(runtime.reference_c) ? "\n参考 max：" + runtime.reference_c + " °C · " + (runtime.source_label || "--") : "") + (runtime.target && finite(runtime.target.brightness) ? "\n目标亮度：" + runtime.target.brightness + "%" : ""));
    }
    syncControls();
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
      request("/fan-policy").then(renderIndependentFanPolicy).catch(() => renderIndependentFanPolicy(null)),
      request("/light-policy").then(renderLightPolicy).catch(() => renderLightPolicy(null)),
      pollInsights(),
      currentDetail && currentDetail.name === "web-config" ? pollWebConfig() : Promise.resolve(),
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
    if (currentDetail && currentDetail.name === "web-config") clearWebPairing();
    currentDetail = null; navigationStack = [];
    currentPage = pages.indexOf(name);
    $("subpage").hidden = true;
    document.querySelector(".swipe-hint").textContent = "左右滑动切换";
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
    if (!event.isPrimary) return;
    pointerStart = null;
    if (event.target.closest("input, select, button, summary, .button, .disk-list")) return;
    pointerStart = { x: event.clientX, y: event.clientY, id: event.pointerId };
  });
  viewport.addEventListener("pointerup", (event) => {
    if (!pointerStart || event.pointerId !== pointerStart.id) return;
    const dx = event.clientX - pointerStart.x, dy = event.clientY - pointerStart.y;
    pointerStart = null;
    if (Math.abs(dx) >= 70 && Math.abs(dx) > Math.abs(dy) * 1.8) {
      suppressGestureClick = true;
      if (currentDetail) { if (dx > 0) backView(); }
      else showPage(dx < 0 ? 1 : -1);
    }
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
      if (name === "pointerdown") suppressGestureClick = false;
      if (name === "click" && suppressGestureClick) {
        suppressGestureClick = false;
        event.preventDefault(); event.stopImmediatePropagation();
        return;
      }
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
    suppressGestureClick = false;
    lastInteraction = lastCarousel = Date.now();
    if (resolveConfirmation) {
      if (event.key === "Escape") { event.preventDefault(); resolveConfirmation(false); }
      if (event.key === "Tab") { event.preventDefault(); ($(document.activeElement === $("confirm-cancel") ? "confirm-accept" : "confirm-cancel")).focus(); }
      return;
    }
    if (event.target.closest("input, select, textarea")) return;
    if (event.key === "Escape" && currentDetail) { event.preventDefault(); backView(); return; }
    if ((event.key === "Enter" || event.key === " ") && event.target.matches('[role="button"][data-open-view], [role="button"][data-open-page]')) { event.preventDefault(); event.target.click(); return; }
    if (currentDetail) { if (event.key === "ArrowLeft") backView(); return; }
    if (event.key === "ArrowRight") showPage(1);
    if (event.key === "ArrowLeft") showPage(-1);
  });
  window.addEventListener("offline", () => { render(null); setConnection("offline"); renderToolbox(null); renderInsights(null); });
  window.addEventListener("online", poll);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { updateClock(); poll(); } });
  updateClock(); render(null); renderToolbox(null); renderInsights(null); setConnection("connecting");
  poll();
  window.setInterval(() => {
    updateClock(); updatePowerCountdown(); updatePairingCountdown();
    const interval = number(settings.page_interval);
    if (interval && !currentDetail && !sleeping && !resolveConfirmation && !actionBusy && !settingsDirty && Date.now() - lastInteraction >= interval * 1000 && Date.now() - lastCarousel >= interval * 1000) showPage(1);
  }, 1000);
  window.setInterval(poll, POLL_INTERVAL);
})();
