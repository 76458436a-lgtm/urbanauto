# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Временная согласованность ряда застройки (v4.2).

Вместо простого накопления (любое пятно, появившееся однажды, остаётся навсегда)
новое пятно года Y принимается, только если оно подтверждается в последующих срезах:
  conf_Y = M_Y ∧ (число последующих срезов, где пиксель — застройка) ≥ k
  C_Y    = C_{Y-1} ∨ conf_Y                     (застройка не исчезает)
Последний срез нечем подтвердить «вперёд». Режимы:
  backward (по умолчанию, проверен в 4.2.1 на 220 точках): новое пятно последнего года
           принимается, если оно видно и в предыдущем срезе (два наблюдения подряд) — нижняя оценка;
  accept:  последний срез принимается как есть — верхняя оценка, предварительно.
В отчёт пишутся обе оценки последнего года.
Расчёт выполняется на сетке опорного растра (обычно NDBI), чтобы срезы совпадали попиксельно.
"""
import os
import tempfile
import uuid
import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()


def _grid(ref_raster):
    ds = gdal.Open(ref_raster)
    return ds.RasterXSize, ds.RasterYSize, ds.GetGeoTransform(), ds.GetProjection()


def vector_source(layer):
    """Путь к файлу и имя слоя для GDAL. Слои в памяти, с фильтром или не-OGR провайдера
    (временные результаты Processing, нарисованные полигоны) сохраняются во временный GeoPackage."""
    if isinstance(layer, str):
        parts = layer.split('|')
        name = next((p.split('=', 1)[1] for p in parts[1:] if p.startswith('layername=')), None)
        return parts[0], name, None
    src = layer.source()
    if layer.providerType() == 'ogr' and not layer.subsetString() and os.path.exists(src.split('|')[0]):
        return vector_source(src)
    from qgis.core import QgsVectorFileWriter, QgsCoordinateTransformContext
    tmp = os.path.join(tempfile.gettempdir(), f'_urban_vec_{uuid.uuid4().hex[:8]}.gpkg')
    opt = QgsVectorFileWriter.SaveVectorOptions()
    opt.driverName = 'GPKG'
    opt.layerName = 'data'
    opt.skipAttributeCreation = True  # нужна только геометрия; поле fid-текст ломает GPKG
    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, tmp, QgsCoordinateTransformContext(), opt)
    if res[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError(f'Не удалось сохранить слой «{layer.name()}» во временный файл: {res[1]}')
    return tmp, 'data', tmp


def rasterize_layer(path, grid, layer_name=None):
    """Полигоны (путь или QgsVectorLayer) → булев массив на сетке опорного растра (пиксель по центру)."""
    path, name, tmp_file = vector_source(path)
    layer_name = layer_name or name
    w, h, gt, proj = grid
    drv = gdal.GetDriverByName('MEM')
    out = drv.Create('', w, h, 1, gdal.GDT_Byte)
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    src = gdal.OpenEx(path, gdal.OF_VECTOR)
    if src is None:
        raise ValueError(f'Не удалось открыть вектор: {path}')
    lyr = src.GetLayerByName(layer_name) if layer_name else src.GetLayer(0)
    if lyr is None:
        raise ValueError(f'Слой не найден: {layer_name}')
    virtual = f'/vsimem/urban_{uuid.uuid4().hex}.gpkg'
    try:
        warped = gdal.VectorTranslate(virtual, src, options=gdal.VectorTranslateOptions(
            dstSRS=proj, layers=[lyr.GetName()], format='GPKG'))
        if warped is None:
            raise ValueError('Не удалось перепроецировать вектор.')
        warped = None
        gdal.Rasterize(out, virtual, burnValues=[1], allTouched=False)
        return out.GetRasterBand(1).ReadAsArray().astype(bool)
    finally:
        lyr = None; src = None; out = None
        gdal.Unlink(virtual)
        if tmp_file:
            try:
                os.remove(tmp_file)
            except OSError:
                pass


LAST_MODES = ('backward', 'accept')


def temporal_consistency(masks, years, min_confirm=2, last_mode='backward'):
    """masks: dict year -> bool array (независимые срезы). Возвращает dict year -> bool array."""
    if last_mode == 'previous':  # имя из 4.2.x
        last_mode = 'backward'
    if last_mode not in LAST_MODES:
        raise ValueError(f"Неизвестный режим последнего среза: {last_mode}. Допустимо: {', '.join(LAST_MODES)}.")
    if isinstance(min_confirm, bool) or not isinstance(min_confirm, (int, np.integer)) or min_confirm < 1:
        raise ValueError('min_confirm должен быть целым числом >= 1.')
    if len(years) != len(set(years)):
        raise ValueError('Годы не должны повторяться.')
    years = sorted(years)
    if not years:
        raise ValueError('Пустой временной ряд.')
    shape = np.asarray(masks[years[0]]).shape
    if any(np.asarray(masks[y]).shape != shape or np.asarray(masks[y]).dtype != np.bool_ for y in years):
        raise ValueError('Маски должны быть булевыми и иметь одинаковую форму.')
    out, prev = {}, None
    for i, y in enumerate(years):
        later = [masks[years[j]] for j in range(i + 1, len(years))]
        if later:
            k = min(min_confirm, len(later))
            cnt = np.zeros_like(masks[y], dtype=np.int16)
            for m in later:
                cnt += m
            conf = masks[y] & (cnt >= k)
        elif last_mode == 'backward' and i > 0:
            conf = masks[y] & masks[years[i - 1]]
        else:
            conf = masks[y].copy()
        out[y] = conf if prev is None else (prev | conf)
        prev = out[y]
    return out


def coverage_on_grid(path, grid):
    """Read a 0/1 coverage raster on a target grid without interpolating classes."""
    w, h, gt, proj = grid
    if gt[2] or gt[4]:
        raise ValueError('Нужна north-up опорная сетка.')
    bounds = (gt[0], gt[3] + h*gt[5], gt[0] + w*gt[1], gt[3])
    ds = gdal.Warp('', path, format='MEM', dstSRS=proj, outputBounds=bounds,
                   width=w, height=h, resampleAlg='near', dstNodata=0)
    if ds is None:
        raise ValueError(f'Не удалось прочитать маску покрытия: {path}')
    return ds.GetRasterBand(1).ReadAsArray() == 1


def polygonize(arr, grid, out_path, layer_name, year):
    """Булев массив → полигоны GeoPackage с полем year."""
    w, h, gt, proj = grid
    drv = gdal.GetDriverByName('MEM')
    ds = drv.Create('', w, h, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    band = ds.GetRasterBand(1)
    band.WriteArray(arr.astype(np.uint8))
    band.SetNoDataValue(0)
    vdrv = ogr.GetDriverByName('GPKG')
    if os.path.exists(out_path):
        vdrv.DeleteDataSource(out_path)
    vds = vdrv.CreateDataSource(out_path)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(proj)
    lyr = vds.CreateLayer(layer_name, srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn('DN', ogr.OFTInteger))
    gdal.Polygonize(band, band, lyr, 0, [], callback=None)
    lyr.CreateField(ogr.FieldDefn('year', ogr.OFTInteger))
    for f in lyr:
        f.SetField('year', int(year))
        lyr.SetFeature(f)
    vds = None
    return out_path


def pixel_area_km2(grid):
    """Площадь пикселя по строкам (км²) для географической или проекционной сетки."""
    w, h, gt, proj = grid
    srs = osr.SpatialReference()
    srs.ImportFromWkt(proj)
    if gt[2] or gt[4]:
        raise ValueError('Расчёт площади требует north-up сетку без вращения.')
    if srs.IsGeographic():
        R = 6371.0088
        lat = gt[3] + (np.arange(h) + 0.5) * gt[5]
        a = (np.radians(abs(gt[1])) * R) * (np.radians(abs(gt[5])) * R) * np.cos(np.radians(lat))
        return np.repeat(a[:, None], w, axis=1)
    unit = srs.GetLinearUnits() or 1.0
    return np.full((h, w), abs(gt[1] * gt[5]) * unit ** 2 / 1e6)
