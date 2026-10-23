"""Learning pack format 2 (v2-native authoring) parser — the subset implemented through M2.

Strict: unknown keys are errors (experimental keys only under `ext`), every referenced file must be a
regular file inside the lesson directory, hooks and check scripts must be executable, nodes and
prerequisites must resolve. Kinds read/task are supported; quiz/reflect/service tabs are refused
until their milestone. The output has the same shape as a converted v1 lesson so one pipeline
builds catalog, recipe and bundle for both.
"""
import os
import re
import stat
import tomllib
from pathlib import Path

ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
RUNTIMES = ('containerlab', 'shell')
HOOKS = ('setup', 'inject', 'reset', 'solve', 'teardown')
TIMEOUT_KEYS = ('setup', 'inject', 'reset', 'solve', 'teardown', 'check', 'probe')
DEFAULT_TIMEOUTS = {'setup': 300, 'teardown': 180, 'check': 30}
PACK_KEYS = {'format', 'id', 'title', 'version', 'summary', 'locale', 'level', 'tags', 'authors', 'license',
             'estimated_minutes', 'environment', 'tutor', 'progress', 'ext'}
PACK_ENV_KEYS = {'runtime', 'default_node', 'timeouts'}
LESSON_KEYS = {'id', 'title', 'number', 'summary', 'level', 'estimated_minutes', 'tags', 'prerequisites',
               'assessment', 'completion', 'guide', 'solution', 'environment', 'tutor', 'steps', 'ext'}
LESSON_ENV_KEYS = {'runtime', 'definition', 'nodes', 'hooks', 'timeouts', 'default_node'}
TUTOR_KEYS = {'concept', 'goal', 'fault', 'topics', 'probes'}
STEP_KEYS = {'id', 'kind', 'title', 'text', 'required', 'hints', 'check', 'test', 'goal', 'ext'}
CHECK_KEYS = {'node', 'argv', 'expect', 'timeout', 'label', 'fallback', 'script', 'all'}
EXPECT_KEYS = {'contains', 'regex', 'stream', 'rc'}


class FormatError(RuntimeError):
    pass


def fail(where, message):
    raise FormatError(f'{where}: {message}')


def strict(table, allowed, where):
    if not isinstance(table, dict):
        fail(where, 'must be a table')
    extra = sorted(set(table) - allowed)
    if extra:
        fail(where, 'unknown keys: ' + ', '.join(extra) + ' (experimental keys go under ext)')


def text(value, where, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        fail(where, 'must be a non-empty string')
    return value


def strings(value, where):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        fail(where, 'must be a list of strings')
    return list(value)


def lesson_file(directory, relative, where, executable=False):
    """A regular file inside the lesson directory (no links, no escaping)."""
    if not isinstance(relative, str) or not relative or relative.startswith('/') or '\\' in relative:
        fail(where, f'invalid path {relative!r}')
    path = Path(os.path.normpath(directory / relative))
    try:
        path.relative_to(directory)
    except ValueError:
        fail(where, f'{relative} escapes the lesson directory')
    current = directory
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            fail(where, f'{relative}: symbolic links are not allowed')
    if not path.is_file():
        fail(where, f'{relative} does not exist')
    if executable and not os.stat(path).st_mode & stat.S_IXUSR:
        fail(where, f'{relative} must be executable')
    return relative


def check_spec(item, nodes, default_node, directory, where, nested=False):
    strict(item, CHECK_KEYS, where)
    kinds = [k for k in ('argv', 'script', 'all') if k in item]
    if len(kinds) != 1:
        fail(where, 'check needs exactly one of argv, script, all')
    kind = kinds[0]
    if kind == 'all':
        if nested:
            fail(where, 'all cannot nest')
        if not isinstance(item['all'], list) or not item['all']:
            fail(where, 'all must be a non-empty list')
        extra = set(item) - {'all'}
        if extra:
            fail(where, 'all takes no other keys')
        out = []
        for i, sub in enumerate(item['all']):
            if 'argv' not in sub:
                fail(f'{where}.all[{i}]', 'all items must be declarative (argv)')
            out.append(check_spec(sub, nodes, default_node, directory, f'{where}.all[{i}]', nested=True))
        return {'all': out}
    if kind == 'script':
        extra = set(item) - {'script', 'timeout', 'label'}
        if extra:
            fail(where, 'script checks take only script, timeout, label')
        out = dict(script=lesson_file(directory, item['script'], where, executable=True),
                   timeout=int(item.get('timeout', 30)))
        if 'label' in item:
            out['label'] = text(item['label'], where)
        return out
    node = item.get('node', default_node)
    if node not in nodes:
        fail(where, f'node {node!r} is not declared (nodes: {nodes})')
    expect = item.get('expect')
    strict(expect, EXPECT_KEYS, where + '.expect')
    if sum(k in expect for k in ('contains', 'regex')) != 1:
        fail(where, 'expect needs exactly one of contains/regex')
    if 'regex' in expect:
        re.compile(expect['regex'])
    if expect.get('stream', 'both') not in ('both', 'stdout'):
        fail(where, 'expect.stream must be both or stdout')
    if 'rc' in expect and not all(isinstance(v, int) and not isinstance(v, bool) for v in expect['rc']):
        fail(where, 'expect.rc must be integers')
    out = dict(node=node, argv=strings(item['argv'], where + '.argv'), expect=dict(expect),
               timeout=int(item.get('timeout', 30)))
    if not out['argv']:
        fail(where, 'argv must not be empty')
    if 'label' in item:
        out['label'] = text(item['label'], where)
    if 'fallback' in item:
        fb = item['fallback']
        if 'fallback' in fb or 'argv' not in fb:
            fail(where, 'fallback must be one declarative check without its own fallback')
        out['fallback'] = check_spec(fb, nodes, default_node, directory, where + '.fallback', nested=True)
    return out


def load_pack(root, manifest):
    root = Path(root)
    where = manifest.relative_to(root).as_posix()
    data = tomllib.loads(manifest.read_text())
    strict(data, PACK_KEYS, where)
    if data.get('format') != 2:
        fail(where, 'format must be 2')
    pack_id = text(data.get('id'), where + ':id')
    if not ID_RE.fullmatch(pack_id):
        fail(where, 'invalid pack id')
    env = data.get('environment', {})
    strict(env, PACK_ENV_KEYS, where + ':environment')
    if 'runtime' in env and env['runtime'] not in RUNTIMES:
        fail(where, f"unknown runtime {env['runtime']}")
    timeouts = env.get('timeouts', {})
    strict(timeouts, set(TIMEOUT_KEYS), where + ':environment.timeouts')
    tutor = data.get('tutor', {})
    strict(tutor, {'redact', 'synonyms'}, where + ':tutor')
    progress = data.get('progress', {})
    strict(progress, {'completion'}, where + ':progress')
    if progress.get('completion', 'required-steps') not in ('required-steps', 'manual'):
        fail(where, 'progress.completion must be required-steps or manual')
    return dict(id=pack_id, title=text(data.get('title'), where + ':title'), version=data.get('version'),
                format=2, root=manifest.parent.relative_to(root).as_posix(),
                environment=dict(runtime=env.get('runtime'), default_node=env.get('default_node'),
                                 timeouts={k: int(v) for k, v in timeouts.items()}),
                completion=progress.get('completion', 'required-steps'),
                tutor=dict(redact=strings(tutor.get('redact'), where), synonyms=tutor.get('synonyms', [])))


def load_lesson(root, pack, lesson_dir):
    root = Path(root)
    directory = (root / lesson_dir).resolve()
    where = (Path(lesson_dir) / 'lesson.toml').as_posix()
    data = tomllib.loads((root / lesson_dir / 'lesson.toml').read_text())
    strict(data, LESSON_KEYS, where)
    lesson_id = text(data.get('id'), where + ':id')
    if not ID_RE.fullmatch(lesson_id):
        fail(where, f'invalid lesson id {lesson_id!r}')
    env = dict(data.get('environment', {}))
    strict(env, LESSON_ENV_KEYS, where + ':environment')
    runtime = env.get('runtime') or pack['environment']['runtime']
    if runtime not in RUNTIMES:
        fail(where, f'unknown runtime {runtime!r}')
    timeouts = dict(DEFAULT_TIMEOUTS, **pack['environment']['timeouts'])
    lesson_timeouts = env.get('timeouts', {})
    strict(lesson_timeouts, set(TIMEOUT_KEYS), where + ':environment.timeouts')
    timeouts.update({k: int(v) for k, v in lesson_timeouts.items()})
    hooks = env.get('hooks', {})
    strict(hooks, set(HOOKS), where + ':environment.hooks')
    hooks = {k: lesson_file(directory, v, f'{where}:hooks.{k}', executable=True) for k, v in hooks.items()}
    definition = prefix = None
    if runtime == 'shell':
        if env.get('definition'):
            fail(where, 'shell lessons have no definition file')
        nodes = strings(env.get('nodes'), where + ':nodes') or ['host']
        if nodes != ['host']:
            fail(where, 'shell lessons have exactly one node: host')
    else:
        definition = lesson_file(directory, text(env.get('definition'), where + ':definition'), where)
        topology = (directory / definition).read_text()
        match = re.search(r'^name:\s*(\S+)', topology, re.M)
        if not match:
            fail(where, f'{definition} has no name')
        prefix = 'clab-' + match.group(1)
        from . import topology_nodes
        declared = topology_nodes(topology)
        nodes = strings(env.get('nodes'), where + ':nodes') or declared
        missing = [n for n in nodes if n not in declared]
        if missing:
            fail(where, f'nodes not in {definition}: {missing}')
        nodes = nodes + ['host']
    default_node = env.get('default_node') or pack['environment'].get('default_node') or 'host'
    tutor = data.get('tutor', {})
    strict(tutor, TUTOR_KEYS, where + ':tutor')
    probes, probe_ids = [], set()
    for i, probe in enumerate(tutor.get('probes', [])):
        strict(probe, {'id', 'label', 'node', 'argv'}, f'{where}:tutor.probes[{i}]')
        pid = text(probe.get('id'), f'{where}:tutor.probes[{i}].id')
        if not ID_RE.fullmatch(pid) or pid in probe_ids:
            fail(where, f'invalid or duplicate probe id {pid!r}')
        probe_ids.add(pid)
        node = probe.get('node', default_node)
        if node not in nodes:
            fail(where, f'probe node {node!r} is not declared')
        probes.append(dict(id=pid, label=text(probe.get('label'), where), node=node,
                           argv=strings(probe.get('argv'), where)))
    for topic in strings(tutor.get('topics'), where + ':topics'):
        re.compile(topic)
    steps, step_ids = [], set()
    raw_steps = data.get('steps')
    if not isinstance(raw_steps, list) or not raw_steps:
        fail(where, 'steps must be a non-empty list')
    for i, step in enumerate(raw_steps):
        w = f'{where}:steps[{i}]'
        strict(step, STEP_KEYS, w)
        sid = text(step.get('id'), w + '.id')
        if not ID_RE.fullmatch(sid) or sid in step_ids:
            fail(w, f'invalid or duplicate step id {sid!r}')
        step_ids.add(sid)
        kind = step.get('kind')
        if kind not in ('read', 'task'):
            fail(w, f'kind {kind!r} is not supported yet (read, task)')
        required = step.get('required', kind == 'task')
        if not isinstance(required, bool):
            fail(w, 'required must be true or false')
        out = dict(id=sid, kind=kind, title=text(step.get('title'), w + '.title'), required=required)
        if kind == 'read':
            out['text'] = (Path(lesson_dir) / lesson_file(directory, text(step.get('text'), w + '.text'), w)).as_posix()
            for key in ('check', 'hints', 'test'):
                if key in step:
                    fail(w, f'read steps take no {key}')
        else:
            if 'text' in step:
                out['text'] = (Path(lesson_dir) / lesson_file(directory, step['text'], w)).as_posix()
            out['goal'] = step.get('goal') or tutor.get('goal')
            out['hints'] = strings(step.get('hints'), w + '.hints')
            if 'check' not in step:
                fail(w, 'task steps need a check')
            out['check'] = check_spec(step['check'], nodes, default_node, directory, w + '.check')
            test = step.get('test', {'initial': 'fail', 'solved': 'pass'})
            strict(test, {'initial', 'solved'}, w + '.test')
            if any(v not in ('pass', 'fail', 'any') for v in test.values()):
                fail(w, 'test values are pass, fail or any')
            out['test'] = dict({'initial': 'fail', 'solved': 'pass'}, **test)
        steps.append(out)
    completion = data.get('completion') or pack['completion']
    required = [s for s in steps if s['required']]
    if completion == 'required-steps' and not required:
        fail(where, 'required-steps needs at least one required step (use completion = "manual")')
    if completion == 'manual' and required:
        fail(where, 'manual completion cannot have required steps')
    if completion not in ('required-steps', 'manual'):
        fail(where, 'completion must be required-steps or manual')
    assessment = data.get('assessment', 'fault')
    if assessment not in ('fault', 'observe', 'exploration'):
        fail(where, 'assessment must be fault, observe or exploration')
    guide = data.get('guide')
    solution = data.get('solution')
    return dict(
        id=f"{pack['id']}/{lesson_id}", pack=pack['id'], lesson=lesson_id, aliases=[],
        title=text(data.get('title'), where + ':title'), number=str(data.get('number', '')),
        dir=Path(lesson_dir).as_posix(), assessment=assessment, completion=completion,
        guide=(Path(lesson_dir) / lesson_file(directory, guide, where)).as_posix() if guide else None,
        solution=(Path(lesson_dir) / lesson_file(directory, solution, where)).as_posix() if solution else None,
        prerequisites=strings(data.get('prerequisites'), where + ':prerequisites'),
        environment=dict(runtime=runtime, definition=definition, prefix=prefix, nodes=nodes,
                         timeouts=timeouts, hooks=hooks),
        steps=steps,
        tutor=dict(concept=text(tutor.get('concept'), where + ':concept'),
                   goal=text(tutor.get('goal'), where + ':goal'), fault=tutor.get('fault', ''),
                   topics=strings(tutor.get('topics'), where), probes=probes))


def manifests(root):
    """format-2 pack manifests under packs/*/pack.toml."""
    found = []
    for manifest in sorted((Path(root) / 'packs').glob('*/pack.toml')):
        if manifest.is_symlink():
            continue
        if tomllib.loads(manifest.read_text()).get('format') == 2:
            found.append(manifest)
    return found


def load_all(root, known_lessons=()):
    """[(pack, [lessons])] for every format-2 pack. known_lessons resolve cross-pack prerequisites."""
    root = Path(root)
    result, ids = [], set(known_lessons)
    for manifest in manifests(root):
        pack = load_pack(root, manifest)
        lessons = []
        for lesson_toml in sorted(manifest.parent.glob('*/lesson.toml')):
            if lesson_toml.is_symlink() or lesson_toml.parent.is_symlink():
                fail(lesson_toml.relative_to(root).as_posix(), 'symbolic links are not allowed')
            lesson = load_lesson(root, pack, lesson_toml.parent.relative_to(root).as_posix())
            if lesson['id'] in ids:
                fail(lesson['dir'], f"duplicate lesson id {lesson['id']}")
            ids.add(lesson['id'])
            lessons.append(lesson)
        result.append((pack, lessons))
    for pack, lessons in result:
        for lesson in lessons:
            resolved = []
            for prereq in lesson['prerequisites']:
                full = prereq if '/' in prereq else f"{pack['id']}/{prereq}"
                if full not in ids:
                    fail(lesson['dir'], f'unknown prerequisite {prereq}')
                resolved.append(full)
            lesson['prerequisites'] = resolved
        _no_cycles(lessons)
    return result


def _no_cycles(lessons):
    graph = {l['id']: l['prerequisites'] for l in lessons}
    state = {}

    def visit(node, trail):
        if state.get(node) == 'done':
            return
        if state.get(node) == 'active':
            fail(node, 'prerequisite cycle: ' + ' -> '.join(trail + [node]))
        state[node] = 'active'
        for nxt in graph.get(node, []):
            visit(nxt, trail + [node])
        state[node] = 'done'

    for node in graph:
        visit(node, [])
