(function(root, factory) {
  const api=factory();
  if(typeof module==='object' && module.exports) module.exports=api;
  else {root.DeepSightDemoRecording=api; document.addEventListener('DOMContentLoaded',()=>api.install());}
})(typeof globalThis!=='undefined'?globalThis:this, function() {
  'use strict';
  const LIMIT_MS=60000, LIMIT_BYTES=96*1024*1024;
  const CUES=[
  {
    "start": 0,
    "end": 3,
    "speaker": "ready",
    "text": "Ready? A stays still. B moves slowly."
  },
  {
    "start": 3,
    "end": 20,
    "speaker": "A",
    "cueId": "three_turns_v2_A1",
    "text": "Hi! I've been working at my desk all morning, so I'm ready for a short break. I'm thinking about getting some coffee and sitting by the window. Would you like to join me?"
  },
  {
    "start": 20,
    "end": 21,
    "speaker": "quiet",
    "text": "Pause. B speaks next."
  },
  {
    "start": 21,
    "end": 38,
    "speaker": "B",
    "cueId": "three_turns_v2_B1",
    "text": "That sounds nice. I could use a break too. I'll walk over slowly while we talk. The window seats look comfortable, and we can decide what to have for lunch."
  },
  {
    "start": 38,
    "end": 39,
    "speaker": "quiet",
    "text": "Pause. A speaks next."
  },
  {
    "start": 39,
    "end": 54,
    "speaker": "A",
    "cueId": "three_turns_v2_A2",
    "text": "Great, let's take those seats. I'll have a small coffee with milk. We can relax for a few minutes, then head back and finish our work together."
  },
  {
    "start": 54,
    "end": 60,
    "speaker": "quiet",
    "text": "Stay quiet. Keep both faces visible and let the movement finish."
  }
];
  const cueAt=ms=>CUES.find(c=>ms/1000>=c.start && ms/1000<c.end)||CUES[CUES.length-1];
  const number=x=>typeof x==='number' && Number.isFinite(x)?x:null;
  const text=x=>typeof x==='string'?x.slice(0,120):'';
  function telemetry(snapshot,ms) {
    const a=snapshot.audio||{},v=snapshot.visual||{},c=a.core_source||{},ids=snapshot.servo_connection||{};
    const f=snapshot.motion_health?.feedback||{},selection=v.binocular?.target_selection||{};
    return {t_ms:Math.max(0,Math.min(65000,Math.round(ms))),kind:'state',revision:text(snapshot.algorithm_revision),
      running:!!snapshot.running,mode:text(snapshot.mode),target_id:number(v.target?.face_id),
      locked_speaker_id:number(snapshot.locked_speaker_id),
      audio:{direction:text(a.direction),azimuth_deg:number(a.azimuth_deg),speech:number(a.speech_confidence),
        doa:number(a.doa_confidence),confidence:number(a.confidence),reason:text(a.noise_state),
        core_goal_yaw:number(c.goal_yaw),core_relative_deg:number(c.relative_azimuth_deg),core_reason:text(c.reason)},
      pose:{yaw:number(snapshot.pose?.yaw),pitch:number(snapshot.pose?.pitch)},
      feedback:{yaw:number(f[ids.yaw_id]?.position),pitch:number(f[ids.pitch_id]?.position),fault:text(snapshot.motion_health?.fault)},
      candidates:(v.targets||[]).slice(0,10).map(t=>({id:number(t.face_id),score:number(t.score),
        bbox:[t.x1,t.y1,t.x2,t.y2].map(number)})),
      selection:{selected_id:number(selection.selected_person_id),candidate_id:number(selection.candidate_person_id),
        reason:text(selection.switch_reason||selection.reason),mode:text(selection.mode),primary:text(snapshot.features?.primary_mode),ranking:(selection.candidates||[]).slice(0,10).map(c=>({id:number(c.person_id),
          rank:number(c.rank),score:number(c.match_score),in_sector:!!c.in_sector}))},
      motion:{moving:!!snapshot.motion?.moving,error:text(snapshot.motion?.error)}};
  }
  function chooseMime(Recorder) {
    return ['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].find(t=>Recorder?.isTypeSupported?.(t))||'';
  }
  async function request(fetcher,url,options={}) {
    const response=await fetcher(url,options);
    const body=await response.json();
    if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:`录制请求失败（${response.status}）`);
    return body;
  }
  class Controller {
    constructor(env) {
      this.env=env;this.phase='idle';this.message='';this.elapsed=0;this.events=[];
      this.session=null;this.blob=null;this.localURL='';this.timer=null;this.pollTimer=null;
      this.lastState=-Infinity;this.lastCue='';this.stopPromise=null;
    }
    update(){this.env.update?.(this);}
    api(path,options){return request(this.env.fetch,'/api/demo-recordings'+path,options);}
    event(kind,extra={}){if(this.events.length<4000)this.events.push({t_ms:Math.round(Math.min(65000,Math.max(0,this.env.now()-this.started))),kind,...extra});}
    observe(snapshot) {
      if(this.phase!=='recording')return;
      const ms=this.env.now()-this.started;
      if(ms-this.lastState<90)return;
      this.lastState=ms;
      if(this.events.length<4000)this.events.push(telemetry(snapshot,ms));
    }
    mark(speaker) {
      if(this.phase==='recording' && ['A','B','quiet'].includes(speaker))this.event('observer_label',{speaker});
    }
    releaseScreen(){for(const t of this.stream?.getTracks?.()||[])t.stop();}
    async start() {
      if(['choosing','recording','stopping','uploading','processing'].includes(this.phase))return;
      const mime=chooseMime(this.env.Recorder);
      if(!this.env.media?.getDisplayMedia || !mime)throw new Error('请使用本机Chrome或Edge打开控制台；浏览器需支持标签页录制');
      if(this.localURL)this.env.revokeURL?.(this.localURL);
      if(this.pollTimer)this.env.clearTimeout?.(this.pollTimer);
      this.blob=null;this.session=null;this.chunks=[];this.bytes=0;this.events=[];this.elapsed=0;
      this.lastState=-Infinity;this.lastCue='';this.stopPromise=null;this.phase='choosing';this.message='请选择此控制台所在的浏览器标签页';this.update();
      try {
        // Must run directly from the user's click, before awaiting any API call.
        this.stream=await this.env.media.getDisplayMedia({video:{displaySurface:'browser',frameRate:{ideal:20,max:30},
          width:{ideal:1920,max:1920},height:{ideal:1080,max:1080}},audio:false,
          preferCurrentTab:true,selfBrowserSurface:'include',monitorTypeSurfaces:'exclude',surfaceSwitching:'exclude'});
        const track=this.stream.getVideoTracks()[0];
        const surface=track?.getSettings?.().displaySurface;
        if(!track || (surface && surface!=='browser'))throw new Error('请重新选择控制台所在的浏览器标签页');
        this.recorder=new this.env.Recorder(this.stream,{mimeType:mime,videoBitsPerSecond:3000000});
        this.session=await this.api('/start',{method:'POST'});
        if(track.readyState==='ended')throw new Error('共享已结束，请重新录制');
        this.recorder.ondataavailable=e=>{
          if(e.data?.size){this.chunks.push(e.data);this.bytes+=e.data.size;if(this.bytes>LIMIT_BYTES)this.stop('size_limit');}
        };
        this.recorder.onstop=()=>this.finish().catch(e=>this.fail(e));
        this.recorder.onerror=()=>this.stop('browser_error');
        track.onended=()=>this.stop('sharing_ended');
        this.started=this.env.now();this.startedUnix=this.env.epoch();this.phase='recording';
        this.nextAudioCheck=1000;this.checkingAudio=false;
        this.message='正在录制当前标签页；声音来自已运行的四麦RAW采集';
        this.event('start');this.recorder.start(1000);
        this.timer=this.env.setInterval(()=>{
          this.elapsed=this.env.now()-this.started;
          const cue=cueAt(this.elapsed);
          if(cue.start!==this.lastCue){this.lastCue=cue.start;this.event('planned_cue',{speaker:cue.speaker,cue:cue.cueId||cue.text});}
          if(this.elapsed>=this.nextAudioCheck && !this.checkingAudio && this.elapsed<LIMIT_MS){
            this.nextAudioCheck=this.elapsed+1000;this.checkingAudio=true;
            const identity=this.session.id;
            this.api('/'+identity).then(info=>{
              if(this.phase==='recording' && this.session.id===identity && info.status!=='recording'){
                this.session=info;this.stop('audio_ended');this.message=info.error||'音频录制已结束，正在保存画面';this.update();
              }
            }).catch(()=>{}).finally(()=>{this.checkingAudio=false;});
          }
          this.update();if(this.elapsed>=LIMIT_MS)this.stop('duration_limit');
        },100);
        this.update();
      } catch(e) {
        this.releaseScreen();
        if(this.session)await this.api('/'+this.session.id+'/stop',{method:'POST'}).catch(()=>{});
        this.phase='error';this.message=e.name==='NotAllowedError'?'已取消共享，尚未开始录制':e.message;this.update();
      }
    }
    stop(reason='user_stop') {
      if(this.phase!=='recording')return;
      this.ended=this.env.now();this.elapsed=this.ended-this.started;this.event(reason);
      this.phase='stopping';this.message='正在结束录制并保存声音';
      this.env.clearInterval(this.timer);this.timer=null;
      this.stopPromise=this.api('/'+this.session.id+'/stop',{method:'POST'});
      // Register an immediate handler so a network error is not an unhandled rejection.
      this.stopPromise.catch(()=>{});
      if(this.recorder.state!=='inactive')this.recorder.stop();
      // On stream-ended, the browser may already mark it inactive while its
      // final dataavailable/stop events are still queued. Wait for onstop.
      this.update();
    }
    async finish() {
      if(this.finishing)return;this.finishing=true;
      try {
        if(this.phase==='recording'){
          this.ended=this.env.now();this.elapsed=this.ended-this.started;
          this.env.clearInterval(this.timer);this.timer=null;
          this.stopPromise=this.api('/'+this.session.id+'/stop',{method:'POST'});
        }
        this.phase='stopping';this.releaseScreen();
        this.blob=new this.env.Blob(this.chunks,{type:'video/webm'});
        this.localURL=this.env.createURL(this.blob);
        if(!this.blob.size)throw new Error('浏览器没有生成视频；已保留本机四路声音');
        await this.stopPromise;
        await this.submit();
      } finally {this.finishing=false;}
    }
    async submit() {
      if(!this.blob||!this.session)throw new Error('没有可以重试保存的视频');
      if(this.blob.size>LIMIT_BYTES)throw new Error('浏览器视频超过96 MiB；请先下载原画面并缩短下一次录制');
      this.phase='uploading';this.message='正在本机保存网页画面与目标时间线';this.update();
      try {
        await this.api('/'+this.session.id+'/timeline',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({video:{started_unix_ms:this.startedUnix,elapsed_ms:Math.max(100,Math.min(65000,this.elapsed)),
            surface:this.stream.getVideoTracks()[0]?.getSettings?.().displaySurface||'unknown'},events:this.events})});
        this.session=await this.api('/'+this.session.id+'/video',{method:'PUT',headers:{'Content-Type':'video/webm'},body:this.blob});
      } catch(error) {
        const latest=await this.api('/'+this.session.id).catch(()=>null);
        if(!latest||!['processing','ready'].includes(latest.status))throw error;
        this.session=latest;
      }
      this.phase='processing';this.message='正在生成带声音的MP4；可先下载原画面';this.update();
      this.pollAttempts=0;await this.poll();
    }
    async poll() {
      const identity=this.session.id;
      try {
        const session=await this.api('/'+identity);
        if(this.session.id!==identity)return;
        this.session=session;
        if(this.session.status==='ready'){
          this.phase='ready';this.message='录制已保存：MP4、四路RAW声音与目标时间线';this.chunks=[];this.update();return;
        }
        if(['export_error','interrupted'].includes(this.session.status))throw new Error(this.session.export_error||this.session.error||'视频生成中断，原始材料已保留');
        if(++this.pollAttempts>150)throw new Error('生成时间较长，请在稍后的录制列表查看；原画面可先下载');
        this.pollTimer=this.env.setTimeout(()=>this.poll(),1000);
      } catch(e) {this.fail(e);}
    }
    fail(error){this.phase=this.blob?'save_error':'error';this.message=error.message;this.update();}
  }

  function install() {
    const $=id=>document.getElementById(id);
    if(!$('demo-record-start'))return;
    const env={media:navigator.mediaDevices,Recorder:window.MediaRecorder,Blob:window.Blob,
      fetch:window.fetch.bind(window),now:()=>performance.now(),epoch:()=>performance.timeOrigin+performance.now(),
      setInterval:window.setInterval.bind(window),clearInterval:window.clearInterval.bind(window),
      setTimeout:window.setTimeout.bind(window),clearTimeout:window.clearTimeout.bind(window),createURL:b=>URL.createObjectURL(b),revokeURL:u=>URL.revokeObjectURL(u)};
    const controls={start:$('demo-record-start'),stop:$('demo-record-stop'),retry:$('demo-record-retry')};
    let backendAvailable=false,audioReady=false,wasRecording=false;
    function links(container,files,localURL) {
      container.replaceChildren();
      for(const [kind,label] of [['video','下载MP4'],['bundle','下载全部材料ZIP'],['audio','下载四路WAV'],['timeline','下载目标时间线'],['screen','下载原网页画面WebM']]){
        if(!files?.[kind])continue;const a=document.createElement('a');a.href=files[kind];a.download='';a.textContent=label;container.append(a);
      }
      if(localURL&&!files?.screen){const a=document.createElement('a');a.href=localURL;a.download='deepsight-screen.webm';a.textContent='下载原网页画面WebM';container.append(a);}
    }
    env.update=c=>{
      document.body.classList.toggle('demo-recording-active',c.phase==='recording');
      if(c.phase==='recording'&&!wasRecording)$('demo-record-guide')?.removeAttribute('open');
      wasRecording=c.phase==='recording';
      controls.start.disabled=!backendAvailable||!audioReady||['choosing','recording','stopping','uploading','processing'].includes(c.phase);
      controls.stop.disabled=c.phase!=='recording';controls.retry.hidden=c.phase!=='save_error';
      $('demo-record-status').textContent=c.message;
      $('demo-record-timer').textContent=`${Math.min(60,Math.floor(c.elapsed/1000))} / 60 s`;
      const cue=cueAt(c.elapsed);$('demo-record-cue-role').textContent=c.phase==='recording'?cue.speaker==='A'?'A · 固定':cue.speaker==='B'?'B · 缓慢移动':cue.speaker==='ready'?'准备':cue.end<60?'换人停顿':'安静收尾':'对话提示';
      $('demo-record-cue').textContent=c.phase==='recording'?cue.text:'三段长对话 A→B→A；A保持原位，B讲话时缓慢移动，换人停1秒。';
      links($('demo-record-downloads'),c.session?.files,c.localURL);
      const player=$('demo-record-replay');
      if(['choosing','recording','stopping','uploading','processing'].includes(c.phase)){player.pause();player.hidden=true;}
      else if(c.session?.status==='ready' && c.session.files?.video){
        if(player.getAttribute('src')!==c.session.files.video)player.src=c.session.files.video;
        player.hidden=false;
      }
      if(c.session?.error || c.session?.missing_frames_padded || c.session?.overflow_blocks){
        $('demo-record-status').textContent+=`；音频诊断：${c.session.error||''} 补零${c.session.missing_frames_padded||0}帧 / 溢出${c.session.overflow_blocks||0}块`;
      }
    };
    const controller=new Controller(env);
    controls.start.onclick=()=>{$('demo-record-replay').pause();return controller.start().catch(e=>controller.fail(e));};
    controls.stop.onclick=()=>controller.stop();
    controls.retry.onclick=()=>controller.submit().catch(e=>controller.fail(e));
    document.querySelectorAll('[data-demo-speaker]').forEach(b=>b.onclick=()=>controller.mark(b.dataset.demoSpeaker));
    window.addEventListener('deepsight-state',e=>{
      const capture=e.detail?.audio?.capture;
      const ready=!!(capture?.ok && capture?.raw_stream_configured && capture?.channels===4);
      if(ready!==audioReady){audioReady=ready;controller.update();}
      controller.observe(e.detail);
    });
    window.addEventListener('beforeunload',e=>{if(['recording','stopping','uploading'].includes(controller.phase)){e.preventDefault();e.returnValue='';}});
    window.addEventListener('pagehide',()=>{
      if(controller.session && ['recording','stopping'].includes(controller.phase))
        navigator.sendBeacon('/api/demo-recordings/'+controller.session.id+'/stop');
    });
    const rows=$('demo-dialogue-lines');
    for(const cue of CUES.filter(c=>['A','B'].includes(c.speaker))){const p=document.createElement('p');p.textContent=`${cue.start}–${cue.end}s · ${cue.speaker}: ${cue.text}`;rows.append(p);}
    controls.start.disabled=true;
    function showHistory(data){
      const last=data.recordings?.find(r=>r.status!=='recording' && Object.keys(r.files||{}).length>1);
      if(last){
        links($('demo-record-history'),last.files);
        if(last.status==='ready' && last.files?.video && !['recording','choosing'].includes(controller.phase)){
          $('demo-record-replay').src=last.files.video;$('demo-record-replay').hidden=false;
        }
      }
    }
    $('demo-record-refresh').onclick=()=>request(env.fetch,'/api/demo-recordings').then(showHistory).catch(e=>{controller.message=e.message;controller.update();});
    request(env.fetch,'/api/demo-recordings').then(data=>{
      backendAvailable=true;audioReady=!!data.audio_ready;
      controller.message=data.audio_ready?'就绪：选此标签页录制，最多60秒':'请先确认四麦RAW采集正常；录制不会启动跟踪';controller.update();
      showHistory(data);
    }).catch(()=>{$('demo-record-status').textContent='录制接口尚未加载：请正常重启主程序并刷新网页';});
    return controller;
  }
  return {Controller,CUES,cueAt,telemetry,chooseMime,install};
});
