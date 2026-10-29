# 原厂媒体任务诊断的读取边界

本页静态核对海康 R1 `V6.3.3 Build_260907` 的缩略图、OCR 和媒体刮削任务入口，日期为 2026-10-29。仅分析用户提供的固件字节，未执行厂商 ELF、脚本或媒体算法，未访问 NAS。样本 `/histor/bin/emhistor` 的 SHA-256 为 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`；下文地址均为 PIE ELF 链接虚拟地址，不能直接作为运行时地址或跨版本 ABI。

## 当前可用性

尚未确认可安全采集这些任务计数的固定 `/run`／`/tmp` 缓存或独立的一次性无触盘 getter。统一业务诊断中的媒体任务数据应明确标记如下；不为此添加一个假装已采集的常量 provider：

```json
{
  "status": "unverified_read_only",
  "reason": "缩略图、OCR 与刮削尚无已确认的无触盘只读状态来源。",
  "data": null
}
```

这个结论只描述 Toolbox 已核验的任务采集能力，**不能解释为媒体服务离线、没有任务或任务数量为零**。未知的任务运行状态、计数和百分比保持未知。统一诊断可以独立读取 `/proc` 的进程身份和监听记录作为真实运行现场，但不能用进程存在、监听端口或注册表项代替任务查询成功。

统一受鉴权诊断可以显示此能力结论；不能因此自动登录原厂服务、读取账号或令牌、刷新授权、枚举媒体路径、打开媒体文件或触发索引／扫描。

## 原厂 REST 的共同副作用

媒体底层函数只读并不代表整条 REST 调用只读。`emhistor` 的 `rest_CGI_proc`（`0x174ec6`）在 token 鉴权后于 `0x175aba` 调用 `rest_check_login`（`0x173b52`）。后者在 `0x173ccf` 查询用户资源；资源缺失时记录尝试启动用户资源的日志，并在 `0x173dc3` 调用 `0x50385e` 启动用户资源。

因此不能借原厂配置中的 token 或假定回环地址豁免去直接发送这些状态动作，即便其末端只读内存。主 REST 的 listener 依赖动态配置回调，本轮也没有核验适合 Toolbox 固定调用的完整传输、授权及副作用契约。

## 可以独立观测的运行现场

| 进程／端口 | 已确认的固件证据 | 能证明的范围 |
|---|---|---|
| `/histor/bin/intelligent`，basename 为 `intelligent` | `restart_intelligent.sh` 的固定 `TARGET` | 可以按固定 `comm` 和 `exe` basename 查询 `/proc`；不读取命令行、环境或用户路径 |
| `127.0.0.1:7786` | `intelligent` 的 `intellgent_server_init`（原厂拼写，`0x17a69`）在 `0x17a6d–0x17a74` 将该字面量传给 `tsock_server_init`（`0x147639`） | 主智能服务的预期回环监听；实际是否已监听仍需读 `/proc/net/tcp*`；本轮不发送业务数据 |
| `/histor/bin/image_ocr`，basename 为 `image_ocr` | `restart_image_ocr.sh` 的固定 `TARGET` | 同样可以只核对 `/proc` 进程身份，不说明有 OCR 任务正在处理 |
| `127.0.0.1:7778` | `image_ocr` 的 `ocr_server_init`（`0x8e1e`）在 `0x8e22–0x8e29` 将该字面量传给 `tsock_server_init`（`0x2d415`） | OCR 子服务的预期回环监听；它与 7786 的主智能服务不是同一个端口 |
| `127.0.0.1:9660` | `emhistor` 的 `scraper_get_http_response_big`（`0x2adae1`）构造固定回环请求目标 | 只确认刮削代理的目的端口，尚未确认服务端程序的 basename，不能猜一个名为 `scraper` 的进程 |

以上只允许读取内核已经维护的进程与监听元信息，不主动连接端口，不代表业务响应成功。若按 socket inode 关联进程，须核对实际文件描述符所有者，不能仅凭端口号把任意进程认作原厂媒体组件。

目前没有确认媒体任务专用的 Unix socket。二进制的通用通信库虽然包含 `/var/tmp/emhistor`、`/var/tmp/ipcm` 等字符串，但客户端引用不是该媒体进程拥有监听 socket 的证据，不能因此赋予所有权或发送查询。

## 缩略图转换

| 内容 | 静态证据 |
|---|---|
| 注册路由／动作 | `/rest/1.1/file`，`get_thumb_convert_process` |
| handler | `rest_get_class_thumb_process`，`0x1a79e5` |
| 用户资源内存读取 | `class_sql_thumb_process_user`，`0x4fa2d5` |
| 拷贝位置 | `0x4fa382–0x4fa39c` 从用户资源 `+0x88a0` 拷贝 `0x30` 字节 |
| 输出形式 | 循环 JSON 分段，boundary 为 `--myboundary`，并非一次性 JSON |
| 线缆字段 | `b`、`st`、`pt`、`sc`、`cc`；构造点 `0x1a7b95–0x1a7c34` |
| 循环间隔 | `0x1a7cce–0x1a7cd3` 调用 `sleep(1)`，状态字段非零时继续 |

这个底层内存拷贝本身没有读取媒体或修改任务，但它依赖已存在的用户资源对象。不能把用户资源的创建、数据库打开、认证路径或整个流式 REST handler 都推断为无触盘；目前也没有可独立读取该内存副本的固定 socket 命令或临时文件。不得读取 `/proc/<pid>/mem` 绕过原厂身份和对象生命周期。

计数字段的完整单位、状态枚举和分母边界尚未独立核验，不能仅凭缩写计算完成百分比。`st`／`pt` 在不同状态分支使用时间差，不能把它们误当任务数量。

## OCR 状态

| 内容 | 静态证据 |
|---|---|
| 注册路由／动作 | `/rest/1.1/file`，`get_ocr_status` |
| handler | `rest_get_ocr_status`，`0x1d4802` |
| 状态链 | `0x1d4aa7` 调用 `get_ocr_schedule`（`0x4fcbf5`） |
| 相册计数 | `get_count_album`（`0x4fcdb7`）经 `0x4fceaa` 调用 `album_db_get_file_num_condition_user`（`0x4630f2`） |
| SQL 证据 | `0x4631e5` 构造 `SELECT COUNT(*) FROM %s %s;`，本调用表名为 `tb_picture`，条件排除 `.gif` |
| 下游 OCR 服务 | `0x4fcf5a` 构造 `{"act":"get_count_ocr", "user_name":"%s"}`，`0x4fd043` 向 `127.0.0.1:7786` 发送 |
| 用户字段输出 | handler 构造 `totalpic`、`processedpic` 等计数，但这是用户范围，不能拼成整机任务数 |
| 完成副作用 | `0x1d4d25` 调用 `send_event_ocr_status`（`0x4fd126`）；`0x4fd29f` 调用 `rest_put_event`，内容含 `ocr recognition OK` |

所以原厂 `get_ocr_status` 不能作为本次无副作用诊断的 getter：即使名称是 `get_`，它仍会查询相册数据库，可能造成元数据磁盘读取，完成分支还会发布事件。也不能自行读相册数据库、重发 OCR 算法任务、枚举账号或把私有文件路径传入 OCR 服务。

邻近 `0x4fdaf5` 的 `ocr_get_process_info` 字符串及 7786 请求，只能证明另有内部动作；该服务端的完整分发、报文格式及副作用尚未确认，不能以名称推断其只读，也不能直接接入。

## 媒体刮削扫描状态

| 内容 | 静态证据 |
|---|---|
| 注册路由／动作 | `/rest/1.1/scraper`，`lib_scan_status` |
| handler | `rest_scraper_lib_scan_status`，`0x28d429` |
| 下游原始 URI | `0x28d61f–0x28d64a` 构造 `/scraper?method=refresh_status` |
| URI 参数 | `0x28d70c–0x28d716` 经 `scraper_convert_url`（`0x2ab9d2`）处理原厂可选 `lid` 字段，按 `&lid=...` 追加；完整下游字段语义仍需核验，不能加入任意用户参数 |
| 请求目的地 | `scraper_get_http_response_big`（`0x2adae1`）使用 `127.0.0.1:9660`；`0x2adf46`／`0x2adf99` 构造 HTTP GET |
| 下游返回投影 | 从 `result` 读取 `status`、`scaned`、`total`、`timestamp`，可选 `num`；原厂单词确为 `scaned` |

`GET` 和 `refresh_status` 的名字不能证明下游实现不会刷新索引、访问媒体目录或打开状态数据库。当前未完成 9660 服务端的实现与副作用审计，未确认固定临时状态缓存，因此本版不连接该端口，也不调用 `lib_scan` 或其他启动扫描动作。

下游 `status` 枚举、多个库的聚合规则以及缓存年龄未确认，不能把 `scaned / total` 自动当成当前整机媒体处理进度。

## 后续接入的最小证据

1. 找到实际安装版本的服务端状态读取实现或原厂持续写入的固定内存状态缓存，证明不访问媒体文件、不启动扫描／索引、不发布任务完成事件。
2. 明确认证、用户范围及对象生命周期，不自动获取账号、登录或刷新令牌，也不将全量原厂响应透传到 Toolbox。
3. 核验运行／空闲／不可用的状态枚举、计数单位、真实零值、未知值、时间戳及缓存失效条件；没有任务与服务不可用必须能区分。
4. 固定调用目标与参数，限制超时、响应大小和查询频率；只投影处理类型、计数、百分比及运行状态，排除文件名、路径、用户名、账号、令牌及其他原始元数据。

本轮只完成离线静态证据核验，没有媒体任务 provider、实机调用或任务状态验收。统一运行现场采集的测试由其实现单独覆盖，不能把端口／进程检查通过写成业务任务查询已通过。
