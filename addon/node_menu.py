import bpy


MENU_CALLBACK_ATTR = '_mcmi_transfer_slot_texture_links_menu'


def _get_node_tree(context):
    space = getattr(context, 'space_data', None)
    if space is not None:
        node_tree = getattr(space, 'edit_tree', None) or getattr(space, 'node_tree', None)
        if node_tree is not None:
            return node_tree
    active_node = getattr(context, 'active_node', None)
    return getattr(active_node, 'id_data', None)


def _get_selected_image_nodes(context, node_tree):
    selected = getattr(context, 'selected_nodes', None)
    if selected is None:
        selected = [node for node in node_tree.nodes if getattr(node, 'select', False)]
    return [node for node in selected if getattr(node, 'type', None) == 'TEX_IMAGE']


def _is_slot_group_link(link):
    to_node = getattr(link, 'to_node', None)
    to_socket = getattr(link, 'to_socket', None)
    if to_node is None or to_socket is None:
        return False
    if getattr(to_node, 'type', None) != 'GROUP':
        return False
    socket_name = getattr(to_socket, 'name', '')
    return socket_name.startswith('ps-t')


def _get_transferable_links(node_tree, node):
    return [
        link for link in node_tree.links
        if link.from_node == node and _is_slot_group_link(link)
    ]


def _get_socket_by_name(sockets, socket_name):
    for socket in sockets:
        if socket.name == socket_name:
            return socket
    return None


def _copy_attr_if_present(target, source, attr_name):
    if not hasattr(target, attr_name) or not hasattr(source, attr_name):
        return
    try:
        setattr(target, attr_name, getattr(source, attr_name))
    except Exception:
        pass


def _copy_image_color_settings(target_image, source_image):
    if target_image is None or source_image is None:
        return

    source_colorspace = getattr(getattr(source_image, 'colorspace_settings', None), 'name', None)
    if source_colorspace:
        try:
            target_image.colorspace_settings.name = source_colorspace
        except Exception:
            pass

    _copy_attr_if_present(target_image, source_image, 'alpha_mode')
    _copy_attr_if_present(target_image, source_image, 'use_half_precision')
    _copy_attr_if_present(target_image, source_image, 'use_view_as_render')


def _copy_texture_node_settings(target_node, source_node):
    for attr_name in ('extension', 'interpolation', 'projection', 'projection_blend'):
        _copy_attr_if_present(target_node, source_node, attr_name)

    source_user = getattr(source_node, 'image_user', None)
    target_user = getattr(target_node, 'image_user', None)
    if source_user is not None and target_user is not None:
        for attr_name in (
            'frame_current',
            'frame_duration',
            'frame_offset',
            'frame_start',
            'use_auto_refresh',
            'use_cyclic',
        ):
            _copy_attr_if_present(target_user, source_user, attr_name)

    _copy_image_color_settings(getattr(target_node, 'image', None), getattr(source_node, 'image', None))


def _swap_node_positions(lhs, rhs):
    lhs_location = lhs.location.copy()
    rhs_location = rhs.location.copy()
    lhs.location = rhs_location
    rhs.location = lhs_location


class MCMI_OT_TransferSlotTextureLinks(bpy.types.Operator):
    bl_idname = 'mcmi_tools.transfer_slot_texture_links'
    bl_label = 'Transfer Slot Links To Selected Image'
    bl_description = 'Move slot material links from the linked image texture node to the other selected image texture node'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node_tree = _get_node_tree(context)
        if node_tree is None:
            return False
        return len(_get_selected_image_nodes(context, node_tree)) == 2

    def execute(self, context):
        node_tree = _get_node_tree(context)
        image_nodes = _get_selected_image_nodes(context, node_tree)
        if len(image_nodes) != 2:
            self.report({'ERROR'}, 'Select exactly two Image Texture nodes.')
            return {'CANCELLED'}

        active_node = getattr(context, 'active_node', None) or getattr(node_tree.nodes, 'active', None)
        links_by_node = {node: _get_transferable_links(node_tree, node) for node in image_nodes}
        linked_nodes = [node for node in image_nodes if links_by_node[node]]

        if len(linked_nodes) == 1:
            source_node = linked_nodes[0]
            target_node = image_nodes[0] if image_nodes[1] == source_node else image_nodes[1]
        elif len(linked_nodes) == 2:
            if active_node not in image_nodes:
                self.report({'ERROR'}, 'Both selected image nodes have slot links. Right-click the target image node.')
                return {'CANCELLED'}
            target_node = active_node
            source_node = image_nodes[0] if image_nodes[1] == target_node else image_nodes[1]
        else:
            self.report({'ERROR'}, 'One selected image node must already be linked to slot group inputs.')
            return {'CANCELLED'}

        transfer_plan = []
        for link in links_by_node[source_node]:
            target_socket = _get_socket_by_name(target_node.outputs, link.from_socket.name)
            if target_socket is None:
                continue
            transfer_plan.append((link.to_socket, target_socket))

        if not transfer_plan:
            self.report({'ERROR'}, 'No compatible Color/Alpha slot links found to transfer.')
            return {'CANCELLED'}

        _copy_texture_node_settings(target_node, source_node)
        _swap_node_positions(source_node, target_node)

        destination_sockets = {to_socket for to_socket, _target_socket in transfer_plan}
        for link in list(node_tree.links):
            if link.to_socket in destination_sockets:
                node_tree.links.remove(link)

        transferred = 0
        for to_socket, target_socket in transfer_plan:
            node_tree.links.new(target_socket, to_socket)
            transferred += 1

        source_node.mute = True
        target_node.mute = False

        self.report({'INFO'}, f'Transferred {transferred} slot links.')
        return {'FINISHED'}


def draw_node_context_menu(self, context):
    if MCMI_OT_TransferSlotTextureLinks.poll(context):
        self.layout.separator()
        self.layout.operator(
            MCMI_OT_TransferSlotTextureLinks.bl_idname,
            text='Transfer Slot Links To Selected Image',
            icon='NODE_TEXTURE',
        )


def register():
    menu = bpy.types.NODE_MT_context_menu
    old_callback = getattr(menu, MENU_CALLBACK_ATTR, None)
    if old_callback is not None:
        try:
            menu.remove(old_callback)
        except Exception:
            pass
    menu.append(draw_node_context_menu)
    setattr(menu, MENU_CALLBACK_ATTR, draw_node_context_menu)


def unregister():
    menu = bpy.types.NODE_MT_context_menu
    callback = getattr(menu, MENU_CALLBACK_ATTR, None)
    if callback is not None:
        try:
            menu.remove(callback)
        except Exception:
            pass
        try:
            delattr(menu, MENU_CALLBACK_ATTR)
        except Exception:
            pass
