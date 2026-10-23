#!/usr/bin/env python3
"""Single-process, bounded WebSocket room; login stays owned by labd."""
import asyncio
from contextlib import suppress
import json
import logging
from http import HTTPStatus
import signal
import time
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import community_auth
import community_chat
from netlab import paths


class Room:
    def __init__(self, secret):
        self.secret = secret
        self.peers = {}
        self.lock = asyncio.Lock()
        self.pending = 0
        self.version = 0

    def online(self):
        # Multiple tabs count once; admin-only observers don't create learners.
        return sorted({community_chat.name(p['owner'], p['client'])
                       for p in self.peers.values() if not p['admin']})

    def enqueue(self, ws, event):
        peer = self.peers.get(ws)
        if not peer:
            return
        try:
            peer['queue'].put_nowait(json.dumps(event, ensure_ascii=False))
        except asyncio.QueueFull:
            # Keep one slow browser from delaying the room or growing memory.
            if not peer.get('closing'):
                peer['closing'] = True
                asyncio.create_task(ws.close(1013, '수신이 느립니다. 다시 연결하세요.'))

    def broadcast(self, event):
        for ws in tuple(self.peers):
            self.enqueue(ws, event)

    def presence(self):
        self.version += 1
        self.broadcast(dict(type='presence', version=self.version, online=self.online()))

    async def writer(self, ws, peer):
        while True:
            await ws.send(await peer['queue'].get())

    async def handle(self, ws):
        if self.pending + len(self.peers) >= 256:
            await ws.close(1013, '접속자가 많습니다. 잠시 후 다시 연결하세요.')
            return
        self.pending += 1
        waiting = True
        writer = None
        try:
            origin = ws.request.headers.get('Origin', '')
            if ws.request.path != '/community/ws' or not origin:
                raise ValueError('지원하지 않는 채팅 경로입니다.')
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if not isinstance(first, dict) or first.get('type') != 'authenticate':
                raise ValueError('채팅 인증이 필요합니다.')
            claims = community_auth.verify(self.secret, first.get('ticket'), origin)
            identity = (claims['owner'], claims['client'], claims['admin'])
            count = sum(p['owner'] == claims['owner'] and
                        (claims['owner'] != 'guest' or p['client'] == claims['client'])
                        for p in self.peers.values())
            if count >= 16:
                await ws.close(1013, '같은 계정의 연결이 너무 많습니다.')
                return
            peer = dict(claims, queue=asyncio.Queue(maxsize=64))
            # Snapshot and registration share the send lock: no missed message
            # between reading history and joining broadcasts.
            async with self.lock:
                data = await asyncio.to_thread(community_chat.handle, claims['owner'],
                                              dict(client=claims['client']), False)
                self.peers[ws] = peer
                self.pending -= 1; waiting = False
                writer = asyncio.create_task(self.writer(ws, peer))
                self.enqueue(ws, dict(type='snapshot', me=data['me'], messages=data['messages'],
                                      online=self.online(), expires=claims['exp']))
                self.presence()
            # A receive timeout enforces expiry even if a browser sends nothing.
            while True:
                remaining = peer['exp'] - time.time()
                if remaining <= 0:
                    raise ValueError('채팅 인증이 만료되었습니다.')
                raw = await asyncio.wait_for(ws.recv(), remaining)
                message = None
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError('잘못된 메시지입니다.')
                    if message.get('type') == 'authenticate':
                        renewed = community_auth.verify(self.secret, message.get('ticket'), origin)
                        if (renewed['owner'], renewed['client'], renewed['admin']) != identity:
                            raise ValueError('로그인 계정이 바뀌었습니다. 다시 연결하세요.')
                        peer['exp'] = renewed['exp']
                        self.enqueue(ws, dict(type='authenticated', expires=renewed['exp']))
                    elif message.get('type') == 'send' and not claims['admin']:
                        async with self.lock:
                            body = dict(action='send', client=claims['client'],
                                        request=message.get('request'), text=message.get('text'))
                            data = await asyncio.to_thread(community_chat.handle, claims['owner'], body, False)
                            self.enqueue(ws, dict(type='ack', request=body['request']))
                            self.broadcast(dict(type='message', message=data['receipt']))
                    else:
                        raise ValueError('지원하지 않는 메시지입니다.')
                except (ValueError, TypeError) as exc:
                    if isinstance(message, dict) and message.get('type') == 'authenticate':
                        raise ValueError('채팅 인증에 실패했습니다.') from None
                    self.enqueue(ws, dict(type='error', request=message.get('request') if isinstance(message, dict) else None,
                                          message=str(exc)[:160]))
        except ConnectionClosed:
            pass
        except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
            await ws.close(1008, '채팅 인증이 필요합니다. 다시 연결하세요.')
        except Exception:
            logging.exception('Community connection failed')
            await ws.close(1011, '채팅 서버 오류입니다. 다시 연결하세요.')
        finally:
            if waiting:
                self.pending -= 1
            if writer:
                writer.cancel()
                with suppress(asyncio.CancelledError, ConnectionClosed):
                    await writer
            if self.peers.pop(ws, None):
                self.presence()


async def main():
    with open(paths.PROXY_SECRET_PATH) as f:
        secret = f.read().strip()
    if not secret:
        raise RuntimeError('Missing community signing secret')
    room = Room(secret)
    def health(connection, request):
        if request.path == '/health':
            return connection.respond(HTTPStatus.OK, 'ok\n')
    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
    async with serve(room.handle, *paths.COMMUNITY_LISTEN, ping_interval=25, ping_timeout=20,
                     max_size=8192, max_queue=8, compression=None, close_timeout=3,
                     server_header=None, process_request=health):
        await stop


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
