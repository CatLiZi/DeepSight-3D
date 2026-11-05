"use strict";
// Execute the actual page helpers with an inert DOM; no device or HTTP calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{value:'',checked:false,textContent:'',addEventListener(){},classList:{remove(){},add(){}}});
  return elements.get(id);
}
const context=vm.createContext({window:{addEventListener(){}},document:{getElementById:element,querySelectorAll(){return [element('center-btn'),element('home-btn')];},addEventListener(){}},
  AudioView:{AudioPresentation:function(){}},setTimeout(){},clearTimeout(){}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const cases=[];
const test=async(name,fn)=>{await fn();cases.push(name);};
(async()=>{
 await test('saving a paused configuration explicitly explains how to resume',async()=>{
   await context.withFeedback('保存控制...','控制配置已保存',async()=>({running:false}));
   assert.match(element('action-feedback').textContent,/已暂停.*开始运行/);
 });
 await test('an unchanged running mode is not falsely reported as paused',async()=>{
   await context.withFeedback('保存开关...','开关已保存',async()=>({running:true}));
   assert.equal(element('action-feedback').textContent,'开关已保存');
 });
 await test('manual commands do not use the configuration pause notice',async()=>{
   await context.withFeedback('点动中...','点动已发送',async()=>({running:false}));
   assert.equal(element('action-feedback').textContent,'点动已发送');
 });
 await test('validation errors show their explanation instead of a raw JSON array',async()=>{
   context.fetch=async()=>({ok:false,status:422,headers:{get(){return null;}},
     json:async()=>({detail:[{msg:'左右轴与俯仰轴不能使用相同的舵机 ID',loc:['body']}]})});
   await assert.rejects(()=>context.api('/test'),e=>e.message==='左右轴与俯仰轴不能使用相同的舵机 ID');
 });
 await test('a failed second audio save cannot leave a success banner',async()=>{
   const requests=[];
   context.fetch=async(url)=>{
     requests.push(url);
     return {ok:requests.length===1,status:requests.length===1?200:500,headers:{get(){return null;}},
       json:async()=>requests.length===1?{audio_config:{swap_channels:false},running:false}:{detail:'配置保存失败'}};
   };
   await assert.rejects(()=>context.saveAudioConfig(),/配置保存失败/);
   assert.equal(requests.length,2);
   assert.match(element('action-feedback').textContent,/失败/);
   assert.doesNotMatch(element('action-feedback').textContent,/已保存/);
 });
 await test('checking listen deselects visual and sends one primary owner',async()=>{
   let payload;
   context.fetch=async(url,options)=>{
     payload=JSON.parse(options.body);
     return {ok:true,json:async()=>({running:false,features:payload})};
   };
   element('audio-enabled-input').checked=true;
   element('visual-enabled-input').checked=true;
   element('camera-enabled-input').checked=true;
   await context.saveFeatureConfig({target:element('audio-enabled-input')});
   assert.equal(payload.primary_mode,'audio_primary');
   assert.equal(payload.visual_enabled,false);
   assert.equal(element('tracking-strategy-select').value,'audio_primary');
 });
 await test('checking visual deselects listen and sends visual primary',async()=>{
   element('visual-enabled-input').checked=true;
   await context.saveFeatureConfig({target:element('visual-enabled-input')});
   assert.equal(element('audio-enabled-input').checked,false);
   assert.equal(element('tracking-strategy-select').value,'visual_priority');
 });
 await test('turning off the active owner produces preview rather than enabling the other',async()=>{
   element('visual-enabled-input').checked=false;
   await context.saveFeatureConfig({target:element('visual-enabled-input')});
   assert.equal(element('audio-enabled-input').checked,false);
   assert.equal(element('visual-enabled-input').checked,false);
 });
 await test('rejected mode change restores server state and enables switches again',async()=>{
   context.fetch=async(url)=>url==='/api/session'
     ? {ok:true,json:async()=>({features:{primary_mode:'visual_priority',audio_enabled:false,visual_enabled:true,camera_enabled:true}})}
     : {ok:false,status:500,headers:{get(){return null;}},json:async()=>({detail:'failed save'})};
   element('audio-enabled-input').checked=true;
   await assert.rejects(()=>context.saveFeatureConfig({target:element('audio-enabled-input')}),/failed save/);
   assert.equal(element('audio-enabled-input').checked,false);
   assert.equal(element('visual-enabled-input').checked,true);
   assert.equal(element('audio-enabled-input').disabled,false);
 });
 await test('centre comes from the server rather than stale browser defaults',async()=>{
   const requests=[];
   context.fetch=async(url,args)=>{
     requests.push({url,body:JSON.parse(args.body)});
     return {ok:true,json:async()=>({target:{yaw:2000,pitch:1550},running:false,status:'submitted',error:null})};
   };
   await context.sendCenter();
   assert.equal(requests[0].url,'/api/head/center');
   assert.deepEqual(requests[0].body,{time_ms:1500});
   assert.equal(element('yaw-input').value,2000);
   assert.equal(element('pitch-input').value,1550);
   assert.match(element('action-feedback').textContent,/目标已提交.*实际位置/);
   assert.doesNotMatch(element('action-feedback').textContent,/已到位/);
 });
 await test('both centre controls are disabled while feedback is pending and duplicate request is coalesced',async()=>{
   let resolve,count=0;
   context.fetch=()=>{count++;return new Promise(done=>{resolve=done;});};
   element('center-btn').disabled=false;element('home-btn').disabled=false;
   const first=context.sendCenter();
   assert.equal(element('center-btn').disabled,true);assert.equal(element('home-btn').disabled,true);
   await context.sendCenter();assert.equal(count,1);
   resolve({ok:true,json:async()=>({target:{yaw:2000,pitch:1550},running:false,error:null})});
   await first;
   assert.equal(element('center-btn').disabled,false);assert.equal(element('home-btn').disabled,false);
 });
 await test('failed centre reports its feedback error and unlocks the controls',async()=>{
   context.fetch=async()=>({ok:true,headers:{get(){return null;}},json:async()=>({error:'No valid position feedback'})});
   await assert.rejects(()=>context.sendCenter(),/No valid position feedback/);
   assert.match(element('action-feedback').textContent,/失败/);
   assert.equal(element('center-btn').disabled,false);
 });
 console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
})().catch(e=>{console.error(e);process.exitCode=1;});
