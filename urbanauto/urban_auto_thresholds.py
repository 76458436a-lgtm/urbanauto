# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Расчёт порогов и формулы классификации застройки.

Общий модуль для диалога и алгоритма Processing (раньше код был продублирован).
Формула использует обозначения калькулятора растров GDAL:
A = NDBI, B = BSI, C = MNDWI (max), D = NDVI.
"""
import math
import numpy as np
from osgeo import gdal
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject

# Допустимый диапазон порога спектрального баланса (NDBI − NDVI).
# В бореальных городах (Урай, 60° с.ш.) застройка имеет NDBI−NDVI ≈ −0.5,
# а лес ≈ −1.1, поэтому нижняя граница должна быть отрицательной.
BALANCE_MIN = -1.0
BALANCE_MAX = 0.3


def _read_band(path, max_size):
    ds = gdal.Open(path)
    if not ds:
        return None, None
    band = ds.GetRasterBand(1)
    bx, by = min(band.XSize, max_size), min(band.YSize, max_size)
    arr = band.ReadAsArray(buf_xsize=bx, buf_ysize=by)
    nodata = band.GetNoDataValue()
    ds = None
    if arr is None:
        return None, None
    arr = arr.astype(np.float64).ravel()
    valid = ~np.isnan(arr) & (arr >= -1.0) & (arr <= 1.0)
    if nodata is not None:
        valid &= arr != nodata
    return arr, valid


def otsu_threshold(raster_path, max_size=2048):
    """Порог Оцу по гистограмме (256 классов) значений индекса в диапазоне [-1; 1]."""
    arr, valid = _read_band(raster_path, max_size)
    if arr is None:
        raise ValueError(f"Не удалось прочитать растр: {raster_path}")
    arr = arr[valid]
    if arr.size < 10:
        raise ValueError(f"Недостаточно валидных пикселей для Оцу: {raster_path}")
    vmin, vmax = arr.min(), arr.max()
    if vmin == vmax:
        raise ValueError(f"Однородный растр: автоматическая калибровка невозможна ({raster_path}).")
    hist, edges = np.histogram(arr, bins=256, range=(vmin, vmax))
    centers = (edges[:-1] + edges[1:]) / 2
    w1 = np.cumsum(hist)
    w2 = np.cumsum(hist[::-1])[::-1]
    m1 = np.cumsum(hist * centers) / np.where(w1 == 0, 1, w1)
    m2 = (np.cumsum((hist * centers)[::-1]) / np.where(w2 == 0, 1, w2)[::-1])[::-1]
    var_between = w1[:-1] * w2[1:] * (m1[:-1] - m2[1:]) ** 2
    return float(centers[int(np.argmax(var_between))])


def kmeans_balance_threshold(ndbi_path, ndvi_path, log_callback=None, max_size=1024, softening=0.33):
    """Порог баланса (NDBI − NDVI) по кластеризации K-Means (k = 3) в пространстве NDBI×NDVI.

    Кластеры упорядочиваются по NDBI − NDVI; порог ставится между вторым и третьим
    (наиболее «застроенным») кластером со смещением softening к второму.
    Исправлено в 4.1: ранее значения < −0.1 заменялись на +0.05, что в таёжной зоне
    обнуляло результат. Теперь порог ограничивается диапазоном [BALANCE_MIN; BALANCE_MAX].
    Возвращает (порог, сведения о кластерах).
    """
    if log_callback:
        log_callback("K-Means (k=3) для порога спектрального баланса...")
    a, va = _read_band(ndbi_path, max_size)
    b, vb = _read_band(ndvi_path, max_size)
    if a is None or b is None or a.size != b.size:
        raise ValueError("K-Means: индексы не прочитаны или их размеры различаются.")
    valid = va & vb
    if valid.sum() < 10:
        raise ValueError("K-Means: недостаточно общих валидных пикселей.")
    data = np.stack([a[valid], b[valid]], axis=1)
    if len(np.unique(data, axis=0)) < 3:
        raise ValueError("K-Means: нужно не менее трёх различных спектральных значений.")
    # 4.3.1: несколько детерминированных стартов, берётся решение с наименьшей
    # внутрикластерной суммой квадратов (SSE). Только «дальние точки» (4.3) на Урае
    # 2020/2025 сходились к худшему минимуму (SSE +27–59 %, κ −0.03), только фиксированные
    # старты 4.2.1 могут потерять малый класс застройки среди леса.
    balance = data[:, 0] - data[:, 1]
    first, last = int(balance.argmin()), int(balance.argmax())
    distance = np.minimum(((data-data[first])**2).sum(axis=1),
                          ((data-data[last])**2).sum(axis=1))
    middle = int(distance.argmax())
    order_b = np.argsort(balance)
    starts = {
        'fixed_4.2': np.array([[0.1, 0.5], [0.3, 0.1], [0.5, -0.2]]),
        'farthest': data[[first, middle, last]].copy(),
        'quantile': data[order_b[[len(order_b) // 10, len(order_b) // 2, (9 * len(order_b)) // 10]]].copy(),
    }

    def _run(c):
        it = 0
        for step in range(1, 51):
            it = step
            lab = np.linalg.norm(data[:, None] - c, axis=2).argmin(axis=1)
            new = np.array([data[lab == i].mean(axis=0) if np.sum(lab == i) > 10 else c[i] for i in range(3)])
            if np.allclose(c, new, atol=1e-4):
                c = new
                break
            c = new
        lab = np.linalg.norm(data[:, None] - c, axis=2).argmin(axis=1)
        sizes_ = np.bincount(lab, minlength=3)
        sse = float(((data - c[lab]) ** 2).sum())
        return c, it, sizes_, sse

    best = None
    for name, c0 in starts.items():
        c, it, sz, sse = _run(c0.astype(float))
        if (sz <= 10).any():
            continue
        if best is None or sse < best[4] - 1e-9:
            best = (name, c, it, sz, sse)
    if best is None:
        raise ValueError("K-Means: недостаточно пикселей в одном из кластеров. Задайте ручной порог баланса.")
    seed_name, centroids, iterations, _, best_sse = best
    labels = np.linalg.norm(data[:, None] - centroids, axis=2).argmin(axis=1)
    sizes = np.bincount(labels, minlength=3)
    if (sizes <= 10).any():
        raise ValueError("K-Means: недостаточно пикселей в одном из кластеров. Задайте ручной порог баланса.")
    diff = centroids[:, 0] - centroids[:, 1]
    order = np.argsort(diff)
    t_raw = diff[order[1]] + softening * (diff[order[2]] - diff[order[1]])
    t = float(np.clip(t_raw, BALANCE_MIN, BALANCE_MAX))
    info = dict(centroids=centroids.round(4).tolist(), iterations=int(iterations), seed=seed_name, sse=round(best_sse, 3),
                sizes=np.bincount(labels, minlength=3).tolist(), raw=float(t_raw))
    if log_callback:
        log_callback(f"K-Means: порог баланса = {t:.3f} (до ограничения {t_raw:.3f}, итераций {iterations})")
    return t, info


def sample_points(points_wgs84, layers, transform_context=None):
    """Значения растров в точках (lon, lat WGS84). Пропускает точки вне охвата / NaN."""
    if not points_wgs84:
        return []
    if transform_context is None:
        transform_context = QgsProject.instance().transformContext()
    xforms = [QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), layer.crs(),
                                    transform_context) for layer in layers]
    out = []
    for lon, lat in points_wgs84:
        vals = []
        for lyr, xform in zip(layers, xforms):
            pt = xform.transform(QgsPointXY(lon, lat))
            provider = lyr.dataProvider()
            v, ok = provider.sample(pt, 1)
            if (not ok or v is None or not math.isfinite(float(v)) or not -1 <= float(v) <= 1
                    or (provider.sourceHasNoDataValue(1) and v == provider.sourceNoDataValue(1))):
                vals = None
                break
            vals.append(float(v))
        if vals:
            out.append(vals)
    return out


def box_expr(box):
    n_lo, n_hi, b_lo, b_hi, v_hi = box
    return f'(A >= {n_lo:.4f}) * (A <= {n_hi:.4f}) * (B >= {b_lo:.4f}) * (B <= {b_hi:.4f}) * (D < {v_hi:.4f})'


BOX_METHODS = [
    ('robust', 'Устойчивые (процентили 20–80, отсев нетипичных точек)'),
    ('minmax', 'Мин/макс по точкам (как до версии 4.2)'),
]
ROBUST_PCT = (20, 80)


def drop_outliers(samples, k=1.5, min_keep=4):
    """Отбрасывает точки, у которых хотя бы один индекс выходит за [Q1 − k·IQR; Q3 + k·IQR]."""
    a = np.asarray(samples, dtype=float)
    if len(a) < min_keep + 1:
        return a, 0
    q1, q3 = np.percentile(a, 25, axis=0), np.percentile(a, 75, axis=0)
    iqr = q3 - q1
    keep = ((a >= q1 - k * iqr) & (a <= q3 + k * iqr)).all(axis=1)
    if keep.sum() < min_keep:
        return a, 0
    return a[keep], int((~keep).sum())


def _range(a, method):
    if method == 'robust':
        lo, hi = ROBUST_PCT
        return np.percentile(a, lo, axis=0), np.percentile(a, hi, axis=0)
    return a.min(axis=0), a.max(axis=0)


def boxes_from_points(samples, margin=0.2, method='minmax'):
    """Три «бокса» NDBI×BSI по значениям в точках города (с запасом margin).

    method='minmax' — границы по минимуму и максимуму (до 4.2);
    method='robust' — по процентилям 20–80 после отсева нетипичных точек (4.2): одна точка во дворе
    с деревьями больше не расширяет класс до леса.
    """
    a = np.asarray(samples, dtype=float)
    lo, hi = _range(a, method)
    n_min, b_min = lo[0], lo[1]
    n_max, b_max, v_max = hi[0], hi[1], hi[2]
    v_lim = v_max + 0.10
    n_lo = n_min - margin * (n_max - n_min)
    n_hi = n_max + margin * (n_max - n_min)
    b_lo = b_min - margin * (b_max - b_min)
    b_hi = b_max + margin * (b_max - b_min)
    sn = (n_hi - n_lo) / 3.0
    sb = (b_hi - b_lo) / 3.0
    return [(float(n_lo), float(n_lo + sn), float(b_lo), float(b_lo + sb), float(v_lim)),
            (float(n_lo + sn), float(n_lo + 2 * sn), float(b_lo + sb), float(b_lo + 2 * sb), float(v_lim)),
            (float(n_lo + 2 * sn), float(n_hi), float(b_lo + 2 * sb), float(b_hi), float(v_lim + 0.10))]


def build_formula(ndbi_lyr, bsi_lyr, ndvi_lyr, *, use_otsu=False, manual_boxes=None,
                  urban_points=None, nonurban_points=None, use_balance=True, balance_ai=True,
                  balance_value=0.0, box_method='robust', log_callback=None, transform_context=None):
    """Возвращает (formula, mode, thresholds_dict)."""
    log = log_callback or (lambda m: None)
    thr = {}
    if box_method not in dict(BOX_METHODS):
        raise ValueError("Неизвестный метод порогов по точкам.")
    for box in manual_boxes or []:
        if len(box) != 5 or not all(math.isfinite(v) for v in box) or box[0] > box[1] or box[2] > box[3]:
            raise ValueError("Некорректные границы ручного бокса.")
    if use_otsu:
        if urban_points or any(manual_boxes or []):
            log("ВНИМАНИЕ: включён метод Оцу — заданные точки/боксы не используются для порогов. "
                "Снимите галочку «Метод Оцу», чтобы считать по точкам (точнее).")
        o_n, o_b, o_v = (otsu_threshold(ndbi_lyr.source()), otsu_threshold(bsi_lyr.source()),
                         otsu_threshold(ndvi_lyr.source()))
        t_n, t_b, t_v = o_n - 0.05, o_b - 0.02, o_v + 0.05
        thr.update(otsu_ndbi=o_n, otsu_bsi=o_b, otsu_ndvi=o_v, ndbi_gt=t_n, bsi_gt=t_b, ndvi_lt=t_v)
        log(f"Пороги Оцу (смягчённые): NDBI>{t_n:.3f}, BSI>{t_b:.3f}, NDVI<{t_v:.3f}")
        formula = f'1 * (A > {t_n:.4f}) * (B > {t_b:.4f}) * (D < {t_v:.4f}) * (C <= 0)'
        mode = 'otsu'
    else:
        boxes = [b for b in (manual_boxes or []) if b]
        mode = 'manual_boxes'
        if not boxes:
            samples = sample_points(urban_points or [], [ndbi_lyr, bsi_lyr, ndvi_lyr], transform_context)
            if not samples:
                raise ValueError("Точки города не заданы или вне охвата растров. "
                                 "Расставьте точки, задайте боксы вручную или включите метод Оцу.")
            dropped = 0
            if box_method == 'robust':
                samples, dropped = drop_outliers(samples)
                if dropped:
                    log(f"Отброшено нетипичных точек города: {dropped}")
            boxes = boxes_from_points(samples, method=box_method)
            mode = f'seed_points(n={len(samples)},{box_method})'
            thr.update(urban_samples=len(samples), urban_dropped=dropped, box_method=box_method)
        for i, b in enumerate(boxes, 1):
            thr[f'box{i}'] = [round(x, 4) for x in b]
        formula = '1 * ( ' + ' + '.join(f'({box_expr(b)})' for b in boxes) + ' > 0 ) * (C <= 0)'

        bg = sample_points(nonurban_points or [], [ndbi_lyr, bsi_lyr], transform_context)
        if bg:
            g = np.asarray(bg, dtype=float)
            if box_method == 'robust':
                g, gd = drop_outliers(g, min_keep=3)
                thr['background_dropped'] = gd
            glo, ghi = _range(g, box_method)
            n_min, n_max, b_min, b_max = float(glo[0]), float(ghi[0]), float(glo[1]), float(ghi[1])
            anti = (f'(A >= {n_min - 0.05:.4f}) * (A <= {n_max + 0.05:.4f}) * '
                    f'(B >= {b_min - 0.05:.4f}) * (B <= {b_max + 0.05:.4f})')
            formula += f' * ({anti} == 0)'
            thr['background_samples'] = len(bg)
            thr['anti_box'] = [n_min - 0.05, n_max + 0.05, b_min - 0.05, b_max + 0.05]

    if use_balance:
        if balance_ai:
            base, info = kmeans_balance_threshold(ndbi_lyr.source(), ndvi_lyr.source(), log)
            bal = base + balance_value
            thr.update(balance_kmeans=base, balance_offset=balance_value, kmeans=info)
            log(f"Баланс: K-Means ({base:.3f}) + смещение ({balance_value:.3f}) = {bal:.3f}")
        else:
            bal = balance_value
            log(f"Ручной порог баланса: {bal:.3f}")
        thr['balance_gt'] = bal
        formula += f' * ((A - D) > {bal:.4f})'
        mode += '+balance'
    return formula, mode, thr
