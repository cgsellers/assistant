"""
/processors -- create, version, publish, retire and trial-run processors.

Thin on purpose: every rule lives in doctypes.authoring, which the panel and
the drafting agent call too. This module only translates HTTP into those
calls and their errors back into status codes, so the API's shape can change
without the rules moving.

A definition can be sent as JSON (`definition`) or as the seed YAML (`yaml`),
which is what the panel edits and what a person pastes from a seed file.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from api.routes.documents import check_upload
from core.auth import Principal, Role, require_role
from core.db import get_session
from core.models import DocumentTypeDefinition
from doctypes import authoring
from doctypes.authoring import AuthoringError, Conflict, NotFound

router = APIRouter(prefix="/processors", tags=["processors"])


# --- Dependencies -----------------------------------------------------------
# Factories rather than module globals, so tests override them and a route
# that never extracts never builds a client or needs an API key.


def get_extractor():
    from llm import get_extractor as build

    return build()


def get_ocr_engine():
    from ocr import get_engine

    return get_engine()


# Drafting and trying are author work; publishing and retiring put code live
# or take a processor away from live traffic, so they need an admin.
_AUTHOR = require_role(Role.AUTHOR)
_ADMIN = require_role(Role.ADMIN)


def author(principal: Principal = Depends(_AUTHOR)) -> str:
    """Who is doing this, for `created_by` and the log."""
    return principal.label


def admin(principal: Principal = Depends(_ADMIN)) -> str:
    return principal.label


# --- Shapes -----------------------------------------------------------------


class DefinitionIn(BaseModel):
    """Exactly one of `definition` (JSON) or `yaml` (the seed format)."""

    definition: dict | None = None
    yaml: str | None = Field(default=None, description="The processor in seed YAML")

    @model_validator(mode="after")
    def _one_of(self) -> DefinitionIn:
        if (self.definition is None) == (self.yaml is None):
            raise ValueError("send exactly one of `definition` or `yaml`")
        return self

    def spec(self) -> dict:
        if self.definition is not None:
            return self.definition
        try:
            loaded = yaml.safe_load(self.yaml or "")
        except yaml.YAMLError as exc:
            raise AuthoringError("the YAML does not parse", [str(exc)]) from None
        if not isinstance(loaded, dict):
            raise AuthoringError("the YAML must be a mapping, like a seed file")
        if isinstance(loaded.get("transform"), dict) and "file" in loaded["transform"]:
            raise AuthoringError(
                "`transform.file` only works in seed files; paste the code as "
                "`transform.code`, or upload the .py in the panel"
            )
        return loaded


class DraftIn(DefinitionIn):
    based_on_version: int | None = Field(
        default=None,
        description="The published version this edits; omit for a new processor",
    )


class TrialIn(BaseModel):
    document_id: str | None = None
    text: str | None = None


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    version: int
    display_name: str | None
    is_published: bool
    is_active: bool
    created_by: str | None
    based_on_version: int | None
    created_at: datetime
    updated_at: datetime


class VersionDetail(VersionOut):
    definition: dict


class SummaryOut(BaseModel):
    name: str
    display_name: str | None
    status: str
    active_version: int | None
    latest_published: int | None
    drafts: list[int]


class TrialOut(BaseModel):
    name: str
    version: int
    routed_to: str | None
    scores: dict[str, dict]
    raw: Any
    data: dict
    errors: list[str]
    derived: list[str]
    model: str
    cost_usd: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    chars: int
    extraction_id: str | None
    profile: str | None


def _detail(row: DocumentTypeDefinition) -> VersionDetail:
    return VersionDetail.model_validate(row, from_attributes=True)


# --- Errors -----------------------------------------------------------------


def authoring_error(_: Request, exc: AuthoringError) -> JSONResponse:
    """Registered on the app: one mapping from the rules' errors to HTTP."""
    if isinstance(exc, NotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, Conflict):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    body: dict[str, Any] = {"detail": str(exc), "problems": exc.details}
    if isinstance(exc, Conflict):
        body["latest_version"] = exc.latest
    return JSONResponse(body, status_code=code)


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=list[SummaryOut], summary="Every processor and its state")
def list_processors(session: Session = Depends(get_session)) -> list[SummaryOut]:
    return [
        SummaryOut(
            name=s.name,
            display_name=s.display_name,
            status=s.status,
            active_version=s.active_version,
            latest_published=s.latest_published,
            drafts=s.drafts,
        )
        for s in authoring.list_processors(session)
    ]


@router.get("/{name}", response_model=list[VersionOut], summary="A processor's versions")
def history(name: str, session: Session = Depends(get_session)) -> list[VersionOut]:
    return [VersionOut.model_validate(r) for r in authoring.history(session, name)]


@router.get(
    "/{name}/versions/{version}", response_model=VersionDetail, summary="One version in full"
)
def get_version(name: str, version: int, session: Session = Depends(get_session)):
    return _detail(authoring.get_version(session, name, version))


@router.post(
    "", response_model=VersionDetail, status_code=status.HTTP_201_CREATED,
    summary="Start a draft",
)
def create_draft(
    body: DraftIn, session: Session = Depends(get_session), who: str = Depends(author)
):
    """
    A draft of a new processor, or of a published version of an existing one
    (`based_on_version`). Nothing routes to a draft until it is published.
    """
    row = authoring.create_draft(
        session, body.spec(), based_on=body.based_on_version, created_by=who
    )
    return _detail(row)


@router.put(
    "/{name}/versions/{version}", response_model=VersionDetail, summary="Save over a draft"
)
def update_draft(
    name: str, version: int, body: DefinitionIn,
    session: Session = Depends(get_session), _: str = Depends(author),
):
    return _detail(authoring.update_draft(session, name, version, body.spec()))


@router.post(
    "/{name}/versions/{version}/publish", response_model=VersionDetail,
    summary="Make a draft the live version",
)
def publish(
    name: str, version: int,
    session: Session = Depends(get_session), who: str = Depends(admin),
):
    """
    409 if another version was published after this draft was started: the
    response names it, so the caller can merge from it.
    """
    return _detail(authoring.publish(session, name, version, published_by=who))


@router.post("/{name}/retire", response_model=VersionDetail, summary="Stop routing to it")
def retire(name: str, session: Session = Depends(get_session), who: str = Depends(admin)):
    return _detail(authoring.retire(session, name, retired_by=who))


@router.post(
    "/{name}/versions/{version}/trial", response_model=TrialOut,
    summary="Try a version on a stored document or on text",
)
def trial(
    name: str, version: int, body: TrialIn,
    session: Session = Depends(get_session), extractor=Depends(get_extractor),
    _: str = Depends(author),
):
    """
    Billed like any extraction. On a stored document the attempt is recorded
    as a trial -- in the cost report, never the document's result.
    """
    result = authoring.trial_run(
        session, name, version, extractor=extractor,
        text=body.text, document_id=body.document_id,
    )
    return TrialOut(**vars(result))


@router.post(
    "/{name}/versions/{version}/trial/file", response_model=TrialOut,
    summary="Try a version on an uploaded sample",
)
def trial_file(
    name: str, version: int,
    file: UploadFile = File(description="A sample: image or PDF"),
    session: Session = Depends(get_session),
    extractor=Depends(get_extractor), engine=Depends(get_ocr_engine),
    _: str = Depends(author),
):
    """
    Read like a real document (text layer, else OCR) but not ingested, so the
    real copy is not deduplicated away when it arrives. Not recorded.
    """
    data = file.file.read()
    text = authoring.text_from_file(data, check_upload(data), engine)
    result = authoring.trial_run(session, name, version, extractor=extractor, text=text)
    return TrialOut(**vars(result))
