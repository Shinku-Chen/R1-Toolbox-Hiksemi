"use strict";

(() => {
  const STATUS_URL = "http://127.0.0.1:18731/api/status";
  const POLL_INTERVAL = 2000;
  const REQUEST_TIMEOUT = 1800;
  const pages = ["overview", "storage", "network"];
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

  async function poll() {
    if (requestRunning) return;
    requestRunning = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const response = await fetch(STATUS_URL, { method: "GET", cache: "no-store", credentials: "omit", signal: controller.signal });
      if (!response.ok) throw new Error("status-unavailable");
      const data = await response.json();
      if (!data || data.schema_version !== 1 || data.available !== true) throw new Error("status-unavailable");
      render(data);
      setConnection("online", data.timestamp);
    } catch (_) {
      render(null);
      setConnection("offline");
    } finally {
      window.clearTimeout(timeout);
      requestRunning = false;
    }
  }

  function showPage(index) {
    currentPage = (index + pages.length) % pages.length;
    pages.forEach((name, position) => {
      const active = position === currentPage;
      const page = document.getElementById(`page-${name}`);
      page.hidden = !active;
      page.classList.toggle("is-active", active);
      const button = document.querySelector(`[data-page="${name}"]`);
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  document.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => showPage(pages.indexOf(button.dataset.page)));
  });
  const viewport = document.getElementById("viewport");
  viewport.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary) return;
    pointerStart = { x: event.clientX, y: event.clientY, id: event.pointerId };
  });
  viewport.addEventListener("pointerup", (event) => {
    if (!pointerStart || event.pointerId !== pointerStart.id) return;
    const dx = event.clientX - pointerStart.x;
    const dy = event.clientY - pointerStart.y;
    pointerStart = null;
    if (Math.abs(dx) >= 55 && Math.abs(dx) > Math.abs(dy) * 1.5) showPage(currentPage + (dx < 0 ? 1 : -1));
  });
  viewport.addEventListener("pointercancel", () => { pointerStart = null; });
  document.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") showPage(currentPage + 1);
    if (event.key === "ArrowLeft") showPage(currentPage - 1);
  });
  window.addEventListener("offline", () => { render(null); setConnection("offline"); });
  window.addEventListener("online", poll);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { updateClock(); poll(); } });
  updateClock();
  render(null);
  setConnection("connecting");
  poll();
  window.setInterval(updateClock, 1000);
  window.setInterval(poll, POLL_INTERVAL);
})();
