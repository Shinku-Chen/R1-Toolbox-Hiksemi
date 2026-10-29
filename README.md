# R1 Toolbox

局域网 AI 接入：网页“设置 → API 管理”可开启 MCP 与 REST、创建按功能授权的访问密钥并查看调用记录。MCP 地址为 `http://NAS_IP:18732/mcp`，默认关闭；详见[MCP 与 API 管理](docs/mcp-api.md)。

海康 R1 NAS 的 Docker 工具箱：使用 `composer.yml` 部署本仓库构建的镜像，UI 和运行脚本均随镜像交付。

最终产物为 **`composer.yml` 与 Docker 镜像**，另提供同一镜像的 `docker save` 离线包。源码仓库保持私有，GHCR 镜像可以公开拉取。

## 下载与部署

镜像：`ghcr.io/shinku-chen/r1-toolbox`，平台 `linux/amd64`。仓库只保留 `dev` 开发分支和 `main` 主分支；`dev` 发布 `preview`，`main` 发布 `latest`，每次构建另有固定的 `sha-<完整提交>` 标签。日常开发在 `dev` 完成，检查通过后将 `main` 快进到已验证的提交。

同一提交从 `dev` 推进到 `main` 时，发布流程复用已有的 SHA 镜像并重新校验、执行隔离检查，再更新 `latest`；不会重新构建并覆盖不可变的 SHA 标签。同一 SHA 的发布任务串行执行。

1. 在 [Actions](https://github.com/Shinku-Chen/R1-Toolbox/actions) 下载成功构建的产物。
2. 在线：在 NAS Docker 市场用产物中的 `composer.yml` 更新或创建项目并启动。
3. 离线：先导入同包的 `r1-toolbox-linux-amd64.tar`，再导入该 Compose。

新版部署文件默认 `UI_MODE: image`，启动镜像自带的小屏界面，**不需要上传前端目录**。如只想检查环境，改为 `UI_MODE: diagnostic`。发布包的 Compose 固定为对应源码提交的镜像，无需 `.env`，也不强制联网重新拉取。

## 内置界面

小屏为本仓库独立实现的触摸界面，适配 376×960 竖屏并兼容 320px 宽。界面采用 fnMonitor 风格的暗色、炭灰大圆角卡、点阵时钟、状态环与清晰中文；所有可点元素至少 56px，正文 22px，底部六个大标签（概览／存储／网络／应用／温度／设置）；主页面不显示顶部栏，点击卡片进入核心指标、处理器、历史趋势、容器／监听端口或网页设置等详情，详情页保留返回栏，右滑返回；左右滑动切换主页面。复杂规则（风扇温控曲线、登录口令等）不在小屏编辑，页面内提供直达网页的入口。所有入口都连接本机 API：

| 页面 | 内容 |
|---|---|
| 概览 | 点阵时钟、状态环与整体健康、CPU/内存/存储/网络速率；进入核心指标、处理器、趋势、温度与风扇 |
| 存储 | 展示数据盘总使用率，以及物理 HDD／SSD／NVMe 的卷标或挂载目录名、型号、容量、温度和 SMART 状态；SMART 不可用时明确显示未读取，不把挂载点当磁盘 |
| 网络 | 默认网卡、各接口 IPv4/MAC/链路状态、速率与实时上下行；监听端口与 Docker 端口映射 |
| 温度 | 温度传感器、风扇状态；CPU／系统风扇分别以单行五档快速切换自动／低／中／高／手动 |
| 设置 | 亮度、可混选温度／使用率的屏幕保护、自动息屏、灯光色相／亮度／四种原生模式、网页设置入口，以及二级页电源管理 |
| 应用 | Docker 运行/总数、容器卡片、资源占用和端口；进入详情后启动/停止/重启；不展示虚拟机 |
| 详情 | 核心指标、处理器与内存、历史趋势、容器详情、监听端口、网页设置、电源管理 |

未知指标显示 `--`，首次差分显示采集中；连接中断时保留上次可用读数并明确标注离线，来源失效的字段不补成 0。没有可信来源的控制、GPU、阵列等模块自动隐藏；网页不再展示 CPU 封装功耗、显卡和能耗卡片，系统中的设备能力详情保留诊断原因。SMART 使用 `smartctl -n standby`，不为该查询主动唤醒休眠盘。基础指标约每2秒更新，设备详情由独立后台低频采集。小屏趋势按 1h／6h／24h 读取宿主 SQLite 历史；网页另有可关闭的 SQLite 历史，最长 30 日，提供温度、CPU、内存、网络和磁盘 I/O 趋势、今日／本月流量、本地告警、SMART 健康观测、JSON 报告及 CSV 导出。网页不再展示CPU封装功耗、显卡和能耗卡片；后端RAPL读数仍保留在接口中，不当成整机功耗。历史会写入数据卷，可能影响其所在硬盘休眠，完整口径见[历史与统计](docs/monitoring.md)。

**CPU 与系统风扇可分别配置。** 网页均提供自动／高／中／低／手动：CPU 高／中／低为独立固定输出 100／75／50%，系统高／中／低为原厂档位，不换算为输出百分比；手动直接编辑温控曲线。点击保存即应用，无需复位或二次确认；同一组已接管通道调整输出或曲线时直接更新目标，保护和必要的原厂交接保留。每路手动温控可单独多选温度源，按原始摄氏度的最高值运行，并显示触发来源、新鲜度和请求输出。静音／普通／全速阈值分别设置，阈值 15–85°C 且递增；静音／普通输出 10–100%，全速固定 100%。小屏保留五档快捷操作，其低／中／高属于独立固定输出，温度页同时显示物理硬盘温度。CPU 与硬盘都是 °C，但正常范围不同，混选时 CPU 可能主导。具体迟滞、保护、保存和接管流程见 [独立风扇温控指南](docs/fan-policy.md)。

已加载 hikio 的完整 GNU note 与支持版匹配，原厂 Unix GET 曾实测返回 b'5'/auto；这些证据不等于本版接管已经验证机械效果。新接管还需原厂温控二进制、唯一进程、字符设备和 RAM 适配层等身份检查。旧 RPM 接口仍不可用，目标百分比不能充当 RPM。原厂 `fan_mode` 的低／中／高／自动仍仅对应系统风扇，与新的两路独立温控分开；物理风扇对应和实际风速变化均待实机验证。详见 [原厂接口证据](docs/histor-interfaces.md)、[功能矩阵](docs/features.md) 与 [附件审查](docs/attachments.md)。

默认正文 22px、导航 18px、主要按钮 22px，辅助说明 14–17px，常用数据用单列大卡片展示；六个底部大标签，长地址换行，复杂信息留在可滚动的二级/三级页。小屏不再提供本地外观、页面或夜间设置；网页端外观在网页面板调整。320px 宽度下隐藏装饰性趋势线，保留读数与进度条。

网页监控位于 `http://NAS_IP:18732`，直接打开即可查看总览、趋势、存储、网络、应用与设备。网页端直接使用 [fnMonitor](https://github.com/MisiteQ/fnmonitor) 的面板框架（MIT，源码固定在 `d002d1d2`，见 `ui/panel-src/`），R1 填充数据、新增专有模块，并把页面重组为与小屏一致的六个标签：概览／存储／网络／应用／温度／设置；磁贴、点阵数字、详情抽屉、布局编辑和设置控件沿用上游。天气使用 Open-Meteo 免费接口，可按公网 IP 自动定位或在设置中填写城市／坐标，失败时显示原因而不伪造天气。公开结构化接口为 `/api/v1/status`，另有历史、流量、告警、报告、CSV及OpenAPI。第三方读取不需要配对，详见[公开API](docs/public-api.md)；面板与鉴权细节见[网页面板](docs/web-panel.md)。

网页容器列表支持搜索和状态筛选，卡片显示真实资源占用与端口，点击打开详情。小屏和已配对网页均可确认后启动、停止或重启普通容器；按当前状态开放操作，Toolbox 自身受保护。停止与重启会等待优雅退出；超时仅表示结果未确认，需要刷新查看，不会自动重复操作。Toolbox 安装容器完成后停止属于正常行为。设置按双路风扇、机身灯光、小屏与显示、历史提醒、外观、登录口令和连接分组；复杂规则在网页编辑，设置页保留常用操作。R1 专有的双路温控、四种原生灯效、小屏亮度与电源操作以 fnMonitor 的磁贴和设置控件加入面板。入口与使用范围见[界面与容器指南](docs/interface-guide.md)。

网页登录有两种模式：宿主默认只使用登录口令，口令默认关闭、用户可在网页设置开启；需要兼容旧配对流程时设置 `R1_WEB_AUTH_MODE: pair`。未设置口令时，任何能访问 18732 的设备都能修改设置，请只在可信局域网使用。两种授权都只解锁已有白名单操作，不提供任意命令。网页与小屏共用面板、两路风扇、灯光及统计配置；网页的风扇/灯光「保存」会一次完成保存并应用，并等待运行状态确认。已解锁网页还可执行已有的确认式容器操作及5秒可取消关机／重启。配对会话最长8小时、服务重启后需重新配对；口令会话按设置保持 1–30 天，配置不丢失。可设 `R1_WEB_PORT: '0'` 禁用网页，或 `R1_WEB_BIND: '127.0.0.1'` 限制为本机。

灯光按原厂DesignWare适配器识别逻辑自动寻找唯一 `0x26` 灯控芯片，验证 `0x01=0x55` 后读取模式、亮度及RGB，不需手选总线。界面只提供关闭、常亮、彩虹、呼吸四种原生灯效和跨午夜亮度上限；温度联动仍保留在底层配置以兼容旧数据，但不在本版 UI 提供编辑。默认只读探测，显式应用的规则才可持久恢复；检测到原厂主题或其他程序改灯时让回控制。详见[灯光策略与固件证据](docs/light-policy.md)。

原有控制令牌仍只用于 NAS 本机小屏；普通前端文件预览保持只读。重启/关机需确认，提交后有 5 秒可取消倒计时；关机后不会自动开机。面板设置与独立风扇规则保存在同一独立 Docker 命名卷；`fan-policy.json` 分开记录待用规则与最后成功应用规则，只有成功应用的配置才会尝试开机恢复。单纯保存新参数不会替换当前运行或开机恢复的旧规则。IT87 hwmon 接管／曲线和 RGB 即时操作仍不作为开机自动执行配置保存。原厂系统风扇策略仅对匹配的已加载驱动开放，每次设置重新核验；它会由官方服务保存配置，恢复 UI 不自动撤销该策略，协议回执也不能替代物理效果验证。

网页面板框架与视觉来自 [fnMonitor](https://github.com/MisiteQ/fnmonitor)（MIT），完整源码固定在 `ui/panel-src/`，构建脚本为 `scripts/build-panel.py`；面板读取的 fnMonitor 兼容接口由 `runtime/fnm_compat.py` 用本机现有快照填充，不导入上游的 fnOS 专有集成。小屏界面仍为本仓库独立实现。信息分组和功能需求参考 [r1-toolbox-go](https://github.com/lemon6515/r1-toolbox-go)、[r1-frontscreen](https://github.com/mingyangbest/r1-frontscreen) 及用户提供的 R1 Panel 1.1.7、R1 Toolbox 4.8.10 安装包。采集与控制代码不导入参考程序的授权模块或受限代码。当前继续使用官方 HistorSub 的 Electron 窗口。

## 宿主文件与运行方式

镜像基于 `docker.io/library/debian:bookworm-slim`，保留原逃生容器的宿主访问方式。启动时从当前容器复制内置 UI 包，验证校验值并展开到宿主 `/run/r1-toolbox`。**仅在确认该目录位于 tmpfs 后才展开 UI**。

宿主临时小屏服务只读挂载该前端目录，再运行 Python 监控/控制服务和原 `AppRun`。原版 AppImage、解包版 `dist` 与启动脚本保持原样；Toolbox 容器随后实际停止，小屏与服务由宿主 systemd 继续管理。本机 API 监听 `127.0.0.1:18731`，读取接口与需要启动时随机令牌的控制接口分开；独立网页配置入口默认监听 IPv4 `0.0.0.0:18732`，使用一次性配对和独立会话，不向浏览器提供小屏令牌。网络详情支持 IPv6 不表示网页入口已监听 IPv6。

UI与服务展开到RAM，重启后从镜像重建。官方AppImage、解包版UI与启动脚本不被覆盖；用户确认的原厂风扇策略可能由厂商服务保存在自己的配置。`/data`独立命名卷保存面板、风扇、灯光、统计配置及已启用的历史数据库，容器重建、镜像升级和NAS重启仍保留。image模式要求可写数据卷，缺失时在停止原小屏前终止预检。更新须复用原项目和卷；删除卷才会丢失这些配置。删除部署后重启可恢复原厂显示，本轮没有覆盖原厂文件。执行记录按boot ID、任务版本、镜像ID、UI与网页监听配置去重。Electron、Docker和系统仍可能写正常缓存／日志。

独立温控的 CPU／主板 sysfs 来源每秒刷新、最多允许 3 秒旧读数；硬盘使用既有 SMART 缓存，通常约 30 秒采样、最多允许 90 秒旧读数，刷新网页不会延长读数寿命。SMART 继续使用 `-n standby`，不为控温唤醒休眠盘。

用户确认应用后，独立 systemd 监督服务以短租约协调控制，重启同一个 `/histor/bin/temp_control` 并加载 RAM 中的窄范围适配层；它保留未选通道和硬盘休眠请求，交接会重建原厂休眠计时。不修改原厂二进制或 `fan_mode`，不重启 `emhistor`。退出、租约失效或故障时请求安全输出并恢复原厂执行者；恢复未确认会显示异常，不能当成恢复成功。该机制已通过替身测试，尚未在用户 NAS 上验证物理调速效果。

详细依赖、目录模式和恢复方法见 [部署说明](tools/boot-once/README.md)。

## 开发

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
python tools/boot-once/build-compose.py
python scripts/build-panel.py
python scripts/build-ui.py
python tools/demo/run.py --screen-port 28731 --web-port 28732  # 本地假 NAS 演示
node tests/check_histor_bridge.cjs
node tests/check_interactions.cjs
node tests/check_web_config.cjs
python tools/boot-once/test_runner.py
python -m unittest discover -s tests -v
docker build --platform linux/amd64 -t r1-toolbox:local .
docker run --rm --network none r1-toolbox:local --help
```

网页面板源码在 `ui/panel-src/`（fnMonitor，MIT），`python scripts/build-panel.py` 生成 `ui/web/index.html` 与 `login.html`；`tools/demo/` 可在普通电脑上启动假 NAS，同时打开网页面板和小屏触摸界面做联动调试（详见 [本地演示](tools/demo/README.md)）；小屏界面源码在 `ui/index.html` 与 `ui/assets/`，`python scripts/build-ui.py` 再打包成镜像内的 UI 包。`runtime/ui_service.py` 提供 API 并管理小屏进程，`runtime/web_config.py` 提供网页面板、只读 `/api/v1/*`、配对与可选口令登录，`runtime/fnm_compat.py` 将现有快照映射为面板接口，`runtime/fnm_auth.py` 处理口令哈希与会话，`runtime/toolbox.py` 负责设置和硬件控制，`runtime/histor.py` 负责有明确协议的原厂接口适配，`runtime/insights.py` 负责系统、磁盘和应用采集，`runtime/device_info.py` 负责有界、只读设备库存；`runtime/temperature_sources.py` 提供带时效的温度来源，`runtime/fan_policy.py` 执行独立规则，`runtime/fan_native.py` 与 `runtime/fan_watchdog.py` 协调白名单适配层及独立恢复服务。修改后重新构建面板、打包和构建镜像，无需将源码或页面复制到 NAS。

前端交互检查需要本机 Chrome/Chromium，可通过 `CHROME_BIN` 指定可执行文件；`playwright-core` 仅为开发依赖。小屏检查使用拦截数据，网页端到端检查使用真实本地 HTTP 服务、临时 sys/dev/data 和内存适配器；均不连接 NAS 或执行真实硬件操作。

自动检查覆盖脚本、Compose、指标采集、控制请求、包与缓存完整性，前端检查覆盖主页面、分级详情、返回/手势、触摸控制与缺失能力隐藏。临时 Linux CI 主机还验证实际只读挂载，以及测试容器真正停止并删除后，宿主测试小屏服务仍能读取内置 UI 和返回状态。CI 使用测试程序，不接触真实 NAS；通过这些检查不代表所有硬件控制已在实机验证。

发布产物还包括 `deployment-manifest.json` 和 `SHA256SUMS`，记录源码提交、镜像 ID、平台及文件校验值。真实 NAS 的小屏画面、触摸与厂商守护进程行为仍需部署验证。
