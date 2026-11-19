"""Слепая независимая проверка Urban Auto (протокол 5.5.1).

Шаги (каждый — отдельная команда, запускать из OSGeo4W Shell / Python QGIS):

  1. python blind_check.py register --plugin-zip ПУТЬ\\UrbanAuto_5.5.1.zip --seed ЧИСЛО
     Выбирает города, фиксирует версию (SHA-256 архива), параметры и объём выборки
     в preregistration.json. Файл и его хеш нужно опубликовать (коммит/письмо) ДО расчётов.
  2. python blind_check.py run --out ПАПКА
     Для каждого города запускает urbanauto:rf_series в двух профилях (строгий и hgb_nt).
  3. python blind_check.py points --out ПАПКА
     Строит стратифицированную выборку по согласию двух карт 2025 г. и пишет таблицу
     для разметки БЕЗ классов карты (Разметка.csv) и KML. Ключ страт — в strata_key.json.
  4. Разметчик заполняет столбец label (1 — застройка, 0 — нет, пусто — не определить)
     по снимку высокого разрешения 2025 г., не открывая карты модуля.
  5. python blind_check.py score --out ПАПКА
     Считает для каждого профиля OA, PA, UA, κ и площадь застройки с 95 % ДИ (Olofsson et al., 2014).

Определение класса для разметчика — «территория застройки»: здания, дворы, проезды и покрытия
внутри жилых и производственных кварталов; огороды, луга, лес, пашня, вода — не застройка.
Одно и то же определение используется для обоих профилей; различие профилей — предмет проверки.
"""
import argparse
import csv
import datetime
import hashlib
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = {
    'strict': dict(DEFINITION=0, MODEL=0, TEMPORAL=True, WEIGHT_OBS=True, FRACTION=True),
    'hgb_nt': dict(DEFINITION=1, MODEL=1, TEMPORAL=False, WEIGHT_OBS=False, FRACTION=False),
}
COMMON = dict(START_YEAR=2000, END_YEAR=2025, RESOLUTION=30, USE_S2=False, SENTINEL_SERIES=False, MIN_RUN=3, MIN_AREA_HA=0.5,
              REPORT_YEARS='2000,2010,2025', ADD_LAYERS=False, CALIBRATE=False, ADAPTIVE_EDGE=False, MIN_COVERAGE=0)
STRATA = ('both', 'strict_only', 'hgb_only', 'near', 'far')        # согласие карт 2025 г.
ALLOC = dict(both=25, strict_only=15, hgb_only=15, near=25, far=20)  # точек на город (всего 100)
NEAR_M = 1000


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter=';'))


def write_csv(path, rows, fields):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fields, delimiter=';', extrasaction='ignore')
        w.writeheader(); w.writerows(rows)


# ----------------------------------------------------------------------------- 1. регистрация
def register(a):
    cand = read_csv(os.path.join(HERE, 'candidates.csv'))
    rng = random.Random(a.seed)
    zones = sorted({c['zone'] for c in cand})
    picked = []
    for z in zones:                                   # по одному из каждой зоны, затем добор случайно
        picked.append(rng.choice([c for c in cand if c['zone'] == z]))
    rest = [c for c in cand if c not in picked]
    picked += rng.sample(rest, max(0, a.n - len(picked)))
    reg = dict(protocol='Urban Auto blind check 5.5.1', created=datetime.datetime.now().isoformat(timespec='seconds'),
               seed=a.seed, plugin_zip=os.path.basename(a.plugin_zip), plugin_sha256=sha256(a.plugin_zip),
               cities=[dict(name=c['city'], region=c['region'], zone=c['zone']) for c in picked[:a.n]],
               profiles=PROFILES, common=COMMON, strata=STRATA, allocation=ALLOC, near_m=NEAR_M,
               reference_year=2025, class_definition='территория застройки (кварталы с дворами и проездами)',
               rule='Параметры, города и выборка не меняются после регистрации; все города входят в итог, включая неудачные.')
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, 'preregistration.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
    print('Зарегистрировано:', ', '.join(c['name'] for c in reg['cities']))
    print('SHA-256 регистрации (опубликуйте до расчётов):', sha256(p))


# ----------------------------------------------------------------------------- 2. расчёт
def run(a):
    reg = json.load(open(os.path.join(a.out, 'preregistration.json'), encoding='utf-8'))
    from qgis.core import QgsApplication
    app = QgsApplication.instance()
    own = app is None
    if own:
        app = QgsApplication([], False); app.initQgis()
    sys.path.insert(0, os.path.join(QgsApplication.pkgDataPath(), 'python', 'plugins'))
    import processing
    from processing.core.Processing import Processing
    Processing.initialize()
    if QgsApplication.processingRegistry().providerById('urbanauto') is None:
        from urbanauto.urban_auto_provider import UrbanAutoProvider
        QgsApplication.processingRegistry().addProvider(UrbanAutoProvider())
    status = {}
    for c in reg['cities']:
        for prof, opts in reg['profiles'].items():
            dest = os.path.join(a.out, c['name'], prof)
            if os.path.exists(os.path.join(dest, 'BuiltYear.tif')):
                status[f"{c['name']}/{prof}"] = 'есть'; continue
            os.makedirs(dest, exist_ok=True)
            comp = os.path.join(a.out, c['name'], 'composites')     # общие композиты для двух профилей
            params = dict(reg['common'], **opts, CITY=f"{c['name']}, {c['region']}", COMPOSITES=comp, OUTPUT_FOLDER=dest)
            try:
                processing.run('urbanauto:rf_series', params)
                status[f"{c['name']}/{prof}"] = 'ok'
            except Exception as ex:                                  # неудача тоже результат протокола
                status[f"{c['name']}/{prof}"] = 'ошибка: ' + str(ex)[:300]
            print(c['name'], prof, status[f"{c['name']}/{prof}"], flush=True)
            json.dump(status, open(os.path.join(a.out, 'run_status.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if own:
        app.exitQgis()


# ----------------------------------------------------------------------------- 3. точки
def _maps(folder):
    import numpy as np
    from osgeo import gdal
    out = {}
    for prof in PROFILES:
        ds = gdal.Open(os.path.join(folder, prof, 'BuiltYear.tif'))
        yr = ds.ReadAsArray()
        out[prof] = yr
        gt, wkt, nod = ds.GetGeoTransform(), ds.GetProjection(), ds.GetRasterBand(1).GetNoDataValue()
    valid = np.ones_like(out['strict'], bool)
    for v in out.values():
        valid &= v != (nod if nod is not None else -1)
    return out, valid, gt, wkt


def strata_map(folder, year=2025):
    import numpy as np
    from scipy import ndimage as ndi
    maps, valid, gt, wkt = _maps(folder)
    s = (maps['strict'] > 0) & (maps['strict'] <= year)
    h = (maps['hgb_nt'] > 0) & (maps['hgb_nt'] <= year)
    from osgeo import osr
    sr = osr.SpatialReference(); sr.ImportFromWkt(wkt)
    if sr.IsGeographic():
        lat = gt[3] + s.shape[0] / 2 * gt[5]
        sampling = (abs(gt[5]) * 110574.0, abs(gt[1]) * 111320.0 * math.cos(math.radians(lat)))
    else:
        sampling = (abs(gt[5]) * sr.GetLinearUnits(), abs(gt[1]) * sr.GetLinearUnits())
    dist = ndi.distance_transform_edt(~(s | h), sampling=sampling)
    st = np.full(s.shape, -1, np.int8)
    st[s & h] = 0; st[s & ~h] = 1; st[~s & h] = 2
    st[~s & ~h & (dist <= NEAR_M)] = 3; st[~s & ~h & (dist > NEAR_M)] = 4
    st[~valid] = -1
    return st, s, h, gt, wkt


def points(a):
    import numpy as np
    from osgeo import osr
    reg = json.load(open(os.path.join(a.out, 'preregistration.json'), encoding='utf-8'))
    rng = np.random.default_rng(reg['seed'])
    sheet, key, kml = [], {}, []
    for c in reg['cities']:
        folder = os.path.join(a.out, c['name'])
        if not all(os.path.exists(os.path.join(folder, p, 'BuiltYear.tif')) for p in PROFILES):
            print('пропуск (нет карт):', c['name']); continue
        st, s, h, gt, wkt = strata_map(folder)
        src = osr.SpatialReference(); src.ImportFromWkt(wkt)
        if src.IsGeographic():                       # площадь пикселя по широте строки
            lat_r = gt[3] + (np.arange(st.shape[0]) + 0.5) * gt[5]
            px = (abs(gt[1]) * 111.320 * np.cos(np.radians(lat_r)) * abs(gt[5]) * 110.574)[:, None] * np.ones(st.shape)
        else:
            px = np.full(st.shape, abs(gt[1] * gt[5]) / 1e6 * src.GetLinearUnits() ** 2)
        dst = osr.SpatialReference(); dst.ImportFromEPSG(4326)
        for x in (src, dst):
            x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(src, dst)
        key[c['name']] = dict(areas_km2={STRATA[k]: float(px[st == k].sum()) for k in range(5)})
        for k, name in enumerate(STRATA):
            idx = np.argwhere(st == k)
            n = min(ALLOC[name], len(idx))
            for r, cc in idx[rng.choice(len(idx), n, replace=False)] if n else []:
                x = gt[0] + (cc + 0.5) * gt[1]; y = gt[3] + (r + 0.5) * gt[5]
                lon, lat, _ = tr.TransformPoint(x, y)
                pid = f"{c['name'][:3].upper()}-{len(sheet) + 1:04d}"
                sheet.append(dict(id=pid, city=c['name'], lon=f'{lon:.6f}', lat=f'{lat:.6f}', year=2025, label='', comment=''))
                key[pid] = dict(city=c['name'], stratum=name, strict=int(s[r, cc]), hgb_nt=int(h[r, cc]))
                kml.append(f'<Placemark><name>{pid}</name><Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point></Placemark>')
    order = list(range(len(sheet))); random.Random(reg['seed']).shuffle(order)     # порядок без связи со стратами
    sheet = [sheet[i] for i in order]
    write_csv(os.path.join(a.out, 'Разметка.csv'), sheet, ['id', 'city', 'lon', 'lat', 'year', 'label', 'comment'])
    with open(os.path.join(a.out, 'Разметка.kml'), 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                + ''.join(kml) + '</Document></kml>')
    with open(os.path.join(a.out, 'strata_key.json'), 'w', encoding='utf-8') as f:
        json.dump(key, f, ensure_ascii=False, indent=1)
    print(f'Точек: {len(sheet)}. Разметчику передайте только Разметка.csv и Разметка.kml; strata_key.json не открывать.')


# ----------------------------------------------------------------------------- 5. подсчёт
def olofsson(rows, areas, prof):
    """rows: [(stratum, map01, ref01)], areas: {stratum: km²}. Оценка по стратам, не совпадающим с классами карты."""
    tot = sum(areas.values())
    P = [[0.0, 0.0], [0.0, 0.0]]
    var_area = 0.0
    for name, A in areas.items():
        rs = [(m, r) for s_, m, r in rows if s_ == name]
        if not rs or A <= 0:
            continue
        W, n = A / tot, len(rs)
        for m, r in rs:
            P[m][r] += W / n
        ref = [r for _, r in rs]
        mean = sum(ref) / n
        s2 = sum((x - mean) ** 2 for x in ref) / (n - 1) if n > 1 else 0.0
        var_area += W ** 2 * s2 / n
    oa = P[0][0] + P[1][1]
    pm, pr = P[1][0] + P[1][1], P[0][1] + P[1][1]
    pe = pm * pr + (1 - pm) * (1 - pr)
    return dict(profile=prof, n=len(rows), OA=oa, UA=P[1][1] / pm if pm else None, PA=P[1][1] / pr if pr else None,
                kappa=(oa - pe) / (1 - pe) if pe < 1 else None, map_km2=pm * tot, ref_km2=pr * tot,
                ref_ci95_km2=1.96 * math.sqrt(var_area) * tot)


def score(a):
    key = json.load(open(os.path.join(a.out, 'strata_key.json'), encoding='utf-8'))
    lab = {r['id']: r['label'].strip() for r in read_csv(a.labels or os.path.join(a.out, 'Разметка.csv'))}
    res, unknown = [], 0
    for city in sorted({v['city'] for k, v in key.items() if 'stratum' in v}):
        for prof in PROFILES:
            rows = []
            for pid, v in key.items():
                if 'stratum' not in v or v['city'] != city:
                    continue
                l = lab.get(pid, '')
                if l not in ('0', '1'):
                    unknown += prof == 'strict'; continue
                rows.append((v['stratum'], v[prof], int(l)))
            if rows:
                m = olofsson(rows, key[city]['areas_km2'], prof); m['city'] = city; res.append(m)
    fields = ['city', 'profile', 'n', 'OA', 'PA', 'UA', 'kappa', 'map_km2', 'ref_km2', 'ref_ci95_km2']
    write_csv(os.path.join(a.out, 'Итог_по_городам.csv'), [{k: (f'{v:.4f}' if isinstance(v, float) else v) for k, v in r.items()} for r in res], fields)
    summary = {}
    for prof in PROFILES:
        rr = [r for r in res if r['profile'] == prof and r['kappa'] is not None]
        if rr:
            summary[prof] = {k: round(sum(r[k] for r in rr if r[k] is not None) / len(rr), 4) for k in ('OA', 'PA', 'UA', 'kappa')}
            summary[prof]['cities'] = len(rr)
            summary[prof]['area_ratio_median'] = round(sorted(r['map_km2'] / r['ref_km2'] for r in rr if r['ref_km2'])[len(rr) // 2], 3)
    summary['unlabeled_points'] = unknown
    json.dump(summary, open(os.path.join(a.out, 'Итог.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('register'); r.add_argument('--plugin-zip', required=True); r.add_argument('--seed', type=int, required=True)
    r.add_argument('--n', type=int, default=10); r.add_argument('--out', default=os.path.join(HERE, 'Прогон'))
    for name in ('run', 'points', 'score'):
        p = sub.add_parser(name); p.add_argument('--out', default=os.path.join(HERE, 'Прогон'))
        if name == 'score':
            p.add_argument('--labels', help='Заполненная таблица (по умолчанию Разметка.csv в папке)')
    a = ap.parse_args(argv)
    dict(register=register, run=run, points=points, score=score)[a.cmd](a)


if __name__ == '__main__':
    main()
