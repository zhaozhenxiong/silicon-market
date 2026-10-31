import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {MobileSnapshotCollector,screenshotSweepLayout} from '../automation/mobile_capture_worker.mjs';

const line=(text,x,y,width=.08,height=.02)=>({text,x,y,width,height});
const screen=()=>({width:708,height:1562,filename:'mobile_setup.png',bytes:Buffer.from('fixture-only'),lines:[
  line('14900',.32,.21),line('近7日成交均价',.05,.35),line('04.30',.01,.55),line('08.01',.45,.55),line('11.01',.9,.55)]});
const catalog={id:1,version:1,model:'i9-14900',market_scope_id:'fixed',query:'i9-14900',scope:{verified:false}};

test('sweep coordinates preserve unknown dates and never derive values from the line',()=>{
  const plan=screenshotSweepLayout(screen(),catalog,3);
  assert.ok(plan.points.length>200&&plan.points.length<400);
  assert.equal(plan.points.at(-1),plan.right);
  assert.equal(plan.date_mapping,'unverified_coordinates_only');
  assert.equal(plan.coverage_verified,false);
  assert.ok(!('dates' in plan)&&!('price' in plan));
});

test('a query string alone, missing axis, or wrong main SKU cannot authorize a sweep',()=>{
  for(const change of ['wrong_model','query_only','no_axis','duplicate_anchor']){
    const shot=screen();
    if(change==='wrong_model')shot.lines[0].text='14900KF';
    if(change==='query_only')shot.lines[0].y=.13;
    if(change==='no_axis')shot.lines=shot.lines.slice(0,3);
    if(change==='duplicate_anchor')shot.lines.push(line('近7日成交均价',.4,.36));
    assert.throws(()=>screenshotSweepLayout(shot,catalog));
  }
});

async function fixture(){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'xianyu-capture-test-')),calls=[],state={allowed:true,shot:screen()};
  const worker={catalog:null,root,app:{click:async point=>calls.push({click:point})},
    api:async(url,payload)=>{
      calls.push({url,payload});
      if(url==='/api/catalog')return {items:[structuredClone(catalog)]};
      if(url==='/api/workflow/status')return {mobile:{operator_paused:false}};
      if(url.endsWith('/control'))return {allowed:state.allowed};
      if(url==='/api/mobile/capture-batches')return {id:1};
      return {ok:true};
    },
    assertTargetAllowed:async()=>({catalog_version:1,market_scope_id:'fixed',task_ids:[6]}),
    inspectScreen:async()=>state.shot,
    saveShot:async()=>({...screen(),filename:'mobile_tooltip.png'}),dimensions:()=>({width:708,height:1562}),heartbeat:async()=>{}};
  return {root,calls,state,collector:new MobileSnapshotCollector({projectDir:root,worker})};
}

test('capture-first saves source frames with null date and no price or reading submissions',async()=>{
  const {root,calls,collector}=await fixture();
  try{
    await collector.begin(1,{stepPx:3});
    const result=await collector.runChunk({maxFrames:2,maxDurationMs:1000});
    assert.equal(result.captured,2);assert.equal(result.verified_dates,0);
    const registered=calls.filter(c=>c.url==='/api/mobile/captures').flatMap(c=>c.payload.frames);
    assert.equal(registered.length,3);
    assert.ok(registered.every(frame=>frame.screen_date===null&&!('price' in frame)));
    assert.ok(calls.every(c=>c.url!=='/api/mobile/readings'));
    assert.ok(calls.filter(c=>c.click).every(c=>c.click[1]===collector.session.layout.y));
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('revoked monitor or changed visible model pauses before any new chart input',async()=>{
  for(const change of ['revoked','changed_screen']){
    const {root,calls,state,collector}=await fixture();
    try{
      await collector.begin(1);
      if(change==='revoked')state.allowed=false;
      else state.shot.lines[0].text='14900KF';
      const result=await collector.runChunk({maxFrames:10,maxDurationMs:1000});
      assert.equal(result.status,'paused');assert.equal(calls.filter(c=>c.click).length,0);
    }finally{await fs.rm(root,{recursive:true,force:true});}
  }
});

test('resampling only changes remaining coordinates and preserves captured evidence',async()=>{
  const {root,collector}=await fixture();
  try{
    await collector.begin(1,{stepPx:2});await collector.runChunk({maxFrames:2,maxDurationMs:1000});
    const done=collector.session.layout.points.slice(0,2),frames=collector.session.frames.length;
    await collector.setStepPx(3);
    assert.deepEqual(collector.session.layout.points.slice(0,2),done);
    assert.equal(collector.session.frames.length,frames);
  }finally{await fs.rm(root,{recursive:true,force:true});}
});
