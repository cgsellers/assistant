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
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from doctypes import registry
from doctypes.definition import Definition
from doctypes.schema import build_json_schema
from evals import draft
from evals.labels import LabelledDocument, check_corpus, load_corpus
from evals.score import Comparison, DocumentScore, Outcome, Report, score_document
from llm.base import ExtractionError, Extractor
from ocr.base import OcrError, UnsupportedMediaType
from ocr.layout import extraction_text

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
    return Resolved(extraction_text(output.full_text, output.blocks), from_cache=False)


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
    include_unverified: bool = False,
    past_routing: bool = False,
) -> Report:
    """
    Score every labelled document in `directory`.

    Routing is exercised rather than bypassed: the label says which type the
    document *should* be, and the registry gets to disagree. A document
    routed to the wrong type is a routing failure, counted separately from
    getting its fields wrong, because they have different causes and
    different fixes.
    """
    prepared = prepare(
        session,
        directory,
        engine=engine,
        limit=limit,
        include_unverified=include_unverified,
    )
    return Report(
        documents=[_score_prepared(p, extractor, past_routing=past_routing) for p in prepared]
    )


@dataclass(frozen=True)
class Prepared:
    """
    One document, resolved and routed, ready for a model to look at.

    Preparation is separated from scoring so a comparison run can do it once
    and hand the *same* text and the *same* routing decision to every variant.
    Re-resolving per variant would let OCR nondeterminism show up as a
    difference between models, which is precisely the thing a comparison is
    supposed to rule out.
    """

    document: LabelledDocument
    expected: Definition
    text: str
    match: registry.RouteResult | None


def prepare(
    session: Session,
    directory: Path,
    *,
    engine=None,
    limit: int | None = None,
    include_unverified: bool = False,
) -> list[Prepared]:
    """
    Load a corpus, refuse it if it has problems, resolve and route every
    document. Does everything that costs nothing.
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

    if not include_unverified:
        drafts = [d.name for d in corpus if not d.label.verified]
        if drafts:
            raise CorpusError(
                "these labels are not verified, so scoring against them would be "
                "marking the model's own homework:\n  "
                + "\n  ".join(drafts)
                + "\n\nCheck each against its document, correct what is wrong, then "
                "set `verified: true`. Pass --include-unverified to score anyway."
            )

    prepared = []
    for document in corpus:
        text = resolve_text(document, engine).text
        prepared.append(
            Prepared(
                document=document,
                expected=definitions[document.label.doc_type],
                text=text,
                match=registry.route(session, text),
            )
        )
    return prepared


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


def _score_prepared(
    prepared: Prepared, extractor: Extractor, *, past_routing: bool = False
) -> DocumentScore:
    document, expected, match = prepared.document, prepared.expected, prepared.match
    routed = None if match is None else match.definition.name

    if routed != expected.name and not past_routing:
        # Routed somewhere else, or nowhere. By default the document is scored
        # as a routing failure with no fields: that is what the pipeline would
        # actually produce for it.
        log.warning("%s routed to %s, expected %s", document.name, routed, expected.name)
        return DocumentScore(
            name=document.name, expected_type=expected.name, actual_type=routed
        )

    # With past_routing, a miss is extracted against the type the label names,
    # so extraction is measured on every document. `actual_type` still records
    # what routing chose, so routing accuracy stays honest; the two numbers
    # just stop hiding each other.
    definition = match.definition if routed == expected.name else expected
    profile = match.profile if routed == expected.name else None
    schema = build_json_schema(definition, profile)
    try:
        result = extractor.extract(
            json_schema=schema, text=prepared.text, doc_type=definition.name
        )
    except ExtractionError as exc:
        # A failed call is not a scoring question; it is a gap in the run, and
        # pretending the model answered nothing would read as a perfect score
        # for every null field.
        raise CorpusError(f"{document.name}: extraction failed: {exc}") from exc

    scored = score_document(
        definition,
        document.label.expect,
        result.data,
        name=document.name,
        actual_type=routed,
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


def compare(
    session: Session,
    directory: Path,
    variants: list[tuple[str, Extractor]],
    *,
    engine=None,
    limit: int | None = None,
    include_unverified: bool = False,
    past_routing: bool = False,
) -> Comparison:
    """
    Score one corpus under two variants and diff them.

    Costs one model call per document *per variant*. The corpus is prepared
    once and shared, so the only thing that differs between the two runs is
    the thing being compared.
    """
    if len(variants) != 2:
        raise CorpusError(f"compare takes exactly two variants, got {len(variants)}")

    prepared = prepare(
        session,
        directory,
        engine=engine,
        limit=limit,
        include_unverified=include_unverified,
    )
    reports = tuple(
        Report(
            documents=[
                _score_prepared(p, extractor, past_routing=past_routing) for p in prepared
            ]
        )
        for _, extractor in variants
    )
    return Comparison(names=(variants[0][0], variants[1][0]), reports=reports)


# --- Reporting --------------------------------------------------------------


def _usd(value: Decimal) -> str:
    """
    Six decimal places, matching the extractions column.

    A per-document average is a division, so it arrives with Decimal's full
    precision -- $0.006449333333333333333333333333 is accurate and unreadable.
    """
    return f"${value.quantize(Decimal('0.000001'))}"


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
        lines.append(f"cost              {_usd(report.total_cost)} total")
        lines.append(f"                  {_usd(report.cost_per_document)} per document")
    latencies = [d.latency_ms for d in documents if d.latency_ms is not None]
    if latencies:
        lines.append(f"latency           {sum(latencies) // len(latencies)}ms mean")

    misrouted_but_scored = [d.name for d in documents if not d.routed_correctly and d.fields]
    if misrouted_but_scored:
        lines.append(
            f"                  ({len(misrouted_but_scored)} routing miss(es) extracted "
            "anyway with --past-routing; the pipeline itself would return nothing for them)"
        )

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
            lines.append(f"  {name}  {path} = {_show(value)}")

    # Every other failure, with both sides. A percentage says a field is weak;
    # only the values say why -- a misread, a different spelling, the wrong
    # line -- and those have different fixes.
    failing = [
        (d.name, f)
        for d in documents
        for f in d.fields
        if f.outcome in (Outcome.WRONG, Outcome.MISSED)
    ]
    if failing:
        lines.append("")
        lines.append("wrong or missed (expected -> got):")
        for name, f in failing:
            lines.append(f"  {name}  {f.path}: {_show(f.expected)} -> {_show(f.actual)}")

    return "\n".join(lines)


def _show(value) -> str:
    """Values as a reader wants them: amounts plainly, not as Decimal('7.15')."""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return repr(value)


def draft_labels(
    session: Session,
    directory: Path,
    *,
    extractor: Extractor,
    engine,
    limit: int | None = None,
    as_type: str | None = None,
) -> list[draft.Draft]:
    """
    Write a label file for every unlabelled document in a corpus.

    `as_type` drafts every document against that type even when routing
    disagrees. Routing is part of what the corpus measures, so a routing
    miss must not stop a document from being labelled -- otherwise the
    corpus would contain only the documents routing already gets right, and
    routing accuracy would read 100% by construction.

    Costs one OCR pass and one model call per document, and produces drafts
    rather than labels -- everything lands `verified: false` and the runner
    will not score it until a human says otherwise.
    """
    forced = None
    if as_type is not None:
        forced = {d.name: d for d in registry.active_definitions(session)}.get(as_type)
        if forced is None:
            raise CorpusError(f"no active document type called {as_type!r}")

    pending = draft.unlabelled(directory)
    if not pending:
        raise CorpusError(f"no unlabelled documents in {directory}")
    if limit is not None:
        pending = pending[:limit]

    drafted = []
    for path in pending:
        try:
            output = engine.read(path.read_bytes(), _mime_for(path))
        except UnsupportedMediaType as exc:
            # This file, not the run: skip it and say so.
            log.warning("%s: unreadable, skipped (%s)", path.name, exc)
            continue
        except OcrError as exc:
            # The engine itself is failing -- usually the container is not
            # running -- so every remaining document would fail the same way.
            raise CorpusError(
                f"OCR failed on {path.name}: {exc}\n"
                "If the OCR container is not running, start it with:\n"
                "  docker compose up -d ocr"
            ) from exc
        text = extraction_text(output.full_text, output.blocks)
        match = registry.route(session, text)
        routed = match.definition.name if match else None
        if forced is not None:
            definition = forced
            profile = match.profile if match and routed == forced.name else None
        elif match is not None:
            definition, profile = match.definition, match.profile
        else:
            # No type claims it and none was named, so there is no schema to
            # draft against. Saying so beats writing an empty file.
            log.warning(
                "%s: no document type matched, skipped (use --as TYPE to draft anyway)",
                path.name,
            )
            continue

        schema = build_json_schema(definition, profile)
        result = extractor.extract(json_schema=schema, text=text, doc_type=definition.name)
        document = draft.build(
            definition,
            extracted=result.data,
            ocr_text=text,
            source=path.name,
            model=result.model,
        )
        if routed != definition.name:
            # Worth keeping on the label: this is a routing miss the scorer
            # will count, and a person checking the draft should know why the
            # pipeline would never have extracted it.
            document["notes"] += (
                f" -- routing chose {routed or 'nothing'}; drafted as {definition.name}"
            )
        label_path = draft.write(path.with_suffix(".yaml"), document)
        log.info(
            "drafted %s (%s%s, $%s)",
            label_path.name,
            definition.name,
            "" if routed == definition.name else f", routing chose {routed or 'nothing'}",
            result.cost_usd,
        )
        drafted.append(
            draft.Draft(
                name=path.stem,
                path=label_path,
                doc_type=definition.name,
                cost_usd=result.cost_usd,
            )
        )
    return drafted


def format_comparison(comparison: Comparison) -> str:
    """
    A diff of two runs, regressions first.

    Regressions lead because they are the finding. An overall number that
    improved can still hide a field that broke, and the broken field is the
    one that needs a decision.
    """
    before_name, after_name = comparison.names
    before, after = comparison.reports
    lines = []

    if comparison.regressions:
        lines.append(f"REGRESSIONS ({len(comparison.regressions)})")
        for change in comparison.regressions:
            lines.append(
                f"  {change.document}  {change.path}: "
                f"{change.before.outcome} -> {change.after.outcome}"
                f"  (expected {change.before.expected!r}, got {change.after.actual!r})"
            )
        lines.append("")
    else:
        lines.append("no regressions")
        lines.append("")

    if comparison.improvements:
        lines.append(f"improvements ({len(comparison.improvements)})")
        for change in comparison.improvements:
            lines.append(
                f"  {change.document}  {change.path}: "
                f"{change.before.outcome} -> {change.after.outcome}"
            )
        lines.append("")

    deltas = comparison.field_deltas
    width = max((len(p) for p in deltas), default=5)
    header = (
        f"{'field'.ljust(width)}  {before_name[:9]:>9}  {after_name[:9]:>9}  delta"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for path, (was, now) in sorted(deltas.items(), key=lambda kv: kv[1][1] - kv[1][0]):
        move = now - was
        marker = "" if abs(move) < 1e-9 else f"{move:+.0%}"
        lines.append(f"{path.ljust(width)}  {was:>9.0%}  {now:>9.0%}  {marker}")
    lines.append("-" * len(header))
    lines.append("")

    lines.append(
        f"{'field accuracy'.ljust(width)}  {before.accuracy:>9.1%}  "
        f"{after.accuracy:>9.1%}  {comparison.accuracy_delta:+.1%}"
    )
    lines.append(
        f"{'routing'.ljust(width)}  {before.routing_accuracy:>9.1%}  "
        f"{after.routing_accuracy:>9.1%}"
    )
    lines.append(
        f"{'cost / doc'.ljust(width)}  {_usd(before.cost_per_document):>9}  "
        f"{_usd(after.cost_per_document):>9}"
    )
    ratio = comparison.cost_ratio
    if ratio is not None:
        lines.append("")
        lines.append(
            f"{after_name} cost {ratio:.1f}x more and moved field accuracy "
            f"{comparison.accuracy_delta:+.1%}"
        )
    return "\n".join(lines)


def report_as_dict(report: Report) -> dict:
    """Machine-readable, for tracking a number across runs."""
    return {
        "documents": len(report.documents),
        "field_accuracy": round(report.accuracy, 4),
        "routing_accuracy": round(report.routing_accuracy, 4),
        "total_cost_usd": str(report.total_cost),
        "cost_per_document_usd": str(
            report.cost_per_document.quantize(Decimal("0.000001"))
        ),
        "per_field": {
            path: {outcome.value: count for outcome, count in counts.items()}
            for path, counts in report.field_outcomes.items()
        },
    }


def comparison_as_dict(comparison: Comparison) -> dict:
    """Machine-readable, for asserting in CI that nothing regressed."""
    before, after = comparison.reports
    return {
        "variants": list(comparison.names),
        "field_accuracy": [round(before.accuracy, 4), round(after.accuracy, 4)],
        "accuracy_delta": round(comparison.accuracy_delta, 4),
        "cost_per_document_usd": [
            str(before.cost_per_document.quantize(Decimal("0.000001"))),
            str(after.cost_per_document.quantize(Decimal("0.000001"))),
        ],
        "regressions": [
            {
                "document": c.document,
                "field": c.path,
                "before": c.before.outcome.value,
                "after": c.after.outcome.value,
            }
            for c in comparison.regressions
        ],
        "improvements": [
            {"document": c.document, "field": c.path, "after": c.after.outcome.value}
            for c in comparison.improvements
        ],
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
    parser.add_argument(
        "--draft-labels",
        action="store_true",
        help="write draft labels for unlabelled documents, for you to correct",
    )
    parser.add_argument(
        "--as",
        dest="as_type",
        default=None,
        metavar="TYPE",
        help="with --draft-labels: draft against TYPE even where routing disagrees",
    )
    parser.add_argument(
        "--past-routing",
        action="store_true",
        help="also extract documents routing missed, so extraction is measured on all of them",
    )
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="score labels nobody has checked; the number means much less",
    )
    parser.add_argument(
        "--compare",
        metavar="MODEL",
        default=None,
        help="also run the corpus with MODEL and diff the two; doubles the cost",
    )
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

    if args.draft_labels:
        with SessionLocal() as session:
            drafts = draft_labels(
                session,
                directory,
                extractor=extractor,
                engine=engine or get_engine(None),
                limit=args.limit,
                as_type=args.as_type,
            )
        total = sum((d.cost_usd for d in drafts if d.cost_usd), start=Decimal("0"))
        log.info("")
        log.info("drafted %d label(s) for %s", len(drafts), total and f"${total}" or "free")
        log.info("Every one says `verified: false`. Check each against its document,")
        log.info("correct what is wrong, then set `verified: true`.")
        return

    with SessionLocal() as session:
        if args.compare:
            baseline = args.model or get_settings().extraction_model
            log.info(
                "comparing %s against %s -- two calls per document", baseline, args.compare
            )
            comparison = compare(
                session,
                directory,
                [(baseline, extractor), (args.compare, get_extractor(args.compare))],
                engine=engine,
                limit=args.limit,
                include_unverified=args.include_unverified,
                past_routing=args.past_routing,
            )
            print(
                json.dumps(comparison_as_dict(comparison), indent=2)
                if args.json
                else format_comparison(comparison)
            )
            return

        report = run_corpus(
            session,
            directory,
            extractor=extractor,
            engine=engine,
            limit=args.limit,
            include_unverified=args.include_unverified,
            past_routing=args.past_routing,
        )

    if args.json:
        print(json.dumps(report_as_dict(report), indent=2))
    else:
        print(format_report(report))


def _cli() -> None:
    """
    A problem with the corpus or the environment is reported as a sentence,
    not a traceback. Tracebacks are for bugs; "the OCR container is not
    running" is an instruction.
    """
    try:
        main()
    except CorpusError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    _cli()
