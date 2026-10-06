"""Run the app's real inbox socket in isolation from module-level lab state."""
import subprocess
import sys
import unittest


class MessageSocketDeliveryTests(unittest.TestCase):
    def test_reconnect_replays_until_ack_and_ack_does_not_mark_read(self):
        script = r'''
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch
with TemporaryDirectory() as tmp:
    os.environ['THE_LAB_REPO'] = tmp
    os.environ.pop('THE_LAB_USER', None)
    os.environ.pop('THE_LAB_PASSWORD', None)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from the_lab import app as server, messages
    from the_lab.routes.messages import router
    server._AUTH_ENABLED = False
    server._DEMO = False
    app = FastAPI()
    app.include_router(router)
    app.websocket('/api/v1/messages/ws')(server.messages_ws_endpoint)
    app.websocket('/api/v1/ws')(server.ws_endpoint)
    @app.middleware('http')
    async def identify(request, call_next):
        request.state.agent_id = 'reader'
        return await call_next(request)
    msg = messages.add_message(server.REPO_DIR, from_agent='sender',
        from_role=None, to='agent:reader', text='A long preview ' * 15)
    with TestClient(app) as client:
        for attempt in range(2):
            with client.websocket_connect('/api/v1/messages/ws?agent_id=reader&ack=1') as ws:
                frame = ws.receive_json()
                assert [m['id'] for m in frame['messages']] == [msg['id']], frame
        response = client.post(f"/api/v1/messages/{msg['id']}/delivered")
        assert response.status_code == 200, response.text
        with client.websocket_connect('/api/v1/messages/ws?agent_id=reader&ack=1') as ws:
            assert ws.receive_json()['messages'] == []
        stored = messages.get_message(server.REPO_DIR, msg['id'])
        assert stored['read_by'] == [], stored
        assert stored['delivered_by'] == ['reader'], stored
        assert client.post(f"/api/v1/messages/{msg['id']}/read").status_code == 200
        assert stored['read_by'] == ['reader']
        # An event emitted during historical replay must reach the same socket.
        from the_lab.ws import Broadcaster
        events = Broadcaster()
        # since=0 (a fresh client) is served from the in-memory ring with no
        # I/O; the race below only exists on the journal path (since>0).
        events.broadcast({'type': 'probe', 'value': 'seen-earlier'})
        events.broadcast({'type': 'probe', 'value': 'before'})
        replay = events.replay_with_journal
        def during_replay(since):
            snapshot = replay(since)
            events.broadcast_soon({'type': 'probe', 'value': 'during'})
            return snapshot
        with patch.object(server._ws_mod, 'broadcaster', events), patch.object(
                events, 'replay_with_journal', side_effect=during_replay):
            with client.websocket_connect('/api/v1/ws?since=1') as ws:
                assert ws.receive_json()['value'] == 'before'
                assert ws.receive_json()['value'] == 'during'
'''
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
