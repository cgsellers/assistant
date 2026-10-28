"""
An organisation's people: who can sign in, with what role, and invitations
to join (DESIGN §9.4; /settings/members in api/routes/settings.py).

Users and invitations are found before anyone is signed in -- by email, by
an invitation's token -- so they are not behind the session scoping that
guards documents. Every function here takes the organisation explicitly and
looks only inside it; the routes pass the caller's own.

Two rules hold whatever is asked: an organisation keeps at least one admin,
and nobody removes themselves (another admin can).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import users
from core.config import get_settings
from core.models import Invitation, Organisation, User, UserSession, utcnow


class Refused(Exception):
    """Not allowed as asked; the message says why, for the person who asked."""

    def __init__(self, message: str, *, status: int = 409):
        super().__init__(message)
        self.status = status


class NotFound(Exception):
    """No such member here -- including one that belongs to another organisation."""


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- Who is in -----------------------------------------------------------------------


def _member(user: User, *, you: str | None) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "status": "active",
        "last_seen_at": user.last_login_at,
        "you": you is not None and user.email == you,
    }


def members_of_invitation(invitation: Invitation) -> dict[str, Any]:
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role": invitation.role,
        "status": "invited",
        "last_seen_at": None,
        "you": False,
    }


def _pending(session: Session, organisation_id: str):
    return select(Invitation).where(
        Invitation.organisation_id == organisation_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > utcnow(),
    )


def members(session: Session, organisation_id: str, *, you: str | None) -> list[dict[str, Any]]:
    """Everyone who can sign in, then everyone invited who has not yet accepted."""
    people = session.scalars(
        select(User)
        .where(User.organisation_id == organisation_id, User.disabled_at.is_(None))
        .order_by(User.created_at)
    )
    invited = session.scalars(_pending(session, organisation_id).order_by(Invitation.created_at))
    return [_member(u, you=you) for u in people] + [members_of_invitation(i) for i in invited]


def _admins(session: Session, organisation_id: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.organisation_id == organisation_id,
            User.disabled_at.is_(None),
            User.role == "admin",
        )
    )


def _find(session: Session, organisation_id: str, member_id: str) -> User | Invitation:
    user = session.get(User, member_id)
    if user is not None and user.organisation_id == organisation_id and user.disabled_at is None:
        return user
    invitation = session.scalar(
        _pending(session, organisation_id).where(Invitation.id == member_id)
    )
    if invitation is not None:
        return invitation
    raise NotFound


def change_role(
    session: Session, organisation_id: str, member_id: str, role: str, *, you: str | None
) -> dict[str, Any]:
    organisation = session.get(Organisation, organisation_id)
    try:
        role = users.check_role(organisation, role)
    except ValueError as exc:
        raise Refused(str(exc), status=422) from None
    found = _find(session, organisation_id, member_id)
    if isinstance(found, Invitation):
        found.role = role
        session.commit()
        return members_of_invitation(found)
    if found.role == "admin" and role != "admin" and _admins(session, organisation_id) == 1:
        raise Refused(
            f"{organisation.name} needs at least one admin. Make someone else an admin first."
        )
    found.role = role
    session.commit()
    return _member(found, you=you)


def remove(session: Session, organisation_id: str, member_id: str, *, you: str | None) -> None:
    """Withdraw an invitation, or stop someone signing in, ending their sessions now."""
    found = _find(session, organisation_id, member_id)
    if isinstance(found, Invitation):
        found.revoked_at = utcnow()
        session.commit()
        return
    if you is not None and found.email == you:
        raise Refused("You can't remove yourself. Ask another admin.")
    if found.role == "admin" and _admins(session, organisation_id) == 1:
        organisation = session.get(Organisation, organisation_id)
        raise Refused(f"{organisation.name} needs at least one admin.")
    users.disable(session, found.email)


# --- Invitations ----------------------------------------------------------------------


@dataclass(frozen=True)
class Invited:
    invitation: Invitation
    #: The link's secret. Exists only here; the table keeps its hash.
    token: str

    @property
    def url(self) -> str:
        return f"{get_settings().portal_url.rstrip('/')}/accept-invite?token={self.token}"


def invite(
    session: Session, organisation_id: str, email: str, role: str | None, *, by: str
) -> Invited:
    """
    An invitation for `email` to join, as `role`. Asking again for someone
    already invited replaces the earlier link, which stops working.
    """
    email = email.strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise Refused("That doesn't look like an email address.", status=422)
    organisation = session.get(Organisation, organisation_id)
    try:
        role = users.check_role(organisation, role)
    except ValueError as exc:
        raise Refused(str(exc), status=422) from None

    existing = session.scalar(select(User).where(User.email == email))
    if existing is not None and existing.organisation_id == organisation_id:
        if existing.disabled_at is None:
            raise Refused(f"{email} is already in the team.")
    elif existing is not None:
        # Email addresses are one account each, across Ledgerline.
        raise Refused(
            f"{email} can't be invited: it already has a Ledgerline account elsewhere. "
            "Ask them for another address, or contact Ledgerline."
        )

    now = utcnow()
    for earlier in session.scalars(
        _pending(session, organisation_id).where(Invitation.email == email)
    ):
        earlier.revoked_at = now
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        organisation_id=organisation_id,
        email=email,
        role=role,
        token_hash=_token_hash(token),
        created_by=by,
        expires_at=now + timedelta(days=get_settings().invitation_days),
    )
    session.add(invitation)
    session.commit()
    return Invited(invitation, token)


def invitation(session: Session, token: str) -> Invitation | None:
    """The invitation a link's token stands for, if it can still be accepted."""
    if not token:
        return None
    found = session.scalar(select(Invitation).where(Invitation.token_hash == _token_hash(token)))
    if found is None or found.accepted_at is not None or found.revoked_at is not None:
        return None
    if found.expires_at <= utcnow():
        return None
    return found


def accept(session: Session, token: str, password: str, *, name: str | None = None) -> User:
    """
    Become the person invited: a new account, or a removed one at this
    organisation back again. The invitation then cannot be used twice.
    """
    found = invitation(session, token)
    if found is None:
        raise Refused("This invitation link doesn't work any more. Ask for a new one.", status=404)
    try:
        password_hash = users.hash_password(password)
    except ValueError as exc:
        raise Refused(str(exc), status=422) from None

    user = session.scalar(select(User).where(User.email == found.email))
    if user is None:
        user = users.create(
            session,
            found.email,
            password,
            organisation_id=found.organisation_id,
            is_staff=False,
            role=found.role,
            name=name,
        )
    elif user.organisation_id == found.organisation_id:
        # Removed once, invited back: the same account, with a new password.
        user.password_hash = password_hash
        user.role = found.role
        user.disabled_at = None
        user.failed_logins = 0
        user.locked_until = None
        user.name = name or user.name
        for row in session.scalars(select(UserSession).where(UserSession.user_id == user.id)):
            row.revoked_at = row.revoked_at or utcnow()
    else:
        raise Refused("This invitation link doesn't work any more. Ask for a new one.", status=404)
    found.accepted_at = utcnow()
    session.commit()
    return user
