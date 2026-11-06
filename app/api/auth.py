"""Web login: the login page, sign-in, sign-out and the gate in front of the app.

* ``GET  /login``        the sign-in form (``/`` and the health checks stay public).
* ``POST /login``        checks the password; sets the session cookie; 303 to ``dashboard``.
* ``POST /logout``       clears the cookie; 303 to ``login``.
* ``GET  /api/session``  who is signed in (the dashboard header uses it).

Everything else needs a valid session (see ``authenticate``): the dashboard
page and ``/docs`` redirect to the login page, every other path answers 401.
Redirects are relative, so the app also works under a reverse-proxy prefix
such as ``/cyclingdays/``.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.dashboard import BASE_SECURITY_HEADERS, WEB_DIR
from app.auth import (
    SESSION_COOKIE,
    USERNAME_PATTERN,
    LoginThrottle,
    SessionSigner,
    UserRecord,
    UserStore,
    dummy_hash,
    normalize_username,
    verify_password,
)
from app.config import Settings

logger = logging.getLogger(__name__)

LOGIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
LOGIN_HEADERS = {
    **BASE_SECURITY_HEADERS,
    "Content-Security-Policy": LOGIN_CSP,
    "Cache-Control": "no-store",
}

PUBLIC_PATHS = frozenset({"/", "/health", "/health/ready", "/login", "/logout"})
PUBLIC_PREFIXES = ("/static/",)
# Unauthenticated GETs of these get a redirect to the login page instead of 401.
LOGIN_REDIRECT_PATHS = frozenset({"/dashboard", "/docs", "/redoc"})

MAX_FORM_BYTES = 4096

router = APIRouter(include_in_schema=False)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _signer(request: Request) -> SessionSigner:
    signer: SessionSigner = request.app.state.session_signer
    return signer


def _users(request: Request) -> UserStore:
    users: UserStore = request.app.state.users
    return users


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _cookie_secure(request: Request) -> bool:
    configured = _settings(request).auth_cookie_secure
    return request.url.scheme == "https" if configured is None else configured


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts.

    ``Sec-Fetch-Site`` first: pages can't forge it, but browsers only send it
    over HTTPS. Otherwise ``Origin``, which browsers send on every POST; the
    pages use ``Referrer-Policy: same-origin`` so that it's the real origin
    (``no-referrer`` makes it ``null``).
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")  # "none": typed/bookmarked by the user
    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser client; the SameSite cookie still applies
    if origin == "null":
        return False  # old browser without Sec-Fetch-Site: can't tell, refuse
    host = request.headers.get("host", "")
    origin_host = urlsplit(origin).hostname or ""
    return origin_host != "" and origin_host == urlsplit(f"//{host}").hostname


def _login_page(status: int = 200, message: str | None = None, username: str = "") -> HTMLResponse:
    template = (WEB_DIR / "login.html").read_text(encoding="utf-8")
    alert = f'<p class="login-error" role="alert">{html.escape(message)}</p>' if message else ""
    page = template.replace("{{message}}", alert).replace(
        "{{username}}", html.escape(username, quote=True)
    )
    return HTMLResponse(page, status_code=status, headers=LOGIN_HEADERS)


async def current_user(request: Request) -> UserRecord | None:
    """The signed-in, still-active user, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    signer = _signer(request)
    claims = signer.verify(token)
    if claims is None:
        return None
    user = await _users(request).get_by_id(claims.user_id)
    if user is None or not user.is_active or not signer.matches(claims, user.password_hash):
        return None
    return user


async def authenticate(request: Request) -> Response | None:
    """None if the request may proceed (``request.state.user`` is set when signed in)."""
    request.state.user = None
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return None
    user = await current_user(request)
    if user is not None:
        request.state.user = user
        return None
    if request.method in ("GET", "HEAD") and path in LOGIN_REDIRECT_PATHS:
        # Relative: /docs and /dashboard sit at the root of the app.
        return RedirectResponse("login", status_code=303, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"detail": "Not authenticated"},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.api_route("/login", methods=["GET", "HEAD"])
async def login_form(request: Request) -> Response:
    if await current_user(request) is not None:
        return RedirectResponse("dashboard", status_code=303)
    return _login_page()


@router.post("/login")
async def login(request: Request) -> Response:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return _login_page(415, "Unsupported form encoding.")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return _login_page(413, "The form is too large.")
    try:
        fields = parse_qs(body.decode("utf-8"), max_num_fields=8)
    except (UnicodeDecodeError, ValueError):
        return _login_page(400, "The form couldn't be read.")
    username = normalize_username(fields.get("username", [""])[0])
    password = fields.get("password", [""])[0]
    if not username or not password:
        return _login_page(400, "Enter your username and password.", username)

    throttle: LoginThrottle = request.app.state.login_throttle
    if throttle.blocked(username):
        logger.warning("login_throttled", extra={"username": username})
        return _login_page(
            429, "Too many failed attempts. Wait a few minutes and try again.", username
        )

    user = (
        await _users(request).get_by_username(username)
        if USERNAME_PATTERN.fullmatch(username)
        else None
    )
    hash_slots: asyncio.Semaphore = request.app.state.hash_slots
    async with hash_slots:
        valid = await asyncio.to_thread(
            verify_password, password, user.password_hash if user else dummy_hash()
        )
    if user is None or not valid or not user.is_active:
        throttle.failure(username)
        logger.warning("login_failed", extra={"username": username})
        return _login_page(401, "Wrong username or password.", username)

    throttle.success(username)
    await _users(request).record_login(user.id)
    settings = _settings(request)
    ttl = settings.auth_session_hours * 3600
    token = _signer(request).issue(user.id, user.password_hash, ttl)
    logger.info("login_succeeded", extra={"username": user.username})
    response = RedirectResponse("dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl,
        path="/",
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    response = RedirectResponse("login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=_cookie_secure(request), samesite="lax"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/session")
async def session_info(request: Request) -> dict[str, Any]:
    user: UserRecord | None = request.state.user
    if user is None:  # unreachable behind the gate; kept for safety
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user.username}
