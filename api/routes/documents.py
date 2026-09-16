"""Upload and inspect documents."""

from __future__ import annotations

import binascii
from base64 import b64decode

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from api.schemas import Base64Upload, DocumentDetail, DocumentOut
from core import repository, storage
from core.config import get_settings
from core.db import get_session

router = APIRouter(prefix="/documents", tags=["documents"])


def _validate(data: bytes) -> str:
    """Size and type checks. Returns the detected mime type, or raises 4xx."""
    settings = get_settings()

    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is empty")

    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / 1024 / 1024
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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


def _ingest(session: Session, data: bytes, filename: str | None) -> DocumentOut:
    mime = _validate(data)
    document, created = repository.ingest(
        session, data=data, mime_type=mime, original_filename=filename
    )
    out = DocumentOut.model_validate(document)
    out.duplicate = not created
    return out


@router.post("", response_model=DocumentOut, summary="Upload a document")
def upload(
    file: UploadFile = File(description="Receipt or invoice: JPEG, PNG, WebP, HEIC, TIFF or PDF"),
    session: Session = Depends(get_session),
) -> DocumentOut:
    """
    Upload a file for processing.

    Deduplicated by SHA-256 of the contents: uploading the same bytes twice
    returns the original document with `duplicate: true` and queues no extra
    work. Safe to retry.
    """
    return _ingest(session, file.file.read(), file.filename)


@router.post("/base64", response_model=DocumentOut, summary="Upload as base64 JSON")
def upload_base64(
    payload: Base64Upload,
    session: Session = Depends(get_session),
) -> DocumentOut:
    """Same as `POST /documents`, for callers that already hold the bytes."""
    try:
        data = b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"content_b64 is not valid base64: {exc}"
        ) from exc
    return _ingest(session, data, payload.filename)


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
