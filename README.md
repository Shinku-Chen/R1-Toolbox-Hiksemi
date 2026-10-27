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

原创 HTML/CSS/JS，适配 376×960 竖屏，支持点击导航、左右滑动和页面内滚动。七个页面都连接本机 API：

| 页面 | 内容 |
|---|---|
| 概览 | 时间、CPU/温度、内存、网络速率、数据盘容量、运行时长 |
| 存储 | 已挂载数据盘容量、物理磁盘信息、可用的温度与 SMART 健康状态 |
| 网络 | 默认网卡及各接口地址、状态、上下行速率 |
| 控制 | 亮度、息屏/唤醒、可用的风扇与 RGB 控制、重启/关机 |
| 应用 | Docker 容器及资源占用、启动/停止/重启；可用的 libvirt 虚拟机操作 |
| 系统 | CPU 分核、负载、Swap、进程、监听端口、GPU 信息、最近采样趋势 |
| 设置 | 四套主题、80–140% 字号、启用页面、自动轮播、无操作息屏、夜间低亮度 |

未知指标显示 `--`，首次差分显示采集中，连接中断后清除旧读数。SMART 使用 `smartctl -n standby`，休眠盘不会为了监控被主动唤醒。趋势仅保留内存中最近 120 次采样，不是长期历史数据库。

**当前官方固件 `6.1.66-001` 只发现 `gpio-it87`，没有 IT87 hwmon/PWM 风扇驱动，因此这台 NAS 暂时不能调风扇。** 页面会明确显示不可用；程序不自动加载驱动，也不通过 `/dev/port` 操作硬件。RGB 同样需要明确选择总线并通过设备读取验证，不能仅凭有按钮判断硬件可控。详细功能对照见 [功能与支持条件](docs/features.md)。

硬件和应用操作只在 NAS 小屏中开放；普通浏览器预览只读。重启/关机需确认，提交后有 5 秒可取消倒计时；关机后不会自动开机。设置保存在独立 Docker 命名卷，风扇接管和 RGB 选择不作为开机自动执行配置保存。

信息分组和功能需求参考 [r1-toolbox-go](https://github.com/lemon6515/r1-toolbox-go)、[r1-frontscreen](https://github.com/mingyangbest/r1-frontscreen) 及用户提供的 r1-panel 安装包。本仓库的界面、采集与控制代码独立实现，不导入参考程序的授权模块或受限代码。当前继续使用官方 HistorSub 的 Electron 窗口。

## 宿主文件与运行方式

镜像基于 `docker.io/library/debian:bookworm-slim`，保留原逃生容器的宿主访问方式。启动时从当前容器复制内置 UI 包，验证校验值并展开到宿主 `/run/r1-toolbox`。**仅在确认该目录位于 tmpfs 后才展开 UI**。

宿主临时小屏服务只读挂载该前端目录，再运行 Python 监控/控制服务和原 `AppRun`。原版 AppImage、解包版 `dist` 与启动脚本保持原样；Toolbox 容器随后实际停止，小屏与服务由宿主 systemd 继续管理。API 只监听 `127.0.0.1:18731`，读取接口与需要启动时随机令牌的控制接口分开。

UI、服务和采样历史所在的运行时目录重启后清空，下一次开机从镜像重建。`/data` 的独立命名卷保存 `settings.json`，容器停止和 NAS 重启后仍保留；不提供数据卷时设置仅保留到本次开机结束。执行记录按宿主 boot ID、任务版本、镜像 ID 和 UI 配置去重；同一次开机更新镜像也会重新执行。Electron 自身、系统及 Docker 仍可能写正常缓存和日志。

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
python tools/boot-once/test_runner.py
python -m unittest discover -s tests -v
docker build --platform linux/amd64 -t r1-toolbox:local .
docker run --rm --network none r1-toolbox:local --help
```

前端在 `ui/`；`runtime/ui_service.py` 提供 API 并管理小屏进程，`runtime/toolbox.py` 负责设置和硬件控制，`runtime/insights.py` 负责系统、磁盘和应用采集。修改后重新打包和构建镜像，无需将源码或页面复制到 NAS。

前端交互检查需要本机 Chrome/Chromium，可通过 `CHROME_BIN` 指定可执行文件；`playwright-core` 仅为开发依赖。检查使用拦截的测试数据，不连接 NAS 或执行真实硬件操作。

自动检查覆盖脚本、Compose、指标采集、控制请求、包与缓存完整性，前端检查覆盖七页交互与缺失能力的显示。临时 Linux CI 主机还验证实际只读挂载，以及测试容器真正停止并删除后，宿主测试小屏服务仍能读取内置 UI 和返回状态。CI 使用测试程序，不接触真实 NAS；通过这些检查不代表所有硬件控制已在实机验证。

发布产物还包括 `deployment-manifest.json` 和 `SHA256SUMS`，记录源码提交、镜像 ID、平台及文件校验值。真实 NAS 的小屏画面、触摸与厂商守护进程行为仍需部署验证。
