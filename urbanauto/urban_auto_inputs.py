# SPDX-License-Identifier: GPL-2.0-or-later
"""Validate, align and mask local index rasters before threshold estimation.

Temporary paths belong to one job. No downloaded imagery or hidden defaults.
"""
import math
import os
import tempfile
from contextlib import contextmanager

import numpy as np
from osgeo import gdal, osr

NODATA = -9999.0


def check_cancel(feedback):
    if feedback is not None and feedback.isCanceled():
        raise RuntimeError('Обработка отменена пользователем.')


def open_raster(path):
    ds = gdal.Open(os.fspath(path))
    if ds is None or ds.RasterCount != 1:
        raise ValueError(f'Нужен читаемый однополосный растр: {path}')
    if not ds.GetProjection():
        raise ValueError(f'У растра не задана система координат: {path}')
    gt = ds.GetGeoTransform()
    if not all(math.isfinite(v) for v in gt) or gt[1] == 0 or gt[5] == 0:
        raise ValueError(f'Некорректная геопривязка: {path}')
    return ds


def _same_crs(a, b):
    sa, sb = osr.SpatialReference(), osr.SpatialReference()
    sa.ImportFromWkt(a); sb.ImportFromWkt(b)
    return bool(sa.IsSame(sb))


@contextmanager
def prepare_rasters(paths, roi_path, *, reference=None, auto_align=True,
                    max_pixels=16000000, feedback=None, log=lambda s: None):
    """Yield aligned QgsRasterLayers and provenance, valid only inside this context.

    All four outputs share the intersection of finite, valid [-1, 1] pixels and
    the ROI. Bilinear interpolation is applied only to continuous index data.
    Reference must have a north-up grid; rotated sources can be warped to it.
    """
    from qgis.core import QgsRasterLayer
    if max_pixels < 1:
        raise ValueError('max_pixels должен быть положительным.')
    check_cancel(feedback)
    datasets = {k: open_raster(p) for k, p in paths.items()}
    ref = open_raster(reference or paths['ndbi'])
    gt = ref.GetGeoTransform()
    if gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0:
        raise ValueError('Опорная сетка должна быть north-up. Перепроецируйте опорный растр.')
    projection = ref.GetProjection()
    for key, ds in datasets.items():
        same = (ds.RasterXSize == ref.RasterXSize and ds.RasterYSize == ref.RasterYSize
                and _same_crs(ds.GetProjection(), projection)
                and np.allclose(ds.GetGeoTransform(), gt, rtol=0, atol=1e-9))
        if not same and not auto_align:
            raise ValueError(f'Сетка {key} отличается от опорной; включите автоматическое выравнивание.')
    with tempfile.TemporaryDirectory(prefix='urban_inputs_') as tmp:
        cutline = os.path.join(tmp, 'roi.gpkg')
        vec = gdal.VectorTranslate(cutline, roi_path, format='GPKG', dstSRS=projection)
        if vec is None:
            raise ValueError('Не удалось перепроецировать область интереса.')
        extent = vec.GetLayer(0).GetExtent()
        vec = None
        if extent is None:
            raise ValueError('Пустая область интереса.')
        x0 = max(0, math.floor((extent[0] - gt[0]) / gt[1]))
        x1 = min(ref.RasterXSize, math.ceil((extent[1] - gt[0]) / gt[1]))
        y0 = max(0, math.floor((gt[3] - extent[3]) / abs(gt[5])))
        y1 = min(ref.RasterYSize, math.ceil((gt[3] - extent[2]) / abs(gt[5])))
        width, height = x1 - x0, y1 - y0
        if width <= 0 or height <= 0:
            raise ValueError('Область интереса не пересекается с опорным растром.')
        if width * height > max_pixels:
            raise ValueError(f'Область содержит {width * height:,} пикселей; лимит {max_pixels:,}. '
                             'Уменьшите территорию или увеличьте лимит осознанно.')
        bounds = (gt[0] + x0 * gt[1], gt[3] + y1 * gt[5],
                  gt[0] + x1 * gt[1], gt[3] + y0 * gt[5])
        common = np.ones((height, width), dtype=bool)
        outputs = {}
        for key, path in paths.items():
            check_cancel(feedback)
            dst = os.path.join(tmp, key + '.tif')
            log(f'Подготовка {key}: общая сетка {width} × {height}, маска ROI...')
            ds = gdal.Warp(dst, path, format='GTiff', dstSRS=projection,
                           outputBounds=bounds, width=width, height=height,
                           outputType=gdal.GDT_Float32, resampleAlg='bilinear',
                           dstNodata=NODATA, cutlineDSName=cutline,
                           creationOptions=['COMPRESS=DEFLATE', 'TILED=YES'])
            if ds is None:
                raise ValueError(f'Не удалось подготовить {key}.')
            a = ds.GetRasterBand(1).ReadAsArray()
            common &= np.isfinite(a) & (a >= -1) & (a <= 1) & (a != NODATA)
            ds = None
            outputs[key] = dst
        valid_count = int(common.sum())
        if valid_count == 0:
            raise ValueError('Нет общих валидных пикселей всех индексов внутри ROI.')
        for path in outputs.values():
            ds = gdal.Open(path, gdal.GA_Update)
            band = ds.GetRasterBand(1)
            a = band.ReadAsArray(); a[~common] = NODATA
            band.WriteArray(a); band.SetNoDataValue(NODATA)
            band = None; ds = None
        layers = {key: QgsRasterLayer(path, key) for key, path in outputs.items()}
        if not all(lyr.isValid() for lyr in layers.values()):
            raise ValueError('QGIS не смог открыть подготовленные растры.')
        info = dict(reference=reference or paths['ndbi'], width=width, height=height,
                    valid_pixels=valid_count, valid_fraction_of_rectangle=valid_count / common.size,
                    bounds=list(bounds), projection=projection, resampling='bilinear',
                    common_valid_mask=True, max_pixels=max_pixels)
        datasets.clear(); ref = None
        try:
            yield layers, info
        finally:
            # Release provider handles before removing temporary files on Windows.
            for layer in layers.values():
                layer.setDataSource('', layer.name(), 'gdal')
            layers.clear()
