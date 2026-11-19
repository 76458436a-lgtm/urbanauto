# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
import os
import json
import math
import datetime
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, asdict, field
from typing import Tuple, Optional, Dict

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsCoordinateReferenceSystem, QgsProcessingContext,
    QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsFeature, QgsDistanceArea, QgsProcessingFeedback
)
import processing

try:
    import requests
except ImportError:  # requests ships with QGIS, but keep the plugin importable without it
    requests = None

from .urban_auto_version import VERSION as PLUGIN_VERSION, USER_AGENT

# Публичные серверы Overpass API (overpass.osm.rambler.ru больше не работает)
OSM_TIMEOUT = 25  # с на сервер; при недоступности — следующий сервер, затем круг
OSM_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


@dataclass
class UrbanConfig:
    year: int
    center: Tuple[float, float]
    radius_km: float
    roi_type: str
    osm_level: int
    polygon_layer_id: Optional[str]
    formula: str
    output_folder: str
    ndbi_path: str
    bsi_path: str
    ndvi_path: str
    mndwi_path: str
    is_geo: bool
    crs_authid: str
    previous_path: Optional[str] = None
    smart_merge_radius: float = 300.0   # радиус привязки к застройке прошлого года, м
    # Параметры морфологической обработки (м / м² / пиксели)
    close_dist: float = 200.0           # буфер наружу (замыкание разрывов)
    shrink_dist: float = 200.0          # буфер внутрь; = close_dist → симметричное замыкание
    min_area_m2: float = 50000.0        # минимальная площадь пятна застройки
    sieve_px: int = 2                   # удаление шумовых пикселей до морфологии
    clip_to_roi: bool = True            # обрезать результат по ROI (а не только выбирать пересекающие)
    thresholds: Dict[str, float] = field(default_factory=dict)  # для протокола расчёта
    mode: str = ""                      # режим расчёта порогов (для протокола)
    threshold_options: Dict = field(default_factory=dict)
    auto_align: bool = True
    max_pixels: int = 16000000
    reference_path: Optional[str] = None
    polygon_path: Optional[str] = None
    allow_osm_fallback: bool = True  # серверы OSM часто недоступны; откат фиксируется в протоколе


def utm_crs_for(lon, lat):
    if not (-180 <= lon <= 180 and -80 <= lat <= 84):
        raise ValueError("Для UTM нужны долгота [-180, 180] и широта [-80, 84].")
    zone = min(60, int((lon + 180) / 6) + 1)
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def _osm_roi(config, center_pt, log_callback, context, feedback):
    """Загружает административные границы OSM и возвращает полигон, содержащий центр."""
    if requests is None:
        log_callback("Модуль requests недоступен — OSM пропущен.")
        return None
    deg_lat = config.radius_km / 111.0
    deg_lon = config.radius_km / (111.0 * max(math.cos(math.radians(center_pt.y())), 0.05))
    s, w, n, e = center_pt.y() - deg_lat, center_pt.x() - deg_lon, center_pt.y() + deg_lat, center_pt.x() + deg_lon
    query = (f'[out:json][timeout:40];relation["boundary"="administrative"]'
             f'["admin_level"="{config.osm_level}"]({s},{w},{n},{e});way(r);out geom;')
    headers = {"User-Agent": USER_AGENT}
    for url in OSM_SERVERS:
        try:
            log_callback(f"Загрузка OSM с {url}...")
            resp = requests.post(url, data={"data": query}, headers=headers, timeout=OSM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            lines = QgsVectorLayer("LineString?crs=EPSG:4326", "osm_lines", "memory")
            prov = lines.dataProvider()
            feats = []
            for el in data.get("elements", []):
                if el.get("type") == "way" and "geometry" in el:
                    pts = [QgsPointXY(p["lon"], p["lat"]) for p in el["geometry"]]
                    if len(pts) > 1:
                        f = QgsFeature()
                        f.setGeometry(QgsGeometry.fromPolylineXY(pts))
                        feats.append(f)
            if not feats:
                log_callback("OSM: границы не найдены.")
                continue
            prov.addFeatures(feats)
            polys = processing.run("native:polygonize", {'INPUT': lines, 'OUTPUT': 'memory:'},
                                   context=context, feedback=feedback)['OUTPUT']
            # Берём только полигон(ы), содержащие центр города, а не все границы в охвате
            center_geom = QgsGeometry.fromPointXY(center_pt)
            chosen = [f for f in polys.getFeatures() if f.geometry().contains(center_geom)]
            if not chosen:
                log_callback("OSM: центр не попал ни в одну границу.")
                continue
            chosen.sort(key=lambda f: f.geometry().area())  # самый «тесный» контур = город
            roi = QgsVectorLayer("Polygon?crs=EPSG:4326", "ROI_OSM", "memory")
            nf = QgsFeature()
            nf.setGeometry(chosen[0].geometry())
            roi.dataProvider().addFeatures([nf])
            roi.setCustomProperty("roi_source", f"osm:{url}")
            return roi
        except Exception as ex:  # сеть / JSON / геометрия
            log_callback(f"Ошибка OSM ({url}): {ex}")
    return None


def load_vector_to_memory(path, name='ROI'):
    """Читает полигоны файла через OGR в слой памяти и сразу закрывает файл.

    Слой-провайдер OGR в QGIS держит файл открытым (пул соединений), и на Windows
    папку запуска нельзя удалить или переместить до перезапуска QGIS (найдено в 4.3.1
    интеграционными тестами на QGIS 3.44 / Windows).
    """
    from osgeo import ogr
    src = path.split('|')[0]
    ds = ogr.Open(src)
    if ds is None or ds.GetLayerCount() == 0:
        raise ValueError(f"Не удалось открыть полигон ROI: {src}")
    lyr = ds.GetLayer(0)
    srs = lyr.GetSpatialRef()
    crs = QgsCoordinateReferenceSystem()
    if srs is not None:
        crs.createFromWkt(srs.ExportToWkt())
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem('EPSG:4326')
    feats = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        qg = QgsGeometry()
        qg.fromWkb(bytes(g.ExportToIsoWkb()))
        if qg.isEmpty():
            continue
        qg.convertToMultiType()
        qf = QgsFeature()
        qf.setGeometry(qg)
        feats.append(qf)
    lyr = None
    ds = None
    mem = QgsVectorLayer("MultiPolygon", name, "memory")
    mem.setCrs(crs)
    mem.dataProvider().addFeatures(feats)
    mem.updateExtents()
    return mem


def create_roi(config: UrbanConfig, transform_context, crs_4326, log_callback, context, feedback):
    center_pt = QgsPointXY(config.center[0], config.center[1])

    if config.roi_type == 'polygon':
        log_callback("Используется пользовательский полигон...")
        if config.polygon_path:
            layer = load_vector_to_memory(config.polygon_path, "ROI")
            if layer.isValid() and layer.featureCount() > 0:
                layer.setCustomProperty("roi_source", "polygon_file")
                return layer
            raise ValueError("Не удалось открыть полигон ROI: " + config.polygon_path)
        if config.polygon_layer_id:
            project = context.project() if context is not None else None
            layer = project.mapLayer(config.polygon_layer_id) if project else None
            if layer and layer.isValid():
                layer.setCustomProperty("roi_source", "polygon")
                return layer
        raise Exception("Пользовательский полигон не найден или не задан.")

    if config.roi_type == 'osm':
        roi = _osm_roi(config, center_pt, log_callback, context, feedback)
        if roi is not None:
            return roi
        if not config.allow_osm_fallback:
            raise ValueError("Граница OSM недоступна. Задайте полигон/круг или явно разрешите резервный круг.")
        log_callback("ВНИМАНИЕ: граница OSM недоступна — использован круговой буфер (roi_source=circle_fallback). "
                     "Для отчётных расчётов задайте полигон ROI вручную.")

    log_callback("Построение кругового буфера...")
    crs_utm = utm_crs_for(center_pt.x(), center_pt.y())
    to_utm = QgsCoordinateTransform(crs_4326, crs_utm, transform_context)
    to_wgs = QgsCoordinateTransform(crs_utm, crs_4326, transform_context)
    circle = QgsGeometry.fromPointXY(to_utm.transform(center_pt)).buffer(config.radius_km * 1000.0, 72)
    circle.transform(to_wgs)
    mem = QgsVectorLayer("Polygon?crs=EPSG:4326", "ROI_Circle", "memory")
    f = QgsFeature()
    f.setGeometry(circle)
    mem.dataProvider().addFeatures([f])
    mem.setCustomProperty("roi_source", "circle_fallback" if config.roi_type == 'osm' else "circle")
    return mem


def measure_layer(layer, transform_context):
    """Площадь (км²), периметр (км), число пятен и компактность по эллипсоиду."""
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), transform_context)
    da.setEllipsoid('WGS84')
    area = perim = 0.0
    parts = 0
    for feat in layer.getFeatures():
        g = feat.geometry()
        if not g or g.isEmpty():
            continue
        area += da.measureArea(g)
        perim += da.measurePerimeter(g)
        parts += len(g.asGeometryCollection()) if g.isMultipart() else 1
    area_km2 = area / 1e6
    perim_km = perim / 1e3
    compact = (4 * math.pi * area_km2 / perim_km ** 2) if perim_km > 0 else 0.0
    return dict(area_km2=area_km2, perimeter_km=perim_km, patches=parts, compactness=compact)


def run_urban_analysis_core(config: UrbanConfig, log_callback, context=None, feedback=None, metrics_sink=None) -> Tuple[str, float]:
    from .urban_auto_inputs import prepare_rasters, check_cancel
    from .urban_auto_thresholds import build_formula
    from .urban_auto_temporal import vector_source
    if not config.clip_to_roi:
        raise ValueError('В 4.3 обрезка по ROI обязательна: включите clip_to_roi для сопоставимых площадей.')
    if not math.isfinite(config.radius_km) or config.radius_km <= 0:
        raise ValueError("Радиус должен быть конечным положительным числом.")
    for key in ('close_dist', 'shrink_dist', 'min_area_m2', 'smart_merge_radius'):
        value = getattr(config, key)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Некорректный параметр {key}: {value}")
    parent_context = context
    resources = ExitStack()
    context = QgsProcessingContext()
    if parent_context is None:
        context.setProject(QgsProject.instance())
    else:
        context.setProject(parent_context.project())
        context.setTransformContext(parent_context.transformContext())
    if feedback is None:
        feedback = QgsProcessingFeedback()

    transform_context = context.transformContext()
    crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
    raster_crs = QgsCoordinateReferenceSystem(config.crs_authid)
    crs_utm = utm_crs_for(config.center[0], config.center[1])

    def run(alg, params):
        check_cancel(feedback)
        return processing.run(alg, params, context=context, feedback=feedback)['OUTPUT']

    try:
        check_cancel(feedback)
        original_paths = {key: getattr(config, key + '_path') for key in ('ndbi', 'bsi', 'ndvi', 'mndwi')}
        raw_roi = create_roi(config, transform_context, crs_4326, log_callback, context, feedback)
        roi_source = raw_roi.customProperty("roi_source", config.roi_type)
        roi_path, roi_name, roi_tmp = vector_source(raw_roi)
        roi_dir = resources.enter_context(tempfile.TemporaryDirectory(prefix='urban_roi_'))
        from osgeo import gdal
        roi_file = os.path.join(roi_dir, 'roi.gpkg')
        translated = gdal.VectorTranslate(roi_file, roi_path, format='GPKG',
                                          layers=[roi_name] if roi_name else None)
        if translated is None:
            raise ValueError("Не удалось сохранить область интереса.")
        translated = None
        if roi_tmp:
            os.remove(roi_tmp)
        layers, input_info = resources.enter_context(prepare_rasters(
            original_paths, roi_file, reference=config.reference_path,
            auto_align=config.auto_align, max_pixels=config.max_pixels,
            feedback=feedback, log=log_callback))
        raster_crs = layers['ndbi'].crs()
        prepared_paths = {key: layer.source() for key, layer in layers.items()}
        import numpy as np
        os.makedirs(config.output_folder, exist_ok=True)
        coverage_path = os.path.join(config.output_folder, f'Valid_{config.year}.tif')
        src = gdal.Open(prepared_paths['ndbi'])
        cov = gdal.GetDriverByName('GTiff').Create(coverage_path, src.RasterXSize,
            src.RasterYSize, 1, gdal.GDT_Byte, options=['COMPRESS=DEFLATE', 'TILED=YES'])
        cov.SetGeoTransform(src.GetGeoTransform()); cov.SetProjection(src.GetProjection())
        data = src.GetRasterBand(1).ReadAsArray()
        valid = np.isfinite(data) & (data >= -1) & (data <= 1)
        cov.GetRasterBand(1).WriteArray(valid.astype(np.uint8))
        cov.GetRasterBand(1).SetNoDataValue(0)
        cov = None; src = None
        del data, valid
        coverage_poly = run('gdal:polygonize', {'INPUT': coverage_path, 'BAND': 1, 'FIELD': 'DN',
                                             'OUTPUT': 'TEMPORARY_OUTPUT'})
        coverage_vec = run('native:extractbyattribute', {'INPUT': coverage_poly, 'FIELD': 'DN',
                             'OPERATOR': 0, 'VALUE': '1', 'OUTPUT': 'TEMPORARY_OUTPUT'})
        formula = config.formula
        if not formula:
            options = dict(config.threshold_options)
            options.setdefault('use_otsu', True)
            options.setdefault('transform_context', transform_context)
            formula, mode, thresholds = build_formula(
                layers['ndbi'], layers['bsi'], layers['ndvi'], log_callback=log_callback, **options)
            config.mode, config.thresholds = mode, thresholds
        if not formula:
            raise ValueError("Пустая формула классификации.")
        log_callback(f"[{config.year}] Шаг 1/5: Расчёт маски застройки по формуле...")
        out_raw = run("gdal:rastercalculator", {
            'INPUT_A': prepared_paths['ndbi'], 'BAND_A': 1,
            'INPUT_B': prepared_paths['bsi'], 'BAND_B': 1,
            'INPUT_C': prepared_paths['mndwi'], 'BAND_C': 1,
            'INPUT_D': prepared_paths['ndvi'], 'BAND_D': 1,
            'FORMULA': formula, 'RTYPE': 0, 'NO_DATA': 255,
            'OUTPUT': 'TEMPORARY_OUTPUT'})

        log_callback(f"[{config.year}] Шаг 2/5: Область интереса (ROI)...")
        roi_layer = run("native:reprojectlayer", {'INPUT': raw_roi, 'TARGET_CRS': raster_crs, 'OUTPUT': 'TEMPORARY_OUTPUT'})

        log_callback(f"[{config.year}] Шаг 3/5: Маска водных объектов (MNDWI > 0)...")
        water_mask = run("gdal:rastercalculator", {'INPUT_A': prepared_paths['mndwi'], 'BAND_A': 1,
                                                    'FORMULA': '(A > 0)*1', 'RTYPE': 0, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        wet_poly = run("gdal:polygonize", {'INPUT': water_mask, 'BAND': 1, 'FIELD': 'DN', 'OUTPUT': 'TEMPORARY_OUTPUT'})
        wet_ext = run("native:extractbyattribute", {'INPUT': wet_poly, 'FIELD': 'DN', 'OPERATOR': 0, 'VALUE': '1',
                                                     'OUTPUT': 'TEMPORARY_OUTPUT'})
        wet_eraser = run("native:fixgeometries", {'INPUT': wet_ext, 'OUTPUT': 'TEMPORARY_OUTPUT'})

        log_callback(f"[{config.year}] Шаг 4/5: Морфологическая очистка...")
        sieved = run("gdal:sieve", {'INPUT': out_raw, 'THRESHOLD': int(config.sieve_px), 'EIGHT_CONNECTEDNESS': False,
                                    'OUTPUT': 'TEMPORARY_OUTPUT'})
        poly = run("gdal:polygonize", {'INPUT': sieved, 'BAND': 1, 'FIELD': 'DN', 'OUTPUT': 'TEMPORARY_OUTPUT'})
        ext = run("native:extractbyattribute", {'INPUT': poly, 'FIELD': 'DN', 'OPERATOR': 0, 'VALUE': '1',
                                                'OUTPUT': 'TEMPORARY_OUTPUT'})
        fixed = run("native:fixgeometries", {'INPUT': ext, 'OUTPUT': 'TEMPORARY_OUTPUT'})

        fixed_utm = run("native:reprojectlayer", {'INPUT': fixed, 'TARGET_CRS': crs_utm, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        buf_plus = run("native:buffer", {'INPUT': fixed_utm, 'DISTANCE': float(config.close_dist), 'SEGMENTS': 8,
                                         'DISSOLVE': True, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        buf_minus = run("native:buffer", {'INPUT': buf_plus, 'DISTANCE': -float(config.shrink_dist), 'SEGMENTS': 8,
                                          'OUTPUT': 'TEMPORARY_OUTPUT'})
        single = run("native:multiparttosingleparts", {'INPUT': buf_minus, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        filtered_utm = run("native:extractbyexpression", {'INPUT': single,
                                                           'EXPRESSION': f'$area > {float(config.min_area_m2)}',
                                                           'OUTPUT': 'TEMPORARY_OUTPUT'})
        urban_clean = run("native:reprojectlayer", {'INPUT': filtered_utm, 'TARGET_CRS': raster_crs,
                                                    'OUTPUT': 'TEMPORARY_OUTPUT'})

        log_callback(f"[{config.year}] Шаг 5/5: Вычитание воды и обрезка по ROI...")
        urban_no_water = run("native:difference", {'INPUT': urban_clean, 'OVERLAY': wet_eraser,
                                                   'OUTPUT': 'TEMPORARY_OUTPUT'})
        if config.clip_to_roi:
            urban_roi = run("native:clip", {'INPUT': urban_no_water, 'OVERLAY': roi_layer, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        else:
            urban_roi = run("native:extractbylocation", {'INPUT': urban_no_water, 'PREDICATE': [0],
                                                         'INTERSECT': roi_layer, 'OUTPUT': 'TEMPORARY_OUTPUT'})

        # --- Накопительный рост: новая застройка учитывается, только если она примыкает к прошлогодней ---
        if config.previous_path and os.path.exists(config.previous_path.split('|')[0]):
            log_callback(f"[{config.year}] Пространственный фильтр относительно прошлого среза...")
            prev_utm = run("native:reprojectlayer", {'INPUT': config.previous_path, 'TARGET_CRS': crs_utm,
                                                     'OUTPUT': 'TEMPORARY_OUTPUT'})
            prev_buf_utm = run("native:buffer", {'INPUT': prev_utm, 'DISTANCE': float(config.smart_merge_radius),
                                                 'DISSOLVE': True, 'OUTPUT': 'TEMPORARY_OUTPUT'})
            prev_buf = run("native:reprojectlayer", {'INPUT': prev_buf_utm, 'TARGET_CRS': raster_crs,
                                                     'OUTPUT': 'TEMPORARY_OUTPUT'})
            urban_filtered = run("native:extractbylocation", {'INPUT': urban_roi, 'PREDICATE': [0],
                                                              'INTERSECT': prev_buf, 'OUTPUT': 'TEMPORARY_OUTPUT'})
            merged = run("native:mergevectorlayers", {'LAYERS': [urban_filtered, config.previous_path],
                                                      'CRS': raster_crs, 'OUTPUT': 'TEMPORARY_OUTPUT'})
            urban_roi = run("native:dissolve", {'INPUT': merged, 'OUTPUT': 'TEMPORARY_OUTPUT'})

        # The previous epoch may use a different ROI or contain now-water pixels.
        if config.clip_to_roi:
            urban_roi = run("native:clip", {'INPUT': urban_roi, 'OVERLAY': roi_layer, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        urban_roi = run("native:difference", {'INPUT': urban_roi, 'OVERLAY': wet_eraser, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        urban_roi = run('native:clip', {'INPUT': urban_roi, 'OVERLAY': coverage_vec, 'OUTPUT': 'TEMPORARY_OUTPUT'})
        log_callback(f"[{config.year}] Подсчёт метрик...")
        layer = context.getMapLayer(urban_roi) if isinstance(urban_roi, str) else urban_roi
        if not layer or not layer.isValid():
            raise ValueError("Алгоритм не вернул валидный векторный слой.")
        metrics = measure_layer(layer, transform_context)

        log_callback(f"[{config.year}] Сохранение результата...")
        os.makedirs(config.output_folder, exist_ok=True)
        final_path = os.path.normpath(os.path.join(config.output_folder, f"Urban_Auto_{config.year}.gpkg")).replace('\\', '/')
        # Оставляем одно поле year вместо служебных DN/layer/path
        cleaned = run("native:refactorfields", {
            'INPUT': urban_roi,
            'FIELDS_MAPPING': [{'expression': str(int(config.year)), 'length': 0, 'name': 'year',
                                'precision': 0, 'type': 2}],
            'OUTPUT': 'TEMPORARY_OUTPUT'})
        processing.run("native:savefeatures", {'INPUT': cleaned, 'OUTPUT': final_path,
                                               'LAYER_NAME': f'urban_{config.year}'},
                       context=context, feedback=feedback)

        # Протокол расчёта — для воспроизводимости (приложение к ВКР)
        protocol = asdict(config)
        protocol.update(metrics=metrics, formula=formula, input_preparation=input_info, coverage_path=coverage_path,
                        status='complete', roi_source=roi_source, plugin_version=PLUGIN_VERSION,
                        timestamp=datetime.datetime.now().isoformat(timespec='seconds'), result=final_path)
        with open(os.path.join(config.output_folder, f"run_protocol_{config.year}.json"), 'w', encoding='utf-8') as fp:
            json.dump(protocol, fp, ensure_ascii=False, indent=2)

        if metrics_sink is not None:
            metrics_sink.update(metrics, mode=config.mode, formula=formula)
        return final_path, metrics['area_km2']

    finally:
        # Processing caches raster providers in its store. Close them while the
        # prepared files still exist (important for GDAL and Windows handles).
        context.temporaryLayerStore().removeAllMapLayers()
        resources.close()

