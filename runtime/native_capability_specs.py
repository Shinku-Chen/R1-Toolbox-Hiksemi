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


def _application(identifier, name, label, program, data, summary, *, background):
    """One statically audited module identity and its native delegation effects.

    These are resource choices, never an installed inventory or package URL.
    The executor must still prove both current catalogs and exact Key access.
    """
    return {'application_id': identifier, 'module_name': name, 'label': label,
            'source': 0, 'module_type': 0, 'native_type': 'extension',
            'resource_effects': {'network_download': True,
                'may_start_background_process': background,
                'program_storage': program, 'application_data': data,
                'file_tool_sandbox_applies': False, 'native_application_delegation': True},
            'effect_summary': summary}


# V6.3.3 Build_260907 emhistor registrations and install/start callbacks were
# individually audited. Adding an entry must never expand an existing Key's
# application_ids; a missing legacy resource field is fixed to ID34 alone.
APPLICATION_INSTALL_SPECS = {
    7: _application(7, 'netdisk_bd', '百度网盘', 'vendor_selected_netdisk_bd_directory',
        'admin_Downloads_and_existing_native_netdisk_state',
        '原厂安装器可能联网下载百度网盘模块，写入独立程序目录、迁移原厂已有 p2p.conf，'
        '初始化数据库及配置，并启动后台下载进程；后续原生网盘任务可使用管理员 Downloads，'
        '也可能加载已有任务。原厂监督脚本可能重启同名进程，并在恢复分支清理宿主页缓存。'
        'Toolbox 文件授权不限制原生应用的数据访问范围。', background=True),
    8: _application(8, 'contact_backup', '通讯录备份', 'native_module_metadata',
        'native_contact_backup_module_state',
        '原厂安装流程可能联网查询并写入通讯录备份模块状态；本固件安装回调只记录用户标识，'
        '启动回调直接返回。受理不表示已备份通讯录，也不建立后台进程已启动的证明；'
        '后续原厂功能仍使用其自身权限和数据范围。', background=False),
    34: _application(34, 'netdisk_bd2', '百度网盘 v2', 'vendor_selected_netdisk_bd2_directory',
        'current_owner_Downloads_and_native_application_state',
        '原厂安装器可能联网下载百度网盘 v2 模块，写入独立程序目录和当前管理员应用状态，'
        '并启动后台进程；后续原生网盘任务可使用管理员 Downloads。'
        'Toolbox 文件授权不限制原生应用的数据访问范围。', background=True),
    35: _application(35, '66_print', '66 打印', 'native_module_metadata',
        'native_66_print_module_state',
        '原厂安装流程可能联网查询并写入 66 打印模块状态；本固件安装回调只记录用户标识，'
        '启动回调直接返回。受理不表示打印服务已启动或已提交打印任务；'
        '后续原厂功能仍使用其自身权限和数据范围。', background=False),
}
APPLICATION_INSTALL_SPECS[7]['resource_effects'].update(
    may_restart_native_processes=True, may_adjust_host_cache=True, may_load_existing_tasks=True)


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
    _spec('list_native_modules', '查询原厂模块库存',
          '显式查询原厂可用模块和已安装模块两个独立目录；原厂可能联系厂商更新支持状态，因此需要读写权限。只返回有限模块身份、版本及原始状态，不推断运行状态，不授予安装或卸载权限。',
          'apps', 'readwrite'),
    _spec('plan_native_application_install', '预览原厂普通应用安装',
          '管理员预览本键 application_ids 授权的已审计未安装应用；核对当前应用和模块目录并返回版本及该应用的网络、后台进程、安装目录和用户数据影响。原厂查询可能更新支持状态；不执行安装，不接收包地址或脚本。',
          'apps', 'admin', {'application_id': {'type': 'integer', 'minimum': 1, 'maximum': 2 ** 31 - 1}}),
    _spec('install_native_application', '安装已审计原厂普通应用',
          '仅新增安装本键 application_ids 授权且预览允许的固定普通应用，需当前预览版本、确认和本键幂等标识；重新核对未安装成员关系及精确包身份，影响以预览为准；受理不等于完成。不更新、卸载或管理核心服务，不接收 URL、路径或脚本。',
          'apps', 'admin', {'application_id': {'type': 'integer', 'minimum': 1, 'maximum': 2 ** 31 - 1},
                            'expected_version': {'type': 'string', 'pattern': '^app_install_[0-9a-f]{32}$'}}, write=True),
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
