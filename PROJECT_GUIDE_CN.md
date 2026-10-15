# 定位机器人 Demo — 项目说明与操作手册

一台**双轴机器人头部**：用双麦克风听声辨位、用摄像头看人、自动转向正在说话的人。

本文覆盖：项目结构、环境安装、启动配置、首次调试流程、参数详解、注意事项、故障排查。
环境安装的踩坑记录另见 [`docs/ENVIRONMENT_SETUP_CN.md`](docs/ENVIRONMENT_SETUP_CN.md)。

---

## 目录

1. [系统组成](#1-系统组成)
2. [目录与文件详解](#2-目录与文件详解)
3. [环境安装](#3-环境安装)
4. [启动](#4-启动)
5. [首次调试流程](#5-首次调试流程必按顺序)
6. [Dashboard 参数详解](#6-dashboard-参数详解)
7. [策略怎么选](#7-策略怎么选)
8. [⚠️ 注意事项](#8-️-注意事项)
9. [故障排查](#9-故障排查)
10. [开发与测试](#10-开发与测试)

---

## 1. 系统组成

```
        ┌─────────────┐
        │  双麦麦克风  │  YDM2MIC，双声道，44.1kHz
        └──────┬──────┘
               │ PCM 双声道
        ┌──────▼───────────────────────────────┐
        │        Python 后端 (FastAPI)          │
        │                                      │
        │  audio.py      声源定位 (GCC-PHAT)    │
        │  camera.py     人脸检测 + 目标跟踪     │
        │  asd.py        谁在说话 (TalkNet/rules)│
        │  head.py       目标 → 舵机步长         │
        │  orchestrator  状态机                 │
        └───┬──────────────────────────┬───────┘
            │ 串口 CH340 (COMx)         │ WebSocket
        ┌───▼──────────┐        ┌──────▼────────┐
        │  双轴舵机     │        │  Dashboard     │
        │  yaw / pitch  │        │  (浏览器)      │
        └───────────────┘        └───────────────┘
```

**闭环逻辑**：麦克风听出声音方向 → 头部粗转过去 → 摄像头看画面里有没有人脸 →
判断哪张脸在说话 → 视觉微调把目标保持在"瞄准点"上。

---

## 2. 目录与文件详解

### 入口

| 文件 | 作用 |
| --- | --- |
| `run_server.py` | **启动入口**。把 `src` 加进 Python 路径，然后跑 `locator_demo.web.app:create_app()`。 |
| `mix.sh` | 音频回放调试的一行命令，调用下面的 `run_audio_mix_replay.py`。 |
| `run_audio_mix_replay.py` | 离线工具：把录下来的 WAV 重新喂给定位算法，用于不开硬件时调音频参数。 |

### `src/locator_demo/` — 核心代码

| 文件 | 作用 | 改动风险 |
| --- | --- | --- |
| `web/app.py` | **总控 + Web 后端**（最大文件）。实现两套追踪策略、speaker lock、silent visual search、audio search、visual reframe，并提供所有 REST/WebSocket 接口。 | 高 |
| `audio.py` | **声源定位算法**。GCC-PHAT / SRP-PHAT 估计水平方向，输出 `azimuth_deg`、`tdoa_s`、`speech_confidence`、`doa_confidence`。含平滑、左右分类、电机噪声抑制。 | 高 |
| `audio_device.py` | **音频采集**。用 ffmpeg DirectShow 读 Windows 麦克风，维护最近音频缓存供 TalkNet 使用。 | 中 |
| `audio_denoise.py` | 可选的 DeepFilterNet 降噪。**注意**：降噪后的音频只给 ASD 用，**不要**送回 GCC-PHAT 定位。 | 低 |
| `camera.py` | **相机与视觉前端**。ffmpeg 读 MJPEG，支持 裁切/缩放/多后端人脸检测（MediaPipe / SCRFD / Haar），输出人脸框、嘴部运动、人体目标，并做短时稳定和 ID 跟踪。 | 高 |
| `vision.py` | **数据结构与几何**。`FaceTarget`、`BodyTarget`、主目标选择、`normalized_center_error()`（算目标相对瞄准点的偏差）。 | 中 |
| `asd.py` | **Active Speaker Detection 后端选择**。`rules`（轻量启发式）或 `talknet`（神经网络）。 | 中 |
| `talknet_adapter.py` | TalkNet 官方模型的适配层。默认找 `third_party/TalkNet-ASD/` 和 `pretrain_*.model`。**硬性要求 CUDA**。 | 低 |
| `head.py` | **舵机目标计算**。手动 jog、音频方位→yaw 步进、视觉误差→yaw/pitch 步进，含死区、最大/最小步长、轴范围钳制。 | **高**（方向符号，改错会让机器人转反） |
| `servo_bus.py` | **真实舵机通信**。串口扫描、ZP 协议读写位置、双轴硬件封装。 | **高** |
| `orchestrator.py` | 经典状态机：`LISTENING → AUDIO_CANDIDATE → SEEK_VISUAL → VISUAL_CONFIRM → TRACK_SPEAKER → HOLD/COOLDOWN`。crowded 策略的主逻辑已移到 `web/app.py`，这里仍提供 `audio_ready` 等判断。 | 中 |
| `settings.py` | **配置读写**。默认值、字段校验、持久化到 `locator_demo_settings.json`。 | 中 |
| `devices.py` | 设备发现。调 ffmpeg 列 DirectShow 音视频设备，按优先级自动选设备。 | 低 |

### `src/locator_demo/web/static/` — 前端

| 文件 | 作用 |
| --- | --- |
| `index.html` | 页面结构 |
| `app.js` | 状态轮询、WebSocket、按钮绑定、画布绘制（人脸框、准星、声源扇形） |
| `styles.css` | 样式 |

前端框的语义：**普通框** = 检测到的人脸；**绿框** = 在说话的候选；
**红框** = 当前锁定的目标（不是身份识别，只表示"正在跟这个人"）。

### `models/` — 模型文件

| 文件 | 何时需要 |
| --- | --- |
| `face_landmarker.task` | 用 MediaPipe Tasks 做人脸关键点时**必需**（默认后端） |
| `pose_landmarker_lite.task` | 用 MediaPipe Pose 辅助重取景时必需 |
| `scrfd.onnx` | 用 SCRFD 人脸检测器时必需（远距离比 MediaPipe 稳） |

**这三个都不要删。** 缺 `face_landmarker.task` 会导致默认的人脸检测完全不可用。

### `third_party/TalkNet-ASD/`

TalkNet 的官方实现（git submodule）。用 `asd_backend = talknet` 时需要。
**权重不在 git 里**（上游 gitignore 了 `*.model`），要单独下载 ~63MB，见第 3 节。
没有权重不会报错，会自动 fallback 到 `rules`。

### `docs/`

| 文件 | 内容 |
| --- | --- |
| `ENVIRONMENT_SETUP_CN.md` | **环境安装踩坑记录**（Windows + CUDA）。首次部署必读。 |
| `ALGORITHM_CONTROL_LOGIC_CN.md` | 算法与控制逻辑的长篇说明 |
| `QUICK_START_CN.md` | 现场演示的简版操作手册 |
| `USAGE_CN.md` | 各模块用法说明 |
| `HARDWARE_DEBUG.md` | 硬件排查（舵机、相机、音频） |
| `CODEX_SETUP.md` | 给编码助手看的接手说明 |

### `tests/`

回归测试。**注意见第 8 节的说明**——当前有 27 个测试是失败的，且属于仓库自身的陈旧问题，不是环境问题。

### `tools/gpu_probe.py`

单独写的 **GPU 容量探针**（不参与运行）。用于测：TalkNet 和 SCRFD 两个 CUDA 模型同进程共存时各占多少显存、DLL 会不会打架。

```powershell
python tools\gpu_probe.py
```

### 配置文件

| 文件 | 说明 |
| --- | --- |
| `locator_demo_settings.example.json` | **模板**，方便迁移或重置 |
| `locator_demo_settings.json` | **本机真实配置**，运行时自动保存。已 gitignore。含 COM 口、舵机 ID、设备名、策略参数 |

---

## 3. 环境安装

> 完整踩坑记录见 [`docs/ENVIRONMENT_SETUP_CN.md`](docs/ENVIRONMENT_SETUP_CN.md)，这里只给结论。

### 3.1 系统依赖

```powershell
winget install --id Gyan.FFmpeg -e      # 必需，读相机和麦克风都靠它
winget install --id Git.Git -e
```

**装完必须重开终端**，PATH 才生效。验证：`ffmpeg -version`

### 3.2 Python 环境

```powershell
conda create -n demo python=3.11 -y
conda activate demo
```

> 若 `conda activate` 报"禁止运行脚本"，执行一次
> `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` 后重开终端。

### 3.3 依赖（⚠️ 有三个坑）

```powershell
python -m pip install -r requirements.txt
```

**坑 1 — `onnxruntime-gpu` 要钉版本。** `requirements.txt` 里的 `>=1.17` 会装到需要
CUDA 13 的版本，而 torch 通常是 CUDA 12，对不上会**静默降级到 CPU**：

```powershell
python -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.22.0
python -m pip install coloredlogs
```

**坑 2 — insightface 会把 GPU 版 onnxruntime 覆盖成 CPU 版。** 上面的
`--force-reinstall` 就是修这个。验证：`python -c "import onnxruntime as o; print(o.get_available_providers())"`
必须看到 `CUDAExecutionProvider`。

**坑 3 — 项目路径不能有中文。** MediaPipe 的原生加载器在 Windows 上打不开含非 ASCII 的
路径，报错却是"文件不存在"。项目必须放在纯英文路径下。

### 3.4 CUDA 版 PyTorch

torch 的 wheel 有 2.4GB，不在 PyPI 上：

```powershell
# 国内建议用镜像，先下载再本地装
curl.exe -L -C - --retry 30 --retry-all-errors `
  -o torch.whl "https://mirrors.aliyun.com/pytorch-wheels/cu124/torch-2.6.0%2Bcu124-cp311-cp311-win_amd64.whl"

python -m pip install --find-links . torch torchvision torchaudio
```

> **不要加 `--no-index`**，否则 torch 的依赖（jinja2、sympy 等）也取不到。

验证：

```powershell
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

### 3.5 `websockets` —— ⚠️ 最容易漏的一个

**uvicorn 自己不会处理 WebSocket**，缺 `websockets` 包时它对所有升级请求返回 **HTTP 404**。

后果极具误导性：**页面能正常打开、所有接口都返回 200、但摄像头全黑且所有按钮无反应**。
因为整个应用循环是 `/ws` 驱动的（它是 `tick()` 的唯一调用者，而 `tick()` 是唯一读相机的地方）。

```powershell
python -m pip install websockets
```

**装完必须重启 server。** 已在 `requirements.txt` 中，但值得单独强调。

### 3.6 TalkNet 子模块与权重

```powershell
git submodule update --init third_party/TalkNet-ASD

# 权重不在 git 里，单独下载
curl.exe -L -o third_party\TalkNet-ASD\pretrain_TalkSet.model `
  https://huggingface.co/AlekseyKorshuk/talknet-asd/resolve/main/pretrain_TalkSet.model
```

正确的 SHA256：`d985ddd07dca08864a28337726f5b7d91b6426bebc7327f0e9b21cf9ff1cc937`

### 3.7 验证环境

```powershell
python tools\gpu_probe.py       # GPU 容量与双模型共存
ffmpeg -hide_banner -list_devices true -f dshow -i dummy   # 列出相机/麦克风
```

---

## 4. 启动

### 模拟模式（不接硬件）

```powershell
conda activate demo
cd <项目路径>
$env:LOCATOR_DEMO_SIM="1"
python run_server.py
```

### 真实硬件模式

```powershell
conda activate demo
cd <项目路径>
Remove-Item Env:\LOCATOR_DEMO_SIM -ErrorAction SilentlyContinue
$env:LOCATOR_DEMO_PORT="8010"      # 可选，默认就是 8010
python run_server.py
```

浏览器打开 **http://127.0.0.1:8010/**

停止：在运行 server 的窗口按 `Ctrl+C`。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `LOCATOR_DEMO_SIM=1` | 模拟模式，不连真实舵机/设备 |
| `LOCATOR_DEMO_PORT` | 端口，默认 8010 |
| `LOCATOR_FACE_LANDMARKER_MODEL` | 覆盖人脸模型路径（路径含中文时的绕行方案） |
| `LOCATOR_POSE_LANDMARKER_MODEL` | 覆盖 pose 模型路径 |
| `LOCATOR_SCRFD_MODEL` | 覆盖 SCRFD 模型路径 |
| `LOCATOR_TALKNET_REPO` / `LOCATOR_TALKNET_MODEL` | 覆盖 TalkNet 仓库/权重路径 |

---

## 5. 首次调试流程（必按顺序）

> **核心原则：一次只动一个东西。** 同时开多个自动功能会分不清是哪个出了问题。

### ① 只开摄像头预览

打开 `摄像头`，**保持 `听觉转头` 和 `视觉跟踪` 关闭**。

改变头部朝向看画面是否跟着变。**看不到画面** → 见第 9 节。

### ② 扫描舵机、填 ID

先确认舵机总线**已通电**，然后点 **`扫描舵机`**。

> 扫描是**只读探测**（发 `#NNNPID!` 查询），**不会让舵机动**，可以放心点。

把扫描到的 ID 填进 `Yaw ID` / `Pitch ID`，点 `保存轴 ID`。

**常见配置**：yaw = `0`，pitch = `2`，但**以实际扫描结果为准**。

> ⚠️ **`Yaw ID` 为空会怎样？** 代码里 `yaw_id is None` 时**根本不会创建 yaw 轴**，
> 症状是"**只能上下动，完全不能左右动**"。这极易被误判成舵机坏了、供电不足或机械卡死，
> 从而去拆机。**先查这个，再查硬件。**

### ③ 手动测试方向

用方向按钮小步测试（步长先用 30-60）。确认：

- 按 `左` → 头**真的往左转**
- 按 `上` → 头**真的往上抬**

**反了**就在 `左键` / `上键` 下拉框里改 `Yaw 反转` / `Pitch 反转`，然后点 `保存方向`。

> **这一步不能跳过。** 方向反了之后再开自动追踪，会越追越偏。

### ④ 设置安全范围

默认 `1200..1800`。如果某个舵机在某段抖动或顶到机械限位，把范围避开那一段，点 `保存范围`。

### ⑤ 测音频

打开 `听觉转头`（`听觉转头` = 用声音控制头部转动）。

对着麦克风说话，**分别靠近左麦和右麦**，观察左侧「声源定位」扇形图的指示。

**左右反了** → 打开 `声道 → 交换`，点 `保存声道`。

### ⑥ 校准视觉瞄准点

这是本项目**最容易被忽略的一步**。

画面里那个十字准星**不在正中央是正常的**——它是「瞄准点」，用来补偿"相机装在头部侧面、
不在旋转轴上"的固定偏差（默认 `Target X = 0.04`，即右偏 4% 的画面宽度）。

**但默认值不一定匹配你的安装**。校准方法：

1. 用方向按钮**手动把头转到正对着一个人**
2. 点右下角 **`Record visual sample`**
3. 结果最后一组数字 `target x_norm,y_norm` 就是答案：

```
pose 1560/1555 · track 412.3,176.5 · px err 66.7,-3.5 · model d ... · target 0.144,-0.010
                                                                          ↑↑↑ 填这两个
```

4. 把它填进 `Target X` / `Target Y`，改完按回车（字段是**自动保存**的）

### ⑦ 最后才开自动追踪

确认前面六步都对，再打开 `视觉跟踪`，选策略，点 `开始`。

---

## 6. Dashboard 参数详解

### 硬件

| 参数 | 含义 |
| --- | --- |
| `串口` | 舵机总线的 COM 口（CH340 芯片） |
| `Yaw ID` / `Pitch ID` | 舵机 ID，**必须扫描后填** |
| `Pitch min / center / max` | 俯仰轴安全范围，默认 1200/1500/1800 |
| `Yaw 转头` / `Pitch 抬头` | 当前目标位置 |

### 方向

| 参数 | 含义 |
| --- | --- |
| `左键` / `上键` | 方向反转开关 |
| `步长` | 手动点动一次走多少（默认 60） |

### 控制

| 参数 | 含义 |
| --- | --- |
| `控制 → 稳健/快速` | 运动更新频率。`稳健` 更保守，`快速` 更灵敏 |
| `保护(ms)` | **舵机转动后抑制音频决策的时间**，防止电机噪声被当成声源 |

### 视觉

| 参数 | 含义 |
| --- | --- |
| `摄像头` | 打开/关闭相机采集 |
| `Face detector` | `mediapipe`（近距）/ `scrfd`（远距更稳） |
| `视觉跟踪` | 用相机数据控制舵机 |
| `Strategy` | `classic audio first` / `crowded visual first` |
| `ASD` | `rules`（启发式）/ `talknet`（神经网络，需 CUDA） |
| `Lock policy` | 锁定策略：`turn_hold` / `until_lost` / `interruptible` |
| `Visual yaw` | `off` / `small` / `full` |
| `Yaw dead` | **死区**。目标偏离准星小于此值时不动作。画面宽度比例 |
| `Yaw step` | 最小步长。设为 0 时微小误差不产生动作 |
| `Yaw max` | 单次最大步长。防止大跳 |
| `Target X` / `Target Y` | 瞄准点偏移，**用 `Record visual sample` 校准** |
| `Near face` | 人脸高度/帧高 的阈值，小于此值视为"太远"，不参与锁定 |
| `Keep face` | 已有锁定目标的保留阈值，通常比 `Near face` 小 0.03 左右 |
| `Silent visual search` | 画面里有人但不说话时，是否允许音频去画面外搜人 |
| `Silent hold s` | 静默保持多久后才允许音频搜索 |
| `Audio interrupt` | 红框锁定后是否允许外部声音打断。现场通常**关** |

> **字段是自动保存的**——改完按回车或点别处即刻写入，没有保存按钮。

---

## 7. 策略怎么选

### 嘈杂现场最稳（open day、旁边很多人说话）

```
Strategy: crowded visual first
ASD: talknet（不稳定就退回 rules）
Lock policy: until_lost
Silent visual search: OFF
Audio interrupt: OFF
Near face: 0.15 - 0.20
Keep face: Near face - 0.03（例如 0.18 / 0.14）
Control: 稳健
```

含义：画面里有近距离候选人时，不被画面外噪声带走；只有画面里的说话人被确认后才锁定。

### 安静环境 / 需要找画面外的人

```
Strategy: crowded visual first
Silent visual search: ON
Silent hold s: 0.8 - 1.2
```

含义：画面里有人但没说话时先短暂保持；外部声源稳定且超过 hold 时间后，允许音频 yaw 搜索。

### 只测声源定位

```
Camera: 可关
Visual tracking: OFF
Audio: ON
Strategy: classic audio first
```

---

## 8. ⚠️ 注意事项

### 8.1 硬件

- **舵机总线必须先通电再启动软件**，否则扫描不到 ID。
- **相机是独占设备**：同时开两个 server 会导致后启动的那个拿不到相机（表现为 `status: starting` 但永远不出图）。
- **关掉某个功能再打开，不一定能立刻恢复**：
  - 相机——采集器内部有自恢复逻辑，只要 WebSocket 正常驱动就没事
  - 音频——关掉后要重新打开才会重新采集
- **舵机首次移动会发 `PULR`（恢复力矩）+ `PMOD1`（设模式）**，有约 0.25-0.5 秒延迟，属正常。
- **`电机保护` 与高频运动冲突**：`保护(ms)` 是每次运动后抑制音频的时长。如果运动变得很频繁
  （见 8.3），这个窗口会不断叠加，**同时开「音频+视觉」时可能导致音频被一直压制**。
  两者一起用时建议把 `保护` 降到 80-120。

### 8.2 开发

- **不要改 `head.py` 里方向符号的默认值**。它直接关系真实舵机的机械标定，改错会让机器人转反，
  而且症状（"能转但总转错方向"）很难归因。要调就在 dashboard 里改，那是运行时配置。
- **`requirements.txt` 没有 CUDA 版本约束**，换机器重装必须按 3.3 节手动修
  `onnxruntime-gpu`。
- **三个 opencv 变体共存**（`opencv-python`、`opencv-python-headless`、
  `opencv-contrib-python`）。后两个是 insightface 和 mediapipe 拉进来的，功能不冲突，
  但服务器场景理论上只需 headless。
- **路径不能有中文**（见 3.3 坑 3）。

### 8.3 运动平滑度调优

如果感觉头部"一段一段地动"而不是平滑跟随，那是**粘滑循环**：

```
误差 < 死区      → 完全不动作
误差 > 死区      → 一步跳满（常被 max 截断成固定值）
跳完误差变小      → 又跌回死区内 → 冻结
目标继续移动      → 误差重新累积 → 再跳一次
```

**默认 `Visual yaw = small` 的死区是 0.18（画面宽度的 18%）**，对于 640 宽的帧就是
115 像素——目标要横移这么多才触发一次动作，所以动起来是一顿一顿的。

**调法**（全在 dashboard 上，不用改代码）：

| 参数 | 从 | 调到 | 作用 |
| --- | --- | --- | --- |
| `Yaw dead` | 空（实际 0.18） | **0.03** | 死区缩小 6 倍 ← 最关键 |
| `Yaw step` | 0 | **2** | 微小误差也能产生微小动作 |
| `Yaw max` | 空（实际 30） | **30** | 保持上限防大跳 |
| `控制` | 稳健 | **快速** | 更新率 4.5Hz → 5.5Hz |

改完后步长会随误差**成比例变化**（3%→6步、10%→20步、15%+→30步），而不是恒定跳 30 步。

**想再进一步**需要改代码：`_profile_params()` 里的 `motion_interval_s`（限制最快多久发一次
运动指令）没有暴露在 dashboard 上，默认 0.18-0.22 秒。把它降到 0.06 并让
`move_time_ms` 保持在 120 左右，舵机会在一条指令没走完时收到下一条，**连续不停运动**——
这才是串口舵机能达到的最平滑状态。

### 8.4 测试套件是陈旧的

运行 `python -m unittest discover -s tests` 会看到 **124 个测试里 24 个失败 + 3 个错误**。

**这不是环境问题，也不是依赖版本问题**，是仓库自身的陈旧测试：

- 测试调用了源码里**根本不存在的 API**（如 `FfmpegAudioLocalizer._voice_gate`、
  `DemoRuntime.shutdown`）
- 有的测试与代码默认值**互相矛盾**（`test_head.py` 里两个测试对
  `yaw_left_sign` 默认值的要求相反）

**看到这批失败不要试图去降级 numpy/scipy 等包**——把时间浪费在错误的方向上。
要修得先确定作者意图，尤其是方向符号。

---

## 9. 故障排查

### 页面能打开，但摄像头全黑 + 所有按钮无反应

**几乎一定是缺 `websockets` 包。** 见 3.5。

后端所有 `/api/*` 都正常返回 200、响应只要 2ms，很容易误判成前端问题。

**确认方法**：用 WebSocket 客户端连 `/ws`。正常会打印
`"WebSocket /ws" [accepted]`；拿到 **404** 就是这个问题。
另外 `/api/state` **不触发 tick**（它只读快照），所以用 curl 轮询它**看不出异常**。

### 只能上下动，不能左右动

**先查 `Yaw ID` 是不是空的。** 见 5-②。不要先去拆硬件。

### TalkNet 显示 fallback

依次检查：
1. `third_party/TalkNet-ASD/talkNet.py` 是否存在（submodule 拉了吗）
2. `pretrain_TalkSet.model` 或 `pretrain_AVA.model` 是否存在
3. `python -c "import torch; print(torch.cuda.is_available())"` 是否为 `True`
   （**TalkNet 硬性要求 CUDA**，没 GPU 直接不可用）
4. `python_speech_features` 是否装了

### SCRFD 不生效

1. `models/scrfd.onnx` 是否存在
2. `onnxruntime.get_available_providers()` 里有没有 `CUDAExecutionProvider`
   （没有就是 3.3 的坑 1/坑 2）
3. Dashboard 里 `Face detector` 是否选了 `scrfd`

### 前端看不到新控件

`Ctrl + F5` 硬刷新。还不行就停掉 server 重开。

### 相机卡住 / 不出图

1. 关掉 `摄像头` 开关
2. 停掉 server
3. 检查有没有残留的 ffmpeg 进程：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'ffmpeg' } |
  Select-Object ProcessId, CommandLine
```

### 远处的人也被框到、容易误触发

提高 `Near face`（例如 0.18），`Keep face` 设成 0.14 左右。
**注意比例基于后端处理帧尺寸，不是相机原始分辨率。**

---

## 10. 开发与测试

```powershell
conda activate demo
cd <项目路径>
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests
```

前端语法检查（需 Node.js）：

```powershell
node --check src\locator_demo\web\static\app.js
```

### 交付前该保留什么

**运行必需**：`run_server.py`、`requirements.txt`、`src/locator_demo/`、
`models/`（用哪个留哪个）、`locator_demo_settings.example.json`

**用 TalkNet 才需要**：`third_party/TalkNet-ASD/` + `pretrain_*.model`

**建议保留**：`docs/`、`tests/`、`tools/`

**不要交付**：`.venv/`、`__pycache__/`、`.tmp/`、
`locator_demo_settings.json`（含本机 COM 口和设备名，不该覆盖别人的）

---

## 附：GPU 使用情况

在一张 8GB 卡（RTX A1000）上实测：

| 组件 | 用 GPU | 触发条件 |
| --- | --- | --- |
| TalkNet | ✅ 硬性要求 CUDA | `ASD = talknet` |
| SCRFD | ✅ onnxruntime-gpu | `Face detector = scrfd` |
| MediaPipe | ❌ 纯 CPU | 默认后端 |

**默认配置（mediapipe + rules）完全不占 GPU。**

两个 CUDA 模型同时开时合计约 **300 MiB**，8GB 卡余量充足。
而且 onnxruntime 会自动复用 torch 已加载的 CUDA/cuDNN 动态库
（它启动时会打印 `Skip loading CUDA and cuDNN DLLs since torch is imported.`），
**不需要为 Windows 上常见的 DLL 冲突做任何特殊处理**。

用 `python tools\gpu_probe.py` 可以在任何一台新机器上复测这些数字。
