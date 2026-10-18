"""The reviewed role rule table, the assessments derived from it and their approval."""

import json
from pathlib import Path

import pytest
from dataset_contracts import DatasetError
from sec_roles import (
    RULES_PATH,
    approve_roles,
    draft_assessments,
    load_rules,
    role_for_sic,
    roles_header,
)
from test_sec_source_preparation import filing

REVIEWER = "role-reviewer-1"
AUTHORITY = "owner-sample-check"


@pytest.mark.parametrize(
    ("sic", "role"),
    [
        ("6000", "negative_control"),
        ("6021", "negative_control"),
        ("6499", "negative_control"),
        ("5999", "corporate"),
        ("6500", "corporate"),
        ("3714", "corporate"),
        ("0100", "corporate"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("abc", "unknown"),
        ("123", "unknown"),
        ("60210", "unknown"),
        ("60 1", "unknown"),
        ("６０２１", "unknown"),
    ],
)
def test_role_by_sic(sic: str, role: str) -> None:
    assert role_for_sic(load_rules(RULES_PATH), sic) == role


def test_the_rule_table_is_a_hashed_tracked_file() -> None:
    rules = load_rules(RULES_PATH)
    assert (rules.negative_control_min, rules.negative_control_max) == (6000, 6499)
    import hashlib

    assert rules.sha256 == hashlib.sha256(RULES_PATH.read_bytes()).hexdigest()


def test_a_malformed_rule_table_is_refused(tmp_path: Path) -> None:
    for text in (
        "version: 2\n",
        "version: 1\nnegative_control_sic: {min: 6500, max: 6000}\n",
        "[1]\n",
    ):
        path = tmp_path / "rules.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(DatasetError, match=r"rules\.yaml"):
            load_rules(path)
    with pytest.raises(DatasetError, match="absent"):
        load_rules(tmp_path / "absent.yaml")


def filings():  # type: ignore[no-untyped-def]
    return [
        filing(1, 11, "Alpha", sic="3714"),
        filing(2, 12, "Bank", sic="6021"),
        filing(3, 13, "Gamma", sic=""),
    ]


def test_one_assessment_per_filing_names_the_rule_table_and_the_filing() -> None:
    rules = load_rules(RULES_PATH)
    assessed = draft_assessments(filings(), rules)
    assert [(a.source_id, a.role) for a in assessed] == [
        (filings()[0].adsh, "corporate"),
        (filings()[1].adsh, "negative_control"),
        (filings()[2].adsh, "unknown"),
    ]
    for a in assessed:
        assert a.review.status == "draft"
        assert a.review.sha256 == rules.sha256
        assert rules.sha256 in a.review.reference and a.source_id in a.review.reference
    assert draft_assessments(list(reversed(filings())), rules) == assessed


def test_the_header_summarises_the_subjects() -> None:
    rules = load_rules(RULES_PATH)
    assessed = draft_assessments(filings(), rules)
    header = roles_header(rules, assessed)
    assert header["kind"] == "roles-subjects"
    assert header["rule_table_sha256"] == rules.sha256
    assert header["count"] == 3
    assert header["roles"] == {"corporate": 1, "negative_control": 1, "unknown": 1}
    assert roles_header(rules, assessed) == header


def verdicts(rules, assessed, *, header="default", verdict="approve", key="default"):  # type: ignore[no-untyped-def]
    head = roles_header(rules, assessed) if header == "default" else header
    row = {
        "key": f"rule-table:{rules.sha256}" if key == "default" else key,
        "verdict": verdict,
        "note": "ok",
    }
    lines = ([head] if head is not None else []) + [row]
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8")


def approve(assessed, data, rules=None, roster=None, **changes):  # type: ignore[no-untyped-def]
    rules = rules or load_rules(RULES_PATH)
    arguments = {
        "reviewer_id": REVIEWER,
        "authority_id": AUTHORITY,
        "evidence_reference": "roles-verdicts.jsonl",
        **changes,
    }
    return approve_roles(assessed, roster or filings(), rules, data, **arguments)


def test_approval_issues_one_record_per_assessment_under_the_one_verdict() -> None:
    import hashlib

    rules = load_rules(RULES_PATH)
    assessed = draft_assessments(filings(), rules)
    data = verdicts(rules, assessed)
    package = approve(assessed, data)
    assert [a.review.status for a in package.assessments] == ["reviewed"] * 3
    assert len(package.approvals) == 3
    assert {r.subject_kind for r in package.approvals} == {"role_assessment"}
    assert {r.subject_sha256 for r in package.approvals} == {
        a.content_sha256() for a in package.assessments
    }
    assert {r.status for r in package.approvals} == {"approved"}
    assert {r.reviewer_id for r in package.approvals} == {REVIEWER}
    assert {r.producer_id for r in package.approvals} == {assessed[0].review.producer_id}
    evidence = {r.evidence_sha256 for r in package.approvals}
    assert len(evidence) == 1
    assert evidence != {hashlib.sha256(data).hexdigest()}  # rule table plus assessment list


def test_every_assessment_is_recomputed_from_the_roster_and_any_mismatch_is_refused() -> None:
    rules = load_rules(RULES_PATH)
    assessed = list(draft_assessments(filings(), rules))
    data = verdicts(rules, assessed)
    flipped = assessed[0].model_copy(update={"role": "negative_control"})
    with pytest.raises(DatasetError, match="recomputed"):
        approve([flipped, *assessed[1:]], data)
    with pytest.raises(DatasetError, match="recomputed"):
        approve(assessed[:2], data)
    extra = assessed[0].model_copy(update={"source_id": "9999999999-24-999999"})
    with pytest.raises(DatasetError, match="recomputed"):
        approve([*assessed, extra], data)
    with pytest.raises(DatasetError, match="twice"):
        approve([*assessed, assessed[0]], data)
    other_roster = [*filings()[:2], filing(3, 13, "Gamma", sic="3714")]
    with pytest.raises(DatasetError, match="recomputed"):
        approve(assessed, data, roster=other_roster)


def test_the_verdict_is_on_the_rule_table_and_must_echo_the_header() -> None:
    rules = load_rules(RULES_PATH)
    assessed = list(draft_assessments(filings(), rules))
    good = roles_header(rules, assessed)
    for data, match in (
        (verdicts(rules, assessed, header=None), "header"),
        (verdicts(rules, assessed, header={**good, "count": 4}), "header"),
        (verdicts(rules, assessed, header={**good, "rule_table_sha256": "0" * 64}), "header"),
        (verdicts(rules, assessed, verdict="reject"), "reject"),
        (verdicts(rules, assessed, key="rule-table:" + "0" * 64), "rule table"),
        (b"", "verdict"),
    ):
        with pytest.raises(DatasetError, match=match):
            approve(assessed, data)


def test_only_drafts_from_the_rule_producer_and_a_distinct_reviewer_are_approved() -> None:
    rules = load_rules(RULES_PATH)
    assessed = list(draft_assessments(filings(), rules))
    data = verdicts(rules, assessed)
    reviewed = assessed[0].model_copy(
        update={"review": assessed[0].review.model_copy(update={"status": "reviewed"})}
    )
    with pytest.raises(DatasetError, match="recomputed"):
        approve([reviewed, *assessed[1:]], data)
    with pytest.raises(ValueError, match="independent"):
        approve(assessed, data, reviewer_id=assessed[0].review.producer_id)


def test_a_verdict_note_with_unicode_line_separators_is_accepted() -> None:
    rules = load_rules(RULES_PATH)
    assessed = draft_assessments(filings(), rules)
    row = {"key": f"rule-table:{rules.sha256}", "verdict": "approve", "note": "a\u2028b\u0085c"}
    data = (
        json.dumps(roles_header(rules, assessed)) + "\n" + json.dumps(row, ensure_ascii=False)
    ).encode()
    assert len(approve(assessed, data).approvals) == 3


def test_the_approval_evidence_binds_the_verdict_bytes() -> None:
    rules = load_rules(RULES_PATH)
    assessed = draft_assessments(filings(), rules)
    plain = verdicts(rules, assessed)
    noted = plain.replace(b'"note": "ok"', b'"note": "read it"')
    assert noted != plain
    first = {r.evidence_sha256 for r in approve(assessed, plain).approvals}
    second = {r.evidence_sha256 for r in approve(assessed, noted).approvals}
    assert len(first) == len(second) == 1 and first != second
    assert first == {r.evidence_sha256 for r in approve(assessed, plain).approvals}
