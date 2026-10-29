# 原厂空间管理显式验证

`POST /api/dev/storage/probe` 在宿主固定访问 `127.0.0.1:80` 的 `emhistor`，调用 `/rest/1.2/config?action=space_manage&check_warning=1`，核对 APP 返回的完整 `disk_sn`、`space_nickname` 与 `space_name`。它是开发者明确触发的主动查询，不进入默认诊断、监控轮询、MCP 或业务缓存。

原厂查询可能创建磁盘设备别名、启用 SMART、维护 SMART 缓存，并对部分 SSD 执行没有待机保护的采集，可能唤醒磁盘。`check_warning` 不能取消这些行为，详见[原厂存储接口审计](vendor-storage-interfaces.md#app-空间管理-restspace_manage)。认证库读取只读，并不表示原厂业务查询全程无副作用。

## 授权与调用

要求显式开发模式、非空局域网 IP／CIDR 白名单、真实 TCP 来源命中且绝对窗口未过期；不需要网页登录会话或命令开关，Bearer Token 和转发头不能替代开发授权。保留同源 `Origin`、JSON 与 `X-FNM-Request: 1` 校验。有效窗口内的开发者必须逐次提交严格的 `confirm:true`。

```bash
curl -sS 'http://NAS_IP:18732/api/dev/storage/probe' \
  -H 'Origin: http://NAS_IP:18732' \
  -H 'Content-Type: application/json' \
  -H 'X-FNM-Request: 1' \
  --data '{"action":"probe","confirm":true}'
```

仅接受上述两个字段，不接受 URL 查询参数、令牌、用户、端口、路径或原厂 action。没有 GET 入口，也不自动重试。与原存储诊断共用非阻塞锁，忙碌返回 `409`；取得锁后重新检查开发窗口。未授权 `403`，请求无效 `400`，采集器异常 `503`。顶层 `ok:true` 只表示请求已处理，原厂结果须检查 `probe.available`、`status`、`reason`、`http_status` 与 `native_code`。

## 宿主认证与返回

宿主复用 `USER_AUTH_DB_101` 中固定所有者 `100000` 已有的有效会话，并在本机生成当次原厂签名；不登录、不刷新会话、不改认证库或系统时间，不尝试其他用户。请求只连接固定 IPv4 回环，不使用系统代理、不跟随重定向。连接、首部和响应体受统一截止时间及大小／深度限制。

仅投影 `probe.data.disks[]` 中的 `disk_sn`、端口、可识别的内核盘名、所属空间名称、来源列表与冲突标记，以及 `probe.data.spaces[]` 的空间名称；不返回原厂原文、签名、令牌、用户路径或异常文本。`credential_read_only:true` 与 `native_query_may_update_state:true` 分别说明两个边界。缺失字段保持 `null`，身份冲突不按盘序猜测。

完整序列号只在这个显式响应中交付，不写入公开快照、审计或持久业务缓存。实机验收应将每个 `disk_sn` 的 UTF-8 SHA-256 前 24 个十六进制字符与新采集的被动存储诊断 `serial_hash` 唯一匹配，再核对设备名和用户名称；多对多、冲突或缺失均保留未确认。

普通名称与缓存身份检查继续使用[开发者被动诊断](dev-diagnostics.md)的 `storage`／显式 `storage_identity`，不因此调用本接口。自动化测试覆盖传输边界、响应投影、真实 HTTP 授权与锁；这些结果不代替 NAS 实机核对。
