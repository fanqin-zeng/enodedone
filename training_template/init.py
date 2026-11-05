"""Create a task-owned base YAML if missing; never mutate the shared code or environment."""
import base64
import json
import os
from pathlib import Path
import subprocess

TEMPLATE = '__EMBED_TRAINING_CONFIG__'


def initialize(context, settings):
    root = Path(context['master_run']);root.mkdir(parents=True, exist_ok=True)
    code = Path(settings['CODE_DIR'])
    if not code.is_dir():raise ValueError('请先准备共享 K31 代码目录: '+str(code))
    entry = code/settings['ENTRYPOINT']
    if not entry.is_file():raise ValueError('K31 训练入口不存在: '+str(entry))
    config = Path(settings['CONFIG_PATH'])
    if not config.is_absolute():raise ValueError('CONFIG_PATH 必须为绝对路径')
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config.open('x', encoding='utf-8') as stream:stream.write(TEMPLATE)
        print('已创建基础配置: '+str(config), flush=True)
    except FileExistsError:
        if not config.is_file():raise ValueError('CONFIG_PATH 不是文件')
        print('保留已有基础配置: '+str(config), flush=True)
    subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'], check=True, timeout=30)
    print('master 初始化完成；CANN/conda 将由每机容器在训练进程内初始化', flush=True)


if __name__ == '__main__':
    initialize(json.loads(base64.b64decode(os.environ['ENODEDONE_CONTEXT'])), os.environ)
