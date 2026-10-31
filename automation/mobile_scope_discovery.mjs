/** Conservative discovery from actual iPhone App screens. No API price calls,
 * date/value interpolation, scope widening, task claiming, or UI on import. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
const DAY=86400000;
const identity=value=>String(value||'').normalize('NFKC').replace(/\s+/g,'').toLowerCase();
const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const DEFAULTS=new Set(['选择','+选择','全部','不限']);
// Changed evidence rules permit one new bounded attempt while retaining old
// discovery records for audit; already verified scopes remain untouched.
export const DISCOVERY_VERSION='screen-scope-v4-double-screen';
const DISCOVERY_CACHE_VERSION=1;
const MAX_DISCOVERY_ATTEMPTS=6;
const catalogVersion=value=>Number.isSafeInteger(value)&&value>0?value:null;
const attemptCount=record=>Number.isSafeInteger(record?.attempt_count)&&record.attempt_count>0?record.attempt_count:1;

function mayAttempt(catalog,record){
  if(!record)return true;
  if(record.status!=='needs_review'||attemptCount(record)>=MAX_DISCOVERY_ATTEMPTS)return false;
  const savedVersion=catalogVersion(record.catalog_version),currentVersion=catalogVersion(catalog.version);
  if(savedVersion!==null)return currentVersion!==null&&currentVersion>savedVersion;
  // Older caches did not store the version returned by their own PATCH. Give
  // them one upgrade attempt, then require a later explicit catalog revision.
  return record.cache_version!==DISCOVERY_CACHE_VERSION;
}

function asciiModelWithCategorySuffix(catalog){
  const model=String(catalog.model||'').trim(),query=String(catalog.query||'').normalize('NFKC').trim();
  if(!/^[\x20-\x7e]+$/.test(model)||!/[A-Za-z0-9]/.test(model))return null;
  const compact=identity(query),base=identity(model);
  return compact.startsWith(base)&&['主板','显卡','处理器','内存'].includes(compact.slice(base.length))?model:null;
}

export function normalizeDiscoveryQuery(catalog) {
  // Remove a category hint only when the entire canonical ASCII model already
  // matches. Never remove extra specification, condition, or brand words.
  return String(asciiModelWithCategorySuffix(catalog)||catalog.query||catalog.model||'').normalize('NFKC').trim()
    .replace(/^i9\s*[- ]\s*(?=\d)/i,'').replace(/^RTX\s*/i,'').trim();
}

function comparison(value,category){
  let text=String(value||'').normalize('NFKC').trim().replace(/^i9\s*[- ]\s*(?=\d)/i,'').replace(/^RTX\s*/i,'');
  // Canonical storage loses the Ryzen tier; equivalence retains the complete
  // four-digit designation and suffix. The actual R7 search text stays intact.
  if(category==='cpu')text=text.replace(/^RYZEN\s*/i,'').replace(/^R[3579]\s*(?=\d{4})/i,'');
  return identity(text);
}

function yesterdayShanghai(){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(p=>[p.type,p.value]));
  return new Date(Date.UTC(Number(parts.year),Number(parts.month)-1,Number(parts.day))-DAY).toISOString().slice(0,10);
}

function axisProof(lines,anchorY,endDate){
  const groups=[];
  for(const line of lines){
    if(Number(line.y)<=anchorY||!/^\d{2}[./]\d{2}$/.test(String(line.text||'').trim()))continue;
    let group=groups.find(g=>Math.abs(g[0].y-line.y)<.015);
    if(!group){group=[];groups.push(group);}group.push(line);
  }
  const ticks=groups.sort((a,b)=>b.length-a.length)[0]||[];
  const end=new Date(endDate+'T00:00:00Z').getTime();
  const values=[];
  for(const tick of ticks){
    const [month,day]=tick.text.trim().split(/[./]/).map(Number);const candidates=[];
    for(let year=new Date(end).getUTCFullYear()-1;year<=new Date(end).getUTCFullYear()+1;year++){
      const stamp=Date.UTC(year,month-1,day),parsed=new Date(stamp);
      if(parsed.getUTCMonth()!==month-1||parsed.getUTCDate()!==day)continue;
      // App axes may round their rightmost tick into the following month.
      // This tolerance is only for recognizing a chart, never a readout date.
      if(end-220*DAY<=stamp&&stamp<=end+31*DAY)candidates.push(stamp);
    }
    if(candidates.length===1)values.push(candidates[0]);
  }
  const unique=[...new Set(values)].sort((a,b)=>a-b);
  const span=unique.length>1?(unique.at(-1)-unique[0])/DAY:0;
  if(unique.length<3||span<30||span>220)return {ok:false,reason:'未核实至少三个真实日期轴刻度及30至220日的合理历史跨度'};
  return {ok:true,start:new Date(unique[0]).toISOString().slice(0,10),end:new Date(unique.at(-1)).toISOString().slice(0,10),
    ticks:unique.length,span_days:span,short_history:span<180,includes_future_tick:unique.at(-1)>end,
    message:'仅为实际显示的日期轴刻度范围，可能含未来常规刻度；不证明逐日数据、实际可读日期或180日完整覆盖。'};
}

export function verifyDiscoveryScreen(catalog,screen,{endDate=yesterdayShanghai()}={}){
  if(catalog.scope?.verified===true)return {ok:false,reason:'已核对范围不参与自动发现或覆盖'};
  const lines=screen.lines||[];
  const anchors=lines.filter(l=>identity(l.text).includes('近7日成交均价'));
  if(anchors.length!==1)return {ok:false,reason:'未找到唯一官方近7日成交均价指标；保留待复核，不能断言永久无行情'};
  const anchor=anchors[0];
  const modelKey=comparison(catalog.model,catalog.category);
  const titleSource=screen.model_lines?.length?screen.model_lines:lines;
  const titles=titleSource.filter(l=>Number(l.y)>.18&&Number(l.y)<Number(anchor.y)-.04&&Number(l.x)<.8);
  const matched=titles.filter(l=>comparison(l.text,catalog.category)===modelKey);
  const observed=titles.map(l=>l.text).filter(t=>/[A-Za-z0-9]/.test(t||'')).join(' / ').slice(0,300);
  if(matched.length!==1)return {ok:false,observed_model:observed,reason:'实际主标题未唯一精确匹配目标完整型号/完整后缀；查询框不算型号证据'};
  const title=matched[0];const bottom=Number(title.y)+Number(title.height||0);
  const region=lines.filter(l=>Number(l.y)>bottom&&Number(l.y)<Number(anchor.y));
  const scope=catalog.scope||{};
  const selectorMap={'品牌':'brand','功能状态':'function_status','成色':'condition','存储容量':'storage','显存容量':'storage','内存容量':'storage','容量':'storage','频率':'frequency','套装':'kit','版本':'version','颜色':'color','网络制式':'network','包装':'packaging'};
  const required=catalog.category==='phone'?['存储容量','成色']:['品牌','功能状态','成色'];
  const headings=Object.keys(selectorMap).map(label=>({label,line:region.find(l=>identity(l.text)===label)})).filter(h=>h.line);
  if(required.some(label=>!headings.some(h=>h.label===label)))return {ok:false,observed_model:title.text,reason:'实际筛选布局不完整或不受支持，无法证明所有未指定筛选均为默认范围'};
  if(region.some(l=>headings.some(h=>Math.abs(Number(l.y)-Number(h.line.y))<.012)&&/[\u4e00-\u9fff]/.test(l.text||'')
      &&!Object.hasOwn(selectorMap,identity(l.text))&&!DEFAULTS.has(identity(l.text))))
    return {ok:false,observed_model:title.text,reason:'筛选标题行出现尚未识别的额外控件，不能宣称全部默认'};
  const filters=scope.filters||{};const used=new Set(),selections={};
  for(const {label,line} of headings){
    const field=selectorMap[label];let expected=scope[field];
    if(expected===undefined||expected===null){
      const key=[label,field].find(k=>Object.hasOwn(filters,k));
      if(key){expected=filters[key];used.add(key);}
    }
    const values=region.filter(l=>Math.abs(Number(l.x)-Number(line.x))<.2&&Number(l.y)>=Number(line.y)-.01&&Number(l.y)<Number(line.y)+.09&&l!==line);
    const actual=values.map(l=>identity(l.text));
    if(expected!==null&&expected!==undefined&&String(expected)!==''){
      if(!actual.includes(identity(expected)))return {ok:false,observed_model:title.text,reason:`实际${label}未精确匹配原目标范围；不会自动选择或改变经济口径`};
      selections[label]=identity(expected);
    }else if(!actual.some(value=>DEFAULTS.has(value))){
      return {ok:false,observed_model:title.text,reason:`未指定的${label}不是明确“+选择/全部/不限”，必选默认条件须复核`};
    }else selections[label]='default';
  }
  if(Object.keys(filters).some(key=>!used.has(key)))return {ok:false,observed_model:title.text,reason:'目录包含尚未完整核对的额外筛选字段'};
  // Every numeric specification carried by the complete canonical model must
  // remain in the matched main title, not merely in an entered query.
  const specs=String(catalog.model).normalize('NFKC').match(/\d+(?:\.\d+)?\s*(?:GB|TB)|DDR\s*[345]|\d{4}\s*(?:MHz|MT\/s)|\d+\s*(?:GB)?\s*[×x]\s*\d+/gi)||[];
  if(specs.some(spec=>!identity(title.text).includes(identity(spec))))return {ok:false,observed_model:title.text,reason:'容量/代际/频率/套装规格未在实际主标题完整出现'};
  const axis=axisProof(lines,Number(anchor.y),endDate);
  if(!axis.ok)return {...axis,observed_model:title.text};
  const aliases=[catalog.model,normalizeDiscoveryQuery({...catalog,query:catalog.model}),...(scope.aliases||[]),title.text];
  const complete=[...new Set(aliases.filter(a=>comparison(a,catalog.category)===modelKey))].slice(0,30);
  return {ok:true,observed_model:title.text,aliases:complete,axis,
    scope_signature:hash([catalog.market_scope_id,modelKey,selections,axis.start,axis.end])};
}

export class MobileScopeDiscovery{
  constructor({projectDir,worker,log=()=>{},wait=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){this.root=projectDir;this.worker=worker;this.log=log;this.wait=wait;
    this.file=path.join(projectDir,'data/native_mobile_discovery.json');this.records=null;}
  async load(){if(this.records)return;try{const value=JSON.parse(await fs.readFile(this.file,'utf8'));this.records=value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{this.records={};}}
  key(catalog){
    const parts=[DISCOVERY_VERSION,catalog.id,catalog.market_scope_id,catalog.query,catalog.model];
    // Only targets affected by this navigation repair get one fresh bounded
    // attempt; unrelated failed-scope evidence is not retried indefinitely.
    if(asciiModelWithCategorySuffix(catalog))parts.push('ascii-category-entry-v1',normalizeDiscoveryQuery(catalog));
    return hash(parts);
  }
  async save(){await fs.mkdir(path.dirname(this.file),{recursive:true});const temp=this.file+'.tmp';await fs.writeFile(temp,JSON.stringify(this.records,null,2),{mode:0o600});await fs.rename(temp,this.file);}
  async next(catalogs){
    await this.load();return catalogs.find(c=>c.enabled&&c.scope?.verified!==true&&!['unsupported','no_market'].includes(c.status)&&mayAttempt(c,this.records[this.key(c)]));
  }
  async discover(catalog,{endDate}={}){
    await this.load();if(catalog.scope?.verified===true)return {status:'skipped',catalog_id:catalog.id};
    if(typeof this.worker.assertTargetAllowed!=='function')throw new Error('发现流程缺少实时监控任务授权检查');
    await this.worker.assertTargetAllowed(catalog.id);
    if(this.worker.remaining?.length||this.worker.lease)throw new Error('发现流程不能切换已领取日期任务的镜像页面');
    const key=this.key(catalog),prior=this.records[key];if(!mayAttempt(catalog,prior))return {...prior,status:'already_attempted'};
    const query=normalizeDiscoveryQuery(catalog);let screen,proof;const screenshots=[];let verificationEvidence=[];
    const oldCatalog=this.worker.catalog;this.worker.catalog=catalog;
    try{
      const opened=await this.worker.openSearch(query);
      screen=opened;
      if(opened.status==='needs_ai_guidance')return {...opened,catalog_id:catalog.id};
      if(opened.status!=='market')proof={ok:false,reason:opened.message||'未能进入可核对的官方行情页'};
      else {
        let previous=null;const deadline=Date.now()+90000;
        for(let attempt=0;attempt<6&&Date.now()<deadline;attempt++){
          if(attempt)await this.wait(250);
          screen=await this.worker.inspectScreen(`discovery_proof_${attempt+1}`);
          const filename=screen.filename||screen.evidence_filename;
          if(filename)screenshots.push(filename);
          proof=verifyDiscoveryScreen(catalog,screen,{endDate});
          if(proof.ok&&filename){
            if(previous&&previous.filename!==filename&&previous.proof.scope_signature===proof.scope_signature){
              verificationEvidence=[previous.filename,filename];break;
            }
            previous={proof,filename};
          }else {
            previous=null;
            // Price can render before its model card. Only title/loading
            // uncertainty gets bounded fresh frames; a proven scope mismatch
            // is not retried until a different screenshot happens to pass.
            if(!/主标题|未找到唯一官方/.test(proof.reason||''))break;
          }
        }
        if(verificationEvidence.length!==2)proof={ok:false,observed_model:proof?.observed_model,
          reason:`布局尚未稳定：${screenshots.length}次新截图未取得两次独立且型号/筛选范围一致的完整证据；${proof?.reason||'独立复核不足'}`};
      }
    }catch(error){
      if(['STOPPED','ABORT_ERR','TARGET_NOT_MONITORED','TARGET_AUTH_UNAVAILABLE','NEEDS_AI_GUIDANCE'].includes(error.code)||/LOCKED|DISCONNECTED|STOPPED|前台|辅助功能|屏幕录制/.test(error.message)||['locked','disconnected','verification'].includes(error.deviceStatus))throw error;
      screen=this.worker.lastScreen;proof={ok:false,reason:String(error.message)};
    }finally{this.worker.catalog=oldCatalog;}
    const filename=screen?.filename||screen?.evidence_filename||null;
    if(proof.ok&&!filename)proof={ok:false,observed_model:proof.observed_model,reason:'实际页面没有可追溯截图文件，不自动批准'};
    await this.worker.assertTargetAllowed(catalog.id);
    const latest=(await this.worker.api('/api/catalog')).items.find(c=>c.id===catalog.id);
    if(!latest||latest.scope?.verified===true||latest.market_scope_id!==catalog.market_scope_id||latest.query!==catalog.query||
       (catalogVersion(catalog.version)!==null&&latest.version!==catalog.version))
      return {status:'scope_changed',catalog_id:catalog.id,reason:'读取期间目录已核对或变更，未覆盖新的批准范围'};
    const history=prior?[...(Array.isArray(prior.history)?prior.history:[]),Object.fromEntries(Object.entries(prior).filter(([name])=>name!=='history'))]:[];
    const record={catalog_id:catalog.id,query,market_scope_id:catalog.market_scope_id,status:proof.ok?'ready':'needs_review',
      observed_model:proof.observed_model||null,reason:proof.reason||(proof.axis?.short_history?
        `完整型号与原范围已核对；历史轴跨度仅${proof.axis.span_days}日，不代表180日完整，缺日仍须逐日核实`:
        '完整型号、原筛选范围与历史日期轴已核对；刻度不是逐日覆盖证据'),
      evidence:filename,verification_count:proof.ok?2:0,verification_evidence:verificationEvidence,
      attempted_screenshots:screenshots,attempted_at:new Date().toISOString(),parser_version:DISCOVERY_VERSION,axis:proof.axis||null,
      cache_version:DISCOVERY_CACHE_VERSION,attempt_count:prior?attemptCount(prior)+1:1,history};
    const metadata={...catalog.scope};if(filename)metadata.evidence=filename;
    if(proof.ok){metadata.verified=true;metadata.verified_at=record.attempted_at;metadata.aliases=proof.aliases;}
    const detail=`实际页面：${record.observed_model||'未能可靠读取'}；${record.reason}；证据：${filename||'未取得可用截图'}`;
    // The verified search entry is navigation metadata, not an economic scope.
    // Existing jobs retain their original query for audit and optional OCR checks.
    const patched=await this.worker.api(`/api/catalog/${catalog.id}`,{status:record.status,scope:metadata,
      ...(proof.ok?{query}:{}),last_error:proof.ok?null:detail.slice(0,1900)});
    // A discovery PATCH itself increments catalog.version. Persist that returned
    // version, not the input version, so our own write cannot schedule a retry.
    record.catalog_version=catalogVersion(patched?.version);
    this.records[key]=record;await this.save();this.log({event:'scope_discovery',...record});return record;
  }
}
