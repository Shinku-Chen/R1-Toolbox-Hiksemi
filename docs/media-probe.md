# 原厂相册识别实机验证

`POST /api/dev/media` 在宿主内部只读复用管理员已有会话，对固定原厂媒体状态接口进行签名查询，仅返回首帧数据和验证状态。它用于明确发起的实机验证，不属于自动轮询或 `/api/dev/diagnostics` 的被动快照。原有 `probe` 验证相册识别，`probe_all` 逐项验证下表七类状态，`probe_transcode` 诊断转码能力与单个文件的清晰度档位。

## 请求与授权

```javascript
const response = await fetch('/api/dev/media', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({action: 'probe'})
});
console.log(await response.json());
```

接口仅接受 `{"action":"probe"}`、`{"action":"probe_all"}`、`{"action":"probe_transcode","path":"..."}` 或 `{"action":"probe_business","confirm":true}`，需要 `R1_DEV_NO_PAIRING=1`、精确实际客户端 `R1_DEV_ALLOW_IPS` 和有效 `R1_DEV_MINUTES` 窗口。开发者白名单授权可直接测试，不依赖浏览器登录或 `R1_DEV_ALLOW_COMMANDS`；同源、JSON 和 CSRF 校验继续生效。没有公开 GET、CORS 或任意参数转发。

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

## 转码能力与清晰度档位诊断

本节针对电视播放器的清晰度切换。影视列表另有“清晰度／解码”筛选，后者的“解码”实际表示杜比视界、HDR、SDR 等动态范围分类；候选列表不足时客户端会隐藏筛选项。该列表的候选数据与显示条件不由 `probe_transcode` 验证，不能把播放接口的拒绝码直接当成列表筛选故障的原因。

### 匹配固件与电视 APK 的静态结论

以下结论来自 `emhistor` SHA-256 `402964bb23e32786db2a8d8b8a0ef2a2121cf6f753b4238cf5da53a4aa1c8d26` 与已核同一 APK 的 DEX：`classes.dex` 为 `fd7af59cc22fb4c324b71e0f3125d247ab221aa70fe011be157a0f51c75c070a`，`classes2.dex` 为 `b7656afa0c333852c905c8dbaf01240ea4b01b624acce77fd78dc57b4006cec2`。这些是样本之间的确定代码关系，尚未证明用户当前电视和 NAS 的请求实际命中：

- 本人播放路径固定发送 `video_capability=1`。原厂通过前置身份／路径等门后，在 `0x24bb08`／`0x24bbb9` 返回 `-5402`；HTTP 200 的带空格 JSON 错误能被该电视网络层正确解析，首次发送旗标使后续能力 JSON 不再覆盖它。电视端回原画并禁止此次清晰度切换。这属于该请求形态与固件的合同不兼容，不能解释成某一种影片编码必然不支持，也不能推广到共享播放的其它路径。
- 静态能力表声明视频转码为 1、转码开关能力为 0；电视机型表包含 `R1`／`R1X`／`SR1`。开关接口缺失本身不是禁用清晰度的充分原因。固件还包含 `/tmp/tr_video_sock` → daemon → `trctl_v1` → FFmpeg 的 VAAPI 转码链，但文件存在不证明实机驱动、进程或转码可用。
- 影视列表走 `/rest/1.1/scraper?action=dynamic_filter`，带 `tv=1` 及当前库的 `lid`／`type`，原厂转发至 scraper `query_tab`。电视只纳入白名单中标记为 1 的分类，非空列表加“全部”，仅总数量大于 2 才显示筛选行。因此只有一种实际分类时会隐藏该行；须核对同一选中库，不能用全库数量替代。后端存在失败时返回 `code=0`／空结构的分支，不能只凭零或空对象认定没有分类。当前 `probe_transcode` 不采集此列表接口。
- `getDefinitionText`（`0x459308`）的原厂标签为 `raw`＝原画、`high`＝高清、`mid`＝清晰、`low`＝流畅；`mid` 使用资源 `2131755012`。标签差异不改变原厂错误码或菜单分支。

电视端播放器的每条门槛要分开看，不能概括成「任一不满足就只原画」：

- `cap_transcode_video` 不为 1（客户端本地机型判断 + 该能力位）→ `buildVideoPath` 直接走原画并把清晰度入口设为不可见；
- `videoSwitchOpen` 为 `false`（只有 `cap_tr_video_switch_v1 == 1` 且查询返回 `code = 0, mode = 0` 才会是 `false`）→ 同样走原画并隐藏入口；
- 确定的传输失败 → `AbilityConfig$2.onFailure` 置 `videoSwitchOpen=true`；非零 `code` 且 `mode` 可解析为整数时，`onSuccess` 的非零码分支也置 true。二者只是客户端回退，不是服务端开关已开启的观测；
- 成功响应 `code=0` 但 `mode` 非法或缺失 → `onSuccess` 在 `0x0070` 跳到 `0x0088`，或进入 JSON 异常 catch，保留旧客户端状态，**不能**当作 `onFailure` 置 true。没有旧状态证据时保持 `switch_state_unknown`；
- `cap_tr_video_switch_v1 != 1` → 客户端根本不发 `get_trvideo_switch`，`videoSwitchOpen` 保持默认 `true`；
- 档位拒绝码 `-5402` 有已审计的不支持分支；其它非零码不套用该分支，不推断播放、菜单或切换支持状态。

因此，服务端开关观测、确定的客户端回退和未知的旧客户端状态要分开看。通用 HTTP wrapper 对错误 envelope 的解析格式敏感，不能仅凭非零 `code` 且无有效 `mode` 就断言触发了 `onFailure`。

```javascript
const response = await fetch('/api/dev/media', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-FNM-Request': '1'},
  body: JSON.stringify({action: 'probe_transcode',
    path: '/drives/sda/Users/admin/Movies/example.mkv',
    confirm: true})
});
console.log(await response.json());
```

请求体必须恰好是这三个字段（`confirm` 必须为 `true`，缺失或 `false` 直接 `400` 且不发任何原厂请求）。确认针对的是本动作的真实副作用：如右侧审计所述，`get_offlinevideo_path` 在返回档位前可能实际读取媒体内容并写入 `user.md5` 扩展属性；这不是无副作用的浏览接口。`path` 必须是 `/drives/<disk>/Users/admin/` 下的绝对路径，并拒绝空段、`.`／`..`、反斜杠、控制字符、非字符串、相对路径以及其它盘根或 `Users`／`admin` 之外的位置。这是**纯字符串形式校验**：宿主不打开、不 `stat`、不解析，不产生任何文件访问授权，该值只作为原厂查询参数；因此它**无法判断末段是文件还是目录**，`/drives/sda/Users/admin/Movies` 这类无尾斜线的目录路径同样会通过，文案不得宣称能拒绝目录。

| 步骤 | 固定原厂请求 | 公开投影 |
|---|---|---|
| `capability` | `GET /rest/1.1/config` `action=get_all_capacity&capacity_detail=0` | 仅 `cap_transcode_video`、`cap_tr_video_switch_v1` 两个已审计能力位，以及未识别／畸形条目计数 |
| `switch` | `GET /rest/1.1/config` `action=get_trvideo_switch` | `mode`（0 关、1 开）与 `open`；不返回原文 |
| `capacity` | `GET /rest/1.1/config` `action=get_offlinevideo_path&video_capability=1&path=<urlencode>&offset=0&sharpness=raw&terminal=<固定标识>` | 规范化 `status`／`ready`、固定档位枚举及字段状态、宽高／码率／帧率、`codec_type`（仅 `video` 或 null）、音轨／字幕条数；路径只报告存在性 |

每次原厂请求都在 `steps.<step>` 给出 `native_code`（仅安全整数，不返回 `msg`／路径／原文）；HTTP 200 只表示请求已处理，业务是否成功要看该步 `ok` 与 `native_code`。

存在但非法的 `code`（布尔值、字符串、null 或超出安全整数范围）固定失败为 `native_code_invalid`，不能当成无 code 的成功。仅 `get_all_capacity` 的 `capacity_list` 数组，以及档位 getter 的 `capabilities` 对象／非空 `video_720p` 结构允许确实缺省 code；开关 getter 不接受无 code 的成功。

`profile`／`offlineprofile` 只接受 `raw`、`high`、`mid`、`low`、`origin` 的严格逗号组合；其它值为 null，并标记 `missing`／`invalid`。明确空串与非法值分开，后者不能触发 720p 补档或 HLS 推导。自由文本 `name`／`codec_name` 不返回，未知 profile 不作标签或原码透传。`status` 仅输出 `ready`／`other`／null，非 ready 的原始字符串不返回。缺失音轨／字幕数组计数为 null，明确空数组才是 0。

固定参数不由调用方选择：`offset=0`、`sharpness=raw`、`video_capability=1`，`terminal` 使用固定的非会话标识。路径、原厂原始路径、`hash_path`、原始错误文本、令牌和会话标识都不出现在返回中；`request.path_sha256_prefix` 只用于把结果和上次请求对上。

**与电视流程的差异**：电视端只在 `cap_tr_video_switch_v1 == 1` 时请求开关，而本诊断固定三次都发；能力位为 0 时 `request.switch_query_in_tv_flow=false`、`extra_getters_not_in_tv_flow=["transcode_switch"]`。该能力位未知／冲突时两者保持 null，不猜电视端是否会查询。

**证据分层**：`evidence` 分开记录服务端观测（`server_capability`、`server_switch_state`）、客户端分支（`client_switch_branch`、`client_switch_fallback`）与播放（`playback_source` 固定 `tv_dex_branch_inference`、`playback_observed` 固定 `false`）。本接口从不拉流，因此不能说 HLS 已经真实启动或菜单一定出现。

响应形状（节选：原厂拒绝了档位查询，客户端旧开关状态仍未知）：

```json
{"ok": true, "transcode": {
  "schema_version": 1, "available": true, "status": "partial",
  "reason": "video_capability_query_unsupported",
  "request": {"offset": 0, "sharpness": "raw", "terminal": "0000…", "path_sha256_prefix": "…"},
  "steps": {"capacity": {"ok": false, "reason": "native_request_rejected", "http_status": 200, "native_code": -5402}},
  "capability": {"structure": "ok", "entries": 3,
                 "values": {"cap_transcode_video": 1, "cap_tr_video_switch_v1": 1},
                 "states": {"cap_transcode_video": "enabled", "cap_tr_video_switch_v1": "enabled"},
                 "unknown_entries": 1, "malformed_entries": 0,
                 "duplicate_entries": 0, "conflicting_entries": 0},
  "switch": null,
  "capacity": {"code": -5402, "structure": "rejected", "ready": null},
  "definitions": null,
  "tv_player": {"definition_menu_visible": null, "playback": null, "labels": [],
                "reason": "video_capability_query_unsupported",
                "switch_state": "unknown_previous_client_state",
                "switch_client_fallback": null,
                "capacity_error_branch": {"native_code": -5402, "supports_switch": false, "conditional": true, "observed": false},
                "playback_observed": false, "requires_tv_model_whitelist": true},
  "evidence": {"server_capability": "enabled", "server_switch_state": null,
               "client_switch_branch": "unknown_previous_client_state",
               "client_switch_fallback": null,
               "playback_source": "tv_dex_branch_inference",
               "playback_observed": false},
  "explicit_getter_only": true, "may_read_media_content": true,
  "may_write_media_xattrs": true, "may_wake_disks": true,
  "getter_effects_fully_audited": false, "playback_observed": false,
  "notes": ["…"]}}
```

### 副作用审计：条件性读内容 + 写元数据 + 唤盘

静态审计（emhistor `402964bb…`，`V6.3.3 Build_260907`）给出的固定调用链：

- `get_offlinevideo_path` 在 `0x24bb08` 对 `video_capability` 做 `atoi`，非 0 时在 `0x24bbb9`
  先发 `-5402`；发送链 `183964`→`183c50`→`951c94` 用 `request+0x18` 做首次发送去重，
  之后的 720p JSON 不能覆盖它。电视与当前诊断都发 `video_capability=1`，因此该分支可达。
- 更早的授权／路径门通过后才进入上述分支；本条只对该形态成立，不外推到所有固件或全部查询。
- 该 getter 在返回前会走 `tr_video_md5`→`wr_md5_generate`：没有有效 `user.md5` 缓存时
  实际 `fopen`／`fread` 媒体（最多 64×1 MiB），随后 `setxattr(user.md5)` 写扩展属性。
  因此调用**可能读取媒体内容、写入元数据并唤盘**；我方不直接读内容，不代表调用链不读。
- 其它 getter 的副作用（是否建任务、改库）仍未完整审计，不能声称「只有以上效果」。

结果固定标记 `explicit_getter_only:true`、`may_read_media_content:true`、
`may_write_media_xattrs:true`、`may_wake_disks:true`、`getter_effects_fully_audited:false`，
不再使用 `read_only_query`。本动作只接受显式请求，与其它媒体检测共用锁并受绝对窗口限制，
不进入被动快照、后台轮询或 MCP。

### 结构与档位：三个容器分支必须分开

`capacity` 保留原厂响应的结构证据，不能把「`capabilities` 为 null」与「`capabilities`
是对象但没有可用 `video` 行」合并：

| 字段 | 取值 | 含义 |
|---|---|---|
| `capabilities_state` | `object`／`null`／`invalid` | 响应里 `capabilities` 是对象、缺失或为 null、还是类型不对 |
| `video_state` | `list`／`empty`／`null`／`invalid` | `capabilities.video` 是非空数组、空数组、缺失／null、还是类型不对 |
| `first_profile_state` | `valid`／`empty`／`missing`／`invalid`／`absent` | 首个视频行的审计档位字段状态；坏首行不由后续行替代 |

按 `classes.dex` SHA-256 `fd7af59c…` 的 `onCapacityResult` 固定引用推导档位：

1. `capabilities == null`：`0x006e` 跳到 `0x016c`，`0x0170` 不跳，落到 `0x0174`
   的 `video_720p` 检查，有值才在 `0x0184` 追加一档「清晰」（独立 fallback）。
2. `capabilities` 是对象但 `video` 缺失／null／空数组：`0x007a`／`0x008e` 跳到
   `0x016c`，`0x0170` 因对象非空跳到 `0x0196`，**不追加任何档位**。
3. `capabilities.video[0]` 存在：`profile` 非空且不含 `origin` 时按逗号拆分；
   随后（`0x0124` 结束后仍落入 `0x0126`，不是二选一）用 `0x013e` 的
   `contains("mid")` 与 `0x0146` 的跳转决定是否追加：不含 `mid` 且
   `video_720p` 非空才追加「清晰」。首个 `profile` 为空时走自己的 fallback，
   同样只看 `video_720p`。

因此：`high`＋720p → 原画／高清／清晰；`origin`＋720p → 原画／清晰；
`origin,mid`＋720p → 原画（含 mid 时原厂跳过追加）；`capabilities={}` 或
`video` 缺失／null／空数组＋720p → 只有原画。非法数组类型、坏首行或未识别档位不返回原文，记 `unmapped_profile_count` 与 `profile_metadata_unknown`，档位列表为空、播放方式未知。

### 播放方式：与档位数量无关，缺证据时为 null

`tv_player.playback` 单独按 DEX 推导，**不能**用「档位列表长度 > 1」当 HLS 证据：

- `0x0196` 未就绪（`status != "ready"`）→ 跳到 `0x0222`：播原始文件或离线 720p，
  记为 `playback:"original"`、`reason:"original_fallback"`。
- 已就绪时 `0x01a6` 若 `capabilities == null` 直接到 `0x0310` 返回，绕过
  `0x01e0` 的 `isPlayM3U8=true` 与 `0x0212` 的 m3u8 构建，因此 `playback` 与
  `definition_menu_visible` 保持 `null`，原因 `capacity_structure_missing`。
- 已就绪、元数据符合固定 schema 且 `capabilities` 是对象 → 仅推导 TV 会尝试构建 m3u8，记 `playback:"hls"`；列表只有
  「原画」时原因记为 `hls_without_alternatives`，多档时 `definitions_available`。

### 判读顺序（把原因缩到一段）

1. `capability_unknown`：能力结构缺失／畸形，或同一能力名出现矛盾值
   （`conflicting_entries`）。**不能**据此说设备不支持。
2. `device_reports_no_video_transcode_capability`：只有 `cap_transcode_video`
   明确为 0 才给这个结论。
3. `switch_state = fallback_open_unverified`：仅确定的传输失败，或非零 code 且整数 mode 的客户端分支会置 true；`switch_client_fallback` 分别标 `open_on_failure`／`open_on_native_rejection`。服务端状态仍未观测。成功响应的无效／缺失 mode 记 `switch_state_unknown`，不推导已开启或 HLS。
4. `transcode_switch_closed`：开关明确为 0。
5. `capacity_query_failed`／`capacity_status_unknown`：档位查询失败或 `status`
   缺失。
5a. `video_capability_query_unsupported`（原厂 `code=-5402`）：`video_capability != 0`
    时原厂固定先回 `-5402`，电视端据此清空档位、`cantSupport=true`、raw 也
    `isPlayM3U8=false`，点击清晰度提示「该视频不支持切换清晰度」。这是静态因果推导
    （`HSPlayerActivity.onGetCapacityError` `code_off 0x45bfc8`），不是本机实测。
5b. `capacity_query_rejected`：其它非 0 原厂码只保留安全整数 `native_code`；`definitions` 为 null，播放／菜单／支持切换均为 null，`status=partial`，不套用 -5402 的不支持分支。开关未知时 -5402 仍保留真实码和条件性 `capacity_error_branch`，不宣称旧客户端开关已开。
6. `capacity_structure_missing`／`capacity_structure_invalid`：就绪但
   `capabilities` 不是对象，播放方式未知。
7. `original_fallback`／`hls_without_alternatives`／`definitions_available`：
   已得出播放结论。

顶层 `status` 由真实依赖推导：`ok`（已得出结论）、`partial`（能力位已知但后续缺
证据）、`unknown`（能力位本身未知）、`denied`（撤权）；`available` 只在能力位可
判定时为 `true`。`steps` 逐项给出 `ok`／`reason`／`http_status`。

每一步前后及发布前重新调用实时授权，首次失效立即停止后续 getter，并锁定本轮拒绝；即使随后重新授权也不复活旧结果。拒绝结果 `steps={}`，路径摘要、业务投影与 `evidence` 全部清空。HTTP 发送前已有的开发窗口复核仍保留。

### 用一次结果排除候选

| 观察 | 可以排除 | 仍需别的证据 |
|---|---|---|
| `capability_unknown` | 只能排除「本接口已给出结论」 | 能力结构、固件版本、该机型是否支持转码 |
| `device_reports_no_video_transcode_capability` | NAS 侧不提供视频转码能力 | 是否可通过设置／升级开启 |
| `switch_state = fallback_open_unverified` | 客户端会继续（`onFailure` 回落 true） | 服务端开关真实状态、默认值来源 |
| `transcode_switch_closed` | 转码被关闭 | 关闭是配置还是策略 |
| `capacity_query_failed` | 该路径上的档位查询失败 | 失败是权限、路径、还是后端 |
| `capacity_status_unknown` | — | 原厂 `status` 语义与就绪条件 |
| `capacity_structure_missing` | NAS 未返回可播放的 m3u8 结构 | 编码格式、后端能力、客户端白名单 |
| `video_capability_query_unsupported`（`-5402`） | 该请求形态下 NAS 不提供可切换档位，TV 会提示不支持 | 其它固件／请求形态、真实播放 |
| `original_fallback` | 仅推导 TV 走非 HLS 分支（状态非 ready） | 未就绪的成因、真实播放情况 |
| `definitions_available`／`hls_without_alternatives` | 仅静态推导 TV 会尝试 HLS | 服务端开关仍可能未知、转码引擎、编码支持、机型白名单、真实拉流；不能据此排除 NAS 侧原因 |

### 本接口不能证明、仍需实机或别的证据

- 电视端机型白名单（`R1`／`R1X`／`SR1` 等）在客户端本地判断，固定标记
  `requires_tv_model_whitelist:true`。
- 具体编码格式是否被转码后端支持、硬解／硬编是否可用：本接口只回原厂档位与
  流参数，不能据此断言可转码或不可转码。
- 原厂是否真的启动了 HLS 会话、字节流是否可播：本接口不发送
  `play_hls_video`／`stop_hls_video`，也没有拉流证据。
- getter 的**全部**副作用：`get_offlinevideo_path` 的条件性内容读取与 `user.md5` 写入已由静态审计确认，
  其它 getter 是否建任务、改库仍未完整审计，固定标记 `getter_effects_fully_audited:false`。
- 原厂静态能力表（`capacity-rows.json`：`cap_transcode_video=1`、`cap_tr_video_switch_v1=0`、
  `cap_web_transcode_video=1`）只是固件常量，不等于实机返回，也不能代替 `get_all_capacity` 的实测值。
- DEX 归属：`classes.dex`（SHA-256 `fd7af59c…`）含 `HSPlayerActivity`；
  `classes2.dex` 含 `AbilityConfig`／`AbilityConfig$2` 与 `VideoDefinitionHelper`
  （`app-methods.txt` 行 3385／5900／5953／5980，`code_off` `0x222edc`／`0x2228c4`／
  `0x2228e8`／`0x222a98`／`0x222ab0`）。不得把 classes2 的方法归到 `fd7af59c…`。
- 电视端实际菜单与播放：受客户端版本、连接模式（SADP／云）与本地机型判断影响。

## Toolbox 自有媒体快照

显式查询完成后，可以把已经脱敏的投影交给 `record_media_snapshot(state_dir, result, kind='batch')`；单项相册识别使用 `kind='face'`。模块再次按七类固定数值字段校验，重新计算可确认的完成比例，过滤凭据、签名地址、用户标识、路径、相册标识、原始错误和额外字段；未知枚举、单位和不可信计数保留 `null`。结果以私有权限 `0600` 原子保存至既有 Toolbox 数据目录的 `media-snapshot.json`，不会保存原厂原始响应。

`collect_media_snapshot(state_dir, root='/')` 只读取这份自有快照，适合网页和有权限的 MCP 客户端反复查看。它不会读取原厂会话库、证书或相册数据库，不会连接媒体端口、自动认证或重试查询。`root` 仅用于业务 provider／fixture 的统一调用形式，不决定读取其他原厂文件。没有缓存时返回 `status:not_collected`、`available:false`、`data:null`，不会为填充页面而主动探测。

快照数据固定包含 `captured_at`、`live:false`、`explicit_refresh:true`、七项 `results` 和 `coverage`。每项保留自己的采集时间、`available/status/reason/query_success/data` 与 `last_failure`；失败会把该项的旧成功数据替换为 `null`，后续成功仍保留上次失败记录。单项 `face` 刷新保留其他类别原有时间，不把旧数据冒充同一次新采集。采集结果即使跨容器重启仍然是历史观测，界面必须显示“上次显式采集”及时间，不称作实时进度。

`available` 只表示至少一项取得真实业务字段，缓存文件存在不代表采集成功。全部失败时返回 `status:cached_empty`、`available:false`，但仍保留各项失败信息，方便区分凭据不可用、原厂拒绝和传输失败。文件损坏、过大、符号链接或读取失败保持不可用；某一项字段损坏不会污染其他已验证项。

`complete` 只表示最近一次 `batch` 的全部适用类别字段完整，不表示全机所有账号、媒体库或相册均已枚举。没有既有管理员共享相册时，该项明确为 `not_applicable`；其余六项完整可使 `complete:true`，同时保留 `coverage.total:7` 和 `coverage.not_applicable:1`。`face` 单项刷新不能宣称已完成一个新的完整批次。相似图片的原厂计数缺陷、缩略图字段单位、共享计数吞错零值和刮削默认范围等限制继续适用。

保存函数返回 `stored/status/reason` 回执。保存失败不改变已完成原厂查询的事实，调用方应保留查询结果并说明快照未保存，不能诱导客户端重新执行有副作用的查询。被动读取不写入缓存、不更新采集时间，也没有自动刷新或超时后重新认证的分支。

静态依据见[媒体接口审计](vendor-media-diagnostics.md)。本实现针对 `V6.3.3 Build_260907`；数据库、会话和接口结构不符时保持不可用。

2026-10-29，`b30e27a` 已在“小可爱NAS”上通过相册识别实机验证：只读取得已有会话，原厂 HTTP 200、`code=0`，图片 `69380/69380`、视频 `5236/5237`，请求约 211 毫秒。这个结果仅证明 `probe` 的相册识别链路，不代表其他六项已经通过。

本地测试仅使用隔离 SQLite、固定证书和本地协议 fixture，不接触 NAS 的真实凭据。批量版本发布并部署后，才能用 `probe_all` 完成其他接口的实机验收；测试或镜像发布成功不等于实机全部验证成功。

2026-10-29，既有部署已进一步完成一次显式 `probe_all` 实机核验：六类状态返回完整可投影字段，共享相册因没有既有管理员创建的相册而标记 `not_applicable`。这证明本次固定调用链和字段可用，不改变各 getter 的副作用、用户范围与状态枚举限制，也不能据此宣称可以后台轮询全部原厂媒体接口。
