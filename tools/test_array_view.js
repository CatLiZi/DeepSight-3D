const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const file=process.argv[2] || path.join(__dirname,'../src/locator_demo/web/static/array_view.js');
const view=require(file),cases=[];
function test(name,fn){fn();cases.push(name);}
const data=()=>({localization_mode:'four_mic',active_channels:[1,2,3,4],ignored_channels:[],
 ear_left_channel:2,ear_right_channel:3,body_left_channel:1,body_right_channel:4,
 channel_roles:{1:'body_left',2:'ear_left',3:'ear_right',4:'body_right'},
 fusion:{status:'ear_fallback',reason:'head_body_pose_unavailable',body_spectral_used:true},
 pairs:{body:{usable:true,azimuth_deg:30}}});
test('all four configured sensors have active named rows',()=>{
 const d=data();for(let ch=1;ch<=4;ch++){assert(view.channel(d,ch).active);assert(!view.channel(d,ch).label.includes('未参与'));}
 assert.equal(view.channel(d,1).label,'身体左 CH1');assert.equal(view.channel(d,4).label,'身体右 CH4');
});
test('legacy two ear snapshots retain excluded body labels',()=>{
 const d={active_channels:[2,3],ear_left_channel:2,ear_right_channel:3};
 assert(!view.channel(d,1).active);assert(view.channel(d,1).label.includes('未参与'));
 assert.equal(view.channel(d,2).label,'左耳 CH2');
});
test('untransformed body azimuth is explicitly labelled in body coordinates',()=>{
 const s=view.summary(data());assert(s.includes('身体方向 左30.0°（身体坐标）'));
 assert(s.includes('姿态未完成角度换算'));assert(s.includes('身体频段辅助'));
});
test('direction fusion is only advertised when the backend actually used it',()=>{
 const d=data();assert(!view.summary(d).includes('身体角度融合'));
 d.fusion.body_direction_used=true;d.fusion.status='pair_consensus';
 assert(view.summary(d).includes('身体角度融合'));assert(!view.summary(d).includes('姿态未完成角度换算'));
});
test('conflicting pairs never advertise a fictitious common direction',()=>{
 const d=data();d.fusion.status='pair_conflict';assert(view.summary(d).includes('保留双耳方向'));
});
test('quiet bodies do not display a stale angle as a reliable source',()=>{
 const d=data();d.pairs.body.usable=false;assert(!view.summary(d).includes('左30.0°'));
 assert(view.summary(d).includes('待可靠人声'));
});
test('missing metrics and nonfinite angles are safe',()=>{
 assert.equal(view.summary({}), '');assert.equal(view.angle(NaN),'—');assert.equal(view.angle(0),'正前方');
});
test('render helper does not mutate acoustic evidence',()=>{
 const d=data(),before=JSON.stringify(d);view.summary(d);for(let ch=1;ch<=4;ch++)view.channel(d,ch);
 assert.equal(JSON.stringify(d),before);
});
test('actual four-row painter uses the body role and does not grey it out',()=>{
 const source=fs.readFileSync(path.join(path.dirname(file),'app.js'),'utf8');
 const code=source.slice(source.indexOf('function renderAudioObservation('),source.indexOf('function renderCandidateScores('));
 function element(){return {children:[],style:{},append(...c){this.children.push(...c);},replaceChildren(){this.children=[];},textContent:''};}
 const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
 const d=data();d.channel_rms=[.01,.02,.02,.01];
 const context={ArrayView:view,AudioView:{reasonText:r=>r},$:(id)=>get(id),document:{createElement:element}};
 vm.runInNewContext(code+'\nrenderAudioObservation({capture:input,noise_state:"no_voice_activity"},null);',
                    {...context,input:d});
 const rows=get('ear-levels').children;assert.equal(rows.length,4);
 assert.equal(rows[0].children[0].textContent,'身体左 CH1');assert(!rows[0].className.includes('excluded'));
 assert.equal(rows[3].children[0].textContent,'身体右 CH4');
});
test('configuration page exposes four-mic mode and body geometry',()=>{
 const html=fs.readFileSync(path.join(path.dirname(file),'index.html'),'utf8');
 for(const id of ['body-left-channel','body-right-channel','body-spacing-mm','body-map-confirmed','body-yaw-scale'])assert(html.includes(`id="${id}"`));
 assert(html.includes('value="four_mic"'));assert(html.indexOf('/static/array_view.js')<html.indexOf('/static/app.js'));
});
test('view contains no control requests or invented distance claims',()=>{
 const source=fs.readFileSync(file,'utf8');assert(!source.includes('fetch('));assert(!source.includes('/api/head'));
 assert(view.summary(data()).includes('不输出声音距离'));
});
process.stdout.write(JSON.stringify({successful:true,tests:cases.length,cases}));
