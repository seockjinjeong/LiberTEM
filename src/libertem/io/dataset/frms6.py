# -*- encoding: utf-8 -*-
import os
import re
import csv
import glob
import typing
import logging
import warnings
import configparser

from numba.typed import List
import scipy.io as sio
import numpy as np
import numba

from libertem.common import Shape, Slice
from libertem.web.messages import MessageConverter
from .base import (
    DataSet, DataSetException, DataSetMeta,
    FileSet, BasePartition, LocalFile, Decoder,
    TilingScheme, make_get_read_ranges,
)

log = logging.getLogger(__name__)
READOUT_MODE_PAT = re.compile(
    r'^"bin:\s*(?P<bin>\d+),\s*windowing:\s*(?P<win_i>\d+)\s*x\s*(?P<win_j>\d+)\s*"$'
)

# file header: 1024 bytes, at the beginning of each file
file_header_dtype = [
    ('header_size', '<u2'),        # fixed value: 1024
    ('frame_header_size', '<u2'),  # fixed value: 64
    ('padding_1', (bytes, 3)),
    ('version', '<u1'),            # fixed value: 6
    ('comment_1', (bytes, 80)),
    ('width', '<u2'),              # -> columns
    ('height', '<u2'),             # -> rows
    ('comment_2', (bytes, 928)),
    ('num_frames', '<u4'),
]

# frame header: 64 bytes, before each frame
frame_header_dtype = [
    ('padding_1', (bytes, 4)),
    ('timestamp_s', '<u4'),
    ('timestamp_us', '<u4'),
    ('frame_number', '<u4'),
    ('padding_2', (bytes, 12)),
    ('comment', (bytes, 36)),
]


class FRMS6DatasetParams(MessageConverter):
    SCHEMA = {
      "$schema": "http://json-schema.org/draft-07/schema#",
      "$id": "http://libertem.org/FRMS6DatasetParams.schema.json",
      "title": "FRMS6DatasetParams",
      "type": "object",
      "properties": {
          "type": {"const": "FRMS6"},
          "path": {"type": "string"},
      },
      "required": ["type", "path"]
    }

    def convert_to_python(self, raw_data):
        data = {
            k: raw_data[k]
            for k in ["path"]
        }
        return data


class GainMapCSVDialect(csv.excel):
    delimiter = ';'


def _unbin(tile_data, factor):
    """
    tile_data should have shape (num_frames, y, x)
    """
    s = tile_data.shape

    # insert a binning dimension:
    tile_data = tile_data.reshape((s[0], s[1], 1, s[2]))
    unbinned = tile_data.repeat(factor, axis=2)
    # FIXME: should we scale the data by the binning factor?
    return unbinned.reshape((s[0], factor * s[1], s[2]))


def _get_base_filename(_path):
    path, ext = os.path.splitext(_path)
    if ext == ".hdr":
        base = path
    elif ext == ".frms6":
        base = re.sub(r'_[0-9]+$', '', path)
    else:
        raise DataSetException("unknown extension: %s" % ext)
    return base


def _read_dataset_hdr(fname):
    if not os.path.exists(fname):
        raise DataSetException(
            "Could not find .hdr file %s" % (
                fname,
            )
        )

    config = configparser.ConfigParser()
    config.read(fname)
    parsed = {}
    sections = config.sections()
    if 'measurementInfo' not in sections:
        raise DataSetException(
            "measurementInfo missing from .hdr file %s, have: %s" % (
                fname,
                repr(sections),
            )
        )
    msm_info = config['measurementInfo']
    int_fields = {'darkframes', 'dwelltimemicroseconds', 'gain', 'signalframes'}
    for key in msm_info:
        value = msm_info[key]
        if key in int_fields:
            parsed[key] = int(value)
        else:
            parsed[key] = value
    # FIXME: are the dimensions the right way aroud? is there a sample file with a non-square
    # scan region?
    parsed['stemimagesize'] = tuple(int(p) for p in parsed['stemimagesize'].split('x'))
    match = READOUT_MODE_PAT.match(parsed['readoutmode'])
    if match is None:
        raise DataSetException("could not parse readout mode")
    readout_mode = match.groupdict()
    parsed['readoutmode'] = {k: int(v) for k, v in readout_mode.items()}
    return parsed


def _read_file_header(path):
    header_raw = np.fromfile(path, dtype=file_header_dtype, count=1)
    header = {}
    for field, dtype in file_header_dtype:
        if type(dtype) != str:
            continue
        header[field] = header_raw[field][0]
        # to prevent overflows in following computations:
        if np.dtype(dtype).kind == "u":
            header[field] = int(header[field])
    header['filesize'] = os.stat(path).st_size
    header['path'] = path
    return header


def _header_valid(header):
    if not (header['header_size'] == 1024
            and header['frame_header_size'] == 64
            and header['version'] == 6):
        return False
    # TODO: file size sanity check?
    return True


def _num_frames(header):
    if header['num_frames'] != 0:
        return header['num_frames']
    # older FRMS6 files don't contain the number of frames in the header,
    # so calculate from filesize:
    w, h = header['width'], header['height']
    bytes_per_frame = w * h * 2
    assert header['filesize'] is not None
    num = (header['filesize'] - 1024)
    denum = (bytes_per_frame + 64)
    res = num // denum
    if num % denum != 0:
        raise DataSetException("could not determine number of frames")
    return res


@numba.njit(inline='always')
def _map_y(y, xs, binning, num_rows):
    """
    Parameters
    ----------

    binning : int
        Binning factor (1, 2, 4)

    num_rows : int
        Total number of rows (should be 264, if not windowing)
    """
    half = num_rows // 2 // binning
    if y < half:
        return (y, 0)
    else:
        return (((num_rows // binning) - y - 1), xs)


@numba.njit(inline='always')
def _frms6_read_ranges_tile_block(
    slices_arr, fileset_arr, slice_sig_sizes, sig_origins,
    inner_indices_start, inner_indices_stop, frame_indices, sig_size,
    px_to_bytes, bpp, frame_header_bytes, frame_footer_bytes, file_idxs,
    slice_offset, extra, sig_shape,
):
    """
    Read ranges for frms6: create read ranges line-by-line
    """
    result = List()

    # positions in the signal dimensions:
    for slice_idx in range(slices_arr.shape[0]):
        # (offset, size) arrays defining what data to read (in pixels)
        slice_origin = slices_arr[slice_idx][0]
        slice_shape = slices_arr[slice_idx][1]

        read_ranges = List()

        xs = slice_shape[1]
        stride = 2 * xs

        binning = extra[0]
        num_rows = sig_shape[0]

        # inner "depth" loop along the (flat) navigation axis of a tile:
        for i, inner_frame_idx in enumerate(range(inner_indices_start, inner_indices_stop)):
            inner_frame = frame_indices[inner_frame_idx]
            file_idx = file_idxs[i]
            f = fileset_arr[file_idx]
            frame_in_file_idx = inner_frame - f[0]

            # we are reading a part of a single frame, so we first need to find
            # the offset caused by headers:
            header_offset = frame_header_bytes * (frame_in_file_idx + 1)

            # now let's figure in the current frame index:
            # (go down into the file by full frames; `sig_size`)
            offset = header_offset + frame_in_file_idx * (sig_size // binning) * bpp

            y_start = slice_origin[0] // binning
            y_stop = (slice_origin[0] + slice_shape[0]) // binning

            for y in range(y_start, y_stop):
                mapped_y, x_offset = _map_y(y, xs, binning, num_rows)
                start = offset + (stride * mapped_y + x_offset) * bpp
                read_ranges.append(
                    (
                        file_idx,
                        start, start + xs * bpp
                    )
                )

        # the indices are compressed to the selected frames
        compressed_slice = np.array([
            [slice_offset + inner_indices_start] + [i for i in slice_origin],
            [inner_indices_stop - inner_indices_start] + [i for i in slice_shape],
        ])

        result.append((slice_idx, compressed_slice, read_ranges))
    return result


frms6_get_read_ranges = make_get_read_ranges(
    read_ranges_tile_block=_frms6_read_ranges_tile_block
)


def _make_decode_frms6(binning):
    @numba.njit(inline='always')
    def _decode_frms6(inp, out, idx, native_dtype, rr, origin, shape, ds_shape):
        """
        Row-for-row decoding of frms6 data
        """
        inp_decoded = inp.reshape((-1,)).view(native_dtype)
        out_3d = out.reshape(out.shape[0], -1, shape[-1])

        num_rows_binned = ds_shape[-2] // binning
        origin_y_binned = origin[1] // binning

        # how many rows need to be *read* for this tile
        # if we are un-binning, we need to read only
        rows_in_tile = shape[1] // binning

        # broadcast the row we read to (start, stop) rows:
        start = (idx % rows_in_tile) * binning
        stop = start + binning

        depth = idx // rows_in_tile

        # print(depth, start, stop, rows_in_tile, origin_y_binned)

        if origin_y_binned + (idx % rows_in_tile) < num_rows_binned // 2:
            out_3d[depth, start:stop, :] = inp_decoded
        else:
            out_3d[depth, start:stop, :] = inp_decoded[::-1]
    return _decode_frms6


decode_frms6_b1 = _make_decode_frms6(1)
decode_frms6_b2 = _make_decode_frms6(2)
decode_frms6_b4 = _make_decode_frms6(4)


class FRMS6Decoder(Decoder):
    def __init__(self, binning, gain_map, dark_frame):
        self._binning = binning
        self._gain_map = gain_map
        self._dark_frame = dark_frame

    def get_decode(self, native_dtype, read_dtype):
        return {
            1: decode_frms6_b1,
            2: decode_frms6_b2,
            4: decode_frms6_b4,
        }[self._binning]

    def get_dark_frame(self):
        return self._dark_frame

    def get_gain_map(self):
        return self._gain_map


class FRMS6File(LocalFile):
    pass


class FRMS6FileSet(FileSet):
    def __init__(self, global_header, *args, **kwargs):
        self._global_header = global_header
        super().__init__(*args, **kwargs)

    def _clone(self, *args, **kwargs):
        return self.__class__(
            global_header=self._global_header,
            *args, **kwargs
        )

    def get_read_ranges(
        self, start_at_frame: int, stop_before_frame: int,
        dtype, tiling_scheme: TilingScheme,
        roi: typing.Union[np.ndarray, None] = None,
    ):
        fileset_arr = self.get_as_arr()
        binning = self._global_header['readoutmode']['bin']
        return frms6_get_read_ranges(
            start_at_frame=start_at_frame,
            stop_before_frame=stop_before_frame,
            roi=roi,
            depth=tiling_scheme.depth,
            slices_arr=tiling_scheme.slices_array,
            fileset_arr=fileset_arr,
            sig_shape=tuple(tiling_scheme.dataset_shape.sig),
            bpp=np.dtype(dtype).itemsize,
            frame_header_bytes=self._frame_header_bytes,
            frame_footer_bytes=self._frame_footer_bytes,
            extra=(binning,),
        )


class FRMS6DataSet(DataSet):
    r"""
    Read PNDetector FRMS6 files. FRMS6 data sets consist of multiple .frms6 files and
    a .hdr file. The first .frms6 file (matching \*_000.frms6) contains dark frames, which
    are subtracted if `enable_offset_correction` is true.

    Parameters
    ----------

    path : string
        Path to one of the files of the FRMS6 dataset (either .hdr or .frms6)

    enable_offset_correction : boolean
        Subtract dark frames when reading data

    gain_map_path : string
        Path to a gain map to apply (.mat format)
    """
    def __init__(self, path, enable_offset_correction=True, gain_map_path=None, dest_dtype=None):
        super().__init__()
        self._path = path
        self._gain_map_path = gain_map_path
        self._dark_frame = None
        self._enable_offset_correction = enable_offset_correction
        self._meta = None
        self._filenames = None
        self._hdr_info = None
        self._fileset = None
        if dest_dtype is not None:
            warnings.warn(
                "dest_dtype is now handled per `get_tiles` call, and ignored here",
                DeprecationWarning
            )

    @property
    def shape(self):
        return self._meta.shape

    def _do_initialize(self):
        self._filenames = list(sorted(glob.glob(self._pattern())))
        self._hdr_info = self._read_hdr_info()
        self._headers = [
            _read_file_header(path)
            for path in self._files()
        ]
        header = self._headers[0]
        raw_frame_size = header['height'], header['width']
        # frms6 frames are folded in a specific way, this is the shape after unfolding:
        frame_size = 2 * header['height'], header['width'] // 2
        assert header['width'] % 2 == 0
        hdr = self._get_hdr_info()
        bin_factor = hdr['readoutmode']['bin']
        if bin_factor > 1:
            frame_size = (frame_size[0] * bin_factor, frame_size[1])

        sig_dims = 2  # FIXME: is there a different cameraMode that doesn't output 2D signals?
        preferred_dtype = np.dtype("<u2")
        if self._enable_offset_correction:
            preferred_dtype = np.dtype("float32")
        self._meta = DataSetMeta(
            raw_dtype=np.dtype("<u2"),
            dtype=preferred_dtype,
            metadata={'raw_frame_size': raw_frame_size},
            shape=Shape(tuple(hdr['stemimagesize']) + frame_size, sig_dims=sig_dims),
        )
        self._dark_frame = self._get_dark_frame()
        self._gain_map = self._get_gain_map()
        self._total_filesize = sum(
            os.stat(path).st_size
            for path in self._files()
        )
        return self

    def initialize(self, executor):
        return executor.run_function(self._do_initialize)

    @classmethod
    def detect_params(cls, path, executor):
        hdr_filename = "%s.hdr" % executor.run_function(_get_base_filename, path)
        try:
            executor.run_function(_read_dataset_hdr, hdr_filename)
        except Exception:
            return False
        return {
            "parameters": {
                "path": path
            },
        }

    @classmethod
    def get_supported_extensions(cls):
        return set(["frms6", "hdr"])

    @classmethod
    def get_msg_converter(cls):
        return FRMS6DatasetParams

    def get_diagnostics(self):
        global_header = self._get_hdr_info()
        return [
            {"name": "Offset correction available and enabled",
             "value": str(self._get_dark_frame() is not None)},
        ] + [
            {"name": str(k),
             "value": str(v)}
            for k, v in global_header.items()
        ]

    def _read_hdr_info(self):
        hdr_filename = "%s.hdr" % _get_base_filename(self._path)
        return _read_dataset_hdr(hdr_filename)

    def _get_hdr_info(self):
        return self._hdr_info

    def _get_dark_frame(self):
        if not self._enable_offset_correction:
            return None

        header = self._headers[0]
        num_frames = _num_frames(header)

        fs = self._get_fileset([self._headers[0]])

        part_slice = Slice(
            origin=(0, 0, 0),
            shape=Shape((num_frames,) + tuple(self.shape.sig), sig_dims=2)
        )
        p = FRMS6Partition(
            meta=self._meta,
            partition_slice=part_slice,
            fileset=fs,
            start_frame=0,
            num_frames=num_frames,
            header=self._headers[0],
            global_header=self._get_hdr_info(),
        )
        tileshape = Shape(
            (128, 8) + (self.shape[-1],),
            sig_dims=2,
        )
        tiling_scheme = TilingScheme.make_for_shape(
            tileshape=tileshape,
            dataset_shape=self.shape,
        )
        tiles = p.get_tiles(tiling_scheme=tiling_scheme, dest_dtype=np.float32)
        out = np.zeros(self.shape.sig, dtype=np.float32)
        for tile in tiles:
            out[tile.tile_slice.get(sig_only=True)] += np.sum(tile.data, axis=0)
        return out / num_frames

    def _get_gain_map(self):
        if self._gain_map_path is None:
            return None
        _, ext = os.path.splitext(self._gain_map_path)
        if ext.lower() == '.mat':
            gain_mat = sio.loadmat(self._gain_map_path)
            return gain_mat['GainMap']
        elif ext.lower() == '.csv':
            with open(self._gain_map_path) as csv_f:
                csv_reader = csv.reader(csv_f, dialect=GainMapCSVDialect)
                csv_gain_data = list(csv_reader)
                csv_gain_nums = [[float(x) for x in row if x != ''] for row in csv_gain_data]
                return np.array(csv_gain_nums).T

    def _pattern(self):
        path, ext = os.path.splitext(self._path)
        if ext == ".hdr":
            pattern = "%s_*.frms6" % path
        elif ext == ".frms6":
            pattern = "%s*.frms6" % (
                re.sub(r'[0-9]+$', '', path)
            )
        else:
            raise DataSetException("unknown extension: %s" % ext)
        return pattern

    def _files(self):
        return self._filenames

    def check_valid(self):
        try:
            for header in self._headers:
                if not _header_valid(header):
                    raise DataSetException(
                        "error while checking validity of %s" % header['path']
                    )
            return True
        except (IOError, OSError) as e:
            raise DataSetException("invalid dataset: %s" % e)

    def get_cache_key(self):
        return {
            "path": self._path,
            "enable_offset_correction": self._enable_offset_correction,
            "gain_map_path": self._gain_map_path,
        }

    @property
    def dtype(self):
        return self._meta.dtype

    @property
    def raw_dtype(self):
        return self._meta.raw_dtype

    def _get_num_partitions(self):
        """
        returns the number of partitions the dataset should be split into
        """
        # let's try to aim for 512MB (converted float data) per partition
        partition_size_px = 512 * 1024 * 1024 // 4
        total_size_px = np.prod(self.shape)
        res = max(self._cores, total_size_px // partition_size_px)
        return res

    def _get_fileset(self, headers=None):
        files = []
        start_idx = 0
        if headers is None:
            headers = self._headers[1:]  # skip first file w/ dark frames...
        header = headers[0]
        native_dtype = np.dtype(np.uint16)
        frame_header_size = header['frame_header_size']
        file_header = header['header_size']
        for header in headers:
            path = header['path']
            sig_shape = (header['height'], header['width'])
            num_frames = _num_frames(header)
            files.append(
                FRMS6File(
                    path=path,
                    start_idx=start_idx,
                    end_idx=start_idx + num_frames,
                    native_dtype=native_dtype,
                    sig_shape=sig_shape,
                    file_header=file_header,
                    frame_header=frame_header_size,
                )
            )
            start_idx += num_frames
        global_header = self._get_hdr_info()
        return FRMS6FileSet(
            files=files,
            global_header=global_header,
            frame_header_bytes=frame_header_size
        )

    def get_partitions(self):
        fileset = self._get_fileset()
        gain_map = self._get_gain_map()
        dark_frame = self._get_dark_frame()
        for part_slice, start, stop in BasePartition.make_slices(
                shape=self.shape,
                num_partitions=self._get_num_partitions()):
            yield FRMS6Partition(
                meta=self._meta,
                partition_slice=part_slice,
                fileset=fileset,
                start_frame=start,
                num_frames=stop - start,
                header=self._headers[0],
                global_header=self._get_hdr_info(),
                gain_map=gain_map,
                dark_frame=dark_frame,
            )


class FRMS6Partition(BasePartition):
    def __init__(self, global_header, header, gain_map=None, dark_frame=None, *args, **kwargs):
        self._header = header
        self._global_header = global_header
        self._gain_map = gain_map
        self._dark_frame = dark_frame
        super().__init__(*args, **kwargs)

    def _get_binning(self):
        return self._global_header['readoutmode']['bin']

    def _get_decoder(self):
        return FRMS6Decoder(
            binning=self._get_binning(),
            gain_map=self._gain_map,
            dark_frame=self._dark_frame,
        )

    def validate_tiling_scheme(self, tiling_scheme):
        binning = self._get_binning()

        a = len(tiling_scheme.shape) == 3
        b = tiling_scheme.shape[1] % binning == 0
        c = tiling_scheme.shape[2] == self.meta.shape.sig[1]
        if not (a and b and c):
            raise ValueError(
                "Invalid tiling scheme: needs to be divisible by binning (%d)" % binning
            )

    def get_base_shape(self):
        return (1, self._get_binning(), self.shape.sig[-1])
