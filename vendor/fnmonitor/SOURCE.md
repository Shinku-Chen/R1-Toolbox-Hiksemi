# fnmonitor 面板框架来源

来源：https://github.com/MisiteQ/fnmonitor

固定提交：`d002d1d2e898222e989786ba607dad9810dcdb9a`（版本 2.19.0，MIT）。

## 本轮移植内容

网页端现在是 fnMonitor 的面板本身，而不是只参考其样式：

- `ui/panel-src/`：完整前端源码（页面结构、样式、点阵数字、磁贴、顶部导航、手机底部导航、
  详情抽屉、搜索、布局编辑、设置页），构建为单文件 `ui/web/index.html`。
- `ui/panel-src/login.html`：口令登录页，构建为 `ui/web/login.html`。
- `runtime/fnm_auth.py`：PBKDF2-HMAC-SHA256 口令哈希、持久会话（只存 sid 的 SHA-256）、
  登录失败限速，沿用 fnMonitor 的参数与行为。
- `runtime/fnm_compat.py`：fnMonitor 的 HTTP 契约（`/api/overview`、`/api/history`、
  `/api/docker` 等）由 Toolbox 现有快照填充；不复制 fnOS 专有集成。
- `runtime/web_config.py`：在原有 18732 端口上同时提供面板、只读 `/api/v1/*`、
  五位配对会话与可选口令登录。
- `ui/panel-src/50-r1.js`、`12-style-r1.css`：R1 专有的双路风扇、机身灯光、小屏与电源、
  登录口令和连接设置；使用 fnMonitor 的 `defineModule` / 设置分组 / 图标与控件实现。

## 保留与限制

- 上游渲染代码与样式未改写，品牌文字替换集中在 `scripts/build-panel.py`。
- fnOS 专有功能（应用中心、天气、iDRAC、阵列卡、巡检、自更新、通知）在 R1 上返回明确的
  “不可用”，不以占位卡片伪装；对应设置分组不显示。
- 监控仍公开只读；修改设置与设备操作需要小屏五位配对码，或开启登录口令后使用会话。
- 完整 MIT 许可位于 `vendor/fnmonitor/LICENSE`。
