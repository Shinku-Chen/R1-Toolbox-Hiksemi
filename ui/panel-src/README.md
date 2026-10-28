# 面板源码（来自 fnmonitor）

这里的文件是 [fnMonitor](https://github.com/MisiteQ/fnmonitor) 的前端源码，固定提交
`d002d1d2e898222e989786ba607dad9810dcdb9a`（版本 2.19.0，MIT 许可，完整许可见
`vendor/fnmonitor/LICENSE`）。网页面板直接以它为框架运行，不再另写一套页面。

## 构建

```sh
python scripts/build-panel.py
```

脚本按 fnMonitor 自己的 `tools/build_ui.sh` 顺序拼接并压缩：

- `00-head.html` + `10-style.css` + `11-style-add.css` + `12-style-r1.css` → 单文件样式
- `20-icons.svg` + `21-body.html` → 页面结构
- `30-core.js` … `49-app.js` → 面板逻辑（`50-r1.js` 在 `49-app.js` 之前）
- 输出 `ui/web/index.html` 与 `ui/web/login.html`

`ui/web/*` 是构建产物，不要手改；改源码后重新运行脚本。构建会做 `node --check`
语法检查（未安装 node 时跳过）。

## 与上游的差异

上游文件只做最小改动：`scripts/build-panel.py` 的 `BRANDING` 表替换产品名和窗口说明，
功耗文案改为“CPU 封装（RAPL）实测，不估算整机功耗”；`21-body.html`、`33-state.js`
与 `48-layout.js` 把导航重组为与小屏一致的概览／存储／网络／应用／温度／设置六页，
并按页分配模块；`46-vms.js` 不参与构建。以后同步上游时需保留这些 R1 差异并重跑构建。

R1 专有内容全部在 `50-r1.js` 与 `12-style-r1.css`：

| 位置 | 内容 |
|---|---|
| `50-r1.js` 模块 | `r1cool` 双路温控、`r1light` 机身灯光、`r1screen` 小屏与电源 |
| `50-r1.js` 设置 | 双路风扇、机身灯光、小屏与显示、历史与告警、登录口令、连接与数据卷、关于 |
| `50-r1.js` 数据 | 读 `/api/r1`，写 `/api/r1/*`，五位配对码入口 |
| `12-style-r1.css` | 仅新增 `.r1-*` 规则，颜色/圆角/字体全部复用上游变量 |

设置页在原框架里重建（`SET_SCHEMA`），因为 fnOS 专有的服务端口、天气、通知、iDRAC、
巡检、自更新等分组在 R1 上没有真实后端；保留并沿用原框架的分组、开关、数字控件、
保存即时生效与外观/阈值分组。

## 数据来源

面板读取的接口由 `runtime/fnm_compat.py` 提供，全部来自 Toolbox 已有的采集快照：
没有为打开页面而新跑的硬件探测，SMART 使用后台 `smartctl -n standby` 缓存，
整机功耗不估算，未知值保持 `--`。
