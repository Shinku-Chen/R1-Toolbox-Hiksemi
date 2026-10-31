"""固定转码能力诊断：三个 getter、缺测保持未知、档位规则对齐复核后的 DEX。"""

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from vendor_business_transport import BusinessTransportError, _validate
import vendor_transcode_probe as probe


PATH = '/drives/sda/Users/admin/Movies/fixture.mkv'
SECRET = 'private-transcode-secret-never-leaves-host'
PRIVATE_PATH = '/drives/sda/Users/admin/private/' + SECRET
HASH_PATH = '/drives/sda/Users/admin/.transcode/' + SECRET


class FakeClient:
    """Transport stand-in with the same request signature and no sockets."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, operation, parameters=None, body=None, authorizer=None, *,
                confirmed=False):
        self.calls.append((operation, dict(parameters or {}), dict(body or {})))
        if not callable(authorizer) or authorizer(operation, dict(parameters or {}), {}) is not True:
            raise BusinessTransportError('authorization_denied')
        value = self.responses.get(operation)
        if value is None:
            raise BusinessTransportError('unsupported_operation')
        if isinstance(value, Exception):
            raise value
        return value


def capabilities(transcode=1, switch=1):
    return (200, {'code': 0, 'capacity_list': [
        {'capacity_name': 'cap_transcode_video', 'capacity_detail': transcode},
        {'capacity_name': 'cap_tr_video_switch_v1', 'capacity_detail': switch},
        {'capacity_name': 'cap_external_backup', 'capacity_detail': 1,
         'path': PRIVATE_PATH, 'access_token': SECRET},
    ], 'access_token': SECRET})


def switch_frame(mode=1):
    return (200, {'code': 0, 'mode': mode, 'msg': SECRET})


def capacity_frame(profile='high,mid,low', *, status='ready', video_720p=None):
    return (200, {
        'code': 0, 'status': status, 'video_720p': video_720p, 'msg': SECRET,
        'capabilities': {
            'hash_path': HASH_PATH, 'path': PRIVATE_PATH, 'duration': 120,
            'video': [{'name': 'main', 'profile': profile, 'offlineprofile': 'origin',
                       'width': 1920, 'height': 1080, 'bit_rate': 8000000,
                       'frame_rate': 24, 'codec_name': 'hevc', 'codec_type': 'video',
                       'path': PRIVATE_PATH}],
            'audio': [{'codec_name': 'aac', 'path': PRIVATE_PATH}],
            'subtitle': [],
        }})


_MISSING = object()


def shape_frame(*, capabilities=_MISSING, video=_MISSING, profile='high,mid,low',
                status='ready', video_720p=None):
    """Build one get_offlinevideo_path frame with an explicit container shape."""
    payload = {'code': 0, 'status': status, 'msg': SECRET}
    if video_720p is not None:
        payload['video_720p'] = video_720p
    if capabilities is _MISSING:
        payload['capabilities'] = {
            'hash_path': HASH_PATH,
            'video': ([{'name': 'main', 'profile': profile,
                        'width': 1920, 'height': 1080}]
                      if video is _MISSING else video),
        }
    else:
        payload['capabilities'] = capabilities
    return (200, payload)


def responses(**overrides):
    base = {'video_capacity': capabilities(), 'transcode_switch': switch_frame(),
            'offline_video_path': capacity_frame()}
    base.update(overrides)
    return base


def run(client, path=PATH, authorizer=None):
    return probe.collect_transcode_probe(
        path, authorizer=authorizer or (lambda operation, parameters, body: True),
        client=client)


class CapabilityProjectionTests(unittest.TestCase):
    def test_capability_projection_keeps_only_audited_names(self):
        value = probe.project_capabilities(capabilities()[1])
        self.assertEqual(value['values'], {'cap_transcode_video': 1,
                                           'cap_tr_video_switch_v1': 1})
        self.assertEqual(value['states'], {'cap_transcode_video': 'enabled',
                                           'cap_tr_video_switch_v1': 'enabled'})
        self.assertEqual(value['structure'], 'ok')
        self.assertEqual(value['unknown_entries'], 1)
        dumped = json.dumps(value)
        self.assertNotIn('cap_external_backup', dumped)
        self.assertNotIn(SECRET, dumped)

    def test_missing_or_empty_list_is_unknown_not_unsupported(self):
        missing = probe.project_capabilities({'code': 0})
        self.assertEqual(missing['structure'], 'missing')
        self.assertEqual(missing['states']['cap_transcode_video'], 'unknown')
        empty = probe.project_capabilities({'code': 0, 'capacity_list': []})
        self.assertEqual(empty['structure'], 'empty')
        self.assertEqual(empty['states']['cap_transcode_video'], 'unknown')
        malformed = probe.project_capabilities({'code': 0, 'capacity_list': [
            'garbage', {'capacity_name': 1, 'capacity_detail': 1}]})
        self.assertEqual(malformed['malformed_entries'], 1)
        self.assertEqual(malformed['unknown_entries'], 1)
        self.assertEqual(malformed['states']['cap_tr_video_switch_v1'], 'unknown')

    def test_explicit_zero_entry_is_reported_as_disabled(self):
        value = probe.project_capabilities(capabilities(transcode=0)[1])
        self.assertEqual(value['states']['cap_transcode_video'], 'disabled')

    def test_switch_mode_maps_to_open_state_without_guessing(self):
        self.assertEqual(probe.project_switch({'code': 0, 'mode': 1})['open'], True)
        self.assertEqual(probe.project_switch({'code': 0, 'mode': 0})['open'], False)
        unknown = probe.project_switch({'code': 0, 'mode': 7})
        self.assertIsNone(unknown['mode'])
        self.assertIsNone(unknown['open'])

    def test_offline_path_projection_returns_metadata_but_no_paths(self):
        value = probe.project_offline_path(capacity_frame()[1])
        self.assertEqual(value['video'][0]['profile'], 'high,mid,low')
        self.assertEqual(value['video'][0]['height'], 1080)
        self.assertIs(value['hash_path_present'], True)
        self.assertIs(value['video_720p_present'], False)
        dumped = json.dumps(value)
        for marker in (HASH_PATH, PRIVATE_PATH, SECRET):
            self.assertNotIn(marker, dumped)

    def test_missing_status_stays_unknown_instead_of_not_ready(self):
        frame = capacity_frame()[1]
        frame.pop('status')
        self.assertIsNone(probe.project_offline_path(frame)['ready'])


class DefinitionTests(unittest.TestCase):
    """Oracle cases derived from classes.dex fd7af59c… onCapacityResult."""

    def labels(self, profile, *, video_720p=None):
        return probe.derive_definitions(probe.project_offline_path(
            capacity_frame(profile, video_720p=video_720p)[1]))['labels']

    def shape(self, **kwargs):
        return probe.derive_definitions(
            probe.project_offline_path(shape_frame(**kwargs)[1]))

    def test_profile_codes_become_the_same_labels_the_tv_player_shows(self):
        self.assertEqual(self.labels('high,mid,low'), ['原画', '高清', '标清', '流畅'])

    def test_offline_720p_appends_mid_after_the_profile_branch(self):
        # 0x0124 结束拆分后落入 0x0126；profile 不含 mid 且 720p 非空 → 追加标清。
        self.assertEqual(self.labels('high', video_720p='/private/' + SECRET),
                         ['原画', '高清', '标清'])

    def test_origin_profile_without_mid_and_with_720p_adds_one_definition(self):
        self.assertEqual(self.labels('origin', video_720p='/private/' + SECRET),
                         ['原画', '标清'])

    def test_origin_profile_already_containing_mid_skips_the_append(self):
        # 0x013e contains("mid") → 0x0146 if-nez → 0x016c 跳过追加。
        self.assertEqual(self.labels('origin,mid', video_720p='/private/' + SECRET),
                         ['原画'])

    def test_profile_without_offline_720p_never_guesses_mid(self):
        self.assertEqual(self.labels('origin'), ['原画'])

    def test_empty_profile_only_uses_the_offline_720p_definition(self):
        self.assertEqual(self.labels('', video_720p='/private/' + SECRET), ['原画', '标清'])

    def test_null_capabilities_has_its_own_720p_fallback(self):
        value = self.shape(capabilities=None, video_720p='/private/' + SECRET)
        self.assertEqual(value['labels'], ['原画', '标清'])
        self.assertEqual(value['source'], 'video_720p')
        self.assertEqual(value['capabilities_state'], 'null')

    def test_object_without_usable_video_rows_never_appends_mid(self):
        cases = ({'video': Path and None}, {'video': []}, {'video': 'garbage'},
                 {'capabilities': {'hash_path': HASH_PATH}})
        for case in cases:
            with self.subTest(case=case):
                kwargs = {'video_720p': '/private/' + SECRET}
                if 'capabilities' in case:
                    kwargs['capabilities'] = case['capabilities']
                else:
                    kwargs['video'] = case['video']
                value = self.shape(**kwargs)
                self.assertEqual(value['labels'], [] if case.get('video') == 'garbage' else ['原画'])
                self.assertEqual(value['source'], 'unknown' if case.get('video') == 'garbage' else 'none')
                self.assertEqual(value['capabilities_state'], 'object')

    def test_empty_object_capabilities_never_appends_mid(self):
        value = self.shape(capabilities={}, video_720p='/private/' + SECRET)
        self.assertEqual(value['labels'], ['原画'])
        self.assertEqual(value['capabilities_state'], 'object')
        self.assertEqual(value['video_state'], 'null')

    def test_unknown_profile_token_is_withheld_and_cannot_become_a_label(self):
        value = probe.derive_definitions(
            probe.project_offline_path(capacity_frame('high,x265')[1]))
        self.assertEqual(value['labels'], [])
        self.assertEqual(value['unmapped_profile_count'], 1)
        self.assertNotIn('x265', json.dumps(value))

    def test_switchable_requires_more_than_the_original_label(self):
        origin_mid = probe.derive_definitions(
            probe.project_offline_path(capacity_frame('origin,mid')[1]))
        self.assertIs(origin_mid['switchable'], False)
        self.assertIs(origin_mid['mid_marker_present'], True)


class CollectTests(unittest.TestCase):
    def test_three_fixed_getters_are_sent_in_order_with_fixed_parameters(self):
        client = FakeClient(responses())
        value = run(client)
        self.assertEqual([call[0] for call in client.calls],
                         ['video_capacity', 'transcode_switch', 'offline_video_path'])
        self.assertEqual(client.calls[0][1], {})
        self.assertEqual(client.calls[1][1], {})
        self.assertEqual(client.calls[2][1], {
            'video_capability': 1, 'path': PATH, 'offset': 0,
            'sharpness': 'raw', 'terminal': probe.TERMINAL})
        self.assertIs(value['available'], True)
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['tv_player']['playback'], 'hls')
        self.assertIs(value['tv_player']['definition_menu_visible'], True)
        self.assertEqual(value['tv_player']['labels'], ['原画', '高清', '标清', '流畅'])

    def test_result_carries_no_private_marker_from_any_step(self):
        value = run(FakeClient(responses()))
        dumped = json.dumps(value, ensure_ascii=False)
        for marker in (SECRET, PRIVATE_PATH, HASH_PATH, PATH):
            self.assertNotIn(marker, dumped)
        self.assertEqual(len(value['request']['path_sha256_prefix']), 16)

    def test_empty_capacity_list_is_unknown_not_unsupported(self):
        value = run(FakeClient(responses(
            video_capacity=(200, {'code': 0, 'capacity_list': []}))))
        self.assertEqual(value['capability']['structure'], 'empty')
        self.assertEqual(value['capability']['states']['cap_transcode_video'], 'unknown')
        self.assertEqual(value['reason'], 'capability_unknown')
        self.assertEqual(value['status'], 'unknown')
        self.assertIs(value['available'], False)
        self.assertIsNone(value['tv_player']['definition_menu_visible'])
        self.assertIsNone(value['tv_player']['playback'])

    def test_missing_capacity_structure_is_unknown_and_stops_the_projection(self):
        value = run(FakeClient(responses(video_capacity=(200, {'code': 0}))))
        self.assertIsNone(value['capability'])
        self.assertEqual(value['steps']['capability']['reason'], 'unexpected_response')
        self.assertEqual(value['reason'], 'capability_unknown')
        self.assertEqual(value['status'], 'unknown')
        self.assertIs(value['available'], False)
        self.assertIsNone(value['tv_player']['playback'])

    def test_capability_step_failure_stays_unknown_instead_of_original_only(self):
        value = run(FakeClient(responses(
            video_capacity=BusinessTransportError('http_error', 503))))
        self.assertEqual(value['steps']['capability']['reason'], 'http_error')
        self.assertEqual(value['status'], 'unknown')
        self.assertEqual(value['reason'], 'capability_unknown')
        self.assertIs(value['available'], False)
        self.assertIsNone(value['tv_player']['playback'])
        self.assertIsNone(value['tv_player']['definition_menu_visible'])

    def test_failed_switch_query_falls_back_to_client_default_without_claiming_open(self):
        # classes2.dex AbilityConfig$2.onFailure(code_off 0x222a98)：videoSwitchOpen=true。
        failures = ((BusinessTransportError('http_error', 503), 'open_on_failure'),
                    ((200, {'code': 5400, 'mode': 1}), 'open_on_native_rejection'))
        for failure, fallback in failures:
            with self.subTest(failure=failure):
                value = run(FakeClient(responses(transcode_switch=failure)))
                self.assertIsNone(value['switch'])
                player = value['tv_player']
                self.assertEqual(player['switch_state'], 'fallback_open_unverified')
                self.assertEqual(player['switch_client_fallback'], fallback)
                # 客户端回落继续，不是电视禁用清晰度的已证实门。
                self.assertEqual(player['playback'], 'hls')
                self.assertEqual(value['status'], 'ok')
                self.assertIs(value['available'], True)
                self.assertEqual(value['evidence']['server_switch_state'], None)
                self.assertEqual(value['evidence']['client_switch_fallback'], fallback)
                self.assertIs(value['playback_observed'], False)
                self.assertIs(value['steps']['switch']['ok'], False)

    def test_switch_capability_zero_is_not_requested_by_the_tv_client(self):
        value = run(FakeClient(responses(video_capacity=capabilities(switch=0))))
        self.assertIs(value['request']['switch_query_in_tv_flow'], False)
        self.assertEqual(value['request']['extra_getters_not_in_tv_flow'], ['transcode_switch'])
        self.assertEqual(value['tv_player']['switch_state'],
                         'not_requested_capability_disabled')
        self.assertEqual(value['tv_player']['playback'], 'hls')
        self.assertEqual(value['evidence']['server_switch_state'], 'open')
        self.assertIs(value['playback_observed'], False)

    def test_switch_query_is_part_of_the_tv_flow_when_capability_is_one(self):
        value = run(FakeClient(responses()))
        self.assertIs(value['request']['switch_query_in_tv_flow'], True)
        self.assertEqual(value['request']['extra_getters_not_in_tv_flow'], [])
        self.assertEqual(value['evidence']['server_switch_state'], 'open')

    def test_missing_capacity_status_is_partial_and_never_claims_hls(self):
        frame = capacity_frame()[1]
        frame.pop('status')
        value = run(FakeClient(responses(offline_video_path=(200, frame))))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['reason'], 'capacity_status_unknown')
        self.assertIsNone(value['tv_player']['playback'])
        self.assertIsNone(value['capacity']['ready'])

    def test_capacity_query_failure_is_partial_with_its_own_reason(self):
        value = run(FakeClient(responses(
            offline_video_path=BusinessTransportError('timeout'))))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['reason'], 'capacity_query_failed')
        self.assertEqual(value['steps']['capacity']['reason'], 'timeout')

    def test_absent_transcode_capability_is_reported_as_disabled(self):
        value = run(FakeClient(responses(video_capacity=capabilities(transcode=0))))
        self.assertEqual(value['reason'],
                         'device_reports_no_video_transcode_capability')
        self.assertIs(value['tv_player']['definition_menu_visible'], False)
        self.assertEqual(value['tv_player']['playback'], 'original')
        self.assertEqual(value['status'], 'ok')
        self.assertIs(value['available'], True)

    def test_closed_transcode_switch_hides_the_tv_menu(self):
        value = run(FakeClient(responses(transcode_switch=switch_frame(0))))
        self.assertEqual(value['reason'], 'transcode_switch_closed')
        self.assertIs(value['tv_player']['definition_menu_visible'], False)
        self.assertEqual(value['tv_player']['switch_state'], 'closed')

    def test_switch_without_capability_support_is_not_treated_as_closed(self):
        value = run(FakeClient(responses(video_capacity=capabilities(switch=0),
                                         transcode_switch=switch_frame(0))))
        self.assertIs(value['switch']['open'], False)
        self.assertEqual(value['tv_player']['switch_state'],
                         'not_requested_capability_disabled')
        self.assertIs(value['tv_player']['definition_menu_visible'], True)

    def test_single_definition_keeps_hls_because_list_size_is_not_the_trigger(self):
        value = run(FakeClient(responses(offline_video_path=capacity_frame('origin'))))
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['tv_player']['reason'], 'hls_without_alternatives')
        self.assertEqual(value['tv_player']['playback'], 'hls')
        self.assertEqual(value['tv_player']['labels'], ['原画'])

    def test_null_capabilities_keeps_labels_but_withholds_playback(self):
        frame = shape_frame(capabilities=None, video_720p='/private/' + SECRET)[1]
        value = run(FakeClient(responses(offline_video_path=(200, frame))))
        self.assertEqual(value['capacity']['capabilities_state'], 'null')
        self.assertEqual(value['definitions']['labels'], ['原画', '标清'])
        # 0x01a6 在 capabilities 为空时直接返回，不建 m3u8。
        self.assertIsNone(value['tv_player']['playback'])
        self.assertEqual(value['reason'], 'capacity_structure_missing')
        self.assertEqual(value['status'], 'partial')
        self.assertIs(value['available'], True)

    def test_object_shapes_without_video_rows_keep_original_list_and_hls(self):
        cases = ({'video': None}, {'video': []}, {'video': 'garbage'},
                 {'capabilities': {'hash_path': HASH_PATH}})
        for case in cases:
            with self.subTest(case=case):
                kwargs = {'video_720p': '/private/' + SECRET}
                kwargs.update(case)
                frame = shape_frame(**kwargs)[1]
                value = run(FakeClient(responses(offline_video_path=(200, frame))))
                if case.get('video') == 'garbage':
                    self.assertEqual(value['definitions']['labels'], [])
                    self.assertIsNone(value['tv_player']['playback'])
                    self.assertEqual(value['status'], 'partial')
                    continue
                self.assertEqual(value['definitions']['labels'], ['原画'])
                # capabilities 是对象且已就绪 → 原厂仍走 m3u8，与档位数量无关。
                self.assertEqual(value['tv_player']['playback'], 'hls')
                self.assertEqual(value['tv_player']['reason'], 'hls_without_alternatives')
                self.assertEqual(value['status'], 'ok')

    def test_not_ready_falls_back_to_original_instead_of_hls(self):
        value = run(FakeClient(responses(
            offline_video_path=capacity_frame('high', status='pending'))))
        self.assertEqual(value['tv_player']['playback'], 'original')
        self.assertEqual(value['tv_player']['reason'], 'original_fallback')
        self.assertEqual(value['status'], 'ok')

    def test_conflicting_duplicate_capability_rows_are_unknown_in_both_orders(self):
        rows = [{'capacity_name': 'cap_tr_video_switch_v1', 'capacity_detail': 1}]
        conflicting = [{'capacity_name': 'cap_transcode_video', 'capacity_detail': 0},
                       {'capacity_name': 'cap_transcode_video', 'capacity_detail': 1}]
        first = run(FakeClient(responses(video_capacity=(200, {'code': 0, 'capacity_list': conflicting + rows}))))
        second = run(FakeClient(responses(video_capacity=(200, {'code': 0, 'capacity_list': list(reversed(conflicting)) + rows}))))
        for value in (first, second):
            self.assertEqual(value['capability']['values']['cap_transcode_video'], None)
            self.assertEqual(value['capability']['states']['cap_transcode_video'], 'unknown')
            self.assertEqual(value['capability']['conflicting_entries'], 1)
            self.assertEqual(value['reason'], 'capability_unknown')
            self.assertEqual(value['status'], 'unknown')
            self.assertIsNone(value['tv_player']['playback'])
        self.assertEqual(first['tv_player'], second['tv_player'])

    def test_agreeing_duplicate_capability_rows_keep_the_known_value(self):
        rows = [{'capacity_name': 'cap_transcode_video', 'capacity_detail': 1},
                {'capacity_name': 'cap_transcode_video', 'capacity_detail': 1}]
        value = run(FakeClient(responses(
            video_capacity=(200, {'code': 0, 'capacity_list': rows}))))
        self.assertEqual(value['capability']['states']['cap_transcode_video'], 'enabled')
        self.assertEqual(value['capability']['duplicate_entries'], 1)
        self.assertEqual(value['capability']['conflicting_entries'], 0)

    def test_capacity_rejection_keeps_code_and_maps_the_tv_branch(self):
        """-5402 是原厂对该请求形态的固定拒绝；TV 会清空档位并提示不支持。"""
        frame = (200, {'code': -5402, 'msg': SECRET})
        value = run(FakeClient(responses(offline_video_path=frame)))
        step = value['steps']['capacity']
        self.assertIs(step['ok'], False)
        self.assertEqual(step['native_code'], -5402)
        self.assertEqual(value['capacity']['structure'], 'rejected')
        self.assertEqual(value['capacity']['code'], -5402)
        self.assertIsNone(value['definitions'])
        player = value['tv_player']
        self.assertEqual(player['reason'], 'video_capability_query_unsupported')
        self.assertEqual(player['playback'], 'original')
        branch = player['capacity_error_branch']
        self.assertEqual(branch['native_code'], -5402)
        self.assertIs(branch['clears_definitions'], True)
        self.assertIs(branch['supports_switch'], False)
        self.assertIs(branch['observed'], False)
        self.assertEqual(value['status'], 'ok')
        self.assertIs(value['playback_observed'], False)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_unknown_native_rejection_code_is_not_called_switching_unsupported(self):
        frame = (200, {'code': -1234, 'msg': SECRET})
        value = run(FakeClient(responses(offline_video_path=frame)))
        self.assertEqual(value['tv_player']['reason'], 'capacity_query_rejected')
        self.assertEqual(value['steps']['capacity']['native_code'], -1234)
        self.assertNotIn(SECRET, json.dumps(value))

    def test_side_effect_flags_do_not_call_this_read_only(self):
        value = run(FakeClient(responses()))
        self.assertNotIn('read_only_query', value)
        self.assertIs(value['explicit_getter_only'], True)
        self.assertIs(value['may_read_media_content'], True)
        self.assertIs(value['may_write_media_xattrs'], True)
        self.assertIs(value['may_wake_disks'], True)
        self.assertIs(value['getter_effects_fully_audited'], False)

    def test_each_failed_step_is_isolated_and_reported(self):
        client = FakeClient(responses(
            video_capacity=BusinessTransportError('http_error', 404),
            transcode_switch=BusinessTransportError('unsupported_operation')))
        value = run(client)
        self.assertEqual(value['steps']['capability']['reason'], 'http_error')
        self.assertEqual(value['steps']['capability']['http_status'], 404)
        self.assertEqual(value['steps']['switch']['reason'], 'unsupported_operation')
        self.assertIs(value['steps']['capacity']['ok'], True)
        self.assertIsNone(value['capability'])
        self.assertIsNone(value['switch'])
        self.assertIsNotNone(value['capacity'])
        self.assertEqual(value['status'], 'unknown')
        self.assertEqual(len(client.calls), 3)

    def test_native_rejection_is_not_read_as_success(self):
        frame = (200, {'code': 5400, 'msg': SECRET})
        value = run(FakeClient(responses(offline_video_path=frame)))
        self.assertEqual(value['steps']['capacity']['reason'], 'native_request_rejected')
        self.assertEqual(value['steps']['capacity']['native_code'], 5400)
        self.assertEqual(value['capacity']['structure'], 'rejected')
        self.assertIsNone(value['definitions'])
        self.assertNotEqual(value['tv_player']['playback'], 'hls')
        self.assertNotIn(SECRET, json.dumps(value))

    def test_codeless_success_needs_a_usable_structure(self):
        usable = (200, {'capacity_list': [
            {'capacity_name': 'cap_transcode_video', 'capacity_detail': 1}]})
        self.assertIs(run(FakeClient(responses(video_capacity=usable)))
                      ['steps']['capability']['ok'], True)
        value = run(FakeClient(responses(video_capacity=(200, {'unexpected': 1}))))
        self.assertEqual(value['steps']['capability']['reason'], 'native_code_unavailable')

    def test_revocation_between_steps_withholds_every_projected_step(self):
        allowed = {'value': True}

        def authorizer(operation, parameters, body):
            return allowed['value']

        client = FakeClient(responses())
        original = client.request

        def request(operation, parameters=None, body=None, authorizer=None, *, confirmed=False):
            value = original(operation, parameters, body, authorizer, confirmed=confirmed)
            if operation == 'video_capacity':
                allowed['value'] = False
            return value

        client.request = request
        value = run(client, authorizer=authorizer)
        self.assertEqual(value['status'], 'denied')
        self.assertEqual(value['reason'], 'authorization_denied')
        self.assertIsNone(value['capability'])
        self.assertIsNone(value['definitions'])

    def test_invalid_path_never_reaches_the_native_client(self):
        client = FakeClient(responses())
        for path in ('', 'relative/x.mkv', '/etc/shadow', '/drives/sda/Users/public/x.mkv',
                     '/drives/sda/Users/admin', '/drives/sda/Users/admin/../x',
                     '/drives//Users/admin/x', '/drives/sda/Users/admin/a\\b',
                     '/drives/sda/Users/admin/x' + '\x00', 12345, None):
            with self.subTest(path=path):
                value = run(client, path=path)
                self.assertIs(value['available'], False)
                self.assertEqual(value['reason'], 'invalid_parameters')
        self.assertEqual(client.calls, [])

    def test_directory_shaped_path_is_accepted_because_only_form_is_checked(self):
        # 纯字符串校验无法判断末段是目录，文档与文案不得宣称能拒绝目录。
        client = FakeClient(responses())
        value = run(client, path='/drives/sda/Users/admin/Movies')
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(len(client.calls), 3)

    def test_missing_authorizer_is_denied_before_any_native_read(self):
        client = FakeClient(responses())
        value = probe.collect_transcode_probe(PATH, client=client)
        self.assertEqual(value['status'], 'denied')
        self.assertEqual(value['reason'], 'authorization_denied')
        self.assertEqual(client.calls, [])


class FinalBoundaryTests(unittest.TestCase):
    """复现真实边界反例，不把错误响应、自由文本或撤权包装成成功。"""

    def assert_denied(self, value):
        self.assertEqual(value['status'], 'denied')
        self.assertEqual(value['reason'], 'authorization_denied')
        self.assertEqual(value['steps'], {})
        for key in ('request', 'scope', 'capability', 'switch', 'capacity',
                    'definitions', 'tv_player', 'evidence'):
            self.assertIsNone(value[key])
        self.assertNotIn(SECRET, json.dumps(value))

    def test_other_rejections_do_not_inherit_minus5402_behavior(self):
        for code in (-5401, -2040, -1234, 5400):
            with self.subTest(code=code):
                value = run(FakeClient(responses(offline_video_path=(200, {'code': code}))))
                self.assertEqual(value['steps']['capacity']['native_code'], code)
                self.assertEqual(value['reason'], 'capacity_query_rejected')
                self.assertEqual(value['status'], 'partial')
                self.assertIsNone(value['definitions'])
                for key in ('playback', 'definition_menu_visible'):
                    self.assertIsNone(value['tv_player'][key])
                branch = value['tv_player']['capacity_error_branch']
                self.assertIsNone(branch['supports_switch'])
                self.assertIsNone(branch['clears_definitions'])

    def test_bad_success_mode_retains_unknown_client_state(self):
        # DEX onSuccess 0070->0088、JSONException catch 均不赋值；旧值可为 false。
        for frame in ({'code': 0}, {'code': 0, 'mode': 99}, {'code': 0, 'mode': '0'},
                      {'code': 0, 'mode': True}, {'code': -1}):
            with self.subTest(frame=frame):
                value = run(FakeClient(responses(transcode_switch=(200, frame))))
                self.assertEqual(value['status'], 'partial')
                self.assertEqual(value['reason'], 'switch_state_unknown')
                self.assertIsNone(value['tv_player']['playback'])
                self.assertIsNone(value['evidence']['server_switch_state'])
                self.assertIsNone(value['tv_player']['switch_client_fallback'])

    def test_minus5402_remains_visible_with_unknown_switch_and_misleading_payload(self):
        reject = capacity_frame()[1]
        reject['code'] = -5402
        for switch in ({'code': 0}, {'code': 0, 'mode': 99}, {'code': True, 'mode': 0}):
            with self.subTest(switch=switch):
                value = run(FakeClient(responses(transcode_switch=(200, switch),
                                                 offline_video_path=(200, reject))))
                self.assertEqual(value['reason'], 'video_capability_query_unsupported')
                self.assertEqual(value['steps']['capacity']['native_code'], -5402)
                self.assertEqual(value['status'], 'partial')
                self.assertIsNone(value['definitions'])
                self.assertIsNone(value['tv_player']['playback'])
                self.assertTrue(value['tv_player']['capacity_error_branch']['conditional'])

    def test_all_present_invalid_codes_fail_in_every_getter(self):
        invalid = (True, False, '0', '-5402', None, [], {}, 2 ** 53, -(2 ** 53))
        for operation in ('video_capacity', 'transcode_switch', 'offline_video_path'):
            for code in invalid:
                with self.subTest(operation=operation, code=code):
                    frame = responses()[operation][1]
                    frame['code'] = code
                    value = run(FakeClient(responses(**{operation: (200, frame)})))
                    key = {'video_capacity': 'capability', 'transcode_switch': 'switch',
                           'offline_video_path': 'capacity'}[operation]
                    self.assertFalse(value['steps'][key]['ok'])
                    self.assertEqual(value['steps'][key]['reason'], 'native_code_invalid')
                    self.assertIsNone(value['steps'][key]['native_code'])
                    self.assertNotEqual(value['tv_player']['playback'], 'hls')

    def test_code_zero_safe_integer_bounds_and_codeless_contracts(self):
        for operation, key in (('video_capacity', 'capability'), ('transcode_switch', 'switch'),
                               ('offline_video_path', 'capacity')):
            with self.subTest(operation=operation):
                self.assertTrue(run(FakeClient(responses()))['steps'][key]['ok'])
                frame = responses()[operation][1]
                frame.pop('code')
                value = run(FakeClient(responses(**{operation: (200, frame)})))
                self.assertEqual(value['steps'][key]['ok'], operation != 'transcode_switch')
                for boundary in (probe.MAX_SAFE_INTEGER, -probe.MAX_SAFE_INTEGER):
                    frame['code'] = boundary
                    value = run(FakeClient(responses(**{operation: (200, frame)})))
                    self.assertFalse(value['steps'][key]['ok'])
                    self.assertEqual(value['steps'][key]['native_code'], boundary)
                    self.assertEqual(value['steps'][key]['reason'], 'native_request_rejected')
        bad = run(FakeClient(responses(offline_video_path=(200, {'status': 'ready'}))))
        self.assertEqual(bad['steps']['capacity']['reason'], 'native_code_unavailable')

    def test_every_free_text_field_withholds_private_text(self):
        for field in ('name', 'profile', 'offlineprofile', 'codec_name', 'codec_type', 'status'):
            for private in (SECRET, PRIVATE_PATH):
                with self.subTest(field=field, private=private):
                    frame = capacity_frame()[1]
                    if field == 'status':
                        frame[field] = private
                    else:
                        frame['capabilities']['video'][0][field] = private
                    value = run(FakeClient(responses(offline_video_path=(200, frame))))
                    self.assertNotIn(private, json.dumps(value))
                    if field == 'profile':
                        self.assertEqual(value['definitions']['labels'], [])
                        self.assertIsNone(value['tv_player']['playback'])
                    if field == 'status':
                        self.assertEqual(value['capacity']['status'], 'other')

    def test_invalid_or_missing_first_profile_cannot_use_720p_or_second_row(self):
        for first in (None, 1, {}, {'profile': None}, {'profile': 'secret'},
                      {'profile': ['high']}, {'profile': 'high,'}, {'profile': ' high'},
                      {'profile': 'high,high'}):
            with self.subTest(first=first):
                frame = shape_frame(video=[first, {'profile': 'high,mid,low'}],
                                    video_720p='/private/' + SECRET)[1]
                value = run(FakeClient(responses(offline_video_path=(200, frame))))
                self.assertEqual(len(value['capacity']['video']), 2)
                self.assertEqual(value['definitions']['labels'], [])
                self.assertEqual(value['reason'], 'profile_metadata_unknown')
                self.assertIsNone(value['tv_player']['playback'])

    def test_revocation_after_any_getter_stops_immediately_and_clears_all_steps(self):
        operations = ('video_capacity', 'transcode_switch', 'offline_video_path')
        for target in operations:
            with self.subTest(target=target):
                allowed = {'value': True}
                client = FakeClient(responses())
                original = client.request

                def request(operation, *args, **kwargs):
                    result = original(operation, *args, **kwargs)
                    if operation == target:
                        allowed['value'] = False
                    return result

                client.request = request
                value = probe.collect_transcode_probe(PATH, client=client,
                    authorizer=lambda *args: allowed['value'])
                self.assert_denied(value)
                self.assertEqual([call[0] for call in client.calls],
                                 list(operations[:operations.index(target) + 1]))

    def test_revocation_during_projection_stops_before_next_request(self):
        allowed = {'value': True}
        original = probe.project_capabilities

        def project(frame):
            allowed['value'] = False
            return original(frame)

        client = FakeClient(responses())
        with mock.patch.object(probe, 'project_capabilities', side_effect=project):
            value = probe.collect_transcode_probe(PATH, client=client,
                authorizer=lambda *args: allowed['value'])
        self.assert_denied(value)
        self.assertEqual([call[0] for call in client.calls], ['video_capacity'])

    def test_final_publication_guard_discards_already_derived_results(self):
        allowed = {'value': True}
        original = probe._tv_player

        def derive(*args, **kwargs):
            value = original(*args, **kwargs)
            allowed['value'] = False
            return value

        client = FakeClient(responses())
        with mock.patch.object(probe, '_tv_player', side_effect=derive):
            value = probe.collect_transcode_probe(PATH, client=client,
                authorizer=lambda *args: allowed['value'])
        self.assert_denied(value)
        self.assertEqual(len(client.calls), 3)

    def test_one_observed_denial_cannot_revive_with_a_later_true(self):
        allowed = {'value': True}
        client = FakeClient(responses())
        original = client.request

        def request(operation, *args, **kwargs):
            value = original(operation, *args, **kwargs)
            allowed['value'] = False
            self.assertFalse(kwargs['authorizer'](operation, {}, {}))
            allowed['value'] = True
            return value

        client.request = request
        value = probe.collect_transcode_probe(PATH, client=client,
            authorizer=lambda *args: allowed['value'])
        self.assert_denied(value)
        self.assertEqual(len(client.calls), 1)


class TransportValidationTests(unittest.TestCase):
    """The transport re-validates every value; the route helper is not enough."""

    def test_offline_video_path_accepts_only_admin_media(self):
        good = {'video_capability': 1, 'path': PATH, 'offset': 0,
                'sharpness': 'raw', 'terminal': probe.TERMINAL}
        self.assertEqual(_validate('offline_video_path', good, {})[0], good)
        for key, value in (('path', '/drives/sda/Users/public/x.mkv'),
                           ('path', '/private/x.mkv'),
                           ('video_capability', 2),
                           ('offset', -1),
                           ('sharpness', 'ultra'),
                           ('terminal', 'bad terminal'),
                           ('extra', 1)):
            with self.subTest(key=key):
                body = dict(good)
                body[key] = value
                with self.assertRaises(BusinessTransportError):
                    _validate('offline_video_path', body, {})

    def test_capability_and_switch_operations_are_read_only_and_unparameterised(self):
        from vendor_business_transport import OPERATIONS
        for operation in ('video_capacity', 'transcode_switch', 'offline_video_path'):
            with self.subTest(operation=operation):
                self.assertIs(OPERATIONS[operation][3], False)
        self.assertEqual(_validate('video_capacity', {}, {})[0], {'capacity_detail': 0})
        with self.assertRaises(BusinessTransportError):
            _validate('video_capacity', {'capacity_detail': 1}, {})
        with self.assertRaises(BusinessTransportError):
            _validate('transcode_switch', {'mode': 1}, {})


if __name__ == '__main__':
    unittest.main()
