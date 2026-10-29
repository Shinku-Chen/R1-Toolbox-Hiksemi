"""Project only the factory device nickname from its existing configuration.

Build 260907 json_conf_get_dev_name reads dev_info.dev_name from config.txt.
This bounded passive read does not execute a factory getter, return unrelated
configuration, or fall back to a Linux hostname/model as a user-set name.
"""

import json
import os
from pathlib import Path
import stat
import unicodedata


MAX_CONFIG_BYTES = 1024 * 1024
MAX_NAME_BYTES = 63  # The verified getter uses a 0x40-byte destination.
NAME_SOURCE = "histor.dev_info.dev_name"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonstandard JSON constant")


def _same_file(before, after):
    # Windows path stat and descriptor stat can disagree on deprecated ctime
    # semantics. Linux metadata-change time is an extra mutation fence.
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if os.name != "nt":
        keys += ("st_ctime_ns",)
    return all(getattr(before, key) == getattr(after, key)
               for key in keys)


def _regular_path(path):
    # root is an internal fixture injection, never an HTTP-selected path.
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("unsafe configuration path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CONFIG_BYTES:
        raise ValueError("unbounded configuration file")
    return info


def collect_vendor_device_name(root="/"):
    """Return a nickname-only snapshot for the existing slow collector/cache.

    The result contains no configuration blob, serial, credential, or exception
    text. An unavailable nickname stays None; source identifies the sole verified
    vendor field even when the file or field is not currently available.
    """
    result = {"available": False, "name": None, "source": NAME_SOURCE,
              "reason": "原厂设备名称暂不可用"}
    descriptor = None
    try:
        path = Path(root).absolute() / "tmp/nand_config/config.txt"
        before = _regular_path(path)
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise ValueError("configuration replaced before read")
        raw = os.read(descriptor, MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES or len(raw) != opened.st_size:
            raise ValueError("configuration read length changed")
        if not _same_file(opened, os.fstat(descriptor)) or not _same_file(opened, _regular_path(path)):
            raise ValueError("configuration changed during read")
        config = json.loads(raw.rstrip(b"\0").decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        info = config.get("dev_info") if isinstance(config, dict) else None
        name = info.get("dev_name") if isinstance(info, dict) else None
        if (not isinstance(name, str) or not name.strip()
                or len(name.encode("utf-8")) > MAX_NAME_BYTES
                or any(unicodedata.category(char) in ("Cc", "Cs") for char in name)):
            result["reason"] = "原厂设备名称未设置或格式不受支持"
            return result
        return {"available": True, "name": name, "source": NAME_SOURCE, "reason": None}
    except FileNotFoundError:
        result["reason"] = "原厂设备名称配置不存在"
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result
