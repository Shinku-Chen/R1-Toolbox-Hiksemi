"""Bounded read-only access to the vendor's current in-memory storage names.

Only the two statically verified list getters are sent to /var/tmp/disk. A
single-disk name requires agreement between both vendor lists and the current
physical disk's independently collected serial; RAID names remain space names.
"""

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import time


MAX_RESPONSE = 131072
MAX_ROWS = 256
MAX_MEMBERS = 128
MAX_NAME = 128
MAX_SERIAL = 512
NAME_SOURCE = "hiksemi_storage_socket"
_DEVICE = re.compile(r"(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)\Z")
_ZONE = re.compile(r"[A-Za-z0-9_.+-]{1,128}\Z")
_GETTERS = ("get_disk_list;", "get_space_list;")
_ERRORS = {
    "socket_missing": "原厂存储只读 socket 尚不存在",
    "socket_invalid": "原厂存储只读 socket 身份不受支持",
    "permission_denied": "原厂存储只读 socket 或暂存目录权限不足",
    "connect_failed": "原厂存储只读 socket 连接失败",
    "timeout": "原厂存储只读接口响应超时",
    "response_too_large": "原厂存储只读接口响应超过大小限制",
    "frame_incomplete": "原厂存储只读接口响应缺少完整 NUL 结束标记",
    "frame_extra": "原厂存储只读接口响应包含额外数据",
    "invalid_utf8": "原厂存储只读接口响应不是有效 UTF-8",
    "invalid_json": "原厂存储只读接口响应不是有效 JSON",
    "schema_invalid": "原厂存储只读列表结构不受支持",
    "vendor_error": "原厂存储只读接口返回失败标记",
    "runtime_unsafe": "原厂存储接口暂存目录不安全或路径过长",
    "transport_error": "原厂存储只读接口传输失败",
}


class _StorageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code])


def _value(value, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit:
        return None
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value if value or allow_empty else None


def _device(value):
    return value if isinstance(value, str) and _DEVICE.fullmatch(value) else None


def _port(value):
    return value if type(value) is int and 0 <= value <= 65535 else None


def _zone(value):
    # A zone is a vendor partition identifier, never a path or a guessed parent.
    return value if isinstance(value, str) and _ZONE.fullmatch(value) else None


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _StorageError("invalid_json")
        result[key] = value
    return result


def _response_list(raw):
    def invalid_constant(_):
        raise _StorageError("invalid_json")
    try:
        value = json.loads(raw, object_pairs_hook=_json_object, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as error:
        raise _StorageError("invalid_json") from error
    rows = value.get("info") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        raise _StorageError("schema_invalid")
    return rows


def _normalize_disks(rows):
    return [{"disk_name": _device(row.get("disk_name")),
             "disk_port": _port(row.get("disk_port")),
             "disk_serial": _value(row.get("disk_serial"), MAX_SERIAL)} for row in rows]


def _normalize_spaces(rows):
    result = []
    for row in rows:
        members = row.get("member")
        valid_members = (isinstance(members, list) and len(members) <= MAX_MEMBERS
                         and all(isinstance(member, dict) for member in members))
        result.append({"space_name": _value(row.get("space_name"), MAX_NAME),
                       "space_disk_id": _value(row.get("space_disk_id"), MAX_RESPONSE - 1),
                       "is_raid": row.get("is_raid") if type(row.get("is_raid")) is int and row["is_raid"] in (0, 1) else None,
                       "port": _port(row.get("port")),
                       "member": [{"port": _port(member.get("port")),
                                   "disk_name": _zone(member.get("disk_name")),
                                   "root_name": _device(member.get("root_name")),
                                   "disk_id": _value(member.get("disk_id"), MAX_SERIAL)}
                                  for member in members] if valid_members else None})
    return result


class VendorStorageClient:
    """Inert until snapshot(); no command, argument or target comes from HTTP."""

    def __init__(self, root="/", runtime_dir="/run/r1-toolbox/storage", socket_factory=None):
        self.root = Path(root).resolve()
        self.runtime_dir = Path(runtime_dir)
        self.socket_factory = socket_factory or socket.socket

    def _request(self, command):
        if command not in _GETTERS:
            raise ValueError("仅支持固定的原厂存储只读查询")
        target = self.root / "var/tmp/disk"
        resolved = target.resolve()
        if self.root not in resolved.parents or not stat.S_ISSOCK(target.lstat().st_mode):
            raise _StorageError("socket_invalid")
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.runtime_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid())):
            raise _StorageError("runtime_unsafe")
        directory = self.runtime_dir.resolve()
        endpoint = directory / ("disk-" + secrets.token_hex(8) + ".sock")
        if len(os.fsencode(endpoint)) >= 104:
            raise _StorageError("runtime_unsafe")
        identity = None
        client = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic() + 2
            client.settimeout(2)
            client.bind(str(endpoint))
            info = endpoint.lstat()
            identity = (info.st_dev, info.st_ino)
            os.chmod(endpoint, 0o700)
            client.connect(str(target))
            # The storage daemon expects no prefix, value or trailing NUL.
            client.sendall(command.encode("ascii"))
            result = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("原厂存储只读接口响应超时")
                client.settimeout(remaining)
                part = client.recv(min(8192, MAX_RESPONSE + 1 - len(result)))
                if not part:
                    break
                result.extend(part)
                if len(result) > MAX_RESPONSE:
                    raise _StorageError("response_too_large")
            head, separator, tail = bytes(result).partition(b"\0")
            if not head or not separator:
                raise _StorageError("frame_incomplete")
            if tail:
                raise _StorageError("frame_extra")
            if head.strip() == b"-1":
                raise _StorageError("vendor_error")
            return _response_list(head.decode("utf-8", "strict"))
        finally:
            client.close()
            try:
                info = endpoint.lstat()
                if identity == (info.st_dev, info.st_ino) and stat.S_ISSOCK(info.st_mode):
                    endpoint.unlink()
            except OSError:
                pass

    def snapshot(self):
        """Return only the fields needed for matching; never vendor paths/media."""
        result = {"available": False, "reason": None, "error_code": None, "failed_getter": None,
                  "source": NAME_SOURCE, "disks": [], "spaces": []}
        getter = _GETTERS[0]
        try:
            disks = self._request(getter)
            getter = _GETTERS[1]
            spaces = self._request(getter)
            result.update(available=True, disks=_normalize_disks(disks), spaces=_normalize_spaces(spaces))
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            code = (error.code if isinstance(error, _StorageError) else
                    "timeout" if isinstance(error, TimeoutError) else
                    "socket_missing" if isinstance(error, FileNotFoundError) else
                    "permission_denied" if isinstance(error, PermissionError) else
                    "connect_failed" if isinstance(error, ConnectionError) else
                    "invalid_utf8" if isinstance(error, UnicodeError) else "transport_error")
            result.update(reason=_ERRORS[code], error_code=code, failed_getter=getter)
        return result


def read_storage_snapshot(root="/", runtime_dir="/run/r1-toolbox/storage", socket_factory=None):
    """Internal fixed-getter sampling; raw serials are for matching, not public UI."""
    return VendorStorageClient(root, runtime_dir, socket_factory).snapshot()


def read_disk_names(serials, storage_snapshot=None, *, client=None):
    """Strictly join one snapshot to current serials; no fallback to migration DB.

    A supplied snapshot is reused without additional IPC. Unknown or conflicting
    per-disk identities stay null even when both list getters were successful.
    """
    empty = {name: {"user_name": None, "space_names": [], "name_source": None} for name in serials}
    snapshot = storage_snapshot if storage_snapshot is not None else (client or VendorStorageClient()).snapshot()
    if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
        code = snapshot.get("error_code") if isinstance(snapshot, dict) else None
        return {"available": False, "reason": _ERRORS.get(code, "原厂存储只读列表暂不可确认"),
                "error_code": code if code in _ERRORS else None, "items": empty}
    disks, spaces = snapshot.get("disks"), snapshot.get("spaces")
    if (not isinstance(disks, list) or not isinstance(spaces, list)
            or len(disks) > MAX_ROWS or len(spaces) > MAX_ROWS
            or any(not isinstance(row, dict) for row in [*disks, *spaces])):
        return {"available": False, "reason": "原厂存储列表结构不受支持", "items": empty}
    # Also validate injected snapshots: callers may reuse data but cannot bypass
    # the same bounded scalar/schema checks as a fresh fixed-getter response.
    disks, spaces = _normalize_disks(disks), _normalize_spaces(spaces)
    cleaned = {name: _value(serial, MAX_SERIAL) for name, serial in serials.items() if _device(name)}
    local_counts = Counter(value for value in cleaned.values() if value)
    physical = {serial: name for name, serial in cleaned.items() if serial and local_counts[serial] == 1}
    names = Counter(row.get("disk_name") for row in disks if _device(row.get("disk_name")))
    ports = Counter(row.get("disk_port") for row in disks if _port(row.get("disk_port")) is not None)
    vendor_serials = Counter(row.get("disk_serial") for row in disks if _value(row.get("disk_serial"), MAX_SERIAL))
    vendor = {row["disk_serial"]: row for row in disks
              if _device(row.get("disk_name")) and _port(row.get("disk_port")) is not None
              and _value(row.get("disk_serial"), MAX_SERIAL) == row.get("disk_serial")
              and names[row["disk_name"]] == ports[row["disk_port"]] == vendor_serials[row["disk_serial"]] == 1}
    candidates, raids = defaultdict(list), defaultdict(list)
    blocked = set()
    for space in spaces:
        members = space.get("member")
        affected = set()
        if isinstance(members, list):
            affected.update(physical[member["disk_id"]] for member in members
                            if isinstance(member, dict) and member.get("disk_id") in physical)
            # A name may reject inconsistent records; it never assigns a label.
            affected.update(member["root_name"] for member in members
                            if isinstance(member, dict) and member.get("root_name") in empty)
        valid = (type(space.get("is_raid")) is int and space["is_raid"] in (0, 1)
                 and isinstance(members, list) and 0 < len(members) <= MAX_MEMBERS
                 and all(isinstance(member, dict) for member in members)
                 and len({member.get("disk_id") for member in members}) == len(members))
        if valid:
            valid = all(member.get("disk_id") in vendor
                        and _zone(member.get("disk_name")) is not None
                        and member.get("root_name") == vendor[member["disk_id"]]["disk_name"]
                        and type(member.get("port")) is int
                        and member["port"] == vendor[member["disk_id"]]["disk_port"]
                        and physical.get(member["disk_id"]) == member["root_name"]
                        for member in members)
        if valid and space["is_raid"] == 0:
            valid = (len(members) == 1 and type(space.get("port")) is int
                     and space["port"] == members[0]["port"])
        if not valid:
            blocked.update(affected)
            continue
        label = _value(space.get("space_name"), MAX_NAME)
        for member in members:
            name = physical.get(member["disk_id"])
            if name:
                if space["is_raid"] == 1:
                    raids[name].append(label)
                else:
                    candidates[name].append((label, member["disk_name"]))
    for name in empty:
        basic, shared = candidates[name], raids[name]
        if name not in blocked:
            # The factory lists both private/data zones of a basic disk. A
            # shared physical identity may have one name across distinct zones;
            # repeated zone records or different/unknown names stay ambiguous.
            labels = {label for label, _ in basic}
            zones = [zone for _, zone in basic]
            user_name = next(iter(labels)) if (len(labels) == 1 and None not in labels
                        and len(set(zones)) == len(zones) and not shared) else None
            space_names = sorted({label for label in shared if label}) if len(shared) == len(set(shared)) else []
            empty[name] = {"user_name": user_name, "space_names": space_names,
                           "name_source": NAME_SOURCE if user_name or space_names else None}
    return {"available": True, "reason": None, "items": empty}
