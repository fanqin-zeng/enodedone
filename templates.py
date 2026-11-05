"""Editable SSH templates. Creation and saving never execute scripts."""
from pathlib import Path

OPERATIONS = {'nodes_ssh':'检查与建立NODES SSH公钥连接','push':'脚本推送 master','ping':'master ping 其他机器','init':'master 配置初始化',
 'distribute':'镜像检查、分发与应用','shared':'共享存储检查','sample':'随机延时采样并同步',
 'train':'多机训练拉起','collect':'汇总数据到 master','analyze':'master 分析数据','custom':'自定义脚本'}

def stage(operation, scope='all', **params):
    if operation=='nodes_ssh':
        return {'id':operation,'name':OPERATIONS[operation],'operation':operation,'scope':'all',
          'script_ip':'127.0.0.1','script_path':'${TASK_DIR}/scripts/nodes_ssh.py','language':'python',
          'script_content':Path(__file__).with_name('nodes_ssh.py').read_text(encoding='utf-8'),'params':{}}
    if operation == 'distribute':
        params = {'IMAGE_PATH':'${MASTER_DIR}/image.tar',
                  'IMAGE_DEST':'/tmp/enodedone/images/image.tar', **params}
    language = 'sh' if operation == 'custom' else 'python'
    content = '#!/usr/bin/env bash\nset -euo pipefail\necho "请填写实际任务脚本"\n' if operation == 'custom' else Path(__file__).with_name('remote_runtime.py').read_text(encoding='utf-8') + '\n' + Path(__file__).with_name('analysis.py').read_text(encoding='utf-8') + f'\n{operation}()\n'
    return {'id':operation,'name':OPERATIONS[operation],'operation':operation,'scope':scope,
      'script_ip':'${MASTER_IP}','script_path':'${MASTER_DIR}/pipeline_scripts/'+operation+('.sh' if language=='sh' else '.py'),
      'language':language,'script_content':content,'params':{k:str(v) for k,v in params.items()}}

def builtins():
    return [
      {'id':'training','name':'训练','icon':'','description':'环境、分发、共享检查、容器训练与结果分析',
       'config':{'command_timeout':7200},'nodes':training_nodes()},
      {'id':'inference','name':'推理','icon':'','description':'执行自己的推理脚本','nodes':[stage('push','master'),stage('distribute'),{**stage('custom'),'name':'启动推理服务'}]},
      {'id':'rl','name':'后训练','icon':'','description':'配置 rollout、actor、reward 实际脚本','nodes':[stage('push','master'),stage('distribute'),{**stage('custom'),'name':'后训练脚本'}]},
      {'id':'copy','name':'镜像分发','icon':'','description':'SHA256 检查、倍增拷贝与应用','nodes':[stage('push','master'),stage('distribute')]},
      {'id':'blank','name':'空白任务','icon':'+','description':'配置 shell 或 Python 脚本','nodes':[stage('push','master'),stage('custom','master')]},
    ]

def training_nodes():
    """Snapshot standalone FSDPTurbo stages into each new editable training task."""
    folder=Path(__file__).with_name('training_template')
    common=(folder/'common.py').read_text(encoding='utf-8')
    code='${MASTER_DIR}/code/FSDPTurbo'
    entry='examples/kimi_k31/train.py'
    config_path='${MASTER_DIR}/configs/train.yaml'
    params={
        'init':{'CODE_DIR':code,'ENTRYPOINT':entry,'CONFIG_PATH':config_path},
        'shared':{'CODE_DIR':code,'ENTRYPOINT':entry},
        'train':{
            'DOCKER_IMAGE':'fsdpturbo-kimi-k31:env-20261031','IMAGE_ENTRYPOINT':'',
            'CODE_DIR':code,'CONTAINER_CODE_DIR':'/workspace/FSDPTurbo','ENTRYPOINT':entry,
            'CONFIG_PATH':config_path,'CONDA_PREFIX':'','CONDA_SH':'','CONDA_ENV':'','CANN_ENV':'',
            'MODELS_DIR':'${MASTER_DIR}/assets/models','DATA_DIR':'${MASTER_DIR}/assets/data',
            'MODEL_PATH':'/workspace/FSDPTurbo/examples/kimi_k31',
            'TOKENIZER_PATH':'/models/kimi-k31','DATASET_PATH':'/data/dataset.jsonl',
            'DRIVER_DIR':'/usr/local/Ascend/driver','NPUS_PER_NODE':'16','MASTER_PORT':'6000',
            'MAX_STEPS':'200','HCCL_SOCKET_IFNAME':'','GLOO_SOCKET_IFNAME':'',
            'HCCL_NPU_SOCKET_PORT_RANGE':'auto','MULTI_STREAM_MEMORY_REUSE':'2','TASK_QUEUE_ENABLE':'2',
            'ASCEND_LAUNCH_BLOCKING':'0','ACLNN_CACHE_LIMIT':'100000','CPU_AFFINITY_CONF':'1',
            'PYTORCH_NPU_ALLOC_CONF':'expandable_segments:True','HCCL_DETERMINISTIC':'True',
            'CLOSE_MATMUL_K_SHIFT':'1','PYTHONHASHSEED':'42','PROFILE_ENABLED':'false',
            'SNAPSHOT_ENABLED':'false','EXTRA_TRAIN_ARGS':''},
        'collect':{},
        'analyze':{'STATS_START_STEP':'101','STATS_END_STEP':'200','GLOBAL_BATCH_SIZE':'auto',
                   'LOG_POLICY':'master','REQUIRE_FULL_WINDOW':'true','SHOW_TABLE':'true'}}
    nodes=[]
    for operation,scope in (('push','master'),('init','master'),('distribute','all'),
                             ('shared','all'),('train','all'),('collect','all'),('analyze','master')):
        node=stage(operation,scope,**params.get(operation,{}))
        if operation in params:
            content=(folder/(operation+'.py')).read_text(encoding='utf-8')
            if operation=='init':
                content=content.replace(repr('__EMBED_TRAINING_CONFIG__'),
                                        repr((folder/'config.yaml').read_text(encoding='utf-8')))
            if operation in ('shared','collect'):content=common+'\n\n'+content
            node['script_content']=content
        nodes.append(node)
    return nodes


def test_nodes():
    return [stage('push','master'),stage('distribute'),stage('shared'),stage('sample',DELAY_MIN_SECONDS=1,DELAY_MAX_SECONDS=5),stage('collect'),stage('analyze','master',SHOW_TABLE='true',CHART_KIND='bar')]

def config():
    return {'master_ip':'','master_dir':'/mnt/share/enodedone',
      'ssh_user':'root','ssh_timeout':15,'command_timeout':1800,'artifact_kind':'docker',
      'shared_dir':'/mnt/share/enodedone/data'}
