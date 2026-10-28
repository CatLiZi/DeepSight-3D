"use strict";
// Execute the actual page helpers with an inert DOM; no device or HTTP calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{value:'',checked:false,textContent:'',addEventListener(){},classList:{remove(){},add(){}}});
  return elements.get(id);
}
const context=vm.createContext({window:{addEventListener(){}},document:{getElementById:element,addEventListener(){}},
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
 console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
})().catch(e=>{console.error(e);process.exitCode=1;});
