"""Read image SHA256 through the task's existing local-to-node SSH sessions."""
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
import re
import time

from image_files import FILE_HELPERS,SOURCE_HELPERS

PREFIX='@@ENODEDONE_IMAGE_CHECK@@'


def query_script(source,destination,kind):
    code='from pathlib import Path\nimport gzip,hashlib,io,json,os,tarfile\n'+FILE_HELPERS+'\n'+SOURCE_HELPERS+'\n'
    code+='result={}\n'
    if source is not None:
        code+='source=prepare_image_source('+repr(source)+','+repr(kind)+')\nresult["source_sha"]=digest_file(source)\n'
    code+='p=Path('+repr(destination)+')\n'
    code+='if p.is_symlink() or (p.exists() and not p.is_file()):raise RuntimeError("目标不是普通文件")\n'
    if source is not None:
        code+='result["existing_sha"]=result["source_sha"] if p==source else digest_file(p) if p.exists() else None\n'
    else:code+='result["existing_sha"]=digest_file(p) if p.exists() else None\n'
    return code+'print('+repr(PREFIX)+'+json.dumps(result),flush=True)\n'


def check_images(transport,hosts,context,stop,on_cell,log):
    """Finish all checks before returning the immutable input to distribution planning."""
    started=time.monotonic();results={};errors=[]
    def check(rank):
        begin=time.monotonic();ip=hosts[rank]['Info']
        on_cell(rank,'running','通过已有 SSH 会话检查文件与 SHA256')
        try:
            code=query_script(context['image_path'] if rank==0 else None,context['image_dest'],context['artifact_kind'])
            output=transport.run(ip,'python3 -u -',code,stop=stop)
            records=[line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
            if len(records)!=1:raise ValueError('远端 SHA 查询结果缺失或重复')
            result=json.loads(records[0])
            for field in ('source_sha','existing_sha') if rank==0 else ('existing_sha',):
                value=result[field]
                if value is None and field=='existing_sha':continue
                if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):raise ValueError('无效的 SHA256 查询结果')
            log(ip+' 本地 SSH 查询与 SHA 用时 '+format(time.monotonic()-begin,'.3f')+'s')
            return rank,result
        except Exception as error:
            on_cell(rank,'failed',str(error));raise
    with ThreadPoolExecutor(max_workers=min(32,len(hosts))) as pool:
        jobs=[pool.submit(check,rank) for rank in range(len(hosts))]
        for job in as_completed(jobs):
            try:rank,result=job.result();results[rank]=result
            except Exception as error:errors.append(error)
    if errors:raise errors[0]
    if stop and stop.is_set():raise InterruptedError(getattr(stop,'reason',lambda:'用户停止任务')())
    sha=results[0]['source_sha'];available=[];pending=[];replace={}
    for rank in range(len(hosts)):
        existing=results[rank]['existing_sha']
        if existing==sha:
            available.append(rank)
            on_cell(rank,'success' if context['artifact_kind']=='archive' else 'running',
                'SHA256 一致，复用现有文件，跳过分发与应用' if context['artifact_kind']=='archive' else 'SHA256 一致，复用现有文件，等待确认 Docker 装载状态')
        else:
            pending.append(rank)
            if existing is not None:replace[str(rank)]=existing
            on_cell(rank,'running','文件不存在，等待倍增分发' if existing is None else 'SHA256 不一致，等待重新分发与应用')
    log('本地并行 SHA 检查完成，用时 '+format(time.monotonic()-started,'.3f')+'s；复用 '+str(len(available))+' 台，待分发 '+str(len(pending))+' 台')
    return {'version':1,'source_ip':hosts[0]['Info'],'image_path':context['image_path'],'image_dest':context['image_dest'],
            'sha256':sha,'available':available,'pending':pending,'replace':replace}
