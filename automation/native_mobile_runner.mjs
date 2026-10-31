/** Persistent, local iPhone-window worker. Importing this file does not run it. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {MobileCuaWorker} from './mobile_cua_driver.mjs';
import {NativeMobileApp} from './native_mobile_adapter.mjs';
import {MobileScopeDiscovery} from './mobile_scope_discovery.mjs';
import {MobileNavigator,classifyMobileScreen} from './mobile_navigator.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const identity=v=>String(v||'').normalize('NFKC').replace(/\s+/g,'').toLowerCase();
const READY_STATUSES=new Set(['ready','partial','complete']);
export const approvedCatalog=c=>Boolean(c?.enabled&&c?.scope?.verified===true&&READY_STATUSES.has(c.status));

export function monitoredTargets(value){
  if(value?.policy!=='enabled_live_monitor_tasks_only'||!Array.isArray(value.targets))throw new Error('实时监控任务白名单不可用；未检查或操作手机');
  const result=new Map();
  for(const target of value.targets){
    if(!Number.isSafeInteger(target.catalog_id)||target.catalog_id<1||target.enabled!==true||!Array.isArray(target.task_ids)||!target.task_ids.length||
       target.task_ids.some(id=>!Number.isSafeInteger(id)||id<1))throw new Error('实时监控目标缺少有效catalog_id/task_ids');
    const previous=result.get(target.catalog_id);
    result.set(target.catalog_id,{...target,task_ids:[...new Set([...(previous?.task_ids||[]),...target.task_ids])].sort((a,b)=>a-b)});
  }
  return result;
}

export function screenStatus(lines) {
  const state=classifyMobileScreen({lines});
  if(['locked','disconnected','verification'].includes(state.kind))return {status:state.kind,message:state.reason};
  if(['XianyuHome','searchEdit','searchResults','market','popup'].includes(state.kind))return {status:'ready',message:state.reason};
  return {status:'waiting_user',message:state.reason};
}

export function verifyMarketScope(catalog,lines) {
  if(!approvedCatalog(catalog))return {ok:false,reason:'目录范围尚未通过实际屏幕核对或尚未获准采集'};
  const anchor=lines.filter(l=>identity(l.text).includes('近7日成交均价'));
  if(anchor.length!==1)return {ok:false,reason:'页面没有唯一近7日成交均价指标'};
  const ay=Number(anchor[0].y);
  const titleLines=lines.filter(l=>Number(l.y)>.18&&Number(l.y)<ay-.04&&Number(l.x)<.75&&/[A-Za-z0-9]/.test(l.text||'')&&!/人在搜|选择|CORE|搜索|回收|折|%/i.test(l.text||''));
  const allowed=[catalog.model,catalog.query,...(catalog.scope.aliases||[])].map(identity);
  const actualTitle=titleLines.find(l=>allowed.includes(identity(l.text)));
  if(!actualTitle)return {ok:false,reason:'行情页实际完整型号与已批准型号/别名不符'};
  const region=lines.filter(l=>Number(l.y)>.18&&Number(l.y)<ay);
  // Numeric capacity chips are not model-title lines. In the phone layout
  // capacity and condition share y≈.303 below the actual model at y≈.218.
  const titleBottom=Number(actualTitle.y)+Number(actualTitle.height||0);
  const filterRegion=region.filter(l=>Number(l.y)>titleBottom);
  const text=filterRegion.map(l=>identity(l.text)).join('|');
  if(catalog.scope.storage){
    const capacity=identity(catalog.scope.storage).replace(/gb/g,'g').replace(/tb/g,'t');
    const capacityPattern=new RegExp(`(?<!\\d)${capacity.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}(?![a-z0-9])`);
    if(!region.some(l=>capacityPattern.test(identity(l.text).replace(/gb/g,'g').replace(/tb/g,'t'))))return {ok:false,reason:'未在实际标题/筛选区域核实目标容量，禁止继承宽容量曲线'};
  }
  if(catalog.scope.condition&&!filterRegion.some(l=>identity(l.text)===identity(catalog.scope.condition)))return {ok:false,reason:'未在实际筛选区域核实已批准成色'};
  const filters=catalog.scope.filters||{};
  for(const value of Object.values(filters)){
    if(typeof value!=='string'||!text.includes(identity(value)))return {ok:false,reason:'实际筛选项未能逐一核对；保持待复核'};
  }
  // The known default market layout exposes brand/function/condition selectors.
  // Unknown selections cannot be substituted by copying verified:true.
  if(!catalog.scope.condition&&!Object.keys(filters).length){
    for(const label of ['品牌','功能状态','成色']){
      const heading=region.find(l=>identity(l.text).includes(label));
      if(!heading||!region.some(l=>/选择|全部|不限/.test(l.text||'')&&Math.abs(Number(l.x)-Number(heading.x))<.2&&Number(l.y)>=Number(heading.y)-.01&&Number(l.y)<Number(heading.y)+.09))
        return {ok:false,reason:`未核实${label}为已批准的默认范围`};
    }
  }
  return {ok:true};
}

export class NativeMobileWorker extends MobileCuaWorker {
  constructor(options){super(options);this.navigator=new MobileNavigator({worker:this});this.navigation=null;}
  async getMonitoredTargets(){try{return monitoredTargets(await this.api('/api/mobile/targets'));}catch(error){error.code='TARGET_AUTH_UNAVAILABLE';throw error;}}
  async assertTargetAllowed(catalogId){
    let targets;
    try{targets=await this.getMonitoredTargets();}catch(error){if(this.lease||this.remaining.length)await this.release('waiting_user','实时监控任务白名单不可核实，暂停读取').catch(()=>{});throw error;}
    const target=targets.get(catalogId);
    if(!target||(this.remaining.length&&this.catalog?.id===catalogId&&target.market_scope_id&&target.market_scope_id!==this.catalog.market_scope_id)){
      const error=new Error('当前型号已不属于已启用的监控任务，停止并释放日期租约');error.code='TARGET_NOT_MONITORED';this.lastControlError=error;
      if(this.lease||this.remaining.length)await this.release('waiting_user',error.message);
      throw error;
    }
    this.taskIds=target.task_ids;return target;
  }
  async openSearch(query){
    if(this.remaining.length)throw new Error('切换型号前须完成或释放当前租约');
    const target=this.catalog||this.navigationTarget;
    const allowed=await this.assertTargetAllowed(target?.id);
    const result=await this.navigator.navigate(query,{...target,task_ids:allowed.task_ids});
    this.navigation=result.navigation||null;return result;
  }
  heartbeat(status='ready',message='独立原生手机驱动已检查本机权限与实际镜像界面') {return super.heartbeat(status,message);}
  async checkpoint() {
    await fs.mkdir(path.join(this.root,'data'),{recursive:true});
    await fs.writeFile(path.join(this.root,'data/native_mobile_worker_trace.json'),JSON.stringify({worker_id:this.workerId,
      catalog_id:this.catalog?.id,updated_at:new Date().toISOString(),trace:this.trace.slice(-1500)},null,2),{mode:0o600});
  }
  async inspectScreen(label='inspect') {
    const screen=await super.inspectScreen(label);this.lastScreen=screen;const actual=screenStatus(screen.lines);
    if(['locked','disconnected','verification'].includes(actual.status)){
      const error=new Error(actual.status==='locked'?'DEVICE_LOCKED':actual.status==='disconnected'?'DEVICE_DISCONNECTED':actual.message);
      error.deviceStatus=actual.status;throw error;
    }
    return screen;
  }
  async api(url,payload) {
    if(url==='/api/mobile/claim'&&payload)await this.assertTargetAllowed(payload.catalog_id);
    if(['/api/mobile/readings','/api/mobile/jobs/status'].includes(url)&&payload)await this.assertTargetAllowed(this.catalog?.id);
    if(url==='/api/mobile/readings'&&payload){
      const latest=(await this.api('/api/catalog')).items.find(c=>c.id===this.catalog?.id);
      if(!approvedCatalog(latest)||latest.market_scope_id!==this.catalog?.market_scope_id)
        throw new Error('提交前当前目录的实际范围批准已撤销或变更，未保存读数');
    }
    const response=await fetch(this.base+url,payload===undefined?{signal:AbortSignal.timeout(10000)}:
      {method:url.startsWith('/api/catalog/')?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(10000)});
    const value=await response.json();if(!response.ok)throw new Error(value.detail||`HTTP ${response.status}`);return value;
  }
  async release(status='waiting_user',message='手机worker停止或等待本人处理，已领取任务保留续跑') {
    try {await super.heartbeat(status,message);}finally{this.lease=null;this.remaining=[];this.layout=null;this.anchors.clear();}
  }
  async claim(catalogId){
    const target=await this.assertTargetAllowed(catalogId);
    if(this.remaining.length)return this.remaining.length;
    await this.heartbeat('ready');
    const result=await this.api('/api/mobile/claim',{worker_id:this.workerId,catalog_id:catalogId,limit:this.claimLimit===40?40:1});
    this.lease=result.lease_token;this.catalog=result.catalog;this.remaining=result.jobs;
    if(this.catalog)this.catalog={...this.catalog,task_ids:target.task_ids};
    if(result.jobs.length){
      const batches=await this.api('/api/mobile/batches');const batch=batches.batches.find(b=>b.id===result.jobs[0].batch_id);
      if(!batch)throw new Error('已领取日期的批次不存在');
      this.end=batch.end_date;this.start=new Date(new Date(batch.end_date).getTime()-(batch.days-1)*86400000).toISOString().slice(0,10);
    }
    return result.jobs.length;
  }
  async prepare(catalog) {
    await this.assertTargetAllowed(catalog.id);this.navigationTarget=catalog;
    await this.claim(catalog.id);
    if(!this.remaining.length)return {done:true};
    const frozen=this.catalog;
    if(frozen.market_scope_id!==catalog.market_scope_id)throw new Error('目录价格范围已变更，旧批次须复核或重建');
    // Keep frozen.query for reading provenance. The current approved query may
    // be a corrected search entry for exactly the same market_scope_id.
    this.catalog={...frozen,scope:catalog.scope,status:catalog.status,enabled:catalog.enabled,version:catalog.version,navigationQuery:catalog.query};
    const owned=this.remaining;this.remaining=[];
    let opened;
    try {opened=await this.openSearch(catalog.query);}finally{this.remaining=owned;}
    if(opened.status!=='market'){const error=new Error(opened.message||'未进入受支持的行情页面');error.code='NEEDS_AI_GUIDANCE';error.navigation=opened.navigation;throw error;}
    if(typeof this.applyScope==='function'){
      await this.applyScope(this.catalog.scope);
      opened=await this.inspectScreen('scope_verified');
    }
    const actual=screenStatus(opened.lines);
    if(actual.status!=='ready')throw new Error(actual.status==='locked'?'DEVICE_LOCKED':actual.message);
    const verification=verifyMarketScope(this.catalog,opened.lines);
    if(!verification.ok)throw new Error(verification.reason);
    return {done:false};
  }
  async readAt(x){await this.assertTargetAllowed(this.catalog?.id);return super.readAt(x);}
  async runChunk(options){
    this.lastControlError=null;await this.assertTargetAllowed(this.catalog?.id||options?.catalogId);
    const result=await super.runChunk(options);if(this.lastControlError)throw this.lastControlError;return result;
  }
}

export class NativeMobileRunner {
  constructor({projectDir=ROOT,baseUrl='http://127.0.0.1:8765',workerId='native-mobile-v1',catalogId=null,monitored=true,discover=false,app,worker,sleep,log}={}) {
    this.root=projectDir;this.app=app||new NativeMobileApp({projectDir});
    this.worker=worker||new NativeMobileWorker({app:this.app,projectDir,baseUrl,workerId});
    this.workerId=workerId;this.catalogId=catalogId;this.monitored=monitored;this.discover=catalogId===null;this.stopping=false;this.failures=new Map();this.activeCatalog=null;this.targets=new Map();
    if(discover&&catalogId!==null)throw new Error('监控发现不能与单型号试运行同时启用');
    this.statePath=path.join(projectDir,'data/native_mobile_worker_state.json');
    this.sleep=sleep||((ms)=>new Promise(resolve=>{this.wake=resolve;this.timer=setTimeout(resolve,ms);}));
    this.log=log||(value=>process.stdout.write(JSON.stringify({...value,time:new Date().toISOString()})+'\n'));
    this.discovery=new MobileScopeDiscovery({projectDir,worker:this.worker,log:this.log});
  }
  async state(phase,device_status,message,connected=false) {
    const value={pid:process.pid,worker_id:this.workerId,mode:this.catalogId!==null?'single':'monitored',phase,device_status,connected,last_update:new Date().toISOString(),active_catalog:this.activeCatalog,
      task_ids:this.targets.get(this.activeCatalog)?.task_ids||[],navigation:this.worker.navigation||null,message};
    this.stateQueue=(this.stateQueue||Promise.resolve()).catch(()=>{}).then(async()=>{
      await fs.mkdir(path.dirname(this.statePath),{recursive:true,mode:0o700});
      const temporary=this.statePath+'.tmp';await fs.writeFile(temporary,JSON.stringify(value,null,2),{mode:0o600});await fs.rename(temporary,this.statePath);
    });
    await this.stateQueue;
    return value;
  }
  async safelyRelease(status,message){try{await this.worker.release(status,message);}catch(error){this.log({event:'heartbeat_unavailable',message:String(error.message)});}}
  async refreshTargets(){
    this.targets=monitoredTargets(await this.worker.api('/api/mobile/targets'));
    const active=this.worker.remaining.length?this.worker.catalog?.id:this.activeCatalog;
    const selected=[...this.targets.keys()].filter(id=>this.catalogId===null||id===this.catalogId);
    if(active!==null&&active!==undefined&&!selected.includes(active)){
      await this.safelyRelease('waiting_user','监控任务已禁用或移除，已释放当前型号日期租约');this.activeCatalog=null;
      await this.state('waiting_tasks','waiting_user','当前型号已不在已启用监控任务中，停止本轮手机操作');return false;
    }
    if(!selected.length){
      await this.state('waiting_tasks','waiting_user',this.catalogId===null?'没有已启用的监控任务，不操作手机':'指定型号不属于已启用的监控任务，不操作手机');return false;
    }
    return true;
  }
  targetCatalogs(items){return items.filter(c=>this.targets.has(c.id)&&(this.catalogId===null||c.id===this.catalogId)&&
    (!this.targets.get(c.id).market_scope_id||this.targets.get(c.id).market_scope_id===c.market_scope_id))
    .map(c=>({...c,task_ids:this.targets.get(c.id).task_ids}));}
  async projectPaused(){
    const workflow=await this.worker.api('/api/workflow/status');
    if(typeof workflow?.mobile?.operator_paused!=='boolean')throw new Error('项目手机暂停状态不可用；未检查或操作设备');
    if(!workflow.mobile.operator_paused)return false;
    const message=workflow.mobile.pause_reason||'本人已暂停手机历史采集；等待明确恢复，保留逐日断点';
    if(this.worker.lease||this.worker.remaining.length)await this.safelyRelease('waiting_user',message);
    await this.state('operator_paused','waiting_user',message,false);return true;
  }
  async waitForDevice(target) {
    let health;
    try{health=await this.app.health();}catch(error){
      await this.safelyRelease('disconnected',error.message);await this.state('waiting_device','disconnected',error.message);return false;
    }
    if(!health.ready){
      const permission=!health.permissions?.accessibility||!health.permissions?.screenRecording;
      const message=permission?'请本人在系统设置授予实际本地手机驱动辅助功能与屏幕录制权限；当前未连接采集':
        !health.appRunning?'请本人打开iPhone镜像应用':'请本人恢复可见的iPhone镜像窗口并连接手机';
      await this.safelyRelease(permission?'waiting_user':'disconnected',message);
      await this.state(permission?'waiting_permission':'waiting_device',permission?'waiting_user':'disconnected',message);return false;
    }
    if(health.targetFrontmost===false){
      const message='iPhone镜像不在前台，等待本人切回；独立驱动不会自动focus或抢占其他应用';
      await this.safelyRelease('waiting_user',message);await this.state('waiting_device','waiting_user',message);return false;
    }
    let screen;
    try {screen=await this.worker.inspectScreen('health');}
    catch(error){const status=error.deviceStatus||'waiting_user';await this.safelyRelease(status,error.message);await this.state('waiting_device',status,error.message);return false;}
    let actual=screenStatus(screen.lines);
    if(actual.status!=='ready'){
      if(actual.status==='waiting_user'){
        const navigator=this.worker.navigator||new MobileNavigator({worker:this.worker});
        const guidance=await navigator.guidance(screen,target,actual.message);this.worker.navigation=guidance.navigation;
        if(guidance.status==='market'){screen=guidance;actual=screenStatus(screen.lines);}
        else {await this.safelyRelease('waiting_user',actual.message);await this.state('needs_ai_guidance','waiting_user',actual.message);return false;}
      }
      if(actual.status!=='ready'){await this.safelyRelease(actual.status,actual.message);await this.state('waiting_device',actual.status,actual.message);return false;}
    }
    if(this.worker.remaining.length&&this.worker.catalog){
      const scope=verifyMarketScope(this.worker.catalog,screen.lines);
      if(!scope.ok){await this.safelyRelease('waiting_user',scope.reason);await this.state('waiting_device','waiting_user',scope.reason);return false;}
    }
    await this.worker.heartbeat('ready','原生镜像权限及实际可见闲鱼页面已通过检查');
    return true;
  }
  async cycle() {
    if(!await this.refreshTargets())return {wait:true,waiting_tasks:true};
    if(await this.projectPaused())return {wait:true,operator_paused:true};
    let result=await this.worker.api('/api/catalog');
    let eligible=this.targetCatalogs(result.items);
    if(!eligible.length){await this.state('waiting_tasks','waiting_user','监控目标的当前目录范围正在变更，未操作手机');return {wait:true,waiting_tasks:true};}
    if(!await this.waitForDevice(eligible.find(c=>c.id===this.worker.catalog?.id)||eligible[0]))return {wait:true};
    if(this.discover&&!this.worker.remaining.length){
      const candidate=await this.discovery.next(eligible);
      if(candidate){
        this.activeCatalog=candidate.id;await this.state('discovering','reading',`正在实际核对${candidate.model}的完整型号和原筛选范围`,true);
        await this.worker.heartbeat('reading','正在实际屏幕发现尚未核对的型号，尚未领取日期任务');
        this.discovering=true;
        let discovered;
        try{discovered=await this.discovery.discover(candidate);}finally{this.discovering=false;}
        if(discovered.status==='needs_ai_guidance'){
          this.worker.navigation=discovered.navigation;await this.state('needs_ai_guidance','waiting_user',discovered.message);return {wait:true};
        }
        if(!await this.refreshTargets())return {wait:true,waiting_tasks:true};
        result=await this.worker.api('/api/catalog');
        eligible=this.targetCatalogs(result.items);
      }
    }
    // One day per scope while discovery is still progressing. Afterwards keep
    // up to forty dates in one scope lease and rotate only when it is finished.
    this.worker.claimLimit=this.discover&&await this.discovery.next(eligible)?1:40;
    let all=eligible;
    if(this.worker.remaining.length){
      const active=all.find(c=>c.id===this.worker.catalog?.id);
      if(!approvedCatalog(active))throw new Error('当前已领取目录的批准已撤销或不在受控范围，停止读取并释放租约');
      all=[active];
    }else if(this.lastCompletedCatalog!==undefined){
      const position=all.findIndex(c=>c.id===this.lastCompletedCatalog);
      if(position>=0)all=[...all.slice(position+1),...all.slice(0,position+1)];
    }
    let didWork=false,waiting=false;
    for(const catalog of all){
      if(this.stopping)break;
      if(!await this.refreshTargets())return {wait:true,waiting_tasks:true};
      if(!this.targets.has(catalog.id))continue;
      if(!approvedCatalog(catalog)){
        if(catalog.enabled&&catalog.scope?.verified!==true&&!['no_market','unsupported','needs_review'].includes(catalog.status))
          await this.worker.api(`/api/catalog/${catalog.id}`,{status:'needs_review',last_error:'独立驱动未得到本人核对的实际App型号与筛选范围，未自动更改verified'});
        continue;
      }
      if((this.failures.get(catalog.id)||0)>=2)continue;
      this.activeCatalog=catalog.id;
      try{
        if(!this.worker.remaining.length){const prepared=await this.worker.prepare(catalog);if(prepared.done)continue;}
        if(!await this.refreshTargets())return {wait:true,waiting_tasks:true};
        const fresh=(await this.worker.api('/api/catalog')).items.find(c=>c.id===catalog.id);
        if(!approvedCatalog(fresh)||fresh.market_scope_id!==catalog.market_scope_id)throw new Error('目录批准已撤销或范围发生变化');
        this.worker.navigation={status:'reading',screen_kind:'market',request:null};
        await this.state('reading','reading',`正在逐日双次复读${catalog.model}`,true);
        const chunk=await this.worker.runChunk({catalogId:catalog.id,maxJobs:1,maxDurationMs:45000});
        this.log({event:'chunk',catalog_id:catalog.id,results:chunk.results,error:chunk.error,remaining:chunk.remaining_claimed});
        if(chunk.needs_user)throw new Error(chunk.error||'手机读取等待本人处理');
        waiting=false;
        didWork ||= Boolean(chunk.results?.length);
        if(!this.worker.remaining.length)this.lastCompletedCatalog=catalog.id;
        if(this.worker.remaining.length)break; // finish one leased scope before another
      }catch(error){
        waiting=true;
        const navigationWait=error.code==='NEEDS_AI_GUIDANCE';
        const monitorWait=['TARGET_NOT_MONITORED','TARGET_AUTH_UNAVAILABLE'].includes(error.code)||/不属于已启用的监控任务/.test(error.message);
        const deviceWait=navigationWait||monitorWait||['locked','disconnected','verification'].includes(error.deviceStatus)||/LOCKED|DISCONNECTED|STOPPED|前台|辅助功能|屏幕录制/.test(error.message);
        const count=(this.failures.get(catalog.id)||0)+(deviceWait?0:1);this.failures.set(catalog.id,count);
        await this.safelyRelease(/LOCKED/.test(error.message)?'locked':'waiting_user',String(error.message));
        if(error.navigation)this.worker.navigation=error.navigation;
        await this.state(navigationWait?'needs_ai_guidance':monitorWait?'waiting_tasks':'waiting_device',error.deviceStatus||(/LOCKED/.test(error.message)?'locked':'waiting_user'),String(error.message));
        this.log({event:'catalog_wait',catalog_id:catalog.id,attempt:count,message:String(error.message)});
        if(!deviceWait)await this.worker.api(`/api/catalog/${catalog.id}`,{status:'needs_review',last_error:String(error.message).slice(0,1000)});
      }
    }
    if(!this.worker.remaining.length&&!waiting){this.activeCatalog=null;await this.state('idle','ready',didWork?'本轮已保存真实读数；继续检查可恢复日期':'没有已批准的待采集日期；未核对目录保持待复核',true);}
    return {didWork};
  }
  stop(){
    this.stopping=true;this.app.stop?.();if(this.timer)clearTimeout(this.timer);this.wake?.();
    this.stopRelease=this.safelyRelease('disconnected','本人停止独立手机worker，已领取日期保留续跑');
  }
  async run({once=false}={}) {
    await this.state('starting','disconnected','独立驱动启动中，尚未通过设备检查');
    let pulseActive=false;
    this.pulse=setInterval(async()=>{
      if(this.stopping||pulseActive||(!this.worker.lease&&!this.discovering))return;
      pulseActive=true;
      try{
        if(!await this.refreshTargets())return;
        if(await this.projectPaused())return;
        const health=await this.app.health();
        if((!this.worker.lease&&!this.discovering)||this.stopping)return;
        if(!health.ready||health.targetFrontmost===false){await this.safelyRelease('waiting_user','镜像权限、窗口或前台状态不可用，已释放租约');await this.state('waiting_device','waiting_user','镜像权限、窗口或前台状态不可用，等待本人恢复');}
        else {
          const message=this.discovering?'独立手机worker正在实际核对候选型号；未领取日期任务':'独立手机worker仍在读取当前已批准范围';
          await this.worker.heartbeat('reading',message);await this.state(this.discovering?'discovering':'reading','reading',message,true);
        }
      }catch(error){this.log({event:'heartbeat_wait',message:error.message});}
      finally{pulseActive=false;}
    },20000);
    try{
      while(!this.stopping){
        try{const result=await this.cycle();if(this.stopping||once)break;await this.sleep(result.didWork?1000:30000);}
        catch(error){this.log({event:'worker_wait',message:String(error.message)});await this.safelyRelease('disconnected',error.message);await this.state('waiting_device','disconnected',error.message);if(this.stopping||once)break;await this.sleep(30000);}
      }
    }finally{clearInterval(this.pulse);await this.stopRelease;await this.safelyRelease('disconnected','独立手机worker已停止，未完成日期保留续跑');await this.state('stopped','disconnected','独立手机worker已停止');}
  }
}

export function parseArgs(args){
  const config={baseUrl:'http://127.0.0.1:8765',workerId:'native-mobile-v1',catalogId:null,once:false,discover:false,monitored:true};
  for(let i=0;i<args.length;i++){
    const key=args[i];if(key==='--once'){config.once=true;continue;}
    if(key==='--discover'){config.discover=true;continue;}
    if(key==='--monitored'){config.monitored=true;continue;}
    const value=args[++i];if(value===undefined)throw new Error('启动参数缺少值');
    if(key==='--base-url'){
      const url=new URL(value);if(!['http:','https:'].includes(url.protocol)||!['127.0.0.1','localhost','[::1]'].includes(url.hostname)||url.username||url.password||url.pathname!=='/'||url.search||url.hash)throw new Error('只允许本机服务地址');
      config.baseUrl=url.origin;
    }else if(key==='--worker-id'){if(!/^[A-Za-z0-9_-]{1,100}$/.test(value))throw new Error('worker-id格式无效');config.workerId=value;
    }else if(key==='--catalog-id'){if(!/^[1-9]\d*$/.test(value)||!Number.isSafeInteger(Number(value)))throw new Error('catalog-id须为正整数');config.catalogId=Number(value);
    }else throw new Error('不支持的启动参数');
  }
  if(config.discover&&config.catalogId!==null)throw new Error('全目录发现不能与单型号试运行同时启用');
  return config;
}

async function main(){
  const options=parseArgs(process.argv.slice(2));const runner=new NativeMobileRunner(options);
  const lock=path.join(ROOT,'data/native_mobile_worker.lock');await fs.mkdir(path.dirname(lock),{recursive:true});
  try{await fs.mkdir(lock);await fs.writeFile(path.join(lock,'pid'),String(process.pid));}
  catch(error){
    if(error.code!=='EEXIST')throw error;
    const pid=Number(await fs.readFile(path.join(lock,'pid'),'utf8').catch(()=>''));
    const stat=await fs.stat(lock);
    if(!pid&&Date.now()-stat.mtimeMs<120000)throw new Error('另一个手机worker正在初始化，请稍后再检查');
    let live=false;try{if(pid>0){process.kill(pid,0);live=true;}}catch{}
    if(live)throw new Error('已有独立手机worker进程运行');
    await fs.rm(lock,{recursive:true,force:true});await fs.mkdir(lock);await fs.writeFile(path.join(lock,'pid'),String(process.pid));
  }
  process.once('SIGTERM',()=>runner.stop());process.once('SIGINT',()=>runner.stop());
  try{await runner.run({once:options.once});}finally{await fs.rm(lock,{recursive:true,force:true});}
}
if(typeof process!=='undefined'&&process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)main().catch(error=>{process.stderr.write(String(error.message)+'\n');process.exitCode=1;});
