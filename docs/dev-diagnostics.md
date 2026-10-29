# 开发者白名单检测

`POST /api/dev/diagnostics` 是开发用的固定只读检测入口。开启开发模式、配置精确客户端 IP 并处于有效窗口时，可以直接查看检测目录，或一次采集设备、存储、业务和风扇现场；无需登录口令会话或 `R1_DEV_ALLOW_COMMANDS`。

这个授权只适用于本接口。原有 `/api/dev/storage`、`/api/dev/business`、`/api/dev/fan` 和 `/api/dev/exec` 保持各自的授权规则。风扇接管重试、配置修改、应用控制和电源操作不属于本接口的检测项目。

媒体签名验证另用明确发起的 `POST /api/dev/media`，请求 `{"action":"probe"}` 验证相册识别，或 `{"action":"probe_all"}` 逐项验证七类媒体状态；它会访问原厂媒体数据库并可能写完成状态，不会随本页的被动快照自动执行。见[媒体验证](media-probe.md)。

## 开启与调用

在原 Compose 项目中保留已有数据卷，添加或保留：

```yaml
environment:
  R1_DEV_NO_PAIRING: "1"
  R1_DEV_ALLOW_IPS: "192.168.31.193"
  R1_DEV_MINUTES: "60"
```

`R1_DEV_ALLOW_IPS` 是服务端 TCP 连接实际看到的客户端 IP，支持逗号分隔的精确地址，不支持网段或通配符。转发头不能替代实际 IP。`R1_DEV_MINUTES` 从宿主服务启动起计时，默认 60 分钟，范围 1～1440 分钟；重启后按当前配置重新计时。关闭总开关、白名单不匹配或窗口过期都会返回 `403`。

调用仍需同源 `Origin`、JSON 请求体与 `X-FNM-Request: 1`。例如从调试电脑运行：

```bash
curl -sS 'http://192.168.31.5:18732/api/dev/diagnostics' \
  -H 'Origin: http://192.168.31.5:18732' \
  -H 'Content-Type: application/json' \
  -H 'X-FNM-Request: 1' \
  --data '{"action":"catalog"}'
```

只允许以下请求形式：

```json
{"action":"catalog"}
{"action":"snapshot"}
{"action":"snapshot","checks":["device","storage"]}
```

不指定 `checks` 时采集全部四项。列表必须非空，项目必须已知且不重复；按请求顺序返回。接口不接受其他字段、URL 查询参数、命令、路径、端口、令牌、用户或任务标识。没有公开 GET 或 CORS 访问，也不会建立登录会话。

## 检测项目

| 标识 | 实际来源与边界 |
|---|---|
| `device` | 从已有监控缓存读取设备昵称、主机名、内核与固件字段；不会刷新硬件，也不导出缓存中的其他字段 |
| `storage` | 固定原厂 `get_space_list`／`get_disk_list` 查询与已有 SMART 身份缓存；交叉核对用户名称和物理盘映射，不运行 SMART、不唤盘；与原存储诊断共用 IPC 锁 |
| `business` | 原厂应用已有缓存、已运行且身份确认的 aria2 有界任务队列、媒体服务进程及监听；与原业务诊断共用采集锁，具体限制见[业务诊断](business-diagnostics.md) |
| `fan` | `FanDiagnostics.snapshot` 的被动预检与现场；不发送温控信号、不启动时间线、不应用策略、不重试接管 |

读取到的应用缓存不等于完整实时安装列表；aria2 不可用或需要授权时数据保持未知；媒体任务进度仍未接入。白名单授权并不改变这些数据来源的限制。

## 返回与验收

目录和快照均返回服务 `version`、实际 `access.client_ip` 及窗口剩余秒数 `access.expires_in`，不返回完整白名单或登录材料。响应使用 `Cache-Control: no-store`。

快照中的 `snapshot.checks.<标识>` 包含：

- `http_status`：该项采集的实际结果码；忙碌为 `409`，异常或来源不可用为 `503`。
- `ok`：该项采集请求是否正常完成。
- `data`：成功时的既有诊断结构，去除该结构最外层 `ok`；失败时为 `null`。
- `error`：失败时的固定说明，不透出异常文本或私有路径。

顶层 `ok:true` 表示这次请求已处理，`all_checks_completed` 表示所有选中采集器均正常完成；两者都不代表每个硬件或原厂业务来源可用。仍须查看 `data` 内各来源的 `available`、`status` 和原因。缺测保持 `null`，不会用零填充。

同一时刻只运行一个统一快照，重叠请求返回 `409`；某个来源忙碌或失败时，其他选中项目继续采集。`started_at` 和 `finished_at` 是 UTC 时间，顺序采样不代表全机原子快照。接口不进行自动重试。

实机验收时，先查询目录和返回的 IP，再按项检查来源状态及名称映射，最后运行一次全项快照。验证结束后关闭开发模式。自动化测试使用本地 HTTP 和隔离采集器，不等于已经完成 NAS 实机验收。
