/** Screenshot-first collector using an injected, authorized CUA App.
 * Only setup/navigation runs local OCR. The sweep saves actual tooltip images;
 * it never derives dates/prices from chart coordinates or waits for a model.
 * Importing this module performs no UI, process, network or model operations.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {NativeMobileWorker} from './native_mobile_runner.mjs';
import {normalizeDiscoveryQuery} from './mobile_scope_discovery.mjs';

const identity=x=>String(x||'').normalize('NFKC').replace(/\s+/g,'').toLowerCase();
const comparable=x=>identity(x).replace(/^i9-/,'').replace(/^rtx/,'');
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');

export function screenshotSweepLayout(screen,catalog,stepPx=3){
  if(!Number.isInteger(stepPx)||stepPx<1||stepPx>8)throw new Error('截图扫描间隔只允许1至8像素');
  const {width,height,lines=[]}=screen;
  if(!Number.isSafeInteger(width)||!Number.isSafeInteger(height)||width<100||height<300)throw new Error('截图尺寸未核对');
  const anchors=lines.filter(l=>identity(l.text).includes('近7日成交均价'));
  if(anchors.length!==1)throw new Error('当前页面没有唯一行情指标，未启动截图扫描');
  const anchor=anchors[0],titles=(screen.model_lines?.length?screen.model_lines:lines).filter(l=>l.y>.18&&l.y<anchor.y-.04&&l.x<.8&&comparable(l.text)===comparable(catalog.model));
  if(titles.length!==1)throw new Error('行情主标题与当前监控完整型号未唯一匹配，未采图');
  const groups=[];
  for(const line of lines){
    if(line.y<=anchor.y||!/^\d{2}[./]\d{2}$/.test(String(line.text||'').trim()))continue;
    let group=groups.find(g=>Math.abs(g[0].y-line.y)<.015);
    if(!group){group=[];groups.push(group);}group.push(line);
  }
  const ticks=groups.sort((a,b)=>b.length-a.length)[0];
  if(!ticks||ticks.length<3)throw new Error('未核对至少三个实际日期轴标签，未扫描');
  const axisY=ticks.reduce((s,l)=>s+l.y+l.height/2,0)/ticks.length;
  const y=Math.round((axisY-.082)*height),left=Math.max(10,Math.round(Math.min(...ticks.map(l=>l.x+l.width/2))*width)-20),
    right=Math.min(width-12,Math.round(Math.max(...ticks.map(l=>l.x+l.width/2))*width)+16);
  if(!(left<right&&right-left>100&&y>anchor.y*height&&y<axisY*height&&axisY<.8))throw new Error('实际图表范围不满足安全扫描边界');
  const points=[];for(let x=left;x<=right;x+=stepPx)points.push(x);if(points.at(-1)!==right)points.push(right);
  return {width,height,y,left,right,step_px:stepPx,points,axis_labels:ticks.map(l=>l.text),observed_model:titles[0].text,
    date_mapping:'unverified_coordinates_only',coverage_verified:false};
}

export class MobileSnapshotCollector{
  constructor({app,projectDir,baseUrl='http://127.0.0.1:8765',workerId='codex-cua-capture-v1',worker}={}){
    this.root=projectDir;this.worker=worker||new NativeMobileWorker({app,projectDir,baseUrl,workerId});this.session=null;
  }
  async authorize(catalog){
    if(this.session?.batch_id&&this.session.catalog.id===catalog.id){
      const control=await this.worker.api(`/api/mobile/capture-batches/${this.session.batch_id}/control`);
      if(control.allowed!==true)throw new Error(control.message||'截图批次授权已撤销，未操作手机');
      return control;
    }
    const workflow=await this.worker.api('/api/workflow/status');
    if(workflow?.mobile?.operator_paused!==false)throw new Error('手机采集支线已暂停；未截图或点击');
    const target=await this.worker.assertTargetAllowed(catalog.id);
    if(target.catalog_version!==catalog.version||target.market_scope_id!==catalog.market_scope_id)throw new Error('目标目录版本或范围已变化，停止扫描');
    if(JSON.stringify(target.task_ids)!==JSON.stringify(catalog.task_ids))throw new Error('目标监控任务集合已变化，停止扫描');
    return target;
  }
  async checkpoint(){
    if(!this.session)return;
    const dir=path.join(this.root,'data/mobile_capture_sessions');await fs.mkdir(dir,{recursive:true,mode:0o700});
    const file=path.join(dir,this.session.session_id+'.json'),tmp=file+'.tmp';
    await fs.writeFile(tmp,JSON.stringify(this.session,null,2),{mode:0o600});await fs.rename(tmp,file);
  }
  frame(shot,x=null){return {evidence_filename:shot.filename,evidence_hash:hash(shot.bytes),captured_at:new Date().toISOString(),chart_x:x===null?null:x/this.session.layout.width,screen_date:null};}
  async begin(catalogId,{navigate=false,stepPx=3}={}){
    if(this.session?.status==='capturing')throw new Error('当前型号截图扫描尚未结束，不能并发切换');
    const rows=(await this.worker.api('/api/catalog')).items,catalog=rows.find(c=>c.id===catalogId);
    if(!catalog)throw new Error('指定目录不存在');
    const target=await this.worker.assertTargetAllowed(catalogId);catalog.task_ids=target.task_ids;
    await this.authorize(catalog);this.worker.catalog=catalog;
    if(navigate){const opened=await this.worker.openSearch(normalizeDiscoveryQuery(catalog));if(opened.status!=='market')throw new Error(opened.message||'未到行情页');}
    const screen=await this.worker.inspectScreen('capture_overview'),layout=screenshotSweepLayout(screen,catalog,stepPx);
    await this.authorize(catalog);
    const batch=await this.worker.api('/api/mobile/capture-batches',{catalog_id:catalogId,label:'MCP截图优先；逐日浮层待gpt-6.1-sol识别'});
    this.session={session_id:randomUUID(),batch_id:batch.id,catalog,status:'capturing',layout,next_index:0,frames:[],registered:0,created_at:new Date().toISOString(),last_error:null};
    this.session.frames.push(this.frame(screen));await this.checkpoint();await this.flush();
    await this.worker.heartbeat('ready',`MCP截图优先：${catalog.model}，扫描${layout.points.length}个实际触点；不等待模型识别`);
    return {session_id:this.session.session_id,batch_id:batch.id,model:catalog.model,planned_touches:layout.points.length,layout};
  }
  async setStepPx(stepPx){
    if(!this.session||this.session.status!=='capturing'||!Number.isInteger(stepPx)||stepPx<1||stepPx>8)throw new Error('没有可调整的未完成截图计划');
    const {layout,next_index}=this.session,done=layout.points.slice(0,next_index),remaining=[];
    for(let x=next_index?done.at(-1)+stepPx:layout.left;x<=layout.right;x+=stepPx)remaining.push(x);
    if(remaining.at(-1)!==layout.right)remaining.push(layout.right);
    layout.points=[...done,...remaining];layout.step_px=stepPx;await this.checkpoint();
    return {already_captured:done.length,remaining_touches:remaining.length,step_px:stepPx,coverage_verified:false};
  }
  async flush(){
    const pending=this.session.frames.slice(this.session.registered);
    for(let n=0;n<pending.length;n+=100){
      const frames=pending.slice(n,n+100);await this.worker.api('/api/mobile/captures',{batch_id:this.session.batch_id,frames});
      this.session.registered+=frames.length;await this.checkpoint();
    }
  }
  async runChunk({maxFrames=30,maxDurationMs=22000}={}){
    const session=this.session;if(!session||session.status!=='capturing')throw new Error('没有已准备的截图扫描');
    const begin=Date.now();let captured=0;
    try{
      await this.authorize(session.catalog);
      const screen=await this.worker.inspectScreen('capture_chunk_check'),checked=screenshotSweepLayout(screen,session.catalog,session.layout.step_px);
      if(checked.width!==session.layout.width||checked.height!==session.layout.height||Math.abs(checked.y-session.layout.y)>5)throw new Error('图表位置或型号页面已变化，停止旧坐标扫描');
      for(;session.next_index<session.layout.points.length&&captured<maxFrames&&Date.now()-begin<maxDurationMs;){
        await this.authorize(session.catalog);
        const x=session.layout.points[session.next_index];
        await this.worker.app.click([x,session.layout.y],{durationMs:120});
        const shot=await this.worker.saveShot('capture_tooltip');
        const dims=this.worker.dimensions(shot.bytes);
        if(dims.width!==session.layout.width||dims.height!==session.layout.height)throw new Error('镜像截图尺寸已变化，停止旧坐标扫描');
        session.frames.push(this.frame(shot,x));session.next_index++;captured++;
        await this.checkpoint();
      }
      await this.flush();
      if(session.next_index===session.layout.points.length)session.status='captured';
      await this.worker.heartbeat('ready',`${session.catalog.model}已存${session.frames.length}张截图；触点${session.next_index}/${session.layout.points.length}，日期/价格识别单独排队`);
      await this.checkpoint();
      return {model:session.catalog.model,batch_id:session.batch_id,status:session.status,captured,stored_frames:session.frames.length,registered:session.registered,remaining_touches:session.layout.points.length-session.next_index,elapsed_ms:Date.now()-begin,verified_dates:0};
    }catch(error){
      session.last_error=String(error.message);session.status='paused';await this.checkpoint();
      try{await this.flush();}catch{}
      try{await this.worker.heartbeat('waiting_user',session.last_error);}catch{}
      return {status:'paused',message:session.last_error,stored_frames:session.frames.length,registered:session.registered,verified_dates:0};
    }
  }
}
