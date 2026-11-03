# 开发与测试

本文归档仓库结构、构建、测试、CI 和发布契约。运行方式见 [架构与运行时](architecture.md)，部署见 [部署与升级](deployment.md)。

## 目录

```text
runtime/           宿主端 Python：ui_service、web_config、toolbox、insights、histor、
                   fan_policy、fan_native、fan_watchdog、temperature_sources、light_policy、
                   monitoring、fnm_auth、fnm_compat、disk_cache、screen_diagnostics
ui/index.html      小屏触摸界面入口
ui/assets/         小屏 CSS/JS 与官方窗口桥
ui/panel-src/      网页面板源码（fnMonitor，MIT）与 R1 扩展
ui/web/            网页面板构建产物（不要手改）
scripts/           build-panel.py、build-ui.py、build-prototype.py
tools/boot-once/   容器前台、RAM 展开、挂载与常驻服务生命周期
tools/demo/        本地假 NAS：真实网页面板 + 真实小屏 UI
native/            fan_guard.c 窄范围风扇适配层
tests/             单元、HTTP、Linux 生命周期与无头浏览器检查
docs/              功能、接口、温控、灯光、监控、面板与运维文档
```

## 环境

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
```

需要 Python 3、Node.js、Docker（构建镜像时）；浏览器检查需要 Chrome／Chromium，可通过 `CHROME_BIN` 指定。

## 构建

```sh
python tools/boot-once/build-compose.py
python scripts/build-panel.py
python scripts/build-ui.py
python scripts/build-prototype.py
```

- 修改 `ui/panel-src/` 后必须运行 `build-panel.py`，生成 `ui/web/index.html` 和 `login.html`。
- 修改小屏样式、图标或交互后运行 `build-ui.py`，并同步 `docs/prototypes/r1-small-screen.html`；CI 会用 `--check` 拒绝未同步的样式和图标。
- `build-compose.py` 生成 `composer.yml`、`composer.dev.yml` 和 `tools/boot-once/runner.sh`。
- `ui/web/` 是构建产物，不要手改；先构建再提交。

## 本地演示

```sh
python tools/demo/run.py --screen-port 28731 --web-port 28732
```

假 NAS 提供真实网页面板和小屏 UI 的联动调试，不连接真实硬件。详见 [本地演示](../tools/demo/README.md)。

## 测试

```sh
python -m unittest discover -s tests -v
node tests/check_histor_bridge.cjs
node tests/check_interactions.cjs
node tests/check_web_config.cjs
node tests/check_network_curves.cjs
node tests/check_temperature_curves.cjs
node tests/check_api_management.cjs
node tests/check_service_settings.cjs
node tests/check_business_status.cjs
python tests/check_fan_startup_linux.py
python tests/check_fan_guard_linux.py
python tools/boot-once/test_runner.py
```

浏览器检查需要本机 Chrome／Chromium：

```sh
CHROME_BIN=/path/to/chrome node tests/check_web_config.cjs
```

Windows 本地可能因缺少 `fcntl`、`O_DIRECTORY`、`/bin/sh` 或符号链接权限出现环境性失败；以 Linux CI 结果为准。

## CI 与发布

Linux CI 依次检查：

- Python／Shell／JavaScript 语法；
- 生成文件未过期；
- fnMonitor 面板、窗口握手和触摸交互；
- 风扇启动与适配层；
- Compose、boot-once 生命周期和只读挂载；
- 文件变更、容量缓存与真实挂载；
- 镜像交付合同。

发布产物包括 `deployment-manifest.json`、`SHA256SUMS`、Compose 和离线镜像包。`dev` 推送只更新 `preview`；`main` 推送只检查、不发布镜像；正式发布由 `v*` 标签触发，同时发布 `latest`、同版本标签和不可变 SHA，三者指向同一镜像。细节见 [部署与升级](deployment.md)。

## 变更约定

- 改代码同步修改受影响测试；没有自动化落点的改动在提交说明中写清缺口和手工验证路径。
- 任何实际文件变更同步更新 `CHANGELOG.md`。
- 最终需求、架构边界和踩坑写进 `AGENTS.md`、README 或 `docs/`，不要只留在会话记录里。
- 中文正文使用全角标点；代码、路径和标识符保持原样。

