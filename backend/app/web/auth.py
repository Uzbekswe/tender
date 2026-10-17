"""Who may use the app (app/web/auth.py): accounts, or one shared password, or nothing.

Settings pick the mode:

- ACCOUNTS=1: accounts (app/accounts.py). Visitors sign up with an email and their own password. With
  SIGNUP_CODE set they must also type that invite code. The first account created is the admin. A
  project belongs to the account that created it: other accounts get 404 for it (web and /api/v1); the
  admin sees all. ACCESS_PASSWORD is ignored in this mode.
- ACCESS_PASSWORD set (ACCOUNTS off): one shared password for everybody.
- neither: the app is open, as before.

Public paths, in both modes: /static/*, /health, /login, /logout, /lang/*, /theme*. In account mode also
/signup. With DEMO_PUBLIC=1 also: GET / (start page), POST /demo/*, GET /demo/samples.zip, and GET or
HEAD requests that read a demo project (settings.demo = true): its pages, fragments and downloads, and
the GET part of /api/v1 for it. Demo projects are open to read only for anonymous visitors; a signed-in
user may also open them (and a write to a demo project is refused by the app, as before).

A request that matches no rule needs a signed-in user (or the shared-password cookie). Unauthenticated
HTML requests are redirected to /login?next=..., API requests get 401 JSON, HTMX requests get 401 with
HX-Redirect.

Ownership (account mode): every id in the path (/projects/{id}, /runs/{id}, /packages/{id},
/documents/{id}, /pages/{id}, /requirements/{id}, /match-results/{id}, ...) is mapped to its project.
If that project belongs to another account, the request gets 404, the same as for a missing project.
Lists (/supplier, /customer, GET /api/v1/projects) and new projects are handled in the routes with
app/accounts.py.

Shared-password cookie `ts_auth`: `<expiry unix time>.<HMAC-SHA256 hex>`. The key is SESSION_SECRET, or
else a key derived from ACCESS_PASSWORD (so changing the password signs everyone out).
Account cookie `ts_auth`: `<user id>.<expiry unix time>.<HMAC-SHA256 hex>`. The key is SESSION_SECRET
plus the user's stored password hash, so changing that hash or deleting the user signs them out.
Both are HttpOnly, SameSite=Lax, Secure when the request came over https, and last 30 days.

Login attempts are limited per client address: 5 wrong passwords (or wrong invite codes) within 60
seconds lock the form for the rest of that minute. The address is the socket peer, which a client
cannot fake. Behind a tunnel or a proxy every visitor shares one address, so the limit is shared too.
"""

import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from app.accounts import (
    MIN_PASSWORD,
    CurrentUser,
    current_user,
    dummy_password_hash,
    hash_password,
    normalize_email,
    verify_password,
)
from app.db import get_engine
from app.models import ChecklistVersion, Document, MatchResult, Package, Page, Project, Requirement, Run, User
from app.settings import Settings, get_settings
from app.web.i18n import LANG_COOKIE, pick_lang, translator
from app.web.routes import THEME_COOKIE, THEMES, templates

COOKIE_NAME = "ts_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW_S = 60.0
READ_METHODS = ("GET", "HEAD")
ACCOUNT_PUBLIC_PATHS = ("/login", "/logout", "/signup")
_ACCOUNT_LABEL = b"tender-sverka account cookie"

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


# ---------- shared-password cookie ----------

def _signing_key(settings: Settings) -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode("utf-8")
    return hmac.new(settings.access_password.encode("utf-8"), b"tender-sverka login cookie", hashlib.sha256).digest()


def _sign(key: bytes, expires: int) -> str:
    return hmac.new(key, str(expires).encode("ascii"), hashlib.sha256).hexdigest()


def make_cookie_value(settings: Settings, now: float | None = None) -> str:
    expires = int(time.time() if now is None else now) + COOKIE_MAX_AGE
    return f"{expires}.{_sign(_signing_key(settings), expires)}"


def cookie_is_valid(settings: Settings, value: str | None, now: float | None = None) -> bool:
    if not value or not settings.access_password:
        return False
    expires_raw, sep, signature = value.partition(".")
    if not sep or not expires_raw.isascii() or not expires_raw.isdigit() or len(expires_raw) > 12:
        return False
    expires = int(expires_raw)
    if expires <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(signature.encode("utf-8"), _sign(_signing_key(settings), expires).encode("ascii"))


def password_matches(settings: Settings, given: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), settings.access_password.encode("utf-8"))


# ---------- account cookie ----------

def _account_key(settings: Settings, password_hash: str) -> bytes:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        _ACCOUNT_LABEL + b"|" + password_hash.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _account_signature(settings: Settings, user_id: int, expires: int, password_hash: str) -> str:
    message = f"{user_id}.{expires}".encode("ascii")
    return hmac.new(_account_key(settings, password_hash), message, hashlib.sha256).hexdigest()


def make_account_cookie(settings: Settings, user_id: int, password_hash: str, now: float | None = None) -> str:
    expires = int(time.time() if now is None else now) + COOKIE_MAX_AGE
    return f"{user_id}.{expires}.{_account_signature(settings, user_id, expires, password_hash)}"


def _digits(raw: str) -> bool:
    return raw.isascii() and raw.isdigit() and len(raw) <= 12


def account_user_from_cookie(settings: Settings, value: str | None, now: float | None = None) -> CurrentUser | None:
    """The signed-in account of a cookie, or None (unknown user, bad signature, expired, malformed)."""
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    user_raw, expires_raw, signature = parts
    if not (_digits(user_raw) and _digits(expires_raw)) or not signature.isascii():
        return None
    expires = int(expires_raw)
    if expires <= (time.time() if now is None else now):
        return None
    with Session(get_engine()) as session:
        user = session.get(User, int(user_raw))
        if user is None:
            return None
        expected = _account_signature(settings, user.id, expires, user.password_hash)
        if not hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii")):
            return None
        return CurrentUser(id=user.id, email=user.email, is_admin=user.is_admin)


# ---------- login attempt limit (in memory, per process) ----------

def _is_locked(key: str, now: float) -> bool:
    with _failures_lock:
        recent = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW_S]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        return len(recent) >= MAX_FAILURES


def _record_failure(key: str, now: float) -> None:
    with _failures_lock:
        _failures.setdefault(key, []).append(now)


def _clear_failures(key: str) -> None:
    with _failures_lock:
        _failures.pop(key, None)


def reset_login_limits() -> None:
    """Forget all failed attempts. For tests."""
    with _failures_lock:
        _failures.clear()


# ---------- resources in a path and their project ----------

def _package_project_id(session: Session, package_id: int) -> int | None:
    package = session.get(Package, package_id)
    return package.project_id if package else None


def _run_project_id(session: Session, run_id: int) -> int | None:
    run = session.get(Run, run_id)
    return _package_project_id(session, run.package_id) if run else None


def _document_project_id(session: Session, document_id: int) -> int | None:
    document = session.get(Document, document_id)
    return _package_project_id(session, document.package_id) if document else None


def _page_project_id(session: Session, page_id: int) -> int | None:
    page = session.get(Page, page_id)
    return _document_project_id(session, page.document_id) if page else None


def _requirement_project_id(session: Session, requirement_id: int) -> int | None:
    row = session.get(Requirement, requirement_id)
    version = session.get(ChecklistVersion, row.checklist_version_id) if row else None
    return version.project_id if version else None


def _match_project_id(session: Session, match_id: int) -> int | None:
    match = session.get(MatchResult, match_id)
    return _run_project_id(session, match.run_id) if match else None


# kind word in a path -> how its id leads to the project. "bidders/{id}" holds a package id.
OWNER_LOOKUPS: dict[str, Callable[[Session, int], int | None]] = {
    "projects": lambda _session, project_id: project_id,
    "packages": _package_project_id,
    "bidders": _package_project_id,
    "runs": _run_project_id,
    "documents": _document_project_id,
    "pages": _page_project_id,
    "requirements": _requirement_project_id,
    "match-results": _match_project_id,
}


def _resources(path: str) -> list[tuple[str, int]]:
    """(kind, id) for every `kind/<digits>` pair in a path, e.g. /projects/3/packages/5/... or /api/v1/runs/7."""
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["api", "v1"]:
        parts = parts[2:]
    return [
        (kind, int(raw))
        for kind, raw in zip(parts, parts[1:])
        if kind in OWNER_LOOKUPS and raw.isascii() and raw.isdigit()
    ]


def _project_of(session: Session, kind: str, rid: int) -> Project | None:
    project_id = OWNER_LOOKUPS[kind](session, rid)
    return session.get(Project, project_id) if project_id is not None else None


def _projects_on_path(path: str) -> list[Project | None]:
    """The project of each resource id in the path (None when the id does not exist)."""
    with Session(get_engine()) as session:
        return [_project_of(session, kind, rid) for kind, rid in _resources(path)]


def _is_demo_project(project: Project | None) -> bool:
    return project is not None and isinstance(project.settings, dict) and project.settings.get("demo") is True


def _reads_demo_project(path: str) -> bool:
    """Shared-password mode: the first resource in the path is a demo project."""
    resources = _resources(path)
    if not resources:
        return False
    with Session(get_engine()) as session:
        return _is_demo_project(_project_of(session, *resources[0]))


def _only_demo_projects(path: str) -> bool:
    """Account mode, anonymous visitor: every resource in the path is a demo project."""
    projects = _projects_on_path(path)
    return bool(projects) and all(_is_demo_project(project) for project in projects)


def _owned_by_someone_else(user: CurrentUser, path: str) -> bool:
    """True when a resource in the path belongs to a project of another account (demo projects are shared)."""
    for project in _projects_on_path(path):
        if project is None or _is_demo_project(project):
            continue  # a missing project is a 404 from the route itself
        if user.is_admin or project.owner_id == user.id:
            continue
        return True
    return False


def _is_public(method: str, path: str, settings: Settings) -> bool:
    if path in ("/login", "/logout"):
        return True
    if method in READ_METHODS and (path == "/health" or path.startswith(("/static/", "/lang/", "/theme"))):
        return True
    if settings.demo_public:
        if method in READ_METHODS and path == "/":
            return True
        if method == "POST" and path.startswith("/demo/"):
            return True
        if method == "GET" and path == "/demo/samples.zip":
            return True
    return False


def _next_target(request: Request) -> str:
    if request.method not in READ_METHODS:
        return "/"
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _deny(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        detail = "Sign in required" if get_settings().accounts else "Access password required"
        return JSONResponse({"detail": detail}, status_code=401)
    login_url = f"/login?next={quote(_next_target(request), safe='')}"
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "not found"}, status_code=404)


class AccessGate:
    """Pure ASGI middleware. Settings are read on each request, so tests can change them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        if settings.accounts:
            await self._check_account(scope, receive, send, settings)
            return
        method, path = scope["method"], scope["path"]
        if not settings.access_password or _is_public(method, path, settings):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if cookie_is_valid(settings, request.cookies.get(COOKIE_NAME)):
            await self.app(scope, receive, send)
            return
        if settings.demo_public and method in READ_METHODS and await run_in_threadpool(_reads_demo_project, path):
            await self.app(scope, receive, send)
            return
        await _deny(request)(scope, receive, send)

    async def _check_account(self, scope: Scope, receive: Receive, send: Send, settings: Settings) -> None:
        request = Request(scope)
        method, path = scope["method"], scope["path"]
        cookie = request.cookies.get(COOKIE_NAME)
        user = await run_in_threadpool(account_user_from_cookie, settings, cookie) if cookie else None
        scope.setdefault("state", {})["user"] = user  # read by routes through app.accounts.current_user
        if path in ACCOUNT_PUBLIC_PATHS or _is_public(method, path, settings):
            await self.app(scope, receive, send)
            return
        if user is None:
            demo_read = (
                settings.demo_public
                and method in READ_METHODS
                and await run_in_threadpool(_only_demo_projects, path)
            )
            if not demo_read:
                await _deny(request)(scope, receive, send)
                return
        elif await run_in_threadpool(_owned_by_someone_else, user, path):
            await _not_found()(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------- login, signup and logout pages ----------

router = APIRouter(tags=["auth"])


def _safe_next(raw: str | None) -> str:
    """Only a path on this site. Anything else (absolute URLs, //host, the login and signup pages) goes to /."""
    if not raw or not raw.startswith("/") or raw.startswith(("//", "/\\", "/login", "/signup")):
        return "/"
    if "\r" in raw or "\n" in raw:
        return "/"
    return raw


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _request_scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
    return forwarded.split(",")[0].strip().lower()


def _set_session_cookie(request: Request, response: Response, value: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_request_scheme(request) == "https",
        path="/",
    )


def _login_view(
    request: Request,
    *,
    next_target: str,
    status_code: int = 200,
    error_key: str | None = None,
    template: str = "login.html",
    accounts: bool = False,
    email: str = "",
) -> HTMLResponse:
    lang = pick_lang(request.cookies.get(LANG_COOKIE))
    t = translator(lang)
    raw_theme = request.cookies.get(THEME_COOKIE)
    query = request.url.query
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        template,
        {
            "active": "login",
            "lang": lang,
            "t": t,
            "theme": raw_theme if raw_theme in THEMES else None,
            "next_url": f"{request.url.path}?{query}" if query else request.url.path,
            "next_value": next_target,
            "error": t(error_key) if error_key else None,
            "demo_public": settings.demo_public,
            "accounts": accounts,
            "email": email,
            "invite_required": bool(settings.signup_code),
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, next_url: str = Query("/", alias="next")) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
    if settings.accounts:
        if current_user(request) is not None:
            return RedirectResponse(target, status_code=303)
        return _login_view(request, next_target=target, accounts=True)
    if not settings.access_password or cookie_is_valid(settings, request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(target, status_code=303)
    return _login_view(request, next_target=target)


@router.post("/login", response_model=None)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next_url: str = Form("/", alias="next"),
) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
    if settings.accounts:
        return _account_login(request, settings, target, email, password)
    if not settings.access_password:
        return RedirectResponse(target, status_code=303)
    address = _client_address(request)
    now = time.time()
    if _is_locked(address, now):
        return _login_view(request, next_target=target, status_code=429, error_key="login.locked")
    if not password_matches(settings, password):
        _record_failure(address, now)
        return _login_view(request, next_target=target, status_code=401, error_key="login.error")
    _clear_failures(address)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(request, response, make_cookie_value(settings))
    return response


def _account_login(request: Request, settings: Settings, target: str, email: str, password: str) -> Response:
    key = f"login:{_client_address(request)}"
    now = time.time()
    if _is_locked(key, now):
        return _login_view(request, next_target=target, status_code=429, error_key="login.locked",
                           accounts=True, email=email)
    normalized = normalize_email(email)
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.email == normalized)).first() if normalized else None
        # The hash is checked even for an unknown email, so both cases take the same time.
        ok = verify_password(password, user.password_hash if user is not None else dummy_password_hash())
        found = (user.id, user.password_hash) if user is not None and ok else None
    if found is None:
        _record_failure(key, now)
        return _login_view(request, next_target=target, status_code=401, error_key="account.login.error",
                           accounts=True, email=email)
    _clear_failures(key)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(request, response, make_account_cookie(settings, *found))
    return response


@router.get("/signup", response_class=HTMLResponse, response_model=None)
def signup_page(request: Request, next_url: str = Query("/", alias="next")) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
    if not settings.accounts:
        return RedirectResponse("/", status_code=303)
    if current_user(request) is not None:
        return RedirectResponse(target, status_code=303)
    return _login_view(request, next_target=target, template="signup.html", accounts=True)


@router.post("/signup", response_model=None)
def signup_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    invite_code: str = Form(""),
    next_url: str = Form("/", alias="next"),
) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
    if not settings.accounts:
        return RedirectResponse("/", status_code=303)
    key = f"signup:{_client_address(request)}"
    now = time.time()

    def refuse(status_code: int, error_key: str) -> Response:
        return _login_view(request, next_target=target, status_code=status_code, error_key=error_key,
                           template="signup.html", accounts=True, email=email)

    if _is_locked(key, now):
        return refuse(429, "login.locked")
    if settings.signup_code and not hmac.compare_digest(
        invite_code.strip().encode("utf-8"), settings.signup_code.encode("utf-8")
    ):
        _record_failure(key, now)
        return refuse(403, "account.error.invite")
    normalized = normalize_email(email)
    if normalized is None:
        return refuse(422, "account.error.email")
    if len(password) < MIN_PASSWORD:
        return refuse(422, "account.error.password_short")
    if password != password_repeat:
        return refuse(422, "account.error.password_mismatch")
    with Session(get_engine()) as session:
        if session.exec(select(User).where(User.email == normalized)).first() is not None:
            _record_failure(key, now)
            return refuse(409, "account.error.email_taken")
        # The first account ever created is the admin.
        is_first = session.exec(select(func.count(User.id))).one() == 0
        user = User(email=normalized, password_hash=hash_password(password), is_admin=is_first)
        session.add(user)
        try:
            session.commit()
        except IntegrityError:  # another sign-up with the same email won the race
            session.rollback()
            _record_failure(key, now)
            return refuse(409, "account.error.email_taken")
        session.refresh(user)
        found = (user.id, user.password_hash)
    _clear_failures(key)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(request, response, make_account_cookie(settings, *found))
    return response


@router.api_route("/logout", methods=["GET", "POST"], response_model=None)
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax")
    return response
