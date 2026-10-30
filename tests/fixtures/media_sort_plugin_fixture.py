"""Independent real WebConfig fixture; media rules exist only in memory.

No SQLite/media files, native programs, NAS endpoints or hardware are used.
The stdin channel belongs to the browser test, not the HTTP product surface.
"""
import copy
import json
import os
from pathlib import Path
import sys
import threading

repo, fixture, assets = (Path(value).resolve() for value in sys.argv[1:4])
assert fixture.name.startswith('r1-media-sort-browser-') and fixture.is_dir()
sys.path.insert(0, str(repo / 'runtime'))
os.environ['R1_WEB_AUTH_MODE'] = 'password'
from toolbox import DEFAULTS
from web_config import WebConfigServer


class Controller:
    def __init__(self):
        self.state_dir = fixture / 'data'

    def snapshot(self):
        return {'settings': copy.deepcopy(DEFAULTS), 'settings_error': None,
                'capabilities': {'screen': {'available': False}, 'rgb': {'available': False}},
                'screen': {}, 'rgb': {}, 'fans': {}, 'power': {}}

    def action(self, *args, **kwargs):
        raise AssertionError('the media browser fixture never performs device actions')


class Plugin:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls, self.discovered = [], []
        self.sequence = 0
        self.state = {'schema_version': 1, 'id': 'media_sort', 'status': 'unknown',
                      'busy': False, 'checked_at': None, 'message': '尚未检测数据库。',
                      'candidates': [], 'may_wake_disks': True}

    def snapshot(self, **kwargs):
        with self.lock:
            return copy.deepcopy(self.state)

    def probe(self, *, current=None, **kwargs):
        assert callable(current) and current() is True
        with self.lock:
            self.calls.append({'action': 'probe', 'body': {}})
            self.state.update(status='ready', busy=False, checked_at=1793390000,
                              message='检测完成。', candidates=copy.deepcopy(self.discovered))
            assert current() is True
            return self.snapshot()

    def change(self, action, candidate_id, version, confirmed, current):
        assert confirmed is True and callable(current) and current() is True
        with self.lock:
            row = next(item for item in self.state['candidates'] if item['id'] == candidate_id)
            assert row['version'] == version, 'fixture stale version'
            self.calls.append({'action': action, 'body': {'candidate_id': candidate_id,
                              'version': version, 'confirmed': confirmed}})
            self.sequence += 1
            row.update(state='enabled' if action == 'apply' else 'disabled',
                       can_apply=action != 'apply', can_revert=action == 'apply',
                       rule_version=2 if action == 'apply' else None,
                       version='revision_' + str(self.sequence), reason=None)
            if action == 'apply':
                row['last_backup_path'] = str(fixture / ('backup-' + 'long-' * 30) / 'scraper.sqlite.backup')
            self.discovered = copy.deepcopy(self.state['candidates'])
            self.state['message'] = '已核验插件当前规则。'
            assert current() is True
            return self.snapshot()

    def apply(self, candidate_id, version, *, confirmed=False, current=None, **kwargs):
        return self.change('apply', candidate_id, version, confirmed, current)

    def revert(self, candidate_id, version, *, confirmed=False, current=None, **kwargs):
        return self.change('revert', candidate_id, version, confirmed, current)

    def close(self, **kwargs):
        pass


plugin = Plugin()
web = WebConfigServer(Controller(), host='127.0.0.1', port=0, asset_dir=assets,
                      dev_root=fixture / 'dev', udev_root=fixture / 'udev',
                      media_sort_plugin=plugin).start()
try:
    print(json.dumps({'ready': True, 'origin': 'http://127.0.0.1:' + str(web.port)}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        with plugin.lock:
            if request['operation'] == 'configure':
                if 'candidates' in request:
                    plugin.discovered = copy.deepcopy(request['candidates'])
                if 'state' in request:
                    plugin.state.update(copy.deepcopy(request['state']))
            elif request['operation'] != 'stats':
                raise AssertionError('unknown trusted fixture command')
            result = {'request_id': request['request_id'], 'calls': copy.deepcopy(plugin.calls),
                      'plugin': plugin.snapshot()}
        print(json.dumps(result), flush=True)
finally:
    web.stop()
