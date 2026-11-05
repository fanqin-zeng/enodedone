"""Offline checks for the public plotting API and generic node artifact collection."""
import ast
import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from core import TaskStore
from enodedone_viz import plot
from runner import SSHRunner
from templates import stage, test_nodes as functional_nodes


class VisualizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/'metrics.csv'
        self.source.write_text('machine,value,delay\nA,2,1\nB,4,3\nC,6,5\n', encoding='utf-8')

    def test_five_chart_types_publish_parseable_svg_and_source_data(self):
        for kind in ('line', 'box', 'bar', 'pie', 'scatter'):
            with self.subTest(kind=kind), redirect_stdout(io.StringIO()) as output:
                result = plot(self.source, True, kind, x='machine', y='value', title='<测试 & 图表>', output_dir=self.folder/'charts')
            event = json.loads(output.getvalue().removeprefix('@@ENODEDONE_VIZ@@'))
            self.assertEqual(event['id'], result['id'])
            svg = Path(result['svg_path']).read_text(encoding='utf-8')
            ET.fromstring(svg)
            self.assertIn('&lt;测试 &amp; 图表&gt;', svg)
            self.assertEqual(result['row_count'], 3)
            self.assertEqual(len(json.loads(Path(result['data_path']).read_text(encoding='utf-8'))), 3)

    def test_scalar_lists_mappings_multiseries_and_invalid_data(self):
        source = self.folder/'data.json'
        source.write_text('[1, 3, 2]', encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(plot(source, True, 'line')['y'], ['value'])
            source.write_text('{"A": 1, "B": 3}', encoding='utf-8')
            self.assertEqual(plot(source, False, 'pie', x='label', y='value')['row_count'], 2)
            self.assertEqual(plot(self.source, True, 'line', x='machine', y=['value', 'delay'])['y'], ['value', 'delay'])
        source.write_text('[-1, 2]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nonnegative'):
            plot(source, True, 'pie', y='value')
        source.write_text('[{"value": NaN}]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'finite'):
            plot(source, True, 'bar', y='value')
        with self.assertRaisesRegex(ValueError, 'existing numeric'):
            plot(self.source, True, 'bar', y='absent')
        with self.assertRaisesRegex(ValueError, 'array or object'):
            source.write_text('42', encoding='utf-8')
            plot(source, True, None)

    def test_cli_matches_python_and_supports_repeated_y(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', 'enodedone_viz.py', '--data', str(self.source),
                                 '--show-table', 'true', '--kind', 'box', '--y', 'value', '--y', 'delay',
                                 '--output-dir', str(self.folder/'cli')], capture_output=True, text=True, encoding='utf-8', check=True)
        event = json.loads(result.stdout.removeprefix('@@ENODEDONE_VIZ@@'))
        metadata = json.loads(Path(event['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(metadata['kind'], 'box')
        self.assertEqual(metadata['y'], ['value', 'delay'])
        ast.parse(Path('enodedone_viz.py').read_text(encoding='utf-8'), feature_version=(3, 8))

    def test_table_switch_and_none_chart_are_independent_of_data_format(self):
        source=self.folder/'text.json'
        source.write_text('[{"machine":"A","note":"<b>text only</b>"},{"machine":"B","note":null}]',encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            table=plot(source,show_table=True,kind=None)
            both=plot(self.source,show_table=True,kind='bar',x='machine',y='value')
            chart=plot(self.source,show_table=False,kind='bar',x='machine',y='value')
        self.assertIsNone(table['kind']);self.assertTrue(table['show_table']);self.assertIsNone(table['svg_path'])
        self.assertEqual(table['y'],[])
        self.assertTrue(Path(table['data_path']).is_file())
        self.assertFalse(any(Path(table['data_path']).parent.glob(table['id']+'.svg')))
        self.assertTrue(both['show_table']);self.assertFalse(chart['show_table'])
        for result in (both,chart):self.assertTrue(Path(result['svg_path']).is_file())
        with redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(plot(self.folder/'not-read.json',False,None))
        self.assertEqual(output.getvalue(),'')
        with self.assertRaisesRegex(ValueError,'show_table must be a boolean'):
            plot(self.source,'false',None)

    def test_cli_none_can_publish_a_table_or_disable_all_output(self):
        for flag in ('true','false'):
            with self.subTest(show_table=flag):
                result=subprocess.run([sys.executable,'-X','utf8','enodedone_viz.py','--data',str(self.source),
                    '--show-table',flag,'--kind','None','--output-dir',str(self.folder/'none-cli')],
                    capture_output=True,text=True,encoding='utf-8',check=True)
                if flag=='false':self.assertEqual(result.stdout,'')
                else:
                    event=json.loads(result.stdout.removeprefix('@@ENODEDONE_VIZ@@'))
                    metadata=json.loads(Path(event['manifest_path']).read_text(encoding='utf-8'))
                    self.assertTrue(metadata['show_table']);self.assertIsNone(metadata['kind'])

    @unittest.skipUnless(shutil.which('bash'), 'Shell test requires bash')
    def test_shell_interface_executes_the_same_api(self):
        result = subprocess.run([shutil.which('bash'), 'enodedone_viz.sh', '--data', str(self.source),
                                 '--show-table', 'true', '--kind', 'bar', '--x', 'machine', '--y', 'value',
                                 '--output-dir', str(self.folder/'shell')],
                                env={**os.environ,'ENODEDONE_PYTHON':Path(sys.executable).as_posix(),'PYTHONIOENCODING':'utf-8'},
                                capture_output=True,text=True,encoding='utf-8',check=True)
        event=json.loads(result.stdout.removeprefix('@@ENODEDONE_VIZ@@'))
        metadata=json.loads(Path(event['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(metadata['kind'],'bar')
        self.assertEqual(metadata['y'],['value'])

    def test_custom_shell_node_gets_sdk_and_publishes_chart_without_analyze_operation(self):
        store = TaskStore(self.folder/'tasks')
        task = store.create('blank', count=1)
        runner = SSHRunner(store)
        artifact = self.folder/'run';artifact.mkdir()
        runner.runs[task['id']] = {'id':'test-run','artifact_dir':str(artifact),'artifacts':[],'visualizations':[]}
        runner.stops[task['id']] = threading.Event()
        node = task['nodes'][1]
        stage = {'events':[]}
        cell = {'rank':0,'ip':'192.0.2.1','output':[]}
        chart_id = 'a'*32
        remote_dir = '/tmp/enodedone/test-run/visualizations/custom/0'
        metadata = {'version':1,'id':chart_id,'kind':'bar','title':'Chart','row_count':1,
                    'svg_path':remote_dir+'/'+chart_id+'.svg','data_path':remote_dir+'/'+chart_id+'.data.json'}
        calls = []
        def run(ip, command, payload='', callback=None, *args, **kwargs):
            calls.append(command)
            if callback:
                compile(payload, '<remote wrapper>', 'exec')
                self.assertIn('enodedone_viz.sh', payload)
                self.assertIn('PYTHONPATH', payload)
                callback('@@ENODEDONE_VIZ@@'+json.dumps({'id':chart_id,'manifest_path':remote_dir+'/'+chart_id+'.json'}))
                return ''
            if command.endswith('.data.json'):return '[{"value":1}]'
            if command.endswith('.svg'):return '<svg xmlns="http://www.w3.org/2000/svg"/>'
            return json.dumps(metadata)
        with patch('runner.SSHTransport.run', side_effect=run):
            runner.execute(task['id'],task,[{'Info':'192.0.2.1','User':'root'}],node,stage,cell,'echo chart')
        self.assertEqual(len(calls), 4)
        chart = runner.runs[task['id']]['visualizations'][0]
        self.assertEqual(chart['node_id'], 'custom')
        self.assertEqual(chart['ip'], cell['ip'])
        self.assertTrue((artifact/'visualizations'/(chart_id+'.svg')).is_file())
        self.assertEqual(json.loads((artifact/'run.json').read_text(encoding='utf-8'))['visualizations'][0]['id'], chart_id)

    def test_runner_collects_a_table_without_requesting_an_svg(self):
        store=TaskStore(self.folder/'tasks');task=store.create('blank',count=1);runner=SSHRunner(store)
        artifact=self.folder/'run';artifact.mkdir()
        runner.runs[task['id']]={'artifact_dir':str(artifact),'artifacts':[],'visualizations':[]}
        chart_id='a'*32;remote_dir='/tmp/enodedone/test/visualizations/custom/0'
        metadata={'version':2,'id':chart_id,'kind':None,'show_table':True,'title':'Table',
                  'svg_path':None,'data_path':remote_dir+'/'+chart_id+'.data.json'}
        rows=[{'machine':'A','note':'text only'},{'machine':'B','note':None}]
        with patch('runner.SSHTransport.run',side_effect=[json.dumps(metadata),json.dumps(rows)]) as transport:
            from runner import SSHTransport
            runner.fetch_visualization(task['id'],SSHTransport({}),{'rank':0,'ip':'192.0.2.1'},task['nodes'][1],
                                       chart_id,remote_dir,threading.Event())
        self.assertEqual(transport.call_count,2)
        self.assertTrue(all('.svg' not in call.args[1] for call in transport.call_args_list))
        self.assertEqual(runner.runs[task['id']]['visualizations'][0]['table_rows'],rows)

    def test_invalid_chart_events_fail_without_fetching_arbitrary_files(self):
        store = TaskStore(self.folder/'tasks');task=store.create('blank', count=1)
        runner=SSHRunner(store);runner.runs[task['id']]={'id':'test-run'};runner.stops[task['id']]=threading.Event()
        def run(ip, command, payload='', callback=None, *args, **kwargs):
            callback('@@ENODEDONE_VIZ@@'+json.dumps({'id':'a'*32,'manifest_path':'/etc/shadow'}))
        with patch('runner.SSHTransport.run',side_effect=run) as transport, self.assertRaisesRegex(ValueError,'事件错误'):
            runner.execute(task['id'],task,[{'Info':'192.0.2.1','User':'root'}],task['nodes'][1],{'events':[]},{'rank':0,'ip':'192.0.2.1'},'true')
        self.assertEqual(transport.call_count,1)

    def test_functional_task_analysis_uses_public_interface_offline(self):
        for rank,value in enumerate((1.5,3.25)):
            folder=self.folder/'nodes'/str(rank);folder.mkdir(parents=True)
            (folder/'metric.json').write_text(json.dumps({'rank':rank,'machine':'n'+str(rank),'ip':'192.0.2.'+str(rank+1),
                'value':value,'epoch':100+rank,'delay_seconds':rank+1,'metric':'CPU load average (1 min)'}),encoding='utf-8')
        context={'rank':0,'master_run':str(self.folder),'run_id':'offline-test','artifact_kind':'archive',
                 'hosts':[{'Info':'192.0.2.1'},{'Info':'192.0.2.2'}]}
        node=next(n for n in functional_nodes() if n['operation']=='analyze')
        with patch.dict(os.environ,ENODEDONE_CONTEXT=base64.b64encode(json.dumps(context).encode()).decode(),
                        ENODEDONE_VIZ_DIR=str(self.folder/'charts'),CHART_KIND='bar'), \
             redirect_stdout(io.StringIO()) as output, patch('subprocess.Popen') as remote:
            exec(compile(node['script_content'],'analyze.py','exec'),{})
        remote.assert_not_called()
        lines=[line for line in output.getvalue().splitlines() if line.startswith('@@ENODEDONE_VIZ@@')]
        self.assertEqual(len(lines),1)
        event=json.loads(lines[0].removeprefix('@@ENODEDONE_VIZ@@'))
        metadata=json.loads(Path(event['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(metadata['source_path'],str((self.folder/'metrics.csv').resolve()))
        self.assertEqual(metadata['y'],['value'])
        self.assertEqual(metadata['row_count'],2)
        self.assertTrue(metadata['show_table'])
        self.assertEqual(json.loads((self.folder/'analysis.json').read_text(encoding='utf-8'))['kind'],'node-metrics')

    def test_analysis_node_may_only_publish_charts_without_legacy_summary_files(self):
        store=TaskStore(self.folder/'tasks');task=store.create('blank',count=1)
        node=stage('analyze','master');node.update(script_ip='127.0.0.1',script_path='${TASK_DIR}/scripts/analyze.py')
        task['nodes']=[node];store.save(task)
        runner=SSHRunner(store);artifact=self.folder/'run';artifact.mkdir()
        hosts=[{'Info':'192.0.2.1','Machine':'node001','User':'root'}]
        cell={'ip':'192.0.2.1','machine':'node001','rank':0,'status':'pending','message':'','output':[]}
        runner.runs[task['id']]={'id':'offline','status':'running','artifact_dir':str(artifact),'console':[],
            'summary':None,'artifacts':[],'visualizations':[],
            'stages':[{'id':'analyze','name':node['name'],'status':'pending','events':[],'cells':[cell]}]}
        runner.stops[task['id']]=threading.Event()
        with patch.object(runner.connections,'ensure') as ensure, patch.object(runner,'execute') as execute, patch('runner.SSHTransport.run',return_value='[]') as transport:
            runner._run(task['id'],task,hosts)
        ensure.assert_called_once()
        execute.assert_called_once();self.assertEqual(transport.call_count,1)
        self.assertEqual(runner.runs[task['id']]['status'],'success')
        self.assertIsNone(runner.runs[task['id']]['summary'])

    def test_training_analysis_publishes_separate_loss_gradient_and_step_time_tabs(self):
        for rank in range(2):
            folder=self.folder/'nodes'/str(rank);folder.mkdir(parents=True)
            (folder/'train.log').write_text('Step 1 | Loss: 8 | Grad Norm: 5 | Step Time: 9s\n'
                f'Step 4 | Loss: 3 | Grad Norm: 2 | Step Time: {rank+1}s\n'
                f'Step 5 | Loss: 2.5 | Grad Norm: 1.5 | Step Time: {rank+2}s\n',encoding='utf-8')
        context={'rank':0,'master_run':str(self.folder),'run_id':'offline-test','artifact_kind':'docker',
                 'hosts':[{'Info':'192.0.2.1'},{'Info':'192.0.2.2'}]}
        node=stage('analyze','master')
        with patch.dict(os.environ,ENODEDONE_CONTEXT=base64.b64encode(json.dumps(context).encode()).decode(),
                        ENODEDONE_VIZ_DIR=str(self.folder/'charts'),WARMUP_STEPS='3',SHOW_TABLE='false'), \
             redirect_stdout(io.StringIO()) as output,patch('subprocess.Popen') as remote:
            exec(compile(node['script_content'],'analyze.py','exec'),{})
        remote.assert_not_called()
        events=[json.loads(line.removeprefix('@@ENODEDONE_VIZ@@')) for line in output.getvalue().splitlines() if line.startswith('@@ENODEDONE_VIZ@@')]
        self.assertEqual(len(events),3);self.assertEqual(len({event['id'] for event in events}),3)
        charts=[json.loads(Path(event['manifest_path']).read_text(encoding='utf-8')) for event in events]
        self.assertEqual([chart['title'] for chart in charts],['Loss','Grad norm','Step time (s)'])
        self.assertEqual([chart['y'] for chart in charts],[['loss'],['grad_norm'],['seconds']])
        self.assertTrue(all(chart['kind']=='line' and chart['x']=='step' and not chart['show_table'] for chart in charts))
        rows=json.loads((self.folder/'training_metrics.json').read_text(encoding='utf-8'))
        self.assertEqual([row['step'] for row in rows],[4,5])
        self.assertEqual([row['seconds'] for row in rows],[2,3])


if __name__ == '__main__':
    unittest.main()
