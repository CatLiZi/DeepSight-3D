# R52阶段算法架构与模块说明

核对日期：2026-11-05。算法源码基线：`5b47c00dcdf43a913c95d89e35f2bf6aab99b88b`，运行接口版本 `R52`，录制功能版本 `D1`。后续仅更新文档的提交不改变该算法基线。

本文用于更新README、技术汇报和算法框架图。它描述已经实现的链路；历史R12/R47/R51文档保留各阶段记录，不应直接当作整套现行架构。[交给制图agent的完整提示词](AGENT_PROMPT_FRAMEWORK_R52_CN.md) · [本阶段HTML说明](report/primary-modes-r52.html) · [发布验证](VALIDATION_R52.md)

## 1. 本阶段的核心变化

R52将“谁决定当前转向”明确拆成两个互斥主模式。双目和四麦仍可持续采集；互斥的是控制优先级，并非只允许一种传感器工作。

| 模块/职责 | 旧框架容易表达成 | R52实际行为 |
| --- | --- | --- |
| 模式选择 | 听声与视觉同时勾选，最终仍由视觉锁定占有控制 | `audio_primary`与`visual_priority`互斥；`preview`仅预览 |
| 听声转头 | 声音仅给可见人脸加分；没有匹配人脸就难以转向 | 可靠声源先建立有限转向目标；接近后只用对应人脸修正 |
| 视觉辅助 | 人脸一旦锁定就持续挡住新声音 | 新确认声源可以打断旧人脸跟随，撤销其未完成轨迹 |
| 声音中断 | 当前方向变成`unknown`即取消转向 | 保留已授权的有限目标，完成本次转向；不把旧角度反复累加 |
| 数据与动作一致性 | 后台旧画面可能继续选回旧目标 | 声源编号、控制轮次、时间戳和模式代次共同限制接管 |
| 演示证据 | 只看网页当下显示 | 独立D1录制网页、四路PCM、回放声音与决策时间线 |

**本轮未重新训练神经网络，未修改视觉检测模型、双目标定、四麦采集参数、舵机行程及平滑轨迹限制。**

## 2. 模块图应包含的真实节点

下表的“输出”是模块间传递的信息，不表示每一步都在独立线程运行。

| 编号 | 模块（建议图中名称） | 输入 → 输出 | 源码依据 |
| --- | --- | --- | --- |
| V1 | 同帧双目采集 / Stereo capture | 一张3840×1080 MJPEG → 左右原始画面、帧号、采集时间戳 | [capture.py](../src/locator_demo/capture.py)、[stereo_vision.py](../src/locator_demo/stereo_vision.py) |
| V2 | 双目对齐与独立检测 / Alignment + detection | 两目画面 → SCRFD人脸框、多尺度补查、画面健康状态 | [stereo_vision.py](../src/locator_demo/stereo_vision.py)、[multiscale_faces.py](../src/locator_demo/multiscale_faces.py)、[stereo_health.py](../src/locator_demo/stereo_health.py) |
| V3 | 跨目关联与共同注视 / Association + fixation | 两目新鲜目标与可信纹理对应 → 暂定目标ID、每眼角度区间、共同注视误差 | [binocular.py](../src/locator_demo/binocular.py)、[stereo_geometry.py](../src/locator_demo/stereo_geometry.py)、[bearing.py](../src/locator_demo/bearing.py)、[fixation.py](../src/locator_demo/fixation.py) |
| A1 | 四路RAW采集 / Four-channel PCM | CH1–CH4、48kHz、16bit → 连续PCM与音频时钟 | [audio_device.py](../src/locator_demo/audio_device.py)、[array_audio.py](../src/locator_demo/array_audio.py) |
| A2 | 人声支持与连续性 / Speech evidence | 原始各通道 → 预训练Silero VAD证据、当前窗支持、受限上下文恢复 | [speech_vad.py](../src/locator_demo/speech_vad.py)、[speech_continuity.py](../src/locator_demo/speech_continuity.py)、[speech_recovery.py](../src/locator_demo/speech_recovery.py) |
| A3 | 分对时差与方向 / Pairwise TDOA | 耳部对和身体对各自PCM → 频段加权GCC时差、竞争峰、方向及不确定度 | [ear_localization.py](../src/locator_demo/ear_localization.py)、[spectral_delay.py](../src/locator_demo/spectral_delay.py)、[band_delay.py](../src/locator_demo/band_delay.py)、[delay_track.py](../src/locator_demo/delay_track.py) |
| A4 | 姿态对齐与条件融合 / Pose-aware fusion | 两对方向、共同频谱支持、可信姿态 → 头部坐标下的方向或回退结果 | [pair_fusion.py](../src/locator_demo/pair_fusion.py)、[array_pose.py](../src/locator_demo/array_pose.py) |
| A5 | 声学证据准入 / Audio admission | 人声、方向质量、时间一致性、运动情境 → 当前可用方向及拒绝原因 | [audio_policy.py](../src/locator_demo/audio_policy.py)、[audio_motion.py](../src/locator_demo/audio_motion.py) |
| S1 | 核心声源与位置记忆 / Dominant sound lock | 新鲜、可靠的方向序列与头部姿态 → 核心声源ID、累计分值、有限Yaw目标 | [dominant_sound.py](../src/locator_demo/dominant_sound.py) |
| P1 | 互斥主模式 / Primary-mode arbitration | 用户选择 → 声音主导、视觉主导或预览 | [settings.py](../src/locator_demo/settings.py)的`FeatureConfig`、[web/app.py](../src/locator_demo/web/app.py) |
| P2a | 声音主导、视觉修正 / Audio primary | 已确认核心目标 + 视觉候选 → 转向、匹配人脸、可被打断的视觉辅助 | [audio_primary.py](../src/locator_demo/audio_primary.py)的`AudioPrimarySelector` |
| P2b | 视觉主导、声音辅助 / Visual primary | 新鲜视觉候选 + 可用声音范围 → 稳定可见目标及有条件换人 | [visual_priority.py](../src/locator_demo/visual_priority.py)、[av_selector.py](../src/locator_demo/av_selector.py) |
| C1 | 自适应视觉纠偏 / Image-error servoing | 目标误差、图像位移速度、当前姿态 → 有界Yaw/Pitch增量及动作时长 | [visual_servo.py](../src/locator_demo/visual_servo.py)、[visual_trigger.py](../src/locator_demo/visual_trigger.py) |
| C2 | 有限目标与平滑执行 / Bounded motion | 当前模式的合法目标 → 限位检查、50Hz五次轨迹、串口位置指令 | [head.py](../src/locator_demo/head.py)、[motion.py](../src/locator_demo/motion.py)、[servo_bus.py](../src/locator_demo/servo_bus.py)、[web/app.py](../src/locator_demo/web/app.py) |
| D1 | 本机演示录制 / Local demo recorder | 浏览器选择的标签页 + 已有RAW历史 + 状态事件 → MP4/WAV/WebM/JSONL/ZIP | [demo_recording.py](../src/locator_demo/demo_recording.py)、[web/demo_recording.py](../src/locator_demo/web/demo_recording.py)、[demo_recording.js](../src/locator_demo/web/static/demo_recording.js) |

V2与A2包含预训练模型推理；A3/A4、目标评分和控制主要是信号处理、几何与规则状态逻辑。**不要把整套系统画成新训练的端到端视听神经网络。**

## 3. 数据连接与反馈连接

主图可分成两条并行感知线：

- 视觉：V1 → V2 → V3 → P2a/P2b。匹配不足时保留可信单眼观察，不强行合并两个不同人；共同注视点分别投影到每只眼睛，正常水平视差保留。
- 听觉：A1 → A2/A3 → A4 → A5 → S1。A2的人声证据参与方向可靠性判断；S1的位置记忆与A5当前可用方向应画成不同输出。
- 决策：P1只选择P2a或P2b。两条模式不会同时向电机竞争下发。P2a可先把S1的有限Yaw目标交给C2，获得匹配脸后才使用C1；P2b以当前视觉目标驱动C1。
- 反馈：C2的实测位置和受反馈约束的轨迹历史回到A4用于头身坐标转换；当前姿态也用于S1构建绝对Yaw目标。新的视觉画面不断回到C1形成图像误差闭环。
- 展示：网页展示实时角度、1秒声音范围保留、核心目标、视觉候选、控制阶段与故障原因。显示保留不是新的声音证据。
- 录制：D1从采集和状态链路旁路取数据。**D1没有通往“开始运动”的箭头，不另开第二个麦克风。**

控制编排约30Hz，平滑指令轨迹配置50Hz，周期位置监测约1Hz；视觉推理、相机采集和音频回调各有自己的时钟。不能将50Hz标成实测反馈频率，也不能把请求60fps标成持续实收60fps。

## 4. 两种模式的实际契约

| 场景 | 听声转头：`audio_primary` | 视觉跟踪：`visual_priority` |
| --- | --- | --- |
| 尚无核心声音，但可见人脸 | 等待可靠声音，不凭最大人脸启动跟随 | 根据视觉质量、相对大小与连续性选择目标 |
| 声源不在当前人脸附近 | 已确认的声源有权接管有限转向 | 当前可信视觉目标优先；冲突声音不直接覆盖该目标 |
| 到达声音方向附近 | 从声音扇区内选实际可见人脸，连续确认后视觉微调 | 视觉目标本身提供跟随误差 |
| 原人脸仍可见，另一声源确认 | 撤销旧视觉目标的剩余动作，转向新声源 | 声音仅辅助可见候选排序，需通过既有换人保护 |
| 说话短暂停顿 | 已授权转向继续；已匹配的人脸可继续视觉跟随 | 继续视觉跟随 |
| 当前没有视觉目标 | 继续已确认的听声目标；到达后有界等待人脸 | 没有视觉拥有者时，现有逻辑仍可授权有限听声搜索，不保证始终有视觉目标 |
| 人脸或图像暂时丢失 | 不按旧像素位置追赶；短暂等同一目标恢复，长期丢失且无新声音不自动恢复旧搜索 | 保留既有视觉重发现与新鲜度保护 |
| `preview`/停止/故障 | 不启动自动动作 | 不启动自动动作 |

“声音主导”表示**已确认的可靠核心方向**拥有优先级，不是任意响声或一帧最大幅度拥有优先级。没有可靠角度时，可以显示检出人声，同时继续等待方向；这不是语音识别或语义理解。

### 听声模式的状态机

| 状态 | 进入条件与作用 | 后续 |
| --- | --- | --- |
| `waiting_sound` | 没有有效核心目标上下文 | 累积当前可靠方向；确认后进入转向 |
| `turn_to_sound` | 核心目标已确认，尚未接近方向 | 继续有限Yaw目标；不能被无关可见人脸拦住 |
| `acquire_face` | 估计残余角度绝对值≤12° | 默认声音范围±15°，以整脸角度区间匹配；至少2个新鲜视觉观察、持续≥0.1s确认 |
| `visual_refine` | 已关联到声源的人脸存在 | 用原自适应视觉控制修正Yaw/Pitch并跟随；新核心声源可打断 |
| `face_lost` | 已匹配脸短暂不可见 | 约0.7s内等待同一目标恢复，不输出旧像素进行纠偏 |
| `hold_sound` | 接近目标后约2s内没有获得对应脸，且没有当前声音续期 | 保持已确认方向、等待新证据；不无限期把后来路过者当作旧说话人 |

这些状态是当前代码的输出，不是新提出的规划。视觉获取后还需经过双目融合的控制视角确认（通常至少3帧且≥0.12s）；表中0.1s、12°等是局部门槛，**不是端到端延迟或实物角度精度保证**。

声源变化以`(acoustic_epoch, core_id)`区分。核心目标切换时立即撤销旧视觉控制，即使后台还返回上一轮画面；停止、模式变化、坐标或设备上下文变化也会清除旧控制意图。同一声源簇内，如果当前可靠声音与已选脸持续冲突，也可在多次新观察确认后解除视觉辅助。

## 5. 核心声源评分和坐标要画对

`DominantSoundLock`把方向归入空间候选簇，按近期有效讲话时长和方向质量累计。简化表达与源码一致：

```text
qᵢ = speechᵢ × sqrt(min(confidenceᵢ, doaᵢ) × stabilityᵢ)
     × exp(-0.5 × (spreadᵢ / 30°)²)

Sₖ = Σ Δtᵢ × qᵢ × exp(-(now - tᵢ) / 1.5s), i属于方向簇k、处于最近2.5s

yaw_goal = clip(yaw_at_audio + left_sign × ticks_per_degree × audio_angle,
                yaw_min, yaw_max)
```

重复数据包不重复加票。首次无竞争可靠声音有较短的获取分支；已有活跃目标时，新候选要满足更高分值、有效讲话时长和持续挑战条件。计分是工程优先级，不是声纹身份、语义重要性、距离或经过校准的概率。

当前配置的物理比例为`2000/270≈7.407407`刻度/度。完整目标与每段步幅分开保存，不能用旧追踪增益代替角度比例。声音暂停后继续的是有限目标位置，不是不断叠加过去的相对角。

四麦只有两对固定内部基线：

| 位置（小熊自身左右） | 通道 | 距离 |
| --- | --- | --- |
| 左耳 / 右耳，随头运动 | CH2 / CH3 | 约13.55cm |
| 左身体 / 右身体，固定 | CH1 / CH4 | 约7.55cm |

身体方向转换近似为`angle_head = angle_body - head_yaw`。其Yaw声学参考为1952，而控制中位是2000，两者用途不同。有效姿态需要可信位置反馈、与音频窗时间匹配的轨迹历史以及当前模型支持的姿态；例如俯仰偏离配置中位超过10刻度时，当前水平近似不输出身体角度融合。

姿态缺失或两对冲突时，不硬平均角度；保留可信耳部方向或显式说明不可用。共同频谱支持可以与身体角度融合分开存在。因此“CH1/CH4参与算法”不等于它们每一帧都必须具有非零方向权重。当前也没有把耳部与身体之间的四组交叉时差用于完整六对三维解算。

阈值来自`AudioDecisionPolicy`统一派生。示例中人声展示门槛0.45，方向/控制语音门槛0.5，身体融合DoA门槛至少0.25，受限恢复人声门槛至少0.7；它们用途不同，框架应标“分层证据准入”，不要标成一条通用概率。

## 6. 控制与记录边界

- 视觉纠偏结合当前图像误差和图像位移速度：偏差大时允许较大步幅和较短动作时长，接近中心时减小；保留死区、触发稳定性、速度/加速度/jerk限制。图像速度包含相机自身运动，不是真实人体速度测量。
- 两种模式共享同一底层50Hz平滑执行器。左右与上下不是两个互斥模式；合法视觉修正可以同时作用于Yaw/Pitch，听声水平转向保留当前Pitch。
- 当前公开配置Yaw为1500/2000/2500，Pitch为1255/1550/1855，均为软件位置刻度。舵机ID为0/2。串口号自动发现，不能把固定COM号画成算法必需条件。
- 自动启动先进行当前位置与运行条件检查；反馈错误、越界或停止请求仍撤销运动。本阶段没有删除这些保护来提高切换速度。
- D1录制最长60秒；网页原画面WebM与已有四路RAW保留，MP4使用耳部两路立体声，JSONL保存模式、目标、方向、控制状态及观察者标记。默认本机保存，不自动上传。
- 当前对话是A→B→A三段90词。A固定，B在自己发言时缓慢移动。计划提词编号不是算法识别出的真实说话人标注，软件时间对齐也不是曝光/ADC延迟标定。

## 7. 完成度和图中证据标签

| 项目 | 可以写 | 不可以写 |
| --- | --- | --- |
| R52模式逻辑 | 已实现，模拟执行器/反例回归通过 | 多人实物切换已全面验收 |
| 发布检查 | 904项Python、123项前端检查通过 | 准确率904/904、实物成功率100% |
| 本机加载 | 用户正常重启后只读确认R52、四路RAW、双目在线 | 该核对已经驱动舵机并验证跟随 |
| 听觉 | 有既有真实录音回放依据，四麦两对协同 | 可靠声学距离、全姿态三维定位、完全消除前后歧义 |
| 双目 | 对齐预览、独立检测、跨目关联与共同注视 | 正式米制深度/测距已经验收；每眼画面必须重合 |
| 模型 | SCRFD和Silero预训练模型推理 | 新训练视听端到端网络、说话内容理解、声纹身份识别 |
| D1 | 本机录制与合成音视频软件检查通过 | 已完成本轮真人双人移动录制并通过验收 |

当前仍为室内演示候选/研究原型。框架图的实线表示已实现的数据或控制连接，**不表示已做实物精度验收**。必要时以独立标签标“软件验证”“既有录音回放”“待现场验证”。不要在主图添加未启用的TalkNet、ASR/LLM、梅尔图像匹配、六对SRP-PHAT、声学测距或神经注意力融合。

## 8. 需要另一个agent更新的图与说明

1. [system-architecture.svg](assets/system-architecture.svg)与其生成器[build_project_diagrams.py](../tools/build_project_diagrams.py)仍为R51视觉优先总图。重画时必须同时更新生成器，避免下次生成覆盖回旧逻辑。
2. 更新README中英文版的图、替代文本与图下注释；当前文档已明确标识旧图，绘制验收后再移除此标识。
3. 建议交付：系统总图、两个模式的对照图、听声主导的状态/接管图。D1放在旁路记录区，不挤占主决策链路。
4. 保留[microphone-layout.svg](assets/microphone-layout.svg)正确的通道与左右关系；头部/身体CAD不在本次制图变更范围。
5. 优先使用可编辑SVG或现有生成脚本，导出便于PPT使用的PNG/PDF；避免用无法核对文字与箭头的生成式图片替代技术框架图。
