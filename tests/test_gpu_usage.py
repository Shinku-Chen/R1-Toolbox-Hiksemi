"""Fixture tests for persistent i915 usage sampling."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import gpu_usage


class GpuUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-gpu-usage-')
        self.addCleanup(self.temp.cleanup)
        self.sys_root = Path(self.temp.name)
        pmu = self.sys_root / 'bus/event_source/devices/i915'
        (pmu / 'events').mkdir(parents=True)
        (pmu / 'type').write_text('23\n', encoding='ascii')
        (pmu / 'cpumask').write_text('0\n', encoding='ascii')
        (pmu / 'events/rcs0-busy').write_text('config=0x0\n', encoding='ascii')
        (pmu / 'events/rcs0-busy.unit').write_text('ns\n', encoding='ascii')
        (pmu / 'events/bcs0-busy').write_text('config=0x1000\n', encoding='ascii')
        (pmu / 'events/bcs0-busy.unit').write_text('ns\n', encoding='ascii')
        (pmu / 'events/actual-frequency').write_text('config=0x100000\n', encoding='ascii')
        self.clock = [0.0]
        self.values = {}
        self.next_fd = 100
        self.closed = []
        self.opened = []

    def opener(self, pmu_type, config, cpu):
        fd = self.next_fd
        self.next_fd += 1
        self.opened.append((pmu_type, config, cpu, fd))
        self.values[fd] = 0
        return fd

    def reader(self, fd):
        return self.values[fd]

    def test_first_sample_warms_then_reports_max_engine(self):
        sampler = gpu_usage.IntelGpuSampler(
            self.sys_root, opener=self.opener, reader=self.reader,
            closer=lambda fd: self.closed.append(fd), clock=lambda: self.clock[0])
        first = sampler.sample()
        self.assertTrue(first['warming'])
        self.assertIsNone(first['util'])
        self.assertEqual(len(self.opened), 2)
        self.clock[0] += 0.2
        self.values[self.opened[0][3]] = 30_000_000
        self.values[self.opened[1][3]] = 10_000_000
        second = sampler.sample()
        self.assertFalse(second['warming'])
        self.assertEqual(second['util'], 15.0)
        self.assertEqual(second['util_source'], 'pmu')
        self.assertEqual(second['engines'], {'bcs0': 15.0, 'rcs0': 5.0})
        sampler.close()
        self.assertEqual(self.closed, [100, 101])

    def test_read_error_closes_and_falls_back_without_fake_zero(self):
        sampler = gpu_usage.IntelGpuSampler(
            self.sys_root, opener=self.opener, reader=lambda fd: (_ for _ in ()).throw(OSError('read failed')),
            closer=lambda fd: self.closed.append(fd), clock=lambda: self.clock[0])
        self.assertIsNone(sampler.sample())
        self.clock[0] += 0.2
        value = sampler.sample()
        self.assertIsNone(value)
        self.assertEqual(self.closed, [100, 101])

    def test_missing_pmu_is_unavailable(self):
        sampler = gpu_usage.IntelGpuSampler(
            self.sys_root / 'empty', opener=self.opener, reader=self.reader,
            closer=lambda fd: None, clock=lambda: self.clock[0])
        self.assertIsNone(sampler.sample())
        self.assertEqual(self.opened, [])


if __name__ == '__main__':
    unittest.main()
