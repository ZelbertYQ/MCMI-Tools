# SQT 新版 WWMIT 与 CORE 形态键全链路解析

## 1. 文档目标

本文档用于回答三个核心问题：

1. SQT 在最新版 WWMIT 与 CORE 中，围绕形态键支持到底改了什么。
2. 这些改动如何在导入、提取、导出、加载（运行时）形成闭环。
3. 这些能力如何被移植到当前 MCMIT，并保持与最新版 CORE 的契约兼容。

分析依据来自以下参考目录：

- RefCode/WWMI-Tools-171
- RefCode/Core/WWMI

---

## 2. 一句话总览

SQT 的关键不是“把形态键上限直接改大”，而是把整个链路从“单批假设”升级为“多批次参数化调度”：

- 提取侧识别并记录每一批（batch）的 dispatch、checksum、vertex offset。
- 导出侧把多个 batch 的偏移数据按 127 键一组组织成统一缓冲布局。
- 模板侧按 batch 循环写入参数，调用批次版 overrider 和 batch loader。
- CORE 侧用 checksum + vertex offset + dispatch size 在运行时识别当前调用批次并选取对应数据。
- SetShapeKey 侧通过 127 分段对齐规则，让用户看到连续 ID，同时映射到底层 128 对齐容器。

---

## 3. 先看旧痛点：为什么过去容易炸

在旧方案里，常见问题是：

- 只有单批思维（单 checksum、单 vertex_count），无法稳定处理 160 这种双批数据。
- 运行时经常靠“顺序猜测第几批”而非“参数识别当前批次”。
- 同屏或特殊帧时，第二批容易误判、错配、串扰。
- SetShapeKey 的逻辑 ID 与底层容器索引不完全对齐，导致高位键行为异常。

SQT 的新版实现针对这些点做了结构性升级。

---

## 4. 提取链路：SQT 在 WWMIT 171 做了什么

## 4.1 从 dump 中收集多批次入口

在 RefCode/WWMI-Tools-171/extract_frame_data/data_extractor.py 中：

- SHAPEKEY_CS_1 不再只保留一次调用。
- 对同一 shapekey 输出 hash，会收集多条 entry（每条带 dispatch_y 与三类缓冲）。
- 通过 dispatch_y 去重，允许同对象多批次并存。

这一步建立了“同一对象有多个 shape key batch”的原始事实数据。

## 4.2 逐批构建统一索引

在 RefCode/WWMI-Tools-171/extract_frame_data/shapekey_builder.py 中：

- ShapeKeysDispatch 记录每批的：
  - vertex_offset
  - vertex_count
  - checksum
  - dispatch_y
  - shapekey_count
- 关键解析点是 cb_data[261]（batch_vertex_offset），用于识别批次在共享顶点列表中的起点。
- 每批读 128 个 offset 槽位，遇到终止 offset 停止；然后拼接进全局 shapekey 索引。
- 当 batch_vertex_offset > 0 时，对当前批偏移做重定基址（+batch_vertex_offset）并去掉重复边界。

结果：提取阶段得到“按批次可复原、按全局可检索”的完整形态键索引。

## 4.3 Metadata 升级为 batch 列表

在 RefCode/WWMI-Tools-171/extract_frame_data/metadata_format.py 与 output_builder.py 中：

- 引入 ExtractedObjectShapeKeysBatch 列表字段 batches。
- 每个 batch 写入 checksum、dispatch_y、vertex_offset、vertex_count、shapekey_count。
- 同时保留旧字段（dispatch_y/checksum）兼容旧 Metadata。

这一步非常关键：导出模板可以直接循环 batches 生成 ini 参数，不需要硬编码“只支持两批”。

---

## 5. 导入链路：为何 Blender 端能正确看到数据

导入并不是直接消费 runtime batch 参数，而是消费“提取后合并好的 shape key 顶点偏移索引”。

在 RefCode/WWMI-Tools-171/extract_frame_data/component_builder.py：

- 组件按 shapekey_hash 关联同一组形态键数据。
- build_shapekey_buffer 会为组件局部顶点范围生成可导入的 shapekey buffer。
- 每个组件导入后在 Blender 中仍以 Deform n 的逻辑 ID 表达，便于编辑与比对。

在 RefCode/WWMI-Tools-171/blender_import/blender_import.py：

- 读取 Metadata.json 与 vb/ib/fmt，导入网格与权重。
- 形态键数量通过 mesh.shape_keys 可直接观察，支持对提取结果做人工验证。

结论：SQT 的导入链路把“多批运行时数据”还原成 Blender 可编辑对象，不丢语义。

---

## 6. 导出链路：SQT 如何把 Blender 形态键重新打包

## 6.1 127 键分段策略

在 RefCode/WWMI-Tools-171/blender_export/data_models/data_model_wwmi.py：

- 单批设计为 127 个逻辑键（因为每批 offset 序列前置一个 0 起点，容器对齐到 128 槽）。
- batch_count = ceil(max_key_id / 127)。
- 每批都写一段 128 长度 offset（首位 0 + 127 键边界）。
- ShapeKeyVertexId 与 ShapeKeyVertexOffset 是批次顺序拼接后的连续数组。

这一步解决“逻辑键连续、底层容器对齐”的矛盾。

## 6.2 导出阶段反推 batch 元数据

在 RefCode/WWMI-Tools-171/blender_export/blender_export.py：

- 通过 ShapeKeyOffset 总长度 / 128 反推出 batches_count。
- 对每个 batch 读取该 batch 的末尾 offset 作为 vertex_count。
- 计算每批的 vertex_offset（在总列表中的起点）。
- 校验“所有 batch 的 vertex_count 求和 == ShapeKeyVertexId 总长度”。

这保证导出后的批次参数与实际缓冲数据一致。

## 6.3 模板按 batch 循环生成参数

在 RefCode/WWMI-Tools-171/templates/per_component.ini.j2 与 merged.ini.j2：

- CommandListSetupShapeKeysBatch：
  - 循环写 shapekey_checksum_batchN
  - 写 shapekey_vertex_offset_original_batchN
  - 写 shapekey_vertex_offset_custom_batchN
  - run = CustomShader\WWMIv1\ShapeKeyBatchOverrider
- CommandListLoadShapeKeysBatch：
  - 循环写 shapekey_dispatch_size_y_original_batchN
  - 写 shapekey_vertex_count_batchN
  - run = CommandList\WWMIv1\LoadShapeKeysBatch

模板设计本质是“批次数驱动”，而不是“写死两批”。

---

## 7. CORE 运行时链路：SQT 在加载核心里做了什么

## 7.1 Ini 变量层升级

在 RefCode/Core/WWMI/WuWa-Model-Importer.ini：

新增批次变量组：

- shapekey_checksum_batch0 / batch1
- shapekey_vertex_offset_original_batch0 / batch1
- shapekey_vertex_offset_custom_batch0 / batch1
- shapekey_vertex_count_batch0 / batch1
- shapekey_dispatch_size_y_original_batch0 / batch1

这把“批次识别”和“批次调度”变成显式配置，不再靠隐式假设。

## 7.2 批次 overrider

同文件中的 CustomShaderShapeKeyBatchOverrider：

- 一次把两批参数传给 ShapeKeyOverrider.hlsl（x0/y0/z0 与 x1/y1/z1）。
- 用于在 CS 中根据当前 cb0 特征匹配批次，挑选对应 offset 页面与顶点基址。

## 7.3 批次 loader 调度

同文件中的 CommandListLoadShapeKeysBatch：

- 用 THREAD_GROUP_COUNT_Y 对照 dispatch_size_y_original_batchN 判断当前是哪个原生批次。
- 决定本次 shapekey_vertex_count 取 batch0 还是 batch1。
- 最终统一调用 CustomShaderShapeKeyLoader。

这一步避免了模板侧“先后顺序判断”引入的不稳定。

## 7.4 Overrider shader v2 的核心机制

在 RefCode/Core/WWMI/Shaders/ShapeKeyOverrider.hlsl：

- 同时支持 batch0 和 batch1 checksum 匹配。
- 使用 cb0[65].y 对 original/custom vertex offset 做重定向。
- 通过 shapekey_offset（0 或 32）选择当前批次的 32 行 offset/values 区域。
- custom value 覆盖保持与旧规则兼容（1000000.0 编码约定）。

## 7.5 SetShapeKey 的 127 对齐映射

在 RefCode/Core/WWMI/Shaders/SkapeKeySetter.hlsl：

- 用户输入 ShapeKeyId 是连续逻辑 ID（0..253）。
- 内部按 container_offset = ShapeKeyId / 127 做“每批 +1 槽”的对齐补偿。
- 再映射到 float4 行/分量写入 CustomShapeKeyValuesRW。

这一步解决了“逻辑连续 ID”与“底层 128 对齐容器”之间的索引偏差问题。

---

## 8. 全链路串起来看（从提取到游戏）

1. 帧分析中出现 SHAPEKEY_CS_1 多次调用（每次可能对应不同 batch）。
2. 提取器收集多条 entry（带 dispatch_y 和三类缓冲）。
3. shapekey_builder 根据 cb0 与 batch_vertex_offset 建立批次索引与全局索引。
4. Metadata 输出 batches 列表（checksum、dispatch_y、offset、count）。
5. 导出器按 127 键规则打包 offset/id/offsetxyz，构建 batch 元数据。
6. 模板循环写 batch 参数，调用批次版 overrider 与 batch loader。
7. CORE 在运行时根据 checksum/offset/dispatch 识别当前批次并装载对应数据。
8. SetShapeKey 通过 127 对齐映射把逻辑 ID 写入正确容器位置。

这就是 SQT 新版“导入导出与加载全链路形态键支持”的闭环。

---

## 9. MCMIT 如何兼容这套新版 CORE

你当前项目中的关键文件已经采用了与 WWMIT 171 同源的批次化结构：

- extract_frame_data/shapekey_builder.py
- extract_frame_data/metadata_format.py
- templates/per_component.ini.j2
- templates/merged.ini.j2

兼容的本质是两件事：

1. 数据结构兼容：
- Metadata 中保留 batches 模型与旧字段兜底。
- 提取阶段把 batch checksum/dispatch/offset 产出齐全。

2. 调用契约兼容：
- 模板输出 batch 参数到 WWMIv1 命名空间。
- 调用 ShapeKeyBatchOverrider + LoadShapeKeysBatch 路径。

只要这两个层面一致，MCMIT 与 SQT 最新 CORE 就能在形态键链路上对齐运行。

---

## 10. 你特别关心的“为什么这个方案比之前稳”

根因是“从猜测转为参数化”：

- 以前：猜当前是不是第二批。
- 现在：明确告诉 CORE 每批 checksum、offset、dispatch、count，让 CORE 动态识别。

再加上 SetShapeKey 的 127 对齐映射，逻辑键与容器键不再错位。

因此在复杂场景（大世界、剧情、同屏）下，稳定性显著提升。

---

## 11. 当前仍需注意的边界

1. 提取样本必须覆盖目标动作
- 某些键按需激活，不触发动作就抓不到完整批次。

2. Metadata 必须来自新版提取
- 旧 Metadata 缺少 batches 关键字段，会导致导出模板参数不完整。

3. 同屏串扰属于高压场景
- 必须单独做双角色同屏回归，而不仅看单角色界面。

4. 实验分支 shader 需分清
- RefCode/Core/WWMI/Shaders/ShapeKeyOverrider_MCMI.hlsl 含大量 MCMI 实验逻辑（对象隔离、合并路由、blink remap），并非默认标准 WWMIT 主链入口。

---

## 12. 推荐验证矩阵（给后续开发/测试）

最小验证集建议如下：

1. 单角色-角色界面
- 46/47、高位键（如 156）SetShapeKey 验证。

2. 单角色-大世界
- 自然眨眼、瞳孔、口型联合观察。

3. 双角色同屏
- 第二批键是否串扰、是否闪烁。

4. 导出后静态检查
- mod.ini 中 batch checksum/offset/dispatch/count 是否全部存在且非异常值。

---

## 13. 结论

SQT 在新版 WWMIT + CORE 中完成的不是单点 patch，而是一套“批次化形态键运行时协议”：

- 提取识别多批
- Metadata 表达多批
- 导出写入多批
- CORE 调度多批
- SetShapeKey 对齐多批容器

你把这些改动移植到 MCMIT 后，形态键支持能力就从“单批容错”升级成“全链路批次化支持”。

这也是当前版本能够对齐最新版 CORE、并继续迭代问题定位的基础。