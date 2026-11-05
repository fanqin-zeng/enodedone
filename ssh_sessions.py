"""Reusable authenticated SSH sessions; commands are never retried after execution starts."""
from pathlib import Path
import codecs
import shlex
import threading
import time
import paramiko
from connection_errors import SSHConnectionError, is_connection_exception


class DisconnectTransport(paramiko.Transport):
    """Notify after Paramiko's reader thread handles EOF, disconnect or socket failure."""
    def __init__(self,*args,on_closed,**kwargs):
        self.on_closed=on_closed
        super().__init__(*args,**kwargs)

    def run(self):
        try:super().run()
        finally:self.on_closed(self)


class SSHSessionPool:
    def __init__(self,on_disconnect=None):
        self.clients = {}
        self.locks = {}
        self.lock = threading.Lock()
        self.native_targets = set()
        self.on_disconnect=on_disconnect

    def client(self, ip, settings):
        user = settings.get('host_users', {}).get(ip, settings.get('ssh_user', 'root'))
        key = (ip, user)
        with self.lock:
            host_lock = self.locks.setdefault(key, threading.Lock())
        # Different hosts connect concurrently; only the same host's initial handshake is serialized.
        with host_lock:
            persistent=settings.get('persistent_sessions',False)
            if key in self.native_targets and not persistent:return None, False
            existing = self.clients.get(key)
            if existing and existing.get_transport() and existing.get_transport().is_active():
                return existing, True
            if existing:existing.close()
            config = paramiko.SSHConfig()
            config_path = Path.home()/'.ssh/config'
            if config_path.exists():
                with config_path.open(encoding='utf-8-sig') as stream:config.parse(stream)
            options = config.lookup(ip)
            proxy=None
            if (options.get('proxycommand') or options.get('proxyjump')) and not persistent:
                self.native_targets.add(key)
                return None, False  # Keep native OpenSSH's proxy semantics for user-defined hosts.
            if options.get('proxycommand'):
                proxy=paramiko.ProxyCommand(options['proxycommand'])
            elif options.get('proxyjump'):
                destination=options.get('hostname',ip)+':'+str(options.get('port',22))
                proxy=paramiko.ProxyCommand('ssh -W '+shlex.quote(destination)+' '+shlex.quote(options['proxyjump']))
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            known_files = [Path.home()/'.ssh/enodedone/known_hosts']
            known_files.extend(Path(value).expanduser() for value in shlex.split(options.get('userknownhostsfile', '')))
            for path in known_files:
                if path.is_file():client.load_host_keys(str(path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            identities = [str(Path(p).expanduser()) for p in options.get('identityfile', []) if Path(p).expanduser().is_file()]
            managed = Path.home()/'.ssh/enodedone/id_ed25519'
            if managed.is_file() and str(managed) not in identities:identities.insert(0, str(managed))
            try:
                transport_options={}
                if self.on_disconnect:
                    def factory(*args,**kwargs):
                        return DisconnectTransport(*args,on_closed=lambda transport:
                            self.on_disconnect(ip,user,transport),**kwargs)
                    transport_options['transport_factory']=factory
                client.connect(hostname=options.get('hostname', ip), port=int(options.get('port', 22)), username=user,
                    key_filename=identities or None, allow_agent=options.get('identitiesonly') != 'yes',
                    look_for_keys=options.get('identitiesonly') != 'yes', timeout=int(settings.get('ssh_timeout', 15)),
                    banner_timeout=20, auth_timeout=20, **({'sock':proxy} if proxy else {}),**transport_options)
            except (paramiko.AuthenticationException,paramiko.PasswordRequiredException):
                client.close()
                if persistent:raise
                self.native_targets.add(key)
                return None, False  # Native agents/certificates may support more authentication methods.
            except BaseException:
                client.close();raise
            self.clients[key] = client
            return client, False

    def check_active(self,ip,settings):
        """Read Paramiko's local connection state without sending commands or packets."""
        user=settings.get('host_users',{}).get(ip,settings.get('ssh_user','root'))
        with self.lock:client=self.clients.get((ip,user))
        if not client or not client.get_transport() or not client.get_transport().is_active():
            raise SSHConnectionError([ip],'SSH 会话已断开')
        return client.get_transport()

    def disconnect(self,ip,settings):
        user=settings.get('host_users',{}).get(ip,settings.get('ssh_user','root'))
        with self.lock:
            client=self.clients.pop((ip,user),None);self.native_targets.discard((ip,user))
        if client:client.close()

    def run(self, ip, settings, command, payload, callback, stop, cancel):
        try:return self._run(ip,settings,command,payload,callback,stop,cancel)
        except SSHConnectionError:raise
        except Exception as error:
            if is_connection_exception(error):raise SSHConnectionError([ip],str(error)) from error
            raise

    def _run(self, ip, settings, command, payload, callback, stop, cancel):
        started = time.monotonic()
        if stop and stop.is_set():raise InterruptedError(getattr(stop,'reason',lambda:'用户停止任务')())
        if settings.get('require_existing_session') and ip in settings.get('host_users',{}):
            key=(ip,settings['host_users'][ip])
            with self.lock:client=self.clients.get(key)
            if not client or not client.get_transport() or not client.get_transport().is_active():
                raise SSHConnectionError([ip],'持久 SSH 会话未就绪，请建立连接')
            reused=True
        else:client, reused = self.client(ip, settings)
        if client is None:return None  # Caller uses native OpenSSH for proxy configurations.
        connected = time.monotonic()
        terminal=settings.get('terminal',False)
        if stop and stop.is_set():raise InterruptedError(getattr(stop,'reason',lambda:'用户停止任务')())
        if callback and not terminal:callback('SSH '+('复用会话' if reused else '首次建连')+' 用时 '+format(connected-started,'.3f')+'s')
        if terminal:
            channel=client.get_transport().open_session(timeout=int(settings.get('ssh_timeout',15)))
            try:
                channel.get_pty(term='dumb',width=100,height=30)
                channel.exec_command(command)
                stdin=channel.makefile_stdin('w');stdout=channel.makefile('r');stderr=channel.makefile_stderr('r')
            except BaseException:channel.close();raise
        else:stdin, stdout, stderr = client.exec_command(command, timeout=int(settings.get('ssh_timeout', 15)))
        channel = stdout.channel
        channel.settimeout(0)
        output, errors = [], []
        buffers=['',''];decoders=[codecs.getincrementaldecoder('utf-8')(errors='replace') for _ in range(2)]
        def deliver(index, raw, final=False):
            decoded=decoders[index].decode(raw,final=final)
            (output if index==0 else errors).append(decoded)
            if terminal:
                if decoded and callback:callback(decoded)
                return
            buffers[index]+=decoded
            while '\n' in buffers[index]:
                line,buffers[index]=buffers[index].split('\n',1)
                if callback:callback(('' if index==0 else '[stderr] ')+line.rstrip('\r'))
            if final and buffers[index] and callback:
                callback(('' if index==0 else '[stderr] ')+buffers[index]);buffers[index]=''
        try:
            # Pipeline scripts read to EOF; terminal commands keep the PTY input open until exit.
            channel.settimeout(int(settings.get('command_timeout',1800)))
            stdin.write(payload);stdin.flush()
            if not terminal:channel.shutdown_write()
            channel.settimeout(0)
            deadline = started+int(settings.get('command_timeout', 1800))
            while True:
                if (stop and stop.is_set()) or time.monotonic()>deadline:
                    raise InterruptedError(getattr(stop,'reason',lambda:'用户停止任务')() if stop and stop.is_set() else '远程命令超时')
                active=False
                for index,ready,receive in ((0,channel.recv_ready,channel.recv),(1,channel.recv_stderr_ready,channel.recv_stderr)):
                    for _ in range(32):
                        if not ready():break
                        raw=receive(65536)
                        if not raw:break
                        deliver(index,raw);active=True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
                if not active:time.sleep(.02)
            for index in range(2):deliver(index,b'',final=True)
            status=channel.recv_exit_status()
            if status==-1:raise SSHConnectionError([ip],'SSH 通道断开，未收到远程退出码')
            if status and not terminal:raise RuntimeError(f'{ip} SSH/脚本返回 {status}: '+''.join(output+errors)[-2000:])
            if callback and not terminal:callback('远程命令及输出 用时 '+format(time.monotonic()-connected,'.3f')+'s')
            return ''.join(output)
        except BaseException:
            if not channel.exit_status_ready():cancel()
            raise
        finally:
            channel.close();stdin.close();stdout.close();stderr.close()

    def close(self):
        with self.lock:
            clients = list(self.clients.values());self.clients.clear()
        for client in clients:client.close()
