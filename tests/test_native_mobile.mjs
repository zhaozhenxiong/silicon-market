// Pure mocks: no helper executable, screen capture, UI input, or worker main runs.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {NativeMobileApp} from '../automation/native_mobile_adapter.mjs';
import {NativeMobileRunner,NativeMobileWorker,approvedCatalog,screenStatus,verifyMarketScope,parseArgs} from '../automation/native_mobile_runner.mjs';

const bundle='com.apple.ScreenContinuity';
const ready={ok:true,bundleIdentifier:bundle,ready:true,appRunning:true,windowFound:true,permissions:{accessibility:true,screenRecording:true}};
const target={id:4,enabled:true,status:'partial',model:'i9-14900KF',query:'14900kf',market_scope_id:'scope4',scope:{model:'i9-14900KF',storage:null,condition:null,filters:{},verified:true,aliases:['14900kf']}};
const line=(text,x,y,width=.15,height=.02)=>({text,x,y,width,height});
const market=[line('14900kf',.2,.13),line('14900kf',.3,.21),
  line('品牌',.15,.28),line('+选择',.15,.31),line('功能状态',.43,.28),line('+选择',.43,.31),
  line('成色',.73,.28),line('+选择',.73,.31),line('07.31近7日成交均价',.15,.39)];

async function temporary(fn){const root=await fs.mkdtemp(path.join(os.tmpdir(),'native-mobile-test-'));try{return await fn(root);}finally{await fs.rm(root,{recursive:true,force:true});}}

test('adapter invokes only fixed helper commands with argument arrays and PNG pixels',async()=>temporary(async(root)=>{
  const calls=[];const execute=async(file,args,options)=>{
    calls.push({file,args,options});let payload={ok:true,bundleIdentifier:bundle,windowId:7,imageWidth:700,imageHeight:1400,frame:{x:1,y:2,width:350,height:700}};
    if(args[0]==='screenshot'){
      const png=Buffer.alloc(24);Buffer.from([137,80,78,71,13,10,26,10]).copy(png);png.writeUInt32BE(700,16);png.writeUInt32BE(1400,20);
      await fs.writeFile(args[1],png);payload.path=args[1];
    }else if(args[0]==='health')payload=ready;
    return {stdout:JSON.stringify(payload)};
  };
  const app=new NativeMobileApp({projectDir:root,execute});
  await assert.rejects(()=>app.click([1,2]),/截图/);
  await assert.rejects(()=>app.typeText('14900kf'),/截图/);
  assert.equal((await app.health()).ready,true);
  await app.getAXState();const image=await app.getScreenshot();assert.equal(image.readUInt32BE(16),700);
  await app.click([350,700]);await app.typeText('14900kf $(never-shell)');
  assert.equal(calls.at(-1).args[1],'14900kf $(never-shell)');
  assert.ok(calls.every(c=>c.file===path.join(root,'output/bin/native-mobile')&&!('shell' in c.options)));
  assert.deepEqual(calls.map(c=>c.args[0]),['health','state','screenshot','click','type']);
  await assert.rejects(()=>app.click([700,2]),/超出/);
  app.stop();await assert.rejects(()=>app.health(),/已停止/);
}));

test('adapter rejects any helper target other than iPhone Mirroring',async()=>{
  const app=new NativeMobileApp({projectDir:'/tmp/mock',execute:async()=>({stdout:JSON.stringify({...ready,bundleIdentifier:'other.app'})})});
  await assert.rejects(()=>app.health(),/目标不是/);
});

test('scope approval never accepts truthy strings or unreviewed states',()=>{
  assert.ok(approvedCatalog(target));
  for(const status of ['pending','needs_review','no_market','unsupported'])assert.equal(approvedCatalog({...target,status}),false);
  assert.equal(approvedCatalog({...target,scope:{...target.scope,verified:'true'}}),false);
  assert.equal(approvedCatalog({...target,enabled:false}),false);
});

test('actual market title and default filters must both be visible',()=>{
  assert.equal(verifyMarketScope(target,market).ok,true);
  assert.equal(verifyMarketScope(target,market.map(l=>l.text==='14900kf'&&l.y===.21?{...l,text:'14900kes'}:l)).ok,false);
  assert.equal(verifyMarketScope(target,market.filter(l=>!(l.text==='+选择'&&l.x===.73))).ok,false);
  const storage={...target,scope:{...target.scope,storage:'256GB'}};
  assert.equal(verifyMarketScope(storage,[...market,line('256GB',.2,.13)]).ok,false); // query text is not filter proof
});

test('phone model and two chips on the same row retain real condition proof',()=>{
  const phone={...target,id:12,model:'iPhone 15 256GB',query:'iphone15',
    scope:{verified:true,storage:'256GB',condition:'少量磕碰划痕',filters:{},aliases:['苹果 iPhone 15','iPhone 15']}};
  const actual=[line('iphone15',.2,.13),line('苹果 iPhone 15',.3,.218,.36,.035),
    line('256GB',.15,.303),line('少量磕碰划痕',.4,.303,.3),line('07.31近7日成交均价',.1,.4)];
  assert.equal(verifyMarketScope(phone,actual).ok,true);
  assert.equal(verifyMarketScope(phone,actual.filter(l=>l.text!=='少量磕碰划痕')).ok,false);
  assert.equal(verifyMarketScope(phone,actual.map(l=>l.text==='256GB'?{...l,text:'1256GB'}:l)).ok,false);
});

test('8GB scope cannot inherit an actual 48GB chip by substring',()=>{
  const gpu={...target,scope:{...target.scope,storage:'8GB'}};
  assert.equal(verifyMarketScope(gpu,[...market,line('48GB',.2,.3)]).ok,false);
});

test('locked, disconnected and verification screens cannot advertise ready',()=>{
  assert.equal(screenStatus([line('iPhone 镜像已锁定',.1,.2)]).status,'locked');
  assert.equal(screenStatus([line('连接已断开',.1,.2)]).status,'disconnected');
  assert.equal(screenStatus([line('请完成安全验证',.1,.2)]).status,'verification');
  assert.equal(screenStatus([line('other unrelated app',.1,.2)]).status,'waiting_user');
});

function workerMock(catalogs){
  const calls=[];return {calls,remaining:[],lease:null,
    async assertTargetAllowed(id){assert.ok(catalogs.some(c=>c.id===id));return {catalog_id:id,task_ids:[1]};},
    async openSearch(){return {status:'needs_ai_guidance',message:'纯mock未知导航',navigation:{status:'needs_ai_guidance',screen_kind:'unknown'}};},
    async api(url,payload){calls.push({url,payload});if(url==='/api/mobile/targets')return {policy:'enabled_live_monitor_tasks_only',targets:catalogs.map(c=>({catalog_id:c.id,task_ids:[1],enabled:true,market_scope_id:c.market_scope_id}))};if(url==='/api/workflow/status')return {mobile:{operator_paused:false}};if(url==='/api/catalog')return {items:catalogs};return {ok:true};},
    async heartbeat(status,message){calls.push({heartbeat:status,message});},
    async release(status,message){calls.push({release:status,message});this.remaining=[];this.lease=null;},
    async inspectScreen(){calls.push({inspect:true});return {lines:market};},
    async prepare(catalog){calls.push({prepare:catalog.id});return {done:true};},
  };
}

test('persistent operator pause is checked before every native health or screenshot call',async()=>temporary(async(root)=>{
  const worker=workerMock([target]);const original=worker.api;let nativeCalls=0;
  worker.api=async(url,payload)=>url==='/api/workflow/status'?{mobile:{operator_paused:true,pause_reason:'用户手机已取走'}}:original(url,payload);
  worker.catalog=target;worker.remaining=[{id:1}];worker.lease='mock-lease';
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>{nativeCalls++;return ready;}},log:()=>{}});
  assert.deepEqual(await runner.cycle(),{wait:true,operator_paused:true});
  assert.deepEqual(await runner.cycle(),{wait:true,operator_paused:true});
  assert.equal(nativeCalls,0);assert.equal(worker.calls.some(c=>c.inspect||c.prepare||c.heartbeat==='ready'),false);
  assert.equal(worker.remaining.length,0);
  const state=JSON.parse(await fs.readFile(runner.statePath));assert.equal(state.phase,'operator_paused');assert.equal(state.connected,false);assert.match(state.message,/手机已取走/);
}));

test('missing project pause API fails closed before checking the native device',async()=>temporary(async(root)=>{
  const worker=workerMock([target]),original=worker.api;worker.api=async(url,payload)=>url==='/api/workflow/status'?{}:original(url,payload);let nativeCalls=0;
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>{nativeCalls++;return ready;}},log:()=>{}});
  await assert.rejects(()=>runner.cycle(),/项目手机暂停状态不可用/);assert.equal(nativeCalls,0);
}));

test('missing screen permission does not capture, navigate, claim or heartbeat ready',async()=>temporary(async(root)=>{
  const worker=workerMock([target]);const app={health:async()=>({...ready,ready:false,permissions:{accessibility:true,screenRecording:false}})};
  const runner=new NativeMobileRunner({projectDir:root,worker,app,log:()=>{}});
  assert.deepEqual(await runner.cycle(),{wait:true});
  assert.ok(worker.calls.every(c=>!c.inspect&&!c.prepare&&c.heartbeat!=='ready'&&c.url!=='/api/mobile/claim'));
  const state=JSON.parse(await fs.readFile(runner.statePath));assert.equal(state.phase,'waiting_permission');assert.equal(state.connected,false);
}));

test('zero enabled monitors waits before pause API, native health, screenshots or guidance creation',async()=>temporary(async(root)=>{
  const worker=workerMock([]);let healthCalls=0;
  const runner=new NativeMobileRunner({projectDir:root,worker,discover:true,app:{health:async()=>{healthCalls++;throw new Error('must not run');}},log:()=>{}});
  assert.deepEqual(await runner.cycle(),{wait:true,waiting_tasks:true});
  assert.equal(healthCalls,0);assert.deepEqual(worker.calls.map(c=>c.url),['/api/mobile/targets']);
  const state=JSON.parse(await fs.readFile(runner.statePath));assert.equal(state.phase,'waiting_tasks');assert.equal(state.connected,false);assert.equal(state.mode,'monitored');
  await assert.rejects(()=>fs.stat(path.join(root,'data/native_mobile_guidance.json')),{code:'ENOENT'});
}));

test('monitored and legacy discover modes filter all catalogue access to actual monitor targets',async()=>temporary(async(root)=>{
  const other={...target,id:39,market_scope_id:'unmonitored-scope'};
  const worker=workerMock([target,other]),original=worker.api;
  worker.api=async(url,payload)=>url==='/api/mobile/targets'?{policy:'enabled_live_monitor_tasks_only',targets:[{catalog_id:4,task_ids:[8,2,8],enabled:true,market_scope_id:target.market_scope_id}]}:original(url,payload);
  const runner=new NativeMobileRunner({projectDir:root,worker,discover:true,app:{health:async()=>ready},log:()=>{}});
  await runner.cycle();assert.deepEqual(worker.calls.filter(c=>c.prepare).map(c=>c.prepare),[4]);
  assert.deepEqual(runner.targets.get(4).task_ids,[2,8]);
  assert.equal(worker.calls.some(c=>c.url==='/api/catalog/39'),false);
}));

test('single mode also rejects an unmonitored catalogue before touching the phone',async()=>temporary(async(root)=>{
  const worker=workerMock([target]);let healthCalls=0;
  const runner=new NativeMobileRunner({projectDir:root,worker,catalogId:39,app:{health:async()=>{healthCalls++;return ready;}},log:()=>{}});
  assert.equal((await runner.cycle()).waiting_tasks,true);assert.equal(healthCalls,0);
}));

test('removing an active monitor releases its lease before any further device check',async()=>temporary(async(root)=>{
  const worker=workerMock([target]),original=worker.api;
  worker.catalog=target;worker.lease='active-lease';worker.remaining=[{id:1}];
  worker.api=async(url,payload)=>url==='/api/mobile/targets'?{policy:'enabled_live_monitor_tasks_only',targets:[]}:original(url,payload);
  let nativeCalls=0;const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>{nativeCalls++;return ready;}},log:()=>{}});
  assert.equal((await runner.cycle()).waiting_tasks,true);
  assert.equal(worker.lease,null);assert.equal(worker.remaining.length,0);assert.equal(nativeCalls,0);
  assert.ok(worker.calls.some(c=>c.release==='waiting_user'));assert.equal(worker.calls.some(c=>c.inspect),false);
}));

test('monitor disabled after preparation prevents the next read chunk',async()=>temporary(async(root)=>{
  const worker=workerMock([target]),original=worker.api;let refreshes=0,readCalls=0;
  worker.api=async(url,payload)=>url==='/api/mobile/targets'?{policy:'enabled_live_monitor_tasks_only',targets:++refreshes>=3?[]:[{catalog_id:4,task_ids:[1],enabled:true}]}:original(url,payload);
  worker.prepare=async()=>{worker.catalog=target;worker.remaining=[{id:1}];worker.lease='just-claimed';return {done:false};};
  worker.runChunk=async()=>{readCalls++;return {results:[]};};
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  assert.equal((await runner.cycle()).waiting_tasks,true);assert.equal(readCalls,0);assert.equal(worker.lease,null);
}));

test('native claim refuses a missing target without heartbeat or native input',async()=>temporary(async(root)=>{
  const worker=new NativeMobileWorker({projectDir:root,app:{}});worker.getMonitoredTargets=async()=>new Map();
  worker.api=async()=>{throw new Error('claim API must not run');};
  await assert.rejects(()=>worker.claim(4),error=>error.code==='TARGET_NOT_MONITORED');
}));

test('reading submit independently rechecks monitor authorization',async(t)=>temporary(async(root)=>{
  const calls=[];t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/api/mobile/targets')?{policy:'enabled_live_monitor_tasks_only',targets:[]}:{ok:true}};
  });
  const worker=new NativeMobileWorker({projectDir:root,app:{}});worker.catalog=target;worker.lease='lease';worker.remaining=[{id:1}];
  await assert.rejects(()=>worker.api('/api/mobile/readings',{readings:[]}),error=>error.code==='TARGET_NOT_MONITORED');
  assert.equal(calls.some(c=>c.url.endsWith('/api/mobile/readings')),false);assert.equal(worker.lease,null);
}));

test('another application in front pauses without screenshot, input or automatic focus',async()=>temporary(async(root)=>{
  const worker=workerMock([target]);const runner=new NativeMobileRunner({projectDir:root,worker,
    app:{health:async()=>({...ready,targetFrontmost:false})},log:()=>{}});
  assert.deepEqual(await runner.cycle(),{wait:true});
  assert.equal(worker.calls.some(c=>c.inspect||c.prepare||c.heartbeat==='ready'),false);
  const state=JSON.parse(await fs.readFile(runner.statePath));assert.equal(state.connected,false);assert.equal(state.device_status,'waiting_user');
}));

test('unverified monitored navigation uncertainty does not permanently mark the catalogue',async()=>temporary(async(root)=>{
  const pending={...target,status:'pending',scope:{...target.scope,verified:false}};const worker=workerMock([pending]);
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  await runner.cycle();assert.equal(worker.calls.some(c=>c.prepare),false);
  assert.equal(worker.calls.some(c=>c.url==='/api/catalog/4'),false);
  assert.equal(JSON.parse(await fs.readFile(runner.statePath)).phase,'needs_ai_guidance');
}));

test('scope failure releases lease and is not overwritten as connected idle',async()=>temporary(async(root)=>{
  const worker=workerMock([target]);worker.prepare=async()=>{worker.lease='mock-lease';throw new Error('实际成色未核对');};
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  await runner.cycle();const state=JSON.parse(await fs.readFile(runner.statePath));
  assert.equal(state.connected,false);assert.equal(state.phase,'waiting_device');
  assert.ok(worker.calls.some(c=>c.release==='waiting_user'));
  assert.ok(worker.calls.some(c=>c.url==='/api/catalog/4'&&c.payload.status==='needs_review'));
}));

test('an existing lease is checked and resumed for its own catalogue only',async()=>temporary(async(root)=>{
  const phone={...target,id:12,market_scope_id:'scope12'};const worker=workerMock([target,phone]);
  worker.catalog=phone;worker.remaining=[{id:7}];worker.lease='mock-lease';
  worker.runChunk=async({catalogId})=>{assert.equal(catalogId,12);worker.remaining=[];worker.lease=null;return {results:[{date:'2026-10-01'}]};};
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  await runner.cycle();assert.equal(worker.calls.some(c=>c.prepare),false);
}));

test('finishing a scope lease rotates the next selection instead of immediately reclaiming it',async()=>temporary(async(root)=>{
  const other={...target,id:12,market_scope_id:'other-scope'};const worker=workerMock([target,other]);
  worker.catalog=target;worker.remaining=[{id:1}];worker.lease='mock-lease';
  worker.runChunk=async()=>{worker.remaining=[];worker.lease=null;return {results:[{date:'2026-10-01'}]};};
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  await runner.cycle();assert.equal(runner.lastCompletedCatalog,4);
  await runner.cycle();assert.deepEqual(worker.calls.filter(c=>c.prepare).map(c=>c.prepare),[12,4]);
}));

test('an already leased phone cannot continue after the visible capacity changes',async()=>temporary(async(root)=>{
  const phone={...target,model:'iPhone 15 256GB',query:'iphone15',scope:{...target.scope,storage:'256GB',condition:'少量磕碰划痕',aliases:['苹果 iPhone 15']}};
  const worker=workerMock([phone]);worker.catalog=phone;worker.remaining=[{id:1}];worker.lease='mock-lease';
  worker.inspectScreen=async()=>({lines:[line('苹果 iPhone 15',.3,.218,.3,.035),line('512GB',.15,.303),line('少量磕碰划痕',.4,.303),line('近7日成交均价',.1,.4)]});
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{health:async()=>ready},log:()=>{}});
  assert.deepEqual(await runner.cycle(),{wait:true});assert.equal(worker.remaining.length,0);
  assert.equal(worker.calls.some(c=>c.prepare||c.heartbeat==='ready'),false);
}));

test('native claims are one during discovery and forty only after selection policy changes',async()=>temporary(async(root)=>{
  const worker=new NativeMobileWorker({projectDir:root,app:{}});const limits=[];
  worker.heartbeat=async()=>{};worker.api=async(url,payload)=>{
    if(url==='/api/mobile/targets')return {policy:'enabled_live_monitor_tasks_only',targets:[{catalog_id:4,task_ids:[1],enabled:true}]};
    if(url==='/api/mobile/claim'){limits.push(payload.limit);return {lease_token:'test',catalog:target,jobs:[{id:1,batch_id:1}]};}
    return {batches:[{id:1,end_date:'2026-10-29',days:180}]};
  };
  await worker.claim(4);worker.remaining=[];worker.claimLimit=40;await worker.claim(4);
  assert.deepEqual(limits,[1,40]);
}));

test('reading submit rechecks current approval and scope without requiring the old query',async(t)=>temporary(async(root)=>{
  let current={...target,query:'corrected navigation'};const calls=[];
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/api/mobile/targets')?{policy:'enabled_live_monitor_tasks_only',targets:[{catalog_id:4,task_ids:[1],enabled:true}]}:url.endsWith('/api/catalog')?{items:[current]}:{accepted:1}};
  });
  const worker=new NativeMobileWorker({projectDir:root,app:{}});worker.catalog=target;
  assert.equal((await worker.api('/api/mobile/readings',{readings:[]})).accepted,1);
  const submitted=()=>calls.filter(c=>c.url.endsWith('/api/mobile/readings')).length;
  assert.equal(submitted(),1);
  current={...current,status:'needs_review'};
  await assert.rejects(()=>worker.api('/api/mobile/readings',{readings:[]}),/批准已撤销/);
  current={...target,market_scope_id:'changed'};
  await assert.rejects(()=>worker.api('/api/mobile/readings',{readings:[]}),/批准已撤销或变更/);
  assert.equal(submitted(),1);
}));

test('runner cleanup releases claimed work and writes stopped without starting real helper',async()=>temporary(async(root)=>{
  const worker=workerMock([]);const runner=new NativeMobileRunner({projectDir:root,worker,
    app:{health:async()=>({...ready,ready:false,permissions:{accessibility:false,screenRecording:false}})},log:()=>{}});
  await runner.run({once:true});
  const state=JSON.parse(await fs.readFile(runner.statePath));assert.equal(state.phase,'stopped');assert.equal(state.connected,false);
  assert.ok(worker.calls.some(c=>c.release==='disconnected'));
}));

test('stop during a cycle releases immediately and does not enter another wait',async()=>temporary(async(root)=>{
  const worker=workerMock([]);let nativeStopped=false,slept=false;
  const runner=new NativeMobileRunner({projectDir:root,worker,app:{stop(){nativeStopped=true;}},sleep:async()=>{slept=true;},log:()=>{}});
  runner.cycle=async()=>{runner.stop();return {didWork:false};};
  await runner.run();assert.equal(nativeStopped,true);assert.equal(slept,false);
  assert.ok(worker.calls.some(c=>c.release==='disconnected'));
}));

test('preparation applies the approved scope before reading actual chips',async()=>temporary(async(root)=>{
  const worker=new NativeMobileWorker({projectDir:root,app:{}});const order=[];
  worker.getMonitoredTargets=async()=>new Map([[target.id,{catalog_id:target.id,task_ids:[1],enabled:true}]]);
  worker.claim=async()=>{worker.remaining=[{id:1}];worker.catalog={...target};};
  worker.openSearch=async()=>{order.push('search');return {status:'market',lines:market};};
  worker.applyScope=async(scope)=>{order.push('scope');assert.equal(scope.verified,true);};
  worker.inspectScreen=async()=>{order.push('read_after_scope');return {lines:market};};
  assert.deepEqual(await worker.prepare(target),{done:false});
  assert.deepEqual(order,['search','scope','read_after_scope']);assert.equal(worker.remaining.length,1);
}));

test('same-scope corrected search entry works without rewriting frozen query or jobs',async()=>temporary(async(root)=>{
  const frozen={...target,model:'RTX4090',query:'RTX4090',market_scope_id:'same-economic-scope'};
  const current={...frozen,query:'4090',scope:{...frozen.scope,model:'RTX4090',aliases:['4090'],verified:true}};
  const gpuLines=market.map(l=>l.text==='14900kf'?{...l,text:'4090'}:l);
  const worker=new NativeMobileWorker({projectDir:root,app:{getAXState:async()=>''}});let navigated,payload;
  worker.getMonitoredTargets=async()=>new Map([[target.id,{catalog_id:target.id,task_ids:[1],enabled:true}]]);
  worker.claim=async()=>{worker.catalog=structuredClone(frozen);worker.lease='mock-lease';worker.remaining=[{id:1,target_date:'2026-10-01',query:'RTX4090'}];};
  worker.openSearch=async(query)=>{navigated=query;return {status:'market',lines:gpuLines};};
  worker.applyScope=async()=>{};worker.inspectScreen=async()=>({lines:gpuLines});
  await worker.prepare(current);
  assert.equal(navigated,'4090');assert.equal(worker.catalog.query,'RTX4090');
  assert.equal(worker.catalog.navigationQuery,'4090');assert.equal(worker.remaining[0].query,'RTX4090');
  worker.calibrate=async()=>{};worker.heartbeat=async()=>{};worker.checkpoint=async()=>{};
  worker.locate=async()=>({status:'read',displayed_date:'2026-10-01',price:12000,evidence_filename:'real-screen.png',observed_model:'4090',observed_query:'4090',verification_count:2});
  worker.api=async(url,body)=>{payload=body;return {accepted:1};};
  const result=await worker.runChunk({maxJobs:1});
  assert.equal(result.results.length,1);assert.equal(payload.readings[0].observed_query,null);
  assert.equal(payload.readings[0].observed_model,'4090');assert.deepEqual(payload.readings[0].scope.aliases,['4090']);
}));

test('CLI permits only local endpoint and strict scalar flags',()=>{
  assert.deepEqual(parseArgs(['--base-url','http://127.0.0.1:6772','--catalog-id','4','--once']),{baseUrl:'http://127.0.0.1:6772',workerId:'native-mobile-v1',catalogId:4,once:true,discover:false,monitored:true});
  assert.equal(parseArgs([]).monitored,true);assert.equal(parseArgs(['--monitored']).monitored,true);
  assert.equal(parseArgs(['--discover']).discover,true);
  assert.throws(()=>parseArgs(['--discover','--catalog-id','4']));
  for(const args of [['--base-url','https://example.com'],['--base-url','http://user:pass@localhost:1'],['--catalog-id','4;cmd'],['--helper-path','evil']])assert.throws(()=>parseArgs(args));
});
