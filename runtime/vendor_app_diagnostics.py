"""Read the factory application's existing NAND cache; never call its getters.

Build 260907 appcenter get_install_progress can write app_center.conf and sync
plugins. Reading that already-written JSON is a narrower diagnostic source.
The cache is partial historical installation metadata, not live running state.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import unicodedata


SOURCE = "histor.app_center.conf"
MAX_CACHE_BYTES = 1024 * 1024
MAX_APPS = 256
MAX_TEXT_BYTES = 63


class _CacheError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _CacheError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise _CacheError("nonstandard JSON constant")


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
            raise _CacheError("unsafe cache path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CACHE_BYTES:
        raise _CacheError("cache file type or size")
    return info


def _text(value, *, empty=False):
    # Only named/version fields are projected; paths and URLs are not labels.
    if (not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES
            or any(unicodedata.category(char) in ("Cc", "Cs") for char in value)
            or any(char in value for char in ("/", "\\"))
            or not empty and not value.strip()):
        raise _CacheError("unsupported application label")
    return value or None


def _project(value, modified):
    rows = value.get("list") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_APPS:
        raise _CacheError("unsupported cache list")
    apps, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _CacheError("unsupported application entry")
        name = _text(row.get("module_name"))
        version = _text(row.get("version"), empty=True)
        status_code = row.get("status")
        if type(status_code) is not int or status_code not in (0, 4) or name in seen:
            raise _CacheError("ambiguous or unsupported cached status")
        seen.add(name)
        apps.append({"name": name, "version": version,
                     "installation_status_code": status_code, "running": None})
    # The vendor counts every in-memory node but serializes status 0/4 only.
    # Therefore its stored `count` is neither a completeness proof nor len(list).
    return {"apps": apps, "cached_entries": len(apps), "live": False,
            "complete": False,
            "cache_modified_at": datetime.fromtimestamp(modified, timezone.utc).isoformat()}


def collect(root="/"):
    """Return a bounded cached diagnostic; root is internal fixture injection.

    No auth token, user ID, installation path, URL, raw config or exception text
    leaves this function. A missing cache does not become an empty success, and
    no network request, vendor executable or directory discovery fills the gap.
    """
    result = {"available": False, "status": "cache_invalid", "source": SOURCE,
              "reason": "原厂应用缓存不可只读确认；已跳过会同步或写配置的原厂查询", "data": None}
    descriptor = None
    try:
        path = Path(root).absolute() / "tmp/nand_config/app_center.conf"
        before = _path_info(path)
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same(before, opened):
            raise _CacheError("cache replaced")
        raw = os.read(descriptor, MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES or len(raw) != opened.st_size:
            raise _CacheError("cache read length changed")
        if not _same(opened, os.fstat(descriptor)) or not _same(opened, _path_info(path)):
            raise _CacheError("cache changed during read")
        value = json.loads(raw.rstrip(b"\0").decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        data = _project(value, opened.st_mtime)
        return {"available": True, "status": "cached", "source": SOURCE,
                "reason": "仅有原厂应用持久缓存；不代表完整实时清单或当前运行状态", "data": data}
    except FileNotFoundError:
        result.update(status="cache_missing", reason="原厂应用持久缓存不存在；未调用会同步或写配置的原厂查询")
    except PermissionError:
        result.update(status="permission_denied", reason="原厂应用持久缓存不可读取")
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result
