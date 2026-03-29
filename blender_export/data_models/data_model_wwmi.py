import time
import re
import numpy
import bpy
import json


from typing import Tuple, List, Dict, Optional


from ...migoto_io.data_model.dxgi_format import DXGIFormat, DXGIType
from ...migoto_io.data_model.byte_buffer import Semantic, AbstractSemantic, BufferSemantic, BufferLayout, NumpyBuffer
from ...migoto_io.data_model.data_model import DataModel


class DataModelWWMI(DataModel):
    # Export full currently observed range for new characters: IDs 0..160.
    # Offset buffer stores one extra terminal cursor, so length is MAX_SHAPEKEY_COUNT + 1.
    MAX_SHAPEKEY_COUNT = 161
    SHAPEKEY_PAGE_SIZE = 128
    # WuWa's second shapekey batch appears to be aligned with a seam slot at the page boundary.
    # Use 127 as page-2 start so runtime ids >=128 map to expected offsets.
    SHAPEKEY_PAGE2_START = 127
    MAX_TEXCOORD_SLOTS = 5

    buffers_format: Dict[str, BufferLayout] = {
        'Index': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Index), DXGIFormat.R32_UINT, stride=12)
        ]),
        'Position': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Position, 0), DXGIFormat.R32G32B32_FLOAT)
        ]),
        'Blend': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Blendindices, 0), DXGIFormat.R8_UINT, stride=4),
            BufferSemantic(AbstractSemantic(Semantic.Blendweight, 0), DXGIFormat.R8_UINT, stride=4),
        ]),
        'Vector': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Tangent, 0), DXGIFormat.R8G8B8A8_SNORM),
            BufferSemantic(AbstractSemantic(Semantic.Normal, 0), DXGIFormat.R8G8B8_SNORM),
            BufferSemantic(AbstractSemantic(Semantic.BitangentSign, 0), DXGIFormat.R8_SNORM),
        ]),
        'Color': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.Color, 0), DXGIFormat.R8G8B8A8_UNORM),
        ]),
        'TexCoord': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 1), DXGIFormat.R16G16_FLOAT),
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 2), DXGIFormat.R16G16_FLOAT),
            BufferSemantic(AbstractSemantic(Semantic.TexCoord, 3), DXGIFormat.R16G16_FLOAT),
        ]),
        'ShapeKeyOffset': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 0), DXGIFormat.R32G32B32A32_UINT),
        ]),
        'ShapeKeyOffset2': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 3), DXGIFormat.R32G32B32A32_UINT),
        ]),
        'ShapeKeyOffsetMerged': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 6), DXGIFormat.R32G32B32A32_UINT),
        ]),
        'ShapeKeyVertexId': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 1), DXGIFormat.R32_UINT),
        ]),
        'ShapeKeyVertexId2': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 4), DXGIFormat.R32_UINT),
        ]),
        'ShapeKeyVertexIdMerged': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 7), DXGIFormat.R32_UINT),
        ]),
        'ShapeKeyVertexOffset': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 2), DXGIFormat.R16_FLOAT),
        ]),
        'ShapeKeyVertexOffset2': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 5), DXGIFormat.R16_FLOAT),
        ]),
        'ShapeKeyVertexOffsetMerged': BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, 8), DXGIFormat.R16_FLOAT),
        ]),
    }

    def __init__(self):
        self.flip_winding = True
        self.flip_bitangent_sign = True
        self.flip_texcoord_v = True
        self.semantic_converters = {
            # Reshape flat array [[0,0,0],[0,0,0]] to [[0,0,0,1],[0,0,0,1]]
            AbstractSemantic(Semantic.Tangent, 0): [lambda data: self.converter_resize_second_dim(data, 4, fill=1)],
            # Normalize weights to 8-bit values, skip sanitizing since it's already done by DataExtractor
            AbstractSemantic(Semantic.Blendweight, 0): [lambda data: self.converter_normalize_wights_8bit(data, sanitize_weights=False)],
        }
        self.format_converters = {
            # Reshape flat array [0,1,2,3,4,5] to [[0,1,2],[3,4,5]]
            AbstractSemantic(Semantic.Index): [lambda data: self.converter_reshape_second_dim(data, 3)],
        }

    def get_data(self, 
                 context: bpy.types.Context, 
                 collection: bpy.types.Collection, 
                 obj: bpy.types.Object, 
                 mesh: bpy.types.Mesh, 
                 excluded_buffers: List[str],
                 buffers_format: Optional[Dict[str, BufferLayout]] = None,
                 mirror_mesh: bool = False,
                 object_index_layout: Optional[List[int]] = None) -> Tuple[Dict[str, NumpyBuffer], int, Optional[List[int]]]:

        if buffers_format is None:
            buffers_format = self.buffers_format

        # Export UVs by slot order (UV0..UV4 -> TEXCOORD0..TEXCOORD4),
        # not by layer names. Keep metadata format as minimum and extend
        # up to the actual mesh UV count (capped for current pipeline).
        if 'TexCoord' in buffers_format:
            buffers_format = dict(buffers_format)
            texcoord_layout = buffers_format['TexCoord']
            existing_texcoord_count = len([
                semantic for semantic in texcoord_layout.semantics
                if semantic.abstract.enum == Semantic.TexCoord
            ])
            mesh_uv_count = len(getattr(mesh, 'uv_layers', []))
            target_texcoord_count = min(max(existing_texcoord_count, mesh_uv_count), self.MAX_TEXCOORD_SLOTS)
            if target_texcoord_count != existing_texcoord_count:
                buffers_format['TexCoord'] = BufferLayout([
                    BufferSemantic(AbstractSemantic(Semantic.TexCoord, uv_index), DXGIFormat.R16G16_FLOAT)
                    for uv_index in range(target_texcoord_count)
                ])

        # Migrate old 3UV+2COLOR Metadata.json export format to 4UV+1COLOR
        if 'TexCoord' in buffers_format:
            texcoord_layout = buffers_format['TexCoord']
            if texcoord_layout.get_element(AbstractSemantic(Semantic.Color, 1)):
                buffers_format = dict(buffers_format)
                buffers_format['TexCoord'] = BufferLayout([
                    BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
                    BufferSemantic(AbstractSemantic(Semantic.TexCoord, 2), DXGIFormat.R16G16_FLOAT),
                    BufferSemantic(AbstractSemantic(Semantic.TexCoord, 3), DXGIFormat.R16G16_FLOAT),
                ])

        build_blend_remaps = object_index_layout is not None and 'Blend' not in excluded_buffers

        # Request 16-bit VG ids for Blend Remap system
        if build_blend_remaps:
            # Number of VGs per vertex may vary based on buffers_format, we should respect it
            num_vgs = buffers_format['Blend'].get_element(AbstractSemantic(Semantic.Blendindices, 0)).get_num_values()
            buffers_format['BlendRemapVertexVG'] = BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Blendindices, 1), DXGIFormat.R16_UINT, stride=num_vgs*2),
            ])

        index_data, vertex_buffer = self.export_data(context, collection, mesh, excluded_buffers, buffers_format, mirror_mesh, build_blend_remaps)

        buffers = self.build_buffers(index_data, vertex_buffer, excluded_buffers, buffers_format)

        vertex_ids = vertex_buffer.get_field(AbstractSemantic(Semantic.VertexId).get_name())

        if build_blend_remaps:
            blend_buffer = buffers.get('Blend', None)
            if blend_buffer is not None:
                index_buffer = buffers.get('Index', None)
                vg_buffer = buffers.get('BlendRemapVertexVG', None)
                blend_remaps = self.build_blend_remap(context, object_index_layout, index_buffer, blend_buffer, vg_buffer)
                buffers.update(blend_remaps)

        shapekeys = self.export_shapekeys(obj, vertex_ids, excluded_buffers, mirror_mesh)
        buffers.update(shapekeys)

        return buffers, len(vertex_ids)

    def export_shapekeys(self, 
                         obj: bpy.types.Object,  
                         vertex_ids: numpy.ndarray, 
                         excluded_buffers: List[str],
                         mirror_mesh: bool = False) -> Dict[str, NumpyBuffer]:
        
        start_time = time.time()

        if obj.data.shape_keys is None or len(getattr(obj.data.shape_keys, 'key_blocks', [])) == 0:
            print(f'No shapekeys found to process!')
            return {}

        buffers = {}
        for buffer_name, buffer_layout in self.buffers_format.items():
            if buffer_name in excluded_buffers:
                continue
            for semantic in buffer_layout.semantics:
                if semantic.abstract.enum == Semantic.ShapeKey:
                    buffers[buffer_name] = NumpyBuffer(buffer_layout)
                    break

        if len(buffers) == 0:
            print(f'Skipped shapekeys fetching!')
            return {}

        shapekey_offsets = []
        shapekey_vertex_ids_by_key = {}
        shapekey_vertex_offsets_by_key = {}

        shapekey_pattern = re.compile(r'.*(?:deform|custom)[_ -]*(\d+).*')
        shapekey_ids = {}
        
        for shapekey in obj.data.shape_keys.key_blocks:
            match = shapekey_pattern.findall(shapekey.name.lower())
            if len(match) == 0:
                continue
            shapekey_id = int(match[0])
            if shapekey_id not in shapekey_ids:
                shapekey_ids[shapekey_id] = []
            shapekey_ids[shapekey_id].append(shapekey.name)

        for shapekey_id in shapekey_ids.keys():
            shapekey_ids[shapekey_id].sort()

        shapekey_names = []
        for names in shapekey_ids.values():
            shapekey_names.extend(names)

        shapekeys = self.data_extractor.get_shapekey_data(obj, names_filter=shapekey_names, deduct_basis=True)

        shapekey_verts_count = 0
        num_shapekeys = max(shapekey_ids.keys()) + 1 if shapekey_ids else 0
        if num_shapekeys > self.MAX_SHAPEKEY_COUNT:
            print(
                f'Warning: Model has {num_shapekeys} shapekeys, but current MCMI core supports '
                f'max {self.MAX_SHAPEKEY_COUNT}. Capping export.'
            )
            num_shapekeys = self.MAX_SHAPEKEY_COUNT
        for group_id in range(num_shapekeys):
            shapekey = None
            for shapekey_name in shapekey_ids.get(group_id, []):
                shapekey_part = shapekeys.get(shapekey_name, None)
                if shapekey_part is None:
                    continue
                if shapekey is None:
                    shapekey = shapekey_part.copy()
                else:
                    shapekey = shapekey + shapekey_part
            if shapekey is None or not (-0.00000001 > numpy.min(shapekey) or numpy.max(shapekey) > 0.00000001):
                shapekey_offsets.extend([shapekey_verts_count if shapekey_verts_count != 0 else 0])
                continue

            shapekey_offsets.extend([shapekey_verts_count])

            shapekey = shapekey[vertex_ids]

            shapekey_vert_ids = numpy.where(numpy.any(shapekey != 0, axis=1))[0]

            shapekey_vertex_ids_by_key[group_id] = shapekey_vert_ids
            shapekey_vertex_offsets_by_key[group_id] = shapekey[shapekey_vert_ids]
            shapekey_verts_count += len(shapekey_vert_ids)
            
        if len(shapekey_vertex_ids_by_key) == 0:
            return {}

        # We allow up to MAX_SHAPEKEY_COUNT in offset array to pretend these shapekeys exist (prevent crash on indexing), 
        # but they will all point to the end of vertex data, mapping to 0 offset values.
        shapekey_offsets.append(shapekey_verts_count)
        if len(shapekey_offsets) > self.MAX_SHAPEKEY_COUNT + 1:
            shapekey_offsets = shapekey_offsets[:self.MAX_SHAPEKEY_COUNT + 1]
            shapekey_offsets[-1] = shapekey_verts_count
        else:
            shapekey_offsets.extend([shapekey_verts_count] * ((self.MAX_SHAPEKEY_COUNT + 1) - len(shapekey_offsets)))

        shapekey_offsets = numpy.array(shapekey_offsets, dtype=numpy.uint32)

        def build_page_data(page_start: int, stop_before_key: Optional[int] = None):
            page_offsets = numpy.zeros(self.SHAPEKEY_PAGE_SIZE, dtype=numpy.uint32)
            page_vertex_ids: List[numpy.ndarray] = []
            page_vertex_offsets: List[numpy.ndarray] = []
            cursor = 0

            for local_id in range(self.SHAPEKEY_PAGE_SIZE):
                global_id = page_start + local_id
                page_offsets[local_id] = cursor

                if stop_before_key is not None and global_id >= stop_before_key:
                    continue

                ids = shapekey_vertex_ids_by_key.get(global_id, None)
                offs = shapekey_vertex_offsets_by_key.get(global_id, None)
                if ids is None or offs is None or len(ids) == 0:
                    continue

                page_vertex_ids.append(ids)
                page_vertex_offsets.append(offs)
                cursor += len(ids)

            if len(page_vertex_ids) > 0:
                page_vertex_ids_np = numpy.concatenate(page_vertex_ids).astype(numpy.uint32, copy=False)
                page_vertex_offsets_np = numpy.concatenate(page_vertex_offsets, axis=0)
            else:
                page_vertex_ids_np = numpy.zeros(0, dtype=numpy.uint32)
                page_vertex_offsets_np = numpy.zeros((0, 3), dtype=numpy.float32)

            page_vertex_offsets_packed = numpy.zeros(len(page_vertex_offsets_np), dtype=(numpy.float16, 6))
            page_vertex_offsets_packed[:, 0:3] = page_vertex_offsets_np

            if mirror_mesh:
                page_vertex_offsets_packed[:, 0] *= -1

            return page_offsets, page_vertex_ids_np, page_vertex_offsets_packed

        # Page0 keeps keys 0..126. Slot 127 is a terminal cursor for key126.
        page0_offsets, page0_ids, page0_offsets_xyz = build_page_data(0, stop_before_key=self.SHAPEKEY_PAGE2_START)
        # Page1 maps local 0..127 to global 127..254.
        page1_offsets, page1_ids, page1_offsets_xyz = build_page_data(self.SHAPEKEY_PAGE2_START)

        merged_offsets = numpy.concatenate((page0_offsets, page1_offsets), axis=0)
        merged_ids = numpy.concatenate((page0_ids, page1_ids), axis=0)
        merged_offsets_xyz = numpy.concatenate((page0_offsets_xyz, page1_offsets_xyz), axis=0)

        buffers['ShapeKeyOffset'].set_data(page0_offsets)
        buffers['ShapeKeyOffset2'].set_data(page1_offsets)
        buffers['ShapeKeyOffsetMerged'].set_data(merged_offsets)
        buffers['ShapeKeyVertexId'].set_data(page0_ids)
        buffers['ShapeKeyVertexOffset'].set_data(page0_offsets_xyz)
        buffers['ShapeKeyVertexId2'].set_data(page1_ids)
        buffers['ShapeKeyVertexOffset2'].set_data(page1_offsets_xyz)
        buffers['ShapeKeyVertexIdMerged'].set_data(merged_ids)
        buffers['ShapeKeyVertexOffsetMerged'].set_data(merged_offsets_xyz)

        total_shapekeyed_vertices = len(page0_ids) + len(page1_ids)
        print(f'Shape Keys formatting time: {time.time() - start_time :.3f}s ({total_shapekeyed_vertices} shapekeyed vertices)')

        return buffers

    def build_blend_remap(self, 
                         context: bpy.types.Context, 
                         index_layout: List[int], 
                         index_buffer: NumpyBuffer,
                         blend_buffer: NumpyBuffer,
                         vg_buffer: NumpyBuffer) -> Dict[str, NumpyBuffer]:
        
        start_time = time.time()

        remapped_vgs_counts = []

        if context.scene.mcmi_tools_settings.index_data_cache:
            # Partial export is enabled and index buffer cache exists, lets load it
            index_data = numpy.array(json.loads(context.scene.mcmi_tools_settings.index_data_cache)).ravel()
        else:
            if index_buffer is None:
                raise ValueError(f'Failed to build blend remap: `Index` buffer does not exist!')
            index_data = index_buffer.get_field(0).ravel()

        vg_ids = vg_buffer.get_field(vg_buffer.layout.get_element(AbstractSemantic(Semantic.Blendindices, 1)).get_name())
        vg_weights = blend_buffer.get_field(blend_buffer.layout.get_element(AbstractSemantic(Semantic.Blendweight, 0)).get_name())
        
        blend_remap_forward = numpy.empty(0, dtype=numpy.uint16)
        blend_remap_reverse = numpy.empty(0, dtype=numpy.uint16)

        index_offset = 0
        for index_count in index_layout:
            # Skip remapping the component if its custom mesh is empty
            if index_count == 0:
                remapped_vgs_counts.append(0)
                continue
    
            # Extract a segment of Index Buffer for the component (index_count number of indices starting from index_offset)
            vertex_ids = index_data[index_offset:index_offset+index_count]
            # Remove duplicate vertex ids (since multiple indices may reference the same vertex)
            vertex_ids = numpy.unique(vertex_ids)

            # Get VG ids used to weight vertices used in the component
            obj_vg_ids = vg_ids[vertex_ids].flatten()
            
            # Skip remapping the component if it references VG ids below 256 only
            if numpy.max(obj_vg_ids) < 256:
                index_offset += index_count
                remapped_vgs_counts.append(0)
                continue

            # Get weights for vertices referenced by the component
            obj_vg_weights = vg_weights[vertex_ids].flatten()
            # Get indices of non-zero weights (to skip remapping VG ids that are listed but not actually used)
            non_zero_idx = numpy.nonzero(obj_vg_weights > 0)[0]

            obj_vg_ids = obj_vg_ids[non_zero_idx]
            obj_vg_ids = numpy.unique(obj_vg_ids)

            if numpy.max(obj_vg_ids) < 256:
                index_offset += index_count
                remapped_vgs_counts.append(0)
                continue
            
            remapped_vgs_counts.append(len(obj_vg_ids))

            forward = numpy.zeros(512, dtype=numpy.uint16)
            forward[numpy.arange(len(obj_vg_ids))] = obj_vg_ids

            reverse = numpy.zeros(512, dtype=numpy.uint16)
            reverse[obj_vg_ids] = numpy.arange(len(obj_vg_ids))

            blend_remap_forward = numpy.concatenate((blend_remap_forward, forward), axis=0)
            blend_remap_reverse = numpy.concatenate((blend_remap_reverse, reverse), axis=0)

            index_offset += index_count

        buffers = {}

        buffers['BlendRemapForward'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 0), DXGIFormat.R16_UINT),
        ]))
        buffers['BlendRemapReverse'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 1), DXGIFormat.R16_UINT),
        ]))
        buffers['BlendRemapLayout'] = NumpyBuffer(BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.RawData, 2), DXGIFormat.R32_UINT),
        ]))

        buffers['BlendRemapForward'].set_data(blend_remap_forward)
        buffers['BlendRemapReverse'].set_data(blend_remap_reverse)
        buffers['BlendRemapLayout'].set_data(numpy.array(remapped_vgs_counts))

        print(f'Blend remap time: {time.time() - start_time :.3f}s ({int(len(blend_remap_forward) / 512)} remaps)')

        return buffers
