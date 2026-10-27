`screen-actions.sh` 由一次性 Docker 项目进入宿主的 PID、mount、UTS、cgroup 命名空间及宿主根目录后执行；容器配置已共享宿主 network 和 IPC。宿主必须有 `python3`，且 `nsenter` 支持 `--cgroup`。

- 默认 `UI_DIR` 为空，只诊断，不停止小屏。旧 `NEW_HTML` 复制参数已停用。
- `UI_DIR` 填 NAS 上完整前端目录的绝对路径。工具通过宿主 transient service 的 `BindReadOnlyPaths` 将它只读挂载到解包版 `dist`，不复制 HTML，不覆盖原目录，不改启动脚本。
- 挂载只影响小屏服务自己的文件视图。宿主仍看到原文件；服务与挂载不依赖 Toolbox 容器继续运行。
- 停原程序前，用相同的绑定属性预检真实挂载身份和只读状态。新进程还需通过自身视图中的挂载、3 秒存活和独立 cgroup 检查。
- 启动失败时尽力停止新服务，再无自定义挂载地启动原解包程序。不创建页面备份或恢复文件。
- 执行记录位于 `/run/r1-toolbox`。日志：`journalctl -u histor-custom-<PID>.service`。
- 恢复方式是禁用项目自动启动或删除项目后重启 NAS；仅停止容器或清空 `UI_DIR` 不会立即撤销已经运行的小屏服务。

完整目录会遮住服务视图中的原 `dist`，所有所需前端资源需一并提供。只读挂载不等于整机零写入，程序自身仍可能写缓存和日志。准备、依赖与限制见 [部署说明](README.md)。
