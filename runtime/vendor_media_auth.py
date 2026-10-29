"""Read one existing factory administrator session without changing its DB.

The owner identity and vendor startup window are fixed. This module neither
logs in nor retries another account/session after an authentication rejection.
Only the explicitly safe status dictionary may leave the private media probe.
"""

import hashlib
from pathlib import Path
import re
import stat
import time
import unicodedata

try:
    import sqlite3
except ImportError:  # Some factory Python builds do not include _sqlite3.
    sqlite3 = None


OWNER_ID = 100000
SESSION_WINDOW = 21
CONNECT_TIMEOUT = 0.2
READ_DEADLINE = 1.0
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_SECONDS = re.compile(r"[0-9]{1,12}\Z")
_RELATIVE_DB = "tmp/nand_config/USER_AUTH_DB_101"
_COLUMNS = {"tb_user_auth": frozenset(("user_id", "user_type")),
            "tb_terminal_token": frozenset(("user_mkey", "user_id", "source_id", "source_type",
                                            "token", "login_time"))}
_REASONS = {
    "ready": "已只读确认原厂管理员的既有会话；凭据仅供本次内部媒体探测",
    "db_missing": "原厂会话数据库不存在；未创建或登录",
    "sqlite_unavailable": "宿主 Python 未提供 sqlite3；无法只读确认原厂会话",
    "permission_denied": "原厂会话数据库不可只读访问",
    "invalid_database": "原厂会话数据库格式或会话字段无法确认",
    "identity_unconfirmed": "无法唯一确认原厂管理员身份",
    "no_session": "原厂启动会话窗口内没有可用的管理员会话",
    "ambiguous_session": "原厂管理员会话存在歧义；未尝试其他凭据",
    "busy": "原厂会话数据库忙或只读查询超过时限",
    "data_unavailable": "原厂会话暂不可只读确认",
}


class _Session:
    """Internal credential carrier; slots and repr prevent accidental dumping."""

    __slots__ = ("user_id", "source_id", "login_time", "base_token")

    def __init__(self, user_id, source_id, login_time, base_token):
        self.user_id = user_id
        self.source_id = source_id
        self.login_time = login_time
        self.base_token = base_token

    def __repr__(self):
        return "_Session(user_id={})".format(self.user_id)

    def derive_access_token(self, ts_int=None):
        """Use the NAS clock and the vendor's MD5(token + decimal seconds)."""
        seconds = int(time.time()) if ts_int is None else ts_int
        if type(seconds) is not int or seconds < 0:
            raise ValueError("invalid token timestamp")
        timestamp = str(seconds)
        proof = hashlib.md5((self.base_token + timestamp).encode("ascii")).hexdigest()
        return "{}:{}:{}".format(proof, timestamp, self.user_id)


class _InvalidDatabase(ValueError):
    pass


def _status(status, *, confirmed=False, checked=0, candidates=0):
    """Explicit whitelist: never include source IDs, token hashes or exceptions."""
    return {"available": status == "ready", "status": status, "reason": _REASONS[status],
            "user_id": OWNER_ID if confirmed else None, "records_checked": checked,
            "candidate_count": candidates,
            "selection": "newest_in_vendor_window" if status == "ready" else None}


def _path(root):
    # Parent links/mounts are legitimate on the NAS. A fixture must still keep
    # the resolved fixed path inside its root, and the final file cannot link.
    base = Path(root).resolve(strict=True)
    path = base / _RELATIVE_DB
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(info.st_mode)):
        raise _InvalidDatabase("not a regular database")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError:
        raise _InvalidDatabase("fixture path escapes root") from None
    return path, resolved, info


def _same_file(before, after):
    # Ordinary database updates are allowed: the SQLite transaction supplies
    # the snapshot. Only pathname replacement/links invalidate its identity.
    return (stat.S_ISREG(after.st_mode)
            and not getattr(after, "st_file_attributes", 0) & 0x400
            and before.st_dev == after.st_dev and before.st_ino == after.st_ino)


def _source(value):
    if (type(value) is not str or not value
            or any(unicodedata.category(char) in ("Cc", "Cs") for char in value)
            or len(value.encode("utf-8")) > 256):
        raise _InvalidDatabase("invalid source identity")
    return value


def _login_seconds(value):
    # Factory DDL stores decimal seconds in VARCHAR64. Integers are accepted
    # for older SQLite fixtures/firmware without accepting floating coercion.
    if type(value) is int and 0 <= value <= 999999999999:
        return value
    if type(value) is str and _SECONDS.fullmatch(value):
        return int(value)
    raise _InvalidDatabase("invalid login time")


def _rows(rows):
    candidates = []
    for row in rows:
        if len(row) != 6:
            raise _InvalidDatabase("invalid session row")
        key, user_id, source_id, source_type, token, login_time = row
        if (type(key) is not int or key <= 0 or type(user_id) is not int or user_id != OWNER_ID
                or type(source_type) is not int or not 0 <= source_type <= 2147483647
                or type(token) is not str):
            raise _InvalidDatabase("invalid session field types")
        source_id, login_time = _source(source_id), _login_seconds(login_time)
        if _TOKEN.fullmatch(token):
            candidates.append((key, user_id, source_id, source_type, token, login_time))
    if not candidates:
        return None, "no_session", 0
    newest = max(row[5] for row in candidates)
    newest_rows = [row for row in candidates if row[5] == newest]
    # The startup query does not define a tie order; do not invent one.
    choices = {(row[1], row[2], row[3], row[4], row[5]) for row in newest_rows}
    if len(choices) != 1:
        return None, "ambiguous_session", len(candidates)
    selected = newest_rows[0]
    # A duplicate source with a different token/timestamp can map differently
    # in factory memory. Do not send either possible credential.
    for row in rows:
        if row[2] == selected[2] and (row[1], row[3], row[4], _login_seconds(row[5])) != (
                selected[1], selected[3], selected[4], selected[5]):
            return None, "ambiguous_session", len(candidates)
    return _Session(selected[1], selected[2], selected[5], selected[4]), "ready", len(candidates)


def _sqlite_status(error):
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 255 if type(code) is int else None
    if primary in (5, 6, 9):  # SQLITE_BUSY, SQLITE_LOCKED, SQLITE_INTERRUPT.
        return "busy"
    if primary in (3, 8, 14, 23):  # PERM, READONLY, CANTOPEN, AUTH.
        return "permission_denied"
    if primary is None:
        # Python before 3.11 has no sqlite_errorcode. These comparisons are
        # private classifications only; exception text is never returned.
        text = str(error).lower()
        if text in ("database is locked", "database table is locked", "interrupted"):
            return "busy"
        if text in ("unable to open database file", "attempt to write a readonly database",
                    "not authorized", "authorization denied"):
            return "permission_denied"
    if isinstance(error, sqlite3.DatabaseError):
        return "invalid_database"
    return "data_unavailable"


def _authorize(action, table, column, database, caller):
    # Reject schema indirection to password/name fields, functions and attached
    # databases even when an unexpected DB view uses an otherwise allowed name.
    if action == sqlite3.SQLITE_READ:
        allowed = database == "main" and column in _COLUMNS.get(table, ())
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def read_session(root="/"):
    """Return (private session or None, safe status); root is fixture-only.

    Read the same oldest-first 21-row owner window loaded by factory startup,
    then select its newest valid token. No login, repairs, DB writes, alternate
    user selection, network access or automatic authentication retry occurs.
    """
    if sqlite3 is None:
        return None, _status("sqlite_unavailable")
    connection = None
    confirmed, checked, candidates = False, 0, 0
    try:
        path, resolved, before = _path(root)
        deadline = time.monotonic() + READ_DEADLINE
        connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True,
                                     timeout=CONNECT_TIMEOUT, isolation_level=None)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorize)
        connection.execute("BEGIN")
        owners = connection.execute(
            "SELECT user_id, user_type FROM tb_user_auth WHERE user_id = ? LIMIT 2", (OWNER_ID,)
        ).fetchall()
        if (len(owners) != 1 or type(owners[0][0]) is not int or owners[0][0] != OWNER_ID
                or type(owners[0][1]) is not int or owners[0][1] != 0):
            return None, _status("identity_unconfirmed")
        confirmed = True
        rows = connection.execute(
            "SELECT user_mkey, user_id, source_id, source_type, token, login_time "
            "FROM tb_terminal_token WHERE user_id = ? ORDER BY login_time LIMIT 21", (OWNER_ID,)
        ).fetchall()
        checked = len(rows)
        if time.monotonic() >= deadline:
            return None, _status("busy", confirmed=confirmed, checked=checked)
        session, status, candidates = _rows(rows)
        if not _same_file(before, path.lstat()) or path.resolve(strict=True) != resolved:
            raise _InvalidDatabase("database path replaced")
        return session, _status(status, confirmed=confirmed, checked=checked, candidates=candidates)
    except FileNotFoundError:
        status = "db_missing"
    except PermissionError:
        status = "permission_denied"
    except _InvalidDatabase:
        status = "invalid_database"
    except sqlite3.Error as error:
        status = _sqlite_status(error)
    except Exception:
        # Neither arbitrary exception text nor partially read credentials leave.
        status = "data_unavailable"
    finally:
        if connection is not None:
            try:
                connection.close()  # Closing a read transaction releases it; no COMMIT/write.
            except Exception:
                pass
    return None, _status(status, confirmed=confirmed, checked=checked, candidates=candidates)
