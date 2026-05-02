# MCMI-Tools

本项目是 [WWMI-Tools](https://github.com/SpectrumQT/WWMI-Tools) 的修改版，旨在提供更实用的功能与改进。

## 中文说明

### 功能清单

1. **双语切换**：添加中文支持。
2. **LOD 模式**：分别提取三次 LOD 转储文件夹（两次也可以），选定路径后导入一次，插件会自动完成顶点组映射；工程中只需要一份集合，导出一次即可自动导出三个 LOD MOD。
3. **指定提取**：在指定哈希栏中选定 Numpad 7/8 轮询的 IB，按 9 复制过来，即可只提取它；避免复杂场景中提取不需要的角色。
4. **不更新贴图**：不勾选更新贴图，导出时不会覆盖「Shading: Textures」部分。
5. **收集提取资源**：勾选后，会在导出文件夹中创建额外文件夹，收集这次提取用到的资源，甚至可以用这个文件夹再提取一遍。
6. **贴图替换重命名**：导出的贴图文件夹重命名为纯 hash 格式，对多模态角色是很有用的底层变动。
7. **移入/移除着色过滤**：在指定 hash 栏中填入贴图 hash，移入过滤后，以后提取便不会再放进去了；主要用于过滤面部 SDF 贴图和一些不需要但高频的贴图。

## English

This project is a modified version of [WWMI-Tools](https://github.com/SpectrumQT/WWMI-Tools), focused on practical features and improvements.

### Feature List

1. **Bilingual UI**: Chinese language support.
2. **LOD Mode**: Extract LOD dumps three times (two is also fine), import once, and the add-on auto-builds vertex-group mappings. One collection is enough; a single export outputs three LOD MODs.
3. **Targeted Extraction**: Pick the IB cycled by Numpad 7/8 in the hash field, press 9 to paste it, and extract only that target. This avoids pulling unwanted characters in complex scenes.
4. **Do Not Update Textures**: When disabled, exporting will not overwrite the "Shading: Textures" section.
5. **Collect Extracted Resources**: When enabled, an extra folder is created in the output directory to collect resources used during extraction; you can even re-extract from it.
6. **Texture Rename for Replacement**: Exported textures are renamed to pure hash format, which is very useful for multi-modal characters.
7. **Add/Remove Shading Filter**: Fill in a texture hash and add it to the filter to exclude it from future extraction; mainly for face SDF textures and other high-frequency but unnecessary textures.