# 原厂主体与真实文件只读后端

本批接入五项真实执行器：`list_directory`、`get_file_info`、`prepare_file_download`、`get_file_transfer`、`cancel_file_transfer`。生产代码为 `runtime/api_file_provider.py` 与 `runtime/api_file_executor.py`，内容传输复用 `api_file_transfers.py`。这不是全用户文件系统管理：固定原厂所有者、精确固件和当前 `Users` 根均核验后，仅支持管理员 `admin` 与 `public` 子树；其他用户子树明确返回 `unsupported_path_class`，上传、改名、复制、移动与回收站仍未接入。

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

原厂根读取和真实目录访问可能唤醒硬盘、更新目录访问时间或原厂日志。不是被动监控，不加入业务缓存 GET、默认开发四项或定时轮询。原厂内部路径类别与文件写入、索引、配额、回收站是不同合同；这里只闭合只读，不据此推导写授权。

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

## 验证

```bash
python -B -m unittest discover -s tests -p test_api_file_provider.py -v
python -B -m unittest discover -s tests -p test_api_file_executor.py -v
python -B -m unittest discover -s tests -p test_api_file_transfers.py -v
python -B -m unittest discover -s tests -p test_api_file_readiness.py -v
```

Linux 使用真实临时目录、mount ID、O_PATH、只读 fd、Range 与逐块撤权；本地 fixture 注入明确的逻辑授权，不代替原厂实机许可。Windows 跳过 14 项依赖 Linux 的新增真实对象用例。尚须 Linux CI，以及用户部署后在既有允许范围对目录／小文件做只读比对，不在真实业务文件上做写验收。

## 文件写协议专项证据（2026-10-30）

本节仍只对应上述精确固件。仅静态读取用户已有 ELF 与反汇编，没有再取 d507 NAS 样本，没有发原厂写请求或执行厂商程序。它解释剩余写接口的真实切入口与下一步门槛，不新增工具、不赋予写权限。

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

普通上传的 `s_rest_check_sapce_able_by_path@0x1274dc` 在 `0x12cded` 被调用。其完整分支先从路径确定空间余量，再给本次所需长度增加保留空间；可见总容量千分之一、最大 50 MiB 的保留量，以及另一类路径约 5 GiB 的额外余量门。**这是空间余量检查，不是已经证明的每用户配额合同。** 不能用 `statvfs` 的空闲数替换它，也不能宣称其存在便已经处理所有用户配额。

目录创建的原厂文件数据库更新线程、上传的相册同步 `album_sync_data_api_v1@0x43c4c1`、改名的双备份线程以及回收站独立数据库，说明最终写入不是一个裸 `os.rename` 就可兼容原厂。把路径改成 `/proc/<pid>/fd/...` 虽可能固定某个打开对象，却会改变原厂路径分类、空间检查与索引键；本实现不把这种做法当作已验证的安全桥。

### 下一次一次性采集与验收的明确范围

先使用已经提供的 `file_protocol` 解决 d507 七根当前真实挂载／目录问题：四阶段打开 errno、预期／实际 mount ID 与设备号、namespace 摘要、前后稳定性和原厂根／管理员／public 响应形态。不得为了让结果通过而改成只比设备号、跟随链接或忽略挂载 ID。已有 d507 的上下文样本不需重复采集。

在真实只读根通过前，不增加自动写探测。后续写验收需要所有者明确选定的**独立临时目录**和新增微小样本，只在额外确认的固定开发入口执行；它不能加入统一被动诊断或通过“缺参调用 writer”推测方法。一次固定验收应收齐：

1. 一个固定文件完整上传与分片上传的请求方法、白名单头和参数类型；正文仅固定样本，记录长度和 SHA-256，不保存令牌／Cookie。
2. 当前临时对象、目标、父目录的真实 mount／inode 身份，原厂 ACK、后续只读属性／字节校验与文件索引可见时间；记录回包与实际完成的差别。
3. 已存在目标和过期版本的拒绝结果，以及目录同名是否自动改名；不得测试覆盖真实文件。
4. 拷贝／移动真实任务标识、状态／终态字段、重复请求关联与失败后的源／目标状态；所有对象均为本次固定样本。
5. 仅将固定样本移入原厂回收站，核对条目与原根／用户／原路径的绑定、恢复冲突和结果；不调用全盘清空或永久删除。
6. 样本的原厂索引／相册／备份状态是否一致；缺少这些证据时，继续明确拒绝对应写工具，不把 HTTP 200 或文件存在当成功。

这份清单是未来隔离验收合同，当前没有执行或授权其写入。纯只读检测也无法证明写入冲突、异步状态或原子提交；这些事实不能靠另一轮少量字段镜像补丁替代。
