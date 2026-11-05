"""Shared host-side helpers embedded into standalone training stage scripts."""
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import threading
from connection_errors import CONNECTION_PREFIX, is_connection_message, report_connection_failure
from connection_errors import connection_print as print

C = json.loads(base64.b64decode(os.environ['ENODEDONE_CONTEXT']))
HOSTS, ROOT = C['hosts'], Path(C['master_run'])
LOCAL = Path('/tmp/enodedone') / C['run_id']
EVENT_LOCK = threading.Lock()


SSH_CONTROL_HELPERS = r'''
def ssh_control_path(run_id,uid):
    # Reserve space for %C (40 bytes) AND OpenSSH's temporary .<16 random bytes> suffix.
    token=hashlib.sha256(str(run_id).encode()).hexdigest()[:16]
    return Path('/tmp')/('ed'+str(uid))/token/'%C'

def ssh_control_options(run_id):
    uid=os.geteuid()
    path=ssh_control_path(run_id,uid)
    for directory in (path.parent.parent,path.parent):
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid!=uid:
            raise RuntimeError('SSH control directory must be owned by the current user, without symlinks')
        directory.chmod(0o700)
    return ['-o','BatchMode=yes','-o','ConnectTimeout=15','-o','ControlMaster=auto',
            '-o','ControlPersist=60','-o','ControlPath='+str(path)]
'''


exec(SSH_CONTROL_HELPERS)


def event(rank, status, message):
    with EVENT_LOCK:
        print('@@ENODEDONE@@'+json.dumps({'rank':rank,'status':status,'message':message},ensure_ascii=False),flush=True)


def command(args, **kwargs):
    result = subprocess.run(args,text=True,capture_output=True,timeout=C['timeout'],**kwargs)
    if result.returncode:
        for line in result.stdout.splitlines():
            if line.startswith(CONNECTION_PREFIX):print(line,flush=True)
        if args[0] in ('ssh','scp') and result.returncode==255 and is_connection_message(result.stderr):
            ips=[host['Info'] for host in HOSTS if any(str(arg)==peer_user(host['Info'])+'@'+host['Info'] or
                 str(arg).startswith(peer_user(host['Info'])+'@'+host['Info']+':') for arg in args)]
            if ips:report_connection_failure(ips,result.stderr)
        raise RuntimeError(f'{args[0]} 返回 {result.returncode}: {result.stderr[-3000:]}')
    return result.stdout


def peer_user(ip):
    return next((h.get('User') or 'root' for h in HOSTS if h['Info']==ip),'root')


def peer(ip, script):
    if ip==HOSTS[0]['Info']:return command(['python3','-u','-'],input=script)
    return command(['ssh',*ssh_options(),peer_user(ip)+'@'+ip,'python3 -u -'],input=script)


def ssh_options():
    return ssh_control_options(C['run_id'])


def parallel(fn):
    failures=[]
    with ThreadPoolExecutor(max_workers=min(32,len(HOSTS))) as pool:
        jobs={pool.submit(fn,rank,host):rank for rank,host in enumerate(HOSTS)}
        for future in as_completed(jobs):
            try: future.result()
            except Exception as error:
                event(jobs[future],'failed',str(error)); failures.append(str(error))
    if failures: raise RuntimeError('; '.join(failures))
