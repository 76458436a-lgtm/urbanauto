# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Классификация застройки по годовому ряду без ручных точек (версия 5.0; схема с 4.4).

Схема:
1. Автоматические обучающие метки на каждый год (`auto_labels`). Застройка — там, где сходятся
   независимые продукты (GHSL BUILT-S, WSF, ESA WorldCover, здания OSM) с учётом года. Фон —
   там, где все продукты согласны, что застройки нет. Фон делится по спектру композита на
   растительность, воду и открытый грунт (песок, карьеры, пустыри, отвалы).
2. Признаки (`features`): каналы и индексы года, текстура окрестности, устойчивость пикселя
   во всём ряду (минимум/размах NDVI, доля «голых» лет). Признаки ряда отличают дом, который
   стоит годами, от пляжа или карьера, у которых спектр «прыгает».
3. Один Random Forest на все годы (`train`), четыре класса: 0 растительность, 1 застройка,
   2 открытый грунт, 3 вода. Метки 220 проверочных точек в обучение не попадают.
4. Год появления застройки (`change_year`): по ряду вероятностей подбирается одна ступенька
   «не застроено → застроено» с наименьшей ошибкой; застройка не исчезает. Пиксель считается
   застроенным, только если ступенька подтверждена не менее чем `min_run` годами.

5.0: многомасштабная текстура и плотность границ; сходство пикселя с его же конечным состоянием
(помогает датировать старую застройку); признаки Sentinel-2 10 м внутри пикселя 30 м (с 2018 г.);
здания Microsoft в метках; модель HistGradientBoosting (быстрее RF, при отсутствии — RF);
доля застроенной поверхности (регрессия) и год появления с весом наблюдений.

Модуль не зависит от QGIS: numpy, scipy, scikit-learn.
"""
import numpy as np

CLASSES = ('vegetation', 'built', 'bare', 'water')
VEG, BUILT, BARE, WATER = 0, 1, 2, 3
NODATA = -9999.0
# то же, что urban_auto_imagery.S2_FEATURES (без импорта — модуль работает и вне пакета)
S2_FEATURES = ('s2_ndvi', 's2_ndvi_sd', 's2_ndbi', 's2_mndwi', 's2_bright', 's2_bright_sd',
               's2_imperv_frac', 's2_veg_frac', 's2_edge', 's2_nir_sd')


# ----------------------------------------------------------------------------- признаки
def _nd(a, b):
    with np.errstate(all='ignore'):
        return (a - b) / (a + b)


def spectral(comp):
    """(>=6,H,W) отражение → dict каналов и индексов, NaN вне данных."""
    a = np.asarray(comp[:6], np.float32).copy()
    a[(a == NODATA) | ~np.isfinite(a)] = np.nan
    blue, green, red, nir, sw1, sw2 = a
    with np.errstate(all='ignore'):
        return dict(blue=blue, green=green, red=red, nir=nir, swir1=sw1, swir2=sw2,
                    ndvi=_nd(nir, red), ndbi=_nd(sw1, nir), mndwi=_nd(green, sw1),
                    bsi=_nd(sw1 + red, nir + blue), nbr=_nd(nir, sw2),
                    swir_ratio=sw2 / sw1, bright=(blue + green + red) / 3)


def _box(a, r):
    """Скользящее среднее (2r+1)² с учётом NaN."""
    from scipy import ndimage as ndi
    v = np.isfinite(a)
    s = ndi.uniform_filter(np.where(v, a, 0).astype(np.float64), 2 * r + 1, mode='nearest')
    c = ndi.uniform_filter(v.astype(np.float64), 2 * r + 1, mode='nearest')
    with np.errstate(all='ignore'):
        return (s / c).astype(np.float32)


def nan_quantiles(stack, qs):
    """Квантили по оси 0 без учёта NaN (линейная интерполяция, как numpy.nanpercentile), в 10–20 раз быстрее:
    одна сортировка вместо отдельного прохода на каждый квантиль."""
    a = np.sort(np.asarray(stack, np.float32), 0)          # NaN уходят в конец
    n = np.isfinite(a).sum(0)
    out = []
    for q in qs:
        pos = np.clip((n - 1) * q, 0, None)
        lo = np.floor(pos).astype(np.int64); hi = np.minimum(lo + 1, np.maximum(n - 1, 0))
        w = (pos - lo).astype(np.float32)
        vlo = np.take_along_axis(a, lo[None], 0)[0]; vhi = np.take_along_axis(a, hi[None], 0)[0]
        v = vlo + (vhi - vlo) * w
        v[n == 0] = np.nan
        out.append(v.astype(np.float32))
    return out


def series_context(spectra, last_n=3):
    """Признаки устойчивости пикселя во всём ряду. spectra: dict год → spectral().

    5.0: «конечное состояние» — медиана каналов за последние last_n лет; признаки года сравнивают пиксель
    с ним (дом, который стоял и в 2000 г., спектрально похож на себя в 2025 г.)."""
    ys = sorted(spectra)
    ndvi = np.stack([spectra[y]['ndvi'] for y in ys])
    ndbi = np.stack([spectra[y]['ndbi'] for y in ys])
    mndwi = np.stack([spectra[y]['mndwi'] for y in ys])
    import warnings
    with np.errstate(all='ignore'), warnings.catch_warnings():
        warnings.simplefilter('ignore')
        bare_years = np.nanmean(((ndvi < 0.3) & (mndwi < 0)).astype(np.float32) +
                                np.where(np.isfinite(ndvi), 0, np.nan), 0)
        q25, q50, q75 = nan_quantiles(ndvi, (0.25, 0.5, 0.75))
        ctx = dict(ndvi_min=np.nanmin(ndvi, 0), ndvi_max=np.nanmax(ndvi, 0),
                   ndvi_med=q50, ndvi_iqr=q75 - q25,
                   ndbi_med=nan_quantiles(ndbi, (0.5,))[0], ndbi_max=np.nanmax(ndbi, 0),
                   mndwi_max=np.nanmax(mndwi, 0), bare_years=bare_years.astype(np.float32))
        tail = ys[-last_n:]
        ctx['_last'] = {b: nan_quantiles(np.stack([spectra[y][b] for y in tail]), (0.5,))[0]
                        for b in ('blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'ndvi', 'ndbi')}
    return {k: (v.astype(np.float32) if isinstance(v, np.ndarray) else v) for k, v in ctx.items()}


def _edges(a):
    """Модуль градиента (Собель) — плотность границ: высокая у кварталов, низкая у полей и воды."""
    from scipy import ndimage as ndi
    z = np.nan_to_num(a)
    g = np.hypot(ndi.sobel(z, 0), ndi.sobel(z, 1)).astype(np.float32)
    g[~np.isfinite(a)] = np.nan
    return g


def features(spec, context, s2=None, temporal=True):
    """Признаки одного года: (H, W, K) float32 и список имён.

    s2 — (len(S2_FEATURES), H, W) признаки Sentinel-2 этого года или None."""
    layers = dict(spec)
    layers['ndvi_m5'] = _box(spec['ndvi'], 2)
    layers['ndbi_m5'] = _box(spec['ndbi'], 2)
    layers['ndbi_m9'] = _box(spec['ndbi'], 4)           # 5.0: 270 м — квартал целиком
    layers['ndvi_m9'] = _box(spec['ndvi'], 4)
    with np.errstate(all='ignore'):
        layers['ndvi_sd3'] = np.sqrt(np.maximum(_box(spec['ndvi'] ** 2, 1) - _box(spec['ndvi'], 1) ** 2, 0))
        layers['bright_sd3'] = np.sqrt(np.maximum(_box(spec['bright'] ** 2, 1) - _box(spec['bright'], 1) ** 2, 0))
        layers['ndvi_sd5'] = np.sqrt(np.maximum(_box(spec['ndvi'] ** 2, 2) - _box(spec['ndvi'], 2) ** 2, 0))
        e = _edges(spec['bright'])
        layers['edge'] = e
        layers['edge_m5'] = _box(e, 2)
        layers['ndvi_dev'] = spec['ndvi'] - context['ndvi_med']
        if temporal and '_last' in context:
            L = context['_last']
            d = sum((spec[b] - L[b]) ** 2 for b in ('blue', 'green', 'red', 'nir', 'swir1', 'swir2'))
            layers['dist_last'] = np.sqrt(d)
            layers['ndvi_vs_last'] = spec['ndvi'] - L['ndvi']
            layers['ndbi_vs_last'] = spec['ndbi'] - L['ndbi']
    layers.update({k: v for k, v in context.items() if not k.startswith('_')})
    if s2 is not None:
        for i, n in enumerate(S2_FEATURES):
            layers[n] = s2[i]
    names = list(layers)
    return np.stack([layers[n] for n in names], -1).astype(np.float32), names


# ----------------------------------------------------------------------------- метки
def interp_epochs(epochs, year):
    """epochs: dict год → массив; линейная интерполяция (за краями — ближайшая эпоха)."""
    ys = sorted(epochs)
    if year <= ys[0]:
        return epochs[ys[0]]
    if year >= ys[-1]:
        return epochs[ys[-1]]
    for a, b in zip(ys, ys[1:]):
        if a <= year <= b:
            w = (year - a) / (b - a) if b != a else 0
            return (1 - w) * epochs[a] + w * epochs[b]


def auto_labels(year, spec, src, *, ghsl_pos=0.05, ghsl_neg=0.01, osm_dist_px=None, protect=None,
                bld_frac_min=None, green_neg=False, sharp_now=False, green_ndvi=0.4):
    """Метки одного года: int8, -1 = не размечено.

    src — словарь источников на сетке композита (доли 0–1, NaN = нет данных):
      ghsl:  dict эпоха → доля застройки GHSL BUILT-S (обязательно);
      wsf:   dict год → 0/1 «застроено к этому году» (WSF evolution, 1985–2015), необязательно;
      wsf2019, esa (dict год → доля built), osm (0/1 здания, современное состояние) — необязательно;
      water_occ — повторяемость воды JRC, % (необязательно).
    Застройка: GHSL ≥ ghsl_pos и подтверждение хотя бы одним независимым продуктом, ближайшим по году.
    Фон: GHSL < ghsl_neg во всех эпохах до года + 5 лет и ни один продукт не видит застройку.

    5.1 (резкие метки):
      bld_frac_min — здания берутся как доля площади пикселя (OSM + Microsoft, src['bfrac']) не меньше порога,
        а не «пиксель задет контуром»: огород рядом с домом больше не учится как застройка;
      green_neg — зелень в посёлке (GHSL > 0, но ни ESA, ни WSF 2019 (10 м), ни контуры зданий застройки
        не видят, NDVI ≥ green_ndvi) учится как растительность — парки, огороды, газоны между домами;
      sharp_now — застройка любого года должна подтверждаться резкими продуктами 2019–2021 (ESA/WSF 2019 ≥ 0,3
        или доля зданий): пиксели, «размазанные» GHSL 90 м, из меток застройки исключаются.
    """
    H, W = spec['ndvi'].shape
    lab = np.full((H, W), -1, np.int8)
    ok = np.isfinite(spec['ndvi']) & np.isfinite(spec['mndwi'])
    g = np.nan_to_num(interp_epochs(src['ghsl'], year))
    g_future = np.nan_to_num(interp_epochs(src['ghsl'], year + 5))
    confirm = np.zeros((H, W), bool)
    any_built = np.zeros((H, W), bool)
    wsf = src.get('wsf') or {}
    if wsf:
        past = [y for y in wsf if y <= year]
        if past and year - max(past) <= 5:
            confirm |= np.nan_to_num(wsf[max(past)]) > 0.5
        for y, a in wsf.items():                     # что WSF видит хоть когда-то до year+5
            if y <= year + 5:
                any_built |= np.nan_to_num(a) > 0.5
    if src.get('wsf2019') is not None and 2016 <= year <= 2023:
        confirm |= np.nan_to_num(src['wsf2019']) > 0.5
    if year >= 2016:
        any_built |= np.nan_to_num(src.get('wsf2019', np.zeros((H, W)))) > 0.2
    esa = src.get('esa') or {}
    for y, a in esa.items():
        if abs(y - year) <= 3:
            confirm |= np.nan_to_num(a) > 0.5
        if y <= year + 5:
            any_built |= np.nan_to_num(a) > 0.2
    osm = buildings_mask(src, bld_frac_min=bld_frac_min)   # 5.0: здания OSM ∪ Microsoft; 5.1: по доле площади
    if osm is not None and year >= 2018:
        confirm |= osm
    if osm is not None and year >= 2015:
        any_built |= osm
    built = ok & (g >= ghsl_pos) & confirm
    sharp = sharp_built_now(src, bld_frac_min or 0.05)
    if sharp_now and sharp is not None:
        built &= sharp
    from scipy import ndimage as ndi
    roads = src.get('roads')
    bld_near = ndi.binary_dilation(osm, iterations=1) if osm is not None else np.zeros((H, W), bool)
    road_only = (roads > 0) & ~bld_near if roads is not None else np.zeros((H, W), bool)
    built &= ~road_only            # 4.5: дорога без зданий рядом — не застройка
    # фон: все молчат (с запасом в 5 лет вперёд — чтобы не учить «будущие дома» как фон раньше времени)
    near_built = ndi.binary_dilation(any_built | (g_future >= ghsl_neg) | built, iterations=2)
    background = ok & (g < ghsl_neg) & (g_future < ghsl_neg) & ~near_built
    lab[built] = BUILT
    occ = src.get('water_occ')
    water = background & ((spec['mndwi'] > 0.1) | ((np.nan_to_num(occ) >= 75) if occ is not None else False))
    bare = background & ~water & (spec['ndvi'] < 0.25) & (spec['mndwi'] < 0)
    # 4.5: дороги вне застройки учатся как «не застройка» (класс грунта / покрытий без зданий)
    bare |= ok & road_only & (g_future < ghsl_pos) & (spec['ndvi'] < 0.35) & (spec['mndwi'] < 0)
    bare &= ~bld_near              # 4.5: где есть здание OSM, грунтом не учим (плоские крыши складов)
    veg = background & ~water & ~bare & (spec['ndvi'] >= 0.35)
    if green_neg and sharp is not None:
        # зелень в посёлке: пиксель ни сейчас, ни раньше не был застроен по резким продуктам (застройка не исчезает,
        # поэтому до 2021 г. включительно это верно и для прошлых лет); позже — только при явной растительности
        g_any = np.nan_to_num(np.max(np.stack(list(src['ghsl'].values())), 0))
        evid = sharp_built_now(src, 0.01, esa_min=0.1, wsf_min=0.1)   # хоть какой-то след застройки 10 м
        thr = green_ndvi if year <= 2021 else green_ndvi + 0.1
        green = ok & ~built & (g_any >= ghsl_neg) & ~evid & (spec['ndvi'] >= thr) & (spec['mndwi'] < 0)
        veg |= green
    lab[veg] = VEG
    lab[bare] = BARE
    lab[water] = WATER
    if protect is not None:
        lab[protect] = -1
    return lab


def building_fraction(src):
    """5.1: доля площади пикселя под зданиями — максимум из OSM (src['osm_bfrac']) и Microsoft (src['ms'])."""
    parts = [np.nan_to_num(np.asarray(src[k], np.float32)) for k in ('osm_bfrac', 'ms') if src.get(k) is not None]
    return np.maximum.reduce(parts) if parts else None


def sharp_built_now(src, bld_min=0.05, esa_min=0.3, wsf_min=0.3):
    """5.1: застроено сейчас по резким продуктам: ESA WorldCover / WSF 2019 (10 м) или доля зданий. None — нет данных."""
    m = None
    for a in (src.get('esa') or {}).values():
        x = np.nan_to_num(a) >= esa_min
        m = x if m is None else (m | x)
    if src.get('wsf2019') is not None:
        x = np.nan_to_num(src['wsf2019']) >= wsf_min
        m = x if m is None else (m | x)
    bf = building_fraction(src)
    if bf is not None:
        x = bf >= bld_min
        m = x if m is None else (m | x)
    return m


def buildings_mask(src, ms_min=0.05, bld_frac_min=None):
    """Здания сейчас: OSM (0/1) ∪ Microsoft Global ML Building Footprints (доля ≥ ms_min). None, если нет обоих.
    5.1: с bld_frac_min — только пиксели, где доля площади под зданиями (OSM + Microsoft) не меньше порога."""
    if bld_frac_min is not None:
        bf = building_fraction(src)
        if bf is not None:
            return bf >= bld_frac_min
    m = None
    if src.get('osm') is not None:
        m = np.asarray(src['osm']) > 0
    if src.get('ms') is not None:
        ms = np.nan_to_num(np.asarray(src['ms'])) >= ms_min
        m = ms if m is None else (m | ms)
    return m


def fraction_target(year, lab, src, *, years_modern=(2019, 2022), stable=0.02):
    """5.0: цель регрессии «доля застроенной поверхности в пикселе» (0–1), NaN — не используется.

    Современная доля — среднее ESA WorldCover (класс 50, 10 м) и WSF 2019 (10 м). В годы 2019–2022 она
    берётся как есть; в другие годы — только там, где GHSL не менялся (|Δ| < stable) с 2020 г. Фон
    (растительность, вода, грунт по меткам) — 0 в любой год."""
    parts = [np.nan_to_num(a) for a in (src.get('esa') or {}).values()]
    if src.get('wsf2019') is not None:
        parts.append(np.nan_to_num(src['wsf2019']))
    t = np.full(lab.shape, np.nan, np.float32)
    if parts:
        mod = np.clip(np.mean(parts, 0), 0, 1).astype(np.float32)
        if years_modern[0] <= year <= years_modern[1]:
            t = mod.copy()
        else:
            g = np.nan_to_num(interp_epochs(src['ghsl'], year)); g0 = np.nan_to_num(interp_epochs(src['ghsl'], 2020))
            st = np.abs(g - g0) < stable
            t[st & (lab == BUILT)] = mod[st & (lab == BUILT)]
    t[(lab == VEG) | (lab == WATER) | (lab == BARE)] = 0.0
    return t


def protect_mask(shape, rows, cols, radius_px=4):
    """Маска вокруг проверочных точек: их окрестность не попадает в обучение."""
    m = np.zeros(shape, bool)
    for r, c in zip(rows, cols):
        m[max(0, r - radius_px):r + radius_px + 1, max(0, c - radius_px):c + radius_px + 1] = True
    return m


# ----------------------------------------------------------------------------- модель
def sample_training(per_year, n_per_class=2500, seed=0, extra=None, extra_repeat=20):
    """per_year: list of (X (H,W,K), labels (H,W)). Возвращает X, y, сбалансированные по классам.

    extra — (X_rows (N,K), y_rows (N,)) — точки пользователя; каждая повторяется extra_repeat раз."""
    rng = np.random.default_rng(seed)
    Xs, ys = [], []
    for X, lab in per_year:
        fin = np.isfinite(X).all(-1)
        for c in range(len(CLASSES)):
            idx = np.flatnonzero((lab.ravel() == c) & fin.ravel())
            if idx.size == 0:
                continue
            take = rng.choice(idx, min(n_per_class, idx.size), replace=False)
            Xs.append(X.reshape(-1, X.shape[-1])[take]); ys.append(np.full(take.size, c, np.int8))
    if extra is not None and len(extra[1]):
        xe, ye = np.asarray(extra[0], np.float32), np.asarray(extra[1], np.int8)
        ok = np.isfinite(xe).all(1)
        Xs.append(np.repeat(xe[ok], extra_repeat, 0)); ys.append(np.repeat(ye[ok], extra_repeat))
    if not Xs:          # год без единого полного набора признаков (например, нет снимков Sentinel-2)
        K = per_year[0][0].shape[-1] if per_year else 0
        return np.zeros((0, K), np.float32), np.zeros(0, np.int8)
    return np.concatenate(Xs), np.concatenate(ys)


def has_hgb():
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: F401
        return True
    except Exception:
        return False


def train(X, y, n_estimators=200, seed=0, n_jobs=-1, kind='auto'):
    """kind: 'rf' — Random Forest (4.x); 'hgb' — HistGradientBoosting (5.0: в 5–10 раз быстрее прогноз);
    'auto' — hgb, если есть в scikit-learn, иначе rf. Возвращает модель с атрибутом quality_ (доля верных
    на отложенных 10 % для hgb или out-of-bag для rf)."""
    if kind == 'auto':
        kind = 'hgb' if has_hgb() else 'rf'
    if kind == 'hgb':
        from sklearn.ensemble import HistGradientBoostingClassifier
        rng = np.random.default_rng(seed)
        hold = rng.random(len(y)) < 0.1
        # 100 итераций × 4 класса: прогноз вдвое быстрее, чем 200 × 0,08, точность на отложенной части та же
        m = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.15, max_leaf_nodes=31,
                                           min_samples_leaf=40, l2_regularization=1.0, early_stopping=False,
                                           random_state=seed)
        m.fit(X[~hold], y[~hold])
        m.quality_ = float((m.predict(X[hold]) == y[hold]).mean()) if hold.any() else float('nan')
        m.kind_ = 'hgb'
        return m
    from sklearn.ensemble import RandomForestClassifier
    rf = RandomForestClassifier(n_estimators=n_estimators, min_samples_leaf=3, max_features='sqrt',
                                class_weight='balanced', n_jobs=n_jobs, random_state=seed, oob_score=True)
    rf.fit(X, y)
    rf.quality_ = float(rf.oob_score_)
    rf.kind_ = 'rf'
    return rf


def sample_fraction(per_year, n=4000, seed=0):
    """per_year: list of (X, target). Выборка со сбалансированными интервалами доли (0, (0,.25], …, (.75,1])."""
    rng = np.random.default_rng(seed)
    Xs, ts = [], []
    edges = (-0.01, 0.0, 0.25, 0.5, 0.75, 1.01)
    for X, t in per_year:
        fin = np.isfinite(X).all(-1).ravel() & np.isfinite(t).ravel()
        tr = t.ravel()
        for a, b in zip(edges, edges[1:]):
            idx = np.flatnonzero(fin & (tr > a) & (tr <= b))
            if idx.size:
                take = rng.choice(idx, min(n // 5, idx.size), replace=False)
                Xs.append(X.reshape(-1, X.shape[-1])[take]); ts.append(tr[take])
    if not Xs:
        K = per_year[0][0].shape[-1] if per_year else 0
        return np.zeros((0, K), np.float32), np.zeros(0, np.float32)
    return np.concatenate(Xs), np.concatenate(ts).astype(np.float32)


def train_fraction(X, t, seed=0):
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
        m = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.15, max_leaf_nodes=31,
                                          min_samples_leaf=40, l2_regularization=1.0, early_stopping=False,
                                          random_state=seed)
    except Exception:
        from sklearn.ensemble import RandomForestRegressor
        m = RandomForestRegressor(n_estimators=120, min_samples_leaf=5, max_features='sqrt', n_jobs=-1, random_state=seed)
    m.fit(X, t)
    return m


def predict_fraction(model, X, chunk=400000):
    H, W, K = X.shape
    flat = X.reshape(-1, K)
    out = np.full(flat.shape[0], np.nan, np.float32)
    idx = np.flatnonzero(np.isfinite(flat).all(1))
    for s in range(0, idx.size, chunk):
        out[idx[s:s + chunk]] = np.clip(model.predict(flat[idx[s:s + chunk]]), 0, 1)
    return out.reshape(H, W)


def predict(rf, X, chunk=200000):
    """(H,W,K) → вероятности (H,W,4), NaN там, где признаков нет."""
    H, W, K = X.shape
    flat = X.reshape(-1, K)
    out = np.full((flat.shape[0], len(CLASSES)), np.nan, np.float32)
    fin = np.isfinite(flat).all(1)
    idx = np.flatnonzero(fin)
    full = np.zeros((idx.size, len(CLASSES)), np.float32)
    for s in range(0, idx.size, chunk):
        p = rf.predict_proba(flat[idx[s:s + chunk]])
        full[s:s + chunk][:, rf.classes_.astype(int)] = p
    out[idx] = full
    return out.reshape(H, W, len(CLASSES))


# ----------------------------------------------------------------------------- время
def change_year(prob, years, min_run=2, fill_gaps=True, weights=None, cost='linear', edge_policy='strict'):
    """Одна ступенька «нет → есть» по вероятностям застройки.

    prob: (T,H,W) вероятность застройки по годам (NaN — нет наблюдения).
    Для каждого пикселя перебираются все годы смены c (и «никогда»), стоимость =
    сумма p до c + сумма (1-p) с c; выбирается минимум. Возвращает (год смены, 0 = не застроено;
    уверенность 0–1 = 1 − стоимость/число наблюдений). Смена признаётся, только если после неё
    не меньше min_run наблюдений с вероятностью ≥ 0,5 (для «застроено с начала ряда» — хотя бы одно).

    5.0: weights (T,H,W) — вес наблюдения (например, по числу чистых сцен в композите: медиана из одной
    сцены шумнее); cost='log' — штраф −log(1−p) / −log(p): уверенные наблюдения весят больше.
    """
    T = len(years)
    p = np.asarray(prob, np.float64)
    obs = np.isfinite(p)
    pz = np.where(obs, p, 0.0)
    if cost == 'log':
        e = 0.02
        a = np.where(obs, -np.log(np.clip(1 - pz, e, 1)), 0.0)
        b = np.where(obs, -np.log(np.clip(pz, e, 1)), 0.0)
    else:
        a = np.where(obs, pz, 0.0)            # стоимость «нет застройки» в году t
        b = np.where(obs, 1.0 - pz, 0.0)      # стоимость «застройка» в году t
    if weights is not None:
        w = np.where(obs, np.nan_to_num(np.asarray(weights, np.float64)), 0.0)
        a, b = a * w, b * w
    ca = np.concatenate([np.zeros((1,) + p.shape[1:]), np.cumsum(a, 0)])   # сумма a[:c]
    cb_rev = np.concatenate([np.cumsum(b[::-1], 0)[::-1], np.zeros((1,) + p.shape[1:])])  # сумма b[c:]
    hi = obs & (pz >= 0.5)                # наблюдения, где пиксель «выглядит застроенным»
    cobs_rev = np.concatenate([np.cumsum(hi[::-1], 0)[::-1], np.zeros((1,) + p.shape[1:])])
    cost_ = ca + cb_rev                   # (T+1, H, W): c = 0..T (T = «никогда»)
    if edge_policy not in ('strict', 'adaptive'):
        raise ValueError('Unknown temporal edge policy: ' + str(edge_policy))
    required = min_run
    if edge_policy == 'adaptive':
        required = np.minimum(min_run, np.arange(T, -1, -1))[:, None, None]
    valid_c = cobs_rev >= required
    valid_c[0] = cobs_rev[0] >= 1         # «с начала ряда» — достаточно данных вообще
    valid_c[T] = True
    cost_ = np.where(valid_c, cost_, np.inf)
    best = np.argmin(cost_, 0)
    n = obs.sum(0)
    # уверенность — по линейной стоимости (сопоставима между режимами)
    la = np.concatenate([np.zeros((1,) + p.shape[1:]), np.cumsum(np.where(obs, pz, 0.0), 0)])
    lb = np.concatenate([np.cumsum(np.where(obs, 1 - pz, 0.0)[::-1], 0)[::-1], np.zeros((1,) + p.shape[1:])])
    lin = np.take_along_axis(la + lb, best[None], 0)[0]
    with np.errstate(all='ignore'):
        conf = 1 - lin / np.maximum(n, 1)
    yr = np.where(best < T, np.asarray(years)[np.minimum(best, T - 1)], 0).astype(np.int16)
    yr[n == 0] = -1
    return yr, conf.astype(np.float32)


def calibrate_threshold(p, y, grid=None, shrink=0.7, bounds=(0.3, 0.7), min_gain=0.03):
    """5.2: порог вероятности «застроено» по точкам пользователя (вероятности получены перекрёстно — моделью,
    которая этих точек не видела). Перебор порогов → максимум сбалансированной точности; сдвиг от 0,5 ослабляется
    (shrink) и ограничивается bounds; если выигрыш меньше min_gain — порог остаётся 0,5.
    Возвращает dict(t, acc, acc0, n) или None, если точек мало или нет обоих классов."""
    p = np.asarray(p, np.float64); y = np.asarray(y, int)
    ok = np.isfinite(p)
    p, y = p[ok], y[ok]
    if len(y) < 10 or y.min() == y.max():
        return None
    grid = np.arange(0.2, 0.81, 0.02) if grid is None else np.asarray(grid)

    def bacc(t):
        pr = p >= t
        return 0.5 * ((pr & (y == 1)).sum() / max((y == 1).sum(), 1) + (~pr & (y == 0)).sum() / max((y == 0).sum(), 1))
    sc = np.array([bacc(t) for t in grid])
    best = sc.max()
    t = float(np.median(grid[sc >= best - 1e-9]))
    acc0 = bacc(0.5)
    if best - acc0 < min_gain:
        t = 0.5
    t = float(np.clip(0.5 + shrink * (t - 0.5), *bounds))
    return dict(t=round(t, 3), acc=round(float(bacc(t)), 3), acc0=round(float(acc0), 3), n=int(len(y)))


def stratified_points(year_map, n=60, min_sep_px=5, seed=0, mask=None, year=None):
    """5.2: проверочные точки — поровну в «застроено» и «не застроено» по карте (не дальше 1 км от застройки),
    случайно, с разносом min_sep_px. Возвращает строки, столбцы и класс карты."""
    from scipy import ndimage as ndi
    rng = np.random.default_rng(seed)
    yr = np.asarray(year_map)
    built = (yr > 0) & ((yr <= year) if year else True)
    near = ndi.distance_transform_edt(~built) <= 33
    valid = yr >= 0
    if mask is not None:
        valid &= mask
    rows, cols, cls = [], [], []
    for k, m in ((1, built & valid), (0, ~built & near & valid)):
        idx = np.argwhere(m); rng.shuffle(idx); got = 0
        for r, c in idx:
            if all((r - r2) ** 2 + (c - c2) ** 2 >= min_sep_px ** 2 for r2, c2 in zip(rows, cols)):
                rows.append(int(r)); cols.append(int(c)); cls.append(k); got += 1
                if got >= n // 2:
                    break
    return rows, cols, cls


def obs_weights(clear_counts, cap=6):
    """Вес композита по числу чистых сцен: 1 сцена — 0,4; ≥ cap — 1."""
    c = np.nan_to_num(np.asarray(clear_counts, np.float32))
    return np.clip(0.4 + 0.6 * (c - 1) / max(cap - 1, 1), 0.4, 1.0).astype(np.float32)


def uncertainty_points(prob, n=40, min_sep_px=5, seed=0, band=(0.3, 0.7), mask=None):
    """5.0: точки для разметки пользователем — там, где модель сомневается (вероятность в band),
    с разносом не меньше min_sep_px. Возвращает списки строк и столбцов."""
    rng = np.random.default_rng(seed)
    p = np.nan_to_num(np.asarray(prob), nan=-1)
    cand = (p >= band[0]) & (p <= band[1])
    if mask is not None:
        cand &= mask
    idx = np.argwhere(cand)
    rng.shuffle(idx)
    rows, cols = [], []
    for r, c in idx:
        if all((r - r2) ** 2 + (c - c2) ** 2 >= min_sep_px ** 2 for r2, c2 in zip(rows, cols)):
            rows.append(int(r)); cols.append(int(c))
            if len(rows) >= n:
                break
    return rows, cols


def effective_built_prob(pb, pbare, osm_bld):
    """4.5: там, где сейчас стоит здание OSM, «грунт» тоже считается свидетельством застройки
    (светлые плоские крыши складов спектрально похожи на песок)."""
    if osm_bld is None:
        return pb
    m = np.asarray(osm_bld) > 0
    return np.where(m[None], np.minimum(1.0, np.nan_to_num(pb) + np.nan_to_num(pbare)), pb).astype(np.float32) \
        if pbare is not None else pb


def refine_new_growth(yr, pb, years, *, roads=None, osm_bld=None, min_mean=None, open_shape=True, road_radius=3):
    """4.5: отсев ложного прироста. Новая застройка (год > первого) снимается, если:
      — (необязательно, min_mean=…) средняя вероятность застройки после смены ниже порога. По умолчанию
        выключено: на Урае и в Калуге снимало настоящий прирост (κ −0,04…−0,05);
      — (необязательно, roads=…) пиксель лежит на дороге OSM и рядом нет здания OSM. По умолчанию
        выключено: на Урае это снимало настоящую застройку (κ 0,94 → 0,87–0,90);
      — пиксель — тонкий отросток шириной 1–2 пикселя (снимается морфологическим открытием 3×3).
    Возвращает (уточнённая карта года, отчёт по числу снятых пикселей)."""
    from scipy import ndimage as ndi
    yr = np.array(yr, copy=True)
    first = years[0]
    new = yr > first
    rep = {}
    if min_mean is not None:
        p = np.asarray(pb, np.float32)
        obs = np.isfinite(p)
        idx = np.searchsorted(np.asarray(years), np.where(new, yr, years[-1] + 1))
        t = np.arange(len(years))[:, None, None]
        after = (t >= idx[None]) & obs
        with np.errstate(all='ignore'):
            mean_after = np.where(after, np.nan_to_num(p), 0).sum(0) / np.maximum(after.sum(0), 1)
        weak = new & (mean_after < min_mean)
        rep['low_mean'] = int(weak.sum()); yr[weak] = 0
    if roads is not None:
        bld = ndi.binary_dilation(np.asarray(osm_bld) > 0, iterations=road_radius) if osm_bld is not None else False
        on_road = (yr > first) & (np.asarray(roads) > 0) & ~bld
        rep['roads'] = int(on_road.sum()); yr[on_road] = 0
    if open_shape:
        built = yr > 0
        opened = ndi.binary_opening(built, np.ones((3, 3), bool))
        thin = (yr > first) & ~opened
        rep['thin'] = int(thin.sum()); yr[thin] = 0
    return yr, rep


def built_mask_for(year_map, year):
    return (year_map > 0) & (year_map <= year)


def bare_persistent(prob_bare, min_years=3):
    """Открытый грунт (карьеры, пески, пляжи, отвалы): грунт — главный класс не менее min_years лет."""
    return np.nansum(np.nan_to_num(prob_bare, nan=0) >= 0.5, 0) >= min_years
