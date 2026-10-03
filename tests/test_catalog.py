import hashlib
import unittest
from unittest.mock import Mock, patch

from dmmclient.backend.games import refresh_catalog, GameService, checked_product
from dmmclient.backend.session import DmmSession, DmmError


class CatalogTests(unittest.TestCase):
    def test_catalog_pages_are_not_dropped(self):
        session=Mock()
        session.request.side_effect=[{'total_hit_count':121,'list':[{'product_id':f'g{i}','title':f'Game {i}'} for i in range(120)]},
                                     {'total_hit_count':121,'list':[{'product_id':'g120','title':'Last game'}]}]
        with patch('dmmclient.backend.games.atomic_write'):
            result=refresh_catalog(session)
        self.assertEqual(len(result),121)
        self.assertIn('page=2',session.request.call_args.args[0])

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
