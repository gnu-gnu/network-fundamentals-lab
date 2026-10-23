#!/usr/bin/env python3
"""Serve the learner panel and its assets for browser tests, without labd, LXD or accounts.

    python3 server/tests/serve_panel.py [port]      → http://127.0.0.1:<port>/u/guest/ctl/

Every API path answers 404 JSON; browser tests mock the endpoints they exercise in the page.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TYPES = {'.js': 'application/javascript', '.css': 'text/css'}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        prefix = '/u/guest/ctl/'
        if path == prefix:
            with open(os.path.join(SERVER, 'templates', 'panel.html'), encoding='utf-8') as f:
                page = f.read().replace('__USER__', 'guest').replace('__TUTOR_SHARED_NOTICE__', '')
            return self.send(200, page, 'text/html; charset=utf-8')
        if path.startswith(prefix + 'assets/'):
            name = path[len(prefix + 'assets/'):]
            ext = os.path.splitext(name)[1]
            full = os.path.join(SERVER, 'assets', name)
            if '/' not in name and ext in TYPES and os.path.isfile(full):
                with open(full, 'rb') as f:
                    return self.send(200, f.read(), TYPES[ext])
        return self.send(404, '{"ok":false}', 'application/json')

    do_POST = do_GET

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18990
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
