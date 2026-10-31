// Pure saved-layout/API mocks: never opens a phone, helper, or worker process.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {normalizeDiscoveryQuery,verifyDiscoveryScreen,MobileScopeDiscovery} from '../automation/mobile_scope_discovery.mjs';
import {NativeMobileRunner} from '../automation/native_mobile_runner.mjs';
const line=(text,x,y,width=.15,height=.02)=>({text,x,y,width,height});
const catalog={id:7,version:1,enabled:true,status:'pending',model:'i9-14900KF',query:'i9-14900KF',market_scope_id:'unchanged-scope',
  scope:{model:'i9-14900KF',storage:null,condition:null,filters:{},verified:false}};
const lines=[line('14900kf',.2,.13),line('14900kf',.3,.218),line('品牌',.15,.28),line('+选择',.15,.31),
  line('功能状态',.43,.28),line('+选择',.43,.31),line('成色',.73,.28),line('+选择',.73,.31),line('近7日成交均价',.15,.39),
  line('05.01',.1,.7),line('07.30',.4,.7),line('10.29',.8,.7)];
const screen={status:'market',filename:'actual-screen.png',lines};
async function temporary(fn){const root=await fs.mkdtemp(path.join(os.tmpdir(),'mobile-discovery-test-'));try{return await fn(root);}finally{await fs.rm(root,{recursive:true,force:true});}}

test('normalization removes only known leading i9/RTX and keeps complete suffix/tier',()=>{
  assert.equal(normalizeDiscoveryQuery({query:'i9-14900KF'}),'14900KF');
  assert.equal(normalizeDiscoveryQuery({query:'RTX 4070 TI SUPER 16GB'}),'4070 TI SUPER 16GB');
  assert.equal(normalizeDiscoveryQuery({query:'R7 7800X3D'}),'R7 7800X3D');
  assert.equal(normalizeDiscoveryQuery({query:'DDR5 32GB (16GB×2) 6000'}),'DDR5 32GB (16GB×2) 6000');
});

test('an ASCII canonical model replaces only a pure Chinese category hint suffix',()=>{
  assert.equal(normalizeDiscoveryQuery({model:'B650',query:'B650 主板'}),'B650');
  assert.equal(normalizeDiscoveryQuery({model:'RTX 4070 TI SUPER 16GB',query:'RTX4070TISUPER16GB 显卡'}),'4070 TI SUPER 16GB');
  assert.equal(normalizeDiscoveryQuery({model:'i9-14900KF',query:'i9-14900KF 处理器'}),'14900KF');
  assert.equal(normalizeDiscoveryQuery({model:'Ryzen 7800X3D',query:'R7 7800X3D'}),'R7 7800X3D');
  assert.equal(normalizeDiscoveryQuery({model:'B650',query:'B650 主板 全新'}),'B650 主板 全新');
  assert.equal(normalizeDiscoveryQuery({model:'B650M',query:'B650 主板'}),'B650 主板');
  assert.equal(normalizeDiscoveryQuery({model:'DDR5 32GB 6000',query:'DDR5 32GB 6400 内存'}),'DDR5 32GB 6400 内存');
});

test('complete title, every default selector and real half-year axis can establish scope',()=>{
  const proof=verifyDiscoveryScreen(catalog,screen,{endDate:'2026-10-29'});
  assert.equal(proof.ok,true);assert.equal(proof.observed_model,'14900kf');assert.ok(proof.aliases.includes('14900kf'));
  assert.equal(proof.axis.start,'2026-05-01');assert.equal(proof.axis.end,'2026-10-29');
});

test('a typed exact query cannot substitute a different actual model suffix',()=>{
  const wrong={...screen,lines:lines.map(l=>l.y===.218?{...l,text:'14900kes'}:l)};
  const proof=verifyDiscoveryScreen(catalog,wrong,{endDate:'2026-10-29'});
  assert.equal(proof.ok,false);assert.match(proof.reason,/主标题/);
});

test('an unspecified mandatory condition is review, never a scope correction',()=>{
  const selected={...screen,lines:lines.map(l=>l.y===.31&&l.x===.73?{...l,text:'少量磕碰划痕'}:l)};
  assert.equal(verifyDiscoveryScreen(catalog,selected,{endDate:'2026-10-29'}).ok,false);
  assert.equal(catalog.scope.condition,null);
});

test('query capacity or another variant cannot establish the full target SKU',()=>{
  const gpu={...catalog,category:'gpu',model:'RTX 4090 48GB',query:'RTX 4090 48GB',scope:{...catalog.scope,model:'RTX 4090 48GB',storage:'48GB'}};
  const wrong={...screen,lines:lines.map(l=>l.y===.13?{...l,text:'4090 48GB'}:l.y===.218?{...l,text:'4090 24GB'}:l)};
  assert.equal(verifyDiscoveryScreen(gpu,wrong,{endDate:'2026-10-29'}).ok,false);
});

test('an axis spanning less than thirty days is not accepted as this history layout',()=>{
  const short={...screen,lines:lines.map(l=>l.text==='05.01'?{...l,text:'10.01'}:l.text==='07.30'?{...l,text:'10.15'}:l)};
  const proof=verifyDiscoveryScreen(catalog,short,{endDate:'2026-10-29'});
  assert.equal(proof.ok,false);assert.match(proof.reason,/日期轴/);
});

test('real CPU axis keeps a rounded future October 31 tick when cutoff is October 29',()=>{
  const ticks=['04.27','06.13','07.30','09.14','10.31'];
  const actual={...screen,lines:[...lines.filter(l=>l.y!==.7),...ticks.map((text,n)=>line(text,.1+n*.17,.7))]};
  const proof=verifyDiscoveryScreen(catalog,actual,{endDate:'2026-10-29'});
  assert.equal(proof.ok,true);assert.equal(proof.axis.start,'2026-04-27');assert.equal(proof.axis.end,'2026-10-31');
  assert.equal(proof.axis.ticks,5);assert.equal(proof.axis.includes_future_tick,true);assert.equal(proof.axis.short_history,false);
  assert.equal('points' in proof,false); // rounded ticks are not price/date observations
});

test('a real 155-day 4090 axis can be read without claiming complete 180-day history',()=>{
  const gpu={...catalog,category:'gpu',model:'RTX 4090',query:'RTX4090',scope:{...catalog.scope,model:'RTX 4090'}};
  const ticks=['05.29','07.07','08.14','09.22','10.31'];
  const actual={...screen,lines:[...lines.filter(l=>l.y!==.7).map(l=>l.y===.218?{...l,text:'4090'}:l),
    ...ticks.map((text,n)=>line(text,.1+n*.17,.7))]};
  const proof=verifyDiscoveryScreen(gpu,actual,{endDate:'2026-10-29'});
  assert.equal(proof.ok,true);assert.equal(proof.axis.span_days,155);assert.equal(proof.axis.short_history,true);
  assert.equal(proof.axis.start,'2026-05-29');assert.equal(proof.axis.end,'2026-10-31');
  assert.match(proof.axis.message,/不证明逐日数据/);assert.equal('verified_days' in proof.axis,false);
});

function workerMock(item,actual=screen){
  const calls=[],items=[structuredClone(item)];let captures=0;
  return {calls,items,remaining:[],lease:null,catalog:null,lastScreen:actual,
    async assertTargetAllowed(id){assert.ok(items.some(c=>c.id===id));return {catalog_id:id,task_ids:[1]};},
    async openSearch(query){calls.push({search:query});return actual;},
    async inspectScreen(){return {...actual,filename:`actual-screen_${++captures}.png`};},
    async api(url,payload){calls.push({url,payload});if(url==='/api/mobile/targets')return {policy:'enabled_live_monitor_tasks_only',targets:items.map(c=>({catalog_id:c.id,task_ids:[1],enabled:true,market_scope_id:c.market_scope_id}))};if(url==='/api/workflow/status')return {mobile:{operator_paused:false}};if(url==='/api/catalog')return {items};if(payload&&url===`/api/catalog/${item.id}`)items[0]={...items[0],...payload,version:items[0].version+1};return items[0];},
    async heartbeat(status){calls.push({heartbeat:status});},
    async release(){this.remaining=[];this.lease=null;},
    async prepare(item){calls.push({prepare:item.id});return {done:true};},
  };
}
const discoveryFor=(root,worker)=>new MobileScopeDiscovery({projectDir:root,worker,wait:async()=>{}});

test('discovery persists the verified search entry, never claims jobs or changes economic scope',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);const discovery=discoveryFor(root,worker);
  const result=await discovery.discover(catalog,{endDate:'2026-10-29'});
  assert.equal(result.status,'ready');assert.equal(worker.calls[0].search,'14900KF');
  const patched=worker.calls.find(c=>c.url==='/api/catalog/7'&&c.payload).payload;
  assert.equal(patched.scope.verified,true);assert.equal(patched.scope.evidence,'actual-screen_2.png');
  assert.equal(result.verification_count,2);assert.deepEqual(result.verification_evidence,['actual-screen_1.png','actual-screen_2.png']);
  for(const key of ['model','storage','condition','filters'])assert.deepEqual(patched.scope[key],catalog.scope[key]);
  assert.equal(patched.query,'14900KF');
  assert.equal(worker.calls.some(c=>c.url?.includes('/claim')),false);
  const restarted=discoveryFor(root,worker);
  assert.equal(await restarted.next([catalog]),undefined); // status/version patches cannot cause an endless loop
}));

test('no curve records review and evidence without a permanent no-market claim',async()=>temporary(async(root)=>{
  const actual={status:'needs_review',message:'未找到行情区域',filename:'missing-curve.png',lines:[]};
  const worker=workerMock(catalog,actual);const discovery=discoveryFor(root,worker);
  const result=await discovery.discover(catalog,{endDate:'2026-10-29'});
  assert.equal(result.status,'needs_review');assert.equal(result.evidence,'missing-curve.png');
  assert.equal(worker.items[0].scope.verified,false);assert.match(worker.items[0].last_error,/未找到行情/);
}));

test('own PATCH and batch retry do not repeat discovery, but an explicit catalog revision does once',async()=>temporary(async(root)=>{
  const actual={status:'needs_review',message:'未找到行情区域',filename:'first-failure.png',lines:[]};
  const worker=workerMock(catalog,actual);const discovery=discoveryFor(root,worker);
  const first=await discovery.discover(catalog,{endDate:'2026-10-29'});
  assert.equal(first.attempt_count,1);assert.equal(first.catalog_version,2);
  const restarted=discoveryFor(root,worker);
  assert.equal(await restarted.next(worker.items),undefined);
  assert.equal(await restarted.next([{...worker.items[0],status:'pending'}]),undefined); // batch retry is not a catalog edit
  assert.equal((await restarted.discover(worker.items[0])).status,'already_attempted');
  assert.equal(worker.calls.filter(c=>c.search).length,1);
  worker.items[0]={...worker.items[0],version:3}; // user saved the same query/scope
  actual.filename='manual-recheck.png';
  const candidate=await restarted.next(worker.items);assert.equal(candidate,worker.items[0]);
  const second=await restarted.discover(candidate,{endDate:'2026-10-29'});
  assert.equal(second.attempt_count,2);assert.equal(second.catalog_version,4);
  assert.equal(second.history.length,1);assert.equal(second.history[0].evidence,'first-failure.png');
  assert.equal(second.history[0].catalog_version,2);assert.equal('history' in second.history[0],false);
  assert.equal(await discoveryFor(root,worker).next(worker.items),undefined);
}));

test('same-key attempts remain capped at six despite later external catalog revisions',async()=>temporary(async(root)=>{
  const actual={status:'needs_review',message:'未找到行情区域',filename:'failure-1.png',lines:[]};
  const worker=workerMock(catalog,actual);const discovery=discoveryFor(root,worker);let result;
  for(let attempt=1;attempt<=6;attempt++){
    if(attempt>1)worker.items[0]={...worker.items[0],version:worker.items[0].version+1};
    actual.filename=`failure-${attempt}.png`;
    const candidate=await discovery.next(worker.items);assert.ok(candidate);
    result=await discovery.discover(candidate,{endDate:'2026-10-29'});
    assert.equal(result.attempt_count,attempt);
    assert.equal(await discovery.next(worker.items),undefined);
  }
  worker.items[0]={...worker.items[0],version:worker.items[0].version+1};
  assert.equal(await discoveryFor(root,worker).next(worker.items),undefined);
  assert.equal((await discovery.discover(worker.items[0])).status,'already_attempted');
  assert.equal(worker.calls.filter(c=>c.search).length,6);
  assert.deepEqual(result.history.map(r=>r.evidence),['failure-1.png','failure-2.png','failure-3.png','failure-4.png','failure-5.png']);
}));

test('legacy cache receives only one compatible upgrade and keeps its earlier evidence',async()=>temporary(async(root)=>{
  const actual={status:'needs_review',message:'未找到行情区域',filename:'upgrade-evidence.png',lines:[]};
  const worker=workerMock(catalog,actual);const discovery=discoveryFor(root,worker);
  await discovery.load();discovery.records[discovery.key(catalog)]={status:'needs_review',catalog_id:7,evidence:'legacy-evidence.png'};
  await discovery.save();
  const restarted=discoveryFor(root,worker);
  assert.equal(await restarted.next(worker.items),worker.items[0]);
  const result=await restarted.discover(worker.items[0],{endDate:'2026-10-29'});
  assert.equal(result.attempt_count,2);assert.equal(result.catalog_version,2);
  assert.equal(result.history[0].evidence,'legacy-evidence.png');
  assert.equal(await discoveryFor(root,worker).next(worker.items),undefined);
}));

test('catalog metadata edited during screen verification is not overwritten',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let captures=0;
  worker.inspectScreen=async()=>{worker.items[0]={...worker.items[0],version:2};return {...screen,filename:`edited-${++captures}.png`};};
  assert.equal((await discoveryFor(root,worker).discover(catalog,{endDate:'2026-10-29'})).status,'scope_changed');
  assert.equal(worker.calls.some(c=>c.payload&&c.url==='/api/catalog/7'),false);
}));

test('already approved phone cannot be discovered or replaced by default-wide scope',async()=>temporary(async(root)=>{
  const approved={...catalog,id:12,status:'partial',scope:{...catalog.scope,storage:'256GB',condition:'少量磕碰划痕',verified:true}};
  const worker=workerMock(approved);const discovery=discoveryFor(root,worker);
  assert.equal(await discovery.next([approved]),undefined);
  assert.equal((await discovery.discover(approved)).status,'skipped');assert.equal(worker.calls.length,0);
}));

test('operator approval arriving during discovery is never overwritten',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);worker.inspectScreen=async()=>{worker.items[0].scope.verified=true;return screen;};
  const discovery=discoveryFor(root,worker);
  assert.equal((await discovery.discover(catalog,{endDate:'2026-10-29'})).status,'scope_changed');
  assert.equal(worker.calls.some(c=>c.payload&&c.url==='/api/catalog/7'),false);
}));

test('monitored mode discovers one target then can process that freshly approved target',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);const runner=new NativeMobileRunner({projectDir:root,discover:true,worker,
    app:{health:async()=>({ready:true,targetFrontmost:true,permissions:{accessibility:true,screenRecording:true}})},log:()=>{}});
  // Fix only the pure proof date; no real helper/phone is involved.
  const original=runner.discovery.discover.bind(runner.discovery);
  runner.discovery.wait=async()=>{};
  runner.discovery.discover=c=>original(c,{endDate:'2026-10-29'});
  await runner.cycle();assert.ok(worker.calls.some(c=>c.search==='14900KF'));assert.ok(worker.calls.some(c=>c.prepare===7));
  assert.equal(worker.claimLimit,40);
  assert.equal(JSON.parse(await fs.readFile(runner.statePath)).mode,'monitored');
}));

test('a delayed model card waits for fresh frames then requires two successful independent proofs',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let captures=0;
  worker.inspectScreen=async()=>({filename:`delayed-${++captures}.png`,lines:captures<=2?lines.filter(l=>l.y!==.218):lines});
  const discovery=discoveryFor(root,worker);const result=await discovery.discover(catalog,{endDate:'2026-10-29'});
  assert.equal(captures,4);assert.equal(result.status,'ready');assert.equal(result.verification_count,2);
  assert.deepEqual(result.verification_evidence,['delayed-3.png','delayed-4.png']);
}));

test('a successful frame followed by a different scope does not auto-approve',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let captures=0;
  worker.inspectScreen=async()=>({filename:`scope-${++captures}.png`,lines:captures===1?lines:lines.map(l=>l.x===.73&&l.y===.31?{...l,text:'少量磕碰划痕'}:l)});
  const result=await discoveryFor(root,worker).discover(catalog,{endDate:'2026-10-29'});
  assert.equal(captures,2);assert.equal(result.status,'needs_review');assert.equal(worker.items[0].scope.verified,false);
}));

test('one reused screenshot cannot masquerade as two independent captures',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let captures=0;
  worker.inspectScreen=async()=>{captures++;return screen;};
  const result=await discoveryFor(root,worker).discover(catalog,{endDate:'2026-10-29'});
  assert.equal(captures,6);assert.equal(result.status,'needs_review');assert.equal(result.verification_count,0);
}));

test('a never-loaded title uses a bounded repair attempt and persists review for restart',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let captures=0;
  worker.inspectScreen=async()=>({filename:`missing-title-${++captures}.png`,lines:lines.filter(l=>l.y!==.218)});
  const discovery=discoveryFor(root,worker);const result=await discovery.discover(catalog,{endDate:'2026-10-29'});
  assert.equal(captures,6);assert.equal(result.status,'needs_review');assert.match(result.reason,/布局尚未稳定/);
  assert.equal(await discoveryFor(root,worker).next([catalog]),undefined);
}));

test('user stop while waiting for a fresh proof does not mark the scope as bad',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);worker.inspectScreen=async()=>{const error=new Error('手机驱动已停止');error.code='STOPPED';throw error;};
  await assert.rejects(()=>discoveryFor(root,worker).discover(catalog,{endDate:'2026-10-29'}),/已停止/);
  assert.equal(worker.calls.some(c=>c.payload&&c.url==='/api/catalog/7'),false);
  assert.equal(await discoveryFor(root,worker).next([catalog]),catalog);
}));

test('a missing monitor cannot trigger discovery or a persistent catalogue update',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);worker.assertTargetAllowed=async()=>{const error=new Error('监控已停用');error.code='TARGET_NOT_MONITORED';throw error;};
  const discovery=discoveryFor(root,worker);
  await assert.rejects(()=>discovery.discover(catalog),error=>error.code==='TARGET_NOT_MONITORED');
  assert.equal(worker.calls.some(c=>c.search||c.payload),false);
}));

test('navigation uncertainty remains a guidance request instead of permanent failed-scope cache',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);worker.openSearch=async()=>({status:'needs_ai_guidance',message:'搜索入口需要截图核对',navigation:{status:'needs_ai_guidance',screen_kind:'XianyuHome'}});
  const discovery=discoveryFor(root,worker),result=await discovery.discover(catalog);
  assert.equal(result.status,'needs_ai_guidance');assert.equal(worker.calls.some(c=>c.payload),false);
  assert.equal(await discovery.next([catalog]),catalog);
}));

test('monitor revoked during screenshot proof prevents scope approval',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);let checks=0;
  worker.assertTargetAllowed=async()=>{if(++checks>1){const error=new Error('监控已移除');error.code='TARGET_NOT_MONITORED';throw error;}return {task_ids:[1]};};
  await assert.rejects(()=>discoveryFor(root,worker).discover(catalog,{endDate:'2026-10-29'}),error=>error.code==='TARGET_NOT_MONITORED');
  assert.equal(worker.calls.some(c=>c.payload),false);assert.equal(worker.items[0].scope.verified,false);
}));

test('discovery still pending keeps one-day claims for model rotation',async()=>temporary(async(root)=>{
  const worker=workerMock(catalog);worker.items.push({...structuredClone(catalog),id:8,model:'i9-14900K',query:'14900K',market_scope_id:'other-scope'});
  const runner=new NativeMobileRunner({projectDir:root,discover:true,worker,
    app:{health:async()=>({ready:true,targetFrontmost:true,permissions:{accessibility:true,screenRecording:true}})},log:()=>{}});
  const original=runner.discovery.discover.bind(runner.discovery);runner.discovery.wait=async()=>{};
  runner.discovery.discover=c=>original(c,{endDate:'2026-10-29'});
  await runner.cycle();assert.equal(worker.claimLimit,1);
}));
