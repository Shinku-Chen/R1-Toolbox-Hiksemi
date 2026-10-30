"""sysfs capacity differs from EOF; no ordinary-file integrity gate is relaxed."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_operations as files
import api_file_native_index as index


RELATIVE = 'sys/block/sdd/sdd1/dev'


def info(**changes):
    fields = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o444,
                  st_size=4096, st_mtime_ns=3, st_ctime_ns=4)
    fields.update(changes)
    return SimpleNamespace(**fields)


class SysfsPartitionDevTests(unittest.TestCase):
    @contextmanager
    def opened(self, *, before=None, after=None, blocks=None):
        before = before or info()
        after = after or before
        with mock.patch.object(files.os, 'open', return_value=123) as opened, \
                mock.patch.object(files.os, 'fstat', side_effect=[before, after]), \
                mock.patch.object(files.os, 'read', side_effect=blocks or [b'8:49\n', b'']) as read, \
                mock.patch.object(files.os, 'close') as close:
            yield SimpleNamespace(open=opened, read=read, close=close)

    def read(self):
        return files._read_sysfs_partition_dev('fixed-host-root', 'sdd', 'sdd1')

    def test_page_sized_declaration_and_short_payload_are_read_to_eof(self):
        with self.opened(blocks=[b'8:', b'4', b'9\n', b'']) as calls:
            self.assertEqual(self.read(), '8:49\n')
            self.assertEqual(calls.read.call_args_list,
                [mock.call(123, count) for count in (65, 63, 62, 60)])
            calls.close.assert_called_once_with(123)
            path, flags = calls.open.call_args.args
            self.assertEqual(path, Path('fixed-host-root').absolute() / RELATIVE)
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR), 0)
            if hasattr(os, 'O_NOFOLLOW'):
                self.assertTrue(flags & os.O_NOFOLLOW)
            if hasattr(os, 'O_NONBLOCK'):
                self.assertTrue(flags & os.O_NONBLOCK)

    def test_ordinary_metadata_still_rejects_declared_oversize_before_any_read(self):
        with self.opened() as calls, self.assertRaisesRegex(ValueError, 'invalid_metadata'):
            files._read_fixed('fixed-host-root', RELATIVE, 64)
        calls.read.assert_not_called()
        calls.close.assert_called_once_with(123)

    def test_ordinary_metadata_still_rejects_declared_length_mismatch(self):
        with self.opened(before=info(st_size=10)) as calls, \
                self.assertRaisesRegex(ValueError, 'metadata_changed'):
            files._read_fixed('fixed-host-root', RELATIVE, 64)
        calls.close.assert_called_once_with(123)

    def test_actual_64_byte_limit_requires_eof_and_65th_byte_is_refused(self):
        with self.opened(blocks=[b'a' * 32, b'b' * 32, b'']) as calls:
            self.assertEqual(self.read(), 'a' * 32 + 'b' * 32)
            self.assertEqual(calls.read.call_args_list,
                             [mock.call(123, count) for count in (65, 33, 1)])
        with self.opened(blocks=[b'a' * 64, b'x']) as calls, \
                self.assertRaisesRegex(ValueError, 'metadata_changed'):
            self.read()
        self.assertEqual(calls.read.call_args_list, [mock.call(123, 65), mock.call(123, 1)])
        calls.close.assert_called_once_with(123)

    def test_virtual_declaration_is_not_a_content_length_requirement(self):
        for size in (0, 5, 4096, 65536):
            with self.subTest(size=size), self.opened(before=info(st_size=size)):
                self.assertEqual(self.read(), '8:49\n')

    def test_each_opened_identity_or_metadata_change_is_refused_and_closed(self):
        for field, changed in (('st_dev', 9), ('st_ino', 9), ('st_mode', stat.S_IFREG | 0o644),
                               ('st_size', 8192), ('st_mtime_ns', 9), ('st_ctime_ns', 9)):
            with self.subTest(field=field), self.opened(after=info(**{field: changed})) as calls, \
                    self.assertRaisesRegex(ValueError, 'metadata_changed'):
                self.read()
            calls.close.assert_called_once_with(123)

    def test_nonregular_attribute_is_rejected_before_read_and_closed(self):
        for kind in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO, stat.S_IFBLK):
            with self.subTest(kind=kind), self.opened(before=info(st_mode=kind | 0o444)) as calls, \
                    self.assertRaisesRegex(ValueError, 'invalid_metadata'):
                self.read()
            calls.read.assert_not_called()
            calls.close.assert_called_once_with(123)

    def test_only_fixed_legal_matching_sd_partition_names_can_be_opened(self):
        for disk, partition in (('sdd', '../dev'), ('../sdd', 'sdd1'), ('sdd', 'sda1'),
                                ('sdd', 'sdd0'), ('sdd', 'sdd01'), ('sdd', 'sdd1/dev'),
                                ('nvme0n1', 'nvme0n1p1'), ('sdd', 'sdd'), (None, 'sdd1')):
            with self.subTest(disk=disk, partition=partition), \
                    mock.patch.object(files.os, 'open') as opened, \
                    self.assertRaisesRegex(ValueError, 'invalid_metadata'):
                files._read_sysfs_partition_dev('fixed-host-root', disk, partition)
            opened.assert_not_called()

    def test_read_error_and_invalid_text_propagate_without_fallback_and_close(self):
        with self.opened(blocks=[OSError(errno.EIO, 'private path')]) as calls, \
                self.assertRaises(OSError) as caught:
            self.read()
        self.assertEqual(caught.exception.errno, errno.EIO)
        calls.close.assert_called_once_with(123)
        with self.opened(blocks=[b'\xff', b'']) as calls, self.assertRaises(UnicodeError):
            self.read()
        calls.close.assert_called_once_with(123)

    def test_sata_still_stops_at_sysfs_stage_if_fixed_reader_fails(self):
        events = []
        row = {'mount': '/drives/data/Users/admin'}
        mount = '605 1 8:49 / /drives/data rw - ext4 /dev/sdd1 rw\n'
        with mock.patch.object(index, '_read_fixed', return_value=mount), \
                mock.patch.object(index, '_read_sysfs_partition_dev',
                    side_effect=OSError(errno.EACCES, 'private sysfs path')) as reader, \
                mock.patch.object(index.os, 'stat') as block:
            self.assertFalse(index._internal_sata('.', row,
                observer=lambda stage, state: events.append((stage, state))))
        reader.assert_called_once_with('.', 'sdd', 'sdd1')
        block.assert_not_called()
        self.assertEqual(events[-1], ('index.sata.sysfs_device', 'start'))


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'O_NOFOLLOW'),
                     'Linux real sysfs text attributes and no-follow required')
class RealSysfsCapacityTests(unittest.TestCase):
    def test_real_sysfs_dev_has_capacity_size_but_is_safely_read_through_eof(self):
        # A fixed virtual null device exists independently of SATA hardware.
        # The fixture symlink models sysfs intermediate links, not a SATA proof;
        # production still independently requires the actual partition/block/ATA.
        target = Path('/sys/devices/virtual/mem/null')
        with tempfile.TemporaryDirectory(prefix='r1-sysfs-dev-') as temporary:
            root = Path(temporary)
            disk = root / 'sys/block/sdd'
            disk.mkdir(parents=True)
            (disk / 'sdd1').symlink_to(target, target_is_directory=True)
            self.assertTrue(stat.S_ISREG((root / RELATIVE).stat().st_mode))
            self.assertGreater((root / RELATIVE).stat().st_size, 64)
            with self.assertRaisesRegex(ValueError, 'invalid_metadata'):
                files._read_fixed(root, RELATIVE, 64)
            self.assertEqual(files._read_sysfs_partition_dev(root, 'sdd', 'sdd1'), '1:3\n')

    def test_real_leaf_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory(prefix='r1-sysfs-nofollow-') as temporary:
            root = Path(temporary)
            parent = root / 'sys/block/sdd/sdd1'
            parent.mkdir(parents=True)
            target = parent / 'saved'
            target.write_bytes(b'8:49\n')
            (parent / 'dev').symlink_to(target)
            with self.assertRaises(OSError) as caught:
                files._read_sysfs_partition_dev(root, 'sdd', 'sdd1')
            self.assertEqual(caught.exception.errno, errno.ELOOP)
            self.assertEqual(target.read_bytes(), b'8:49\n')


if __name__ == '__main__':
    unittest.main()
