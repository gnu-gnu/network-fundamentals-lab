"""Learning pack v2 compiler (M1 scope).

Inputs are the v1 pack manifests and lesson.toml files read by build_knowledge.py. Outputs:

  server catalog   knowledge/v2/catalog.json  lessons, steps, hints, tutor data, revisions
  VM bundle        recipe.json + manifest.json + files/<lesson dir>/...   (built by `packctl bundle`)

The VM bundle is the lesson directory minus a deny list (compat mode for the network pack).
The original lesson.toml/pack.toml never enter the bundle; the runner reads the generated recipe.
Revisions follow docs/PACK_IMPLEMENTATION_CONTRACTS.md "팩과 공개 범위".
"""
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import tarfile
from pathlib import Path

FORMAT = 1                     # catalog / recipe format
PROTOCOL = {'min': 1, 'max': 1}  # runner protocol range the recipe works with
# v1-converted lessons registered in v2 (M2 pilot). Format-2 packs register all of their lessons.
SELECTED = {'network': ('03', '12', '17-ospf', '17-bgp')}

DEFAULT_TIMEOUTS = {'setup': 300, 'teardown': 180, 'check': 30}
EXCLUDED_NAMES = {'lesson.toml', 'pack.toml', '.DS_Store'}
EXCLUDED_DIRS = {'quiz', 'hints', '.git', '__pycache__'}
EDITOR_SUFFIXES = ('~', '.swp', '.swo', '.bak', '.orig', '.tmp', '.rej')
REFERENCE_SUFFIX = r'(?:sh|ya?ml|conf|cfg|txt|py|json|j2|toml|ini)'
PATH_TOKEN = re.compile(r'(?<![\w/.-])(\.{0,2}/?(?:[\w.-]+/)*[\w.-]+\.' + REFERENCE_SUFFIX + r')(?![\w/-])')
LINK = re.compile(r'\]\(([^)\s]+)\)')


class PackError(RuntimeError):
    pass


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ───────────────────────────── file selection ─────────────────────────────

def excluded(relative_parts):
    name = relative_parts[-1]
    if any(part in EXCLUDED_DIRS or part.startswith('clab-') for part in relative_parts[:-1]):
        return True
    return (name in EXCLUDED_NAMES or name.startswith('clab-') or name.startswith('.#')
            or 'grading' in name.lower() or name.endswith(EDITOR_SUFFIXES))


def lesson_files(root, directory):
    """Regular files of a lesson directory that belong in the VM bundle, sorted by path.

    Symbolic links and special files are errors, not silently skipped: a bundle must not
    depend on anything outside the lesson directory.
    """
    base = Path(root) / directory
    found = []
    for current, dirs, names in os.walk(base):
        rel_dir = Path(current).relative_to(base)
        dirs[:] = sorted(d for d in dirs if not excluded(rel_dir.parts + (d, 'x')))
        for d in dirs:
            if (Path(current) / d).is_symlink():
                raise PackError(f'{directory}/{(rel_dir / d).as_posix()}: symbolic links are not allowed')
        for name in sorted(names):
            parts = rel_dir.parts + (name,)
            path = Path(current) / name
            if excluded(parts):
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise PackError(f'{directory}/{Path(*parts).as_posix()}: symbolic links are not allowed')
            if not stat.S_ISREG(info.st_mode):
                raise PackError(f'{directory}/{Path(*parts).as_posix()}: only regular files are allowed')
            found.append(dict(path=posixpath.join(directory, *parts), sha256=file_sha(path),
                              exec=bool(info.st_mode & stat.S_IXUSR)))
    return sorted(found, key=lambda item: item['path'])


def generated(relative_parts):
    """Files that are never authoring input: containerlab output, VCS, caches, editor temp files."""
    name = relative_parts[-1]
    return (any(part.startswith('clab-') or part in ('.git', '__pycache__') for part in relative_parts)
            or name.startswith('.#') or name.endswith(EDITOR_SUFFIXES) or name == '.DS_Store')


def authoring_files(root, directory):
    """Every authoring source of a lesson directory (lesson.toml, hints, quiz included) for the pack revision."""
    base = Path(root) / directory
    found = []
    for path in sorted(base.rglob('*')):
        parts = path.relative_to(base).parts
        if generated(parts) or path.is_symlink() or not path.is_file():
            continue
        found.append(dict(path=posixpath.join(directory, *parts), sha256=file_sha(path),
                          exec=bool(os.lstat(path).st_mode & stat.S_IXUSR)))
    return found


# ─────────────────────────── topology inspection ───────────────────────────

def topology_nodes(text):
    """Node names under topology.nodes of a containerlab file (no YAML dependency)."""
    lines = text.splitlines()
    nodes, inside, child_indent = [], False, None
    topology_indent = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped == 'topology:':
            topology_indent = indent
            continue
        if topology_indent is not None and not inside and stripped == 'nodes:' and indent > topology_indent:
            inside, child_indent = indent, None
            continue
        if inside is not False:
            if indent <= inside:
                break
            if child_indent is None:
                child_indent = indent
            match = re.match(r'^([A-Za-z0-9][\w.-]*):\s*(#.*)?$', stripped)
            if indent == child_indent and match:
                nodes.append(match.group(1))
    return nodes


def topology_binds(text):
    """Host-side paths of `binds:` entries (relative paths only matter for the bundle)."""
    paths, in_binds, binds_indent = [], False, 0
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith('binds:'):
            in_binds, binds_indent = True, indent
            continue
        if in_binds:
            if stripped and not stripped.startswith('-') and indent <= binds_indent:
                in_binds = False
                continue
            match = re.match(r"^-\s*['\"]?([^:'\"\s]+):", stripped)
            if match:
                paths.append(match.group(1))
    return paths


# ─────────────────────────── reference completeness ───────────────────────────

def references(root, lesson_dir, bundle_paths):
    """Check local file references of guide, solution and topology files.

    Returns (missing, unverified). `missing` are references that resolve to a file inside the
    lesson directory that is not in the bundle (or a bind that does not exist): build errors.
    `unverified` are references that resolve nowhere (files created during the lab, or dynamic
    shell paths); they are reported for review on a dedicated VM, never assumed safe.
    """
    root = Path(root)
    missing, unverified = [], []
    lesson_root = (root / lesson_dir).resolve()

    def inside_lesson(path):
        try:
            path.resolve().relative_to(lesson_root)
            return True
        except ValueError:
            return False

    def resolve(token, source):
        token = token.split('#', 1)[0]
        if not token or token.startswith('/') or re.match(r'^[a-z]+:', token):
            return
        candidates = [(root / lesson_dir / token), (root / token)]
        for candidate in candidates:
            candidate = Path(os.path.normpath(candidate))
            if candidate.is_file():
                if inside_lesson(candidate):
                    rel = candidate.resolve().relative_to(root.resolve()).as_posix()
                    if rel not in bundle_paths:
                        missing.append(f'{source}: {token} is not in the bundle')
                return              # outside the lesson: the work copy carries other chapters too
        unverified.append(f'{source}: {token}')

    for name in sorted(os.listdir(root / lesson_dir)):
        path = root / lesson_dir / name
        if not path.is_file() or excluded((name,)):
            continue
        source = f'{lesson_dir}/{name}'
        text = path.read_text(errors='replace')
        if name.endswith('.md'):
            for target in LINK.findall(text):
                if not re.match(r'^(https?:|mailto:|#)', target):
                    resolve(target, source)
            for token in PATH_TOKEN.findall(text):
                resolve(token, source)
        elif name.endswith(('.clab.yml', '.clab.yaml')):
            for bind in topology_binds(text):
                if bind.startswith('/'):
                    continue
                target = Path(os.path.normpath(root / lesson_dir / bind))
                if not target.exists() or not inside_lesson(target):
                    missing.append(f'{source}: bind {bind} must exist inside the lesson directory')
    return sorted(set(missing)), sorted(set(unverified))


# ─────────────────────────────── conversion ───────────────────────────────

def check_item(item):
    out = dict(node=item['node'], argv=list(item['argv']), expect=dict(item['expect']),
               timeout=int(item['timeout']))
    if 'label' in item:
        out['label'] = item['label']
    if item.get('fallback'):
        out['fallback'] = check_item(item['fallback'])
    return out


def convert_lesson(pack, v1, root):
    """v1 compiled lesson -> v2 catalog lesson. Meaning-preserving (contract "호환성")."""
    lesson_dir = v1['dir']
    definition = v1.get('topology')
    if not definition:
        raise PackError(f"{v1['id']}: only containerlab lessons are supported in M1")
    topology = (Path(root) / lesson_dir / definition).read_text()
    nodes = topology_nodes(topology)
    if not nodes:
        raise PackError(f"{v1['id']}: no nodes found in {definition}")
    nodes = nodes + ['host']          # 'host' = the learner VM itself (v1 probes without a node)
    hooks = {}
    fix = Path(root) / lesson_dir / 'fix.sh'
    if fix.is_file() and not fix.is_symlink():
        if not os.stat(fix).st_mode & stat.S_IXUSR:
            raise PackError(f'{lesson_dir}/fix.sh must be executable to serve as the solve hook')
        hooks['solve'] = 'fix.sh'     # compat mode: the chapter's fix script is its solve hook
    files = pack['files']
    steps = [dict(id='guide', kind='read', required=False, title='실습 안내',
                  text=posixpath.join(lesson_dir, files['guide']))]
    completion = 'manual'
    if v1['assessment'] == 'fault':
        items = [check_item(c) for c in v1['checks']]
        for item in items:
            for each in (item, item.get('fallback')):
                if each and each['node'] not in nodes:
                    raise PackError(f"{v1['id']}: check node {each['node']} is not in the topology")
        steps.append(dict(id='verify', kind='task', required=True, title='목표 달성 확인',
                          goal=v1['goal'], hints=list(v1['hints']), check={'all': items},
                          test={'initial': 'fail', 'solved': 'pass'}))
        completion = 'required-steps'
    probes = []
    for index, probe in enumerate(v1['probes'], 1):
        if probe['node'] is not None and probe['node'] not in nodes:
            raise PackError(f"{v1['id']}: probe node {probe['node']} is not in the topology")
        probes.append(dict(id=f'probe-{index}', label=probe['label'], node=probe['node'] or 'host',
                           argv=list(probe['argv'])))
    solution = posixpath.join(lesson_dir, files['solution'])
    return dict(
        id=f"{pack['id']}/{v1['id']}", pack=pack['id'], lesson=v1['id'], aliases=[v1['id']],
        title=v1['title'], number=v1['number'], dir=lesson_dir, assessment=v1['assessment'],
        completion=completion, guide=posixpath.join(lesson_dir, files['guide']),
        solution=solution if (Path(root) / solution).is_file() else None,
        environment=dict(runtime='containerlab', definition=definition, prefix=v1['prefix'],
                         nodes=nodes, timeouts=dict(DEFAULT_TIMEOUTS), hooks=hooks),
        steps=steps,
        tutor=dict(concept=v1['concept'], goal=v1['goal'], fault=v1['fault'],
                   topics=list(v1['topics']), probes=probes),
    )


def recipe_lesson(lesson):
    """What the VM runner needs: ids, runtime, nodes, checks, probes, timeouts. No hints, no fault text."""
    steps = {}
    for step in lesson['steps']:
        if step['kind'] == 'task':
            steps[step['id']] = dict(check=step['check'])
    env = lesson['environment']
    return dict(dir=lesson['dir'], runtime=env['runtime'], definition=env['definition'],
                prefix=env['prefix'], nodes=list(env['nodes']), timeouts=dict(env['timeouts']),
                hooks=dict(env.get('hooks') or {}), steps=steps,
                probes={p['id']: dict(node=p['node'], argv=p['argv']) for p in lesson['tutor']['probes']})


def build(root, selection=None):
    """Compile the v2 catalog (a dict): selected v1 lessons (converted) + every format-2 pack."""
    import build_knowledge     # server/ is on sys.path for every caller
    from . import native
    root = Path(root)
    selection = SELECTED if selection is None else selection
    v1_packs, v1_lessons = build_knowledge.load_all(root)
    by_pack = {pack['id']: pack for pack in v1_packs}
    catalog = dict(format=FORMAT, packs={}, lessons={}, aliases={}, documents={})
    for pack_id in sorted(selection):
        if pack_id not in by_pack:
            raise PackError(f'unknown pack in selection: {pack_id}')
        pack = by_pack[pack_id]
        wanted = list(selection[pack_id])
        v1 = {lesson['id']: lesson for lesson in v1_lessons if lesson['pack'] == pack_id}
        unknown = [w for w in wanted if w not in v1]
        if unknown:
            raise PackError(f'{pack_id}: unknown lessons in selection: {unknown}')
        lessons = [convert_lesson(pack, v1[w], root) for w in wanted]
        _add_pack(catalog, root, pack, lessons, Path(pack['root'] or '.') / 'pack.toml')
    try:
        native_packs = native.load_all(root, known_lessons=catalog['lessons'])
    except native.FormatError as exc:
        raise PackError(str(exc)) from None
    for pack, lessons in native_packs:
        if pack['id'] in catalog['packs']:
            raise PackError(f"duplicate pack id: {pack['id']}")
        _add_pack(catalog, root, pack, lessons, Path(pack['root']) / 'pack.toml')
        for lesson in lessons:          # read-step texts of native lessons are not in the v1 corpus
            for step in lesson['steps']:
                path = step.get('text')
                if path:
                    body = (root / path).read_text()
                    catalog['documents'][path] = dict(sha256=hashlib.sha256(body.encode()).hexdigest(), text=body)
    catalog['revision'] = digest(sorted((pid, p['revision']) for pid, p in catalog['packs'].items()))
    return catalog


def _add_pack(catalog, root, pack, lessons, manifest_path):
    pack_id = pack['id']
    directories = sorted({lesson['dir'] for lesson in lessons})
    bundle_files, sources, unverified = [], [], []
    for directory in directories:
        files = lesson_files(root, directory)
        bundle_files.extend(files)
        missing, unknown_refs = references(root, directory, {f['path'] for f in files})
        if missing:
            raise PackError('; '.join(missing))
        unverified.extend(unknown_refs)
        sources.extend(authoring_files(root, directory))
    sources.append(dict(path=manifest_path.as_posix(), sha256=file_sha(root / manifest_path), exec=False))
    sources.sort(key=lambda item: item['path'])
    recipe = dict(format=FORMAT, protocol=dict(PROTOCOL), pack=pack_id,
                  lessons={lesson['id']: recipe_lesson(lesson) for lesson in lessons})
    bundle_revision = bundle_digest(pack_id, recipe, bundle_files)
    pack_revision = digest(dict(format=FORMAT, pack=pack, lessons=lessons, sources=sources))
    catalog['packs'][pack_id] = dict(
        id=pack_id, title=pack['title'], revision=pack_revision, lessons=[l['id'] for l in lessons],
        bundle=dict(revision=bundle_revision, recipe=recipe, files=bundle_files),
        unverified_references=sorted(set(unverified)))
    for lesson in lessons:
        catalog['lessons'][lesson['id']] = lesson
        for alias in lesson['aliases']:
            if alias in catalog['aliases']:
                raise PackError(f'duplicate alias: {alias}')
            catalog['aliases'][alias] = lesson['id']


def bundle_digest(pack_id, recipe, files):
    return digest(dict(format=FORMAT, pack=pack_id, recipe=hashlib.sha256(canonical(recipe)).hexdigest(),
                       files=[dict(path=f['path'], sha256=f['sha256'], exec=f['exec']) for f in files]))


# ─────────────────────────────── bundle tar ───────────────────────────────

def bundle_manifest(pack_id, pack_entry):
    bundle = pack_entry['bundle']
    return dict(format=FORMAT, pack=pack_id, bundle=bundle['revision'],
                recipe_sha256=hashlib.sha256(canonical(bundle['recipe'])).hexdigest(),
                files=bundle['files'])


def write_bundle(root, catalog, pack_id, output):
    """Write the VM bundle tar for one pack. Files are re-hashed so a stale catalog is refused."""
    pack_entry = catalog['packs'][pack_id]
    files = pack_entry['bundle']['files']
    recipe_bytes = canonical(pack_entry['bundle']['recipe'])
    manifest = bundle_manifest(pack_id, pack_entry)

    def add_bytes(archive, name, data, mode=0o644):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0
        archive.addfile(info, io.BytesIO(data))

    with tarfile.open(output, 'w') as archive:
        add_bytes(archive, 'recipe.json', recipe_bytes)
        add_bytes(archive, 'manifest.json', canonical(manifest))
        for item in files:
            data = (Path(root) / item['path']).read_bytes()
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise PackError(f"{item['path']}: changed since the catalog was built; rebuild first")
            add_bytes(archive, 'files/' + item['path'], data, 0o755 if item['exec'] else 0o644)
    return manifest


def read_bundle(path, expected_revision=None):
    """Validate a bundle tar and return (manifest, recipe, {path: (bytes, exec)}).

    Every member must be a regular file at a safe relative path, every file in the manifest must
    be present with the recorded hash and mode, and nothing else may be present.
    """
    members = {}
    with tarfile.open(path, 'r') as archive:
        for info in archive.getmembers():
            name = info.name
            if not info.isfile():
                if info.isdir():
                    continue
                raise PackError(f'bundle member is not a regular file: {name}')
            if name.startswith('/') or '..' in Path(name).parts or '\\' in name:
                raise PackError(f'unsafe bundle path: {name}')
            if name in members:
                raise PackError(f'duplicate bundle member: {name}')
            members[name] = (archive.extractfile(info).read(), bool(info.mode & 0o100))
    for required in ('recipe.json', 'manifest.json'):
        if required not in members:
            raise PackError(f'bundle lacks {required}')
    manifest = json.loads(members.pop('manifest.json')[0])
    recipe_bytes = members.pop('recipe.json')[0]
    recipe = json.loads(recipe_bytes)
    if hashlib.sha256(canonical(recipe)).hexdigest() != manifest.get('recipe_sha256'):
        raise PackError('recipe does not match the manifest')
    files = {}
    for item in manifest['files']:
        key = 'files/' + item['path']
        if key not in members:
            raise PackError(f"bundle lacks {item['path']}")
        data, executable = members.pop(key)
        if hashlib.sha256(data).hexdigest() != item['sha256'] or executable != item['exec']:
            raise PackError(f"{item['path']}: hash or mode differs from the manifest")
        files[item['path']] = (data, executable)
    if members:
        raise PackError('unexpected bundle members: ' + ', '.join(sorted(members)))
    revision = bundle_digest(manifest['pack'], recipe, manifest['files'])
    if revision != manifest['bundle']:
        raise PackError('bundle revision does not match its contents')
    if expected_revision and revision != expected_revision:
        raise PackError('bundle revision differs from the server catalog; rebuild or redeploy')
    return manifest, recipe, files
