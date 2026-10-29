# 开发者白名单检测

`POST /api/dev/diagnostics` 是开发用的固定只读检测入口。开启开发模式、真实连接来源命中局域网 IP／CIDR 白名单且处于有效窗口时，可以直接查看检测目录，或一次采集设备、存储、业务和风扇现场；无需登录口令会话或 `R1_DEV_ALLOW_COMMANDS`。

这个授权只适用于本接口。原有 `/api/dev/storage`、`/api/dev/business`、`/api/dev/fan` 和 `/api/dev/exec` 保持各自的授权规则。风扇接管重试、配置修改、应用控制和电源操作不属于本接口的检测项目。

媒体签名验证另用明确发起的 `POST /api/dev/media`，请求 `{"action":"probe"}` 验证相册识别，或 `{"action":"probe_all"}` 逐项验证七类媒体状态；它会访问原厂媒体数据库并可能写完成状态，不会随本页的被动快照自动执行。见[媒体验证](media-probe.md)。

APP 空间管理验证另用 `POST /api/dev/storage/probe`，严格请求 `{"action":"probe","confirm":true}`。它复用宿主已有原厂认证，通过回环 `emhistor:80` 取得 `disk_sn`；原厂查询可能改变 SMART／设备链接并唤盘，不属于本页被动项目。见[空间管理显式验证](storage-probe.md)。

## 开启与调用

推荐在网页“设置 → 服务与开发”打开开发设置，填写非空来源白名单和 1～1440 分钟有效时长，默认 60 分钟。开发模式默认关闭，保存时把绝对 `expires_at` 写入原数据卷的 `web/service-settings.json`，重启不自动续期；到期后需要重新保存一个窗口。“窗口有效”仅表示时间有效，每个诊断请求仍要校验来源，不代表打开网页的设备自动获得开发权限。

Web 管理接口是 `GET /api/services`、`POST /api/services/developer`，写入固定结构为：

```json
{"enabled":true,"allowed_ips":["192.168.31.0/24"],"minutes":60,"allow_commands":false}
```

这些设置使用网页所有者授权：默认首次无口令可管理，设置口令后需要会话；显式配对模式沿用原流程。同源／JSON／CSRF 和操作锁继续生效，Bearer Token 或开发白名单均不能管理自己的授权。`allow_commands:false` 已足够使用本页统一只读检测；开启它也不提供任意 Shell 或原厂 action 代理。风扇重试等原有操作仍需其独立授权、明确确认和全部硬件预检，不由本页只读检测代为执行。

来源支持精确 IPv4／IPv6、CIDR 与 IPv4 点分掩码，最多 32 项。规则规范化，网段必须完整落在私网／回环范围；例如 `192.168.31.0/24`、`fd00::/64`、`192.168.31.0/255.255.255.0`。不能用域名、通配符、空白名单或 `0.0.0.0/0` 开放开发模式。只看 TCP 实际 peer，转发头不替代来源。关闭总开关、未命中或窗口过期返回 `403`；存储异常关闭授权。

兼容 Compose 初始化方式：在原项目保留已有数据卷，添加：

```yaml
environment:
  R1_DEV_NO_PAIRING: "1"
  R1_DEV_ALLOW_IPS: "192.168.31.193"
  R1_DEV_MINUTES: "60"
```

环境白名单也接受逗号分隔的局域网 IP／CIDR。尚无持久服务设置时，环境初始化窗口从本次启动计算；这是旧部署的兼容行为，未保存时再次启动仍会重新计算。Web 保存服务设置后以数据卷为准，环境不覆盖已保存的开发开关和绝对到期时间。正常使用推荐从网页管理，不靠重复重启延续诊断授权。

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
{"action":"snapshot","checks":["storage_identity"]}
```

不指定 `checks` 时仍只采集 `device`／`storage`／`business`／`fan` 原四项，不包含完整硬盘序列号。目录额外列出 `storage_identity`，只有在 `checks` 中明确选择才采集。列表必须非空，项目必须已知且不重复；按请求顺序返回。接口不接受其他字段、URL 查询参数、命令、路径、端口、序列号覆盖、令牌、用户或任务标识。没有公开 GET 或 CORS 访问，也不会建立 Toolbox 网页登录会话；业务 qB 的固定认证边界另见[业务状态](business-diagnostics.md)。

## 检测项目

| 标识 | 实际来源与边界 |
|---|---|
| `device` | 从已有监控缓存读取设备昵称、主机名、内核与固件字段；不会刷新硬件，也不导出缓存中的其他字段 |
| `storage` | 固定原厂 `get_space_list`／`get_disk_list` 查询与已有 SMART 身份缓存；交叉核对用户名称和物理盘映射，不运行 SMART、不唤盘；与原存储诊断共用 IPC 锁 |
| `storage_identity` | 仅显式选择：复用同一固定查询、身份缓存和 IPC 锁，核验当前设备生命周期及唯一映射后返回完整序列号；默认快照与原 `storage` 仍只含序列号哈希 |
| `business` | 原厂应用已有缓存或 Toolbox 上次显式查询、身份确认的 aria2／qB 有界任务队列、上次显式媒体脱敏结果及真实服务现场；与原业务诊断共用采集锁，具体限制见[业务状态](business-diagnostics.md) |
| `fan` | `FanDiagnostics.snapshot` 的被动预检与现场；不发送温控信号、不启动时间线、不应用策略、不重试接管 |

读取到的应用缓存不等于完整实时安装列表；下载来源不可用或需要授权时数据保持未知；媒体读取上次显式采集结果和逐项时间，不自动发起原厂媒体查询。白名单授权并不改变这些数据来源的限制。

## 显式核对硬盘完整序列号

使用同一个 `POST /api/dev/diagnostics`，请求体为 `{"action":"snapshot","checks":["storage_identity"]}`。仍要求有效开发模式、实际连接来源命中非空局域网 IP／CIDR 白名单、尚未到期的绝对窗口，以及同源 JSON／CSRF 标记。登录口令会话、Bearer Token 或转发头都不能替代开发窗口与真实来源授权，也不因此获得设置管理或控制权限。

结果位于 `snapshot.checks.storage_identity.data.storage.items[]`，每盘只包含：

| 字段 | 含义 |
|---|---|
| `id`／`device` | 已有设备标识与固定设备名，例如 `sda`／`/dev/sda`；不能通过请求选择其他路径 |
| `user_name`／`space_names`／`name_source` | 按现有严格名称映射取得的自定义盘名或 RAID 空间名；无匹配时保持 `null`／空列表 |
| `serial` | 完整序列号；只有当前生命周期有效、缓存来源无冲突，并与原厂同设备唯一序列号及端口记录一致时返回 |
| `serial_status` | `ok` 为核对通过；`missing`／`invalid`／`conflict`／`identity_changed` 表示本机身份缺失、无效、矛盾或已更换；`vendor_unavailable`／`vendor_missing` 表示原厂列表未确认或缺少该盘身份 |
| `serial_sources` | 只列来源标识，例如 `smart_cache`、`smart_identity_cache`、`sysfs:0`；核对通过时追加 `hiksemi_storage_socket`，不导出原始缓存或原厂列表 |
| `matched` | 当前缓存身份与唯一原厂物理盘记录是否一致；不代表已设置自定义名称，未命名盘仍可核对序列号 |

采集先冻结已有设备生命周期，再使用固定 `get_disk_list;`／`get_space_list;`。序列号只复用相同生命周期的后台 SMART 缓存、已核验的休眠身份缓存，以及已有 sysfs／udev 身份元数据；返回前再次检查生命周期和稳定身份。序列号来源冲突、重复映射、设备拔插或身份改变均返回 `serial:null`，不会拿原厂列表中的另一个序列号补齐。至少一盘核对通过时该来源 `available:true`，否则仍保留逐盘原因，`available:false`。

接口不调用 SMART、不触发后台刷新、不打开块设备或用户文件，不唤醒休眠盘。与 `storage`／旧 `/api/dev/storage` 共用非阻塞存储锁，忙碌返回该项 `409`。不把完整序列号写入默认诊断、公开概览、业务快照或审计；原四项默认快照和 `storage` 的哈希投影不变。

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
