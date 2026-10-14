"""Pure authorization of statement starts from unpadded locate sources (spec 11)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from fra_core.schemas import StatementType
from fra_ingest.locate import plan_conversion
from fra_ingest.results import PageScore, StatementRange


@dataclass(frozen=True)
class SourcePlan:
    ranges: tuple[StatementRange, ...]
    pages: tuple[PageScore, ...]
    spans: tuple[tuple[int, int], ...]
    primary: tuple[StatementRange, ...]
    primary_span: tuple[int, int] | None
    tied_spans: tuple[tuple[int, int], ...]
    ambiguous: bool
    page_count: int
    ambiguous_types: tuple[StatementType, ...] = ()

    def is_ambiguous(self, kind: StatementType) -> bool:
        """Whether ``kind`` has no single primary range. Without any candidate block every type
        is ambiguous; otherwise only the types of competing tied blocks are."""
        if self.primary_span is None:
            return self.ambiguous
        return kind in self.ambiguous_types


@dataclass(frozen=True)
class TableSourceContext:
    proposed_type: StatementType
    page_no: int
    matching: tuple[StatementRange, ...]
    primary: StatementRange | None
    role: str
    distance: int | None
    titles: tuple[StatementType, ...]
    negatives: tuple[str, ...]
    ambiguous: bool
    data_rows: int


@dataclass(frozen=True)
class SourceDecision:
    allowed: bool
    reason: str
    group_key: str | None
    evidence: tuple[str, ...]


def source_plan(
    ranges: Sequence[StatementRange],
    pages: Sequence[PageScore],
    enabled: Sequence[StatementType],
    page_count: int,
    pad_pages: int,
) -> SourcePlan:
    if page_count < 1 or pad_pages < 0:
        raise ValueError("source page_count must be positive and pad_pages nonnegative")
    selected = tuple(
        sorted(
            (r for r in ranges if r.type in enabled),
            key=lambda r: (r.type.value, r.rank, r.first_page, r.last_page),
        )
    )
    by_page = {p.page_no: p for p in pages}
    if len(by_page) != len(pages) or any(n > page_count for n in by_page):
        raise ValueError("source page evidence has duplicate or out-of-bounds pages")
    ranks: set[tuple[StatementType, int]] = set()
    for r in selected:
        if r.last_page < r.first_page or r.last_page > page_count:
            raise ValueError(f"source range invalid: {r.type.value}:{r.first_page}-{r.last_page}")
        if (r.type, r.rank) in ranks:
            raise ValueError(f"source range rank duplicated: {r.type.value}:{r.rank}")
        ranks.add((r.type, r.rank))
        if any(n not in by_page for n in range(r.first_page, r.last_page + 1)):
            raise ValueError(
                f"source page evidence missing for {r.type.value}:{r.first_page}-{r.last_page}"
            )
    spans = tuple(plan_conversion(selected, enabled, page_count, pad_pages))
    if not selected:
        return SourcePlan(
            (), tuple(sorted(pages, key=lambda p: p.page_no)), (), (), None, (), False, page_count
        )
    supported = {
        s: tuple(
            r
            for r in selected
            if s[0] <= r.first_page <= r.last_page <= s[1]
            and any(
                by_page[n].is_candidate(r.type) and not by_page[n].negatives
                for n in range(r.first_page, r.last_page + 1)
            )
        )
        for s in spans
    }
    types = {s: {r.type for r in rs} for s, rs in supported.items()}
    support = {
        s: (
            len({r.type for r in rs if r.rank == 1}),
            len(types[s]),
            -sum(min(r.rank for r in rs if r.type is t) for t in types[s]),
        )
        for s, rs in supported.items()
    }
    best = max(support.values())
    tied = tuple(s for s in spans if support[s] == best)
    primary_span = tied[0] if best[1] else None
    groups = _competing_groups(tied, types)
    # A block sharing no type with another tied block is a primary source on its own; of
    # blocks that compete, only the first supplies the reserved ranges.
    primary_spans = tuple(g[0] for g in groups) if primary_span is not None else ()
    ambiguous_types = sorted(
        {t for g in groups if len(g) > 1 for s in g for t in types[s]}, key=lambda t: t.value
    )
    return SourcePlan(
        selected,
        tuple(sorted(pages, key=lambda p: p.page_no)),
        spans,
        _lowest_ranked(selected, primary_spans),
        primary_span,
        tied,
        bool(ambiguous_types) or not best[1],
        page_count,
        tuple(ambiguous_types),
    )


def _competing_groups(
    tied: Sequence[tuple[int, int]], types: dict[tuple[int, int], set[StatementType]]
) -> list[list[tuple[int, int]]]:
    """The tied blocks grouped by shared statement types, directly or through another block;
    each group keeps the order of ``tied``."""
    groups: list[list[tuple[int, int]]] = []
    for s in tied:
        joined = [g for g in groups if any(types[s] & types[o] for o in g)]
        merged = sorted([s, *(o for g in joined for o in g)], key=tied.index)
        groups = [g for g in groups if g not in joined] + [merged]
    return sorted(groups, key=lambda g: tied.index(g[0]))


def _lowest_ranked(
    selected: Sequence[StatementRange], spans: Sequence[tuple[int, int]]
) -> tuple[StatementRange, ...]:
    """The lowest-rank range of each type that lies wholly inside one of ``spans``."""
    inside = [r for r in selected if any(a <= r.first_page <= r.last_page <= b for a, b in spans)]
    return tuple(
        min((r for r in inside if r.type is t), key=lambda r: r.rank)
        for t in sorted({r.type for r in inside}, key=lambda t: t.value)
    )


def resolve_tied_blocks(plan: SourcePlan) -> SourcePlan:
    """The plan once two tied blocks are proved to be opposite scopes: nothing is ambiguous, and
    the second block is a primary source for each type the first does not name. A type both
    blocks name keeps the first block's range. A tie in which neither block was located as a
    candidate has no primary block to extend, so it stays as it is."""
    if plan.primary_span is None:
        return plan
    if len(plan.tied_spans) != 2:
        raise ValueError(f"only two tied source blocks can be resolved, got {len(plan.tied_spans)}")
    named = {r.type for r in plan.primary}
    second = tuple(
        r for r in _lowest_ranked(plan.ranges, plan.tied_spans[1:]) if r.type not in named
    )
    primary = sorted((*plan.primary, *second), key=lambda r: r.type.value)
    return replace(plan, primary=tuple(primary), ambiguous=False, ambiguous_types=())


def table_source(
    plan: SourcePlan, page_no: int, proposed_type: StatementType, data_rows: int
) -> TableSourceContext:
    if not 1 <= page_no <= plan.page_count or data_rows < 0:
        raise ValueError(f"source table page/rows invalid: {page_no}/{data_rows}")
    matching = tuple(
        r for r in plan.ranges if r.type is proposed_type and r.first_page <= page_no <= r.last_page
    )
    primary = next((r for r in plan.primary if r.type is proposed_type), None)
    if primary is not None and primary in matching:
        role = "primary"
    elif matching:
        role = "secondary_range"
    elif any(a <= page_no <= b for a, b in plan.spans):
        role = "unrelated_padding"
    else:
        role = "outside_range"
    distance = (
        max(primary.first_page - page_no, page_no - primary.last_page, 0) if primary else None
    )
    page = next((p for p in plan.pages if p.page_no == page_no), None)
    if page is None:
        raise ValueError(f"source page evidence missing for table page {page_no}")
    return TableSourceContext(
        proposed_type,
        page_no,
        matching,
        primary,
        role,
        distance,
        tuple(page.title_types),
        tuple(page.negatives),
        plan.is_ambiguous(proposed_type),
        data_rows,
    )


def start_decision(
    context: TableSourceContext,
    primary_scope: bool | None,
    local_scope: bool | None,
    locally_titled: bool,
) -> SourceDecision:
    c = context
    evidence = (
        f"source_proposed:{c.proposed_type.value}",
        f"source_role:{c.role}",
        f"source_distance:{c.distance}",
        f"source_data_rows:{c.data_rows}",
        *(
            f"source_range:{r.type.value}:{r.first_page}-{r.last_page}:rank={r.rank}"
            for r in c.matching
        ),
        *(
            (
                f"source_primary:{c.primary.type.value}:{c.primary.first_page}-{c.primary.last_page}:rank={c.primary.rank}",
            )
            if c.primary
            else ()
        ),
        *(f"source_title:{t.value}" for t in c.titles),
        *(f"source_negative:{n}" for n in c.negatives),
    )
    reason = ""
    group = f"primary:{c.proposed_type.value}"
    if c.negatives:
        reason = "source_non_statement_page"
    elif c.titles and c.proposed_type not in c.titles:
        reason = "source_conflicting_title"
    elif c.ambiguous:
        reason = "source_primary_ambiguous"
    elif c.role == "primary" and not (
        primary_scope is not None and local_scope is not None and local_scope is not primary_scope
    ):
        return SourceDecision(True, "source_primary", group, evidence)
    elif c.matching and locally_titled:
        if primary_scope is None or local_scope is None or local_scope is primary_scope:
            reason = "source_scope_unproven"
        else:
            r = c.matching[0]
            return SourceDecision(
                True,
                "source_opposite_scope",
                f"scope:{c.proposed_type.value}:{local_scope}:{r.first_page}-{r.last_page}",
                evidence,
            )
    else:
        reason = f"source_{c.role}" if c.role != "primary" else "source_scope_unproven"
    return SourceDecision(False, reason, None, (*evidence, reason))
