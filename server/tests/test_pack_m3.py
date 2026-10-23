"""M3: runner image build/record/activation, warm pool migration, emergency stop, progress DB backup."""
import importlib.machinery
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import pack_format  # noqa: E402
import packctl  # noqa: E402
from netlab import instances, lesson_progress, lessons, maintainer, paths, store  # noqa: E402
sys.path.insert(0, str(SERVER / 'tests'))
from test_lesson_api import INC, LessonApiCase  # noqa: E402

BASE_FP = 'a' * 64
NEW_FP = 'b' * 64
BUILDER = 'netlab-imgbuild-0123456789'


class FakeHost:
    """LXD on the host as packctl sees it: images by alias, a builder VM with a runner inside."""

    def __init__(self, catalog, aliases=None, check='pass', existing=()):
        self.catalog, self.check = catalog, check
        self.aliases = dict(aliases or {'netlab': BASE_FP})
        self.calls, self.generation, self.lesson = [], 0, None
        self.existing = list(existing)

    def bundles(self):
        return {p: e['bundle']['revision'] for p, e in self.catalog['packs'].items()}

    def __call__(self, *args, timeout=120, stdin=None):
        self.calls.append(args)
        if args[0] == 'query':
            path = args[1]
            if path.startswith('/1.0/images/aliases/'):
                fp = self.aliases.get(path.rsplit('/', 1)[1])
                return (0, json.dumps({'target': fp}), '') if fp else (1, '', 'not found')
            fp = path.rsplit('/', 1)[1]
            if fp in self.aliases.values():
                return 0, json.dumps({'fingerprint': fp, 'type': 'virtual-machine'}), ''
            return 1, '', 'not found'
        if args[0] == 'list':
            return 0, json.dumps(self.existing), ''
        if args[0] == 'publish':
            self.aliases[args[3]] = NEW_FP
            return 0, '', ''
        if args[:3] == ('exec', BUILDER, '--') and args[3] == packctl.RUNNER_PATH:
            if len(args) == 5 and args[4] == '--init':
                return 0, '{"ok": true}', ''
            return 0, json.dumps(self.runner(json.loads(stdin))), ''
        return 0, '', ''

    def runner(self, request):
        token = dict(incarnation=INC, generation=self.generation)
        action = request['action']
        if action == 'status':
            return dict(ok=True, state='running' if self.lesson else 'idle', observed=token,
                        installed=self.bundles())
        assert request['expected'] == token, request
        if action == 'check':
            return dict(ok=True, observed=token, result=dict(status=self.check, items=[]))
        self.generation += 1
        self.lesson = None if action == 'stop' else request.get('target_lesson', self.lesson)
        return dict(ok=True)

    def did(self, verb):
        return [c for c in self.calls if c[0] == verb]


class ImageBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.catalog = pack_format.build(SERVER.parent)
        self.catalog_path = d / 'catalog.json'
        self.catalog_path.write_text(json.dumps(self.catalog))
        self.config = d / 'config.json'
        self.config.write_text(json.dumps({'ip_prefix': '10.200.0.', 'ip_last': 199, 'pool_size': 1}))
        os.chmod(self.config, 0o640)
        self.records = d / 'images'
        self.bundles = []
        for pack in self.catalog['packs']:
            path = d / f'{pack}.tar'
            pack_format.write_bundle(SERVER.parent, self.catalog, pack, path)
            self.bundles.append(path)
        uuid_patch = patch.object(packctl.uuid, 'uuid4', side_effect=self.uuids())
        uuid_patch.start()
        self.addCleanup(uuid_patch.stop)

    @staticmethod
    def uuids():
        import uuid as real
        yield real.UUID('01234567-89ab-4cde-8f01-23456789abcd')       # builder name
        while True:
            yield real.UUID(bytes=os.urandom(16), version=4)

    def build(self, host, alias='netlab-v2-test', bundles=None):
        return packctl.build_image(alias, bundles or self.bundles, run=host, catalog_path=self.catalog_path,
                                   config_path=self.config, records=self.records, say=lambda *_: None)

    def test_build_installs_smokes_and_records_without_touching_old_image(self):
        host = FakeHost(self.catalog)
        record = self.build(host)
        self.assertEqual(record['fingerprint'], NEW_FP)
        self.assertEqual(record['base'], dict(image='netlab', fingerprint=BASE_FP))
        self.assertEqual(record['bundles'], host.bundles())
        self.assertEqual(record['runner']['protocol'], 1)
        smoked = {s['lesson'] for s in record['smoke']}
        self.assertIn('network/12', smoked)
        self.assertIn('example-shell/permissions', smoked)
        self.assertEqual(host.aliases['netlab'], BASE_FP)                    # old alias untouched
        publish = host.did('publish')[0]
        self.assertEqual(publish[1:4], (BUILDER, '--alias', 'netlab-v2-test'))
        self.assertTrue(any(p.startswith('netlab.bundles=') for p in publish))
        self.assertEqual(host.did('delete'), [('delete', BUILDER)])          # only the builder
        init = host.did('init')[0]
        self.assertEqual(init[1:3], ('netlab', BUILDER))
        self.assertIn(('config', 'device', 'override', BUILDER, 'eth0', 'ipv4.address=10.200.0.250'), host.calls)
        saved = json.loads((self.records / 'netlab-v2-test.json').read_text())
        self.assertEqual(saved, record)

    def test_failed_smoke_keeps_builder_and_publishes_nothing(self):
        host = FakeHost(self.catalog, check='fail')
        clock = iter(range(0, 10 ** 6, 100))
        with patch.object(packctl.time, 'time', side_effect=lambda: next(clock)), patch.object(packctl.time, 'sleep'):
            with self.assertRaisesRegex(packctl.ImageError, '해설 적용 뒤에도 fail'):
                self.build(host)
        self.assertEqual(host.did('publish'), [])
        self.assertEqual(host.did('delete'), [])
        self.assertFalse((self.records / 'netlab-v2-test.json').exists())

    def test_smoke_waits_for_convergence(self):
        host = FakeHost(self.catalog, check='fail')
        checks = []

        def converge(request):
            if request['action'] == 'check':
                checks.append(1)
                if len(checks) % 3 == 0:
                    host.check = 'pass'
            elif request['action'] == 'start':
                host.check = 'fail'
            return FakeHost.runner(host, request)
        host.runner = converge
        with patch.object(packctl.time, 'sleep') as sleep:
            record = self.build(host)
        self.assertTrue(all(set(s['steps'].values()) == {'pass'} for s in record['smoke']))
        self.assertTrue(sleep.called)

    def test_one_reset_retry_is_recorded(self):
        host = FakeHost(self.catalog)
        resets = []

        def flaky(request):
            if request['action'] == 'start' and request['target_lesson'] == 'network/12':
                host.check = 'fail'
            elif request['action'] == 'reset':
                resets.append(request['lesson'])
                host.check = 'pass'
            return FakeHost.runner(host, request)
        host.runner = flaky
        clock = iter(range(0, 10 ** 6, 100))
        with patch.object(packctl.time, 'time', side_effect=lambda: next(clock)), patch.object(packctl.time, 'sleep'):
            record = self.build(host)
        self.assertEqual(resets, ['network/12'])
        attempts = {s['lesson']: s['attempts'] for s in record['smoke']}
        self.assertEqual(attempts['network/12'], 2)
        self.assertEqual({a for lesson, a in attempts.items() if lesson != 'network/12'}, {1})

    def test_existing_alias_and_incomplete_bundles_are_refused_before_any_vm(self):
        with self.assertRaisesRegex(packctl.ImageError, '이미 있는'):
            self.build(FakeHost(self.catalog), alias='netlab')
        host = FakeHost(self.catalog)
        with self.assertRaisesRegex(packctl.ImageError, '모든 팩'):
            self.build(host, bundles=self.bundles[:1])
        self.assertEqual(host.did('init'), [])
        with self.assertRaisesRegex(packctl.ImageError, '별칭'):
            self.build(FakeHost(self.catalog), alias='Bad_Name')

    def test_builder_address_avoids_managed_range_and_used_addresses(self):
        used = [{'expanded_devices': {'eth0': {'ipv4.address': '10.200.0.250'}}, 'state': {
            'network': {'eth0': {'addresses': [{'family': 'inet', 'address': '10.200.0.249'}]}}}}]
        self.assertEqual(packctl.builder_ip(FakeHost(self.catalog, existing=used),
                                            {'ip_prefix': '10.200.0.', 'ip_last': 199}), '10.200.0.248')

    def activate(self, host, alias, **kw):
        return packctl.activate(alias, run=host, catalog_path=self.catalog_path, config_path=self.config,
                                records=self.records, **kw)

    def test_activate_requires_matching_record_and_keeps_other_settings(self):
        host = FakeHost(self.catalog)
        self.build(host)
        with self.assertRaisesRegex(packctl.ImageError, '기록이 없습니다'):
            self.activate(host, 'netlab')
        result = self.activate(host, 'netlab-v2-test')
        self.assertEqual((result['previous'], result['image'], result['runner']), ('netlab', 'netlab-v2-test', True))
        config = json.loads(self.config.read_text())
        self.assertEqual(config, {'ip_prefix': '10.200.0.', 'ip_last': 199, 'pool_size': 1, 'image': 'netlab-v2-test'})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        # Rollback to the pre-runner image is explicit.
        self.assertFalse(self.activate(host, 'netlab', legacy=True)['runner'])
        self.assertEqual(json.loads(self.config.read_text())['image'], 'netlab')

    def test_activate_refuses_moved_alias_or_stale_bundles(self):
        host = FakeHost(self.catalog)
        self.build(host)
        host.aliases['netlab-v2-test'] = BASE_FP
        with self.assertRaisesRegex(packctl.ImageError, '다른 이미지'):
            self.activate(host, 'netlab-v2-test')
        host.aliases['netlab-v2-test'] = NEW_FP
        changed = json.loads(self.catalog_path.read_text())
        changed['packs']['network']['bundle']['revision'] = 'f' * 64
        self.catalog_path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(packctl.ImageError, '카탈로그와 다릅니다'):
            self.activate(host, 'netlab-v2-test')
        self.assertNotIn('image', json.loads(self.config.read_text()))


class PoolMigrationTests(unittest.TestCase):
    def setUp(self):
        store.CONFIG.clear(); store.CONFIG.update(store.DEFAULTS)
        store.CONFIG['image'] = 'netlab-v2-test'
        store.STATE.clear(); store.STATE.update({'users': {}, 'pool': []})
        instances._IMAGE_CACHE.clear()
        self.base = {}
        self.deleted = []

    def lxc(self, *args, timeout=600):
        if args[0] == 'query' and args[1] == '/1.0/images/aliases/netlab-v2-test':
            return 0, json.dumps({'target': NEW_FP}), ''
        if args[:2] == ('config', 'get') and args[3] == 'volatile.base_image':
            fp = self.base.get(args[2])
            return (0, fp, '') if fp else (1, '', 'unknown')
        if args[0] == 'delete':
            self.deleted.append(args[1])
        return 0, '', ''

    def spare(self, name, base, **extra):
        self.base[name] = base
        store.STATE['pool'].append(dict(instance=name, ip='10.200.0.20', ready=True, claim=None, **extra))

    def test_stale_spare_is_not_assigned(self):
        self.spare('netlab-old0000000', BASE_FP)
        with patch.object(instances, 'lxc', side_effect=self.lxc), patch.object(instances, 'save_state'), \
                patch.object(instances, 'regen_routing'), patch.object(instances, 'create_instance') as create:
            instances.claim_for('alice')
        create.assert_called_once_with(claim='alice')
        self.assertEqual(store.STATE['pool'][0]['instance'], 'netlab-old0000000')
        self.assertNotIn('alice', store.STATE['users'])

    def test_current_spare_is_assigned(self):
        self.spare('netlab-new0000000', NEW_FP)
        with patch.object(instances, 'lxc', side_effect=self.lxc), patch.object(instances, 'save_state'), \
                patch.object(instances, 'regen_routing'), patch.object(instances, 'ensure_lab_permissions', return_value=True):
            instances.claim_for('alice')
        self.assertEqual(store.STATE['users']['alice']['instance'], 'netlab-new0000000')

    def test_only_unclaimed_ready_stale_spares_are_retired(self):
        self.spare('netlab-old0000000', BASE_FP)
        self.spare('netlab-new0000000', NEW_FP)
        self.spare('netlab-unknown000', None)                         # fingerprint unknown → kept
        self.spare('netlab-claimed000', BASE_FP, )
        store.STATE['pool'][-1]['claim'] = 'bob'
        store.STATE['users']['carol'] = dict(instance='netlab-user000000', ip='10.200.0.30')
        self.base['netlab-user000000'] = BASE_FP
        with patch.object(instances, 'lxc', side_effect=self.lxc), patch.object(maintainer, 'lxc', side_effect=self.lxc), \
                patch.object(maintainer, 'save_state'):
            maintainer.retire_stale_pool()
        self.assertEqual(self.deleted, ['netlab-old0000000'])
        self.assertEqual([p['instance'] for p in store.STATE['pool']],
                         ['netlab-new0000000', 'netlab-unknown000', 'netlab-claimed000'])

    def test_unresolvable_configured_image_retires_nothing(self):
        store.CONFIG['image'] = 'missing'
        self.spare('netlab-old0000000', BASE_FP)
        with patch.object(instances, 'lxc', side_effect=self.lxc), patch.object(maintainer, 'lxc', side_effect=self.lxc), \
                patch.object(maintainer, 'save_state'):
            maintainer.retire_stale_pool()
        self.assertEqual(self.deleted, [])


class EmergencyStopTests(LessonApiCase):
    def test_paused_refuses_new_work_but_keeps_reading(self):
        store.CONFIG['lessons_paused'] = True
        code, body = self.act('start')
        self.assertEqual((code, body['error']), (503, 'paused'))
        self.assertEqual(self.client.calls, [])
        code, body = self.state()
        self.assertEqual(code, 200)
        self.assertTrue(body['environment']['paused'])
        self.assertEqual(body['environment']['path'], 'runner')
        self.assertEqual(lessons.handle_get('alice', ['lesson', 'network', '12'])[0], 200)
        store.CONFIG['lessons_paused'] = False
        self.assertEqual(self.act('start')[0], 202)

    def test_runner_less_vm_reports_the_legacy_path(self):
        self.client.missing.add('alice')
        code, body = self.state()
        self.assertEqual((code, body['error'], body['path']), (410, 'runner_missing', 'legacy'))
        self.assertIn('반납', body['message'])


backup = importlib.machinery.SourceFileLoader('netlab_backup_m3', str(SERVER / 'netlab-backup')).load_module()


class ProgressBackupTests(LessonApiCase):
    """The progress DB is copied through the SQLite backup API, checked, and never re-runs jobs on restore."""

    def make_history(self):
        self.client.check_result = {'status': 'pass', 'items': []}
        self.act('start')
        self.act('check')                                           # completes network/12 for alice
        job, _ = lesson_progress.admit('alice', '99999999-2222-4333-8444-555555555555', 'reset', 'network/12', None,
                                       self.token(), 'vm-alice')
        lesson_progress.set_job(job['job_id'], 'running')          # in flight when the backup is taken
        return job['job_id']

    def test_backup_copy_is_consistent_and_restore_marks_running_jobs_interrupted(self):
        job_id = self.make_history()
        live = Path(paths.LEARNING_DB)
        keep_open = sqlite3.connect(live)                          # labd-like reader holding the WAL open
        self.addCleanup(keep_open.close)
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        target = Path(out.name) / 'payload'
        backup.copy_tree(live.parent, target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['progress.sqlite3'])   # no -wal/-shm
        self.assertEqual(backup.verify_databases(target), ['progress.sqlite3'])
        with patch.object(paths, 'LEARNING_DB', str(target / 'progress.sqlite3')):
            lessons.startup()
            restored = lesson_progress.job('alice', job_id)
            self.assertEqual(restored['state'], 'interrupted')
            self.assertEqual(self.client.calls[-1]['action'], 'check')   # nothing was re-sent to the runner
            with lesson_progress.connect(immediate=False) as c:
                self.assertEqual(c.execute('SELECT completions FROM lesson_summary').fetchone()[0], 1)

    def test_relation_damage_is_reported(self):
        self.make_history()
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        copy = Path(out.name) / 'copy'
        backup.copy_tree(Path(paths.LEARNING_DB).parent, copy)
        db = sqlite3.connect(copy / 'progress.sqlite3')
        db.execute('UPDATE lesson_summary SET completions = completions + 1')
        db.execute("UPDATE completion_events SET account = 'mallory'")
        db.commit()
        db.close()
        problems = backup.verify_sqlite(copy / 'progress.sqlite3')
        self.assertIn('progress.sqlite3: summary differs from completions', problems)
        self.assertIn('progress.sqlite3: completion of another account or lesson', problems)
        with self.assertRaisesRegex(RuntimeError, 'Database check failed'):
            backup.verify_databases(copy)


if __name__ == '__main__':
    unittest.main()
