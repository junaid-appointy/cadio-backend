"""Runtime data locations.

Everything CADIO writes at runtime (the project database, run artifacts,
uploaded references, sandbox caches) lives OUTSIDE the repo, under ~/.cadio
(override with CADIO_HOME). This is load-bearing: dev servers watch the repo
for changes, and any runtime write inside it restarts the server
mid-conversation.

Layout (Track A):
  ~/.cadio/
    cadio.db                       SQLite: projects, messages, runs, assets
    projects/<pid>/runs/<run_id>/  run artifacts (stl/step/glb/program.py)
    projects/<pid>/refs/<file>     uploaded reference images
    previews/                      ephemeral slider-preview scratch (global)
    sandbox_home/                  OCCT caches (warm workers)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Load a local .env (GOOGLE_CLIENT_ID/SECRET, CADIO_SESSION_SECRET, provider keys)
# before anything reads os.environ. config is imported early everywhere, so this
# runs ahead of auth.py's module-level credential lookup. Best-effort.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

DATA_DIR = Path(os.environ.get("CADIO_HOME", Path.home() / ".cadio"))

# One-time move for installs from before the Forma -> CADIO rename: the whole
# runtime dir moved from ~/.forma to ~/.cadio, so upgrading in place would
# otherwise silently start every user from an empty database.
_legacy_home = Path.home() / ".forma"
if _legacy_home.is_dir() and not DATA_DIR.exists():
    try:
        DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(_legacy_home), str(DATA_DIR))
        _legacy_db = DATA_DIR / "forma.db"
        if _legacy_db.exists():
            _legacy_db.rename(DATA_DIR / "cadio.db")
    except OSError:
        pass  # cross-device or permission issue — start fresh instead

DB_PATH = DATA_DIR / "cadio.db"
PROJECTS_DIR = DATA_DIR / "projects"
PREVIEW_DIR = DATA_DIR / "previews"
SANDBOX_HOME = DATA_DIR / "sandbox_home"

# legacy flat dirs (pre-Track-A); kept only so the store can migrate them
LEGACY_RUNS_DIR = DATA_DIR / "runs"
LEGACY_ASSETS_DIR = DATA_DIR / "assets"


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def project_runs_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid / "runs"


def project_refs_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid / "refs"


def _migrate_repo_leftover(old: Path, new: Path) -> None:
    """One-time move of pre-~/.cadio data written inside the repo."""
    if not old.is_dir() or new.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
    except OSError:
        pass  # cross-device or permission issue — start fresh instead


# pull any data still sitting in the repo out to ~/.cadio (older installs)
_migrate_repo_leftover(REPO_ROOT / "runs", LEGACY_RUNS_DIR)
_migrate_repo_leftover(REPO_ROOT / "assets", LEGACY_ASSETS_DIR)
_migrate_repo_leftover(REPO_ROOT / ".sandbox_home", SANDBOX_HOME)

for _d in (PROJECTS_DIR, PREVIEW_DIR, SANDBOX_HOME):
    _d.mkdir(parents=True, exist_ok=True)


# ---- database backend ------------------------------------------------------
# Metadata lives in SQLite by default (single box, ~$0). Set DATABASE_URL to a
# Postgres/Supabase connection string to switch the whole Store over — nothing
# else changes. Use Supabase's *pooled* connection string (port 6543) for the app.
#   postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres
DATABASE_URL = os.environ.get("DATABASE_URL") or None


def is_postgres() -> bool:
    return bool(DATABASE_URL) and DATABASE_URL.split(":", 1)[0] in ("postgres", "postgresql")


# ---- object storage (Cloudflare R2) ----------------------------------------
# Run artifacts live on local disk by default (a write-through cache). When the
# R2_* creds below are all present, the disk becomes a cache in front of R2, and
# R2 holds the durable copy. R2 has NO hard spend cap, so we enforce the free
# tier ourselves: the caps below are checked in cadio/storage.py before every
# billable operation (storage bytes via LRU eviction, Class A/B ops via a
# monthly counter in the DB). Headroom is deliberate — stay UNDER the real limit.
def _env_str(name: str) -> str | None:
    """Read an env var, tolerating a stray inline comment. Docker's --env-file
    keeps everything after `=` verbatim (`VAR=val # note` -> `val # note`), so a
    value that is ENTIRELY a comment is treated as unset."""
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    if v.startswith("#"):
        return None
    return v or None


R2_ACCOUNT_ID = _env_str("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = _env_str("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = _env_str("R2_SECRET_ACCESS_KEY")
R2_BUCKET = _env_str("R2_BUCKET")
# optional explicit endpoint; only accept a real URL, else derive from account id
_endpoint = _env_str("R2_ENDPOINT")
if _endpoint and not _endpoint.startswith("http"):
    _endpoint = None
R2_ENDPOINT = _endpoint or (
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else None
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# free-tier caps (with headroom under 10 GB / 1M / 10M)
R2_MAX_STORAGE_BYTES = _env_int("R2_MAX_STORAGE_BYTES", 9_500_000_000)   # 9.5 GB
R2_MAX_CLASS_A_OPS = _env_int("R2_MAX_CLASS_A_OPS", 950_000)             # writes/mo
R2_MAX_CLASS_B_OPS = _env_int("R2_MAX_CLASS_B_OPS", 9_500_000)          # reads/mo


def r2_enabled() -> bool:
    return all((R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT))


# ---- split deploy (separate frontend / backend origins) --------------------
# When the frontend is served from its OWN origin (a different container/host)
# and reaches this API over the network, set these. Left unset, the app stays
# single-origin (the backend serves the SPA) and nothing below changes.
#   CADIO_FRONTEND_URL  comma-separated allowed browser origin(s) of the frontend
#                       — enables CORS (with credentials) and is where OAuth
#                       returns the user after sign-in.
#   CADIO_PUBLIC_URL    this backend's OWN public base URL, so artifact/preview
#                       links come back absolute (…/files/…) and load from the
#                       API origin instead of the frontend's.
CADIO_FRONTEND_URLS = [u.strip().rstrip("/") for u in
                       os.environ.get("CADIO_FRONTEND_URL", "").split(",") if u.strip()]
CADIO_PUBLIC_URL = (os.environ.get("CADIO_PUBLIC_URL", "").strip().rstrip("/") or None)


def cross_site() -> bool:
    """True when the frontend is on a different origin (cookies must be
    SameSite=None; Secure and CORS must allow credentials)."""
    return bool(CADIO_FRONTEND_URLS)


def public_url(path: str) -> str:
    """Absolutize a backend-relative path (/files/…, /previews/…) using
    CADIO_PUBLIC_URL when set, so a cross-origin frontend loads it from the API
    origin. No-op (returns the path unchanged) in single-origin mode."""
    if CADIO_PUBLIC_URL and path.startswith("/"):
        return CADIO_PUBLIC_URL + path
    return path


def frontend_home() -> str:
    """Where to send the browser after OAuth: the frontend origin when split,
    else same-origin root."""
    return CADIO_FRONTEND_URLS[0] if CADIO_FRONTEND_URLS else "/"
