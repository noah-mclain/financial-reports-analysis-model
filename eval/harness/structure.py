"""Score the structure stage over the golden set (spec 11, Scoring).

    uv run python eval/harness/structure.py

Per document: statements per enabled type, scale and currency against the manifest, identity
status and flags. Per language pair: numeric rows without a counterpart. Only the Almarai pair
is gated. Writes var/eval/structure-golden.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.structure import structure_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
PAIRS = (
    ("almarai-2025-en", "almarai-2025-ar", True),
    ("juhayna-2025-en-consolidated", "juhayna-2025-ar-consolidated", False),
    ("juhayna-2024-en-consolidated", "juhayna-2024-ar-consolidated", False),
    ("edita-2025-en-consolidated-eas", "edita-2025-ar-consolidated", False),
)
_FLAGGED = {"scale_missing", "scale_conflict", "currency_missing", "currency_conflict"}


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
    """A manifest scale of exactly "unconfirmed" is not scored; only the currency is."""
    scale_ok = expected_scale == "unconfirmed" or statement.scale == expected_scale
    if scale_ok and statement.currency == expected_currency:
        return True
    return bool(_FLAGGED & set(statement.flags))


def identity_excuses(
    statement: Statement, checks: list[CheckResult]
) -> list[dict[str, str]] | None:
    """The cells with ``numbers_missing`` on rows of each failed identity check; None when a
    failed check has no such cell, so its failure is not excused."""
    excuses: list[dict[str, str]] = []
    for check in checks:
        if check.kind != "balance_identity" or check.status != "fail":
            continue
        rows = set(check.line_item_ids)
        found = [
            {"item": item.id, "period": cell.period_key}
            for item in statement.line_items
            if item.id in rows
            for cell in item.cells
            if "numbers_missing" in cell.flags
        ]
        if not found:
            return None
        excuses.extend(found)
    return excuses


def _load_checks(path: Path) -> dict[str, list[CheckResult]]:
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
        checks = _load_checks(config.artifact_root / result.sha256 / "table_checks.json")
        found: dict[StatementType, Statement] = {}
        for s in result.statements:
            found.setdefault(s.type, s)
        by_id[entry["id"]] = found
        row: dict[str, Any] = {"id": entry["id"], "flags": result.flags, "statements": {}}
        for statement_type in config.enabled_types:
            s = found.get(statement_type)
            if s is None:
                row["statements"][statement_type.value] = None
                continue
            ok = metadata_ok(s, entry["scale"], str(entry["currency"]))
            identity = (
                "failed"
                if "identity_failed" in s.flags
                else "skipped"
                if "identity_totals_not_found" in s.flags
                else "ok"
            )
            row["statements"][statement_type.value] = {
                "pages": s.source_pages,
                "lines": len(s.line_items),
                "scale": s.scale,
                "currency": s.currency,
                "metadata_ok": ok,
                "identity": identity,
                "flags": s.flags,
            }
            if not ok:
                reasons.append(
                    f"{entry['id']} {statement_type.value}: scale/currency {s.scale} {s.currency} "
                    f"against {entry['scale']} {entry['currency']}"
                )
            if identity == "failed":
                excuses = identity_excuses(s, checks.get(s.id, []))
                row["statements"][statement_type.value]["identity_excused_by"] = excuses
                if excuses is None:
                    reasons.append(f"{entry['id']}: identity failed with every value present")
        rows.append(row)
        print(
            f"{entry['id']:34} "
            + "  ".join(
                f"{k} {'-' if v is None else str(v['lines']) + ' lines ' + v['identity']}"
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
                reasons.append(f"{left}/{right} {statement_type.value}: {misses}")
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
