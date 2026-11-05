"""Stdlib-only remote helpers shared by local SSH checks and image receivers."""
FILE_HELPERS = r'''
def digest_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4194304),b''):digest.update(block)
    return digest.hexdigest()

def checked_publish(incoming,destination,expected,previous=None):
    p,q=Path(incoming),Path(destination)
    if p.is_symlink() or not p.is_file():raise RuntimeError('Incoming file is not regular')
    try:
        if digest_file(p)!=expected:raise RuntimeError('接收端 SHA 校验失败')
        if previous is not None and q.exists():
            if q.is_symlink() or not q.is_file():raise RuntimeError('目标不是普通文件，禁止替换')
            current=digest_file(q)
            if current==expected:return
            if current!=previous:raise RuntimeError('目标文件发生变化，禁止覆盖')
            os.replace(p,q);return
        try:os.link(p,q)
        except FileExistsError:
            if q.is_symlink() or not q.is_file() or digest_file(q)!=expected:
                raise RuntimeError('目标文件发生变化，禁止覆盖')
    finally:p.unlink(missing_ok=True)
'''

SOURCE_HELPERS = r'''
def prepare_image_source(path,kind):
    source=Path(path)
    if kind=='archive':
        source.parent.mkdir(parents=True,exist_ok=True)
        if not source.exists():
            payload=io.BytesIO()
            with gzip.GzipFile(fileobj=payload,mode='wb',mtime=0) as zipped:
                with tarfile.open(fileobj=zipped,mode='w') as archive:
                    info=tarfile.TarInfo('test.txt');info.size=4;info.mtime=0
                    archive.addfile(info,io.BytesIO(b'test'))
            try:
                with source.open('xb') as stream:stream.write(payload.getvalue())
            except FileExistsError:pass
        with tarfile.open(source) as archive:
            if archive.getnames()!=['test.txt'] or archive.extractfile('test.txt').read()!=b'test':
                raise RuntimeError('现有测试包内容不符，禁止覆盖')
    if not source.is_file():raise RuntimeError('master 源镜像不存在: '+str(source))
    return source
'''
