import unittest
from unittest.mock import patch, Mock
from urbanauto import urban_auto_labels as m

class GeographicSources(unittest.TestCase):
 def test_norway_actual_ghsl_tile(self):
  self.assertEqual(m._ghsl_tile(5.3,59.4),(3,19))
  self.assertEqual(m._ghsl_tile(5.3,59.0),(4,19))
  self.assertEqual(m._ghsl_tile(-0.005,51.5),(4,19))
 def test_cache_different_tiles_same_part(self):
  a='https://host/RegionName=France/quadkey=120220011/part-001.csv.gz'
  b='https://host/RegionName=Norway/quadkey=120021010/part-001.csv.gz'
  self.assertNotEqual(m._ms_cache_path('.',a),m._ms_cache_path('.',b))
  self.assertEqual(m._ms_cache_path('.',a),m._ms_cache_path('.',a+'?token=new'))
 def test_global_city_and_explicit_country(self):
  import tempfile
  tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
  self.enterContext(patch.object(m,'_user_cache',return_value=tmp.name))
  response=Mock();response.json.return_value=[dict(lon='5.73',lat='58.97',display_name='Stavanger',**{'class':'place'})]
  with patch('requests.get',return_value=response) as get:
   self.assertEqual(m.geocode('Stavanger')[:2],(5.73,58.97))
   self.assertNotIn('countrycodes',get.call_args.kwargs['params'])
   m.geocode('Stavanger',country='no')
   self.assertEqual(get.call_args.kwargs['params']['countrycodes'],'no')

if __name__=='__main__': unittest.main()
