"""shell runtime: lessons that run on the learner VM itself (node 'host'), with no containers.

There is nothing to deploy: the lesson's hooks prepare and clean up the environment. Readiness is
the runner's own durable state (contract: shell 재개는 컨테이너 목록이 아니라 완료 표식으로 판단).
"""
import os

from . import store
from .containerlab import copy_work, run_host


class Shell:
    name = 'shell'

    def __init__(self, paths):
        self.paths = paths

    def lesson_dir(self, ldef):
        return store.work_dir(self.paths, ldef)

    def ensure_work_copy(self, ldef, bundle_dir):
        return copy_work(self.paths, ldef, bundle_dir, self.lesson_dir(ldef)) or os.path.isdir(self.lesson_dir(ldef))

    def current(self, ldef):
        return None                      # no observable deployment; the runner state is authoritative

    def ready(self, ldef):
        return True

    def up(self, ldef, timeout):
        return True, ''

    def exec(self, ldef, node, argv, timeout, cancel=None, track=None):
        return run_host(argv, timeout, cancel)

    def stop_inner(self, container, pidfile, wait=3.0):
        return True
