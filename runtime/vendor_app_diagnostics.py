"""Read Toolbox's explicit app snapshots or the existing factory NAND cache.

Build 260907 appcenter get_install_progress can write app_center.conf and sync
plugins. Reading that already-written JSON is a narrower diagnostic source.
The cache is partial historical installation metadata, not live running state.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unicodedata


SOURCE = "histor.app_center.conf"
PROBE_SOURCE = "histor.applycenter.get_apply_list"
SNAPSHOT_SOURCE = "toolbox.application_snapshot"
SNAPSHOT_FILE = "application-snapshot.json"
MAX_CACHE_BYTES = 1024 * 1024
MAX_APPS = 256
MAX_TEXT_BYTES = 63
REJECTION_CODES = ("unsupported_application_label", "unsupported_application_entry",
                   "duplicate_application_name", "unsupported_cached_status")


class _CacheError(ValueError):
    """A fixed local classification, never a pathname or upstream message."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _CacheError("json_duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise _CacheError("json_nonstandard_constant")


def _same(before, after):
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    # Windows path/fd stat disagree about deprecated ctime semantics.
    if os.name != "nt":
        keys += ("st_ctime_ns",)
    return all(getattr(before, key) == getattr(after, key) for key in keys)


def _path_info(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _CacheError("unsafe_cache_path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CACHE_BYTES:
        raise _CacheError("cache_file_bounds")
    return info


def _text(value, *, empty=False):
    # Only named/version fields are projected; paths and URLs are not labels.
    if (not isinstance(value, str)
            or any(unicodedata.category(char) in ("Cc", "Cs") for char in value)
            or any(char in value for char in ("/", "\\"))
            or not empty and not value.strip()
            or len(value.encode("utf-8")) > MAX_TEXT_BYTES):
        raise _CacheError("unsupported_application_label")
    return value or None


def _project(value, modified):
    rows = value.get("list") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_APPS:
        raise _CacheError("unsupported_cache_list")
    # The vendor counts every in-memory node but serializes status 0/4 only.
    # Therefore its stored `count` is neither a completeness proof nor len(list).
    return _project_entries(rows, datetime.fromtimestamp(modified, timezone.utc).isoformat(), cached=True)


def _project_entries(rows, sampled_at, *, cached=False):
    """Isolate bad catalog nodes without widening the approved label grammar.

    Rejection summaries contain fixed codes and counts only. A genuinely empty
    list is observable; a nonempty list with no accepted node remains a failure.
    """
    accepted, seen, rejected = [], set(), {}
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise _CacheError("unsupported_application_entry")
            name = _text(row.get("module_name"))
            if name in seen:
                raise _CacheError("duplicate_application_name")
            try:
                version = _text(row.get("version"), empty=True)
            except _CacheError:
                if cached:
                    raise
                version = None
            code = row.get("status")
            if cached and (type(code) is not int or code not in (0, 4)):
                raise _CacheError("unsupported_cached_status")
            seen.add(name)
            accepted.append({"name": name, "version": version,
                             "installation_status_code": code if type(code) is int and code in (0, 4) else None,
                             "running": None})
        except _CacheError as error:
            code = str(error)
            rejected[code] = rejected.get(code, 0) + 1
    if rows and not accepted:
        raise _CacheError(next(iter(rejected)))
    return {"apps": accepted, "cached_entries": len(accepted), "live": False,
            "complete": False, "cache_modified_at": sampled_at,
            "partial": bool(rejected), "rejected_entries": sum(rejected.values()),
            "rejections": [{"code": code, "count": rejected[code]}
                           for code in REJECTION_CODES if code in rejected]}


def _previous_rejections(data, accepted_count):
    """Validate persisted partial metadata; repeated projection cannot erase it."""
    fields = {"partial", "rejected_entries", "rejections"}
    if not fields.intersection(data):
        return {}  # Previously valid snapshots did not have rejection metadata.
    count, rows = data.get("rejected_entries"), data.get("rejections")
    if (not fields <= set(data) or type(data.get("partial")) is not bool
            or type(count) is not int or not 0 <= count <= MAX_APPS - accepted_count
            or not isinstance(rows, list) or len(rows) > len(REJECTION_CODES)):
        raise _CacheError("invalid_application_snapshot")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"code", "count"}
                or row.get("code") not in REJECTION_CODES or row["code"] in result
                or type(row.get("count")) is not int or not 1 <= row["count"] <= MAX_APPS):
            raise _CacheError("invalid_application_snapshot")
        result[row["code"]] = row["count"]
    if sum(result.values()) != count or data["partial"] != (count > 0) or count and not accepted_count:
        raise _CacheError("invalid_application_snapshot")
    return result


def _read_json(path):
    """Read one bounded ordinary file, rejecting replacement during the read."""
    descriptor = None
    try:
        before = _path_info(path)
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same(before, opened):
            raise _CacheError("cache_replaced")
        raw = os.read(descriptor, MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES or len(raw) != opened.st_size:
            raise _CacheError("cache_read_length_changed")
        if not _same(opened, os.fstat(descriptor)) or not _same(opened, _path_info(path)):
            raise _CacheError("cache_changed_during_read")
        try:
            text = raw.rstrip(b"\0").decode("utf-8")
        except UnicodeError:
            raise _CacheError("invalid_utf8") from None
        try:
            value = json.loads(text, object_pairs_hook=_unique_object,
                               parse_constant=_invalid_constant)
        except json.JSONDecodeError:
            raise _CacheError("invalid_json") from None
        return value, opened.st_mtime
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _collect_cache(root="/"):
    """Return a bounded cached diagnostic; root is internal fixture injection.

    No auth token, user ID, installation path, URL, raw config or exception text
    leaves this function. A missing cache does not become an empty success, and
    no network request, vendor executable or directory discovery fills the gap.
    """
    result = {"available": False, "status": "cache_invalid", "source": SOURCE,
              "reason": "原厂应用缓存不可只读确认；未触发原厂查询", "error_code": "cache_invalid", "data": None}
    try:
        path = Path(root).absolute() / "tmp/nand_config/app_center.conf"
        value, modified = _read_json(path)
        data = _project(value, modified)
        return {"available": True, "status": "cached_partial" if data["partial"] else "cached", "source": SOURCE,
                "reason": "部分原厂缓存条目未通过校验，仅保留有效历史条目；不代表当前运行状态" if data["partial"]
                else "仅有原厂应用持久缓存；不代表完整实时清单或当前运行状态", "data": data}
    except FileNotFoundError:
        result.update(status="cache_missing", error_code="cache_missing", reason="原厂应用持久缓存不存在；未触发原厂查询")
    except PermissionError:
        result.update(status="permission_denied", error_code="permission_denied", reason="原厂应用持久缓存不可读取")
    except _CacheError as error:
        result["error_code"] = str(error)
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        pass
    return result


def project_application_list(frame, sampled_at):
    """Project the audited in-memory list, without guessing status enum meanings.

    The native list includes catalog entries, not just installed applications.
    Only status 0/4 are also evidenced in the historical installation cache;
    other status values remain unknown, and no value implies a running process.
    """
    rows = frame.get("list") if isinstance(frame, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_APPS:
        raise _CacheError("unsupported_application_list")
    # count/support_ecloud, IDs, paths, vendor IDs, guest flags and URLs do not
    # escape. A one-time catalog observation is not a complete live inventory.
    return _project_entries(rows, sampled_at)


def _stamp(value):
    if type(value) is not str or len(value) > 40:
        raise _CacheError("invalid_snapshot_timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        raise _CacheError("invalid_snapshot_timestamp") from None
    if timestamp.tzinfo is None:
        raise _CacheError("invalid_snapshot_timestamp")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _snapshot_projection(value):
    """Re-project on both write and read; no raw probe or credential is stored."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or value.get("source") not in (PROBE_SOURCE, SNAPSHOT_SOURCE)
            or type(value.get("available")) is not bool):
        raise _CacheError("invalid_application_snapshot")
    sampled_at = _stamp(value.get("sampled_at"))
    result = {"schema_version": 1, "source": SNAPSHOT_SOURCE, "sampled_at": sampled_at,
              "available": False, "status": "refresh_failed", "error_code": "refresh_failed",
              "reason": "最近一次显式应用刷新失败；未保留为新的成功清单", "data": None}
    if not value["available"]:
        code = value.get("error_code")
        # Import only the fixed set, never an arbitrary source/exception string.
        if type(code) is str and code in APPLICATION_ERRORS:
            result["error_code"] = code
        return result
    if value.get("status") not in ("ok", "partial", "cached", "cached_partial"):
        raise _CacheError("invalid_application_snapshot")
    data = value.get("data")
    rows = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_APPS:
        raise _CacheError("invalid_application_snapshot")
    previous = _previous_rejections(data, len(rows))
    native_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise _CacheError("invalid_application_snapshot")
        native_rows.append({"module_name": row.get("name"),
                            "version": row.get("version") or "",
                            "status": row.get("installation_status_code")})
    projected = project_application_list({"list": native_rows}, sampled_at)
    for row in projected["rejections"]:
        previous[row["code"]] = previous.get(row["code"], 0) + row["count"]
    projected.update(partial=bool(previous), rejected_entries=sum(previous.values()),
                     rejections=[{"code": code, "count": previous[code]}
                                 for code in REJECTION_CODES if code in previous])
    result.update(available=True, status="cached_partial" if projected["partial"] else "cached", error_code=None,
                  reason="部分原厂目录条目未通过校验，仅保留有效快照；不代表当前运行状态" if projected["partial"]
                  else "仅为最近一次显式刷新的应用目录快照；不代表完整清单或当前运行状态",
                  data=projected)
    return result


# These codes may be persisted or returned; their text never comes from upstream.
APPLICATION_ERRORS = frozenset((
    "credential_read_failed", "credential_unavailable", "token_unavailable",
    "tls_unavailable", "certificate_unavailable", "certificate_invalid", "certificate_mismatch",
    "timeout", "transport_failed", "headers_too_large", "body_too_large", "invalid_http",
    "unsupported_encoding", "unsupported_content_type", "invalid_multipart", "incomplete_response",
    "invalid_json", "redirect_refused", "http_error", "invalid_transport_result",
    "native_code_unavailable", "native_request_rejected", "unsupported_application_list",
    "unsupported_application_entry", "unsupported_application_label", "duplicate_application_name",
    "refresh_failed", "invalid_application_snapshot",
))


def record_application_snapshot(state_dir, probe):
    """Atomically persist this attempt, including failure, in Toolbox state only.

    The owner-controlled state directory is an internal argument. Failure
    replaces any earlier success, so stale results cannot appear newly valid.
    """
    temporary = None
    try:
        value = _snapshot_projection(probe)
        folder = Path(state_dir).absolute()
        # Check existing ancestors before mkdir, so even the internal fixture
        # path cannot create a directory through a pre-existing link.
        for part in reversed((folder, *folder.parents)):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise _CacheError("unsafe_snapshot_path")
        folder.mkdir(parents=True, exist_ok=True)
        before = folder.stat()
        destination = folder / SNAPSHOT_FILE
        if destination.exists() or destination.is_symlink():
            info = destination.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise _CacheError("unsafe_snapshot_path")
        descriptor, temporary = tempfile.mkstemp(prefix=".application-", suffix=".tmp", dir=folder)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        after = folder.lstat()
        if (not stat.S_ISDIR(after.st_mode) or stat.S_ISLNK(after.st_mode)
                or getattr(after, "st_file_attributes", 0) & 0x400
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise _CacheError("unsafe_snapshot_path")
        os.replace(temporary, destination)
        temporary = None
        return True
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def collect(root="/", *, state_dir=None):
    """Passively read the last explicit attempt, then the original NAND cache.

    No signing/network/native getter occurs here. state_dir and root are
    internal fixture/configuration values, never caller-selected file paths.
    """
    snapshot = None
    if state_dir is not None:
        try:
            value, _ = _read_json(Path(state_dir).absolute() / SNAPSHOT_FILE)
            snapshot = _snapshot_projection(value)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
            snapshot = {"available": False, "status": "snapshot_invalid", "source": SNAPSHOT_SOURCE,
                        "error_code": "invalid_application_snapshot", "data": None,
                        "reason": "Toolbox 应用快照不可确认；未自动重试原厂查询"}
        if snapshot is not None and snapshot["available"]:
            return snapshot
    cached = _collect_cache(root)
    if snapshot is None:
        return cached
    if cached["available"]:
        cached.update(reason="最近一次显式刷新失败；以下仅为原厂部分历史缓存",
                      error_code=snapshot["error_code"], latest_attempt_at=snapshot.get("sampled_at"))
        return cached
    snapshot["cache_error_code"] = cached.get("error_code")
    return snapshot
