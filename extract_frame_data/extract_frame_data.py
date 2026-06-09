import os
import re
import sys
import time
import json
import shutil
import traceback
import struct
import subprocess

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
from ..migoto_io.dump_parser.resource_collector import Source, ResourceCollector
from ..migoto_io.dump_parser.calls_collector import ShaderMap, Slot, BranchCall, ShaderCallBranch
from ..migoto_io.dump_parser.data_collector import DataMap, DataCollector
from ..migoto_io.dump_parser.log_parser import CallParameters

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


@dataclass
class ExtractFrameDataResult:
    output_builder: OutputBuilder
    written_object_folders: Dict[str, str]

    @property
    def objects(self):
        return self.output_builder.objects


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
        'SHAPEKEY_VERTEX_ID_HASH': DataMap([Source('SHAPEKEY_CS_1', ShaderType.Compute, SlotType.Texture, SlotId(0))]),
        'SHAPEKEY_VERTEX_OFFSET_HASH': DataMap([Source('SHAPEKEY_CS_1', ShaderType.Compute, SlotType.Texture, SlotId(1))]),


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
            if hasattr(sk_data_list, 'entries'):
                for sk_entry in sk_data_list.entries:
                    paths.update(getattr(sk_entry, 'raw_resource_paths', ()))
            else:
                for sk_data in sk_data_list:
                    paths.update(getattr(sk_data, 'raw_resource_paths', ()))

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


DXGI_FORMAT_NAMES = {
    2: 'R32G32B32A32_FLOAT',
    3: 'R32G32B32A32_UINT',
    10: 'R16G16B16A16_FLOAT',
    11: 'R16G16B16A16_UNORM',
    28: 'R8G8B8A8_UNORM',
    29: 'R8G8B8A8_UNORM_SRGB',
    71: 'BC1_UNORM',
    72: 'BC1_UNORM_SRGB',
    74: 'BC2_UNORM',
    75: 'BC2_UNORM_SRGB',
    77: 'BC3_UNORM',
    78: 'BC3_UNORM_SRGB',
    80: 'BC4_UNORM',
    83: 'BC5_UNORM',
    95: 'BC6H_UF16',
    96: 'BC6H_SF16',
    98: 'BC7_UNORM',
    99: 'BC7_UNORM_SRGB',
}


DDS_FOURCC_FORMAT_NAMES = {
    b'DXT1': 'BC1_UNORM',
    b'DXT3': 'BC2_UNORM',
    b'DXT5': 'BC3_UNORM',
    b'ATI1': 'BC4_UNORM',
    b'BC4U': 'BC4_UNORM',
    b'ATI2': 'BC5_UNORM',
    b'BC5U': 'BC5_UNORM',
}


def get_dds_texture_info(texture_path: Path):
    try:
        with open(texture_path, 'rb') as f:
            header = f.read(148)
        if len(header) < 128 or header[:4] != b'DDS ':
            return {}

        height = struct.unpack_from('<I', header, 12)[0]
        width = struct.unpack_from('<I', header, 16)[0]
        fourcc = header[84:88]
        info = {'width': width, 'height': height}

        if fourcc == b'DX10' and len(header) >= 148:
            dxgi_format = struct.unpack_from('<I', header, 128)[0]
            if dxgi_format in DXGI_FORMAT_NAMES:
                info['format'] = DXGI_FORMAT_NAMES[dxgi_format]
        elif fourcc in DDS_FOURCC_FORMAT_NAMES:
            info['format'] = DDS_FOURCC_FORMAT_NAMES[fourcc]

        return info
    except Exception:
        return {}


def decode_subprocess_output(data):
    if not data:
        return ''
    if isinstance(data, str):
        return data
    for encoding in ('utf-8', 'mbcs', 'cp936'):
        try:
            return data.decode(encoding)
        except Exception:
            pass
    return data.decode('utf-8', errors='replace')


def get_texture_info(texdiag_path, texture_path):
    texture_path = Path(texture_path)
    if texture_path.suffix.lower() == '.dds':
        dds_info = get_dds_texture_info(texture_path)
        if dds_info.get('width') and dds_info.get('height') and dds_info.get('format'):
            return dds_info

    if not Path(texdiag_path).is_file():
        return get_dds_texture_info(texture_path) if texture_path.suffix.lower() == '.dds' else {}
    try:
        result = subprocess.run(
            [str(texdiag_path), 'info', str(texture_path)],
            capture_output=True,
            timeout=30
        )
        info = {}
        output = decode_subprocess_output(result.stdout)
        for line in output.splitlines():
            match = re.match(r'\s*(\S+(?:\s+\S+)?)\s*=\s*(.+)', line.strip())
            if not match:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key == 'width':
                info['width'] = int(value)
            elif key == 'height':
                info['height'] = int(value)
            elif key == 'format':
                info['format'] = value.replace('DXGI_FORMAT_', '')
        if texture_path.suffix.lower() == '.dds':
            dds_info = get_dds_texture_info(texture_path)
            dds_info.update({key: value for key, value in info.items() if value})
            return dds_info
        return info
    except Exception as e:
        print(f'Warning: Failed to get texture info for {texture_path}: {e}')
        return get_dds_texture_info(texture_path) if texture_path.suffix.lower() == '.dds' else {}


def ensure_draw_vs_calls(frame_data: DataCollector, dump: Dump, shader_resources: Dict[str, DataMap]):
    branch = frame_data.call_branches.get('DRAW_VS')
    if branch is not None and len(branch.calls) > 0:
        return

    draw_sources = []
    for resource_tag, data_map in shader_resources.items():
        sources = [source for source in data_map.sources if source.shader_id == 'DRAW_VS']
        if sources:
            draw_sources.append((resource_tag, data_map, sources))

    if not draw_sources:
        return

    resource_collector = ResourceCollector(shader_resources={}, call_branches={})
    fallback_branch = ShaderCallBranch(shader_id='DRAW_VS', calls=[], nested_branches=[])
    candidates = 0
    skipped = 0
    skipped_reasons = OrderedDict()

    def note_skip(reason):
        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    def enum_equal(lhs, rhs):
        if lhs == rhs:
            return True
        if getattr(lhs.__class__, '__name__', None) == getattr(rhs.__class__, '__name__', None):
            return getattr(lhs, 'name', None) == getattr(rhs, 'name', None) or getattr(lhs, 'value', None) == getattr(rhs, 'value', None)
        return False

    for call in sorted(dump.calls.values(), key=lambda c: int(c.id)):
        if not call.has_parameter(CallParameters.DrawIndexed):
            continue
        if not any(enum_equal(shader.type, ShaderType.Vertex) for shader in call.shaders.values()):
            continue

        candidates += 1
        branch_call = BranchCall(call=call, resources={})
        missing = False

        for resource_tag, data_map, sources in draw_sources:
            collected = False
            last_error = None
            for source in sources:
                try:
                    resource_collector.collect_branch_call_resource(branch_call, resource_tag, source, data_map.layout)
                    collected = resource_tag in branch_call.resources
                    if collected:
                        break
                except Exception as e:
                    last_error = e

            if not collected and not all(source.ignore_missing for source in sources):
                missing = True
                note_skip(f'{resource_tag}: {last_error or "missing"}')
                break

        if missing:
            skipped += 1
            continue

        fallback_branch.calls.append(branch_call)

    if branch is None:
        frame_data.call_branches['DRAW_VS'] = fallback_branch
    else:
        branch.calls = fallback_branch.calls

    print(
        f'DRAW_VS fallback: scanned {candidates} DrawIndexed VS calls, '
        f'accepted {len(fallback_branch.calls)}, skipped {skipped}'
    )
    if len(fallback_branch.calls) == 0 and skipped_reasons:
        for reason, count in list(skipped_reasons.items())[:10]:
            print(f'DRAW_VS fallback skip x{count}: {reason}')


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


def _texture_format_from_info(texture_hash, deduped_texture_info):
    info = deduped_texture_info.get(texture_hash.lower(), {})
    formats = info.get('source_formats', [])
    if not isinstance(formats, list) or len(formats) == 0:
        return ''
    return formats[0]


def write_objects(output_directory, objects: Dict[str, ObjectData], allow_missing_shapekeys = False, deduped_texture_info=None):
    output_directory = Path(output_directory)

    output_directory.mkdir(parents=True, exist_ok=True)

    if deduped_texture_info is None:
        deduped_texture_info = {}

    def enum_equal(lhs, rhs):
        if lhs == rhs:
            return True
        if getattr(lhs.__class__, '__name__', None) == getattr(rhs.__class__, '__name__', None):
            return getattr(lhs, 'name', None) == getattr(rhs, 'name', None) or getattr(lhs, 'value', None) == getattr(rhs, 'value', None)
        return False

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
        shader_texture_usage = {}
        texture_format = {
            'version': 1,
            'textures': OrderedDict(),
        }
        texture_metadata = {}
        
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
            shader_texture_usage[component_filename] = OrderedDict()
            for texture in component.textures:

                if texture.hash not in textures:
                    textures[texture.hash] = {
                        'path': texture.path,
                        'components': [],
                        'usage': []
                    }

                textures[texture.hash]['components'].append(str(component_id))
                textures[texture.hash]['usage'].append({
                    'component': component_id,
                    'call_id': int(texture.call_id) if texture.call_id is not None else None,
                    'slot': texture.get_slot(),
                    'slot_id': texture.slot_id,
                    'shader_type': texture.slot_shader_type.value if texture.slot_shader_type is not None else '',
                    'shaders': {
                        shader.type.value: shader.hash
                        for shader in texture.shaders
                        if shader.type is not None
                    },
                    'file': texture.raw,
                })

                slot = texture.get_slot()
                if slot not in texture_usage[component_filename]:
                    texture_usage[component_filename][slot] = []
                shaders = '-'.join([shader.raw for shader in texture.shaders])
                texture_usage[component_filename][slot].append(f'{texture.hash}-{shaders}')

                vs_ref = next((s for s in texture.shaders if enum_equal(s.type, ShaderType.Vertex)), None)
                ps_ref = next((s for s in texture.shaders if enum_equal(s.type, ShaderType.Pixel)), None)
                vs_key = vs_ref.raw if vs_ref else ''
                ps_key = ps_ref.raw if ps_ref else ''
                if vs_key not in shader_texture_usage[component_filename]:
                    shader_texture_usage[component_filename][vs_key] = OrderedDict()
                if ps_key not in shader_texture_usage[component_filename][vs_key]:
                    shader_texture_usage[component_filename][vs_key][ps_key] = OrderedDict()
                shader_texture_usage[component_filename][vs_key][ps_key][slot] = texture.hash
                
            texture_usage[component_filename] = OrderedDict(sorted(texture_usage[component_filename].items()))

        for texture_hash, texture in textures.items():
            texture_info_start = time.time()
            path = Path(texture['path'])
            output_texture_filename = f'{texture_hash}{path.suffix}'
            shutil.copyfile(path, object_directory / output_texture_filename)
            texture_hash_l = texture_hash.lower()
            deduped_info = deduped_texture_info.get(texture_hash_l, {})
            output_texture_path = object_directory / output_texture_filename
            texdiag_path = Path(__file__).resolve().parent.parent / 'DirectXTex' / 'texdiag.exe'
            texdiag_info = get_texture_info(texdiag_path, output_texture_path)
            width, height = texdiag_info.get('width'), texdiag_info.get('height')
            if width is None or height is None:
                width, height = get_image_size(output_texture_path)
            source_formats = deduped_info.get('source_formats', [])
            if texdiag_info.get('format') and texdiag_info['format'] not in source_formats:
                source_formats = [texdiag_info['format']] + list(source_formats)
            texture_components = sorted([int(component_id) for component_id in set(texture['components'])])
            texture_format['textures'][texture_hash_l] = {
                'file': output_texture_filename,
                'source_formats': source_formats,
                'size': [width, height] if width is not None and height is not None else [],
                'components': texture_components,
                'usage': sorted(texture.get('usage', []), key=lambda item: (
                    item.get('component', -1),
                    item.get('call_id') if item.get('call_id') is not None else -1,
                    item.get('slot_id') if item.get('slot_id') is not None else -1,
                )),
            }
            texture_metadata[texture_hash_l] = {
                'format': source_formats[0] if source_formats else '',
                'width': width or 0,
                'height': height or 0,
            }
            elapsed = time.time() - texture_info_start
            if elapsed > 1.0:
                print(f'Warning: Texture metadata for {output_texture_filename} took {elapsed:.3f}s')

        for component_key in shader_texture_usage:
            for vs_key in shader_texture_usage[component_key]:
                for ps_key in shader_texture_usage[component_key][vs_key]:
                    for slot_key in shader_texture_usage[component_key][vs_key][ps_key]:
                        hash_value = shader_texture_usage[component_key][vs_key][ps_key][slot_key]
                        texture_info = texture_format['textures'].get(hash_value.lower(), {})
                        metadata = texture_metadata.get(hash_value.lower(), {})
                        shader_texture_usage[component_key][vs_key][ps_key][slot_key] = {
                            'filename': texture_info.get('file', ''),
                            'hash': hash_value,
                            'format': metadata.get('format', _texture_format_from_info(hash_value, deduped_texture_info)),
                            'width': metadata.get('width', 0),
                            'height': metadata.get('height', 0),
                        }
            
        with open(object_directory / f'TextureUsage.json', "w") as f:
            f.write(json.dumps(texture_usage, indent=4))

        with open(object_directory / f'ShaderTextureUsage.json', "w") as f:
            f.write(json.dumps(shader_texture_usage, indent=4))

        with open(object_directory / f'Metadata.json', "w") as f:
            f.write(object_data.metadata)

        with open(object_directory / 'TextureFormat.json', "w") as f:
            f.write(json.dumps(texture_format, indent=4))
        export_bc7_srgb_textures(object_directory, texture_format, enabled=False)


def extract_frame_data(cfg):

    start_time = time.time()
    last_stage_time = start_time

    def trace_stage(stage_name):
        nonlocal last_stage_time
        now = time.time()
        print(f'Extract stage {stage_name}: {now - last_stage_time:.3f}s (total {now - start_time:.3f}s)')
        last_stage_time = now

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
        print(f'Frame dump indexed: {len(dump.resources)} resources, {len(dump.calls)} resource calls, {len(dump.log.calls)} log calls')
        trace_stage('Dump')

        # Get data view from dump data model
        frame_data = DataCollector(
            dump=dump,
            shader_data_pattern=configuration.shader_data_pattern,
            shader_resources=configuration.shader_resources
        )
        trace_stage('DataCollector')

        ensure_draw_vs_calls(frame_data, dump, configuration.shader_resources)
        trace_stage('DRAW_VS fallback')

        # Extract mesh objects data from data view
        data_extractor = DataExtractor(
            call_branches=frame_data.call_branches
        )
        print(f'Data extracted: {len(data_extractor.draw_data)} draw entries, {len(data_extractor.shape_key_data)} shapekey sets')
        trace_stage('DataExtractor')

        # Build shape keys index from byte buffers
        shapekeys = ShapeKeyBuilder(
            shapekey_data=data_extractor.shape_key_data
        )
        trace_stage('ShapeKeyBuilder')

        # Build components from byte buffers
        component_builder = ComponentBuilder(
            output_vb_layout=configuration.output_vb_layout,
            shader_hashes=data_extractor.shader_hashes,
            shapekeys=shapekeys.shapekeys,
            draw_data=data_extractor.draw_data
        )
        trace_stage('ComponentBuilder')

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
        trace_stage('OutputBuilder')

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
    trace_stage('HashFilter')

    deduped_texture_info = build_deduped_texture_info(dump_path)
    trace_stage('DedupedTextureInfo')

    extract_output_path = resolve_path(cfg.extract_output_folder)
    write_objects(extract_output_path, objects_to_write, cfg.allow_missing_shapekeys, deduped_texture_info)
    trace_stage('WriteObjects')

    written_object_folders = {
        vb_hash: str((extract_output_path / vb_hash).resolve())
        for vb_hash, object_data in objects_to_write.items()
        if cfg.allow_missing_shapekeys or object_data.shapekeys.shapekey_offsets
    }

    if collect_on_error:
        output_dir_path = Path(extract_output_path)
        for vb_hash in objects_to_write:
            collect_raw_resources(output_dir_path, data_extractor, vb_hash, dump_path)
        trace_stage('CollectRawResources')

    print(f"Execution time: %s seconds" % (time.time() - start_time))

    return ExtractFrameDataResult(output_builder, written_object_folders)


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
