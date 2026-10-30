"""Pure, implemented native business tool definitions shared by policy/catalog.

No executor, authentication, filesystem, or policy imports belong here.
"""


def _object(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {},
            'required': list(properties or {}) if required is None else required,
            'additionalProperties': False}


_CONFIRM = {'confirmed': {'type': 'boolean', 'const': True},
            'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                                'pattern': '^[A-Za-z0-9._:-]{1,128}$'}}
_ID = {'type': 'integer', 'minimum': 1, 'maximum': 2 ** 53 - 1}


def _spec(name, title, description, scope, role, properties=None, *, write=False,
          destructive=False):
    fields = dict(properties or {})
    if write:
        fields.update(_CONFIRM)
    return {'name': name, 'title': title, 'description': description,
            'scope': scope, 'min_role': role, 'inputSchema': _object(fields),
            'annotations': {'readOnlyHint': False, 'destructiveHint': destructive,
                            'idempotentHint': False, 'openWorldHint': False}}


# Authenticated native GETs may initialize owner resources. Even these reads
# deliberately require readwrite/admin and carry no readOnlyHint promise.
TOOL_SPECS = (
    _spec('list_native_applications', '查询原厂应用目录',
          '显式查询原厂当前目录，返回应用 ID、名称、版本和原始状态；不表示全部已安装或运行应用，不安装软件。',
          'apps', 'readwrite'),
    _spec('get_application_install_progress', '核对应用安装状态',
          '显式推进并核对指定既有目录应用的原厂安装状态；原厂可能同步插件并写完成状态，须确认和幂等键；不伪造进度百分比。',
          'apps', 'admin', {'application_id': {'type': 'integer', 'minimum': 1,
                                             'maximum': 2 ** 31 - 1}}, write=True),
    _spec('list_backup_tasks', '查询原厂备份任务',
          '显式读取当前原厂管理员的运行、等待和完成备份任务，只返回任务元数据与原始状态，不返回来源或目标文件路径。',
          'backup', 'readwrite'),
    _spec('get_auto_backup_settings', '读取原厂自动备份设置',
          '显式读取 USB 与 SD 自动备份原始设置值；不扫描目录、不创建备份任务，未知枚举保持未知。',
          'backup', 'readwrite'),
    _spec('disable_auto_backup', '关闭原厂自动导入备份',
          '管理员关闭指定 USB 或 SD 自动导入；原厂还可能清理该来源的失败任务记录。不会开启备份或接受来源、目标路径；须确认和幂等键。',
          'backup', 'admin', {'source': {'type': 'string', 'enum': ['usb', 'sd']}}, write=True),
    _spec('pause_backup_task', '暂停原厂备份任务',
          '暂停当前管理员目录中的既有运行任务；须当前任务版本、确认和幂等键，不创建、恢复任务或读取用户文件。',
          'backup', 'admin', {'task_id': _ID, 'expected_version': {'type': 'string',
                              'pattern': '^backup_[0-9a-f]{32}$'}}, write=True),
    _spec('cancel_backup_task', '取消原厂备份任务',
          '取消当前管理员目录中的既有运行或等待任务；须当前任务版本、确认和幂等键，不删除备份文件，原厂确认不等于线程已退出。',
          'backup', 'admin', {'task_id': _ID, 'expected_version': {'type': 'string',
                              'pattern': '^backup_[0-9a-f]{32}$'}}, write=True, destructive=True),
    _spec('list_nas_users', '查询 NAS 用户目录',
          '管理员显式读取本机用户 ID、名称与昵称；不返回账号、邮箱、头像、权限原文、凭据或会话。',
          'system', 'admin'),
    _spec('get_nas_name', '读取 NAS 自定义名称',
          '显式查询原厂设备自定义名称，仅返回名称，不返回设备序列号或原厂原文。',
          'system', 'admin'),
    _spec('set_nas_name', '修改 NAS 自定义名称',
          '管理员修改原厂设备名称，原厂会通知云端并更新本地 dev_info；须确认和幂等键，回执不代表云端传播完成。',
          'system', 'admin', {'name': {'type': 'string', 'minLength': 1,
                                       'maxLength': 63}}, write=True),
    _spec('get_nas_ai_settings', '读取原厂 AI 识别开关',
          '管理员显式读取原厂 AI 识别开关，未知值保持 null；不读取照片、人脸或媒体文件。',
          'system', 'admin'),
    _spec('set_nas_ai_enabled', '修改原厂 AI 识别开关',
          '管理员修改 ai_config.ai_switch 并通知原厂识别线程；可能启动或停止原厂后台识别，须确认和幂等键；不提供文件内容读取。',
          'system', 'admin', {'enabled': {'type': 'boolean'}}, write=True),
    _spec('get_movie_library_scan_status', '查询影视库扫描状态',
          '显式查询指定既有影视库的原厂扫描状态与真实计数；不启动扫描、不输出媒体路径，不从计数推断完成百分比。',
          'movie', 'readwrite', {'library_id': _ID}),
    _spec('delete_album', '删除普通自建相册',
          '仅删除当前原厂管理员目录中普通 custom 相册及其索引关系；保留原厂异步清理语义，回执不代表清理完成，不允许共享或保留相册。',
          'albums', 'readwrite', {'album_id': {'type': 'string', 'minLength': 1,
                                             'maxLength': 10,
                                             'pattern': '^[1-9][0-9]{0,9}$'}},
          write=True, destructive=True),
)
