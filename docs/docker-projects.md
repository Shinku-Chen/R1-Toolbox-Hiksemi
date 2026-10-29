# 固定 Docker 项目读取与授权

读取工具为 `list_docker_projects`、`get_docker_project_status` 和 `get_docker_project_summary`。另已注册管理员启停／重启、镜像更新规划与执行，以及本键任务查询，具体执行边界见[固定项目操作](docker-project-operations.md)。当前全量 MCP 目录共 36 项，实际可见和可调用集合始终按 Token 过滤；原有 policy v1／v2 键继续冻结原十项，不自动扩权。

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

新键按 policy v3 选择具体工具、`docker` 域和逐项目操作：`inspect` 用于读取，最低角色为只读；`control` 用于已登记项目启停／重启，`update_image` 用于受控镜像更新，后两项同时要求管理员及对应具体工具。网页按项目展示三种操作，新增默认不选，降级时清除不满足等级的操作；管理员仍不能绕过资源范围。

机器不能提交真实项目名、配置路径、Compose、原厂 action、命令或任意 URL；项目只能通过后端分配的 ID 选择。更新规划额外接受已授权服务 ID 与受限镜像引用，不等于开放任意配置编辑。所有者登记本身只改 Toolbox 元数据，不执行这些机器操作。

原十工具仍受各自角色和域限制。旧键只有所有者明确选择逐工具授权才升级；纯改名、IP、有效期和轮换保留原策略。真实权限变化推进授权修订，轮换推进凭据代际。新读取回调在取数据前后复验当前键，读取期间暂停、撤销、轮换、到期或改范围后丢弃尚未发送的结果。

## 来源和绑定

读取固定本机 `/var/run/docker.sock`，仅允许固定 `/info`、容器列表及完整十六进制容器 ID 的 inspect GET。不使用 shell、任意代理或原厂 `project_list`。后者存在自动 `down` 分支，禁止用于只读发现。

- 核验 socket peer 为 root dockerd，绑定 PID／启动时刻／二进制身份；请求前后及整个批次校验来源，结合 `/info` 引擎 ID。
- 整批共用八秒绝对期限；最多 128 个容器，单体 JSON 最多 1 MiB、累计最多 8 MiB，头部和分块有独立上限。重复键、异常帧、身份变化和超限均拒绝，使用非阻塞来源锁。
- 候选由固定 Compose 标签交叉核对：项目名、引擎、单个配置来源、服务／容器 ID 和配置 hash。不完整、重建、多来源、重复服务或配置身份变化不能自动绑定同名项目。
- 配置只用 Linux `O_PATH` 和无符号链接的目录遍历验证元数据，绑定 dev／inode／大小／mtime／ctime，不读正文。其他平台或证据不足时保持不可用。
- 注册表私有保存绑定元数据，原子写入并限制大小，不存 Compose、环境变量、认证或业务数据。写失败、损坏、符号链接和非法权限保持关闭。

项目及 API 管理使用自己的锁；引擎读取不占风扇、灯光和电源的控制锁。

配置身份核验包含文件系统元数据访问，可能唤醒休眠磁盘；这些读取只在显式发现／查询时进行，不加入后台轮询。文件根规划只使用原有缓存和挂载信息，两者不能混称为同一种采集。

## 输出含义与限制

运行和健康状态来自真实引擎，未知保持 `null`；`native_status/pop_status` 同样为 `null`，不冒充原厂 App 内存状态。

摘要只包含服务 ID／名称、可核验镜像引用、端口、挂载类型与读写属性、重启策略和固定风险标记。不输出环境、命令、日志、宿主路径、容器内私有路径、任意标签或 Compose 原文。镜像或配置无法确认的字段保持 `null`。`unresolved_configuration` 明确指出未解析 Compose 全部语义，不能把当前容器集合说成已证明的完整项目配置。

二十项文件／Docker 合同中，文件根元数据、三个项目读取及三个更新／任务合同已有实现，共七项；十三项文件内容合同尚未实现，不注册浏览、下载或写入假工具。项目启停／重启另有独立工具。定义目录中的 `execution_allowed:false` 说明读取合同不授予执行权；是否可调用仍以当前 Token 的能力目录及实际授权结果为准。更新的异步任务、冲突保留和安全执行要求统一见[执行器说明](docker-project-operations.md)，不由本页读取结果证明更新通过。

## 开发和只读实机核对

有效开发窗口和真实来源白名单下，固定 `/api/dev/diagnostics` 可显式选择 `docker_project_readiness`、`file_access_readiness` 或 `file_backend_context`；不需要机器 Token，也不能借此登记项目或管理密钥。前两项分别检查可信项目候选、注册表及文件访问就绪状态；最后一项补充固定管理员逻辑身份、Unix 映射候选与 Users 目录句柄元数据，不列目录、不读用户内容，仍可能唤盘。默认四项被动检查不扩展，过期后丢弃结果。

自动化使用隔离 Unix／HTTP fixture，证明协议和权限边界，不代表新增能力已在 NAS 实机通过。部署复用原项目及 `r1-toolbox-data`，先按[只读验收清单](file-docker-readonly-acceptance.md)核对。没有用户指定的样本文件和非 Toolbox 项目时，不为验收创建业务服务或读取私人文件；启停与更新须另有明确对象和授权，不能用只读验收替代。
