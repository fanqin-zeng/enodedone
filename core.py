"""Local task persistence, log analysis and remote execution helpers."""
from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from templates import builtins, config, OPERATIONS
from machine_state import CSV_FIELDS, read_rows, copy_machine_csv

IMAGE_PATH_KEYS = {'image_path':'IMAGE_PATH', 'image_dest':'IMAGE_DEST'}


def migrate_image_paths(record: dict) -> bool:
    """Move legacy task or template paths to its distribution nodes."""
    settings = record.get('config', {})
    legacy = {param:settings.pop(key) for key, param in IMAGE_PATH_KEYS.items() if key in settings}
    if not legacy:
        return False
    for node in record['nodes']:
        if node['operation'] == 'distribute':
            for param, value in legacy.items():
                node['params'].setdefault(param, value if isinstance(value, str) else '')
    return True


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def machine_rows(path: Path) -> list[dict]:
    """Read CSV rows in file order without exposing passwords."""
    _,rows=read_rows(path)
    machines = [{key: (row.get(key) or '').strip() for key in CSV_FIELDS if key != 'Pwd'} for row in rows]
    for machine in machines:
        machine['User'] = machine['User'] or 'root'
        machine['Connected']='Y' if machine['Connected'].upper()=='Y' else 'N'
    return machines


def validate_machines(machines: list[dict], require_addresses=False) -> list[dict]:
    for machine in machines:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',machine['User']):
            raise ValueError('CSV 的 User 不是有效的 SSH 用户名')
    if not machines:
        raise ValueError('机器清单不能为空')
    for key in ('Machine', 'Info'):
        values = [row[key] for row in machines]
        present = [value for value in values if value]
        if ((key == 'Machine' or require_addresses) and any(not value for value in values)) or len(set(present)) != len(present):
            raise ValueError(f'CSV 的 {key} 必须非空且唯一')
    return machines


def read_machines(path: Path, require_addresses=False) -> list[dict]:
    return validate_machines(machine_rows(path), require_addresses)


def positive_count(value) -> int:
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('机器数必须为正整数')
    return int(value)


def default_name(template=None) -> str:
    prefix='任务' if template is None or template['id']=='blank' else template['name']+'任务'
    return prefix + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def write_machine_csv(path: Path, count: int) -> None:
    count = positive_count(count)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for index in range(count):
            writer.writerow({'Machine': f'node{index+1:03}', 'Info': '', 'User': 'root','Connected':'N'})


def image_action(existing_sha: str | None, expected_sha: str) -> str:
    """Missing files are copied, matching files reused, mismatches fail closed."""
    if existing_sha is None:
        return 'copy'
    if existing_sha.lower() != expected_sha.lower():
        raise ValueError('镜像文件存在但 SHA256 不一致，禁止覆盖')
    return 'reuse'


def distribution_plan(hosts: list[str], available: list[str] | None = None) -> list[list[dict]]:
    """Only verified replicas from previous rounds can send in the next round."""
    if not hosts or len(set(hosts)) != len(hosts):
        raise ValueError('分发主机列表必须非空且唯一')
    sources = list(dict.fromkeys(available or [hosts[0]]))
    if not sources or any(host not in hosts for host in sources):
        raise ValueError('分发源必须属于任务机器')
    pending = [host for host in hosts if host not in sources]
    rounds = []
    while pending:
        targets, pending = pending[:len(sources)], pending[len(sources):]
        rounds.append([{'source': source, 'target': target} for source, target in zip(sources, targets)])
        sources.extend(targets)
    return rounds

class TaskStore:
    """Task JSON and an adjacent machine CSV, with explicit file open/save support."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.defaults = config()
        self.distribution_defaults = {}
        local_defaults = self.root / 'defaults.json'
        if local_defaults.is_file():
            overrides = json.loads(local_defaults.read_text(encoding='utf-8-sig'))
            allowed = {'master_dir', 'shared_dir', *IMAGE_PATH_KEYS.values()}
            if not isinstance(overrides, dict) or set(overrides) - allowed:
                raise ValueError('defaults.json 只支持 master_dir、shared_dir、IMAGE_PATH、IMAGE_DEST')
            for key, value in overrides.items():
                if not isinstance(value, str) or not value.startswith('/'):
                    raise ValueError(f'defaults.json 的 {key} 必须为远程绝对路径')
                if key in IMAGE_PATH_KEYS.values():
                    self.distribution_defaults[key] = value
                else:
                    self.defaults[key] = value
        self.locations = {}
        for path in self.root.glob('*/task.json'):
            self.locations[path.parent.name] = path

    def templates(self) -> list[dict]:
        result = builtins()
        for template in result:
            for node in template['nodes']:
                if node['operation'] == 'distribute':
                    node['params'].update(self.distribution_defaults)
        folder = self.root / '_templates'
        if folder.exists():
            result.extend(json.loads(path.read_text(encoding='utf-8')) for path in folder.glob('*.json'))
        for template in result:
            migrate_image_paths(template)
            template.get('config', {}).pop('expected_sha', None)
        return result

    def create(self, template_id='training', name='', count=8, paths=None) -> dict:
        template = next((item for item in self.templates() if item['id'] == template_id), None)
        if not template:
            raise ValueError('模板不存在')
        count = positive_count(count)
        if paths is None:paths={}
        if not isinstance(paths,dict) or set(paths)-{'master_dir'}:
            raise ValueError('创建任务的路径配置仅支持 master_dir')
        paths={key:value.strip() if isinstance(value,str) else value for key,value in paths.items()}
        for key,value in paths.items():
            if not isinstance(value,str) or not value.startswith('/'):
                raise ValueError(key+' 必须为远程绝对路径')
        identifier = uuid.uuid4().hex
        folder = self.root / identifier
        folder.mkdir()
        write_machine_csv(folder / 'machines.csv', count)
        task = {'version': 1, 'id': identifier, 'name': name.strip() or default_name(template), 'template': template['name'],
                'csv_path': 'machines.csv', 'machine_count': count, 'config': {**self.defaults, **template.get('config', {}),**paths},
                'nodes': deepcopy(template['nodes']), 'updated_at': timestamp()}
        task['config']['master_ip'] = ''
        self.locations[identifier] = folder / 'task.json'
        self.save(task)
        return self.view(identifier)

    def load(self, identifier: str) -> dict:
        if identifier not in self.locations:
            raise ValueError('任务不存在')
        task = json.loads(self.locations[identifier].read_text(encoding='utf-8-sig'))
        migrate_image_paths(task)
        task['config'].pop('expected_sha', None)
        return task

    def csv_file(self, task: dict) -> Path:
        path = Path(task['csv_path'])
        return path if path.is_absolute() else self.locations[task['id']].parent / path

    def selected_machines(self, task: dict, ready=False, rows=None) -> list[dict]:
        rows = machine_rows(self.csv_file(task)) if rows is None else rows
        if 'machine_count' in task:
            rows = rows[:positive_count(task['machine_count'])]
        return validate_machines(rows, require_addresses=ready)

    def localize_csv(self,task):
        target=self.locations[task['id']].parent/'machines.csv'
        source=self.csv_file(task)
        if source.resolve()!=target.resolve():
            copy_machine_csv(source,target)
            task=deepcopy(task);task['csv_path']='machines.csv';self.save(task)
        return task

    def validate(self, task: dict, ready=False) -> list[dict]:
        if task.get('version') != 1 or not task.get('name', '').strip():
            raise ValueError('任务格式错误或名称为空')
        hosts = self.selected_machines(task, ready)
        settings = task['config']
        if ready and settings['master_ip'] not in [host['Info'] for host in hosts]:
            raise ValueError('请在选中的机器范围内指定 Master IP')
        if not str(settings.get('master_dir', '')).startswith('/'):
            raise ValueError('master_dir 必须为远程绝对路径')
        if not 1 <= len(task['nodes']) <= 64:
            raise ValueError('pipeline 需要 1–64 个节点')
        ids = []
        for node in task['nodes']:
            if node['operation'] not in OPERATIONS or node['scope'] not in ('master', 'all'):
                raise ValueError('未知节点类型或执行范围')
            if not node['name'].strip() or not node['script_ip'].strip() or not node['script_path'].strip():
                raise ValueError('节点名称、脚本 IP 和路径不能为空')
            if node['language'] not in ('sh', 'python'):
                raise ValueError('只支持 sh / python 脚本')
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', node['id']):
                raise ValueError('非法节点 ID')
            if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str) for key, value in node['params'].items()):
                raise ValueError('参数必须是字符串键值对，键使用变量名格式')
            ids.append(node['id'])
        if len(set(ids)) != len(ids):
            raise ValueError('节点 ID 重复')
        master = settings['master_ip']
        return sorted(hosts, key=lambda host: host['Info'] != master)

    def save(self, task: dict) -> None:
        task = deepcopy(task)
        migrate_image_paths(task)
        task['config'].pop('expected_sha', None)
        hosts = self.validate(task)
        if task['config']['master_ip'] not in [host['Info'] for host in hosts]:
            task['config']['master_ip'] = ''
        if 'machine_count' in task:
            task['machine_count'] = positive_count(task['machine_count'])
        for key in ('machines', 'csv_machines', 'file_path', 'csv_file', 'runtime'):
            task.pop(key, None)
        task['updated_at'] = timestamp()
        path = self.locations[task['id']]
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        scripts = path.parent / 'scripts'
        scripts.mkdir(exist_ok=True)
        for node in task['nodes']:
            extension = '.py' if node['language'] == 'python' else '.sh'
            (scripts / (node['id'] + extension)).write_text(node.get('script_content', ''), encoding='utf-8')

    def view(self, identifier: str) -> dict:
        task = self.load(identifier)
        task['csv_machines'] = machine_rows(self.csv_file(task))
        task['machines'] = self.selected_machines(task, rows=task['csv_machines'])
        task.setdefault('machine_count', len(task['machines']))
        if task['config']['master_ip'] not in [host['Info'] for host in task['machines']]:
            task['config']['master_ip'] = ''
        task['file_path'] = str(self.locations[identifier])
        task['csv_file'] = str(self.csv_file(task).resolve())
        return task

    def listing(self) -> list[dict]:
        items = [{'id': item['id'], 'name': item['name'], 'template': item['template'], 'updated_at': item['updated_at']}
                 for item in (self.load(identifier) for identifier in self.locations)]
        return sorted(items, key=lambda item: item['updated_at'], reverse=True)

    def delete(self,identifier):
        if identifier not in self.locations:raise ValueError('任务不存在')
        path=self.locations[identifier];folder=self.root/identifier
        if path==folder/'task.json':
            # Move only an owned, direct child; never follow a junction into another directory.
            if folder.resolve().parent!=self.root or path.resolve().parent!=folder.resolve():
                raise ValueError('任务目录超出本地工作空间，拒绝移动')
            trash=self.root/'.trash'
            if trash.resolve()!=self.root/'.trash':raise ValueError('回收目录不能指向工作空间之外')
            trash.mkdir(exist_ok=True)
            destination=trash/(identifier+'-'+uuid.uuid4().hex)
            if destination.resolve().parent!=trash.resolve():raise ValueError('无效的回收路径')
            folder.rename(destination)
            result={'recovery_path':str(destination/'task.json'),'source_preserved':False}
        else:
            # Imported files can live alongside unrelated user files. Remove only their library entry.
            result={'recovery_path':str(path),'source_preserved':True}
        del self.locations[identifier]
        return result

    def copy(self, identifier: str) -> dict:
        original = self.load(identifier)
        task = deepcopy(original)
        task['id'] = uuid.uuid4().hex
        task['name'] += ' · 副本'
        folder = self.root / task['id']
        folder.mkdir()
        copy_machine_csv(self.csv_file(original), folder / 'machines.csv')
        task['csv_path'] = 'machines.csv'
        self.locations[task['id']] = folder / 'task.json'
        self.save(task)
        return self.view(task['id'])

    def open_file(self, path: Path) -> dict:
        path = path.resolve()
        task = json.loads(path.read_text(encoding='utf-8-sig'))
        migrate_image_paths(task)
        if not re.fullmatch('[a-f0-9]{32}', task.get('id', '')):
            raise ValueError('不是有效的 eNodeDone 任务文件')
        existing = self.locations.get(task['id'])
        if existing and existing != path:
            raise ValueError('相同 ID 的任务已打开；请使用“复制任务”创建独立任务')
        self.locations[task['id']] = path
        try:
            self.validate(task)
        except (ValueError, KeyError, OSError):
            if existing:
                self.locations[task['id']] = existing
            else:
                del self.locations[task['id']]
            raise
        return self.view(task['id'])

    def save_template(self, identifier: str, name: str) -> dict:
        task = self.load(identifier)
        template = {'id': uuid.uuid4().hex, 'name': name or task['name'], 'icon': '',
                    'description': '自定义任务模板', 'nodes': task['nodes'], 'config': task['config']}
        folder = self.root / '_templates'
        folder.mkdir(exist_ok=True)
        (folder / (template['id'] + '.json')).write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding='utf-8')
        return template
