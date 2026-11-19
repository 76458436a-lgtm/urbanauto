# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Алгоритмы Processing 5.0: годовой ряд застройки без ручных точек; точки для разметки пользователем."""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterVectorLayer,
                       QgsProcessingParameterNumber, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFile, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString, QgsProcessingOutputString, QgsProcessing,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProcessingParameterBoolean,
                       QgsProcessingParameterEnum)


def _vector_path(layer, out_dir, tag):
    """Слой QGIS → путь к файлу (подслой GeoPackage выносится в отдельный файл)."""
    from .urban_auto_temporal import vector_source
    path, name, tmp = vector_source(layer)
    if name:
        from osgeo import gdal
        single = os.path.join(out_dir, f'_{tag}.gpkg')
        ds = gdal.VectorTranslate(single, path, format='GPKG', layers=[name]); ds = None  # noqa: F841
        return single, tmp
    return path, tmp


def _cleanup(tmp):
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass


class RFSeriesAlgorithm(QgsProcessingAlgorithm):
    ROI, CITY, START, END, RES, REF, COMP, VALPTS, USERPTS, S2, MODEL, MINRUN, MINAREA, REPYEARS, ADD, OUT, DEF = (
        'ROI', 'CITY', 'START_YEAR', 'END_YEAR', 'RESOLUTION', 'REFERENCE', 'COMPOSITES', 'VALIDATION_POINTS',
        'USER_POINTS', 'USE_S2', 'MODEL', 'MIN_RUN', 'MIN_AREA_HA', 'REPORT_YEARS', 'ADD_LAYERS', 'OUTPUT_FOLDER', 'DEFINITION')
    MODELS = ['auto', 'hgb', 'rf']
    DEFS = ['strict', 'territory']

    def name(self):
        return 'rf_series'

    def displayName(self):
        return 'Годовой ряд застройки (автоматически, без точек)'

    def group(self):
        return 'Городской анализ'

    def groupId(self):
        return 'urban_analysis'

    def shortHelpString(self):
        return ('Полностью автоматический анализ застройки за каждый год. Укажите территорию полигоном или просто '
                'название города — охват определится по фактической застройке. Модуль сам скачивает летние композиты '
                'Landsat и Sentinel-2 (с 2018 г., 10 м), строит обучающие метки по согласию открытых продуктов (GHSL, WSF, '
                'ESA WorldCover, здания OSM и Microsoft), обучает модель на четыре класса (растительность, застройка, '
                'открытый грунт, вода), находит год появления застройки и долю застроенной поверхности в каждом пикселе.\n\n'
                'Результаты: BuiltYear.tif, BuiltYear_confidence.tif, Prob_built_by_year.tif, Built_fraction_by_year.tif, '
                'New_built_<годы>.gpkg (пятна новой застройки с годом, площадью и уверенностью), Urban_<год>.gpkg, '
                'BareSoil_persistent, annual_series.csv, report.html, rf_series_protocol.json.\n\n'
                'Определение застройки: «строгое» — здания, дворы, промплощадки и покрытия, огороды и газоны без построек '
                'не считаются (лучше для малых городов с частным сектором); «территория» — кварталы целиком, с дворами и '
                'зеленью (как в 5.0 и в ВКР по Ураю).\n\n'
                'Точки пользователя (необязательно): слой с полем label (1 — застройка, 0 — нет) и, если нужно, year. '
                'Их удобно получить инструментом «Точки для разметки» по готовому результату. '
                'Точки независимой проверки исключаются из обучения вместе с окрестностью 120 м.')

    def createInstance(self):
        return RFSeriesAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(self.ROI, 'Территория (полигон)', [QgsProcessing.TypeVectorPolygon],
                                                            optional=True))
        self.addParameter(QgsProcessingParameterString(self.CITY, '…или название города (если полигон не задан)',
                                                       optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.START, 'Первый год', defaultValue=2000, minValue=1985, maxValue=2100))
        self.addParameter(QgsProcessingParameterNumber(self.END, 'Последний год', defaultValue=2025, minValue=1985, maxValue=2100))
        self.addParameter(QgsProcessingParameterNumber(self.RES, 'Размер пикселя, м (UTM)', QgsProcessingParameterNumber.Double,
                                                       defaultValue=30.0, minValue=10.0, maxValue=120.0))
        self.addParameter(QgsProcessingParameterRasterLayer(self.REF, 'Опорный растр сетки (вместо UTM, необязательно)', optional=True))
        self.addParameter(QgsProcessingParameterFile(self.COMP, 'Папка готовых композитов (необязательно)',
                                                     behavior=QgsProcessingParameterFile.Folder, optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.VALPTS, 'Точки проверки (поле label: 1/0 — точность в отчёт; в обучение не входят)',
                                                            [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.USERPTS, 'Точки пользователя (поле label: 1/0)',
                                                            [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.DEF, 'Определение застройки',
                                                     ['строгое: здания и покрытия (по умолчанию)',
                                                      'территория: кварталы вместе с дворами и зеленью (как в 5.0)'],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean('SENTINEL_SERIES',
            'Строить основной ряд по Sentinel-2 (с 2018 г.; для сетки 10 м задайте размер пикселя 10)', defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(self.S2, 'Добавить признаки Sentinel-2 10 м (с 2018 г., дольше)', defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum(self.MODEL, 'Модель', ['авто (градиентный бустинг)',
                                                                            'градиентный бустинг', 'Random Forest'],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean('ADAPTIVE_EDGE',
            'Показывать предварительный прирост у конца ряда при неполном сроке подтверждения', defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(self.MINRUN, 'Минимум лет подтверждения новой застройки',
                                                       defaultValue=3, minValue=1, maxValue=5))
        self.addParameter(QgsProcessingParameterNumber(self.MINAREA, 'Минимальная площадь пятна в полигонах, га',
                                                       QgsProcessingParameterNumber.Double, defaultValue=0.5, minValue=0))
        self.addParameter(QgsProcessingParameterString(self.REPYEARS, 'Годы для полигонов (через запятую)',
                                                       defaultValue='2000,2005,2010,2015,2020,2025', optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.ADD, 'Добавить результаты в проект', defaultValue=True))
        for key, title, default in [
                ('TEMPORAL', 'Использовать временные признаки', True),
                ('WEIGHT_OBS', 'Учитывать число ясных наблюдений', True),
                ('FRACTION', 'Рассчитывать долю застройки', True),
                ('CALIBRATE', 'Подбирать порог по обучающим точкам пользователя', False)]:
            self.addParameter(QgsProcessingParameterBoolean(key, title, defaultValue=default))
        for key, title, default, lo, hi in [
                ('N_TREES', 'Число деревьев Random Forest', 200, 20, 2000),
                ('N_PER_CLASS', 'Обучающих примеров на класс и год', 2000, 100, 20000),
                ('WORKERS', 'Потоков загрузки', 6, 1, 16),
                ('PROTECT_RADIUS', 'Радиус исключения точек проверки, пиксели (0 — автоматически)', 0, 0, 100),
                ('GROWTH_SINCE', 'Начало периода прироста (0 — первый год)', 0, 0, 2100),
                ('MIN_COVERAGE', 'Исключать годы, где снимок покрывает меньше, % территории (0 — не исключать)', 0, 0, 100)]:
            self.addParameter(QgsProcessingParameterNumber(key, title, defaultValue=default, minValue=lo, maxValue=hi))
        self.addParameter(QgsProcessingParameterEnum('COST', 'Мера ошибки определения года',
                                                     ['Линейная', 'Логарифмическая'], defaultValue=0))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, 'Папка результатов'))
        self.addOutput(QgsProcessingOutputString('PROTOCOL', 'Протокол'))
        self.addOutput(QgsProcessingOutputString('REPORT', 'Отчёт HTML'))

    def processAlgorithm(self, parameters, context, feedback):
        from .urban_auto_deps import missing, install_hint
        if missing():
            raise QgsProcessingException('Не хватает библиотек Python: ' + ', '.join(missing()) + '.\n' + install_hint())
        from . import urban_auto_series as S
        roi = self.parameterAsVectorLayer(parameters, self.ROI, context)
        city = (self.parameterAsString(parameters, self.CITY, context) or '').strip()
        if roi is None and not city:
            raise QgsProcessingException('Задайте территорию полигоном или название города.')
        y0 = self.parameterAsInt(parameters, self.START, context)
        y1 = self.parameterAsInt(parameters, self.END, context)
        if y1 - y0 < 2:
            raise QgsProcessingException('Нужно не меньше трёх лет.')
        if self.parameterAsBool(parameters, 'SENTINEL_SERIES', context) and y0 < 2018:
            raise QgsProcessingException('Для основного ряда Sentinel-2 выберите годы с 2018.')
        growth = self.parameterAsInt(parameters, 'GROWTH_SINCE', context)
        if growth and not y0 <= growth <= y1:
            raise QgsProcessingException('Начало прироста должно попадать в выбранный период.')
        out_dir = self.parameterAsString(parameters, self.OUT, context)
        os.makedirs(out_dir, exist_ok=True)
        ref = self.parameterAsRasterLayer(parameters, self.REF, context)
        res_m = self.parameterAsDouble(parameters, self.RES, context)
        roi_mask, title = None, city or None
        if roi is not None:
            xf = QgsCoordinateTransform(roi.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), context.transformContext())
            e = xf.transformBoundingBox(roi.extent())
            bbox = (e.xMinimum() - 0.01, e.yMinimum() - 0.005, e.xMaximum() + 0.01, e.yMaximum() + 0.005)
            grid = S.grid_from_raster(ref.source()) if ref is not None else S.utm_grid_for(bbox, res_m)
        elif ref is not None:
            grid = S.grid_from_raster(ref.source())
        else:
            try:
                grid, info = S.grid_for_city(city, res_m, log=feedback.pushInfo)
            except Exception as ex:
                raise QgsProcessingException(f'Не удалось определить охват «{city}»: {ex}')
            title = f'{city}: застройка по годам'
        if grid[1] * grid[2] > 12_000_000:
            raise QgsProcessingException(f'Сетка {grid[1]}×{grid[2]} слишком велика; уменьшите территорию или увеличьте пиксель.')
        if roi is not None:
            path, tmp = _vector_path(roi, out_dir, 'roi')
            try:
                roi_mask = S.mask_from_vector(path, grid)
            finally:
                _cleanup(tmp)
            if not roi_mask.any():
                raise QgsProcessingException('Полигон территории не попал на сетку.')
        protect, check = None, None
        pts = self.parameterAsVectorLayer(parameters, self.VALPTS, context)
        if pts is not None:
            path, tmp = _vector_path(pts, out_dir, 'val')
            try:
                protect = S.points_to_rc(path, grid)
                feedback.pushInfo(f'Из обучения исключены окрестности {len(protect[0])} точек проверки.')
                check = S.read_user_points(path, grid) or None
                if check:
                    feedback.pushInfo(f'Размеченных точек проверки: {len(check)} — точность попадёт в отчёт.')
            finally:
                _cleanup(tmp)
        user = None
        up = self.parameterAsVectorLayer(parameters, self.USERPTS, context)
        if up is not None:
            path, tmp = _vector_path(up, out_dir, 'user')
            try:
                user = S.read_user_points(path, grid)
                feedback.pushInfo(f'Точек пользователя с меткой: {len(user)}.')
            finally:
                _cleanup(tmp)
        text = self.parameterAsString(parameters, self.REPYEARS, context) or ''
        try:
            rep = [int(s) for s in text.replace(';', ',').split(',') if s.strip()]
        except ValueError:
            raise QgsProcessingException('Годы полигонов задаются целыми числами через запятую.')
        rep = sorted(set(y for y in rep if y0 <= y <= y1))
        comp = self.parameterAsString(parameters, self.COMP, context) or None
        res = S.run_series(out_dir, grid, list(range(y0, y1 + 1)), roi_mask=roi_mask, composites_dir=comp,
                           protect_points=protect, user_points=user, check_points=check,
                           use_s2=self.parameterAsBool(parameters, self.S2, context),
                           model=self.MODELS[self.parameterAsEnum(parameters, self.MODEL, context)],
                           mode=self.DEFS[self.parameterAsEnum(parameters, self.DEF, context)],
                           sentinel_series=self.parameterAsBool(parameters, 'SENTINEL_SERIES', context),
                           edge_policy='adaptive' if self.parameterAsBool(parameters, 'ADAPTIVE_EDGE', context) else 'strict',
                           min_run=self.parameterAsInt(parameters, self.MINRUN, context),
                           min_area_ha=self.parameterAsDouble(parameters, self.MINAREA, context),
                           temporal=self.parameterAsBool(parameters, 'TEMPORAL', context),
                           weight_obs=self.parameterAsBool(parameters, 'WEIGHT_OBS', context),
                           fraction=self.parameterAsBool(parameters, 'FRACTION', context),
                           calibrate=self.parameterAsBool(parameters, 'CALIBRATE', context),
                           n_trees=self.parameterAsInt(parameters, 'N_TREES', context),
                           n_per_class=self.parameterAsInt(parameters, 'N_PER_CLASS', context),
                           workers=self.parameterAsInt(parameters, 'WORKERS', context),
                           protect_radius_px=self.parameterAsInt(parameters, 'PROTECT_RADIUS', context) or None,
                           growth_since=growth or None,
                           min_coverage=self.parameterAsInt(parameters, 'MIN_COVERAGE', context) / 100.0,
                           cost=('linear', 'log')[self.parameterAsEnum(parameters, 'COST', context)],
                           report_years=rep or None, log=feedback.pushInfo, title=title,
                           progress=feedback.setProgress, cancel=feedback.isCanceled)
        self._outputs = res['outputs'] if self.parameterAsBool(parameters, self.ADD, context) else None
        return {'PROTOCOL': res['outputs']['protocol'], 'REPORT': res['outputs'].get('report', '')}

    def postProcessAlgorithm(self, context, feedback):
        out = getattr(self, '_outputs', None)
        if out:
            from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
            proj = QgsProject.instance()
            lyr = QgsRasterLayer(out['built_year'], 'Год появления застройки')
            if lyr.isValid():
                proj.addMapLayer(lyr)
            for y, p in sorted(out.get('polygons', {}).items()):
                v = QgsVectorLayer(p, f'Застройка {y}', 'ogr')
                if v.isValid():
                    proj.addMapLayer(v)
            if out.get('growth'):
                v = QgsVectorLayer(out['growth'], 'Новая застройка (пятна с годом)', 'ogr')
                if v.isValid():
                    proj.addMapLayer(v)
            v = QgsVectorLayer(out['bare_polygons'], 'Открытый грунт (устойчивый)', 'ogr')
            if v.isValid():
                proj.addMapLayer(v)
        return {}


class LabelPointsAlgorithm(QgsProcessingAlgorithm):
    """5.0: точки, где модель сомневается, — для быстрой разметки пользователем (активное обучение)."""
    FOLDER, N, ADD, PURPOSE = 'RESULT_FOLDER', 'N_POINTS', 'ADD_LAYER', 'PURPOSE'

    def name(self):
        return 'label_points'

    def displayName(self):
        return 'Точки для разметки (где модель сомневается)'

    def group(self):
        return 'Городской анализ'

    def groupId(self):
        return 'urban_analysis'

    def shortHelpString(self):
        return ('По папке готового результата «Годового ряда застройки» выбирает точки, где вероятность застройки '
                'в последний год ближе всего к 50 %, с разносом не меньше 150 м. Откройте слой, сверьте каждую точку со '
                'снимком высокого разрешения и заполните поле label: 1 — застройка, 0 — нет (поле year — если метка '
                'относится не к последнему году). Затем запустите расчёт заново, указав этот слой как «Точки пользователя». '
                'Обычно 30–50 точек (10–15 минут) заметно улучшают карту там, где автоматические метки ошибаются; '
                'по этим же точкам модуль подбирает порог «застроено».\n\n'
                'Точки «для проверки точности» выбираются случайно, поровну в застройке и вне её. Разметьте их так же и '
                'передайте в расчёт параметром «Точки проверки»: в обучение они не входят, а отчёт покажет каппу, '
                'общую точность и площадь застройки с 95 % доверительным интервалом.')

    def createInstance(self):
        return LabelPointsAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.FOLDER, 'Папка результата', behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterEnum(self.PURPOSE, 'Назначение точек',
                                                     ['для обучения — где модель сомневается',
                                                      'для проверки точности — случайно по классам карты'], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.N, 'Число точек', defaultValue=40, minValue=5, maxValue=500))
        self.addParameter(QgsProcessingParameterBoolean(self.ADD, 'Добавить слой в проект', defaultValue=True))
        self.addOutput(QgsProcessingOutputString('POINTS', 'Точки'))

    def processAlgorithm(self, parameters, context, feedback):
        from . import urban_auto_series as S
        folder = self.parameterAsString(parameters, self.FOLDER, context)
        if not os.path.exists(os.path.join(folder, 'Prob_built_by_year.tif')):
            raise QgsProcessingException('В папке нет Prob_built_by_year.tif — сначала выполните «Годовой ряд застройки».')
        purpose = ('train', 'check')[self.parameterAsEnum(parameters, self.PURPOSE, context)]
        path = S.make_label_points(folder, self.parameterAsInt(parameters, self.N, context), purpose=purpose)
        self._name = 'Точки для разметки' if purpose == 'train' else 'Точки для проверки точности'
        self._path = path if self.parameterAsBool(parameters, self.ADD, context) else None
        return {'POINTS': path}

    def postProcessAlgorithm(self, context, feedback):
        if getattr(self, '_path', None):
            from qgis.core import QgsProject, QgsVectorLayer
            v = QgsVectorLayer(self._path, getattr(self, '_name', 'Точки для разметки'), 'ogr')
            if v.isValid():
                v.setName(v.name())
                QgsProject.instance().addMapLayer(v)
        return {}
