"""Run the real shell functions with stubbed Docker/containerlab, never real VMs."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class LabSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        for chapter in ('01', '02', '04'):
            folder = self.root / 'network-lab' / (chapter + '-fixture')
            folder.mkdir(parents=True)
            path = folder / (chapter + '.clab.yml')
            path.touch()
            self.paths[chapter] = str(path)

    def run_shell(self, listing, command='lab 04', destroy_rc=0, docker_rc=0):
        source = (Path(__file__).parents[1] / 'lab-shell.sh').read_text()
        # Do not override the process HOME; substitute only the test copy's paths.
        source = source.replace('$HOME', '${LAB_TEST_HOME}')
        script = 'LAB_TEST_HOME=' + shlex.quote(str(self.root)) + '\n' + source
        script += '\ndocker() { printf "%s\\n" ' + shlex.quote(listing) + '; return ' + str(docker_rc) + '; }\n'
        script += 'containerlab() { printf "CALL %s %s\\n" "$1" "$3"; '
        script += 'if [ "$1" = destroy ]; then return ' + str(destroy_rc) + '; fi; }\n'
        script += command + '\n'
        return subprocess.run(['bash'], input=script, text=True, capture_output=True)

    def test_switch_destroys_other_labs_once_and_keeps_selected(self):
        result = self.run_shell('\n'.join(self.paths[c] for c in ('01', '01', '02', '04')))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [row for row in result.stdout.splitlines() if row.startswith('CALL')]
        self.assertEqual(calls, ['CALL destroy ' + self.paths['01'],
                                 'CALL destroy ' + self.paths['02']])
        self.assertIn('컨테이너 내부 변경 사항은 사라집니다', result.stdout)

    def test_same_lab_does_not_destroy(self):
        result = self.run_shell(self.paths['04'])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('CALL destroy', result.stdout)
        self.assertNotIn('CALL deploy', result.stdout)

    def test_new_lab_is_deployed_after_destroy(self):
        result = self.run_shell(self.paths['01'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CALL destroy ' + self.paths['01'], result.stdout)
        self.assertIn('CALL deploy ' + self.paths['04'], result.stdout)

    def test_destroy_failure_prevents_new_deployment(self):
        result = self.run_shell(self.paths['01'], destroy_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('CALL deploy', result.stdout)

    def test_untrusted_or_missing_topology_aborts_before_any_destroy(self):
        for path in ('/tmp/outside.clab.yml', str(self.root / 'network-lab/missing.clab.yml')):
            result = self.run_shell(self.paths['01'] + '\n' + path)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('CALL ', result.stdout)

    def test_docker_failure_prevents_deployment(self):
        result = self.run_shell('', docker_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('CALL ', result.stdout)

    def test_unlab_destroys_all_labs(self):
        result = self.run_shell(self.paths['01'] + '\n' + self.paths['04'], command='unlab')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count('CALL destroy'), 2)

    def test_switch_lock_prevents_concurrent_mutation(self):
        result = self.run_shell(self.paths['01'], command='exec 8>"$LAB_TEST_HOME/.netlab-switch.lock"; flock 8; lab 04')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('CALL ', result.stdout)
