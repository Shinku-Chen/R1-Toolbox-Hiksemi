# 固定 Docker 项目读取

本批真实接入 `list_docker_projects`、`get_docker_project_status` 和 `get_docker_project_summary`。文件侧新增 `list_file_roots`，仅返回当前键的根绑定元数据。合计十四项真实 MCP 工具；原有 policy v1／v2 键继续冻结原十项，不能凭预授权自动获得新工具。

## 所有者登记和授权

网页 API 管理先显式发现可信项目候选，再登记已有项目。仅修改 Toolbox 私有注册表，不部署、启停、删除或更新 Docker。所有者接口沿用 Web `password/pair/open`、同源 JSON／CSRF；Bearer 和单独开发白名单不能代管。

| 接口 | 行为 |
|---|---|
| `GET /api/docker/projects/catalog` | 显式发现有完整身份的 Compose 项目候选 |
| `GET /api/docker/projects` | 读取注册表并实时核对项目绑定 |
| `POST /api/docker/projects/register` | 接受 `candidate_id` 和可选 `label`，重新核验后生成 `project_<24hex>` |
| `POST /api/docker/projects/update` | 接受 `project_id,label`，只改 Toolbox 标签 |
| `POST /api/docker/projects/revoke` | 接受 `project_id`，只撤销登记 |

`GET /api/access` 仅加载注册表元数据，不自动连接 Docker。元数据的 `available:null`、`verification:not_checked` 不表示实际项目可用。显式发现、项目状态及摘要才读取引擎；源不可用返回真实原因，不能凭空列表推断没有容器。

新键按 policy v3 选择具体工具、`docker` 域及 `docker_projects:[{id,operations:["inspect"]}]`。最低角色为只读；管理员仍须相同工具和资源授权。机器不能提交真实项目名、配置路径、Compose、原厂 action、命令或 URL；只能提交后端分配的项目 ID。

原十工具仍受各自角色和域限制。旧键只有所有者明确选择逐工具授权才升级；纯改名、IP、有效期和轮换保留原策略。真实权限变化推进授权修订，轮换推进凭据代际。新读取回调在取数据前后复验当前键，读取期间暂停、撤销、轮换、到期或改范围后丢弃尚未发送的结果。

## 来源和绑定

读取固定本机 `/var/run/docker.sock`，仅允许固定 `/info`、容器列表及完整十六进制容器 ID 的 inspect GET。不使用 shell、任意代理或原厂 `project_list`。后者存在自动 `down` 分支，禁止用于只读发现。

- 核验 socket peer 为 root dockerd，绑定 PID／启动时刻／二进制身份；请求前后及整个批次校验来源，结合 `/info` 引擎 ID。
- 整批共用八秒绝对期限；最多 128 个容器，单体 JSON 最多 1 MiB、累计最多 8 MiB，头部和分块有独立上限。重复键、异常帧、身份变化和超限均拒绝，使用非阻塞来源锁。
- 候选由固定 Compose 标签交叉核对：项目名、引擎、单个配置来源、服务／容器 ID 和配置 hash。不完整、重建、多来源、重复服务或配置身份变化不能自动绑定同名项目。
- 配置只用 Linux `O_PATH` 和无符号链接的目录遍历验证元数据，绑定 dev／inode／大小／mtime／ctime，不读正文。其他平台或证据不足时保持不可用。
- 注册表私有保存绑定元数据，原子写入并限制大小，不存 Compose、环境变量、认证或业务数据。写失败、损坏、符号链接和非法权限保持关闭。

项目及 API 管理使用自己的锁；引擎读取不占风扇、灯光和电源的控制锁。

## 输出含义与限制

运行和健康状态来自真实引擎，未知保持 `null`；`native_status/pop_status` 同样为 `null`，不冒充原厂 App 内存状态。

摘要只包含服务 ID／名称、可核验镜像引用、端口、挂载类型与读写属性、重启策略和固定风险标记。不输出环境、命令、日志、宿主路径、容器内私有路径、任意标签或 Compose 原文。镜像或配置无法确认的字段保持 `null`。`unresolved_configuration` 明确指出未解析 Compose 全部语义，不能把当前容器集合说成已证明的完整项目配置。

本批不注册项目更新、文件浏览／下载或写入工具。定义目录中的 `execution_allowed:false` 只说明“读取合同不授予执行权”，逐项 `implemented` 标注四项已接入与十六项待接入。MCP 是否可调用以当前 Token 的能力目录及实际授权结果为准。

## 开发和只读实机核对

有效开发窗口和真实来源白名单下，固定 `/api/dev/diagnostics` 可显式选择 `docker_project_readiness` 与 `file_access_readiness`；不需要机器 Token，也不能借此登记项目或管理密钥。前者只发现可信候选并读取注册表元数据，后者逐项列根规划、账号映射、ACL 与安全打开的缺失证明。默认四项被动检查不扩展，过期后丢弃结果。

自动化使用隔离 Unix HTTP fixture，证明协议和权限边界，不代表 NAS 实机通过。部署复用原项目及 `r1-toolbox-data`，按[只读验收清单](file-docker-readonly-acceptance.md)核对；当前没有用户指定的样本文件和非 Toolbox 项目，不能为验收创建业务服务或读取私人文件。
