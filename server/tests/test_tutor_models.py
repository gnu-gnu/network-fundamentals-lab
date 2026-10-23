import concurrent.futures
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]))
import tutor_models as models
import netlab_tutor as tutor
from netlab import paths,store


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'tutor.json'
        self.config={'max_tokens':8192,'api_key':'global-secret','default_model':'qwen','unrelated':{'keep':True},
          'models':{'qwen':{'label':'Qwen','model':'qwen','endpoint':'http://127.0.0.1:8002/v1/chat/completions','api_key':'private-key'},
                    'muse':{'label':'Muse','model':'muse','endpoint':'http://127.0.0.1:8001/v1/chat/completions','chat_template_kwargs':{'reasoning_strength':'low'}}}}
        self.path.write_text(json.dumps(self.config))

    def change(self,**body):
        body.setdefault('revision',models.catalog(self.path)['revision'])
        return models.update(body,self.path)

    def test_visibility_enforced_and_default_reassigned(self):
        data=self.change(action='visibility',id='qwen',enabled=False)
        cfg=json.loads(self.path.read_text());options,default=tutor.model_options(cfg)
        self.assertEqual(options,[{'id':'muse','label':'Muse'}]);self.assertEqual(default,'muse')
        with self.assertRaises(ValueError):tutor.model_config(cfg,'qwen')
        self.assertEqual(data['default_model'],'muse')
        self.change(action='visibility',id='muse',enabled=False)
        self.assertEqual(tutor.model_options(json.loads(self.path.read_text())),([],''))

    def test_edit_preserves_key_global_settings_and_creates_private_backup(self):
        item=models.catalog(self.path)['models'][0]
        self.assertNotIn('private-key',json.dumps(item));self.assertNotIn('global-secret',json.dumps(item))
        self.change(action='save',id='qwen',model={**item,'label':'새 이름','parameters':{'max_tokens':4096}})
        cfg=json.loads(self.path.read_text())
        self.assertEqual(cfg['models']['qwen']['api_key'],'private-key')
        self.assertEqual(cfg['max_tokens'],8192);self.assertEqual(cfg['unrelated'],{'keep':True})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),0o600)
        backup=next((self.path.parent/'tutor-backups').iterdir())
        self.assertEqual(json.loads(backup.read_text()),self.config)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode),0o600)
        self.change(action='save',id='qwen',model={**item,'api_key':''})
        self.assertEqual(json.loads(self.path.read_text())['models']['qwen']['api_key'],'')

    def test_add_delete_and_empty_registry_do_not_restore_legacy_models(self):
        item={'label':'New','model':'new-api-name','endpoint':'http://localhost:9999/v1/chat/completions','enabled':True,'parameters':{}}
        self.change(action='save',id='new',create=True,model=item,set_default=True)
        self.assertEqual(models.catalog(self.path)['default_model'],'new')
        with self.assertRaises(ValueError):self.change(action='save',id='new',create=True,model=item)
        for key in ('qwen','muse','new'):self.change(action='delete',id=key)
        cfg=json.loads(self.path.read_text());cfg.update(endpoint='http://legacy',model='legacy')
        self.assertEqual(tutor.model_options(cfg),([],''))
        with self.assertRaises(ValueError):tutor.model_config(cfg,'default')

    def test_concurrent_changes_conflict_instead_of_losing_edits(self):
        revision=models.catalog(self.path)['revision']
        barrier=threading.Barrier(2)
        def edit(key):
            barrier.wait()
            try:self.change(action='visibility',id=key,enabled=False,revision=revision);return 'saved'
            except models.Conflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(edit,['qwen','muse'])),['saved','conflict'])

    def test_legacy_configuration_migrates_without_losing_credentials(self):
        self.path.write_text(json.dumps({'endpoint':'http://localhost/v1/chat/completions','model':'legacy','api_key':'legacy-secret','temperature':1}))
        item=models.catalog(self.path)['models'][0]
        self.change(action='save',id='default',model={**item,'label':'Legacy renamed'})
        cfg=json.loads(self.path.read_text())
        self.assertEqual(tutor.model_config(cfg)['api_key'],'legacy-secret')
        self.assertEqual(cfg['temperature'],1)

    def test_invalid_endpoint_and_parameters_leave_file_unchanged(self):
        raw=self.path.read_bytes();item=models.catalog(self.path)['models'][0]
        for extra in ({'endpoint':'file:///etc/passwd'},{'endpoint':'http://secret:password@localhost/v1'},
                      {'parameters':{'api_key':'not-allowed'}},{'parameters':{'max_tokens':0}}):
            with self.assertRaises(ValueError):self.change(action='save',id='qwen',model={**item,**extra})
            self.assertEqual(self.path.read_bytes(),raw)


class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'tutor.json';self.path.write_text(json.dumps({'models':{}}))
        for target,name,value in ((paths,'TUTOR_CONFIG_PATH',str(self.path)),(store,'PROXY_SECRET','fixture-secret')):
            override=patch.object(target,name,value);override.start();self.addCleanup(override.stop)
        spec=importlib.util.spec_from_file_location('models_http_labd',Path(__file__).parents[1]/'labd.py')
        self.app=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.app)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),self.app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,method='GET',body=None,admin=True,origin='http://127.0.0.1'):
        headers={'X-Lab-Proxy-Auth':'fixture-secret','Content-Type':'application/json','Origin':origin,'Host':'127.0.0.1'}
        if admin:headers['X-Lab-Admin']='admin'
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        connection.request(method,'/admin/models',json.dumps(body) if body is not None else None,headers)
        response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result

    def test_admin_only_and_cross_origin_rejection(self):
        self.assertEqual(self.request(admin=False)[0],403)
        _,catalog=self.request()
        body={'action':'save','id':'one','create':True,'revision':catalog['revision'],
              'model':{'label':'One','model':'one','endpoint':'http://localhost/v1/chat/completions','api_key':'private-key'}}
        self.assertEqual(self.request('POST',body,origin='https://evil.example')[0],403)
        status,saved=self.request('POST',body);self.assertEqual(status,200)
        self.assertNotIn('private-key',json.dumps(saved));self.assertTrue(saved['models'][0]['has_api_key'])
        self.assertEqual(self.request('POST',body)[0],409)
