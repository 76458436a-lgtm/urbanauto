# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""5.5.1: английский интерфейс без переписывания кода.

Если язык QGIS не русский (Настройки → Параметры → Общие → Язык), подписи окна Urban Auto, меню и
инструментов Processing подставляются из словаря EN. Журнал расчёта и HTML-отчёт остаются на русском.
Принудительно: переменная окружения URBANAUTO_LANG=en или ru."""
import os

EN = {
    # окно и меню
    'Urban Auto — анализ городской застройки': 'Urban Auto — urban built-up analysis',
    'Городская застройка по годам • автоматический запуск и ваши настройки': 'Built-up area by year • automatic run or your own settings',
    'Расчёт': 'Run', '1. Выберите способ работы': '1. Choose how to work', 'Режим': 'Mode', 'Профиль': 'Profile',
    '2. Территория и период': '2. Area and period', 'Территория': 'Area', 'Например: Урай, ХМАО, Россия': 'E.g. Uray, Khanty-Mansi AO, Russia',
    'Город': 'City', 'Слой границы': 'Boundary layer',
    'Для «Текущего вида карты» охват берётся в момент запуска. Увеличьте нужный участок в основном окне QGIS.':
        'For “Current map view” the extent is taken at run time. Zoom to the area in the main QGIS window.',
    'Годы включительно': 'Years (inclusive)', '3. Сохраните результат': '3. Save the result', 'Папка': 'Folder',
    'Добавить полученные карты в текущий проект QGIS': 'Add the resulting maps to the current QGIS project',
    'Добавить карты в текущий проект QGIS': 'Add maps to the current QGIS project', 'Настройки': 'Settings',
    'Перейти к ручной настройке': 'Switch to manual settings', 'Снимки и сетка': 'Imagery and grid', 'Основной источник': 'Main source',
    'Размер пикселя, м': 'Pixel size, m', 'Взять сетку из растра': 'Take grid from raster',
    'Необязательно — иначе модуль загрузит снимки': 'Optional — otherwise imagery is downloaded', 'Готовые композиты': 'Existing composites',
    'Модель и обучение': 'Model and training', 'Что считать городом': 'What counts as urban', 'Модель': 'Model',
    'Деревьев Random Forest': 'Random Forest trees', 'Примеров на класс и год': 'Samples per class and year',
    'Точки для обучения': 'Training points', 'Динамика и выходные карты': 'Change detection and output maps',
    'Лет подтверждения застройки': 'Years to confirm new built-up', 'Минимальная площадь полигона, га': 'Minimum polygon area, ha',
    'Ошибка определения года': 'Year detection cost', 'Пусто — первый и последний годы': 'Empty — first and last year',
    'Годы векторных карт': 'Years of vector maps', 'Считать прирост с года': 'Count growth from year', 'Первый год ряда': 'First year of the series',
    'Исключать снимок, если покрытие меньше, %': 'Exclude composite if coverage below, %', 'Не исключать': 'Never exclude',
    'Ресурсы и защита проверки': 'Resources and check-point protection', 'Параллельных загрузок': 'Parallel downloads',
    'Защитный радиус, пиксели': 'Protection radius, pixels', 'Автоматически': 'Automatic', 'Проверка и инструменты': 'Validation and tools',
    'Проверка результата': 'Result validation', 'Контрольные точки': 'Check points', 'Индексы и ручные пороги…': 'Indices and manual thresholds…',
    'Ваши настройки': 'Your settings', 'Отчёт': 'Report', 'Сохранить журнал…': 'Save log…', 'Журнал и результат': 'Log and result',
    'Укажите территорию — остальное можно оставить автоматически.': 'Set the area — everything else can stay automatic.',
    'Запустить расчёт': 'Run', 'Остановить': 'Stop', 'Закрыть': 'Close', 'Выбрать…': 'Browse…', 'Выберите папку': 'Choose folder',
    'Сохранить настройки': 'Save settings', 'Загрузить настройки': 'Load settings', 'Проверьте параметры': 'Check the parameters',
    'Шаг ': 'Step ', 'Расчёт остановлен. Частичные файлы сохранены в папке запуска.': 'Run stopped. Partial files are kept in the run folder.',
    'Сохранить журнал': 'Save log', 'Текст (*.txt)': 'Text (*.txt)', 'Предупреждение: ': 'Warning: ', 'Ошибка: ': 'Error: ',
    'Настроить самостоятельно': 'Manual settings', 'Строгий — здания и покрытия': 'Strict — buildings and paved surfaces',
    'Высокая полнота — профиль hgb_nt': 'High recall — hgb_nt profile', 'Название города': 'City name', 'Полигон из проекта': 'Polygon from project',
    'Текущий вид карты': 'Current map view', 'с': 'from', 'по': 'to',
    'Нужны минимум 3 года. Снимки и автоматические обучающие метки загружаются при расчёте; готовый локальный кэш можно указать в настройках.':
        'At least 3 years are needed. Imagery and automatic training labels are downloaded during the run; a local cache can be set in Settings.',
    'Каждый запуск создаёт отдельную подпапку с городом, годами и временем.': 'Each run creates its own subfolder named by city, years and time.',
    'Опорный растр задаёт сетку вместо размера пикселя. Для композитов используйте их исходную сетку и соответствующий источник снимков.':
        'A reference raster defines the grid instead of the pixel size. For existing composites use their own grid and matching imagery source.',
    'Добавить признаки Sentinel-2 к ряду Landsat': 'Add Sentinel-2 features to the Landsat series', 'Использовать временные признаки': 'Use temporal features',
    'Учитывать число ясных наблюдений': 'Weight by number of clear observations', 'Рассчитывать долю застройки в пикселе': 'Estimate built-up fraction per pixel',
    'Необязательный слой с полем label: 1 — город, 0 — не город. Поле year задаёт год. Проверочные точки выбираются отдельно.':
        'Optional layer with a label field: 1 — urban, 0 — not urban; year field sets the year. Check points are chosen separately.',
    'Подобрать порог по обучающим точкам': 'Tune threshold with training points', 'Показывать предварительный прирост последних лет': 'Show provisional growth for the last years',
    'Предварительный прирост ещё не подтверждён полным числом последующих лет.': 'Provisional growth is not yet confirmed by the full number of later years.',
    'Год, где летний композит покрывает меньшую часть территории (например, одна сцена Landsat 7 с полосами), не участвует в расчёте и помечается в отчёте.':
        'A year whose summer composite covers less of the area (e.g. a single striped Landsat 7 scene) is left out and flagged in the report.',
    'Автоматический радиус — около 120 м, минимум 4 пикселя. Окрестности контрольных точек исключаются из обучения. При большом радиусе останется меньше обучающих пикселей.':
        'Automatic radius is about 120 m, at least 4 pixels. Surroundings of check points are excluded from training; a large radius leaves fewer training pixels.',
    'Необязательно. Эти точки не участвуют в обучении. Для оценки точности нужны label (1/0) и при необходимости year. Не выбирайте здесь тот же слой, что и для обучения.':
        'Optional. These points are not used for training. Accuracy needs label (1/0) and optionally year. Do not use the training layer here.',
    'Точки для разметки': 'Points to label', 'Обучающие или случайные проверочные точки по готовому результату.': 'Training or random check points from an existing result.',
    'Оценить точность': 'Assess accuracy', 'Сравнение классификации с эталонными данными.': 'Compare the classification with reference data.',
    'Временная согласованность': 'Temporal consistency', 'Согласование уже построенных карт по годам.': 'Make existing yearly maps consistent.',
    'Сравнить с WSF': 'Compare with WSF', 'Сопоставление результата с внешним продуктом WSF.': 'Compare the result with the WSF product.',
    'Загрузить WSF Tracker': 'Download WSF Tracker', 'Подготовка данных WSF для выбранной территории.': 'Prepare WSF data for the selected area.',
    'Обработать папку индексов': 'Process a folder of indices', 'Пакетная обработка готовых индексов NDBI, BSI, NDVI и MNDWI.': 'Batch processing of NDBI, BSI, NDVI and MNDWI rasters.',
    'Прежние инструменты выделения города, рисование области и ручная калибровка.': 'Legacy urban extraction tools, area drawing and manual calibration.',
    'Экспорт настроек…': 'Export settings…', 'Импорт настроек…': 'Import settings…', 'Сбросить профиль': 'Reset profile',
    'Здесь появятся ход расчёта, предупреждения и путь к результатам. QGIS остаётся доступным во время работы.':
        'Progress, warnings and the output path appear here. QGIS stays usable while the run is in progress.',
    'Параметры доступны для изменения. Выбор профиля возвращает его настройки.': 'Parameters can be changed. Choosing a profile restores its settings.',
    'Автоматический режим использует настройки выбранного профиля. Здесь можно перейти к ручному управлению.':
        'Automatic mode uses the selected profile. You can switch to manual control here.',
    'Профиль hgb_nt: кварталы с дворами и зеленью. В проверке Урая за 2025 год полнота 98,87%, пользовательская точность 96,34%. В других городах возможны лишние выделения.':
        'hgb_nt profile: whole blocks with yards and greenery. On the Uray 2025 check: recall 98.87 %, user’s accuracy 96.34 %. Other towns may get false positives.',
    'Автоматическое обучение по открытым данным. Строгое выделение зданий и покрытий; точность зависит от территории и исходных снимков.':
        'Automatic training on open data. Strict mapping of buildings and paved surfaces; accuracy depends on the area and imagery.',
    'Неподдерживаемый файл настроек': 'Unsupported settings file', 'Неверная структура файла настроек': 'Invalid settings file structure',
    'Неверный тип территории': 'Invalid area type', 'Выберите период не короче трёх лет, например 2023–2025.': 'Choose a period of at least three years, e.g. 2023–2025.',
    'Sentinel-2: первый год должен быть не раньше 2018.': 'Sentinel-2: the first year must be 2018 or later.',
    'Год начала прироста должен быть внутри выбранного периода.': 'The growth start year must be inside the period.',
    'Годы векторных карт должны попадать в выбранный период.': 'Vector map years must be inside the period.',
    'Для обучения и проверки выберите разные наборы точек.': 'Use different point layers for training and checking.',
    'Обучающие': 'Training', 'Контрольные': 'Check', 'Папка композитов не найдена.': 'Composite folder not found.',
    'Выберите папку для сохранения результатов.': 'Choose an output folder.', 'Расчёт выполняется. Можно продолжать работу в QGIS.': 'Running. You can keep working in QGIS.',
    'Скрыть окно': 'Hide window', 'Остановка запрошена. Дождитесь завершения текущего шага.': 'Stop requested. Waiting for the current step to finish.',
    'Готово. Карты и отчёт сохранены.': 'Done. Maps and report saved.', 'Расчёт не завершён. Причина указана в журнале.': 'Run not completed. See the log for the reason.',
    'Landsat (с 1985)': 'Landsat (since 1985)', 'Sentinel-2 (с 2018)': 'Sentinel-2 (since 2018)', 'Здания и покрытия': 'Buildings and paved surfaces',
    'Кварталы с дворами и зеленью': 'Blocks with yards and greenery', 'Авто (градиентный бустинг)': 'Auto (gradient boosting)',
    'Градиентный бустинг': 'Gradient boosting', 'Линейная': 'Linear', 'Логарифмическая': 'Logarithmic', 'Неверный режим настроек': 'Invalid settings mode',
    'Неверный год': 'Invalid year', 'Укажите годы векторных карт через запятую, например 2000, 2010, 2025.': 'List vector map years separated by commas, e.g. 2000, 2010, 2025.',
    'Введите название города или выберите другой способ задания территории.': 'Enter a city name or choose another way to set the area.',
    'Область текущего вида': 'Current view extent', 'Алгоритм Urban Auto не зарегистрирован. Перезапустите плагин.': 'Urban Auto algorithm is not registered. Restart the plugin.',
    'Папка результата: ': 'Output folder: ', 'Инструмент не найден: ': 'Tool not found: ', 'Неверное значение настройки ': 'Invalid setting value ',
    'Сохранённые настройки не прочитаны. Использованы значения профиля.': 'Saved settings could not be read; profile values are used.',
    'Выберите полигональный слой границы из проекта.': 'Choose a polygon boundary layer from the project.',
    'Текущий вид карты пуст. Сначала откройте территорию в QGIS.': 'The map view is empty. Open the area in QGIS first.',
    ' точки: в слое отсутствует поле label (1 — город, 0 — не город).': ' points: the layer has no label field (1 — urban, 0 — not urban).',
    'Не удалось сохранить журнал: ': 'Could not save the log: ',
    # Processing
    'Годовой ряд застройки (автоматически, без точек)': 'Annual built-up series (automatic, no points)', 'Городской анализ': 'Urban analysis',
    'Точки для разметки (где модель сомневается)': 'Points to label (where the model is uncertain)', 'Точки для проверки точности': 'Accuracy check points',
    'Территория (полигон)': 'Area (polygon)', '…или название города (если полигон не задан)': '…or city name (if no polygon)', 'Первый год': 'First year',
    'Последний год': 'Last year', 'Размер пикселя, м (UTM)': 'Pixel size, m (UTM)', 'Опорный растр сетки (вместо UTM, необязательно)': 'Reference grid raster (instead of UTM, optional)',
    'Папка готовых композитов (необязательно)': 'Folder with existing composites (optional)',
    'Точки проверки (поле label: 1/0 — точность в отчёт; в обучение не входят)': 'Check points (label 1/0 — accuracy in report; not used for training)',
    'Точки пользователя (поле label: 1/0)': 'User training points (label 1/0)', 'Определение застройки': 'Built-up definition',
    'Строить основной ряд по Sentinel-2 (с 2018 г.; для сетки 10 м задайте размер пикселя 10)': 'Main series from Sentinel-2 (2018+; set pixel size 10 for a 10 m grid)',
    'Добавить признаки Sentinel-2 10 м (с 2018 г., дольше)': 'Add Sentinel-2 10 m features (2018+, slower)',
    'Показывать предварительный прирост у конца ряда при неполном сроке подтверждения': 'Show provisional growth at the end of the series (incomplete confirmation)',
    'Минимум лет подтверждения новой застройки': 'Minimum years to confirm new built-up', 'Минимальная площадь пятна в полигонах, га': 'Minimum patch area in polygons, ha',
    'Годы для полигонов (через запятую)': 'Years for polygons (comma-separated)', 'Добавить результаты в проект': 'Add results to project',
    'Рассчитывать долю застройки': 'Estimate built-up fraction', 'Подбирать порог по обучающим точкам пользователя': 'Tune threshold with user training points',
    'Число деревьев Random Forest': 'Random Forest trees', 'Обучающих примеров на класс и год': 'Training samples per class and year', 'Потоков загрузки': 'Download threads',
    'Радиус исключения точек проверки, пиксели (0 — автоматически)': 'Check-point exclusion radius, pixels (0 — automatic)',
    'Начало периода прироста (0 — первый год)': 'Growth period start (0 — first year)',
    'Исключать годы, где снимок покрывает меньше, % территории (0 — не исключать)': 'Exclude years whose composite covers less than, % of area (0 — never)',
    'Мера ошибки определения года': 'Year detection cost', 'Папка результатов': 'Output folder', 'Протокол': 'Protocol', 'Отчёт HTML': 'HTML report',
    'Задайте территорию полигоном или название города.': 'Set the area as a polygon or a city name.', 'Нужно не меньше трёх лет.': 'At least three years are needed.',
    'Для основного ряда Sentinel-2 выберите годы с 2018.': 'For a Sentinel-2 series choose years from 2018.', 'Начало прироста должно попадать в выбранный период.': 'Growth start must be inside the period.',
    'Год появления застройки': 'Year built-up appeared', 'Открытый грунт (устойчивый)': 'Bare soil (persistent)', 'Папка результата': 'Result folder',
    'Назначение точек': 'Point purpose', 'Число точек': 'Number of points', 'Добавить слой в проект': 'Add layer to project', 'Точки': 'Points',
    'В папке нет Prob_built_by_year.tif — сначала выполните «Годовой ряд застройки».': 'No Prob_built_by_year.tif in the folder — run “Annual built-up series” first.',
    'строгое: здания и покрытия (по умолчанию)': 'strict: buildings and paved surfaces (default)', 'территория: кварталы вместе с дворами и зеленью (как в 5.0)': 'territory: whole blocks with yards and greenery (as in 5.0)',
    'авто (градиентный бустинг)': 'auto (gradient boosting)', 'градиентный бустинг': 'gradient boosting', 'Полигон территории не попал на сетку.': 'The area polygon does not overlap the grid.',
    'Годы полигонов задаются целыми числами через запятую.': 'Polygon years must be integers separated by commas.', 'Новая застройка (пятна с годом)': 'New built-up (patches with year)',
    'для обучения — где модель сомневается': 'for training — where the model is uncertain', 'для проверки точности — случайно по классам карты': 'for accuracy check — random by map class',
    'Не хватает библиотек Python: ': 'Missing Python libraries: ', 'Urban Auto — открыть модуль': 'Urban Auto — open', 'Годовой ряд — форма Processing': 'Annual series — Processing form',
    'Urban Auto — спектральные индексы и ручные пороги': 'Urban Auto — spectral indices and manual thresholds',
    '. Отключите их в «Модули → Управление и установка», чтобы не путать меню и результаты.': '. Disable them in “Plugins → Manage and Install” to avoid mixing menus and results.',
    '. Годовой ряд не запустится. ': '. The annual series will not run. ', 'Включены старые копии модуля: ': 'Old copies of the plugin are enabled: ',
    'Авто-калибровка городской застройки': 'Urban built-up auto-calibration (indices)', 'Слой NDBI': 'NDBI layer', 'Слой BSI': 'BSI layer', 'Слой NDVI': 'NDVI layer',
    'Слой MNDWI (max)': 'MNDWI layer (max)', 'Год': 'Year', 'Результат прошлого среза (накопительный рост)': 'Previous result (cumulative growth)',
    'Радиус привязки к прошлому срезу, м': 'Snap radius to previous result, m', 'Центр города': 'City centre', 'Полигон ROI (приоритет над кругом/OSM)': 'ROI polygon (overrides circle/OSM)',
    'Автоматически выровнять растры и маску NoData': 'Align rasters and NoData mask automatically', 'Лимит пикселей в ROI': 'Pixel limit in ROI', 'Радиус ROI, км': 'ROI radius, km',
    'ROI по границе OSM': 'ROI from OSM boundary', 'Метод Оцу (авто-пороги)': 'Otsu method (auto thresholds)', 'Спектральный баланс NDBI−NDVI': 'NDBI−NDVI spectral balance',
    'Порог баланса по K-Means': 'Balance threshold by K-Means', 'Смещение K-Means или ручной порог NDBI−NDVI': 'K-Means offset or manual NDBI−NDVI threshold',
    'Точки ГОРОДА (долгота, широта WGS84)': 'URBAN points (lon, lat WGS84)', 'Пороги по точкам': 'Thresholds from points', 'Точки ФОНА (опционально)': 'BACKGROUND points (optional)',
    'Морфология: буфер наружу, м': 'Morphology: outward buffer, m', 'Морфология: буфер внутрь, м': 'Morphology: inward buffer, m', 'Минимальная площадь пятна, га': 'Minimum patch area, ha',
    'Обрезать по ROI': 'Clip to ROI', 'Папка вывода': 'Output folder', 'Границы застройки': 'Built-up boundaries', 'Площадь, км²': 'Area, km²',
    'Автономный анализ папки индексов (все годы)': 'Batch analysis of an index folder (all years)', 'Папка индексов': 'Index folder', 'Годы через запятую (пусто = все)': 'Years, comma-separated (empty = all)',
    'Полигон ROI (необязательно)': 'ROI polygon (optional)', 'Число будущих подтверждений': 'Number of later confirmations', 'Последний срез': 'Last slice',
    'Согласовать временной ряд': 'Make the series consistent', 'Продолжать другие годы после ошибки': 'Continue with other years after an error',
    'Папка run_... для возобновления (необязательно)': 'run_... folder to resume (optional)', 'Папка запуска': 'Run folder', 'Статус': 'Status',
    'подтверждать предыдущим (нижняя оценка)': 'confirm by previous (lower estimate)', 'как есть (верхняя оценка)': 'as is (upper estimate)',
    'подтверждать предыдущим срезом (нижняя оценка, рекомендуется)': 'confirm by previous slice (lower estimate, recommended)',
    'принимать без подтверждения (верхняя оценка, предварительно)': 'accept without confirmation (upper estimate, provisional)',
    'Объекты-эталон (здания): застройка ≤ буфера, фон > расстояния': 'Reference features (buildings): built-up ≤ buffer, background > distance',
    'Сплошная карта-эталон (все пиксели)': 'Wall-to-wall reference map (all pixels)', 'Временная согласованность ряда застройки': 'Temporal consistency of the built-up series',
    'Оценка точности застройки (матрица ошибок)': 'Built-up accuracy assessment (confusion matrix)', 'Годовые результаты (полигоны)': 'Yearly results (polygons)',
    'Опорный растр (сетка, например NDBI)': 'Reference raster (grid, e.g. NDBI)', 'Подтверждений в последующих срезах (k)': 'Confirmations in later slices (k)',
    'Исходная площадь, км²': 'Original area, km²', 'Согласованная площадь, км²': 'Consistent area, km²', 'Прирост, км²': 'Growth, km²', 'Статус подтверждения': 'Confirmation status',
    'Результат (полигоны застройки)': 'Result (built-up polygons)', 'Эталон (здания или карта застройки)': 'Reference (buildings or built-up map)', 'Тип эталона': 'Reference type',
    'Растр-сетка (например NDBI)': 'Grid raster (e.g. NDBI)', 'Буфер застройки вокруг зданий, м': 'Built-up buffer around buildings, m', 'Фон — дальше от зданий, чем, м': 'Background — farther from buildings than, m',
    'Водные объекты (исключить из фона)': 'Water bodies (exclude from background)', 'Размер клетки шахматки, м (0 — без шахматки)': 'Checkerboard cell size, m (0 — none)', 'Папка отчёта': 'Report folder',
    'Urban Auto — сравнение с WSF по точкам': 'Urban Auto — compare with WSF at points', 'Urban Auto: бинарная карта 0/1': 'Urban Auto: binary map 0/1',
    'WSF: подготовленная бинарная карта 0/1': 'WSF: prepared binary map 0/1', 'Независимые контрольные точки': 'Independent check points', 'Поле эталона (0/1)': 'Reference field (0/1)',
    'Urban Auto — скачать WSF Tracker по области карты': 'Urban Auto — download WSF Tracker for the map extent', 'Растр Urban Auto (границы и сетка результата)': 'Urban Auto raster (result extent and grid)',
    'Год WSF Tracker': 'WSF Tracker year', 'Папка для WSF': 'WSF folder', 'Коды класса застройки WSF (пусто = не создавать бинарную маску)': 'WSF built-up class codes (empty = no binary mask)',
}


def language():
    forced = os.environ.get('URBANAUTO_LANG', '').lower()
    if forced in ('ru', 'en'):
        return forced
    try:
        from qgis.PyQt.QtCore import QSettings, QLocale
        loc = QSettings().value('locale/userLocale', '') or QLocale.system().name()
        if QSettings().value('locale/overrideFlag', False) in (False, 'false') and not loc:
            loc = QLocale.system().name()
    except Exception:
        loc = os.environ.get('LANG', 'ru')
    return 'ru' if str(loc).lower().startswith('ru') else 'en'


def tr(text):
    """Перевод строки интерфейса; без перевода — исходный текст."""
    if not isinstance(text, str) or language() == 'ru':
        return text
    return EN.get(text, text)


def translate_widget(root):
    """Подставляет переводы во все подписи виджета и его потомков (вызывается после построения окна)."""
    if language() == 'ru':
        return
    from qgis.PyQt.QtWidgets import (QWidget, QAbstractButton, QLabel, QGroupBox, QComboBox, QTabWidget, QLineEdit,
                                     QAbstractSpinBox)
    widgets = [root] + root.findChildren(QWidget)
    for w in widgets:
        if w.windowTitle():
            w.setWindowTitle(tr(w.windowTitle()))
        if w.toolTip():
            w.setToolTip(tr(w.toolTip()))
        if isinstance(w, (QAbstractButton, QLabel)) and w.text():
            w.setText(tr(w.text()))
        if isinstance(w, QGroupBox) and w.title():
            w.setTitle(tr(w.title()))
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                w.setItemText(i, tr(w.itemText(i)))
        if isinstance(w, QTabWidget):
            for i in range(w.count()):
                w.setTabText(i, tr(w.tabText(i)))
        if isinstance(w, QLineEdit) and w.placeholderText():
            w.setPlaceholderText(tr(w.placeholderText()))
        if isinstance(w, QAbstractSpinBox) and getattr(w, 'specialValueText', None) and w.specialValueText():
            w.setSpecialValueText(tr(w.specialValueText()))


def translate_algorithm(alg):
    """Переводит подписи параметров и название алгоритма Processing."""
    if language() == 'ru':
        return
    for p in alg.parameterDefinitions():
        p.setDescription(tr(p.description()))
        if hasattr(p, 'options') and hasattr(p, 'setOptions'):
            try:
                p.setOptions([tr(o) for o in p.options()])
            except Exception:
                pass
    for o in alg.outputDefinitions():
        o.setDescription(tr(o.description()))


def translate_class(cls):
    """Оборачивает displayName/group класса алгоритма переводом (один раз)."""
    if getattr(cls, '_urbanauto_tr', False):
        return cls
    for name in ('displayName', 'group'):
        f = getattr(cls, name, None)
        if f is not None:
            setattr(cls, name, (lambda f: lambda self: tr(f(self)))(f))
    init = getattr(cls, 'initAlgorithm', None)
    if init is not None:
        def initAlgorithm(self, config=None, _init=init):
            _init(self, config)
            translate_algorithm(self)
        cls.initAlgorithm = initAlgorithm
    cls._urbanauto_tr = True
    return cls
