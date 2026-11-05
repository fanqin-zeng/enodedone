"""Offline installer checks; no key generation or SSH connections."""
import ast
import io
import json
from pathlib import Path
import tempfile
import threading
import struct
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from nodes_ssh import HELPERS, REMOTE, merge_authorized, merge_config, redact, event, parallel_phase, remember_host_key, main
from templates import stage
from core import TaskStore
from runner import SSHRunner


class NodesSSHTests(unittest.TestCase):
    def test_runner_starts_task_owned_local_script_with_no_password_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store=TaskStore(Path(directory));task=store.create(count=1)
            node=stage('nodes_ssh');task['nodes']=[node];store.save(task)
            runner=SSHRunner(store);runner.stops[task['id']]=threading.Event()
            cell={'rank':0,'status':'pending','output':[]};current={'cells':[cell]}
            process=MagicMock();process.returncode=0;process.poll.return_value=0
            process.stdout=io.StringIO('@@ENODEDONE@@'+json.dumps({'rank':0,'status':'success','message':'verified'})+'\n')
            with patch('runner.subprocess.Popen',return_value=process) as launch:
                runner.bootstrap_nodes(task['id'],task,[{'Info':'192.0.2.1','Machine':'n1'}],node,current)
            self.assertEqual(Path(launch.call_args.args[0][-1]),store.locations[task['id']].parent/'scripts/nodes_ssh.py')
            self.assertIn('utf8',launch.call_args.args[0])
            self.assertEqual(launch.call_args.kwargs['env']['PYTHONIOENCODING'],'utf-8')
            self.assertEqual(launch.call_args.kwargs['env']['PYTHONUTF8'],'1')
            payload=json.loads(process.stdin.write.call_args.args[0])
            self.assertEqual(set(payload),{'hosts','ssh_user','csv_path','machine_count'})
            self.assertEqual(payload['machine_count'],1)
            self.assertNotIn('Pwd',json.dumps(payload))
            self.assertEqual(cell['status'],'success')

    def test_chinese_event_can_be_decoded_as_utf8_even_with_gbk_child_stdout(self):
        message='已配置节点公钥与 known_hosts，等待互联验证'
        output=io.BytesIO();stream=io.TextIOWrapper(output,encoding='gbk')
        with redirect_stdout(stream):event(0,'running',message)
        stream.flush()
        wire=output.getvalue().decode('utf-8')
        self.assertEqual(json.loads(wire[len('@@ENODEDONE@@'):])['message'],message)

    def test_parallel_phase_is_bounded_and_keeps_rank_order(self):
        barrier=threading.Barrier(4,timeout=5);lock=threading.Lock();active=0;maximum=0
        def operation(rank,host):
            nonlocal active,maximum
            with lock:active+=1;maximum=max(maximum,active)
            barrier.wait()
            with lock:active-=1
            return rank
        with patch('nodes_ssh.MAX_NODE_WORKERS',4),redirect_stdout(io.StringIO()):
            result=parallel_phase([str(i) for i in range(12)],operation,'offline')
        self.assertEqual(maximum,4);self.assertEqual(result,list(range(12)))

    def test_parallel_failure_reports_rank_and_redacts_password(self):
        def fail(rank,host):raise RuntimeError('test-secret')
        with patch('nodes_ssh.event') as report,redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,r'\[REDACTED\]'):
                parallel_phase(['test-host'],fail,'offline',['test-secret'])
        self.assertEqual(report.call_args.args,(0,'failed','[REDACTED]'))

    def test_concurrent_first_host_keys_are_merged_and_changes_rejected(self):
        import paramiko
        def public(byte):
            name=b'ssh-ed25519';value=bytes([byte])*32
            return paramiko.Ed25519Key(data=struct.pack('>I',len(name))+name+struct.pack('>I',len(value))+value)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'known_hosts';lock=threading.Lock();key=public(1)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i:remember_host_key(paramiko,path,f'192.0.2.{i+1}',key,lock),range(8)))
            loaded=paramiko.HostKeys(str(path));self.assertEqual(len(loaded),8)
            with self.assertRaises(paramiko.BadHostKeyException):
                remember_host_key(paramiko,path,'192.0.2.1',public(2),lock)
            self.assertEqual(paramiko.HostKeys(str(path)).lookup('192.0.2.1')['ssh-ed25519'],key)

    def test_embedded_stage_has_no_parameters_or_external_script_reference(self):
        node=stage('nodes_ssh')
        self.assertEqual(node['name'],'检查与建立NODES SSH公钥连接')
        self.assertEqual(node['params'],{})
        self.assertEqual(node['script_ip'],'127.0.0.1')
        self.assertNotIn('setup_remote_ssh',node['script_content'])
        ast.parse(node['script_content'],feature_version=(3,8))
        ast.parse('import base64\n'+HELPERS+"\nPAYLOAD='e30='\n"+REMOTE,feature_version=(3,8))

    def test_authorized_keys_preserved_and_deduplicated_by_key_identity(self):
        original='# existing\nfrom="10.0.0.1" ssh-ed25519 AAA existing restriction\nssh-rsa BBB old'
        result=merge_authorized(original,['ssh-ed25519 AAA new-comment','ssh-ed25519 CCC node-key'])
        self.assertTrue(result.startswith(original))
        self.assertEqual(result.count('AAA'),1)
        self.assertIn('ssh-ed25519 CCC node-key',result)
        self.assertEqual(merge_authorized(result,['ssh-ed25519 CCC changed-comment']),result)

    def test_config_is_idempotent_preserves_other_hosts_and_uses_task_user(self):
        original='Host personal\n    User someone\n    HostName example.invalid\n'
        args=(['192.0.2.1','192.0.2.2'],'alice',Path('/home/alice/.ssh/node-key'),Path('/home/alice/.ssh/nodes-known'),Path('/home/alice/.ssh/known_hosts'))
        updated=merge_config(original,*args)
        self.assertTrue(updated.endswith(original))
        self.assertIn('User alice',updated)
        self.assertIn('StrictHostKeyChecking yes',updated)
        self.assertNotIn('StrictHostKeyChecking no',updated)
        self.assertEqual(merge_config(updated,*args),updated)
        mixed=merge_config('',args[0],{'192.0.2.1':'alice','192.0.2.2':'bob'},*args[2:])
        self.assertIn('HostName 192.0.2.1\n    User alice',mixed)
        self.assertIn('HostName 192.0.2.2\n    User bob',mixed)
        with self.assertRaisesRegex(ValueError,'Malformed'):
            merge_config(updated.replace('# END enodedone','# BROKEN enodedone',1),*args)

    def test_passwords_are_redacted(self):
        self.assertEqual(redact('error longer-secret secret',['secret','longer-secret']),'error [REDACTED] [REDACTED]')

    def test_connected_csv_rows_skip_network_and_only_pending_rows_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);managed=root/'.ssh/enodedone';managed.mkdir(parents=True)
            key_path=managed/'id_ed25519';key_path.write_text('offline placeholder')
            csv_path=root/'machines.csv';csv_path.write_text('Machine,Info,User,Pwd,Connected\na,192.0.2.1,root,,Y\nb,192.0.2.2,root,,N\nc,192.0.2.3,root,,Y\n')
            context={'hosts':[{'Machine':'a','Info':'192.0.2.1'},{'Machine':'b','Info':'192.0.2.2'},{'Machine':'c','Info':'192.0.2.3'}],
                     'csv_path':str(csv_path)}
            actions=[]
            def remote(client,payload):
                actions.append(payload)
                return 'ok'
            with patch('nodes_ssh.Path.home',return_value=root), \
                 patch('nodes_ssh.local_key',return_value=(MagicMock(),'ssh-ed25519 LOCAL',key_path)), \
                 patch('nodes_ssh.private_permissions'),patch('paramiko.SSHClient') as clients, \
                 patch('nodes_ssh.remote',side_effect=remote),redirect_stdout(io.StringIO()):
                server=clients.return_value.get_transport.return_value.get_remote_server_key.return_value
                server.get_base64.return_value='host-public';server.get_name.return_value='ssh-ed25519'
                main(context,[])
                self.assertEqual([action['action'] for action in actions],['verify'])
                self.assertEqual(actions[0]['hosts'],['192.0.2.2'])
                self.assertEqual({call.kwargs['hostname'] for call in clients.return_value.connect.call_args_list},{'192.0.2.1','192.0.2.2'})
                from machine_state import update_connected
                update_connected(csv_path,{('192.0.2.2','root'):True})
                actions.clear();clients.reset_mock();main(context,[])
                self.assertFalse(actions);clients.assert_not_called()

    def test_pending_subset_errors_keep_original_rank(self):
        with patch('nodes_ssh.event') as report,redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                parallel_phase(['test-host'],lambda rank,host:(_ for _ in ()).throw(RuntimeError('failed')),'offline',ranks=[5])
        self.assertEqual(report.call_args.args[0],5)

    def test_pending_node_with_missing_public_key_still_performs_setup(self):
        import paramiko
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);managed=root/'.ssh/enodedone';managed.mkdir(parents=True)
            key_path=managed/'id_ed25519';key_path.write_text('offline placeholder')
            csv_path=root/'machines.csv';csv_path.write_text('Machine,Info,User,Pwd,Connected\na,192.0.2.1,root,temporary-secret,N\nb,192.0.2.2,root,temporary-secret,N\n')
            context={'hosts':[{'Info':'192.0.2.1'},{'Info':'192.0.2.2'}],'csv_path':str(csv_path)}
            installed=set();actions=[]
            def factory():
                client=MagicMock()
                def connect(**kwargs):
                    client.host=kwargs['hostname']
                    if client.host not in installed and kwargs.get('password')!='temporary-secret':
                        raise paramiko.AuthenticationException('public key missing')
                client.connect.side_effect=connect
                server=client.get_transport.return_value.get_remote_server_key.return_value
                server.get_base64.return_value='host-public';server.get_name.return_value='ssh-ed25519'
                return client
            def remote(client,payload):
                actions.append(payload['action'])
                if payload['action']=='prepare':installed.add(client.host);return json.dumps({'public':'ssh-ed25519 AAA'})
                return 'ok'
            with patch('nodes_ssh.Path.home',return_value=root), \
                 patch('nodes_ssh.local_key',return_value=(MagicMock(),'ssh-ed25519 LOCAL',key_path)), \
                 patch('nodes_ssh.private_permissions'),patch('paramiko.SSHClient',side_effect=factory), \
                 patch('nodes_ssh.remote',side_effect=remote),redirect_stdout(io.StringIO()) as output:
                main(context,[])
            self.assertEqual(actions.count('prepare'),2);self.assertEqual(actions.count('install'),2);self.assertEqual(actions.count('verify'),2)
            self.assertNotIn('temporary-secret',output.getvalue())


if __name__=='__main__':unittest.main()
