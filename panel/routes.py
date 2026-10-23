"""
The panel's pages.

Server-rendered with Jinja2, with HTMX for the requests that change something
(DESIGN §9.1). Every page calls the same doctypes.authoring functions as the
JSON API, so the rules are checked once, in one place, whichever way a
processor is edited.

Authentication is the app's, not the panel's: the same app-wide dependency
that guards the API guards these pages, and a page simply reads the
principal it produced. Two things differ for a browser:

  * **No credentials means a redirect to the sign-in page**, not a JSON 401.
    `unauthenticated` does that, and only for /panel paths.
  * **Anything that changes state goes through HTMX**, which sends the CSRF
    token from the page in a header on every request. A plain HTML form
    could not send the header, so no state-changing route here takes one --
    except signing in, which has no session to protect yet.

Every response also carries a strict Content Security Policy: scripts only
from this origin (HTMX is vendored, never loaded from a CDN), no inline
script, no framing. An injected `<script>` would not run even if escaping
ever failed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes.auth import set_session_cookie
from core import users
from core.auth import SESSION_COOKIE, Principal, require_principal
from core.db import get_session
from doctypes import authoring
from doctypes.authoring import AuthoringError, Conflict, NotFound

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")

router = APIRouter(prefix="/panel", include_in_schema=False)

#: Applied to every /panel response. `style-src 'self'` holds because the CSS
#: is a file and HTMX's own inline indicator styles are switched off in the
#: page's htmx-config.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def _page(
    request: Request, template: str, principal: Principal | None, *, status_code: int = 200,
    **context,
) -> HTMLResponse:
    signed_in = getattr(request.state, "user_session", None)
    return templates.TemplateResponse(
        request,
        template,
        {
            "principal": principal,
            "csrf_token": signed_in.csrf_token if signed_in is not None else None,
            **context,
        },
        status_code=status_code,
    )


# --- Signing in and out -----------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    if users.session_for(session, request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/panel", status.HTTP_303_SEE_OTHER)
    return _page(request, "login.html", None)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    session: Session = Depends(get_session),
):
    """
    A plain form post, so it works before any script has loaded. The one
    state-changing route without a CSRF token: there is no session yet for a
    forged request to ride, and SameSite keeps the cookie it sets first-party.
    """
    try:
        signed_in = users.sign_in(session, email, password)
    except users.SignInFailed:
        return _page(
            request, "login.html", None,
            error="Wrong email or password.", email=email,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    response = RedirectResponse("/panel", status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, signed_in.token)
    return response


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    """Called through HTMX, so the app-wide check has already seen the CSRF token."""
    signed_in = getattr(request.state, "user_session", None)
    if signed_in is not None:
        users.sign_out(session, session.merge(signed_in))
    response = Response(headers={"HX-Redirect": "/panel/login"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- Processors -------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def processors(
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    return _page(
        request, "processors.html", principal,
        processors=authoring.list_processors(session),
    )


@router.get("/processors/{name}", response_class=HTMLResponse)
def history(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    versions = authoring.history(session, name)
    return _page(
        request, "history.html", principal,
        name=name, versions=versions,
        compare={row.version: _compare_with(row, versions) for row in versions},
    )


@router.get("/processors/{name}/versions/{version}", response_class=HTMLResponse)
def version(
    name: str,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    row = authoring.get_version(session, name, version)
    versions = authoring.history(session, name)
    return _page(
        request, "version.html", principal,
        row=row, text=authoring.to_yaml(row.definition),
        compare_with=_compare_with(row, versions),
    )


@router.get("/processors/{name}/diff", response_class=HTMLResponse)
def diff(
    name: str,
    a: int,
    b: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    older = authoring.get_version(session, name, a)
    newer = authoring.get_version(session, name, b)
    lines = authoring.diff(
        older.definition, newer.definition, older_label=f"v{a}", newer_label=f"v{b}"
    )
    return _page(
        request, "diff.html", principal,
        name=name, a=a, b=b, lines=[(_kind(line), line) for line in lines],
    )


def _compare_with(row, versions) -> int | None:
    """What a version is best read against: its base, else the one before it."""
    if row.based_on_version is not None:
        return row.based_on_version
    earlier = [v.version for v in versions if v.version < row.version]
    return max(earlier) if earlier else None


def _kind(line: str) -> str:
    if line.startswith(("+++", "---")):
        return "file"
    if line.startswith("@@"):
        return "hunk"
    return {"+": "add", "-": "del"}.get(line[:1], "same")


# --- Wiring, done by api.main -------------------------------------------------


async def unauthenticated(request: Request, exc: StarletteHTTPException):
    """
    A browser with no session is sent to sign in, not shown JSON. An HTMX
    request whose session ran out is redirected the HTMX way. Every other
    path keeps the API's ordinary error.
    """
    path = request.url.path
    if exc.status_code == 401 and path.startswith("/panel"):
        if request.headers.get("hx-request"):
            return Response(headers={"HX-Redirect": "/panel/login"})
        return RedirectResponse("/panel/login", status.HTTP_303_SEE_OTHER)
    if exc.status_code == 404 and path.startswith("/panel"):
        return _page(request, "missing.html", None, detail=exc.detail, status_code=404)
    return await http_exception_handler(request, exc)


def authoring_page(request: Request, exc: AuthoringError) -> HTMLResponse:
    """
    The rules' errors as a page. api.main registers one handler for
    AuthoringError and sends /panel paths here, the rest to the JSON one --
    a second handler keyed on NotFound would have caught the API's too.
    """
    code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
    return _page(
        request, "missing.html" if code == 404 else "problem.html", None,
        detail=str(exc), problems=exc.details, status_code=code,
    )


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/panel"):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
    return response
