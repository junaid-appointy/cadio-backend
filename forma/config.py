"""Runtime data locations.

Everything Forma writes at runtime (runs, previews, uploaded references,
sandbox caches) lives OUTSIDE the repo, under ~/.forma (override with
FORMA_HOME). This is load-bearing: dev servers watch the repo for changes,
and any runtime write inside it restarts the server mid-conversation.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(os.environ.get("FORMA_HOME", Path.home() / ".forma"))
RUNS_DIR = DATA_DIR / "runs"
ASSETS_DIR = DATA_DIR / "assets"
SANDBOX_HOME = DATA_DIR / "sandbox_home"


def _migrate_legacy(old: Path, new: Path) -> None:
    """One-time move of pre-~/.forma data written inside the repo."""
    if not old.is_dir() or new.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
    except OSError:
        pass  # cross-device or permission issue — start fresh instead


_migrate_legacy(REPO_ROOT / "runs", RUNS_DIR)
_migrate_legacy(REPO_ROOT / "assets", ASSETS_DIR)
_migrate_legacy(REPO_ROOT / ".sandbox_home", SANDBOX_HOME)

for _d in (RUNS_DIR, ASSETS_DIR, SANDBOX_HOME):
    _d.mkdir(parents=True, exist_ok=True)
