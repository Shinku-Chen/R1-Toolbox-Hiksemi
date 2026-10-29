"""Read-only factory-session fixtures; no live credentials or NAS requests."""

import builtins
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import vendor_media_auth as auth


TOKEN = "0123456789abcdef0123456789abcdef"
OTHER_TOKEN = "fedcba9876543210fedcba9876543210"


class VendorMediaAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r1-media-auth-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tmp/nand_config/USER_AUTH_DB_101"
        self.path.parent.mkdir(parents=True)

    def write(self, rows=None, owners=None, untyped=False):
        if self.path.exists():
            self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE tb_user_auth (user_id, user_type, "
                       "user_name TEXT, user_auth TEXT, password_digest TEXT)")
            columns = ("user_mkey, user_id, source_id, source_name, source_type, token, login_time"
                       if untyped else "user_mkey INTEGER PRIMARY KEY AUTOINCREMENT, user_id int64, "
                       "source_id VARCHAR256, source_name VARCHAR256, source_type int, "
                       "token VARCHAR64 NOT NULL, login_time VARCHAR64")
            db.execute("CREATE TABLE tb_terminal_token ({})".format(columns))
            db.executemany("INSERT INTO tb_user_auth VALUES (?, ?, ?, ?, ?)", [
                (user_id, role, "private-owner-name", "private-password-auth", "private-digest")
                for user_id, role in ([(100000, 0)] if owners is None else owners)])
            db.executemany("INSERT INTO tb_terminal_token VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (key, user_id, source, "private-source-name", kind, token, seconds)
                for key, user_id, source, kind, token, seconds in (
                    [(1, 100000, "private-owner-source", 1, TOKEN, "1700000000")]
                    if rows is None else rows)])

    def collect(self):
        return auth.read_session(self.root)

    def assert_status(self, status):
        session, result = self.collect()
        self.assertIsNone(session)
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], status)
        return result

    def test_plaintext_database_is_readonly_and_only_needed_columns_are_read(self):
        self.write()
        before = self.path.read_bytes()
        files = set(self.path.parent.iterdir())
        statements, reads, writes = [], [], []
        connect = sqlite3.connect
        real_authorize = auth._authorize

        def authorize(action, table, column, database, caller):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE):
                writes.append(action)
            return real_authorize(action, table, column, database, caller)

        def readonly_connect(filename, **kwargs):
            self.assertIn("?mode=ro", filename)
            self.assertNotIn("immutable", filename)
            self.assertTrue(kwargs["uri"])
            self.assertLessEqual(kwargs["timeout"], 0.25)
            connection = connect(filename, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(auth.sqlite3, "connect", side_effect=readonly_connect), \
                mock.patch.object(auth, "_authorize", side_effect=authorize), \
                mock.patch("socket.socket", side_effect=AssertionError("no socket")), \
                mock.patch("subprocess.run", side_effect=AssertionError("no command")):
            session, result = self.collect()
        self.assertEqual(result, {"available": True, "status": "ready", "reason": auth._REASONS["ready"],
                                  "user_id": 100000, "records_checked": 1, "candidate_count": 1,
                                  "selection": "newest_in_vendor_window"}, (statements, reads))
        self.assertEqual(session.user_id, 100000)
        self.assertEqual(session.source_id, "private-owner-source")
        self.assertEqual(session.login_time, 1700000000)
        self.assertEqual(session.base_token, TOKEN)
        self.assertIn("BEGIN", statements)
        self.assertIn("PRAGMA query_only=ON", statements)
        self.assertFalse(writes)
        self.assertTrue(reads)
        self.assertNotIn(("tb_user_auth", "user_auth"), reads)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(files, set(self.path.parent.iterdir()))

    def test_private_session_repr_and_public_status_do_not_expose_secrets(self):
        self.write()
        session, result = self.collect()
        text = repr(session) + str(session) + json.dumps(result, ensure_ascii=False)
        for private in (TOKEN, "private-owner-source", "private-owner-name", "private-source-name",
                        "private-digest", "private-password-auth", "USER_AUTH_DB", str(self.root),
                        "base_token", "source_id"):
            self.assertNotIn(private, text)
        self.assertFalse(hasattr(session, "__dict__"))
        with self.assertRaises(TypeError):
            vars(session)
        self.assertEqual(set(result), {"available", "status", "reason", "user_id", "records_checked",
                                       "candidate_count", "selection"})

    def test_access_token_exact_vendor_derivation_and_nas_clock(self):
        self.write()
        session, _ = self.collect()
        expected = "224dd730e3a37f3a004dbf08aaaef729:1700000000:100000"
        self.assertEqual(session.derive_access_token(1700000000), expected)
        with mock.patch.object(auth.time, "time", return_value=1700000000.9):
            self.assertEqual(session.derive_access_token(), expected)
        for timestamp in (True, -1, 1.5, "1700000000"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                session.derive_access_token(timestamp)

    def test_missing_database_and_missing_sqlite_never_create_database(self):
        self.assert_status("db_missing")
        self.assertFalse(self.path.exists())
        with mock.patch.object(auth, "sqlite3", None):
            self.assert_status("sqlite_unavailable")
        self.assertFalse(self.path.exists())

    def test_module_import_survives_unavailable_sqlite_extension(self):
        spec = importlib.util.spec_from_file_location("media_auth_without_sqlite", auth.__file__)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def without_sqlite(name, *args, **kwargs):
            if name == "sqlite3":
                raise ImportError("no _sqlite3")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=without_sqlite):
            spec.loader.exec_module(module)
        self.assertIsNone(module.sqlite3)
        self.assertEqual(module.read_session(self.root)[1]["status"], "sqlite_unavailable")

    def test_owner_identity_must_be_unique_exact_integer_and_admin(self):
        for owners in ([], [(100000, 1)], [(100000, "0")], [("100000", 0)],
                       [(100000, 0), (100000, 0)], [(100001, 0)]):
            with self.subTest(owners=owners):
                self.write(owners=owners)
                result = self.assert_status("identity_unconfirmed")
                self.assertIsNone(result["user_id"])
                self.assertEqual(result["records_checked"], 0)

    def test_other_user_tokens_are_never_candidates(self):
        self.write([(1, 100001, "other-source", 1, OTHER_TOKEN, "1800000000")])
        result = self.assert_status("no_session")
        self.assertEqual(result["user_id"], 100000)
        self.assertEqual(result["records_checked"], 0)

    def test_unconfirmed_identity_does_not_read_any_session_token(self):
        self.write(owners=[(100000, 1)])
        reads = []
        real_authorize = auth._authorize

        def authorize(action, table, column, database, caller):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
            return real_authorize(action, table, column, database, caller)

        with mock.patch.object(auth, "_authorize", side_effect=authorize):
            self.assert_status("identity_unconfirmed")
        self.assertTrue(reads)
        self.assertTrue(all(table == "tb_user_auth" for table, column in reads))

    def test_no_session_or_no_valid_lowercase_32_hex_token(self):
        self.write([])
        self.assert_status("no_session")
        for token in ("", TOKEN.upper(), TOKEN[:-1], TOKEN + "0", "g" * 32,
                      "0" * 32 + "\n", "secret-path/" + "0" * 20):
            with self.subTest(token=token):
                self.write([(1, 100000, "source", 0, token, "1700000000")])
                result = self.assert_status("no_session")
                self.assertEqual(result["candidate_count"], 0)

    def test_selects_newest_valid_inside_vendor_21_oldest_first_window(self):
        rows = [(index, 100000, "source-{}".format(index), 0,
                 TOKEN if index <= 20 else "invalid", str(1700000000 + index))
                for index in range(1, 22)]
        rows.append((22, 100000, "outside-window", 0, OTHER_TOKEN, "1900000000"))
        self.write(rows)
        session, result = self.collect()
        self.assertTrue(result["available"])
        self.assertEqual(result["records_checked"], 21)
        self.assertEqual(result["candidate_count"], 20)
        self.assertEqual(session.source_id, "source-20")
        self.assertEqual(session.login_time, 1700000020)
        self.assertEqual(session.base_token, TOKEN)

    def test_factory_text_order_is_preserved_instead_of_invented_numeric_window(self):
        rows = [(index, 100000, "source-{}".format(index), 0, TOKEN, str(index))
                for index in range(1, 23)]
        self.write(rows)
        session, result = self.collect()
        # SQLite VARCHAR startup order puts "9" outside the first 21, while
        # "22" is inside. Choosing the numeric latest 21 is a different query.
        self.assertEqual(result["records_checked"], 21)
        self.assertEqual(session.login_time, 22)

    def test_duplicate_selected_source_conflict_is_unavailable(self):
        for old_token, old_time in ((OTHER_TOKEN, "1690000000"), (TOKEN, "1690000000"),
                                    ("invalid", "1690000000")):
            with self.subTest(old_token=old_token, old_time=old_time):
                self.write([(1, 100000, "same-source", 0, old_token, old_time),
                            (2, 100000, "same-source", 0, TOKEN, "1700000000")])
                self.assert_status("ambiguous_session")
        self.write([(1, 100000, "same-source", 0, TOKEN, "1700000000"),
                    (2, 100000, "same-source", 0, TOKEN, "1700000000")])
        self.assertTrue(self.collect()[1]["available"])

    def test_equal_latest_distinct_sources_do_not_get_an_arbitrary_tie_break(self):
        self.write([(1, 100000, "source-a", 0, TOKEN, "1700000000"),
                    (2, 100000, "source-b", 0, OTHER_TOKEN, "1700000000")])
        self.assert_status("ambiguous_session")

    def test_unselected_duplicate_source_does_not_force_a_different_session(self):
        self.write([(1, 100000, "old-source", 0, TOKEN, "1680000000"),
                    (2, 100000, "old-source", 0, OTHER_TOKEN, "1690000000"),
                    (3, 100000, "new-source", 0, TOKEN, "1700000000")])
        self.assertEqual(self.collect()[0].source_id, "new-source")

    def test_invalid_row_types_sources_and_timestamps_fail_closed(self):
        baseline = [1, 100000, "source", 0, TOKEN, "1700000000"]
        invalid = [(0, "1"), (0, 0), (1, "100000"), (2, None), (2, ""), (2, "x" * 257),
                   (2, "温" * 86), (2, "bad\nsource"), (3, "0"), (3, None), (4, b"0" * 32),
                   (4, None), (5, None), (5, 1.5), (5, "-1"), (5, "1.0"),
                   (5, "1e9"), (5, "1" * 13)]
        for index, value in invalid:
            with self.subTest(index=index, value=value):
                row = list(baseline)
                row[index] = value
                self.write([row], untyped=True)
                # Non-integer owner IDs do not match the integer-bound window.
                self.assert_status("no_session" if index == 1 else "invalid_database")
        self.write([(1, 100000, "x" * 256, 0, TOKEN, 0)], untyped=True)
        session, result = self.collect()
        self.assertTrue(result["available"])
        self.assertEqual(session.login_time, 0)

    def test_corrupt_database_unknown_schema_and_errors_never_return_exception_text(self):
        self.path.write_bytes(b"not-a-database-private-token")
        result = self.assert_status("invalid_database")
        self.assertNotIn("private", str(result))
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE wrong_table (token TEXT)")
        self.assert_status("invalid_database")
        self.write()
        for error, status in ((PermissionError("private-path-token"), "permission_denied"),
                              (RuntimeError("private-path-token"), "data_unavailable")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(auth.sqlite3, "connect", side_effect=error):
                result = self.assert_status(status)
                self.assertNotIn("private", str(result))

    def test_exclusive_lock_returns_busy_with_bounded_wait_and_no_writes(self):
        self.write()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.assert_status("busy")
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_progress_handler_interrupts_expensive_snapshot_query(self):
        self.write()
        connection = sqlite3.connect(self.path)
        try:
            # The known plain table can grow unexpectedly. Sorting its complete
            # owner range must remain bounded before any selected token leaves.
            connection.execute("WITH RECURSIVE numbers(x) AS (VALUES(2) UNION ALL "
                               "SELECT x+1 FROM numbers WHERE x<100000) "
                               "INSERT INTO tb_terminal_token "
                               "SELECT x, 100000, 'source-' || x, 'private-source', 0, ?, "
                               "'1700000000' FROM numbers", (TOKEN,))
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(auth, "READ_DEADLINE", 0.001):
            started = time.monotonic()
            self.assert_status("busy")
            self.assertLess(time.monotonic() - started, 0.8)

    def test_schema_indirection_cannot_read_password_columns_or_call_functions(self):
        for expression in ("user_auth", "password_digest", "lower(user_auth)", "lower('{}')".format(TOKEN)):
            with self.subTest(expression=expression):
                self.write()
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute("DROP TABLE tb_terminal_token")
                    connection.execute("CREATE VIEW tb_terminal_token AS SELECT 1 AS user_mkey, "
                                       "user_id, 'source' AS source_id, 0 AS source_type, "
                                       + expression + " AS token, '1700000000' AS login_time "
                                       "FROM tb_user_auth")
                session, result = self.collect()
                self.assertIsNone(session)
                self.assertFalse(result["available"])
                self.assertEqual(result["records_checked"], 0)
                self.assertNotIn("private", str(result))

    def test_identity_and_sessions_share_one_sqlite_snapshot(self):
        self.write()
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        connect = sqlite3.connect

        class ConnectionProxy:
            def __init__(proxy, connection):
                proxy.connection = connection

            def __getattr__(proxy, name):
                return getattr(proxy.connection, name)

            def execute(proxy, sql, *args):
                if "FROM tb_terminal_token" in sql:
                    writer.execute("INSERT INTO tb_terminal_token VALUES (2, 100000, 'new-source', "
                                   "'private-source', 0, ?, '1800000000')", (OTHER_TOKEN,))
                    writer.commit()
                return proxy.connection.execute(sql, *args)

        with mock.patch.object(auth.sqlite3, "connect", side_effect=lambda *a, **kw: ConnectionProxy(connect(*a, **kw))):
            session, result = self.collect()
        self.assertTrue(result["available"])
        self.assertEqual(result["records_checked"], 1)
        self.assertEqual(session.base_token, TOKEN)

    def test_final_symlink_and_nonregular_database_are_rejected(self):
        self.path.mkdir()
        self.assert_status("invalid_database")
        self.path.rmdir()
        target = self.root / "regular-db"
        self.write()
        self.path.rename(target)
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.assert_status("invalid_database")

    def test_parent_symlink_within_fixture_is_allowed_and_escape_is_rejected(self):
        self.write()
        directory = self.root / "actual-config"
        self.path.parent.rename(directory)
        try:
            self.path.parent.symlink_to(directory, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.assertTrue(self.collect()[1]["available"])
        self.path.parent.unlink()
        with tempfile.TemporaryDirectory(prefix="r1-auth-outside-") as outside:
            self.path.parent.symlink_to(Path(outside), target_is_directory=True)
            (Path(outside) / self.path.name).write_bytes((directory / self.path.name).read_bytes())
            self.assert_status("invalid_database")


if __name__ == "__main__":
    unittest.main()
