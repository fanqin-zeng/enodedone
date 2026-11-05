"""Offline lifecycle tests; fake sessions never contact remote hosts."""
import csv
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock,patch

from core import TaskStore,read_machines
from machine_state import CSV_FIELDS
from runner import SSHRunner
from connection_errors import SSHConnectionError
from templates import stage


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=TaskStore(Path(temporary.name)/'tasks');self.task=self.store.create('blank',count=3)
        self.path=Path(self.task['csv_file'])
        with self.path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader()
            writer.writerows({'Machine':'n'+str(i),'Info':f'192.0.2.{i+1}','User':'root',
                'Pwd':'fixture,secret','Connected':'Y'} for i in range(3))
        self.task['config']['master_ip']='192.0.2.1';self.store.save(self.task)
        self.runner=SSHRunner(self.store);self.manager=self.runner.connections;self.id=self.task['id']
        self.addCleanup(self.manager.close)
        self.entry=self.manager.entry(self.id);self.pool=self.entry['pool']
        for method in ('client','check_active','disconnect','close'):
            mock=patch.object(self.pool,method).start();self.addCleanup(patch.stopall)
            setattr(self,'fake_'+method,mock)
        self.fake_client.return_value=(MagicMock(),False)
        self.monitor=patch.object(self.manager,'_monitor').start()

    def connect(self):
        self.manager.connect(self.id);self.entry['thread'].join(timeout=3)
        self.assertFalse(self.entry['thread'].is_alive());return self.manager.state(self.id)

    def flags(self):return [h['Connected'] for h in read_machines(self.path)]

    def test_stale_markers_reset_without_any_network_or_credential_changes(self):
        self.assertEqual(self.flags(),['N']*3)
        self.fake_client.assert_not_called();self.fake_check_active.assert_not_called()
        self.assertTrue(all(self.entry['trust'].values()))
        with self.path.open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual([r['Pwd'] for r in csv.DictReader(stream)],['fixture,secret']*3)

    def test_connect_parallel_and_repeated_connect_reuses_sessions(self):
        barrier=threading.Barrier(3,timeout=2)
        self.fake_client.side_effect=lambda *args:(barrier.wait(),False)
        with patch.object(self.manager,'prepare') as prepare:
            state=self.connect();self.manager.connect(self.id)
        self.assertEqual(state['connected'],3);self.assertEqual(self.flags(),['Y']*3)
        self.assertEqual(self.fake_client.call_count,3);prepare.assert_not_called()
        self.fake_close.assert_not_called()

    def test_disconnect_then_reconnect_does_not_rebuild_public_keys(self):
        self.connect();self.manager.disconnect(self.id)
        self.assertEqual(self.flags(),['N']*3);self.fake_close.assert_called_once()
        with patch.object(self.manager,'prepare') as prepare:self.connect()
        prepare.assert_not_called();self.assertEqual(self.flags(),['Y']*3)

    def test_shrink_and_expand_keep_csv_bytes_and_reuse_existing_sessions(self):
        self.connect()
        original=self.path.read_bytes()
        task=self.store.load(self.id);task['machine_count']=2
        self.store.save(task)
        state=self.manager.state(self.id)
        self.assertEqual([cell['ip'] for cell in state['cells']],['192.0.2.1','192.0.2.2'])
        self.assertEqual(state['connected'],2)
        self.assertEqual(self.path.read_bytes(),original)
        task['machine_count']=3;self.store.save(task)
        self.assertEqual(self.manager.state(self.id)['connected'],3)
        self.assertEqual(self.path.read_bytes(),original)
        self.fake_close.assert_not_called()

    def test_disconnect_event_updates_only_broken_session_and_never_reconnects(self):
        self.connect();self.fake_client.reset_mock()
        client=MagicMock();transport=client.get_transport.return_value
        transport.is_active.return_value=False;transport.get_exception.return_value=ConnectionResetError('连接重置')
        self.pool.clients[('192.0.2.2','root')]=client
        event=(self.id,self.pool,'192.0.2.2','root',transport)
        self.manager.process_disconnect(event)
        state=self.manager.state(self.id)
        self.assertEqual(self.flags(),['Y','N','Y']);self.assertEqual(state['connected'],2)
        self.assertEqual(state['cells'][1]['status'],'failed');self.assertEqual(len(state['events']),1)
        self.assertIn('连接重置',state['events'][0]['message']);self.fake_client.assert_not_called()
        self.pool.clients.clear();self.manager.process_disconnect(event)
        self.assertEqual(len(self.manager.state(self.id)['events']),1)
        self.connect();self.fake_client.assert_called_once();self.assertEqual(self.flags(),['Y']*3)

    def test_delayed_event_from_old_connection_cannot_clear_replacement_session(self):
        self.connect();replacement=MagicMock();old=MagicMock();old.is_active.return_value=False
        self.pool.clients[('192.0.2.2','root')]=replacement
        self.manager.process_disconnect((self.id,self.pool,'192.0.2.2','root',old))
        self.assertEqual(self.flags(),['Y']*3);self.fake_disconnect.assert_not_called()

    def test_disconnect_event_can_retry_after_csv_write_failure(self):
        self.connect();client=MagicMock();transport=client.get_transport.return_value
        transport.is_active.return_value=False;transport.get_exception.return_value=ConnectionResetError('reset')
        self.pool.clients[('192.0.2.2','root')]=client
        event=(self.id,self.pool,'192.0.2.2','root',transport)
        with patch('connections.update_connected',side_effect=PermissionError('CSV open')),self.assertRaises(PermissionError):
            self.manager.process_disconnect(event)
        self.fake_disconnect.assert_not_called();self.assertEqual(self.flags(),['Y']*3)
        self.manager.process_disconnect(event)
        self.assertEqual(self.flags(),['Y','N','Y']);self.assertEqual(len(self.manager.state(self.id)['events']),1)

    def test_intentional_close_events_cannot_be_reported_as_unexpected_disconnects(self):
        self.connect();self.manager.disconnect(self.id)
        self.manager.process_disconnect((self.id,self.pool,'192.0.2.2','root',MagicMock()))
        self.assertEqual(self.flags(),['N']*3);self.assertFalse(self.manager.state(self.id)['events'])

    def test_session_not_marked_connected_if_transport_already_closed(self):
        self.fake_check_active.side_effect=SSHConnectionError(['192.0.2.1'],'session closed')
        state=self.connect();self.assertEqual(state['connected'],0);self.assertEqual(self.flags(),['N']*3)

    def test_close_during_run_is_rejected(self):
        self.connect();self.runner.runs[self.id]={'status':'running'}
        with self.assertRaisesRegex(ValueError,'停止任务'):self.manager.disconnect(self.id)
        self.fake_close.assert_not_called();self.assertEqual(self.flags(),['Y']*3)

    def test_unknown_keys_prepare_before_connect_without_publishing_y(self):
        self.entry['trust']={key:False for key in self.entry['trust']}
        def prepare(*args):
            self.assertEqual(self.flags(),['N']*3);self.fake_client.assert_not_called()
        with patch.object(self.manager,'prepare',side_effect=prepare) as called:self.connect()
        called.assert_called_once();self.assertEqual(self.flags(),['Y']*3)

    def test_run_auto_connects_and_keeps_sessions_after_task(self):
        task=self.store.load(self.id);task['nodes']=task['nodes'][:1];self.store.save(task)
        def execute(*args,**kwargs):
            self.assertEqual(self.flags(),['Y']*3)
            self.assertEqual(self.fake_client.call_count,3)
            return ''
        with patch('runner.SSHTransport.run',side_effect=execute):
            self.runner.start(self.id);self.runner.threads[self.id].join(timeout=3)
        self.assertEqual(self.runner.state(self.id)['status'],'success')
        self.fake_close.assert_not_called();self.assertEqual(self.flags(),['Y']*3)

    def test_connection_failure_prevents_pipeline_execution(self):
        self.fake_client.side_effect=SSHConnectionError(['192.0.2.1'],'connection refused')
        with patch('runner.SSHTransport.run') as execute:
            self.runner.start(self.id);self.runner.threads[self.id].join(timeout=3)
        execute.assert_not_called();self.assertEqual(self.runner.state(self.id)['status'],'failed')
        self.assertEqual(self.flags(),['N']*3)

    def test_runtime_scp_error_cannot_change_live_session_state(self):
        self.connect();hosts=self.store.validate(self.task)
        transport=self.runner.transport(self.id,self.task,hosts)
        def remote(*args):
            args[4]('@@ENODEDONE_CONNECTION@@{"ips":["192.0.2.2"],"message":"Permission denied (publickey)"}')
            raise RuntimeError('scp failed')
        with patch.object(self.pool,'run',side_effect=remote),self.assertRaises(RuntimeError):
            transport.run('192.0.2.1','copy',callback=lambda line:None)
        self.assertEqual(self.flags(),['Y']*3);self.assertEqual(self.manager.state(self.id)['connected'],3)

    def test_first_actual_parallel_failure_is_preserved_and_other_ranks_are_stopped(self):
        node=stage('sample');node.update(script_ip='127.0.0.1',script_path='${TASK_DIR}/scripts/sample.py')
        task=self.store.load(self.id);task['nodes']=[node];self.store.save(task)
        barrier=threading.Barrier(3,timeout=2)
        def execute(identifier,task,hosts,node,current,cell,code,stop):
            barrier.wait()
            if cell['rank']==1:raise SSHConnectionError([cell['ip']],'原始错误：F2 会话已退出')
            while not stop.is_set():threading.Event().wait(.01)
            raise InterruptedError('同节点其他机器失败，终止本机执行')
        with patch.object(self.runner,'execute',side_effect=execute):
            self.runner.start(self.id);self.runner.threads[self.id].join(timeout=3)
        run=self.runner.state(self.id)
        self.assertEqual(run['status'],'failed')
        self.assertEqual([cell['status'] for cell in run['stages'][0]['cells']],['stopped','failed','stopped'])
        self.assertIn('原始错误：F2 会话已退出',run['console'][-1])
        self.assertEqual(self.flags(),['Y']*3)

    def test_missing_persistent_session_does_not_silently_reconnect(self):
        with patch.object(self.pool,'client') as connect,self.assertRaises(SSHConnectionError):
            self.pool.run('192.0.2.1',{'require_existing_session':True,'host_users':{'192.0.2.1':'root'}},
                          'command','',None,None,lambda:None)
        connect.assert_not_called()


if __name__=='__main__':unittest.main()
