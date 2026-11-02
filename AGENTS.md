# AGENTS.md

给 AI agent（Claude Code / Codex / Cursor 等）读的仓库说明。本文件只写这个项目特有的约定；通用的个人规范由 Shinku 的 `cli/shared/*.md` 提供。

## 项目概述

R1 Toolbox 是海康 HikSemi R1 NAS 的常驻 Docker 工具箱：容器前台监督宿主命名空间中的网页监控／设置与硬件控制，小屏默认开启，可独立关闭并恢复官方界面，MCP 默认关闭。首次无网页登录口令，可在设置中添加。不改写原厂 AppImage、启动脚本和厂商服务。

- 小屏：`ui/index.html` + `ui/assets/`，采用已评审原型的六标签、点阵读数与详情页，面向 376×960 触摸屏；温度页提供受限风扇五档，设置页提供四种原生灯效和二级电源管理，复杂曲线引导到网页。
- 网页：`http://NAS_IP:18732`，直接使用 fnMonitor 2.19.0 的面板框架（MIT，源码固定在 `ui/panel-src/`），由 `runtime/fnm_compat.py` 用本机快照填充数据；R1 专有模块与设置在 `ui/panel-src/50-r1.js`。
- 宿主核心：`runtime/ui_service.py --resident` 提供本机 API（18731）、必需的网页入口（18732）；可选小屏由 `runtime/screen_manager.py` 独立管理，不得让图形环境或窗口故障阻止 Web 启动。

## 目录结构

```
runtime/           宿主端 Python：ui_service、web_config、toolbox、insights、device_info、histor、
                   fan_policy、fan_native、fan_watchdog、temperature_sources、light_policy、monitoring、
                   fnm_auth、fnm_compat
ui/index.html      小屏触摸界面入口（随镜像展开到宿主 /run）
ui/assets/         小屏 CSS/JS 与官方窗口桥
ui/panel-src/      网页面板源码（fnMonitor，MIT）与 R1 扩展 50-r1.js
ui/web/            网页面板构建产物 index.html / login.html（不要手改）
scripts/build-panel.py  构建网页面板
scripts/build-ui.py     打包镜像内 UI bundle
tools/boot-once/   容器前台监督、RAM 展开、挂载与常驻服务生命周期（保留原目录名）
tools/demo/        本地假 NAS：真实网页面板 + 真实小屏 UI 的联动演示
native/            fan_guard.c 窄范围风扇适配层
tests/             单元、HTTP、Linux 生命周期与无头浏览器检查
docs/              功能、接口、温控、灯光、监控、面板说明
```

## 构建与验证

```bash
python scripts/build-panel.py          # 修改 ui/panel-src/ 后必须执行
python scripts/build-api-contracts.py  # 修改文件／Docker 接口定义后同步 JSON；CI 使用 --check
python scripts/build-prototype.py      # 修改小屏样式或图标后同步离线原型
python scripts/build-ui.py             # 打包 UI bundle（镜像构建也会执行）
python -m unittest discover -s tests -v
node tests/check_interactions.cjs      # 小屏真实 API、触摸/导航/详情与写操作，需要 Chrome/Chromium
node tests/check_web_config.cjs        # 网页面板 + 真实 HTTP fixture
node tests/check_api_management.cjs    # Token 创建、编辑、停用、轮换与权限范围
node tests/check_service_settings.cjs  # 服务开关、实际显示状态与开发白名单
node tests/check_business_status.cjs   # 应用／下载／媒体来源、显式采集及移动端展示
node tests/check_small_screen_prototype.cjs  # 独立小屏原型（离线 HTML）
python tests/check_fan_guard_linux.py
python tests/check_fan_startup_linux.py
python tools/boot-once/test_runner.py
python tools/demo/run.py --screen-port 28731 --web-port 28732  # 本地联调
docker build --platform linux/amd64 -t r1-toolbox:local .
```

改动后至少跑与改动范围匹配的定向测试；提交前跑完整套件。浏览器检查需要 `CHROME_BIN` 指向 Chrome/Chromium。CI（Linux）的结果才是最终判据，Windows 上部分依赖 `os.sysconf` / `statvfs` / 符号链接的测试会失败，不要据此判断功能回归。

## 安全与硬件边界

这些是产品红线，不是建议：

- 风扇冷启动恢复旧 SMART serial 来源时，只为有效 active policy 已选摘要建立温控用途映射；当前同代 sysfs／udev 元数据须唯一且无冲突，已有合法 owner SMART／身份缓存仅增拒绝证据，不代替缺失元数据。不可扫描 SMART 找盘，不全局换 stable_identity／文件根／Token ID，不扩完整 SN 或 raw。测量与目录消费复核实际通道、policy epoch、collector 调度 epoch、磁盘生命周期；撤销后旧映射不能被新策略复用，同 ID 自然恢复时去重，重复／冲突／换盘仍未知并保持保护。
- 冷启动用途映射与温度须按同一设备代次投影，不能仅凭 sdX 拼接两次快照；有界内核库存须与发布库存完整对应，截断、漏项或无法核验时拒绝该用途映射，不能把前 128 行当作唯一性证明。控制循环在采目录前和取得控制锁后核同一 policy epoch，期间重配／撤销后丢旧目录，不以旧授权来源驱动新策略。保留真实样本时间、待机跳过与缺测保护，不宣称连续身份锁。
- 不绕过风扇接管预检：必须核验原厂温控二进制、唯一进程、字符设备与已加载驱动身份；不伪造 RPM，不盲扫 I²C，不停转风扇，不用目标百分比冒充实测转速。
- SMART 在无人查看且无有效风扇硬盘需求时不采：启动、库存、任意 GET、被动诊断与健康只读合法新鲜缓存；三十秒过期、身份失效或缺测为 `null`／`unknown`。所有者手动 `POST /api/r1/storage/smart/refresh` 保留严格 `{"force":false}` 或 `{"force":true}`；普通固定 `smartctl -n standby --json=o -a`，休眠跳过，保护不支持保持不可用，不自动强制。旧版 ATA 电源文字仅限已审计版本与唯一精确工具行，须与正式字段一致；未知、冲突、忽略保护或非 ATA 不授容量。`--json=o` 仅保留同次已有文字，不增加设备查询；`smartctl.output[]` 在首次复制前移除，完整详情、强制、测量与失败缓存均不保存原文。`force:true` 仅本次省略 `-n`，须明确可能唤盘，不持久化。手动接口仍保留同源／JSON／CSRF、SMART 来源串行锁与缓存锁、三十秒批次绝对时限、单命令至多三秒、逐盘设备生命周期及撤权丢整轮；不授 Bearer、MCP、小屏令牌或开发白名单，不接设备／路径／命令参数、不启动自检。磁盘库存读取任何盘字段前捕获可信生命周期，行采样后及发布前复核；采样中换盘整行丢弃，不给新身份拼接旧型号或名称。前台和风扇受限例外见下文及 `docs/smart-refresh.md`。
- 功耗只显示 RAPL 实测的 CPU 封装；不估算整机功耗，不把封装功耗当整机功耗。
- 后台磁盘监控仅用内核元数据和合法缓存：启动、周期采样、GET／被动诊断不读取磁盘 hwmon 温度及阈值（含 nvme、drivetemp 和关联别名），不对 `/drives` 挂载执行 statvfs、realpath 或 stat，不改用空间扫描；CPU／主板、风扇与身份／名称库存继续。磁盘温度只用同代三十秒 SMART 缓存，不续期或放宽风扇缺测保护。所有者手动容量 `POST /api/r1/storage/capacity/refresh {}` 保留，元数据测量可能唤盘，不宣称跳过休眠；前台缺测补齐另须可信非待机依据。两者复用真实挂载／设备复核、非排队锁和绝对窗口，拒 FUSE／未知／冲突。容量不持久化、不在启动初始化；按真实 mount ID、设备代次、target／root／fs 身份绑定，卸载／替换／重挂／身份不明即拒旧。GET 保留 `capacity_sampled_at` 原时刻，`capacity_source:manual_statvfs_cache` 是兼容来源标识，也包括前台按需快照，不表示一定手动。真实挂载 FD 仅采集／提交短暂持有，缓存不得长期 pin 用户挂载；内核挂载事件和身份复核保守失效，证明不足即丢历史值，不宣传原子路径锁。详见 `docs/capacity-refresh.md`。
- Web 进入存储页、磁盘详情或从 hidden 返回该视图时，以所有者 `POST /api/r1/storage/refresh {}` 显式刷新一次温度、状态与容量；不接 view／device／path／force，GET 永远被动。正 SMART 缓存不能吞掉本次普通采集；容量即使已有快照，也只对本批独立确认非待机且同代的目标重新测量，standby:null 不能当 false。待机或电源不明不测容量，保留合法旧快照及原时间，不编造新温度、健康或日期；同代负结果可短暂去重。概览缺测仍用 Web `POST /api/r1/storage/fill {}`，独立本机小屏仍用 `POST /api/storage/fill {}`，二者只补缺测、容量缓存命中不测，授权不互换。各自动入口每个可见周期最多一次，离页／hidden／屏保后不再发，失败／休眠／unsupported／busy 不因轮询重试；仅普通 SMART，不自动 force。风扇温度授权仅来自有效 active_config 和实际 active 温控通道所选硬盘，采前／发布前重验策略与盘代次，撤销立即停；与手动／前台共用锁，不采容量、不扩完整 SN、不放宽三十秒缺测保护。灯光策略本身不授额外后台采集权。
- 会话状态 `owner_authorized` 仅按真实 `panel_authorized(headers, peer)` 的 password／pair／open 结果提供布尔 UI 提示；保留 authorized／dev_mode 原语义及判断顺序，不能以开发窗口开启排除正常所有者，也不能以提示取代 POST 的实时 owner／Bearer／CSRF／代次检查。旧服务兼容仅 logged_in:true 或 password＋auth_required:false＋authorized:true，缺明确证明的旧 pair 只读缓存。
- 默认 `password` 模式首次无口令，网页监控与管理直接可用；设置并开启口令后需登录会话。显式 `pair` 模式兼容原五位一次性配对流程。两种模式均保留同源／JSON／CSRF、操作确认和 5 秒可取消电源操作；小屏控制令牌不得发给网页。
- 部署/升级必须复用原 Docker 项目与 `r1-toolbox-data` 数据卷；不删除卷、不使用 `down -v`。删除部署后正常重启可恢复官方显示。
- 未知读数保持 `null` / `--`，不用 0 或猜测填补；不可用的能力隐藏或标注原因，不生成占位卡片。
- 风扇接管排障优先使用受限的 `/api/dev/fan` 诊断接口，一次采集完整预检、进程现场和监督端真实判定时间线；不要反复追加少量错误字段、打包并要求用户部署试探。被截断的错误文本不足以判定根因，先对照结构化现场再修复。
- “服务与开发”通过所有者授权的 `/api/services/*` 管理独立小屏／MCP 开关和开发窗口，不能由 Bearer Token 或开发者白名单管理自己。小屏返回保存意图与实际 `active/busy/last_error`，不得按勾选成功宣称完成切换。Web 是常驻核心，端口不可用必须报告启动失败，不用空循环伪装健康容器。停止容器执行官方窗口、风扇等恢复，失败保留真实原因。
- 开发者白名单直接检测统一使用 `/api/dev/diagnostics`：只在显式开发模式、实际连接来源命中非空局域网 IP／CIDR 白名单和有效窗口内免登录读取固定项目，保持同源／JSON／CSRF。Web 保存绝对到期时间，重启不续期；过期、配置损坏或无白名单都关闭授权。只读授权不得扩展到控制、风扇重试或任意命令，采集复用各来源的并发锁，逐项报告失败。接口边界见 `docs/dev-diagnostics.md`。
- 完整硬盘序列号可在开发检测显式选择 `storage_identity`，或所有者网页的磁盘总览／SMART 详情中返回。网页只用已授权 `password/pair/open` 请求的缓存投影，Bearer 和开发白名单本身不授予此权限；默认快照、公开 v1、小屏、MCP 和原 `storage` 检查继续脱敏。只从当前设备生命周期有效的身份／SMART 缓存与固定原厂列表交叉核验，冲突或换盘保持 `null`，不为序列号额外运行 SMART、唤盘或开放任意查询。
- 原厂媒体会话验证使用显式 `POST /api/dev/media`，不自动加入被动检测。只读复用固定管理员已有会话，令牌与签名留在宿主，校验证书后只读首个状态帧；不重新登录、校时、重置令牌或转发任意原厂动作。原厂查询可能写完成状态，必须按 `docs/media-probe.md` 如实说明。
- 影视播放清晰度诊断仅使用显式 `POST /api/dev/media` 的严格 `{"action":"probe_transcode","path":"/drives/<disk>/Users/admin/...","confirm":true}`，固定查询能力、开关与单文件档位，保持实际来源开发白名单、绝对窗口、同源／JSON／CSRF 和媒体锁。固定 GET 不等于只读：匹配原厂 getter 可能读取媒体、写入 `user.md5` 并唤盘，其它效果未完全审计；不显式播放、停止、写开关，不入被动快照或 MCP。路径只作受限字符串校验，不能证明实际文件或授权范围；返回固定枚举／计数／安全整数，不透传路径、凭据或原厂自由文本。畸形业务码与能力冲突保持未知，仅对已审计 `-5402` 推导对应不支持分支，不泛化其它错误；区分服务端观测、客户端回落与实际播放，不得宣称静态 HLS 分支代表转码已成功。逐步与结束前复核授权，撤权立即停止并丢弃整轮结果。新模块须进入 bundle、两处 RAM 允许清单并重新生成 `runner.sh`。完整分支、副作用、打包与验收边界见 `docs/media-probe.md`；列表筛选的清晰度／动态范围不由此诊断证明。
- 原厂 TLS 监听排障复用生产 `collect_runtime`／`require_emhistor_listener` 的固定 observer，在三个清晰度 getter 中分别记录初次、连接后、发前、收帧后复核和请求发送／接收／丢弃阶段；未知旧错误不能据 `identity_unverified` 断言没有发送。额外复查仅用同一媒体入口的严格 `{"action":"probe_listener"}`，无 PID／端口／路径／confirm 参数，仅固定 proc 元数据，不读凭据、证书、媒体或发送原厂请求。沿用开发实际 peer 白名单、同源／JSON／CSRF、媒体／业务锁及十秒绝对窗口，明确拒绝 `Authorization`，所有者会话也不代替开发授权；逐步与结束前撤权即丢弃整轮。不加入 GET、默认快照、MCP 或轮询，不用缓存诊断放行，不忽略 `proc_partial`、省略共享 FD 持有者或重置窗口；未执行阶段为 `not_run`，只返回白名单原因、扫描预算和生命周期计数，不泄露 argv、环境、FD 目标、完整路径或异常原文。probe_listener 与 probe_transcode 的开发守卫绑定真实 peer 和 Web 代次，并贯通采集及响应 JSON 序列化前后；首次失效锁存，复权或重启不复活旧响应，两动作明确拒绝 Authorization。此门不证明 ELF 摘要或实际转码能力，也不扩展 scraper 权限例外。当前 `30ea3c4` 基线能力 33 项、档位仅身份失败，不等于静态表 24 项或实机 `-5402`；新监听现场仍待显式验证。见 `docs/media-probe.md`。
- 原厂空间管理验证只允许显式 `POST /api/dev/storage/probe` 和严格 `{"action":"probe","confirm":true}`，复用固定所有者已有会话，固定宿主 `127.0.0.1:80` 的 v1.2 `space_manage`；逐次确认原厂可能写 SMART／设备链接并唤盘的主动查询，不加入统一被动诊断、业务轮询或 MCP。保持真实来源开发白名单、绝对窗口、同源／JSON／CSRF、共享存储锁和有界传输，只返回白名单序列号与空间身份，令牌／签名／原文不出宿主。见 `docs/storage-probe.md`。
- 媒体 `probe_all` 固定验证七类状态，每项取一次首帧，失败相互隔离。共享相册只使用当前管理员创建的既有相册，不由网页传入标识；没有相册时标记不适用，不造样本。原厂共享列表存在修复故障分支、计数有吞错零值，不能宣传为全程无副作用或据零值推断空闲。
- 原厂业务 `GET /api/business` 不自动发起应用／媒体原厂查询；这两类查询仅由所有者显式 `POST /api/r1/business/refresh` 或原有显式媒体接口触发，再保存 Toolbox 脱敏快照。保留逐项时间和最近失败，不在 GET／被动诊断里为应用／媒体重签名或自动重试；刷新后旧采集不得覆盖新结果。下载任务使用受限只读协议，qB 核验唯一 IPv4／IPv6 本机监听后固定连接回环地址，不猜双栈。见 `docs/business-diagnostics.md`。
- qB `8181` 是本机已确认配置，默认需要账号密码；2026-10-29 外部免凭据查询成功源于用户配置的局域网放行，不代表默认行为或宿主回环同样获准。用户已明确授权把本机既有账号密码作为 `runtime/vendor_download_diagnostics.py` 的代码默认值，这不是所有 qB 的通用默认凭据，文档／网页／测试报告不得复述真实值。优先使用现有放行或有效 SID，仅在固定查询 401／403 时允许本轮一次固定登录；发送凭据前与接收 SID 后复核 PID／启动时刻／唯一监听 inode／回环地址。SID 仅在内存按身份及凭据绑定，身份或凭据变化即清除，服务端拒绝后最多重登一次；失败退避 300 秒，不在轮询中反复试密码。qB 网络操作共用 4 秒绝对窗口，不因登录／重试重置；aria2 仍为 2 秒且不登录。登录会创建服务会话，不宣传采集全程无副作用；不启动／恢复进程、不改认证配置、不读其他凭据文件、不开放任意地址／账号／Cookie 参数或登录代理，SID 与凭据不进入输出／快照／审计。
- 局域网 MCP `/mcp` 与 `/api/integration/*` 只使用独立 API Token，不把网页会话、小屏令牌或开发者窗口转成机器授权。服务默认关闭，新键按只读／读写／管理员角色、显式功能域及文件范围共同授权，列表与调用共用 `api_permissions`；管理员不绕过域与资源，旧键只保留原十工具和原六 scope。Token 支持编辑、暂停／启用、不可逆撤销和原子轮换；省略权限或有效期字段保持原值，不把旧键自动升级。来源接受完整落在局域网／回环范围的 IP、CIDR 或点分掩码，始终匹配真实 peer，不能用转发头或公网全放行网段。API 管理沿用网页授权与同源／CSRF；新工具复用原有校验和锁，禁止任意命令、SQL 或原厂 action 代理。文件功能使用可信根 ID 加相对路径，真实打开时还须重新核验身份、ACL、符号链接和句柄，不能将授权规划缓存当安全证明。未实现领域标记待接入，不制造假工具。Token 仅创建／轮换响应显示一次、持久化只存摘要；轮换立即使旧摘要失效，审计不记录参数／结果／凭据。见 `docs/mcp-api.md` 与 `docs/ai-capability-roadmap.md`。
- 文件数据总边界固定为 `/drives/*/Users/admin/`，根使用 admin 专用 v3 摘要，旧整盘和 Users v2 授权均不可静默换根或改写相对路径。规划只使用已有身份缓存、sysfs／udev 生命周期核验及挂载元数据，不访问 Users／admin 目录；同物理盘不同分区允许分别绑定，根下存在额外挂载不再使整根不可选，完整根歧义仍拒绝。实际打开逐级核验原物理挂载的 mount ID、设备和句柄，跨入子挂载（包括同设备 bind）仍拒绝，列表跳过该入口；不跟随链接、不开放 public 或其他用户。固定 Users get_info 仅保留为内部原厂 ACL 前置证明，不授文件浏览。业务新增五类只读工具仅返回已有脱敏快照，不能因 MCP 调用自动刷新应用、媒体或下载器；旧 policy v1／v2 不扩权，读取中撤权、轮换或到期后丢弃结果。
- 磁盘休眠排障使用显式 `disk_activity` 固定开发检查，默认四项检测不扩展。只读 proc/sys 计数和已有历史状态，不运行 SMART、空间扫描、休眠指令或任务控制，不读命令行、环境、FD 目标与用户文件。差分绑定启动和设备／PID 生命周期；零 I/O 不是休眠证明，进程总 I/O 不能冒充逐盘归因。原厂准确固件为至少三十分钟逐盘资格、约六十秒轮询；驱动空闲时间戳不以 proc 零计数替代，修改阈值没有已核验的原厂接口。见 `docs/disk-sleep-diagnostics.md`。
- 容量缓存排障只显式选择统一开发诊断的 `capacity_cache`；默认四项、GET 和 MCP 不增加采集。生产决策在容量锁内写有界 RAM ledger，完整 key 原文仅分域摘要；保留最近成功提交及其后首次正记录删除／替换，空轮询不覆盖，新成功提交才换代。监听、库存与真实失效门不因诊断改变；诊断读取不得调用库存、poll、capacity_snapshot、sample、SMART、worker 或 Fnm 投影。库存结果与 observation 必须同次绑定；Monitor／Fnm 正常消费才记有界输出、异常、匹配和汇总，原对象／线程来源与各自 seq 分别关联，不借最新值拼旧帧、不跨锁宣称原子。观察入口失败不改业务结果或原兜底。4 MiB 明确体积门不悄剪首错，超额／编码失败返回固定状态且保留 RAM 原件，大整数无损编码；客户端建议 5 MiB。真实 peer 开发窗口与首次撤权锁存贯通 JSON 序列化前后，Bearer／所有者／小屏不替代。`a47ab33` 实机容量仍曾清空，旧现场缺少失效谓词，不能把本地反例当唯一实机根因。详见 `docs/dev-diagnostics.md` 与 `docs/capacity-refresh.md`。

## 代码约定

前述容量条款的“拒旧／丢历史值”继续约束原当前容量、按盘绑定的最近观测及其安全用途。用户已选择新增独立的“过去测量档案”；这一例外只作用于不绑定现盘的过去数字，不改变原失效与不复活约定。

- 最近容量观测独立于当前容量缓存。所有者网页可展示 `capacity_last_observation` 与独立的 `capacity_last_observation_summary`，必须标为历史、保留原日期与 `measured_filesystem_subset` 范围，`coverage.complete:false`；不得写回当前 `total/used/usage` 或 `disk_size/disk_used/disk_total`，也不参与 SMART／statvfs 资格、文件空间预检、根规划、Token 或控制授权。历史只来自原容量业务最终成功后的私有候选，经同一所有者代次和 JSON 前后守卫、服务器响应写入成功后在原容量锁内发布；传输成功不等于客户端收到，失败不重放，也不回滚别人已发布的历史。候选使用独立业务递增序号，迟到 HTTP 不覆盖较新批次，顶部历史只合计一个批次，待机兄弟可保留自己的旧日期但不混入新批。绑定复用同次真实 `_capacity_device` 私有八段 tuple，不从诊断 ledger／摘要／sdX／SN 认盘；原生产挂载库存并非完整物理库存，缺行、未知、冲突或换代时隐藏／丢弃历史。同盘多个分区的物理父代次、设备号和 diskseq 必须一致，冲突整组拒绝。所有当前挂载／命名空间／设备／权限失效门照旧，历史不能使旧 current 复活；start／close 清空历史，不长期持有文件系统 FD，不新增 SMART、statvfs、用户挂载访问或内核轮询。仅 owner 的固定 Web 投影包含历史，公开 v1、小屏、MCP、Bearer 和开发白名单不扩权；私有同次 sideband 有界、不可变且可 deepcopy，JSON 不序列化它。

- 独立过去测量档案仅在真实 password／pair／open 所有者的现有 `GET /api/overview` 顶层披露 `capacity_observation_archive`，无档案为 `null`。固定对象为 `schema_version:1`、`status:historical`、`source:past_successful_observation`、`reason:current_binding_unverified`、原业务批次十进制字符串 `observation_id`、`total/used/available/percent`、最早／最晚原 Unix 秒 `sampled_at/sampled_at_latest`、`coverage` 和 `retention:memory_until_restart`；范围固定 `measured_filesystem_subset`、正整数文件系统／盘数及 `complete:false`。档案仅取最近一个真实新测量且原 owner JSON 前后、响应写入和最后发布守卫全部成功的批次，按原可信同设备去重，只含本批成功子集；不混旧批、物理大小或未测旧分区。空候选、全失败、cached-only、回滚、迟到旧批、JSON／写入／最后守卫失败不更新也不续原日期，撤权后失败候选不能因复权复活；与原提交共用实例、epoch、绝对期限及批次排序，回滚不能覆盖较新档案。公开对象不含路径、SN、FD 或身份 tuple，不加 per-disk archive，不自动关联现在盘位或池。库存不完整、换盘、分区／parent／alias／namespace 变化后可保留过去数字，但原 current、`capacity_last_observation` 与其 summary 的完整库存、ABA、watch、key、FD、epoch 和永久 prune 原样；档案不能复活它们，也不授当前容量、可用空间、空间预检、文件根、Token 或控制资格。正常 snapshot 的同次私有 sideband 携带档案，不另做 snapshot、库存、poll、SMART 或 statvfs，不扩原每批 128 个候选测量。仅 RAM、无 TTL、不持久化，start／close／重启清空；无 monitor 或 legacy 来源保持 `null`。披露沿用真实 owner 首次失败锁存，公开 v1、MCP、本机小屏、Bearer 和开发白名单均不扩权。UI 独立标明“过去测量档案”“当前归属未确认”“已测 N 个文件系统／M 块盘（子集）”“重启清空”及原完整日期区间；`available=total-used` 只是当时未用量，不是 `f_bavail` 或当前可写空间。细节见 `docs/capacity-refresh.md`。

- MCP 缓存历史、健康事件和流量仅读已有内存；健康必须同锁取得告警及评估，缺测、过期或不完整不得报正常，不输出动态 SMART／传感器身份。固定项目 stats 只使用已登记项目及 `inspect`，每次连接后复核授权和容器生命周期；CPU 无合法实际两帧差分则 `null`，网络／块 I/O 不重复聚合。proc 元数据必须有界读至 EOF，不能把一次短读当完整 mountinfo。固定 sysfs 分区 `dev` 文本属性的 `st_size` 是声明容量，不能作为实际长度；只用严格构造的 `sys/block/sdX/sdXN/dev` 专用读取器，实际 EOF 上限 64 字节并核对打开前后身份，普通文件长度门保持不变。见 `docs/mcp-monitoring-tools.md`、`docs/docker-projects.md` 与 `docs/file-mutations.md`。

- 镜像 RAM 缓存严格拒绝清单之外的文件和目录；所有执行或导入缓存运行时的 Python 入口必须带 `-B`，包括启动脚本在 `exec --resident` 前的导入，避免字节码缓存污染导致重启失败。
- 小屏是触摸优先：最小点击目标 56px、正文 22px、导航 18px、单列布局、底部六标签导航；不引入 hover 依赖、PC 双栏或需要精确指针的交互。风扇/灯光策略、登录口令等复杂设置只放在网页。
- 小屏实际展示与 `docs/prototypes/r1-small-screen.html` 必须保持一致。图标、样式通过 `scripts/build-prototype.py` 从实际小屏同步，原型专有 CSS 只用于外部画板；可见交互改动同步更新离线演示及检查，不能只改正式界面。CI 用 `--check` 拒绝未同步的图标与样式。
- 网页面板优先复用 fnMonitor 的 `defineModule`、设置分组、抽屉与控件；R1 专有内容放 `50-r1.js` / `12-style-r1.css`，保持上游模块结构、布局及控件。现有窄兼容仅包括 `33-state.js` 的可选网络汇总与空值内存辅助函数、`41-modules.js` 的缺测网络状态判定，以及 `42-modules.js`／`45-sheets.js` 的内存与交换区缺测保护，避免重复汇总、未知假空闲或假零值；其余不改上游渲染逻辑。R1 缺少构成来源时明确 `breakdown_available:false`，不从 MemAvailable 倒算 MemFree 或估算应用内存。构建时只做品牌与功耗文案替换。
- 对不自解释的函数、接口、结构字段、请求参数和关键返回值补简洁注释，说明职责、语义和使用边界；不要写变量赋值式废话。
- 改代码必须同步新增或修改受影响的测试；没有自动化落点的改动要在汇报里说明缺口与手工验证路径。
- 任何实际文件变更都要更新 `CHANGELOG.md`（本仓库已存在）。最终需求、架构边界、踩坑、运行/测试/发布方式写进本文件、README 或 docs，不要只留在会话记忆里。
- 项目不使用 Redis，无 TTL 约定。
- 手动温控的标准配置为 schema v2，每路只有 `low_temp/high_temp` 两个递增阈值：低于低温静音、两阈值之间（含端点）普通、高于高温全速，不附加隐式迟滞。旧 schema v1 严格校验后按 `normal_at/full_at` 迁移，保留输出、来源、原厂档位和开机恢复意图；所有入口使用相同归一化规则。独立 CPU 过热、缺测及租约保护始终优先。
- 小屏与网页的系统自动／高／中／低必须同义，明确请求原厂档位，不能把退出接管但保留高档显示成自动；CPU 高／中／低仍是独立固定输出。真实系统接管尚未释放时，配置 `fan_mode=5` 不能省略交接。恢复前保留安全全速，原厂初始化并同步自动后不得再写全速覆盖；恢复失败保留未确认原因和安全保护。原厂 `/tmp/fan` 是控制用档位缓存，不是 PWM 或 RPM 读回；原厂同档可能不重复写入，不能用配置或 ACK 证明物理调速。
- 文件／Docker 的二十一项合同维护在 `runtime/api_contracts.py` 并同步 JSON；覆盖使用独立 `prepare_file_overwrite`，旧 `prepare_file_upload` 仍只新建文件。所有者查定义不授执行权，policy v1／v2 冻结原十工具，v3 精确工具与资源共同授权。存储 v6 增加 `application_ids`，旧格式只读不改字节、真实写原子迁移；旧安装键仅保留 ID34，新键默认空，不因白名单扩展自动扩权。Docker 固定资源和数据卷保护继续按 `docs/docker-deployments.md` 执行，禁止任意 Compose／命令。
- 文件读取严格限定匹配固件、原厂固定所有者和 admin 子树；写入仅已核验内置 SATA 的 admin 后代，不能拿规划、root、O_PATH 或逻辑 ID 替代 ACL 与真实句柄。复制源可只读；写目标与父目录、移动源与父目录须各自授权。覆盖单独授权、确认及目标版本；Linux 无源 inode CAS，改名／移动须受信区捕获后核验，错捕保留对象且不发布／通知，不虚称单步原子改名。内部恢复区固定 Users/admin/.r1-toolbox-ops，用户文件与保管对象均在 admin 根内；私有恢复区仅由 Web 所有者显式预检、初始化、撤销和恢复，普通文件列表、内容传输、复制来源和写目标均拒绝该精确子树，根级完整 Token 也不能访问。禁止自动初始化、接管未知目录或修改原厂目录权限。新文件信任锚为实际 admin FD 与私有恢复区，外层目录的 UID、可写位与 ACL 仅作定位信息；保留固定路径、逐级 no-follow、真实物理盘与挂载身份检查，不授予外层文件访问。新登记写 file-mutation-admin-authorities.json v3，显式 layout=admin_custody_v1、trust_model=admin_anchor_v1 并持久绑定 admin_identity；旧版此文件 v2 仍按完整祖先策略执行，仅所有者显式确认核验原恢复区 inode 后升级。admin 被父目录的其他写入者移走或替换时拒绝重开、不追扫或换绑，不能宣传原子路径锁。任意层路径组件的 .r1-toolbox-ops 名称及大小写变体均保留，真实打开每级还须拒绝私有 inode 的文件系统名称别名，防旧 admin 被移入新根后泄露保管内容；旧 file-mutation-authorities.json v1 只读保留，旧 Users v2 根及旧 admin v3 根的 Users 保管布局均不能静默复用、迁移、删除或接管，升级前须用原镜像处理旧保管对象。恢复保留原路径、目标冲突不覆盖；数据已恢复与 index_verified 分开。原厂回收必须真实归档与精确数据库／索引确认，不以暂存或永久删除代替。详细协议、崩溃不重放和实机边界见 `docs/file-mutations.md`；file_protocol 等显式诊断不进入默认或被动轮询。
- MCP 当前八十一工具。显式下载任务操作与被动业务缓存分开；qB／aria2 唯一监听、连接后授权、进程和窗口仍逐次核验。任务写须当前版本、确认和本键幂等，移除保留文件；下载、Docker 和原生应用委托不宣称整体被 Users 文件沙箱约束。文件内容与新增目的地须真实文件后端证明，不提供任意地址、Cookie、RPC 或命令代理。
- 原厂模块目录 `list_native_modules` 固定读取 `get_modulize_table`，最低读写及精确 `apps` 工具；原厂会更新支持状态且可能联系厂商，不能放入被动快照或默认诊断。保留可用／已安装两表和原始状态，不推断运行、不合并重复 ID，不以目录记录发安装或卸载授权。
- 新原厂相册／影视库目录也可能初始化或修复内部状态，最低读写，不能标记只读或无副作用；原缓存五工具保持只读。固定七操作复用管理员已有会话、20443 叶证书 pin 和严格 emhistor 监听身份。写入须确认与本键幂等，原厂 ACK 不是异步完成；不明结果不重放。新目录验证仅显式 `/api/dev/media` 的 `probe_business`＋`confirm:true`；`file_backend_context` 仅显式统一开发诊断，目录元数据可能唤盘，不加入默认四项。详见 `docs/mcp-business-operations.md`。
- 原厂业务工具的唯一定义来源为 native_capability_specs.py。查询也可能初始化资源；写入须确认、本键幂等和当前对象／计划版本，ACK 不代表异步完成。普通应用新装只允许静态审计 ID7/netdisk_bd、ID8/contact_backup、ID34/netdisk_bd2、ID35/66_print，角色 admin、apps、plan/install 精确工具和 application_ids 同时限制，计划绑定授权集合，每步和回执重验。禁止核心模块、更新、卸载和任意包路径／URL／脚本；原厂空名称展示回退只限审计注册 ID2／35，不用于安装／有副作用进度身份。预览必须说明原生数据权限、已有任务、后台进程及可能宿主缓存清理等实际影响。固定开发 probe/packages 不执行安装或 setter，见 docs/native-capability-completion.md。
- 中文正文、UI 文案与文档使用全角标点；代码、路径、标识符保持原样。
- 原厂文件写入索引排障使用显式 `POST /api/dev/files/write-preflight`，精确 `action:preflight/confirm:true/root_id`；复用生产 SATA、监听、索引 DB 和 SQLite 句柄门的固定阶段观察，不能为诊断跳过安全失败或继续依赖步骤。仅有效开发白名单与绝对窗口授权，拒绝 Bearer、路径、SQL 和原厂 action，不进入默认诊断、MCP 或轮询。服务端仅生成 admin 根下未发布的随机目标，不通知、暂存或发布文件；SQLite 元数据副作用和唤盘须如实说明。该预检不证明子目录、恢复区或真实写入验收，见 `docs/file-mutations.md`。

- 影视追更排序是内置所有者 Web 可选插件，固定 `/api/plugins/media-sort` 的缓存 GET 与显式 probe／apply／revert；不授予 MCP、Bearer 或开发白名单，不接收路径／SQL，不在启动、GET 或轮询中扫描原厂库。数据库只由匹配原厂 scraper 生命周期和 `-d` 自动发现，多候选明确选择；写前私有一致性备份，事务内处理严格识别的规则。规则沿用用户原始 `metadata_path_guard` + `AFTER INSERT` 触发器：同一 `(id,lid,type)` 的路径数组变长时更新时间，无 TV 类型过滤、无 `UPDATE` 触发器、无去重或跨类型基线；已有精确对象直接 `enabled`，不重装／重 seed／升级，未知、部分或增强版残留报告冲突且不接管。撤销只删自身表与触发器，不恢复整库或清除已调整时间，容器停止不撤销原厂库内已提交规则。坏 JSON 由原 SQL 的 `json_array_length` 报错，不静默跳过，也不承诺不阻断原厂写入。详见 `docs/media-sort-plugin.md`。
- 影视追更排序的固定只读诊断仅复用严格 `{}` 的所有者 `POST /api/plugins/media-sort/probe`，在原锁与 12 秒绝对窗口内缓存 `plugin.diagnostics` schema 1；构造／启动／GET／轮询不采集，初始与关闭清为空 `unknown`。固定阶段覆盖扫描完整性、最多 16 个 scraper 的内核身份／exe／受限参数／末次代次、全部进程门通过后的 no-follow 数据库／辅助文件／真实 SQLite FD／schema／JSON1／自有对象，以及发布前来源与 owner epoch。独立安全内核观测可继续，但不替代生产放行；hash 和参数各自保留前置门，依赖未满足标 `not_run`，身份变化不拼代，超时／撤权丢弃本轮私有结果。只输出白名单阶段／原因／观测，不输出异常原文、argv、环境、`-d`／cwd 值或凭据，不接受 PID／路径／命令／SQL，也不授予开发窗口、机器 Token 或 MCP。d9 实机只定位到发现／进程门拒绝，不能据此断言 UID／namespace／DB／原 INSERT 根因；规则文件与全部生产身份／FD／结构门不因诊断放宽。
- 原厂 scraper 的 exe 专用元数据门允许用户授权的精确 `0777` 布局，保留原权限，不 chmod，也不提供通用 force 参数。例外仍要求 root UID、普通文件、单链接、精确大小和真实 FD 的固定 SHA；其他带组／其他用户写位的模式及附加特殊位不自动扩展，共享 `_owned`／`_regular`、数据库／辅助文件／私有备份权限门不放宽。每次 expected 来源复核先核身份与链接，再实际重读 exe FD 核固定摘要，前后完整文件身份和 PID 代次仍须一致；缓存来源在首次打开数据库链／辅助文件前也重验，后续 current 检查不省略。重验失败按实际 PID 和真实最内层阶段／原因更新拒绝状态，身份变化清除该代；保留其他 PID 及已有真实数据库观测，不为未执行依赖补造结果。命名空间／root／参数／owner epoch 与原 12 秒绝对窗口保留，失败不进入数据库。FD／摘要不等于原子路径锁或持续不可变。80cd 新实机诊断已确定当轮 `0777` 命中 exe 权限门，ELF／参数／DB 未执行，不能倒推 d9 当时已确诊或宣称兼容后续链已通过；原始 INSERT 规则和已启用 no-op 不变。

## 提交规范

- 仓库只保留 `dev` 和 `main` 两个分支。日常修改与提交使用 `dev`，通过完整检查后将 `main` 快进到已验证提交；不为每项任务新建长期分支。清理旧分支前必须核对独有提交，并通过合并历史保留全部提交，不用强制推送或重置丢弃历史。
- commit 标题：`type(scope): 简述`，`type` ∈ `feat/fix/docs/refactor/perf/test/chore/build/ci`；祈使句、≤50 字符、结尾不加句号，默认中文，技术术语保留英文。
- 一个 commit 只做一件事；不把无关修复、格式化或临时探针混进来。
- 改完当轮 push；协作/公开动作（开 PR、发版）按仓库规则另行确认。
- push 前检查 `tests/`、`scripts/build-panel.py` 产物与文档是否同步；`ui/web/` 为构建产物，先构建再提交。
