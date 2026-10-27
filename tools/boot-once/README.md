# R1 Toolbox 部署说明

NAS 使用 `composer.yml` 指向 `ghcr.io/shinku-chen/r1-toolbox` 镜像。运行脚本已装进镜像，部署时无需粘贴脚本或在 NAS 构建镜像。

## 第一次部署

1. 从本仓库 Actions 下载成功构建的部署产物。
2. 在线使用：在 NAS Docker 市场新建项目，导入该包的 `composer.yml` 后启动。
3. 离线使用：先导入同包的 `r1-toolbox-linux-amd64.tar`，再导入 Compose，保持镜像标签一致。
4. 保持默认 `UI_DIR: ''`。容器只检查环境，不停止小屏；任务完成后容器应处于“已停止”。
5. 从 NAS 管理页面正常重启。Docker 启动后会再次执行一次并停止。

容器名和 `TASK_CONTAINER` 都是 `r1-toolbox`，需要保持一致。保留 `restart: always`。产物 Compose 固定为本次构建的 sha 镜像，根目录配置默认是 `preview` 通道。

## 准备并挂载自定义界面

在 NAS 数据盘建立自己的前端目录，例如 `/drives/实际磁盘/R1-Toolbox/ui`：

```text
ui/
  index.html
  assets/       # 页面用到的 JS、CSS、图片等
```

目录需包含页面引用的全部资源。单个自包含 HTML 可以直接作为 `index.html`；依赖官方资源的页面需要同时提供相应资源，目录挂载会遮住该小屏服务视图里的整个原版 `dist`。

修改 Compose 环境变量，填写真实的 NAS 宿主路径：

```yaml
environment:
  TASK_CONTAINER: r1-toolbox
  TASK_REVISION: '2'
  UI_DIR: /drives/实际磁盘/R1-Toolbox/ui
```

`UI_DIR` 不是容器视角的 `/host/...` 路径。支持普通中文目录；为避免 systemd 解析歧义，路径暂不支持空白、冒号、反斜杠、引号、`%`、`$` 和控制字符。源目录不能与原 `dist` 相同，也不能包含或位于原 `dist` 内。修改目录、内容或任务后递增 `TASK_REVISION`，再重新部署。

无需为 `UI_DIR` 再增加 Docker volume。已有 `/:/host` 提供宿主访问入口；工具让**宿主小屏服务**通过 `BindReadOnlyPaths` 将该目录只读挂载到 `/screen/squashfs-root/resources/app/dist`。普通 Docker volume 仅影响容器，无法单独改变宿主小屏进程看到的文件。

启动前先用同样的挂载属性运行临时检查服务，确认目标目录确实指向源目录并只读，再停止已确认的 `hiksemi-sub`。新小屏主进程必须连续存活 3 秒、位于独立服务 cgroup，且从它自己的文件视图验证只读挂载，之后才记录成功并停止 Toolbox 容器。

原版 AppImage、解包版 `dist` 和 `/screen/screen-app-startup.sh` 不被覆盖。私有挂载由宿主 systemd 服务保持，容器停止后继续有效；宿主全局查看原版目录仍能看到原文件。

旧版 `NEW_HTML` 复制参数已停用，更新 Compose 时请移除此项。

## 开机一次与自停

`restart: always` 的容器在任务结束后明确调用 `docker stop` 停止自己；下次 Docker 守护进程启动时再次运行。宿主 `boot_id` 与 `TASK_REVISION` 的成功记录在 `/run/r1-toolbox/last-success`，只在当前 NAS 开机期间保留。单独重启 Docker 时，同次 NAS 开机成功过的同一任务会跳过。

任务失败不记录成功，也会明确停止容器，避免循环停屏。Docker 接口或自停请求不可用时，容器打印错误并等待，不反复执行任务；在市场界面停止并修正依赖后重试。

## 恢复官方界面

先删除 Toolbox 项目，或将 `restart` 改为 `"no"` 后重新部署，再正常重启 NAS。临时小屏服务和挂载随重启消失，官方开机流程启动原版界面。

容器运行完成后本来就会停止，因此仅停止容器、或将 `UI_DIR` 清空再运行诊断，均不会立即撤销已经运行的小屏服务。挂载不回退旧版曾经执行过的文件复制；如果使用过旧版替换功能，需先恢复当时的原文件备份。

## 依赖、日志与限制

- 宿主需要 `docker`、支持 `--cgroup` 的 `nsenter`、`mountpoint` 和 `python3`。实际挂载还需要支持 `BindReadOnlyPaths` 的 systemd（233 起）、`systemd-run`、`systemctl` 和现有小屏图形环境，以实际预检通过为准。
- 需已存在可运行的 `/screen/squashfs-root/AppRun`。工具不会自动解包，也不会停止 Xorg 或 NAS 后端。
- 预检失败不停止原程序。停止后的失败会尽力停止新服务，并无自定义挂载地重新运行原解包程序，结果记录在容器日志。
- 小屏服务名打印在日志，宿主日志命令为 `journalctl -u histor-custom-<PID>.service`。
- 只读是小屏服务的挂载视图，数据盘上的目录仍可由用户更新。原子更名整个源目录后需重新执行挂载，已挂载的旧目录不会自动切换。
- 工具仅在 `/run` 写临时执行记录与服务运行信息；Electron 自身及系统仍可能写正常缓存和日志，不能保证整机零写入。
- 固件升级、官方小屏升级或厂商守护程序抢回界面的行为仍需实机验证。

## 镜像与产物校验

发布包包括 `composer.yml`、`r1-toolbox-linux-amd64.tar`、`deployment-manifest.json`、`SHA256SUMS`。镜像文件是 `docker save` 格式，可通过 Docker 市场导入。

部署文件固定为与 tar 内 RepoTag 一致的镜像，manifest 记录源码提交、镜像 ID 和 `linux/amd64` 平台。源码仓库保持私有，镜像包可独立设为公开；公开后在线部署不需要 GitHub 登录。

自动检查覆盖模拟生命周期、挂载操作与交付契约。真实 NAS 的首次开机、小屏画面和厂商管理界面仍需验证。

参考：[systemd 私有绑定挂载](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#BindPaths=)、[Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)、[GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。
