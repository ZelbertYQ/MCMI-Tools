import numpy
import struct

from enum import Enum, IntEnum
from typing import Tuple


class DXGIType(Enum):
    FLOAT32 = (numpy.float32, None, None, None, None)
    FLOAT16 = (numpy.float16, None, None, None, None)
    UINT32 = (numpy.uint32, None, None, None, None)
    UINT16 = (numpy.uint16, None, None, None, None)
    UINT8 = (numpy.uint8, None, None, None, None)
    SINT32 = (numpy.int32, None, None, None, None)
    SINT16 = (numpy.int16, None, None, None, None)
    SINT8 = (numpy.int8, None, None, None, None)
    UNORM16 = (
        numpy.uint16,
        lambda data: numpy.fromiter(data, numpy.float32),
        None,
        lambda data: numpy.around(data * 65535.0).astype(numpy.uint16),
        lambda data: data / 65535.0)
    UNORM8 = (
        numpy.uint8,
        lambda data: numpy.fromiter(data, numpy.float32),
        None,
        lambda data: numpy.around(data * 255.0).astype(numpy.uint8),
        lambda data: data / 255.0)
    SNORM16 = (
        numpy.int16,
        lambda data: numpy.fromiter(data, numpy.float32),
        None,
        lambda data: numpy.around(data * 32767.0).astype(numpy.int16),
        lambda data: data / 32767.0)
    SNORM8 = (
        numpy.int8,
        lambda data: numpy.fromiter(data, numpy.float32),
        None,
        lambda data: numpy.around(data * 127.0).astype(numpy.int8),
        lambda data: data / 127.0)


class DXGIFormat(Enum):
    def __new__(cls, fmt, dxgi_type):
        
        (numpy_type, list_encoder, list_decoder, type_encoder, type_decoder) = dxgi_type.value

        obj = object.__new__(cls)
        obj._value_ = fmt
        obj.format = fmt
        obj.byte_width = 0
        obj.num_values = 0
        obj.value_bit_width = 0
        obj.value_byte_width = 0
        obj.dxgi_type = dxgi_type
        obj.numpy_base_type = numpy_type
        obj.type_encoder = type_encoder
        obj.type_decoder = type_decoder

        if list_encoder is None:
            obj.encoder = lambda data: numpy.fromiter(data, obj.numpy_base_type)
        else:
            obj.encoder = list_encoder

        if list_decoder is None:
            obj.decoder = lambda data: numpy.frombuffer(data, obj.numpy_base_type)
        else:
            obj.decoder = list_decoder

        if type_encoder is not None:
            obj.encoder = lambda data: type_encoder(obj.encoder(data))
        else:
            # Special encoder is not defined, lets use basic type conversion
            # We shouldn't do it earlier, as list encoder already does it via fromiter
            obj.type_encoder = lambda data: data.astype(obj.numpy_base_type)

        if type_decoder is not None:
            obj.decoder = lambda data: type_decoder(obj.decoder(data))
        
        for value_bit_width, value_byte_width in {'32': 4, '16': 2, '8': 1}.items():
            if value_bit_width in obj.dxgi_type.name:
                obj.num_values = obj.format.count(value_bit_width)
                obj.byte_width = obj.num_values * value_byte_width
                obj.value_bit_width = value_bit_width
                obj.value_byte_width = value_byte_width
                break
    
        if obj.byte_width <= 0:
            raise ValueError(f'Invalid byte width {obj.byte_width} for {obj.format}!')

        return obj

    def get_format(self):
        return 'DXGI_FORMAT_' + self.format
    
    def get_num_values(self, data_stride = 0):
        if data_stride > 0:
            # Caller specified data_stride, number of values may differ from the base dtype
            return int(data_stride / self.value_byte_width)
        else:
            return self.num_values

    def get_numpy_type(self, data_stride = 0):
        num_values = self.get_num_values(data_stride)
        # Tuple format of (type, 1) is deprecated, so we have to take special care
        if num_values == 1:
            return self.numpy_base_type
        else:
            return (self.numpy_base_type, num_values)
            
    # Float 32
    R32G32B32A32_FLOAT = 'R32G32B32A32_FLOAT', DXGIType.FLOAT32
    R32G32B32_FLOAT = 'R32G32B32_FLOAT', DXGIType.FLOAT32
    R32G32_FLOAT = 'R32G32_FLOAT', DXGIType.FLOAT32
    R32_FLOAT = 'R32_FLOAT', DXGIType.FLOAT32
    # Float 16
    R16G16B16A16_FLOAT = 'R16G16B16A16_FLOAT', DXGIType.FLOAT16
    R16G16B16_FLOAT = 'R16G16B16_FLOAT', DXGIType.FLOAT16
    R16G16_FLOAT = 'R16G16_FLOAT', DXGIType.FLOAT16
    R16_FLOAT = 'R16_FLOAT', DXGIType.FLOAT16
    # UINT 32
    R32G32B32A32_UINT = 'R32G32B32A32_UINT', DXGIType.UINT32
    R32G32B32_UINT = 'R32G32B32_UINT', DXGIType.UINT32
    R32G32_UINT = 'R32G32_UINT', DXGIType.UINT32
    R32_UINT = 'R32_UINT', DXGIType.UINT32
    # UINT 16
    R16G16B16A16_UINT = 'R16G16B16A16_UINT', DXGIType.UINT16
    R16G16B16_UINT = 'R16G16B16_UINT', DXGIType.UINT16
    R16G16_UINT = 'R16G16_UINT', DXGIType.UINT16
    R16_UINT = 'R16_UINT', DXGIType.UINT16
    # UINT 8
    R8G8B8A8_UINT = 'R8G8B8A8_UINT', DXGIType.UINT8
    R8G8B8_UINT = 'R8G8B8_UINT', DXGIType.UINT8
    R8G8_UINT = 'R8G8_UINT', DXGIType.UINT8
    R8_UINT = 'R8_UINT', DXGIType.UINT8
    # SINT 32
    R32G32B32A32_SINT = 'R32G32B32A32_SINT', DXGIType.SINT32
    R32G32B32_SINT = 'R32G32B32_SINT', DXGIType.SINT32
    R32G32_SINT = 'R32G32_SINT', DXGIType.SINT32
    R32_SINT = 'R32_SINT', DXGIType.SINT32
    # SINT 16
    R16G16B16A16_SINT = 'R16G16B16A16_SINT', DXGIType.SINT16
    R16G16B16_SINT = 'R16G16B16_SINT', DXGIType.SINT16
    R16G16_SINT = 'R16G16_SINT', DXGIType.SINT16
    R16_SINT = 'R16_SINT', DXGIType.SINT16
    # SINT 8
    R8G8B8A8_SINT = 'R8G8B8A8_SINT', DXGIType.SINT8
    R8G8B8_SINT = 'R8G8B8_SINT', DXGIType.SINT8
    R8G8_SINT = 'R8G8_SINT', DXGIType.SINT8
    R8_SINT = 'R8_SINT', DXGIType.SINT8
    # UNORM 16
    R16G16B16A16_UNORM = 'R16G16B16A16_UNORM', DXGIType.UNORM16
    R16G16B16_UNORM = 'R16G16B16_UNORM', DXGIType.UNORM16
    R16G16_UNORM = 'R16G16_UNORM', DXGIType.UNORM16
    R16_UNORM = 'R16_UNORM', DXGIType.UNORM16
    # UNORM 8
    R8G8B8A8_UNORM = 'R8G8B8A8_UNORM', DXGIType.UNORM8
    R8G8B8_UNORM = 'R8G8B8_UNORM', DXGIType.UNORM8
    R8G8_UNORM = 'R8G8_UNORM', DXGIType.UNORM8
    R8_UNORM = 'R8_UNORM', DXGIType.UNORM8
    # SNORM 16
    R16G16B16A16_SNORM = 'R16G16B16A16_SNORM', DXGIType.SNORM16
    R16G16B16_SNORM = 'R16G16B16_SNORM', DXGIType.SNORM16
    R16G16_SNORM = 'R16G16_SNORM', DXGIType.SNORM16
    R16_SNORM = 'R16_SNORM', DXGIType.SNORM16
    # SNORM 8
    R8G8B8A8_SNORM = 'R8G8B8A8_SNORM', DXGIType.SNORM8
    R8G8B8_SNORM = 'R8G8B8_SNORM', DXGIType.SNORM8
    R8G8_SNORM = 'R8G8_SNORM', DXGIType.SNORM8
    R8_SNORM = 'R8_SNORM', DXGIType.SNORM8


class DXGIFormatIndex(IntEnum):
    UNKNOWN = 0
    R32G32B32A32_TYPELESS = 1
    R32G32B32A32_FLOAT = 2
    R32G32B32A32_UINT = 3
    R32G32B32A32_SINT = 4
    R32G32B32_TYPELESS = 5
    R32G32B32_FLOAT = 6
    R32G32B32_UINT = 7
    R32G32B32_SINT = 8
    R16G16B16A16_TYPELESS = 9
    R16G16B16A16_FLOAT = 10
    R16G16B16A16_UNORM = 11
    R16G16B16A16_UINT = 12
    R16G16B16A16_SNORM = 13
    R16G16B16A16_SINT = 14
    R32G32_TYPELESS = 15
    R32G32_FLOAT = 16
    R32G32_UINT = 17
    R32G32_SINT = 18
    R32G8X24_TYPELESS = 19
    D32_FLOAT_S8X24_UINT = 20
    R32_FLOAT_X8X24_TYPELESS = 21
    X32_TYPELESS_G8X24_UINT = 22
    R10G10B10A2_TYPELESS = 23
    R10G10B10A2_UNORM = 24
    R10G10B10A2_UINT = 25
    R11G11B10_FLOAT = 26
    R8G8B8A8_TYPELESS = 27
    R8G8B8A8_UNORM = 28
    R8G8B8A8_UNORM_SRGB = 29
    R8G8B8A8_UINT = 30
    R8G8B8A8_SNORM = 31
    R8G8B8A8_SINT = 32
    R16G16_TYPELESS = 33
    R16G16_FLOAT = 34
    R16G16_UNORM = 35
    R16G16_UINT = 36
    R16G16_SNORM = 37
    R16G16_SINT = 38
    R32_TYPELESS = 39
    D32_FLOAT = 40
    R32_FLOAT = 41
    R32_UINT = 42
    R32_SINT = 43
    R24G8_TYPELESS = 44
    D24_UNORM_S8_UINT = 45
    R24_UNORM_X8_TYPELESS = 46
    X24_TYPELESS_G8_UINT = 47
    R8G8_TYPELESS = 48
    R8G8_UNORM = 49
    R8G8_UINT = 50
    R8G8_SNORM = 51
    R8G8_SINT = 52
    R16_TYPELESS = 53
    R16_FLOAT = 54
    D16_UNORM = 55
    R16_UNORM = 56
    R16_UINT = 57
    R16_SNORM = 58
    R16_SINT = 59
    R8_TYPELESS = 60
    R8_UNORM = 61
    R8_UINT = 62
    R8_SNORM = 63
    R8_SINT = 64
    A8_UNORM = 65
    R1_UNORM = 66
    R9G9B9E5_SHAREDEXP = 67
    R8G8_B8G8_UNORM = 68
    G8R8_G8B8_UNORM = 69
    BC1_TYPELESS = 70
    BC1_UNORM = 71
    BC1_UNORM_SRGB = 72
    BC2_TYPELESS = 73
    BC2_UNORM = 74
    BC2_UNORM_SRGB = 75
    BC3_TYPELESS = 76
    BC3_UNORM = 77
    BC3_UNORM_SRGB = 78
    BC4_TYPELESS = 79
    BC4_UNORM = 80
    BC4_SNORM = 81
    BC5_TYPELESS = 82
    BC5_UNORM = 83
    BC5_SNORM = 84
    B5G6R5_UNORM = 85
    B5G5R5A1_UNORM = 86
    B8G8R8A8_UNORM = 87
    B8G8R8X8_UNORM = 88
    R10G10B10_XR_BIAS_A2_UNORM = 89
    B8G8R8A8_TYPELESS = 90
    B8G8R8A8_UNORM_SRGB = 91
    B8G8R8X8_TYPELESS = 92
    B8G8R8X8_UNORM_SRGB = 93
    BC6H_TYPELESS = 94
    BC6H_UF16 = 95
    BC6H_SF16 = 96
    BC7_TYPELESS = 97
    BC7_UNORM = 98
    BC7_UNORM_SRGB = 99
    AYUV = 100
    Y410 = 101
    Y416 = 102
    NV12 = 103
    P010 = 104
    P016 = 105
    FORMAT_420_OPAQUE = 106
    YUY2 = 107
    Y210 = 108
    Y216 = 109
    NV11 = 110
    AI44 = 111
    IA44 = 112
    P8 = 113
    A8P8 = 114
    B4G4R4A4_UNORM = 115
    P208 = 130
    V208 = 131
    V408 = 132
    SAMPLER_FEEDBACK_MIN_MIP_OPAQUE = 189
    SAMPLER_FEEDBACK_MIP_REGION_USED_OPAQUE = 190
    FORCE_UINT = 0xffffffff

    def to_typeless(self):
        prefix = self.name.split('_', 1)[0]
        if prefix == 'R8':
            return self
        typeless_name = f'{prefix}_TYPELESS'
        try:
            return DXGIFormatIndex[typeless_name]
        except KeyError:
            return self

    def get_same_prefix_formats(self):
        prefix = self.name.split('_', 1)[0] + '_'
        return [fmt for fmt in DXGIFormatIndex if fmt.name.startswith(prefix)]
