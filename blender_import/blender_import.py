import os
import numpy
import time
import re
import json
import bpy

from pathlib import Path

from bpy_extras.io_utils import axis_conversion

from ..addon.exceptions import ConfigError

from ..migoto_io.blender_interface.utility import *
from ..migoto_io.blender_interface.collections import *
from ..migoto_io.blender_interface.objects import *
from ..migoto_io.data_model.data_model import DataModel
from ..migoto_io.data_model.byte_buffer import NumpyBuffer, MigotoFmt, AbstractSemantic, Semantic
from ..migoto_io.data_model.dxgi_format import DXGIFormat
from ..migoto_io.blender_tools.vertex_groups import remove_unused_vertex_groups

from ..extract_frame_data.metadata_format import read_metadata


# TODO: Add support of import of unhandled semantics into vertex attributes
class ObjectImporter:

    def read_texture_usage(self, object_source_folder: Path):
        texture_usage_path = object_source_folder / 'TextureUsage.json'
        if not texture_usage_path.is_file():
            return {}
        try:
            with open(texture_usage_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def read_texture_format(self, object_source_folder: Path):
        texture_format_path = object_source_folder / 'TextureFormat.json'
        if not texture_format_path.is_file():
            return {}
        try:
            with open(texture_format_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def parse_shading_filter_hashes(self, cfg):
        raw = getattr(cfg, 'shading_filter_hashes', '[]')
        try:
            data = json.loads(raw)
        except Exception:
            return set()
        if not isinstance(data, list):
            return set()
        result = set()
        for value in data:
            if isinstance(value, str) and re.fullmatch(r'[a-fA-F0-9]{8}', value):
                result.add(value.lower())
        return result

    def resolve_texture_path(self, object_source_folder: Path, texture_hash):
        texture_hash = texture_hash.lower()
        if texture_hash:
            candidates = list(object_source_folder.glob(f'*t={texture_hash}.*'))
            if len(candidates) > 0:
                return candidates[0]

        return None

    def parse_component_id(self, component_name: str):
        if not isinstance(component_name, str):
            return None
        result = re.findall(r'component[ -_]*([0-9]+)', component_name.lower())
        if len(result) != 1:
            return None
        return int(result[0])

    def is_bc7_2048_texture(self, texture_hash, texture_format):
        textures_map = texture_format.get('textures', {})
        texture_info = textures_map.get(texture_hash.lower(), {})
        source_formats = texture_info.get('source_formats', [])
        if 'BC7_UNORM_SRGB' not in [fmt.upper() for fmt in source_formats if isinstance(fmt, str)]:
            return False
        size = texture_info.get('size', [])
        if not isinstance(size, list) or len(size) != 2:
            return None
        return size[0] == 2048 and size[1] == 2048

    def build_shading_plan(self, texture_format, shading_filter_hashes):
        textures_map = texture_format.get('textures', {})
        remaining = {}
        all_components = set()
        file_by_hash = {}
        for texture_hash, texture_info in textures_map.items():
            texture_hash_l = texture_hash.lower()
            if texture_hash_l in shading_filter_hashes:
                continue
            if not self.is_bc7_2048_texture(texture_hash_l, texture_format):
                continue
            components = texture_info.get('components', [])
            if not isinstance(components, list):
                continue
            component_ids = set()
            for component_id in components:
                if isinstance(component_id, int):
                    component_ids.add(component_id)
                elif isinstance(component_id, str) and component_id.isdigit():
                    component_ids.add(int(component_id))
            if len(component_ids) == 0:
                continue
            remaining[texture_hash_l] = component_ids
            all_components.update(component_ids)
            file_by_hash[texture_hash_l] = texture_info.get('file', '')

        assignments = {}
        assigned_hashes = set()
        changed = True
        while changed:
            changed = False
            for texture_hash in list(remaining.keys()):
                if texture_hash in assigned_hashes:
                    del remaining[texture_hash]
                    changed = True
                    continue
                reduced_components = set([component_id for component_id in remaining[texture_hash] if component_id not in assignments])
                if reduced_components != remaining[texture_hash]:
                    remaining[texture_hash] = reduced_components
                    changed = True
                if len(reduced_components) == 0:
                    del remaining[texture_hash]
                    changed = True
                    continue
                if len(reduced_components) == 1:
                    component_id = next(iter(reduced_components))
                    if component_id not in assignments:
                        assignments[component_id] = texture_hash
                        assigned_hashes.add(texture_hash)
                        del remaining[texture_hash]
                        changed = True

        conflicts = {}
        for component_id in all_components:
            if component_id in assignments:
                continue
            candidates = sorted([texture_hash for texture_hash, component_ids in remaining.items() if component_id in component_ids])
            if len(candidates) > 0:
                conflicts[component_id] = candidates

        return {
            'assignments': assignments,
            'conflicts': conflicts,
            'files': file_by_hash,
        }

    def create_image_node(self, nodes, image, x, y, label):
        image_texture = nodes.new('ShaderNodeTexImage')
        image_texture.label = label
        image_texture.location = (x, y)
        image_texture.image = image
        return image_texture

    def apply_auto_diffuse_material(self, obj, object_source_folder: Path, component_name: str, shading_plan):
        component_id = self.parse_component_id(component_name)
        if component_id is None:
            return

        assignments = shading_plan.get('assignments', {})
        conflicts = shading_plan.get('conflicts', {})
        file_by_hash = shading_plan.get('files', {})

        material = bpy.data.materials.get(obj.name)
        if material is None:
            material = bpy.data.materials.new(name=obj.name)
        material.use_nodes = True

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        principled = next((node for node in nodes if node.type == 'BSDF_PRINCIPLED'), None)
        if principled is None:
            principled = nodes.new('ShaderNodeBsdfPrincipled')

        material_output = next((node for node in nodes if node.type == 'OUTPUT_MATERIAL'), None)
        if material_output is None:
            material_output = nodes.new('ShaderNodeOutputMaterial')

        if not principled.outputs['BSDF'].is_linked:
            links.new(principled.outputs['BSDF'], material_output.inputs['Surface'])

        for link in list(principled.inputs['Base Color'].links):
            links.remove(link)

        x_base = principled.location[0] - 350
        y_base = principled.location[1]

        diffuse_hash = assignments.get(component_id)
        if diffuse_hash is not None:
            texture_file = file_by_hash.get(diffuse_hash, '')
            texture_path = object_source_folder / texture_file if texture_file else self.resolve_texture_path(object_source_folder, diffuse_hash)
            if texture_path is not None and Path(texture_path).is_file():
                image = bpy.data.images.load(str(texture_path), check_existing=True)
                image.alpha_mode = 'CHANNEL_PACKED'
                image_texture = self.create_image_node(nodes, image, x_base, y_base, 'Diffuse Texture')
                links.new(image_texture.outputs['Color'], principled.inputs['Base Color'])
        else:
            conflict_hashes = conflicts.get(component_id, [])
            x_conflict = x_base - 350
            for index, texture_hash in enumerate(conflict_hashes):
                texture_file = file_by_hash.get(texture_hash, '')
                texture_path = object_source_folder / texture_file if texture_file else self.resolve_texture_path(object_source_folder, texture_hash)
                if texture_path is None or not Path(texture_path).is_file():
                    continue
                image = bpy.data.images.load(str(texture_path), check_existing=True)
                image.alpha_mode = 'CHANNEL_PACKED'
                self.create_image_node(nodes, image, x_conflict, y_base - index * 280, f'Conflict {texture_hash}')

        if len(obj.data.materials) == 0:
            obj.data.materials.append(material)
        else:
            obj.data.materials[0] = material

    def import_object(self, operator, context, cfg):

        object_source_folder = resolve_path(cfg.object_source_folder)
        texture_usage = self.read_texture_usage(object_source_folder)
        texture_format = self.read_texture_format(object_source_folder)
        shading_filter_hashes = self.parse_shading_filter_hashes(cfg)
        shading_plan = self.build_shading_plan(texture_format, shading_filter_hashes)

        if not object_source_folder.is_dir():
            raise ConfigError('object_source_folder', 'Specified sources folder does not exist!')

        start_time = time.time()
        print(f"Object import started for '{object_source_folder.stem}' folder")

        imported_objects = []
        
        for filename in os.listdir(object_source_folder):
            if not filename.endswith('fmt'):
                continue

            fmt_path = object_source_folder / filename
            ib_path = fmt_path.with_suffix('.ib')
            vb_path = fmt_path.with_suffix('.vb')

            if not ib_path.is_file():
                raise ConfigError('object_source_folder', f'Specified folder is missing .fmt file for {fmt_path.stem}!')
            if not vb_path.is_file():
                raise ConfigError('object_source_folder', f'Specified folder is missing .fmt file for {fmt_path.stem}!')

            obj = self.import_component(
                operator, context, cfg, fmt_path, ib_path, vb_path,
                texture_usage=texture_usage,
                texture_format=texture_format,
                shading_filter_hashes=shading_filter_hashes,
                shading_plan=shading_plan
            )

            # from .import_old import import_3dmigoto_vb_ib
            # obj = import_3dmigoto_vb_ib(operator, context, cfg, [((vb_path, fmt_path), (ib_path, fmt_path), True, None)], flip_mesh=cfg.mirror_mesh, flip_winding=True)
            
            imported_objects.append(obj)
        
        if len(imported_objects) == 0:
            raise ConfigError('object_source_folder', 'Specified folder is missing .fmt files for components!')

        col = new_collection(object_source_folder.stem)
        for obj in imported_objects:
            link_object_to_collection(obj, col)
            if cfg.skip_empty_vertex_groups and cfg.import_skeleton_type == 'MERGED':
                remove_unused_vertex_groups(context, obj)

        print(f'Total import time: {time.time() - start_time :.3f}s')

    def import_component(self, operator, context, cfg, fmt_path: Path, ib_path: Path, vb_path: Path, axis_forward='Y', axis_up='Z', texture_usage=None, texture_format=None, shading_filter_hashes=None, shading_plan=None):

        start_time = time.time()

        with open(fmt_path, 'r') as fmt, open(ib_path, 'rb') as ib, open(vb_path, 'rb') as vb:
            migoto_fmt = MigotoFmt(fmt)

            # Migrate old 3UV+2COLOR layout to 4UV+1COLOR
            # Old WWMI-Tools incorrectly mapped TEXCOORD1 as COLOR1 (R16G16_UNORM)
            color1 = migoto_fmt.vb_layout.get_element(AbstractSemantic(Semantic.Color, 1))
            if color1 is not None and color1.format == DXGIFormat.R16G16_UNORM:
                for sem in migoto_fmt.vb_layout.semantics:
                    if sem.abstract.enum == Semantic.Color and sem.abstract.index == 1:
                        sem.abstract = AbstractSemantic(Semantic.TexCoord, 1)
                        sem.format = DXGIFormat.R16G16_FLOAT
                    elif sem.abstract.enum == Semantic.TexCoord and sem.abstract.index >= 1:
                        sem.abstract = AbstractSemantic(Semantic.TexCoord, sem.abstract.index + 1)

            index_buffer = NumpyBuffer(migoto_fmt.ib_layout)
            index_buffer.import_raw_data(ib.read())

            vertex_buffer = NumpyBuffer(migoto_fmt.vb_layout)
            vertex_buffer.import_raw_data(vb.read())

            object_source_folder = resolve_path(cfg.object_source_folder)
            try:
                extracted_object = read_metadata(object_source_folder / 'Metadata.json')
            except FileNotFoundError:
                raise ConfigError('object_source_folder', 'Specified folder is missing Metadata.json!')
            except Exception as e:
                raise ConfigError('object_source_folder', f'Failed to load Metadata.json:\n{e}')
            
            vg_remap = None
            if cfg.import_skeleton_type == 'MERGED':
                component_pattern = re.compile(r'.*component[ -_]*([0-9]+).*')
                result = component_pattern.findall(fmt_path.name.lower())
                if len(result) == 1:
                    component = extracted_object.components[int(result[0])]
                    vg_remap = numpy.array(list(component.vg_map.values()))

            mesh = bpy.data.meshes.new(fmt_path.stem)
            obj = bpy.data.objects.new(mesh.name, mesh)

            global_matrix = axis_conversion(from_forward=axis_forward, from_up=axis_up).to_4x4()
            obj.matrix_world = global_matrix

            model = DataModel()
            model.flip_winding = True
            model.flip_texcoord_v = True
            model.legacy_vertex_colors = cfg.color_storage == 'LEGACY'

            model.set_data(obj, mesh, index_buffer, vertex_buffer, vg_remap, mirror_mesh=cfg.mirror_mesh, mesh_scale=0.01, mesh_rotation=(0, 0, 180))
            if texture_usage is None:
                texture_usage = {}
            if texture_format is None:
                texture_format = {}
            if shading_filter_hashes is None:
                shading_filter_hashes = set()
            if shading_plan is None:
                shading_plan = {}
            self.apply_auto_diffuse_material(obj, object_source_folder, fmt_path.stem, shading_plan)

            num_shapekeys = 0 if obj.data.shape_keys is None else len(getattr(obj.data.shape_keys, 'key_blocks', []))

            print(f'{fmt_path.stem} import time: {time.time()-start_time :.3f}s ({len(obj.data.vertices)} vertices, {len(obj.data.loops)} indices, {num_shapekeys} shapekeys)')

            return obj


def blender_import(operator, context, cfg):
    object_importer = ObjectImporter()
    object_importer.import_object(operator, context, cfg)
