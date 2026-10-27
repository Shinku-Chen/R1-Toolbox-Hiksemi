# R1 Toolbox

海康 R1 NAS 的 Docker 工具箱：使用 `composer.yml` 部署本仓库构建的镜像，UI 和运行脚本均随镜像交付。

最终产物为 **`composer.yml` 与 Docker 镜像**，另提供同一镜像的 `docker save` 离线包。源码仓库保持私有，GHCR 镜像可以公开拉取。

## 下载与部署

镜像：`ghcr.io/shinku-chen/r1-toolbox`，平台 `linux/amd64`。开发分支发布 `preview`，主分支发布 `latest`；每次构建另有固定的 `sha-<完整提交>` 标签。

1. 在 [Actions](https://github.com/Shinku-Chen/R1-Toolbox/actions) 下载成功构建的产物。
2. 在线：在 NAS Docker 市场用产物中的 `composer.yml` 更新或创建项目并启动。
3. 离线：先导入同包的 `r1-toolbox-linux-amd64.tar`，再导入该 Compose。

新版部署文件默认 `UI_MODE: image`，启动镜像自带的小屏界面，**不需要上传前端目录**。如只想检查环境，改为 `UI_MODE: diagnostic`。发布包的 Compose 固定为对应源码提交的镜像，无需 `.env`，也不强制联网重新拉取。

## 内置界面

原创 HTML/CSS/JS，适配 376×960 竖屏并兼容 320px 宽。首页保留重点指标，点击卡片或列表进入二级/三级详情，再点返回；左右手势切换主页面，内容可独立滚动。默认使用低饱和暗色，避免大量信息挤在一屏。七个入口都连接本机 API：

| 页面 | 内容 |
|---|---|
| 概览 | 时间、CPU/温度、内存、网络速率、数据盘容量、运行时长 |
| 存储 | 已挂载数据盘容量、物理磁盘信息、可用的温度与 SMART 健康状态 |
| 网络 | 默认网卡、各接口 IPv4/IPv6 与前缀、双栈默认网关/metric、DNS/上游/搜索域与来源、链路和流量 |
| 控制 | 亮度、息屏/唤醒、可用的风扇与 RGB 控制、重启/关机 |
| 应用 | Docker 容器及资源占用、启动/停止/重启；可用的 libvirt 虚拟机操作 |
| 系统 | 设备/固件/内核、DMI/PCI、CPU 拓扑/频率/分核、全部温度与风扇传感器、阵列、进程、端口、GPU 和短时趋势 |
| 设置 | 四套主题、80–140% 字号、启用页面、自动轮播、无操作息屏、夜间低亮度 |

未知指标显示 `--`，首次差分显示采集中，连接中断或来源失效时清除旧读数。没有可信来源的控制、GPU、虚拟机、阵列等模块自动隐藏；系统中的设备能力详情保留诊断原因。SMART 使用 `smartctl -n standby`，不为该查询主动唤醒休眠盘。基础指标约每2秒更新，设备详情由独立后台低频采集。小屏保留最近120次内存趋势；网页另有可关闭的SQLite历史，最长30日，提供温度、CPU、内存、网络和磁盘I/O趋势、今日／本月流量、本地告警、SMART健康观测、JSON报告及CSV导出。只在有可信RAPL读数时显示CPU封装功率，不当成整机功耗。历史会写入数据卷，可能影响其所在硬盘休眠，完整口径见[历史与统计](docs/monitoring.md)。

**CPU 与系统风扇现可分别配置。** 网页为两路各提供原厂管理、自定义温控或固定输出；每路单独多选温度源，按原始摄氏度的最高值运行，并显示触发来源、新鲜度和请求输出。静音／普通／全速阈值分别设置，阈值 15–85°C 且递增；静音／普通输出 50–100%，全速固定 100%。CPU 与硬盘都是 °C，但正常范围不同，混选时 CPU 可能主导。具体迟滞、保护、保存和接管流程见 [独立风扇温控指南](docs/fan-policy.md)。

已加载 hikio 的完整 GNU note 与支持版匹配，原厂 Unix GET 曾实测返回 b'5'/auto；这些证据不等于本版接管已经验证机械效果。新接管还需原厂温控二进制、唯一进程、字符设备和 RAM 适配层等身份检查。旧 RPM 接口仍不可用，目标百分比不能充当 RPM。原厂 `fan_mode` 的低／中／高／自动仍仅对应系统风扇，与新的两路独立温控分开；物理风扇对应和实际风速变化均待实机验证。详见 [原厂接口证据](docs/histor-interfaces.md)、[功能矩阵](docs/features.md) 与 [附件审查](docs/attachments.md)。

默认正文放大为 20px、辅助说明 18px，常用数据用单列大卡片展示；七个导航入口分成两行，长地址换行，复杂信息留在可滚动的二级/三级页。字号设置继续可调。

网页监控位于 `http://NAS_IP:18732`，直接打开即可查看总览、趋势、存储、网络、应用与设备；桌面侧栏、手机横向导航，支持明暗切换和减少动效。公开结构化接口为 `/api/v1/status`，另有历史、流量、告警、报告、CSV及OpenAPI。第三方读取不需要配对，详见[公开API](docs/public-api.md)。

只有设置和控制操作需要解锁：在小屏“设置 → 网页配置”生成五位数字配对码，180秒有效、一次使用。网页与小屏共用面板、两路风扇、灯光及统计配置；保存待用规则不会直接接管，应用后等待运行状态确认。已配对网页还可执行已有的确认式容器／虚拟机操作及5秒可取消关机／重启，不提供任意命令。会话最长8小时，服务重启需重新配对，配置不丢失。可设 `R1_WEB_PORT: '0'` 禁用网页，或 `R1_WEB_BIND: '127.0.0.1'` 限制为本机。

灯光按原厂DesignWare适配器识别逻辑自动寻找唯一 `0x26` 灯控芯片，验证 `0x01=0x55` 后读取模式、亮度及RGB，不需手选总线。支持手动灯效、多温度源最高温颜色渐变和跨午夜亮度上限计划。默认只读探测，显式应用的规则才可持久恢复；检测到原厂主题或其他程序改灯时让回控制。详见[灯光策略与固件证据](docs/light-policy.md)。

原有控制令牌仍只用于 NAS 本机小屏；普通前端文件预览保持只读。重启/关机需确认，提交后有 5 秒可取消倒计时；关机后不会自动开机。面板设置与独立风扇规则保存在同一独立 Docker 命名卷；`fan-policy.json` 分开记录待用规则与最后成功应用规则，只有成功应用的配置才会尝试开机恢复。单纯保存新参数不会替换当前运行或开机恢复的旧规则。IT87 hwmon 接管／曲线和 RGB 即时操作仍不作为开机自动执行配置保存。原厂系统风扇策略仅对匹配的已加载驱动开放，每次设置重新核验；它会由官方服务保存配置，恢复 UI 不自动撤销该策略，协议回执也不能替代物理效果验证。

监控展示、趋势和报告需求也参考 [fnMonitor](https://github.com/MisiteQ/fnmonitor)。信息分组和功能需求参考 [r1-toolbox-go](https://github.com/lemon6515/r1-toolbox-go)、[r1-frontscreen](https://github.com/mingyangbest/r1-frontscreen) 及用户提供的 R1 Panel 1.1.7、R1 Toolbox 4.8.10 安装包。本仓库的界面、采集与控制代码独立实现，不导入参考程序的授权模块或受限代码。当前继续使用官方 HistorSub 的 Electron 窗口。

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
python scripts/build-ui.py
node tests/check_histor_bridge.cjs
node tests/check_interactions.cjs
node tests/check_web_config.cjs
python tools/boot-once/test_runner.py
python -m unittest discover -s tests -v
docker build --platform linux/amd64 -t r1-toolbox:local .
docker run --rm --network none r1-toolbox:local --help
```

前端在 `ui/`；`runtime/ui_service.py` 提供 API 并管理小屏进程，`runtime/toolbox.py` 负责设置和硬件控制，`runtime/histor.py` 负责有明确协议的原厂接口适配，`runtime/insights.py` 负责系统、磁盘和应用采集，`runtime/device_info.py` 负责有界、只读设备库存；`runtime/temperature_sources.py` 提供带时效的温度来源，`runtime/fan_policy.py` 执行独立规则，`runtime/fan_native.py` 与 `runtime/fan_watchdog.py` 协调白名单适配层及独立恢复服务。修改后重新打包和构建镜像，无需将源码或页面复制到 NAS。

前端交互检查需要本机 Chrome/Chromium，可通过 `CHROME_BIN` 指定可执行文件；`playwright-core` 仅为开发依赖。小屏检查使用拦截数据，网页端到端检查使用真实本地 HTTP 服务、临时 sys/dev/data 和内存适配器；均不连接 NAS 或执行真实硬件操作。

自动检查覆盖脚本、Compose、指标采集、控制请求、包与缓存完整性，前端检查覆盖主页面、分级详情、返回/手势、触摸控制与缺失能力隐藏。临时 Linux CI 主机还验证实际只读挂载，以及测试容器真正停止并删除后，宿主测试小屏服务仍能读取内置 UI 和返回状态。CI 使用测试程序，不接触真实 NAS；通过这些检查不代表所有硬件控制已在实机验证。

发布产物还包括 `deployment-manifest.json` 和 `SHA256SUMS`，记录源码提交、镜像 ID、平台及文件校验值。真实 NAS 的小屏画面、触摸与厂商守护进程行为仍需部署验证。
