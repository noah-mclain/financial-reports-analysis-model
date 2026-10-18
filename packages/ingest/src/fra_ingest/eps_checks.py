"""Structure EPS notes with the ordinary part builder, then check their statement cells.

A note is authorized by its printed reference and a tie to attributable profit. Monetary and
share scales are local to the note; neither is borrowed from the statement or inferred from
whether a quotient looks plausible. Missing or competing evidence vouches for nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product

from fra_core.labels import normalize_label
from fra_core.numbers import normalize_digits
from fra_core.schemas import Cell, CheckResult, LineItem, Statement, StatementType, TextSource
from fra_core.tolerance import rounding_tolerance
from fra_core.units import detect_currency, detect_scale, unit_marker_spans
from fra_ingest.classify import Classification
from fra_ingest.header import parse_header
from fra_ingest.label_mapping import eps_heading
from fra_ingest.label_match import LabelIndex, squash
from fra_ingest.metadata import Metadata, detect_metadata, statement_title_scope
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.period_evidence import period_identity
from fra_ingest.review import is_critical
from fra_ingest.table_grid import Grid

# EPS printed in a currency subunit would be vouched 100x too small against profit / shares.
# Whole words only: "recent", "Centre" and Arabic "للسنتين" are not subunits.
_SUBUNIT_WORDS = frozenset(
    normalize_label(word)
    for word in [
        "halala",
        "halalas",
        "halalah",
        "halalahs",
        "fils",
        "cent",
        "cents",
        "pence",
        "penny",
        "piastre",
        "piastres",
        "piaster",
        "piasters",
        "qirsh",
        "qurush",
        "هللة",
        "هللات",
        "هلله",
        "فلس",
        "فلوس",
        "قرش",
        "قروش",
        "سنت",
        "سنتات",
    ]
)
_ARABIC_PREFIXES = ("", "ب", "ل", "و", "ال", "بال", "لل")
_BASIC_WORDS = frozenset({"basic", "الاساسيه", "والاساسيه"})
_DILUTED_WORDS = frozenset({"diluted", "المخفضه", "والمخفضه"})
# The words of the expected label, "weighted average number of ordinary shares for the purposes
# of basic/diluted earnings". Any other word (not, un-, excl., fully, a unit) leaves it unsettled.
_SHARE_WORDS = (
    "weighted average number of ordinary shares for the purposes calculating "
    "earnings earning per share eps "
    "المتوسط المرجح لعدد الأسهم العادية لأغراض احتساب ربحية السهم"
)
_SHARE_VOCABULARY = (
    _BASIC_WORDS | _DILUTED_WORDS | frozenset(normalize_label(w) for w in _SHARE_WORDS.split())
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_label(text)))


def _names_subunit(text: str) -> bool:
    return any(
        word.removeprefix(prefix) in _SUBUNIT_WORDS
        for word in _words(text)
        for prefix in _ARABIC_PREFIXES
        if word.startswith(prefix)
    )


def _label_context(statement: Statement) -> dict[str, str]:
    """For each EPS row, the text that states its unit: the EPS heading, the caption rows
    directly above the figures, and the row's own label."""
    context: dict[str, str] = {}
    eps_head = ""
    block: list[str] = []  # consecutive caption rows above the figures
    for item in statement.line_items:
        if not item.cells:
            block.append(item.raw_label)
            eps_head = item.raw_label if eps_heading(item.raw_label) else ""
        elif item.canonical_id not in ("eps_basic", "eps_diluted"):
            block = []
        context[item.id] = " ".join((eps_head, *block, item.raw_label))
    return context


def _currencies(text: str) -> set[str]:
    markers = [text, *(text[start:end] for start, end in unit_marker_spans(text))]
    return {code for marker in markers if (code := detect_currency(marker)) is not None}


def _discriminates(lower: Decimal, upper: Decimal) -> bool:
    """Whether the interval is too narrow to hold both a figure and the same figure with a
    currency subunit slip (a factor of 100). A check that cannot tell them apart vouches for
    nothing."""
    if (lower > 0) != (upper > 0) or lower == 0 or upper == 0:
        return False
    near, far = sorted((abs(lower), abs(upper)))
    return far < 100 * near


@dataclass
class EpsNote:
    part: PartialStatement
    references: frozenset[str]
    numerator_ids: tuple[str, ...]
    share_ids: dict[str, tuple[str, ...]]
    scales: dict[str, int | None]
    currencies: frozenset[str]
    metadata: Metadata


def _share_kind(label: str) -> str | None:
    text = squash(label)
    weighted = (
        "weightedaveragenumberofordinaryshares" in text
        or squash("المتوسط المرجح لعدد الأسهم العادية") in text
    )
    if not weighted:
        return None
    unit_free = label
    for start, end in reversed(unit_marker_spans(label)):  # a unit marker is not a modifier
        unit_free = unit_free[:start] + " " + unit_free[end:]
    tokens = set(re.findall(r"\w+", normalize_label(unit_free)))
    if not tokens <= _SHARE_VOCABULARY:
        return None
    basic, diluted = bool(tokens & _BASIC_WORDS), bool(tokens & _DILUTED_WORDS)
    return ("basic" if basic else "diluted") if basic != diluted else None


def _numerator(label: str) -> bool:
    text = squash(label)
    return (
        "profitfortheyearattributabletotheshareholdersofthecompany" in text
        or squash("ربح السنة العائد إلى المساهمين في الشركة") in text
    )


def _scale(text: str, fallback: int | None = None) -> int | None:
    signals = {
        signal.scale
        for start, end in unit_marker_spans(text)
        if (signal := detect_scale(text[start:end])) is not None
    }
    if (signal := detect_scale(text)) is not None:
        signals.add(signal.scale)
    if re.search(r"\b(?:units|actual number)\b", text, re.I) or squash("بالوحدات") in squash(text):
        signals.add(1)
    if not signals:
        return fallback
    return next(iter(signals)) if len(signals) == 1 else None


def structure_eps_note(
    grid: Grid,
    headings: Sequence[str],
    index: LabelIndex,
    *,
    source: TextSource,
    visual: bool = False,
    context_texts: Sequence[str] = (),
) -> EpsNote | None:
    layout = parse_header(
        grid, StatementType.INCOME, None, period_evidence=grid.period_evidence, visual=visual
    )
    if layout.label_col is None:
        return None
    labels = [grid.text(r, layout.label_col) for r in range(grid.num_rows)]
    if not any(_share_kind(label) is not None for label in labels):
        return None
    part = build_part(
        grid,
        layout,
        Classification(type=StatementType.INCOME, confidence=1),
        source=source,
        index=index,
    )
    references = frozenset(
        str(int(match.group(1)))
        for heading in headings
        if eps_heading(heading)
        if (
            match := re.match(
                r"\s*(?:Note\s+)?(\d{1,3})\s*[.\-–—]", normalize_digits(heading)[0], re.I
            )
        )
    )
    if not references:
        return None  # a share-count row alone does not make a table an EPS note
    shares: dict[str, list[str]] = {"basic": [], "diluted": []}
    numerators = []
    scales: dict[str, int | None] = {}
    share_scale = None
    for item in part.line_items:
        label = item.raw_label
        # The share section's own caption, not a monetary header above it.
        if not item.cells and (
            "numberofshares" in squash(label) or squash("عدد الأسهم") in squash(label)
        ):
            share_scale = _scale(label)
        kind = _share_kind(label)
        if kind is not None:
            shares[kind].append(item.id)
            scales[item.id] = _scale(label, share_scale)
        if item.cells and _numerator(label):
            numerators.append(item.id)
            scales[item.id] = _scale(label, _scale(part.header_text))
    unit_texts = [
        *layout.header_texts.values(),
        *(i.raw_label for i in part.line_items if i.id in numerators),
    ]
    currencies = frozenset(
        currency
        for text in unit_texts
        for marker in [text, *(text[start:end] for start, end in unit_marker_spans(text))]
        if (currency := detect_currency(marker)) is not None
    )
    context = [*headings, *context_texts]
    metadata = detect_metadata(
        header_text=part.header_text, context_texts=context, document_texts=[]
    )
    metadata.consolidated = statement_title_scope(context)
    return EpsNote(
        part,
        references,
        tuple(numerators),
        {k: tuple(v) for k, v in shares.items()},
        scales,
        currencies,
        metadata,
    )


def _cell(item: LineItem, key: str) -> Cell | None:
    cells = [c for c in item.cells if c.period_key == key and c.reported is not None]
    return cells[0] if len(cells) == 1 else None


def _quantum(cell: Cell) -> Decimal:
    assert cell.reported is not None
    # parse_number preserves printed decimal places in the Decimal exponent.
    exponent = cell.reported.as_tuple().exponent
    assert isinstance(exponent, int)
    return Decimal(1).scaleb(min(0, exponent))


def _evidence(item: LineItem, cell: Cell, scale: int) -> str:
    p = cell.provenance
    return (
        f"{item.id}[page={p.page_no},table={p.table_ref},row={p.row},col={p.col},"
        f"reported={cell.reported},scale={scale}]"
    )


def check_eps(
    statement: Statement, notes: Sequence[EpsNote], header_text: str = ""
) -> list[CheckResult]:
    if statement.type is not StatementType.INCOME or not notes:
        return []
    results: list[CheckResult] = []
    context = _label_context(statement)
    for item in statement.line_items:
        if item.canonical_id not in ("eps_basic", "eps_diluted"):
            continue
        reference = normalize_digits(item.note_ref or "")[0].strip(" ()")
        matching = [n for n in notes if reference in n.references]
        settled = [n for n in matching if n.metadata.consolidated is not None]
        scoped = [
            n
            for n in settled
            if n.metadata.consolidated == statement.consolidated
            and (
                not statement.entity_name
                or not n.metadata.entity_name
                or n.metadata.entity_name.casefold() == statement.entity_name.casefold()
            )
        ]
        for period in statement.periods:
            key = period.key
            actual = item.value_for(key)
            result = CheckResult(
                id=f"{statement.id}:eps:{item.id}:{key}",
                statement_id=statement.id,
                kind="eps",
                period_key=key,
                status="skipped",
                actual=actual,
                line_item_ids=[item.id],
            )
            reason = ""
            if _names_subunit(f"{header_text} {context[item.id]}"):
                reason = "eps_unit_unsupported: EPS is printed in a currency subunit"
            elif _currencies(context[item.id]) - {statement.currency}:
                reason = "currency_conflict: EPS names a currency other than the statement's"
            elif not matching:
                reason = "note_reference_unsettled: supply an explicit EPS note reference"
            elif statement.consolidated is None or (not scoped and len(settled) != len(matching)):
                reason = "scope_unsettled: supply explicit consolidation scope"
            elif not scoped:
                reason = "scope_conflict: reconcile note and statement entity and consolidation"
            elif (
                len(scoped) != 1
                or len(scoped[0].references) != 1
                or any(f.startswith("ambiguous_statement:") for f in statement.flags)
            ):
                reason = "note_ambiguous: settle the statement and its unique EPS note"
            else:
                result = _compare(statement, item, period.key, scoped[0], result)
            results.append(result.model_copy(update={"detail": reason}) if reason else result)
    return results


def _compare(
    statement: Statement, item: LineItem, key: str, note: EpsNote, result: CheckResult
) -> CheckResult:
    by_id = {i.id: i for i in note.part.line_items}
    kind = (item.canonical_id or "").removeprefix("eps_")
    share_ids = note.share_ids[kind]
    reason = ""
    # Never substitute consolidated profit when an attributable line cannot be settled.
    attributed = [
        i for i in statement.line_items if i.canonical_id == "net_income_attributable_parent"
    ]
    if len(note.numerator_ids) != 1 or len(attributed) != 1:
        reason = (
            "numerator_ambiguous: supply one note numerator and its attributable statement line"
        )
    elif len(share_ids) != 1:
        reason = f"shares_ambiguous: supply one printed weighted average for {kind}"
    elif note.part.period_conflicts or {period_identity(p) for p in note.part.periods} != {
        period_identity(p) for p in statement.periods
    }:
        reason = "period_unsettled: settle matching note and statement periods"
    elif note.currencies and note.currencies != {statement.currency}:
        reason = "currency_conflict: settle note and statement currency"
    if reason:
        return result.model_copy(update={"detail": reason})
    numerator, shares = by_id[note.numerator_ids[0]], by_id[share_ids[0]]
    profit_scale, share_scale = note.scales[numerator.id], note.scales[shares.id]
    if share_scale is None or profit_scale is None:
        reason = "share_scale_unsettled" if share_scale is None else "profit_scale_unsettled"
        return result.model_copy(update={"detail": f"{reason}: supply the note's own unit/scale"})
    profit, count, eps, tied = (
        _cell(numerator, key),
        _cell(shares, key),
        _cell(item, key),
        _cell(attributed[0], key),
    )
    if any(c is None for c in (profit, count, eps, tied)):
        return result.model_copy(
            update={"detail": "missing_values: supply all note and statement operands"}
        )
    assert profit is not None and count is not None and eps is not None and tied is not None
    assert (
        profit.reported is not None
        and count.reported is not None
        and eps.reported is not None
        and tied.reported is not None
    )
    if any(is_critical(c) or "percent" in c.flags for c in (profit, count, eps, tied)):
        return result.model_copy(
            update={"detail": "operand_flagged: resolve uncertain source cells"}
        )
    p, s = profit.reported * profit_scale, count.reported * share_scale
    p_error = rounding_tolerance(1) * _quantum(profit) * profit_scale
    s_error = rounding_tolerance(1) * _quantum(count) * share_scale
    if s - s_error <= 0:
        return result.model_copy(
            update={"detail": "shares_not_positive: supply a positive weighted average"}
        )
    detail = (
        f"note={note.part.table_refs[0]}; numerator={_evidence(numerator, profit, profit_scale)}; "
        f"shares={_evidence(shares, count, share_scale)}; tied_to={attributed[0].id}"
    )
    tied_profit = tied.reported * statement.scale
    tied_error = rounding_tolerance(1) * _quantum(tied) * statement.scale
    profit_lower = max(p - p_error, tied_profit - tied_error)
    profit_upper = min(p + p_error, tied_profit + tied_error)
    if profit_lower > profit_upper:
        return result.model_copy(
            update={
                "status": "fail",
                "detail": detail
                + "; numerator_tie_failed: reconcile attributable profit with the note",
            }
        )
    expected = (profit_lower + profit_upper) / 2 / s
    bounds = [
        numerator / (s + ds)
        for numerator, ds in product((profit_lower, profit_upper), (-s_error, s_error))
    ]
    eps_error = rounding_tolerance(1) * _quantum(eps)
    lower, upper = min(bounds) - eps_error, max(bounds) + eps_error
    passed = lower <= eps.reported <= upper
    if passed and not _discriminates(lower, upper):
        return result.model_copy(
            update={
                "detail": detail
                + f"; precision_insufficient: rounding_interval=[{lower},{upper}] cannot tell "
                "the figure from a 100x subunit slip; supply more precise operands"
            }
        )
    # Record the bound on the side of the observed figure; do not widen an asymmetric interval.
    tolerance = upper - expected if eps.reported >= expected else expected - lower
    return result.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "expected": expected,
            "difference": eps.reported - expected,
            "tolerance": tolerance,
            "detail": detail
            + f"; rounding_interval=[{lower},{upper}]"
            + ("" if passed else "; reconcile EPS with attributable profit and weighted shares"),
        }
    )
