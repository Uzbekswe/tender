"""Optional access password for the whole app (ACCESS_PASSWORD, DEMO_PUBLIC, SESSION_SECRET).

With ACCESS_PASSWORD empty nothing here changes the app. With it set, every request needs a
signed login cookie `ts_auth` except the public paths:

- always: /static/*, /health, /login, /logout, /lang/*, /theme*
- with DEMO_PUBLIC=1 also: GET / (start page), POST /demo/*, GET /demo/samples.zip, and GET or
  HEAD requests that read a demo project (settings.demo = true): its pages, fragments and
  downloads, and the GET part of /api/v1 for it.

Demo projects are open to read only. Every write (POST, PATCH, DELETE) still needs the cookie.
A request that matches no rule needs the cookie. Unauthenticated HTML requests are redirected
to /login?next=..., API requests get 401 JSON, HTMX requests get 401 with HX-Redirect.

The cookie is `<expiry unix time>.<HMAC-SHA256 hex>`. The key is SESSION_SECRET, or else a key
derived from ACCESS_PASSWORD (so changing the password signs everyone out). It is HttpOnly,
SameSite=Lax, Secure when the request came over https, and lasts 30 days.

Login attempts are limited per client address: 5 wrong passwords within 60 seconds lock the
form for the rest of that minute. The address is the socket peer, which a client cannot fake.
Behind a tunnel or a proxy every visitor shares one address, so the limit is shared too.
"""

import hashlib
import hmac
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import get_engine
from app.models import Document, Package, Page, Project, Run
from app.settings import Settings, get_settings
from app.web.i18n import LANG_COOKIE, pick_lang, translator
from app.web.routes import THEME_COOKIE, THEMES, templates

COOKIE_NAME = "ts_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW_S = 60.0
READ_METHODS = ("GET", "HEAD")
RESOURCE_KINDS = ("projects", "packages", "runs", "pages", "documents")

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


# ---------- cookie and password ----------

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
    return hmac.compare_digest(signature, _sign(_signing_key(settings), expires))


def password_matches(settings: Settings, given: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), settings.access_password.encode("utf-8"))


# ---------- login attempt limit (in memory, per process) ----------

def _is_locked(address: str, now: float) -> bool:
    with _failures_lock:
        recent = [t for t in _failures.get(address, []) if now - t < FAILURE_WINDOW_S]
        if recent:
            _failures[address] = recent
        else:
            _failures.pop(address, None)
        return len(recent) >= MAX_FAILURES


def _record_failure(address: str, now: float) -> None:
    with _failures_lock:
        _failures.setdefault(address, []).append(now)


def _clear_failures(address: str) -> None:
    with _failures_lock:
        _failures.pop(address, None)


def reset_login_limits() -> None:
    """Forget all failed attempts. For tests."""
    with _failures_lock:
        _failures.clear()


# ---------- public paths and demo projects ----------

def _resource(path: str) -> tuple[str, int] | None:
    """(kind, id) for a path such as /projects/3/..., /api/v1/runs/7/... or /pages/9/image."""
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["api", "v1"]:
        parts = parts[2:]
    if len(parts) < 2 or parts[0] not in RESOURCE_KINDS or not (parts[1].isascii() and parts[1].isdigit()):
        return None
    return parts[0], int(parts[1])


def _project_id(session: Session, kind: str, rid: int) -> int | None:
    if kind == "projects":
        return rid
    if kind == "packages":
        package = session.get(Package, rid)
        return package.project_id if package else None
    if kind == "runs":
        run = session.get(Run, rid)
        package = session.get(Package, run.package_id) if run else None
        return package.project_id if package else None
    if kind == "documents":
        document = session.get(Document, rid)
        package = session.get(Package, document.package_id) if document else None
        return package.project_id if package else None
    page = session.get(Page, rid)  # pages
    document = session.get(Document, page.document_id) if page else None
    package = session.get(Package, document.package_id) if document else None
    return package.project_id if package else None


def _reads_demo_project(path: str) -> bool:
    ref = _resource(path)
    if ref is None:
        return False
    with Session(get_engine()) as session:
        project_id = _project_id(session, *ref)
        project = session.get(Project, project_id) if project_id is not None else None
        return bool(project and isinstance(project.settings, dict) and project.settings.get("demo") is True)


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
        return JSONResponse({"detail": "Access password required"}, status_code=401)
    login_url = f"/login?next={quote(_next_target(request), safe='')}"
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


class AccessGate:
    """Pure ASGI middleware. Settings are read on each request, so tests can change them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
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


# ---------- login and logout pages ----------

router = APIRouter(tags=["auth"])


def _safe_next(raw: str | None) -> str:
    """Only a path on this site. Anything else (absolute URLs, //host, the login page) goes to /."""
    if not raw or not raw.startswith("/") or raw.startswith(("//", "/\\", "/login")):
        return "/"
    if "\r" in raw or "\n" in raw:
        return "/"
    return raw


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _request_scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
    return forwarded.split(",")[0].strip().lower()


def _login_view(
    request: Request,
    *,
    next_target: str,
    status_code: int = 200,
    error_key: str | None = None,
) -> HTMLResponse:
    lang = pick_lang(request.cookies.get(LANG_COOKIE))
    t = translator(lang)
    raw_theme = request.cookies.get(THEME_COOKIE)
    query = request.url.query
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "active": "login",
            "lang": lang,
            "t": t,
            "theme": raw_theme if raw_theme in THEMES else None,
            "next_url": f"{request.url.path}?{query}" if query else request.url.path,
            "next_value": next_target,
            "error": t(error_key) if error_key else None,
            "demo_public": get_settings().demo_public,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, next_url: str = Query("/", alias="next")) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
    if not settings.access_password or cookie_is_valid(settings, request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(target, status_code=303)
    return _login_view(request, next_target=target)


@router.post("/login", response_model=None)
def login_submit(
    request: Request,
    password: str = Form(""),
    next_url: str = Form("/", alias="next"),
) -> Response:
    settings = get_settings()
    target = _safe_next(next_url)
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
    response.set_cookie(
        COOKIE_NAME,
        make_cookie_value(settings),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_request_scheme(request) == "https",
        path="/",
    )
    return response


@router.api_route("/logout", methods=["GET", "POST"], response_model=None)
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax")
    return response
