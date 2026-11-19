import unittest
from wsf_validation import parse_built_codes, validate_coverage, stac_next

class TestWSFValidation(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(parse_built_codes('1, 2'), [1, 2])
        for value in ('', '1,,2', '-1', '1,1', 'abc', '65536'):
            with self.subTest(value=value), self.assertRaises(ValueError): parse_built_codes(value)
    def test_coverage(self):
        validate_coverage(100, 100)
        for a,b in ((99,100),(0,100),(0,0)):
            with self.assertRaises(ValueError): validate_coverage(a,b)
    def test_stac_pagination(self):
        self.assertEqual(stac_next({'href':'?page=2'}, 'https://x.test/search', {'bbox':[1]}), ('https://x.test/search?page=2', None))
        self.assertEqual(stac_next({'href':'/search','method':'POST','body':{'token':'a'}}, 'https://x.test/search', {}), ('https://x.test/search', {'token':'a'}))
        with self.assertRaises(ValueError): stac_next({'href':'/search','method':'DELETE'},'https://x.test/search',{})
if __name__ == '__main__': unittest.main()
