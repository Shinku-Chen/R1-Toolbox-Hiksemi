# NAS 开机执行一次后停止的 Docker 项目

使用与逃生容器相同的 `docker.io/library/debian:bookworm-slim`、宿主 PID 和 `/host` 挂载。只需将 `compose.yaml` 导入 NAS 的 Docker 项目，或把完整 YAML 粘贴到项目编辑器；不需要先上传其它脚本，也不需要构建镜像或安装软件包。

## 第一次部署

1. 新建 Docker 项目，名称可用 `r1-boot-once`，导入 `compose.yaml` 后启动。
2. 保持默认 `NEW_HTML: ''`。本版只检查宿主环境，不停止小屏、不替换页面。
3. 打开容器日志，应看到诊断结果、`[OK] 本次任务完成，正在停止容器。`，随后容器处于“已停止”。
4. 从 NAS 管理页面正常重启。Docker 启动后，此项目会再次检查一次并停止。

`container_name` 与 `TASK_CONTAINER` 必须一致，均默认为 `r1-boot-once`。不要单独改其中一个，也不要在市场界面把重启策略改成 `unless-stopped`。

## 启用小屏文件替换

先把待安装的 HTML 放到 NAS 的持久目录，确保它引用的 JS、CSS、图片已存在于解包版的 `dist` 目录，或 HTML 本身不依赖外部资源。然后修改 YAML 中两项：

```yaml
environment:
  TASK_CONTAINER: r1-boot-once
  TASK_REVISION: '2'
  NEW_HTML: /screen/custom/index.html
```

`NEW_HTML` 必须换成你实际存放文件的宿主绝对路径；不能填容器视角的 `/host/...`，也不能直接填目标页面本身。上面只是路径示例，项目不会凭空生成这份 HTML。

保存并重新部署项目。每次改任务或页面版本时递增 `TASK_REVISION`；相同开机、相同任务版本成功过就会跳过，因此直接重新启动容器通常不会重复改文件。

启用替换后的执行顺序：

1. 等待 `/screen` 挂载、待安装文件和现有小屏主进程就绪。
2. 检查解包版、图形环境和宿主 `systemd-run` 是否可用，检查通过前不停止任何进程。
3. 备份目标 HTML；只停止已确认来自官方 AppImage 或指定解包目录的 `hiksemi-sub`，不停止 Xorg 或 NAS 后端。
4. 原子替换 `/screen/squashfs-root/resources/app/dist/index.html`。
5. 通过宿主临时 systemd 服务启动解包版 `AppRun`，检查主进程连续存活及独立 cgroup。
6. 记录本次开机成功，再由 Docker 明确停止容器。小屏由宿主服务继续运行。

这不修改 `/screen/screen-app-startup.sh`。即使官方开机流程先启动了原 AppImage，也会等它就绪后再进行上述替换。项目只替换 HTML，不会自动解包 AppImage。

## 运行机制

`restart: always` 配合作业末尾的 `docker stop r1-boot-once`。明确 stop 会抑制本次立即重启；下次 Docker 守护进程启动时，`always` 会再次启动该容器。直接 `exit 0` 配 `always` 会形成重复执行，不能替代明确 stop。

Docker 单独重启也会唤起容器。项目用宿主 `boot_id` 和 `TASK_REVISION` 去重，成功记录保存在 `/screen/docker-boot-once/last-success`；同一次 NAS 开机不会重复执行同一成功任务。失败不会写成功记录，但失败后同样明确停止容器，避免反复停屏。

从容器里 `chroot ... nohup 程序 &` 或 `nsenter` 后直接后台启动，进程仍可能处于 Docker cgroup，不能保证容器停止后存活。本项目让宿主服务管理器创建小屏服务，容器退出后小屏不依赖容器进程。

## 依赖与排错

- 宿主需要 `docker`、支持 `--cgroup` 的 `nsenter`、`mountpoint` 和 `python3`。进入宿主 cgroup 命名空间可确保独立服务的进程路径检查与 systemd 使用同一视角；它本身不会改变任务进程的 cgroup 归属。真正替换页面还需要可用的 `systemd-run`、`systemctl` 和已经运行的小屏图形环境。
- 自停接口不可用时，容器会记录错误并等待，不会反复执行任务；此时在市场界面停止容器，修正依赖或容器名后再启动。尚未执行任何任务的 Docker 接口检查失败也采用这一行为。
- 页面替换预检失败时不停止原程序。停止程序后的失败会尝试恢复原 HTML 并重新启动解包版，日志会报告恢复结果。
- 备份在目标 HTML 同目录，名称形如 `index.html.before-docker-日期时间-PID`。
- 小屏服务日志在宿主 `journalctl -u histor-custom-<PID>.service`。服务名会打印在容器日志。
- 普通重启后按上述流程重跑；固件和官方小屏升级后的路径、服务行为与接口兼容性仍需重新确认。
- 容器已完成并停止后，在市场点击“停止”并不能撤销 `always` 在下次 Docker 启动时的行为。要长期禁用项目，把 `restart` 改成 `"no"` 并重新部署，或使用市场的删除项目功能。

本工具尚未完成真实 NAS 验证。Compose 配置校验、Shell/Python 语法检查及 6 项本地模拟测试通过，覆盖成功/失败后的自停、停止接口失败不重复执行、开机与任务版本去重、失败后重试。测试使用临时伪宿主工具；首次真实开机循环、Linux PID 1 的行为、小屏实际画面和厂商 Docker 管理界面仍需在 NAS 上验证。

本地可执行 `docker-compose -f compose.yaml config --quiet` 和 `python3 test_runner.py` 复查。测试临时文件在 macOS 移入废纸篓，其它平台保留并报告路径。

## 本地源文件

`runner.sh.in` 处理自停，`host-job.sh.in` 处理每次开机去重，`screen-actions.sh` 是可选的小屏操作。`build-compose.py` 将它们打包进单个 YAML，并正确转义 Compose 的 `$`；修改源文件后运行该脚本重新生成 `compose.yaml`。

参考：[Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)；[systemd-run 临时服务](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)。
