# 局域网 AI 与 API 管理

Toolbox 在常驻 Web 的 `18732` 端口提供 MCP 与固定 REST 接口，供局域网 AI／自动化客户端读取 NAS 状态和执行明确授权的操作。小屏可独立关闭，MCP 默认关闭；开启后仍需独立 Token。配置与 Token 摘要随原 `r1-toolbox-data` 数据卷保存，重建服务后继续有效。

## 网页配置

进入网页“设置 → API 管理 → 打开 API 管理”，或从“服务与开发”进入。创建命名 Token，选择角色、功能域、具体工具、文件范围、已登记 Docker 项目、来源 IP／CIDR 和有效期。默认只读，仅勾选状态读取，有效期九十天；可选择长期。创建或轮换成功时只显示一次完整 Token 与可复制的 MCP 配置，关闭弹窗即清空，后续列表不再提供明文。

列表支持编辑、停用／启用、轮换和撤销。停用保留 Token 与配置；轮换原子替换摘要，旧 Token 立即失效，新 Token 只显示一次，权限、停用状态和到期时间不变。撤销不可恢复；到期需明确修改有效期，不能靠启用或轮换绕过。编辑默认保持原权限与到期时间，旧密钥不会自动升级成管理员。

关闭 MCP 服务立即阻止所有机器访问，重新开启不会重新生成或删除密钥。管理页面默认无口令可使用，设置口令后需要网页登录；显式 `pair` 模式保留旧配对流程。同源、JSON、CSRF 始终校验；API Token 不能作为网页管理凭据，开发者白名单也不能代管密钥。

API 管理只在主动打开和操作后读取列表，不参加十秒监控轮询。最近调用记录只保留时间、密钥 ID／名称、操作、来源、结果和耗时，最多一百条，不记录请求参数、工具输出、请求头或凭据。

## MCP 接入

地址为 `http://NAS_IP:18732/mcp`，传输方式为 Streamable HTTP。在支持自定义请求头的 MCP 客户端中填入此地址，并设置 `Authorization: Bearer <网页创建的访问密钥>`。网页生成的参考配置如下，不同客户端的配置入口可能不同：

```json
{
  "mcpServers": {
    "toolbox": {
      "url": "http://NAS_IP:18732/mcp",
      "headers": {"Authorization": "Bearer <访问密钥>"}
    }
  }
}
```

实现兼容协议 `2026-07-28`、`2025-11-25`、`2025-06-18` 和 `2025-03-26`。新版使用逐请求 `_meta`、`server/discover` 和 `Mcp-Method`／`Mcp-Name` 镜像，旧版使用 `initialize`／`notifications/initialized`。返回普通 JSON，不建立协议会话，也不提供 SSE 长连接；GET／DELETE 返回 405。请求必须声明接受 `application/json` 和 `text/event-stream`，质量值须大于零。未实现 OAuth 自动登录，仅支持部署者明确配置的访问密钥。

协议依据：[当前 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[当前服务发现](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)、[2025 传输与兼容](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 权限与工具

新策略同时要求角色、功能域、精确工具及资源范围。只读 `readonly` 可查询；读写 `readwrite` 增加已接入的普通修改；管理员 `admin` 增加已接入的管理操作。管理员仍受具体工具、文件范围和项目范围限制。切换角色不会自动勾选其他工具，也不使尚未实现的文件写入或项目更新变得可用。

工具列表和实际调用使用同一个权限判断；缺少域、具体工具或所需资源时拒绝，角色等级不足也不能调用。HTTP 200 或工具出现在客户端缓存中均不代表操作已获授权。policy v1／v2 固定保留原十工具，再受其原角色与域限制；以前勾选过 `files/docker` 的预授权也不会自动激活新能力。所有者须明确选择升级并勾选具体工具，提交 `tool_grants` 后才进入 policy v3。

当前共十四个真实 MCP／REST 工具：原十项加文件根元数据一项、固定项目读取三项。具体工具如下：

| 权限 | 新密钥最低角色 | 工具 | 行为 |
|---|---|---|---|
| `read` | 只读 | `get_nas_status` | 读取 `overview/system/storage/temperatures/network/containers/apps/fans/lights/screen`；默认概览 |
| `fan` | 读写 | `set_fan_policy`、`release_fan` | 保存／应用完整风扇策略或恢复原厂管理；必须明确传入 `confirmed:true` |
| `light` | 读写 | `set_light_policy`、`set_rgb` | 修改灯光策略或四种原生灯效 |
| `screen` | 读写 | `set_screen`、`update_screen_settings` | 亮度、息屏、唤醒及既有屏幕／屏保设置 |
| `power` | 管理员 | `request_power`、`cancel_power` | 重启／关机要求 `confirmed:true`，保留五秒可取消窗口 |
| `media` | 读写 | `get_media_status` | `face` 查询相册识别；`all` 批量验证七类状态；原厂可能写完成标记，明确按需调用 |
| `files` | 只读 | `list_file_roots` | 仅投影本键已授权根和子目录的绑定元数据，不列盘内文件或传输内容 |
| `docker` | 只读 | `list_docker_projects`、`get_docker_project_status`、`get_docker_project_summary` | 仅读取本键获授且已登记项目的状态及脱敏摘要，要求项目 `inspect` |

风扇／灯光的完整配置先通过状态读取取得，再修改所需字段；设备控制器继续校验内部结构及硬件能力。写入与网页使用同一操作锁，媒体与开发调试使用同一媒体锁；忙碌时返回明确失败，客户端应重新读取后判断，不把未确认结果当作已生效。

原有 `get_nas_status` 使用已有监控、SMART 后缓存和 Toolbox／原厂应用缓存，不临时启动 SMART 自检或唤醒休眠盘，不猜测未知读数、RPM 或整机功耗。磁盘温度保留自定义名称。应用缓存不可用时明确报告来源状态，不用空列表表示“没有应用”。显式 `get_media_status` 查询结果同时写入 Toolbox 媒体快照，供网页被动读取逐项时间和结果；缓存写失败单独报告，不重复原厂查询。见[业务来源](business-diagnostics.md)。

媒体权限独立于普通读取：原厂状态查询可能写完成标记、发布事件或进入厂商内部资源修复分支，不能当作无副作用轮询。七项实际请求和零计数限制见[媒体验证](media-probe.md)。

### 文件根元数据与待接入的文件读写

完整文件读写及固定项目更新见[接口方案](file-docker-api-plan.md)。目标仍包括只读浏览／下载、读写角色的上传与文件管理，以及管理员受控镜像更新；这些内容访问和写工具尚未注册执行器，不能勾选或调用。本次 NAS 验收先做元数据与项目状态核对，不取消写能力目标。

二十项接口的[机器可读定义](api/file-docker-contracts.json)中，四项已有执行适配，另外十六项仍待接入。所有者可在 API 管理中按需查看，或读取 `GET /api/access/contracts`。该入口不接受机器 Token 或单独开发窗口；读取定义本身不授予执行权限，也不迁移旧键。单项 `implemented` 标记不替代当前 Token 授权、真实来源可用性或实机验收。具体部署后核对顺序见[只读验收清单](file-docker-readonly-acceptance.md)。

`files/docker` 已部分接入，新增工具和资源默认不选；`apps/downloads/movie/albums/sharing/backup/system` 仍无对应机器工具。待接入域的元数据不授予未来工具。网页只允许选择已注册的具体操作，后续新增工具必须再次明确选择。完整目标与原厂证据见[AI 功能接入路线](ai-capability-roadmap.md)。

勾选文件管理时，必须选择当前可信的数据盘根，可填写相对 POSIX 子目录，并为每个范围选择只读或读写。只读角色不能授予读写范围；只读挂载不能授予写入。根目录来自已有物理盘序列号缓存与 `/proc/self/mountinfo`，仅支持可核对的 `/drives/` 下本地物理盘挂载。缺少身份、重复挂载、无法核对的 RAID／虚拟源等不会生成可选目录；界面明确说明不可用，不猜测一个根目录。

根 ID 绑定物理身份、分区源、文件系统与挂载路径，修改显示名称不改变 ID；换盘或路径变化后旧绑定不匹配。子目录不接受绝对路径、`.`／`..`、反斜杠、重复斜杠或控制字符。网页支持同根多条子目录的无损编辑，精确重复拒绝；授权按允许范围并集计算，子目录只读授权不会收窄上级已授读写范围。

`list_file_roots` 返回 `verification:metadata_only` 和文件后端未核验原因，不暴露宿主挂载路径。当前不浏览子目录、不打开文件、不上传或下载。NAS 主体、uid／gid、原厂 ACL、虚拟路径到实际对象的映射和安全打开证明尚未闭合，不能以根规划或管理员原厂会话冒充文件访问权限。`GET /api/access/file-readiness` 仅报告这些缺失证据，不读用户文件、凭据或 SMART，不提供 root 读取回退。

### 固定 Docker 项目读取

所有者先从项目候选中登记，再将项目 ID 的 `inspect` 授予具体密钥。网页提供 `GET /api/docker/projects/catalog`、`GET /api/docker/projects`，以及 `POST /api/docker/projects/register`、`/update`、`/revoke`。登记、改标签和撤销仅修改 Toolbox 私有登记，不创建、部署、启停或删除原项目。API 管理自己的登记目录只读本地元数据；主动读取上述项目接口或调用项目工具时才核验引擎来源。

读取器固定使用本机 Docker Unix socket 和允许的 GET，核验 socket 与 root `dockerd` peer 身份；一轮查询共享八秒绝对网络窗口，并有单体、累计字节和并发上限。以引擎、项目、容器服务及配置文件元数据绑定资源，重建或配置身份变化后不按同名换绑。仅取得配置文件身份，不读取 Compose 原文，也不返回环境变量、命令、日志或绝对宿主路径。

当前状态和摘要均使用引擎证据；项目状态返回 `source:engine`，`native_status/pop_status` 保持 `null`，不把引擎响应伪装成原厂 REST 状态。Docker 三项工具及文件根元数据工具均在短读前后执行 `is_current()`，重新核验有效存储、服务开关、键状态、自然到期、授权修订和 Token 代际，失效则丢弃未发送结果。详见[固定项目读取](docker-projects.md)。

## REST 接入

与 MCP 使用相同 Bearer 密钥、IP 限制、权限及调用记录，不接受 Cookie 或开发者白名单代替密钥。

- `GET /api/integration/status?section=overview`：返回 `ok/section/data`；状态类别与 `get_nas_status` 相同。
- `GET /api/integration/capabilities`：任意有效密钥可查询自身 `role/policy_version/scopes/tool_grants/file_roots/docker_projects/tools/domains`，无需 `read` 域。工具列表与 MCP 一致，域只返回已选择的项；文件范围仅投影当前密钥所选根，失效或变为只读的绑定标记不可用，不输出绝对 `mount`。此接口不读取业务文件、不探测 Docker 引擎、不调用媒体或安装 getter，不接受 URL 参数。
- `POST /api/integration/action`：JSON 为 `{"tool":"set_screen","arguments":{"action":"wake"}}`，返回标准工具结果 `content/structuredContent/isError`。HTTP 200 不替代 `isError` 或业务 `ok`。

网页所有者管理使用以下固定接口，全部拒绝 Bearer Token 代替网页授权：

| 接口 | 请求与含义 |
|---|---|
| `GET /api/access` | 安全元数据、角色／功能／文件根目录与有界审计，不含摘要或 Token |
| `GET /api/access/contracts` | 二十项接口定义与当前适配标记；不以定义授予执行权限 |
| `GET /api/access/file-readiness` | 文件访问就绪证据与未通过门槛；不打开用户文件 |
| `POST /api/access/settings` | `{"enabled":true}`；与“服务与开发”的 MCP 总开关共用状态 |
| `POST /api/access/keys` | 创建具名 Token；成功响应只在本次含明文 |
| `POST /api/access/keys/update` | `id` 加要修改的 `name/role/scopes/tool_grants/file_roots/docker_projects/allowed_ips/expires_days`，至少一项修改 |
| `POST /api/access/keys/enable` | `{"id":"...","enabled":false}` 暂停；`true` 恢复，不能恢复已撤销 Token |
| `POST /api/access/keys/rotate` | `{"id":"..."}`；新 Token 只在本次响应显示，旧摘要立即移除 |
| `POST /api/access/keys/revoke` | `{"id":"..."}`；永久撤销 |

创建 policy v3 显式发送 `role`、`scopes`、`tool_grants` 及所需资源。文件范围项为 `{"id":"root_<24 位十六进制>","path":"相对/子目录","access":"readonly"}`，空 `path` 表示整个已选根，`access` 也可为 `readwrite`；勾选 `files` 须至少一项，未选则必须为空。Docker 项为 `{"id":"project_<24 位十六进制>","operations":["inspect"]}`，须对应未撤销的登记，选 `docker` 须至少一项。当前管理入口只允许 `inspect`，不能借预授权启用 `update_image`。

没有 `role` 的旧创建请求仍为 policy v1，仅允许旧六 scope；有角色但没有 `tool_grants` 的旧请求保持 policy v2，两者均只能使用原十工具。policy v3 中即使保留某个域，未选具体工具也不获授权。

更新只合并显式字段：省略 `role/scopes/tool_grants/file_roots/docker_projects` 保留原权限，省略 `expires_days` 保留原到期时间。明确提供有效期时从本次保存重新计算，`0` 为长期，`1～365` 为天数。旧键单独选择角色最多按兼容行为转 policy v2，只有明确提交工具集才转 policy v3；已有 v3 改角色不会降回 v2。降低角色或移除域时须同步收紧不兼容的工具和资源，否则整项拒绝。尚未挂载的旧根不阻止只改名称／IP 等元数据，实际资源调用仍重新核验身份。

服务所有者另有 `GET /api/services` 和固定 `POST /api/services/screen`、`/api/services/mcp`、`/api/services/developer`，管理可选小屏、MCP 开关与有时限的开发白名单。它们不属于机器工具，拒绝 API Token、URL 查询参数和片段，写入保留网页授权、同源、JSON、CSRF 和操作锁。小屏保存意图与实际接管分开；开发窗口有效不代表任意来源获权，见[开发诊断](dev-diagnostics.md)。

三类入口不能混用：外部 AI 的 MCP／REST 始终使用独立 Bearer；Toolbox 内部 Python 模块直接调用已限定的函数，不需要 Toolbox Token，也不能据此开放 HTTP 免授权操作；开发者固定诊断不需要 Token，但必须处于开发模式、实际 IP／CIDR 非空白名单及绝对有效窗口内，并保留同源／JSON／CSRF。开发白名单不能管理自己、机器密钥或项目登记。

开发诊断可显式请求 `file_access_readiness`、`docker_project_readiness`，例如 `{"action":"snapshot","checks":["file_access_readiness","docker_project_readiness"]}`。默认快照仍只有原 `device/storage/business/fan` 四项；新项不参加默认轮询。采集结束再次检查开发窗口，已过期或收回时不返回采集结果。文件项只报告缺失证明，项目项仅读取候选与登记绑定，不部署或读取 Compose 原文。

公网来源、伪造转发头、任意原厂地址／SQL／命令代理和 URL 中传入密钥均不受支持。文件工具将使用明确的根范围和结构化参数，不能由当前预授权推出任意系统文件访问。

## 存储与运行边界

每个密钥包含三十二字节随机熵，持久化文件 `web/api-access.json` 只保存 SHA-256 摘要和元数据，以原子替换和 `0600` 权限写入。最多三十二个有效密钥、六十四条历史记录。文件损坏或权限异常时拒绝授权；持久写入失败会锁定当前服务实例的访问能力，修复存储并重启后恢复，避免继续使用没有成功保存的授权状态。

状态文件当前为 v4，读取兼容 v1／v2／v3；权限策略版本与文件格式版本分开。单纯读取配置或校验凭据不为迁移重写文件；真正配置、编辑、轮换或审计写入在同一次原子替换中升级存储格式，保留旧 policy v1／v2 的原十工具边界。v3 权限策略增加精确 `tool_grants` 和项目范围，不因存储升级自动启用。摘要、标识、到期、撤销和来源限制保留，升级不要求重建原数据卷。超过有界存储上限的修改拒绝保存并保留旧状态。

键保存 `grant_revision` 和 `credential_generation`。权限、来源、到期、启停及首次撤销的实际变化推进授权修订，轮换推进凭据代际；纯改名、审计、最近使用时间或没有实际变化的保存不推进。`is_current()` 只续验已经由 `authorize()` 认证的内部 principal，不是初始认证入口，不接受 HTTP 提交 principal，也不返回摘要。自然到期独立比较当前时间，不能因修订号未变而继续放行。

只允许真实连接来源为 RFC1918 IPv4、IPv6 ULA 或 loopback。来源规则为空时允许持 Token 的上述客户端；填写后必须命中精确 IP 或局域网网段。支持 IPv4／IPv6、CIDR 和 IPv4 点分掩码，例如 `192.168.31.0/24`、`fd00::/64`、`192.168.31.0/255.255.255.0`；服务端规范化网络地址，单主机 `/32`／`/128` 归并为精确 IP，拒绝等价重复规则。网段必须完整位于允许的私网或回环范围，拒绝域名、通配符、`0.0.0.0/0` 和部分覆盖公网的网络。`Forwarded`／`X-Forwarded-For` 不替代真实 peer。HTTP `Host` 必须是实际本机地址；提供 `Origin` 时必须同源，非浏览器客户端可省略 `Origin`，无 CORS 放行。

当前复用网页的 HTTP 监听，密钥在局域网链路中不受 TLS 加密保护；需要跨不可信网络连接时应在部署层提供 HTTPS。此版本没有开放公网接入或任意主机转发能力。

## 验证

自动化包括临时密钥库、真实本机 HTTP、Mock 硬件和网页管理测试：

```sh
python -m unittest discover -s tests -p 'test_api_access.py' -v
python -m unittest discover -s tests -p 'test_api_permissions.py' -v
python -m unittest discover -s tests -p 'test_api_file_roots.py' -v
python -m unittest discover -s tests -p 'test_api_file_readiness.py' -v
python -m unittest discover -s tests -p 'test_api_docker_projects.py' -v
python -m unittest discover -s tests -p 'test_mcp_service.py' -v
python -m unittest discover -s tests -p 'test_integration_http.py' -v
python -m unittest discover -s tests -p 'test_integration_roles_http.py' -v
python -m unittest discover -s tests -p 'test_integration_resources_http.py' -v
node tests/check_api_management.cjs
node tests/check_service_settings.cjs
```

2026-10-29，使用官方 `@modelcontextprotocol/sdk@1.32.2` 对本机隔离 WebConfig 服务完成初始化、权限过滤、状态读取、屏幕 Mock 调用与关闭连接；越权电源调用未触发控制器。新版协议另有原始 HTTP discovery／tools-list 验证。新权限、固定项目与文件就绪检查以隔离 fixture 和真实本机 HTTP 验证；新镜像部署后仍须按清单用受限密钥完成 NAS 核对，当前不能宣称项目工具已实机通过，更不能据此宣称文件读写通过。
