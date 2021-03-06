# coding=utf-8
from __future__ import absolute_import, division, print_function

import logging
import math
import os
import warnings

from scipy.ndimage import morphology
from haversine import haversine
import numpy as np
import rasterio
from rasterio import transform, warp, windows
from rasterio._err import CPLE_OutOfMemoryError
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from rasterio.windows import Window
from scipy import ndimage

from . import mosaic
from .stats import Timer
from .utils import Bounds, PixelCollection

WEB_MERCATOR_CRS = CRS.from_epsg(3857)
WGS84_CRS = CRS.from_epsg(4326)
LOG = logging.getLogger(__name__)

EXTENTS = {
    str(WEB_MERCATOR_CRS): (-20037508.342789244, -20037508.342789244,
                            20037508.342789244, 20037508.342789244),
}

# initialize GDAL environment variables that can't be set w/ Lambda
os.environ["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = os.getenv(
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".vrt,.tif,.ovr,.msk")


class NoDataAvailable(Exception):
    pass


def _isimage(data_format):
    return data_format.upper() in ["RGB", "RGBA"]


def _mask(data, nodata):
    if np.issubdtype(data.dtype, float):
        return np.ma.masked_values(data, nodata, copy=False)

    return np.ma.masked_equal(data, nodata, copy=False)


def _nodata(dtype):
    if np.issubdtype(dtype, float):
        return np.finfo(dtype).min
    else:
        return np.iinfo(dtype).min


def crop(pixel_collection, data_format, offsets):
    data, (bounds, data_crs), _ = pixel_collection
    left, bottom, right, top = offsets

    if _isimage(data_format):
        width, height, _ = data.shape
        t = transform.from_bounds(*bounds, width=width, height=height)

        data = data[top:height - bottom, left:width - right, :]

        cropped_window = windows.Window(left, top, width, height)
        cropped_bounds = windows.bounds(cropped_window, t)

        return (data, (cropped_bounds, data_crs))

    _, height, width = data.shape
    t = transform.from_bounds(*bounds, width=width, height=height)

    data = data[:, top:height - bottom, left:width - right]

    cropped_window = windows.Window(left, top, width, height)
    cropped_bounds = windows.bounds(cropped_window, t)

    return PixelCollection(data, Bounds(cropped_bounds, data_crs))


def get_extent(crs):
    return EXTENTS[str(crs)]


def get_resolution(bounds, dims):
    height, width = dims
    t = transform.from_bounds(*bounds.bounds, width=width, height=height)

    return abs(t.a), abs(t.e)


def get_resolution_in_meters(bounds, dims):
    if bounds.crs.is_geographic:
        bounds, _ = bounds
        height, width = dims

        left = (bounds[0], (bounds[1] + bounds[3]) / 2)
        right = (bounds[2], (bounds[1] + bounds[3]) / 2)
        top = ((bounds[0] + bounds[2]) / 2, bounds[3])
        bottom = ((bounds[0] + bounds[2]) / 2, bounds[1])

        return (haversine(left, right) * 1000 / width,
                haversine(top, bottom) * 1000 / height)

    return get_resolution(bounds, dims)


def get_source(path):
    """Cached source opening."""
    with rasterio.Env():
        return rasterio.open(path)


def get_zoom(resolution, op=round):
    return int(
        op(
            math.log((2 * math.pi * 6378137) / (resolution * 256)) / math.log(
                2)))


def read_window(src, bounds, target_shape):
    # TODO use this for DEMs (not all single-band sources) to avoid
    # stairstepping artifacts
    if src.count == 1 and bounds.crs == WEB_MERCATOR_CRS:
        # special case for web Mercator; use a target image size that most
        # closely matches the source resolution (and is a power of 2)
        zoom = min(
            22,
            get_zoom(
                max(
                    get_resolution_in_meters(
                        Bounds(src.bounds, src.crs), (src.height, src.width))),
                op=math.ceil))

        dst_width = dst_height = (2**zoom) * 256
        extent = get_extent(bounds.crs)
        resolution = ((extent[2] - extent[0]) / dst_width,
                      (extent[3] - extent[1]) / dst_height)

        dst_transform = Affine(resolution[0], 0.0, extent[0], 0.0,
                               -resolution[1], extent[3])
    else:
        # use a target image size that most closely matches the target
        # resolution
        # calculate natural width, height, and transform (so the mask can be
        # warped)
        # TODO providing resolution reduces the target dimensions but loses the
        # ability to read overviews

        # if raster is overly-large, approximate the transform based on
        # a scaled-down version and scale it back after
        attempts = 0
        scale_factor = 1
        dst_transform = None

        while (dst_transform is None and src.width // scale_factor > 0
               and src.height // scale_factor > 0):
            try:
                (dst_transform, dst_width,
                 dst_height) = warp.calculate_default_transform(
                     src.crs, bounds.crs, src.width // scale_factor,
                     src.height // scale_factor, *src.bounds)

                scale = Affine.scale(scale_factor, scale_factor)

                dst_transform *= ~scale
                dst_width, dst_height = scale * (dst_width, dst_height)
            except (MemoryError, CPLE_OutOfMemoryError):
                attempts += 1
                scale_factor = 2 * attempts

    # Some OAM sources have invalid NODATA values (-1000 for a file with a
    # dtype of Byte). rasterio returns None under these circumstances
    # (indistinguishable from sources that actually have no NODATA values).
    # Providing a synthetic value "correctly" masks the output at the expense
    # of masking valid pixels with that value. This was previously (partially;
    # in the form of the bounding box but not NODATA pixels) addressed by
    # creating a VRT that mapped the mask to an alpha channel (something we
    # can't do w/o adding nDstAlphaBand to rasterio/_warp.pyx).
    #
    # Creating external masks and reading them separately (as below) is a
    # better solution, particularly as it avoids artifacts introduced when the
    # NODATA values are resampled using something other than nearest neighbor.

    # TODO resampling alg should be a catalog property
    with WarpedVRT(
            src,
            src_nodata=src.nodata or _nodata(src.meta['dtype']),
            dst_crs=bounds.crs,
            dst_width=dst_width,
            dst_height=dst_height,
            dst_transform=dst_transform,
            resampling=Resampling.lanczos) as vrt:
        dst_window = vrt.window(*bounds.bounds)

        resolution = get_resolution(bounds, target_shape)
        src_resolution = get_resolution(Bounds(vrt.bounds, vrt.crs), vrt.shape)
        scale_factor = (round(dst_window.width / target_shape[1], 6), round(
            dst_window.height / target_shape[0], 6))

        height, width = target_shape

        if vrt.count == 1 and (
                scale_factor[0] < 1 or scale_factor[1] < 1
                or src_resolution[0] > resolution[0]
                or src_resolution[1] > resolution[1]
        ) and round(dst_window.width) > 1.0 and round(dst_window.height) > 1.0:
            # scale_factor will always be (1.0, 1.0) unless using the web
            # Mercator-specific calculations
            # instead, compare source resolution (in m) to resolution (as
            # scale_factor) and modify dst_window to correspond to a smaller,
            # lower resolution window (as target_window)

            # TODO sources like ETOPO1 may end up with funky y scale factors
            # (for web Mercator, due to distortion)
            # if these problems can be addressed, the web Mercator-specific
            # calculations (above, for single-band sources) can be removed
            if scale_factor[0] < 1 or scale_factor[1] < 1:
                scaled_transform = vrt.transform * Affine.scale(*scale_factor)
                target_window = windows.from_bounds(
                    *bounds.bounds, transform=scaled_transform)
            else:
                scale_factor = (resolution[0] / src_resolution[0],
                                resolution[1] / src_resolution[1])
                target_window = Window(dst_window.col_off * scale_factor[0],
                                       dst_window.row_off * scale_factor[1],
                                       dst_window.width * scale_factor[0],
                                       dst_window.height * scale_factor[1])

            # buffer apparently needs to be 50% of the target size in order
            # for spline knots to match between adjacent tiles
            # however, to avoid creating overly-large uncropped areas, we
            # limit the buffer size to 2048px on a side
            # TODO the resulting window is still much too large (e.g.
            # 18/151153/84343@2x)
            buffer_pixels = (min(target_window.width / 2,
                                 math.ceil(2048 * scale_factor[0])), min(
                                     target_window.height / 2,
                                     math.ceil(2048 * scale_factor[1])))

            r, c = dst_window.toranges()
            window = Window.from_slices((max(
                0, r[0] - buffer_pixels[1]), r[1] + buffer_pixels[1]), (max(
                    0, c[0] - buffer_pixels[0]), c[1] + buffer_pixels[0]))

            data = vrt.read(boundless=True, window=window)

            # mask with NODATA values
            if vrt.nodata is not None:
                data = _mask(data, vrt.nodata)
            else:
                data = np.ma.masked_array(data, mask=np.ma.nomask)

            mask = data.mask

            order = 3

            if mask.any():
                # need to preserve NODATA; drop spline interpolation order to 1
                order = 1

            LOG.info(
                "Applying spline interpolation (order %d @ scale factor %s)",
                order, scale_factor)

            zoom = (1, round(1 / scale_factor[1]), round(1 / scale_factor[0]))

            LOG.info("target dimensions: %s", (data.shape[1] * zoom[1],
                                               data.shape[2] * zoom[2]))

            # resample data, respecting NODATA values
            data = ndimage.zoom(
                # prevent resulting values from producing cliffs
                data.astype(np.float32),
                zoom,
                order=order)

            scaled_buffer = (int((data.shape[2] - width) / 2), int(
                (data.shape[1] - height) / 2))

            # crop data
            data = data[:, scaled_buffer[1]:scaled_buffer[1] + height,
                        scaled_buffer[0]:scaled_buffer[0] + width]

            if len(mask.shape) > 0:
                mask = ndimage.zoom(mask, zoom, mode='nearest')

                # crop mask
                mask = mask[:, scaled_buffer[1]:scaled_buffer[1] + height,
                            scaled_buffer[0]:scaled_buffer[0] + width]

            # copy the mask over
            data = np.ma.masked_array(data, mask=mask)
        else:
            data = vrt.read(
                boundless=True,
                out_shape=(vrt.count, height, width),
                window=dst_window)

            # mask with NODATA values
            if vrt.nodata is not None:
                data = _mask(data, vrt.nodata)
            else:
                data = np.ma.masked_array(data, mask=np.ma.nomask)

        data = data.astype(np.float32)

    # open the mask separately so we can take advantage of its overviews
    try:
        warnings.simplefilter("ignore")
        with rasterio.open("{}.msk".format(src.name), crs=src.crs) as mask_src:
            with WarpedVRT(
                    mask_src,
                    src_crs=src.crs,
                    src_transform=src.transform,
                    dst_crs=bounds.crs,
                    dst_width=dst_width,
                    dst_height=dst_height,
                    dst_transform=dst_transform) as mask_vrt:
                warnings.simplefilter("default")
                dst_window = mask_vrt.window(*bounds.bounds)

                mask = mask_vrt.read(
                    boundless=True,
                    out_shape=(mask_vrt.count, height, width),
                    window=dst_window)

                data.mask = data.mask | ~mask
    except Exception:
        # no mask; assume the included one was dirty and expand it
        if data.mask.any():
            data.mask = morphology.binary_dilation(
                data.mask, iterations=2)

    return PixelCollection(data, bounds)


def render(bounds,
           catalog,
           shape,
           target_crs,
           format,
           data_band_count,
           transformation=None):
    """Render data intersecting bounds into shape using an optional
    transformation."""
    resolution_m = get_resolution_in_meters(bounds, shape)
    stats = []

    if transformation:
        bounds, shape, offsets = transformation.expand(bounds, shape)

    with Timer() as t:
        sources = catalog.get_sources(bounds, resolution_m)
    stats.append(("get sources", t.elapsed))

    with Timer() as t:
        sources_used, pixels = mosaic.composite(sources, bounds, shape,
                                                target_crs, data_band_count)
    stats.append(("composite", t.elapsed))

    if pixels.data is None:
        raise NoDataAvailable()

    data_format = "raw"

    if transformation:
        with Timer() as t:
            pixels, data_format = transformation.transform(pixels)
        stats.append(("transform", t.elapsed))

        with Timer() as t:
            pixels = transformation.postprocess(pixels, data_format, offsets)

        stats.append(("postprocess", t.elapsed))

    with Timer() as t:
        (content_type, formatted) = format(pixels, data_format)
    stats.append(("format", t.elapsed))

    headers = {
        "Content-Type":
        content_type,
        "X-Imagery-Sources":
        ", ".join(s[1].split('/', 3)[3] for s in sources_used),
    }

    if os.environ.get('MARBLECUTTER_DEBUG_TIMERS'):
        headers.update({
            "X-Timers":
            ", ".join("{}: {:0.2f}".format(*s) for s in stats)
        })

    return (headers, formatted)
