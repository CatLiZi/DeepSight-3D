# 定位机器人 Demo 简易操作手册

2026-10-28 当前源码 **R42**（待操作人员正常重启）：本轮复核修复配置切换保留旧指令、开关关闭未取消轨迹、配置文件损坏回退旧中位、无效行程/重复轴ID、四麦VAD关闭时误入旧双麦分支、保存录音设置重置人声检测，以及网页保存状态误导。676项Python回归和66项前端检查通过。当前实查8010为暂停的R41；本轮没有舵机动作、没有新录音，也没有代停或代重启服务。详表见 `../hardware_inventory/system-review-20261028/index.html`。

最终装配参数：Yaw ID0为 **1404 / 1952 / 2500**（左右各548，总1096）；Pitch ID2为 **1255 / 1555 / 1855**（上下各300，总600）。以上均为刻度。50Hz平滑规划与速度、加速度参数保持。左耳CH2/右耳CH3继续参与定位；身体左CH1/右CH4已确认但暂未加入融合。最终装配后双目残差、四麦动态几何及真实声源角度精度仍需单独验证。下面按日期保留历史记录，旧“待重启”和旧参数只描述当时状态。

2026-10-28 R41：使用新采集画面更新双目相对姿态；对齐参数已通过现有相机配置入口加载到R40服务。8组拟合后新图的垂直P95降至1.1–2.9像素，有效画面1874×1053，正式距离仍关闭。R41修复对齐自检误报、原始诊断命令绕过反馈/重复启用、相机更换未取消旧目标及校正矩阵校验，650项回归与60项显示检查通过；R41源码需正常重启。平滑规划及听觉算法源码不变，本轮没有舵机动作。最新可离线说明见 `../hardware_inventory/review-alignment-20261028/index.html`。

2026-10-28 R40：修复R39把真实 `#000MOD1!` 误判为unknown的兼容性错误，仍保留模式、位置和启用跳动保护。633项回归通过，20条真实日志均能识别；当前R39，需正常重启加载R40。

2026-10-28 R39：初始化不再改写工作模式，加入启用前后反馈检查、舵机串口只读自动搜索、四麦拔插恢复、双目启动健康检查与首次可靠声源快速确认。625项回归和57项显示检查通过，16组轨迹与R38完全一致。当前运行R38且暂停；新R39待正常重启，ID0间歇丢回包与下砸的物理根因未宣称解决。

2026-10-28 R38：整链路逻辑审查修复6处边界，594项回归、55组独立模拟及41+6+5项显示检查通过。位置记忆与动作许可分离，稳定声音/视觉接管和说话停顿；检测/几何、视觉运动、DSP与舵机底层冻结。实查已加载R37且暂停，新R38待用户正常重启；本轮未做实体动作。

2026-10-28 核心声源锁定（源码 **R37**，待正常重启，当前服务实查R36）：573项完整回归、41项声音显示检查及6项新核心标记检查通过。听声按最近2.5秒的有效语音时长与方向质量选核心方向，确认后保留有限目标位置；短暂unknown不清除、旧相对角度不反复累加。挑战方向需更高分及连续确认才切换，独立听声不再被预览人脸或过期画面拦截。金色虚线为锁定目标估计，蓝色实时范围仍只在结束后保留1秒；原始定位器、视觉检测/几何及舵机底层未改。目标位置沿用控制增益估计，不能据此声称已标定精确角度。详见上级 `algorithm_guide/index.html#core-sound-r37` 与 `hardware_inventory/audio-aim-20261028/index.html`。定时任务保持暂停。

2026-10-28 上午交接（当前源码 **R36**，现有8010服务实查仍为暂停的 **R32**）：R33保留运动期间的可靠声音，R34增加保守分频段冲突保护，R35避免长断档前后的声音累计为连续确认，R36补齐声画各自的新鲜度检查。当前源码的555项严格回归、41项声音显示检查及2项新增绘制检查通过。视觉检测、双目几何、相机及既有视觉控制参数保持。两个听觉研究候选因独立反例或真实录音退化未采用；右侧角度压缩、真实电机噪声与远距离声音仍未验收。完整表格和正常启动步骤见上级 `algorithm_guide/MORNING_HANDOFF_20261028_CN.txt` 与 `algorithm_guide/index.html#night-handoff-20261028`。下方日期记录保留各自的历史验证范围；最新代码需在原运行终端正常退出后重新运行，本次没有代停或代重启服务。

2026-10-27 听觉/声视融合候选（R33）：521项回归、41项声音显示及6项运动绘制检查通过。取消“运动就一律禁用方向”，使用原有声学质量门槛判定可靠声音；保留电机下发节奏，声音测量继续。修复新PCM但角度变化小于6°时过早停止，以及超时重置搜索预算。新增声视匹配数值日志；蓝色历史范围明确“不触发转向”。视觉检测、几何、视觉控制和参数冻结于R32。已确认R32加载，R33需正常重启；右侧角度压缩与真实运动噪声性能未验收，未拉伸声源角度。本轮未进行新录音或新舵机动作。见上级 `algorithm_guide/index.html#audio-motion-r33` 和 `hardware_inventory/av-listening-20261027/`。

2026-10-27 声视协同与动态追踪候选（R32）：504项回归、41项声音显示检查通过；在旧版复现5个问题，同组修复通过。增加同目标图像速度估计、大偏差大步/近中心减速与160–420ms请求时长；中心静止轴保留已有目标，避免不断取消轨迹中间位置。左右符号改为+1、视觉横向翻转关闭，成对保持原视觉方向；左右修正上限120，速度/加速度/行程及四麦参数未改。配置已保存，源码尚待正常重启加载；没有新舵机动作。新正常讲话RAW及现有服务均检出人声，断续方向、实物方向和顺滑度仍待确认。见上级 `algorithm_guide/index.html#av-coordination-r32` 与 `hardware_inventory/av-coordination-20261027/`。

2026-10-27 前端工作台更新（R31）：控制页整理为“实时感知 / 头部控制 / 设备与标定 / 运行日志”，首页可切换双目并排与主视角。原163个界面标识、77个输入控件及31个操作按钮全部保留；489项回归、41项声音显示检查和12组响应式检查通过。Python算法仍为R28，声音渐变及结束后保留1秒仍为R30。旧版7份原文件备份在 `ui_backups/r30-before-redesign-20261027/`，含校验清单、`RESTORE.txt` 和 `Restore-UI.ps1`；在线旧版入口为 `http://127.0.0.1:8010/static/legacy-r30/index.html`，新导航也保留入口。对8010网页按Ctrl+F5即可加载，无需重启。检查没有发送舵机动作。证据见上级 `hardware_inventory/ui-redesign-20261027/`，汇报见 `algorithm_guide/index.html#ui-redesign-r31`。

2026-10-27 R30显示保留：R29讲话触发渐变已获操作者确认；当前将方向图、两眼和主图改为声音结束后保持原有颜色强度1秒，满1000毫秒后隐藏，新声音立即更新。消息中断时也由独立计时器清除；历史范围不参与选人或舵机控制。489项回归与41项前端检查通过。Ctrl+F5刷新控制页即可，无需重启。预览页新增“模拟声音结束”按钮。

2026-10-27 R29显示修复：补上正前方扇区；声源范围独立检查可靠声学方向，方向图、两眼与大图共用一份状态，以中心淡蓝、向±15°两侧透明渐隐显示。489项回归与31项前端检查通过。Python后端仍为R28、模型与阈值不变；控制页按Ctrl+F5即可加载，无需重启。能量有值但方向unknown时不会伪造范围，现场正常讲话对应待确认。见 `../algorithm_guide/index.html#audio-gradient-r29`。

2026-10-27 白天更新（当前 R28，已由用户正常重启加载）：489项严格回归通过。原网页新增四路输入电平、默认±15°淡蓝色声源范围和候选匹配排序；综合方向接近度、相对脸部大小、检测质量与连续性，声音失效时保留视觉。约1秒淡化的历史范围只供显示，不计为新控制证据。正常讲话诊断已收到人声；此前未录制的无响应片段原因仍未确认。自动转头继续暂停，本轮没有舵机动作。相对大小不是测距，轻声/远距离、真人多目标切换与实物稳定性仍待现场验证。见上级 `algorithm_guide/index.html#audio-visual-score-r28` 与 `hardware_inventory/audio-visual-score-20261027-124942/`。

2026-10-27 夜间历史交接（当时源码 R27）：478项严格回归通过；新增24组分阶段离线集成与2048次更新通过，收尾没有再修改生产算法。当前左耳CH2 / 右耳CH3，身体CH1/CH4排除。原8010服务仍旧版暂停，夜间源码待用户正常重启加载；没有新录音或真实舵机动作。入口见上级 `algorithm_guide/MORNING_HANDOFF_CN.txt` 与 `algorithm_guide/index.html#night-handoff`。以下日期条目保留历史验证范围。

2026-10-26 左右行程更新（R14）：Yaw总范围统一为 **800–2200**、中位1500；Pitch仍为1200–1800，本机水平基准1553。覆盖配置默认值、本机/示例配置、逻辑限幅、连续轨迹、位置反馈检查、诊断运动接口、网页滑块与预设。网页可编辑Yaw min/center/max；预设由后台按当前范围和方向统一生成，当前为800/1150/1500/1850/2200。保存范围会暂停自动控制并要求重新读取位置。新增16项离线测试通过，全套360项没有新增失败（仍保留26项历史问题）；本轮没有进行实物大幅或端点转动验证。证据：`../hardware_inventory/yaw-range-20261026/`。

2026-10-26 控制稳定性更新（R13）：默认俯仰进入阈值从画面高度约5%降至约2%，连续两张新画面确认，采用约1.2%回中释放阈值。运行中约每秒检查逐轴反馈，连续两次无效就停车；静止时只读重连最多一次，失败保持停止。保存运行与串口日志到 `logs/runtime.jsonl`、`logs/serial.jsonl`，网页可下载。按实物反馈校正自动左右映射；俯仰±30刻度实测反馈1561→1536→1558。新增20项测试通过，全套344项没有新增失败（24项失败与2项错误为历史问题）。完整自动跟随和长期稳定性仍待现场验收。刷新控制网页后，先点“检查通信与恢复连接”，再勾选视觉跟踪并点“开始”；只勾选不会启动动作。

2026-10-26 视觉主导更新（R12）：无声时优先较大的可信人脸；有可靠声音时搜索默认 ±15° 扇区，按整张脸的角度区间匹配，扇区内优先大脸、相近大小再参考角度。没有可见对应时保留视觉。画面中心定义为 0°，加入较大 / 中等 / 精细三段图像误差闭环，逐帧逼近目标方向，不依赖米制距离。新增 30 项测试通过，全套 324 项无新增回归；实采约 49.58 fps。自动转头和正式距离保持关闭。详见 [视觉主导与分段逼近说明](docs/VISUAL_PRIORITY_CN.md)。

2026-10-25 声视联合更新（R11）：当前启用 **声音方向辅助选人、视觉持续跟随**；增加本地 Silero 预训练语音检测、多频段时差一致性、跨目纹理方向匹配与稳定选人。安静、模糊或过期声音回退视觉，短暂遮挡不输出旧坐标。新增 29 项测试通过，全套 294 项无新增回归；实采约 49.61 fps，四路音频无溢出/丢块。**正式距离输出与自动转头仍关闭**；真人选人、角度精度及物理跟随尚待在场验收。说明与复测入口见 [声视联合跟踪说明](docs/AV_GUIDED_TRACKING_CN.md)。以下各日期记录保留当时的验证范围。

2026-10-25 双目几何算法更新（R10）：新增按时间分组验证、Soft-L1 抗异常点联合估计和逐姿态/角点误差记录；同一 65 训练 / 18 验证划分中，投影 P95 从 4.74 降至 4.56 px，仍未达 3 px 门槛。匹配增加纹理一致性、特征分布和小视差噪声检查。**实时保持 R09 的每眼 1836×1050 对齐预览，三维距离和自动转向关闭。** 60 项相关测试通过，全套 265 项保留 26 个历史问题，无新增回归。详见 [双目标定操作说明](docs/STEREO_CALIBRATION_CN.md)。

2026-10-25：左右目已同时执行检测与独立跟踪，原网页显示两侧检测框、成对帧号及控制视角切换原因。跨目外观暂定关联、短暂切换确认、失效/过期目标清理已接入；尚未启用标定深度。30 秒实采约 **49.64 fps**，双目成对处理近期约 **16.30 fps**，两眼算法输入仍各 1920×1080；四路音频 0 溢出/丢块，未打开舵机串口。实物遮挡和机械转向待人工验证。详见 [双目识别升级](docs/BINOCULAR_UPGRADE_CN.md)。

2026-10-24：继续使用原控制网页；俯仰水平基准实测设为 **1553**，上键为 Pitch 增大。加入 SCRFD 全画面与局部小脸搜索、原像素检测输入和双目清晰度辅助。设备实测 **不提供 Focus / Zoom 控制**，当前 MF 镜头无法由软件实现光学自动对焦。15 秒预热后采集约 33.09 fps，四路音频无溢出或队列丢块；自动转头关闭。详见 [视觉更新](docs/VISUAL_CLARITY_UPGRADE_CN.md) 与 [舵机现场记录](docs/SERVO_CALIBRATION_CN.md)。

2026-10-23：V17 头部实物已接入，使用 **左耳 CH3 / 右耳 CH2**，身体 CH1 / CH4 暂不参与定位、人声判断或 ASD。已修正系统音效导致四路近似相同的问题：保持 WASAPI 共享、48 kHz、16 bit，并为本程序启用 RAW 流。两眼分别旋转 180°，二维检测可自动回退到有效画面。15 秒主程序测试约 33.16 fps、音频 0 溢出/丢块。舵机识别为 COM3 / ID 0、2，但小幅测试时反馈中断，正反方向和实物顺滑度仍待核对；自动控制保持关闭。详见 [头部实物适配记录](docs/HEAD_ASSEMBLY_ADAPTATION_CN.md)。以下第一阶段段落保留当时的验证边界。

2026-10-21 第一阶段升级已接入主程序与本机/示例配置：双目同屏 **3840×1080（每眼 1920×1080）、MJPEG、60 fps 请求档位**；音频 **4 通道、48 kHz、16 bit PCM、WASAPI 共享模式**。主程序连续约 30 秒收到 995 帧，实际 **33.12 fps**；四通道录音无溢出、无录音丢块。已加入独立 **50 Hz 平滑运动轨迹**并通过离线验证；该阶段当时舵机板尚未接入，后续接入与夜间暂停状态见最新交接。完整说明与复测入口见 [第一阶段升级记录](docs/STAGE1_UPGRADE_CN.md)，参数见 [硬件升级规格](docs/hardware-upgrade-profile.json)，导师汇报见 [HTML 汇报页](../algorithm_guide/index.html)。

当前新硬件使用双目二维识别、左耳 CH2 / 右耳 CH3 定位（以2026-10-26重插与逐耳确认结果为准），原始双目及四路录音完整保留；深度与声学角度精度尚待标定。以下旧操作章节中的“双麦左右互换、普通相机裁切”适用于旧设备后端；新的采集与运行方式以最近的升级记录为准。

这份文档面向现场演示和快速交接，说明哪些文件是必要的、如何安装依赖、如何启动 demo、dashboard 怎么操作，以及当前各模块的作用。当前版本的 Python 依赖已经整理到一个完整的 `requirements.txt`；增强模型文件仍按需放置。更长的算法说明见 `docs/ALGORITHM_CONTROL_LOGIC_CN.md`，硬件排查见 `docs/HARDWARE_DEBUG.md`。

## 1. 必要文件

### 必须保留

| 路径 | 作用 |
| --- | --- |
| `run_server.py` | 启动 FastAPI/uvicorn 后端的入口。 |
| `requirements.txt` | 完整 Python 依赖清单：Web 后端、串口、OpenCV、MediaPipe、SCRFD/ONNXRuntime、TalkNet/PyTorch 等。 |
| `src/locator_demo/` | Demo 核心代码，包含音频、视觉、舵机、状态机和 Web 后端。 |
| `src/locator_demo/web/static/` | Dashboard 前端页面、JS 和 CSS。 |
| `locator_demo_settings.example.json` | 当前配置文件模板，方便迁移或重置。 |

### 现场常用但不需要提交的文件

| 路径 | 作用 |
| --- | --- |
| `locator_demo_settings.json` | 本机真实配置，运行时自动保存。包含 COM 口、舵机 ID、相机、音频和策略参数。 |
| `.venv/` 或 conda 环境 | Python 虚拟环境。每台电脑自己创建即可。 |
| `.tmp/` | 测试或调试临时文件。 |

### 可选增强文件

| 路径 | 什么时候需要 |
| --- | --- |
| `models/face_landmarker.task` | 使用 MediaPipe Tasks 人脸关键点检测时需要。 |
| `models/pose_landmarker_lite.task` | 使用 MediaPipe Pose 辅助重取景时需要。 |
| `models/scrfd.onnx` | 使用 SCRFD 人脸检测器时需要。远距离人脸检测通常比基础方案稳。 |
| `requirements-scrfd-gpu.txt` | 旧的 SCRFD 拆分依赖说明。完整安装已经合并到 `requirements.txt`，交付时可以不带。 |
| `third_party/TalkNet-ASD/` | 使用 TalkNet active speaker detection 时需要。 |
| `third_party/TalkNet-ASD/pretrain_TalkSet.model` 或 `pretrain_AVA.model` | TalkNet 预训练权重。没有权重会 fallback 到 rules。 |
| `requirements-asd-gpu.txt` | 旧的 TalkNet 拆分依赖说明。完整安装已经合并到 `requirements.txt`，交付时可以不带。 |
| `tests/` | 回归测试，不影响现场运行，但修改代码后建议保留。 |
| `docs/` | 文档，不影响运行，但建议保留。 |


## 2. 安装依赖与启动前检查

在 VSCode PowerShell 中进入项目：

```powershell
conda activate demo
Set-Location C:\Users\SLAI\Desktop\DeepSight-3D\positioning_robots
```

安装完整 Python 依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果要使用 NVIDIA GPU 跑 TalkNet，建议先安装与你电脑 CUDA/驱动匹配的 PyTorch，再安装完整依赖。例如：

```powershell
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
python -m pip install -r requirements.txt
```

如果 `onnxruntime-gpu` 在目标电脑安装失败，可以把 `requirements.txt` 里的 `onnxruntime-gpu` 临时改成 `onnxruntime`，SCRFD 会走 CPU 或其它可用 provider；TalkNet 不可用时 dashboard 会显示 fallback 到 `rules`。

系统依赖还需要单独安装 `ffmpeg`，并保证 PowerShell 可以直接运行 `ffmpeg`。

检查 ffmpeg：

```powershell
ffmpeg -version
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

检查串口：

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Description
```

如果要确认 Python 依赖：

```powershell
python -c "import fastapi, uvicorn, serial, cv2, numpy, mediapipe; print('core deps ok')"
python -c "import torch, onnxruntime; print('cuda=', torch.cuda.is_available(), 'providers=', onnxruntime.get_available_providers())"
```

## 3. 启动 Demo

真实硬件模式：

```powershell
Set-Location "C:\Users\SLAI\Desktop\DeepSight-3D\positioning_robots"
$env:LOCATOR_DEMO_PORT = "8010"
$env:LOCATOR_DEMO_SIM = "0"
& "C:\Users\SLAI\miniconda3\envs\demo\python.exe" -X utf8 .\run_server.py
```

如旧服务仍在运行，先在它原来的 PowerShell 窗口按 `Ctrl+C`，看到退出完成后再运行上述命令。不要并开另一个真实硬件服务。重新启动并对网页按 `Ctrl+F5` 后，可只读查看 `http://127.0.0.1:8010/api/state`：`algorithm_revision` 应为 `R42`，`running` 应为 `false`。源码导入与启动参数已做隔离模拟检查，真实服务的正常重启仍需操作人员完成。

浏览器打开：

```text
http://127.0.0.1:8010/
```

模拟模式，不接机器人时使用：

```powershell
conda activate demo
Set-Location C:\Users\SLAI\Desktop\DeepSight-3D\positioning_robots
$env:LOCATOR_DEMO_SIM="1"
python run_server.py
```

停止 server：在运行 server 的 PowerShell 窗口按 `Ctrl+C`。

如果改过前端 JS/HTML，刷新 dashboard 时按 `Ctrl+F5`。

## 4. Dashboard 推荐操作顺序

1. 先不要急着开自动追踪。先关闭或保持关闭 `Audio`、`Visual tracking`，只保留必要的 `Camera` 预览。
2. 设置串口和舵机 ID。常见配置是 `Yaw ID = 0`，`Pitch ID = 2`，但以现场扫描结果为准。
3. 用手动按钮测试 yaw/pitch。确认左、右、上、下方向正确；如果反了，在方向配置里调整 yaw/pitch sign。
4. 设置舵机范围。默认安全范围是 `1200..1800`，如果某个舵机在某段抖动，就把范围避开。
5. 新双目相机选择“双目原始 MJPEG”、`3840x1080`、采集档位 `60`、每眼算法输入宽度 `1920`。原始采集始终保留双目；旧设备裁切选项在此模式下不生效。
6. 设置人脸检测器。近距离可用 `mediapipe`；远距离人脸不稳时用 `scrfd`，并确认 `models/scrfd.onnx` 存在。
7. 新四麦先核对界面的 4 通道 / 48000 Hz / 16 bit 和四路音量；空间布局标定与四麦方向算法完成前，听声方向保持 unknown。旧双麦后端才使用 `swap channels`。
8. 选择策略，再点击 `Start` 开始自动逻辑。

## 5. 现场策略怎么选

### 嘈杂现场最稳配置

用于 open day、旁边很多人说话、背景噪声不可控的场景：

```text
Strategy: crowded visual first
ASD: talknet，如果 TalkNet 不稳定就用 rules
Lock policy: until_lost
Silent visual search: OFF
Audio interrupt: OFF
Near face: 0.15 - 0.20
Keep face: Near face - 0.03 左右，例如 0.18 / 0.14
Control profile: stable
```

含义：画面里有近距离候选人时，不被画面外噪声带走；只有画面里的说话人被确认后才红框锁定。

### 安静测试或需要找画面外说话人

```text
Strategy: crowded visual first
Silent visual search: ON
Silent hold s: 0.8 - 1.2
Audio interrupt: OFF
```

含义：画面里有人但没说话时，先短暂保持；如果外部声源稳定，超过 hold 时间后允许音频 yaw 搜索，把人带入视野。

### 只测声源定位

```text
Camera: 可关
Visual tracking: OFF
Audio: ON
Strategy: classic audio first
```

含义：主要看左右声源定位和 yaw 粗转，不测试 active speaker 视觉确认。

## 6. 当前模块功能

### `run_server.py`

启动入口。它把 `src` 加入 Python 路径，然后运行 `locator_demo.web.app:create_app()`。环境变量：

- `LOCATOR_DEMO_PORT`：端口，默认 `8010`。
- `LOCATOR_DEMO_SIM=1`：模拟模式，不连接真实舵机/设备。

### `src/locator_demo/web/app.py`

Web 后端和总控运行时。负责：

- 提供 dashboard 页面和 REST/WebSocket 状态。
- 保存硬件、相机、音频、策略配置。
- 调用音频、视觉、状态机和舵机控制。
- 实现 `classic_audio_first` 与 `crowded_visual_first` 两套策略。
- 实现 speaker lock、silent visual search、audio search、visual reframe 等控制逻辑。

### `src/locator_demo/web/static/`

Dashboard 前端：

- `index.html`：页面结构。
- `app.js`：读取状态、保存配置、按钮操作、绘制框和状态。
- `styles.css`：页面样式。

前端框含义：

- 普通/候选框：检测到人脸或候选目标。
- 绿色语义：active speaker candidate。
- 红框语义：当前锁定的 specific speaker，不是身份识别，只表示当前跟踪目标。

### `src/locator_demo/audio.py`

声源定位算法核心：

- GCC-PHAT / SRP-PHAT 前方半平面方向估计。
- 输出 `tdoa_s`、`azimuth_deg`、`direction`、`speech_confidence`、`doa_confidence`。
- 音频平滑、左右方向分类、motor guard 抑制。
- 当前双麦主要估计水平 yaw，不用音频估计 pitch。

### `src/locator_demo/audio_device.py`

音频采集：

- 通过 ffmpeg DirectShow 读取 Windows 麦克风。
- 自动优先选择常见设备名，如 `YDM2MIC`、`Realtek`。
- 维护最近音频缓存，供 TalkNet 或 ASD 后端使用。

### `src/locator_demo/camera.py`

相机和视觉前端：

- 通过 ffmpeg DirectShow 读取 USB camera。
- 支持输出尺寸、FPS、左半裁切。
- 支持 MediaPipe、SCRFD、Haar fallback。
- 输出多个人脸目标、bbox、tracking center、嘴部运动、人体/pose 辅助目标。
- 对人脸目标做短时稳定和 ID 跟踪，避免多人时框闪烁或重复红框。

### `src/locator_demo/vision.py`

视觉目标数据结构和几何工具：

- `FaceTarget`、`BodyTarget`。
- 选择主目标 / active speaker 候选。
- `normalized_center_error()` 计算目标相对画面瞄准点的误差，用于视觉控制 yaw/pitch。

### `src/locator_demo/asd.py`

Active Speaker Detection 后端选择：

- `rules`：轻量规则，用嘴部运动、人脸正对、音频区域一致性给分。
- `talknet`：调用 TalkNet 插件。可用时使用 GPU；不可用会显示 fallback 信息。

### `src/locator_demo/talknet_adapter.py`

TalkNet 官方模型适配层：

- 默认查找 `third_party/TalkNet-ASD/`。
- 默认查找 `pretrain_TalkSet.model` 或 `pretrain_AVA.model`。
- 需要 CUDA、PyTorch、OpenCV、python_speech_features 等依赖。

### `src/locator_demo/orchestrator.py`

经典状态机：

```text
LISTENING -> AUDIO_CANDIDATE -> SEEK_VISUAL -> VISUAL_CONFIRM -> TRACK_SPEAKER -> HOLD/COOLDOWN
```

现在 crowded 策略的主要逻辑在 `web/app.py` 里，但仍复用这里的 audio_ready、target_confirmed 等判断。

### `src/locator_demo/head.py`

舵机目标计算：

- 手动 jog。
- 音频 azimuth 到 yaw 步进。
- 视觉中心误差到 yaw/pitch 步进。
- deadband、最大步长、最小步长和轴范围 clamp。

### `src/locator_demo/servo_bus.py`

真实舵机通信：

- 扫描串口和舵机 ID。
- Zhongling/ZP 协议读写位置。
- 双轴头部硬件封装。

### `src/locator_demo/settings.py`

配置读写：

- 默认配置、字段范围校验、保存到 `locator_demo_settings.json`。
- 重要字段包括 camera、audio、axis_limits、vision strategy、speaker lock、silent visual search。

### `src/locator_demo/devices.py`

设备发现：

- 调用 ffmpeg 列 DirectShow audio/video 设备。
- 自动选择合适的麦克风或相机。

### `tests/`

单元测试和回归测试：

- 音频算法、视觉目标、舵机协议、Web API、策略状态机。
- 修改控制逻辑后建议运行。

## 7. 常用参数解释

| 参数 | 位置 | 含义 |
| --- | --- | --- |
| `tracking_strategy` | Vision processing | `classic_audio_first` 或 `crowded_visual_first`。 |
| `asd_backend` | Vision processing | `rules` 或 `talknet`。 |
| `speaker_lock_policy` | Vision processing | `turn_hold`、`until_lost`、`interruptible`。 |
| `audio_search_after_silent_visual` | Dashboard: Silent visual search | 静默近脸是否允许释放给画面外音频搜索。 |
| `silent_visual_hold_s` | Dashboard: Silent hold s | 静默近脸至少保持多久后才允许音频搜索。 |
| `audio_interrupt_enabled` | Dashboard: Audio interrupt | 红框锁定后，是否允许外部声音打断。现场通常关。 |
| `min_face_height_ratio` | Dashboard: Near face | bbox 高度 / 处理帧高度。小于该值视为远处人，不参与锁定。 |
| `keep_face_height_ratio` | Dashboard: Keep face | 已有候选/锁定目标的保留阈值，通常比 Near face 小。 |
| `target_offset_x_norm` / `target_offset_y_norm` | Dashboard: Target X/Y | 视觉瞄准点偏移，补偿左眼相机不在头部中心。 |
| `visual_yaw_mode` | Dashboard: Visual yaw | `off`、`small`、`full`。现场一般用 `small`。 |
| `control_profile` | Control | `stable` 更稳，`fast` 更灵敏。 |
| `motor_guard_ms` | Control | 舵机转动后音频抑制时间，防止电机声二次触发。 |

## 8. 常见问题

### 听到声音只动一下，还没到位就开始找画面

当前代码已改为 `AUDIO_SEARCH` 优先完成音频 yaw，再让 visual reframe 接手。请重启 server，确保用的是最新代码。

### 画面里有人但没说话，画面外有人说话，机器人不动

这是 `crowded visual first` 且 `Silent visual search = OFF` 的保守现场策略。打开 `Silent visual search` 后，静默候选超过 `Silent hold s` 会允许外部音频搜索。

### 远处人也被框到，容易误触发

提高 `Near face`，例如 `0.18`；`Keep face` 设成 `0.14` 左右。注意比例基于后端处理帧尺寸，不是相机原始尺寸。

### 前端没有新控件

按 `Ctrl+F5` 硬刷新。如果还没有，停止 server 后重新运行 `python run_server.py`。

### TalkNet 显示 fallback

检查：

- `third_party/TalkNet-ASD/talkNet.py` 是否存在。
- `pretrain_TalkSet.model` 或 `pretrain_AVA.model` 是否存在。
- 当前环境是否能 `import torch` 且 `torch.cuda.is_available()` 为 `True`。
- `requirements.txt` 里的 TalkNet 相关依赖是否装齐。

### SCRFD 不生效

检查：

- `models/scrfd.onnx` 是否存在，或在 dashboard 填了正确模型路径。
- 安装了 `requirements.txt` 里的 InsightFace/ONNXRuntime 相关依赖。
- Dashboard 里 `Face detector` 选择了 `scrfd`。

## 9. 交付前清理建议

推荐交付的最小可运行文件：

- `run_server.py`
- `requirements.txt`
- `README_CN.md`
- `locator_demo_settings.example.json`
- `src/locator_demo/`
- `models/face_landmarker.task` 和 `models/pose_landmarker_lite.task`，如果现场使用 MediaPipe Tasks / Pose
- `models/scrfd.onnx`，如果现场使用 SCRFD
- `third_party/TalkNet-ASD/` 和 `pretrain_*.model`，如果现场使用 TalkNet

推荐保留但不是运行必需：

- `docs/`：算法、硬件、快速手册等说明。
- `tests/`：修改代码后做回归测试。
- `requirements-asd-gpu.txt`、`requirements-scrfd-gpu.txt`：旧的拆分安装说明，已经合并后可以不交付。

可以删除或不交付：

- `.venv/`、conda 环境目录：每台电脑重新创建。
- `.git/`：如果不是以 Git 仓库形式交付，可以删除。
- `locator_demo_settings.json`：本机真实配置，包含 COM 口、设备名、舵机 ID，不建议交给别人覆盖使用。
- `.tmp/`、`__pycache__/`、`.pytest_cache/`、临时图片、旧抓帧、日志。

不要误删：

- 使用 SCRFD 时不要删 `models/scrfd.onnx`。
- 使用 MediaPipe Tasks / Pose 时不要删 `models/face_landmarker.task` 和 `models/pose_landmarker_lite.task`。
- 使用 TalkNet 时不要删 `third_party/TalkNet-ASD/talkNet.py` 和预训练权重。
- 不确定接收方是否要继续开发时，保留 `tests/` 和 `docs/`。

## 10. 修改代码后的验证

```powershell
conda activate demo
Set-Location C:\Users\SLAI\Desktop\DeepSight-3D\positioning_robots
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests
```

如果本机有 Node.js，也可以检查前端语法：

```powershell
node --check src\locator_demo\web\static\app.js
```


## 11. R15 遗留回归与当前四麦输入检查（2026-10-26）

原 26 项失败均重新复现并逐项处理：17 项仅修复生产代码即可通过，5 项同时修复代码与过期测试条件，4 项仅纠正旧测试的接口、阈值或确认窗口。原测试全部保留；严格全套回归零失败、零错误、零跳过。详细表格位于上级 `algorithm_guide/index.html#regression-repair-r15`。

- 四麦板必须使用 WASAPI RAW、4 通道 / 48 kHz / 16 bit；新板不允许走旧双声道 DirectShow 降混路径。左耳 CH3、右耳 CH2，CH1/CH4 不进入定位、VAD 或 ASD。
- 增加 PCM 实际到达速率与溢出检查；异常音频停止定位并写入 `logs/runtime.jsonl`，采集重新打开时清除上一会话的人声状态与时差历史。
- 公共视觉控制与手动方向统一；渐进控制移除补偿性反号，本机镜像、安装方向和上下水平基准保持。旧说话人策略与当前视觉优先策略分别验证。
- 旧双声道录音也修复短读、EOF 尾部丢失、无降噪时重复录音及整段内存缓存，改为有界队列写盘。

本轮真实 RAW 采集出现高噪声与到达速率异常；多个采集方式复现，不能仅因“四路不同”就认定录音正常。普通共享模式又近似复制四路，不能用作定位替代。需要重新插拔四麦板并对比 USB 口/数据线，再做逐耳与固定方向说话记录；如仍异常，比较厂家工具或另一台电脑。当前不把声学校准或该异常的根因标记为已完成。

三轮只读查询中 ID 0、ID 2 均正常反馈，本轮没有发送转动指令。左右软件范围仍 800–2200，上下 1200–1800、水平 1553；机械极限和带负载稳定性仍待现场验收。

可复现的严格软件验证（不访问实物设备）：

```powershell
conda activate demo
python tools/validate_regression_repair.py --output ../hardware_inventory/regression-repair-20261026/recheck
```

此命令遇到任何失败返回非零，不对历史失败放行。`tools/probe_audio_integrity.py --seconds 8 --output <新目录>` 会读取真实麦克风，运行前应先停止占用设备的主程序；它不访问舵机。所有本轮证据在上级 `hardware_inventory/regression-repair-20261026/`。


## 12. R16 麦克风重插复测与最新耳位（2026-10-26）

同一算法源码下，USB重新插拔后RAW四通道采样恢复；8秒、约180秒和约171秒录音均正常。两段长录音缓冲溢出与录音丢块均为0。本轮依据两组近耳轻擦和操作者确认，本机当前映射改为小熊自身左耳CH2、右耳CH3，身体CH1/CH4继续排除；不要把此前R03/R15的通道表当作当前配置。

面对小熊时，操作者左侧是小熊右侧。方位测试实际顺序为“小熊右前方→正前方→左前方”，距离约40–50厘米，不是1米，也不是精确角度或距离真值。三段人声均检出；方向状态抽样仅右前段出现一次约右47.7°有效输出，后两段多数被频段一致性／时差峰检查拒绝。方向稳定性尚未通过，不把unknown视作正前方。

本轮只修改本机耳位配置，算法和保护阈值保持；自动转动暂停，未发送舵机运动命令。需要继续用已保存录音分析频段可靠性与时差估计，不能通过调低门槛宣称通过。原USB异常的具体驱动／传输／板卡根因仍未确认。证据：上级hardware_inventory/microphone-replug-20261026/，汇报：algorithm_guide/index.html#microphone-replug-r16。

## 13. R17 两耳频谱可靠性与时差升级（2026-10-26）

当前映射为小熊左CH2、右CH3，CH1/CH4身体通道排除；按操作者13.5–13.6cm的估计采用135.5mm工作间距，固定延迟补偿保持0。不是精密尺寸或声学零点标定。四通道、48kHz、16bit、WASAPI RAW采集不变。

- 本地Silero先判断人声，无声窗口学习每耳背景功率谱。192ms上下文分短帧平均相干性，用相干性与语音余量加权，部分白化后求GCC时差。
- 检查归一化相关、相干性和独立竞争峰；超出物理范围拒绝，保留亚采样插值。短时中位数降低跳变，单个候选置信度为0；质量不足当帧unknown，静音、断流、过期或持续失败清理历史。
- 当前源码通过390项严格回归，零失败、零错误、零跳过。完整录音采用实际人声模型做旧新版对照，三段可用方向显著增加；静音、独立噪声、纯音与近耳摩擦均无可用方向。
- 录音位置仅约40–50cm、没有真实角度；左前部分窗口仍接近正中，严格的三段75%定性区域开发目标未全部达到。这是开发回放，不是独立房间、说话人或测角验收。

新版主程序已加载并保持自动运动暂停；启动期间一次到达速率异常已隔离、写日志并恢复，预热后约15秒采集正常且无溢出。本轮未发送舵机运动指令。所有证据在上级hardware_inventory/audio-reliability-20261026；汇报页为algorithm_guide/index.html#audio-reliability-r17。

离线重放（不访问硬件，输出到已含冻结R16基线的新目录）：

```powershell
python tools/validate_audio_reliability.py --output ../hardware_inventory/audio-reliability-20261026
```

保留该目录中的原录音和ear_localization-r16.py。重跑会更新回放摘要；严格75%开发目标单独列为未通过，不能用脚本退出码替代实物验收结论。标准软件验证仍使用tools/validate_regression_repair.py。

## 14. R18 新录音近场检查（2026-10-26）

生产算法、阈值与零点保持R17，与390项通过软件测试的源码哈希一致。按用户要求先回中，再固定头部录音；左CH2、右CH3、135.5mm工作间距。约半米正常音量新录音的可用方向比例为右前45.3%、大致正前79.1%、左前65.3%，旧版相应约0.3%、3.5%、4.1%。方向中位顺序相符，但右侧仍有少量反向与断续；轻声录音未形成稳定人声段。窗口重叠、方位为目测，这些比例不是实际测角准确率。

三轮采集无溢出和录音丢块。远距离依用户要求暂缓，未纳入测试；自动跟踪保持暂停。新录音未用于调参或拟合正前零点。证据见上级hardware_inventory/audio-direction-check-20261026-213212，汇报入口algorithm_guide/index.html#independent-audio-r18。

## 15. R19 夜间方向时间保护（2026-10-26）

源码增加按PCM采样时钟确认跨侧突跳、拒绝重复采样时钟、在静音和断流时清理参考。401项严格软件测试通过；现有录音回放各片段保留至少92.7%原可用方向，右前片段反侧窗口7→5，仍有残余。真实换边的合成信号约0.10秒恢复可用方向，不代表实物响应时间。简单频谱降噪证据不足未接入。

自动批准审查拒绝了停止/重启本地服务，仅返回blocked by policy。当前8010仍运行R17算法/R18验证版本，新源码待用户正常重启程序后加载；自动转头持续暂停。未执行实物动作、远距离测试或精确零点拟合。记录见上级hardware_inventory/overnight-20261026，汇报入口algorithm_guide/index.html#overnight-r19。后续夜间迭代须保留这一轮冻结证据，避免覆盖已完成阶段的数据。


## 16. R20 声音选人入口保护（离线验证，待重启）

只允许有限且范围正确的声音概率、方向、不确定度、时间戳和非负整数采样序号参与选人；异常数据退回视觉并清空声音确认计数。两种选人策略共26个故障场景通过，44组正常对照1360次决策与冻结R19一致。完整413项测试通过，原26项问题无复发、无跳过。

证据：`../hardware_inventory/overnight-20261026/selector-r20/`；汇报：`../algorithm_guide/index.html#selector-health-r20`。这是故障注入的软件验证，不代表声音测角、轻声或实物跟随已通过。此前重启被自动批准审查拒绝（仅返回 blocked by policy），现有8010进程仍运行原版并保持暂停，R19/R20源码待正常重启加载；没有通过其他工具重试，没有发送舵机动作。


## 17. R21 双目中断恢复确认（离线验证，待重启）

检测更新中断超过原有0.75秒失效窗口后，双目配对、声音选人和控制视角的确认计数重新累计。新坐标可供观察，控制就绪仍需至少3次新观察与0.12秒持续证据。正常跳帧、单眼遮挡等既有行为保持；不跨较长中断完成换人确认。

完整423项严格回归全通过，原26项问题持续通过；81组中断模拟、2205次正常决策对照和5类历史真实样张数字遮挡通过。证据见 `../hardware_inventory/overnight-20261026/stereo-gap-r21/`，汇报 `../algorithm_guide/index.html#stereo-gap-r21`。未进行现场遮挡或舵机动作，没有实物顺滑度或正式测距验收。当前8010仍为原版暂停，R19–R21源码待正常重启加载；未绕过此前自动批准审查对重启操作的拒绝。


## 18. R22 串口会话启用缓存（模拟验证，待重启）

舵机启用缓存只在同一个已打开串口连接内复用；旧连接关闭、隐式重连或新连接打开失败时，旧缓存被清除。启用操作先确认连接并在通信锁内完成，诊断加入当前缓存与失效事件。只读查询不发送启用或位置动作指令；正常连续控制不重复启用。

全433项严格回归通过，原26项问题持续通过；12个模拟重连场景、800次正常操作/1968次模拟写入对照通过。没有打开或写入真实舵机串口，不代表历史实物反馈问题或顺滑度已验收。R22记录的总控旧手动请求取消问题，已由R23补上软件保护。证据见 `../hardware_inventory/overnight-20261026/servo-session-r22/`；汇报 `../algorithm_guide/index.html#servo-session-r22`。原8010服务继续暂停，已验证源码待正常重启；没有绕过自动批准审查的重启拒绝。


## 19. R23 停止取消旧控制请求（软件验证，待重启）

请求编号在HTTP工作线程排队前和运行时锁等待前捕获；停止或新请求作废旧编号，位置读取前后与提交前再次核对。旧反馈不能清除新故障，迟到轨迹被取消且旧目标不写回状态；预期取消不锁定为自动控制通信故障。停止意图锁不跨越反馈读取或后端写入。

446项严格回归全通过、原26项持续通过；34个取消组合和500次正常对照操作通过。全部为模拟反馈/执行器，没有打开或写入真实舵机串口。已经交给驱动的写入无法撤回，真实停止延迟和机械制动未验收。证据见 `../hardware_inventory/overnight-20261026/manual-cancel-r23/`；汇报 `../algorithm_guide/index.html#manual-cancel-r23`。8010原服务保持暂停，R19–R23源码待正常重启加载；没有绕过自动批准审查对重启的拒绝。


## 20. R24 低信噪比评估（生产源码保持R23）

新增40组开发、40组分离语音片段的基线条件与8类反例；信号与噪声在同一6秒插入区间计算SNR。60/120Hz高通候选平均只增加0.414/1.107个百分点的噪声条件参考证据保留，未达到预先设定的采用条件，没有接入生产。模型、阈值与源码保持R23。清洁参考证据由原模型定义，缺少人工逐帧标注，不是识别准确率；真实轻声和远距离效果未验收。

446项严格回归全部通过，原26项持续通过。最终证据见 `../hardware_inventory/overnight-20261026/vad-benchmark-r24/exact-snr-v2/`；初稿定标口径核对与记录保留，汇报 `../algorithm_guide/index.html#vad-benchmark-r24`。原8010服务仍为旧版暂停，R19–R23源码待正常重启加载；本轮没有实物动作或重启尝试。

## 21. R25 轨迹绑定串口连接（软件验证，待重启）

模拟复现并修复：串口失效或被只读重连后，已有平滑轨迹可能通过新连接继续启用、发送旧位置。现在创建轨迹器时绑定有效串口会话，核对连接和发送指令使用同一通信锁；准备过程中也不能隐式重开端口。会话失效或写入异常后锁定停止、清除旧目标；读取新位置不会自行解锁旧轨迹器。正常位置同步创建新的轨迹器后，才可接受明确的新目标。此保护不代替位置反馈，连接不变的硬件复位仍需反馈监测。

458项严格回归全通过，原26项持续通过；27组故障模拟、4059个正常轨迹采样和2038次模拟写入对照通过。没有打开或写入真实舵机串口，实物反馈故障原因、停止延迟及顺滑度未验收。证据见 `../hardware_inventory/overnight-20261026/trajectory-session-r25/`；汇报 `../algorithm_guide/index.html#trajectory-session-r25`。原8010服务保持旧版暂停，R25累计源码待正常重启加载；未绕过此前自动批准审查对停止/重启操作的拒绝（仅返回 blocked by policy）。

## 22. R26 音频快照与采样时钟一致性（软件验证，待重启）

模拟复现并修复：旧采集适配器先读取最新音频编号，再读取声音窗口；回调推进、重连或清空缓冲发生在两次读取之间时，新声音可能配上旧时钟。现在只读取一次受采集锁保护的窗口，从该窗口末尾确定编号、采样结束时钟与接收时间。重复快照不会增加语音/方向证据，溢出或空缓冲仍退回未知。

24组交错场景通过；5段既有四通道录音及7类合成输入共35407个窗口，全部估计字段和实际本地Silero调用次数与冻结R25一致。466项严格回归全通过、原26项持续通过，新增8项快照测试。此项仅修复数据一致性，未改变语音模型、时差估计、通道映射、RAW配置、13.55cm估计间距或舵机参数；不能据此宣称轻声、远距离或测角精度改善。证据见 `../hardware_inventory/overnight-20261026/audio-snapshot-r26/`；汇报 `../algorithm_guide/index.html#audio-snapshot-r26`。原8010仍旧版暂停，R26累计源码待正常重启；没有新录音、打开真实舵机端口或绕过此前的重启拒绝。

## 23. R27 声音选人的新观察与重置保护（软件验证，待重启）

模拟复现并修复：音频编号反复交替或重播时，旧代码把“编号不同”当作新的独立证据；重置后的新输入也可能沿用旧确认。现在两套选人策略共用观察与确认逻辑：编号和接收时间都前进才增加次数，同一快照不重复计票；时间更新而编号回退/复用时清除旧确认，旧时间或乱序输入不降低已接受基准。合法编号跳跃仍可使用，重置后新的持续证据仍能恢复；视觉选择与原有保持时间不变。

84组异常模拟不再产生错误确认，其中旧版36组错误换人变为0组。48组正常控制/4320次决策及5段既有录音方向时间线进入两套策略后的68014次决策和原有诊断字段保持一致；12组合法重置能重新确认恢复。478项严格回归通过，原26项持续通过，新增12项测试。既有方向时间线配合的是合成人脸，属于兼容性控制，不能当作新的现场声画、说话人身份或角度准确率验收；没有更改人声模型、时差DSP或硬件参数。证据见 `../hardware_inventory/overnight-20261026/selector-sequence-r27/`；汇报 `../algorithm_guide/index.html#selector-sequence-r27`。原8010仍旧版暂停，R27累计源码待正常重启；没有真实舵机动作或重试此前被自动批准审查拒绝的停止/重启操作。
