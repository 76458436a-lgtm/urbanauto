# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Годовой ряд застройки без ручных точек: композиты → метки → модель → год появления (5.0; схема с 4.4).

`run_series` не зависит от интерфейса QGIS (нужны GDAL, numpy, scipy, scikit-learn) и вызывается
алгоритмом Processing «Годовой ряд застройки (Random Forest)» и из консоли.
"""
import csv
import datetime
import json
import math
import os
import time

import numpy as np

from . import urban_auto_imagery as IM
from . import urban_auto_rf as RF
from . import urban_auto_labels as LB
from .urban_auto_version import VERSION as PLUGIN_VERSION, CORE


def utm_grid_for(bbox_wgs, res_m=30.0):
    """Сетка UTM (bounds, width, height, 'EPSG:…') по охвату WGS 84, выровненная по res_m."""
    w, s, e, n = bbox_wgs
    lon, lat = (w + e) / 2, (s + n) / 2
    epsg = (32600 if lat >= 0 else 32700) + min(60, int((lon + 180) / 6) + 1)
    from osgeo import osr
    g = osr.SpatialReference(); g.ImportFromEPSG(4326); g.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    u = osr.SpatialReference(); u.ImportFromEPSG(epsg); u.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(g, u)
    pts = [ct.TransformPoint(x, y)[:2] for x in (w, e) for y in (s, n)]
    x0 = math.floor(min(p[0] for p in pts) / res_m) * res_m
    x1 = math.ceil(max(p[0] for p in pts) / res_m) * res_m
    y0 = math.floor(min(p[1] for p in pts) / res_m) * res_m
    y1 = math.ceil(max(p[1] for p in pts) / res_m) * res_m
    return (x0, y0, x1, y1), int(round((x1 - x0) / res_m)), int(round((y1 - y0) / res_m)), f'EPSG:{epsg}'


def grid_from_raster(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    bounds = (gt[0], gt[3] + h * gt[5], gt[0] + w * gt[1], gt[3])
    crs = ds.GetProjection()
    ds = None
    return bounds, w, h, crs


def _gt(grid):
    bounds, w, h, crs = grid
    return (bounds[0], (bounds[2] - bounds[0]) / w, 0, bounds[3], 0, -(bounds[3] - bounds[1]) / h)


def _wkt(grid):
    from osgeo import osr
    s = osr.SpatialReference(); s.SetFromUserInput(grid[3])
    return s.ExportToWkt()


def write_raster(path, arrays, grid, dtype='Float32', nodata=None, names=None):
    from osgeo import gdal
    arrays = arrays if isinstance(arrays, (list, tuple)) else [arrays]
    gtype = getattr(gdal, 'GDT_' + dtype)
    ds = gdal.GetDriverByName('GTiff').Create(path, grid[1], grid[2], len(arrays), gtype,
                                             options=['COMPRESS=DEFLATE', 'TILED=YES'])
    ds.SetGeoTransform(_gt(grid)); ds.SetProjection(_wkt(grid))
    for i, a in enumerate(arrays):
        b = ds.GetRasterBand(i + 1)
        b.WriteArray(a)
        if nodata is not None:
            b.SetNoDataValue(nodata)
        if names:
            b.SetDescription(str(names[i]))
    ds = None


def read_composite(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    a = ds.ReadAsArray().astype(np.float32)
    ds = None
    return a


def pixel_area_km2(grid):
    """Площадь пикселя (км²) по строкам: для UTM — постоянная, для градусов — с учётом широты."""
    from osgeo import osr
    bounds, w, h, crs = grid
    s = osr.SpatialReference(); s.SetFromUserInput(crs)
    dx, dy = (bounds[2] - bounds[0]) / w, (bounds[3] - bounds[1]) / h
    if s.IsGeographic():
        lat = bounds[3] - (np.arange(h) + 0.5) * dy
        a = (dy * 110574.0) * (dx * 111320.0 * np.cos(np.radians(lat))) / 1e6
        return np.repeat(a[:, None], w, 1).astype(np.float64)
    return np.full((h, w), dx * dy / 1e6)


def mask_from_vector(path, grid):
    """Полигон ROI → булева маска на сетке."""
    from osgeo import gdal, ogr
    src = ogr.Open(path.split('|')[0])
    lyr = src.GetLayer(0)
    mem = gdal.GetDriverByName('MEM').Create('', grid[1], grid[2], 1, gdal.GDT_Byte)
    mem.SetGeoTransform(_gt(grid)); mem.SetProjection(_wkt(grid))
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    m = mem.GetRasterBand(1).ReadAsArray().astype(bool)
    lyr = None; src = None
    return m


def points_to_rc(path, grid):
    """Точки (например, независимой проверки) → строки/столбцы сетки; их окрестность исключается из обучения."""
    from osgeo import ogr, osr
    src = ogr.Open(path.split('|')[0]); lyr = src.GetLayer(0)
    s_src = lyr.GetSpatialRef(); s_dst = osr.SpatialReference(); s_dst.SetFromUserInput(grid[3])
    for s in (s_src, s_dst):
        if s is not None:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s_src, s_dst) if s_src is not None else None
    gt = _gt(grid); rows, cols = [], []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        x, y = g.GetX(), g.GetY()
        if ct:
            x, y = ct.TransformPoint(x, y)[:2]
        c, r = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
        if 0 <= r < grid[2] and 0 <= c < grid[1]:
            rows.append(r); cols.append(c)
    return rows, cols


# Определение застройки (5.1). «строгое» — здания и покрытия (доля зданий OSM/Microsoft, зелень в посёлке —
# растительность, подтверждение ESA/WSF 10 м); выбрано по 11 размеченным городам: средняя κ 2025 0,78 против 0,75.
# «территория» — как в 5.0: застроенная территория вместе с дворами и зеленью кварталов (определение ВКР по Ураю).
QUALITY_MIN_COVERAGE = 0.0      # 5.5.1: доля территории с данными, ниже — год исключается (0 — не исключать)
QUALITY_LOW = 0.6               # ниже — «низкое покрытие» (предупреждение)
QUALITY_PARTIAL = 0.9           # ниже — год помечается «частичное покрытие»
QUALITY_ANOMALY = (0.6, 1.6)    # доля «застроено» среди пикселей с данными относительно соседних лет


def composite_quality(spectra, clear, roi_mask=None, min_coverage=QUALITY_MIN_COVERAGE):
    """5.5.1: качество годовых композитов. Возвращает {год: dict(coverage, scenes, status)}.

    coverage — доля пикселей территории с данными; scenes — медиана числа чистых сцен на этих пикселях.
    status: ok | partial (coverage < QUALITY_PARTIAL) | low (coverage < QUALITY_LOW) | excluded (coverage < min_coverage).
    Повод: композит Урая 2003 г. (одна сцена Landsat 7 SLC-off) покрывал 26 % территории,
    «сырая» площадь застройки за год получалась 1,7 км² вместо ~14. Пиксели без данных и раньше
    не участвовали в определении года появления; искажались только годовые «сырые» величины.
    По умолчанию год не исключается: на Урае исключение 2003 г. меняло κ по годам разнонаправленно
    (2010: 0,87 → 0,92; 2025: 0,98 → 0,93), без общего выигрыша."""
    q = {}
    for y, sp in spectra.items():
        v = np.isfinite(sp['ndvi'])
        if roi_mask is not None:
            v_roi, total = v & roi_mask, int(roi_mask.sum())
        else:
            v_roi, total = v, v.size
        cov = float(v_roi.sum()) / max(total, 1)
        c = np.asarray(clear.get(y), np.float32) if y in clear else None
        scenes = float(np.nanmedian(c[v_roi])) if c is not None and v_roi.any() else 0.0
        status = ('excluded' if cov < min_coverage else 'low' if cov < QUALITY_LOW else
                  'partial' if cov < QUALITY_PARTIAL else 'ok')
        q[y] = dict(coverage=round(cov, 4), scenes=round(scenes, 1), status=status)
    return q


def flag_anomalies(quality, years, built_share):
    """5.5.1: год, где доля «застроено» среди пикселей с данными отличается от медианы соседних
    (±2 года, только годы ok/partial) больше чем в QUALITY_ANOMALY раз, помечается anomaly
    (предупреждение, из расчёта не исключается)."""
    for i, y in enumerate(years):
        nb = [built_share[j] for j in range(max(0, i - 2), min(len(years), i + 3))
              if j != i and np.isfinite(built_share[j])]
        if len(nb) < 2 or not np.isfinite(built_share[i]):
            continue
        med = float(np.median(nb))
        if med <= 0:
            continue
        r = built_share[i] / med
        quality[y]['built_share'] = round(float(built_share[i]), 4)
        quality[y]['ratio_to_neighbours'] = round(r, 3)
        if not QUALITY_ANOMALY[0] <= r <= QUALITY_ANOMALY[1] and quality[y]['status'] in ('ok', 'partial'):
            quality[y]['status'] = 'anomaly'
    return quality


PROFILE_GHSL_PEAK = 0.75        # 5.5.1: порог подсказки профиля (см. recommend_profile)


def recommend_profile(sources):
    """5.5.1: подсказка определения застройки по пиковой доле застройки GHSL 2020 на территории.

    Плотные города (пик ≥ PROFILE_GHSL_PEAK) — «территория» (профиль «Высокая полнота»), малые и
    малоэтажные — «строгое». На 15 размеченных городах (проверка 4.11.2026) правило выбирает лучший
    профиль в 11 случаях; средняя κ 2025 — 0,790 против 0,775 у всегда строгого (0,781 при выборе
    порога без проверяемого города). Это подсказка, а не гарантия."""
    g = (sources or {}).get('ghsl', {})
    a = g.get(2020) if 2020 in g else (g[max(g)] if g else None)
    if a is None:
        return None
    a = np.nan_to_num(np.asarray(a, np.float32))
    peak = float(a.max()) / (100.0 if a.max() > 1.5 else 1.0)
    return dict(ghsl_peak=round(peak, 3), recommended='territory' if peak >= PROFILE_GHSL_PEAK else 'strict',
                threshold=PROFILE_GHSL_PEAK)


LABEL_MODES = {'strict': dict(bld_frac_min=0.08, green_neg=True, sharp_now=True), 'territory': {}}
LABEL_OPTS = LABEL_MODES['strict']


def grid_for_city(name, res_m=30.0, *, buffer_m=1500.0, search_km=15.0, ghsl_min=0.12, max_km=40.0, log=print):
    """5.0: город по названию → сетка UTM по фактической застройке.

    Центр — геокодер Nominatim; охват — связная область GHSL 2020 (доля ≥ ghsl_min, разрывы до ~0,5 км
    сшиваются), содержащая центр или ближайшая к нему, плюс буфер buffer_m. Возвращает (grid, info)."""
    from scipy import ndimage as ndi
    lon, lat, disp = LB.geocode(name, log=log)
    dlat = search_km / 111.32
    dlon = search_km / (111.32 * math.cos(math.radians(lat)))
    coarse = utm_grid_for((lon - dlon, lat - dlat, lon + dlon, lat + dlat), 90.0)
    g = np.nan_to_num(LB.ghsl_epoch(coarse, 2020))
    m = ndi.binary_closing(g >= ghsl_min, np.ones((3, 3), bool), iterations=3)
    lab, n = ndi.label(m, np.ones((3, 3)))
    if not n:
        raise RuntimeError(f'GHSL не видит застройки около «{name}»')
    H, W = m.shape
    cr, cc = H // 2, W // 2
    if lab[cr, cc]:
        k = lab[cr, cc]
    else:
        idx = np.argwhere(lab > 0)
        d = (idx[:, 0] - cr) ** 2 + (idx[:, 1] - cc) ** 2
        k = lab[tuple(idx[np.argmin(d)])]
    rr, cc_ = np.nonzero(lab == k)
    gt = _gt(coarse)
    x0, x1 = gt[0] + cc_.min() * gt[1] - buffer_m, gt[0] + (cc_.max() + 1) * gt[1] + buffer_m
    y1, y0 = gt[3] + rr.min() * gt[5] + buffer_m, gt[3] + (rr.max() + 1) * gt[5] - buffer_m
    if (x1 - x0) > max_km * 1000 or (y1 - y0) > max_km * 1000:
        raise RuntimeError('охват города больше допустимого; задайте территорию полигоном')
    x0, y0 = math.floor(x0 / res_m) * res_m, math.floor(y0 / res_m) * res_m
    x1, y1 = math.ceil(x1 / res_m) * res_m, math.ceil(y1 / res_m) * res_m
    grid = ((x0, y0, x1, y1), int(round((x1 - x0) / res_m)), int(round((y1 - y0) / res_m)), coarse[3])
    info = dict(name=name, found=disp, lon=lon, lat=lat, km=[round((x1 - x0) / 1000, 1), round((y1 - y0) / 1000, 1)])
    log(f'«{name}»: {disp}; охват {info["km"][0]}×{info["km"][1]} км')
    return grid, info


def read_user_points(path, grid):
    """5.0: точки пользователя (слой с полями label 0/1 и необязательным year) → список (r, c, label, year)."""
    from osgeo import ogr, osr
    src = ogr.Open(path.split('|')[0]); lyr = src.GetLayer(0)
    s_src = lyr.GetSpatialRef(); s_dst = osr.SpatialReference(); s_dst.SetFromUserInput(grid[3])
    for s_ in (s_src, s_dst):
        if s_ is not None:
            s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s_src, s_dst) if s_src is not None else None
    gt = _gt(grid); out = []
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName().lower() for i in range(lyr.GetLayerDefn().GetFieldCount())]
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or 'label' not in names:
            continue
        lab = f.GetField(names.index('label'))
        if lab in (None, ''):
            continue
        yr = f.GetField(names.index('year')) if 'year' in names else None
        x, y = g.GetX(), g.GetY()
        if ct:
            x, y = ct.TransformPoint(x, y)[:2]
        c, r = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
        if 0 <= r < grid[2] and 0 <= c < grid[1]:
            out.append((r, c, int(lab), int(yr) if yr not in (None, '') else None))
    return out


def write_points(path, grid, rows, cols, fields=None, layer='points'):
    """Точки в центрах пикселей → GeoPackage (поля label, year пустые — для разметки)."""
    from osgeo import ogr, osr
    drv = ogr.GetDriverByName('GPKG')
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    sr = osr.SpatialReference(); sr.SetFromUserInput(grid[3])
    lyr = ds.CreateLayer(layer, sr, ogr.wkbPoint)
    for n_, t in (('id', ogr.OFTInteger), ('label', ogr.OFTInteger), ('year', ogr.OFTInteger), ('p_built', ogr.OFTReal)):
        lyr.CreateField(ogr.FieldDefn(n_, t))
    gt = _gt(grid)
    for i, (r, c) in enumerate(zip(rows, cols)):
        f = ogr.Feature(lyr.GetLayerDefn())
        pt = ogr.Geometry(ogr.wkbPoint); pt.AddPoint_2D(gt[0] + (c + .5) * gt[1], gt[3] + (r + .5) * gt[5])
        f.SetGeometry(pt); f.SetField('id', i + 1)
        if fields and 'p_built' in fields:
            f.SetField('p_built', float(fields['p_built'][i]))
        lyr.CreateFeature(f)
    ds = None
    return path


def make_label_points(out_dir, n=40, seed=0, purpose='train'):
    """По готовому результату — точки для разметки пользователем (поле label: 1 — застройка, 0 — нет).

    purpose='train' — где модель сомневается сильнее всего (последний год): их добавляют в расчёт как точки
    пользователя (5.0); purpose='check' — 5.2: случайные точки поровну в «застроено» и «не застроено» по карте;
    их передают как точки проверки — в обучение они не входят, а отчёт покажет κ и площадь с 95 % ДИ."""
    from osgeo import gdal
    pp = os.path.join(out_dir, 'Prob_built_by_year.tif')
    ds = gdal.Open(pp)
    p = ds.GetRasterBand(ds.RasterCount).ReadAsArray().astype(np.float32)
    ds = None
    p[p == 255] = np.nan
    p /= 100.0
    grid = grid_from_raster(pp)
    if purpose == 'check':
        ds = gdal.Open(os.path.join(out_dir, 'BuiltYear.tif')); yr = ds.GetRasterBand(1).ReadAsArray(); ds = None
        rows, cols, _ = RF.stratified_points(yr, n=n, seed=seed + 1)
        path = os.path.join(out_dir, 'Points_to_check.gpkg'); layer = 'to_check'
    else:
        rows, cols = RF.uncertainty_points(p, n=n, seed=seed)
        path = os.path.join(out_dir, 'Points_to_label.gpkg'); layer = 'to_label'
    write_points(path, grid, rows, cols, fields=dict(p_built=[p[r, c] for r, c in zip(rows, cols)]), layer=layer)
    return path


def _feature_sets(spec, ctx, s2, temporal):
    """Наборы признаков года: 'A' — Landsat (всегда), 'B' — Landsat + Sentinel-2 (если есть)."""
    out = {}
    out['A'] = RF.features(spec, ctx, None, temporal)
    if s2 is not None:
        out['B'] = RF.features(spec, ctx, s2, temporal)
    return out


def _combine(vals, mode):
    """Вероятности/доли разных наборов → одна величина. mode: 'blend' — среднее, 'b' — S2, если есть."""
    if len(vals) == 1:
        return vals['A']
    if mode == 'b':
        return np.where(np.isfinite(vals['B']), vals['B'], vals['A'])
    if mode == 'a':
        return vals['A']
    a, b = vals['A'], vals['B']
    return np.where(np.isfinite(b), (a + np.nan_to_num(b)) / np.where(np.isfinite(a), 2, 1), a)


def run_series(out_dir, grid, years, *, roi_mask=None, composites_dir=None, protect_points=None,
               n_trees=200, n_per_class=2000, min_run=3, min_area_ha=0.5, report_years=None,
               log=print, progress=None, cancel=None, sources=None, workers=6,
               model='auto', use_s2=False, s2_mode='blend', cost='linear', weight_obs=True, temporal=True,
               fraction=True, user_points=None, growth_since=None, html_report=True, title=None, label_opts=None, mode=None, calibrate=False,
               check_points=None, edge_policy='strict', sentinel_series=False, protect_radius_px=None,
               min_coverage=QUALITY_MIN_COVERAGE):
    """Полный цикл. Возвращает dict с путями и сводкой. Промежуточные данные кэшируются.

    5.0: model ('auto'|'hgb'|'rf'); use_s2 — признаки Sentinel-2 10 м с 2018 г. (по умолчанию выключено:
    на 6 городах выигрыша не дало, +5 мин загрузки на город); s2_mode — как сводить
    модели с S2 и без ('blend'|'b'|'a'); cost/weight_obs — год появления с весом наблюдений;
    fraction — доля застроенной поверхности по годам; user_points — [(r, c, label, year|None)];
    growth_since — год, с которого строится слой новой застройки (по умолчанию первый год ряда);
    calibrate — 5.2: подбирать порог «застроено» по точкам пользователя (перекрёстно: модель без половины точек
    оценивается на ней). По умолчанию выключено: на 10 городах среднюю κ не улучшило (0,798 → 0,782); check_points — 5.2: размеченные проверочные точки [(r, c, label, year|None)] — в обучение не
    входят, по ним в отчёт пишутся κ, общая точность и площадь с 95 % ДИ (Olofsson, страты — классы карты);
    mode — определение застройки: 'strict' (по умолчанию) или 'territory' (как в 5.0);
    label_opts — параметры меток вручную (см. urban_auto_rf.auto_labels), перекрывают mode;
    min_coverage — 5.5.1: композит, покрывающий меньшую долю территории, исключается из ряда
    (см. composite_quality); 0 — не исключать."""
    t_start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    if sentinel_series and min(years) < IM.S2_FIRST_YEAR:
        raise ValueError('Для ряда Sentinel-2 выберите годы начиная с 2018.')
    comp_dir = composites_dir or os.path.join(out_dir, 'composites_s2' if sentinel_series else 'composites')
    prog = progress or (lambda p: None)
    stop = cancel or (lambda: False)

    def check():
        if stop():
            raise RuntimeError('Отменено.')
    timing = {}
    t_mark = [time.time()]

    def tick(name):
        timing[name] = round(time.time() - t_mark[0], 1); t_mark[0] = time.time()
    bbox = LB._bbox_wgs(grid)
    proto = dict(version=PLUGIN_VERSION, core=CORE, module='urbanauto', started=datetime.datetime.now().isoformat(timespec='seconds'), years=list(years),
                 grid=dict(bounds=list(grid[0]), width=grid[1], height=grid[2], crs=grid[3]),
                 n_trees=n_trees, n_per_class=n_per_class, min_run=min_run, min_area_ha=min_area_ha,
                 model=model, use_s2=use_s2, s2_mode=s2_mode, cost=cost, weight_obs=weight_obs, temporal=temporal)

    # 1. композиты Landsat (+ Sentinel-2)
    from osgeo import gdal, osr
    sensor = 'Sentinel-2' if sentinel_series else 'Landsat'
    proto['composite_sensor'] = sensor
    log(f'Шаг 1/6: годовые летние композиты {sensor} (Planetary Computer)...')
    missing = [y for y in years if not os.path.exists(os.path.join(comp_dir, f'Composite_{y}.tif'))]
    if missing:
        download = IM.download_s2_composites if sentinel_series else IM.download_series
        download(bbox, grid, missing, comp_dir, log=log, cancel=stop, workers=workers)
    spectra, clear = {}, {}
    for y in years:
        p = os.path.join(comp_dir, f'Composite_{y}.tif')
        if os.path.exists(p):
            ds = gdal.Open(p)
            crs = osr.SpatialReference()
            crs.SetFromUserInput(grid[3])
            crs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            actual = ds.GetSpatialRef() if ds else None
            if not actual or not crs.IsSame(actual) or (ds.RasterXSize, ds.RasterYSize) != tuple(grid[1:3]) or not np.allclose(ds.GetGeoTransform(), _gt(grid), rtol=0, atol=1e-7):
                raise ValueError(f'Сетка композита за {y} отличается от заданной: {p}')
            if (ds.GetMetadataItem('HARMONIZED') == 'S2_L2A') != sentinel_series:
                raise ValueError(f'Композит за {y} не соответствует выбранному источнику {sensor}: {p}')
            ds = None
            arr = IM.read_harmonized(p, y)
            spectra[y] = RF.spectral(arr)
            clear[y] = arr[6] if arr.shape[0] > 6 else np.full(arr.shape[1:], 3, np.float32)
    quality = composite_quality(spectra, clear, roi_mask, min_coverage)
    excluded = [y for y, q in sorted(quality.items()) if q['status'] == 'excluded']
    for y in excluded:
        log(f"Композит {y} исключён: данные только на {quality[y]['coverage']:.0%} территории "
            f"(порог {min_coverage:.0%}, медиана чистых сцен {quality[y]['scenes']:g}).")
        spectra.pop(y, None); clear.pop(y, None)
    for y, q in sorted(quality.items()):
        if q['status'] in ('partial', 'low'):
            log(f"Композит {y}: {'низкое' if q['status'] == 'low' else 'частичное'} покрытие {q['coverage']:.0%} "
                f"(медиана чистых сцен {q['scenes']:g}) — на пикселях без данных год появления определяется по соседним "
                "годам; годовые «сырые» площади этого года не приводятся.")
    proto['composite_quality'] = quality
    proto['excluded_years'] = excluded
    years = sorted(spectra)
    if len(years) < 3:
        raise RuntimeError('Композитов меньше трёх — нечего анализировать.')
    proto['years_with_data'] = years
    s2 = {}
    if use_s2 and years[-1] >= IM.S2_FIRST_YEAR:
        log('Sentinel-2: признаки 10 м внутри пикселя (с 2018 г.)...')
        try:
            proto['s2'] = IM.download_s2_series(bbox, grid, [y for y in years if y >= IM.S2_FIRST_YEAR], comp_dir,
                                                log=log, cancel=stop, workers=workers)
        except Exception as ex:
            proto['s2'] = 'ошибка: ' + str(ex)[:200]
            log('Sentinel-2 недоступен: ' + str(ex)[:150])
        for y in years:
            a = IM.load_s2(comp_dir, y)
            if a is not None and a.shape[1:] == (grid[2], grid[1]):
                s2[y] = a
    proto['s2_years'] = sorted(s2)
    tick('imagery')
    prog(25)
    check()

    # 2. источники меток
    log('Шаг 2/6: источники автоматических меток (GHSL, WSF, ESA, JRC, OSM, Microsoft)...')
    if sources is None:
        sources, srep = LB.fetch_sources(grid, log=log, cancel=stop, cache_dir=comp_dir)
    else:
        srep = {k: 'передано' for k in sources}
    proto['label_sources'] = srep
    lo = dict(label_opts if label_opts is not None else LABEL_MODES.get(mode or 'strict', LABEL_OPTS))
    proto['label_opts'] = lo
    hint = recommend_profile(sources)
    if hint:
        hint['used'] = 'manual' if label_opts is not None else (mode or 'strict')
        proto['profile_hint'] = hint
        if hint['used'] in ('strict', 'territory') and hint['used'] != hint['recommended']:
            names = dict(strict='строгое (здания и покрытия)', territory='территория (профиль «Высокая полнота»)')
            log(f"Подсказка: пиковая доля застройки GHSL {hint['ghsl_peak']:.2f} — для такой территории на проверочных "
                f"городах обычно точнее определение «{names[hint['recommended']]}». Выбрано: {names[hint['used']]}.")
    bld = RF.buildings_mask(sources, bld_frac_min=lo.get('bld_frac_min'))
    tick('sources')
    prog(35)

    # 3. обучение (признаки года считаются на лету — в памяти только выборка)
    log('Шаг 3/6: метки и обучение моделей...')
    ctx = RF.series_context(spectra)
    prot = None
    shape = spectra[years[0]]['ndvi'].shape
    if protect_points:
        if protect_radius_px is None:
            crs = osr.SpatialReference()
            crs.SetFromUserInput(grid[3])
            pixel_m = abs(_gt(grid)[1])
            if crs.IsGeographic():
                pixel_m *= 111320 * math.cos(math.radians((grid[0][1] + grid[0][3]) / 2))
            else:
                pixel_m *= crs.GetLinearUnits()
            protect_radius_px = max(4, round(120 / pixel_m))
        proto['protect_radius_px'] = protect_radius_px
        prot = RF.protect_mask(shape, protect_points[0], protect_points[1], protect_radius_px)
    up = list(user_points or [])
    samp = {'A': [], 'B': []}; fsamp = {'A': [], 'B': []}; extra = {'A': ([], [], []), 'B': ([], [], [])}
    prow = {'A': {}, 'B': {}}             # 5.2: признаки точки пользователя в год её метки (для подбора порога)
    counts, names = {}, {}
    for y in years:
        fs = _feature_sets(spectra[y], ctx, s2.get(y), temporal)
        lab = RF.auto_labels(y, spectra[y], sources, protect=prot, **lo)
        counts[y] = {RF.CLASSES[c]: int((lab == c).sum()) for c in range(4)}
        tgt = RF.fraction_target(y, lab, sources) if fraction else None
        for k, (X, nm) in fs.items():
            names[k] = nm
            samp[k].append(RF.sample_training([(X, lab)], n_per_class=n_per_class, seed=y))
            if fraction:
                fsamp[k].append(RF.sample_fraction([(X, tgt)], n=n_per_class * 2, seed=y))
            for i, (r, c, lb, yy) in enumerate(up):   # точки пользователя: застройка — с года и позже, нет — до года
                yy = yy or years[-1]
                if (lb == 1 and y >= yy) or (lb == 0 and y <= yy):
                    extra[k][0].append(X[r, c]); extra[k][1].append(RF.BUILT if lb == 1 else RF.VEG); extra[k][2].append(i)
                if y == yy or (y == years[-1] and yy > years[-1]):
                    prow[k][i] = X[r, c].copy()
        check()
    n_built = sum(c['built'] for c in counts.values())
    if n_built < 200:
        raise RuntimeError(f'Слишком мало автоматических меток застройки ({n_built}). Проверьте источники: {srep}')
    models, fmodels = {}, {}
    oof = {'A': {}, 'B': {}}
    for k in ('A', 'B'):
        if not samp[k]:
            continue
        Xt = np.concatenate([a for a, _ in samp[k]]); yt = np.concatenate([b for _, b in samp[k]])
        if len(np.unique(yt)) < 2 or len(yt) < 500:
            log(f'Набор признаков {k}: мало обучающих пикселей ({len(yt)}) — пропущен')
            continue
        auto_X, auto_y = Xt, yt
        Xt, yt = _with_points(auto_X, auto_y, extra[k])
        models[k] = RF.train(Xt, yt, n_estimators=n_trees, kind=model)
        log(f'Модель {k} ({models[k].kind_}): обучающих {len(yt)}, точность {models[k].quality_:.3f}')
        if calibrate and len(prow[k]) >= 10:
            # перекрёстная оценка: модель без половины точек → вероятность на этой половине
            for half in (0, 1):
                keep = [j for j, i in enumerate(extra[k][2]) if i % 2 != half]
                Xh, yh = _with_points(auto_X, auto_y, ([extra[k][0][j] for j in keep], [extra[k][1][j] for j in keep], None))
                mh = RF.train(Xh, yh, n_estimators=n_trees, kind=model)
                idx = [i for i in prow[k] if i % 2 == half]
                if idx:
                    pr = mh.predict_proba(np.asarray([prow[k][i] for i in idx], np.float32))
                    full = np.zeros((len(idx), len(RF.CLASSES)), np.float32); full[:, mh.classes_.astype(int)] = pr
                    for i, row in zip(idx, full):
                        oof[k][i] = (row[RF.BUILT], row[RF.BARE])
        if fraction and fsamp[k]:
            Xf = np.concatenate([a for a, _ in fsamp[k]]); tf = np.concatenate([b for _, b in fsamp[k]])
            if len(tf) >= 500:
                fmodels[k] = RF.train_fraction(Xf, tf)
        check()
    del samp, fsamp
    tick('train')
    proto.update(label_counts=counts, models={k: dict(kind=m.kind_, quality=round(m.quality_, 4), features=names[k])
                                               for k, m in models.items()},
                 oob=round(models['A'].quality_, 4), user_points=len(up))
    if getattr(models['A'], 'feature_importances_', None) is not None:
        proto['importance'] = {n_: round(float(v), 4) for v, n_ in
                               sorted(zip(models['A'].feature_importances_, names['A']), reverse=True)}
    prog(50)

    # 4. прогноз по годам
    log('Шаг 4/6: вероятности и доля застройки по годам...')
    T = len(years)
    pb = np.full((T,) + shape, np.nan, np.float32); pbare = np.full_like(pb, np.nan)
    frac = np.full_like(pb, np.nan) if fmodels else None
    def _pred(y):
        fs = _feature_sets(spectra[y], ctx, s2.get(y), temporal)
        vb, vg, vf = {}, {}, {}
        for k, (X, _) in fs.items():
            if k not in models:
                continue
            pr = RF.predict(models[k], X)
            vb[k], vg[k] = pr[..., RF.BUILT], pr[..., RF.BARE]
            if k in fmodels:
                vf[k] = RF.predict_fraction(fmodels[k], X)
        del fs
        return _combine(vb, s2_mode), _combine(vg, s2_mode), (_combine(vf, s2_mode) if vf else None)
    # годы считаются параллельно: прогноз деревьев и фильтры numpy/scipy отпускают GIL
    from concurrent.futures import ThreadPoolExecutor
    n_par = max(1, min(4, (os.cpu_count() or 2) // 2))
    done = 0
    with ThreadPoolExecutor(n_par) as ex:
        for i, (a, b, f) in enumerate(ex.map(_pred, years)):
            pb[i], pbare[i] = a, b
            if frac is not None and f is not None:
                frac[i] = f
            done += 1
            prog(50 + 30 * done / T)
            check()
    proto['predict_threads'] = n_par
    inroi_q = roi_mask if roi_mask is not None else np.ones(shape, bool)
    share = []
    for i in range(T):
        v = np.isfinite(pb[i]) & inroi_q
        share.append(float((pb[i][v] >= 0.5).mean()) if v.any() else float('nan'))
    flag_anomalies(quality, years, share)
    for y in years:
        if quality[y]['status'] == 'anomaly':
            log(f"Внимание: {y} — доля застройки среди пикселей с данными в {quality[y]['ratio_to_neighbours']:.2f} раза "
                "отличается от соседних лет. Проверьте композит (облака, дым, снег, полосы SLC-off).")
    tick('predict')
    # 5. год появления
    peff = RF.effective_built_prob(pb, pbare, bld)
    thr = None
    if calibrate and oof['A']:
        k = 'B' if oof['B'] and s2_mode == 'b' else 'A'
        pts, ps = [], []
        for i, (pbi, pgi) in oof[k].items():
            r, c, lb, _ = up[i]
            pe = min(1.0, pbi + pgi) if (bld is not None and bld[r, c]) else pbi
            pts.append(lb); ps.append(pe)
        thr = RF.calibrate_threshold(np.asarray(ps), np.asarray(pts))
        proto['threshold'] = thr
        if thr is not None and abs(thr['t'] - 0.5) > 1e-6:
            peff = np.clip(peff + (0.5 - thr['t']), 0, 1).astype(np.float32)
            log(f"Порог по точкам пользователя: {thr['t']:.2f} (точность на отложенных точках {thr['acc0']:.2f} → {thr['acc']:.2f})")
    w = np.stack([RF.obs_weights(clear[y]) for y in years]) if weight_obs else None
    yr, conf = RF.change_year(peff, years, min_run=min_run, weights=w, cost=cost, edge_policy=edge_policy)
    proto['edge_policy'] = edge_policy
    proto['provisional_years'] = list(years[max(1, len(years) - min_run + 1):]) if edge_policy == 'adaptive' else []
    if proto['provisional_years']:
        log(f"Предварительные годы появления застройки: {proto['provisional_years']}")
    yr, refine_rep = RF.refine_new_growth(yr, peff, years)
    proto['refine'] = refine_rep
    pb = peff
    bare = RF.bare_persistent(pbare) & (yr <= 0)
    if roi_mask is not None:
        yr = np.where(roi_mask, yr, -1).astype(np.int16)
        conf = np.where(roi_mask, conf, np.nan).astype(np.float32)
        bare &= roi_mask
    tick('change_year')
    prog(85)

    # 6. выходные данные
    log('Шаг 6/6: растры, полигоны, слой новой застройки, отчёт...')
    out = {}
    out['built_year'] = os.path.join(out_dir, 'BuiltYear.tif')
    write_raster(out['built_year'], yr, grid, 'Int16', nodata=-1, names=['built_year'])
    out['confidence'] = os.path.join(out_dir, 'BuiltYear_confidence.tif')
    write_raster(out['confidence'], np.nan_to_num(conf, nan=-1).astype(np.float32), grid, nodata=-1)
    out['prob_built'] = os.path.join(out_dir, 'Prob_built_by_year.tif')
    write_raster(out['prob_built'], [np.nan_to_num(p * 100, nan=255).astype(np.uint8) for p in pb], grid, 'Byte',
                 nodata=255, names=years)
    if frac is not None:
        out['fraction'] = os.path.join(out_dir, 'Built_fraction_by_year.tif')
        write_raster(out['fraction'], [np.nan_to_num(f * 100, nan=255).astype(np.uint8) for f in frac], grid, 'Byte',
                     nodata=255, names=years)
    out['bare'] = os.path.join(out_dir, 'BareSoil_persistent.tif')
    write_raster(out['bare'], bare.astype(np.uint8), grid, 'Byte', nodata=0, names=['bare_persistent'])
    area = pixel_area_km2(grid)
    inroi = roi_mask if roi_mask is not None else np.ones(shape, bool)
    rows, prev = [], None
    for i, y in enumerate(years):
        m = RF.built_mask_for(yr, y)
        a = float(area[m].sum())
        bare_y = (pbare[i] >= 0.5) & (yr <= 0) & inroi
        row = dict(year=y, built_km2=round(a, 4), growth_km2=None if prev is None else round(a - prev, 4),
                   new_built_km2=round(float(area[yr == y].sum()), 4),
                   bare_km2=round(float(area[bare_y].sum()), 4),
                   raw_built_km2=round(float(area[(pb[i] >= 0.5) & inroi].sum()), 4))
        if frac is not None:
            f = np.where(inroi, np.nan_to_num(frac[i]), 0)
            row['built_surface_km2'] = round(float((f * area).sum()), 4)
        q = quality.get(y, {})
        row['coverage_pct'] = round(100 * q.get('coverage', 1.0), 1)
        row['quality'] = q.get('status', 'ok')
        if row['quality'] in ('partial', 'low'):   # «сырые» величины по неполному снимку несопоставимы с соседними годами
            for k_ in ('bare_km2', 'raw_built_km2', 'built_surface_km2'):
                if k_ in row:
                    row[k_] = None
        rows.append(row)
        prev = a
    for y in excluded:                       # год без пригодного композита: только накопленная площадь
        if not years[0] < y < years[-1]:
            continue
        a = float(area[RF.built_mask_for(yr, y)].sum())
        row = dict(year=y, built_km2=round(a, 4), growth_km2=None, new_built_km2=None, bare_km2=None, raw_built_km2=None)
        if frac is not None:
            row['built_surface_km2'] = None
        row.update(coverage_pct=round(100 * quality[y]['coverage'], 1), quality='excluded')
        rows.append(row)
    rows.sort(key=lambda r: r['year'])
    for i, r in enumerate(rows):            # прирост пересчитывается по полному ряду лет
        r['growth_km2'] = None if i == 0 else round(r['built_km2'] - rows[i - 1]['built_km2'], 4)
    out['csv'] = os.path.join(out_dir, 'annual_series.csv')
    with open(out['csv'], 'w', newline='', encoding='utf-8-sig') as f:
        w_ = csv.DictWriter(f, list(rows[0]), delimiter=';'); w_.writeheader(); w_.writerows(rows)
    from .urban_auto_temporal import polygonize
    gg = (grid[1], grid[2], _gt(grid), _wkt(grid))
    from scipy import ndimage as ndi
    min_km2 = min_area_ha * 0.01
    out['polygons'] = {}
    for y in (report_years or [years[0], years[-1]]):
        if y not in years:
            continue
        m = _drop_small(RF.built_mask_for(yr, y), area, min_km2)
        p = os.path.join(out_dir, f'Urban_{y}.gpkg')
        polygonize(m, gg, p, f'urban_{y}', y)
        out['polygons'][y] = p
    p = os.path.join(out_dir, 'BareSoil_persistent.gpkg')
    polygonize(bare, gg, p, 'bare', years[-1]); out['bare_polygons'] = p
    since = growth_since or years[0]
    out['growth'] = os.path.join(out_dir, f'New_built_{since + 1}_{years[-1]}.gpkg')
    proto['growth_patches'] = growth_polygons(yr, conf, grid, since, out['growth'], area, min_km2)
    acc = None
    if check_points:
        acc = points_accuracy(yr, check_points, years, area, inroi)
        proto['accuracy'] = acc
    tick('outputs')
    proto.update(timing=timing, series=rows, outputs=out, sec=round(time.time() - t_start, 1),
                 finished=datetime.datetime.now().isoformat(timespec='seconds'))
    out['protocol'] = os.path.join(out_dir, 'rf_series_protocol.json')
    with open(out['protocol'], 'w', encoding='utf-8') as f:
        json.dump(proto, f, ensure_ascii=False, indent=1, default=str)
    if html_report:
        out['report'] = os.path.join(out_dir, 'report.html')
        try:
            from .urban_auto_report import write_report
            write_report(out['report'], proto, title=title,
                         accuracy={int(y): v for y, v in acc.items()} if acc else None)
        except Exception as ex:
            log('Отчёт не записан: ' + str(ex)[:150])
    prog(100)
    log(f'Готово за {proto["sec"]:.0f} с. Застройка {years[0]}: {rows[0]["built_km2"]:.2f} км², '
        f'{years[-1]}: {rows[-1]["built_km2"]:.2f} км².')
    return dict(outputs=out, series=rows, protocol=proto)


def _with_points(Xa, ya, extra, repeat=20):
    """Автоматическая выборка + точки пользователя (каждая повторяется repeat раз)."""
    if not extra[1]:
        return Xa, ya
    xe = np.asarray(extra[0], np.float32); ye = np.asarray(extra[1], np.int8)
    ok = np.isfinite(xe).all(1)
    return np.concatenate([Xa, np.repeat(xe[ok], repeat, 0)]), np.concatenate([ya, np.repeat(ye[ok], repeat)])


def points_accuracy(yr, pts, years, area, inroi=None):
    """5.2: точность по проверочным точкам для каждого года, к которому есть метки. Страты — «застроено» и
    «не застроено» по карте этого года (Olofsson et al., 2014). Возвращает {год: dict(kappa, oa, area_km2, ci95_km2,
    map_km2, n)}."""
    from .urban_auto_report import olofsson
    out = {}
    by_year = {}
    for r, c, lb, yy in pts:
        if lb in (0, 1):
            by_year.setdefault(yy or years[-1], []).append((r, c, lb))
    m_all = inroi if inroi is not None else np.ones(yr.shape, bool)
    for y, rows in sorted(by_year.items()):
        m = RF.built_mask_for(yr, y) & m_all
        W = {1: float(area[m].sum()), 0: float(area[m_all & ~m].sum())}
        mp = [int(m[r, c]) for r, c, _ in rows]; rf = [lb for _, _, lb in rows]
        if len(set(mp)) < 1 or len(rows) < 5:
            continue
        a = olofsson(mp, rf, mp, W)
        out[int(y)] = dict(kappa=round(a['kappa'], 3), oa=round(a['oa'], 3), area_km2=round(a['area_km2'], 3),
                           ci95_km2=round(a['ci95_km2'], 3), map_km2=round(W[1], 3), n=len(rows))
    return out


def _drop_small(m, area, min_km2):
    from scipy import ndimage as ndi
    lab, n = ndi.label(m, np.ones((3, 3)))
    if not n or min_km2 <= 0:
        return m
    sz = ndi.sum(area, lab, range(1, n + 1)); keep = np.zeros(n + 1, bool); keep[1:] = np.asarray(sz) >= min_km2
    return keep[lab]


def growth_polygons(yr, conf, grid, since, path, area, min_km2=0.005):
    """5.0: слой новой застройки (год > since): пятна по году появления с полями year, area_ha,
    confidence (средняя уверенность), n_px. Возвращает число пятен."""
    from osgeo import gdal, ogr, osr
    from scipy import ndimage as ndi
    new = yr > since
    ids = np.zeros(yr.shape, np.int32)
    attrs = {}
    nid = 0
    cf_all = np.nan_to_num(conf).astype(np.float64)
    for y in np.unique(yr[new]):
        lab, n = ndi.label(yr == y, np.ones((3, 3)))
        if not n:
            continue
        flat = lab.ravel()
        sz = np.bincount(flat, weights=area.ravel(), minlength=n + 1)[1:]
        npx = np.bincount(flat, minlength=n + 1)[1:]
        cf = np.bincount(flat, weights=cf_all.ravel(), minlength=n + 1)[1:] / np.maximum(npx, 1)
        keep = np.flatnonzero(sz >= min_km2)
        if not keep.size:
            continue
        remap = np.zeros(n + 1, np.int32)
        remap[keep + 1] = nid + 1 + np.arange(keep.size)
        m = remap[lab]                                    # один проход по растру на год (было: на каждое пятно)
        ids = np.where(m > 0, m, ids)
        for j, k in enumerate(keep):
            attrs[nid + 1 + j] = (int(y), round(float(sz[k]) * 100, 3), round(float(cf[k]), 3), int(npx[k]))
        nid += keep.size
    drv = ogr.GetDriverByName('GPKG')
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    sr = osr.SpatialReference(); sr.SetFromUserInput(grid[3])
    lyr = ds.CreateLayer('new_built', sr, ogr.wkbPolygon)
    for n_, t in (('pid', ogr.OFTInteger), ('year', ogr.OFTInteger), ('area_ha', ogr.OFTReal),
                  ('confidence', ogr.OFTReal), ('n_px', ogr.OFTInteger)):
        lyr.CreateField(ogr.FieldDefn(n_, t))
    mem = gdal.GetDriverByName('MEM').Create('', grid[1], grid[2], 1, gdal.GDT_Int32)
    mem.SetGeoTransform(_gt(grid)); mem.SetProjection(_wkt(grid))
    b = mem.GetRasterBand(1); b.WriteArray(ids); b.SetNoDataValue(0)
    gdal.Polygonize(b, b, lyr, 0, [], callback=None)
    lyr.ResetReading()
    for f in lyr:
        a = attrs.get(f.GetField('pid'))
        if a:
            f.SetField('year', a[0]); f.SetField('area_ha', a[1]); f.SetField('confidence', a[2]); f.SetField('n_px', a[3])
            lyr.SetFeature(f)
    ds = None
    return nid
