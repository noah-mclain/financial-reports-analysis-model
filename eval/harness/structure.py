"""Score the structure stage over the golden set (spec 11, Scoring).

    uv run python eval/harness/structure.py

Per document: statements per enabled type, scale and currency against the manifest, identity
status (from the balance identity checks in table_checks.json), review status and flags. A
failed identity is accepted only on a statement held for review, with a cell on the check's
rows that explains it (spec 12, Scoring). Per language pair: numeric rows without a
counterpart. Only the Almarai pair is gated. Writes
var/eval/structure-golden.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview
from fra_ingest.structure import structure_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
PAIRS = (
    ("almarai-2025-en", "almarai-2025-ar", True),
    ("juhayna-2025-en-consolidated", "juhayna-2025-ar-consolidated", False),
    ("juhayna-2024-en-consolidated", "juhayna-2024-ar-consolidated", False),
    ("edita-2025-en-consolidated-eas", "edita-2025-ar-consolidated", False),
)
_SCALE_FLAGS = {"scale_missing", "scale_conflict"}
_CURRENCY_FLAGS = {"currency_missing", "currency_conflict"}
_EXCUSE_FLAGS = ("numbers_missing", "digit_suspect")


def value_rows(statement: Statement) -> Counter[tuple[tuple[str, Decimal], ...]]:
    rows: Counter[tuple[tuple[str, Decimal], ...]] = Counter()
    for item in statement.line_items:
        values = tuple(
            sorted(
                (c.period_key, c.reported * statement.scale)
                for c in item.cells
                if c.reported is not None
            )
        )
        if values:
            rows[values] += 1
    return rows


def pair_misses(a: Statement, b: Statement) -> tuple[int, int]:
    left, right = value_rows(a), value_rows(b)
    return sum((left - right).values()), sum((right - left).values())


def metadata_ok(statement: Statement, expected_scale: object, expected_currency: str) -> bool:
    """Scale and currency are each right or flagged unsure by their own flags. A manifest scale
    of exactly "unconfirmed" is not scored."""
    flags = set(statement.flags)
    scale_ok = (
        expected_scale == "unconfirmed"
        or statement.scale == expected_scale
        or bool(_SCALE_FLAGS & flags)
    )
    currency_ok = statement.currency == expected_currency or bool(_CURRENCY_FLAGS & flags)
    return scale_ok and currency_ok


def identity_status(checks: list[CheckResult]) -> tuple[str, str]:
    """ "failed" when any balance identity check failed, "ok" when at least one passed and none
    failed, else "skipped" with the skipped checks' details ("not_checked" when there were none)."""
    identity = [c for c in checks if c.kind == "balance_identity"]
    if any(c.status == "fail" for c in identity):
        return "failed", ""
    if any(c.status == "pass" for c in identity):
        return "ok", ""
    details = sorted({c.detail for c in identity if c.detail})
    return "skipped", ",".join(details) or "not_checked"


def identity_excuses(
    statement: Statement, checks: list[CheckResult]
) -> list[dict[str, str]] | None:
    """The cells that explain each failed identity check: those on its rows, in its period, that
    carry ``numbers_missing`` or ``digit_suspect``. None when a failed check has no such cell, so
    nothing on its rows explains it."""
    excuses: list[dict[str, str]] = []
    for check in checks:
        if check.kind != "balance_identity" or check.status != "fail":
            continue
        rows = set(check.line_item_ids)
        found = [
            {"item": item.id, "period": cell.period_key, "flag": flag}
            for item in statement.line_items
            if item.id in rows
            for cell in item.cells
            if cell.period_key == check.period_key
            for flag in _EXCUSE_FLAGS
            if flag in cell.flags
        ]
        if not found:
            return None
        excuses.extend(found)
    return excuses


def identity_accepted(excuses: list[dict[str, str]] | None, review: StatementReview | None) -> bool:
    """A failed identity is accepted only when a cell on its rows explains it and the
    statement is held for review."""
    return excuses is not None and review is not None and review.status == "needs_review"


def _statement_summary(row: dict[str, Any]) -> str:
    held = "passed" if row["review"] == "passed" else "held"
    return f"{row['lines']} lines {row['identity']} {held}"


def first_statements(
    result: StructureResult, types: Iterable[StatementType]
) -> dict[StatementType, Statement]:
    """The first statement the stage found of each wanted type."""
    wanted = set(types)
    found: dict[StatementType, Statement] = {}
    for statement in result.statements:
        if statement.type in wanted:
            found.setdefault(statement.type, statement)
    return found


def load_checks(path: Path) -> dict[str, list[CheckResult]]:
    by_statement: dict[str, list[CheckResult]] = {}
    for raw in json.loads(path.read_text(encoding="utf-8")):
        check = CheckResult.model_validate(raw)
        by_statement.setdefault(check.statement_id, []).append(check)
    return by_statement


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="eval/harness/structure.py").parse_args(argv)
    config = load_config()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[StatementType, Statement]] = {}
    reasons: list[str] = []
    for entry in documents:
        pdf = MANIFEST.parent / entry["file"]
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = structure_pdf(pdf, config, None, use_cache=False)
        except IngestError as exc:
            rows.append({"id": entry["id"], "error": f"{exc.reason} {exc.detail}".strip()})
            reasons.append(f"{entry['id']}: {exc.reason}")
            continue
        checks = load_checks(config.artifact_root / result.sha256 / "table_checks.json")
        reviews = {r.statement_id: r for r in result.reviews}
        found = first_statements(result, config.enabled_types)
        by_id[entry["id"]] = found
        row: dict[str, Any] = {"id": entry["id"], "flags": result.flags, "statements": {}}
        for statement_type in config.enabled_types:
            s = found.get(statement_type)
            if s is None:
                row["statements"][statement_type.value] = None
                continue
            ok = metadata_ok(s, entry["scale"], str(entry["currency"]))
            review = reviews.get(s.id)
            identity, identity_detail = (
                identity_status(checks.get(s.id, []))
                if s.type is StatementType.BALANCE
                else ("n/a", "")
            )
            row["statements"][statement_type.value] = {
                "pages": s.source_pages,
                "lines": len(s.line_items),
                "scale": s.scale,
                "currency": s.currency,
                "metadata_ok": ok,
                "identity": identity,
                "identity_detail": identity_detail,
                "flags": s.flags,
                "review": review.status if review else None,
                "review_reasons": review.reasons if review else [],
                "checked_cells": review.checked_cells if review else 0,
                "numeric_cells": review.numeric_cells if review else 0,
            }
            if not ok:
                reasons.append(
                    f"{entry['id']} {statement_type.value}: scale/currency {s.scale} {s.currency} "
                    f"against {entry['scale']} {entry['currency']}"
                )
            if identity == "failed":
                excuses = identity_excuses(s, checks.get(s.id, []))
                row["statements"][statement_type.value]["identity_excused_by"] = excuses
                if not identity_accepted(excuses, review):
                    reasons.append(
                        f"{entry['id']}: identity failed and nothing on its rows explains it"
                    )
        rows.append(row)
        print(
            f"{entry['id']:34} "
            + "  ".join(
                f"{k} {'-' if v is None else _statement_summary(v)}"
                for k, v in row["statements"].items()
            )
        )

    pairs = []
    for left, right, gated in PAIRS:
        for statement_type in config.enabled_types:
            a, b = by_id.get(left, {}).get(statement_type), by_id.get(right, {}).get(statement_type)
            misses = pair_misses(a, b) if a and b else None
            pairs.append(
                {
                    "pair": f"{left}/{right}",
                    "type": statement_type.value,
                    "gated": gated,
                    "misses": misses,
                }
            )
            print(
                f"pair {left:32} {right:32} {statement_type.value:22} "
                f"{misses if misses is not None else 'missing statement'}"
            )
            if gated and misses != (0, 0):
                found_text = "missing statement" if misses is None else str(misses)
                reasons.append(f"{left}/{right} {statement_type.value}: {found_text}")
    print("PASS" if not reasons else "FAIL\n  " + "\n  ".join(reasons))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "structure-golden.json").write_text(
        json.dumps(
            {"documents": rows, "pairs": pairs, "reasons": reasons},
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
