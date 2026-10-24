#!/usr/bin/env python3
"""Isolated real-nginx routing test; no production config, JWT or VM mutations."""
import argparse
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--nginx-module')
    args=parser.parse_args()
    sys.path.insert(0,args.server_root)
    from netlab import store
    spec=importlib.util.spec_from_file_location('netlab.nginx_fixture',args.nginx_module or str(Path(args.server_root)/'netlab/nginx.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if not shutil.which('nginx'):raise SystemExit('nginx is required')
    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/_access':
                identity=self.headers.get('X-Test-Identity')
                target=self.headers.get('X-Lab-Target-User')
                status=401 if not identity else 200 if identity==target or identity=='admin' else 403
            else:status=200
            self.send_response(status);self.end_headers();self.wfile.write(b'fixture')
        def log_message(self,*args):pass
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
    thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start()
    with socket.socket() as free:
        free.bind(('127.0.0.1',0));port=free.getsockname()[1]
    store.STATE.clear();store.STATE.update({'users':{},'pool':[]})
    routes=module.render('tunnel',['alice','bob']).replace('http://127.0.0.1:9000/',f'http://127.0.0.1:{backend.server_port}/')
    with tempfile.TemporaryDirectory(prefix='netlab-nginx-auth-') as tmp:
        path=Path(tmp)
        config=path/'nginx.conf'
        config.write_text(f'pid {path}/nginx.pid; error_log {path}/error.log; events {{}} http {{ access_log off; server {{ listen 127.0.0.1:{port}; {routes} }} }}')
        proc=subprocess.Popen(['nginx','-p',tmp,'-c',str(config),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def status(uri,identity=None,**extra):
            headers={'X-Test-Identity':identity} if identity else {}
            headers.update(extra)
            req=urllib.request.Request(f'http://127.0.0.1:{port}'+uri,headers=headers)
            try:
                with opener.open(req,timeout=3) as response:return response.status
            except urllib.error.HTTPError as exc:return exc.code
        try:
            for _ in range(50):
                try:status('/u/alice/ctl/');break
                except OSError:time.sleep(.1)
            checks=[('/u/alice/ctl/',None,{},401),('/u/alice/ctl/','alice',{},200),
              ('/u/bob/ctl/','alice',{},403),
              ('/u/bob/ctl/?target=alice','alice',{'X-Lab-Target-User':'alice','X-Lab-Auth-Scheme':'target'},403),
              ('/u/alice/ctl/?target=bob','alice',{},200),
              ('/u/alice/../bob/ctl/','alice',{},403),('/u/%62ob/ctl/','alice',{},403),
              ('/_netlab_access_bob?target=alice','alice',{},404),('/u/bob/ctl/','admin',{},200)]
            for uri,identity,extra,expected in checks:
                actual=status(uri,identity,**extra)
                assert actual==expected, (uri,actual,expected)
            print(json.dumps({'ok':True,'realNginxCases':len(checks),'ownAccount':True,'crossAccountDenied':True,'queryAndHeaderSpoofDenied':True,'normalizedPathsDenied':True,'internalLocationDenied':True}))
        finally:
            proc.terminate();proc.wait(timeout=5);backend.shutdown();backend.server_close();thread.join(3)


if __name__=='__main__':main()
