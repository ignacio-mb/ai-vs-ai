"""A simple username/password gate for the app.

Credentials come from the environment, so nothing is hard-coded:

* ``APP_USERS`` — comma-separated ``user:password`` pairs, e.g.
  ``APP_USERS="ada:hunter2,grace:s3cret"``
* ``APP_PASSWORD`` — a single shared password (any username accepted).

If neither is set, auth is DISABLED (handy for local dev); the app logs a
warning so you don't accidentally expose it publicly without a password.

Sessions are signed cookies (Flask's ``session``), so the secret matters in
production — set ``SECRET_KEY``. Without it we generate a random one at startup,
which works but logs everyone out on every restart.
"""

import hmac
import os
import secrets
from functools import wraps

from flask import jsonify, redirect, request, session, url_for


def _load_users():
    users = {}
    raw = os.environ.get("APP_USERS", "").strip()
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, pwd = pair.split(":", 1)
        name = name.strip()
        if name:
            users[name] = pwd
    return users


_USERS = _load_users()
_SHARED = os.environ.get("APP_PASSWORD", "").strip()
ENABLED = bool(_USERS or _SHARED)

# Paths reachable without a session.
_OPEN_EXACT = {"/login", "/api/login", "/healthz"}


def init_app(app):
    """Configure the secret key, cookie hardening, and the request gate."""
    app.secret_key = (
        os.environ.get("SECRET_KEY")
        or os.environ.get("APP_SECRET")
        or secrets.token_hex(32)
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Send the cookie only over HTTPS in production (Render serves HTTPS).
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    )

    if not ENABLED:
        app.logger.warning(
            "AUTH DISABLED: no APP_USERS/APP_PASSWORD set. Anyone can use this "
            "app. Set credentials before exposing it publicly."
        )

    @app.before_request
    def _gate():
        if not ENABLED:
            return None
        path = request.path
        if path in _OPEN_EXACT or path.startswith("/static/"):
            return None
        if session.get("user"):
            return None
        # Not authenticated.
        if path.startswith("/api/"):
            return jsonify({"error": "Authentication required."}), 401
        return redirect(url_for("login_page"))


def check(username, password):
    """Constant-time credential check against the configured store."""
    username = (username or "").strip()
    password = password or ""
    ok = False
    if username in _USERS and hmac.compare_digest(_USERS[username], password):
        ok = True
    if _SHARED and hmac.compare_digest(_SHARED, password):
        ok = True
    return ok


def login(username):
    session["user"] = (username or "").strip() or "user"


def logout():
    session.clear()
