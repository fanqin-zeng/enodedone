import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

from analysis import analyze_logs
from core import CSV_FIELDS, TaskStore, positive_count, read_machines, distribution_plan, image_action, migrate_image_paths
from runner import SSHRunner, SSHTransport, target
from templates import builtins, test_nodes as functional_nodes

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=TaskStore(Path(self.temp.name))

    def test_positive_integer_and_utc_name(self):
        for value in (0,-1,1.5,True,'1e3','', '2.0'):
            with self.assertRaises(ValueError):positive_count(value)
        self.assertEqual(positive_count('513'),513)
        with patch('runner.subprocess.Popen') as process:
            task=self.store.create(count=8)
            process.assert_not_called()
        self.assertRegex(task['name'],r'^训练任务\d{8}T\d{6}Z$')
        self.assertEqual(len(task['machines']),8)
        with Path(task['csv_file']).open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual(next(csv.reader(stream)),['Machine','Info','User','Pwd','Connected'])
        self.assertNotIn('container_name',task['config'])
        self.assertNotIn('steps',task['config'])
        self.assertNotIn('warmup_steps',task['config'])
        self.assertTrue(all(not h['Info'] for h in task['machines']))
        self.assertTrue(all(h['User']=='root' for h in task['machines']))
        with self.assertRaises(ValueError):self.store.validate(task,ready=True)

    def test_default_name_uses_selected_template_and_preserves_custom_names(self):
        for template,prefix in (('inference','推理任务'),('rl','后训练任务'),('blank','任务')):
            self.assertRegex(self.store.create(template,count=1)['name'],'^'+prefix+r'\d{8}T\d{6}Z$')
        self.assertEqual(self.store.create('training',name='我自己的任务名',count=1)['name'],'我自己的任务名')

    def test_creation_path_defaults_match_new_task_form(self):
        task=self.store.create('blank',count=1)
        self.assertEqual(task['config']['master_dir'],'/mnt/share/enodedone')
        self.assertNotIn('image_path',task['config'])
        self.assertNotIn('image_dest',task['config'])
        self.assertFalse(any(node['operation']=='distribute' for node in task['nodes']))
        training=self.store.create('training',count=1)
        distribute=next(node for node in training['nodes'] if node['operation']=='distribute')
        self.assertEqual(distribute['params'],{
            'IMAGE_PATH':'${MASTER_DIR}/image.tar',
            'IMAGE_DEST':'/tmp/enodedone/images/image.tar'})

    def test_local_path_defaults_apply_to_new_tasks_only(self):
        existing=self.store.create('training',count=1)
        path=self.store.root/'defaults.json'
        path.write_text(json.dumps({'master_dir':'/site/work','shared_dir':'/site/data',
                                    'IMAGE_PATH':'/site/source.tar','IMAGE_DEST':'/site/cache.tar'}),encoding='utf-8')
        store=TaskStore(self.store.root)
        created=store.create('training',count=1)
        self.assertEqual(created['config']['master_dir'],'/site/work')
        self.assertEqual(created['config']['shared_dir'],'/site/data')
        node=next(node for node in created['nodes'] if node['operation']=='distribute')
        self.assertEqual(node['params'],{'IMAGE_PATH':'/site/source.tar','IMAGE_DEST':'/site/cache.tar'})
        self.assertEqual(store.load(existing['id'])['config'],existing['config'])
        self.assertEqual(store.load(existing['id'])['nodes'],existing['nodes'])

    def test_creation_paths_override_template_and_are_saved_without_changing_template(self):
        source=self.store.create('blank',count=1);source['config']['master_dir']='/template/work'
        self.store.save(source);template=self.store.save_template(source['id'],'path fixture')
        paths={'master_dir':' /chosen/work '}
        created=self.store.create(template['id'],count=2,paths=paths)
        saved=self.store.load(created['id'])
        self.assertEqual({key:saved['config'][key] for key in paths},{key:value.strip() for key,value in paths.items()})
        self.assertEqual(saved['nodes'],source['nodes'])
        self.assertEqual(next(t for t in self.store.templates() if t['id']==template['id'])['config']['master_dir'],'/template/work')

    def test_legacy_import_and_template_keep_image_paths_and_history_signature(self):
        task=self.store.create('training',count=1)
        node=next(node for node in task['nodes'] if node['operation']=='distribute')
        node['params']={}
        task['config'].update(image_path='/legacy/source.tar',image_dest='/legacy/cache.tar')
        old_signature=SSHRunner.signature(task,self.store.validate(task))
        path=Path(task['file_path'])
        path.write_text(json.dumps(task),encoding='utf-8')
        imported=self.store.open_file(path)
        params=next(node['params'] for node in imported['nodes'] if node['operation']=='distribute')
        self.assertEqual(params,{'IMAGE_PATH':'/legacy/source.tar','IMAGE_DEST':'/legacy/cache.tar'})
        self.assertNotIn('image_path',imported['config'])
        self.assertEqual(SSHRunner.signature(imported,self.store.validate(imported)),old_signature)
        self.assertFalse(migrate_image_paths(imported))

        template=self.store.save_template(imported['id'],'legacy fixture')
        template_path=self.store.root/'_templates'/(template['id']+'.json')
        template_path.write_text(json.dumps({**template,'nodes':task['nodes'],'config':task['config']}),encoding='utf-8')
        created=self.store.create(template['id'],count=1)
        self.assertNotIn('image_dest',created['config'])
        self.assertEqual(next(n['params'] for n in created['nodes'] if n['operation']=='distribute'),params)

    def test_invalid_creation_paths_do_not_leave_partial_tasks(self):
        for paths in ({'master_dir':'relative/path'},{'image_path':''},{'image_dest':None},{'ssh_user':'someone'},[]):
            with self.subTest(paths=paths),self.assertRaises(ValueError):self.store.create('blank',count=1,paths=paths)
            self.assertEqual(self.store.locations,{})
            self.assertEqual(list(self.store.root.iterdir()),[])

    def test_templates_and_embedded_scripts_compile_without_execution(self):
        self.assertEqual([t['name'] for t in builtins()],['训练','推理','后训练','镜像分发','空白任务'])
        self.assertEqual([t['icon'] for t in builtins()],['','','','','+'])
        analysis=next(n for n in builtins()[0]['nodes'] if n['operation']=='analyze')
        self.assertEqual(analysis['params']['STATS_START_STEP'],'101')
        self.assertEqual(analysis['params']['STATS_END_STEP'],'200')
        for template in builtins()+[{'nodes':functional_nodes()}]:
            for node in template['nodes']:
                if node['language']=='python':compile(node['script_content'],node['script_path'],'exec')
        self.assertEqual([n['operation'] for n in functional_nodes()],['push','distribute','shared','sample','collect','analyze'])
        self.assertEqual(functional_nodes()[0]['params'],{})

    def test_csv_credentials_copy_and_template(self):
        task=self.store.create(count=2)
        path=Path(task['csv_file'])
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader()
            for i in range(2):writer.writerow({'Machine':f'n{i}','Info':f'192.0.2.{i+1}','User':'alice' if i==0 else '', 'Pwd':'not-public'})
        self.assertEqual([h['User'] for h in read_machines(path)],['alice','root'])
        task['config']['master_ip']='192.0.2.1';self.store.save(task)
        self.assertNotIn('not-public',json.dumps(self.store.view(task['id'])))
        copied=self.store.copy(task['id']);self.assertNotEqual(copied['csv_file'],task['csv_file'])
        template=self.store.save_template(task['id'],'mine')
        self.assertEqual(len(self.store.create(template['id'],count=3)['machines']),3)

    def test_legacy_availability_setting_is_not_used(self):
        task=self.store.create(count=1);path=Path(task['csv_file'])
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader();writer.writerow({'Machine':'n','Info':'192.0.2.1'})
        status=Path(self.temp.name)/'free.csv';status.write_text('Machine,Info,Free\nn,192.0.2.1,N\n')
        task['config'].update(master_ip='192.0.2.1',availability_csv=str(status));self.store.save(task)
        runner=SSHRunner(self.store)
        status.unlink()  # A removed or unavailable external CSV must not affect eNodeDone.
        with patch.object(runner,'_run') as dispatch, patch('runner.subprocess.Popen') as process:
            result=runner.start(task['id']);runner.threads[task['id']].join(timeout=2)
            self.assertEqual(result['status'],'running')
            dispatch.assert_called_once()
            process.assert_not_called()

    def test_ssh_quoting_and_distribution(self):
        for ip in ('-oProxyCommand=bad','ip;echo bad','user@host'):
            with self.assertRaises(ValueError):target(ip,'root')
        args=SSHTransport({'ssh_user':'root'}).argv('192.0.2.1','echo hello')
        self.assertEqual(args[-2:],['root@192.0.2.1','echo hello'])
        args=SSHTransport({'host_users':{'192.0.2.1':'alice'}}).argv('192.0.2.1','true')
        self.assertEqual(args[-2],'alice@192.0.2.1')
        rounds=distribution_plan(list('12345678'))
        self.assertEqual([len(r) for r in rounds],[1,2,4])
        verified={'1'}
        for edges in rounds:
            self.assertTrue(all(e['source'] in verified for e in edges));verified.update(e['target'] for e in edges)
        self.assertEqual(image_action(None,'a'*64),'copy')
        with self.assertRaises(ValueError):image_action('b'*64,'a'*64)

    def test_real_log_analysis(self):
        result=analyze_logs({'a':'Step 1 | Loss: 9 | Step Time: 9s\nStep 4 | Loss: 3 | Grad Norm: 2 | Step Time: 1s',
            'b':'iteration 4 | elapsed time per iteration (ms): 2000 | loss: 99'},'a',2)
        self.assertEqual(result['mean_seconds'],2)
        self.assertEqual(result['mean_loss'],3)

    def test_ssh_stderr_does_not_corrupt_downloaded_json(self):
        process=MagicMock()
        process.stdin=io.StringIO();process.stdout=io.StringIO('{"value":1}\n')
        process.stderr=io.StringIO('SSH banner\n');process.returncode=0;process.poll.return_value=0
        lines=[]
        with patch('runner.subprocess.Popen',return_value=process):
            output=SSHTransport({}).run('192.0.2.1','cat analysis.json',callback=lines.append)
        self.assertEqual(json.loads(output),{'value':1})
        self.assertIn('[stderr] SSH banner',lines)

if __name__=='__main__':unittest.main()
