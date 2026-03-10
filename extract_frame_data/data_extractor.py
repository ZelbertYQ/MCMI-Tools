
from dataclasses import dataclass, field
from typing import Union, List, Dict
from enum import Enum

from ..migoto_io.data_model.byte_buffer import ByteBuffer, IndexBuffer, BufferLayout, BufferSemantic, AbstractSemantic, Semantic
from ..migoto_io.dump_parser.log_parser import CallParameters
from ..migoto_io.dump_parser.filename_parser import ResourceDescriptor, SlotType
from ..migoto_io.dump_parser.resource_collector import ShaderCallBranch


class PoseConstantBufferFormat(Enum):
    static = 1
    animated = 2


@dataclass(frozen=True)
class ShapeKeyData:
    shapekey_hash: str
    shapekey_scale_hash: str
    dispatch_y: int
    shapekey_offset_buffer: ByteBuffer
    shapekey_vertex_id_buffer: ByteBuffer
    shapekey_vertex_offset_buffer: ByteBuffer
    raw_resource_paths: tuple = ()


@dataclass
class DrawData:
    vb_hash: str
    ib_hash: str
    cb3_hash: str
    cb4_hash: str
    vertex_offset: int
    vertex_count: int
    index_offset: int
    index_count: int
    index_buffer: IndexBuffer
    position_buffer: ByteBuffer
    vector_buffer: ByteBuffer
    texcoord_buffer: ByteBuffer
    color_buffer: ByteBuffer
    blend_buffer: ByteBuffer
    skeleton_data: ByteBuffer
    skeleton_data_cb3: ByteBuffer
    shapekey_hash: Union[str, None]
    textures: List[ResourceDescriptor]
    raw_resource_paths: list = field(default_factory=list)


@dataclass
class DataExtractor:
    # Input
    call_branches: Dict[str, ShaderCallBranch]
    # Output
    shader_hashes: Dict[str, str] = field(init=False)
    shape_key_data: Dict[str, ShapeKeyData] = field(init=False)
    draw_data: Dict[tuple, DrawData] = field(init=False)

    def __post_init__(self):
        self.shader_hashes = {}
        self.shape_key_data = {}
        self.draw_data = {}

        self.handle_shapekey_cs_0(list(self.call_branches.values()))
        self.handle_static_draw_vs(list(self.call_branches.values()))

    def handle_shapekey_cs_0(self, call_branches):
        for call_branch in call_branches:
            if call_branch.shader_id != 'SHAPEKEY_CS_0':
                continue
            cs0_paths = []
            for branch_call in call_branch.calls:
                self.verify_shader_hash(branch_call.call, call_branch.shader_id, 1)
                cs0_paths.extend(rd.path for rd in branch_call.call.resources.values())
            # We don't need any data from this call, lets go deeper
            self.handle_shapekey_cs_1(call_branch.nested_branches, cs0_paths)

    def handle_shapekey_cs_1(self, call_branches, parent_paths=None):
        for call_branch in call_branches:
            if call_branch.shader_id != 'SHAPEKEY_CS_1':
                continue

            for branch_call in call_branch.calls:
                try:
                    self.verify_shader_hash(branch_call.call, call_branch.shader_id, 1)
                    cs2_paths = self.handle_shapekey_cs_2(call_branch.nested_branches)
                except Exception as e:
                    print(f'Warning! Failed to process Shape Key CS call {branch_call.call}, data may end up missing! (safe to ignore if no fatal errors)')
                    continue

                cs1_paths = list(rd.path for rd in branch_call.call.resources.values())
                shape_key_data = ShapeKeyData(
                    shapekey_hash=branch_call.resources['SHAPEKEY_OUTPUT'].hash,
                    shapekey_scale_hash=branch_call.resources['SHAPEKEY_SCALE_OUTPUT'].hash,
                    dispatch_y=branch_call.call.parameters[CallParameters.Dispatch].ThreadGroupCountY,
                    shapekey_offset_buffer=branch_call.resources['SHAPEKEY_OFFSET_BUFFER'],
                    shapekey_vertex_id_buffer=branch_call.resources['SHAPEKEY_VERTEX_ID_BUFFER'],
                    shapekey_vertex_offset_buffer=branch_call.resources['SHAPEKEY_VERTEX_OFFSET_BUFFER'],
                    raw_resource_paths=tuple(list(parent_paths or []) + cs1_paths + cs2_paths),
                )

                cached_shape_key_data = self.shape_key_data.get(shape_key_data.shapekey_hash, None)

                if cached_shape_key_data is None:
                    self.shape_key_data[shape_key_data.shapekey_hash] = shape_key_data
                else:
                    if shape_key_data.dispatch_y != cached_shape_key_data.dispatch_y:
                        if shape_key_data.dispatch_y > cached_shape_key_data.dispatch_y:
                            self.shape_key_data[shape_key_data.shapekey_hash] = shape_key_data
                            print(f'Warning! Shapekey output {shape_key_data.shapekey_hash} seen with larger dispatch_y '
                                  f'({cached_shape_key_data.dispatch_y} -> {shape_key_data.dispatch_y}), updating to larger value.')
                        else:
                            print(f'Warning! Shapekey output {shape_key_data.shapekey_hash} seen with smaller dispatch_y '
                                  f'({shape_key_data.dispatch_y} < {cached_shape_key_data.dispatch_y}), keeping existing larger value.')

    def handle_shapekey_cs_2(self, call_branches):
        cs2_paths = []
        for call_branch in call_branches:
            if call_branch.shader_id != 'SHAPEKEY_CS_2':
                continue
            outputs = 0
            for branch_call in call_branch.calls:
                try:
                    self.verify_shader_hash(branch_call.call, call_branch.shader_id, 1)
                except Exception as e:
                    continue
                outputs += 1
                cs2_paths.extend(rd.path for rd in branch_call.call.resources.values())
            if outputs == 0:
                raise ValueError(f'No outputs for shader {call_branch.shader_id}')
            # We don't need any data from this call as well, lets just ensure that it's here
        return cs2_paths

    def handle_static_draw_vs(self, call_branches):
        for call_branch in call_branches:
            if call_branch.shader_id == 'DRAW_VS':
                self.handle_draw_vs(call_branches, 'DRAW_VS')

    def handle_draw_vs(self, call_branches, daw_vs_tag):
        for call_branch in call_branches:

            if call_branch.shader_id != daw_vs_tag:
                continue

            for branch_call in call_branch.calls:

                shapekey_input_resource = branch_call.resources['SHAPEKEY_INPUT']

                if shapekey_input_resource is not None:
                    shapekey_hash = shapekey_input_resource.hash
                else:
                    shapekey_hash = None

                index_buffer = branch_call.resources['IB_BUFFER_TXT']

                # Get IB hash from the raw resource descriptor (before it was converted to IndexBuffer)
                ib_resource = branch_call.call.get_filtered_resource({'slot_type': SlotType.IndexBuffer})
                ib_hash = ib_resource.hash if ib_resource is not None else ''

                vb_hash = branch_call.resources['POSE_INPUT_0'].hash

                vertex_indices = [x for y in index_buffer.faces for x in y]
                vertex_offset = min(vertex_indices)
                vertex_count = max(vertex_indices) - vertex_offset + 1

                draw_guid = (vertex_offset, vertex_count, vb_hash)

                position_buffer = branch_call.resources['POSITION_BUFFER']
                blend_buffer = branch_call.resources['BLEND_BUFFER']

                if blend_buffer.num_elements != position_buffer.num_elements:
                    
                    blend_buffer_wide = branch_call.resources['BLEND_BUFFER_WIDE']
                    if blend_buffer_wide.num_elements == position_buffer.num_elements:
                        blend_buffer = blend_buffer_wide
                    else:
                        print(f'Object type not recognized for call {branch_call.call}')
                        continue

                vector_buffer = branch_call.resources['VECTOR_BUFFER']

                if vector_buffer.num_elements != position_buffer.num_elements:
                    raise ValueError(f'VECTOR_BUFFER size must match POSITION_BUFFER!')

                color_buffer = branch_call.resources['COLOR_BUFFER']
                if color_buffer.num_elements == position_buffer.num_elements:
                    color_buffer = color_buffer.get_fragment(vertex_offset, vertex_count)
                else:
                    color_buffer = ByteBuffer(layout=color_buffer.layout)

                texcoord_buffer = branch_call.resources['TEXCOORD_BUFFER']
                if texcoord_buffer.num_elements == position_buffer.num_elements:
                    texcoord_buffer = texcoord_buffer.get_fragment(vertex_offset, vertex_count)
                else:
                    texcoord_buffer_static = branch_call.resources['TEXCOORD_BUFFER_STATIC']
                    if texcoord_buffer_static.num_elements == position_buffer.num_elements:
                        texcoord_buffer = texcoord_buffer_static
                    else:
                        texcoord_buffer = ByteBuffer(layout=texcoord_buffer.layout)

                textures = []
                for texture_id in range(16):
                    texture = branch_call.resources.get(f'TEXTURE_{texture_id}', None)
                    if texture is not None:
                        textures.append(texture)

                draw_data = DrawData(
                    vb_hash=branch_call.resources['POSE_INPUT_0'].hash,
                    ib_hash=ib_hash,
                    cb3_hash=branch_call.resources['SKELETON_DATA_CB3'].hash,
                    cb4_hash=branch_call.resources['SKELETON_DATA'].hash,
                    vertex_offset=vertex_offset,
                    vertex_count=vertex_count,
                    index_offset=branch_call.call.parameters[CallParameters.DrawIndexed].StartIndexLocation,
                    index_count=branch_call.call.parameters[CallParameters.DrawIndexed].IndexCount,
                    # dispatch_x=branch_call.call.parameters[CallParameters.Dispatch].ThreadGroupCountX,
                    index_buffer=index_buffer,
                    position_buffer=position_buffer.get_fragment(vertex_offset, vertex_count),
                    vector_buffer=vector_buffer.get_fragment(vertex_offset, vertex_count),
                    texcoord_buffer=texcoord_buffer,
                    color_buffer=color_buffer,
                    blend_buffer=blend_buffer.get_fragment(vertex_offset, vertex_count),
                    skeleton_data=branch_call.resources['SKELETON_DATA_BUFFER'],
                    skeleton_data_cb3=branch_call.resources['SKELETON_DATA_BUFFER_CB3'],
                    textures=textures,
                    shapekey_hash=shapekey_hash,
                    raw_resource_paths=[rd.path for rd in branch_call.call.resources.values()],
                )

                cached_draw_data = self.draw_data.get(draw_guid, None)

                if cached_draw_data is None:
                    self.draw_data[draw_guid] = draw_data
                else:
                    if index_buffer.num_elements != cached_draw_data.index_buffer.num_elements:
                        raise ValueError(f'index data mismatch for DRAW_VS')

                    if color_buffer.num_elements != 0:
                        cached_draw_data.color_buffer = color_buffer

                    if texcoord_buffer.num_elements != 0:
                        cached_draw_data.texcoord_buffer = texcoord_buffer

                    cached_draw_data.textures.extend(textures)
                    cached_draw_data.raw_resource_paths.extend(draw_data.raw_resource_paths)

    def verify_shader_hash(self, call, shader_id, max_call_shaders):
        if len(call.shaders) != max_call_shaders:
            raise ValueError(f'number of associated shaders for {shader_id} call should be equal to {max_call_shaders}!')
        cached_shader_hash = self.shader_hashes.get(shader_id, None)
        call_shader_hash = next(iter(call.shaders.values())).hash
        if cached_shader_hash is None:
            self.shader_hashes[shader_id] = call_shader_hash
        elif cached_shader_hash != call_shader_hash:
            print(f'Warning! Shader hash variant detected for {shader_id}: expected {cached_shader_hash}, got {call_shader_hash}. Processing anyway.')
