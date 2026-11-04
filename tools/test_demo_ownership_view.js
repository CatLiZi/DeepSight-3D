"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},style:{},closest(){return null;}});
  return elements.get(id);
};
const window={};
const context={window,document:{getElementById:element,querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}},performance:{now(){return 1000;}}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
const update=(core,steeringReason='',revision='R38')=>window.DeepSightShell.update({algorithm_revision:revision,running:true,
  features:{audio_enabled:true,visual_enabled:true},audio:{direction:'unknown',core_source:core,steering:{stop_reason:steeringReason}},visual:{},motion:{}});
const cases=[];const test=(name,fn)=>{fn();cases.push(name);};
test('a retained location does not advertise permission to turn',()=>{
  update({locked:true,current_score:0,historical:true,motion_authorized:false});
  assert.match(element('ui-core-source').textContent,/等待新的声音/);
});
test('visual takeover is clearly distinguished from sound loss',()=>{
  update({locked:true,current_score:18,historical:true,motion_authorized:false,motion_reason:'visual_target_has_priority'});
  assert.match(element('ui-core-source').textContent,/视觉目标优先/);
});
test('an authorized finite turn remains described through unknown',()=>{
  update({locked:true,current_score:0,historical:true,motion_authorized:true});
  assert.match(element('ui-core-source').textContent,/本次听声转向有效/);
});
test('no acquired source still requests reliable evidence',()=>{
  update({locked:false});assert.match(element('ui-core-source').textContent,/等待持续/);
});
test('R38 is recognized as loaded rather than awaiting a restart',()=>{
  assert.match(element('ui-runtime-revision').textContent,/R38/);
  assert.match(element('ui-adaptive-motion').textContent,/已加载/);
});
test('staged motion explains that the final target is retained',()=>{
  update({locked:true,motion_authorized:true},'core_step_submitted','R50');
  assert.match(element('ui-acoustic-state').textContent,/分段.*最终位置已保留/);
});
test('missing angle calibration is distinct from unreliable speech',()=>{
  update({locked:false},'yaw_angle_calibration_missing','R50');
  assert.match(element('ui-acoustic-state').textContent,/缺少偏航.*比例/);
});
test('R50 is recognized with its preserved smooth controller',()=>{
  update({locked:false},'','R50');
  assert.match(element('ui-runtime-revision').textContent,/R50/);
  assert.match(element('ui-adaptive-motion').textContent,/已加载/);
});
test('R51 is recognized with its preserved smooth controller',()=>{
  update({locked:false},'','R51');
  assert.match(element('ui-runtime-revision').textContent,/R51/);
  assert.match(element('ui-adaptive-motion').textContent,/已加载/);
});
console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
