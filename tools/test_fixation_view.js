const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const view=require(process.argv[2]||'../src/locator_demo/web/static/fixation_view.js');
const cases=[];function test(name,f){f();cases.push(name);}
const data=()=>({target:{face_id:7},fixation:{mode:'binocular',person_id:7,eyes:{
  left:{target_px:[360,180],goal_px:[360,180],goal_visible:true},
  right:{target_px:[280,180],goal_px:[280,180],goal_visible:true}}}});
test('each eye uses its own point, never the shared virtual coordinate',()=>{
  const d=data();assert.deepEqual(view.marks(d,'left').point,[360,180]);assert.deepEqual(view.marks(d,'right').point,[280,180]);
});
test('missing eye and wrong identity cannot create marks',()=>{
  const d=data();d.target.face_id=8;assert.equal(view.marks(d,'left'),null);
  delete d.fixation.eyes.right;d.target.face_id=7;assert.equal(view.marks(d,'right'),null);
});
test('stale frames and invalid points are hidden',()=>{
  const d=data();d.fixation.mode='stale';assert.equal(view.marks(d,'left'),null);
  d.fixation.mode='binocular';d.fixation.eyes.left.target_px=[NaN,0];assert.equal(view.marks(d,'left'),null);
});
test('old runtime without fixation is compatible',()=>{assert.equal(view.marks({target:{face_id:7}},'left'),null);});
test('draws target cross and desired ring at separate observed coordinates',()=>{
  const arcs=[],lines=[];const ctx={save(){},restore(){},beginPath(){},arc(...v){arcs.push(v)},moveTo(){},lineTo(...v){lines.push(v)},stroke(){},setLineDash(){}};
  const d=data();d.fixation.eyes.right.goal_px=[310,180];assert(view.drawSelected(ctx,d,'right'));
  assert(arcs.some(a=>a[0]===280));assert(arcs.some(a=>a[0]===310));assert(!arcs.some(a=>a[0]===320));
});
test('out of view desired point is not clamped into a false goal',()=>{
  const d=data();d.fixation.eyes.right.goal_visible=false;assert.equal(view.marks(d,'right').goalVisible,false);
});
test('single view is labelled as single view',()=>{
  const d=data();d.fixation.mode='single_eye';delete d.fixation.eyes.left;
  assert(view.description(d.fixation).startsWith('暂用右眼'));assert(view.description({mode:'stale'}).includes('过期'));
});
test('main render carries eye identity and geometry into audio refresh cache',()=>{
  const app=fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]||'../src/locator_demo/web/static/fixation_view.js'),'app.js'),'utf8');
  assert(app.includes('fixation:visual.fixation,display_eye:visual.display_eye'));
  assert(!app.includes('t.stereo_position?.virtual_u_px'));
});
process.stdout.write(JSON.stringify({successful:true,tests:cases.length,cases}));
