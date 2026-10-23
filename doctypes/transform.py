"""
Running a document type's transform: Python written by a person, applied to
what the model read.

The model reads and code computes. A total the document never printed is
arithmetic, and arithmetic the model is told not to do; a transform is where
it happens instead, per document type, because what to derive depends on the
format.

Three things keep that from undermining the rest of the pipeline:

  * **It runs in its own interpreter.** `python -I -S`: no site-packages, no
    environment, a scratch working directory, a time limit, and on Linux a CPU
    and memory cap. The authors are trusted -- transforms are written by
    people, not generated -- so this is containment rather than a sandbox
    against hostile code: an infinite loop or a crash costs one document, not
    the worker.
  * **What it changes is recorded.** Every field whose value differs from what
    the model read is listed as derived, on the extraction and on the ledger
    row, so "read off the page" and "worked out" stay distinguishable.
  * **Its output is validated like the model's.** Coerced against the same
    field types and checked by the same rules, so a transform cannot produce
    something its own document type would reject.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from core import money
from doctypes.definition import Definition, Transform
from doctypes.validate import coerce, evaluate

#: Linux only; macOS does not enforce RLIMIT_AS. The timeout is the guard
#: everywhere.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

# Executed by the child interpreter. It cannot import the project, so it
# renders Decimal and date itself -- the same wire format the model answers in.
_RUNNER = r"""
import json, sys
from datetime import date, datetime
from decimal import Decimal

def _wire(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"transform returned a {type(value).__name__}, which is not JSON")

payload = json.load(sys.stdin)
out, sys.stdout = sys.stdout, sys.stderr  # a stray print() must not corrupt the result
namespace = {"__name__": "transform"}
exec(compile(payload["code"], "<transform>", "exec"), namespace)
result = namespace["transform"](payload["data"])
if not isinstance(result, dict):
    raise TypeError(f"transform must return a dict, got {type(result).__name__}")
out.write(json.dumps(result, default=_wire))
"""


#: Keys of definitions whose examples passed -- see check_examples.
_PASSED: set[str] = set()


class TransformError(Exception):
    """The transform raised, timed out, or returned something unusable."""


@dataclass(frozen=True)
class Processed:
    """An extraction after coercion, the transform, and the rules."""

    #: Typed values -- what the ledger is built from.
    data: dict
    #: Everything wrong: the model's output shape, the transform, the rules.
    errors: list[str]
    #: What the transform returned, as JSON. None if there is no transform or
    #: it failed, in which case `data` is what the model read.
    transformed: dict | None = None
    #: Top-level fields whose value is not what the model read.
    derived: list[str] = field(default_factory=list)


def process(definition: Definition, raw_json: Any) -> Processed:
    """
    Coerce, transform, validate. Replaces a bare `validate` wherever a
    document type may carry a transform.

    A transform that fails does not fail the job: the document goes to review
    with the reason, and the model's reading is kept, because a broken
    transform is a bug in the type and retrying will not fix it.
    """
    read, shape_errors = coerce(definition, raw_json)
    if definition.transform is None:
        return Processed(read, shape_errors + evaluate(definition, read))

    try:
        output = run(definition.transform, to_wire(read))
    except TransformError as exc:
        errors = shape_errors + evaluate(definition, read) + [f"transform failed: {exc}"]
        return Processed(read, errors)

    # Keys the definition does not declare are dropped here, exactly as they
    # are from the model's answer.
    data, output_errors = coerce(definition, output)
    derived = [name for name in definition.fields if data[name] != read[name]]
    errors = (
        shape_errors
        + [f"transform returned {e}" for e in output_errors]
        + evaluate(definition, data)
    )
    return Processed(data, errors, output, derived)


def run(transform: Transform, data: dict) -> dict:
    """The transform's output for one input, or TransformError."""
    payload = json.dumps({"code": transform.code, "data": data})
    with tempfile.TemporaryDirectory() as scratch:
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _RUNNER],
                input=payload,
                capture_output=True,
                text=True,
                timeout=transform.timeout_seconds,
                cwd=scratch,
                env={},
                preexec_fn=_limits(transform.timeout_seconds) if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired:
            raise TransformError(f"timed out after {transform.timeout_seconds:g}s") from None

    if completed.returncode != 0:
        raise TransformError(_explain(completed.stderr) or f"exited {completed.returncode}")
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TransformError(f"output is not JSON: {exc}") from None
    return output


def check_examples(definition: Definition) -> list[str]:
    """
    Run a transform against its own examples. Empty means they all pass.

    Inputs go through the same coercion as a real extraction, so an example
    exercises the transform exactly as a document would; expected values are
    compared after coercion, so `1120` and `"1120.00"` are the same amount.
    """
    transform = definition.transform
    if transform is None:
        return []
    # The same code, examples and field types give the same answer, so a pass
    # is remembered for the life of the process. Seeding a fresh database and
    # previewing a publish would otherwise start an interpreter per example
    # every time. Failures are not remembered: they are rare, and a person is
    # about to read them.
    key = definition.model_dump_json(include={"fields", "transform"})
    if key in _PASSED:
        return []

    failures = []
    for number, example in enumerate(transform.examples, start=1):
        read, _ = coerce(definition, example.input)
        try:
            output = run(transform, to_wire(read))
        except TransformError as exc:
            failures.append(f"example {number}: {exc}")
            continue
        got, _ = coerce(definition, output)
        wanted, _ = coerce(definition, example.expected)
        for name in example.expected:
            if name not in definition.fields:
                failures.append(f"example {number}: {name!r} is not a field")
            elif got[name] != wanted[name]:
                failures.append(
                    f"example {number}: {name} should be {_show(wanted[name])}, "
                    f"got {_show(got[name])}"
                )
    if not failures:
        _PASSED.add(key)
    return failures


def to_wire(value: Any) -> Any:
    """Typed values back to JSON: money as a string, dates as YYYY-MM-DD."""
    if isinstance(value, Decimal):
        return money.to_str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_wire(v) for v in value]
    return value


def _limits(timeout: float):
    def apply() -> None:
        import resource

        cpu = int(timeout) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        except (ValueError, OSError):
            pass  # not enforceable here (macOS); the timeout still applies

    return apply


def _explain(stderr: str) -> str:
    """The exception line, with the transform's line number when there is one."""
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    where = re.findall(r'File "<transform>", line (\d+)', stderr)
    return f"{lines[-1]} (line {where[-1]})" if where else lines[-1]


def _show(value: Any) -> str:
    return "null" if value is None else repr(to_wire(value))
