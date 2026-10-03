import hashlib
import unittest
from unittest.mock import Mock, patch

from dmmclient.backend.games import refresh_catalog, save_cover, GameService, checked_product, cover_path
from dmmclient.backend.session import DmmSession, DmmError


class CatalogTests(unittest.TestCase):
    def test_catalog_pages_are_not_dropped(self):
        session=Mock()
        session.request.side_effect=[{'total_hit_count':121,'list':[{'product_id':f'g{i}','title':f'Game {i}'} for i in range(120)]},
                                     {'total_hit_count':121,'list':[{'product_id':'g120','title':'Last game'}]},
                                     {'total_hit_count':1,'list':[{'product_id':'r18game','title':'Adult game'}]}]
        with patch('dmmclient.backend.games.atomic_write'):
            result=refresh_catalog(session)
        self.assertEqual(len(result),122)
        self.assertTrue(all(not item['adult'] for item in result if item['product'].startswith('g')))
        self.assertEqual([item for item in result if item['product']=='r18game'][0]['adult'],True)
        self.assertIn('isAdult=true',session.request.call_args_list[-1].args[0])

    def test_adult_list_failure_keeps_general_catalog(self):
        session=Mock()
        session.request.side_effect=[{'total_hit_count':1,'list':[{'product_id':'g0','title':'Game 0'}]},DmmError('HTTP 503')]
        with patch('dmmclient.backend.games.atomic_write'):
            result=refresh_catalog(session)
        self.assertEqual([item['product'] for item in result],['g0'])
        self.assertTrue(session.progress.called)

    def test_cover_is_cached_from_detail_page(self):
        http=Mock()
        page=Mock(status_code=200,text='<meta property="og:image" content="https://pics.dmm.co.jp/x.jpg&amp;a=1">')
        image=Mock(status_code=200,content=b'\xff\xd8\xff'+b'x'*600)
        http.get.side_effect=[page,image]
        with patch('dmmclient.backend.games.atomic_write') as write:
            path=save_cover(http,'somegame',__import__('pathlib').Path('.'))
        self.assertEqual(path.name,'somegame.jpg')
        url=http.get.call_args_list[1].args[0]
        self.assertEqual(url,'https://pics.dmm.co.jp/x.jpg&a=1')

    def test_cover_rejects_bad_product(self):
        http=Mock()
        with self.assertRaises(DmmError): save_cover(http,'../evil',__import__('pathlib').Path('.'))

    def test_game_path_cannot_escape(self):
        for product in ['../other','a/b','C:\\game','.',None]:
            with self.assertRaises(DmmError): checked_product(product)

    def test_new_catalog_game_can_request_installation(self):
        session=Mock();session.device={}
        session.request.side_effect=[{'game_type':'GCL'}, {'product_id':'new_game','file_list_url':'/manifest','sign':'signature'}]
        with patch('dmmclient.backend.games.catalog',return_value=[{'product':'new_game','title':'New'}]),patch('dmmclient.backend.games.records',return_value={}):
            data=GameService(session,'new_game').request_info(True)
        self.assertEqual(data['product_id'],'new_game')

    def test_browser_only_game_not_launched_as_executable(self):
        session=Mock();session.device={};session.request.return_value={'game_type':'GBR'}
        with self.assertRaises(DmmError): GameService(session,'priconner').request_info(True)

    def test_platform_identity_does_not_need_a_game(self):
        session=DmmSession.__new__(DmmSession);session.identity=None;session.save=Mock()
        session.request=Mock(return_value={'open_id':'account-fixture'})
        session.verify_game_identity()
        self.assertEqual(session.identity,'dmm:'+hashlib.sha256(b'account-fixture').hexdigest())
        self.assertEqual(session.request.call_args.args[0],'/v5/userinfo')

    def test_wrong_platform_account_preserves_identity(self):
        session=DmmSession.__new__(DmmSession)
        session.identity='dmm:'+hashlib.sha256(b'original').hexdigest();original=session.identity
        session.save=Mock();session.request=Mock(return_value={'open_id':'other'})
        with self.assertRaises(DmmError): session.verify_game_identity()
        self.assertEqual(session.identity,original);session.save.assert_not_called()


if __name__=='__main__': unittest.main()
