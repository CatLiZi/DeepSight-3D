# 环境搭建记录（Windows + 单卡 A1000）

本文记录一套**已验证可用**的安装流程，重点是 `requirements.txt` 直接装会踩到的三个坑。按本文顺序做可以避开。

已验证环境：Windows 11 + NVIDIA RTX A1000 8GB + Python 3.11 + CUDA 12.4。

## 一、四个必须知道的坑

### 坑 1：`onnxruntime-gpu` 不要装最新版

`requirements.txt` 里写的是 `onnxruntime-gpu>=1.17`，没有任何上限。pip 会装到最新版，
而 **onnxruntime-gpu 1.31 要求 CUDA 13**。如果 torch 装的是 CUDA 12（cu124），
两者对不上，报错长这样：

```text
Failed to load cublasLt64_13.dll / cudart64_13.dll
Requires cuDNN 9.* and CUDA 13.*
Failed to create CUDAExecutionProvider.
```

ORT 会**静默降级到 CPU**，不崩溃，但 SCRFD 就用不上 GPU 了（dashboard 上看不出来，
只有日志里有）。

**做法**：装 CUDA 12 对应的版本，即 `onnxruntime-gpu==1.22.0`。

### 坑 2：insightface 会把 GPU 版 onnxruntime 覆盖成 CPU 版

`insightface 2.1` 硬依赖 **CPU 版** `onnxruntime`，而 `requirements.txt` 要的是
`onnxruntime-gpu`。同一个进程里 pip 后装 CPU 版，会覆盖 `onnxruntime_pybind11_state.pyd`，
结果：

```python
onnxruntime.get_available_providers()
# ['AzureExecutionProvider', 'CPUExecutionProvider']   ← CUDA provider 消失
```

**做法**：装完 `requirements.txt` 后，强制把 GPU 版装回去：

```powershell
python -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.22.0
```

注意此时要额外补一个依赖（`--no-deps` 装不到）：`python -m pip install coloredlogs`。

验证：

```python
import onnxruntime as ort
print(ort.get_available_providers())
# 期望看到 'CUDAExecutionProvider'
```

### 坑 3：项目路径不能有中文

MediaPipe 的原生模型加载器用 C++ `ifstream`，在 Windows 上打不开含中文的路径：

```text
FileNotFoundError: Unable to open file at ...\3D人头\...\models\face_landmarker.task
```

**只有 MediaPipe 受影响**。同一路径下 ONNX Runtime 和 insightface 都正常。
但 mediapipe 是默认人脸检测后端，所以必须解决。

**做法**：项目放在纯 ASCII 路径下。本项目已放在 `C:\Users\SLAI\Desktop\DeepSight-3D\`。

如果实在不能改路径，可以用环境变量把模型指到别处（项目自带支持）：

```powershell
$env:LOCATOR_FACE_LANDMARKER_MODEL="C:\models\face_landmarker.task"
$env:LOCATOR_POSE_LANDMARKER_MODEL="C:\models\pose_landmarker_lite.task"
$env:LOCATOR_SCRFD_MODEL="C:\models\scrfd.onnx"
```

### 坑 4：缺少 `websockets`，dashboard 会整个变成死的

`requirements.txt` 原本只有裸 `uvicorn>=0.27`。**uvicorn 本身不会处理 WebSocket**——
需要额外安装 `websockets` 或 `wsproto`。缺了它，uvicorn 对**每一个** WebSocket 升级请求
直接返回 **HTTP 404**。

后果特别具有误导性，因为页面本身完全正常：

- `http://127.0.0.1:8010/` 能打开，界面控件全都在
- 所有 `/api/*` 接口都返回 200，响应只有几毫秒
- 但**摄像头预览全黑、点任何按钮都没反应**

原因是 `app.py` 里整个应用循环由 WebSocket 驱动：

```python
@app.websocket("/ws")
async def websocket(websocket: WebSocket):
    while True:
        snapshot = await asyncio.to_thread(runtime.tick, include_frame=include_frame)
        await websocket.send_json(snapshot)
        await asyncio.sleep(0.1)
```

**只有 WebSocket 连着，`tick()` 才会被调用**；`tick()` 是唯一读相机的地方。
所以 WebSocket 一断，相机不采集、状态机不转、前端拿不到状态，表现为全面"假死"。

注意 `/api/state` **不触发 tick**（它只调 `runtime.snapshot()`，纯读取），所以用 curl
轮询这个接口是看不出问题的。

**做法**：

```powershell
python -m pip install websockets
```

装完**必须重启 server**（uvicorn 在启动时就决定了有没有 WebSocket 支持）。

排查手法：用 WebSocket 客户端连一下 `/ws`。正常应看到 uvicorn 打印
`"WebSocket /ws" [accepted]`；如果拿到 HTTP 404，就是这个坑。

### 坑 5（次要）：摄像头关闭后重新开启，会留下陈旧的"已停止"状态

**已修复。** 记录在此是因为这个状态字符串在排查时很有用。

修复前 `update_feature_config()` 只有"关闭"分支，没有"启用"分支：关掉摄像头会把
`last_vision_status` 设成 `"camera off"`，但重新勾选时这个值不会被清掉，于是
`features.camera_enabled` 已经是 `True`、状态却还写着 `"camera off"`。

采集本身**不会**因此卡死——`FfmpegCameraSnapshot.read()` 内部会调 `self._start()`
自动重启。所以只要 WebSocket 在正常驱动 tick（见坑 4），相机下一拍就会恢复。问题在于
那个陈旧状态窗口：如果 tick 没在跑（WebSocket 刚断、或页面刚打开），dashboard 会一直
显示相机已停止，和复选框状态自相矛盾。

现在启用分支会调用 `_clear_visual_state()`，把状态置为 `"starting"`，如实反映
"已启用、采集尚未开始"。另外用 `camera_was_enabled` 做了转换判断，所以前端重复提交
相同配置（页面加载时会这么做）不会打断正在工作的相机。

排查时看 `/api/state` 里的 `visual.status.message`：

| 值 | 含义 |
| --- | --- |
| `camera off` | 相机被主动关闭 |
| `starting` | 已启用，ffmpeg 采集正在启动 |
| `streaming` | 正常出图 |
| `no camera frames after 5s` | 启动了但 5 秒没出图，采集器已自行重启 |

## 二、完整安装流程

### 1. 网络（国内）

pip 全局走阿里云：

```powershell
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
pip config set install.trusted-host mirrors.aliyun.com
pip config set global.timeout 120
```

CUDA 版 PyTorch 不在 PyPI 上，走阿里云的 pytorch-wheels 镜像
（实测 0.6~1.3 MB/s，官方源约 0.56 MB/s）：

```text
https://mirrors.aliyun.com/pytorch-wheels/cu124/
```

HuggingFace 用 `https://hf-mirror.com` 替换 `https://huggingface.co` 即可。

### 2. Python 环境

```powershell
conda create -n demo python=3.11 -y
conda activate demo
python -m pip install --upgrade pip
```

### 3. 非 torch 依赖

```powershell
python -m pip install -r requirements.txt
```

### 4. CUDA 版 PyTorch

torch 的 wheel 有 2.4GB，建议用 curl 下载（支持断点续传和卡死自动重试），
再本地安装：

```powershell
$base="https://mirrors.aliyun.com/pytorch-wheels/cu124/"
curl.exe -L -C - --retry 30 --retry-all-errors --speed-limit 50000 --speed-time 30 `
  -o torch.whl "$base`torch-2.6.0%2Bcu124-cp311-cp311-win_amd64.whl"

python -m pip install --find-links . torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0
```

注意**不要**加 `--no-index`，否则 torch 的依赖（jinja2、sympy 等）也取不到。

### 5. 修 onnxruntime（见坑 1、坑 2）

```powershell
python -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.22.0
python -m pip install coloredlogs
```

### 6. TalkNet 子模块

仓库里 `third_party/TalkNet-ASD` 原本是 gitlink 但没有 `.gitmodules`，
`git submodule update --init` 会报 "no submodule mapping found"。现在已补上：

```powershell
git submodule update --init third_party/TalkNet-ASD
```

权重不在 git 里（上游 gitignore 了 `*.model`），需单独下载约 63MB：

```powershell
curl.exe -L -o third_party\TalkNet-ASD\pretrain_TalkSet.model `
  https://hf-mirror.com/AlekseyKorshuk/talknet-asd/resolve/main/pretrain_TalkSet.model
```

SHA256 应为 `d985ddd07dca08864a28337726f5b7d91b6426bebc7327f0e9b21cf9ff1cc937`。

没有权重不会报错，ASD 会 fallback 到 `rules` 后端。

### 7. ffmpeg（系统依赖）

```powershell
winget install --id Gyan.FFmpeg -e
```

装完需要重开终端才认 PATH。用它读取 Windows 摄像头和麦克风。

## 三、验证

```powershell
# GPU 容量与双模型共存
python tools\gpu_probe.py

# 回归测试
$env:PYTHONPATH="$PWD\src"; python -m unittest discover -s tests

# 模拟模式启动
$env:LOCATOR_DEMO_SIM="1"; python run_server.py
# 浏览器打开 http://127.0.0.1:8010/
```

## 四、本机验证结论

### GPU 容量

A1000 共 8188 MiB。桌面常驻程序（浏览器、编辑器等）占约 1500 MiB，剩余约 6.6 GB。

| 阶段 | GPU 占用增量 | torch 分配器 |
| --- | --- | --- |
| torch CUDA context | +65 MiB | 22 MiB |
| onnxruntime CUDA session | +14 MiB | — |
| onnxruntime 首次推理（含 arena） | +66 MiB | — |
| TalkNet 推理（1 个人脸） | +140 MiB | 160 MiB |

**两个 CUDA 模型合计约 300 MiB**，8GB 卡上余量充足。

### 三个组件的 GPU 使用情况

| 组件 | 用 GPU | 触发条件 |
| --- | --- | --- |
| TalkNet | 是（硬性要求 CUDA） | `asd_backend = talknet` |
| SCRFD | 是（onnxruntime-gpu） | `face_detector_backend = scrfd` |
| MediaPipe | 否，纯 CPU | 默认后端 |

默认配置（mediapipe + rules）完全不占 GPU。

### 关键：两者共存不需要特殊处理

同一进程内同时加载 torch 和 onnxruntime-gpu，ORT 会打印：

```text
Skip loading CUDA and cuDNN DLLs since torch is imported.
```

它会自动复用 torch 已加载的 CUDA/cuDNN 运行库，避免 Windows 上常见的
DLL 版本冲突。实测两者可同时驻留并各自推理，无需手动干预。

### 实测性能

| 项目 | 数值 |
| --- | --- |
| SCRFD GPU 推理（640×640） | 6.2 ms/帧 |
| FaceLandmarker（CPU） | 1 ms/帧 |
| PoseLandmarker（CPU） | 8 ms/帧 |
| TalkNet 单次推理 | 可用，未单独计时 |

## 五、已知遗留问题

- `requirements.txt` 没有 CUDA 版本约束，换机器重装需按坑 1、坑 2 手动修。
  若要固化，建议按本文第二节的版本组合写 lock 文件。
- 三个 opencv 变体共存（`opencv-python`、`opencv-python-headless`、
  `opencv-contrib-python`，都是 5.0.0.93）。后两个分别是 insightface 和
  mediapipe 拉进来的，功能不冲突，但服务器场景理论上只需 headless。
