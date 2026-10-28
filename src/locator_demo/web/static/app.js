const state = {
  connected: false,
  eventsSeen: new Set(),
  lastFrame: "",
  dshowDevices: [],
  axisLimits: {
    yaw_min: 800,
    yaw_center: 1500,
    yaw_max: 2200,
    pitch_min: 1200,
    pitch_center: 1500,
    pitch_max: 1800,
  },
};

const $ = (id) => document.getElementById(id);
const audioPresentation = new AudioView.AudioPresentation(1.0);

function optionalNumber(id) {
  const value = $(id).value.trim();
  return value === "" ? null : Number(value);
}

let feedbackTimer = null;

function api(path, options = {}) {
  return fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.error) {
      let message = body.error || body.detail || `请求失败（${response.status}）`;
      if (Array.isArray(message)) message = message.map(item => item.msg || String(item)).join("；");
      if (typeof message !== "string") message = JSON.stringify(message);
      message = message.replace(/Servo (\d+) has no (?:valid )?position feedback; motion remains stopped/g,
        "舵机 ID $1 未返回有效位置，已停止动作；请点击检查通信");
      message = message.replace(/Servo (\d+) mode is (\w+); expected 1; automatic mode rewriting is disabled/g,
        "舵机 ID $1 的工作模式为 $2，当前需要模式1；已停止，程序不会自动改写模式")
        .replace(/Servo (\d+) moved unexpectedly during activation \((\d+) to (\d+)\); motion remains stopped/g,
        "舵机 ID $1 启用时位置异常变化（$2 → $3），已停止")
        .replace(/Servo (\d+) is outside configured limits; calibrate before motion/g,
        "舵机 ID $1 的实测位置超出当前范围，已停止；请检查安装位置与标定");
      const identity = response.headers.get("X-Request-ID");
      const error = new Error(message + (identity ? `（记录 ${identity}）` : ""));
      error.reported = true;
      throw error;
    }
    return body;
  });
}

function setFeedback(message, kind = "ok") {
  const chip = $("action-feedback");
  if (!chip) return;
  chip.textContent = message;
  chip.classList.remove("is-busy", "is-ok", "is-error");
  chip.classList.add(kind === "busy" ? "is-busy" : kind === "error" ? "is-error" : "is-ok");
  clearTimeout(feedbackTimer);
  if (kind === "ok") {
    feedbackTimer = setTimeout(() => {
      chip.textContent = "就绪";
      chip.classList.remove("is-busy", "is-ok", "is-error");
    }, 2600);
  }
}

function reportClientError(message) {
  fetch("/api/diagnostics/client-error", {method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({message:String(message).slice(0,1000), page:location.pathname})}).catch(()=>{});
}
window.addEventListener("unhandledrejection", event => {
  if (event.reason?.reported) {event.preventDefault(); return;}
  reportClientError(event.reason?.message || event.reason);
});
window.addEventListener("error", event => reportClientError(event.message));

async function withFeedback(busyText, doneText, action) {
  setFeedback(busyText, "busy");
  try {
    const result = await action();
    const savedWhilePaused = doneText.includes("已保存") && result?.running === false;
    setFeedback(savedWhilePaused ? `${doneText}；自动转动已暂停，准备好后点击“开始运行”` : doneText, "ok");
    return result;
  } catch (error) {
    setFeedback(`失败: ${error.message}`, "error");
    throw error;
  }
}

function drawDirections(display) {
  const canvas=$("direction-canvas");
  AudioView.drawCompass(canvas.getContext("2d"),display,canvas.width,canvas.height);
  AudioView.drawCoreCompass(canvas.getContext("2d"),state.coreSource,canvas.width,canvas.height);
}

function drawOverlay(visual, canvasId = "overlay", wrapId = "video-wrap") {
  const canvas = $(canvasId);
  const frameWidth = visual.frame_width || 640;
  const frameHeight = visual.frame_height || 400;
  if (canvas.width !== frameWidth || canvas.height !== frameHeight) {
    canvas.width = frameWidth;
    canvas.height = frameHeight;
    $(wrapId).style.aspectRatio = `${frameWidth} / ${frameHeight}`;
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const guide=visual.guidance;
  const project=guide?.projection;
  if(project)AudioView.drawCoreOverlay(ctx,project,guide?.core,canvas.width,canvas.height);
  if(guide?.active && project && AudioView.drawSectorOverlay(ctx,project,guide,canvas.width,canvas.height)){
    ctx.fillStyle='#93c5fd';ctx.font=`${Math.max(16,Math.round(canvas.width/65))}px Segoe UI`;
    const name=Math.abs(guide.azimuth_deg)<.5 ? '正前方' : `${guide.azimuth_deg>0 ? '左' : '右'}${Math.abs(guide.azimuth_deg).toFixed(0)}°`;
    ctx.fillText(`${guide.label}：${name} ±${guide.spread_deg}° · 中心较强，边缘渐隐`,12,Math.max(28,canvas.height-58));
  }
  ctx.strokeStyle = "rgba(148,163,184,0.35)";
  ctx.setLineDash([4, 8]);
  ctx.strokeRect(canvas.width * 0.42, canvas.height * 0.38, canvas.width * 0.16, canvas.height * 0.24);
  ctx.setLineDash([]);
  const sx = canvas.width / frameWidth;
  const sy = canvas.height / frameHeight;
  const eye = visual.display_eye || visual.target?.source_eye;
  FixationView.drawReference(ctx,canvas.width,canvas.height);
  if (visual.body_target) {
    const b = visual.body_target;
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(b.x1 * sx, b.y1 * sy, (b.x2 - b.x1) * sx, (b.y2 - b.y1) * sy);
    ctx.setLineDash([]);
    ctx.fillStyle = "#38bdf8";
    ctx.fillText(`body ${(b.score || 0).toFixed(2)}`, b.x1 * sx + 4, b.y1 * sy + 16);
  }
  const drawFaceBox = (t, selected) => {
    const boxColor = selected ? "#facc15" : "#38bdf8";
    const borderColor = selected ? "rgba(250,204,21,0.95)" : "rgba(56,189,248,0.8)";
    const controlX = Number.isFinite(t.tracking_x) ? t.tracking_x : (t.x1 + t.x2) / 2;
    const controlY = Number.isFinite(t.tracking_y) ? t.tracking_y : (t.y1 + t.y2) / 2;
    const cx = controlX * sx;
    const cy = controlY * sy;
    ctx.strokeStyle = boxColor;
    ctx.lineWidth = selected ? 4 : 2;
    ctx.strokeRect(t.x1 * sx, t.y1 * sy, (t.x2 - t.x1) * sx, (t.y2 - t.y1) * sy);
    ctx.fillStyle = boxColor;
    ctx.font = `${Math.max(16, Math.round(canvas.width / 40))}px Segoe UI`;
    const tag = selected ? "目标" : "人脸";
    const angle=t.bearing?.azimuth_left_deg;
    const angleText=Number.isFinite(angle) ? (Math.abs(angle)<.05 ? " · 0°" : ` · ${angle>0 ? "左" : "右"}${Math.abs(angle).toFixed(1)}°`) : "";
    ctx.fillText(`${tag} #${t.face_id}${angleText}`, Math.max(4,Math.min(canvas.width-300,t.x1*sx+4)), Math.max(24,t.y1*sy+18));
    const match=guide?.candidates?.find(row=>row.person_id===t.face_id);
    if(Number.isFinite(match?.match_score)){
      ctx.font=`${Math.max(14,Math.round(canvas.width/65))}px Segoe UI`;
      ctx.fillText(`#${match.rank} 匹配 ${(match.match_score*100).toFixed(0)}%`,
        Math.max(4,Math.min(canvas.width-220,t.x1*sx+4)),Math.min(canvas.height-50,t.y2*sy+28));
    }
    if(selected && FixationView.drawSelected(ctx,visual,eye))return;
    if(selected && visual.fixation?.mode!=='stale')FixationView.cross(ctx,cx,cy,16,boxColor);
    else if(visual.fixation?.mode!=='stale'){
      ctx.fillStyle=boxColor;ctx.beginPath();ctx.arc(cx,cy,5,0,Math.PI*2);ctx.fill();
    }
  };
  const targets = visual.targets || [];
  const selectedTarget = visual.target ? targets.find(t => t.face_id === visual.target.face_id) : null;
  if (targets.length > 0) {
    for (const target of targets) drawFaceBox(target, target === selectedTarget);
  } else if (visual.target) {
    drawFaceBox(visual.target, true);
  }
  if(visual.direction_view?.zero_reference==="rectified_image_center" && project){
    const y=canvas.height-8;
    ctx.font=`${Math.max(18,Math.round(canvas.width/65))}px Segoe UI`;
    ctx.fillStyle="rgba(15,23,42,.65)";ctx.fillRect(0,canvas.height-45,canvas.width,45);
    ctx.fillStyle="#fff";ctx.textAlign="center";
    for(let angle=-80;angle<=80;angle+=10){
      const x=-Math.tan(angle*Math.PI/180),z=project[2][0]*x+project[2][2];
      const u=z>0 ? (project[0][0]*x+project[0][2])/z : -1;
      if(u<25 || u>canvas.width-25)continue;
      ctx.fillText(angle===0 ? "0°" : `${angle>0 ? "左" : "右"}${Math.abs(angle)}°`,u,y);
    }
    ctx.textAlign="start";
  }
}

function renderAudioObservation(audio, display) {
  const capture=audio.capture;
  const container=$("ear-levels");container.replaceChildren();
  if(capture?.channel_rms){
    for(let ch=1;ch<=4;ch++){
      const active=[capture.ear_left_channel,capture.ear_right_channel].includes(ch);
      const row=document.createElement('div');row.className='ear-level'+(active ? '' : ' excluded');
      const label=document.createElement('span');label.textContent=ch===capture.ear_left_channel ? `左耳 CH${ch}` : ch===capture.ear_right_channel ? `右耳 CH${ch}` : `CH${ch} 未参与`;
      const meter=document.createElement('div');meter.className='ear-meter';
      const bar=document.createElement('i');const level=capture.channel_rms[ch-1];
      const db=Number.isFinite(level) && level>0 ? 20*Math.log10(level) : -120;
      bar.style.width=`${Math.max(0,Math.min(100,(db+70)*2))}%`;meter.append(bar);
      const value=document.createElement('span');value.textContent=`${db.toFixed(0)} dBFS`;
      row.append(label,meter,value);container.append(row);
    }
  }
  const probability=capture?.speech_vad?.speech_probability;
  const speech=Number.isFinite(probability) ? `人声${probability>=.5 ? '已检出' : '尚未通过'} ${(probability*100).toFixed(0)}%` : '人声检测信息暂不可用';
  const raw=audio.raw_observation || audio;
  const reason=AudioView.reasonText(capture?.localization_message || raw.noise_state);
  const guard=audio.motor_suppressed ? '转头噪声保护中，声音不参与控制' : '输入电平只表示收到声音，不代表距离';
  const last=display?.active ? `；${display.historical ? '最近' : '当前'}声源 ${display.azimuth_deg>=0 ? '左' : '右'}${Math.abs(display.azimuth_deg).toFixed(1)}° ±${display.spread_deg}°${display.historical ? '（声音结束后保留1秒，仅显示，不计为新证据）' : ''}` : '';
  const displayNote=display?.active && !display.historical && !display.selector_accepted
    ? '；声学方向可显示，视觉选人尚未采用该声音'
    : !display?.active ? '；尚无可靠方向，暂不绘制范围' : '';
  $("audio-observation-status").textContent=`${speech}；${reason}；${guard}${last}${displayNote}`;
}

function renderCandidateScores(selection) {
  const body=$("face-score-rows");body.replaceChildren();
  const rows=(selection?.candidates || []).filter(row=>Number.isFinite(row.match_score));
  if(!selection?.score_kind){
    $("face-score-note").textContent='新版评分接口尚未加载；程序正常重启后显示候选排序。';return;
  }
  const audioMode=rows.some(row=>row.score_mode==='audio_visual');
  $("face-score-note").textContent=audioMode
    ? '匹配度综合方向52%、相对人脸大小30%、视觉质量13%、保持当前目标5%。范围外不由声音选入；切换仍需稳定确认。大小不是声音测距，分数不是身份概率。'
    : '当前没有可靠的声视匹配，显示视觉大小与质量排序并保持稳定跟随。相对大小不能直接换算成米数。';
  for(const row of rows){
    const tr=document.createElement('tr');if(row.person_id===selection.selected_person_id)tr.className='selected';
    const values=[`#${row.rank} · 人脸${row.person_id}`,`${(row.match_score*100).toFixed(0)}%`,
      audioMode ? `${(row.angular_score*100).toFixed(0)}%` : '—',`${(row.relative_size_score*100).toFixed(0)}%`,
      row.person_id===selection.selected_person_id ? '跟踪中' : audioMode && !row.in_sector ? '范围外' : row.person_id===selection.candidate_person_id ? '候选/确认中' : '可见候选'];
    for(const text of values){const cell=document.createElement('td');cell.textContent=text;tr.append(cell);}body.append(tr);
  }
}

function refreshAudioDisplay(nowMs=performance.now()) {
  const cache=state.audioOverlayCache;
  if(!cache)return;
  const displayAudio=audioPresentation.read(nowMs);
  if(displayAudio.active && !displayAudio.historical)return;
  if(!displayAudio.active && !state.audioOverlayDisplayActive)return;
  const guidance={...cache.guidance,...displayAudio};
  drawDirections(displayAudio);
  if(cache.main)drawOverlay({...cache.main,guidance});
  for(const eye of cache.eyes)
    drawOverlay({...eye.visual,guidance:{...guidance,projection:eye.projection}},eye.canvasId,eye.wrapId);
  renderAudioObservation(cache.audio,displayAudio);
  state.audioOverlayDisplayActive=displayAudio.active;
}

function render(snapshot) {
  state.connected = true;
  $("status-chip").textContent = "已连接";
  $("running-label").textContent = String(snapshot.running);
  $("mode-label").textContent = snapshot.mode;
  const health = snapshot.motion_health;
  const positions = health?.feedback || {};
  const axisText = Object.entries(positions).map(([id, row]) => {
    const age = Math.max(0, Date.now()/1000-row.ts);
    return `ID ${id}：${row.valid ? row.position : "无有效反馈"}${age>3 ? "（较早读数）" : ""}`;
  }).join("；");
  $("motion-health").textContent = health?.fault ? `已停止：${health.fault}；${axisText}`
    : `${snapshot.running ? "自动控制已启动" : "自动转动已暂停；勾选视觉跟踪后还需点击开始"}。${axisText}`;
  const control = snapshot.visual.control;
  const reasonText = {tracking:"持续跟随",centered:"回中保持",inside_enter_band:"在回中缓冲区内",confirming_direction:"等待连续画面确认",waiting_observation:"等待新画面"};
  $("visual-trigger-status").textContent = control?.reason === "image_feedback"
    ? `左右：${reasonText[control.yaw?.reason] || "等待"}；上下：${!control.pitch_enabled ? "已关闭" : reasonText[control.pitch?.reason] || "等待"}${control.pitch_limit ? "（已到俯仰"+(control.pitch_limit==="upper" ? "上限" : "下限")+"）" : ""}。上下偏差占画面高度 ${((control.raw_error?.[1] || 0)*50).toFixed(1)}%。`
    : snapshot.running ? (snapshot.visual.visible ? "已看到人脸，等待新的有效检测结果" : "尚无有效人脸，等待脸部进入两眼画面") : "上下箭头可单独点动；自动跟踪需启动后并检测到人脸。";
  applyFeatureConfig(snapshot.features);
  $("audio-direction").textContent = snapshot.audio.direction;
  $("audio-confidence").textContent = `置信度 ${(snapshot.audio.confidence * 100).toFixed(0)}%`;
  $("audio-tdoa").textContent = snapshot.audio.tdoa_s.toFixed(6);
  $("audio-energy").textContent = snapshot.audio.energy.toFixed(2);
  $("audio-device").textContent = `${snapshot.audio.status.device_name}: ${snapshot.audio.status.message}`;
  if(snapshot.audio.processing)state.audioProcessingConfig={...snapshot.audio.processing};
  $("yaw-value").textContent = snapshot.pose.yaw;
  $("pitch-value").textContent = snapshot.pose.pitch;
  $("yaw-input").value = snapshot.pose.yaw;
  $("pitch-input").value = snapshot.pose.pitch;
  const cameraEnabled = snapshot.features?.camera_enabled ?? true;
  const visualEnabled = snapshot.features?.visual_enabled ?? true;
  const visualVisible = Boolean(snapshot.visual.visible || snapshot.visual.target);
  const specificSpeaker = Boolean(snapshot.specific_speaker_detected || snapshot.visual?.specific_speaker_detected);
  $("visual-state").textContent = visualEnabled
    ? (cameraEnabled ? (specificSpeaker ? "specific speaker" : (snapshot.visual.locked ? "已锁定" : (visualVisible ? "已识别" : "未锁定"))) : "摄像头关")
    : (cameraEnabled ? (visualVisible ? "已识别 · 跟踪关" : "跟踪关") : "摄像头关");
  $("target-label").textContent = specificSpeaker
    ? `id ${snapshot.locked_speaker_id ?? snapshot.visual.locked_speaker_id} score ${(snapshot.locked_speaker_score ?? snapshot.visual.locked_speaker_score ?? 0).toFixed(2)}`
    : (snapshot.visual.target?.label || "--");
  const detector = snapshot.visual.status?.detector && snapshot.visual.status.detector !== "unknown"
    ? ` · ${snapshot.visual.status.detector}`
    : "";
  const strategy = snapshot.strategy ? ` · ${snapshot.strategy}` : "";
  const asd = snapshot.asd_backend_status || snapshot.visual?.asd_backend_status;
  const asdText = asd ? ` · asd:${asd.actual}${asd.fallback ? " fallback" : ""}` : "";
  $("camera-status").textContent = snapshot.visual.status
    ? `${snapshot.visual.status.source}: ${snapshot.visual.status.message}${detector}${strategy}${asdText}`
    : "--";
  const capture = snapshot.visual.capture;
  const binocular = snapshot.visual.binocular;
  snapshot.visual.display_eye=binocular?.selected_eye;
  if($("fixation-status"))$("fixation-status").textContent=FixationView.description(snapshot.visual.fixation);
  const rectification=capture?.rectification;
  const position=snapshot.visual.target?.stereo_position;
  const bearing=snapshot.visual.target?.bearing;
  const selection=binocular?.target_selection;
  const directionProjection=capture?.direction_projection || {};
  const displayAudio=audioPresentation.update(snapshot.audio,selection,performance.now(),snapshot.diagnostics?.session_id || '');
  state.coreSource=snapshot.audio.core_source;
  renderAudioObservation(snapshot.audio,displayAudio);
  const guidance={...displayAudio,core:snapshot.audio.core_source,candidates:selection?.candidates || [],projection:directionProjection[binocular?.selected_eye]};
  snapshot.visual.guidance=guidance;
  snapshot.visual.direction_view=capture?.direction_view;
  $("stereo-space-status").textContent=position
    ? `双目空间估计：前方 ${position.z_m.toFixed(2)} m · 横向 ${position.x_m.toFixed(2)} m · 高低 ${(-position.y_m).toFixed(2)} m · 共同匹配点 ${position.inliers} 个（相机中点坐标，距离精度待核验）`
    : bearing ? `${bearing.source==="binocular_feature_rays" ? "双眼共同" : "单眼降级"}方向：${bearing.azimuth_left_deg>=0 ? "左" : "右"} ${Math.abs(bearing.azimuth_left_deg).toFixed(1)}° · ${bearing.elevation_up_deg>=0 ? "上" : "下"} ${Math.abs(bearing.elevation_up_deg).toFixed(1)}° · 距离输出关闭，角度精度待实测`
    : rectification?.calibrated ? `两眼共同校正已启用 · 有效画面 ${rectification.output_size.join("×")} · 当前无可靠三维匹配，使用二维观察`
    : rectification?.rectification_enabled ? `画面对齐预览已启用 · 共同有效画面 ${rectification.output_size.join("×")} · 正式测距未通过，距离输出关闭`
    : `立体校正与测距尚未启用：${rectification?.message || "需要先完成实物标定"}`;
  $("binocular-panel").hidden = !capture?.dual_eye_detection || !cameraEnabled;
  if (capture?.dual_eye_detection && cameraEnabled) {
    const reasons = {no_faces:"尚未检测到人脸",no_usable_eye:"控制视角暂无可用画面",observed_face:"选用当前人脸观察",target_retained:"保留稳定观察",confirming_view:"切换确认中，暂缓自动转向",stale_frame:"结果已过期，暂停视觉转向"};
    const eyeName = binocular?.selected_eye === "left" ? "左半幅" : binocular?.selected_eye === "right" ? "右半幅" : "暂无";
    const commonDirection=binocular?.uses_two_eye_geometry || binocular?.uses_two_eye_bearing;
    $("binocular-status").textContent = `双目识别 · ${commonDirection ? "显示视角" : "当前视角"}：${eyeName} · ${binocular?.uses_two_eye_bearing ? "双眼共同方向 · " : ""}${reasons[binocular?.selection_reason] || "等待检测"} · 同帧编号 ${binocular?.sample_sequence || "--"}`;
    $("binocular-coordinate-note").textContent=rectification?.calibrated
      ? "两眼已共同去畸变、立体校正并裁取有效矩形。可靠匹配时使用双目中点空间坐标；匹配不足时保留二维观察。下方大图是当前显示视角。"
      : rectification?.rectification_enabled ?
        (binocular?.uses_two_eye_bearing ? "当前目标已有可靠双目纹理对应，共同估计方向；正式距离输出关闭。" : "两眼都在检测；当前目标未取得可靠双目纹理对应，采用可用眼跟踪。") +
        " 校正主要消除上下错位，左右位置与单眼角度仍会因正常视差不同。画面中心为该视角0°，不代表两眼看见的目标必须重合。"
      : "左右半幅来自同一采集帧；两边均参与识别。跨目关联为外观暂定结果，尚未标定距离或统一空间角度。下方大图显示当前控制视角。";
    for (const eye of ["left","right"]) {
      const metrics = capture.per_eye?.[eye];
      const quality = capture.eye_health?.[eye];
      const faces = snapshot.visual.eye_targets?.[eye] || [];
      const qualityText = {ok:"可用",dark:"过暗",overexposed:"过曝",low_texture:"纹理不足",detector_error:"检测异常"}[quality?.reason] || "等待画面";
      $("eye-"+eye+"-status").textContent = `${qualityText} · 人脸 ${faces.length} 张 · ${(metrics?.inference_fps || 0).toFixed(1)} fps`;
      const image = snapshot.visual.eye_frames?.[eye];
      if (image) $("eye-"+eye+"-preview").src = "data:image/jpeg;base64,"+image;
      drawOverlay({...snapshot.visual,targets:faces,body_target:null,display_eye:eye,
        guidance:{...guidance,projection:directionProjection[eye]},
        target:faces.find(t=>t.face_id===snapshot.visual.target?.face_id) || null},"eye-"+eye+"-overlay","eye-"+eye+"-wrap");
    }
    const people = binocular?.people || [];
    $("binocular-people").textContent = people.length ? people.map(p=>`目标 #${p.person_id}：${p.association==="calibrated_correspondence" ? "双目空间匹配" : p.association==="aligned_feature_correspondence" ? "双眼纹理对应" : p.eyes.length===2 ? "两侧外观暂定关联" : p.eyes[0]==="left" ? "左侧观察" : "右侧观察"}`).join("；") : "两侧均在检测，当前没有有效人脸目标。";
    const modes={audio_guided:"声音辅助锁定",audio_sector_visual:"声音扇区内按视觉选人",visual_priority:"视觉主导",visual_fallback:"视觉持续跟随",visual_reacquire_wait:"短暂丢失，等待重新发现"};
    const audioReasons={stale_audio:"声音已过期",audio_disabled:"声音辅助关闭",awaiting_core_confirmation:"正在确认核心声音方向",no_voice_activity:"当前没有可靠语音",quiet:"安静",ambiguous_delay:"方向模糊",angular_ambiguity:"多个候选方向接近",direction_not_stable:"方向尚未稳定",audio_outside_visible_faces:"声音方向不在可见人脸中",motor_guard:"电机声音保护",inconsistent_frequency_bands:"频段方向不一致",uncertain_direction:"方向不确定"};
    audioReasons.visual_audio_conflict="声视未匹配，保持视觉";
    audioReasons.visible_faces_in_audio_sector="声音扇区内存在可见人脸";
    audioReasons.audio_visual_score_ambiguous="候选匹配度接近，保留视觉观察";
    audioReasons.replayed_audio_observation="音频重复或乱序，等待新观察";
    audioReasons.future_video_for_audio="画面时间异常，暂按视觉跟随";
    audioReasons.stale_video_for_audio="画面更新稍慢，声音暂不参与选人";
    $("av-guidance-status").textContent=["audio_visual_guided","visual_priority"].includes(snapshot.strategy)
      ? `${modes[selection?.mode] || "等待稳定观察"}${selection?.selected_person_id ? " · 目标 #"+selection.selected_person_id : ""} · ${audioReasons[selection?.audio_reason] || (selection?.audio_usable ? "声音方向可用" : "声音暂不参与选人")} · ${snapshot.running ? "自动控制已启动" : "仅预览，自动转头关闭"}` : "当前为旧策略对照模式";
    renderCandidateScores(selection);
    const approach=snapshot.visual.approach;
    const stages={coarse:"较大偏差",medium:"中等偏差",fine:"精细接近",hold:"保持"};
    const view=capture.direction_view?.visible_azimuth_interval_deg;
    $("visual-approach-status").textContent=snapshot.strategy==="visual_priority"
      ? `画面中心 0° · 声音搜索 ±${snapshot.visual.processing.audio_sector_half_width_deg || 15}° · ${view ? "当前有效视野约右"+Math.abs(view[0]).toFixed(0)+"°至左"+view[1].toFixed(0)+"° · " : ""}${approach?.mode ? "分段逼近：左右"+stages[approach.yaw_stage]+" / 上下"+stages[approach.pitch_stage] : "分段逼近已配置，等待启用视觉跟随"}；人脸大小只近似表示远近。` : "";
  }
  const focus = capture?.focus_capability;
  $("focus-capability").textContent = focus?.status === "unsupported"
    ? "设备未提供电动对焦控制（MF 手动调焦）；当前无法启用真正的自动对焦。请用下方局部画面辅助旋转镜头调焦。"
    : focus?.optical_autofocus_available ? "设备提供自动对焦能力；此版本尚未驱动调焦机构。"
    : focus?.status === "unavailable" ? "暂未读到对焦能力，不能据此判断支持自动对焦。"
    : "正在读取镜头对焦能力……";
  $("focus-panel").hidden = !capture || !cameraEnabled || !state.cameraConfig?.focus_assist;
  const search = capture?.multi_scale_search;
  $("face-search-status").textContent = search?.enabled
    ? `左右全画面检测 + 原像素局部搜索 · 累计补充 ${search.extra_passes} 次 · 当前每眼 ${capture.detector_input_width}×${capture.detector_input_height}。局部补查轮流执行，目标大小随画面自适应。`
    : "当前为全画面检测；开启 SCRFD 和近远小脸搜索后启用补充扫描。";
  for (const name of ["left","right"]) {
    const q = capture?.focus_assist?.[name];
    if(q) $("focus-"+name+"-score").textContent = `${name === "left" ? "左" : "右"}半幅：清晰度 ${q.score.toFixed(1)} · 亮度 ${q.mean_luma.toFixed(0)} · ${q.roi_source === "detected_face" ? "人脸区域" : "画面中央"}${q.reason === "low_texture" ? " / 纹理不足" : q.reason === "dark" ? " / 偏暗" : q.reason === "bright" ? " / 偏亮" : ""}`;
    const frame = snapshot.visual.focus_previews?.[name];
    if(frame) $("focus-"+name+"-image").src = "data:image/jpeg;base64,"+frame;
  }
  $("capture-metrics").textContent = capture
    ? `原始采集 ${capture.width}×${capture.height} / ${capture.measured_fps.toFixed(2)} fps（档位 ${capture.requested_fps}） · ${capture.dual_eye_detection ? "双目成对处理" : "单目处理"} ${capture.inference_fps.toFixed(2)} fps · 完整双目保留`
    : "";
  const audioCapture = snapshot.audio.capture;
  const vad=audioCapture?.speech_vad;
  $("speech-vad-note").textContent=vad ? `语音检测：${vad.backend==="silero_onnx_cpu" ? "Silero 本地模型" : "频谱门控降级"} · 语音概率 ${((vad.speech_probability || 0)*100).toFixed(0)}% · 方向时间一致性 ${((snapshot.audio.temporal_stability || 0)*100).toFixed(0)}%` : "";
  $("array-audio-note").hidden = !audioCapture;
  if (audioCapture) $("array-audio-note").textContent = audioCapture.localization_mode === "ear_pair"
    ? `头部两耳：左耳 CH${audioCapture.ear_left_channel} / 右耳 CH${audioCapture.ear_right_channel}；排除 ${audioCapture.ignored_channels.map(c => "CH" + c).join("、")}。${audioCapture.wasapi_raw ? "独立原始信号" : "系统处理信号"}；状态 ${audioCapture.localization_message}。两耳方案尚不能区分前后，角度使用名义间距。`
    : "四路原始录音保留；配置并确认耳部通道后启用定位。";
  $("microphone-capture-metrics").textContent = audioCapture
    ? `${audioCapture.backend} · ${audioCapture.channels} 通道 / ${audioCapture.sample_rate} Hz / ${audioCapture.sample_bits} bit · 溢出 ${audioCapture.overflow_count} · ` + audioCapture.channel_rms.map((value,i)=>`CH${i+1}: ${(20*Math.log10(Math.max(value,1e-9))).toFixed(1)} dBFS`).join(" / ")
    : "";
  $("motion-metrics").textContent = snapshot.motion?.enabled
    ? `平滑轨迹 ${snapshot.motion.sample_hz} Hz 采样 · 已发位置命令 ${snapshot.motion.position_commands} · ${snapshot.motion.error || "位置、速度与加速度连续"}`
    : "";
  $("stereo-panel").hidden = !capture || !cameraEnabled;
  if (snapshot.visual.stereo_frame && !$("stereo-panel").hidden) $("stereo-preview").src = `data:image/jpeg;base64,${snapshot.visual.stereo_frame}`;
  drawDirections(displayAudio);
  drawOverlay(snapshot.visual);
  const overlayOnly=visual=>({frame_width:visual.frame_width,frame_height:visual.frame_height,targets:visual.targets,
    target:visual.target,body_target:visual.body_target,processing:visual.processing,direction_view:visual.direction_view,
    fixation:visual.fixation,display_eye:visual.display_eye});
  state.audioOverlayCache={audio:snapshot.audio,guidance,
    main:cameraEnabled && capture?.ok!==false ? overlayOnly(snapshot.visual) : null,
    eyes:cameraEnabled && capture?.ok!==false && capture?.dual_eye_detection ? ['left','right'].map(eye=>{
      const targets=snapshot.visual.eye_targets?.[eye] || [];
      return {visual:{...overlayOnly(snapshot.visual),targets,body_target:null,display_eye:eye,
        target:targets.find(t=>t.face_id===snapshot.visual.target?.face_id) || null},
        projection:directionProjection[eye],canvasId:'eye-'+eye+'-overlay',wrapId:'eye-'+eye+'-wrap'};
    }) : []};
  state.audioOverlayDisplayActive=displayAudio.active;
  if (!cameraEnabled || capture?.ok === false || snapshot.visual.frame_included && !snapshot.visual.frame) {
    $("preview").removeAttribute("src");
    for (const eye of ["left","right"]) $("eye-"+eye+"-preview").removeAttribute("src");
    $("stereo-preview").removeAttribute("src");
    for (const eye of ["left","right"]) $("focus-"+eye+"-image").removeAttribute("src");
    state.lastFrame = "";
    if (cameraEnabled) $("capture-metrics").textContent = capture?.ok === false
      ? `摄像头画面已中断，正在重新连接；已暂停使用过期画面。${capture?.message || ""}`
      : "等待新的双目处理结果；已暂停使用过期画面。";
  } else if (snapshot.visual.frame && snapshot.visual.frame !== state.lastFrame) {
    $("preview").src = `data:image/jpeg;base64,${snapshot.visual.frame}`;
    state.lastFrame = snapshot.visual.frame;
  }
  const log = $("event-log");
  for (const event of snapshot.events || []) {
    const key = `${event.ts}-${event.event}`;
    if (state.eventsSeen.has(key)) continue;
    state.eventsSeen.add(key);
    const li = document.createElement("li");
    li.textContent = `${event.ts} ${event.event} ${JSON.stringify(event.detail)}`;
    log.prepend(li);
    while (log.children.length > 80) {
      log.removeChild(log.lastElementChild);
    }
  }
  window.DeepSightShell?.update(snapshot);
}

function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (event) => render(JSON.parse(event.data));
  ws.onopen = () => { $("status-chip").textContent = "实时连接"; };
  ws.onclose = () => {
    $("status-chip").textContent = "重连中";
    setTimeout(connectWs, 1000);
  };
}

async function sendPose() {
  const yaw = Number($("yaw-input").value);
  const pitch = Number($("pitch-input").value);
  const snapshot = await withFeedback("发送姿态...", "姿态已发送", () =>
    api("/api/head/move", {
      method: "POST",
      body: JSON.stringify({ yaw, pitch, time_ms: 1200 }),
    }).then((result) => {
      if (result.error) throw new Error(result.error);
      return result;
    })
  );
  $("yaw-value").textContent = snapshot.target.yaw;
  $("pitch-value").textContent = snapshot.target.pitch;
}

function applyDirectionConfig(direction) {
  if (!direction) return;
  $("yaw-left-sign").value = String(direction.yaw_left_sign);
  $("pitch-up-sign").value = String(direction.pitch_up_sign);
  $("manual-step-input").value = direction.manual_step;
  $("direction-config-status").textContent = `已加载: 左=${direction.yaw_left_sign > 0 ? "Yaw增大" : "Yaw减小"} 上=${direction.pitch_up_sign > 0 ? "Pitch增大" : "Pitch减小"} 步长=${direction.manual_step}`;
}

function applyFeatureConfig(features) {
  if (!features) return;
  $("audio-enabled-input").checked = Boolean(features.audio_enabled);
  $("visual-enabled-input").checked = Boolean(features.visual_enabled);
  $("camera-enabled-input").checked = features.camera_enabled !== false;
}

function populateCameraDevices() {
  const select = $("camera-device-select");
  const current = select.value;
  select.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "自动选择";
  select.appendChild(auto);
  for (const device of state.dshowDevices.filter((item) => item.kind === "video")) {
    const option = document.createElement("option");
    option.value = device.name;
    option.textContent = device.name;
    if (device.alternative_name) option.title = device.alternative_name;
    select.appendChild(option);
  }
  select.value = current;
}

function ensureCameraOption(deviceName) {
  if (!deviceName) return;
  const select = $("camera-device-select");
  if ([...select.options].some((option) => option.value === deviceName)) return;
  const option = document.createElement("option");
  option.value = deviceName;
  option.textContent = `${deviceName} (未枚举)`;
  select.appendChild(option);
}

function applyCameraConfig(camera) {
  if (!camera) return;
  state.cameraConfig = camera;
  $("multi-scale-search").checked = camera.multi_scale_search !== false;
  $("focus-assist-enabled").checked = camera.focus_assist !== false;
  ensureCameraOption(camera.device_name);
  $("camera-device-select").value = camera.device_name || "";
  $("camera-video-size-input").value = camera.video_size || "";
  $("camera-fps-input").value = camera.fps ?? "";
  $("camera-input-fps-input").value = camera.input_fps ?? 60;
  $("camera-capture-backend-select").value = camera.capture_backend || "stereo_mjpeg";
  $("camera-output-width-input").value = camera.output_width ?? 1920;
  $("tracking-eye").value = camera.tracking_eye || "auto";
  $("left-eye-rotation").value = String(camera.left_rotation || 0);
  $("right-eye-rotation").value = String(camera.right_rotation || 0);
  $("rectification-enabled").checked = camera.stereo_geometry_enabled !== false;
  $("face-detector-backend-select").value = camera.face_detector_backend || "mediapipe";
  $("scrfd-model-path-input").value = camera.scrfd_model_path || "";
  $("scrfd-threshold-input").value = camera.scrfd_threshold ?? 0.35;
  $("scrfd-input-size-input").value = camera.scrfd_input_size ?? 640;
  $("camera-crop-select").value = camera.crop_left_half === null || camera.crop_left_half === undefined
    ? ""
    : String(Boolean(camera.crop_left_half));
  updateCameraCaptureControls();
}

function updateCameraCaptureControls() {
  const native = $("camera-capture-backend-select").value === "stereo_mjpeg";
  $("camera-crop-select").disabled = native;
  $("camera-crop-select").title = native ? "双目原始画面完整保留；检测使用经过方向修正的有效画面。" : "旧设备采集的裁切选项";
}

$("camera-capture-backend-select").addEventListener("change", updateCameraCaptureControls);

function applyAudioConfig(audioConfig) {
  if (!audioConfig) return;
  $("audio-channel-map").value = audioConfig.swap_channels ? "swap" : "normal";
}

function applyEarConfig(config) {
  if (!config) return;
  state.audioCaptureConfig = config;
  $("ear-mode").value = config.localization_mode || "disabled";
  $("ear-left-channel").value = config.ear_left_channel ?? "";
  $("ear-right-channel").value = config.ear_right_channel ?? "";
  $("ear-spacing-mm").value = config.ear_spacing_m == null ? "" : config.ear_spacing_m * 1000;
  $("ear-map-confirmed").checked = Boolean(config.ear_mapping_confirmed);
  $("ear-raw").checked = config.wasapi_raw !== false;
}

async function saveEarConfig() {
  const config = state.audioCaptureConfig || {};
  const session = await withFeedback("保存耳部配置...", "耳部配置已保存", () => api("/api/config/audio-capture", {
    method: "POST", body: JSON.stringify({...config, localization_mode: $("ear-mode").value,
      ear_left_channel: optionalNumber("ear-left-channel"), ear_right_channel: optionalNumber("ear-right-channel"),
      ear_spacing_m: optionalNumber("ear-spacing-mm") == null ? null : Number($("ear-spacing-mm").value) / 1000,
      ear_mapping_confirmed: $("ear-map-confirmed").checked, wasapi_raw: $("ear-raw").checked})
  }));
  applyEarConfig(session.audio_capture);
}

function applyControlConfig(controlConfig) {
  if (!controlConfig) return;
  state.controlConfig = controlConfig;
  $("smooth-motion-select").value = String(controlConfig.smooth_motion !== false);
  $("control-profile-select").value = controlConfig.control_profile || "stable";
  $("motor-guard-input").value = controlConfig.motor_guard_ms ?? 250;
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, Number(value)));
}

function applyAxisLimitsConfig(axisLimits) {
  if (!axisLimits) return;
  state.axisLimits = {
    yaw_min: axisLimits.yaw_min ?? 800,
    yaw_center: axisLimits.yaw_center ?? 1500,
    yaw_max: axisLimits.yaw_max ?? 2200,
    pitch_min: axisLimits.pitch_min ?? 1200,
    pitch_center: axisLimits.pitch_center ?? 1500,
    pitch_max: axisLimits.pitch_max ?? 1800,
  };
  $("yaw-min-input").value = state.axisLimits.yaw_min;
  $("yaw-center-input").value = state.axisLimits.yaw_center;
  $("yaw-max-input").value = state.axisLimits.yaw_max;
  $("pitch-min-input").value = state.axisLimits.pitch_min;
  $("pitch-center-input").value = state.axisLimits.pitch_center;
  $("pitch-max-input").value = state.axisLimits.pitch_max;
  $("yaw-input").min = state.axisLimits.yaw_min;
  $("yaw-input").max = state.axisLimits.yaw_max;
  $("pitch-input").min = state.axisLimits.pitch_min;
  $("pitch-input").max = state.axisLimits.pitch_max;
  $("yaw-input").value = clamp($("yaw-input").value, state.axisLimits.yaw_min, state.axisLimits.yaw_max);
  $("pitch-input").value = clamp($("pitch-input").value, state.axisLimits.pitch_min, state.axisLimits.pitch_max);
  // All yaw presets and the centre pad share the calibrated horizontal pitch.
  for (const button of document.querySelectorAll("[data-yaw]")) {
    button.dataset.pitch = String(state.axisLimits.pitch_center);
    if (button.classList.contains("home")) button.dataset.yaw = String(state.axisLimits.yaw_center);
  }
}

function applyHeadPresets(presets) {
  if (!presets) return;
  for (const button of document.querySelectorAll("[data-pose-preset]")) {
    const pose = presets[button.dataset.posePreset];
    if (!pose) continue;
    button.dataset.yaw = String(pose.yaw);
    button.dataset.pitch = String(pose.pitch);
    button.disabled = false;
  }
}

function applyProcessingConfig(audioProcessing, visionProcessing) {
  if (audioProcessing) {
    state.audioProcessingConfig={...audioProcessing};
    $("vad-enabled-input").checked = audioProcessing.vad_enabled !== false;
    $("audio-threshold-input").value = audioProcessing.audio_confidence_threshold ?? 0.20;
    $("speech-threshold-input").value = audioProcessing.speech_confidence_threshold ?? 0.15;
    $("doa-threshold-input").value = audioProcessing.doa_confidence_threshold ?? 0.05;
    $("audio-hits-input").value = audioProcessing.required_audio_hits ?? 1;
  }
  if (visionProcessing) {
    $("active-speaker-input").checked = visionProcessing.active_speaker_enabled !== false;
    $("tracking-strategy-select").value = visionProcessing.tracking_strategy || "classic_audio_first";
    $("audio-sector-half-width").value=visionProcessing.audio_sector_half_width_deg ?? 15;
    $("asd-backend-select").value = visionProcessing.asd_backend || "rules";
    $("speaker-lock-policy-select").value = visionProcessing.speaker_lock_policy || "turn_hold";
    $("visual-yaw-mode-select").value = visionProcessing.visual_yaw_mode || "small";
    $("visual-pitch-input").checked = visionProcessing.visual_pitch_enabled !== false;
    $("visual-mirror-x-input").checked = visionProcessing.visual_mirror_x === true;
    $("visual-yaw-deadband-input").value = visionProcessing.visual_yaw_deadband ?? "";
    $("visual-pitch-deadband-input").value = visionProcessing.visual_pitch_deadband ?? "";
    $("visual-yaw-min-delta-input").value = visionProcessing.visual_yaw_min_delta ?? 0;
    $("visual-yaw-max-delta-input").value = visionProcessing.visual_yaw_max_delta ?? "";
    $("visual-pitch-min-delta-input").value = visionProcessing.visual_pitch_min_delta ?? 0;
    $("visual-pitch-max-delta-input").value = visionProcessing.visual_pitch_max_delta ?? "";
    $("target-offset-x-input").value = visionProcessing.target_offset_x_norm ?? 0.04;
    $("target-offset-y-input").value = visionProcessing.target_offset_y_norm ?? 0;
    $("speaker-threshold-input").value = visionProcessing.visual_speaker_threshold ?? 0.30;
    $("mouth-threshold-input").value = visionProcessing.mouth_evidence_threshold ?? 0.06;
    $("min-face-height-input").value = visionProcessing.min_face_height_ratio ?? 0.12;
    $("keep-face-height-input").value = visionProcessing.keep_face_height_ratio ?? 0.09;
    $("talknet-threshold-input").value = visionProcessing.talknet_threshold ?? 0.55;
    $("speaker-hold-input").value = visionProcessing.speaker_lock_hold_s ?? 1.2;
    $("speaker-lost-input").value = visionProcessing.speaker_lost_timeout_s ?? 0.8;
    $("audio-interrupt-input").checked = visionProcessing.audio_interrupt_enabled === true;
    $("silent-visual-audio-search-input").checked = visionProcessing.audio_search_after_silent_visual === true;
    $("silent-visual-hold-input").value = visionProcessing.silent_visual_hold_s ?? 1.2;
  }
}

async function saveFeatureConfig() {
  const session = await withFeedback("保存开关...", "开关已保存", () =>
    api("/api/config/features", {
      method: "POST",
      body: JSON.stringify({
        audio_enabled: $("audio-enabled-input").checked,
        visual_enabled: $("visual-enabled-input").checked,
        camera_enabled: $("camera-enabled-input").checked,
      }),
    })
  );
  applyFeatureConfig(session.features);
}

async function saveCameraConfig() {
  const cropValue = $("camera-crop-select").value;
  const fpsValue = $("camera-fps-input").value.trim();
  const outputWidthValue = $("camera-output-width-input").value.trim();
  const session = await withFeedback("保存相机...", "相机配置已保存", () =>
    api("/api/config/camera", {
      method: "POST",
      body: JSON.stringify({
        device_name: $("camera-device-select").value || null,
        video_size: $("camera-video-size-input").value.trim() || null,
        crop_left_half: cropValue === "" ? null : cropValue === "true",
        fps: fpsValue === "" ? null : Number(fpsValue),
        output_width: outputWidthValue === "" ? 1920 : Number(outputWidthValue),
        input_fps: Number($("camera-input-fps-input").value || 60),
        capture_backend: $("camera-capture-backend-select").value,
        usb_vid: state.cameraConfig?.usb_vid || "1BCF",
        usb_pid: state.cameraConfig?.usb_pid || "0B15",
        tracking_eye: $("tracking-eye").value,
        left_rotation: Number($("left-eye-rotation").value),
        right_rotation: Number($("right-eye-rotation").value),
        multi_scale_search: $("multi-scale-search").checked,
        focus_assist: $("focus-assist-enabled").checked,
        preview_jpeg_quality: state.cameraConfig?.preview_jpeg_quality ?? 95,
        stereo_calibration_path: state.cameraConfig?.stereo_calibration_path || "calibration/stereo_camera.json",
        stereo_geometry_enabled: $("rectification-enabled").checked,
        face_detector_backend: $("face-detector-backend-select").value || "mediapipe",
        scrfd_model_path: $("scrfd-model-path-input").value.trim() || null,
        scrfd_threshold: Number($("scrfd-threshold-input").value || 0.35),
        scrfd_input_size: Number($("scrfd-input-size-input").value || 640),
      }),
    })
  );
  applyCameraConfig(session.camera);
}

async function saveAudioConfig() {
  const session = await withFeedback("保存声音设置...", "声音设置已保存", async () => {
    const mapping = await api("/api/config/audio", {
      method: "POST",
      body: JSON.stringify({
        swap_channels: $("audio-channel-map").value === "swap",
      }),
    });
    applyAudioConfig(mapping.audio_config);
    return api("/api/config/audio-processing", {
    method: "POST",
    body: JSON.stringify({
      vad_enabled: $("vad-enabled-input").checked,
      audio_confidence_threshold: Number($("audio-threshold-input").value || 0.20),
      speech_confidence_threshold: Number($("speech-threshold-input").value || 0.15),
      doa_confidence_threshold: Number($("doa-threshold-input").value || 0.05),
      required_audio_hits: Number($("audio-hits-input").value || 1),
      denoise_enabled: state.audioProcessingConfig?.denoise_enabled ?? false,
      denoise_dry_mix: state.audioProcessingConfig?.denoise_dry_mix ?? 0.15,
      denoise_output_dir: state.audioProcessingConfig?.denoise_output_dir ?? null,
      recording_enabled: state.audioProcessingConfig?.recording_enabled ?? false,
      use_denoised_for_localization: state.audioProcessingConfig?.use_denoised_for_localization ?? false,
    }),
    });
  });
  applyProcessingConfig(session.audio_processing || session, null);
}

async function saveVisionProcessingConfig() {
  const visionProcessing = await withFeedback("保存说话人...", "说话人配置已保存", () =>
    api("/api/config/vision-processing", {
      method: "POST",
      body: JSON.stringify({
        active_speaker_enabled: $("active-speaker-input").checked,
        tracking_strategy: $("tracking-strategy-select").value || "classic_audio_first",
        audio_sector_half_width_deg:Number($("audio-sector-half-width").value || 15),
        asd_backend: $("asd-backend-select").value || "rules",
        speaker_lock_policy: $("speaker-lock-policy-select").value || "turn_hold",
        visual_yaw_mode: $("visual-yaw-mode-select").value || "small",
        visual_pitch_enabled: $("visual-pitch-input").checked,
        visual_mirror_x: $("visual-mirror-x-input").checked,
        visual_yaw_deadband: optionalNumber("visual-yaw-deadband-input"),
        visual_pitch_deadband: optionalNumber("visual-pitch-deadband-input"),
        visual_yaw_min_delta: Number($("visual-yaw-min-delta-input").value || 0),
        visual_yaw_max_delta: optionalNumber("visual-yaw-max-delta-input"),
        visual_pitch_min_delta: Number($("visual-pitch-min-delta-input").value || 0),
        visual_pitch_max_delta: optionalNumber("visual-pitch-max-delta-input"),
        target_offset_x_norm: Number($("target-offset-x-input").value || 0),
        target_offset_y_norm: Number($("target-offset-y-input").value || 0),
        visual_speaker_threshold: Number($("speaker-threshold-input").value || 0.30),
        mouth_evidence_threshold: Number($("mouth-threshold-input").value || 0.06),
        min_face_height_ratio: Number($("min-face-height-input").value || 0.12),
        keep_face_height_ratio: Number($("keep-face-height-input").value || 0.09),
        talknet_threshold: Number($("talknet-threshold-input").value || 0.55),
        speaker_lock_hold_s: Number($("speaker-hold-input").value || 1.2),
        speaker_lost_timeout_s: Number($("speaker-lost-input").value || 0.8),
        audio_interrupt_enabled: $("audio-interrupt-input").checked,
        audio_search_after_silent_visual: $("silent-visual-audio-search-input").checked,
        silent_visual_hold_s: Number($("silent-visual-hold-input").value || 1.2),
      }),
    })
  );
  applyProcessingConfig(null, visionProcessing.vision_processing || visionProcessing);
}

async function saveControlConfig() {
  const session = await withFeedback("保存控制...", "控制配置已保存", () =>
    api("/api/config/control", {
      method: "POST",
      body: JSON.stringify({
        ...state.controlConfig,
        smooth_motion: $("smooth-motion-select").value === "true",
        control_profile: $("control-profile-select").value,
        motor_guard_ms: Number($("motor-guard-input").value || 250),
      }),
    })
  );
  applyControlConfig(session.control);
}

async function saveAxisLimitsConfig() {
  const session = await withFeedback("保存范围...", "范围已保存", () =>
    api("/api/config/axis-limits", {
      method: "POST",
      body: JSON.stringify({
        yaw_min: Number($("yaw-min-input").value || 800),
        yaw_center: Number($("yaw-center-input").value || 1500),
        yaw_max: Number($("yaw-max-input").value || 2200),
        pitch_min: Number($("pitch-min-input").value || 1200),
        pitch_center: Number($("pitch-center-input").value || 1500),
        pitch_max: Number($("pitch-max-input").value || 1800),
      }),
    })
  );
  applyAxisLimitsConfig(session.axis_limits);
  applyHeadPresets(session.head_presets);
}

async function sendJog(direction) {
  const amount = Number($("manual-step-input").value || 60);
  const snapshot = await withFeedback("点动中...", "点动已发送", () =>
    api("/api/head/jog", {
      method: "POST",
      body: JSON.stringify({ direction, amount, time_ms: 900 }),
    }).then((result) => {
      if (result.error) throw new Error(result.error);
      return result;
    })
  );
  $("yaw-value").textContent = snapshot.target.yaw;
  $("pitch-value").textContent = snapshot.target.pitch;
  $("yaw-input").value = snapshot.target.yaw;
  $("pitch-input").value = snapshot.target.pitch;
}

async function recordVisualCalibrationSample() {
  const yaw = Number($("yaw-input").value || 1500);
  const pitch = Number($("pitch-input").value || 1500);
  const sample = await withFeedback("Recording visual sample...", "Visual sample recorded", () =>
    api("/api/debug/visual-calibration", {
      method: "POST",
      body: JSON.stringify({ yaw, pitch, note: "manual-aligned" }),
    })
  );
  const output = $("visual-calibration-result");
  if (!sample.ok) {
    output.textContent = `No target: ${sample.error || "unknown"}`;
    return;
  }
  const px = sample.pixel_error || {};
  const delta = sample.model_delta_from_aligned || {};
  const need = sample.offset_needed_for_tracking || {};
  const tracking = sample.tracking_center || {};
  output.textContent =
    `pose ${sample.aligned_pose.yaw}/${sample.aligned_pose.pitch} · ` +
    `track ${Number(tracking.x || 0).toFixed(1)},${Number(tracking.y || 0).toFixed(1)} · ` +
    `px err ${Number(px.x || 0).toFixed(1)},${Number(px.y || 0).toFixed(1)} · ` +
    `model d ${Number(delta.yaw || 0)},${Number(delta.pitch || 0)} · ` +
    `target ${Number(need.x_norm || 0).toFixed(3)},${Number(need.y_norm || 0).toFixed(3)}`;
}

async function saveDirectionConfig() {
  const session = await withFeedback("保存方向...", "方向已保存", () =>
    api("/api/config/direction", {
      method: "POST",
      body: JSON.stringify({
        yaw_left_sign: Number($("yaw-left-sign").value),
        pitch_up_sign: Number($("pitch-up-sign").value),
        manual_step: Number($("manual-step-input").value || 60),
      }),
    })
  );
  applyDirectionConfig(session.direction);
  applyHeadPresets(session.head_presets);
  $("direction-config-status").textContent = "方向配置已永久保存";
}

window.addEventListener("DOMContentLoaded", async () => {
  drawDirections({active:false});
  setInterval(()=>refreshAudioDisplay(),100);
  connectWs();
  try {
    const devices = await api("/api/devices");
    state.dshowDevices = devices.dshow || [];
    populateCameraDevices();
    const session = await api("/api/session");
    $("servo-config").textContent = `${session.servo.port} yaw:${session.servo.yaw_id ?? "未设"} pitch:${session.servo.pitch_id}`;
    $("port-input").value = session.servo.port;
    $("yaw-id-input").value = session.servo.yaw_id ?? "";
    $("pitch-id-input").value = session.servo.pitch_id;
    applyDirectionConfig(session.direction);
    applyFeatureConfig(session.features);
    applyCameraConfig(session.camera);
    applyAudioConfig(session.audio_config);
    applyEarConfig(session.audio_capture);
    applyControlConfig(session.control);
    applyAxisLimitsConfig(session.axis_limits || session.servo?.axis_limits);
    applyHeadPresets(session.head_presets);
    applyProcessingConfig(session.audio_processing, session.vision_processing);
  } catch (error) {
    $("status-chip").textContent = "后端不可用";
  }

  $("start-btn").onclick = async () => {
    $("start-btn").disabled = true;
    try {
      const result = await withFeedback("检查两轴反馈并启动...", "Demo 已开始", () => api("/api/demo/start", { method: "POST" }));
      $("running-label").textContent = String(result.running);
    } finally {$("start-btn").disabled = false;}
  };
  $("stop-btn").onclick = async () => {
    const result = await withFeedback("停止中...", "Demo 已停止", () => api("/api/demo/stop", { method: "POST" }));
    $("running-label").textContent = String(result.running);
  };
  $("center-btn").onclick = async () => {
    const snapshot = await withFeedback("回中中...", "回中已发送", () =>
      api("/api/head/move", {
        method: "POST",
        body: JSON.stringify({
          yaw: state.axisLimits.yaw_center,
          pitch: state.axisLimits.pitch_center,
          time_ms: 1500,
        }),
      })
        .then((result) => {
          if (result.error) throw new Error(result.error);
          return result;
        })
    );
    $("yaw-value").textContent = snapshot.target.yaw;
    $("pitch-value").textContent = snapshot.target.pitch;
    $("yaw-input").value = snapshot.target.yaw;
    $("pitch-input").value = snapshot.target.pitch;
  };
  $("send-pose-btn").onclick = sendPose;
  $("visual-calibration-btn").onclick = recordVisualCalibrationSample;
  $("scan-btn").onclick = async () => {
    const result = await withFeedback("扫描中...", "扫描完成", () =>
      api("/api/servo/scan", { method: "POST", body: JSON.stringify({ ids: Array.from({ length: 16 }, (_, i) => i) }) })
    );
    $("scan-result").textContent = result.error
      ? `扫描失败: ${result.error}`
      : `发现 ID: ${result.found_ids.join(", ") || "无"}`;
    if (result.error) setFeedback(`扫描失败: ${result.error}`, "error");
  };
  $("save-axis-btn").onclick = async () => {
    const portRaw = $("port-input").value.trim();
    const yawRaw = $("yaw-id-input").value.trim();
    const pitchRaw = $("pitch-id-input").value.trim();
    const session = await withFeedback("保存轴配置...", "轴配置已保存", () =>
      api("/api/config/servos", {
        method: "POST",
        body: JSON.stringify({
          port: portRaw === "" ? null : portRaw,
          yaw_id: yawRaw === "" ? null : Number(yawRaw),
          pitch_id: pitchRaw === "" ? 2 : Number(pitchRaw),
        }),
      })
    );
    $("servo-config").textContent = `${session.servo.port} yaw:${session.servo.yaw_id ?? "未设"} pitch:${session.servo.pitch_id}`;
  };
  $("save-direction-btn").onclick = saveDirectionConfig;
  $("read-actual-position-btn").onclick = async () => {
    const results = await withFeedback("读取实测位置...", "位置查询完成", async () => {
      const values = [];
      for (const [label, id] of [["Yaw", $("yaw-id-input").value], ["Pitch", $("pitch-id-input").value]]) {
        if (id === "") { values.push(`${label}：未配置 ID`); continue; }
        const result = await api("/api/servo/read-raw", {method:"POST", body:JSON.stringify({servo_id:Number(id),samples:1,interval_ms:0})});
        const record = result.records?.[0];
        values.push(`${label} / ID ${id}：${record?.position ?? "无位置反馈"}${record?.raw ? "（"+record.raw+"）" : ""}`);
      }
      return values;
    });
    $("actual-position-result").textContent = `${results.join("；")} · ${new Date().toLocaleTimeString()}`;
  };
  $("check-servo-btn").onclick = async () => {
    $("check-servo-btn").disabled = true;
    try {
      const result = await withFeedback("只读检查两轴位置...", "两轴位置反馈正常；自动转动保持暂停", () => api("/api/servo/check", {method:"POST"}));
      $("actual-position-result").textContent = Object.entries(result.feedback).map(([id,row])=>`ID ${id}：${row.position}`).join("；");
    } finally {$("check-servo-btn").disabled=false;}
  };
  $("discover-servo-btn").onclick = async () => {
    const button=$("discover-servo-btn");button.disabled=true;
    try {
      const result=await withFeedback("搜索USB串口并只读核对两轴...","搜索完成，转动保持暂停",()=>api("/api/servo/discover",{method:"POST"}));
      const detail=(result.trials || []).map(row=>`${row.device} ${row.description}：`+
        Object.entries(row.positions || {}).map(([id,p])=>`ID ${id} ${p ?? "无回包"}`).join("，")).join("；");
      $("device-discovery-result").textContent=(result.ok?`已确认舵机串口 ${result.port}。`:
        result.reason==="multiple_matching_adapters"?"发现多个匹配的控制器，请手动选择。":"未获得两轴完整回包；不能仅凭串口存在就启用转动。")+detail;
      if(result.ok){
        $("port-input").value=result.port;
        const session=await api("/api/session");
        $("servo-config").textContent=`${session.servo.port} yaw:${session.servo.yaw_id ?? "未设"} pitch:${session.servo.pitch_id}`;
      }
    } finally {button.disabled=false;}
  };
  $("discover-devices-btn").onclick = async () => {
    const button=$("discover-devices-btn");button.disabled=true;
    try {
      const devices=await api("/api/devices");state.dshowDevices=devices.dshow || [];populateCameraDevices();
      const inputs=(devices.wasapi || []).filter(row=>row.max_input_channels>=4 && /YDPI4MIC/i.test(row.name || ""));
      $("device-discovery-result").textContent=`已刷新：视频设备 ${state.dshowDevices.filter(row=>row.kind==="video").length} 个；YDPI4MIC四路输入 ${inputs.length} 个；`+
        `当前舵机串口 ${devices.bindings?.servo_port || "未绑定"}。相机按1BCF:0B15、麦克风按EF09:459D和四路输入自动匹配，不按易变的设备序号绑定。`;
    } finally {button.disabled=false;}
  };
  $("check-stereo-btn").onclick = async () => {
    const button=$("check-stereo-btn");button.disabled=true;
    try {
      const result=await withFeedback("检查双目共同特征...","双目检查完成，自动转动保持暂停",()=>api("/api/stereo/check",{method:"POST"}));
      $("stereo-preflight-status").textContent=result.message;
    } finally {button.disabled=false;}
  };
  $("save-camera-config-btn").onclick = saveCameraConfig;
  $("focus-native-btn").onclick = () => $("focus-crops").classList.toggle("native-size");
  $("multi-scale-search").onchange = saveCameraConfig;
  $("focus-assist-enabled").onchange = saveCameraConfig;
  $("rectification-enabled").onchange = saveCameraConfig;
  $("save-ear-config-btn").onclick = saveEarConfig;
  $("camera-fps-input").onblur = saveCameraConfig;
  $("camera-input-fps-input").onblur = saveCameraConfig;
  $("camera-output-width-input").onblur = saveCameraConfig;
  $("face-detector-backend-select").onchange = saveCameraConfig;
  $("scrfd-model-path-input").onblur = saveCameraConfig;
  $("scrfd-threshold-input").onchange = saveCameraConfig;
  $("scrfd-input-size-input").onchange = saveCameraConfig;
  $("save-audio-config-btn").onclick = saveAudioConfig;
  $("audio-threshold-input").onchange = saveAudioConfig;
  $("speech-threshold-input").onchange = saveAudioConfig;
  $("doa-threshold-input").onchange = saveAudioConfig;
  $("audio-hits-input").onchange = saveAudioConfig;
  $("active-speaker-input").onchange = saveVisionProcessingConfig;
  $("tracking-strategy-select").onchange = saveVisionProcessingConfig;
  $("asd-backend-select").onchange = saveVisionProcessingConfig;
  $("speaker-lock-policy-select").onchange = saveVisionProcessingConfig;
  $("visual-yaw-mode-select").onchange = saveVisionProcessingConfig;
  $("visual-pitch-input").onchange = saveVisionProcessingConfig;
  $("visual-mirror-x-input").onchange = saveVisionProcessingConfig;
  $("visual-yaw-deadband-input").onchange = saveVisionProcessingConfig;
  $("visual-pitch-deadband-input").onchange = saveVisionProcessingConfig;
  $("visual-yaw-min-delta-input").onchange = saveVisionProcessingConfig;
  $("visual-yaw-max-delta-input").onchange = saveVisionProcessingConfig;
  $("visual-pitch-min-delta-input").onchange = saveVisionProcessingConfig;
  $("visual-pitch-max-delta-input").onchange = saveVisionProcessingConfig;
  $("target-offset-x-input").onchange = saveVisionProcessingConfig;
  $("target-offset-y-input").onchange = saveVisionProcessingConfig;
  $("target-offset-x-input").onblur = saveVisionProcessingConfig;
  $("target-offset-y-input").onblur = saveVisionProcessingConfig;
  $("speaker-threshold-input").onchange = saveVisionProcessingConfig;
  $("mouth-threshold-input").onchange = saveVisionProcessingConfig;
  $("min-face-height-input").onchange = saveVisionProcessingConfig;
  $("keep-face-height-input").onchange = saveVisionProcessingConfig;
  $("talknet-threshold-input").onchange = saveVisionProcessingConfig;
  $("speaker-hold-input").onchange = saveVisionProcessingConfig;
  $("speaker-lost-input").onchange = saveVisionProcessingConfig;
  $("audio-interrupt-input").onchange = saveVisionProcessingConfig;
  $("silent-visual-audio-search-input").onchange = saveVisionProcessingConfig;
  $("silent-visual-hold-input").onchange = saveVisionProcessingConfig;
  $("save-control-btn").onclick = saveControlConfig;
  $("save-axis-limits-btn").onclick = saveAxisLimitsConfig;
  $("camera-enabled-input").onchange = saveFeatureConfig;
  $("audio-enabled-input").onchange = saveFeatureConfig;
  $("visual-enabled-input").onchange = saveFeatureConfig;
  $("clear-log-btn").onclick = () => { $("event-log").innerHTML = ""; state.eventsSeen.clear(); };
  for (const button of document.querySelectorAll("[data-jog]")) {
    button.addEventListener("click", () => sendJog(button.dataset.jog));
  }
  for (const button of document.querySelectorAll("[data-yaw]")) {
    button.addEventListener("click", () => {
      $("yaw-input").value = button.dataset.yaw;
      $("pitch-input").value = button.dataset.pitch;
      sendPose();
    });
  }
});

let calibrationTimer=null;
async function refreshCalibration() {
  try {
    const result=await api("/api/stereo-calibration/state");
    const names={not_started:"本次未开始新组",collecting:"正在采集",collected:"已暂停采集",solving:"正在计算",accepted:"检查通过",rejected:"需要补采",error:"暂未通过"};
    $("calibration-status").textContent=`${names[result.status] || result.status} · 已保存 ${result.pairs}/${result.minimum_pairs} 组（至少） · ${result.message}`;
    const labels={left_rms:"左目重投影",right_rms:"右目重投影",stereo_rms:"双目拟合",holdout_epipolar:"留出极线误差",holdout_reprojection:"留出投影误差",baseline_plausible:"基线尺寸",horizontal_baseline:"水平安装",horizontal_coverage:"左右覆盖",vertical_coverage:"上下覆盖",scale_diversity:"距离变化",pose_diversity:"倾角变化",optimizer_converged:"参数优化收敛",positive_training_depth:"两眼前方几何"};
    const quality=result.quality;
    if(quality){
      const failed=Object.entries(quality.checks).filter(([,ok])=>!ok).map(([key])=>`${labels[key] || key}未通过`);
      const diagnostics=[`留出行误差 P95 ${quality.holdout_epipolar_p95_px.toFixed(2)} px`,
        `留出投影 P95 ${quality.holdout_reprojection_p95_px.toFixed(2)} px`,
        `参数拟合 ${quality.training_pairs} / 独立检查 ${quality.holdout_pairs} 组`,
        quality.validation_split?.temporal_separation_verified ? "按采集时间分组验证" : "采集时间隔离未核验",
        `基线${quality.metric_scale_confirmed ? "" : "名义值"} ${(quality.baseline_m*1000).toFixed(2)} mm`];
      if(quality.residual_audit)diagnostics.push(`平面拟合警告 ${quality.residual_audit.warning_views} 组（不能据此确定纸面弯曲）`);
      diagnostics.push(failed.length ? failed.join("；") : "质量检查通过；实际距离精度仍需实测");
      $("calibration-quality").textContent=diagnostics.join(" · ");
    }else{
      $("calibration-quality").textContent=result.alignment_preview_exists ? "已有图像对齐预览，正式距离输出关闭；本次尚未开始新一组采集，之前的样本仍保留。" : "校正与测距等待实物样本；倾斜角度不会使用目测值代替。";
    }
    const busy=result.status==="collecting" || result.status==="solving";
    $("calibration-start-btn").disabled=busy || !$("calibration-scale-confirmed").checked;
    $("calibration-resume-btn").disabled=busy || !result.session || result.pairs>=45 || result.status==="accepted";
    $("calibration-stop-btn").disabled=result.status!=="collecting";
    $("calibration-solve-btn").disabled=result.pairs<20 || result.status==="solving";
    if(result.preview){ $("calibration-sample-preview").src="data:image/jpeg;base64,"+result.preview; $("calibration-sample-preview").hidden=false; }
  } catch(error){ $("calibration-status").textContent=`标定状态：${error.message}`; }
}
document.addEventListener("DOMContentLoaded",()=>{
  $("calibration-scale-confirmed").addEventListener("change",refreshCalibration);
  $("calibration-panel").addEventListener("toggle",()=>{
    clearInterval(calibrationTimer);
    if($("calibration-panel").open){ refreshCalibration(); calibrationTimer=setInterval(refreshCalibration,1000); }
  });
  $("calibration-start-btn").onclick=async()=>{
    await withFeedback("准备标定采集...","已开始采集，自动转向关闭",()=>api("/api/stereo-calibration/start",{method:"POST",body:JSON.stringify({square_mm:Number($("calibration-square-mm").value),scale_confirmed:$("calibration-scale-confirmed").checked})}));
    refreshCalibration();
  };
  $("calibration-stop-btn").onclick=async()=>{ await api("/api/stereo-calibration/stop",{method:"POST"}); refreshCalibration(); };
  $("calibration-resume-btn").onclick=async()=>{
    await withFeedback("继续采集...","已继续本组采集，自动转向关闭",()=>api("/api/stereo-calibration/resume",{method:"POST"}));
    refreshCalibration();
  };
  $("calibration-solve-btn").onclick=async()=>{
    try { await withFeedback("计算并检查标定...","标定检查已完成",()=>api("/api/stereo-calibration/solve",{method:"POST"})); }
    finally { refreshCalibration(); }
  };
});
