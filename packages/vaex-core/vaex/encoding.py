"""Private module that determines how data is encoded and serialized, to be able to send it over a wire, or save to disk"""

import base64
import io
import json
import numbers
import pickle
import uuid
import struct
import collections.abc

import numpy as np
import pyarrow as pa
import vaex
from .datatype import DataType

registry = {}


def register(name):
    def wrapper(cls):
        assert name not in registry
        registry[name] = cls
        return cls
    return wrapper


@register("json")  # this will pass though data as is
class vaex_json_encoding:
    @classmethod
    def encode(cls, encoding, result):
        return result

    @classmethod
    def decode(cls, encoding, result_encoded):
        return result_encoded


@register("vaex-task-result")
class vaex_task_result_encoding:
    @classmethod
    def encode(cls, encoding, result):
        return encoding.encode('vaex-evaluate-result', result)

    @classmethod
    def decode(cls, encoding, result_encoded):
        return encoding.decode('vaex-evaluate-result', result_encoded)


@register("vaex-rmi-result")
class vaex_rmi_result_encoding:
    @classmethod
    def encode(cls, encoding, result):
        return encoding.encode('json', result)

    @classmethod
    def decode(cls, encoding, result_encoded):
        return encoding.decode('json', result_encoded)


@register("vaex-evaluate-result")
class vaex_evaluate_results_encoding:
    @classmethod
    def encode(cls, encoding, result):
        if isinstance(result, (list, tuple)):
            return [cls.encode(encoding, k) for k in result]
        else:
           return encoding.encode('array', result)

    @classmethod
    def decode(cls, encoding, result_encoded):
        if isinstance(result_encoded, (list, tuple)):
            return [cls.decode(encoding, k) for k in result_encoded]
        else:
            return encoding.decode('array', result_encoded)


@register("array")
class array_encoding:
    @classmethod
    def encode(cls, encoding, result):
        if isinstance(result, np.ndarray):
            return {'type': 'ndarray', 'data': encoding.encode('ndarray', result)}
        elif isinstance(result, vaex.array_types.supported_arrow_array_types):
            return {'type': 'arrow-array', 'data': encoding.encode('arrow-array', result)}
        elif isinstance(result, numbers.Number):
            try:
                result = result.item()  # for numpy scalars
            except:  # noqa
                pass
            return {'type': 'json', 'data': result}
        else:
            raise ValueError('Cannot encode: %r' % result)

    @classmethod
    def decode(cls, encoding, result_encoded):
        return encoding.decode(result_encoded['type'], result_encoded['data'])


@register("arrow-array")
class arrow_array_encoding:
    @classmethod
    def encode(cls, encoding, array):
        schema = pa.schema({'x': array.type})
        with pa.BufferOutputStream() as sink:
            with pa.ipc.new_stream(sink, schema) as writer:
                writer.write_table(pa.table({'x': array}))
        blob = sink.getvalue()
        return {'arrow-ipc-blob': encoding.add_blob(blob)}

    @classmethod
    def decode(cls, encoding, result_encoded):
        if 'arrow-serialized-blob' in result_encoded:  # backward compatibility
            blob = encoding.get_blob(result_encoded['arrow-serialized-blob'])
            return pa.deserialize(blob)
        else:
            blob = encoding.get_blob(result_encoded['arrow-ipc-blob'])
            with pa.BufferReader(blob) as source:
                with pa.ipc.open_stream(source) as reader:
                    table = reader.read_all()
                    assert table.num_columns == 1
                    ar = table.column(0)
                    if len(ar.chunks) == 1:
                        ar = ar.chunks[0]
            return ar

@register("ndarray")
class ndarray_encoding:
    @classmethod
    def encode(cls, encoding, array):
        # if array.dtype.kind == 'O':
        #     raise ValueError('Numpy arrays with objects cannot be serialized: %r' % array)
        mask = None
        dtype = array.dtype
        if np.ma.isMaskedArray(array):
            values = array.data
            mask = array.mask
        else:
            values = array
        if values.dtype.kind in 'mM':
            values = values.view(np.uint64)
        if values.dtype.kind == 'O':
            data = {
                    'values': values.tolist(),  # rely on json encoding
                    'shape': array.shape,
                    'dtype': encoding.encode('dtype', DataType(dtype))
            }
        else:
            data = {
                    'values': encoding.add_blob(values),
                    'shape': array.shape,
                    'dtype': encoding.encode('dtype', DataType(dtype))
            }
        if mask is not None:
            data['mask'] = encoding.add_blob(mask)
        return data

    @classmethod
    def decode(cls, encoding, result_encoded):
        if isinstance(result_encoded, (list, tuple)):
            return [cls.decode(encoding, k) for k in result_encoded]
        else:
            dtype = encoding.decode('dtype', result_encoded['dtype'])
            shape = result_encoded['shape']
            if dtype.kind == 'O':
                data = result_encoded['values']
                array = np.array(data, dtype=dtype.numpy)
            else:
                data = encoding.get_blob(result_encoded['values'])
                array = np.frombuffer(data, dtype=dtype.numpy).reshape(shape)
            if 'mask' in result_encoded:
                mask_data = encoding.get_blob(result_encoded['mask'])
                mask_array = np.frombuffer(mask_data, dtype=np.bool_).reshape(shape)
                array = np.ma.array(array, mask=mask_array)
            return array


@register("numpy-scalar")
class numpy_scalar_encoding:
    @classmethod
    def encode(cls, encoding, scalar):
        if scalar.dtype.kind in 'mM':
            value = int(scalar.astype(int))
        else:
            value = scalar.item()
        return {'value': value, 'dtype': encoding.encode('dtype', DataType(scalar.dtype))}

    @classmethod
    def decode(cls, encoding, scalar_spec):
        dtype = encoding.decode('dtype', scalar_spec['dtype'])
        value = scalar_spec['value']
        return np.array([value], dtype=dtype.numpy)[0]

@register("dtype")
class dtype_encoding:
    @staticmethod
    def encode(encoding, dtype):
        dtype = dtype.internal
        return str(dtype)

    @staticmethod
    def decode(encoding, type_spec):
        if type_spec == 'string':
            return DataType(pa.string())
        if type_spec == 'large_string':
            return DataType(pa.large_string())
        # TODO: find a proper way to support all arrow types
        if type_spec == 'timestamp[ms]':
            return DataType(pa.timestamp('ms'))
        else:
            return DataType(np.dtype(type_spec))


@register("dataframe-state")
class dataframe_state_encoding:
    @staticmethod
    def encode(encoding, state):
        return state

    @staticmethod
    def decode(encoding, state_spec):
        return state_spec


@register("selection")
class selection_encoding:
    @staticmethod
    def encode(encoding, selection):
        return selection.to_dict() if selection is not None else None

    @staticmethod
    def decode(encoding, selection_spec):
        if selection_spec is None:
            return None
        selection = vaex.selections.selection_from_dict(selection_spec)
        return selection


@register("function")
class function_encoding:
    @staticmethod
    def encode(encoding, function):
        return vaex.serialize.to_dict(function.f)

    @staticmethod
    def decode(encoding, function_spec, trusted=False):
        if function_spec is None:
            return None
        function = vaex.serialize.from_dict(function_spec, trusted=trusted)
        return function



@register("variable")
class selection_encoding:
    @staticmethod
    def encode(encoding, obj):
        if isinstance(obj, np.ndarray):
            return {'type': 'ndarray', 'data': encoding.encode('ndarray', obj)}
        elif isinstance(obj, vaex.array_types.supported_arrow_array_types):
            return {'type': 'arrow-array', 'data': encoding.encode('arrow-array', obj)}
        elif isinstance(obj, vaex.hash.ordered_set):
            return {'type': 'ordered-set', 'data': encoding.encode('ordered-set', obj)}
        elif isinstance(obj, np.generic):
            return {'type': 'numpy-scalar', 'data': encoding.encode('numpy-scalar', obj)}
        elif isinstance(obj, np.integer):
            return obj.item()
        elif isinstance(obj, np.floating):
            return obj.item()
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, np.bytes_):
            return obj.decode('UTF-8')
        elif isinstance(obj, bytes):
            return str(obj, encoding='utf-8');
        else:
            return obj

    @staticmethod
    def decode(encoding, obj_spec):
        if isinstance(obj_spec, dict):
            return encoding.decode(obj_spec['type'], obj_spec['data'])
        else:
            return obj_spec




@register("binner")
class binner_encoding:
    @staticmethod
    def encode(encoding, binner):
        name = type(binner).__name__
        if name.startswith('BinnerOrdinal_'):
            datatype = name[len('BinnerOrdinal_'):]
            if datatype.endswith("_non_native"):
                datatype = datatype[:-len('64_non_native')]
                datatype = encoding.encode('dtype', DataType(np.dtype(datatype).newbyteorder()))
            return {'type': 'ordinal', 'expression': binner.expression, 'datatype': datatype, 'count': binner.ordinal_count, 'minimum': binner.min_value}
        elif name.startswith('BinnerScalar_'):
            datatype = name[len('BinnerScalar_'):]
            if datatype.endswith("_non_native"):
                datatype = datatype[:-len('64_non_native')]
                datatype = encoding.encode('dtype', DataType(np.dtype(datatype).newbyteorder()))
            return {'type': 'scalar', 'expression': binner.expression, 'datatype': datatype, 'count': binner.bins, 'minimum': binner.vmin, 'maximum': binner.vmax}
        else:
            raise ValueError('Cannot serialize: %r' % binner)

    @staticmethod
    def decode(encoding, binner_spec):
        type = binner_spec['type']
        dtype = encoding.decode('dtype', binner_spec['datatype'])
        if type == 'ordinal':
            cls = vaex.utils.find_type_from_dtype(vaex.superagg, "BinnerOrdinal_", dtype)
            return cls(binner_spec['expression'], binner_spec['count'], binner_spec['minimum'])
        elif type == 'scalar':
            cls = vaex.utils.find_type_from_dtype(vaex.superagg, "BinnerScalar_", dtype)
            return cls(binner_spec['expression'], binner_spec['minimum'], binner_spec['maximum'], binner_spec['count'])
        else:
            raise ValueError('Cannot deserialize: %r' % binner_spec)


@register("grid")
class grid_encoding:
    @staticmethod
    def encode(encoding, grid):
        return encoding.encode_list('binner', grid.binners)

    @staticmethod
    def decode(encoding, grid_spec):
        return vaex.superagg.Grid(encoding.decode_list('binner', grid_spec))


@register("ordered-set")
class ordered_set_encoding:
    @staticmethod
    def encode(encoding, obj):
        values = list(obj.extract().items())
        clsname = obj.__class__.__name__
        return {
            'class': clsname,
            'data': {
                'values': values,
                'count': obj.count,
                'nan_count': obj.nan_count,
                'missing_count': obj.null_count
            }
        }


    @staticmethod
    def decode(encoding, obj_spec):
        clsname = obj_spec['class']
        cls = getattr(vaex.hash, clsname)
        value = cls(dict(obj_spec['data']['values']), obj_spec['data']['count'], obj_spec['data']['nan_count'], obj_spec['data']['missing_count'])
        return value



class Encoding:
    def __init__(self, next=None):
        self.registry = {**registry}
        self.blobs = {}
        # for sharing objects
        self._object_specs = {}
        self._objects = {}

    def set_object(self, id, obj):
        assert id not in self._objects
        self._objects[id] = obj

    def get_object(self, id):
        return self._objects[id]

    def has_object(self, id):
        return id in self._objects

    def set_object_spec(self, id, obj):
        assert id not in self._object_specs, f"Overwriting id {id}"
        self._object_specs[id] = obj

    def get_object_spec(self, id):
        return self._object_specs[id]

    def has_object_spec(self, id):
        return id in self._object_specs

    def encode(self, typename, value):
        encoded = self.registry[typename].encode(self, value)
        return encoded

    def encode_list(self, typename, values):
        encoded = [self.registry[typename].encode(self, k) for k in values]
        return encoded

    def encode_list2(self, typename, values):
        encoded = [self.encode_list(typename, k) for k in values]
        return encoded

    def encode_dict(self, typename, values):
        encoded = {key: self.registry[typename].encode(self, value) for key, value in values.items()}
        return encoded

    def decode(self, typename, value, **kwargs):
        decoded = self.registry[typename].decode(self, value, **kwargs)
        return decoded

    def decode_list(self, typename, values, **kwargs):
        decoded = [self.registry[typename].decode(self, k, **kwargs) for k in values]
        return decoded

    def decode_list2(self, typename, values, **kwargs):
        decoded = [self.decode_list(typename, k, **kwargs) for k in values]
        return decoded

    def decode_dict(self, typename, values, **kwargs):
        decoded = {key: self.registry[typename].decode(self, value, **kwargs) for key, value in values.items()}
        return decoded

    def add_blob(self, buffer):
        blob_id = str(uuid.uuid4())
        self.blobs[blob_id] = memoryview(buffer).tobytes()
        return f'blob:{blob_id}'

    def get_blob(self, blob_ref):
        assert blob_ref.startswith('blob:')
        blob_id = blob_ref[5:]
        return self.blobs[blob_id]


class inline:
    @staticmethod
    def serialize(data, encoding):
        import base64
        blobs = {key: base64.b64encode(value).decode('ascii') for key, value in encoding.blobs.items()}
        return json.dumps({'data': data, 'blobs': blobs})

    @staticmethod
    def deserialize(data, encoding):
        data = json.loads(data)
        encoding.blobs = {key: base64.b64decode(value.encode('ascii')) for key, value in data['blobs'].items()}
        return data['data']


def _pack_blobs(*blobs):
    count = len(blobs)
    lenghts = [len(blob) for blob in blobs]
    stream = io.BytesIO()
    # header: <number of blobs>,<offset 0>, ... <offset N-1> with 8 byte unsigned ints
    header_length = 8 * (2 + count)
    offsets = (np.cumsum([0] + lenghts) + header_length).tolist()
    stream.write(struct.pack(f'{count+2}q', count, *offsets))
    for blob in blobs:
        stream.write(blob)
    bytes = stream.getvalue()
    assert offsets[-1] == len(bytes)
    return bytes


def _unpack_blobs(bytes):
    stream = io.BytesIO(bytes)

    count, = struct.unpack('q', stream.read(8))
    offsets = struct.unpack(f'{count+1}q', stream.read(8 * (count + 1)))
    assert offsets[-1] == len(bytes)
    blobs = []
    for i1, i2 in zip(offsets[:-1], offsets[1:]):
        blobs.append(bytes[i1:i2])
    return blobs


class binary:
    @staticmethod
    def serialize(data, encoding):
        blob_refs = list(encoding.blobs.keys())
        blobs = [encoding.blobs[k] for k in blob_refs]
        json_blob = json.dumps({'data': data, 'blob_refs': blob_refs, 'objects': encoding._object_specs})
        return _pack_blobs(json_blob.encode('utf8'), *blobs)

    @staticmethod
    def deserialize(data, encoding):
        json_data, *blobs = _unpack_blobs(data)
        json_data = json_data.decode('utf8')
        json_data = json.loads(json_data)
        data = json_data['data']
        encoding.blobs = {key: blob for key, blob in zip(json_data['blob_refs'], blobs)}
        encoding._object_specs = json_data['objects']
        return data


serialize = binary.serialize
deserialize = binary.deserialize
