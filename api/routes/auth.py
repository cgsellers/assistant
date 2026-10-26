"""
/auth -- signing people in and out of the panel.

Machines never come here: they send an API key with each request. A sign-in
mints a server-side session (core/users.py) and sets its token as a cookie
the page's scripts cannot read. The response carries the CSRF token the
panel must send back on every request that changes something.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import users
from core.auth import CSRF_HEADER, SESSION_COOKIE, Principal, require_principal
from core.config import get_settings
from core.db import get_session
from core.models import Organisation

router = APIRouter(prefix="/auth", tags=["auth"])


class SignInIn(BaseModel):
    email: str
    password: str


class MeOut(BaseModel):
    kind: str
    name: str
    role: str
    organisation_id: str | None = None
    #: Shown to the person, e.g. in the portal's header.
    organisation_name: str | None = None
    #: The Ledgerline team, which sees every organisation's documents.
    is_staff: bool = False
    #: Only for a signed-in browser: what to send in the CSRF header.
    csrf_token: str | None = None
    csrf_header: str = CSRF_HEADER


def set_session_cookie(response: Response, token: str) -> None:
    """The one place the session cookie's attributes are decided."""
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        # Lax: sent on a top-level navigation to the panel, not on a
        # cross-site POST. The CSRF token covers what Lax does not.
        samesite="lax",
        path="/",
    )


@router.post("/login", response_model=MeOut, summary="Sign in to the panel")
def login(body: SignInIn, response: Response, session: Session = Depends(get_session)):
    """
    401 for every failure -- wrong password, unknown email, disabled or
    locked -- so this cannot be used to find out who has an account. Ten
    failures in a row lock the account for fifteen minutes.
    """
    try:
        signed_in = users.sign_in(session, body.email, body.password)
    except users.SignInFailed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong email or password") from None

    set_session_cookie(response, signed_in.token)
    return MeOut(
        kind="user",
        name=signed_in.user.email,
        role=signed_in.user.role,
        organisation_id=signed_in.user.organisation_id,
        organisation_name=_organisation_name(session, signed_in.user.organisation_id),
        is_staff=signed_in.user.is_staff,
        csrf_token=signed_in.session.csrf_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    """Ends the session on the server, not just in this browser."""
    signed_in = getattr(request.state, "user_session", None)
    if signed_in is not None:
        users.sign_out(session, session.merge(signed_in))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=MeOut, summary="Who this request is")
def me(
    request: Request,
    principal: Principal | None = Depends(require_principal),
    session: Session = Depends(get_session),
):
    signed_in = getattr(request.state, "user_session", None)
    return MeOut(
        kind=principal.kind,
        name=principal.name,
        role=principal.role,
        organisation_id=principal.organisation_id,
        organisation_name=_organisation_name(session, principal.organisation_id),
        is_staff=principal.is_staff,
        csrf_token=signed_in.csrf_token if signed_in is not None else None,
    )


def _organisation_name(session: Session, organisation_id: str | None) -> str | None:
    if organisation_id is None:
        return None
    organisation = session.get(Organisation, organisation_id)
    return organisation.name if organisation is not None else None
