"""Contract tests for the user's original INSERT-only media sort rule.

All databases are synthetic and in-memory.  The rule intentionally has no TV
type filter, no UPDATE trigger, no de-duplication and no cross-type baseline:
it compares the raw JSON path array length for (id, lid, type).
"""

import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "media_sort_plugin_rules", ROOT / "runtime/media_sort_plugin_rules.py")
rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rules)


class MediaSortRulesTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.con.close)
        self.con.execute("""CREATE TABLE metadata (
            id INTEGER, lid INTEGER, type INTEGER, path TEXT,
            timestamp INTEGER, name TEXT,
            PRIMARY KEY(id,lid,type))""")

    def write(self, paths, ty=2, ident=7, lid=10, timestamp=100):
        value = json.dumps(paths, separators=(',', ':')) if isinstance(paths, list) else paths
        self.con.execute("INSERT OR REPLACE INTO metadata VALUES(?,?,?,?,?,?)",
                         (ident, lid, ty, value, timestamp, "synthetic"))

    def timestamp(self, ty=2, ident=7, lid=10):
        return self.con.execute(
            "SELECT timestamp FROM metadata WHERE id=? AND lid=? AND type=?",
            (ident, lid, ty)).fetchone()[0]

    def guard(self, ty=2, ident=7, lid=10):
        row = self.con.execute(
            "SELECT path FROM " + rules.GUARD_TABLE + " WHERE id=? AND lid=? AND type=?",
            (ident, lid, ty)).fetchone()
        return row[0] if row else None

    def install(self):
        self.con.execute("BEGIN")
        result = rules.install(self.con)
        self.con.execute("COMMIT")
        return result

    def remove(self):
        self.con.execute("BEGIN")
        result = rules.remove(self.con)
        self.con.execute("COMMIT")
        return result

    def test_exact_original_sql_and_repeat_apply_do_not_reseed(self):
        self.write(["p0"])
        self.assertEqual(rules.RULE_VERSION, 1)
        self.assertIn("AFTER INSERT ON metadata", rules.INSERT_TRIGGER_SQL)
        self.assertNotIn("AFTER UPDATE", rules.INSERT_TRIGGER_SQL)
        self.assertIn("INSERT OR REPLACE INTO metadata_path_guard", rules.SEED_SQL)
        self.assertTrue(self.install())
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        installed = dict(self.con.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?)",
            (rules.GUARD_TABLE, rules.TRIGGER_NAMES[0])).fetchall())
        self.assertEqual(rules._normalized_sql(installed[rules.GUARD_TABLE]),
                         rules._normalized_sql(rules.CREATE_GUARD_SQL))
        self.assertEqual(rules._normalized_sql(installed[rules.TRIGGER_NAMES[0]]),
                         rules._normalized_sql(rules.INSERT_TRIGGER_SQL))
        self.assertEqual(self.guard(), '["p0"]')
        self.con.execute("UPDATE metadata_path_guard SET path='[\"sentinel\"]'")
        self.assertFalse(self.install())
        self.assertEqual(self.guard(), '["sentinel"]')
        self.assertEqual(self.timestamp(), 100)

    def test_insert_or_replace_longer_path_updates_microsecond_timestamp_and_guard(self):
        self.write(["p0"])
        self.install()
        self.write(["p0", "p1"])
        stamp = self.timestamp()
        self.assertGreater(stamp, 100)
        self.assertGreater(stamp, 1_000_000_000_000_000)
        self.assertEqual(self.guard(), '["p0","p1"]')

    def test_same_length_reorder_and_shorter_paths_do_not_bump(self):
        self.write(["p0", "p1"])
        self.install()
        for paths in (["p1", "p0"], ["p0", "p1"], ["p0"]):
            with self.subTest(paths=paths):
                self.write(paths)
                self.assertEqual(self.timestamp(), 100)
        # The original rule copies the latest array into the guard even when
        # the length did not grow.
        self.assertEqual(self.guard(), '["p0"]')

    def test_duplicate_entry_that_grows_raw_array_still_bumps(self):
        self.write(["p0"])
        self.install()
        self.write(["p0", "p0"])
        self.assertGreater(self.timestamp(), 100)
        self.assertEqual(self.guard(), '["p0","p0"]')

    def test_valid_non_array_json_keeps_original_sqlite_behavior(self):
        self.write(["p0"])
        self.install()
        self.write('{"path":"synthetic"}')
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.guard(), '{"path":"synthetic"}')
        self.write(["p0"])
        self.assertGreater(self.timestamp(), 100)

    def test_user_supplied_script_is_recognized_without_reinstalling(self):
        # This fixture is the user's original SQL, independent of module SQL.
        self.write(["p0"])
        self.con.executescript('''
CREATE TABLE IF NOT EXISTS metadata_path_guard (
  id INTEGER, lid INTEGER, type INTEGER, path TEXT,
  PRIMARY KEY (id, lid, type)
);
INSERT OR REPLACE INTO metadata_path_guard (id, lid, type, path)
  SELECT id, lid, type, path FROM metadata;
CREATE TRIGGER IF NOT EXISTS metadata_guard_insert
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
END;
''')
        self.assertEqual(rules.owned_artifacts(self.con), 'enabled')
        original = self.con.execute(
            "SELECT sql FROM sqlite_master WHERE name='metadata_guard_insert'").fetchone()[0]
        self.assertEqual(rules._normalized_sql(original),
                         rules._normalized_sql(rules.INSERT_TRIGGER_SQL))
        self.con.execute("UPDATE metadata_path_guard SET path='[\"sentinel\"]'")
        self.assertFalse(self.install())
        self.assertEqual(self.guard(), '["sentinel"]')
        self.assertEqual(self.con.execute(
            "SELECT sql FROM sqlite_master WHERE name='metadata_guard_insert'").fetchone()[0],
                         original)

    def test_update_path_does_not_trigger_and_does_not_change_guard(self):
        self.write(["p0"])
        self.install()
        self.con.execute("UPDATE metadata SET path='[\"p0\",\"p1\"]'")
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.guard(), '["p0"]')

    def test_first_insert_without_old_baseline_keeps_factory_timestamp(self):
        self.install()
        self.write(["p0"])
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.guard(), '["p0"]')
        self.write(["p0", "p1"])
        self.assertGreater(self.timestamp(), 100)
        self.assertEqual(self.guard(), '["p0","p1"]')

    def test_type_is_part_of_the_key_and_not_a_tv_filter(self):
        for ty in (0, 1, 2, 3, 4, 5):
            self.write(["p0"], ty=ty, ident=ty)
        self.install()
        for ty in (0, 1, 2, 3, 4, 5):
            with self.subTest(ty=ty):
                self.write(["p0", "p1"], ty=ty, ident=ty)
                self.assertGreater(self.timestamp(ty=ty, ident=ty), 100)
                self.assertEqual(self.guard(ty=ty, ident=ty), '["p0","p1"]')
        # Same id/lid but another type has an independent baseline.
        self.write(["p0"], ty=4, ident=77)
        self.write(["p0"], ty=2, ident=77)
        self.write(["p0", "p1"], ty=2, ident=77)
        self.assertGreater(self.timestamp(ty=2, ident=77), 100)
        self.assertEqual(self.timestamp(ty=4, ident=77), 100)
        self.assertEqual(self.guard(ty=4, ident=77), '["p0"]')

    def test_remove_preserves_metadata_and_adjusted_timestamp(self):
        self.write(["p0"])
        self.install()
        self.write(["p0", "p1"])
        stamp = self.timestamp()
        self.assertTrue(self.remove())
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")
        self.assertEqual(self.timestamp(), stamp)
        self.assertEqual(self.con.execute("SELECT count(*) FROM metadata").fetchone()[0], 1)
        self.assertFalse(self.remove())

    def test_caller_transaction_is_required_and_rollback_restores_state(self):
        with self.assertRaisesRegex(RuntimeError, "caller-owned transaction"):
            rules.install(self.con)
        self.con.execute("BEGIN")
        rules.install(self.con)
        self.con.execute("ROLLBACK")
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")
        self.install()
        self.con.execute("BEGIN")
        rules.remove(self.con)
        self.con.execute("ROLLBACK")
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        with self.assertRaisesRegex(RuntimeError, "caller-owned transaction"):
            rules.remove(self.con)

    def test_unknown_same_name_partial_and_v2_artifacts_conflict_without_mutation(self):
        self.write(["p0"])
        cases = (
            "CREATE TABLE metadata_path_guard(unrelated TEXT)",
            "CREATE TRIGGER metadata_guard_insert AFTER INSERT ON metadata BEGIN SELECT 1; END",
            "CREATE TABLE r1_toolbox_media_sort_baseline_v2(id INTEGER)",
            "CREATE TRIGGER r1_toolbox_media_sort_insert_v2 AFTER INSERT ON metadata BEGIN SELECT 1; END",
            "CREATE TRIGGER unknown_hook AFTER INSERT ON metadata BEGIN SELECT 1; END",
        )
        for statement in cases:
            with self.subTest(statement=statement):
                con = sqlite3.connect(":memory:", isolation_level=None)
                con.execute("""CREATE TABLE metadata (
                    id INTEGER, lid INTEGER, type INTEGER, path TEXT, timestamp INTEGER,
                    PRIMARY KEY(id,lid,type))""")
                con.execute(statement)
                self.assertEqual(rules.owned_artifacts(con), "conflict")
                before = list(con.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
                con.execute("BEGIN")
                for operation in (rules.install, rules.remove):
                    with self.assertRaises(rules.RuleConflictError):
                        operation(con)
                con.execute("ROLLBACK")
                self.assertEqual(before, list(con.execute(
                    "SELECT name,sql FROM sqlite_master ORDER BY name")))
                con.close()

    def test_partial_owned_objects_and_extra_table_objects_conflict(self):
        self.install()
        self.con.execute("DROP TRIGGER " + rules.TRIGGER_NAMES[0])
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.con.execute("CREATE TRIGGER " + rules.TRIGGER_NAMES[0]
                         + " AFTER INSERT ON metadata BEGIN SELECT 1; END")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.con.execute("CREATE INDEX unrelated_index ON metadata_path_guard(type)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.assertTrue(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='unrelated_index'").fetchone())

    def test_other_index_on_exact_guard_is_preserved_and_blocks_removal(self):
        self.install()
        self.con.execute('CREATE INDEX unrelated_index ON metadata_path_guard(type)')
        before = list(self.con.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
        self.assertEqual(rules.owned_artifacts(self.con), 'conflict')
        self.con.execute('BEGIN')
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)
        self.con.execute('ROLLBACK')
        self.assertEqual(before, list(self.con.execute(
            'SELECT name,sql FROM sqlite_master ORDER BY name')))

    def test_temp_metadata_conflicts_and_row_factory_works(self):
        self.con.execute("CREATE TEMP TABLE metadata(id INTEGER)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.con.execute("DROP TABLE temp.metadata")
        self.con.row_factory = sqlite3.Row
        self.write(["p0"])
        self.install()
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.remove()
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")

    def test_bad_json_is_not_silently_skipped(self):
        # The original SQL calls json_array_length on NEW.path and the guard.
        # A malformed array makes SQLite raise; the plugin does not add a
        # WHEN/validity guard or claim that the vendor write cannot be blocked.
        self.write(["p0"])
        self.install()
        with self.assertRaises(sqlite3.OperationalError):
            self.write("not JSON")
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.guard(), '["p0"]')
        self.con.execute("UPDATE metadata_path_guard SET path='not JSON'")
        with self.assertRaises(sqlite3.OperationalError):
            self.write(["p0", "p1"])
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.guard(), "not JSON")

    def test_seed_copies_all_types_and_raw_paths_without_dedup(self):
        self.write(["p0", "p0"], ty=0, ident=1)
        self.write(["p1"], ty=4, ident=4)
        self.install()
        self.assertEqual(self.guard(ty=0, ident=1), '["p0","p0"]')
        self.assertEqual(self.guard(ty=4, ident=4), '["p1"]')


if __name__ == "__main__":
    unittest.main()
