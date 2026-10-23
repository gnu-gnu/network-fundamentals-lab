"""v2 compiler: meaning-preserving conversion, compat bundle contents, references, revisions (M1-09)."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
import build_knowledge  # noqa: E402
import pack_format as pf  # noqa: E402

DEMO_PACK = '''id = "demo"
title = "Demo"
runtime = "containerlab"
[files]
definition = ["*.clab.yml"]
'''
DEMO_LESSON = '''id = "d1"
title = "D1"
concept = "c"
goal = "g"
fault = "f"
topology = "d1.clab.yml"
[[checks]]
node = "a"
argv = ["true"]
expect = { contains = "" }
'''
DEMO_TOPO = 'name: d1\ntopology:\n  nodes:\n    a:\n      kind: linux\n'


class PackFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = pf.build(ROOT)

    def copy_repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shutil.copy(ROOT / 'pack.toml', root / 'pack.toml')
        for path in ROOT.iterdir():
            if path.is_dir() and path.name[:2].isdigit() and (path / 'lesson.toml').exists():
                shutil.copytree(path, root / path.name, symlinks=True)
        return root

    def test_conversion_preserves_v1_checks_and_ids(self):
        v1 = {l['id']: l for l in build_knowledge.load_all(ROOT)[1]}['12']
        lesson = self.catalog['lessons']['network/12']
        self.assertEqual((lesson['lesson'], lesson['aliases'], self.catalog['aliases']['12']), ('12', ['12'], 'network/12'))
        task = [s for s in lesson['steps'] if s['kind'] == 'task'][0]
        self.assertTrue(task['required'])
        self.assertEqual(lesson['completion'], 'required-steps')
        self.assertEqual(task['hints'], v1['hints'])
        self.assertEqual(len(task['check']['all']), len(v1['checks']))
        for converted, original in zip(task['check']['all'], v1['checks']):
            for key in ('node', 'argv', 'expect', 'timeout'):
                self.assertEqual(converted[key], original[key])
            self.assertEqual(converted['fallback']['expect'], original['fallback']['expect'])
            self.assertEqual(converted['fallback']['label'], original['fallback']['label'])
        recipe = self.catalog['packs']['network']['bundle']['recipe']['lessons']['network/12']
        self.assertEqual(recipe['steps']['verify']['check'], task['check'])
        self.assertEqual(recipe['nodes'], ['h1', 'r1', 'r2', 'h2', 'host'])
        self.assertEqual(recipe['hooks'], {'solve': 'fix.sh'})          # compat: fix.sh is the solve hook
        self.assertEqual(recipe['prefix'], 'clab-12-mtu-pmtud')

    def test_bundle_keeps_lesson_files_and_drops_authoring_and_private_data(self):
        pack = self.catalog['packs']['network']
        files = {f['path']: f for f in pack['bundle']['files']}
        self.assertTrue(files['12-mtu-pmtud/fix.sh']['exec'])
        for name in ('README.md', 'WALKTHROUGH.md', '12-mtu-pmtud.clab.yml'):
            self.assertIn('12-mtu-pmtud/' + name, files)
        self.assertNotIn('12-mtu-pmtud/lesson.toml', files)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'b.tar'
            pf.write_bundle(ROOT, self.catalog, 'network', out)
            with tarfile.open(out) as archive:
                names = archive.getnames()
                blob = b''.join(archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()
                                and not m.name.startswith('files/'))
            manifest, recipe, contents = pf.read_bundle(out, pack['bundle']['revision'])
        self.assertNotIn('files/12-mtu-pmtud/lesson.toml', names)
        self.assertFalse(any(n.endswith('pack.toml') for n in names))
        lesson = self.catalog['lessons']['network/12']
        private = [lesson['tutor']['fault'], lesson['tutor']['concept']] + lesson['steps'][1]['hints']
        for text in private:                     # recipe/manifest carry no hint, fault or concept text
            self.assertNotIn(text.encode(), blob)
        self.assertEqual(set(contents), set(files))

    def test_catalog_on_disk_matches_and_rebuild_is_stable(self):
        on_disk = json.loads((SERVER / 'knowledge/v2/catalog.json').read_text())
        self.assertEqual(on_disk, self.catalog)
        self.assertEqual(pf.build(ROOT), self.catalog)

    def test_revisions_follow_content_mode_and_scope(self):
        root = self.copy_repo()
        (root / 'packs/demo/d1').mkdir(parents=True)
        (root / 'packs/demo/pack.toml').write_text(DEMO_PACK)
        (root / 'packs/demo/d1/lesson.toml').write_text(DEMO_LESSON)
        (root / 'packs/demo/d1/d1.clab.yml').write_text(DEMO_TOPO)
        (root / 'packs/demo/d1/README.md').write_text('# d1\n')
        selection = {'network': ('12',), 'demo': ('d1',)}
        base = pf.build(root, selection)
        rev = lambda c, p: (c['packs'][p]['revision'], c['packs'][p]['bundle']['revision'])
        fix = root / '12-mtu-pmtud/fix.sh'
        fix.write_text(fix.read_text() + '\n# changed\n')
        changed = pf.build(root, selection)
        self.assertNotEqual(rev(changed, 'network'), rev(base, 'network'))     # content
        self.assertEqual(rev(changed, 'demo'), rev(base, 'demo'))              # other pack untouched
        readme = root / '12-mtu-pmtud/README.md'
        os.chmod(readme, 0o755)
        self.assertNotEqual(rev(pf.build(root, selection), 'network'), rev(changed, 'network'))   # exec bit
        os.chmod(readme, 0o644)
        os.chmod(fix, 0o644)
        with self.assertRaisesRegex(pf.PackError, 'solve hook'):          # a solve hook must be executable
            pf.build(root, selection)
        os.chmod(fix, 0o755)
        (root / '04-l3-routing/README.md').write_text('unselected lesson changed\n')
        self.assertEqual(rev(pf.build(root, selection), 'network'), rev(changed, 'network'))
        (root / 'packs/demo/d1/README.md').write_text('# d1 changed\n')
        after = pf.build(root, selection)
        self.assertEqual(rev(after, 'network'), rev(changed, 'network'))
        self.assertNotEqual(rev(after, 'demo'), rev(base, 'demo'))
        lesson_toml = root / '12-mtu-pmtud/lesson.toml'
        lesson_toml.write_text(lesson_toml.read_text().replace('h1 경로 MTU', 'h1 경로 MTU 확인'))
        edited = pf.build(root, selection)
        self.assertNotEqual(edited['packs']['network']['revision'], after['packs']['network']['revision'])
        self.assertNotEqual(edited['revision'], after['revision'])

    def test_two_lessons_in_one_directory_share_one_bundle_copy(self):
        ospf, bgp = self.catalog['lessons']['network/17-ospf'], self.catalog['lessons']['network/17-bgp']
        self.assertEqual(ospf['dir'], bgp['dir'])
        self.assertEqual((ospf['environment']['definition'], bgp['environment']['definition']), ('ospf.clab.yml', 'bgp.clab.yml'))
        paths = [f['path'] for f in self.catalog['packs']['network']['bundle']['files']]
        self.assertEqual(paths.count('17-dynamic-routing/fix.sh'), 1)
        three = self.catalog['lessons']['network/03']['steps'][1]['check']['all']
        self.assertEqual(len(three), 2)                                  # 03: both conditions kept

    def test_native_shell_pack(self):
        lesson = self.catalog['lessons']['example-shell/permissions']
        self.assertEqual(lesson['environment']['runtime'], 'shell')
        self.assertEqual(lesson['environment']['nodes'], ['host'])
        self.assertEqual(lesson['steps'][1]['check'], {'script': 'checks/fix.sh', 'timeout': 10})
        self.assertIn(lesson['steps'][0]['text'], self.catalog['documents'])
        files = [f['path'] for f in self.catalog['packs']['example-shell']['bundle']['files']]
        self.assertIn('packs/example-shell/permissions/env/setup.sh', files)
        self.assertNotIn('packs/example-shell/permissions/lesson.toml', files)
        self.assertNotIn('packs/example-shell/pack.toml', files)
        self.assertEqual(lesson['aliases'], [])                          # only the network pack has v1 aliases

    def native_copy(self):
        root = self.copy_repo()
        shutil.copytree(ROOT / 'packs', root / 'packs', symlinks=True)
        return root, root / 'packs/example-shell/permissions/lesson.toml'

    def test_native_format_is_strict(self):
        cases = [
            (lambda t: t.replace('assessment = "fault"', 'assessment = "fault"\nsurprise = 1'), 'unknown keys'),
            (lambda t: t.replace('script = "checks/fix.sh"', 'script = "../../../pack.toml"'), 'escapes'),
            (lambda t: t.replace('script = "checks/fix.sh"', 'script = "checks/missing.sh"'), 'does not exist'),
            (lambda t: t.replace('node = "host"', 'node = "r9"'), 'not declared'),
            (lambda t: t.replace('kind = "task"', 'kind = "quiz"'), 'not supported yet'),
            (lambda t: t.replace('completion = "required-steps"', 'completion = "manual"'), 'manual completion'),
            (lambda t: t.replace('summary = ', 'prerequisites = ["nowhere"]\nsummary = '), 'unknown prerequisite'),
        ]
        for mutate, message in cases:
            root, lesson = self.native_copy()
            lesson.write_text(mutate(lesson.read_text()))
            with self.assertRaisesRegex(pf.PackError, message):
                pf.build(root)
        root, lesson = self.native_copy()
        os.chmod(root / 'packs/example-shell/permissions/env/setup.sh', 0o644)
        with self.assertRaisesRegex(pf.PackError, 'executable'):
            pf.build(root)
        root, lesson = self.native_copy()
        lesson.write_text(lesson.read_text().replace('summary = ', 'prerequisites = ["permissions"]\nsummary = '))
        with self.assertRaisesRegex(pf.PackError, 'cycle'):
            pf.build(root)

    def test_reference_to_an_excluded_file_is_an_error(self):
        root = self.copy_repo()
        (root / '12-mtu-pmtud/hints').mkdir()
        (root / '12-mtu-pmtud/hints/secret.txt').write_text('answer')
        readme = root / '12-mtu-pmtud/README.md'
        readme.write_text(readme.read_text() + '\n참고: `hints/secret.txt`\n')
        with self.assertRaisesRegex(pf.PackError, 'not in the bundle'):
            pf.build(root)

    def test_dynamic_references_are_reported_not_trusted(self):
        root = self.copy_repo()
        readme = root / '12-mtu-pmtud/README.md'
        readme.write_text(readme.read_text() + '\n결과를 `capture-output.txt` 로 저장하세요.\n')
        unverified = pf.build(root)['packs']['network']['unverified_references']
        self.assertTrue(any('capture-output.txt' in ref for ref in unverified))

    def test_missing_bind_and_symlinks_are_errors(self):
        root = self.copy_repo()
        topo = root / '12-mtu-pmtud/12-mtu-pmtud.clab.yml'
        topo.write_text(topo.read_text().replace('      image: nicolaka/netshoot:latest\n',
                                                 '      image: nicolaka/netshoot:latest\n      binds:\n        - conf/r.conf:/etc/r.conf\n', 1))
        with self.assertRaisesRegex(pf.PackError, 'bind'):
            pf.build(root)
        root = self.copy_repo()
        os.symlink('/etc/hostname', root / '12-mtu-pmtud/link.txt')
        with self.assertRaisesRegex(pf.PackError, 'symbolic'):
            pf.build(root)

    def test_tampered_bundles_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'good.tar'
            pf.write_bundle(ROOT, self.catalog, 'network', good)
            with tarfile.open(good) as archive:
                members = [(m, archive.extractfile(m).read()) for m in archive.getmembers()]

            def rewrite(name, mutate):
                out = Path(tmp) / name
                with tarfile.open(out, 'w') as archive:
                    for member, data in mutate(list(members)):
                        info = tarfile.TarInfo(member.name)
                        info.size, info.mode = len(data), member.mode
                        archive.addfile(info, io.BytesIO(data))
                return out

            changed = rewrite('changed.tar', lambda ms: [(m, d + b'x' if m.name.endswith('fix.sh') else d) for m, d in ms])
            extra = rewrite('extra.tar', lambda ms: ms + [(tarfile.TarInfo('files/12-mtu-pmtud/evil.sh'), b'x')])
            unsafe = rewrite('unsafe.tar', lambda ms: ms + [(tarfile.TarInfo('../evil'), b'x')])
            for bad in (changed, extra, unsafe):
                with self.assertRaises(pf.PackError):
                    pf.read_bundle(bad)
            with self.assertRaisesRegex(pf.PackError, 'differs from the server catalog'):
                pf.read_bundle(good, 'f' * 64)


if __name__ == '__main__':
    unittest.main()
