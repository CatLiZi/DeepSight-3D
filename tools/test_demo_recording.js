'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const {Controller,CUES,cueAt,telemetry}=require(path.resolve(process.argv[2]));
let tests=0;
const check=(fn)=>{fn();tests++;};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(options={}) {
  const calls=[];let current=0,interval=null,permissionCalls=0,failUpload=!!options.failUpload,uploaded=false;
  const track={readyState:'live',getSettings:()=>({displaySurface:options.surface||'browser'}),stop(){this.readyState='ended';}};
  const stream={getVideoTracks:()=>[track],getTracks:()=>[track]};
  class Recorder {
    static isTypeSupported(){return !options.unsupported;}
    constructor(){this.state='inactive';}
    start(){this.state='recording';}
    stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['synthetic browser video'])});this.onstop?.();}
  }
  const env={Recorder,Blob,media:{async getDisplayMedia(){permissionCalls++;if(options.cancel){const e=new Error('cancel');e.name='NotAllowedError';throw e;}return stream;}},
    fetch:async(url,args={})=>{
      calls.push({url,args});let body;
      if(url.endsWith('/start'))body={id:'a'.repeat(32),status:'recording'};
      else if(url.endsWith('/video')){
        if(failUpload)return {ok:false,status:503,json:async()=>({detail:'upload unavailable'})};
        uploaded=true;body={id:'a'.repeat(32),status:'processing'};
      }else if(url.endsWith('/timeline')||url.endsWith('/stop'))body={status:'awaiting_video'};
      else body={id:'a'.repeat(32),status:failUpload||(options.audioEnded&&!uploaded)?'awaiting_video':'ready',files:{video:'/local-video'}};
      return {ok:true,json:async()=>body};
    },now:()=>current,epoch:()=>100000+current,
    setInterval:fn=>{interval=fn;return 1;},clearInterval:()=>{interval=null;},setTimeout:()=>2,clearTimeout:()=>{},
    createURL:()=> 'blob:synthetic',revokeURL:()=>{},update:()=>{}};
  const controller=new Controller(env);
  return {controller,calls,track,setTime:v=>{current=v;},tick:()=>interval?.(),permissionCalls:()=>permissionCalls,
    allowUpload:()=>{failUpload=false;}};
}
async function main(){
  check(()=>{
    assert.equal(CUES[0].start,0);assert.equal(CUES.at(-1).end,60);
    for(let i=1;i<CUES.length;i++)assert.equal(CUES[i-1].end,CUES[i].start);
    assert.equal(cueAt(9000).speaker,'A');
    assert.equal(cueAt(20000).speaker,'quiet');
    assert.equal(cueAt(22000).speaker,'B');
    assert.equal(cueAt(40000).speaker,'A');
    const speech=CUES.filter(c=>['A','B'].includes(c.speaker));
    assert.deepEqual(speech.map(c=>c.speaker),['A','B','A']);
    assert(speech.every(c=>c.text.split(/\s+/).length>=25));
    assert(speech.every(c=>typeof c.cueId==='string'&&c.cueId.length<=120));
    assert(CUES.filter(c=>['A','B'].includes(c.speaker)).flatMap(c=>c.text.split(/\s+/)).length<110);
  });
  check(()=>{
    const data=telemetry({algorithm_revision:'R51',visual:{frame:'private pixels',targets:[{face_id:4,score:.9,x1:1,y1:2,x2:3,y2:4}],target:{face_id:4}},audio:{azimuth_deg:NaN},pose:{}},123);
    assert.equal(data.target_id,4);assert.equal(data.audio.azimuth_deg,null);
    assert(!JSON.stringify(data).includes('private pixels'));assert.equal(data.candidates.length,1);
  });
  {
    const t=setup({cancel:true});await t.controller.start();
    check(()=>{assert.equal(t.controller.phase,'error');assert.equal(t.calls.length,0);assert.equal(t.permissionCalls(),1);});
  }
  {
    const t=setup({surface:'monitor'});await t.controller.start();
    check(()=>{assert.equal(t.controller.phase,'error');assert.equal(t.calls.length,0);assert.equal(t.track.readyState,'ended');});
  }
  {
    const t=setup({unsupported:true});await assert.rejects(t.controller.start());
    check(()=>{assert.equal(t.permissionCalls(),0);assert.equal(t.calls.length,0);});
  }
  {
    const t=setup();await t.controller.start();t.setTime(1000);
    t.controller.observe({visual:{target:{face_id:10}},audio:{}});t.controller.observe({visual:{target:{face_id:11}},audio:{}});
    t.controller.mark('B');t.controller.stop();t.controller.stop();await flush();await flush();
    check(()=>{
      assert.equal(t.controller.phase,'ready');assert.equal(t.track.readyState,'ended');
      assert.equal(t.calls.filter(c=>c.url.endsWith('/video')).length,1);
      const payload=JSON.parse(t.calls.find(c=>c.url.endsWith('/timeline')).args.body);
      assert.equal(payload.events.filter(e=>e.kind==='state').length,1);
      assert(payload.events.some(e=>e.kind==='observer_label'&&e.speaker==='B'));
      assert(t.calls.every(c=>c.url.startsWith('/api/demo-recordings')));
    });
  }
  {
    const t=setup({failUpload:true});await t.controller.start();t.setTime(1500);t.controller.stop();await flush();await flush();
    check(()=>{assert.equal(t.controller.phase,'save_error');assert(t.controller.blob.size>0);assert.equal(t.controller.localURL,'blob:synthetic');});
    t.allowUpload();await t.controller.submit();
    check(()=>{assert.equal(t.controller.phase,'ready');assert.equal(t.permissionCalls(),1);});
  }
  {
    const t=setup();await t.controller.start();t.setTime(4000);t.tick();
    check(()=>{const planned=t.controller.events.find(e=>e.kind==='planned_cue');assert.equal(planned.cue,'three_turns_v2_A1');assert(planned.cue.length<=120);});
    t.setTime(60001);t.tick();await flush();await flush();
    check(()=>{assert.equal(t.controller.phase,'ready');assert(t.controller.events.some(e=>e.kind==='duration_limit'));});
  }
  {
    const t=setup();await t.controller.start();t.setTime(1200);t.track.readyState='ended';t.track.onended();await flush();await flush();
    check(()=>{assert.equal(t.controller.phase,'ready');assert.equal(t.calls.filter(c=>c.url.endsWith('/stop')).length,1);});
  }
  {
    const t=setup({audioEnded:true});await t.controller.start();t.setTime(1200);t.tick();await flush();await flush();
    check(()=>{assert.equal(t.controller.phase,'ready');assert(t.controller.events.some(e=>e.kind==='audio_ended'));});
  }
  {
    const t=setup();await t.controller.start();t.setTime(1400);t.controller.recorder.state='inactive';t.track.onended();
    check(()=>{assert.equal(t.controller.phase,'stopping');assert.equal(t.controller.blob,null);});
    t.controller.recorder.ondataavailable({data:new Blob(['last queued browser chunk'])});t.controller.recorder.onstop();await flush();await flush();
    check(()=>{assert.equal(t.controller.phase,'ready');assert(t.controller.blob.size>0);});
  }
  process.stdout.write(JSON.stringify({successful:true,tests,hardware_io:false}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
