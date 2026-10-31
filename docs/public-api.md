# 公开监控与网页设置

网页入口为 `http://NAS_IP:18732`。网页直接使用 fnMonitor 的面板框架（见[网页面板](web-panel.md)），随常驻容器提供监控和简单管理。默认 `R1_WEB_AUTH_MODE=password`，首次不设口令即可访问和保存；不依赖小屏接管或 MCP 开启。

可以在网页“设置 → 登录口令”开启口令登录（PBKDF2 哈希，会话持久化 1–30 天）；开启后访问面板需先登录，读写都由该会话授权。关闭口令后恢复免密码访问。显式设置 `R1_WEB_AUTH_MODE=pair` 仅兼容旧配对部署：五位码有效 180 秒、一次使用，会话最长 8 小时且重启失效；普通部署无需设置这一模式。

公开接口只读取状态。设置口令后，修改设置、风扇／灯光规则、容器操作及关机／重启均需有效会话；所有网页写请求仍需同源、JSON 和 CSRF 标记（`X-FNM-Request: 1`，旧配对接口使用 `X-R1-CSRF`）。电源与应用生命周期操作还需界面明确确认。小屏专用控制令牌不会发送到网页。MCP／REST 另用独立 Bearer 密钥与功能权限，见 [API 管理](mcp-api.md)。

`GET /api/services` 和 `POST /api/services/screen|mcp|developer` 对应“服务与开发”设置，需要网页授权，不接受 API Bearer 密钥、查询参数或片段。小屏默认开启、MCP 默认关闭，分别保存；开发者模式必须配置局域网 IP／CIDR 白名单和时限，详见 [开发诊断](dev-diagnostics.md)。小屏切换为异步，`enabled` 是保存的偏好，`active`／`busy`／`last_error` 才表示实际接管情况。

磁盘容量只保留当前挂载／设备身份有效的内存快照；启动、任意 GET、公开状态与轮询均不临时测量，相关页面可用固定 POST 按需补齐。所有者显式 `POST /api/r1/storage/capacity/refresh` 只接受 `{}`，会读取文件系统元数据并可能唤盘；不授 Bearer、MCP 或开发白名单，不接受目标路径，详见 [容量刷新](capacity-refresh.md)。SMART 是另一份三十秒有效缓存，容量刷新不触发它。

## 第三方应用读取

```sh
curl http://NAS_IP:18732/api/v1/status
curl 'http://NAS_IP:18732/api/v1/history?range=24h'
curl http://NAS_IP:18732/api/v1/report
```

浏览器应用可直接读取公开JSON，接口返回只读CORS许可，不需要携带Cookie：

```js
const response = await fetch('http://NAS_IP:18732/api/v1/status', {
  credentials: 'omit', cache: 'no-store'
});
const state = await response.json();
console.log(state.status.cpu, state.fan_policy.channels);
```

本版服务仍监听IPv4；网络详情包含IPv6并不表示网页服务已经监听IPv6。默认使用局域网HTTP。请求的Host须与实际访问的NAS地址和端口一致；设置接口不允许跨源写入。

| 接口 | 内容 |
|---|---|
| `/api/v1` | 版本与只读接口索引 |
| `/api/v1/status` | 统一实时监控，含基本状态、设备详情、双路风扇、灯光和统计摘要 |
| `/api/v1/history?range=24h` | 有界时间序列，每条最多720个点 |
| `/api/v1/traffic` | 网卡今日／本月累计流量与实际覆盖范围 |
| `/api/v1/alerts` | 当前本地告警、历史事件及SMART健康观测 |
| `/api/v1/report` | 当前JSON健康报告 |
| `/api/v1/export.csv?range=24h` | 趋势CSV下载 |
| `/api/v1/openapi.json` | 机器可读接口说明 |

历史与CSV仅接受单个`range`参数：`10m`、`1h`、`6h`、`24h`、`7d`或`30d`，未知、重复或额外参数返回400。其他接口不接受查询参数。统计服务不可用时相应接口返回503及通用原因，不能伪装成空的正常历史。

## 统一状态

`schema_version`目前为1。新增兼容字段不会改变版本；调用方应忽略不认识的字段。

| 字段 | 含义 |
|---|---|
| `timestamp` | 本次响应UTC时间；不代表所有设备均在此刻采样 |
| `available` | 基本状态当前是否有效 |
| `status` | 原本机`/api/status`基本状态，包括CPU、内存、网速、数据盘和运行时长 |
| `insights` | CPU分核、进程、接口、磁盘、容器、虚拟机、设备库存与各自时间／能力状态 |
| `controls` | 可用控制能力及屏幕、灯光、风扇、电源当前状态；不包含面板设置与授权材料 |
| `fan_policy` | 两路独立温控运行状态、来源、请求输出和异常恢复状态 |
| `light_policy` | 灯光策略运行状态、温度来源、目标颜色／亮度和原因 |
| `analytics` | 历史、流量、告警、实测功率等统计摘要 |

没有读数使用`null`，不会用零填补未知。首次差分网速、CPU用量和功率也可能为空。CPU风扇与系统风扇名称沿用原厂逻辑通道；目标百分比不是RPM或实际PWM读回。当前原厂RPM接口不可读时保持未知。

`insights.containers.items` 提供容器 `id`、`name`、`image`、`state`、`status`、`ports`、CPU／内存读数，以及 `protected` 和 `available_actions`。操作数组只说明该次采样下可用的白名单操作，不授予写权限：运行中通常为 `stop`／`restart`，已退出或未启动为 `start`，受保护、暂停、转换中或未知状态为空数组。提交时服务会重新读取当前状态与保护标记，拒绝过期列表中的不适用操作。容器名和端口均为文本，第三方界面应按普通文本显示。

CPU／主板温度与 SMART 有各自的采样时间和有效期；请求 API 不延长旧温度寿命。公开 API 只消费至多三十秒且设备身份有效的缓存，缺失／过期为未知；公开接口不能触发普通或强制刷新。可见页面补齐及已启用的风扇硬盘需求有独立受限入口，不由公开 GET 授权。曲线空值必须画为缺口，不应连成假连续曲线。见 [SMART 按需刷新](smart-refresh.md)。

历史点为`[Unix秒, 数值或null]`，每条序列自带`id`、`label`和`unit`。流量按真实计数器差值累计，首次启动以前没有监测到的时间不补算；重置、重启及接口变化也不应产生负值或巨大跳变。CPU RAPL功率仅表示CPU封装，不作为整机插座功耗。

## 设置与即时操作

`GET /api/session`在未配对时返回401，配对后提供CSRF和配置。`POST /api/action`只接受已有白名单动作。公开API没有生成配对码、修改配置、任意命令或取得小屏令牌的入口。

网页上的风扇与灯光只有一个“保存”，会一次完成保存并应用；底层接口仍接受 `apply` 参数，便于脚本分步保存。无论哪种调用，异步应用都必须等运行状态确认，不能只根据请求成功就认为硬件已经生效。只有成功应用并启用恢复的配置才会在下次服务启动重新尝试，且仍须通过硬件身份检查。

Docker 读取和操作固定连接本机 `unix:///var/run/docker.sock`，不使用环境变量指定的远程端点或 API 版本。后台列表与统计分别最多等待 20 秒，HTTP 状态接口只返回内存快照，不等待 Docker；首次完整采样最多约 40 秒。操作前重新读取状态最多等待 20 秒，启动命令最多等待 20 秒，停止／重启命令最多等待 30 秒；两端容器操作请求等待 60 秒，覆盖复核与命令预算。

容器命令完成后仍需刷新监控列表核对。网页返回 `pending: true`／`refresh_pending: true` 表示等待新状态，不能当成已读回目标状态；命令超时返回结果未确认，不应自动重试。停止与重启保留 Docker 的优雅停止行为，不增加强制停止或删除操作。

数据卷中的持久文件与恢复边界见[部署说明](../tools/boot-once/README.md)、[独立风扇温控](fan-policy.md)及[功能差距核对](requirements-audit.md)。
