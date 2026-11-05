"""Offline connection reuse and nonblocking output checks; never contact an SSH server."""
from collections import deque
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from ssh_sessions import SSHSessionPool


class Channel:
    def __init__(self, chunks=(), errors=(), status=0):
        self.output=deque(chunks);self.errors=deque(errors);self.status=status;self.closed=False
    def settimeout(self,value):pass
    def shutdown_write(self):pass
    def recv_ready(self):return bool(self.output)
    def recv_stderr_ready(self):return bool(self.errors)
    def recv(self,size):return self.output.popleft()
    def recv_stderr(self,size):return self.errors.popleft()
    def exit_status_ready(self):return self.status is not None
    def recv_exit_status(self):return self.status
    def close(self):self.closed=True


class SessionTests(unittest.TestCase):
    def streams(self,channel):
        stdin=io.StringIO();stdout=io.BytesIO();stderr=io.BytesIO();stdout.channel=channel
        return stdin,stdout,stderr

    def test_parallel_commands_share_one_authenticated_client(self):
        import paramiko
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory,patch('ssh_sessions.Path.home',return_value=Path(directory)), \
             patch('paramiko.SSHClient') as factory:
            pool=SSHSessionPool();client=factory.return_value
            client.get_transport.return_value.is_active.return_value=True
            with ThreadPoolExecutor(max_workers=8) as workers:
                results=list(workers.map(lambda _:pool.client('192.0.2.1',{}),range(16)))
            factory.assert_called_once();client.connect.assert_called_once()
            self.assertEqual(sum(not reused for _,reused in results),1)
            self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0],paramiko.RejectPolicy)
            self.assertEqual(client.connect.call_args.kwargs['username'],'root')
            pool.close();client.close.assert_called_once()

    def test_different_hosts_connect_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(2,timeout=3)
        def factory():
            client=MagicMock();client.connect.side_effect=lambda **kwargs:barrier.wait();return client
        with tempfile.TemporaryDirectory() as directory,patch('ssh_sessions.Path.home',return_value=Path(directory)), \
             patch('paramiko.SSHClient',side_effect=factory):
            pool=SSHSessionPool()
            with ThreadPoolExecutor(max_workers=2) as workers:
                results=list(workers.map(lambda ip:pool.client(ip,{}),['192.0.2.1','192.0.2.2']))
            self.assertEqual(len(results),2);pool.close()

    def test_output_is_drained_before_exit_status_and_utf8_chunks_are_preserved(self):
        pool=SSHSessionPool();client=MagicMock();encoded='中文输出\n尾行'.encode('utf-8')
        channel=Channel([encoded[:2],encoded[2:7],encoded[7:]],[b'SSH banner\n'])
        client.exec_command.return_value=self.streams(channel);lines=[]
        with patch.object(pool,'client',return_value=(client,True)):
            result=pool.run('192.0.2.1',{},'command','',lines.append,None,lambda:None)
        self.assertEqual(result,'中文输出\n尾行');self.assertIn('中文输出',lines);self.assertIn('尾行',lines)
        self.assertIn('[stderr] SSH banner',lines);self.assertTrue(channel.closed)
        client.exec_command.assert_called_once()

    def test_failed_command_is_not_retried(self):
        pool=SSHSessionPool();client=MagicMock();channel=Channel([b'failed output\n'],status=7)
        client.exec_command.return_value=self.streams(channel)
        with patch.object(pool,'client',return_value=(client,True)),self.assertRaisesRegex(RuntimeError,'7'):
            pool.run('192.0.2.1',{},'side-effecting command','',None,None,lambda:None)
        client.exec_command.assert_called_once()

    def test_console_uses_pty_and_preserves_raw_columns_without_internal_logs(self):
        pool=SSHSessionPool();client=MagicMock();channel=Channel([b'data  pipeline_scripts  runs\r\n'])
        stdin,stdout,stderr=self.streams(channel)
        channel.get_pty=MagicMock();channel.exec_command=MagicMock()
        channel.makefile_stdin=MagicMock(return_value=stdin);channel.makefile=MagicMock(return_value=stdout)
        channel.makefile_stderr=MagicMock(return_value=stderr)
        client.get_transport.return_value.open_session.return_value=channel;output=[]
        with patch.object(pool,'client',return_value=(client,True)):
            result=pool.run('192.0.2.1',{'terminal':True},'ls','',output.append,None,lambda:None)
        self.assertEqual(result,'data  pipeline_scripts  runs\r\n');self.assertEqual(output,[result])
        channel.get_pty.assert_called_once_with(term='dumb',width=100,height=30)
        channel.exec_command.assert_called_once_with('ls');client.exec_command.assert_not_called()

    def test_console_stderr_and_nonzero_exit_are_not_reprinted_as_runner_errors(self):
        pool=SSHSessionPool();client=MagicMock();channel=Channel(errors=[b'ls: missing file\r\n'],status=2)
        stdin,stdout,stderr=self.streams(channel)
        channel.get_pty=MagicMock();channel.exec_command=MagicMock()
        channel.makefile_stdin=MagicMock(return_value=stdin);channel.makefile=MagicMock(return_value=stdout)
        channel.makefile_stderr=MagicMock(return_value=stderr)
        client.get_transport.return_value.open_session.return_value=channel;output=[]
        with patch.object(pool,'client',return_value=(client,True)):
            pool.run('192.0.2.1',{'terminal':True},'ls missing','',output.append,None,lambda:None)
        self.assertEqual(output,['ls: missing file\r\n'])

    def test_stop_cancels_registered_command_once_and_closes_channel(self):
        pool=SSHSessionPool();client=MagicMock();channel=Channel([b'running\n'],status=None)
        client.exec_command.return_value=self.streams(channel);stop=threading.Event();cancel=MagicMock()
        def output(line):
            if line=='running':stop.set()
        with patch.object(pool,'client',return_value=(client,False)),self.assertRaises(InterruptedError):
            pool.run('192.0.2.1',{},'command','',output,stop,cancel)
        cancel.assert_called_once();self.assertTrue(channel.closed)

    def test_host_key_failure_does_not_fall_back_to_native_ssh(self):
        import paramiko
        key=MagicMock();key.get_name.return_value='ssh-ed25519';key.get_base64.return_value='AAA'
        with tempfile.TemporaryDirectory() as directory,patch('ssh_sessions.Path.home',return_value=Path(directory)), \
             patch('paramiko.SSHClient') as factory:
            factory.return_value.connect.side_effect=paramiko.BadHostKeyException('192.0.2.1',key,key)
            pool=SSHSessionPool()
            with self.assertRaises(paramiko.BadHostKeyException):pool.client('192.0.2.1',{})
            self.assertFalse(pool.native_targets)

    def test_persistent_auth_failure_cannot_fall_back_to_ephemeral_native_session(self):
        import paramiko
        with tempfile.TemporaryDirectory() as directory,patch('ssh_sessions.Path.home',return_value=Path(directory)), \
             patch('paramiko.SSHClient') as factory:
            factory.return_value.connect.side_effect=paramiko.AuthenticationException('denied')
            pool=SSHSessionPool()
            with self.assertRaises(paramiko.AuthenticationException):pool.client('192.0.2.1',{'persistent_sessions':True})
            self.assertFalse(pool.native_targets);self.assertFalse(pool.clients)

    def test_passive_state_check_never_opens_channels_or_sends_commands(self):
        pool=SSHSessionPool();client=MagicMock();transport=client.get_transport.return_value
        transport.is_active.return_value=True;pool.clients[('192.0.2.1','root')]=client
        with patch.object(pool,'client') as connect:
            for _ in range(10):self.assertIs(pool.check_active('192.0.2.1',{}),transport)
        connect.assert_not_called();transport.open_session.assert_not_called();client.exec_command.assert_not_called()
        transport.set_keepalive.assert_not_called()

    def test_passive_state_check_rejects_inactive_transport_without_reconnecting(self):
        from connection_errors import SSHConnectionError
        pool=SSHSessionPool();client=MagicMock();pool.clients[('192.0.2.1','root')]=client
        client.get_transport.return_value.is_active.return_value=False
        with patch.object(pool,'client') as connect,self.assertRaises(SSHConnectionError):pool.check_active('192.0.2.1',{})
        connect.assert_not_called();client.get_transport.return_value.open_session.assert_not_called()

    def test_transport_reader_eof_emits_notification_without_heartbeat_commands(self):
        import socket
        from queue import Queue
        from ssh_sessions import DisconnectTransport
        local,peer=socket.socketpair();events=Queue()
        transport=DisconnectTransport(local,on_closed=events.put);transport.logger=MagicMock()
        self.addCleanup(transport.close);self.addCleanup(peer.close)
        transport.start_client(event=threading.Event())
        peer.close()  # Local socket fixture only: no SSH server or remote host is contacted.
        closed=events.get(timeout=3);transport.join(timeout=3)
        self.assertIs(closed,transport);self.assertFalse(transport.is_active());self.assertFalse(transport.is_alive())
        self.assertTrue(events.empty())


if __name__=='__main__':unittest.main()
