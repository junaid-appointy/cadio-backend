"""Google OAuth (Authlib) + session-cookie auth for the multi-user beta.

Mechanism: Starlette SessionMiddleware (signed itsdangerous cookie) holds only
the user id; Authlib runs the OpenID Connect flow against Google. SessionMiddleware
covers both http AND websocket scopes, so the chat socket authenticates for free
by reading `ws.session["uid"]` (see app.ws_chat).

Config (env):
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   OAuth credentials (auth is OFF if unset)
  FORMA_SESSION_SECRET                       cookie signing key (REQUIRED in prod)
  FORMA_ALLOWED_EMAILS / FORMA_ALLOWED_DOMAIN optional beta allowlist (else open —
                                             gate via Google Console test users)

When Google creds are unset, auth is disabled and every request runs as a single
local "dev" user — so `uv run forma` works out of the box with no OAuth setup.
"""

from __future__ import annotations

import logging
import os
import secrets

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

log = logging.getLogger("forma.api")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

_ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("FORMA_ALLOWED_EMAILS", "").split(",") if e.strip()}
_ALLOWED_DOMAIN = (os.environ.get("FORMA_ALLOWED_DOMAIN", "").strip().lower() or None)

# set by app.py at import time so the dependencies can reach the Store without a
# circular import (app imports auth, not the reverse)
_store = None  # type: ignore[assignment]
_dev_user_id: str | None = None


def configure(store) -> None:
    global _store
    _store = store


def auth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def session_secret() -> str:
    s = os.environ.get("FORMA_SESSION_SECRET")
    if s:
        return s
    log.warning("FORMA_SESSION_SECRET is not set — using a random per-boot secret; "
                "sessions won't survive a restart. Set it in production.")
    return secrets.token_urlsafe(32)


def cookie_secure() -> bool:
    env = os.environ.get("FORMA_COOKIE_SECURE")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    return auth_enabled()  # default: secure in prod (creds set), lax for local dev


def _allowed(email: str) -> bool:
    if not _ALLOWED_EMAILS and not _ALLOWED_DOMAIN:
        return True  # no allowlist configured — rely on Google Console test users
    email = email.lower()
    if email in _ALLOWED_EMAILS:
        return True
    return bool(_ALLOWED_DOMAIN and email.endswith("@" + _ALLOWED_DOMAIN))


oauth = OAuth()
if auth_enabled():
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )

router = APIRouter(prefix="/api/auth")


@router.get("/login")
async def login(request: Request):
    if not auth_enabled():
        return JSONResponse({"error": "auth is not configured"}, status_code=503)
    # In dev the app is served by Vite on a different origin (5173) than the API
    # (8000); the OAuth round-trip must land back on the FRONTEND origin so the
    # session cookie is set there. FORMA_OAUTH_REDIRECT_URI pins that explicitly
    # (and must exactly match a URI registered in Google Console). Same-origin in
    # prod → the request-derived URL is already correct, so the env var is optional.
    redirect_uri = os.environ.get("FORMA_OAUTH_REDIRECT_URI") or str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def callback(request: Request):
    if not auth_enabled():
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse("/?login_error=failed")
    info = token.get("userinfo") or {}
    email = info.get("email")
    if not email or not info.get("email_verified"):
        return RedirectResponse("/?login_error=unverified")
    if not _allowed(email):
        return RedirectResponse("/?login_error=not_invited")
    user = _store.upsert_user(info["sub"], email, info.get("name"), info.get("picture"))
    request.session["uid"] = user["id"]
    return RedirectResponse("/")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


def _dev_user() -> dict:
    """The synthetic single user used when OAuth is disabled. Created once (and
    thus adopts any pre-auth projects, like a first real login would)."""
    global _dev_user_id
    if _dev_user_id is None:
        u = _store.upsert_user("dev-local", "dev@localhost", "Local Dev", None)
        _dev_user_id = u["id"]
        return u
    return _store.get_user(_dev_user_id) or _store.upsert_user("dev-local", "dev@localhost", "Local Dev", None)


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or 401. With auth disabled,
    resolves to the local dev user so the app is usable without OAuth."""
    if not auth_enabled():
        return _dev_user()
    uid = request.session.get("uid")
    if uid and (user := _store.get_user(uid)):
        return user
    raise HTTPException(status_code=401, detail="not signed in")


def current_uid(session: dict) -> str | None:
    """User id from a session mapping (used for the websocket handshake, which
    has `ws.session` but not a Request). Falls back to the dev user when auth
    is disabled."""
    if not auth_enabled():
        return _dev_user()["id"]
    return session.get("uid")


def require_project(pid: str, user: dict) -> dict:
    """Load a project the user owns, or 404 (never 403 — don't leak existence)."""
    proj = _store.get_project(pid)
    if not proj or proj.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="project not found")
    return proj
