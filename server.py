"""Loopback-only local UI server. Execution is started only by an explicit user request."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from core import TaskStore
from runner import SSHRunner
from templates import OPERATIONS

ROOT = Path(__file__).resolve().parent
DIALOG_LOCK = threading.Lock()


def choose_file(kind: str) -> str:
    """Show the Windows native file picker only after an explicit UI action."""
    import tkinter
    from tkinter import filedialog
    with DIALOG_LOCK:
        window = tkinter.Tk()
        window.withdraw()
        window.attributes('-topmost', True)
        try:
            return filedialog.askopenfilename(title='导入任务' if kind == 'task' else '选择机器 CSV',
                filetypes=[('任务 JSON', '*.json')] if kind == 'task' else [('机器 CSV', '*.csv')])
        finally:
            window.destroy()


def make_handler(store: TaskStore, runner: SSHRunner):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid logging task content, CSV fields or console input.
            return

        def send_json(self, value, status=200):
            content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            try:
                path = unquote(urlparse(self.path).path)
                if path == '/api/bootstrap':
                    return self.send_json({'tasks': store.listing(), 'templates': store.templates(),
                                           'operations': OPERATIONS, 'mode': 'ssh', 'visualization_api': 3, 'execution_api': 11,
                                           'creation_defaults':{'master_dir':store.defaults['master_dir']}})
                if path.startswith('/api/tasks/'):
                    parts = path.strip('/').split('/')
                    identifier = parts[2]
                    if len(parts)==4 and parts[3]=='connections':
                        return self.send_json(runner.connections.state(identifier))
                    if len(parts) in (5,6) and parts[3] == 'visualizations':
                        run = runner.state(identifier)
                        chart_id = parts[4]
                        if not run or not any(item['id'] == chart_id for item in run.get('visualizations', [])):
                            raise ValueError('图表不存在')
                        if len(parts) == 6:
                            if parts[5] != 'data':raise ValueError('未知图表请求')
                            data = (Path(run['artifact_dir']) / 'visualizations' / (chart_id + '.data.json')).resolve()
                            if not data.is_relative_to(Path(run['artifact_dir']).resolve() / 'visualizations') or not data.is_file():
                                raise ValueError('图表数据不存在')
                            return self.send_json(json.loads(data.read_text(encoding='utf-8')))
                        chart = (Path(run['artifact_dir']) / 'visualizations' / (chart_id + '.svg')).resolve()
                        if not chart.is_relative_to(Path(run['artifact_dir']).resolve() / 'visualizations') or not chart.is_file():
                            raise ValueError('图表文件不存在')
                        content = chart.read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/svg+xml; charset=utf-8')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                        self.send_header('Content-Length', str(len(content)))
                        self.end_headers()
                        return self.wfile.write(content)
                    if len(parts) == 4 and parts[3] == 'report':
                        run = runner.state(identifier)
                        if not run:
                            raise ValueError('尚无分析报告')
                        report = (Path(run['artifact_dir']) / 'report.svg').resolve()
                        if not report.is_relative_to(store.locations[identifier].parent / 'runs') or not report.is_file():
                            raise ValueError('报告不存在')
                        content = report.read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/svg+xml')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                        self.send_header('Content-Length', str(len(content)))
                        self.end_headers()
                        return self.wfile.write(content)
                    if len(parts) == 4 and parts[3] == 'run':
                        return self.send_json(runner.state(identifier))
                    if len(parts) == 3:
                        return self.send_json(store.view(identifier))
                    raise ValueError('未知请求')
                filename = 'index.html' if path == '/' else path.lstrip('/')
                file = (ROOT / 'web' / filename).resolve()
                if not file.is_relative_to(ROOT / 'web') or not file.is_file():
                    return self.send_json({'error': '文件不存在'}, 404)
                self.send_response(200)
                mime = 'application/javascript' if file.suffix in ('.js','.mjs') else mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith(('text/', 'application/javascript')) else ''))
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(file.read_bytes())
            except (ValueError, KeyError, OSError) as error:
                self.send_json({'error': str(error)}, 400)

        def do_POST(self):
            try:
                origin = self.headers.get('Origin')
                allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
                if origin and origin not in allowed:
                    return self.send_json({'error': '仅接受本地同源请求'}, 403)
                if self.headers.get('Host') not in {urlparse(item).netloc for item in allowed}:
                    return self.send_json({'error': '非法 Host'}, 403)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send_json({'error': '需要 application/json'}, 415)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2_000_000:
                    raise ValueError('请求内容过大或为空')
                data = json.loads(self.rfile.read(length))
                path = urlparse(self.path).path
                if path == '/api/tasks':
                    return self.send_json(store.create(data.get('template', 'training'), data.get('name', ''), data.get('count', 8),paths=data.get('paths')))
                if path == '/api/open':
                    chosen = data.get('path') or choose_file('task')
                    return self.send_json(store.open_file(Path(chosen)) if chosen else None)
                parts = path.strip('/').split('/')
                if len(parts) != 4 or parts[:2] != ['api', 'tasks']:
                    raise ValueError('未知请求')
                identifier, action = parts[2:]
                current = runner.state(identifier)
                if current and current['status'] == 'running' and action not in ('stop', 'console'):
                    raise ValueError('运行中请先停止，再修改、复制或切换文件')
                if action=='connect':return self.send_json(runner.connections.connect(identifier))
                if action=='disconnect':return self.send_json(runner.connections.disconnect(identifier))
                if action=='delete':return self.send_json(runner.delete(identifier))
                if action == 'save':
                    if data['id'] != identifier:
                        raise ValueError('任务 ID 不一致')
                    with runner.lock:
                        active=runner.connections.entries.get(identifier)
                        if active and active['busy']:raise ValueError('请等待连接操作完成后再修改配置')
                        if runner.runs.get(identifier,{}).get('status')=='running':
                            raise ValueError('运行中请先停止，再修改配置')
                        store.save(data)
                    return self.send_json(store.view(identifier))
                if action == 'copy':
                    return self.send_json(store.copy(identifier))
                if action == 'run':
                    return self.send_json(runner.start(identifier))
                if action == 'stop':
                    runner.stop(identifier)
                    return self.send_json({'ok': True})
                if action == 'console':
                    return self.send_json(runner.console(identifier, data['command'], data.get('context', 'container')))
                if action == 'template':
                    return self.send_json(store.save_template(identifier, data.get('name', '')))
                task = store.load(identifier)
                if action == 'csv-open':
                    os.startfile(str(store.csv_file(task).resolve()))
                    return self.send_json({'ok': True})
                if action == 'folder-open':
                    os.startfile(str(store.locations[identifier].parent))
                    return self.send_json({'ok': True})
                if action == 'csv-select':
                    chosen = data.get('path') or choose_file('csv')
                    if not chosen:
                        return self.send_json(None)
                    task['csv_path'] = str(Path(chosen).resolve())
                    with runner.lock:
                        active=runner.connections.entries.get(identifier)
                        if active and active['busy']:raise ValueError('请等待连接操作完成后再修改配置')
                        if runner.runs.get(identifier,{}).get('status')=='running':
                            raise ValueError('运行中请先停止，再修改配置')
                        store.validate(task)
                        store.save(store.localize_csv(task))
                    return self.send_json(store.view(identifier))
                else:
                    raise ValueError('未知操作')
            except (ValueError, KeyError, TypeError, OSError, AttributeError) as error:
                self.send_json({'error': str(error)}, 400)

    return Handler


def main():
    parser = argparse.ArgumentParser(description='eNodeDone — SSH 多机任务工作台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    store = TaskStore(args.data_dir)
    runner = SSHRunner(store)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store, runner))
    print(f'eNodeDone SSH: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for event in runner.stops.values():
            event.set()
        for thread in runner.threads.values():
            thread.join(timeout=30)
        runner.connections.close()
        server.server_close()


if __name__ == '__main__':
    main()
