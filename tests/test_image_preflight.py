"""Local SSH preflight orchestration, with no real SSH or Docker execution."""
from contextlib import redirect_stdout
import ast
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock,patch

from core import TaskStore
from image_files import FILE_HELPERS
from image_preflight import PREFIX,check_images,query_script
from machine_state import CSV_FIELDS
from runner import SSHRunner
from templates import stage


class ImagePreflightTests(unittest.TestCase):
    def setUp(self):
        self.hosts=[{'Info':f'192.0.2.{rank+1}','User':'root','Machine':f'n{rank}'} for rank in range(8)]
        self.context={'image_path':'/master/image.tar','image_dest':'/node/image.tar','artifact_kind':'archive'}

    def transport(self,existing,barrier=None):
        def run(ip,command,payload,**kwargs):
            self.assertEqual(command,'python3 -u -')
            compile(payload,'sha-query.py','exec')
            if barrier:barrier.wait()
            result={'existing_sha':existing.get(ip)}
            if ip==self.hosts[0]['Info']:result['source_sha']='a'*64
            return PREFIX+json.dumps(result)+'\n'
        return MagicMock(run=MagicMock(side_effect=run))

    def test_all_nodes_are_queried_concurrently_through_existing_transport(self):
        transport=self.transport({'192.0.2.1':'a'*64,'192.0.2.2':'b'*64},threading.Barrier(8,timeout=3))
        events=[];stop=threading.Event()
        result=check_images(transport,self.hosts,self.context,stop,lambda *args:events.append(args),lambda line:None)
        self.assertEqual(result['available'],[0]);self.assertEqual(result['pending'],list(range(1,8)))
        self.assertEqual(result['replace'],{'1':'b'*64})
        self.assertEqual(transport.run.call_count,8)
        self.assertTrue(all(call.kwargs['stop'] is stop for call in transport.run.call_args_list))
        self.assertEqual([e for e in events if e[1]=='success'][0][0],0)

    def test_query_failure_prevents_distribution_snapshot(self):
        transport=self.transport({})
        original=transport.run.side_effect
        def fail(ip,*args,**kwargs):
            if ip=='192.0.2.3':raise RuntimeError('query failed')
            return original(ip,*args,**kwargs)
        transport.run.side_effect=fail;events=[]
        with self.assertRaisesRegex(RuntimeError,'query failed'):
            check_images(transport,self.hosts,self.context,None,lambda *args:events.append(args),lambda line:None)
        self.assertIn((2,'failed','query failed'),events);self.assertEqual(transport.run.call_count,8)

    def test_query_creates_only_master_test_source_and_does_not_create_missing_destinations(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'master/test.tar.gz';destination=Path(folder)/'absent/image.tar.gz'
            with redirect_stdout(io.StringIO()) as output:exec(query_script(str(source),str(destination),'archive'),{})
            result=json.loads(output.getvalue().strip().removeprefix(PREFIX))
            self.assertEqual(result['source_sha'],hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertIsNone(result['existing_sha']);self.assertFalse(destination.parent.exists())
            with redirect_stdout(io.StringIO()) as output:exec(query_script(None,str(source),'archive'),{})
            self.assertEqual(json.loads(output.getvalue().strip().removeprefix(PREFIX))['existing_sha'],result['source_sha'])

    def test_checked_replacement_preserves_changed_targets_or_corrupt_incoming_files(self):
        import os
        scope={'Path':Path,'hashlib':hashlib,'os':os};exec(FILE_HELPERS,scope)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'image';incoming=Path(folder)/'incoming'
            for current,payload,success in ((b'old',b'new',True),(b'changed',b'new',False),(b'old',b'broken',False)):
                with self.subTest(current=current,payload=payload):
                    target.write_bytes(current);incoming.write_bytes(payload)
                    args=(incoming,target,hashlib.sha256(b'new').hexdigest(),hashlib.sha256(b'old').hexdigest())
                    if success:scope['checked_publish'](*args)
                    else:
                        with self.assertRaises(RuntimeError):scope['checked_publish'](*args)
                    self.assertEqual(target.read_bytes(),b'new' if success else current)
                    self.assertFalse(incoming.exists())

    def test_cache_hit_skips_remote_script_fetch_execution_and_application(self):
        with tempfile.TemporaryDirectory() as folder:
            store=TaskStore(Path(folder)/'tasks');task=store.create('blank',count=8)
            with Path(task['csv_file']).open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader()
                writer.writerows(dict(host,Pwd='',Connected='N') for host in self.hosts)
            task['nodes']=[stage('distribute')];task['config'].update(master_ip='192.0.2.1',artifact_kind='archive');store.save(task)
            runner=SSHRunner(store);transport=self.transport({host['Info']:'a'*64 for host in self.hosts})
            with patch.object(runner.connections,'ensure'),patch.object(runner,'transport',return_value=transport),patch.object(runner,'execute') as execute:
                runner.start(task['id']);runner.threads[task['id']].join(timeout=3)
            run=runner.state(task['id']);self.assertEqual(run['status'],'success')
            self.assertTrue(all(cell['status']=='success' for cell in run['stages'][0]['cells']))
            self.assertEqual(run['stages'][0]['rounds'],[]);execute.assert_not_called()
            self.assertEqual(transport.run.call_count,8)

    def test_partial_cache_results_reach_master_script_via_stdin_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store=TaskStore(Path(folder)/'tasks');task=store.create('blank',count=8)
            with Path(task['csv_file']).open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader()
                writer.writerows(dict(host,Pwd='',Connected='N') for host in self.hosts)
            node=stage('distribute',IMAGE_PATH='${MASTER_DIR}/images/source.tar',IMAGE_DEST='/cache/${RUN_ID}/image.tar')
            node.update(script_ip='127.0.0.1',script_path='${TASK_DIR}/scripts/distribute.py')
            task['nodes']=[node];task['config'].update(master_ip='192.0.2.1',artifact_kind='archive');store.save(task)
            runner=SSHRunner(store);snapshots=[]
            query=self.transport({h['Info']:'b'*64 if rank==1 else 'a'*64 for rank,h in enumerate(self.hosts)})
            def run(ip,command,payload,callback=None,*args,**kwargs):
                if callback is None:return query.run(ip,command,payload,**kwargs)
                tree=ast.parse(payload,feature_version=(3,8))
                assignment=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(
                    isinstance(t,ast.Subscript) and isinstance(t.slice,ast.Constant) and
                    t.slice.value=='ENODEDONE_DISTRIBUTION_PREFLIGHT' for t in n.targets))
                snapshots.append(json.loads(ast.literal_eval(assignment.value)))
                self.assertNotIn('ENODEDONE_DISTRIBUTION_PREFLIGHT',command)
                self.assertIn('image_files.py',payload)
                for rank in range(8):callback('@@ENODEDONE@@'+json.dumps({'rank':rank,'status':'success','message':'verified'}))
                return ''
            transport=MagicMock(run=MagicMock(side_effect=run))
            with patch.object(runner.connections,'ensure'),patch.object(runner,'transport',return_value=transport):
                runner.start(task['id']);runner.threads[task['id']].join(timeout=3)
            self.assertEqual(runner.state(task['id'])['status'],'success')
            self.assertEqual(query.run.call_count,8);self.assertEqual(len(snapshots),1)
            self.assertEqual(snapshots[0]['pending'],[1]);self.assertEqual(snapshots[0]['replace'],{'1':'b'*64})
            self.assertEqual(snapshots[0]['image_path'],task['config']['master_dir']+'/images/source.tar')
            self.assertEqual(snapshots[0]['image_dest'],'/cache/'+runner.state(task['id'])['id']+'/image.tar')

            del node['params']['IMAGE_DEST']
            store.save(task)
            with patch.object(runner.connections,'entry') as connect, self.assertRaisesRegex(ValueError,'IMAGE_DEST'):
                runner.start(task['id'])
            connect.assert_not_called()


if __name__=='__main__':unittest.main()
