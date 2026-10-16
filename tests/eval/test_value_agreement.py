"""Value-only agreement is a reviewed-slot diagnostic, never a source-accuracy gate."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from harness.value_agreement import agreement, load_evidence


def evidence() -> dict[str, Any]:
    return {
        "document_id": "synthetic",
        "document_sha256": "a" * 64,
        "pool": "dev",
        "reference_method": "assisted_review",
        "run_count": 2,
        "observations": [
            {
                "statement": "balance",
                "row": 0,
                "period": "2024",
                "expected": "5",
                "reported": ["5", "6"],
                "cause": "recognition_digits",
                "page": 1,
            },
            {
                "statement": "balance",
                "row": 1,
                "period": "2024",
                "expected": "0",
                "reported": [None, None],
                "cause": "lost_dash",
                "page": 1,
            },
            {
                "statement": "income",
                "row": 0,
                "period": "FY2024",
                "expected": "2",
                "reported": ["2", "2"],
                "cause": None,
                "page": 2,
            },
        ],
    }


def test_failures_and_missing_values_stay_in_denominator() -> None:
    report = agreement(evidence())
    assert report["runs"][0]["right"] == 2
    assert report["runs"][0]["cells"] == 3
    assert report["runs"][1]["right"] == 1
    assert report["runs"][1]["causes"] == {"recognition_digits": 1, "lost_dash": 1}
    assert report["gate_verdict"] is None
    assert report["runs"][0]["by_statement"]["income"]["right"] == 1


def test_evidence_cannot_drop_or_duplicate_slots() -> None:
    data = evidence()
    data["observations"].append(deepcopy(data["observations"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        agreement(data)
    data = evidence()
    data["observations"][0]["reported"] = ["5"]
    with pytest.raises(ValueError, match="run count"):
        agreement(data)


@pytest.mark.parametrize("change", ["pool", "nonfinite", "reason"])
def test_invalid_or_unclassified_inputs_fail_loudly(change: str) -> None:
    data = evidence()
    if change == "pool":
        data["pool"] = "blind"
    elif change == "nonfinite":
        data["observations"][0]["expected"] = "NaN"
    else:
        data["observations"][1]["cause"] = None
    with pytest.raises(ValueError):
        agreement(data)


def test_public_dev_evidence_reproduces_reviewed_result() -> None:
    root = Path(__file__).resolve().parents[2]
    report = agreement(
        load_evidence(root / "eval/golden/reviewed/juhayna-2024-en-consolidated.values.json")
    )
    assert [(r["right"], r["cells"]) for r in report["runs"]] == [(97, 124), (97, 124)]
    assert report["repeatability"] == {"observed_cells": 140, "values_agree": 140}
    assert report["runs"][0]["causes"] == {
        "recognition_digits": 16,
        "lost_dash": 4,
        "trailing_mark": 1,
        "merged_amounts": 2,
        "row_association": 4,
    }
    assert report["runs"][0]["by_statement"]["income"] == {
        "right": 36,
        "cells": 36,
        "agreement": 1.0,
    }


def test_public_reference_is_explicitly_a_packet_subset() -> None:
    from harness.independent_gold import load_gold

    root = Path(__file__).resolve().parents[2]
    reference = load_gold(root / "eval/golden/reviewed/juhayna-2024-en-consolidated.gold.json")
    assert reference.reference_scope == "reviewed_packet_slots"


@pytest.mark.parametrize("missing", [True, False])
def test_loaded_evidence_requires_reference_metadata(tmp_path: Path, missing: bool) -> None:
    data = evidence()
    if not missing:
        data["reference_file"] = None
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="reference"):
        load_evidence(path)


def test_loaded_evidence_cannot_drop_all_mismatches(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[2] / "eval/golden/reviewed"
    source = root / "juhayna-2024-en-consolidated.values.json"
    data = json.loads(source.read_text())
    shutil.copyfile(root / data["reference_file"], tmp_path / data["reference_file"])
    data["observations"] = [c for c in data["observations"] if c["cause"] is None]
    data.pop("reference_cells")
    path = tmp_path / "dropped.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cover every"):
        load_evidence(path)


@pytest.mark.parametrize("corruption", ["duplicate", "presence"])
def test_repeatability_refuses_duplicates_and_nonboolean_presence(corruption: str) -> None:
    data = evidence()
    data["repeat_cells"] = [
        {
            "statement": "balance",
            "label": "Assets",
            "period": "2024",
            "occurrence": 0,
            "values": ["5", "5"],
            "present": [True, True],
        }
    ]
    if corruption == "duplicate":
        data["repeat_cells"].append(deepcopy(data["repeat_cells"][0]))
    else:
        data["repeat_cells"][0]["present"] = ["false", "false"]
    with pytest.raises(ValueError):
        agreement(data)
