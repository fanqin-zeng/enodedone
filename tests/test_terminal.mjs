import {test} from 'node:test';
import assert from 'node:assert/strict';
import {formatTerminal,formatTaskLogs} from '../web/terminal.mjs';

const time='2026-10-31T17:48:13+00:00';
test('local timestamps keep the time only and ls retains its column spacing',()=>{
  const output=formatTerminal([{time,text:'$ ls\n'},{time,text:'data  pipeline_scripts  runs\r\n'}],'Asia/Shanghai');
  assert.equal(output,'[01:48:13] $ ls\n[01:48:13] data  pipeline_scripts  runs\n');
  assert.ok(!output.includes('UTC'));
});
test('split chunks and split CRLF do not create extra timestamps or lines',()=>{
  assert.equal(formatTerminal([{time,text:'data  pipe'},{time,text:'line_scripts  runs\r'},{time,text:'\n'}],'UTC'),
    '[17:48:13] data  pipeline_scripts  runs\n');
});
test('blank lines, tabs, carriage returns and Unicode are preserved as terminal text',()=>{
  assert.equal(formatTerminal([{time,text:'\n甲\t乙\nabc\rxy\n'}],'UTC'),'\n[17:48:13] 甲\t乙\n[17:48:13] xyc\n');
});
test('pipeline timestamps display local time for old and new stored log formats',()=>{
  assert.equal(formatTaskLogs(['[17:48:13 UTC] old','[2026-10-31T17:48:13+00:00] new'],time,'Asia/Shanghai'),
    '[01:48:13] old\n[01:48:13] new');
});
