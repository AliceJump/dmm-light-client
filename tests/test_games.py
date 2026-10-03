import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dmmclient.backend.games import GameService
from dmmclient.backend.session import DmmError


class GameTests(unittest.TestCase):
    def test_does_not_accept_different_product(self):
        session=Mock(); session.device={}; session.request.side_effect=[{'game_type':'GCL'},{'product_id':'gakumas'}]
        with self.assertRaises(DmmError): GameService(session,'priconner').request_info(True)

    def test_installer_requires_support(self):
        session=Mock(); session.device={}; session.request.side_effect=[{'game_type':'GCL'},{'product_id':'priconner','has_installer':True}]
        with self.assertRaises(DmmError): GameService(session,'priconner').request_info(True)

    def test_independent_game_identity_payload(self):
        session=Mock(); session.device={'user_os':'win'}
        session.request.side_effect=[{'game_type':'GCL'},{'product_id':'priconner','file_list_url':'/manifest','sign':'signature'}]
        GameService(session,'priconner').request_info(False)
        path,payload=session.request.call_args.args
        self.assertEqual(path,'/v5/r2/launch/cl')
        self.assertEqual(payload['product_id'],'priconner')
        self.assertEqual(payload['launch_type'],'LIB')
        session.accept_identity.assert_not_called()

    def test_update_and_refetch_before_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'game.exe').write_bytes(b'fixture')
            session=Mock(); session.cancel_check=lambda:None
            service=GameService(session,'priconner')
            old={'product_id':'priconner','latest_version':'1','exec_file_name':'game.exe','execute_args':'old'}
            fresh=dict(old,execute_args='new')
            events=[]
            service.request_info=Mock(side_effect=lambda installing:(events.append('fetch') or (old if len(events)==1 else fresh)))
            plan=Mock(); plan.pending.return_value=[]
            service.plan=Mock(return_value=plan)
            updater=Mock(); updater.run.side_effect=lambda:events.append('update')
            service.updater=Mock(return_value=updater); service.stopped=Mock()
            with patch('dmmclient.backend.games.records',return_value={'priconner':{'root':str(root)}}), patch('dmmclient.backend.games.atomic_write'), patch('dmmclient.backend.games.subprocess.Popen',side_effect=lambda *a,**kw:(events.append('launch') or Mock(pid=1))) as spawn:
                service.execute('launch')
            self.assertEqual(events,['fetch','update','fetch','launch'])
            self.assertEqual(spawn.call_args.args[0][1:],["new"])


if __name__=='__main__': unittest.main()
