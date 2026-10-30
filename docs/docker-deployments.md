# 受控 Docker 新服务部署

新的部署资源与已有 Compose 项目登记分开。`project_*` 仍只代表已存在且身份完整的项目；`deployment_*` 代表所有者明确登记的未来部署合同，不能相互替代。接口直接使用已核验的本机 Unix Engine API，不执行 Compose、shell、容器内 `exec` 或任意命令。

## 所有者登记

网页授权、同源、JSON 和 CSRF 校验后的所有者可使用以下路由；机器 Token 和开发白名单不能登记或扩大自己的资源。

| 路由 | 内容 |
| --- | --- |
| `GET /api/docker/deployments` | 仅读取持久登记元数据，不自动探测 Docker。 |
| `GET /api/docker/deployments/catalog` | 显式读取既有本地卷和网络候选，返回资源名，不返回宿主路径。 |
| `POST /api/docker/deployments/register` | 严格登记一个未来服务合同，不创建容器、卷或网络。 |
| `POST /api/docker/deployments/refresh` | `{"deployment_id":"deployment_…","confirm":true}`，所有者显式重核同一固定合同及既有容器，更新生命周期证据；不是机器续权接口。 |
| `POST /api/docker/deployments/revoke` | `{"deployment_id":"deployment_…"}`，撤销后新阶段停止，已有容器与卷保留。 |

登记正文示例：

```json
{
  "label": "测试服务",
  "spec": {
    "name": "test-service",
    "image_repository": "example/service",
    "network": "existing-test-network",
    "volumes": [{"name": "existing-test-data", "target": "/data", "readonly": false}],
    "ports": [{"host_ip": "192.168.31.5", "published": 18080, "target": 8080, "protocol": "tcp"}],
    "user": "1000:1000",
    "memory_bytes": 67108864,
    "nano_cpus": 500000000
  }
}
```

以上值只是格式示例，不代表 NAS 已存在对应镜像、卷或网络。`spec` 和各子对象拒绝未列字段。`name` 使用现有项目名字符范围，最多 64 字符。`image_repository` 是不含 tag 或 digest 的精确仓库名；机器调用必须再指定该仓库的 `@sha256:<64hex>`。资源合同只接受：

- 已有 `local` 且无驱动选项的具名卷；记录 Engine 元数据和真实目录的设备、inode、挂载身份，复核后再使用。禁止 bind、匿名卷、卷驱动参数和 Toolbox 数据卷。保护同时识别精确短名、Compose 逻辑卷标签以及当前 Toolbox 容器实际挂载的具名卷，避免项目名前缀或自定义卷名绕过；不按模糊子串误伤其他卷。目录身份检查只读元数据，可能唤醒卷所在磁盘。
- 已有本机 `bridge` 网络且没有额外驱动选项，或 `none`。禁止 host、容器共享网络与 swarm 网络。`none` 不能发布端口。
- 具体 RFC1918、ULA 或回环 IP 的固定端口；禁止通配 IP、公网 IP、动态端口和在机器请求中更换目标。
- 非零数值 UID 与 GID，范围为 1 至 2147483647；内存 16 MiB 至 16 GiB，CPU 以纳核表示，范围为 10000000 至 8000000000。

创建固定开启只读根文件系统、`CapDrop=ALL`、`no-new-privileges`、256 PID 限额、精确内存／CPU 限额和 `unless-stopped` 重启策略。不接受环境变量、命令、入口、设备、额外权限或任意 Engine JSON。镜像自身声明 `VOLUME` 时拒绝创建，避免隐式生成匿名卷。部分原生镜像不支持非 root 或只读根文件系统，会真实报告失败；不得宣传为可运行任意 Compose 应用。

具名卷是所有者独立授予容器的资源，不因文件授权根为 `/drives/*/Users/admin/` 就声称容器被文件访问沙箱隔离。任何获得卷读写能力的镜像必须按该委托范围理解。

## Token 和工具

新键 policy v3 通过独立字段授予资源：

```json
{
  "role": "admin",
  "scopes": ["docker"],
  "tool_grants": ["list_docker_deployments", "plan_docker_deployment", "execute_docker_deployment", "get_docker_deployment_task"],
  "docker_deployments": [{"id": "deployment_0123456789abcdef01234567", "operations": ["inspect", "deploy", "control", "remove"]}]
}
```

| 工具 | 最低角色 | 参数与资源操作 |
| --- | --- | --- |
| `list_docker_deployments` | 只读 | 无参数；只返回有 `inspect` 授权的既有登记元数据。 |
| `plan_docker_deployment` | 管理员 | `deployment_id`、`operation`，以及部署／更新时的 `image_reference`。 |
| `execute_docker_deployment` | 管理员 | `plan_id`、严格布尔 `confirmed:true`、`idempotency_key`；还必须有规划工具授权。 |
| `get_docker_deployment_task` | 只读 | `deployment_id`、`task_id`；须有 `inspect`，任务绑定当前键、凭据代次及授权修订。 |

`operation=deploy/update` 使用 `deploy` 资源操作；`start/stop/restart` 使用 `control`；`remove` 使用 `remove`。`image_reference` 仅能用于 `deploy/update`，其余操作禁止此字段。`remove` 的目标必须已停止；删除始终使用 `v=0&force=0`，不删卷、不强制删除正在运行的服务。

管理员仍须域、精确工具、具体资源和对应操作同时获权。旧 Token 不增加字段或能力。规划有效 120 秒，进程重启失效。所有 Docker 项目／未来部署共用任务数量限制、凭据绑定的幂等记录和后台任务集合，同一幂等键不能另作他用。

NAS 或 dockerd 重启后，旧启动时刻／挂载生命周期不能直接继承。所有者须显式使用 `refresh`，重新核验原有精确 spec、同一 Engine、容器 ID、镜像、卷、端口和网络后再委托；不能收编其他容器或 Engine。刷新递增登记修订，使旧计划失效，Token 的域、角色与资源操作保持不变。GET 与机器调用不会自动刷新。

## 执行与故障语义

规划只读检查登记合同、Engine、peer、网络、具名卷、容器名及既有容器。执行先返回真实任务 ID；每个写请求连接完成后、发送前复核当前授权、所有者登记和来源身份，并受 120 秒绝对任务时限约束。

初次部署显式拉取固定 digest，核验实际 image ID 与 repo digest，创建后检查真实容器设置、卷和网络，再保存准确容器 ID，最后启动并查询真实状态。配置和 ACK 不能证明服务已就绪。健康检查处于 `starting` 时最多观察 30 秒，`unhealthy`、超时或无法确认会报告 `result_unconfirmed`。

更新只允许同一合同仓库的其他 digest。停止旧容器后，将其更名为 `服务名-r1-old-<任务短码>` 保留，创建并启动替换容器。新容器健康已确认，且旧容器身份及停止状态重新核对后，才删除旧容器，始终保留具名卷。完成前失败将保留实际服务和备份，任务标记 `recovery_required:true`；不自动回滚，不把未知结果当作失败重放。

撤销 Token、轮换、过期、关闭 MCP 或撤销登记都会阻止下一阶段写入。已经接收的拉取、创建、停止或删除可能已生效，任务保留真实未知终态；撤权后的读取不返回旧结果。进程重启后不恢复未完成任务，也不自动重复操作。没有 ACK 的创建即使未保存容器 ID，也会因容器名冲突阻止再次部署，需所有者核对实际状态。

## 验证边界

`tests/test_api_docker_deployment.py` 使用隔离的真实 HTTP 传输和 Linux Unix socket／peer credential fixture，验证部署、更新、启停、删除保留卷、精确权限、资源替换、连接后撤权、幂等、期限与故障保留。Linux CI 必须执行实际目录句柄和 Unix socket 检查；Windows 跳过对应测试。

fixture 不能代替 NAS 实机验收。实机先读取登记和候选资源，只能使用所有者另行指定的专用测试资源验证写操作；不得把现有下载器、媒体应用或 Toolbox 当作测试项目。

固定传输参数对照 [Docker Engine API v1.45 官方定义](https://raw.githubusercontent.com/moby/moby/v26.1.5/docs/api/v1.45.yaml)：`fromImage` 可携带 digest，具名卷可复用，`NoCopy` 禁止镜像内容初始化卷，容器删除不请求删除具名卷。当前传输继续兼容仓库既有无版本前缀的本机 Engine 路径；不在此批改写旧项目读取协议，实际 daemon 差异须由实机核对。
