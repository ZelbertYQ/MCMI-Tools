import time
import shutil
import json
import re
import hashlib
import os
import subprocess
import bpy

from typing import List, Dict, Union
from dataclasses import dataclass, field
from pathlib import Path

from ..addon.exceptions import ConfigError

from ..migoto_io.blender_interface.utility import *
from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.objects import *
from ..migoto_io.blender_interface.mesh import *
from ..migoto_io.data_model.byte_buffer import NumpyBuffer
from ..migoto_io.data_model.data_model import DataModel
from ..migoto_io.data_model.dxgi_format import DXGIFormatIndex

from ..extract_frame_data.metadata_format import read_metadata, ExtractedObject

from .object_merger import ObjectMerger, SkeletonType, VgRemapMode, MergedObject, MergedObjectShapeKeysBatch
from .metadata_collector import Version, ModInfo
from .texture_collector import Texture, get_textures
from .ini_maker import IniMaker

from .data_models.data_model_wwmi import DataModelWWMI

class Fatal(Exception): pass


data_models: Dict[str, DataModel] = {
    'WWMI': DataModelWWMI(),
}


def _slot_filter_index(match_format_enum):
    prefix = match_format_enum.name.split('_')[0]
    ascii_digits = ''.join(str(ord(c)) for c in prefix)
    return float(f"83.{ascii_digits}")


def _slot_var_name(slot_name):
    return f"$mcmi_slot_{slot_name.replace('-', '_')}"


def _slot_id(slot_name):
    match = re.search(r't(\d+)$', slot_name)
    return int(match.group(1)) if match else -1


def _shader_filter_index(shader_hash, shader_type):
    if not shader_hash:
        return 0.0
    prefix = '85' if shader_type == 'vs' else '86'
    value = int(shader_hash[:6], 16) % 900000 + 100000
    return float(f"{prefix}.{value:06d}")


def _shader_condition_pair(vs_value, ps_value):
    return f"(vs == {_shader_filter_index(vs_value, 'vs')} && ps == {_shader_filter_index(ps_value, 'ps')})"


def _build_shader_condition(shader_pairs):
    seen = set()
    parts = []
    for vs_value, ps_value in shader_pairs:
        key = (vs_value, ps_value)
        if key in seen:
            continue
        seen.add(key)
        parts.append(_shader_condition_pair(vs_value, ps_value))
    if not parts:
        return ''
    if len(parts) == 1:
        return parts[0]
    return '(' + ' || '.join(parts) + ')'


def _sanitize_resource_token(value, fallback='texture'):
    value = str(value or '').strip()
    value = re.sub(r'[^a-zA-Z0-9_\-]', '_', value)
    value = value.strip('_-')
    return value or fallback


def _merge_shader_pair_entry(entry, vs_value, ps_value):
    pair = (vs_value, ps_value)
    shader_pairs = entry.setdefault('shader_pairs', [])
    if pair not in shader_pairs:
        shader_pairs.append(pair)
    entry['shader_condition'] = _build_shader_condition(shader_pairs)
    return entry


def _is_path_inside(path_text, folder: Path):
    if not path_text:
        return False
    try:
        path = Path(path_text).resolve()
        folder = Path(folder).resolve()
        path.relative_to(folder)
        return True
    except Exception:
        return False


def _slot_texture_base_name(component_id, slot_name, source_hash, image_name, source_path):
    slot_token = _sanitize_resource_token(slot_name.replace('-', '_'), 'slot')
    source_hash = (source_hash or '').lower()
    if source_hash:
        return f'C{component_id}-{source_hash}', f'C{component_id}_{slot_token}_{source_hash}'

    dot_index = image_name.find('.')
    base_name = image_name[:dot_index] if dot_index > 0 else image_name
    if any(ord(c) > 127 for c in base_name):
        base_name = hashlib.sha256(base_name.encode('utf-8')).hexdigest()[:16]
    else:
        base_name = _sanitize_resource_token(base_name)

    if source_path:
        path_hash = hashlib.sha256(source_path.encode('utf-8')).hexdigest()[:8]
        base_name = f'{base_name}_{path_hash}'

    return f'C{component_id}-{base_name}', f'C{component_id}_{slot_token}_{base_name}'


def _ensure_unique_slot_texture_name(base_filename, base_resource, used_textures, instance_key):
    existing_key = used_textures.get(base_filename)
    if existing_key is None or existing_key == instance_key:
        used_textures[base_filename] = instance_key
        return base_filename, base_resource

    suffix = hashlib.sha256('|'.join(str(part) for part in instance_key).encode('utf-8')).hexdigest()[:8]
    stem = Path(base_filename).stem
    filename = f'{stem}-{suffix}.dds'
    resource = f'{base_resource}_{suffix}'
    counter = 2
    while filename in used_textures and used_textures[filename] != instance_key:
        filename = f'{stem}-{suffix}-{counter}.dds'
        resource = f'{base_resource}_{suffix}_{counter}'
        counter += 1

    used_textures[filename] = instance_key
    return filename, resource


def _collect_slot_shader_markers(merged_object):
    markers = []
    seen = set()

    for component in merged_object.components:
        for source in (getattr(component, 'material', None),):
            if not source:
                continue
            for node_group in source.get('node_groups', []):
                for shader_type, shader_hash in (('vs', node_group.get('vs')), ('ps', node_group.get('ps'))):
                    if not shader_hash:
                        continue
                    key = (shader_type, shader_hash)
                    if key in seen:
                        continue
                    seen.add(key)
                    markers.append({
                        'shader_type': shader_type,
                        'shader_hash': shader_hash,
                        'filter_index': node_group.get(f'{shader_type}_filter_index', 0.0),
                    })
        for temp_object in component.objects:
            source = getattr(temp_object, 'material', None)
            if not source:
                continue
            for node_group in source.get('node_groups', []):
                for shader_type, shader_hash in (('vs', node_group.get('vs')), ('ps', node_group.get('ps'))):
                    if not shader_hash:
                        continue
                    key = (shader_type, shader_hash)
                    if key in seen:
                        continue
                    seen.add(key)
                    markers.append({
                        'shader_type': shader_type,
                        'shader_hash': shader_hash,
                        'filter_index': node_group.get(f'{shader_type}_filter_index', 0.0),
                    })

    return markers


class ObjectMergerSlot(ObjectMerger):
    def __init__(self, **kwargs):
        self._texture_mode = kwargs.pop('texture_mode', 'HASH')
        self._shader_texture_usage = kwargs.pop('shader_texture_usage', None)
        self._object_source_folder = kwargs.pop('object_source_folder', None)
        self._slot_textures = []
        super().__init__(**kwargs)

    def pre_join_objects(self):
        if self._texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            self._collect_slot_material_info()

    def _collect_slot_material_info(self):
        if self._shader_texture_usage is None:
            return

        shader_texture_usage = self._shader_texture_usage
        is_simple = self._texture_mode == 'SLOT_SIMPLE'
        material_pattern = re.compile(r'.*component[_ -]*(\d+).*', re.IGNORECASE)
        node_group_pattern = re.compile(r'vs=([0-9a-f]+)-ps=([0-9a-f]+)', re.IGNORECASE)
        all_images = {}
        used_texture_names = {}
        slot_hash_filter_indices = {}

        def get_slot_hash_filter_index(texture_hash):
            if not texture_hash:
                return 0.0
            if texture_hash not in slot_hash_filter_indices:
                # 3DMigoto filter_index comparisons are reliable with float-like
                # markers (same style as 83.BC* and 3381.7777). Large integers
                # can fail to behave like resource markers on ps-t slots.
                slot_hash_filter_indices[texture_hash] = float(f"84.{100000 + len(slot_hash_filter_indices):06d}")
            return slot_hash_filter_indices[texture_hash]

        for component_id, component in enumerate(self.components):
            component_match_formats = {}
            component_slot_hashes = {}
            component_material_collected = False

            for temp_object in component.objects:
                obj = temp_object.object
                material_info = {
                    'material_name': None,
                    'material_index': None,
                    'node_groups': [],
                }

                matched_material = None
                if obj.data.materials:
                    for mat in obj.data.materials:
                        if mat is None:
                            continue
                        match = material_pattern.match(mat.name)
                        if not match:
                            continue
                        matched_material = mat
                        material_info['material_name'] = mat.name
                        material_info['material_index'] = match.group(1)

                        obj_name = obj.name[5:] if obj.name.startswith('TEMP_') else obj.name
                        obj_match = material_pattern.match(obj_name)
                        if obj_match and obj_match.group(1) != match.group(1):
                            raise ConfigError(
                                'component_collection',
                                f"Material Component index ({match.group(1)}) doesn't match object Component index ({obj_match.group(1)})!\n"
                                f"Object: '{obj_name}', Material: '{mat.name}'"
                            )
                        break

                if is_simple and component_material_collected:
                    continue

                if matched_material is not None and matched_material.use_nodes:
                    node_group_materials = [matched_material]
                elif obj.data.materials:
                    node_group_materials = [mat for mat in obj.data.materials if mat is not None and mat.use_nodes]
                else:
                    node_group_materials = []

                for mat in node_group_materials:
                    for node in mat.node_tree.nodes:
                        if node.type != 'GROUP' or node.node_tree is None or node.mute:
                            continue
                        ng_match = node_group_pattern.match(node.node_tree.name)
                        if not ng_match:
                            continue

                        vs_value = ng_match.group(1)
                        ps_value = ng_match.group(2)
                        node_group_info = {
                            'name': node.node_tree.name,
                            'vs': vs_value,
                            'ps': ps_value,
                            'vs_filter_index': _shader_filter_index(vs_value, 'vs'),
                            'ps_filter_index': _shader_filter_index(ps_value, 'ps'),
                            'shader_condition': _build_shader_condition([(vs_value, ps_value)]),
                            'inputs': [],
                            'shader_pairs': [(vs_value, ps_value)],
                        }

                        for input_socket in node.inputs:
                            input_name = input_socket.name
                            if not input_name.startswith('ps-t') or 'alpha' in input_name.lower():
                                continue
                            if not input_socket.is_linked:
                                continue
                            link = self._get_active_texture_link(input_socket)
                            if link is None:
                                continue
                            from_node = link.from_node
                            image = from_node.image

                            component_key = f"Component {material_info['material_index']}" if material_info['material_index'] is not None else f"Component {component_id}"
                            vs_key = f"vs={vs_value}"
                            ps_key = f"ps={ps_value}"
                            format_enum = None
                            match_format_enum = None
                            if material_info['material_index'] is not None:
                                slot_data = (
                                    shader_texture_usage
                                    .get(component_key, {})
                                    .get(vs_key, {})
                                    .get(ps_key, {})
                                )
                                texture_info = slot_data.get(input_name, {})
                                format_str = texture_info.get('format', '')
                                source_hash = (texture_info.get('hash', '') or '').lower()
                                original_filename = texture_info.get('filename', '')
                                if format_str:
                                    try:
                                        format_enum = DXGIFormatIndex[format_str]
                                        match_format_enum = format_enum.to_typeless()
                                    except KeyError:
                                        print(f"Warning: Unknown format '{format_str}' for {component_key}/{vs_key}/{ps_key}/{input_name}")
                            else:
                                source_hash = ''
                                original_filename = ''

                            format_filter_index = _slot_filter_index(match_format_enum) if match_format_enum is not None else 0.0
                            source_filter_index = get_slot_hash_filter_index(source_hash) if source_hash else format_filter_index
                            image_path = bpy.path.abspath(image.filepath or image.filepath_raw)
                            source_path = os.path.normpath(image_path) if image_path else ''
                            if self._is_original_slot_texture(source_path, original_filename, source_hash):
                                print(f"Slot texture unchanged, skipped: {component_key}/{vs_key}/{ps_key}/{input_name} -> {image_path}")
                                continue
                            instance_key = (
                                component_id,
                                vs_value,
                                ps_value,
                                input_name,
                                source_hash,
                                source_path.lower(),
                                image.name,
                            )
                            base_filename, base_resource = _slot_texture_base_name(
                                component_id,
                                input_name,
                                source_hash,
                                image.name,
                                source_path,
                            )
                            dds_export_name, resource_name = _ensure_unique_slot_texture_name(
                                f'{base_filename}.dds',
                                base_resource,
                                used_texture_names,
                                instance_key,
                            )
                            print(f"Slot texture selected: {component_key}/{vs_key}/{ps_key}/{input_name} -> {image_path}")
                            input_info = {
                                'slot': input_name,
                                'slot_id': _slot_id(input_name),
                                'slot_var': _slot_var_name(input_name),
                                'format': format_enum,
                                'match_format': match_format_enum,
                                'filter_index': source_filter_index,
                                'format_filter_index': format_filter_index,
                                'source_hash': source_hash,
                                'source_filter_index': source_filter_index,
                                'image': image,
                                'source_path': source_path,
                                'dds_export_name': dds_export_name,
                                'resource_name': resource_name,
                                'instance_key': '|'.join(str(part) for part in instance_key),
                                'shader_pairs': [(vs_value, ps_value)],
                            }
                            node_group_info['inputs'].append(input_info)

                            if source_hash and match_format_enum is not None:
                                slot_hash_key = (input_name, source_hash)
                                if slot_hash_key in component_slot_hashes:
                                    _merge_shader_pair_entry(component_slot_hashes[slot_hash_key], vs_value, ps_value)
                                else:
                                    component_slot_hashes[slot_hash_key] = {
                                        'slot': input_name,
                                        'slot_id': _slot_id(input_name),
                                        'slot_var': _slot_var_name(input_name),
                                        'hash': source_hash,
                                        'match_format': match_format_enum,
                                        'filter_index': source_filter_index,
                                        'shader_pairs': [(vs_value, ps_value)],
                                        'shader_condition': _build_shader_condition([(vs_value, ps_value)]),
                                    }

                            if not source_hash and match_format_enum is not None and match_format_enum.value not in component_match_formats:
                                if is_simple:
                                    component_match_formats[match_format_enum.value] = {
                                        'match_format': match_format_enum,
                                        'filter_index': format_filter_index,
                                    }
                                else:
                                    component_match_formats[match_format_enum.value] = {
                                        'match_format': match_format_enum,
                                        'match_formats': match_format_enum.get_same_prefix_formats(),
                                        'filter_index': format_filter_index,
                                    }

                            if dds_export_name not in all_images:
                                all_images[dds_export_name] = {
                                    'image': image,
                                    'source_path': source_path,
                                    'dds_export_name': dds_export_name,
                                    'resource_name': resource_name,
                                    'source_hash': source_hash,
                                    'slot': input_name,
                                    'component_id': component_id,
                                    'instance_key': input_info['instance_key'],
                                    'shader_pairs': [(vs_value, ps_value)],
                                    'shader_condition': _build_shader_condition([(vs_value, ps_value)]),
                                }
                            else:
                                _merge_shader_pair_entry(all_images[dds_export_name], vs_value, ps_value)

                        if node_group_info['inputs']:
                            material_info['node_groups'].append(node_group_info)

                if is_simple:
                    if not component_material_collected and material_info['node_groups']:
                        component.material = material_info
                        component_material_collected = True
                else:
                    temp_object.material = material_info

            component.match_formats = list(component_match_formats.values())
            component.slot_hashes = list(component_slot_hashes.values())

        self._slot_textures = list(all_images.values())
        print(f"Slot mode ({'simple' if is_simple else 'complex'}): collected {len(self._slot_textures)} unique textures")

    @staticmethod
    def _get_active_texture_link(input_socket):
        # Blender input sockets can retain more than one link after repeated
        # reconnects in some node editor workflows. Prefer the most recent
        # usable link instead of blindly reading links[0].
        for link in reversed(list(input_socket.links)):
            if link.is_muted:
                continue
            from_node = link.from_node
            if from_node.type != 'TEX_IMAGE' or from_node.mute:
                continue
            if from_node.image is None:
                continue
            return link
        return None

    def _is_original_slot_texture(self, source_path, original_filename, source_hash):
        if not source_path:
            return False
        if not self._object_source_folder or not _is_path_inside(source_path, self._object_source_folder):
            return False
        source_name = Path(source_path).name.lower()
        original_name = Path(original_filename).name.lower() if original_filename else ''
        if original_name and source_name == original_name:
            return True
        if source_hash and source_name.startswith(source_hash.lower()):
            return True
        return False


# TODO: Add support of export of unhandled semantics from vertex attributes
class ModExporter:
    extracted_object: ExtractedObject
    merged_object: MergedObject
    buffers: Dict[str, NumpyBuffer]
    textures: List[Texture] = {}
    slot_textures: List[Dict] = None
    ini: IniMaker

    def __init__(self, context, cfg, excluded_buffers: List[str]):
        self.context = context
        self.cfg = cfg
        self.excluded_buffers = excluded_buffers

        self.object_source_folder = resolve_path(cfg.object_source_folder)
        self.mod_output_folder = resolve_path(cfg.mod_output_folder)
        self.meshes_path = self.mod_output_folder / 'Meshes'
        self.meshes_path.mkdir(parents=True, exist_ok=True)
        self.textures_path = self.mod_output_folder / 'Textures'
        self.textures_path.mkdir(parents=True, exist_ok=True)
        self.local_mod_logo_path = self.textures_path / 'Logo.dds'

    def export_mod(self):
    
        self.verify_config()

        start_time = time.time()
        print(f"Mod export started for '{self.cfg.component_collection.name}' object")

        if self.cfg.custom_template_live_update:
            self.cfg.partial_export = False
            self.cfg.write_ini = True

        try:
            self.extracted_object = read_metadata(self.object_source_folder / 'Metadata.json')
        except FileNotFoundError:
            raise ConfigError('object_source_folder', 'Specified folder is missing Metadata.json!')
        except Exception as e:
            raise ConfigError('object_source_folder', f'Failed to load Metadata.json:\n{e}')

        user_context = get_user_context(self.context)

        try:
            self.build_merged_object()
        except ConfigError as e:
            raise e
        except Exception as e:
            raise ConfigError('component_collection', f'Failed to create merged object from collection:\n{e}')

        try:
            self.build_data_buffers()
        except Exception as e:
            raise e
        finally:
            if self.cfg.remove_temp_object:
                remove_mesh(self.merged_object.object.data)
            set_user_context(self.context, user_context)

        if not self.cfg.partial_export:
            self.textures = get_textures(self.object_source_folder, ['af26db30', '1320a071', '10d7937d', '87505b2b'] if self.cfg.skip_known_cubemap_textures else [])

            if self.cfg.write_ini:
                try:
                    self.build_mod_ini()
                except FileNotFoundError:
                    raise ConfigError('custom_template_source', f'Specified custom template file not found!')
                except Exception as e:
                    raise ConfigError('use_custom_template', f'Failed to build mod.ini from ini template:\n{e}')

        if self.cfg.custom_template_live_update:
            print(f'Total live ini template initialization time: {time.time() - start_time :.3f}s')
            return

        try:
            self.write_files()
        except Exception as e:
            raise ConfigError('mod_output_folder', f'Failed to write files to mod folder:\n{e}')

        print(f'Total mod export time: {time.time() - start_time :.3f}s')

    def verify_config(self):
        if self.cfg.component_collection is None:
            raise ConfigError('component_collection', f'Components collection is not specified!')
        if self.cfg.component_collection not in list(get_scene_collections()):
            raise ConfigError('component_collection', f'Collection "{self.cfg.component_collection.name}" is not a member of "Scene Collection"!')

    def resolve_export_skeleton(self):
        mode = self.cfg.mod_skeleton_type
        if mode == 'MERGED':
            return SkeletonType.Merged, None
        if mode == 'COMPONENT':
            return SkeletonType.PerComponent, None
        if mode == 'MERGED_TO_COMPONENT':
            return SkeletonType.PerComponent, VgRemapMode.MergedToComponent
        if mode == 'COMPONENT_TO_MERGED':
            return SkeletonType.Merged, VgRemapMode.ComponentToMerged
        raise ValueError(f'Unknown skeleton type {mode}!')

    def build_merged_object(self):
        start_time = time.time()
        skeleton_type, vg_remap_mode = self.resolve_export_skeleton()
        shader_texture_usage = None
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            shader_usage_path = self.object_source_folder / 'ShaderTextureUsage.json'
            if not shader_usage_path.is_file():
                raise ConfigError('object_source_folder', 'ShaderTextureUsage.json not found in object source folder. This file is required for slot texture modes.')
            with open(shader_usage_path, 'r', encoding='utf-8') as f:
                shader_texture_usage = json.load(f)

        object_merger = ObjectMergerSlot(
            extracted_object=self.extracted_object,
            ignore_nested_collections=self.cfg.ignore_nested_collections,
            ignore_hidden_collections=self.cfg.ignore_hidden_collections,
            ignore_hidden_objects=self.cfg.ignore_hidden_objects,
            ignore_muted_shape_keys=self.cfg.ignore_muted_shape_keys,
            apply_modifiers=self.cfg.apply_all_modifiers,
            context=self.context,
            collection=self.cfg.component_collection,
            skeleton_type=skeleton_type,
            vg_remap_mode=vg_remap_mode,
            mesh_scale=0.01,
            mesh_rotation=(0, 0, 180),
            add_missing_vertex_groups=self.cfg.add_missing_vertex_groups,
            texture_mode=self.cfg.texture_mode,
            shader_texture_usage=shader_texture_usage,
            object_source_folder=self.object_source_folder,
        )
        self.merged_object = object_merger.merged_object
        if not self.cfg.partial_export and self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX'):
            self.slot_textures = getattr(object_merger, '_slot_textures', [])
        print(f'Merged object build time: {time.time() - start_time :.3f}s ({self.merged_object.vertex_count} vertices, {self.merged_object.index_count} indices)')

    def build_data_buffers(self):
        start_time = time.time()

        global data_models
        data_model = data_models['WWMI']

        buffers_format = None
        if self.extracted_object.export_format is not None and len(self.extracted_object.export_format) > 0:
            buffers_format = {}
            for buffer_name, buffer_layout in self.extracted_object.export_format.items():
                buffers_format[buffer_name] = buffer_layout.get_layout()

        index_layout = None
        if len(self.merged_object.object.vertex_groups) > 256:
            index_layout = []
            for component in self.merged_object.components:
                index_layout.append(component.index_count)
                
        self.buffers, vertex_count = data_model.get_data(
            self.context, 
            self.cfg.component_collection, 
            self.merged_object.object, 
            self.merged_object.mesh, 
            self.excluded_buffers,
            buffers_format,
            self.cfg.mirror_mesh,
            index_layout)

        self.merged_object.vertex_count = vertex_count

        # Build shapekeys batches metadata (171 pipeline)
        shapekey_offsets = self.buffers.get('ShapeKeyOffset', None)
        if shapekey_offsets:
            batches_count = int(len(shapekey_offsets.data) / 128)

            for batch_id in range(batches_count):
                batch_vertex_count = shapekey_offsets.data[((batch_id + 1) * 128) - 1]
                self.merged_object.shapekeys.batches.append(MergedObjectShapeKeysBatch(
                    vertex_count=batch_vertex_count,
                    vertex_offset=self.merged_object.shapekeys.vertex_count,
                ))
                self.merged_object.shapekeys.vertex_count += batch_vertex_count

            self.merged_object.shapekeys.vertex_count_batch0 = (
                self.merged_object.shapekeys.batches[0].vertex_count
                if len(self.merged_object.shapekeys.batches) > 0 else 0
            )
            self.merged_object.shapekeys.vertex_count_batch1 = (
                self.merged_object.shapekeys.batches[1].vertex_count
                if len(self.merged_object.shapekeys.batches) > 1 else 0
            )
            self.merged_object.shapekeys.shapekey_count = max(0, len(shapekey_offsets.data) - batches_count)

            # Ensure offsets sanity
            shapekey_vertex_ids = self.buffers.get('ShapeKeyVertexId', None)
            if shapekey_vertex_ids is not None and self.merged_object.shapekeys.vertex_count != len(shapekey_vertex_ids):
                raise ValueError(
                    f'Total vertex count in ShapeKeyOffset across {batches_count} bathces '
                    f'does not match ShapeKeyVertexId size of {len(shapekey_vertex_ids)}!'
                )

        remapped_vgs_counts = self.buffers.pop('BlendRemapLayout', None)
        if remapped_vgs_counts is not None:
            remap_id = 0
            for component_id, vg_count in enumerate(remapped_vgs_counts.data.tolist()):
                if vg_count == 0:
                    continue
                component = self.merged_object.components[component_id]
                if vg_count > 256:            
                    raise ConfigError('component_collection', f'Component{component_id} 256 VG limit exceeded!\n'
                                      f'Currently it consists of {len(component.objects)} object(s) using total of {vg_count} VGs with non-zero weights.\n'
                                      f'Please reduce the number of non-empty VGs or split objects between different components.')
                component.blend_remap_id = remap_id
                component.blend_remap_vg_count = vg_count
                remap_id += 1
            self.merged_object.blend_remap_count = remap_id

        print(f'Total mesh data collection time: {time.time() - start_time :.3f}s')
    
    def build_mod_ini(self):
        start_time = time.time()
        slot_shader_markers = _collect_slot_shader_markers(self.merged_object) if self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX') else None

        ini_maker = IniMaker(
            cfg=self.cfg,
            mod_info=ModInfo(
                mcmi_tools_version=Version(self.cfg.mcmi_tools_version),
                required_wwmi_version=Version(self.cfg.required_wwmi_version),
                mod_name=self.cfg.mod_name,
                mod_author=self.cfg.mod_author,
                mod_desc=self.cfg.mod_desc,
                mod_link=self.cfg.mod_link,
                mod_logo=self.local_mod_logo_path,
            ),
            extracted_object=self.extracted_object,
            merged_object=self.merged_object,
            buffers=self.buffers,
            textures=self.textures,
            comment_code=self.cfg.comment_ini,
            skeleton_scale=self.cfg.skeleton_scale,
            unrestricted_custom_shape_keys=self.cfg.unrestricted_custom_shape_keys,
            slot_textures=self.slot_textures if self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX') else None,
            slot_shader_markers=slot_shader_markers,
        )

        self.ini = ini_maker

        if self.cfg.custom_template_live_update:
            self.ini.start_live_write(self.context, self.cfg)
        else:
            self.ini.build_from_template(self.context, self.cfg, with_checksum=True)

        print(f'Total mod ini build time: {time.time() - start_time :.3f}s')

    def write_files(self):
        start_time = time.time()

        for buffer_name, buffer in self.buffers.items():
            print(f'Writing {buffer_name}.buf...')
            with open(self.meshes_path / f'{buffer_name}.buf', 'wb') as f:
                f.write(buffer.get_bytes())

        if not self.cfg.partial_export:
            # Write textures
            if self.cfg.copy_textures and self.cfg.texture_mode == 'HASH':
                for texture in self.textures:
                    texture_path = self.textures_path / texture.filename
                    if texture_path.is_file():
                        continue
                    texture_path.parent.mkdir(parents=True, exist_ok=True)
                    print(f'Copying {texture.filename}...')
                    shutil.copy(texture.path, texture_path)
            if self.cfg.texture_mode in ('SLOT_SIMPLE', 'SLOT_COMPLEX') and self.slot_textures:
                self.write_slot_textures()
            # Write mod logo
            mod_logo_path = resolve_path(self.cfg.mod_logo)
            if mod_logo_path.is_file():
                print(f'Copying {self.local_mod_logo_path.name}...')
                shutil.copy(mod_logo_path, self.local_mod_logo_path)
            # Write mod.ini
            if self.cfg.write_ini:
                self.ini.write(ini_path=self.mod_output_folder / 'mod.ini')
                # self.ini.write(ini_path=self.mod_output_folder / 'mod_old.ini', ini_string=self.ini.build_old())
                
        print(f'Disk write time: {time.time() - start_time :.3f}s')

    def _find_texture_format(self, dds_export_name):
        for component in self.merged_object.components:
            if component.material:
                for ng in component.material.get('node_groups', []):
                    for inp in ng.get('inputs', []):
                        if inp.get('dds_export_name') == dds_export_name and inp.get('format') is not None:
                            return inp['format'].name
            for temp_object in component.objects:
                if temp_object.material is None:
                    continue
                for ng in temp_object.material.get('node_groups', []):
                    for inp in ng.get('inputs', []):
                        if inp.get('dds_export_name') == dds_export_name and inp.get('format') is not None:
                            return inp['format'].name
        return 'BC7_UNORM'

    def write_slot_textures(self):
        if not self.cfg.export_textures:
            return

        texconv_path = Path(os.path.realpath(__file__)).parent.parent / 'DirectXTex' / 'texconv.exe'
        if not texconv_path.is_file():
            raise ConfigError('mod_output_folder', f'texconv.exe not found at {texconv_path}!')

        for slot_texture in self.slot_textures:
            image = slot_texture['image']
            dds_export_name = slot_texture['dds_export_name']
            dest_path = self.textures_path / dds_export_name

            src_path = self._resolve_slot_texture_source(slot_texture)
            if src_path is not None and src_path.is_file():
                if src_path.suffix.lower() == '.dds':
                    print(f'Copying selected DDS {src_path.name} -> {dds_export_name}...')
                    try:
                        if src_path.resolve() != dest_path.resolve():
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy(src_path, dest_path)
                    except Exception as e:
                        print(f"Warning: Failed to copy DDS '{src_path}' to '{dest_path}': {e}")
                    continue
                try:
                    src_path.relative_to(self.object_source_folder)
                    if src_path.suffix.lower() == '.dds':
                        print(f'Copying source DDS {dds_export_name}...')
                        shutil.copy(src_path, dest_path)
                        continue
                except ValueError:
                    pass

            if src_path is None:
                print(f"Warning: Could not resolve source file for slot texture '{dds_export_name}', falling back to Blender image conversion")

            target_format = self._find_texture_format(dds_export_name)
            tga_name = dds_export_name.replace('.dds', '.tga')
            tga_path = self.textures_path / tga_name

            print(f'Converting {dds_export_name} via TGA...')
            if not self._save_image_as_tga(image, tga_path):
                continue

            cmd = [
                str(texconv_path),
                '-f', target_format,
                '-srgb',
                '-m', '1',
                '-y',
                '-o', str(self.textures_path),
                str(tga_path),
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=60)
                if result.returncode != 0:
                    print(f"Warning: texconv failed for {tga_name}: {result.stderr.decode('utf-8', errors='replace')}")
            except subprocess.TimeoutExpired:
                print(f"Warning: texconv timed out for {tga_name}")
            except Exception as e:
                print(f"Warning: texconv error for {tga_name}: {e}")
            finally:
                if tga_path.is_file():
                    tga_path.unlink()

    @staticmethod
    def _save_image_as_tga(image, tga_path: Path):
        original_filepath_raw = image.filepath_raw
        try:
            original_file_format = image.file_format
        except Exception:
            original_file_format = None
        try:
            image.filepath_raw = str(tga_path)
            image.file_format = 'TARGA'
            image.save()
            return True
        except Exception as e:
            print(f"Warning: Failed to save image '{image.name}' as TGA: {e}")
            try:
                image.save_render(filepath=str(tga_path))
                return True
            except Exception as render_error:
                print(f"Warning: Failed to save image '{image.name}' as render TGA: {render_error}")
                try:
                    return ModExporter._save_image_pixels_as_tga(image, tga_path)
                except Exception as pixels_error:
                    print(f"Warning: Failed to export image '{image.name}' from pixels as TGA: {pixels_error}")
                    return False
        finally:
            image.filepath_raw = original_filepath_raw
            if original_file_format:
                try:
                    image.file_format = original_file_format
                except Exception as restore_error:
                    print(f"Warning: Failed to restore image '{image.name}' file format '{original_file_format}': {restore_error}")

    @staticmethod
    def _save_image_pixels_as_tga(image, tga_path: Path):
        width, height = image.size[0], image.size[1]
        if width <= 0 or height <= 0:
            raise ValueError('image size is empty')

        temp_image = bpy.data.images.new(
            name=f'{image.name}_slot_export',
            width=width,
            height=height,
            alpha=True,
        )
        try:
            temp_pixels = list(image.pixels)
            if not temp_pixels:
                raise ValueError('image has no pixel data')
            temp_image.pixels.foreach_set(temp_pixels)
            temp_image.filepath_raw = str(tga_path)
            temp_image.file_format = 'TARGA'
            temp_image.save()
            return True
        finally:
            bpy.data.images.remove(temp_image)

    @staticmethod
    def _normalize_existing_path(path_text):
        if not path_text:
            return None
        try:
            expanded = bpy.path.abspath(path_text)
        except Exception:
            expanded = path_text
        normalized = Path(os.path.normpath(expanded))
        if normalized.is_file():
            return normalized
        return None

    def _resolve_slot_texture_source(self, slot_texture):
        image = slot_texture['image']
        candidates = [
            slot_texture.get('source_path'),
            getattr(image, 'filepath', ''),
            getattr(image, 'filepath_raw', ''),
        ]
        combined = image.filepath or image.filepath_raw
        if combined:
            candidates.append(bpy.path.abspath(combined))
        checked = []
        for candidate in candidates:
            if isinstance(candidate, Path):
                candidate = str(candidate)
            if not candidate:
                continue
            checked.append(str(candidate))
            path = self._normalize_existing_path(str(candidate))
            if path is not None:
                return path
        print(f"Warning: Source candidates not found for {slot_texture.get('dds_export_name')}: {checked}")
        return None

    def compare_outputs(self, old_path: Path, new_path: Path):

        global data_models
        data_model = data_models['WWMI']

        for buffer_name, layout in data_model.buffers_format.items():

            print(f'Comparing {buffer_name}.buf buffers...')

            with open(old_path / (buffer_name + '.buf'), 'rb') as f1, open(new_path / (buffer_name + '.buf'), 'rb') as f2:
                
                old_buffer = NumpyBuffer(layout)
                old_buffer.import_raw_data(f1.read())

                new_buffer = NumpyBuffer(layout)
                new_buffer.import_raw_data(f2.read())

                for semantic in layout.semantics:

                    old_semantic_data = old_buffer.get_field(semantic.get_name()).tolist()
                    new_semantic_data = new_buffer.get_field(semantic.get_name()).tolist()

                    if old_semantic_data == new_semantic_data:
                        print(f'{buffer_name} {semantic.abstract} matches!')
                    else:
                        # print(f'{buffer_name} {semantic.abstract} differs:')

                        verbose = True
                        if buffer_name == 'Vector':
                            print(f'Comparing {semantic.abstract} in silent mode...')
                            verbose = False
                        else:
                            print(f'Comparing {semantic.abstract} in verbose mode...')

                        num_diffs = 0

                        for i in range(len(old_semantic_data)):
                            old_data = old_semantic_data[i]
                            new_data = new_semantic_data[i]

                            if old_data != new_data:
                                num_diffs += 1
                                if verbose:
                                    print(f'Element {i} diff: {old_data} != {new_data}')

                        print(f'Found {num_diffs} diffs (out of {len(old_semantic_data)} entries)')

def blender_export(operator, context, cfg, excluded_buffers):
    mod_exporter = ModExporter(context, cfg, excluded_buffers)
    mod_exporter.export_mod()
