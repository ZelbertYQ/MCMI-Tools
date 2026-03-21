import os
import re
import sys
import time
import json
import shutil
import traceback
import struct

from pathlib import Path
from typing import Dict
from dataclasses import dataclass
from collections import OrderedDict

from ..addon.exceptions import ConfigError

from ..migoto_io.blender_interface.utility import *

from ..migoto_io.data_model.dxgi_format import DXGIFormat
from ..migoto_io.data_model.byte_buffer import BufferLayout, BufferSemantic, AbstractSemantic, Semantic, ByteBuffer

from ..migoto_io.dump_parser.filename_parser import ShaderType, SlotType, SlotId
from ..migoto_io.dump_parser.dump_parser import Dump
from ..migoto_io.dump_parser.resource_collector import Source
from ..migoto_io.dump_parser.calls_collector import ShaderMap, Slot
from ..migoto_io.dump_parser.data_collector import DataMap, DataCollector

from .data_extractor import DataExtractor
from .shapekey_builder import ShapeKeyBuilder
from .component_builder import ComponentBuilder
from .output_builder import OutputBuilder, TextureFilter, ObjectData


@dataclass
class Configuration:
    # output_path: str
    # dump_dir_path: str
    shader_data_pattern: Dict[str, ShaderMap]
    shader_resources: Dict[str, DataMap]
    output_vb_layout: BufferLayout


# In WuWa VB is dynamically calculated by dedicated compute shaders (aka Pose CS)
# So mesh is getting rendered via following chain:
#               BONES -v  COLOR+TEXCOORD -v
#   BLEND+NORM+POS -> Pose CS -> VB -> VS & PS -> RENDER_TARGET
#    SHAPEKEY_OFFSETS -^            IB -^    ^- Textures
#                ^- Shape Keys Application CS Chain
#   SHAPEKEY_BUFFERS -^
#
# So we can grab all relevant data in 3 steps:
#   1. Collect VS>PS calls from dump
#   2. Collect CS calls from dump that output VB to #1 calls (cs-u0 and cs-u1 to vb)
#   3. For each unique VB output (cs-u0 & cs-u1) from #2 calls:
#        3.1. [BLEND+NORM+POS] Collect CS calls from #2 with VB as output (cs-u0 & cs-u1)
#        3.2. [VERT_COLOR_GROUPS] Collect PS calls from dump with output to #3.1 calls (cs-u0 to cs-t3)
#        3.3. [COLOR+TEXCOORD+IB+Textures] Collect VS>PS calls from #1 with VB as input (vb from cs-u0 and cs-u1)
#
configuration = Configuration(
    # output_path=r'C:\Projects\Wuthering Waves\3DMIGOTO_DEV\!PROJECTS\Collect',
    # dump_dir_path=r'C:\Projects\Wuthering Waves\3DMIGOTO_DEV\FrameAnalysis-2024-06-14-120528',
    # dump_dir_path=r'C:\Projects\Wuthering Waves\3DMIGOTO_DEV\FrameAnalysis-2024-06-10-190045',
    shader_data_pattern={
        'SHAPEKEY_CS_0': ShaderMap(ShaderType.Compute,
                                   inputs=[],
                                   outputs=[Slot('SHAPEKEY_CS_1', ShaderType.Empty, SlotType.UAV, SlotId(0))]),
        'SHAPEKEY_CS_1': ShaderMap(ShaderType.Compute,
                                   inputs=[Slot('SHAPEKEY_CS_0', ShaderType.Empty, SlotType.UAV, SlotId(1))],
                                   outputs=[Slot('SHAPEKEY_CS_2', ShaderType.Empty, SlotType.UAV, SlotId(0))]),
        'SHAPEKEY_CS_2': ShaderMap(ShaderType.Compute,
                                   inputs=[Slot('SHAPEKEY_CS_1', ShaderType.Empty, SlotType.UAV, SlotId(0))],
                                   outputs=[Slot('DRAW_VS_DUMMY', ShaderType.Empty, SlotType.UAV, SlotId(0))]),
        'DRAW_VS_DUMMY': ShaderMap(ShaderType.Vertex,
                             inputs=[Slot('SHAPEKEY_CS_2', ShaderType.Empty, SlotType.VertexBuffer, SlotId(6)),],
                             outputs=[]),
        'DRAW_VS': ShaderMap(ShaderType.Vertex,
                             # Hack: When shader is short cirquited on itself, calls with listed input slots will be excluded from resulting branch
                             inputs=[],
                             # Hack: Short cirquit shader on itself to allow search of shaders without outputs
                             outputs=[Slot('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(5))],),
    },
    shader_resources={
        'SHAPEKEY_OFFSET_BUFFER': DataMap([
                Source('SHAPEKEY_CS_1', ShaderType.Compute, SlotType.ConstantBuffer, SlotId(0)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.RawData), DXGIFormat.R32_UINT),
            ])),
        'SHAPEKEY_VERTEX_ID_BUFFER': DataMap([
                Source('SHAPEKEY_CS_1', ShaderType.Compute, SlotType.Texture, SlotId(0)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.RawData), DXGIFormat.R32_UINT),
            ])),
        'SHAPEKEY_VERTEX_OFFSET_BUFFER': DataMap([
                Source('SHAPEKEY_CS_1', ShaderType.Compute, SlotType.Texture, SlotId(1)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.RawData), DXGIFormat.R16G16B16_FLOAT),
            ])),

        'SHAPEKEY_OUTPUT': DataMap([Source('SHAPEKEY_CS_1', ShaderType.Empty, SlotType.UAV, SlotId(0))]),
        'SHAPEKEY_SCALE_OUTPUT': DataMap([Source('SHAPEKEY_CS_1', ShaderType.Empty, SlotType.UAV, SlotId(1))]),


        'SHAPEKEY_INPUT': DataMap([Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(6), ignore_missing=True)]),

        'POSE_INPUT_0': DataMap([Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(0))]),

        'SKELETON_DATA': DataMap([Source('DRAW_VS', ShaderType.Vertex, SlotType.ConstantBuffer, SlotId(4))]),

        'SKELETON_DATA_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Vertex, SlotType.ConstantBuffer, SlotId(4)),
            ],
            BufferLayout(
                semantics=[
                    BufferSemantic(AbstractSemantic(Semantic.RawData, 0), DXGIFormat.R32_FLOAT, stride=48),
                ],
                force_stride=True)),

        'SKELETON_DATA_CB3': DataMap([Source('DRAW_VS', ShaderType.Vertex, SlotType.ConstantBuffer, SlotId(3))]),

        'SKELETON_DATA_BUFFER_CB3': DataMap([
                Source('DRAW_VS', ShaderType.Vertex, SlotType.ConstantBuffer, SlotId(3)),
            ],
            BufferLayout(
                semantics=[
                    BufferSemantic(AbstractSemantic(Semantic.RawData, 0), DXGIFormat.R32_FLOAT, stride=48),
                ],
                force_stride=True)),

        'POSE_CB': DataMap([
                Source('DRAW_VS', ShaderType.Vertex, SlotType.ConstantBuffer, SlotId(0)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.RawData), DXGIFormat.R32G32B32A32_UINT)
            ])),

        'IB_BUFFER_TXT': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.IndexBuffer, file_ext='txt')
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Index, 0), DXGIFormat.R16G16B16_UINT),
            ])
        ),

        'POSITION_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(0)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Position, 0), DXGIFormat.R32G32B32_FLOAT),
            ])),
        'VECTOR_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(1)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Tangent, 0), DXGIFormat.R8G8B8A8_SNORM),
                BufferSemantic(AbstractSemantic(Semantic.Normal, 0), DXGIFormat.R8G8B8A8_SNORM),
            ])),
        'TEXCOORD_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(2), file_ext='buf'),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 1), DXGIFormat.R16G16_FLOAT),
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 2), DXGIFormat.R16G16_FLOAT),
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 3), DXGIFormat.R16G16_FLOAT),
            ])),
        'TEXCOORD_BUFFER_STATIC': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(2), file_ext='buf'),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
                BufferSemantic(AbstractSemantic(Semantic.TexCoord, 1), DXGIFormat.R16G16_FLOAT),
            ])),
        'COLOR_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(3), file_ext='buf'),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Color, 0), DXGIFormat.R8G8B8A8_UNORM),
            ])),
        'BLEND_BUFFER': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(4)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Blendindices, 0), DXGIFormat.R8G8B8A8_UINT),
                BufferSemantic(AbstractSemantic(Semantic.Blendweight, 0), DXGIFormat.R8G8B8A8_UNORM),
            ], force_stride=True)),
        'BLEND_BUFFER_WIDE': DataMap([
                Source('DRAW_VS', ShaderType.Empty, SlotType.VertexBuffer, SlotId(4)),
            ],
            BufferLayout([
                BufferSemantic(AbstractSemantic(Semantic.Blendindices, 0), DXGIFormat.R8_UINT, stride=8),
                BufferSemantic(AbstractSemantic(Semantic.Blendweight, 0), DXGIFormat.R8_UNORM, stride=8),
            ], force_stride=True)),
        
        'TEXTURE_0': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(0), ignore_missing=True)]),
        'TEXTURE_1': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(1), ignore_missing=True)]),
        'TEXTURE_2': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(2), ignore_missing=True)]),
        'TEXTURE_3': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(3), ignore_missing=True)]),
        'TEXTURE_4': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(4), ignore_missing=True)]),
        'TEXTURE_5': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(5), ignore_missing=True)]),
        'TEXTURE_6': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(6), ignore_missing=True)]),
        'TEXTURE_7': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(7), ignore_missing=True)]),
        'TEXTURE_8': DataMap([Source('DRAW_VS', ShaderType.Pixel, SlotType.Texture, SlotId(8), ignore_missing=True)]),
        
    },
    output_vb_layout=BufferLayout([
        BufferSemantic(AbstractSemantic(Semantic.Position, 0), DXGIFormat.R32G32B32_FLOAT),
        BufferSemantic(AbstractSemantic(Semantic.Tangent, 0), DXGIFormat.R8G8B8A8_SNORM),
        BufferSemantic(AbstractSemantic(Semantic.Normal, 0), DXGIFormat.R8G8B8A8_SNORM),
        BufferSemantic(AbstractSemantic(Semantic.Blendindices, 0), DXGIFormat.R8G8B8A8_UINT),
        BufferSemantic(AbstractSemantic(Semantic.Blendweight, 0), DXGIFormat.R8G8B8A8_UNORM),
        BufferSemantic(AbstractSemantic(Semantic.Color, 0), DXGIFormat.R8G8B8A8_UNORM),
        BufferSemantic(AbstractSemantic(Semantic.TexCoord, 0), DXGIFormat.R16G16_FLOAT),
        BufferSemantic(AbstractSemantic(Semantic.TexCoord, 1), DXGIFormat.R16G16_FLOAT),
        BufferSemantic(AbstractSemantic(Semantic.TexCoord, 2), DXGIFormat.R16G16_FLOAT),
        BufferSemantic(AbstractSemantic(Semantic.TexCoord, 3), DXGIFormat.R16G16_FLOAT),
    ]),
)


def collect_raw_resources(output_directory, data_extractor: DataExtractor, vb_hash: str, dump_path: Path):
    """Copy all raw frame dump files used for the given vb_hash into an ExtractResources subfolder,
    including IB companion .txt files and a filtered log.txt so the folder can be used as a dump source."""
    collect_dir = Path(output_directory) / vb_hash / 'ExtractResources'
    collect_dir.mkdir(parents=True, exist_ok=True)

    paths = set()
    shapekey_hashes = set()

    for draw_data in data_extractor.draw_data.values():
        if draw_data.vb_hash != vb_hash:
            continue
        paths.update(draw_data.raw_resource_paths)
        if draw_data.shapekey_hash:
            shapekey_hashes.add(draw_data.shapekey_hash)

    for sk_hash, sk_data_list in data_extractor.shape_key_data.items():
        if sk_hash in shapekey_hashes:
            for sk_data in sk_data_list:
                paths.update(sk_data.raw_resource_paths)

    call_ids = set()
    for path in paths:
        src = Path(path)
        if not src.is_file():
            continue
        dest = collect_dir / src.name
        if not dest.exists():
            shutil.copyfile(src, dest)
        # Track call ID for log filtering
        m = re.match(r'^(\d+)-', src.name)
        if m:
            call_ids.add(m.group(1))
        # IB .buf files have a companion .txt file used by the resource collector
        if '-ib=' in src.name and src.suffix == '.buf':
            txt_src = src.with_suffix('.txt')
            if txt_src.is_file():
                txt_dest = collect_dir / txt_src.name
                if not txt_dest.exists():
                    shutil.copyfile(txt_src, txt_dest)

    # Write a filtered log.txt containing only the relevant call entries
    src_log = Path(dump_path) / 'log.txt'
    if src_log.is_file() and call_ids:
        dest_log = collect_dir / 'log.txt'
        with open(src_log, 'r') as f_in, open(dest_log, 'w') as f_out:
            include = False
            for line in f_in:
                raw_call_id = line[0:6]
                if raw_call_id.isnumeric():
                    include = raw_call_id in call_ids
                if include:
                    f_out.write(line)


def _iter_branch_resource_paths(call_branches):
    """Recursively yield all raw resource file paths from call branches."""
    for branch in call_branches.values() if isinstance(call_branches, dict) else call_branches:
        for branch_call in branch.calls:
            for rd in branch_call.call.resources.values():
                if hasattr(rd, 'path') and rd.path:
                    yield rd.path
        if branch.nested_branches:
            yield from _iter_branch_resource_paths(branch.nested_branches)


def _copy_resource_files(paths, collect_dir, dump_path):
    """Copy a set of file paths into collect_dir and return matched call IDs."""
    call_ids = set()
    for path in paths:
        src = Path(path)
        if not src.is_file():
            continue
        dest = collect_dir / src.name
        if not dest.exists():
            shutil.copyfile(src, dest)
        m = re.match(r'^(\d+)-', src.name)
        if m:
            call_ids.add(m.group(1))
        if '-ib=' in src.name and src.suffix == '.buf':
            txt_src = src.with_suffix('.txt')
            if txt_src.is_file():
                txt_dest = collect_dir / txt_src.name
                if not txt_dest.exists():
                    shutil.copyfile(txt_src, txt_dest)
    return call_ids


def _write_filtered_log(dump_path, collect_dir, call_ids):
    src_log = Path(dump_path) / 'log.txt'
    if not src_log.is_file():
        return
    dest_log = collect_dir / 'log.txt'
    if call_ids:
        with open(src_log, 'r') as f_in, open(dest_log, 'w') as f_out:
            include = False
            for line in f_in:
                raw_call_id = line[0:6]
                if raw_call_id.isnumeric():
                    include = raw_call_id in call_ids
                if include:
                    f_out.write(line)
    else:
        if not dest_log.exists():
            shutil.copyfile(src_log, dest_log)


def collect_resources_on_error(output_directory, call_branches, dump_path, error_msg: str):
    """Resource collection fallback when the extraction pipeline fails and call branches are available.
    Copies raw dump files referenced by call branches into ExtractError/ExtractResources."""
    collect_dir = Path(output_directory) / 'ExtractError' / 'ExtractResources'
    collect_dir.mkdir(parents=True, exist_ok=True)
    paths = set(_iter_branch_resource_paths(call_branches))
    call_ids = _copy_resource_files(paths, collect_dir, dump_path)
    _write_filtered_log(dump_path, collect_dir, call_ids)
    with open(collect_dir / 'error.txt', 'w', encoding='utf-8') as f:
        f.write(error_msg)


def collect_resources_on_error_from_dump(output_directory, dump, dump_path, error_msg: str):
    """Resource collection fallback when even call branch parsing failed.
    Copies ALL raw dump files from the Dump object into ExtractError/ExtractResources."""
    collect_dir = Path(output_directory) / 'ExtractError' / 'ExtractResources'
    collect_dir.mkdir(parents=True, exist_ok=True)
    paths = set()
    for call in dump.calls.values():
        for rd in call.resources.values():
            if hasattr(rd, 'path') and rd.path:
                paths.add(rd.path)
    # No call_ids filtering: copy log.txt as-is since we have no specific calls to filter
    _copy_resource_files(paths, collect_dir, dump_path)
    _write_filtered_log(dump_path, collect_dir, set())
    with open(collect_dir / 'error.txt', 'w', encoding='utf-8') as f:
        f.write(error_msg)


def get_image_size(image_path: Path):
    try:
        with open(image_path, 'rb') as f:
            header = f.read(64)
            if len(header) < 24:
                return None, None
            if header[:4] == b'DDS ':
                f.seek(12)
                height = struct.unpack('<I', f.read(4))[0]
                width = struct.unpack('<I', f.read(4))[0]
                return width, height
            if header[:8] == b'\x89PNG\r\n\x1a\n':
                width = struct.unpack('>I', header[16:20])[0]
                height = struct.unpack('>I', header[20:24])[0]
                return width, height
            if header[:2] == b'\xff\xd8':
                f.seek(2)
                while True:
                    marker_prefix = f.read(1)
                    if marker_prefix != b'\xff':
                        return None, None
                    marker = f.read(1)
                    while marker == b'\xff':
                        marker = f.read(1)
                    if marker in [b'\xc0', b'\xc1', b'\xc2', b'\xc3', b'\xc5', b'\xc6', b'\xc7', b'\xc9', b'\xca', b'\xcb', b'\xcd', b'\xce', b'\xcf']:
                        f.read(3)
                        height = struct.unpack('>H', f.read(2))[0]
                        width = struct.unpack('>H', f.read(2))[0]
                        return width, height
                    segment_length_data = f.read(2)
                    if len(segment_length_data) != 2:
                        return None, None
                    segment_length = struct.unpack('>H', segment_length_data)[0]
                    if segment_length < 2:
                        return None, None
                    f.seek(segment_length - 2, 1)
    except Exception:
        return None, None
    return None, None


def build_deduped_texture_info(dump_path: Path):
    deduped_texture_info = {}
    deduped_path = Path(dump_path) / 'deduped'
    if not deduped_path.is_dir():
        return deduped_texture_info

    hash_pattern = re.compile(r'([a-fA-F0-9]{8})')
    format_pattern = re.compile(r'([A-Z0-9]+(?:_[A-Z0-9]+)*_(?:UNORM|SNORM|FLOAT)(?:_SRGB)?)')

    for file_path in deduped_path.iterdir():
        if not file_path.is_file():
            continue
        hash_result = hash_pattern.search(file_path.name)
        if hash_result is None:
            continue
        texture_hash = hash_result.group(1).lower()
        source_formats = []
        for detected_format in format_pattern.findall(file_path.stem.upper()):
            if detected_format not in source_formats:
                source_formats.append(detected_format)
        deduped_texture_info[texture_hash] = {
            'deduped_file': file_path.name,
            'source_formats': source_formats,
        }

    return deduped_texture_info


def export_bc7_srgb_textures(object_directory: Path, texture_format, enabled=False):
    if not enabled:
        return
    bc7_directory = object_directory / 'BC7_UNORM_SRGB'
    bc7_directory.mkdir(parents=True, exist_ok=True)
    for texture_info in texture_format.get('textures', {}).values():
        source_formats = [fmt.upper() for fmt in texture_info.get('source_formats', []) if isinstance(fmt, str)]
        if 'BC7_UNORM_SRGB' not in source_formats:
            continue
        texture_file = texture_info.get('file')
        if not texture_file:
            continue
        source_texture_path = object_directory / texture_file
        if source_texture_path.is_file():
            shutil.copyfile(source_texture_path, bc7_directory / source_texture_path.name)


def write_objects(output_directory, objects: Dict[str, ObjectData], allow_missing_shapekeys = False, deduped_texture_info=None):
    output_directory = Path(output_directory)

    output_directory.mkdir(parents=True, exist_ok=True)

    if deduped_texture_info is None:
        deduped_texture_info = {}

    for object_hash, object_data in objects.items():
        object_name = object_hash
        
        if object_data.shapekeys.offsets_hash and not object_data.shapekeys.shapekey_offsets:
            if allow_missing_shapekeys:
                object_name += '_MISSING_SHAPEKEYS'
            else:
                continue

        object_directory = output_directory / object_name
        object_directory.mkdir(parents=True, exist_ok=True)

        textures = {}
        texture_usage = {}
        texture_format = {
            'version': 1,
            'textures': OrderedDict(),
        }
        
        for component_id, component in enumerate(object_data.components):

            component_filename = f'Component {component_id}'

            # Write buffers
            with open(object_directory / f'{component_filename}.ib', "wb") as f:
                f.write(component.ib)
            with open(object_directory / f'{component_filename}.vb', "wb") as f:
                f.write(component.vb)
            with open(object_directory / f'{component_filename}.fmt', "w") as f:
                f.write(component.fmt)

            # Write textures
            texture_usage[component_filename] = OrderedDict()
            for texture in component.textures:

                if texture.hash not in textures:
                    textures[texture.hash] = {
                        'path': texture.path,
                        'components': []
                    }

                textures[texture.hash]['components'].append(str(component_id))

                slot = texture.get_slot()
                if slot not in texture_usage[component_filename]:
                    texture_usage[component_filename][slot] = []
                shaders = '-'.join([shader.raw for shader in texture.shaders])
                texture_usage[component_filename][slot].append(f'{texture.hash}-{shaders}')
                
            texture_usage[component_filename] = OrderedDict(sorted(texture_usage[component_filename].items()))

        for texture_hash, texture in textures.items():
            path = Path(texture['path'])
            components = '-'.join(sorted(list(set(texture['components']))))
            output_texture_filename = f'Components-{components} t={texture_hash}{path.suffix}'
            shutil.copyfile(path, object_directory / output_texture_filename)
            texture_hash_l = texture_hash.lower()
            deduped_info = deduped_texture_info.get(texture_hash_l, {})
            width, height = get_image_size(object_directory / output_texture_filename)
            texture_components = sorted([int(component_id) for component_id in set(texture['components'])])
            texture_format['textures'][texture_hash_l] = {
                'file': output_texture_filename,
                'source_formats': deduped_info.get('source_formats', []),
                'size': [width, height] if width is not None and height is not None else [],
                'components': texture_components,
            }
            
        with open(object_directory / f'TextureUsage.json', "w") as f:
            f.write(json.dumps(texture_usage, indent=4))

        with open(object_directory / f'Metadata.json', "w") as f:
            f.write(object_data.metadata)

        with open(object_directory / 'TextureFormat.json', "w") as f:
            f.write(json.dumps(texture_format, indent=4))
        export_bc7_srgb_textures(object_directory, texture_format, enabled=False)


def extract_frame_data(cfg):

    start_time = time.time()

    dump_path = resolve_path(cfg.frame_dump_folder)

    if not dump_path.is_dir():
        raise ConfigError('frame_dump_folder', 'Specified dump folder does not exist!')
    if not Path(dump_path / 'log.txt').is_file():
        raise ConfigError('frame_dump_folder', 'Specified dump folder is missing log.txt file!')

    collect_on_error = getattr(cfg, 'collect_extracted_resources', False)
    dump = None
    frame_data = None

    try:
        # Create data model of the frame dump
        dump = Dump(
            dump_directory=dump_path
        )

        # Get data view from dump data model
        frame_data = DataCollector(
            dump=dump,
            shader_data_pattern=configuration.shader_data_pattern,
            shader_resources=configuration.shader_resources
        )

        # Extract mesh objects data from data view
        data_extractor = DataExtractor(
            call_branches=frame_data.call_branches
        )

        # Build shape keys index from byte buffers
        shapekeys = ShapeKeyBuilder(
            shapekey_data=data_extractor.shape_key_data
        )

        # Build components from byte buffers
        component_builder = ComponentBuilder(
            output_vb_layout=configuration.output_vb_layout,
            shader_hashes=data_extractor.shader_hashes,
            shapekeys=shapekeys.shapekeys,
            draw_data=data_extractor.draw_data
        )

        # Build output data object
        output_builder = OutputBuilder(
            shapekeys=shapekeys.shapekeys,
            mesh_objects=component_builder.mesh_objects,
            texture_filter=TextureFilter(
                min_file_size=cfg.skip_small_textures_size*1024 if cfg.skip_small_textures else 0,
                exclude_extensions=['jpg'] if cfg.skip_jpg_textures else [],
                exclude_same_slot_hash_textures=cfg.skip_same_slot_hash_textures,
                exclude_hashes=['af26db30', '1320a071', '10d7937d', '87505b2b'] if cfg.skip_known_cubemap_textures else []
            )
        )

    except Exception:
        if collect_on_error:
            error_msg = traceback.format_exc()
            output_dir = resolve_path(cfg.extract_output_folder)
            if frame_data is not None and frame_data.call_branches:
                # call_branches are available: intelligent per-call collection
                collect_resources_on_error(output_dir, frame_data.call_branches, dump_path, error_msg)
            elif dump is not None:
                # DataCollector itself failed but Dump succeeded: collect all dump files
                collect_resources_on_error_from_dump(output_dir, dump, dump_path, error_msg)
        raise

    # Filter by IB hash if specified
    assign_hash = getattr(cfg, 'assign_hash', '').strip().lower()
    if assign_hash:
        ib_to_vb = {dd.ib_hash.lower(): dd.vb_hash for dd in data_extractor.draw_data.values() if dd.ib_hash}
        target_vb_hash = ib_to_vb.get(assign_hash)
        if target_vb_hash is None:
            available = ', '.join(sorted(ib_to_vb.keys()))
            print(f'Warning! IB hash "{assign_hash}" not found in frame dump, extraction will continue without IB hash filter. Available IB hashes: {available}')
            objects_to_write = output_builder.objects
        else:
            objects_to_write = {k: v for k, v in output_builder.objects.items() if k == target_vb_hash}
    else:
        objects_to_write = output_builder.objects

    deduped_texture_info = build_deduped_texture_info(dump_path)
    write_objects(resolve_path(cfg.extract_output_folder), objects_to_write, cfg.allow_missing_shapekeys, deduped_texture_info)

    if collect_on_error:
        output_dir_path = Path(resolve_path(cfg.extract_output_folder))
        for vb_hash in objects_to_write:
            collect_raw_resources(output_dir_path, data_extractor, vb_hash, dump_path)

    print(f"Execution time: %s seconds" % (time.time() - start_time))

    return output_builder


def get_dir_path():
    dir_path = ""

    if len(sys.argv) > 1:
        dir_path = sys.argv[1]

    if not os.path.exists(dir_path):
        print('Enter the name of frame dump folder:')
        dir_path = input()

    dir_path = os.path.abspath(dir_path)

    if not os.path.exists(dir_path):
        raise ValueError(f'Folder not found: {dir_path}!')
    if not os.path.isdir(dir_path):
        raise ValueError(f'Not a folder: {dir_path}!')

    return dir_path


if __name__ == "__main__":
    # try:
    extract_frame_data(configuration.dump_dir_path, configuration.output_path)
    # except Exception as e:
    #     print(f'Error: {e}')
    #     input()
