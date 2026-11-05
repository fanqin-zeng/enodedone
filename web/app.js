import {buildFigure, chartConfig} from './charts.mjs';
import {formatTerminal,formatTaskLogs} from './terminal.mjs';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {pending:'待运行',running:'运行中',success:'运行成功',failed:'运行失败',stopped:'已停止'};
let library = [], templates = [], operations = {}, creationDefaults = {}, task = null, run = null, connections = null, dirty = false, polling = false;
let chartKey = 'seconds', consoleContext = 'host', selectedDetail = null, expandedKV = new Set(), history = [], historyIndex = 0;
let lastRuntime = '';
let visualizationSession = '', activeVisualization = '';
const visualizationViews = new Map();
const connectionEvents = new Map();
const connectionLabels={pending:'未连接',running:'连接中',success:'已连接',failed:'连接异常'};

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function toast(message, error = false) {
  const notice = $('toast');
  clearTimeout(toast.timer); clearTimeout(toast.hideTimer);
  notice.textContent = message; notice.className = error ? 'error' : ''; notice.hidden = false;
  toast.timer = setTimeout(() => {
    notice.classList.add('fading');
    toast.hideTimer = setTimeout(() => notice.hidden = true, 250);
  }, 4250);
}
function protect(action) { return async (...args) => { try { await action(...args); } catch (error) { toast(error.message, true); } }; }
function badge(status = 'pending') { return `<span class="badge ${status}"><i></i>${labels[status] || status}</span>`; }
function markDirty() { dirty = true; $('saved-state').textContent = '有未保存更改'; $('save-task').classList.add('unsaved'); }
function expand(value) {
  return String(value).replaceAll('${MASTER_IP}', task.config.master_ip).replaceAll('${MASTER_DIR}', task.config.master_dir)
    .replaceAll('${WARMUP_STEPS}', String(task.config.warmup_steps ?? 0)).replaceAll('${TASK_DIR}', task.file_path.replace(/[\\/][^\\/]+$/,''));
}
function stageState(index) {
  const node = task.nodes[index];
  const found = run?.stages.find(stage => stage.id === node.id);
  if (found) return found;
  const machines = orderedMachines();
  const targets = node.scope === 'master' ? machines.filter(machine => task.config.master_ip && machine.Info === task.config.master_ip) : machines;
  return {status:'pending', cells:targets.map(host => ({ip:host.Info,machine:host.Machine,rank:machines.indexOf(host),status:'pending',message:'等待运行'})),events:[],rounds:[]};
}
function creationTemplates(){
  return templates.filter(item=>item.id!=='copy').sort((a,b)=>Number(a.id==='blank')-Number(b.id==='blank'));
}
function generatedTaskName(templateId,stamp){
  const template=templates.find(item=>item.id===templateId);
  return (templateId==='blank'?'任务':`${template?.name || ''}任务`)+stamp;
}
function renderLibrary() {
  const search = $('task-search').value.toLowerCase();
  $('task-list').innerHTML = library.filter(item => item.name.toLowerCase().includes(search)).map(item =>
    `<button class="library-task ${item.id === task?.id ? 'selected' : ''}" data-task="${item.id}"><span>◈</span><div>${esc(item.name)}</div></button>`).join('') || '<p class="empty-text">没有匹配的任务</p>';
  $('template-list').innerHTML = creationTemplates().map(item => `<button class="template-link" data-template="${item.id}">${item.icon ? `<span>${esc(item.icon)}</span>` : ''}${esc(item.name)}<span class="template-arrow">↗</span></button>`).join('');
}
async function refreshLibrary() {
  const data = await api('/api/bootstrap'); if(data.mode !== 'ssh' || data.visualization_api !== 3 || data.execution_api !== 11) throw new Error('执行服务版本不匹配，请使用更新后的 eNodeDone 服务地址'); library = data.tasks; templates = data.templates; operations = data.operations; creationDefaults = data.creation_defaults; renderLibrary();
}
async function selectTask(identifier) {
  if (dirty && !confirm('当前任务有未保存修改。放弃修改并切换？')) return;
  connections = await api(`/api/tasks/${identifier}/connections`);
  task = await api(`/api/tasks/${identifier}`); run = await api(`/api/tasks/${identifier}/run`);
  if(!connectionEvents.has(identifier)) connectionEvents.set(identifier,0);
  dirty = false; selectedDetail = null; closeDrawer(); lastRuntime = ''; renderTask();
}
function clearTask(){
  task=null;run=null;connections=null;dirty=false;lastRuntime='';closeDrawer();
  $('crumb-name').textContent='未选择任务';document.querySelector('.page').hidden=true;
  for(const id of ['delete-task','copy-task','save-task','connect-nodes','disconnect-nodes','run-task'])$(id).disabled=true;
  $('run-task').hidden=false;$('stop-task').hidden=true;renderLibrary();
}
function renderTask() {
  document.querySelector(".page").hidden=false;
  $('task-name').value = task.name; $('crumb-name').textContent = task.name;
  $('master-dir').value = task.config.master_dir;
  renderMachineFields();
  $('csv-name').textContent = task.csv_file.split(/[\\/]/).pop(); $('csv-open').title = task.csv_file;
  $('task-path').textContent = task.file_path; $('task-path').title = task.file_path;
  $('saved-state').textContent = '已保存到本地'; $('save-task').classList.remove('unsaved');
  renderLibrary(); renderRuntime(true);
}
function renderMachineFields() {
  $('machine-limit').value = task.machine_count;
  $('master-ip').innerHTML = '<option value="">请选择 Master IP</option>' +
    task.machines.filter(machine => machine.Info).map(machine => `<option value="${esc(machine.Info)}">${esc(machine.Info)} · ${esc(machine.Machine)}</option>`).join('');
  $('master-ip').value = task.config.master_ip;
  $('machine-count').textContent = `${task.machines.length} 台机器`;
  $('console-caption').textContent = task.config.master_ip ? `${task.config.master_ip} · Master SSH` : 'Master SSH';
}
function applyMachineCount() {
  const value = $('machine-limit').value.trim(), count = Number(value);
  if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(count)) throw new Error('机器数必须为正整数');
  if (count === task.machine_count) return;
  const master = $('master-ip').value;
  task.machine_count = count;
  task.machines = task.csv_machines.slice(0, count);
  task.config.master_ip = task.machines.some(machine => machine.Info === master) ? master : '';
  renderMachineFields();
  run = null; closeDrawer(); markDirty(); renderRuntime(true);
  if (master && !task.config.master_ip) toast('原 Master 超出当前机器范围，请重新选择 Master IP');
}
function readForm() {
  applyMachineCount();
  task.name = $('task-name').value.trim();
  const fields = {'master-ip':'master_ip','master-dir':'master_dir'};
  for (const [id,key] of Object.entries(fields)) task.config[key] = $(id).value.trim();
}
async function saveTask(notify = true) {
  readForm();
  const master = task.config.master_ip;
  task = await api(`/api/tasks/${task.id}/save`, {...task, csv_machines:undefined}); dirty = false;
  renderMachineFields();
  $('saved-state').textContent = '已保存到本地'; $('save-task').classList.remove('unsaved'); $('crumb-name').textContent = task.name;
  await refreshLibrary();
  if (master && !task.config.master_ip) toast('原 Master 超出当前机器范围，请重新选择 Master IP');
  else if (notify) toast('任务、CSV 引用与脚本副本已保存');
}
function renderStageCard(node,index,stage,ssh=false) {
    const master = node.scope === 'master', names = ssh ? connectionLabels : labels;
    const renderCell = (cell,cellIndex) => `<button class="matrix-cell ${cell.status}" data-cell="${index}:${cellIndex}" aria-label="${esc(node.name)} ${esc(cell.machine)} ${names[cell.status]}" title="rank ${cell.rank} · ${esc(cell.machine)} · ${esc(cell.ip)} · ${names[cell.status]}">${master ? 'M' : ''}</button>`;
    const clusters = [];
    for (let clusterOffset = 0; clusterOffset < stage.cells.length && !master; clusterOffset += 512) {
      const clusterCells = stage.cells.slice(clusterOffset,clusterOffset + 512), groups = [];
      for (let groupOffset = 0; groupOffset < clusterCells.length; groupOffset += 64) {
        const groupCells = clusterCells.slice(groupOffset,groupOffset + 64), racks = [];
        for (let rackOffset = 0; rackOffset < groupCells.length; rackOffset += 8) {
          const cells = groupCells.slice(rackOffset,rackOffset + 8);
          // Keep the stage-wide cell index so details still open the correct rank after grouping.
          const offset = clusterOffset + groupOffset + rackOffset;
          const padding = '<i class="matrix-ghost" aria-hidden="true"></i>'.repeat(8-cells.length);
          racks.push(`<div class="machine-rack" role="group" aria-label="柜 ${rackOffset/8+1} · Rank ${cells[0].rank}–${cells.at(-1).rank}"><span class="rack-number" title="柜 ${rackOffset/8+1}">${String(rackOffset/8+1).padStart(2,'0')}</span><div class="matrix">${cells.map((cell,localIndex) => renderCell(cell,offset+localIndex)).join('')}${padding}</div></div>`);
        }
        groups.push(`<section class="matrix-group" aria-label="${esc(node.name)} 群 ${clusterOffset/512+1} 组 ${groupOffset/64+1}"><div class="matrix-group-heading"><b>组 ${String(groupOffset/64+1).padStart(2,'0')}</b><span>Rank ${groupCells[0].rank}–${groupCells.at(-1).rank}</span></div><div class="machine-racks">${racks.join('')}</div></section>`);
      }
      clusters.push(`<section class="machine-cluster" aria-label="${esc(node.name)} 群 ${clusterOffset/512+1}"><div class="cluster-heading"><b>群 ${String(clusterOffset/512+1).padStart(2,'0')}</b><span>Rank ${clusterCells[0].rank}–${clusterCells.at(-1).rank}</span></div><div class="matrix-groups">${groups.join('')}</div></section>`);
    }
    const matrix = master
      ? `<div class="master-machine"><div class="matrix master">${stage.cells.map(renderCell).join('')}</div><p class="master-caption">${esc(task.config.master_ip)}</p></div>`
      : `<div class="machine-clusters">${clusters.join('')}</div>`;
    const done = stage.cells.filter(cell => cell.status === 'success').length;
    return `<article class="stage-card ${stage.status} ${master ? 'master-stage' : ''}"><div class="stage-heading"><span class="stage-number">${ssh ? 'SSH' : String(index).padStart(2,'0')}</span><span class="scope-tag">${master ? 'MASTER' : 'ALL NODES'}</span><h3 title="${esc(node.name)}">${esc(node.name)}</h3><button class="stage-more" data-detail="${index}" aria-label="查看${esc(node.name)}详情">↗</button></div>${matrix}<div class="stage-footer"><span class="stage-status ${stage.status}">● ${names[stage.status]}${Number.isFinite(stage.elapsed_seconds) ? ` · ${stage.elapsed_seconds.toFixed(2)}s` : ''}</span><b>${done}/${stage.cells.length}</b></div></article>${!ssh && index < task.nodes.length-1 ? '<div class="stage-connector" aria-hidden="true">↓</div>' : ''}`;
}
function orderedMachines() {
  return [...task.machines].sort((a,b) => Number(a.Info !== task.config.master_ip) - Number(b.Info !== task.config.master_ip));
}
function visibleConnections() {
  if (!connections) return null;
  const byIp = new Map(connections.cells.map(cell => [cell.ip, cell]));
  const cells = orderedMachines().map((host,rank) => ({
    status:'pending',message:'尚未建立 SSH 会话',output:[],...byIp.get(host.Info),
    rank,ip:host.Info,machine:host.Machine
  }));
  const connected = cells.filter(cell => cell.status === 'success').length;
  const status = connections.busy ? 'running' : connected === cells.length ? 'success' :
    cells.some(cell => cell.status === 'failed') ? 'failed' : 'pending';
  return {...connections,cells,connected,total:cells.length,status};
}
function renderConnections() {
  const visible = visibleConnections();
  if (!visible) return;
  const count=visible.connected,total=visible.total,busy=visible.busy,running=run?.status==='running';
  $('connect-nodes').disabled=busy || count===total || running;
  $('disconnect-nodes').disabled=busy || count===0 || running;
  $('ssh-count').textContent=`${count}/${total} 已连接`;
  $('ssh-board').innerHTML=renderStageCard({name:'SSH 会话',scope:'all'},-1,visible,true);
}
function renderRuntime(force = false) {
  if (!task) return;
  const serialized = JSON.stringify([run,connections]);
  if (!force && serialized === lastRuntime) return;
  lastRuntime = serialized;
  const running = run?.status === 'running', busy = connections?.busy;
  $('overall-status').outerHTML = badge(run?.status).replace('class="badge', 'id="overall-status" class="badge');
  $('run-task').disabled = !!run?.terminal_busy; $('run-task').hidden = running; $('stop-task').hidden = !running;
  for (const id of ['save-task','copy-task','add-node','csv-select','csv-open','refresh-csv','task-name','master-ip','master-dir','machine-limit']) $(id).disabled = running || busy;
  $('delete-task').disabled=running || busy || !!run?.terminal_busy;
  $('pipeline-count').textContent = task.nodes.length;
  const boardScrollTop = $('pipeline-board').scrollTop;
  $('pipeline-board').innerHTML = task.nodes.map((node,index)=>renderStageCard(node,index,stageState(index))).join('');
  renderConnections();
  $('pipeline-board').scrollTop = boardScrollTop;
  $('node-rows').innerHTML = task.nodes.map((node,index) => {
    const stage = stageState(index), pairs = Object.entries(node.params);
    return `<tr><td><div class="reorder"><span>${String(index).padStart(2,'0')}</span><div><button data-move="${index}:-1" ${running||index===0?'disabled':''} title="上移">↑</button><button data-move="${index}:1" ${running||index===task.nodes.length-1?'disabled':''} title="下移">↓</button></div></div></td><td><button class="node-name" data-detail="${index}">${esc(node.name)}</button><small>${node.scope==='master'?'MASTER':'ALL NODES'}</small></td><td class="script-column"><span class="script-host">${esc(expand(node.script_ip))}</span><code title="${esc(expand(node.script_path))}">${esc(expand(node.script_path))}</code></td><td class="kv-column">${pairs.length ? `<details data-kv="${node.id}" ${expandedKV.has(node.id)?'open':''}><summary>${pairs.length} 个参数</summary>${pairs.map(([key,value]) => `<div class="kv-row"><b>${esc(key)}</b><span title="${esc(value)}">${esc(value)}</span></div>`).join('')}</details>` : '<span class="subtle">无参数</span>'}</td><td>${badge(stage.status)}</td><td class="row-actions"><button data-detail="${index}">详情</button><button data-edit="${index}" ${running?'disabled':''}>编辑</button><button data-clone="${index}" ${running?'disabled':''} title="复制节点">⧉</button><button data-delete="${index}" ${running?'disabled':''} title="删除节点">×</button></td></tr>`;
  }).join('');
  document.querySelectorAll('[data-kv]').forEach(details => details.addEventListener('toggle', () => details.open ? expandedKV.add(details.dataset.kv) : expandedKV.delete(details.dataset.kv)));
  renderConsole(force);
  renderSummary(); if (selectedDetail) renderDetail();
}
function renderConsole(force=false){
  const output=$('console-output'),atBottom=output.scrollHeight-output.scrollTop-output.clientHeight<45;
  output.textContent=consoleContext==='host' ? formatTerminal(run?.terminal || []) : formatTaskLogs(run?.console || [],run?.started_at);
  $('console-form').hidden=consoleContext!=='host';
  $('console-input').disabled=!!run?.terminal_busy;
  $('console-form').querySelector('button').disabled=!!run?.terminal_busy;
  if(atBottom || force)output.scrollTop=output.scrollHeight;
}
function visualizationTable(chart, rows = chart.table_rows || []) {
  if (!chart.show_table) return '';
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  const display = value => value === null || value === undefined ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  return `<div class="table-scroll visualization-data"><table aria-label="${esc(chart.title)} 数据表"><thead><tr>${columns.map(column=>`<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(column=>`<td>${esc(display(row[column]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function selectVisualization(id, focus = false) {
  activeVisualization = id;
  for (const [key, view] of visualizationViews) {
    view.panel.hidden = key !== id;
    const tab = $('visualization-gallery').querySelector(`[data-viz-tab="${key}"]`);
    tab.setAttribute('aria-selected',String(key === id));tab.tabIndex=key === id ? 0 : -1;
    if (key === id && focus) tab.focus();
  }
  const view = visualizationViews.get(id);
  if (view) void loadVisualization(view);
}
async function loadVisualization(view) {
  if (view.state === 'ready') {
    if (view.chart.kind) {
      try {await window.Plotly.Plots.resize(view.panel.querySelector('.interactive-chart'));}
      catch(error) {if(view.panel.isConnected && !view.panel.hidden)toast(error.message,true);}
    }
    return;
  }
  if (view.state === 'loading' || view.state === 'drawing') return;
  const content = view.panel.querySelector('.visualization-content');
  try {
    if (!view.rows) {
      view.state='loading';content.innerHTML='<p class="subtle">正在加载可视化数据…</p>';
      view.rows=await api(`/api/tasks/${task.id}/visualizations/${view.chart.id}/data`);
      view.state='data';
    }
    if (!view.panel.isConnected || view.panel.hidden) return;
    view.state='drawing';
    const chart=view.chart, axisChart=chart.kind && chart.kind !== 'pie';
    content.innerHTML=(chart.kind ? `<div class="visualization-tools" role="group" aria-label="图表操作"><button data-viz-action="pan" aria-pressed="true" ${axisChart?'':'disabled'}>平移</button><button data-viz-action="zoom" aria-pressed="false" ${axisChart?'':'disabled'}>框选缩放</button><button data-viz-action="reset" ${axisChart?'':'disabled'}>重置视图</button><button data-viz-action="download">下载 PNG</button></div><div class="interactive-chart" role="img" aria-label="${esc(chart.title)} 交互图表"></div>` : '')+visualizationTable(chart,view.rows);
    if (chart.kind) {
      const figure=buildFigure(chart,view.rows);
      const graph=content.querySelector('.interactive-chart');
      await window.Plotly.newPlot(graph,figure.data,figure.layout,chartConfig);
      if (!view.panel.isConnected) window.Plotly.purge(graph);
    }
    view.state='ready';
  } catch (error) {
    view.state='error';
    content.innerHTML=`<p class="visualization-error">${esc(error.message)}</p><button data-viz-action="retry">重试加载</button>`;
  }
}
function renderVisualizations(charts) {
  const gallery=$('visualization-gallery'), session=`${task.id}/${run?.id || 'draft'}`;
  if (session !== visualizationSession) {
    for (const view of visualizationViews.values()) {
      const graph=view.panel.querySelector('.interactive-chart');if(graph && window.Plotly)window.Plotly.purge(graph);
    }
    visualizationViews.clear();activeVisualization='';visualizationSession=session;
    gallery.innerHTML='<div class="visualization-tabs" role="tablist" aria-label="可视化结果页签"></div><div class="visualization-panels"></div>';
  }
  gallery.hidden=!charts.length;
  let added=false;
  const kinds={line:'折线图',box:'箱线图',bar:'柱状图',pie:'饼图',scatter:'散点图'};
  for (const chart of charts) {
    if (visualizationViews.has(chart.id)) continue;
    const panel=document.createElement('article');panel.className='visualization-card';panel.id=`viz-panel-${chart.id}`;
    panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',`viz-tab-${chart.id}`);panel.dataset.plotId=chart.id;panel.hidden=true;
    panel.innerHTML=`<div class="visualization-heading"><h3>${esc(chart.title)}</h3><span>${esc(chart.kind ? kinds[chart.kind] || chart.kind : '仅数据表')}${chart.kind && chart.show_table ? ' · 数据表' : ''} · ${esc(chart.row_count)} 条数据</span></div><div class="visualization-content"></div><p class="subtle">${esc(chart.node_name)} · Rank ${esc(chart.rank)} · ${esc(chart.ip)}</p><details><summary>数据来源</summary><code class="break">${esc(chart.source_path)}</code>${chart.kind ? `<p class="subtle">X：${esc(chart.x || '序号')} · Y：${esc(chart.y.join(', '))}</p>` : ''}</details>`;
    gallery.querySelector('.visualization-panels').append(panel);
    visualizationViews.set(chart.id,{chart,panel,state:'idle',rows:null});added=true;
  }
  if (added) gallery.querySelector('.visualization-tabs').innerHTML=charts.map(chart=>`<button role="tab" id="viz-tab-${chart.id}" data-viz-tab="${chart.id}" aria-controls="viz-panel-${chart.id}" title="${esc(chart.title)} · Rank ${esc(chart.rank)}">${esc(chart.title)}</button>`).join('');
  if (charts.length && (!activeVisualization || !visualizationViews.has(activeVisualization))) activeVisualization=charts[0].id;
  if (activeVisualization && added) selectVisualization(activeVisualization);
}
$('visualization-gallery').onclick=protect(async event=>{
  const tab=event.target.closest('[data-viz-tab]');if(tab)return selectVisualization(tab.dataset.vizTab);
  const button=event.target.closest('[data-viz-action]');if(!button)return;
  const view=visualizationViews.get(button.closest('[data-plot-id]').dataset.plotId);
  const action=button.dataset.vizAction;
  if(action==='retry')return loadVisualization(view);
  const graph=view.panel.querySelector('.interactive-chart');
  if(action==='download') {
    button.disabled=true;
    try {
      const filename=String(view.chart.title || 'chart').replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').slice(0,120);
      await window.Plotly.downloadImage(graph,{format:'png',filename,width:1400,height:700,scale:2});
    } finally {button.disabled=false;}
  } else {
    const mode=action==='zoom'?'zoom':'pan';
    await window.Plotly.relayout(graph,action==='reset'?{'xaxis.autorange':true,'yaxis.autorange':true,dragmode:mode}:{dragmode:mode});
    view.panel.querySelector('[data-viz-action="pan"]').setAttribute('aria-pressed',String(mode==='pan'));
    view.panel.querySelector('[data-viz-action="zoom"]').setAttribute('aria-pressed',String(mode==='zoom'));
  }
});
$('visualization-gallery').onkeydown=event=>{
  const tab=event.target.closest('[data-viz-tab]');if(!tab || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const ids=[...visualizationViews.keys()],index=ids.indexOf(tab.dataset.vizTab);
  const next=event.key==='Home'?0:event.key==='End'?ids.length-1:(index+(event.key==='ArrowRight'?1:-1)+ids.length)%ids.length;
  selectVisualization(ids[next],true);
};
function renderSummary() {
  const charts = run?.visualizations || [];
  renderVisualizations(charts);
  const summary = run?.summary;
  const showLegacy = !!summary && !(summary.kind === 'node-metrics' && (charts.length || summary.visualization_api === 2));
  $('summary-content').hidden = !showLegacy;
  $('summary-policy').textContent = charts.length ? `${charts.length} 项结果` : '';
  if (!showLegacy) return;
  if(summary.kind==='node-metrics') {
    $('metric-chart').hidden=false;
    $('summary-policy').textContent=summary.metric+' · master 汇总分析';
    $('summary-metrics').innerHTML=''; document.querySelector('.chart-header').hidden=true;
    $('metric-chart').innerHTML=`<img src="/api/tasks/${task.id}/report?run=${run.id}" alt="各机器 CPU 一分钟平均负载">`;
    $('summary-content').querySelector('summary').textContent='查看各机器采样数据';
    $('summary-table').innerHTML='<table><thead><tr><th>机器 / Rank</th><th>IP</th><th>CPU load 1m</th><th>延时 / s</th><th>采样时间 / UTC</th></tr></thead><tbody>'+summary.rows.map(row=>`<tr><td>${esc(row.machine)} / ${esc(row.rank)}</td><td>${esc(row.ip)}</td><td>${esc(row.value)}</td><td>${esc(row.delay_seconds)}</td><td>${esc(new Date(row.epoch*1000).toISOString())}</td></tr>`).join('')+'</tbody></table>';
    return;
  }
  document.querySelector('.chart-header').hidden=!!charts.length;
  $('metric-chart').hidden=!!charts.length;
  $('summary-content').querySelector('summary').textContent='查看 warmup 后逐步指标';

  $('summary-policy').textContent = `排除前 ${summary.warmup} steps · ${summary.count} 个有效 steps`;
  $('summary-metrics').innerHTML = [['平均 Step time',(summary.mean_seconds*1000).toFixed(1),'ms','同 step 跨机器最大值'],['平均 Loss',summary.mean_loss?.toFixed(4) ?? '—','','Master 日志'],['平均 Grad norm',summary.mean_grad_norm?.toFixed(4) ?? '—','','Master 日志'],['有效步骤',summary.count,'steps',`warmup > ${summary.warmup}`]].map(([label,value,unit,note]) => `<div class="metric"><span>${label}</span><strong>${value}<small>${unit}</small></strong><p>${note}</p></div>`).join('');
  const rows = summary.rows.filter(row => Number.isFinite(row[chartKey])), width=1000, height=155, pad=30;
  if (rows.length) {
    const values=rows.map(row=>row[chartKey]), min=Math.min(...values), max=Math.max(...values), span=max-min || Math.abs(max)*.1 || 1;
    const points=rows.map((row,index)=>`${pad+(width-pad*2)*index/Math.max(1,rows.length-1)},${height-pad-(row[chartKey]-min)/span*(height-pad*2)}`).join(' ');
    $('metric-chart').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${chartKey} 指标曲线"><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#7774ed" stop-opacity=".18"/><stop offset="1" stop-color="#7774ed" stop-opacity="0"/></linearGradient></defs>${[0,1,2].map(i=>`<line x1="${pad}" x2="${width-pad}" y1="${pad+i*(height-pad*2)/2}" y2="${pad+i*(height-pad*2)/2}" stroke="#edf0f6"/>`).join('')}<polygon points="${pad},${height-pad} ${points} ${width-pad},${height-pad}" fill="url(#chart-fill)"/><polyline points="${points}" fill="none" stroke="#6963d9" stroke-width="2.5" stroke-linejoin="round"/><text x="${pad}" y="${height-6}">step ${rows[0].step}</text><text x="${width-pad}" y="${height-6}" text-anchor="end">step ${rows.at(-1).step}</text></svg>`;
  } else $('metric-chart').textContent='没有该指标数据';
  $('chart-note').textContent = summary.policy;
  $('summary-table').innerHTML='<table><thead><tr><th>Step</th><th>Time / s</th><th>Loss</th><th>Grad norm</th></tr></thead><tbody>'+summary.rows.map(row=>`<tr><td>${row.step}</td><td>${row.seconds.toFixed(4)}</td><td>${row.loss?.toFixed(6)??'—'}</td><td>${row.grad_norm?.toFixed(4)??'—'}</td></tr>`).join('')+'</tbody></table>';
}
function showDialog(title, content, onSave, button='确定') {
  $('dialog-title').textContent=title; $('dialog-content').innerHTML=content;
  $('dialog-actions').innerHTML='<button type="button" id="dialog-cancel">取消</button><button type="button" id="dialog-save" class="primary">'+esc(button)+'</button>';
  $('dialog-save').onclick=protect(async()=>{ await onSave(); $('dialog').close(); });
  $('dialog-cancel').onclick=()=>$('dialog').close();
  $('dialog').querySelector('form').onsubmit=event=>{event.preventDefault();$('dialog-save').click();};
  $('dialog').showModal();
}
function newTask(templateId='training') {
  const choices=creationTemplates();
  if(!choices.some(item=>item.id===templateId))templateId=choices[0]?.id;
  const stamp=new Date().toISOString().replace(/[-:]/g,'').replace(/\.\d{3}Z$/,'Z');
  let suggestedName=generatedTaskName(templateId,stamp);
  showDialog('新建多机任务', `<div class="template-grid">${choices.map(item=>`<button type="button" class="template-card ${item.id===templateId?'chosen':''}" data-choice="${item.id}">${item.icon ? `<span>${esc(item.icon)}</span>` : ''}<strong>${esc(item.name)}</strong><small>${esc(item.description)}</small></button>`).join('')}</div><div class="dialog-form"><label>任务名称<input id="new-name" value="${esc(suggestedName)}"></label><label>机器数<input id="new-count" type="text" inputmode="numeric" pattern="[1-9][0-9]*" value="8" required></label><label class="full">Master 工作目录<input id="new-master-dir" value="${esc(creationDefaults.master_dir)}" required spellcheck="false"></label></div>`,async()=>{
    const countText=$('new-count').value.trim(); if(!/^[1-9][0-9]*$/.test(countText)||!Number.isSafeInteger(Number(countText))) throw new Error('机器数必须为正整数');
    const masterDir=$('new-master-dir').value.trim();
    if(!masterDir.startsWith('/')) throw new Error('Master 工作目录必须为远程绝对路径');
    if(dirty) await saveTask(false);
    const created=await api('/api/tasks',{template:templateId,name:$('new-name').value,count:Number(countText),paths:{master_dir:masterDir}});
    dirty=false; await refreshLibrary(); await selectTask(created.id); toast('任务已创建，请在机器 CSV 中填写实际地址');
  },'创建任务');
  document.querySelectorAll('[data-choice]').forEach(button=>button.onclick=()=>{
    const input=$('new-name'),useSuggested=input.value===suggestedName || !input.value.trim();
    templateId=button.dataset.choice;suggestedName=generatedTaskName(templateId,stamp);
    if(useSuggested)input.value=suggestedName;
    document.querySelectorAll('[data-choice]').forEach(item=>item.classList.toggle('chosen',item===button));
  });
}
function editNode(index) {
  const node=index===null?{id:crypto.randomUUID().replaceAll('-',''),name:'自定义节点',operation:'custom',scope:'all',language:'sh',script_ip:'${MASTER_IP}',script_path:'${MASTER_DIR}/pipeline_scripts/custom.sh',params:{},script_content:'#!/usr/bin/env bash\nset -eo pipefail\necho "custom task"\n'}:structuredClone(task.nodes[index]);
  showDialog(index===null?'添加 Pipeline 节点':'编辑 Pipeline 节点', `<div class="dialog-form"><label>节点名称<input id="edit-name" value="${esc(node.name)}"></label><label>执行范围<select id="edit-scope"><option value="master" ${node.scope==='master'?'selected':''}>MASTER</option><option value="all" ${node.scope==='all'?'selected':''}>ALL NODES</option></select></label><label>功能类型<select id="edit-operation">${Object.entries(operations).map(([key,name])=>`<option value="${key}" ${node.operation===key?'selected':''}>${esc(name)}</option>`).join('')}</select></label><label>脚本类型<select id="edit-language"><option value="sh" ${node.language==='sh'?'selected':''}>Shell (.sh)</option><option value="python" ${node.language==='python'?'selected':''}>Python (.py)</option></select></label><label>脚本所在 IP<input id="edit-ip" value="${esc(node.script_ip)}"></label><label>脚本路径<input id="edit-path" value="${esc(node.script_path)}"></label><label class="full">Key-values <small>每行 KEY=value；值可包含 =。支持 &#36;{MASTER_IP}、&#36;{MASTER_DIR}、&#36;{WARMUP_STEPS}、&#36;{TASK_DIR}、&#36;{RUN_ID}。</small><small id="image-path-help" ${node.operation==='distribute'?'':'hidden'}>IMAGE_PATH 为 Master 镜像路径，IMAGE_DEST 为各机镜像目标路径。</small><textarea id="edit-params" rows="4" spellcheck="false">${esc(Object.entries(node.params).map(([key,value])=>key+'='+value).join('\n'))}</textarea></label><label class="full">脚本内容 <span class="subtle">运行任务时执行此脚本</span><input id="script-file" type="file" accept=".sh,.py"><textarea id="edit-content" rows="8" spellcheck="false">${esc(node.script_content)}</textarea></label></div>`,async()=>{
    const params={}; for(const line of $('edit-params').value.split('\n').filter(line=>line.trim())) {const at=line.indexOf('=');const key=line.slice(0,at).trim();if(at<1||!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)||Object.hasOwn(params,key)) throw new Error('参数需为唯一的 KEY=value，每行一个');params[key]=line.slice(at+1);}
    Object.assign(node,{name:$('edit-name').value.trim(),scope:$('edit-scope').value,operation:$('edit-operation').value,language:$('edit-language').value,script_ip:$('edit-ip').value.trim(),script_path:$('edit-path').value.trim(),params,script_content:$('edit-content').value});
    if(!node.name||!node.script_ip||!node.script_path) throw new Error('名称、IP 和脚本路径不能为空');
    if(index===null) task.nodes.push(node); else task.nodes[index]=node;
    run=null; markDirty(); renderRuntime(true);
  },'应用到任务');
  $('edit-operation').onchange=()=>{ $('image-path-help').hidden=$('edit-operation').value!=='distribute'; };
  $('script-file').onchange=protect(async()=>{const file=$('script-file').files[0]; if(!file)return; if(file.size>200000)throw new Error('脚本文件请小于200KB');$('edit-content').value=await file.text();$('edit-language').value=file.name.endsWith('.py')?'python':'sh';});
}
function openDetail(index, cellIndex=null) { selectedDetail={index,cellIndex};$('detail-drawer').hidden=false;$('drawer-backdrop').hidden=false;renderDetail(); }
function closeDrawer(){selectedDetail=null;$('detail-drawer').hidden=true;$('drawer-backdrop').hidden=true;}
function renderDetail(){
  const {index,cellIndex}=selectedDetail;
  if(index===-1){
    const visible=visibleConnections();
    const cell=cellIndex===null?null:visible?.cells[cellIndex];$('detail-title').textContent='SSH 连接状态';
    $('detail-content').innerHTML=cell ? `<div class="inspector-status"><span class="badge ${cell.status}">${connectionLabels[cell.status]}</span></div><div class="detail-facts"><div><label>机器 / Rank</label><b>${esc(cell.machine)} / ${cell.rank}</b></div><div><label>IP</label><code>${esc(cell.ip)}</code></div><div class="full"><label>状态说明</label><p>${esc(cell.message)}</p></div></div>` : `<p>${visible.connected}/${visible.total} 已连接 · 接收 SSH 连接退出事件</p>`;
    return;
  }
  const node=task.nodes[index],stage=stageState(index);if(!node)return closeDrawer();
  const cell=cellIndex===null?null:stage.cells[cellIndex]; $('detail-title').textContent=node.name;
  const facts=cell ? `<div class="detail-facts"><div><label>机器 / Rank</label><b>${esc(cell.machine)} / ${cell.rank}</b></div><div><label>IP</label><code>${esc(cell.ip)}</code></div><div class="full"><label>状态说明</label><p>${esc(cell.message)}</p></div>${cell.command?`<div class="full"><label>执行命令</label><code>${esc(cell.command)}</code></div>`:''}</div>` : '';
  const params=Object.entries(node.params).map(([key,value])=>`<div><b>${esc(key)}</b><code>${esc(expand(value))}</code></div>`).join('')||'<p class="subtle">无参数</p>';
  const rounds=stage.rounds.map((edges,number)=>{
    const paths=edges.map(edge=>`<code>${esc(edge.source)} → ${esc(edge.target)}</code>`).join('');
    return `<details ${number===0?'open':''}><summary>第 ${number+1} 轮 · ${edges.length} 条路径</summary><div class="edge-list">${paths}</div></details>`;
  }).join('');
  const artifacts=run?.artifacts?.length ? `<h3>本地结果文件</h3><p class="subtle break">${esc(run.artifact_dir)}</p><div class="edge-list">${run.artifacts.slice(0,30).map(path=>`<code>${esc(path)}</code>`).join('')}${run.artifacts.length>30?'<p>更多文件请打开任务目录查看。</p>':''}</div>` : '';
  $('detail-content').innerHTML=`<div class="inspector-status">${badge(cell?.status||stage.status)}<span>${node.scope==='master'?'MASTER':'ALL NODES'} · ${stage.cells.length} 台</span></div>${facts}${stage.source_image?`<h3>Master 镜像 SHA256 · 自动计算</h3><div class="detail-facts"><div class="full"><label>源镜像</label><code>${esc(stage.source_image.ip)}:${esc(stage.source_image.path)}</code></div><div class="full"><label>本次分发基准</label><code>${esc(stage.source_image.sha256)}</code></div></div>`:""}${cell?.output?.length?`<h3>该机器输出</h3><pre>${esc(cell.output.join("\n"))}</pre>`:""}<h3>脚本定义</h3><div class="detail-facts"><div class="full"><label>来源 IP</label><code>${esc(expand(node.script_ip))}</code></div><div class="full"><label>远程路径</label><code>${esc(expand(node.script_path))}</code></div></div><h3>Key-values</h3><div class="detail-kvs">${params}</div><details><summary>查看脚本内容</summary><pre>${esc(node.script_content)}</pre></details>${rounds?`<h3>倍增分发计划 · ${stage.rounds.length} 轮</h3>${rounds}`:''}<h3>节点事件</h3><pre class="event-log">${esc(formatTaskLogs(stage.events,run?.started_at)||'等待运行。')}</pre>${artifacts}`;
}
async function startRun(){ if(!connections?.busy)await saveTask(false);else if(dirty)throw new Error('请等待连接完成后保存配置'); run=await api(`/api/tasks/${task.id}/run`,{});renderRuntime(true);toast('任务已启动，连接就绪后自动执行 pipeline'); }
async function consoleSend(command){ if(!command.trim())return;await api(`/api/tasks/${task.id}/console`,{command,context:consoleContext});history.push(command);historyIndex=history.length;$('console-input').value='';run=await api(`/api/tasks/${task.id}/run`);renderRuntime(true); }

$('new-task').onclick=()=>newTask();
$('task-search').oninput=renderLibrary;
$('task-list').onclick=protect(async event=>{const button=event.target.closest('[data-task]');if(button)await selectTask(button.dataset.task);});
$('template-list').onclick=event=>{const button=event.target.closest('[data-template]');if(button)newTask(button.dataset.template);};
const openTask=protect(async()=>{if(dirty&&!confirm('放弃未保存修改并导入任务？'))return;const opened=await api('/api/open',{});if(opened){dirty=false;await refreshLibrary();await selectTask(opened.id);}});
$('open-task').onclick=openTask;$('open-file').onclick=openTask;
$('save-task').onclick=protect(()=>saveTask());
$('delete-task').onclick=()=>{
  if(!task)return;
  const id=task.id;
  showDialog('删除任务',`<p>确认删除任务“${esc(task.name)}”？</p><p class="subtle">本地任务及记录将移入 data/.trash，可通过“导入任务”恢复。导入的原始文件会保留。</p>`,async()=>{
    const result=await api(`/api/tasks/${id}/delete`,{});
    clearTask();await refreshLibrary();
    if(library.length)await selectTask(library[0].id);
    toast(result.source_preserved?'任务已从任务库删除，原始文件保留':'任务已移入本地回收目录');
  },'删除任务');
  $('dialog-save').className='delete-task';
};
$('copy-task').onclick=protect(async()=>{await saveTask(false);const copy=await api(`/api/tasks/${task.id}/copy`,{});await refreshLibrary();await selectTask(copy.id);toast('任务和机器 CSV 已独立复制');});
$('run-task').onclick=protect(startRun);
$('connect-nodes').onclick=protect(async()=>{await saveTask(false);connections=await api(`/api/tasks/${task.id}/connect`,{});renderRuntime(true);toast('正在建立持久 SSH 会话');});
$('disconnect-nodes').onclick=protect(async()=>{connections=await api(`/api/tasks/${task.id}/disconnect`,{});renderRuntime(true);toast('SSH 会话已关闭');});
$('stop-task').onclick=protect(async()=>{await api(`/api/tasks/${task.id}/stop`,{});toast('正在停止，后续节点不再启动');});
$('csv-open').onclick=protect(()=>api(`/api/tasks/${task.id}/csv-open`,{}));
$('csv-select').onclick=protect(async()=>{await saveTask(false);const selected=await api(`/api/tasks/${task.id}/csv-select`,{});if(selected){task=selected;run=null;renderTask();toast('CSV 已关联；Pwd 不会传给页面');}});
$('refresh-csv').onclick=protect(async()=>{if(dirty&&!confirm('重新读取会放弃尚未保存的配置修改，继续？'))return;task=await api(`/api/tasks/${task.id}`);run=null;dirty=false;renderTask();toast('机器清单已重新读取；若 master 已移除，请重新选择');});
$('add-node').onclick=()=>editNode(null);
$('node-rows').onclick=event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.detail!==undefined)return openDetail(Number(button.dataset.detail));
  if(button.dataset.edit!==undefined)return editNode(Number(button.dataset.edit));
  if(button.dataset.move){const [index,offset]=button.dataset.move.split(':').map(Number);[task.nodes[index],task.nodes[index+offset]]=[task.nodes[index+offset],task.nodes[index]];}
  if(button.dataset.clone!==undefined){const index=Number(button.dataset.clone),node=structuredClone(task.nodes[index]);node.id=crypto.randomUUID().replaceAll('-','');node.name+=' · 副本';task.nodes.splice(index+1,0,node);}
  if(button.dataset.delete!==undefined){if(task.nodes.length===1)return toast('至少保留一个节点',true);task.nodes.splice(Number(button.dataset.delete),1);}
  run=null;markDirty();renderRuntime(true);
};
$('ssh-board').onclick=$('pipeline-board').onclick=event=>{const cell=event.target.closest('[data-cell]'),detail=event.target.closest('[data-detail]');if(cell){const [index,cellIndex]=cell.dataset.cell.split(':').map(Number);openDetail(index,cellIndex);}else if(detail)openDetail(Number(detail.dataset.detail));};
$('expand-board').onclick=()=>{const expanded=document.querySelector('.pipeline-section').classList.toggle('expanded');$('expand-board').textContent=expanded?'⛶ 退出展开':'⛶ 展开';};
$('drawer-backdrop').onclick=closeDrawer;$('close-drawer').onclick=closeDrawer;
$('open-folder').onclick=protect(()=>api(`/api/tasks/${task.id}/folder-open`,{}));
$('console-form').onsubmit=protect(async event=>{event.preventDefault();await consoleSend($('console-input').value);});
$('console-input').onkeydown=event=>{if(event.key==='ArrowUp'||event.key==='ArrowDown'){event.preventDefault();historyIndex=Math.max(0,Math.min(history.length,historyIndex+(event.key==='ArrowUp'?-1:1)));event.target.value=history[historyIndex]||'';}};
$('clear-console').onclick=protect(()=>consoleSend('clear'));
$('toggle-console').onclick=()=>{const hidden=$('console-body').hidden=!$('console-body').hidden;$('toggle-console').textContent=hidden?'展开':'收起';};
document.querySelectorAll('[data-console]').forEach(button=>button.onclick=()=>{consoleContext=button.dataset.console;document.querySelectorAll('[data-console]').forEach(item=>item.classList.toggle('active',item===button));renderConsole(true);});
document.querySelectorAll('[data-chart]').forEach(button=>button.onclick=()=>{chartKey=button.dataset.chart;document.querySelectorAll('[data-chart]').forEach(item=>item.classList.toggle('active',item===button));renderSummary();});
document.querySelectorAll('.configuration input,#task-name').forEach(input=>input.addEventListener('input',markDirty));
$('machine-limit').onchange=protect(applyMachineCount);
$('machine-limit').onkeydown=event=>{
  if(event.key==='Enter') event.currentTarget.blur();
};
$('master-ip').onchange=()=>{
  task.config.master_ip=$('master-ip').value;
  run=null; closeDrawer(); markDirty(); renderMachineFields(); renderRuntime(true);
};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='s'){event.preventDefault();if(task&&run?.status!=='running')protect(()=>saveTask())();}if(event.key==='Escape')closeDrawer();if(event.key.toLowerCase()==='n'&&!event.ctrlKey&&!event.metaKey&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&!$('dialog').open)newTask();});
async function poll(){
  if(polling||!task)return;polling=true;const id=task.id;
  try{
    const [state,ssh]=await Promise.all([api(`/api/tasks/${id}/run`),api(`/api/tasks/${id}/connections`)]);
    if(task?.id===id){
      connections=ssh;
      const previous=Math.min(connectionEvents.get(id) || 0,ssh.events.at(-1)?.id || 0), fresh=ssh.events.filter(event=>event.id>previous);
      if(!ssh.events.length)connectionEvents.set(id,0);
      if(fresh.length){toast(fresh.map(event=>event.message).join('；'),true);connectionEvents.set(id,fresh.at(-1).id);}
      if(!dirty){run=state;renderRuntime();}else{renderConnections();if(selectedDetail?.index===-1)renderDetail();}
    }
  }catch(error){$('saved-state').textContent='本地服务连接中断';}finally{polling=false;}
}
await protect(async()=>{await refreshLibrary();if(library.length)await selectTask(library[0].id);else clearTask();})();
setInterval(poll,650);

$('dialog-close').onclick=()=>$('dialog').close();
