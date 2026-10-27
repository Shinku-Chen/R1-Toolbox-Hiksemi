// Fresh headless browser + intercepted fixture APIs only. No NAS service is contacted.
const { chromium } = require('playwright-core');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {pathToFileURL}=require('node:url');
const uiURL=pathToFileURL(path.resolve(__dirname,'../ui/index.html')).href;
const screenshotDir=process.env.R1_UI_SCREENSHOT_DIR;
if(screenshotDir)fs.mkdirSync(screenshotDir,{recursive:true});
const chromeCandidates=[process.env.CHROME_BIN,
 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
 '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
 '/usr/bin/google-chrome','/usr/bin/google-chrome-stable','/usr/bin/chromium','/usr/bin/chromium-browser',
 ...String(process.env.PATH||'').split(path.delimiter).flatMap(folder=>['google-chrome','google-chrome-stable','chromium','chromium-browser'].map(name=>path.join(folder,name)))];
const chromeBin=chromeCandidates.find(candidate=>candidate&&fs.existsSync(candidate));
assert.ok(chromeBin,'未找到 Chrome/Chromium，请通过 CHROME_BIN 指定可执行文件');
const blockedRequests=[];
async function fixtureOnly(context){
 await context.route('**/*',route=>{
  const url=route.request().url();
  if(url.startsWith('file:'))return route.continue();
  // Page-level fixture API handlers run first; everything else is denied.
  blockedRequests.push(url);return route.abort('blockedbyclient');
 });
}
(async()=>{
 const browser=await chromium.launch({executablePath:chromeBin,headless:true});
 try{
 const context=await browser.newContext({viewport:{width:376,height:960},deviceScaleFactor:1,locale:'zh-CN',hasTouch:true});
 await context.addInitScript(()=>{window.require=(name)=>name==='process'?{env:{R1_TOOLBOX_CONTROL_TOKEN:'fixture-only-not-real'}}:{ipcRenderer:{once:(_n,cb)=>{window._fixtureAck=cb},send:(n)=>{if(n==='showWin'&&window._fixtureAck)window._fixtureAck()}}};});
 await fixtureOnly(context);
 const page=await context.newPage();const errors=[],posts=[],sizes=[];let offline=false;let failNextAction=false;
 page.on('pageerror',e=>errors.push(e.message));
 const settings={theme:'dark',font_scale:100,page_interval:0,sleep_timeout:0,brightness:80,night_enabled:false,night_start:'22:00',night_end:'07:00',night_brightness:20,enabled_pages:['overview','storage','network','controls','apps','system','settings']};
 const state={capabilities:{screen:{available:true,reason:''},fans:{available:false,reason:'缺少 IT87 hwmon 风扇驱动/PWM 通道'},rgb:{available:false,reason:'需明确指定 I2C bus 并验证 0x26；默认不写入'},power:{available:true,reason:''}},settings,screen:{brightness:55,asleep:false,max_brightness:96000},fans:{channels:[],temperature_c:46},rgb:{bus:null,mode:null,color:null,availableBuses:[0,4]},power:{pending:null,executing:false,last_error:null}};
 const insights={schema_version:1,timestamp:new Date().toISOString(),system:{model:'HIKSEMI R1',cpu_model:'Intel N100',cpu_logical_cores:4,load_average:[.34,.52,.6],process_count:142,memory:{swap_used_bytes:0,swap_total_bytes:0},cpu_cores:[0,1,2,3].map(id=>({id,usage_percent:16+id}))},interfaces:[{name:'eth0',ipv4:['192.168.1.10'],mac:'00:11:22:33:44:55',operstate:'up',rx_bytes_per_sec:4096,tx_bytes_per_sec:1024}],disks:{available:true,reason:'',items:[{id:'sda',device:'/dev/sda',model:'Test disk',size_bytes:4e12,temp_c:38,health:{passed:true},standby:false}]},containers:{available:true,reason:'',items:[{id:'abcdef123456',name:'test-db',image:'postgres:17',state:'running',status:'Up 2 hours',cpu_percent:125,memory_bytes:125829120,protected:false},{id:'123456abcdef',name:'r1-toolbox',image:'ghcr.io/shinku-chen/r1-toolbox:preview',state:'exited',protected:true}]},vms:{available:false,reason:'宿主未提供 libvirt 服务',items:[]},gpu:{available:true,devices:[{name:'Intel UHD',busy_percent:null,vram_total_bytes:null,vram_used_bytes:null}]},ports:[{protocol:'tcp',address:'127.0.0.1',port:18731}],processes:[{pid:900,name:'hiksemi-sub',cpu_percent:120,memory_bytes:1024*1024*200}],history:[10,20,15,40,28].map(cpu=>({cpu_usage_percent:cpu,memory_usage_percent:35}))};
 const status={schema_version:1,available:true,timestamp:new Date().toISOString(),hostname:'R1-NAS',uptime_seconds:281940,cpu:{usage_percent:18,temp_c:46},memory:{used_bytes:3e9,total_bytes:16e9,usage_percent:18.75},network:{interface:'eth0',ipv4:'192.168.1.10',rx_bytes_per_sec:4096,tx_bytes_per_sec:1024},disks:[{mount:'/drives/data',total_bytes:4e12,used_bytes:1e12,usage_percent:25}]};
 await page.route('http://127.0.0.1:18731/api/**',async route=>{
  if(offline)return route.abort('connectionrefused');
  const req=route.request();const endpoint=new URL(req.url()).pathname;let data;
  if(req.method()==='OPTIONS')return route.fulfill({status:204,headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'GET, POST','Access-Control-Allow-Headers':'Content-Type, X-R1-Token'}});
  if(endpoint.endsWith('/action')){
   assert.equal(req.headers()['x-r1-token'],'fixture-only-not-real');
   const body=req.postDataJSON();posts.push(body);
   if(failNextAction&&body.action==='screen.brightness'){
    failNextAction=false;
    return route.fulfill({status:409,headers:{'Access-Control-Allow-Origin':'*','Content-Type':'application/json'},body:JSON.stringify({ok:false,error:'fixture_failure',reason:'模拟亮度调整被拒绝'})});
   }
   if(body.action==='fan.set'){Object.assign(state.fans.channels[0],{owned:true,mode:body.mode,duty_percent:body.value});}
   if(body.action==='fan.restore'){Object.assign(state.fans.channels[0],{owned:false,mode:'native'});}
   if(body.action==='screen.brightness')state.screen.brightness=body.value;
   if(body.action==='screen.sleep')state.screen.asleep=true;
   if(body.action==='screen.wake')state.screen.asleep=false;
   if(body.action==='settings.update')Object.assign(settings,body.settings);
   if(body.action==='power.request'){assert.equal(body.confirmed,true);state.power.pending={operation:body.operation,seconds_remaining:5};}
   if(body.action==='power.cancel')state.power.pending=null;
   if(body.action==='container.action'){assert.equal(body.confirmed,true);insights.containers.items[0].state=body.operation==='stop'?'exited':'running';}
   if(body.action==='rgb.set'){state.rgb={bus:body.bus,mode:body.mode,color:body.color,availableBuses:[0,4]};state.capabilities.rgb.available=true;}
   data={ok:true,state,insights};
  }else data=endpoint.endsWith('/status')?status:endpoint.endsWith('/toolbox')?state:insights;
  return route.fulfill({status:200,headers:{'Access-Control-Allow-Origin':'*','Content-Type':'application/json'},body:JSON.stringify(data)});
 });
 await page.goto(uiURL);
 await page.waitForFunction(()=>!document.getElementById('brightness-save').disabled);
 assert.equal(posts.filter(x=>x.action==='power.request').length,0,'no startup power request');
 for(const width of [376,320]){
  await page.setViewportSize({width,height:960});
  for(const name of settings.enabled_pages){
   await page.locator(`[data-page="${name}"]`).click();
   const m=await page.evaluate(name=>{const p=document.getElementById('page-'+name);return{width:innerWidth,name,body:document.body.scrollWidth,scroll:p.scrollWidth,client:p.clientWidth,height:p.scrollHeight,visible:p.clientHeight}},name);
   sizes.push(m);assert.ok(m.body<=width&&m.scroll<=m.client+1,'horizontal overflow '+JSON.stringify(m));
   if(screenshotDir&&['controls','apps','settings','overview'].includes(name))await page.screenshot({path:path.join(screenshotDir,`preview-${width}-${name}.png`)});
  }
 }
 await page.setViewportSize({width:376,height:960});
 await page.locator('[data-page="controls"]').click();
 assert.match(await page.locator('#fan-controls').textContent(),/缺少 IT87/);
 assert.equal(await page.locator('#fan-controls button').count(),0,'missing PWM must not expose buttons');
 state.capabilities.fans={available:true,reason:''};state.fans.channels=[{id:'hwmon2:pwm3',driver:'it87',duty_percent:80,owned:false,mode:'native',rpm:null,rpm_verified:false,rpm_reason:'同号传感器未确认物理对应'}];
 await page.waitForSelector('.fan-input');
 await page.getByRole('button',{name:'自动温控',exact:true}).click();assert.match(await page.locator('#toast').textContent(),/请先勾选/);assert.equal(posts.filter(x=>x.action==='fan.set').length,0);
 await page.locator('.fan-channel input[type=checkbox]').check();await page.getByRole('button',{name:'自动温控',exact:true}).click();await page.locator('#confirm-accept').click();await page.waitForFunction(()=>document.querySelector('.fan-channel').textContent.includes('已由 Toolbox 接管'));assert.equal(posts.find(x=>x.action==='fan.set').takeover,true);
 await page.getByRole('button',{name:'恢复原控制',exact:true}).click();await page.locator('#confirm-accept').click();await page.waitForFunction(()=>document.querySelector('.fan-channel').textContent.includes('尚未接管'));assert.equal(posts.filter(x=>x.action==='fan.restore').length,1);
 failNextAction=true;await page.locator('#brightness-save').click();await page.waitForFunction(()=>document.getElementById('toast').textContent==='模拟亮度调整被拒绝');assert.equal(state.screen.brightness,55);await page.waitForFunction(()=>!document.getElementById('brightness-save').disabled);
 await page.locator('#brightness').fill('65');await page.locator('#brightness').dispatchEvent('input');await page.locator('#brightness-save').click();
 await page.waitForFunction(()=>document.getElementById('toast').textContent==='亮度已调整');assert.equal(state.screen.brightness,65);
 await page.locator('#power-off').click();assert.equal(posts.filter(x=>x.action==='power.request').length,0,'opening modal cannot request power');
 await page.locator('#confirm-cancel').click();assert.equal(posts.filter(x=>x.action==='power.request').length,0);
 await page.locator('#power-reboot').click();await page.locator('#confirm-accept').click();
 await page.waitForFunction(()=>!document.getElementById('power-pending').hidden);assert.equal(state.power.pending.operation,'reboot');
 await page.locator('#power-cancel').click();await page.waitForFunction(()=>document.getElementById('power-pending').hidden);assert.equal(state.power.pending,null);
 await page.locator('#screen-sleep').click();await page.locator('#confirm-accept').click();await page.waitForFunction(()=>!document.getElementById('sleep-overlay').hidden);
 const previousPower=posts.filter(x=>x.action==='power.request').length;
 await page.locator('#sleep-overlay').dispatchEvent('pointerdown',{pointerId:2,isPrimary:true,clientX:100,clientY:300});
 await page.waitForFunction(()=>document.getElementById('sleep-overlay').hidden);
 await page.locator('#power-off').dispatchEvent('click');
 assert.equal(await page.locator('#confirm-backdrop').isHidden(),true,'first wake pointer sequence must not open power modal');
 assert.equal(posts.filter(x=>x.action==='power.request').length,previousPower);
 await page.waitForTimeout(700);
 assert.equal(await page.locator('#rgb-mode option[value="2"]').textContent(),'彩虹');assert.equal(await page.locator('#rgb-mode option[value="3"]').textContent(),'呼吸');
 await page.locator('#rgb-mode').selectOption('3');
 await page.locator('#rgb-bus').selectOption('4');await page.locator('#rgb-save').click();await page.locator('#confirm-accept').click();
 await page.waitForFunction(()=>document.getElementById('rgb-state').textContent==='可控制');assert.equal(state.rgb.bus,4);assert.equal(state.rgb.mode,3);
 await page.locator('[data-page="apps"]').click();
 assert.match(await page.locator('#container-list').textContent(),/125.0%/,'multi-core CPU may exceed 100%');
 const protectedButtons=page.locator('#container-list article').filter({hasText:'r1-toolbox'}).locator('button');assert.equal(await protectedButtons.count(),1);assert.equal(await protectedButtons.first().isDisabled(),true);
 await page.locator('#container-list article').filter({hasText:'test-db'}).getByRole('button',{name:'停止',exact:true}).click();
 await page.locator('#confirm-accept').click();await page.waitForFunction(()=>Array.from(document.querySelectorAll('#container-list button')).some(x=>x.textContent==='启动'&&!x.disabled));
 await page.locator('[data-page="settings"]').click();await page.locator('#setting-theme').selectOption('jade');await page.locator('#setting-theme').dispatchEvent('input');
 await page.locator('#setting-font').fill('120');await page.locator('#setting-font').dispatchEvent('input');await page.locator('#settings-save').click();
 await page.waitForFunction(()=>document.documentElement.dataset.theme==='jade');assert.equal(settings.font_scale,120);
 await page.locator('#setting-font').fill('140');await page.locator('#setting-font').dispatchEvent('input');await page.locator('#settings-save').click();await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--ui-scale')==='1.4');await page.setViewportSize({width:320,height:960});
 for(const name of settings.enabled_pages){await page.locator(`[data-page="${name}"]`).click();const m=await page.evaluate(name=>{const p=document.getElementById('page-'+name);return{width:innerWidth,name,scroll:p.scrollWidth,client:p.clientWidth}},name);assert.ok(m.scroll<=m.client+1,'font120 overflow '+JSON.stringify(m));}
 offline=true;await page.waitForFunction(()=>document.getElementById('brightness-save').disabled&&document.getElementById('connection-label').textContent==='数据暂不可用');
 assert.equal(await page.locator('#power-off').isDisabled(),true);assert.equal(await page.locator('#settings-save').isDisabled(),true);
 assert.equal(errors.length,0,'page errors '+errors.join(';'));
 const ro=await browser.newContext({viewport:{width:376,height:960}});await fixtureOnly(ro);const rp=await ro.newPage();await rp.route('http://127.0.0.1:18731/api/**',r=>r.fulfill({status:200,contentType:'application/json',headers:{'Access-Control-Allow-Origin':'*'},body:JSON.stringify(r.request().url().endsWith('/toolbox')?state:r.request().url().endsWith('/insights')?insights:status)}));
 await rp.goto(uiURL);await rp.waitForTimeout(300);
 assert.equal(await rp.locator('#brightness-save').isDisabled(),true);assert.match(await rp.locator('#control-access').textContent(),/只读/);
 const storageValues=await page.evaluate(()=>[...Object.keys(sessionStorage).map(key=>sessionStorage.getItem(key)),...Object.keys(localStorage).map(key=>localStorage.getItem(key))].join('|'));assert.ok(!storageValues.includes('fixture-only-not-real'),'control token must not enter browser storage');assert.ok(!(await page.content()).includes('fixture-only-not-real'),'control token must not enter DOM');
 assert.equal(blockedRequests.length,0,'unexpected network requests '+blockedRequests.join(';'));
 console.log(JSON.stringify({passed:true,pages:7,widths:[376,320],sizes,checks:['no startup power action','missing PWM disabled','fan takeover consent + auto temperature control','fan restore original control','action errors show Chinese reason and restore controls','brightness writes','power cancel+confirm+countdown','sleep first-touch swallowed','explicit RGB bus confirmation','protected container disabled','multi-core CPU >100','container confirm action','settings save+server theme','font120 and font140/320 overflow','offline disabled','ordinary browser read-only'],pageErrors:errors,postActions:posts.map(x=>x.action)},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
