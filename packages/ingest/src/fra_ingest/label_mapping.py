"""Map each valued row of a balance sheet or income statement to a canonical item, or flag it.

Evidence in order: taxonomy aliases (``LabelIndex``, both languages), then structural anchors
for what the aliases leave open. An anchor reads only what the structure stage already settled:
the heading above a section, and the sums ``table_checks`` confirmed. Nothing is summed again
here, and nothing is read from a row's position.

A total is mapped only when a check confirms it: its subtotal check passed (and no period's
failed), or a passed balance identity names it. An alias to a total on a row nothing confirms is
flagged, as is a row whose label fits more than one item, whose alias disagrees with an anchor,
or an item two rows claim with different figures. A flagged row is left unmapped: a mapping is
never guessed. Section headings are not aliases (``LabelIndex.headings``): they only tell which
total sits under them. Cash flow is not mapped yet. Headings carry no values and are left alone.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from fra_core.schemas import CheckResult, LineItem, MappingSource, Statement, StatementType
from fra_core.taxonomy.loader import CanonicalItem
from fra_ingest.label_match import CLOSING_TOTAL_ID, LabelIndex

MAPPED_TYPES = (StatementType.BALANCE, StatementType.INCOME)
# A total printed over totals: the parts it must have as checked children. The closing total
# is mapped by this sum or by a passed balance identity, never by where it sits.
ANCHOR_SUMS: Mapping[str, frozenset[str]] = {
    "total_assets": frozenset({"total_current_assets", "total_non_current_assets"}),
    "total_liabilities": frozenset({"total_current_liabilities", "total_non_current_liabilities"}),
    "total_equity": frozenset({"equity_attributable_parent", "non_controlling_interests"}),
    CLOSING_TOTAL_ID: frozenset({"total_liabilities", "total_equity"}),
}
_Anchors = dict[str, list[tuple[str, str]]]


def _ids(items: tuple[CanonicalItem, ...]) -> tuple[str, ...]:
    return tuple(sorted(i.id for i in items))


def _unfailed_passes(checks: Sequence[CheckResult], kind: str) -> list[CheckResult]:
    """One passed result for each check of this kind that passed in a period and failed in
    none. A check that failed in any period confirms nothing. A subtotal check is the same
    check in every period by its total; an identity by the rows it names."""
    by_check: dict[str, list[CheckResult]] = defaultdict(list)
    for check in checks:
        if check.kind == kind and check.line_item_ids:
            key = check.line_item_ids[-1] if kind == "subtotal" else "|".join(check.line_item_ids)
            by_check[key].append(check)
    return [
        next(c for c in group if c.status == "pass")
        for group in by_check.values()
        if any(c.status == "pass" for c in group) and not any(c.status == "fail" for c in group)
    ]


def checked_totals(checks: Sequence[CheckResult]) -> frozenset[str]:
    """Ids of the totals whose subtotal check passed in a period and failed in none."""
    return frozenset(c.line_item_ids[-1] for c in _unfailed_passes(checks, "subtotal"))


def confirmed_totals(checks: Sequence[CheckResult]) -> frozenset[str]:
    """The checked totals, and every row a passed balance identity (failed in no period) names:
    a total with no parts of its own is confirmed by the identity it closes."""
    named = (i for c in _unfailed_passes(checks, "balance_identity") for i in c.line_item_ids)
    return checked_totals(checks) | frozenset(named)


def _section_anchors(
    statement: Statement, index: LabelIndex, checked: frozenset[str], anchors: _Anchors
) -> None:
    """A total under a heading that names a total: the heading above its first row, or a
    heading run into that row's label."""
    items = statement.line_items
    position = {item.id: n for n, item in enumerate(items)}
    for total in items:
        if not (total.is_subtotal and total.cells and total.id in checked):
            continue
        children = [i for i in items if i.parent_id == total.id]
        if not children:
            continue
        first = children[0]
        above = items[position[first.id] - 1] if position[first.id] else None
        if above is not None and not above.cells:
            named = index.headings(above.raw_label, statement.type)
        else:
            named = index.run_in_heading(first.raw_label, statement.type)
        if len(named) == 1:
            anchors[total.id].append(("section_heading", named[0].id))


def _sum_anchors(
    statement: Statement, mapped: Mapping[str, str], checked: frozenset[str], anchors: _Anchors
) -> None:
    for total in statement.line_items:
        if not (total.is_subtotal and total.cells and total.id in checked):
            continue
        parts = {
            mapped[i.id] for i in statement.line_items if i.parent_id == total.id and i.id in mapped
        }
        for item_id, required in ANCHOR_SUMS.items():
            if required <= parts:
                anchors[total.id].append(("sum_of_mapped", item_id))


def _resolve(
    item: LineItem,
    aliased: tuple[CanonicalItem, ...],
    anchored: list[tuple[str, str]],
    confirmed: frozenset[str],
) -> tuple[str | None, MappingSource | None, str, str | None]:
    """The canonical id, its source, the evidence, and the flag for one row."""
    alias_ids = _ids(aliased)
    anchor_ids = sorted({a for _, a in anchored})
    if len(aliased) > 1:
        return None, None, f"alias_multiple:{'|'.join(alias_ids)}", "ambiguous"
    if len(anchor_ids) > 1:
        kinds = "|".join(f"{k}={a}" for k, a in sorted(anchored))
        return None, None, f"anchor_conflict:{kinds}", "ambiguous"
    if aliased and anchor_ids and alias_ids[0] != anchor_ids[0]:
        kind = anchored[0][0]
        evidence = f"anchor_conflict:alias={alias_ids[0]}|{kind}={anchor_ids[0]}"
        return None, None, evidence, "ambiguous"
    if aliased and aliased[0].subtotal and item.id not in confirmed:
        return None, None, f"alias_total_unconfirmed:{alias_ids[0]}", "ambiguous"
    if aliased:
        return alias_ids[0], MappingSource.LEXICON, "alias", None
    if anchor_ids:
        return anchor_ids[0], MappingSource.ANCHOR, f"anchor:{anchored[0][0]}", None
    return None, None, "no_alias_no_anchor", "unmapped"


def _same_figures(a: LineItem, b: LineItem) -> bool:
    return {(c.period_key, c.reported) for c in a.cells} == {
        (c.period_key, c.reported) for c in b.cells
    }


def _under(row: LineItem, ancestor_id: str, by_id: Mapping[str, LineItem]) -> bool:
    parent = row.parent_id
    while parent is not None:
        if parent == ancestor_id:
            return True
        parent = by_id[parent].parent_id
    return False


def _outer_row(group: list[LineItem], by_id: Mapping[str, LineItem]) -> LineItem | None:
    """The one row every other row of the group sits under (two "Total equity" rows, the inner
    one over the parent's share only). A row left mapped has been confirmed by a check, so an
    outer row that is not confirmed is already flagged and keeps nothing."""
    for candidate in group:
        others = [r for r in group if r is not candidate]
        if all(_under(r, candidate.id, by_id) for r in others):
            return candidate
    return None


def _claim(row: LineItem) -> str | None:
    """The item a row stakes a claim to: the one it is mapped to, or the total its alias names
    that no check confirmed."""
    if row.canonical_id is not None:
        return row.canonical_id
    prefix = "alias_total_unconfirmed:"
    evidence = row.mapping_evidence or ""
    return evidence.removeprefix(prefix) if evidence.startswith(prefix) else None


def _settle_duplicates(rows: list[LineItem]) -> list[LineItem]:
    """One row per item. A repeat of the same figures is the same figure printed twice. Rows
    with different figures cannot be told apart, so all are flagged, except an outer row a
    passed check confirmed. A row whose alias names the item but is not confirmed still claims
    it, so it cannot leave its twin looking like the only one."""
    claims: dict[str, list[LineItem]] = defaultdict(list)
    for row in rows:
        if (item_id := _claim(row)) is not None:
            claims[item_id].append(row)
    by_id = {row.id: row for row in rows}
    flagged: dict[str, str] = {}
    for item_id, group in claims.items():
        if len(group) < 2:
            continue
        if all(_same_figures(group[0], other) for other in group[1:]):
            held = [r for r in group if r.canonical_id is not None]
            for other in held[1:]:
                flagged[other.id] = f"repeat_of:{held[0].id}:{item_id}"
        else:
            outer = _outer_row(group, by_id)
            for row in group:
                if row is not outer:
                    flagged[row.id] = f"duplicate:{item_id}"
    return [
        row.model_copy(
            update={
                "canonical_id": None,
                "mapping_source": None,
                "mapping_flag": "ambiguous",
                "mapping_evidence": flagged[row.id],
            }
        )
        if row.id in flagged
        else row
        for row in rows
    ]


def map_statement(
    statement: Statement, index: LabelIndex, checks: Sequence[CheckResult]
) -> Statement:
    if statement.type not in MAPPED_TYPES:
        return statement
    aliased = {i.id: index.match_all(i.raw_label, statement.type) for i in statement.line_items}
    checked = checked_totals(checks)
    confirmed = confirmed_totals(checks)
    anchors: _Anchors = defaultdict(list)
    _section_anchors(statement, index, checked, anchors)
    first_pass = {
        i.id: found
        for i in statement.line_items
        if i.cells
        and (found := _resolve(i, aliased[i.id], anchors[i.id], confirmed))[0] is not None
    }
    _sum_anchors(statement, {k: v[0] for k, v in first_pass.items() if v[0]}, checked, anchors)
    rows: list[LineItem] = []
    for item in statement.line_items:
        if not item.cells:
            rows.append(item)
            continue
        canonical, source, evidence, flag = _resolve(
            item, aliased[item.id], anchors[item.id], confirmed
        )
        rows.append(
            item.model_copy(
                update={
                    "canonical_id": canonical,
                    "mapping_source": source,
                    "mapping_flag": flag,
                    "mapping_evidence": evidence,
                }
            )
        )
    return statement.model_copy(update={"line_items": _settle_duplicates(rows)})
