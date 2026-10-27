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

原创 HTML/CSS/JS，适配 376×960 竖屏，支持点击导航和左右滑动：

- 概览：时间、CPU 使用率和温度、内存、网络速率、数据盘容量、运行时长。
- 存储：已挂载数据盘的容量与使用情况。
- 网络：默认路由网卡、IPv4 地址与上下行速率。

页面每 2 秒读取本机只读状态接口。缺失指标显示 `--`，首次差分显示采集中，连接中断后清除旧读数。无天气服务、外部字体、CDN 或硬件控制按钮。

信息分组参考 [r1-toolbox-go](https://github.com/lemon6515/r1-toolbox-go) 与 [r1-frontscreen](https://github.com/mingyangbest/r1-frontscreen)，本仓库的界面、采集代码和图形素材独立实现。当前继续使用官方 HistorSub 的 Electron 窗口。

## 宿主文件与运行方式

镜像基于 `docker.io/library/debian:bookworm-slim`，保留原逃生容器的宿主访问方式。启动时从当前容器复制内置 UI 包，验证校验值并展开到宿主 `/run/r1-toolbox`。**仅在确认该目录位于 tmpfs 后才展开 UI**。

宿主临时小屏服务只读挂载该前端目录，再运行轻量 Python 采集服务和原 `AppRun`。原版 AppImage、解包版 `dist` 与启动脚本保持原样；Toolbox 容器随后实际停止，小屏与采集服务由宿主 systemd 继续管理。状态接口只监听 `127.0.0.1:18731`。

运行时目录重启后清空，下一次开机从镜像重建。执行记录按宿主 boot ID、任务版本、镜像 ID 和 UI 配置去重；同一次开机更新镜像也会重新执行。Electron 自身、系统及 Docker 仍可能写正常缓存和日志。

详细依赖、目录模式和恢复方法见 [部署说明](tools/boot-once/README.md)。

## 开发

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python tools/boot-once/build-compose.py
python scripts/build-ui.py
python tools/boot-once/test_runner.py
python -m unittest discover -s tests -v
docker build --platform linux/amd64 -t r1-toolbox:local .
docker run --rm --network none r1-toolbox:local --help
```

前端在 `ui/`，只读采集与小屏进程管理在 `runtime/ui_service.py`。修改后重新打包和构建镜像，无需将源码或页面复制到 NAS。

自动检查覆盖脚本、Compose、指标采集、包与缓存完整性。临时 Linux CI 主机还验证实际只读挂载，以及测试容器真正停止并删除后，宿主测试小屏服务仍能读取内置 UI 和返回状态。CI 使用测试程序，不接触真实 NAS。

发布产物还包括 `deployment-manifest.json` 和 `SHA256SUMS`，记录源码提交、镜像 ID、平台及文件校验值。真实 NAS 的小屏画面、触摸与厂商守护进程行为仍需部署验证。
