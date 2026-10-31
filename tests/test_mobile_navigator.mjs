// Synthetic OCR, filesystem temp fixtures and mocked input only. No helper/UI.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {MobileNavigator,GuidanceMailbox,classifyMobileScreen,trustedXianyuHome,sensitiveHomeChrome} from '../automation/mobile_navigator.mjs';
const line=(text,x,y,width=.15,height=.02)=>({text,x,y,width,height});
const target={id:4,version:3,task_ids:[2],model:'i9-14900KF',query:'14900kf',scope:{verified:true,filters:{}}};
const shot=(name,lines)=>({filename:name+'.png',width:708,height:1562,lines});
const home=shot('home',[line('首页',.1,.095),line('集市',.3,.095),line('圈子',.5,.095),line('回收',.7,.095),line('搜索感兴趣的商品',.08,.176,.8,.03)]);
const empty=shot('empty',[line('搜索宝贝',.2,.131,.35),line('搜索',.86,.131,.08),line('历史搜索',.1,.25)]);
const filled=shot('filled',[line('14900kf',.2,.131,.35),line('搜索',.86,.131,.08),line('历史搜索',.1,.25)]);
const results=shot('results',[line('14900kf',.2,.131,.35),line('搜索',.86,.131,.08),line('宝贝',.2,.175),line('行情',.7,.175),line('综合',.1,.24)]);
const market=shot('market',[line('14900kf',.2,.131,.35),line('14900KF',.3,.218),line('近7日成交均价',.1,.39)]);
const unknown=shot('unknown',[line('无法识别的页面',.1,.3)]);
const actualHome=shot('actual-home',[...home.lines.filter(l=>!l.text.includes('搜索')),line('活力28消毒液',.13,.173,.65,.028),
  line('首页',.12,.94),line('消息',.58,.94),line('我的',.8,.94)]);
const mergedHome=shot('merged-home',[line('首页集市 圈子境拍中',.047,.125,.7,.022),line('回收',.81,.125,.1),
  line('mlb帽子（',.121,.17,.62,.024),line('支付方式',.16,.61,.2),
  line('首页',.12,.945),line('消息',.58,.945),line('我',.8,.945)]);
async function temporary(fn){const root=await fs.mkdtemp(path.join(os.tmpdir(),'mobile-navigator-test-'));try{return await fn(root);}finally{await fs.rm(root,{recursive:true,force:true});}}
function workerMock(root,frames){
  let cursor=0;const calls=[];
  return {root,calls,anchors:new Map(),
    async assertTargetAllowed(id){calls.push({authorize:id});assert.equal(id,4);return {catalog_id:4,task_ids:[2]};},
    async inspectScreen(){const screen=frames[Math.min(cursor++,frames.length-1)];calls.push({capture:screen.filename});return structuredClone(screen);},
    app:{async click(point){calls.push({click:point});},async typeText(text){calls.push({type:text});}}};
}
const navigatorFor=(root,worker,extra={})=>new MobileNavigator({worker,mailbox:new GuidanceMailbox({projectDir:root,requireEvidence:false}),wait:async()=>{},...extra});
async function responseFor(mailbox,request,actions,extra={}){
  const response={schema_version:'mobile-guidance-suggestion-v1',request_id:request.request_id,screen_hash:request.screen_hash,
    catalog_id:request.catalog_id,task_ids:request.task_ids,created_at:new Date().toISOString(),actions,...extra};
  await fs.writeFile(mailbox.responseFile,JSON.stringify(response));return response;
}

test('homepage with evidenced search text at y .176 is supported',()=>{
  assert.equal(classifyMobileScreen(home).kind,'XianyuHome');
  assert.equal(classifyMobileScreen(empty).kind,'searchEdit');
  assert.equal(classifyMobileScreen(results).kind,'searchResults');
  assert.equal(classifyMobileScreen(market).kind,'market');
  assert.equal(classifyMobileScreen(unknown).kind,'unknown');
});

test('observed homepage advertising placeholder is recognized but is not a search-button proof',async()=>temporary(async(root)=>{
  const actual=actualHome;
  assert.equal(classifyMobileScreen(actual).kind,'XianyuHome');
  const worker=workerMock(root,[actual]),result=await navigatorFor(root,worker).navigate(target.query,target);
  assert.equal(result.status,'needs_ai_guidance');assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('Codex semantic home-search suggestion binds actual placeholder then normal navigation validates result',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[actualHome,empty,empty,empty,filled,results,market]),nav=navigatorFor(root,worker);
  const first=await nav.guidance(actualHome,target,'占位文字不等于搜索语义，等待截图指导');
  await responseFor(nav.mailbox,first.navigation.request,[{kind:'tap_home_search',text:'活力28消毒液'}]);
  const result=await nav.guidance(actualHome,target,'收到当前截图指导');
  assert.equal(result.status,'market');assert.equal(result.navigation.status,'verifying_scope');
  assert.equal(worker.calls.find(c=>c.click).click[1],Math.round((.173+.028/2)*1562));
  assert.deepEqual(worker.calls.filter(c=>c.type).map(c=>c.type),['14900kf']);
}));

test('observed merged header, bottom 我 and feed 支付方式 retain trusted home without guessing its pill',async()=>temporary(async(root)=>{
  assert.equal(trustedXianyuHome(mergedHome),true);
  assert.equal(classifyMobileScreen(mergedHome).kind,'XianyuHome');
  assert.equal(sensitiveHomeChrome(mergedHome),false);
  const worker=workerMock(root,[mergedHome]),nav=navigatorFor(root,worker);
  const first=await nav.navigate(target.query,target);
  assert.equal(first.status,'needs_ai_guidance');assert.equal(worker.calls.some(c=>c.click||c.type),false);
  assert.equal(first.navigation.request.screen_kind,'XianyuHome');
  // Only an explicit semantic suggestion for the actual source text may click.
  await responseFor(nav.mailbox,first.navigation.request,[{kind:'tap_home_search',text:'mlb帽子（'}]);
  const follow=workerMock(root,[mergedHome,empty,empty,empty,filled,results,market]);
  const result=await navigatorFor(root,follow).guidance(mergedHome,target,'当前Codex确认的搜索pill');
  assert.equal(result.status,'market');assert.equal(follow.calls.find(c=>c.click).click[1],Math.round((.17+.024/2)*1562));
}));

test('a merged header alone cannot replace the three bottom navigation proofs',()=>{
  for(const missing of ['首页','消息','我']){
    const partial={...mergedHome,lines:mergedHome.lines.filter(l=>!(l.y>.91&&l.text===missing))};
    assert.equal(trustedXianyuHome(partial),false);
  }
  assert.equal(sensitiveHomeChrome({...mergedHome,lines:[...mergedHome.lines,line('登录',.3,.2)]}),true);
  assert.equal(classifyMobileScreen({...mergedHome,lines:[...mergedHome.lines,line('请完成安全验证',.2,.5)]}).kind,'verification');
  assert.equal(classifyMobileScreen({...mergedHome,lines:[...mergedHome.lines,line('确认付款',.2,.5)]}).kind,'verification');
});

test('semantic home-search suggestions reject invented text, wrong region, stale layout and sensitive pages',async()=>temporary(async(root)=>{
  for(const [requestScreen,text,fresh] of [
    [actualHome,'不存在的搜索词',actualHome],
    [{...actualHome,lines:actualHome.lines.map(l=>l.text==='活力28消毒液'?{...l,y:.4}:l)},'活力28消毒液',actualHome],
    [actualHome,'活力28消毒液',{...actualHome,lines:actualHome.lines.filter(l=>l.text!=='消息')}],
    [{...actualHome,lines:[...actualHome.lines,line('权限申请',.2,.4)]},'活力28消毒液',actualHome],
  ]){
    const worker=workerMock(root,[fresh]),nav=navigatorFor(root,worker);
    const first=await nav.guidance(requestScreen,target,'严格语义导航');
    await responseFor(nav.mailbox,first.navigation.request,[{kind:'tap_home_search',text}]);
    const result=await nav.guidance(requestScreen,target,'执行前复核');
    assert.equal(result.status,'needs_ai_guidance');assert.equal(worker.calls.some(c=>c.click||c.type),false);
  }
}));

test('local OCR navigates homepage, edit, search result and market using fresh evidence',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[home,empty,empty,filled,results,market]);
  const result=await navigatorFor(root,worker).navigate(target.query,target);
  assert.equal(result.status,'market');assert.equal(result.navigation.status,'verifying_scope');
  assert.deepEqual(worker.calls.filter(c=>c.type).map(c=>c.type),['14900kf']);
  assert.equal(worker.calls.filter(c=>c.click).length,4);
  const firstClick=worker.calls.find(c=>c.click).click;
  assert.equal(firstClick[1],Math.round((.176+.03/2)*1562)); // actual visible home search label
}));

test('clear control must be actually visible before replacing an old query',async()=>temporary(async(root)=>{
  const old=shot('old',[line('old-query',.2,.131,.35),line('×',.78,.131,.025),line('搜索',.86,.131,.08),line('历史搜索',.1,.25)]);
  const worker=workerMock(root,[old,empty,empty,filled,results,market]);
  assert.equal((await navigatorFor(root,worker).navigate(target.query,target)).status,'market');
  assert.deepEqual(worker.calls.filter(c=>c.type).map(c=>c.type),['14900kf']);
  assert.equal(worker.calls.find(c=>c.click).click[0],Math.round((.78+.025/2)*708));
  const noClear=workerMock(root,[shot('no-clear',old.lines.filter(l=>l.text!=='×'))]);
  const result=await navigatorFor(root,noClear).navigate(target.query,target);
  assert.equal(result.status,'needs_ai_guidance');assert.equal(noClear.calls.some(c=>c.click||c.type),false);
}));

test('unknown screen produces persistent bounded guidance material without any click or AI call',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[unknown]);const nav=navigatorFor(root,worker);
  const result=await nav.navigate(target.query,target);
  assert.equal(result.status,'needs_ai_guidance');assert.equal(result.navigation.screen_kind,'unknown');
  const envelope=await nav.mailbox.read();assert.equal(envelope.status,'pending');
  assert.equal(envelope.request.catalog_id,4);assert.deepEqual(envelope.request.task_ids,[2]);
  assert.equal(envelope.request.capabilities.external_ai_called,false);assert.equal(envelope.request.capabilities.multimodal_model_connected,false);
  assert.ok(envelope.request.created_at&&envelope.request.expires_at);
  assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('locked, disconnected, verification, login, permission and payment screens never accept navigation',async()=>temporary(async(root)=>{
  for(const [text,kind] of [['iPhone 镜像已锁定','locked'],['连接已断开','disconnected'],['请完成安全验证','verification'],['手机号登录','verification'],['权限申请','verification'],['确认付款','verification']]){
    const screen=shot('blocked',[line(text,.1,.2),line('取消',.5,.6)]),worker=workerMock(root,[screen]);
    assert.equal(classifyMobileScreen(screen).kind,kind);
    await assert.rejects(()=>navigatorFor(root,worker).navigate(target.query,target));
    assert.equal(worker.calls.some(c=>c.click||c.type),false);
  }
}));

test('only ordinary uniquely evidenced close/cancel can dismiss a popup',async()=>temporary(async(root)=>{
  const popup=shot('popup',[line('温馨提示',.3,.4),line('关闭',.6,.65)]),worker=workerMock(root,[popup,market]);
  assert.equal((await navigatorFor(root,worker).navigate(target.query,target)).status,'market');
  assert.equal(worker.calls.filter(c=>c.click).length,1);
  const permission=shot('permission',[line('用户协议',.3,.4),line('关闭',.6,.65)]);
  assert.equal(classifyMobileScreen(permission).kind,'verification');
}));

test('a query whose input effect is not visible is never typed repeatedly or submitted',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[empty]);
  const result=await navigatorFor(root,worker).navigate(target.query,target);
  assert.equal(result.status,'needs_ai_guidance');assert.equal(worker.calls.filter(c=>c.type).length,1);
  assert.equal(worker.calls.filter(c=>c.click).length,1);
}));

test('live target revocation before an action prevents all input',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[home]);let checks=0;
  worker.assertTargetAllowed=async()=>{if(++checks>1){const error=new Error('监控已停用');error.code='TARGET_NOT_MONITORED';throw error;}return {task_ids:[2]};};
  await assert.rejects(()=>navigatorFor(root,worker).navigate(target.query,target),/监控已停用/);
  assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('optional guidance callback can only select an evidenced action, never inject coordinates or prices',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[home]);
  const nav=navigatorFor(root,worker,{guide:async request=>({request_id:request.request_id,screen_hash:request.screen_hash,action_index:0,x:20,price:123})});
  assert.equal((await nav.navigate(target.query,target)).status,'needs_ai_guidance');
  assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('request is stable while pending so a later fresh screenshot does not invalidate an in-progress answer',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[unknown]),nav=navigatorFor(root,worker);
  const first=await nav.guidance(unknown,target,'等待截图引导');
  const second=await nav.guidance({...unknown,filename:'new-screenshot.png'},target,'再次等待');
  assert.equal(first.navigation.request.request_id,second.navigation.request.request_id);
}));

test('unknown to trusted-home upgrade replaces a pending request immediately and keeps its round count',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[unknown]),nav=navigatorFor(root,worker);
  const first=await nav.guidance(unknown,target,'旧版本未识别主页');
  const prior=await nav.mailbox.read();await nav.mailbox.write({...prior,round_count:1});
  const upgraded=await nav.guidance(mergedHome,target,'修正后的可信主页');
  assert.notEqual(upgraded.navigation.request.request_id,first.navigation.request.request_id);
  assert.equal(upgraded.navigation.request.screenshot.filename,'merged-home.png');
  assert.equal(upgraded.navigation.screen_kind,'XianyuHome');assert.equal((await nav.mailbox.read()).round_count,1);
  const sameKind=await nav.guidance({...mergedHome,filename:'new-home-frame.png'},target,'同种页面保持答复稳定');
  assert.equal(sameKind.navigation.request.request_id,upgraded.navigation.request.request_id);
}));

test('file suggestion closes the loop with fresh unique text and then ordinary scope navigation',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[results,market,market]),nav=navigatorFor(root,worker);
  const first=await nav.guidance(results,target,'需要核对行情入口');
  await responseFor(nav.mailbox,first.navigation.request,[{kind:'tap_text',text:'行情'}]);
  const result=await nav.guidance(results,target,'等待本机答复');
  assert.equal(result.status,'market');assert.equal(result.navigation.status,'verifying_scope');
  const envelope=await nav.mailbox.read();assert.equal(envelope.status,'completed');assert.equal(envelope.round_count,1);
  assert.equal(worker.calls.filter(c=>c.click).length,1);
  await assert.rejects(()=>fs.stat(nav.mailbox.responseFile),{code:'ENOENT'});
  assert.equal('price' in result,false); // suggestions cannot create prices
}));

test('suggested text is re-found on a fresh safe page, never reuses the old coordinate',async()=>temporary(async(root)=>{
  const moved=shot('moved',results.lines.map(l=>l.text==='行情'?{...l,x:.52}:l));
  const worker=workerMock(root,[moved,market,market]),nav=navigatorFor(root,worker);
  const initial=await nav.guidance(results,target,'核对行情');
  await responseFor(nav.mailbox,initial.navigation.request,[{kind:'tap_text',text:'行情'}]);
  await nav.guidance(results,target,'执行建议');
  assert.equal(worker.calls.find(c=>c.click).click[0],Math.round((.52+.15/2)*708));
}));

test('duplicate or missing fresh anchors and unknown pages reject file guidance without input',async()=>temporary(async(root)=>{
  for(const fresh of [shot('duplicate',[...results.lines,line('行情',.5,.23)]),unknown]){
    const worker=workerMock(root,[fresh]),nav=navigatorFor(root,worker);
    const initial=await nav.guidance(results,target,'核对行情');
    await responseFor(nav.mailbox,initial.navigation.request,[{kind:'tap_text',text:'行情'}]);
    const result=await nav.guidance(results,target,'执行建议');
    assert.equal(result.status,'needs_ai_guidance');assert.equal(worker.calls.some(c=>c.click||c.type),false);
  }
}));

test('guided typing uses only target query and requires a freshly empty editor before and after focus',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[empty,empty,filled,filled,results,market]),nav=navigatorFor(root,worker);
  const initial=await nav.guidance(empty,target,'填写已核实的监控查询');
  await responseFor(nav.mailbox,initial.navigation.request,[{kind:'type_target_query'}]);
  assert.equal((await nav.guidance(empty,target,'执行建议')).status,'market');
  assert.deepEqual(worker.calls.filter(c=>c.type).map(c=>c.type),['14900kf']);
}));

test('mailbox rejects stale, expired, unmonitored and arbitrary-action suggestions',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[results]),nav=navigatorFor(root,worker);
  const bad=[
    {request_id:'stale'}, {screen_hash:'stale'}, {catalog_id:39}, {task_ids:[999]},
    {created_at:'2000-01-01T00:00:00Z'}, {actions:[{kind:'tap_text',text:'支付'}]},
    {actions:[{kind:'tap_text',text:'行情',x:23}]}, {actions:[{kind:'type_target_query',query:'secret'}]},
    {actions:Array(4).fill({kind:'tap_text',text:'行情'})}, {price:100},
  ];
  for(const changes of bad){
    const envelope=await nav.mailbox.publish(nav.request(results,target,'测试严格建议'));
    await responseFor(nav.mailbox,envelope.request,[{kind:'tap_text',text:'行情'}],changes);
    const response=await nav.mailbox.takeResponse(envelope.request,target,id=>worker.assertTargetAllowed(id));
    assert.equal(response.rejected,true);
  }
  assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('a mailbox response is consumed once and no more than two rounds may execute',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[results]),nav=navigatorFor(root,worker);
  for(let round=1;round<=2;round++){
    const envelope=await nav.mailbox.publish(nav.request({...results,filename:`round-${round}.png`},target,'测试轮次'));
    assert.equal(envelope.status,'pending');
    await responseFor(nav.mailbox,envelope.request,[{kind:'tap_text',text:'行情'}]);
    const response=await nav.mailbox.takeResponse(envelope.request,target,id=>worker.assertTargetAllowed(id));
    assert.equal(response.round_count,round);
    assert.equal(await nav.mailbox.takeResponse(envelope.request,target,id=>worker.assertTargetAllowed(id)),null);
    await nav.mailbox.finish('rejected',{message:'模拟本轮尚未完成'});
  }
  const capped=await nav.mailbox.publish(nav.request(results,target,'第三轮'));
  assert.equal(capped.status,'limit_reached');assert.equal(capped.round_count,2);
}));

test('mailbox request expires and live task set changes reject an earlier answer',async()=>temporary(async(root)=>{
  let now=Date.now();const mailbox=new GuidanceMailbox({projectDir:root,now:()=>now,ttlMs:1000,requireEvidence:false});
  const worker=workerMock(root,[results]),nav=navigatorFor(root,worker,{mailbox});
  const first=await mailbox.publish(nav.request(results,target,'时限检查'));
  await responseFor(mailbox,first.request,[{kind:'tap_text',text:'行情'}]);now+=2000;
  assert.equal((await mailbox.takeResponse(first.request,target,id=>worker.assertTargetAllowed(id))).rejected,true);
  now=Date.now();const second=await mailbox.publish(nav.request(results,target,'任务集变化'));
  await responseFor(mailbox,second.request,[{kind:'tap_text',text:'行情'}]);
  assert.equal((await mailbox.takeResponse(second.request,target,async()=>({task_ids:[2,3]}))).rejected,true);
}));

test('production guidance requires real ordinary screenshot bytes and binds their SHA256',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[unknown]),nav=navigatorFor(root,worker),mailbox=new GuidanceMailbox({projectDir:root});
  await assert.rejects(()=>mailbox.publish(nav.request(unknown,target,'缺失真实截图')),/截图证据不可核实/);
  const directory=path.join(root,'data/market_evidence');await fs.mkdir(directory,{recursive:true});
  const filename='mobile_4_123_1_navigator.png',bytes=Buffer.from([137,80,78,71,13,10,26,10,1,2,3]);
  await fs.writeFile(path.join(directory,filename),bytes);
  const screen={...results,filename,bytes};
  const envelope=await mailbox.publish(nav.request(screen,target,'实际截图'));
  assert.equal(envelope.request.screenshot.sha256,createHash('sha256').update(bytes).digest('hex'));
  assert.equal(envelope.request.screen_hash_method,'sha256(image_sha256+dimensions+ocr)');
  await responseFor(mailbox,envelope.request,[{kind:'tap_text',text:'行情'}]);
  await fs.writeFile(path.join(directory,filename),Buffer.concat([bytes,Buffer.from([4])]));
  assert.equal((await mailbox.takeResponse(envelope.request,target,id=>worker.assertTargetAllowed(id))).rejected,true);
  assert.equal(worker.calls.some(c=>c.click||c.type),false);
}));

test('production guidance refuses a screenshot symlink or path outside the evidence directory',async()=>temporary(async(root)=>{
  const worker=workerMock(root,[unknown]),nav=navigatorFor(root,worker),mailbox=new GuidanceMailbox({projectDir:root});
  const directory=path.join(root,'data/market_evidence');await fs.mkdir(directory,{recursive:true});
  const outside=path.join(root,'outside.png');await fs.writeFile(outside,Buffer.from([137,80,78,71,13,10,26,10]));
  await fs.symlink(outside,path.join(directory,'mobile_4_link.png'));
  await assert.rejects(()=>mailbox.publish(nav.request({...results,filename:'mobile_4_link.png'},target,'symlink')),/截图证据不可核实/);
  await assert.rejects(()=>mailbox.publish(nav.request({...results,filename:'../../outside.png'},target,'outside')),/截图证据不可核实/);
}));
