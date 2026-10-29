# 原厂应用、下载与媒体业务诊断

`POST /api/dev/business` 一次采集三个业务类别的受限只读结果和真实运行现场。它不代理原厂 REST，不接收命令、路径、URL、端口、令牌、用户 ID 或任务 ID。

## 请求及授权

需要 `R1_DEV_NO_PAIRING=1`、精确客户端 `R1_DEV_ALLOW_IPS`、有效的 `R1_DEV_MINUTES` 窗口及 `R1_DEV_ALLOW_COMMANDS=1`。已启用登录口令时还需要正常登录会话；开发者身份不替代口令，同源、JSON 和 CSRF 检查继续生效。接口没有公开 GET 或 CORS 放行。

唯一允许的请求体是 `{"action":"snapshot"}`：

```javascript
const response = await fetch('/api/dev/business', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({action: 'snapshot'})
});
console.log(await response.json());
```

同一时刻只接受一个业务诊断请求；已有请求运行时返回 `409`。`200/ok:true` 表示完成一次诊断请求，各业务是否取得数据由 `snapshot.categories.<类别>.available` 和 `status` 分别表达，不等于三类业务都可读取。

## 本版能实际检查什么

| 类别 | 数据来源 | 返回语义 |
|---|---|---|
| `applications` | 已有 `/tmp/nand_config/app_center.conf` | 只投影缓存应用名、版本和原厂状态代码；`status:cached`，`live:false`、`complete:false`、`running:null`，保留缓存修改时间 |
| `downloads` | 已在运行并经 `/proc` 核验的 `aria2c-nox`，固定 `127.0.0.1:6800/jsonrpc` | 先确认版本，再只读 active／waiting／stopped 队列，返回状态、字节、百分比和速度；需要原厂授权时停止，不读取或刷新令牌 |
| `media` | 真实 `/proc` 进程、监听与归属现场 | 可观察 `intelligent`、`image_ocr` 等进程和已确认端口；任务读取维持 `unverified_read_only/data:null`，不伪造媒体任务数或进度 |

应用缓存的原厂 `count` 统计范围与序列化 `list[]` 不同，不能当成已缓存条目数或当前已安装应用总数。缓存文件存在也不能证明应用当前运行。缺缓存返回 `cache_missing/data:null`，不调用原厂 getter 生成缓存。细节见 [应用来源与副作用](vendor-app-diagnostics.md)。

下载只发 `aria2.getVersion` 及固定 `system.multicall` 中的 `aria2.tellActive`、`tellWaiting`、`tellStopped`。每队列最多 128 项；`counts` 仅是这次有界窗口的条目数，`complete:false` 不表示取得全部历史任务。`tellActive` 返回超限时拒绝整个快照，不截成全量成功。没有任务只有在身份和协议查询均通过、真实返回空队列时才能表示为零；缺服务、缺授权或读取失败均保留 `data:null`。URL、文件名、文件路径、GID、账号、令牌及原始响应不会透出聚合接口。详见 [下载安全读取链](vendor-download-diagnostics.md)。

媒体任务查询仍未确认安全的一次性来源：缩略图入口是流式循环；OCR 会查询用户图片表并写完成事件；刮削入口代理到尚未审明的 `refresh_status`。它们不会为了诊断被调用。`intelligent` 的已确认端口为 `7786`，`image_ocr` 为 `7778`，`9660` 仅确认是刮削代理目的端口，不能据此推断进程名。详见 [媒体证据与边界](vendor-media-diagnostics.md)。

## 运行现场与身份核验

`runtime/vendor_runtime.py` 只读取固定 `/proc` 元数据：进程 `comm`、`stat`、`exe` 链接、FD 的 socket inode 和 TCP 监听表。不读取命令行、环境变量、可执行文件内容、用户目录或下载 session；不扫描端口、运行命令或启动服务。

输出只保留固定服务名、PID、启动时刻和监听元数据；没有确认的归属保持未知。扫描有总时间、进程数、FD 数及文件大小上限，`runtime.complete:false` 时不能把未观察到的进程当成服务已停止。

下载 RPC 发送前、版本读取后和任务读取后，均要求同一个 `aria2c-nox` 的 PID、启动时刻和监听 inode。只有固定 IPv4 回环可到的 `6800` 监听才能使用；请求始终发向 `127.0.0.1`，不使用代理环境变量、重定向或任意主机。TCP／HTTP 响应的头、正文、条目数与时间均有上限。

主要状态包括：

| 状态 | 含义 |
|---|---|
| `cached` | 成功读取已有应用缓存；完整性和当前运行态未保证 |
| `ok` | 成功读取已核验 aria2 的本次有界队列窗口 |
| `cache_missing`／`cache_invalid` | 没有可用应用缓存；不会自动创建或修复 |
| `not_listening` | 完整现场里未观察到固定下载监听 |
| `proc_unavailable`／`owner_unverified`／`ambiguous` | 现场或监听归属无法严格确认，未发送下载请求 |
| `auth_required` | 原厂 RPC 要求授权，未自动登录、读取或刷新令牌 |
| `identity_changed` | 查询期间进程或监听变更，丢弃结果 |
| `timeout`／`limit_exceeded`／`invalid_response` | 查询超时、超限或不符合已验证协议，数据保持未知 |
| `unverified_read_only` | 媒体任务读取链尚未确认安全；独立保留真实服务现场 |

原厂 REST 自身也可能在鉴权后启动用户资源；Toolbox 登录会话与原厂 token 不相互替代。本版不调用应用更新／安装、下载启动／暂停／删除／恢复、媒体扫描／OCR、自检或任意控制动作。系统和服务正常网络日志仍可能变化。

## 验收范围

自动化使用隔离配置、`/proc` fixture 与本地 HTTP fixture，不访问 NAS、真实下载文件或用户相册。运行时与业务诊断不能代替实机验证。

部署本版后，应在一次快照里核对各类别 `status/source/data` 和 `runtime`：应用是否确有缓存；aria2 是否运行、监听归属是否一致以及是否需要原厂授权；媒体服务是否实际可观察。未部署时只能说明代码和协议测试通过，不能宣称已经拿到实机应用或任务列表。
