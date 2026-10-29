"""Read existing aria2 queues through a fixed, ownership-checked loopback RPC.

This deliberately avoids factory REST getters: their lazy initialization can
launch/recover download clients or refresh authentication. No secret is read.
"""
import json
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
REASONS = {
    "proc_unavailable": "无法读取下载服务的进程现场。",
    "not_listening": "没有发现已运行的原厂 aria2 下载监听。",
    "owner_unverified": "下载监听无法关联到已核验的 aria2c-nox 进程。",
    "ambiguous": "下载监听存在多个执行者，无法确认来源。",
    "identity_changed": "查询期间下载进程或监听身份发生变化。",
    "auth_required": "下载 RPC 需要原厂授权；未读取或刷新令牌。",
    "rpc_error": "原厂下载 RPC 返回错误；未执行登录、恢复或重试。",
    "invalid_response": "下载 RPC 的返回格式不符合受限只读协议。",
    "limit_exceeded": "下载快照超过诊断大小或条目上限，未返回不完整数据。",
    "timeout": "下载只读查询超过时间限制。",
    "service_unavailable": "已观察到的下载监听当前无法连接。",
    "transport_error": "下载只读连接未能完成。",
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


def _http_rpc(payload, deadline, clock, socket_factory):
    """Two fixed POST shapes only; reject redirects, compression and streaming.

    A monotonic deadline applies before every socket operation, so a server
    sending tiny chunks cannot keep the diagnostic alive indefinitely.
    """
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    request = (f"POST {PATH} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
               "Content-Type: application/json\r\nAccept: application/json\r\n"
               "Accept-Encoding: identity\r\nConnection: close\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
    sock = socket_factory((HOST, PORT), timeout=_remaining(deadline, clock))
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
        ceiling = 4096 if payload["method"] == "aria2.getVersion" else MAX_BODY
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
        try:
            return json.loads(content.decode("utf8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise _Failure("invalid_response") from None
    finally:
        sock.close()


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


def _identity(check, root):
    value = check(root=root, port=PORT, comm="aria2c-nox")
    if not isinstance(value, dict):
        raise _Failure("owner_unverified")
    if value.get("available") is not True or value.get("status") != "verified":
        status = value.get("status")
        raise _Failure(status if status in ("proc_unavailable", "not_listening", "owner_unverified", "ambiguous") else "owner_unverified")
    pid, ticks, inode = value.get("pid"), value.get("start_ticks"), value.get("socket_inode")
    if type(pid) is not int or pid <= 0 or type(ticks) is not int or ticks <= 0 or not isinstance(inode, str) or not re.fullmatch(r"[0-9]{1,24}", inode):
        raise _Failure("owner_unverified")
    return pid, ticks, inode


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


def collect(root="/", *, ownership_check=None, socket_factory=None, monotonic=None):
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
