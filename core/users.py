"""
Panel users: passwords, sign-in and sessions.

People and machines authenticate differently on purpose. An API key is 256
random bits, so a fast SHA-256 is enough to store it (core/auth.py). A
password is something a person chose: it has little entropy and is reused, so
it is stored with argon2id, which is slow and memory-hard by design -- every
guess against a stolen table costs real time and memory. That slowness is
paid once per sign-in, not on every request: a sign-in mints a session, and
the session token is random again, so it goes back to SHA-256.

Sign-in failures all look the same from outside -- wrong password, unknown
email, disabled, locked -- so the endpoint cannot be used to find out who has
an account. An unknown email still costs one hash, so it cannot be told apart
by timing either.

Run it with:  uv run python -m core.users create ana@example.com --role admin
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.models import User, UserSession, utcnow

log = logging.getLogger("users")

ROLES = ("author", "admin")

#: Length over complexity rules: those push people to "Password1!", and a
#: long passphrase is both easier to remember and harder to guess.
MIN_PASSWORD = 12

#: Consecutive failures before an account is locked, and for how long. Ten is
#: generous for a person mistyping and nothing for a script.
MAX_FAILURES = 10
LOCKOUT = timedelta(minutes=15)

_hasher = PasswordHasher()


class SignInFailed(Exception):
    """Deliberately one error for every reason. See the module docstring."""


@dataclass(frozen=True)
class SignedIn:
    user: User
    session: UserSession
    #: The cookie value. Exists only here; the table holds its hash.
    token: str


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters")
    return _hasher.hash(password)


def create(
    session: Session, email: str, password: str, *, role: str = "author", name: str | None = None
) -> User:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    email = _normalise(email)
    # The columns' limits, checked here so the CLI says so instead of the
    # database failing the insert.
    if len(email) > 254:
        raise ValueError("an email address is at most 254 characters")
    if name is not None and len(name) > 200:
        raise ValueError("a name is at most 200 characters")
    if session.scalar(select(User).where(User.email == email)) is not None:
        raise ValueError(f"{email} already has an account")
    user = User(email=email, name=name, role=role, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    return user


def sign_in(session: Session, email: str, password: str) -> SignedIn:
    """A new session, or SignInFailed whatever went wrong."""
    user = session.scalar(select(User).where(User.email == _normalise(email)))
    now = utcnow()

    if user is None:
        _verify(_dummy_hash(), password)  # same cost as a real check
        raise SignInFailed
    if user.disabled_at is not None or (user.locked_until and user.locked_until > now):
        _verify(user.password_hash, password)
        raise SignInFailed

    if not _verify(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILURES:
            user.locked_until = now + LOCKOUT
            user.failed_logins = 0
            log.warning("locked %s after %d failed sign-ins", user.email, MAX_FAILURES)
        session.commit()
        raise SignInFailed

    # Parameters get stronger over time; a hash made with older ones is
    # upgraded the one moment the password is in hand.
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now

    token = secrets.token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        token_hash=_fingerprint(token),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(hours=get_settings().session_hours),
    )
    session.add(row)
    session.commit()
    return SignedIn(user=user, session=row, token=token)


def session_for(session: Session, token: str | None) -> UserSession | None:
    """The live session for a cookie value, or None. Does not raise."""
    if not token:
        return None
    row = session.scalar(
        select(UserSession).where(UserSession.token_hash == _fingerprint(token))
    )
    if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
        return None
    if row.user.disabled_at is not None:
        return None
    return row


def sign_out(session: Session, row: UserSession) -> None:
    row.revoked_at = utcnow()
    session.commit()


def csrf_ok(row: UserSession, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(row.csrf_token, presented)


def disable(session: Session, email: str) -> User | None:
    """Stop someone signing in, and end every session they have now."""
    user = session.scalar(select(User).where(User.email == _normalise(email)))
    if user is None:
        return None
    now = utcnow()
    user.disabled_at = user.disabled_at or now
    for row in session.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ):
        row.revoked_at = now
    session.commit()
    return user


def set_password(session: Session, email: str, password: str) -> User | None:
    """New password; every existing session ends, as after any reset."""
    user = session.scalar(select(User).where(User.email == _normalise(email)))
    if user is None:
        return None
    user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None
    now = utcnow()
    for row in session.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ):
        row.revoked_at = now
    session.commit()
    return user


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normalise(email: str) -> str:
    return email.strip().lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage panel users")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("create", help="add a user; prompts for the password")
    made.add_argument("email")
    made.add_argument("--name")
    made.add_argument("--role", choices=ROLES, default="author")
    sub.add_parser("list", help="show users")
    off = sub.add_parser("disable", help="stop a user signing in and end their sessions")
    off.add_argument("email")
    reset = sub.add_parser("password", help="set a new password; prompts for it")
    reset.add_argument("email")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from core.db import SessionLocal

    with SessionLocal() as session:
        if args.command in ("create", "password"):
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Again: "):
                raise SystemExit("the two passwords differ")
            try:
                if args.command == "create":
                    user = create(session, args.email, password, role=args.role, name=args.name)
                    log.info("created %s (%s)", user.email, user.role)
                else:
                    user = set_password(session, args.email, password)
                    if user is None:
                        raise SystemExit("no such user")
                    log.info("password changed for %s", user.email)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            return

        if args.command == "disable":
            user = disable(session, args.email)
            log.info("disabled %s", user.email) if user else log.info("no such user")
            return

        rows = session.scalars(select(User).order_by(User.created_at)).all()
        if not rows:
            log.info("no users; create one with: python -m core.users create <email> --role admin")
        for user in rows:
            state = "disabled" if user.disabled_at else "active"
            seen = user.last_login_at.date().isoformat() if user.last_login_at else "never"
            log.info("%-30s %-6s %-8s last sign-in %s", user.email, user.role, state, seen)


if __name__ == "__main__":
    main()
