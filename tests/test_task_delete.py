"""Deletion fixtures live in temporary folders; no user tasks or SSH sessions are touched."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from core import TaskStore
from runner import SSHRunner


class TaskDeleteTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.store=TaskStore(self.root/'tasks')
        self.task=self.store.create('blank',count=1);self.id=self.task['id']

    def test_local_delete_is_recoverable_and_does_not_return_after_restart(self):
        folder=self.store.locations[self.id].parent
        history=folder/'runs'/'sample';history.mkdir(parents=True);(history/'log.txt').write_text('keep me',encoding='utf-8')
        csv_before=(folder/'machines.csv').read_bytes()
        result=self.store.delete(self.id);recovery=Path(result['recovery_path'])
        self.assertFalse(folder.exists());self.assertNotIn(self.id,self.store.locations)
        self.assertEqual(recovery.parent.parent,self.store.root/'.trash')
        self.assertEqual((recovery.parent/'machines.csv').read_bytes(),csv_before)
        self.assertEqual((recovery.parent/'runs'/'sample'/'log.txt').read_text(encoding='utf-8'),'keep me')
        restarted=TaskStore(self.store.root);self.assertEqual(restarted.listing(),[])
        self.assertEqual(restarted.open_file(recovery)['id'],self.id)

    def test_imported_task_is_removed_from_library_without_moving_source_files(self):
        source=TaskStore(self.root/'import-source');task=source.create('blank',count=1)
        path=source.locations[task['id']];before=path.read_bytes()
        self.store.open_file(path);result=self.store.delete(task['id'])
        self.assertTrue(result['source_preserved']);self.assertEqual(path.read_bytes(),before)
        self.assertTrue((path.parent/'machines.csv').is_file());self.assertNotIn(task['id'],self.store.locations)
        self.assertEqual(self.store.open_file(path)['id'],task['id'])

    def test_move_failure_preserves_task_and_library_entry(self):
        path=self.store.locations[self.id]
        with patch.object(Path,'rename',side_effect=PermissionError('folder open')),self.assertRaises(PermissionError):
            self.store.delete(self.id)
        self.assertIn(self.id,self.store.locations);self.assertTrue(path.is_file())

    def test_redirected_directory_cannot_be_moved(self):
        folder=self.store.locations[self.id].parent;original=Path.resolve
        def resolve(path,*args,**kwargs):
            if path==folder:return self.root/'outside'/'redirected'
            return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',resolve),patch.object(Path,'rename') as move,self.assertRaises(ValueError):
            self.store.delete(self.id)
        move.assert_not_called();self.assertTrue(self.store.locations[self.id].is_file())

    def test_active_task_or_console_cannot_be_deleted(self):
        runner=SSHRunner(self.store)
        for current in ({'status':'running'},{'status':'success','terminal_busy':True}):
            runner.runs[self.id]=current
            with patch.object(self.store,'delete') as remove,patch.object(runner.connections,'disconnect') as close,self.assertRaises(ValueError):
                runner.delete(self.id)
            remove.assert_not_called();close.assert_not_called()
        runner.runs.clear();runner.connections.entries[self.id]={'busy':True}
        with self.assertRaises(ValueError):runner.delete(self.id)
        self.assertTrue(self.store.locations[self.id].is_file())

    def test_runner_removes_completed_task_state_and_closes_its_pool(self):
        runner=SSHRunner(self.store);pool=MagicMock();runner.pools[self.id]=pool
        runner.runs[self.id]={'status':'success'}
        result=runner.delete(self.id)
        pool.close.assert_called_once();self.assertNotIn(self.id,runner.runs);self.assertNotIn(self.id,runner.pools)
        self.assertTrue(Path(result['recovery_path']).is_file())


if __name__=='__main__':unittest.main()
