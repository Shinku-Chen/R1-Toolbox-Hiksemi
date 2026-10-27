# R1 Toolbox 部署说明

## 直接使用镜像内置 UI

1. 从 Actions 下载成功构建的部署文件，或使用提供的 `composer.yml`。
2. 在 NAS Docker 市场更新原项目配置或创建项目，然后启动。
3. 默认 `UI_MODE: image`、`UI_DIR: ''`：无需上传任何 HTML 或前端目录。
4. 观察日志：镜像 UI 包校验、RAM 目录展开、只读挂载预检、小屏启动验证通过后，容器应显示“已停止”。

离线部署先在镜像页面导入同包的 `r1-toolbox-linux-amd64.tar`。Compose 与离线包的镜像标签必须一致。

当前仍需已有可运行的 `/screen/squashfs-root/AppRun`，不自动解包官方 AppImage。新版环境变量示例：

```yaml
environment:
  TASK_CONTAINER: r1-toolbox
  TASK_REVISION: '3'
  UI_MODE: image
  UI_DIR: ''
```

内置界面为概览、存储、网络三个页面，可点击导航或左右滑动。真实数据通过随小屏运行的 Python 服务持续采集，Toolbox 容器结束不影响刷新。未知温度、首次差分和读取失败会显示 `--` 或采集中，不填模拟数据。

## UI 模式

| UI_MODE | 用途 | UI_DIR |
|---|---|---|
| image | 使用镜像内置 UI，默认部署方式 | 必须为空 |
| diagnostic | 只检查环境，不停止或更换小屏 | 保持为空 |
| directory | 只读挂载用户在 NAS 数据盘上的完整前端目录 | NAS 宿主绝对路径 |

旧 Compose 未设置 `UI_MODE` 时，`UI_DIR` 非空按 directory 处理，否则只诊断。旧 `NEW_HTML` 复制参数已停用，请移除。

改变任务配置时递增 `TASK_REVISION`。更换镜像后，镜像 ID 的变化会自动触发同次开机重新执行，不会因旧成功记录而跳过。

## 镜像如何提供 UI

构建时将 `ui/` 和原创 Python 采集服务打包进镜像。容器启动后通过宿主 `docker cp` 读取内置包，先确认 `/run` 及其运行目录确实位于 tmpfs，再验证 SHA256、文件清单、路径、类型与大小限制。在随机暂存目录中校验完成后，原子发布到 `/run/r1-toolbox/ui-<内容摘要>`；复用缓存也重新校验。

宿主小屏临时服务将缓存的 `frontend` 目录通过 `BindReadOnlyPaths` 只读挂载到 `/screen/squashfs-root/resources/app/dist`。挂载仅影响该服务的文件视图，宿主原目录保持原样。采集进程和 `AppRun` 属于同一临时服务，容器停止或重新创建后均不依赖原容器的文件系统或 PID。

仅复制、展开到 `/run` 运行目录，不覆盖原版 AppImage、解包版前端或 `/screen/screen-app-startup.sh`。不绑定 Docker 内部镜像层路径，也不要求新版 Compose 的 image volume 语法。

停原程序前会先用同样的只读绑定属性启动检查服务，确认挂载身份与只读状态。新小屏主进程还需在独立 cgroup 连续存活 3 秒，并通过自己的 `/proc/PID/root` 文件视图验证挂载。失败时尽力停止新服务，无自定义挂载地恢复原解包程序。

## 实时指标

Python 标准库采集 `/proc`、sysfs 与真实数据盘挂载点，每 2 秒更新：

- CPU 使用率、可用的 CPU 温度、内存占用、运行时长。
- 默认路由网卡的 IPv4、上下行速率，避免累计 Docker 网桥。
- `/drives/` 下数据盘容量，按文件系统去重。

状态接口只监听 `127.0.0.1:18731/api/status`，不提供风扇、RGB、关机或其他写入控制。未知值为 null；不唤醒硬盘读取 SMART，不调用天气服务。当前未提供磁盘温度与多网卡聚合。

采集进程随小屏退出；停止宿主小屏服务会同时结束采集与页面进程。若本地端口已被无关程序占用，新服务会启动失败并尝试恢复原界面，不覆盖或结束占用端口的程序。

## 开机一次与恢复

保留 `restart: always`。任务完成或失败后明确调用 Docker 停止当前容器，下次 Docker 启动时再次运行；同次 NAS 开机、相同任务版本、镜像和 UI 配置成功后跳过。成功记录在 `/run/r1-toolbox/last-success`，失败不记录。

恢复官方界面：先删除项目，或将 `restart` 改为 `"no"` 后重新部署，再正常重启 NAS。临时服务、内存 UI 和挂载随重启消失，官方启动流程继续使用原文件。

容器本来就会停止，仅停止容器或改为诊断不会立即撤销已运行的小屏服务。旧版若曾覆盖过 HTML，需要另外恢复当时的备份；内存挂载不会回退历史文件修改。

## 自定义目录模式

将自有界面放在数据盘，例如 `/drives/实际磁盘/R1-Toolbox/ui`，包含 `index.html` 及其引用的 JS、CSS、图片。然后设置：

```yaml
TASK_REVISION: '4'
UI_MODE: directory
UI_DIR: /drives/实际磁盘/R1-Toolbox/ui
```

这是宿主路径，不是 `/host/...`。无需额外添加 Docker volume，已有宿主访问入口会让小屏服务建立私有挂载。目录会遮住服务视图中的整个原版 `dist`，必须提供完整资源。directory 模式直接启动原 AppRun，不额外提供内置 UI 的状态 API。

目录路径支持普通中文，不支持空白、冒号、反斜杠、引号、`%`、`$`、控制字符。源目录不能与目标相同或互为祖先；原子更名整个源目录后需要重跑挂载。

## 依赖、日志与边界

- 宿主需要 `docker`、支持 `--cgroup` 的 `nsenter`、`mountpoint`、`python3`。实际挂载还需要 systemd 233 起的 `BindReadOnlyPaths`、`systemd-run`、`systemctl` 和现有小屏图形环境。以实际预检通过为准。
- 容器名与 `TASK_CONTAINER` 保持 `r1-toolbox`。镜像继承 `docker.io/library/debian:bookworm-slim`，保留原宿主访问机制，不联网安装宿主工具。
- 镜像模式要求 `/run` 为 tmpfs；不满足时在停原小屏前拒绝部署 UI。缺少依赖或包损坏也不会停止原程序。
- 临时服务名打印在容器日志，宿主可用 `journalctl -u histor-custom-<PID>.service` 查看。失败暂存保留至重启，便于排查。
- 执行状态和 UI 位于运行时目录；Electron、系统、Docker 仍可能写正常配置、缓存和日志，不能保证整机零写入。
- 真实 NAS 的画面、触摸、开机时序及厂商守护程序行为需要实机验证。

参考：[systemd 私有挂载](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#BindPaths=)、[Docker cp](https://docs.docker.com/reference/cli/docker/container/cp/)、[Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)。
