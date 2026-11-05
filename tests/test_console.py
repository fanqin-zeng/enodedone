"""Manual console output is independent of pipeline logs; no remote commands are run."""
import csv
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock,patch

from core import TaskStore
from machine_state import CSV_FIELDS
from runner import SSHRunner


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);self.store=TaskStore(root/'tasks');task=self.store.create('blank',count=1)
        with Path(task['csv_file']).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader()
            writer.writerow({'Machine':'n0','Info':'192.0.2.1','User':'root','Pwd':'','Connected':'N'})
        task['config']['master_ip']='192.0.2.1';self.store.save(task);self.id=task['id']
        self.runner=SSHRunner(self.store);folder=root/'run';folder.mkdir()
        self.run={'id':'offline','status':'success','artifact_dir':str(folder),'console':['pipeline diagnostic'],
                  'signature':self.runner.signature(task,self.store.validate(task))}
        self.runner.runs[self.id]=self.run;self.transport=MagicMock(settings={})

    def wait(self):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with self.runner.lock:
                if not self.run.get('terminal_busy'):return
            threading.Event().wait(.01)
        self.fail('Console command did not finish')

    def test_only_user_command_and_raw_result_enter_manual_console(self):
        def execute(ip,command,callback):callback('data  pipeline_scripts  runs\r\n')
        self.transport.run.side_effect=execute
        with patch.object(self.runner.connections,'ensure'),patch.object(self.runner,'transport',return_value=self.transport):
            self.runner.console(self.id,'ls','host');self.wait()
        self.assertEqual(self.run['console'],['pipeline diagnostic'])
        self.assertEqual(''.join(item['text'] for item in self.run['terminal']),'$ ls\ndata  pipeline_scripts  runs\r\n')
        self.assertTrue(all('time' in item for item in self.run['terminal']))
        self.assertTrue(self.transport.settings['terminal'])
        self.assertIn('cd ',self.transport.run.call_args.args[1])  # Internal working-directory setup stays internal.

    def test_manual_commands_are_serial_and_cannot_overlap(self):
        release=threading.Event();started=threading.Event()
        def execute(*args,**kwargs):started.set();release.wait(timeout=2)
        self.transport.run.side_effect=execute
        with patch.object(self.runner.connections,'ensure'),patch.object(self.runner,'transport',return_value=self.transport):
            self.runner.console(self.id,'sleep 1','host');self.assertTrue(started.wait(timeout=1))
            try:
                with self.assertRaisesRegex(ValueError,'仍在执行'):self.runner.console(self.id,'ls','host')
            finally:release.set();self.wait()
        self.assertEqual(len(self.run['terminal']),1)

    def test_clear_terminal_does_not_clear_pipeline_logs(self):
        self.run['terminal']=[{'time':'2026-11-01T00:00:00+00:00','text':'old output\n'}]
        self.runner.console(self.id,'clear','host')
        self.assertEqual(self.run['terminal'],[]);self.assertEqual(self.run['console'],['pipeline diagnostic'])


if __name__=='__main__':unittest.main()
