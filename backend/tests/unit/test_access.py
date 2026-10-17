"""Optional access password (app/web/auth.py): the gate, the login page, the cookie and demo projects.

Settings are changed with monkeypatch env and get_settings.cache_clear(), as in tests/conftest.py.
The middleware reads settings on each request, so one client can be switched between modes.
"""

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import session_scope
from app.models import Package, PackageRole, Project, Run, RunKind, RunStatus
from app.settings import get_settings
from app.web import auth
from app.web.i18n import translator

PASSWORD = "correct-horse-battery"
Configure = Callable[..., None]


@pytest.fixture(autouse=True)
def _fresh_login_limits() -> Iterator[None]:
    auth.reset_login_limits()
    yield
    auth.reset_login_limits()


@pytest.fixture
def gate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[Configure]:
    """Switch the app between open and gated. Redirects are not followed, so each one can be checked."""
    client.follow_redirects = False

    def configure(password: str = "", *, demo_public: bool = False, session_secret: str = "") -> None:
        monkeypatch.setenv("ACCESS_PASSWORD", password)
        monkeypatch.setenv("DEMO_PUBLIC", "1" if demo_public else "0")
        monkeypatch.setenv("SESSION_SECRET", session_secret)
        get_settings.cache_clear()

    yield configure
    client.cookies.clear()


def _project(*, demo: bool) -> int:
    """A project with a tender and a bid package. Only `settings.demo` differs."""
    with session_scope() as s:
        project = Project(name="Synthetic lot", settings={"demo": True} if demo else {})
        s.add(project)
        s.flush()
        s.add(Package(project_id=project.id, role=PackageRole.tender, label="Tender"))
        s.add(Package(project_id=project.id, role=PackageRole.bid, label="Bid"))
        s.flush()
        return project.id


def _login(client: TestClient, password: str = PASSWORD, next_url: str = "/") -> None:
    response = client.post("/login", data={"password": password, "next": next_url})
    assert response.status_code == 303, response.text


def _tamper(value: str) -> str:
    expires, _, signature = value.partition(".")
    return f"{int(expires) + 1000}.{signature}"


def _project_count() -> int:
    with session_scope() as s:
        return len(s.exec(select(Project)).all())


def _bid_package(session: Session, project_id: int) -> Package:
    return session.exec(
        select(Package).where(Package.project_id == project_id, Package.role == PackageRole.bid)
    ).one()


def _run(project_id: int) -> tuple[int, int]:
    """A finished bid run in the project. Returns (run id, package id)."""
    with session_scope() as s:
        bid = _bid_package(s, project_id)
        run = Run(package_id=bid.id, kind=RunKind.bid, status=RunStatus.done)
        s.add(run)
        s.flush()
        return run.id, bid.id


# ---------- no password: the app is open, as before ----------

def test_no_password_leaves_every_page_open(gate: Configure, client: TestClient) -> None:
    gate("")
    assert client.get("/").status_code == 200
    assert client.get("/supplier").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/login").status_code == 303  # the login page only redirects when there is nothing to log in to


# ---------- password set: the gate ----------

def test_html_page_redirects_to_login_with_next(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.get("/supplier")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsupplier"


def test_api_request_gets_401_json(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.json() == {"detail": "Access password required"}


def test_htmx_request_gets_hx_redirect(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.get("/supplier", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"].startswith("/login?next=")


def test_public_paths_stay_open(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/login").status_code == 200
    lang = client.get("/lang/en")
    assert lang.status_code == 303 and lang.headers["location"] == "/"


def test_login_page_is_translated(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.get("/login?next=/supplier", cookies={"lang": "en"})
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert 'name="next" value="/supplier"' in response.text
    assert translator("en")("login.lead") in response.text


def test_wrong_password_shows_error_and_sets_no_cookie(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.post("/login", data={"password": "nope", "next": "/supplier"})
    assert response.status_code == 401
    assert translator("ru")("login.error") in response.text
    assert auth.COOKIE_NAME not in response.cookies
    assert client.get("/supplier").status_code == 303


def test_right_password_sets_cookie_and_opens_the_app(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.post("/login", data={"password": PASSWORD, "next": "/supplier"})
    assert response.status_code == 303
    assert response.headers["location"] == "/supplier"
    set_cookie = response.headers["set-cookie"]
    assert f"{auth.COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()  # plain http in the test client
    assert "Max-Age=2592000" in set_cookie  # 30 days
    assert client.get("/supplier").status_code == 200


def test_cookie_is_secure_behind_https(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "secure" in response.headers["set-cookie"].lower()


def test_tampered_cookie_is_rejected(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    _login(client)
    valid = client.cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: valid}).status_code == 200
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: _tamper(valid)}).status_code == 303
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: "garbage"}).status_code == 303
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: "1.2"}).status_code == 303


def test_expired_cookie_is_rejected(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    settings = get_settings()
    old = auth.make_cookie_value(settings, now=time.time() - auth.COOKIE_MAX_AGE - 60)
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: old}).status_code == 303


def test_changing_the_password_signs_everyone_out(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    _login(client)
    cookie = client.cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    gate("another-password")
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: cookie}).status_code == 303


def test_session_secret_signs_the_cookie(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, session_secret="first-secret")
    _login(client)
    cookie = client.cookies[auth.COOKIE_NAME]
    client.cookies.clear()
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: cookie}).status_code == 200
    gate(PASSWORD, session_secret="second-secret")
    assert client.get("/supplier", cookies={auth.COOKIE_NAME: cookie}).status_code == 303


def test_logout_clears_the_cookie(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    _login(client)
    response = client.get("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert auth.COOKIE_NAME in response.headers["set-cookie"]
    client.cookies.clear()
    assert client.get("/supplier").status_code == 303


def test_next_must_stay_on_this_site(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    for bad in ("https://evil.example/", "//evil.example/", "/\\evil.example", "/login?next=/x"):
        response = client.post("/login", data={"password": PASSWORD, "next": bad})
        assert response.headers["location"] == "/", bad
    assert auth._safe_next(None) == "/"
    assert auth._safe_next("/projects/3?x=1") == "/projects/3?x=1"


def test_repeated_wrong_passwords_lock_the_form(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    locked = client.post("/login", data={"password": "nope"})
    assert locked.status_code == 429
    assert translator("ru")("login.locked") in locked.text
    # The right password is refused too while the lock lasts.
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429
    auth.reset_login_limits()
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303


def test_successful_login_clears_the_failure_count(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD)
    for _ in range(auth.MAX_FAILURES - 1):
        client.post("/login", data={"password": "nope"})
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    for _ in range(auth.MAX_FAILURES - 1):
        assert client.post("/login", data={"password": "nope"}).status_code == 401


# ---------- DEMO_PUBLIC: demo pages stay open, writes and other projects do not ----------

def test_demo_public_opens_start_page_and_demo_project(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    demo_id = _project(demo=True)
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{demo_id}").status_code == 200
    assert client.get(f"/projects/{demo_id}/checklist").status_code == 200
    assert client.get(f"/api/v1/projects/{demo_id}").status_code == 200


def test_demo_public_keeps_other_projects_and_lists_closed(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    _project(demo=True)
    other_id = _project(demo=False)
    assert client.get(f"/projects/{other_id}").status_code == 303
    assert client.get(f"/projects/{other_id}/checklist").status_code == 303
    assert client.get(f"/api/v1/projects/{other_id}").status_code == 401
    assert client.get("/supplier").status_code == 303
    assert client.get("/customer").status_code == 303
    assert client.get("/api/v1/projects").status_code == 401
    assert client.get("/projects/999999").status_code == 303  # unknown ids stay closed


def test_demo_public_refuses_writes_to_demo_projects(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    demo_id = _project(demo=True)
    before = _project_count()
    create = client.post("/projects", data={"name": "New", "mode": "supplier"})
    assert create.status_code == 303 and create.headers["location"].startswith("/login")
    write = client.post(f"/projects/{demo_id}/bidders", json={"name": "X"})
    assert write.status_code == 303 and write.headers["location"].startswith("/login")
    api_write = client.post("/api/v1/projects", json={"name": "New"})
    assert api_write.status_code == 401
    assert _project_count() == before


def test_demo_public_opens_demo_sample_and_demo_start_routes(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    # The routes come from the demo feature. Whether they exist yet or not, the gate must not send them to login.
    for response in (client.get("/demo/samples.zip"), client.post("/demo/supplier")):
        assert response.status_code != 401
        assert "/login" not in response.headers.get("location", "")


def test_without_demo_public_the_demo_project_needs_the_cookie(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=False)
    demo_id = _project(demo=True)
    assert client.get("/").status_code == 303
    assert client.get(f"/projects/{demo_id}").status_code == 303
    assert client.get("/demo/samples.zip").status_code == 303


def test_login_page_mentions_the_demo_when_it_is_public(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    assert translator("ru")("login.demo_note") in client.get("/login").text


def test_run_and_package_ids_resolve_to_their_project(gate: Configure) -> None:
    gate(PASSWORD, demo_public=True)
    demo_id = _project(demo=True)
    other_id = _project(demo=False)
    demo_run_id, demo_package_id = _run(demo_id)
    other_run_id, other_package_id = _run(other_id)
    assert auth._reads_demo_project(f"/runs/{demo_run_id}/compare")
    assert auth._reads_demo_project(f"/api/v1/runs/{demo_run_id}/results")
    assert auth._reads_demo_project(f"/packages/{demo_package_id}/history")
    assert not auth._reads_demo_project(f"/runs/{other_run_id}/compare")
    assert not auth._reads_demo_project(f"/packages/{other_package_id}/history")
    assert not auth._reads_demo_project("/projects/abc")
    assert not auth._reads_demo_project("/runs/999999/compare")


def test_demo_run_is_readable_and_other_run_is_not(gate: Configure, client: TestClient) -> None:
    gate(PASSWORD, demo_public=True)
    demo_run_id, _ = _run(_project(demo=True))
    other_run_id, _ = _run(_project(demo=False))
    demo = client.get(f"/api/v1/runs/{demo_run_id}/results")
    assert demo.status_code != 401
    assert "/login" not in demo.headers.get("location", "")
    other = client.get(f"/api/v1/runs/{other_run_id}/results")
    assert other.status_code == 401


def test_settings_default_to_no_gate(data_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_PASSWORD", "")
    monkeypatch.setenv("DEMO_PUBLIC", "0")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.access_password == ""
    assert settings.demo_public is False
    assert settings.session_secret == ""
    get_settings.cache_clear()
