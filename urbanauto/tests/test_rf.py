"""Тесты классификации 4.4 (numpy/scipy/scikit-learn, без QGIS)."""
import importlib.util
import sys
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
class ChangeYear(unittest.TestCase):
    def setUp(self):
        self.R = load('urban_auto_rf')

    def test_clear_step(self):
        years = list(range(2000, 2010))
        p = np.array([.1, .1, .2, .1, .9, .8, .9, .95, .9, .9])[:, None, None]
        yr, conf = self.R.change_year(p, years)
        self.assertEqual(int(yr[0, 0]), 2004)
        self.assertGreater(float(conf[0, 0]), 0.8)

    def test_single_noisy_year_is_not_construction(self):
        years = list(range(2000, 2010))
        p = np.array([.1, .1, .9, .1, .1, .1, .1, .1, .1, .1])[:, None, None]
        yr, _ = self.R.change_year(p, years)
        self.assertEqual(int(yr[0, 0]), 0)

    def test_last_year_needs_min_run(self):
        years = list(range(2000, 2006))
        p = np.array([.1, .1, .1, .1, .1, .95])[:, None, None]
        self.assertEqual(int(self.R.change_year(p, years, min_run=2)[0][0, 0]), 0)
        self.assertEqual(int(self.R.change_year(p, years, min_run=1)[0][0, 0]), 2005)

    def test_built_from_start_and_gaps(self):
        years = list(range(2000, 2006))
        p = np.array([.9, np.nan, .8, np.nan, .9, .9])[:, None, None]
        self.assertEqual(int(self.R.change_year(p, years)[0][0, 0]), 2000)
        q = np.full((6, 1, 1), np.nan)
        self.assertEqual(int(self.R.change_year(q, years)[0][0, 0]), -1)

    def test_monotonic_masks(self):
        rng = np.random.default_rng(1)
        years = list(range(2000, 2012))
        yr, _ = self.R.change_year(rng.random((12, 20, 20)), years)
        for a, b in zip(years, years[1:]):
            self.assertFalse((self.R.built_mask_for(yr, a) & ~self.R.built_mask_for(yr, b)).any())


@unittest.skipUnless(HAVE, 'нужны scipy и scikit-learn')
class Refine45(unittest.TestCase):
    def setUp(self):
        self.R = load('urban_auto_rf')

    def test_roof_under_osm_building_counts_as_built(self):
        pb = np.full((4, 1, 2), 0.2, np.float32); pbare = np.full((4, 1, 2), 0.7, np.float32)
        osm = np.array([[1, 0]])
        pe = self.R.effective_built_prob(pb, pbare, osm)
        self.assertTrue((pe[:, 0, 0] >= 0.89).all()); self.assertTrue((pe[:, 0, 1] < 0.3).all())

    def test_weak_and_thin_new_growth_removed(self):
        years = list(range(2000, 2008))
        yr = np.zeros((9, 9), np.int16); yr[2:7, 2:7] = 2000; yr[4, 7:9] = 2004   # тонкий отросток
        yr[0, 0] = 2003                                                               # одиночный слабый пиксель
        p = np.full((8, 9, 9), 0.9, np.float32); p[:, 0, 0] = 0.55
        y2, rep = self.R.refine_new_growth(yr, p, years, min_mean=0.6)
        self.assertNotIn('low_mean', self.R.refine_new_growth(yr, p, years)[1])
        self.assertEqual(int(y2[0, 0]), 0); self.assertEqual(int(y2[4, 8]), 0)
        self.assertTrue((y2[2:7, 2:7] == 2000).all())
        self.assertEqual(rep['low_mean'], 1)

    def test_edge_growth_attached_to_city_kept(self):
        years = list(range(2000, 2008))
        yr = np.zeros((10, 10), np.int16); yr[2:7, 2:7] = 2000; yr[2:7, 7:9] = 2005    # полоса 2 пикселя у края
        p = np.full((8, 10, 10), 0.9, np.float32)
        y2, _ = self.R.refine_new_growth(yr, p, years)
        self.assertTrue((y2[2:7, 7:9] == 2005).all())


@unittest.skipUnless(HAVE, 'нужны scipy и scikit-learn')
class LabelsAndModel(unittest.TestCase):
    def setUp(self):
        self.R = load('urban_auto_rf')

    def synthetic(self, year):
        H = W = 60
        rng = np.random.default_rng(year)
        comp = np.zeros((7, H, W), np.float32)
        veg = np.array([.03, .05, .04, .30, .15, .07]); built = np.array([.08, .10, .11, .16, .22, .18])
        sand = np.array([.20, .25, .28, .32, .38, .30]); water = np.array([.04, .05, .03, .02, .01, .005])
        cls = np.zeros((H, W), int); cls[10:25, 10:25] = 1; cls[40:50, 5:20] = 2; cls[30:60, 40:60] = 3
        for c, s in enumerate((veg, built, sand, water)):
            m = cls == c
            comp[:6, m] = (s[:, None] * (1 + 0.05 * rng.standard_normal((6, m.sum())))).astype(np.float32)
        comp[6] = 5
        ghsl = (cls == 1).astype(np.float32) * 0.6
        return comp, cls, dict(ghsl={2000: ghsl, 2020: ghsl}, osm=(cls == 1).astype(np.uint8),
                              esa={2020: (cls == 1).astype(np.float32)}, wsf={2000: (cls == 1).astype(np.float32)})

    def test_labels_and_classes(self):
        comp, cls, src = self.synthetic(2020)
        spec = self.R.spectral(comp)
        lab = self.R.auto_labels(2020, spec, src)
        self.assertTrue((lab[cls == 1] != self.R.VEG).all())
        self.assertGreater((lab == self.R.BUILT).sum(), 100)
        self.assertGreater((lab == self.R.BARE).sum(), 50)
        self.assertGreater((lab == self.R.WATER).sum(), 100)
        self.assertFalse(((lab == self.R.BUILT) & (cls != 1)).any())

    def test_protect_excludes_points(self):
        comp, cls, src = self.synthetic(2020)
        prot = self.R.protect_mask(cls.shape, [15], [15], 3)
        lab = self.R.auto_labels(2020, self.R.spectral(comp), src, protect=prot)
        self.assertTrue((lab[12:19, 12:19] == -1).all())

    def test_rf_separates_sand_from_built(self):
        per = []
        for y in (2000, 2010, 2020):
            comp, cls, src = self.synthetic(y)
            spec = self.R.spectral(comp)
            ctx = self.R.series_context({y: spec})
            X, _ = self.R.features(spec, ctx)
            per.append((X, self.R.auto_labels(2020, spec, src)))
        Xt, yt = self.R.sample_training(per, 300)
        rf = self.R.train(Xt, yt, n_estimators=30)
        comp, cls, src = self.synthetic(2030)
        spec = self.R.spectral(comp)
        X, _ = self.R.features(spec, self.R.series_context({2030: spec}))
        pr = self.R.predict(rf, X)
        self.assertGreater(pr[15, 15, self.R.BUILT], 0.5)
        self.assertLess(pr[45, 12, self.R.BUILT], 0.5)
        self.assertGreater(pr[45, 12, self.R.BARE], 0.5)


class Imagery(unittest.TestCase):
    def test_platforms(self):
        im = load('urban_auto_imagery')
        self.assertIn('landsat-5', im.platforms_for(2005))
        self.assertEqual(im.platforms_for(2012), ['landsat-7'])
        self.assertEqual(im.platforms_for(2020), ['landsat-8'])
        self.assertIn('landsat-9', im.platforms_for(2023))


if __name__ == '__main__':
    unittest.main()
