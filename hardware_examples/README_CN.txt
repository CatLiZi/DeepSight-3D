Python 调用新摄像头和四麦阵列的独立样例

一、USB 接口不等于 COM 串口

USB 是物理连接和总线。同一块板可以暴露多个功能接口：
1. 摄像头：Windows 视频驱动 -> DirectShow -> FFmpeg -> Python 图像字节。
2. 麦克风：Windows 音频驱动 -> WASAPI -> sounddevice -> Python 四通道采样。
3. 串口：Windows COM 驱动 -> pyserial -> 按设备协议收发字节。

Python 是调用这些接口的程序语言，不决定设备使用哪一种通信接口。
YDPI4MIC 的音频接口和 COM4 属于同一块板，但音频采集使用 WASAPI。
COM4 的波特率、命令和用途尚未确认。本目录的样例不会自动向它发命令。
原项目的舵机控制使用串口；实际舵机端口需要另外识别。

二、运行环境

当前电脑的 demo 环境已经有 sounddevice、numpy 和 pyserial，FFmpeg 已在 PATH。
PowerShell 运行：

conda activate demo
Set-Location 'C:\Users\SLAI\Desktop\DeepSight-3D'

新环境需要 python -m pip install sounddevice pyserial；FFmpeg 需单独安装并加入 PATH。
以下脚本不依赖主项目，也不会修改其配置。

三、摄像头：capture_camera.py

抓一张完整双目拼接图（3840x1080）：
python .\hardware_examples\capture_camera.py

抓取画面左半幅并缩小到 1280x720：
python .\hardware_examples\capture_camera.py --eye left --width 1280

抓取右半幅：
python .\hardware_examples\capture_camera.py --eye right --width 1280

这里 left/right 表示拼接图的左右半幅，安装时再对应物理左眼/右眼。
FFmpeg 按支持的 MJPEG、3840x1080、60 FPS 输入档位打开；短测实际到帧约 33 FPS。
该样例仅抓一帧，返回值是 JPEG 字节。后续可解码成人脸检测使用的图像矩阵。
连续采集时，应另外设置算法输出帧率，例如 15 FPS，不能把它混作输入档位。

四、麦克风：record_microphone.py

录制三秒四声道 WAV：
python .\hardware_examples\record_microphone.py --seconds 3

脚本按设备名称 YDPI4MIC 和 WASAPI 接口实时查找，不写死设备编号。
使用已验证的共享模式、48000 Hz、int16、4 通道。
数据在内存中交错排列：
第0个采样时刻：[CH1, CH2, CH3, CH4]
第1个采样时刻：[CH1, CH2, CH3, CH4]
以此类推。每个通道每个样本2字节，一帧共8字节。

需要矩阵时，可在算法代码中转换：
import numpy as np
samples = np.frombuffer(bytes(data), dtype='<i2').reshape(-1, 4)
mic1 = samples[:, 0]
mic2 = samples[:, 1]
mic3 = samples[:, 2]
mic4 = samples[:, 3]

四个通道的物理位置需要逐个咪头测试确认；声源定位需要实际间距和坐标。
按商家说明检查并关闭 Windows 音频增强，再验证各轨道的独立性。
本样例只做采集，不修改系统音频设置，也不实现定位算法。

五、对照真正的串口编程：serial_basics.py

只枚举当前 COM 设备：
python .\hardware_examples\serial_basics.py

文件中的 exchange_command 函数展示 serial.Serial / write / read 的编程方式。
没有自动调用该函数。baudrate、command、应答拆包均应来自真实设备协议。
不要把该示意函数当成 YDPI4MIC 或舵机的完整协议实现。

六、输出

图像和 WAV 默认保存在本目录 output 文件夹，使用时间戳命名。
可用 --output 显式指定路径，已存在的文件不会被覆盖。
摄像头或麦克风若正被其他程序独占，请先结束对应采集，再运行样例。

本目录是教学和采集验证样例，主项目的正式适配代码尚未修改。
