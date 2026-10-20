"""Message claims serialize concurrent owners without restricting discussion."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from the_lab import message_claims, messages
from the_lab.routes import messages as routes


class MessageClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_concurrent_threads_and_processes_have_one_owner(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda n: message_claims.change_claim(self.repo, 1, owner=f'agent-{n}'), range(24)))
        self.assertEqual(sum(r['acquired'] for r in results), 1)
        owner = next(r['claim']['owner'] for r in results if r['acquired'])
        self.assertTrue(all(r['claim']['owner'] == owner for r in results))
        script = 'import json,sys; from pathlib import Path; from the_lab.message_claims import change_claim; print(json.dumps(change_claim(Path(sys.argv[1]), 2, owner=sys.argv[2])))'
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.repo), str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(6)]
        outcomes = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err); outcomes.append(json.loads(out))
        self.assertEqual(sum(r['acquired'] for r in outcomes), 1)
        self.assertEqual(len({r['claim']['owner'] for r in outcomes}), 1)

    def test_retry_expiry_renew_complete_and_release(self):
        first = message_claims.change_claim(self.repo, 1, owner='a')
        self.assertEqual(message_claims.change_claim(self.repo, 1, owner='a')['claim'], first['claim'])
        self.assertFalse(message_claims.change_claim(self.repo, 1, owner='b', action='release')['changed'])
        renewed = message_claims.change_claim(self.repo, 1, owner='a', action='renew', ttl_seconds=7200)
        self.assertGreater(renewed['claim']['expires_at'], first['claim']['expires_at'])
        done = message_claims.change_claim(self.repo, 1, owner='a', action='complete', result_message_id=2)
        self.assertEqual(done['claim']['status'], 'completed')
        self.assertFalse(message_claims.change_claim(self.repo, 1, owner='b')['acquired'])
        self.assertFalse(message_claims.change_claim(self.repo, 1, owner='a')['acquired'])
        self.assertEqual(message_claims.list_claims(self.repo)['1']['result_message_id'], 2)
        message_claims.change_claim(self.repo, 1, owner='operator', action='release', operator=True)
        self.assertTrue(message_claims.change_claim(self.repo, 1, owner='b')['acquired'])
        path = self.repo / '.the_lab/message_claims.json'; data = json.loads(path.read_text())
        data['1']['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(data))
        self.assertNotIn('1', message_claims.list_claims(self.repo))
        self.assertTrue(message_claims.change_claim(self.repo, 1, owner='c')['acquired'])


class MessageClaimRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        for p in (patch.object(routes, 'REPO_DIR', self.repo), patch.object(routes.agents_mod, 'lookup_agent', side_effect=lambda repo, aid: {'role':'worker'} if aid in ('a','b') else None)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI()
        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.agent_id=request.headers.get('X-Agent-Id'); request.state.auth_tier='admin'
            return await call_next(request)
        app.include_router(routes.router); self.client = TestClient(app)
        self.root = messages.add_message(self.repo, from_agent='user:david', from_role=None, to='all', text='Question')
        self.url = f"/api/v1/messages/{self.root['id']}/claim"

    def claim(self, agent, **body):
        return self.client.post(self.url, headers={'X-Agent-Id':agent} if agent else {}, json=body)

    def test_claim_owner_exposed_and_discussion_unrestricted(self):
        self.assertTrue(self.claim('a').json()['acquired'])
        conflict = self.claim('b'); self.assertEqual(conflict.status_code, 200)
        self.assertFalse(conflict.json()['acquired']); self.assertEqual(conflict.json()['claim']['owner'],'a')
        response = self.client.post('/api/v1/messages', headers={'X-Agent-Id':'b'}, json={'in_reply_to':self.root['id'],'text':'Independent review'})
        self.assertEqual(response.status_code,201)
        response = self.client.get(f"/api/v1/threads/{self.root['id']}?peek=1",headers={'X-Agent-Id':'b'})
        self.assertEqual(next(m for m in response.json()['messages'] if m['id']==self.root['id'])['claim']['owner'],'a')
        response = self.client.get(f"/api/v1/messages/{self.root['id']}",headers={'X-Agent-Id':'b'})
        self.assertEqual(response.json()['claim']['owner'],'a')
        self.assertEqual(self.claim('a',action='complete',result_message_id=response.json()['id']).status_code,400)

    def test_identity_privacy_and_payload_checks(self):
        self.assertEqual(self.claim(None).status_code,403)
        self.assertEqual(self.claim('unknown').status_code,403)
        self.assertEqual(self.claim('a',ttl_seconds=0).status_code,422)
        private = messages.add_message(self.repo,from_agent='a',from_role=None,to='agent:a',text='private')
        self.assertEqual(self.client.post(f"/api/v1/messages/{private['id']}/claim",headers={'X-Agent-Id':'b'},json={}).status_code,404)
