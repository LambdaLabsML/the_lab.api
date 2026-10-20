"""Exercise the real notification middleware without polluting module lab state."""
import subprocess
import sys
import unittest


class NotificationBoundsTests(unittest.TestCase):
    def test_anonymous_history_and_agent_batches_are_bounded(self):
        script = r'''
import json, os
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
with TemporaryDirectory() as tmp:
    os.environ['THE_LAB_REPO']=tmp
    from the_lab import app as server, messages
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    server._DEMO=False; server._notif_seen_cache={}
    events=[(f'failure:{i}',{'type':'failure','message':f'experiment {i} failed','action':f'GET /api/v1/experiments/{i}/log'}) for i in range(5000)]
    keys={k for k,_ in events}
    assert server._filter_and_mark_seen(None, events, keys)==[]
    assert server._filter_and_mark_seen('new',events,keys)==[]
    server._notif_seen_cache['a']=set()
    batch=server._filter_and_mark_seen('a',events,keys)
    assert len(batch)==8
    assert len(json.dumps(batch).encode())<=4096
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches=list(pool.map(lambda _:server._filter_and_mark_seen('a',events,keys),range(8)))
    emitted=[json.dumps(n) for group in [batch,*batches] for n in group]
    assert len(emitted)==len(set(emitted))==72, 'withheld events must remain pending, emitted events must not repeat'
    app=FastAPI()
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.agent_id=request.headers.get('X-Agent-Id')
        return await call_next(request)
    app.middleware('http')(server.inject_notifications)
    @app.get('/api/v1/small')
    def small():return {'ok':True}
    @app.get('/api/v1/list')
    def listing():return []
    @app.get('/api/v1/notifications')
    def notifications(request:Request):return {'notifications':server.build_notifications(request)}
    server._notif_seen_cache['b']=set()
    for i in range(20):messages.add_message(server.REPO_DIR,from_agent='sender',from_role=None,to='agent:b',text='hello')
    failed=[{'id':i,'label':f'{i}.1'} for i in range(5000)]
    with patch.object(server.store,'list_experiments_by_status',return_value=failed), patch.object(server.store,'list_ideas',return_value=[]), patch('the_lab.agents.list_agents',return_value=[]), patch('the_lab.agents.list_past_agents',return_value=[]):
        with TestClient(app) as client:
            first=client.get('/api/v1/small').json()
            assert first=={'ok':True}, first
            explicit=client.get('/api/v1/notifications').json()
            assert explicit=={'notifications':[]}, explicit
            client.get('/api/v1/list',headers={'X-Agent-Id':'b'})
            assert not server._notif_seen_cache['b'], 'list response must not consume events it cannot carry'
            first=client.get('/api/v1/small',headers={'X-Agent-Id':'b'}).json()['_notifications']
            assert len(first)==16, len(first)
            assert sum(n['type']=='message' for n in first)==8
            assert len(json.dumps(first).encode())<16384
            second=client.get('/api/v1/small',headers={'X-Agent-Id':'b'}).json()['_notifications']
            assert {json.dumps(n) for n in first}.isdisjoint(json.dumps(n) for n in second)
'''
        result = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
