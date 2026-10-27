`screen-actions.sh` 是可选的宿主小屏操作示例，由一次性 Docker 项目通过 `nsenter` 进入宿主的 PID、mount、UTS、cgroup 命名空间及宿主根目录后执行；容器配置已经共享宿主的 network 和 IPC 命名空间。宿主必须有 `python3`，且 `nsenter` 支持 `--cgroup`。

- 默认 `NEW_HTML` 为空，只检查解包目录、宿主服务工具和小屏进程，随后返回，不改文件、不停止进程。
- 要启用替换，把 `NEW_HTML` 设置为 NAS 宿主上现有 HTML 的绝对路径，例如 `/screen/custom/index.html`；必须与目标文件不同。
- 目标固定为 `/screen/squashfs-root/resources/app/dist/index.html`；需要提前解包。脚本不会修改 `/screen/screen-app-startup.sh`，不会自动解包，也不会创建持久的 systemd unit。
- 变更前要求唯一的小屏主进程，继承其必要图形环境。预检服务管理工具和图形环境后，备份 HTML、定向停止 `hiksemi-sub`、原子换文件，再通过宿主的 transient service 运行 `AppRun`。不会停止 `Xorg` 或 `emhistor`。
- 成功要求新主进程在独立宿主 service cgroup 内连续存活 3 秒。容器随后可以停止，小屏由宿主 systemd 继续管理。日志：`journalctl -u histor-custom-<PID>.service`。
- 失败时尽量恢复原 HTML；如果没有其它小屏进程，会用宿主服务再运行解包版。备份为目标目录下 `index.html.before-docker-<时间>-<PID>`。官方守护程序、固件更新及应用更新的行为尚未验证，不能保证不会抢回界面或覆盖文件。

如果未配置页面输入，本示例就是安全的运行机制检查。只有 HTML 引用的相对资源已经在原 `dist` 目录内时，单独替换 HTML 才足够。
