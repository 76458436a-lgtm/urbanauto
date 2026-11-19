# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingParameterNumber, QgsProcessingParameterPoint,
    QgsProcessingParameterMatrix, QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean, QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
    QgsProcessingOutputVectorLayer, QgsProcessingOutputNumber, QgsProcessingException,
    QgsCoordinateReferenceSystem, QgsProcessingContext, QgsProcessingParameterEnum
)
from qgis.PyQt.QtCore import QCoreApplication
from .urban_auto_core import run_urban_analysis_core, UrbanConfig
from .urban_auto_thresholds import BOX_METHODS


class UrbanAutoProcessingAlgorithm(QgsProcessingAlgorithm):
    YEAR = 'YEAR'
    CENTER = 'CENTER'
    RADIUS = 'RADIUS'
    USE_OSM = 'USE_OSM'
    OSM_ADMIN_LEVEL = 'OSM_ADMIN_LEVEL'
    NDBI_LAYER = 'NDBI_LAYER'
    BSI_LAYER = 'BSI_LAYER'
    NDVI_LAYER = 'NDVI_LAYER'
    MNDWI_LAYER = 'MNDWI_LAYER'
    USE_OTSU = 'USE_OTSU'
    USE_BALANCE = 'USE_BALANCE'
    USE_BALANCE_AI = 'USE_BALANCE_AI'
    BALANCE_THRESHOLD = 'BALANCE_THRESHOLD'
    PREV_SHAPE = 'PREV_SHAPE'
    SMART_MERGE_RADIUS = 'SMART_MERGE_RADIUS'
    CLOSE_DIST = 'CLOSE_DIST'
    SHRINK_DIST = 'SHRINK_DIST'
    MIN_AREA_HA = 'MIN_AREA_HA'
    CLIP_TO_ROI = 'CLIP_TO_ROI'
    SEED_URBAN = 'SEED_URBAN'
    SEED_NON_URBAN = 'SEED_NON_URBAN'
    OUTPUT_FOLDER = 'OUTPUT_FOLDER'
    OUTPUT_LAYER = 'OUTPUT_LAYER'
    AREA_KM2 = 'AREA_KM2'
    BOX_METHOD = 'BOX_METHOD'
    AUTO_ALIGN, MAX_PIXELS, ROI = 'AUTO_ALIGN', 'MAX_PIXELS', 'ROI'

    BOX_DEFAULTS = {
        1: (-0.25, -0.05, -0.10, 0.05, 0.65),
        2: (-0.10, 0.35, -0.02, 0.35, 0.65),
        3: (-0.05, 0.20, 0.01, 0.25, 0.75),
    }
    BOX_KEYS = ('NDBI_MIN', 'NDBI_MAX', 'BSI_MIN', 'BSI_MAX', 'NDVI_MAX')

    def tr(self, string):
        return QCoreApplication.translate('UrbanAutoProcessingAlgorithm', string)

    def createInstance(self):
        return UrbanAutoProcessingAlgorithm()

    def checkParameterValues(self, parameters, context):
        valid, message = super().checkParameterValues(parameters, context)
        if not valid:
            return valid, message
        if not self.parameterAsBool(parameters, self.CLIP_TO_ROI, context):
            return False, self.tr('В 4.3 обрезка по ROI обязательна.')
        return True, ''

    def name(self):
        return 'urban_auto_processing'

    def displayName(self):
        return self.tr('Авто-калибровка городской застройки')

    def group(self):
        return self.tr('Городской анализ')

    def groupId(self):
        return 'urban_analysis'

    def shortHelpString(self):
        return self.tr(
            "Выделение городской застройки по спектральным индексам NDBI, BSI, NDVI и MNDWI.\n\n"
            "Пороги: (1) по точкам города — три «бокса» NDBI×BSI; (2) ручные боксы; (3) метод Оцу. "
            "Дополнительно — порог спектрального баланса NDBI−NDVI (K-Means или вручную).\n"
            "Далее: удаление воды (MNDWI>0), морфологическое замыкание буферами, фильтр по площади, "
            "обрезка по ROI (круг или граница OSM). Сохраняется GeoPackage и JSON-протокол расчёта.")

    def initAlgorithm(self, config=None):
        num = QgsProcessingParameterNumber
        self.addParameter(QgsProcessingParameterRasterLayer(self.NDBI_LAYER, self.tr('Слой NDBI')))
        self.addParameter(QgsProcessingParameterRasterLayer(self.BSI_LAYER, self.tr('Слой BSI')))
        self.addParameter(QgsProcessingParameterRasterLayer(self.NDVI_LAYER, self.tr('Слой NDVI')))
        self.addParameter(QgsProcessingParameterRasterLayer(self.MNDWI_LAYER, self.tr('Слой MNDWI (max)')))
        self.addParameter(num(self.YEAR, self.tr('Год'), defaultValue=2025, minValue=1970, maxValue=2100))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.PREV_SHAPE, self.tr('Результат прошлого среза (накопительный рост)'), optional=True))
        # Числовые параметры в метрах (раньше Distance-параметры брали единицы СК слоя — градусы)
        self.addParameter(num(self.SMART_MERGE_RADIUS, self.tr('Радиус привязки к прошлому срезу, м'),
                              type=num.Double, defaultValue=300.0, minValue=0))

        self.addParameter(QgsProcessingParameterPoint(self.CENTER, self.tr('Центр города')))
        self.addParameter(QgsProcessingParameterVectorLayer(self.ROI, self.tr('Полигон ROI (приоритет над кругом/OSM)'), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.AUTO_ALIGN, self.tr('Автоматически выровнять растры и маску NoData'), defaultValue=True))
        self.addParameter(num(self.MAX_PIXELS, self.tr('Лимит пикселей в ROI'), defaultValue=16000000, minValue=100))
        self.addParameter(num(self.RADIUS, self.tr('Радиус ROI, км'), type=num.Double, defaultValue=15.0, minValue=0.1))
        self.addParameter(QgsProcessingParameterBoolean(self.USE_OSM, self.tr('ROI по границе OSM'), defaultValue=False))
        self.addParameter(num(self.OSM_ADMIN_LEVEL, self.tr('OSM admin_level'), defaultValue=8, minValue=2, maxValue=11))

        self.addParameter(QgsProcessingParameterBoolean(self.USE_OTSU, self.tr('Метод Оцу (авто-пороги)'), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(self.USE_BALANCE, self.tr('Спектральный баланс NDBI−NDVI'), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(self.USE_BALANCE_AI, self.tr('Порог баланса по K-Means'), defaultValue=True))
        self.addParameter(num(self.BALANCE_THRESHOLD, self.tr('Смещение K-Means или ручной порог NDBI−NDVI'),
                              type=num.Double, defaultValue=0.0))

        for i, defaults in self.BOX_DEFAULTS.items():
            self.addParameter(QgsProcessingParameterBoolean(f'MANUAL_BOX{i}', self.tr(f'Включить BOX{i} (ручной)'),
                                                            defaultValue=False))
            labels = ('NDBI min', 'NDBI max', 'BSI min', 'BSI max', 'NDVI <')
            for key, lab, dv in zip(self.BOX_KEYS, labels, defaults):
                self.addParameter(num(f'BOX{i}_{key}', self.tr(f'BOX{i} {lab}'), type=num.Double, defaultValue=dv))

        self.addParameter(QgsProcessingParameterMatrix(self.SEED_URBAN, self.tr('Точки ГОРОДА (долгота, широта WGS84)'),
                                                       numberRows=1, headers=['Lon', 'Lat'],
                                                       optional=True, defaultValue=[]))
        self.addParameter(QgsProcessingParameterEnum(self.BOX_METHOD, self.tr('Пороги по точкам'),
                                                     [t for _, t in BOX_METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterMatrix(self.SEED_NON_URBAN, self.tr('Точки ФОНА (опционально)'),
                                                       numberRows=1, headers=['Lon', 'Lat'], optional=True))

        self.addParameter(num(self.CLOSE_DIST, self.tr('Морфология: буфер наружу, м'), type=num.Double, defaultValue=200.0, minValue=0))
        self.addParameter(num(self.SHRINK_DIST, self.tr('Морфология: буфер внутрь, м'), type=num.Double, defaultValue=200.0, minValue=0))
        self.addParameter(num(self.MIN_AREA_HA, self.tr('Минимальная площадь пятна, га'), type=num.Double, defaultValue=5.0, minValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.CLIP_TO_ROI, self.tr('Обрезать по ROI'), defaultValue=True))

        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, self.tr('Папка вывода')))
        self.addOutput(QgsProcessingOutputVectorLayer(self.OUTPUT_LAYER, self.tr('Границы застройки')))
        self.addOutput(QgsProcessingOutputNumber(self.AREA_KM2, self.tr('Площадь, км²')))

    @staticmethod
    def parse_matrix(matrix):
        pts = []
        for i in range(0, len(matrix or []) - 1, 2):
            try:
                lon, lat = float(matrix[i]), float(matrix[i + 1])
            except (TypeError, ValueError):
                continue
            if lon or lat:
                pts.append((lon, lat))
        return pts

    def processAlgorithm(self, parameters, context, feedback):
        L = lambda k: self.parameterAsRasterLayer(parameters, k, context)
        ndbi, bsi, ndvi, mndwi = L(self.NDBI_LAYER), L(self.BSI_LAYER), L(self.NDVI_LAYER), L(self.MNDWI_LAYER)
        if not all([ndbi, bsi, ndvi, mndwi]):
            raise QgsProcessingException(self.tr("Не выбраны все растровые слои."))

        boxes = []
        for i in self.BOX_DEFAULTS:
            if self.parameterAsBool(parameters, f'MANUAL_BOX{i}', context):
                boxes.append(tuple(self.parameterAsDouble(parameters, f'BOX{i}_{k}', context) for k in self.BOX_KEYS))

        threshold_options = dict(
            use_otsu=self.parameterAsBool(parameters, self.USE_OTSU, context),
            manual_boxes=boxes,
            urban_points=self.parse_matrix(self.parameterAsMatrix(parameters, self.SEED_URBAN, context)),
            nonurban_points=self.parse_matrix(self.parameterAsMatrix(parameters, self.SEED_NON_URBAN, context)),
            use_balance=self.parameterAsBool(parameters, self.USE_BALANCE, context),
            balance_ai=self.parameterAsBool(parameters, self.USE_BALANCE_AI, context),
            balance_value=self.parameterAsDouble(parameters, self.BALANCE_THRESHOLD, context),
            box_method=BOX_METHODS[self.parameterAsEnum(parameters, self.BOX_METHOD, context)][0])
        roi = self.parameterAsVectorLayer(parameters, self.ROI, context)
        from .urban_auto_temporal import vector_source
        roi_path, roi_name, roi_tmp = vector_source(roi) if roi else (None, None, None)
        if roi_path and roi_name:
            roi_path += '|layername=' + roi_name
        prev_layer = self.parameterAsVectorLayer(parameters, self.PREV_SHAPE, context)
        # центр всегда переводим в WGS84 (раньше брались координаты в СК проекта)
        center = self.parameterAsPoint(parameters, self.CENTER, context, QgsCoordinateReferenceSystem("EPSG:4326"))
        year = self.parameterAsInt(parameters, self.YEAR, context)

        config = UrbanConfig(
            year=year,
            center=(center.x(), center.y()),
            radius_km=self.parameterAsDouble(parameters, self.RADIUS, context),
            roi_type='polygon' if roi else ('osm' if self.parameterAsBool(parameters, self.USE_OSM, context) else 'circle'),
            osm_level=self.parameterAsInt(parameters, self.OSM_ADMIN_LEVEL, context),
            polygon_layer_id=None,
            formula="",
            output_folder=self.parameterAsString(parameters, self.OUTPUT_FOLDER, context),
            ndbi_path=ndbi.source(), bsi_path=bsi.source(), ndvi_path=ndvi.source(), mndwi_path=mndwi.source(),
            is_geo=ndbi.crs().isGeographic(),
            crs_authid=ndbi.crs().authid(),
            previous_path=prev_layer.source() if prev_layer else None,
            smart_merge_radius=self.parameterAsDouble(parameters, self.SMART_MERGE_RADIUS, context),
            close_dist=self.parameterAsDouble(parameters, self.CLOSE_DIST, context),
            shrink_dist=self.parameterAsDouble(parameters, self.SHRINK_DIST, context),
            min_area_m2=self.parameterAsDouble(parameters, self.MIN_AREA_HA, context) * 10000.0,
            clip_to_roi=self.parameterAsBool(parameters, self.CLIP_TO_ROI, context),
            threshold_options=threshold_options,
            polygon_path=roi_path,
            auto_align=self.parameterAsBool(parameters, self.AUTO_ALIGN, context),
            max_pixels=self.parameterAsInt(parameters, self.MAX_PIXELS, context),
        )
        try:
            final_path, area = run_urban_analysis_core(config, feedback.pushInfo, context=context, feedback=feedback)
        finally:
            if roi_tmp:
                import os
                try:
                    os.remove(roi_tmp)
                except OSError:
                    pass
        feedback.pushInfo(f"Площадь застройки {year}: {area:.3f} км²")

        # Загрузка результата в проект — штатно через контекст (потокобезопасно)
        context.addLayerToLoadOnCompletion(final_path, QgsProcessingContext.LayerDetails(
            f"Urban_Auto_{year}", context.project(), self.OUTPUT_LAYER))
        return {self.OUTPUT_LAYER: final_path, self.AREA_KM2: area}
