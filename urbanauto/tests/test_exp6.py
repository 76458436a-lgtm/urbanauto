"""Regression checks for missing observations and incomplete settlement coverage."""
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import numpy as np
from urbanauto import urban_auto_imagery as imagery


class LandsatCoverage(unittest.TestCase):
    def composite(self, stacks):
        features = [dict(id=str(i), properties=dict(datetime='2025-07-01', platform='landsat-8',
                                                    **{'eo:cloud_cover': 0})) for i in range(len(stacks))]
        with patch.object(imagery, 'search_scenes', return_value=features), \
             patch.object(imagery._Token, 'get', return_value='test'), \
             patch.object(imagery, 'scene_stack', side_effect=stacks), warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            return imagery.annual_composite(None, None, 2025, workers=1, log=lambda _: None)

    def test_median_ignores_uncovered_pixels(self):
        first = np.full((6, 1, 2), np.nan, dtype=np.float32)
        first[:, 0, 0] = 1
        arr, info = self.composite([(first, 1), (first * 3, 1)])
        np.testing.assert_array_equal(arr[:, 0, 0], [2] * 7)
        np.testing.assert_array_equal(arr[:, 0, 1], [imagery.NODATA] * 6 + [0])
        self.assertEqual(info['clear_fraction'], 0.5)

    def test_no_valid_scenes(self):
        arr, info = self.composite([(np.full((6, 1, 1), np.nan), 0)])
        self.assertIsNone(arr)
        self.assertEqual(info['scenes'], [])


class MicrosoftCache(unittest.TestCase):
    def test_failed_replacement_preserves_complete_cache(self):
        from urbanauto.urban_auto_labels import _store_download
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.csv'
            _store_download(path, b'old')
            with patch('os.replace', side_effect=OSError), self.assertRaises(OSError):
                _store_download(path, b'partial')
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.iterdir()), [path])

class TemporalEdge(unittest.TestCase):
    def test_last_observation_is_opt_in(self):
        from urbanauto.urban_auto_rf import change_year
        years = [2021, 2022, 2023, 2024, 2025]
        prob = np.array([.05, .05, .05, .05, .95])[:, None, None]
        self.assertEqual(change_year(prob, years, min_run=3)[0].item(), 0)
        self.assertEqual(change_year(prob, years, min_run=3, edge_policy='adaptive')[0].item(), 2025)
        prob[:] = np.nan
        self.assertEqual(change_year(prob, years, min_run=3, edge_policy='adaptive')[0].item(), -1)

class SentinelSeries(unittest.TestCase):
    def test_native_composite_keeps_grid_and_nodata(self):
        from osgeo import gdal
        grid = ((500000, 6650000, 500020, 6650010), 2, 1, 'EPSG:32641')
        med = np.full((6, 1, 2), 0.3, dtype=np.float32)
        count = np.array([[2, 0]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp, patch.object(imagery, 's2_median',
                return_value=(med, count, dict(scenes=3, clear=0.5))) as fetch:
            imagery.download_s2_composites(None, grid, [2017, 2018], tmp, log=lambda _: None)
            self.assertFalse((Path(tmp) / 'Composite_2017.tif').exists())
            ds = gdal.Open(str(Path(tmp) / 'Composite_2018.tif'))
            self.assertEqual(ds.GetMetadataItem('HARMONIZED'), 'S2_L2A')
            self.assertEqual(ds.GetGeoTransform()[1], 10)
            np.testing.assert_array_equal(ds.ReadAsArray()[:, 0, 1], [imagery.NODATA] * 6 + [0])
            ds = None
            imagery.download_s2_composites(None, grid, [2018], tmp, log=lambda _: None)
            fetch.assert_called_once()
