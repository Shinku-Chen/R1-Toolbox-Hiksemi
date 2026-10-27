# R1 Toolbox

海康 R1 NAS 的 Docker 工具箱。NAS 通过 `composer.yml` 部署本仓库构建的镜像，运行脚本随镜像交付。

最终部署产物是 **`composer.yml` 与 Docker 镜像**。同时提供 `r1-toolbox-linux-amd64.tar`，可在 NAS 上离线导入同一个镜像。源码仓库保持私有，发布的 GHCR 镜像可以公开拉取。

## 下载与部署

镜像仓库：`ghcr.io/shinku-chen/r1-toolbox`，目标平台为 `linux/amd64`，与当前海康 R1 一致。开发分支更新 `preview`，主分支更新 `latest`，每个构建还发布不可变的 `sha-<完整提交>` 标签。

1. 打开 [Actions](https://github.com/Shinku-Chen/R1-Toolbox/actions)，下载最新成功运行的部署产物。
2. 在线部署：将产物中的 `composer.yml` 导入 NAS Docker 市场的新项目，再启动项目。
3. 离线部署：先在 NAS 导入同包的 `r1-toolbox-linux-amd64.tar`，再导入该 `composer.yml`。两者使用相同的镜像标签，无需在 NAS 构建镜像。

发布包里的 Compose 固定为对应提交的镜像；仓库根目录的 [composer.yml](composer.yml) 默认使用 `preview`，也可用 `R1_TOOLBOX_IMAGE` 指定版本。配置不强制联网拉取，便于离线导入。

产物还包括 `deployment-manifest.json` 和 `SHA256SUMS`，用于核对提交、镜像 ID、平台和文件校验值。每次打包会验证 Docker archive 的标签和平台与部署文件一致。

## 首版功能

- 环境检查：默认 `UI_DIR` 为空，只输出宿主依赖、解包目录和小屏进程状态。
- 小屏目录挂载：指定 NAS 上的完整前端目录后，由宿主临时 systemd 服务只读挂载该目录并启动小屏。原版 `dist` 内容和启动脚本保持原样。
- 开机执行一次：同一次 NAS 开机、同一个 `TASK_REVISION` 成功后跳过重复操作，正常完成后容器自行停止。执行记录在 `/run/r1-toolbox` 临时目录中。

详细参数、目录准备与恢复方式见[部署说明](tools/boot-once/README.md)。`UI_DIR` 需包含 `index.html` 及其引用的 JS、CSS、图片；工具不会自动解包官方 AppImage。旧版 `NEW_HTML` 复制参数已停用，更新时请移除此项。

## 运行与更新

镜像继承 `docker.io/library/debian:bookworm-slim`，复用原逃生容器的 `/host` 和宿主命名空间机制。NAS 无需安装镜像构建工具，也无需复制运行脚本。

`restart: always` 配合明确停止自己，按宿主 `boot_id` 与任务版本去重；未配置目录时只诊断。小屏交给宿主服务管理器运行，私有挂载随小屏服务存活，容器停止不影响显示。工具不再向 `/screen` 写入页面、备份或执行记录；小屏自身仍可能写入正常缓存和日志。

源码或任务更新后重新发布镜像。部署时选择同一构建的 Compose 与镜像，修改 `TASK_REVISION` 可以在同一次开机重新执行新任务。

镜像首次发布时 GHCR 默认私有，需要在包设置中将**镜像包**设为公开。此设置独立于源码仓库可见性，源码仓库保持私有。公开状态以匿名拉取验证为准。

## 开发与构建

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python tools/boot-once/build-compose.py
python tools/boot-once/test_runner.py
python -m unittest discover -s tests -v
docker build --platform linux/amd64 -t r1-toolbox:local .
docker run --rm --network none r1-toolbox:local --help
```

自动流程检查生成文件、Compose、脚本和模拟测试，随后构建镜像、执行无宿主挂载的冒烟测试。非 PR 构建将镜像发布到 GHCR，并通过 `docker save` 导出离线镜像及部署文件。PR 仅构建与检查，不发布镜像。

镜像 CLI 支持默认 `run`、`--help`、`--version`。帮助与版本命令不访问 NAS 宿主。构建上下文采用白名单，只将工具运行必需的脚本放入镜像。

真实 NAS 的首次开机、小屏实际画面和厂商 Docker 市场行为仍需部署验证。
