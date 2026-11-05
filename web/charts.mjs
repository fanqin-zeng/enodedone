// Convert the public file-based API's metadata and rows into interactive Plotly figures.
const safeText = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function numeric(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'boolean' || !Number.isFinite(Number(value))) throw new Error('绘图数据包含无效数值');
  return Number(value);
}

export function buildFigure(chart, rows) {
  if (!chart.kind) return null;
  const labels = rows.map((row, index) => chart.x ? row[chart.x] : index);
  const numericX = labels.every(value => value !== null && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value)));
  const x = numericX ? labels.map(Number) : labels.map(safeText);
  const traces = chart.y.map(key => {
    const y = rows.map(row => numeric(row[key]));
    const name = safeText(key);
    if (chart.kind === 'pie') return {type:'pie',labels:labels.map(safeText),values:y,name,sort:false,
      textinfo:'label+percent',hovertemplate:'%{label}<br>%{value}<br>%{percent}<extra></extra>'};
    if (chart.kind === 'box') return {type:'box',y,name,boxpoints:'outliers',quartilemethod:'linear',hoveron:'boxes+points'};
    return {type:chart.kind === 'bar' ? 'bar' : 'scatter',x,y,name,
      ...(chart.kind === 'bar' ? {} : {mode:chart.kind === 'scatter' ? 'markers' : 'lines+markers',connectgaps:false}),
      hovertemplate:`${safeText(chart.x || '序号')}: %{x}<br>${name}: %{y:.6g}<extra></extra>`};
  });
  return {data:traces,layout:{
    title:{text:safeText(chart.title),font:{size:18}},
    font:{family:'Inter, Segoe UI, Microsoft YaHei, sans-serif',size:14,color:'#586076'},
    colorway:['#655ce0','#26a18e','#ec9a3c','#dc6387','#498fce','#a978ca'],
    paper_bgcolor:'#fff',plot_bgcolor:'#fff',height:430,
    margin:{l:76,r:30,t:65,b:70},dragmode:'pan',hovermode:'closest',
    uirevision:chart.id,showlegend:traces.length > 1 || chart.kind === 'pie',
    legend:{orientation:'h',y:1.12},barmode:'group',
    xaxis:{title:{text:chart.kind === 'box' ? '' : safeText(chart.x || '序号')},automargin:true,gridcolor:'#edf0f6',zerolinecolor:'#d4d9e4'},
    yaxis:{title:{text:chart.y.length === 1 ? safeText(chart.y[0]) : ''},automargin:true,gridcolor:'#edf0f6',zerolinecolor:'#d4d9e4'}
  }};
}

export const chartConfig = {scrollZoom:true,responsive:true,displayModeBar:false,displaylogo:false,
  doubleClick:'reset+autosize',locale:'zh-CN'};
