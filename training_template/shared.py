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
    C['shared_dir']=os.environ['CODE_DIR']
    if not Path(C['shared_dir']).is_dir():raise ValueError('CODE_DIR 不存在')
    if not (Path(C['shared_dir'])/os.environ['ENTRYPOINT']).is_file():raise ValueError('CODE_DIR 缺少训练入口')
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


shared()
