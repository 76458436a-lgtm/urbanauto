# Urban Auto 5.5.1 – модуль QGIS

Годовые карты застройки и рост городов по снимкам Landsat без ручных точек.
*Annual built-up maps and urban growth from Landsat, without manual training points (English below).*

## Что делает

Достаточно указать город или нарисовать полигон. Дальше модуль:

1. скачивает летние композиты Landsat 5/7/8/9 за каждый год (по желанию – Sentinel-2) из Microsoft Planetary Computer;
2. строит обучающие метки там, где сходятся открытые продукты: GHSL, WSF, ESA WorldCover, JRC GSW, здания OpenStreetMap и Microsoft;
3. обучает модель (градиентный бустинг или Random Forest) на четыре класса: растительность, застройка, открытый грунт, вода;
4. определяет год появления застройки в каждом пикселе и считает площади по годам;
5. записывает растры, слои GeoPackage, таблицу `annual_series.csv` и HTML-отчёт с перечнем источников.

Не каждый год даёт хороший снимок. Бывает, что от лета остаётся одна сцена Landsat 7 с полосами. Такие годы помечаются
в отчёте, их «сырые» площади не выводятся, а при желании их можно исключить параметром
«Исключать снимок, если покрытие меньше, %».

## Установка

1. QGIS 3.34 LTR или новее.
2. **Библиотеки Python.** Модулю нужны `scikit-learn`, `scipy` и `requests`. Если чего-то не хватает, модуль скажет об этом при запуске.
   * Windows: закройте QGIS, откройте **OSGeo4W Shell** (меню Пуск → QGIS) и выполните
     `python -m pip install scikit-learn scipy requests`.
   * macOS: `/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install scikit-learn scipy requests`.
   * Ubuntu/Debian: `sudo apt install python3-sklearn python3-scipy python3-requests`.
3. **Модули → Управление и установка → Установить из ZIP** → `urbanauto-5.5.1.zip`.
4. Если раньше стояли `urban_auto_exp54`, `UrbanAutoCalibrator` или `ekyla*`, отключите их, иначе они будут мешать.
   Модуль сам предупредит, если они включены. Кэш прежней версии (`~/.urban_auto_exp54_cache`) подхватывается автоматически.

С версии 5.5.1 идентификатор модуля – `urbanauto`, инструменты Processing называются `urbanauto:rf_series`,
`urbanauto:label_points` и т. д. Если в ваших скриптах и моделях остались старые имена `urban_auto_exp54:*`, замените префикс.

## Быстрый старт

Откройте **Модули → Urban Auto → Urban Auto – открыть модуль**.

1. Оставьте режим «Автоматически» и выберите профиль:
   * **Строгий** – только здания и покрытия (по умолчанию);
   * **Высокая полнота (hgb_nt)** – кварталы целиком, с дворами и зеленью.

   Какой профиль точнее, зависит от плотности застройки. Модуль оценит её по GHSL и подскажет в журнале и отчёте.
2. Введите город **вместе с регионом** («Белогорск, Амурская область»): одноимённых городов больше, чем кажется.
   Можно также выбрать полигон или текущий вид карты.
3. Задайте годы (минимум три) и папку, затем нажмите «Запустить расчёт». Один город считается 5–15 минут.

## Результаты

| Файл | Содержание |
|---|---|
| `BuiltYear.tif` | год появления застройки (0 – не застроено) |
| `BuiltYear_confidence.tif`, `Prob_built_by_year.tif` | уверенность и вероятности по годам |
| `Built_fraction_by_year.tif` | доля застроенной поверхности в пикселе (строгий профиль) |
| `Urban_<год>.gpkg`, `New_built_<годы>.gpkg` | контуры застройки и пятна новой застройки |
| `annual_series.csv` | площади по годам; столбцы `coverage_pct` и `quality` – покрытие и состояние снимка |
| `report.html`, `rf_series_protocol.json` | отчёт и полный протокол параметров |

Новая застройка засчитывается, только если её подтверждают три года наблюдений. Так модуль не принимает за стройку
случайный всплеск на одном снимке. Обратная сторона: прирост последних двух лет даёт лишь нижнюю оценку, и отчёт
об этом предупреждает. Предварительную оценку можно включить параметром «Показывать предварительный прирост последних лет».

## Точность: что известно

Проверка 4 ноября 2026 г. проведена на 16 размеченных городах. Метки этих городов известны, но при проверке они
исключались из обучения вместе с окрестностью. Простое среднее за 2025 год:

| Профиль | Полнота (PA) | Точность пользователя (UA) | Оба показателя > 90 % |
|---|---:|---:|---|
| Строгий | 84,2 % | 82,5 % | Ханты-Мансийск |
| Высокая полнота | 91,6 % | 73,4 % | Урай |

* Это не слепая проверка: часть точек раньше использовалась при разработке. Протокол независимой проверки на новых
  городах лежит в папке `blind_check/` репозитория.
* Точность выше 0,9 на любой территории **не гарантируется**. Если результат нужен для выводов, разметьте 50–100 точек
  (инструмент «Точки для разметки» → «для проверки точности») и передайте их как «Точки проверки».
  Тогда отчёт покажет κ, общую точность и площадь с 95 % доверительным интервалом.
* Слабое место – малые города с частным сектором. Профиль «Высокая полнота» там завышает площадь,
  принимая огороды и сады за застройку.

## Источники данных и лицензии

Landsat – USGS (общественное достояние); Sentinel-2 – Copernicus/ESA; GHSL – EC JRC (CC BY 4.0);
WSF – DLR (CC BY 4.0); ESA WorldCover (CC BY 4.0); JRC Global Surface Water (CC BY 4.0);
OpenStreetMap и Nominatim – © участники OSM (ODbL); Microsoft Building Footprints (ODbL).
Публикуя карты, указывайте использованные источники. Отчёт модуля перечисляет их за вас.

К Nominatim модуль обращается не чаще раза в секунду и кэширует ответы, как того требуют правила OSM.

## Лицензия

GPL-2.0-or-later. © 2025–2026 Руслан Каленов (Ekyla). Ошибки и предложения:
https://github.com/76458436a-lgtm/urbanauto/issues

---

## English

**Urban Auto** builds a built-up map for every year of a period from a city name or a polygon.
It downloads summer Landsat composites, derives training labels from open products (GHSL, WSF, ESA WorldCover, JRC GSW,
OSM and Microsoft buildings), trains a gradient boosting model, detects the year built-up land appeared and writes rasters,
GeoPackages, a CSV series and an HTML report with data attribution.

* **Install:** QGIS ≥ 3.34; install `scikit-learn scipy requests` into the QGIS Python (Windows: OSGeo4W Shell →
  `python -m pip install scikit-learn scipy requests`); then *Plugins → Install from ZIP*.
* **Language:** the window, menus and Processing tools follow the QGIS language (English or Russian). Logs and the HTML report are in Russian.
* **Profiles:** *Strict* (buildings and paved surfaces) or *High recall* (whole blocks with yards). A hint based on GHSL density suggests which one usually fits.
* **Quality control:** composites with low coverage are flagged and can optionally be excluded by a coverage threshold.
* **Accuracy:** on 16 labelled Russian towns (not a blind test), mean 2025 recall/user’s accuracy is 84/82 % (strict) and
  92/73 % (high recall). Accuracy elsewhere is not guaranteed. Label 50–100 check points, and the report will give
  kappa, overall accuracy and area with a 95 % CI (Olofsson et al., 2014).
* **Processing ids** changed in 5.5.1: `urban_auto_exp54:*` → `urbanauto:*`.
