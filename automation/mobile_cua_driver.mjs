/** Mobile worker executed inside the authorized cua_repl runtime.
 * All UI input/capture uses the injected documented CUA App APIs. No alternate
 * OS input, screen capture, browser interception, or phone-private API is used.
 * Local subprocesses perform OCR of already captured files only.
 * The backend remains honest: absent this live worker, the device is disconnected.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec = promisify(execFile);
const DAY = 86400000;
const identity = value => String(value||'').normalize('NFKC').replace(/\s+/g,'').toLowerCase();

export class MobileCuaWorker {
  constructor({app,projectDir,baseUrl='http://127.0.0.1:8765',workerId='codex-cua-mobile-v1'}) {
    this.app=app;this.root=projectDir;this.base=baseUrl;this.workerId=workerId;
    this.dir=path.join(projectDir,'data/market_evidence');this.serial=0;this.lease=null;
    this.catalog=null;this.remaining=[];this.anchors=new Map();this.trace=[];
  }
  async api(url,payload) {
    const response=await fetch(this.base+url,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const value=await response.json();if(!response.ok)throw new Error(value.detail||`HTTP ${response.status}`);return value;
  }
  async heartbeat(status='ready',message='手机已连接授权的CUA会话') {
    return this.api('/api/mobile/heartbeat',{worker_id:this.workerId,lease_token:this.lease,device_status:status,message});
  }
  async claim(catalogId) {
    if(this.remaining.length)return this.remaining.length;
    await this.heartbeat('ready');
    const result=await this.api('/api/mobile/claim',{worker_id:this.workerId,catalog_id:catalogId,limit:40});
    this.lease=result.lease_token;this.catalog=result.catalog;this.remaining=result.jobs;
    if(result.jobs.length){
      const batches=await this.api('/api/mobile/batches');const batch=batches.batches.find(b=>b.id===result.jobs[0].batch_id);
      this.end=batch.end_date;this.start=new Date(new Date(batch.end_date).getTime()-(batch.days-1)*DAY).toISOString().slice(0,10);
    }
    return result.jobs.length;
  }
  async saveShot(label='screen') {
    await fs.mkdir(this.dir,{recursive:true});
    const state=await this.app.getAXState({emit:false});
    if(/镜像已锁定|使用触控ID|输入.*登录信息/.test(state))throw new Error('DEVICE_LOCKED');
    const bytes=await this.app.getScreenshot({emit:false});
    const ext=bytes[0]===255&&bytes[1]===216?'jpg':'png';
    const filename=`mobile_${this.catalog?.id||0}_${Date.now()}_${++this.serial}_${label}.${ext}`;
    const absolute=path.join(this.dir,filename);await fs.writeFile(absolute,bytes);
    return {filename,absolute,bytes};
  }
  async rawOcr(absolute) {
    return (await this.ocrData(absolute)).lines;
  }
  async ocrData(absolute) {
    const out=await exec(path.join(this.root,'output/bin/mobile-ocr'),[absolute],{cwd:this.root,timeout:20000,maxBuffer:2e6});
    const value=JSON.parse(out.stdout);if(value.error)throw new Error(value.error);return value;
  }
  async inspectScreen(label='inspect') {
    const shot=await this.saveShot(label);
    return {...shot,...await this.ocrData(shot.absolute),...this.dimensions(shot.bytes)};
  }
  async openSearch(query) {
    if(this.remaining.length)throw new Error('切换型号前须完成或释放当前租约');
    let screen=await this.inspectScreen('navigation');
    // The search bar and its clear control have been verified on this App's
    // search/edit screens. Re-observe after every state change and scale using
    // the current captured dimensions; never carry chart coordinates over.
    await this.app.click([Math.round(screen.width*.38),Math.round(screen.height*.131)]);
    await this.app.getAXState({emit:false});screen=await this.inspectScreen('search_edit');
    const search=screen.lines.find(l=>l.text.replace(/\s/g,'')==='搜索'&&l.y<.16);
    if(!search)throw new Error('未显示已核对的搜索编辑页，停止切换');
    let empty=false;
    for(let n=0;n<3;n++){
      await this.app.click([Math.round(screen.width*.806),Math.round(screen.height*.131)]);
      await this.app.getAXState({emit:false});screen=await this.inspectScreen('search_clear');
      const text=screen.lines.filter(l=>l.y>.105&&l.y<.15&&l.x>.1&&l.x<.8).map(l=>l.text).join('');
      if(text.includes('搜索宝贝')||!text){empty=true;break;}
    }
    if(!empty)throw new Error('旧查询未成功清空，拒绝追加输入');
    await this.app.click([Math.round(screen.width*.34),Math.round(screen.height*.131)]);
    await this.app.getAXState({emit:false});
    await this.app.typeText(query.toLowerCase());await this.app.getAXState({emit:false});
    screen=await this.inspectScreen('search_query');
    const queryLines=screen.query_lines?.length?screen.query_lines:screen.lines.filter(l=>l.y>.11&&l.y<.15&&l.x>.1&&l.x<.8);
    const actual=queryLines.map(l=>l.text.replace(/\s*[xX×]\s*$/,'')).join('');
    if(identity(actual)!==identity(query))throw new Error(`输入查询尚未核对一致：${actual}`);
    // Submit the verified input itself; search suggestions can lag behind text.
    const submit=screen.lines.find(l=>l.text.replace(/\s/g,'')==='搜索'&&l.y<.16);
    if(!submit)throw new Error('未找到可见搜索/行情入口');
    await this.app.click([Math.round((submit.x+submit.width/2)*screen.width),Math.round((submit.y+submit.height/2)*screen.height)]);
    await this.app.getAXState({emit:false});
    for(let n=0;n<7;n++){
      screen=await this.inspectScreen('market_scope');
      if(screen.lines.some(l=>l.text.replace(/\s/g,'').includes('近7日成交均价'))){this.layout=null;this.anchors.clear();return {status:'market',evidence_filename:screen.filename,lines:screen.lines};}
      const tab=screen.lines.find(l=>l.text.trim()==='行情'&&l.y>.15&&l.y<.2);
      if(tab&&n===0){await this.app.click([Math.round((tab.x+tab.width/2)*screen.width),Math.round((tab.y+tab.height/2)*screen.height)]);await this.app.getAXState({emit:false});}
    }
    return {status:'needs_review',message:'已打开查询但未识别行情价格区，保留入口截图核对',evidence_filename:screen.filename,lines:screen.lines};
  }
  async applyScope(scope) {
    if(Object.keys(scope.filters||{}).length)throw new Error('当前未实现此筛选控件，不能把目录值当作已应用');
    const wanted=[scope.storage,scope.condition].filter(Boolean);
    let screen=await this.inspectScreen('scope_before');
    if(!wanted.length)return {status:'market',...screen,evidence_filename:screen.filename};
    const normal=l=>identity(l.text);
    const applied=()=>!screen.lines.some(l=>l.text.includes('选择规格'))&&wanted.every(value=>screen.lines.some(l=>l.y>.23&&l.y<.34&&normal(l)===identity(value)));
    if(applied())return {status:'market',...screen,evidence_filename:screen.filename};
    let header;
    for(let n=0;n<7;n++){
      header=screen.lines.find(l=>/^(存储容量|显存容量|内存容量)$/.test(l.text.trim())&&l.y>.2&&l.y<.34);
      if(header)break;screen=await this.inspectScreen('scope_header');
    }
    if(!header)throw new Error('未找到可见容量筛选，须复核该规格是否可用');
    await this.app.click([Math.round((header.x+header.width/2)*screen.width),Math.round((header.y+header.height/2+.022)*screen.height)]);
    await this.app.getAXState({emit:false});
    const stableOption=async value=>{
      let previous;
      for(let n=0;n<8;n++){
        screen=await this.inspectScreen('scope_options');
        const option=screen.lines.find(l=>normal(l)===identity(value)&&l.y>.35);
        if(option&&previous&&Math.abs(previous.y-option.y)<.003)return option;
        previous=option;
      }
      throw new Error(`未找到稳定可见的精确筛选项：${value}`);
    };
    if(scope.storage){
      const option=await stableOption(scope.storage);
      await this.app.click([Math.round((option.x+option.width/2)*screen.width),Math.round((option.y+option.height/2)*screen.height)]);
      await this.app.getAXState({emit:false});
    }
    if(scope.condition){
      screen=await this.inspectScreen('scope_condition_tab');
      const tab=screen.lines.find(l=>l.text.trim()==='成色'&&l.x<.25&&l.y>.35);
      if(!tab)throw new Error('成色筛选入口不可见，禁止沿用默认条件');
      await this.app.click([Math.round((tab.x+tab.width/2)*screen.width),Math.round((tab.y+tab.height/2)*screen.height)]);
      await this.app.getAXState({emit:false});
      const option=await stableOption(scope.condition);
      await this.app.click([Math.round((option.x+option.width/2)*screen.width),Math.round((option.y+option.height/2)*screen.height)]);
      await this.app.getAXState({emit:false});
    }
    screen=await this.inspectScreen('scope_confirm');
    const confirm=screen.lines.find(l=>l.text.trim()==='确认'&&l.y>.8);
    if(!confirm)throw new Error('筛选确认按钮不可见');
    await this.app.click([Math.round((confirm.x+confirm.width/2)*screen.width),Math.round((confirm.y+confirm.height/2)*screen.height)]);
    await this.app.getAXState({emit:false});
    for(let n=0;n<8;n++){
      screen=await this.inspectScreen('scope_applied');
      if(applied()){this.layout=null;this.anchors.clear();return {status:'market',...screen,evidence_filename:screen.filename};}
    }
    throw new Error('确认后实际筛选标签未吻合；禁止采集其他范围曲线');
  }
  dimensions(bytes) {
    const b=Buffer.from(bytes);
    if(b[0]===137)return {width:b.readUInt32BE(16),height:b.readUInt32BE(20)};
    for(let i=2;i<b.length;){if(b[i]!==255){i++;continue;}const marker=b[i+1];if(marker===216||marker===217){i+=2;continue;}const size=b.readUInt16BE(i+2);if([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker))return {height:b.readUInt16BE(i+5),width:b.readUInt16BE(i+7)};i+=size+2;}
    throw new Error('图片尺寸不可读');
  }
  async calibrate({reset=true}={}) {
    const shot=await this.saveShot('layout');const lines=await this.rawOcr(shot.absolute);
    const dims=this.dimensions(shot.bytes);const anchors=lines.filter(l=>l.text.replace(/\s/g,'').includes('近7日成交均价'));
    if(anchors.length!==1)throw new Error('未找到唯一行情标签，手机页面需要核对');
    const labels=lines.filter(l=>/^\d{2}[./]\d{2}$/.test(l.text.trim())&&l.y>anchors[0].y);
    const groups=[];
    for(const l of labels){let g=groups.find(g=>Math.abs(g[0].y-l.y)<.015);if(!g){g=[];groups.push(g);}g.push(l);}
    const ticks=groups.sort((a,b)=>b.length-a.length)[0];
    if(!ticks||ticks.length<3)throw new Error('未找到半年图的日期轴，禁止盲点屏幕');
    const axisY=ticks.reduce((s,l)=>s+l.y+l.height/2,0)/ticks.length;
    this.layout={width:dims.width,height:dims.height,
      y:Math.round((axisY-.082)*dims.height),left:Math.max(10,Math.round(Math.min(...ticks.map(l=>l.x+l.width/2))*dims.width)-20),
      right:Math.min(dims.width-12,Math.round(Math.max(...ticks.map(l=>l.x+l.width/2))*dims.width)+16)};
    if(reset)this.anchors.clear();return {layout:this.layout,ticks:ticks.map(l=>l.text)};
  }
  async readAt(x) {
    await this.app.click([x,this.layout.y],{durationMs:120});
    const shot=await this.saveShot('read');
    const args=['-m','app.mobile_reader',shot.absolute,'--start','2026-04-01','--end','2026-10-30'];
    // Window is the actual batch ±31 days, for recognizing nearby non-target dates.
    args[args.indexOf('--start')+1]=new Date(new Date(this.start).getTime()-31*DAY).toISOString().slice(0,10);
    args[args.indexOf('--end')+1]=new Date(new Date(this.end).getTime()+1*DAY).toISOString().slice(0,10);
    let out;try{out=await exec(path.join(this.root,'.venv/bin/python'),args,{cwd:this.root,timeout:20000,maxBuffer:2e6});}catch(e){out={stdout:e.stdout||''};}
    let result;try{result=JSON.parse(out.stdout);}catch{result={status:'needs_review',message:'OCR未返回有效结构'};}
    if(result.status==='locked'||result.status==='disconnected')throw new Error(`DEVICE_${result.status.toUpperCase()}`);
    if(result.status==='read'){
      const allowed=[this.catalog.model,this.catalog.query,...(this.catalog.scope.aliases||[])].map(identity);
      if(!allowed.includes(identity(result.observed_model))&&!allowed.includes(identity(result.observed_query)))throw new Error(`手机页面型号不符：${result.observed_model}`);
      // A tap on an App date with no data can leave the old tooltip visible.
      // Keep the first observed coordinate rather than moving that date's
      // anchor to every later failed touch. Displayed dates still gate writes.
      if(!this.anchors.has(result.displayed_date))this.anchors.set(result.displayed_date,{x,price:result.price});
    }
    this.trace.push({x,date:result.displayed_date,status:result.status,evidence:shot.filename,message:result.message});
    return {...result,evidence_filename:shot.filename};
  }
  proposeX(day) {
    const stamp=new Date(day).getTime();const known=[...this.anchors].map(([d,v])=>({t:new Date(d).getTime(),...v})).sort((a,b)=>a.t-b.t);
    if(known.length>=2){let a=known[0],b=known.at(-1);for(let i=1;i<known.length;i++)if(known[i].t>=stamp){a=known[i-1];b=known[i];break;}
      if(b.t!==a.t)return Math.round(a.x+(stamp-a.t)/(b.t-a.t)*(b.x-a.x));}
    if(known.length===1)return Math.round(known[0].x+(stamp-known[0].t)/DAY*3.5);
    return Math.round((this.layout.left+this.layout.right)/2);
  }
  async locate(day) {
    const tried=new Set();let x=this.proposeX(day);let best=null;
    const deadline=Date.now()+14000;
    for(let attempt=0;attempt<10&&Date.now()<deadline;attempt++){
      x=Math.max(this.layout.left,Math.min(this.layout.right,Math.round(x)));
      if(tried.has(x)){const near=[...tried].sort((a,b)=>a-b);const direction=best&&best.displayed_date<day?1:-1;x=near.at(direction>0?-1:0)+direction;if(x<this.layout.left||x>this.layout.right)break;}
      tried.add(x);const result=await this.readAt(x);
      if(result.status==='read'){
        best=result;
        if(result.displayed_date===day){const second=await this.readAt(x);if(second.status==='read'&&second.displayed_date===day&&second.price===result.price)return {...second,verification_count:2};throw new Error('双次日期/金额复读不一致');}
        const delta=(new Date(day)-new Date(result.displayed_date))/DAY;
        x=this.proposeX(day);if(tried.has(x))x+=Math.sign(delta);
      }else {x += attempt%2===0?2:-3;}
    }
    return {status:'needs_review',message:`已尝试${tried.size}个实际触点，未核验目标日期${day}；保留待复核，未插值`,tried:[...tried]};
  }
  async runChunk({catalogId,maxJobs=8,maxDurationMs=22000}={}) {
    const start=Date.now();const results=[];
    try {
      await this.app.getAXState({emit:false});
      if(!this.remaining.length){this.lease=null;await this.claim(catalogId);if(!this.remaining.length)return {done:true,results};}
      await this.calibrate({reset:!this.layout});
      await this.heartbeat('reading',`正在核对${this.catalog.model}的真实逐日历史`);
      for(let n=0;n<maxJobs&&this.remaining.length&&Date.now()-start<maxDurationMs;n++){
        const job=this.remaining[0];const value=await this.locate(job.target_date);
        if(value.status==='read'){
          const payload={worker_id:this.workerId,lease_token:this.lease,readings:[{job_id:job.id,displayed_date:value.displayed_date,price:value.price,evidence_filename:value.evidence_filename,observed_model:value.observed_model,observed_query:identity(value.observed_query)===identity(this.catalog.query)?value.observed_query:null,scope:{...this.catalog.scope,verified:true},read_method:'mobile_screen_ocr',ocr_text:value.ocr_text,parser_version:value.parser_version,verification_count:2}]};
          const stored=await this.api('/api/mobile/readings',payload);results.push({date:value.displayed_date,price:value.price,...stored});
        }else {await this.api('/api/mobile/jobs/status',{worker_id:this.workerId,lease_token:this.lease,job_ids:[job.id],status:'needs_review',message:value.message});results.push({date:job.target_date,status:value.status});}
        this.remaining.shift();if(this.remaining.length)await this.heartbeat('reading');
        await this.checkpoint();
      }
      if(!this.remaining.length)this.lease=null;
      await this.checkpoint();
      return {model:this.catalog?.model,results,remaining_claimed:this.remaining.length,elapsed_ms:Date.now()-start};
    }catch(error){
      try{await this.heartbeat(/LOCKED/.test(error.message)?'locked':'waiting_user',String(error.message));}catch{}
      this.remaining=[];this.lease=null;return {needs_user:true,error:String(error.message),results};
    }
  }
  async checkpoint() {
    await fs.writeFile(path.join(this.root,'data/mobile_worker_trace.json'),JSON.stringify({worker_id:this.workerId,catalog_id:this.catalog?.id,updated_at:new Date().toISOString(),trace:this.trace.slice(-1500)},null,2));
  }
}
