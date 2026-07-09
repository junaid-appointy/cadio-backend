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
