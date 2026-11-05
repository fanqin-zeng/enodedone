"""Real SSH execution, live stdout, sequential stages and per-stage barriers."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid

from core import IMAGE_PATH_KEYS, timestamp
from ssh_sessions import SSHSessionPool
from connection_errors import SSHConnectionError, is_connection_message
from connections import Connections
from image_preflight import check_images


def expand(value, context):
    return re.sub(r'\$\{([A-Z_]+)\}',lambda match:str(context.get(match[1],match[0])),str(value))


def target(ip, user):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*',ip) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',user):
        raise ValueError('非法 SSH 地址或用户名')
    return user+'@'+ip


def connection_settings(task, hosts):
    return {**task['config'],'host_users':{h['Info']:h.get('User') or 'root' for h in hosts}}


class StageStop:
    def __init__(self, user, failure):
        self.user=user;self.failure=failure

    def is_set(self):
        return self.user.is_set() or self.failure.is_set()

    def reason(self):
        return '用户停止任务' if self.user.is_set() else '同节点其他机器失败，终止本机执行'


class SSHTransport:
    def __init__(self, settings, pool=None):
        self.settings=settings;self.pool=pool

    def argv(self, ip, command):
        return ['ssh','-o','BatchMode=yes','-o','ConnectTimeout='+str(int(self.settings.get('ssh_timeout',15))),
                target(ip,self.settings.get('host_users',{}).get(ip,self.settings.get('ssh_user','root'))),command]

    def cancel(self, ip, run_id):
        # Only kill process groups registered by this run, after checking Linux process start time.
        code="""import json,os,signal
from pathlib import Path
for f in Path('/tmp/enodedone/RUN/pids').glob('*.json'):
 try:
  r=json.loads(f.read_text());p=r['pid']
  stat=Path('/proc/'+str(p)+'/stat').read_text().rsplit(')',1)[1].split()
  if stat[19]==r['start'] and os.getpgid(p)==p:os.killpg(p,signal.SIGTERM)
  f.unlink(missing_ok=True)
 except (OSError,ValueError,KeyError):pass
""".replace('RUN',run_id)
        subprocess.run(self.argv(ip,'python3 -'),input=code,text=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)

    def run(self, ip, command, payload='', callback=None, stop=None, run_id=None):
        if self.pool:
            result=self.pool.run(ip,self.settings,command,payload,callback,stop,
                                 lambda:self.cancel(ip,run_id) if run_id else None)
            if result is not None:return result
        output=[];errors=[];reader_errors=[]
        process=subprocess.Popen(self.argv(ip,command),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
        def read(stream, sink, prefix=''):
            try:
                for line in stream:
                    sink.append(line)
                    if callback:callback(prefix+line.rstrip())
            except Exception as error:reader_errors.append(error)
        reader=threading.Thread(target=read,args=(process.stdout,output),daemon=True);reader.start()
        error_reader=threading.Thread(target=read,args=(process.stderr,errors,'[stderr] '),daemon=True);error_reader.start()
        try:
            try:process.stdin.write(payload);process.stdin.close()
            except BrokenPipeError:pass
            deadline=time.monotonic()+int(self.settings.get('command_timeout',1800))
            while process.poll() is None:
                if reader_errors:
                    if run_id:self.cancel(ip,run_id)
                    process.terminate();raise reader_errors[0]
                if (stop and stop.is_set()) or time.monotonic()>deadline:
                    try:
                        if run_id:self.cancel(ip,run_id)
                    finally:process.terminate()
                    raise InterruptedError(getattr(stop,'reason',lambda:'用户停止任务')() if stop and stop.is_set() else '远程命令超时')
                time.sleep(.1)
            reader.join(timeout=5)
            error_reader.join(timeout=5)
            if reader_errors:raise reader_errors[0]
            if process.returncode:
                message=f'{ip} SSH/脚本返回 {process.returncode}: '+''.join(output+errors)[-2000:]
                if process.returncode==255 and is_connection_message(''.join(errors)):
                    raise SSHConnectionError([ip],message)
                raise RuntimeError(message)
            return ''.join(output)
        finally:
            if process.poll() is None:process.kill()
            process.wait();reader.join(timeout=5);error_reader.join(timeout=5)
            process.stdout.close();process.stderr.close()


class SSHRunner:
    def __init__(self, store):
        self.store=store;self.lock=threading.RLock();self.runs={};self.stops={};self.threads={};self.pools={}
        self.connections=Connections(self)

    def transport(self, identifier, task, hosts):
        with self.lock:pool=self.pools.setdefault(identifier,SSHSessionPool())
        return SSHTransport({**connection_settings(task,hosts),'persistent_sessions':True,'require_existing_session':True},pool=pool)

    @staticmethod
    def signature(task, hosts):
        identities=[{key:value for key,value in host.items() if key!='Connected'} for host in hosts]
        settings, nodes = task['config'], task['nodes']
        distributions = [node for node in nodes if node['operation']=='distribute']
        if distributions:
            image_params = {param:distributions[0]['params'].get(param) for param in IMAGE_PATH_KEYS.values()}
            # A shared pair of paths has the same meaning before and after migration.
            # Keep matching historical runs visible without rewriting run.json files.
            if all(value is not None for value in image_params.values()) and all(
                all(node['params'].get(param)==value for param, value in image_params.items()) for node in distributions
            ):
                settings = {**settings, **{key:image_params[param] for key, param in IMAGE_PATH_KEYS.items()}}
                nodes = [{**node, 'params':{key:value for key, value in node['params'].items() if key not in image_params}}
                         if node['operation']=='distribute' else node for node in nodes]
        return hashlib.sha256(json.dumps({'config':settings,'nodes':nodes,'hosts':identities},sort_keys=True).encode()).hexdigest()

    def persist(self, identifier):
        with self.lock:
            run=self.runs[identifier];path=Path(run['artifact_dir'])/'run.json'
            temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)

    def state(self, identifier):
        with self.lock:
            if identifier not in self.runs:
                path=self.store.locations.get(identifier)
                history=sorted((path.parent/'runs').glob('*/run.json'),key=lambda p:p.stat().st_mtime) if path else []
                if history:
                    run=json.loads(history[-1].read_text(encoding='utf-8'))
                    if run.get('mode')!='ssh':return None
                    if run['status']=='running':
                        run.update(status='failed',finished_at=timestamp())
                        run['console'].append('服务曾重启，远端进程状态待人工核实。')
                    self.runs[identifier]=run
                    if run.get('terminal_busy'):
                        run['terminal_busy']=False
                        run.setdefault('terminal',[]).append({'time':timestamp(),'text':'\n上次 Console 命令因本地服务重启中断。\n'})
            run=self.runs.get(identifier)
            if run and run.get('signature')!=self.signature(self.store.load(identifier),self.store.validate(self.store.load(identifier))):return None
            return deepcopy(run)

    def start(self, identifier):
        with self.lock:
            if self.runs.get(identifier,{}).get('status')=='running':raise ValueError('任务已运行')
            if self.runs.get(identifier,{}).get('terminal_busy'):raise ValueError('请等待 Console 命令完成')
            task=self.store.localize_csv(self.store.load(identifier));hosts=self.store.validate(task,ready=True)
            for host in hosts:target(host['Info'],host.get('User') or 'root')
            run_id=uuid.uuid4().hex
            for node in task['nodes']:
                if node['operation']=='distribute':
                    self.context(task,hosts,{'id':run_id},node=node)
            self.connections.entry(identifier)
            folder=self.store.locations[identifier].parent/'runs'/run_id;folder.mkdir(parents=True)
            run={'id':run_id,'mode':'ssh','status':'running','started_at':timestamp(),'finished_at':None,
                 'stages':[],'console':[],'terminal':deepcopy(self.runs.get(identifier,{}).get('terminal',[])),
                 'terminal_busy':False,'summary':None,'visualizations':[],'artifacts':[],'artifact_dir':str(folder),'signature':self.signature(task,hosts)}
            for node in task['nodes']:
                targets=hosts[:1] if node['scope']=='master' else hosts
                run['stages'].append({'id':node['id'],'name':node['name'],'status':'pending','events':[],'rounds':[],
                    'cells':[{'ip':h['Info'],'machine':h['Machine'],'rank':hosts.index(h),'status':'pending','message':'等待上游节点完成','output':[]} for h in targets]})
            self.runs[identifier]=run;self.stops[identifier]=threading.Event();self.persist(identifier)
            thread=threading.Thread(target=self._run,args=(identifier,task,hosts),daemon=True)
            self.threads[identifier]=thread;thread.start();return deepcopy(run)

    def stop(self, identifier):
        if identifier in self.stops:self.stops[identifier].set()

    def delete(self,identifier):
        with self.lock:
            current=self.runs.get(identifier,{})
            thread=self.threads.get(identifier)
            if current.get('status')=='running' or (thread and thread.is_alive()):raise ValueError('请先停止任务再删除')
            if current.get('terminal_busy'):raise ValueError('请等待 Console 命令完成再删除')
            entry=self.connections.entries.get(identifier)
            if entry and entry['busy']:raise ValueError('请等待连接操作完成再删除')
            if entry:self.connections.disconnect(identifier)
            result=self.store.delete(identifier)
            self.connections.entries.pop(identifier,None)
            pool=self.pools.pop(identifier,None)
            if pool:pool.close()
            for items in (self.runs,self.stops,self.threads):items.pop(identifier,None)
            return result

    def bootstrap_nodes(self, identifier, task, hosts, node, stage, connected=None, stop=None,
                        on_event=None, script=None):
        """Execute this task's embedded installer locally, before any remote script push."""
        script=script or self.store.locations[identifier].parent/'scripts'/(node['id']+'.py')
        current=connected if connected is not None else {
            (host['Info'],host['User']):host['Connected']=='Y' for host in self.store.selected_machines(task)}
        signal=stop if stop is not None else self.stops.get(identifier,threading.Event())
        payload={'hosts':hosts,'ssh_user':task['config'].get('ssh_user','root'),
                 'csv_path':str(self.store.csv_file(task).resolve()),'machine_count':len(hosts)}
        if connected is not None:
            payload['key_ready']=[host['Info'] for host in hosts if current.get((host['Info'],host['User']),False)]
        with self.lock:
            for cell in stage['cells']:
                host=hosts[cell['rank']]
                cached=current.get((host['Info'],host.get('User') or 'root'),False)
                cell.update(status='success' if cached else 'running',message='复用已验证的公钥配置' if cached else '等待 SSH 公钥检查')
        if all(cell['status']=='success' for cell in stage['cells']):
            self.log(identifier,'已有公钥配置可复用，跳过公钥检查与上传。',stage);return
        process=subprocess.Popen([sys.executable,'-X','utf8','-u',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
            env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
        errors=[]
        def read():
            for line in process.stdout:
                line=line.rstrip()
                if line.startswith('@@ENODEDONE@@'):
                    try:
                        event=json.loads(line[len('@@ENODEDONE@@'):])
                        if event['status'] not in ('pending','running','success','failed'):raise ValueError('非法状态')
                        cell=next(c for c in stage['cells'] if c['rank']==int(event['rank']))
                        with self.lock:
                            cell.update(status=event['status'],message=str(event['message']))
                            cell['output']=(cell['output']+[str(event['message'])])[-300:]
                        if on_event:on_event(event)
                    except (ValueError,KeyError,StopIteration) as error:errors.append(str(error))
                else:self.log(identifier,line,stage)
        reader=threading.Thread(target=read,daemon=True);reader.start()
        try:
            try:process.stdin.write(json.dumps(payload));process.stdin.close()
            except BrokenPipeError:pass
            deadline=time.monotonic()+int(task['config'].get('command_timeout',1800))
            while process.poll() is None:
                if signal.is_set() or time.monotonic()>deadline:
                    process.terminate();raise InterruptedError('SSH 公钥节点已停止或超时；已追加的公钥保留，重试会复用')
                time.sleep(.1)
            reader.join(timeout=5)
            if process.returncode or errors:raise RuntimeError('SSH 公钥连接检查失败，请查看节点日志')
            if any(c['status']!='success' for c in stage['cells']):raise RuntimeError('部分节点未通过 SSH 公钥验证')
        finally:
            if process.poll() is None:process.kill()
            process.wait();reader.join(timeout=5);process.stdout.close()

    def log(self, identifier, message, stage=None):
        with self.lock:
            run=self.runs.get(identifier);line='['+timestamp()+'] '+str(message)
            if run:
                run['console'].append(line);run['console']=run['console'][-1500:]
            if stage is not None:stage['events'].append(line);stage['events']=stage['events'][-1500:]

    def context(self, task, hosts, run, rank=0, node=None):
        settings=task['config'];values={'MASTER_DIR':settings['master_dir'],'MASTER_IP':settings['master_ip'],
            'RUN_ID':run['id'],'WARMUP_STEPS':settings.get('warmup_steps',0),'TASK_DIR':str(self.store.locations[task['id']].parent)}
        context = {**settings,'warmup_steps':settings.get('warmup_steps',0),'run_id':run['id'],'rank':rank,'hosts':hosts,
            'master_run':settings['master_dir']+'/runs/'+run['id'],
            'timeout':int(settings.get('command_timeout',1800))}
        if node and node['operation']=='distribute':
            for key, param in IMAGE_PATH_KEYS.items():
                path = expand(node['params'].get(param,''),values)
                if not path.startswith('/'):
                    raise ValueError(f"{node['name']} 的 {param} 必须为远程绝对路径")
                context[key] = path
        return context,values

    def execute(self, identifier, task, hosts, node, stage, cell, code, stop=None, distribution=None):
        run=self.runs[identifier];context,values=self.context(task,hosts,run,cell['rank'],node)
        env={**{k:str(v) for k,v in values.items()},**{k:expand(v,values) for k,v in node['params'].items()},'ENODEDONE_CONTEXT':base64.b64encode(json.dumps(context).encode()).decode(),
             'NODE_RANK':str(cell['rank']),'NNODES':str(len(hosts)),'MASTER_ADDR':hosts[0]['Info']}
        pid_path='/tmp/enodedone/'+run['id']+'/pids/'+node['id']+'-'+str(cell['rank'])+'.json'
        body=code if node['language']=='python' else 'import subprocess\nsubprocess.run(["bash","-c",'+repr(code)+'],check=True)'
        sdk_dir='/tmp/enodedone/'+run['id']+'/sdk'
        viz_dir='/tmp/enodedone/'+run['id']+'/visualizations/'+node['id']+'/'+str(cell['rank'])
        sdk={name:Path(__file__).with_name(name).read_text(encoding='utf-8') for name in ('enodedone_viz.py','enodedone_viz.sh','connection_errors.py','image_files.py')}
        # Provision the same public SDK for every Python/Shell node, including non-master nodes.
        setup='import sys\nsdk=Path('+repr(sdk_dir)+')\nsdk.mkdir(parents=True,exist_ok=True)\n'
        setup+='for name,content in '+repr(sdk)+'.items():\n target=sdk/name\n if not target.is_file() or target.read_text(encoding="utf-8")!=content:target.write_text(content,encoding="utf-8")\n'
        setup+='(sdk/"enodedone_viz.sh").chmod(0o700)\nsys.path.insert(0,str(sdk))\n'
        setup+='os.environ["PATH"]=str(sdk)+os.pathsep+os.environ.get("PATH","")\nos.environ["PYTHONPATH"]=str(sdk)+os.pathsep+os.environ.get("PYTHONPATH","")\n'
        setup+='os.environ["ENODEDONE_VIZ_DIR"]='+repr(viz_dir)+'\nos.environ["ENODEDONE_PYTHON"]=sys.executable\n'
        if distribution is not None:
            # Send the snapshot in stdin, outside the command line, and keep each env value bounded.
            setup+='os.environ["ENODEDONE_DISTRIBUTION_PREFLIGHT"]='+repr(json.dumps(distribution))+'\n'
        wrapper='import os,json\nfrom pathlib import Path\nif os.getpgrp()!=os.getpid():os.setsid()\n_enodedone_pid_file=Path('+repr(pid_path)+')\n_enodedone_pid_file.parent.mkdir(parents=True,exist_ok=True)\n'
        wrapper+=setup+"_enodedone_pid_file.write_text(json.dumps({'pid':os.getpid(),'start':Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]}))\ntry:\n exec(compile("+repr(body)+','+repr(node['script_path'])+",'exec'))\nfinally:\n _enodedone_pid_file.unlink(missing_ok=True)\n"
        command='env '+' '.join(k+'='+shlex.quote(v) for k,v in env.items())+' python3 -u -'
        charts=[];chart_errors=[]
        def output(line):
            if line.startswith('@@ENODEDONE_VIZ@@'):
                try:
                    value=json.loads(line[len('@@ENODEDONE_VIZ@@'):])
                    chart_id=value.get('id','')
                    expected=viz_dir+'/'+chart_id+'.json'
                    if not re.fullmatch(r'[a-f0-9]{32}',chart_id) or value.get('manifest_path')!=expected:
                        raise ValueError('非法可视化事件路径')
                    if chart_id not in charts:charts.append(chart_id)
                except (ValueError,TypeError,AttributeError) as error:chart_errors.append(str(error))
            elif line.startswith('@@ENODEDONE_SOURCE@@') and node['operation']=='distribute':
                value=json.loads(line[len('@@ENODEDONE_SOURCE@@'):])
                if re.fullmatch(r'[a-f0-9]{64}',value.get('sha256','')):
                    with self.lock:stage['source_image']=value
            elif line.startswith('@@ENODEDONE_PLAN@@') and node['operation']=='distribute':
                with self.lock:stage['rounds'].append(json.loads(line[len('@@ENODEDONE_PLAN@@'):]))
            elif line.startswith('@@ENODEDONE@@') and node['operation'] in ('distribute','shared','collect'):
                try:
                    event=json.loads(line[len('@@ENODEDONE@@'):]);status=event['status']
                    if status not in ('pending','running','success','failed'):raise ValueError('状态非法')
                    destination=next(c for c in stage['cells'] if c['rank']==int(event['rank']))
                    with self.lock:destination.update(status=status,message=str(event['message']))
                except (ValueError,KeyError,StopIteration):self.log(identifier,line,stage)
            else:
                self.log(identifier,cell['ip']+' | '+line,stage)
                with self.lock:cell['output']=(cell['output']+[line])[-300:]
        transport=self.transport(identifier,task,hosts)
        result=transport.run(cell['ip'],command,wrapper,output,stop or self.stops[identifier],run['id'])
        if chart_errors:raise ValueError('可视化事件错误: '+'; '.join(chart_errors))
        for chart_id in charts:
            self.fetch_visualization(identifier,transport,cell,node,chart_id,viz_dir,stop or self.stops[identifier])
        return result

    def fetch_visualization(self, identifier, transport, cell, node, chart_id, remote_dir, stop):
        """Collect only files declared by the node SDK, independently of operation type."""
        run=self.runs[identifier]
        manifest=json.loads(transport.run(cell['ip'],'cat -- '+shlex.quote(remote_dir+'/'+chart_id+'.json'),stop=stop))
        if manifest.get('id')!=chart_id or manifest.get('version') not in (1,2) or manifest.get('kind') not in (None,'line','box','bar','pie','scatter'):
            raise ValueError('非法图表描述')
        show_table=manifest.get('show_table',False)
        if not isinstance(show_table,bool) or (manifest.get('kind') is None and not show_table):raise ValueError('无效的表格/图形配置')
        assets=[('data_path','.data.json')]
        if manifest.get('kind') is not None:assets.insert(0,('svg_path','.svg'))
        for key,suffix in assets:
            if PurePosixPath(manifest[key])!=PurePosixPath(remote_dir+'/'+chart_id+suffix):
                raise ValueError('图表文件必须位于本节点可视化目录')
        folder=Path(run['artifact_dir'])/'visualizations';folder.mkdir(exist_ok=True)
        files=[]
        for key,suffix in assets:
            content=transport.run(cell['ip'],'cat -- '+shlex.quote(manifest[key]),stop=stop)
            if key=='data_path':
                rows=json.loads(content)
                if not isinstance(rows,list) or not all(isinstance(row,dict) for row in rows):raise ValueError('无效的数据表内容')
                if show_table:manifest['table_rows']=rows
            local=folder/(chart_id+suffix);local.write_text(content,encoding='utf-8');files.append(str(local))
        manifest.update(node_id=node['id'],node_name=node['name'],rank=cell['rank'],ip=cell['ip'])
        local_manifest=folder/(chart_id+'.json');local_manifest.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
        with self.lock:
            run.setdefault('visualizations',[]).append(manifest)
            run['artifacts'].extend(files+[str(local_manifest)])
        self.persist(identifier)

    def _run(self, identifier, task, hosts):
        run=self.runs[identifier];transport=None;stage=None
        try:
            self.log(identifier,'检查持久 SSH 会话，自动连接尚未连接的机器')
            self.connections.ensure(identifier,self.stops[identifier])
            transport=self.transport(identifier,task,hosts)
            for node,stage in zip(task['nodes'],run['stages']):
                stage_begin=None
                if self.stops[identifier].is_set():raise InterruptedError('用户停止任务')
                stage_begin=time.monotonic()
                with self.lock:stage.update(status='running',started_at=timestamp())
                self.log(identifier,'开始 '+node['name'],stage)
                context,values=self.context(task,hosts,run,node=node)
                if node['operation']=='nodes_ssh':
                    self.connections.ensure(identifier,self.stops[identifier])
                    for cell in stage['cells']:cell.update(status='success',message='框架持久 SSH 会话已就绪')
                elif node['operation']=='push':
                    files={expand(n['script_path'],values):n['script_content'] for n in task['nodes']
                           if expand(n['script_ip'],values)==hosts[0]['Info'] and expand(n['script_path'],values).startswith(task['config']['master_dir']+'/pipeline_scripts/')}
                    payload='from pathlib import Path\nfiles='+repr(files)+"\nupdated=0\nfor path,content in files.items():\n p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)\n if not p.is_file() or p.read_text(encoding='utf-8')!=content:p.write_text(content,encoding='utf-8');updated+=1\nprint('脚本更新 '+str(updated)+' 个，复用 '+str(len(files)-updated)+' 个')\n"
                    transport.run(hosts[0]['Info'],'python3 -u -',payload,lambda line:self.log(identifier,line,stage),self.stops[identifier])
                else:
                    distribution=None
                    if node['operation']=='distribute':
                        def image_cell(rank,status,message):
                            with self.lock:stage['cells'][rank].update(status=status,message=message)
                        distribution=check_images(transport,hosts,context,self.stops[identifier],image_cell,
                            lambda line:self.log(identifier,line,stage))
                        with self.lock:
                            stage['source_image']={'ip':hosts[0]['Info'],'path':context['image_path'],'sha256':distribution['sha256']}
                        self.persist(identifier)
                        if not distribution['pending'] and context['artifact_kind']=='archive':
                            with self.lock:stage.update(status='success',finished_at=timestamp(),elapsed_seconds=round(time.monotonic()-stage_begin,3))
                            self.log(identifier,'全部镜像 SHA256 一致，跳过分发与应用',stage);self.persist(identifier)
                            continue
                    source=expand(node['script_ip'],values);path=expand(node['script_path'],values)
                    code=Path(path).read_text(encoding='utf-8') if source in ('127.0.0.1','localhost') else transport.run(source,'cat -- '+shlex.quote(path),stop=self.stops[identifier])
                    orchestrated=node['operation'] in ('distribute','shared','collect')
                    targets=stage['cells'][:1] if orchestrated else stage['cells']
                    failure=threading.Event();signal=StageStop(self.stops[identifier],failure)
                    root_failures=[]
                    def work(cell):
                        with self.lock:cell.update(status='running',message='执行远程脚本')
                        try:
                            if distribution is not None:self.execute(identifier,task,hosts,node,stage,cell,code,signal,distribution=distribution)
                            else:self.execute(identifier,task,hosts,node,stage,cell,code,signal)
                            if not orchestrated:
                                with self.lock:cell.update(status='success',message='远程脚本执行成功；本节点完成后统一同步')
                        except Exception as error:
                            cancelled=isinstance(error,InterruptedError) and signal.is_set()
                            failure.set()
                            with self.lock:
                                cell.update(status='stopped' if cancelled else 'failed',message=str(error))
                                if not cancelled:root_failures.append(error)
                            if not cancelled:self.log(identifier,cell['ip']+' 执行失败：'+str(error),stage)
                            raise
                    if node['operation']=='train':
                        for host in hosts:
                            output=transport.run(host['Info'],'npu-smi info',stop=self.stops[identifier])
                            if len(re.findall(r'No running processes found in NPU',output))<int(node['params'].get('NPUS_PER_NODE',8)):
                                raise RuntimeError('NPU 未全部空闲: '+host['Info'])
                    failures=[]
                    with ThreadPoolExecutor(max_workers=min(32,len(targets))) as pool:
                        jobs=[pool.submit(work,c) for c in targets]
                        for job in jobs:
                            try:job.result()
                            except Exception as error:failures.append(error)
                    if failures:raise root_failures[0] if root_failures else failures[0]
                    if len(stage['cells'])>1:self.log(identifier,'全部机器完成，本节点同步屏障通过',stage)
                    if orchestrated and any(c['status']!='success' for c in stage['cells']):raise RuntimeError('部分机器未报告成功')
                    if node['operation']=='analyze':
                        folder=Path(run['artifact_dir'])
                        # Built-in/legacy result files are optional: a generic analysis node may only call plot().
                        candidates=['analysis.json','metrics.csv','report.svg']
                        probe='from pathlib import Path; import json; p=Path('+repr(context['master_run'])+'); print(json.dumps([n for n in '+repr(candidates)+' if (p/n).is_file()]))'
                        names=json.loads(transport.run(hosts[0]['Info'],'python3 -c '+shlex.quote(probe),stop=self.stops[identifier]))
                        if not isinstance(names,list) or any(name not in candidates for name in names):raise ValueError('非法分析文件清单')
                        for name in names:
                            content=transport.run(hosts[0]['Info'],'cat -- '+shlex.quote(context['master_run']+'/'+name),stop=self.stops[identifier])
                            (folder/name).write_text(content,encoding='utf-8');run['artifacts'].append(str(folder/name))
                        if 'analysis.json' in names:run['summary']=json.loads((folder/'analysis.json').read_text(encoding='utf-8'))
                with self.lock:
                    stage.update(status='success',finished_at=timestamp(),elapsed_seconds=round(time.monotonic()-stage_begin,3))
                    for cell in stage['cells']:cell.update(status='success',message=cell.get('message') or '已完成')
                self.log(identifier,'完成 '+node['name'],stage);self.persist(identifier)
            run['status']='success'
        except Exception as error:
            stopped=self.stops[identifier].is_set();status='stopped' if stopped else 'failed'
            with self.lock:
                run['status']=status
                if stage:
                    stage.update(status=status,finished_at=timestamp(),elapsed_seconds=round(time.monotonic()-stage_begin,3) if stage_begin is not None else 0)
                    for cell in stage['cells']:
                        if cell['status'] in ('pending','running'):cell.update(status=status,message=str(error))
            self.log(identifier,str(error),stage)
            if stage and node['operation']=='shared':
                try:
                    context,_=self.context(task,hosts,run)
                    script=Path(__file__).with_name('remote_runtime.py').read_text(encoding='utf-8')+'\ncleanup_shared()\n'
                    env=base64.b64encode(json.dumps(context).encode()).decode()
                    transport.run(hosts[0]['Info'],'env ENODEDONE_CONTEXT='+shlex.quote(env)+' python3 -u -',script,lambda line:self.log(identifier,line))
                except Exception as cleanup_error:self.log(identifier,'共享标记清理未完成: '+str(cleanup_error))
        finally:
            run['finished_at']=timestamp();self.persist(identifier)

    def append_terminal(self,identifier,text):
        with self.lock:
            terminal=self.runs[identifier].setdefault('terminal',[])
            terminal.append({'time':timestamp(),'text':str(text)[-200000:]})
            size=sum(len(item['text']) for item in terminal)
            while len(terminal)>1 and (size>200000 or len(terminal)>2000):size-=len(terminal.pop(0)['text'])

    def console(self, identifier, command, context):
        task=self.store.load(identifier);hosts=self.store.validate(task,ready=True)
        if not self.state(identifier):raise ValueError('请先启动任务以创建运行记录')
        if context not in ('host','logs'):raise ValueError('未知 Console 类型')
        if command=='clear':
            with self.lock:self.runs[identifier]['terminal' if context=='host' else 'console']=[]
            self.persist(identifier);return []
        if context!='host':raise ValueError('请在 Master Console 输入命令')
        with self.lock:
            if self.runs[identifier].get('terminal_busy'):raise ValueError('上一条 Console 命令仍在执行')
            self.runs[identifier]['terminal_busy']=True
            self.append_terminal(identifier,'$ '+command+'\n');self.persist(identifier)
        remote='cd '+shlex.quote(task['config']['master_dir'])+' && bash -lc '+shlex.quote(command)
        def work():
            try:
                self.connections.ensure(identifier,threading.Event())
                transport=self.transport(identifier,task,hosts)
                transport.settings={**transport.settings,'terminal':True}
                transport.run(hosts[0]['Info'],remote,callback=lambda chunk:self.append_terminal(identifier,chunk))
            except Exception as error:self.append_terminal(identifier,str(error)+'\n')
            finally:
                with self.lock:
                    self.runs[identifier]['terminal_busy']=False
                    self.persist(identifier)
        threading.Thread(target=work,daemon=True).start();return []
