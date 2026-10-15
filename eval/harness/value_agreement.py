"""Reproduce agreement at human-reviewed dev packet slots, without another extraction.

A slot can be reassigned by review; this diagnostic does not establish physical-source
alignment, full statement coverage, mapping accuracy or a gate verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fra_ingest.config import REPO_ROOT
from harness.independent_gold import load_gold

CAUSES = frozenset(
    {"recognition_digits", "lost_dash", "trailing_mark", "merged_amounts", "row_association"}
)
DEFAULT_EVIDENCE = REPO_ROOT / "eval/golden/reviewed/juhayna-2024-en-consolidated.values.json"


def number(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("evidence numbers must be decimal strings or null")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid evidence decimal") from exc
    if not parsed.is_finite():
        raise ValueError("evidence numbers must be finite")
    return parsed


def load_evidence(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    reference = data.get("reference_file")
    if not isinstance(reference, str) or not reference or Path(reference).name != reference:
        raise ValueError("loaded evidence requires a pinned reference file")
    target = path.parent / reference
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != data.get("reference_sha256"):
        raise ValueError("reviewed reference hash differs from evidence")
    gold = load_gold(target)
    if (
        gold.document.id != data.get("document_id")
        or gold.document.sha256 != data.get("document_sha256")
        or gold.method != data.get("reference_method")
    ):
        raise ValueError("reviewed reference identity differs from evidence")
    expected = {
        (s.type.value, i, period): value
        for s in gold.document.statements
        for i, row in enumerate(s.rows)
        for period, value in row.values.items()
    }
    observed = {
        (cell["statement"], cell["row"], cell["period"]): number(cell["expected"])
        for cell in data["observations"]
    }
    if expected != observed:
        raise ValueError("evidence must cover every reviewed reference cell exactly")
    return data


def agreement(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("pool") != "dev" or data.get("reference_method") != "assisted_review":
        raise ValueError("this diagnostic accepts assisted dev evidence only")
    runs = data.get("run_count")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 2 or runs > 3:
        raise ValueError("run count must be two or three")
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("reviewed observations must not be empty")
    seen = set()
    for cell in observations:
        key = (cell["statement"], cell["row"], cell["period"])
        if key in seen:
            raise ValueError("duplicate reviewed slot")
        seen.add(key)
        if not isinstance(cell["reported"], list) or len(cell["reported"]) != runs:
            raise ValueError("observation run count differs from evidence")
        expected = number(cell["expected"])
        readings = [number(value) for value in cell["reported"]]
        if any(value != expected for value in readings) and cell.get("cause") not in CAUSES:
            raise ValueError("every mismatch needs a primary evidence class")
    if data.get("reference_cells", len(observations)) != len(observations):
        raise ValueError("evidence is missing reviewed reference slots")
    summaries = []
    for run in range(runs):
        right = 0
        groups: dict[str, dict[str, int | float]] = {}
        periods: dict[str, dict[str, int | float]] = {}
        causes: Counter[str] = Counter()
        mismatches = []
        for cell in observations:
            same = number(cell["reported"][run]) == number(cell["expected"])
            right += same
            for container, name in ((groups, cell["statement"]), (periods, cell["period"])):
                group = container.setdefault(name, {"right": 0, "cells": 0})
                group["right"] += same
                group["cells"] += 1
            if not same:
                causes[cell["cause"]] += 1
                mismatches.append(cell)
        for container in (groups, periods):
            for group in container.values():
                group["agreement"] = group["right"] / group["cells"]
        summaries.append(
            {
                "run": run + 1,
                "right": right,
                "cells": len(observations),
                "agreement": right / len(observations),
                "by_statement": groups,
                "by_period": periods,
                "causes": dict(causes),
                "mismatches": mismatches,
            }
        )
    repeated = data.get("repeat_cells", [])
    repeat_agree = 0
    repeat_seen = set()
    for cell in repeated:
        repeat_key = (cell["statement"], cell["label"], cell["occurrence"], cell["period"])
        if repeat_key in repeat_seen:
            raise ValueError("duplicate repeat-cell key")
        repeat_seen.add(repeat_key)
        if not isinstance(cell["present"], list) or any(
            type(v) is not bool for v in cell["present"]
        ):
            raise ValueError("repeat-cell presence must contain booleans")
        if len(cell["values"]) != runs or len(cell["present"]) != runs:
            raise ValueError("repeat-cell run count differs from evidence")
        values = [number(v) for v in cell["values"]]
        repeat_agree += all(cell["present"]) and len(set(values)) == 1
    return {
        "repeatability": {"observed_cells": len(repeated), "values_agree": repeat_agree},
        "document_id": data["document_id"],
        "pool": "dev",
        "method": "reviewed_packet_slots",
        "reference_method": "assisted_review",
        "gate_verdict": None,
        "limitations": "Slot association is supplied by review; no independent source alignment, "
        "complete-row recall, language generalization or gate acceptance is measured.",
        "runs": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)
    print(json.dumps(agreement(load_evidence(args.evidence)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
