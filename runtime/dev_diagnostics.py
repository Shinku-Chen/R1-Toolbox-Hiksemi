"""固定的只读诊断目录与聚合器，不访问文件、网络或外部命令。

HTTP 层负责授权、同源检查和来源锁；此处仅依次调用被选中的采集器。
采集器负责限制与投影数据，本模块保留完整证据，不裁剪或补造读数。
"""

from datetime import datetime, timezone


CHECKS = ('device', 'storage', 'business', 'fan')
# Identity serials require an explicit selection; the default snapshot remains
# the original four checks even as the discovery catalog grows.
ALLOWED_CHECKS = CHECKS + ('storage_identity', 'file_access_readiness', 'docker_project_readiness', 'disk_activity')
_CHECK_DETAILS = {
    'device': ('设备信息', '只读现有监控中的设备身份缓存。'),
    'storage': ('存储信息', '固定名称查询与现有身份缓存，不运行 SMART 或唤醒磁盘。'),
    'business': ('业务状态', '采集业务只读证据，并保留不可用类别的原因。'),
    'fan': ('风扇现场', '采集风扇预检、进程和监督端现场，不重试接管。'),
    'storage_identity': ('硬盘完整身份', '仅显式选择时交叉核对现有缓存与原厂列表，返回完整序列号；不运行 SMART 或唤盘。'),
    'file_access_readiness': ('文件访问就绪证据', '只读可信根规划与缺失安全证明，不读取用户文件、凭据或 SMART。'),
    'docker_project_readiness': ('固定项目就绪证据', '显式读取已核验本机 Docker 元数据和登记绑定，不部署、启停或读取 Compose 原文。'),
    'disk_activity': ('磁盘活动现场', '仅显式读取 proc/sys 磁盘和进程 I/O 计数；再次采样比较增量，不执行 SMART 或休眠命令。'),
}
_INVALID_REQUEST = '诊断操作或参数无效。'
_INVALID_CHECKS = '诊断项目必须是非空且不重复的固定项目列表。'
_ERRORS = {
    401: '该项诊断需要登录。',
    403: '该项诊断未授权。',
    409: '该项诊断正在进行，请稍后重试。',
}
_UNAVAILABLE = '该项诊断采集器暂不可用。'
_FAILED = '该项诊断暂不可确认。'


def catalog():
    """返回固定检查目录；调用方修改结果不会影响后续请求。"""
    return {'schema_version': 1, 'read_only': True,
            'checks': [{'id': check, 'label': _CHECK_DETAILS[check][0],
                        'description': _CHECK_DETAILS[check][1]} for check in ALLOWED_CHECKS]}


def _selected_checks(value):
    # 先验证元素类型，再判断成员和重复值，避免未哈希输入引发 TypeError。
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= len(ALLOWED_CHECKS):
        raise ValueError(_INVALID_CHECKS)
    selected = []
    for check in value:
        if not isinstance(check, str) or check not in ALLOWED_CHECKS or check in selected:
            raise ValueError(_INVALID_CHECKS)
        selected.append(check)
    return tuple(selected)


def parse_request(body):
    """只接受目录查询或固定快照；目录查询返回空的检查元组。"""
    if not isinstance(body, dict):
        raise ValueError(_INVALID_REQUEST)
    action = body.get('action')
    if not isinstance(action, str) or action not in ('catalog', 'snapshot'):
        raise ValueError(_INVALID_REQUEST)
    allowed = {'action'} if action == 'catalog' else {'action', 'checks'}
    if set(body) - allowed:
        raise ValueError(_INVALID_REQUEST)
    if action == 'catalog':
        return action, ()
    if 'checks' not in body:
        return action, CHECKS
    if not isinstance(body['checks'], list):
        raise ValueError(_INVALID_CHECKS)
    return action, _selected_checks(body['checks'])


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _failure(status=503, error=None):
    status = status if type(status) is int and 400 <= status <= 599 else 503
    return {'http_status': status, 'ok': False, 'data': None,
            'error': error or _ERRORS.get(status, _FAILED)}


def _collect_one(check, collectors):
    try:
        provider = collectors.get(check)
        if not callable(provider):
            return _failure(error=_UNAVAILABLE)
        status, payload = provider()
        if (type(status) is int and 200 <= status <= 299
                and isinstance(payload, dict) and payload.get('ok') is True):
            return {'http_status': status, 'ok': True,
                    'data': {key: value for key, value in payload.items() if key != 'ok'},
                    'error': None}
        return _failure(status)
    except Exception:
        # 采集器错误可能含路径、凭据或上游响应，不能写入响应。
        return _failure()


def collect(checks, collectors):
    """按请求顺序采集一次，并隔离每个检查的失败；不重试。

    collectors 按固定项目名提供无参数回调，返回 HTTP 状态与已投影的
    JSON 对象。成功项的 data 一律移除顶层 ok，保留其余字段和嵌套证据。
    all_checks_completed 仅表示所选回调成功，不表示所有嵌套来源均可用。
    """
    selected = _selected_checks(checks)
    started_at = _stamp()
    results = {check: _collect_one(check, collectors) for check in selected}
    return {'schema_version': 1, 'read_only': True,
            'started_at': started_at, 'finished_at': _stamp(), 'checks': results,
            'all_checks_completed': all(result['ok'] for result in results.values())}
