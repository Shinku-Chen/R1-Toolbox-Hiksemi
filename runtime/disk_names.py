"""Read cached HikSemi space names without contacting disks or vendor services.

Only an unambiguous basic-space/physical-serial match is a disk user_name.
RAID names describe a shared space and stay in the separate space_names field.
"""

from collections import Counter, defaultdict
from pathlib import Path
import sqlite3
import stat
import time


MAX_DB_BYTES = 4 * 1024 * 1024
MAX_ROWS = 256
MAX_NAME = 128
MAX_SERIAL = 512
QUERY_SECONDS = 0.075
NAME_SOURCE = "hiksemi_space_db"


def _plain_path(path):
    """Reject symlinks/reparse points in the fixed configuration path."""
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("名称数据库路径包含符号链接")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DB_BYTES:
        raise ValueError("名称数据库不是有界普通文件")
    return info


def _no_pending_journal(path):
    # immutable guarantees zero sidecar writes, but ignores journals. Refuse a
    # pending transaction rather than interpreting an old name as current.
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise ValueError("名称数据库存在未合并事务")


def _unchanged(before, after):
    return all(getattr(before, key) == getattr(after, key)
               for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _value(value, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("名称数据库字段类型或长度不受支持")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("名称数据库包含无效字符")
    if not value and not allow_empty:
        raise ValueError("名称数据库缺少关联字段")
    return value


def _label(value):
    # The vendor also stores a single 0x04 byte for an unnamed space. Never
    # publish sentinel/control characters, truncate a name, or invent a label.
    try:
        return _value(value, MAX_NAME, allow_empty=True) or None
    except ValueError:
        return None


def read_disk_names(serials, config_root="/tmp/nand_config"):
    """Return names keyed by physical ID; serials are internal, never published.

    config_root exists only for isolated fixtures. Production always uses the
    fixed NAND path; neither HTTP input nor database contents can choose a path.
    """
    empty = {name: {"user_name": None, "space_names": [], "name_source": None}
             for name in serials}
    unavailable = lambda reason: {"available": False, "reason": reason, "items": empty}
    path = Path(config_root).absolute() / "disk_space.db"
    connection = None
    try:
        before = _plain_path(path)
        _no_pending_journal(path)
        started = time.monotonic()
        # immutable avoids creating or updating -shm/-wal in the vendor folder.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True,
                                     timeout=0.025)
        connection.execute("PRAGMA query_only=ON")
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
        connection.set_progress_handler(
            lambda: int(time.monotonic() - started > QUERY_SECONDS), 100)
        schema = connection.execute(
            "SELECT type FROM sqlite_master WHERE name='space_table'").fetchall()
        if schema != [("table",)]:
            raise ValueError("名称数据库缺少受支持的数据表")
        records = connection.execute(
            "SELECT type,name,member,disk_id FROM space_table LIMIT ?", (MAX_ROWS + 1,)).fetchall()
        if len(records) > MAX_ROWS or time.monotonic() - started > QUERY_SECONDS:
            raise ValueError("名称数据库查询超过限制")
        _no_pending_journal(path)
        if not _unchanged(before, _plain_path(path)):
            raise ValueError("名称数据库采集期间发生变化")

        cleaned = {name: value.strip() for name, value in serials.items()
                   if isinstance(value, str) and 0 < len(value.strip()) <= MAX_SERIAL
                   and not any(ord(char) < 32 or ord(char) == 127 for char in value.strip())}
        counts = Counter(cleaned.values())
        physical = {serial: name for name, serial in cleaned.items() if counts[serial] == 1}
        candidates = defaultdict(set)
        spaces = defaultdict(set)
        for kind, label, member, disk_ids in records:
            kind = _value(kind, 32)
            label = _label(label)
            member = _value(member, 512, allow_empty=True)
            disk_ids = _value(disk_ids, 2048)
            # Native RAID IDs/ports are colon-separated strings, not JSON.
            ids = disk_ids.split(":")
            if any(not value or value != value.strip() or len(value) > MAX_SERIAL for value in ids):
                raise ValueError("名称数据库磁盘关联格式不受支持")
            if kind == "basic" and len(ids) == 1 and ":" not in member:
                if ids[0] in physical:
                    candidates[physical[ids[0]]].add(label)
            elif kind == "raid" or len(ids) > 1:
                if label:
                    for serial in ids:
                        if serial in physical:
                            spaces[physical[serial]].add(label)
        for name in empty:
            labels = candidates[name]
            user_name = next(iter(labels)) if len(labels) == 1 and not spaces[name] else None
            space_names = sorted(spaces[name])[:MAX_ROWS]
            empty[name] = {"user_name": user_name, "space_names": space_names,
                           "name_source": NAME_SOURCE if user_name or space_names else None}
        return {"available": True, "reason": None, "items": empty}
    except FileNotFoundError:
        return unavailable("原厂名称数据库尚不存在")
    except (OSError, ValueError, sqlite3.Error):
        return unavailable("原厂名称数据库暂不可只读确认")
    finally:
        if connection is not None:
            connection.close()
