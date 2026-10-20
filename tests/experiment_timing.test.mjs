import {test} from 'node:test';
import assert from 'node:assert/strict';
import {jobStart,jobRuntimeSeconds,formatRunDuration} from '../dashboard/src/lib/experiment-timing.ts';
test('live job runtime advances from actual start and freezes at completion',()=>{
 const exp={status:'running',started_at:'2026-01-01T00:00:00Z',attempt_started_at:'2026-01-01T01:00:00Z',scheduler_state:'RUNNING'};
 assert.equal(jobRuntimeSeconds(exp,Date.parse('2026-01-01T01:01:00Z')),60);
 assert.equal(jobRuntimeSeconds(exp,Date.parse('2026-01-01T01:01:02Z')),62);
 assert.equal(jobRuntimeSeconds({...exp,status:'completed',runtime_seconds:65},Date.now()),65);
 assert.equal(jobRuntimeSeconds({...exp,status:'completed',finished_at:'2026-01-01T01:02:00Z'}),120);
 assert.equal(formatRunDuration(3662),'1h 01m 02s');
});
test('pending Slurm jobs and missing allocation starts never use submission time',()=>{
 assert.equal(jobStart({status:'running',started_at:'2026-01-01T00:00:00Z',scheduler_state:'PENDING'}),null);
 assert.equal(jobStart({status:'running',started_at:'2026-01-01T00:00:00Z',scheduler_state:'RUNNING'}),null);
 assert.equal(jobRuntimeSeconds({status:'queued',started_at:'2026-01-01T00:00:00Z'}),null);
 assert.equal(jobStart({status:'running',started_at:'2026-01-01T00:00:00Z'}),'2026-01-01T00:00:00Z');
});
