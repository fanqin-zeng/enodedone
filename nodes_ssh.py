"""Task-owned SSH key bootstrap; run only as an explicit local pipeline stage."""
import base64
import csv
from functools import lru_cache
from itertools import islice
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

OUTPUT_LOCK = threading.Lock()
MAX_NODE_WORKERS = 8


def log(message):
    with OUTPUT_LOCK:print(message,flush=True)


def parallel_phase(hosts, operation, label, secrets=(), ranks=None):
    started=time.monotonic();results=[None]*len(hosts);errors=[]
    log(label+'：'+str(len(hosts))+' 台，并发上限 '+str(MAX_NODE_WORKERS))
    def work(rank, host):
        begin=time.monotonic()
        try:return operation(rank,host)
        finally:log(label+' '+host+' 用时 '+format(time.monotonic()-begin,'.2f')+'s')
    with ThreadPoolExecutor(max_workers=min(MAX_NODE_WORKERS,len(hosts))) as pool:
        jobs={pool.submit(work,ranks[index] if ranks is not None else index,host):(index,ranks[index] if ranks is not None else index) for index,host in enumerate(hosts)}
        for future in as_completed(jobs):
            index,rank=jobs[future]
            try:results[index]=future.result()
            except Exception as error:
                message=redact(str(error),secrets)
                event(rank,'failed',message);errors.append(hosts[index]+': '+message)
    log(label+' 阶段总用时 '+format(time.monotonic()-started,'.2f')+'s')
    if errors:raise RuntimeError('; '.join(errors))
    return results

# This source is embedded in both the task script and the remote installer.
HELPERS = r'''
def key_identity(line):
    words=line.split()
    for index,word in enumerate(words[:-1]):
        if word in ('ssh-ed25519','ssh-rsa') or word.startswith('ecdsa-sha2-'):
            return word,words[index+1]
    raise ValueError('Invalid public key')

def merge_authorized(original, keys):
    existing=set()
    for line in original.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        try:existing.add(key_identity(line))
        except ValueError:pass
    result=original
    for key in keys:
        identity=key_identity(key)
        if identity not in existing:
            if result and not result.endswith('\n'):result+='\n'
            result+=key.strip()+'\n';existing.add(identity)
    return result

def config_path(path):
    value=str(path).replace('\\','/')
    if any(c in value for c in ('"','\r','\n','%')):raise ValueError('Unsupported SSH path')
    return '"'+value+'"'

def merge_config(original, hosts, user, key, known, global_known):
    blocks=[];text=original
    for host in hosts:
        host_user=user.get(host,'root') if isinstance(user,dict) else user
        token=hashlib.sha256((host_user+'@'+host).encode()).hexdigest()[:16]
        begin='# BEGIN enodedone '+token;end='# END enodedone '+token
        pattern=re.compile(r'(?m)^'+re.escape(begin)+r'\r?\n.*?^'+re.escape(end)+r'(?:\r?\n)?',re.S)
        if text.count(begin)!=text.count(end) or text.count(begin)>1 or (begin in text and not pattern.search(text)):
            raise ValueError('Malformed managed SSH config block')
        text=pattern.sub('',text)
        blocks.append('\n'.join([begin,'Host '+host,'    HostName '+host,'    User '+host_user,
            '    IdentityFile '+config_path(key),'    IdentitiesOnly yes','    StrictHostKeyChecking yes',
            '    UserKnownHostsFile '+config_path(known)+' '+config_path(global_known),
            'Host *',end,'']))
    return ''.join(blocks)+text
'''
exec(HELPERS)

REMOTE = r'''
from pathlib import Path
import hashlib,json,os,re,subprocess,tempfile
D=json.loads(base64.b64decode(PAYLOAD))
home=Path.home();ssh=home/'.ssh';managed=ssh/'enodedone-nodes'
for directory in (ssh,managed):
    if directory.is_symlink():raise RuntimeError('Refusing SSH directory symlink')
    if D['action']!='inspect':directory.mkdir(mode=0o700,exist_ok=True);directory.chmod(0o700)
def regular(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):raise RuntimeError('Refusing non-regular SSH file')
def write(path, text, mode):
    regular(path)
    if path.exists() and path.read_text(encoding='utf-8')==text:
        if path.stat().st_mode & 0o777 != mode:path.chmod(mode)
        return
    fd,name=tempfile.mkstemp(prefix='.enodedone-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(text)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def install_authorized(keys):
    import fcntl
    auth=ssh/'authorized_keys';regular(auth)
    descriptor=os.open(auth,os.O_CREAT|os.O_RDWR|os.O_APPEND,0o600)
    with os.fdopen(descriptor,'r+',encoding='utf-8') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX);os.fchmod(stream.fileno(),0o600)
        original=stream.read();updated=merge_authorized(original,keys)
        if updated!=original:
            stream.write(updated[len(original):]);stream.flush();os.fsync(stream.fileno())
key=managed/('id_ed25519_'+hashlib.sha256(D['host'].encode()).hexdigest()[:16])
regular(key);regular(key.with_suffix('.pub'))
if D['action']=='inspect':
    files=[key,key.with_suffix('.pub'),ssh/'authorized_keys',managed/'known_hosts',ssh/'config']
    for path in files+[ssh/'known_hosts']:regular(path)
    ready=all(path.is_file() for path in files)
    digest=hashlib.sha256()
    if ready:
        for path in files+[ssh/'known_hosts']:
            digest.update(str(path).encode());digest.update(str(path.stat().st_mode & 0o777).encode() if path.exists() else b'missing')
            if path.exists():digest.update(path.read_bytes())
    print(json.dumps({'ready':ready,'stamp':digest.hexdigest() if ready else None}))
elif D['action']=='prepare':
    if not key.exists():
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key),'-C','enodedone-node'],check=True,timeout=30)
    key.chmod(0o600)
    public=subprocess.check_output(['ssh-keygen','-y','-f',str(key)],text=True,timeout=15).strip()+' enodedone-node'
    pub=key.with_suffix('.pub')
    if pub.exists() and key_identity(pub.read_text())!=key_identity(public):raise RuntimeError('Existing public/private key mismatch')
    if not pub.exists():write(pub,public+'\n',0o644)
    install_authorized([D['local_public']])
    print(json.dumps({'public':public}))
elif D['action']=='install':
    install_authorized(D['publics'])
    known=managed/'known_hosts';regular(known)
    text=known.read_text() if known.exists() else ''
    for host,key_type,key_data in D['host_keys']:
        for path in (ssh/'known_hosts',known):
            regular(path)
            if not path.exists():continue
            found=subprocess.run(['ssh-keygen','-F',host,'-f',str(path)],text=True,capture_output=True,timeout=15)
            if found.returncode not in (0,1):raise RuntimeError('Cannot inspect known_hosts')
            for line in found.stdout.splitlines():
                words=line.split()
                if len(words)>=3 and not line.startswith('#') and words[1]==key_type and words[2]!=key_data:
                    raise RuntimeError('SSH host key changed: '+host)
        line=host+' '+key_type+' '+key_data
        if line not in text.splitlines():text+=('' if not text or text.endswith('\n') else '\n')+line+'\n'
    write(known,text,0o600)
    config=ssh/'config';regular(config);original=config.read_text() if config.exists() else ''
    backup=managed/'config.before-enodedone';regular(backup)
    if config.exists() and not backup.exists():
        with backup.open('x') as stream:stream.write(original)
        backup.chmod(0o600)
    write(config,merge_config(original,D['hosts'],D['users'],key,known,ssh/'known_hosts'),0o600)
    print('Public keys and peer host keys installed')
elif D['action']=='verify':
    from concurrent.futures import ThreadPoolExecutor,as_completed
    def verify_peer(host):
        args=['ssh','-i',str(key),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
              '-o','PreferredAuthentications=publickey','-o','PasswordAuthentication=no',
              '-o','KbdInteractiveAuthentication=no','-o','StrictHostKeyChecking=yes',
              '-o','ConnectTimeout=15',D['users'][host]+'@'+host,"printf '%s' enodedone-public-key-ok"]
        result=subprocess.run(args,text=True,capture_output=True,timeout=30)
        if result.returncode or result.stdout!='enodedone-public-key-ok':
            raise RuntimeError('Public-key connection failed to '+host+': '+result.stderr[-1000:])
    failures=[]
    with ThreadPoolExecutor(max_workers=min(4,len(D['hosts']))) as pool:
        jobs={pool.submit(verify_peer,host):host for host in D['hosts']}
        for future in as_completed(jobs):
            try:future.result()
            except Exception as error:failures.append(str(error))
    if failures:raise RuntimeError('; '.join(failures))
    print('All peer public-key logins verified')
else:raise RuntimeError('Unknown installer action')
'''


def event(rank,status,message):
    # ASCII JSON remains valid even if a caller accidentally uses a legacy Windows code page.
    log('@@ENODEDONE@@'+json.dumps({'rank':rank,'status':status,'message':message},ensure_ascii=True))


@lru_cache(maxsize=1)
def windows_sid():
    sid=subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command',
        '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],text=True).strip()
    if not re.fullmatch(r'S-1-[0-9-]+',sid):raise RuntimeError('Cannot determine Windows user SID')
    return sid


def private_permissions(path, directory=False):
    if os.name!='nt':path.chmod(0o700 if directory else 0o600);return
    sid=windows_sid()
    grant='*'+sid+':'+('(OI)(CI)F' if directory else 'F')
    subprocess.run(['icacls.exe',str(path),'/inheritance:r','/grant:r',grant],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)


def local_key(paramiko, directory):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if directory.is_symlink():raise ValueError('Refusing local SSH directory symlink')
    directory.mkdir(parents=True,exist_ok=True);private_permissions(directory,True)
    path=directory/'id_ed25519'
    if path.is_symlink():raise ValueError('Refusing private key symlink')
    if not path.exists():
        value=Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.OpenSSH,serialization.NoEncryption())
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(value)
    private_permissions(path)
    key=paramiko.Ed25519Key.from_private_key_file(str(path))
    return key,key.get_name()+' '+key.get_base64()+' enodedone-local',path


def remote(client, payload):
    encoded=base64.b64encode(json.dumps(payload).encode()).decode()
    code='import base64\n'+HELPERS+'\nPAYLOAD='+repr(encoded)+'\n'+REMOTE
    timeout=30*((len(payload.get('hosts',[]))+3)//4)+10 if payload['action']=='verify' else 45
    stdin,stdout,stderr=client.exec_command('python3 -u -',timeout=timeout)
    stdin.write(code);stdin.flush();stdin.channel.shutdown_write()
    output=stdout.read().decode('utf-8',errors='replace');error=stderr.read().decode('utf-8',errors='replace')
    if stdout.channel.recv_exit_status():raise RuntimeError(error[-2000:] or output[-2000:])
    return output.strip()


def redact(message, passwords):
    for value in sorted({p for p in passwords if p},key=len,reverse=True):message=message.replace(value,'[REDACTED]')
    return message


def remember_host_key(paramiko, path, host, key, lock):
    # Concurrent first connections must merge host keys instead of overwriting one another.
    with lock:
        keys=paramiko.HostKeys()
        if path.exists():keys.load(str(path))
        existing=keys.lookup(host)
        if existing and key.get_name() in existing and existing[key.get_name()]!=key:
            raise paramiko.BadHostKeyException(host,key,existing[key.get_name()])
        keys.add(host,key.get_name(),key)
        import tempfile
        descriptor,name=tempfile.mkstemp(prefix='known.enodedone-',dir=path.parent);os.close(descriptor)
        temporary=Path(name)
        try:keys.save(str(temporary));os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)


def main(context, passwords):
    hosts=context['hosts'];addresses=[h['Info'] for h in hosts]
    users={h['Info']:h.get('User') or 'root' for h in hosts}
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',u) for u in users.values()):raise ValueError('Invalid SSH user')
    for address in addresses:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*',address):raise ValueError('Invalid SSH host')
    with Path(context['csv_path']).open(encoding='utf-8-sig',newline='') as stream:
        selected = islice(csv.DictReader(stream), context.get('machine_count'))
        rows={(row.get('Info') or '').strip():row for row in selected}
    pending=[]
    for rank,address in enumerate(addresses):
        if address in context.get('key_ready',[]) or (rows[address].get('Connected') or '').strip().upper()=='Y':
            event(rank,'success','复用已验证的公钥配置')
        else:pending.append(rank)
    if not pending:
        log('全部节点公钥配置可复用。');return
    import paramiko
    for address in addresses:
        passwords.append(rows[address].get('Pwd') or '')
    directory=Path.home()/'.ssh'/'enodedone';key,public,key_path=local_key(paramiko,directory)
    known=directory/'known_hosts';global_known=Path.home()/'.ssh'/'known_hosts'
    if known.is_symlink():raise ValueError('Refusing managed known_hosts symlink')
    config=paramiko.SSHConfig();config_path_local=Path.home()/'.ssh'/'config'
    if config_path_local.exists():
        with config_path_local.open(encoding='utf-8-sig') as stream:config.parse(stream)
    identities={host:[str(Path(p).expanduser()) for p in config.lookup(host).get('identityfile',[]) if Path(p).expanduser().is_file()] for host in addresses}
    known_lock=threading.Lock()
    def connect(host,password=None,only_key=False):
        client=paramiko.SSHClient()
        if global_known.exists():client.load_system_host_keys(str(global_known))
        with known_lock:
            if known.exists():client.load_host_keys(str(known))
        class TrustNew(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, ssh_client, hostname, value):
                remember_host_key(paramiko,known,hostname,value,known_lock)
                ssh_client.get_host_keys().add(hostname,value.get_name(),value)
                fp=base64.b64encode(hashlib.sha256(value.asbytes()).digest()).decode().rstrip('=')
                log('首次记录主机指纹 '+hostname+' SHA256:'+fp)
        client.set_missing_host_key_policy(TrustNew())
        try:
            client.connect(hostname=host,username=users[host],pkey=key if password is None else None,
                password=password,key_filename=None if only_key or password is not None else identities[host],
                allow_agent=not only_key and password is None,look_for_keys=not only_key and password is None,
                timeout=15,banner_timeout=20,auth_timeout=20)
            return client
        except BaseException:client.close();raise
    clients=[None]*len(addresses);managed_auth=[False]*len(addresses)
    client_locks=[threading.Lock() for _ in hosts];auth_rejected=set()
    def existing_client(rank):
        with client_locks[rank]:
            if clients[rank] is None:
                clients[rank]=connect(addresses[rank],only_key=True);managed_auth[rank]=True
            return clients[rank]
    try:
        def quick_check(rank,host):
            event(rank,'running','验证已有公钥连接')
            try:
                existing_client(rank)
                if rank!=0:
                    try:master_client=existing_client(0)
                    except Exception:
                        event(0,'failed','无法连接 master，请检查连接配置');raise
                    remote(master_client,{'action':'verify','host':addresses[0],'hosts':[host],'users':users})
                event(rank,'success','本机及 master 公钥连接通过，复用已有配置')
                return True
            except paramiko.AuthenticationException:
                auth_rejected.add(rank);return False
            except RuntimeError as error:
                if any(token in str(error).lower() for token in ('permission denied (publickey','identity file','no such file or directory')):
                    return False
                raise
        checks=parallel_phase([addresses[rank] for rank in pending],quick_check,'验证待连接节点',passwords,ranks=pending)
        if all(checks):log('待连接节点已通过验证，无需生成或上传公钥。');return
        log('待连接节点的公钥需要修复，将执行必要的集群互信配置同步。')
        def prepare(rank,host):
            event(rank,'running','修复或同步节点互信配置')
            client=clients[rank]
            if client is None:
                try:
                    if rank in auth_rejected:raise paramiko.AuthenticationException('Managed key unavailable')
                    client=connect(host,only_key=True);managed_auth[rank]=True
                except paramiko.AuthenticationException:
                    managed_auth[rank]=False
                    try:client=connect(host)
                    except paramiko.AuthenticationException:
                        password=rows[host].get('Pwd')
                        if not password:raise RuntimeError('公钥认证失败，且该任务 CSV 的 Pwd 为空: '+host) from None
                        client=connect(host,password=password)
                clients[rank]=client
            try:
                prepared=json.loads(remote(client,{'action':'prepare','host':host,'local_public':public}))
                if managed_auth[rank]:fresh=client
                else:client.close();fresh=connect(host,only_key=True)
            except BaseException:client.close();raise
            clients[rank]=fresh
            server_key=fresh.get_transport().get_remote_server_key()
            event(rank,'running','本机公钥认证已独立验证，等待建立节点间连接')
            return prepared['public'],(host,server_key.get_name(),server_key.get_base64())
        prepared=parallel_phase(addresses,prepare,'准备公钥',passwords)
        publics=[public]+[value[0] for value in prepared];host_keys=[value[1] for value in prepared]
        # Publish verified host keys, not keys obtained through unauthenticated ssh-keyscan.
        def install(rank,host):
            client=clients[rank]
            remote(client,{'action':'install','host':host,'hosts':addresses,'users':users,'publics':publics,'host_keys':host_keys})
            event(rank,'running','已配置节点公钥与 known_hosts，等待互联验证')
        parallel_phase(addresses,install,'配置节点互信',passwords)
        def verify(rank,host):
            client=clients[rank]
            remote(client,{'action':'verify','host':host,'hosts':addresses,'users':users})
            event(rank,'success','本机连接及到全部 '+str(len(hosts))+' 个节点的公钥连接均已验证')
        parallel_phase(addresses,verify,'验证节点互联',passwords)
        if config_path_local.is_symlink():raise ValueError('Refusing local SSH config symlink')
        original=config_path_local.read_text(encoding='utf-8-sig') if config_path_local.exists() else ''
        updated=merge_config(original,addresses,users,key_path.resolve(),known.resolve(),global_known.resolve())
        backup=directory/'config.before-enodedone'
        if backup.is_symlink():raise ValueError('Refusing backup symlink')
        if config_path_local.exists() and not backup.exists():
            with backup.open('x',encoding='utf-8') as stream:stream.write(original)
            private_permissions(backup)
        import tempfile
        if updated!=original:
            fd,name=tempfile.mkstemp(prefix='config.enodedone-',dir=config_path_local.parent)
            temporary=Path(name)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(updated)
                private_permissions(temporary);os.replace(temporary,config_path_local)
            finally:temporary.unlink(missing_ok=True)
        log('NODES SSH 公钥连接建立完毕；私钥未离开各自所在机器。')
    finally:
        for client in clients:
            if client is not None:client.close()


if __name__=='__main__':
    passwords=[]
    try:main(json.load(sys.stdin),passwords)
    except Exception as error:
        print('SSH 公钥节点失败: '+redact(str(error),passwords),file=sys.stderr,flush=True);sys.exit(1)
    finally:passwords.clear()
