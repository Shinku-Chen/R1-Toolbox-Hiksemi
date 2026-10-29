# 原厂相册识别实机验证

`POST /api/dev/media` 在宿主内部只读复用管理员已有会话，对固定原厂媒体状态接口进行签名查询，仅返回首帧数据和验证状态。它用于明确发起的实机验证，不属于自动轮询或 `/api/dev/diagnostics` 的被动快照。原有 `probe` 验证相册识别，`probe_all` 逐项验证下表七类状态。

## 请求与授权

```javascript
const response = await fetch('/api/dev/media', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({action: 'probe'})
});
console.log(await response.json());
```

接口仅接受 `{"action":"probe"}` 或 `{"action":"probe_all"}`，需要 `R1_DEV_NO_PAIRING=1`、精确实际客户端 `R1_DEV_ALLOW_IPS` 和有效 `R1_DEV_MINUTES` 窗口。开发者白名单授权可直接测试，不依赖浏览器登录或 `R1_DEV_ALLOW_COMMANDS`；同源、JSON 和 CSRF 校验继续生效。没有公开 GET、CORS 或任意参数转发。

请求不能指定用户、SQL、文件路径、令牌、原厂动作、地址或端口。同一时间只接受一个测试，重复请求返回 `409`。控制、风扇重试及原有诊断接口的授权不变。

## 七类状态批量验证

把示例中的 `action` 改成 `probe_all`。一次读取已有会话，每项重新签名；一次最多八个原厂请求，包含共享相册发现。每个请求总时限十秒，全部超时时约八十一秒，客户端应允许至少九十秒。单项异常、认证拒绝或超时保存在各自结果内，后续项继续执行；不换账号或换凭据重试。

| 结果 ID | 原厂动作 | 固定请求及公开数据 |
|---|---|---|
| `face` | `fcabm/get_face_ai_status` | GET；图片／视频的已处理数、总数及明确区分的同步字段 |
| `ocr` | `file/get_ocr_status` | GET；`totalpic`、`processedpic`、`remainpic`，有效正分母时计算完成比 |
| `thumbnail` | `file/get_thumb_convert_process` | GET；仅五个原始整数 `b/st/pt/sc/cc`，单位／状态未核验，不计算百分比 |
| `similar` | `file/get_similar_status` | GET；原始计数和 `pending_hashes`；原厂总数等于剩余数的缺陷使其不适合作为完成率 |
| `ai` | `fcabm/get_ai_status` | GET；配置状态与识别范围数量，隐藏用户名和范围路径 |
| `scraper` | `scraper/lib_scan_status` | 固定 POST、JSON `{}`；`scaned/total/timestamp/num` 和原始数值状态，不传 `lid`，不猜聚合范围 |
| `shared_album` | `album/share_album_progress` | GET；仅一个已有相册的 `status/scan_num/failed_num/await_num`，相册标识留在宿主 |

共享相册先固定 GET `album/get_share_album_list`。原厂列表 SQL 限定当前 creator，Toolbox 还要求 `creater` 为整数 `1`，只接受 `share_[0-9]{1,10}` 和列表原有的 1～31 字符安全 ASCII `type`。标识不由网页传入，不回传相册名、封面、路径、共享链接或 ID。没有可用的既有相册时返回 `not_applicable`，不会创建或导入样本。发现失败或同 ID 上下文冲突时不调用进度接口。

批量结果在外层 `batch` 中；`results` 始终含七项。每项分别给出 `attempted`、`query_success`、`available`、`status`、`reason`、耗时、原厂 HTTP／业务码及安全数据。`coverage` 分别统计调用、响应成功、数据可用和字段完整数量，`discovery` 仅给出相册发现的安全状态。只有所有项字段完整时顶层 `status` 才为 `ok`；`all_queries_succeeded` 单独表示是否七项都返回预期接口结构。HTTP 200／外层 `ok:true` 表示批量请求已处理，不能代替逐项成功。

缩略图和共享进度正常没有 `code`，必须核对完整整数结构；其余动作要求原厂 `code=0`。负计数投影为 `null`，未知状态不推断运行或空闲。`query_success` 表示返回了预期接口结构，`available` 表示有可用字段；字段缺失或无效时保留 `partial`，不能把接口响应有效解释为数据全都可靠。共享计数的部分数据库错误被原厂吞为零，即使四字段都是零也不能独立证明没有任务。

## 实际执行范围

1. 只读打开固定 `/tmp/nand_config/USER_AUTH_DB_101`，确认本机管理员 `user_id=100000`、`user_type=0`；不读取密码摘要。
2. 在同一用户的原厂启动加载窗口内读取至多 21 条会话，选择其中有效且较新的已有终端记录；拒绝账号冲突、同终端冲突、无效令牌或无法一致读取的数据库。不会创建或修改会话，也不遍历其他账号尝试鉴权。
3. 使用宿主当前时间按原厂算法生成请求签名；基础令牌与签名仅保存在本次调用的内存中。
4. 固定连接 `127.0.0.1:20443`，在发送带签名的 HTTP 请求前，用本机 `/tmp/Cert.pem` 公共证书校验服务端证书。不会读取私钥、不访问任意主机、不使用代理、不跟随重定向。
5. 对 `/rest/1.1/fcabm` 的 `get_face_ai_status` 发一次请求，取得首个完整 JSON 帧后关闭连接。不会等待约八秒后的下一帧，也不修改或停止识别任务。

基础令牌、完整签名地址、会话表、用户名、终端标识、原厂原始响应与错误文本均不会透出接口或写入 Toolbox 日志。记录存在不保证原厂当前内存仍接受；失效会明确报告，不通过重新登录、重置或轮换其他用户的令牌来补救。

## 返回语义

HTTP `200`、外层 `ok:true` 表示测试请求已处理。应继续检查 `probe` 内的可用状态、凭据来源状态、原厂 HTTP／业务结果及数据；不能只凭外层成功判断令牌或进度查询已通过。

- 凭据不可用时不会发原厂请求；缺少 SQLite 支持不会导致整个网页服务无法启动。
- 只有确认签名查询成功后才投影进度数据。认证拒绝、证书不符、连接失败、超时或超限分别报告，未知值保留 `null`。
- `totalpic`／`processedpic`、`totalvid`／`processedvid` 分别是当前管理员识别范围内的图片和视频计数，不是全机所有账号的汇总。
- 图片、视频完成比例仅在分母大于零且计数有效时计算，使用 0～1 的比值；零总数不自动解释为完成或空闲。
- 原厂 `percent` 作为 `sync_percent` 返回，表示识别范围同步阶段，不作为识别完成率。
- `left` 为估算剩余量，单位尚未独立核验；原厂 `status` 与 `range_status` 不用于推断统一的运行／空闲状态。

## 原厂副作用与验证边界

令牌读取是只读操作，但原厂相册状态查询本身可能初始化用户资源、访问媒体数据库、上报计数或写识别完成标记。该接口按需调用，不宣传为全程无副作用的后台采集。它不调用正常登录，因此不走登录 handler 的校时逻辑；不发送媒体扫描、重新识别或其他控制动作。

批量验证还可能触发 OCR 完成事件和共享相册完成标记；原厂共享列表 getter 在数据库故障时有修复／重新打开资源的内部路径。刮削扩展的 `refresh_status` 下游实现未包含在固件中，其内部写入与聚合语义仍未知。Toolbox 只发固定状态请求，不额外调用修复、扫描、导入、重试或取消动作。这些接口不得加入被动快照轮询。

静态依据见[媒体接口审计](vendor-media-diagnostics.md)。本实现针对 `V6.3.3 Build_260907`；数据库、会话和接口结构不符时保持不可用。

2026-10-29，`b30e27a` 已在“小可爱NAS”上通过相册识别实机验证：只读取得已有会话，原厂 HTTP 200、`code=0`，图片 `69380/69380`、视频 `5236/5237`，请求约 211 毫秒。这个结果仅证明 `probe` 的相册识别链路，不代表其他六项已经通过。

本地测试仅使用隔离 SQLite、固定证书和本地协议 fixture，不接触 NAS 的真实凭据。批量版本发布并部署后，才能用 `probe_all` 完成其他接口的实机验收；测试或镜像发布成功不等于实机全部验证成功。
