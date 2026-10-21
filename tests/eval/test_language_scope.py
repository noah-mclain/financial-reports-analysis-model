"""Whole-pool manifest language decides gates, including unreadable inputs."""

from typing import Any

import pytest
from harness.gates import gate_report
from test_gates import doc


def failure(language: str) -> dict[str, str]:
    return {
        "id": "unreadable",
        "language": language,
        "reason": "unreadable_pdf",
        "detail": "bad bytes",
    }


def total(documents: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    return gate_report(documents, [], errors)["strata"]["total"]


def test_arabic_unreadable_does_not_change_any_english_verdict() -> None:
    fig = {"status": "checked", "page_mode": "digital", "cells": 200, "right": 200}
    english = doc("english", "annual", figures=[fig])
    baseline = total([english], [])
    actual = total([english], [failure("ar")])
    assert actual["gate_a"] == baseline["gate_a"]
    assert actual["gate_b"] == baseline["gate_b"]
    assert actual["reported_languages"]["ar"]["unreadable"] == [failure("ar")]
    assert actual["reported_languages"]["en"]["unreadable"] == []


def test_english_unreadable_fails_measured_english_and_leaves_arabic_report_unchanged() -> None:
    english = doc("english", "annual")
    arabic = {**doc("arabic", "annual"), "language": "ar"}
    baseline = total([english, arabic], [])
    actual = total([english, arabic], [failure("en")])
    assert actual["gate_a"]["identity"]["met"] is False
    assert actual["gate_a"]["scale_currency"]["met"] is False
    assert actual["gate_b"]["met"] is False
    assert actual["reported_languages"]["ar"] == baseline["reported_languages"]["ar"]
    assert actual["reported_languages"]["en"]["unreadable"] == [failure("en")]


@pytest.mark.parametrize("documents", [[], [{**doc("arabic", "annual"), "language": "ar"}]])
@pytest.mark.parametrize("errors", [[], [failure("ar")], [failure("en")]])
def test_no_measured_english_is_not_measured_even_with_unreadables(
    documents: list[dict[str, Any]], errors: list[dict[str, str]]
) -> None:
    actual = total(documents, errors)
    assert actual["gate_a"]["identity"]["met"] is None
    assert actual["gate_a"]["scale_currency"]["met"] is None
    assert actual["gate_a"]["pairs"]["met"] is None
    assert actual["gate_b"]["met"] is None
    assert actual["unreadable"] == errors


@pytest.mark.parametrize(
    "bad",
    [
        "unreadable: no language",
        {"id": "bad", "reason": "unreadable_pdf"},
        {**failure("en"), "language": "unknown"},
        {**failure("en"), "id": "english"},
    ],
)
def test_missing_or_inconsistent_manifest_attribution_fails_loudly(bad: Any) -> None:
    with pytest.raises(ValueError, match=r"language|document|attribution"):
        total([doc("english", "annual")], [bad])


def test_arabic_failures_are_reported_without_gating_identity_scale_or_mapping() -> None:
    arabic = {
        **doc(
            "arabic",
            "annual",
            identity="failed_unexplained",
            scale=(3, 1, 0),
            mapping={"statement_not_found": 8},
        ),
        "language": "ar",
    }
    actual = total([doc("english", "annual"), arabic], [])
    assert actual["gate_a"]["identity"]["met"] is True
    assert actual["gate_a"]["scale_currency"]["met"] is True
    assert actual["gate_b"]["met"] is True
    reported = actual["reported_languages"]["ar"]
    assert reported["gate_a"]["identity"] == {"counts": {"failed_unexplained": 1}, "met": None}
    assert reported["gate_a"]["scale_currency"]["found"] == 1
    assert reported["gate_a"]["scale_currency"]["met"] is None
    assert reported["gate_b"]["statement_not_found"] == 8
    assert reported["gate_b"]["met"] is None


def test_empty_english_does_not_inherit_a_pair_verdict() -> None:
    pair = {
        "pair": "a/b",
        "type": "balance",
        "gated": True,
        "misses": [0, 0],
        "period_kind": "annual",
    }
    report = gate_report([], [pair], [failure("en")])
    assert report["strata"]["total"]["gate_a"]["pairs"]["met"] is None


def test_all_default_language_pairs_are_report_only_with_measured_english() -> None:
    from harness.gates import pair_records

    pairs = pair_records({}, {})
    assert pairs and all(not pair["gated"] for pair in pairs)
    block = gate_report([doc("english", "annual")], pairs)["strata"]["total"]["gate_a"]["pairs"]
    assert block == {"gated": 0, "identical": 0, "ungated": len(pairs), "met": None}
