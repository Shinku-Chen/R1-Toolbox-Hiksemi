"""Persistent opt-in lighting rules over the verified, non-force I2C adapter."""
import copy
import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time


DEFAULT_CONFIG = {'schema_version': 1, 'enabled': False, 'mode': 'manual', 'sources': [],
                  'stops': [{'temperature': 35, 'color': '#00C832'},
                            {'temperature': 60, 'color': '#FFAA00'},
                            {'temperature': 80, 'color': '#FF3030'}],
                  'brightness': 70, 'manual': {'mode': 1, 'color': '#00C832', 'brightness': 70},
                  'schedule': {'enabled': False, 'start': '22:00', 'end': '07:00', 'brightness': 15}}


def require(value, message):
    if not value:
        raise ValueError(message)


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, '灯光数值超出范围')
    return value


def color(value):
    require(isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value), '灯光颜色需为 #RRGGBB')
    return value.upper()


def validate_config(value):
    require(isinstance(value, dict) and set(value) == set(DEFAULT_CONFIG), '灯光配置字段无效')
    result = copy.deepcopy(value)
    require(type(result['schema_version']) is int and result['schema_version'] == 1, '不支持的灯光配置版本')
    require(type(result['enabled']) is bool and result['mode'] in ('manual', 'temperature'), '灯光模式无效')
    sources = result['sources']
    require(isinstance(sources, list) and len(sources) <= 32
            and all(isinstance(item, str) and 0 < len(item) <= 256
                    and not any(ord(character) < 32 for character in item) for item in sources)
            and len(set(sources)) == len(sources), '温度来源需为不重复的最多32项列表')
    require(not result['enabled'] or result['mode'] != 'temperature' or sources, '温度联动需选择温度来源')
    stops = result['stops']
    require(isinstance(stops, list) and 2 <= len(stops) <= 8, '温度颜色需要2–8个节点')
    previous = None
    for point in stops:
        require(isinstance(point, dict) and set(point) == {'temperature', 'color'}, '温度颜色节点字段无效')
        temperature = integer(point['temperature'], -40, 150)
        require(previous is None or previous < temperature, '颜色节点温度必须递增')
        point['color'], previous = color(point['color']), temperature
    integer(result['brightness'], 0, 100)
    manual, schedule = result['manual'], result['schedule']
    require(isinstance(manual, dict) and set(manual) == {'mode', 'color', 'brightness'}, '手动灯效字段无效')
    integer(manual['mode'], 0, 3)
    integer(manual['brightness'], 0, 100)
    manual['color'] = color(manual['color'])
    require(isinstance(schedule, dict) and set(schedule) == {'enabled', 'start', 'end', 'brightness'}
            and type(schedule['enabled']) is bool, '夜间灯光计划字段无效')
    for name in ('start', 'end'):
        require(isinstance(schedule[name], str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', schedule[name]),
                '计划时间需为 HH:MM')
    require(not schedule['enabled'] or schedule['start'] != schedule['end'], '夜间起止时间不能相同')
    integer(schedule['brightness'], 0, 100)
    return result


def _finite(value):
    return type(value) in (int, float) and -40 <= value <= 150 and math.isfinite(value)


def decide(config, rows, now, wall):
    target = copy.deepcopy(config['manual'])
    reference = identity = label = None
    reason, fallback = '手动灯效', False
    if config['mode'] == 'temperature':
        by_id = {row.get('id'): row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}
        selected, missing = [], False
        for identity in config['sources']:
            row = by_id.get(identity)
            stamp, age = (row.get('sampled_monotonic'), row.get('max_age_seconds')) if row else (None, None)
            fresh = (type(stamp) in (int, float) and 0 <= stamp <= now and math.isfinite(stamp)
                     and type(age) in (int, float) and 0 < age <= 300 and now - stamp <= age)
            if not row or not fresh or row.get('status') not in ('ok', 'standby'):
                missing = True
            elif row['status'] == 'ok':
                if _finite(row.get('temperature_c')):
                    selected.append(row)
                else:
                    missing = True
        hottest = max(selected, key=lambda row: row['temperature_c']) if selected else None
        reference = hottest['temperature_c'] if hottest else None
        identity, label = (hottest['id'], hottest.get('label')) if hottest else (None, None)
        if missing or hottest is None:
            target = {'mode': 1, 'color': '#FFAA00', 'brightness': min(20, config['brightness'])}
            reason, fallback = '所选温度缺失、过期或均在休眠，使用低亮警示色', True
        else:
            stops = config['stops']
            shade = stops[0]['color'] if reference <= stops[0]['temperature'] else stops[-1]['color']
            for left, right in zip(stops, stops[1:]):
                if left['temperature'] <= reference <= right['temperature']:
                    ratio = (reference - left['temperature']) / (right['temperature'] - left['temperature'])
                    channels = []
                    for offset in (1, 3, 5):
                        a, b = int(left['color'][offset:offset + 2], 16), int(right['color'][offset:offset + 2], 16)
                        channels.append(round(a + (b - a) * ratio))
                    shade = '#' + ''.join(f'{value:02X}' for value in channels)
                    break
            target = {'mode': 1, 'color': shade, 'brightness': config['brightness']}
            reason = '所选有效来源取最高温，按颜色节点连续插值'
    schedule, night = config['schedule'], False
    if schedule['enabled']:
        current, start, end = wall.strftime('%H:%M'), schedule['start'], schedule['end']
        night = start <= current < end if start < end else current >= start or current < end
        if night:
            target['brightness'] = min(target['brightness'], schedule['brightness'])
    return {'target': target, 'reference_c': reference, 'source_id': identity, 'source_label': label,
            'reason': reason, 'fallback': fallback, 'schedule_active': night}


class LightPolicyController:
    def __init__(self, controller, sources, state_dir=None, *, clock=None, wall_clock=None):
        self.controller, self.sources = controller, sources
        self.state_dir = Path(state_dir) if state_dir is not None else controller.state_dir
        self.clock, self.wall_clock = clock or time.monotonic, wall_clock or datetime.datetime.now
        self.lock, self.control_lock = threading.RLock(), threading.Lock()
        self.stopped = threading.Event()
        self.thread = self.worker = None
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.active_config = copy.deepcopy(DEFAULT_CONFIG)
        self.resume_on_boot = self.active = self.busy = False
        self.original = self.last_state = self.target = None
        self.last_verified = None
        self.catalog, self.decision = [], {}
        self.capability = {'available': False, 'reason': '正在自动识别灯光接口'}
        self.last_error = self.settings_error = None
        self.settings_saved, self.last_apply_ok = None, False
        self._load()

    def _load(self):
        path = self.state_dir / 'light-policy.json'
        try:
            if not path.exists() and not path.is_symlink():
                return
            require(not self.state_dir.is_symlink(), '灯光数据目录不能是符号链接')
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536, '灯光配置文件无效')
                raw = stream.read(65537)
            require(len(raw) <= 65536, '灯光配置文件过大')
            data = json.loads(raw)
            require(isinstance(data, dict) and set(data) == {'config', 'active_config', 'resume_on_boot'}
                    and type(data['resume_on_boot']) is bool, '灯光配置格式无效')
            self.config, self.active_config = validate_config(data['config']), validate_config(data['active_config'])
            require(not data['resume_on_boot'] or self.active_config['enabled'], '灯光恢复意图与活动配置不一致')
            self.resume_on_boot = data['resume_on_boot']
            self.settings_saved = True
        except (OSError, ValueError, TypeError, RecursionError):
            self.config = copy.deepcopy(DEFAULT_CONFIG)
            self.active_config = copy.deepcopy(DEFAULT_CONFIG)
            self.resume_on_boot = False
            self.settings_error = '灯光配置未能读取，保持原厂灯光'
            self.settings_saved = False

    def _save(self, config, active_config, resume):
        require(not self.state_dir.is_symlink(), '灯光数据目录不能是符号链接')
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.state_dir / 'light-policy.json'
        require(not target.is_symlink(), '灯光配置不能是符号链接')
        data = json.dumps({'config': config, 'active_config': active_config, 'resume_on_boot': resume},
                          ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        require(len(data) <= 65536, '灯光配置过大')
        with tempfile.NamedTemporaryFile(prefix='.light-policy-', dir=self.state_dir, delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(target)
        directory = os.open(self.state_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.settings_saved = True

    def snapshot(self):
        with self.lock:
            rows, now = copy.deepcopy(self.catalog), self.clock()
            for row in rows:
                stamp, maximum = row.get('sampled_monotonic'), row.get('max_age_seconds')
                age = now - stamp if type(stamp) in (int, float) and 0 <= stamp <= now else None
                row['age_seconds'] = age
                row['fresh'] = bool(age is not None and type(maximum) in (int, float) and 0 < maximum <= 300 and age <= maximum)
            applied = bool(self.last_apply_ok and not self.busy and self.config == self.active_config
                           and (self.active or not self.config['enabled']))
            runtime = {'active': self.active, 'busy': self.busy, 'applied': applied,
                       'reason': '尚未启用灯光联动', 'fallback': False, 'schedule_active': False,
                       **copy.deepcopy(self.decision), 'target': copy.deepcopy(self.target)}
            runtime['source_age_seconds'] = next((row['age_seconds'] for row in rows if row.get('id') == runtime.get('source_id')), None)
            return {'config': copy.deepcopy(self.config), 'active_config': copy.deepcopy(self.active_config),
                    'active_on_boot': self.resume_on_boot, 'active': self.active, 'busy': self.busy,
                    'pending_changes': self.config != self.active_config or (self.config['enabled'] and not self.active),
                    'capability': copy.deepcopy(self.capability), 'sources': rows,
                    **copy.deepcopy(self.decision), 'target': copy.deepcopy(self.target),
                    'runtime': runtime, 'applied': applied, 'settings_saved': self.settings_saved,
                    'settings_error': self.settings_error, 'last_error': self.last_error}

    def _probe(self):
        try:
            value = self.controller.lighting_probe()
            with self.lock:
                self.capability = {'available': True, 'reason': None}
            return value
        except Exception:
            with self.lock:
                self.capability = {'available': False, 'reason': '未验证唯一灯控设备；未执行灯光写入'}
            raise

    def _release(self):
        try:
            restored = self.controller.lighting_release(self, self.original, self.last_state)
        finally:
            with self.lock:
                self.original = self.last_state = self.target = None
                self.active = False
        return restored

    def _fault(self, message):
        restored = False
        try:
            restored = self._release()
        except Exception:
            pass
        try:
            capability = copy.deepcopy(self.controller.snapshot()['capabilities']['rgb'])
        except Exception:
            capability = {'available': False, 'reason': '灯控设备当前状态未确认'}
        with self.lock:
            self.resume_on_boot = False
            self.last_apply_ok = False
            self.active_config = copy.deepcopy(DEFAULT_CONFIG)
            self.last_error = message + ('' if restored else '；接管前灯光恢复未确认或已让回外部设置')
            self.capability = capability
            try:
                self._save(self.config, self.active_config, False)
            except (OSError, ValueError):
                self.settings_error = '联动已停止，但停用状态未能保存，请检查设置数据卷'

    def _apply_worker(self, config):
        with self.control_lock:
            try:
                if self.stopped.is_set():
                    return
                if not config['enabled']:
                    restored = self._release()
                    with self.lock:
                        self._save(self.config, config, False)
                        self.active_config, self.resume_on_boot = copy.deepcopy(config), False
                        self.decision = {'reason': '已停止联动' if restored else '已停止联动，保留外部灯光设置'}
                        self.last_error = None
                        self.last_apply_ok = True
                    return
                current = self.controller.lighting_claim(self)
                if self.original is None:
                    self.original = current
                require(self.last_state is None or current == self.last_state, '原厂或其他应用已改变灯光，联动停止')
                rows = self._source_rows()
                decision = decide(config, rows, self.clock(), self.wall_clock())
                actual = self.controller.lighting_apply(self, decision['target'], expected=current)
                self.last_state, self.last_verified = actual, self.clock()
                if self.stopped.is_set():
                    self._release()
                    return
                with self.lock:
                    self._save(self.config, config, True)
                    self.active_config, self.resume_on_boot, self.active = copy.deepcopy(config), True, True
                    self.target, self.catalog, self.decision = decision['target'], copy.deepcopy(rows), decision
                    self.capability = {'available': True, 'reason': None}
                    self.last_error = self.settings_error = None
                    self.last_apply_ok = True
            except Exception:
                self._fault('灯光配置已保存，但应用未完成；请检查设备后重新应用')
            finally:
                with self.lock:
                    self.busy = False

    def action(self, name, payload):
        saved = False
        try:
            require(name == 'light.policy.update' and isinstance(payload, dict)
                    and not set(payload) - {'config', 'apply'} and 'config' in payload
                    and type(payload.get('apply', False)) is bool, '灯光配置请求无效')
            config, apply = validate_config(payload['config']), payload.get('apply', False)
            with self.lock:
                require(not self.busy and not self.stopped.is_set(), '灯光正在切换或停止，请稍后重试')
                resume = self.resume_on_boot if not apply or config['enabled'] else False
                self._save(config, self.active_config, resume)
                saved, self.config, self.resume_on_boot = True, config, resume
                self.settings_error = None
                if apply:
                    self.busy = True
                    self.worker = threading.Thread(target=self._apply_worker, args=(copy.deepcopy(config),),
                                                   daemon=True, name='r1-light-apply')
                    try:
                        self.worker.start()
                    except RuntimeError:
                        self.busy = False
                        raise
            return {'ok': True, 'settings_saved': True, 'applied': False, 'pending': apply, 'light_policy': self.snapshot()}
        except (OSError, ValueError, TypeError, RuntimeError):
            if not saved:
                self.settings_saved = False
            return {'ok': False, 'settings_saved': saved, 'applied': False, 'pending': False,
                    'error': '灯光配置未应用，请检查配置、设备与数据卷', 'light_policy': self.snapshot()}

    def tick(self):
        rows = self._source_rows()
        with self.lock:
            self.catalog = copy.deepcopy(rows)
            active, config = self.active and not self.busy, copy.deepcopy(self.active_config)
        if not active or self.stopped.is_set() or not self.control_lock.acquire(blocking=False):
            return
        try:
            if self.stopped.is_set():
                return
            with self.lock:
                if self.busy or not self.active:
                    return
                config = copy.deepcopy(self.active_config)
            decision = decide(config, rows, self.clock(), self.wall_clock())
            if decision['target'] != self.target:
                actual = self.controller.lighting_apply(self, decision['target'], expected=self.last_state)
                self.last_state, self.last_verified = actual, self.clock()
                self.target = decision['target']
            elif self.last_verified is None or self.clock() - self.last_verified >= 30:
                require(self._probe() == self.last_state, '原厂或其他应用已改变灯光，联动停止')
                self.last_verified = self.clock()
            with self.lock:
                self.decision = decision
        except Exception:
            self._fault('灯光设备或原厂设置发生变化，联动已停止，需重新应用')
        finally:
            self.control_lock.release()

    def _source_rows(self):
        try:
            rows = self.sources.snapshot()
            return [row for row in rows[:512] if isinstance(row, dict)] if isinstance(rows, list) else []
        except Exception:
            return []

    def _loop(self):
        try:
            self._probe()  # Default startup reads only the fixed known identity.
        except Exception:
            pass
        if self.resume_on_boot and not self.stopped.is_set():
            with self.lock:
                self.busy = True
                config = copy.deepcopy(self.active_config)
            self._apply_worker(config)
        while not self.stopped.wait(1):
            try:
                self.tick()
            except Exception:
                if self.control_lock.acquire(blocking=False):
                    try:
                        if self.active:
                            self._fault('温度采集异常，灯光联动已停止')
                    finally:
                        self.control_lock.release()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._loop, name='r1-light-policy', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        for thread in (self.thread, self.worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=15)
                require(not thread.is_alive(), '灯光操作尚未结束，恢复状态未确认')
        with self.control_lock:
            try:
                restored = self._release()
                with self.lock:
                    self.decision = {'reason': '已恢复接管前灯光寄存器' if restored else '已停止联动，保留外部灯光设置'}
            except Exception:
                with self.lock:
                    self.last_error = '联动已停止，但接管前灯光恢复未确认'
                raise
