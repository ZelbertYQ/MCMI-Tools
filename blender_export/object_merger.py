import re
import bpy

from typing import List, Dict, Union, Tuple, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from textwrap import dedent

from ..addon.exceptions import ConfigError

from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.objects import *

from ..migoto_io.blender_tools.modifiers import apply_modifiers_for_object_with_shape_keys
from ..migoto_io.blender_tools.vertex_groups import fill_gaps_in_vertex_groups

from ..extract_frame_data.metadata_format import ExtractedObject


class SkeletonType(Enum):
    Merged = 'Merged'
    PerComponent = 'Per-Component'


class VgRemapMode(Enum):
    MergedToComponent = 'MERGED_TO_COMPONENT'
    ComponentToMerged = 'COMPONENT_TO_MERGED'


def _enum_equal(lhs, rhs):
    if lhs == rhs:
        return True
    lhs_value = getattr(lhs, 'value', getattr(lhs, 'name', lhs))
    rhs_value = getattr(rhs, 'value', getattr(rhs, 'name', rhs))
    return lhs_value == rhs_value


@dataclass
class TempObject:
    name: str
    object: bpy.types.Object
    vertex_count: int = 0
    index_count: int = 0
    index_offset: int = 0
    material: dict = None


@dataclass
class MergedObjectComponent:
    objects: List[TempObject]
    vertex_count: int = 0
    index_count: int = 0
    index_offset: int = 0
    blend_remap_id: int = -1
    blend_remap_vg_count: int = 0
    match_formats: list = field(default_factory=list)
    slot_hashes: list = field(default_factory=list)
    material: dict = None
    
    def get_object(self, object_name):
        for obj in self.objects:
            if obj.name == object_name:
                return obj


@dataclass
class MergedObjectShapeKeysBatch:
    vertex_count: int = field(default_factory=list)
    vertex_offset: int = field(default_factory=list)


@dataclass
class MergedObjectShapeKeys:
    vertex_count: int = 0
    batches: list[MergedObjectShapeKeysBatch] = field(default_factory=list)
    vertex_count_batch0: int = 0
    vertex_count_batch1: int = 0
    shapekey_count: int = 0


@dataclass
class MergedObject:
    object: bpy.types.Object
    mesh: bpy.types.Mesh
    components: List[MergedObjectComponent]
    shapekeys: MergedObjectShapeKeys
    skeleton_type: SkeletonType
    vertex_count: int = 0
    index_count: int = 0
    vg_count: int = 0
    blend_remap_count: int = 0


@dataclass
class ObjectMerger:
    # Input
    context: bpy.types.Context
    extracted_object: ExtractedObject
    ignore_nested_collections: bool
    ignore_hidden_collections: bool
    ignore_hidden_objects: bool
    ignore_muted_shape_keys: bool
    apply_modifiers: bool
    collection: str
    skeleton_type: SkeletonType
    vg_remap_mode: Optional[VgRemapMode] = None
    mesh_scale: float = 1.0
    mesh_rotation: Tuple[float] = (0.0, 0.0, 0.0)
    add_missing_vertex_groups: bool = False
    # Output
    merged_object: MergedObject = field(init=False)

    @staticmethod
    def _parse_vg_id(name: str, fallback: int) -> int:
        match = re.match(r'^\s*(\d+)', name or '')
        if match:
            return int(match.group(1))
        return fallback

    @classmethod
    def _get_vg_id(cls, vg: bpy.types.VertexGroup) -> int:
        return cls._parse_vg_id(vg.name, vg.index)

    @classmethod
    def _collect_used_vg_ids(cls, obj: bpy.types.Object) -> Set[int]:
        used = set()
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                if group.weight <= 0:
                    continue
                vg = obj.vertex_groups[group.group]
                used.add(cls._get_vg_id(vg))
        return used

    @staticmethod
    def _normalize_vg_map(raw_map: Dict) -> Dict[int, int]:
        result = {}
        if not isinstance(raw_map, dict):
            return result
        for key, value in raw_map.items():
            if isinstance(key, str) and key.isdigit():
                key = int(key)
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            if isinstance(key, int) and isinstance(value, int):
                result[key] = value
        return result

    @classmethod
    def _invert_vg_map(cls, vg_map: Dict[int, int], component_id: int) -> Dict[int, int]:
        inverse = {}
        duplicates = {}
        for local_id, global_id in vg_map.items():
            if global_id in inverse and inverse[global_id] != local_id:
                duplicates.setdefault(global_id, set()).update([inverse[global_id], local_id])
                continue
            inverse[global_id] = local_id
        if duplicates:
            dup_lines = []
            for global_id, locals_set in sorted(duplicates.items()):
                locals_list = ', '.join(str(v) for v in sorted(locals_set))
                dup_lines.append(f'Global {global_id} -> locals {locals_list}')
            raise ConfigError(
                'component_collection',
                'Component {0} has duplicate global VG ids in Metadata.json:\n{1}'.format(
                    component_id, '\n'.join(dup_lines)
                )
            )
        return inverse

    @classmethod
    def _find_vg_by_id(cls, obj: bpy.types.Object, vg_id: int) -> Optional[bpy.types.VertexGroup]:
        target_name = str(vg_id)
        vg = obj.vertex_groups.get(target_name)
        if vg is not None:
            return vg
        for candidate in obj.vertex_groups:
            if cls._get_vg_id(candidate) == vg_id:
                return candidate
        return None

    @classmethod
    def _get_or_create_vg(cls, obj: bpy.types.Object, vg_id: int) -> bpy.types.VertexGroup:
        vg = obj.vertex_groups.get(str(vg_id))
        if vg is None:
            vg = obj.vertex_groups.new(name=str(vg_id))
        return vg

    @classmethod
    def _remap_vertex_groups(cls, obj: bpy.types.Object, mapping: Dict[int, int], component_id: int, label: str):
        used_ids = cls._collect_used_vg_ids(obj)
        missing = [vg_id for vg_id in sorted(used_ids) if vg_id not in mapping]
        if missing:
            missing_str = ', '.join(str(vg_id) for vg_id in missing)
            raise ConfigError(
                'component_collection',
                f'Component {component_id} has VG ids not found in {label} mapping: {missing_str}'
            )

        weights_by_target = {}
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                if group.weight <= 0:
                    continue
                src_group = obj.vertex_groups[group.group]
                src_id = cls._get_vg_id(src_group)
                if src_id not in mapping:
                    continue
                dst_id = mapping.get(src_id, src_id)
                weights_by_target.setdefault(dst_id, []).append((vertex.index, group.weight))

        target_ids = set(weights_by_target.keys())
        if not target_ids:
            return
        max_target_id = max(target_ids)

        for vg in list(obj.vertex_groups):
            obj.vertex_groups.remove(vg)

        for vg_id in range(max_target_id + 1):
            obj.vertex_groups.new(name=str(vg_id))

        for dst_id, weights in weights_by_target.items():
            if dst_id < len(obj.vertex_groups):
                dst_group = obj.vertex_groups[dst_id]
                for vertex_index, weight in weights:
                    dst_group.add([vertex_index], weight, 'ADD')

    def _apply_merged_to_component_remap(self, obj: bpy.types.Object, component_id: int):
        component_meta = self.extracted_object.components[component_id]
        vg_map = self._normalize_vg_map(component_meta.vg_map)
        inverse = self._invert_vg_map(vg_map, component_id)
        self._remap_vertex_groups(obj, inverse, component_id, 'Merged->Per')

    def _apply_component_to_merged_remap(self, obj: bpy.types.Object, component_id: int):
        component_meta = self.extracted_object.components[component_id]
        vg_map = self._normalize_vg_map(component_meta.vg_map)
        self._remap_vertex_groups(obj, vg_map, component_id, 'Per->Merged')

    @staticmethod
    def _format_vg_conflicts(conflicts: Dict[int, Set[int]]) -> str:
        lines = []
        for vg_id, components in sorted(conflicts.items()):
            comp_list = ', '.join(str(comp_id) for comp_id in sorted(components))
            lines.append(f'{vg_id}: components {comp_list}')
        return '\n'.join(lines)

    @staticmethod
    def _rename_layer_collection_sequential(layer_collection, name_builder, temp_prefix):
        if layer_collection is None or len(layer_collection) == 0:
            return

        # Two-pass rename to avoid collisions (Blender may append .001 otherwise).
        for layer_index, layer in enumerate(layer_collection):
            layer.name = f'{temp_prefix}_{layer_index}'

        for layer_index, layer in enumerate(layer_collection):
            layer.name = name_builder(layer_index)

    @classmethod
    def rename_temp_attribute_layers_by_slot(cls, temp_obj: bpy.types.Object):
        mesh = getattr(temp_obj, 'data', None)
        if mesh is None:
            return

        if hasattr(mesh, 'uv_layers'):
            cls._rename_layer_collection_sequential(
                mesh.uv_layers,
                lambda uv_index: f'TEXCOORD{uv_index if uv_index > 0 else ""}.xy',
                '__MCMI_TMP_UV'
            )

        if hasattr(mesh, 'color_attributes') and mesh.color_attributes is not None and len(mesh.color_attributes) > 0:
            cls._rename_layer_collection_sequential(
                mesh.color_attributes,
                lambda color_index: f'COLOR{color_index if color_index > 0 else ""}',
                '__MCMI_TMP_COLOR'
            )
        elif hasattr(mesh, 'vertex_colors') and mesh.vertex_colors is not None:
            cls._rename_layer_collection_sequential(
                mesh.vertex_colors,
                lambda color_index: f'COLOR{color_index if color_index > 0 else ""}',
                '__MCMI_TMP_COLOR'
            )

    def __post_init__(self):
        collection_was_hidden = collection_is_hidden(self.collection)
        unhide_collection(self.collection)

        self.initialize_components()
        try:
            self.import_objects_from_collection()
            self.prepare_temp_objects()
            self.pre_join_objects()
            self.build_merged_object()
        except Exception as e:
            self.remove_temp_objects()
            raise e
        
        if collection_was_hidden:
            hide_collection(self.collection)

    def pre_join_objects(self):
        pass

    def initialize_components(self):
        self.components = []
        for component_id, component in enumerate(self.extracted_object.components): 
            self.components.append(
                MergedObjectComponent(
                    objects=[],
                    index_count=0,
                )
            )

    def import_objects_from_collection(self):

        num_objects = 0
        
        component_pattern = re.compile(r'.*component[_ -]*(\d+).*')

        for obj in get_collection_objects(self.collection, 
                                          recursive = not self.ignore_nested_collections, 
                                          skip_hidden_collections = self.ignore_hidden_collections):

            if self.ignore_hidden_objects and object_is_hidden(obj):
                continue

            if obj.name.startswith('TEMP_'):
                continue
            
            match = component_pattern.findall(obj.name.lower())
            if len(match) == 0:
                continue
            component_id = int(match[0])

            if component_id >= len(self.components):
                raise ConfigError('object_source_folder', f'Metadata.json in specified folder is missing Component {component_id}!\nMost likely it contains sources for other object.')

            temp_obj = copy_object(self.context, obj, name=f'TEMP_{obj.name}', collection=self.collection)

            self.components[component_id].objects.append(TempObject(
                name=obj.name,
                object=temp_obj,
            ))

            num_objects += 1

        if num_objects == 0:
            raise ValueError(f'No eligible `Component` objects found!')

    def prepare_temp_objects(self):
        index_offset = 0
        component_used_vg_ids = {}

        for component_id, component in enumerate(self.components):

            component.objects.sort(key=lambda x: x.name)
            component.index_offset = index_offset

            for temp_object in component.objects:
                temp_obj = temp_object.object
                # Remove muted shape keys
                if self.ignore_muted_shape_keys and temp_obj.data.shape_keys:
                    muted_shape_keys = []
                    for shapekey_id in range(len(temp_obj.data.shape_keys.key_blocks)):
                        shape_key = temp_obj.data.shape_keys.key_blocks[shapekey_id]
                        if shape_key.mute:
                            muted_shape_keys.append(shape_key)
                    for shape_key in muted_shape_keys:
                        temp_obj.shape_key_remove(shape_key)
                # Modify temporary object
                with OpenObject(self.context, temp_obj, mode='OBJECT') as obj:
                    # Apply all transforms
                    bpy.ops.object.transform_apply(location = True, rotation = True, scale = True)
                    # Apply all modifiers
                    if self.apply_modifiers:
                        selected_modifiers = [modifier.name for modifier in get_modifiers(obj)]
                        apply_modifiers_for_object_with_shape_keys(self.context, selected_modifiers, None)
                    # Triangulate (this step is crucial since export supports only triangles)
                    triangulate_object(self.context, temp_obj)
                # Normalize UV/Color layer names by slot order on temp objects only.
                # This keeps export deterministic even if source layer names are arbitrary.
                self.rename_temp_attribute_layers_by_slot(temp_obj)
                if _enum_equal(self.vg_remap_mode, VgRemapMode.MergedToComponent):
                    used_ids = self._collect_used_vg_ids(temp_obj)
                    if used_ids:
                        component_used_vg_ids.setdefault(component_id, set()).update(used_ids)
                    self._apply_merged_to_component_remap(temp_obj, component_id)
                elif _enum_equal(self.vg_remap_mode, VgRemapMode.ComponentToMerged):
                    self._apply_component_to_merged_remap(temp_obj, component_id)
                # Handle Vertex Groups
                vertex_groups = get_vertex_groups(temp_obj)
                # Fill gaps in Vertex Groups list based on VG names (i.e. add group '1' between '0' and '2' if it's missing)
                if self.add_missing_vertex_groups:
                    fill_gaps_in_vertex_groups(self.context, temp_obj, internal_call=True)
                # Remove ignored or unexpected vertex groups
                ignore_list = []
                if _enum_equal(self.skeleton_type, SkeletonType.Merged):
                    # Exclude VGs with 'ignore' tag or with higher VG id than total VG count from Metadata.ini
                    total_vg_count = sum([component.vg_count for component in self.extracted_object.components])
                    ignore_list = [
                        vg for vg in vertex_groups
                        if (
                            'ignore' in vg.name.lower()
                            or not vg.name.strip().isdigit()
                            or (self._get_vg_id(vg) if self.vg_remap_mode else vg.index) >= total_vg_count
                        )
                    ]
                elif _enum_equal(self.skeleton_type, SkeletonType.PerComponent):
                    # Exclude VGs with 'ignore' tag or with higher id VG count from Metadata.ini for current component
                    extracted_component = self.extracted_object.components[component_id]
                    total_vg_count = len(extracted_component.vg_map)
                    ignore_list = [
                        vg for vg in vertex_groups
                        if (
                            'ignore' in vg.name.lower()
                            or not vg.name.strip().isdigit()
                            or (self._get_vg_id(vg) if self.vg_remap_mode else vg.index) >= total_vg_count
                        )
                    ]
                remove_vertex_groups(temp_obj, ignore_list)
                # Rename VGs to their indicies to merge ones of different components together
                for vg in get_vertex_groups(temp_obj):
                    vg.name = str(vg.index)
                # Calculate vertex count of temporary object
                temp_object.vertex_count = len(temp_obj.data.vertices)
                # Calculate index count of temporary object, IB stores 3 indices per triangle
                temp_object.index_count = len(temp_obj.data.polygons) * 3
                # Set index offset of temporary object to global index_offset
                temp_object.index_offset = index_offset
                # Update global index_offset
                index_offset += temp_object.index_count
                # Update vertex and index count of custom component
                component.vertex_count += temp_object.vertex_count
                component.index_count += temp_object.index_count

        if _enum_equal(self.vg_remap_mode, VgRemapMode.MergedToComponent) and component_used_vg_ids:
            conflicts = {}
            for component_id, used_ids in component_used_vg_ids.items():
                for vg_id in used_ids:
                    conflicts.setdefault(vg_id, set()).add(component_id)
            conflicts = {vg_id: comps for vg_id, comps in conflicts.items() if len(comps) > 1}
            if conflicts:
                conflict_text = self._format_vg_conflicts(conflicts)
                print('Warning: Merged->Per shared VG ids across components:\n' + conflict_text)

    def remove_temp_objects(self):
        for component_id, component in enumerate(self.components):
            for temp_object in component.objects:
                remove_mesh(temp_object.object.data)

    def transform_merged_object(self, merged_object):
        change_scale = self.mesh_scale != 1.0
        change_rotation = self.mesh_rotation != (0.0, 0.0, 0.0)
        if not change_scale and not change_rotation:
            return
        # Compensate transforms we're about to set
        if change_scale:
            inverted_scale = 1 / self.mesh_scale
            merged_object.scale = inverted_scale, inverted_scale, inverted_scale
        if change_rotation:
            inverted_rotation = tuple([360 - r if r != 0 and r != 0 else 0 for r in self.mesh_rotation])
            merged_object.rotation_euler = to_radians(inverted_rotation)
        bpy.ops.object.transform_apply(location = False, rotation = True, scale = True)
        # Set merged object transforms
        if change_scale:
            merged_object.scale = self.mesh_scale, self.mesh_scale, self.mesh_scale
        if change_rotation:
            merged_object.rotation_euler = to_radians(self.mesh_rotation)

    def build_merged_object(self):

        merged_object = []
        vertex_count, index_count = 0, 0
        for component in self.components:
            for temp_object in component.objects:
                merged_object.append(temp_object.object)
            vertex_count += component.vertex_count
            index_count += component.index_count
            
        join_objects(self.context, merged_object)

        obj = merged_object[0]

        rename_object(obj, 'TEMP_EXPORT_OBJECT')

        deselect_all_objects()
        select_object(obj)
        set_active_object(bpy.context, obj)
        
        self.transform_merged_object(obj)

        mesh = obj.evaluated_get(self.context.evaluated_depsgraph_get()).to_mesh()

        self.merged_object = MergedObject(
            object=obj,
            mesh=mesh,
            components=self.components,
            vertex_count=len(obj.data.vertices),
            index_count=len(obj.data.polygons) * 3,
            vg_count=len(get_vertex_groups(obj)),
            shapekeys=MergedObjectShapeKeys(),
            skeleton_type=self.skeleton_type,
        )

        if vertex_count != self.merged_object.vertex_count:
            raise ValueError('vertex_count mismatch between merged object and its components')

        if index_count != self.merged_object.index_count:
            raise ValueError('index_count mismatch between merged object and its components')
