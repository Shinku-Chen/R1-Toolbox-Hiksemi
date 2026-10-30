"""Shared, scoped NAS operations for authenticated MCP and REST clients.

The HTTP layer supplies an authenticated principal. Tool schemas and scopes are
checked by McpService; this adapter uses only the existing validated device
operations, with their normal locks, fan preflight and cancellable power delay.
"""

import copy
from datetime import datetime, timezone
import time

from api_access import ApiAccessStore, AUDIT_OPERATIONS
from api_contracts import IMPLEMENTED_TOOLS, contract_catalog
from api_docker_projects import DockerProjectRegistry
from api_docker_operations import DockerProjectOperations
from api_docker_deployment import DockerDeploymentRegistry, DockerDeploymentOperations
from api_file_readiness import collect_file_access_readiness
from api_file_roots import collect_file_roots, validate_file_grants, is_private_recovery_path
from api_file_executor import FileReadExecutor, TOOLS as FILE_EXECUTOR_TOOLS, WRITE_TOOLS as FILE_WRITE_TOOLS
from api_file_mutation_authority import FileMutationAuthority
from api_file_operations import FileAccessError
from mcp_service import McpService, SUPPORTED_VERSIONS, META_REQUIRED_SCOPE
from vendor_app_diagnostics import collect as collect_applications
from vendor_business import _result as project_business_category
from vendor_media_snapshot import collect_media_snapshot
from download_operations import DownloadOperations, TOOLS as DOWNLOAD_TOOLS
from vendor_media_operations import MediaOperations, TOOLS as MEDIA_OPERATION_TOOLS
from api_permissions import MIN_ROLE_BY_TOOL, permission_allows
from native_capability_specs import TOOL_SPECS as NATIVE_TOOL_SPECS, APPLICATION_INSTALL_SPECS
from vendor_native_capabilities import NativeCapabilities, _OPERATIONS as NATIVE_OPERATIONS
import api_monitoring


# Each media domain receives only its own rows, never the whole media cache.
BUSINESS_TOOLS = {
    'get_app_status': ('applications', ()),
    'get_download_status': ('downloads', ()),
    'get_movie_status': ('media', ('scraper',)),
    'get_album_status': ('media', ('face', 'ocr', 'thumbnail', 'similar', 'ai')),
    'get_share_status': ('media', ('shared_album',)),
}


def _observed_time(value):
    """Accept an aware cache timestamp; unknown/future age is never invented."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _freshness(value):
    stamp = _observed_time(value)
    try:
        elapsed = time.time() - datetime.fromisoformat(stamp).timestamp() if stamp else None
    except (OSError, ValueError, OverflowError):
        elapsed = None
    return {'observed_at': stamp,
            'age_seconds': int(elapsed) if elapsed is not None and elapsed >= 0 else None,
            'live': False, 'refresh': 'not_requested'}


class IntegrationApi:
    def __init__(self, web, *, state_dir, version='dev'):
        self.web = web
        self._closed = False
        self._close_error = None
        self._service_generation = 1
        self.access = ApiAccessStore(state_dir)
        self._state_dir = state_dir
        self.file_authority = FileMutationAuthority(web.panel, state_dir=state_dir)
        self.docker_projects = DockerProjectRegistry(state_dir)
        self.docker_operations = DockerProjectOperations(self.docker_projects, authorizer=self._is_current)
        self.docker_deployments = DockerDeploymentRegistry(state_dir)
        self.deployment_operations = DockerDeploymentOperations(self.docker_deployments, authorizer=self._is_current,
                                                                shared_operations=self.docker_operations)
        self.download_operations = DownloadOperations(authorizer=self._is_current)
        self.media_operations = MediaOperations()
        self.native_operations = NativeCapabilities()
        self.file_operations = FileReadExecutor(web.panel, authorizer=self._is_current,
                                              mutation_authority=self.file_authority)
        self.mcp = McpService(self.execute, version=version)

    def _is_current(self, principal):
        """An in-flight operation cannot outlive the resident Web service."""
        generation = principal.get('_service_generation', self._service_generation) if isinstance(principal, dict) else None
        return (not self._closed and type(generation) is int
                and generation == self._service_generation and self.access.is_current(principal))

    def close(self):
        if self._closed:
            return
        self._closed = True
        # A later open must never revive the authorization of a blocked call.
        self._service_generation += 1
        first_error = None
        for resource in (self.docker_operations, self.deployment_operations,
                         self.file_operations, self.file_authority):
            closer = getattr(resource, 'close', None)
            if callable(closer):
                try:
                    closer()
                except Exception as error:
                    if first_error is None:
                        first_error = error
        self._close_error = first_error
        if first_error is not None:
            raise first_error

    def open(self):
        if self._closed:
            # Closed workers and their plans never become active again when a
            # test or embedder restarts this Web object in the same process.
            self.docker_operations = DockerProjectOperations(self.docker_projects, authorizer=self._is_current)
            self.deployment_operations = DockerDeploymentOperations(self.docker_deployments, authorizer=self._is_current,
                                                                    shared_operations=self.docker_operations)
            self.download_operations = DownloadOperations(authorizer=self._is_current)
            self.media_operations = MediaOperations()
            self.native_operations = NativeCapabilities()
            self.file_authority = FileMutationAuthority(self.web.panel, state_dir=self._state_dir)
            self.file_operations = FileReadExecutor(self.web.panel, authorizer=self._is_current,
                                                  mutation_authority=self.file_authority)
            self._close_error = None
            self._closed = False

    def snapshot(self, *, current=None):
        current = current or (lambda: not self._closed)
        denied = {'ok': False, 'error': {'code': 'invalid_key', 'message': '网页登录授权已失效。'}}
        if current() is not True:
            return denied
        state = self.access.snapshot()
        names = {row['id']: row['name'] for row in state['keys']}
        state['audit'] = [dict(row, key_name=names.get(row.get('key_id')))
                          for row in state['audit']]
        result = {'ok': True, **state,
                'scope_catalog': self.scope_catalog(),
                'endpoints': {'mcp': '/mcp', 'status': '/api/integration/status',
                              'action': '/api/integration/action',
                              'capabilities': '/api/integration/capabilities',
                              'contracts': '/api/access/contracts'},
                'protocol_versions': list(SUPPORTED_VERSIONS),
                'file_root_catalog': self.file_root_catalog(),
                'file_mutation_authority': self.file_authority_catalog(current=current),
                'application_install_catalog': [
                    {field: copy.deepcopy(spec[field]) for field in
                     ('application_id', 'label', 'effect_summary', 'resource_effects')}
                    for spec in APPLICATION_INSTALL_SPECS.values()],
                'docker_project_catalog': self.docker_projects.metadata(),
                'docker_deployment_catalog': self.docker_deployments.metadata(),
                'tools': self.mcp.tool_catalog()}
        return result if current() is True else denied

    @staticmethod
    def scope_catalog():
        """Project partial implementation without granting pending operations."""
        from api_permissions import SCOPE_CATALOG
        result = copy.deepcopy(list(SCOPE_CATALOG))
        for item in result:
            if item['id'] == 'files':
                description = ('在逐次原厂权限核验的 /drives/*/Users/admin 范围浏览和下载，相对路径以 admin 为起点；'
                    '根下存在子挂载仍可选，实际跨入拒绝且列表跳过入口。'
                    '内部恢复区不授予文件访问。'
                    '已核验 admin 后代支持上传、独立覆盖、目录创建、复制、可恢复改名／移动和原厂回收站，'
                    '仍须精确工具、源／目标授权、恢复区及原厂索引等实际门槛。')
                item.update(implemented=True, description=description)
            elif item['id'] == 'docker':
                item.update(implemented=True, description='读取并控制已登记项目；管理员还可在所有者预先登记的独立资源上部署、更新或移除容器，始终保留数据卷。')
        return result

    def file_readiness(self):
        """No user file or credential reads; report every missing access proof."""
        return {'ok': True, **collect_file_access_readiness(self.web.panel)}

    @staticmethod
    def contracts():
        """Owner-only definitions; these do not grant or register operations."""
        return {'ok': True, **contract_catalog()}

    def file_root_catalog(self):
        """Owner-only planning metadata; this never enumerates user files."""
        return collect_file_roots(self.web.panel)

    def file_authority_catalog(self, *, current=None):
        """Owner metadata only; no Users directories are opened by this GET."""
        try:
            return self.file_authority.catalog(current=current or (lambda: not self._closed))
        except (FileAccessError, OSError, ValueError):
            return {'ok': False, 'authorities': [], 'error': {'code': 'write_backend_unverified'}}

    def file_authority_manage(self, route, body, *, current):
        """Fixed owner actions; Bearer and developer peers cannot initialize custody."""
        actions = {'/api/files/authority/initialize': 'initialize',
                   '/api/files/authority/preflight': 'preflight',
                   '/api/files/authority/inspect': 'snapshot',
                   '/api/files/authority/revoke': 'revoke',
                   '/api/files/authority/restore': 'restore'}
        action = actions.get(route)
        read_action = action in ('snapshot', 'preflight')
        fields = {'root_id'} if read_action else {'root_id', 'confirmed'}
        if action == 'restore':
            fields.add('recovery_id')
        if (action is None or not isinstance(body, dict) or set(body) != fields
                or not isinstance(body.get('root_id'), str)
                or not read_action and body.get('confirmed') is not True
                or action == 'restore' and not isinstance(body.get('recovery_id'), str)):
            raise ValueError('文件恢复管理参数无效')
        kwargs = {'current': current}
        if not read_action:
            kwargs['confirmed'] = True
        if action == 'restore':
            kwargs['recovery_id'] = body['recovery_id']
        try:
            return getattr(self.file_authority, action)(body['root_id'], **kwargs)
        except FileAccessError as error:
            return {'ok': False, 'error': {'code': error.code}}

    def capabilities(self, principal):
        """Expose this key's effective tools and selected domains, not NAS data.

        Any authenticated key can introspect its permissions without the read
        scope. Future domain grants are explicitly pending; they create no tool
        callbacks and do not query native services or install dependencies.
        """
        denied = {'ok': False, 'error': {'code': 'invalid_key', 'message': '读取能力期间授权已失效。', 'retryable': False}}
        if not self._is_current(principal):
            return denied
        tools = self.mcp.tool_catalog(principal)
        scopes = list(principal['scopes'])
        domains = []
        for domain in self.scope_catalog():
            if domain['id'] not in scopes:
                continue
            available = [tool['name'] for tool in tools
                         if tool['_meta'][META_REQUIRED_SCOPE] == domain['id']]
            implemented = domain.get('implemented') is not False
            pending_enable = (principal.get('policy_version', 1) < 3
                              and domain['id'] in ('files', 'docker', 'apps', 'downloads',
                                                   'movie', 'albums', 'sharing'))
            domains.append({'id': domain['id'], 'label': domain['label'],
                            'implemented': implemented, 'available_tools': available,
                            'status': 'pending_enable' if pending_enable else 'available' if available else 'not_granted' if implemented else 'backend_unverified',
                            'reason': '待接入，当前没有可调用工具。' if not implemented
                            else '需要所有者显式启用逐工具授权，旧密钥不会自动获得新工具。' if pending_enable
                            else '当前角色没有可执行的工具。' if not available else None})
        grants = []
        selected = principal.get('file_roots', []) if 'files' in scopes else []
        if selected:
            roots = {row['id']: row for row in self.file_root_catalog()['roots']}
            for grant in selected:
                root = roots.get(grant['id'])
                private = is_private_recovery_path(grant['path'])
                # Preserve the grant's configured mode, and report changed
                # mount state separately; never silently broaden or downgrade.
                can_use = not private and root is not None and not (root['read_only'] and grant['access'] == 'readwrite')
                grants.append({**copy.deepcopy(grant), 'available': can_use,
                               'label': root['label'] if root else None,
                               'read_only': root['read_only'] if root else None,
                               'reason': '内部恢复区仅由所有者恢复界面管理，不能授予文件访问。' if private
                               else '根目录当前无法确认。' if root is None
                               else '挂载已变为只读，原写入授权当前不可用。' if not can_use else None})
        result = {'ok': True, 'role': principal.get('role', 'legacy'),
                'policy_version': principal.get('policy_version', 1),
                'scopes': scopes, 'file_roots': grants,
                'tool_grants': list(principal.get('tool_grants', [])),
                'docker_projects': copy.deepcopy(principal.get('docker_projects', [])),
                'docker_deployments': copy.deepcopy(principal.get('docker_deployments', [])),
                'application_ids': list(principal.get('application_ids', [34] if set(
                    principal.get('tool_grants', [])).intersection((
                        'plan_native_application_install', 'install_native_application')) else [])),
                'tools': tools, 'domains': domains}
        return result if self._is_current(principal) else denied

    def _validate_new_grants(self, body):
        """Owner grants may select only registered callbacks and resources."""
        if 'tool_grants' in body:
            active = {tool['name'] for tool in self.mcp.tool_catalog()}
            grants = body['tool_grants']
            if not isinstance(grants, list) or any(not isinstance(name, str) or name not in active for name in grants):
                raise ValueError('只能选择已接入的具体工具，待接入操作不能授权。')
        if 'docker_projects' in body:
            projects = body['docker_projects']
            registered = {item['id']: item for item in self.docker_projects.metadata().get('projects', [])
                          if item.get('revoked') is not True}
            if (not isinstance(projects, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('id'), str) or row['id'] not in registered
                    or not isinstance(row.get('operations'), list) or not row['operations']
                    or any(operation not in ('inspect', 'control', 'update_image') for operation in row['operations'])
                    for row in projects)):
                raise ValueError('Docker 范围只能选择已登记项目及其明确操作。')
        if 'docker_deployments' in body:
            registered = {row['id'] for row in self.docker_deployments.metadata().get('deployments', [])
                          if row.get('revoked') is not True}
            grants = body['docker_deployments']
            if (not isinstance(grants, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('id'), str) or row['id'] not in registered for row in grants)):
                raise ValueError('新部署范围只能选择所有者已登记且未撤销的资源。')

    def manage(self, route, body):
        """Called only after the browser's existing session and CSRF gates."""
        if not isinstance(body, dict):
            raise ValueError('API 管理参数需要是对象')
        project_operations = {
            '/api/docker/projects/register': self.docker_projects.register,
            '/api/docker/projects/update': self.docker_projects.update,
            '/api/docker/projects/revoke': self.docker_projects.revoke,
            '/api/docker/deployments/register': self.docker_deployments.register,
            '/api/docker/deployments/revoke': self.docker_deployments.revoke,
            '/api/docker/deployments/refresh': self.docker_deployments.refresh,
        }
        if route in project_operations:
            return project_operations[route](body)
        if route == '/api/access/settings':
            return {'ok': True, **self.access.configure(body)}
        if route == '/api/access/keys':
            self._validate_new_grants(body)
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
            self._validate_new_grants(body)
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

    def _read_resource(self, name, arguments, principal):
        """Read registered metadata only, after initial machine authorization."""
        if name == 'list_file_roots':
            catalog = {root['id']: root for root in self.file_root_catalog()['roots']}
            grouped = {}
            for grant in principal.get('file_roots', []):
                grouped.setdefault(grant['id'], []).append({'path': grant['path'], 'access': grant['access']})
            roots = []
            for root_id, grants in grouped.items():
                root = catalog.get(root_id)
                usable = root is not None and not (root.get('read_only') and any(g['access'] == 'readwrite' for g in grants))
                roots.append({'root_id': root_id, 'label': str(root['label'])[:128] if root else None,
                              'grants': grants, 'binding_status': 'present' if usable else 'unavailable',
                              'verification': 'metadata_only',
                              'backend': {'status': 'backend_unverified' if usable else 'resource_unavailable',
                                          'reason': '仅确认根绑定元数据，文件 ACL 与安全打开尚未验证。' if usable else '存储根或已授权挂载模式当前无法确认。'}})
            return {'ok': True, 'roots': roots, 'may_wake_disks': False}
        if name == 'list_docker_projects':
            return self.docker_projects.list_projects(principal)
        if name == 'get_docker_project_status':
            return self.docker_projects.get_status(principal, arguments['project_id'])
        if name == 'get_docker_project_summary':
            return self.docker_projects.get_summary(principal, arguments['project_id'])
        raise ValueError('不支持的资源读取')

    def _cached_downloads(self):
        """Copy only an already published snapshot; never run download/login RPC.

        A refresh invalidates its epoch before publishing newer data. Missing or
        invalidated data stays missing instead of initiating a collector here.
        """
        lock = getattr(self.web, 'business_state_lock', None)
        if lock is None:
            return None, None
        with lock:
            snapshot = getattr(self.web, '_business_cached', None)
            if (not isinstance(snapshot, dict)
                    or getattr(self.web, '_business_cached_epoch', None)
                    != getattr(self.web, '_business_epoch', None)):
                return None, None
            categories = snapshot.get('categories')
            value = categories.get('downloads') if isinstance(categories, dict) else None
            return copy.deepcopy(value), snapshot.get('sampled_at')

    def _read_business(self, name):
        """Reuse passive provider projections, with no signed/native getter.

        Applications and media have validated fixed Toolbox cache readers.
        Downloads have no persistent task cache: only the owner's existing
        business snapshot can supply them. The UI's projection fence is applied
        again so future providers cannot accidentally disclose new fields.
        """
        category, selected = BUSINESS_TOOLS[name]
        captured_at = None
        if category == 'applications':
            provider = lambda **unused: collect_applications(state_dir=self.web.state_dir)
        elif category == 'media':
            provider = lambda **unused: collect_media_snapshot(self.web.state_dir)
        else:
            cached, captured_at = self._cached_downloads()
            provider = lambda **unused: cached if isinstance(cached, dict) else {
                'available': False, 'status': 'not_collected',
                'source': 'verified_local_download_engines', 'data': None,
                'reason': '尚无已采集的下载业务快照；未自动联网刷新。'}
        result = project_business_category(provider, '/', category)
        data = result.get('data')
        last_failure = None
        if category == 'applications':
            captured_at = result.get('sampled_at') or (data.get('cache_modified_at') if isinstance(data, dict) else None)
            if result.get('error_code'):
                last_failure = {'error_code': result['error_code'],
                                'observed_at': _observed_time(result.get('latest_attempt_at') or captured_at)}
        elif category == 'media' and isinstance(data, dict):
            rows = data.get('results') if isinstance(data.get('results'), list) else []
            rows = [row for row in rows if isinstance(row, dict) and row.get('id') in selected]
            # Preserve unavailable/partial rows and per-item historical failures;
            # no other domain's progress, timestamps or coverage counts escape.
            for row in rows:
                row['freshness'] = _freshness(row.get('captured_at'))
            stamps = [_observed_time(row.get('captured_at')) for row in rows]
            captured_at = max((stamp for stamp in stamps if stamp), default=None)
            complete = (len(rows) == len(selected) and all(
                row.get('status') == 'not_applicable' or row.get('status') == 'ok'
                and row.get('available') is True and row.get('query_success') is True for row in rows))
            available = any(row.get('available') is True for row in rows)
            result.update(available=available,
                          status='cached' if complete and available else 'partial' if available
                          else 'not_applicable' if rows and all(row.get('status') == 'not_applicable' for row in rows)
                          else 'not_collected' if not rows or all(row.get('status') == 'not_collected' for row in rows)
                          else 'unavailable')
            data = {'complete': complete, 'results': rows}
            last_failure = [{'id': row['id'], **row['last_failure']} for row in rows
                            if isinstance(row.get('last_failure'), dict)] or None
        elif category == 'downloads':
            failures = [row for row in result.get('engines', []) if isinstance(row, dict)
                        and row.get('available') is False]
            last_failure = [{'engine': row.get('engine'), 'status': row.get('status'),
                             'observed_at': _observed_time(captured_at)} for row in failures] or None
        return {'ok': True, 'read_only': True, 'cached': True,
                'available': result['available'], 'status': result['status'],
                'source': result.get('source'), 'reason': result.get('reason'),
                'freshness': _freshness(captured_at), 'last_failure': last_failure,
                'data': data, **({'engines': result['engines']} if 'engines' in result else {})}

    def execute(self, name, arguments, principal):
        """Fixed callbacks; short resource reads also recheck before disclosure."""
        if not isinstance(principal, dict):
            return {'ok': False, 'error': {'code': 'invalid_key', 'message': 'MCP 授权无效。', 'retryable': False}}
        principal = dict(principal)
        principal.setdefault('_service_generation', self._service_generation)
        if (self._closed or type(principal['_service_generation']) is not int
                or principal['_service_generation'] != self._service_generation):
            return {'ok': False, 'error': {'code': 'invalid_key', 'message': 'MCP 服务已停止。', 'retryable': False}}
        if name in FILE_EXECUTOR_TOOLS or name in FILE_WRITE_TOOLS and name in IMPLEMENTED_TOOLS:
            return self.file_operations.execute(name, arguments, principal)
        if name in api_monitoring.TOOLS:
            denied = {'ok': False, 'error': {'code': 'invalid_key', 'message': '读取期间授权已失效。', 'retryable': False}}
            if not permission_allows(principal, 'read', 'readonly', tool_name=name) or not self._is_current(principal):
                return denied
            result = api_monitoring.execute(getattr(self.web, 'monitoring', None), name, arguments)
            return result if self._is_current(principal) else denied
        if name == 'get_docker_project_resource_usage':
            return self.docker_projects.get_resource_usage(principal, arguments['project_id'], authorizer=self._is_current)
        if name == 'list_docker_deployments':
            return self.deployment_operations.list_deployments(principal)
        if name == 'plan_docker_deployment':
            return self.deployment_operations.plan(principal, arguments['deployment_id'], arguments['operation'],
                                                   arguments.get('image_reference'))
        if name == 'execute_docker_deployment':
            return self.deployment_operations.execute(principal, arguments['plan_id'], arguments['confirmed'],
                                                      arguments['idempotency_key'])
        if name == 'get_docker_deployment_task':
            return self.deployment_operations.get_task(principal, arguments['deployment_id'], arguments['task_id'])
        if name in NATIVE_OPERATIONS:
            scope = next(item['scope'] for item in NATIVE_TOOL_SPECS if item['name'] == name)
            def authorize_native(operation, parameters, body):
                return (operation == NATIVE_OPERATIONS[name]
                        and permission_allows(principal, scope, MIN_ROLE_BY_TOOL[name], tool_name=name)
                        and self._is_current(principal))
            if not authorize_native(NATIVE_OPERATIONS[name], {}, {}):
                return {'ok': False, 'error': {'code': 'invalid_key', 'message': '当前授权不允许此原厂操作。', 'retryable': False}}
            if not self.web.media_probe_lock.acquire(blocking=False):
                return {'ok': False, 'error': {'code': 'operation_busy', 'message': '已有原厂查询或操作正在进行。', 'retryable': True}}
            try:
                result = self.native_operations.execute(name, arguments, principal=principal, authorizer=authorize_native)
                result['ok'] = result.get('available') is True
                if not result['ok']:
                    result['error'] = {'code': result.get('reason') or 'native_unavailable',
                                       'message': '原厂操作未完成，请查看具体原因。', 'retryable': False}
                if not self._is_current(principal):
                    return {'ok': False, 'error': {'code': 'invalid_key', 'message': '操作期间授权已失效。', 'retryable': False}}
                return result
            finally:
                self.web.media_probe_lock.release()
        if name == 'control_docker_project':
            return self.docker_operations.control(principal, arguments['project_id'], arguments['operation'],
                                                  arguments['confirmed'], arguments['idempotency_key'])
        if name == 'plan_docker_project_update':
            return self.docker_operations.plan_update(principal, arguments['project_id'], arguments['service_id'], arguments['image_reference'])
        if name == 'execute_docker_project_update':
            return self.docker_operations.execute_update(principal, arguments['plan_id'], arguments['confirmed'], arguments['idempotency_key'])
        if name == 'get_docker_project_task':
            return self.docker_operations.get_task(principal, arguments['project_id'], arguments['task_id'])
        if name in DOWNLOAD_TOOLS:
            return self.download_operations.execute(name, arguments, principal)
        if name in MEDIA_OPERATION_TOOLS:
            metadata = MEDIA_OPERATION_TOOLS[name]
            def authorize(operation_id, parameters, body):
                return (operation_id == metadata['operation']
                        and permission_allows(principal, metadata['scope'], MIN_ROLE_BY_TOOL[name], tool_name=name)
                        and self._is_current(principal))
            if not authorize(metadata['operation'], {}, {}):
                return {'ok': False, 'error': {'code': 'invalid_key', 'message': '当前密钥未授权此媒体操作。', 'retryable': False}}
            if not self.web.media_probe_lock.acquire(blocking=False):
                return {'ok': False, 'error': {'code': 'operation_busy', 'message': '已有媒体查询或操作正在进行。', 'retryable': True}}
            try:
                result = self.media_operations.execute(name, arguments, authorizer=authorize, principal=principal)
                result['ok'] = result.get('available') is True
                if not result['ok']:
                    result['error'] = {'code': result.get('reason') or 'native_unavailable',
                        'message': '原厂媒体操作未完成，请查看具体原因。', 'retryable': False}
                if not self._is_current(principal):
                    return {'ok': False, 'error': {'code': 'invalid_key', 'message': '操作期间授权已失效，请读取当前状态。', 'retryable': False}}
                return result
            finally:
                self.web.media_probe_lock.release()
        if name in IMPLEMENTED_TOOLS or name in BUSINESS_TOOLS:
            denied = {'ok': False, 'error': {'code': 'invalid_key',
                       'message': '读取期间授权已失效，请使用当前有效密钥。', 'retryable': False}}
            if not self._is_current(principal):
                return denied
            result = (self._read_business(name) if name in BUSINESS_TOOLS
                      else self._read_resource(name, arguments, principal))
            return result if self._is_current(principal) else denied
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
