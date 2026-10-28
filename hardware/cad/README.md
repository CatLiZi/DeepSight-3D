# 熊猫建模核心文件

2026年10月28日，项目所有者指定 `panda_blender_github.zip` 中的两份Blender工程为当前暂定的核心建模版本。仓库按头部、身体分别归档，模型字节与原始压缩包一致。

| 部分 | 原生模型 | 版本 | 文件大小 |
|---|---|---|---:|
| 头部 | [panda_head_only_V17.blend](head_shell/panda_head_only_V17.blend) | V17 | 约12.67 MiB |
| 身体 | [panda_body_V25_shells_V16_cylinder.blend](body_shell/panda_body_V25_shells_V16_cylinder.blend) | V25壳体与V16圆柱底座组合 | 约23.88 MiB |

在Blender中选择“文件→打开”，打开所需的 `.blend`。本次使用 **Blender 5.2.2 LTS** 成功读取两份文件；原生文件保留了独立对象、材质、装配件与参考件。两份文件均为公制，内部以米计量、界面显示毫米。导出制造文件时须核对毫米尺寸、朝向及比例。

本次检查未发现外部链接库、未打包图片或未打包字体依赖。未进行Windows/Linux实机打开测试。详细文件大小、SHA256与读取检查结果见 [panda_source_manifest.json](panda_source_manifest.json)。

## 使用与继续修改

- 头部结构与归档约定见 [head_shell/README.md](head_shell/README.md)。
- 身体结构与归档约定见 [body_shell/README.md](body_shell/README.md)。
- `ASSEMBLED_`通常为装配状态副本；`REF_`、`WIRE_`、`HARDWARE_`等通常为参考结构、线束或硬件。名称或属性中包含 `PRINT` 的对象仍需确认实际用途，不能把全部对象一起导出打印。
- 文件中可能同时保留隐藏的打印母件和可见的装配副本。修改时应同步相应副本，并从当前版本重新导出与检查。
- 本次交付包只含建模核心文件，没有STL、3MF或切片项目。后续导出件分别归入 `hardware/print/head_shell/`、`hardware/print/body_shell/`；图纸与装配说明归入对应的 `hardware/drawings/` 子目录。

本次入库状态为“当前暂定建模版本，文件读取与依赖检查通过”，没有新增实物试装、试打印、承载、运动或声学验证结论。

仅两份原生模型和必要说明纳入Git。交付ZIP、Blender自动备份、缓存与中间版本不重复提交。仓库根目录的 `models/` 仍用于算法权重。
