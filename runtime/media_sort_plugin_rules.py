"""Fixed, opt-in TV sorting rules; the caller owns the SQLite transaction.

The shadow stores the distinct union of observed TV paths by (id, lid).  It
survives the vendor's delete-type-2/insert-type-4 transition.  These are paths,
not verified episode identities, and removal never restores old timestamps.
"""

import re
import sqlite3


RULE_VERSION = 2
GUARD_TABLE = "r1_toolbox_media_sort_baseline_v2"
TRIGGER_NAMES = (
    "r1_toolbox_media_sort_insert_v2",
    "r1_toolbox_media_sort_update_v2",
)
LEGACY_GUARD_TABLE = "metadata_path_guard"
LEGACY_TRIGGER = "metadata_guard_insert"


class RuleConflictError(RuntimeError):
    """An ambiguous source or unrecognized artifact must not be taken over."""


# This is the exact audited helper definition, not a generic legacy matcher.
LEGACY_TABLE_SQL = """CREATE TABLE IF NOT EXISTS metadata_path_guard (
  id INTEGER,
  lid INTEGER,
  type INTEGER,
  path TEXT,
  PRIMARY KEY (id, lid, type)
)"""
LEGACY_TRIGGER_SQL = """CREATE TRIGGER IF NOT EXISTS metadata_guard_insert
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

CREATE_GUARD_SQL = """CREATE TABLE r1_toolbox_media_sort_baseline_v2 (
  id INTEGER NOT NULL,
  lid INTEGER NOT NULL,
  paths TEXT NOT NULL,
  PRIMARY KEY (id, lid)
) WITHOUT ROWID"""


def _valid_array(expression):
    """CASE, unlike a WHERE/AND ordering assumption, gates unsafe JSON calls."""
    return """(CASE WHEN typeof({value}) = 'text' AND json_valid({value})
      THEN CASE WHEN json_type({value}) = 'array'
        THEN NOT EXISTS (
          SELECT 1 FROM json_each({value}) AS checked_path
           WHERE checked_path.type <> 'text'
        ) ELSE 0 END
      ELSE 0 END)""".format(value=expression)


_VALID_NEW = _valid_array("NEW.path")
_VALID_BASELINE = _valid_array("baseline.paths")
_VALID_STORED = _valid_array("paths")


def _trigger_sql(name, event):
    # No DELETE trigger: REPLACE and the type transition must retain history.
    # An outer INSERT OR REPLACE can override an inner conflict policy, so the
    # seed INSERT excludes existing keys instead of relying on OR IGNORE.
    return """CREATE TRIGGER {name}
AFTER {event} ON metadata
FOR EACH ROW
WHEN NEW.type IN (2, 4)
 AND typeof(NEW.id) = 'integer' AND typeof(NEW.lid) = 'integer'
 AND {valid_new}
 AND (SELECT count(*) FROM metadata AS tv_group
       WHERE tv_group.id = NEW.id AND tv_group.lid = NEW.lid
         AND tv_group.type IN (2, 4)) = 1
BEGIN
  UPDATE metadata
     SET timestamp = CAST((julianday('now') - 2440587.5) * 86400000000 AS INTEGER)
   WHERE id = NEW.id AND lid = NEW.lid AND type = NEW.type
     AND EXISTS (
       SELECT 1 FROM {table} AS baseline
        WHERE baseline.id = NEW.id AND baseline.lid = NEW.lid
          AND CASE WHEN {valid_baseline} THEN EXISTS (
            SELECT 1 FROM json_each(NEW.path) AS candidate
             WHERE NOT EXISTS (
               SELECT 1 FROM json_each(baseline.paths) AS observed
                WHERE observed.value = candidate.value
             )
          ) ELSE 0 END
     );
  INSERT INTO {table} (id, lid, paths)
    SELECT NEW.id, NEW.lid,
      (SELECT json_group_array(value) FROM
        (SELECT DISTINCT value FROM json_each(NEW.path)))
     WHERE NOT EXISTS (
       SELECT 1 FROM {table} WHERE id = NEW.id AND lid = NEW.lid
     );
  UPDATE {table}
     SET paths = CASE WHEN {valid_stored} THEN
       (SELECT json_group_array(value) FROM (
         SELECT value FROM json_each(paths)
         UNION SELECT value FROM json_each(NEW.path)
       )) ELSE paths END
   WHERE id = NEW.id AND lid = NEW.lid;
END""".format(name=name, event=event, table=GUARD_TABLE,
               valid_new=_VALID_NEW, valid_baseline=_VALID_BASELINE,
               valid_stored=_VALID_STORED)


INSERT_TRIGGER_SQL = _trigger_sql(TRIGGER_NAMES[0], "INSERT")
UPDATE_TRIGGER_SQL = _trigger_sql(
    TRIGGER_NAMES[1], "UPDATE OF path, type, id, lid")
SCHEMA_SQL = (CREATE_GUARD_SQL, INSERT_TRIGGER_SQL, UPDATE_TRIGGER_SQL)

_V2_COLUMNS = (
    (0, "id", "INTEGER", 1, None, 1),
    (1, "lid", "INTEGER", 1, None, 2),
    (2, "paths", "TEXT", 1, None, 0),
)
_LEGACY_COLUMNS = (
    (0, "id", "INTEGER", 0, None, 1),
    (1, "lid", "INTEGER", 0, None, 2),
    (2, "type", "INTEGER", 0, None, 3),
    (3, "path", "TEXT", 0, None, 0),
)
_NAMES = (GUARD_TABLE,) + TRIGGER_NAMES + (LEGACY_GUARD_TABLE, LEGACY_TRIGGER)


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
    placeholders = ",".join("?" for _ in _NAMES)
    # A temporary object could change unqualified resolution for this handle.
    if con.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE lower(name) IN ("
            + placeholders + ",?) LIMIT 1", _NAMES + ("metadata",)).fetchone():
        return "conflict"
    rows = list(con.execute(
        "SELECT name FROM main.sqlite_master WHERE lower(name) IN ("
        + placeholders + ")", _NAMES))
    names = {row[0] for row in rows}
    if not names:
        return "disabled"
    if len(names) != len(rows):
        return "conflict"
    if names == {GUARD_TABLE, *TRIGGER_NAMES}:
        if (_table_matches(con, GUARD_TABLE, _V2_COLUMNS, CREATE_GUARD_SQL)
                and all(_trigger_matches(con, name, sql) for name, sql in
                        zip(TRIGGER_NAMES, SCHEMA_SQL[1:]))):
            return "enabled"
    if names == {LEGACY_GUARD_TABLE, LEGACY_TRIGGER}:
        if (_table_matches(con, LEGACY_GUARD_TABLE, _LEGACY_COLUMNS,
                           LEGACY_TABLE_SQL)
                and _trigger_matches(con, LEGACY_TRIGGER, LEGACY_TRIGGER_SQL)):
            return "legacy"
    return "conflict"


def _ambiguous_source(con):
    # Only keys/counts are inspected here; callers validate the full DB schema.
    return bool(con.execute(
        "SELECT 1 FROM main.metadata WHERE type IN (2,4) GROUP BY id,lid "
        "HAVING count(*) > 1 OR typeof(id) <> 'integer' "
        "OR typeof(lid) <> 'integer' LIMIT 1").fetchone())


def owned_artifacts(con):
    """Return disabled/enabled/legacy/conflict without exposing source values.

    An ambiguous current TV group is unavailable even when the owned SQL is
    intact.  remove() still separately verifies ownership to allow disabling it.
    """
    try:
        state = _artifact_state(con)
        return "conflict" if _ambiguous_source(con) else state
    except sqlite3.DatabaseError:
        return "conflict"


def _require_transaction(con):
    if not con.in_transaction:
        raise RuntimeError("media-sort changes require a caller-owned transaction")


def _seed_sql(legacy):
    sources = "SELECT id,lid,type,path FROM main.metadata"
    if legacy:
        sources += " UNION ALL SELECT id,lid,type,path FROM " + LEGACY_GUARD_TABLE
    valid = _valid_array("source.path")
    # The protected json_each input remains safe independently of query plans.
    return """WITH sources AS ({sources}), valid_sources AS (
      SELECT id,lid,path FROM sources AS source
       WHERE type IN (2,4) AND typeof(id) = 'integer'
         AND typeof(lid) = 'integer' AND {valid}
    )
    INSERT INTO {table} (id,lid,paths)
      SELECT groups.id,groups.lid,
        (SELECT json_group_array(value) FROM (
          SELECT DISTINCT entry.value AS value FROM valid_sources AS source
          JOIN json_each(CASE WHEN {valid} THEN source.path ELSE '[]' END) AS entry
           WHERE source.id = groups.id AND source.lid = groups.lid
        ))
      FROM (SELECT DISTINCT id,lid FROM valid_sources) AS groups""".format(
          sources=sources, valid=valid, table=GUARD_TABLE)


def install(con, upgrade_legacy=False):
    """Install exact v2 rules; a known legacy upgrade needs explicit consent.

    Return whether objects changed.  No BEGIN, COMMIT or executescript is used;
    callers must roll back their transaction on any exception.
    """
    _require_transaction(con)
    state = _artifact_state(con)
    if state == "conflict" or _ambiguous_source(con):
        raise RuleConflictError("media-sort artifacts or TV groups are ambiguous")
    if state == "enabled":
        return False
    if state == "legacy" and not upgrade_legacy:
        raise RuleConflictError("known legacy media-sort rules require confirmed upgrade")
    try:
        con.execute("SELECT json_valid('[]'),json_type('[]'),"
                    "json_array_length('[]'),json_group_array(value) FROM json_each('[]')")
    except sqlite3.DatabaseError as error:
        raise RuleConflictError("required SQLite JSON1 functions are unavailable") from error
    con.execute(CREATE_GUARD_SQL)
    con.execute(_seed_sql(state == "legacy"))
    con.execute(INSERT_TRIGGER_SQL)
    con.execute(UPDATE_TRIGGER_SQL)
    if state == "legacy":
        con.execute("DROP TRIGGER main." + LEGACY_TRIGGER)
        con.execute("DROP TABLE main." + LEGACY_GUARD_TABLE)
    return True


def remove(con):
    """Remove only exact owned v2/legacy objects; leave metadata untouched."""
    _require_transaction(con)
    state = _artifact_state(con)
    if state == "conflict":
        raise RuleConflictError("unrecognized media-sort artifacts must not be removed")
    if state == "disabled":
        return False
    if state == "legacy":
        con.execute("DROP TRIGGER main." + LEGACY_TRIGGER)
        con.execute("DROP TABLE main." + LEGACY_GUARD_TABLE)
    else:
        for name in TRIGGER_NAMES:
            con.execute("DROP TRIGGER main." + name)
        con.execute("DROP TABLE main." + GUARD_TABLE)
    return True
