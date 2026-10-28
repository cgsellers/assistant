"""
/settings -- an organisation's API keys and people, for its admins
(DESIGN §9.4; portal/src/features/settings).

For people only: a key is refused, since keys live inside other systems and
must not be able to mint more of themselves. Anyone signed in may read; every
change needs an admin. Every query names the caller's own organisation:
keys, users and invitations are found before anyone is signed in, so they are
not behind the session scoping that guards documents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import auth, people, tenancy
from core.auth import Principal, require_manage, require_principal
from core.db import get_session
from core.models import ApiKey, utcnow

router = APIRouter(prefix="/settings", tags=["settings"])

AnyRole = Literal["author", "admin", "member", "viewer"]


def require_person(principal: Principal | None = Depends(require_principal)) -> Principal:
    """Signed in as a person: settings are not for keys."""
    if principal is None or principal.kind == "key":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Settings are for people: sign in to see them."
        )
    return principal


def organisation_of(principal: Principal, session: Session) -> str:
    """The caller's organisation; with auth switched off for local work, the team's own."""
    return principal.organisation_id or tenancy.demo_organisation(session).id


def _you(principal: Principal) -> str | None:
    return principal.name if principal.kind == "user" else None


# --- API keys -------------------------------------------------------------------------


class ApiKeyOut(BaseModel):
    id: str
    name: str
    #: The start of the key: enough to recognise it, not to use it.
    prefix: str
    created_at: datetime
    created_by: str | None
    last_used_at: datetime | None


class KeyIn(BaseModel):
    name: str = Field(max_length=200)


class CreatedKeyOut(BaseModel):
    key: ApiKeyOut
    #: The only time it is ever shown: only its fingerprint is kept.
    secret: str


@router.get("/keys", response_model=list[ApiKeyOut], summary="The organisation's API keys")
def list_keys(
    principal: Principal = Depends(require_person), session: Session = Depends(get_session)
):
    """Live keys, newest first. Revoked ones are kept, for the audit, but not listed."""
    return session.scalars(
        select(ApiKey)
        .where(
            ApiKey.organisation_id == organisation_of(principal, session),
            ApiKey.revoked_at.is_(None),
        )
        .order_by(ApiKey.created_at.desc())
    ).all()


@router.post(
    "/keys",
    response_model=CreatedKeyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Make an API key",
)
def create_key(
    body: KeyIn,
    principal: Principal = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """
    A key for one of the organisation's own systems. It may upload and correct
    documents, and see only this organisation's. Its secret is in this answer
    and nowhere else, ever.
    """
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Give the key a name, so it can be recognised later.",
        )
    row, secret = auth.create(
        session, name, organisation_id=organisation_of(principal, session), role=auth.Role.MEMBER
    )
    row.created_by = principal.name
    session.commit()
    return CreatedKeyOut(key=ApiKeyOut.model_validate(row, from_attributes=True), secret=secret)


@router.delete(
    "/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke an API key"
)
def revoke_key(
    key_id: str,
    principal: Principal = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """At once: anything using it is refused from its next request."""
    row = session.get(ApiKey, key_id)
    if row is None or row.organisation_id != organisation_of(principal, session) or row.revoked_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such key")
    row.revoked_at = utcnow()
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- People ---------------------------------------------------------------------------


class MemberOut(BaseModel):
    id: str
    email: str
    role: AnyRole
    #: "invited" until they accept and choose a password.
    status: Literal["active", "invited"]
    last_seen_at: datetime | None
    #: The person asking.
    you: bool


class InviteIn(BaseModel):
    email: str = Field(max_length=254)
    role: AnyRole


class InvitationCreatedOut(BaseModel):
    member: MemberOut
    #: For the admin to pass on: Ledgerline does not send email yet.
    invite_url: str
    expires_at: datetime


class RoleIn(BaseModel):
    role: AnyRole


def _refused(exc: people.Refused) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("/members", response_model=list[MemberOut], summary="Who can sign in")
def list_members(
    principal: Principal = Depends(require_person), session: Session = Depends(get_session)
):
    """Everyone who can sign in, then everyone invited who has not accepted yet."""
    return people.members(session, organisation_of(principal, session), you=_you(principal))


@router.post(
    "/members",
    response_model=InvitationCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone",
)
def invite(
    body: InviteIn,
    principal: Principal = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """
    An invitation, as a link for the admin to pass on. It works once, for a
    week; inviting the same person again replaces it.
    """
    try:
        invited = people.invite(
            session, organisation_of(principal, session), body.email, body.role, by=principal.name
        )
    except people.Refused as exc:
        raise _refused(exc) from None
    return InvitationCreatedOut(
        member=people.members_of_invitation(invited.invitation),
        invite_url=invited.url,
        expires_at=invited.invitation.expires_at,
    )


@router.patch("/members/{member_id}", response_model=MemberOut, summary="Change someone's role")
def change_role(
    member_id: str,
    body: RoleIn,
    principal: Principal = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Refused (409) if it would leave the organisation with no admin."""
    try:
        return people.change_role(
            session, organisation_of(principal, session), member_id, body.role, you=_you(principal)
        )
    except people.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member") from None
    except people.Refused as exc:
        raise _refused(exc) from None


@router.delete(
    "/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove someone"
)
def remove_member(
    member_id: str,
    principal: Principal = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """
    Withdraw an invitation, or stop someone signing in: their sessions end now.
    What they corrected or approved stays on record. Not yourself, and not the
    last admin (409).
    """
    try:
        people.remove(session, organisation_of(principal, session), member_id, you=_you(principal))
    except people.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member") from None
    except people.Refused as exc:
        raise _refused(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
