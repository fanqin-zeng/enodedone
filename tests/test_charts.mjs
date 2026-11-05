import test from 'node:test';
import assert from 'node:assert/strict';
import {buildFigure,chartConfig} from '../web/charts.mjs';

const chart={id:'chart',title:'Training loss',kind:'line',x:'step',y:['loss']};
test('line data keeps real step spacing, zeros and missing points',()=>{
  const figure=buildFigure(chart,[{step:4,loss:'0'},{step:7,loss:null},{step:12,loss:2.5}]);
  assert.deepEqual(figure.data[0].x,[4,7,12]);
  assert.deepEqual(figure.data[0].y,[0,null,2.5]);
  assert.equal(figure.data[0].connectgaps,false);
  assert.equal(figure.layout.uirevision,'chart');
  assert.equal(figure.layout.dragmode,'pan');
  assert.equal(chartConfig.scrollZoom,true);
});
test('multiple y columns become separate named traces',()=>{
  const figure=buildFigure({...chart,y:['loss','grad_norm']},[{step:4,loss:2,grad_norm:3}]);
  assert.deepEqual(figure.data.map(trace=>trace.name),['loss','grad_norm']);
  assert.deepEqual(figure.data.map(trace=>trace.y),[[2],[3]]);
});
test('bar, scatter, box and pie retain their native interactive chart types',()=>{
  const rows=[{step:'A',loss:2},{step:'B',loss:4}];
  assert.equal(buildFigure({...chart,kind:'bar'},rows).data[0].type,'bar');
  assert.equal(buildFigure({...chart,kind:'scatter'},rows).data[0].mode,'markers');
  const box=buildFigure({...chart,kind:'box'},rows).data[0];
  assert.equal(box.type,'box');assert.equal(box.quartilemethod,'linear');assert.deepEqual(box.y,[2,4]);
  const pie=buildFigure({...chart,kind:'pie'},rows).data[0];
  assert.deepEqual(pie.labels,['A','B']);assert.deepEqual(pie.values,[2,4]);
  assert.match(pie.hovertemplate,/%\{percent\}/);
});
test('table-only results do not construct charts; invalid numbers are rejected',()=>{
  assert.equal(buildFigure({...chart,kind:null},[{note:'text only'}]),null);
  for(const loss of [NaN,Infinity,true,'invalid'])assert.throws(()=>buildFigure(chart,[{step:4,loss}]),/无效数值/);
});
test('text is escaped before entering Plotly rich-text fields',()=>{
  const figure=buildFigure({...chart,title:'<b>Title</b>',x:'machine',y:['value']},[{machine:'<b>A</b>',value:2}]);
  assert.equal(figure.layout.title.text,'&lt;b&gt;Title&lt;/b&gt;');
  assert.equal(figure.data[0].x[0],'&lt;b&gt;A&lt;/b&gt;');
});
