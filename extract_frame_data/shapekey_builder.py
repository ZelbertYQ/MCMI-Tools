from dataclasses import dataclass, field

from typing import List, Dict

from ..migoto_io.data_model.dxgi_format import DXGIFormat
from ..migoto_io.data_model.byte_buffer import ByteBuffer, BufferLayout, BufferSemantic, AbstractSemantic, Semantic

from .data_extractor import ShapeKeyData, DrawData


@dataclass
class ShapeKeys:
    offsets_hash: str
    scale_hash: str = ''
    dispatch_y: int = 0
    shapekey_offsets: list = field(default_factory=lambda: [])
    # Sum of first 4 raw cb0 uint values (cb0[0].xyzw), used by ShapeKeyOverrider checksum
    cb0_checksum: int = 0
    # ShapeKey ID based indexed list of {VertexID: VertexOffsets}
    shapekeys_index: List[Dict[int, List[float]]] = field(default_factory=lambda: [])
    # Vertex ID based indexed dict of {ShapeKeyID: VertexOffsets}
    indexed_shapekeys: Dict[int, Dict[int, List[float]]] = field(default_factory=lambda: {})

    def get_shapekey_ids(self, vertex_offset, vertex_count):
        """
        Returns sorted list of shapekey ids applied to provided range of vertices
        """
        shapekey_ids = []
        for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
            shapekeys = self.indexed_shapekeys.get(vertex_id, None)
            if shapekeys is None:
                continue
            for shapekey_id in shapekeys.keys():
                if shapekey_id not in shapekey_ids:
                    shapekey_ids.append(shapekey_id)
        shapekey_ids.sort()
        return shapekey_ids

    def build_shapekey_buffer(self, vertex_offset, vertex_count):
        """
        Returns Blender-importable ByteBuffer for shapekeys within provided range of vertices
        """
        shapekey_ids = self.get_shapekey_ids(vertex_offset, vertex_count)

        if len(shapekey_ids) == 0:
            return None

        layout = BufferLayout([
            BufferSemantic(AbstractSemantic(Semantic.ShapeKey, shapekey_id), DXGIFormat.R16G16B16_FLOAT)
            for shapekey_id in shapekey_ids
        ])

        shapekey_buffer = ByteBuffer(layout)
        shapekey_buffer.extend(vertex_count)

        for vertex_id in range(vertex_offset, vertex_offset + vertex_count):
            indexed_vertex_shapekeys = self.indexed_shapekeys.get(vertex_id, None)
            element_id = vertex_id - vertex_offset
            for semantic in shapekey_buffer.layout.semantics:
                shapekey_id = semantic.abstract.index
                if indexed_vertex_shapekeys is None or shapekey_id not in indexed_vertex_shapekeys:
                    shapekey_buffer.get_element(element_id).set_value(semantic, [0, 0, 0])
                else:
                    shapekey_buffer.get_element(element_id).set_value(semantic, indexed_vertex_shapekeys[shapekey_id])

        return shapekey_buffer


@dataclass
class ShapeKeyBuilder:
    # Input: Dict mapping output hash -> list of ShapeKeyData (one per CS_1 batch)
    shapekey_data: Dict[str, list]
    # Output
    shapekeys: Dict[str, ShapeKeys] = field(init=False)

    def __post_init__(self):
        self.shapekeys = {}

        for shapekey_hash, shapekey_data_list in self.shapekey_data.items():

            # Parse cb0 from each batch and sort by base_offset (cb0[65].y)
            batches = []
            for sd in shapekey_data_list:
                all_cb0_values = sd.shapekey_offset_buffer.get_values(AbstractSemantic(Semantic.RawData))
                # cb0[65].y stores the base offset for this batch (0 for the first batch)
                base_offset = all_cb0_values[65 * 4 + 1]
                batches.append((base_offset, sd, all_cb0_values))

            batches.sort(key=lambda x: x[0])

            # Build combined offset list by merging all batches
            # Each batch's cb0 rows 0-31 contain 128 uint offset values
            # Offsets index into the shared t0/t1 buffers
            # Non-first batches need their offsets shifted by base_offset
            combined_offsets = []
            for batch_idx, (base_offset, sd, all_cb0_values) in enumerate(batches):
                raw_offsets = list(all_cb0_values[0:128])

                # Find end of strictly monotonic region (padding starts where values stop increasing)
                mono_count = 1
                for i in range(1, 128):
                    if raw_offsets[i] > raw_offsets[i - 1]:
                        mono_count += 1
                    else:
                        break

                if batch_idx == 0:
                    # First batch: use offsets directly
                    combined_offsets.extend(raw_offsets[:mono_count])
                else:
                    # Subsequent batches: shift by base_offset, skip first value (shared boundary)
                    for i in range(1, mono_count):
                        combined_offsets.append(base_offset + raw_offsets[i])

            if len(batches) > 1:
                print(f'ShapeKey multi-batch merge: {len(batches)} batches, {len(combined_offsets)} offsets, '
                      f'{len(combined_offsets) - 1} shapekeys')

            # Checksum from first batch's cb0[0:4]
            first_cb0 = batches[0][2]
            cb0_checksum = sum(first_cb0[0:4])

            # Use first batch's t0/t1 buffers (shared across batches, same resource hash)
            first_sd = batches[0][1]
            vertex_ids = first_sd.shapekey_vertex_id_buffer.get_values(AbstractSemantic(Semantic.RawData))
            vertex_offsets = first_sd.shapekey_vertex_offset_buffer.get_values(AbstractSemantic(Semantic.RawData))

            # R32 vertex IDs and R16G16B16_FLOAT offsets are parallel arrays.
            # Keep processing inside the shortest valid range.
            max_entries = min(len(vertex_ids), len(vertex_offsets) // 6)

            shapekey_offsets = combined_offsets

            # Defensive clamp: malformed/misdetected offsets must not index outside buffers.
            if shapekey_offsets:
                clamped_offsets = []
                prev = 0
                for off in shapekey_offsets:
                    safe_off = max(0, min(off, max_entries))
                    if safe_off < prev:
                        safe_off = prev
                    clamped_offsets.append(safe_off)
                    prev = safe_off
                shapekey_offsets = clamped_offsets

            last_data_entry_id = shapekey_offsets[-1]

            # Process shapekey entries, we'll build both VertexID and ShapeKeyID based outputs for fast indexing
            shapekeys_index = []
            indexed_shapekeys = {}
            for shapekey_id, first_entry_id in enumerate(shapekey_offsets):
                # Stop processing if next entries have no data
                if first_entry_id >= last_data_entry_id:
                    break
                # Guard: last element has no successor to read the next offset from
                if shapekey_id + 1 >= len(shapekey_offsets):
                    break
                # Process all entries from current shapekey offset 'till offset of the next shapekey
                entries = {}
                for entry_id in range(first_entry_id, shapekey_offsets[shapekey_id + 1]):
                    if entry_id >= max_entries:
                        break
                    vertex_id = vertex_ids[entry_id]
                    vertex_offset = vertex_offsets[entry_id * 6:entry_id * 6 + 3]
                    entries[vertex_id] = vertex_offset
                    if vertex_id not in indexed_shapekeys:
                        indexed_shapekeys[vertex_id] = {}
                    indexed_shapekeys[vertex_id][shapekey_id] = vertex_offset
                shapekeys_index.append(entries)

            self.shapekeys[shapekey_hash] = ShapeKeys(
                offsets_hash=first_sd.shapekey_hash,
                scale_hash=first_sd.shapekey_scale_hash,
                dispatch_y=sum(sd.dispatch_y for _, sd, _ in batches),
                shapekey_offsets=shapekey_offsets,
                cb0_checksum=cb0_checksum,
                shapekeys_index=shapekeys_index,
                indexed_shapekeys=indexed_shapekeys,
            )
