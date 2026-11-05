import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const functions=source.slice(source.indexOf('function renderStageCard('),source.indexOf('function renderRuntime('));
function view(count,total=8,busy=false,running=false){
  const elements=Object.fromEntries(['connect-nodes','disconnect-nodes','ssh-count','ssh-board'].map(id=>[id,{}]));
  const cells=Array.from({length:total},(_,i)=>({rank:i,ip:`192.0.2.${i+1}`,machine:`n${i}`,status:i<count?'success':'pending'}));
  const context={connections:{connected:count,total,busy,cells,status:busy?'running':count===total?'success':'pending'},
    run:running?{status:'running'}:null,task:{nodes:[],config:{},machines:cells.map(cell=>({Info:cell.ip,Machine:cell.machine}))},
    labels:{},connectionLabels:{success:'已连接',pending:'未连接',running:'连接中',failed:'连接异常'},
    esc:value=>String(value),$:id=>elements[id]};
  vm.runInNewContext(functions+';renderConnections();',context);return elements;
}

test('all disconnected enables connect and disables disconnect',()=>{
  const elements=view(0);assert.equal(elements['connect-nodes'].disabled,false);
  assert.equal(elements['disconnect-nodes'].disabled,true);assert.equal(elements['ssh-count'].textContent,'0/8 已连接');
});
test('all connected disables connect and enables disconnect',()=>{
  const elements=view(8);assert.equal(elements['connect-nodes'].disabled,true);
  assert.equal(elements['disconnect-nodes'].disabled,false);
});
test('partial connection enables both actions; in-progress operations prevent conflicting changes',()=>{
  const elements=view(3);assert.equal(elements['connect-nodes'].disabled,false);assert.equal(elements['disconnect-nodes'].disabled,false);
  for(const states of [[true,false],[false,true]]){
    const busy=view(3,8,...states);assert.equal(busy['connect-nodes'].disabled,true);assert.equal(busy['disconnect-nodes'].disabled,true);
  }
});
test('SSH board uses the same 512-node grouping and exact clickable rank indexes',()=>{
  const html=view(200,512)['ssh-board'].innerHTML;
  assert.equal((html.match(/class="matrix-cell /g)||[]).length,512);
  assert.equal((html.match(/class="machine-rack"/g)||[]).length,64);
  assert.equal((html.match(/class="matrix-group"/g)||[]).length,8);
  assert.equal((html.match(/class="machine-cluster"/g)||[]).length,1);
  assert.ok(html.includes('data-cell="-1:511"'));assert.ok(html.includes('Rank 0–511'));
  assert.ok(html.includes('已连接'));assert.ok(!html.includes('stage-connector'));
});

test('shrinking the CSV range clears an excluded Master and narrows both selectors and SSH cells',()=>{
  const fields=Object.fromEntries(['machine-limit','master-ip','machine-count','console-caption'].map(id=>[id,{}]));
  fields['machine-limit'].value='2';
  fields['master-ip'].value='192.0.2.3';
  const csv_machines=[1,2,3].map(i=>({Info:`192.0.2.${i}`,Machine:`n${i}`}));
  const task={machine_count:3,csv_machines,machines:csv_machines,config:{master_ip:'192.0.2.3'}};
  const notices=[];
  const context={task,run:{status:'success'},connections:{cells:csv_machines.map(host=>({ip:host.Info,status:'success'}))},
    $:id=>fields[id],esc:String,closeDrawer:()=>{},markDirty:()=>{},renderRuntime:()=>{},toast:message=>notices.push(message)};
  const edit=source.slice(source.indexOf('function renderMachineFields('),source.indexOf('function readForm('));
  vm.runInNewContext(functions+edit+';applyMachineCount();',context);
  assert.equal(task.machine_count,2);
  assert.equal(task.config.master_ip,'');
  assert.equal(fields['master-ip'].value,'');
  assert.ok(fields['master-ip'].innerHTML.includes('请选择 Master IP'));
  assert.ok(!fields['master-ip'].innerHTML.includes('192.0.2.3'));
  assert.match(notices[0],/重新选择 Master IP/);
  assert.equal(vm.runInNewContext('visibleConnections().total',context),2);
  fields['machine-limit'].value='3';
  vm.runInNewContext('applyMachineCount();',context);
  assert.equal(task.machines.length,3);
  assert.equal(task.config.master_ip,'');
});
