# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""Оценка точности результата (v4.2): площадная матрица ошибок, каппа, F1, шахматная проверка."""
import numpy as np


def confusion(pred, ref_u, ref_n, area, zone=None):
    pred, ref_u, ref_n = (np.asarray(x, dtype=bool) for x in (pred, ref_u, ref_n))
    area = np.asarray(area, dtype=float)
    if any(x.shape != pred.shape for x in (ref_u, ref_n, area)):
        raise ValueError('Маски и площади должны иметь одинаковую форму.')
    if np.any(ref_u & ref_n):
        raise ValueError('Классы эталона пересекаются: увеличьте расстояние фона относительно буфера застройки.')
    if not np.isfinite(area).all() or (area < 0).any():
        raise ValueError('Площади пикселей должны быть конечными неотрицательными числами.')
    if zone is not None:
        ref_u, ref_n = ref_u & zone, ref_n & zone
    tp = float(area[pred & ref_u].sum()); fn = float(area[~pred & ref_u].sum())
    fp = float(area[pred & ref_n].sum()); tn = float(area[~pred & ref_n].sum())
    n = tp + fn + fp + tn
    if n == 0:
        return None
    oa = (tp + tn) / n
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n ** 2
    kappa = (oa - pe) / (1 - pe) if pe < 1 else 0.0
    pa = tp / (tp + fn) if tp + fn else 0.0
    ua = tp / (tp + fp) if tp + fp else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0
    return dict(TP_km2=tp, FN_km2=fn, FP_km2=fp, TN_km2=tn, OA=oa, producer_acc=pa, user_acc=ua,
                kappa=kappa, F1=f1, omission=1 - pa, commission=1 - ua)


def checkerboard(shape, gt, is_geographic, block_m=1000.0, unit_m=1.0):
    """Шахматка блоков block_m×block_m метров: True = контрольные клетки, False = калибровочные."""
    if not np.isfinite(block_m) or block_m <= 0 or not np.isfinite(unit_m) or unit_m <= 0:
        raise ValueError('Размер клетки и единицы должны быть положительными.')
    h, w = shape
    if is_geographic:
        lat0 = gt[3] + gt[5] * h / 2
        px_x = abs(gt[1]) * 111320.0 * np.cos(np.radians(lat0))
        px_y = abs(gt[5]) * 110574.0
    else:
        px_x, px_y = abs(gt[1]) * unit_m, abs(gt[5]) * unit_m
    bx = (np.arange(w) * px_x // block_m).astype(int)
    by = (np.arange(h) * px_y // block_m).astype(int)
    return ((by[:, None] + bx[None, :]) % 2).astype(bool)


def format_report(title, res_all, res_check, res_cal, notes):
    def row(name, r):
        if not r:
            return f'{name:<22}—\n'
        return (f"{name:<22}{r['TP_km2']:>10.3f}{r['FN_km2']:>10.3f}{r['FP_km2']:>10.3f}{r['TN_km2']:>11.3f}"
                f"{r['OA']:>8.3f}{r['producer_acc']:>8.3f}{r['user_acc']:>8.3f}{r['kappa']:>8.3f}{r['F1']:>8.3f}\n")
    s = '=' * 105 + f'\n{title}\n' + '=' * 105 + '\n'
    s += f"{'Зона':<22}{'TP, км²':>10}{'FN, км²':>10}{'FP, км²':>10}{'TN, км²':>11}{'OA':>8}{'PA':>8}{'UA':>8}{'Kappa':>8}{'F1':>8}\n"
    s += '-' * 105 + '\n'
    s += row('Вся территория', res_all) + row('Контрольные клетки', res_check) + row('Калибровочные клетки', res_cal)
    s += '-' * 105 + '\n'
    s += 'TP — верно застройка, FN — пропуск, FP — лишняя застройка, TN — верно фон; PA — полнота, UA — точность пользователя.\n'
    for n in notes:
        s += f'* {n}\n'
    return s
