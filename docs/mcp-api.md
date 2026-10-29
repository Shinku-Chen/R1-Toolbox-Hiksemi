# 局域网 AI 与 API 管理

Toolbox 在已有 `18732` 端口提供 MCP 与固定 REST 接口，供局域网 AI／自动化客户端读取 NAS 状态和执行明确授权的操作。没有新增容器或宿主依赖，配置与密钥摘要随原 `r1-toolbox-data` 数据卷保存，重建服务后继续有效。

## 网页配置

进入网页“设置 → API 管理 → 打开 API 管理”。服务默认关闭，打开后创建命名访问密钥，选择权限、可选来源 IP 和有效期。默认仅勾选状态读取，有效期九十天；可以选择长期。创建成功时显示一次完整密钥和可复制的 MCP 配置，关闭弹窗即清空，后续列表不会再次提供密钥。

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

工具列表按每次请求的密钥权限过滤；直接调用隐藏工具也会被拒绝，不能只绕过客户端列表来获得权限。

| 权限 | 工具 | 行为 |
|---|---|---|
| `read` | `get_nas_status` | 读取 `overview/system/storage/temperatures/network/containers/apps/fans/lights/screen`；默认概览 |
| `fan` | `set_fan_policy`、`release_fan` | 保存／应用完整风扇策略或恢复原厂管理；必须明确传入 `confirmed:true` |
| `light` | `set_light_policy`、`set_rgb` | 修改灯光策略或四种原生灯效 |
| `screen` | `set_screen`、`update_screen_settings` | 亮度、息屏、唤醒及既有屏幕／屏保设置 |
| `power` | `request_power`、`cancel_power` | 重启／关机要求 `confirmed:true`，保留五秒可取消窗口 |
| `media` | `get_media_status` | `face` 查询相册识别；`all` 批量验证七类状态；明确按需调用 |

风扇／灯光的完整配置先通过状态读取取得，再修改所需字段；设备控制器继续校验内部结构及硬件能力。写入与网页使用同一操作锁，媒体与开发调试使用同一媒体锁；忙碌时返回明确失败，客户端应重新读取后判断，不把未确认结果当作已生效。

状态读取使用已有监控、SMART 后缓存和原厂应用缓存，不临时启动 SMART 自检或唤醒休眠盘，不猜测未知读数、RPM 或整机功耗。磁盘温度保留自定义名称。应用缓存不可用时明确报告来源状态，不用空列表表示“没有应用”。

媒体权限独立于普通读取：原厂状态查询可能写完成标记、发布事件或进入厂商内部资源修复分支，不能当作无副作用轮询。七项实际请求和零计数限制见[媒体验证](media-probe.md)。

## REST 接入

与 MCP 使用相同 Bearer 密钥、IP 限制、权限及调用记录，不接受 Cookie 或开发者白名单代替密钥。

- `GET /api/integration/status?section=overview`：返回 `ok/section/data`；状态类别与 `get_nas_status` 相同。
- `POST /api/integration/action`：JSON 为 `{"tool":"set_screen","arguments":{"action":"wake"}}`，返回标准工具结果 `content/structuredContent/isError`。HTTP 200 不替代 `isError` 或业务 `ok`。

网页管理使用 `GET /api/access` 和固定 POST `/api/access/settings`、`/api/access/keys`、`/api/access/keys/revoke`，不支持由访问密钥创建更多密钥。公网来源、伪造转发头、任意原厂地址／SQL／命令／文件参数和 URL 中传入密钥均不受支持。

## 存储与运行边界

每个密钥包含三十二字节随机熵，持久化文件 `web/api-access.json` 只保存 SHA-256 摘要和元数据，以原子替换和 `0600` 权限写入。最多三十二个有效密钥、六十四条历史记录。文件损坏或权限异常时拒绝授权；持久写入失败会锁定当前服务实例的访问能力，修复存储并重启后恢复，避免继续使用没有成功保存的授权状态。

只允许真实连接来源为 RFC1918 IPv4、IPv6 ULA 或 loopback。来源 IP 列表为空时允许持密钥的上述客户端；填写列表后要求精确匹配，不接受域名、网段和通配符。HTTP `Host` 必须是实际本机地址；提供 `Origin` 时必须同源，非浏览器客户端可以省略 `Origin`，无 CORS 放行。

当前复用网页的 HTTP 监听，密钥在局域网链路中不受 TLS 加密保护；需要跨不可信网络连接时应在部署层提供 HTTPS。此版本没有开放公网接入或任意主机转发能力。

## 验证

自动化包括临时密钥库、真实本机 HTTP、Mock 硬件和网页管理测试：

```sh
python -m unittest discover -s tests -p 'test_api_access.py' -v
python -m unittest discover -s tests -p 'test_mcp_service.py' -v
python -m unittest discover -s tests -p 'test_integration_http.py' -v
node tests/check_api_management.cjs
```

2026-10-29，使用官方 `@modelcontextprotocol/sdk@1.32.2` 对本机隔离 WebConfig 服务完成初始化、权限过滤、状态读取、屏幕 Mock 调用与关闭连接；越权电源调用未触发控制器。新版协议另有原始 HTTP discovery／tools-list 验证。以上均未执行 NAS 实机控制；镜像部署后仍需用网页生成的受限密钥完成实机验收。
