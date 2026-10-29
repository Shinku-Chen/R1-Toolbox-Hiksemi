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
  let reads = 0, contractReads = 0, failContracts = false, failCreate = false, failRead = false, failManage = false;
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
        .map(([id, label]) => ({ id, label, description: '分领域授权', min_role: 'readonly', implemented: false })),
      { id: 'unrecognized', label: '未知功能不可伪造', implemented: true },
    ],
    file_root_catalog: { available: true, status: 'ok', reason: null, roots: [
      { id: 'root_' + '1'.repeat(24), label: '<img src=x onerror="window.apiRootInjected=1">视频资源', mount: '/drives/space_1', read_only: false },
      { id: 'root_' + '2'.repeat(24), label: '备份磁盘', mount: '/drives/space_2', read_only: true },
    ] },
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
      if (url.pathname !== '/api/access' && !url.pathname.startsWith('/api/access/')) return route.continue();
      const reply = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
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
        const key = { id: 'fixture-key-' + (state.keys.length + 1), ...body, created_at: 1793240000, enabled: true, disabled: false,
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
          if (Object.hasOwn(body, 'role')) key.policy_version = 2;
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
    await page.locator('#set-r1-api [data-ref="open"]').click();
    const manager = page.locator('#r1ApiManager');
    await manager.locator('[data-ref="content"]:not([hidden])').waitFor();
    check('服务默认关闭', !(await manager.locator('[data-ref="enabled"]').isChecked()));
    check('默认仅勾选读取状态', JSON.stringify(await manager.locator('[data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    check('默认只读角色与三级选项', await manager.locator('select[data-ref="role"]').inputValue() === 'readonly' && JSON.stringify(await manager.locator('select[data-ref="role"] option').evaluateAll(options => options.map(option => option.value))) === '["readonly","readwrite","admin"]');
    check('十五项功能只显示已知领域', await manager.locator('[data-ref="scopes"] input').count() === 15 && await manager.locator('[data-ref="scopes"] input[value="unrecognized"]').count() === 0);
    check('新增功能明确显示待接入', (await manager.locator('[data-ref="scopes"] label').filter({ hasText: '文件管理' }).innerText()).includes('待接入') && (await manager.locator('[data-ref="scopes"] label').filter({ hasText: 'Docker 部署' }).innerText()).includes('仅预授权'));
    check('未选择文件功能时隐藏范围', await manager.locator('[data-ref="fileRoots"]').isHidden());
    await manager.locator('select[data-ref="role"]').selectOption('admin');
    check('管理员角色不自动新增功能授权', JSON.stringify(await manager.locator('[data-ref="scopes"] input:checked').evaluateAll(inputs => inputs.map(input => input.value))) === '["read"]');
    await manager.locator('select[data-ref="role"]').selectOption('readonly');
    check('默认有效期 90 天', await manager.locator('[data-ref="expiry"]').inputValue() === '90');
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
    check('真实服务返回20项待接入接口与输入输出schema', definition.status === 'definitions_only' && definition.tools.length === 20 && definition.tools.every(tool => tool.implemented === false && tool.inputSchema && tool.outputSchema));
    check('接口显示三级权限且不改变表单授权', (await manager.locator('[data-ref="contractList"]').innerText()).includes('管理员') && (await manager.locator('[data-ref="contractList"]').innerText()).includes('读写') && (await manager.locator('[data-ref="contractList"]').innerText()).includes('只读') && await manager.locator('select[data-ref="role"]').inputValue() === 'readonly' && calls.length === 0);
    await page.setViewportSize({ width: 320, height: 960 });
    check('320宽度接口定义无需横向滚动', await manager.evaluate(node => node.scrollWidth <= node.clientWidth + 2));
    await page.setViewportSize({ width: 1280, height: 1000 });
    await manager.locator('[data-ref="contracts"] summary').click();

    await manager.locator('[data-ref="enabled"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="serviceStatus"]').textContent === '已开启');
    check('启用服务保持 JSON 与 CSRF 请求头', calls[0].body.enabled === true && calls[0].headers['x-fnm-request'] === '1' && calls[0].headers['content-type'] === 'application/json');

    await manager.locator('input[data-ref="name"]').fill(unsafeName);
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();
    check('低角色保留功能选择但说明工具不可调用', (await manager.locator('[data-ref="roleStatus"]').innerText()).includes('当前角色不可调用') && await manager.locator('[data-ref="scopes"] input[value="fan"]').isChecked());
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
    check('创建提交明确角色、功能、空文件范围、来源与有效期', created.body.role === 'readonly' && JSON.stringify(created.body.file_roots) === '[]' && JSON.stringify(created.body.scopes) === '["read","fan"]' && created.body.allowed_ips.length === 2 && created.body.expires_days === 90);
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
      && !Object.hasOwn(metadataEdit, 'expires_days') && !Object.hasOwn(metadataEdit, 'role') && !Object.hasOwn(metadataEdit, 'scopes') && !Object.hasOwn(metadataEdit, 'file_roots'));
    await manager.locator('.r1-api-item [data-ref="edit"]').click();
    await manager.locator('select[data-ref="role"]').selectOption('readwrite');
    await manager.locator('[data-ref="scopes"] input[value="screen"]').check();
    await manager.locator('[data-ref="expiry"]').selectOption('7');
    await manager.locator('[data-ref="create"]').click(); await page.locator('.fullmask [data-yes]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="create"]').textContent === '创建密钥');
    const permissionEdit = calls.filter(call => call.path === '/api/access/keys/update').at(-1).body;
    check('明确编辑角色权限和有效期才提交对应字段', permissionEdit.role === 'readwrite'
      && JSON.stringify(permissionEdit.scopes) === '["read","fan","screen"]' && permissionEdit.expires_days === 7 && JSON.stringify(permissionEdit.file_roots) === '[]');
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
    await manager.locator('[data-ref="scopes"] input[value="fan"]').check();

    await manager.locator('.r1-api-item [data-ref="revoke"]').click();
    await page.locator('.fullmask [data-yes]').click();
    await manager.locator('.r1-api-item [data-ref="status"]').filter({ hasText: '已撤销' }).waitFor();
    check('撤销后刷新安全元数据', calls.some(call => call.path === '/api/access/keys/revoke') && await manager.locator('.r1-api-item [data-ref="revoke"]').isHidden());
    check('已撤销Token不能恢复编辑启用或轮换', await manager.locator('.r1-api-item [data-ref="edit"]').isHidden()
      && await manager.locator('.r1-api-item [data-ref="toggle"]').isHidden() && await manager.locator('.r1-api-item [data-ref="rotate"]').isHidden());

    failCreate = true;
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
    check('文件功能显示真实根与待接入说明', await manager.locator('[data-ref="fileRootOptions"] .r1-api-file-root').count() === 2 && (await manager.locator('[data-ref="fileRoots"]').innerText()).includes('待接入，当前仅保存范围授权'));
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
    check('子目录限制 1024 字符且根名称不注入 HTML', await writableRoot.locator('[data-root-path]').getAttribute('maxlength') === '1024' && await manager.locator('[data-ref="fileRootOptions"] img').count() === 0 && (await writableRoot.innerText()).includes('视频资源'));
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
    check('列表展示角色和范围预授权', (await manager.locator('.r1-api-item').last().innerText()).includes('读写') && (await manager.locator('.r1-api-item').last().innerText()).includes('电影/2026') && (await manager.locator('.r1-api-item').last().innerText()).includes('待接入'));

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
      && !Object.hasOwn(legacyEdit, 'role') && !Object.hasOwn(legacyEdit, 'scopes') && !Object.hasOwn(legacyEdit, 'expires_days'));
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
    await manager.locator('[data-ref="scopes"] input[value="files"]').check();
    await manager.locator('.r1-api-file-root [data-root-check]').first().check();
    check('320px 管理页面和文件范围不横向溢出', await manager.evaluate(element => [...element.querySelectorAll('.r1-api-dialog, .r1-api-item, input, textarea, select, fieldset')].every(node => node.getBoundingClientRect().right <= 321 && node.getBoundingClientRect().left >= -1)));
    await manager.locator('[data-ref="enabled"]').click();
    await page.waitForFunction(() => document.querySelector('#r1ApiManager [data-ref="serviceStatus"]').textContent === '已关闭');
    check('关闭服务可立即提交', calls.at(-1).path === '/api/access/settings' && calls.at(-1).body.enabled === false);
    state.scope_catalog = state.scope_catalog.filter(scope => scope.id !== 'docker');
    await manager.locator('[data-ref="close"]').click();
    await page.locator('#set-r1-api [data-ref="open"]').click();
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
