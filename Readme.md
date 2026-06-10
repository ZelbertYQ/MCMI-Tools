# MCMI Tools

MCMI Tools 是 [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools) 的修改版，用于《鸣潮》模组制作。它保留了原插件的导入、导出、提取和模板工作流，并针对中文用户、LOD、多形态贴图替换和新版 WWMI 帧分析数据做了实用增强。

当前版本：`1.7.3.16`

English documentation is available after the Chinese section.

## 安装

1. 从最新 GitHub Release 下载源码 zip。
2. 在 Blender 中打开 `编辑 > 偏好设置 > 插件 > 安装...`。
3. 选择下载的 zip，并启用 `MCMI Tools`。
4. 在 3D 视图侧边栏打开 `MCMI Tools` 标签页。

如果你已经安装过旧版本，更新后建议重启 Blender。插件内置更新器会检查 GitHub Releases。

## 相比 WWMI Tools 的主要改动

### 中英文界面

插件面板和常用操作支持中文与英文标签。可以在插件偏好设置中切换语言。

注意：默认语言是中文。

### LOD 导入与导出

MCMI Tools 可以同时导入 LOD0、LOD1、LOD2 的对象源文件夹，并自动生成顶点组映射表。

基本流程：

1. 启用 `LOD`。
2. 将 `Object Sources` 设置为 LOD0 的提取文件夹。
3. 按需设置 `LOD1 Sources` 和 `LOD2 Sources`。
4. 点击 `Import Object`。
5. 像普通工程一样编辑 LOD0 集合。
6. 点击 `Export Mod`；如果提供了 LOD1/LOD2 源文件夹，插件会额外导出 `LOD1` 和 `LOD2` 子文件夹。

注意事项：

- 映射表由顶点组中心点计算生成，并存储在 Blender 文本块 `MCMI_LodMaps` 中。
- 导出前可以通过 LOD 映射编辑器打开并手动调整映射表。
- 合并骨骼模式下，插件会临时复制并合并集合来计算映射。
- LOD 导出会修补主 `mod.ini`，让共享贴图覆盖在 LOD1/LOD2 对象激活时仍然可用。

### 指定帧分析提取

`Assign Hash` 可用于只提取匹配指定 IB hash 的对象。复杂场景里有多个可提取对象时，这个功能可以避免提取到不需要的角色或物件。

典型用法：

1. 在 Hunting Mode 中轮询目标 IB hash。
2. 将 8 位 hash 填入 `Assign Hash`。
3. 运行 `Extract Frame Data`。

留空 `Assign Hash` 时，会按普通逻辑提取所有兼容对象。

### 自动漫反射贴图匹配

导入器会尝试自动为每个 Component 连接最可能的漫反射贴图。当前版本不再只依赖旧的槽位和尺寸规则。

当前匹配逻辑：

- 候选贴图要求为 SRGB、等边贴图，并且尺寸为 `1024x1024` 或 `2048x2048`；
- 优先使用 `TextureFormat.json` 中的帧分析元数据建立候选关系；
- 当 usage 数据不完整时，回退使用 component 元数据；
- 基于 UV 岛在固定网格上采样；
- 分数基于 RGB 方差计算，并按每个 UV 岛的实际采样点数量加权；
- 全局去重会尽量避免同一张贴图被多个 Component 复用；
- 被判断为描边/辅助贴图的候选会从后续 Component 选择中移除；
- 每次导入会在对象源文件夹旁写出 `DiffuseTextureMatch.log` 诊断日志。

注意事项：

- `DiffuseTextureMatch.log` 只用于调试，不需要放进导出的 Mod。
- 自动匹配仍然是启发式算法。遇到特殊材质布局时，请先检查日志再判断是否匹配错误。
- 加入着色过滤的贴图 hash 会被排除在自动漫反射匹配之外。

### 贴图 hash 身份与子文件夹

贴图身份由文件名里的 8 位 hash 决定。导入器会递归搜索对象源文件夹下的 `.dds`、`.png`、`.jpg`、`.jpeg` 文件。

支持示例：

```text
9ac598b5.dds
C1-漫反射-9ac598b5.dds
Textures/Body/C1-Diffuse-9ac598b5.dds
```

如果多个文件包含同一个 hash，插件会稳定选择其中一个。

导出行为：

- 导出时会保留导入贴图的相对子文件夹结构和原文件名；
- 提取阶段生成的贴图文件名仍然保持纯 hash；
- `mod.ini` 中的贴图引用会保留导出后的相对路径。

### 保留贴图覆盖段

`Update Textures` 控制导出时是否重写 `mod.ini` 中生成的 `Shading: Textures` 部分。

如果你手动编辑过贴图覆盖，只想更新网格缓冲或其他生成段落，可以关闭这个选项。

### 收集提取资源

在调试设置中启用 `Collect Extracted Resources` 后，插件会把本次提取用到的原始帧分析资源复制到 `ExtractResources` 文件夹。

如果提取失败，插件会尽可能把资源保存到 `ExtractError/ExtractResources`。这个功能主要用于调试和分享最小复现。

### COLOR1 / TEXCOORD1 处理

部分 WWMI 数据会把类似 UV 的数据存储在 `COLOR1` 中。MCMI Tools 调整了导入和导出处理，使这类布局能按 `4UV + 1COLOR` 更稳定地往返。

如果你在处理旧 Blender 工程，且工程中仍使用旧版顶点色层，可以使用工具箱里的 `Convert Vertex Colors`。

### 可选反向 Mod 提取

如果插件旁边存在带反向工具的 `Reverse` 文件夹，提取来源中会出现从已有 Mod 文件夹反向提取对象源的模式。

这个功能是可选辅助功能，不属于核心公开发布内容。普通帧分析提取不需要它。

## GitHub Release 工作流

插件更新器读取以下仓库的 GitHub Releases：

```text
https://github.com/ZelbertYQ/MCMI-Tools/releases
```

版本 tag 应与 `bl_info["version"]` 对应，例如：

```text
v1.7.3.16
```

更新器下载的是 GitHub Release 自动提供的源码 zip，因此不需要额外上传更新资产。

## 致谢

本项目基于 [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools) 修改。

原始与持续贡献者：SpectrumQT, LeoTorreZ, SinsOfSeven, SilentNightSound, DarkStarSword, ZelbertYQ。

---

# MCMI Tools English

MCMI Tools is a modified version of [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools) for Wuthering Waves modding. It keeps the original import, export, extraction and template workflows, and adds practical improvements for Chinese users, LOD workflows, texture replacement and recent WWMI frame-dump changes.

Current version: `1.7.3.16`

## Installation

1. Download the source zip from the latest GitHub Release.
2. In Blender, open `Edit > Preferences > Add-ons > Install...`.
3. Select the downloaded zip and enable `MCMI Tools`.
4. Open the sidebar tab `MCMI Tools`.

If you already installed an older build, restart Blender after updating. The built-in updater checks GitHub Releases.

## Main Changes From WWMI Tools

### Chinese and English UI

The add-on panel and most common actions support Chinese and English labels. Change language from the add-on preferences.

Note: the default language is Chinese.

### LOD Import and Export

MCMI Tools can import LOD0, LOD1 and LOD2 object sources together and generate vertex-group mapping tables.

Basic workflow:

1. Enable `LOD`.
2. Set `Object Sources` to the LOD0 extracted folder.
3. Optionally set `LOD1 Sources` and `LOD2 Sources`.
4. Click `Import Object`.
5. Edit the LOD0 collection as usual.
6. Click `Export Mod`; the add-on exports LOD0 plus `LOD1` and `LOD2` subfolders when source folders are provided.

Notes:

- The mapping is generated from vertex-group centers and stored in the Blender text block `MCMI_LodMaps`.
- You can open and edit the mapping table from the LOD map editor before export.
- For merged skeleton workflows, the add-on temporarily duplicates and merges collections to calculate mappings.
- LOD export patches the main `mod.ini` so shared texture overrides can still work when LOD1/LOD2 objects are active.

### Targeted Frame-Dump Extraction

Use `Assign Hash` to extract only the object matching a specific IB hash. This is useful in crowded scenes where multiple compatible objects appear in the same frame dump.

Typical use:

1. In hunting mode, cycle the target IB hash.
2. Paste the 8-character hash into `Assign Hash`.
3. Run `Extract Frame Data`.

Leave `Assign Hash` empty to extract all compatible objects.

### Automatic Diffuse Texture Matching

The importer can connect a likely diffuse texture to each component automatically. This version no longer relies only on old slot and size assumptions.

Current matching behavior:

- candidate textures are filtered by SRGB format, square dimensions, and accepted sizes `1024x1024` or `2048x2048`;
- candidates are grouped by frame-analysis metadata from `TextureFormat.json`;
- if usage data is incomplete, component metadata is used as a fallback;
- UV islands are sampled on a fixed grid;
- candidate scores are based on RGB variance, weighted by each UV island's actual sample count;
- a global de-duplication pass avoids assigning the same texture to multiple components when another valid candidate exists;
- likely outline textures can be removed from later component selection;
- a diagnostic `DiffuseTextureMatch.log` is written beside the imported object sources.

Notes:

- The log is for debugging and is not needed in exported mods.
- The matcher is heuristic. If a character uses unusual material layouts, check `DiffuseTextureMatch.log` before assuming the selected texture is correct.
- Texture hashes added to the shading filter are excluded from automatic diffuse matching.

### Texture Hash Identity and Subfolders

Texture identity is based on the 8-character hash in the filename. The importer recursively searches `.dds`, `.png`, `.jpg` and `.jpeg` files under the object source folder.

Supported examples:

```text
9ac598b5.dds
C1-Diffuse-9ac598b5.dds
Textures/Body/C1-Diffuse-9ac598b5.dds
```

If multiple files contain the same hash, one is selected deterministically.

Export behavior:

- texture files are copied while preserving the imported relative subfolder and filename;
- generated extracted texture names remain pure hash names;
- `mod.ini` texture references keep the exported relative path.

### Texture Section Preservation

`Update Textures` controls whether export rewrites the generated `Shading: Textures` section in `mod.ini`.

Disable it when you manually edited texture overrides and only want to update mesh buffers or other generated sections.

### Slot Mode

Slot mode manages textures by `ps-t0` to `ps-t8` slots instead of only by hash.
It is useful when you want to edit texture links directly in Blender and keep export behavior aligned with the slot graph.

Modes:

- `SLOT_SIMPLE`: one material configuration per component.
- `SLOT_COMPLEX`: per-object material detection.

How to use:

1. Import the object first and make sure `ShaderTextureUsage.json` and `TextureFormat.json` exist in the object source folder.
2. Set `Texture` to `SLOT_SIMPLE` or `SLOT_COMPLEX`.
3. After import, the add-on builds slot-aware node groups and connects images to the matching `ps-t` sockets.
4. Edit links manually in Blender if needed. Export uses the current node connections.
5. Images stored under `Disabled-...` or `Disabled+...` folders are treated as disabled textures, but still stay in the slot system.
6. Non-DDS images such as PNG, JPG or TGA are converted to DDS during export with `texconv.exe`.

Disabled folder behavior:

- Both `Disabled-xxx` and `Disabled+xxx` are recognized as disabled prefixes.
- If the selected source path is already inside a `Disabled-...` folder, the importer still resolves it normally.
- This lets you batch-enable or batch-disable textures outside Blender without breaking slot import.

Drag-and-drop behavior:

- Dragged images are loaded by Blender as external file references.
- If the image is not DDS, export converts it to DDS.
- Keep final replacement textures in the object source tree when possible, so export does not fall back to older dump textures.

Right-click helper:

- In the node editor, there is a helper to swap slot links between two image nodes.
- It transfers the slot connections from one linked image node to the other and disconnects the original node.
- This is useful when you want to move a linked slot setup from one texture to another quickly.

### 插槽模式

插槽模式会按 `ps-t0` 到 `ps-t8` 的槽位来管理贴图，而不是只按 hash 管理。
它适合在 Blender 里直接调整贴图连接，并让导出结果跟着当前节点图走的场景。

模式：

- `SLOT_SIMPLE`：每个 Component 只取一套材质配置。
- `SLOT_COMPLEX`：按每个对象分别识别材质。

使用方法：

1. 先导入对象，确保对象源文件夹中有 `ShaderTextureUsage.json` 和 `TextureFormat.json`。
2. 在 `Texture` 里切换到 `SLOT_SIMPLE` 或 `SLOT_COMPLEX`。
3. 导入后，插件会自动按槽位建立节点组，并把图像连到对应的 `ps-t` 输入。
4. 你可以在 Blender 里手动拖图、改连线；导出时会按当前节点连接导出。
5. 放在 `Disabled-...` 或 `Disabled+...` 文件夹里的贴图，会被识别成禁用贴图，但仍然保留在槽位系统中。
6. 如果拖入的是 PNG、JPG、TGA 等非 DDS 图片，导出时会自动转成 DDS。

`Disabled` 目录规则：

- `Disabled-xxx` 和 `Disabled+xxx` 都会被识别为禁用前缀；
- 如果你直接选中的路径本身就在 `Disabled-...` 文件夹里，插件也会正常解析；
- 这样你可以在 Blender 外部批量启用或禁用贴图，而不用手工一张张断线。

拖拽贴图时：

- Blender 会把拖入的图片当作外部文件引用；
- 只要不是 DDS，导出时就会转成 DDS；
- 尽量把最终要替换的贴图放在对象源文件树里，避免导出时回退到旧的转储贴图。

右键工具：

- 在节点编辑器里，可以对两个图像节点使用“交换插槽链接”功能；
- 它会把一个已连接图像节点上的槽位连接转移到另一个图像节点，并断开原图；
- 适合快速把某套插槽关系从一张图换到另一张图上。

### Extracted Resource Collection

Enable `Collect Extracted Resources` in debug settings to copy raw frame-dump resources used by the extracted object into an `ExtractResources` folder.

If extraction fails, resources are saved under `ExtractError/ExtractResources` when possible. This is intended for debugging and sharing a minimal repro.

### COLOR1 / TEXCOORD1 Handling

Some WWMI data stores UV-like data in `COLOR1`. MCMI Tools adjusts import and export handling so these layouts round-trip as `4UV + 1COLOR` where appropriate.

Use the toolbox action `Convert Vertex Colors` when working with older Blender files that still store color data in legacy vertex color layers.

### Optional Reverse Mod Extraction

If a `Reverse` folder with reverse tools exists beside the add-on, an extra extraction source mode appears for extracting object sources from an existing mod folder.

This helper is optional and not part of the core public release. Normal frame-dump extraction does not require it.

## GitHub Release Workflow

The add-on updater reads GitHub Releases from:

```text
https://github.com/ZelbertYQ/MCMI-Tools/releases
```

Version tags should match `bl_info["version"]`, for example:

```text
v1.7.3.16
```

The updater downloads GitHub's release source zip, so no separate update asset is required.

## Credits

Based on [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools).

Original and continued credits: SpectrumQT, LeoTorreZ, SinsOfSeven, SilentNightSound, DarkStarSword, ZelbertYQ.
