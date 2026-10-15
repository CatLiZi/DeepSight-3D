# 定位机器人算法与控制逻辑说明

本文整理当前 demo 已经实际使用的算法、融合逻辑和舵机控制逻辑。目标行为是：优先转向“正在朝机器人说话的人”；如果画面中没有人，则允许仅根据声源方向做 yaw 粗搜索；pitch 不由双麦音频估计，主要由视觉目标控制。

## 1. 总体数据流

```text
双声道麦克风 -> 多频段 VAD/SRP-PHAT 前方半平面搜索 -> 左右方向候选
USB Camera -> 人脸/嘴部运动/正脸程度 -> active speaker 候选
音频 + 视觉 -> 状态机 -> yaw/pitch 目标
目标姿态 -> deadband/限幅/发令间隔/motor guard -> 舵机
```

当前控制不是“听到声音立即持续转”，而是分两类：

- 有视觉 active speaker：视觉中心误差持续控制 yaw/pitch。
- 没有视觉目标：音频只做 yaw 粗转，pitch 保持中心。

## 2. 音频算法

相关文件：

- `src/locator_demo/audio_device.py`
- `src/locator_demo/audio.py`

### 2.1 音频采集

音频通过 ffmpeg DirectShow 读取：

```text
ffmpeg -f dshow -i audio=<device> -ac 2 -ar 16000 -f s16le -
```

当前按双声道、16 kHz、40 ms 一帧处理。每帧拆成 left/right 两路浮点采样。

### 2.2 能量估计

每帧计算左右声道 RMS：

```text
left_level = rms(left)
right_level = rms(right)
energy = max(left_level, right_level) * 12.0
```

`energy` 是归一化后的响度指标，用于判断当前帧是否足够强。当前音频分类默认最低能量门槛约为 `0.08~0.12`。

### 2.3 轻量 VAD / 语音置信度

当前没有硬依赖 Silero，而是用了一个轻量规则 VAD：

1. 把左右声道混成 mono。
2. 做 FFT。
3. 计算多段语音频带能量比例：

```text
voiced_band = 80 Hz ~ 250 Hz
speech_band = 250 Hz ~ 1000 Hz
clarity_band = 1000 Hz ~ 4000 Hz
```

4. 计算低频噪声比例：

```text
rumble_band = 20 Hz ~ 80 Hz
low_motor_band = 80 Hz ~ 180 Hz
```

5. 用 adaptive noise floor 估计背景底噪。

输出：

- `speech_confidence`: 当前帧像不像语音。
- `noise_state`: `quiet`、`noise`、`speech_like`、`voiced_speech`、`fan_or_motor`、`low_band_noise` 等。

如果 80~250 Hz 低基频段有能量，同时 250 Hz 以上有谐波/辅音能量，会被认为是低音人声，不再简单压掉。如果 20~80 Hz rumble 很强，或者 80~180 Hz 稳定低频很强但高频语音线索不足，则会被认为更像风扇/电机声，语音置信度会被压低。

### 2.4 近场左右能量差规则

如果某人离某一侧麦克风很近，两个声道 RMS 差异会很明显。代码先用一个快速规则处理这种情况：

```text
balance = (left_level - right_level) / (left_level + right_level)
```

触发条件大致是：

- `energy >= 0.08`
- `speech_confidence >= 0.25`
- `abs(balance) >= 0.30`

如果左声道强很多，则给一个左侧 TDOA；右声道强很多，则给一个右侧 TDOA。这个规则比 GCC-PHAT 更适合近距离、单侧音量差特别明显的情况。

### 2.5 多频段 SRP-PHAT 前方半平面定位

如果左右能量差规则没有触发，则使用 SRP-PHAT 风格的前方半平面搜索。核心仍然是 PHAT 相位加权互相关，但不再直接取单个 TDOA 峰，而是在当前头部朝向的前方 180° 内扫角度：

```text
R = FFT(left) * conj(FFT(right))
R_phat = R / abs(R)
theta in [-90°, +90°]
expected_tdoa(theta) = sin(theta) * mic_spacing / speed_of_sound
score(theta) = PHAT_corr(expected_tdoa(theta))
azimuth = argmax(score)
```

当前输出：

- `tdoa_s`: 两个麦克风之间的到达时间差。
- `peak_ratio`: 主峰和次峰的比值。
- `doa_confidence`: 根据 `peak_ratio` 得到的方向置信度。
- `azimuth_deg`: 当前头部坐标系下的局部声源角度，正值表示左侧，负值表示右侧。

搜索只覆盖前方半平面，这是因为左右双麦无法可靠区分前后声源。这个约束能避免系统把后方或强混响声错误解释成需要无限追转。

定位前还会做轻量预处理：

- DC removal
- Hann window
- pre-emphasis

并按频段组合 PHAT 响应：

```text
80~250 Hz     低基频/低音人声，保留但权重较低
250~1000 Hz   主语音区域
1000~4000 Hz  辅音和更清楚的定位线索，权重较高
```

如果 numpy 不可用，会 fallback 到有限窗口互相关。

### 2.6 TDOA 到左右方向

当前硬件假设是左右水平双麦，因此音频只估计 yaw/azimuth，不估计 pitch。

TDOA 会被换算成粗 azimuth：

```text
azimuth = asin(tdoa * speed_of_sound / mic_spacing)
```

当前默认：

- 声速：`343 m/s`
- 麦克风间距：`0.12 m`

方向分类：

- `abs(tdoa_s) <= 0.00006` -> `center`
- `tdoa_s > 0` -> `left`
- `tdoa_s < 0` -> `right`
- 能量/语音置信度不足 -> `unknown`

### 2.7 音频平滑

音频方向不是单帧决定，而是进入 `AudioDirectionSmoother`：

- 窗口大小：4 帧。
- 至少需要 2 个有效方向样本。
- 候选样本需要 `confidence >= 0.35`。
- 左右方向分数差距不足时输出 `unknown`。

这能减少左右来回跳，但也会带来一点确认延迟。

### 2.8 声道映射

如果麦克风左右装反，dashboard 的 audio channel swap 会执行：

```text
left <-> right
tdoa_s *= -1
azimuth_deg *= -1
```

### 2.9 电机声音抑制

每次发舵机命令后，系统进入 motor guard：

```text
guard_until = now + move_time_ms + motor_guard_ms
```

guard 期间：

- `direction` 被强制设为 `unknown`
- `confidence` 降低
- `noise_state = motor_guard`
- `motor_suppressed = true`

这样可以避免舵机转动声触发二次追转。

### 2.10 VAD 开关

dashboard 关闭 VAD 时，不是完全关闭音频，而是放宽语音门控：

- 用当前 `tdoa_s` 重新分类方向。
- 抬高 `speech_confidence` 到至少 `0.6`。
- `noise_state = vad_disabled`。

这个模式适合现场调试麦克风灵敏度，但误转风险会更高。

## 3. 视觉算法

相关文件：

- `src/locator_demo/camera.py`
- `src/locator_demo/vision.py`

### 3.1 相机采集

相机通过 ffmpeg DirectShow 采集 MJPEG，再输出 JPEG pipe 给 Python：

```text
ffmpeg -f dshow -video_size <size> -framerate <fps> -vcodec mjpeg -i video=<device> ...
```

当前常用配置：

- USB Camera
- `1280x800`
- 不裁切
- 前端显示链路约 `fps=15`

如果是 `2560x800` 双目/宽画面，会走左半裁切，当前裁切路径较慢，默认不建议。

### 3.2 人脸与嘴部运动

当前视觉有两层：

1. 如果安装了 `mediapipe`，优先使用 MediaPipe FaceMesh。
2. 如果没有安装，fallback 到 OpenCV Haar face detector。

MediaPipe 路径会估计：

- 人脸 bbox。
- `face_yaw_deg`: 粗略脸部朝向。
- `frontal_score`: 越正脸越高。
- 嘴部开合变化：用上下唇 landmark 差异计算 `mouth_motion_score`。

Haar fallback 路径无法精确拿唇部关键点，所以用脸框下半部分 ROI 的帧间差分估计嘴部运动：

```text
mouth_roi = lower face region
mouth_motion = mean(abs(current_roi - previous_roi)) / 18
```

### 3.3 active speaker score

每个人脸会变成一个 `FaceTarget`：

- `bbox`
- `center`
- `face_yaw_deg`
- `frontal_score`
- `mouth_motion_score`
- `mouth_audio_sync_score`
- `active_speaker_score`

当前 active speaker score 是轻量启发式，不是 TalkNet：

```text
MediaPipe: 0.25 * frontal_score + 0.60 * mouth_motion + 0.05
Haar:      0.25 * frontal_score + 0.65 * mouth_motion + 0.08 * face_score
```

也就是说，嘴部运动权重最高；静止正脸不会轻易成为可控制目标。

### 3.4 音频方向辅助选脸

当画面中有多个人脸时，`choose_active_speaker_target` 会选择 active speaker score 最高者。

如果音频方向是 left/right，还会给画面左右半区一个小偏置：

- 音频 left，脸在画面左半边：加分。
- 音频 right，脸在画面右半边：加分。
- 不匹配：减分。

这个偏置只辅助选择，不会替代嘴部/正脸证据。

## 4. 融合状态机

相关文件：

- `src/locator_demo/orchestrator.py`

当前状态：

```text
LISTENING
  -> AUDIO_CANDIDATE
  -> SEEK_VISUAL
  -> TRACK_SPEAKER
  -> HOLD
  -> LISTENING
```

### 4.1 audio_ready 条件

音频候选必须满足：

- 不在 motor guard。
- 方向不是 `unknown` 或 `center`。
- `confidence >= 0.30`
- `speech_confidence >= 0.25`
- `doa_confidence >= 0.12`

并且默认需要连续 2 次同方向命中：

```text
required_audio_hits = 2
```

第一次命中进入 `AUDIO_CANDIDATE`，第二次同方向命中进入 `SEEK_VISUAL`。

### 4.2 target_confirmed 条件

视觉目标能驱动持续跟踪，需要满足：

- `speaker_score >= 0.35`
- `frontal_score >= 0.25`
- `mouth_motion_score` 或 `mouth_audio_sync_score >= 0.25`
- 如果最近 1.5 秒内有音频方向，可以放宽持续确认；否则需要更明确的嘴部运动。

这条规则的意义是：画面里有人脸不等于要跟踪，必须像是在说话。

### 4.3 状态转移

核心逻辑：

- `LISTENING`: 等待可靠音频或可靠视觉 speaker。
- `AUDIO_CANDIDATE`: 听到一次可靠方向，但还要继续确认。
- `SEEK_VISUAL`: 连续听到同方向，开始用 yaw 粗转把 speaker 带入视野。
- `TRACK_SPEAKER`: 视觉确认 active speaker 后，用视觉中心误差持续跟踪。
- `HOLD`: 目标短暂丢失后等待，避免一丢脸就乱转。
- `HOLD` 中如果又听到可靠音频，会立刻回到搜索，不硬等。

## 5. 舵机控制逻辑

相关文件：

- `src/locator_demo/head.py`
- `src/locator_demo/web/app.py`

### 5.1 坐标和安全范围

默认舵机中心：

```text
yaw center = 1500
pitch center = 1500
```

安全范围：

```text
1200 <= yaw/pitch <= 1800
```

所有目标在发送前都会 clamp 到安全范围。

### 5.2 方向映射

不同机器人装配方向可能不同，所以 dashboard 里有：

- `yaw_left_sign`
- `pitch_up_sign`
- `manual_step`

例如：

```text
left:  yaw += yaw_left_sign * step
right: yaw -= yaw_left_sign * step
up:    pitch += pitch_up_sign * step
down:  pitch -= pitch_up_sign * step
```

如果发现左说话却向右转，应优先检查：

- audio channel swap
- yaw_left_sign

### 5.3 音频粗转

音频只控制 yaw，不控制 pitch。

当前音频控制已经从“中心绝对目标”改为“当前头部坐标系下的相对小步搜索”：

```text
local azimuth > deadband  -> 从当前 yaw 往左小步
local azimuth < -deadband -> 从当前 yaw 往右小步
abs(local azimuth) <= deadband -> yaw 不动，pitch 回中心
```

具体加减方向取决于 `yaw_left_sign`。

每轮声源搜索开始时会记录：

```text
seek_origin_yaw = current_yaw
```

之后只允许在这个起点附近的前方搜索窗口内移动：

```text
seek_min_yaw = seek_origin_yaw - audio_seek_window
seek_max_yaw = seek_origin_yaw + audio_seek_window
```

这样即使持续听到左/右，也不会无限转过头。

音频粗转触发条件：

- 状态机进入 `SEEK_VISUAL`；或者
- 画面中没有人脸目标，且音频 `audio_ready = true`。

这就是“画面没人时，仍然根据声源定位转向”的逻辑。

为了防止越转越偏，搜索还会记录上一轮局部 azimuth。如果同方向移动后局部角度没有收敛，或者左右方向在很小角度内反转，就会停止追加移动，等待视觉确认或回到 listening/hold。

音频粗搜索比视觉跟踪更积极，使用独立参数：

```text
stable: audio move time 350 ms, max step 150, min step 60, window 260
fast:   audio move time 250 ms, max step 220, min step 80, window 340
```

也就是说，视觉跟踪仍然平滑；找声源时会更快把头转到大致方向。

### 5.4 视觉跟踪

视觉跟踪只在 `target_confirmed = true` 时驱动。

步骤：

1. 计算目标中心相对画面中心的归一化误差：

```text
error_x = (target_center_x - frame_center_x) / half_width
error_y = (target_center_y - frame_center_y) / half_height
```

2. 对误差做 EMA 平滑：

```text
smoothed = alpha * current + (1 - alpha) * previous
```

3. 用 deadband 防小幅抖动：

```text
abs(error_x) <= yaw_deadband -> yaw 不动
abs(error_y) <= pitch_deadband -> pitch 不动
```

4. 误差转换成舵机增量：

```text
yaw_delta   = -yaw_left_sign   * error_x * 200
pitch_delta = -pitch_up_sign   * error_y * 200
```

5. 限制单次最大变化，再 clamp 到安全范围。

### 5.5 控制 profile

dashboard 有两档：

稳健 `stable`：

- 发令间隔：`0.35 s`
- yaw deadband：`0.08`
- pitch deadband：`0.12`
- EMA alpha：`0.35`
- move time：`700 ms`
- 单次 yaw 最大变化：`120`
- 单次 pitch 最大变化：`60`

快速 `fast`：

- 发令间隔：`0.18 s`
- yaw deadband：`0.06`
- pitch deadband：`0.10`
- EMA alpha：`0.60`
- move time：`450 ms`
- 单次 yaw 最大变化：`120`
- 单次 pitch 最大变化：`60`

稳健模式误转更少，快速模式响应更快但更容易被噪声影响。

### 5.6 motor guard

每次发出舵机运动后都会进入 motor guard：

```text
motor_guard = move_time_ms + motor_guard_ms
```

默认 `motor_guard_ms = 250`。

在这段时间里，音频不会单独触发新的转向。这样避免“舵机自己转动的声音又被麦克风听到，造成二次追转”。

## 6. Dashboard 开关对逻辑的影响

### 6.1 audio_enabled

关闭后：

- 不使用音频进入状态机。
- 不做音频粗转。

### 6.2 visual_enabled

关闭后：

- 仍然可以显示相机预览和检测框。
- 不使用视觉目标控制舵机。
- 如果 audio 开着，则音频可以单独粗转 yaw，pitch 自动保持中心。

### 6.3 camera_enabled

关闭后：

- 停止相机读取。
- 清空画面和视觉目标。
- 如果 audio 开着，系统会按“画面没人”逻辑用音频粗转。

### 6.4 active_speaker_enabled

开启时：

- 只有 active speaker 候选能驱动视觉跟踪。
- 普通人脸可以显示，但不会直接控制舵机。

关闭时：

- 回到更传统的人脸跟踪逻辑：检测到的人脸会被视作可跟踪目标。
- 适合调试相机/舵机，不适合多人说话场景。

### 6.5 vad_enabled

开启时：

- 使用语音置信度过滤风扇/电机/背景声。

关闭时：

- 放宽语音门控，按 TDOA 重新分类方向。
- 适合调试麦克风灵敏度。
- 误转风险更高。

## 7. 现场调试时看哪些指标

接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8010/api/state | ConvertTo-Json -Depth 5
```

重点看：

- `audio.energy`: 麦克风响度。
- `audio.speech_confidence`: 当前声音像不像语音。
- `audio.doa_confidence`: SRP-PHAT 前方半平面方向峰是否清楚。
- `audio.peak_ratio`: 主峰和次峰差距。
- `audio.noise_state`: 噪声状态。
- `audio.motor_suppressed`: 是否正在 motor guard。
- `audio.direction`: 当前分类方向。
- `mode`: 当前状态机状态。
- `visual.targets`: 当前检测到的全部人脸。
- `visual.target.active_speaker_score`: 当前显示目标的说话人分数。

典型判断：

- `energy` 很低：系统麦克风增益、设备选择或麦克风硬件问题。
- `speech_confidence` 低：VAD 认为不像语音，可以先关闭 VAD 做排查。
- `doa_confidence` 低：双麦定位峰不清楚，可能是混响、麦距、声源太近/太远、左右通道不是同一设备。
- `motor_suppressed = true`：舵机刚动完，音频暂时被抑制，这是正常保护。
- `visual.target` 有人但 `target_confirmed = false`：显示到了人脸，但嘴部/正脸/同步证据不足，不会持续跟踪。

## 8. 当前限制

- 双麦只估计左右 yaw，不估计 pitch。
- 当前 active speaker 是轻量规则，不是完整 TalkNet/ASD。
- MediaPipe 是可选依赖；没安装时使用 Haar + ROI 嘴部运动估计。
- 嘴部运动估计依赖画面稳定，镜头抖动或曝光变化会影响分数。
- 音频 DOA 在强混响、双麦太近、电脑风扇很强时仍可能不稳定。
