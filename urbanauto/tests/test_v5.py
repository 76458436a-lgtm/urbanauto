"""Тесты 5.0 (numpy/scipy/scikit-learn, без QGIS)."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HAVE = all(importlib.util.find_spec(m) for m in ('scipy', 'sklearn'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE, 'нужны scipy и scikit-learn')
class Model5(unittest.TestCase):
    def setUp(self):
        self.R = load('urban_auto_rf')

    def test_hgb_and_rf_learn_same_rule(self):
        rng = np.random.default_rng(0)
        X = rng.random((4000, 6)).astype(np.float32); y = (X[:, 0] > 0.5).astype(np.int8)
        y[X[:, 1] > 0.8] = 2
        for kind in ('hgb', 'rf'):
            m = self.R.train(X, y, n_estimators=40, kind=kind)
            self.assertGreater(m.quality_, 0.9, kind)
            pr = self.R.predict(m, X[:100].reshape(10, 10, 6))
            self.assertEqual(pr.shape, (10, 10, 4))
            self.assertTrue(np.allclose(pr.sum(-1), 1, atol=1e-5))

    def test_fraction_regression(self):
        rng = np.random.default_rng(1)
        X = rng.random((60, 60, 4)).astype(np.float32); t = np.clip(X[..., 0] * 1.2 - 0.1, 0, 1)
        Xs, ts = self.R.sample_fraction([(X, t)], n=2500)
        m = self.R.train_fraction(Xs, ts)
        f = self.R.predict_fraction(m, X)
        self.assertLess(np.abs(f - t).mean(), 0.06)

    def test_weighted_change_year_ignores_weak_year(self):
        years = list(range(2000, 2008))
        p = np.array([.1, .1, .1, .7, .2, .9, .9, .9])[:, None, None]       # 2003 — шумный год из одной сцены
        w = np.ones_like(p); w[3] = 0.1
        y_plain = int(self.R.change_year(p, years, min_run=1)[0][0, 0])
        y_w = int(self.R.change_year(p, years, min_run=1, weights=w)[0][0, 0])
        self.assertEqual(y_w, 2005)
        self.assertLessEqual(y_plain, 2005)
        y_log = int(self.R.change_year(p, years, min_run=1, weights=w, cost='log')[0][0, 0])
        self.assertEqual(y_log, 2005)

    def test_sampling_without_complete_features(self):
        X = np.full((5, 5, 3), np.nan, np.float32); lab = np.zeros((5, 5), np.int8)
        Xs, ys = self.R.sample_training([(X, lab)], 10)
        self.assertEqual(Xs.shape, (0, 3)); self.assertEqual(len(ys), 0)
        Xf, tf = self.R.sample_fraction([(X, np.zeros((5, 5), np.float32))], 10)
        self.assertEqual(Xf.shape, (0, 3))

    def test_obs_weights(self):
        w = self.R.obs_weights(np.array([0, 1, 3, 6, 12]))
        self.assertAlmostEqual(float(w[1]), 0.4); self.assertAlmostEqual(float(w[3]), 1.0); self.assertTrue((np.diff(w) >= 0).all())

    def test_fraction_target_and_buildings(self):
        lab = np.array([[1, 1, 0, -1]], np.int8)
        src = dict(ghsl={2000: np.array([[.5, 0, 0, 0]]), 2020: np.array([[.5, .4, 0, 0]])},
                   esa={2020: np.array([[.8, .6, 0, .3]])}, wsf2019=np.array([[.6, .6, 0, .3]]),
                   osm=np.array([[0, 1, 0, 0]]), ms=np.array([[0, 0, 0, .2]]))
        t20 = self.R.fraction_target(2020, lab, src)
        self.assertAlmostEqual(float(t20[0, 0]), 0.7, places=5); self.assertEqual(float(t20[0, 2]), 0.0)
        t00 = self.R.fraction_target(2000, lab, src)
        self.assertAlmostEqual(float(t00[0, 0]), 0.7, places=5)      # GHSL не менялся
        self.assertTrue(np.isnan(t00[0, 1]))                          # построено после 2000
        self.assertEqual(self.R.buildings_mask(src).tolist(), [[False, True, False, True]])

    def test_sharp_labels_51(self):
        # 1 строка × 6 пикселей: дом | огород у дома | газон в посёлке | многоэтажка | поле | дом (только OSM)
        H, W = 1, 6
        spec = dict(ndvi=np.array([[.1, .6, .6, .1, .6, .1]], np.float32), mndwi=np.full((H, W), -.3, np.float32))
        g = np.array([[.3, .2, .05, .5, 0, .2]], np.float32)
        src = dict(ghsl={2000: g, 2020: g}, esa={2020: np.array([[.6, .05, 0, .9, 0, .1]])},
                   wsf2019=np.array([[.7, .1, 0, .9, 0, .2]]), osm=np.array([[1, 1, 0, 1, 0, 1]]),
                   osm_bfrac=np.array([[.3, 0, 0, .5, 0, .2]], np.float32))
        old = self.R.auto_labels(2020, spec, src)
        new = self.R.auto_labels(2020, spec, src, bld_frac_min=0.08, green_neg=True, sharp_now=True)
        self.assertEqual(int(new[0, 0]), self.R.BUILT); self.assertEqual(int(new[0, 3]), self.R.BUILT)
        self.assertEqual(int(new[0, 5]), self.R.BUILT)                 # подтверждён долей зданий OSM
        self.assertEqual(int(new[0, 2]), self.R.VEG)                   # газон в посёлке — растительность
        self.assertNotEqual(int(old[0, 2]), self.R.VEG)                # в 5.0 он оставался без метки
        self.assertNotEqual(int(new[0, 1]), self.R.BUILT)              # огород у дома больше не застройка
        self.assertEqual(self.R.buildings_mask(src, bld_frac_min=0.08).tolist(), [[True, False, False, True, False, True]])

    def test_calibrate_threshold_52(self):
        rng = np.random.default_rng(3)
        y = np.r_[np.ones(30), np.zeros(30)].astype(int)
        p = np.r_[rng.uniform(.3, .6, 30), rng.uniform(0, .3, 30)]      # застройка недооценена моделью
        t = self.R.calibrate_threshold(p, y)
        self.assertLess(t['t'], 0.5); self.assertGreaterEqual(t['acc'], t['acc0'])
        p2 = np.r_[rng.uniform(.7, 1, 30), rng.uniform(0, .3, 30)]      # и так хорошо — порог не трогаем
        self.assertEqual(self.R.calibrate_threshold(p2, y)['t'], 0.5)
        self.assertIsNone(self.R.calibrate_threshold(p[:5], y[:5]))

    def test_stratified_points_52(self):
        yr = np.zeros((60, 60), np.int16); yr[20:40, 20:40] = 2005
        r, c, k = self.R.stratified_points(yr, n=20, min_sep_px=3)
        self.assertEqual(sum(k), 10); self.assertEqual(len(k), 20)
        self.assertTrue(all((yr[a, b] > 0) == bool(z) for a, b, z in zip(r, c, k)))

    def test_uncertainty_points(self):
        p = np.full((50, 50), 0.95); p[10:40, 10:40] = 0.5
        r, c = self.R.uncertainty_points(p, n=10, min_sep_px=5)
        self.assertEqual(len(r), 10)
        self.assertTrue(all(10 <= a < 40 and 10 <= b < 40 for a, b in zip(r, c)))
        d = min((r[i] - r[j]) ** 2 + (c[i] - c[j]) ** 2 for i in range(10) for j in range(i))
        self.assertGreaterEqual(d, 25)

    def test_features_temporal_and_s2(self):
        rng = np.random.default_rng(2)
        comp = (rng.random((7, 20, 20)) * 0.3 + 0.02).astype(np.float32)
        spec = self.R.spectral(comp)
        ctx = self.R.series_context({2000: spec, 2001: spec})
        X, names = self.R.features(spec, ctx)
        self.assertIn('dist_last', names); self.assertTrue(np.allclose(X[..., names.index('dist_last')], 0, atol=1e-6))
        X2, n2 = self.R.features(spec, ctx, s2=np.zeros((len(self.R.S2_FEATURES), 20, 20), np.float32))
        self.assertEqual(len(n2), len(names) + len(self.R.S2_FEATURES))
        self.assertFalse(any(n.startswith('_') for n in n2))


class Imagery5(unittest.TestCase):
    def test_s2_cell_features(self):
        im = load('urban_auto_imagery')
        med = np.full((6, 6, 6), 0.1, np.float32)
        med[3, :3, :3] = 0.4                       # «трава» в левой верхней ячейке 3×3
        med[3, 3:, 3:] = 0.12; med[4, 3:, 3:] = 0.25   # «крыши/асфальт»
        f = im.s2_cell_features(med, 3)
        self.assertEqual(f.shape, (len(im.S2_FEATURES), 2, 2))
        i = im.S2_FEATURES.index
        self.assertGreater(f[i('s2_ndvi'), 0, 0], 0.5)
        self.assertEqual(f[i('s2_veg_frac'), 0, 0], 1.0)
        self.assertEqual(f[i('s2_imperv_frac'), 1, 1], 1.0)
        self.assertEqual(im.S2_FEATURES, load('urban_auto_rf').S2_FEATURES)


class Labels5(unittest.TestCase):
    def test_quadkey(self):
        lb = load('urban_auto_labels')
        self.assertEqual(lb.quadkey(-122.3321, 47.6062, 9), '021230030')


class Report5(unittest.TestCase):
    def test_report_and_olofsson(self):
        rep = load('urban_auto_report')
        proto = dict(version='5.0.0', finished='2026-11-01T01:00:00', min_run=3,
                     grid=dict(width=10, height=10, crs='EPSG:32642'), years_with_data=[2000, 2001],
                     series=[dict(year=2000, built_km2=1.0, new_built_km2=0, bare_km2=0.1, built_surface_km2=0.5),
                             dict(year=2001, built_km2=1.5, new_built_km2=0.5, bare_km2=0.1, built_surface_km2=0.7)],
                     models={'A': dict(kind='hgb', quality=0.97)}, label_sources={'ghsl_2000': 'ok'})
        acc = rep.olofsson([1, 1, 0, 0, 0, 1], [1, 1, 0, 0, 1, 1], [1, 1, 2, 2, 2, 1], {1: 2.0, 2: 8.0})
        self.assertAlmostEqual(acc['area_km2'], 2.0 + 8.0 / 3, places=6)
        with tempfile.TemporaryDirectory() as d:
            p = rep.write_report(os.path.join(d, 'r.html'), proto, title='Тест', accuracy={2001: acc})
            t = open(p, encoding='utf-8').read()
            self.assertIn('<svg', t); self.assertIn('Тест', t); self.assertIn('4,67', t)


if __name__ == '__main__':
    unittest.main()
