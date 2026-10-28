/* View navigation and read-only presentation. No hardware/configuration APIs. */
(function(root){
  'use strict';
  const byId=id=>document.getElementById(id);
  const finite=value=>typeof value==='number' && Number.isFinite(value);
  const put=(id,value)=>{const element=byId(id);if(element)element.textContent=value;};
  const views={
    live:{title:'感知工作台',label:'实时感知',description:'双目视觉、双耳声源与头部动作，在同一工作区协同。'},
    control:{title:'头部控制',label:'头部控制',description:'独立操作两轴，查看位置反馈与连续运动配置。'},
    devices:{title:'设备与标定',label:'设备与标定',description:'配置采集、通道、跟踪策略与双目几何。'},
    logs:{title:'运行日志',label:'运行日志',description:'查看本次事件，下载完整的运行与串口记录。'}
  };
  let lastReceived=0;
  function showView(name){
    const key=Object.hasOwn(views,name)?name:'live';
    const workspace=document.querySelector('.workspace');if(workspace)workspace.dataset.view=key;
    for(const id of Object.keys(views)){const section=byId('view-'+id);if(section)section.hidden=id!==key;}
    document.querySelectorAll('a[data-view]').forEach(link=>{
      const selected=link.dataset.view===key;link.classList.toggle('active',selected);
      if(selected)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
      link.setAttribute('title',views[link.dataset.view]?.label || '');
    });
    const title=byId('ui-page-title');
    if(title){title.textContent=views[key].title;const dot=document.createElement('span');dot.className='heading-dot';dot.textContent='.';title.append(dot);}
    put('ui-page-description',views[key].description);put('ui-breadcrumb',views[key].label);
    window.scrollTo(0,0);
    return key;
  }
  function freshness(){
    const age=lastReceived ? (performance.now()-lastReceived)/1000 : Infinity;
    const chip=byId('status-chip');if(chip)chip.dataset.fresh=String(age<2.5);
    put('ui-freshness',age<2.5?'实时数据 · 刚刚更新':lastReceived?'数据较早 · 等待更新':'等待实时数据');
  }
  function update(snapshot){
    lastReceived=performance.now();freshness();
    const visual=snapshot.visual || {},capture=visual.capture || {},audio=snapshot.audio || {},input=audio.capture || {};
    const binocular=visual.binocular || {},motion=snapshot.motion || {};
    if(capture.ok && !capture.dual_eye_detection){
      const panel=byId('ui-vision-panel');if(panel)panel.dataset.layout='focus';
      document.querySelectorAll('button[data-layout]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.layout==='focus')));
    }
    put('ui-capture-fps',finite(capture.measured_fps)?capture.measured_fps.toFixed(1):'—');
    put('ui-camera-size',capture.width&&capture.height?`${capture.width} × ${capture.height} · 请求 ${capture.requested_fps ?? '—'} fps`:'等待双目画面');
    put('ui-audio-channels',finite(input.channels)?String(input.channels):'—');
    put('ui-audio-format',finite(input.sample_rate)?`${input.sample_rate/1000} kHz · ${input.sample_bits ?? '—'} bit · ${input.wasapi_raw?'RAW':'共享采集'}`:'等待音频设备');
    const candidates=Array.isArray(binocular.people)?binocular.people.length:Array.isArray(visual.targets)?visual.targets.length:null;
    put('ui-candidate-count',candidates===null?'—':String(candidates));
    put('ui-target-summary',binocular.selected_person_id!=null?`当前目标 #${binocular.selected_person_id} · 视觉观察`:'当前没有可跟随目标');
    put('ui-run-state',snapshot.running?'运行中':'已暂停');
    const features=snapshot.features || {};
    const featureMode=features.audio_enabled && features.visual_enabled?'视觉跟随 + 声音辅助':features.visual_enabled?'纯视觉跟随':features.audio_enabled?'听声搜索':'仅采集预览';
    put('ui-run-description',snapshot.running?featureMode:'自动转动暂停 · 采集继续');
    const runCard=byId('ui-run-state')?.closest('.state-summary');if(runCard)runCard.dataset.running=String(Boolean(snapshot.running));
    const eye=binocular.selected_eye==='right'?'右目':binocular.selected_eye==='left'?'左目':'等待视角';
    put('ui-eye-label',`${eye} · ${finite(capture.inference_fps)?capture.inference_fps.toFixed(1)+' fps':'等待推理'}`);
    const liveDot=document.querySelector('.live-label i');if(liveDot)liveDot.dataset.ready=String(Boolean(capture.ok));
    put('ui-frame-sequence',binocular.sample_sequence!=null?'FRAME '+String(binocular.sample_sequence).padStart(6,'0'):'等待帧');
    const probability=input.speech_vad?.speech_probability;
    put('ui-speech-value',finite(probability)?(Math.max(0,Math.min(1,probability))*100).toFixed(0)+'%':'—');
    const meter=byId('ui-speech-bar');if(meter)meter.style.width=finite(probability)?`${Math.max(0,Math.min(1,probability))*100}%`:'0%';
    const messages={no_voice_activity:'采集中 · 等待人声',speech_like:'已获得声源方向',ear_pair:'已获得声源方向',
      direction_transition:'正在确认新的方向',ambiguous_delay:'已收到声音 · 方向存在歧义',low_coherence:'两耳信号一致性不足',
      low_snr:'声音较弱 · 等待可靠方向',motor_guard:'旧版转动保护',motion_acoustic_uncertain:'已收到声音 · 当前方向证据不足',quiet:'环境安静 · 等待声音'};
    const reason=audio.noise_state;
    const motionVoice=audio.motion_context?.active && audio.motion_context?.admitted;
    const steeringReason=audio.steering?.stop_reason;
    const core=audio.core_source;
    const coreMessages={collecting_core_direction:'正在累计核心方向证据',finishing_core_motion:'核心目标已锁定 · 完成当前转向',core_goal_submitted:'正在转向已确认的核心目标',core_goal_reached:'核心目标位置已到达 · 等待更强方向',awaiting_current_core_evidence:'保留声音位置 · 等待新的可靠声音再转向'};
    const steeringNote=steeringReason==='yaw_limit'?'已到左右行程边界':steeringReason==='step_budget_reached'?'本轮听声搜索已达步数上限':coreMessages[steeringReason] || null;
    put('ui-acoustic-state',input.ok===false?'采集暂不可用 · 请查看输入诊断':steeringNote || (motionVoice?'转动中 · 可靠声音方向继续可用':messages[reason] || '正在检查声音方向'));
    const ownership=core?.motion_authorized?'本次听声转向有效':core?.motion_reason==='visual_target_has_priority'?'视觉目标优先':'位置记忆保留，等待新的声音';
    put('ui-core-source',core?.locked?`核心目标已锁定 · ${finite(core.current_score)?core.current_score.toFixed(1):'—'} 分 · ${core.historical?'实时方向暂缺，保留目标位置':'持续接收核心方向'} · ${ownership}`:'核心方向：等待持续、可靠的语音证据');
    const ranked=Array.isArray(core?.candidates)?core.candidates:[];
    put('ui-core-scores',ranked.length?ranked.slice(0,3).map(row=>`${row.selected?'● ':''}${finite(row.relative_azimuth_deg)?(row.relative_azimuth_deg>=0?'左':'右')+Math.abs(row.relative_azimuth_deg).toFixed(0)+'°估计':'方向候选'} ${finite(row.score)?row.score.toFixed(1):'—'}分 / ${finite(row.voiced_s)?row.voiced_s.toFixed(2):'—'}s`).join('；'):'分值综合有效讲话时长、方向质量和近期程度，幅度大不等于优先级高。');
    const acoustic=byId('ui-acoustic-state');if(acoustic)acoustic.dataset.quality=audio.direction && audio.direction!=='unknown'?'voice':'waiting';
    const sector=document.querySelector('.mini-badge');const half=visual.processing?.audio_sector_half_width_deg;
    if(sector)sector.textContent=`±${finite(half)?half:15}° 范围可调`;
    const ears=document.querySelector('.channels-heading small');
    if(ears)ears.textContent=input.localization_mode==='ear_pair'?`CH${input.ear_left_channel ?? '—'} / CH${input.ear_right_channel ?? '—'} · 两耳`:'四路保留采集';
    const servo=byId('servo-config')?.textContent || '';
    put('ui-motion-port',snapshot.servo_connection?.port || /COM\d+/i.exec(servo)?.[0] || '等待端口信息');
    put('ui-motion-profile',motion.enabled?`${motion.sample_hz ?? '—'} Hz 连续轨迹`:'定时单条指令');
    const fault=snapshot.motion_health?.fault || motion.error;
    put('stereo-preflight-status',snapshot.stereo_preflight?.message || '等待开始前双目检查');
    put('ui-motion-state',fault?'故障停止':motion.moving?'正在转动':'静止');
    const approach=visual.approach || {},velocity=approach.image_velocity_norm_s;
    put('ui-runtime-revision',snapshot.algorithm_revision ? snapshot.algorithm_revision+' 算法':'原算法 · 待重启加载');
    const speed=Array.isArray(velocity)?Math.hypot(velocity[0]*(visual.frame_width || 0)/2,velocity[1]*(visual.frame_height || 0)/2):null;
    put('ui-adaptive-motion',approach.controller==='adaptive_image_velocity'
      ? `图像移动约 ${finite(speed)?Math.round(speed):'—'} px/s · 请求动作 ${approach.move_time_ms} ms · 左右步幅上限 ${approach.yaw_step_cap} 刻度`
      : ['R32','R33','R34','R35','R36','R37','R38','R39','R40','R41','R42','R43'].includes(snapshot.algorithm_revision)?'动态追踪已加载 · 等待新目标画面':'动态追踪待正常重启加载');
    const health=byId('motion-health');if(health)health.dataset.fault=String(Boolean(fault));
    const ready=Boolean(capture.ok && input.ok);const deviceDot=byId('ui-device-dot');if(deviceDot)deviceDot.dataset.ready=String(ready);
    put('ui-device-summary',ready?'双目与音频已连接':capture.ok?'视觉在线 · 等待音频':input.ok?'音频在线 · 等待视觉':'等待采集设备');
  }
  document.addEventListener('DOMContentLoaded',()=>{
    showView(location.hash.slice(1));
    document.querySelectorAll('a[data-view]').forEach(link=>link.addEventListener('click',event=>{
      if(event.metaKey||event.ctrlKey||event.shiftKey)return;
      event.preventDefault();const view=showView(link.dataset.view);history.replaceState(null,'','#'+view);
    }));
    document.querySelectorAll('[data-go-view]').forEach(button=>button.addEventListener('click',()=>{
      const view=showView(button.dataset.goView);history.replaceState(null,'','#'+view);
      if(button.dataset.focusTarget)byId(button.dataset.focusTarget)?.scrollIntoView({behavior:'smooth',block:'start'});
    }));
    document.querySelectorAll('button[data-layout]').forEach(button=>button.addEventListener('click',()=>{
      const panel=byId('ui-vision-panel');if(panel)panel.dataset.layout=button.dataset.layout;
      document.querySelectorAll('button[data-layout]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    }));
    window.addEventListener('hashchange',()=>showView(location.hash.slice(1)));
    setInterval(freshness,500);
  });
  root.DeepSightShell={update};
})(window);
