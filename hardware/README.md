# 机械结构资料

这个目录存放机器人头部的**机械设计资料**：3D 模型、结构图、装配图、打印件等。

> **不要和仓库根目录的 `models/` 混淆。** 那个 `models/` 装的是 **AI 模型文件**
> （MediaPipe、SCRFD 的 `.task` / `.onnx`），是软件依赖；这里的 `hardware/` 是**硬件设计**。

## 目录结构

```
hardware/
├── cad/         3D 模型源文件（可编辑、可修改的原始文件）
├── drawings/    结构图 / 装配图 / 尺寸标注图（扁平化的图，用于阅读和沟通）
└── print/       3D 打印导出件（直接可以丢给切片软件的文件）
```

现有机械参考放在 `cad/reference/`，关键视图放在 `drawings/reference/`。后续头部外壳的设计源文件与打印导出件继续分别归入 `cad/` 和 `print/`。

外壳开发者请先阅读[头部外壳源文件位置与开发者约定](cad/head_shell/README.md)，并按其中的位置、命名、单位和验证状态要求交付。

## 各目录放什么

### `cad/` — 3D 模型源文件

**可编辑的原始文件**，是唯一的"真相来源"。修改设计从这里改。

| 格式 | 说明 |
| --- | --- |
| `.step` / `.stp` | **推荐**。通用交换格式，任何 CAD 都能打开，跨软件协作最保险 |
| `.f3d` | Fusion 360 原生格式 |
| `.sldprt` / `.sldasm` | SolidWorks 原生格式 |
| `.blend` | Blender |
| `.FCStd` | FreeCAD |

> 实体CAD在工具支持时建议附STEP；Blender网格设计保留.blend源文件，并提供打印所需的STL或3MF导出件。

### `drawings/` — 结构图与装配图

给人看的图：整体结构、零件分解、装配关系、关键尺寸标注。

推荐 **`.pdf`**（矢量、能放大、能打印）或 **`.svg`**。截图存 `.png` 也可以，
但避免用微信/QQ 传过的二次压缩图，尺寸标注会糊。

想表达"内部结构"或"装配顺序"时，**爆炸图（exploded view）** 比剖视图更直观。

### `print/` — 打印导出件

按零件导出的打印模型或切片项目。随文件标注验证状态；完成导出不等于已经完成切片、试装或试打印。

| 格式 | 说明 |
| --- | --- |
| `.stl` | 最通用，但**不含单位信息**——切片前务必确认是毫米 |
| `.3mf` | **推荐**。能带单位、朝向、支撑设置、多色信息 |

导出时建议**一个零件一个文件**，文件名带上零件名和版本，例如
`head_shell_left_v2.stl`。不然后面会有十几个 `output_final_最终版.stl`。

---

## ⚠️ 关于 git 和大文件

**这里的设计文件会公开到 GitHub，而且 git 对二进制文件很不友好：**

- 大量二进制迭代文件可能增加仓库历史体积，应精简为必要的设计和交付版本。
- Git的打包压缩会影响实际占用，不能用文件大小乘版本数直接估算；参见[Git打包说明](https://git-scm.com/book/en/v2/Git-Internals-Packfiles)。
- GitHub常规Git文件的大小限制参见[官方大文件说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。

**所以：**

1. **只提交源文件（`.step`）和最终定版**，中间迭代版本不要提交
2. **别把切片软件的缓存、G-code 往里放** —— G-code 动辄几百 MB，且和具体机器绑定，对别人没用
3. 如果确实有大文件（扫描件、渲染视频），考虑：
   - 压缩后上传
   - 或改用 Git LFS（`git lfs track "*.step"`）
   - 或放到网盘/图床，在这里只放链接

**提交前看一眼大小：**

```powershell
Get-ChildItem hardware -Recurse -File | Sort-Object Length -Descending |
  Select-Object -First 10 @{n='MB';e={[math]::Round($_.Length/1MB,2)}}, FullName
```

---

## 命名建议

| 类型 | 建议 |
| --- | --- |
| 零件 | `head_shell_left.step`（部位_零件_左右） |
| 装配 | `head_assembly_full.step` |
| 结构图 | `head_exploded_view.pdf` |
| 带版本的定版 | `servo_bracket_v3.step`（版本号放最后） |

**避免**：`最终版`、`最终版2`、`真的最终版`、`新建文件夹`、中文空格混排。
用英文小写 + 下划线，跨平台不会出问题。

---

## 结构图与代码参数的对应关系

机械设计直接决定几个控制参数。**改机械的时候，这几个参数可能要跟着改：**

| 机械上的东西 | 影响的代码参数 | 在哪调 |
| --- | --- | --- |
| **相机安装在头部的偏心量** | `Target X` / `Target Y`（视觉瞄准点偏移） | Dashboard，或用 `Record visual sample` 校准 |
| 舵机行程的机械限位 | `Pitch min / center / max`（安全范围） | Dashboard |
| 舵机安装方向（正装/反装） | `Yaw 反转` / `Pitch 反转`（方向符号） | Dashboard |
| 舵机减速比 / 连杆比 | `Yaw step` / `Yaw max`（步长） | Dashboard |
| 头部转动惯量 | `控制` 档位（稳健/快速） | Dashboard |

**特别说明相机偏心**：代码里有个 `target_offset_x_norm`（Dashboard 上的 `Target X`），
默认 0.04，作用就是补偿"相机不在头部旋转轴上"。**如果你改了相机的安装位置，
这个值必须重新校准** —— 方法见 [`../PROJECT_GUIDE_CN.md`](../PROJECT_GUIDE_CN.md) 第 5 节第 ⑥ 步。

原理很简单：头部绕自己的轴转，但相机是偏心装的，相机看到的"正前方"和头部的"正前方"
差一个固定角度。准星代表"头部要朝的方向"，所以必须把这个偏差补偿掉。

---

## 已整理的机械参考

首批保留4个核心素材，尺寸与来源记录见 [source_manifest.json](cad/reference/source_manifest.json)。

| 文件 | 用途 |
| --- | --- |
| [jibot1-240103_asm.stp](cad/reference/jibot1-240103_asm.stp) | 原始机械臂装配，保留精确CAD几何，用于核对底部机构和安装接口。 |
| [head_base_reference.blend](cad/reference/head_base_reference.blend) | 从装配中提取的10个主要零件，作为头部内部两轴机构的可编辑静态网格参考。 |
| [assembly_overview.png](drawings/reference/assembly_overview.png) | 完整装配概览，显示原始机械臂的范围及执行器分布。 |
| [head_base_axes.png](drawings/reference/head_base_axes.png) | 朝上摆放的底座局部及两条轴线示意，便于设计头壳空间。 |

### 参考范围与单位

原始STEP使用毫米。Blender参考文件内部使用米、界面显示毫米，1个Blender单位对应1米；导出到切片软件时应核对最终毫米尺寸。

底座局部的网格包络约为110.87×110.87×92.50毫米，包含10个主要零件。该局部未包含全部支撑件及紧固件，不能把92.50毫米当作商品云台的完整装配高度。网格转换参数0.15毫米也不代表制造公差。

材料提供者确认原始机械臂底部对应[此二维云台商品系列](https://item.taobao.com/item.htm?id=525140448860)。D款是外形较接近的候选，准确型号、孔位、完整尺寸、舵盘与舵机仍待核实。图中的两条箭头仅示意几何轴线；Blender文件没有经过验证的运动关节或控制绑定。CAD零件编号与舵机通信ID应分别记录。

这批文件是机械机构参考，尚无头部外壳设计或打印就绪验证。完整Blender装配、重复视图、可再生成的网格JSON及转换过程文件暂存本地；旧控制代码与采购聊天材料不属于本批机械素材。

### 后续外壳文件归档

- 设计源文件放入 [cad/head_shell/](cad/head_shell/README.md)，例如 `head_shell_front_v1.step`；保留可编辑源文件，并遵循该目录的开发者约定。
- 尺寸图和装配说明放入 `drawings/head_shell/`，注明相机开孔、轴线、安装孔、转动避让与装配关系。
- 打印导出件放入 `print/head_shell/`，与源文件版本对应，并记录毫米单位、材料、打印方向和必要的切片配置。

## 当前状态

已整理机械臂原始STEP、两轴底座局部Blender参考及两张关键视图，并建立外壳源文件目录与开发者约定。外壳原文件和打印导出件尚待后续设计；`print/` 当前只有目录占位。
