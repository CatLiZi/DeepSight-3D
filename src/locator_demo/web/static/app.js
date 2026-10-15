const state = {
  connected: false,
  eventsSeen: new Set(),
  lastFrame: "",
  dshowDevices: [],
  axisLimits: {
    yaw_min: 1200,
    yaw_center: 1500,
    yaw_max: 1800,
    pitch_min: 1200,
    pitch_center: 1500,
    pitch_max: 1800,
  },
};

const $ = (id) => document.getElementById(id);

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
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  });
}

function setFeedback(message, kind = "ok") {
  const chip = $("action-feedback");
  if (!chip) return;
  chip.textContent = message;
  chip.classList.remove("is-busy", "is-ok", "is-error");
  chip.classList.add(kind === "busy" ? "is-busy" : kind === "error" ? "is-error" : "is-ok");
  clearTimeout(feedbackTimer);
  if (kind !== "busy") {
    feedbackTimer = setTimeout(() => {
      chip.textContent = "就绪";
      chip.classList.remove("is-busy", "is-ok", "is-error");
    }, 2600);
  }
}

async function withFeedback(busyText, doneText, action) {
  setFeedback(busyText, "busy");
  try {
    const result = await action();
    setFeedback(doneText, "ok");
    return result;
  } catch (error) {
    setFeedback(`失败: ${error.message}`, "error");
    throw error;
  }
}

function drawDirections(direction, confidence) {
  const canvas = $("direction-canvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#f8fafc";
  ctx.fillRect(0, 0, w, h);

  const sectors = [
    ["left_back", -160, -105],
    ["left", -105, -75],
    ["left_front", -75, -20],
    ["right_front", 20, 75],
    ["right", 75, 105],
    ["right_back", 105, 160],
  ];
  const cx = w / 2;
  const cy = h * 0.82;
  const radius = Math.min(w * 0.45, h * 0.75);

  for (const [name, a0, a1] of sectors) {
    const active = name === direction || (direction === "left" && name === "left") || (direction === "right" && name === "right");
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, radius, (Math.PI * (a0 - 90)) / 180, (Math.PI * (a1 - 90)) / 180);
    ctx.closePath();
    ctx.fillStyle = active ? `rgba(15,118,110,${0.35 + confidence * 0.45})` : "#e8eef5";
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.stroke();

    const mid = ((a0 + a1) / 2 - 90) * Math.PI / 180;
    const tx = cx + Math.cos(mid) * radius * 0.62;
    const ty = cy + Math.sin(mid) * radius * 0.62;
    ctx.fillStyle = active ? "#064e3b" : "#52606d";
    ctx.font = "14px Segoe UI";
    ctx.textAlign = "center";
    ctx.fillText(name.replace("_", " "), tx, ty);
  }

  ctx.beginPath();
  ctx.arc(cx, cy, 18, 0, Math.PI * 2);
  ctx.fillStyle = "#17202a";
  ctx.fill();
}

function drawOverlay(visual) {
  const canvas = $("overlay");
  const frameWidth = visual.frame_width || 640;
  const frameHeight = visual.frame_height || 400;
  if (canvas.width !== frameWidth || canvas.height !== frameHeight) {
    canvas.width = frameWidth;
    canvas.height = frameHeight;
    $("video-wrap").style.aspectRatio = `${frameWidth} / ${frameHeight}`;
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "rgba(148,163,184,0.35)";
  ctx.setLineDash([4, 8]);
  ctx.strokeRect(canvas.width * 0.42, canvas.height * 0.38, canvas.width * 0.16, canvas.height * 0.24);
  ctx.setLineDash([]);
  const sx = canvas.width / frameWidth;
  const sy = canvas.height / frameHeight;
  const aimX = canvas.width * (0.5 + (visual.processing?.target_offset_x_norm ?? 0));
  const aimY = canvas.height * (0.5 + (visual.processing?.target_offset_y_norm ?? 0));
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
    ctx.font = "16px Segoe UI";
    const tag = selected ? "TARGET" : "FACE";
    ctx.fillText(`${tag} ${((t.x2 - t.x1) * (t.y2 - t.y1)).toFixed(0)}`, t.x1 * sx + 4, t.y1 * sy + 18);
    ctx.strokeStyle = borderColor;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(aimX, aimY);
    ctx.stroke();
    ctx.fillStyle = boxColor;
    ctx.beginPath();
    ctx.arc(cx, cy, selected ? 6 : 5, 0, Math.PI * 2);
    ctx.fill();
  };
  const targets = visual.targets || [];
  const selectedTarget = targets.reduce((best, current) => {
    if (!best) return current;
    const bestArea = (best.x2 - best.x1) * (best.y2 - best.y1);
    const curArea = (current.x2 - current.x1) * (current.y2 - current.y1);
    return curArea > bestArea ? current : best;
  }, null);
  if (targets.length > 0) {
    for (const target of targets) drawFaceBox(target, target === selectedTarget);
  } else if (visual.target) {
    drawFaceBox(visual.target, true);
  }
  ctx.lineWidth = 4;
  ctx.strokeStyle = "rgba(15,23,42,0.85)";
  ctx.beginPath();
  ctx.arc(aimX, aimY, 12, 0, Math.PI * 2);
  ctx.moveTo(aimX - 18, aimY);
  ctx.lineTo(aimX + 18, aimY);
  ctx.moveTo(aimX, aimY - 18);
  ctx.lineTo(aimX, aimY + 18);
  ctx.stroke();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#facc15";
  ctx.beginPath();
  ctx.arc(aimX, aimY, 12, 0, Math.PI * 2);
  ctx.moveTo(aimX - 18, aimY);
  ctx.lineTo(aimX + 18, aimY);
  ctx.moveTo(aimX, aimY - 18);
  ctx.lineTo(aimX, aimY + 18);
  ctx.stroke();
}

function render(snapshot) {
  state.connected = true;
  $("status-chip").textContent = "已连接";
  $("running-label").textContent = String(snapshot.running);
  $("mode-label").textContent = snapshot.mode;
  applyFeatureConfig(snapshot.features);
  $("audio-direction").textContent = snapshot.audio.direction;
  $("audio-confidence").textContent = `置信度 ${(snapshot.audio.confidence * 100).toFixed(0)}%`;
  $("audio-tdoa").textContent = snapshot.audio.tdoa_s.toFixed(6);
  $("audio-energy").textContent = snapshot.audio.energy.toFixed(2);
  $("audio-device").textContent = `${snapshot.audio.status.device_name}: ${snapshot.audio.status.message}`;
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
  drawDirections(snapshot.audio.direction, snapshot.audio.confidence);
  drawOverlay(snapshot.visual);
  if (!cameraEnabled) {
    $("preview").removeAttribute("src");
    state.lastFrame = "";
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
  ensureCameraOption(camera.device_name);
  $("camera-device-select").value = camera.device_name || "";
  $("camera-video-size-input").value = camera.video_size || "";
  $("camera-fps-input").value = camera.fps ?? "";
  $("camera-output-width-input").value = camera.output_width ?? 640;
  $("face-detector-backend-select").value = camera.face_detector_backend || "mediapipe";
  $("scrfd-model-path-input").value = camera.scrfd_model_path || "";
  $("scrfd-threshold-input").value = camera.scrfd_threshold ?? 0.35;
  $("scrfd-input-size-input").value = camera.scrfd_input_size ?? 640;
  $("camera-crop-select").value = camera.crop_left_half === null || camera.crop_left_half === undefined
    ? ""
    : String(Boolean(camera.crop_left_half));
}

function applyAudioConfig(audioConfig) {
  if (!audioConfig) return;
  $("audio-channel-map").value = audioConfig.swap_channels ? "swap" : "normal";
}

function applyControlConfig(controlConfig) {
  if (!controlConfig) return;
  $("control-profile-select").value = controlConfig.control_profile || "stable";
  $("motor-guard-input").value = controlConfig.motor_guard_ms ?? 250;
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, Number(value)));
}

function applyAxisLimitsConfig(axisLimits) {
  if (!axisLimits) return;
  state.axisLimits = {
    yaw_min: axisLimits.yaw_min ?? 1200,
    yaw_center: axisLimits.yaw_center ?? 1500,
    yaw_max: axisLimits.yaw_max ?? 1800,
    pitch_min: axisLimits.pitch_min ?? 1200,
    pitch_center: axisLimits.pitch_center ?? 1500,
    pitch_max: axisLimits.pitch_max ?? 1800,
  };
  $("pitch-min-input").value = state.axisLimits.pitch_min;
  $("pitch-center-input").value = state.axisLimits.pitch_center;
  $("pitch-max-input").value = state.axisLimits.pitch_max;
  $("yaw-input").min = state.axisLimits.yaw_min;
  $("yaw-input").max = state.axisLimits.yaw_max;
  $("pitch-input").min = state.axisLimits.pitch_min;
  $("pitch-input").max = state.axisLimits.pitch_max;
  $("yaw-input").value = clamp($("yaw-input").value, state.axisLimits.yaw_min, state.axisLimits.yaw_max);
  $("pitch-input").value = clamp($("pitch-input").value, state.axisLimits.pitch_min, state.axisLimits.pitch_max);
}

function applyProcessingConfig(audioProcessing, visionProcessing) {
  if (audioProcessing) {
    $("vad-enabled-input").checked = audioProcessing.vad_enabled !== false;
    $("audio-threshold-input").value = audioProcessing.audio_confidence_threshold ?? 0.20;
    $("speech-threshold-input").value = audioProcessing.speech_confidence_threshold ?? 0.15;
    $("doa-threshold-input").value = audioProcessing.doa_confidence_threshold ?? 0.05;
    $("audio-hits-input").value = audioProcessing.required_audio_hits ?? 1;
  }
  if (visionProcessing) {
    $("active-speaker-input").checked = visionProcessing.active_speaker_enabled !== false;
    $("tracking-strategy-select").value = visionProcessing.tracking_strategy || "classic_audio_first";
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
        output_width: outputWidthValue === "" ? 640 : Number(outputWidthValue),
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
  const session = await withFeedback("保存声道...", "声道配置已保存", () =>
    api("/api/config/audio", {
      method: "POST",
      body: JSON.stringify({
        swap_channels: $("audio-channel-map").value === "swap",
      }),
    })
  );
  applyAudioConfig(session.audio_config);
  const audioProcessing = await api("/api/config/audio-processing", {
    method: "POST",
    body: JSON.stringify({
      vad_enabled: $("vad-enabled-input").checked,
      audio_confidence_threshold: Number($("audio-threshold-input").value || 0.20),
      speech_confidence_threshold: Number($("speech-threshold-input").value || 0.15),
      doa_confidence_threshold: Number($("doa-threshold-input").value || 0.05),
      required_audio_hits: Number($("audio-hits-input").value || 1),
      denoise_enabled: true,
      denoise_dry_mix: 0.15,
      denoise_output_dir: "denoise_output",
      recording_enabled: true,
    }),
  });
  applyProcessingConfig(audioProcessing.audio_processing || audioProcessing, null);
}

async function saveVisionProcessingConfig() {
  const visionProcessing = await withFeedback("保存说话人...", "说话人配置已保存", () =>
    api("/api/config/vision-processing", {
      method: "POST",
      body: JSON.stringify({
        active_speaker_enabled: $("active-speaker-input").checked,
        tracking_strategy: $("tracking-strategy-select").value || "classic_audio_first",
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
        yaw_min: state.axisLimits.yaw_min,
        yaw_center: state.axisLimits.yaw_center,
        yaw_max: state.axisLimits.yaw_max,
        pitch_min: Number($("pitch-min-input").value || 1200),
        pitch_center: Number($("pitch-center-input").value || 1500),
        pitch_max: Number($("pitch-max-input").value || 1800),
      }),
    })
  );
  applyAxisLimitsConfig(session.axis_limits);
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
  $("direction-config-status").textContent = "方向配置已永久保存";
}

window.addEventListener("DOMContentLoaded", async () => {
  drawDirections("unknown", 0);
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
    applyControlConfig(session.control);
    applyAxisLimitsConfig(session.axis_limits || session.servo?.axis_limits);
    applyProcessingConfig(session.audio_processing, session.vision_processing);
  } catch (error) {
    $("status-chip").textContent = "后端不可用";
  }

  $("start-btn").onclick = async () => {
    const result = await withFeedback("启动中...", "Demo 已开始", () => api("/api/demo/start", { method: "POST" }));
    $("running-label").textContent = String(result.running);
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
  $("save-camera-config-btn").onclick = saveCameraConfig;
  $("camera-fps-input").onblur = saveCameraConfig;
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
