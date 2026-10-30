"""Original user-provided update-time rule; the caller owns the transaction.

The rule is deliberately small: it keeps one observed path array per
(id, lid, type) and refreshes metadata.timestamp only when a new INSERT OR
REPLACE carries a longer path array.  There is no TV type filter, no UPDATE
trigger and no cross-type/union baseline.  Those were later experiments and
must not be installed as this plugin's rule.
"""

import re
import sqlite3


RULE_VERSION = 1
GUARD_TABLE = "metadata_path_guard"
TRIGGER_NAMES = ("metadata_guard_insert",)

# Previous enhanced artifacts are recognized only to refuse ownership.
_V2_NAMES = (
    "r1_toolbox_media_sort_baseline_v2",
    "r1_toolbox_media_sort_insert_v2",
    "r1_toolbox_media_sort_update_v2",
)

CREATE_GUARD_SQL = """CREATE TABLE IF NOT EXISTS metadata_path_guard (
  id INTEGER,
  lid INTEGER,
  type INTEGER,
  path TEXT,
  PRIMARY KEY (id, lid, type)
)"""

SEED_SQL = """INSERT OR REPLACE INTO metadata_path_guard (id, lid, type, path)
  SELECT id, lid, type, path FROM metadata"""

INSERT_TRIGGER_SQL = """CREATE TRIGGER IF NOT EXISTS metadata_guard_insert
AFTER INSERT ON metadata
FOR EACH ROW
BEGIN
  UPDATE metadata
     SET timestamp = CAST((julianday('now') - 2440587.5) * 86400000000 AS INTEGER)
   WHERE id = NEW.id AND lid = NEW.lid AND type = NEW.type
     AND EXISTS (
       SELECT 1 FROM metadata_path_guard g
        WHERE g.id = NEW.id AND g.lid = NEW.lid AND g.type = NEW.type
          AND json_array_length(NEW.path) > json_array_length(g.path)
     );
  INSERT OR REPLACE INTO metadata_path_guard (id, lid, type, path)
    VALUES (NEW.id, NEW.lid, NEW.type, NEW.path);
END"""

SCHEMA_SQL = (CREATE_GUARD_SQL, SEED_SQL, INSERT_TRIGGER_SQL)

_GUARD_COLUMNS = (
    (0, "id", "INTEGER", 0, None, 1),
    (1, "lid", "INTEGER", 0, None, 2),
    (2, "type", "INTEGER", 0, None, 3),
    (3, "path", "TEXT", 0, None, 0),
)
_OWNED_NAMES = (GUARD_TABLE,) + TRIGGER_NAMES
_CONFLICT_NAMES = _OWNED_NAMES + _V2_NAMES


class RuleConflictError(RuntimeError):
    """An unknown, partial or enhanced artifact must not be taken over."""


def _normalized_sql(sql):
    """Allow SQLite's removal of IF NOT EXISTS, not arbitrary SQL rewrites."""
    value = re.sub(r"\s+", " ", sql or "").strip().rstrip(";").strip()
    return re.sub(r"^(CREATE (?:TABLE|TRIGGER)) IF NOT EXISTS ", r"\1 ", value)


def _table_matches(con, name, columns, sql):
    row = con.execute(
        "SELECT type, tbl_name, sql FROM main.sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    if (not row or tuple(row[:2]) != ("table", name)
            or _normalized_sql(row[2]) != _normalized_sql(sql)):
        return False
    # Names here are fixed module constants, never supplied by the caller.
    actual = tuple(tuple(row) for row in con.execute(
        "PRAGMA main.table_info(" + name + ")"))
    if actual != columns:
        return False
    # DROP TABLE also drops associated indexes/triggers: never take over those.
    extras = con.execute(
        "SELECT 1 FROM main.sqlite_master WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL LIMIT 1",
        (name,),
    ).fetchone()
    return not extras


def _trigger_matches(con, name, sql):
    row = con.execute(
        "SELECT type, tbl_name, sql FROM main.sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    return bool(row and tuple(row[:2]) == ("trigger", "metadata")
                and _normalized_sql(row[2]) == _normalized_sql(sql))


def _artifact_state(con):
    placeholders = ",".join("?" for _ in _CONFLICT_NAMES)
    # A temporary object could change unqualified resolution for this handle.
    if con.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE lower(name) IN ("
            + placeholders + ",?) LIMIT 1", _CONFLICT_NAMES + ("metadata",)).fetchone():
        return "conflict"
    rows = list(con.execute(
        "SELECT name FROM main.sqlite_master WHERE lower(name) IN ("
        + placeholders + ")", _CONFLICT_NAMES))
    names = {row[0] for row in rows}
    if len(names) != len(rows):
        return "conflict"
    # Unknown triggers on metadata are also a conflict: never drop or adopt
    # objects this plugin did not create.
    if con.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='trigger' "
            "AND tbl_name='metadata' AND lower(name) <> lower(?) LIMIT 1",
            (TRIGGER_NAMES[0],)).fetchone():
        return "conflict"
    if any(name in names for name in _V2_NAMES):
        return "conflict"
    if not names:
        return "disabled"
    if names == set(_OWNED_NAMES):
        if (_table_matches(con, GUARD_TABLE, _GUARD_COLUMNS, CREATE_GUARD_SQL)
                and _trigger_matches(con, TRIGGER_NAMES[0], INSERT_TRIGGER_SQL)):
            return "enabled"
    return "conflict"


def owned_artifacts(con):
    """Return disabled/enabled/conflict without exposing source values."""
    try:
        return _artifact_state(con)
    except sqlite3.DatabaseError:
        return "conflict"


def _require_transaction(con):
    if not con.in_transaction:
        raise RuntimeError("media-sort changes require a caller-owned transaction")


def install(con):
    """Install the exact original rule; an already-enabled DB is unchanged.

    The caller must start and commit/rollback the transaction.  A repeated
    apply does not reseed or rebuild the existing baseline.
    """
    _require_transaction(con)
    state = _artifact_state(con)
    if state == "conflict":
        raise RuleConflictError("unrecognized media-sort artifacts must not be taken over")
    if state == "enabled":
        return False
    try:
        con.execute("SELECT json_array_length('[]')")
    except sqlite3.DatabaseError as error:
        raise RuleConflictError("required SQLite JSON1 functions are unavailable") from error
    con.execute(CREATE_GUARD_SQL)
    con.execute(SEED_SQL)
    con.execute(INSERT_TRIGGER_SQL)
    return True


def remove(con):
    """Remove only the exact owned table/trigger; metadata is untouched."""
    _require_transaction(con)
    state = _artifact_state(con)
    if state == "conflict":
        raise RuleConflictError("unrecognized media-sort artifacts must not be removed")
    if state == "disabled":
        return False
    con.execute("DROP TRIGGER main." + TRIGGER_NAMES[0])
    con.execute("DROP TABLE main." + GUARD_TABLE)
    return True
