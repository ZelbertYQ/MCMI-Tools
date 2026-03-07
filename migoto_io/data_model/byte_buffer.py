import io
import copy
import textwrap
import math
import numpy

from typing import Union, List, Optional, Dict
from dataclasses import dataclass
from enum import Enum

from .dxgi_format import *


class Semantic(Enum):
    VertexId = 'VERTEXID'
    Index = 'INDEX'
    Tangent = 'TANGENT'
    BitangentSign = 'BITANGENTSIGN'
    Normal = 'NORMAL'
    TexCoord = 'TEXCOORD'
    Color = 'COLOR'
    Position = 'POSITION'
    Blendindices = 'BLENDINDICES'
    Blendweight = 'BLENDWEIGHT'
    ShapeKey = 'SHAPEKEY'
    RawData = 'RAWDATA'

    def __str__(self):
        return f'{self.value}'

    def __repr__(self):
        return f'{self.value}'


@dataclass
class AbstractSemantic:
    enum: Semantic
    index: int = 0

    def __init__(self, semantic, semantic_index=0):
        self.enum = semantic
        self.index = semantic_index

    def __hash__(self):
        return hash((self.enum, self.index))

    def __str__(self):
        return f'{self.enum}_{self.index}'

    def __repr__(self):
        return f'{self.enum}_{self.index}'
    
    def get_name(self):
        name = self.enum.value
        if self.index > 0:
            name += str(self.index)
        if self.enum == Semantic.TexCoord:
            name += '.xy'
        return name


@dataclass
class BufferSemantic:
    abstract: AbstractSemantic
    format: DXGIFormat
    stride: int = 0
    offset: int = 0
    name: Optional[str] = None
    extract_format: Optional[DXGIFormat] = None

    def __post_init__(self):
        # Calculate byte stride
        if self.stride == 0:
            self.stride = self.format.byte_width
        
    def __hash__(self):
        return hash((self.abstract, self.format.format, self.stride, self.offset))

    def __repr__(self):
        return f'{self.abstract} ({self.format.format} stride={self.stride} offset={self.offset})'

    def to_string(self, indent=2):
        return textwrap.indent(textwrap.dedent(f'''
            SemanticName: {self.abstract.enum}
            SemanticIndex: {self.abstract.index}
            Format: {self.format.format}
            InputSlot: 0
            AlignedByteOffset: {self.offset}
            InputSlotClass: per-vertex
            InstanceDataStepRate: 0
        ''').lstrip(), ' ' * indent)

    def get_format(self):
        return self.format.get_format()

    def get_name(self):
        return self.name if self.name else self.abstract.get_name()

    def get_num_values(self):
        return self.format.get_num_values(self.stride)

    def get_numpy_type(self):
        return self.format.get_numpy_type(self.stride)
    

@dataclass
class BufferLayout:
    semantics: List[BufferSemantic]
    stride: int = 0
    force_stride: bool = False

    def __post_init__(self):
        # Autofill byte Stride and Offsets
        if self.stride == 0:
            # Calculate byte stride
            for element in self.semantics:
                self.stride += element.stride
            # Calculate byte offsets
            offset = 0
            for element in self.semantics:
                element.offset = offset
                offset += element.stride
        # Autofill Semantic Index
        groups = {}
        for semantic in self.semantics:
            if semantic not in groups:
                groups[semantic] = 0
                continue
            if semantic.abstract.index == 0:
                groups[semantic] += 1
                semantic.abstract.index = groups[semantic]

    def get_element(self, abstract: AbstractSemantic):
        for element in self.semantics:
            if abstract == element.abstract:
                return element

    def add_element(self, semantic: BufferSemantic):
        if self.get_element(semantic.abstract) is not None:
            return
        semantic = copy.deepcopy(semantic)
        semantic.offset = self.stride
        self.semantics.append(semantic)
        self.stride += semantic.stride

    def merge(self, layout):
        for semantic in layout.semantics:
            if not self.get_element(semantic):
                self.add_element(semantic)

    def to_string(self):
        ret = ''
        for i, semantic in enumerate(self.semantics):
            ret += 'element[%i]:\n' % i
            ret += semantic.to_string()
        return ret

    def get_numpy_type(self):
        dtype = numpy.dtype([])
        for semantic in self.semantics:
            dtype = numpy.dtype(dtype.descr + [(semantic.abstract.get_name(), (semantic.get_numpy_type()))])
        return dtype
    

class NumpyBuffer:
    layout: BufferLayout
    data: numpy.ndarray

    def __init__(self, layout: BufferLayout, data: Optional[numpy.ndarray] = None, size = 0):
        self.set_layout(layout)
        self.set_data(data, size)

    def set_layout(self, layout: BufferLayout):
        self.layout = layout

    def set_data(self, data: Optional[numpy.ndarray], size = 0):
        if data is not None:
            self.data = data
        elif size > 0:
            self.data = numpy.zeros(size, dtype=self.layout.get_numpy_type())

    def set_field(self, field: str, data: Optional[numpy.ndarray]):
        self.data[field] = data

    def get_data(self, indices: Optional[numpy.ndarray] = None) -> numpy.ndarray:
        if indices is None:
            return self.data
        else:
            return self.data[indices]

    def get_field(self, field: Union[str, int]) -> numpy.ndarray:
        if isinstance(field, int):
            field = self.layout.semantics[field].get_name()
        return self.data[field]

    def remove_duplicates(self, keep_order = True):
        if keep_order:
            _, unique_index = numpy.unique(self.data, return_index=True)
            self.data = self.data[numpy.sort(unique_index)]
        else:
            self.data = numpy.unique(self.data)

    def import_semantic_data(self,
                             data: numpy.ndarray, 
                             semantic: Union[BufferSemantic, int], 
                             semantic_converters: Optional[List[callable]] = None,
                             format_converters: Optional[List[callable]] = None):
        
        if isinstance(semantic, int):
            semantic = self.layout.semantics[semantic]
        current_semantic = self.layout.get_element(semantic.abstract)
        if current_semantic is None:
            raise ValueError(f'NumpyBuffer is missing {semantic.abstract} semantic data!')
        if semantic_converters is not None:
            for data_converter in semantic_converters:
                data = data_converter(data)
        if current_semantic.format != semantic.format:
            data = current_semantic.format.type_encoder(data)
        if format_converters is not None:
            for data_converter in format_converters:
                data = data_converter(data)
        self.set_field(current_semantic.get_name(), data)

    def import_data(self,
                    data: 'NumpyBuffer',
                    semantic_converters: Dict[AbstractSemantic, List[callable]],
                    format_converters: Dict[AbstractSemantic, List[callable]]):
        
        for buffer_semantic in self.layout.semantics:

            data_semantic = data.layout.get_element(buffer_semantic.abstract)

            if data_semantic is None:
                continue
            field_data = data.get_field(buffer_semantic.get_name())

            self.import_semantic_data(
                field_data, 
                data_semantic,
                semantic_converters.get(buffer_semantic.abstract, []),
                format_converters.get(buffer_semantic.abstract, []))
            
    def import_raw_data(self, data: numpy.ndarray):
        self.data = numpy.frombuffer(data, dtype=self.layout.get_numpy_type())

    def get_bytes(self):
        return self.data.tobytes()

    def __len__(self):
        return len(self.data)


class MigotoFmt:
    def __init__(self, fmt_file: io.IOBase):
        vb_elements = []

        converters = {
            'SemanticName': lambda value: Semantic(value),
            'SemanticIndex': lambda value: int(value),
            'Format': lambda value: DXGIFormat(value.replace('DXGI_FORMAT_', '')),
            'AlignedByteOffset': lambda value: int(value),
        }

        element = None

        # Parse fmt
        for line in map(str.strip, fmt_file):
            data = line.split(':')
            if len(data) != 2:
                continue
            data = list(map(str.strip, data))
            key, value = data[0], data[1]

            if line.startswith('stride'):
                vb_stride = int(value)
            elif line.startswith('format') and element is None:
                ib_format = DXGIFormat(value.replace('DXGI_FORMAT_', ''))
                if ib_format == DXGIFormat.R8_UINT:
                    ib_format = DXGIFormat.R8G8B8_UINT
                elif ib_format == DXGIFormat.R16_UINT:
                    ib_format = DXGIFormat.R16G16B16_UINT
                elif ib_format == DXGIFormat.R32_UINT:
                    ib_format = DXGIFormat.R32G32B32_UINT
                self.ib_layout = BufferLayout([BufferSemantic(AbstractSemantic(Semantic.Index, 0), ib_format)])
            elif line.startswith('element'):
                if element is not None:
                    if len(element) == 4:
                        vb_elements.append(element)
                    elif len(element) > 0:
                        raise ValueError(f'malformed buffer element format: {element}')
                element = {}
            else:
                for search_key, converter in converters.items():
                    if key.startswith(search_key):
                        element[search_key] = converter(value)
                        break

        if len(element) == 4:
            vb_elements.append(element)

        # Calculate per-element stride
        vb_stride = 0
        vb_byte_offset = 0
        for i, element in enumerate(vb_elements):
            next_element_id = i + 1
            if len(vb_elements) > next_element_id:
                next_offset = vb_elements[next_element_id]['AlignedByteOffset']
            else:
                next_offset = vb_stride
            element['Stride'] = next_offset - vb_byte_offset
            vb_byte_offset = next_offset

        # Build layout
        self.vb_layout = BufferLayout([])
        for element in vb_elements:
            self.vb_layout.add_element(BufferSemantic(AbstractSemantic(element['SemanticName'], element['SemanticIndex']), element['Format'], stride=element['Stride']))

        if vb_stride != self.vb_layout.stride:
            raise ValueError(f'vb buffer layout format stride mismatch: {vb_stride} != {self.vb_layout.stride}')


class BufferElement:
    def __init__(self, buffer, index):
        self.buffer = buffer
        self.index = index
        self.layout = self.buffer.layout

    def get_bytes(self, semantic, return_buffer_semantic=False):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        byte_offset = self.index * semantic.stride
        data_bytes = self.buffer.data[semantic][byte_offset : byte_offset + semantic.stride]
        if not return_buffer_semantic:
            return data_bytes
        else:
            return data_bytes, semantic

    def set_bytes(self, semantic, data_bytes):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        byte_offset = self.index * semantic.stride
        self.buffer.data[semantic][byte_offset: byte_offset + semantic.stride] = data_bytes

    def get_value(self, semantic):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        data_bytes = self.get_bytes(semantic)
        return semantic.format.decoder(data_bytes).tolist()

    def set_value(self, semantic, value):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        self.set_bytes(semantic, semantic.format.encoder(value).tobytes())

    def get_all_bytes(self):
        data_bytes = bytearray()
        for semantic in self.layout.semantics:
            data_bytes.extend(self.get_bytes(semantic))
        return data_bytes


class ByteBuffer:
    def __init__(self, layout, data_bytes=None):
        self.layout = None
        self.data = {}
        self.num_elements = 0

        self.update_layout(layout)

        if data_bytes is not None:
            self.from_bytes(data_bytes)

    def validate(self):
        result = {}
        for semantic in self.layout.semantics:
            result[semantic] = len(self.data[semantic]) / semantic.stride
        if min(result.values()) != max(result.values()):
            result = ', '.join([f'{k.abstract}: {v}' for k, v in result.items()])
            raise ValueError(f'elements count mismatch in buffers: {result}')
        if len(self.layout.semantics) != len(self.data):
            raise ValueError(f'data structure must match buffer layout!')
        self.num_elements = int(min(result.values()))

    def update_layout(self, layout):
        self.layout = layout
        if len(self.data) != 0:
            self.validate()

    def from_bytes(self, data_bytes):
        if self.layout.force_stride:
            data_bytes.extend(bytearray((math.ceil(len(data_bytes) / self.layout.stride)) * self.layout.stride - len(data_bytes)))

        num_elements = len(data_bytes) / self.layout.stride
        if num_elements % 1 != 0:
            raise ValueError(f'buffer stride {self.layout.stride} must be multiplier of bytes len {len(data_bytes)}')
        num_elements = int(num_elements)

        self.data = {}
        for semantic in self.layout.semantics:
            self.data[semantic] = bytearray()

        byte_offset = 0
        for element_id in range(num_elements):
            for semantic in self.layout.semantics:
                self.data[semantic].extend(data_bytes[byte_offset:byte_offset+semantic.stride])
                byte_offset += semantic.stride

        if byte_offset != len(data_bytes):
            raise ValueError(f'layout mismatch: input ended at {byte_offset} instead of {len(data_bytes)}')

        self.validate()

    def get_element(self, index):
        return BufferElement(self, index)

    def extend(self, num_elements):
        if num_elements <= 0:
            raise ValueError(f'cannot extend buffer by {num_elements} elements')
        for semantic in self.layout.semantics:
            if semantic in self.data:
                self.data[semantic].extend(bytearray(num_elements * semantic.stride))
            else:
                self.data[semantic] = bytearray(num_elements * semantic.stride)
        self.validate()

    def get_fragment(self, offset, element_count):
        fragment = ByteBuffer(self.layout)
        for semantic in self.layout.semantics:
            byte_offset = offset * semantic.stride
            byte_count = element_count * semantic.stride
            fragment.data[semantic] = self.data[semantic][byte_offset:byte_offset+byte_count]
        fragment.validate()
        return fragment

    def import_buffer(self, src_byte_buffer, semantic_map=None, skip_missing=False):
        """
        Imports elements from source buffer based on their semantics
        Without 'semantic_map' provided creates new 'semantic_map' containing all source semantics
        Errors if any of 'semantic_map' elements is not found in src or dst buffers and 'skip_missing' is False
        """
        # Ensure equal number of elements in both buffers
        if src_byte_buffer.num_elements != self.num_elements:
            raise ValueError('source buffer len %d differs from destination buffer len %d' % (
                    src_byte_buffer.num_elements, self.num_elements))
        
        # Calculate semantic map
        semantic_map = self.map_semantics(src_byte_buffer, self, semantic_map=semantic_map, skip_missing=skip_missing)

        # Import data bytes
        for src_semantic, dst_semantic in semantic_map.items():
            if src_semantic.format == dst_semantic.format:
                self.data[dst_semantic] = src_byte_buffer.data[src_semantic]
            else:
                src_values = src_semantic.format.decoder(src_byte_buffer.data[src_semantic]).tolist()
                self.data[dst_semantic] = dst_semantic.format.encoder(src_values).tobytes()

        self.validate()

    def get_bytes(self, semantic=None):
        if semantic is None:
            data_bytes = bytearray()
            for element_id in range(self.num_elements):
                data_bytes.extend(self.get_element(element_id).get_all_bytes())
            return data_bytes
        else:
            if isinstance(semantic, AbstractSemantic):
                semantic = self.layout.get_element(semantic)
            return self.data[semantic]

    def get_values(self, semantic):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        data_bytes = self.get_bytes(semantic)
        return semantic.format.decoder(data_bytes).tolist()

    def set_bytes(self, semantic, data_bytes):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        self.data[semantic] = data_bytes
        self.validate()

    def set_values(self, semantic, values):
        if isinstance(semantic, AbstractSemantic):
            semantic = self.layout.get_element(semantic)
        self.set_bytes(semantic, semantic.format.encoder(values).tobytes())

    @staticmethod
    def map_semantics(src_byte_buffer, dst_byte_buffer, semantic_map=None, skip_missing=False):
        """
        
        """
        verified_semantic_map = {}
        if semantic_map is not None:
            # Semantic map may consist of AbstractSemantic instead of BufferSemantic, we need to convert it in this case
            # AbstractSemantic is independent of buffer specifics and contains only SemanticName and SemanticIndex
            # BufferSemantic wraps AbstractSemantic and describes where AbstractSemantic is located in given buffer
            for src_semantic, dst_semantic in semantic_map.items():
                # Ensure source semantic location in source buffer
                src_semantic = src_semantic
                if isinstance(src_semantic, AbstractSemantic):
                    src_semantic = src_byte_buffer.layout.get_element(src_semantic)
                if src_semantic not in src_byte_buffer.layout.semantics:
                    if not skip_missing:
                        raise ValueError(f'source buffer has no {src_semantic.abstract} semantic')
                    continue
                # Ensure destination semantic location in destination buffer
                dst_semantic = src_semantic
                if isinstance(src_semantic, AbstractSemantic):
                    dst_semantic = dst_byte_buffer.layout.get_element(dst_semantic)
                if dst_semantic not in dst_byte_buffer.layout.semantics:
                    if not skip_missing:
                        raise ValueError(f'destination buffer has no {dst_semantic.abstract} semantic')
                    continue
                # Add semantic to verified map
                verified_semantic_map[src_semantic] = dst_semantic
        else:
            # If there is no semantics map provided, map everything by default
            for src_semantic in src_byte_buffer.layout.semantics:
                # Locate matching semantic in destination buffer
                dst_semantic = dst_byte_buffer.layout.get_element(src_semantic.abstract)
                if dst_semantic is None:
                    if not skip_missing:
                        raise ValueError(f'destination buffer has no {src_semantic.abstract} semantic')
                    continue
                verified_semantic_map[src_semantic] = dst_semantic

        return verified_semantic_map


class IndexBuffer(ByteBuffer):
    def __init__(self, layout, data, load_indices=True):
        self.offset = None
        self.first_index = None
        self.index_count = None
        self.topology = None
        self.format = None
        self.faces = None

        if isinstance(data, io.IOBase):
            self.parse_format(data)
            if load_indices:
                self.parse_faces(data)
            super().__init__(layout)
        elif isinstance(data, bytearray):
            super().__init__(layout, data)
            self.bytes_to_faces()
        else:
            raise ValueError(f'unknown IB data format {data}')

    def parse_format(self, f):
        for line in map(str.strip, f):
            if line.startswith('byte offset:'):
                self.offset = int(line[13:])
            elif line.startswith('first index:'):
                self.first_index = int(line[13:])
            elif line.startswith('index count:'):
                self.index_count = int(line[13:])
            elif line.startswith('topology:'):
                self.topology = line[10:]
                if line != 'topology: trianglelist':
                    raise ValueError('"%s" is not yet supported' % line)
            elif line.startswith('format:'):
                dxgi_format = line[8:].replace('DXGI_FORMAT_', '')
                self.format = dxgi_format
            elif line == '':
                if any(x is None for x in [self.offset, self.first_index, self.index_count, self.topology, self.format]):
                    raise ValueError('failed to parse IB format')
                break

    def parse_faces(self, f):
        self.faces = []
        for line in map(str.strip, f):
            face = tuple(map(int, line.split()))
            assert (len(face) == 3)
            self.faces.append(face)
        assert (len(self.faces) * 3 == self.index_count)

    def faces_to_bytes(self):
        indices = []
        for face in self.faces:
            assert (len(face) == 3)
            indices.extend(list(face))
        assert (len(indices) == self.index_count)
        data_bytes = self.layout.semantics[0].format.encoder(indices).tobytes()
        self.from_bytes(data_bytes)
        assert (self.num_elements * 3 == self.index_count)

    def bytes_to_faces(self):
        self.faces = []
        for element_id in range(self.num_elements):
            face = self.get_element(element_id).get_value(self.layout.semantics[0])
            self.faces.append(tuple(face))

    def get_bytes(self, semantic=None):
        if self.num_elements * 3 != self.index_count:
            self.faces_to_bytes()
        assert (self.num_elements * 3 == self.index_count)
        return super().get_bytes(semantic)

    def get_format(self):
        return self.layout.get_element(AbstractSemantic(Semantic.Index)).get_format()