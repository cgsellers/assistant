"""Upload and inspect documents."""

from __future__ import annotations

import binascii
from base64 import b64decode
from datetime import date
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.schemas import (
    Base64Upload,
    DocTypeOut,
    DocumentDetail,
    DocumentFieldsOut,
    DocumentListOut,
    DocumentOut,
    DocumentSummaryOut,
    FieldsPatchIn,
    FieldsRefusedOut,
    OcrResultOut,
    ResultOut,
)
from core import repository, storage, tenancy
from core.auth import Principal, require_principal, require_work
from core.config import get_settings
from core.db import get_session
from doctypes import listing, review
from doctypes.fields import document_fields
from doctypes.results import document_result

router = APIRouter(prefix="/documents", tags=["documents"])


def check_upload(data: bytes) -> str:
    """Size and type checks. Returns the detected mime type, or raises 4xx."""
    settings = get_settings()

    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is empty")

    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / 1024 / 1024
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"File is {len(data) / 1024 / 1024:.1f} MB, limit is {limit_mb:.0f} MB",
        )

    # Trust the bytes, not the caller's Content-Type header.
    mime = storage.sniff_mime(data)
    if mime is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Unrecognised file type. Expected an image or PDF.",
        )
    if mime not in settings.allowed_mime_types:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{mime} is not accepted. Allowed: {', '.join(settings.allowed_mime_types)}",
        )
    return mime


def owner(
    principal: Principal | None = Depends(require_work),
    session: Session = Depends(get_session),
) -> str:
    """
    The organisation an upload belongs to: the caller's. With auth switched
    off for local work there is no caller, so it is the team's own.
    """
    if principal is not None and principal.organisation_id is not None:
        return principal.organisation_id
    return tenancy.demo_organisation(session).id


def _ingest(
    session: Session, organisation_id: str, data: bytes, filename: str | None
) -> DocumentOut:
    mime = check_upload(data)
    document, created = repository.ingest(
        session, organisation_id=organisation_id, data=data, mime_type=mime,
        original_filename=filename,
    )
    out = DocumentOut.model_validate(document)
    out.duplicate = not created
    return out


@router.post("", response_model=DocumentOut, summary="Upload a document")
def upload(
    file: UploadFile = File(description="Receipt or invoice: JPEG, PNG, WebP, HEIC, TIFF or PDF"),
    session: Session = Depends(get_session),
    organisation_id: str = Depends(owner),
) -> DocumentOut:
    """
    Upload a file for processing. It belongs to the caller's organisation.

    Deduplicated by SHA-256 of the contents, per organisation: uploading the
    same bytes twice returns the original document with `duplicate: true` and
    queues no extra work. Safe to retry.
    """
    return _ingest(session, organisation_id, file.file.read(), file.filename)


@router.post("/base64", response_model=DocumentOut, summary="Upload as base64 JSON")
def upload_base64(
    payload: Base64Upload,
    session: Session = Depends(get_session),
    organisation_id: str = Depends(owner),
) -> DocumentOut:
    """Same as `POST /documents`, for callers that already hold the bytes."""
    try:
        data = b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"content_b64 is not valid base64: {exc}"
        ) from exc
    return _ingest(session, organisation_id, data, payload.filename)


# --- The list ---------------------------------------------------------------------
# Declared before /{document_id}, which would otherwise take "summary", "types"
# and "export" for document ids.


Outcome = Literal["processing", "done", "needs_review", "unsupported", "failed"]
Sort = Literal["received", "document_date"]


def list_filters(
    outcome: Outcome | None = Query(default=None, description="Only documents with this outcome"),
    type: str | None = Query(
        default=None, description="Only documents read as this processor (its name)"
    ),
    q: str | None = Query(
        default=None, max_length=200, description="Matches the supplier or the file name"
    ),
    received_from: date | None = Query(
        default=None, alias="from", description="Received on or after this day (in `tz`)"
    ),
    received_to: date | None = Query(
        default=None, alias="to", description="Received on or before this day (in `tz`)"
    ),
    tz: str = Query(
        default="UTC",
        max_length=64,
        description="The IANA time zone `from` and `to` are days in, e.g. Europe/Madrid",
    ),
) -> listing.Filters:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown time zone {tz!r}"
        ) from None
    return listing.Filters(
        outcome=outcome,
        doc_type=type,
        q=q,
        received_from=received_from,
        received_to=received_to,
        tz=zone,
    )


@router.get("", response_model=DocumentListOut, summary="List documents")
def list_documents(
    filters: listing.Filters = Depends(list_filters),
    sort: Sort = Query(
        default="received", description="Newest first, by when it arrived or by its own date"
    ),
    cursor: str | None = Query(
        default=None, max_length=500, description="`next_cursor` from the page before"
    ),
    limit: int = Query(default=25, ge=1, le=100),
    session: Session = Depends(get_session),
) -> DocumentListOut:
    """
    The caller's documents, newest first, a page at a time: what each one is,
    who issued it, its own date and total, and its outcome. Documents in review
    show what was read from them.

    Paged by cursor, which stays correct while new documents arrive. Pass
    `next_cursor` back as `cursor`, with the same filters and sort.
    """
    try:
        documents, next_cursor = listing.page(
            session, filters, sort=sort, cursor=cursor, limit=limit
        )
    except listing.BadCursor as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    label = listing.labels(session, {d.doc_type for d in documents if d.doc_type})
    return DocumentListOut.model_validate(
        {"items": [listing.row(d, label) for d in documents], "next_cursor": next_cursor}
    )


@router.get(
    "/summary", response_model=DocumentSummaryOut, summary="Count and total the documents"
)
def summarise_documents(
    filters: listing.Filters = Depends(list_filters),
    session: Session = Depends(get_session),
) -> DocumentSummaryOut:
    """
    How many documents match the filters, by outcome, and their totals per
    currency -- never added across currencies.
    """
    return DocumentSummaryOut.model_validate(listing.summary(session, filters))


@router.get("/types", response_model=list[DocTypeOut], summary="The kinds of document read")
def document_types(session: Session = Depends(get_session)) -> list[DocTypeOut]:
    """The processors the caller's documents were read as, for filtering the list."""
    return [DocTypeOut.model_validate(t) for t in listing.types(session)]


@router.get(
    "/export",
    response_class=Response,
    summary="Export the list as CSV",
    responses={200: {"content": {"text/csv": {}}}},
)
def export_documents(
    filters: listing.Filters = Depends(list_filters),
    sort: Sort = Query(default="received"),
    session: Session = Depends(get_session),
) -> Response:
    """
    Every document matching the filters, as CSV, up to 50,000 rows. Text that
    a spreadsheet would run as a formula is prefixed with an apostrophe.
    """
    filename = f"ledgerline-documents-{date.today().isoformat()}.csv"
    return Response(
        content=listing.export(session, filters, sort=sort),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/{document_id}/ocr",
    response_model=OcrResultOut,
    summary="Get the OCR text for a document",
)
def get_ocr(
    document_id: str,
    include_blocks: bool = Query(
        default=False, description="Include every text block with its bounding box"
    ),
    session: Session = Depends(get_session),
) -> OcrResultOut:
    """
    The recognised text, the engine that produced it, and the confidence.

    Returns 404 with distinct messages for "no such document" and "OCR has not
    run yet", because those need different actions from the caller.
    """
    document = repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")

    result = repository.get_ocr_result(session, document_id)
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No OCR result yet; document status is {document.status!r}",
        )

    out = OcrResultOut.model_validate(result)
    out.block_count = len(result.layout_blocks or [])
    out.layout_blocks = result.layout_blocks if include_blocks else None
    return out


@router.get(
    "/{document_id}/file",
    response_class=Response,
    summary="Get the original file",
    responses={200: {"content": {"image/*": {}, "application/pdf": {}}}},
)
def get_file(document_id: str, session: Session = Depends(get_session)) -> Response:
    """
    The bytes exactly as uploaded, to view beside what was extracted from them.

    Served as the type detected at upload -- never one the caller names -- and
    only types on the upload allowlist ever got stored, so nothing here can be
    HTML or SVG and run in a browser. `nosniff` stops the browser guessing
    otherwise, and `no-store` keeps documents full of personal data out of
    every cache on the way.
    """
    document = repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")
    try:
        data = storage.load(document.storage_key)
    except FileNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The file is missing from storage"
        ) from None
    return Response(
        content=data,
        media_type=document.mime_type,
        headers={
            "Content-Disposition": _disposition(document.original_filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def _disposition(filename: str | None) -> str:
    """
    `inline`, with the uploader's filename made safe for a header.

    The name is whatever the uploader sent. A quote, a backslash or a line
    break in it would otherwise end the header value early or start a new
    header, so the plain `filename` gets a sanitised ASCII copy and the exact
    name travels percent-encoded in `filename*` (RFC 6266), which browsers prefer.
    """
    if not filename:
        return "inline"
    plain = "".join(
        c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename
    )
    return f"inline; filename=\"{plain}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get(
    "/{document_id}/fields",
    response_model=DocumentFieldsOut,
    summary="Get a document's fields, for a person to read",
)
def get_fields(document_id: str, session: Session = Depends(get_session)) -> DocumentFieldsOut:
    """
    One document as the client portal shows it: every field in its processor's
    order, labelled and typed, with the problem holding it in review, whether
    it was worked out rather than read, and where on the original it was
    printed. Shaped for a person; `/result` is the shape for a system.
    """
    document = repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")
    return DocumentFieldsOut.model_validate(document_fields(session, document))


@router.patch(
    "/{document_id}/fields",
    response_model=DocumentFieldsOut,
    summary="Correct a document's fields, or approve it as it is",
    responses={
        409: {"model": FieldsRefusedOut, "description": "Changed since, or not read yet"},
        422: {"model": FieldsRefusedOut, "description": "Values that do not fit their fields"},
    },
)
def correct_fields(
    document_id: str,
    patch: FieldsPatchIn | None = Body(default=None),
    principal: Principal | None = Depends(require_principal),
    session: Session = Depends(get_session),
) -> DocumentFieldsOut | JSONResponse:
    """
    Correct values read from a document, approve it with its problems, or both.

    The values are laid over what was read and the document goes through its
    processor again: the transform works out what it worked out before, the
    rules run, and the document is ready -- and in the ledger -- or still in
    review, unless approved. `/result` gives the corrected values from then on.

    Send the `revision` the change was made against. If someone saved in
    between, the answer is 409 and nothing changes: load the document again.
    """
    document = repository.get_document(session, document_id)
    # Before the body is looked at, so another client's document answers
    # exactly as a missing one, whatever was sent.
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")
    if principal is not None and not principal.may_work:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Your role can see documents but not change them."
        )
    if patch is None:
        return _refused(422, "Send the revision and the values to change.")
    try:
        review.correct(
            session,
            document,
            revision=patch.revision,
            values=patch.values,
            approve=bool(patch.approve),
            by=principal.name if principal else "local",
        )
    except review.NothingRead:
        return _refused(409, "Nothing has been read from this document yet.")
    except review.Stale:
        return _refused(409, "Someone else changed this document since you opened it.")
    except review.Refused as exc:
        return _refused(422, str(exc), exc.fields)
    return DocumentFieldsOut.model_validate(document_fields(session, document))


def _refused(code: int, detail: str, fields: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        FieldsRefusedOut(detail=detail, fields=fields or {}).model_dump(), status_code=code
    )


@router.get(
    "/{document_id}/result",
    response_model=ResultOut,
    summary="Get a document's extracted data",
)
def get_result(document_id: str, session: Session = Depends(get_session)) -> ResultOut:
    """
    What a client system imports: the extracted fields, normalised, with what
    was read, what was worked out, what failed review, and the ledger row.

    Answers 200 in every state -- poll until `outcome` is not "processing".
    Only an unknown document is a 404.
    """
    document = repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")
    return ResultOut.model_validate(document_result(session, document))


@router.get("/{document_id}", response_model=DocumentDetail, summary="Get a document")
def get_document(
    document_id: str,
    session: Session = Depends(get_session),
) -> DocumentDetail:
    """Current status plus the full audit trail of how it got there."""
    document = repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such document")
    return DocumentDetail.model_validate(document)
