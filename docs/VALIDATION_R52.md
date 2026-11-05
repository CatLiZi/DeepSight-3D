# R52 + D1 发布检查 · 2026-11-05

本轮将已实现的互斥优先模式、60秒本机录制与三段英文演示文案同步到项目仓库。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| Python严格回归 | 904通过，0失败、0错误、0跳过 | 包含28项优先模式场景、录制与状态文件读写竞争检查 |
| 前端检查 | 123通过 | 模式互斥、段落时间提示、录制上传与取消、回放与数据限制 |
| 发布资料 | 当前配置、Silero模型哈希、标定板PDF、两份CAD源文件哈希通过 | 不访问实体设备 |
| 本机加载核对 | 用户正常重启后，接口确认R52、四路48kHz/16bit RAW和双目在线 | 自动运动保持暂停，未发动作 |
| 三段文案 | 90词，A→B→A | 提词3–20s、21–38s、39–54s；换人各留1s |

录制时间线使用 `three_turns_v2_A1`、`three_turns_v2_B1`、`three_turns_v2_A2` 记录计划段落；完整文本在[英文文案](DEMO_DIALOGUE_EN.md)，计划段落不能当作实测说话人标签。

```powershell
python -X utf8 tools/validate_regression_repair.py --output .tmp/r52-tests
python -X utf8 tools/validate_frontend.py
python -X utf8 tools/validate_publication.py --output .tmp/r52-publication
```

[发布检查JSON](publication-validation-r52.json)记录这次检查结果。源文件SHA-256和Git blob SHA-256分别保留；换行转换造成的字节差异不等于逻辑差异。使用本机已安装环境，未证明全新机器依赖安装与GPU兼容性。

## 保留的边界

- 未重新测试实物双人轮流讲话、同时讲话、遮挡和移动时的切换效果。
- 声音定位不是说话人身份识别或声学测距；两人同向或同时讲话仍可能歧义。
- 动态四麦全姿态、准确角度、远距、运动噪声、机械边界与正式双目测距仍需现场验收。
- Blender模型不在本次变更范围，保留远端当前版本；原始录音、视频、本机设置和私人日志不上传。
