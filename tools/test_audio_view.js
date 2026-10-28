"use strict";
const assert=require('node:assert/strict');
const {AudioPresentation,projectSector,reasonText,angularWeight,angularSlices,drawSectorOverlay,drawCompass}=require(process.argv[2]);
const audio=(seq=1)=>({direction:'left',azimuth_deg:25,confidence:.9,speech_confidence:.95,temporal_stability:1,age_s:.02,motor_suppressed:false,
  raw_observation:{sample_sequence:seq,observed_monotonic:10+seq*.02}});
const selection={audio_usable:true,sector_half_width_deg:15};
const cases=[];
function test(name,fn){fn();cases.push(name);}
test('fresh audio shows a current sector',()=>{const r=new AudioPresentation().update(audio(),selection,1000,'a');assert(r.active&&!r.historical);assert.equal(r.spread_deg,15);});
test('canonical cache cannot refresh display lifetime',()=>{const p=new AudioPresentation();p.update(audio(),selection,1000,'a');assert(p.update(audio(),selection,1600,'a').historical);assert.equal(p.update(audio(),selection,2400,'a').active,false);});
test('no speech can only show bounded historical memory',()=>{const p=new AudioPresentation();p.update(audio(),selection,1000,'a');assert(p.update({direction:'unknown'},null,1100,'a').historical);assert.equal(p.update({},null,2100,'a').active,false);});
test('a restart never displays the prior session direction',()=>{const p=new AudioPresentation();p.update(audio(),selection,1000,'a');assert.equal(p.update({},null,1200,'b').active,false);});
test('motor suppression cannot create a fresh control sector',()=>{assert.equal(new AudioPresentation().update({...audio(),motor_suppressed:true},selection,1000,'a').active,false);});
test('stale input never initializes a sector',()=>{assert.equal(new AudioPresentation().update({...audio(),age_s:.5},selection,1000,'a').active,false);});
test('invalid scalar evidence never initializes a sector',()=>{for(const value of [NaN,Infinity,-1,2])assert.equal(new AudioPresentation().update({...audio(),confidence:value},selection,1000,'a').active,false);});
test('invalid angles never initialize a sector',()=>{for(const value of [NaN,Infinity,-91,91])assert.equal(new AudioPresentation().update({...audio(),azimuth_deg:value},selection,1000,'a').active,false);});
test('display logic does not mutate source evidence',()=>{const a=Object.freeze(audio());new AudioPresentation().update(a,Object.freeze(selection),1000,'a');assert.equal(a.azimuth_deg,25);});
test('zero bearing projects to centered line',()=>{const r=projectSector([[100,0,320],[0,100,180],[0,0,1]],0,15,360);assert.deepEqual(r.center,[[320,0],[320,360]]);});
test('left-positive bearing is on the left side',()=>{const r=projectSector([[100,0,320],[0,100,180],[0,0,1]],25,15,360);assert(r.center.every(([x])=>x<320));});
test('right-negative bearing is on the right side',()=>{const r=projectSector([[100,0,320],[0,100,180],[0,0,1]],-25,15,360);assert(r.center.every(([x])=>x>320));});
test('roll/skew is reflected in sector edges',()=>{const r=projectSector([[100,8,320],[-8,100,180],[0,0,1]],20,15,360);assert.notEqual(r.center[0][0],r.center[1][0]);});
test('bad projection matrices return no geometry',()=>{for(const p of [null,[],[[NaN,0,1],[0,1,1],[0,0,1]],[[1,0,0],[0,0,0],[0,0,1]]])assert.equal(projectSector(p,20,15,360),null);});
test('no voice and motor guard have distinct explanations',()=>{assert.notEqual(reasonText('no_voice_activity'),reasonText('motor_guard'));});

function context(){
  const fills=[],texts=[];
  return {fills,texts,save(){},restore(){},clearRect(){},fillRect(){},beginPath(){},moveTo(){},lineTo(){},arc(){},closePath(){},stroke(){},setLineDash(){},
    fill(){fills.push(this.fillStyle);},fillText(value){texts.push(value);}};
}

test('reliable acoustic direction displays even when visual selector declines it',()=>{
  const r=new AudioPresentation().update(audio(),{...selection,audio_usable:false},1000,'a');
  assert(r.active);assert.equal(r.selector_accepted,false);
});
test('a face or visual selector is not required for acoustic display',()=>{
  assert(new AudioPresentation().update(audio(),null,1000,'a').active);
});
test('center direction is drawn in the compass',()=>{
  const view=new AudioPresentation().update({...audio(),direction:'center',azimuth_deg:0},null,1000,'a');
  const ctx=context();assert(drawCompass(ctx,view,420,300));
  assert(ctx.fills.some(color=>color.startsWith('rgba(96,165,250,')));
  assert(ctx.texts.some(text=>text.includes('正前方 0°')));
});
test('an energy value alone cannot light a direction',()=>{
  const ctx=context(),view=new AudioPresentation().update({direction:'unknown',energy:.8,capture:{localization_diagnostics:{raw_azimuth_deg:30}}},null,1000,'a');
  assert.equal(view.active,false);assert.equal(drawCompass(ctx,view,420,300),false);
  assert.equal(ctx.fills.some(color=>color.startsWith('rgba(')),false);
});
test('low speech probability cannot create an acoustic sector',()=>{
  assert.equal(new AudioPresentation().update({...audio(),speech_confidence:.48},selection,1000,'a').active,false);
});
test('weak direction confidence or excessive angular uncertainty cannot display',()=>{
  for(const patch of [{doa_confidence:.1},{doa_confidence:NaN},{azimuth_std_deg:26},{azimuth_std_deg:Infinity}])
    assert.equal(new AudioPresentation().update({...audio(),...patch},selection,1000,'a').active,false);
});
test('capture faults clear even a previously displayed historical range',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');
  assert.equal(p.update({...audio(2),capture:{sample_clock_valid:false}},selection,1200,'a').active,false);
});
test('an older clock with a new counter cannot refresh a displayed direction',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');
  const old={...audio(3),raw_observation:{sample_sequence:100,observed_monotonic:9}};
  assert(p.update(old,selection,1600,'a').historical);assert.equal(p.update(old,selection,2400,'a').active,false);
});
test('gradient weights peak at the sound direction and vanish at both boundaries',()=>{
  assert.equal(angularWeight(0,15),1);assert.equal(angularWeight(-15,15),0);assert.equal(angularWeight(15,15),0);
  assert.equal(angularWeight(NaN,15),0);assert.equal(angularWeight(0,0),0);
});
test('gradient strength decreases symmetrically toward both sides',()=>{
  let previous=1;
  for(let n=0;n<=15;n++){const w=angularWeight(n,15);assert(w<=previous+1e-12);assert.equal(w,angularWeight(-n,15));previous=w;}
});
test('angular strips form one bounded continuous range',()=>{
  const slices=angularSlices({active:true,azimuth_deg:-25,spread_deg:15,alpha:.34});assert.equal(slices.length,64);
  assert(Math.abs(slices[0].angle-slices[0].halfWidth+40)<1e-10);
  assert(Math.abs(slices.at(-1).angle+slices.at(-1).halfWidth+10)<1e-10);
  assert(slices.every(s=>s.alpha>0&&s.alpha<=.34));
});
test('bad or fully transparent display views paint no gradient',()=>{
  for(const patch of [{active:false},{alpha:0},{alpha:NaN},{alpha:2},{spread_deg:40},{azimuth_deg:91}])
    assert.deepEqual(angularSlices({active:true,azimuth_deg:0,spread_deg:15,alpha:.34,...patch}),[]);
});
test('camera gradient paints correctly through a rolled projection',()=>{
  const ctx=context();assert(drawSectorOverlay(ctx,[[100,8,320],[-8,100,180],[0,0,1]],{active:true,azimuth_deg:-25,spread_deg:15,alpha:.34},640,360));
  assert.equal(ctx.fills.length,64);assert(new Set(ctx.fills).size>20);
});
test('a missing projection cannot manufacture a camera range',()=>{
  const ctx=context();assert.equal(drawSectorOverlay(ctx,null,{active:true,azimuth_deg:0,spread_deg:15,alpha:.34},640,360),false);assert.equal(ctx.fills.length,0);
});
test('historical direction retains visibility without becoming new selection evidence',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');
  const recent=p.update({},null,1500,'a');assert(recent.active&&recent.historical);assert.equal(recent.selector_accepted,false);
  assert(Math.max(...angularSlices(recent).map(r=>r.alpha))>.33);
});
test('main page uses the same display object in radar and camera overlays',()=>{
  const fs=require('node:fs'),path=require('node:path');const app=fs.readFileSync(path.join(path.dirname(process.argv[2]),'app.js'),'utf8');
  assert(app.includes('drawDirections(displayAudio);'));assert(app.includes('AudioView.drawSectorOverlay('));
  assert(!app.includes('drawDirections(snapshot.audio.direction'));
});

test('explicit voice end keeps the full range for 999ms and clears at 1000ms',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');p.update({direction:'unknown',noise_state:'no_voice_activity'},null,1100,'a');
  assert(p.read(2099).active&&p.read(2099).historical);assert.equal(p.read(2100).active,false);
});
test('historical opacity stays equal to live opacity throughout the hold',()=>{
  const p=new AudioPresentation(),live=p.update(audio(),selection,1000,'a');p.update({direction:'unknown'},null,1100,'a');
  for(const now of [1100,1300,1700,2099])assert.equal(p.read(now).alpha,live.alpha);
});
test('continued silence does not restart the one-second deadline',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');
  for(let t=1100;t<=2000;t+=100)assert(p.update({direction:'unknown'},null,t,'a').active);
  assert.equal(p.update({direction:'unknown'},null,2100,'a').active,false);
  assert.equal(p.update({direction:'unknown'},null,3000,'a').active,false);
});
test('hold starts when the speech stream ends rather than when speech begins',()=>{
  const p=new AudioPresentation();
  for(let n=0;n<100;n++)assert(p.update(audio(n+1),selection,1000+n*100,'a').active);
  p.update({direction:'unknown'},null,11000,'a');assert(p.read(11999).active);assert.equal(p.read(12000).active,false);
});
test('a new sound direction immediately replaces a held range',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');p.update({direction:'unknown'},null,1100,'a');
  const next=p.update({...audio(2),direction:'right',azimuth_deg:-35},selection,1500,'a');
  assert(next.active&&!next.historical);assert.equal(next.azimuth_deg,-35);
});
test('timer-only reads remove a dead-stream range without receiving another message',()=>{
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');
  assert(p.read(1800).active&&p.read(1800).historical);assert.equal(p.read(2330).active,false);
});
test('hold never alters backend source probabilities or selection permissions',()=>{
  const p=new AudioPresentation(),a=Object.freeze(audio());const s=Object.freeze({...selection});
  p.update(a,s,1000,'a');const view=p.update({direction:'unknown'},s,1100,'a');
  assert.equal(view.selector_accepted,false);assert.equal(a.confidence,.9);assert.equal(s.audio_usable,true);
});
test('capture failure and session change clear a held range immediately',()=>{
  for(const mode of ['fault','restart']){
    const p=new AudioPresentation();p.update(audio(),selection,1000,'a');p.update({direction:'unknown'},null,1100,'a');
    assert.equal(p.update(mode==='fault'?{capture:{ok:false}}:{},null,1200,mode==='restart'?'b':'a').active,false);
  }
});
test('the expiry painter clears the compass and every camera overlay together',()=>{
  const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
  const app=fs.readFileSync(path.join(path.dirname(process.argv[2]),'app.js'),'utf8');
  const code=app.slice(app.indexOf('function refreshAudioDisplay('),app.indexOf('function render(snapshot)'));
  const p=new AudioPresentation();p.update(audio(),selection,1000,'a');p.update({direction:'unknown'},null,1100,'a');
  const paints=[];const state={audioOverlayDisplayActive:true,audioOverlayCache:{audio:{},main:{},guidance:{active:true},
    eyes:[{visual:{},projection:[],canvasId:'left',wrapId:'l'},{visual:{},projection:[],canvasId:'right',wrapId:'r'}]}};
  vm.runInNewContext(code+'\nrefreshAudioDisplay(2100);',{state,audioPresentation:p,drawDirections:v=>paints.push(['radar',v.active]),
    drawOverlay:(v,id='main')=>paints.push([id,v.guidance.active]),renderAudioObservation(){},api(){throw Error('No API calls allowed');}});
  assert.deepEqual(paints,[['radar',false],['main',false],['left',false],['right',false]]);
  assert.equal(state.audioOverlayDisplayActive,false);
});
test('expiry timer is installed independently from new WebSocket messages',()=>{
  const fs=require('node:fs'),path=require('node:path');const app=fs.readFileSync(path.join(path.dirname(process.argv[2]),'app.js'),'utf8');
  assert(app.includes('setInterval(()=>refreshAudioDisplay(),100)'));
});
console.log(JSON.stringify({successful:true,tests:cases.length,cases}));
