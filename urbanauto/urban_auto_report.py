# SPDX-License-Identifier: GPL-2.0-or-later
# Urban Auto — (c) 2025-2026 Руслан Каленов (Ekyla)
"""5.0: отчёт по расчёту в одном HTML-файле (без внешних ресурсов): площадь застройки по годам,
прирост, доля застроенной поверхности, источники меток, модели, параметры и, если переданы точки
проверки, оценка точности и поправленная площадь (Olofsson et al., 2014)."""
import html
import math

import numpy as np


def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return '—'
    return f'{v:.{nd}f}'.replace('.', ',')


def _chart(series, keys, labels, colors, w=760, h=260, bars=None):
    """Простая линейная диаграмма SVG; bars — ключ для столбиков прироста (вторая ось)."""
    xs = [r['year'] for r in series]
    vals = [r.get(k) for r in series for k in keys if r.get(k) is not None]
    if not vals:
        return ''
    vmax = max(vals) * 1.1 or 1
    L, R, T, B = 52, 16, 14, 30
    X = lambda i: L + (w - L - R) * (i / max(len(xs) - 1, 1))
    Y = lambda v: T + (h - T - B) * (1 - v / vmax)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;max-width:{w}px">']
    for k in range(5):
        v = vmax * k / 4
        out.append(f'<line x1="{L}" x2="{w - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="ax">{_fmt(v, 0 if vmax > 20 else 1)}</text>')
    if bars:
        bv = [0] + [r.get(bars) or 0 for r in series[1:]]      # первый год — вся застройка, не прирост
        bmax = max(max(bv), 1e-9) * 1.1
        bw = (w - L - R) / max(len(xs), 1) * 0.6
        for i, v in enumerate(bv):
            if v > 0:
                hh = (h - T - B) * 0.35 * v / bmax
                out.append(f'<rect x="{X(i) - bw / 2:.1f}" y="{h - B - hh:.1f}" width="{bw:.1f}" height="{hh:.1f}" fill="var(--bar)">'
                           f'<title>{xs[i]}: +{_fmt(v, 2)} км²</title></rect>')
    for k, c in zip(keys, colors):
        pts = [(X(i), Y(r[k])) for i, r in enumerate(series) if r.get(k) is not None]
        if pts:
            out.append('<polyline fill="none" stroke-width="2.2" stroke="%s" points="%s"/>'
                       % (c, ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)))
    step = max(1, len(xs) // 9)
    for i, x in enumerate(xs):
        if (i % step == 0 and len(xs) - 1 - i > step / 2) or i == len(xs) - 1:
            out.append(f'<text x="{X(i):.1f}" y="{h - 10}" text-anchor="middle" class="ax">{x}</text>')
    out.append('</svg>')
    leg = ''.join(f'<span class="lg"><i style="background:{c}"></i>{html.escape(t)}</span>' for t, c in zip(labels, colors))
    if bars:
        leg += '<span class="lg"><i style="background:var(--bar)"></i>новая застройка за год</span>'
    return '<div class="legend">' + leg + '</div>' + ''.join(out)


def olofsson(map_bin, ref_bin, strata, weights_km2):
    """Стратифицированная оценка: доля застройки, κ, общая точность. strata — номер страты точки,
    weights_km2 — площадь страт. Возвращает dict (area_km2, ci95_km2, oa, kappa)."""
    tot = sum(weights_km2.values())
    q = np.zeros((2, 2))
    var = 0.0
    for h, a in weights_km2.items():
        idx = [i for i, s in enumerate(strata) if s == h]
        if not idx or a <= 0:
            continue
        W = a / tot
        m = np.asarray(map_bin)[idx]; r = np.asarray(ref_bin)[idx]
        for i, j in zip(m, r):
            q[int(i), int(j)] += W / len(idx)
        pr = r.mean()
        if len(idx) > 1:
            var += W ** 2 * pr * (1 - pr) / (len(idx) - 1)
    oa = q[0, 0] + q[1, 1]
    pe = q[1].sum() * q[:, 1].sum() + q[0].sum() * q[:, 0].sum()
    k = (oa - pe) / (1 - pe) if pe < 1 else float('nan')
    share = q[:, 1].sum()
    return dict(area_km2=share * tot, ci95_km2=1.96 * math.sqrt(var) * tot, oa=oa, kappa=k)


def _bars(series, key, w=760, h=170):
    """Столбики прироста за год (своя ось, км²)."""
    xs = [r['year'] for r in series]
    vals = [max(r.get(key) or 0, 0) for r in series]
    if not vals or max(vals) <= 0:
        return ''
    vmax = max(vals) * 1.15
    L, R, T, B = 52, 16, 10, 30
    bw = (w - L - R) / len(xs)
    X = lambda i: L + bw * (i + 0.5)
    Y = lambda v: T + (h - T - B) * (1 - v / vmax)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;max-width:{w}px">']
    for k in range(3):
        v = vmax * k / 2
        out.append(f'<line x1="{L}" x2="{w - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" class="ax">{_fmt(v, 2)}</text>')
    for i, v in enumerate(vals):
        if v > 0:
            out.append(f'<rect x="{X(i) - bw * 0.35:.1f}" y="{Y(v):.1f}" width="{bw * 0.7:.1f}" height="{h - B - Y(v):.1f}" '
                       f'rx="2" fill="var(--bar)"><title>{xs[i]}: +{_fmt(v, 2)} км²</title></rect>')
    step = max(1, len(xs) // 9)
    for i, x in enumerate(xs):
        if (i % step == 0 and len(xs) - 1 - i > step / 2) or i == len(xs) - 1:
            out.append(f'<text x="{X(i):.1f}" y="{h - 10}" text-anchor="middle" class="ax">{x}</text>')
    out.append('</svg>')
    return ''.join(out)


ATTRIBUTION_HTML = (
    '<h2>Источники данных и лицензии</h2><ul class="mut">'
    '<li>Landsat 5/7/8/9 Collection 2 Level-2 — USGS, общественное достояние; доступ через Microsoft Planetary Computer.</li>'
    '<li>Sentinel-2 L2A — Copernicus (ESA), содержит модифицированные данные Copernicus Sentinel.</li>'
    '<li>GHSL BUILT-S R2023A — European Commission, JRC, CC BY 4.0.</li>'
    '<li>World Settlement Footprint — DLR, CC BY 4.0.</li>'
    '<li>ESA WorldCover 2020/2021 — © ESA WorldCover project, CC BY 4.0.</li>'
    '<li>JRC Global Surface Water — EC JRC / Google, CC BY 4.0.</li>'
    '<li>© участники OpenStreetMap, ODbL 1.0 (здания, дороги, границы; геокодер Nominatim).</li>'
    '<li>Microsoft Global ML Building Footprints — ODbL 1.0.</li></ul>'
    '<p class="mut">При публикации карт укажите использованные источники. Результат — модельная оценка; '
    'точность на другой территории не гарантируется и должна проверяться по независимым точкам.</p>')


def _crs_name(crs):
    """Короткое имя системы координат вместо полного WKT."""
    text = str(crs)
    try:
        from osgeo import osr
        sr = osr.SpatialReference(); sr.SetFromUserInput(text)
        code = sr.GetAuthorityCode(None)
        name = sr.GetName() or ''
        return f'EPSG:{code} ({name})' if code else (name or text[:60])
    except Exception:
        return text if len(text) <= 60 else text[:57] + '…'


def write_report(path, proto, title=None, accuracy=None):
    s = proto.get('series', [])
    t = title or 'Застройка по годам'
    y0, y1 = (s[0]['year'], s[-1]['year']) if s else ('', '')
    a0, a1 = (s[0]['built_km2'], s[-1]['built_km2']) if s else (0, 0)
    surf = [r for r in s if r.get('built_surface_km2') is not None]
    css = '''
:root{--bg:#fbfaf7;--fg:#22211d;--mut:#6b6a63;--grid:#e6e3da;--card:#fff;--acc:#2f6fd0;--acc2:#c2632e;--bar:#9db8e8;--bd:#e3e0d6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#1c1c1a;--fg:#ecebe6;--mut:#a3a29a;--grid:#34332f;--card:#252522;--acc:#79a6f2;--acc2:#e59a6b;--bar:#3f5f96;--bd:#3a3935}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}.mut{color:var(--mut)}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}
.kpi div{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}
th,td{padding:4px 8px;border-bottom:1px solid var(--bd);text-align:right}th:first-child,td:first-child{text-align:left}
.ax{font-size:11px;fill:var(--mut)}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--mut)}
.lg i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.wrap{overflow-x:auto}code{font-size:13px}\ntd{overflow-wrap:anywhere}
.warn{border-left:3px solid var(--acc2);padding:6px 10px;background:var(--card)}'''
    h = [f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>{html.escape(t)}</title><style>{css}</style></head><body><main>',
         f'<h1>{html.escape(t)}</h1><div class="mut">Urban Auto {html.escape(str(proto.get("version", "")))} · '
         f'{html.escape(str(proto.get("finished", ""))[:16].replace("T", " "))}</div>']
    h.append('<div class="kpi">'
             f'<div><span class="mut">Застройка {y0}</span><b>{_fmt(a0)} км²</b></div>'
             f'<div><span class="mut">Застройка {y1}</span><b>{_fmt(a1)} км²</b></div>'
             f'<div><span class="mut">Прирост {y0}–{y1}</span><b>+{_fmt(a1 - a0)} км²</b></div>'
             + (f'<div><span class="mut">Застроенная поверхность {y1}</span><b>{_fmt(surf[-1]["built_surface_km2"])} км²</b></div>' if surf else '')
             + '</div>')
    if proto.get('provisional_years'):
        dates = ', '.join(str(int(year)) for year in proto['provisional_years'])
        h.append(f'<p class="warn">Предварительные годы появления застройки: {dates}. Для них срок подтверждения '
                 'сокращён — новая застройка этих лет может не подтвердиться.</p>')
    elif s and proto.get('min_run', 3) > 1 and proto.get('edge_policy', 'strict') == 'strict':
        k = proto.get('min_run', 3)
        tail = [r['year'] for r in s if r.get('quality') != 'excluded'][-(k - 1):]
        if tail:
            h.append(f'<p class="warn">Последние годы ({", ".join(map(str, tail))}): новая застройка здесь не может '
                     f'появиться — её нужно подтвердить {k} годами наблюдений. Прирост за эти годы — нижняя оценка; '
                     'для предварительной оценки включите «адаптивный край ряда» (ADAPTIVE_EDGE).</p>')
    hint = proto.get('profile_hint') or {}
    if hint.get('recommended') and hint.get('used') in ('strict', 'territory') and hint['used'] != hint['recommended']:
        rec = 'территория (профиль «Высокая полнота»)' if hint['recommended'] == 'territory' else 'строгое'
        h.append(f'<p class="warn">Подсказка: пиковая доля застройки GHSL {_fmt(hint["ghsl_peak"])}. Для таких территорий '
                 f'на 15 проверочных городах чаще точнее определение «{rec}». Сравните оба профиля по точкам проверки.</p>')
    bad = {int(y): q for y, q in (proto.get('composite_quality') or {}).items() if q.get('status') != 'ok'}
    if bad:
        names = dict(excluded='исключён из расчёта', low='низкое покрытие', partial='частичное покрытие', anomaly='нетипичная доля застройки')
        h.append('<h2>Качество снимков</h2><div class="wrap"><table><tr><th>Год</th><th>Покрытие</th>'
                 '<th>Чистых сцен (медиана)</th><th>Состояние</th></tr>')
        for y, q in sorted(bad.items()):
            h.append(f'<tr><td>{y}</td><td>{_fmt(100 * q.get("coverage", 0), 0)} %</td><td>{_fmt(q.get("scenes"), 1)}</td>'
                     f'<td style="text-align:left">{names.get(q.get("status"), q.get("status"))}</td></tr>')
        h.append('</table></div><p class="mut">На пикселях без данных год появления определяется по соседним годам. '
                 'Исключённый год (порог задаётся параметром) не участвует ни в обучении, ни в определении года. '
                 'Годовые «сырые» величины (грунт, доля поверхности) для лет с неполным покрытием не приводятся.</p>')
    h.append('<h2>Площадь по годам</h2>')
    keys, labels, colors = ['built_km2'], ['территория застройки (пиксели 30 м)'], ['var(--acc)']
    if surf:
        keys.append('built_surface_km2'); labels.append('застроенная поверхность (сумма долей)'); colors.append('var(--acc2)')
    h.append(_chart(s, keys, labels, colors))
    if len(s) > 1:
        h.append('<h2>Новая застройка за год, км²</h2>' + _bars(s[1:], 'new_built_km2'))
    h.append('<p class="mut">Территория застройки — пиксели, где застройка появилась и подтверждена не менее '
             f'{proto.get("min_run", 3)} лет. Застроенная поверхность — сумма долей зданий и покрытий в пикселях '
             '(меньше территории: дворы, газоны и проезды внутри кварталов в неё не входят).</p>')
    if accuracy:
        h.append('<h2>Точность по точкам проверки</h2><div class="wrap"><table><tr><th>Год</th><th>κ</th><th>Общая точность</th>'
                 '<th>Площадь по точкам, км²</th><th>95 % ДИ, км²</th><th>Площадь по карте, км²</th></tr>')
        for y, a in sorted(accuracy.items()):
            m = next((r['built_km2'] for r in s if r['year'] == y), None)
            h.append(f'<tr><td>{y}</td><td>{_fmt(a["kappa"])}</td><td>{_fmt(a["oa"] * 100, 1)} %</td><td>{_fmt(a["area_km2"])}</td>'
                     f'<td>± {_fmt(a["ci95_km2"])}</td><td>{_fmt(m)}</td></tr>')
        h.append('</table></div>')
    h.append('<h2>Таблица</h2><div class="wrap"><table><tr><th>Год</th><th>Застройка, км²</th><th>Новая, км²</th>'
             + ('<th>Поверхность, км²</th>' if surf else '') + '<th>Открытый грунт, км²</th><th>Покрытие снимком</th></tr>')
    for r in s:
        h.append(f'<tr><td>{r["year"]}</td><td>{_fmt(r["built_km2"])}</td><td>{_fmt(r["new_built_km2"])}</td>'
                 + (f'<td>{_fmt(r.get("built_surface_km2"))}</td>' if surf else '') + f'<td>{_fmt(r["bare_km2"])}</td>'
                 + f'<td>{_fmt(r.get("coverage_pct"), 0)} %{"" if r.get("quality", "ok") == "ok" else " ⚠"}</td></tr>')
    h.append('</table></div>')
    h.append('<h2>Как получено</h2><table>')
    mods = proto.get('models', {})
    rows = [('Сетка', f'{proto["grid"]["width"]}×{proto["grid"]["height"]} пикселей, {html.escape(_crs_name(proto["grid"]["crs"]))}'),
            ('Годы с данными', f'{len(proto.get("years_with_data", []))} ({y0}–{y1})'
             + (f'; исключены: {", ".join(map(str, proto["excluded_years"]))}' if proto.get('excluded_years') else '')),
            ('Определение застройки', 'строгое (здания и покрытия)' if proto.get('label_opts') else 'территория (кварталы с дворами и зеленью)'),
            ('Источник основных композитов', proto.get('composite_sensor', 'не указан')),
            ('Дополнительные признаки Sentinel-2', ', '.join(map(str, proto.get('s2_years', []))) or 'не использован'),
            ('Модели', '; '.join(f'{k}: {v["kind"]}, внутренняя оценка {_fmt(v["quality"], 3)}' for k, v in mods.items())),
            ('Точки пользователя', str(proto.get('user_points', 0))),
            ('Пятен новой застройки', str(proto.get('growth_patches', '—'))),
            ('Время расчёта', f'{_fmt(proto.get("sec", 0) / 60, 1)} мин')]
    for k, v in rows:
        h.append(f'<tr><td>{k}</td><td style="text-align:left">{v}</td></tr>')
    h.append('</table><h2>Источники меток</h2><table>')
    for k, v in (proto.get('label_sources') or {}).items():
        h.append(f'<tr><td>{html.escape(k)}</td><td style="text-align:left">{html.escape(str(v))}</td></tr>')
    h.append('</table>' + ATTRIBUTION_HTML)
    h.append('</main></body></html>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(h))
    return path
