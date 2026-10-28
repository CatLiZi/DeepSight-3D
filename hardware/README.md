# 机械结构资料

这个目录存放熊猫机器人头部与身体的**机械设计资料**：3D 模型、结构图、装配图、打印件等。

> **不要和仓库根目录的 `models/` 混淆。** 那个 `models/` 装的是 **AI 模型文件**
> （MediaPipe、SCRFD 的 `.task` / `.onnx`），是软件依赖；这里的 `hardware/` 是**硬件设计**。

## 目录结构

```
hardware/
├── cad/         3D 模型源文件（可编辑、可修改的原始文件）
├── drawings/    结构图 / 装配图 / 尺寸标注图（扁平化的图，用于阅读和沟通）
└── print/       3D 打印导出件（直接可以丢给切片软件的文件）
```

当前暂定的熊猫建模核心文件见 [cad/README.md](cad/README.md)：头部V17放在 `cad/head_shell/`，身体V25壳体与V16圆柱底座组合放在 `cad/body_shell/`。这两份Blender原生文件来自2026年10月28日确认的交付包，保留原始内容。

历史机械参考的来源与尺寸记录放在 `cad/reference/source_manifest.json`，关键视图放在 `drawings/reference/`。旧STEP与底座局部Blender参考文件已移出Git历史，和当前熊猫原生模型分开记录。

外壳开发者请先阅读[头部外壳源文件位置与开发者约定](cad/head_shell/README.md)及[身体源文件说明](cad/body_shell/README.md)，并按其中的位置、命名、单位和验证状态要求交付。

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

1. **仅提交确认需要保留的源文件和选定交付版本**；当前保留头部V17与身体V25/V16组合两份核心Blender工程，旧机构参考不重新入库，中间迭代与大型打印包另行归档
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

本节为历史机构参考，保留两张关键视图，尺寸与来源记录见 [source_manifest.json](cad/reference/source_manifest.json)。两份旧3D参考源文件的文件名与校验值保留在清单的 `archived_3d_sources` 中，原文件不再随Git仓库分发。当前熊猫模型另见 [CAD核心文件](cad/README.md)。

| 文件 | 用途 |
| --- | --- |
| [assembly_overview.png](drawings/reference/assembly_overview.png) | 完整装配概览，显示原始机械臂的范围及执行器分布。 |
| [head_base_axes.png](drawings/reference/head_base_axes.png) | 朝上摆放的底座局部及两条轴线示意，便于设计头壳空间。 |

### 参考范围与单位

以下数据来自历史3D机构参考，用于理解现有视图，不代表当前熊猫外壳的实测尺寸。相关旧参考原文件不随仓库提供，后续安装接口应按实物复核。

历史STEP使用毫米。历史Blender参考文件内部使用米、界面显示毫米，1个Blender单位对应1米；导出到切片软件时应核对最终毫米尺寸。

底座局部的网格包络约为110.87×110.87×92.50毫米，包含10个主要零件。该局部未包含全部支撑件及紧固件，不能把92.50毫米当作商品云台的完整装配高度。网格转换参数0.15毫米也不代表制造公差。

材料提供者确认原始机械臂底部对应[此二维云台商品系列](https://item.taobao.com/item.htm?id=525140448860)。D款是外形较接近的候选，准确型号、孔位、完整尺寸、舵盘与舵机仍待核实。图中的两条箭头仅示意几何轴线；Blender文件没有经过验证的运动关节或控制绑定。CAD零件编号与舵机通信ID应分别记录。

这些视图与历史尺寸记录仅用于机械机构参考，尚无头部外壳设计或打印就绪验证。旧3D源文件及转换过程资料另行归档；本仓库保留必要的视图、来源和校验记录。

### 后续外壳文件归档

- 设计源文件放入 [cad/head_shell/](cad/head_shell/README.md)，例如 `head_shell_front_v1.step`；保留可编辑源文件，并遵循该目录的开发者约定。
- 尺寸图和装配说明放入 `drawings/head_shell/`，注明相机开孔、轴线、安装孔、转动避让与装配关系。
- 打印导出件放入 `print/head_shell/`，与源文件版本对应，并记录毫米单位、材料、打印方向和必要的切片配置。

## 当前状态

已收录用户确认的两份暂定核心Blender模型，以及两张历史机械参考视图、来源记录和外壳开发约定。两份当前模型已通过文件读取和外部资源依赖检查，SHA256见 [panda_source_manifest.json](cad/panda_source_manifest.json)。本次交付包未包含STL/3MF，`print/`仍为目录占位；制造文件应从当前对应版本导出并复核，不能仅因已入库而视为完成试装或试打印。
