// Runs the actual page scripts against a minimal DOM surface, not a browser layout test.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const fixtures = JSON.parse(fs.readFileSync(0, 'utf8')), staticDir = process.argv[2];
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const elements = new Map(), events = new Map(), requests = [];
const element = id => {
  if(!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',addEventListener(){},contains(){return false;}});
  return elements.get(id);
};
const context = {
  URL, URLSearchParams, Date, console, Promise, Set, Map,
  location: {hash:'#/schedules',search:'?lang=zh',port:'8769',origin:'http://127.0.0.1:8769'},
  history: {replaceState(){}}, navigator: {language:'zh-CN'},
  localStorage: {getItem(){return null;},setItem(){}},
  matchMedia(){return {matches:false,addEventListener(){}};},
  document: {
    documentElement:{lang:'en',dataset:{}}, hidden:false,
    getElementById: element, querySelectorAll(){return [];},querySelector(){return null;},
    addEventListener(name,fn){events.set('doc:'+name,fn);}
  },
  addEventListener(name,fn){events.set(name,fn);},
  scrollTo(){},getSelection(){return {isCollapsed:true};},setInterval(){},
  setTimeout,clearTimeout,CSS:{escape:x=>x}
};
context.fetch = async (url, options = {}) => {
  const u = new URL(url, context.location.origin);
  requests.push(u.pathname);
  let data;
  if(options.method === 'POST') {
    const body = JSON.parse(options.body);
    let keys = body.keys || [];
    if(u.pathname === '/api/schedules/previous') {
      const task = fixtures.schedules.tasks.find(t => t.source === body.source && t.id === body.id);
      keys = task.runs.slice(1).filter(r => ['stuck','fail','miss'].includes(r.status) && !r.handled).map(r=>r.dismiss_key);
    }
    const on = u.pathname !== '/api/restore';
    for(const task of fixtures.schedules.tasks) {
      for(const run of task.runs) if(keys.includes(run.dismiss_key)) {
        run.handled = on;
        const session = fixtures.sessions.rows.find(r=>r.key === run.session_key);
        if(session) session.dismissed = on;
      }
      if(task.enabled && !task.runs.some(r=>r.status === 'run')) task.group =
        task.runs[0] && ['stuck','fail','miss'].includes(task.runs[0].status) && !task.runs[0].handled ? 'fail' : 'ok';
    }
    data = {ok:true,keys};
  }
  else if(u.pathname === '/api/sessions') data = fixtures.sessions;
  else if(u.pathname === '/api/schedules') data = fixtures.schedules;
  else if(u.pathname === '/api/session') data = fixtures.details[u.searchParams.get('key')];
  else if(u.pathname.endsWith('/diagnosis')) data = {asleep:false,launchd_loaded:true,
    desktop_running:true,dispatched:false,modified:null,disabled:null};
  else if(u.pathname.endsWith('/log')) data = {lines:[
    {stream:'stdout',text:'Starting my-app check'},
    {stream:'stderr',text:'<img src=x onerror=alert(1)> request to example.com timed out'}
  ],unavailable:false};
  return {ok:!!data,status:data?200:404,json:async()=>JSON.parse(JSON.stringify(data || {error:'missing fixture'}))};
};
context.window=context;
vm.createContext(context);
for(const name of ['i18n.js','schedules.js'])
  vm.runInContext(fs.readFileSync(path.join(staticDir,name),'utf8'),context,{filename:name});
for(const [i,match] of [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].entries())
  vm.runInContext(match[1],context,{filename:'app-script-'+i});
const sleep = () => new Promise(r => setTimeout(r,25));
const content = () => element('view').innerHTML;
const click = (attr,value) => {
  context.Schedules.click({target:{closest(selector){
    return selector==='['+attr+']' ? {getAttribute(){return value;},dataset:{scheduleRun:value}} : null;
  }}});
};
const go = async hash => {
  context.location.hash=hash;
  events.get('hashchange')();
  await sleep();
};
(async()=>{
  await sleep();
  assert(content().includes('需要处理'));
  assert.equal((content().match(/data-schedule-card=/g)||[]).length,5);
  assert.equal((content().match(/data-schedule="#\/schedules/g)||[]).length,4);
  assert.equal((content().match(/class="block /g)||[]).length,56);
  click('data-schedule-source','claude');
  assert.equal((content().match(/data-schedule="#\/schedules/g)||[]).length,2);
  click('data-schedule-sort','next');
  click('data-schedule-source','all');
  click('data-schedule-card','ok');
  assert(content().includes('未运行'));
  assert(content().includes('class="raw" title="FREQ=DAILY;BYHOUR=11;BYMINUTE=0"'));
  const css = fs.readFileSync(path.join(staticDir,'schedules.css'),'utf8');
  const rawStyle = css.match(/\.schedules \.sched \.raw\{([^}]+)\}/)[1];
  for (const rule of ['white-space:nowrap','overflow:hidden','text-overflow:ellipsis'])
    assert(rawStyle.includes(rule));
  click('data-schedule-card','off');
  assert.equal((content().match(/data-schedule="#\/schedules/g)||[]).length,2);
  assert.equal((content().match(/class="next">—<\/span>/g)||[]).length,2);
  assert(!content().includes('未运行'));
  assert(!content().includes('还没运行过'));
  const offCases = [
    {id:'unloaded-fixture',source:'launchd',loaded:false,reason:'unloaded'},
    {id:'disabled-fixture',source:'launchd',loaded:false,reason:'disabled'},
    {id:'paused-fixture',source:'claude',loaded:true,reason:'paused'}
  ];
  const base = fixtures.schedules.tasks.find(task => task.id === 'paused-check');
  for (const test of offCases) fixtures.schedules.tasks.push({...base,...test,title:test.id,
    group:'off',enabled:false,runs:[],off_reason:{m:'schedule.'+test.reason,a:[]}});
  const calendarTask = fixtures.schedules.tasks.find(task => task.id === 'com.example.cleanup');
  calendarTask.raw = 'StartCalendarInterval [{"Hour":13,"Minute":10},{"Hour":16,"Minute":50},{"Hour":19,"Minute":20}]';
  calendarTask.raw_summary = {m:'schedule.launchd_times',a:[3]};
  calendarTask.schedule = fixtures.calendar_descriptions[0].message;
  const rawTitle = calendarTask.raw.replace(/"/g,'&quot;');
  const rawLabel = `<span class="raw" title="${rawTitle}">launchd · 3 个时间点</span>`;
  await context.Schedules.refresh(true);
  click('data-schedule-card','off');
  assert.equal((content().match(/data-schedule="#\/schedules/g)||[]).length,5);
  for (const test of offCases) {
    const row = content().split(`data-schedule="#/schedules/${test.source}/${test.id}"`)[1].split('</div>')[0];
    assert(row.includes('<span class="last"><span class="faint">—</span></span>'));
    assert(!row.includes('class="chip"'));
    const reason = context.tm({m:'schedule.'+test.reason,a:[]});
    assert.equal(row.split(reason).length-1,1);
    if (test.reason === 'disabled') assert(!row.includes(context.t('schedule.unloaded')));
  }
  click('data-schedule-card','ok');
  assert(content().includes(rawLabel));
  assert(content().includes('每天 13:10、16:50、19:20'));
  await go('#/schedules/launchd/com.example.cleanup');
  assert(content().includes(rawLabel));
  assert(!content().includes('>StartCalendarInterval'));
  await go('#/schedules');
  click('data-schedule-card','svc');
  assert(content().includes('com.example.preview'));
  await go('#/schedules/launchd/com.example.backup?run=1');
  assert(content().includes('class="rr sel"'));
  assert(content().includes('Starting my-app check'));
  assert(content().includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!content().includes('<img src=x'));
  click('data-schedule-run','1');
  assert(!content().includes('class="rr sel"'));
  await go('#/schedules/claude/review-plan');
  assert(content().includes('去回复'));
  assert(content().includes('待你处理'));
  assert(content().includes('下一步'));
  const reviewFixture = fixtures.schedules.tasks.find(t=>t.id==='review-plan');
  assert(reviewFixture.runs[0].items.length > 0);
  assert(content().includes(vm.runInContext('esc(tm(' + JSON.stringify(reviewFixture.runs[0].items[0].text) + '))',context)));
  assert(content().includes('把之前的都标为已处理'));
  assert(content().includes('✓ 已处理'));
  assert(!content().includes('卡住，等你回复'));
  assert.equal(requests.filter(p=>p.endsWith('/diagnosis')).length,0);
  const target=content().match(/class="alert[\s\S]*?href="([^"]+)"/)[1];
  await go(target);
  assert(content().includes('Refactor billing webhooks'));
  await go('#/schedules/claude/review-plan');
  const review = fixtures.schedules.tasks.find(t=>t.id==='review-plan');
  click('data-schedule-previous','review-plan');
  await sleep();
  assert(review.runs.slice(1).filter(r=>r.status==='stuck').every(r=>r.handled));
  assert(!review.runs[0].handled);
  assert(!content().includes('data-schedule-previous'));
  click('data-schedule-handle','1');
  await sleep();
  assert.equal(review.group,'ok');
  assert(fixtures.sessions.rows.find(r=>r.key===review.runs[0].session_key).dismissed);
  assert(content().includes('✓ 已处理'));
  assert(!context.Schedules.nav(true).includes('>4</span>'));
  click('data-schedule-handle','1');
  await sleep();
  assert.equal(review.group,'fail');
  assert(!fixtures.sessions.rows.find(r=>r.key===review.runs[0].session_key).dismissed);
  await go('#/schedules/launchd/com.example.sync?run=1');
  assert(content().includes('已连续漏跑 2 次'));
  assert(content().includes('触发时电脑是否睡眠'));
  assert(content().includes('当时 launchd 是否已加载'));
  assert(content().includes('未知') === false); // Both fake launchd evidence fields are known.
  const diagnosisCount = requests.filter(p=>p.endsWith('/diagnosis')).length;
  await go('#/schedules');
  await go('#/schedules/launchd/com.example.sync?run=1');
  assert.equal(requests.filter(p=>p.endsWith('/diagnosis')).length,diagnosisCount);
  await go('#/schedules/claude/content-check?run=2');
  assert(content().includes('重新登录 Claude 后重跑'));
  await go('#/schedules/claude/content-check?run=1');
  assert(content().includes('检查网络后'));
  await go('#/schedules/claude/content-check?run=3');
  assert(content().includes('Unexpected response: example fixture error'));
  assert(!content().includes('重新登录 Claude 后重跑'));
  const continued = fixtures.schedules.tasks.find(task => task.id === 'test-check');
  continued.runs[0].duration_unknown = true;
  continued.runs[0].end = continued.runs[0].start + 72 * 3600;
  const baseline = fixtures.schedules.tasks.find(task => task.id === 'com.example.cleanup');
  baseline.runs = [{id:'baseline',start:null,end:Date.now()/1000,status:'ok',before_monitoring:true,
    approximate:true,summary:{m:'schedule.before_monitoring_result',a:[{m:'schedule.exit',a:[0]}]}}];
  await context.Schedules.refresh(true);
  await go('#/schedules/claude/test-check');
  assert(/data-schedule-run="1">[\s\S]*?<td class="hide-m">—<\/td>/.test(content()));
  await go('#/schedules/launchd/com.example.cleanup');
  assert(content().includes('<td>监控前</td>'));
  assert(content().includes('开始监控前的最后一次：退出码 0'));
  assert(content().includes('<td class="hide-m">—</td>'));
  assert(content().includes('结束时间按日志修改时间估算'));
  const paused = {...base,id:'inactive-period',title:'Inactive fixture',source:'claude',
    enabled:true,group:'ok',runs:[],state_history_unavailable:false,
    disabled_periods:[{start:new Date('2026-11-04T10:26:21').getTime()/1000,
      end:new Date('2026-11-06T13:58:16').getTime()/1000,inferred:false,end_inferred:true}]};
  fixtures.schedules.tasks.push(paused);
  await context.Schedules.refresh(true);
  await go('#/schedules');
  click('data-schedule-card','ok');
  const pausedRow = content().split('data-schedule="#/schedules/claude/inactive-period"')[1].split('</div>')[0];
  assert.equal((pausedRow.match(/class="block b-paused"/g)||[]).length,1);
  assert(!pausedRow.includes('b-miss'));
  await go('#/schedules/claude/inactive-period');
  assert.equal((content().match(/class="pause-note"/g)||[]).length,1);
  assert(content().includes('停用期间（11/4 10:26 - 11/6 13:58），未触发 · 推断'));
  assert(!content().includes('class="alert'));
  assert(!content().includes('data-schedule-handle'));
  assert(!content().includes('data-schedule-run='));
  assert(content().includes('class="big s-fail">0</span>漏跑'));
  paused.runs = [{id:'real-miss',start:new Date('2026-11-09T11:00:00').getTime()/1000,
    status:'miss',summary:{m:'schedule.missed',a:[]},state_inferred:true,state_unconfirmed:true}];
  paused.group = 'fail';
  paused.state_history_unavailable = true;
  await context.Schedules.refresh(true);
  await go('#/schedules/claude/inactive-period?run=real-miss');
  assert(content().includes('无法确认当时是否停用'));
  assert.equal((content().match(/class="pause-note"/g)||[]).length,1);
  assert(content().includes('class="big s-fail">1</span>漏跑'));
  const nonAttention = {...base,id:'non-attention',title:'Non-attention fixture',source:'claude',
    enabled:true,group:'ok',runs:['ok','run','unknown'].flatMap(status =>
      [true,false].map(handled => ({id:status+'-'+handled,start:Date.now()/1000,end:Date.now()/1000+1,
        status,handled,handled_reason:handled?'replied':null,summary:'Fake '+status,
        dismiss_key:'schedule:'+status+'-'+handled,session_key:null})))};
  fixtures.schedules.tasks.push(nonAttention);
  await context.Schedules.refresh(true);
  await go('#/schedules/claude/non-attention');
  for (const run of nonAttention.runs) {
    const row = content().split(`data-schedule-run="${run.id}"`)[1].split('</tr>')[0];
    assert(!row.includes('handled-label'),run.id);
    assert(!row.includes('已处理'),run.id);
    assert(!row.includes('<button'),run.id);
    assert(!row.includes('撤销'),run.id);
    assert(!row.includes('忽略这次'),run.id);
  }
  assert(!content().includes('data-schedule-previous'));
  const postCount = requests.filter(p=>p==='/api/dismiss'||p==='/api/restore').length;
  for (const run of nonAttention.runs) click('data-schedule-handle',run.id);
  await sleep();
  assert.equal(requests.filter(p=>p==='/api/dismiss'||p==='/api/restore').length,postCount);
  await go('#/schedules');
  click('data-schedule-card','ok');
  const nonAttentionRow = content().split('data-schedule="#/schedules/claude/non-attention"')[1].split('</div>')[0];
  assert(!nonAttentionRow.includes('已处理'));
  const blocks = [...nonAttentionRow.matchAll(/<a class="block b-(ok|run|unknown)[\s\S]*?<\/a>/g)];
  assert.equal(blocks.length,nonAttention.runs.length);
  for (const [block] of blocks) {
    assert(!block.includes('handled'));
    assert(!block.includes('✓'));
  }
  assert.equal(blocks.filter(([block])=>block.startsWith('<a class="block b-ok"')).length,2);
  assert(css.includes('.schedules .b-ok{background:var(--ok-bar)}'));
  assert.deepEqual(Object.keys(context.I18N.en).sort(),Object.keys(context.I18N.zh).sort());
  assert.equal(vm.runInContext("t('schedule.weekdays_at','16:00')",context),'工作日 16:00');
  assert.equal(vm.runInContext("t('schedule.unloaded')",context),'未加载（launchctl 里没有）');
  assert.equal(vm.runInContext("t('schedule.disabled')",context),'plist 里设了 Disabled');
  for (const test of [...fixtures.calendar_descriptions,...fixtures.once_descriptions])
    assert.equal(context.tm(test.message),test.zh);
  assert.equal(context.t('schedule.launchd_times',1),'launchd · 1 个时间点');
  assert.equal(context.t('schedule.every_hours',2),'每 2 小时');
  await go('#/schedules');
  vm.runInContext("setLang('en');setTheme('dark')",context);
  assert.equal(vm.runInContext("t('schedule.weekdays_at','16:00')",context),'Weekdays at 16:00');
  assert(content().includes('Scheduled tasks'));
  assert.equal(context.document.documentElement.dataset.theme,'dark');
  assert(!/schedule\.[a-z_]+/.test(content()));
  for (const test of [...fixtures.calendar_descriptions,...fixtures.once_descriptions])
    assert.equal(context.tm(test.message),test.en);
  assert.equal(context.t('schedule.launchd_times',1),'launchd · 1 time');
  assert.equal(context.t('schedule.every_hours',2),'Every 2 hours');
  click('data-schedule-card','ok');
  assert(content().includes('>launchd · 3 times</span>'));
  assert(content().includes('Daily 13:10, 16:50, 19:20'));
  click('data-schedule-card','off');
  assert(!content().includes('Not run'));
  assert(!content().includes('Never run'));
  await go('#/schedules/claude/inactive-period?run=real-miss');
  assert(content().includes('Disabled (11/4 10:26 - 11/6 13:58), not triggered · Inferred'));
  assert(content().includes('Cannot confirm whether the task was disabled at that time'));
  assert(!content().includes('停用期间'));
  process.stdout.write('PASS: actual page scripts; cards, filters, sort, history, off/service states, logs, session route, continued duration, baseline, raw truncation/title, merged calendars, local one-time dates, off badges, inactive intervals/neutral notes/counts, missing-state guidance, non-attention rows without handling controls or checked blocks, en/zh key parity and theme\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
