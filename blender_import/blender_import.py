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
    def __init__(self):
        self._texture_mask_cache = {}
        self._uv_mask_cache = {}
        self._diffuse_match_logs = {}
        self._texture_path_index_cache = {}
        self._performance_log = {
            'stages': {},
            'components': {},
            'textures': {},
        }

    def perf_now(self):
        return time.perf_counter()

    def perf_add(self, bucket, key, duration):
        if duration is None:
            return
        target = self._performance_log.setdefault(bucket, {})
        if key not in target:
            target[key] = {
                'count': 0,
                'seconds': 0.0,
            }
        target[key]['count'] += 1
        target[key]['seconds'] += float(duration)

    def perf_component_add(self, component_key, stage, duration):
        target = self._performance_log.setdefault('components', {}).setdefault(component_key, {})
        if stage not in target:
            target[stage] = {
                'count': 0,
                'seconds': 0.0,
            }
        target[stage]['count'] += 1
        target[stage]['seconds'] += float(duration)

    def perf_texture_add(self, texture_hash, stage, duration):
        target = self._performance_log.setdefault('textures', {}).setdefault(texture_hash, {})
        if stage not in target:
            target[stage] = {
                'count': 0,
                'seconds': 0.0,
            }
        target[stage]['count'] += 1
        target[stage]['seconds'] += float(duration)


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

    def is_disabled_source_dir(self, path: Path):
        return path.name.lower().startswith('disabled')

    def is_in_disabled_source_dir(self, path: Path, root: Path):
        try:
            relative_path = path.relative_to(root)
        except ValueError:
            return False
        return any(part.lower().startswith('disabled') for part in relative_path.parts[:-1])

    def iter_enabled_files(self, root: Path):
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not dirname.lower().startswith('disabled')
            ]
            current_path = Path(current_root)
            for filename in filenames:
                yield current_path / filename

    def resolve_texture_path(self, object_source_folder: Path, texture_hash):
        texture_hash = texture_hash.lower()
        if texture_hash:
            texture_index = self.build_texture_path_index(object_source_folder)
            indexed_path = texture_index.get(texture_hash)
            if indexed_path is not None and indexed_path.is_file():
                return indexed_path

            candidates = sorted([
                texture_path
                for texture_path in self.iter_enabled_files(object_source_folder)
                if texture_path.name.lower().endswith(tuple(['.dds', '.jpg', '.jpeg', '.png']))
                and f't={texture_hash}.' in texture_path.name.lower()
            ])
            if len(candidates) > 0:
                return candidates[0]
            candidates = sorted([
                texture_path
                for texture_path in self.iter_enabled_files(object_source_folder)
                if texture_path.stem.lower() == texture_hash
            ])
            if len(candidates) > 0:
                return candidates[0]

        return None

    def build_texture_path_index(self, object_source_folder: Path):
        cache_key = str(object_source_folder.resolve())
        cached_index = self._texture_path_index_cache.get(cache_key)
        if cached_index is not None:
            return cached_index

        texture_index = {}
        hash_pattern = re.compile(r'(?<![a-fA-F0-9])([a-fA-F0-9]{8})(?![a-fA-F0-9])')
        for texture_path in sorted(self.iter_enabled_files(object_source_folder)):
            if not texture_path.is_file():
                continue
            if texture_path.suffix.lower() not in {'.dds', '.jpg', '.jpeg', '.png'}:
                continue
            matches = hash_pattern.findall(texture_path.name)
            if len(matches) == 0:
                continue
            texture_hash = matches[-1].lower()
            if texture_hash not in texture_index:
                texture_index[texture_hash] = texture_path

        self._texture_path_index_cache[cache_key] = texture_index
        return texture_index

    def parse_component_id(self, component_name: str):
        if not isinstance(component_name, str):
            return None
        result = re.findall(r'component[ -_]*([0-9]+)', component_name.lower())
        if len(result) != 1:
            return None
        return int(result[0])

    def texture_source_formats(self, texture_info):
        return [
            fmt.upper()
            for fmt in texture_info.get('source_formats', [])
            if isinstance(fmt, str)
        ]

    def texture_size(self, texture_info):
        size = texture_info.get('size', [])
        if not isinstance(size, list) or len(size) != 2:
            return None
        if not all(isinstance(value, int) and value > 0 for value in size):
            return None
        return size

    def is_diffuse_format_candidate(self, texture_info):
        source_formats = self.texture_source_formats(texture_info)
        if not any(fmt.endswith('_SRGB') for fmt in source_formats):
            return False

        size = self.texture_size(texture_info)
        if size is None:
            return False
        width, height = size
        if width != height:
            return False
        if width not in {1024, 2048}:
            return False

        return True

    def diffuse_texture_score(self, texture_info, usage=None):
        if not self.is_diffuse_format_candidate(texture_info):
            return None

        source_formats = self.texture_source_formats(texture_info)
        score = 100.0
        if any(fmt.startswith(('BC1_', 'BC3_', 'BC7_')) for fmt in source_formats):
            score += 20.0
        if any(fmt.startswith(('BC7_', 'B8G8R8A8_', 'R8G8B8A8_')) for fmt in source_formats):
            score += 10.0

        if isinstance(usage, dict):
            slot_id = usage.get('slot_id')
            if isinstance(slot_id, int):
                if slot_id in {0, 1, 2, 3}:
                    score += 6.0
                elif slot_id >= 6:
                    score -= 10.0

        return score

    def build_shading_plan(self, texture_format, shading_filter_hashes):
        start_time = self.perf_now()
        textures_map = texture_format.get('textures', {})
        file_by_hash = {}
        component_candidates = {}
        component_usage = {}
        all_components = set()
        for texture_hash, texture_info in textures_map.items():
            texture_hash_l = texture_hash.lower()
            file_by_hash[texture_hash_l] = texture_info.get('file', '')
            if texture_hash_l in shading_filter_hashes:
                continue
            if not self.is_diffuse_format_candidate(texture_info):
                continue

            components = texture_info.get('components', [])
            if isinstance(components, list):
                for component_id in components:
                    if isinstance(component_id, int):
                        all_components.add(component_id)
                    elif isinstance(component_id, str) and component_id.isdigit():
                        all_components.add(int(component_id))

            usage_entries = texture_info.get('usage', [])
            if not isinstance(usage_entries, list):
                usage_entries = []
            for usage in usage_entries:
                if not isinstance(usage, dict):
                    continue
                component_id = usage.get('component')
                call_id = usage.get('call_id')
                if not isinstance(component_id, int) or not isinstance(call_id, int):
                    continue
                if texture_hash_l not in component_candidates.setdefault(component_id, []):
                    component_candidates[component_id].append(texture_hash_l)
                component_usage.setdefault(component_id, {}).setdefault(texture_hash_l, []).append(usage)

            if len(usage_entries) == 0 and isinstance(components, list):
                for component_id in components:
                    if isinstance(component_id, str) and component_id.isdigit():
                        component_id = int(component_id)
                    if not isinstance(component_id, int):
                        continue
                    if texture_hash_l not in component_candidates.setdefault(component_id, []):
                        component_candidates[component_id].append(texture_hash_l)
                    component_usage.setdefault(component_id, {}).setdefault(texture_hash_l, []).append({
                        'component': component_id,
                        'call_id': None,
                        'slot': 'components_fallback',
                        'slot_id': 99,
                        'shader_type': None,
                        'shaders': {},
                        'file': texture_info.get('file', ''),
                    })

        assignments = {}
        conflicts = {}
        ranked_candidates = {}
        for component_id, texture_hashes in component_candidates.items():
            ranked = sorted(
                texture_hashes,
                key=lambda texture_hash: (
                    self.diffuse_texture_score(textures_map.get(texture_hash, {})) or 0,
                    min([
                        usage.get('slot_id')
                        for usage in component_usage.get(component_id, {}).get(texture_hash, [])
                        if isinstance(usage.get('slot_id'), int)
                    ] or [99]) * -1,
                    texture_hash
                ),
                reverse=True
            )
            ranked_candidates[component_id] = ranked
            if len(ranked) == 1:
                assignments[component_id] = ranked[0]
            elif len(ranked) > 1:
                conflicts[component_id] = ranked

        result = {
            'assignments': assignments,
            'conflicts': conflicts,
            'files': file_by_hash,
            'textures': textures_map,
            'usage': component_usage,
            'candidates': {
                component_id: texture_hashes
                for component_id, texture_hashes in ranked_candidates.items()
            },
        }
        self.perf_add('stages', 'build_shading_plan', self.perf_now() - start_time)
        return result

    def create_image_node(self, nodes, image, x, y, label):
        image_texture = nodes.new('ShaderNodeTexImage')
        image_texture.label = label
        image_texture.location = (x, y)
        image_texture.image = image
        return image_texture

    def texture_file_for_hash(self, object_source_folder: Path, texture_hash, file_by_hash):
        texture_path = self.resolve_texture_path(object_source_folder, texture_hash)
        if texture_path is None:
            texture_file = file_by_hash.get(texture_hash, '')
            texture_path = object_source_folder / texture_file if texture_file else None
            if texture_path is not None and self.is_in_disabled_source_dir(texture_path, object_source_folder):
                texture_path = None
        if texture_path is not None and Path(texture_path).is_file():
            return texture_path
        return None

    def normalize_block(self, cells, grid_size):
        xs = [cell[0] for cell in cells]
        ys = [cell[1] for cell in cells]
        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)
        max_y = max(ys)
        width = max_x - min_x + 1
        height = max_y - min_y + 1
        return {
            'cells': set(cells),
            'grid_size': grid_size,
            'area': len(cells),
            'bbox': (min_x, min_y, max_x, max_y),
            'centroid': (
                (sum(xs) / len(xs) + 0.5) / grid_size,
                (sum(ys) / len(ys) + 0.5) / grid_size,
            ),
            'aspect': width / max(height, 1),
            'fill': len(cells) / max(width * height, 1),
        }

    def uv_blocks(self, obj, grid_size=12):
        start_time = self.perf_now()
        cache_key = (obj.name, 'uv_blocks', grid_size)
        cached_blocks = self._uv_mask_cache.get(cache_key)
        if cached_blocks is not None:
            self.perf_component_add(obj.name, f'uv_blocks_{grid_size}_cache_hit', self.perf_now() - start_time)
            return cached_blocks

        mesh = obj.data
        if len(mesh.uv_layers) == 0:
            self.perf_component_add(obj.name, f'uv_blocks_{grid_size}', self.perf_now() - start_time)
            return None

        uv_layer = mesh.uv_layers[0].data
        occupied = set()

        def wrap_uv(value):
            value = value % 1.0
            if value < 0:
                value += 1.0
            return value

        def edge(a, b, p):
            return (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])

        for poly in mesh.polygons:
            if poly.loop_total != 3:
                continue
            uvs = []
            for loop_index in poly.loop_indices:
                uv = uv_layer[loop_index].uv
                uvs.append((wrap_uv(uv.x), wrap_uv(uv.y)))

            min_x = max(0, int(min(uv[0] for uv in uvs) * grid_size) - 1)
            max_x = min(grid_size - 1, int(max(uv[0] for uv in uvs) * grid_size) + 1)
            min_y = max(0, int(min(uv[1] for uv in uvs) * grid_size) - 1)
            max_y = min(grid_size - 1, int(max(uv[1] for uv in uvs) * grid_size) + 1)

            if max_x < min_x or max_y < min_y:
                continue

            area = edge(uvs[0], uvs[1], uvs[2])
            if abs(area) < 1e-8:
                continue

            for y in range(min_y, max_y + 1):
                for x in range(min_x, max_x + 1):
                    point = ((x + 0.5) / grid_size, (y + 0.5) / grid_size)
                    w0 = edge(uvs[1], uvs[2], point)
                    w1 = edge(uvs[2], uvs[0], point)
                    w2 = edge(uvs[0], uvs[1], point)
                    if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                        occupied.add((x, y))

        if len(occupied) == 0:
            self.perf_component_add(obj.name, f'uv_blocks_{grid_size}', self.perf_now() - start_time)
            return None

        blocks = []
        visited = set()
        for cell in occupied:
            if cell in visited:
                continue
            stack = [cell]
            visited.add(cell)
            block_cells = []
            while stack:
                current = stack.pop()
                block_cells.append(current)
                x, y = current
                for neighbor in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]:
                    if neighbor not in occupied or neighbor in visited:
                        continue
                    visited.add(neighbor)
                    stack.append(neighbor)
            blocks.append(self.normalize_block(block_cells, grid_size))

        blocks.sort(key=lambda item: item['area'], reverse=True)
        self._uv_mask_cache[cache_key] = blocks
        self.perf_component_add(obj.name, f'uv_blocks_{grid_size}', self.perf_now() - start_time)
        return blocks

    def sampled_image_colors(self, image, grid_size):
        cache_key = (image.name, 'sampled_colors', grid_size)
        cached_colors = self._texture_mask_cache.get(cache_key)
        if cached_colors is not None:
            return cached_colors

        if image.size[0] <= 0 or image.size[1] <= 0:
            return None

        scaled_image = None
        try:
            scaled_image = image.copy()
            scaled_image.scale(grid_size, grid_size)
            pixels = list(scaled_image.pixels)
        except Exception:
            return None
        finally:
            if scaled_image is not None:
                try:
                    bpy.data.images.remove(scaled_image)
                except Exception:
                    pass

        colors = {}
        for y in range(grid_size):
            for x in range(grid_size):
                offset = (y * grid_size + x) * 4
                colors[(x, y)] = numpy.array([
                    pixels[offset],
                    pixels[offset + 1],
                    pixels[offset + 2],
                ], dtype=numpy.float32)

        self._texture_mask_cache[cache_key] = colors
        return colors

    def texture_color_affinity(self, image, grid_size=12):
        cache_key = (image.name, 'color_affinity', grid_size)
        cached_affinity = self._texture_mask_cache.get(cache_key)
        if cached_affinity is not None:
            return cached_affinity

        colors = self.sampled_image_colors(image, grid_size)
        if colors is None:
            return None

        neighbor_diffs = []
        neighbor_pairs = []
        for y in range(grid_size):
            for x in range(grid_size):
                cell = (x, y)
                for neighbor in [(x + 1, y), (x, y + 1)]:
                    if neighbor not in colors:
                        continue
                    diff = float(numpy.linalg.norm(colors[cell] - colors[neighbor]))
                    neighbor_diffs.append(diff)
                    neighbor_pairs.append((cell, neighbor, diff))

        if len(neighbor_diffs) == 0:
            return None

        # Use an adaptive sigma so hand-painted gradients form soft color clouds,
        # while strong seams still appear as weak affinities.
        sigma = max(0.045, float(numpy.percentile(neighbor_diffs, 65)) * 1.35)
        edges = []
        for cell, neighbor, diff in neighbor_pairs:
            affinity = float(numpy.exp(-(diff * diff) / (2.0 * sigma * sigma)))
            edges.append((cell, neighbor, affinity))

        result = {
            'grid_size': grid_size,
            'colors': colors,
            'edges': edges,
            'sigma': sigma,
        }
        self._texture_mask_cache[cache_key] = result
        return result

    def uv_block_cloud_score(self, uv_block, color_affinity):
        cells = uv_block['cells']
        internal_affinities = []
        boundary_affinities = []

        for cell, neighbor, affinity in color_affinity['edges']:
            cell_inside = cell in cells
            neighbor_inside = neighbor in cells
            if cell_inside and neighbor_inside:
                internal_affinities.append(affinity)
            elif cell_inside != neighbor_inside:
                boundary_affinities.append(affinity)

        if len(internal_affinities) == 0:
            return 0.0

        internal_mean = float(numpy.mean(internal_affinities))
        internal_p20 = float(numpy.percentile(internal_affinities, 20))
        if len(boundary_affinities) > 0:
            boundary_mean = float(numpy.mean(boundary_affinities))
            boundary_p35 = float(numpy.percentile(boundary_affinities, 35))
        else:
            boundary_mean = internal_mean
            boundary_p35 = internal_mean

        cloud_coherence = internal_mean * 70.0 + internal_p20 * 35.0
        edge_alignment = max(0.0, internal_mean - boundary_mean) * 85.0
        seam_strength = max(0.0, internal_p20 - boundary_p35) * 45.0

        boundary_scale = min(1.0, len(boundary_affinities) / max((uv_block['area'] ** 0.5) * 4.0, 1.0))
        edge_alignment *= boundary_scale
        seam_strength *= boundary_scale

        return cloud_coherence + edge_alignment + seam_strength

    def texture_uv_geometry_score(self, obj, image):
        uv_blocks = self.uv_blocks(obj)
        color_affinity = self.texture_color_affinity(image)
        if uv_blocks is None or color_affinity is None:
            return None
        if len(uv_blocks) == 0:
            return None

        total_uv_area = sum(block['area'] for block in uv_blocks)
        if total_uv_area == 0:
            return None

        score = 0.0
        for uv_block in uv_blocks:
            block_score = self.uv_block_cloud_score(uv_block, color_affinity)
            score += block_score * (uv_block['area'] / total_uv_area)

        return score

    def uv_island_samples(self, obj, island_count=3, samples_per_island=10, grid_size=20):
        start_time = self.perf_now()
        cache_key = (obj.name, 'uv_island_samples', grid_size, island_count, samples_per_island)
        cached_samples = self._uv_mask_cache.get(cache_key)
        if cached_samples is not None:
            self.perf_component_add(obj.name, 'uv_island_samples_cache_hit', self.perf_now() - start_time)
            return cached_samples

        uv_blocks = self.uv_blocks(obj, grid_size=grid_size)
        if not uv_blocks:
            self.perf_component_add(obj.name, 'uv_island_samples', self.perf_now() - start_time)
            return None

        islands = []
        for island_index, block in enumerate(uv_blocks[:island_count]):
            cells = sorted(block['cells'])
            if len(cells) == 0:
                continue

            points = numpy.array([
                ((x + 0.5) / grid_size, (y + 0.5) / grid_size)
                for x, y in cells
            ], dtype=numpy.float32)
            center = numpy.mean(points, axis=0)
            if len(points) > 1:
                covariance = numpy.cov((points - center).T)
                try:
                    eigenvalues, eigenvectors = numpy.linalg.eigh(covariance)
                    axis = eigenvectors[:, int(numpy.argmax(eigenvalues))]
                    projections = (points - center) @ axis
                    order = numpy.argsort(projections)
                    ordered_points = points[order]
                except Exception:
                    ordered_points = points
            else:
                ordered_points = points

            sample_count = min(samples_per_island, len(ordered_points))
            if sample_count <= 0:
                continue
            if len(ordered_points) == 1:
                selected_points = ordered_points
            else:
                indices = numpy.linspace(0, len(ordered_points) - 1, sample_count)
                selected_points = ordered_points[numpy.rint(indices).astype(numpy.int32)]

            islands.append({
                'index': island_index,
                'area': block['area'],
                'bbox': block['bbox'],
                'centroid': block['centroid'],
                'cells': sorted(block['cells']),
                'samples': [
                    {
                        'index': sample_index,
                        'uv': (float(point[0]), float(point[1])),
                    }
                    for sample_index, point in enumerate(selected_points)
                ],
            })

        if len(islands) == 0:
            self.perf_component_add(obj.name, 'uv_island_samples', self.perf_now() - start_time)
            return None

        self._uv_mask_cache[cache_key] = islands
        self.perf_component_add(obj.name, 'uv_island_samples', self.perf_now() - start_time)
        return islands

    def sample_image_rgb_at_uv(self, image, uv):
        start_time = self.perf_now()
        if image.size[0] <= 0 or image.size[1] <= 0:
            self.perf_texture_add(image.name, 'sample_rgb_invalid_image', self.perf_now() - start_time)
            return None

        width = int(image.size[0])
        height = int(image.size[1])
        u = uv[0] % 1.0
        v = uv[1] % 1.0
        x = min(width - 1, max(0, int(u * width)))
        y = min(height - 1, max(0, int(v * height)))
        offset = (y * width + x) * 4
        if offset + 2 >= len(image.pixels):
            self.perf_texture_add(image.name, 'sample_rgb', self.perf_now() - start_time)
            return None
        try:
            result = numpy.array([
                image.pixels[offset],
                image.pixels[offset + 1],
                image.pixels[offset + 2],
            ], dtype=numpy.float32)
        except Exception:
            self.perf_texture_add(image.name, 'sample_rgb_failed', self.perf_now() - start_time)
            return None
        self.perf_texture_add(image.name, 'sample_rgb', self.perf_now() - start_time)
        return result

    def texture_grid_samples(self, image, cells, grid_size=20):
        start_time = self.perf_now()
        cells = set(cells or [])
        if len(cells) == 0:
            self.perf_texture_add(image.name, f'texture_grid_samples_{grid_size}_empty', self.perf_now() - start_time)
            return {}

        if image.size[0] <= 0 or image.size[1] <= 0:
            self.perf_texture_add(image.name, f'texture_grid_samples_{grid_size}', self.perf_now() - start_time)
            return None

        cache_key = (image.name, 'texture_grid_samples', grid_size)
        cached_grid = self._texture_mask_cache.setdefault(cache_key, {})
        missing_cells = [cell for cell in cells if cell not in cached_grid]
        if len(missing_cells) == 0:
            self.perf_texture_add(image.name, f'texture_grid_samples_{grid_size}_cache_hit', self.perf_now() - start_time)
            return {
                cell: cached_grid[cell]
                for cell in cells
                if cell in cached_grid
            }

        scaled_image = None
        try:
            scaled_image = image.copy()
            scaled_image.scale(grid_size, grid_size)
            pixels = list(scaled_image.pixels)
            for x, y in missing_cells:
                if x < 0 or x >= grid_size or y < 0 or y >= grid_size:
                    continue
                uv = ((x + 0.5) / grid_size, (y + 0.5) / grid_size)
                offset = (y * grid_size + x) * 4
                color = numpy.array([
                    pixels[offset],
                    pixels[offset + 1],
                    pixels[offset + 2],
                ], dtype=numpy.float32)
                cached_grid[(x, y)] = {
                    'uv': uv,
                    'rgb': color,
                }
        except Exception:
            cached_grid = None
        finally:
            if scaled_image is not None:
                try:
                    bpy.data.images.remove(scaled_image)
                except Exception:
                    pass

        self.perf_texture_add(image.name, f'texture_grid_samples_{grid_size}', self.perf_now() - start_time)
        if cached_grid is None:
            return None
        return {
            cell: cached_grid[cell]
            for cell in cells
            if cell in cached_grid
        }

    def rgb_variance_score(self, colors):
        if colors is None or len(colors) < 2:
            return None

        color_array = numpy.array(colors, dtype=numpy.float32)
        if color_array.ndim != 2 or color_array.shape[0] < 2 or color_array.shape[1] != 3:
            return None

        channel_variance = numpy.var(color_array, axis=0)
        mean_variance = float(numpy.mean(channel_variance))
        score = 1.0 / (1.0 + mean_variance * 1000.0)
        return score, mean_variance, [float(value) for value in channel_variance]

    def score_texture_by_uv_samples(self, obj, image, islands):
        start_time = self.perf_now()
        grid_size = 20
        requested_cells = set()
        for island in islands:
            requested_cells.update(island.get('cells', []))

        texture_samples = self.texture_grid_samples(image, requested_cells, grid_size=grid_size)
        if texture_samples is None:
            self.perf_component_add(obj.name, 'score_texture_by_uv_samples', self.perf_now() - start_time)
            return None, []

        weighted_score_sum = 0.0
        total_sample_count = 0
        island_logs = []
        for island in islands:
            colors = []
            sample_logs = []
            island_cells = set(island.get('cells', []))
            selected_samples = [
                (cell, sample)
                for cell, sample in texture_samples.items()
                if cell in island_cells
            ]
            if len(selected_samples) == 0:
                fallback_cells = [
                    tuple([
                        min(grid_size - 1, max(0, int(sample['uv'][0] * grid_size))),
                        min(grid_size - 1, max(0, int(sample['uv'][1] * grid_size))),
                    ])
                    for sample in island.get('samples', [])
                ]
                selected_samples = [
                    (cell, texture_samples[cell])
                    for cell in fallback_cells
                    if cell in texture_samples
                ]

            for sample_index, (cell, sample) in enumerate(selected_samples):
                color = sample['rgb']
                if color is None:
                    continue
                colors.append(color)
                sample_logs.append({
                    'index': sample_index,
                    'cell': cell,
                    'uv': sample['uv'],
                    'rgb': [float(color[0]), float(color[1]), float(color[2])],
                })

            score_result = self.rgb_variance_score(colors)
            if score_result is None:
                continue
            score, mean_variance, channel_variance = score_result
            sample_count = len(sample_logs)
            weighted_score_sum += score * sample_count
            total_sample_count += sample_count
            island_logs.append({
                'index': island['index'],
                'area': island['area'],
                'bbox': island['bbox'],
                'centroid': island['centroid'],
                'score': score,
                'mean_variance': mean_variance,
                'channel_variance': channel_variance,
                'requested_sample_count': len(island_cells),
                'actual_sample_count': sample_count,
                'samples': sample_logs,
            })

        if total_sample_count == 0:
            self.perf_component_add(obj.name, 'score_texture_by_uv_samples', self.perf_now() - start_time)
            return None, island_logs

        result = weighted_score_sum / total_sample_count, island_logs
        self.perf_component_add(obj.name, 'score_texture_by_uv_samples', self.perf_now() - start_time)
        self.perf_texture_add(image.name, 'score_texture_by_uv_samples', self.perf_now() - start_time)
        return result

    def apply_diffuse_dedup(self):
        start_time = self.perf_now()
        min_dedup_uv_area = 6

        def texture_resolution_area(score_entry):
            size = score_entry.get('size')
            if isinstance(size, list) and len(size) == 2:
                return int(size[0]) * int(size[1])
            return 0

        def score_entry_colors(score_entry):
            colors = []
            for island in score_entry.get('islands', []):
                for sample in island.get('samples', []):
                    rgb = sample.get('rgb')
                    if isinstance(rgb, list) and len(rgb) == 3:
                        colors.append(rgb)
            if len(colors) == 0:
                return None
            return numpy.array(colors, dtype=numpy.float32)

        def score_entry_color_similarity(score_entry_a, score_entry_b):
            colors_a = score_entry_colors(score_entry_a)
            colors_b = score_entry_colors(score_entry_b)
            if colors_a is None or colors_b is None:
                return None
            sample_count = min(len(colors_a), len(colors_b))
            if sample_count == 0:
                return None
            colors_a = colors_a[:sample_count]
            colors_b = colors_b[:sample_count]
            mean_distance = float(numpy.mean(numpy.linalg.norm(colors_a - colors_b, axis=1)))
            return max(0.0, 1.0 - mean_distance / 1.7320508075688772)

        def ranked_score_entries(log_entry):
            score_entries = [
                score_entry
                for score_entry in log_entry.get('scores', [])
                if score_entry.get('score') is not None and score_entry.get('texture') is not None
            ]
            if len(score_entries) <= 1:
                return sorted(score_entries, key=lambda item: float(item.get('score')), reverse=True)

            score_ranked = sorted(score_entries, key=lambda item: float(item.get('score')), reverse=True)
            resolution_areas = [texture_resolution_area(score_entry) for score_entry in score_entries]
            if len(set(resolution_areas)) <= 1:
                log_entry['resolution_priority'] = {
                    'used': False,
                    'reason': 'same resolution',
                }
                return score_ranked

            best_score_entry = score_ranked[0]
            max_resolution_area = max(resolution_areas)
            high_resolution_entries = sorted(
                [
                    score_entry
                    for score_entry in score_entries
                    if texture_resolution_area(score_entry) == max_resolution_area
                ],
                key=lambda item: float(item.get('score')),
                reverse=True
            )
            best_high_resolution_entry = high_resolution_entries[0]

            similarity = score_entry_color_similarity(best_score_entry, best_high_resolution_entry)
            similarity_threshold = 0.60
            use_resolution_priority = (
                best_score_entry.get('texture') != best_high_resolution_entry.get('texture')
                and similarity is not None
                and similarity >= similarity_threshold
            )
            log_entry['resolution_priority'] = {
                'used': use_resolution_priority,
                'similarity': similarity,
                'threshold': similarity_threshold,
                'score_best': best_score_entry.get('texture'),
                'resolution_best': best_high_resolution_entry.get('texture'),
            }
            if not use_resolution_priority:
                return score_ranked

            high_resolution_hashes = {
                score_entry.get('texture')
                for score_entry in high_resolution_entries
            }
            return high_resolution_entries + [
                score_entry
                for score_entry in score_ranked
                if score_entry.get('texture') not in high_resolution_hashes
            ]

        component_uv_area = {}
        for log_entry in self._diffuse_match_logs.values():
            component_id = log_entry.get('component')
            if component_id is None:
                continue
            component_uv_area[int(component_id)] = sum([
                island.get('area', 0)
                for island in log_entry.get('islands', [])
                if isinstance(island.get('area'), int)
            ])

        ranked_entries_by_component = {}
        outline_texture_hashes = set()
        for component_key, log_entry in self._diffuse_match_logs.items():
            component_id = log_entry.get('component')
            if component_id is None:
                continue
            ranked_entries = ranked_score_entries(log_entry)
            ranked_entries_by_component[int(component_id)] = ranked_entries
            resolution_priority = log_entry.get('resolution_priority', {})
            if resolution_priority.get('used'):
                score_best = resolution_priority.get('score_best')
                resolution_best = resolution_priority.get('resolution_best')
                if score_best is not None and score_best != resolution_best:
                    outline_texture_hashes.add(score_best)

        entries = []
        for component_key, log_entry in self._diffuse_match_logs.items():
            component_id = log_entry.get('component')
            if component_id is not None and component_uv_area.get(int(component_id), 0) < min_dedup_uv_area:
                continue
            for rank_index, score_entry in enumerate(ranked_entries_by_component.get(int(component_id), [])):
                score = score_entry.get('score')
                texture_hash = score_entry.get('texture')
                if component_id is None or texture_hash is None or score is None:
                    continue
                if texture_hash in outline_texture_hashes:
                    continue
                entries.append((-rank_index, float(score), int(component_id), texture_hash))

        entries.sort(key=lambda item: (item[0], item[1], -item[2], item[3]), reverse=True)
        assigned_components = set()
        assigned_textures = set()
        final_by_component = {}
        for rank_priority, score, component_id, texture_hash in entries:
            if component_id in assigned_components or texture_hash in assigned_textures:
                continue
            final_by_component[component_id] = texture_hash
            assigned_components.add(component_id)
            assigned_textures.add(texture_hash)

        fallback_components = set()
        component_ids = set()
        for component_key, log_entry in self._diffuse_match_logs.items():
            component_id = log_entry.get('component')
            if component_id is not None:
                component_ids.add(int(component_id))
        for component_id in sorted(component_ids):
            if component_id in final_by_component:
                continue
            component_scores = [
                (-rank_index, float(score_entry.get('score')), score_entry.get('texture'))
                for log_entry in self._diffuse_match_logs.values()
                if log_entry.get('component') == component_id
                for rank_index, score_entry in enumerate(ranked_entries_by_component.get(int(component_id), []))
                if score_entry.get('score') is not None and score_entry.get('texture') is not None
                and score_entry.get('texture') not in outline_texture_hashes
            ]
            if len(component_scores) == 0:
                continue
            component_scores.sort(reverse=True)
            final_by_component[component_id] = component_scores[0][2]
            fallback_components.add(component_id)

        for component_key, log_entry in self._diffuse_match_logs.items():
            component_id = log_entry.get('component')
            selected = final_by_component.get(component_id)
            log_entry['pre_dedup_selected'] = log_entry.get('selected')
            log_entry['selected'] = selected
            log_entry['outline_textures'] = sorted(outline_texture_hashes)
            log_entry['dedup_uv_area'] = component_uv_area.get(int(component_id), 0) if component_id is not None else 0
            log_entry['dedup_eligible'] = log_entry['dedup_uv_area'] >= min_dedup_uv_area
            if component_id in fallback_components:
                if not log_entry['dedup_eligible']:
                    log_entry['reason'] = 'uv islands too small for global dedup, used conditional resolution/color similarity fallback'
                else:
                    log_entry['reason'] = 'all candidates were deduped, used conditional resolution/color similarity fallback'
            else:
                log_entry['reason'] = 'conditional resolution/color similarity after global texture dedup'
            for score_entry in log_entry.get('scores', []):
                texture_hash = score_entry.get('texture')
                score_entry['outline_removed'] = (
                    texture_hash in outline_texture_hashes
                    and texture_hash != selected
                    and score_entry.get('score') is not None
                )
                score_entry['dedup_removed'] = (
                    (
                        texture_hash in assigned_textures
                        or texture_hash in outline_texture_hashes
                    )
                    and texture_hash != selected
                    and score_entry.get('score') is not None
                )
        self.perf_add('stages', 'apply_diffuse_dedup', self.perf_now() - start_time)
        return final_by_component

    def write_diffuse_match_log(self, object_source_folder: Path):
        if not self._diffuse_match_logs:
            return
        log_path = object_source_folder / 'DiffuseTextureMatch.log'
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                log_data = {
                    'performance': self._performance_log,
                    'components': self._diffuse_match_logs,
                }
                f.write(json.dumps(log_data, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def choose_diffuse_by_uv(self, obj, object_source_folder: Path, component_id, shading_plan, max_candidates=None):
        start_time = self.perf_now()
        plan_candidates = shading_plan.get('candidates', {})
        assigned_hash = shading_plan.get('assignments', {}).get(component_id)
        candidates = []
        for texture_hash in plan_candidates.get(component_id, []):
            if texture_hash not in candidates:
                candidates.append(texture_hash)
        for texture_hash in plan_candidates.get(str(component_id), []):
            if texture_hash not in candidates:
                candidates.append(texture_hash)
        if assigned_hash and assigned_hash not in candidates:
            candidates.append(assigned_hash)

        if max_candidates is not None:
            candidates = candidates[:max_candidates]

        log_entry = {
            'component': component_id,
            'object': obj.name,
            'source_component': f'Component {component_id}',
            'candidates': candidates,
            'candidate_usage': {},
            'selected': None,
            'reason': '',
            'islands': [],
            'scores': [],
        }
        plan_usage = shading_plan.get('usage', {})
        for texture_hash in candidates:
            log_entry['candidate_usage'][texture_hash] = [
                {
                    'slot': usage.get('slot'),
                    'slot_id': usage.get('slot_id'),
                    'call_id': usage.get('call_id'),
                    'shader_type': usage.get('shader_type'),
                }
                for usage in plan_usage.get(component_id, {}).get(texture_hash, [])
            ]

        if len(candidates) == 0:
            log_entry['reason'] = 'no candidates'
            self._diffuse_match_logs[f'Component {component_id}'] = log_entry
            self.perf_component_add(obj.name, 'choose_diffuse_by_uv', self.perf_now() - start_time)
            return None

        islands = self.uv_island_samples(obj)
        if not islands:
            log_entry['reason'] = 'no uv islands'
            self._diffuse_match_logs[f'Component {component_id}'] = log_entry
            self.perf_component_add(obj.name, 'choose_diffuse_by_uv', self.perf_now() - start_time)
            return assigned_hash if assigned_hash else candidates[0]

        log_entry['islands'] = [
            {
                'index': island['index'],
                'area': island['area'],
                'bbox': island['bbox'],
                'centroid': island['centroid'],
                'cells': island.get('cells', []),
                'sample_uvs': [sample['uv'] for sample in island['samples']],
            }
            for island in islands
        ]

        file_by_hash = shading_plan.get('files', {})
        textures_map = shading_plan.get('textures', {})
        scored = []
        for texture_hash in candidates:
            texture_path = self.texture_file_for_hash(object_source_folder, texture_hash, file_by_hash)
            if texture_path is None:
                log_entry['scores'].append({
                    'texture': texture_hash,
                    'score': None,
                    'reason': 'missing texture file',
                })
                continue
            try:
                load_start_time = self.perf_now()
                image = bpy.data.images.load(str(texture_path), check_existing=True)
                image.alpha_mode = 'CHANNEL_PACKED'
                self.perf_texture_add(texture_hash, 'image_load', self.perf_now() - load_start_time)
            except Exception:
                self.perf_texture_add(texture_hash, 'image_load_failed', self.perf_now() - load_start_time)
                log_entry['scores'].append({
                    'texture': texture_hash,
                    'score': None,
                    'reason': 'failed to load image',
                })
                continue

            score, island_logs = self.score_texture_by_uv_samples(obj, image, islands)
            if score is None:
                log_entry['scores'].append({
                    'texture': texture_hash,
                    'score': None,
                    'reason': 'failed to score samples',
                    'file': str(texture_path),
                    'islands': island_logs,
                })
                continue
            log_entry['scores'].append({
                'texture': texture_hash,
                'score': score,
                'file': str(texture_path),
                'size': self.texture_size(textures_map.get(texture_hash, {})),
                'islands': island_logs,
            })
            scored.append((score, texture_hash))

        if not scored:
            diffuse_hash = assigned_hash if assigned_hash else candidates[0]
            log_entry['selected'] = diffuse_hash
            log_entry['reason'] = 'no scored candidates, used fallback'
            self._diffuse_match_logs[f'Component {component_id}'] = log_entry
            self.perf_component_add(obj.name, 'choose_diffuse_by_uv', self.perf_now() - start_time)
            return diffuse_hash

        scored.sort(reverse=True)
        best_score, best_hash = scored[0]
        log_entry['selected'] = best_hash
        log_entry['reason'] = 'highest average island RGB variance score before global dedup'
        if len(scored) > 1:
            log_entry['best_gap'] = best_score - scored[1][0]
        self._diffuse_match_logs[f'Component {component_id}'] = log_entry
        self.perf_component_add(obj.name, 'choose_diffuse_by_uv', self.perf_now() - start_time)
        return best_hash

    def apply_auto_diffuse_material(self, obj, object_source_folder: Path, component_name: str, shading_plan, diffuse_hash_override=None):
        start_time = self.perf_now()
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

        diffuse_hash = diffuse_hash_override
        if diffuse_hash is None:
            diffuse_hash = assignments.get(component_id)
        if diffuse_hash is not None:
            texture_path = self.texture_file_for_hash(object_source_folder, diffuse_hash, file_by_hash)
            if texture_path is not None and Path(texture_path).is_file():
                load_start_time = self.perf_now()
                image = bpy.data.images.load(str(texture_path), check_existing=True)
                image.alpha_mode = 'CHANNEL_PACKED'
                self.perf_texture_add(diffuse_hash, 'material_image_load', self.perf_now() - load_start_time)
                image_texture = self.create_image_node(nodes, image, x_base, y_base, 'Diffuse Texture')
                links.new(image_texture.outputs['Color'], principled.inputs['Base Color'])
        else:
            conflict_hashes = conflicts.get(component_id, [])
            if len(conflict_hashes) == 1:
                texture_hash = conflict_hashes[0]
                texture_path = self.texture_file_for_hash(object_source_folder, texture_hash, file_by_hash)
                if texture_path is not None and Path(texture_path).is_file():
                    image = bpy.data.images.load(str(texture_path), check_existing=True)
                    image.alpha_mode = 'CHANNEL_PACKED'
                    image_texture = self.create_image_node(nodes, image, x_base, y_base, 'Diffuse Texture (Fallback)')
                    links.new(image_texture.outputs['Color'], principled.inputs['Base Color'])
                    conflict_hashes = []

            x_conflict = x_base - 350
            for index, texture_hash in enumerate(conflict_hashes):
                texture_path = self.texture_file_for_hash(object_source_folder, texture_hash, file_by_hash)
                if texture_path is None or not Path(texture_path).is_file():
                    continue
                image = bpy.data.images.load(str(texture_path), check_existing=True)
                image.alpha_mode = 'CHANNEL_PACKED'
                self.create_image_node(nodes, image, x_conflict, y_base - index * 280, f'Conflict {texture_hash}')

        if len(obj.data.materials) == 0:
            obj.data.materials.append(material)
        else:
            obj.data.materials[0] = material
        self.perf_component_add(component_name, 'apply_auto_diffuse_material', self.perf_now() - start_time)

    def import_object(self, operator, context, cfg):
        total_start_time = self.perf_now()

        object_source_folder = resolve_path(cfg.object_source_folder)
        read_start_time = self.perf_now()
        texture_usage = self.read_texture_usage(object_source_folder)
        texture_format = self.read_texture_format(object_source_folder)
        shading_filter_hashes = self.parse_shading_filter_hashes(cfg)
        self.perf_add('stages', 'read_texture_metadata', self.perf_now() - read_start_time)
        shading_plan = self.build_shading_plan(texture_format, shading_filter_hashes)

        if not object_source_folder.is_dir():
            raise ConfigError('object_source_folder', 'Specified sources folder does not exist!')

        start_time = time.time()
        print(f"Object import started for '{object_source_folder.stem}' folder")

        imported_components = []
        import_components_start_time = self.perf_now()
        
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

            imported_components.append((obj, fmt_path.stem))

        self.perf_add('stages', 'import_components', self.perf_now() - import_components_start_time)
        
        if len(imported_components) == 0:
            raise ConfigError('object_source_folder', 'Specified folder is missing .fmt files for components!')

        selected_diffuse_by_component = {}
        score_start_time = self.perf_now()
        for obj, component_name in imported_components:
            component_id = self.parse_component_id(component_name)
            if component_id is None:
                continue
            self.choose_diffuse_by_uv(obj, object_source_folder, component_id, shading_plan)
        self.perf_add('stages', 'score_diffuse_candidates', self.perf_now() - score_start_time)

        selected_diffuse_by_component = self.apply_diffuse_dedup()
        material_start_time = self.perf_now()
        for obj, component_name in imported_components:
            component_id = self.parse_component_id(component_name)
            if component_id is None:
                continue
            self.apply_auto_diffuse_material(
                obj,
                object_source_folder,
                component_name,
                shading_plan,
                diffuse_hash_override=selected_diffuse_by_component.get(component_id)
            )
        self.perf_add('stages', 'apply_diffuse_materials', self.perf_now() - material_start_time)

        col = new_collection(object_source_folder.stem)
        link_start_time = self.perf_now()
        for obj, component_name in imported_components:
            link_object_to_collection(obj, col)
            if cfg.skip_empty_vertex_groups and cfg.import_skeleton_type == 'MERGED':
                remove_unused_vertex_groups(context, obj)
        self.perf_add('stages', 'link_objects', self.perf_now() - link_start_time)

        self.perf_add('stages', 'total_before_log_write', self.perf_now() - total_start_time)
        self.write_diffuse_match_log(object_source_folder)

        print(f'Total import time: {time.time() - start_time :.3f}s')

    def import_component(self, operator, context, cfg, fmt_path: Path, ib_path: Path, vb_path: Path, axis_forward='Y', axis_up='Z', texture_usage=None, texture_format=None, shading_filter_hashes=None, shading_plan=None):

        start_time = time.time()
        perf_start_time = self.perf_now()

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

            num_shapekeys = 0 if obj.data.shape_keys is None else len(getattr(obj.data.shape_keys, 'key_blocks', []))

            print(f'{fmt_path.stem} import time: {time.time()-start_time :.3f}s ({len(obj.data.vertices)} vertices, {len(obj.data.loops)} indices, {num_shapekeys} shapekeys)')
            self.perf_component_add(fmt_path.stem, 'import_component', self.perf_now() - perf_start_time)

            return obj


def blender_import(operator, context, cfg):
    object_importer = ObjectImporter()
    object_importer.import_object(operator, context, cfg)
