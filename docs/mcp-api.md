# 局域网 AI 与 API 管理

Toolbox 在已有 `18732` 端口提供 MCP 与固定 REST 接口，供局域网 AI／自动化客户端读取 NAS 状态和执行明确授权的操作。没有新增容器或宿主依赖，配置与密钥摘要随原 `r1-toolbox-data` 数据卷保存，重建服务后继续有效。

## 网页配置

进入网页“设置 → API 管理 → 打开 API 管理”。服务默认关闭，打开后创建命名访问密钥，选择角色、功能权限、可选来源 IP 和有效期。默认角色为只读，仅勾选状态读取，有效期九十天；可以选择长期。创建成功时显示一次完整密钥和可复制的 MCP 配置，关闭弹窗即清空，后续列表不会再次提供密钥。

可以在列表查看权限、到期时间、最后使用和撤销状态。关闭服务立即阻止所有机器访问，重新开启不会重新生成密钥；撤销、到期和来源限制每次请求都重新检查。管理操作沿用网页已有登录／配对授权及同源、JSON、CSRF 校验；API 密钥不能直接作为网页管理凭据。

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

新密钥的角色和功能权限必须同时满足。只读 `readonly` 可查询；读写 `readwrite` 增加普通业务修改；管理员 `admin` 增加管理操作。管理员仍需逐项勾选功能，并受文件范围限制，不能凭角色进入未授权功能。切换角色不会自动勾选其他功能。

工具列表和实际调用使用同一个权限判断；缺少功能返回 `scope_denied`，角色等级不足返回 `role_denied`。HTTP 200 或工具出现在客户端缓存中均不代表操作已获授权。现有旧密钥显示“原有精确权限”，保留原十个工具与原 scope 的行为；不会因升级获得文件、部署、安装等新能力。

| 权限 | 新密钥最低角色 | 工具 | 行为 |
|---|---|---|---|
| `read` | 只读 | `get_nas_status` | 读取 `overview/system/storage/temperatures/network/containers/apps/fans/lights/screen`；默认概览 |
| `fan` | 读写 | `set_fan_policy`、`release_fan` | 保存／应用完整风扇策略或恢复原厂管理；必须明确传入 `confirmed:true` |
| `light` | 读写 | `set_light_policy`、`set_rgb` | 修改灯光策略或四种原生灯效 |
| `screen` | 读写 | `set_screen`、`update_screen_settings` | 亮度、息屏、唤醒及既有屏幕／屏保设置 |
| `power` | 管理员 | `request_power`、`cancel_power` | 重启／关机要求 `confirmed:true`，保留五秒可取消窗口 |
| `media` | 读写 | `get_media_status` | `face` 查询相册识别；`all` 批量验证七类状态；原厂可能写完成标记，明确按需调用 |

风扇／灯光的完整配置先通过状态读取取得，再修改所需字段；设备控制器继续校验内部结构及硬件能力。写入与网页使用同一操作锁，媒体与开发调试使用同一媒体锁；忙碌时返回明确失败，客户端应重新读取后判断，不把未确认结果当作已生效。

状态读取使用已有监控、SMART 后缓存和原厂应用缓存，不临时启动 SMART 自检或唤醒休眠盘，不猜测未知读数、RPM 或整机功耗。磁盘温度保留自定义名称。应用缓存不可用时明确报告来源状态，不用空列表表示“没有应用”。

媒体权限独立于普通读取：原厂状态查询可能写完成标记、发布事件或进入厂商内部资源修复分支，不能当作无副作用轮询。七项实际请求和零计数限制见[媒体验证](media-probe.md)。

### 待接入功能与文件范围

管理界面还提供 `files/docker/apps/downloads/movie/albums/sharing/backup/system` 的明确预授权，全部默认不选，并标记“待接入”。这些域目前没有对应 MCP 工具；预授权不会立刻发起文件访问、部署、安装、扫描或分享。之后接入的具体工具仍要声明最低角色、参数结构与资源边界。完整目标与原厂证据见[AI 功能接入路线](ai-capability-roadmap.md)。

勾选文件管理时，必须选择当前可信的数据盘根，可填写相对 POSIX 子目录，并为每个范围选择只读或读写。只读角色不能授予读写范围；只读挂载不能授予写入。根目录来自已有物理盘序列号缓存与 `/proc/self/mountinfo`，仅支持可核对的 `/drives/` 下本地物理盘挂载。缺少身份、重复挂载、无法核对的 RAID／虚拟源等不会生成可选目录；界面明确说明不可用，不猜测一个根目录。

根 ID 绑定物理身份、分区源、文件系统与挂载路径，修改显示名称不改变 ID；换盘或路径变化后旧绑定不匹配。子目录不接受绝对路径、`.`／`..`、反斜杠、重复斜杠或控制字符。当前只是保存授权范围，不浏览子目录或传输文件；未来文件操作还必须即时核验磁盘与挂载身份、用户 ACL、符号链接和已打开的文件句柄，不能把这份缓存描述当作安全打开路径的证明。

## REST 接入

与 MCP 使用相同 Bearer 密钥、IP 限制、权限及调用记录，不接受 Cookie 或开发者白名单代替密钥。

- `GET /api/integration/status?section=overview`：返回 `ok/section/data`；状态类别与 `get_nas_status` 相同。
- `GET /api/integration/capabilities`：任意有效密钥可查询自身 `role/policy_version/scopes/file_roots/tools/domains`，无需 `read` 域。工具列表与 MCP 一致，域只返回已选择的项，待接入域明确没有工具；文件范围只投影当前密钥所选根，失效或变为只读的绑定标记不可用。不读取业务文件，不调用媒体或安装 getter。不接受 URL 参数。
- `POST /api/integration/action`：JSON 为 `{"tool":"set_screen","arguments":{"action":"wake"}}`，返回标准工具结果 `content/structuredContent/isError`。HTTP 200 不替代 `isError` 或业务 `ok`。

网页管理使用 `GET /api/access` 和固定 POST `/api/access/settings`、`/api/access/keys`、`/api/access/keys/revoke`，不支持由访问密钥创建更多密钥。创建新策略显式发送 `role`、`scopes`、`file_roots`；文件范围项为 `{"id":"root_<24 位十六进制>","path":"相对/子目录","access":"readonly"}`，空 `path` 表示整个已选根，`access` 也可为 `readwrite`。未选 `files` 时范围必须为空。没有 `role` 的旧创建请求保留旧六 scope 合同，不能使用新域或文件范围，不会自动变成管理员。

公网来源、伪造转发头、任意原厂地址／SQL／命令代理和 URL 中传入密钥均不受支持。文件工具将使用明确的根范围和结构化参数，不能由当前预授权推出任意系统文件访问。

## 存储与运行边界

每个密钥包含三十二字节随机熵，持久化文件 `web/api-access.json` 只保存 SHA-256 摘要和元数据，以原子替换和 `0600` 权限写入。最多三十二个有效密钥、六十四条历史记录。文件损坏或权限异常时拒绝授权；持久写入失败会锁定当前服务实例的访问能力，修复存储并重启后恢复，避免继续使用没有成功保存的授权状态。

状态格式读取兼容 v1／v2：查询列表与鉴权不会重写旧文件，真正的配置、创建、撤销或审计写入在同一次原子替换中升级。旧键以 `policy_version:1/role:legacy` 保留原权限；新角色策略使用 `policy_version:2`。摘要、标识、到期、撤销和来源限制均保留，升级不要求重建原数据卷。超过有界存储上限的修改拒绝保存并保留旧状态。

只允许真实连接来源为 RFC1918 IPv4、IPv6 ULA 或 loopback。来源 IP 列表为空时允许持密钥的上述客户端；填写列表后要求精确匹配，不接受域名、网段和通配符。HTTP `Host` 必须是实际本机地址；提供 `Origin` 时必须同源，非浏览器客户端可以省略 `Origin`，无 CORS 放行。

当前复用网页的 HTTP 监听，密钥在局域网链路中不受 TLS 加密保护；需要跨不可信网络连接时应在部署层提供 HTTPS。此版本没有开放公网接入或任意主机转发能力。

## 验证

自动化包括临时密钥库、真实本机 HTTP、Mock 硬件和网页管理测试：

```sh
python -m unittest discover -s tests -p 'test_api_access.py' -v
python -m unittest discover -s tests -p 'test_api_permissions.py' -v
python -m unittest discover -s tests -p 'test_api_file_roots.py' -v
python -m unittest discover -s tests -p 'test_mcp_service.py' -v
python -m unittest discover -s tests -p 'test_integration_http.py' -v
python -m unittest discover -s tests -p 'test_integration_roles_http.py' -v
node tests/check_api_management.cjs
```

2026-10-29，使用官方 `@modelcontextprotocol/sdk@1.32.2` 对本机隔离 WebConfig 服务完成初始化、权限过滤、状态读取、屏幕 Mock 调用与关闭连接；越权电源调用未触发控制器。新版协议另有原始 HTTP discovery／tools-list 验证。以上均未执行 NAS 实机控制；镜像部署后仍需用网页生成的受限密钥完成实机验收。
