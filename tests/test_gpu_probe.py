"""Fixture-only tests for the fixed i915 PMU probe."""

import errno
from pathlib import Path
import tempfile
import unittest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
import gpu_probe


class GpuProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='r1-gpu-probe-')
        self.addCleanup(self.temp.cleanup)
        self.sys_root = Path(self.temp.name)
        pmu = self.sys_root / 'bus/event_source/devices/i915'
        (pmu / 'events').mkdir(parents=True)
        (pmu / 'type').write_text('10\n', encoding='ascii')
        (pmu / 'cpumask').write_text('0-3\n', encoding='ascii')
        (pmu / 'events/rcs0-busy').write_text('config=0x100\n', encoding='ascii')
        (pmu / 'events/rcs0-busy.unit').write_text('ns' + chr(10), encoding='ascii')
        (pmu / 'events/bcs0-busy').write_text('config=0x200\n', encoding='ascii')
        (pmu / 'events/bcs0-busy.unit').write_text('ns' + chr(10), encoding='ascii')
        rc6 = self.sys_root / 'class/drm/card0/gt/gt0'
        rc6.mkdir(parents=True)
        (rc6 / 'rc6_residency_ms').write_text('1000\n', encoding='ascii')
        proc = self.sys_root / 'proc/sys/kernel'
        proc.mkdir(parents=True)
        (proc / 'perf_event_paranoid').write_text('2\n', encoding='ascii')
        self.rc6_calls = 0

    def sys_reader(self, path):
        if path.name == 'rc6_residency_ms':
            self.rc6_calls += 1
            return '1000\n' if self.rc6_calls == 1 else '1100\n'
        return Path(path).read_text(encoding='ascii')

    def test_discovers_and_reads_fixed_i915_events_and_rc6(self):
        opened, closed, controlled, slept = [], [], [], []
        def opener(pmu_type, config, cpu):
            opened.append((pmu_type, config, cpu))
            return 40 + len(opened)
        def reader(fd):
            return 75_000_000
        def controller(fd, request):
            controlled.append((fd, request))
        def closer(fd):
            closed.append(fd)
        value = gpu_probe.probe(sys_root=str(self.sys_root), proc_root=str(self.sys_root / "proc"),
                                opener=opener, reader=reader, controller=controller,
                                closer=closer, sleeper=lambda seconds: slept.append(seconds),
                                sys_reader=self.sys_reader)
        self.assertTrue(value['read_only'])
        self.assertEqual(value['perf_event_paranoid'], 2)
        candidate = value['pmu']['candidates'][0]
        self.assertEqual(candidate['name'], 'i915')
        self.assertEqual(candidate['type'], 10)
        self.assertEqual(candidate['events_seen'], 2)
        self.assertEqual(value['pmu']['opened_events'], 2)
        self.assertEqual(value['pmu']['readable_events'], 2)
        self.assertEqual([item['name'] for item in candidate['events']], ['bcs0-busy', 'rcs0-busy'])
        self.assertTrue(all(item['open'] and item['read'] for item in candidate['events']))
        self.assertEqual({item['sample_utilization_percent'] for item in candidate['events']}, {50.0})
        self.assertEqual(opened, [(10, 0x200, 0), (10, 0x100, 0)])
        self.assertEqual(closed, [41, 42])
        self.assertTrue(value['rc6']['available'])
        self.assertEqual(value['rc6']['utilization_percent'], 50.0)
        self.assertIn(gpu_probe.SAMPLE_SECONDS, slept)

    def test_open_failure_is_reported_and_rc6_still_runs(self):
        def opener(*_):
            raise OSError(errno.EACCES, 'private kernel detail')
        value = gpu_probe.probe(sys_root=str(self.sys_root), proc_root=str(self.sys_root / "proc"),
                                opener=opener, reader=lambda fd: 0,
                                controller=lambda fd, request: None,
                                closer=lambda fd: None, sleeper=lambda seconds: None,
                                sys_reader=self.sys_reader)
        events = value['pmu']['candidates'][0]['events']
        self.assertEqual({item['errno'] for item in events}, {'EACCES'})
        self.assertEqual(value['pmu']['opened_events'], 0)
        self.assertEqual(value['reason'], 'no_event_opened')
        self.assertNotIn('private kernel detail', str(value))
        self.assertTrue(value['rc6']['available'])

    def test_missing_pmu_is_fixed_unknown(self):
        value = gpu_probe.probe(sys_root=str(self.sys_root / 'empty'), proc_root=str(self.sys_root / "proc"),
                                opener=lambda *_: 1, reader=lambda fd: 0,
                                controller=lambda fd, request: None,
                                closer=lambda fd: None, sleeper=lambda seconds: None,
                                sys_reader=lambda path: '1000')
        self.assertFalse(value['pmu']['available'])
        self.assertEqual(value['pmu']['candidates'], [])
        self.assertEqual(value['reason'], 'i915_pmu_not_found')
        self.assertFalse(value['rc6']['available'])


if __name__ == '__main__':
    unittest.main()
