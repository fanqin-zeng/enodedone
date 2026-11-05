"""Training log aggregation shared by local and remote analysis."""
import re

def analyze_logs(logs: dict, master: str, warmup: int) -> dict:
    """Aggregate real step IDs, max per-host latency and master-only loss/grad norm."""
    records = {}
    for host, content in logs.items():
        rows = {}
        for line in content.splitlines():
            step = re.search(r'(?:\bStep\s+|\biteration\s+)(\d+)', line, re.I)
            turbo_time = re.search(r'Step Time:\s*([\d.eE+-]+)s', line)
            mm_time = re.search(r'elapsed time per iteration \(ms\):\s*([\d.eE+-]+)', line, re.I)
            if not step or not (turbo_time or mm_time):
                continue
            loss = re.search(r'\bloss:\s*([\d.eE+-]+)', line, re.I)
            grad = re.search(r'grad[ _]norm:\s*([\d.eE+-]+)', line, re.I)
            rows[int(step[1])] = {'step': int(step[1]),
                'seconds': float(turbo_time[1]) if turbo_time else float(mm_time[1]) / 1000,
                'loss': float(loss[1]) if loss else None, 'grad_norm': float(grad[1]) if grad else None}
        records[host] = rows
    if not records or master not in records:
        raise ValueError('没有 master 训练日志')
    common = set.intersection(*(set(rows) for rows in records.values()))
    steps = sorted(step for step in common if step > warmup)
    if not steps:
        raise ValueError('warmup 后没有所有机器共有的有效 step')
    result = [{**records[master][step], 'seconds': max(rows[step]['seconds'] for rows in records.values())} for step in steps]
    import math
    if any(not math.isfinite(row[key]) for row in result for key in ('seconds', 'loss', 'grad_norm') if row[key] is not None):
        raise ValueError('指标包含非有限数值')
    def mean(key):
        values = [row[key] for row in result if row[key] is not None]
        return sum(values) / len(values) if values else None
    return {'warmup': warmup, 'count': len(result), 'rows': result, 'mean_seconds': mean('seconds'),
            'mean_loss': mean('loss'), 'mean_grad_norm': mean('grad_norm'),
            'excluded_steps': sorted(set.union(*(set(rows) for rows in records.values())) - set(steps)),
            'policy': '按 step 排除 warmup；耗时取各机器最大值；loss / grad_norm 取 master；仅汇总各机器共有 step。'}
