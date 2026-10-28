# 第一阶段：新硬件采集与连续运动控制

2026-10-21。已修改主程序，迁移本机与示例配置，完成真实音视频采集和离线运动验证。舵机控制板未连接，本轮没有向舵机或麦克风串口发送运动命令。

## 1. 当前运行方式

在 `positioning_robots` 目录中启动：

```powershell
conda activate demo
python -m pip install -r requirements.txt
python run_server.py
```

打开 `http://127.0.0.1:8010/`。系统还需要 PATH 中可用的 FFmpeg。新音频依赖 `sounddevice>=0.5.6`。本机配置已保留原有功能开关：相机开、声音控制关、视觉控制关；启动采集不会自动启动头部运动。

默认配置：

```json
{
  "camera": {
    "capture_backend": "stereo_mjpeg",
    "device_name": "USB Video Device",
    "usb_vid": "1BCF", "usb_pid": "0B15",
    "video_size": "3840x1080", "input_fps": 60,
    "fps": null, "output_width": 1920, "crop_left_half": false
  },
  "audio_capture": {
    "backend": "wasapi", "device_name": "YDPI4MIC",
    "usb_vid": "EF09", "usb_pid": "459D",
    "channels": 4, "sample_rate": 48000, "sample_bits": 16,
    "block_ms": 20
  }
}
```

`input_fps` 是向摄像头请求的驱动档位；`fps` 是视觉模型处理上限，null 表示随采集能力读取最新帧。它们不会把实测 33 fps 变成 60 fps。`output_width` 只影响单眼算法输入，不改变 3840×1080 原始双目采集。

本机原配置备份：`../../hardware_inventory/stage1/settings-before-upgrade.json`。迁移工具 `tools/apply_upgrade_profile.py` 会设置选定规格，平时无需重复运行。

## 2. 设备识别和数据路径

- 相机按 USB VID/PID 定位 DirectShow 接口，使用设备路径打开，避免同名或插拔导致选错相机。原始 MJPEG 直接复制到 Python，不在采集端做降帧、裁切、缩放或重新编码。
- `StereoCameraCapture` 独立读线程保留原始左右目、帧序号和主机接收时间。采集历史和统计窗口有界，模型慢时不会让推理结果排队增长。
- `StereoVisionTracker` 在独立线程处理最新帧。目前原有二维检测器明确读取左目 1920×1080，同时向界面提供完整双目预览。右目保留不代表已实现视差、深度或同步精度标定。
- 四麦通过 WASAPI 共享模式采集 4×48 kHz×16 bit，20 ms 一块。每块是 960 个四通道采样帧、7680 字节。缓存保留全部四路，连续 WAV 通过独立写盘线程输出，并统计缓冲溢出、录音丢块和写盘错误。
- 可选 ASD 的单声道输入是四路采集后的派生平均信号，原始四路数据不被覆盖。
- 音频 ADC 时间映射到主机单调时钟，视频记录 JPEG 到达主机时间。当前没有校准相机曝光与音频 ADC 的绝对偏移，不能将二者等同于已完成硬件音画同步。
- COM4 是 YDPI4MIC 的另一个接口，已从舵机选择中排除；显式指定它也会拒绝。没有已识别的舵机板时，串口保持空，不再回退到首个串口。

声源方向需要四个咪头的坐标、朝向及通道顺序。当前四个咪头集中放置且未标定，`WasapiArrayAudio` 返回 unknown 和零方向置信度；关闭旧 VAD 也不会生成未经标定的方向。旧双麦算法仅在 `audio_capture.backend="legacy"` 时保留使用。

## 3. 连续运动改动

原来浏览器连接驱动总控，视觉动作还会等待电机保护期；500 ms 左右的动作与保护时间叠加，容易出现“动一下、等一下”。新真实模式改为独立目标 30 Hz 总控，WebSocket 只读取快照；视觉纠偏可以在音频电机保护期间继续更新。

`SmoothMotionPlanner` 使用五次多项式，改目标时继承当前规划位置、速度和加速度，保持 C2 连续，并检查全段位置、速度、加速度及 jerk 极值。当前默认上限分别为 180、600、3000，单位是舵机位置刻度及其时间导数，不能直接称为度/秒。

`SmoothHeadHardware` 独立以 50 Hz 采样，只在整数位置变化时发送短时长命令（当前 T40 ms），双轴变化合并到一次串口写入。舵机模式与使能准备在轨迹计时前完成；正常位置流不再每次清空串口接收缓冲。状态中的位置是规划估计值，没有新增编码器闭环反馈。

速度、加速度和 jerk 限制是软件轨迹约束。舵机板对连续 T40 ms 命令的插值行为、负载、回差及实际机械顺滑度，需要接板后验证。本轮不修改串口协议格式，不宣称物理运动已通过测试。

## 4. 已验证结果与复测

证据统一在 `../../hardware_inventory/stage1/`。

| 检查 | 本轮结果 | 文件 |
| --- | --- | --- |
| 原始双目 + 四麦独立采集，15 秒 | 497 帧，33.13 fps；音频 720000 帧、0 溢出 | `native-both-first/validation.json` |
| 主程序、模型与连续录音同时运行，约 30 秒 | 995 帧，33.12 fps；完整录音 1441920 帧、0 溢出、0 录音丢块 | `runtime-final/validation.json` |
| 模型线程与总控节拍 | 左目处理近期 32.83 fps；总控近期 30.001 Hz | 同上 |
| 帧率排查 | 手动曝光 −8/−10、自动曝光、2560×720/120 档位均约 33 fps，曝光设置已恢复 | `camera-matrix/matrix.json` |
| USB 链路 | 摄像头 HighSpeed USB 2.0，未降到 FullSpeed | `usb-speed.json` |
| 离线连续运动 | 9 秒 451 个采样点，183 条位置命令；规划上限检查通过 | `motion-replay.json` |
| 无浏览器采集与关闭释放 | 采集正常，控制线程、相机进程、音频流均关闭，串口未打开 | `runtime-final/validation.json` |

本轮新样张接近全黑、未检测到人脸，因此模型处理帧率只代表当前场景的处理节拍，不能外推为有人脸/多人场景速度或跟踪效果。此前有可见内容的接入样张保留在 `capability-probe/`，用于说明拼接结构。原始直采、曝光与低分辨率对比均未达到 60 fps，说明限速已在当前主机接收路径中出现；具体设备固件、传感器模式或驱动原因尚未确认，不能断言某一个是根因。

自动化回归结果见 `test-results.json`：新增采集、运行时与平滑运动测试全部通过，未新增历史用例回归。完整历史测试集仍有原先已存在的 ASD/选人、方向配置及旧音频处理失败项，故不宣称全项目测试通过；具体数量以结果文件为准。

复测（摄像头同一时刻只由一个程序占用；输出目录必须是新目录）：

```powershell
python tools/probe_upgrade.py --seconds 15 --output ../hardware_inventory/stage1/recheck-native
python tools/probe_runtime_capture.py --seconds 30 --output ../hardware_inventory/stage1/recheck-runtime
python tools/probe_motion.py
python tools/validate_stage1.py
```

`probe_runtime_capture.py` 保持自动运行关闭并检查串口未打开。`probe_motion.py` 使用虚拟执行器，不接串口。曝光矩阵工具只临时改变可恢复的曝光值，测试完成会恢复原值；OpenCV 协商到了 YUY2 的试验已标为无效比较，不计入 MJPEG 帧率结论。

## 5. 接口与后续边界

- `GET /api/devices`：串口、DirectShow、WASAPI 输入，串口含音频保留标记。
- `GET /api/state`：原始采集、模型处理、四路音量、录音完整性、总控节拍和运动状态。
- `GET/POST /api/config/camera`：采集档位与处理输入分别配置。
- `GET/POST /api/config/audio-capture`：WASAPI 四通道格式；选择新后端时拒绝降为双通道。
- `GET/POST /api/config/control`：`smooth_motion`、`motion_hz`、`max_velocity`、`max_acceleration`、`max_jerk`。

下一阶段涉及的双目标定/深度、四麦几何与空间定位、多人说话人选择和整机跟踪实测，本轮未实现。3D 结构保持由另一 agent 负责，待最终模型与打印照片提供后再加入汇报。
