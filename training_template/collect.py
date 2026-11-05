def collect():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Only task outputs are archived; checkpoints are intentionally not bulk-copied.
    pack = '''from pathlib import Path
import tarfile
root = Path(LOCAL_PATH)
if not (root/'train.log').is_file():raise ValueError('缺少 train.log')
with tarfile.open(root/'artifacts.tar.gz','w:gz') as archive:
 for name in ('train.log','launch.json','effective_config.yaml','profiling','snapshot'):
  path=root/name
  if not path.exists():continue
  paths=sorted(path.rglob('*')) if path.is_dir() else [path]
  for item in paths:
   if item.is_symlink():raise ValueError('输出目录不允许符号链接: '+str(item))
   if item.is_file():archive.add(item,arcname=str(item.relative_to(root)),recursive=False)
print('已打包 log / profiling / snapshot')
'''.replace('LOCAL_PATH', repr(str(LOCAL)))
    def fetch(rank, host):
        print(peer(host['Info'], pack), flush=True)
        destination = ROOT/'nodes'/str(rank);destination.mkdir(parents=True, exist_ok=True)
        bundle = destination/'artifacts.tar.gz'
        if rank == 0:shutil.copyfile(LOCAL/'artifacts.tar.gz', bundle)
        else:command(['scp','-O',*ssh_options(),peer_user(host['Info'])+'@'+host['Info']+':'+
                     shlex.quote(str(LOCAL/'artifacts.tar.gz')),str(bundle)])
        with tarfile.open(bundle) as archive:
            for member in archive.getmembers():
                # Do not use extractall: reject traversal, absolute paths, links and devices.
                parts = member.name.split('/')
                if not member.isfile() or member.name.startswith('/') or any(p in ('', '.', '..') or '\\' in p for p in parts):
                    raise ValueError('非法输出归档成员: '+member.name)
                if parts[0] not in ('train.log','launch.json','effective_config.yaml','profiling','snapshot'):
                    raise ValueError('未知输出归档成员: '+member.name)
                target = destination.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if any(p.is_symlink() for p in [target,*target.parents]):raise ValueError('输出目标包含符号链接')
                with archive.extractfile(member) as source, target.open('wb') as stream:
                    shutil.copyfileobj(source, stream)
        event(rank,'success','log / profiling / snapshot 已汇总: '+str(destination))
    parallel(fetch)


collect()
