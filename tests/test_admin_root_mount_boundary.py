"""Real disposable Linux mounts; injected ACL fixtures are not NAS acceptance."""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import test_api_file_executor as policy_fixture
from api_file_executor import FileReadExecutor, _Handle
from api_file_operations import FileAccessError, _descriptor_mount_id


_PREFIX = 'r1-admin-root-mount-ci-'
_ADMIN = '/drives/data/Users/admin'
_MAX_MOUNTINFO = 262144
_ESCAPES = re.compile(r'\\(040|011|012|134)')
_ROOT_LINUX = sys.platform == 'linux' and hasattr(os, 'geteuid') and os.geteuid() == 0


def _unescape(value):
    return _ESCAPES.sub(lambda match: chr(int(match[1], 8)), value)


def _escape(value):
    return value.replace('\\', r'\134').replace(' ', r'\040').replace('\t', r'\011').replace('\n', r'\012')


def _kernel_mounts():
    """Read the actual namespace to bounded EOF, including after short reads."""
    descriptor = os.open('/proc/self/mountinfo', os.O_RDONLY | os.O_CLOEXEC)
    try:
        raw = bytearray()
        while len(raw) <= _MAX_MOUNTINFO:
            block = os.read(descriptor, min(65536, _MAX_MOUNTINFO + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if len(raw) > _MAX_MOUNTINFO:
            raise AssertionError('real mountinfo exceeds the test/production metadata limit')
    finally:
        os.close(descriptor)
    rows = []
    for line in raw.decode('utf-8', 'strict').splitlines():
        before_text, separator, after_text = line.partition(' - ')
        before, after = before_text.split(), after_text.split()
        if (not separator or len(before) < 6 or len(after) < 3
                or not re.fullmatch(r'[0-9]+', before[0], re.ASCII)
                or not re.fullmatch(r'[0-9]+', before[1], re.ASCII)
                or not re.fullmatch(r'[0-9]+:[0-9]+', before[2], re.ASCII)):
            raise AssertionError('invalid actual kernel mount record')
        rows.append({'before': before, 'after': after, 'path': _unescape(before[4])})
    return rows


class _AdminFixtureAuthority(policy_fixture.FixtureAuthority):
    """Reuse explicit policy fixtures without importing their TestCase classes."""

    def authorize(self, root_id, path, current):
        return dict(super().authorize(root_id, path, current), mount=_ADMIN)


@unittest.skipUnless(_ROOT_LINUX, 'real Linux bind/overlay mounts require UID 0')
class AdminRootMountBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0)
        self.fixture = Path(tempfile.mkdtemp(prefix=_PREFIX, dir='/tmp')).resolve(strict=True)
        self.fixture_identity = self.fixture.lstat()
        self.host, self.physical = self.fixture / 'host', self.fixture / 'physical'
        self.disk = self.host / 'drives/data'
        self.users, self.admin = self.disk / 'Users', self.disk / 'Users/admin'
        self.targets, self.sentinels = [], []
        self.executor, self.case_passed = None, False
        self.addCleanup(self._cleanup)
        self._validate_fixture()
        self.mount_bin = self._program('mount')
        self.umount_bin = self._program('umount')
        (self.physical / 'Users/admin/ordinary').mkdir(parents=True)
        self._sentinel(self.physical / 'Users/admin/normal.txt', policy_fixture.DATA)
        self._sentinel(self.physical / 'Users/admin/ordinary/sibling.txt', b'ordinary sibling')
        self.disk.mkdir(parents=True)
        proc = self.host / 'proc/self'
        proc.mkdir(parents=True)
        (proc / 'fdinfo').symlink_to('/proc/self/fdinfo', target_is_directory=True)
        self._mount(self.disk, [self.mount_bin, '--bind', str(self.physical), str(self.disk)])
        self.row = dict(policy_fixture.ROW, mount=_ADMIN)
        self.executor = FileReadExecutor(None, root=self.host, authorizer=lambda unused: True,
                                         authority=_AdminFixtureAuthority())
        self.key = policy_fixture.principal()
        self._normal_access()

    def _program(self, name):
        path = shutil.which(name, path='/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertIsNotNone(path, name + ' must be installed on the disposable Linux runner')
        return path

    def _validate_fixture(self):
        """Never recursively delete a computed or replaced directory."""
        self.assertEqual(self.fixture.parent, Path('/tmp').resolve(strict=True))
        self.assertTrue(self.fixture.name.startswith(_PREFIX))
        info = self.fixture.lstat()
        self.assertTrue(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode))
        self.assertEqual((info.st_dev, info.st_ino),
                         (self.fixture_identity.st_dev, self.fixture_identity.st_ino))
        self.assertEqual(info.st_uid, 0)

    def _sentinel(self, path, content):
        path.write_bytes(content)
        info = path.stat()
        self.sentinels.append((path, content, (info.st_dev, info.st_ino)))

    def _check_sentinels(self):
        for path, content, identity in self.sentinels:
            info = path.stat()
            self.assertEqual((info.st_dev, info.st_ino), identity, str(path))
            self.assertEqual(path.read_bytes(), content, str(path))

    def _entry(self, target):
        rows = [row for row in _kernel_mounts() if row['path'] == str(target)]
        self.assertEqual(len(rows), 1, 'expected exactly one owned real mount: ' + str(target))
        return rows[0]

    def _run(self, argv):
        try:
            return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        except subprocess.CalledProcessError as error:
            raise AssertionError(f'{Path(argv[0]).name} failed ({error.returncode}): {error.stderr.strip()}') from error

    def _mount(self, target, argv, *, project=True):
        self.assertTrue(target == self.disk or self.disk in target.parents)
        self.assertFalse(any(row['path'] == str(target) for row in _kernel_mounts()),
                         'refuse to stack onto a pre-existing mount')
        # Register before running: timeout/error may occur after the mount happened.
        self.targets.append(target)
        self._run(argv)
        self._entry(target)
        if project:
            self._project_mountinfo()

    def _unmount(self, target):
        rows = _kernel_mounts()
        exact = [row for row in rows if row['path'] == str(target)]
        self.assertLessEqual(len(exact), 1, 'unexpected stacked mount; preserve fixture')
        if exact:
            self.assertFalse(any(row['path'].startswith(str(target) + '/') for row in rows),
                             'never detach an ancestor while an owned descendant remains mounted')
            self._run([self.umount_bin, '--', str(target)])
        self.assertFalse(any(row['path'] == str(target) for row in _kernel_mounts()),
                         'ordinary umount did not remove the real mount')

    def _project_mountinfo(self):
        """Only rewrite the private virtual-host prefix, preserving kernel IDs/dev."""
        projected = []
        prefix = str(self.host)
        for row in _kernel_mounts():
            before = list(row['before'])
            if row['path'].startswith(prefix + '/'):
                before[4] = _escape(row['path'][len(prefix):])
            projected.append(' '.join(before) + ' - ' + ' '.join(row['after']))
        (self.host / 'proc/self/mountinfo').write_text('\n'.join(projected) + '\n', encoding='utf-8')

    def _call(self, tool, path=''):
        return self.executor.execute(tool, {'root_id': policy_fixture.ROOT, 'path': path}, self.key)

    def _normal_access(self, *, exclude=None):
        with _Handle(self.host, self.row, 'normal.txt', reader=True) as opened:
            self.assertEqual(os.pread(opened.fd, len(policy_fixture.DATA), 0), policy_fixture.DATA)
            self.assertEqual(_descriptor_mount_id(self.host, opened.fd),
                             self._entry(self.disk)['before'][0])
        value = self._call('get_file_info', 'normal.txt')
        self.assertTrue(value['ok'], value)
        listing = self._call('list_directory')
        self.assertTrue(listing['ok'], listing)
        names = {row['name'] for row in listing['entries']}
        self.assertTrue({'normal.txt', 'ordinary'} <= names, listing)
        if exclude is not None:
            self.assertNotIn(exclude, names, listing)

    def _child_denied(self):
        for path, options in (('mounted', {'directory': True}),
                              ('mounted/visible.txt', {'reader': True})):
            with self.assertRaises(FileAccessError) as caught:
                with _Handle(self.host, self.row, path, **options):
                    self.fail('a real descendant mount was opened')
            self.assertEqual(caught.exception.code, 'cross_mount_denied')
            result = self._call('get_file_info', path)
            self.assertEqual(result['error']['code'], 'cross_mount_denied', result)
        listing = self._call('list_directory', 'mounted')
        self.assertEqual(listing['error']['code'], 'cross_mount_denied', listing)
        self.assertNotIn('entries', listing)

    def _root_denied(self):
        for path in ('', 'normal.txt'):
            with self.assertRaises(FileAccessError) as caught:
                with _Handle(self.host, self.row, path, directory=not path):
                    self.fail('a replaced Users/admin root chain was opened')
            self.assertEqual(caught.exception.code, 'root_unavailable')
        for tool, path in (('list_directory', ''), ('get_file_info', 'normal.txt')):
            result = self._call(tool, path)
            self.assertEqual(result['error']['code'], 'root_unavailable', result)
            self.assertNotIn('entries', result)

    def _mounted_target(self):
        target = self.admin / 'mounted'
        target.mkdir()
        self._sentinel(self.physical / 'Users/admin/mounted/underlying.txt', b'underlying source remains')
        return target

    def _check_mount_identity(self, target, *, same_device):
        descriptor = os.open(target, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self.assertEqual(_descriptor_mount_id(self.host, descriptor), self._entry(target)['before'][0])
            self.assertNotEqual(_descriptor_mount_id(self.host, descriptor), self._entry(self.disk)['before'][0])
            if same_device:
                self.assertEqual(os.fstat(descriptor).st_dev, self.admin.stat().st_dev)
        finally:
            os.close(descriptor)

    def _child_mount_phases(self, target, snapshot, *, same_device):
        """A newly mounted child must fail by real fdinfo before proc refresh."""
        projected_path = b' /drives/data/Users/admin/mounted '
        self.assertNotIn(projected_path, snapshot)
        self.assertEqual((self.host / 'proc/self/mountinfo').read_bytes(), snapshot)
        self._check_mount_identity(target, same_device=same_device)
        self._normal_access(exclude='mounted')
        self._child_denied()
        self._project_mountinfo()
        self.assertIn(projected_path, (self.host / 'proc/self/mountinfo').read_bytes())
        self._normal_access(exclude='mounted')
        self._child_denied()

    def test_same_device_bind_descendant_skips_only_mount_entry(self):
        target, source = self._mounted_target(), self.fixture / 'bind-source'
        source.mkdir()
        self._sentinel(source / 'visible.txt', b'bind source remains')
        snapshot = (self.host / 'proc/self/mountinfo').read_bytes()
        self._mount(target, [self.mount_bin, '--bind', str(source), str(target)], project=False)
        self._child_mount_phases(target, snapshot, same_device=True)
        self._unmount(target)
        self._project_mountinfo()
        self._normal_access()
        self.assertTrue(self._call('get_file_info', 'mounted/underlying.txt')['ok'])
        self.case_passed = True

    def test_real_overlay_descendant_skips_only_mount_entry(self):
        target = self._mounted_target()
        lower, upper, work = (self.fixture / ('overlay-' + name) for name in ('lower', 'upper', 'work'))
        for path in (lower, upper, work):
            path.mkdir()
        self._sentinel(lower / 'visible.txt', b'overlay lower remains')
        options = f'lowerdir={lower},upperdir={upper},workdir={work}'
        snapshot = (self.host / 'proc/self/mountinfo').read_bytes()
        self._mount(target, [self.mount_bin, '-t', 'overlay', 'overlay', '-o', options, str(target)], project=False)
        self.assertEqual(self._entry(target)['after'][0], 'overlay')
        self._child_mount_phases(target, snapshot, same_device=False)
        self._unmount(target)
        self._project_mountinfo()
        self._normal_access()
        self.assertTrue(self._call('get_file_info', 'mounted/underlying.txt')['ok'])
        self.case_passed = True

    def _root_bind(self, component):
        target, source = self.disk / component, self.physical / component
        before = target.stat()
        self._mount(target, [self.mount_bin, '--bind', str(source), str(target)])
        after = target.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self._check_mount_identity(target, same_device=True)
        self._root_denied()
        self._unmount(target)
        self._project_mountinfo()
        self._normal_access()
        self.case_passed = True

    def test_same_device_users_bind_rejects_the_whole_admin_page(self):
        self._root_bind('Users')

    def test_same_device_admin_bind_rejects_the_whole_admin_page(self):
        self._root_bind('Users/admin')

    def _cleanup(self):
        """Fail closed: plain umount, prove absence, then delete owned temp data."""
        errors = []
        if self.executor is not None:
            try:
                self.executor.close()
            except Exception as error:
                errors.append(str(error))
        for target in reversed(self.targets):
            try:
                self._unmount(target)
            except Exception as error:
                errors.append(str(error))
        try:
            self._validate_fixture()
            remaining = [row['path'] for row in _kernel_mounts()
                         if row['path'] == str(self.fixture) or row['path'].startswith(str(self.fixture) + '/')]
            self.assertFalse(remaining, 'owned mounts still present: ' + repr(remaining))
            self.assertFalse(errors, '; '.join(errors))
            self._check_sentinels()
            self.assertTrue(self.case_passed, 'test did not complete; retain evidence')
            # Delete the unmounted virtual host first; source sentinels outside it
            # must survive, proving cleanup cannot traverse a still-mounted source.
            self.assertEqual(self.host.resolve(strict=True), self.fixture / 'host')
            shutil.rmtree(self.host)
            self._check_sentinels()
            self._validate_fixture()
            self.assertFalse(any(row['path'] == str(self.fixture) or row['path'].startswith(str(self.fixture) + '/')
                                 for row in _kernel_mounts()))
            shutil.rmtree(self.fixture)
        except Exception as error:
            print('PRESERVED_FIXTURE=' + str(self.fixture) + '; cleanup/test failure: ' + str(error), file=sys.stderr)
            raise


if __name__ == '__main__':
    unittest.main()
