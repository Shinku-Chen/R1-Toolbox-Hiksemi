# 文件读写与固定 Docker 项目接口方案

状态：逐工具授权、文件根元数据和固定 Docker 项目读取已接入；真实文件内容操作与项目更新仍待接入。日期：2026-10-29；本批前代码基线：`4328137fb862fbc9fce9e921d9b3b075d41e10d6`。

本方案覆盖文件读写，以及已登记 Docker 项目的读取和受控镜像更新。用户选择的“先只做文件读取和项目状态核对”仅限定 NAS 实机验收，不取消写能力设计。二十项新增合同中，`list_file_roots` 与三个 Docker 读取工具已有实际回调；其余十六项仍待接入，须完成后端核验、测试和明确授权后才开放。

## 0．已实现的定义入口

- 机器可读定义：[file-docker-contracts.json](api/file-docker-contracts.json)，包含二十项新工具的输入／输出 JSON Schema、最低角色、功能域、资源条件、错误和传输约定。
- 定义来源：`runtime/api_contracts.py`；运行 `python scripts/build-api-contracts.py` 同步 JSON，`--check` 拒绝未同步产物。测试使用开发依赖中的固定 JSON Schema 校验器；不增加 NAS 运行时依赖。
- 所有者接口：`GET /api/access/contracts`，无查询参数；遵循原有 Web `password/pair/open` 授权、同源规则和 `no-store`，Bearer（即使同时带所有者 Cookie）与单独开发窗口不能使用。MCP 关闭时仍可由所有者查阅。
- 网页“API 管理 → 文件与 Docker 接口定义”按需读取、展示和复制定义；不创建 Token、不更改权限、不访问文件或查询原厂服务。

定义目录仍返回 `status:definitions_only`、`execution_allowed:false`，因为查阅定义本身不授予执行权；逐项 `implemented` 反映四项已接入、十六项待接入。这不是 MCP `tools/list`。当前全量可调用目录为十四项，policy v1／v2 仍冻结原十项。`policy_version:3`、逐工具授权、项目登记和存储 v4 已落地；真实文件 ACL 与安全打开尚未闭合。Docker 实现边界见[固定项目读取](docker-projects.md)，只读实机核对见[验收清单](file-docker-readonly-acceptance.md)。

## 1．能力范围与调用方式

MCP `/mcp` 和 `POST /api/integration/action` 共用工具目录、参数校验、授权和回调。JSON 接口沿用现有工具结果包装：`content`、`structuredContent`、`isError`；HTTP 200 不能代替业务 `ok:true`。

| 角色 | 可授权能力 | 资源限制 |
|---|---|---|
| `readonly` | 文件浏览、属性、下载；项目状态与脱敏摘要 | 只读文件范围；已登记项目的 `inspect` |
| `readwrite` | 继承读取；上传、新建目录、改名、复制、移动、回收站删除 | `files` 域、具体工具及对应源／目标范围 |
| `admin` | 继承已授读写；固定 Docker 项目更新规划与执行 | `docker` 域、具体工具、项目／服务／镜像范围及有效计划 |

角色只是权限上限，选择管理员不会自动授予全部工具或全部文件。只读角色仅授只读范围；读写／管理员角色的读取工具也可读取其已授读写范围。普通文件写操作在预授权范围内执行，不增加每次 Web 审批；覆盖和删除等操作仍须提交对应的明确确认。

### 读取工具

| 工具名 | 参数 | 最低角色／域 | 作用 |
|---|---|---|---|
| `list_file_roots` | `{}` | `readonly`／`files` | 只列本键已授予的根及子目录，不浏览盘内目录 |
| `list_directory` | `root_id,path,limit?,cursor?` | `readonly`／`files` | 在已授权目录内分页列出可见条目 |
| `get_file_info` | `root_id,path` | `readonly`／`files` | 单个文件或目录的最小属性 |
| `prepare_file_download` | `root_id,path` | `readonly`／`files` | 核验普通文件，创建绑定当前键的短期下载句柄 |
| `get_file_transfer` | `transfer_id` | `readonly`／`files` | 查询本键下载的状态及实际已发送字节 |
| `cancel_file_transfer` | `transfer_id` | `readonly`／`files` | 取消本键的下载，关闭句柄并释放名额 |
| `list_docker_projects` | `{}` | `readonly`／`docker` | 只列本键获授的已登记项目 |
| `get_docker_project_status` | `project_id` | `readonly`／`docker` | 查询该项目的原厂状态及可确认的运行状态 |
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
    "path": "视频资源/接口验收",
    "limit": 50
  }
}
```

实际授权的相对目录由所有者选择，示例不代表 NAS 上存在该目录。文件写入和固定项目更新属于本方案；任意 Compose 编辑、任意命令和通用原厂 action 代理不开放。创建全新 Docker 项目另按部署能力设计，不能冒用已登记项目的更新权限。

## 2．权限与旧键兼容

每次调用同时满足：有效 Token、MCP 总开关、真实来源 IP、最低角色、显式功能域、精确工具及资源范围。Cookie、小屏令牌、开发窗口不替代机器授权；管理员不绕过任何条件。`tools/list`、REST、下载通道和实际回调使用同一个判定。

### 版本与授权结构

当前持久格式为 v4，兼容读取 v1／v2／v3；权限策略为 policy v1／v2／v3，两者不同。以下为 policy v3 的结构合同：

以下为本批四项新读取工具的授权结构示例，实际根和项目 ID 必须由所有者选择已核验的资源。当前所有者接口只接受已接入的十四项工具，尚未接入的文件内容操作拒绝授权。后续写入键须另选具体写工具并获得目录读写范围；Docker 更新还须 `update_image`，不能仅凭 `inspect` 执行，本批所有者接口只开放项目 `inspect`。

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
      "path": "视频资源/接口验收",
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
4. 读取旧格式只在内存归一化；真正写入才原子迁移至 v4，保留摘要、撤销、停用、到期与来源限制。状态文件升级不代表权限升级。
5. 权限、来源、到期或启停变化推进 `grant_revision`；轮换推进 `credential_generation`。未执行计划与下载句柄按代际失效；仅改名、审计或最近使用时间不改变授权修订。

创建／更新接口沿用 `/api/access/keys` 与 `/api/access/keys/update`，新增字段只接受所有者管理通道。旧客户端省略新字段继续保持旧策略，不能通过提交 `role` 触发新工具授权。

### 网页 API 管理

在功能域下增加具体操作选择；新增操作默认不勾选。文件范围支持同根多条子目录的无损编辑，不能只保留第一条。Docker 另选已登记项目，区分“读取项目”和管理员“更新镜像”；未通过后端核验的操作显示原因并禁止勾选，不能假装已可用。

同根多范围按明确允许范围的并集授权，源和目标分别完整匹配；精确重复拒绝。子目录只读范围不会收窄已经授予的上级读写范围，网页须提示重叠授权的实际效果。

元数据编辑与权限编辑分开跟踪，修改名称或 IP 不重写资源授权。资源失效时保留原 ID 并标原因，不自动换绑同名盘或项目。`capabilities` 区分 `not_granted`、`pending_enable`、`resource_unavailable` 与 `backend_unverified`，不以空结果伪装可用。

## 3．文件范围与安全打开

### 路径语义

- `root_id` 沿用 `root_<24hex>`，绑定物理盘、分区、文件系统和挂载；`path` 始终相对物理根。上限 1024 个 Unicode 字符，并另设有界 UTF-8 字节上限。
- 路径须等于已授予子目录或位于其后代，按组件比较。例如授权 `Movies/私人` 不允许访问 `""`、`Movies` 或 `Movies/私人备份`。
- 拒绝绝对路径、`.`／`..`、反斜杠、重复斜杠、控制字符和无效 UTF-8。保持名称原有大小写和 Unicode，不替用户改名或归一化。
- 不接受原厂路径、用户 ID、端口、URL、原厂 action 或 shell 参数。JSON 路径与原厂 URI 的编解码必须逐层确认，不允许二次解码形成另一条路径。
- 列表不返回宿主挂载路径、未授权根、父目录、兄弟目录或未过滤的原厂总数。Toolbox 私有状态、凭据和恢复资料不因位于数据盘而变成可下载对象。

`list_file_roots` 只复用已授予范围的规划元数据，返回根标签、原配置范围、`binding_status` 与实际后端能力；规划存在不代表已通过 ACL 或安全打开核验。

### 必须先补齐的实现门槛

现有 `api_file_roots.py` 只生成授权规划。principal 没有 NAS 账户映射，也没有安全文件句柄。本批文件读取和写入上线前须完成：

1. 明确所有者委托的 NAS 主体，以及原厂虚拟目录与可信 `/drives` 根之间的一一映射。现有会话 UID `100000` 是原厂逻辑身份，不能直接当作 Linux uid 或任意用户目录的读取许可。
2. 核验账号状态、实际 uid/gid／附加组及原厂共享／虚拟 ACL；需要时同时核验 POSIX ACL。权限证明必须对应最终对象，不能仅凭管理员签名请求成功。
3. 在实际打开时重新核对当前挂载、物理身份、根、父目录和目标对象。拒绝换盘、叠挂载、跨挂载、符号链接、magic link、特殊文件；首批普通文件的多硬链接也先拒绝。
4. 使用经过证明的原厂安全对象／句柄机制，或在 NAS ACL 与内核身份映射闭合后使用受限身份的 fd 读取服务。只处理安全打开返回的同一对象。
5. 原厂若只按路径重新打开，Toolbox 先 `stat/open` 再发路径不能证明对象相同。`realpath`、前后 `stat` 和建议锁也不能闭合竞态；没有等价机制则保持不可用，不退回宿主 root 读取，不用 `/proc/.../fd` 绕过原厂授权。

门槛未满足分别返回 `acl_unverified`、`secure_open_unavailable`、`root_unavailable`，不返回成功空目录。根规划可单独可用；尚未具备真实文件能力的工具在能力目录中标明原因，不宣传已实现文件读取。

文件操作是显式盘访问，可能唤醒硬盘；不加入网页监控、被动开发诊断、自动目录预读或 SMART 采集。

## 4．目录与属性响应

`list_directory` 默认 `limit:50`，范围 1～200；只返回当前主体与文件范围允许、且后端已核验的条目。按固定规则排序，游标按相同顺序续页，不跨目录或跨键使用。

```json
{
  "ok": true,
  "root_id": "root_aaaaaaaaaaaaaaaaaaaaaaaa",
  "path": "视频资源/接口验收",
  "entries": [
    {
      "name": "示例.txt",
      "path": "视频资源/接口验收/示例.txt",
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

单页 JSON 上限建议 128 KiB，达到字节上限也通过游标续页，不能丢条目后宣称完整。游标为有界不透明值，绑定键、代际、授权修订、根实时身份、目录身份和排序；每页重新鉴权，游标本身不授予权限。检测到目录变化返回 `directory_changed`，从第一页重新读取。

不暴露未经授权过滤的 `count/totalpage`，不能确认的总数为 `null`。过滤后为空只表示没有可返回条目，不证明物理目录为空。`get_file_info` 返回同类最小属性和可下载状态，目录大小为 `null`；不递归统计，不读内容算哈希，不返回 ACL 原文或链接目标。

## 5．文件传输与写入

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
- 初始建议每键最多 2 个、全局最多 4 个非终态下载句柄，同一句柄最多 1 个活动 GET；活动连接另计并保留 Web 控制请求名额。准备后五分钟内须首次开始 GET；首次开始后进入总长三十分钟的单调时钟传输窗口。HEAD、Range 重试与连接重建均不续期；空闲 socket 建议十秒超时。
- 每个输出块前检查当前授权修订、代际、服务开关、当前时间与 Token 的 `expires_at`，以及对象状态；自然过期不会改变修订号，不能仅依赖状态文件变化。配置丢失、损坏、时钟不可用或写失败同样关闭传输。续验可使用有一致性保证的授权修订与状态文件身份，不能只持有最初 principal。
- 明确开销与失效界限：已排入 socket 的一块无法追回，暂停／撤销后停止后续块；写 socket 也必须有短超时。
- ACL 或命名空间变化无法继续证明对象归属时停止。每次流结束、客户端断开或超时立即关闭活动 fd、释放活动连接名额；逻辑句柄及其名额按以下状态机保留或释放。续传若允许重新打开，必须再次完成相同对象及 ACL 核验。
- 首版状态：`prepared/streaming/ready/interrupted/completed/cancelled/expired/failed`。成功发送一个部分 Range 后为 `ready`，关闭本次流的 fd、释放活动连接名额，保留逻辑句柄与原对象绑定；下一次 GET 须重新完成同对象及 ACL 核验。连接中断为 `interrupted`，同样关闭活动 fd，剩余窗口内才可由客户端显式重试；不在后台重新发送。
- `completed` 仅用于一次覆盖完整文件的 GET 成功发送；它表示服务端按声明长度发送完成，不代替客户端落盘或哈希校验。多个部分 Range 的累计已发送字节可能重叠，不能据此计算整文件完成率。客户端完成分段读取后可显式取消并释放句柄。
- `completed/cancelled/expired/failed` 为终态，释放非终态句柄名额；`prepared/ready/interrupted` 在各自原窗口届满时过期。终态内存记录最多保留 64 条，淘汰或重启后返回句柄失效。任何续接都不能放宽原对象、授权或期限。
- 已发出 HTTP 头后出现错误只关闭流，不追加 JSON 或伪造第二个状态码；终态由 `get_file_transfer` 返回，MCP 调用本身不占用长流。

HTTP Range 与 HEAD 语义依据 [RFC 9110，第 9.3.2、14 节](https://www.rfc-editor.org/rfc/rfc9110.html#section-14)；上述范围限制是本批明确的服务契约。

### 写工具与资源要求

以下同属待实现的工具目录，最低为 `readwrite`＋`files`＋具体工具授权。管理员也须满足对应目录范围。

| 工具名 | 参数要点 | 范围与行为 |
|---|---|---|
| `prepare_file_upload` | `root_id,path,size_bytes,sha256,overwrite?,expected_target_version?,confirmed?` | 目标目录可写；创建绑定目标和校验值的上传任务 |
| `commit_file_upload` | `transfer_id` | 校验完整上传后提交；不能变更原目标、覆盖条件或文件内容 |
| `create_directory` | `root_id,path` | 父目录可写；只创建单个目录，不隐式递归补建父目录 |
| `rename_file` | `root_id,path,new_name,expected_source_version` | 源与目标父目录可写；新名称只允许单个路径组件 |
| `copy_file` | `source_root_id,source_path,target_root_id,target_path,expected_source_version` | 源可读、目标可写；首版仅普通文件，默认不覆盖 |
| `move_file` | `source_root_id,source_path,target_root_id,target_path,expected_source_version` | 源与目标均可写；首版限同一文件系统的可核验原子移动 |
| `trash_file` | `root_id,path,expected_source_version,confirmed:true` | 对象及父目录可写；只进入已核验的原厂回收站 |
| `get_file_operation` | `operation_id` | 读取本键已授权写任务的终态或实际进度，仍须原操作授权和资源范围 |

除查询外，各写工具还接受必填的有界 `idempotency_key`，重复同键同参数返回原任务，参数变化返回冲突。`sha256` 为 64 位十六进制值，文件长度为非负安全整数。写操作不接受原厂路径、任意 action、原始身份或 shell。操作 ID 使用 `operation_<32hex>`，只定位绑定当前键的任务，不授予权限；文件名、长度、校验值和所有参数均有界。源／目标路径分别复用第 3 节规则，根本身、私有状态及未授权父目录不能作为变更对象。首版不递归复制或删除目录，不用“复制后删源”冒充原子移动。

### 上传和变更提交

- 上传准备返回固定相对内容地址 `/api/integration/files/transfers/<transfer_id>/content`；上传只在该句柄的 `PUT` 接收有界流。每次请求须 Bearer、真实来源、上传工具及目标写权限，不通过 MCP JSON 或 Base64 传文件。下载句柄不能 PUT，上传句柄不能 GET 文件内容。
- 首版不接收任意分块偏移；一次 PUT 严格检查 Content-Length、声明大小及 SHA-256，有界缓冲写入不可见临时对象。禁止未知长度正文、超长或短写提交。中断先查任务状态，不自动重放写入；完成校验后才允许 `commit_file_upload`。
- 临时对象、最终父目录和目标身份绑定同一任务；临时对象按后端已核验的 ACL、配额和索引语义创建，不能由宿主 root 任意写盘。提交时重新证明权限与对象身份，在已验证的同文件系统内原子安装，不能暴露半文件。后端不能证明这些语义时，上传保持不可用。
- 默认不覆盖。上传覆盖须在准备时显式 `overwrite:true`、指定准确目标版本及 `confirmed:true`，提交仍需匹配；确认不替代 Token 授权。首版改名、复制、移动遇目标存在均返回冲突，不暗中覆盖。版本核验与实际变更必须使用同一安全对象机制，不能先检查再按路径覆盖。
- 删除须明确确认并使用已核验的原厂回收站；不可用时拒绝，不能退化为永久删除。清空回收站和永久删除另行设计，不借普通文件读写权限开放。
- PUT 每块、上传提交及其他操作的实际变更点均复验当前 Token、到期时间、代际、授权修订、服务开关、ACL、根和源／目标身份。暂停、撤销、轮换或收紧授权后停止后续动作；已完成变更按真实结果报告，不能宣称全部回滚。
- 写任务绑定键、代际、授权修订、资源与幂等标识，限额、期限及清理均有界。`get_file_transfer/cancel_file_transfer` 也可管理本键上传，但须同时保留对应工具和上传授权；只读键不能取消其他键的写入。任务中断先核对对象与已完成部分，禁止盲目重做。复制等异步任务由 `get_file_operation` 查询，后端无法可靠取消则不提供取消入口。

本次 NAS 实机验收不执行这些写工具。自动化使用隔离 fixture 覆盖写入、覆盖冲突、回收站不可用、撤权、竞态和部分失败；后续写入实机验收使用明确指定的专用目录及样本，不能把只读验收通过写成写入已通过。

## 6．Docker 固定项目与只读工具

### 所有者登记

新增所有者接口，沿用网页登录、同源、JSON、CSRF，拒绝 Bearer 或开发白名单代管：

| 接口 | 作用 |
|---|---|
| `GET /api/docker/projects/catalog` | 读取有界结构化 Docker 元数据，生成安全候选；不调用原厂 `project_list` |
| `GET /api/docker/projects` | 列出 Toolbox 已登记策略与可确认的状态 |
| `POST /api/docker/projects/register` | 按候选 ID 登记，生成 `project_<24hex>` |
| `POST /api/docker/projects/update` | 修改标签等登记元数据，不修改原 Docker 项目 |
| `POST /api/docker/projects/revoke` | 撤销登记，不删除或停止原项目 |

登记保存可信项目名、引擎身份、配置文件位置及文件身份、服务身份、修订号；这些由后端取得，不接受机器传入。候选不含原始环境变量、命令行或日志。候选 ID 只定位，登记时重新验证。

通过固定本机 Docker 通道读取允许的 Compose 标签，交叉确认项目与配置来源。若信息不足，只能显示不可登记原因，不从同名猜测。机器只提交 `project_id`；换引擎、配置路径或项目重建后旧绑定失效，不自动绑定同名资源。

登记默认只允许 `inspect`；所有者可另行登记第 7 节的 `update_image` 服务及镜像范围，只有通过后端门槛才可启用。本次实机验收只登记 `inspect`。`r1-toolbox` 等自身项目可在所有者明确选择后提供只读摘要；机器更新始终受实际容器／项目身份保护，不能靠改名绕过。MCP 本身不登记资源。

### 实际读取来源与原厂备选合同

本批实际走固定本机 Docker Unix socket 的受限 GET，并核验 dockerd peer、引擎与已登记配置／服务身份；项目状态和摘要只声称引擎证据，不读取 Compose 正文。`native_status/pop_status` 为 `null`，摘要明确配置尚未完整解析。下述原厂签名 HTTP 读取仍为备选合同，未作为本批生产适配器开放，不能把静态证据当成已实测。

后端将登记 ID 映射至固定项目，调用已审计的：

```http
POST /rest/1.1/docker?action=get_project_status
Content-Type: application/json

{"name":"后端已核验的项目名"}
```

配置摘要使用同一路由的 `get_project_compose`；固定本机原厂认证，令牌和签名留宿主。不向机器开放原始 Compose、真实路径、任意 name、SQL、shell 或原厂 action。

原厂读取器可能通过 shell 使用项目名，本批名称须匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，拒绝选项前缀、空白、引号、控制字符及 shell 元字符；不通过大小写折叠或重写名称匹配另一个项目。配置文件还须核验为有界普通文件；拒绝符号链接、模糊的多来源和失效身份。多服务项目可提供只读摘要。

`get_docker_project_status` 返回 `project_id/label/native_status/pop_status/running/health/observed_at/source`；`running/health` 必须有真实引擎证据，否则为 `null`，原厂内存状态单独保留。找不到项目的 `code:0,status:-1` 不当成功存在。

`get_docker_project_summary` 返回服务名、有效字面量镜像、可确认的端口、挂载类型及授权标签、重启策略和风险标记。绝对宿主路径、环境变量值、敏感标签、原始 Compose、命令与日志不输出。插值或无法解析字段为 `null`，不展开 `.env` 后把秘密拼进摘要。安全解析拒绝自定义 YAML 构造及无界 alias。

列表与 capabilities 只返回当前键获授的登记 ID；未授权项目统一拒绝，不用错误差异探测存在性。原有 `read` 域全局监控语义保持；需要只看固定项目的键只授予 `docker` 及本批工具。

### 原厂末尾换行的精确修正

此前实机看见读回多一个 LF，进一步反汇编确认来源为 `docker_read_file@0x42b2c6`：`0x42b4a1` 在读取结果末尾无条件附加一个合成 LF。不是已证明的保存器追加行为。

对已核验版本，只能移除恰好一个合成 LF 后计算原文 hash／生成差异；不能 `rstrip()` 去掉原有空行，也不能把 getter 响应原样反复保存。版本或响应约定无法确认时保持不可用。首批摘要不写回 Compose。

## 7．管理员受控更新工具

本节属于本批读写方案，仍待实现和后端核验；本次 NAS 验收不执行更新。

| 工具名 | 参数要点 | 最低角色／资源 |
|---|---|---|
| `plan_docker_project_update` | `project_id,service_id,image_reference` | `admin`＋`docker`＋具体工具＋项目 `update_image`；目标须在所有者预登记的服务及镜像仓库范围内 |
| `execute_docker_project_update` | `plan_id,idempotency_key,confirmed:true` | 同上；只执行本键生成、未失效且未消费的绑定计划 |
| `get_docker_project_task` | `project_id,task_id` | `readonly`＋`docker`＋具体工具＋项目 `inspect`；只读本键任务的脱敏状态 |

计划和任务 ID 分别为 `plan_<32hex>`、`task_<32hex>`；输入均有界且拒绝额外字段。执行只接受计划 ID，不允许替换项目、服务、镜像或附加 Compose。任务查询权限不包含执行权限。

- 所有者预登记允许更新的项目、服务与精确镜像仓库；Token 另授予具体规划／执行工具及项目操作。范围内 admin＋docker＋有效计划＋`confirmed:true` 即可执行，不额外增加每次 Web 审批。
- 更新镜像是在容器现有挂载、设备和特权下运行新代码，执行最低 `admin`，不是普通 `readwrite` 设置。文件工具的目录范围不能限制该代码的宿主权限。Toolbox 自更新首批机器流程禁止。
- 只允许服务 image 差异；镜像标签必须解析并绑定可信不可变 digest。计划绑定键代际、授权修订、项目修订、引擎／配置身份、原文 hash、服务与目标 digest，十分钟内首次执行；重启使未执行计划失效。
- 已审原厂 `project_update_all` 执行整个项目的 `up -d --pull always`，不能选择一个服务。首轮执行只支持单服务、单 Compose 文件；多服务、`build/include/extends`、未闭合的插值、hooks 或外部配置先拒绝，避免顺带更新其他服务或执行未知步骤。
- 同一计划原子消费并绑定幂等键，重复只返回原任务。未知、断线或重启后先读取现状，不自动重发。任务需要私有有界恢复记录，不能泄漏原文或凭据。
- 原厂 `set_project_compose` 为非原子 `O_TRUNC` 且没有 CAS。Toolbox 项目锁不能排除原厂 APP 并发；保存前后核验，检测冲突停止，不声称具备跨客户端事务，也不自动覆盖回滚。并发边界未审定前不开放执行。
- 保存与读回不符时不能调用更新。原厂取消含强杀／`down` 分支，先不开放取消部署工具。
- 成功需核对 pinned 引用、平台 manifest／config digest、实际容器 image ID、运行／健康及原挂载；`container.Image` 是 config ID，不与 registry manifest digest 直接比较。原厂 `code:0/status:1/pop_status:7` 不能代替验收。

镜像身份层次依据 [OCI image manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md)。Compose 的重建、依赖与卷行为见 [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)；厂商具体命令及副作用仍以本固件审计为准。

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

实施顺序：

1. 完成 policy v3、旧键冻结、精确工具和项目资源登记；更新 API 管理 UI，验证同根多范围无损保存。
2. 审定原厂 `file/get_list/get_info/download` 的方法、虚拟路径、ACL、编解码、分页和实际打开链。固定有界诊断可一次返回全部证据状态，不能反复只加少量错误字段打包试探。
3. 实现满足门槛的安全文件后端、下载／上传通道及逐项写操作。若 ACL／句柄或写入语义证据不足，对应能力保持未开放并给出结构化原因；不能用 Mock 或根目录列表证明真实访问安全。
4. 实现原厂固定项目读取、投影、登记、Token 范围校验及管理员镜像更新计划／任务；不调用 `project_list`，并发与安全执行门槛未通过则禁止更新执行。
5. 定向与完整 Linux 检查通过后发布一个功能镜像，复用原 NAS 项目及 `r1-toolbox-data` 部署；镜像构建和部署成功都不代表实机验收通过。
6. 所有者在 API 管理中明确选择文件目录与已有项目，创建短期只读 Token。实机先读取、下载比对和项目状态，不更新项目、不创建样本服务、不修改已有业务文件。当前未指定真实验收目录和文件名，不在方案中虚构或替用户选择私人文件。

建议新增测试文件：`test_api_file_io.py`、`test_file_transfer_http.py`、`test_docker_project_reads.py`、`test_docker_project_updates.py`、`test_integration_resources_http.py`；扩展 `test_api_access.py`、`test_api_permissions.py`、`test_mcp_service.py`、`test_api_file_roots.py`、`test_bundle.py` 和 `check_api_management.cjs`。实现时按最终模块名调整，新增运行时同步所有 RAM 缓存白名单。

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

## 10．依据与当前代码缺口

- `api_permissions.permission_allows()` 的 policy v2 只判角色、域和文件范围，增加新工具前须加精确工具约束。
- `api_file_roots.collect_file_roots()` 只读挂载元数据和已有身份缓存，不能作为实际打开的安全证明。
- `api_access.authorize()` 只返回请求开始时的 principal，需补授权修订、Token 代际和传输续验。
- `web_config.Handler.send()` 会整段缓冲，HEAD 当前统一拒绝；下载必须有专门路由和发送器。
- API 管理当前同根只选首条 grant 的展示逻辑需要修复，权限脏状态不能与名称／IP 编辑混用。
- 原厂接口证据与范围见[完整注册表](vendor-emhistor-interfaces.md)、[网页后端](vendor-web-interfaces.md)、[MCP 现状](mcp-api.md)及[AI 接入路线](ai-capability-roadmap.md)。

方案由主代理汇总；原生子代理分别复核文件传输、固定项目合同和权限迁移。静态审计及已保存的实机证据不代替本批新增接口的部署验收。
