// Temp files and mocked execFile only; tests never call Codex, a model or UI.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {CodexPriceVision,MODEL,buildArgs,parseEvents,validateReading,classifyCliError,parseArgs} from '../automation/codex_price_vision.mjs';
const overview={schema_version:'codex-price-vision-v1',screen_kind:'market_overview',model_text:'14900KF',metric:'rolling_7d_sold_mean',
  price:2400,price_text:'¥2400',date:null,date_text:null,is_daily_tooltip:false,confidence:.98,evidence_quotes:['14900KF','近7日成交均价','¥2400'],warnings:['未见逐日浮层，日期轴不能证明当前均价属于某日']};
const png=Buffer.from([137,80,78,71,13,10,26,10,1,2,3,4]);
async function temporary(fn){const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'codex-price-vision-test-')),root=await fs.realpath(tmp);try{
  const image=path.join(root,'data/market_evidence/mobile_4_test.png');await fs.mkdir(path.dirname(image),{recursive:true});await fs.writeFile(image,png);return await fn(root,image);
}finally{await fs.rm(root,{recursive:true,force:true});}}
function fakeExecute(reading=overview,events=[{type:'turn.completed',usage:{input_tokens:42,cached_input_tokens:2,output_tokens:80}}]){
  return async(_binary,args)=>{await fs.writeFile(args[args.indexOf('--output-last-message')+1],JSON.stringify(reading));return {stdout:events.map(e=>JSON.stringify(e)).join('\n'),stderr:''};};
}

test('CLI model and permissions are fixed without auth/provider overrides or tool expansion',()=>{
  const args=buildArgs({tempDir:'/tmp/empty',imagePath:'/public/image.png',finalFile:'/tmp/empty/final.json',prompt:'只读图片'});
  assert.equal(MODEL,'gpt-6.1-sol');assert.equal(args[args.indexOf('--model')+1],MODEL);
  assert.equal(args[args.indexOf('--sandbox')+1],'read-only');assert.ok(args.includes('--ephemeral'));
  assert.ok(args.includes('mcp_servers={}'));
  for(const name of ['shell_tool','apps','plugins','hooks','browser_use','computer_use','multi_agent'])assert.ok(args.some((arg,n)=>arg==='--disable'&&args[n+1]===name));
  assert.equal(args.some(arg=>/dangerously|workspace-write|add-dir|api.key|oauth|access.token/.test(arg)),false);
});

test('construction/status never imply a connection or invoke the existing CLI',async()=>temporary(async(root)=>{
  let calls=0;const adapter=new CodexPriceVision({projectDir:root,execute:async()=>{calls++;}});
  assert.equal((await adapter.status()).status,'not_tested');assert.equal(calls,0);
}));

test('valid overview preserves 2400 but never converts it into a daily reading',async()=>temporary(async(root,image)=>{
  const fake=fakeExecute();const adapter=new CodexPriceVision({projectDir:root,execute:async(binary,args,options)=>{
    assert.equal(options.killSignal,'SIGKILL');assert.equal(options.timeout,120000);
    return fake(binary,args,options);
  }});
  const result=await adapter.recognize({imagePath:image});
  assert.equal(result.ok,true);assert.equal(result.model,MODEL);assert.equal(result.reading.price,2400);
  assert.equal(result.reading.date,null);assert.equal(result.eligible_daily_candidate,false);assert.equal(result.canonical_verified,false);
  assert.deepEqual(result.usage,{input_tokens:42,cached_input_tokens:2,output_tokens:80});
  const state=await adapter.status();assert.equal(state.status,'last_request_succeeded');assert.ok(state.last_success_at);
  assert.equal(state.last_request.image_sha256,result.image_sha256);
  assert.equal((await fs.stat(path.join(root,state.result_file))).mode&0o777,0o600);
}));

test('a daily tooltip is only a candidate for its actually observed target date',async()=>temporary(async(root,image)=>{
  const reading={...overview,screen_kind:'daily_tooltip',date:'2026-07-31',date_text:'07.31',is_daily_tooltip:true,evidence_quotes:['07.31近7日成交均价','¥2400']};
  const adapter=new CodexPriceVision({projectDir:root,execute:fakeExecute(reading)});
  const exact=await adapter.recognize({imagePath:image,context:{target_date:'2026-07-31',year_context:2026}});
  const mismatch=await adapter.recognize({imagePath:image,context:{target_date:'2026-07-30',year_context:2026}});
  assert.equal(exact.eligible_daily_candidate,true);assert.equal(exact.canonical_verified,false);
  assert.equal(mismatch.ok,true);assert.equal(mismatch.eligible_daily_candidate,false);assert.equal(mismatch.reading.date,'2026-07-31');
}));

test('chart-axis date attached to an overview, missing quote and extra fields are rejected',()=>{
  for(const reading of [{...overview,date:'2026-07-31'},{...overview,date_text:'07.31'},{...overview,is_daily_tooltip:true},
    {...overview,price_text:'¥9999'},{...overview,confidence:1.1},{...overview,price:NaN},{...overview,extra:true},
    {...overview,screen_kind:'home'}])assert.throws(()=>validateReading(reading),/JSON/);
});

test('unexpected tool calls reject even an otherwise valid model answer',async()=>temporary(async(root,image)=>{
  const adapter=new CodexPriceVision({projectDir:root,execute:fakeExecute(overview,[{type:'item.completed',item:{type:'command_execution',command:'unsafe'}}])});
  const result=await adapter.recognize({imagePath:image});assert.equal(result.ok,false);assert.equal(result.error_category,'unexpected_tool_use');
  assert.equal(result.actual_model_request,false);assert.equal(result.canonical_verified,false);
}));

test('timeout/auth failures keep only safe categories and retain historical successful request time',async()=>temporary(async(root,image)=>{
  const adapter=new CodexPriceVision({projectDir:root,execute:fakeExecute()});
  await adapter.recognize({imagePath:image});const previous=(await adapter.status()).last_success_at;
  adapter.execute=async()=>{const error=new Error('secret stdout ignored');error.stderr='401 unauthorized Bearer SECRET_TOKEN_NEVER_STORE';throw error;};
  const failed=await adapter.recognize({imagePath:image});assert.equal(failed.error_category,'authentication');assert.equal(failed.actual_model_request,false);
  assert.equal(JSON.stringify(failed).includes('SECRET_TOKEN'),false);
  assert.equal((await adapter.status()).last_success_at,previous);assert.equal((await adapter.status()).status,'last_request_failed');
  adapter.execute=async()=>{const error=new Error('timeout raw log');error.killed=true;throw error;};
  assert.equal((await adapter.recognize({imagePath:image})).error_category,'timeout');
  const files=await fs.readdir(path.join(root,'data/codex_price_vision_results'));
  for(const file of files)assert.equal((await fs.readFile(path.join(root,'data/codex_price_vision_results',file),'utf8')).includes('SECRET_TOKEN'),false);
}));

test('invalid JSON and missing CLI produce failures without claiming completed requests',async()=>temporary(async(root,image)=>{
  const bad=new CodexPriceVision({projectDir:root,execute:async(_binary,args)=>{await fs.writeFile(args[args.indexOf('--output-last-message')+1],'not JSON');return {stdout:'',stderr:''};}});
  assert.equal((await bad.recognize({imagePath:image})).error_category,'invalid_output');
  bad.execute=async()=>{const error=new Error('missing');error.code='ENOENT';throw error;};
  const missing=await bad.recognize({imagePath:image});assert.equal(missing.error_category,'cli_unavailable');assert.equal(missing.model_request_attempted,false);
}));

test('image path, symlink, content and unknown context fields fail before calling Codex',async()=>temporary(async(root,image)=>{
  let calls=0;const adapter=new CodexPriceVision({projectDir:root,execute:async()=>{calls++;}});
  const outside=path.join(root,'outside.png');await fs.writeFile(outside,png);
  await assert.rejects(()=>adapter.recognize({imagePath:outside}));
  const link=path.join(root,'data/market_evidence/link.png');await fs.symlink(image,link);
  await assert.rejects(()=>adapter.recognize({imagePath:link}));
  await assert.rejects(()=>adapter.recognize({imagePath:image,context:{api_key:'not allowed'}}));
  await assert.rejects(()=>adapter.recognize({imagePath:image,context:{target_date:'2026-02-30'}}));
  assert.equal(calls,0);
}));

test('arguments accept only image/context and never an alternative model or key',()=>{
  assert.deepEqual(parseArgs(['--image','image.png','--year-context','2026']),{imagePath:'image.png',context:{year_context:2026}});
  for(const args of [['--image','x','--model','gpt-6-astra'],['--image','x','--api-key','secret'],['--image','x','--timeout','999999'],['--image','x','--year-context','NaN']])assert.throws(()=>parseArgs(args));
});

test('event parsing stores usage/types but never raw text, tokens, IDs or commands',()=>{
  const parsed=parseEvents([JSON.stringify({type:'thread.started',thread_id:'secret-thread'}),JSON.stringify({type:'turn.completed',usage:{input_tokens:5,output_tokens:2,token:'secret'}})].join('\n'));
  assert.deepEqual(parsed.usage,{input_tokens:5,output_tokens:2});assert.equal(JSON.stringify(parsed).includes('secret'),false);
  assert.equal(classifyCliError({stderr:'429 rate limit'}),'rate_limit');assert.equal(classifyCliError({stderr:'network connect error'}),'connection');
});
