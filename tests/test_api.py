import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from core import TaskStore
from runner import SSHRunner
from server import make_handler


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=TaskStore(Path(self.temp.name));self.task=self.store.create(count=2)
        self.runner=SSHRunner(self.store)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.runner))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def request(self,path,data=None,origin=None):
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        request=Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        with urlopen(request) as response:return json.load(response)

    def test_task_create_save_open_copy_and_csv_default_application(self):
        created=self.request('/api/tasks',{'template':'copy','name':'test','count':8})
        self.assertNotIn('expected_sha', created['config'])
        created['name']='saved';self.request(f'/api/tasks/{created["id"]}/save',created)
        opened=self.request('/api/open',{'path':created['file_path']})
        self.assertEqual(opened['name'],'saved')
        copied=self.request(f'/api/tasks/{created["id"]}/copy',{})
        self.assertNotEqual(copied['id'],created['id'])
        with patch('server.os.startfile',create=True) as startfile:
            self.request(f'/api/tasks/{created["id"]}/csv-open',{})
            startfile.assert_called_once_with(created['csv_file'])

    def test_creation_has_no_execution_and_run_requires_complete_csv(self):
        self.assertEqual(self.request('/api/bootstrap')['mode'],'ssh')
        identifier=self.task['id']
        with patch('runner.subprocess.Popen') as process:
            with self.assertRaises(HTTPError) as error:
                self.request(f'/api/tasks/{identifier}/run',{})
            self.assertEqual(error.exception.code,400)
            process.assert_not_called()
        self.assertIsNone(self.request(f'/api/tasks/{identifier}/run'))
        with self.assertRaises(HTTPError) as error:
            self.request('/api/tasks',{'count':2},'https://example.invalid')
        self.assertEqual(error.exception.code,403)
        for count in (0,-1,1.5,True):
            with self.assertRaises(HTTPError) as error:self.request('/api/tasks',{'count':count})
            self.assertEqual(error.exception.code,400)

    def test_machine_limit_saves_csv_prefix_and_clears_master_without_rewriting_csv(self):
        identifier=self.task['id']
        path=Path(self.task['csv_file'])
        csv='Machine,Info,User,Pwd,Connected\r\nn1,192.0.2.1,root,fixture-secret,N\r\nn2,192.0.2.2,root,,N\r\nn3,192.0.2.3,root,,N\r\n'
        path.write_bytes(csv.encode('utf-8-sig'))
        task=self.task
        task['machine_count']=3;task['config']['master_ip']='192.0.2.3'
        self.request(f'/api/tasks/{identifier}/save',task)
        task['machine_count']=2
        saved=self.request(f'/api/tasks/{identifier}/save',task)
        self.assertEqual(saved['config']['master_ip'],'')
        self.assertEqual(saved['machine_count'],2)
        self.assertEqual([host['Info'] for host in saved['machines']],['192.0.2.1','192.0.2.2'])
        self.assertEqual(len(saved['csv_machines']),3)
        self.assertNotIn('fixture-secret',json.dumps(saved))
        self.assertEqual(path.read_bytes(),csv.encode('utf-8-sig'))
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/tasks/{identifier}/run',{})
        self.assertEqual(error.exception.code,400)
        saved['machine_count']=0
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/tasks/{identifier}/save',saved)
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.request(f'/api/tasks/{identifier}')['machine_count'],2)
        self.assertEqual(path.read_bytes(),csv.encode('utf-8-sig'))
        saved['machine_count']=2
        saved['config']['master_ip']='192.0.2.2'
        self.request(f'/api/tasks/{identifier}/save',saved)
        with patch.object(self.runner,'_run') as dispatch:
            self.request(f'/api/tasks/{identifier}/run',{})
            self.runner.threads[identifier].join(timeout=2)
        hosts=dispatch.call_args.args[2]
        self.assertEqual([host['Info'] for host in hosts],['192.0.2.2','192.0.2.1'])
        self.assertEqual(path.read_bytes(),csv.encode('utf-8-sig'))

    def test_visualization_asset_is_served_only_for_registered_chart(self):
        identifier=self.task['id'];chart_id='a'*32
        folder=self.store.locations[identifier].parent/'runs'/'offline'/'visualizations';folder.mkdir(parents=True)
        svg='<svg xmlns="http://www.w3.org/2000/svg"><text>offline</text></svg>'
        (folder/(chart_id+'.svg')).write_text(svg,encoding='utf-8')
        (folder/(chart_id+'.data.json')).write_text('[{"step":4,"loss":2.5}]',encoding='utf-8')
        self.runner.runs[identifier]={'status':'success','artifact_dir':str(folder.parent),'visualizations':[{'id':chart_id}],
            'signature':self.runner.signature(self.store.load(identifier),self.store.validate(self.store.load(identifier)))}
        with urlopen(self.url+f'/api/tasks/{identifier}/visualizations/{chart_id}') as response:
            self.assertIn('image/svg+xml',response.headers['Content-Type'])
            self.assertIn('sandbox',response.headers['Content-Security-Policy'])
            self.assertEqual(response.read().decode(),svg)
        self.assertEqual(self.request(f'/api/tasks/{identifier}/visualizations/{chart_id}/data'),[{'step':4,'loss':2.5}])
        with self.assertRaises(HTTPError):
            urlopen(self.url+f'/api/tasks/{identifier}/visualizations/'+('b'*32))
        with self.assertRaises(HTTPError):
            urlopen(self.url+f'/api/tasks/{identifier}/visualizations/'+('b'*32)+'/data')

    def test_chart_module_has_browser_compatible_javascript_mime(self):
        with urlopen(self.url+'/charts.mjs') as response:
            self.assertEqual(response.headers['Content-Type'],'application/javascript; charset=utf-8')
            self.assertIn(b'export function buildFigure',response.read())


if __name__=='__main__':unittest.main()
