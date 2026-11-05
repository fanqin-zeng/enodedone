"""File-based visualization API for Python and Shell pipeline nodes (Python 3.8+)."""
import argparse
import csv
import html
import json
import math
import os
from pathlib import Path
import statistics
import uuid

KINDS = ('line', 'box', 'bar', 'pie', 'scatter')
COLORS = ('#655ce0', '#26a18e', '#ec9a3c', '#dc6387', '#498fce', '#a978ca')


def load_data(path):
    """Read row lists, numeric lists, column dictionaries or label/value mappings."""
    path = Path(path)
    if path.suffix.lower() in ('.csv', '.tsv'):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ','))
    else:
        text = path.read_text(encoding='utf-8-sig')
        data = [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix.lower() in ('.jsonl', '.ndjson') else json.loads(text)
        if isinstance(data, list):
            rows = data
        else:
            if not isinstance(data, dict) or not data:
                raise ValueError('Data must be a JSON array or object')
            if all(isinstance(value, list) for value in data.values()):
                if len({len(value) for value in data.values()}) != 1:
                    raise ValueError('Column arrays must have equal length')
                rows = [dict(zip(data, values)) for values in zip(*data.values())]
            elif all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in data.values()):
                rows = [{'label': key, 'value': value} for key, value in data.items()]
            else:
                rows = [data]
    if not rows:
        raise ValueError('No data to plot')
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in rows):
        rows = [{'index': index, 'value': value} for index, value in enumerate(rows)]
    if not all(isinstance(row, dict) and row for row in rows):
        raise ValueError('Data must be a list of records or numbers')
    return rows


def number(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('Boolean values are not numeric chart values')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Chart values must be finite')
    return result


def text(value):
    return html.escape(str(value), quote=True)


def svg_chart(rows, kind, x, ys, title):
    values = [[number(row.get(key)) for row in rows] for key in ys]
    if not any(value is not None for series in values for value in series):
        raise ValueError('Selected columns contain no numeric data')
    labels = [str(row.get(x, index)) if x else str(index) for index, row in enumerate(rows)]
    width, height = 1000, 420
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<g font-family="system-ui,sans-serif" font-size="14" fill="#586076">',
             f'<text x="30" y="30" font-size="18" fill="#22283b">{text(title)}</text>']
    if kind == 'pie':
        if len(ys) != 1:
            raise ValueError('Pie charts require exactly one y column')
        series = values[0]
        if any(value is None or value < 0 for value in series) or sum(series) <= 0:
            raise ValueError('Pie charts require nonnegative values with a positive sum')
        angle = -math.pi / 2
        total = sum(series)
        for index, (label, value) in enumerate(zip(labels, series)):
            delta = value / total * math.tau
            color = COLORS[index % len(COLORS)]
            if delta >= math.tau - 1e-10:
                parts.append(f'<circle cx="280" cy="225" r="145" fill="{color}"/>')
            elif delta > 0:
                a, b = 280 + 145 * math.cos(angle), 225 + 145 * math.sin(angle)
                c, d = 280 + 145 * math.cos(angle + delta), 225 + 145 * math.sin(angle + delta)
                parts.append(f'<path d="M280,225 L{a},{b} A145,145 0 {int(delta > math.pi)},1 {c},{d} Z" fill="{color}"><title>{text(label)}: {value:g} ({value/total:.1%})</title></path>')
            angle += delta
            column, line = divmod(index, 18)
            px, py = 490 + column * 245, 62 + line * 19
            parts.append(f'<rect x="{px}" y="{py-11}" width="12" height="12" fill="{color}"/><text x="{px+20}" y="{py}">{text(label[:20])}: {value:g} ({value/total:.1%})</text>')
        return ''.join(parts) + '</g></svg>'

    left, right, top, bottom = 75, 970, 78, 342
    all_values = [v for series in values for v in series if v is not None]
    low, high = min(all_values), max(all_values)
    if kind == 'bar':
        low, high = min(0, low), max(0, high)
    span = high - low or abs(high) * .2 or 1
    if kind == 'bar':
        low = low - span * .07 if low < 0 else 0
        high = high + span * .07 if high > 0 else (1 if low == 0 else 0)
    else:
        low, high = low - span * .07, high + span * .07
    scale = lambda value: bottom - (value-low)/(high-low)*(bottom-top)
    for index in range(6):
        value = low + (high-low)*index/5
        py = scale(value)
        parts.append(f'<line x1="{left}" x2="{right}" y1="{py}" y2="{py}" stroke="#e9ecf3"/><text x="{left-12}" y="{py+5}" text-anchor="end">{value:.3g}</text>')
    parts.append(f'<line x1="{left}" x2="{left}" y1="{top}" y2="{bottom}" stroke="#bac1d1"/>')

    def tick(px, label):
        parts.append(f'<text x="{px}" y="{bottom+25}" text-anchor="middle">{text(label[:22])}</text>')

    if kind == 'box':
        # A box represents the distribution of each selected numeric column.
        for index, (key, series) in enumerate(zip(ys, values)):
            sample = sorted(v for v in series if v is not None)
            if not sample:
                continue
            def quantile(fraction):
                position = (len(sample)-1)*fraction
                lo, hi = math.floor(position), math.ceil(position)
                return sample[lo]+(sample[hi]-sample[lo])*(position-lo)
            q1, median, q3 = quantile(.25), statistics.median(sample), quantile(.75)
            iqr = q3-q1
            inside = [v for v in sample if q1-1.5*iqr <= v <= q3+1.5*iqr]
            lower, upper = min(inside), max(inside)
            px = left+(right-left)*(index+.5)/len(ys)
            box_width = min(85, (right-left)/len(ys)*.55)
            color = COLORS[index % len(COLORS)]
            parts.append(f'<line x1="{px}" x2="{px}" y1="{scale(lower)}" y2="{scale(upper)}" stroke="{color}"/>')
            for value in (lower, upper):
                parts.append(f'<line x1="{px-box_width/3}" x2="{px+box_width/3}" y1="{scale(value)}" y2="{scale(value)}" stroke="{color}"/>')
            parts.append(f'<rect x="{px-box_width/2}" y="{scale(q3)}" width="{box_width}" height="{max(.8,scale(q1)-scale(q3))}" fill="{color}" fill-opacity=".18" stroke="{color}"><title>{text(key)}: min={sample[0]:g}, Q1={q1:g}, median={median:g}, Q3={q3:g}, max={sample[-1]:g}</title></rect>')
            parts.append(f'<line x1="{px-box_width/2}" x2="{px+box_width/2}" y1="{scale(median)}" y2="{scale(median)}" stroke="{color}" stroke-width="3"/>')
            for value in sample:
                if value < lower or value > upper:
                    parts.append(f'<circle cx="{px}" cy="{scale(value)}" r="3" fill="{color}"/>')
            tick(px, key)
    else:
        count = len(rows)
        numeric_x = None
        if kind in ('line', 'scatter') and x:
            try:
                candidate = [number(row.get(x)) for row in rows]
                if all(v is not None for v in candidate):
                    numeric_x = candidate
            except (TypeError, ValueError):
                pass  # Text x values are categories in row order.
        def position(index):
            if numeric_x is not None:
                minimum, maximum = min(numeric_x), max(numeric_x)
                return (left+right)/2 if maximum == minimum else left+(right-left)*(numeric_x[index]-minimum)/(maximum-minimum)
            if kind == 'bar':
                return left+(right-left)*(index+.5)/count
            return left+(right-left)*index/max(1,count-1)
        for index in range(0, count, max(1, math.ceil(count/12))):
            tick(position(index), labels[index])
        for series_index, (key, series) in enumerate(zip(ys, values)):
            color = COLORS[series_index % len(COLORS)]
            segment = []
            for index, value in enumerate(series):
                if value is None:
                    if segment and kind == 'line':
                        parts.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2.5"/>')
                    segment = []
                    continue
                px, py = position(index), scale(value)
                tip = text(labels[index]+' / '+key+': '+format(value,'.6g'))
                if kind == 'bar':
                    unit = (right-left)/count*.75/len(ys)
                    bx = px-unit*len(ys)/2+series_index*unit
                    parts.append(f'<rect x="{bx}" y="{min(py,scale(0))}" width="{unit*.9}" height="{abs(py-scale(0))}" fill="{color}"><title>{tip}</title></rect>')
                else:
                    segment.append(f'{px},{py}')
                    parts.append(f'<circle cx="{px}" cy="{py}" r="{3 if kind == "line" else 4}" fill="{color}"><title>{tip}</title></circle>')
            if segment and kind == 'line':
                parts.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2.5"/>')
    for index, key in enumerate(ys):
        parts.append(f'<rect x="{left+index*180}" y="52" width="12" height="12" fill="{COLORS[index%len(COLORS)]}"/><text x="{left+20+index*180}" y="63">{text(key)}</text>')
    if x and kind != 'box':
        parts.append(f'<text x="{(left+right)/2}" y="400" text-anchor="middle">{text(x)}</text>')
    return ''.join(parts) + '</g></svg>'


def plot(data_path, show_table=True, kind=None, *, x=None, y=None, title=None, output_dir=None):
    """Publish a data table and/or chart. kind=None disables the chart; False/None is a no-op."""
    if not isinstance(show_table, bool):
        raise ValueError('show_table must be a boolean')
    if kind is not None and kind not in KINDS:
        raise ValueError('Unsupported chart kind: '+str(kind))
    if not show_table and kind is None:
        return None
    source = Path(data_path).resolve()
    rows = load_data(source)
    # Validate serialization before writing or publishing any files, including table-only data.
    try:
        serialized_rows = json.dumps(rows, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError('Data values must be finite and JSON serializable') from error
    if x is None and all('index' in row and 'value' in row for row in rows):
        x = 'index'
    if kind is not None and x and any(x not in row for row in rows):
        raise ValueError('Missing x column: '+x)
    if kind is None:
        ys = []
    elif y is None:
        ys = []
        for key in rows[0]:
            if key == x:
                continue
            try:
                if any(number(row.get(key)) is not None for row in rows):
                    # Validate every nonempty value, not just the first numeric row.
                    [number(row.get(key)) for row in rows]
                    ys.append(key)
            except (TypeError, ValueError):
                continue
    else:
        ys = [y] if isinstance(y, str) else list(y)
    if kind is not None and (not ys or any(any(key not in row for row in rows) for key in ys)):
        raise ValueError('Select existing numeric y columns')
    title = title or source.stem
    svg = svg_chart(rows, kind, x, ys, title) if kind is not None else None
    directory = Path(output_dir or os.environ.get('ENODEDONE_VIZ_DIR') or source.parent/'visualizations').resolve()
    if os.environ.get('ENODEDONE_VIZ_DIR') and directory != Path(os.environ['ENODEDONE_VIZ_DIR']).resolve():
        raise ValueError('Pipeline charts must use ENODEDONE_VIZ_DIR; omit output_dir')
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    metadata = {'version': 2, 'id': identifier, 'title': title, 'kind': kind, 'source_path': str(source),
                'show_table': show_table, 'x': x, 'y': ys, 'row_count': len(rows),
                'svg_path': str(directory/(identifier+'.svg')) if svg is not None else None,
                'data_path': str(directory/(identifier+'.data.json'))}
    if svg is not None:
        Path(metadata['svg_path']).write_text(svg, encoding='utf-8')
    Path(metadata['data_path']).write_text(serialized_rows, encoding='utf-8')
    manifest = directory/(identifier+'.json')
    manifest.write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    print('@@ENODEDONE_VIZ@@'+json.dumps({'id': identifier, 'manifest_path': str(manifest)}, ensure_ascii=True), flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True, help='JSON, JSONL, CSV or TSV data file')
    parser.add_argument('--show-table', choices=('true', 'false'), required=True, help='Display a data table')
    parser.add_argument('--kind', type=str.lower, choices=('none',)+KINDS, required=True, help='none disables the chart')
    parser.add_argument('--x')
    parser.add_argument('--y', action='append', help='Numeric column; repeat for multiple series')
    parser.add_argument('--title')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    plot(args.data, args.show_table == 'true', None if args.kind == 'none' else args.kind, x=args.x, y=args.y, title=args.title, output_dir=args.output_dir)


if __name__ == '__main__':
    main()
