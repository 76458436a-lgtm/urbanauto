# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Источники автоматических меток для Random Forest (4.4): загрузка на сетку композитов.

Все наборы открытые и глобальные; для любого города нужен только охват (bbox, WGS 84):
  GHSL BUILT-S R2023A (JRC), 3″ ≈ 90 м, эпохи 2000–2030 — обязательный источник;
  WSF evolution 1985–2015 и WSF 2019 (DLR), 30 и 10 м;
  ESA WorldCover 2020, 2021, 10 м (класс 50 — застройка);
  JRC Global Surface Water occurrence, 30 м;
  здания OSM (Overpass API) — современное состояние;
  5.0: здания Microsoft Global ML Building Footprints (доля площади зданий в пикселе) — закрывают
  пробелы OSM в малых городах.
Каждый слой приводится к доле застройки 0–1 на целевой сетке (resample average).
Отсутствующий необязательный источник просто пропускается, это пишется в журнал.
"""
import math
import hashlib
import json
import time
import threading
try:
    from .urban_auto_version import USER_AGENT as UA
except ImportError:            # модуль загружен отдельно (тесты, скрипты)
    UA = 'QGIS-UrbanAuto (+https://github.com/76458436a-lgtm/urbanauto)'
import os
import tempfile

import numpy as np

GHSL = 'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_BUILT_S_GLOBE_R2023A/'
GHSL_EPOCHS = (2000, 2005, 2010, 2015, 2020, 2025, 2030)
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
GDAL_NET = dict(GDAL_HTTP_TIMEOUT='90', GDAL_HTTP_MAX_RETRY='4', GDAL_HTTP_RETRY_DELAY='3',
                GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', CPL_VSIL_CURL_USE_HEAD='NO', VSI_CACHE='TRUE')


def _gdal():
    from osgeo import gdal
    for k, v in GDAL_NET.items():
        gdal.SetConfigOption(k, v)
    return gdal


def _warp(src, grid, resample='average', src_nodata=None):
    gdal = _gdal()
    bounds, width, height, crs = grid
    ds = gdal.Warp('', src, format='MEM', outputBounds=bounds, width=width, height=height, dstSRS=crs,
                   resampleAlg=resample, outputType=gdal.GDT_Float32, srcNodata=src_nodata, dstNodata=np.nan)
    if ds is None:
        raise RuntimeError('не прочитан ' + src[-90:])
    a = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds = None
    return a


def _bbox_wgs(grid):
    """Охват сетки в WGS 84 (lon_min, lat_min, lon_max, lat_max)."""
    from osgeo import osr
    (x0, y0, x1, y1), _, _, crs = grid
    s = osr.SpatialReference(); s.SetFromUserInput(crs); s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    g = osr.SpatialReference(); g.ImportFromEPSG(4326); g.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s, g)
    pts = [ct.TransformPoint(x, y)[:2] for x in (x0, x1) for y in (y0, y1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _ghsl_tile(lon, lat):
    """R2023A 3ss tile grid, using the actual GeoTIFF origin, not 90N/180W.

    R4_C19: upper-left (-0.00791664424, 59.09958329838), 12000 pixels.
    The roughly 0.9 degree latitude offset matters for coastal Norway.
    """
    return (max(1, int((89.09958329838 - lat) // 10) + 1),
            min(36, max(1, int((lon + 180.00791664424) // 10) + 1)))


def ghsl_epoch(grid, epoch):
    w, s, e, n = _bbox_wgs(grid)
    r0, c0 = _ghsl_tile(w, n)
    r1, c1 = _ghsl_tile(e, s)
    tiles = {(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
    if len(tiles) > 1:
        srcs = []
        for r, c in sorted(tiles):
            base = f'GHS_BUILT_S_E{epoch}_GLOBE_R2023A_4326_3ss'; nm = f'{base}_V1_0_R{r}_C{c}'
            srcs.append(f'/vsizip//vsicurl/{GHSL}{base}/V1-0/tiles/{nm}.zip/{nm}.tif')
        gdal = _gdal()
        vrt = gdal.BuildVRT('', srcs)
        src = vrt
    else:
        (r, c), = tiles
        base = f'GHS_BUILT_S_E{epoch}_GLOBE_R2023A_4326_3ss'; nm = f'{base}_V1_0_R{r}_C{c}'
        src = f'/vsizip//vsicurl/{GHSL}{base}/V1-0/tiles/{nm}.zip/{nm}.tif'
    a = _warp(src, grid, 'bilinear')
    # BUILT-S 3″ — м² застройки в ячейке ≈ 92 × 92·cos(φ) м
    lat = (s + n) / 2
    cell = (3 / 3600 * 111320) * (3 / 3600 * 111320 * math.cos(math.radians(lat)))
    return np.clip(a / cell, 0, 1)


def wsf_evolution(grid):
    """WSF evolution: значение = год появления застройки (1985–2015), 0 — нет. → dict год → 0/1."""
    w, s, e, n = _bbox_wgs(grid)
    lon0, lat0 = int(math.floor(w / 2) * 2), int(math.floor(s / 2) * 2)
    src = f'/vsicurl/https://download.geoservice.dlr.de/WSF_EVO/files/WSFevolution_v1_{lon0}_{lat0}.tif'
    yr = _warp(src, grid, 'near')
    return {y: ((yr > 0) & (yr <= y)).astype(np.float32) for y in (1990, 1995, 2000, 2005, 2010, 2015)}


def wsf2019(grid):
    w, s, e, n = _bbox_wgs(grid)
    lon0, lat0 = int(math.floor(w / 2) * 2), int(math.floor(s / 2) * 2)
    a = _warp(f'/vsicurl/https://download.geoservice.dlr.de/WSF2019/files/WSF2019_v1_{lon0}_{lat0}.tif', grid, 'average')
    return np.clip(a / 255.0, 0, 1)


def esa_worldcover(grid, year):
    w, s, e, n = _bbox_wgs(grid)
    lat0, lon0 = int(math.floor(s / 3) * 3), int(math.floor(w / 3) * 3)
    tag = f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"
    ver = 'v100' if year == 2020 else 'v200'
    src = f'/vsicurl/https://esa-worldcover.s3.eu-central-1.amazonaws.com/{ver}/{year}/map/ESA_WorldCover_10m_{year}_{ver}_{tag}_Map.tif'
    gdal = _gdal()
    bounds, width, height, crs = grid
    # доля класса 50: сначала маска на исходной сетке 10 м, затем среднее
    ds = gdal.Warp('', src, format='MEM', outputBounds=_bbox_wgs(grid), outputBoundsSRS='EPSG:4326')
    a = (ds.GetRasterBand(1).ReadAsArray() == 50).astype(np.float32)
    mem = gdal.GetDriverByName('MEM').Create('', a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    mem.SetGeoTransform(ds.GetGeoTransform()); mem.SetProjection(ds.GetProjection())
    mem.GetRasterBand(1).WriteArray(a)
    ds = None
    return _warp(mem, grid, 'average')


def jrc_occurrence(grid):
    w, s, e, n = _bbox_wgs(grid)
    lon0, lat1 = int(math.floor(w / 10) * 10), int(math.ceil(n / 10) * 10)
    tag = f"{abs(lon0)}{'E' if lon0 >= 0 else 'W'}_{abs(lat1)}{'N' if lat1 >= 0 else 'S'}"
    return _warp(f'/vsicurl/https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/occurrence_{tag}v1_4_2021.tif',
                 grid, 'average')


def osm_buildings(grid, log=print, timeout=120):
    """Здания OSM (Overpass) → 0/1 на сетке (пиксель, который пересекает контур здания)."""
    import requests
    gdal = _gdal()
    from osgeo import ogr, osr
    w, s, e, n = _bbox_wgs(grid)
    q = f'[out:json][timeout:{timeout}];(way["building"]({s},{w},{n},{e});way["landuse"~"industrial|commercial|retail|residential_garages"]({s},{w},{n},{e}););out geom;'
    data = None
    for url in OVERPASS:
        try:
            r = requests.post(url, data={'data': q}, timeout=timeout + 30, headers={'User-Agent': UA})
            r.raise_for_status(); data = r.json(); break
        except Exception as ex:
            log(f'Overpass {url}: {str(ex)[:80]}')
    if data is None:
        raise RuntimeError('здания OSM не загружены')
    drv = ogr.GetDriverByName('Memory'); vds = drv.CreateDataSource('b')
    g4326 = osr.SpatialReference(); g4326.ImportFromEPSG(4326); g4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = vds.CreateLayer('b', g4326, ogr.wkbPolygon)
    for el in data.get('elements', []):
        pts = el.get('geometry') or []
        if len(pts) < 4:
            continue
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for p in pts:
            ring.AddPoint_2D(p['lon'], p['lat'])
        ring.CloseRings()
        poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(poly); lyr.CreateFeature(f)
    bounds, width, height, crs = grid
    mem = gdal.GetDriverByName('MEM').Create('', width, height, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((bounds[0], (bounds[2] - bounds[0]) / width, 0, bounds[3], 0, -(bounds[3] - bounds[1]) / height))
    sr = osr.SpatialReference(); sr.SetFromUserInput(crs); mem.SetProjection(sr.ExportToWkt())
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=['ALL_TOUCHED=TRUE'])
    a = mem.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    log(f'OSM: объектов {lyr.GetFeatureCount()}, пикселей застройки {int(a.sum())}')
    return a


def osm_footprints(grid, log=print, timeout=120):
    """5.1: контуры зданий OSM (только building=*) → доля площади зданий в пикселе (0–1)."""
    import requests
    from osgeo import ogr, osr
    w, s, e, n = _bbox_wgs(grid)
    q = f'[out:json][timeout:{timeout}];(way["building"]({s},{w},{n},{e}););out geom;'
    data = None
    for url in OVERPASS:
        try:
            r = requests.post(url, data={'data': q}, timeout=timeout + 30, headers={'User-Agent': UA})
            r.raise_for_status(); data = r.json(); break
        except Exception as ex:
            log(f'Overpass {url}: {str(ex)[:80]}')
    if data is None:
        raise RuntimeError('контуры зданий OSM не загружены')
    vds = ogr.GetDriverByName('Memory').CreateDataSource('f')
    g4326 = osr.SpatialReference(); g4326.ImportFromEPSG(4326); g4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = vds.CreateLayer('f', g4326, ogr.wkbPolygon)
    for el in data.get('elements', []):
        pts = el.get('geometry') or []
        if len(pts) < 4:
            continue
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for p in pts:
            ring.AddPoint_2D(p['lon'], p['lat'])
        ring.CloseRings()
        poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(poly); lyr.CreateFeature(f)
    a = _rasterize_fraction(lyr, grid)
    log(f'OSM: контуров зданий {lyr.GetFeatureCount()}, пикселей со зданиями {int((a > 0).sum())}')
    return a


ROAD_TYPES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
              'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link', 'service', 'track')


def osm_roads(grid, log=print, timeout=120):
    """Дороги и железные дороги OSM → 0/1 на сетке (пиксели, через которые проходит линия)."""
    import requests
    gdal = _gdal()
    from osgeo import ogr, osr
    w, s, e, n = _bbox_wgs(grid)
    hw = '|'.join(ROAD_TYPES)
    q = (f'[out:json][timeout:{timeout}];(way["highway"~"^({hw})$"]({s},{w},{n},{e});'
         f'way["railway"~"^(rail|light_rail|tram)$"]({s},{w},{n},{e}););out geom;')
    data = None
    for url in OVERPASS:
        try:
            r = requests.post(url, data={'data': q}, timeout=timeout + 30, headers={'User-Agent': UA})
            r.raise_for_status(); data = r.json(); break
        except Exception as ex:
            log(f'Overpass {url}: {str(ex)[:80]}')
    if data is None:
        raise RuntimeError('дороги OSM не загружены')
    drv = ogr.GetDriverByName('Memory'); vds = drv.CreateDataSource('r')
    g4326 = osr.SpatialReference(); g4326.ImportFromEPSG(4326); g4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = vds.CreateLayer('r', g4326, ogr.wkbLineString)
    for el in data.get('elements', []):
        pts = el.get('geometry') or []
        if len(pts) < 2:
            continue
        ln = ogr.Geometry(ogr.wkbLineString)
        for p in pts:
            ln.AddPoint_2D(p['lon'], p['lat'])
        f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(ln); lyr.CreateFeature(f)
    bounds, width, height, crs = grid
    mem = gdal.GetDriverByName('MEM').Create('', width, height, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((bounds[0], (bounds[2] - bounds[0]) / width, 0, bounds[3], 0, -(bounds[3] - bounds[1]) / height))
    sr = osr.SpatialReference(); sr.SetFromUserInput(crs); mem.SetProjection(sr.ExportToWkt())
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=['ALL_TOUCHED=TRUE'])
    a = mem.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    log(f'OSM: дорог {lyr.GetFeatureCount()}, пикселей {int(a.sum())}')
    return a


# ----------------------------------------------------------------------------- Microsoft (5.0)
MS_LINKS = 'https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv'


def quadkey(lon, lat, z=9):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    la = math.radians(max(-85.0511, min(85.0511, lat)))
    y = int((1 - math.log(math.tan(la) + 1 / math.cos(la)) / math.pi) / 2 * n)
    x, y = min(max(x, 0), n - 1), min(max(y, 0), n - 1)
    q = ''
    for i in range(z, 0, -1):
        d, m = 0, 1 << (i - 1)
        if x & m:
            d += 1
        if y & m:
            d += 2
        q += str(d)
    return q


LEGACY_CACHE = '.urban_auto_exp54_cache'      # кэш 5.4/5.5.0 используется, если он уже есть


def _user_cache():
    home = os.path.expanduser('~')
    d = os.path.join(home, '.urbanauto_cache')
    old = os.path.join(home, LEGACY_CACHE)
    if not os.path.isdir(d) and os.path.isdir(old):
        return old
    os.makedirs(d, exist_ok=True)
    return d


def _rasterize_fraction(lyr, grid, factor=6):
    """Полигоны → доля площади пикселя (0–1): прожиг на подсетке factor×factor и среднее."""
    gdal = _gdal()
    from osgeo import osr
    bounds, width, height, crs = grid
    mem = gdal.GetDriverByName('MEM').Create('', width * factor, height * factor, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((bounds[0], (bounds[2] - bounds[0]) / width / factor, 0, bounds[3], 0,
                         -(bounds[3] - bounds[1]) / height / factor))
    sr = osr.SpatialReference(); sr.SetFromUserInput(crs); mem.SetProjection(sr.ExportToWkt())
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    a = mem.GetRasterBand(1).ReadAsArray().astype(np.float32)
    mem = None
    return a.reshape(height, factor, width, factor).mean((1, 3))


def _store_download(path, content):
    tmp = tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False)
    try:
        with tmp:
            tmp.write(content)
        os.replace(tmp.name, path)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _ms_cache_path(folder, url):
    # A part filename is reused by hundreds of different quadkeys/countries.
    key = hashlib.sha256(url.split('?', 1)[0].encode('utf-8')).hexdigest()
    return os.path.join(folder, 'ms_v2_' + key + '.geojsonl.gz')


def ms_buildings(grid, log=print, timeout=180):
    """Контуры зданий Microsoft (квадраты Bing уровня 9) → доля площади зданий в пикселе."""
    import csv
    import gzip
    import io
    import json
    import requests
    from osgeo import ogr, osr
    w, s, e, n = _bbox_wgs(grid)
    links = os.path.join(_user_cache(), 'ms_dataset_links.csv')
    if not os.path.exists(links):
        r = requests.get(MS_LINKS, timeout=timeout, headers={'User-Agent': UA})
        r.raise_for_status()
        _store_download(links, r.content)
    qks = {quadkey(x, y) for x in np.linspace(w, e, 6) for y in np.linspace(s, n, 6)}
    urls = []
    with open(links, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('QuadKey') in qks:
                urls.append(row['Url'])
    if not urls:
        raise RuntimeError('нет квадратов Microsoft для этой территории')
    urls = list(dict.fromkeys(urls))
    log(f'Microsoft: найдено файлов тайлов {len(urls)}')
    g4326 = osr.SpatialReference(); g4326.ImportFromEPSG(4326); g4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    vds = ogr.GetDriverByName('Memory').CreateDataSource('ms')
    lyr = vds.CreateLayer('ms', g4326, ogr.wkbPolygon)
    defn = lyr.GetLayerDefn()
    nb = 0
    for u in urls:
        cache = _ms_cache_path(_user_cache(), u)
        if not os.path.exists(cache):
            log('Microsoft: загрузка ' + u.split('?', 1)[0])
            r = requests.get(u, timeout=timeout, headers={'User-Agent': UA})
            r.raise_for_status()
            _store_download(cache, r.content)
        with gzip.open(cache, 'rt', encoding='utf-8') as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                geom = obj.get('geometry', obj)
                ring = (geom.get('coordinates') or [[]])[0]
                if not ring:
                    continue
                x0, y0 = ring[0][0], ring[0][1]
                if not (w <= x0 <= e and s <= y0 <= n):
                    continue
                r_ = ogr.Geometry(ogr.wkbLinearRing)
                for x, y in ring:
                    r_.AddPoint_2D(x, y)
                pg = ogr.Geometry(ogr.wkbPolygon); pg.AddGeometry(r_)
                ft = ogr.Feature(defn); ft.SetGeometry(pg); lyr.CreateFeature(ft); nb += 1
    a = _rasterize_fraction(lyr, grid)
    log(f'Microsoft: зданий {nb}, пикселей со зданиями {int((a > 0).sum())}')
    if nb == 0:
        raise RuntimeError('в охвате нет зданий Microsoft')
    return a


# ----------------------------------------------------------------------------- город по названию (5.0)
_NOMINATIM_LOCK = threading.Lock()
_NOMINATIM_LAST = [0.0]


def _haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(a)))


def geocode(name, country=None, timeout=30, log=None):
    """Название населённого пункта → (lon, lat, отображаемое имя) через Nominatim (OSM).

    5.5.1: не чаще одного запроса в секунду (правила Nominatim), User-Agent с адресом проекта,
    кэш ответов на диске; если найдено несколько одноимённых мест дальше 50 км друг от друга,
    в журнал пишется предупреждение со списком — тогда укажите регион («Белогорск, Амурская область»)."""
    import requests
    key = hashlib.sha256(f'{name.strip().lower()}|{country or ""}'.encode('utf-8')).hexdigest()[:24]
    cache = os.path.join(_user_cache(), f'geocode_{key}.json')
    data = None
    if os.path.exists(cache):
        try:
            with open(cache, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = None
    if data is None:
        params = dict(q=name, format='json', limit=5, addressdetails=0)
        if country:
            params['countrycodes'] = country
        with _NOMINATIM_LOCK:
            wait = 1.05 - (time.time() - _NOMINATIM_LAST[0])
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.get('https://nominatim.openstreetmap.org/search', params=params,
                                 headers={'User-Agent': UA, 'Accept-Language': 'ru,en'}, timeout=timeout)
            finally:
                _NOMINATIM_LAST[0] = time.time()
        r.raise_for_status()
        data = r.json()
        try:
            with open(cache, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass
    res = [x for x in data if x.get('class') in ('place', 'boundary')] or data
    if not res:
        raise RuntimeError(f'не найден населённый пункт «{name}»')
    x = res[0]
    lon, lat = float(x['lon']), float(x['lat'])
    far = [y for y in res[1:] if _haversine_km(lon, lat, float(y['lon']), float(y['lat'])) > 50]
    if far and log:
        log('Внимание: найдено несколько мест с таким названием. Выбрано: ' + x.get('display_name', name)
            + '. Другие: ' + '; '.join(y.get('display_name', '?') for y in far[:3])
            + '. Если выбрано не то место, укажите регион или задайте территорию полигоном.')
    return lon, lat, x.get('display_name', name)


def fetch_sources(grid, log=print, cancel=None, cache_dir=None):
    """Собирает все источники. Возвращает dict для urban_auto_rf.auto_labels и отчёт о загрузке."""
    src, report = {}, {}

    def step(name, fn):
        cache_name = name + '_v2' if name.startswith('ghsl_') or name == 'ms_buildings' else name
        if cancel and cancel():
            return None
        if cache_dir:
            p = os.path.join(cache_dir, f'labels_{cache_name}.npz')
            if os.path.exists(p):
                z = np.load(p, allow_pickle=True)
                v = z['v'].item() if z['v'].dtype == object else z['v']
                if not (name.startswith('ghsl') and not np.nanmax(np.nan_to_num(v)) > 0):
                    report[name] = 'кэш'
                    return v
        try:
            v = fn()
            if name.startswith('ghsl') and not np.nanmax(np.nan_to_num(v)) > 0:
                raise RuntimeError('пустой слой (нет застройки или неверный тайл)')
            report[name] = 'ok'
            if cache_dir:
                np.savez_compressed(os.path.join(cache_dir, f'labels_{cache_name}.npz'), v=np.array(v, dtype=object) if isinstance(v, dict) else v)
            return v
        except Exception as ex:
            report[name] = 'нет: ' + str(ex)[:120]
            log(f'Источник {name} недоступен: {str(ex)[:120]}')
            return None
    ghsl = {}
    for ep in GHSL_EPOCHS:
        a = step(f'ghsl_{ep}', lambda ep=ep: ghsl_epoch(grid, ep))
        if a is not None:
            ghsl[ep] = a
    if len(ghsl) < 3:
        raise RuntimeError('GHSL BUILT-S загружен меньше чем для трёх эпох — автоматические метки невозможны.')
    src['ghsl'] = ghsl
    wsf = step('wsf_evolution', lambda: wsf_evolution(grid))
    if wsf:
        src['wsf'] = wsf
    w19 = step('wsf2019', lambda: wsf2019(grid))
    if w19 is not None:
        src['wsf2019'] = w19
    esa = {}
    for y in (2020, 2021):
        a = step(f'esa_{y}', lambda y=y: esa_worldcover(grid, y))
        if a is not None:
            esa[y] = a
    if esa:
        src['esa'] = esa
    occ = step('jrc_occurrence', lambda: jrc_occurrence(grid))
    if occ is not None:
        src['water_occ'] = occ
    osm = step('osm_buildings', lambda: osm_buildings(grid, log))
    if osm is not None:
        src['osm'] = osm
    roads = step('osm_roads', lambda: osm_roads(grid, log))
    if roads is not None:
        src['roads'] = roads
    ms = step('ms_buildings', lambda: ms_buildings(grid, log))
    if ms is not None:
        src['ms'] = ms
    fp = step('osm_bfrac', lambda: osm_footprints(grid, log))
    if fp is not None:
        src['osm_bfrac'] = fp
    return src, report
