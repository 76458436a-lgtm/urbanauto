# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
import os
import re
import csv
import datetime
import traceback
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import (QDialog, QListWidgetItem, QFileDialog, QHBoxLayout,
                                 QButtonGroup, QRadioButton, QPushButton, QInputDialog, QLabel, QLineEdit, QCheckBox, QDoubleSpinBox,
                                 QComboBox, QSpinBox, QScrollArea, QWidget, QVBoxLayout,
                                 QLayout, QApplication)
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.gui import QgsMapToolEmitPoint, QgsMapToolPan, QgsMapToolCapture, QgsMapLayerComboBox
from qgis.core import (QgsPointXY, QgsProject, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsGeometry, QgsFeature, QgsVectorLayer,
                       QgsMapLayerProxyModel, QgsMessageLog, Qgis)
from .urban_auto_core import run_urban_analysis_core, UrbanConfig, PLUGIN_VERSION
from .urban_auto_thresholds import sample_points, BOX_METHODS

FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'urban_auto_dialog.ui'))

class PolygonCaptureTool(QgsMapToolCapture):
    def __init__(self, canvas, cadDockWidget, dialog):
        super().__init__(canvas, cadDockWidget, QgsMapToolCapture.CapturePolygon)
        self.dialog = dialog

    def activate(self):
        super().activate()
        self.startCapturing()

    def cadCanvasReleaseEvent(self, e):
        if e.button() == Qt.RightButton:
            captured_points = [QgsPointXY(p) for p in self.points()]
            super().cadCanvasReleaseEvent(e)
            self.stopCapturing()
            self.dialog.handle_captured_polygon(captured_points)
            return
        super().cadCanvasReleaseEvent(e)


class UrbanAutoDialog(QDialog, FORM_CLASS):
    def __init__(self, iface, parent=None):
        super(UrbanAutoDialog, self).__init__(parent)
        self.setupUi(self)
        self.iface = iface
        self.canvas = self.iface.mapCanvas()
        self.urban_points = []
        self.nonurban_points = []
        self.polygon_layer = None
        self.polygon_tool = None
        self.current_tool = 'pan'

        self.pan_tool = QgsMapToolPan(self.canvas)
        self.point_tool = QgsMapToolEmitPoint(self.canvas)
        self.point_tool.canvasClicked.connect(self.on_point_clicked)

        self.setup_extra_controls()
        self.setup_layer_combos()

        self.btn_add_urban.clicked.connect(lambda: self.set_mode('urban_point'))
        self.btn_add_nonurban.clicked.connect(lambda: self.set_mode('nonurban_point'))
        self.btn_remove_last_urban.clicked.connect(lambda: self.remove_point(self.urban_points, self.list_urban, self.label_urban_count))
        self.btn_remove_last_nonurban.clicked.connect(lambda: self.remove_point(self.nonurban_points, self.list_nonurban, self.label_nonurban_count))
        self.btn_clear_urban.clicked.connect(lambda: self.clear_points(self.urban_points, self.list_urban, self.label_urban_count))
        self.btn_clear_nonurban.clicked.connect(lambda: self.clear_points(self.nonurban_points, self.list_nonurban, self.label_nonurban_count))

        self.btn_run.clicked.connect(self.run_analysis)
        self.btn_close.clicked.connect(self.close)
        self.btn_browse_output.clicked.connect(self.browse_output_folder)
        self.cb_roi_type.currentIndexChanged.connect(self.on_roi_type_changed)
        self.btn_draw_polygon.clicked.connect(self.start_polygon_drawing)

        self.spin_year.valueChanged.connect(self.auto_select_layers_for_year)

        self.le_output_folder.setText(os.path.expanduser("~"))
        self.refresh_lists()
        self.on_roi_type_changed(self.cb_roi_type.currentIndex())
        self.set_mode('pan')
        self.auto_select_layers_for_year(self.spin_year.value())
        self.setup_scroll_area()

    def setup_scroll_area(self):
        """Keep actions visible while the parameter form scrolls on small screens."""
        outer = self.layout()
        content = QWidget(self)
        form = QVBoxLayout(content)
        form.setSizeConstraint(QLayout.SetMinimumSize)
        footer = []
        def reparent_item(item):
            if item.widget() is not None:
                item.widget().setParent(content)
            elif item.layout() is not None:
                layout = item.layout()
                for index in range(layout.count()):
                    reparent_item(layout.itemAt(index))

        while outer.count():
            item = outer.takeAt(0)
            if item.widget() is self.progressBar or item.layout() is self.bottomButtons:
                footer.append(item)
            else:
                reparent_item(item)
                form.addItem(item)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName('urbanauto_parameter_scroll')
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)
        for item in footer:
            outer.addItem(item)
        self.setMinimumSize(480, 320)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(1000, available.width() - 40),
                        min(800, available.height() - 80))

    def setup_extra_controls(self):
        main_layout = self.layout()
        if not main_layout:
            return
        tool_layout = QHBoxLayout()
        self.mode_group = QButtonGroup(self)

        modes = [("Панорамирование", 'pan'),
                 ("Точки города", 'urban_point'),
                 ("Точки фона", 'nonurban_point'),
                 ("Выбрать центр", 'center_point'),
                 ("Рисовать полигон", 'polygon')]

        for i, (text, mode) in enumerate(modes):
            rb = QRadioButton(text)
            self.mode_group.addButton(rb, i)
            if mode == 'polygon':
                rb.toggled.connect(lambda checked, m=mode: self.start_polygon_drawing() if checked else None)
            else:
                rb.toggled.connect(lambda checked, m=mode: self.set_mode(m) if checked else None)
            tool_layout.addWidget(rb)

        self.mode_group.buttons()[0].setChecked(True)
        self.btn_manual_poly = QPushButton("Координаты вручную")
        self.btn_manual_poly.clicked.connect(self.enter_polygon_manually)
        tool_layout.addWidget(self.btn_manual_poly)
        tool_layout.addStretch()
        main_layout.insertLayout(1, tool_layout)

    def setup_layer_combos(self):
        layout = self.tabBasic.layout()
        row = layout.rowCount()
        self.combo_ndbi = QgsMapLayerComboBox()
        self.combo_bsi = QgsMapLayerComboBox()
        self.combo_ndvi = QgsMapLayerComboBox()
        self.combo_mndwi = QgsMapLayerComboBox()

        for combo, label in zip([self.combo_ndbi, self.combo_bsi, self.combo_ndvi, self.combo_mndwi],
                                ["Слой NDBI:", "Слой BSI:", "Слой NDVI:", "Слой MNDWI (Max):"]):
            combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
            layout.addWidget(QLabel(label), row, 0)
            layout.addWidget(combo, row, 1)
            row += 1

        layout.addWidget(QLabel("Несколько лет (через запятую):"), row, 0)
        self.le_multi_years = QLineEdit()
        self.le_multi_years.setPlaceholderText("Например: 2000, 2005, 2010")
        layout.addWidget(self.le_multi_years, row, 1)
        row += 1

        self.cb_otsu = QCheckBox("Метод Оцу (автоматические пороги)")
        self.cb_otsu.setChecked(False)
        self.cb_otsu.setToolTip("Пороги NDBI, BSI, NDVI по методу Оцу. Полностью автоматический режим, точность ниже, чем по опорным точкам.")
        self.cb_otsu.setStyleSheet("font-weight: bold; color: #d35400; padding-top: 10px;")
        layout.addWidget(self.cb_otsu, row, 0, 1, 3)
        row += 1

        self.cb_balance = QCheckBox("Спектральный баланс NDBI−NDVI")
        self.cb_balance.setChecked(True)
        self.cb_balance.setStyleSheet("font-weight: bold; color: #2980b9;")
        layout.addWidget(self.cb_balance, row, 0, 1, 3)
        row += 1

        self.cb_balance_ai = QCheckBox("Авто-порог (K-Means)")
        self.cb_balance_ai.setChecked(True)
        self.cb_balance_ai.setToolTip("Порог NDBI−NDVI находится кластеризацией K-Means (k=3). Значение справа — смещение порога.")
        self.cb_balance_ai.setStyleSheet("color: #8e44ad; font-weight: bold;")
        
        self.dsb_balance = QDoubleSpinBox()
        self.dsb_balance.setDecimals(3)
        self.dsb_balance.setMinimum(-1.0)
        self.dsb_balance.setMaximum(1.0)
        self.dsb_balance.setValue(0.000) 
        self.dsb_balance.setSingleStep(0.05)
        self.dsb_balance.setToolTip("Если K-Means включён — смещение (−0.05 добавит застройки). Если выключен — точный порог NDBI−NDVI (для тайги ≈ −0.8).")

        layout.addWidget(self.cb_balance_ai, row, 0)
        layout.addWidget(QLabel("Смещение / Порог >"), row, 1, Qt.AlignRight)
        layout.addWidget(self.dsb_balance, row, 2)
        row += 1

        self.cb_cumulative = QCheckBox("Накопительный рост")
        self.cb_cumulative.setToolTip("Новые пятна сохраняются только если они находятся в радиусе привязки от старого города.")
        self.cb_cumulative.setChecked(False)
        self.cb_cumulative.setStyleSheet("font-weight: bold; color: #27ae60; padding-top: 10px;")
        
        self.dsb_merge_radius = QDoubleSpinBox()
        self.dsb_merge_radius.setDecimals(0)
        self.dsb_merge_radius.setMinimum(0)
        self.dsb_merge_radius.setMaximum(5000)
        self.dsb_merge_radius.setValue(300) 
        self.dsb_merge_radius.setSingleStep(50)
        self.dsb_merge_radius.setEnabled(False)
        self.dsb_merge_radius.setToolTip("Максимальное расстояние (в метрах) от старого города, на котором признается новая застройка.")
        
        self.cb_cumulative.toggled.connect(lambda checked: self.dsb_merge_radius.setEnabled(checked))

        layout.addWidget(self.cb_cumulative, row, 0)
        layout.addWidget(QLabel("Радиус привязки (м) >"), row, 1, Qt.AlignRight)
        layout.addWidget(self.dsb_merge_radius, row, 2)
        row += 1

        def spin(val, lo, hi, step, dec=0, tip=""):
            w = QDoubleSpinBox()
            w.setDecimals(dec)
            w.setRange(lo, hi)
            w.setSingleStep(step)
            w.setValue(val)
            w.setToolTip(tip)
            return w

        layout.addWidget(QLabel("Морфология: буфер наружу / внутрь (м):"), row, 0)
        self.dsb_close = spin(200, 0, 2000, 10, tip="Буфер наружу — замыкает разрывы между кварталами")
        self.dsb_shrink = spin(200, 0, 2000, 10, tip="Буфер внутрь. Равен буферу наружу → симметричное замыкание "
                                                      "(в версиях до 4.1 было 180 м, что добавляло ~20 м по контуру)")
        box = QHBoxLayout()
        box.addWidget(self.dsb_close)
        box.addWidget(self.dsb_shrink)
        layout.addLayout(box, row, 1, 1, 2)
        row += 1
        layout.addWidget(QLabel("Мин. площадь пятна (га):"), row, 0)
        self.dsb_min_area = spin(5, 0, 1000, 1, 1, "Пятна меньше этой площади отбрасываются (5 га = 50 000 м²)")
        layout.addWidget(self.dsb_min_area, row, 1)
        self.cb_clip = QCheckBox("Обрезать по ROI")
        self.cb_clip.setChecked(True)
        self.cb_clip.setEnabled(False)
        self.cb_clip.setToolTip("В 4.3 обрезка по ROI обязательна: расчёт порогов, валидная маска и площадь относятся к одной территории.")
        layout.addWidget(self.cb_clip, row, 2)
        row += 1

        # --- v4.2: устойчивые пороги по точкам ---
        layout.addWidget(QLabel("Пороги по точкам:"), row, 0)
        self.cb_box_method = QComboBox()
        for key, title in BOX_METHODS:
            self.cb_box_method.addItem(title, key)
        self.cb_box_method.setToolTip("Устойчивые: границы по процентилям значений в точках, нетипичные точки отбрасываются "
                                      "(правило 1,5 IQR). Мин/макс — как в версиях до 4.2: одна неудачная точка "
                                      "(например, во дворе с деревьями) расширяет класс до леса.")
        layout.addWidget(self.cb_box_method, row, 1, 1, 2)
        row += 1

        # --- v4.2: временная согласованность ---
        self.cb_temporal = QCheckBox("Временная согласованность ряда")
        self.cb_temporal.setChecked(True)
        self.cb_temporal.setStyleSheet("font-weight: bold; color: #27ae60;")
        self.cb_temporal.setToolTip("Для нескольких лет: новая застройка года принимается, только если она подтверждается "
                                    "в k последующих срезах; застройка не исчезает. Рекомендуется вместо накопительного роста.")
        self.sb_confirm = QSpinBox()
        self.sb_confirm.setRange(1, 5)
        self.sb_confirm.setValue(2)
        self.sb_confirm.setToolTip("k — сколько последующих срезов должны подтвердить пятно. 2 (по умолчанию) — надёжно при обычной "
                                   "расстановке точек; 1 — только для тщательно выверенных точек каждого года.")
        self.cb_last_confirm = QCheckBox("последний год — подтверждать предыдущим")
        self.cb_last_confirm.setChecked(True)
        self.cb_last_confirm.setToolTip("Последний срез нечем подтвердить «вперёд». Включено (рекомендуется): новое пятно последнего года "
                                        "принимается, если оно видно и в предыдущем срезе — нижняя оценка. Выключено: последний срез "
                                        "как есть — верхняя оценка. В отчёт пишутся обе.")
        layout.addWidget(self.cb_temporal, row, 0)
        hb = QHBoxLayout()
        hb.addWidget(QLabel("k ="))
        hb.addWidget(self.sb_confirm)
        hb.addWidget(self.cb_last_confirm)
        layout.addLayout(hb, row, 1, 1, 2)

        def _exclusive(src, other):
            if src.isChecked():
                other.setChecked(False)
        self.cb_temporal.toggled.connect(lambda c: _exclusive(self.cb_temporal, self.cb_cumulative))
        self.cb_cumulative.toggled.connect(lambda c: _exclusive(self.cb_cumulative, self.cb_temporal))

    def auto_select_layers_for_year(self, year):
        project = QgsProject.instance()
        ndbi_candidates = project.mapLayersByName(f"NDBI_{year}")
        bsi_candidates = project.mapLayersByName(f"BSI_{year}")
        ndvi_candidates = project.mapLayersByName(f"NDVI_{year}")
        mndwi_candidates = project.mapLayersByName(f"MNDWI_max_{year}")

        if ndbi_candidates:
            self.combo_ndbi.setLayer(ndbi_candidates[0])
        if bsi_candidates:
            self.combo_bsi.setLayer(bsi_candidates[0])
        if ndvi_candidates:
            self.combo_ndvi.setLayer(ndvi_candidates[0])
        if mndwi_candidates:
            self.combo_mndwi.setLayer(mndwi_candidates[0])

    def set_mode(self, mode):
        self.current_tool = mode
        if mode == 'pan':
            self.canvas.setMapTool(self.pan_tool)
        elif mode != 'polygon':
            self.canvas.setMapTool(self.point_tool)
        self.status_label.setText(f"Текущий режим: {mode}")

    def on_point_clicked(self, point, button):
        crs_canvas = self.canvas.mapSettings().destinationCrs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs_canvas != crs_wgs84:
            point = QgsCoordinateTransform(crs_canvas, crs_wgs84, QgsProject.instance()).transform(point)

        lon, lat = point.x(), point.y()

        if self.current_tool in ('urban_point', 'nonurban_point'):
            hint, warn = self.point_hint(lon, lat, self.current_tool == 'urban_point')
            item = QListWidgetItem(f"{lon:.5f}, {lat:.5f}{hint}")
            if warn:
                item.setForeground(Qt.red)
                item.setToolTip(warn)
                self.status_label.setText(warn)
        if self.current_tool == 'urban_point':
            self.urban_points.append((lon, lat))
            self.list_urban.addItem(item)
            self.label_urban_count.setText(f"Количество: {len(self.urban_points)}")
        elif self.current_tool == 'nonurban_point':
            self.nonurban_points.append((lon, lat))
            self.list_nonurban.addItem(item)
            self.label_nonurban_count.setText(f"Количество: {len(self.nonurban_points)}")
        elif self.current_tool == 'center_point':
            self.dsb_center_lon.setValue(lon)
            self.dsb_center_lat.setValue(lat)
            self.mode_group.buttons()[0].setChecked(True)

    def point_hint(self, lon, lat, is_urban):
        """Значения индексов в точке и предупреждение о нетипичной точке (v4.2)."""
        lyrs = [self.combo_ndbi.currentLayer(), self.combo_bsi.currentLayer(),
                self.combo_ndvi.currentLayer(), self.combo_mndwi.currentLayer()]
        if not all(lyrs):
            return '', ''
        try:
            vals = sample_points([(lon, lat)], lyrs)
        except Exception:
            return '', ''
        if not vals:
            return '  (вне растра)', 'Точка вне охвата растров — она не будет учтена.'
        n, b, v, w = vals[0]
        hint = f"  NDBI {n:+.2f} · BSI {b:+.2f} · NDVI {v:.2f}"
        warn = ''
        if is_urban:
            if w > 0:
                warn = 'Точка города на воде (MNDWI > 0) — перенесите её на здание.'
            elif v > 0.6:
                warn = (f'Точка города слишком «зелёная» (NDVI {v:.2f} > 0,6): двор с деревьями расширит класс до леса. '
                        'Ставьте точки на крыши и асфальт внутри кварталов.')
            elif n - v < -1.0:
                warn = f'Точка похожа на лес (NDBI−NDVI = {n - v:.2f}). Проверьте её на снимке.'
        else:
            if n - v > -0.6:
                warn = (f'Точка фона спектрально похожа на застройку (NDBI−NDVI = {n - v:.2f}). Это допустимо для песков и '
                        'вырубок, но убедитесь, что это не здание.')
        return hint, warn

    def start_polygon_drawing(self):
        self.status_label.setText("Рисуйте полигон: левый клик – вершина, правый клик – завершить")
        if self.polygon_layer:
            QgsProject.instance().removeMapLayer(self.polygon_layer.id())
            self.polygon_layer = None

        self.polygon_tool = PolygonCaptureTool(self.canvas, self.iface.cadDockWidget(), self)
        self.canvas.setMapTool(self.polygon_tool)

    def handle_captured_polygon(self, points):
        if len(points) < 3:
            self.status_label.setText("Ошибка: нужно минимум 3 точки.")
            return

        if points[0] != points[-1]:
            points.append(QgsPointXY(points[0]))
        geom = QgsGeometry.fromPolygonXY([points])

        if geom.isGeosValid():
            # Точки захвачены в СК карты, а не активного слоя
            pts_crs = self.canvas.mapSettings().destinationCrs()

            self.polygon_layer = QgsVectorLayer(f"Polygon?crs={pts_crs.authid()}", "ROI_Polygon_User", "memory")
            self.polygon_layer.startEditing()
            f = QgsFeature()
            f.setGeometry(geom)
            self.polygon_layer.addFeature(f)
            self.polygon_layer.commitChanges()

            QgsProject.instance().addMapLayer(self.polygon_layer)
            self.cb_roi_type.setCurrentIndex(2)
            self.status_label.setText("Полигон успешно создан.")
        else:
            self.status_label.setText("Ошибка: недействительная geometry.")

    def enter_polygon_manually(self):
        text, ok = QInputDialog.getMultiLineText(self, "Координаты", "По одной вершине в строке: широта долгота (WGS84), напр. 60.13 64.78")
        if ok and text:
            pts = []
            for line in text.split('\n'):
                parts = [p for p in line.strip().replace(',', ' ').split() if p]
                if len(parts) >= 2:
                    try:
                        lat, lon = float(parts[0]), float(parts[1])
                        pts.append(QgsPointXY(lon, lat))  # QgsPointXY(x=долгота, y=широта)
                    except ValueError:
                        pass

            if len(pts) > 2:
                if pts[0] != pts[-1]:
                    pts.append(QgsPointXY(pts[0]))
                geom = QgsGeometry.fromPolygonXY([pts])
                if geom.isGeosValid():
                    if self.polygon_layer:
                        QgsProject.instance().removeMapLayer(self.polygon_layer.id())
                    self.polygon_layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "ROI_Polygon_Manual", "memory")
                    self.polygon_layer.startEditing()
                    f = QgsFeature()
                    f.setGeometry(geom)
                    self.polygon_layer.addFeature(f)
                    self.polygon_layer.commitChanges()
                    QgsProject.instance().addMapLayer(self.polygon_layer)
                    self.cb_roi_type.setCurrentIndex(2)

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбор", self.le_output_folder.text())
        if folder:
            self.le_output_folder.setText(folder)

    def remove_point(self, pts_list, list_widget, count_label):
        if pts_list:
            pts_list.pop()
            list_widget.takeItem(list_widget.count() - 1)
            count_label.setText(f"Количество: {len(pts_list)}")

    def clear_points(self, pts_list, list_widget, count_label):
        pts_list.clear()
        list_widget.clear()
        count_label.setText("Количество: 0")

    def refresh_lists(self):
        self.clear_points(self.urban_points, self.list_urban, self.label_urban_count)
        self.clear_points(self.nonurban_points, self.list_nonurban, self.label_nonurban_count)

    def get_box_params(self, is_manual, min_n, max_n, min_b, max_b, max_v):
        if is_manual:
            return (min_n.value(), max_n.value(), min_b.value(), max_b.value(), max_v.value())
        return None

    def manual_boxes(self):
        boxes = []
        for i in (1, 2, 3):
            if getattr(self, f'cb_manual_box{i}').isChecked():
                boxes.append(tuple(getattr(self, f'dsb_box{i}_{k}').value()
                                   for k in ('ndbi_min', 'ndbi_max', 'bsi_min', 'bsi_max', 'ndvi_max')))
        return boxes

    def calculate_formula_for_layers(self, ndbi_lyr, bsi_lyr, ndvi_lyr, mndwi_lyr, log_callback=None):
        return dict(
            use_otsu=self.cb_otsu.isChecked(),
            manual_boxes=self.manual_boxes(),
            urban_points=self.urban_points,
            nonurban_points=self.nonurban_points,
            use_balance=self.cb_balance.isChecked(),
            balance_ai=self.cb_balance_ai.isChecked(),
            balance_value=self.dsb_balance.value(),
            box_method=self.cb_box_method.currentData(),
            )

    def generate_growth_report(self, report_data, folder):
        """report_data: список dict(year, area_km2, perimeter_km, patches, compactness, mode, formula)."""
        if not report_data:
            return None
        report_data.sort(key=lambda r: r['year'])
        base = os.path.join(folder, "urban_growth_report")
        txt_path = os.path.normpath(base + ".txt").replace('\\', '/')
        csv_path = os.path.normpath(base + ".csv").replace('\\', '/')

        rows = []
        prev = None
        for r in report_data:
            row = dict(r)
            if prev is None:
                row.update(abs_growth_km2=None, growth_pct=None, annual_rate_pct=None)
            else:
                dy = r['year'] - prev['year']
                row['abs_growth_km2'] = r['area_km2'] - prev['area_km2']
                row['growth_pct'] = (row['abs_growth_km2'] / prev['area_km2'] * 100) if prev['area_km2'] > 0 else None
                row['annual_rate_pct'] = ((r['area_km2'] / prev['area_km2']) ** (1 / dy) - 1) * 100 \
                    if prev['area_km2'] > 0 and dy > 0 and r['area_km2'] > 0 else None
            rows.append(row)
            prev = r

        fields = ['year', 'temporal_status', 'common_observed_area_km2', 'area_km2', 'abs_growth_km2', 'growth_pct', 'annual_rate_pct',
                  'perimeter_km', 'patches', 'compactness', 'mode', 'formula']
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(fields)
            for r in rows:
                w.writerow([('' if r.get(k) is None else
                             (f"{r[k]:.4f}".replace('.', ',') if isinstance(r.get(k), float) else r.get(k)))
                            for k in fields])

        def fmt(v, spec):
            w = re.search(r'\d+', spec).group()
            return format('-', '<' + w) if v is None else format(v, spec)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("=" * 92 + "\n")
            f.write("        АНАЛИТИЧЕСКИЙ ОТЧЁТ: ДИНАМИКА ПЛОЩАДИ ГОРОДСКОЙ ЗАСТРОЙКИ\n")
            f.write("=" * 92 + "\n\n")
            f.write(f"Urban Auto {PLUGIN_VERSION}\n")
            f.write(f"Дата генерации: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Целевая директория: {folder}\n")
            f.write(f"Морфология: +{self.dsb_close.value():.0f} / -{self.dsb_shrink.value():.0f} м; "
                    f"мин. пятно {self.dsb_min_area.value():.1f} га; накопительный рост: "
                    f"{'да, ' + format(self.dsb_merge_radius.value(), '.0f') + ' м' if self.cb_cumulative.isChecked() else 'нет'}; "
                    f"временная согласованность: {'да, k = ' + str(self.sb_confirm.value()) if self.cb_temporal.isChecked() else 'нет'}; "
                    f"пороги по точкам: {self.cb_box_method.currentText()}\n\n")
            f.write(f"{'Год':<7}{'Площадь, км²':<15}{'Прирост, км²':<15}{'Прирост, %':<12}"
                    f"{'Ср.год., %':<12}{'Периметр, км':<15}{'Пятен':<8}{'Компактн.':<10}\n")
            f.write("-" * 92 + "\n")
            for r in rows:
                f.write(f"{r['year']:<7}{r['area_km2']:<15.3f}{fmt(r['abs_growth_km2'], '<+15.3f')}"
                        f"{fmt(r['growth_pct'], '<+12.2f')}{fmt(r['annual_rate_pct'], '<+12.2f')}"
                        f"{r['perimeter_km']:<15.2f}{r['patches']:<8}{r['compactness']:<10.3f}\n")
            f.write("-" * 92 + "\n\n")
            f.write("ОСНОВНЫЕ ВЫВОДЫ:\n")
            if len(rows) > 1:
                a, z = rows[0], rows[-1]
                ch = z['area_km2'] - a['area_km2']
                f.write(f"* За {a['year']}–{z['year']} гг. площадь застройки изменилась на {ch:+.3f} км²")
                if a['area_km2'] > 0:
                    span = z['year'] - a['year']
                    cagr = ((z['area_km2'] / a['area_km2']) ** (1 / span) - 1) * 100 if span else 0
                    f.write(f" ({ch / a['area_km2'] * 100:+.2f}%; в среднем {cagr:+.2f}% в год).\n")
                else:
                    f.write(".\n")
                best = max(rows[1:], key=lambda r: r['abs_growth_km2'] or 0)
                f.write(f"* Максимальный прирост: период до {best['year']} г. ({best['abs_growth_km2']:+.3f} км²).\n")
            else:
                f.write("* Рассчитан один временной срез — динамика не определяется.\n")
            if self.cb_temporal.isChecked() and len(rows) > 1:
                z = rows[-1]
                lo, hi = z.get('last_lower_km2'), z.get('last_upper_km2')
                if lo is not None and hi is not None:
                    f.write(f"* Последний срез ({z['year']}) предварительный: площадь от {lo:.3f} км² (подтверждение предыдущим срезом) "
                            f"до {hi:.3f} км² (без подтверждения); в слой записана "
                            f"{'нижняя' if self.cb_last_confirm.isChecked() else 'верхняя'} оценка.\n")
                else:
                    f.write("* Последний срез предварительный: его новые пятна ещё не подтверждены будущими наблюдениями.\n")
            f.write("\nФОРМУЛЫ КЛАССИФИКАЦИИ (A=NDBI, B=BSI, C=MNDWI, D=NDVI):\n")
            for r in rows:
                f.write(f"{r['year']} [{r['mode']}]: {r['formula']}\n")
            f.flush()
            os.fsync(f.fileno())
        return txt_path

    def apply_temporal(self, layers_to_add, report_data, grid_src, folder):
        """Независимые годовые результаты → согласованный ряд (v4.2). Исходные файлы сохраняются."""
        import numpy as np
        from .urban_auto_temporal import rasterize_layer, temporal_consistency, polygonize, pixel_area_km2, _grid, coverage_on_grid
        from .urban_auto_core import measure_layer
        grid = _grid(grid_src)
        area = pixel_area_km2(grid)
        common = None
        for year, _ in layers_to_add:
            valid = coverage_on_grid(os.path.join(folder, f'Valid_{year}.tif'), grid)
            common = valid if common is None else common & valid
        if common is None or not common.any():
            raise ValueError("Нет общей наблюдаемой территории между годами.")
        masks = {y: rasterize_layer(p, grid) & common for y, p in layers_to_add}
        years = sorted(masks)
        last_mode = 'backward' if self.cb_last_confirm.isChecked() else 'accept'
        cons = temporal_consistency(masks, years, self.sb_confirm.value(), last_mode)
        alt = temporal_consistency(masks, years, self.sb_confirm.value(),
                                   'accept' if last_mode == 'backward' else 'backward')
        min_km2 = self.dsb_min_area.value() * 0.01
        try:
            from scipy import ndimage as ndi
        except ImportError:
            raise RuntimeError("Для согласования ряда требуется scipy; установите её в среду QGIS.")

        def area_filter(mask):
            lab, n = ndi.label(mask, np.ones((3, 3)))  # 8-связность, как в 4.2.1
            if n and min_km2 > 0:
                sz = ndi.sum(area, lab, range(1, n + 1))
                keep = np.zeros(n + 1, bool); keep[1:] = sz >= min_km2
                return keep[lab]
            return mask

        bounds_last = sorted([float(area[area_filter(cons[years[-1]])].sum()),
                              float(area[area_filter(alt[years[-1]])].sum())])
        new_layers, new_report = [], []
        raw = {r['year']: r for r in report_data}
        for y in years:
            m = area_filter(cons[y])
            path = os.path.normpath(os.path.join(folder, f"Urban_Consistent_{y}.gpkg")).replace('\\', '/')
            for old in QgsProject.instance().mapLayersByName(f"Urban_Consistent_{y}"):
                QgsProject.instance().removeMapLayer(old.id())
            polygonize(m, grid, path, f"urban_{y}", y)
            lyr = QgsVectorLayer(path, f"Urban_Consistent_{y}", "ogr")
            met = measure_layer(lyr, QgsProject.instance().transformContext())
            r = dict(raw.get(y, {}))
            future_count = len(years) - years.index(y) - 1
            r.update(met, year=y, raw_area_km2=raw.get(y, {}).get('area_km2'),
                     temporal_status='provisional_last' if future_count == 0 else ('limited_future_support' if future_count < self.sb_confirm.value() else 'future_supported'),
                     common_observed_area_km2=float(area[common].sum()),
                     last_mode=last_mode if future_count == 0 else None,
                     last_lower_km2=bounds_last[0] if future_count == 0 else None,
                     last_upper_km2=bounds_last[1] if future_count == 0 else None,
                     mode=f"{raw.get(y, {}).get('mode', '')}+temporal(k={self.sb_confirm.value()},last={last_mode})")
            new_report.append(r)
            new_layers.append((y, path))
        return new_layers, new_report

    def remove_result_layer(self, year):
        """Убирает из проекта прошлый результат за этот год, чтобы файл можно было перезаписать (Windows)."""
        project = QgsProject.instance()
        for lyr in project.mapLayersByName(f"Urban_Auto_{year}"):
            project.removeMapLayer(lyr.id())
        QCoreApplication.processEvents()

    def on_roi_type_changed(self, index):
        self.btn_draw_polygon.setEnabled(index == 2)
        self.spin_osm_level.setEnabled(index == 1)

        if index == 2:
            self.status_label.setText("Выбран пользовательский полигон. Нарисуйте его кнопкой «✏️ Нарисовать полигон».")
        elif index == 1:
            self.status_label.setText("Выбрана OSM граница. Укажите admin_level (обычно 8 для города).")
        else:
            self.status_label.setText("Выбран круг по радиусу.")

    def run_analysis(self):
        project = QgsProject.instance()
        output_folder = self.le_output_folder.text() or os.path.expanduser("~")

        years_str = self.le_multi_years.text().strip()
        if years_str:
            try:
                years = [int(y.strip()) for y in years_str.replace(';', ',').split(',') if y.strip()]
            except ValueError:
                self.status_label.setText("Ошибка: Неверный формат списка лет. Используйте числа через запятую.")
                return
        else:
            years = [self.spin_year.value()]

        if not years or len(years) != len(set(years)):
            self.status_label.setText("Ошибка: список лет пуст или содержит повторы.")
            return
        years.sort()

        self.progressBar.setRange(0, len(years))
        self.progressBar.setValue(0)
        self.setEnabled(False) 

        report_data = []
        layers_to_add = [] 
        prev_result_path = None
        reference_path = None
        job_error = None

        def log_progress(msg):
            self.status_label.setText(msg)
            QCoreApplication.processEvents()

        try:
            for idx, y in enumerate(years):
                log_progress(f"Подготовка данных для {y} года...")

                if years_str:
                    ndbi_lyr_list = project.mapLayersByName(f"NDBI_{y}")
                    bsi_lyr_list = project.mapLayersByName(f"BSI_{y}")
                    ndvi_lyr_list = project.mapLayersByName(f"NDVI_{y}")
                    mndwi_lyr_list = project.mapLayersByName(f"MNDWI_max_{y}")

                    if not (ndbi_lyr_list and bsi_lyr_list and ndvi_lyr_list and mndwi_lyr_list):
                        err_text = f"ОШИБКА: Нет слоев для {y} г. (нужны NDBI_{y}, BSI_{y}, NDVI_{y}, MNDWI_max_{y})"
                        self.status_label.setText(err_text)
                        raise ValueError(err_text)
                    ndbi = ndbi_lyr_list[0]
                    bsi = bsi_lyr_list[0]
                    ndvi = ndvi_lyr_list[0]
                    mndwi = mndwi_lyr_list[0]
                else:
                    ndbi = self.combo_ndbi.currentLayer()
                    bsi = self.combo_bsi.currentLayer()
                    ndvi = self.combo_ndvi.currentLayer()
                    mndwi = self.combo_mndwi.currentLayer()

                if not all([ndbi, bsi, ndvi, mndwi]):
                    self.status_label.setText(f"Ошибка: Не выбраны слои для расчета {y} года.")
                    raise ValueError(f"Не выбраны все растры для {y} года.")

                polygon_id = None
                if self.cb_roi_type.currentIndex() == 2:
                    if not self.polygon_layer:
                        self.status_label.setText("Ошибка: вы выбрали полигон, но не нарисовали его.")
                        raise ValueError("Не задан полигон ROI.")
                    polygon_id = self.polygon_layer.id()

                try:
                    threshold_options = self.calculate_formula_for_layers(ndbi, bsi, ndvi, mndwi, log_progress)
                    if reference_path is None:
                        reference_path = ndbi.source()

                    config = UrbanConfig(
                        year=y,
                        center=(self.dsb_center_lon.value(), self.dsb_center_lat.value()),
                        radius_km=self.dsb_radius_km.value(),
                        roi_type=['circle', 'osm', 'polygon'][self.cb_roi_type.currentIndex()],
                        osm_level=self.spin_osm_level.value(),
                        polygon_layer_id=polygon_id,
                        formula="",
                        output_folder=output_folder,
                        ndbi_path=ndbi.source(),
                        bsi_path=bsi.source(),
                        ndvi_path=ndvi.source(),
                        mndwi_path=mndwi.source(),
                        is_geo=ndbi.crs().isGeographic(),
                        crs_authid=ndbi.crs().authid(),
                        previous_path=prev_result_path if self.cb_cumulative.isChecked() else None,
                        smart_merge_radius=self.dsb_merge_radius.value(),
                        close_dist=self.dsb_close.value(),
                        shrink_dist=self.dsb_shrink.value(),
                        min_area_m2=self.dsb_min_area.value() * 10000.0,
                        clip_to_roi=self.cb_clip.isChecked(),
                        threshold_options=threshold_options,
                        reference_path=reference_path,
                    )
                    self.remove_result_layer(y)

                    m = {}
                    result_path, area_km2 = run_urban_analysis_core(config, log_progress, metrics_sink=m)
                    m.update(year=y)
                    report_data.append(m)
                    layers_to_add.append((y, result_path))
                    grid_src = os.path.join(output_folder, f"Valid_{years[0]}.tif")
                    
                    if self.cb_cumulative.isChecked():
                        prev_result_path = result_path

                except Exception as e:
                    QgsMessageLog.logMessage(traceback.format_exc(), 'Urban Auto', Qgis.Critical)
                    job_error = f"ОШИБКА НА ГОДУ {y}: {str(e)}"
                    self.status_label.setText(job_error)
                    break

                self.progressBar.setValue(idx + 1)

            if not job_error and self.cb_temporal.isChecked() and len(layers_to_add) > 1:
                log_progress("Временная согласованность ряда...")
                layers_to_add, report_data = self.apply_temporal(layers_to_add, report_data, grid_src, output_folder)

            if report_data:
                rep_file = self.generate_growth_report(report_data, output_folder)
                self.status_label.setText(f"ЧАСТИЧНЫЙ РЕЗУЛЬТАТ: {job_error}. Отчёт: {rep_file}" if job_error
                                          else f"Успешно! Отчёт сохранён: {rep_file}")
            elif not layers_to_add:
                self.status_label.setText("Расчет завершился без результатов.")

            for y, path in layers_to_add:
                name = f"Urban_Consistent_{y}" if 'Urban_Consistent_' in path else f"Urban_Auto_{y}"
                for old in project.mapLayersByName(name):
                    project.removeMapLayer(old.id())
                layer = QgsVectorLayer(path, name, "ogr")
                if layer.isValid():
                    project.addMapLayer(layer)

        except Exception as e:
            QgsMessageLog.logMessage(traceback.format_exc(), 'Urban Auto', Qgis.Critical)
            self.status_label.setText(f"Ошибка: {e}")
        finally:
            self.setEnabled(True)
