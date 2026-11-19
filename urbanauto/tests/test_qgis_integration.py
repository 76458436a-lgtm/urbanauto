"""Run using QGIS Python; automatically skipped in plain Python environments."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('qgis'), 'Requires QGIS Python')
class QgisIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from qgis.core import QgsApplication
        cls.app = QgsApplication.instance()
        cls.owns_app = cls.app is None
        if cls.owns_app:
            cls.app = QgsApplication([],False); cls.app.initQgis()
        from processing.core.Processing import Processing
        Processing.initialize()
        from urbanauto.urban_auto_provider import UrbanAutoProvider
        reg = QgsApplication.processingRegistry()
        cls.provider = None
        if reg.providerById('urbanauto') is None:      # провайдер мог остаться от другого набора тестов
            cls.provider = UrbanAutoProvider()
            reg.addProvider(cls.provider)

    @classmethod
    def tearDownClass(cls):
        import gc
        from qgis.core import QgsApplication, QgsProject
        QgsProject.instance().removeAllMapLayers()
        if cls.provider is not None:
            QgsApplication.processingRegistry().removeProvider(cls.provider)
        cls.provider = None
        gc.collect()
        if cls.owns_app: cls.app.exitQgis()

    def fixture(self, folder):
        import numpy as np
        from osgeo import gdal,osr
        folder=Path(folder);folder.mkdir(exist_ok=True)
        srs=osr.SpatialReference();srs.ImportFromEPSG(32641)
        rng=np.random.default_rng(42)
        for year,size in [(2000,24),(2010,36),(2025,52)]:
            y,x=np.indices((128,128));urban=(x>=40)&(x<40+size)&(y>=40)&(y<40+size)
            meadow=(x<28); water=(y<8)
            arrays={
                'NDBI':np.where(urban,.18,np.where(meadow,-.2,-.5)),
                'BSI':np.where(urban,.22,np.where(meadow,-.1,-.35)),
                'NDVI':np.where(urban,.16,np.where(meadow,.4,.7)),
                'MNDWI_max':np.where(water,.55,-.3)}
            for key,arr in arrays.items():
                arr=arr+rng.normal(0,.01,arr.shape)
                # One missing square inside the urban footprint must never become built-up.
                arr[50:54,50:54]=-9999
                if key=='BSI':arr=arr[::2,::2];res=60
                else:res=30
                ds=gdal.GetDriverByName('GTiff').Create(str(folder/f'{key}_{year}.tif'),arr.shape[1],arr.shape[0],1,gdal.GDT_Float32)
                ds.SetProjection(srs.ExportToWkt());ds.SetGeoTransform((410000,res,0,6670000,0,-res))
                ds.GetRasterBand(1).WriteArray(arr);ds.GetRasterBand(1).SetNoDataValue(-9999);ds=None

    def test_autonomous_batch_growth_coverage_and_resume(self):
        from urbanauto.urban_auto_batch import run_folder
        from urbanauto.urban_auto_temporal import rasterize_layer,_grid
        from osgeo import gdal
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            inputs=Path(tmp)/'inputs';outputs=Path(tmp)/'outputs';self.fixture(inputs)
            result=run_folder(str(inputs),str(outputs),last_mode='accept',log=lambda s:None)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(len(result['rows']),3)
            self.assertGreater(result['rows'][-1]['growth_km2'],0)
            self.assertEqual(result['rows'][-1]['temporal_status'],'provisional_last')
            rd=Path(result['run_dir']);cov=rd/'2025'/'Valid_2025.tif'
            ds=gdal.Open(str(cov));valid=ds.GetRasterBand(1).ReadAsArray()==1;ds=None
            pred=rasterize_layer(str(rd/'2025'/'Urban_Auto_2025.gpkg'),_grid(str(cov)))
            self.assertFalse((pred&~valid).any())
            self.assertFalse(pred[51,51])
            stamp=(rd/'2025'/'run_protocol_2025.json').stat().st_mtime_ns
            restored=run_folder(str(inputs),str(outputs),resume=str(rd),last_mode='accept',log=lambda s:None)
            self.assertEqual(restored['status'],'complete')
            self.assertEqual((rd/'2025'/'run_protocol_2025.json').stat().st_mtime_ns,stamp)
            ds=gdal.Open(str(inputs/'NDBI_2025.tif'),gdal.GA_Update)
            ds.SetMetadataItem('changed','1');ds=None
            with self.assertRaisesRegex(ValueError,'изменились'):
                run_folder(str(inputs),str(outputs),resume=str(rd),last_mode='accept',log=lambda s:None)

    def test_strict_grid_rejects_misaligned_indices(self):
        from urbanauto.urban_auto_batch import run_folder
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            inputs=Path(tmp)/'inputs';self.fixture(inputs)
            with self.assertRaisesRegex(ValueError,'Сетка'):
                run_folder(str(inputs),str(Path(tmp)/'outputs'),auto_align=False,log=lambda s:None)

    def test_processing_provider_and_accuracy(self):
        from qgis.core import QgsApplication,QgsVectorLayer,QgsRasterLayer
        import processing
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            inputs=Path(tmp)/'inputs';self.fixture(inputs)
            self.assertIsNotNone(QgsApplication.processingRegistry().algorithmById('urbanauto:batch_folder'))
            result=processing.run('urbanauto:batch_folder',{'INPUT_FOLDER':str(inputs),
                'OUTPUT_FOLDER':str(Path(tmp)/'out'),'YEARS':'2025','TEMPORAL':False})
            self.assertEqual(result['STATUS'],'complete')
            rd=Path(result['RUN_FOLDER'])/'2025'
            vector=QgsVectorLayer(str(rd/'Urban_Auto_2025.gpkg'),'result','ogr')
            grid=QgsRasterLayer(str(rd/'Valid_2025.tif'),'grid')
            result=processing.run('urbanauto:accuracy_assessment',{'RESULT':vector,'REFERENCE':vector,
                'MODE':1,'GRID':grid,'BLOCK_M':0,'OUTPUT_FOLDER':str(Path(tmp)/'accuracy')})
            self.assertAlmostEqual(result['OA'],1.)
            with self.assertRaises(Exception):
                processing.run('urbanauto:accuracy_assessment',{'RESULT':vector,'REFERENCE':vector,
                'MODE':0,'GRID':grid,'URBAN_BUFFER':300,'BACKGROUND_DISTANCE':30,
                'OUTPUT_FOLDER':str(Path(tmp)/'bad_accuracy')})

    def test_single_year_processing_and_temporal_processing(self):
        import processing
        from urbanauto.urban_auto_batch import run_folder
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            inputs=Path(tmp)/'inputs';self.fixture(inputs)
            result=processing.run('urbanauto:urban_auto_processing', {
                'NDBI_LAYER':str(inputs/'NDBI_2025.tif'),
                'BSI_LAYER':str(inputs/'BSI_2025.tif'),
                'NDVI_LAYER':str(inputs/'NDVI_2025.tif'),
                'MNDWI_LAYER':str(inputs/'MNDWI_max_2025.tif'),
                'CENTER':'61.38,60.14 [EPSG:4326]', 'RADIUS':8.,
                'YEAR':2025,'OUTPUT_FOLDER':str(Path(tmp)/'single')})
            self.assertGreater(result['AREA_KM2'],0)
            result=run_folder(str(inputs),str(Path(tmp)/'out'),temporal=False,log=lambda s:None)
            root=Path(result['run_dir'])
            processing.run('urbanauto:temporal_consistency', {
                'LAYERS':[str(root/str(y)/f'Urban_Auto_{y}.gpkg') for y in (2000,2010,2025)],
                'REF':str(root/'2000'/'Valid_2000.tif'),'LAST_MODE':1,'OUTPUT_FOLDER':str(Path(tmp)/'temporal')})
            protocol=json.loads((Path(tmp)/'temporal'/'temporal_consistency_protocol.json').read_text())
            self.assertEqual(protocol['rows'][-1]['temporal_status'],'provisional_last')
            self.assertGreater(protocol['rows'][-1]['growth_km2'],0)


    def test_wsf_rejects_uncovered_edge_but_accepts_valid_zero(self):
        import shutil
        from unittest.mock import patch
        from osgeo import gdal, osr
        from qgis.core import QgsProcessingException
        import processing
        from urbanauto import urban_auto_wsf_download as wsf
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crs = osr.SpatialReference()
            crs.ImportFromEPSG(32641)
            def create(name, width):
                path = root / name
                ds = gdal.GetDriverByName('GTiff').Create(str(path), width, 2, 1, gdal.GDT_Byte)
                ds.SetGeoTransform((500000, 30, 0, 6650000, 0, -30))
                ds.SetProjection(crs.ExportToWkt())
                ds.GetRasterBand(1).Fill(0)
                ds = None
                return path
            ref = create('reference.tif', 4)
            tile = create('partial.tif', 2)
            output = root / 'result'
            from qgis.core import QgsRasterLayer
            memory_path = '/vsimem/urban_auto_exp6_reference.tif'
            gdal.Translate(memory_path, str(ref))
            self.addCleanup(gdal.Unlink, memory_path)
            ref_layer = QgsRasterLayer(memory_path, 'reference')
            params = dict(REFERENCE=ref_layer, YEAR=2025, OUTPUT=str(output))
            item = dict(id='tile', assets={'raster': dict(href='https://example.invalid/wsf.tif')})
            with patch.object(wsf, 'discover', return_value=[item]), patch.object(wsf, 'download_file',
                    side_effect=lambda url, path, feedback: shutil.copyfile(tile, path)):
                with self.assertRaises(QgsProcessingException):
                    processing.run('urbanauto:wsf_download', params)
                self.assertFalse((output / 'WSF_Tracker_2025_aligned.tif').exists())
                tile = ref
                processing.run('urbanauto:wsf_download', params)
                manifest = json.loads((output / 'wsf_source_manifest.json').read_text(encoding='utf8'))
                self.assertEqual(manifest['observed_values'], [0])
                self.assertEqual(manifest['coverage_fraction'], 1.0)
            del params, ref_layer
            import gc
            gc.collect()


if __name__=='__main__':unittest.main()
