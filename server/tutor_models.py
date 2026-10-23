"""Administrator-owned model registry; preserve runtime settings and never return keys."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from netlab import paths

PARAMETERS = {'max_tokens', 'temperature', 'top_p', 'top_k', 'reasoning_effort',
              'request_timeout_seconds', 'chat_template_kwargs'}
ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')


class Conflict(ValueError):
    pass


def read(path):
    raw = path.read_bytes() if path.exists() else b'{}'
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ValueError('튜터 설정 형식이 올바르지 않습니다.')
    return cfg, hashlib.sha256(raw).hexdigest(), raw


def registry(cfg):
    if 'models' in cfg:
        models = cfg['models']
        if not isinstance(models, dict) or any(not isinstance(m, dict) for m in models.values()):
            raise ValueError('모델 설정 형식이 올바르지 않습니다.')
        return dict(models)
    if cfg.get('endpoint') and cfg.get('model'):
        return {'default': {k: cfg[k] for k in ({'endpoint', 'model', 'api_key'} | PARAMETERS) if k in cfg}}
    return {}


def view(cfg, revision):
    models = registry(cfg)
    visible = [key for key, value in models.items() if value.get('enabled', True)]
    default = cfg.get('default_model')
    if default not in visible:
        default = next(iter(visible), '')
    return dict(ok=True, revision=revision, default_model=default, models=[
        dict(id=key, label=value.get('label', key), model=value.get('model', cfg.get('model', '')),
             endpoint=value.get('endpoint', cfg.get('endpoint', '')), enabled=value.get('enabled', True),
             has_api_key=bool(value.get('api_key', cfg.get('api_key'))),
             parameters={k: v for k, v in value.items() if k in PARAMETERS})
        for key, value in models.items()])


def catalog(path=None):
    cfg, revision, _ = read(Path(path or paths.TUTOR_CONFIG_PATH))
    return view(cfg, revision)


def validated_parameters(value):
    if not isinstance(value, dict) or set(value) - PARAMETERS:
        raise ValueError('지원하지 않는 추론 설정입니다.')
    for key, item in value.items():
        if key in ('max_tokens', 'top_k', 'request_timeout_seconds'):
            bounds = {'max_tokens': (1, 262144), 'top_k': (-1, 10000), 'request_timeout_seconds': (10, 1800)}[key]
            if type(item) is not int or not bounds[0] <= item <= bounds[1]:
                raise ValueError(key + ' 범위를 확인하세요.')
        elif key in ('temperature', 'top_p'):
            if type(item) not in (int, float) or not math.isfinite(item) or not 0 <= item <= (2 if key == 'temperature' else 1):
                raise ValueError(key + ' 범위를 확인하세요.')
        elif key == 'reasoning_effort':
            if item not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                raise ValueError('추론 수준을 확인하세요.')
        elif key == 'chat_template_kwargs':
            if not isinstance(item, dict) or len(json.dumps(item, allow_nan=False)) > 4096:
                raise ValueError('채팅 템플릿 설정을 확인하세요.')
    return value


def update(body, path=None):
    if not isinstance(body, dict):
        raise ValueError('잘못된 요청입니다.')
    path = Path(path or paths.TUTOR_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path)+'.models.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg, revision, raw = read(path)
        if body.get('revision') != revision:
            raise Conflict('설정이 변경됐습니다. 목록을 새로고침한 뒤 다시 저장하세요.')
        models = registry(cfg)
        key, action = body.get('id'), body.get('action')
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError('모델 ID는 영문·숫자·밑줄·하이픈으로 1~64자 입력하세요.')
        if action == 'save':
            value = body.get('model')
            if not isinstance(value, dict):
                raise ValueError('모델 설정이 필요합니다.')
            if body.get('create') and key in models:
                raise ValueError('이미 사용 중인 모델 ID입니다.')
            if not body.get('create') and key not in models:
                raise ValueError('모델을 찾을 수 없습니다. 목록을 새로고침하세요.')
            if key not in models and len(models) >= 100:
                raise ValueError('모델은 최대 100개까지 등록할 수 있습니다.')
            label, model, endpoint = (value.get(field, '') for field in ('label', 'model', 'endpoint'))
            if any(not isinstance(v, str) or not v.strip() for v in (label, model, endpoint)) or len(label) > 80 or len(model) > 256 or len(endpoint) > 2048:
                raise ValueError('표시명·모델 이름·접속 주소를 확인하세요.')
            try:
                url = urlsplit(endpoint.strip())
                valid = url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password and not url.fragment
                _ = url.port
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in endpoint):
                raise ValueError('접속 주소는 사용자 정보가 없는 HTTP 또는 HTTPS 주소여야 합니다.')
            enabled = value.get('enabled', True)
            if type(enabled) is not bool:
                raise ValueError('사용자 노출 여부를 확인하세요.')
            entry = dict(models.get(key, {}))
            for parameter in PARAMETERS:
                entry.pop(parameter, None)
            entry.update(validated_parameters(value.get('parameters', {})))
            entry.update(label=label.strip(), model=model.strip(), endpoint=endpoint.strip(), enabled=enabled)
            if 'api_key' in value:
                if not isinstance(value['api_key'], str) or len(value['api_key']) > 4096 or any(c in value['api_key'] for c in '\r\n'):
                    raise ValueError('API 키 형식을 확인하세요.')
                entry['api_key'] = value['api_key']
            elif key not in models:
                entry['api_key'] = ''
            models[key] = entry
            if body.get('set_default'):
                if not enabled:
                    raise ValueError('숨긴 모델은 기본 모델로 지정할 수 없습니다.')
                cfg['default_model'] = key
        elif action in ('delete', 'default', 'visibility'):
            if key not in models:
                raise ValueError('모델을 찾을 수 없습니다.')
            if action == 'delete':
                del models[key]
            elif action == 'default':
                if not models[key].get('enabled', True):
                    raise ValueError('숨긴 모델은 기본 모델로 지정할 수 없습니다.')
                cfg['default_model'] = key
            else:
                if type(body.get('enabled')) is not bool:
                    raise ValueError('사용자 노출 여부를 확인하세요.')
                models[key] = dict(models[key], enabled=body['enabled'])
        else:
            raise ValueError('지원하지 않는 동작입니다.')
        cfg['models'] = models
        enabled = [k for k, v in models.items() if v.get('enabled', True)]
        if cfg.get('default_model') not in enabled:
            cfg['default_model'] = next(iter(enabled), '')
        encoded = (json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
        if path.exists():
            backup = path.parent / 'tutor-backups'
            backup.mkdir(mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix='tutor-', suffix='.json', dir=backup, delete=False) as saved:
                saved.write(raw); saved.flush(); os.fsync(saved.fileno())
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.tutor-', delete=False) as output:
            temporary = output.name
            try:
                output.write(encoded); output.flush(); os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return view(cfg, hashlib.sha256(encoded).hexdigest())
