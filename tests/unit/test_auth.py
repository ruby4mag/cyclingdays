"""Web login: hashing, session tokens, throttling and the gate (no database)."""

from __future__ import annotations

from functools import lru_cache

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    SessionSigner,
    UserRecord,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from app.main import create_app
from tests.conftest import make_unit_settings

SECRET = "x" * 40
PASSWORD = "correct horse battery"


@lru_cache(maxsize=1)
def _known_hash() -> str:
    return hash_password(PASSWORD)


class FakeUserStore:
    def __init__(self, *users: UserRecord) -> None:
        self.users = {u.id: u for u in users}
        self.logins: list[int] = []

    async def get_by_username(self, username: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.username == username), None)

    async def get_by_id(self, user_id: int) -> UserRecord | None:
        return self.users.get(user_id)

    async def record_login(self, user_id: int) -> None:
        self.logins.append(user_id)


def rider(**changes: object) -> UserRecord:
    values: dict[str, object] = {
        "id": 1,
        "username": "rider",
        "password_hash": _known_hash(),
        "is_active": True,
    }
    values.update(changes)
    return UserRecord(**values)  # type: ignore[arg-type]


def signed_in_client(app: FastAPI, store: FakeUserStore | None = None) -> TestClient:
    """A client for ``app`` (lifespan not run) signed in as ``rider``."""
    app.state.users = store or FakeUserStore(rider())
    client = TestClient(app)
    response = client.post(
        "/login", data={"username": "rider", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def store() -> FakeUserStore:
    return FakeUserStore(rider())


@pytest.fixture
def app(store: FakeUserStore) -> FastAPI:
    application = create_app(make_unit_settings(auth_secret_key=SECRET), athlete_slug="primary")
    application.state.users = store
    return application


@pytest.fixture
def anon(app: FastAPI) -> TestClient:
    return TestClient(app)


def _login(client: TestClient, username: str = "rider", password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def test_password_hash_round_trip_and_salting() -> None:
    first = _known_hash()
    assert first.startswith("scrypt$32768$8$1$")
    assert verify_password(PASSWORD, first)
    assert not verify_password(PASSWORD + "!", first)
    assert hash_password(PASSWORD) != first  # random salt
    for broken in ("", "scrypt$1$2", "bcrypt$1$1$1$AA$AA", "scrypt$x$8$1$AA$AA"):
        assert not verify_password(PASSWORD, broken)
    assert not verify_password("a" * 5000, first)


def test_username_and_password_rules() -> None:
    assert validate_username("  Rider.One ") == "rider.one"
    for bad in ("", "a b", "x" * 65, "ünïcode", "a/b"):
        with pytest.raises(ValueError):
            validate_username(bad)
    with pytest.raises(ValueError):
        validate_password("short")
    assert validate_password("twelve chars") == "twelve chars"


def test_session_token_sign_verify_expire_and_tamper() -> None:
    signer = SessionSigner(SECRET)
    token = signer.issue(7, "hash-a", ttl_seconds=60, now=1_000)
    claims = signer.verify(token, now=1_030)
    assert claims is not None and claims.user_id == 7
    assert signer.matches(claims, "hash-a")
    assert not signer.matches(claims, "hash-b")  # password changed: session ends
    assert signer.verify(token, now=1_060) is None  # expired
    assert SessionSigner("y" * 40).verify(token, now=1_030) is None  # other key
    uid, exp, fp, sig = token.split(".")
    assert signer.verify(f"8.{exp}.{fp}.{sig}", now=1_030) is None
    assert signer.verify(f"{uid}.9999999999.{fp}.{sig}", now=1_030) is None
    for junk in ("", "a.b.c", "....", "é.1.2.3", "1.2.3." + "a" * 300):
        assert signer.verify(junk, now=1_030) is None
    with pytest.raises(ValueError):
        SessionSigner("too-short")


def test_throttle_per_user_and_global() -> None:
    t = LoginThrottle(per_user=3, total=5, window=100)
    for i in range(3):
        assert not t.blocked("a", now=i)
        t.failure("a", now=i)
    assert t.blocked("a", now=3)
    assert not t.blocked("b", now=3)
    assert not t.blocked("a", now=100.5)  # oldest failure left the window
    t.failure("b", now=101)
    t.failure("c", now=101)
    t.failure("d", now=101)
    t.failure("e", now=101)
    t.failure("f", now=101)
    assert t.blocked("new", now=102)  # global cap
    assert not t.blocked("new", now=202)
    t.failure("g", now=300)
    t.success("g")
    assert not t.blocked("g", now=301)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_public_paths_need_no_login(anon: TestClient) -> None:
    assert anon.get("/health").status_code == 200
    assert anon.get("/static/dashboard.css").status_code == 200
    assert anon.get("/", follow_redirects=False).status_code == 307
    page = anon.get("/login")
    assert page.status_code == 200
    assert "form-action 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert 'action="login"' in page.text and "{{" not in page.text
    assert anon.get("/static/login.css").status_code == 200
    for photo in ("login-hero.jpg", "login-hero-small.jpg"):
        image = anon.get(f"/static/{photo}")
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert "style=" not in page.text and "<script" not in page.text  # CSP: none inline


def test_protected_paths_without_login(anon: TestClient) -> None:
    for path in ("/dashboard", "/docs"):
        response = anon.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "login"  # relative: works under a prefix
    for path in ("/api/dashboard/summary", "/api/session", "/openapi.json", "/nope"):
        response = anon.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"detail": "Not authenticated"}
    assert anon.post("/api/dashboard/summary").status_code == 401


def test_login_sets_hardened_cookie_and_opens_the_app(
    anon: TestClient, store: FakeUserStore
) -> None:
    response = _login(anon, username="  RIDER ")
    assert response.status_code == 303
    assert response.headers["location"] == "dashboard"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # plain-HTTP test request, AUTH_COOKIE_SECURE unset
    assert "Max-Age=604800" in cookie
    assert store.logins == [1]
    assert anon.get("/dashboard").status_code == 200
    assert anon.get("/api/session").json() == {"username": "rider"}
    assert anon.get("/login", follow_redirects=False).headers["location"] == "dashboard"


def test_wrong_password_unknown_user_and_inactive_user(
    store: FakeUserStore, anon: TestClient
) -> None:
    store.users[2] = rider(id=2, username="gone", is_active=False)
    attempts = (("rider", "wrong password!"), ("nobody", PASSWORD), ("gone", PASSWORD))
    for username, password in attempts:
        response = _login(anon, username, password)
        assert response.status_code == 401
        assert "Wrong username or password." in response.text
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert store.logins == []


def test_login_page_escapes_the_echoed_username(anon: TestClient) -> None:
    response = _login(anon, username='"><script>x</script>', password="whatever")
    assert response.status_code == 401
    assert "<script>x" not in response.text
    assert "&quot;&gt;&lt;script&gt;" in response.text


def test_login_rejects_bad_forms_and_cross_site_posts(anon: TestClient) -> None:
    assert _login(anon, password="").status_code == 400
    assert anon.post("/login", json={"username": "rider"}).status_code == 415
    big = anon.post(
        "/login",
        content="username=rider&password=" + "a" * 5000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 413
    cross = anon.post(
        "/login",
        data={"username": "rider", "password": PASSWORD},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert cross.status_code == 403
    same = anon.post(
        "/login",
        data={"username": "rider", "password": PASSWORD},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert same.status_code == 303


def test_browser_form_post_with_null_origin_uses_sec_fetch_site(anon: TestClient) -> None:
    # Origin: null is what browsers send under Referrer-Policy: no-referrer.
    def post(**headers: str) -> int:
        return anon.post(
            "/login",
            data={"username": "rider", "password": PASSWORD},
            headers=headers,
            follow_redirects=False,
        ).status_code

    assert post(origin="null", **{"sec-fetch-site": "same-origin"}) == 303
    assert post(origin="null", **{"sec-fetch-site": "cross-site"}) == 403
    assert post(origin="null", **{"sec-fetch-site": "same-site"}) == 403
    assert post(origin="http://testserver", **{"sec-fetch-site": "cross-site"}) == 403
    assert post(origin="null") == 403  # no Sec-Fetch-Site: can't tell
    # Plain HTTP: no Sec-Fetch-Site, real Origin (pages use same-origin policy).
    assert post(origin="http://testserver") == 303


def test_pages_keep_the_origin_for_their_own_form_posts(anon: TestClient) -> None:
    from app.api.dashboard import WEB_DIR

    assert anon.get("/login").headers["referrer-policy"] == "same-origin"
    for name in ("index.html", "login.html"):
        html = (WEB_DIR / name).read_text()
        assert '<meta name="referrer" content="same-origin">' in html, name


def test_repeated_failures_are_throttled(anon: TestClient) -> None:
    for _ in range(5):
        assert _login(anon, password="wrong password!").status_code == 401
    blocked = _login(anon)  # even the right password, until the window passes
    assert blocked.status_code == 429
    assert _login(anon, username="other", password="wrong password!").status_code == 401


def test_logout_clears_the_session(anon: TestClient) -> None:
    _login(anon)
    response = anon.post("/logout", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "login"
    assert f'{SESSION_COOKIE}=""' in response.headers["set-cookie"]
    assert anon.get("/api/session").status_code == 401


def test_session_ends_when_password_changes_or_user_disabled(
    anon: TestClient, store: FakeUserStore
) -> None:
    _login(anon)
    assert anon.get("/api/session").status_code == 200
    store.users[1] = rider(password_hash=hash_password("a brand new password"))
    assert anon.get("/api/session").status_code == 401
    store.users[1] = rider()
    assert anon.get("/api/session").status_code == 200
    store.users[1] = rider(is_active=False)
    assert anon.get("/api/session").status_code == 401


def test_forged_and_foreign_cookies_are_refused(app: FastAPI) -> None:
    other = SessionSigner("z" * 40).issue(1, _known_hash(), 3600)
    client = TestClient(app, cookies={SESSION_COOKIE: other})
    assert client.get("/api/session").status_code == 401
    client = TestClient(app, cookies={SESSION_COOKIE: "garbage"})
    assert client.get("/api/session").status_code == 401


def test_secure_cookie_when_configured() -> None:
    application = create_app(make_unit_settings(auth_secret_key=SECRET, auth_cookie_secure=True))
    application.state.users = FakeUserStore(rider())
    response = _login(TestClient(application))
    assert "Secure" in response.headers["set-cookie"]


def test_short_secret_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="AUTH_SECRET_KEY"):
        make_unit_settings(auth_secret_key="short")
