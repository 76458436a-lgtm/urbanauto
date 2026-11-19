"""User-facing workspace. Runs the existing Processing algorithm in a QGIS task."""
import datetime
import json
import os
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QSettings, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFont
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)
from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsMapLayerProxyModel,
    QgsProcessingAlgRunnerTask, QgsProcessingContext, QgsProcessingFeedback,
    QgsProject, QgsVectorLayer)
from qgis.gui import QgsMapLayerComboBox

from .urban_auto_i18n import tr, translate_widget, language


class SafeCombo(QComboBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SafeSpin(QSpinBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SafeDouble(QDoubleSpinBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class WorkspaceFeedback(QgsProcessingFeedback):
    message = pyqtSignal(str)

    def pushInfo(self, text):
        self.message.emit(str(text))

    def pushWarning(self, text):
        self.message.emit('Предупреждение: ' + str(text))

    def reportError(self, text, fatalError=False):
        self.message.emit('Ошибка: ' + str(text))

    def pushDebugInfo(self, text):
        self.message.emit(str(text))

    def pushCommandInfo(self, text):
        self.message.emit(str(text))


class UrbanWorkspace(QDialog):
    SETTINGS = 'UrbanAuto/workspace_v1/'

    def __init__(self, iface, legacy_callback=None, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.legacy_callback = legacy_callback
        self.running = False
        self.last_output = ''
        self.last_report = ''
        self.controls = {}
        self.forms = []
        self.setFont(QFont('Segoe UI', 10))
        self.setObjectName('urbanWorkspace')
        self.setWindowTitle('Urban Auto — анализ городской застройки')
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(560, 420)
        self.setStyleSheet('''
            QGroupBox { font-weight: bold; border: 1px solid palette(mid); border-radius: 6px;
                        margin-top: 12px; padding: 12px 10px 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
            QPushButton { padding: 6px 10px; }
            QPushButton#startCalculation { background: #196b76; color: white; font-weight: bold;
                                           border: none; border-radius: 5px; padding: 9px 18px; }
            QPushButton#startCalculation:disabled { background: #829698; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { min-height: 26px; }
            QTabWidget::pane { border: 1px solid palette(mid); }
        ''')
        outer = QVBoxLayout(self)
        title = QLabel('Urban Auto')
        title.setStyleSheet('font-size: 22px; font-weight: bold;')
        outer.addWidget(title)
        subtitle = self.hint('Городская застройка по годам • автоматический запуск и ваши настройки')
        outer.addWidget(subtitle)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.pages = []
        self.scrolls = []
        basic = self.page('Расчёт')
        mode = self.group(basic, '1. Выберите способ работы')
        self.mode = self.combo(['Автоматически', 'Настроить самостоятельно'])
        mode.addRow('Режим', self.mode)
        self.profile = self.combo(['Строгий — здания и покрытия', 'Высокая полнота — профиль hgb_nt'])
        mode.addRow('Профиль', self.profile)
        self.profile_help = self.hint('')
        mode.addRow(self.profile_help)

        area = self.group(basic, '2. Территория и период')
        self.scope = self.combo(['Название города', 'Полигон из проекта', 'Текущий вид карты'])
        area.addRow('Территория', self.scope)
        self.city = QLineEdit()
        self.city.setPlaceholderText('Например: Урай, ХМАО, Россия')
        area.addRow('Город', self.city)
        self.roi = self.layer_combo(QgsMapLayerProxyModel.PolygonLayer)
        area.addRow('Слой границы', self.roi)
        self.extent_help = self.hint('Для «Текущего вида карты» охват берётся в момент запуска. '
                                     'Увеличьте нужный участок в основном окне QGIS.')
        area.addRow(self.extent_help)
        self.start_year = self.spin(2000, 1985, 2100)
        self.end_year = self.spin(2025, 1985, 2100)
        years = QWidget(); yr = QHBoxLayout(years); yr.setContentsMargins(0, 0, 0, 0)
        yr.addWidget(QLabel('с')); yr.addWidget(self.start_year); yr.addWidget(QLabel('по')); yr.addWidget(self.end_year)
        area.addRow('Годы включительно', years)
        area.addRow(self.hint('Нужны минимум 3 года. Снимки и автоматические обучающие метки '
                             'загружаются при расчёте; готовый локальный кэш можно указать в настройках.'))

        output = self.group(basic, '3. Сохраните результат')
        self.output_root = QLineEdit(str(Path.home() / 'Documents' / 'UrbanAuto'))
        output.addRow('Папка', self.path_row(self.output_root))
        output.addRow(self.hint('Каждый запуск создаёт отдельную подпапку с городом, годами и временем.'))
        self.add_layers = QCheckBox('Добавить полученные карты в текущий проект QGIS')
        self.add_layers.setChecked(True)
        self.add_layers.setAccessibleName('Добавить карты в текущий проект QGIS')
        output.addRow(self.add_layers, self.hint(self.add_layers.text()))
        self.add_layers.setText('')
        self.summary = self.hint('')
        output.addRow(self.summary)
        basic.addStretch()

        advanced = self.page('Настройки')
        self.manual_notice = self.hint('')
        advanced.addWidget(self.manual_notice)
        self.customize = QPushButton('Перейти к ручной настройке')
        self.customize.clicked.connect(lambda: self.mode.setCurrentIndex(1))
        advanced.addWidget(self.customize)
        self.advanced_body = QWidget(); body = QVBoxLayout(self.advanced_body)
        body.setContentsMargins(0, 0, 0, 0); advanced.addWidget(self.advanced_body)
        data = self.group(body, 'Снимки и сетка')
        self.add_control(data, 'SENTINEL_SERIES', 'Основной источник', self.combo(['Landsat (с 1985)', 'Sentinel-2 (с 2018)']))
        self.add_control(data, 'RESOLUTION', 'Размер пикселя, м', self.spin(30, 10, 120, True))
        self.reference = self.layer_combo(QgsMapLayerProxyModel.RasterLayer)
        data.addRow('Взять сетку из растра', self.reference)
        data.addRow(self.hint('Опорный растр задаёт сетку вместо размера пикселя. Для композитов '
                             'используйте их исходную сетку и соответствующий источник снимков.'))
        self.composites = QLineEdit(); self.composites.setPlaceholderText('Необязательно — иначе модуль загрузит снимки')
        data.addRow('Готовые композиты', self.path_row(self.composites))
        self.add_control(data, 'USE_S2', '', QCheckBox('Добавить признаки Sentinel-2 к ряду Landsat'))

        model = self.group(body, 'Модель и обучение')
        self.add_control(model, 'DEFINITION', 'Что считать городом', self.combo(['Здания и покрытия', 'Кварталы с дворами и зеленью']))
        self.add_control(model, 'MODEL', 'Модель', self.combo(['Авто (градиентный бустинг)', 'Градиентный бустинг', 'Random Forest']))
        self.add_control(model, 'N_TREES', 'Деревьев Random Forest', self.spin(200, 20, 2000))
        self.add_control(model, 'N_PER_CLASS', 'Примеров на класс и год', self.spin(2000, 100, 20000))
        self.add_control(model, 'TEMPORAL', '', QCheckBox('Использовать временные признаки'))
        self.add_control(model, 'WEIGHT_OBS', '', QCheckBox('Учитывать число ясных наблюдений'))
        self.add_control(model, 'FRACTION', '', QCheckBox('Рассчитывать долю застройки в пикселе'))
        self.training = self.layer_combo(QgsMapLayerProxyModel.PointLayer)
        model.addRow('Точки для обучения', self.training)
        model.addRow(self.hint('Необязательный слой с полем label: 1 — город, 0 — не город. '
                              'Поле year задаёт год. Проверочные точки выбираются отдельно.'))
        self.add_control(model, 'CALIBRATE', '', QCheckBox('Подобрать порог по обучающим точкам'))

        timeline = self.group(body, 'Динамика и выходные карты')
        self.add_control(timeline, 'MIN_RUN', 'Лет подтверждения застройки', self.spin(3, 1, 5))
        self.add_control(timeline, 'MIN_AREA_HA', 'Минимальная площадь полигона, га', self.spin(.5, 0, 10000, True))
        self.add_control(timeline, 'COST', 'Ошибка определения года', self.combo(['Линейная', 'Логарифмическая']))
        self.add_control(timeline, 'ADAPTIVE_EDGE', '', QCheckBox('Показывать предварительный прирост последних лет'))
        timeline.addRow(self.hint('Предварительный прирост ещё не подтверждён полным числом последующих лет.'))
        self.report_years = QLineEdit(); self.report_years.setPlaceholderText('Пусто — первый и последний годы')
        timeline.addRow('Годы векторных карт', self.report_years)
        self.add_control(timeline, 'GROWTH_SINCE', 'Считать прирост с года', self.spin(0, 0, 2100))
        self.controls['GROWTH_SINCE'].setSpecialValueText('Первый год ряда')
        self.add_control(timeline, 'MIN_COVERAGE', 'Исключать снимок, если покрытие меньше, %', self.spin(0, 0, 100))
        self.controls['MIN_COVERAGE'].setSpecialValueText('Не исключать')
        timeline.addRow(self.hint('Год, где летний композит покрывает меньшую часть территории (например, одна сцена '
                                  'Landsat 7 с полосами), не участвует в расчёте и помечается в отчёте.'))
        perf = self.group(body, 'Ресурсы и защита проверки')
        self.add_control(perf, 'WORKERS', 'Параллельных загрузок', self.spin(6, 1, 16))
        self.add_control(perf, 'PROTECT_RADIUS', 'Защитный радиус, пиксели', self.spin(0, 0, 100))
        self.controls['PROTECT_RADIUS'].setSpecialValueText('Автоматически')
        perf.addRow(self.hint('Автоматический радиус — около 120 м, минимум 4 пикселя. '
                             'Окрестности контрольных точек исключаются из обучения. '
                             'При большом радиусе останется меньше обучающих пикселей.'))
        advanced.addStretch()

        check = self.page('Проверка и инструменты')
        validation = self.group(check, 'Проверка результата')
        self.validation = self.layer_combo(QgsMapLayerProxyModel.PointLayer)
        validation.addRow('Контрольные точки', self.validation)
        validation.addRow(self.hint('Необязательно. Эти точки не участвуют в обучении. '
            'Для оценки точности нужны label (1/0) и при необходимости year. '
            'Не выбирайте здесь тот же слой, что и для обучения.'))
        for label, ident, help_text in [
            ('Точки для разметки', 'label_points', 'Обучающие или случайные проверочные точки по готовому результату.'),
            ('Оценить точность', 'accuracy_assessment', 'Сравнение классификации с эталонными данными.'),
            ('Временная согласованность', 'temporal_consistency', 'Согласование уже построенных карт по годам.'),
            ('Сравнить с WSF', 'wsf_benchmark', 'Сопоставление результата с внешним продуктом WSF.'),
            ('Загрузить WSF Tracker', 'wsf_download', 'Подготовка данных WSF для выбранной территории.'),
            ('Обработать папку индексов', 'batch_folder', 'Пакетная обработка готовых индексов NDBI, BSI, NDVI и MNDWI.')]:
            button = QPushButton(label); button.clicked.connect(lambda _, name=ident: self.open_tool(name))
            validation.addRow(button, self.hint(help_text))
        legacy = QPushButton('Индексы и ручные пороги…')
        legacy.clicked.connect(lambda: self.legacy_callback() if self.legacy_callback else None)
        validation.addRow(legacy, self.hint('Прежние инструменты выделения города, рисование области и ручная калибровка.'))
        presets = self.group(check, 'Ваши настройки')
        for label, callback in [('Экспорт настроек…', self.export_settings),
                                ('Импорт настроек…', self.import_settings),
                                ('Сбросить профиль', self.apply_profile)]:
            button = QPushButton(label); button.clicked.connect(callback); presets.addRow(button)
        check.addStretch()

        journal = QWidget(); jl = QVBoxLayout(journal)
        jl.addWidget(self.hint('Здесь появятся ход расчёта, предупреждения и путь к результатам. '
                              'QGIS остаётся доступным во время работы.'))
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(10000)
        jl.addWidget(self.log, 1)
        actions = QHBoxLayout()
        self.folder_button = QPushButton('Папка'); self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(lambda: self.open_local(self.last_output))
        self.report_button = QPushButton('Отчёт'); self.report_button.setEnabled(False)
        self.report_button.clicked.connect(lambda: self.open_local(self.last_report))
        save_log = QPushButton('Сохранить журнал…'); save_log.clicked.connect(self.save_log)
        for button in (self.folder_button, self.report_button, save_log): actions.addWidget(button)
        jl.addLayout(actions); self.tabs.addTab(journal, 'Журнал и результат')
        self.status = self.hint('Укажите территорию — остальное можно оставить автоматически.')
        outer.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0)
        outer.addWidget(self.progress)
        footer = QHBoxLayout()
        self.run_button = QPushButton('Запустить расчёт'); self.run_button.setObjectName('startCalculation')
        self.run_button.setAutoDefault(False); self.run_button.clicked.connect(self.start_run)
        self.cancel_button = QPushButton('Остановить'); self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_run)
        self.close_button = QPushButton('Закрыть'); self.close_button.clicked.connect(self.close)
        footer.addWidget(self.run_button); footer.addWidget(self.cancel_button); footer.addStretch(); footer.addWidget(self.close_button)
        outer.addLayout(footer)

        self.profile.currentIndexChanged.connect(self.apply_profile)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.scope.currentIndexChanged.connect(self.update_dependencies)
        for key in ('SENTINEL_SERIES', 'MODEL'):
            self.controls[key].currentIndexChanged.connect(self.update_dependencies)
        self.training.layerChanged.connect(self.update_dependencies)
        self.reference.layerChanged.connect(self.update_dependencies)
        self.start_year.valueChanged.connect(self.update_summary)
        self.end_year.valueChanged.connect(self.update_summary)
        self.apply_profile()
        self.restore_preferences()
        translate_widget(self)
        self.mode_changed()
        self.resize(900, 720)
        saved = QSettings().value(self.SETTINGS + 'geometry')
        if saved is not None: self.restoreGeometry(saved)
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(self.width(), area.width()-30), min(self.height(), area.height()-40))
            if not area.contains(self.frameGeometry().center()): self.move(area.center()-self.rect().center())

    @staticmethod
    def hint(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        label.setMinimumWidth(0)
        return label

    @staticmethod
    def combo(items):
        w = SafeCombo(); w.addItems(items); w.setFocusPolicy(Qt.StrongFocus)
        w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon); w.setMinimumContentsLength(12)
        return w

    @staticmethod
    def spin(value, lo, hi, double=False):
        w = SafeDouble() if double else SafeSpin(); w.setRange(lo, hi); w.setValue(value)
        w.setFocusPolicy(Qt.StrongFocus)
        if double: w.setDecimals(2); w.setSingleStep(.1)
        return w

    @staticmethod
    def layer_combo(filters):
        w = QgsMapLayerComboBox(); w.setFilters(filters); w.setAllowEmptyLayer(True); w.setLayer(None)
        w.setMinimumWidth(0)
        return w

    def page(self, title):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget(); layout = QVBoxLayout(content); layout.setSpacing(12)
        scroll.setWidget(content); self.tabs.addTab(scroll, title)
        self.pages.append(content); self.scrolls.append(scroll)
        return layout

    def group(self, layout, title):
        box = QGroupBox(title); form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.forms.append(form)
        form.setVerticalSpacing(10); layout.addWidget(box)
        return form

    def path_row(self, edit):
        w = QWidget(); layout = QHBoxLayout(w); layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton('Выбрать…'); button.clicked.connect(lambda: self.choose_folder(edit))
        layout.addWidget(edit, 1); layout.addWidget(button)
        return w

    def choose_folder(self, edit):
        path = QFileDialog.getExistingDirectory(self, 'Выберите папку', edit.text() or str(Path.home()))
        if path: edit.setText(path)

    def add_control(self, form, key, title, widget):
        self.controls[key] = widget; widget.setObjectName(key)
        if isinstance(widget, QCheckBox):
            text = widget.text(); widget.setText(''); widget.setAccessibleName(text)
            label = self.hint(text); label.setBuddy(widget)
            form.addRow(widget, label)
        elif title: form.addRow(self.hint(title), widget)
        else: form.addRow(widget)

    @staticmethod
    def value(widget):
        if isinstance(widget, QCheckBox): return widget.isChecked()
        if isinstance(widget, QComboBox): return widget.currentIndex()
        return widget.value()

    @staticmethod
    def set_value(widget, value):
        if isinstance(widget, QCheckBox): widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox): widget.setCurrentIndex(int(value))
        else: widget.setValue(value)

    def apply_profile(self, *_):
        high = self.profile.currentIndex() == 1
        values = dict(SENTINEL_SERIES=0, RESOLUTION=30, USE_S2=False,
                      DEFINITION=int(high), MODEL=1 if high else 0, N_TREES=200,
                      N_PER_CLASS=2000, TEMPORAL=not high, WEIGHT_OBS=not high,
                      FRACTION=not high, CALIBRATE=False, MIN_RUN=3, MIN_AREA_HA=.5,
                      COST=0, ADAPTIVE_EDGE=False, GROWTH_SINCE=0, WORKERS=6, PROTECT_RADIUS=0, MIN_COVERAGE=0)
        for key, value in values.items(): self.set_value(self.controls[key], value)
        self.update_summary(); self.update_dependencies()

    def mode_changed(self, *_):
        manual = self.mode.currentIndex() == 1
        if not manual:
            self.apply_profile()
        self.advanced_body.setEnabled(manual and not self.running)
        self.customize.setVisible(not manual)
        self.manual_notice.setText(tr('Параметры доступны для изменения. Выбор профиля возвращает его настройки.' if manual else
                                  'Автоматический режим использует настройки выбранного профиля. Здесь можно перейти к ручному управлению.'))
        self.update_dependencies(); self.update_summary()

    def update_dependencies(self, *_):
        if not hasattr(self, 'training'): return
        self.city.setEnabled(self.scope.currentIndex() == 0)
        self.roi.setEnabled(self.scope.currentIndex() == 1)
        self.extent_help.setVisible(self.scope.currentIndex() == 2)
        s2 = self.controls['SENTINEL_SERIES'].currentIndex() == 1
        self.controls['USE_S2'].setEnabled(not s2)
        self.controls['N_TREES'].setEnabled(self.controls['MODEL'].currentIndex() == 2)
        self.controls['CALIBRATE'].setEnabled(self.training.currentLayer() is not None)
        self.controls['RESOLUTION'].setEnabled(self.reference.currentLayer() is None)

    def update_summary(self, *_):
        if not hasattr(self, 'summary'): return
        self.profile_help.setText(tr('Профиль hgb_nt: кварталы с дворами и зеленью. В проверке Урая за 2025 год '
            'полнота 98,87%, пользовательская точность 96,34%. В других городах возможны лишние выделения.'
            if self.profile.currentIndex() else
            'Автоматическое обучение по открытым данным. Строгое выделение зданий и покрытий; '
            'точность зависит от территории и исходных снимков.'))
        y0, y1 = self.start_year.value(), self.end_year.value()
        self.summary.setText(f'Result: built-up maps {y0}–{y1}, year of appearance, areas by year, vector layers and HTML report.'
                             if language() == 'en' else
                             f'Результат: карты застройки {y0}–{y1}, год появления, площади по годам, векторные слои и HTML-отчёт.')

    def preferences(self):
        return dict(schema=1, mode=self.mode.currentIndex(), profile=self.profile.currentIndex(),
            scope=self.scope.currentIndex(), city=self.city.text(), start=self.start_year.value(),
            end=self.end_year.value(), output=self.output_root.text(), composites=self.composites.text(),
            report_years=self.report_years.text(), add_layers=self.add_layers.isChecked(),
            controls={k: self.value(w) for k, w in self.controls.items()},
            layers={k: w.currentLayer().id() if w.currentLayer() else '' for k, w in
                    [('roi', self.roi), ('reference', self.reference), ('training', self.training), ('validation', self.validation)]})

    def load_preferences(self, data):
        if not isinstance(data, dict) or data.get('schema') != 1: raise ValueError('Неподдерживаемый файл настроек')
        if not isinstance(data.get('controls', {}), dict) or not isinstance(data.get('layers', {}), dict):
            raise ValueError('Неверная структура файла настроек')
        # Validate before modifying any widget.
        for key in ('mode', 'profile'):
            if data.get(key, 0) not in (0, 1): raise ValueError('Неверный режим настроек')
        if data.get('scope', 0) not in (0, 1, 2): raise ValueError('Неверный тип территории')
        for key, value in data.get('controls', {}).items():
            if key not in self.controls: continue
            w = self.controls[key]
            if isinstance(w, QCheckBox): valid = type(value) is bool
            elif isinstance(w, QComboBox): valid = type(value) is int and 0 <= value < w.count()
            else: valid = type(value) in (int, float) and w.minimum() <= value <= w.maximum()
            if not valid: raise ValueError('Неверное значение настройки ' + key)
        for key in ('start', 'end'):
            if type(data.get(key, 2000)) is not int or not 1985 <= data.get(key, 2000) <= 2100: raise ValueError('Неверный год')
        self.mode.setCurrentIndex(data.get('mode', 0)); self.profile.setCurrentIndex(data.get('profile', 0))
        self.scope.setCurrentIndex(data.get('scope', 0))
        for widget, key in [(self.city, 'city'), (self.output_root, 'output'), (self.composites, 'composites'), (self.report_years, 'report_years')]:
            widget.setText(str(data.get(key, '')))
        self.start_year.setValue(data.get('start', 2000)); self.end_year.setValue(data.get('end', 2025))
        self.add_layers.setChecked(bool(data.get('add_layers', True)))
        for key, value in data.get('controls', {}).items():
            if key in self.controls: self.set_value(self.controls[key], value)
        for key in ('roi', 'reference', 'training', 'validation'):
            getattr(self, key).setLayer(QgsProject.instance().mapLayer(data.get('layers', {}).get(key, '')))
        self.mode_changed()

    def restore_preferences(self):
        saved = QSettings().value(self.SETTINGS + 'preferences', '')
        if saved:
            try: self.load_preferences(json.loads(saved))
            except (ValueError, TypeError, KeyError): self.log.appendPlainText('Сохранённые настройки не прочитаны. Использованы значения профиля.')

    def save_preferences(self):
        settings = QSettings(); settings.setValue(self.SETTINGS + 'geometry', self.saveGeometry())
        settings.setValue(self.SETTINGS + 'preferences', json.dumps(self.preferences(), ensure_ascii=False))

    def export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить настройки', 'UrbanAuto_settings.json', 'JSON (*.json)')
        if path:
            try: Path(path).write_text(json.dumps(self.preferences(), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as ex: self.show_error(str(ex))

    def import_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Загрузить настройки', '', 'JSON (*.json)')
        if path:
            try: self.load_preferences(json.loads(Path(path).read_text(encoding='utf-8-sig')))
            except (OSError, ValueError, TypeError, KeyError) as ex: self.show_error(str(ex))

    def collect_parameters(self):
        manual = self.mode.currentIndex() == 1
        y0, y1 = self.start_year.value(), self.end_year.value()
        if y1-y0 < 2: raise ValueError('Выберите период не короче трёх лет, например 2023–2025.')
        params = {k: self.value(w) for k, w in self.controls.items()}
        if params['SENTINEL_SERIES'] and y0 < 2018: raise ValueError('Sentinel-2: первый год должен быть не раньше 2018.')
        if params['SENTINEL_SERIES']: params['USE_S2'] = False
        if params['GROWTH_SINCE'] and not y0 <= params['GROWTH_SINCE'] <= y1:
            raise ValueError('Год начала прироста должен быть внутри выбранного периода.')
        raw = self.report_years.text().strip() if manual else ''
        try: years = [int(s.strip()) for s in raw.replace(';', ',').split(',') if s.strip()] if raw else [y0, y1]
        except ValueError: raise ValueError('Укажите годы векторных карт через запятую, например 2000, 2010, 2025.')
        if any(not y0 <= year <= y1 for year in years): raise ValueError('Годы векторных карт должны попадать в выбранный период.')
        params.update(START_YEAR=y0, END_YEAR=y1, REPORT_YEARS=','.join(map(str, sorted(set(years)))),
                      ADD_LAYERS=self.add_layers.isChecked(), CITY='', ROI=None,
                      REFERENCE=self.reference.currentLayer() if manual else None,
                      COMPOSITES=self.composites.text().strip() if manual else '',
                      USER_POINTS=self.training.currentLayer() if manual else None,
                      VALIDATION_POINTS=self.validation.currentLayer())
        if self.scope.currentIndex() == 0:
            params['CITY'] = self.city.text().strip()
            if not params['CITY']: raise ValueError('Введите название города или выберите другой способ задания территории.')
        elif self.scope.currentIndex() == 1:
            params['ROI'] = self.roi.currentLayer()
            if params['ROI'] is None: raise ValueError('Выберите полигональный слой границы из проекта.')
        else:
            canvas = self.iface.mapCanvas(); extent = canvas.extent()
            if extent.isEmpty(): raise ValueError('Текущий вид карты пуст. Сначала откройте территорию в QGIS.')
            layer = QgsVectorLayer('Polygon', 'Область текущего вида', 'memory')
            layer.setCrs(canvas.mapSettings().destinationCrs())
            feature = QgsFeature(); feature.setGeometry(QgsGeometry.fromRect(extent))
            layer.dataProvider().addFeatures([feature]); layer.updateExtents()
            self.extent_layer = layer; params['ROI'] = layer
        train, check = params['USER_POINTS'], params['VALIDATION_POINTS']
        if train and check and (train.id() == check.id() or train.source() == check.source()):
            raise ValueError('Для обучения и проверки выберите разные наборы точек.')
        for label, layer in [('Обучающие', train), ('Контрольные', check)]:
            if layer and layer.fields().indexFromName('label') < 0:
                raise ValueError(label + ' точки: в слое отсутствует поле label (1 — город, 0 — не город).')
        if not train: params['CALIBRATE'] = False
        if params['COMPOSITES'] and not Path(params['COMPOSITES']).is_dir(): raise ValueError('Папка композитов не найдена.')
        if not self.output_root.text().strip(): raise ValueError('Выберите папку для сохранения результатов.')
        return params

    def show_error(self, text):
        self.status.setText(tr(str(text))); QMessageBox.warning(self, tr('Проверьте параметры'), tr(str(text)))

    def start_run(self):
        if self.running: return
        try:
            params = self.collect_parameters()
            alg = QgsApplication.processingRegistry().algorithmById('urbanauto:rf_series')
            if alg is None: raise ValueError('Алгоритм Urban Auto не зарегистрирован. Перезапустите плагин.')
            self.context = QgsProcessingContext(); self.context.setProject(QgsProject.instance())
            if self.scope.currentIndex() == 2: self.context.temporaryLayerStore().addMapLayer(self.extent_layer)
            title = params['CITY'] or (params['ROI'].name() if params['ROI'] else 'Территория')
            import re
            safe = re.sub(r'[^\w-]+', '_', title).strip('_')[:60] or 'Город'
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            folder = Path(self.output_root.text().strip()) / f'{safe}_{params["START_YEAR"]}_{params["END_YEAR"]}_{stamp}'
            params['OUTPUT_FOLDER'] = str(folder)
            ok, message = alg.checkParameterValues(params, self.context)
            if not ok: raise ValueError(message)
            folder.mkdir(parents=True, exist_ok=False)
            self.active_output = str(folder)
            self.last_output = str(folder); self.last_report = ''
            self.folder_button.setEnabled(True); self.report_button.setEnabled(False)
            config = self.preferences(); config['resolved_output'] = str(folder)
            (folder / 'interface_settings.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
            self.save_preferences()
            self.feedback = WorkspaceFeedback(); self.feedback.message.connect(self.on_message)
            self.feedback.progressChanged.connect(lambda value: self.progress.setValue(int(value)))
            self.task = QgsProcessingAlgRunnerTask(alg, params, self.context, self.feedback)
            self.task.executed.connect(self.finished)
            self.log.clear(); self.on_message('Папка результата: ' + str(folder))
            self.set_running(True); self.progress.setValue(0)
            self.tabs.setCurrentIndex(3); self.status.setText(tr('Расчёт выполняется. Можно продолжать работу в QGIS.'))
            QgsApplication.taskManager().addTask(self.task)
        except Exception as ex:
            self.set_running(False); self.show_error(str(ex))

    def on_message(self, text):
        self.log.appendPlainText(text)
        if text.startswith('Шаг '): self.status.setText(text)

    def set_running(self, running):
        self.running = running
        self.run_button.setEnabled(not running); self.cancel_button.setEnabled(running)
        for page in self.pages: page.setEnabled(not running)
        self.close_button.setText(tr('Скрыть окно' if running else 'Закрыть'))
        if not running: self.mode_changed()

    def cancel_run(self):
        if self.running:
            self.feedback.cancel(); self.task.cancel(); self.cancel_button.setEnabled(False)
            self.status.setText(tr('Остановка запрошена. Дождитесь завершения текущего шага.'))

    def finished(self, success, results):
        canceled = self.feedback.isCanceled()
        self.set_running(False)
        self.last_output = self.active_output
        self.folder_button.setEnabled(Path(self.last_output).is_dir())
        self.last_report = str(results.get('REPORT', '')) if success and not canceled else ''
        self.report_button.setEnabled(bool(self.last_report) and Path(self.last_report).is_file())
        if canceled: message = 'Расчёт остановлен. Частичные файлы сохранены в папке запуска.'
        elif success: self.progress.setValue(100); message = 'Готово. Карты и отчёт сохранены.'
        else: message = 'Расчёт не завершён. Причина указана в журнале.'
        self.status.setText(message); self.on_message(message)
        try: (Path(self.last_output) / 'interface_log.txt').write_text(self.log.toPlainText(), encoding='utf-8')
        except OSError as ex: self.on_message('Не удалось сохранить журнал: ' + str(ex))
        self.task = None

    def open_tool(self, name):
        import processing
        ident = 'urbanauto:' + name
        if not QgsApplication.processingRegistry().algorithmById(ident):
            self.show_error('Инструмент не найден: ' + name); return
        processing.execAlgorithmDialog(ident)

    @staticmethod
    def open_local(path):
        if path and Path(path).exists(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить журнал', 'UrbanAuto_log.txt', 'Текст (*.txt)')
        if path:
            try: Path(path).write_text(self.log.toPlainText(), encoding='utf-8')
            except OSError as ex: self.show_error(str(ex))

    def closeEvent(self, event):
        self.save_preferences()
        if self.running:
            self.hide(); event.ignore()
        else: super().closeEvent(event)

    def reject(self):
        self.close()

    def resizeEvent(self, event):
        for form in getattr(self, 'forms', []):
            form.setRowWrapPolicy(QFormLayout.WrapAllRows if self.width() < 700 else QFormLayout.WrapLongRows)
        super().resizeEvent(event)
