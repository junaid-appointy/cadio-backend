"""Object storage (Cloudflare R2) with self-enforced free-tier caps.

R2 is S3-compatible and has zero egress fees, but NO hard spend limit — exceeding
the free tier just bills you. So we enforce the free tier in-app, before every
billable operation, using a meter kept in the metadata DB (shared across
instances, survives restarts — see the r2_usage / r2_objects tables in db.py):

- Storage (10 GB): tracked exactly in `r2_objects`. Before a write we EVICT the
  least-recently-used objects until the incoming bytes fit under the cap. Safe
  because everything in R2 is a regenerable cache — an evicted object is rebuilt
  from the run's program.py on next access. R2 DeleteObject is free.
- Class A ops (writes, 1M/mo) and Class B ops (reads, 10M/mo): a per-month
  counter, incremented atomically only while under the cap. Over the cap, the op
  is refused and the caller falls back to local disk / regeneration.

Everything here is BEST-EFFORT: any R2 or meter error is swallowed so a build or
a file request never fails because object storage misbehaved. Local disk remains
a write-through cache in front of R2; when R2 is disabled (creds absent) this
module is inert and the app behaves exactly as the disk-only build did.
"""

from __future__ import annotations

import mimetypes
import threading
import time
from pathlib import Path

from . import config, db

_CONTENT_TYPES = {
    ".glb": "model/gltf-binary",
    ".stl": "model/stl",
    ".step": "application/step",
    ".stp": "application/step",
    ".json": "application/json",
    ".py": "text/x-python",
    ".png": "image/png",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _content_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _CONTENT_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


class ObjectStore:
    def __init__(self):
        self.enabled = config.r2_enabled()
        self._client = None
        self._client_lock = threading.Lock()
        self._evict_lock = threading.Lock()   # serialize eviction so we don't over/under-free
        self._local = threading.local()        # one DB connection per thread

    # ---- lazy clients -----------------------------------------------------

    def _s3(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import boto3
                    from botocore.config import Config

                    self._client = boto3.client(
                        "s3",
                        endpoint_url=config.R2_ENDPOINT,
                        aws_access_key_id=config.R2_ACCESS_KEY_ID,
                        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
                        region_name="auto",
                        config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
                    )
        return self._client

    def _db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.open_connection()
            self._local.conn = conn
        return conn

    # ---- meter ------------------------------------------------------------

    def _bump(self, column: str, cap: int, n: int = 1) -> bool:
        """Atomically add `n` to this month's counter iff it stays under `cap`.
        Returns True if it incremented (op allowed), False if the cap is reached."""
        conn = self._db()
        period = _period()
        try:
            # ensure the month row exists (idempotent; ignores the race where two
            # threads insert — the unique PK makes the second a no-op).
            if config.is_postgres():
                conn.execute(
                    "INSERT INTO r2_usage(period) VALUES (?) ON CONFLICT (period) DO NOTHING",
                    (period,),
                )
            else:
                conn.execute("INSERT OR IGNORE INTO r2_usage(period) VALUES (?)", (period,))
            conn.commit()
            row = conn.execute(db.bump_counter_sql(column), (n, period, cap)).fetchone()
            conn.commit()
            return row is not None
        except Exception:
            return False  # meter unavailable -> treat as over-cap (don't risk billing)

    def _storage_used(self) -> int:
        try:
            row = self._db().execute("SELECT COALESCE(SUM(size_bytes),0) AS b FROM r2_objects").fetchone()
            return int(row["b"] or 0)
        except Exception:
            return 0

    def _record(self, key: str, size: int) -> None:
        conn = self._db()
        conn.execute(
            db.upsert_sql("r2_objects", ["key", "size_bytes", "last_access_at"], ["key"]),
            (key, size, _now()),
        )
        conn.commit()

    def _touch(self, key: str) -> None:
        try:
            conn = self._db()
            conn.execute("UPDATE r2_objects SET last_access_at=? WHERE key=?", (_now(), key))
            conn.commit()
        except Exception:
            pass

    def _forget(self, key: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM r2_objects WHERE key=?", (key,))
        conn.commit()

    def _make_room(self, incoming: int) -> bool:
        """Evict least-recently-used objects until `incoming` bytes fit under the
        storage cap. Returns False if even an empty bucket can't fit it."""
        cap = config.R2_MAX_STORAGE_BYTES
        if incoming > cap:
            return False
        with self._evict_lock:
            while self._storage_used() + incoming > cap:
                row = self._db().execute(
                    "SELECT key, size_bytes FROM r2_objects ORDER BY last_access_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    return self._storage_used() + incoming <= cap
                try:
                    self._s3().delete_object(Bucket=config.R2_BUCKET, Key=row["key"])  # free op
                except Exception:
                    pass
                self._forget(row["key"])
        return True

    # ---- public API -------------------------------------------------------

    def put(self, key: str, data: bytes, content_type: str | None = None) -> bool:
        """Upload one object under the caps. False -> not stored (disabled, over
        write cap, or too big); caller keeps the local-disk copy as the record."""
        if not self.enabled:
            return False
        try:
            if not self._bump("class_a", config.R2_MAX_CLASS_A_OPS):
                return False  # monthly write cap reached — stay local-only
            if not self._make_room(len(data)):
                return False
            self._s3().put_object(
                Bucket=config.R2_BUCKET, Key=key, Body=data,
                ContentType=content_type or _content_type(key),
            )
            self._record(key, len(data))
            return True
        except Exception:
            return False

    def put_dir(self, run_dir: Path, key_prefix: str) -> int:
        """Upload every file under run_dir; returns the count actually stored."""
        if not self.enabled or not run_dir.is_dir():
            return 0
        stored = 0
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(run_dir).as_posix()
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if self.put(f"{key_prefix}/{rel}", data):
                stored += 1
        return stored

    def has(self, key: str) -> bool:
        try:
            return self._db().execute(
                "SELECT 1 FROM r2_objects WHERE key=?", (key,)
            ).fetchone() is not None
        except Exception:
            return False

    def download_to(self, key: str, dest: Path) -> bool:
        """Fetch an object into `dest` (repopulating the local-disk cache) under
        the read cap. R2→server egress is free. False if unavailable/over cap."""
        if not self.enabled or not self.has(key):
            return False
        try:
            if not self._bump("class_b", config.R2_MAX_CLASS_B_OPS):
                return False  # monthly read cap reached
            obj = self._s3().get_object(Bucket=config.R2_BUCKET, Key=key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(obj["Body"].read())
            self._touch(key)
            return True
        except Exception:
            return False

    def delete_prefix(self, key_prefix: str) -> None:
        """Drop every object under a prefix (project/run deletion). Deletes are
        free on R2, so no cap check."""
        if not self.enabled:
            return
        try:
            rows = self._db().execute(
                "SELECT key FROM r2_objects WHERE key LIKE ?", (key_prefix + "%",)
            ).fetchall()
            for row in rows:
                try:
                    self._s3().delete_object(Bucket=config.R2_BUCKET, Key=row["key"])
                except Exception:
                    pass
                self._forget(row["key"])
        except Exception:
            pass


# module singleton — inert unless R2_* env is set
store = ObjectStore()
