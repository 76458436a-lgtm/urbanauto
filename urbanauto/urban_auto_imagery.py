# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Годовые летние композиты Landsat Collection 2 Level-2 из Microsoft Planetary Computer.

Для каждого года ищутся сцены Landsat 5/7/8/9 за летний сезон (по умолчанию 1 июня – 31 августа),
по маске QA_PIXEL убираются заполнение, облака (в т. ч. расширенные), тени и снег, затем
по каждому пикселю берётся медиана отражения по всем чистым наблюдениям.

Результат: Composite_{год}.tif — 6 каналов отражения (blue, green, red, nir, swir1, swir2,
масштаб 0–1) + 7-й канал — число чистых наблюдений. NoData = -9999.

Работает в среде QGIS (GDAL с /vsicurl/, requests). Сеть нужна только к
planetarycomputer.microsoft.com и хранилищу *.blob.core.windows.net.
"""
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

STAC = 'https://planetarycomputer.microsoft.com/api/stac/v1/search'
TOKEN = 'https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2'
BANDS = ('blue', 'green', 'red', 'nir08', 'swir16', 'swir22')
NODATA = -9999.0
# QA_PIXEL: 0 fill, 1 dilated cloud, 3 cloud, 4 cloud shadow, 5 snow
QA_BAD = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 4) | (1 << 5)
try:
    from .urban_auto_version import USER_AGENT as UA
except ImportError:            # модуль загружен отдельно (тесты, скрипты)
    UA = 'QGIS-UrbanAuto (+https://github.com/76458436a-lgtm/urbanauto)'
HEADERS = {'User-Agent': UA}
# Приведение отражения TM/ETM+ к шкале OLI (Roy et al., 2016, Remote Sens. Environ. 185:57–70, табл. 2, OLS):
# OLI = a + b · ETM+ для blue, green, red, nir, swir1, swir2
ETM_TO_OLI = ((0.0003, 0.8474), (0.0088, 0.8483), (0.0061, 0.9047), (0.0412, 0.8462), (0.0254, 0.8937), (0.0172, 0.9071))


def harmonize_to_oli(arr):
    """(>=6,H,W) отражение TM/ETM+ → шкала OLI; NoData и NaN сохраняются."""
    out = np.array(arr, np.float32, copy=True)
    for i, (a, b) in enumerate(ETM_TO_OLI):
        band = out[i]
        ok = np.isfinite(band) & (band != NODATA)
        band[ok] = a + b * band[ok]
    return out


def platforms_for(year):
    """Спутники, данные которых пригодны в данном году (L7 после 2003 — с полосами SLC-off)."""
    p = []
    if year <= 2011:
        p.append('landsat-5')
    if year >= 1999:
        p.append('landsat-7')
    if year >= 2013:
        p.append('landsat-8')
    if year >= 2022:
        p.append('landsat-9')
    if year >= 2013:  # хватает L8/9; L7 SLC-off только ухудшает медиану
        p = [x for x in p if x != 'landsat-7']
    return p


class _Token:
    def __init__(self, requests, url=TOKEN):
        self.r, self.url, self.value, self.t = requests, url, None, 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.time() - self.t > 1800:
                self.value = self.r.get(self.url, headers=HEADERS, timeout=30).json()['token']
                self.t = time.time()
            return self.value


def search_scenes(bbox, year, months=(6, 8), max_cloud=70, limit=200, requests=None):
    import requests as _rq
    requests = requests or _rq
    w, s, e, n = bbox
    q = {'collections': ['landsat-c2-l2'],
         'intersects': {'type': 'Polygon', 'coordinates': [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
         'datetime': f'{year}-{months[0]:02d}-01T00:00:00Z/{year}-{months[1]:02d}-'
                     f'{(31 if months[1] in (1, 3, 5, 7, 8, 10, 12) else 30):02d}T23:59:59Z',
         'query': {'eo:cloud_cover': {'lt': max_cloud}, 'platform': {'in': platforms_for(year)}},
         'limit': limit}
    feats = requests.post(STAC, json=q, headers=HEADERS, timeout=90).json().get('features', [])
    feats.sort(key=lambda f: f['properties']['eo:cloud_cover'])
    return feats


def _read(href, token, grid, resample):
    from osgeo import gdal
    bounds, width, height, crs = grid
    url = '/vsicurl/' + href + ('&' if '?' in href else '?') + token
    ds = gdal.Warp('', url, format='MEM', outputBounds=bounds, width=width, height=height,
                   dstSRS=crs, resampleAlg=resample, dstNodata=0, srcNodata=0)
    if ds is None:
        raise RuntimeError('GDAL не прочитал ' + href.split('?')[0][-60:])
    a = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return a


def scene_stack(feature, token, grid):
    """Читает одну сцену на сетку grid → (6, H, W) float32 с NaN на плохих пикселях, число чистых."""
    qa = _read(feature['assets']['qa_pixel']['href'], token, grid, 'near').astype(np.uint32)
    good = (qa != 0) & ((qa & QA_BAD) == 0)
    if good.sum() < 0.02 * good.size:
        return None, 0
    out = np.full((len(BANDS),) + qa.shape, np.nan, np.float32)
    for i, b in enumerate(BANDS):
        dn = _read(feature['assets'][b]['href'], token, grid, 'bilinear').astype(np.float32)
        refl = dn * 2.75e-05 - 0.2
        ok = good & (dn > 0) & (refl > -0.05) & (refl < 1.2)
        if feature['properties'].get('platform') in ('landsat-5', 'landsat-7'):
            a, b = ETM_TO_OLI[i]
            refl = a + b * refl
        out[i][ok] = refl[ok]
    valid = ~np.isnan(out).any(0)
    out[:, ~valid] = np.nan
    return out, int(valid.sum())


def annual_composite(bbox, grid, year, *, max_scenes=10, months=(6, 8), workers=6, log=print,
                     cancel=None, max_cloud=70):
    import requests
    tok = _Token(requests)
    feats = search_scenes(bbox, year, months=months, max_cloud=max_cloud, requests=requests)
    if not feats and months == (6, 8):
        feats = search_scenes(bbox, year, months=(5, 9), max_cloud=max_cloud, requests=requests)
    feats = feats[:max_scenes]
    log(f'{year}: найдено сцен {len(feats)}')
    stacks, used, errors = [], [], []

    def one(f):
        if cancel and cancel():
            return None
        for attempt in range(3):
            try:
                arr, n = scene_stack(f, tok.get(), grid)
                return f, arr, n
            except Exception as ex:  # сеть: повторяем
                err = str(ex)[:160]
                time.sleep(2 + 3 * attempt)
        errors.append(f"{f['id']}: {err}")
        return None

    with ThreadPoolExecutor(workers) as ex:
        for res in ex.map(one, feats):
            if res and res[1] is not None and res[2] > 0:
                f, arr, n = res
                stacks.append(arr)
                used.append(dict(id=f['id'], date=f['properties']['datetime'][:10],
                                 platform=f['properties']['platform'],
                                 cloud=f['properties']['eo:cloud_cover'], clear_px=n))
    if not stacks:
        return None, dict(year=year, scenes=used, errors=errors)
    cube = np.stack(stacks)                       # (n, 6, H, W)
    count = (~np.isnan(cube[:, 0])).sum(0).astype(np.float32)
    covered = count > 0
    med = np.full(cube.shape[1:], NODATA, dtype=np.float32)
    med[:, covered] = np.nanmedian(cube[:, :, covered], axis=0)
    return np.concatenate([med, count[None]]), dict(year=year, scenes=used, errors=errors,
                                                     clear_fraction=float((count > 0).mean()))


def write_composite(path, arr, grid, sensor='OLI_ROY2016'):
    from osgeo import gdal, osr
    bounds, width, height, crs = grid
    drv = gdal.GetDriverByName('GTiff')
    ds = drv.Create(path, width, height, arr.shape[0], gdal.GDT_Float32,
                    options=['COMPRESS=DEFLATE', 'PREDICTOR=3', 'TILED=YES'])
    ds.SetGeoTransform((bounds[0], (bounds[2] - bounds[0]) / width, 0, bounds[3], 0, -(bounds[3] - bounds[1]) / height))
    srs = osr.SpatialReference(); srs.SetFromUserInput(crs)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetMetadataItem('HARMONIZED', sensor)
    names = list(BANDS) + ['clear_count']
    for i in range(arr.shape[0]):
        b = ds.GetRasterBand(i + 1)
        b.WriteArray(arr[i]); b.SetDescription(names[i])
        if i < len(BANDS):
            b.SetNoDataValue(NODATA)
    ds = None


def download_series(bbox, grid, years, out_dir, *, log=print, cancel=None, workers=6, max_scenes=10,
                    status_path=None):
    """Скачивает композиты за каждый год; уже готовые годы пропускаются. Возвращает сводку."""
    os.makedirs(out_dir, exist_ok=True)
    summary = dict(started=datetime.datetime.now().isoformat(timespec='seconds'), bbox=list(bbox),
                   grid=dict(bounds=list(grid[0]), width=grid[1], height=grid[2], crs=grid[3]), years={})
    status_path = status_path or os.path.join(out_dir, 'composites_status.json')
    if os.path.exists(status_path):
        try:
            summary['years'] = json.load(open(status_path, encoding='utf-8')).get('years', {})
        except Exception:
            pass

    def save():
        with open(status_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    for y in years:
        if cancel and cancel():
            break
        path = os.path.join(out_dir, f'Composite_{y}.tif')
        if os.path.exists(path) and summary['years'].get(str(y), {}).get('status') == 'ok':
            continue
        t0 = time.time()
        try:
            arr, info = annual_composite(bbox, grid, y, log=log, cancel=cancel, workers=workers,
                                         max_scenes=max_scenes)
            if arr is None:
                info['status'] = 'no_data'
            else:
                write_composite(path, arr, grid)
                info['status'] = 'ok'
        except Exception as ex:
            info = dict(year=y, status='error', error=str(ex)[:300])
        info['sec'] = round(time.time() - t0, 1)
        summary['years'][str(y)] = info
        log(f"{y}: {info['status']} за {info['sec']} с, сцен {len(info.get('scenes', []))}, "
            f"покрытие {info.get('clear_fraction', 0):.0%}")
        save()
    summary['finished'] = datetime.datetime.now().isoformat(timespec='seconds')
    save()
    return summary


def read_harmonized(path, year):
    """Читает композит; файлы без отметки HARMONIZED за годы до 2013 (только TM/ETM+) приводятся к OLI."""
    from osgeo import gdal
    ds = gdal.Open(path)
    arr = ds.ReadAsArray().astype(np.float32)
    tag = ds.GetMetadataItem('HARMONIZED')
    ds = None
    if not tag and year <= 2012:
        arr[:6] = harmonize_to_oli(arr[:6])
    return arr


def indices_from_composite(arr):
    """(7,H,W) композит → dict индексов и каналов (NaN на NoData)."""
    a = arr[:6].astype(np.float32).copy()
    a[a == NODATA] = np.nan
    blue, green, red, nir, sw1, sw2 = a
    with np.errstate(all='ignore'):
        nd = lambda x, y: (x - y) / (x + y)
        out = dict(blue=blue, green=green, red=red, nir=nir, swir1=sw1, swir2=sw2,
                   ndvi=nd(nir, red), ndbi=nd(sw1, nir), mndwi=nd(green, sw1),
                   bsi=((sw1 + red) - (nir + blue)) / ((sw1 + red) + (nir + blue)),
                   nbr=nd(nir, sw2), swir_ratio=sw2 / sw1, bright=(blue + green + red) / 3)
    return out


# ============================================================================ Sentinel-2 (5.0)
# Летние композиты Sentinel-2 L2A (10 м) с 2018 г. сводятся к сетке Landsat (30 м) как признаки
# «внутри пикселя»: средние индексы и их разброс по 3×3 подпикселям 10 м, доля «непроницаемых» и
# «зелёных» подпикселей. Разброс внутри 30-метрового пикселя — главный признак редкой малоэтажной
# застройки и дач, которую Landsat смешивает с лугом.
S2_COLLECTION = 'sentinel-2-l2a'
S2_TOKEN = 'https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a'
S2_BANDS = ('B02', 'B03', 'B04', 'B08', 'B11', 'B12')
S2_SCL_BAD = (0, 1, 3, 8, 9, 10, 11)       # нет данных, насыщение, тени облаков, облака, перистые, снег
S2_FIRST_YEAR = 2018
S2_FEATURES = ('s2_ndvi', 's2_ndvi_sd', 's2_ndbi', 's2_mndwi', 's2_bright', 's2_bright_sd',
               's2_imperv_frac', 's2_veg_frac', 's2_edge', 's2_nir_sd')


def _s2_search(bbox, year, months=(6, 8), max_cloud=40, limit=100, requests=None):
    import requests as _rq
    requests = requests or _rq
    w, s, e, n = bbox
    last = 31 if months[1] in (1, 3, 5, 7, 8, 10, 12) else 30
    q = {'collections': [S2_COLLECTION],
         'intersects': {'type': 'Polygon', 'coordinates': [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
         'datetime': f'{year}-{months[0]:02d}-01T00:00:00Z/{year}-{months[1]:02d}-{last}T23:59:59Z',
         'query': {'eo:cloud_cover': {'lt': max_cloud}}, 'limit': limit}
    feats = requests.post(STAC, json=q, headers=HEADERS, timeout=90).json().get('features', [])
    feats.sort(key=lambda f: f['properties']['eo:cloud_cover'])
    return feats


def _fine_grid(grid, res=10.0):
    bounds, w, h, crs = grid
    f = max(1, int(round((bounds[2] - bounds[0]) / w / res)))
    return (bounds, w * f, h * f, crs), f


def _s2_offset(feature):
    pb = str(feature['properties'].get('s2:processing_baseline', '0'))
    try:
        return 1000.0 if float(pb) >= 4.0 else 0.0
    except ValueError:
        return 0.0


def s2_median(bbox, grid, year, *, max_scenes=6, workers=6, log=print, cancel=None):
    """Летний медианный композит Sentinel-2 L2A прямо на сетке grid (обычно 10 м):
    (6, H, W) отражение blue, green, red, nir, swir1, swir2 (NaN — нет данных) и число чистых сцен (H, W)."""
    import requests
    import warnings
    tok = _Token(requests, S2_TOKEN)
    feats = _s2_search(bbox, year, requests=requests)
    if len(feats) < 3:
        feats = _s2_search(bbox, year, months=(5, 9), max_cloud=60, requests=requests)
    feats = feats[:max_scenes]
    log(f'S2 {year}: сцен {len(feats)}, сетка {grid[1]}×{grid[2]}')
    if not feats:
        return None, None, dict(year=year, scenes=0)

    def read(feat, band, resample):
        for attempt in range(3):
            try:
                return _read(feat['assets'][band]['href'], tok.get(), grid, resample)
            except Exception:
                if cancel and cancel():
                    raise
                time.sleep(2 + 3 * attempt)
        return None
    with ThreadPoolExecutor(workers) as ex:
        scl = list(ex.map(lambda ft: read(ft, 'SCL', 'near'), feats))
    keep = [i for i, s_ in enumerate(scl) if s_ is not None]
    if not keep:
        return None, None, dict(year=year, scenes=0)
    good = np.stack([~np.isin(scl[i], S2_SCL_BAD) for i in keep])          # (n, H, W)
    del scl
    if not good.any():
        return None, None, dict(year=year, scenes=len(keep), clear=0)
    med = []
    joint_valid = good.copy()
    for band in S2_BANDS:
        if cancel and cancel():
            raise RuntimeError('Отменено.')
        with ThreadPoolExecutor(workers) as ex:
            arrs = list(ex.map(lambda i: read(feats[i], band, 'bilinear'), keep))
        st = np.full(good.shape, np.nan, np.float32)
        for j, (i, a) in enumerate(zip(keep, arrs)):
            if a is None:
                continue
            r = (a.astype(np.float32) - _s2_offset(feats[i])) / 10000.0
            ok = good[j] & (a > 0) & (r > -0.05) & (r < 1.5)
            st[j][ok] = r[ok]
        joint_valid &= np.isfinite(st)
        with np.errstate(all='ignore'), warnings.catch_warnings():
            warnings.simplefilter('ignore')
            med.append(np.nanmedian(st, 0).astype(np.float32))
        del st, arrs
    med = np.stack(med)
    count = joint_valid.sum(0).astype(np.float32)
    return med, count, dict(year=year, scenes=len(keep), clear=float((count > 0).mean()))


def s2_annual(bbox, grid, year, *, max_scenes=6, workers=6, log=print, cancel=None):
    """Летний композит Sentinel-2 → признаки на сетке grid: (len(S2_FEATURES), H, W) float32, NaN без данных."""
    fine, f = _fine_grid(grid)
    med, _, info = s2_median(bbox, fine, year, max_scenes=max_scenes, workers=workers, log=log, cancel=cancel)
    if med is None:
        return None, info
    return s2_cell_features(med, f), info


def download_s2_composites(bbox, grid, years, out_dir, *, log=print, cancel=None, workers=6, max_scenes=6):
    """5.3: годовые летние композиты Sentinel-2 в формате композитов Landsat (Composite_{год}.tif: 6 каналов +
    число чистых сцен) — на любой сетке, обычно 10 м. После этого весь годовой ряд (метки, модель, год появления)
    считается на этой сетке тем же run_series. Готовые годы пропускаются. Возвращает отчёт по годам."""
    os.makedirs(out_dir, exist_ok=True)
    rep = {}
    for y in years:
        if y < S2_FIRST_YEAR or (cancel and cancel()):
            continue
        p = os.path.join(out_dir, f'Composite_{y}.tif')
        if os.path.exists(p):
            rep[y] = 'кэш'
            continue
        t0 = time.time()
        try:
            med, count, info = s2_median(bbox, grid, y, max_scenes=max_scenes, workers=workers, log=log, cancel=cancel)
            if med is None or not count.any():
                rep[y] = 'нет данных'
                continue
            med[~np.isfinite(med)] = NODATA
            med[:, count == 0] = NODATA
            write_composite(p, np.concatenate([med, count[None]]), grid, sensor='S2_L2A')
            rep[y] = f"ok, сцен {info['scenes']}, покрытие {info['clear']:.0%}, {time.time() - t0:.0f} с"
        except Exception as ex:
            rep[y] = 'ошибка: ' + str(ex)[:150]
        log(f'S2 {y}: {rep[y]}')
    return rep


def refine_grid(grid, factor=3):
    """Сетка в factor раз мельче (30 м → 10 м) с тем же охватом."""
    bounds, w, h, crs = grid
    return bounds, w * factor, h * factor, crs


def s2_cell_features(med, f):
    """(6, H·f, W·f) медиана отражения 10 м → (K, H, W) признаки ячеек f×f."""
    blue, green, red, nir, sw1, sw2 = med
    with np.errstate(all='ignore'):
        ndvi = (nir - red) / (nir + red)
        ndbi = (sw1 - nir) / (sw1 + nir)
        mndwi = (green - sw1) / (green + sw1)
        bright = (blue + green + red) / 3
        gy, gx = np.gradient(np.nan_to_num(bright))
        edge = np.hypot(gx, gy)
        edge[~np.isfinite(bright)] = np.nan
        imperv = np.where(np.isfinite(ndvi), ((ndvi < 0.3) & (mndwi < 0)).astype(np.float32), np.nan)
        veg = np.where(np.isfinite(ndvi), (ndvi > 0.5).astype(np.float32), np.nan)

    def cells(a):
        H, W = a.shape[0] // f, a.shape[1] // f
        return a[:H * f, :W * f].reshape(H, f, W, f).transpose(0, 2, 1, 3).reshape(H, W, f * f)
    import warnings
    out = []
    with warnings.catch_warnings(), np.errstate(all='ignore'):
        warnings.simplefilter('ignore')
        for a, how in ((ndvi, 'mean'), (ndvi, 'sd'), (ndbi, 'mean'), (mndwi, 'mean'), (bright, 'mean'),
                       (bright, 'sd'), (imperv, 'mean'), (veg, 'mean'), (edge, 'mean'), (nir, 'sd')):
            c = cells(a)
            out.append(np.nanmean(c, -1) if how == 'mean' else np.nanstd(c, -1))
    return np.stack(out).astype(np.float32)


def download_s2_series(bbox, grid, years, out_dir, *, log=print, cancel=None, workers=6, max_scenes=6):
    """Признаки Sentinel-2 по годам (≥ 2018) → S2feat_{год}.npy в out_dir; готовые годы пропускаются."""
    os.makedirs(out_dir, exist_ok=True)
    rep = {}
    for y in years:
        if y < S2_FIRST_YEAR or (cancel and cancel()):
            continue
        p = os.path.join(out_dir, f'S2feat_{y}.npy')
        if os.path.exists(p):
            rep[y] = 'кэш'
            continue
        t0 = time.time()
        try:
            arr, info = s2_annual(bbox, grid, y, max_scenes=max_scenes, workers=workers, log=log, cancel=cancel)
            if arr is not None:
                np.save(p, arr.astype(np.float16))
                rep[y] = f"ok, сцен {info['scenes']}, {time.time() - t0:.0f} с"
            else:
                rep[y] = 'нет данных'
        except Exception as ex:
            rep[y] = 'ошибка: ' + str(ex)[:150]
        log(f'S2 {y}: {rep[y]}')
    return rep


def load_s2(out_dir, year):
    p = os.path.join(out_dir, f'S2feat_{year}.npy')
    if not os.path.exists(p):
        return None
    return np.load(p).astype(np.float32)
