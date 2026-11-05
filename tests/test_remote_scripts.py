"""Exercise distribution planning with an in-memory peer, never with SSH or Docker."""
import ast
import base64
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
import threading
import sys
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class RemoteScriptTests(unittest.TestCase):
    def test_sample_parameters_and_validation_without_waiting(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            scope['LOCAL']=Path(folder)/'sample'
            with patch.dict(os.environ,DELAY_MIN_SECONDS='7',DELAY_MAX_SECONDS='9'), \
                 patch.object(scope['random'],'randint',return_value=8) as randint, \
                 patch.object(scope['time'],'sleep') as sleep, \
                 patch.object(scope['os'],'getloadavg',return_value=(1.,2.,3.),create=True), \
                 redirect_stdout(io.StringIO()):
                scope['sample']()
            randint.assert_called_once_with(7,9);sleep.assert_called_once_with(8)
            self.assertEqual(json.loads((scope['LOCAL']/'metric.json').read_text())['delay_seconds'],8)
            with patch.dict(os.environ,DELAY_MIN_SECONDS='5',DELAY_MAX_SECONDS='1'), \
                 patch.object(scope['time'],'sleep') as sleep:
                with self.assertRaisesRegex(ValueError,'DELAY_MAX_SECONDS'):scope['sample']()
                sleep.assert_not_called()
            with patch.dict(os.environ,PING_COUNT='0'):
                with self.assertRaisesRegex(ValueError,'PING_COUNT'):scope['ping']()

    def namespace(self, folder, existing):
        source=Path(folder)/'image.tar';source.write_text('unit test input')
        context={'rank':0,'hosts':[{'Info':f'192.0.2.{i+1}','Machine':f'n{i}'} for i in range(8)],
                 'master_run':folder,'run_id':'unit-test','image_path':str(source),
                 'image_dest':'/tmp/enodedone/unit-test/image.tar','artifact_kind':'docker',
                 'ssh_user':'root','timeout':10,'shared_dir':folder}
        namespace={}
        with patch.dict(os.environ,ENODEDONE_CONTEXT=base64.b64encode(json.dumps(context).encode()).decode()):
            exec(compile(Path('remote_runtime.py').read_text(encoding='utf-8'),'remote_runtime.py','exec'),namespace)
        transfers=[];events=[]
        namespace['LOCAL']=Path(folder)/'local'
        namespace['ssh_control_options']=lambda run_id:['-o','ControlPath='+str(Path(folder)/'ssh/%C')]
        verified={ip for ip,digest in existing.items() if digest=='a'*64}
        lock=threading.Lock()
        def peer(ip, code):
            tree=ast.parse(code)  # Also verifies the dynamically generated remote Python.
            if "print('MISSING')" in code:return existing.get(ip,'MISSING')
            if 'SEED_SOURCE=' in code:verified.add('192.0.2.1')
            if 'TRANSFER_ARGS=' in code:
                assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='TRANSFER_ARGS')
                args=ast.literal_eval(assignment.value)
                source=args[-1].split('@')[1].split(':')[0]
                with lock:
                    self.assertIn(source,verified,'Receiver must never copy from an unverified source')
                    transfers.append((source,ip));verified.add(ip)
            return ''
        namespace['_base_command']=namespace['command']
        namespace.update(peer=peer,command=lambda *args,**kwargs:'a'*64,event=lambda *args:events.append(args))
        return namespace,transfers,events

    def test_control_path_budgets_expansion_and_openssh_random_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            with patch.dict(scope,Path=PurePosixPath):
                paths=[]
                for uid in (0,1000,4294967295):
                    for run_id in ('41b9e6c886cf4ec3b6492fa011746540','x'*10000):
                        path=str(scope['ssh_control_path'](run_id,uid))
                        expanded=path.replace('%C','a'*40)+'.'+'a'*16
                        self.assertLess(len(expanded.encode()),100)
                        self.assertNotIn(run_id,path)
                        paths.append(path)
                self.assertEqual(len(paths),len(set(paths)))

    def test_control_directory_is_private_and_rejects_foreign_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            control=Path(folder)/'owner'/'run'/'%C'
            uid=Path(folder).stat().st_uid
            # Exercise the real helper instead of the distribution transport fixture.
            helper={**scope};exec(scope['SSH_CONTROL_HELPERS'],helper)
            with patch.dict(helper,ssh_control_path=lambda run_id,user:control),patch.object(os,'geteuid',return_value=uid,create=True):
                options=helper['ssh_control_options']('run')
            self.assertIn('ControlPath='+str(control),options)
            with patch.dict(helper,ssh_control_path=lambda run_id,user:control),patch.object(os,'geteuid',return_value=uid+1,create=True):
                with self.assertRaisesRegex(RuntimeError,'owned by the current user'):
                    helper['ssh_control_options']('run')

    def test_missing_images_compile_and_use_verified_doubling_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,events=self.namespace(folder,{})
            with redirect_stdout(io.StringIO()) as output:scope['distribute']()
            self.assertEqual(len(transfers),7)
            rounds=[line for line in output.getvalue().splitlines() if line.startswith('分发轮次')]
            self.assertEqual(len(rounds),3)
            for line,count in zip(rounds,[1,2,4]):
                edges=ast.literal_eval(line.split(': ',1)[1]);self.assertEqual(len(edges),count)
            self.assertEqual(sum(e[1]=='success' for e in events),8)

    def test_conflict_aborts_before_copy_and_cached_images_are_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,_=self.namespace(folder,{'192.0.2.3':'b'*64})
            with redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'SHA 冲突'):scope['distribute']()
            self.assertEqual(transfers,[])
            scope,transfers,_=self.namespace(folder,{f'192.0.2.{i+1}':'a'*64 for i in range(8)})
            with redirect_stdout(io.StringIO()):scope['distribute']()
            self.assertEqual(transfers,[])

    def test_all_destinations_are_preflighted_concurrently_before_copying(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,_=self.namespace(folder,{})
            original=scope['peer'];barrier=threading.Barrier(8,timeout=3)
            def peer(ip,code):
                if "print('MISSING')" in code:barrier.wait()
                return original(ip,code)
            scope['peer']=peer
            with redirect_stdout(io.StringIO()):scope['distribute']()
            self.assertEqual(len(transfers),7)

    def test_local_sha_snapshot_skips_master_peer_checks_and_applies_only_changed_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,events=self.namespace(folder,{'192.0.2.1':'a'*64,'192.0.2.2':'a'*64})
            scope['C']['artifact_kind']='archive'
            snapshot={'version':1,'source_ip':'192.0.2.1','image_path':scope['C']['image_path'],
                'image_dest':scope['C']['image_dest'],'sha256':'a'*64,'available':[0,1],
                'pending':list(range(2,8)),'replace':{'2':'b'*64}}
            peer=scope['peer'];applied=[];receiver=[]
            def observed(ip,code):
                self.assertNotIn("print('MISSING')",code)
                if 'with tarfile.open(' in code:applied.append(ip)
                if 'TRANSFER_ARGS=' in code:receiver.append((ip,code))
                return peer(ip,code)
            scope['peer']=observed
            with patch.dict(os.environ,ENODEDONE_DISTRIBUTION_PREFLIGHT=json.dumps(snapshot)),redirect_stdout(io.StringIO()):scope['distribute']()
            self.assertEqual(len(transfers),6)
            self.assertEqual(set(applied),{f'192.0.2.{i}' for i in range(3,9)})
            self.assertTrue(any(ip=='192.0.2.3' and repr('b'*64) in code for ip,code in receiver))
            self.assertEqual(sum(event[1]=='success' for event in events),8)

    def test_local_sha_snapshot_of_all_cached_images_never_calls_peer_or_applies(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            scope['C']['artifact_kind']='archive'
            snapshot={'version':1,'source_ip':'192.0.2.1','image_path':scope['C']['image_path'],
                'image_dest':scope['C']['image_dest'],'sha256':'a'*64,'available':list(range(8)),
                'pending':[],'replace':{}}
            def forbidden(*args):self.fail('Cached distribution must not invoke SSH peers or commands')
            scope.update(peer=forbidden,command=forbidden)
            with patch.dict(os.environ,ENODEDONE_DISTRIBUTION_PREFLIGHT=json.dumps(snapshot)),redirect_stdout(io.StringIO()):scope['distribute']()

    def test_docker_file_cache_hit_still_verifies_loaded_images_without_copying(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,events=self.namespace(folder,{})
            snapshot={'version':1,'source_ip':'192.0.2.1','image_path':scope['C']['image_path'],
                'image_dest':scope['C']['image_dest'],'sha256':'a'*64,'available':list(range(8)),
                'pending':[],'replace':{}}
            checked=[]
            def peer(ip,code):
                self.assertIn('\napply_docker(',code);checked.append(ip);return 'cached image IDs verified'
            scope['peer']=peer
            with patch.dict(os.environ,ENODEDONE_DISTRIBUTION_PREFLIGHT=json.dumps(snapshot)),redirect_stdout(io.StringIO()):scope['distribute']()
            self.assertEqual(set(checked),{f'192.0.2.{i}' for i in range(1,9)})
            self.assertEqual(transfers,[]);self.assertEqual(sum(event[1]=='success' for event in events),8)

    def test_independent_branches_continue_and_received_nodes_finish_before_slow_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,transfers,events=self.namespace(folder,{})
            original=scope['peer'];original_event=scope['event']
            second_applied=threading.Event();fast_branch=threading.Event();release_slow=threading.Event()
            def peer(ip,code):
                if 'TRANSFER_ARGS=' in code:
                    if ip=='192.0.2.3':
                        self.assertTrue(second_applied.wait(3),'A completed receiver must finish before unrelated transfers')
                        self.assertTrue(release_slow.wait(3),'Scheduler must not wait for an entire doubling round')
                    if ip=='192.0.2.6':fast_branch.set();release_slow.set()
                return original(ip,code)
            def event(rank,status,message):
                original_event(rank,status,message)
                if rank==1 and status=='success':second_applied.set()
            scope.update(peer=peer,event=event)
            with redirect_stdout(io.StringIO()):scope['distribute']()
            self.assertTrue(fast_branch.is_set());self.assertEqual(len(transfers),7)
            self.assertEqual(sum(event[1]=='success' for event in events),8)

    def test_atomic_publish_verifies_once_and_never_overwrites_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            incoming=Path(folder)/'incoming';target=Path(folder)/'target'
            incoming.write_bytes(b'new');target.write_bytes(b'old')
            with self.assertRaisesRegex(RuntimeError,'禁止覆盖'):
                scope['checked_publish'](incoming,target,hashlib.sha256(b'new').hexdigest())
            self.assertEqual(target.read_bytes(),b'old');self.assertFalse(incoming.exists())
            incoming.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError,'SHA'):
                scope['checked_publish'](incoming,Path(folder)/'absent','a'*64)
            self.assertFalse((Path(folder)/'absent').exists())

    def test_verified_shared_markers_are_cleaned_locally_without_ssh(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            for rank in range(8):scope['marker'](rank).write_text('unit-test:'+str(rank))
            unrelated=Path(folder)/'keep';unrelated.write_text('user data')
            with patch.dict(scope,peer=lambda *args:(_ for _ in ()).throw(AssertionError('Unexpected SSH'))):
                scope['cleanup_shared'](verified=True)
            self.assertFalse(list(Path(folder).glob('*.marker')));self.assertEqual(unrelated.read_text(),'user data')
            changed=scope['marker'](0);changed.write_text('not this run')
            scope['cleanup_shared'](verified=True)
            self.assertEqual(changed.read_text(),'not this run')

    def test_docker_load_is_reused_only_while_image_references_still_match(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            namespace={'Path':Path,'json':json,'os':os,'tempfile':tempfile,'subprocess':__import__('subprocess')}
            exec(scope['DOCKER_HELPER'],namespace)
            image_id='sha256:'+'a'*64
            def result(stdout):return SimpleNamespace(returncode=0,stdout=stdout,stderr='')
            responses=[result('Loaded image: sample:test\n'),result(image_id+'\n'),result(image_id+'\n'),
                       result('sha256:'+'b'*64+'\n'),result('Loaded image: sample:test\n'),result(image_id+'\n')]
            with patch('pathlib.Path.home',return_value=Path(folder)),patch('subprocess.run',side_effect=responses) as command,redirect_stdout(io.StringIO()):
                for _ in range(3):namespace['apply_docker']('/image.tar','a'*64,10)
            loads=[call for call in command.call_args_list if call.args[0][:2]==['docker','load']]
            self.assertEqual(len(loads),2,'A retagged/deleted image must invalidate the successful-load cache')

    def test_master_ssh_failure_emits_precise_connection_event(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            for code,message,should_report in ((255,'Connection refused',True),(1,'Permission denied: /data/file',False),
                                               (255,'unix_listener: too long for Unix domain socket',False)):
                result=SimpleNamespace(returncode=code,stdout='',stderr=message)
                with patch('subprocess.run',return_value=result),redirect_stdout(io.StringIO()) as output,self.assertRaises(RuntimeError):
                    scope['_base_command'](['ssh','root@192.0.2.2','true'])
                events=[json.loads(line.removeprefix('@@ENODEDONE_CONNECTION@@')) for line in output.getvalue().splitlines() if line.startswith('@@ENODEDONE_CONNECTION@@')]
                self.assertEqual(bool(events),should_report)
                if events:self.assertEqual(events[0]['ips'],['192.0.2.2'])

    def test_generated_receiver_reports_scp_failure_without_real_network_or_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            scope,_,_=self.namespace(folder,{})
            scope['C']['image_dest']=str(Path(folder)/'target/image.tar')
            original=scope['peer'];scripts=[]
            def peer(ip,code):
                if 'TRANSFER_ARGS=' in code:scripts.append((ip,code))
                return original(ip,code)
            scope['peer']=peer
            with redirect_stdout(io.StringIO()):scope['distribute']()
            target,script=scripts[0];real_path=Path
            fake_pathlib=types.ModuleType('pathlib')
            fake_pathlib.Path=lambda value:real_path(folder)/'temporary' if str(value)=='/tmp' else real_path(value)
            failure=SimpleNamespace(returncode=255,stdout='',stderr='Permission denied (publickey)')
            uid=real_path(folder).stat().st_uid
            with patch.dict(sys.modules,pathlib=fake_pathlib),patch.object(os,'geteuid',return_value=uid,create=True), \
                 patch('subprocess.run',return_value=failure),redirect_stdout(io.StringIO()) as output,self.assertRaisesRegex(RuntimeError,'scp 返回 255'):
                exec(compile(script,'receiver.py','exec'),{})
            events=[json.loads(line.removeprefix('@@ENODEDONE_CONNECTION@@')) for line in output.getvalue().splitlines() if line.startswith('@@ENODEDONE_CONNECTION@@')]
            self.assertEqual(len(events),1);self.assertIn(target,events[0]['ips']);self.assertIn('192.0.2.1',events[0]['ips'])
            self.assertFalse(list((Path(folder)/'target').glob('.enodedone-*')))


if __name__=='__main__':unittest.main()
