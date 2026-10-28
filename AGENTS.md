# AGENTS.md

给 AI agent（Claude Code / Codex / Cursor 等）读的仓库说明。本文件只写这个项目特有的约定；通用的个人规范由 Shinku 的 `cli/shared/*.md` 提供。

## 项目概述

R1 Toolbox 是海康 HikSemi R1 NAS 的本地工具箱：通过 Docker Compose 部署一次镜像，把宿主小屏界面、网页监控/设置和硬件控制装进官方固件，不改写原厂 AppImage、启动脚本和厂商服务。

- 小屏：`ui/index.html` + `ui/assets/`，面向 376×960 触摸屏，复杂设置引导到网页。
- 网页：`http://NAS_IP:18732`，直接使用 fnMonitor 2.19.0 的面板框架（MIT，源码固定在 `ui/panel-src/`），由 `runtime/fnm_compat.py` 用本机快照填充数据；R1 专有模块与设置在 `ui/panel-src/50-r1.js`。
- 宿主服务：`runtime/ui_service.py` 起步，提供本机只读 API（18731）、网页入口（18732）和原厂窗口。

## 目录结构

```
runtime/           宿主端 Python：ui_service、web_config、toolbox、insights、device_info、histor、
                   fan_policy、fan_native、fan_watchdog、temperature_sources、light_policy、monitoring、
                   fnm_auth、fnm_compat
ui/index.html      小屏触摸界面入口（随镜像展开到宿主 /run）
ui/assets/         小屏 CSS/JS 与官方窗口桥
ui/panel-src/      网页面板源码（fnMonitor，MIT）与 R1 扩展 50-r1.js
ui/web/            网页面板构建产物 index.html / login.html（不要手改）
scripts/build-panel.py  构建网页面板
scripts/build-ui.py     打包镜像内 UI bundle
tools/boot-once/   宿主一次性部署、挂载、服务生命周期
tools/demo/        本地假 NAS：真实网页面板 + 真实小屏 UI 的联动演示
native/            fan_guard.c 窄范围风扇适配层
tests/             单元、HTTP、Linux 生命周期与无头浏览器检查
docs/              功能、接口、温控、灯光、监控、面板说明
```

## 构建与验证

```bash
python scripts/build-panel.py          # 修改 ui/panel-src/ 后必须执行
python scripts/build-ui.py             # 打包 UI bundle（镜像构建也会执行）
python -m unittest discover -s tests -v
node tests/check_interactions.cjs      # 小屏触摸/导航/设置，需要 Chrome/Chromium
node tests/check_web_config.cjs        # 网页面板 + 真实 HTTP fixture
node tests/check_small_screen_prototype.cjs  # 独立小屏原型（离线 HTML）
python tests/check_fan_guard_linux.py
python tests/check_fan_startup_linux.py
python tools/boot-once/test_runner.py
python tools/demo/run.py --screen-port 28731 --web-port 28732  # 本地联调
docker build --platform linux/amd64 -t r1-toolbox:local .
```

改动后至少跑与改动范围匹配的定向测试；提交前跑完整套件。浏览器检查需要 `CHROME_BIN` 指向 Chrome/Chromium。CI（Linux）的结果才是最终判据，Windows 上部分依赖 `os.sysconf` / `statvfs` / 符号链接的测试会失败，不要据此判断功能回归。

## 安全与硬件边界

这些是产品红线，不是建议：

- 不绕过风扇接管预检：必须核验原厂温控二进制、唯一进程、字符设备与已加载驱动身份；不伪造 RPM，不盲扫 I²C，不停转风扇，不用目标百分比冒充实测转速。
- SMART 只用 `smartctl -n standby` 且走后缓存；打开网页/详情不得临时启动自检或唤醒休眠盘。
- 功耗只显示 RAPL 实测的 CPU 封装；不估算整机功耗，不把封装功耗当整机功耗。
- 网页监控默认公开只读；写操作必须五位一次性配对码或登录口令会话，并保留同源/CSRF 校验、确认流程与 5 秒可取消电源操作。小屏控制令牌不得发给网页。
- 部署/升级必须复用原 Docker 项目与 `r1-toolbox-data` 数据卷；不删除卷、不使用 `down -v`。删除部署后正常重启可恢复官方显示。
- 未知读数保持 `null` / `--`，不用 0 或猜测填补；不可用的能力隐藏或标注原因，不生成占位卡片。

## 代码约定

- 小屏是触摸优先：最小点击目标 56px、正文 20px、单列布局、底部大标签导航；不引入 hover 依赖、PC 双栏或需要精确指针的交互。风扇/灯光策略、登录口令等复杂设置只放在网页。
- 网页面板优先复用 fnMonitor 的 `defineModule`、设置分组、抽屉与控件；R1 专有内容放 `50-r1.js` / `12-style-r1.css`，不改上游渲染逻辑。构建时只做品牌与功耗文案替换。
- 对不自解释的函数、接口、结构字段、请求参数和关键返回值补简洁注释，说明职责、语义和使用边界；不要写变量赋值式废话。
- 改代码必须同步新增或修改受影响的测试；没有自动化落点的改动要在汇报里说明缺口与手工验证路径。
- 任何实际文件变更都要更新 `CHANGELOG.md`（本仓库已存在）。最终需求、架构边界、踩坑、运行/测试/发布方式写进本文件、README 或 docs，不要只留在会话记忆里。
- 项目不使用 Redis，无 TTL 约定。
- 中文正文、UI 文案与文档使用全角标点；代码、路径、标识符保持原样。

## 提交规范

- commit 标题：`type(scope): 简述`，`type` ∈ `feat/fix/docs/refactor/perf/test/chore/build/ci`；祈使句、≤50 字符、结尾不加句号，默认中文，技术术语保留英文。
- 一个 commit 只做一件事；不把无关修复、格式化或临时探针混进来。
- 改完当轮 push；协作/公开动作（开 PR、发版）按仓库规则另行确认。
- push 前检查 `tests/`、`scripts/build-panel.py` 产物与文档是否同步；`ui/web/` 为构建产物，先构建再提交。
