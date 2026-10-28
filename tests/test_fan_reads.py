"""Bounded fan state reads; no Linux-only imports or hardware access required."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fan_reads_native", ROOT / "runtime/fan_native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class BoundedFanReadTests(unittest.TestCase):
    def setUp(self):
        # Preserve artifacts for inspection instead of permanently deleting them.
        self.folder = Path(tempfile.mkdtemp(prefix="r1-fan-reads-test-"))
        self.path = self.folder / "state"
        # Windows has no O_NONBLOCK. Ordinary fixture files do not need it;
        # Linux retains the real flag and exercises the actual no-follow gate.
        nonblock = patch.object(native.os, "O_NONBLOCK", getattr(os, "O_NONBLOCK", 0), create=True)
        nonblock.start()
        self.addCleanup(nonblock.stop)

    def test_short_reads_return_all_bytes_until_eof(self):
        content = b"first segment\0second segment\nlast segment"
        self.path.write_bytes(content)
        actual_read = os.read

        with patch.object(native.os, "read", side_effect=lambda fd, size: actual_read(fd, min(size, 3))):
            self.assertEqual(native.read_bytes(self.path, 128), content)

    def test_exact_limit_succeeds_only_after_observing_eof(self):
        content = b"0123456789abcdef"
        self.path.write_bytes(content)
        actual_read = os.read
        observed = []

        def short_read(fd, size):
            chunk = actual_read(fd, min(size, 4))
            observed.append(chunk)
            return chunk

        with patch.object(native.os, "read", side_effect=short_read):
            self.assertEqual(native.read_bytes(self.path, len(content)), content)
        self.assertEqual(observed[-1], b"")

    def test_exact_limit_with_failed_eof_check_is_rejected(self):
        content = b"0123456789abcdef"
        self.path.write_bytes(content)
        actual_read = os.read
        consumed = 0

        def fail_after_limit(fd, size):
            nonlocal consumed
            if consumed == len(content):
                raise OSError("fixture EOF confirmation failed")
            chunk = actual_read(fd, min(size, 4))
            consumed += len(chunk)
            return chunk

        with patch.object(native.os, "read", side_effect=fail_after_limit):
            self.assertIsNone(native.read_bytes(self.path, len(content)))

    def test_zero_stat_size_does_not_allow_oversize_stream(self):
        self.path.write_bytes(b"x" * 33)
        actual_read = os.read
        proc_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=0)

        with patch.object(native.os, "fstat", return_value=proc_info), \
                patch.object(native.os, "read", side_effect=lambda fd, size: actual_read(fd, min(size, 7))):
            self.assertIsNone(native.read_bytes(self.path, 32))

    def test_read_error_after_valid_prefix_never_returns_partial_content(self):
        self.path.write_bytes(b"valid prefix\nremaining content")
        actual_read = os.read
        prefix_read = False

        def fail_after_prefix(fd, size):
            nonlocal prefix_read
            if prefix_read:
                raise OSError("fixture process disappeared during read")
            prefix_read = True
            return actual_read(fd, min(size, 5))

        with patch.object(native.os, "read", side_effect=fail_after_prefix):
            self.assertIsNone(native.read_bytes(self.path, 128))

    def test_empty_file_is_complete_even_with_zero_limit(self):
        self.path.write_bytes(b"")
        self.assertEqual(native.read_bytes(self.path, 0), b"")

    def test_nonempty_zero_stat_file_is_rejected_at_zero_limit(self):
        self.path.write_bytes(b"x")
        proc_info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=0)
        with patch.object(native.os, "fstat", return_value=proc_info):
            self.assertIsNone(native.read_bytes(self.path, 0))

    def test_stat_oversize_is_rejected_without_reading_content(self):
        self.path.write_bytes(b"x" * 33)
        with patch.object(native.os, "read") as read:
            self.assertIsNone(native.read_bytes(self.path, 32))
        read.assert_not_called()

    def test_nonregular_descriptor_is_rejected_without_reading_content(self):
        self.path.write_bytes(b"ordinary fixture")
        nonregular = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=0)
        with patch.object(native.os, "fstat", return_value=nonregular), \
                patch.object(native.os, "read") as read:
            self.assertIsNone(native.read_bytes(self.path, 128))
        read.assert_not_called()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "Requires OS support for O_NOFOLLOW")
    def test_symlink_is_rejected_instead_of_following_valid_content(self):
        self.path.write_bytes(b"valid state")
        link = self.folder / "link"
        try:
            link.symlink_to(self.path)
        except OSError as error:
            self.skipTest(f"Symlink fixture unavailable: {error}")
        self.assertIsNone(native.read_bytes(link, 128))


if __name__ == "__main__":
    unittest.main()
