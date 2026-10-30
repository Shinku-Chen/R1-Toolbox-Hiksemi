"""Exercise fixed sort rules using only fresh, synthetic in-memory databases."""

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
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("""CREATE TABLE metadata (
            id INTEGER, lid INTEGER, type INTEGER, path TEXT,
            timestamp INTEGER, name TEXT,
            PRIMARY KEY(id,lid,type))""")

    def write(self, paths, ty=2, ident=7, lid=10, timestamp=100):
        value = json.dumps(paths) if isinstance(paths, list) else paths
        self.con.execute("INSERT OR REPLACE INTO metadata VALUES(?,?,?,?,?,?)",
                         (ident, lid, ty, value, timestamp, "synthetic"))

    def timestamp(self, ty=2, ident=7, lid=10):
        return self.con.execute(
            "SELECT timestamp FROM metadata WHERE id=? AND lid=? AND type=?",
            (ident, lid, ty)).fetchone()[0]

    def baseline(self, ident=7, lid=10):
        row = self.con.execute(
            "SELECT paths FROM " + rules.GUARD_TABLE + " WHERE id=? AND lid=?",
            (ident, lid)).fetchone()
        return set(json.loads(row[0])) if row else None

    def begin(self):
        if not self.con.in_transaction:
            self.con.execute("BEGIN")

    def install(self, **kwargs):
        self.begin()
        result = rules.install(self.con, **kwargs)
        self.con.commit()
        return result

    def remove(self):
        self.begin()
        result = rules.remove(self.con)
        self.con.commit()
        return result

    def legacy(self):
        self.con.execute(rules.LEGACY_TABLE_SQL)
        self.con.execute("INSERT INTO metadata_path_guard "
                         "SELECT id,lid,type,path FROM metadata")
        self.con.execute(rules.LEGACY_TRIGGER_SQL)
        self.con.commit()

    def test_new_install_repeat_and_remove_preserve_metadata(self):
        self.write(["p0"])
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")
        self.assertTrue(self.install())
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.assertFalse(self.install())
        self.write(["p0", "p1"])
        stamp = self.timestamp()
        self.assertGreater(stamp, 100)
        self.assertTrue(self.remove())
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")
        self.assertEqual(self.timestamp(), stamp)
        self.assertFalse(self.remove())

    def test_requires_caller_transaction_and_does_not_commit(self):
        with self.assertRaisesRegex(RuntimeError, "caller-owned transaction"):
            rules.install(self.con)
        self.con.execute("BEGIN")
        rules.install(self.con)
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")
        with self.assertRaisesRegex(RuntimeError, "caller-owned transaction"):
            rules.remove(self.con)

    def test_remove_can_be_rolled_back(self):
        self.install()
        self.con.execute("BEGIN")
        rules.remove(self.con)
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")

    def test_same_array_reordering_duplicates_and_metadata_do_not_bump(self):
        self.write(["p0", "p1"])
        self.install()
        for paths in (["p0", "p1"], ["p1", "p0"], ["p0", "p0", "p1"]):
            with self.subTest(paths=paths):
                self.write(paths)
                self.assertEqual(self.timestamp(), 100)
        self.con.execute("UPDATE metadata SET name='changed synthetic'")
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0", "p1"})

    def test_distinct_growth_and_equal_length_new_path_bump(self):
        for new in (["p0", "p1"], ["p1"]):
            with self.subTest(new=new):
                self.con.execute("DELETE FROM metadata")
                if rules.owned_artifacts(self.con) == "enabled":
                    self.remove()
                self.write(["p0"])
                self.install()
                self.write(new)
                self.assertGreater(self.timestamp(), 100)
                self.assertEqual(self.baseline(), {"p0", "p1"})

    def test_new_group_without_baseline_keeps_factory_timestamp(self):
        self.install()
        self.write(["p0"])
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0"})
        self.write(["p0", "p1"])
        self.assertGreater(self.timestamp(), 100)

    def test_empty_array_is_valid_baseline(self):
        self.write([])
        self.install()
        self.assertEqual(self.baseline(), set())
        self.write(["p0"])
        self.assertGreater(self.timestamp(), 100)

    def test_non_tv_types_are_untouched(self):
        for ty in (0, 1, 3, 5):
            self.write(["p0"], ty=ty, ident=ty)
        self.install()
        for ty in (0, 1, 3, 5):
            self.write(["p0", "p1"], ty=ty, ident=ty)
            self.assertEqual(self.timestamp(ty=ty, ident=ty), 100)
            self.assertIsNone(self.baseline(ident=ty))

    def test_update_bumps_once_without_timestamp_recursion(self):
        self.write(["p0"])
        self.install()
        self.con.execute("PRAGMA recursive_triggers=ON")
        self.con.execute("UPDATE metadata SET path='[\"p0\",\"p1\"]'")
        self.assertGreater(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0", "p1"})
        # A later factory reparse with unchanged paths must not become a bump.
        self.write(["p0", "p1"])
        self.assertEqual(self.timestamp(), 100)

    def test_delete_type2_insert_type4_keeps_baseline_and_bumps(self):
        self.write(["p0"])
        self.install()
        self.con.execute("DELETE FROM metadata WHERE type=2")
        self.write(["p0", "p1"], ty=4)
        self.assertGreater(self.timestamp(ty=4), 100)
        self.assertEqual(self.baseline(), {"p0", "p1"})
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM " + rules.GUARD_TABLE).fetchone()[0], 1)

    def test_direct_type_update_keeps_stable_group(self):
        self.write(["p0"])
        self.install()
        self.con.execute("UPDATE metadata SET type=4,path='[\"p0\",\"p1\"]'")
        self.assertGreater(self.timestamp(ty=4), 100)
        self.assertEqual(self.baseline(), {"p0", "p1"})

    def test_complete_union_survives_subset_and_outer_replace_policy(self):
        self.write(["p0", "p1"])
        self.install()
        self.write(["p0"])
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0", "p1"})
        self.write(["p1"])
        self.assertEqual(self.timestamp(), 100)
        self.write(["p2"])
        self.assertGreater(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0", "p1", "p2"})

    def test_library_groups_do_not_share_baseline(self):
        self.write(["p0"], lid=10)
        self.write(["q0"], lid=20)
        self.install()
        self.write(["p0", "p1"], lid=10)
        self.assertGreater(self.timestamp(lid=10), 100)
        self.assertEqual(self.timestamp(lid=20), 100)
        self.assertEqual(self.baseline(lid=20), {"q0"})

    def test_invalid_json_and_non_string_arrays_never_block_insert_or_update(self):
        self.write(["p0"])
        self.install()
        invalid = (None, "not JSON", "{", "{}", '"p1"', "1", "null",
                   "[1]", '["p0",null]', "[[]]", '[{"x":1}]', b'[]')
        for value in invalid:
            with self.subTest(value=value):
                self.write(value)
                self.assertEqual(self.timestamp(), 100)
                self.con.execute("UPDATE metadata SET path=?", (value,))
                self.assertEqual(self.timestamp(), 100)
                self.assertEqual(self.baseline(), {"p0"})
        self.write(["p0", "p1"])
        self.assertGreater(self.timestamp(), 100)

    def test_invalid_existing_source_does_not_break_install_or_first_valid_write(self):
        self.write("not JSON")
        self.install()
        self.assertIsNone(self.baseline())
        self.write(["p0"])
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(self.baseline(), {"p0"})

    def test_malformed_baseline_does_not_block_vendor_write(self):
        self.write(["p0"])
        self.install()
        for value in ("invalid JSON", "{}", '["p0",1]'):
            self.con.execute("UPDATE " + rules.GUARD_TABLE + " SET paths=?", (value,))
            self.write(["p0", "p1"])
            self.assertEqual(self.timestamp(), 100)
            self.assertEqual(self.con.execute(
                "SELECT paths FROM " + rules.GUARD_TABLE).fetchone()[0], value)

    def test_existing_two_tv_types_are_conflict_and_install_is_nonmutating(self):
        self.write(["p0"], ty=2)
        self.write(["q0"], ty=4)
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.install(self.con)
        self.assertFalse(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (rules.GUARD_TABLE,)).fetchone())

    def test_runtime_two_tv_types_are_not_guessed_but_owned_remove_is_allowed(self):
        self.write(["p0"], ty=2)
        self.install()
        self.write(["q0"], ty=4)
        self.assertEqual(self.timestamp(ty=4), 100)
        self.assertEqual(self.baseline(), {"p0"})
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.assertTrue(self.remove())
        self.assertEqual(self.con.execute("SELECT count(*) FROM metadata").fetchone()[0], 2)

    def test_null_identity_is_conflict_and_runtime_does_not_abort(self):
        self.install()
        self.write(["p0"], ident=None)
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.assertEqual(self.con.execute(
            "SELECT timestamp FROM metadata WHERE id IS NULL").fetchone()[0], 100)

    def test_exact_legacy_detection_explicit_upgrade_and_preserved_union(self):
        self.write(["p0"])
        self.legacy()
        self.assertEqual(rules.owned_artifacts(self.con), "legacy")
        self.begin()
        with self.assertRaisesRegex(rules.RuleConflictError, "confirmed upgrade"):
            rules.install(self.con)
        self.assertEqual(rules.owned_artifacts(self.con), "legacy")
        # Deleted legacy type2 records retain paths useful to the stable group.
        self.con.execute("DELETE FROM metadata WHERE type=2")
        self.write(["p1"], ty=4)
        self.assertTrue(self.install(upgrade_legacy=True))
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.assertEqual(self.baseline(), {"p0", "p1"})
        self.assertEqual(self.timestamp(ty=4), 100)
        self.write(["p0", "p1"], ty=4)
        self.assertEqual(self.timestamp(ty=4), 100)
        self.write(["p2"], ty=4)
        self.assertGreater(self.timestamp(ty=4), 100)
        self.assertFalse(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN (?,?)",
            (rules.LEGACY_GUARD_TABLE, rules.LEGACY_TRIGGER)).fetchone())

    def test_legacy_remove_preserves_timestamp(self):
        self.write(["p0"])
        self.legacy()
        self.assertTrue(self.remove())
        self.assertEqual(self.timestamp(), 100)
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")

    def test_legacy_upgrade_and_ddl_are_owned_by_caller_transaction(self):
        self.write(["p0"])
        self.legacy()
        before = list(self.con.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name"))
        self.con.execute("BEGIN")
        rules.install(self.con, upgrade_legacy=True)
        self.assertTrue(self.con.in_transaction)
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.con.rollback()
        self.assertEqual(rules.owned_artifacts(self.con), "legacy")
        self.assertEqual(before, list(self.con.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name")))
        self.assertEqual(self.timestamp(), 100)

    def test_legacy_invalid_json_is_skipped_during_explicit_upgrade(self):
        self.write("invalid JSON")
        self.legacy()
        self.install(upgrade_legacy=True)
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.assertIsNone(self.baseline())
        self.write(["p0"])
        self.assertEqual(self.timestamp(), 100)

    def test_unknown_same_name_trigger_and_table_are_never_adopted_or_removed(self):
        self.write(["p0"])
        self.con.execute(rules.LEGACY_TABLE_SQL)
        self.con.execute("CREATE TRIGGER metadata_guard_insert AFTER INSERT ON metadata "
                         "BEGIN SELECT 1; END")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        before = list(self.con.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        self.begin()
        for operation in (rules.install, rules.remove):
            with self.assertRaises(rules.RuleConflictError):
                operation(self.con)
        self.assertEqual(before, list(self.con.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name")))

    def test_unknown_compatible_table_alone_is_not_seeded(self):
        self.write(["p0"])
        self.con.execute(rules.LEGACY_TABLE_SQL)
        self.con.execute("INSERT INTO metadata_path_guard VALUES(7,10,2,'foreign')")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.install(self.con)
        self.assertEqual(self.con.execute(
            "SELECT path FROM metadata_path_guard").fetchone()[0], "foreign")

    def test_modified_v2_sql_partial_artifacts_and_extra_table_objects_conflict(self):
        self.install()
        self.con.execute("DROP TRIGGER " + rules.TRIGGER_NAMES[0])
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.con.execute("CREATE TRIGGER " + rules.TRIGGER_NAMES[0]
                         + " AFTER INSERT ON metadata BEGIN SELECT 1; END")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)

    def test_extra_index_on_owned_table_prevents_destructive_remove(self):
        self.install()
        self.con.execute("CREATE INDEX unrelated_index ON " + rules.GUARD_TABLE + "(lid)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)
        self.assertTrue(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='unrelated_index'").fetchone())

    def test_extra_trigger_on_owned_table_prevents_destructive_remove(self):
        self.install()
        self.con.execute("CREATE TRIGGER unrelated_trigger AFTER INSERT ON "
                         + rules.GUARD_TABLE + " BEGIN SELECT 1; END")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)
        self.assertTrue(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='unrelated_trigger'").fetchone())

    def test_mixed_versions_and_case_variant_names_are_conflict(self):
        self.install()
        self.con.execute(rules.LEGACY_TABLE_SQL)
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)
        self.con.execute("DROP TABLE metadata_path_guard")
        self.assertTrue(self.remove())
        self.con.execute("CREATE TABLE METADATA_PATH_GUARD(unrelated TEXT)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.install(self.con)

    def test_enable_and_disable_do_not_issue_transaction_boundaries(self):
        self.write(["p0"])
        self.con.commit()
        statements = []
        self.con.set_trace_callback(statements.append)
        self.con.execute("BEGIN")
        statements.clear()
        rules.install(self.con)
        rules.remove(self.con)
        self.assertFalse(any(statement.strip().upper().startswith(
            ("BEGIN", "COMMIT", "END", "ROLLBACK")) for statement in statements))
        self.assertTrue(self.con.in_transaction)

    def test_temp_shadow_and_wrong_table_schema_conflict(self):
        self.con.execute("CREATE TEMP TABLE metadata(id INTEGER)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        self.begin()
        with self.assertRaises(rules.RuleConflictError):
            rules.install(self.con)
        self.con.execute("DROP TABLE temp.metadata")
        self.con.execute("CREATE TABLE " + rules.GUARD_TABLE + "(unrelated TEXT)")
        self.assertEqual(rules.owned_artifacts(self.con), "conflict")
        with self.assertRaises(rules.RuleConflictError):
            rules.remove(self.con)

    def test_row_factory_supported(self):
        self.con.row_factory = sqlite3.Row
        self.write(["p0"])
        self.install()
        self.assertEqual(rules.owned_artifacts(self.con), "enabled")
        self.remove()
        self.assertEqual(rules.owned_artifacts(self.con), "disabled")


if __name__ == "__main__":
    unittest.main()
