"""Task-owned SSH sessions with passive transport-disconnect notifications."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import time
from queue import Queue,Empty

from core import timestamp
from machine_state import update_connected
from ssh_sessions import SSHSessionPool


class Connections:
    def __init__(self,runner):
        self.runner=runner;self.entries={};self.closed=threading.Event()
        self.monitor=None;self.disconnections=Queue();self.pending_events=[]

    def select_hosts(self, entry, task, hosts, identity):
        """Change the active range while keeping existing sessions and CSV markers."""
        previous = dict(zip(entry['identity'][1], entry['cells']))
        retained = entry['retained']
        for key, cell in previous.items():
            if cell['status']=='success':
                retained.add(key)
        cells = []
        for rank, (host, key) in enumerate(zip(hosts, identity[1])):
            cell = previous.get(key)
            if cell is None:
                connected = key in retained
                cell = {'status':'success' if connected else 'pending',
                        'message':'SSH 会话已建立，任务间持续复用' if connected else '尚未建立 SSH 会话',
                        'output':[]}
            retained.discard(key)
            cells.append(dict(cell, rank=rank, ip=host['Info'], machine=host['Machine']))
            entry['trust'].setdefault(key, host['Connected']=='Y')
        entry.update(identity=identity, task=task, hosts=hosts, cells=cells)
        return entry

    def clear_retained(self, entry):
        if entry['retained']:
            update_connected(self.runner.store.csv_file(entry['task']),dict.fromkeys(entry['retained'],False))
            entry['retained'].clear()

    def entry(self,identifier):
        runner=self.runner
        with runner.lock:
            task=runner.store.localize_csv(runner.store.load(identifier));hosts=runner.store.validate(task)
            identity=(str(runner.store.csv_file(task).resolve()),tuple((h['Info'],h['User']) for h in hosts))
            old=self.entries.get(identifier)
            if old and old['identity']==identity:
                old['task']=task;old['hosts']=hosts
                for cell,host in zip(old['cells'],hosts):cell['machine']=host['Machine']
                return old
            if old:
                if old['busy'] or runner.runs.get(identifier,{}).get('status')=='running':
                    raise ValueError('请先停止当前操作，再修改机器清单或 Master')
                previous, selected = set(old['identity'][1]), set(identity[1])
                if old['identity'][0]==identity[0] and (previous <= selected or selected <= previous):
                    return self.select_hosts(old,task,hosts,identity)
                old['pool'].close()
                self.clear_retained(old)
            pool=SSHSessionPool(on_disconnect=lambda ip,user,transport:
                self.disconnections.put((identifier,pool,ip,user,transport)))
            runner.pools[identifier]=pool
            entry={'identity':identity,'task':task,'hosts':hosts,'pool':pool,'busy':False,
                   'trust':{(h['Info'],h['User']):h.get('Connected')=='Y' for h in hosts},
                   'events':[],'sequence':0,'thread':None,'retained':set(),
                   'cells':[{'rank':i,'ip':h['Info'],'machine':h['Machine'],'status':'pending',
                             'message':'尚未建立 SSH 会话','output':[]} for i,h in enumerate(hosts)]}
            # CSV markers describe sessions owned by this service, never sessions from an older process.
            update_connected(runner.store.csv_file(task),{(h['Info'],h['User']):False for h in hosts})
            self.entries[identifier]=entry
            return entry

    def settings(self,entry):
        return {**entry['task']['config'],'persistent_sessions':True,
                'host_users':{h['Info']:h['User'] for h in entry['hosts']}}

    def state(self,identifier):
        entry=self.entry(identifier)
        with self.runner.lock:
            count=sum(c['status']=='success' for c in entry['cells'])
            return {'busy':entry['busy'],'cells':deepcopy(entry['cells']),'events':deepcopy(entry['events']),
                    'connected':count,'total':len(entry['cells']),
                    'status':'running' if entry['busy'] else 'success' if count==len(entry['cells']) else
                             'failed' if any(c['status']=='failed' for c in entry['cells']) else 'pending'}

    def set_cell(self,identifier,entry,rank,status,message):
        with self.runner.lock:
            if self.entries.get(identifier) is not entry:return
            host=entry['hosts'][rank]
            update_connected(self.runner.store.csv_file(entry['task']),{(host['Info'],host['User']):status=='success'})
            entry['cells'][rank].update(status=status,message=str(message))
            if status=='success':entry['trust'][(host['Info'],host['User'])]=True

    def fail(self,identifier,ips,message):
        with self.runner.lock:
            entry=self.entries.get(identifier)
            if not entry:return
            for cell in entry['cells']:
                if cell['ip'] not in ips:continue
                was_connected=cell['status']=='success'
                self.set_cell(identifier,entry,cell['rank'],'failed',message)
                entry['pool'].disconnect(cell['ip'],self.settings(entry))
                if was_connected:
                    entry['sequence']+=1
                    entry['events'].append({'id':entry['sequence'],'time':timestamp(),
                        'message':cell['machine']+' ('+cell['ip']+') SSH 连接断开：'+str(message)})
                    entry['events']=entry['events'][-100:]

    def connect(self,identifier):
        entry=self.entry(identifier)
        with self.runner.lock:
            from runner import target
            for host in entry['hosts']:target(host['Info'],host['User'])
            if entry['busy']:return self.state(identifier)
            if all(c['status']=='success' for c in entry['cells']):return self.state(identifier)
            if self.closed.is_set():raise ValueError('SSH 连接管理已关闭')
            entry['busy']=True
            thread=threading.Thread(target=self._connect,args=(identifier,entry),daemon=True)
            entry['thread']=thread;thread.start()
            if self.monitor is None:
                self.monitor=threading.Thread(target=self._monitor,daemon=True);self.monitor.start()
            return self.state(identifier)

    def prepare(self,identifier,entry):
        from templates import stage
        node=stage('nodes_ssh')
        current={'events':[],'cells':[dict(c,output=[]) for c in entry['cells']]}
        def progress(event):
            with self.runner.lock:
                rank=int(event['rank'])
                if entry['cells'][rank]['status']!='success':
                    entry['cells'][rank].update(status='running',message=str(event['message']))
        self.runner.bootstrap_nodes(identifier,entry['task'],entry['hosts'],node,current,
            connected=entry['trust'],stop=self.closed,on_event=progress,
            script=Path(__file__).with_name('nodes_ssh.py'))
        for cell in current['cells']:
            if cell['status']=='success':
                host=entry['hosts'][cell['rank']];entry['trust'][(host['Info'],host['User'])]=True

    def _connect(self,identifier,entry):
        try:
            pending=[c for c in entry['cells'] if c['status']!='success']
            for cell in pending:self.set_cell(identifier,entry,cell['rank'],'running','正在建立持久 SSH 会话')
            if not all(entry['trust'][(host['Info'],host['User'])] for host in entry['hosts']):
                self.prepare(identifier,entry)
            def open_session(cell):
                try:
                    if self.closed.is_set():raise InterruptedError('连接管理已关闭')
                    client,_=entry['pool'].client(cell['ip'],self.settings(entry))
                    if client is None:raise RuntimeError('无法创建持久 SSH 会话')
                    if self.closed.is_set():
                        entry['pool'].disconnect(cell['ip'],self.settings(entry));return
                    with self.runner.lock:
                        entry['pool'].check_active(cell['ip'],self.settings(entry))
                        self.set_cell(identifier,entry,cell['rank'],'success','SSH 会话已建立，任务间持续复用')
                except Exception as error:
                    entry['pool'].disconnect(cell['ip'],self.settings(entry))
                    self.set_cell(identifier,entry,cell['rank'],'failed',str(error))
            with ThreadPoolExecutor(max_workers=min(32,len(pending))) as workers:
                list(workers.map(open_session,pending))
        except Exception as error:
            for cell in entry['cells']:
                if cell['status']!='success':self.set_cell(identifier,entry,cell['rank'],'failed',str(error))
        finally:
            with self.runner.lock:entry['busy']=False

    def ensure(self,identifier,stop):
        self.connect(identifier);entry=self.entry(identifier)
        while entry['busy']:
            if stop.is_set():raise InterruptedError('用户停止任务')
            time.sleep(.05)
        failed=[c for c in entry['cells'] if c['status']!='success']
        if failed:raise RuntimeError('SSH 连接未就绪：'+'；'.join(c['machine']+': '+c['message'] for c in failed))

    def disconnect(self,identifier):
        entry=self.entry(identifier)
        with self.runner.lock:
            if entry['busy']:raise ValueError('正在建立连接，请等待完成')
            if self.runner.runs.get(identifier,{}).get('status')=='running':raise ValueError('请先停止任务，再关闭连接')
            entry['pool'].close()
            self.clear_retained(entry)
            for cell in entry['cells']:self.set_cell(identifier,entry,cell['rank'],'pending','SSH 会话已主动关闭')
            return self.state(identifier)

    def process_disconnect(self,event):
        identifier,pool,ip,user,transport=event
        with self.runner.lock:
            entry=self.entries.get(identifier)
            if not entry or entry['pool'] is not pool:return
            with pool.lock:client=pool.clients.get((ip,user))
            # Intentional closes remove clients first. A queued event from an older session
            # must never clear its replacement, even for the same IP and user.
            if not client or client.get_transport() is not transport:return
            if transport.is_active():return
            error=transport.get_exception()
            message=(str(error) or type(error).__name__) if error else '远端关闭 SSH 连接（EOF / SSH_DISCONNECT）'
            if (ip,user) in entry['retained']:
                update_connected(self.runner.store.csv_file(entry['task']),{(ip,user):False})
                pool.disconnect(ip,{'ssh_user':user})
                entry['retained'].remove((ip,user))
                return
            self.fail(identifier,[ip],message)

    def _monitor(self):
        while not self.closed.is_set():
            try:
                event=self.pending_events.pop(0) if self.pending_events else self.disconnections.get(timeout=.5)
            except Empty:continue
            try:self.process_disconnect(event)
            except OSError as error:
                # A CSV held open by Excel must not permanently stop liveness monitoring.
                self.pending_events.append(event)
                with self.runner.lock:
                    for entry in self.entries.values():
                        entry['sequence']+=1
                        entry['events'].append({'id':entry['sequence'],'time':timestamp(),
                            'message':'SSH 状态写入失败，将重试：'+str(error)})
                        entry['events']=entry['events'][-100:]
                self.closed.wait(5)

    def close(self):
        self.closed.set()
        for entry in list(self.entries.values()):
            thread=entry.get('thread')
            if thread and thread.is_alive():thread.join(timeout=25)
            entry['pool'].close()
            self.clear_retained(entry)
            for cell in entry['cells']:
                self.set_cell(entry['task']['id'],entry,cell['rank'],'pending','本地服务已关闭')
