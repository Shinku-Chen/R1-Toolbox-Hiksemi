# 固定 Docker 项目读取与授权

新增资源读数的字段与单次采样参数可对照 [Docker Engine API 的 ContainerStats](https://docs.docker.com/reference/api/engine/version/v1.51/#operation/ContainerStats)；内存缓存的 cgroup 口径见 [Docker stats 文档](https://docs.docker.com/reference/cli/docker/container/stats/)。Toolbox 保留原始计数及来源，不用缺失差分补造测量值。

读取工具为 `list_docker_projects`、`get_docker_project_status`、`get_docker_project_summary` 和 `get_docker_project_resource_usage`。另有管理员启停／重启、镜像更新规划与执行，以及本键任务查询，见[固定项目操作](docker-project-operations.md)。工具总数以当前服务目录为准，可见与可调用集合按 Token 精确过滤；policy v1／v2 继续冻结原十项，不自动扩权。

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

读取固定本机 `/var/run/docker.sock`。原有 GET 白名单仍仅 `/info`、固定容器列表与完整 ID 的 inspect；资源采集器在专用分支额外允许完整已登记 CID 的字面 `/stats?stream=false&one-shot=true`。不接收容器 ID、URL 或查询参数，不使用 shell、任意代理、日志或原厂 `project_list`；后者存在自动 `down` 分支，禁止只读发现使用。

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

二十项文件／Docker 合同的标记以 `runtime/api_contracts.py` 为准，资源用量工具不额外制造定义或假回调。定义目录 `execution_allowed:false` 说明查阅不授予执行权；可调用仍取决于当前工具与资源授权。只读统计不证明更新通过，更新的任务、冲突保留和执行保护见[执行器说明](docker-project-operations.md)。

## 已登记项目的单次资源用量

工具 `get_docker_project_resource_usage` 只接受 `{"project_id":"project_<24hex>"}`，最低只读、`docker` 域、精确工具及对应项目 `inspect` 同时满足。管理员也不能查询未登记项目；新工具不会自动加入旧 Token。内部调用为：

```python
registry.get_resource_usage(principal, project_id, authorizer=integration._is_current)
```

`authorizer` 是可信 Python 回调，不是 JSON 参数。初次读取、每次连接后发送前、每次响应后和最终输出前复核当前键及原登记修订；所有者在读期间可撤销登记，采集不长时间持有登记锁。授权失效丢弃整次结果。共享 `_READ_LOCK`、八秒绝对窗口、1 MiB 单响应和 8 MiB 总上限；最多 32 服务、每服务 64 接口、1024 条块 I/O 行／128 个设备，公开结果最多 128 KiB。超时或超限失败，不改为后台流、不隐式重试。

每个已登记服务只发一次固定 `GET /containers/<已核验 CID>/stats?stream=false&one-shot=true`。前后核对 Engine／dockerd peer、配置身份、项目成员、服务／容器 ID、镜像 ID、启动时刻、PID 和重启计数，整批结束再核对所有服务。响应 ID 或名称不匹配、重建／重启、成员或来源改变均拒绝；不会按同名新容器换绑。有效采样时间早于当前启动的旧帧同样拒绝。

返回 `source:engine_stats`、`consistency:per_service_observation` 和独立 `services`，不聚合项目总量。`observed_at` 是 Toolbox 完成查询时间，`read_at` 是 Docker 帧时间；不同服务并非原子同时采样。服务只返回已授权的服务 ID／名称、实际运行标志及以下白名单：

| 字段 | 语义与未知值 |
| --- | --- |
| `cpu.usage_percent` | 仅用同一 Docker 响应里的 `cpu_stats/precpu_stats` 实际两帧差分：容器 CPU 增量／系统 CPU 增量 × 合法 CPU 数 × 100。100% 表示一个逻辑 CPU，可超过 100%；不使用 CPU 配额或虚构样本。须有效前后帧时间、同一启动之后、非倒退计数及稳定合法 CPU 数；缺帧、冷启动零前帧、异常值、停止状态为 null，真实零增量才为 0 |
| `cpu.online_cpus` | Docker `online_cpus` 的合法正整数；旧 daemon 缺失时才用已验证 `percpu_usage` 数组长度。cgroup v2 可不提供逐核数组；不从宿主或限额猜数量 |
| `cpu.total_usage_ns/system_usage_ns` | Docker 原始累计 CPU 计数，不是即时速率；不是安全整数的值为 null |
| `memory.usage_bytes/limit_bytes` | Docker 原始使用／上限，使用量包含其内核计费的缓存；limit 不冒充整机容量、剩余内存或已证明的无限额 |
| `memory.cache_bytes/cache_source` | v1 原始 `stats.cache`，缺失时可用 v2 原始 `stats.file`，同时明确字段来源；不推断全部可回收，不倒算工作集 |
| `memory.inactive_file_bytes/inactive_file_source` | 独立投影 v1 `total_inactive_file` 或 v2 `inactive_file`；不与 cache 重复相加，也不从 usage 减去后伪造另一读数 |
| `network.interfaces` | 各接口各自的收发字节、包、错误和丢弃累计计数。不累加接口，不将单帧计数冒充速率或物理网口总流量；缺失为 null，明确空列表与计数零保持原样 |
| `block_io.devices` | `io_service_bytes_recursive` 按 major／minor 分设备保留 Read／Write 字节计数；忽略 Total／Sync 等重叠行，不重复加总。重复同设备同方向拒绝。不同设备可能为叠加层，不求和、不当逐盘归因；未提供为 null |

计数仅接受非负安全整数，布尔、负值、字符串和超范围值保持 null，不补零。原始帧、容器 ID、环境、命令、标签、宿主路径和错误正文不出宿主。该接口给 AI 提供容器观察值；整机余量仍须结合独立的 NAS 监控，不能直接相加容器限制计算。

协议语义对照 [Docker Engine v1.45 官方定义](https://raw.githubusercontent.com/moby/moby/v26.1.5/docs/api/v1.45.yaml)。单次 `one-shot` 不等待第二个采集周期，因此实际 daemon 可能没有可用前帧，此时 CPU 百分比保持 null；不为填百分比额外发第二次请求。

验证：`python -B -m unittest discover -s tests -p test_docker_project_resource_usage.py -v`。隔离真实 HTTP 及 Linux AF_UNIX／peer credential fixture 覆盖字段投影、cgroup v1／v2、前帧缺失、重启／容器替换、授权与登记撤销、超限／错误及无聚合。fixture 不替代 NAS；实机仅对所有者已有登记项目做只读比对，不启动容器、任务或部署。

## 开发和只读实机核对

有效开发窗口和真实来源白名单下，固定 `/api/dev/diagnostics` 可显式选择 `docker_project_readiness`、`file_access_readiness` 或 `file_backend_context`；不需要机器 Token，也不能借此登记项目或管理密钥。前两项分别检查可信项目候选、注册表及文件访问就绪状态；最后一项补充固定管理员逻辑身份、Unix 映射候选与 Users 目录句柄元数据，不列目录、不读用户内容，仍可能唤盘。默认四项被动检查不扩展，过期后丢弃结果。

自动化使用隔离 Unix／HTTP fixture，证明协议和权限边界，不代表新增能力已在 NAS 实机通过。部署复用原项目及 `r1-toolbox-data`，先按[只读验收清单](file-docker-readonly-acceptance.md)核对。没有用户指定的样本文件和非 Toolbox 项目时，不为验收创建业务服务或读取私人文件；启停与更新须另有明确对象和授权，不能用只读验收替代。
