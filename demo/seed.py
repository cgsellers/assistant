"""
A demo client, Acme Demo Ltd, with its people and about thirty synthetic invoices
and receipts, read through the real pipeline without a single paid call.

    uv run python -m demo.seed              # make it (refuses if it exists)
    uv run python -m demo.seed --replace    # wipe it and make it again

Every document goes the way an upload does: stored, OCR'd (its PDF's text
layer, read locally), routed to a processor, extracted, transformed, checked
by the rules, into the ledger or held for review, with its list columns and
its history. The one difference is the model: the documents are generated,
so what a model would read from each is known, and an extractor that answers
with it stands in -- at no cost, and the same every time.

Then it is made to look lived in: each document dated to when it "arrived"
over the last month, one receipt corrected by a member, an Italian one
approved as it is by the admin. `--replace` is what a public demo would run
every night.
"""

from __future__ import annotations

import argparse
import os
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from core import repository, tenancy, users
from core.config import get_settings
from core.models import (
    ApiKey,
    Correction,
    Document,
    DocumentEvent,
    Extraction,
    Invitation,
    Job,
    OcrResult,
    Organisation,
    Transaction,
    User,
    UserSession,
    utcnow,
)
from core.scoping import Scope, set_scope
from core.status import DocStatus, JobKind, JobStatus
from demo.documents import Synthetic, catalogue
from doctypes import review
from llm.base import ExtractionResult
from ocr.base import OcrError, OcrOutput
from ocr.pdftext import read_text_layer
from worker import run as worker

NAME, SLUG = "Acme Demo Ltd", "acme-demo"
DEFAULT_PASSWORD = "acme demo passphrase"
PEOPLE = (
    ("admin@acme.example", "admin"),
    ("member@acme.example", "member"),
    ("viewer@acme.example", "viewer"),
)


class TextLayer:
    """OCR for the demo: its PDFs' own text layers, read locally."""

    name = "pdf-text"

    def read(self, data: bytes, mime_type: str) -> OcrOutput:
        output = read_text_layer(data)
        if output is None:
            raise OcrError("the demo's documents all have a text layer")
        return output


class KnownAnswers:
    """
    Stands in for the model: answers each document with what it says, known
    because the document was generated. Found by the reference it prints.
    """

    model = "demo-known-answers"

    def __init__(self, documents: list[Synthetic]):
        self.answers = {d.key: d.answer for d in documents if d.answer is not None}

    def extract(self, *, json_schema: dict, text: str, doc_type: str) -> ExtractionResult:
        for key, answer in self.answers.items():
            if key in text:
                return ExtractionResult(data=answer, model=self.model, cost_usd=Decimal("0"))
        return ExtractionResult(data={}, model=self.model, cost_usd=Decimal("0"))


SEEDER = "demo-seed"


@contextmanager
def held(session: Session):
    """
    Every job this session makes is the seed's from the moment it is written:
    never queued, so a worker that is running cannot take one and read a demo
    document with the real, paid model. The seed then runs its own jobs.
    """

    def hold(session_, _context, _instances) -> None:
        for row in session_.new:
            if isinstance(row, Job) and row.status in (None, JobStatus.QUEUED):
                row.status, row.locked_by, row.locked_at = JobStatus.RUNNING, SEEDER, utcnow()
                row.attempts = 1

    event.listen(session, "before_flush", hold)
    try:
        yield
    finally:
        event.remove(session, "before_flush", hold)


def _job(session: Session, document: Document, kind: str) -> Job | None:
    """This document's job of this kind, held for the seed."""
    return session.scalar(
        select(Job).where(
            Job.document_id == document.id,
            Job.kind == kind,
            Job.locked_by == SEEDER,
            Job.status == JobStatus.RUNNING,
        )
    )


def wipe(session: Session, organisation: Organisation) -> None:
    """Everything the demo client has, rows and files; the organisation itself stays."""
    org = organisation.id
    for model in (Correction, Transaction, Extraction, Job, DocumentEvent, OcrResult, Document):
        session.execute(delete(model).where(model.organisation_id == org))
    people = select(User.id).where(User.organisation_id == org)
    session.execute(delete(UserSession).where(UserSession.user_id.in_(people)))
    for model in (Invitation, User, ApiKey):
        session.execute(delete(model).where(model.organisation_id == org))
    session.commit()
    # A folder per organisation (core/storage.py): one to remove.
    shutil.rmtree(get_settings().storage_dir / "raw" / org, ignore_errors=True)


def _arrived(session: Session, document: Document, at: datetime) -> None:
    """Date a document, and everything that happened to it, to when it 'arrived'."""
    document.uploaded_at = at
    for i, happened in enumerate(
        session.scalars(
            select(DocumentEvent)
            .where(DocumentEvent.document_id == document.id)
            .order_by(DocumentEvent.created_at)
        )
    ):
        happened.created_at = at + timedelta(seconds=4 + i * 6)
    for row in session.scalars(select(Extraction).where(Extraction.document_id == document.id)):
        row.created_at = at + timedelta(seconds=21)
    for row in session.scalars(select(Transaction).where(Transaction.document_id == document.id)):
        row.created_at = at + timedelta(seconds=27)


def seed(
    session: Session, *, replace: bool = False, password: str = DEFAULT_PASSWORD
) -> Organisation:
    organisation = tenancy.by_slug(session, SLUG)
    if organisation is not None and not replace:
        raise SystemExit(f"{NAME} ({SLUG}) is already there; --replace wipes it and starts again")
    if organisation is None:
        organisation = tenancy.create(session, NAME, SLUG)
    else:
        wipe(session, organisation)

    for email, role in PEOPLE:
        users.create(
            session, email, password, organisation_id=organisation.id, is_staff=False, role=role
        )

    today = utcnow().date()
    documents = catalogue(today)
    engine, extractor = TextLayer(), KnownAnswers(documents)
    made: list[tuple[Synthetic, Document]] = []
    with held(session):
        for synthetic in documents:
            document, _ = repository.ingest(
                session,
                organisation_id=organisation.id,
                data=synthetic.pdf,
                mime_type="application/pdf",
                original_filename=synthetic.filename,
            )
            worker.run_ocr_job(session, _job(session, document, JobKind.OCR), engine)
            if (job := _job(session, document, JobKind.EXTRACT)) is not None:
                worker.run_extract_job(session, job, extractor)
            session.refresh(document)
            made.append((synthetic, document))

    for synthetic, document in made:
        arrived = datetime.combine(synthetic.received, time(9), tzinfo=UTC) + timedelta(
            days=1, hours=len(synthetic.filename) % 9, minutes=len(synthetic.key) * 7 % 60
        )
        _arrived(session, document, min(arrived, utcnow() - timedelta(minutes=5)))
    session.commit()

    _lived_in(session, made)
    return organisation


def _lived_in(session: Session, made: list[tuple[Synthetic, Document]]) -> None:
    """
    Two things people did: a member fixed a receipt's base, and the admin
    approved one Italian receipt as it is. The other Italian one is left in
    review, for whoever tries the demo to approve.
    """
    approved = False
    for synthetic, document in made:
        if document.status != DocStatus.NEEDS_REVIEW or synthetic.answer is None:
            continue
        if "base plus IVA" in synthetic.shows:
            right = Decimal(synthetic.answer["total"]) - Decimal(synthetic.answer["tax_amount"])
            review.correct(
                session, document, revision=document.revision,
                values={"subtotal": str(right)}, approve=False, by="member@acme.example",
            )  # fmt: skip
        elif "Italian" in synthetic.shows and not approved:
            approved = True
            review.correct(
                session, document, revision=document.revision,
                values={}, approve=True, by="admin@acme.example",
            )  # fmt: skip


def main() -> None:
    from core.db import SessionLocal

    parser = argparse.ArgumentParser(
        prog="python -m demo.seed", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--replace", action="store_true", help="wipe the demo client and seed it again"
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("DEMO_PASSWORD", DEFAULT_PASSWORD),
        help="for all three demo people (default: $DEMO_PASSWORD, else a fixed local one)",
    )
    args = parser.parse_args()
    with SessionLocal() as session:
        set_scope(session, Scope.everyone("demo.seed"))
        organisation = seed(session, replace=args.replace, password=args.password)
        rows = session.scalars(
            select(Document)
            .where(Document.organisation_id == organisation.id)
            .order_by(Document.uploaded_at.desc())
        ).all()
        counts: dict[str, int] = {}
        for document in rows:
            counts[document.status] = counts.get(document.status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    print(f"{NAME}: {len(rows)} documents ({summary})")
    print("Sign in to the portal as any of these, with the password", repr(args.password))
    for email, role in PEOPLE:
        print(f"  {email:<22} {role}")


if __name__ == "__main__":
    main()
