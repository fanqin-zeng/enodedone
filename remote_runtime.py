"""Standard-library remote stages embedded in each editable Python script."""
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import csv
import gzip
import hashlib
import html
import io
import json
import os
from pathlib import Path
import random
import shlex
import subprocess
import shutil
import tarfile
import time
import threading
from connection_errors import CONNECTION_HELPERS, CONNECTION_PREFIX, is_connection_message, report_connection_failure
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


def push():
    raise RuntimeError('脚本推送由本地 SSH 执行器处理')


def integer_param(name, default, minimum=0):
    try:value=int(os.environ.get(name,str(default)))
    except ValueError:raise ValueError(name+' 必须是整数') from None
    if value<minimum:raise ValueError(name+' 必须不小于 '+str(minimum))
    return value


def ping():
    count=integer_param('PING_COUNT',1,1)
    timeout=integer_param('PING_TIMEOUT_SECONDS',5,1)
    with ThreadPoolExecutor(max_workers=min(32,max(1,len(HOSTS)-1))) as pool:
        list(pool.map(lambda host:print(command(['ping','-c',str(count),'-W',str(timeout),host['Info']]),flush=True),HOSTS[1:]))


from image_files import FILE_HELPERS, SOURCE_HELPERS

exec(FILE_HELPERS)
exec(SOURCE_HELPERS)

DOCKER_HELPER = r'''
def apply_docker(path,sha,timeout):
    directory=Path.home()/'.cache/enodedone/docker-load'
    marker=directory/(sha+'.json')
    if marker.is_symlink():raise RuntimeError('Invalid Docker state file')
    cached={}
    if marker.is_file():
        try:cached=json.loads(marker.read_text(encoding='utf-8'))
        except ValueError:pass
    def inspect(refs):
        return subprocess.run(['docker','image','inspect','--format','{{.Id}}','--',*refs],
            text=True,capture_output=True,timeout=timeout)
    if isinstance(cached,dict) and cached.get('sha256')==sha and cached.get('refs') and cached.get('ids'):
        checked=inspect(cached['refs'])
        if checked.returncode==0 and checked.stdout.splitlines()==cached['ids']:
            print('镜像引用和 ID 已核实，复用已加载镜像，跳过 docker load',flush=True);return
    loaded=subprocess.run(['docker','load','-i',path],text=True,capture_output=True,timeout=timeout)
    if loaded.returncode:raise RuntimeError(loaded.stderr[-2000:] or loaded.stdout[-2000:])
    print(loaded.stdout,flush=True)
    refs=[]
    for line in loaded.stdout.splitlines():
        for prefix in ('Loaded image: ','Loaded image ID: '):
            if line.startswith(prefix):refs.append(line[len(prefix):].strip())
    if refs:
        checked=inspect(refs)
        if checked.returncode==0 and len(checked.stdout.splitlines())==len(refs):
            directory.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix='.load-',dir=directory)
            try:
                with os.fdopen(fd,'w',encoding='utf-8') as stream:
                    json.dump({'sha256':sha,'refs':refs,'ids':checked.stdout.splitlines()},stream)
                os.replace(name,marker)
            finally:
                if os.path.exists(name):os.unlink(name)
        else:print('docker load 已完成；无法记录装载缓存，下次仍检查装载',flush=True)
'''


def init():
    ROOT.mkdir(parents=True,exist_ok=True)
    command(['bash','-lc','source /home/pkg/cann920/cann/set_env.sh && python3 --version'])


def distribute():
    source, dest=Path(C['image_path']),C['image_dest']
    snapshot=json.loads(os.environ['ENODEDONE_DISTRIBUTION_PREFLIGHT']) if os.environ.get('ENODEDONE_DISTRIBUTION_PREFLIGHT') else None
    if snapshot is not None:
        if snapshot.get('version')!=1 or snapshot.get('source_ip')!=HOSTS[0]['Info'] or snapshot.get('image_path')!=C['image_path'] or snapshot.get('image_dest')!=dest:
            raise ValueError('本地 SHA 检查结果与当前镜像配置不一致')
        sha=snapshot['sha256'];available=snapshot['available'][:];pending=snapshot['pending'][:]
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('无效的源 SHA256')
        if any(type(rank) is not int for rank in available+pending) or sorted(available+pending)!=list(range(len(HOSTS))):
            raise ValueError('本地 SHA 检查结果的机器清单不完整')
        for rank in available:event(rank,'success' if C['artifact_kind']=='archive' else 'running',
            'SHA256 一致，复用，跳过分发与应用' if C['artifact_kind']=='archive' else 'SHA256 一致，等待确认 Docker 装载状态')
        replacements=snapshot['replace']
        if any(key not in [str(rank) for rank in pending] or not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value) for key,value in replacements.items()):
            raise ValueError('无效的待替换 SHA256')
    else:
        prepare_image_source(source,C['artifact_kind'])
        started=time.monotonic()
        sha=command(['python3','-c','from pathlib import Path\nimport hashlib,os\n'+FILE_HELPERS+'\nprint(digest_file('+repr(str(source))+'))']).strip()
        print('Master 源 SHA 用时 '+format(time.monotonic()-started,'.3f')+'s',flush=True)
        replacements={}
    print('源镜像 SHA256: '+sha,flush=True)
    print('@@ENODEDONE_SOURCE@@'+json.dumps({'ip':HOSTS[0]['Info'],'path':str(source),'sha256':sha}),flush=True)
    if snapshot is None:available,pending=[],[]
    lock=threading.Lock()
    # Parallel preflight still finishes everywhere before any data is copied.
    def preflight(rank,host):
        begin=time.monotonic()
        code='from pathlib import Path\nimport hashlib,os\n'+FILE_HELPERS+'\np=Path('+repr(dest)+")\nif p.is_symlink() or (p.exists() and not p.is_file()):raise RuntimeError('目标不是普通文件')\n"
        code+="p.parent.mkdir(parents=True,exist_ok=True)\nif not p.exists():print('MISSING')\nelse:print(digest_file(p))\n"
        existing=peer(host['Info'],code).strip()
        if existing!='MISSING' and existing!=sha:
            event(rank,'failed','SHA256 不一致，禁止覆盖');raise RuntimeError('SHA 冲突: '+host['Info'])
        with lock:(pending if existing=='MISSING' else available).append(rank)
        event(rank,'running','目标缺失' if existing=='MISSING' else 'SHA 一致，复用')
        print('目标检查与 SHA '+host['Info']+' 用时 '+format(time.monotonic()-begin,'.3f')+'s',flush=True)
    if snapshot is None:parallel(preflight)
    available.sort();pending.sort();need_apply=set(pending) if snapshot is not None and C['artifact_kind']=='archive' else set(range(len(HOSTS)))
    if 0 in pending:
        incoming=dest+'.incoming-'+C['run_id']
        code='from pathlib import Path\nimport shutil,hashlib,os\n'+FILE_HELPERS+'\nSEED_SOURCE='+repr(str(source))+'\np=Path('+repr(incoming)+')\np.parent.mkdir(parents=True,exist_ok=True)\n'
        code+="with p.open('xb') as target,Path(SEED_SOURCE).open('rb') as stream:shutil.copyfileobj(stream,target)\nchecked_publish(p,"+repr(dest)+','+repr(sha)+','+repr(replacements.get('0'))+')'
        peer(HOSTS[0]['Info'],code)
        pending.remove(0);available.insert(0,0)
    # Keep the reviewable doubling plan, but schedule each edge as soon as its source is ready.
    planned=[];planned_sources=list(available);remaining=list(pending);round_number=0
    while remaining:
        targets=remaining[:len(planned_sources)];remaining=remaining[len(planned_sources):]
        edges=list(zip(planned_sources,targets));round_number+=1;planned.extend(edges)
        print('分发轮次 '+str(round_number)+': '+str([(HOSTS[a]['Info'],HOSTS[b]['Info']) for a,b in edges]),flush=True)
        print('@@ENODEDONE_PLAN@@'+json.dumps([{'source':HOSTS[a]['Info'],'target':HOSTS[b]['Info']} for a,b in edges]),flush=True)
        planned_sources.extend(targets)
    def copy_pair(pair):
        a,b=pair;target=HOSTS[b]['Info'];begin=time.monotonic()
        # Receiver pulls, verifies once and atomically publishes in a single remote command.
        args=['scp','-O',peer_user(HOSTS[a]['Info'])+'@'+HOSTS[a]['Info']+':'+shlex.quote(dest)]
        code='from pathlib import Path\nimport subprocess,tempfile,time,os,hashlib\n'+FILE_HELPERS+'\n'+SSH_CONTROL_HELPERS+'\n'+CONNECTION_HELPERS+'\nTRANSFER_ARGS='+repr(args)+'\n'
        code+='directory=Path('+repr(dest)+').parent\ndirectory.mkdir(parents=True,exist_ok=True)\n'
        code+='fd,name=tempfile.mkstemp(prefix=".enodedone-'+C['run_id']+'-",dir=directory)\nos.close(fd)\np=Path(name)\n'
        code+='try:\n start=time.monotonic()\n result=subprocess.run(TRANSFER_ARGS[:-1]+ssh_control_options('+repr(C['run_id'])+')+[TRANSFER_ARGS[-1],str(p)],text=True,capture_output=True,timeout='+str(C['timeout'])+')\n'
        code+=' if result.returncode:\n  if result.returncode==255 and is_connection_message(result.stderr):report_connection_failure('+repr([HOSTS[a]['Info'],target])+',result.stderr)\n  raise RuntimeError("scp 返回 "+str(result.returncode)+": "+result.stderr[-2000:])\n'
        code+=' copied=time.monotonic()\n checked_publish(p,'+repr(dest)+','+repr(sha)+','+repr(replacements.get(str(b)))+')\n'
        code+=' print("SCP "+format(copied-start,".3f")+"s；SHA 与原子发布 "+format(time.monotonic()-copied,".3f")+"s",flush=True)\nfinally:p.unlink(missing_ok=True)\n'
        try:
            details=peer(target,code).strip()
            event(b,'running','镜像接收及 SHA 校验完成，正在应用')
            print('拷贝 '+HOSTS[a]['Info']+' → '+target+' 总用时 '+format(time.monotonic()-begin,'.3f')+'s；'+details,flush=True)
        except Exception as error:event(b,'failed',str(error));raise
        return b
    def apply(rank,host):
        if C['artifact_kind']=='archive':
            code='import tarfile\nfrom pathlib import Path\np=Path('+repr(str(LOCAL/'applied/test.txt'))+')\np.parent.mkdir(parents=True,exist_ok=True)\n'
            code+='with tarfile.open('+repr(dest)+") as a:\n assert a.getnames()==['test.txt']\n data=a.extractfile('test.txt').read()\n assert data==b'test'\np.write_bytes(data)\nprint(str(p))"
        else:
            code='from pathlib import Path\nimport os,json,tempfile,subprocess\n'+DOCKER_HELPER+'\napply_docker('+repr(dest)+','+repr(sha)+','+str(C['timeout'])+')'
        begin=time.monotonic()
        try:
            details=peer(host['Info'],code).strip()
            event(rank,'success','SHA 校验及应用成功: '+details)
            print('应用 '+host['Info']+' 用时 '+format(time.monotonic()-begin,'.3f')+'s',flush=True)
        except Exception as error:event(rank,'failed',str(error));raise
    ready=set(available);busy=set();copies={};applies=[]
    with ThreadPoolExecutor(max_workers=32) as transfer_pool,ThreadPoolExecutor(max_workers=32) as apply_pool:
        for rank in available:
            if rank in need_apply:applies.append(apply_pool.submit(apply,rank,HOSTS[rank]))
        while planned or copies:
            for pair in list(planned):
                a,b=pair
                if a in ready and a not in busy and len(copies)<32:
                    busy.add(a);planned.remove(pair);copies[transfer_pool.submit(copy_pair,pair)]=pair
            if not copies:raise RuntimeError('没有可用的已校验分发源')
            done,_=wait(copies,return_when=FIRST_COMPLETED)
            for future in done:
                a,b=copies.pop(future);future.result();busy.remove(a);ready.add(b)
                applies.append(apply_pool.submit(apply,b,HOSTS[b]))
        for future in as_completed(applies):future.result()


def publish(ip, incoming, destination, expected):
    # A concurrently created destination must also be verified, never overwritten.
    code='from pathlib import Path\nimport os,hashlib\n'+FILE_HELPERS+'\nchecked_publish('+repr(incoming)+','+repr(destination)+','+repr(expected)+')\n'
    peer(ip,code)


def marker(rank):
    return Path(C['shared_dir'])/('enodedone-'+C['run_id']+'-node-'+str(rank)+'.marker')


def cleanup_shared(verified=False):
    if verified:
        for rank in range(len(HOSTS)):
            path=marker(rank)
            if path.is_file() and path.read_text()==C['run_id']+':'+str(rank):path.unlink()
        return
    failures=[]
    def clean(rank,host):
        code='from pathlib import Path\np=Path('+repr(str(marker(rank)))+')\n'
        code+='if p.is_file() and p.read_text()=='+repr(C['run_id']+':'+str(rank))+':p.unlink()\n'
        try:peer(host['Info'],code)
        except Exception as error:failures.append(host['Info']+': '+str(error))
    with ThreadPoolExecutor(max_workers=min(32,len(HOSTS))) as pool:
        list(pool.map(lambda pair:clean(*pair),enumerate(HOSTS)))
    if failures:raise RuntimeError('标记清理失败: '+'; '.join(failures))


def shared():
    verified=False
    try:
        def create(rank,host):
            code='from pathlib import Path\np=Path('+repr(str(marker(rank)))+')\np.parent.mkdir(parents=True,exist_ok=True)\n'
            code+="with p.open('x') as f:f.write("+repr(C['run_id']+':'+str(rank))+')\n'
            peer(host['Info'],code);event(rank,'running','标记已创建，等待 master 检查')
        parallel(create)
        for rank,host in enumerate(HOSTS):
            if not marker(rank).is_file() or marker(rank).read_text()!=C['run_id']+':'+str(rank):
                event(rank,'failed','master 未看到共享标记');raise RuntimeError('共享存储不一致: '+host['Info'])
        verified=True
    finally:cleanup_shared(verified)
    for rank in range(len(HOSTS)):event(rank,'success','master 已确认共享标记并完成清理')


def sample():
    minimum=integer_param('DELAY_MIN_SECONDS',1)
    maximum=integer_param('DELAY_MAX_SECONDS',5)
    if maximum<minimum:raise ValueError('DELAY_MAX_SECONDS 必须不小于 DELAY_MIN_SECONDS')
    delay=random.randint(minimum,maximum);time.sleep(delay)
    row={'rank':C['rank'],'ip':HOSTS[C['rank']]['Info'],'machine':HOSTS[C['rank']]['Machine'],
         'value':os.getloadavg()[0],'epoch':time.time(),'delay_seconds':delay,'metric':'CPU load average (1 min)'}
    LOCAL.mkdir(parents=True,exist_ok=True)
    (LOCAL/'metric.json').write_text(json.dumps(row),encoding='utf-8')
    print(json.dumps(row,ensure_ascii=False),flush=True)


def train():
    LOCAL.mkdir(parents=True,exist_ok=True)
    args=['torchrun','--nnodes',str(len(HOSTS)),'--node_rank',str(C['rank']),
          '--nproc_per_node',os.environ['NPUS_PER_NODE'],'--master_addr',HOSTS[0]['Info'],
          '--master_port',os.environ['MASTER_PORT'],os.environ['ENTRYPOINT'],'--config',os.environ['CONFIG_PATH']]
    with (LOCAL/'train.log').open('w') as stream:
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for line in process.stdout:print(line,end='',flush=True);stream.write(line);stream.flush()
        if process.wait():raise RuntimeError('训练进程失败')


def collect():
    ROOT.mkdir(parents=True,exist_ok=True)
    filename='metric.json' if C['artifact_kind']=='archive' else 'train.log'
    def fetch(rank,host):
        dest=ROOT/'nodes'/str(rank);dest.mkdir(parents=True,exist_ok=True)
        if rank==0:shutil.copyfile(LOCAL/filename,dest/filename)
        else:command(['scp','-O',*ssh_options(),peer_user(host['Info'])+'@'+host['Info']+':'+shlex.quote(str(LOCAL/filename)),str(dest/filename)])
        event(rank,'success','已汇总到 master: '+str(dest/filename))
    parallel(fetch)


def analyze():
    if C['artifact_kind']!='archive':
        logs={host['Info']:(ROOT/'nodes'/str(rank)/'train.log').read_text() for rank,host in enumerate(HOSTS)}
        result=analyze_logs(logs,HOSTS[0]['Info'],int(os.environ.get('WARMUP_STEPS',C.get('warmup_steps',0))))
        (ROOT/'analysis.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        data=ROOT/'training_metrics.json'
        data.write_text(json.dumps(result['rows'],ensure_ascii=False),encoding='utf-8')
        from enodedone_viz import plot
        show_table=os.environ.get('SHOW_TABLE','true').lower()
        if show_table not in ('true','false'):raise ValueError('SHOW_TABLE 必须为 true 或 false')
        for key,title in [('loss','Loss'),('grad_norm','Grad norm'),('seconds','Step time (s)')]:
            if any(row[key] is not None for row in result['rows']):
                plot(data,show_table=show_table=='true',kind='line',x='step',y=key,title=title)
            else:print('日志没有 '+key+'，跳过该图表',flush=True)
        print('训练日志分析完成，已分别发布训练指标页签',flush=True);return
    rows=[json.loads((ROOT/'nodes'/str(rank)/'metric.json').read_text()) for rank in range(len(HOSTS))]
    (ROOT/'analysis.json').write_text(json.dumps({'kind':'node-metrics','visualization_api':2,'metric':'CPU load average (1 min)','rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    with (ROOT/'metrics.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    from enodedone_viz import plot
    show_table=os.environ.get('SHOW_TABLE','true').lower()
    if show_table not in ('true','false'):raise ValueError('SHOW_TABLE 必须为 true 或 false')
    chart_kind=os.environ.get('CHART_KIND','bar').lower()
    plot(ROOT/'metrics.csv', show_table=show_table=='true', kind=None if chart_kind=='none' else chart_kind,
         x='machine', y='value', title='CPU load average (1 min)')
    print('分析完成，已通过 eNodeDone 接口发布图表',flush=True)
