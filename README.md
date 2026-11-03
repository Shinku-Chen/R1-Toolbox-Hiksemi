# R1 Toolbox

适用于海康 R1 NAS 的开源增强工具箱：不刷原厂系统，用 Docker 容器补上小屏、网页监控、风扇／灯光控制、完整 SMART、磁盘容量和受控 Docker 管理。

> 非海康官方项目。容器可回退，原厂 AppImage、启动脚本和厂商服务不被替换。

## 为什么用它

- **小屏不再只是状态展示**：六页触摸界面，概览／存储／网络／应用／温度／设置。
- **网页集中管理**：在局域网浏览器打开 `http://NAS_IP:18732`，查看实时指标、趋势、硬盘、容器和原厂业务状态。
- **硬盘数据更完整**：显示全部物理 HDD／SSD／NVMe，每块盘的使用率、温度、健康与完整 SMART 属性；休眠盘不因没有 SMART 而消失。
- **页面切换更稳定**：最近一次成功观测会写入 `disk-cache.json`，重启或切换页面先显示缓存，再尝试一次真实采集；页面不展示采样时间或缓存状态。
- **硬件控制集中在网页**：CPU／系统风扇分别配置，支持自动／固定档／手动曲线；机身灯光提供关闭、常亮、彩虹、呼吸四种原生模式。
- **影视中心排序补丁**：可选插件为原厂影视库增加追更排序，只在显式检测、确认和备份后应用，可撤销。
- **自定义温度风扇曲线**：每路风扇可多选温度来源并编辑静音／普通／全速阈值，保留原厂自动模式和故障回退。
- **API/MCP**：局域网 MCP 与固定 REST 接口，默认关闭；独立 Token 按角色、功能域、文件和来源授权，旧键不自动扩权。
- **可编程但不默认放开**：MCP 默认关闭；Token 分只读／读写／管理员、功能域和文件范围授权；开发者模式默认关闭，必须配置真实来源白名单和时限。
- **本地优先**：不使用云端账号，不上传监控数据；未知读数保持 `--`，不估算、不伪造。

## 界面

- **小屏**：适配 376×960，兼容 320px；大字号、56px 以上点击目标、六标签导航。
- **网页**：基于 [fnMonitor](https://github.com/MisiteQ/fnmonitor)（MIT）面板框架，重组为概览／存储／网络／应用／温度／设置六页。
- 详细页面和操作说明见 [小屏与网页使用说明](docs/interface-guide.md) 与 [网页面板](docs/web-panel.md)。

## 快速开始

### 要求

- 海康 R1 NAS，官方固件，x86_64。
- 可用的 Docker／Compose 环境。
- 一个独立数据卷 `r1_toolbox_data`（Compose 已声明）。**升级和重建必须复用它。**
- 先在不含关键数据的机器上验证；不要在生产数据盘上直接尝鲜。

### 在线部署

1. 从正式 `v*` 标签对应的 [Actions](https://github.com/Shinku-Chen/R1-Toolbox-Hiksemi/actions) 运行或 Release 下载成功构建的产物；`main` 推送本身不再发布镜像。
2. 在 NAS Docker 管理页用产物中的 `composer.yml` 创建或更新项目。
3. 启动容器，打开 `http://NAS_IP:18732`。

镜像：`ghcr.io/shinku-chen/r1-toolbox`，平台 `linux/amd64`。`dev` 只更新 `preview`；正式版本由 `v*` 标签同时发布 `latest` 和同版本标签，两者指向同一镜像。

### 离线部署

1. 下载同一产物的 `r1-toolbox-linux-amd64.tar` 和 `composer.yml`。
2. 在 NAS 上导入镜像。
3. 用对应 Compose 创建项目并启动。

### 精简 Compose 部署

以下是从 `composer.yml` 去掉注释后的最小配置，保存为 NAS 上的 `compose.yaml`：

```yaml
services:
  r1-toolbox:
    image: ghcr.io/shinku-chen/r1-toolbox:latest
    container_name: r1-toolbox
    restart: always
    stop_grace_period: 3m
    privileged: true
    pid: host
    ipc: host
    network_mode: host
    environment:
      UI_MODE: image
      R1_WEB_AUTH_MODE: password
    volumes:
      - /:/host
      - /dev:/host/dev
      - /proc:/host/proc
      - /sys:/host/sys
      - /run:/host/run
      - r1_toolbox_data:/data
volumes:
  r1_toolbox_data: {}
```

部署步骤：

1. 在 NAS 上创建项目目录，把上面的 `compose.yaml` 放进去。
2. 启动：

   ```sh
   docker compose -f compose.yaml up -d
   ```

3. 确认容器持续运行：

   ```sh
   docker compose -f compose.yaml ps
   docker compose -f compose.yaml logs --tail=100
   ```

4. 浏览器打开 `http://NAS_IP:18732`。
5. 在“设置 → 登录口令”设置口令；MCP 仍默认关闭，需要时在“设置 → API 管理”创建 Token。

升级只替换镜像标签并重建容器，保留 `r1_toolbox_data`：

```sh
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d
```

不要执行 `docker compose down -v`。固定版本可把 `latest` 换成正式 `v*` 标签或 `sha-*` 标签。

### 首次使用

- 默认是登录口令模式，首次没有口令，局域网内可直接打开网页管理；建议在“设置 → 登录口令”设置口令。
- 需要兼容旧五位配对流程时，显式设置 `R1_WEB_AUTH_MODE: pair`。
- 小屏默认开启；可在网页“设置 → 服务与开发”关闭并恢复官方界面。
- MCP 默认关闭，需要单独开启并创建 Token。
- 开发者模式默认关闭，仅用于固定诊断，不提供任意 Shell。

部署、升级、回退和端口说明见 [部署与升级](docs/deployment.md)。

## 安全与回退

- 不替换原厂 `AppImage`、解包目录或启动脚本；图像环境故障不会阻止网页启动。
- 更新复用原 Docker 项目和 `r1_toolbox_data` 数据卷，**不要执行 `down -v`**。
- 停止容器会尝试恢复官方小屏和原厂风扇／灯光状态；恢复失败会保留原因。
- SMART 默认使用待机保护；手动强制刷新可能唤盘，必须明确确认。
- 风扇输出百分比是请求值，不是实测 RPM；不伪造转速。
- 页面只显示可信来源；未知、过期、身份冲突保持未知，不填 0。
- 文件操作、MCP、Docker 和原厂业务接口都有独立授权与限制，详见对应文档。

## 文档

### 使用与界面

- [小屏与网页使用说明](docs/interface-guide.md)
- [功能矩阵](docs/features.md)
- [网页面板](docs/web-panel.md)
- [屏幕保护](docs/screen-saver.md)

### 存储与硬件

- [容量刷新与显示缓存](docs/capacity-refresh.md)
- [SMART 按需刷新](docs/smart-refresh.md)
- [磁盘名称](docs/disk-names.md)
- [独立风扇温控](docs/fan-policy.md)
- [灯光策略](docs/light-policy.md)
- [原厂接口证据](docs/histor-interfaces.md)

### 接口与自动化

- [MCP 与 API 管理](docs/mcp-api.md)
- [MCP 能力完成矩阵](docs/mcp-completion-matrix.md)
- [公开 API](docs/public-api.md)
- [开发者白名单检测](docs/dev-diagnostics.md)

### 文件、Docker 与业务

- [文件变更与恢复](docs/file-mutations.md)
- [Docker 部署](docs/docker-deployments.md)
- [业务数据来源与实机验收](docs/business-diagnostics.md)
- [影视追更排序](docs/media-sort-plugin.md)

### 运行与开发

- [架构与运行时](docs/architecture.md)
- [部署与升级](docs/deployment.md)
- [开发与测试](docs/development.md)
- [监控统计](docs/monitoring.md)

## 开发

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
python tools/boot-once/build-compose.py
python scripts/build-panel.py
python scripts/build-ui.py
python -m unittest discover -s tests -v
```

小屏源码在 `ui/index.html` 与 `ui/assets/`；网页面板源码在 `ui/panel-src/`，生成产物在 `ui/web/`。本地假 NAS 演示：

```sh
python tools/demo/run.py --screen-port 28731 --web-port 28732
```

完整构建、测试、CI 和发布契约见 [开发与测试](docs/development.md)。

## 声明

本项目为社区项目，与海康威视／HikSemi 无隶属关系。原厂硬件、固件、协议和接口可能随版本变化；所有硬件控制均应在可回退、非关键数据的环境先验证。网页面板框架来自 [fnMonitor](https://github.com/MisiteQ/fnmonitor)（MIT），需求与信息分组参考了 [r1-toolbox-go](https://github.com/lemon6515/r1-toolbox-go)、[r1-frontscreen](https://github.com/mingyangbest/r1-frontscreen) 及用户提供的 R1 Panel／R1 Toolbox 安装包；不导入参考程序的授权模块或受限代码。
