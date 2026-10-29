"""Read existing download queues through fixed, verified loopback endpoints.

This deliberately avoids factory REST getters: their lazy initialization can
launch/recover download clients or refresh authentication. No secret is read.
"""
import json
import math
import re
import socket
import time

HOST = "127.0.0.1"
PORT = 6800
PATH = "/jsonrpc"
MAX_HEADER = 8192
MAX_BODY = 128 * 1024
MAX_QUEUE = 128
NETWORK_SECONDS = 2.0
KEYS = ("gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed")
SOURCE = "aria2_loopback_rpc"
QB_PORT = 8181
QB_SOURCE = "qbittorrent_loopback_webapi"
QB_PATHS = ("/api/v2/app/version", "/api/v2/transfer/info",
            "/api/v2/torrents/info?filter=all&sort=hash&limit=129&offset=0")
QB_STATES = frozenset(("error", "missingFiles", "uploading", "pausedUP", "queuedUP",
                       "stalledUP", "checkingUP", "forcedUP", "allocating", "downloading",
                       "metaDL", "pausedDL", "queuedDL", "stalledDL", "checkingDL",
                       "forcedDL", "checkingResumeData", "moving", "stoppedDL", "stoppedUP",
                       "forcedMetaDL", "checkingMetaDL"))
REASONS = {
    "proc_unavailable": "无法读取下载服务的进程现场。",
    "proc_partial": "下载进程现场只能部分确认，尚不能核验唯一监听归属。",
    "not_listening": "没有发现该下载引擎的固定本机监听。",
    "owner_unverified": "下载监听无法关联到该引擎唯一且已核验的进程。",
    "ambiguous": "下载监听存在多个执行者，无法确认来源。",
    "identity_changed": "查询期间下载进程或监听身份发生变化。",
    "auth_required": "下载接口需要授权；未登录、读取口令或刷新令牌。",
    "rpc_error": "原厂下载 RPC 返回错误；未执行登录、恢复或重试。",
    "invalid_response": "下载 RPC 的返回格式不符合受限只读协议。",
    "limit_exceeded": "下载快照超过诊断大小或条目上限，未返回不完整数据。",
    "timeout": "下载只读查询超过时间限制。",
    "service_unavailable": "已观察到的下载监听当前无法连接。",
    "transport_error": "下载只读连接未能完成。",
    "no_readable_engine": "未取得可核验的下载任务快照；请分别查看各引擎状态。",
}


class _Failure(Exception):
    def __init__(self, status):
        self.status = status


def _remaining(deadline, clock):
    value = deadline - clock()
    if value <= 0:
        raise _Failure("timeout")
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _Failure("invalid_response")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise _Failure("invalid_response")


def _http_exchange(request, port, ceiling, deadline, clock, socket_factory, *, host=HOST):
    """Fixed caller-built requests; reject redirects, compression and streaming.

    A monotonic deadline applies before every socket operation, so a server
    sending tiny chunks cannot keep the diagnostic alive indefinitely.
    """
    sock = socket_factory((host, port), timeout=_remaining(deadline, clock))
    try:
        sock.settimeout(_remaining(deadline, clock))
        sock.sendall(request)
        data = bytearray()
        while b"\r\n\r\n" not in data:
            sock.settimeout(_remaining(deadline, clock))
            block = sock.recv(min(4096, MAX_HEADER + 4 - len(data)))
            if not block:
                raise _Failure("invalid_response")
            data.extend(block)
            if len(data) > MAX_HEADER and b"\r\n\r\n" not in data:
                raise _Failure("limit_exceeded")
        header, content = bytes(data).split(b"\r\n\r\n", 1)
        if len(header) > MAX_HEADER:
            raise _Failure("limit_exceeded")
        lines = header.split(b"\r\n")
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?", lines[0])
        if not match or len(lines) > 65:
            raise _Failure("invalid_response")
        status = int(match.group(1))
        if status in (401, 403):
            raise _Failure("auth_required")
        if status != 200:
            raise _Failure("invalid_response")
        headers = {}
        for line in lines[1:]:
            if b":" not in line:
                raise _Failure("invalid_response")
            key, value = line.split(b":", 1)
            if not re.fullmatch(rb"[A-Za-z0-9-]+", key):
                raise _Failure("invalid_response")
            key, value = key.lower(), value.strip()
            if key in headers:
                raise _Failure("invalid_response")
            headers[key] = value
        if b"transfer-encoding" in headers or headers.get(b"content-encoding", b"identity").lower() != b"identity":
            raise _Failure("invalid_response")
        length = headers.get(b"content-length", b"")
        if not re.fullmatch(rb"[0-9]{1,9}", length):
            raise _Failure("invalid_response")
        length = int(length)
        if length > ceiling:
            raise _Failure("limit_exceeded")
        while len(content) < length:
            sock.settimeout(_remaining(deadline, clock))
            block = sock.recv(min(4096, length - len(content)))
            if not block:
                raise _Failure("invalid_response")
            content += block
        if len(content) != length:
            raise _Failure("invalid_response")
        _remaining(deadline, clock)
        return content
    finally:
        sock.close()


def _json(content):
    try:
        return json.loads(content.decode("utf8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _Failure("invalid_response") from None


def _http_rpc(payload, deadline, clock, socket_factory):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    request = (f"POST {PATH} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
               "Content-Type: application/json\r\nAccept: application/json\r\n"
               "Accept-Encoding: identity\r\nConnection: close\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
    ceiling = 4096 if payload["method"] == "aria2.getVersion" else MAX_BODY
    return _json(_http_exchange(request, PORT, ceiling, deadline, clock, socket_factory))


def _qb_get(path, deadline, clock, socket_factory, *, host=HOST):
    if path not in QB_PATHS or host not in (HOST, '::1'):
        raise ValueError("仅允许固定的 qB 只读查询")
    authority = ('[::1]' if host == '::1' else HOST) + ':' + str(QB_PORT)
    request = (f"GET {path} HTTP/1.1\r\nHost: {authority}\r\n"
               f"Referer: http://{authority}/\r\n"
               "Accept: application/json, text/plain\r\nAccept-Encoding: identity\r\n"
               "Connection: close\r\n\r\n").encode("ascii")
    return _http_exchange(request, QB_PORT, 4096 if path == QB_PATHS[0] else MAX_BODY,
                          deadline, clock, socket_factory, host=host)


def _rpc_error(value):
    """Use an error only for classification; never return vendor error text."""
    if not isinstance(value, dict):
        raise _Failure("rpc_error")
    message = value.get("message", value.get("faultString"))
    if isinstance(message, str) and re.fullmatch(r"\s*(?:Unauthorized|Authorization failed)\.?\s*", message, re.I):
        raise _Failure("auth_required")
    raise _Failure("rpc_error")


def _result(response, expected_id):
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != expected_id:
        raise _Failure("invalid_response")
    if "error" in response:
        _rpc_error(response["error"])
    if "result" not in response:
        raise _Failure("invalid_response")
    return response["result"]


def _identity(check, root, *, port=PORT, comm="aria2c-nox"):
    value = check(root=root, port=port, comm=comm)
    if not isinstance(value, dict):
        raise _Failure("owner_unverified")
    if value.get("available") is not True or value.get("status") != "verified":
        status = value.get("status")
        raise _Failure(status if status in ("proc_unavailable", "proc_partial", "not_listening", "owner_unverified", "ambiguous") else "owner_unverified")
    pid, ticks, inode = value.get("pid"), value.get("start_ticks"), value.get("socket_inode")
    if type(pid) is not int or pid <= 0 or type(ticks) is not int or ticks <= 0 or not isinstance(inode, str) or not re.fullmatch(r"[0-9]{1,24}", inode):
        raise _Failure("owner_unverified")
    host = value.get('connect_host', HOST)
    if host not in (HOST, '::1') or comm == 'aria2c-nox' and host != HOST:
        raise _Failure("owner_unverified")
    return pid, ticks, inode, host


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
        raise _Failure("invalid_response")
    value = int(value)
    if value > (1 << 63) - 1:
        raise _Failure("invalid_response")
    return value


def _project_queues(value, version):
    if not isinstance(value, list) or len(value) != 3:
        raise _Failure("invalid_response")
    tasks, seen, counts = [], set(), {}
    allowed = ({"active"}, {"waiting", "paused"}, {"complete", "error", "removed"})
    for queue, states, item in zip(("active", "waiting", "stopped"), allowed, value):
        if isinstance(item, dict):
            _rpc_error(item)
        if not isinstance(item, list) or len(item) != 1 or not isinstance(item[0], list):
            raise _Failure("invalid_response")
        entries = item[0]
        if len(entries) > MAX_QUEUE:
            raise _Failure("limit_exceeded")
        counts[queue] = len(entries)
        for row in entries:
            if not isinstance(row, dict):
                raise _Failure("invalid_response")
            gid, status = row.get("gid"), row.get("status")
            if not isinstance(gid, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", gid) or not isinstance(status, str) or status not in states or gid.lower() in seen:
                raise _Failure("invalid_response")
            seen.add(gid.lower())
            total, completed = _number(row.get("totalLength")), _number(row.get("completedLength"))
            if completed > total:
                raise _Failure("invalid_response")
            tasks.append({"gid": gid.lower(), "status": status, "total_bytes": total, "completed_bytes": completed,
                          "progress_percent": round(completed * 100 / total, 2) if total else None,
                          "download_bytes_per_second": _number(row.get("downloadSpeed")),
                          "upload_bytes_per_second": _number(row.get("uploadSpeed"))})
    # Counts describe only these bounded windows, never all historical jobs.
    return {"engine": "aria2", "version": version, "tasks": tasks, "counts": counts,
            "limits": {"active": MAX_QUEUE, "waiting": MAX_QUEUE, "stopped": MAX_QUEUE}, "complete": False}


def collect_aria2(root="/", *, ownership_check=None, socket_factory=None, monotonic=None):
    """Return a bounded snapshot of an already-running, verified aria2 daemon.

    Injection arguments are Python-only fixtures, never HTTP request fields.
    Missing/unauthorized/unverified services keep data null, not an empty list.
    """
    if ownership_check is None:
        from vendor_runtime import require_one_listener
        ownership_check = require_one_listener
    socket_factory = socket_factory or socket.create_connection
    clock = monotonic or time.monotonic
    try:
        identity = _identity(ownership_check, root)
        deadline = clock() + NETWORK_SECONDS
        version_request = {"jsonrpc": "2.0", "id": "r1-download-version", "method": "aria2.getVersion", "params": []}
        response = _http_rpc(version_request, deadline, clock, socket_factory)
        value = _result(response, version_request["id"])
        if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?(?:[A-Za-z0-9.+_-]{0,24})", value["version"]):
            raise _Failure("invalid_response")
        features = value.get("enabledFeatures")
        if not isinstance(features, list) or len(features) > 32 or any(not isinstance(v, str) or len(v) > 64 for v in features):
            raise _Failure("invalid_response")
        if _identity(ownership_check, root) != identity:
            raise _Failure("identity_changed")
        # Only these three read methods; no URL, path, token or task ID input.
        methods = [{"methodName": "aria2.tellActive", "params": [list(KEYS)]},
                   {"methodName": "aria2.tellWaiting", "params": [0, MAX_QUEUE, list(KEYS)]},
                   {"methodName": "aria2.tellStopped", "params": [0, MAX_QUEUE, list(KEYS)]}]
        request = {"jsonrpc": "2.0", "id": "r1-download-queues", "method": "system.multicall", "params": [methods]}
        response = _http_rpc(request, deadline, clock, socket_factory)
        data = _project_queues(_result(response, request["id"]), value["version"])
        if _identity(ownership_check, root) != identity:
            raise _Failure("identity_changed")
        return {"available": True, "status": "ok", "reason": None, "source": SOURCE, "data": data}
    except _Failure as error:
        status = error.status
    except (TimeoutError, socket.timeout):
        status = "timeout"
    except ConnectionRefusedError:
        status = "service_unavailable"
    except OSError:
        status = "transport_error"
    return {"available": False, "status": status, "reason": REASONS[status], "source": SOURCE, "data": None}


def _qb_number(value):
    """Missing measurements stay unknown; present invalid numbers reject data."""
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise _Failure("invalid_response")
    return value


def _project_qb(transfer, rows, version):
    if not isinstance(transfer, dict) or not isinstance(rows, list):
        raise _Failure("invalid_response")
    if len(rows) > MAX_QUEUE:
        raise _Failure("limit_exceeded")
    tasks, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _Failure("invalid_response")
        identifier = row.get("hash")
        if (not isinstance(identifier, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", identifier)
                or identifier.lower() in seen):
            raise _Failure("invalid_response")
        seen.add(identifier.lower())
        total, completed = _qb_number(row.get("size")), _qb_number(row.get("completed"))
        if total is not None and completed is not None and completed > total:
            raise _Failure("invalid_response")
        progress = row.get("progress")
        if progress is not None and (type(progress) not in (int, float)
                or not 0 <= progress <= 1 or not math.isfinite(progress)):
            raise _Failure("invalid_response")
        status = row.get("state")
        tasks.append({"status": status if isinstance(status, str) and status in QB_STATES else None,
                      "total_bytes": total, "completed_bytes": completed,
                      "progress_percent": round(progress * 100, 2) if progress is not None else None,
                      "download_bytes_per_second": _qb_number(row.get("dlspeed")),
                      "upload_bytes_per_second": _qb_number(row.get("upspeed"))})
    connection = transfer.get("connection_status")
    # These counts refer only to this bounded result, not all engines/history.
    return {"engine": "qbittorrent", "version": version, "tasks": tasks,
            "counts": {"tasks": len(tasks)}, "limits": {"tasks": MAX_QUEUE}, "complete": False,
            "download_bytes_per_second": _qb_number(transfer.get("dl_info_speed")),
            "upload_bytes_per_second": _qb_number(transfer.get("up_info_speed")),
            "connection_status": connection if isinstance(connection, str)
            and connection in ("connected", "firewalled", "disconnected") else None}


def collect_qbittorrent(root="/", *, ownership_check=None, socket_factory=None, monotonic=None):
    """Query the verified fixed WebUI without logging in or loading a password.

    This succeeds only if its existing localhost/anonymous policy permits the
    requests. Authentication rejection is terminal; no session is created.
    """
    if ownership_check is None:
        from vendor_runtime import require_one_listener
        ownership_check = require_one_listener
    socket_factory = socket_factory or socket.create_connection
    clock = monotonic or time.monotonic
    try:
        identity = _identity(ownership_check, root, port=QB_PORT, comm="qbittorrent-nox")
        deadline = clock() + NETWORK_SECONDS
        version = _qb_get(QB_PATHS[0], deadline, clock, socket_factory, host=identity[3])
        try:
            version = version.decode("ascii").strip()
        except UnicodeError:
            raise _Failure("invalid_response") from None
        if not re.fullmatch(r"v[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?(?:[A-Za-z0-9.+_-]{0,24})", version):
            raise _Failure("invalid_response")
        if _identity(ownership_check, root, port=QB_PORT, comm="qbittorrent-nox") != identity:
            raise _Failure("identity_changed")
        transfer = _json(_qb_get(QB_PATHS[1], deadline, clock, socket_factory, host=identity[3]))
        if _identity(ownership_check, root, port=QB_PORT, comm="qbittorrent-nox") != identity:
            raise _Failure("identity_changed")
        tasks = _json(_qb_get(QB_PATHS[2], deadline, clock, socket_factory, host=identity[3]))
        data = _project_qb(transfer, tasks, version)
        if _identity(ownership_check, root, port=QB_PORT, comm="qbittorrent-nox") != identity:
            raise _Failure("identity_changed")
        return {"available": True, "status": "ok", "reason": None, "source": QB_SOURCE, "data": data}
    except _Failure as error:
        status = error.status
    except (TimeoutError, socket.timeout):
        status = "timeout"
    except ConnectionRefusedError:
        status = "service_unavailable"
    except OSError:
        status = "transport_error"
    return {"available": False, "status": status, "reason": REASONS[status], "source": QB_SOURCE, "data": None}


def collect(root="/", *, aria2=None, qbittorrent=None):
    """Independently observe both fixed engines; never invent combined counts.

    Top-level engines always retains safe per-source status even when no data
    could be read. data.engines holds the successful bounded projections and
    explicit null failures, without merging task IDs or totals across clients.
    """
    engines = []
    for name, provider in (("aria2", aria2 or collect_aria2),
                           ("qbittorrent", qbittorrent or collect_qbittorrent)):
        source = SOURCE if name == "aria2" else QB_SOURCE
        try:
            value = provider(root=root)
            if (not isinstance(value, dict) or type(value.get('available')) is not bool
                    or value.get('available') and (value.get('status') != 'ok' or not isinstance(value.get('data'), dict))
                    or not value.get('available') and value.get('status') not in REASONS):
                raise ValueError('invalid internal download provider')
            value = {"available": value['available'], "status": value['status'], "source": source,
                     "reason": None if value['available'] else REASONS[value['status']],
                     "data": value['data'] if value['available'] else None}
        except Exception:
            value = {"available": False, "status": "diagnostic_error", "reason": "下载来源采集未能完成。",
                     "source": source, "data": None}
        engines.append({"engine": name, **value})
    available = any(row["available"] is True for row in engines)
    return {"available": available, "status": "ok" if available else "no_readable_engine",
            "reason": None if available else REASONS["no_readable_engine"],
            "source": "verified_local_download_engines",
            "engines": [{key: row[key] for key in ("engine", "available", "status", "reason", "source")}
                        for row in engines],
            "data": {"engines": engines, "complete": False} if available else None}
