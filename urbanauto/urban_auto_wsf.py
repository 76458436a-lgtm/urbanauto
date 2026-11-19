# SPDX-License-Identifier: GPL-2.0-or-later
"""Urban Auto: independent local WSF benchmark (no model retraining)."""
import csv
import json
import os
from qgis.core import (QgsProcessingAlgorithm, QgsProcessing, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterField,
    QgsProcessingParameterFolderDestination, QgsProcessingException,
    QgsCoordinateTransform, QgsProject, QgsPointXY, QgsWkbTypes)


def metrics(pairs):
    tp = sum(a == 1 and b == 1 for a, b in pairs)
    tn = sum(a == 0 and b == 0 for a, b in pairs)
    fp = sum(a == 1 and b == 0 for a, b in pairs)
    fn = sum(a == 0 and b == 1 for a, b in pairs)
    n = tp + tn + fp + fn
    if not n:
        return None
    oa = (tp + tn) / n
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n*n)
    return {'n': n, 'TP': tp, 'TN': tn, 'FP': fp, 'FN': fn,
            'OA': oa, 'kappa': (oa-pe)/(1-pe) if pe < 1 else 0,
            'F1_built': 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0,
            'precision_built': tp/(tp+fp) if tp+fp else 0,
            'recall_built': tp/(tp+fn) if tp+fn else 0}


class WSFBenchmarkAlgorithm(QgsProcessingAlgorithm):
    BASE = 'BASE'; WSF = 'WSF'; POINTS = 'POINTS'; LABEL = 'LABEL'; OUTPUT = 'OUTPUT'

    def name(self): return 'wsf_benchmark'
    def displayName(self): return 'Urban Auto — сравнение с WSF по точкам'
    def group(self): return 'Городской анализ'
    def groupId(self): return 'urban_analysis'
    def shortHelpString(self):
        return ('Сравнивает бинарную карту Urban Auto с локальным растром WSF по независимым точкам. '
                'Оба растра должны кодировать застройку как 1, фон как 0. '
                'Никакой автоматической интерпретации классов WSF нет: предварительно подготовьте бинарную маску '
                'по выбранному определению застройки. Точки: label=1 (застройка), 0 (фон). '
                'Создаёт CSV по точкам и JSON с метриками, без изменения основной модели. '
                'Точки должны быть независимыми от обучения и настройки порогов. '
                'Метрики по стратифицированной выборке НЕ являются оценкой площади по Olofsson.')
    def createInstance(self): return WSFBenchmarkAlgorithm()
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.BASE, 'Urban Auto: бинарная карта 0/1'))
        self.addParameter(QgsProcessingParameterRasterLayer(self.WSF, 'WSF: подготовленная бинарная карта 0/1'))
        self.addParameter(QgsProcessingParameterVectorLayer(self.POINTS, 'Независимые контрольные точки', types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(self.LABEL, 'Поле эталона (0/1)', parentLayerParameterName=self.POINTS))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, 'Папка отчёта'))
    def processAlgorithm(self, parameters, context, feedback):
        base = self.parameterAsRasterLayer(parameters, self.BASE, context)
        wsf = self.parameterAsRasterLayer(parameters, self.WSF, context)
        pts = self.parameterAsVectorLayer(parameters, self.POINTS, context)
        field = self.parameterAsString(parameters, self.LABEL, context)
        out = self.parameterAsString(parameters, self.OUTPUT, context)
        if not all([base, wsf, pts, field, out]):
            raise QgsProcessingException('Не заполнены обязательные параметры')
        if QgsWkbTypes.geometryType(pts.wkbType()) != QgsWkbTypes.PointGeometry:
            raise QgsProcessingException('Нужен точечный слой')
        os.makedirs(out, exist_ok=True)
        transforms = [QgsCoordinateTransform(pts.crs(), layer.crs(), context.transformContext()) for layer in (base, wsf)]
        records = []; skipped = {'invalid_label': 0, 'outside_or_nodata': 0, 'nonbinary_raster': 0}
        for i, feature in enumerate(pts.getFeatures()):
            if feedback.isCanceled(): break
            try:
                raw = float(feature[field])
                if raw not in (0, 1): raise ValueError('label')
                geom = feature.geometry()
                if geom.isEmpty(): raise ValueError('geometry')
                p = geom.asPoint() if not geom.isMultipart() else geom.asMultiPoint()[0]
                values = []
                for layer, tr in zip((base, wsf), transforms):
                    point = tr.transform(QgsPointXY(p))
                    value, ok = layer.dataProvider().sample(point, 1)
                    if not ok: raise LookupError('nodata')
                    value = float(value)
                    if value not in (0, 1): raise ArithmeticError('nonbinary')
                    values.append(int(value))
                records.append((str(feature.id()), int(raw), values[0], values[1]))
            except ArithmeticError:
                skipped['nonbinary_raster'] += 1
            except LookupError:
                skipped['outside_or_nodata'] += 1
            except (ValueError, TypeError, IndexError):
                skipped['invalid_label'] += 1
            if i % 100 == 0: feedback.setProgress(min(99, i * 100 / max(pts.featureCount(), 1)))
        if not records:
            raise QgsProcessingException('Нет пригодных точек: проверьте 0/1, CRS, NoData и границы растров')
        pairs_base = [(r[2], r[1]) for r in records]
        pairs_wsf = [(r[3], r[1]) for r in records]
        improved = sum(r[2] != r[1] and r[3] == r[1] for r in records)
        worsened = sum(r[2] == r[1] and r[3] != r[1] for r in records)
        result = {'urban_auto': metrics(pairs_base), 'wsf': metrics(pairs_wsf),
                  'n_common': len(records), 'wsf_corrected': improved, 'wsf_worsened': worsened,
                  'disagreement_points': sum(r[2] != r[3] for r in records),
                  'skipped': skipped, 'note': 'Unweighted point metrics; not area-adjusted Olofsson estimates. Compare only matching built-up definitions.'}
        csv_path = os.path.join(out, 'comparison_points.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f); writer.writerow(['feature_id', 'reference', 'urban_auto', 'wsf', 'urban_auto_correct', 'wsf_correct'])
            for fid, ref, a, b in records: writer.writerow([fid, ref, a, b, int(a == ref), int(b == ref)])
        json_path = os.path.join(out, 'benchmark_metrics.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        feedback.pushInfo('Urban Auto κ: %.3f; WSF κ: %.3f' % (result['urban_auto']['kappa'], result['wsf']['kappa']))
        feedback.setProgress(100)
        return {'CSV': csv_path, 'JSON': json_path, 'POINT_COUNT': len(records)}
