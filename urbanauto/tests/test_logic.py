"""Dependency-light regression tests; GDAL/QGIS integration is separate.

Functions are compiled from their actual module AST, not reimplemented here.
"""
import ast
import math
from pathlib import Path
import tempfile
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def functions(file, names, **globals_):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8'))
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = dict(np=np, math=math, **globals_)
    exec(compile(tree, str(ROOT / file), 'exec'), env)
    return env


temporal = functions('urban_auto_temporal.py', {'temporal_consistency'},
                     LAST_MODES=('backward', 'accept'))['temporal_consistency']
confusion = functions('urban_auto_accuracy.py', {'confusion'})['confusion']
checkerboard = functions('urban_auto_accuracy.py', {'checkerboard'})['checkerboard']
otsu = functions('urban_auto_thresholds.py', {'otsu_threshold'})
balance = functions('urban_auto_thresholds.py', {'kmeans_balance_threshold'}, BALANCE_MIN=-1., BALANCE_MAX=.3)
discover = functions('urban_auto_batch.py', {'discover_indices'}, Path=Path, re=__import__('re'))['discover_indices']


class TemporalRegression(unittest.TestCase):
    def test_final_growth_survives(self):
        masks = {2000:np.array([1,0,0],bool),2005:np.array([1,1,0],bool),2010:np.array([1,1,1],bool)}
        out = temporal(masks, list(masks), last_mode='accept')
        self.assertEqual(int((out[2010] & ~out[2005]).sum()), 1)

    def test_backward_last_slice_needs_two_observations(self):
        # пиксель 2: виден в 2005 и 2010 -> принят в 2010; пиксель 3: только в 2010 -> нижняя оценка его не включает
        masks = {2000:np.array([1,0,0,0],bool),2005:np.array([1,1,1,0],bool),2010:np.array([1,1,1,1],bool)}
        lo = temporal(masks, list(masks), min_confirm=2, last_mode='backward')
        hi = temporal(masks, list(masks), min_confirm=2, last_mode='accept')
        self.assertEqual(lo[2010].tolist(), [True, True, True, False])
        self.assertEqual(hi[2010].tolist(), [True, True, True, True])
        self.assertFalse((lo[2010] & ~hi[2010]).any())

    def test_previous_alias_kept_for_4_2_projects(self):
        masks = {2000:np.array([1,0],bool),2005:np.array([1,1],bool)}
        np.testing.assert_array_equal(temporal(masks, list(masks), last_mode='previous')[2005],
                                      temporal(masks, list(masks), last_mode='backward')[2005])

    def test_isolated_early_false_alarm_removed(self):
        masks = {y:np.array([y==2000,True],bool) for y in (2000,2005,2010)}
        out = temporal(masks, list(masks))
        self.assertFalse(out[2000][0]); self.assertTrue(out[2000][1])

    def test_monotonicity_and_inputs_not_modified(self):
        rng = np.random.default_rng(42)
        masks = {y:rng.random((12,13))>.6 for y in (2000,2005,2010,2015)}
        copy = {y:m.copy() for y,m in masks.items()}
        out = temporal(masks,list(masks))
        for a,b in zip(sorted(out),sorted(out)[1:]):
            self.assertFalse((out[a]&~out[b]).any())
        for y in masks: np.testing.assert_array_equal(masks[y],copy[y])

    def test_empty_duplicate_wrong_shape_and_old_mode_fail(self):
        with self.assertRaises(ValueError): temporal({},[])
        m={2000:np.ones((2,2),bool),2005:np.ones((3,2),bool)}
        with self.assertRaises(ValueError): temporal(m,list(m))
        with self.assertRaises(ValueError): temporal({2000:m[2000]},[2000,2000])
        with self.assertRaises(ValueError): temporal({2000:m[2000]},[2000],last_mode='unknown')

    def test_integer_masks_rejected(self):
        with self.assertRaises(ValueError): temporal({2000:np.ones(3,int)},[2000])


class AccuracyRegression(unittest.TestCase):
    def test_osm_published_matrix(self):
        area=np.array([11.67,1.,2.2,195.42])
        result=confusion(np.array([1,0,1,0],bool),np.array([1,1,0,0],bool),np.array([0,0,1,1],bool),area)
        self.assertAlmostEqual(result['kappa'], .8713239948, places=9)
        self.assertAlmostEqual(result['OA'], .9847829188, places=9)

    def test_overlap_rejected(self):
        a=np.ones((3,3),bool)
        with self.assertRaises(ValueError): confusion(a,a,a,np.ones((3,3)))

    def test_invalid_area_rejected(self):
        a=np.ones(2,bool)
        with self.assertRaises(ValueError): confusion(a,a,~a,np.array([1.,np.nan]))

    def test_no_observations_not_fake_accuracy(self):
        a=np.zeros(2,bool)
        self.assertIsNone(confusion(a,a,a,np.ones(2)))

    def test_projected_feet_match_metres(self):
        metres=checkerboard((40,40),(0,30,0,0,0,-30),False,1000)
        feet=checkerboard((40,40),(0,30/.3048,0,0,0,-30/.3048),False,1000,.3048)
        np.testing.assert_array_equal(metres,feet)


class ThresholdRegression(unittest.TestCase):
    def test_otsu_separates_two_modes(self):
        arr=np.concatenate([np.linspace(-.8,-.5,500),np.linspace(.1,.4,500)])
        otsu['_read_band']=lambda *_:(arr,np.ones(arr.size,bool))
        self.assertTrue(-.51 <= otsu['otsu_threshold']('unused') <= .11)

    def test_otsu_no_data_fails(self):
        otsu['_read_band']=lambda *_:(np.zeros(40),np.zeros(40,bool))
        with self.assertRaises(ValueError): otsu['otsu_threshold']('unused')

    def test_boreal_balance_uses_real_negative_clusters(self):
        rng=np.random.default_rng(4)
        data=np.vstack([rng.normal(c,.015,(200,2)) for c in [(-.5,.6),(-.3,.4),(-.1,.3)]])
        balance['_read_band']=lambda p,*_: (data[:,0 if p=='ndbi' else 1],np.ones(len(data),bool))
        threshold,info=balance['kmeans_balance_threshold']('ndbi','ndvi')
        self.assertTrue(-.8 < threshold < -.3)
        self.assertTrue(all(s>10 for s in info['sizes']))

    def test_small_urban_cluster_not_lost_in_forest(self):
        rng=np.random.default_rng(7)
        data=np.vstack([rng.normal(c,.01,(n,2)) for c,n in
                        [((-.5,.7),8500),((-.2,.4),1200),((.18,.16),300)]])
        balance['_read_band']=lambda p,*_: (data[:,0 if p=='ndbi' else 1],np.ones(len(data),bool))
        threshold,info=balance['kmeans_balance_threshold']('ndbi','ndvi')
        self.assertGreater(threshold,-.5)
        self.assertLess(min(info['sizes']),500)


class DiscoveryRegression(unittest.TestCase):
    def fixtures(self,d,year=2025):
        for prefix in ('NDBI','BSI','NDVI','MNDWI_max'):
            (Path(d)/f'{prefix}_{year}.tif').touch()

    def test_discover_complete_year(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixtures(d); self.assertEqual(set(discover(d)[2025]),{'ndbi','bsi','ndvi','mndwi'})

    def test_missing_index_fails(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixtures(d);(Path(d)/'BSI_2025.tif').unlink()
            with self.assertRaises(ValueError): discover(d)

    def test_duplicates_fail(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixtures(d);(Path(d)/'sub').mkdir();(Path(d)/'sub'/'NDBI_2025.tif').touch()
            with self.assertRaises(ValueError): discover(d)


if __name__=='__main__':
    unittest.main()
