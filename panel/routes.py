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

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.routes.auth import set_session_cookie
from api.routes.documents import check_upload
from api.routes.processors import get_drafter, get_extractor, get_ocr_engine
from core import repository, tenancy, users
from core.auth import SESSION_COOKIE, Principal, Role, require_principal, require_role
from core.db import get_session
from doctypes import authoring, results
from doctypes.authoring import AuthoringError, Conflict, NotFound

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")


def _duration(value: timedelta | None) -> str:
    """How long a step took, at a precision a person reads: 340 ms, 2.4 s, 3 min."""
    if value is None:
        return ""
    seconds = value.total_seconds()
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


templates.env.filters["duration"] = _duration

router = APIRouter(prefix="/panel", include_in_schema=False)

_AUTHOR = require_role(Role.AUTHOR)
_ADMIN = require_role(Role.ADMIN)

#: What a new processor starts as: the smallest definition that validates,
#: with a comment-free shape an author can grow.
NEW_PROCESSOR = """\
name: new_type
display_name: New document type
description: What this document is, in a sentence.
matches:
  keywords: [first keyword, second keyword]
  min_score: 0.2
fields:
  total:
    type: money
    description: The amount to pay, labelled Total. Null if it is not printed.
rules:
- type: required
  field: total
"""

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
            "is_admin": principal is not None and principal.may(Role.ADMIN),
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


# --- Documents ---------------------------------------------------------------
# Every document from every client: the panel is the Ledgerline team's. The
# client portal will get its own, scoped view once tenancy exists (DESIGN §9.3).

#: The age filter's choices, in days. None is "all time".
SINCE_DAYS = {"1": 1, "7": 7, "30": 30}


@router.get("/documents", response_class=HTMLResponse)
def documents(
    request: Request,
    outcome: str = "",
    processor: str = "",
    organisation: str = "",
    days: str = "",
    before: str = "",
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    since = None
    if days in SINCE_DAYS:
        since = datetime.now(UTC) - timedelta(days=SINCE_DAYS[days])
    cursor = _parse_cursor(before)
    rows = results.list_documents(
        session,
        outcome=outcome or None,
        processor=processor or None,
        organisation_id=organisation or None,
        since=since,
        before=cursor,
        limit=PAGE_SIZE,
    )
    return _page(
        request, "documents.html", principal,
        rows=rows,
        filters={
            "outcome": outcome, "processor": processor, "organisation": organisation,
            "days": days,
        },
        organisations=tenancy.all_organisations(session),
        outcomes=sorted(set(results.OUTCOMES.values())),
        processors=[p.name for p in authoring.list_processors(session)],
        next_before=rows[-1].uploaded_at.isoformat() if len(rows) == PAGE_SIZE else None,
    )


@router.get("/documents/{document_id}", response_class=HTMLResponse)
def document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    row = repository.get_document(session, document_id)
    if row is None:
        return _page(
            request, "missing.html", principal, detail="No such document", status_code=404
        )
    detail = results.document_detail(session, row)
    previewable = row.mime_type in ("image/jpeg", "image/png", "image/webp", "application/pdf")
    return _page(request, "document.html", principal, previewable=previewable, **detail)


PAGE_SIZE = 50


def _parse_cursor(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# --- Drafting and editing ----------------------------------------------------
# Changes go through HTMX (hx-post), so the app-wide check has seen the CSRF
# token before any of these run. Results come back as fragments swapped into
# the page; a failure is a 4xx with the problems listed, which the page's
# htmx-config tells HTMX to show rather than drop.


@router.get("/new", response_class=HTMLResponse)
def new_page(request: Request, principal: Principal = Depends(_AUTHOR)):
    return _page(request, "new.html", principal, text=NEW_PROCESSOR)


@router.post("/new")
def create_new(
    request: Request,
    text: str = Form(""),
    session: Session = Depends(get_session),
    principal: Principal = Depends(_AUTHOR),
):
    spec = authoring.join_edit(text, "")
    row = authoring.create_draft(session, spec, based_on=None, created_by=principal.label)
    return _go(f"/panel/processors/{row.name}/versions/{row.version}/edit")


@router.post("/new/ai", response_class=HTMLResponse)
def draft_with_ai(
    request: Request,
    description: str = Form(""),
    sample_text: str = Form(""),
    sample: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    drafter=Depends(get_drafter),
    engine=Depends(get_ocr_engine),
    principal: Principal = Depends(_AUTHOR),
):
    """
    A model drafts the processor from a description and, optionally, a
    sample -- a file read the way the pipeline reads documents, or pasted
    text. The result is a draft to review, never live and never with code.
    """
    from doctypes import drafting

    data = sample.file.read() if sample is not None else b""
    text = authoring.text_from_file(data, check_upload(data), engine) if data else sample_text
    outcome = drafting.draft(
        session, description=description, sample=text,
        created_by=f"{principal.label} (ai draft)", drafter=drafter,
    )
    return _fragment(request, "_drafted.html", outcome=outcome)


@router.post("/processors/{name}/versions/{version}/draft")
def draft_from(
    name: str,
    version: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_AUTHOR),
):
    """A new draft starting from a published version -- how every edit begins."""
    base = authoring.get_version(session, name, version)
    row = authoring.create_draft(
        session, base.definition, based_on=version, created_by=principal.label
    )
    return _go(f"/panel/processors/{name}/versions/{row.version}/edit")


@router.get("/processors/{name}/versions/{version}/edit", response_class=HTMLResponse)
def edit_page(
    name: str,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_AUTHOR),
):
    row = authoring.get_version(session, name, version)
    if row.is_published:
        # Published versions never change. Send the author to the version,
        # where "start a draft from this" is the way to edit it.
        return RedirectResponse(
            f"/panel/processors/{name}/versions/{version}", status.HTTP_303_SEE_OTHER
        )
    text, code = authoring.editable(row.definition)
    return _page(request, "edit.html", principal, row=row, text=text, code=code)


@router.post("/processors/{name}/versions/{version}/save", response_class=HTMLResponse)
def save(
    name: str,
    version: int,
    request: Request,
    text: str = Form(""),
    code: str = Form(""),
    code_file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    principal: Principal = Depends(_AUTHOR),
):
    """
    Saves over the draft. An uploaded .py replaces whatever is in the code
    box -- the "attach a file" way of writing a transform.
    """
    uploaded = code_file.file.read() if code_file is not None else b""
    if uploaded:
        try:
            code = uploaded.decode("utf-8")
        except UnicodeDecodeError:
            raise AuthoringError("the uploaded file is not UTF-8 text") from None
    row = authoring.update_draft(session, name, version, authoring.join_edit(text, code))
    return _fragment(request, "_saved.html", row=row, from_file=bool(uploaded))


# --- Trying a version ---------------------------------------------------------


@router.get("/processors/{name}/versions/{version}/try", response_class=HTMLResponse)
def try_page(
    name: str,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_AUTHOR),
):
    row = authoring.get_version(session, name, version)
    return _page(request, "try.html", principal, row=row)


@router.post("/processors/{name}/versions/{version}/try", response_class=HTMLResponse)
def run_trial(
    name: str,
    version: int,
    request: Request,
    text: str = Form(""),
    document_id: str = Form(""),
    sample: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    extractor=Depends(get_extractor),
    engine=Depends(get_ocr_engine),
    principal: Principal = Depends(_AUTHOR),
):
    """
    One of: an uploaded sample (read like a real document, not ingested), a
    stored document (recorded as a trial), or pasted text. In that order.
    """
    data = sample.file.read() if sample is not None else b""
    if data:
        text = authoring.text_from_file(data, check_upload(data), engine)
        result = authoring.trial_run(session, name, version, extractor=extractor, text=text)
    elif document_id.strip():
        result = authoring.trial_run(
            session, name, version, extractor=extractor, document_id=document_id.strip()
        )
    else:
        result = authoring.trial_run(session, name, version, extractor=extractor, text=text)

    row = authoring.get_version(session, name, version)
    fields = list(authoring.parse(row.definition).fields)
    # Ranked the way routing ranks: signals hit, then share of the type's own.
    ranked = sorted(
        result.scores.items(), key=lambda kv: (kv[1]["hits"], kv[1]["score"]), reverse=True
    )
    return _fragment(request, "_trial.html", trial=result, fields=fields, ranked=ranked)


# --- Publishing and retiring --------------------------------------------------
# Admin only: publishing puts a transform's code live, and retiring takes a
# processor away from live traffic. Each has a confirmation page that shows
# exactly what will happen, then a confirm button that goes through HTMX.


@router.get("/processors/{name}/versions/{version}/publish", response_class=HTMLResponse)
def publish_page(
    name: str,
    version: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_ADMIN),
):
    preview = authoring.publish_preview(session, name, version)
    return _page(
        request, "publish.html", principal,
        preview=preview, lines=[(_kind(line), line) for line in preview.diff],
    )


@router.post("/processors/{name}/versions/{version}/publish")
def publish(
    name: str,
    version: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_ADMIN),
):
    """Every check runs again here: the page may be minutes old."""
    authoring.publish(session, name, version, published_by=principal.label)
    return _go(f"/panel/processors/{name}/versions/{version}")


@router.get("/processors/{name}/retire", response_class=HTMLResponse)
def retire_page(
    name: str,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_ADMIN),
):
    authoring.history(session, name)  # 404 for an unknown processor
    live = repository.get_active_document_type(session, name)
    extracted = authoring.extraction_count(session, name)
    return _page(request, "retire.html", principal, name=name, live=live, extracted=extracted)


@router.post("/processors/{name}/retire")
def retire(
    name: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(_ADMIN),
):
    authoring.retire(session, name, retired_by=principal.label)
    return _go(f"/panel/processors/{name}")


def _go(url: str) -> Response:
    """Navigate the browser after an HTMX request."""
    return Response(headers={"HX-Redirect": url})


def _fragment(request: Request, template: str, *, status_code: int = 200, **context):
    return templates.TemplateResponse(request, template, context, status_code=status_code)


# --- Wiring, done by api.main -------------------------------------------------


async def unauthenticated(request: Request, exc: StarletteHTTPException):
    """
    A browser with no session is sent to sign in, not shown JSON. An HTMX
    request whose session ran out is redirected the HTMX way. Every other
    path keeps the API's ordinary error.
    """
    path = request.url.path
    if not path.startswith("/panel"):
        return await http_exception_handler(request, exc)
    if exc.status_code == 401:
        if request.headers.get("hx-request"):
            return Response(headers={"HX-Redirect": "/panel/login"})
        return RedirectResponse("/panel/login", status.HTTP_303_SEE_OTHER)
    if exc.status_code == 404:
        return _page(request, "missing.html", None, detail=exc.detail, status_code=404)
    if exc.status_code == 403:
        return _problem(request, str(exc.detail), [], 403)
    return await http_exception_handler(request, exc)


def authoring_page(request: Request, exc: AuthoringError) -> HTMLResponse:
    """
    The rules' errors as a page. api.main registers one handler for
    AuthoringError and sends /panel paths here, the rest to the JSON one --
    a second handler keyed on NotFound would have caught the API's too.
    """
    code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
    if code == 404 and not request.headers.get("hx-request"):
        return _page(request, "missing.html", None, detail=str(exc), status_code=404)
    return _problem(request, str(exc), exc.details, code)


def _problem(request: Request, detail: str, problems: list[str], code: int):
    """A fragment for HTMX to swap in beside the form, or a page otherwise."""
    if request.headers.get("hx-request"):
        return _fragment(
            request, "_problems.html", detail=detail, problems=problems, status_code=code
        )
    return _page(request, "problem.html", None, detail=detail, problems=problems, status_code=code)


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/panel"):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
    return response
