# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
import os
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication
from .urban_auto_dialog import UrbanAutoDialog
from .urban_auto_workspace import UrbanWorkspace
from .urban_auto_provider import UrbanAutoProvider
from .urban_auto_i18n import tr

class UrbanAutoPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.legacy_dialog = None
        self.provider = None

    def initProcessing(self):
        if self.provider is not None:
            return
        provider = UrbanAutoProvider()
        if not QgsApplication.processingRegistry().addProvider(provider):
            raise RuntimeError('Urban Auto: provider registration failed')
        self.provider = provider

    def initGui(self):
        self.initProcessing()
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, tr("Urban Auto — открыть модуль"), self.iface.mainWindow())
        self.action.triggered.connect(self.run_dialog)
        self.iface.addPluginToMenu("&Urban Auto", self.action)
        self.iface.addToolBarIcon(self.action)
        self.batch_action = QAction(tr('Обработать папку индексов'), self.iface.mainWindow())
        self.batch_action.triggered.connect(self.run_batch)
        self.iface.addPluginToMenu('&Urban Auto', self.batch_action)
        self.rf_action = QAction(icon, tr('Годовой ряд — форма Processing'), self.iface.mainWindow())
        self.rf_action.triggered.connect(self.run_rf)
        self.iface.addPluginToMenu('&Urban Auto', self.rf_action)
        self.wsf_action = QAction(tr('Сравнить с WSF'), self.iface.mainWindow())
        self.wsf_action.triggered.connect(self.run_wsf)
        self.iface.addPluginToMenu('&Urban Auto', self.wsf_action)
        self.wsf_download_action = QAction(tr('Загрузить WSF Tracker'), self.iface.mainWindow())
        self.wsf_download_action.triggered.connect(self.run_wsf_download)
        self.iface.addPluginToMenu('&Urban Auto', self.wsf_download_action)
        self._startup_checks()

    def _startup_checks(self):
        from qgis.core import Qgis
        from .urban_auto_deps import missing, install_hint, active_legacy
        bar = self.iface.messageBar()
        m = missing()
        if m:
            bar.pushMessage('Urban Auto', tr('Не хватает библиотек Python: ') + ', '.join(m)
                            + tr('. Годовой ряд не запустится. ') + install_hint(m).replace('\n', ' '), Qgis.Warning, 0)
        old = active_legacy()
        if old:
            bar.pushMessage('Urban Auto', tr('Включены старые копии модуля: ') + ', '.join(old)
                            + tr('. Отключите их в «Модули → Управление и установка», чтобы не путать меню и результаты.'),
                            Qgis.Info, 15)

    def unload(self):
        if hasattr(self, 'wsf_download_action'):
            self.iface.removePluginMenu('&Urban Auto', self.wsf_download_action)
        if hasattr(self, 'wsf_action'):
            self.iface.removePluginMenu('&Urban Auto', self.wsf_action)
        if hasattr(self, 'rf_action'):
            self.iface.removePluginMenu('&Urban Auto', self.rf_action)
        if hasattr(self, 'batch_action'):
            self.iface.removePluginMenu('&Urban Auto', self.batch_action)
        if hasattr(self, 'action'):
            self.iface.removePluginMenu("&Urban Auto", self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        if self.dialog is not None:
            if self.dialog.running:
                self.dialog.cancel_run()
                self.dialog.task.executed.connect(self.dialog.deleteLater)
                self.dialog.hide()
            else:
                self.dialog.close()
                self.dialog.deleteLater()
            self.dialog = None
        if self.legacy_dialog is not None:
            self.legacy_dialog.close(); self.legacy_dialog.deleteLater(); self.legacy_dialog = None

    def run_dialog(self):
        if self.dialog is None:
            self.dialog = UrbanWorkspace(self.iface, self.run_legacy)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def run_legacy(self):
        if self.legacy_dialog is None:
            self.legacy_dialog = UrbanAutoDialog(self.iface)
            self.legacy_dialog.setWindowTitle(tr('Urban Auto — спектральные индексы и ручные пороги'))
        self.legacy_dialog.show(); self.legacy_dialog.raise_()

    def run_batch(self):
        import processing
        processing.execAlgorithmDialog('urbanauto:batch_folder')

    def run_rf(self):
        import processing
        processing.execAlgorithmDialog('urbanauto:rf_series')

    def run_wsf(self):
        import processing
        processing.execAlgorithmDialog('urbanauto:wsf_benchmark')

    def run_wsf_download(self):
        import processing
        processing.execAlgorithmDialog('urbanauto:wsf_download')
