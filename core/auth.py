"""
API keys: who is allowed to talk to the ingestion API at all.

This is authentication, not authorisation. Nothing in the schema has an
owner -- no `user_id`, no `tenant_id` -- so every key that gets in sees
everything. Adding per-caller access control means adding tenancy to the data
model first, which is a much larger decision than a locked front door and
should not be smuggled in behind one.

Keys are stored hashed, never in plaintext, so a database dump does not hand
over access. The hash is a plain SHA-256 rather than bcrypt or argon2, which
is the right call *here* and the wrong call for a password: those are slow on
purpose because human-chosen secrets have little entropy and must be
expensive to guess. A key from `secrets.token_urlsafe(32)` has 256 bits of
it, so there is nothing to brute force and no reason to pay for slowness on
every request.

Lookup is by hash, so there is no secret-dependent comparison to leak timing.

Run it with:  uv run python -m core.auth create "ingestion worker"
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import get_session
from core.models import ApiKey, utcnow

log = logging.getLogger("auth")

#: Identifies a Ledgerline key on sight, in a log or a config file, and makes
#: secret-scanning rules possible.
PREFIX = "lk_"

#: Enough of the key to tell two apart in a listing without being enough to
#: use. Stored alongside the hash, which cannot be reversed to show it.
VISIBLE = len(PREFIX) + 8

#: Paths that answer without a key. `/health` is a liveness probe -- a load
#: balancer cannot hold a credential, and refusing it would make an
#: unauthenticated deploy look healthy and an authenticated one look dead.
#: The docs are open because this is a project meant to be read; a deployment
#: that cared would close them.
PUBLIC_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})

#: `last_used_at` is useful for spotting a key nobody uses any more, but
#: writing it on every request turns every GET into a write, and SQLite
#: serialises writers. Once a minute is plenty to answer "is this key live".
TOUCH_AFTER = timedelta(minutes=1)


def generate() -> str:
    """A new key. Shown once, at creation, and never recoverable."""
    return PREFIX + secrets.token_urlsafe(32)


def fingerprint(key: str) -> str:
    """What gets stored. See the module docstring for why SHA-256 is enough."""
    return hashlib.sha256(key.encode()).hexdigest()


def create(session: Session, name: str) -> tuple[ApiKey, str]:
    """
    Mint a key. Returns the row and the secret, which is the only time the
    secret exists anywhere.
    """
    secret = generate()
    row = ApiKey(name=name, key_hash=fingerprint(secret), prefix=secret[:VISIBLE])
    session.add(row)
    session.commit()
    return row, secret


def revoke(session: Session, prefix: str) -> ApiKey | None:
    """Retire a key by its visible prefix. Idempotent."""
    row = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        session.commit()
    return row


def authenticate(session: Session, key: str | None) -> ApiKey | None:
    """The row for a presented key, or None. Does not raise."""
    if not key:
        return None
    row = session.scalar(select(ApiKey).where(ApiKey.key_hash == fingerprint(key)))
    if row is None or row.revoked_at is not None:
        return None

    now = utcnow()
    if row.last_used_at is None or now - _aware(row.last_used_at) > TOUCH_AFTER:
        row.last_used_at = now
        session.commit()
    return row


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; UTCDateTime normalises on read."""
    return value


def bearer(request: Request) -> str | None:
    """
    The key from the request.

    `Authorization: Bearer <key>` is the conventional place. `X-API-Key` is
    also accepted because enough clients reach for it, and rejecting a
    correct key for being in the wrong header is a bad first experience.
    Never a query parameter: those land in access logs and browser history.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.headers.get("x-api-key") or None


def require_api_key(
    request: Request, session: Session = Depends(get_session)
) -> ApiKey | None:
    """
    Applied to the whole app rather than to a router.

    That way a route added later is protected because nobody did anything,
    rather than unprotected because somebody forgot. Opening a path is an
    explicit edit to PUBLIC_PATHS, which is a line a reviewer will see.
    """
    if request.url.path in PUBLIC_PATHS:
        return None

    if not get_settings().require_auth:
        return None

    key = authenticate(session, bearer(request))
    if key is None:
        # The same answer for absent, malformed, unknown and revoked. Telling
        # a caller which of those it was tells an attacker whether a key
        # exists, and tells a legitimate caller nothing they can act on that
        # "check your key" does not.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "a valid API key is required; send it as "
                "'Authorization: Bearer <key>'"
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage API keys")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("create", help="mint a key and print it once")
    made.add_argument("name", help="what this key is for, e.g. 'ingestion worker'")
    sub.add_parser("list", help="show keys, without their secrets")
    killed = sub.add_parser("revoke", help="retire a key")
    killed.add_argument("prefix", help="the visible prefix, e.g. lk_A1b2C3d4")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from core.db import SessionLocal

    with SessionLocal() as session:
        if args.command == "create":
            row, secret = create(session, args.name)
            log.info("%s", secret)
            log.info("")
            log.info("Saved as %s. This is the only time it is shown.", row.prefix)
            return

        if args.command == "revoke":
            row = revoke(session, args.prefix)
            log.info("revoked %s", row.prefix) if row else log.info("no such key")
            return

        rows = session.scalars(select(ApiKey).order_by(ApiKey.created_at)).all()
        if not rows:
            log.info("no keys; create one with: python -m core.auth create <name>")
        for row in rows:
            state = "revoked" if row.revoked_at else "active"
            used = row.last_used_at.date().isoformat() if row.last_used_at else "never"
            log.info("%-14s %-8s last used %-10s %s", row.prefix, state, used, row.name)


if __name__ == "__main__":
    main()
