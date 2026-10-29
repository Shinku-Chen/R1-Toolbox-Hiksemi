# 固定 Docker 项目操作执行器

`runtime/api_docker_operations.py` 提供真实 Docker Engine Unix HTTP 执行器，无 shell、`exec`、Compose 子进程或任意原厂动作代理。它只操作所有者已经登记且当前身份仍匹配的项目；MCP／REST 的工具注册和 Token 管理由集成层完成。

## 集成接口

构造 `DockerProjectOperations(registry, authorizer=integration._is_current)`，`registry` 为当前服务已有 `DockerProjectRegistry`，回调同时核对服务关闭状态、当前服务代际与 Token 当前授权。服务停止调用 `close()`，阻止后台任务后续副作用，Web 重新启动也不恢复旧调用；MCP 关闭／重开通过 Token 授权修订失效，旧任务不会复活。

| 工具 | 内部方法 | 必需项目 operation |
|---|---|---|
| `control_docker_project` | `control(principal, project_id, operation, confirmed, idempotency_key)`；操作仅 `start/stop/restart` | `control` |
| `plan_docker_project_update` | `plan_update(principal, project_id, service_id, image_reference)` | `update_image` |
| `execute_docker_project_update` | `execute_update(principal, plan_id, confirmed, idempotency_key)` | 原计划项目的 `update_image`，且仍有规划工具授权 |
| `get_docker_project_task` | `get_task(principal, project_id, task_id)` | `inspect`，同键、凭据代际、授权修订和项目 |

三个修改入口要求 policy v3、管理员、`docker` 域、精确工具及对应项目 operation；管理员不绕过范围。执行必须 `confirmed=true`。所有入口、关键副作用发送前和结果披露前都调用 `authorizer(principal)`；授权撤销、轮换、到期或修订变化立即阻止后续动作，已取得的未发送结果丢弃。

现有读取器兼容管理员 Token 同时带 `inspect/control/update_image`，读取仍要求 `inspect`；缺少该项、旧 policy 或低角色不能借新 operation 获得读取权限。
执行器本身不管理 Token，也不把开发者窗口或网页登录会话当成机器授权。

## 固定传输与保护

- 复用固定 `/var/run/docker.sock`、root dockerd peer 的 PID／启动时刻／二进制身份校验和 `/info` 引擎 ID；连接前后检查，不使用 TCP、代理、DNS 或网页指定的 socket。
- 仅允许完整 CID 的 inspect、start、固定十秒 stop/restart、内部生成名称的 create/rename，以及固定 `v=0&force=0` 删除已停旧容器。不存在 exec、kill、任意删除、卷删除或 `down -v`。
- 镜像仅允许同一原仓库的 `@sha256:` 引用，固定镜像 inspect 与按 digest 拉取；不接受标签更新、其它仓库、导入 URL、构建上下文或认证参数，不读取其它凭据文件。
- 单个 JSON／拉取进度流最多 1 MiB，累计最多 8 MiB；头部、行、分块和条目有界。一次工作共用 120 秒绝对期限，内部项目快照仍至多八秒且受剩余工作时间限制。不转发 daemon 错误正文、请求头或进度原文。
- 项目名、服务名、镜像身份和 `r1-toolbox-data` 卷共同保护 Toolbox。镜像更新拒绝特权、宿主／其它容器 namespace、设备、cap-add、不受约束安全策略、控制 socket、未经现有挂载交叉确认的 HostConfig。所有 bind 项目本轮拒绝更新：旧容器 inspect 的来源字符串不能证明重建时仍是同一 inode／挂载对象。卷必须复用原有命名卷，绝不删除卷。
- 创建新容器时保留当前已核验的配置、环境、命令、端口、重启策略、现有卷和网络别名；这些内容仅在宿主内存及固定 daemon 请求中使用，不进入工具结果、计划公开字段或审计。新镜像不得增加未预先存在的 VOLUME 目标；静态 IP 分配等尚未闭合的网络语义拒绝。

## 更新计划与配置边界

只接受已登记的单服务、单配置来源。配置文件通过无符号链接的元数据遍历和安全文件句柄核验，绑定设备、inode、大小、mtime、ctime；文件必须普通、单硬链接且非组／其他用户可写。再次读取原文仅供本次显式更新审计，原文不离开宿主。

支持 JSON Compose 和受限的常规 block YAML：恰好一个服务、一个 literal `image` 字段；JSON 只改该服务的镜像，YAML 保留其它字节并只替换已审定的 image 行。不依赖宿主安装 PyYAML，不解释标签、锚点、合并、插值、include、build、多文档或多服务图。不能证明的配置返回 `write_backend_unverified`，不会调用任意 Compose 作为兜底。

计划公开字段沿用已有合同：计划 ID、项目／服务 ID、当前／目标镜像与 digest、镜像单项变更、确认要求和绝对到期时间。计划有效期为 120 秒；服务重启使全部内存计划失效。规划不拉取镜像、不创建容器、不改配置。

执行顺序如下：

1. 核对计划的同键、凭据代际、授权修订、到期和未消费状态；持有同一服务进程各客户端共享的项目操作锁。
2. 原子消费计划并保存任务和幂等关联，立即返回真实任务 ID，由后台执行剩余步骤。再进行可能产生副作用的 digest 拉取，核对镜像 `RepoDigests`、真实 image ID 和 RootFS layer digest 格式；这是受信任 daemon 的镜像存储证据，不冒充独立逐字节重算镜像层。
3. 创建已停的 staging 容器；每步复查引擎、原 CID／名称／配置／HostConfig／Mounts／稳定网络配置指纹、项目完整成员、注册表修订、配置文件身份，以及 staging 容器自身身份和停止状态。网络连接／断开、别名、静态配置变化使计划失效，临时 IP／Endpoint 计数不冒充配置变化。新容器的实际既有卷与网络配置也须匹配。
4. 在已打开的真实父目录句柄内写临时文件并 fsync，使用 Linux `renameat2(RENAME_EXCHANGE)` 原子交换两个条目，保留权限和所有者。缺少该原语时规划拒绝，不使用无条件 `replace` 冒充 CAS。交换后验证被换出的原文件 inode、完整有界字节和其它稳定身份；若最后瞬间被第三方改写／替换，双方版本都保留，任务为 `result_unconfirmed`，不继续停止原容器、不盲目恢复。目录符号链接／重命名竞态不能把写入重定向到另一目录。
5. 若原服务运行，固定 stop 原 CID；将已停旧容器改为内部备份名，替换容器恢复原名，并只按原运行意图启动。
6. 核对新容器真实 image ID、固定目标引用和运行状态；`starting` 在共享工作期限内至多等待三十秒变为 `healthy`，每轮复验授权，不重复 start。`unhealthy`／超时保留未知终态。确认健康后才删除已停旧容器，严格不删卷。最后重新读取真实项目，用内部 CAS 将原登记 ID 接续到新 CID 和新配置身份；不会把同名其它项目自动纳入旧授权。

原容器的可写层随正常镜像替换被替换，数据持久化仍依靠既有卷。故障或撤权时不盲目回滚、重启或删除业务对象；保留已发生的实际状态、旧容器／备份，任务标为 `result_unconfirmed`，由所有者先核对现场。配置目录中的 `.r1-image-*` 是原权限／所有者的私有恢复材料：成功交换也保留旧文件，失败可能保留待安装文件或第三方版本，工具不披露路径／正文、不自动删除或声称已回滚。所有者应在核对当前配置与 daemon 后清理或恢复；重启不自动处理它们。

## 幂等、任务与重启

计划、任务和幂等关联仅在当前进程有界保存（最多 128 项），不写入原厂或额外恢复任务。同一键／代际／修订的相同幂等标识返回原任务；不同请求复用标识为 `idempotency_conflict`。控制与更新共用原子任务登记，跨项目并发也只有一个请求能占用同一标识，不先发副作用再登记。已消费或结果未知的计划不能再次执行；传输超时不等于操作未发生。

任务仅返回合同规定的 `pending/running/completed/failed/conflict/result_unconfirmed`、真实运行状态、已核验的目标 digest／配置 digest／image ID、更新时间和固定错误，不制造百分比。更新提交在任何 daemon 查询前返回 `pending/running` 的真实任务 ID，后台使用同一项目锁执行，最多同时八项、单项共用一百二十秒期限；可立即调用任务查询。相同幂等标识始终取回同一任务，不再开线程，不能换新标识重试旧计划。固定启停／重启仍为有界同步操作。
重启后计划／任务查询不可用，不推断上次成功，也不自动继续中断更新。对已运行对象造成的副作用不会因任务记录消失而被抹去，所有者须先核对 daemon／配置现场。

## 验证和未接入范围

`tests/test_api_docker_operations.py` 使用隔离的真实 HTTP 服务，没有连接 NAS 或宿主 Docker。可移植用例通过真实 loopback HTTP 验证协议、授权、计划、拉取、后台阻塞时早返回／同键幂等／服务停止栅栏、健康转移和生命周期；其中 Windows 更新测试显式使用 fixture 文件适配器，不把它当作 Linux 文件安全证明。Linux 用例改用真实 AF_UNIX、SO_PEERCRED 和生产 O_PATH／原子交换流程，注入交换前第三方改写；独立审阅 `tests/test_docker_update_safety_review.py` 再覆盖第三方替换的字节保留。Windows 明确跳过这些 Linux 用例。另有实际非 dockerd peer 的拒绝校验。

本轮执行器不提供任意新建项目部署。新的结构化部署需要所有者先登记独立的目标项目、已有卷／端口／网络等资源，再形成可授权的创建范围；当前项目登记合同要求真实既有容器，不能把虚构项目 ID 或不受限 Compose 当成这种授权。尚未闭合的资源创建、复杂编排、私有仓库认证、静态 IP 与多服务更新没有占位回调。
自动化通过不代表 NAS 上实际操作已验收；本轮禁止以实机启停、更新或新建业务做验证。

固定 HTTP 动作依据 [Docker Engine API](https://docs.docker.com/reference/api/engine/version/v1.51/) 的容器与镜像端点；Toolbox 仅使用本页列出的受限子集。
