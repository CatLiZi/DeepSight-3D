# 定位机器人 Demo 简易操作手册

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

可以清理但不要提交的内容：`__pycache__/`、`.pytest_cache/`、旧截图、临时抓帧图片、`.tmp/`。

如果只交付一个可运行包，建议把 `README_CN.md` 放在根目录作为入口文档；`docs/QUICK_START_CN.md` 可以保留为备份版本。

## 2. 安装依赖与启动前检查

在 VSCode PowerShell 中进入项目：

```powershell
conda activate demo
Set-Location E:\demo\positioning_robots
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
conda activate demo
Set-Location E:\demo\positioning_robots
Remove-Item Env:\LOCATOR_DEMO_SIM -ErrorAction SilentlyContinue
$env:LOCATOR_DEMO_PORT="8010"
python run_server.py
```

浏览器打开：

```text
http://127.0.0.1:8010/
```

模拟模式，不接机器人时使用：

```powershell
conda activate demo
Set-Location E:\demo\positioning_robots
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
5. 设置相机。普通 USB Camera 推荐 `1280x800` 或 `1280x720`，不裁切；双目宽画面才考虑左半裁切。
6. 设置人脸检测器。近距离可用 `mediapipe`；远距离人脸不稳时用 `scrfd`，并确认 `models/scrfd.onnx` 存在。
7. 设置音频。确认左右方向是否正确；如果左右反了，打开 `swap channels`。
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
Set-Location E:\demo\positioning_robots
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests
```

如果本机有 Node.js，也可以检查前端语法：

```powershell
node --check src\locator_demo\web\static\app.js
```
