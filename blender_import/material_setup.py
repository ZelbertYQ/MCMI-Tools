import json
import bpy

from pathlib import Path

TEXTURE_EXTENSIONS = {'.dds', '.jpg', '.jpeg', '.png', '.tga', '.tif', '.tiff', '.webp', '.bmp'}


def setup_materials(object_source_folder, imported_objects):
    shader_usage_path = Path(object_source_folder) / 'ShaderTextureUsage.json'
    if not shader_usage_path.is_file():
        print(f"Warning: No ShaderTextureUsage.json found in '{object_source_folder}', skipping slot material setup")
        return

    with open(shader_usage_path, 'r', encoding='utf-8') as f:
        shader_usage = json.load(f)

    texture_index = _build_texture_file_index(Path(object_source_folder))
    for component_i, obj in enumerate(imported_objects):
        if obj is None:
            continue
        component_key = f'Component {component_i}'
        if component_key not in shader_usage:
            print(f"Warning: '{component_key}' not found in ShaderTextureUsage.json, skipping material setup")
            continue
        _create_component_material(obj, component_key, object_source_folder, shader_usage[component_key], {}, texture_index)

    print(f"Slot material setup completed for {len(imported_objects)} components")


def _create_component_material(obj, component_key, object_source_folder, vs_ps_data, node_group_cache, texture_index):
    mat = bpy.data.materials.new(component_key)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output_node = nodes.new('ShaderNodeOutputMaterial')
    output_node.location = (2000, 0)

    ng_start_x = 500
    ng_dx = 500
    ng_dy = 500
    texture_start_x = 0
    texture_spacing_x = 300
    texture_spacing_y = 300
    texture_col_size = 8

    ng_index = 0
    tex_index = 0
    tex_nodes_by_file = {}
    for vs_key, ps_dict in vs_ps_data.items():
        for ps_key, texture_slots in ps_dict.items():
            ng_name = f'{vs_key}-{ps_key}'
            ng = _get_or_create_node_group(ng_name, texture_slots, node_group_cache)

            ng_node = nodes.new('ShaderNodeGroup')
            ng_node.node_tree = ng
            ng_node.name = ng_name
            ng_node.mute = False
            ng_node.location = (ng_start_x + ng_index * ng_dx, -(ng_index * ng_dy))
            if hasattr(ng_node, 'width'):
                ng_node.width = 400

            sorted_slots = sorted(texture_slots.keys(), key=_slot_sort_key)
            for slot_key in sorted_slots:
                texture_info = texture_slots[slot_key]
                filename = texture_info.get('filename', '')
                file_key = filename if filename else f'{ng_name}_{slot_key}'

                if file_key in tex_nodes_by_file:
                    tex_node = tex_nodes_by_file[file_key]
                else:
                    col = tex_index // texture_col_size
                    row = tex_index % texture_col_size
                    tex_node = nodes.new('ShaderNodeTexImage')
                    tex_node.name = filename if filename else f'{ng_name}_{slot_key}'
                    tex_node.label = tex_node.name
                    tex_node.location = (texture_start_x - col * texture_spacing_x, -(row * texture_spacing_y))

                    if filename:
                        texture_file = _resolve_texture_file(object_source_folder, filename, texture_index)
                        tex_node.mute = texture_file['disabled'] if texture_file is not None else True
                        if texture_file is not None and texture_file['path'].is_file():
                            img = _load_texture_image(texture_file['path'])
                            if img is not None:
                                tex_node.image = img
                                tex_node.label = texture_file['relative']
                                fmt = texture_info.get('format', '')
                                img.colorspace_settings.name = 'sRGB' if 'SRGB' in fmt.upper() else 'Non-Color'
                                img.alpha_mode = 'CHANNEL_PACKED'
                    else:
                        tex_node.mute = True

                    tex_nodes_by_file[file_key] = tex_node
                    tex_index += 1

                links.new(tex_node.outputs['Color'], _socket_by_name(ng_node.inputs, slot_key))
                links.new(tex_node.outputs['Alpha'], _socket_by_name(ng_node.inputs, f'{slot_key} alpha'))

            ng_index += 1

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def _build_texture_file_index(object_source_folder):
    object_source_folder = Path(object_source_folder)
    index = {}
    if not object_source_folder.is_dir():
        return index

    for path in sorted(object_source_folder.rglob('*'), key=lambda item: item.as_posix().lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXTURE_EXTENSIONS:
            continue

        relative_path = path.relative_to(object_source_folder)
        disabled = _is_disabled_relative_path(relative_path)
        key = path.name.lower()
        candidate = {
            'path': path,
            'relative': relative_path.as_posix(),
            'disabled': disabled,
            'depth': len(relative_path.parts),
        }
        existing = index.get(key)
        if existing is None or _texture_candidate_priority(candidate) < _texture_candidate_priority(existing):
            index[key] = candidate

    return index


def _resolve_texture_file(object_source_folder, filename, texture_index):
    object_source_folder = Path(object_source_folder)
    relative_filename = Path(filename)
    direct_path = object_source_folder / relative_filename
    if direct_path.is_file():
        relative_path = direct_path.relative_to(object_source_folder)
        return {
            'path': direct_path,
            'relative': relative_path.as_posix(),
            'disabled': _is_disabled_relative_path(relative_path),
            'depth': len(relative_path.parts),
        }

    indexed = texture_index.get(relative_filename.name.lower())
    if indexed is not None:
        return indexed

    print(f"Warning: Texture '{filename}' was not found in '{object_source_folder}' or its subfolders")
    return None


def _texture_candidate_priority(candidate):
    return (
        1 if candidate['disabled'] else 0,
        candidate['depth'],
        candidate['relative'].lower(),
    )


def _is_disabled_relative_path(relative_path):
    parts = relative_path.parts[:-1]
    return any(part.lower().startswith('disabled') for part in parts)


def _slot_sort_key(slot_key):
    try:
        return int(slot_key.split('-t')[-1])
    except Exception:
        return 999


def _socket_by_name(sockets, name):
    for socket in sockets:
        if socket.name == name:
            return socket
    raise KeyError(f"Socket '{name}' not found")


def _load_texture_image(img_path):
    path_str = str(img_path)
    normalized_path = str(img_path.resolve()).lower()

    for image in list(bpy.data.images):
        image_path = bpy.path.abspath(getattr(image, 'filepath', '') or getattr(image, 'filepath_raw', ''))
        if image_path and str(Path(image_path).resolve()).lower() == normalized_path and image.users == 0:
            bpy.data.images.remove(image)

    try:
        img = bpy.data.images.load(path_str, check_existing=False)
        try:
            img.reload()
        except Exception:
            pass
        return img
    except Exception as e:
        print(f"Warning: Failed to load texture image '{img_path}': {e}")
        return None


def _get_or_create_node_group(ng_name, texture_slots, node_group_cache):
    if ng_name in node_group_cache:
        return node_group_cache[ng_name]

    ng = bpy.data.node_groups.new(ng_name, 'ShaderNodeTree')
    node_group_cache[ng_name] = ng
    sorted_slots = sorted(texture_slots.keys(), key=_slot_sort_key)

    for slot_key in sorted_slots:
        ng.interface.new_socket(slot_key, socket_type='NodeSocketColor', in_out='INPUT')
        ng.interface.new_socket(f'{slot_key} alpha', socket_type='NodeSocketFloat', in_out='INPUT')
        ng.interface.new_socket(slot_key, socket_type='NodeSocketColor', in_out='OUTPUT')
        ng.interface.new_socket(f'{slot_key} alpha', socket_type='NodeSocketFloat', in_out='OUTPUT')

    input_node = ng.nodes.new('NodeGroupInput')
    input_node.location = (-400, 0)

    output_node = ng.nodes.new('NodeGroupOutput')
    output_node.location = (400, 0)

    for slot_key in sorted_slots:
        ng.links.new(_socket_by_name(input_node.outputs, slot_key), _socket_by_name(output_node.inputs, slot_key))
        ng.links.new(_socket_by_name(input_node.outputs, f'{slot_key} alpha'), _socket_by_name(output_node.inputs, f'{slot_key} alpha'))

    return ng
