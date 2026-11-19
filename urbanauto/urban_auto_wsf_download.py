# SPDX-License-Identifier: GPL-2.0-or-later
"""WSF Tracker STAC discovery and GeoTIFF download, with explicit source validation."""
import json
import os
import shutil
import tempfile
from .wsf_validation import parse_built_codes, validate_coverage, stac_next
try:
    from .urban_auto_version import USER_AGENT
except ImportError:            # модуль загружен отдельно (тесты, скрипты)
    USER_AGENT = 'QGIS-UrbanAuto (+https://github.com/76458436a-lgtm/urbanauto)'
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber, QgsProcessingParameterString, QgsProcessingParameterFolderDestination,
    QgsProcessingException, QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsRectangle)

STAC = 'https://geoservice.dlr.de/eoc/ogc/stac/v1'
COLLECTION = 'WSF_TRACKER'

def request_json(url, payload=None, timeout=45):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'Accept': 'application/geo+json, application/json', 'User-Agent': USER_AGENT}
    if data is not None: headers['Content-Type'] = 'application/json'
    with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
        return json.load(response)

def select_assets(item):
    choices = []
    for key, asset in item.get('assets', {}).items():
        href = asset.get('href', '')
        typ = asset.get('type', '').lower()
        roles = asset.get('roles', [])
        if not href or not urlparse(href).scheme in ('https', 'http'): continue
        if any(x in key.lower() for x in ('thumbnail', 'preview', 'metadata', 'overview')): continue
        if 'image/tiff' in typ or href.lower().split('?')[0].endswith(('.tif', '.tiff')):
            priority = 0 if 'data' in roles else 1
            choices.append((priority, key, href))
    return sorted(choices)

def bbox_wgs84(layer):
    tr = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
    rect = tr.transformBoundingBox(layer.extent())
    return [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]

def intersect(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

def discover(bbox, year, feedback):
    url = STAC + '/search'
    payload = {'collections': [COLLECTION], 'bbox': bbox, 'datetime': '%d-01-01T00:00:00Z/%d-12-31T23:59:59Z' % (year, year), 'limit': 100}
    items = []; seen = set()
    for page in range(10):
        obj = request_json(url, payload)
        for item in obj.get('features', []):
            if item.get('id') not in seen and intersect(item.get('bbox', [-180,-90,180,90]), bbox):
                seen.add(item.get('id')); items.append(item)
        nxt = next((link for link in obj.get('links', []) if link.get('rel') == 'next'), None)
        if not nxt: break
        url, payload = stac_next(nxt, url, payload)
        if feedback.isCanceled(): raise QgsProcessingException('Отменено пользователем')
    return items

def download_file(url, dest, feedback):
    tmp = dest + '.part'
    try:
        with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=90) as resp, open(tmp, 'wb') as out:
            total = int(resp.headers.get('Content-Length', 0))
            if total > 2 * 1024**3: raise QgsProcessingException('Файл больше 2 ГБ; используйте локальную загрузку')
            count = 0
            while True:
                if feedback.isCanceled(): raise QgsProcessingException('Загрузка отменена')
                chunk = resp.read(1024*1024)
                if not chunk: break
                count += len(chunk)
                if count > 2 * 1024**3: raise QgsProcessingException('Превышен лимит 2 ГБ')
                out.write(chunk)
        if count < 512: raise QgsProcessingException('Получен слишком маленький файл вместо растра')
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp): os.remove(tmp)

class WSFDownloadAlgorithm(QgsProcessingAlgorithm):
    REFERENCE='REFERENCE'; YEAR='YEAR'; OUTPUT='OUTPUT'; BUILT_CODES='BUILT_CODES'
    def name(self): return 'wsf_download'
    def displayName(self): return 'Urban Auto — скачать WSF Tracker по области карты'
    def group(self): return 'Городской анализ'
    def groupId(self): return 'urban_analysis'
    def createInstance(self): return WSFDownloadAlgorithm()
    def shortHelpString(self): return ('Автоматически ищет WSF Tracker через официальный STAC DLR, скачивает исходные GeoTIFF и обрезает по границам выбранного растра. '
      'Данные не бинаризуются: проверьте легенду/коды значений WSF перед сравнением 0/1. '
      'Если STAC не публикует прямые GeoTIFF, выдаётся явная ошибка. Не используйте WMS-картинку как научную маску. '
      'Для скачивания требуется интернет; для обработки GDAL из состава QGIS. Источник: DLR WSF Tracker, CC BY 4.0.')
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.REFERENCE, 'Растр Urban Auto (границы и сетка результата)'))
        self.addParameter(QgsProcessingParameterNumber(self.YEAR, 'Год WSF Tracker', type=QgsProcessingParameterNumber.Integer, defaultValue=2025, minValue=2016, maxValue=2026))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, 'Папка для WSF'))
        self.addParameter(QgsProcessingParameterString(self.BUILT_CODES, 'Коды класса застройки WSF (пусто = не создавать бинарную маску)', optional=True, defaultValue=''))
    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, self.REFERENCE, context)
        year = self.parameterAsInt(parameters, self.YEAR, context)
        output = self.parameterAsString(parameters, self.OUTPUT, context)
        code_text = self.parameterAsString(parameters, self.BUILT_CODES, context).strip()
        try: built_codes = parse_built_codes(code_text) if code_text else None
        except ValueError as exc: raise QgsProcessingException(str(exc))
        if not layer or not layer.isValid(): raise QgsProcessingException('Нужен действительный опорный растр')
        os.makedirs(output, exist_ok=True)
        bbox = bbox_wgs84(layer)
        if bbox[2]-bbox[0] > 3 or bbox[3]-bbox[1] > 3:
            raise QgsProcessingException('Область слишком велика: выберите городской растр (до 3° по каждой оси)')
        feedback.pushInfo('Поиск WSF Tracker в STAC DLR за %d год…' % year)
        try: items = discover(bbox, year, feedback)
        except Exception as exc: raise QgsProcessingException('Не удалось получить каталог STAC DLR: %s' % exc)
        candidates = []
        for item in items:
            assets = select_assets(item)
            if assets: candidates.append((item, assets[0][2]))
        if not candidates:
            raise QgsProcessingException('STAC не вернул GeoTIFF для выбранного года/области. WMS нельзя использовать как исходные классы; скачайте оригинальный растр WSF вручную.')
        if len(candidates) > 24: raise QgsProcessingException('Найдено слишком много тайлов (%d); уменьшите область' % len(candidates))
        from osgeo import gdal
        gdal.UseExceptions()
        paths = []
        for index, (item, href) in enumerate(candidates):
            path = os.path.join(output, 'wsf_source_%02d.tif' % index)
            feedback.pushInfo('Скачивание %d/%d: %s' % (index+1,len(candidates),item.get('id','')))
            try: download_file(href, path, feedback)
            except HTTPError as exc:
                if exc.code == 403:
                    raise QgsProcessingException('DLR отклонил загрузку WSF (HTTP 403). Для сравнения укажите доступный локальный растр WSF в инструменте wsf_benchmark.') from exc
                raise QgsProcessingException(f'Ошибка скачивания {href}: {exc}') from exc
            except Exception as exc: raise QgsProcessingException('Ошибка скачивания %s: %s' % (href, exc))
            ds = gdal.Open(path)
            if ds is None: raise QgsProcessingException('Источник не является читаемым растром GeoTIFF: ' + href)
            ds = None
            paths.append(path)
        dest = os.path.join(output, 'WSF_Tracker_%d_aligned.tif' % year)
        with tempfile.TemporaryDirectory(dir=output) as stage:
            staged = os.path.join(stage, 'aligned.tif')
            extent = layer.extent()
            opts = gdal.WarpOptions(format='GTiff', outputBounds=[extent.xMinimum(),extent.yMinimum(),extent.xMaximum(),extent.yMaximum()],
                 width=layer.width(), height=layer.height(), dstSRS=layer.crs().authid(), resampleAlg='near', dstAlpha=True,
                 creationOptions=['COMPRESS=LZW','TILED=YES'], multithread=True)
            try:
                ds = gdal.Warp(staged, paths, options=opts)
                if ds is None: raise RuntimeError('GDAL.Warp вернул None')
                ds.FlushCache(); ds = None
            except Exception as exc: raise QgsProcessingException('Не удалось привести WSF к сетке Urban Auto: %s' % exc)
            # Verify full coverage from the GDAL validity mask. Never treat missing pixels as background.
            import numpy as np
            aligned = gdal.Open(staged)
            if aligned is None: raise QgsProcessingException('Не удалось открыть выровненный WSF')
            band = aligned.GetRasterBand(1)
            values = band.ReadAsArray()
            valid = aligned.GetRasterBand(aligned.RasterCount).ReadAsArray() != 0
            try: validate_coverage(int(np.count_nonzero(valid)), int(valid.size))
            except ValueError as exc:
                aligned = None
                raise QgsProcessingException(str(exc))
            unique_values = [int(v) for v in np.unique(values[valid])]
            binary_path = None
            if built_codes is not None:
                if not set(built_codes).issubset(set(unique_values)):
                    aligned = None
                    raise QgsProcessingException('Коды застройки %s отсутствуют в растре. Найдены: %s' % (built_codes, unique_values[:50]))
                binary_path = os.path.join(output, 'WSF_Tracker_%d_binary.tif' % year)
                driver = gdal.GetDriverByName('GTiff')
                binary_stage = os.path.join(stage, 'binary.tif')
                binary = driver.Create(binary_stage, aligned.RasterXSize, aligned.RasterYSize, 1, gdal.GDT_Byte, options=['COMPRESS=LZW','TILED=YES'])
                binary.SetGeoTransform(aligned.GetGeoTransform())
                binary.SetProjection(aligned.GetProjection())
                binary.GetRasterBand(1).WriteArray(np.isin(values, built_codes).astype('uint8'))
                binary.GetRasterBand(1).SetNoDataValue(255)
                binary.FlushCache(); binary = None
            aligned = None
            os.replace(staged, dest)
            if binary_path:
                os.replace(binary_stage, binary_path)
        manifest = {'source':'DLR WSF Tracker', 'collection': COLLECTION, 'year_requested':year,
          'bbox_wgs84':bbox, 'items':[{'id':item.get('id'),'datetime':item.get('properties',{}).get('datetime'), 'asset':href} for item,href in candidates],
          'aligned_raster':dest, 'binary_raster':binary_path, 'observed_values':unique_values, 'built_codes':built_codes, 'coverage_fraction':1.0, 'warning':'Значения WSF НЕ перекодированы в бинарную маску. Проверьте классы, NoData и определение застройки.',
          'license':'CC BY 4.0 — attribution required'}
        with open(os.path.join(output,'wsf_source_manifest.json'),'w',encoding='utf-8') as file: json.dump(manifest,file,ensure_ascii=False,indent=2)
        feedback.pushInfo('WSF сохранён: ' + dest)
        feedback.setProgress(100)
        return {'WSF_RASTER':dest, 'WSF_BINARY':binary_path or '', 'MANIFEST':os.path.join(output,'wsf_source_manifest.json')}
