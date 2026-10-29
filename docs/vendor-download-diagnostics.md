# 原厂下载任务：只读诊断入口与副作用审计

样本为用户提供的 `R1_X01_GML_STD_V6.3.3_Build_260907.zip`，复核日期为 2026-10-29。本文地址均为 `emhistor` ELF 链接虚拟地址，SHA-256 为 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。未运行厂商 ELF／脚本，未访问 NAS、查询用户文件、启动下载或读取令牌。

## 实际接入：已有 aria2 的固定只读 RPC

`runtime/vendor_download_diagnostics.py` 的 `collect(root='/')` 查询已经运行的原厂 `aria2c-nox`，不调用原厂业务 REST。

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

qB 的确切 basename 为 `qbittorrent-nox`，证据是 `qb_check_exe_run@0x2da833`。WebUI 端口由运行配置填入 `%s --profile=%s --webui-port=%d --daemon`，没有充分证据把它固定写成 `8080`。运行现场可以观察进程，但不能把进程存在当成取得了 qB 任务列表。

## Thunder 也存在自动授权与写状态

已复核的 `td_get_url`、`td_query_bind`、`td_url_control`、`td_data_forward` 均先进入 `check_thunder_is_start@0x3aedb4`。它检查／更新过期 tdid，部分分支删除旧 token，读取用户盘 `/.td_virtual_id`、`/token` 并重试。`td_read_token@0x3a985e` 本身读取磁盘令牌。转发还会调用 `write_open_count@0x3a7e46` 写 `/tmp/nand_config/td_open_count`。

底层 `send_http_get_v2@0x3ad72f` 在 `0x3ad9d1` 将 `0x13ba` 交给 `htons`，确认访问 `127.0.0.1:5050`；POST helper `0x3ae21e` 同样使用该端口。但这是转发客户端证据，不能由端口认定监听者就是迅雷；本轮只找到 `xunlei-pan` 模块标识，没有确认可用于安全白名单的真实 daemon basename。Toolbox 不调用这些 REST 或本地转发路由，也不取得迅雷账户与令牌。

## 验证边界

定向测试使用注入的进程身份、内存 socket 和本机临时 HTTP fixture，覆盖固定方法、三次身份复核、授权失败、禁止重定向／代理、大小与时间上限、字段脱敏、缺测、真零和真实空队列。`vendor_runtime` 的 `/proc` fixture 与聚合 HTTP 鉴权由对应模块测试覆盖。

本轮未连接用户 NAS。部署后只有已核验的 `aria2c-nox` 正在监听且无需额外授权时，才可能获得任务快照；qB／迅雷观察仍只是运行现场，不能宣称已获取其任务列表。
