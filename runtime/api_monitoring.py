"""Small machine projections of existing in-memory monitoring caches only.

No method here samples hardware, opens history storage or refreshes a vendor
service. Dynamic sensor/SMART identities and raw diagnostic text stay private.
The integration caller must recheck the real key before and after projection.
"""

import hashlib
import math
import re
import time


METRICS = {
    'cpu_usage_percent': ('CPU 使用率', '%'),
    'memory_usage_percent': ('内存使用率', '%'),
    'rx_bytes_per_sec': ('已计网口接收速率', 'B/s'),
    'tx_bytes_per_sec': ('已计网口发送速率', 'B/s'),
    'cpu_temperature_c': ('CPU 最高温度', '°C'),
    'disk_temperature_c': ('硬盘最高温度', '°C'),
    'cpu_package_w': ('CPU 封装功耗 · RAPL', 'W'),
    'disk_read_bytes_per_sec': ('物理磁盘读取速率', 'B/s'),
    'disk_write_bytes_per_sec': ('物理磁盘写入速率', 'B/s'),
    'fan_cpu_target_percent': ('CPU 风扇请求输出', '%'),
    'fan_system_target_percent': ('系统风扇请求输出', '%'),
}
RANGES = ('10m', '1h', '6h', '24h', '7d', '30d')
TOOLS = frozenset(('get_monitoring_history', 'get_health_events', 'get_network_traffic_totals'))
_MAX = 2 ** 53 - 1
_INTERFACE = re.compile(r'[A-Za-z0-9_.:@-]{1,64}\Z', re.ASCII)
_ALERTS = {'cpu_load': 'CPU 使用率', 'memory': '内存使用率',
           'cpu_temperature': 'CPU 温度', 'disk_temperature': '硬盘温度'}


def _number(value, minimum=0, maximum=_MAX):
    return (value if type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum else None)


def _integer(value):
    return value if type(value) is int and 0 <= value <= _MAX else None


def _boolean(value):
    return value if type(value) is bool else None


def _failure(code):
    return {'ok': False, 'error': {'code': code, 'message': '监控缓存或参数暂不可用。', 'retryable': False}}


def _history(monitor, arguments):
    if (set(arguments) - {'metric', 'range', 'limit'} or arguments.get('metric') not in METRICS
            or arguments.get('range', '1h') not in RANGES
            or type(arguments.get('limit', 720)) is not int
            or not 1 <= arguments.get('limit', 720) <= 720):
        return _failure('invalid_arguments')
    metric, span, limit = arguments['metric'], arguments.get('range', '1h'), arguments.get('limit', 720)
    raw = monitor.history(span)
    if type(raw) is not dict or type(raw.get('series')) is not list or len(raw['series']) > 64:
        return _failure('invalid_cache')
    selected = [row for row in raw['series'] if type(row) is dict and row.get('id') == metric]
    if len(selected) > 1:
        return _failure('invalid_cache')
    points, invalid = [], 0
    if selected:
        series = selected[0].get('points')
        if type(series) is not list or len(series) > 720:
            return _failure('invalid_cache')
        previous = None
        for point in series:
            if (type(point) is not list or len(point) != 2 or _number(point[0]) is None
                    or previous is not None and point[0] <= previous):
                return _failure('invalid_cache')
            maximum = (100 if metric.endswith('percent') else 150 if metric.endswith('temperature_c')
                       else 1000 if metric == 'cpu_package_w' else _MAX)
            value = _number(point[1], -40 if metric.endswith('temperature_c') else 0, maximum)
            invalid += int(point[1] is not None and value is None)
            points.append([point[0], value])
            previous = point[0]
    return {'ok': True, 'cached': True, 'read_only': True, 'source': 'toolbox.monitoring.history_cache',
            'available': raw.get('available') is True and bool(selected),
            'metric': metric, 'label': METRICS[metric][0], 'unit': METRICS[metric][1], 'range': span,
            'aggregation': 'last' if raw.get('aggregation') == 'last' else None,
            'sample_interval_seconds': _number(raw.get('sample_interval_seconds'), 1, 86400),
            'recording': _boolean(raw.get('recording')), 'points': points[-limit:],
            'truncated': len(points) > limit, 'projection_complete': invalid == 0,
            'known_points': sum(point[1] is not None for point in points[-limit:]),
            'missing_points': sum(point[1] is None for point in points[-limit:]),
            'sampling_continuity': 'not_asserted',
            'reason': 'metric_history_unavailable' if not selected or raw.get('available') is not True else None}


def _alert(row):
    if type(row) is not dict or type(row.get('id')) is not str:
        return None
    identity = row['id']
    if identity in _ALERTS:
        public_id, label = identity, _ALERTS[identity]
    elif re.fullmatch(r'capacity:[0-9a-f]{16}', identity):
        public_id, label = identity, '数据盘已用空间'
    elif identity.startswith('smart:') and 6 < len(identity) <= 160:
        public_id = 'smart:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
        label = '磁盘 SMART 告警'
    else:
        return None
    if row.get('level') not in ('info', 'warning', 'critical') or _number(row.get('timestamp')) is None:
        return None
    return {'id': public_id, 'label': label, 'level': row['level'],
            'timestamp': row['timestamp'], 'since': _number(row.get('since')),
            'value': _number(row.get('value'), -40),
            'unit': row.get('unit') if row.get('unit') in ('%', '°C') else None,
            'stale': _boolean(row.get('stale', False))}


def _health(monitor, arguments):
    if (set(arguments) - {'limit', 'since'} or type(arguments.get('limit', 50)) is not int
            or not 1 <= arguments.get('limit', 50) <= 100
            or 'since' in arguments and _number(arguments['since']) is None):
        return _failure('invalid_arguments')
    combined = monitor.health_events()
    if type(combined) is not dict:
        return _failure('invalid_cache')
    raw, state = combined.get('alerts'), combined.get('state')
    if (type(raw) is not dict or type(state) is not dict
            or any(type(raw.get(key)) is not list or len(raw[key]) > 2000 for key in ('active', 'events'))):
        return _failure('invalid_cache')
    active, events, skipped = [], [], 0
    for key, target in (('active', active), ('events', events)):
        for item in raw[key]:
            row = _alert(item)
            if row is None or key == 'events' and item.get('kind') not in ('raised', 'changed', 'recovered'):
                skipped += 1
                continue
            if key == 'events':
                if row['timestamp'] < arguments.get('since', 0):
                    continue
                row['kind'] = item['kind']
            target.append(row)
    summary = state.get('summary') if type(state.get('summary')) is dict else {}
    current = state.get('current') if type(state.get('current')) is dict else {}
    observed = _number(current.get('sampled_at'))
    age = time.time() - observed if observed is not None else None
    age = age if age is not None and age >= 0 else None
    fresh = age is not None and age <= 90
    status = summary.get('status') if summary.get('status') in ('ok', 'warning', 'critical', 'unknown') else 'unknown'
    assessment_complete = (fresh and bool(_integer(summary.get('known_metrics')))
                           and _integer(summary.get('unknown_metrics')) == 0 and skipped == 0)
    if not fresh or status == 'ok' and not assessment_complete:
        status = 'unknown'
    limit = arguments.get('limit', 50)
    return {'ok': True, 'cached': True, 'read_only': True, 'source': 'toolbox.monitoring.alerts_cache',
            'available': observed is not None, 'observed_at': observed, 'assessment': status,
            'age_seconds': age, 'fresh': fresh,
            'assessment_complete': assessment_complete,
            'known_metrics': _integer(summary.get('known_metrics')),
            'unknown_metrics': _integer(summary.get('unknown_metrics')),
            'active': active[-100:], 'events': events[-limit:],
            'complete': skipped == 0, 'skipped_entries': skipped,
            'truncated': len(active) > 100 or len(events) > limit,
            'coverage': 'locally_observed_cached_metrics'}


def _traffic(monitor, arguments):
    if arguments:
        return _failure('invalid_arguments')
    raw = monitor.traffic()
    if type(raw) is not dict or type(raw.get('interfaces')) is not list or len(raw['interfaces']) > 128:
        return _failure('invalid_cache')
    def counters(value):
        if type(value) is not dict:
            return None
        return {key: _integer(value.get(key)) for key in ('rx_bytes', 'tx_bytes')}
    interfaces, seen = [], set()
    for row in raw['interfaces']:
        if (type(row) is not dict or type(row.get('name')) is not str
                or not _INTERFACE.fullmatch(row['name']) or row['name'] in seen):
            return _failure('invalid_cache')
        seen.add(row['name'])
        interfaces.append({'name': row['name'], 'today': counters(row.get('today')),
                           'month': counters(row.get('month'))})
    periods = raw.get('periods') if type(raw.get('periods')) is dict else {}
    totals = raw.get('totals') if type(raw.get('totals')) is dict else {}
    return {'ok': True, 'cached': True, 'read_only': True, 'source': 'toolbox.monitoring.traffic_cache',
            'available': raw.get('available') is True, 'interfaces': interfaces,
            'totals': {key: counters(totals.get(key)) for key in ('today', 'month')},
            'periods': {key: periods.get(key) if type(periods.get(key)) is str and re.fullmatch(pattern, periods[key])
                        else None for key, pattern in (('day', r'\d{4}-\d{2}-\d{2}'), ('month', r'\d{4}-\d{2}'))},
            'partial': _boolean(raw.get('partial')), 'coverage': 'observed_counter_deltas'}


def execute(monitor, tool, arguments):
    """Invoke only cache accessors; never accept a collector or diagnostic route."""
    if tool not in TOOLS or type(arguments) is not dict:
        return _failure('invalid_arguments')
    if monitor is None:
        return _failure('monitoring_unavailable')
    try:
        return {'get_monitoring_history': _history, 'get_health_events': _health,
                'get_network_traffic_totals': _traffic}[tool](monitor, arguments)
    except Exception:
        return _failure('monitoring_unavailable')
