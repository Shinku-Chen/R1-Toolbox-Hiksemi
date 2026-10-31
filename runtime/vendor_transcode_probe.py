"""显式固定诊断：原厂转码能力与单个文件的清晰度档位证据。

电视端播放器在开播前依次查询三件事：能力位（`cap_transcode_video`、
`cap_tr_video_switch_v1`）、转码开关（`get_trvideo_switch`）和该文件的离线
档位（`get_offlinevideo_path` 的 `profile` / `video_720p`）。各条门槛的效果
不同：能力位不为 1 或 `videoSwitchOpen` 为 false 时走原画并隐藏清晰度入口；
开关查询失败时 `AbilityConfig$2.onFailure` 回落 `videoSwitchOpen = true`、
客户端继续；`cap_tr_video_switch_v1 != 1` 时客户端根本不查询开关；档位查询
失败则回落原画并提示。所以只看界面无法判断卡在哪一步。

本模块固定发三个原厂 getter；TV 在开关能力为 0 时省略的查询单独标注。
它不显式发送 `play_hls_video`／`stop_hls_video`，也不发送 `set_trvideo_switch`
或任何设置写入。但固定 GET 不等于只读：静态审计（emhistor `402964…`）显示
`get_offlinevideo_path` 在返回档位前会走 `tr_video_md5`→`wr_md5_generate`，
没有有效 `user.md5` 缓存时会实际 `fopen`／`fread` 媒体文件（最多 64×1 MiB）
并 `setxattr(user.md5)`；因此调用可能读取媒体内容、写元数据并唤盘。其它
getter 的副作用仍未完整审计。路径只作为原厂查询的字符串参数，宿主本身不
打开、不 stat、不解析该文件，不构成文件访问授权。

缺测一律保持 `null`／`unknown` 并给出固定原因：能力结构缺失或畸形不等于
「明确不支持」，开关查询失败不等于「已开启」，`status` 缺失不等于「未就绪」。
"""

from datetime import datetime, timezone
import hashlib

from vendor_business_transport import (BusinessTransportError, VendorBusinessClient,
                                       REASONS as TRANSPORT_REASONS, media_path,
                                       public_trace)


OWNER_ID = 100000
# 固定查询参数；不显式发送会话操作，原厂内部副作用见模块说明。
OFFSET = 0
SHARPNESS = 'raw'
# 电视端用本机 UUID 作 terminal；诊断只做状态查询，使用固定的非会话标识。
TERMINAL = '00000000-0000-0000-0000-000000000000'
CAPABILITY_NAMES = ('cap_transcode_video', 'cap_tr_video_switch_v1')
PROFILE_CODES = frozenset(('raw', 'high', 'mid', 'low', 'origin'))
PROFILE_LABELS = {'raw': '原画', 'high': '高清', 'mid': '清晰', 'low': '流畅'}
ORIGINAL_LABEL = '原画'
MID_LABEL = '清晰'
MAX_STREAMS = 32
MAX_SAFE_INTEGER = 2 ** 53 - 1
_NOTES = (
    'fixed_vendor_getters_only',
    'no_explicit_session_or_setting_request',
    'capacity_getter_may_hash_media_content',
    'capacity_getter_may_write_user_md5_xattr',
    'capacity_getter_may_wake_disks',
    'other_getter_effects_not_fully_audited',
    'path_string_only_no_local_file_access',
    'transcode_paths_not_returned',
    'device_model_whitelist_is_client_side',
    'definition_labels_mirror_reviewed_tv_dex',
    'missing_measurement_is_unknown_not_unsupported',
    'tv_expectation_is_static_dex_inference_not_observed',
)
# 原厂 capacity getter 对 video_capability != 0 的固定拒绝码（静态审计 0x24bbb9）。
CAPACITY_REJECT_REASONS = {-5402: 'video_capability_query_unsupported'}
_STEP_REASONS = frozenset(TRANSPORT_REASONS) | frozenset((
    'unexpected_response', 'native_request_rejected', 'native_code_unavailable',
    'native_code_invalid',
))


def _code(value):
    return value if type(value) is int and abs(value) <= MAX_SAFE_INTEGER else None


def _integer(value, low=0, high=MAX_SAFE_INTEGER):
    return value if type(value) is int and low <= value <= high else None


def _profile(value):
    """只保留审计档位组合；缺失、非法与明确空串不能合并为同一种 fallback。"""
    if value is None:
        return None, 'missing'
    if value == '' and type(value) is str:
        return '', 'empty'
    if type(value) is not str or len(value) > 64:
        return None, 'invalid'
    codes = value.split(',')
    if (len(codes) > len(PROFILE_CODES) or len(set(codes)) != len(codes)
            or any(code not in PROFILE_CODES for code in codes)):
        return None, 'invalid'
    return ','.join(codes), 'valid'


def _present(value, limit=4096):
    """只判断原厂是否给了该字段，不复制路径本身。"""
    return type(value) is str and 0 < len(value) <= limit


def _fingerprint(path):
    return hashlib.sha256(path.encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def validate_path(value):
    """HTTP 层前置校验：规则与传输层相同，失败抛 ValueError，不碰网络。

    只校验路径**形式**（绝对、`/drives/<disk>/Users/admin/` 下、无 `.`／`..`／空段等），
    纯字符串判断无法确认末段是文件还是目录，因此不宣称「不接受目录」。
    """
    return media_path(value)


def project_capabilities(frame):
    """只投影两个已审计能力位；未识别条目只计数，不复制名称或值。

    `structure` 区分结构本身缺失与条目未知：`missing`／`empty` 时不能推断
    设备不支持转码；只有明确 `capacity_detail == 0` 的条目才算「已报告关闭」。
    """
    if not isinstance(frame, dict):
        raise BusinessTransportError('invalid_json')
    rows = frame.get('capacity_list')
    values = {name: None for name in CAPABILITY_NAMES}
    observed = {name: [] for name in CAPABILITY_NAMES}
    unknown = malformed = 0
    if type(rows) is list:
        for row in rows:
            if not isinstance(row, dict):
                malformed += 1
                continue
            name, detail = row.get('capacity_name'), row.get('capacity_detail')
            if type(name) is not str or name not in CAPABILITY_NAMES:
                unknown += 1
                continue
            observed[name].append(_integer(detail, 0, 1))
    # 同一能力名出现多次且取值矛盾时不能由行序决定结论：固定判为 unknown。
    conflicts = 0
    for name in CAPABILITY_NAMES:
        seen = observed[name]
        if not seen:
            continue
        if len(set(seen)) > 1:
            conflicts += 1
            continue
        values[name] = seen[0]
    duplicates = sum(max(0, len(observed[name]) - 1) for name in CAPABILITY_NAMES)
    states = {name: {1: 'enabled', 0: 'disabled', None: 'unknown'}[values[name]]
              for name in CAPABILITY_NAMES}
    if not isinstance(rows, list):
        structure = 'missing'
    elif not rows:
        structure = 'empty'
    else:
        structure = 'ok'
    return {'code': _code(frame.get('code')),
            'structure': structure,
            'entries': len(rows) if type(rows) is list else None,
            'values': values,
            'states': states,
            'unknown_entries': unknown,
            'malformed_entries': malformed,
            'duplicate_entries': duplicates,
            'conflicting_entries': conflicts}


def project_switch(frame):
    """`get_trvideo_switch` 只返回 code 与 mode；mode 0 关闭、1 开启。"""
    if not isinstance(frame, dict):
        raise BusinessTransportError('invalid_json')
    mode = _integer(frame.get('mode'), 0, 1)
    return {'code': _code(frame.get('code')),
            'mode': mode,
            'open': None if mode is None else mode == 1}


def _rows(capabilities, key):
    rows = capabilities.get(key) if isinstance(capabilities, dict) else None
    return rows if type(rows) is list else []


def project_offline_path(frame):
    """投影档位与流信息；文件路径、hash_path、目录与原始错误文本一律不返回。"""
    if not isinstance(frame, dict):
        raise BusinessTransportError('invalid_json')
    raw_capabilities = frame.get('capabilities')
    if isinstance(raw_capabilities, dict):
        capabilities_state = 'object'
    elif raw_capabilities is None:
        capabilities_state = 'null'
    else:
        capabilities_state = 'invalid'
    capabilities = raw_capabilities if capabilities_state == 'object' else {}
    raw_video = capabilities.get('video') if capabilities else None
    if isinstance(raw_video, list):
        video_state = 'list' if raw_video else 'empty'
    elif raw_video is None:
        video_state = 'null'
    else:
        video_state = 'invalid'
    video = []
    for row in _rows(capabilities, 'video')[:MAX_STREAMS]:
        if not isinstance(row, dict):
            # 保留原数组位置：坏首行不能被后面的好行替代。
            video.append({'row_state': 'invalid', 'profile': None,
                          'profile_state': 'invalid', 'offlineprofile': None,
                          'offlineprofile_state': 'invalid'})
            continue
        profile, profile_state = _profile(row.get('profile'))
        offline, offline_state = _profile(row.get('offlineprofile'))
        # name／codec_name 是非必需自由文本，不返回；codec_type 只接受视频枚举。
        video.append({'row_state': 'object', 'profile': profile,
                      'profile_state': profile_state,
                      'offlineprofile': offline, 'offlineprofile_state': offline_state,
                      'width': _integer(row.get('width'), 0, 65535),
                      'height': _integer(row.get('height'), 0, 65535),
                      'bit_rate': _integer(row.get('bit_rate')),
                      'frame_rate': _integer(row.get('frame_rate'), 0, 1000),
                      'codec_type': 'video' if row.get('codec_type') == 'video' else None})
    raw_status = frame.get('status')
    status = ('ready' if raw_status == 'ready' else 'other') if type(raw_status) is str else None
    structure = ('ok' if (capabilities_state != 'null' or _present(frame.get('video_720p'))
                          or status is not None) else 'missing')
    profile_state = 'absent'
    if video_state == 'list':
        profile_state = video[0]['profile_state']
    return {'code': _code(frame.get('code')),
            'structure': structure,
            'capabilities_state': capabilities_state,
            'video_state': video_state,
            'first_profile_state': profile_state,
            'status': status,
            # 原厂 `status` 缺失时保留未知，不据此断言未就绪。
            'ready': None if status is None else status == 'ready',
            'hash_path_present': _present(capabilities.get('hash_path')),
            'video_720p_present': _present(frame.get('video_720p')),
            'video': video,
            'audio_streams': (len(capabilities['audio'])
                              if type(capabilities.get('audio')) is list else None),
            'subtitle_streams': (len(capabilities['subtitle'])
                                 if type(capabilities.get('subtitle')) is list else None)}


def derive_definitions(capacity):
    """按已复核电视端 DEX（classes.dex `fd7af59c…`，onCapacityResult）推导档位。

    结构分支必须分开保留，不能把「capabilities 为 null」与「capabilities 是对象
    但没有 video 行」合并：

    - `capabilities == null`：`0x006e` 直接跳到 `0x016c`，`0x0170` 因对象为空而
      不跳，落到 `0x0174` 的 `video_720p` 检查；有值才在 `0x0184` 追加一档「清晰」。
    - `capabilities` 是对象但 `video` 缺失／null／空数组：`0x007a`／`0x008e` 跳到
      `0x016c`，`0x0170` 因对象非空跳到 `0x0196`，**不会再追加**。
    - `capabilities.video[0]` 存在：`profile` 非空且不含 `origin` 时按逗号拆分；
      随后（拆分分支 `0x0124` 结束后仍落入 `0x0126`，不是二选一）在
      `0x013e` 判断 `contains("mid")`，不含且 `video_720p` 非空才追加「清晰」。
      首个 `profile` 为空则走自己的 fallback：同样按 `video_720p` 决定是否追加。

    未识别档位码不返回原文；非法首行保持未知，不拿下一行代替。
    """
    labels = [ORIGINAL_LABEL]
    profiles = []
    source = 'none'
    capabilities_state = capacity.get('capabilities_state') if isinstance(capacity, dict) else None
    video_state = capacity.get('video_state') if isinstance(capacity, dict) else None
    available_720p = bool(capacity.get('video_720p_present')) if isinstance(capacity, dict) else False
    if (capabilities_state == 'invalid' or video_state == 'invalid'
            or (video_state == 'list' and capacity.get('first_profile_state')
                not in ('valid', 'empty'))):
        return {'source': 'unknown', 'profiles': [], 'labels': [],
                'switchable': None, 'origin_marker_present': None,
                'mid_marker_present': None, 'unmapped_profile_count': 1,
                'capabilities_state': capabilities_state, 'video_state': video_state,
                'reason': 'profile_metadata_unknown'}
    profile = None
    if capabilities_state == 'object' and video_state == 'list':
        first = (capacity.get('video') or [None])[0]
        profile = first.get('profile') if isinstance(first, dict) else None
    has_mid = bool(profile) and 'mid' in profile
    if profile and 'origin' not in profile:
        source = 'profile'
        for token in profile.split(','):
            token = token.strip()
            if not token:
                continue
            profiles.append(token)
            # origin 仅是分支标记，走此分支的 token 必然属于固定标签表。
            labels.append(PROFILE_LABELS[token])
    if capabilities_state == 'null':
        # 原厂 capabilities 为空对象时的独立 fallback，与 video 行无关。
        append_mid = available_720p
    elif capabilities_state == 'object' and video_state == 'list':
        # 原厂在拆分分支之后继续检查 mid，因此不能写成 elif。
        append_mid = not has_mid and available_720p
    else:
        append_mid = False
    if append_mid and MID_LABEL not in labels:
        source = source if source != 'none' else 'video_720p'
        if 'mid' not in profiles:
            profiles.append('mid')
        labels.append(MID_LABEL)
    return {'source': source,
            'profiles': profiles,
            'labels': labels,
            'switchable': len(labels) > 1,
            'origin_marker_present': bool(profile) and 'origin' in profile,
            'mid_marker_present': has_mid,
            'unmapped_profile_count': 0,
            'reason': None,
            'capabilities_state': capabilities_state,
            'video_state': video_state}


def _tv_player(capability, switch, capacity, definitions, switch_step=None):
    """电视端在当前证据下会怎么表现；缺测保持 `null` 而不是猜成「原画」。

    机型白名单（`R1`／`R1X`／`SR1` 等）由客户端本地判断，这里只给出服务端可确认
    的部分，因此固定标记 `requires_tv_model_whitelist`。
    """
    states = (capability or {}).get('states') or {}
    result = {'definition_menu_visible': None, 'playback': None,
              'labels': [], 'reason': None, 'switch_state': None,
              'switch_client_fallback': None,
              'playback_observed': False,
              'requires_tv_model_whitelist': True}
    rejected = isinstance(capacity, dict) and capacity.get('structure') == 'rejected'
    reject_code = capacity.get('code') if rejected else None
    if rejected:
        # 只有 -5402 有审计过的不支持分支；其它错误不能继承其能力/播放结论。
        result['capacity_error_branch'] = {
            'native_code': reject_code, 'clears_definitions': True if reject_code == -5402 else None,
            'supports_switch': False if reject_code == -5402 else None,
            'source': 'static_dex_inference' if reject_code == -5402 else 'native_rejection_only',
            'observed': False, 'conditional': True}
    if not capability or states.get('cap_transcode_video') not in ('enabled', 'disabled'):
        result['reason'] = 'capability_unknown'
        return result
    if states.get('cap_transcode_video') == 'disabled':
        result.update(definition_menu_visible=False, playback='original',
                      switch_state='not_requested_capability_disabled',
                      reason='device_reports_no_video_transcode_capability')
        return result
    switch_unknown = False
    if states.get('cap_tr_video_switch_v1') == 'enabled':
        if switch is None or switch.get('open') is None:
            fallback = (switch_step or {}).get('client_fallback_basis')
            if fallback in ('transport_failure', 'nonzero_code_with_integer_mode'):
                # onFailure 置 true；onSuccess 的非零 code 且 mode 可解析也置 true。
                # code=0 的非法/缺失 mode 则直接返回或 catch，保留旧状态，不能套用。
                result.update(switch_state='fallback_open_unverified',
                              switch_client_fallback='open_on_failure' if fallback == 'transport_failure'
                              else 'open_on_native_rejection')
            else:
                result['switch_state'] = 'unknown_previous_client_state'
                switch_unknown = True
        elif switch.get('open') is False:
            result.update(definition_menu_visible=False, playback='original',
                          switch_state='closed', reason='transcode_switch_closed')
            return result
        else:
            result['switch_state'] = 'open'
    else:
        # 能力位未报告可切换开关时电视端不查询该项，按客户端默认继续。
        result['switch_state'] = ('not_requested_capability_disabled'
                                  if states.get('cap_tr_video_switch_v1') == 'disabled'
                                  else 'not_requested_capability_unknown')
        switch_unknown = states.get('cap_tr_video_switch_v1') != 'disabled'
    if capacity is None:
        result['reason'] = 'capacity_query_failed'
        return result
    if rejected:
        result['reason'] = CAPACITY_REJECT_REASONS.get(reject_code, 'capacity_query_rejected')
        if reject_code == -5402 and not switch_unknown:
            result.update(definition_menu_visible=True, playback='original')
            result['capacity_error_branch']['conditional'] = False
        return result
    if switch_unknown:
        result['reason'] = 'switch_state_unknown'
        return result
    if definitions is not None and definitions.get('reason') is not None:
        result['reason'] = definitions['reason']
        return result
    if capacity.get('ready') is None:
        result['reason'] = 'capacity_status_unknown'
        return result
    if capacity.get('ready') is False:
        # `0x0196` 在未就绪时跳到 `0x0222`：原始或离线 720p 播放，不建 HLS。
        result.update(definition_menu_visible=True, playback='original', reason='original_fallback')
        return result
    # 就绪分支：`0x01a6` 在 capabilities 为 null 时直接到 `0x0310` 返回，
    # 绕过 `0x01e0` 的 isPlayM3U8 与 `0x0212` 的 m3u8 构建，因此播放方式未知。
    structure = capacity.get('capabilities_state')
    if structure == 'null':
        result['reason'] = 'capacity_structure_missing'
        return result
    if structure != 'object':
        result['reason'] = 'capacity_structure_invalid'
        return result
    switchable = bool(definitions and definitions.get('switchable'))
    # 档位数量不是 HLS 的证据；是否建 m3u8 只由就绪与 capabilities 结构决定。
    result.update(definition_menu_visible=True, playback='hls',
                  labels=list(definitions['labels']) if definitions else [],
                  reason='definitions_available' if switchable else 'hls_without_alternatives')
    return result


def _reason(reason):
    return reason if reason in _STEP_REASONS else 'transport_failed'


def _step(client, operation, parameters, authorizer, usable):
    """单次固定 getter；失败只落在本步，不中断后续步骤。"""
    observation = {}

    def observed(value):
        # 原厂帧与异常均不属于诊断；只保留生产检查实际填入的固定白名单。
        value['transport'] = public_trace(observation)
        return value

    try:
        status_code, frame = client.request(operation, parameters, {},
                                            authorizer=authorizer, observer=observation)
    except BusinessTransportError as error:
        return observed({'ok': False, 'reason': _reason(error.reason),
                'http_status': error.http_status, 'frame': None,
                'request_sent': error.request_sent is True,
                'client_fallback_basis': ('transport_failure' if error.reason in
                                          ('http_error', 'timeout', 'transport_failed') else None)})
    except Exception:
        return observed({'ok': False, 'reason': 'transport_failed', 'http_status': None, 'frame': None})
    if status_code != 200 or not isinstance(frame, dict):
        return observed({'ok': False, 'reason': 'unexpected_response',
                'http_status': _integer(status_code, 100, 599), 'frame': None})
    code = _code(frame.get('code'))
    failed = {'ok': False, 'http_status': status_code, 'frame': None, 'code': code,
              'client_fallback_basis': None}
    if 'code' in frame and code is None:
        return observed(dict(failed, reason='native_code_invalid'))
    if 'code' not in frame:
        # 只有这两个已审计 getter 有无 code 的正常对象；switch.onSuccess 要求 code。
        codeless = (operation == 'video_capacity' and _usable_capabilities(frame)
                    or operation == 'offline_video_path' and
                    (isinstance(frame.get('capabilities'), dict) or _present(frame.get('video_720p'))))
        if not codeless or not usable(frame):
            return observed(dict(failed, reason='native_code_unavailable'))
    elif code != 0:
        # 原厂通用 wrapper 对错误 envelope 的解析格式敏感。仅有非零 code 不足以
        # 证明 onFailure；但 mode 为严格整数时，onSuccess 的 code!=0 分支也置 true。
        if operation == 'transcode_switch' and _code(frame.get('mode')) is not None:
            failed['client_fallback_basis'] = 'nonzero_code_with_integer_mode'
        return observed(dict(failed, reason='native_request_rejected'))
    elif not usable(frame):
        return observed(dict(failed, reason='unexpected_response'))
    return observed({'ok': True, 'reason': None, 'http_status': status_code,
            'frame': frame, 'code': code, 'client_fallback_basis': None})


def _public_step(step):
    # HTTP 200 不等于业务成功；只公开安全整数的原厂 code，不返回 msg／路径／原文。
    return {'ok': step['ok'], 'reason': step['reason'],
            'http_status': step['http_status'], 'native_code': step.get('code'),
            'request_sent': (step['request_sent'] if 'request_sent' in step
                             else (step.get('transport') or {}).get('request_sent')),
            'transport': public_trace(step.get('transport'))}


def project_capacity_rejection(code):
    """原厂业务拒绝时的最小结构；不伪造档位流，也不丢拒绝码。"""
    return {'code': code, 'structure': 'rejected',
            'capabilities_state': None, 'video_state': None,
            'first_profile_state': None, 'status': None, 'ready': None,
            'hash_path_present': None, 'video_720p_present': None,
            'video': [], 'audio_streams': None, 'subtitle_streams': None}


def _usable_capabilities(frame):
    return type(frame.get('capacity_list')) is list


def _usable_switch(frame):
    return _integer(frame.get('mode'), 0, 1) is not None


def _usable_capacity(frame):
    return (isinstance(frame.get('capabilities'), dict)
            or _present(frame.get('video_720p')) or type(frame.get('status')) is str)


def _denied_result():
    """撤权只返回固定拒绝状态，不残留前面步骤、路径摘要或业务证据。"""
    return {'schema_version': 1, 'available': False, 'status': 'denied',
            'reason': 'authorization_denied', 'steps': {}, 'request': None,
            'scope': None, 'capability': None, 'switch': None, 'capacity': None,
            'definitions': None, 'tv_player': None, 'evidence': None}


def collect_listener_preflight(*, root='/', authorizer=None, client=None):
    """只复用生产监听门：不加载凭据、证书，不连接原厂或检查媒体文件。

    诊断仅证明该次有界 proc 采样的结果，不能替代随后运输时的四次复核。
    任一撤权均丢弃本轮观察，不能在后续授权恢复时发布旧结果。
    """
    result = {'schema_version': 1, 'kind': 'emhistor_listener',
              'available': False, 'status': 'unavailable', 'reason': None,
              'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'diagnostics': None, 'proc_metadata_only': True,
              'native_request_performed': False, 'credentials_read': False,
              'certificate_read': False, 'media_accessed': False}
    revoked = False

    def denied():
        return {'schema_version': 1, 'kind': 'emhistor_listener',
                'available': False, 'status': 'denied',
                'reason': 'authorization_denied', 'diagnostics': None}

    def live(*_fixed_context):
        nonlocal revoked
        if revoked:
            return False
        try:
            allowed = callable(authorizer) and authorizer('probe_listener', {}, {}) is True
        except Exception:
            allowed = False
        revoked = not allowed
        return allowed

    if not live():
        return denied()
    worker = client if client is not None else VendorBusinessClient(root=root)
    observation = {}
    try:
        gate = worker.probe_listener(authorizer=live, observer=observation)
        # 实际门仍由 transport._identity 执行，观察数据不作为放行证明。
        if type(gate) is dict and gate.get('available') is True and gate.get('status') == 'verified':
            result.update(available=True, status='verified')
        else:
            result['reason'] = 'identity_unverified'
    except BusinessTransportError as error:
        if error.reason == 'authorization_denied':
            return denied()
        result['reason'] = _reason(error.reason)
    except Exception:
        result['reason'] = 'transport_failed'
    if not live():
        return denied()
    result['diagnostics'] = public_trace(observation)
    if not live():
        return denied()
    return result


def collect_transcode_probe(path, *, root='/', authorizer=None, client=None):
    """显式诊断入口：路径已校验，authorizer 必须是开发白名单实时判定。

    三次查询共用同一个 `VendorBusinessClient`（或注入的同签名假客户端），
    各自独立成败。原厂会话、叶证书、监听身份与撤权复验全部沿用传输层。
    """
    result = {'schema_version': 1, 'available': False, 'status': 'unknown',
              'reason': None,
              'sampled_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'scope': {'user_id': OWNER_ID, 'path_scope': 'drives_users_admin'},
              'request': {'offset': OFFSET, 'sharpness': SHARPNESS, 'terminal': TERMINAL,
                          'path_sha256_prefix': None},
              'steps': {}, 'capability': None, 'switch': None, 'capacity': None,
              'definitions': None, 'tv_player': None,
              'explicit_getter_only': True,
              'may_read_media_content': True,
              'may_write_media_xattrs': True,
              'may_wake_disks': True,
              'getter_effects_fully_audited': False,
              'playback_observed': False,
              'evidence': {'server_capability': None, 'server_switch_state': None,
                           'client_switch_fallback': None,
                           'playback_source': 'tv_dex_branch_inference'},
              'notes': list(_NOTES)}
    try:
        checked = media_path(path)
    except (BusinessTransportError, ValueError):
        result['reason'] = 'invalid_parameters'
        return result
    if not callable(authorizer):
        return _denied_result()

    revoked = False

    def live(operation, parameters, body):
        # 本轮一旦观察到撤权就不能复活；参数副本也不能被回调修改为其它请求。
        nonlocal revoked
        if revoked:
            return False
        try:
            allowed = authorizer(operation, dict(parameters), dict(body)) is True
        except Exception:
            allowed = False
        revoked = not allowed
        return allowed

    def query(operation, parameters, usable):
        nonlocal revoked
        if not live(operation, parameters, {}):
            return None
        step = _step(worker, operation, parameters, live, usable)
        if step['reason'] == 'authorization_denied' or not live(operation, parameters, {}):
            revoked = True
            return None
        return step

    result['request']['path_sha256_prefix'] = _fingerprint(checked)
    worker = client if client is not None else VendorBusinessClient(root=root)

    steps = {}
    capability_step = query('video_capacity', {}, _usable_capabilities)
    if capability_step is None:
        return _denied_result()
    steps['capability'] = _public_step(capability_step)
    if capability_step['ok']:
        try:
            result['capability'] = project_capabilities(capability_step['frame'])
        except BusinessTransportError:
            steps['capability'].update(ok=False, reason='invalid_json')

    switch_step = query('transcode_switch', {}, _usable_switch)
    if switch_step is None:
        return _denied_result()
    steps['switch'] = _public_step(switch_step)
    if switch_step['ok']:
        try:
            result['switch'] = project_switch(switch_step['frame'])
        except BusinessTransportError:
            steps['switch'].update(ok=False, reason='invalid_json')

    capacity_parameters = {
        'video_capability': 1, 'path': checked, 'offset': OFFSET,
        'sharpness': SHARPNESS, 'terminal': TERMINAL}
    capacity_step = query('offline_video_path', capacity_parameters, _usable_capacity)
    if capacity_step is None:
        return _denied_result()
    steps['capacity'] = _public_step(capacity_step)
    if capacity_step['ok']:
        try:
            result['capacity'] = project_offline_path(capacity_step['frame'])
            result['definitions'] = derive_definitions(result['capacity'])
        except BusinessTransportError:
            steps['capacity'].update(ok=False, reason='invalid_json')
    elif steps['capacity'].get('native_code') is not None and steps['capacity']['native_code'] != 0:
        # 业务拒绝：保留拒绝码与最小结构，别把「有 HTTP 响应」当成档位可用。
        result['capacity'] = project_capacity_rejection(steps['capacity']['native_code'])
        result['definitions'] = None

    result['steps'] = steps
    switch_capability = ((result['capability'] or {}).get('states') or {}).get(
        'cap_tr_video_switch_v1')
    # 电视端只在 cap_tr_video_switch_v1==1 时请求开关；诊断多发的 getter 如实标注。
    result['request']['switch_query_in_tv_flow'] = (
        switch_capability == 'enabled' if switch_capability in ('enabled', 'disabled') else None)
    result['request']['extra_getters_not_in_tv_flow'] = (
        [] if switch_capability == 'enabled' else ['transcode_switch']
        if switch_capability == 'disabled' else None)
    result['tv_player'] = _tv_player(result['capability'], result['switch'],
                                     result['capacity'], result['definitions'], switch_step)
    result['evidence'] = {
        'server_capability': ((result['capability'] or {}).get('states') or {}).get(
            'cap_transcode_video'),
        'server_switch_state': ('open' if (result['switch'] or {}).get('open') is True
                                else 'closed' if (result['switch'] or {}).get('open') is False
                                else None),
        'client_switch_branch': result['tv_player']['switch_state'],
        'client_switch_fallback': result['tv_player']['switch_client_fallback'],
        'client_switch_fallback_basis': switch_step.get('client_fallback_basis'),
        'playback_source': 'tv_dex_branch_inference',
        'playback_observed': False,
    }
    result['playback_observed'] = False
    result['reason'] = result['tv_player']['reason']
    states = (result['capability'] or {}).get('states') or {}
    if states.get('cap_transcode_video') not in ('enabled', 'disabled'):
        result['status'] = 'unknown'
    elif result['tv_player']['playback'] is None:
        result['status'] = 'partial'
        result['available'] = True
    else:
        result['status'] = 'ok'
        result['available'] = True
    if not live('offline_video_path', capacity_parameters, {}):
        return _denied_result()
    return result
