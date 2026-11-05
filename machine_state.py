"""Atomic updates of the task-local machine CSV, preserving credential values."""
import csv
import os
from pathlib import Path
import tempfile
import threading

CSV_BASE_FIELDS=['Machine','Info','User','Pwd']
CSV_FIELDS=CSV_BASE_FIELDS+['Connected']
CSV_LOCK=threading.RLock()


def read_rows(path):
    with CSV_LOCK:
        with Path(path).open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            if reader.fieldnames not in (CSV_BASE_FIELDS,CSV_FIELDS):
                raise ValueError('CSV 字段及顺序必须为 '+','.join(CSV_FIELDS))
            fields=list(reader.fieldnames);rows=list(reader)
    for row in rows:
        if (row.get('Connected') or '').strip().upper() not in ('','Y','N'):
            raise ValueError('Connected 必须为 Y、N 或空白')
    return fields,rows


def update_connected(path, updates):
    """updates maps (IP, User) to bool. An empty mapping only adds a missing column."""
    path=Path(path)
    with CSV_LOCK:
        fields,rows=read_rows(path);changed=fields!=CSV_FIELDS;matched=[]
        for row in rows:
            if 'Connected' not in row:row['Connected']='N'
            identity=((row.get('Info') or '').strip(),(row.get('User') or '').strip() or 'root')
            if identity in updates:
                value='Y' if updates[identity] else 'N'
                if row['Connected']!=value:row['Connected']=value;changed=True
                matched.append(identity)
        if not changed:return matched
        write_rows(path,rows)
        return matched


def write_rows(path,rows):
    descriptor,name=tempfile.mkstemp(prefix='.machines-',suffix='.csv',dir=Path(path).parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=CSV_FIELDS);writer.writeheader();writer.writerows(rows)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def copy_machine_csv(source,destination):
    with CSV_LOCK:
        _,rows=read_rows(source)
        for row in rows:row.setdefault('Connected','N')
        write_rows(destination,rows)
