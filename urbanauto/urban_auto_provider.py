# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider
from .urban_auto_processing import UrbanAutoProcessingAlgorithm
from .urban_auto_tools import TemporalConsistencyAlgorithm, AccuracyAssessmentAlgorithm
from .urban_auto_batch_processing import FolderBatchAlgorithm
from .urban_auto_rf_processing import RFSeriesAlgorithm, LabelPointsAlgorithm
from .urban_auto_wsf import WSFBenchmarkAlgorithm
from .urban_auto_wsf_download import WSFDownloadAlgorithm
from .urban_auto_i18n import translate_class

for _cls in (UrbanAutoProcessingAlgorithm, TemporalConsistencyAlgorithm, AccuracyAssessmentAlgorithm, FolderBatchAlgorithm,
             RFSeriesAlgorithm, LabelPointsAlgorithm, WSFBenchmarkAlgorithm, WSFDownloadAlgorithm):
    translate_class(_cls)


class UrbanAutoProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(UrbanAutoProcessingAlgorithm())
        self.addAlgorithm(TemporalConsistencyAlgorithm())
        self.addAlgorithm(AccuracyAssessmentAlgorithm())
        self.addAlgorithm(FolderBatchAlgorithm())
        self.addAlgorithm(RFSeriesAlgorithm())
        self.addAlgorithm(LabelPointsAlgorithm())
        self.addAlgorithm(WSFBenchmarkAlgorithm())
        self.addAlgorithm(WSFDownloadAlgorithm())

    def id(self):
        return 'urbanauto'

    def name(self):
        return 'Urban Auto'

    def longName(self):
        return 'Urban Auto — анализ городской застройки'

    def icon(self):
        path = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(path) if os.path.exists(path) else QgsProcessingProvider.icon(self)
