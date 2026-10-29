// API management UI against the real WebConfig fixture and fixed API responses.
// All production panel assets are built into a temporary folder, never ui/web.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'r1-api-browser-'));
const fixtureDir = path.join(temporary, 'r1-web-browser-api');
const buildDir = path.join(temporary, 'panel');
fs.mkdirSync(fixtureDir);
const python = process.env.PYTHON_BIN || 'python3';
const candidates = [process.env.CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
const chromeBin = candidates.find(candidate => candidate && fs.existsSync(candidate));
assert.ok(chromeBin, '未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件');
const build = spawnSync(python, ['scripts/build-panel.py', '--output', buildDir], { cwd: root, encoding: 'utf8' });
assert.equal(build.status, 0, '临时面板构建失败：' + (build.stderr || build.stdout));

// Share the complete hardware-free real fixture without changing the larger
// regression test. Its actual controllers and HTTP server remain unmocked.
const source = fs.readFileSync(path.join(__dirname, 'check_web_config.cjs'), 'utf8');
const fixtureMatch = source.match(/const pythonFixture = String\.raw`([\s\S]*?)`;/);
assert.ok(fixtureMatch, '无法找到现有真实网页 fixture');
const pythonFixture = fixtureMatch[1].replaceAll("asset_dir=repo / 'ui/web'", 'asset_dir=Path(sys.argv[3])');

(async () => {
  const child = spawn(python, ['-u', '-c', pythonFixture, root, fixtureDir, buildDir], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = '', browser, readyResolve, readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const timer = setTimeout(() => readyReject(new Error('本地网页 fixture 启动超时')), 20000);
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-6000); });
  child.on('error', readyReject);
  child.on('exit', code => { if (code) readyReject(new Error('网页 fixture 退出：' + code + '\n' + stderr)); });
  readline.createInterface({ input: child.stdout }).on('line', line => {
    try { const value = JSON.parse(line); if (value.ready) { clearTimeout(timer); readyResolve(value); } } catch (_) { /* fixture log */ }
  });
  const checks = [], pageErrors = [];
  const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
  const secret = 'r1-fixture-secret-ONLY-ONCE-<img src=x>';
  const rotatedSecret = 'r1-fixture-rotated-ONLY-ONCE-<svg onload=x>';
  const unsafeName = '<img src=x onerror="window.apiNameInjected=1">客厅 AI';
  const calls = [];
  let reads = 0, contractReads = 0, projectReads = 0, serviceReads = 0, failServiceRead = true, failServiceWrite = false, releaseServiceWrite = null, holdServiceWrite = false, failContracts = false, failCreate = false, failRead = false, failManage = false;
  const fixtureTools = [
    ['get_nas_status', 'read', 'readonly'], ['get_media_status', 'media', 'readwrite'],
    ['set_fan_policy', 'fan', 'readwrite'], ['release_fan', 'fan', 'readwrite'],
    ['set_light_policy', 'light', 'readwrite'], ['set_rgb', 'light', 'readwrite'],
    ['set_screen', 'screen', 'readwrite'], ['update_screen_settings', 'screen', 'readwrite'],
    ['request_power', 'power', 'admin'], ['cancel_power', 'power', 'admin'],
    ['list_file_roots', 'files', 'readonly'], ['list_docker_projects', 'docker', 'readonly'],
    ['get_docker_project_status', 'docker', 'readonly'], ['get_docker_project_summary', 'docker', 'readonly'],
    ['control_docker_project', 'docker', 'admin'], ['plan_docker_project_update', 'docker', 'admin'],
    ['execute_docker_project_update', 'docker', 'admin'], ['get_docker_project_task', 'docker', 'readonly'],
    ['get_app_status', 'apps', 'readonly'], ['get_download_status', 'downloads', 'readonly'],
    ['get_movie_status', 'movie', 'readonly'], ['get_album_status', 'albums', 'readonly'], ['get_share_status', 'sharing', 'readonly'],
  ].map(([name, scope, min_role]) => ({ name, title: name, scope, min_role }));
  const projectId = 'project_' + '3'.repeat(24), candidateId = 'candidate_' + '4'.repeat(24);
  const unsafeProject = '<svg onload="window.apiProjectInjected=1">片库';
  const state = {
    ok: true, enabled: false, storage_ok: true, keys: [],
    role_catalog: [
      { id: 'readonly', label: '只读', description: '读取已授权功能的数据；不会调用写工具。' },
      { id: 'readwrite', label: '读写', description: '允许已授权功能的日常控制。' },
      { id: 'admin', label: '管理员', description: '允许已授权的管理工具；不会自动勾选其他功能。' },
    ],
    scope_catalog: [
      { id: 'read', label: '读取状态', description: '读取监控与设备信息', min_role: 'readonly', implemented: true },
      { id: 'fan', label: '风扇策略', description: '设置风扇策略', min_role: 'readwrite', implemented: true },
      { id: 'light', label: '机身灯光', description: '修改灯光', min_role: 'readwrite', implemented: true },
      { id: 'screen', label: '小屏显示', description: '屏幕与亮度', min_role: 'readwrite', implemented: true },
      { id: 'power', label: '电源操作', description: '关机或重启', min_role: 'admin', implemented: true },
      { id: 'media', label: '媒体状态', description: '查询原厂媒体任务', min_role: 'readwrite', implemented: true },
      ...[['files', '文件管理'], ['docker', 'Docker 部署'], ['apps', '应用市场'], ['downloads', '下载器'],
        ['movie', '影视墙'], ['albums', '相册'], ['sharing', '分享'], ['backup', '备份'], ['system', '系统管理']]
        .map(([id, label]) => ({ id, label, description: '分领域授权', min_role: 'readonly', implemented: ['files', 'docker', 'apps', 'downloads', 'movie', 'albums', 'sharing'].includes(id) })),
      { id: 'unrecognized', label: '未知功能不可伪造', implemented: true },
    ],
    file_root_catalog: { available: true, status: 'ok', reason: null, roots: [
      { id: 'root_' + '1'.repeat(24), label: '<img src=x onerror="window.apiRootInjected=1">视频资源', mount: '/drives/space_1', read_only: false },
      { id: 'root_' + '2'.repeat(24), label: '备份磁盘', mount: '/drives/space_2', read_only: true },
    ] },
    tools: fixtureTools,
    docker_project_catalog: { ok: true, projects: [] },
    audit: [{ timestamp: 1793240000, key_id: 'fixture-key-1', key_name: unsafeName,
      operation: 'get_status', peer: '192.168.31.100', outcome: 'ok', duration_ms: 12 }],
    endpoints: { mcp: '/mcp', status: '/api/integration/status', action: '/api/integration/action', capabilities: '/api/integration/capabilities' },
    protocol_versions: ['2025-03-26'],
  };
  try {
    const fixture = await ready, origin = fixture.primary.origin;
    browser = await chromium.launch({ executablePath: chromeBin, headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, locale: 'zh-CN' });
    await context.addInitScript(() => {
      window.__apiStorageWrites = []; window.__apiCopied = [];
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) { window.__apiStorageWrites.push([String(key), String(value)]); return set.call(this, key, value); };
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.__apiCopied.push(text); } } });
    });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin && url.protocol !== 'data:') return route.abort('blockedbyclient');
      const reply = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/services' && request.method() === 'GET') {
        serviceReads += 1;
        if (failServiceRead === 'pair') return reply(403, { error: 'need_pairing' });
        return failServiceRead ? reply(503, { error: 'fixture：服务状态不可读' }) : reply(200, { ok: true, mcp: { enabled: state.enabled } });
      }
      if (url.pathname === '/api/services/mcp' && request.method() === 'POST') {
        const body = request.postDataJSON(); calls.push({ path: url.pathname, body, headers: request.headers() });
        if (holdServiceWrite) await new Promise(resolve => { releaseServiceWrite = resolve; });
        if (failServiceWrite) return reply(503, { error: 'fixture：MCP 开关保存失败' });
        state.enabled = body.enabled;
        return reply(200, { ok: true, services: { mcp: { enabled: state.enabled } } });
      }
      if (url.pathname.startsWith('/api/docker/projects')) {
        if (request.method() === 'GET') {
          projectReads += 1;
          return reply(200, url.pathname.endsWith('/catalog')
            ? { ok: true, available: true, candidates: [{ id: candidateId, label: unsafeProject, available: true }] }
            : state.docker_project_catalog);
        }
        const body = request.postDataJSON(); calls.push({ path: url.pathname, body, headers: request.headers() });
        if (url.pathname.endsWith('/register')) {
          assert.deepEqual(Object.keys(body).sort(), ['candidate_id', 'label']);
          assert.equal(body.candidate_id, candidateId);
          state.docker_project_catalog.projects.push({ id: projectId, label: body.label, operations: ['inspect'], available: true, revoked: false, reason: null });
        } else if (url.pathname.endsWith('/update')) {
          assert.deepEqual(Object.keys(body).sort(), ['label', 'project_id']);
          state.docker_project_catalog.projects.find(row => row.id === body.project_id).label = body.label;
        } else if (url.pathname.endsWith('/revoke')) {
          assert.deepEqual(Object.keys(body), ['project_id']);
          Object.assign(state.docker_project_catalog.projects.find(row => row.id === body.project_id), { revoked: true, available: false, reason: 'fixture：登记已撤销' });
        } else throw new Error('unexpected project mutation');
        return reply(200, { ok: true });
      }
      if (url.pathname !== '/api/access' && !url.pathname.startsWith('/api/access/')) return route.continue();
      if (url.pathname === '/api/access/contracts') {
        contractReads += 1;
        if (failContracts) return reply(503, { error: 'fixture：接口定义读取失败' });
        return route.continue();
      }
      if (request.method() === 'GET') {
        reads += 1;
        if (failRead) return reply(503, { error: 'fixture：配置读取失败' });
        return reply(200, state);
      }
      const body = request.postDataJSON();
      calls.push({ path: url.pathname, body, headers: request.headers() });
      if (url.pathname === '/api/access/settings') { state.enabled = body.enabled; return reply(200, { ok: true }); }
      if (url.pathname === '/api/access/keys') {
        if (failCreate) return reply(429, { error: 'fixture：密钥创建失败' });
        const key = { id: 'fixture-key-' + (state.keys.length + 1), ...body, policy_version: Object.hasOwn(body, 'tool_grants') ? 3 : 2, created_at: 1793240000, enabled: true, disabled: false,
          expires_at: body.expires_days === 0 ? null : 1801016000, revoked_at: null, last_used_at: null, status: 'active' };
        state.keys.push(key); return reply(200, { ok: true, key, token: secret });
      }
      if (url.pathname === '/api/access/keys/revoke') {
        const key = state.keys.find(item => item.id === body.id);
        assert.ok(key, '撤销已有 key'); key.status = 'revoked'; key.revoked_at = 1793240100;
        return reply(200, { ok: true });
      }
      if (['/api/access/keys/update', '/api/access/keys/enable', '/api/access/keys/rotate'].includes(url.pathname)) {
        if (failManage) return reply(503, { error: 'fixture：密钥管理失败' });
        const key = state.keys.find(item => item.id === body.id);
        assert.ok(key && key.status !== 'revoked', '只管理未撤销 key');
        if (url.pathname.endsWith('/update')) {
          Object.assign(key, body);
          if (Object.hasOwn(body, 'tool_grants')) key.policy_version = 3;
          else if (Object.hasOwn(body, 'role') && key.policy_version !== 3) key.policy_version = 2;
          if (Object.hasOwn(body, 'expires_days')) key.expires_at = body.expires_days === 0 ? null : 1802222000;
          return reply(200, { ok: true, key });
        }
        if (url.pathname.endsWith('/enable')) {
          key.enabled = body.enabled; key.disabled = !body.enabled; key.status = body.enabled ? 'active' : 'disabled';
          return reply(200, { ok: true, key });
        }
        return reply(200, { ok: true, key, token: rotatedSecret });
      }
      return reply(404, { error: 'unknown fixture route' });
    });
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(origin + '/#/settings');
    await page.waitForSelector('#set-r1-api');
    check('设置中有 API 管理入口', await page.locator('#setNav [data-g="r1-api"]').count() === 1);
    check('构建设置不自动获取密钥列表', reads === 0);
    const home = page.locator('#set-r1-api');
    await home.locator('[data-ref="error"]').filter({ hasText: '服务状态不可读' }).waitFor();
    check('服务状态读取失败不猜测开启状态', await home.locator('[data-ref="enabled"]').isDisabled() && await home.locator('[data-ref="retry"]').isVisible());
    failServiceRead = 'pair';
    await home.locator('[data-ref="retry"]').click();
    await home.locator('[data-ref="error"]').filter({ hasText: '需要先解锁网页管理' }).waitFor();
    check('兼容配对未授权只就地提示且不阻断监控页面', await page.locator('#blocked').count() === 0 && await page.locator('.fullmask').count() === 0 && await home.locator('[data-ref="enabled"]').isDisabled());
    failServiceRead = false;
    await home.locator('[data-ref="retry"]').click();
    await home.locator('[data-ref="serviceStatus"]').filter({ hasText: '已关闭' }).waitFor();
    check('设置卡片具有直接开关和三个独立入口', await home.locator('[role="switch"]').count() === 1 && await home.locator('.r1-api-entrypoints button').allTextContents().then(labels => JSON.stringify(labels) === '["添加 Token","Token 管理","Token 操作"]'));
    failServiceWrite = true;
    await home.locator('[data-ref="enabled"]').click();
    await home.locator('[data-ref="error"]').filter({ hasText: 'MCP 开关保存失败' }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#set-r1-api [data-ref="enabled"]').disabled);
    check('开关失败恢复服务端状态并且不弹窗', !(await home.locator('[data-ref="enabled"]').isChecked()) && await page.locator('.fullmask').count() === 0 && calls.length === 1 && serviceReads === 4);
    failServiceWrite = false; holdServiceWrite = true;
    await home.locator('[data-ref="enabled"]').click();
    await page.waitForFunction(() => document.querySelector('#set-r1-api [data-ref="enabled"]').disabled);
    await home.locator('[data-ref="enabled"]').evaluate(input => input.click());
    check('开关保存期间禁止重复提交且无确认弹窗', calls.length === 2 && await page.locator('.fullmask').count() === 0);
    releaseServiceWrite(); holdServiceWrite = false;
    await home.locator('[data-ref="serviceStatus"]').filter({ hasText: '已开启' }).waitFor();
    check('开关直接保存JSON与CSRF请求头', calls.at(-1).path === '/api/services/mcp' && calls.at(-1).body.enabled === true && calls.at(-1).headers['x-fnm-request'] === '1' && calls.at(-1).headers['content-type'] === 'application/json');
    await home.locator('[data-ref="enabled"]').click();
    await home.locator('[data-ref="serviceStatus"]').filter({ hasText: '已关闭' }).waitFor();
    const serviceWrites = calls.length;
    await home.locator('[data-ref="add"]').click();
    const manager = page.locator('#r1ApiManager');
    const view = async name => { await manager.locator('[data-api-nav="' + name + '"]').click(); await manager.locator('[data-api-page="' + name + '"]:not([hidden])').waitFor(); };
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('添加页独立展示且不混入管理和记录', await manager.locator('[data-api-page="create"]').isVisible() && await manager.locator('[data-ref="keys"]').isHidden() && await manager.locator('[data-ref="audit"]').isHidden() && await manager.locator('[data-ref="serviceStatus"]').innerText() === '已关闭');
    check('默认仅勾选读取状态', JSON.stringify(await manager.locator('[data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    check('默认只读角色与三级选项', await manager.locator('select[data-ref="role"]').inputValue() === 'readonly' && JSON.stringify(await manager.locator('select[data-ref="role"] option').evaluateAll(options => options.map(option => option.value))) === '["readonly","readwrite","admin"]');
    check('十五项功能只显示已知领域', await manager.locator('[data-ref="scopes"] input').count() === 15 && await manager.locator('[data-ref="scopes"] input[value="unrecognized"]').count() === 0);
    check('文件边界明确Users且按真实工具能力授权', (await manager.locator('[data-ref="fileRoots"]').innerText()).includes('/drives/*/Users/') && (await manager.locator('[data-ref="scopes"] label').filter({ hasText: 'Docker 部署' }).innerText()).includes('已接入'));
    check('具体工具按真实目录展示且默认仅状态读取', await manager.locator('[data-ref="toolOptions"] input').count() === fixtureTools.length && JSON.stringify(await manager.locator('[data-ref="toolOptions"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["get_nas_status"]');
    check('角色不足或未选功能的工具不可选', await manager.locator('[data-ref="toolOptions"] input[value="set_fan_policy"]').isDisabled() && await manager.locator('[data-ref="toolOptions"] input[value="list_file_roots"]').isDisabled());
    await manager.locator('[data-ref="scopes"] input[value="apps"]').check();
    check('新增只读业务工具按功能启用而不自动勾选', !(await manager.locator('[data-ref="toolOptions"] input[value="get_app_status"]').isDisabled()) && !(await manager.locator('[data-ref="toolOptions"] input[value="get_app_status"]').isChecked()));
    await manager.locator('[data-ref="toolOptions"] input[value="get_app_status"]').check();
    await manager.locator('[data-ref="scopes"] input[value="apps"]').uncheck();
    check('取消业务功能同时移除对应工具授权', !(await manager.locator('[data-ref="toolOptions"] input[value="get_app_status"]').isChecked()) && await manager.locator('[data-ref="toolOptions"] input[value="get_app_status"]').isDisabled());
    check('项目候选和登记不自动轮询', projectReads === 0);
    check('未选择文件功能时隐藏范围', await manager.locator('[data-ref="fileRoots"]').isHidden());
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    check('管理员角色不自动新增功能授权', JSON.stringify(await manager.locator('[data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    check('默认有效期 90 天', await manager.locator('[data-ref="expiry"]').inputValue() === '90');
    await view('operations');
    check('Token操作页隔离表单列表且没有机器调用代理', await manager.locator('[data-ref="form"]').isHidden() && await manager.locator('[data-ref="keys"]').isHidden() && await manager.locator('[data-ref="audit"]').isVisible() && await manager.locator('[data-ref="capabilities"] .r1-api-capability').count() === fixtureTools.length && await manager.locator('[data-api-page="operations"] input:not([readonly]), [data-api-page="operations"] button[data-call]').count() === 0);
    check('MCP 地址与当前设备一致', await manager.locator('[data-ref="mcp"]').inputValue() === origin + '/mcp');
    check('能力目录地址与当前设备一致', await manager.locator('[data-ref="restCapabilities"]').inputValue() === origin + '/api/integration/capabilities');
    check('安全调用记录可见', (await manager.locator('[data-ref="audit"]').innerText()).includes('192.168.31.100') && (await manager.locator('[data-ref="audit"]').innerText()).includes('get_status'));

    check('接口定义按需读取且不混入当前密钥工具', contractReads === 0);
    await page.evaluate(async code => {
      const response = await fetch('/api/pair', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-FNM-Request': '1' },
        body: JSON.stringify({ code }) });
      if (!response.ok) throw new Error('fixture owner pairing failed');
    }, fixture.primary.code);
    await manager.locator('[data-ref="contracts"] summary').click();
    failContracts = true;
    await manager.locator('[data-ref="loadContracts"]').click();
    await manager.locator('[data-ref="contractStatus"]').filter({ hasText: '读取失败' }).waitFor();
    check('接口定义失败可重试且不保留旧JSON', await manager.locator('[data-ref="contractJson"]').isHidden() && !(await manager.locator('[data-ref="loadContracts"]').isDisabled()));
    failContracts = false;
    await manager.locator('[data-ref="loadContracts"]').click();
    await page.waitForFunction(() => document.querySelectorAll('#r1ApiManager .r1-api-contract').length === 20);
    const definition = JSON.parse(await manager.locator('[data-ref="contractDefinition"]').inputValue());
    check('真实服务返回20项定义并区分7项已接入与13项待接入', definition.status === 'definitions_only' && definition.execution_allowed === false && definition.tools.length === 20 && definition.tools.filter(tool => tool.implemented === true).length === 7 && definition.tools.filter(tool => tool.implemented === false).length === 13 && definition.tools.every(tool => tool.inputSchema && tool.outputSchema));
    check('接口显示三级权限且不改变表单授权', (await manager.locator('[data-ref="contractList"]').innerText()).includes('管理员') && (await manager.locator('[data-ref="contractList"]').innerText()).includes('读写') && (await manager.locator('[data-ref="contractList"]').innerText()).includes('只读') && await manager.locator('select[data-ref="role"]').inputValue() === 'readonly' && calls.length === serviceWrites);
    await page.setViewportSize({ width: 320, height: 960 });
    check('320宽度接口定义无需横向滚动', await manager.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    await page.setViewportSize({ width: 1280, height: 1000 });
    await manager.locator('[data-ref="contracts"] summary').click();

    await view('create');
    await manager.locator('input[data-ref="name"]').fill(unsafeName);
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();
    check('低角色保留功能选择但说明工具不可调用', (await manager.locator('[data-ref="roleStatus"]').innerText()).includes('当前角色不可调用') && await manager.locator('[data-ref="scopes"] input[value="fan"]').isChecked());
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    await manager.locator('[data-ref="toolOptions"] input[value="set_fan_policy"]').check();
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    check('降级角色清除不允许的工具', !(await manager.locator('[data-ref="toolOptions"] input[value="set_fan_policy"]').isChecked()) && await manager.locator('[data-ref="toolOptions"] input[value="set_fan_policy"]').isDisabled());
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    check('提高角色不重新自动勾选工具', !(await manager.locator('[data-ref="toolOptions"] input[value="set_fan_policy"]').isChecked()));
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    await manager.locator('textarea[data-ref="ips"]').fill('192.168.31.100\nfd00::2');
    await manager.locator('[data-ref="create"]').click();
    await page.locator('.fullmask [data-yes]').click();
    const secretDialog = page.locator('#r1ApiSecret');
    await secretDialog.waitFor();
    const secretField = await secretDialog.locator('[data-ref="token"]').elementHandle();
    check('密钥只在创建对话框中显示', await secretDialog.locator('[data-ref="token"]').inputValue() === secret);
    const config = JSON.parse(await secretDialog.locator('[data-ref="config"]').inputValue());
    check('MCP 配置包含对应 URL 和 Bearer 请求头', config.mcpServers.toolbox.url === origin + '/mcp' && config.mcpServers.toolbox.headers.Authorization === 'Bearer ' + secret);
    const created = calls.find(call => call.path === '/api/access/keys');
    check('创建显式policy3且仅授权默认状态工具及空项目文件范围', created.body.role === 'readonly' && JSON.stringify(created.body.file_roots) === '[]' && JSON.stringify(created.body.docker_projects) === '[]' && JSON.stringify(created.body.tool_grants) === '["get_nas_status"]' && JSON.stringify(created.body.scopes) === '["read","fan"]' && created.body.allowed_ips.length === 2 && created.body.expires_days === 90);
    await secretDialog.locator('[data-ref="copyConfig"]').click();
    check('复制 MCP 配置', await page.evaluate(() => window.__apiCopied.some(text => JSON.parse(text).mcpServers.toolbox.url.endsWith('/mcp'))));
    await page.evaluate(() => { Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }); document.execCommand = () => false; });
    await secretDialog.locator('[data-ref="copyToken"]').click();
    check('HTTP 复制失败时保留可手动复制文本', (await secretDialog.locator('[data-ref="copyStatus"]').innerText()).includes('请手动复制') && await secretDialog.locator('[data-ref="token"]').evaluate(input => input.selectionStart === 0 && input.selectionEnd === input.value.length));
    await secretDialog.locator('[data-ref="close"]').click();
    check('关闭后清除临时密钥字段', await secretField.inputValue() === '' && await page.locator('#r1ApiSecret').count() === 0);
    await manager.locator('.r1-api-item').waitFor();
    check('名称作为文本展示且不注入 HTML', await manager.locator('.r1-api-item img').count() === 0 && (await manager.locator('.r1-api-item').innerText()).includes(unsafeName));
    check('未知最后使用时间不显示假零值', (await manager.locator('.r1-api-item [data-ref="used"]').innerText()) === '尚无记录');
    const beforePoll = reads;
    await page.waitForTimeout(11000);
    check('定时状态轮询不重复获取访问密钥', reads === beforePoll);
    await manager.locator('[data-ref="refresh"]').click();
    await page.waitForFunction(expected => window.document.querySelector('#r1ApiManager').getAttribute('aria-busy') === 'false', null);
    check('按需刷新不再次显示密钥', reads === beforePoll + 1 && await page.locator('#r1ApiSecret').count() === 0);
    check('密钥未写入浏览器存储', !(await page.evaluate(() => JSON.stringify(window.__apiStorageWrites))).includes(secret));
    check('安全元数据列表不包含密钥', !(await manager.innerText()).includes(secret));

    await manager.locator('.r1-api-item [data-ref="edit"]').click();
    check('编辑加载原有角色和权限并默认保持有效期', await manager.locator('select[data-ref="role"]').inputValue() === 'readonly'
      && await manager.locator('[data-ref="expiry"]').inputValue() === 'keep'
      && await manager.locator('[data-ref="scopes"] input[value="fan"]').isChecked());
    await manager.locator('textarea[data-ref="ips"]').fill('192.168.31.0/24\nfd00::/64\n192.168.32.0/255.255.255.0');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const metadataEdit = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('IP编辑接受CIDR和点分掩码且不重设有效期或扩大权限', JSON.stringify(metadataEdit.allowed_ips) === JSON.stringify(['192.168.31.0/24', 'fd00::/64', '192.168.32.0/255.255.255.0'])
      && !Object.hasOwn(metadataEdit, 'expires_days') && !Object.hasOwn(metadataEdit, 'role') && !Object.hasOwn(metadataEdit, 'scopes') && !Object.hasOwn(metadataEdit, 'file_roots') && !Object.hasOwn(metadataEdit, 'tool_grants') && !Object.hasOwn(metadataEdit, 'docker_projects'));
    await manager.locator('.r1-api-item [data-ref="edit"]').click();
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    await manager.locator('[data-ref="scopes"] input[value="screen"]').check();
    await manager.locator('[data-ref="expiry"]').selectOption('7');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const permissionEdit = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('角色功能有效期分别提交且不重写未编辑工具和资源', permissionEdit.role === 'readwrite'
      && JSON.stringify(permissionEdit.scopes) === '["read","fan","screen"]' && permissionEdit.expires_days === 7 && !Object.hasOwn(permissionEdit, 'file_roots') && !Object.hasOwn(permissionEdit, 'tool_grants') && !Object.hasOwn(permissionEdit, 'docker_projects'));
    await manager.locator('.r1-api-item [data-ref="toggle"]').click();
    await manager.locator('.r1-api-item [data-ref="status"]').filter({ hasText: '已停用' }).waitFor();
    check('单key停用不撤销且可以再启用', calls.at(-1).path === '/api/access/keys/enable' && calls.at(-1).body.enabled === false
      && await manager.locator('.r1-api-item [data-ref="toggle"]').innerText() === '启用');
    await manager.locator('.r1-api-item [data-ref="toggle"]').click();
    await manager.locator('.r1-api-item [data-ref="status"]').filter({ hasText: '有效' }).waitFor();
    failManage = true;
    await manager.locator('.r1-api-item [data-ref="toggle"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '密钥管理失败' }).waitFor();
    check('停用失败显示原因且不误报停用', await manager.locator('.r1-api-item [data-ref="status"]').innerText() === '有效');
    failManage = false;
    await manager.locator('.r1-api-item [data-ref="rotate"]').click();
    check('轮换前明确旧Token立即失效', (await page.locator('.fullmask').last().innerText()).includes('旧 Token 将立即失效'));
    await page.locator('.fullmask [data-yes]').click();
    await page.locator('#r1ApiSecret').waitFor();
    const rotatedField = await page.locator('#r1ApiSecret [data-ref="token"]').elementHandle();
    check('轮换仅一次显示新Token而不返回旧Token', await rotatedField.inputValue() === rotatedSecret
      && calls.at(-1).path === '/api/access/keys/rotate');
    await page.keyboard.press('Escape');
    check('轮换窗口关闭后清除Token且不写浏览器存储', await rotatedField.inputValue() === ''
      && !(await manager.innerText()).includes(rotatedSecret)
      && !(await page.evaluate(() => JSON.stringify(window.__apiStorageWrites))).includes(rotatedSecret));
    await view('create');
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();

    await view('manage');
    await manager.locator('.r1-api-item [data-ref="revoke"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await manager.locator('.r1-api-item [data-ref="status"]').filter({ hasText: '已撤销' }).waitFor();
    check('撤销后刷新安全元数据', calls.some(call => call.path === '/api/access/keys/revoke') && await manager.locator('.r1-api-item [data-ref="revoke"]').isHidden());
    check('已撤销Token不能恢复编辑启用或轮换', await manager.locator('.r1-api-item [data-ref="edit"]').isHidden()
      && await manager.locator('.r1-api-item [data-ref="toggle"]').isHidden() && await manager.locator('.r1-api-item [data-ref="rotate"]').isHidden());

    failCreate = true;
    await view('create');
    await manager.locator('input[data-ref="name"]').fill('失败测试');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '密钥创建失败' }).waitFor();
    check('创建失败可见且不展示旧密钥', await page.locator('#r1ApiSecret').count() === 0 && !(await manager.locator('[data-ref="create"]').isDisabled()));
    failCreate = false; failRead = true;
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '配置读取失败' }).waitFor();
    check('读取失败反馈具体原因', !(await manager.locator('[data-ref="refresh"]').isDisabled()));
    failRead = false;

    await manager.locator('input[data-ref="name"]').fill('文件范围 AI');
    await manager.locator('[data-ref="scopes"] input[value="files"]').check();
    check('文件功能显示真实根与Users授权边界', await manager.locator('[data-ref="fileRootOptions"] .r1-api-file-root').count() === 2 && (await manager.locator('[data-ref="fileRoots"]').innerText()).includes('/drives/*/Users/'));
    const writesBeforeFiles = calls.length;
    await manager.locator('[data-ref="create"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '请至少勾选一个' }).waitFor();
    check('文件权限不能省略真实范围', calls.length === writesBeforeFiles && await page.locator('#r1ApiSecret').count() === 0);
    const writableRoot = manager.locator('.r1-api-file-root').first(), readonlyRoot = manager.locator('.r1-api-file-root').last();
    await writableRoot.locator('[data-root-check]').check();
    const initialFileGrant = await writableRoot.evaluate(element => ({ access: element.querySelector('[data-root-access]').value,
      path: element.querySelector('[data-root-path]').value, writeDisabled: element.querySelector('option[value="readwrite"]').disabled,
      role: document.querySelector('#r1ApiManager [data-ref="role"]').value }));
    check('默认文件范围只读且子目录为空：' + JSON.stringify(initialFileGrant), initialFileGrant.access === 'readonly' && initialFileGrant.path === '' && initialFileGrant.writeDisabled);
    check('子目录容纳1024个Unicode标量且根名称不注入 HTML', await writableRoot.locator('[data-root-path]').getAttribute('maxlength') === '2048' && await manager.locator('[data-ref="fileRootOptions"] img').count() === 0 && (await writableRoot.innerText()).includes('视频资源'));
    await writableRoot.locator('[data-root-access]').evaluate(select => { select.value = 'readwrite'; });
    await manager.locator('[data-ref="create"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '只读角色或只读挂载' }).waitFor();
    check('篡改禁用选项也不能授予只读角色写范围', calls.length === writesBeforeFiles);
    await writableRoot.locator('[data-root-access]').selectOption('readonly');
    for (const invalidPath of ['../私有', '/drives', '电影//2026', '电影/./2026', '电影/../2026', '电影\\2026', 'C:私有', '电影/\u0000', '电影/\u0001']) {
      await writableRoot.locator('[data-root-path]').fill(invalidPath);
      await manager.locator('[data-ref="create"]').click();
      await manager.locator('[data-ref="error"]').filter({ hasText: '相对 POSIX 路径' }).waitFor();
    }
    await writableRoot.locator('[data-root-path]').evaluate(input => { input.value = 'a'.repeat(1025); });
    await manager.locator('[data-ref="create"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '相对 POSIX 路径' }).waitFor();
    check('越界及非规范子目录均在本地拒绝且不提交', calls.length === writesBeforeFiles);
    await writableRoot.locator('[data-root-path]').fill('电影/2026');
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    await writableRoot.locator('[data-root-access]').selectOption('readwrite');
    await readonlyRoot.locator('[data-root-check]').check();
    check('只读挂载在读写角色下仍不能写', await readonlyRoot.locator('option[value="readwrite"]').evaluate(option => option.disabled) && await readonlyRoot.locator('[data-root-access]').inputValue() === 'readonly' && (await readonlyRoot.innerText()).includes('只读挂载'));
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    check('降低角色立即收紧文件读写授权', await writableRoot.locator('[data-root-access]').inputValue() === 'readonly' && await writableRoot.locator('option[value="readwrite"]').evaluate(option => option.disabled));
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    await writableRoot.locator('[data-root-access]').selectOption('readwrite');
    await manager.locator('[data-ref="create"]').click();
    check('确认清楚列出角色与文件范围且不注入名称', (await page.locator('.fullmask').last().innerText()).includes('角色：读写') && (await page.locator('.fullmask').last().innerText()).includes('电影/2026') && await page.locator('.fullmask').last().locator('img').count() === 0);
    await page.locator('.fullmask [data-yes]').click();
    await page.locator('#r1ApiSecret').waitFor();
    await page.locator('#r1ApiSecret [data-ref="close"]').click();
    const fileRequest = calls.filter(call => call.path === '/api/access/keys').at(-1).body;
    check('文件范围只提交稳定根标识相对路径与访问方式', fileRequest.role === 'readwrite' && JSON.stringify(fileRequest.file_roots) === JSON.stringify([
      { id: 'root_' + '1'.repeat(24), path: '电影/2026', access: 'readwrite' },
      { id: 'root_' + '2'.repeat(24), path: '', access: 'readonly' },
    ]) && !JSON.stringify(fileRequest).includes('/drives'));
    check('列表展示角色范围及实际工具边界', (await manager.locator('.r1-api-item').last().innerText()).includes('读写') && (await manager.locator('.r1-api-item').last().innerText()).includes('电影/2026') && (await manager.locator('.r1-api-item').last().innerText()).includes('已授权的接入工具'));

    await manager.locator('.r1-api-item').last().locator('[data-ref="edit"]').click();
    const sameRoot = manager.locator('.r1-api-file-root').first();
    await sameRoot.locator('[data-add-range]').click();
    await sameRoot.locator('[data-root-path]').nth(1).fill('备份/资料');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const multiGrantEdit = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('同根多个子目录无损保存且只提交已改文件范围', JSON.stringify(multiGrantEdit.file_roots) === JSON.stringify([
      { id: 'root_' + '1'.repeat(24), path: '电影/2026', access: 'readwrite' },
      { id: 'root_' + '1'.repeat(24), path: '备份/资料', access: 'readonly' },
      { id: 'root_' + '2'.repeat(24), path: '', access: 'readonly' },
    ]) && !Object.hasOwn(multiGrantEdit, 'role') && !Object.hasOwn(multiGrantEdit, 'scopes') && !Object.hasOwn(multiGrantEdit, 'tool_grants'));
    await manager.locator('.r1-api-item').last().locator('[data-ref="edit"]').click();
    check('再次编辑保留同根全部范围及并集提示', await manager.locator('.r1-api-file-root').first().locator('[data-root-path]').count() === 2 && (await manager.locator('[data-ref="fileRoots"]').innerText()).includes('并集'));
    await manager.locator('input[data-ref="name"]').fill('文件范围 AI 已编辑');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const multiMetadata = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('只改名称不重写多范围或工具和项目授权', !Object.hasOwn(multiMetadata, 'file_roots') && !Object.hasOwn(multiMetadata, 'tool_grants') && !Object.hasOwn(multiMetadata, 'docker_projects') && state.keys.at(-1).file_roots.length === 3);
    state.file_root_catalog.roots[0].read_only = true;
    await manager.locator('[data-ref="refresh"]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager').getAttribute('aria-busy') === 'false');
    await manager.locator('.r1-api-item').last().locator('[data-ref="edit"]').click();
    check('挂载变只读仍显示原读写授权而非静默改写', await manager.locator('.r1-api-file-root').first().locator('[data-root-access]').first().inputValue() === 'readwrite');
    await manager.locator('input[data-ref="name"]').fill('文件范围 AI 挂载变更保留');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    check('失效写绑定不阻止只改名称且原多范围保持', !Object.hasOwn(calls.filter(call => call.path === '/api/access/keys/update').at(-1).body, 'file_roots') && state.keys.at(-1).file_roots[0].access === 'readwrite');
    state.file_root_catalog.roots[0].read_only = false;
    await manager.locator('[data-ref="refresh"]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager').getAttribute('aria-busy') === 'false');
    await view('create');
    await manager.locator('[data-ref="scopes"] input[value="files"]').check();

    const rootsBeforeUnavailable = state.file_root_catalog;
    state.file_root_catalog = { available: false, status: 'unavailable', reason: 'fixture：尚无可信挂载', roots: [] };
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="fileRootsStatus"]').filter({ hasText: '尚无可信挂载' }).waitFor();
    check('缺少可信根时不造占位盘并阻止文件密钥创建', await manager.locator('[data-ref="fileRootOptions"] .r1-api-file-root').count() === 0 && await manager.locator('[data-ref="create"]').isDisabled());
    state.file_root_catalog = rootsBeforeUnavailable;
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('[data-ref="fileRootOptions"] .r1-api-file-root').last().waitFor();
    check('挂载恢复不自动重新勾选文件范围', await manager.locator('[data-root-check]:checked').count() === 0);
    await manager.locator('[data-ref="scopes"] input[value="files"]').uncheck();

    await view('manage');
    state.keys.push({ id: 'fixture-legacy', name: '旧权限密钥', scopes: ['read', 'power'], allowed_ips: [], created_at: 1793240000,
      expires_at: null, revoked_at: null, last_used_at: null, status: 'active' });
    await manager.locator('[data-ref="refresh"]').click();
    await manager.locator('.r1-api-item').filter({ hasText: '旧权限密钥' }).waitFor();
    const legacy = manager.locator('.r1-api-item').filter({ hasText: '旧权限密钥' });
    check('旧密钥按原有精确权限展示而不误标管理员', (await legacy.locator('[data-ref="role"]').innerText()) === '原有精确权限' && !(await legacy.locator('[data-ref="role"]').innerText()).includes('管理员'));
    state.keys.find(key => key.id === 'fixture-legacy').role = 'legacy';
    await manager.locator('[data-ref="refresh"]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager').getAttribute('aria-busy') === 'false');
    check('显式 legacy 也保持原有精确权限标签', (await legacy.locator('[data-ref="role"]').innerText()) === '原有精确权限');
    await legacy.locator('[data-ref="edit"]').click();
    check('编辑旧key保留legacy选项且不默认升级角色', await manager.locator('select[data-ref="role"]').inputValue() === 'legacy'
      && await manager.locator('[data-ref="scopes"] input[value="power"]').isDisabled());
    await manager.locator('input[data-ref="name"]').fill('旧权限密钥已编辑');
    await manager.locator('textarea[data-ref="ips"]').fill('192.168.31.0/24');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const legacyEdit = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('旧key仅编辑元数据不发送角色功能或新过期时间', legacyEdit.id === 'fixture-legacy'
      && !Object.hasOwn(legacyEdit, 'role') && !Object.hasOwn(legacyEdit, 'scopes') && !Object.hasOwn(legacyEdit, 'expires_days') && !Object.hasOwn(legacyEdit, 'tool_grants') && !Object.hasOwn(legacyEdit, 'docker_projects'));
    await legacy.locator('[data-ref="edit"]').click();
    await manager.locator('[data-ref="upgradePolicy"]').check();
    check('旧键仅显式升级后启用逐工具且新增工具不自动授权', await manager.locator('select[data-ref="role"]').inputValue() === 'readonly' && JSON.stringify(await manager.locator('[data-ref="toolOptions"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["get_nas_status"]');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const upgraded = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('显式升级准确提交工具权限而非全部scope工具', upgraded.id === 'fixture-legacy' && JSON.stringify(upgraded.tool_grants) === '["get_nas_status"]' && state.keys.find(key => key.id === 'fixture-legacy').policy_version === 3);

    await view('create');
    await manager.locator('input[data-ref="name"]').fill('项目读取 AI');
    await manager.locator('[data-ref="scopes"] input[value="docker"]').check();
    const beforeProjectKey = calls.length;
    await manager.locator('[data-ref="create"]').click();
    await manager.locator('[data-ref="error"]').filter({ hasText: '已登记 Docker 项目' }).waitFor();
    check('Docker范围没有登记项目时不会虚构授权', calls.length === beforeProjectKey && await manager.locator('[data-ref="dockerProjectOptions"] input').count() === 0);
    await manager.locator('[data-ref="projectManager"] summary').click();
    await manager.locator('[data-ref="loadProjects"]').click();
    await manager.locator('[data-ref="projectCandidates"] [data-register]').waitFor();
    check('项目候选仅按需读且动态名称不注入', projectReads === 2 && await manager.locator('[data-ref="projectCandidates"] svg').count() === 0);
    await manager.locator('[data-ref="projectCandidates"] [data-register]').click();
    check('登记确认明确不部署或启停', (await page.locator('.fullmask').last().innerText()).includes('不部署、更新或启停 Docker'));
    await page.locator('.fullmask [data-yes]').click();
    await manager.locator('[data-ref="dockerProjectOptions"] [data-project-check]').waitFor();
    await manager.locator('[data-ref="dockerProjectOptions"] [data-project-check]').check();
    const projectControl = manager.locator('[data-project-operation][value="control"]');
    const projectUpdate = manager.locator('[data-project-operation][value="update_image"]');
    check('项目默认仅选读取且写范围需要管理员', !(await projectControl.isChecked()) && !(await projectUpdate.isChecked()) && await projectControl.isDisabled() && await projectUpdate.isDisabled());
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    await projectControl.check(); await projectUpdate.check();
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    check('降级角色清除并禁用项目写操作', !(await projectControl.isChecked()) && !(await projectUpdate.isChecked()) && await projectControl.isDisabled());
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    check('提高角色不恢复项目写操作', !(await projectControl.isChecked()) && !(await projectUpdate.isChecked()));
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    await manager.locator('[data-ref="toolOptions"] input[value="get_docker_project_status"]').check();
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.locator('#r1ApiSecret').waitFor(); await page.locator('#r1ApiSecret [data-ref="close"]').click();
    const projectKey = calls.filter(call => call.path === '/api/access/keys').at(-1).body;
    check('项目键只发送已登记inspect和显式读取工具', JSON.stringify(projectKey.docker_projects) === JSON.stringify([{ id: projectId, operations: ['inspect'] }]) && JSON.stringify(projectKey.tool_grants) === '["get_nas_status","get_docker_project_status"]');
    await view('manage');
    await manager.locator('.r1-api-item').last().locator('[data-ref="edit"]').click();
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    for (const operation of ['control', 'update_image']) await manager.locator(`[data-project-operation][value="${operation}"]`).check();
    for (const tool of ['control_docker_project', 'plan_docker_project_update', 'execute_docker_project_update']) await manager.locator(`[data-ref="toolOptions"] input[value="${tool}"]`).check();
    await page.setViewportSize({ width: 320, height: 960 });
    check('320宽度项目操作授权无需横向滚动', await manager.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    await page.setViewportSize({ width: 1280, height: 1000 });
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await manager.locator('.r1-api-item').last().locator('[data-ref="role"]').filter({ hasText: '管理员' }).waitFor();
    const writeProject = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('管理员逐项目显式提交三项操作且保持具体工具授权', JSON.stringify(writeProject.docker_projects) === JSON.stringify([{ id: projectId, operations: ['inspect', 'control', 'update_image'] }]) && writeProject.tool_grants.includes('execute_docker_project_update'));
    await view('create');
    await manager.locator('[data-ref="registeredProjects"] [data-project-label]').fill('片库新标签');
    await manager.locator('[data-ref="registeredProjects"] [data-update]').click(); await page.locator('.fullmask [data-yes]').click();
    await manager.locator('[data-ref="registeredProjects"] strong').filter({ hasText: '片库新标签' }).waitFor();
    check('项目标签更新只修改元数据且保留授权ID', state.keys.at(-1).docker_projects[0].id === projectId && calls.some(call => call.path === '/api/docker/projects/update' && call.body.project_id === projectId));
    await manager.locator('[data-ref="registeredProjects"] [data-revoke]').click(); await page.locator('.fullmask [data-yes]').click();
    await manager.locator('[data-ref="registeredProjects"] [data-project-reason]').filter({ hasText: '已撤销登记' }).waitFor();
    check('撤销登记不调用部署/停止且失效项目不可勾选', await manager.locator('[data-ref="dockerProjectOptions"] [data-project-check]').isDisabled() && calls.filter(call => call.path.startsWith('/api/docker')).every(call => ['/api/docker/projects/register','/api/docker/projects/update','/api/docker/projects/revoke'].includes(call.path)));
    check('项目管理没有执行动态名称脚本', await page.evaluate(() => !window.apiProjectInjected));
    await manager.locator('[data-ref="scopes"] input[value="docker"]').uncheck();
    await manager.locator('[data-ref="projectManager"] summary').click();
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();

    await manager.locator('input[data-ref="name"]').fill('长期 AI');
    await manager.locator('[data-ref="expiry"]').selectOption('0');
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    await manager.locator('[data-ref="scopes"] input[value="power"]').check();
    await manager.locator('[data-ref="create"]').click();
    check('授予电源权限时明确说明', (await page.locator('.fullmask').last().innerText()).includes('电源权限可安排关机或重启'));
    await page.locator('.fullmask [data-yes]').click();
    await page.locator('#r1ApiSecret').waitFor();
    const escapedField = await page.locator('#r1ApiSecret [data-ref="config"]').elementHandle();
    await page.keyboard.press('Escape');
    check('Escape 清理密钥而保留管理页', await escapedField.inputValue() === '' && await manager.count() === 1 && await page.locator('#r1ApiSecret').count() === 0);
    await manager.locator('.r1-api-item').last().waitFor();
    check('长期有效期明确提交零值', calls.filter(call => call.path === '/api/access/keys').at(-1).body.expires_days === 0 && (await manager.locator('.r1-api-item').last().innerText()).includes('长期'));
    check('管理员创建仍只携带明确勾选功能且不带未选文件授权', calls.filter(call => call.path === '/api/access/keys').at(-1).body.role === 'admin' && JSON.stringify(calls.filter(call => call.path === '/api/access/keys').at(-1).body.scopes) === '["read","fan","power"]' && JSON.stringify(calls.filter(call => call.path === '/api/access/keys').at(-1).body.file_roots) === '[]');

    await page.setViewportSize({ width: 320, height: 740 });
    await view('create');
    await manager.locator('[data-ref="scopes"] input[value="files"]').check();
    await manager.locator('.r1-api-file-root [data-root-check]').first().check();
    check('320px 管理页面和文件范围不横向溢出', await manager.evaluate(element => [...element.querySelectorAll('.r1-api-dialog, .r1-api-item, input, textarea, select, fieldset')].every(node => node.getBoundingClientRect().right <= 321 && node.getBoundingClientRect().left >= -1)));
    await view('operations');
    check('320px工具能力与记录不横向溢出', await manager.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    await view('manage');
    check('320pxToken列表不横向溢出', await manager.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    state.scope_catalog = state.scope_catalog.filter(scope => scope.id !== 'docker');
    await manager.locator('[data-ref="close"]').click();
    check('320pxAPI设置入口不横向溢出', await home.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    await home.locator('[data-ref="enabled"]').click();
    await home.locator('[data-ref="serviceStatus"]').filter({ hasText: '已开启' }).waitFor();
    await home.locator('[data-ref="enabled"]').click();
    await home.locator('[data-ref="serviceStatus"]').filter({ hasText: '已关闭' }).waitFor();
    check('关闭服务可立即提交且保留Token', calls.at(-1).path === '/api/services/mcp' && calls.at(-1).body.enabled === false && state.keys.length > 0 && await page.locator('.fullmask').count() === 0);
    await home.locator('[data-ref="manage"]').click();
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('Token管理入口只展示管理页', await manager.locator('[data-ref="keys"]').isVisible() && await manager.locator('[data-ref="form"]').isHidden() && await manager.locator('[data-ref="audit"]').isHidden());
    await manager.locator('[data-ref="close"]').click();
    await home.locator('[data-ref="operations"]').click();
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('Token操作入口只展示能力和记录', await manager.locator('[data-ref="audit"]').isVisible() && await manager.locator('[data-ref="form"]').isHidden() && await manager.locator('[data-ref="keys"]').isHidden());
    await manager.locator('[data-ref="close"]').click();
    await home.locator('[data-ref="add"]').click();
    await page.locator('#r1ApiManager [data-ref="content"]:not([hidden])').waitFor();
    check('重新打开只返回元数据', await page.locator('#r1ApiSecret').count() === 0 && !(await page.locator('#r1ApiManager').innerText()).includes(secret));
    check('缺少领域元数据时不虚构授权选项', await page.locator('#r1ApiManager [data-ref="scopes"] input[value="docker"]').count() === 0);
    check('重新打开恢复只读与默认状态域且不默认授予文件', await page.locator('#r1ApiManager select[data-ref="role"]').inputValue() === 'readonly' && JSON.stringify(await page.locator('#r1ApiManager [data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    check('名称未执行脚本', await page.evaluate(() => !window.apiNameInjected));
    check('文件根名称未执行脚本', await page.evaluate(() => !window.apiRootInjected));
    check('页面无脚本异常', pageErrors.length === 0);
    console.log(JSON.stringify({ ok: true, checks, fixture: fixtureDir, reads, writes: calls.length }, null, 2));
  } catch (error) {
    console.error(JSON.stringify({ ok: false, checks, pageErrors, error: String(error && error.stack || error), stderr }, null, 2));
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
    if (browser) await browser.close().catch(() => {});
    child.kill('SIGTERM');
  }
})();
