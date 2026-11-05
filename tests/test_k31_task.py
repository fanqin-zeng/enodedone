"""Validate the reusable training template offline, without SSH/Docker/accelerators."""
import ast
import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tarfile
import types
import unittest
from unittest.mock import MagicMock, patch

from core import TaskStore
from runner import expand
from templates import builtins


class K31TaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = next(template for template in builtins() if template['id']=='training')
        cls.nodes = {node['operation']:node for node in cls.template['nodes']}

    def load(self, operation):
        namespace = {'__name__':'offline_validation'}
        exec(compile(self.nodes[operation]['script_content'], operation+'.py', 'exec'), namespace)
        return namespace

    def load_orchestrator(self, operation, context):
        tree=ast.parse(self.nodes[operation]['script_content'])
        tree.body.pop()  # Remove only the stage invocation; imports/helpers remain unchanged.
        namespace={'__name__':'offline_validation'}
        with patch.dict(os.environ,ENODEDONE_CONTEXT=base64.b64encode(json.dumps(context).encode()).decode()):
            exec(compile(tree,operation+'.py','exec'),namespace)
        return namespace

    def settings(self):
        values = {'MASTER_DIR':'/example/training'}
        return {key:expand(value, values) for key, value in self.nodes['train']['params'].items()}

    def context(self, rank=0):
        return {'run_id':'offline-test', 'rank':rank,
                'hosts':[{'Info':'192.0.2.'+str(i+1)} for i in range(8)]}

    def test_task_is_ready_and_scripts_match_saved_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            store=TaskStore(Path(folder));task=store.create('training',count=8,paths={
                'master_dir':'/new/work'})
            self.assertEqual(task['config']['command_timeout'],7200)
            self.assertEqual(task['config']['master_ip'],'')
            self.assertTrue(all(not host['Info'] for host in task['machines']))
            for node in task['nodes']:
                ast.parse(node['script_content'], feature_version=(3,8))
                self.assertEqual(node['script_content'],
                    (Path(folder)/task['id']/'scripts'/(node['id']+'.py')).read_text(encoding='utf-8'))
                for key in ('CODE_DIR','CONFIG_PATH','MODELS_DIR','DATA_DIR'):
                    if key in node['params']:
                        self.assertTrue(expand(node['params'][key],{'MASTER_DIR':'/new/work'}).startswith('/new/work/'))
            self.assertNotIn('master_ip',self.template['config'])
        self.assertEqual(len({self.nodes[op]['params']['CODE_DIR'] for op in ('init','shared','train')}),1)
        self.assertEqual(len({self.nodes[op]['params']['ENTRYPOINT'] for op in ('init','shared','train')}),1)
        self.assertEqual(self.nodes['init']['params']['CONFIG_PATH'],self.nodes['train']['params']['CONFIG_PATH'])

    def test_image_environment_is_inherited_or_explicitly_overridden(self):
        ns=self.load('train');settings=self.settings();settings['HCCL_SOCKET_IFNAME']='eth0'
        argv=ns['build_command'](self.context(),settings,[])[0]
        self.assertNotIn('--entrypoint',argv)
        env=dict(argv[i+1].split('=',1) for i,arg in enumerate(argv) if arg=='--env')
        self.assertNotIn('CANN_ENV',env);self.assertNotIn('CONDA_PREFIX',env)
        explicit={**settings,'CANN_ENV':'/custom/cann/set_env.sh','CONDA_PREFIX':'/custom/env',
                  'IMAGE_ENTRYPOINT':'/custom/entrypoint.sh'}
        argv=ns['build_command'](self.context(),explicit,[])[0]
        self.assertEqual(argv[argv.index('--entrypoint')+1],'/custom/entrypoint.sh')
        self.assertIn('CANN_ENV=/custom/cann/set_env.sh',argv)
        self.assertIn('CONDA_PREFIX=/custom/env',argv)
        # Every call returns independent editable node snapshots.
        changed=next(item for item in builtins() if item['id']=='training')
        changed['nodes'][4]['params']['MAX_STEPS']='999'
        self.assertEqual(next(item for item in builtins() if item['id']=='training')['nodes'][4]['params']['MAX_STEPS'],'200')

    def test_rank_launch_mounts_and_same_process_environment(self):
        ns = self.load('train');settings = self.settings();settings['HCCL_SOCKET_IFNAME']='eth0'
        with patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            commands = [ns['build_command'](self.context(rank), settings, ['/dev/davinci0'])[0] for rank in range(8)]
            popen.assert_not_called();run.assert_not_called()
        for rank, argv in enumerate(commands):
            environment = dict(argv[i+1].split('=',1) for i,arg in enumerate(argv) if arg=='--env')
            self.assertEqual(environment['NNODES'],'8');self.assertEqual(environment['NODE_RANK'],str(rank))
            self.assertEqual(environment['MASTER_ADDR'],'192.0.2.1')
            self.assertEqual(environment['NPUS_PER_NODE'],'16')
            self.assertEqual(environment['GLOO_SOCKET_IFNAME'],'eth0')
            mounts = [argv[i+1] for i,arg in enumerate(argv) if arg=='--mount']
            self.assertEqual(len(mounts),6)
            self.assertFalse(any(m.startswith('type=bind,src=/mnt/share,') for m in mounts))
            self.assertEqual(argv[argv.index('--workdir')+1],'/workspace/FSDPTurbo')
            self.assertIn('source "$CANN_ENV"', argv[-1])
            self.assertIn('conda activate "$CONDA_ENV"', argv[-1])
            command = shlex.split(argv[-1].split('\nexec ',1)[1])
            self.assertEqual(command[-3:],['examples/kimi_k31/train.py','--config','/outputs/effective_config.yaml'])
            self.assertNotIn('MM_FORCE_EP_BALANCE', environment)

    def test_bad_configuration_is_rejected_before_any_process(self):
        ns = self.load('train');base = self.settings();base['HCCL_SOCKET_IFNAME']='eth0'
        for key,value in (('NPUS_PER_NODE','0'),('MASTER_PORT','65536'),('HCCL_SOCKET_IFNAME',''),
                          ('ENTRYPOINT','../train.py'),('PROFILE_ENABLED','yes'),('MAX_STEPS','1.2'),
                          ('DOCKER_IMAGE','-bad'),('CODE_DIR','relative'),('CONDA_SH','/conda.sh')):
            with self.subTest(key=key),patch('subprocess.run') as run,patch('subprocess.Popen') as popen:
                with self.assertRaises(ValueError):ns['build_command'](self.context(),{**base,key:value},[])
                run.assert_not_called();popen.assert_not_called()

    def test_container_shell_parses_without_execution(self):
        ns = self.load('train');settings = self.settings();settings['HCCL_SOCKET_IFNAME']='eth0'
        settings['EXTRA_TRAIN_ARGS'] = '--model-name "path with spaces; $(touch never)"'
        shell = ns['build_command'](self.context(),settings,[])[0][-1]
        bash = shutil.which('bash')
        self.assertIsNotNone(bash)
        result = subprocess.run([bash,'--noprofile','--norc','-n'],input=shell,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(shlex.split(shell.split('\nexec ',1)[1])[-1],'path with spaces; $(touch never)')

    def test_effective_yaml_and_batch_metadata_without_importing_torch(self):
        ns = self.load('train')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'configs').mkdir();(root/'outputs').mkdir()
            # JSON is valid YAML. Substitute the YAML codec, exercise the actual preparation logic.
            fixture={'model':{},'data':{'batch_size':1},'run':{'gradient_accumulation_steps':1},
                     'distributed':{'fully_shard_parallel_size':8,'expert_parallel_size':8}}
            (root/'configs/source.yaml').write_text(json.dumps(fixture),encoding='utf-8')
            settings=self.settings();settings.update(NNODES='8',NODE_RANK='0')
            for key in ('MODEL_PATH','TOKENIZER_PATH','DATASET_PATH'):
                settings[key]='/'+key.lower();(root/key.lower()).mkdir()
            def mapped(path):return root/str(path).lstrip('/')
            codec=types.SimpleNamespace(safe_load=json.loads,safe_dump=lambda value,**kw:json.dumps(value))
            with patch.dict('sys.modules',{'yaml':codec}),patch.dict(os.environ,settings),patch('pathlib.Path',side_effect=mapped), \
                 patch('subprocess.run') as run,patch('subprocess.Popen') as popen:
                exec(ns['PREPARE_CONFIG'],{})
                run.assert_not_called();popen.assert_not_called()
            cfg=json.loads((root/'outputs/effective_config.yaml').read_text())
            self.assertEqual(cfg['run']['max_steps'],200)
            self.assertEqual(cfg['run']['logging_steps'],1)
            self.assertEqual(cfg['run']['profile']['output_dir'],'/outputs/profiling')
            self.assertFalse(cfg['run']['profile']['enabled'])
            self.assertEqual(cfg['run']['memory_snapshot']['save_path'],'/outputs/snapshot')
            meta=json.loads((root/'outputs/launch.json').read_text())
            self.assertEqual(meta['samples_per_step'],128)

    def test_master_only_logs_and_original_performance_window(self):
        ns=self.load('analyze')
        turbo='\n'.join(f'Step {i} | Loss: 1e-2 | Grad Norm: 2.5 | Step Time: {i/1000}s' for i in range(1,201))
        mm='\n'.join(f'iteration {i} | loss: 1e-2 | grad norm: 2.5 | elapsed time per iteration (ms): {i}' for i in range(1,201))
        for text in (turbo,mm):
            result=ns['summarize']({'master':text,'peer':'worker has no rank-0 metrics'},'master',101,200,128)
            self.assertEqual(result['count'],100);self.assertAlmostEqual(result['mean_ms'],150.5)
            self.assertAlmostEqual(result['samples_per_second'],128/.1505)
            self.assertAlmostEqual(result['mean_loss'],.01);self.assertEqual(len(result['rows']),200)
        with self.assertRaisesRegex(ValueError,'缺少'):ns['summarize']({'master':turbo},'master',101,201,128)
        result=ns['summarize']({'master':turbo},'master',101,201,128,require_full=False)
        self.assertEqual(result['missing_steps'],[201])
        with self.assertRaisesRegex(ValueError,'缺少'):
            ns['summarize']({'master':turbo,'peer':''},'master',101,200,128,policy='slowest')

    def test_analysis_publishes_three_tabs_through_public_interface(self):
        ns=self.load('analyze')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'nodes/0').mkdir(parents=True)
            (root/'nodes/0/train.log').write_text('Step 1 | Loss: 1 | Grad Norm: 2 | Step Time: .1s\n')
            (root/'nodes/0/launch.json').write_text('{"samples_per_step":128}')
            ctx={**self.context(),'master_run':folder,'hosts':[{'Info':'192.0.2.1'}]}
            env={**self.nodes['analyze']['params'],'STATS_START_STEP':'1','STATS_END_STEP':'1',
                 'ENODEDONE_CONTEXT':base64.b64encode(json.dumps(ctx).encode()).decode()}
            plot=MagicMock()
            with patch.dict(os.environ,env),patch.dict('sys.modules',{'enodedone_viz':types.SimpleNamespace(plot=plot)}),redirect_stdout(io.StringIO()):
                ns['main']()
            self.assertEqual([call.kwargs['y'] for call in plot.call_args_list],['loss','grad_norm','seconds'])
            self.assertAlmostEqual(json.loads((root/'analysis.json').read_text(encoding='utf-8'))['samples_per_second'],1280)

    def test_init_preserves_user_yaml_and_only_checks_docker_version(self):
        ns=self.load('init')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'code').mkdir();(root/'code/train.py').write_text('# entry')
            settings={'CODE_DIR':str(root/'code'),'ENTRYPOINT':'train.py','CONFIG_PATH':str(root/'config.yaml')}
            with patch('subprocess.run') as run,redirect_stdout(io.StringIO()):
                ns['initialize']({'master_run':str(root/'run')},settings)
                self.assertEqual((root/'config.yaml').read_text(encoding='utf-8'),ns['TEMPLATE'])
                (root/'config.yaml').write_text('user yaml')
                ns['initialize']({'master_run':str(root/'run')},settings)
                self.assertEqual((root/'config.yaml').read_text(),'user yaml')
                self.assertTrue(all(call.args[0][:2]==['docker','version'] for call in run.call_args_list))

    def test_container_failure_and_interruption_keep_logs_and_stop_only_own_container(self):
        ns=self.load('train')
        for interrupted in (False,True):
            with self.subTest(interrupted=interrupted),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);settings=self.settings();settings['ENTRYPOINT']='entry.py'
                for key in ('CODE_DIR','MODELS_DIR','DATA_DIR','DRIVER_DIR'):
                    settings[key]=str(root/key);Path(settings[key]).mkdir()
                (Path(settings['CODE_DIR'])/'entry.py').write_text('# fake entry')
                settings['CONFIG_PATH']=str(root/'source.yaml');Path(settings['CONFIG_PATH']).write_text('{}')
                settings['HCCL_SOCKET_IFNAME']='eth0';(root/'interface').mkdir()
                device=root/'device';device.touch()
                ctx=self.context()
                settings['ENODEDONE_CONTEXT']=base64.b64encode(json.dumps(ctx).encode()).decode()
                def mapped(path):
                    if str(path).startswith('/dev/'):return device
                    if str(path)=='/sys/class/net':return root
                    return Path(path)
                (root/'eth0').mkdir()
                process=MagicMock();process.wait.return_value=7
                process.poll.return_value=None if interrupted else 7
                process.stdout=MagicMock()
                def lines():
                    yield 'captured output\n'
                    if interrupted:raise InterruptedError('synthetic cancellation')
                process.stdout.__iter__.side_effect=lines
                with patch.dict(os.environ,settings),patch.dict(ns,Path=mapped), \
                     patch.object(ns['glob'],'glob',return_value=[str(device)]), \
                     patch.dict(ns,build_command=lambda *a:(['docker','run','fixture'],'own-container',str(root/'outputs'))), \
                     patch('subprocess.run') as run,patch('subprocess.Popen',return_value=process), \
                     patch.object(ns['signal'],'signal'),redirect_stdout(io.StringIO()):
                    with self.assertRaises(InterruptedError if interrupted else RuntimeError):ns['main']()
                self.assertEqual((root/'outputs/train.log').read_text(),'captured output\n')
                stop_calls=[call.args[0] for call in run.call_args_list if call.args[0][1]=='stop']
                self.assertEqual(stop_calls,[['docker','stop','--time','10','own-container']] if interrupted else [])

    def test_collect_archives_real_local_files_without_remote_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);local=root/'local';local.mkdir();master=root/'master'
            for name in ('train.log','launch.json','effective_config.yaml','profiling/trace.json','snapshot/memory.pickle'):
                path=local/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('test '+name)
            ctx={**self.context(),'hosts':[{'Info':'192.0.2.1'}],'master_run':str(master), 'timeout':10}
            ns=self.load_orchestrator('collect',ctx);ns['LOCAL']=local;events=[]
            def peer(ip,code):
                with redirect_stdout(io.StringIO()):exec(code,{})
                return 'packed locally'
            ns.update(peer=peer,command=lambda *a,**k:self.fail('No SSH/SCP allowed'),event=lambda *a:events.append(a))
            with redirect_stdout(io.StringIO()):ns['collect']()
            for name in ('train.log','launch.json','effective_config.yaml','profiling/trace.json','snapshot/memory.pickle'):
                self.assertEqual((master/'nodes/0'/name).read_text(),'test '+name)
            self.assertEqual(events[0][1],'success')
            # Receiver rejects malicious names rather than extracting outside its destination.
            with tarfile.open(local/'artifacts.tar.gz','w:gz') as archive:
                member=tarfile.TarInfo('../escape');member.size=1;archive.addfile(member,io.BytesIO(b'x'))
            ns['peer']=lambda *args:''
            with redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'非法'):ns['collect']()
            self.assertFalse((master/'nodes/escape').exists())

    def test_shared_code_markers_are_verified_and_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'train.py').write_text('# entry');keep=root/'user-file';keep.write_text('keep')
            ctx={**self.context(),'master_run':str(root/'run'),'shared_dir':'/unused','timeout':10}
            ns=self.load_orchestrator('shared',ctx);events=[]
            ns.update(peer=lambda ip,code:exec(code,{}),event=lambda *a:events.append(a))
            with patch.dict(os.environ,CODE_DIR=str(root),ENTRYPOINT='train.py'),redirect_stdout(io.StringIO()):ns['shared']()
            self.assertEqual(sum(event[1]=='success' for event in events),8)
            self.assertFalse(list(root.glob('*.marker')));self.assertEqual(keep.read_text(),'keep')
