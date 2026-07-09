"""Project store — SQLite persistence for projects, conversations, runs, assets.

Files stay on disk (config.project_*_dir); the DB holds metadata only.
Single shared connection guarded by a lock (local single-user scale); WAL so
reads never block. The worker pool never touches the DB.

Message records are stored in a persistence-friendly, base64-free shape (user
messages reference image assets by id, not inline data) so the same rows drive
both LLM-history replay and UI scrollback — see cadio/api/history.py.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    google_sub  TEXT UNIQUE,
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    picture     TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT,
    thumb_run   TEXT,
    user_id     TEXT              -- owner; NULL for pre-auth projects (adopted by first user)
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role       TEXT NOT NULL,          -- user|assistant|tool|event
    content    TEXT NOT NULL,          -- JSON payload
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT NOT NULL,       -- run_id (unique within a project)
    project_id    TEXT NOT NULL REFERENCES projects(id),
    parent_run_id TEXT,                -- tweak lineage -> version tree
    label         TEXT,
    ok            INTEGER NOT NULL,
    meta          TEXT NOT NULL,       -- JSON (engine result payload)
    created_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
CREATE TABLE IF NOT EXISTS assets (
    id         TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    file       TEXT NOT NULL,
    name       TEXT NOT NULL,
    mime       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._migrate_add_project_owner()
        self._migrate_legacy_flat()

    def _migrate_add_project_owner(self) -> None:
        """Add projects.user_id on databases created before multi-user (the
        CREATE TABLE above only adds it to fresh DBs). Idempotent."""
        with self._lock:
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "user_id" not in cols:
                self._conn.execute("ALTER TABLE projects ADD COLUMN user_id TEXT")
                self._conn.commit()

    # ---- users ------------------------------------------------------------

    DEV_SUB = "dev-local"  # google_sub of the placeholder used when OAuth is off

    def upsert_user(self, google_sub: str, email: str,
                    name: str | None = None, picture: str | None = None) -> dict:
        """Find-or-create the user for a Google identity, refreshing profile fields.

        Project adoption keeps work from being stranded across the dev→Google
        transition: the local-dev placeholder adopts pre-auth (owner-less)
        projects, and then the FIRST real Google user inherits BOTH the owner-less
        projects AND everything the dev placeholder was holding — so signing in
        with Google for the first time carries all existing history over.

        Portable SQL only (see store docstring): the read-then-write is race-free
        under the store-wide lock today; the Postgres port keeps it in one pooled
        transaction."""
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM users WHERE google_sub=?", (google_sub,)).fetchone()
            if existing:
                uid = existing["id"]
                self._conn.execute(
                    "UPDATE users SET email=?, name=?, picture=? WHERE id=?",
                    (email, name, picture, uid))
            else:
                is_dev = google_sub == self.DEV_SUB
                any_user = self._conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
                real_user = self._conn.execute(
                    "SELECT 1 FROM users WHERE google_sub != ? LIMIT 1", (self.DEV_SUB,)).fetchone() is not None
                uid = _new_id()
                self._conn.execute(
                    "INSERT INTO users(id, google_sub, email, name, picture, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (uid, google_sub, email, name, picture, now))
                if is_dev and not any_user:
                    # local-dev placeholder adopts pre-auth projects
                    self._conn.execute("UPDATE projects SET user_id=? WHERE user_id IS NULL", (uid,))
                elif not is_dev and not real_user:
                    # first real owner inherits owner-less projects + the dev placeholder's
                    self._conn.execute(
                        "UPDATE projects SET user_id=? WHERE user_id IS NULL "
                        "OR user_id IN (SELECT id FROM users WHERE google_sub=?)",
                        (uid, self.DEV_SUB))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._user_row(row)

    def get_user(self, uid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._user_row(row) if row else None

    def _user_row(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "email": row["email"], "name": row["name"],
                "picture": row["picture"], "created_at": row["created_at"]}

    # ---- projects ---------------------------------------------------------

    def create_project(self, name: str, user_id: str | None = None) -> dict:
        pid = _new_id()
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects(id, name, created_at, updated_at, user_id) VALUES (?,?,?,?,?)",
                (pid, name.strip() or "Untitled project", now, now, user_id),
            )
            self._conn.commit()
        config.project_runs_dir(pid).mkdir(parents=True, exist_ok=True)
        config.project_refs_dir(pid).mkdir(parents=True, exist_ok=True)
        return self.get_project(pid)

    def list_projects(self, user_id: str, include_archived: bool = False) -> list[dict]:
        q = "SELECT * FROM projects WHERE user_id=?"
        if not include_archived:
            q += " AND archived_at IS NULL"
        q += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(q, (user_id,)).fetchall()
        return [self._project_row(r) for r in rows]

    def count_runs_today(self, user_id: str) -> int:
        """Runs this user created since local midnight — for the daily quota.
        created_at is ISO local time, so a lexical `>=` range works on any DB."""
        start = time.strftime("%Y-%m-%dT00:00:00")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM runs JOIN projects ON runs.project_id = projects.id "
                "WHERE projects.user_id = ? AND runs.created_at >= ?", (user_id, start)).fetchone()
        return row["c"]

    def get_project(self, pid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return self._project_row(row) if row else None

    def update_project(self, pid: str, name: str | None = None, archived: bool | None = None) -> dict | None:
        sets, vals = [], []
        if name is not None:
            sets.append("name=?"); vals.append(name.strip() or "Untitled project")
        if archived is not None:
            sets.append("archived_at=?"); vals.append(_now() if archived else None)
        if not sets:
            return self.get_project(pid)
        sets.append("updated_at=?"); vals.append(_now())
        vals.append(pid)
        with self._lock:
            self._conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)
            self._conn.commit()
        return self.get_project(pid)

    def delete_project(self, pid: str) -> bool:
        """Permanently remove a project: DB rows + all its files on disk."""
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                return False
            for tbl in ("messages", "runs", "assets"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE project_id=?", (pid,))
            self._conn.execute("DELETE FROM projects WHERE id=?", (pid,))
            self._conn.commit()
        import shutil as _sh
        _sh.rmtree(config.project_dir(pid), ignore_errors=True)
        return True

    def _touch(self, pid: str) -> None:
        self._conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (_now(), pid))

    def _project_row(self, row: sqlite3.Row) -> dict:
        with self._lock:
            counts = self._conn.execute(
                "SELECT COUNT(*) c FROM runs WHERE project_id=? AND ok=1", (row["id"],)
            ).fetchone()
        d = dict(row)
        d["model_count"] = counts["c"]
        # thumbnail is a rendered PNG (Track B produces render.png per run);
        # None until then — the UI falls back to an icon.
        thumb = row["thumb_run"]
        thumb_png = config.project_runs_dir(row["id"]) / thumb / "render.png" if thumb else None
        d["thumb_url"] = (
            self._run_file_url(row["id"], thumb, "render.png") if thumb_png and thumb_png.exists() else None
        )
        return d

    # ---- messages ---------------------------------------------------------

    def add_message(self, pid: str, role: str, content: dict) -> dict:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(project_id, role, content, created_at) VALUES (?,?,?,?)",
                (pid, role, json.dumps(content), now),
            )
            self._touch(pid)
            self._conn.commit()
            mid = cur.lastrowid
        return {"id": mid, "role": role, "content": content, "created_at": now}

    def get_messages(self, pid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE project_id=? ORDER BY id", (pid,)
            ).fetchall()
        return [
            {"id": r["id"], "role": r["role"], "content": json.loads(r["content"]), "created_at": r["created_at"]}
            for r in rows
        ]

    # ---- runs -------------------------------------------------------------

    def add_run(self, pid: str, run_id: str, label: str, ok: bool, meta: dict,
                parent_run_id: str | None = None, created_at_override: str | None = None) -> dict:
        now = created_at_override or _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(id, project_id, parent_run_id, label, ok, meta, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, pid, parent_run_id, label, int(ok), json.dumps(meta), now),
            )
            # first successful run becomes the project thumbnail
            if ok:
                proj = self._conn.execute("SELECT thumb_run FROM projects WHERE id=?", (pid,)).fetchone()
                if proj and not proj["thumb_run"]:
                    self._conn.execute("UPDATE projects SET thumb_run=? WHERE id=?", (run_id, pid))
            self._touch(pid)
            self._conn.commit()
        return {"run_id": run_id, "label": label, "ok": ok, "meta": meta, "created_at": now,
                "parent_run_id": parent_run_id}

    def list_runs(self, pid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC", (pid,)
            ).fetchall()
        return [self._run_row(r) for r in rows]

    def get_run(self, pid: str, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE project_id=? AND id=?", (pid, run_id)
            ).fetchone()
        return self._run_row(row) if row else None

    def _run_row(self, row: sqlite3.Row) -> dict:
        meta = json.loads(row["meta"])
        return {
            "run_id": row["id"],
            "project_id": row["project_id"],
            "parent_run_id": row["parent_run_id"],
            "label": row["label"] or "",
            "ok": bool(row["ok"]),
            "meta": meta,
            "created_at": row["created_at"],
        }

    def _run_file_url(self, pid: str, run_id: str, name: str) -> str:
        return f"/files/{pid}/runs/{run_id}/{name}"

    # ---- assets -----------------------------------------------------------

    def add_asset(self, pid: str, asset_id: str, file: str, name: str, mime: str) -> dict:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO assets(id, project_id, file, name, mime, created_at) VALUES (?,?,?,?,?,?)",
                (asset_id, pid, file, name, mime, now),
            )
            self._touch(pid)
            self._conn.commit()
        return self._asset_dict(pid, asset_id, file, name, mime, now)

    def list_assets(self, pid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE project_id=? ORDER BY created_at DESC", (pid,)
            ).fetchall()
        return [self._asset_dict(pid, r["id"], r["file"], r["name"], r["mime"], r["created_at"]) for r in rows]

    def delete_asset(self, pid: str, asset_id: str) -> bool:
        """Remove a reference asset: DB row + file on disk."""
        with self._lock:
            row = self._conn.execute(
                "SELECT file FROM assets WHERE project_id=? AND id=?", (pid, asset_id)
            ).fetchone()
            if not row:
                return False
            self._conn.execute("DELETE FROM assets WHERE project_id=? AND id=?", (pid, asset_id))
            self._touch(pid)
            self._conn.commit()
        (config.project_refs_dir(pid) / row["file"]).unlink(missing_ok=True)
        return True

    def get_asset(self, pid: str, asset_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE project_id=? AND id=?", (pid, asset_id)
            ).fetchone()
        if not row:
            return None
        return self._asset_dict(pid, row["id"], row["file"], row["name"], row["mime"], row["created_at"])

    def _asset_dict(self, pid: str, aid: str, file: str, name: str, mime: str, created_at: str) -> dict:
        return {
            "id": aid, "file": file, "name": name, "mime": mime, "created_at": created_at,
            "url": f"/files/{pid}/refs/{file}",
        }

    # ---- migration --------------------------------------------------------

    def _migrate_legacy_flat(self) -> None:
        """Move pre-Track-A flat runs/assets into an 'Unsorted imports' project.
        Idempotent: only runs when there are no projects yet and legacy dirs
        have content. Nothing is deleted — files are moved."""
        with self._lock:
            has_projects = self._conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
        legacy_runs = sorted(
            d for d in config.LEGACY_RUNS_DIR.glob("*")
            if d.is_dir() and d.name != "_preview" and (d / "meta.json").exists()
        ) if config.LEGACY_RUNS_DIR.exists() else []
        legacy_assets = list(config.LEGACY_ASSETS_DIR.glob("*.json")) if config.LEGACY_ASSETS_DIR.exists() else []
        if has_projects or (not legacy_runs and not legacy_assets):
            return

        proj = self.create_project("Unsorted imports")
        pid = proj["id"]
        import shutil as _sh

        for run_dir in legacy_runs:
            try:
                meta = json.loads((run_dir / "meta.json").read_text())
            except (json.JSONDecodeError, OSError):
                continue
            dest = config.project_runs_dir(pid) / run_dir.name
            try:
                _sh.move(str(run_dir), str(dest))
            except OSError:
                continue
            # rewrite the engine result payload into the new project-scoped shape
            new_meta = self._legacy_run_meta(run_dir.name, meta, dest)
            self.add_run(pid, run_dir.name, meta.get("label", ""), bool(meta.get("ok")), new_meta,
                         created_at_override=meta.get("created_at"))

        for meta_file in legacy_assets:
            try:
                a = json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            src = config.LEGACY_ASSETS_DIR / a["file"]
            if not src.exists():
                continue
            dest = config.project_refs_dir(pid) / a["file"]
            try:
                _sh.move(str(src), str(dest))
            except OSError:
                continue
            self.add_asset(pid, a["id"], a["file"], a.get("name", a["file"]), a.get("mime", "image/png"))

    def _legacy_run_meta(self, run_id: str, old: dict, dest: Path) -> dict:
        """Strip old urls; keep engine facts; rebuild the artifacts filename
        map from files actually present. URLs are recomputed by the API."""
        keep = {k: old.get(k) for k in
                ("ok", "params", "manifest", "bbox", "volume_mm3", "validation", "error")}
        keep["run_id"] = run_id
        artifacts = {}
        for kind, name in (("stl", "model.stl"), ("step", "model.step"), ("glb", "model.glb")):
            if (dest / name).exists():
                artifacts[kind] = name
        keep["artifacts"] = artifacts
        return keep
