# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Алгоритмы Processing v4.2: временная согласованность ряда и оценка точности."""
import os
import re
import csv
import json
import datetime
import numpy as np
from osgeo import gdal, osr
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterMultipleLayers, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber, QgsProcessingParameterEnum, QgsProcessingParameterFolderDestination,
    QgsProcessingParameterVectorLayer, QgsProcessingOutputNumber,
    QgsProcessingOutputString, QgsProcessingException, QgsProcessing, QgsProcessingContext,
    QgsCoordinateReferenceSystem
)
from qgis.PyQt.QtCore import QCoreApplication
import processing

from .urban_auto_temporal import rasterize_layer, temporal_consistency, polygonize, pixel_area_km2, _grid
from .urban_auto_accuracy import confusion, checkerboard, format_report
from .urban_auto_core import utm_crs_for, PLUGIN_VERSION


def _tr(s):
    return QCoreApplication.translate('UrbanAutoTools', s)


def _year_of(layer):
    m = re.search(r'(19|20)\d{2}', layer.name()) or re.search(r'(19|20)\d{2}', os.path.basename(layer.source()))
    if m:
        return int(m.group(0))
    idx = layer.fields().indexOf('year')
    if idx >= 0:
        for f in layer.getFeatures():
            return int(f['year'])
    return None


class TemporalConsistencyAlgorithm(QgsProcessingAlgorithm):
    LAYERS, REF, MIN_CONFIRM, LAST_MODE, MIN_AREA, OUT = 'LAYERS', 'REF', 'MIN_CONFIRM', 'LAST_MODE', 'MIN_AREA_HA', 'OUTPUT_FOLDER'
    LAST_OPTIONS = ['подтверждать предыдущим срезом (нижняя оценка, рекомендуется)',
                    'принимать без подтверждения (верхняя оценка, предварительно)']

    def createInstance(self):
        return TemporalConsistencyAlgorithm()

    def name(self):
        return 'temporal_consistency'

    def displayName(self):
        return _tr('Временная согласованность ряда застройки')

    def group(self):
        return _tr('Городской анализ')

    def groupId(self):
        return 'urban_analysis'

    def shortHelpString(self):
        return _tr('Объединяет независимые годовые результаты в согласованный ряд. Новая застройка года принимается, '
                   'только если она встречается не менее чем в k последующих срезах; застройка не исчезает. '
                   'Заменяет простое накопление, при котором ложные пятна остаются навсегда. Год берётся из имени слоя '
                   '(Urban_Auto_2005) или поля year. Расчёт ведётся на сетке опорного растра.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(self.LAYERS, _tr('Годовые результаты (полигоны)'),
                                                               QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterRasterLayer(self.REF, _tr('Опорный растр (сетка, например NDBI)')))
        self.addParameter(QgsProcessingParameterNumber(self.MIN_CONFIRM, _tr('Подтверждений в последующих срезах (k)'),
                                                       defaultValue=2, minValue=1, maxValue=10))
        self.addParameter(QgsProcessingParameterEnum(self.LAST_MODE, _tr('Последний срез'), self.LAST_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.MIN_AREA, _tr('Минимальная площадь пятна, га'),
                                                       QgsProcessingParameterNumber.Double, defaultValue=5.0, minValue=0))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, _tr('Папка вывода')))
        self.addOutput(QgsProcessingOutputString('REPORT', _tr('Отчёт')))

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.LAYERS, context)
        ref = self.parameterAsRasterLayer(parameters, self.REF, context)
        k = self.parameterAsInt(parameters, self.MIN_CONFIRM, context)
        last_mode = ('backward', 'accept')[self.parameterAsEnum(parameters, self.LAST_MODE, context)]
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context) * 0.01  # км²
        out_dir = self.parameterAsString(parameters, self.OUT, context)
        os.makedirs(out_dir, exist_ok=True)
        if len(layers) < 2:
            raise QgsProcessingException(_tr('Нужно минимум два годовых слоя.'))
        grid = _grid(ref.source())
        if grid[0] * grid[1] > 16000000:
            raise QgsProcessingException(_tr('Слишком большая сетка; обрежьте опорный растр до ROI (лимит 16 млн пикселей).'))
        area = pixel_area_km2(grid)
        masks = {}
        for lyr in layers:
            y = _year_of(lyr)
            if y is None:
                raise QgsProcessingException(_tr(f'Не удалось определить год слоя {lyr.name()}'))
            if y in masks:
                raise QgsProcessingException(_tr(f'Повторный год: {y}.'))
            masks[y] = rasterize_layer(lyr, grid)
            feedback.pushInfo(f'{y}: исходная площадь {area[masks[y]].sum():.3f} км²')
        from .urban_auto_temporal import coverage_on_grid
        import numpy as np
        ds = gdal.Open(ref.source())
        b = ds.GetRasterBand(1); values = b.ReadAsArray(); nodata = b.GetNoDataValue()
        common = np.isfinite(values)
        if nodata is not None and not np.isnan(nodata):
            common &= values != nodata
        ds = None; b = None
        for layer in layers:
            year = _year_of(layer)
            coverage = os.path.join(os.path.dirname(layer.source().split('|')[0]), f'Valid_{year}.tif')
            if os.path.isfile(coverage):
                common &= coverage_on_grid(coverage, grid)
            else:
                feedback.pushInfo(f'{year}: маска наблюдений не найдена; охват ограничен только опорным растром.')
        if not common.any():
            raise QgsProcessingException(_tr('Нет общей наблюдаемой территории.'))
        masks = {y: m & common for y, m in masks.items()}
        years = sorted(masks)
        cons = temporal_consistency(masks, years, k, last_mode)
        rows = []
        prev_area = None
        for y in years:
            m = cons[y]
            try:
                from scipy import ndimage as ndi
            except ImportError:
                if min_area > 0:
                    raise QgsProcessingException(_tr('Для фильтра площади требуется scipy.'))
                ndi = None
            if min_area > 0 and ndi is not None:
                lab, n = ndi.label(m, np.ones((3, 3)))
                if n:
                    s = ndi.sum(area, lab, range(1, n + 1))
                    keep = np.zeros(n + 1, bool); keep[1:] = s >= min_area
                    m = keep[lab]
                    cons[y] = m
            path = os.path.join(out_dir, f'Urban_Consistent_{y}.gpkg')
            polygonize(m, grid, path, f'urban_{y}', y)
            a = float(area[m].sum())
            rows.append(dict(year=y, raw_km2=float(area[masks[y]].sum()), consistent_km2=a,
                             growth_km2=None if prev_area is None else a - prev_area,
                             temporal_status='provisional_last' if y == years[-1] else (
                                 'limited_future_support' if len(years)-years.index(y)-1 < k else 'future_supported')))
            prev_area = a
            context.addLayerToLoadOnCompletion(path, QgsProcessingContext.LayerDetails(
                f'Urban_Consistent_{y}', context.project(), 'OUTPUT'))
            feedback.pushInfo(f'{y}: согласованная площадь {a:.3f} км²')
        csv_path = os.path.join(out_dir, 'temporal_consistency_report.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['Год', 'Исходная площадь, км²', 'Согласованная площадь, км²', 'Прирост, км²', 'Статус подтверждения'])
            for r in rows:
                w.writerow([r['year'], f"{r['raw_km2']:.4f}".replace('.', ','), f"{r['consistent_km2']:.4f}".replace('.', ','),
                            '' if r['growth_km2'] is None else f"{r['growth_km2']:.4f}".replace('.', ','), r['temporal_status']])
        with open(os.path.join(out_dir, 'temporal_consistency_protocol.json'), 'w', encoding='utf-8') as f:
            json.dump(dict(plugin_version=PLUGIN_VERSION, min_confirm=k, last_mode=last_mode, min_area_ha=min_area * 100,
                           layers=[l.source() for l in layers], rows=rows, common_observed_area_km2=float(area[common].sum()),
                           timestamp=datetime.datetime.now().isoformat(timespec='seconds')), f, ensure_ascii=False, indent=2)
        return {'REPORT': csv_path}


class AccuracyAssessmentAlgorithm(QgsProcessingAlgorithm):
    RESULT, REFERENCE, MODE, GRID, BUF_U, BUF_N, WATER, BLOCK, OUT = (
        'RESULT', 'REFERENCE', 'MODE', 'GRID', 'URBAN_BUFFER', 'BACKGROUND_DISTANCE', 'WATER', 'BLOCK_M', 'OUTPUT_FOLDER')
    MODES = ['Объекты-эталон (здания): застройка ≤ буфера, фон > расстояния', 'Сплошная карта-эталон (все пиксели)']

    def createInstance(self):
        return AccuracyAssessmentAlgorithm()

    def name(self):
        return 'accuracy_assessment'

    def displayName(self):
        return _tr('Оценка точности застройки (матрица ошибок)')

    def group(self):
        return _tr('Городской анализ')

    def groupId(self):
        return 'urban_analysis'

    def shortHelpString(self):
        return _tr('Площадная матрица ошибок, общая точность, полнота, точность пользователя, каппа Коэна и F1. '
                   'Эталон — здания (например, OSM: застройка = в пределах буфера от здания, фон = дальше заданного '
                   'расстояния, полоса между ними не оценивается) или сплошная карта застройки. '
                   'Шахматная разбивка на калибровочные и контрольные клетки позволяет проверить, '
                   'не подогнаны ли опорные точки под эталон: ставьте точки только на калибровочных клетках.')

    def checkParameterValues(self, parameters, context):
        valid, message = super().checkParameterValues(parameters, context)
        if not valid:
            return valid, message
        if self.parameterAsEnum(parameters, self.MODE, context) == 0:
            urban = self.parameterAsDouble(parameters, self.BUF_U, context)
            background = self.parameterAsDouble(parameters, self.BUF_N, context)
            if background < urban:
                return False, _tr('Расстояние фона должно быть не меньше буфера застройки.')
        return True, ''

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(self.RESULT, _tr('Результат (полигоны застройки)'),
                                                            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.REFERENCE, _tr('Эталон (здания или карта застройки)'),
                                                            [QgsProcessing.TypeVectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterEnum(self.MODE, _tr('Тип эталона'), self.MODES, defaultValue=0))
        self.addParameter(QgsProcessingParameterRasterLayer(self.GRID, _tr('Растр-сетка (например NDBI)')))
        self.addParameter(QgsProcessingParameterNumber(self.BUF_U, _tr('Буфер застройки вокруг зданий, м'),
                                                       QgsProcessingParameterNumber.Double, defaultValue=30.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.BUF_N, _tr('Фон — дальше от зданий, чем, м'),
                                                       QgsProcessingParameterNumber.Double, defaultValue=300.0, minValue=0))
        self.addParameter(QgsProcessingParameterVectorLayer(self.WATER, _tr('Водные объекты (исключить из фона)'),
                                                            [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.BLOCK, _tr('Размер клетки шахматки, м (0 — без шахматки)'),
                                                       QgsProcessingParameterNumber.Double, defaultValue=1000.0, minValue=0))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, _tr('Папка отчёта')))
        for k in ['OA', 'KAPPA', 'F1', 'PRODUCER_ACC', 'USER_ACC', 'KAPPA_CHECK']:
            self.addOutput(QgsProcessingOutputNumber(k, k))
        self.addOutput(QgsProcessingOutputString('REPORT', _tr('Отчёт')))

    def _buffer_raster(self, layer, dist, grid, context, feedback):
        if dist <= 0:
            return rasterize_layer(layer, grid)
        c = layer.extent().center()
        crs4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        from qgis.core import QgsCoordinateTransform, QgsProject
        c = QgsCoordinateTransform(layer.crs(), crs4326, QgsProject.instance()).transform(c)
        utm = utm_crs_for(c.x(), c.y())
        rp = processing.run('native:reprojectlayer', {'INPUT': layer, 'TARGET_CRS': utm, 'OUTPUT': 'TEMPORARY_OUTPUT'},
                            context=context, feedback=feedback, is_child_algorithm=True)['OUTPUT']
        import tempfile, uuid
        tmp = os.path.join(tempfile.gettempdir(), f'_urban_buf_{int(dist)}_{uuid.uuid4().hex[:8]}.gpkg')
        processing.run('native:buffer', {'INPUT': rp, 'DISTANCE': dist, 'SEGMENTS': 8, 'DISSOLVE': True, 'OUTPUT': tmp},
                       context=context, feedback=feedback, is_child_algorithm=True)
        arr = rasterize_layer(tmp, grid)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return arr

    def processAlgorithm(self, parameters, context, feedback):
        res_l = self.parameterAsVectorLayer(parameters, self.RESULT, context)
        ref_l = self.parameterAsVectorLayer(parameters, self.REFERENCE, context)
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        grid_l = self.parameterAsRasterLayer(parameters, self.GRID, context)
        bu = self.parameterAsDouble(parameters, self.BUF_U, context)
        bn = self.parameterAsDouble(parameters, self.BUF_N, context)
        water_l = self.parameterAsVectorLayer(parameters, self.WATER, context)
        block = self.parameterAsDouble(parameters, self.BLOCK, context)
        out_dir = self.parameterAsString(parameters, self.OUT, context)
        if mode == 0 and bn < bu:
            raise QgsProcessingException(_tr('Расстояние фона должно быть не меньше буфера застройки.'))
        os.makedirs(out_dir, exist_ok=True)

        grid = _grid(grid_l.source())
        if grid[0] * grid[1] > 16000000:
            raise QgsProcessingException(_tr('Слишком большая сетка оценки; обрежьте растр до ROI.'))
        area = pixel_area_km2(grid)
        ds = gdal.Open(grid_l.source())
        band = ds.GetRasterBand(1).ReadAsArray()
        nod = ds.GetRasterBand(1).GetNoDataValue()
        valid = np.isfinite(band.astype(float))
        if nod is not None and not np.isnan(nod):
            valid &= band != nod
        ds = None

        pred = rasterize_layer(res_l, grid)
        if mode == 1:
            ref_u = rasterize_layer(ref_l, grid) & valid
            ref_n = ~ref_u & valid
            notes = ['Эталон — сплошная карта застройки; оцениваются все пиксели растра.']
        else:
            feedback.pushInfo('Буферы вокруг объектов эталона...')
            ref_u = self._buffer_raster(ref_l, bu, grid, context, feedback) & valid
            near = self._buffer_raster(ref_l, bn, grid, context, feedback)
            ref_n = ~near & valid
            notes = [f'Эталон «застройка»: пиксели не дальше {bu:.0f} м от объектов эталона; «фон»: дальше {bn:.0f} м.',
                     f'Полоса {bu:.0f}–{bn:.0f} м не оценивается.']
        if water_l is not None:
            wat = rasterize_layer(water_l, grid)
            ref_n &= ~wat
            notes.append('Водные объекты исключены из фона.')
        if not ref_u.any():
            raise QgsProcessingException(_tr('Эталон не содержит застройки в охвате растра сетки: проверьте СК и охват слоёв.'))
        if not pred.any():
            notes.append('ВНИМАНИЕ: результат не пересекается с растром сетки (нет застройки в охвате).')
        srs = osr.SpatialReference(); srs.ImportFromWkt(grid[3])
        r_all = confusion(pred, ref_u, ref_n, area)
        r_chk = r_cal = None
        if block > 0:
            chk = checkerboard(pred.shape, grid[2], srs.IsGeographic(), block, srs.GetLinearUnits() or 1.0)
            r_chk = confusion(pred, ref_u, ref_n, area, chk)
            r_cal = confusion(pred, ref_u, ref_n, area, ~chk)
            notes.append(f'Шахматка {block:.0f}×{block:.0f} м: контрольные клетки — нечётные (оценка без участия в калибровке).')
        if r_all is None:
            raise QgsProcessingException(_tr('Нет пикселей для оценки: проверьте охват эталона и растра.'))
        notes.append(f'Площадь результата: {area[pred].sum():.3f} км²; Urban Auto {PLUGIN_VERSION}.')
        notes.append('Шахматка является независимым контролем только если её контрольные клетки не использовались для выбора точек и параметров.')
        title = f'ОЦЕНКА ТОЧНОСТИ: {res_l.name()} относительно {ref_l.name()}'
        txt = format_report(title, r_all, r_chk, r_cal, notes)
        base = os.path.join(out_dir, f'accuracy_{re.sub(r"[^0-9A-Za-zА-Яа-я_]+", "_", res_l.name())}')
        with open(base + '.txt', 'w', encoding='utf-8') as f:
            f.write(txt)
        with open(base + '.json', 'w', encoding='utf-8') as f:
            json.dump(dict(all=r_all, check=r_chk, calibration=r_cal, notes=notes), f, ensure_ascii=False, indent=2)
        feedback.pushInfo(txt)
        result = {'OA': r_all['OA'], 'KAPPA': r_all['kappa'], 'F1': r_all['F1'],
                  'PRODUCER_ACC': r_all['producer_acc'], 'USER_ACC': r_all['user_acc'],
                  'REPORT': base + '.txt'}
        if r_chk is not None:
            result['KAPPA_CHECK'] = r_chk['kappa']
        return result
