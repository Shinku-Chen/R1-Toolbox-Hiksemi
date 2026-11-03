# 文件与 Docker 只读实机核对

本轮交付逐工具授权、文件根元数据和固定 Docker 项目读取，不执行上传、改名、复制、移动、删除、安装、部署、启停或更新。定义、根规划、实际项目状态和文件内容分别验收，不能互相代替；本批尚未做 NAS 实机验收。

## 一．定义与授权回归

使用新镜像的原项目和原 `r1_toolbox_data` 卷。容器启动、Web 可用、服务开关和旧密钥保持原值；无需为了查看定义开启 MCP。

1. 所有者在 Web 登录后打开“API 管理 → 文件与 Docker 接口定义”，读取 `/api/access/contracts`。确认 `schema_version:1`、`status:definitions_only`、四项 `implemented:true` 与十六项待接入，输入／输出和资源条件齐全。定义本身的 `execution_allowed:false` 不代表已接入工具不能在另外的机器通道获授权调用。
2. 最低角色分布为只读十项、读写八项、管理员两项；管理员更新执行仍要求项目授权、绑定计划、幂等键和确认。
3. 单独 Bearer、开发白名单和小屏控制令牌不能读取所有者定义。带 Bearer 的所有者会话也拒绝；参数、片段和异源请求拒绝。
4. 复用已有 policy v1／v2 Token 查询 MCP `tools/list` 或 `/api/integration/capabilities`，确认仍只有原授权范围内的十项工具。只有所有者显式启用逐工具授权并选择新工具与资源，才进入 policy v3；修改名字、IP 或轮换不能自动扩权。仅查看定义无需创建或轮换密钥。

定义成功只说明合同可读取，不说明文件或 Docker 新工具可执行。

## 二．已有元数据预检

开发窗口必须有效，且真实来源命中非空 IP／CIDR 白名单。保持同源 JSON 和 `X-FNM-Request: 1`，使用现有固定入口：

```http
POST /api/dev/diagnostics
Content-Type: application/json
X-FNM-Request: 1
Origin: http://NAS_IP:18732

{"action":"catalog"}
```

再显式选择设备、存储及新增就绪证据，均复用同一固定入口：

```json
{"action":"snapshot","checks":["device","storage","file_access_readiness","docker_project_readiness"]}
```

核对返回 `version`、真实 `access.client_ip`、有效窗口及逐项结果。设备名称、自定义盘名和身份冲突保持真实值；缺失不补零、不重试 SMART、不唤盘。无需 `/api/dev/storage/probe`、媒体探测或业务刷新。

所有者 `/api/access` 的 `file_root_catalog` 可核对根 ID、盘名、挂载和只读属性，只作为授权规划。文件就绪诊断分别列账号映射、ACL 与最终对象绑定等未验证项，不能提升为已通过。Docker 就绪诊断显式读取可信候选和注册表元数据，不登记项目、不读取 Compose、不部署；默认四项被动检查不包含这两项新检查。开发窗口在读取期间过期时丢弃结果。

所有者在 API 管理中按可信候选登记已有项目，给短期只读 policy v3 Token 勾选三个项目读取工具和该项目 `inspect`。核对实际项目列表、运行／健康状态和脱敏服务摘要；原厂状态为空应保持 `null`，不能按引擎结果猜填。项目重建、改配置身份或撤销登记后须失效，不能按同名换绑。登记和 Token 配置仅保存 Toolbox 授权元数据，不执行 Docker 部署动作。

## 三．真实后端就绪后的读取验收

当前尚未指定实际样本目录、文件及非 Toolbox 测试项目，不能替用户选私人文件，也不能为验收创建服务。未选资源记录 `not_selected`。Token policy v3 和项目登记已经实现；真实文件 ACL 与安全打开仍未闭合，文件浏览／属性／下载保留 `backend_unverified`，不得算通过。

后端完成后，使用所有者明确选择的根 ID、子目录、已知小文件及已登记项目；只读 Token 须逐工具和资源授权：

- 目录分页、文件属性和普通文件下载；本地样本 SHA-256 比对，空文件、单 Range、HEAD、中断、撤权和句柄失效按接口合同核对。文件实际访问可能唤醒硬盘，应列为显式访问。
- 指定项目状态和脱敏摘要，与 APP／可信引擎证据比对。原厂 `get_project_status/get_project_compose` 只能由后端按已登记 ID 映射固定项目；配置原文和认证留宿主。
- 禁止原厂 `project_list`，其存在自动 `down` 分支；禁止通过机器参数传原始路径、项目名、Compose、原厂 action 或命令。

本次不运行任何写入验收。后续写能力须另用明确指定的隔离目录和测试项目，自动化通过不能替代实机结果。

验收记录至少包含镜像 SHA、时间、实际来源、接口版本、已完成步骤、逐项失败／未选择／未接入原因和原有配置是否保持；不记录 Token、原厂签名、用户文件内容或 Compose 原文。
