# 文件读写与固定 Docker 项目接口方案

状态：policy v3、受限文件读取／上传／变更、固定项目读取／更新和独立未来部署均有执行器。2026-10-30 新增独立覆盖工具，文件／Docker 合同扩为二十一项；原始方案仅作历史，不代替实际门槛和 NAS 验收。

二十一项合同均有实际适配；新增覆盖、新建目录、复制、改名、移动和真实原厂回收。源对象、目标冲突、所有者恢复区和独立原厂索引按[文件变更与恢复](file-mutations.md)执行。本批尚未进行 NAS 写入验收。项目控制和四项未来部署仍为独立工具。

## 0．已实现的定义入口

- 机器可读定义：[file-docker-contracts.json](api/file-docker-contracts.json)，包含二十一项合同的输入／输出 Schema、最低角色、功能域、资源、错误和传输约定。
- 定义来源：`runtime/api_contracts.py`；运行 `python scripts/build-api-contracts.py` 同步 JSON，`--check` 拒绝未同步产物。测试使用开发依赖中的固定 JSON Schema 校验器；不增加 NAS 运行时依赖。
- 所有者接口：`GET /api/access/contracts`，无查询参数；遵循原有 Web `password/pair/open` 授权、同源规则和 `no-store`，Bearer（即使同时带所有者 Cookie）与单独开发窗口不能使用。MCP 关闭时仍可由所有者查阅。
- 网页“API 管理 → Token 操作 → 文件与 Docker 接口定义”按需读取、展示和复制定义；不创建 Token、不更改权限、不访问文件或查询原厂服务。

定义目录保持 status:definitions_only、execution_allowed:false，因为查定义不授执行权；二十一项均有具体适配，全量八十一工具再按 Token 过滤。policy v1／v2 冻结原十工具；存储 v6 增加 application_ids 且不扩大旧安装资源。文件与 Docker 的生产门槛和 NAS 验收分开。

## 1．能力范围与调用方式

MCP `/mcp` 和 `POST /api/integration/action` 共用工具目录、参数校验、授权和回调。JSON 接口沿用现有工具结果包装：`content`、`structuredContent`、`isError`；HTTP 200 不能代替业务 `ok:true`。

| 角色 | 可授权能力 | 资源限制 |
|---|---|---|
| `readonly` | 根元数据、受限文件浏览／属性／下载、项目状态与脱敏摘要、未来资源登记／本键任务 | 精确文件范围及生产门槛；项目或未来资源 `inspect` |
| `readwrite` | 已核验 admin 后代的上传、覆盖、目录、复制、改名、同盘移动和原厂回收 | files、精确工具、对应源／目标及父目录、当前对象与恢复／索引门槛 |
| `admin` | 继承已授能力；现有项目控制／更新、未来资源新建／更新／控制／删除 | `docker`、精确工具、具体项目／未来资源及所需有效计划 |

角色只是权限上限，不自动增加工具或资源。普通文件变更在明确范围内执行；覆盖使用独立工具、目标版本和 confirmed:true，原厂回收也必须确认。所有者初始化与恢复区管理不能由机器 Token 或开发白名单代理。

### 读取合同与当前可用范围

下表九项均已有真实回调。五项文件内容读取／传输严格限定 admin 子树，364b858 已在三个实际根完成 admin 浏览，但内容下载仍待样本；出现在目录不证明可取得内容。

| 工具名 | 参数 | 最低角色／域 | 作用 |
|---|---|---|---|
| `list_file_roots` | `{}` | `readonly`／`files` | 只列本键已授予的根及子目录，不浏览盘内目录 |
| `list_directory` | `root_id,path,limit?,cursor?` | `readonly`／`files` | 在已授权目录内分页列出可见条目 |
| `get_file_info` | `root_id,path` | `readonly`／`files` | 单个文件或目录的最小属性 |
| `prepare_file_download` | `root_id,path` | `readonly`／`files` | 核验普通文件，创建绑定当前键的短期下载句柄 |
| `get_file_transfer` | `transfer_id` | `readonly`／`files` | 查询本键下载的状态及实际已发送字节 |
| `cancel_file_transfer` | `transfer_id` | `readonly`／`files` | 取消本键的下载，关闭句柄并释放名额 |
| `list_docker_projects` | `{}` | `readonly`／`docker` | 只列本键获授的已登记项目 |
| `get_docker_project_status` | `project_id` | `readonly`／`docker` | 查询该项目可确认的引擎运行／健康状态，原厂未核验状态保持未知 |
| `get_docker_project_summary` | `project_id` | `readonly`／`docker` | 返回服务、镜像、端口等白名单摘要 |

所有参数对象拒绝额外字段；`readwrite`、`admin` 仍须满足工具与资源范围。上述九工具是读取部分，文件写工具见第 5 节，Docker 更新工具见第 7 节。取消自己的下载不需要文件写权限；上传查询与取消须继续满足上传授权。

参数类型与上限：

| 字段 | 约束 |
|---|---|
| `root_id` | `^root_[0-9a-f]{24}$` |
| `project_id` | `^project_[0-9a-f]{24}$` |
| `transfer_id` | `^transfer_[0-9a-f]{32}$` |
| `path` | UTF-8 字符串，最多 1024 个 Unicode 字符及 4096 字节；允许空串表示根，仍受子目录范围限制 |
| `limit` | 整数 1～200，默认 50；布尔值不当整数 |
| `cursor` | 可省略或为 `null`；非空时为最多 2048 个 ASCII 字符的不透明值 |

未知读数为 `null`。JSON 整数须在安全整数范围内；无法准确表示的文件大小不能伪造后继续下载。文件名和原厂文字只作为不可信数据展示，不作为 HTML、指令或可执行地址。

示例：

```json
{
  "tool": "list_directory",
  "arguments": {
    "root_id": "root_aaaaaaaaaaaaaaaaaaaaaaaa",
    "path": "admin/接口验收",
    "limit": 50
  }
}
```

实际授权的相对目录由所有者选择，示例不代表 NAS 上存在该目录。文件写入和固定项目更新属于本方案；任意 Compose 编辑、任意命令和通用原厂 action 代理不开放。创建全新 Docker 项目另按部署能力设计，不能冒用已登记项目的更新权限。

## 2．权限与旧键兼容

每次调用同时满足：有效 Token、MCP 总开关、真实来源 IP、最低角色、显式功能域、精确工具及资源范围。Cookie、小屏令牌、开发窗口不替代机器授权；管理员不绕过任何条件。`tools/list`、REST、下载通道和实际回调使用同一个判定。

### 版本与授权结构

当前持久格式 v6 兼容读取 v1～v5，权限策略仍为 policy v1／v2／v3。docker_deployments 保留原语义；新增 application_ids 新键默认空，旧安装键只保留 ID34。读取不改字节，真实写原子迁移。

实际根和项目 ID 必须由所有者选择。所有者接口仅接受已注册工具，上传三项可明确授权，五项未接文件变更拒绝授权。项目操作区分 `inspect/control/update_image`，未来资源区分 `inspect/deploy/control/remove`；Docker 写操作同时需管理员及对应工具，新文件上传最低读写并须具体工具及目标／父目录范围。新增默认不选，降级收紧，提高角色不恢复。Docker 域可只选现有项目或未来资源，不强制两者同时选择。

```json
{
  "policy_version": 3,
  "role": "readonly",
  "scopes": ["files", "docker"],
  "tool_grants": [
    "list_file_roots",
    "list_docker_projects", "get_docker_project_status", "get_docker_project_summary"
  ],
  "file_roots": [
    {
      "id": "root_aaaaaaaaaaaaaaaaaaaaaaaa",
      "path": "admin/接口验收",
      "access": "readonly"
    }
  ],
  "docker_projects": [
    {"id": "project_bbbbbbbbbbbbbbbbbbbbbbbb", "operations": ["inspect"]}
  ],
  "grant_revision": 1,
  "credential_generation": 1
}
```

原有 `id/name/digest/disabled/allowed_ips/created_at/expires_at/revoked_at/last_used_at` 保留。摘要不输出；Token 仍只在创建或轮换时显示一次。

兼容规则：

1. policy v1／v2 固定保留原十工具，再受其原角色和域限制。即使以前选过 `files/docker`，也不能自动获得本批新工具；原范围保留并显示“待所有者启用”。
2. 所有者明确勾选新增工具并提交 `tool_grants`，才升级为 policy v3。仅改名、角色、IP、有效期、启停或轮换均不顺带启用新策略。legacy 显式选择角色可保留原有转为 policy v2 的行为，仍只允许原十工具；已有 policy v3 改角色不能被降回 v2，须同步收紧不再满足等级的工具与资源组合。
3. policy v3 不自动追加未来工具。字段省略保持，显式数组整体替换；未知工具、通配符、重复值或不满足角色／域／资源依赖的组合拒绝保存。
4. 旧格式只在内存归一化，读取不改字节，真实写才原子迁移 v6。保留摘要、有效期和原权限；新工具／新应用不自动授权。
5. 权限、来源、到期或启停变化推进 `grant_revision`；轮换推进 `credential_generation`。未执行计划与下载句柄按代际失效；仅改名、审计或最近使用时间不改变授权修订。

创建／更新接口沿用 `/api/access/keys` 与 `/api/access/keys/update`，新增字段只接受所有者管理通道。旧客户端省略新字段继续保持旧策略，不能通过提交 `role` 触发新工具授权。

### 网页 API 管理

功能域下已有具体操作选择，新增操作默认不勾选。文件范围支持同根多条子目录的无损编辑；Docker 逐已登记项目选择读取、管理员启停／重启及镜像更新操作。具体界面和降级行为见[API 管理](mcp-management-ui.md)。未实现文件操作不授予，不能假装已可用。

同根多范围按明确允许范围的并集授权，源和目标分别完整匹配；精确重复拒绝。子目录只读范围不会收窄已经授予的上级读写范围，网页须提示重叠授权的实际效果。

元数据编辑与权限编辑分开跟踪，修改名称或 IP 不重写资源授权。资源失效时保留原 ID 并标原因，不自动换绑同名盘或项目。`capabilities` 区分 `not_granted`、`pending_enable`、`resource_unavailable` 与 `backend_unverified`，不以空结果伪装可用。

## 3．文件范围与安全打开

### 路径语义

- `root_id` 沿用 `root_<24hex>`，绑定物理盘、分区、文件系统、挂载及固定 `/drives/*/Users/admin/` 根；`path` 始终相对该 admin 根，不相对整盘。上限 1024 个 Unicode 字符，并另设有界 UTF-8 字节上限。
- 路径须等于已授予子目录或位于其后代，按组件比较。例如授权 `Movies/私人` 不允许访问 `""`、`Movies` 或 `Movies/私人备份`。
- 拒绝绝对路径、`.`／`..`、反斜杠、重复斜杠、控制字符和无效 UTF-8。保持名称原有大小写和 Unicode，不替用户改名或归一化。
- 不接受原厂路径、用户 ID、端口、URL、原厂 action 或 shell 参数。JSON 路径与原厂 URI 的编解码必须逐层确认，不允许二次解码形成另一条路径。
- 列表不返回宿主挂载路径、未授权根、父目录、兄弟目录或未过滤的原厂总数。Toolbox 私有状态、凭据和恢复资料不因位于数据盘而变成可下载对象。

`list_file_roots` 只复用已授予范围的规划元数据，返回根标签、原配置范围、`binding_status` 与实际后端能力；规划存在不代表已通过 ACL 或安全打开核验。

### 当前只读执行器与剩余门槛

`api_file_roots.py` 和 `file_backend_context` 仍只生成规划与上下文，不授予执行权。新的 `api_file_provider.py`／`api_file_executor.py` 在精确固件下完整追踪 `rest_check_path_auth`，只实现固定原厂管理员的 admin 逻辑正子树；原厂根读取许可、真实同挂载拓扑及最终对象句柄共同通过后才能读取。不把逻辑 ID 当 Unix UID，不回退 root 直读。完整分支、严格 `get_info` 成功格式和逐块续验见[后端证明](native-file-readonly.md)。

原厂下载 handler 的分离按路径打开不直接复用。当前执行器从可信根逐级 O_PATH／NOFOLLOW，同设备、同 mount ID 检查后，只对已经核验的普通对象以本进程持有 fd 取得同 inode 只读句柄；不跟链接、不先打开设备再判断，拒绝硬链接与额外挂载。文件权限与最终对象必须持续同时成立。

此前挂载短读已修，364b858 已确认三个实际根的 admin 浏览。file_protocol 仍只显式采集完整身份、打开和 namespace 证据；不把规划／fixture 当实机证明。外部别名不授执行权；堆叠和规范根歧义仍拒绝。子挂载存在不排除整根，逐路径真实打开继续拒绝跨入子挂载。生产读写与新的恢复区门槛见[file-mutations.md](file-mutations.md)，本批写入未在 NAS 验收。

文件操作是显式盘访问，可能唤醒硬盘；不加入网页监控、被动开发诊断、自动目录预读或 SMART 采集。

## 4．目录与属性合同（受限只读已实现）

`list_directory` 默认 `limit:50`，范围 1～200；只返回当前主体与文件范围允许、且后端已核验的条目。按固定规则排序，游标按相同顺序续页，不跨目录或跨键使用。

```json
{
  "ok": true,
  "root_id": "root_aaaaaaaaaaaaaaaaaaaaaaaa",
  "path": "admin/接口验收",
  "entries": [
    {
      "name": "示例.txt",
      "path": "admin/接口验收/示例.txt",
      "type": "file",
      "size_bytes": 1234,
      "modified_at": "2026-10-29T10:00:00Z",
      "downloadable": true
    }
  ],
  "page": {"limit": 50, "next_cursor": null, "total": null},
  "visibility": "authorized_supported_only",
  "consistency": "best_effort",
  "may_wake_disks": true
}
```

单页 JSON 上限 128 KiB，达到字节上限通过游标续页，不能丢条目后宣称完整。游标有界且不透明，绑定键、代际、授权修订、根实时身份、目录身份和排序；每页重新鉴权，游标本身不授予权限。目录变化返回 `directory_changed`，从第一页重读。

不暴露未经授权过滤的 `count/totalpage`，不能确认的总数为 `null`。过滤后为空只表示没有可返回条目，不证明物理目录为空。`get_file_info` 返回同类最小属性和可下载状态，目录大小为 `null`；不递归统计，不读内容算哈希，不返回 ACL 原文或链接目标。

## 5．文件传输与写入合同（下载与新文件上传已实现）

### 下载准备与传输

`prepare_file_download` 完成实时授权及安全对象核验后返回：

```json
{
  "ok": true,
  "transfer_id": "transfer_cccccccccccccccccccccccccccccccc",
  "content_path": "/api/integration/files/transfers/transfer_cccccccccccccccccccccccccccccccc/content",
  "name": "示例.txt",
  "size_bytes": 1234,
  "object_version": "opaque-object-version",
  "state": "prepared",
  "first_get_deadline": "2026-10-29T10:05:00Z",
  "transfer_deadline": null,
  "may_wake_disks": true
}
```

`content_path` 是固定相对地址，不是分享链接。句柄不含路径和凭据，也不代替认证。内容通道只接受：

```http
GET /api/integration/files/transfers/<transfer_id>/content
Authorization: Bearer <Toolbox Token>
Range: bytes=0-65535
```

同路径支持 `HEAD`，重新鉴权且不返回正文；`HEAD` 忽略 Range。句柄 ID 为固定前缀加随机 128 位值，URL 不接受额外查询参数、片段或 Token。无需扩大其他路由的请求行上限。MCP 不传文件字节或 Base64。

每次 GET／HEAD／Range 都重新检查当前 Token、真实 peer、MCP 总开关、精确 `prepare_file_download` 授权、资源及当前 ACL。句柄绑定键 ID、Token 代际、授权修订、peer、根与对象版本；轮换、收紧授权或重启后失效。Cookie 和开发窗口不能用于下载。

准备阶段只设置 `first_get_deadline`；首次 GET 开始时固定 `transfer_deadline`，后续状态查询返回它。HEAD 不开始、消耗或延长准备窗口。两种期限还受 Token 自身到期时间约束，不以一个含义不明的超时字段兼指准备与传输阶段。

### HTTP 契约

| 请求 | 响应 |
|---|---|
| GET 无 Range | `200`，完整字节流 |
| 单个 `bytes=start-end`、`start-`、`-suffix` | `206`，精确范围与 `Content-Range` |
| 有效但不可满足的范围 | `416`，`Content-Range: bytes */<size>` |
| 多范围、畸形或重复 Range | `400 unsupported_range` |
| `If-Match/If-Range` 等首批未实现的条件请求 | 明确拒绝，不假称可靠的条件续传 |

首批只支持普通文件，不自动打包目录。HEAD 正常返回 `200` 和完整文件长度；空文件 GET 正常 `200`、长度零，带任何不可满足的范围返回 `416`。

响应由 Toolbox 构造：附件、`application/octet-stream`、正确长度、`Accept-Ranges: bytes`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。中文名用安全编码的 `filename*`，ASCII 回退使用固定名称，拒绝 CRLF；不透传原厂 Cookie、重定向或错误正文，不压缩字节流。

inode、大小、mtime 等只能作为对象变化检测，不能伪装强内容 ETag。每次范围读取须匹配已绑定对象版本；检测到替换或内容变更则中止，不能拼接新旧对象。首批 Range 是传输能力，不承诺不可变快照；没有内容版本证据时不自动做跨版本续传。

### 资源与授权续验

- 独立流式发送器，每块最多 64 KiB；不能复用当前会整段物化 `bytes` 的 `Handler.send()`。
- 当前每键最多 2 个、全局最多 4 个非终态上传／下载会话（共用限额），同一句柄最多 1 个活动 GET；活动连接另计并保留 Web 控制请求名额。准备后五分钟内须首次开始 GET；首次开始后进入总长三十分钟的单调时钟传输窗口。HEAD、Range 重试与连接重建均不续期；上传读 socket 每次最多两秒无数据等待，并受原传输寿命和最多三十分钟请求体绝对窗口限制；不因连接重建续期。
- 每个输出块前检查当前授权修订、代际、服务开关、当前时间与 Token 的 `expires_at`，以及对象状态；自然过期不会改变修订号，不能仅依赖状态文件变化。配置丢失、损坏、时钟不可用或写失败同样关闭传输。续验可使用有一致性保证的授权修订与状态文件身份，不能只持有最初 principal。
- 明确开销与失效界限：已排入 socket 的一块无法追回，暂停／撤销后停止后续块；写 socket 也必须有短超时。
- ACL 或命名空间变化无法继续证明对象归属时停止。每次流结束、客户端断开或超时立即关闭活动 fd、释放活动连接名额；逻辑句柄及其名额按以下状态机保留或释放。续传若允许重新打开，必须再次完成相同对象及 ACL 核验。
- 首版状态：`prepared/streaming/ready/interrupted/completed/cancelled/expired/failed`。成功发送一个部分 Range 后为 `ready`，关闭本次流的 fd、释放活动连接名额，保留逻辑句柄与原对象绑定；下一次 GET 须重新完成同对象及 ACL 核验。连接中断为 `interrupted`，同样关闭活动 fd，剩余窗口内才可由客户端显式重试；不在后台重新发送。
- `completed` 仅用于一次覆盖完整文件的 GET 成功发送；它表示服务端按声明长度发送完成，不代替客户端落盘或哈希校验。多个部分 Range 的累计已发送字节可能重叠，不能据此计算整文件完成率。客户端完成分段读取后可显式取消并释放句柄。
- `completed/cancelled/expired/failed` 为终态，释放非终态句柄名额；`prepared/ready/interrupted` 在各自原窗口届满时过期。总会话记录最多 128 条，终态保留有界时间后可淘汰，重启后句柄失效。任何续接都不能放宽原对象、授权或期限。
- 已发出 HTTP 头后出现错误只关闭流，不追加 JSON 或伪造第二个状态码；终态由 `get_file_transfer` 返回，MCP 调用本身不占用长流。

HTTP Range 与 HEAD 语义依据 [RFC 9110，第 9.3.2、14 节](https://www.rfc-editor.org/rfc/rfc9110.html#section-14)；上述范围限制是本批明确的服务契约。

### 写工具与资源要求

文件变更执行协议以[文件变更与恢复](file-mutations.md)为准。旧 prepare_file_upload 只接受 overwrite:false；新增 prepare_file_overwrite 单独精确授权、目标版本与确认，commit 从可信传输记录恢复实际准备工具。

新建目录仅单层，复制普通文件源只读／目标读写，改名／移动使用受信区捕获后核对 FD 和内容；不宣称单步 source-CAS，不跨盘复制后删除，不覆盖并发目标。真实回收使用原厂归档、固定 schema 和精确事务，不能退化为永久删除。

所有者预检／初始化／撤销／检查／恢复在固定 /api/files/authority/*，机器和开发白名单不能自授权。文件变更的持久幂等与恢复记录在进程重启后保持结果未确认，不自动重放。原厂索引真实匹配后才 completed，恢复对象与 index_verified 分开报告。

本批尚未执行 NAS 写入验收。部署后，用户指定的 admin 根内相对路径 `临时共享/FWA229P.bin` 只用于属性和下载读取；上传另建同目录的 `r1-toolbox-accept-<随机值>.bin`，使用固定的 8193 字节内容及 SHA-256，核对无覆盖发布、原厂索引和下载回读，保留新文件供用户查看。临时 Token 限定该目录、所需工具与实际来源，验收后撤销；不对原文件改名、覆盖或删除，不把 fixture 或只读结果当作写入已通过。

## 6．固定 Docker 项目读取与登记

所有者登记接口沿用网页登录、同源、JSON、CSRF，拒绝 Bearer 或开发白名单代管：

| 接口 | 作用 |
|---|---|
| `GET /api/docker/projects/catalog` | 显式读取固定引擎元数据，生成有完整身份的候选 |
| `GET /api/docker/projects` | 核对已登记项目及当前绑定 |
| `POST /api/docker/projects/register` | 按候选登记已有项目，只修改 Toolbox 私有注册表 |
| `POST /api/docker/projects/update` | 修改 Toolbox 标签，不修改业务项目 |
| `POST /api/docker/projects/revoke` | 撤销登记，不停止或删除原项目 |

读取器使用固定本机 Docker Unix GET，核验 dockerd peer、引擎、Compose 标签及配置／服务身份，不使用 shell 或原厂 `project_list`。配置仅核验元数据，不读正文；来源不完整、身份变化或同名重建不自动换绑。发现配置所在文件系统的元数据可能唤盘，仅在显式调用时访问。项目状态的 `native_status/pop_status` 仍为 `null`，不冒充原厂 App 状态。

列表与摘要只返回当前键获授项目的白名单数据，不输出环境、命令、任意标签、Compose 原文或真实路径。读取要求具体工具、`docker` 域及项目 `inspect`，不借管理员等级扩大范围。`GET /api/access` 只读注册表元数据，不自动连接引擎。完整读取约束见[固定项目读取与授权](docker-projects.md)。

## 7．管理员项目控制与受控更新

以下工具已有执行器；自动化验证不表示 NAS 新版本已验收。当前实现以[固定项目操作执行器](docker-project-operations.md)为准，不采用原始方案中的原厂脚本／配置写回路径。

| 工具 | 最低角色／项目操作 | 当前作用 |
|---|---|---|
| `control_docker_project` | `admin`＋`docker`＋具体工具＋`control` | 仅启停／重启已登记项目，须确认及幂等键 |
| `plan_docker_project_update` | `admin`＋`docker`＋具体工具＋`update_image` | 按已有项目、服务与受限镜像引用规划更新 |
| `execute_docker_project_update` | 同上；须仍有规划工具权限 | 只执行有效的本键计划，须确认及幂等键 |
| `get_docker_project_task` | `readonly`＋`docker`＋具体工具＋`inspect` | 查询本键、同凭据代际和授权修订的脱敏任务 |

`control_docker_project` 和未来部署四工具不计入本页二十一合同；三个更新／任务合同计入。未来资源使用独立 deployment_*，所有者固定登记后管理员规划执行，见[受控部署](docker-deployments.md)。

更新只接受已核验的固定项目和受限镜像变更，不开放任意 Compose、命令、部署目录或原厂 action。执行前后的服务身份、计划、授权和资源条件继续核验；原厂或 Engine 接受请求不等于异步结果完成。异步任务、配置并发冲突保留、挂载限制、网络与健康有界核验统一由执行器文档说明；不能证明的配置或状态保持不可用，不自动重放结果未知的写请求。

文件范围无法把 Docker 新镜像变成 Users 沙箱，项目更新始终要求管理员，并保护 Toolbox 本身和原数据卷。验收先做已登记项目只读核对，启停／更新需用户指定对象和单独授权；不为测试创建业务服务。

## 8．错误、隐私和并发

新 JSON 操作使用固定错误结构 `error.code/message/retryable`，不透出私有路径或异常原文。建议错误如下：

| 类别 | 代码示例 |
|---|---|
| 认证／授权 | `invalid_key`、`scope_denied`、`role_denied`、`tool_denied`、`permission_denied` |
| 文件来源 | `root_unavailable`、`acl_unverified`、`secure_open_unavailable`、`unsupported_file_type` |
| 文件变化／传输 | `directory_changed`、`file_changed`、`transfer_expired`、`transfer_busy` |
| 文件写入 | `target_exists`、`target_changed`、`checksum_mismatch`、`recycle_unavailable`、`write_backend_unverified` |
| 项目 | `project_unavailable`、`project_binding_changed`、`native_unavailable` |

JSON REST 参数错误为 400，未认证为 401，越权为 403，已授权范围内不存在为 404，冲突／忙碌为 409，过期句柄为 410，来源门槛未满足为 503。MCP 仍按协议返回工具错误，不把内部 HTTP 码当协议帧。

开发白名单只能访问另外审定的固定诊断；不能创建机器 Token、登记项目、修改资源策略或把测试豁免变成生产权限。首批不新增任意文件／Docker 调试代理。

文件元数据和 Docker 查询使用独立有界并发，不长期占用风扇、屏幕和电源的操作锁。原厂查询复用其固定来源锁。审计只记固定操作、键 ID、来源、结果与耗时；不记路径、文件名、目录项、参数、句柄、原文、头部、凭据或文件字节。下载统计只在当前授权键的传输结果中返回。

## 9．实现、发布与只读 NAS 验收

当前进展与后续验收顺序：

1. 已实现 policy v3、旧键冻结、精确工具、项目登记及 API 管理逐资源选择；继续保留同根多范围与降级收紧回归。
2. 已闭合固定固件 admin 只读正子树、原厂许可与真实句柄链；已有 364b858 三根目录浏览证据，继续用 file_protocol 完整定位尚未通过的根，不能反复只加少量字段部署试探。
3. 文件读取与变更均有生产适配；逐盘显式预检恢复区、ACL、对象／索引及真实回收条件。当前未 NAS 写验收，失败报告实际阻断，不以回调注册替代实机证明。
4. 项目读取、Token 范围校验及管理员控制／更新计划／任务已有执行器，实际使用受各自前置门槛约束；以执行器文档核对，不调用原厂 `project_list`。
5. 定向与完整 Linux 检查通过后发布一个功能镜像，复用原 NAS 项目及 `r1-toolbox-data` 部署；镜像构建和部署成功都不代表实机验收通过。
6. 所有者明确选择根／项目，创建短期只读 Token，先核对元数据。实机同挂载和完整 ACL 门通过后，按明确授权的 admin 样本做字节比对；不为诊断擅读私人文件或修改业务，Docker 写另用专用资源。

当前回归包括文件 provider／executor／传输、项目读取／操作、新部署、原厂业务及真实 HTTP／权限／界面测试；Linux 执行真实 O_PATH、fd、mount ID、Unix socket 与 peer 检查。写入仍需独立原厂证明及对应自动化，所有新增运行时须同步 RAM 缓存白名单。

| 验收范围 | 必测内容 |
|---|---|
| 兼容与权限 | v1／v2 预授权不自动扩权；元数据编辑不升级；三角色×域×精确工具×资源；管理接口拒绝 Bearer／开发白名单 |
| 文件目录 | 同根多子目录、中文／空格、父与兄弟越界、遍历／双重编码、符号链接／硬链接／挂载与目录替换竞态；不泄漏未经授权总数 |
| 文件 ACL | 主体及 uid/gid 映射、虚拟 ACL 与目标 fd 一致；拒绝其他用户私有范围，缺证据无 root 回退 |
| 下载 | 样本整文件 SHA-256、空文件、单 Range 首／尾／开放范围、越界、多范围、HEAD、变更、中断、取消、超时和名额回收 |
| 传输授权 | 发送中停用／撤销／轮换／过期／收紧目录／改来源／关闭 MCP，后续块停止；旧句柄不能用新键或其他来源接续 |
| 文件写入自动化 | 源读／目标写与双向写分别授权；同根重叠授权；上传校验、不可见临时对象、覆盖冲突、回收站拒绝、撤权续验、对象竞态、幂等与部分失败 |
| Docker 只读 | 只见已登记且获授项目，真实状态与 APP／引擎比对；不泄漏环境、原文或路径；项目重建／改路径不按同名换绑；全过程无部署／启停／删除调用 |
| Docker 更新自动化 | 普通读写角色拒绝；项目／服务／仓库越界、计划替换／过期／重放、并发编辑、读回冲突、多服务拒绝、未知任务不重发及真实 digest 分层核验 |
| 运行与隐私 | 大文件不全量缓冲，不挤占硬件控制；错误／审计／日志无凭据和用户内容；打包白名单、重启及新镜像常驻正常 |

验收记录分开列自动化、固定后端实测、Token 真实调用及未通过门槛。只有实际取得可信响应和样本字节比对，才把文件读取／下载标为通过；写入与项目更新须单独列实现和实测状态，不以本次只读结果代替。

## 10．当前证据与剩余门槛

- `api_permissions.permission_allows()` 已按 policy v3 校验角色、功能域、具体工具和资源；旧 policy v1／v2 仍固定原十工具。资源预授权不会生成未实现能力。
- 文件只读与字节通道已有生产执行器，每次元数据和每个块前后重核当前授权、原厂许可、根与最终对象；只处理精确固件下 admin 子树，已有三根目录浏览证明，下载内容及本批上传仍待专用样本实机核对。
- `api_file_roots.collect_file_roots()` 与显式 `file_backend_context` 提供规划和上下文证据；目录句柄元数据不等于最终文件 ACL、安全打开、原厂写索引／配额及原子性证明。
- 二十一项合同均有实际适配；覆盖单独授权。上传准备、PUT、提交和原厂索引确认分开，变更保留可恢复意图；不明结果不重放，详细语义见[file-mutations.md](file-mutations.md)。
- API 管理已支持同根多范围、逐工具及逐项目操作；权限编辑与元数据编辑分开，不把降级或改名变成扩权。
- 全量八十一项另含业务缓存、下载器、媒体和固定原厂能力。原生应用／Docker／下载委托不宣称整体处于 Users 文件沙箱；应用安装需独立精确对象和实际影响预览。
- 固定开发媒体 `probe_business` 及文件上下文只用于显式取证，不默认轮询。固件动作名、静态审计、本地 fixture、HTTP 集成或镜像发布都不能代替 NAS 实机验收。
- 原厂接口证据与范围见[完整注册表](vendor-emhistor-interfaces.md)、[网页后端](vendor-web-interfaces.md)、[MCP 现状](mcp-api.md)及[AI 接入路线](ai-capability-roadmap.md)。

文件、业务和 Docker 的实际验收须按[完成矩阵](mcp-completion-matrix.md)分别列证据与未通过门槛，不把其中一项或一层通过当作全部能力完成。
