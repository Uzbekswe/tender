"""Accounts (ACCOUNTS=1): sign-up, log-in, log-out, ownership of projects, demo access, and the old modes.

Settings are changed with monkeypatch env and get_settings.cache_clear(), as in test_access.py.
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.accounts import hash_password, verify_password
from app.db import session_scope
from app.models import (
    ChecklistVersion,
    Document,
    MatchResult,
    Package,
    PackageRole,
    Page,
    Project,
    Requirement,
    Run,
    RunKind,
    RunStatus,
    Scope,
    User,
)
from app.settings import get_settings
from app.web import auth
from app.web.i18n import translator

PASSWORD = "correct-horse-battery"
COOKIE = auth.COOKIE_NAME
Configure = Callable[..., None]


@pytest.fixture(autouse=True)
def _fresh_login_limits() -> Iterator[None]:
    auth.reset_login_limits()
    yield
    auth.reset_login_limits()


@pytest.fixture
def accounts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[Configure]:
    client.follow_redirects = False

    def configure(*, signup_code: str = "", demo_public: bool = False, accounts_on: bool = True,
                  access_password: str = "") -> None:
        monkeypatch.setenv("ACCOUNTS", "1" if accounts_on else "0")
        monkeypatch.setenv("SIGNUP_CODE", signup_code)
        monkeypatch.setenv("DEMO_PUBLIC", "1" if demo_public else "0")
        monkeypatch.setenv("ACCESS_PASSWORD", access_password)
        get_settings.cache_clear()

    configure()
    yield configure
    client.cookies.clear()


def _sign_up(client: TestClient, email: str, password: str = PASSWORD, invite: str | None = None) -> str:
    """Sign up and return the session cookie value."""
    data = {"email": email, "password": password, "password_repeat": password}
    if invite is not None:
        data["invite_code"] = invite
    response = client.post("/signup", data=data)
    assert response.status_code == 303, response.text
    value = client.cookies[COOKIE]
    client.cookies.clear()
    return value


def _as(value: str) -> dict[str, str]:
    return {COOKIE: value}


def _user_id(email: str) -> int:
    with session_scope() as s:
        return s.exec(select(User).where(User.email == email)).one().id


def _make_project(owner_id: int | None, name: str, *, demo: bool = False, mode: str = "supplier") -> int:
    with session_scope() as s:
        project = Project(name=name, owner_id=owner_id, mode=mode, settings={"demo": True} if demo else {})
        s.add(project)
        s.flush()
        s.add(Package(project_id=project.id, role=PackageRole.tender, label="Tender"))
        s.add(Package(project_id=project.id, role=PackageRole.bid, label="Bid"))
        s.flush()
        return project.id


def _project_owner(project_id: int) -> int | None:
    with session_scope() as s:
        return s.get(Project, project_id).owner_id


def _bid_chain(project_id: int) -> dict[str, int]:
    """A finished bid run with a document, a page, a checklist requirement and a match result."""
    with session_scope() as s:
        bid = s.exec(select(Package).where(Package.project_id == project_id, Package.role == PackageRole.bid)).one()
        run = Run(package_id=bid.id, kind=RunKind.bid, status=RunStatus.done)
        s.add(run)
        document = Document(package_id=bid.id, file_name="a.pdf", stored_path="x/a.pdf", sha256="0" * 64)
        s.add(document)
        s.flush()
        page = Page(document_id=document.id, number=1)
        version = ChecklistVersion(project_id=project_id)
        s.add_all([page, version])
        s.flush()
        requirement = Requirement(checklist_version_id=version.id, code="T-001", scope=Scope.goods,
                                  category="general", parameter="Price")
        s.add(requirement)
        s.flush()
        match = MatchResult(run_id=run.id, requirement_id=requirement.id)
        s.add(match)
        s.flush()
        return {"run": run.id, "package": bid.id, "document": document.id, "page": page.id,
                "requirement": requirement.id, "match": match.id}


# ---------- passwords ----------

def test_password_is_stored_as_scrypt_and_verifies() -> None:
    stored = hash_password("abcdefgh")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(bytes.fromhex(salt)) == 16
    assert verify_password("abcdefgh", stored)
    assert not verify_password("abcdefgi", stored)
    assert not verify_password("abcdefgh", "garbage")
    assert hash_password("abcdefgh") != stored  # a fresh salt each time


# ---------- sign-up ----------

def test_sign_up_logs_in_and_redirects_to_next(accounts: Configure, client: TestClient) -> None:
    accounts()
    response = client.post("/signup", data={"email": "Ann@Example.com", "password": PASSWORD,
                                            "password_repeat": PASSWORD, "next": "/supplier"})
    assert response.status_code == 303
    assert response.headers["location"] == "/supplier"
    assert COOKIE in response.headers["set-cookie"]
    assert client.get("/supplier").status_code == 200
    with session_scope() as s:
        assert s.exec(select(User).where(User.email == "ann@example.com")).one() is not None  # lower-case


def test_duplicate_email_is_refused_whatever_the_case(accounts: Configure, client: TestClient) -> None:
    accounts()
    _sign_up(client, "ann@example.com")
    response = client.post("/signup", data={"email": "ANN@example.com", "password": PASSWORD,
                                            "password_repeat": PASSWORD})
    assert response.status_code == 409
    assert translator("ru")("account.error.email_taken") in response.text
    assert COOKIE not in response.cookies
    with session_scope() as s:
        assert len(s.exec(select(User)).all()) == 1


def test_short_password_and_mismatch_are_refused(accounts: Configure, client: TestClient) -> None:
    accounts()
    short = client.post("/signup", data={"email": "b@example.com", "password": "short",
                                         "password_repeat": "short"})
    assert short.status_code == 422
    assert translator("ru")("account.error.password_short") in short.text
    mismatch = client.post("/signup", data={"email": "b@example.com", "password": PASSWORD,
                                            "password_repeat": PASSWORD + "x"})
    assert mismatch.status_code == 422
    assert translator("ru")("account.error.password_mismatch") in mismatch.text
    bad_email = client.post("/signup", data={"email": "not-an-email", "password": PASSWORD,
                                             "password_repeat": PASSWORD})
    assert bad_email.status_code == 422
    with session_scope() as s:
        assert s.exec(select(User)).all() == []


def test_invite_code_is_required_when_signup_code_is_set(accounts: Configure, client: TestClient) -> None:
    accounts(signup_code="letmein-tester")
    page = client.get("/signup")
    assert 'name="invite_code"' in page.text
    wrong = client.post("/signup", data={"email": "c@example.com", "password": PASSWORD,
                                         "password_repeat": PASSWORD, "invite_code": "guess"})
    assert wrong.status_code == 403
    assert translator("ru")("account.error.invite") in wrong.text
    assert COOKIE not in wrong.cookies
    missing = client.post("/signup", data={"email": "c@example.com", "password": PASSWORD,
                                           "password_repeat": PASSWORD})
    assert missing.status_code == 403
    right = client.post("/signup", data={"email": "c@example.com", "password": PASSWORD,
                                         "password_repeat": PASSWORD, "invite_code": " letmein-tester "})
    assert right.status_code == 303


def test_invite_field_is_hidden_without_signup_code(accounts: Configure, client: TestClient) -> None:
    accounts(signup_code="")
    assert 'name="invite_code"' not in client.get("/signup").text
    assert _sign_up(client, "open@example.com")  # anyone may sign up


def test_repeated_wrong_invite_codes_lock_sign_up(accounts: Configure, client: TestClient) -> None:
    accounts(signup_code="letmein-tester")
    for _ in range(auth.MAX_FAILURES):
        client.post("/signup", data={"email": "d@example.com", "password": PASSWORD,
                                     "password_repeat": PASSWORD, "invite_code": "guess"})
    locked = client.post("/signup", data={"email": "d@example.com", "password": PASSWORD,
                                          "password_repeat": PASSWORD, "invite_code": "letmein-tester"})
    assert locked.status_code == 429


def test_first_user_is_admin_later_users_are_not(accounts: Configure, client: TestClient) -> None:
    accounts()
    _sign_up(client, "first@example.com")
    _sign_up(client, "second@example.com")
    with session_scope() as s:
        first = s.exec(select(User).where(User.email == "first@example.com")).one()
        second = s.exec(select(User).where(User.email == "second@example.com")).one()
        assert first.is_admin is True
        assert second.is_admin is False


# ---------- log-in and log-out ----------

def test_login_right_and_wrong_password(accounts: Configure, client: TestClient) -> None:
    accounts()
    _sign_up(client, "e@example.com")
    wrong = client.post("/login", data={"email": "e@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == 401
    assert translator("ru")("account.login.error") in wrong.text
    assert COOKIE not in wrong.cookies
    unknown = client.post("/login", data={"email": "nobody@example.com", "password": PASSWORD})
    assert unknown.status_code == 401
    right = client.post("/login", data={"email": "E@example.com", "password": PASSWORD, "next": "/supplier"})
    assert right.status_code == 303 and right.headers["location"] == "/supplier"
    assert COOKIE in right.headers["set-cookie"]


def test_logout_clears_the_session(accounts: Configure, client: TestClient) -> None:
    accounts()
    value = _sign_up(client, "f@example.com")
    assert client.get("/supplier", cookies=_as(value)).status_code == 200
    response = client.get("/logout")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert COOKIE in response.headers["set-cookie"]
    assert client.get("/supplier").status_code == 303  # the jar was cleared by the logout response


def test_forged_or_stale_account_cookie_is_refused(accounts: Configure, client: TestClient) -> None:
    accounts()
    value = _sign_up(client, "g@example.com")
    user_id, expires, signature = value.split(".")
    assert client.get("/supplier", cookies=_as(f"{user_id}.{expires}.{'0' * 64}")).status_code == 303
    assert client.get("/supplier", cookies=_as(f"{int(user_id) + 1}.{expires}.{signature}")).status_code == 303
    assert client.get("/supplier", cookies=_as("garbage")).status_code == 303
    old = auth.make_account_cookie(get_settings(), int(user_id), "scrypt$x", now=1.0)
    assert client.get("/supplier", cookies=_as(old)).status_code == 303


def test_wrong_passwords_lock_log_in(accounts: Configure, client: TestClient) -> None:
    accounts()
    _sign_up(client, "h@example.com")
    for _ in range(auth.MAX_FAILURES):
        client.post("/login", data={"email": "h@example.com", "password": "nope-nope-nope"})
    assert client.post("/login", data={"email": "h@example.com", "password": PASSWORD}).status_code == 429


def test_header_shows_the_email_and_log_out_when_signed_in(accounts: Configure, client: TestClient) -> None:
    accounts()
    value = _sign_up(client, "head@example.com")
    signed_in = client.get("/supplier", cookies=_as(value)).text
    assert "head@example.com" in signed_in
    assert translator("ru")("account.nav.logout") in signed_in
    anonymous = client.get("/login").text
    assert translator("ru")("account.nav.login") in anonymous
    assert "head@example.com" not in anonymous


# ---------- ownership ----------

def test_user_cannot_open_or_list_another_users_project(accounts: Configure, client: TestClient) -> None:
    accounts()
    alice = _sign_up(client, "alice@example.com")
    bob = _sign_up(client, "bob@example.com")
    project_id = _make_project(_user_id("alice@example.com"), "Alice lot")
    assert client.get(f"/projects/{project_id}", cookies=_as(bob)).status_code == 404
    assert client.get(f"/projects/{project_id}/checklist", cookies=_as(bob)).status_code == 404
    assert client.get(f"/api/v1/projects/{project_id}", cookies=_as(bob)).status_code == 404
    assert client.get(f"/api/v1/projects/{project_id}/packages", cookies=_as(bob)).status_code == 404
    assert client.post(f"/projects/{project_id}/deadline", data={}, cookies=_as(bob)).status_code == 404
    assert "Alice lot" not in client.get("/supplier", cookies=_as(bob)).text
    assert "Alice lot" in client.get("/supplier", cookies=_as(alice)).text
    assert client.get(f"/projects/{project_id}", cookies=_as(alice)).status_code == 200
    api_list = client.get("/api/v1/projects", cookies=_as(bob)).json()
    assert project_id not in [p["id"] for p in api_list]
    alice_list = client.get("/api/v1/projects", cookies=_as(alice)).json()
    assert project_id in [p["id"] for p in alice_list]


def test_user_cannot_reach_runs_packages_documents_pages_or_requirements_of_others(
    accounts: Configure, client: TestClient,
) -> None:
    accounts()
    _sign_up(client, "owner@example.com")
    bob = _sign_up(client, "other@example.com")
    project_id = _make_project(_user_id("owner@example.com"), "Owner lot")
    ids = _bid_chain(project_id)
    checks = [
        ("GET", f"/runs/{ids['run']}/compare"),
        ("GET", f"/runs/{ids['run']}/summary"),
        ("GET", f"/api/v1/runs/{ids['run']}"),
        ("GET", f"/api/v1/runs/{ids['run']}/results"),
        ("GET", f"/packages/{ids['package']}/history"),
        ("GET", f"/api/v1/packages/{ids['package']}/documents"),
        ("GET", f"/api/v1/pages/{ids['page']}/image"),
        ("PATCH", f"/api/v1/requirements/{ids['requirement']}"),
        ("DELETE", f"/api/v1/requirements/{ids['requirement']}"),
        ("POST", f"/api/v1/match-results/{ids['match']}/override"),
        ("PATCH", f"/api/v1/documents/{ids['document']}"),
    ]
    for method, url in checks:
        response = client.request(method, url, cookies=_as(bob), json={})
        assert response.status_code == 404, (method, url, response.status_code)


def test_owner_still_reaches_their_run_and_admin_sees_everything(accounts: Configure, client: TestClient) -> None:
    accounts()
    owner = _sign_up(client, "boss@example.com")  # the first account is the admin
    member = _sign_up(client, "member@example.com")
    project_id = _make_project(_user_id("member@example.com"), "Member lot")
    ids = _bid_chain(project_id)
    assert client.get(f"/api/v1/runs/{ids['run']}", cookies=_as(member)).status_code == 200
    assert client.get(f"/projects/{project_id}", cookies=_as(owner)).status_code == 200  # admin
    assert client.get(f"/api/v1/runs/{ids['run']}", cookies=_as(owner)).status_code == 200
    admin_list = client.get("/supplier", cookies=_as(owner)).text
    assert "Member lot" in admin_list


def test_old_projects_without_owner_are_for_the_admin_only(accounts: Configure, client: TestClient) -> None:
    accounts()
    admin = _sign_up(client, "root@example.com")
    member = _sign_up(client, "plain@example.com")
    legacy_id = _make_project(None, "Legacy lot")
    assert client.get(f"/projects/{legacy_id}", cookies=_as(member)).status_code == 404
    assert "Legacy lot" in client.get("/supplier", cookies=_as(admin)).text
    assert "Legacy lot" not in client.get("/supplier", cookies=_as(member)).text


def test_customer_list_is_limited_to_own_projects(accounts: Configure, client: TestClient) -> None:
    accounts()
    _sign_up(client, "cust-admin@example.com")
    value = _sign_up(client, "cust@example.com")
    other = _sign_up(client, "cust2@example.com")
    _make_project(_user_id("cust@example.com"), "Customer mine", mode="customer")
    _make_project(_user_id("cust2@example.com"), "Customer theirs", mode="customer")
    mine = client.get("/customer", cookies=_as(value)).text
    assert "Customer mine" in mine and "Customer theirs" not in mine
    assert "Customer theirs" in client.get("/customer", cookies=_as(other)).text


def test_projects_get_their_owner_on_web_and_api_create(accounts: Configure, client: TestClient) -> None:
    accounts()
    value = _sign_up(client, "maker@example.com")
    web = client.post("/projects", data={"name": "Web made", "mode": "supplier"}, cookies=_as(value))
    assert web.status_code == 303
    web_id = int(web.headers["location"].rsplit("/", 1)[-1])
    assert _project_owner(web_id) == _user_id("maker@example.com")
    api = client.post("/api/v1/projects", json={"name": "Api made"}, cookies=_as(value))
    assert api.status_code == 201
    assert _project_owner(api.json()["id"]) == _user_id("maker@example.com")


def test_anonymous_visitor_is_sent_to_log_in(accounts: Configure, client: TestClient) -> None:
    accounts()
    project_id = _make_project(None, "Anon lot")
    assert client.get("/supplier").status_code == 303
    assert client.get(f"/projects/{project_id}").headers["location"].startswith("/login?next=")
    assert client.get("/api/v1/projects").status_code == 401


# ---------- demo projects stay open with DEMO_PUBLIC ----------

def test_demo_projects_stay_open_and_others_do_not(accounts: Configure, client: TestClient) -> None:
    accounts(demo_public=True)
    demo_id = _make_project(None, "Demo lot", demo=True)
    other_id = _make_project(None, "Private lot")
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{demo_id}").status_code == 200
    assert client.get(f"/api/v1/projects/{demo_id}").status_code == 200
    assert client.get(f"/projects/{other_id}").status_code == 303
    assert client.get(f"/api/v1/projects/{other_id}").status_code == 401
    _sign_up(client, "root-demo@example.com")  # the first account is the admin
    bob = _sign_up(client, "demo-user@example.com")
    assert client.get(f"/projects/{demo_id}", cookies=_as(bob)).status_code == 200
    assert client.get(f"/projects/{other_id}", cookies=_as(bob)).status_code == 404


def test_demo_projects_need_sign_in_without_demo_public(accounts: Configure, client: TestClient) -> None:
    accounts(demo_public=False)
    demo_id = _make_project(None, "Demo lot", demo=True)
    assert client.get("/").status_code == 303
    assert client.get(f"/projects/{demo_id}").status_code == 303


# ---------- the other modes are unchanged ----------

def test_accounts_off_keeps_the_shared_password(accounts: Configure, client: TestClient) -> None:
    accounts(accounts_on=False, access_password=PASSWORD)
    assert client.get("/supplier").status_code == 303
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    assert client.get("/supplier").status_code == 200
    assert client.get("/signup").headers["location"] == "/"
    client.cookies.clear()
    open_app = _make_project(None, "Open lot")
    accounts(accounts_on=False, access_password="")
    assert client.get(f"/projects/{open_app}").status_code == 200


def test_accounts_on_ignores_the_shared_password(accounts: Configure, client: TestClient) -> None:
    accounts(accounts_on=True, access_password=PASSWORD)
    response = client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 401  # no email given: the shared password does not open anything
    assert client.get("/supplier").status_code == 303


def test_existing_database_gets_the_owner_column(data_dir: Path) -> None:
    path = data_dir.parent / "test.db"  # conftest puts the SQLite file here
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE project (
            id INTEGER NOT NULL PRIMARY KEY, company_id INTEGER NOT NULL, name VARCHAR NOT NULL,
            customer VARCHAR, lot_number VARCHAR, subject VARCHAR, submission_deadline DATETIME,
            report_lang VARCHAR NOT NULL, mode VARCHAR NOT NULL, settings JSON NOT NULL,
            created_at DATETIME NOT NULL
        );
        INSERT INTO project (company_id, name, report_lang, mode, settings, created_at)
        VALUES (1, 'Old lot', 'ru', 'supplier', '{}', '2025-01-01 00:00:00');
        """
    )
    con.commit()
    con.close()
    db.init_db()
    con = sqlite3.connect(path)
    columns = [row[1] for row in con.execute("PRAGMA table_info(project)")]
    owner = con.execute("SELECT owner_id FROM project WHERE name = 'Old lot'").fetchone()[0]
    con.close()
    assert "owner_id" in columns
    assert owner is None
