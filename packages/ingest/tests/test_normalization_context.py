"""Synthetic structure fixtures exercise the shadow adapter without inference."""

import json

import pytest
from test_label_mapping import liabilities, row, statement

from fra_core.schemas import LineItem, Statement, StatementType
from fra_core.schemas.normalization import MAX_RELATED_LABELS
from fra_ingest.hierarchy import RowInput, infer_hierarchy
from fra_ingest.normalization_context import build_normalization_request


def section(heading: str = "Current liabilities") -> Statement:
    items = [row(1, heading, None), row(2, "Lease liabilities", "123456.78"), row(3, "Loans", "10")]
    nodes = infer_hierarchy(
        [
            RowInput(
                row=n, label=i.raw_label, indent=50 if not i.cells else 60, has_values=bool(i.cells)
            )
            for n, i in enumerate(items)
        ]
    )
    for item, node in zip(items, nodes, strict=True):
        item.parent_id = items[node.parent_row].id if node.parent_row is not None else None
        item.depth = node.depth
    return statement(items)


def test_same_printed_lease_label_has_distinct_inferred_heading_context() -> None:
    current = build_normalization_request(section(), "r2")
    noncurrent = build_normalization_request(section("Non-current liabilities"), "r2")
    assert current.payload.label == noncurrent.payload.label == "Lease liabilities"
    assert current.payload.context.heading == "Current liabilities"
    assert noncurrent.payload.context.heading == "Non-current liabilities"
    assert current.payload.context.status == noncurrent.payload.context.status == "inferred"
    assert current.model_payload_json() != noncurrent.model_payload_json()


def test_eps_neighbours_never_confirm_a_kind() -> None:
    source = statement(
        [
            row(1, "Earnings per share", None),
            row(2, "Basic", "1", parent=1),
            row(3, "Earnings per share", "1", parent=1),
            row(4, "Diluted", "1", parent=1),
        ],
        StatementType.INCOME,
    )
    payload = build_normalization_request(source, "r3").payload
    assert payload.context.related == ("Basic", "Diluted")
    assert payload.context.status == "inferred"
    assert "kind" not in payload.model_dump_json()


def test_nearby_heading_without_a_parent_link_is_unavailable() -> None:
    source = section()
    source.line_items[1].parent_id = None
    request = build_normalization_request(source, "r2")
    assert request.payload.context.status == "unavailable"
    assert request.payload.context.heading is None
    assert request.payload.context.related == ()


def test_blank_closing_subtotal_does_not_become_a_printed_parent_or_heading() -> None:
    request = build_normalization_request(statement(liabilities()), "r3")
    assert request.payload.context.status == "unavailable"
    assert request.payload.context.parent is None
    assert request.payload.context.heading is None


def test_printed_subtotal_parent_is_inferred_without_importing_other_sections() -> None:
    items = liabilities()
    items[3].raw_label = "Total non-current liabilities"
    request = build_normalization_request(statement(items), "r3")
    assert request.payload.context.parent == "Total non-current liabilities"
    assert request.payload.context.heading is None
    assert request.payload.context.related == ("Loans and borrowings",)
    assert "Bank overdrafts" not in request.model_payload_json()


def test_blank_subtotal_with_a_retained_heading_ancestor_keeps_only_printed_context() -> None:
    source = statement(
        [
            row(1, "Current liabilities", None),
            row(2, "Lease liabilities", "2", parent=4),
            row(3, "Loans", "1", parent=4),
            row(4, "", "3", parent=1, subtotal=True),
        ]
    )
    context = build_normalization_request(source, "r2").payload.context
    assert context.status == "inferred"
    assert context.heading == "Current liabilities"
    assert context.parent is None
    assert context.related == ("Loans",)


def test_section_boundary_conflict_withholds_all_context() -> None:
    source = section()
    source.line_items.extend(
        [row(4, "Non-current liabilities", None), row(5, "Lease liabilities", "1", parent=1)]
    )
    context = build_normalization_request(source, "r5").payload.context
    assert context.status == "conflicting"
    assert context.heading is None and context.parent is None and context.related == ()


def test_sibling_selection_stops_at_heading_even_with_shared_parent() -> None:
    source = section()
    source.line_items.extend(
        [row(4, "Non-current liabilities", None), row(5, "Other leases", "1", parent=1)]
    )
    context = build_normalization_request(source, "r2").payload.context
    assert context.related == ("Loans",)


@pytest.mark.parametrize(
    "case",
    ["duplicate", "missing_parent", "self_cycle", "cycle", "unrelated_cycle", "missing_target"],
)
def test_malformed_graphs_fail_loudly(case: str) -> None:
    source = section()
    target = "r2"
    match = "cycle"
    if case == "duplicate":
        source.line_items.append(source.line_items[1].model_copy())
        match = "duplicate.*r2"
    elif case == "missing_parent":
        source.line_items[1].parent_id = "missing"
        match = "missing.*parent|parent.*missing"
    elif case == "self_cycle":
        source.line_items[1].parent_id = "r2"
    elif case == "cycle":
        source.line_items[0].parent_id = "r2"
    elif case == "unrelated_cycle":
        source.line_items.append(row(4, "Other", "1", parent=4))
    else:
        target = "absent"
        match = "target.*absent"
    with pytest.raises(ValueError, match=match):
        build_normalization_request(source, target)


def test_long_parent_chain_is_validated_without_recursion() -> None:
    items = [LineItem(id=f"x{n}", raw_label="Heading", parent_id=f"x{n + 1}") for n in range(1500)]
    items[-1].parent_id = None
    source = statement(items)
    assert build_normalization_request(source, "x0").payload.context.status == "conflicting"


def test_values_answers_and_source_identifiers_stay_outside_payload() -> None:
    source = section()
    target = source.line_items[1]
    target.canonical_id = "SECRET_GOLD"
    target.mapping_evidence = "SECRET_TAG"
    target.note_ref = "SECRET_NOTE"
    source.flags.append("SECRET_ROLE")
    original = source.model_dump_json()
    request = build_normalization_request(source, "r2")
    encoded = request.model_payload_json()
    assert json.loads(encoded) == request.payload.model_dump(mode="json")
    for forbidden in (
        "123456",
        "SECRET",
        "canonical",
        "reported",
        "bbox",
        "audit",
        "r2",
        "tables",
        source.document_sha256,
    ):
        assert forbidden not in encoded
    assert "audit" not in request.model_dump_json()
    assert request.audit.document_sha256 == source.document_sha256
    target_audit = next(r for r in request.audit.rows if r.role == "target")
    assert target_audit.line_item_id == "r2"
    assert target_audit.value_rows[0].row == 2
    assert target_audit.label_region == "unavailable"
    heading_audit = next(r for r in request.audit.rows if r.role == "heading")
    assert heading_audit.value_rows == ()
    assert "bbox" not in request.audit.model_dump_json()
    assert source.model_dump_json() == original


def test_context_is_bounded_in_source_order_and_deterministic() -> None:
    source = section()
    source.line_items.extend(row(n, f"Printed label {n}", "1", parent=1) for n in range(4, 20))
    request = build_normalization_request(source, "r8")
    assert request.payload.context.related == tuple(
        f"Printed label {n}" for n in (5, 6, 7, 9, 10, 11)
    )
    assert len(request.payload.context.related) == MAX_RELATED_LABELS
    assert (
        request.model_payload_json()
        == build_normalization_request(source, "r8").model_payload_json()
    )
    related = [r.source_order for r in request.audit.rows if r.role == "related"]
    assert related == sorted(related)
