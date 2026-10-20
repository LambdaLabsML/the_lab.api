import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseDashboardUrl as parse, formatDashboardUrl as format, defaultOverview, DEFAULT_CONTROLS, MAX_INLINE_URL_LENGTH } from '../dashboard/src/lib/dashboard-url.ts';

test('human readable paths run from general section to specific message or viewer', () => {
 for (const path of ['/messages/threads/42/messages/99', '/overview/ideas/123/experiments/123.4/output', '/queue/ideas/123/experiments/123.4/log', '/database/graph/ideas/123', '/settings/agents', '/artifacts/reports/run%201.md']) {
  assert.equal(format(parse(path)), path);
 }
});
test('independent graph controls, dimensions, filters and highlights round trip', () => {
 const view=defaultOverview();view.metric='accuracy';view.main.chartXAxis='time';view.main.improvementsOnly=false;
 view.filter='long';view.where='MAX_WALL_SECONDS >= 9000';view.tags=['baseline','canary'];view.highlights=[{category:'idea',value:'123',label:'#123'},{category:'experiment',value:'124',label:'exp/124.1'}];
 view.plots=[{id:'one',type:'metrics',metric:'loss',height:420,width:'half',controls:{...DEFAULT_CONTROLS,logScale:true,plotFilter:'train;eval & score'}},{id:'two',type:'scatter',x:'train/loss',y:'a,b:c;d',controls:{...DEFAULT_CONTROLS,ideaMean:true}}];
 const url=format({route:{section:'review'},overview:view});const restored=parse(url);
 assert.equal(restored.overview.main.improvementsOnly,false);assert.equal(restored.overview.plots[0].controls.logScale,true);
 assert.equal(restored.overview.plots[1].y,'a,b:c;d');assert.equal(restored.overview.plots[1].x,'train/loss');
 assert.equal(restored.overview.plots[0].controls.plotFilter,'train;eval & score');assert.equal(format(restored),url);
 assert.ok(url.length>MAX_INLINE_URL_LENGTH,'complex cases must use the saved-view fallback');
});
test('a minimal URL has stable defaults and can explicitly remove extra graphs', () => {
 assert.equal(format(parse('/overview')),'/overview');
 const state=parse('/overview?metric=loss&graph=none');assert.equal(state.overview.plots.length,0);assert.equal(format(state),'/overview?metric=loss&graph=none');
});
test('thread audience and inbox filters coexist with idea details', () => {
 const path='/messages/threads/42/messages/99/ideas/123/experiments/123.4/log?inbox=mine&audience=me&q=cache&status=closed';
 assert.equal(format(parse(path)),path);
});
test('legacy idea, hash-thread and lightbox links migrate', () => {
 const a=parse('/ideas/123?m=accuracy&e=123.4&view=output/123.4#review');assert.equal(a.route.idea,123);assert.equal(a.route.view,'output');assert.equal(a.overview.metric,'accuracy');
 assert.match(format(a),/^\/overview\/ideas\/123\/experiments\/123.4\/output/);
 assert.equal(format(parse('/#messages/42/99')),'/messages/threads/42/messages/99');
 assert.equal(parse('/overview/views/accuracy-3-graphs-abcd').viewRef,'accuracy-3-graphs-abcd');
});
