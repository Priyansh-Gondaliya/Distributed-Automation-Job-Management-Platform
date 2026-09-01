"""
Controller configuration — adjust host/port and paths for your environment.
"""
import os
from typing import Optional

# Project root (parent of the app/ package) — keeps .env and uploads/ stable
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path: Optional[str] = None) -> None:
    """Load KEY=VALUE pairs into os.environ without overriding existing vars."""
    env_path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()

# Flask controller bind address
HOST = os.environ.get("CONTROLLER_HOST", "0.0.0.0")
PORT = int(os.environ.get("CONTROLLER_PORT", "7561"))
# URL shown at startup / for workers (not the bind address)
PUBLIC_URL = os.environ.get(
    "CONTROLLER_PUBLIC_URL",
    f"http://192.168.50.89:{PORT}",
).rstrip("/")

# PostgreSQL (company DB) — password via env / .env only (never commit secrets)
PGHOST = os.environ.get("PGHOST", "192.168.50.18")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGDATABASE = os.environ.get("PGDATABASE", "sitewisedata")
PGUSER = os.environ.get("PGUSER", "crawling")
PGPASSWORD = os.environ.get("PGPASSWORD", "")
PGSCHEMA = os.environ.get("PGSCHEMA", "public")

# Worker considered offline after this many seconds without heartbeat
WORKER_OFFLINE_SECONDS = int(os.environ.get("WORKER_OFFLINE_SECONDS", "30"))

# Secret key for Flask sessions (change in production)
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

# Directory for legacy worker artifacts (tree JSON). Live file trees use DB worker_file_tree.
WORKER_ROOT = os.environ.get("WORKER_ROOT", os.path.join(BASE_DIR, "uploads"))

# Internal Chat API (Schedule Tracking notify). Token only via env / .env.
CHAT_API_BASE = os.environ.get("CHAT_API_BASE", "http://192.168.50.216:85/chat/api/v1").rstrip("/")
CHAT_BOT_TOKEN = os.environ.get("CHAT_BOT_TOKEN", "")
CHAT_SCHEDULES_CHANNEL = os.environ.get("CHAT_SCHEDULES_CHANNEL", "73")
CHAT_ALSO_DM_USER = os.environ.get("CHAT_ALSO_DM_USER", "1").strip().lower() in ("1", "true", "yes")
