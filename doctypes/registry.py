"""
Loading document types out of the database and routing a document to one.

Routing is deterministic and free -- `match_score` is string and regex matching
over OCR text -- so every registered type is scored against every document and
the one with the most evidence wins. That is the whole point of keeping matchers in the
definition: a model classifier only has to be paid for when nothing matches,
which is what `route` returning None tells the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.models import DocumentTypeDefinition, Processor, ProcessorAccess
from doctypes.definition import Definition, Profile
from doctypes.schema import match_evidence, select_profile

log = logging.getLogger("doctypes.registry")

# Parsed definitions, keyed by (row id, updated_at).
#
# The SELECT is never skipped: which version of a type is active is exactly the
# thing an analyst changes, and serving a deactivated type from memory is a
# correctness bug. What is cached is only the Pydantic validation of the JSON
# column, which is pure and depends on nothing but the row. Keying on
# updated_at means an in-place edit of an unpublished draft invalidates its own
# entry -- the row cannot change without the key changing -- so there is no
# invalidation protocol to get wrong.
#
# The cost that remains is one indexed query per document, against an LLM call
# that takes seconds. The cost avoided is re-validating every rule and profile
# of every registered type on every document.
_PARSED: dict[tuple[str, datetime], Definition] = {}

# Superseded keys are never removed individually, so the map grows by one entry
# per edit. Dropping the lot past a threshold is cruder than an LRU and costs
# one re-parse per live type afterwards, which is cheaper than maintaining
# eviction order for a map that in practice holds a few dozen entries.
_CACHE_LIMIT = 512


@dataclass(frozen=True, slots=True)
class RouteResult:
    """The winning type, plus what routing learned on the way there."""

    definition: Definition
    #: The issuer profile key, or None when this issuer is not yet known. Worth
    #: carrying separately from `profile`: it is what goes on
    #: `extractions.profile`, which is how "did the new hints help?" is answered.
    profile_key: str | None
    profile: Profile | None
    score: float


def clear_cache() -> None:
    """Drop parsed definitions. For tests and for a process that wants a cold start."""
    _PARSED.clear()


def active_definitions(
    session: Session, organisation_id: str | None = None
) -> list[Definition]:
    """
    Every type new documents can currently route to -- for one organisation,
    or, with None, regardless of who may use them (the eval harness, the
    team's own tools).

    Unpublished rows are excluded because a type an analyst is still authoring
    against sample documents must not start catching live traffic halfway
    through being written.

    Ordered by name so that routing ties break the same way every run.
    """
    return [candidate.definition for candidate in candidates(session, organisation_id)]


@dataclass(frozen=True, slots=True)
class Candidate:
    definition: Definition
    #: Granted to this organisation specifically, rather than everyone's.
    #: Wins an exact tie against a shared processor (DESIGN §9.3).
    dedicated: bool = False


def candidates(session: Session, organisation_id: str | None = None) -> list[Candidate]:
    """
    The live versions an organisation's documents may route to: processors
    available to every organisation, plus those granted to this one. A
    processor nobody has been given access to routes nothing.
    """
    stmt = (
        select(DocumentTypeDefinition, Processor.available_to_all)
        .join(Processor, Processor.name == DocumentTypeDefinition.name)
        .where(
            DocumentTypeDefinition.is_active.is_(True),
            DocumentTypeDefinition.is_published.is_(True),
        )
        .order_by(DocumentTypeDefinition.name)
    )
    if organisation_id is not None:
        granted = (
            select(ProcessorAccess.processor_name)
            .where(
                ProcessorAccess.processor_name == Processor.name,
                ProcessorAccess.organisation_id == organisation_id,
            )
            .exists()
        )
        stmt = stmt.where(or_(Processor.available_to_all.is_(True), granted))

    found = []
    for row, available_to_all in session.execute(stmt):
        definition = _parse(row)
        if definition is not None:
            dedicated = organisation_id is not None and not available_to_all
            found.append(Candidate(definition, dedicated))
    return found


def route(
    session: Session, ocr_text: str, organisation_id: str | None = None
) -> RouteResult | None:
    """
    Pick the document type this text looks most like, or None.

    Among the processors `organisation_id` may use; None considers every live
    one, for callers that are not routing a client's document.

    None is an ordinary outcome, not an error: it means no registered type
    cleared its own `min_score`, and the caller should escalate to a model
    classifier (or mark the document unsupported) rather than guess.
    """
    picked = pick(candidates(session, organisation_id), ocr_text)
    if picked is None:
        return None
    best, best_score = picked

    # Only the winner's profiles are worth scanning: a profile on a type we are
    # not extracting with changes nothing.
    profile_key, profile = select_profile(best, ocr_text)
    return RouteResult(
        definition=best,
        profile_key=profile_key,
        profile=profile,
        score=best_score,
    )


def scores(
    session: Session, ocr_text: str, organisation_id: str | None = None
) -> dict[str, dict[str, float]]:
    """
    Every routable type's evidence for this text: {name: {"hits": n, "score": s}}.

    Recorded with the routing decision, because the winner's score alone does
    not explain a wrong route ("invoice 2 signals, receipt 3") and an
    unsupported document -- where nobody won -- would otherwise keep nothing.
    Only the types this organisation may use: those were the choice.
    """
    return {
        d.name: dict(zip(("hits", "score"), match_evidence(d, ocr_text), strict=True))
        for d in active_definitions(session, organisation_id)
    }


def pick(
    options: list[Definition] | list[Candidate], ocr_text: str
) -> tuple[Definition, float] | None:
    """
    The winning type and its score, or None. Pure, so it can be run over a
    corpus without a database.

    Most signals hit wins; the share of the type's own signals breaks a tie;
    a processor dedicated to the organisation breaks an exact tie with a
    shared one. Ranking by share alone made a type lose for having more
    keywords -- see `match_evidence`.
    """
    best: Definition | None = None
    best_key: tuple = (0, 0.0, False)

    for option in options:
        candidate = option if isinstance(option, Candidate) else Candidate(option)
        hits, score = match_evidence(candidate.definition, ocr_text)
        key = (hits, score, candidate.dedicated)
        # match_evidence has already applied this type's own min_score and
        # returned zeros below it, so anything surviving a strict `>` against
        # a zero floor is a real match. Strict rather than >= also means a tie
        # keeps the first type in name order, which makes routing reproducible
        # instead of dependent on row insertion order.
        if hits and key > best_key:
            best, best_key = candidate.definition, key

    return None if best is None else (best, best_key[1])


def _parse(row: DocumentTypeDefinition) -> Definition | None:
    """
    Validate one row's JSON, or None if it does not validate.

    Skipping rather than raising: a definition is data, and data arrives broken
    -- a hand-edited row, a seed file from an older format, a column written
    before a field became required. Letting one bad row raise would stop every
    other document type from routing, turning one analyst's typo into a full
    outage. The row is logged at error level on every attempt, which is noisy
    by design: it stays broken until someone fixes it.
    """
    key = (row.id, row.updated_at)
    cached = _PARSED.get(key)
    if cached is not None:
        return cached

    try:
        definition = Definition.model_validate(row.definition)
    except ValidationError as exc:
        log.error(
            "document type %s v%s (%s) has an invalid definition and was skipped: %s",
            row.name,
            row.version,
            row.id,
            exc,
        )
        return None

    if len(_PARSED) >= _CACHE_LIMIT:
        _PARSED.clear()
    _PARSED[key] = definition
    return definition
