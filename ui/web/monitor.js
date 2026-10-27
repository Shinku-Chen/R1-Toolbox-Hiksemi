"use strict";
(() => {
  const $ = id => document.getElementById(id), list = v => Array.isArray(v) ? v : [];
  const finite = v => typeof v === "number" && Number.isFinite(v);
  let current = null, online = false, authorized = false, actionBusy = false, page = "overview", pollPromise = null, historyEpoch = 0, containerFilter = "all", selectedApp = null, detailActionSignature = "";
  const titles = {overview:"运行总览",trends:"历史与提醒",storage:"存储空间",network:"网络连接",apps:"应用与服务",device:"设备详情",controls:"控制中心",settings:"设置"};
  function el(tag, cls, value) { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = String(value); return n; }
  function put(id, value) { $(id).textContent = value; }
  function empty(node, text = "尚未取得采样") { node.replaceChildren(el("p", "small", text)); }
  function fmt(v, decimals = 0) { return finite(v) ? v.toFixed(decimals) : "--"; }
  function bytes(v, suffix = "") { if (!finite(v) || v < 0) return "--"; const u = ["B","KB","MB","GB","TB","PB"], k = v === 0 ? 0 : Math.min(5, Math.floor(Math.log(v)/Math.log(1024))); return (v / 1024 ** k).toFixed(k && v / 1024 ** k < 100 ? 1 : 0) + " " + u[k] + suffix; }
  function duration(v) { if (!finite(v)) return "运行时长未知"; return "已运行 " + (v >= 86400 ? Math.floor(v / 86400) + " 天 " : "") + Math.floor(v % 86400 / 3600) + " 小时"; }
  function stamp(v) { const d = new Date(typeof v === "number" ? v * 1000 : v); return Number.isNaN(d.getTime()) ? "采样时间未知" : d.toLocaleString("zh-CN",{hour12:false}); }
  function row(node, label, value) { const r = el("div","reading-row"); r.append(el("span","",label),el("strong","",value === null || value === undefined || value === "" ? "--" : value)); node.append(r); }
  function lines(v) { return list(v).length ? list(v).join("\n") : "未取得数据"; }
  const metadataText = value => typeof value === "string" ? value.trim() : "";
  // The public API keeps release metadata structured; render only known scalar
  // fields here, while accepting older snapshots that supplied a single string.
  function osLabel(value) {
    if (typeof value === "string") return metadataText(value);
    if (!value || typeof value !== "object" || Array.isArray(value)) return "";
    const name = metadataText(value.NAME) || metadataText(value.ID);
    const version = metadataText(value.VERSION) || metadataText(value.VERSION_ID);
    const label = metadataText(value.PRETTY_NAME) || (name && version && name !== version && !name.endsWith(" " + version) ? name + " " + version : name || version);
    return [label, metadataText(value.BUILD_ID) && "构建 " + metadataText(value.BUILD_ID)].filter(Boolean).join("\n");
  }
  function firmwareLabel(value) {
    if (typeof value === "string") return metadataText(value);
    if (!value || typeof value !== "object" || Array.isArray(value)) return "";
    const version = ["version", "firmware_version", "hiksemi-release", "histor-release"].map(key => metadataText(value[key])).find(Boolean);
    const model = metadataText(value.model) || metadataText(value.product_name);
    const build = finite(value.build) ? String(value.build) : metadataText(value.build);
    const builtAt = [metadataText(value.build_date), metadataText(value.build_time)].filter(Boolean).join(" ");
    return [version, model && "型号 " + model, build && "构建 " + build, builtAt && "构建时间 " + builtAt].filter(Boolean).join("\n");
  }
  function metric(label, value, unit, note, ratio) { const c = el("article","metric-card"); c.append(el("div","metric-label",label)); const n = el("div","metric-value",value); if (unit) n.append(el("span","metric-unit",unit)); c.append(n,el("p","metric-note",note || "实时采样")); if (finite(ratio)) { const bar = el("div","meter"), fill = el("span"); fill.style.width = Math.max(0,Math.min(100,ratio)) + "%"; bar.append(fill); c.append(bar); } return c; }
  async function read(path) { const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 5000); try { const response = await fetch(path,{credentials:"omit",cache:"no-store",signal:abort.signal}); if (!response.ok) throw new Error("HTTP " + response.status); return await response.json(); } finally { clearTimeout(timer); } }
  function switchPage(name) { if (!titles[name]) return; page = name; document.querySelectorAll("[data-web-view]").forEach(n => n.hidden = n.dataset.webView !== name); document.querySelectorAll("[data-web-page]").forEach(n => n.setAttribute("aria-current",n.dataset.webPage === name ? "page" : "false")); put("page-title",titles[name]); if (location.hash !== "#" + name) history.replaceState(null,"","#"+name); window.scrollTo({top:0,behavior:"auto"}); document.dispatchEvent(new CustomEvent("r1:web-page",{detail:name})); if (name === "trends") loadTrends(); }
  document.querySelectorAll("[data-web-page]").forEach(n => n.addEventListener("click",() => switchPage(n.dataset.webPage)));
  document.addEventListener("click",event => { const link = event.target.closest("[data-go]"); if (link) switchPage(link.dataset.go); });
  function openSettings(tab) {
    switchPage("settings");
    document.dispatchEvent(new CustomEvent("r1:settings-open", {detail:tab || "panel"}));
    if (!authorized) $("code").focus();
  }
  $("unlock-settings").addEventListener("click",() => openSettings("panel"));
  document.querySelectorAll("[data-open-settings]").forEach(node => node.addEventListener("click", () => openSettings(node.dataset.openSettings)));
  window.addEventListener("hashchange",() => switchPage(location.hash.slice(1)));
  $("web-theme").addEventListener("click",() => { const root = document.documentElement; root.dataset.webTheme = root.dataset.webTheme === "light" ? "dark" : "light"; });
  function clock() { const d = new Date(); put("web-clock",d.toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false})); put("web-date",d.toLocaleDateString("zh-CN",{month:"long",day:"numeric",weekday:"short"})); } clock(); setInterval(clock,30000);
  function svgChart(target, series, max = 100) {
    target.replaceChildren(); const all = series.flatMap(s => s.points).filter(p => finite(p[1])); if (!all.length) { target.append(el("div","chart-empty","暂无有效历史采样")); return; }
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns,"svg"); svg.setAttribute("viewBox","0 0 600 150"); svg.setAttribute("preserveAspectRatio","none"); svg.setAttribute("role","img"); svg.setAttribute("aria-label",series.map(s => s.label).join("、") + "历史曲线");
    const start = Math.min(...series.flatMap(s => s.points.map(p => p[0]))), end = Math.max(start + 1,...series.flatMap(s => s.points.map(p => p[0]))); const ymax = max || Math.max(1,...all.map(p => p[1])) * 1.1;
    [0,50,100,149].forEach(y => { const line=document.createElementNS(ns,"line"); line.setAttribute("x1","0");line.setAttribute("x2","600");line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("class","chart-grid");svg.append(line); });
    series.forEach((s,i) => { let d="",drawing=false; s.points.forEach(p => { if (!finite(p[1]) || !finite(p[0])) { drawing=false;return; } const x=(p[0]-start)/(end-start)*600,y=146-Math.min(1,Math.max(0,p[1]/ymax))*140;d+=(drawing?"L":"M")+x.toFixed(2)+","+y.toFixed(2);drawing=true; }); const path=document.createElementNS(ns,"path"); path.setAttribute("d",d);path.setAttribute("class","chart-line"+(i%2?" secondary":""));svg.append(path); }); target.append(svg);
  }
  function renderFans(data) {
    const box=$("overview-fans"), policy=data.fan_policy || {};box.replaceChildren();
    for (const [id,name] of [["cpu","CPU 风扇"],["system","系统风扇"]]) {
      const r=policy.channels && policy.channels[id] || {}, item=el("div","fan-reading"), reason=String(r.reason||"");
      const uncertain=/未确认|待确认/.test(reason)||policy.recovery_confirmed===false||(r.active===false && r.level && r.level!=="vendor");
      const level=uncertain ? "控制 / 恢复未确认" : policy.busy ? "正在切换控制" : ({vendor:"原厂管理",quiet:"静音",normal:"普通",full:"全速",manual:"手动",failsafe:"保护全速"}[r.level]||"状态未知");
      const target=uncertain ? "请求输出未确认" : finite(r.target_percent)?"请求 "+fmt(r.target_percent)+"%":"请求输出未知";
      item.append(el("h3","",name),el("p","",level+" · "+target),el("strong","fan-temp",fmt(r.reference_c,1)+"°"),el("p","",(r.source_label||"温度来源未提供")+" · "+(finite(r.rpm)?r.rpm+" RPM":"RPM 未知")));
      if(reason || policy.last_error)item.append(el("p","",reason||policy.last_error));box.append(item);
    }
  }

  function renderOverview(data) { const s=data.status||{},i=data.insights||{},sys=i.system||{},mem=s.memory||sys.memory||{},cpu=s.cpu||{},dev=i.device||{},interfaces=list(i.interfaces); const totalRate=key=>{const values=interfaces.map(n=>n[key]).filter(finite);return values.length?values.reduce((a,v)=>a+v,0):null;}; const rx=s.network && finite(s.network.rx_bytes_per_sec)?s.network.rx_bytes_per_sec:totalRate("rx_bytes_per_sec"),tx=s.network && finite(s.network.tx_bytes_per_sec)?s.network.tx_bytes_per_sec:totalRate("tx_bytes_per_sec");
    put("host-name",s.hostname || dev.identity && dev.identity.hostname || "本地 NAS"); put("host-model",sys.model||dev.identity && dev.identity.dmi && dev.identity.dmi.model||"NAS · 本地监控");put("host-subtitle",duration(s.uptime_seconds||sys.uptime_seconds)+" · "+(sys.cpu_model||"设备状态实时采集"));
    const metrics=$("overview-metrics");metrics.replaceChildren(metric("CPU 使用率",fmt(cpu.usage_percent ?? sys.cpu_usage_percent,1),"%",fmt(cpu.temp_c,1)+" °C · "+(sys.cpu_logical_cores||"--")+" 逻辑核心",cpu.usage_percent ?? sys.cpu_usage_percent),metric("内存使用",fmt(mem.usage_percent,1),"%",bytes(mem.used_bytes)+" / "+bytes(mem.total_bytes),mem.usage_percent),metric("网络接收",bytes(rx),"/s","发送 "+bytes(tx,"/s")),metric("CPU 温度",fmt(cpu.temp_c,1),"°C","真实传感器读数"));
    const rows=list(i.history);svgChart($("overview-chart"),[{label:"CPU",points:rows.map((v,n)=>[n,v.cpu_usage_percent])},{label:"内存",points:rows.map((v,n)=>[n,v.memory_usage_percent])}]);renderFans(data);
    const storage=$("overview-storage");storage.replaceChildren(); const volumes=list(s.disks);if (!volumes.length)empty(storage,"尚未取得存储空间");volumes.slice(0,3).forEach(d=>{row(storage,d.mount||d.name||"存储空间",bytes(d.used_bytes)+" / "+bytes(d.total_bytes));});
    const network=$("overview-network");network.replaceChildren();interfaces.slice(0,3).forEach(n=>row(network,n.name,"↓ "+bytes(n.rx_bytes_per_sec,"/s")+"\n↑ "+bytes(n.tx_bytes_per_sec,"/s"))); if(!interfaces.length)empty(network,"尚未取得接口采样");
    const power=data.analytics && data.analytics.current && data.analytics.current.power;if(power && power.available && finite(power.cpu_package_w))metrics.append(metric("CPU 封装功率",fmt(power.cpu_package_w,1),"W","RAPL 实测 · 非整机功率"));
  }
  function diskCard(d) { const card=el("article","card"),good=d.health && d.health.passed;card.append(el("p","eyebrow",d.device||d.id||"DISK"),el("h2","",d.model||"未识别型号"));row(card,"容量",bytes(d.size_bytes));row(card,"温度",finite(d.temp_c)?fmt(d.temp_c,1)+" °C":d.standby?"休眠 · 不唤醒采样":"--");row(card,"SMART",good===true?"已有观测通过":good===false?"检测到异常":"未取得健康状态");row(card,"运行状态",d.standby?"休眠":d.type||"已读取设备"); if(d.health && d.health.reason)card.append(el("p","small",d.health.reason));const details=el("details");details.append(el("summary","","查看采集字段"),el("pre","",JSON.stringify(d,null,2)));card.append(details);return card; }
  function renderStorage(data) { const s=data.status||{},i=data.insights||{},v=$("storage-volumes"),d=$("storage-disks");v.replaceChildren();list(s.disks).forEach(n=>v.append(metric(n.mount||n.name||"存储空间",fmt(n.usage_percent,1),"%",bytes(n.used_bytes)+" / "+bytes(n.total_bytes),n.usage_percent)));if(!v.children.length)empty(v,"空间数据暂不可读取");d.replaceChildren();list(i.disks && i.disks.items).forEach(n=>d.append(diskCard(n)));if(!d.children.length)empty(d,i.disks && i.disks.reason||"物理硬盘信息暂不可读取");const raid=$("storage-raid");raid.replaceChildren();list(i.device && i.device.storage && i.device.storage.raid).forEach((n,index)=>row(raid,n.name||n.device||"阵列 "+(index+1),typeof n === "string"?n:JSON.stringify(n)));if(!raid.children.length)empty(raid,"未取得阵列信息"); }
  function gate(g) { return g.gateway===null?"直连路由（无下一跳网关）":g.gateway===undefined?"网关地址未提供":g.gateway; }
  function renderNetwork(data) { const i=data.insights||{},device=i.device||{},box=$("network-interfaces"),values=new Map(list(device.network).map(v=>[v.name,v])); list(i.interfaces).forEach(v=>values.set(v.name,{...values.get(v.name),...v}));box.replaceChildren();values.forEach(n=>{const card=el("article","card");card.append(el("p","eyebrow",n.operstate||n.state||"INTERFACE"),el("h2","",n.name));row(card,"IPv4",lines(n.ipv4)); const ipv6=list(n.ipv6_addresses);row(card,"IPv6",ipv6.length?ipv6.map(a=>a.address+(Number.isInteger(a.prefix_length)?"/"+a.prefix_length:"")+"\n"+(a.scope||"scope 未提供")).join("\n\n"):list(n.ipv6).length?lines(n.ipv6):n.address_status && n.address_status.ipv6!=="ok"?"IPv6 地址读取未确认":"未取得 IPv6 地址");row(card,"接收 / 发送",bytes(n.rx_bytes_per_sec,"/s")+" / "+bytes(n.tx_bytes_per_sec,"/s"));row(card,"累计收 / 发",bytes(n.rx_bytes)+" / "+bytes(n.tx_bytes));row(card,"链路",finite(n.speed_mbps)?n.speed_mbps+" Mbps · "+(n.duplex||"--"):"速率未知");row(card,"MAC",n.mac);row(card,"MTU",n.mtu);box.append(card);});if(!box.children.length)empty(box,"尚未取得网络接口"); const config=device.network_config||{},routes=$("network-routes");routes.replaceChildren();for(const family of ["ipv4","ipv6"]){const items=list(config.default_routes && config.default_routes[family]);items.forEach(g=>row(routes,family.toUpperCase()+" · "+(g.interface||"--"),gate(g)+"\nmetric："+(g.metric??"--")+"\n来源\n"+(g.source||"未提供")));if(!items.length)row(routes,family.toUpperCase(),config.route_status && config.route_status[family]==="ok"?"已读取 · 无默认路由":"路由读取未确认");}const dns=$("network-dns"),n=config.dns||{};dns.replaceChildren();row(dns,"服务器",lines(n.servers));row(dns,"上游解析器",lines(n.upstream_servers));row(dns,"搜索域",lines(n.search));row(dns,"默认域名",lines(n.domains));if(n.reason)dns.append(el("p","small",n.reason));list(n.sources).forEach(source=>{const details=el("details");details.append(el("summary","",source.source+" · "+source.status),el("p","small",source.path),el("p","small",lines(source.servers)));dns.append(details);}); }
  function confirmed(title, description) { return new Promise(resolve=>{const dialog=$("web-confirm");put("web-confirm-title",title);put("web-confirm-description",description);dialog.returnValue="cancel";dialog.addEventListener("close",()=>resolve(dialog.returnValue==="accept"),{once:true});dialog.showModal();}); }
  async function mutate(action, payload, title, description, guard) {
    if (!authorized || !online || actionBusy || (guard && !guard())) return;
    if (title && !(await confirmed(title, description))) return;
    // Confirmation can remain open while the device or authorization changes.
    if (guard) await poll();
    if (!authorized || !online || actionBusy || (guard && !guard())) {
      put("message", "设备连接、授权或容器状态已改变，本次操作未提交。请重新读取状态。"); return;
    }
    actionBusy = true; syncActions(); put("message", "正在提交设备操作，请等待状态确认…");
    try {
      const result = await new Promise((resolve, reject) => document.dispatchEvent(new CustomEvent("r1:web-action", {detail:{action,payload,resolve,reject}})));
      put("message", action === "container.action" || action === "vm.action"
        ? "命令已返回，正在刷新应用状态。请核对最新状态，勿连续提交。"
        : result.pending ? "操作已受理，请等待状态确认。" : "操作请求已返回，请核对实时状态。");
    } catch (error) {
      put("message", error.name === "AbortError" ? "操作超时，结果尚未确认；请刷新状态核对，勿连续提交。" : error.message || "操作结果未确认，请刷新状态。");
    } finally {
      // Only a read is repeated. Device operations are never retried here.
      await poll(); actionBusy = false; syncActions();
    }
  }
  function syncActions() {
    $("web-power-cancel").disabled = !authorized || !online || actionBusy;
    document.querySelectorAll("[data-device-action]").forEach(node => {
      node.disabled = !authorized || !online || actionBusy || node.dataset.protected === "true";
    });
    const power = current && current.controls && current.controls.capabilities && current.controls.capabilities.power;
    $("web-power-card").hidden = !(authorized && online && power && power.available);
    $("unlock-settings").textContent = authorized ? "管理设置" : "解锁设置";
    $("control-pair-hint").textContent = authorized
      ? "设备操作仍需逐项确认。关机后，本机网页与触摸程序不再运行。"
      : "请先解锁设置，再执行设备操作。关机后本机网页与触摸程序不再运行。";
  }
  const stateNames = {running:"运行中",exited:"已停止",created:"待启动",paused:"已暂停",restarting:"重启中",removing:"移除中",dead:"异常",stopped:"已停止","shut off":"已关机",shutoff:"已关机",shutdown:"关闭中"};
  function operations(item, isVM) {
    if (!item || item.protected === true || typeof item.id !== "string") return [];
    const allowed = isVM
      ? ({running:["shutdown","reboot","suspend"],paused:["resume"],"shut off":["start"],shutoff:["start"],stopped:["start"]}[item.state] || [])
      : ({running:["stop","restart"],exited:["start"],created:["start"]}[item.state] || []);
    return Array.isArray(item.available_actions) ? allowed.filter(value => item.available_actions.includes(value)) : allowed;
  }
  function liveItem(key, id) {
    const group = current && current.insights && current.insights[key];
    return group && group.available !== false ? list(group.items).find(item => item.id === id) : null;
  }
  function statePill(item) {
    const pill = el("span", "status-pill"), state = item.state || "unknown";
    pill.dataset.state = state;
    pill.append(el("i", "status-dot"), document.createTextNode(stateNames[state] || "状态未知"));
    return pill;
  }
  function portBadges(value, expanded = false) {
    const box = el("div", "port-badges");
    const raw = typeof value === "string" ? value : "";
    const ports = raw.split(",").map(part => part.trim()).filter(Boolean);
    ports.slice(0, expanded ? 64 : 4).forEach(port => box.append(el("span", "port-badge", port)));
    if (ports.length > 4 && !expanded) box.append(el("span", "port-badge", "+" + (ports.length - 4)));
    if (!ports.length) box.append(el("span", "port-empty", "端口信息未提供"));
    return box;
  }
  function appActions(node, item, isVM) {
    const actions = el("div", "button-row"), key = isVM ? "vms" : "containers";
    if (item.protected === true) { actions.append(el("span", "app-protected", "受保护，仅查看")); node.append(actions); return; }
    if (!authorized) {
      const unlock = el("button", "text-button", "解锁后管理 ↗"); unlock.type = "button";
      unlock.addEventListener("click", () => openSettings("connection")); actions.append(unlock); node.append(actions); return;
    }
    const labels = {start:"启动",stop:"停止",restart:"重启",shutdown:"关机",reboot:"重启",suspend:"挂起",resume:"恢复"};
    const ops = operations(item, isVM);
    ops.forEach(operation => {
      const button = el("button", "quiet-button", labels[operation]); button.type = "button";
      button.dataset.deviceAction = "true"; button.dataset.operation = operation;
      const guard = () => operations(liveItem(key, item.id), isVM).includes(operation);
      button.addEventListener("click", () => mutate(isVM ? "vm.action" : "container.action", {id:item.id,operation,confirmed:true},
        labels[operation] + " " + (item.name || item.id),
        "将对这项" + (isVM ? "虚拟机" : "容器") + "执行“" + labels[operation] + "”。可能中断相关服务，确认继续？", guard));
      actions.append(button);
    });
    if (!ops.length && !item.protected) actions.append(el("span", "app-protected", "当前状态无可用操作"));
    node.append(actions);
  }
  function openApp(key, id) {
    selectedApp = {key,id}; renderAppDetail();
    if (!$("app-detail").open) $("app-detail").showModal();
  }
  $("app-detail-close").addEventListener("click", () => $("app-detail").close());
  $("app-detail").addEventListener("close", () => { selectedApp = null; detailActionSignature = ""; });
  function renderAppDetail() {
    if (!selectedApp) return;
    const {key,id} = selectedApp, isVM = key === "vms", item = liveItem(key,id), fields = $("app-detail-fields");
    put("app-detail-kind", isVM ? "VIRTUAL MACHINE" : "DOCKER CONTAINER");
    fields.replaceChildren(); $("app-detail-ports").replaceChildren();
    if (!item) {
      put("app-detail-title", "应用状态未确认"); $("app-detail-status").replaceChildren(); $("app-detail-actions").replaceChildren(); detailActionSignature = "";
      put("app-detail-note", "这项应用已不在最新列表，或采集来源不可用。已移除操作入口，请关闭后刷新列表。");
      return;
    }
    put("app-detail-title", item.name || item.id); $("app-detail-status").replaceChildren(statePill(item));
    put("app-detail-note", item.protected === true
      ? "Toolbox 受保护 · 安装任务完成后停止属正常，不表示部署异常。"
      : online ? "以下为最新宿主采样；操作前会再次核对状态。" : "连接中断，此前读数仅供参考，设备操作已禁用。");
    for (const [name,value] of [["完整 ID",item.id],["镜像",item.image],["原始状态",item.state],["运行情况",item.status],
      ["CPU",finite(item.cpu_percent) ? fmt(item.cpu_percent,1) + "%" : null],
      ["内存",finite(item.memory_bytes) ? bytes(item.memory_bytes) : null],
      ["内存上限",finite(item.memory_limit_bytes) ? bytes(item.memory_limit_bytes) : null],
      ["分配 vCPU",item.vcpus],["分配内存",finite(item.memory_max_bytes) ? bytes(item.memory_max_bytes) : null]]) {
      if (!isVM && ["分配 vCPU","分配内存"].includes(name)) continue;
      row(fields,name,value);
    }
    $("app-detail-ports").append(portBadges(item.ports, true));
    const signature = JSON.stringify([key,id,authorized,item.protected,operations(item,isVM)]);
    if (signature !== detailActionSignature) {
      $("app-detail-actions").replaceChildren(); appActions($("app-detail-actions"),item,isVM); detailActionSignature = signature;
    }
    syncActions();
  }
  function updateAppCard(card, item, isVM) {
    const parts = card._appParts;
    parts.title.textContent = item.name || item.id;
    if (parts.state !== item.state) {
      parts.pill.replaceWith(statePill(item)); parts.pill = parts.heading.lastElementChild; parts.state = item.state;
    }
    parts.image.textContent = item.image || (isVM ? "libvirt 虚拟机" : "镜像信息未提供");
    parts.cpu.textContent = finite(item.cpu_percent) ? fmt(item.cpu_percent,1) + "%" : "--";
    parts.memory.textContent = bytes(item.memory_bytes);
    if (!isVM && parts.portsValue !== item.ports) {
      const ports = portBadges(item.ports); parts.ports.replaceWith(ports); parts.ports = ports; parts.portsValue = item.ports;
    }
    parts.status.textContent = item.protected ? "Toolbox 受保护 · " + (stateNames[item.state] || item.state || "状态未知") : item.status || item.state || "状态未提供";
    const signature = JSON.stringify([authorized,item.protected,operations(item,isVM)]);
    if (parts.actionsSignature !== signature) {
      parts.actions.replaceChildren(); appActions(parts.actions,item,isVM); parts.actionsSignature = signature;
    }
  }
  function appCard(item, isVM) {
    const card = el("article", "application-row"), heading = el("div", "app-card-heading"); card.dataset.appId = item.id;
    const title = el("button", "app-title"); title.type = "button"; title.setAttribute("aria-label",(item.name || item.id) + "，查看详情");
    title.addEventListener("click", () => openApp(isVM ? "vms" : "containers",item.id));
    const pill = statePill(item); heading.append(title,pill);
    const image = el("p","app-image"), resources = el("div","app-resources"), cpu = el("p","","CPU"), memory = el("p","","内存");
    const cpuValue = el("strong"), memoryValue = el("strong"); cpu.append(cpuValue); memory.append(memoryValue); resources.append(cpu,memory);
    const ports = portBadges(item.ports), status = el("p","app-status-text"), footer = el("div","app-actions");
    const detail = el("button","text-button","查看详情 ↗"); detail.type = "button";
    detail.addEventListener("click", () => openApp(isVM ? "vms" : "containers",item.id));
    const actions = el("div"); footer.append(detail,actions); card.append(heading,image,resources); if (!isVM) card.append(ports); card.append(status,footer);
    card._appParts = {heading,title,pill,state:item.state,image,cpu:cpuValue,memory:memoryValue,ports,portsValue:item.ports,status,actions,actionsSignature:null};
    updateAppCard(card,item,isVM); return card;
  }
  function appList(box, items, isVM, reason) {
    const existing = new Map(Array.from(box.querySelectorAll(":scope > .application-row"),card => [card.dataset.appId,card]));
    const ids = new Set(items.map(item => item.id));
    for (const [id,card] of existing) if (!ids.has(id)) card.remove();
    box.querySelectorAll(":scope > p").forEach(node => node.remove());
    items.forEach((item,index) => {
      let card = existing.get(item.id);
      if (card) updateAppCard(card,item,isVM); else card = appCard(item,isVM);
      if (box.children[index] !== card) box.insertBefore(card,box.children[index] || null);
    });
    if (!items.length) empty(box,reason);
  }
  function renderApps(data) {
    const insight = data.insights || {}, group = insight.containers || {}, items = list(group.items), available = group.available === true;
    const running = items.filter(item => item.state === "running"), count = available ? items.length : null;
    const countNode = $("docker-running"); countNode.replaceChildren(document.createTextNode(available ? String(running.length) : "--"),el("small",""," / " + (count === null ? "--" : count)));
    put("docker-count", available ? items.length + " 项" : "采集未确认");
    put("docker-summary-note", available ? running.length + " 项运行中 · " + (items.length - running.length) + " 项未运行" : group.reason || "Docker 采样暂不可读取");
    put("overview-docker-count", available ? running.length + " / " + items.length : "-- / --");
    put("overview-docker-note", available ? "运行中 / 全部容器" : "Docker 状态未确认");
    const resources = $("docker-resources"); resources.replaceChildren();
    const sum = name => available && running.every(item => finite(item[name])) ? running.reduce((total,item) => total + item[name],0) : null;
    row(resources,"运行容器 CPU",finite(sum("cpu_percent")) ? fmt(sum("cpu_percent"),1) + "%" : "未取得完整资源采样");
    row(resources,"运行容器内存",bytes(sum("memory_bytes")));
    row(resources,"可管理范围","启动 · 停止 · 重启");
    const search = $("container-search").value.trim().toLowerCase(), box = $("web-containers");
    const filtered = items.filter(item => (containerFilter === "all" || (containerFilter === "running" ? item.state === "running" : item.state !== "running"))
      && String((item.name || "") + " " + (item.image || "")).toLowerCase().includes(search));
    appList(box,available ? filtered : [],false,available ? items.length ? "没有符合筛选的容器" : "当前没有容器" : group.reason || "Docker 服务尚不可读取");
    const vms = insight.vms || {}, vmBox = $("web-vms"); $("web-vms-card").hidden = !vms.available;
    appList(vmBox,vms.available ? list(vms.items) : [],true,"当前没有虚拟机");
    const ports = $("web-ports"); ports.replaceChildren(); list(insight.ports).slice(0,60).forEach(port => row(ports,(port.protocol || "--").toUpperCase(),(port.address || "--") + ":" + port.port));
    if (!ports.children.length) empty(ports,"尚未取得监听端口");
    renderAppDetail(); syncActions();
  }
  $("container-search").addEventListener("input", () => { if (current) renderApps(current); });
  document.querySelectorAll("[data-container-filter]").forEach(button => button.addEventListener("click", () => {
    containerFilter = button.dataset.containerFilter;
    document.querySelectorAll("[data-container-filter]").forEach(node => node.setAttribute("aria-pressed",String(node === button)));
    if (current) renderApps(current);
  }));
  function renderControls(data) {
    const controls = data.controls || {}, caps = controls.capabilities || {}, screen = controls.screen || {};
    const brightness = caps.screen && caps.screen.available && finite(screen.brightness);
    $("control-brightness").replaceChildren(document.createTextNode(brightness && finite(screen.brightness) ? String(screen.brightness) : "--"),el("small","","%"));
    put("control-screen-state",caps.screen && caps.screen.available ? screen.asleep ? "已息屏" : "显示中" : "能力未确认");
    put("control-screen-note",brightness ? "背光亮度 · 首次触摸只唤醒" : "没有可信背光读数，相关设置受能力检查限制");
    const fans = $("control-fans"), policy = data.fan_policy || {}; fans.replaceChildren();
    for (const [id,name] of [["cpu","CPU 风扇"],["system","系统风扇"]]) {
      const channel = policy.channels && policy.channels[id] || {};
      row(fans,name,channel.active && finite(channel.target_percent) ? "请求 " + channel.target_percent + "%" : String(channel.reason || "原厂或当前输出未确认"));
    }
    const light = data.light_policy || {}, runtime = light.runtime || light, active = runtime.active === true || light.active === true;
    const requested = runtime.target && Object.keys(runtime.target).length ? runtime.target : light.target;
    const target = active ? requested || {} : caps.rgb && caps.rgb.available ? controls.rgb || {} : {};
    put("control-light-state",runtime.busy ? "正在切换" : active ? "策略目标" : caps.rgb && caps.rgb.available ? "已验证读数" : "接口未确认");
    put("control-light-title",active && finite(runtime.reference_c) ? runtime.reference_c + " °C · 温度联动" : ({0:"关闭",1:"常亮",2:"彩虹",3:"呼吸"}[target.mode] || "灯光状态未确认"));
    put("control-light-note",(finite(target.brightness) ? (active ? "请求亮度 " : "当前亮度 ") + target.brightness + "% · " : "") + (active ? runtime.source_label || runtime.reason || "策略输出以回执为准" : "颜色与亮度以真实接口为准"));
    $("control-light-color").style.backgroundColor = /^#[0-9a-f]{6}$/i.test(target.color || "") ? target.color : "var(--raised)";
  }
  function renderDevice(data) { const i=data.insights||{},dev=i.device||{},identity=dev.identity||{},sys=i.system||{},c=dev.cpu||{}; const box=$("web-identity");box.replaceChildren();[["主机名",identity.hostname||data.status && data.status.hostname],["系统",osLabel(identity.os)],["内核",identity.kernel],["固件",firmwareLabel(identity.firmware)],["运行时长",duration(sys.uptime_seconds||data.status && data.status.uptime_seconds)]].forEach(v=>row(box,...v));const cpu=$("web-cpu");cpu.replaceChildren();[["型号",sys.cpu_model||c.model],["架构",c.architecture],["核心 / 线程",(c.physical_cores||"--")+" / "+(sys.cpu_logical_cores||c.logical_cores||"--")],["负载 1 / 5 / 15",list(sys.load_average).map(v=>fmt(v,2)).join(" / ")],["内存",bytes(sys.memory && sys.memory.used_bytes)+" / "+bytes(sys.memory && sys.memory.total_bytes)]].forEach(v=>row(cpu,...v));const cores=$("web-cores");cores.replaceChildren();list(sys.cpu_cores).forEach(v=>{const n=el("div","core-cell","核心 "+v.id);n.append(el("strong","",fmt(v.usage_percent,1)+"%"));cores.append(n);});const thermal=$("web-thermal");thermal.replaceChildren();list(dev.thermal).forEach(v=>row(thermal,(v.driver||"")+" · "+(v.label||v.channel||v.id),finite(v.celsius)?fmt(v.celsius,1)+" °C":"--"));if(!thermal.children.length)empty(thermal,"未取得温度传感器");const gpu=$("web-gpu");gpu.replaceChildren();list(i.gpu && i.gpu.devices).forEach(v=>{row(gpu,"设备",v.name||v.vendor);row(gpu,"使用率",finite(v.busy_percent)?fmt(v.busy_percent,1)+"%":"--");row(gpu,"显存",bytes(v.vram_used_bytes)+" / "+bytes(v.vram_total_bytes));});if(!gpu.children.length)empty(gpu,i.gpu && i.gpu.reason||"图形设备采样不可用");const processes=$("web-processes");processes.replaceChildren();list(i.processes).slice(0,30).forEach(v=>row(processes,v.name+" · "+v.pid,(finite(v.cpu_percent)?fmt(v.cpu_percent,1)+"%":"--")+" · "+bytes(v.memory_bytes)));if(!processes.children.length)empty(processes,"未取得进程采样");syncActions(); }
  function renderPower(data) { const power=data.controls && data.controls.power||{},pending=power.pending;$("web-power-pending").hidden=!pending; if(pending)put("web-power-label",(pending.operation==="poweroff"?"关机":"重启")+"倒计时 · "+(pending.seconds_remaining??"--")+" 秒");$("web-power-cancel").disabled=!authorized||!online||actionBusy; }
  document.querySelectorAll("[data-power]").forEach(n=>{n.dataset.deviceAction="true";n.addEventListener("click",()=>mutate("power.request",{operation:n.dataset.power,confirmed:true},n.dataset.power==="poweroff"?"关闭 NAS":"重启 NAS","将中断所有文件访问和应用。请先保存工作。确认后进入 5 秒可取消倒计时。"));});$("web-power-cancel").addEventListener("click",()=>mutate("power.cancel",{}));
  function setConnection(ok,error) { online=ok;document.documentElement.dataset.monitorOnline=String(ok);document.dispatchEvent(new CustomEvent("r1:monitor-online",{detail:ok}));put("live-status",ok?"实时连接":"连接中断");put("sidebar-status",ok?"设备在线":"设备连接中断");$("sidebar-dot").classList.toggle("connected",ok);$("monitor-error").hidden=ok;if(!ok)put("monitor-error",error||"实时采样暂不可读取。当前状态未确认，设备操作已禁用。");syncActions(); }
  async function poll() {
    if (pollPromise) return pollPromise;
    pollPromise = (async () => {
      try {
        const data = await read("/api/v1/status");
        if (data.available === false) throw new Error(data.reason || "监控采样尚未准备好");
        current = data; setConnection(true);
        renderOverview(data); renderStorage(data); renderNetwork(data); renderApps(data); renderDevice(data); renderControls(data); renderPower(data);
        put("last-sample", "最近采样 " + stamp(data.status && data.status.timestamp || data.insights && data.insights.timestamp || data.timestamp));
        document.dispatchEvent(new CustomEvent("r1:public-snapshot", {detail:data}));
      } catch (_) {
        setConnection(false,"实时采样暂不可读取。请检查 NAS 连接；此前读数仅供参考，设备操作已禁用。");
        put("last-sample","连接中断 · 读数未确认");
      }
    })();
    try { return await pollPromise; } finally { pollPromise = null; }
  }
  document.addEventListener("r1:authorized",event=>{authorized=event.detail===true;if(current)renderApps(current);syncActions();});
  const metricLabels={cpu_usage_percent:["CPU 使用率","%",100],memory_usage_percent:["内存使用率","%",100],rx_bytes_per_sec:["网络接收","B/s",0],tx_bytes_per_sec:["网络发送","B/s",0],cpu_temperature_c:["CPU 温度","°C",0],disk_temperature_c:["硬盘最高温度","°C",0],cpu_package_w:["CPU 封装实测功率","W",0],disk_read_bytes_per_sec:["磁盘读取","B/s",0],disk_write_bytes_per_sec:["磁盘写入","B/s",0]};
  function renderHistory(data) { const charts=$("history-charts");charts.replaceChildren();put("history-note",data.reason||"仅展示已有记录。断线表示无有效采样；功率仅 CPU 封装实测，不等于整机用电。");list(data.series).forEach(s=>{const dynamic=s.id && (s.id.startsWith("temperature.")||s.id.startsWith("fan_rpm.")||["fan_cpu_target_percent","fan_system_target_percent"].includes(s.id));if(!metricLabels[s.id]&&!dynamic)return;const def=metricLabels[s.id]||[s.label||s.id,s.unit||"",s.unit==="%"?100:0],card=el("article","card"),chart=el("div","chart-area");card.append(el("p","eyebrow",s.unit||def[1]),el("h2","",s.label||def[0]),chart);svgChart(chart,[{label:s.label||def[0],points:list(s.points)}],def[2]);const points=list(s.points),values=points.map(p=>p[1]).filter(finite);card.append(el("p","small",values.length?(finite(points.at(-1)?.[1])?"最新 "+fmt(points.at(-1)[1],1):"当前缺测 · 最近有效 "+fmt(values[values.length-1],1))+" · 峰值 "+fmt(Math.max(...values),1)+" "+(s.unit||def[1]):"暂无有效采样"));if(points.length){const axis=el("div","chart-axis");axis.append(el("span","",stamp(points[0][0])),el("span","",stamp(points[points.length-1][0])));card.append(axis);}charts.append(card);});if(!charts.children.length)empty(charts,data.reason||"暂无历史记录，等待后台采样。"); }
  function renderTraffic(data) { const box=$("traffic-summary");box.replaceChildren();const totals=data.totals||{};for(const [key,label]of[["today","今日"],["month","本月"]]){const n=totals[key]||{};box.append(metric(label+"接收",bytes(n.rx_bytes),"",data.reason||"自记录开始累计"),metric(label+"发送",bytes(n.tx_bytes),"",data.coverage && data.coverage.reason||"统计覆盖以已有记录为准"));} }
  function renderAlerts(data) { const box=$("alerts-list");box.replaceChildren();const events=[...list(data.active),...list(data.events)];events.slice(0,50).forEach(a=>{const n=el("div","device-list-item");n.append(el("h3","",a.label||a.message||a.id||"状态提醒"),el("p","",[a.level,finite(a.value)?a.value+" "+(a.unit||""):null,a.timestamp?stamp(a.timestamp):null].filter(Boolean).join(" · ")));box.append(n);});if(!box.children.length)empty(box,"已有记录中暂无提醒");$("overview-alerts-card").hidden=!list(data.active).length;const preview=$("overview-alerts");preview.replaceChildren();list(data.active).slice(0,3).forEach(a=>row(preview,a.label||a.id,finite(a.value)?a.value+" "+(a.unit||""):a.level||"提醒")); }
  async function loadTrends() { const epoch=++historyEpoch,range=$("history-range").value;$("export-csv").href="/api/v1/export.csv?range="+range;put("history-note","正在读取记录…");const results=await Promise.allSettled([read("/api/v1/history?range="+range),read("/api/v1/traffic"),read("/api/v1/alerts")]);if(epoch!==historyEpoch)return;if(results[0].status==="fulfilled")renderHistory(results[0].value);else{empty($("history-charts"),"历史记录暂不可读取，请稍后重试。");put("history-note","历史服务尚未准备好或连接中断。");}if(results[1].status==="fulfilled")renderTraffic(results[1].value);else empty($("traffic-summary"),"累计流量暂不可读取");if(results[2].status==="fulfilled")renderAlerts(results[2].value);else empty($("alerts-list"),"提醒记录暂不可读取"); }
  $("history-range").addEventListener("change",loadTrends);$("load-report").addEventListener("click",async()=>{$("load-report").disabled=true;$("report-card").hidden=false;empty($("report-content"),"正在整理已有采样…");try{const report=await read("/api/v1/report"),box=$("report-content");box.replaceChildren();box.append(el("p","small","仅为已有只读采样的摘要，不是 SMART 自检或完整硬件诊断。"));row(box,"采样结论",report.summary && report.summary.label);row(box,"有效 / 缺测指标",((report.summary && report.summary.known_metrics) ?? "--")+" / "+((report.summary && report.summary.unknown_metrics) ?? "--"));row(box,"活跃提醒",report.summary && report.summary.active_alerts);row(box,"生成时间",stamp(report.generated_at));list(report.current && report.current.metrics).slice(0,16).forEach(n=>row(box,n.label||n.id,finite(n.value)?fmt(n.value,1)+" "+(n.unit||""):"未取得采样"));list(report.limitations).forEach(n=>box.append(el("p","small",n)));const details=el("details");details.append(el("summary","","查看结构化报告"),el("pre","report-json",JSON.stringify(report,null,2)));box.append(details);}catch(_){empty($("report-content"),"健康摘要暂不可读取，请稍后重试。");}finally{$("load-report").disabled=false;}});
  switchPage(titles[location.hash.slice(1)]?location.hash.slice(1):"overview");poll();setInterval(()=>{if(!document.hidden){poll();if(page==="trends")loadTrends();}},3000);
})();
