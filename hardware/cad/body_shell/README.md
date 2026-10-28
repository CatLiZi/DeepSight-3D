# 身体建模源文件

当前暂定核心文件为 [panda_body_V25_shells_V16_cylinder.blend](panda_body_V25_shells_V16_cylinder.blend)。文件由项目所有者于2026年10月28日通过 `panda_blender_github.zip` 提供，本次原样入库。

该工程包含身体前后壳、左右颈部传感器及卡匣结构、固定线束与尾部线束，以及交付说明中记录的直径126mm圆柱底座模型。文件名表示V25壳体与V16圆柱底座的组合，内部保留的旧组件名称不应被当作独立交付版本。

已使用Blender 5.2.2 LTS读取，场景共31个对象。文件内部以米计量、界面显示毫米；未发现外部链接库或未打包图片、字体依赖。详细校验信息见 [panda_source_manifest.json](../panda_source_manifest.json)。

## 继续建模与导出

配套头部见 [head_shell/README.md](../head_shell/README.md)。请从当前文件编辑，不要直接混用旧身体版本的打印导出件。导出前确认所选对象是否为实际制造零件，区分隐藏母件、装配副本、底座和硬件参考。

身体制造导出件放入 `hardware/print/body_shell/`，图纸和装配说明放入 `hardware/drawings/body_shell/`，使用与源模型对应的版本号。记录毫米尺寸、材料、摆向、支撑以及已完成的试装或试打印结果。

本次提供的ZIP没有STL/3MF或切片配置，入库时没有改变几何，也没有重新进行制造、云台运动或声学验证。
