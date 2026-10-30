# 原厂主体、真实文件读取与新文件上传

已接入五项真实读取执行器：`list_directory`、`get_file_info`、`prepare_file_download`、`get_file_transfer`、`cancel_file_transfer`。生产代码为 `runtime/api_file_provider.py` 与 `runtime/api_file_executor.py`，内容传输复用 `api_file_transfers.py`。固定原厂所有者、精确固件和当前 `Users` 根均核验后，读取仅支持管理员 `admin` 与 `public` 子树；其他用户子树明确返回 `unsupported_path_class`。新增文件上传后端位于 `api_file_write_backend.py`，仅导出 `prepare_file_upload`、`commit_file_upload`、`get_file_operation`，并须逐次通过独立原厂索引桥；改名、复制、移动、目录创建、覆盖与回收站仍不注册执行回调。

## 判权依据与完整分支

静态对象为用户固件 `V6.3.3_Build_260907` 的 `/histor/bin/emhistor`，大小 `18425520`，SHA-256 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26`。没有运行固件程序或在 NAS 写入业务。

完整追踪的逻辑 ACL 链是 `rest_check_path_auth@0x281dc9` → 管理员名称检查 `0x281280`、路径类别构造 `rest_get_path_info@0x281689`，直到 `0x28208c` 返回。该函数没有逐文件 POSIX ACL／uid／gid／附加组查询。它按原厂当前空间分类、用户名、路径类别与访问类别判权，不能把 `100000` 当 Unix UID。

| 原厂分支 | 当前根读取许可 | 在该许可下可证明的只读子集 |
|---|---|---|
| 内部空间，原厂 `+0x11a8 == 0` | 读类别 `eAuth=1` 且管理员名成立时直接允许 | 管理员／`public`，实现主动收窄，不扩到其他用户 |
| 内部空间，原厂 `+0x11a8 != 0` | `Users` 根类别 2，读类别加管理员成立 | 用户名相同或 `public` 的类别 3／4，包含子目录和普通文件 |
| 外部空间分支 | `user_id` 的原厂 `external_disk` 许可必须精确返回 1 | 相同根下的管理员／`public`，仍主动收窄 |

实现没有猜测空间标记。每次访问先用固定所有者既有会话执行原厂 `get_info(Users根)`：分派器 `0x158436–0x15845d` 在没有 `scraper` 绕过参数时以 `eAuth=1` 调用同一 ACL。这个实际成功结果覆盖上表各分支各自的必要许可；然后在同一真实根的已打开对象拓扑上重新执行上述确定的管理员／`public` 正子集。它不是拿原厂任意管理员会话当全盘授权，也不把 O_PATH 目录存在或布尔 `acl_verified` 当权限证明。

`rest_get_info@0x13ba38` 的成功 JSON **不必有 `code:0`**：`0x13c467` 起构造原请求 `path`、`isdir`、`size`、`ctime`、`mtime` 等；失败走 `0x183964`。这里严格要求回显路径精确等于目标 `Users`、`isdir` 为整数 1、`size` 为整数 0、时间为有界非负整数；若出现 `code` 则只能为整数 0。缺字段、错误、改变路径、认证失败或不识别格式均不可用。请求不带 `size`、`append`、`temp_suffix` 等参数，避免该 handler 的临时文件／扩展属性修复分支；文件协议差异不得靠轮询重试写操作探测。

## 每次操作的生产门槛

- API Token 仍须当前有效、精确工具、`files` 域和所选根／相对路径共同允许；旧 policy v1／v2 不扩权。`readonly` 足以读取，不自动给读写键新增工具。
- 只读固定 `USER_AUTH_DB_101.tb_user_auth` 的 `user_id/user_type/user_name`，必须唯一为 `100000/0/admin`。不从请求接受账号，不推断 Unix UID；SQLite authorizer 拒绝其他表、列和视图带入凭据。原厂访问令牌只由既有会话模块在 TLS 验证后私下读取，不登录或修改认证。
- 固定 `/histor/bin/emhistor` 普通文件必须完整匹配上述大小和 SHA-256，正在拥有唯一 20443 监听的 `/proc/<pid>/exe` 必须是同一 inode／设备／大小／mtime／ctime。缓存只复用完全相同文件身份，身份变化重新散列。未知固件不降级成 root 读取。
- 每次原厂请求均核对唯一进程／启动时刻／监听 inode、固定回环地址和公开叶证书 pin；连接后与发送前再次核对授权、身份和剩余时间，响应后复验。单次请求固定 12 秒绝对期限，无任意 action／URL／参数／账号代理。
- 在发原厂根请求前已使用目录 O_PATH 句柄核验真实根没有链接／额外挂载；请求后再次核对。实际目录或文件访问从真实根逐级 `O_NOFOLLOW`，每级 `mnt_id` 与设备号必须相同，拒绝 Users 下额外挂载。
- 最终对象先用 O_PATH 检查类型。符号链接、硬链接普通文件、FIFO、socket、字符／块设备均拒绝。只有已检查的普通文件／目录才能经本进程 `/proc/self/fd/<owned-fd>` 取得同 inode 只读 fd，避免先打开设备再判断类型。不会普通路径重开或通过符号链接跟随。
- 返回元数据与每个下载块之前都重新执行当前原厂许可、当前键和根／对象拓扑；重开严格句柄链比较最终对象的 inode、设备、类型、链接数、大小与时间。期间撤权、换盘、改挂载、改名替换、内容变化或服务关闭即丢弃结果／停止发送。已经交给客户端的块无法撤回；不能宣传客户端已落盘。

原厂根读取和真实目录访问可能唤醒硬盘、更新目录访问时间或原厂日志。不是被动监控，不加入业务缓存 GET、默认开发四项或定时轮询。原厂内部路径类别与文件写入、索引、配额、回收站是不同合同；读取许可本身不赋予写入权限，新增文件须另过下述门槛。

## 浏览与下载

目录最多扫描 4096 项，一页最多 200 项，JSON 上限 128 KiB。`Users` 根只检查两个固定子目录，不枚举其他用户名。只投影当前授权且支持的对象，`visibility=authorized_supported_only`、`consistency=best_effort`，不返回未过滤总数。分页游标只存在内存，绑定真实 peer、键、修订、代际、根、路径、页大小和目录版本；改名或更新后需重新浏览。

下载准备只返回绑定该普通文件的随机传输 ID 与 Toolbox 相对内容地址，原厂令牌不离开宿主：

```text
GET|HEAD /api/integration/files/transfers/<transfer_id>/content
Authorization: Bearer <Toolbox API Token>
Range: bytes=<start>-<end>   # 可选，单范围
```

内容请求使用独立 API Bearer 与真实 peer，不能用网页会话或开发白名单替代；拒绝额外查询参数、重复 Range、条件请求和请求正文。HEAD 不开始／延长 GET 窗口；支持完整、开放和后缀单范围，416 带真实长度。每块 64 KiB 前后重验授权与对象。传输状态和取消只允许本键／代际／peer，服务重启全部失效。既有窗口、名额与发送语义见 [file-backend-context.md](file-backend-context.md)。

## 一次完整固定检测

显式统一开发检查选择 `file_protocol`，主线调用：

```python
from api_file_provider import collect_file_protocol_probe
result = collect_file_protocol_probe(panel, root='/', authorizer=current_dev_window)
```

不接受外部路径／action。只从当前规划取得最多八个 Users 根，分别检查根／`admin`／`public` 的 `get_info` 与最多两项的 `get_list`，总计最多 48 次、共享 12 秒窗口。每项单独保留失败原因；总窗口结束时保留已完成的结构化项目并标记不完整，不循环重试。当前开发窗口和根规划变化时丢弃整次结果。只有精确固件且真实目录句柄可确认时才发原厂请求。结果包括 host root／drives／实际挂载／Users 四阶段的打开 errno、预期／实际 mount ID 与设备号、目录身份和 self／PID1／emhistor 挂载 namespace 摘要，以及前后拓扑是否稳定；还包括固定路径类别、HTTP／原厂状态、白名单字段的类型与包含关系。不返回目录项名称、原始路径、文件内容、会话、任意原厂消息。

`file_access_available:false` 是诊断本身不发执行授权的固定声明，不能被请求方当作后端能力结果。`file_access_readiness` 仍只检查根规划，其 `execution_backend.status=requires_per_operation_verification`；只有执行时完整通过上述所有门槛才可读。静态／fixture 的通过不冒充 NAS 实机通过。

### 首次完整实机现场与元数据短读修复

`45e75d3` 的一次完整现场确认精确固件匹配，Toolbox、PID 1 与原厂进程的挂载命名空间摘要相同，根拓扑前后稳定。七个规划候选中，四个分区的 `Users` 打开返回 `ENOENT`；另外三个能够打开物理挂载与 `Users`，两级实际 mount ID 与设备号各自相同，但读取到的预期挂载条目缺失。这个结果不能解释成原厂 ACL 拒绝：原厂查询尚未发出，原先的 `identity_unverified` 由本地根打开门槛失败统一映射而来。

已修正固定 proc 元数据读取器的一次 `os.read` 假设。`mountinfo` 是大小为零的 proc seq_file，一次读取允许在 EOF 前返回短块；读取器现在在同一 fd 内有界累积到 EOF 或上限外一个字节，然后按原规则复验身份、大小与时间，并统一 UTF-8 解码。没有将预期 mount ID 改成从待验证句柄自行推导，也没有降低设备、挂载、符号链接或 ACL 校验。诊断里的本地失败现在明确为 `root_open_unverified`，附固定 `open_error_code`，不会冒充 listener／运行 ELF 核验失败。

规划器仍只读取挂载元数据与既有身份缓存，所以一个候选不等于其 `Users` 已存在。不存在的四个候选继续按真实失败拒绝，不在被动根规划中新增目录访问或创建 `Users`。只读实机验收应选择显式诊断中完整通过真实挂载的根；修复后的 NAS 结果与字节比对仍须独立核对。监听身份元组的第一项为 PID，已增加默认运行二进制核验的契约测试，未将短读故障误修成放宽 listener 身份。

### 原厂列表的名称合同与已完成只读验收

`364b8586` 的完整诊断中，三个真实 `Users` 根已通过同挂载、设备号与前后拓扑检查，原厂 `get_info` 的结构和请求路径回显也成功。随后真实 MCP 验收已在这三个根的 `admin` 目录完成 `list_directory`，分别返回真实页面及游标；其中两个根的 `get_file_info` 成功，备份根的一次属性请求返回 `acl_unverified`，紧接的目录请求成功。现有收据不保留这次内部失败阶段，不能按耗时推断为忙碌、会话错误或目录变化，也没有据此放宽校验或循环重试。临时机器 Token 已撤销，并以 401 核对失效。以上证明目录／属性读取，不是下载内容的字节一致性或文件写入验收。

同一诊断的原厂 `get_list` 投影失败属于另一个解析问题，真实执行器浏览使用自己的安全 fd，不依赖这份原厂列表。精确固件的 `rest_get_list@0x1414af` 普通序列化回调 `0x13e0df` 在 `0x13e5ee` 写入目录项的 `name`，顶层写 `mtime/file_attr/list`，不保证 `path` 或 `code`。分页回调 `0x13f1f5` 也写 `name`；另一回调 `0x1405bb` 在 `0x140c38` 写 `name`，只有额外 flag 为 1 时才于 `0x140f5e` 写入绝对 `path`。原投影强求子项绝对路径和 `code:0`，因此把具有真实条目的成功结构误判。

现在仅在宿主内部把严格的字面量 `name` 拼接到本次固定请求目录，判断它是否为直接子项。拒绝缺失名称、重复名称、`.`／`..`、路径分隔符、反斜杠、控制／代理字符与超过 255 字节的 UTF-8 名称；可选 `path` 必须精确指向同名的直接子项。不会 URL 解码、Unicode 归一化或接受仅有 `path` 的未知格式。顶层元数据、可选 `count/totalpage` 和错误码仍严格检查，列表结构成功标为 `get_list_name_children_v260907`。规范化路径和名称都不进入开发响应；结构成功仍明确 `acl_proven:false/opened_object_proven:false`，不能当作对象存在、ACL、分页完整或真实打开证明。原厂枚举异常有吞错分支，尤其不能仅凭空列表推断目录确实为空。

固定查询参数同时改为 `page=1&pagesize=2`。旧分派器 `0x155382/0x15544e` 解析这两个参数，`0x158708–0x1587c9` 构造分页对象；`fs_get_dir_list_v4@0x5ae418` 在 `0x5af432–0x5af442` 计算 `(page-1)*pagesize`。旧 `limit/offset` 不会进入这一分页分支，因而现场实际返回了超过两项。原厂仍可能先枚举目录再截页，两项是返回页上限，不是磁盘访问或原厂扫描成本上限；总传输与绝对时间窗口继续有界。

为免把传输不可用误称为 ACL 拒绝，原厂客户端的固定传输异常现在投影为已有合同的 `native_unavailable`、`timeout` 或 `resource_busy`；本键撤销仍为 `invalid_key`。只有原厂许可／结构无法形成授权证明才保留 `acl_unverified`，所有失败仍拒绝读取且不自动重放。固定 owner 或精确固件前置证明失败也仍可能给出该码，因此这项分类改进不能追溯判定前述已结束的单次失败。

## 验证

```bash
python -B -m unittest discover -s tests -p test_api_file_provider.py -v
python -B -m unittest discover -s tests -p test_api_file_executor.py -v
python -B -m unittest discover -s tests -p test_api_file_transfers.py -v
python -B -m unittest discover -s tests -p test_api_file_readiness.py -v
```

Linux 用例使用真实临时目录、mount ID、O_PATH、只读 fd、Range 与逐块撤权；本地 fixture 注入明确的逻辑授权，不代替原厂实机许可。Windows 跳过依赖 Linux 的真实对象用例，不能把 Windows 测试通过当成 Linux 执行证明。新增文件用例还覆盖真实 `O_TMPFILE` 与 no-clobber 发布，仍须 Linux CI，以及部署后的受限实机验收；原用户文件始终只读保留。

## 新文件上传执行合同

生产最小子集只有三项，中央层仍取角色、`files` 域、精确工具及当前根／目录范围的交集；固定原厂逻辑主体 `admin` 不会把 Token 角色自动提升为管理员。最低角色为 `readwrite`，准备与提交都必须同时拥有目标和父目录的读写范围，不能用单个叶子文件授权在父目录创建对象。旧键不自动增加这些工具。

`NativeOwnerWriteAuthority` 在现有精确固件、固定所有者、原厂根读取和实时根检查之外，再执行已审计的写类别正子集：路径必须是 `admin/` 后代，`admin` 自身、`public`、其他用户、只读挂载均拒绝。`rest_check_path_auth@0x281dc9` 对同名类别 4 的 `eAuth=0` 正分支和 `base_check_path_if_public@0xadc6c3` 的前置拒绝分别保留。`get_info.writeable` 来自 `0x4320cb → 0xaaed98` 的盘／文件系统状态，不是逻辑用户写 ACL；代码和测试不会把它当写授权。

| 工具 | 输入与真实效果 | 成功的含义 |
|---|---|---|
| `prepare_file_upload` | 固定根 ID、`admin/...` 相对目标、`size_bytes`（0～8 GiB）、SHA-256、本键幂等标识；`overwrite` 只能为 `false`，拒绝版本覆盖和 `confirmed` 参数 | 原厂索引桥预检、同父目录／挂载证明与空间检查通过后，创建本进程独占的匿名普通文件。尚未发布任何用户可见目标 |
| `commit_file_upload` | 固定传输 ID 与本键幂等标识；再次核验当前授权、字节数、SHA-256、匿名 inode 与父目录 | 内核 no-clobber 发布同一 inode，原厂通知发送一次后返回 `pending`。文件存在或通知 ACK 均不等于操作完成 |
| `get_file_operation` | 固定操作 ID，仍须本键／代际／真实 peer、原始上传范围和当前精确工具授权 | 仅查询已通知操作的独立原厂索引结果并复验同一 fd／最终路径；精确匹配才 `completed`。未知、撤权、换对象或超时不冒充成功 |

上传内容通道固定为 `PUT /api/integration/files/transfers/<transfer_id>/content`，使用同一 Toolbox Bearer。其 framing、正文限额与拒绝规则见 [file-upload-transport.md](file-upload-transport.md)。它只写入准备阶段保留的匿名 fd；每块前后重验当前键、原厂许可、同父目录、挂载与容量。只接受声明长度和 SHA-256 完整相符的内容。准备和提交分别绑定幂等标识；重复提交返回原操作，不重新发布或重发索引通知。

匿名 staging 使用真实父目录 fd 上的 `O_TMPFILE`，权限为 `0600`，没有可见临时路径。发布使用标准库 `os.link('/proc/self/fd/<owned-fd>', name, dst_dir_fd=<verified-parent>, follow_symlinks=True)`，即 Linux 文档的 `O_TMPFILE`／`linkat` 方法；只跟随本进程仍持有的内核 fd，不跟随用户输入的链接。发布前后核对同 inode、普通文件和链接数 0→1。同名目标由内核原子拒绝，返回 `target_exists`，不覆盖、合并或自动改名。发布后再核对最终路径仍是同一对象，父目录同步失败或身份变化保留不明结果。没有 `rename`、可见临时文件或 root 普通路径写入回退。

容量检查只使用相同真实 fd 的 `fstatvfs`，要求块大小／碎片大小无歧义，并用 `f_bavail` 保留严格大于“本次剩余字节＋5 GiB＋min（总量／1000，50 MiB）”的空间。它复刻并收严原厂本机空间储备，不宣称替代云或外部服务的配额。完整普通 `rest_upload_file@0x12c64f` 在 `0x12cded` 调用的 `0x1274dc` 只检查 mount／statfs 和上述储备，不接受用户或 quota 参数。其 `0x50098c` 用户结构指针仅在后续相册与同步调用使用，写入前没有读取配额字段；完整原生 mkdir 路径同样没有这个比较。`user_quota` 确为数据库 enum 7 的 int64，`s_user_db_cmp_and_chg@0x5075b5` 的 `0x507910` 分支将其保存到用户结构 `+0x920`，默认值为 -1；字段存在不构成另一个已被证实的本机上传配额门，也不据此宣称所有服务配额无限。

`FileWriteExecutor` 的原厂桥是私有可信依赖，方法固定为：

```python
preflight(row, relative_path, *, current)
notify_new(row, relative_path, fd, *, directory=False, current)  # 私有收据
verify_new(receipt, row, relative_path, fd, *, directory=False, current)  # 精确 bool
```

`preflight` 在匿名创建及发布前运行，默认拒绝原厂数据库已有的同名行，包括 stale 索引；发布后不再要求“索引行不存在”，避免自动索引线程已插入时误报。`notify_new` 只发一次，`verify_new=False` 只保留 `pending`，不会重放通知。缺模块、未知索引协议或未证实的盘类别均在预检阶段拒绝，不以 fixture 回调、任意 SQL／action 或 `get_info` 代替真实索引核对。生产桥仅可复用经过静态审计的固定消息和私有数据库投影；原文、绝对路径和凭据不得返回 MCP。

`PendingInstall` 在内存保留同一已发布 fd，只允许独立索引核对将操作置为完成。通知丢失、失败或结果不明返回 `result_unconfirmed`；关闭服务或到期只关闭本进程持有的句柄，不删除已经发布的文件。发布后的取消返回 `transfer_busy`，不能把“取消”宣传为已回滚。进程重启会丢失内存操作，不能据此重放写入；调用方应使用原定范围进行只读核对。

当前的 Linux 测试用私有索引 fixture 验证匿名字节接收、同 inode 发布、并发同名拒绝、父目录／最终对象替换、撤权、通知不明和到期行为；这不是原厂索引／实机写入已经通过。已授权的实机方案只在用户给定测试文件的同目录创建 `r1-toolbox-accept-<nonce>.bin` 的独立 8193 字节对象，核对 SHA-256、原厂索引和读取结果。名称没有前导点，原厂不会正常索引隐藏文件；原文件与新增对象均保留，不覆盖、删除或执行原文件。由统一部署后的固定验收完成，不为小错误字段重复打包试探。

## 文件写协议专项证据（2026-10-30）

本节仍只对应上述精确固件，仅静态读取用户已有 ELF 与反汇编，没有发原厂写请求或执行厂商程序。表中的原生 HTTP writer 与 Toolbox 的匿名 fd 上传不是同一个协议；列出入口不赋予执行权限。新文件子集须通过上一节的完整生产门槛，其他类别仍未注册。

### 八类业务路径

| 类别 | 已确证入口与参数 | 已确证行为与返回边界 | 尚须闭合的具体事项 |
|---|---|---|---|
| 普通上传／提交 | `/rest/1.0/file?action=upload`，普通分支在 `0x15699d` 直接调用 `rest_upload_file@0x12c64f`；公共参数解析明确含 `path/append/offset/size/mode/type/mtime/ctime/enable_md5/temp_suffix`。`mode=2` 的 `0x1568aa → 0x1b0d6c` 是 `rest_upload_livephoto_v1` 专支，不是普通上传的通用 wrapper | 普通上传消费 HTTP 正文长度；`0x12d675–0x12d803` 校验当前临时文件大小与 `offset`、以及 `offset + dataLen == size`；命名包含 `.Temped/.temptd` 及带后缀／重名版本。写入、`fsync`、主文件和镜像副本 `rename` 分布在不同分支 | HTTP 方法和正文长度／分片完整合同仍需真实报文核对，不能猜成 multipart/form-data 或 Content-Range 上传；原厂临时名是可见路径，不符合 Toolbox 现有匿名 staging 的生产承诺；最终目标 CAS、未知结果、配额、索引和镜像部分失败尚未闭合 |
| 创建目录 | `/rest/1.0/file?action=mkdir` → `rest_mkdir@0x13ade0`，参数 `path/append`；目录创建落在 `fs_opr_dir_crt@0x5a2801` | `append=1` 且目标存在可走既有对象分支；默认冲突会查找并生成 `路径(n)`。`0x13b483` 先发送 HTTP 200 与 `path/ctime/mtime`，`0x13b756` 后才启动 `rest_mkdir_thread@0x13abc1`；线程调用 `file_db_api_insert_dir@0x557796` 并处理时间等后续事项 | Toolbox 的“同名拒绝、目标不可变”不能直接沿用原厂自动改名语义。HTTP ACK 不证明索引线程成功；创建与索引不是已经证明的一个原子事务。原厂路径输入和实际 parent fd 的同对象变更点仍未闭合 |
| 改名 | `/rest/1.0/file?action=rename`，`old_name/new_name` 为最终传入源／目标；`0x159c3c` 执行双路径检查 `0x28208d`，`0x159d94` 调用 `rest_rename@0x144663` | helper 在 `0x144bf2` 使用普通路径 `rename`，还包含用户属性、相册／双备份后续处理。不能把名字中的 `old_name/new_name` 当成只需传 basename，也不能把该旧入口参数直接套到 v1.1 | 原目标版本、无覆盖原子性、两个父目录的句柄绑定及后处理失败恢复；前置路径检查不是后续路径 `rename` 的同对象保证 |
| 复制 | `/rest/1.1/file?action=copy` → `rest_cm_v1@0x1a0414`；至少要求 `from/to/atid`。构造 `rest_build_fs_task_param@0x19f4f1`，经 `0x28208d` 做双路径权限检查 | 是原厂任务参数与后续执行链；`atid` 从请求传入并复制到任务结构，不是已经证明的服务端随机 task ID。旧 v1.0 的 `from/to` 分支与这个 handler 不构成可互换合同 | 原生任务的实际创建／终态查询、全部文件列表／单路径形态、覆盖、取消、跨盘部分成功，以及 task 与当前 Token／两个资源范围的绑定 |
| 移动 | `/rest/1.1/file?action=move` 也注册到 `0x1a0414`；`0x1a094d` 选择移动分支，另在 `0x1a0c3c` 检查同步空间限制 | `SYNC_SPACE! NO PERMISSION` 为独立拒绝条件。源与目标权限、任务模式和同步空间条件不能仅由现有只读根许可推导 | 同上，并需证明跨卷复制后删除的终态、撤权／断线后的源保留与结果可查询；不以 HTTP 接收或客户端 `atid` 宣称完成 |
| 移入回收站／删除 | `/rest/1.0/file?action=delete` 公共解析含 `path/recycle`，`recycle=1` 有独立分支；普通删除存在 `rest_delete@0x1436f5` 路径 | 删除不等于回收；不能漏传／降级 `recycle` 后把永久删除冒充 `trash_file`。不存在目标的分支仍可能清理原厂文件数据库记录，因而不能拿缺参／不存在路径做“无副作用写接口探测” | 回收站目标、保留期限、当前用户与原文件授权范围、原索引关系、同对象移动和目录递归边界；任何永久删除不属于现有 `trash_file` 合同 |
| 回收站管理 | v1.1 `file_recovery@0x3a1ce4 → recycle_file_recovery_to_srcpath@0x64c879`；`recycle_file_delete@0x3a1e60 → 0x64cd25`；`recycle_get_list@0x3a20ea → 0x64fcd2`；`recycle_clear@0x3a23e7 → 0x64d3fd`。公共解析明确含 `recyclepath` | 恢复按原厂保存的源路径工作，有独立的“源位置／冲突”错误分支；不是客户端任意指定恢复目标。列表采用 SSE／multipart 分段，并最后发送 `file_count/dir_count`，首帧不保证目录完整。清空含目录改名和后台删除线程；回收条目关联 `tb_recycle` 的 `userid/md5name/name` 等数据 | 必须把每条归档映射回原 Users 根、用户和原文件范围，逐项授权；恢复冲突、分页／终帧、异步清空和数据库变更一致性待核。不得开放当前用户全部回收站后声称仍受单个相对路径范围限制 |
| 文件／目录分享 | `/rest/1.1/file?action=create_share_link` 注册到 `0x1cb9e9`，另有 `get_share_id_file_list/get_share_id_file_stat/delete_share_link` | 文件分享属于文件范围加公开链接的独立业务，与固定相册／影视业务工具不能混用权限；分享链接创建是写入／外部可见操作，不是文件下载准备 | 具体文件源与当前根／ACL／版本绑定、有效期／密码和公开可达性、服务端生成 share ID、返回字段脱敏与撤销归属；不能把已支持的某项分享业务推导成任意文件分享已闭合 |

### 只读权限不能直接升级成写权限

旧文件分派器的上传在 `0x1564f4` 先调用 `base_check_path_if_public@0xadc6c3`，再于 `0x1565ec` 执行 `eAuth=0` 的 ACL。完整 helper 表明它提取用户名类别，匹配 `public` 时返回 0；调用方据此明确拒绝，并记录 `PUBLIC HAS BEEN UPGRADED! NO PERMISSION`。改名等分支对源／目标也存在这一前置检查。因此，即使 `public` 只读已经成立，也不能自动给它增加原厂写入；还须逐入口保留这类原厂额外限制。

普通上传的 `s_rest_check_sapce_able_by_path@0x1274dc` 在 `0x12cded` 被调用。其完整分支先从路径确定空间余量，再给本次所需长度增加保留空间；可见总容量千分之一、最大 50 MiB 的保留量，以及另一类路径约 5 GiB 的额外余量门。**这是空间余量检查，不是已经证明的每用户配额合同。** 不能只看 `statvfs` 的空闲数而忽略这些保留量；本批对同一已核验句柄使用 `fstatvfs` 并同时保留两类余量，不能据此宣称已经处理所有用户配额。

目录创建的原厂文件数据库更新线程、上传的相册同步 `album_sync_data_api_v1@0x43c4c1`、改名的双备份线程以及回收站独立数据库，说明最终写入不是一个裸 `os.rename` 就可兼容原厂。把路径改成 `/proc/<pid>/fd/...` 虽可能固定某个打开对象，却会改变原厂路径分类、空间检查与索引键；本实现不把这种做法当作已验证的安全桥。

### 下一次一次性采集与验收的明确范围

早期 d507 七根挂载／目录问题已通过完整 `file_protocol` 定位，后续三个真实根已完成上述只读浏览验收。不重复旧上下文取样，不改成只比设备号、跟随链接或忽略挂载 ID。SSD 完整文件系统的外部 `/home`／`/mnt` 别名不会成为第二个授权根；多个可选 `/drives/<name>` 完整根、缺失 canonical 根或 Users 内额外挂载仍拒绝，最终真实句柄门槛不变。

新增文件的受限验收已按上一节获得授权；以下较广的原生 HTTP／复制／移动／回收站矩阵仍是后续方案，未授权也未执行。只使用所有者选定的独立临时对象，不加入统一被动诊断，也不通过“缺参调用 writer”推测方法。未来一次隔离验收应收齐：

1. 一个固定文件完整上传与分片上传的请求方法、白名单头和参数类型；正文仅固定样本，记录长度和 SHA-256，不保存令牌／Cookie。
2. 当前临时对象、目标、父目录的真实 mount／inode 身份，原厂 ACK、后续只读属性／字节校验与文件索引可见时间；记录回包与实际完成的差别。
3. 已存在目标和过期版本的拒绝结果，以及目录同名是否自动改名；不得测试覆盖真实文件。
4. 拷贝／移动真实任务标识、状态／终态字段、重复请求关联与失败后的源／目标状态；所有对象均为本次固定样本。
5. 仅将固定样本移入原厂回收站，核对条目与原根／用户／原路径的绑定、恢复冲突和结果；不调用全盘清空或永久删除。
6. 样本的原厂索引／相册／备份状态是否一致；缺少这些证据时，继续明确拒绝对应写工具，不把 HTTP 200 或文件存在当成功。

这份清单是未来隔离验收合同，当前没有执行或授权其写入。纯只读检测也无法证明写入冲突、异步状态或原子提交；这些事实不能靠另一轮少量字段镜像补丁替代。
