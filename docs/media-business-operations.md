# 相册、影视与分享业务执行器

这批能力由 `runtime/vendor_media_operations.py` 提供执行器，中央 MCP 目录仍须逐工具注册、授权。旧业务缓存工具保持原语义；这些新操作只由明确调用触发，不加入 `GET /api/business`、被动检测或后台媒体刷新。

## 已实现的固定操作

| 工具 | 功能域 | 原厂固定动作 | 参数与范围 |
| --- | --- | --- | --- |
| `list_albums` | `albums` | `/rest/1.1/album`，`get_all_album_list` | 无业务参数；仅返回相册 ID、名称、类型与已知计数 |
| `list_shared_albums` | `albums` | `/rest/1.1/album`，`get_share_album_list` | 无业务参数；不返回成员、生日、性别、封面地址或底层文件路径 |
| `create_album` | `albums` | `/rest/1.1/album`，`create_custom_album` | `name`；映射为 `album_name` |
| `rename_album` | `albums` | `/rest/1.1/album`，`rename_custom_album` | `album_id`、`name`；映射为 `album_id`、`album_new_name`，只允许数值 ID 大于 7 |
| `list_movie_libraries` | `movie` | `/rest/1.1/scraper`，`lib_list` | 无业务参数，固定空 JSON；仅投影名称与原厂 `lid`，缺少 `lid` 时保持未知，不猜测 `id` 字段 |
| `rename_movie_library` | `movie` | `/rest/1.1/scraper`，`lib_modify` | `library_id`、`name`；只提交 `lid`、`name`，不修改来源、成员或字幕设置 |
| `revoke_share` | `sharing` | `/rest/1.1/file`，`delete_share_link` | `share_id`；固定单元素 `share_id_list`，不批量扩大范围 |

七个原厂显式工具都需要读写或管理员角色；三个列表可能触发原厂资源初始化、共享资源修复或数据库内部状态更新，沿用显式媒体查询的权限要求，不标为只读或幂等。只读 Token 仍可选择原有脱敏业务缓存工具，不获得这些原厂实时查询。

四个写工具均必须提供 `confirmed: true` 和 `idempotency_key`。幂等键限制为 1～128 个 `[A-Za-z0-9._:-]` 字符。所有者网页会话、开发窗口和 API Token 不能互相替代；调用者仍须满足工具、功能域、角色及当前授权版本。管理员不能绕过这些条件。

原厂部分写动作使用 GET，这不改变 Toolbox 对其写权限与确认的要求。创建、重命名相册成功还必须获得整数 `code=0`，且响应中的相册 ID、类型与名称符合预期；仅 HTTP 200 不算成功。影视重命名与撤销分享的 `code=0` 只表示原厂确认接收，返回 `verification=factory_acknowledgement_only`，不冒充独立复查。

## 原厂契约证据

依据用户提供的 `R1_X01_GML_STD_V6.3.3_Build_260907.zip` 内 `/histor/bin/emhistor`，SHA-256：`402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。下列地址为 ELF 链接时虚拟地址，仅用于审计，不作为运行期 ABI：

- 相册分派器 `0x164236`：`0x1644bd` 将 `album_name` 读入结构 `+0x28`；`0x16459d` 将 `album_new_name` 读入 `+0x70`；`0x1646f9` 将 `album_id` 读入 `+0x18`。
- 创建相册处理器 `0x1f9172`：使用当前用户资源及 `+0x28` 名称；响应写入 `album_id`、`name`、固定 `type=custom`、`count`、`code`。
- 相册重命名处理器 `0x200a3d`：`0x200c82`～`0x200cd4` 将 ID 转成整数并拒绝小于等于 7 的保留相册；`0x200ce3`～`0x200d1c` 将 ID 与解码后的新名称传给 `0x4ce19c`。
- 相册列表 `0x1fa238` 无必须业务参数，调用 `0x4d3bfb` 的固定模式 0，再由 `0x1f87a6`／`0x1f8074` 生成 `all_album_list`。每项字段由 `0x1f5a08` 生成。列表可能修复原厂数据库或处理封面，不能宣传为完全无副作用。
- 共享相册列表 `0x20480b`，序列化器 `0x20404e` 明确输出 `share_album_list`、`album_id`、`name`、`type`、`creater`、`count` 等；本执行器只投影白名单元数据。
- 影视列表 `0x28e0fd` 固定转发 `/scraper?method=library&operate=query`，从下游 `result` 生成响应 `list`。原厂通过 `0x2b4f02` 按创建时间排序，不将来源路径转成新的受限文件能力。
- 影视修改 `0x28b648` 从 JSON 取 `lid`，转发固定 `library&operate=modify`。`0x28b98b`～`0x28ba63` 依次按需转换 `lid`、`name`、`language`、`auto_subtitle`、`tv_available`、`del`、`add`。辅助函数 `0x2ab9d2` 对不存在的字段保留原 URI，因此本执行器只发送 `lid` 与 `name`。原厂未对内部 URL 参数统一编码，名称额外拒绝 `&`、`#`、`?`、`%`、`=` 等分隔符，防止改变其他设置。
- 分享撤销 `0x1cc91b` 读取 JSON `share_id_list`。`0x1ccc64`～`0x1ccc79` 将当前已认证用户资源中的 UID 与分享 ID 一起传入 `0x608aab`。该辅助函数在 `0x608b77`～`0x608b8f` 构造 `DELETE FROM "tb_share_link" WHERE "userid" = %d AND "sid" = "%s";`。因此删除被原厂用户归属条件限制；Toolbox 额外限制单对象 ID、禁止 `%`、引号及路径字符，不能只凭“格式合法”声称跨用户授权。

## 传输与错误边界

`VendorBusinessClient.request(operation_id, parameters, body, authorizer, confirmed=...)` 是内部固定白名单调用器，不接受主机、端口、URL、原厂 action、账号、Cookie 或访问令牌参数。`authorizer(operation_id, canonical_parameters, canonical_body)` 必须明确返回 `True`；读写均检查，发送前再检查一次，异常或真值替代均拒绝。

传输固定宿主回环 `20443`，独立 `require_emhistor_listener` 验证唯一监听 inode、进程 PID／启动时刻和精确 `/histor/bin/emhistor` 路径。实际监听 IPv4 与 IPv6 分别选择对应回环，不猜测双栈。连接后将叶证书与固定 `/tmp/Cert.pem` 核对；签名发送前再次核对监听身份和证书，完成最后一次进程扫描后还须重新核验当前授权，防止扫描期间撤权后继续发送。使用已有固定管理员会话按现有 `vendor_media_auth` 规则签名，不登录、不重置令牌，不读取私钥。响应后身份变化即丢弃结果。

锁等待、身份采样、TLS、认证、发送和读取共用 10 秒绝对窗口。请求最长 16 KiB；复用已有媒体解析器的 32 KiB 头部、64 KiB 正文和 JSON 深度上限。只读取首个 JSON 帧，不跟随重定向、不压缩、不自动重试。原厂错误消息、响应原文、认证信息、封面地址与文件路径不对外返回。

## 幂等与不确定结果

主线授权生成内部可信 `principal` 字典：`key_id`、`grant_revision`、`credential_generation`；它不是工具参数，也不含实际凭据。写操作按此绑定、幂等键和规范化请求摘要记录。访问旧结果前仍须重新授权；相同键不同请求拒绝，并发重复返回 `busy`。

原厂已确认的结果与已发送但无法确认的结果均保留于进程内存；不记录请求参数日志、不落盘。若发送可能已发生，返回 `reason=result_unconfirmed`、`mutation_outcome=unknown`，后续相同请求只读取原记录，不能盲重放。可证明尚未发送的失败不占用记录。存储达到 1024 条后拒绝新写，不通过淘汰未知结果放开重复创建。

此保证以当前常驻进程生命周期为界；重启会丢失内存记录。重启后不能凭原幂等键认定先前请求未发生，应先用列表核对。原厂非零 `code` 也不会触发自动重试。

## 明确待接入与固定诊断

分享创建还需原厂服务端提供的 `share_id` 与真实外链返回契约；不能本地生成任意 ID 或用回环 URL 拼接分享地址。影视库创建和来源变更的 `sources`／`members` 结构，以及相册文件列表的分页与受限文件根映射尚未完整核实，当前不提供假执行器。

`collect_media_operations_probe` 可供主线接入固定开发诊断项目；只执行相册、共享相册、影视库三个已审计列表，各一次，逐项隔离失败，聚合安全结果与待接入原因。必须显式选择，保持开发者实际来源白名单及绝对窗口。标记 `native_query_may_update_state` 和 `may_wake_disks`，不自动加入被动轮询、不创建测试相册或分享。

本批已完成本地固定传输与业务响应 fixture 验证；未对 NAS 执行业务写入。上线后应先显式运行三列表诊断，再由用户指定一个明确对象进行写入验收。
