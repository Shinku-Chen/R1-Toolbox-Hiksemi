# 海康原厂 REST 与内置网页后端接口

2026-10-29，基于用户提供的 `V6.3.3_Build_260907` 固件、定向提取的前端源码和当前 NAS 只读核对。网页入口、静态代码中的调用和业务接口实测分别记录，不把发现路由当作业务验收通过。

## 已确认的实机入口

- `http://192.168.31.5:80/rest/1.1/openinfo?action=get_ip_info&web=1`：未携带令牌的固定 GET 返回 HTTP 200、原厂 `code:-1004` 和 `Access_token invalid!`。这确认了实际 HTTP REST 端口和认证层，不能据此判断用户提供的签名有效或无效。用户提供的真实签名未写入仓库、日志或用于此请求。
- `192.168.31.5:20443`：外部 TLS 握手成功，协议为 TLSv1.2。本轮未在未建立可信 pin 的外部连接上发送 HTTP 查询或凭据；已有宿主媒体适配使用本机公共证书 pin 访问这一固定端口。
- `http://192.168.31.5:9000/`：Portainer Docker 管理网页，实际进入 `/#!/auth` 账户登录页。本轮没有正常登录会话，未尝试任何固件静态凭据、qB 凭据或其他账户口令。
- `GET /api/system/status`：在同一 `9000` 服务上无凭据返回 HTTP 200，版本为 `2.45.3`。只投影版本，不保存或输出实例标识。这个状态接口可读不代表容器、Compose 或账户接口免认证。
- `http://192.168.31.5/`：标准 HTTP 根路径返回 404，响应标识为 `REST-Webs`。能确认原厂 HTTP 服务存在，尚未找到这里的完整 NAS 管理页面。

实机 Portainer 的前端主包为 `main.6ede6ef01f87c80db8f6.js`，SHA-256 为 `f84b009fe8887f40e60b9c3410cfeed7433067d5f977f43734aca519274e429a`。它与固件归档的前端不同；下面固件源码确认的调用合同仍需按当前服务逐项核对，不能以相同页面标题代替版本匹配。

对实机公开主包的只读交叉核对也确认了 `api` 基址、`/endpoints`、`/stacks`、`/system` 和由环境 ID 构造的 `/docker/` 代理路径。该主包声明了同名 `.js.map`，本轮未进一步请求映射。以上是当前前端能力证据，仍不等于业务 API 已授权调用成功。

## `openinfo/get_ip_info` 的额外限制

该 action 在固件中注册为 `/rest/1.1/openinfo` 的 `get_ip_info`，handler 为 `0x390ae9`。handler 在 `0x390bf6` 读取连接 peer，于 `0x390c00–0x390c25` 比较 `127.0.0.1`；非回环返回通用 `code:-1`。成功返回的 `ip_list` 含 `ipaddr`、`ifname` 和字面端口 `80`，另有 `nginx_installed` 字符串。它是受限查询，不是网页 HTML 入口；外部拒绝不能解释成端口错误。

`openinfo` 使用独立 SDK 认证路径，不能将普通 NAS 账号 token、已有媒体签名或 Portainer 会话直接套用。认证函数 `0x16730d` 的时间差超过 ±43200 秒时可能通过 `0x167258` 调用 `ntp_check_temporary_v1`（`0xb11ca3`），启动原厂 `NtpClient` 尝试校时。因此不把整个调用链称为完全无副作用，也不为探测生成签名、修改时间或重置会话。本轮仅做无令牌响应与静态核验。`PROXY_TARGET`、`web=1` 在此前置链路中的完整作用未确认，不据参数名开放代理功能。

## APP 硬盘信息的云代理入口

用户随后给出的实际路径为 `/rest/1.2/config?action=space_manage&check_warning=1&PROXY_TARGET=<device>`，经海康分配的 HTTPS 云代理访问。本次给定代理端口 `8018` 的证书校验与 TLSv1.3 连接成功；该端口属于这次代理分配，不硬编码为 NAS 本机 API 端口。签名没有写入仓库或日志，也没有发送该业务请求。

固件 handler 确认物理盘序列号键为 `disk_sn`，新空间的自定义名称主要在 `space_nickname`，不能统一取 `space_name`。该查询同时包含设备链接维护、条件性 SMART 状态写入和缺少休眠保护的采集路径，不适合被动监控轮询；`check_warning` 不是关闭这些行为的开关。字段、版本差异与调用证据见[空间管理 REST 审计](vendor-storage-interfaces.md#app-空间管理-restspace_manage)。

## 固件里的 Docker 网页证据

完整 Portainer 前端位于 `/histor/bin/docker/hiksemi.tar`，镜像标识为 `hiksemi:260202`，入口 `/portainer`。镜像声明端口不能代替宿主映射实测，尤其不能将 `8000` 当作普通网页入口。

定向提取了 `public/index.html`、`main.0cd7bccd5479b10594d1.js` 及其 sourcemap，未执行厂商二进制或解包脚本。主包 SHA-256 为 `3a1fb825f20af0f6deef8f5be51112024c2be51152651f5cb7e2c85354b44060`，sourcemap 含 2846 个原始前端源码条目。主要证据为：

- `app/portainer/services/axios.ts`：同源基址 `api`。
- `app/portainer/rest/auth.js`、`services/authentication.js`：`POST /api/auth`，JSON 账号密码；正常登录后读取当前用户。
- `app/portainer/users/user.service.ts`：`GET /api/users/me`。
- `app/react/portainer/system/`：`GET /api/system/status`、`version`、`info`；不是猜测旧版 `/api/status`。
- `app/react/docker/`、`app/portainer/rest/stack.js`：Docker 代理与 Compose 项目调用。

## 可接入 Toolbox 的 Portainer 能力

下表是固件前端确认的合同，除上述版本状态外，本轮未通过认证实测业务调用。环境 ID 和项目 ID 必须从当前授权清单取得，不固定为 `1`。

| 功能 | 前端使用的后端入口 | 接入方式与边界 |
|---|---|---|
| 环境及 Docker 信息 | `GET /api/endpoints`；`GET /api/endpoints/{id}`；`GET /api/endpoints/{id}/docker/info`、`version`、`_ping` | 适合读取可用环境、版本与状态；保留端点授权和字段投影 |
| 容器状态 | `GET .../docker/containers/json`；`GET .../containers/{cid}/json`、`top`、`stats?stream=false`、`logs` | 可扩展健康与应用观测；inspect、环境变量和日志可能含凭据，需要独立范围和脱敏 |
| 容器控制 | `POST .../containers/{cid}/start`、`stop`、`restart`、`pause`、`unpause` | 可封装成固定工具；优先复用 Toolbox 已有启停锁、确认和自身容器保护 |
| 镜像、网络、卷 | `GET .../images/json`、`networks`、`volumes`；`POST .../images/create`、`volumes/create` | 可接清单和有限创建；拉取任务、镜像源认证、卷归属分别处理 |
| 创建与重建容器 | `POST .../containers/create`；`POST /api/docker/{id}/containers/{cid}/recreate` | 先生成部署计划，校验挂载、端口、特权和资源上限，再固定执行；不裸透传 Docker JSON |
| Compose 查询 | `GET /api/stacks`；`GET /api/stacks/{sid}`；`GET /api/stacks/{sid}/file` | 可保留 Portainer 项目管理关系；Compose 原文可能含密码，不直接写入审计或默认响应 |
| Compose 部署 | `POST /api/stacks/create/standalone/string?endpointId={id}`；同类 `file`、`repository` 路由 | 文本部署可作为固定计划执行通道；文件和 Git 认证单独处理，不开放任意地址代理 |
| Compose 更新与启停 | `PUT /api/stacks/{sid}`；`POST .../{sid}/start`、`stop`；`DELETE .../{sid}` | 需要目标项目核验、真实进度、失败恢复与卷保护；升级 Toolbox 复用原项目和数据卷 |
| Agent 卷文件 | `/api/endpoints/{id}/docker/v{agentVersion}/browse/ls`、`get`、`put`、`rename`、`delete` | 依赖 Portainer Agent 模式与版本；仅特定 Docker 卷，不代表 NAS 全文件浏览或上传下载能力 |
| Portainer 账户和 Token | `/api/users`、`teams`、`resource_controls`；`/api/users/{userId}/tokens` | 是独立账户／密钥体系，不能用 Toolbox API Token、qB SID 或海康 access_token 替代 |

前端还支持容器终端、exec、kill 和强制删除。这只是能力存在的证据，不能自动变成 AI 的任意命令或原厂动作代理。管理员角色仍受 Toolbox 功能域、文件根和目标资源限制。

## NAS 文件、相册等业务使用另一套接口

Portainer 主要管理 Docker，不等于海康完整 NAS 管理端。其他业务已确认的原厂切入口如下，静态注册也不代表全部已接入：

| NAS 业务 | 固件后端切入口 | 当前状态 |
|---|---|---|
| 普通文件浏览、上传、下载 | `/rest/1.0/file` 的 `get_list`、`get_info`、`upload`、`download` 等 action | 分派与流式传输已有静态证据；用户目录、ACL、分块／Range 和写入边界尚需逐项实测 |
| 应用市场 | `/rest/1.1/applycenter` | 固定列表显式刷新已接入；安装、卸载、进度副作用分别验证 |
| 下载管理 | `/rest/1.1/dleng`；独立 aria2／qB 协议 | 原厂与独立客户端认证分开；qB 本机身份预检修复后仍需实机验收 |
| 相册、识别、OCR | `/rest/1.1/album`、`fcabm`、`file` | 已有显式媒体首帧查询与脱敏快照；创建相册和分享等写操作待接入 |
| 影视墙 | `/rest/1.1/scraper` | 状态读取与媒体库、字幕、配置写入分别适配 |
| NAS 自有 Docker 项目 | `/rest/1.1/docker` | 与 Portainer 项目使用不同参数、元数据和认证，不混用 stack ID 与原厂项目名 |
| 系统、网络、存储、共享 | `/rest/1.1/config`、`/rest/1.2/config` 等 | 不按 getter 名称推定无副作用；断网、格式化、迁移和权限修改保留专门边界 |

原厂 REST 的完整静态索引见 [emhistor 接口清单](vendor-emhistor-interfaces.md)，业务适配状态见[业务来源](business-diagnostics.md)和 [AI 能力路线](ai-capability-roadmap.md)。现有媒体固定管理员会话、证书 pin 和签名只服务于既定原厂查询，不扩展为任意原厂 action 代理。

## 后续实机核对

正常登录 Portainer 后，先固定读取当前用户、环境和容器列表，确认端点及项目归属，再验证受限的项目读取与更新。不能从可读的版本接口或前端代码直接宣称已能部署容器。

用户随后给出的 `openinfo/get_ip_info` 路径已在上述端口核对；它属于原厂 REST，与 Portainer 不同。当前仍未取得完整 NAS 管理网页的静态客户端入口，后续业务以具体路由、正常授权调用和参数语义逐项核验；不猜端口，不扫描服务。
