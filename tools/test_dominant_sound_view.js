"use strict";
const assert=require('node:assert/strict');
const view=require(process.argv[2]);
const cases=[];
const test=(name,fn)=>{fn();cases.push(name);};
const core=Object.freeze({locked:true,relative_azimuth_deg:-30,historical:true,goal_yaw:1350,current_score:0});
function context(){
  const paths=[],texts=[];
  return {paths,texts,save(){},restore(){},setLineDash(){},beginPath(){this.path=[];},
    moveTo(x,y){this.path.push([x,y]);},lineTo(x,y){this.path.push([x,y]);},
    stroke(){paths.push({color:this.strokeStyle,points:this.path});},fillText(s){texts.push(s);}};
}
test('a retained core is drawn even without a current raw direction',()=>{
  const ctx=context();assert(view.drawCoreCompass(ctx,core,420,300));
  assert(ctx.texts.some(s=>s.includes('非实时')));assert(ctx.paths[0].points[1][0]>210);
});
test('both eye projections can draw the same retained core target',()=>{
  for(const cx of [310,330]){
    const ctx=context();assert(view.drawCoreOverlay(ctx,[[100,0,cx],[0,100,180],[0,0,1]],core,640,360));
    assert(ctx.paths[0].points.every(([x])=>x>cx));assert(ctx.texts.some(s=>s.includes('位置估计')));
  }
});
test('raw blue display still expires at one second while the distinct core survives',()=>{
  const p=new view.AudioPresentation();
  p.update({direction:'right',azimuth_deg:-30,confidence:.9,doa_confidence:.9,speech_confidence:.9,
    temporal_stability:.9,azimuth_std_deg:12,age_s:0,motor_suppressed:false,
    raw_observation:{sample_sequence:1,observed_monotonic:10}},null,1000,'a');
  p.update({direction:'unknown'},null,1100,'a');
  assert.equal(p.read(2100).active,false);
  assert(view.drawCoreCompass(context(),core,420,300));
});
test('legacy snapshots and invalid core angles draw no fake target',()=>{
  for(const value of [undefined,{}, {...core,locked:false},{...core,relative_azimuth_deg:NaN},
                     {...core,relative_azimuth_deg:91}]){
    assert.equal(view.drawCoreCompass(context(),value,420,300),false);
    assert.equal(view.drawCoreOverlay(context(),[[100,0,320],[0,100,180],[0,0,1]],value,640,360),false);
  }
});
test('rolled camera projection retains the correct geometry',()=>{
  const ctx=context();assert(view.drawCoreOverlay(ctx,[[100,8,320],[-8,100,180],[0,0,1]],core,640,360));
  assert.notEqual(ctx.paths[0].points[0][0],ctx.paths[0].points[1][0]);
});
test('the display does not mutate stored goals or send control requests',()=>{
  const ctx=context();view.drawCoreCompass(ctx,core,420,300);
  assert.equal(core.goal_yaw,1350);assert.equal(core.current_score,0);
  const fs=require('node:fs'),source=fs.readFileSync(process.argv[2],'utf8');
  assert(!source.includes('fetch('));assert(!source.includes('/api/head'));
});
console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
