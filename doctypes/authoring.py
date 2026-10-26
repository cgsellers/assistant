"""
Authoring processors: the rules every way of editing one goes through.

The JSON API, the panel and (later) the drafting agent are all thin callers of
these functions, so a processor is checked the same way however it arrives:
the definition format, the nullable cap, the transform's examples, and whether
somebody else published first.

The lifecycle, in the vocabulary of `document_types`:

  * **draft**: unpublished, inactive. Editable in place until published --
    nothing routes to it and no real extraction cites it, so there is nothing
    for an edit to invalidate.
  * **published**: immutable from then on. Publishing makes it the active
    version, the one routing uses. Extractions cite it by version number.
  * **retired**: no version active. The type stops routing and keeps its
    history; publishing a new draft brings it back.

Nothing is ever deleted. Extractions name the version that produced them, and
a ledger row traces back through its extraction; deleting a version would cut
that thread.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from core import repository
from core.models import (
    DocumentTypeDefinition,
    Extraction,
    Organisation,
    Processor,
    ProcessorAccess,
    utcnow,
)
from doctypes import registry
from doctypes.definition import Definition
from doctypes.schema import build_json_schema, match_evidence, select_profile
from doctypes.transform import check_examples, process, to_wire
from ocr.layout import blocks_from_json, extraction_text

log = logging.getLogger("doctypes.authoring")


class AuthoringError(Exception):
    """The request cannot be carried out as asked. `details` says exactly why."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


class NotFound(AuthoringError):
    pass


class Conflict(AuthoringError):
    """
    Somebody published since this draft was started.

    The lost-update guard: without it, publishing a draft of v7 after a
    colleague published v8 would quietly undo v8. `latest` is the version to
    look at and merge from.
    """

    def __init__(self, message: str, *, latest: int | None):
        super().__init__(message)
        self.latest = latest


@dataclass(frozen=True)
class Summary:
    name: str
    display_name: str | None
    #: The version routing uses, or None when retired or never published.
    active_version: int | None
    latest_published: int | None
    drafts: list[int]
    #: Who may use it: everyone, or this many organisations (0 is nobody).
    available_to_all: bool = False
    granted: int = 0

    @property
    def unreachable(self) -> bool:
        """Live, and yet no organisation may be routed to it."""
        return self.active_version is not None and not self.available_to_all and not self.granted

    @property
    def status(self) -> str:
        if self.active_version is not None:
            return "active"
        return "retired" if self.latest_published is not None else "draft"


# --- Reading ----------------------------------------------------------------


def list_processors(session: Session) -> list[Summary]:
    rows = session.scalars(
        select(DocumentTypeDefinition).order_by(
            DocumentTypeDefinition.name, DocumentTypeDefinition.version
        )
    ).all()
    by_name: dict[str, list[DocumentTypeDefinition]] = {}
    for row in rows:
        by_name.setdefault(row.name, []).append(row)

    processors = {p.name: p for p in session.scalars(select(Processor))}
    granted = dict(
        session.execute(
            select(ProcessorAccess.processor_name, func.count())
            .group_by(ProcessorAccess.processor_name)
        ).all()
    )
    summaries = []
    for name, versions in by_name.items():
        published = [r.version for r in versions if r.is_published]
        active = next((r.version for r in versions if r.is_active), None)
        summaries.append(
            Summary(
                name=name,
                display_name=versions[-1].display_name,
                active_version=active,
                latest_published=max(published) if published else None,
                drafts=[r.version for r in versions if not r.is_published],
                available_to_all=bool(processors.get(name) and processors[name].available_to_all),
                granted=granted.get(name, 0),
            )
        )
    return summaries


def history(session: Session, name: str) -> list[DocumentTypeDefinition]:
    """Every version of a processor, newest first."""
    rows = session.scalars(
        select(DocumentTypeDefinition)
        .where(DocumentTypeDefinition.name == name)
        .order_by(DocumentTypeDefinition.version.desc())
    ).all()
    if not rows:
        raise NotFound(f"no processor called {name!r}")
    return list(rows)


def extraction_count(session: Session, name: str) -> int:
    """How many real (non-trial) extractions a processor has produced, ever."""
    return session.scalar(
        select(func.count(Extraction.id)).where(
            Extraction.doc_type == name, Extraction.is_trial.is_(False)
        )
    ) or 0


def get_version(session: Session, name: str, version: int) -> DocumentTypeDefinition:
    row = session.scalar(
        select(DocumentTypeDefinition).where(
            DocumentTypeDefinition.name == name,
            DocumentTypeDefinition.version == version,
        )
    )
    if row is None:
        raise NotFound(f"{name} has no version {version}")
    return row


def parse(spec: dict | Definition) -> Definition:
    """A definition, or AuthoringError listing every problem with a path to it."""
    if isinstance(spec, Definition):
        return spec
    try:
        return Definition.model_validate(spec)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(p) for p in error['loc']) or 'definition'}: {error['msg']}"
            for error in exc.errors()
        ]
        raise AuthoringError("not a valid processor", details) from None


def to_yaml(stored: dict | Definition) -> str:
    """
    A version in the seed format: what the panel shows, diffs and edits.

    Defaults are left out, so a field reads `type: money` and its
    description rather than five lines of `required: false` and nulls; the
    version number is left out because the server assigns it. Multi-line
    strings -- a transform's code -- come out as `|` blocks, so code reads
    and diffs as code. Parsing the result gives back the same definition.
    """
    definition = parse(stored if isinstance(stored, Definition) else dict(stored))
    data = definition.model_dump(mode="json", exclude_defaults=True, exclude={"version"})
    for spec in data.get("fields", {}).values():
        _type_first(spec)
        for item in (spec.get("item_fields") or {}).values():
            _type_first(item)
    # A rule's `type` is a default too -- each rule class fixes its own -- but
    # it is the discriminator, so without it the YAML would not parse back.
    if definition.rules:
        data["rules"] = [
            {"type": rule.type, **dumped}
            for rule, dumped in zip(definition.rules, data["rules"], strict=True)
        ]
    return dump_yaml(data)


def editable(stored: dict | Definition) -> tuple[str, str]:
    """
    A version split for the editor: the YAML without the transform's code,
    and the code on its own. Python inside a YAML block is miserable to edit
    -- one wrong indent and it is a different string -- so the panel gives
    each its own box and `join_edit` puts them back together.
    """
    data = yaml.safe_load(to_yaml(stored))
    code = ""
    if isinstance(data.get("transform"), dict):
        code = data["transform"].pop("code", "")
    return dump_yaml(data), code


def join_edit(text: str, code: str) -> dict:
    """The editor's two boxes back into one definition, or AuthoringError."""
    try:
        spec = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise AuthoringError("the YAML does not parse", [str(exc)]) from None
    if not isinstance(spec, dict):
        raise AuthoringError("the YAML must be a mapping, like a seed file")
    code = (code or "").replace("\r\n", "\n")
    transform = spec.get("transform")
    if code.strip():
        spec["transform"] = {**(transform if isinstance(transform, dict) else {}), "code": code}
    elif isinstance(transform, dict) and "code" not in transform:
        if transform.get("examples"):
            raise AuthoringError("the transform has examples but no code")
        spec.pop("transform")
    return spec


def dump_yaml(data: dict) -> str:
    """Any spec as seed-style YAML, valid or not -- for showing a draft to fix."""
    return yaml.dump(data, Dumper=_SeedDumper, sort_keys=False, allow_unicode=True, width=88)


def diff(older: dict, newer: dict, *, older_label: str, newer_label: str) -> list[str]:
    """Unified diff of two versions in their YAML form."""
    return list(
        difflib.unified_diff(
            to_yaml(older).splitlines(),
            to_yaml(newer).splitlines(),
            fromfile=older_label,
            tofile=newer_label,
            lineterm="",
        )
    )


def _type_first(spec: dict) -> None:
    """`type: string` is the default, but a field reads better saying so."""
    kind = spec.pop("type", "string")
    rest = dict(spec)
    spec.clear()
    spec.update({"type": kind, **rest})


class _SeedDumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_SeedDumper.add_representer(str, _str)


# --- Writing ----------------------------------------------------------------


def create_draft(
    session: Session,
    spec: dict | Definition,
    *,
    based_on: int | None,
    created_by: str,
) -> DocumentTypeDefinition:
    """
    Start a new draft, either of a new processor (`based_on` None) or from a
    published version of an existing one.

    Drafting from a published version only: a draft of a draft has no stable
    base to check against at publish time.
    """
    definition = parse(spec)
    latest = latest_published(session, definition.name)

    if based_on is None and latest is not None:
        raise Conflict(
            f"{definition.name} already exists; start the draft from v{latest}",
            latest=latest,
        )
    if based_on is not None:
        base = get_version(session, definition.name, based_on)
        if not base.is_published:
            raise AuthoringError(
                f"v{based_on} is a draft; start from a published version instead"
            )

    row = repository.create_document_type_version(
        session,
        definition,
        publish=False,
        activate=False,
        created_by=created_by,
        based_on_version=based_on,
    )
    log.info("%s drafted %s v%d from v%s", created_by, row.name, row.version, based_on)
    return row


def update_draft(
    session: Session, name: str, version: int, spec: dict | Definition
) -> DocumentTypeDefinition:
    """Save over a draft. A published version is never edited."""
    row = get_version(session, name, version)
    if row.is_published:
        raise AuthoringError(
            f"{name} v{version} is published, and published versions never change; "
            "start a draft from it instead"
        )
    definition = parse(spec)
    if definition.name != name:
        raise AuthoringError("a draft cannot be renamed; start a new processor instead")

    stored = definition.model_copy(update={"version": version})
    row.definition = stored.model_dump(mode="json")
    row.display_name = stored.display_name
    row.description = stored.description
    session.commit()
    return row


def publish(
    session: Session, name: str, version: int, *, published_by: str
) -> DocumentTypeDefinition:
    """
    Make a draft the version routing uses.

    Checked in this order, cheapest first: still a draft; still valid (the
    format may have tightened since it was saved); nobody published since it
    was started; the transform passes its examples. The previous active
    version is demoted in the same transaction, so there is never a moment
    with two active versions or none.
    """
    row = get_version(session, name, version)
    if row.is_published:
        raise AuthoringError(f"{name} v{version} is already published")

    definition = parse(row.definition)

    # Two publishes of the same type racing would both pass the check below
    # and the second would silently demote the first. Locking the type's rows
    # makes the second wait, then see the first's version and conflict. A
    # no-op on SQLite, which lets one writer in at a time anyway.
    session.scalars(
        select(DocumentTypeDefinition.id)
        .where(DocumentTypeDefinition.name == name)
        .with_for_update()
    ).all()
    latest = latest_published(session, name)
    if latest != row.based_on_version:
        started = f"v{row.based_on_version}" if row.based_on_version else "nothing"
        raise Conflict(
            f"{name} v{latest} was published after this draft was started from "
            f"{started}; merge its changes into a new draft from v{latest}",
            latest=latest,
        )

    failures = check_examples(definition)
    if failures:
        raise AuthoringError("the transform fails its examples", failures)

    session.execute(
        update(DocumentTypeDefinition)
        .where(
            DocumentTypeDefinition.name == name,
            DocumentTypeDefinition.is_active.is_(True),
        )
        .values(is_active=False)
    )
    row.is_published = True
    row.is_active = True
    session.commit()
    log.info("%s published %s v%d", published_by, name, version)
    return row


def retire(session: Session, name: str, *, retired_by: str) -> DocumentTypeDefinition:
    """Stop routing to a processor. Its versions, and what they produced, stay."""
    active = repository.get_active_document_type(session, name)
    if active is None:
        history(session, name)  # NotFound if it never existed
        raise AuthoringError(f"{name} is not active, so there is nothing to retire")
    active.is_active = False
    session.commit()
    log.info("%s retired %s (was v%d)", retired_by, name, active.version)
    return active


def latest_published(session: Session, name: str) -> int | None:
    """The newest published version, live or retired. What a draft must be based on."""
    return session.scalar(
        select(func.max(DocumentTypeDefinition.version)).where(
            DocumentTypeDefinition.name == name,
            DocumentTypeDefinition.is_published.is_(True),
        )
    )


@dataclass(frozen=True)
class PublishPreview:
    """Everything a person should see before putting a draft live."""

    row: DocumentTypeDefinition
    #: What it replaces: the live version, else the newest published one (a
    #: retired type coming back). None for a brand-new processor.
    against: DocumentTypeDefinition | None
    diff: list[str]
    #: The transform's code differs from what it replaces -- new code will run
    #: on the server. The one change worth a second look before confirming.
    code_changed: bool
    example_failures: list[str]
    #: Set when someone published after this draft was started: publishing
    #: would be refused, so the preview says so up front.
    conflict_with: int | None

    @property
    def can_publish(self) -> bool:
        return self.conflict_with is None and not self.example_failures


def publish_preview(session: Session, name: str, version: int) -> PublishPreview:
    """
    What `publish` would do, checked the same way, without doing it.

    Runs the transform's examples, so it costs an interpreter per example --
    the same price publishing pays, paid while a person can still read the
    failures.
    """
    row = get_version(session, name, version)
    if row.is_published:
        raise AuthoringError(f"{name} v{version} is already published")
    definition = parse(row.definition)

    active = repository.get_active_document_type(session, name)
    latest = latest_published(session, name)
    against = active or (get_version(session, name, latest) if latest else None)

    if against is not None:
        label = f"v{against.version}" + (" (live)" if against.is_active else "")
        lines = diff(
            against.definition, row.definition,
            older_label=label, newer_label=f"v{version} (this draft)",
        )
        old_code = (against.definition.get("transform") or {}).get("code")
    else:
        lines, old_code = [], None
    new_code = definition.transform.code if definition.transform else None

    return PublishPreview(
        row=row,
        against=against,
        diff=lines,
        code_changed=old_code != new_code,
        example_failures=check_examples(definition),
        conflict_with=latest if latest != row.based_on_version else None,
    )


# --- Who may use it ---------------------------------------------------------
# DESIGN §9.3: a processor is for every organisation, or for those granted it.
# Admin-only at the API and the panel; these functions trust their caller to
# have checked.


@dataclass(frozen=True)
class Access:
    name: str
    available_to_all: bool
    #: Organisations granted it, by name -- meaningful when not everyone's.
    organisations: list[Organisation]
    changed_at: datetime | None
    changed_by: str | None

    @property
    def nobody(self) -> bool:
        return not self.available_to_all and not self.organisations


def access(session: Session, name: str) -> Access:
    processor = session.get(Processor, name)
    if processor is None:
        raise NotFound(f"no processor called {name!r}")
    organisations = list(
        session.scalars(
            select(Organisation)
            .join(ProcessorAccess, ProcessorAccess.organisation_id == Organisation.id)
            .where(ProcessorAccess.processor_name == name)
            .order_by(Organisation.name)
        )
    )
    return Access(
        name=name,
        available_to_all=processor.available_to_all,
        organisations=organisations,
        changed_at=processor.access_changed_at,
        changed_by=processor.access_changed_by,
    )


def may_use(session: Session, name: str, organisation_id: str) -> bool:
    """Whether documents of `organisation_id` may be routed to processor `name`."""
    processor = session.get(Processor, name)
    if processor is None:
        return False
    if processor.available_to_all:
        return True
    return session.get(ProcessorAccess, (name, organisation_id)) is not None


def set_access(
    session: Session,
    name: str,
    *,
    available_to_all: bool,
    organisation_ids: list[str],
    changed_by: str,
) -> Access:
    """
    Replace who may use a processor. Grants that stay keep their original
    date and author; only additions are new. Takes effect on the next
    document routed -- access is not versioned, so taking it away does not
    wait for anything to be published.
    """
    processor = session.get(Processor, name)
    if processor is None:
        raise NotFound(f"no processor called {name!r}")
    wanted = set(organisation_ids)
    known = set(session.scalars(select(Organisation.id).where(Organisation.id.in_(wanted))))
    if unknown := wanted - known:
        raise AuthoringError("no such organisation", sorted(unknown))

    current = {grant.organisation_id: grant for grant in processor.grants}
    for organisation_id in set(current) - wanted:
        processor.grants.remove(current[organisation_id])
    for organisation_id in wanted - set(current):
        processor.grants.append(
            ProcessorAccess(organisation_id=organisation_id, granted_by=changed_by)
        )
    processor.available_to_all = available_to_all
    processor.access_changed_at = utcnow()
    processor.access_changed_by = changed_by
    session.commit()
    log.info(
        "%s set access on %s: %s", changed_by, name,
        "every organisation" if available_to_all else f"{len(wanted)} organisation(s)",
    )
    return access(session, name)


# --- Trying it --------------------------------------------------------------


@dataclass
class Trial:
    """What a version does to one document, without the result counting."""

    name: str
    version: int
    #: What routing would pick with this version standing in for its type.
    routed_to: str | None
    #: Every candidate's evidence: {name: {"hits": n, "score": s}}.
    scores: dict[str, dict]
    #: What the model returned, and what came out after the transform.
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
    #: Set when the trial ran on a stored document and was recorded.
    extraction_id: str | None = None
    profile: str | None = field(default=None)
    #: On a stored document: whether its organisation may use this processor.
    #: False means it would never be routed there, however well it matched.
    allowed: bool | None = None


def trial_run(
    session: Session,
    name: str,
    version: int,
    *,
    extractor,
    text: str | None = None,
    document_id: str | None = None,
) -> Trial:
    """
    Run one version against one document and report everything.

    Works on drafts as well as published versions -- that is the point of it.
    A trial on a stored document is recorded as an `is_trial` extraction, so
    its cost appears in the cost report, but it is never the document's
    result and never reaches the ledger. A trial on loose text has no
    document to attach to, so it is returned and not stored; a sample is
    deliberately not ingested, because ingesting would deduplicate the real
    copy away when it later arrives.
    """
    from llm.base import ExtractionError

    row = get_version(session, name, version)
    definition = parse(row.definition)

    organisation_id = None
    if document_id is not None:
        text = _document_text(session, document_id)
        organisation_id = repository.get_document(session, document_id).organisation_id
    elif not text or not text.strip():
        raise AuthoringError("give a stored document or some text to try it on")

    # On a stored document, routing as it would run for that document: among
    # the processors its organisation may use. The version being tried always
    # takes part -- it is what is being tried -- and `allowed` says whether
    # that organisation could actually be routed to it once it is live.
    others = [
        c for c in registry.candidates(session, organisation_id) if c.definition.name != name
    ]
    allowed = None if organisation_id is None else may_use(session, name, organisation_id)
    tried = registry.Candidate(
        definition,
        dedicated=bool(allowed) and not access(session, name).available_to_all,
    )
    candidates = sorted([*others, tried], key=lambda c: c.definition.name)
    picked = registry.pick(candidates, text)
    scores = {
        c.definition.name: dict(
            zip(("hits", "score"), match_evidence(c.definition, text), strict=True)
        )
        for c in candidates
    }

    profile_key, profile = select_profile(definition, text)
    try:
        result = extractor.extract(
            json_schema=build_json_schema(definition, profile), text=text, doc_type=name
        )
    except ExtractionError as exc:
        raise AuthoringError(f"the extraction failed: {exc}") from exc

    processed = process(definition, result.data)

    extraction_id = None
    if document_id is not None:
        extraction = repository.record_extraction(
            session,
            document_id=document_id,
            doc_type=name,
            schema_version=version,
            model_used=result.model,
            profile=profile_key,
            raw_json=result.data,
            validation_errors=processed.errors,
            transformed_json=processed.transformed,
            derived_fields=processed.derived if definition.transform else None,
            is_trial=True,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        extraction_id = extraction.id

    return Trial(
        name=name,
        version=version,
        routed_to=picked[0].name if picked else None,
        scores=scores,
        raw=result.data,
        data=to_wire(processed.data),
        errors=processed.errors,
        derived=processed.derived,
        model=result.model,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        chars=len(text),
        extraction_id=extraction_id,
        profile=profile_key,
        allowed=allowed,
    )


def text_from_file(data: bytes, mime_type: str, engine) -> str:
    """
    A sample file's text, read the same way the pipeline reads a document:
    the PDF text layer when there is one, OCR otherwise, rows rebuilt.
    """
    output = engine.read(data, mime_type)
    return extraction_text(output.full_text, output.blocks)


def _document_text(session: Session, document_id: str) -> str:
    if repository.get_document(session, document_id) is None:
        raise NotFound(f"no document {document_id}")
    ocr = repository.get_ocr_result(session, document_id)
    if ocr is None:
        raise AuthoringError(f"document {document_id} has not been through OCR yet")
    return extraction_text(ocr.full_text or "", blocks_from_json(ocr.layout_blocks))
