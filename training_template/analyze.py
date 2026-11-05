"""FSDPTurbo/MindSpeed log metrics; plotting uses eNodeDone's public interface."""
import base64
import json
import math
import os
from pathlib import Path
import re


NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'


def parse_log(content):
    rows = {}
    for line in content.splitlines():
        step = re.search(r'\b(?:Step|iteration)\s+(\d+)', line, re.I)
        seconds = re.search(r'\bStep Time:\s*('+NUMBER+r')s', line, re.I)
        millis = re.search(r'elapsed time per iteration\s*\(ms\):\s*('+NUMBER+r')', line, re.I)
        if not step or not (seconds or millis):continue
        row = {'step':int(step[1]), 'seconds':float(seconds[1]) if seconds else float(millis[1])/1000}
        for key, pattern in (('loss', r'\bloss:'), ('grad_norm', r'\bgrad[ _]norm:')):
            match = re.search(pattern+r'\s*('+NUMBER+r')', line, re.I)
            row[key] = float(match[1]) if match else None
        if row['seconds'] <= 0 or any(not math.isfinite(v) for v in row.values() if v is not None):
            raise ValueError('日志包含无效指标: '+line)
        rows[row['step']] = row
    return rows


def summarize(logs, master, start, end, global_batch, policy='master', require_full=True):
    """Use step IDs 101..200 by default; rank-0 logging does not require peer metrics."""
    if start < 1 or end < start:raise ValueError('无效统计步数区间')
    if policy not in ('master', 'slowest'):raise ValueError('LOG_POLICY 必须为 master/slowest')
    records = {ip:parse_log(text) for ip, text in logs.items()}
    if not records.get(master):raise ValueError('master 没有有效训练指标')
    rows = []
    for step, original in sorted(records[master].items()):
        row = dict(original)
        if policy == 'slowest':
            if any(step not in values for values in records.values()):continue
            row['seconds'] = max(values[step]['seconds'] for values in records.values())
        rows.append(row)
    selected = [row for row in rows if start <= row['step'] <= end]
    missing = sorted(set(range(start, end+1))-{row['step'] for row in selected})
    if not selected or (require_full and missing):
        raise ValueError('统计区间缺少训练 step: '+str(missing[:20])+(' ...' if len(missing)>20 else ''))
    if global_batch is not None and (not math.isfinite(global_batch) or global_batch <= 0):
        raise ValueError('GLOBAL_BATCH_SIZE 必须大于零')
    def mean(key):
        values = [row[key] for row in selected if row[key] is not None]
        return sum(values)/len(values) if values else None
    seconds = mean('seconds')
    return {'kind':'training', 'rows':rows, 'stats_rows':selected, 'count':len(selected),
            'start_step':start, 'end_step':end, 'missing_steps':missing, 'policy':policy,
            'global_batch_size':global_batch, 'mean_seconds':seconds, 'mean_ms':seconds*1000,
            'mean_loss':mean('loss'), 'mean_grad_norm':mean('grad_norm'),
            'samples_per_second':global_batch/seconds if global_batch is not None else None}


def main():
    context = json.loads(base64.b64decode(os.environ['ENODEDONE_CONTEXT']))
    root = Path(context['master_run'])
    logs = {host['Info']:(root/'nodes'/str(rank)/'train.log').read_text(encoding='utf-8', errors='replace')
            for rank, host in enumerate(context['hosts'])}
    requested = os.environ['GLOBAL_BATCH_SIZE'].strip()
    master = context['hosts'][0]['Info']
    if requested.lower() == 'auto':
        metadata = root/'nodes/0/launch.json'
        batch = json.loads(metadata.read_text())['samples_per_step'] if metadata.is_file() else None
        if batch is None:
            match = re.search(r'global batch size:\s*('+NUMBER+r')', logs[master], re.I)
            batch = float(match[1]) if match else None
    else:batch = float(requested)
    for key in ('SHOW_TABLE', 'REQUIRE_FULL_WINDOW'):
        if os.environ[key] not in ('true', 'false'):raise ValueError(key+' 必须为 true/false')
    result = summarize(logs, master, int(os.environ['STATS_START_STEP']), int(os.environ['STATS_END_STEP']),
                       batch, os.environ['LOG_POLICY'], os.environ['REQUIRE_FULL_WINDOW']=='true')
    if requested.lower() == 'auto':
        result['batch_policy'] = '来自 launch.json 的标准非 packed batch；特殊输入请显式设置 GLOBAL_BATCH_SIZE'
    (root/'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    data = root/'training_metrics.json'
    data.write_text(json.dumps(result['rows']), encoding='utf-8')
    print('Elapsed Time Per iteration (ms): '+format(result['mean_ms'], '.1f'), flush=True)
    if result['samples_per_second'] is not None:
        print('Average Samples per Second: '+format(result['samples_per_second'], '.3f'), flush=True)
    else:print('日志没有 batch size；耗时统计有效，无法计算 samples/s', flush=True)
    print('统计 step '+str(result['start_step'])+'–'+str(result['end_step'])+
          '；有效 '+str(result['count'])+' 步；Loss='+str(result['mean_loss'])+
          '；Grad norm='+str(result['mean_grad_norm']), flush=True)
    from enodedone_viz import plot
    for key, title in (('loss', 'Loss'), ('grad_norm', 'Grad norm'), ('seconds', 'Step time (s)')):
        if any(row[key] is not None for row in result['rows']):
            plot(data, show_table=os.environ['SHOW_TABLE']=='true', kind='line', x='step', y=key, title=title)
        else:print('日志没有 '+key+'，跳过该图表', flush=True)


if __name__ == '__main__':
    main()
