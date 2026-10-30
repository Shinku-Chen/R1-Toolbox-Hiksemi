# 文件上传字节通道

本批接入 `prepare_file_upload`、`commit_file_upload`、`get_file_operation` 三个真实写入／任务工具，字节通过固定的 `PUT /api/integration/files/transfers/<transfer_id>/content` 接收。它只消费文件后端已经建立的私有上传会话，不能凭 JSON 中的权限声明或任意 URL 创建目标。实时工具目录与 [接口合同](api/file-docker-contracts.json) 分别标明执行器和传输层状态；原厂写 ACL、容量保留、同对象发布、原厂通知和独立索引读取缺少任一运行时证明，都在相应阶段拒绝操作。

生产上传仅允许逻辑所有者 `admin` 目录内的新文件，不改 `admin` 根本身，不支持 `public` 或其他用户写入。API Token 最低为“读写”，还须明确选择 `files`、具体工具及读写根／相对目录；NAS 的逻辑管理员身份不等于机器 Token 角色必须为管理员。目标和父目录均须在授权范围内，给单个叶子文件的范围不能授予其父目录创建权。覆盖、新建目录、改名、复制、移动和回收站仍未接入。

## 请求与阶段

上传按“准备 → 传输 → 提交 → 查询真实任务”分开。准备参数为根 ID、相对文件路径、完整大小、SHA-256 和本键幂等标识；可省略 `overwrite`，提供时只能为 `false`，不接收目标覆盖版本或确认参数。受信文件后端将这些参数与访问范围、密钥 ID／凭据代际／授权修订和实际连接 IP 绑定，并持有匿名暂存句柄。HTTP 只接收该 ID 的字节；接收成功为 `verified`，**不会自动安装文件**。

```http
PUT /api/integration/files/transfers/transfer_<32位十六进制>/content HTTP/1.1
Host: NAS_IP:18732
Authorization: Bearer <独立API Token>
Content-Type: application/octet-stream
Content-Length: <准备时的完整字节数>
```

不使用网页 Cookie、小屏控制令牌或开发者白名单作为机器权限，也不把它们换成 API Token。原生客户端可以省略 Origin；提供 Origin 时仍须与真实 Host 同源。二进制不经过 MCP JSON，SHA-256 使用受信准备记录，不能在 PUT 中替换预期值。

- 只接受整个文件一次传输，不接受任意分块偏移、`Range`、`Content-Range`、`Upload-Offset`、条件请求头、内容编码、chunked 或 Expect。
- Content-Length 必须规范且与准备记录相等，单文件不超过 8 GiB；空文件需要显式 `Content-Length: 0` 和空内容的 SHA-256。
- 每次读取至多 64 KiB；前后重新检查当前密钥、原始写授权、来源、传输寿命与同一暂存对象的原厂权限，不把整文件缓存在内存。
- 传输绝对寿命保持准备记录中的原截止时间；请求体最多另受 1800 秒绝对窗口和每次至多 2 秒无数据等待约束，不因重试续期。
- 短正文、已收到的超长正文、校验失败、撤权或取消使本次暂存失败，不发布目标。不接受复用已验证的会话重新上传。
- Content-Length 是 HTTP 消息边界。到达边界时拒绝当前已缓冲的额外字节，并始终关闭连接；之后才到达的追加字节直接丢弃，不等待客户端关闭写端，也不宣称能无限等待并识别未来字节。

显式提交必须再次恢复原始工具、根范围、代际和真实对象的权限，并使用本键幂等标识。匿名 O_TMPFILE 通过同一受核验父目录的内核无覆盖链接发布；同名并发目标返回真实 `target_exists`，保留已有文件。目标发布和原厂索引分阶段完成：固定创建／关闭通知不等于索引完成，`get_file_operation` 显式读取原厂索引并核对同对象后才可报告 completed。此前如实保持 pending／running／result_unconfirmed，不把 ACK 记成完成、盲目回滚或重放。查询该任务可能触盘，因此成功响应固定带 `may_wake_disks:true`。

原厂通知只面向已核验内置 SATA，并拒绝路径中的字面量 `|` 与隐藏片段，避免原厂分隔协议或索引过滤造成歧义。独立索引读取固定管理员的既有 `FILE_DB_V102/tb_file_db_all`；索引只提供路径、类型、大小和秒级 stat 时间证据，同对象证明仍来自保留的真实文件句柄，不能把索引记录当 inode 或哈希证明。

SQLite 采用 `mode=ro` 与 `readonly_shm=1` 读取既有 WAL／SHM；必要状态缺失即拒绝，不创建侧文件、不恢复日志、不复制主库后加 `immutable=1`。2026-10-30 核对 [SQLite 官方 Unix VFS 源码](https://raw.githubusercontent.com/sqlite/sqlite/master/src/os_unix.c) 中 `unixOpenSharedMemory` 的既有 SHM 只读分支及 `robustFchown`：即使只读打开，root 仍可能对既有 SHM 执行与数据库相同 UID／GID 的 fchown；因此不承诺零文件系统元数据活动。

## 状态与验证

身份失效为 401、文件权限不足为 403、非本键／代际／来源会话为 404、过期为 410、校验不一致为 422、限额为 413、固定窗口超时为 408。日志与审计只保留既有 `integration/file.content` 操作和结果，不记录文件内容、上传参数、API Token 或原厂凭据。

`tests/test_file_upload_http.py` 使用真实 HTTP 检查来源、独立 Bearer、重复头、方法、长度、偏移和编码；Linux 专项使用真实匿名 O_RDWR 文件句柄检查逐块接收、SHA-256、显式提交、幂等、撤权／ACL／取消、轮换／到期／服务重开。测试中的内部 UploadTarget 只证明传输协议，不冒充 NAS 原厂写权限、配额或索引验收。已有只读下载与 HEAD／Range 测试继续保留。
