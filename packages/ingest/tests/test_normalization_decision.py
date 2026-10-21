"""Construction tests for the evidence boundary; no document quality claim."""

import json

import pytest
from pydantic import ValidationError
from test_label_mapping import INDEX, TAXONOMY, check, liabilities, passing, row, statement
from test_normalization_context import section

from fra_core.schemas import CheckResult, Statement, StatementType
from fra_core.schemas.normalization import NormalizationDecision
from fra_ingest.normalization_decision import assess_proposal, baseline_decision


def assess(
    source: Statement, target: str, cid: str | None, checks: tuple[CheckResult, ...] = ()
) -> NormalizationDecision:
    return assess_proposal(source, target, TAXONOMY, checks, json.dumps({"canonical_id": cid}))


@pytest.mark.parametrize(
    "label,cid",
    [
        ("Revenue", "revenue"),
        ("Basic earnings per share", "eps_basic"),
        ("Diluted earnings per share", "eps_diluted"),
    ],
)
def test_exact_alias_and_matching_proposal(label: str, cid: str) -> None:
    source = statement([row(1, label, "1")], StatementType.INCOME)
    original = source.model_dump_json()
    assert baseline_decision(source, "r1", INDEX, ()).canonical_id == cid
    decision = assess(source, "r1", cid)
    assert decision.state == "supported" and decision.canonical_id == cid
    assert decision.audit.rows[0].label_region == "unavailable"
    assert source.model_dump_json() == original


@pytest.mark.parametrize(
    "label,cid",
    [
        ("Basic earnings per share", "eps_basic"),
        ("Diluted earnings per share", "eps_diluted"),
    ],
)
def test_exact_eps_alias_allows_known_per_share_unit_note(label: str, cid: str) -> None:
    source = statement([row(1, label, "1")], StatementType.INCOME)
    source.line_items[0].cells[0].flags.append("per_share")

    decision = assess(source, "r1", cid)

    assert decision.state == "supported" and decision.canonical_id == cid


def test_revenue_is_not_held_by_a_per_share_peer() -> None:
    source = statement(
        [row(1, "Revenue", "1"), row(2, "Basic earnings per share", "0.5")],
        StatementType.INCOME,
    )
    source.line_items[1].cells[0].flags.append("per_share")

    decision = assess(source, "r1", "revenue")

    assert decision.state == "supported" and decision.canonical_id == "revenue"


@pytest.mark.parametrize("flag", ["row_misaligned", "per_share:unverified"])
def test_unknown_or_uncertain_flags_stay_held_with_per_share(flag: str) -> None:
    source = statement([row(1, "Basic earnings per share", "1")], StatementType.INCOME)
    source.line_items[0].cells[0].flags.extend(["per_share", flag])

    decision = assess(source, "r1", "eps_basic")

    assert decision.state == "unresolved" and decision.canonical_id is None
    assert decision.reason == "source_cell_uncertainty"
    assert decision.needed_evidence == (
        f"Resolve source cell r1:2025-12-31 flags: per_share,{flag}",
    )


def test_checked_sections_and_contradictory_proposal() -> None:
    items = liabilities()
    checks = tuple(passing(items))
    source = statement(items)
    assert assess(source, "r3", "lease_liabilities_non_current", checks).state == "supported"
    assert assess(source, "r8", "lease_liabilities_current", checks).state == "supported"
    rejected = assess(source, "r3", "lease_liabilities_current", checks)
    assert rejected.state == "rejected" and rejected.canonical_id is None
    assert rejected.reason == "proposal_disagrees"
    assert rejected.supplied_anchor_check_ids == tuple(c.id for c in checks)


@pytest.mark.parametrize("case", ["generic_eps", "unlinked", "opposing", "blank_parent", "prefix"])
def test_original_missing_or_conflicting_controls_stay_negative(case: str) -> None:
    if case == "generic_eps":
        source = statement(
            [
                row(1, "Earnings per share", None),
                row(2, "Basic", "1", parent=1),
                row(3, "Earnings per share", "1", parent=1),
                row(4, "Diluted", "1", parent=1),
            ],
            StatementType.INCOME,
        )
        target, cid = "r3", "eps_basic"
    elif case == "prefix":
        source = statement([row(1, "Property, plant and equipment, net", "1")])
        target, cid = "r1", "ppe_net"
    elif case == "blank_parent":
        source = statement(liabilities())
        target, cid = "r3", "lease_liabilities_non_current"
    else:
        source = section()
        target, cid = "r2", "lease_liabilities_current"
        if case == "unlinked":
            source.line_items[1].parent_id = None
        else:
            source.line_items.extend(
                [
                    row(4, "Non-current liabilities", None),
                    row(5, "Lease liabilities", "1", parent=1),
                ]
            )
            target = "r5"
    # A prior answer, source label and maximum confidence do not supply evidence.
    source.line_items[
        next(n for n, r in enumerate(source.line_items) if r.id == target)
    ].canonical_id = cid
    source.line_items[
        next(n for n, r in enumerate(source.line_items) if r.id == target)
    ].mapping_confidence = 1.0
    decision = assess(source, target, cid)
    assert decision.state == "unresolved"
    assert decision.canonical_id is None and decision.needed_evidence


@pytest.mark.parametrize("status", ["fail", "skipped", "missing"])
def test_absent_or_unconfirmed_checks_cannot_anchor(status: str) -> None:
    items = liabilities()
    checks = () if status == "missing" else (check("subtotal", ["r2", "r3", "r4"], status),)
    assert (
        assess(statement(items), "r3", "lease_liabilities_non_current", checks).state
        == "unresolved"
    )


@pytest.mark.parametrize("change", ["statement", "period", "row", "unscoped"])
def test_foreign_anchor_scope_fails_loudly(change: str) -> None:
    anchor = check("subtotal", ["r2", "r3", "r4"])
    updates: dict[str, dict[str, object]] = {
        "statement": {"statement_id": "foreign"},
        "period": {"period_key": "foreign"},
        "row": {"line_item_ids": ["foreign", "r4"]},
        "unscoped": {"statement_id": ""},
    }
    update = updates[change]
    with pytest.raises(ValueError, match="check"):
        baseline_decision(
            statement(liabilities()), "r3", INDEX, (anchor.model_copy(update=update),)
        )


def test_non_anchor_note_references_are_not_mapping_proof() -> None:
    source = statement(liabilities())
    checks = (
        check("eps", ["outside_note", "r4"]),
        check("net_profit_tie", ["other_statement", "r4"]),
    )
    assert assess(source, "r3", "lease_liabilities_non_current", checks).state == "unresolved"


@pytest.mark.parametrize(
    "response,state",
    [
        ('{"canonical_id":null}', "model_abstention"),
        ("null", "invalid"),
        ("{}", "invalid"),
        ('{"canonical_id":"total_assets"}', "invalid"),
        ('{"canonical_id":"bogus"}', "invalid"),
        ('{"canonical_id":null,"canonical_id":null}', "invalid"),
    ],
)
def test_semantic_null_is_separate_from_invalid(response: str, state: str) -> None:
    result = assess_proposal(
        statement([row(1, "Revenue", "1")], StatementType.INCOME), "r1", TAXONOMY, (), response
    )
    assert result.state == state and result.canonical_id is None


@pytest.mark.parametrize("case", ["duplicate", "parent", "target", "cycle", "unrelated_cycle"])
def test_invalid_graphs_terminate(case: str) -> None:
    source = section()
    target = "r2"
    if case == "duplicate":
        source.line_items.append(source.line_items[1].model_copy())
    elif case == "parent":
        source.line_items[1].parent_id = "missing"
    elif case == "target":
        target = "missing"
    elif case == "cycle":
        source.line_items[0].parent_id = "r2"
    else:
        source.line_items.append(row(4, "Other", "1", parent=4))
    with pytest.raises(ValueError):
        baseline_decision(source, target, INDEX, ())


@pytest.mark.parametrize("case", ["heading", "no_value", "flag", "cell", "period"])
def test_source_uncertainty_is_held(case: str) -> None:
    source = statement([row(1, "Revenue", "1")], StatementType.INCOME)
    if case == "heading":
        source.line_items[0].cells = []
    elif case == "no_value":
        source.line_items[0].cells[0].reported = None
    elif case == "flag":
        source.flags.append("source_scope_unproven")
    elif case == "cell":
        source.line_items[0].cells[0].flags.append("row_misaligned")
    else:
        source.flags.append("period_conflict")
    result = assess(source, "r1", "revenue")
    assert result.state == "unresolved" and result.canonical_id is None


@pytest.mark.parametrize("state", ["unresolved", "invalid", "model_abstention", "rejected"])
def test_decision_state_invariant(state: str) -> None:
    supported = assess(statement([row(1, "Revenue", "1")], StatementType.INCOME), "r1", "revenue")
    with pytest.raises(ValidationError):
        NormalizationDecision.model_validate({**supported.model_dump(), "state": state})


@pytest.mark.parametrize("label,cid", [("Basic", "eps_basic"), ("Diluted", "eps_diluted")])
def test_short_eps_uses_existing_heading_rule_only(label: str, cid: str) -> None:
    source = statement(
        [row(1, "Earnings per share", None), row(2, label, "1", parent=1)], StatementType.INCOME
    )
    assert assess(source, "r2", cid).state == "supported"
    source.line_items[0].raw_label = "Dividends per share"
    assert assess(source, "r2", cid).state == "unresolved"


@pytest.mark.parametrize(
    "label",
    [
        "Basic and diluted earnings per share",
        "Not diluted earnings per share",
        "Basic earnings per share excluding basic",
    ],
)
def test_mixed_and_negated_captions_cannot_invent_share_kind(label: str) -> None:
    source = statement([row(1, label, "1")], StatementType.INCOME)
    assert assess(source, "r1", "eps_basic").state == "unresolved"
    assert assess(source, "r1", "eps_diluted").state == "unresolved"


def test_failed_period_cancels_a_passing_anchor() -> None:
    from test_label_mapping import P2

    items = liabilities()
    for item in items:
        if item.cells:
            item.cells.append(item.cells[0].model_copy(update={"period_key": P2.key}))
    checks = (*passing(items), check("subtotal", ["r2", "r3", "r4"], "fail", P2))
    assert (
        assess(statement(items), "r3", "lease_liabilities_non_current", checks).state
        == "unresolved"
    )


def test_audit_reports_observations_and_inference_without_label_boxes() -> None:
    source = statement(liabilities())
    decision = assess(
        source, "r3", "lease_liabilities_non_current", tuple(passing(source.line_items))
    )
    observed = next(o for o in decision.observations if o.source_row.line_item_id == "r3")
    assert observed.printed_label == "Lease liabilities"
    assert observed.inferred_parent_id == "r4"
    assert observed.source_row.label_region == "unavailable"
    assert observed.source_row.value_rows[0].row == 3
    assert len(decision.observations) == len(source.line_items)
    assert "bbox" not in decision.model_dump_json()
    from fra_ingest.normalization_context import build_normalization_request

    payload = build_normalization_request(source, "r3").model_payload_json()
    for forbidden in (
        "canonical",
        "reported",
        "123456",
        "audit",
        "tables",
        "r3",
        source.document_sha256,
    ):
        assert forbidden not in payload


@pytest.mark.parametrize("case", ["blank_label", "source_type", "period", "statement_type"])
def test_invalid_source_contract_fails_loudly(case: str) -> None:
    source = statement([row(1, "Revenue", "1")], StatementType.INCOME)
    if case == "blank_label":
        source.line_items[0].raw_label = " "
    elif case == "source_type":
        source.line_items[0].cells[0].provenance = (
            source.line_items[0].cells[0].provenance.model_copy(update={"source": "unknown"})
        )
    elif case == "period":
        source.line_items[0].cells[0].period_key = "foreign"
    else:
        source = source.model_copy(update={"type": "unknown"})
    with pytest.raises(ValueError):
        baseline_decision(source, "r1", INDEX, ())


def test_source_mapping_metadata_and_uncertainty_are_not_mutated() -> None:
    source = statement(liabilities())
    target = source.line_items[2]
    target.canonical_id = "lease_liabilities_current"
    target.mapping_confidence = 1.0
    target.mapping_evidence = "invented proof"
    original = source.model_dump_json()
    checks = tuple(passing(source.line_items))
    assert (
        assess(source, "r3", "lease_liabilities_non_current", checks).canonical_id
        == "lease_liabilities_non_current"
    )
    assert source.model_dump_json() == original
    source.flags.append("ambiguous_statement:balance")
    assert assess(source, "r3", "lease_liabilities_non_current", checks).state == "unresolved"


def test_skipped_scope_cannot_be_promoted_by_another_period_pass() -> None:
    from test_label_mapping import P2

    items = liabilities()
    for item in items:
        if item.cells:
            item.cells.append(item.cells[0].model_copy(update={"period_key": P2.key}))
    skipped = check("subtotal", ["r2", "r3", "r4"], "skipped", P2).model_copy(
        update={"detail": "subtotal_scope_uncertain"}
    )
    decision = assess(
        statement(items), "r3", "lease_liabilities_non_current", (*passing(items), skipped)
    )
    assert decision.state == "unresolved" and "scope" in decision.needed_evidence[0]


def test_decision_revalidates_copied_instances() -> None:
    supported = assess(statement([row(1, "Revenue", "1")], StatementType.INCOME), "r1", "revenue")
    with pytest.raises(ValidationError):
        NormalizationDecision.model_validate(supported.model_copy(update={"state": "invalid"}))
