# 原厂下载任务：受限查询、qB 会话与副作用审计

样本为用户提供的 `R1_X01_GML_STD_V6.3.3_Build_260907.zip`，复核日期为 2026-10-29。本文地址均为 `emhistor` ELF 链接虚拟地址，SHA-256 为 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。固件静态审计未运行厂商 ELF／脚本。下述实机证据涉及已授权的 qB 版本、传输统计与有界任务窗口，不包含用户文件内容、任务写操作或凭据读取。

## 下载数据与进程现场是两种结果

`vendor_business` 的 `runtime.available:true` 只表示能读取 `/proc`，不表示进程扫描完整，更不表示已取得任务。`vendor_runtime.require_one_listener` 还必须确认 `complete:true`、唯一监听归属及目标进程身份。过去把“可读但不完整”一律返回 `proc_unavailable`，容易误判为现场完全不可读；现在分别返回 `proc_partial` 与 `proc_unavailable`。

此前扫描所有进程 FD 时，无关进程正常关闭一个 FD 或正常退出，也会令完整性失败。现在仅忽略无关 FD 明确的 `ENOENT`，以及确认 PID 目录已经不存在的自然退出；存活进程身份变化、权限拒绝、覆盖预算不足、目标服务 FD 变化或监听变化仍使核验失败。目标 PID／启动时刻／可执行身份及 socket inode 的前后复核保持严格，不因状态文案调整而跳过预检。

## 实际接入：已有 aria2 的固定只读 RPC

`runtime/vendor_download_diagnostics.py` 的 `collect_aria2(root='/')` 查询已经运行的原厂 `aria2c-nox`，不调用原厂业务 REST。

1. 使用 `vendor_runtime.require_one_listener` 核对唯一进程、`comm`、可执行 basename、PID、启动时刻及监听 socket inode。仅允许已证的 `aria2c-nox` 拥有 IPv4 本机 `6800` 监听，缺失或不完整就不连接。
2. 固定向 `127.0.0.1:6800/jsonrpc` 发 `aria2.getVersion`，参数为空；不带 secret。
3. 再次核对相同进程与监听，随后只发固定 `system.multicall`：`aria2.tellActive`、`aria2.tellWaiting`、`aria2.tellStopped`。每个方法仅请求 `gid`、`status`、`totalLength`、`completedLength`、`downloadSpeed`、`uploadSpeed` 六个字段；等待／停止队列只取 `offset=0, num=128`。
4. 最后再次核对 PID／启动时刻／监听 inode。任一步发生变化，整份任务结果作废。

传输只用固定 IPv4 socket，不读取代理环境、不解析用户 URL、不跟随重定向。头部上限 8 KiB；版本响应上限 4 KiB；任务响应上限 128 KiB；从首次请求开始使用 2 秒绝对时窗，逐次 socket 操作都核对剩余时间。中间身份复核也占用该时窗，最终 `/proc` 身份复核另有其自身扫描预算。拒绝重复 HTTP／JSON 字段、压缩、chunked、缺少长度及畸形 JSON；不会把原厂原始错误、响应头、文件路径或令牌返回网页。

`tellActive` 本身没有分页参数。这里不修改原厂并发配置来限制它，而是最多接受 128 个 active 项；超量或超大响应返回 `limit_exceeded`，不静默截成“完整列表”。等待／停止计数也只代表本次最多 128 条的窗口，停止队列不是所有历史任务。因此输出始终明确 `complete: false`。

内部 provider 的成功数据固定如下；`/api/dev/business` 聚合层会进一步剔除 `gid`，不会向网页返回任务标识：

```json
{
  "engine": "aria2",
  "version": "<真实版本>",
  "tasks": [
    {
      "gid": "<16 位十六进制任务 ID>",
      "status": "active",
      "total_bytes": 100,
      "completed_bytes": 25,
      "progress_percent": 25.0,
      "download_bytes_per_second": 12,
      "upload_bytes_per_second": 3
    }
  ],
  "counts": {"active": 1, "waiting": 0, "stopped": 0},
  "limits": {"active": 128, "waiting": 128, "stopped": 128},
  "complete": false
}
```

示例数值仅解释 schema，不代表 NAS 当前任务。真实空队列只在监听归属、版本和任务 RPC 全部成功时返回。零字节保留为真实 `0`，总长度为 `0` 时进度保持 `null`；缺失、负数、溢出或矛盾的数值不以 `0` 代替。`gid` 仅为受限十六进制任务 ID；下载地址、任务文件名、`files`、`dir`、磁力链接、账户及 token 均不投影。

需要授权时返回 `available: false, status: auth_required, data: null`；不会读取配置凑 secret、登录、续令牌或再次试密码。服务不可用、归属不明、畸形响应、RPC 错误或身份变化也保持 `data: null`。生产调用不接受来自 HTTP 请求的 host、端口、方法、路径、token 或任务 ID。`ownership_check`／`socket_factory`／`monotonic` 仅供 Python fixture 注入。

## 已有 qB 的固定只读 WebAPI

用户已明确本机 qB WebUI 端口为 `8181`，默认需要账号密码，并已自行配置局域网放行。2026-10-29 的授权实机外部只读检查确认：下述三个 GET 从调试电脑 `192.168.31.193` 发起，不发送账号、口令或 Cookie，返回 HTTP 200，版本为 `v5.1.4`；全局连接状态为 `connected`，任务列表实际返回 3 项、状态均为 `uploading`，正文约 14 KiB。这里的免凭据结果来自用户的局域网放行配置，不是默认认证行为，也不能证明宿主 `127.0.0.1`／`::1` 同样获准。速度是采样瞬时值，不作为固定验收值。对应运行进程名为 `qbittorrent-nox`，启动参数亦明确该端口；既有监控还观察到该监听为 TCP6 `::`。新镜像的回环授权与监听归属仍需分别验收。

`collect_qbittorrent` 先严格核验固定本机 `8181` 监听归属 `qbittorrent-nox`。IPv4 监听固定连接 `127.0.0.1:8181`；TCP6 的 `::`／`::1` 监听固定连接 `[::1]:8181`，不依据全局 `bindv6only` 猜测单个 socket 能否双栈转发。跨 IPv4／IPv6 出现多个候选监听时直接拒绝。随后依次发送三个 GET：

1. `/api/v2/app/version`，确认真实版本文本。
2. `/api/v2/transfer/info`，投影当前全局下载／上传速度和有限枚举的连接状态。
3. `/api/v2/torrents/info?filter=all&sort=hash&limit=129&offset=0`，读取一个有界任务窗口。

固定同源 `Host`／`Referer`，IPv6 地址按 URL 规范加方括号；不使用代理或 Authorization，不访问偏好设置、日志、种子文件、Tracker、任务文件或任务控制路由。除下面明确授权的固定登录外，只读方法及字段依据 [qBittorrent 官方 WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)。初始身份预检后，为本次 qB 查询建立 4 秒绝对网络时窗；每次响应后及下一次请求前复核同一 PID／启动时刻／socket inode／连接地址，中间复核占用该时窗，认证与重试不重新计时，最终 `/proc` 复核另有其自身扫描预算。aria2 仍为 2 秒。HTTP 头部／正文上限沿用 aria2 限制，版本文本与登录响应上限均为 4 KiB。

请求 129 项用于发现超过 128 项的情况；超量时整份 qB 结果返回 `limit_exceeded`，不把截断窗口描述成全量。成功仍标记 `complete:false`。`counts.tasks` 只表示本次返回任务条目数，不是跨引擎总量或全部历史记录。任务只保留状态、选定下载内容的字节数、已完成字节、原生进度百分比和速率；缺测保持 `null`，未知状态不会透传为任意文本。真实零值仍为 `0`。qB 的 `size` 表示选定下载文件总量，不用包含未选文件的 `total_size` 替代。

qB 不支持请求时只选返回字段，因此原始列表可能在进程内存中含有文件名、路径、磁力链接或 hash；投影后这些字段一律丢弃，不向网页、审计或日志输出。不读取未知 profile、用户口令文件或其他应用的会话。

### 固定账号认证与内存 SID

2026-10-29，用户明确授权将此前为本机提供的账号密码作为代码默认值，并在下载查询确实需要认证时使用。默认值只位于 `runtime/vendor_download_diagnostics.py`，不是所有 qB 安装的通用默认凭据；本文、网页、审计和测试报告不复述真实值。本轮不增加凭据管理页面、Compose／环境配置或可由 HTTP 传入的账号参数。

没有缓存 SID 时先使用 qB 现有放行规则读取固定接口，获准就不登录；已有 SID 则按已核验的原身份复用。只有固定查询返回 HTTP 401／403 时，才允许在同一轮采集内向同一回环地址发送一次 `POST /api/v2/auth/login`，以表单提交固定账号密码。发送凭据前和接收响应后均重新核验原 PID、启动时刻、唯一监听 inode 和连接地址。任一步无法确认或身份改变，丢弃认证状态，不接受或继续发送 SID。

登录成功必须同时取得正文 `Ok.` 和合法 SID，HTTP 200 本身不是成功证明。上游 5.1.4 的 `loginAction` 在已有会话时也可直接返回 `Ok.`；错误凭据则可能以 HTTP 200 返回 `Fails.` 并累计失败，已封禁来源返回 403。因此没有新合法 SID 时不得声称取得了认证会话，也不通过重复登录试探。[qBittorrent 5.1.4 认证实现](https://raw.githubusercontent.com/qbittorrent/qBittorrent/release-5.1.4/src/webui/api/authcontroller.cpp)

Cookie 只提取唯一 `SID`，不透传原始 `Set-Cookie` 或其他 Cookie；SID 限 16～128 个 ASCII 字母、数字、下划线或连字符，拒绝重复 `Set-Cookie`、控制字符注入和 `Domain`。缺少 `Path` 可接受；出现时只允许 `/`，不会建立可跨地址发送的通用 Cookie jar。SID 只在当前 Toolbox 进程内存中保存，绑定 PID／启动时刻／监听 inode／回环地址和当前默认凭据。身份无法确认时立即清除 SID，但保留该实例的失败退避；稳定新身份或凭据变化才清除原绑定与退避。没有固定十分钟续期，服务端拒绝后才允许按本轮预算重新登录；成功认证后的普通任务读取错误不会主动清除 SID，避免下次采集重复登录。SID 不写入持久卷、快照、网页响应、审计或日志，进程退出即丢失。

同一轮最多提交一次登录，登录后再次被拒绝就停止；认证失败后 300 秒内不重复提交凭据，避免网页轮询反复登录。退避只限制登录 POST，仍允许无 SID 的固定 GET；用户修复放行后可以直接恢复读取，不必等待退避结束。认证仍使用固定 `Host`／`Referer`，IPv6 为 `[::1]:8181`，Cookie 只发送到已经核验的固定 qB 接口。会话状态由内部非阻塞锁串行管理，已有采集占用时返回忙碌，不排队等待。不开放任意 URL、端口、账号、Cookie、登录动作或任务操作代理。`QbAuthState` 与 `collect_qbittorrent(..., auth_state=None, credentials=None)` 的参数仅为进程内复用及隔离 fixture 服务，不属于 HTTP 请求字段。

登录会创建或更新 qB 服务会话，失败也可能影响其认证计数及日志。因此这里只声明任务查询与任务操作只读，不把整个采集流程称为完全无副作用。不会借认证启动／恢复进程、重置密码、调整认证配置、读取其他用户凭据或执行下载控制。认证失败保持 `available:false/data:null`，只返回固定原因，不透传服务端认证正文。

| qB 认证状态 | 含义 |
|---|---|
| `auth_rejected` | 登录被拒绝、没有取得有效 SID，或本轮登录后的查询再次被拒绝；进入登录退避 |
| `auth_banned` | 固定登录收到 403，来源可能已被 qB 封禁；进入登录退避，不修改封禁配置 |
| `auth_backoff` | 固定 GET 仍需认证，当前 300 秒登录退避未结束；未再次提交凭据 |
| `auth_busy` | 同一内存认证状态已有采集占用；本次不等待，不提交登录 |
| `auth_config_invalid` | 进程内部凭据配置无效；不尝试拼凑或读取其他口令 |

传输超时、大小／协议错误和身份变化仍沿用固定错误码；不把网络失败误写成空任务。aria2 的 `auth_required` 保持原意，不使用本轮 qB 认证状态或凭据。

## 两种引擎分别采集

`collect(root='/')` 分别执行 aria2 与 qB 来源，单源失败不阻断另一源。顶层 `engines` 始终保留各来源的 `engine/available/status/reason/source`；至少一源成功时，`data.engines` 额外包含各自受限 `data`，并明确总层 `complete:false`。所有来源均失败时顶层 `data:null`，状态为 `no_readable_engine`；不会返回空任务列表假装采集成功。

两引擎的任务状态与计数定义不同，且原厂可能迁移任务；实现不合并任务标识，不对两种来源求和，也不生成统一的“全部下载数量”。业务聚合层另有字段白名单，不应直接转发原始 provider JSON。

## 固件中这条旧 BT 查询链的证据

| 层 | 函数／地址 | 直接证据 |
|---|---|---|
| 原厂旧 REST 业务 handler | `/rest/1.1/plug?action=get_bt_task_list`，`0x2bd402` | 调用 `bt_get_active_list@0x767b24`；Toolbox 不经过此 REST 层。 |
| 固定底层 RPC | `bt_get_active_list@0x767b24` | 固定 `http://127.0.0.1:6800/jsonrpc`，`system.multicall` 包含三种 tell 查询，没有启动／暂停／删除方法。 |
| HTTP helper | `curl_get_return@0x765090` | 仅 curl 初始化、配置、发送、读取信息、清理及日志；未见客户端创建、登录、恢复、shell 或用户文件访问。 |
| 返回缓冲回调 | `simple_get_aria2_body@0x764f77` | `realloc`／`memcpy` 扩展内存响应；没有文件操作。新实现额外限制返回大小，不直接复用这个无界缓冲。 |
| 已证进程名 | `aria_check_exe_run@0x2e09fc` | 明确比较 `aria2c-nox`；不是凭通用 `aria2c` 字符串猜测。 |

原厂旧请求还选择了 `files`、`dir` 等字段；Toolbox 重新构造自己的六字段固定请求，未照抄完整敏感响应。

上游官方文档说明三种 tell 方法读取当前下载队列，等待／停止支持有界窗口，`system.multicall` 的每项成功结果为单元素数组；secret 缺失会由 RPC 拒绝。[aria2 官方手册](https://aria2.github.io/manual/en/html/aria2c.html#rpc-interface)

上游 `release-1.37.0` 实现从 `RequestGroupMan` 的现有队列／结果中取长度与速度，`getVersion` 返回版本与功能；HTTP 返回带明确长度。这个源码支持协议的只读判断，**不是缺失的厂商 daemon 字节与上游完全相同的证明**，因此还核验实际执行者和协议响应。[RPC 实现](https://github.com/aria2/aria2/blob/release-1.37.0/src/RpcMethodImpl.cc)、[HTTP 实现](https://github.com/aria2/aria2/blob/release-1.37.0/src/HttpServer.cc)

## 新版 dleng getter 不能直接调用

| 入口／下游 | 地址 | 已确认副作用 |
|---|---|---|
| `get_task_properties`／`get_task_files` | `0x2eaf57`／`0x2eb5b1` | 均先调用 `qb_client_get@0x2db408`。 |
| `qb_client_create` | `0x2dab96` | client 缺失时创建并启动 `qbittorrent-nox`，设置默认路径、迁移旧 BT 任务及初始化其他状态。 |
| `qb_client_ensure_running` | `0x2dceac` | GET 传输 `0x2dd143` 会调用它；失败时进入恢复与重启。 |
| `qb_ensure_valid_session`／登录 | `0x2dc17d`／`0x2db991` | 查询前可能自动登录及取得新的 SID。 |
| `qb_client_perform_recovery` | `0x2dc8a9` | 重启进程、重建 CURL、再次登录。 |
| `aria_client_create` | `0x2e111c` | 复制／迁移 session，设定目录并启动下载进程。 |
| `aria_client_ensure_running` | `0x2e29a3` | 新版 aria HTTP wrapper 的恢复检查；不能为查询调用。 |
| `sync_task_maindata` | `0x2ea62b` | 混合访问 qB／aria，重建原厂任务表并更新状态；不是纯内存快照。 |

`custom.json` 的初始化 `0x2d99bd` 从动态模块 profile 路径取文件；该文件与 `aria2.session` 不属于已经证明的 RAM 统计缓存。读取它们可能触及用户盘，并且持久配置不等于实时任务进度。因此不通过这些文件补查询，也不扫描下载目录。

qB 的确切 basename 为 `qbittorrent-nox`，证据是 `qb_check_exe_run@0x2da833`。WebUI 端口由运行配置填入 `%s --profile=%s --webui-port=%d --daemon`，不能凭通用默认值写成 `8080`。本版使用的 `8181` 来自用户明确配置与实机启动现场；进程存在仍不能代替任务协议成功。

## Thunder 也存在自动授权与写状态

已复核的 `td_get_url`、`td_query_bind`、`td_url_control`、`td_data_forward` 均先进入 `check_thunder_is_start@0x3aedb4`。它检查／更新过期 tdid，部分分支删除旧 token，读取用户盘 `/.td_virtual_id`、`/token` 并重试。`td_read_token@0x3a985e` 本身读取磁盘令牌。转发还会调用 `write_open_count@0x3a7e46` 写 `/tmp/nand_config/td_open_count`。

底层 `send_http_get_v2@0x3ad72f` 在 `0x3ad9d1` 将 `0x13ba` 交给 `htons`，确认访问 `127.0.0.1:5050`；POST helper `0x3ae21e` 同样使用该端口。但这是转发客户端证据，不能由端口认定监听者就是迅雷；本轮只找到 `xunlei-pan` 模块标识，没有确认可用于安全白名单的真实 daemon basename。Toolbox 不调用这些 REST 或本地转发路由，也不取得迅雷账户与令牌。

## 验证边界

定向测试使用注入的进程身份、内存 socket 和本机临时 HTTP fixture，覆盖 aria2 固定 POST、qB 固定 GET 与有界登录、匿名放行不登录、SID 复用与失效、登录次数与退避、各阶段身份复核、IPv6 同源头、禁止重定向／代理、大小与时间上限、字段脱敏、缺测、真零、真实空队列及多来源失败隔离。认证 fixture 使用独立假凭据，不连接 NAS，也不输出代码中的真实默认值。`vendor_runtime` 的 `/proc` fixture 额外覆盖无关 FD 关闭／进程退出不会误拒绝、权限错误仍失败关闭、qB 端口与进程必须精确匹配。

实机 qB 外部免凭据三项查询已核对；这只是此前局域网放行结果，不代表本轮默认凭据与 SID 分支已经通过 NAS 验收。新 provider 部署前仍不能宣称其宿主回环、唯一监听 gate、认证与任务列表投影已经在 NAS 通过。部署后须分别核对两个来源的状态、唯一监听归属、实际认证分支和任务协议结果。迅雷仍只是运行现场，未接入任务读取。
