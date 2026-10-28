"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},style:{},closest(){return null;}});return elements.get(id);};
const window={};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{window,document:{getElementById:element,querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}},performance:{now(){return 1000;}}});
const cases=[];const test=(name,fn)=>{fn();cases.push(name);};
test('actual backend COM binding overrides stale frontend text',()=>{
 element('servo-config').textContent='COM3 yaw:0 pitch:2';
 window.DeepSightShell.update({algorithm_revision:'R39',servo_connection:{port:'COM8'}});
 assert.equal(element('ui-motion-port').textContent,'COM8');
});
test('a previous backend without discovery fields remains readable',()=>{
 window.DeepSightShell.update({algorithm_revision:'R38'});assert.equal(element('ui-motion-port').textContent,'COM3');
});
test('a stereo warning is visible without implying recalibration',()=>{
 window.DeepSightShell.update({algorithm_revision:'R39',stereo_preflight:{message:'共同纹理不足，未改写参数'}});
 assert.match(element('stereo-preflight-status').textContent,/未改写参数/);
});
test('feedback faults are separate from source scoring',()=>{
 window.DeepSightShell.update({motion_health:{fault:'Servo 0 no reply'},motion:{moving:false}});
 assert.equal(element('ui-motion-state').textContent,'故障停止');
});
test('R39 recognizes the unchanged adaptive motion controller',()=>{
 window.DeepSightShell.update({algorithm_revision:'R39'});
 assert.match(element('ui-adaptive-motion').textContent,/已加载/);
});
test('R42 is reported from the backend and does not falsely request an adaptive-controller reload',()=>{
 window.DeepSightShell.update({algorithm_revision:'R42'});
 assert.equal(element('ui-runtime-revision').textContent,'R42 算法');
 assert.match(element('ui-adaptive-motion').textContent,/已加载/);
});
console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
