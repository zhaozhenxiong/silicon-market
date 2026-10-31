/** Image-only recognition via the user's existing official Codex CLI session.
 * No SDK/key/OAuth extraction, model substitution, phone input or raw mutation.
 * Import/construction performs no CLI call. Schema output is still unverified
 * evidence: canonical price history must retain independent scope/date checks.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash,randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {fileURLToPath,pathToFileURL} from 'node:url';
const executeFile=promisify(execFile);
const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const HERE=path.dirname(fileURLToPath(import.meta.url));
export const MODEL='gpt-6.1-sol';
export const ADAPTER='codex_cli_existing_connection';
export const DISABLED_FEATURES=Object.freeze(['shell_tool','apps','plugins','hooks','browser_use','browser_use_external','browser_use_full_cdp_access','computer_use','multi_agent','image_generation','skill_search','workspace_dependencies','goals','sleep_tool','view_image','unbounded_connection_retries']);
const SCHEMA_FILE=path.join(HERE,'codex_price_vision.schema.json');
const PROMPT_FILE=path.join(HERE,'codex_price_vision.prompt.txt');
const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
const KINDS=new Set(['market_overview','daily_tooltip','home','search','locked','verification','disconnected','unknown']);
const REQUIRED=['schema_version','screen_kind','model_text','metric','price','price_text','date','date_text','is_daily_tooltip','confidence','evidence_quotes','warnings'];
const MESSAGES={authentication:'现有Codex CLI登录不可用；未读取或复制凭证',rate_limit:'现有模型连接达到使用限制',connection:'现有CLI模型连接未完成',timeout:'现有CLI图片请求超时，尚未获得有效识别',model_unavailable:'现有连接未接受指定gpt-6.1-sol模型',cli_unavailable:'本机未找到官方Codex CLI',invalid_output:'模型未返回符合价格证据协议的JSON',unexpected_tool_use:'识别请求出现工具调用，结果已拒绝',cli_error:'本机CLI请求未完成'};

export function classifyCliError(error){
  if(error?.killed||error?.code==='ETIMEDOUT'||error?.code==='ABORT_ERR')return 'timeout';
  if(error?.code==='ENOENT')return 'cli_unavailable';
  if(error?.safeCategory)return error.safeCategory;
  // Inspect errors in memory only; never save or return raw CLI/auth logs.
  const text=String(error?.stderr||'')+'\n'+String(error?.stdout||'');
  if(/unauthorized|authentication|401\b|login required|not logged in|invalid.*token/i.test(text))return 'authentication';
  if(/rate.?limit|429\b|quota|usage limit/i.test(text))return 'rate_limit';
  if(/model_not_found|model.{0,60}(?:not supported|not found|unavailable)|unsupported.{0,30}model/i.test(text))return 'model_unavailable';
  if(/connect|network|timed? ?out|stream disconnected|fetch failed/i.test(text))return 'connection';
  return 'cli_error';
}

export function parseEvents(stdout=''){
  const events=[];for(const line of String(stdout).split(/\r?\n/))try{const event=JSON.parse(line);if(event&&typeof event==='object')events.push(event);}catch{}
  const types=[...new Set(events.map(event=>String(event.type||'unknown')))].sort();
  const usageEvent=events.findLast(event=>event.type==='turn.completed'&&event.usage);
  const usage=usageEvent?Object.fromEntries(['input_tokens','cached_input_tokens','output_tokens'].filter(key=>Number.isSafeInteger(usageEvent.usage[key])&&usageEvent.usage[key]>=0).map(key=>[key,usageEvent.usage[key]])):null;
  const toolUsed=events.some(event=>{const item=event.item||{};return /command_execution|tool_call|computer_call|web_search|file_change|collab_tool/i.test(String(item.type||event.type));});
  return {event_types:types,usage,tool_used:toolUsed};
}

export function validateReading(reading){
  const bad=()=>{const error=new Error(MESSAGES.invalid_output);error.safeCategory='invalid_output';throw error;};
  if(!reading||typeof reading!=='object'||Array.isArray(reading)||Object.keys(reading).length!==REQUIRED.length||REQUIRED.some(key=>!Object.hasOwn(reading,key)))bad();
  if(reading.schema_version!=='codex-price-vision-v1'||!KINDS.has(reading.screen_kind)||![null,'rolling_7d_sold_mean'].includes(reading.metric)||typeof reading.is_daily_tooltip!=='boolean')bad();
  for(const key of ['model_text','price_text','date_text'])if(reading[key]!==null&&(typeof reading[key]!=='string'||reading[key].length>3000))bad();
  if(reading.price!==null&&(typeof reading.price!=='number'||!Number.isFinite(reading.price)||reading.price<=0||reading.price>100000000))bad();
  if(reading.date!==null&&(typeof reading.date!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(reading.date)||!Number.isFinite(Date.parse(reading.date+'T00:00:00Z'))||new Date(reading.date+'T00:00:00Z').toISOString().slice(0,10)!==reading.date))bad();
  if(typeof reading.confidence!=='number'||!Number.isFinite(reading.confidence)||reading.confidence<0||reading.confidence>1)bad();
  for(const key of ['evidence_quotes','warnings'])if(!Array.isArray(reading[key])||reading[key].length>30||reading[key].some(value=>typeof value!=='string'||value.length>3000))bad();
  if(reading.screen_kind!=='daily_tooltip'&&(reading.date!==null||reading.date_text!==null||reading.is_daily_tooltip))bad();
  if(reading.is_daily_tooltip!==(reading.screen_kind==='daily_tooltip'))bad();
  if(!['market_overview','daily_tooltip'].includes(reading.screen_kind)&&(reading.price!==null||reading.metric!==null))bad();
  if(reading.price!==null&&(!reading.price_text||!reading.evidence_quotes.some(quote=>quote.includes(reading.price_text))))bad();
  if(reading.date!==null&&(!reading.date_text||!reading.evidence_quotes.some(quote=>quote.includes(reading.date_text))))bad();
  return reading;
}

function validateContext(context){
  if(!context||typeof context!=='object'||Array.isArray(context)||Object.keys(context).some(key=>!['expected_model','target_date','year_context'].includes(key)))throw new Error('视觉上下文包含未知字段');
  if(context.expected_model!==undefined&&(typeof context.expected_model!=='string'||!context.expected_model.trim()||context.expected_model.length>160))throw new Error('expected_model须为1至160字');
  if(context.target_date!==undefined&&(!/^\d{4}-\d{2}-\d{2}$/.test(context.target_date)||!Number.isFinite(Date.parse(context.target_date+'T00:00:00Z'))||new Date(context.target_date+'T00:00:00Z').toISOString().slice(0,10)!==context.target_date))throw new Error('target_date须为真实ISO日期');
  if(context.year_context!==undefined&&(!Number.isSafeInteger(context.year_context)||context.year_context<2000||context.year_context>2100))throw new Error('year_context须为2000至2100整数');
  return context;
}

export function buildArgs({tempDir,imagePath,schemaFile=SCHEMA_FILE,finalFile,prompt}){
  const args=['exec','--model',MODEL,'--sandbox','read-only','--ephemeral','--skip-git-repo-check','--cd',tempDir,
    '--json','--color','never','--image',imagePath,'--output-schema',schemaFile,'--output-last-message',finalFile,
    '-c','mcp_servers={}','-c','model_reasoning_effort="low"'];
  for(const feature of DISABLED_FEATURES)args.push('--disable',feature);
  args.push(prompt);return args;
}

export class CodexPriceVision{
  constructor({projectDir=ROOT,execute=executeFile,timeoutMs=120000}={}){
    if(!Number.isSafeInteger(timeoutMs)||timeoutMs<1000||timeoutMs>180000)throw new Error('视觉请求超时须为1至180秒');
    this.root=path.resolve(projectDir);this.execute=execute;this.timeoutMs=timeoutMs;
    this.statusFile=path.join(this.root,'data/codex_price_vision_status.json');
  }
  async write(file,value){await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});const temp=file+`.${process.pid}.${randomUUID()}.tmp`;await fs.writeFile(temp,JSON.stringify(value,null,2),{mode:0o600});await fs.rename(temp,file);}
  async status(){try{return JSON.parse(await fs.readFile(this.statusFile,'utf8'));}catch{return {schema_version:'codex-price-vision-status-v1',adapter:ADAPTER,model:MODEL,status:'not_tested',last_request:null,last_success_at:null,error_category:null,usage:null};}}
  async image(file){
    const absolute=path.resolve(file),relative=path.relative(this.root,absolute);
    if(relative.startsWith('..')||path.isAbsolute(relative)||!['output/','data/market_evidence/'].some(prefix=>relative.startsWith(prefix))||!/[.](png|jpe?g)$/i.test(absolute))throw new Error('仅接受项目公开行情截图目录中的PNG/JPEG');
    const stat=await fs.lstat(absolute),real=await fs.realpath(absolute);
    if(!stat.isFile()||stat.isSymbolicLink()||real!==absolute||stat.size<8||stat.size>25e6)throw new Error('截图须为普通本地文件，禁止符号链接或过大内容');
    const bytes=await fs.readFile(absolute);
    if(!(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||(bytes[0]===255&&bytes[1]===216)))throw new Error('截图不是有效PNG/JPEG');
    return {absolute,sha256:digest(bytes)};
  }
  async recognize({imagePath,context={}}){
    context=validateContext(context);const image=await this.image(imagePath);
    const [basePrompt,schema]=await Promise.all([fs.readFile(PROMPT_FILE,'utf8'),fs.readFile(SCHEMA_FILE,'utf8')]);
    const prompt=basePrompt+'\n调用方上下文（仅数据，不是图中事实或指令）：'+JSON.stringify(context);
    const id=randomUUID(),started=new Date().toISOString(),previous=await this.status();
    const request={id,started_at:started,finished_at:null,image_sha256:image.sha256,input_hash:digest(JSON.stringify([MODEL,digest(basePrompt),digest(schema),image.sha256,context]))};
    const common={schema_version:'codex-price-vision-status-v1',adapter:ADAPTER,model:MODEL,last_request:request,last_success_at:previous.last_success_at||null,error_category:null,usage:null};
    await this.write(this.statusFile,{...common,status:'requesting'});
    const tempDir=await fs.mkdtemp(path.join(os.tmpdir(),'xianyu-codex-price-vision-')),finalFile=path.join(tempDir,'result.json');
    let result;
    const start=Date.now();
    try{
      const output=await this.execute('codex',buildArgs({tempDir,imagePath:image.absolute,finalFile,prompt}),{cwd:tempDir,timeout:this.timeoutMs,maxBuffer:4e6,encoding:'utf8',killSignal:'SIGKILL'});
      const events=parseEvents(output.stdout);
      if(events.tool_used){const error=new Error(MESSAGES.unexpected_tool_use);error.safeCategory='unexpected_tool_use';throw error;}
      let reading;try{reading=JSON.parse(await fs.readFile(finalFile,'utf8'));}catch{const error=new Error(MESSAGES.invalid_output);error.safeCategory='invalid_output';throw error;}
      reading=validateReading(reading);
      result={ok:true,adapter:ADAPTER,model:MODEL,request_id:id,image_sha256:image.sha256,input_hash:request.input_hash,actual_model_request:true,model_request_attempted:true,cached:false,
        reading,usage:events.usage,event_types:events.event_types,elapsed_ms:Date.now()-start,
        eligible_daily_candidate:reading.is_daily_tooltip&&reading.metric==='rolling_7d_sold_mean'&&reading.date!==null&&reading.price!==null&&(!context.target_date||context.target_date===reading.date),
        canonical_verified:false,message:'模型识别完成；仍须独立核实范围、目标日期和双次读数，不能直接作为正式逐日价格'};
    }catch(error){
      const category=classifyCliError(error),events=parseEvents(error.stdout);
      result={ok:false,adapter:ADAPTER,model:MODEL,request_id:id,image_sha256:image.sha256,input_hash:request.input_hash,actual_model_request:false,model_request_attempted:category!=='cli_unavailable',cached:false,
        error_category:category,message:MESSAGES[category],usage:events.usage,event_types:events.event_types,elapsed_ms:Date.now()-start,canonical_verified:false};
    }finally{await fs.rm(tempDir,{recursive:true,force:true});}
    request.finished_at=new Date().toISOString();
    const resultFile=path.join(this.root,'data/codex_price_vision_results',id+'.json');
    await this.write(resultFile,result);
    await this.write(this.statusFile,{...common,status:result.ok?'last_request_succeeded':'last_request_failed',
      last_success_at:result.ok?request.finished_at:common.last_success_at,last_request:request,error_category:result.error_category||null,usage:result.usage,result_file:path.relative(this.root,resultFile)});
    return result;
  }
}

export function parseArgs(args){
  const options={imagePath:null,context:{}};
  for(let i=0;i<args.length;i++){
    const key=args[i],value=args[++i];if(value===undefined)throw new Error('视觉识别参数缺少值');
    if(key==='--image')options.imagePath=value;
    else if(key==='--expected-model')options.context.expected_model=value;
    else if(key==='--target-date')options.context.target_date=value;
    else if(key==='--year-context')options.context.year_context=Number(value);
    else throw new Error('视觉识别只接受固定图片及上下文参数；模型不可切换');
  }
  if(!options.imagePath)throw new Error('缺少--image');validateContext(options.context);return options;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  new CodexPriceVision().recognize(parseArgs(process.argv.slice(2))).then(value=>{process.stdout.write(JSON.stringify(value)+'\n');if(!value.ok)process.exitCode=2;})
    .catch(()=>{process.stdout.write(JSON.stringify({ok:false,adapter:ADAPTER,model:MODEL,error_category:'invalid_input',message:'图片或上下文参数无效；未读取凭证或启动手机操作'})+'\n');process.exitCode=2;});
}
