"""Accounts (ACCOUNTS=1): passwords, the signed-in user of a request, and who may see which project.

Passwords use hashlib.scrypt and are stored as "scrypt$n$r$p$salt_hex$hash_hex"; they are checked with
hmac.compare_digest. The login cookie and the sign-up / log-in pages are in app/web/auth.py.

The gate (app/web/auth.py) sets the signed-in user on request.state before any route runs. Routes
use current_user() and visibility_clauses() to limit lists, and owner_for_new_project() to set the
owner of a new project. With ACCOUNTS off nothing here limits anything.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException, Request
from sqlalchemy import false

from app.models import Project
from app.settings import get_settings

MIN_PASSWORD = 8
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    is_admin: bool


def normalize_email(raw: str) -> str | None:
    """Lower-case, trimmed address, or None when it does not look like an email address."""
    value = raw.strip().lower()
    if len(value) > 254 or not _EMAIL.fullmatch(value):
        return None
    return value


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
            maxmem=_SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """Checked when the email is unknown, so a wrong email takes as long as a wrong password."""
    return hash_password("not-the-password-of-any-account")


def current_user(request: Request) -> CurrentUser | None:
    return getattr(request.state, "user", None)


def visibility_clauses(request: Request) -> list:
    """Where clauses that limit the projects a request may list.

    Accounts off: none. Admin: none. Another signed-in user: their own projects only. Nobody signed in
    while accounts are on: nothing (fails closed).
    """
    if not get_settings().accounts:
        return []
    user = current_user(request)
    if user is None:
        return [false()]
    if user.is_admin:
        return []
    return [Project.owner_id == user.id]


def owner_for_new_project(request: Request) -> int | None:
    """The owner of a project created now: the signed-in user, or None with accounts off."""
    if not get_settings().accounts:
        return None
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return user.id
