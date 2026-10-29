"""Shared, scoped NAS operations for authenticated MCP and REST clients.

The HTTP layer supplies an authenticated principal. Tool schemas and scopes are
checked by McpService; this adapter uses only the existing validated device
operations, with their normal locks, fan preflight and cancellable power delay.
"""

import copy
import time

from api_access import ApiAccessStore, AUDIT_OPERATIONS
from api_contracts import contract_catalog
from api_file_roots import collect_file_roots, validate_file_grants
from mcp_service import McpService, SUPPORTED_VERSIONS, META_REQUIRED_SCOPE
from vendor_app_diagnostics import collect as collect_applications


class IntegrationApi:
    def __init__(self, web, *, state_dir, version='dev'):
        self.web = web
        self.access = ApiAccessStore(state_dir)
        self.mcp = McpService(self.execute, version=version)

    def snapshot(self):
        state = self.access.snapshot()
        names = {row['id']: row['name'] for row in state['keys']}
        state['audit'] = [dict(row, key_name=names.get(row.get('key_id')))
                          for row in state['audit']]
        return {'ok': True, **state,
                'endpoints': {'mcp': '/mcp', 'status': '/api/integration/status',
                              'action': '/api/integration/action',
                              'capabilities': '/api/integration/capabilities',
                              'contracts': '/api/access/contracts'},
                'protocol_versions': list(SUPPORTED_VERSIONS),
                'file_root_catalog': self.file_root_catalog(),
                'tools': self.mcp.tool_catalog()}

    @staticmethod
    def contracts():
        """Owner-only definitions; these do not grant or register operations."""
        return {'ok': True, **contract_catalog()}

    def file_root_catalog(self):
        """Owner-only planning metadata; this never enumerates user files."""
        return collect_file_roots(self.web.panel)

    def capabilities(self, principal):
        """Expose this key's effective tools and selected domains, not NAS data.

        Any authenticated key can introspect its permissions without the read
        scope. Future domain grants are explicitly pending; they create no tool
        callbacks and do not query native services or install dependencies.
        """
        from api_permissions import SCOPE_CATALOG
        tools = self.mcp.tool_catalog(principal)
        scopes = list(principal['scopes'])
        domains = []
        for domain in SCOPE_CATALOG:
            if domain['id'] not in scopes:
                continue
            available = [tool['name'] for tool in tools
                         if tool['_meta'][META_REQUIRED_SCOPE] == domain['id']]
            implemented = domain.get('implemented') is not False
            domains.append({'id': domain['id'], 'label': domain['label'],
                            'implemented': implemented, 'available_tools': available,
                            'reason': '待接入，当前没有可调用工具。' if not implemented
                            else '当前角色没有可执行的工具。' if not available else None})
        grants = []
        selected = principal.get('file_roots', []) if 'files' in scopes else []
        if selected:
            roots = {row['id']: row for row in self.file_root_catalog()['roots']}
            for grant in selected:
                root = roots.get(grant['id'])
                # Preserve the grant's configured mode, and report changed
                # mount state separately; never silently broaden or downgrade.
                can_use = root is not None and not (root['read_only'] and grant['access'] == 'readwrite')
                grants.append({**copy.deepcopy(grant), 'available': can_use,
                               'label': root['label'] if root else None,
                               'mount': root['mount'] if root else None,
                               'read_only': root['read_only'] if root else None,
                               'reason': '根目录当前无法确认。' if root is None
                               else '挂载已变为只读，原写入授权当前不可用。' if not can_use else None})
        return {'ok': True, 'role': principal.get('role', 'legacy'),
                'policy_version': principal.get('policy_version', 1),
                'scopes': scopes, 'file_roots': grants,
                'tools': tools, 'domains': domains}

    def manage(self, route, body):
        """Called only after the browser's existing session and CSRF gates."""
        if not isinstance(body, dict):
            raise ValueError('API 管理参数需要是对象')
        if route == '/api/access/settings':
            return {'ok': True, **self.access.configure(body)}
        if route == '/api/access/keys':
            if body.get('file_roots'):
                validate_file_grants(body['file_roots'], self.file_root_catalog())
            created = self.access.create_key(body)
            token = created.pop('token')
            return {'ok': True, 'key': created, 'token': token}
        if route == '/api/access/keys/revoke':
            if set(body) != {'id'} or type(body['id']) is not str:
                raise ValueError('撤销密钥需要有效的 id')
            return {'ok': True, **self.access.revoke_key(body['id'])}
        if route == '/api/access/keys/update':
            if body.get('file_roots'):
                validate_file_grants(body['file_roots'], self.file_root_catalog())
            return {'ok': True, 'key': self.access.update_key(body)}
        if route == '/api/access/keys/enable':
            if set(body) != {'id', 'enabled'} or type(body['enabled']) is not bool:
                raise ValueError('密钥开关需要 id 与 enabled 布尔值')
            return {'ok': True, 'key': self.access.set_key_enabled(body['id'], body['enabled'])}
        if route == '/api/access/keys/rotate':
            if set(body) != {'id'} or type(body['id']) is not str:
                raise ValueError('轮换密钥需要有效的 id')
            created = self.access.rotate_key(body['id'])
            token = created.pop('token')
            return {'ok': True, 'key': created, 'token': token}
        raise ValueError('API 管理操作不存在')

    def record(self, principal, operation, peer, result, *, started, touch_key=True):
        """Do not persist arguments, results, headers or arbitrary tool names."""
        if operation not in AUDIT_OPERATIONS:
            operation = 'tools/call'
        outcome = 'success'
        if isinstance(result, dict):
            if 'error' in result or result.get('isError') is True or result.get('ok') is False:
                outcome = 'error'
            code = result.get('error', {}).get('code') if isinstance(result.get('error'), dict) else None
            structured = result.get('structuredContent')
            if isinstance(structured, dict) and isinstance(structured.get('error'), dict):
                code = structured['error'].get('code')
            if code in ('scope_denied', 'role_denied', 'permission_denied', 'forbidden'):
                outcome = 'denied'
        try:
            self.access.record(principal, operation, peer, outcome,
                               duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                               touch_key=touch_key)
        except (OSError, ValueError):
            # The access store locks itself on persistence failure. A completed
            # hardware operation must not be reported as unexecuted and retried.
            pass

    def _status(self, section):
        panel, web = self.web.panel, self.web
        if section == 'overview':
            return panel.overview()
        if section == 'system':
            data = panel.system()
            data.pop('data_dir', None)
            return data
        if section == 'storage':
            overview = panel.overview()
            return {'disks': overview.get('disks_detail'),
                    'used_bytes': overview.get('disk_used'), 'total_bytes': overview.get('disk_size'),
                    'health': panel.disk_health()}
        if section == 'temperatures':
            overview = panel.overview()
            disks = overview.get('disks_detail')
            return {'sensors': panel.sensors(), 'temperature': overview.get('temp'),
                    'disks': [{key: row.get(key) for key in ('name', 'user_name', 'space_names', 'temp', 'standby')}
                              for row in disks if isinstance(row, dict)] if isinstance(disks, list) else None}
        if section == 'network':
            return {'traffic': panel.traffic(), 'interfaces': panel.overview().get('net')}
        if section == 'containers':
            return panel.docker()
        if section == 'apps':
            return collect_applications(state_dir=web.state_dir)
        if section == 'fans':
            return {'fan_policy': web._read(web.fan_policy), 'sensors': panel.fans()}
        if section == 'lights':
            return {'light_policy': web._read(web.light_policy), 'rgb': web._state().get('rgb')}
        if section == 'screen':
            state = web._state()
            return {key: state.get(key) for key in ('screen', 'settings', 'capabilities')}
        raise ValueError('不支持的状态类别')

    def execute(self, name, arguments, principal):
        """Fixed tool callbacks; caller scopes and schemas are checked first."""
        if name == 'get_nas_status':
            return {'ok': True, 'section': arguments.get('section', 'overview'),
                    'data': self._status(arguments.get('section', 'overview'))}
        if name == 'get_media_status':
            if not self.web.media_probe_lock.acquire(blocking=False):
                return {'ok': False, 'error': '已有媒体测试正在进行', 'status': 409}
            try:
                # Resolve from web_config so the browser and machine routes use
                # exactly the same collectors and fixture replacement points.
                from web_config import collect_media_probe, collect_media_batch
                action = arguments.get('action', 'face')
                value = collect_media_batch() if action == 'all' else collect_media_probe()
                receipt = self.web._save_media_snapshot(value, 'batch' if action == 'all' else 'face')
                return {'ok': True, 'action': action, 'data': value, 'cache': receipt}
            finally:
                self.web.media_probe_lock.release()
        routes = {'set_fan_policy': '/api/r1/fan', 'release_fan': '/api/r1/fan/release',
                  'set_light_policy': '/api/r1/light', 'set_rgb': '/api/r1/rgb',
                  'set_screen': '/api/r1/screen', 'update_screen_settings': '/api/r1/settings',
                  'request_power': '/api/r1/power', 'cancel_power': '/api/r1/power/cancel'}
        if name not in routes:
            raise ValueError('不支持的集成操作')
        if not self.web.operation_lock.acquire(blocking=False):
            return {'ok': False, 'error': '已有设置正在保存或应用，请稍后重试', 'status': 409}
        try:
            body = copy.deepcopy(arguments)
            if name in ('set_fan_policy', 'set_light_policy'):
                body.setdefault('apply', True)
            status, payload = self.web._panel_write(routes[name], body, {})
            return {'ok': 200 <= status < 300 and payload.get('ok') is not False,
                    'status': status, 'data': payload}
        finally:
            self.web.operation_lock.release()
