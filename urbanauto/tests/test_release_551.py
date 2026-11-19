"""5.5.1: контроль качества композитов, подсказка профиля, геокодер, перевод, зависимости, идентификатор."""
import os
import tempfile
import unittest
from unittest.mock import patch, Mock

import numpy as np

from urbanauto import urban_auto_series as S
from urbanauto import urban_auto_labels as LB
from urbanauto import urban_auto_i18n as I18N
from urbanauto import urban_auto_deps as DEPS
from urbanauto import urban_auto_version as V


def _spectra(valid):
    nd = np.where(valid, 0.3, np.nan).astype(np.float32)
    return dict(ndvi=nd)


class CompositeQuality(unittest.TestCase):
    def test_statuses(self):
        full = np.ones((10, 10), bool)
        quarter = np.zeros((10, 10), bool); quarter[:5, :5] = True
        most = np.ones((10, 10), bool); most[0, :5] = False
        sp = {2002: _spectra(full), 2003: _spectra(quarter), 2004: _spectra(most)}
        cl = {y: np.full((10, 10), 3, np.float32) for y in sp}
        q = S.composite_quality(sp, cl)
        self.assertEqual(q[2002]['status'], 'ok')
        self.assertEqual(q[2003]['status'], 'low')
        self.assertAlmostEqual(q[2003]['coverage'], 0.25)
        self.assertEqual(q[2004]['status'], 'ok')        # 95 % ≥ QUALITY_PARTIAL
        q = S.composite_quality(sp, cl, min_coverage=0.6)
        self.assertEqual(q[2003]['status'], 'excluded')

    def test_roi_limits_coverage(self):
        valid = np.zeros((10, 10), bool); valid[:, :5] = True
        roi = np.zeros((10, 10), bool); roi[:, :5] = True
        q = S.composite_quality({2010: _spectra(valid)}, {2010: np.ones((10, 10))}, roi)
        self.assertEqual(q[2010]['coverage'], 1.0)

    def test_default_keeps_years(self):
        self.assertEqual(S.QUALITY_MIN_COVERAGE, 0.0)

    def test_anomaly(self):
        years = [2000, 2001, 2002, 2003, 2004]
        q = {y: dict(coverage=1.0, scenes=3, status='ok') for y in years}
        S.flag_anomalies(q, years, [0.10, 0.11, 0.02, 0.10, 0.105])
        self.assertEqual(q[2002]['status'], 'anomaly')
        self.assertEqual(q[2001]['status'], 'ok')

    def test_anomaly_ignores_nan(self):
        years = [2000, 2001, 2002]
        q = {y: dict(coverage=1.0, scenes=3, status='ok') for y in years}
        S.flag_anomalies(q, years, [float('nan'), 0.1, 0.1])
        self.assertTrue(all(v['status'] == 'ok' for v in q.values()))


class ProfileHint(unittest.TestCase):
    def test_threshold(self):
        dense = {'ghsl': {2020: np.array([[0.0, 0.9]], np.float32)}}
        sparse = {'ghsl': {2020: np.array([[0.0, 0.5]], np.float32)}}
        self.assertEqual(S.recommend_profile(dense)['recommended'], 'territory')
        self.assertEqual(S.recommend_profile(sparse)['recommended'], 'strict')

    def test_percent_scale_and_missing(self):
        pct = {'ghsl': {2015: np.array([[80.0]], np.float32)}}
        self.assertEqual(S.recommend_profile(pct)['recommended'], 'territory')
        self.assertIsNone(S.recommend_profile({}))


class Geocoder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(LB, '_user_cache', return_value=self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop(); self.tmp.cleanup()

    def _resp(self, items):
        r = Mock(); r.json.return_value = items; r.raise_for_status = Mock(); return r

    def test_user_agent_cache_and_ambiguity(self):
        items = [dict(lon='128.47', lat='50.92', display_name='Белогорск, Амурская область', **{'class': 'place'}),
                 dict(lon='34.6', lat='45.05', display_name='Белогорск, Крым', **{'class': 'place'})]
        logs = []
        with patch('requests.get', return_value=self._resp(items)) as get:
            lon, lat, disp = LB.geocode('Белогорск', log=logs.append)
            self.assertIn('76458436a-lgtm/urbanauto', get.call_args.kwargs['headers']['User-Agent'])
            LB.geocode('Белогорск', log=logs.append)          # второй раз — из кэша
            self.assertEqual(get.call_count, 1)
        self.assertEqual((lon, lat), (128.47, 50.92))
        self.assertTrue(any('несколько мест' in m and 'Крым' in m for m in logs))

    def test_rate_limit(self):
        items = [dict(lon='1', lat='2', display_name='A', **{'class': 'place'})]
        LB._NOMINATIM_LAST[0] = 0.0
        with patch('requests.get', return_value=self._resp(items)), patch.object(LB.time, 'sleep') as sl:
            LB.geocode('A1'); LB.geocode('A2')
        self.assertTrue(sl.called)


class Translation(unittest.TestCase):
    def test_tr(self):
        with patch.dict(os.environ, {'URBANAUTO_LANG': 'en'}):
            self.assertEqual(I18N.tr('Запустить расчёт'), 'Run')
            self.assertEqual(I18N.tr('строка без перевода'), 'строка без перевода')
        with patch.dict(os.environ, {'URBANAUTO_LANG': 'ru'}):
            self.assertEqual(I18N.tr('Запустить расчёт'), 'Запустить расчёт')

    def test_no_empty_translations(self):
        self.assertTrue(all(v.strip() for v in I18N.EN.values()))


class DependenciesAndIdentity(unittest.TestCase):
    def test_missing_and_hint(self):
        with patch('importlib.util.find_spec', side_effect=lambda m: None if m == 'sklearn' else object()):
            self.assertEqual(DEPS.missing(), ['scikit-learn'])
            with self.assertRaises(RuntimeError) as cm:
                DEPS.require()
        self.assertIn('pip install', str(cm.exception)); self.assertIn('scikit-learn', str(cm.exception))

    def test_version_and_ids(self):
        self.assertEqual(V.PLUGIN_ID, 'urbanauto')
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        meta = open(os.path.join(here, 'metadata.txt'), encoding='utf-8').read()
        self.assertIn('version=' + V.VERSION, meta)
        for f in os.listdir(here):
            if f.endswith('.py'):
                txt = open(os.path.join(here, f), encoding='utf-8').read()
                self.assertNotIn("'urban_auto_exp54:", txt, f)
                self.assertNotIn('[EXP 54]', txt, f)

    def test_legacy_cache_reused(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
            os.makedirs(os.path.join(home, LB.LEGACY_CACHE))
            self.assertTrue(LB._user_cache().endswith(LB.LEGACY_CACHE))
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
            self.assertTrue(LB._user_cache().endswith('.urbanauto_cache'))


class ReportQuality(unittest.TestCase):
    def test_report_sections(self):
        from urbanauto.urban_auto_report import write_report
        proto = dict(version='5.5.1', finished='2026-11-06T21:00', grid=dict(width=1, height=1, crs='EPSG:32641'),
                     min_run=3, edge_policy='strict', label_opts={},
                     composite_quality={'2003': dict(coverage=0.26, scenes=1.0, status='low')},
                     profile_hint=dict(ghsl_peak=0.5, recommended='strict', used='territory'),
                     series=[dict(year=y, built_km2=1.0 + i, new_built_km2=0.1, bare_km2=None if y == 2003 else 0.5,
                                  coverage_pct=26.0 if y == 2003 else 100.0, quality='low' if y == 2003 else 'ok')
                             for i, y in enumerate(range(2000, 2006))])
        with tempfile.TemporaryDirectory() as d:
            p = write_report(os.path.join(d, 'r.html'), proto)
            html = open(p, encoding='utf-8').read()
        self.assertIn('Качество снимков', html)
        self.assertIn('низкое покрытие', html)
        self.assertIn('нижняя оценка', html)
        self.assertIn('ODbL', html)
        self.assertIn('Подсказка', html)


if __name__ == '__main__':
    unittest.main()
