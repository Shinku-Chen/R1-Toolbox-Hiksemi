# R1 Toolbox 部署说明

NAS 使用 `composer.yml` 指向 `ghcr.io/shinku-chen/r1-toolbox` 镜像。运行脚本已经装进镜像，部署时无需粘贴脚本或在 NAS 构建镜像。

## 第一次部署

1. 从本仓库 Actions 下载成功构建的部署产物。
2. 在线使用：在 NAS Docker 市场新建项目，导入该包的 `composer.yml` 后启动。
3. 离线使用：先导入同包的 `r1-toolbox-linux-amd64.tar`，再导入 Compose。镜像标签必须保持一致。
4. 保持默认 `NEW_HTML: ''`。容器只检查环境，不停止小屏、不替换页面；日志显示完成后，容器应处于“已停止”。
5. 从 NAS 管理页面正常重启。Docker 启动后会再次执行一次并停止。

容器名和 `TASK_CONTAINER` 都是 `r1-toolbox`，需要保持一致。保留配置中的 `restart: always`。产物 Compose 固定为本次构建的 sha 镜像，根目录配置默认是 `preview` 通道。

## 启用小屏页面替换

先把待安装的 HTML 放到 NAS 持久目录。它引用的 JS、CSS、图片须已存在于解包版 `dist` 中，或 HTML 本身不依赖这些外部资源。修改环境变量，例如：

```yaml
environment:
  TASK_CONTAINER: r1-toolbox
  TASK_REVISION: '2'
  NEW_HTML: /screen/custom/index.html
```

`NEW_HTML` 换成实际的宿主绝对路径，不能使用容器视角 `/host/...`，也不能指向目标页面本身。每次更换页面或任务时递增 `TASK_REVISION`，再重新部署。

执行顺序：等待 `/screen` 挂载及现有小屏就绪，预检解包版与宿主服务工具，备份 HTML，只停止已确认的 `hiksemi-sub`，原子替换 `/screen/squashfs-root/resources/app/dist/index.html`，由宿主临时服务运行 `AppRun`，确认主进程存活及独立 cgroup 后记录成功，最后停止容器。

本工具不修改 `/screen/screen-app-startup.sh`，不停止 Xorg 或 NAS 后端。官方开机流程可以先启动原小屏，工具等待它就绪后执行替换。

## 开机一次与自停

`restart: always` 的容器在任务结束后明确调用 `docker stop` 停止自己；下次 Docker 守护进程启动时再次运行。宿主 `boot_id` 与 `TASK_REVISION` 的成功记录在 `/screen/docker-boot-once/last-success`。单独重启 Docker 时，同次 NAS 开机成功过的同一任务会跳过。

任务失败不记录成功，也会明确停止容器，避免循环停屏。Docker 接口或自停请求不可用时，容器打印错误并等待，不反复执行任务；在市场界面停止并修正依赖后重试。

启动小屏通过宿主 `systemd-run` 创建独立服务，避免仅在容器里后台启动后又被容器停止操作清理。

## 依赖、日志与恢复

- 宿主需要 `docker`、支持 `--cgroup` 的 `nsenter`、`mountpoint` 和 `python3`。实际替换还需要可用的 `systemd-run`、`systemctl` 和现有小屏图形环境。
- 镜像不联网安装上述宿主工具。缺少依赖时先报告，不停止原程序。
- 预检失败时保持原程序；停止后的失败会尽力恢复 HTML 并重新启动解包版，结果记录在容器日志。
- HTML 备份在目标同目录，名称形如 `index.html.before-docker-日期时间-PID`。
- 小屏服务名打印在容器日志，宿主日志命令为 `journalctl -u histor-custom-<PID>.service`。
- 固件或官方小屏升级后，需要重新检查解包目录、宿主依赖及厂商守护程序是否会抢回界面。
- 长期禁用项目时，将重启策略改为 `"no"` 后重新部署，或在市场删除项目。仅停止一个已自停的 `always` 容器不会取消下次 Docker 启动的恢复行为。

## 镜像与产物校验

发布包包括 `composer.yml`、`r1-toolbox-linux-amd64.tar`、`deployment-manifest.json`、`SHA256SUMS`。镜像文件是 `docker save` 格式，可以通过 Docker 市场导入；不是容器文件系统导出。

部署文件固定为与 tar 内 RepoTag 一致的镜像，manifest 记录源码提交、镜像 ID 和 `linux/amd64` 平台。源码仓库保持私有，镜像包可独立设为公开；公开后在线部署不需要 GitHub 登录。

自动检查包含模拟宿主生命周期和部署契约测试。真实 NAS 的首次开机、自停、屏幕画面和厂商管理界面仍需验证。

参考：[Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)；[GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。
