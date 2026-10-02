"""Passwords, login lockout, session user, roles, active company and CSRF."""

import hmac
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse

from app.db import get_db
from app.enums import Role
from app.models import Company, LoginAttempt, User
from app.weeks import to_db

MIN_PASSWORD_LENGTH = 12
LOCK_THRESHOLD = 10
LOCK_WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=15)
LOCKOUT_MESSAGE = "Too many attempts. Try again in 15 minutes."
WRONG_DETAILS_MESSAGE = "Email or password is incorrect."

_hasher = PasswordHasher()  # argon2id


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def temporary_password() -> str:
    return secrets.token_urlsafe(12)


def password_error(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


# ---------------------------------------------------------------------------
# Login lockout: 10 failures for one email within 15 minutes lock that email
# for 15 minutes after the 10th failure.
# ---------------------------------------------------------------------------


def locked_until(db: Session, email: str, now: datetime) -> datetime | None:
    since = to_db(now) - LOCK_WINDOW - LOCK_DURATION
    failures = db.scalars(
        select(LoginAttempt.attempted_at)
        .where(LoginAttempt.email == email, LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at >= since)
        .order_by(LoginAttempt.attempted_at, LoginAttempt.id)
    ).all()
    until = None
    for i in range(LOCK_THRESHOLD - 1, len(failures)):
        if failures[i] - failures[i - (LOCK_THRESHOLD - 1)] <= LOCK_WINDOW:
            candidate = failures[i] + LOCK_DURATION
            until = candidate if until is None or candidate > until else until
    if until is not None and to_db(now) < until:
        return until
    return None


def record_attempt(db: Session, email: str, now: datetime, succeeded: bool) -> None:
    db.add(LoginAttempt(email=email, attempted_at=to_db(now), succeeded=succeeded))
    db.commit()


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        # Spend the same effort as a real check so timing does not reveal accounts.
        verify_password(_DUMMY_HASH, password)
        return None
    return user if verify_password(user.password_hash, password) else None


_DUMMY_HASH = hash_password("not-a-real-password")


# ---------------------------------------------------------------------------
# Session user, roles and the active company
# ---------------------------------------------------------------------------


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


PASSWORD_CHANGE_PATHS = {"/account", "/logout"}


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        raise LoginRequired()
    if user.must_change_password and request.url.path not in PASSWORD_CHANGE_PATHS:
        raise PasswordChangeRequired()
    return user


def require_operator(user: User = Depends(current_user)) -> User:
    if user.role != Role.OPERATOR:
        raise HTTPException(status_code=403)
    return user


def active_company(request: Request, user: User, db: Session) -> Company | None:
    if user.role == Role.OWNER:
        return db.get(Company, user.company_id)
    company_id = request.session.get("company_id")
    company = db.get(Company, company_id) if company_id else None
    if company is None:
        company = db.scalar(select(Company).order_by(Company.id))
        if company is not None:
            request.session["company_id"] = company.id
    return company


def can_access_company(user: User, company_id: int) -> bool:
    return user.role == Role.OPERATOR or user.company_id == company_id


def scoped_or_404(record, company: Company | None):
    """Tenant isolation: a record from another company is a 404."""
    if record is None or company is None or getattr(record, "company_id", None) != company.id:
        raise HTTPException(status_code=404)
    return record


# ---------------------------------------------------------------------------
# CSRF: a per-session token checked on every POST except POST /r/{token}.
# ---------------------------------------------------------------------------

CSRF_FIELD = "csrf_token"
CSRF_HEADER = b"x-csrf-token"
_MULTIPART_FIELD = re.compile(rb'name="csrf_token"\r\n\r\n([^\r\n]*)')


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def rotate_csrf(request: Request) -> str:
    request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


class CSRFMiddleware:
    """Pure ASGI middleware; must run inside SessionMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].startswith("/r/"):
            await self.app(scope, receive, send)
            return

        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)

        expected = scope.get("session", {}).get("csrf")
        supplied = self._supplied_token(scope, body)
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            response = PlainTextResponse("CSRF token missing or invalid.", status_code=403)
            await response(scope, receive, send)
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _supplied_token(scope, body: bytes) -> str | None:
        headers = dict(scope.get("headers") or [])
        if CSRF_HEADER in headers:
            return headers[CSRF_HEADER].decode("latin-1")
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        if content_type.startswith("application/x-www-form-urlencoded"):
            values = parse_qs(body.decode("utf-8", errors="replace")).get(CSRF_FIELD)
            return values[0] if values else None
        if content_type.startswith("multipart/form-data"):
            match = _MULTIPART_FIELD.search(body)
            return match.group(1).decode("utf-8", errors="replace") if match else None
        return None
