import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1]))
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import community_auth as auth
import community_chat as chat
from community_ws import Room

SECRET='test-secret-not-for-production'
ORIGIN='https://lab.example'


class GrantTests(unittest.TestCase):
    def test_tampering_expiry_and_origin_binding(self):
        ticket=auth.issue(SECRET,'alice','a'*32,ORIGIN)['ticket']
        self.assertEqual(auth.verify(SECRET,ticket,ORIGIN)['owner'],'alice')
        for secret,token,origin in ((SECRET,ticket, 'https://other.example'),
                                    ('wrong',ticket,ORIGIN),(SECRET,ticket+'x',ORIGIN)):
            with self.assertRaises(ValueError):auth.verify(secret,token,origin)
        with patch.object(auth.time,'time',return_value=time.time()+301):
            with self.assertRaises(ValueError):auth.verify(SECRET,ticket,ORIGIN)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.override=patch.object(chat,'DB',self.tmp.name+'/chat.sqlite3');self.override.start()
        self.room=Room(SECRET)
        self.server=await serve(self.room.handle,'127.0.0.1',0,max_size=8192,compression=None)
        self.url='ws://127.0.0.1:%d/community/ws'%self.server.sockets[0].getsockname()[1]
        self.clients=[]

    async def asyncTearDown(self):
        for client in self.clients:await client.close()
        self.server.close();await self.server.wait_closed()
        self.override.stop();self.tmp.cleanup()

    async def event(self,ws,kind):
        async with asyncio.timeout(3):
            while True:
                event=json.loads(await ws.recv())
                if event['type']==kind:return event

    async def join(self,owner='alice',client='a'*32,admin=False):
        ws=await connect(self.url,origin=ORIGIN,proxy=None);self.clients.append(ws)
        await ws.send(json.dumps(dict(type='authenticate',ticket=auth.issue(SECRET,owner,client,ORIGIN,admin)['ticket'])))
        snapshot=await self.event(ws,'snapshot')
        return ws,snapshot

    async def test_presence_deduplicates_tabs_and_excludes_admin_observer(self):
        first,_=await self.join()
        second,snapshot=await self.join('alice','b'*32)
        self.assertEqual(snapshot['online'],['alice'])
        admin,snapshot=await self.join('operator','c'*32,True)
        self.assertEqual(snapshot['online'],['alice'])
        guest,snapshot=await self.join('guest','d'*32)
        self.assertEqual(snapshot['online'],['alice','guest-dddddd'])
        await first.close();await second.close()
        while True:
            data=await self.event(admin,'presence')
            if data['online']==['guest-dddddd']:break
        await guest.close()
        self.assertEqual((await self.event(admin,'presence'))['online'],[])

    async def test_commit_ack_broadcast_retry_and_reconnect_history(self):
        alice,_=await self.join();bob,_=await self.join('bob','b'*32)
        message=dict(type='send',request='1'*32,text='<img src=x> hello',author='admin')
        await alice.send(json.dumps(message));await self.event(alice,'ack')
        received=(await self.event(bob,'message'))['message']
        self.assertEqual(received['author'],'alice')
        self.assertEqual(received['text'],message['text'])
        await alice.send(json.dumps(message));await self.event(alice,'ack')
        self.assertEqual((await self.event(bob,'message'))['message']['id'],received['id'])
        await bob.close();_,snapshot=await self.join('bob','b'*32)
        self.assertEqual(len(snapshot['messages']),1)
        # WS traffic must not create durable heartbeat/presence records.
        with chat.connection() as db:self.assertEqual(db.execute('select count(*) from presence').fetchone()[0],0)

    async def test_invalid_frames_do_not_break_valid_connection_and_renewal(self):
        ws,_=await self.join()
        await ws.send('not-json');self.assertEqual((await self.event(ws,'error'))['type'],'error')
        await ws.send(json.dumps(dict(type='send',request='2'*32,text='x'*1001)))
        self.assertIn('1000', (await self.event(ws,'error'))['message'])
        await ws.send(json.dumps(dict(type='authenticate',ticket=auth.issue(SECRET,'alice','a'*32,ORIGIN)['ticket'])))
        await self.event(ws,'authenticated')
        await ws.send(json.dumps(dict(type='authenticate',ticket=auth.issue(SECRET,'bob','b'*32,ORIGIN)['ticket'])))
        with self.assertRaises(ConnectionClosed):
            while True:await ws.recv()
        self.assertEqual(ws.close_code,1008)

    async def test_wrong_origin_and_missing_auth_cannot_observe_room(self):
        ws=await connect(self.url,origin='https://evil.example',proxy=None);self.clients.append(ws)
        await ws.send(json.dumps(dict(type='authenticate',ticket=auth.issue(SECRET,'alice','a'*32,ORIGIN)['ticket'])))
        with self.assertRaises(ConnectionClosed):await ws.recv()
        self.assertEqual(len(self.room.peers),0)
