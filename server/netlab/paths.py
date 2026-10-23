"""호스트 경로와 리스너 주소. 다른 모듈은 여기서만 가져다 쓴다."""
import os

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(CODE_DIR, "assets")
TEMPLATE_DIR = os.path.join(CODE_DIR, "templates")

LISTEN = ("127.0.0.1", 9000)
COMMUNITY_LISTEN = ("127.0.0.1", 9001)

ETC = "/etc/netlab"
CONFIG_PATH = os.path.join(ETC, "config.json")
TUTOR_CONFIG_PATH = os.path.join(ETC, "tutor.json")
PROXY_SECRET_PATH = os.path.join(ETC, "proxy.secret")
HTPASSWD_DIR = os.path.join(ETC, "htpasswd")
HTPASSWD_ALL = os.path.join(ETC, "htpasswd.all")
ADMIN_HTPASSWD = os.path.join(ETC, "admin.htpasswd")

VAR = "/var/lib/netlab"
STATE_PATH = os.path.join(VAR, "state.json")
USERS_PATH = os.path.join(VAR, "users.json")
RECORD_ROOT = os.path.join(VAR, "recordings")
COMMUNITY_DB = os.path.join(VAR, "community", "chat.sqlite3")
NOTICE_DB = os.path.join(VAR, "notice", "notice.sqlite3")
TUTOR_DB = os.path.join(VAR, "tutor", "chat.sqlite3")

NGINX_SNIPPET_DIR = "/etc/nginx/netlab.d"
NGINX_LAN = os.path.join(NGINX_SNIPPET_DIR, "lan-users.conf")       # 사내망 직결 — basic auth
NGINX_TUNNEL = os.path.join(NGINX_SNIPPET_DIR, "tunnel-users.conf")  # 터널 — Cloudflare Access

VM_RECORD_DIR = "/var/log/lab-sessions"   # 게스트 VM 안의 녹화 위치
CF_METRICS = "http://127.0.0.1:20241"
