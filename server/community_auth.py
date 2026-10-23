"""Short-lived, origin-bound chat grants issued only after labd authentication."""
import base64
import hashlib
import hmac
import json
import time
import community_chat

TTL = 300


def issue(secret, owner, client, origin, admin=False):
    if not secret or not isinstance(client, str) or not community_chat.TOKEN.fullmatch(client):
        raise ValueError('브라우저 식별자가 올바르지 않습니다.')
    if not origin or not origin.startswith(('http://', 'https://')):
        raise ValueError('접속 출처를 확인할 수 없습니다.')
    claims = dict(owner=owner, client=client, origin=origin, admin=admin, exp=int(time.time())+TTL)
    data = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), ('community-v1:'+data).encode(), hashlib.sha256).hexdigest()
    return dict(ok=True, ticket=data+'.'+signature, expires=claims['exp'])


def verify(secret, ticket, origin):
    if not secret or not isinstance(ticket, str) or len(ticket)>4096:
        raise ValueError('채팅 인증이 필요합니다.')
    try:
        data, signature = ticket.split('.')
        expected = hmac.new(secret.encode(), ('community-v1:'+data).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(data+'='*(-len(data)%4)))
        if claims['origin'] != origin or not time.time() < claims['exp'] <= time.time()+TTL+5:
            raise ValueError()
        if not isinstance(claims['owner'], str) or not community_chat.TOKEN.fullmatch(claims['client']):
            raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError):
        raise ValueError('채팅 인증이 만료되었거나 유효하지 않습니다.') from None
