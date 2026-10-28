# DeepSight-3D · 视听感知机器人

当前软件版本：**R43（2026-10-28）**。双目同帧采集、四路原始音频、声音辅助选人、双轴平滑跟踪，以及本地网页控制台。

本次更新保留仓库已有的头部、身体 Blender 模型及整个 `hardware/` 目录，仅更新软件和配套资料。

## 当前能力

- 双目独立检测与跟踪，可靠关联后两眼共同计算跟随误差；遮挡或关联不明确时退回有效单眼。
- R43：黄色准星标记各自的人脸锚点，绿色圆环标记各自的期望位置，灰色小十字仅表示图像中心。正常水平视差保留，不强迫两眼的人脸同时落在图像正中心。
- 四路 48 kHz、16 bit PCM、WASAPI RAW 采集。当前定位使用左耳 CH2、右耳 CH3；身体左 CH1、右 CH4 的安装映射已确认，暂未接入控制。
- 本地 Silero VAD + 多频带可靠性加权时差估计；核心声音按时间证据锁定，视觉优先，可靠声音辅助匹配人脸。
- 声音结束后蓝色范围保持 1 秒，仅供显示；持有的核心转向目标与实时声音证据分开处理。
- 独立 50 Hz 平滑运动规划，串口自动发现、位置反馈、故障停止和本地诊断日志。

## Windows 快速运行

克隆后，**进入包含 `run_server.py` 的仓库根目录**。GitHub 仓库根目录对应实验室旧工作区的 `positioning_robots/`，不需要再进入同名子目录。

```powershell
git clone https://github.com/CatLiZi/DeepSight-3D.git
Set-Location .\DeepSight-3D

# 使用已有 demo 环境；新电脑可先 conda create -n demo python=3.11，再安装依赖
conda activate demo
python -m pip install -r requirements.txt
ffmpeg -version

# 仅首次配置；此文件是当前这台小熊的实机参数快照
Copy-Item .\configs\panda-final-r43.json .\locator_demo_settings.json

$env:LOCATOR_DEMO_PORT = "8010"
$env:LOCATOR_DEMO_SIM = "0"
python -X utf8 .\run_server.py
```

打开 <http://127.0.0.1:8010/>。看到 `Uvicorn running` 后保持终端打开；停止服务用该终端的 Ctrl+C。启动服务不会自行开启自动转头，需要在页面明确点击“开始运行”。新电脑接入硬件后先使用页面的设备刷新/串口检查，再核对通道、方向和行程。不要用旧示例覆盖已经确认好的实机配置。

本机已有环境也可直接用 `& "C:\Users\SLAI\miniconda3\envs\demo\python.exe" -X utf8 .\run_server.py`。该绝对路径仅适用于实验室电脑，其他电脑使用自己的 Python 路径。

FFmpeg 为单独安装的系统依赖，需加入 PATH。默认检测使用仓库已有 `models/scrfd.onnx`；ONNX Runtime GPU 不可用时按环境文档选择 CPU 版本。`docs/environment-observed-r43.json` 记录本次运行回归的环境版本，属于实际环境记录，不是已验证的新电脑一键安装锁文件。

### 仿真与汇报页

```powershell
# 仿真模式：启动前设置；不作为实体硬件验收
$env:LOCATOR_DEMO_SIM = "1"
python -X utf8 .\run_server.py

# 另一个终端启动不含现场人脸/录音的汇报摘要
python -m http.server 8769 --bind 127.0.0.1 --directory .\docs\report
```

汇报摘要也可直接打开 [docs/report/index.html](docs/report/index.html) 离线阅读。实验室完整历史汇报含现场画面和录音证据，保留在本机，未复制到公共仓库。

## 最终装配参数

| 部件 | 当前配置 | 说明 |
|---|---|---|
| 摄像头 | SC233HGS；3840×1080 双目同屏；MJPEG；请求 60 fps | 每眼 1920×1080；最近实采约 49.6 fps，未宣称稳定达到 60 fps |
| 对焦 | 手动调焦 MF | 软件不能产生镜头没有提供的电动对焦能力 |
| 麦克风 | 4 通道 / 48 kHz / 16 bit / WASAPI RAW | 左耳 CH2、右耳 CH3；左右以小熊自身为准 |
| 两耳间距 | 0.1355 m；延迟零点 0 | 估计工作间距；身体两路尚未参与控制 |
| Yaw ID 0 | 最小 1404 / 中位 1952 / 最大 2500 | 左右各 548，总 1096 刻度 |
| Pitch ID 2 | 最小 1255 / 中位 1555 / 最大 1855 | 上下各 300，总 600 刻度 |
| 平滑控制 | 50 Hz；最大速度 180、加速度 600、jerk 3000 | 沿用已调试参数；刻度不是已标定的真实角度 |

`configs/panda-final-r43.json` 保存最终装配参数；`locator_demo_settings.example.json` 为历史通用示例，保留用于兼容性测试，不是当前小熊的推荐配置。

`calibration/stereo_camera-alignment.json` 是当前这套镜头安装的对齐预览参数，**正式米制距离仍关闭**。它不能保证另一台装配或移动镜头后仍然有效。标定板在 [output/calibration/stereo-charuco-A4-20mm.pdf](output/calibration/stereo-charuco-A4-20mm.pdf)，100%实际大小打印并测量格长。

## 目录

| 目录 / 文件 | 内容 |
|---|---|
| `src/locator_demo/` | 音频定位、视觉关联、目标选择、轨迹规划、串口与 Web 后端 |
| `src/locator_demo/web/static/` | 当前控制台，以及 `legacy-r30/` 原界面备份 |
| `tests/`、`tools/` | 离线回归、前端检查、采集及诊断工具 |
| `configs/`、`calibration/` | 当前实机配置快照、双目对齐与麦克风映射 |
| `models/` | 已有视觉权重及新增官方 Silero VAD 权重、来源和许可证 |
| `third_party/TalkNet-ASD` | 原仓库已锁定的可选上游子模块；默认 `rules` 路径不需要它 |
| `hardware/` | 用户从另一台电脑提交的头部和身体 CAD/Blender 模型，原样保留 |
| `hardware_examples/` | USB 相机、麦克风、串口调用示例 |
| `ui_backups/` | 原前端备份和恢复说明 |
| `docs/` | 使用说明、当前摘要、历史记录、发布验证结果 |

如需可选 TalkNet，运行 `git submodule update --init --recursive`，再按照上游说明自行准备权重；默认演示不依赖 TalkNet 权重。保留上游许可证和使用条件，不把未启用模块算作已验收能力。

## 验证

回归采用模拟硬件，屏蔽真实舵机自动探测，不开启真实相机或录音。完整前端检查需要 Node.js。

```powershell
python -X utf8 .\tools\validate_regression_repair.py --output .\.tmp\regression
python -X utf8 .\tools\validate_publication.py --output .\.tmp\publication
```

本地 R43 上一轮通过 690 项 Python 回归、75 项前端检查。此次上传目录将重新验证，具体结果见 [docs/publication-validation-r43.json](docs/publication-validation-r43.json)。已有 8 组真实保存双目帧完成离线重放，其中 6 组在时间确认后形成双眼共同跟随；这不等同于新的真人动态跟随或实体机械验收。

仍待验证：远距离及多说话人定位、舵机偶发无反馈的硬件根因、最终机械端点、四麦随头部运动的几何关系、正式测距。不能从两耳音量或人脸大小推出可靠的米数。

## 发布范围

源码来自本机 R43 当前版本。仅作便携化整理：打印标定板同时支持仓库内路径；离线测试改为使用仓库内的历史问题 ID 和实机配置快照，不依赖实验室外层目录。算法、控制参数和生产数据处理逻辑未为此次上传调整。

不上传：原始录音、现场人脸图像/视频、串口运行日志、临时文件、历史重复源码快照、浏览器/会话迁移数据、凭据和虚拟环境。它们仍保留在原电脑。

历史文档见 [docs/README_HISTORY_CN.md](docs/README_HISTORY_CN.md)，其中旧参数、旧版本“待重启”状态及指向本机 `hardware_inventory` 的链接只描述当时情况。当前使用以本页、R43 源码和最终装配配置为准。
