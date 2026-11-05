"""Task-local Connected persistence and precise invalidation; all SSH execution is replaced."""
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock,patch

from core import TaskStore,read_machines
from machine_state import CSV_BASE_FIELDS,CSV_FIELDS,read_rows,update_connected
from connection_errors import CONNECTION_PREFIX,SSHConnectionError,connection_print,report_connection_failure
from runner import SSHRunner,SSHTransport
from templates import stage


class MachineStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=TaskStore(self.root/'tasks')
        self.task=self.store.create('blank',count=3)
        self.csv=Path(self.task['csv_file'])
        self.rows=[{'Machine':'n'+str(i),'Info':f'192.0.2.{i+1}','User':'root','Pwd':' fixture,secret\n"quoted" ','Connected':'Y'} for i in range(3)]
        with self.csv.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader();writer.writerows(self.rows)
        self.task['config']['master_ip']='192.0.2.1';self.store.save(self.task)
        self.hosts=self.store.validate(self.task)
        self.runner=SSHRunner(self.store);self.runner.runs[self.task['id']]={'console':[]}

    def transport(self):
        transport=self.runner.transport(self.task['id'],self.task,self.hosts)
        transport.pool=None  # Replace execution with mocked native subprocesses, never contact SSH.
        return transport

    def process(self,status,stdout='',stderr=''):
        process=MagicMock();process.returncode=status;process.poll.return_value=status
        process.stdin=io.StringIO();process.stdout=io.StringIO(stdout);process.stderr=io.StringIO(stderr)
        return process

    def test_legacy_schema_gains_connected_without_changing_credentials(self):
        with self.csv.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_BASE_FIELDS);writer.writeheader()
            writer.writerows({key:row[key] for key in CSV_BASE_FIELDS} for row in self.rows)
        self.assertTrue(all(host['Connected']=='N' for host in read_machines(self.csv)))
        update_connected(self.csv,{('192.0.2.2','root'):True})
        fields,rows=read_rows(self.csv)
        self.assertEqual(fields,CSV_FIELDS)
        self.assertEqual([row['Connected'] for row in rows],['N','Y','N'])
        self.assertEqual([{key:row[key] for key in CSV_BASE_FIELDS} for row in rows],
                         [{key:row[key] for key in CSV_BASE_FIELDS} for row in self.rows])
        self.assertNotIn('fixture,secret',json.dumps(read_machines(self.csv)))

    def test_concurrent_invalidations_do_not_lose_other_nodes_updates(self):
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(lambda host:update_connected(self.csv,{(host['Info'],'root'):False}),self.hosts))
        self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['N']*3)

    def test_atomic_replace_failure_preserves_original_csv(self):
        before=self.csv.read_bytes()
        with patch('machine_state.os.replace',side_effect=PermissionError('CSV is open')),self.assertRaises(PermissionError):
            update_connected(self.csv,{('192.0.2.2','root'):False})
        self.assertEqual(before,self.csv.read_bytes());self.assertFalse(list(self.csv.parent.glob('.machines-*')))

    def test_all_connected_bootstrap_does_not_launch_any_process(self):
        current={'events':[],'cells':[{'rank':i,'status':'pending','output':[]} for i in range(3)]}
        with patch('runner.subprocess.Popen') as launch:
            self.runner.bootstrap_nodes(self.task['id'],self.task,self.hosts,stage('nodes_ssh'),current)
        launch.assert_not_called();self.assertTrue(all(cell['status']=='success' for cell in current['cells']))

    def test_key_bootstrap_does_not_change_session_state(self):
        update_connected(self.csv,{('192.0.2.2','root'):False,('192.0.2.3','root'):False})
        node=stage('nodes_ssh');self.task['nodes']=[node];self.store.save(self.task)
        current={'events':[],'cells':[{'rank':i,'status':'pending','output':[]} for i in range(3)]}
        output='\n'.join('@@ENODEDONE@@'+json.dumps({'rank':rank,'status':status,'message':'offline'})
                         for rank,status in ((1,'success'),(2,'failed')))+'\n'
        process=MagicMock();process.returncode=1;process.poll.return_value=1
        process.stdout=io.StringIO(output)
        self.runner.stops[self.task['id']]=threading.Event()
        with patch('runner.subprocess.Popen',return_value=process),self.assertRaises(RuntimeError):
            self.runner.bootstrap_nodes(self.task['id'],self.task,self.hosts,node,current)
        self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['Y','N','N'])

    def test_native_connection_error_does_not_change_session_flags(self):
        with patch('runner.subprocess.Popen',return_value=self.process(255,stderr='ssh: connect to host 192.0.2.2 port 22: Connection refused')):
            with self.assertRaises(SSHConnectionError):self.transport().run('192.0.2.2','command')
        self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['Y']*3)
        self.assertEqual([row['Pwd'] for row in read_rows(self.csv)[1]],[row['Pwd'] for row in self.rows])

    def test_script_errors_and_socket_path_errors_leave_connection_marks_unchanged(self):
        for status,message in ((1,'Permission denied: /data/train.log'),
            (255,'unix_listener: path too long for Unix domain socket')):
            with self.subTest(message=message),patch('runner.subprocess.Popen',return_value=self.process(status,stderr=message)):
                with self.assertRaises(RuntimeError):self.transport().run('192.0.2.2','command')
            self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['Y']*3)

    def test_nested_copy_failure_does_not_change_session_flags(self):
        event=CONNECTION_PREFIX+json.dumps({'ips':['192.0.2.2','192.0.2.3'],'message':'Permission denied (publickey)'})+'\n'
        with patch('runner.subprocess.Popen',return_value=self.process(1,stdout=event,stderr='scp failed')):
            with self.assertRaises(RuntimeError):self.transport().run('192.0.2.1','orchestrate',callback=lambda line:None)
        self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['Y']*3)

    def test_connected_updates_do_not_invalidate_live_run_signature(self):
        identifier=self.task['id'];self.runner.runs.clear()
        with patch.object(self.runner,'_run'):
            run=self.runner.start(identifier);self.runner.threads[identifier].join(timeout=2)
        update_connected(self.csv,{('192.0.2.2','root'):False})
        self.assertEqual(self.runner.state(identifier)['id'],run['id'])

    def test_external_csv_is_copied_into_task_before_status_updates(self):
        external=self.root/'external.csv';external.write_bytes(self.csv.read_bytes());before=external.read_bytes()
        self.task['csv_path']=str(external);self.store.save(self.task)
        local=self.store.localize_csv(self.task)
        update_connected(self.store.csv_file(local),{('192.0.2.2','root'):False})
        self.assertEqual(local['csv_path'],'machines.csv');self.assertEqual(external.read_bytes(),before)

    def test_stale_identity_update_does_not_clear_a_different_user(self):
        update_connected(self.csv,{('192.0.2.2','alice'):False})
        self.assertEqual([host['Connected'] for host in read_machines(self.csv)],['Y']*3)

    def test_concurrent_connection_reports_remain_complete_json_lines(self):
        def report(index):
            connection_print('ordinary output '+str(index))
            report_connection_failure(['192.0.2.'+str(index+1)],'Connection refused')
        with redirect_stdout(io.StringIO()) as output:
            with ThreadPoolExecutor(max_workers=8) as workers:list(workers.map(report,range(16)))
        events=[json.loads(line[len(CONNECTION_PREFIX):]) for line in output.getvalue().splitlines() if line.startswith(CONNECTION_PREFIX)]
        self.assertEqual(len(events),16)


if __name__=='__main__':unittest.main()
