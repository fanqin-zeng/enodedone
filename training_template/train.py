"""Task-owned Kimi K31 launcher. Only main() starts a container."""
import base64
import glob
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess


ENV_KEYS = ('HCCL_SOCKET_IFNAME', 'GLOO_SOCKET_IFNAME', 'HCCL_NPU_SOCKET_PORT_RANGE',
            'MULTI_STREAM_MEMORY_REUSE', 'TASK_QUEUE_ENABLE', 'ASCEND_LAUNCH_BLOCKING',
            'ACLNN_CACHE_LIMIT', 'CPU_AFFINITY_CONF', 'PYTORCH_NPU_ALLOC_CONF',
            'HCCL_DETERMINISTIC', 'CLOSE_MATMUL_K_SHIFT', 'PYTHONHASHSEED')

# No torch/fsdp_turbo imports: prepare YAML before the training entry imports them.
PREPARE_CONFIG = r'''
import json, os
from pathlib import Path
import yaml
cfg = yaml.safe_load(Path('/configs/source.yaml').read_text())
if not isinstance(cfg, dict):raise ValueError('训练 YAML 必须是 mapping')
cfg['model']['model_name_or_path'] = os.environ['MODEL_PATH']
cfg['model']['tokenizer_name_or_path'] = os.environ['TOKENIZER_PATH']
cfg['data']['dataset_path'] = os.environ['DATASET_PATH']
for key in ('MODEL_PATH','TOKENIZER_PATH','DATASET_PATH'):
    if not Path(os.environ[key]).exists():raise ValueError(key+' 不存在: '+os.environ[key])
run = cfg.setdefault('run', {})
run['max_steps'] = int(os.environ['MAX_STEPS'])
run['logging_steps'] = 1
profile = run.setdefault('profile', {})
profile['enabled'] = os.environ['PROFILE_ENABLED'] == 'true'
profile['output_dir'] = '/outputs/profiling'
snapshot = run.setdefault('memory_snapshot', {})
snapshot['enable'] = os.environ['SNAPSHOT_ENABLED'] == 'true'
snapshot['save_path'] = '/outputs/snapshot'
cfg.setdefault('checkpoint', {})['output_dir'] = '/outputs/checkpoints'
dist = cfg.get('distributed', {})
world = int(os.environ['NNODES']) * int(os.environ['NPUS_PER_NODE'])
tp = int(dist.get('tensor_parallel_size', 1))
cp = max(int(dist.get('ulysses_parallel_size', 1)), int(dist.get('kcp_parallel_size', 1)))
kv = int(dist.get('kv_allgather_parallel_size', 1))
fsdp = int(dist.get('fully_shard_parallel_size', 1))
ep = int(dist.get('expert_parallel_size', 1))
efsdp = int(dist.get('expert_fully_shard_parallel_size', 1))
for size in (tp, cp, kv, fsdp, ep, efsdp):
    if size < 1:raise ValueError('并行维度必须为正整数')
for size in (tp*fsdp, ep*efsdp, tp*cp*kv):
    if world % size:raise ValueError('WORLD_SIZE 不能整除配置中的并行维度')
batch = int(cfg['data']['batch_size']) * int(run.get('gradient_accumulation_steps', 1))
samples = batch * (world // (tp*cp*kv))
Path('/outputs/effective_config.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))
Path('/outputs/launch.json').write_text(json.dumps({
    'rank':int(os.environ['NODE_RANK']), 'world_size':world, 'samples_per_step':samples,
    'batch_policy':'standard non-packed samples; micro_batch * accumulation * world/(TP*CP*KV)',
    'max_steps':run['max_steps'], 'entrypoint':os.environ['ENTRYPOINT'],
    'image':os.environ['DOCKER_IMAGE'], 'code_dir':os.environ['CODE_DIR']}, indent=2))
'''


def positive(settings, key):
    value = str(settings[key])
    if not re.fullmatch(r'[1-9][0-9]*', value):raise ValueError(key+' 必须为正整数')
    return int(value)


def build_command(context, settings, devices):
    """Build argv only; never inspect a device or start a process."""
    count = positive(settings, 'NPUS_PER_NODE')
    if positive(settings, 'MASTER_PORT') > 65535:raise ValueError('MASTER_PORT 超出范围')
    positive(settings, 'MAX_STEPS')
    hosts = context['hosts']
    ips = [str(ipaddress.IPv4Address(host['Info'])) for host in hosts]
    if not ips or len(ips) != len(set(ips)):raise ValueError('机器 IP 为空或重复')
    rank = context['rank']
    if type(rank) is not int or not 0 <= rank < len(hosts):raise ValueError('无效 NODE_RANK')
    for key in ('PROFILE_ENABLED', 'SNAPSHOT_ENABLED'):
        if settings[key] not in ('true', 'false'):raise ValueError(key+' 必须为 true/false')
    for key in ('CODE_DIR', 'CONFIG_PATH', 'MODELS_DIR', 'DATA_DIR', 'DRIVER_DIR',
                'CONTAINER_CODE_DIR',
                'MODEL_PATH', 'TOKENIZER_PATH', 'DATASET_PATH'):
        if not str(settings[key]).startswith('/'):raise ValueError(key+' 必须为绝对路径')
    for key in ('CONDA_PREFIX', 'CONDA_SH', 'CANN_ENV', 'IMAGE_ENTRYPOINT'):
        if settings[key] and not settings[key].startswith('/'):raise ValueError(key+' 必须为空或绝对路径')
    if settings['CONDA_SH'] and not settings['CONDA_ENV']:raise ValueError('设置 CONDA_SH 时必须填写 CONDA_ENV')
    entry = PurePosixPath(settings['ENTRYPOINT'])
    if entry.is_absolute() or '..' in entry.parts:raise ValueError('ENTRYPOINT 必须为代码目录内相对路径')
    interface = settings['HCCL_SOCKET_IFNAME']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):raise ValueError('请配置实际 HCCL_SOCKET_IFNAME')
    image = settings['DOCKER_IMAGE']
    if not image or image.startswith('-') or any(c.isspace() for c in image):raise ValueError('无效 DOCKER_IMAGE')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', context['run_id']):raise ValueError('无效 RUN_ID')
    output = '/tmp/enodedone/'+context['run_id']
    name = 'enodedone-'+context['run_id'][:16]+'-rank'+str(rank)
    argv = ['docker', 'run', '--rm', '--name', name, '--network', 'host', '--ipc', 'host',
            '--user', '0:0', '--workdir', settings['CONTAINER_CODE_DIR']]
    if settings['IMAGE_ENTRYPOINT']:argv += ['--entrypoint', settings['IMAGE_ENTRYPOINT']]
    for source, destination, readonly in (
        (settings['CODE_DIR'], settings['CONTAINER_CODE_DIR'], True),
        (settings['CONFIG_PATH'], '/configs/source.yaml', True),
        (settings['MODELS_DIR'], '/models', True), (settings['DATA_DIR'], '/data', True),
        (settings['DRIVER_DIR'], '/usr/local/Ascend/driver', True), (output, '/outputs', False)):
        if any(c in source+destination for c in ',\n\r'):raise ValueError('挂载路径不能含逗号或换行')
        argv += ['--mount', 'type=bind,src='+source+',dst='+destination+(',readonly' if readonly else '')]
    for device in devices:argv += ['--device', device+':'+device]
    environment = {key:str(settings[key]) for key in ENV_KEYS}
    environment['GLOO_SOCKET_IFNAME'] = settings['GLOO_SOCKET_IFNAME'] or interface
    environment.update({key:str(settings[key]) for key in (
        'CONDA_PREFIX', 'CONDA_SH', 'CONDA_ENV', 'CANN_ENV', 'MODEL_PATH', 'TOKENIZER_PATH',
        'DATASET_PATH', 'MAX_STEPS', 'PROFILE_ENABLED', 'SNAPSHOT_ENABLED', 'DOCKER_IMAGE', 'ENTRYPOINT')})
    # Empty optional overrides inherit the environment already configured in the image.
    for key in ('CONDA_PREFIX', 'CANN_ENV'):
        if not environment[key]:environment.pop(key)
    environment.update(NNODES=str(len(hosts)), NODE_RANK=str(rank), MASTER_ADDR=ips[0],
                       NPUS_PER_NODE=str(count), MASTER_PORT=settings['MASTER_PORT'],
                       CODE_DIR=settings['CODE_DIR'], FSDP_ROOT=settings['CONTAINER_CODE_DIR'],
                       PYTHONUNBUFFERED='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    for key, value in environment.items():argv += ['--env', key+'='+value]
    torchrun = ['torchrun', '--nnodes', str(len(hosts)), '--node_rank', str(rank),
                '--nproc_per_node', str(count), '--master_addr', ips[0],
                '--master_port', settings['MASTER_PORT'], str(entry), '--config', '/outputs/effective_config.yaml']
    torchrun += shlex.split(settings['EXTRA_TRAIN_ARGS'])
    shell = '''set -eo pipefail
if [[ -n "$CONDA_SH" ]]; then
    source "$CONDA_SH"
    conda activate "$CONDA_ENV"
elif [[ -n "${CONDA_PREFIX:-}" ]]; then
    export PATH="$CONDA_PREFIX/bin:$PATH"
fi
[[ -n "${CANN_ENV:-}" && -r "$CANN_ENV" ]] || { echo "Configure CANN_ENV in KV or image ENV" >&2; exit 2; }
source "$CANN_ENV"
command -v torchrun >/dev/null
[[ -r "$ENTRYPOINT" ]] || { echo "Missing ENTRYPOINT=$ENTRYPOINT" >&2; exit 2; }
python3 - <<'ENODEDONE_CONFIG_PY'
'''+PREPARE_CONFIG+"\nENODEDONE_CONFIG_PY\nexec "+' '.join(shlex.quote(arg) for arg in torchrun)
    return argv+[image, '/bin/bash', '--noprofile', '--norc', '-c', shell], name, output


def main():
    context = json.loads(base64.b64decode(os.environ['ENODEDONE_CONTEXT']))
    settings = dict(os.environ)
    devices = sorted(glob.glob('/dev/davinci[0-9]*'))
    devices += ['/dev/davinci_manager', '/dev/devmm_svm', '/dev/hisi_hdc']
    argv, name, output = build_command(context, settings, devices)
    # Host filesystem/network checks only. The framework checks NPU idleness before this node.
    for key in ('CODE_DIR', 'MODELS_DIR', 'DATA_DIR', 'DRIVER_DIR'):
        if not Path(settings[key]).is_dir():raise ValueError(key+' 目录不存在: '+settings[key])
    if not Path(settings['CONFIG_PATH']).is_file():raise ValueError('CONFIG_PATH 文件不存在')
    if not (Path(settings['CODE_DIR'])/settings['ENTRYPOINT']).is_file():raise ValueError('共享目录中缺少 ENTRYPOINT')
    if not glob.glob('/dev/davinci[0-9]*'):raise ValueError('未找到计算设备')
    for device in devices:
        if not Path(device).exists():raise ValueError('设备不存在: '+device)
    for interface in (settings['HCCL_SOCKET_IFNAME'], settings['GLOO_SOCKET_IFNAME'] or settings['HCCL_SOCKET_IFNAME']):
        if not (Path('/sys/class/net')/interface).is_dir():raise ValueError('网络接口不存在: '+interface)
    subprocess.run(['docker', 'image', 'inspect', '--', settings['DOCKER_IMAGE']], check=True,
                   stdout=subprocess.DEVNULL, timeout=30)
    directory = Path(output);directory.mkdir(parents=True, exist_ok=True)
    print('NNODES='+str(len(context['hosts']))+' NODE_RANK='+str(context['rank'])+
          ' WORLD_SIZE='+str(len(context['hosts'])*int(settings['NPUS_PER_NODE'])), flush=True)
    print('Container command: '+' '.join(shlex.quote(arg) for arg in argv), flush=True)
    previous = {}
    def stop(signum, frame):
        raise InterruptedError('训练被停止')
    for sig in (signal.SIGTERM, signal.SIGINT):previous[sig] = signal.signal(sig, stop)
    try:
        with (directory/'train.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', start_new_session=True)
            try:
                for line in process.stdout:
                    print(line, end='', flush=True);log.write(line);log.flush()
                code = process.wait()
                if code:raise RuntimeError('容器训练失败，退出码 '+str(code))
            finally:
                if process.poll() is None:
                    subprocess.run(['docker', 'stop', '--time', '10', name], timeout=20, check=False)
                    process.wait(timeout=20)
                process.stdout.close()
    finally:
        for sig, handler in previous.items():signal.signal(sig, handler)


if __name__ == '__main__':
    main()
