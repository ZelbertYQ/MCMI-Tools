import subprocess
import json
import re
import os
import sys
import importlib
import time
import traceback
from pathlib import Path
import bpy

from textwrap import dedent

from bpy.props import BoolProperty, StringProperty, PointerProperty, IntProperty, FloatProperty, CollectionProperty

from .. import bl_info
from .. import addon_updater_ops

from .exceptions import clear_error, ConfigError

from ..migoto_io.blender_interface.objects import *
from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.utility import *

from ..blender_export.blender_export import blender_export
from ..blender_export.ini_maker import IniMaker
from ..extract_frame_data.metadata_format import read_metadata

from .modules.toolbox.ui import *
from ..language import tr
from ..migoto_io.blender_tools.vertex_groups import fill_gaps_in_vertex_groups, merge_vertex_groups


class TeeStream:
    def __init__(self, *streams):
        self.streams = [stream for stream in streams if stream is not None]

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def get_error_log_path():
    return Path(__file__).resolve().parents[1] / 'Error.log'


def parse_component_id(name):
    result = re.findall(r'component[ \-_]*([0-9]+)', (name or '').lower())
    if len(result) == 0:
        return None
    return int(result[0])


def reverse_folder_available():
    addon_root = Path(__file__).resolve().parents[1]
    return (addon_root / 'Reverse').is_dir()


def get_reloaded_extract_frame_data():
    package_root = __package__.rsplit('.', 1)[0]
    reload_order = [
        'migoto_io.data_model.dxgi_format',
        'migoto_io.data_model.byte_buffer',
        'migoto_io.dump_parser.dict_filter',
        'migoto_io.dump_parser.log_parser',
        'migoto_io.dump_parser.filename_parser',
        'migoto_io.dump_parser.dump_parser',
        'migoto_io.dump_parser.calls_collector',
        'migoto_io.dump_parser.resource_collector',
        'migoto_io.dump_parser.data_collector',
        'extract_frame_data.data_extractor',
        'extract_frame_data.shapekey_builder',
        'extract_frame_data.component_builder',
        'extract_frame_data.output_builder',
        'extract_frame_data.extract_frame_data',
    ]
    module = None
    for relative_name in reload_order:
        module_name = f'{package_root}.{relative_name}'
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(f'..{relative_name}', __package__)
    return module.extract_frame_data


def get_reloaded_blender_import():
    package_root = __package__.rsplit('.', 1)[0]
    reload_order = [
        'migoto_io.data_model.dxgi_format',
        'migoto_io.data_model.byte_buffer',
        'migoto_io.data_model.data_importer',
        'migoto_io.data_model.data_extractor',
        'migoto_io.data_model.data_model',
        'extract_frame_data.metadata_format',
        'blender_import.material_setup',
        'blender_import.blender_import',
    ]
    module = None
    for relative_name in reload_order:
        module_name = f'{package_root}.{relative_name}'
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(f'..{relative_name}', __package__)
    return module.blender_import


def is_effective_merged_skeleton(mode: str) -> bool:
    return mode in ['MERGED', 'COMPONENT_TO_MERGED']


def get_reverse_tools():
    if not reverse_folder_available():
        return None
    try:
        from ..Reverse import reverse_tools
    except Exception:
        return None
    return reverse_tools


def get_collection_component_meshes(collection):
    result = {}
    for obj in get_collection_objects(collection, recursive=True, skip_hidden_collections=False):
        if obj.type != 'MESH':
            continue
        component_id = parse_component_id(obj.name)
        if component_id is None:
            continue
        if component_id not in result:
            result[component_id] = []
        result[component_id].append(obj)
    for meshes in result.values():
        meshes.sort(key=lambda item: item.name)
    return result


def get_vg_id(vg):
    if vg.name.isdigit():
        return int(vg.name)
    return vg.index


def get_vg_centers(objs):
    if not isinstance(objs, list):
        objs = [objs]
    accum = {}
    for obj in objs:
        for vg in obj.vertex_groups:
            vg_id = get_vg_id(vg)
            if vg_id not in accum:
                accum[vg_id] = [0.0, 0.0, 0.0, 0.0]
            for vertex in obj.data.vertices:
                weight = 0.0
                for group in vertex.groups:
                    if group.group == vg.index:
                        weight = group.weight
                        break
                if weight <= 0:
                    continue
                co = vertex.co
                accum[vg_id][0] += co.x * weight
                accum[vg_id][1] += co.y * weight
                accum[vg_id][2] += co.z * weight
                accum[vg_id][3] += weight
    centers = {}
    for vg_id, values in accum.items():
        if values[3] > 0:
            centers[vg_id] = (values[0] / values[3], values[1] / values[3], values[2] / values[3])
    return centers


def get_vg_ids_from_meshes(meshes):
    vg_ids = set()
    for obj in meshes:
        for vg in obj.vertex_groups:
            vg_ids.add(get_vg_id(vg))
    return vg_ids


def calculate_vg_map(base_objs, target_objs):
    vg_map = {}
    base_centers = get_vg_centers(base_objs)
    target_centers = get_vg_centers(target_objs)
    base_vg_ids = get_vg_ids_from_meshes(base_objs)
    target_vg_ids = get_vg_ids_from_meshes(target_objs)
    if len(target_vg_ids) == 0:
        return vg_map

    for base_vg in sorted(base_vg_ids):
        base_center = base_centers.get(base_vg, None)
        if base_center is not None and len(target_centers) > 0:
            best = None
            best_dist = None
            for target_vg, target_center in target_centers.items():
                dx = base_center[0] - target_center[0]
                dy = base_center[1] - target_center[1]
                dz = base_center[2] - target_center[2]
                dist = dx * dx + dy * dy + dz * dz
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = target_vg
        else:
            best = sorted(target_vg_ids, key=lambda value: abs(value - base_vg))[0]
        vg_map[str(base_vg)] = int(best)
    return vg_map


def build_lod_vg_map(base_collection, target_collection):
    base_components = get_collection_component_meshes(base_collection)
    target_components = get_collection_component_meshes(target_collection)
    result = {}
    component_ids = set(base_components.keys()) & set(target_components.keys())
    for component_id in sorted(component_ids):
        base_meshes = base_components.get(component_id, [])
        target_meshes = target_components.get(component_id, [])
        result[f'Component {component_id}'] = calculate_vg_map(base_meshes, target_meshes)
    return result


def build_lod_vg_map_merged(base_meshes, target_meshes):
    return calculate_vg_map(base_meshes, target_meshes)


def get_component_ids_from_source_folder(source_folder):
    source_path = resolve_path(source_folder)
    metadata_path = source_path / 'Metadata.json'
    if not metadata_path.is_file():
        return set()
    try:
        metadata = read_metadata(metadata_path)
        components = getattr(metadata, 'components', [])
        return set(range(len(components)))
    except Exception:
        return set()


def get_component_vg_ids_from_source_folder(source_folder, use_local_ids=False):
    source_path = resolve_path(source_folder)
    metadata_path = source_path / 'Metadata.json'
    if not metadata_path.is_file():
        return {}
    try:
        metadata = read_metadata(metadata_path)
        result = {}
        for component_id, component in enumerate(getattr(metadata, 'components', [])):
            vg_map = getattr(component, 'vg_map', {})
            if isinstance(vg_map, dict):
                if use_local_ids:
                    ids = set(range(len(vg_map)))
                else:
                    ids = set()
                    for value in vg_map.values():
                        if isinstance(value, int):
                            ids.add(value)
                result[component_id] = ids
        return result
    except Exception:
        return {}


def get_all_vg_ids_from_source_folder(source_folder):
    component_vg_ids = get_component_vg_ids_from_source_folder(source_folder)
    result = set()
    for ids in component_vg_ids.values():
        result |= set(ids)
    return result


def ensure_vg_layout_on_collection(context, collection, source_folder, merged_mode):
    component_meshes = get_collection_component_meshes(collection)
    if merged_mode:
        target_ids = sorted(get_all_vg_ids_from_source_folder(source_folder))
        for meshes in component_meshes.values():
            for obj in meshes:
                existing = {get_vg_id(vg) for vg in obj.vertex_groups}
                for vg_id in target_ids:
                    if vg_id in existing:
                        continue
                    obj.vertex_groups.new(name=str(vg_id))
                fill_gaps_in_vertex_groups(context, obj, internal_call=True)
        return

    target_ids_map = get_component_vg_ids_from_source_folder(source_folder, use_local_ids=True)
    for component_id, meshes in component_meshes.items():
        target_ids = sorted(target_ids_map.get(component_id, set()))
        if len(target_ids) == 0:
            continue
        for obj in meshes:
            existing = {get_vg_id(vg) for vg in obj.vertex_groups}
            for vg_id in target_ids:
                if vg_id in existing:
                    continue
                obj.vertex_groups.new(name=str(vg_id))
            fill_gaps_in_vertex_groups(context, obj, internal_call=True)


def merge_collection_meshes_for_mapping(context, collection):
    meshes = [obj for obj in get_collection_objects(collection, recursive=True, skip_hidden_collections=False) if obj.type == 'MESH']
    if len(meshes) <= 1:
        return meshes[0] if len(meshes) == 1 else None
    meshes = sorted(meshes, key=lambda obj: obj.name)
    join_objects(context, meshes)
    return meshes[0]


def duplicate_collection_for_lod(context, source_collection, name_suffix):
    scene_collection = context.scene.collection
    duplicated = bpy.data.collections.new(f'{source_collection.name}_{name_suffix}')
    scene_collection.children.link(duplicated)
    for obj in get_collection_objects(source_collection, recursive=True, skip_hidden_collections=False):
        copy_obj = obj.copy()
        if obj.data is not None:
            copy_obj.data = obj.data.copy()
        duplicated.objects.link(copy_obj)
    return duplicated


def apply_lod_vg_map_to_collection(context, collection, lod_map):
    component_meshes = get_collection_component_meshes(collection)
    if isinstance(lod_map, dict) and all(str(key).isdigit() for key in lod_map.keys()):
        for meshes in component_meshes.values():
            for obj in meshes:
                rename_vertex_groups_on_object(obj, lod_map)
                merge_vertex_groups(context, obj)
                fill_gaps_in_vertex_groups(context, obj, internal_call=True)
        return
    for component_name, vg_map in lod_map.items():
        component_id = parse_component_id(component_name)
        if component_id is None:
            continue
        meshes = component_meshes.get(component_id, [])
        if len(meshes) == 0:
            continue
        for obj in meshes:
            rename_vertex_groups_on_object(obj, vg_map)
            merge_vertex_groups(context, obj)
            fill_gaps_in_vertex_groups(context, obj, internal_call=True)


def rename_vertex_groups_on_object(obj, vg_map):
    if not isinstance(vg_map, dict) or len(vg_map) == 0:
        return
    for source_key, target_id in vg_map.items():
        if not str(source_key).isdigit():
            continue
        if not isinstance(target_id, int):
            continue
        src_name = str(int(source_key))
        dst_name = str(target_id)
        if src_name == dst_name:
            continue
        src_group = obj.vertex_groups.get(src_name)
        if src_group is None:
            continue
        src_group.name = f'{dst_name}.{src_name}'


def remap_vertex_groups_on_object(obj, vg_map):
    if not isinstance(vg_map, dict) or len(vg_map) == 0:
        return
    source_weights = {}
    for source_key, target_id in vg_map.items():
        if not str(source_key).isdigit():
            continue
        if not isinstance(target_id, int):
            continue
        src_name = str(int(source_key))
        src_group = obj.vertex_groups.get(src_name)
        if src_group is None:
            continue
        src_index = src_group.index
        source_weights[src_name] = []
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                if group.group == src_index and group.weight > 0:
                    source_weights[src_name].append((vertex.index, group.weight))
                    break

    for source_key, target_id in vg_map.items():
        if not str(source_key).isdigit():
            continue
        if not isinstance(target_id, int):
            continue
        src_name = str(int(source_key))
        dst_name = str(target_id)
        if src_name == dst_name:
            continue
        dst_group = obj.vertex_groups.get(dst_name)
        if dst_group is None:
            dst_group = obj.vertex_groups.new(name=dst_name)
        for vertex_index, weight in source_weights.get(src_name, []):
            dst_group.add([vertex_index], weight, 'ADD')
    for source_key, target_id in vg_map.items():
        if not str(source_key).isdigit():
            continue
        src_name = str(int(source_key))
        dst_name = str(target_id) if isinstance(target_id, int) else ''
        if src_name == dst_name:
            continue
        src_group = obj.vertex_groups.get(src_name)
        if src_group is not None:
            obj.vertex_groups.remove(src_group)


def parse_texture_hashes_from_folder(folder_path):
    result = set()
    if not os.path.isdir(folder_path):
        return result
    pattern_new = re.compile(r'.*t=([a-f0-9]{8}).*')
    pattern_hash = re.compile(r'^([a-f0-9]{8})\.[a-z0-9]+$')
    pattern_old = re.compile(r'.*component_\d-ps-t\d-([a-f0-9]{8}).*')
    for filename in os.listdir(folder_path):
        lower = filename.lower()
        if not (lower.endswith('.dds') or lower.endswith('.jpg')):
            continue
        found = pattern_new.findall(lower)
        if len(found) != 1:
            found = pattern_hash.findall(lower)
        if len(found) != 1:
            found = pattern_old.findall(lower)
        if len(found) == 1:
            result.add(found[0])
    return result


def split_ini_sections(lines):
    preamble = []
    sections = []
    current_header = None
    current_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if current_header is None:
                preamble = list(current_lines)
            else:
                sections.append((current_header, list(current_lines)))
            current_header = stripped
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_header is None:
        preamble = list(current_lines)
    else:
        sections.append((current_header, list(current_lines)))
    return preamble, sections


def parse_component0_hash_from_ini(mod_folder):
    ini_path = os.path.join(mod_folder, 'mod.ini')
    if not os.path.isfile(ini_path):
        return None
    with open(ini_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    _, sections = split_ini_sections(lines)
    for header, section_lines in sections:
        if header.lower() != '[textureoverridecomponent0]':
            continue
        for line in section_lines:
            match = re.findall(r'^\s*hash\s*=\s*([a-f0-9]{8})\s*$', line.strip().lower())
            if len(match) == 1:
                return match[0]
    return None


def insert_missing_constants(lines, entries):
    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == '[constants]':
            section_start = i
            break
    if section_start is None:
        return lines
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section_end = i
            break
    section_existing = {line.strip().lower() for line in lines[section_start + 1:section_end]}
    missing = [entry for entry in entries if entry.lower() not in section_existing]
    if len(missing) == 0:
        return lines
    insertion = [f'{entry}\n' for entry in missing]
    return lines[:section_end] + insertion + lines[section_end:]


def insert_missing_present_logic(lines, entries):
    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == '[present]':
            section_start = i
            break
    if section_start is None:
        return lines
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section_end = i
            break
    section_existing = {line.strip().lower() for line in lines[section_start + 1:section_end]}
    missing = [entry for entry in entries if entry.lower() not in section_existing]
    if len(missing) == 0:
        return lines
    insertion = [f'{entry}\n' for entry in missing]
    return lines[:section_end] + insertion + lines[section_end:]


def insert_lod_override_sections(lines, lod_hashes):
    if len(lod_hashes) == 0:
        return lines
    joined = ''.join(lines).lower()
    if '[textureoverridelod1]' in joined or '[textureoverridelod2]' in joined:
        return lines
    shading_idx = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith('; shading: textures'):
            shading_idx = i
            break
    if shading_idx is None:
        return lines

    block = ['\n']
    if 'LOD1' in lod_hashes:
        block.extend([
            '[TextureOverrideLOD1]\n',
            f'hash = {lod_hashes["LOD1"]}\n',
            '$LOD1 = 1\n',
            '\n',
        ])
    if 'LOD2' in lod_hashes:
        block.extend([
            '[TextureOverrideLOD2]\n',
            f'hash = {lod_hashes["LOD2"]}\n',
            '$LOD2 = 1\n',
            '\n',
        ])
    return lines[:shading_idx + 1] + block + lines[shading_idx + 1:]


def patch_texture_override_conditions(lines):
    result = list(lines)
    in_texture_override = False
    for i, line in enumerate(result):
        stripped = line.strip()
        stripped_lower = stripped.lower()
        if stripped.startswith('[') and stripped.endswith(']'):
            in_texture_override = re.fullmatch(r'\[textureoverridetexture(-[a-f0-9]{8}|\d+)\]', stripped_lower) is not None
            continue
        if in_texture_override and stripped_lower == 'if $object_detected':
            result[i] = line.replace('if $object_detected', 'if $object_detected || $LOD >= 1')
    return result


def patch_lod0_ini_for_shared_textures(mod_folder, lod_hashes):
    if len(lod_hashes) == 0:
        return
    ini_path = os.path.join(mod_folder, 'mod.ini')
    if not os.path.isfile(ini_path):
        return
    with open(ini_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    lines = insert_missing_constants(lines, [
        'global persist $LOD = 0',
        'global persist $LOD1 = 0',
        'global persist $LOD2 = 0',
    ])
    lines = insert_missing_present_logic(lines, [
        '$LOD = $LOD1+$LOD2',
        '$LOD1 = 0',
        '$LOD2 = 0',
    ])
    lines = insert_lod_override_sections(lines, lod_hashes)
    lines = patch_texture_override_conditions(lines)

    with open(ini_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def dedupe_lod_textures_and_ini(mod_folder, lod_hashes):
    textures_dir = os.path.join(mod_folder, 'Textures')
    if os.path.isdir(textures_dir):
        for filename in os.listdir(textures_dir):
            lower = filename.lower()
            hash_match = re.search(r't=([a-f0-9]{8})', lower)
            if hash_match is None:
                hash_match = re.match(r'^([a-f0-9]{8})\.[a-z0-9]+$', lower)
            if hash_match is not None and hash_match.group(1) in lod_hashes:
                try:
                    os.remove(os.path.join(textures_dir, filename))
                except Exception:
                    pass
    ini_path = os.path.join(mod_folder, 'mod.ini')
    if not os.path.isfile(ini_path):
        return
    with open(ini_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    preamble, sections = split_ini_sections(lines)

    duplicate_texture_indices = set()
    duplicate_texture_hashes = set()
    for header, section in sections:
        header_lower = header.lower()
        match_header = re.fullmatch(r'\[textureoverridetexture(-[a-f0-9]{8}|\d+)\]', header_lower)
        if match_header is None:
            continue
        header_key = match_header.group(1)
        header_hash = header_key[1:] if header_key.startswith('-') else None
        if header_hash is not None and header_hash in lod_hashes:
            duplicate_texture_hashes.add(header_hash)
            continue
        hash_value = None
        for line in section:
            match_hash = re.findall(r'^\s*hash\s*=\s*([a-f0-9]{8})\s*$', line.strip().lower())
            if len(match_hash) == 1:
                hash_value = match_hash[0]
                break
        if hash_value is not None and hash_value in lod_hashes:
            if header_hash is not None:
                duplicate_texture_hashes.add(header_hash)
            else:
                duplicate_texture_indices.add(header_key)

    filtered = list(preamble)
    for header, section in sections:
        header_lower = header.lower()
        texture_resource_match = re.fullmatch(r'\[resourcetexture(-[a-f0-9]{8}|\d+)\]', header_lower)
        texture_override_match = re.fullmatch(r'\[textureoverridetexture(-[a-f0-9]{8}|\d+)\]', header_lower)
        texture_key = None
        if texture_resource_match is not None:
            texture_key = texture_resource_match.group(1)
        elif texture_override_match is not None:
            texture_key = texture_override_match.group(1)
        if texture_key is not None:
            if texture_key.startswith('-'):
                if texture_key[1:] in duplicate_texture_hashes:
                    continue
            elif texture_key in duplicate_texture_indices:
                continue
        filtered.extend(section)
    with open(ini_path, 'w', encoding='utf-8') as f:
        f.writelines(filtered)


def parse_lod_map(raw):
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_character_map_key(source_folder):
    source_path = resolve_path(source_folder)
    metadata_path = source_path / 'Metadata.json'
    if metadata_path.is_file():
        try:
            metadata = read_metadata(metadata_path)
            vb0_hash = getattr(metadata, 'vb0_hash', '')
            if vb0_hash:
                return str(vb0_hash).lower()
        except Exception:
            pass
    return source_path.name.lower()


def get_selected_profile_key(cfg):
    selected_profile = getattr(cfg, 'lod_map_profile', 'AUTO')
    if selected_profile and selected_profile != 'AUTO':
        return selected_profile
    return get_character_map_key(cfg.object_source_folder)


def get_lod_maps_text():
    text_name = 'MCMI_LodMaps'
    text = bpy.data.texts.get(text_name)
    if text is None:
        text = bpy.data.texts.new(text_name)
        text.write(json.dumps({'characters': {}}, indent=4, ensure_ascii=False))
    return text


def get_profile_lod_map_text(profile_key):
    text_name = profile_key
    text = bpy.data.texts.get(text_name)
    if text is None:
        text = bpy.data.texts.new(text_name)
    return text


def parse_lod_maps_document(raw):
    data = parse_lod_map(raw)
    if 'characters' not in data or not isinstance(data['characters'], dict):
        data = {'characters': {}}
    return data


def read_lod_maps_document():
    text = get_lod_maps_text()
    return parse_lod_maps_document(text.as_string())


def write_lod_maps_document(doc):
    text = get_lod_maps_text()
    text.clear()
    text.write(json.dumps(doc, indent=4, ensure_ascii=False))


def save_lod_maps_for_character(cfg, lod1_map, lod2_map):
    character_key = get_character_map_key(cfg.object_source_folder)
    doc = read_lod_maps_document()
    profile_data = {
        'lod1': lod1_map if isinstance(lod1_map, dict) else {},
        'lod2': lod2_map if isinstance(lod2_map, dict) else {},
        'object_source_folder': str(resolve_path(cfg.object_source_folder)),
    }
    doc['characters'][character_key] = profile_data
    write_lod_maps_document(doc)
    profile_text = get_profile_lod_map_text(character_key)
    profile_text.clear()
    profile_text.write(json.dumps(profile_data, indent=4, ensure_ascii=False))
    cfg.lod_map_profile = character_key


def resolve_lod_maps_for_character(cfg):
    character_key = get_selected_profile_key(cfg)
    profile_text = bpy.data.texts.get(character_key)
    if profile_text is not None:
        profile_data = parse_lod_map(profile_text.as_string())
        lod1_map = profile_data.get('lod1', {}) if isinstance(profile_data, dict) else {}
        lod2_map = profile_data.get('lod2', {}) if isinstance(profile_data, dict) else {}
        if isinstance(lod1_map, dict) and isinstance(lod2_map, dict) and (len(lod1_map) > 0 or len(lod2_map) > 0):
            return lod1_map, lod2_map
    doc = read_lod_maps_document()
    character = doc.get('characters', {}).get(character_key, {})
    lod1_map = character.get('lod1', {})
    lod2_map = character.get('lod2', {})
    if not isinstance(lod1_map, dict):
        lod1_map = {}
    if not isinstance(lod2_map, dict):
        lod2_map = {}
    if len(lod1_map) == 0:
        lod1_map = parse_lod_map(getattr(cfg, 'lod1_vg_map', '{}'))
    if len(lod2_map) == 0:
        lod2_map = parse_lod_map(getattr(cfg, 'lod2_vg_map', '{}'))
    return lod1_map, lod2_map


def cleanup_temp_collection(collection):
    if collection is None:
        return
    for obj in list(collection.objects):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            continue
        except Exception:
            continue
    try:
        bpy.data.collections.remove(collection)
    except Exception:
        pass


def keep_lod_temp_export_object(context, temp_collection, lod_name, lod_map=None, merged_mode=False):
    candidates = []
    for obj in list(temp_collection.objects):
        if obj.name.startswith('TEMP_EXPORT_OBJECT'):
            candidates.append(obj)
    if len(candidates) == 0:
        return
    candidates.sort(key=lambda item: item.name)
    export_obj = candidates[-1]
    if merged_mode and isinstance(lod_map, dict) and all(str(key).isdigit() for key in lod_map.keys()):
        remap_vertex_groups_on_object(export_obj, lod_map)
        fill_gaps_in_vertex_groups(context, export_obj, internal_call=True)
    target_name = f'TEMP_EXPORT_OBJECT_{lod_name}'
    export_obj.name = target_name
    if export_obj.name != target_name:
        export_obj.name = target_name
    if context.scene.collection not in export_obj.users_collection:
        context.scene.collection.objects.link(export_obj)
    if temp_collection in export_obj.users_collection:
        temp_collection.objects.unlink(export_obj)


def add_row_with_error_handler(layout, cfg, setting_names):
    if not cfg.last_error_setting_name or cfg.last_error_setting_name not in setting_names:
        return layout.row()
    else:
        layout.alert = True
        row = layout.row()
        error_lines = cfg.last_error_text.split('\n')
        
        if len(error_lines) == 1:
            error_row = layout.row()
            error_row.alignment = 'CENTER'
            error_row.label(text=error_lines[0], icon='ERROR')
        else:
            error_box = layout.box()
            error_box.label(text=error_lines[0], icon='ERROR')
            for line in error_lines[1:]:
                if not line.strip():
                    continue
                error_box.label(text=line, icon='BLANK1') 
        layout.alert = False
        return row


DISABLED_EXPORT_PREFIXES = ('Disabled-', 'Disabled+')


def resolve_export_mod_output_folder(mod_output_folder):
    mod_output_folder = (mod_output_folder or '').strip()
    if not mod_output_folder:
        return None

    base_path = resolve_path(mod_output_folder)
    base_name = base_path.name
    if not base_name:
        return None

    stripped_name = base_name
    for prefix in DISABLED_EXPORT_PREFIXES:
        if stripped_name.startswith(prefix):
            stripped_name = stripped_name[len(prefix):]
            break

    candidate_names = []

    def add_candidate(name):
        if name and name not in candidate_names:
            candidate_names.append(name)

    add_candidate(base_name)
    if stripped_name != base_name:
        add_candidate(stripped_name)
        for prefix in DISABLED_EXPORT_PREFIXES:
            add_candidate(prefix + stripped_name)
    else:
        for prefix in DISABLED_EXPORT_PREFIXES:
            add_candidate(prefix + base_name)

    candidates = []
    seen_paths = set()
    for name in candidate_names:
        candidate = base_path.with_name(name).resolve()
        if candidate not in seen_paths:
            seen_paths.add(candidate)
            candidates.append(candidate)

    existing = [candidate for candidate in candidates if candidate.is_dir()]
    if not existing:
        return None

    for candidate in existing:
        if candidate == base_path:
            return candidate

    for candidate in existing:
        if not candidate.name.startswith(DISABLED_EXPORT_PREFIXES):
            return candidate

    return existing[0]


class MCMI_TOOLS_PT_SIDEBAR(bpy.types.Panel):
    """
    Wuthering Waves modding toolkit
    """

    bl_idname = "MCMI_TOOLS_PT_SIDEBAR"
    bl_label = "MCMI Tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCMI Tools"
    # bl_context = "objectmode"

    # @classmethod
    # def poll(cls, context):
    #     return (context.object is not None)

    def draw_header(self, context):
        layout = self.layout
        row = layout.row()
        row.alignment = 'RIGHT'
        row.label(text="v"+".".join(str(i) for i in bl_info.get('version', (0, 0, 0))))

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        cfg = context.scene.mcmi_tools_settings
        layout = self.layout

        prefs = addon_updater_ops.get_user_preferences(context)
        if prefs:
            row = layout.row(align=True)
            row.prop(prefs, 'ui_language', expand=True)

        if bpy.app.version[:2] == (4, 2):
            layout.alert = True
            layout.label(text=tr('blender_42_warning'), icon="ERROR")
            layout.label(text=tr('blender_42_detail'))
            layout.alert = False
            return

        row = layout.row(align=True)
        row.prop(cfg, 'tool_mode', text=tr('tool_mode'))
        row.prop(cfg, 'enable_lod_mode', text=tr('enable_lod_mode'), toggle=True)

        if cfg.tool_mode == 'TOOLS_MODE':
            self.draw_menu_tools_mode(context)

        if cfg.tool_mode == 'EXPORT_MOD':
            self.draw_menu_export_mod(context)

        elif cfg.tool_mode == 'IMPORT_OBJECT':
            self.draw_menu_import_object(context)

        elif cfg.tool_mode == 'EXTRACT_FRAME_DATA':
            self.draw_menu_extract_frame_data(context)

    def draw_menu_tools_mode(self, context):
        cfg = context.scene.mcmi_tools_settings
        layout = self.layout

        layout.row().operator(MCMI_ApplyModifierForObjectWithShapeKeysOperator.bl_idname, text=tr('apply_modifier_sk'))
        layout.row().operator(MCMI_MergeVertexGroups.bl_idname, text=tr('merge_vg'))
        layout.row().operator(MCMI_FillGapsInVertexGroups.bl_idname, text=tr('fill_gaps_vg'))
        layout.row().operator(MCMI_RemoveUnusedVertexGroups.bl_idname, text=tr('remove_unused_vg'))
        layout.row().operator(MCMI_RemoveAllVertexGroups.bl_idname, text=tr('remove_all_vg'))
        layout.row().operator(MCMI_CreateMergedObject.bl_idname, text=tr('create_merged'))
        layout.row().operator(MCMI_ApplyMergedObjectSculpt.bl_idname, text=tr('apply_merged_sculpt'))
        layout.row().operator(MCMI_ApplyMergedObjectSculptWithShapekeys.bl_idname, text=tr('apply_merged_sculpt_sk'))
        layout.row().operator(MCMI_ConvertVertexColors.bl_idname, text=tr('convert_vertex_colors'))
        

    def draw_menu_export_mod(self, context):
        cfg = context.scene.mcmi_tools_settings
        layout = self.layout
        
        layout.row()
        
        row = add_row_with_error_handler(layout, cfg, 'component_collection')
        row.prop(cfg, 'component_collection', text=tr('component_collection'))

        row = add_row_with_error_handler(layout, cfg, 'object_source_folder')
        row.prop(cfg, 'object_source_folder', text=tr('object_source_folder'))
        if cfg.enable_lod_mode:
            row = add_row_with_error_handler(layout, cfg, 'lod1_source_folder')
            row.prop(cfg, 'lod1_source_folder', text=tr('lod1_source_folder'))
            row = add_row_with_error_handler(layout, cfg, 'lod2_source_folder')
            row.prop(cfg, 'lod2_source_folder', text=tr('lod2_source_folder'))
            row = layout.row(align=True)
            row.prop(cfg, 'lod_map_profile', text=tr('lod_map_profile'))
            row.operator(MCMI_OpenLodMapEditor.bl_idname, text=tr('open_lod_map_editor'))

        row = add_row_with_error_handler(layout, cfg, 'mod_output_folder')
        row.prop(cfg, 'mod_output_folder', text=tr('mod_output_folder'))
        
        layout.row().prop(cfg, 'mod_skeleton_type', text=tr('mod_skeleton_type'))
        layout.row().prop(cfg, 'texture_mode', text=tr('texture_mode'))

        if not cfg.partial_export:

            layout.row()

            layout.row().prop(cfg, 'mirror_mesh', text=tr('mirror_mesh'))
            layout.row().prop(cfg, 'apply_all_modifiers', text=tr('apply_all_modifiers'))
            row = layout.row(align=True)
            row.prop(cfg, 'copy_textures', text=tr('copy_textures'))
            row.prop(cfg, 'update_textures', text=tr('update_textures'))
            if cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
                row.prop(cfg, 'export_textures', text=tr('export_textures'))

            col = layout.column(align=True)
            grid = col.grid_flow(columns=2, align=True)
            grid.alignment = 'LEFT'
            grid.prop(cfg, 'write_ini', text=tr('write_ini'))
            if cfg.write_ini:
                grid.prop(cfg, 'comment_ini', text=tr('comment_ini'))
                    
            layout.row()
            layout.row()

            if bpy.app.version >= (3, 5):
                row = layout.row()
                row.prop(cfg, 'ignore_nested_collections', text=tr('ignore_nested_collections'))
                if not cfg.ignore_nested_collections:
                    row.prop(cfg, 'ignore_hidden_collections', text=tr('ignore_hidden_collections'))
                
            layout.row().prop(cfg, 'ignore_hidden_objects', text=tr('ignore_hidden_objects'))
            layout.row().prop(cfg, 'ignore_muted_shape_keys', text=tr('ignore_muted_shape_keys'))


    def draw_menu_import_object(self, context):
        cfg = context.scene.mcmi_tools_settings
        layout = self.layout
        
        layout.row()

        row = add_row_with_error_handler(layout, cfg, 'object_source_folder')
        row.prop(cfg, 'object_source_folder', text=tr('object_source_folder'))
        if cfg.enable_lod_mode:
            row = add_row_with_error_handler(layout, cfg, 'lod1_source_folder')
            row.prop(cfg, 'lod1_source_folder', text=tr('lod1_source_folder'))
            row = add_row_with_error_handler(layout, cfg, 'lod2_source_folder')
            row.prop(cfg, 'lod2_source_folder', text=tr('lod2_source_folder'))

        layout.row().prop(cfg, 'color_storage', text=tr('color_storage'))
        layout.row().prop(cfg, 'import_skeleton_type', text=tr('import_skeleton_type'))
        if cfg.import_skeleton_type == 'MERGED':
            layout.row().prop(cfg, 'skip_empty_vertex_groups', text=tr('skip_empty_vertex_groups'))
        layout.row().prop(cfg, 'mirror_mesh', text=tr('mirror_mesh'))

        layout.row()

        layout.row().operator(MCMI_Import.bl_idname, text=tr('import_object'))

    def draw_menu_extract_frame_data(self, context):
        cfg = context.scene.mcmi_tools_settings
        layout = self.layout
        
        layout.row()

        reverse_enabled = reverse_folder_available()
        if reverse_enabled:
            layout.row().prop(cfg, 'extract_source_mode', text=tr('extract_source_mode'))

        if reverse_enabled and cfg.extract_source_mode == 'MOD_FOLDER':
            row = add_row_with_error_handler(layout, cfg, 'reverse_mod_folder')
            row.prop(cfg, 'reverse_mod_folder', text=tr('reverse_mod_folder'))

            row = add_row_with_error_handler(layout, cfg, 'extract_output_folder')
            row.prop(cfg, 'extract_output_folder', text=tr('extract_output_folder'))

            layout.row().operator(MCMI_ExtractMod.bl_idname, text=tr('extract_mod_object'))
            return

        row = add_row_with_error_handler(layout, cfg, 'frame_dump_folder')
        row.prop(cfg, 'frame_dump_folder', text=tr('frame_dump_folder'))

        layout.row().prop(cfg, 'extract_output_folder', text=tr('extract_output_folder'))

        layout.row().prop(cfg, 'assign_hash', text=tr('assign_hash'))

        layout.row()

        col = layout.column(align=True)
        grid = col.grid_flow(columns=2, align=True)
        grid.alignment = 'LEFT'
        grid.prop(cfg, 'skip_small_textures', text=tr('skip_small_textures'))
        if cfg.skip_small_textures:
            grid.prop(cfg, 'skip_small_textures_size', text=tr('skip_small_textures_size'))

        filter_col = layout.column(align=True)
        filter_col.prop(cfg, 'skip_jpg_textures', text=tr('skip_jpg_textures'))
        filter_col.prop(cfg, 'skip_known_cubemap_textures', text=tr('skip_known_cubemap_textures'))
        filter_col.prop(cfg, 'skip_same_slot_hash_textures', text=tr('skip_same_slot_hash_textures'))

        layout.row().operator(MCMI_ExtractFrameData.bl_idname, text=tr('extract_frame_data'))


class MCMI_TOOLS_PT_SidePanelPartialExport(bpy.types.Panel):
    bl_label = "Partial Export"
    bl_parent_id = "MCMI_TOOLS_PT_SIDEBAR"
    # bl_options = {'DEFAULT_CLOSED'}
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCMI Tools"
    bl_options = {'HIDE_HEADER'}
    bl_idname = 'mcmi_1'
    bl_order = 12

    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.tool_mode == 'EXPORT_MOD' and cfg.partial_export

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings
        box = layout.box()
        box.row().prop(cfg, 'export_index', text=tr('export_index'))
        box.row().prop(cfg, 'export_positions', text=tr('export_positions'))
        box.row().prop(cfg, 'export_blends', text=tr('export_blends'))
        box.row().prop(cfg, 'export_vectors', text=tr('export_vectors'))
        box.row().prop(cfg, 'export_colors', text=tr('export_colors'))
        box.row().prop(cfg, 'export_texcoords', text=tr('export_texcoords'))
        box.row().prop(cfg, 'export_shapekeys', text=tr('export_shapekeys'))


class MCMI_TOOLS_PT_SidePanelAdvancedExport(bpy.types.Panel):
    bl_label = " "
    bl_idname = "MCMI_TOOLS_PT_ADVANCED_EXPORT"
    bl_parent_id = "MCMI_TOOLS_PT_SIDEBAR"
    # bl_options = {'DEFAULT_CLOSED'}
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCMI Tools"
    bl_order = 10

    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.tool_mode == 'EXPORT_MOD'

    def draw_header(self, context):
        self.layout.label(text=tr('panel_advanced'))

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings

        if not cfg.partial_export:
            layout.row().prop(cfg, 'skip_known_cubemap_textures', text=tr('skip_known_cubemap_textures'))
            layout.row().prop(cfg, 'add_missing_vertex_groups', text=tr('add_missing_vertex_groups'))
            layout.row().prop(cfg, 'unrestricted_custom_shape_keys', text=tr('unrestricted_custom_shape_keys'))
            if is_effective_merged_skeleton(cfg.mod_skeleton_type):
                layout.row().prop(cfg, 'skeleton_scale', text=tr('skeleton_scale'))

        layout.row().prop(cfg, 'partial_export', text=tr('partial_export'))


class MCMI_TOOLS_PT_SidePanelModInfo(bpy.types.Panel):
    bl_label = " "
    bl_idname = "MCMI_TOOLS_PT_MOD_INFO"
    bl_parent_id = "MCMI_TOOLS_PT_SIDEBAR"
    # bl_options = {'DEFAULT_CLOSED'}
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCMI Tools"
    bl_order = 13

    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.tool_mode == 'EXPORT_MOD' and not cfg.partial_export

    def draw_header(self, context):
        self.layout.label(text=tr('panel_mod_info'))

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings
        layout.row().prop(cfg, 'mod_name', text=tr('mod_name'))
        layout.row().prop(cfg, 'mod_author', text=tr('mod_author'))
        layout.row().prop(cfg, 'mod_desc', text=tr('mod_desc'))
        layout.row().prop(cfg, 'mod_link', text=tr('mod_link'))
        layout.row().prop(cfg, 'mod_logo', text=tr('mod_logo'))


class MCMI_TOOLS_PT_SidePanelIniTemplate(bpy.types.Panel):
    bl_label = " "
    bl_idname = "MCMI_TOOLS_PT_INI_TEMPLATE"
    bl_parent_id = "MCMI_TOOLS_PT_SIDEBAR"
    bl_options = {'DEFAULT_CLOSED'}
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCMI Tools"
    bl_order = 80

    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.tool_mode == 'EXPORT_MOD' and not cfg.partial_export

    def draw_header(self, context):
        self.layout.label(text=tr('panel_ini_template'))

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings
    
        row = add_row_with_error_handler(layout, cfg, ['use_custom_template', 'custom_template_source'])

        split = row.split(factor=0.5)

        col_left = split.column()
        col_left.prop(cfg, 'use_custom_template', text=tr('use_custom_template'))

        col_left = split.column()
        col_left.prop(cfg, 'custom_template_source', text=tr('custom_template_source'))
        
        if cfg.custom_template_source == 'INTERNAL':
            layout.row().operator(MCMI_OpenIniTemplateEditor.bl_idname, text=tr('edit_template'))

        elif cfg.custom_template_source == 'EXTERNAL':
            row = add_row_with_error_handler(layout, cfg, 'custom_template_path')
            row.prop(cfg, 'custom_template_path', text=tr('custom_template_path'))

            row = layout.row()
            split = row.split(factor=0.5)

            col_left = split.column()
            col_left.operator(MCMI_OpenIniTemplateEditor.bl_idname, text=tr('edit_template'))
            
            col_right = split.column()
            if cfg.custom_template_live_update:
                col_right.operator(MCMI_IniTemplateEditor_ToggleLiveUpdates.bl_idname, text=tr('stop_ini_updates'))
            else:
                col_right.operator(MCMI_IniTemplateEditor_ToggleLiveUpdates.bl_idname, text=tr('start_ini_updates'))


class MCMI_TOOLS_PT_SidePanelExportFooter(bpy.types.Panel):
    bl_label = "Export"
    bl_parent_id = "MCMI_TOOLS_PT_SIDEBAR"
    # bl_options = {'DEFAULT_CLOSED'}
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCMI Tools"
    bl_options = {'HIDE_HEADER'}
    bl_order = 99

    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.tool_mode == 'EXPORT_MOD'
    
    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings
        if cfg.custom_template_live_update:
            layout.operator(MCMI_IniTemplateEditor_ToggleLiveUpdates.bl_idname, text=tr('stop_ini_updates'))
        else:
            layout.row().operator(MCMI_Export.bl_idname, text=tr('export_mod'))


class MCMI_OpenLodMapEditor(bpy.types.Operator):
    bl_idname = "mcmi_tools.open_lod_map_editor"
    bl_label = "Open LOD Map"
    bl_description = "Open generated LOD mapping table in Text Editor"

    def execute(self, context):
        cfg = context.scene.mcmi_tools_settings
        profile_key = get_selected_profile_key(cfg)
        existing_lod1, existing_lod2 = resolve_lod_maps_for_character(cfg)
        doc = read_lod_maps_document()
        doc['characters'][profile_key] = {
            'lod1': existing_lod1 if isinstance(existing_lod1, dict) else {},
            'lod2': existing_lod2 if isinstance(existing_lod2, dict) else {},
            'object_source_folder': str(resolve_path(cfg.object_source_folder)),
        }
        write_lod_maps_document(doc)
        text = get_profile_lod_map_text(profile_key)
        text.clear()
        text.write(json.dumps(doc['characters'][profile_key], indent=4, ensure_ascii=False))
        new_window = bpy.ops.wm.window_new()
        new_window_context = bpy.context.window_manager.windows[-1]
        text_area = None
        for area in new_window_context.screen.areas:
            area.type = 'TEXT_EDITOR'
            text_area = area
            for space in area.spaces:
                if space.type == 'TEXT_EDITOR':
                    space.text = text
        if text_area:
            for region in text_area.regions:
                if region.type == 'UI':
                    bpy.ops.wm.context_toggle(data_path="space_data.show_region_ui")
                    break
        return {'FINISHED'}


# @orientation_helper(axis_forward='-Z', axis_up='Y')
class MCMI_Import(bpy.types.Operator):
    """
    Import object extracted from frame dump data with WWMI
    """
    bl_idname = "mcmi_tools.import_object"
    bl_label = "Import Object"
    bl_description = "Import object extracted from frame dump data with WWMI"

    bl_options = {'UNDO'}

    def execute(self, context):
        try:
            cfg = context.scene.mcmi_tools_settings

            clear_error(cfg)

            cfg.mod_skeleton_type = cfg.import_skeleton_type
            import_func = get_reloaded_blender_import()

            reverse_tools = get_reverse_tools()
            if reverse_tools is not None and reverse_tools.is_mod_folder(cfg.object_source_folder):
                original_source = cfg.object_source_folder
                try:
                    temp_folder = reverse_tools.reverse_mod_extract_to_temp(cfg.object_source_folder)
                    cfg.object_source_folder = str(temp_folder)
                    import_func(self, context, cfg)
                finally:
                    cfg.object_source_folder = original_source
                return {'FINISHED'}

            if not cfg.enable_lod_mode:
                import_func(self, context, cfg)
            else:
                source_folders = [cfg.object_source_folder]
                if (cfg.lod1_source_folder or '').strip():
                    source_folders.append(cfg.lod1_source_folder)
                if (cfg.lod2_source_folder or '').strip():
                    source_folders.append(cfg.lod2_source_folder)

                imported_collections = []
                original_source = cfg.object_source_folder
                for source_folder in source_folders:
                    before = {collection.name for collection in bpy.data.collections}
                    cfg.object_source_folder = source_folder
                    import_func(self, context, cfg)
                    after = {collection.name for collection in bpy.data.collections}
                    created = sorted(list(after - before))
                    if len(created) == 0:
                        continue
                    imported_collections.append(bpy.data.collections[created[-1]])
                cfg.object_source_folder = original_source
                mapping_collections = imported_collections
                temp_mapping_collections = []
                try:
                    if cfg.import_skeleton_type == 'MERGED':
                        for i, collection in enumerate(imported_collections):
                            temp_collection = duplicate_collection_for_lod(context, collection, f'MapTmp_{i}')
                            temp_mapping_collections.append(temp_collection)
                        mapping_collections = temp_mapping_collections

                    if cfg.import_skeleton_type == 'MERGED':
                        merged_objects = [merge_collection_meshes_for_mapping(context, collection) for collection in mapping_collections]
                        base_vg_ids = get_all_vg_ids_from_source_folder(source_folders[0]) if len(source_folders) >= 1 else set()
                        if len(merged_objects) >= 2 and merged_objects[0] is not None and merged_objects[1] is not None:
                            lod1_target_vg_ids = get_all_vg_ids_from_source_folder(source_folders[1])
                            lod1_map = build_lod_vg_map_merged(
                                [merged_objects[0]],
                                [merged_objects[1]]
                            )
                            cfg.lod1_vg_map = json.dumps(lod1_map, ensure_ascii=False)
                        else:
                            lod1_map = {}
                            cfg.lod1_vg_map = '{}'

                        if len(merged_objects) >= 3 and merged_objects[0] is not None and merged_objects[2] is not None:
                            lod2_target_vg_ids = get_all_vg_ids_from_source_folder(source_folders[2])
                            lod2_map = build_lod_vg_map_merged(
                                [merged_objects[0]],
                                [merged_objects[2]]
                            )
                            cfg.lod2_vg_map = json.dumps(lod2_map, ensure_ascii=False)
                        else:
                            lod2_map = {}
                            cfg.lod2_vg_map = '{}'
                    else:
                        if len(imported_collections) >= 2:
                            lod1_map = build_lod_vg_map(
                                mapping_collections[0],
                                mapping_collections[1]
                            )
                            cfg.lod1_vg_map = json.dumps(lod1_map, ensure_ascii=False)
                        else:
                            lod1_map = {}
                            cfg.lod1_vg_map = '{}'
                        if len(imported_collections) >= 3:
                            lod2_map = build_lod_vg_map(
                                mapping_collections[0],
                                mapping_collections[2]
                            )
                            cfg.lod2_vg_map = json.dumps(lod2_map, ensure_ascii=False)
                        else:
                            lod2_map = {}
                            cfg.lod2_vg_map = '{}'
                    save_lod_maps_for_character(cfg, lod1_map, lod2_map)
                finally:
                    for collection in temp_mapping_collections:
                        cleanup_temp_collection(collection)

                self.report({'INFO'}, tr('lod_mapping_built'))

        except ConfigError as e:
            self.report({'ERROR'}, str(e))
        
        return {'FINISHED'}


class MCMI_Export(bpy.types.Operator):
    """
    Export object as WWMI mod
    """
    bl_idname = "mcmi_tools.export_mod"
    bl_label = "Export Mod"
    bl_description = "Export object as WWMI mod"

    def get_excluded_buffers(self, context):
        """
        Calculates list of exported buffers and processed semantics based on partial export settings
        Speeds up export of single buffer up to 5 times compared to full export
        """
        cfg = context.scene.mcmi_tools_settings

        if cfg.partial_export:
            # Loop data is used to create list of exported vertices, so there are only two options for partial export:
            # 1. Recalculate each time whenever Index / Vector / Color / TexCoord buffers is selected
            # 2. Load from cache if there is no Index / Vector / Color / TexCoord buffers selected
            exclude_buffers = []

            if not cfg.export_index:
                exclude_buffers.append('Index')
            if not cfg.export_positions:
                exclude_buffers.append('Position')
            if not cfg.export_blends:
                exclude_buffers.append('Blend')
            if not cfg.export_vectors:
                exclude_buffers.append('Vector')
            if not cfg.export_colors:
                exclude_buffers.append('Color')
            if not cfg.export_texcoords:
                exclude_buffers.append('TexCoord')
            if not cfg.export_shapekeys:
                exclude_buffers.append('ShapeKeyOffset')
                exclude_buffers.append('ShapeKeyVertexId')
                exclude_buffers.append('ShapeKeyVertexOffset')
                
            return exclude_buffers
    
        else:

            return []

    def _execute_export(self, context):
        original_visible_mod_output = None
        export_path_notice = None
        try:
            cfg = context.scene.mcmi_tools_settings

            clear_error(cfg)

            export_mod_output_folder = resolve_export_mod_output_folder(cfg.mod_output_folder)
            if export_mod_output_folder is not None and export_mod_output_folder != resolve_path(cfg.mod_output_folder):
                original_visible_mod_output = cfg.mod_output_folder
                cfg.mod_output_folder = str(export_mod_output_folder)
                export_path_notice = (
                    f'已自动切换 MOD 导出路径: '
                    f'{resolve_path(original_visible_mod_output).name} -> {export_mod_output_folder.name}'
                )

            excluded_buffers = self.get_excluded_buffers(context)

            if not cfg.enable_lod_mode:
                blender_export(self, context, cfg, excluded_buffers)
            else:
                lod1_map, lod2_map = resolve_lod_maps_for_character(cfg)
                original_component_collection = cfg.component_collection
                original_object_source = cfg.object_source_folder
                original_mod_output = cfg.mod_output_folder
                try:
                    blender_export(self, context, cfg, excluded_buffers)

                    lod0_hashes = parse_texture_hashes_from_folder(cfg.object_source_folder)

                    for lod_name, source_folder, lod_map in [
                        ('LOD1', cfg.lod1_source_folder, lod1_map),
                        ('LOD2', cfg.lod2_source_folder, lod2_map),
                    ]:
                        if not (source_folder or '').strip():
                            continue
                        if len(lod_map) == 0:
                            self.report({'WARNING'}, f'{lod_name}: {tr("lod_mapping_missing")}')
                            continue

                        temp_collection = duplicate_collection_for_lod(context, original_component_collection, lod_name)
                        try:
                            apply_lod_vg_map_to_collection(context, temp_collection, lod_map)
                            ensure_vg_layout_on_collection(
                                context,
                                temp_collection,
                                source_folder,
                                merged_mode=is_effective_merged_skeleton(cfg.mod_skeleton_type)
                            )

                            cfg.component_collection = temp_collection
                            cfg.object_source_folder = source_folder
                            cfg.mod_output_folder = str(resolve_path(original_mod_output) / lod_name)

                            blender_export(self, context, cfg, excluded_buffers)

                            if not cfg.remove_temp_object:
                                keep_lod_temp_export_object(
                                    context,
                                    temp_collection,
                                    lod_name,
                                    lod_map=lod_map,
                                    merged_mode=is_effective_merged_skeleton(cfg.mod_skeleton_type)
                                )

                            dedupe_lod_textures_and_ini(cfg.mod_output_folder, lod0_hashes)
                        finally:
                            cleanup_temp_collection(temp_collection)

                    lod_hashes = {}
                    for lod_name in ['LOD1', 'LOD2']:
                        lod_mod_folder = str(resolve_path(original_mod_output) / lod_name)
                        hash_value = parse_component0_hash_from_ini(lod_mod_folder)
                        if hash_value:
                            lod_hashes[lod_name] = hash_value

                    patch_lod0_ini_for_shared_textures(original_mod_output, lod_hashes)
                finally:
                    cfg.component_collection = original_component_collection
                    cfg.object_source_folder = original_object_source
                    cfg.mod_output_folder = original_mod_output

            if export_path_notice is not None:
                self.report({'INFO'}, export_path_notice)
            
        except ConfigError as e:
            self.report({'ERROR'}, str(e))
        finally:
            if original_visible_mod_output is not None:
                cfg = context.scene.mcmi_tools_settings
                cfg.mod_output_folder = original_visible_mod_output
            
        return {'FINISHED'}

    def execute(self, context):
        log_path = get_error_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        with open(log_path, 'w', encoding='utf-8', errors='replace') as log_file:
            tee_stdout = TeeStream(original_stdout, log_file)
            tee_stderr = TeeStream(original_stderr, log_file)
            sys.stdout = tee_stdout
            sys.stderr = tee_stderr
            print(f'MCMI export log started: {time.strftime("%Y-%m-%d %H:%M:%S")}')
            print(f'Error.log: {log_path}')
            try:
                return self._execute_export(context)
            except Exception:
                traceback.print_exc()
                raise
            finally:
                print(f'MCMI export log finished: {time.strftime("%Y-%m-%d %H:%M:%S")}')
                sys.stdout = original_stdout
                sys.stderr = original_stderr


class MCMI_ExtractFrameData(bpy.types.Operator):
    """
    Extract objects from frame dump
    """
    bl_idname = "mcmi_tools.extract_frame_data"
    bl_label = "Extract Objects From Dump"
    bl_description = "Extract objects from frame dump"

    def execute(self, context):
        try:
            cfg = context.scene.mcmi_tools_settings

            clear_error(cfg)

            output = get_reloaded_extract_frame_data()(cfg)

            written_object_folders = getattr(output, 'written_object_folders', {})
            if isinstance(written_object_folders, dict) and len(written_object_folders) > 0:
                if len(written_object_folders) == 1:
                    cfg.last_extract_object_folder = next(iter(written_object_folders.values()))
                else:
                    cfg.last_extract_object_folder = str(resolve_path(cfg.extract_output_folder).resolve())
            
            objects_missing_shapekeys = []
            for object_hash, object_data in output.objects.items():
                if object_data.shapekeys.offsets_hash and not object_data.shapekeys.shapekey_offsets:
                    objects_missing_shapekeys.append(object_hash)
            if len(objects_missing_shapekeys) > 0:
                self.report({'WARNING'}, dedent(f"""
                    Objects {', '.join(objects_missing_shapekeys)} were skipped:
                    Frame dump is missing shapekeys data!
                    Try to make another dump with ongoing facial animation.
                """).strip())
            
        except ConfigError as e:
            self.report({'ERROR'}, str(e))

        except Exception as e:
            if getattr(context.scene.mcmi_tools_settings, 'collect_extracted_resources', False):
                self.report({'WARNING'}, dedent(f"""
                    Extraction failed: {e}
                    Raw resources were saved to ExtractError/ExtractResources in the output folder.
                    Please share that folder for debugging.
                """).strip())
            else:
                raise
            
        return {'FINISHED'}


class MCMI_ExtractMod(bpy.types.Operator):
    """
    Extract object sources from an existing mod folder
    """
    bl_idname = "mcmi_tools.extract_mod"
    bl_label = "Extract Objects From Mod"
    bl_description = "Extract object sources from mod folder"

    def execute(self, context):
        try:
            cfg = context.scene.mcmi_tools_settings

            clear_error(cfg)

            reverse_tools = get_reverse_tools()
            if reverse_tools is None:
                raise ConfigError('reverse_mod_folder', 'Reverse tools are not available. Create a Reverse folder to enable this feature.')

            reverse_tools.reverse_mod_extract(cfg.reverse_mod_folder, cfg.extract_output_folder)

        except ConfigError as e:
            self.report({'ERROR'}, str(e))

        return {'FINISHED'}


class MCMI_OpenIniTemplateEditor(bpy.types.Operator):
    """
    Open current custom template in internal or external editor.
    """
    bl_idname = "mcmi_tools.open_ini_template_editor"
    bl_label = "Edit Template"
    bl_description = "Open current custom template file in internal or external editor."

    def execute(self, context):
        cfg = context.scene.mcmi_tools_settings

        if cfg.custom_template_source == 'EXTERNAL':
            template_path = resolve_path(cfg.custom_template_path)
            if not template_path.is_file():
                raise ValueError(f'Custom ini template file not found: `{template_path}`!')
            subprocess.Popen([f'{str(template_path)}'], shell=True)
            return {'FINISHED'}

        text_name = "CustomIniTemplate"

        if text_name in bpy.data.texts:
            text = bpy.data.texts[text_name]
        else:
            text = bpy.data.texts.new(text_name)
        
        if not text.as_string().strip():
            text.clear()
            text.write(IniMaker.get_default_template(context, cfg, remove_code_comments=True))
            text.cursor_set(0)

        new_window = bpy.ops.wm.window_new()
        
        new_window_context = bpy.context.window_manager.windows[-1]

        # Switch the area to TEXT_EDITOR and assign the text
        for area in new_window_context.screen.areas:
            area.type = 'TEXT_EDITOR'
            text_area = area
            for space in area.spaces:
                if space.type == 'TEXT_EDITOR':
                    space.text = text

        # Toggle Tools sidebar
        if text_area:
            for region in text_area.regions:
                if region.type == 'UI':
                    bpy.ops.wm.context_toggle(data_path="space_data.show_region_ui")
                    break
        
        return {'FINISHED'}


class MCMI_IniTemplateEditor_ToggleLiveUpdates(bpy.types.Operator):
    bl_idname = "mcmi_tools.ini_template_start_live_updates"
    bl_label = "Start Ini Updates"
    bl_description = "Once started, MCMI Tools will run export with current settings and start writing mod.ini on each template edit.\n"
    "Warning! Mod export will be blocked until live updates are stopped!"

    def execute(self, context):
        cfg = context.scene.mcmi_tools_settings
        if cfg.custom_template_live_update:
            cfg.custom_template_live_update = False
        else:
            cfg.custom_template_live_update = True
            bpy.ops.mcmi_tools.export_mod()
        return {'FINISHED'}
    
    @classmethod
    def poll(cls, context):
        cfg = context.scene.mcmi_tools_settings
        return cfg.use_custom_template


class MCMI_IniTemplateEditor_Reset(bpy.types.Operator):
    bl_idname = "mcmi_tools.ini_template_editor_reset"
    bl_label = "Reset Template"
    bl_description = "Warning! This action will reset custom template to default!"

    def execute(self, context):
        cfg = context.scene.mcmi_tools_settings
        
        text_name = "CustomIniTemplate"

        if text_name in bpy.data.texts:
            text = bpy.data.texts[text_name]
        else:
            text = bpy.data.texts.new(text_name)
        
        text.clear()
        text.write(IniMaker.get_default_template(context, cfg, remove_code_comments=True))
        text.cursor_set(0)

        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    

class MCMI_TOOLS_PT_TEXT_EDITOR_IniTemplate(bpy.types.Panel):
    bl_label = "Ini Template - MCMI Tools"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Text"

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings
        
        if cfg.custom_template_live_update:
            layout.operator(MCMI_IniTemplateEditor_ToggleLiveUpdates.bl_idname, text=tr('stop_ini_updates'))
        else:
            layout.operator(MCMI_IniTemplateEditor_ToggleLiveUpdates.bl_idname, text=tr('start_ini_updates'))

        layout.operator(MCMI_IniTemplateEditor_Reset.bl_idname)


class UpdaterPanel(bpy.types.Panel):
    """Update Panel"""
    bl_label = " "
    bl_idname = "MCMI_TOOLS_PT_UpdaterPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    # bl_context = "objectmode"
    bl_category = "MCMI Tools"
    bl_order = 99
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text=tr('panel_update_settings'))

    def draw(self, context):
        layout = self.layout

        # Call to check for update in background.
        # Note: built-in checks ensure it runs at most once, and will run in
        # the background thread, not blocking or hanging blender.
        # Internally also checks to see if auto-check enabled and if the time
        # interval has passed.
        addon_updater_ops.check_for_update_background()
        col = layout.column()
        col.scale_y = 0.7
        # Could also use your own custom drawing based on shared variables.
        if addon_updater_ops.updater.update_ready:
            layout.label(text=tr('update_available'), icon="INFO")

        # Call built-in function with draw code/checks.
        addon_updater_ops.update_notice_box_ui(self, context)
        addon_updater_ops.update_settings_ui(self, context)


class DebugPanel(bpy.types.Panel):
    """Debug Panel"""
    bl_label = " "
    bl_idname = "MCMI_TOOLS_PT_DebugPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    # bl_context = "objectmode"
    bl_category = "MCMI Tools"
    bl_order = 80
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text=tr('panel_debug_settings'))

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.mcmi_tools_settings

        layout.row().prop(cfg, 'allow_missing_shapekeys', text=tr('allow_missing_shapekeys'))
        layout.row().prop(cfg, 'remove_temp_object', text=tr('remove_temp_object'))
        layout.row().prop(cfg, 'export_on_reload', text=tr('export_on_reload'))
        layout.row().prop(cfg, 'collect_extracted_resources', text=tr('collect_extracted_resources'))
