# Changelog

## Unreleased

- 初始化项目 AI 说明文件：新增 `AGENTS.md`、`CHANGELOG.md`。
- feat(web): 网页面板改为直接使用 fnMonitor 2.19.0 框架（源码在 `ui/panel-src/`），新增 `runtime/fnm_compat.py` 数据适配与 `runtime/fnm_auth.py` 可选登录口令；保留五位配对码、只读 `/api/v1/*`、确认流程和数据卷配置（`b906bec`）。
- feat(ui): 小屏改为触摸优先：五个底部大标签 + 右上角齿轮设置，点阵时钟、56px 点击目标、20px 正文；风扇/灯光改为只读状态并引导到网页，复杂规则不再在小屏编辑（`f0a7dc6`）。
- fix(ui): 补齐控制中心抽屉把手、关闭按钮与电源倒计时卡片的触摸样式。
- docs: 更新 README、小屏使用说明、功能矩阵，新增网页面板说明。
