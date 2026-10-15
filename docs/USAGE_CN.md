# 定位机器人 Demo 中文使用说明

这份说明基于当前 codebase 阅读整理，目标是让你自己按命令完成安装、启动和硬件配置。项目建议先用模拟模式确认网页和控制逻辑正常，再切到真实硬件。

## 这个项目做什么

这是一个“听声定位 + 双轴机器人头 + 可选视觉跟踪”的 Windows demo：

- 后端用 FastAPI/uvicorn 提供网页、REST API 和 WebSocket 状态推送。
- 音频用 `ffmpeg` 的 DirectShow 输入读取双声道麦克风，估计左右声源方向。
- 相机用 `ffmpeg` 的 DirectShow 输入读取 MJPEG，再用 OpenCV Haar cascade 检测人脸。
- 舵机用串口控制 Zhongling/ZP 总线舵机，默认 `115200` 波特率。
- Dashboard 可以保存串口、yaw/pitch 舵机 ID、运动方向、相机、功能开关、音频左右声道映射。
- 机器相关设置保存到 `locator_demo_settings.json`，这个文件不提交 git。

## 需要下载或准备

必须项：

- Windows 10/11。
- Python 3.10 或更新版本。建议从 python.org 安装，并勾选 Add Python to PATH。
- Git。如果你要重新 clone 仓库，还需要 SSH 或 HTTPS 访问权限。
- FFmpeg，并确保 `ffmpeg.exe` 在 PATH 中。Windows 推荐 `winget install Gyan.FFmpeg`。
- Python 依赖：不用逐个下载，运行 `python -m pip install -r requirements.txt` 会安装：
  - `fastapi`
  - `uvicorn`
  - `pyserial`
  - `httpx`
  - `opencv-python-headless`

可选项：

- Node.js：只用于检查前端 JS 语法，运行 demo 不需要。
- CH340/USB 串口驱动：如果插上舵机串口后看不到 `COMx`，需要安装。

硬件项：

- CH340 或其他 USB 串口转接器。
- Zhongling/ZP 协议总线舵机。
- 双声道麦克风。
- DirectShow 摄像头。普通摄像头通常用 `1280x720`；双目 USB 相机可能用 `2560x800` 并裁切左半幅。

## 一次性安装命令

在 PowerShell 中进入项目目录：

```powershell
cd E:\demo\positioning_robots
```

创建并激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 `python` 打开的是 Microsoft Store 或提示找不到 Python，需要先安装真正的 Python，或把 Python 安装目录加入 PATH。

## 软件和设备检查命令

检查 Python：

```powershell
python --version
```

检查 FFmpeg：

```powershell
ffmpeg -version
```

列出摄像头和麦克风：

```powershell
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

列出某个摄像头支持的模式，把名字替换成你的设备名：

```powershell
ffmpeg -hide_banner -list_options true -f dshow -i video="Integrated Camera"
```

列出串口：

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
```

运行 Python 测试：

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests -v
```

可选：检查前端 JS 语法：

```powershell
node --check src\locator_demo\web\static\app.js
```

## 启动方式

先跑模拟模式，不需要硬件：

```powershell
$env:LOCATOR_DEMO_SIM='1'
python run_server.py
```

浏览器打开：

```text
http://127.0.0.1:8010/
```

确认网页能打开、WebSocket 能实时刷新、手动点动会改变模拟 yaw/pitch 值。

切到真实硬件模式：

```powershell
Remove-Item Env:\LOCATOR_DEMO_SIM -ErrorAction SilentlyContinue
python run_server.py
```

如果要换端口：

```powershell
$env:LOCATOR_DEMO_PORT='8020'
python run_server.py
```

## Dashboard 推荐配置顺序

1. 先关闭 `摄像头`、`听声转头`、`视觉跟踪` 三个开关，只做手动舵机检查。
2. 点击 `扫描舵机`，查看总线能找到哪些 ID。
3. 填写并保存：
   - `串口`：例如 `COM15`；留空或 `auto` 时程序会优先找 CH340。
   - `Yaw ID`：当前参考硬件常见值是 `0`。
   - `Pitch ID`：当前参考硬件常见值是 `2`。
4. 把 `步长` 设小一点，比如 `30` 或 `60`，用方向键测试。
5. 如果按 `左` 头却向右转，调整 `左键` 下拉框：
   - `Yaw 减小`
   - `Yaw 增大`
6. 如果按 `上` 头却向下动，调整 `上键` 下拉框：
   - `Pitch 减小`
   - `Pitch 增大`
7. 点击 `保存方向`。运动命令会被限制在 `1200..1800` 安全脉宽内。
8. 打开 `摄像头`，选择相机、尺寸和裁切方式，再点击 `保存相机`。
9. 打开 `听声转头`，分别靠近麦克风左右两侧说话或拍手，看 dashboard 的方向是否正确。
10. 如果左右反了，把 `声道` 改成 `左右互换`，点击 `保存声道`。
11. 手动方向、相机预览、音频左右都正确后，再打开 `视觉跟踪`。

## 相机配置怎么填

Dashboard 的相机配置会调用：

```text
POST /api/config/camera
```

字段含义：

- `摄像头`：DirectShow 设备名。留空时自动选设备。
- `尺寸`：传给 ffmpeg 的 `-video_size`，如 `1280x720` 或 `2560x800`。
- `裁切`：
  - `自动`：如果尺寸是 `2560x800`，默认裁左半幅；否则不裁切。
  - `左半幅`：用于双目/宽幅相机，只取左半张画面。
  - `不裁切`：普通摄像头常用。

典型配置：

- 普通 webcam：`1280x720`，`不裁切`。
- 双目 USB camera：`2560x800`，`左半幅`。

## 麦克风和声道映射

当前网页没有单独的“麦克风设备选择”输入框，真实音频设备由后端自动选择 DirectShow audio 设备。选择逻辑会优先匹配：

- 显式传入的设备名，如果代码里设置了；
- `YDM2MIC`;
- `Realtek`;
- `Virtual Desktop Audio`;
- 否则使用找到的第一个音频设备。

Dashboard 里的 `声道` 只控制左右映射：

- `正常`：保持原始左右声道。
- `左右互换`：把 left/right、left_front/right_front、left_back/right_back 镜像，同时把 TDOA 符号取反。

如果你需要在网页上手动选择麦克风，当前 codebase 还没有对应 UI/API，需要额外加一个 `audio.device_name` 配置项。

## 代码结构注释

入口：

- `run_server.py`：把 `src` 加入 `sys.path`，读取 `LOCATOR_DEMO_PORT` 和 `LOCATOR_DEMO_SIM`，启动 uvicorn。

后端 Web：

- `src/locator_demo/web/app.py`：核心运行时和所有 API。`DemoRuntime` 管理配置、舵机、音频、视觉、状态机和持久化设置。
- `GET /api/session`：返回当前配置。
- `GET /api/devices`：通过 ffmpeg 枚举 DirectShow 摄像头/麦克风。
- `POST /api/config/servos`：保存串口、yaw ID、pitch ID。
- `POST /api/config/direction`：保存左/上方向映射和手动步长。
- `POST /api/config/features`：保存摄像头、听声、视觉跟踪开关。
- `POST /api/config/camera`：保存相机名、尺寸、裁切。
- `POST /api/config/audio`：保存左右声道是否互换。
- `POST /api/head/jog`：方向键点动。
- `POST /api/head/move`：直接发送 yaw/pitch 脉宽。
- `WebSocket /ws`：每 0.1 秒推送状态，约每 0.5 秒带一帧图像。

配置：

- `src/locator_demo/settings.py`：定义 `SavedSettings`，读写 `locator_demo_settings.json`。
- `locator_demo_settings.example.json`：配置文件示例。

舵机：

- `src/locator_demo/servo_bus.py`：串口扫描、ZP 舵机协议、双轴硬件封装。
- `choose_servo_port()` 会优先选择描述里包含 `CH340`、`1A86:7523` 或 `usb-serial` 的串口。
- 舵机命令格式类似 `#002P1500T1000!`。
- 每个舵机第一次移动前会发 `PULR` 恢复力矩和 `PMOD1` 设置模式。

头部运动逻辑：

- `src/locator_demo/head.py`：安全范围、中心点、方向映射、点动、视觉误差转 yaw/pitch。
- 默认中心是 `1500`，安全范围是 `1200..1800`。
- 默认 `左` 表示 yaw 减小，`上` 表示 pitch 减小；可以在网页改。

音频：

- `src/locator_demo/audio.py`：TDOA 分类、平滑器、左右声道互换。
- `src/locator_demo/audio_device.py`：用 ffmpeg 读取双声道 PCM，并在后台线程里估计方向。
- 声音方向需要连续稳定命中，避免单帧误触发。

视觉：

- `src/locator_demo/camera.py`：用 ffmpeg 读取相机 MJPEG，裁切/缩放后送到网页，并用 OpenCV 检测人脸。
- `src/locator_demo/vision.py`：选择最大目标框，计算目标中心相对画面中心的归一化误差。

状态机：

- `src/locator_demo/orchestrator.py`：`listening`、`turning_to_sound`、`visual_acquire`、`tracking` 等模式切换。
- 有人脸目标时进入 `tracking`。
- 连续两次稳定音频方向后才认为声源有效。
- 目标丢失超过超时会回到 `listening`。

前端：

- `src/locator_demo/web/static/index.html`：Dashboard 布局。
- `src/locator_demo/web/static/app.js`：请求 API、连接 WebSocket、绘制声源方向、更新相机预览和舵机控件。
- `src/locator_demo/web/static/styles.css`：页面样式。

测试：

- `tests/test_audio.py`：音频方向、TDOA、平滑器。
- `tests/test_audio_device.py`：ffmpeg 音频读取不阻塞、近场左右能量差判断。
- `tests/test_camera.py`：相机滤镜、裁切和缩放。
- `tests/test_devices.py`：DirectShow 设备解析。
- `tests/test_head.py`：安全范围、方向映射、视觉误差、点动。
- `tests/test_orchestrator.py`：状态机。
- `tests/test_servo_bus.py`：串口协议、扫描、双轴移动。
- `tests/test_web_app.py`：API、配置持久化、模拟运行、开关行为。

## 常见问题

### 网页打开但没有相机画面

先运行设备枚举命令，确认 DirectShow 能看到摄像头。然后在 dashboard 里选中设备，尝试 `1280x720`。如果是双目宽幅相机，尝试 `2560x800` + `左半幅`。

### 相机卡住或被占用

先关闭 dashboard 的 `摄像头` 开关；必要时停止服务。检查残留 ffmpeg：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'ffmpeg' -and $_.CommandLine -match 'video=|Camera' } |
  Select-Object ProcessId,Name,CommandLine
```

### 听声方向反了

把 `声道` 改成 `左右互换` 并保存。这个只改软件映射，不需要换线。

### 按方向键运动反了

修改 `左键` 和 `上键` 两个下拉框，然后保存方向。先用小步长测试。

### 扫不到舵机

检查舵机供电、串口号、CH340 驱动、波特率和接线。程序默认 `115200`，扫描范围是 `0..15`。

### 想重置所有校准

停止服务后删除：

```powershell
Remove-Item .\locator_demo_settings.json
```

