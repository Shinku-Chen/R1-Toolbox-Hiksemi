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

- 不绕过风扇接管预检：必须核验原厂温控二进制、唯一进程、字符设备与已加载驱动身份；不伪造 RPM，不盲扫 I²C，不停转风扇，不用目标百分比冒充实测转速。
- SMART 只用 `smartctl -n standby` 且走后缓存；打开网页/详情不得临时启动自检或唤醒休眠盘。
- 功耗只显示 RAPL 实测的 CPU 封装；不估算整机功耗，不把封装功耗当整机功耗。
- 默认 `password` 模式首次无口令，网页监控与管理直接可用；设置并开启口令后需登录会话。显式 `pair` 模式兼容原五位一次性配对流程。两种模式均保留同源／JSON／CSRF、操作确认和 5 秒可取消电源操作；小屏控制令牌不得发给网页。
- 部署/升级必须复用原 Docker 项目与 `r1-toolbox-data` 数据卷；不删除卷、不使用 `down -v`。删除部署后正常重启可恢复官方显示。
- 未知读数保持 `null` / `--`，不用 0 或猜测填补；不可用的能力隐藏或标注原因，不生成占位卡片。
- 风扇接管排障优先使用受限的 `/api/dev/fan` 诊断接口，一次采集完整预检、进程现场和监督端真实判定时间线；不要反复追加少量错误字段、打包并要求用户部署试探。被截断的错误文本不足以判定根因，先对照结构化现场再修复。
- “服务与开发”通过所有者授权的 `/api/services/*` 管理独立小屏／MCP 开关和开发窗口，不能由 Bearer Token 或开发者白名单管理自己。小屏返回保存意图与实际 `active/busy/last_error`，不得按勾选成功宣称完成切换。Web 是常驻核心，端口不可用必须报告启动失败，不用空循环伪装健康容器。停止容器执行官方窗口、风扇等恢复，失败保留真实原因。
- 开发者白名单直接检测统一使用 `/api/dev/diagnostics`：只在显式开发模式、实际连接来源命中非空局域网 IP／CIDR 白名单和有效窗口内免登录读取固定项目，保持同源／JSON／CSRF。Web 保存绝对到期时间，重启不续期；过期、配置损坏或无白名单都关闭授权。只读授权不得扩展到控制、风扇重试或任意命令，采集复用各来源的并发锁，逐项报告失败。接口边界见 `docs/dev-diagnostics.md`。
- 原厂媒体会话验证使用显式 `POST /api/dev/media`，不自动加入被动检测。只读复用固定管理员已有会话，令牌与签名留在宿主，校验证书后只读首个状态帧；不重新登录、校时、重置令牌或转发任意原厂动作。原厂查询可能写完成状态，必须按 `docs/media-probe.md` 如实说明。
- 媒体 `probe_all` 固定验证七类状态，每项取一次首帧，失败相互隔离。共享相册只使用当前管理员创建的既有相册，不由网页传入标识；没有相册时标记不适用，不造样本。原厂共享列表存在修复故障分支、计数有吞错零值，不能宣传为全程无副作用或据零值推断空闲。
- 原厂业务 `GET /api/business` 不自动发起应用／媒体原厂查询；这两类查询仅由所有者显式 `POST /api/r1/business/refresh` 或原有显式媒体接口触发，再保存 Toolbox 脱敏快照。保留逐项时间和最近失败，不在 GET／被动诊断里为应用／媒体重签名或自动重试；刷新后旧采集不得覆盖新结果。下载任务使用受限只读协议，qB 核验唯一 IPv4／IPv6 本机监听后固定连接回环地址，不猜双栈。见 `docs/business-diagnostics.md`。
- qB `8181` 是本机已确认配置，默认需要账号密码；2026-10-29 外部免凭据查询成功源于用户配置的局域网放行，不代表默认行为或宿主回环同样获准。用户已明确授权把本机既有账号密码作为 `runtime/vendor_download_diagnostics.py` 的代码默认值，这不是所有 qB 的通用默认凭据，文档／网页／测试报告不得复述真实值。优先使用现有放行或有效 SID，仅在固定查询 401／403 时允许本轮一次固定登录；发送凭据前与接收 SID 后复核 PID／启动时刻／唯一监听 inode／回环地址。SID 仅在内存按身份及凭据绑定，身份或凭据变化即清除，服务端拒绝后最多重登一次；失败退避 300 秒，不在轮询中反复试密码。qB 网络操作共用 4 秒绝对窗口，不因登录／重试重置；aria2 仍为 2 秒且不登录。登录会创建服务会话，不宣传采集全程无副作用；不启动／恢复进程、不改认证配置、不读其他凭据文件、不开放任意地址／账号／Cookie 参数或登录代理，SID 与凭据不进入输出／快照／审计。
- 局域网 MCP `/mcp` 与 `/api/integration/*` 只使用独立 API Token，不把网页会话、小屏令牌或开发者窗口转成机器授权。服务默认关闭，新键按只读／读写／管理员角色、显式功能域及文件范围共同授权，列表与调用共用 `api_permissions`；管理员不绕过域与资源，旧键只保留原十工具和原六 scope。Token 支持编辑、暂停／启用、不可逆撤销和原子轮换；省略权限或有效期字段保持原值，不把旧键自动升级。来源接受完整落在局域网／回环范围的 IP、CIDR 或点分掩码，始终匹配真实 peer，不能用转发头或公网全放行网段。API 管理沿用网页授权与同源／CSRF；新工具复用原有校验和锁，禁止任意命令、SQL 或原厂 action 代理。文件功能使用可信根 ID 加相对路径，真实打开时还须重新核验身份、ACL、符号链接和句柄，不能将授权规划缓存当安全证明。未实现领域标记待接入，不制造假工具。Token 仅创建／轮换响应显示一次、持久化只存摘要；轮换立即使旧摘要失效，审计不记录参数／结果／凭据。见 `docs/mcp-api.md` 与 `docs/ai-capability-roadmap.md`。

## 代码约定

- 镜像 RAM 缓存严格拒绝清单之外的文件和目录；所有执行或导入缓存运行时的 Python 入口必须带 `-B`，包括启动脚本在 `exec --resident` 前的导入，避免字节码缓存污染导致重启失败。
- 小屏是触摸优先：最小点击目标 56px、正文 22px、导航 18px、单列布局、底部六标签导航；不引入 hover 依赖、PC 双栏或需要精确指针的交互。风扇/灯光策略、登录口令等复杂设置只放在网页。
- 小屏实际展示与 `docs/prototypes/r1-small-screen.html` 必须保持一致。图标、样式通过 `scripts/build-prototype.py` 从实际小屏同步，原型专有 CSS 只用于外部画板；可见交互改动同步更新离线演示及检查，不能只改正式界面。CI 用 `--check` 拒绝未同步的图标与样式。
- 网页面板优先复用 fnMonitor 的 `defineModule`、设置分组、抽屉与控件；R1 专有内容放 `50-r1.js` / `12-style-r1.css`，不改上游渲染逻辑。构建时只做品牌与功耗文案替换。
- 对不自解释的函数、接口、结构字段、请求参数和关键返回值补简洁注释，说明职责、语义和使用边界；不要写变量赋值式废话。
- 改代码必须同步新增或修改受影响的测试；没有自动化落点的改动要在汇报里说明缺口与手工验证路径。
- 任何实际文件变更都要更新 `CHANGELOG.md`（本仓库已存在）。最终需求、架构边界、踩坑、运行/测试/发布方式写进本文件、README 或 docs，不要只留在会话记忆里。
- 项目不使用 Redis，无 TTL 约定。
- 中文正文、UI 文案与文档使用全角标点；代码、路径、标识符保持原样。

## 提交规范

- 仓库只保留 `dev` 和 `main` 两个分支。日常修改与提交使用 `dev`，通过完整检查后将 `main` 快进到已验证提交；不为每项任务新建长期分支。清理旧分支前必须核对独有提交，并通过合并历史保留全部提交，不用强制推送或重置丢弃历史。
- commit 标题：`type(scope): 简述`，`type` ∈ `feat/fix/docs/refactor/perf/test/chore/build/ci`；祈使句、≤50 字符、结尾不加句号，默认中文，技术术语保留英文。
- 一个 commit 只做一件事；不把无关修复、格式化或临时探针混进来。
- 改完当轮 push；协作/公开动作（开 PR、发版）按仓库规则另行确认。
- push 前检查 `tests/`、`scripts/build-panel.py` 产物与文档是否同步；`ui/web/` 为构建产物，先构建再提交。
