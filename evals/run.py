"""
Running a corpus and reporting what came out.

Every run spends real money -- one model call per labelled document -- so the
corpus is validated first and the cost is printed at the end rather than
buried. `--dry-run` does everything except the calls.

Extractions from a run are deliberately **not** written to `extractions`.
Nothing here is a real document: there is no `documents` row to hang them
off, and a corpus re-run every time a description changes would swamp the
table it is meant to measure. `is_trial` exists for previewing a draft type
against a *live* document, which is a different thing.

Run it with:  uv run python -m evals.run
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from doctypes import registry
from doctypes.definition import Definition
from doctypes.schema import build_json_schema
from evals.labels import LabelledDocument, check_corpus, load_corpus
from evals.score import DocumentScore, Outcome, Report, score_document
from llm.base import ExtractionError, Extractor

log = logging.getLogger("evals")

EXAMPLES_DIR = Path(__file__).parent / "examples"


class CorpusError(Exception):
    """The corpus cannot be scored against, so the run should not start."""


@dataclass(frozen=True)
class Resolved:
    """One document's text, and how it was obtained."""

    text: str
    from_cache: bool


def resolve_text(document: LabelledDocument, engine=None) -> Resolved:
    """
    The OCR text for a labelled document.

    Cached text in the label is preferred: it makes a run reproducible and
    removes the container from the loop, which is what you want while tuning
    descriptions. Passing an engine measures OCR and extraction together,
    which is what you want when comparing engines.
    """
    if document.label.ocr_text and engine is None:
        return Resolved(document.label.ocr_text, from_cache=True)

    path = document.document_path()
    if path is None:
        if document.label.ocr_text:
            return Resolved(document.label.ocr_text, from_cache=True)
        raise CorpusError(f"{document.name}: no cached ocr_text and no document on disk")
    if engine is None:
        raise CorpusError(
            f"{document.name}: has no cached ocr_text, so an OCR engine is required"
        )

    output = engine.read(path.read_bytes(), _mime_for(path))
    return Resolved(output.full_text, from_cache=False)


def _mime_for(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".pdf": "application/pdf",
    }.get(path.suffix.lower(), "application/octet-stream")


def run_corpus(
    session: Session,
    directory: Path,
    *,
    extractor: Extractor,
    engine=None,
    limit: int | None = None,
) -> Report:
    """
    Score every labelled document in `directory`.

    Routing is exercised rather than bypassed: the label says which type the
    document *should* be, and the registry gets to disagree. A document
    routed to the wrong type is a routing failure, counted separately from
    getting its fields wrong, because they have different causes and
    different fixes.
    """
    corpus = load_corpus(directory)
    if not corpus:
        raise CorpusError(f"no labels found in {directory}")
    if limit is not None:
        corpus = corpus[:limit]

    definitions = _definitions_for(session, corpus)
    problems = [
        p
        for document in corpus
        for p in check_corpus([document], definitions[document.label.doc_type])
    ]
    if problems:
        raise CorpusError(
            "the corpus has problems, so nothing was run:\n  " + "\n  ".join(problems)
        )

    scores = [
        _score_one(session, document, definitions[document.label.doc_type], extractor, engine)
        for document in corpus
    ]
    return Report(documents=scores)


def _definitions_for(
    session: Session, corpus: list[LabelledDocument]
) -> dict[str, Definition]:
    """Every document type the corpus mentions, or a readable failure."""
    wanted = {d.label.doc_type for d in corpus}
    active = {d.name: d for d in registry.active_definitions(session)}
    missing = sorted(wanted - set(active))
    if missing:
        raise CorpusError(
            f"no active document type for {missing}; run `python -m doctypes.seed`"
        )
    return {name: active[name] for name in wanted}


def _score_one(
    session: Session,
    document: LabelledDocument,
    definition: Definition,
    extractor: Extractor,
    engine,
) -> DocumentScore:
    text = resolve_text(document, engine).text
    match = registry.route(session, text)

    if match is None or match.definition.name != definition.name:
        # Routed somewhere else, or nowhere. Extracting against the type the
        # label names would measure a pipeline that does not exist, so the
        # document is scored as a routing failure with no fields.
        actual = None if match is None else match.definition.name
        log.warning("%s routed to %s, expected %s", document.name, actual, definition.name)
        return DocumentScore(
            name=document.name,
            expected_type=definition.name,
            actual_type=actual,
        )

    schema = build_json_schema(match.definition, match.profile)
    try:
        result = extractor.extract(
            json_schema=schema, text=text, doc_type=match.definition.name
        )
    except ExtractionError as exc:
        # A failed call is not a scoring question; it is a gap in the run, and
        # pretending the model answered nothing would read as a perfect score
        # for every null field.
        raise CorpusError(f"{document.name}: extraction failed: {exc}") from exc

    scored = score_document(
        match.definition,
        document.label.expect,
        result.data,
        name=document.name,
        actual_type=match.definition.name,
    )
    return DocumentScore(
        name=scored.name,
        expected_type=scored.expected_type,
        actual_type=scored.actual_type,
        fields=scored.fields,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


# --- Reporting --------------------------------------------------------------


def format_report(report: Report) -> str:
    """
    A report meant to be read in a terminal and pasted into a commit message.

    Per field rather than per document: "87% of documents were perfect" hides
    which field is costing the 13%.
    """
    lines = []
    documents = report.documents
    lines.append(f"{len(documents)} document(s)")
    lines.append("")

    width = max((len(p) for p in report.field_outcomes), default=5)
    header = f"{'field'.ljust(width)}  {'acc':>6}  correct  absent  missed  halluc  wrong"
    lines.append(header)
    lines.append("-" * len(header))

    for path, counts in sorted(
        report.field_outcomes.items(), key=lambda kv: report.field_accuracy[kv[0]]
    ):
        lines.append(
            f"{path.ljust(width)}  {report.field_accuracy[path]:>6.0%}  "
            f"{counts[Outcome.CORRECT]:>7}  {counts[Outcome.ABSENT_OK]:>6}  "
            f"{counts[Outcome.MISSED]:>6}  {counts[Outcome.HALLUCINATED]:>6}  "
            f"{counts[Outcome.WRONG]:>5}"
        )

    lines.append("-" * len(header))
    lines.append("")
    lines.append(f"field accuracy    {report.accuracy:.1%}")
    lines.append(f"routing accuracy  {report.routing_accuracy:.1%}")

    priced = [d for d in documents if d.cost_usd is not None]
    if priced:
        lines.append(f"cost              ${report.total_cost} total")
        lines.append(f"                  ${report.cost_per_document} per document")
    latencies = [d.latency_ms for d in documents if d.latency_ms is not None]
    if latencies:
        lines.append(f"latency           {sum(latencies) // len(latencies)}ms mean")

    hallucinated = [
        (d.name, f.path, f.actual)
        for d in documents
        for f in d.fields
        if f.outcome is Outcome.HALLUCINATED
    ]
    if hallucinated:
        lines.append("")
        lines.append("invented values (the document said nothing):")
        for name, path, value in hallucinated:
            lines.append(f"  {name}  {path} = {value!r}")

    return "\n".join(lines)


def report_as_dict(report: Report) -> dict:
    """Machine-readable, for tracking a number across runs."""
    return {
        "documents": len(report.documents),
        "field_accuracy": round(report.accuracy, 4),
        "routing_accuracy": round(report.routing_accuracy, 4),
        "total_cost_usd": str(report.total_cost),
        "cost_per_document_usd": str(report.cost_per_document),
        "per_field": {
            path: {outcome.value: count for outcome, count in counts.items()}
            for path, counts in report.field_outcomes.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a labelled corpus")
    parser.add_argument(
        "--dir", type=Path, default=None, help="corpus directory (default: from settings)"
    )
    parser.add_argument("--examples", action="store_true", help="use the bundled examples")
    parser.add_argument("--limit", type=int, default=None, help="only the first N documents")
    parser.add_argument("--model", default=None, help="override the extraction model")
    parser.add_argument(
        "--ocr", default=None, help="OCR the source files with this engine instead of cached text"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate the corpus, call nothing"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from core.config import get_settings
    from core.db import SessionLocal

    directory = (
        EXAMPLES_DIR if args.examples else (args.dir or get_settings().eval_corpus_dir)
    )

    if args.dry_run:
        corpus = load_corpus(directory)
        with SessionLocal() as session:
            definitions = _definitions_for(session, corpus)
            problems = [
                p
                for d in corpus
                for p in check_corpus([d], definitions[d.label.doc_type])
            ]
        if problems:
            raise SystemExit("corpus problems:\n  " + "\n  ".join(problems))
        log.info("%d label(s) in %s, all usable", len(corpus), directory)
        return

    from llm import get_extractor
    from ocr import get_engine

    extractor = get_extractor(args.model)
    engine = get_engine(args.ocr) if args.ocr else None

    with SessionLocal() as session:
        report = run_corpus(
            session, directory, extractor=extractor, engine=engine, limit=args.limit
        )

    if args.json:
        print(json.dumps(report_as_dict(report), indent=2))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
