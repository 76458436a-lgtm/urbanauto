"""Real Qt/QGIS tests for window sizing, configuration and background dispatch."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qgis.PyQt.QtCore import QPoint, QSettings
from qgis.PyQt.QtWidgets import QMainWindow
from qgis.core import QgsApplication, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from urbanauto import urban_auto_workspace as ui


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QgsApplication.instance() or QgsApplication([], True)
        cls.app.initQgis()
        from processing.core.Processing import Processing
        Processing.initialize()
        from urbanauto.urban_auto_provider import UrbanAutoProvider
        cls.provider = UrbanAutoProvider()
        QgsApplication.processingRegistry().addProvider(cls.provider)
        cls.root = Path(tempfile.mkdtemp(prefix='workspace_ui_'))

    def setUp(self):
        self.main = QMainWindow(); self.canvas = QgsMapCanvas()
        class Interface:
            def mainWindow(inner): return self.main
            def mapCanvas(inner): return self.canvas
        self.settings = patch.object(ui, 'QSettings', lambda: QSettings(str(self.root / (self._testMethodName+'.ini')), QSettings.IniFormat))
        self.settings.start()
        self.dialog = ui.UrbanWorkspace(Interface())
        self.dialog.city.setText('Урай')
        self.dialog.output_root.setText(str(self.root))

    def tearDown(self):
        self.dialog.close(); self.dialog.deleteLater()
        self.app.processEvents(); self.settings.stop()
        self.main.close(); self.canvas.close()
        QgsProject.instance().removeAllMapLayers()

    def test_resize_scroll_and_fixed_actions(self):
        d = self.dialog; d.show()
        for width, height in [(1000, 740), (800, 550), (560, 420)]:
            d.resize(width, height); self.app.processEvents()
            self.assertEqual((d.width(), d.height()), (width, height))
            for idx, scroll in enumerate(d.scrolls):
                d.tabs.setCurrentIndex(idx); self.app.processEvents()
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0, (width, idx))
                for button in (d.run_button, d.cancel_button, d.close_button):
                    top = button.mapTo(d, QPoint(0, 0))
                    self.assertTrue(d.rect().contains(top))
                    self.assertTrue(d.rect().contains(top+QPoint(button.width()-1, button.height()-1)))
            self.assertGreater(d.scrolls[1].verticalScrollBar().maximum(), 0)
        d.tabs.setCurrentIndex(0); d.resize(900, 720); self.app.processEvents()
        d.grab().save(str(Path.cwd() / 'test_tmp/workspace_main.png'))
        d.mode.setCurrentIndex(1); d.tabs.setCurrentIndex(1); self.app.processEvents()
        d.scrolls[1].verticalScrollBar().setValue(0)
        d.grab().save(str(Path.cwd() / 'test_tmp/workspace_settings.png'))

    def test_profiles_and_custom_settings_roundtrip(self):
        d = self.dialog
        d.profile.setCurrentIndex(1)
        p = d.collect_parameters()
        self.assertEqual(p['DEFINITION'], 1)
        self.assertEqual(p['MODEL'], 1)
        for key in ('TEMPORAL', 'WEIGHT_OBS', 'FRACTION'): self.assertFalse(p[key])
        d.mode.setCurrentIndex(1); d.controls['N_PER_CLASS'].setValue(3100)
        data = json.loads(json.dumps(d.preferences()))
        d.controls['N_PER_CLASS'].setValue(1000); d.load_preferences(data)
        self.assertEqual(d.collect_parameters()['N_PER_CLASS'], 3100)
        d.mode.setCurrentIndex(0)
        self.assertEqual(d.collect_parameters()['N_PER_CLASS'], 2000)
        self.assertFalse(d.advanced_body.isEnabled())

    def test_validation_before_execution(self):
        d = self.dialog
        d.city.clear()
        with self.assertRaisesRegex(ValueError, 'название'): d.collect_parameters()
        d.city.setText('Урай'); d.start_year.setValue(2025)
        with self.assertRaisesRegex(ValueError, 'трёх'): d.collect_parameters()
        d.start_year.setValue(2000); d.mode.setCurrentIndex(1)
        d.controls['SENTINEL_SERIES'].setCurrentIndex(1)
        with self.assertRaisesRegex(ValueError, '2018'): d.collect_parameters()
        d.controls['SENTINEL_SERIES'].setCurrentIndex(0)
        pts = QgsVectorLayer('Point?crs=EPSG:4326&field=label:integer', 'Контроль', 'memory')
        QgsProject.instance().addMapLayer(pts)
        d.training.setLayer(pts); d.validation.setLayer(pts)
        with self.assertRaisesRegex(ValueError, 'разные'): d.collect_parameters()
        d.training.setLayer(None); d.report_years.setText('2026')
        with self.assertRaisesRegex(ValueError, 'период'): d.collect_parameters()

    def test_background_profile_dispatch_and_success(self):
        from urbanauto import urban_auto_series as series
        from urbanauto.urban_auto_rf_processing import RFSeriesAlgorithm
        from qgis.PyQt.QtCore import QThread
        d = self.dialog; d.profile.setCurrentIndex(1); d.add_layers.setChecked(False)
        calls = []
        post_calls = []
        def post(algorithm, context, feedback):
            post_calls.append(QThread.currentThread() == self.app.thread())
            return {}
        def run(folder, grid, years, **options):
            calls.append(options)
            Path(folder, 'report.html').write_text('<html>test</html>')
            return {'outputs': {'protocol': str(Path(folder, 'protocol.json')), 'report': str(Path(folder, 'report.html'))}}
        with patch.object(series, 'grid_for_city', return_value=(((0, 0, 300, 300), 10, 10, 'EPSG:32641'), {})), patch.object(series, 'run_series', side_effect=run), patch.object(RFSeriesAlgorithm, 'postProcessAlgorithm', post):
            with patch.object(d, 'show_error', side_effect=AssertionError):
                d.start_run()
            deadline = time.monotonic()+20
            while d.running and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.02)
            self.assertFalse(d.running)
            self.assertEqual(len(calls), 1)
            self.assertEqual(post_calls, [True])
            self.assertEqual(calls[0]['mode'], 'territory')
            self.assertFalse(calls[0]['temporal'])
            self.assertFalse(calls[0]['weight_obs'])
            self.assertFalse(calls[0]['fraction'])
            self.assertEqual(d.progress.value(), 100)
            self.assertTrue(d.report_button.isEnabled())
            self.assertTrue(Path(d.last_output, 'interface_log.txt').is_file())

    def test_cancel_and_close_do_not_destroy_active_task(self):
        from urbanauto import urban_auto_series as series
        d = self.dialog; d.add_layers.setChecked(False)
        def run(folder, grid, years, **options):
            for _ in range(200):
                if options['cancel'](): raise RuntimeError('Отменено')
                time.sleep(.01)
            return {'outputs': {'protocol': ''}}
        with patch.object(series, 'grid_for_city', return_value=(((0, 0, 300, 300), 10, 10, 'EPSG:32641'), {})), patch.object(series, 'run_series', side_effect=run):
            d.start_run(); d.show(); d.close()
            self.assertTrue(d.running); self.assertFalse(d.isVisible())
            d.cancel_run()
            deadline = time.monotonic()+15
            while d.running and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.02)
            self.assertFalse(d.running)
            self.assertIn('остановлен', d.status.text())


if __name__ == '__main__': unittest.main()
