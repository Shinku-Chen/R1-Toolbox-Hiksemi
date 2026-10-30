"""Fixed write-index diagnostics never allocate staging or publish a file."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import api_file_provider as files
import api_file_native_index as index
import api_file_native_index_reader as reader
from api_file_operations import FileAccessError

ROOT = 'root_' + 'a' * 24


class WritePreflightTests(unittest.TestCase):
    def setUp(self):
        self.panel = object()
        self.row = {'id': ROOT, 'mount': '/drives/fixture/Users/admin', 'read_only': False}
        self.authority_patch = mock.patch.object(files, 'NativeOwnerWriteAuthority')
        self.authority = self.authority_patch.start()
        self.addCleanup(self.authority_patch.stop)
        self.authority.return_value.authorize.return_value = self.row
        self.index_patch = mock.patch.object(index, 'NativeFileIndex')
        self.backend = self.index_patch.start()
        self.addCleanup(self.index_patch.stop)

        def observed_success(*args, **kwargs):
            observer = self.backend.call_args.kwargs['observer']
            for stage in ('reader.final_check', 'index.final_current'):
                observer(stage, 'start')
                observer(stage, 'pass')

        self.backend.return_value.preflight.side_effect = observed_success

    def collect(self, current=lambda: True):
        return files.collect_file_write_preflight(self.panel, ROOT, authorizer=current)

    def test_one_generated_exact_key_reuses_authority_and_native_preflight_only(self):
        result = self.collect()
        self.assertTrue(result['ready'])
        self.assertEqual(result['scope'], 'native_index_preflight')
        call = self.authority.return_value.authorize.call_args
        self.assertEqual(call.args[0], ROOT)
        self.assertRegex(call.args[1], r'^r1-toolbox-preflight-[0-9a-f]{32}$')
        self.assertTrue(call.args[2]())
        self.backend.return_value.preflight.assert_called_once_with(
            self.row, call.args[1], current=call.args[2])
        self.backend.return_value.notify_new.assert_not_called()
        self.backend.return_value.verify_new.assert_not_called()
        self.assertFalse(result['file_published'])
        self.assertFalse(result['native_notification_sent'])
        self.assertFalse(result['write_acceptance_proven'])
        self.assertTrue(result['may_wake_disks'])
        self.assertTrue(result['sqlite_sidecar_metadata_may_change'])
        self.assertNotIn(call.args[1], json.dumps(result))
        self.assertNotIn(self.row['mount'], json.dumps(result))
        self.assertEqual(result['root_id'], ROOT)

    def test_authority_failure_does_not_touch_native_index(self):
        self.authority.return_value.authorize.side_effect = FileAccessError('acl_unverified')
        result = self.collect()
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'acl_unverified')
        self.assertEqual(result['failure_stage'], 'authority.native_owner')
        self.backend.assert_not_called()
        others = [row for row in result['stages'] if row['id'] != 'authority.native_owner']
        self.assertTrue(all(row['state'] == 'not_run' for row in others))

    def test_missing_final_markers_cannot_be_reported_ready(self):
        self.backend.return_value.preflight.side_effect = None
        result = self.collect()
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'result_unconfirmed')
        self.assertTrue(result['trace_schema_valid'])

    def test_failed_stage_is_fixed_and_later_stages_remain_unrun(self):
        stage = reader.PREFLIGHT_STAGES[-1]

        def failure(*args, **kwargs):
            observer = self.backend.call_args.kwargs['observer']
            observer(stage, 'start')
            raise FileAccessError('write_backend_unverified')

        self.backend.return_value.preflight.side_effect = failure
        result = self.collect()
        self.assertFalse(result['ready'])
        self.assertEqual(result['failure_stage'], stage)
        rows = {row['id']: row for row in result['stages']}
        self.assertEqual(rows[stage]['state'], 'failed')
        self.assertEqual(rows['authority.final_authorization']['state'], 'not_run')

    def test_exception_text_and_unknown_observer_values_cannot_escape(self):
        def failure(*args, **kwargs):
            observer = self.backend.call_args.kwargs['observer']
            observer('PRIVATE-PATH-AND-TOKEN', 'start')
            raise RuntimeError('PRIVATE-SQL-ROW-AND-SESSION')

        self.backend.return_value.preflight.side_effect = failure
        result = self.collect()
        self.assertEqual(result['error_code'], 'write_backend_unverified')
        self.assertFalse(result['trace_schema_valid'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_expired_developer_window_prevents_native_access_and_drops_readiness(self):
        result = self.collect(lambda: False)
        self.assertEqual(result['error_code'], 'invalid_key')
        self.authority.assert_not_called()
        self.backend.assert_not_called()
        valid = True

        def expire(*args, **kwargs):
            nonlocal valid
            valid = False

        self.backend.return_value.preflight.side_effect = expire
        result = self.collect(lambda: valid)
        self.assertFalse(result['ready'])
        self.assertEqual(result['failure_stage'], 'authority.final_authorization')
        self.assertEqual(result['error_code'], 'invalid_key')

    def test_invalid_ids_never_reach_authority(self):
        for invalid in ('/drives/private', None, True, ROOT + '\n', 'root_' + 'A' * 24):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                files.collect_file_write_preflight(self.panel, invalid, authorizer=lambda: True)
        self.authority.assert_not_called()
        self.backend.assert_not_called()


class WritePreflightTraceTests(unittest.TestCase):
    def test_repeated_checks_are_bounded_and_nested_failure_is_identified(self):
        trace = files._WritePreflightTrace(('outer', 'inner', 'dependent'))
        trace('outer', 'start')
        for _ in range(3):
            trace('inner', 'start')
            trace('inner', 'pass')
        trace('inner', 'start')
        self.assertEqual(trace.finish('timeout'), 'inner')
        self.assertEqual(trace.rows['inner']['attempts'], 4)
        self.assertEqual(trace.rows['inner']['state'], 'failed')
        self.assertEqual(trace.rows['outer']['state'], 'unconfirmed')
        self.assertEqual(trace.rows['dependent']['state'], 'not_run')
        self.assertEqual(len(trace.rows), 3)

    def test_unclosed_or_invalid_trace_never_looks_complete(self):
        trace = files._WritePreflightTrace(('gate',))
        trace('gate', 'start')
        self.assertEqual(trace.finish(), 'gate')
        self.assertEqual(trace.rows['gate']['state'], 'unconfirmed')
        trace('unknown', 'start')
        self.assertFalse(trace.valid)
        self.assertNotIn('unknown', trace.rows)


if __name__ == '__main__':
    unittest.main()
