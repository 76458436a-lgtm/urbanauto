# SPDX-License-Identifier: GPL-2.0-or-later
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination, QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber, QgsProcessingParameterEnum, QgsProcessingOutputString, QgsProcessingException)


class FolderBatchAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return 'batch_folder'

    def displayName(self):
        return 'Автономный анализ папки индексов (все годы)'

    def group(self):
        return 'Городской анализ'

    def groupId(self):
        return 'urban_analysis'

    def createInstance(self):
        return FolderBatchAlgorithm()

    def shortHelpString(self):
        return ('Выберите папку с NDBI_ГОД.tif, BSI_ГОД.tif, NDVI_ГОД.tif, MNDWI[_max]_ГОД.tif. '
                'Точки и загрузка слоёв в проект не нужны. Растры выравниваются; пороги Оцу + K-Means '
                'рассчитываются внутри ROI. Без полигона берётся охват первого NDBI, а не граница города. '
                'Облака/тени должны быть заранее замаскированы NoData. Последний год предварительный. '
                'Новая папка run_... содержит GPKG, маски покрытия, CSV и status.json. '
                'При ошибке запуск не объявляется успешным.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile('INPUT_FOLDER', 'Папка индексов',
                                                    behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT_FOLDER', 'Папка результатов'))
        self.addParameter(QgsProcessingParameterString('YEARS', 'Годы через запятую (пусто = все)', optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer('ROI', 'Полигон ROI (необязательно)', optional=True))
        self.addParameter(QgsProcessingParameterNumber('MAX_PIXELS', 'Лимит пикселей в ROI',
                                                       defaultValue=16000000, minValue=100))
        self.addParameter(QgsProcessingParameterNumber('MIN_CONFIRM', 'Число будущих подтверждений',
                                                       defaultValue=2, minValue=1, maxValue=10))
        self.addParameter(QgsProcessingParameterEnum('LAST_MODE', 'Последний срез',
                                                     ['подтверждать предыдущим (нижняя оценка)', 'как есть (верхняя оценка)'],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber('MIN_AREA_HA', 'Минимальная площадь пятна, га',
                                                       QgsProcessingParameterNumber.Double, defaultValue=5.0, minValue=0))
        self.addParameter(QgsProcessingParameterBoolean('TEMPORAL', 'Согласовать временной ряд', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean('KEEP_GOING', 'Продолжать другие годы после ошибки', defaultValue=False))
        self.addParameter(QgsProcessingParameterFile('RESUME', 'Папка run_... для возобновления (необязательно)',
                                                    behavior=QgsProcessingParameterFile.Folder, optional=True))
        self.addOutput(QgsProcessingOutputString('RUN_FOLDER', 'Папка запуска'))
        self.addOutput(QgsProcessingOutputString('STATUS', 'Статус'))

    def processAlgorithm(self, parameters, context, feedback):
        import os
        from .urban_auto_temporal import vector_source
        roi = self.parameterAsVectorLayer(parameters, 'ROI', context)
        self._roi_identity = None
        if roi is not None:
            import hashlib
            digest = hashlib.sha256(roi.crs().toWkt().encode('utf-8'))
            for geometry in sorted(bytes(f.geometry().asWkb()) for f in roi.getFeatures()):
                digest.update(geometry)
            self._roi_identity = 'geometry_sha256:' + digest.hexdigest()
        path, name, tmp = vector_source(roi) if roi else (None, None, None)
        try:
            if name:
                # Export one selected layer into a standalone file for batch ROI hashing.
                import tempfile
                from osgeo import gdal
                with tempfile.TemporaryDirectory(prefix='urban_batch_roi_') as d:
                    single = os.path.join(d, 'roi.gpkg')
                    ds = gdal.VectorTranslate(single, path, format='GPKG', layers=[name])
                    if ds is None:
                        raise ValueError('Не удалось экспортировать ROI.')
                    ds = None  # noqa: F841 — закрыть и записать файл
                    return self._run(parameters, context, feedback, single)
            return self._run(parameters, context, feedback, path)
        except Exception as ex:
            raise QgsProcessingException(str(ex))
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _run(self, parameters, context, feedback, roi_path):
        from .urban_auto_batch import run_folder
        text = self.parameterAsString(parameters, 'YEARS', context).strip()
        years = [int(y.strip()) for y in text.replace(';', ',').split(',') if y.strip()] if text else None
        if years is not None and (not years or len(set(years)) != len(years)):
            raise ValueError('Список лет пуст или содержит повторы.')
        result = run_folder(
            self.parameterAsFile(parameters, 'INPUT_FOLDER', context),
            self.parameterAsString(parameters, 'OUTPUT_FOLDER', context), years=years,
            roi_path=roi_path, max_pixels=self.parameterAsInt(parameters, 'MAX_PIXELS', context),
            min_confirm=self.parameterAsInt(parameters, 'MIN_CONFIRM', context),
            last_mode=('backward', 'accept')[self.parameterAsEnum(parameters, 'LAST_MODE', context)],
            min_area_ha=self.parameterAsDouble(parameters, 'MIN_AREA_HA', context),
            temporal=self.parameterAsBool(parameters, 'TEMPORAL', context),
            keep_going=self.parameterAsBool(parameters, 'KEEP_GOING', context),
            resume=self.parameterAsFile(parameters, 'RESUME', context) or None,
            context=context, feedback=feedback, log=feedback.pushInfo, roi_identity=self._roi_identity)
        if result['status'] != 'complete':
            feedback.reportError('Частичный результат. Подробности: ' + result['run_dir'] + '/status.json', False)
        return {'RUN_FOLDER':result['run_dir'], 'STATUS':result['status']}
