"""What the results page shows, built from an analysis without computing anything new.

Every figure on the page is a printed cell or a computed metric. A cell is shown as parsed,
with its printed text beside it, and links to the page region it was read from; a metric
shows its formula and links to each input cell. Formatting here only rounds for display: a
ratio as a percentage, times and days to the precision a reader uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import urlencode

from fra_analytics import format_amount, format_metric
from fra_core.citations import place_claims
from fra_core.presentation import caveat_text
from fra_core.schemas import (
    Analysis,
    BBox,
    JobStatus,
    MetricInput,
    MetricValue,
    Narrative,
    Provenance,
    Sentence,
    Statement,
    StatementReview,
    StatementType,
)
from fra_core.schemas.caveat import CaveatId
from fra_model import GroundingError, validate_summary

STATEMENT_TITLES = {
    StatementType.BALANCE: "Statement of financial position",
    StatementType.INCOME: "Profit or loss",
    StatementType.COMPREHENSIVE_INCOME: "Comprehensive income",
    StatementType.CASH_FLOW: "Cash flows",
    StatementType.EQUITY: "Changes in equity",
}
SCALE_WORDS = {1: "", 1_000: "thousands", 1_000_000: "millions", 1_000_000_000: "billions"}
RTL_LANGUAGES = frozenset({"ar"})
DUPLICATE_CELL = "duplicate_cell"

PLAIN_LABELS = {
    "revenue": "Revenue",
    "gross_profit": "Gross profit",
    "operating_income": "Operating income",
    "net_income": "Net income",
    "net_income_attributable_parent": "Net income attributable to owners",
    "depreciation_amortization": "Depreciation and amortization",
    "cost_of_revenue": "Cost of revenue",
    "finance_costs": "Finance costs",
    "total_assets": "Total assets",
    "total_current_assets": "Total current assets",
    "total_liabilities": "Total liabilities",
    "total_current_liabilities": "Total current liabilities",
    "total_equity": "Total equity",
    "equity_attributable_parent": "Equity attributable to owners",
    "EBITDA": "EBITDA",
    "DSO": "DSO",
    "EPS": "EPS",
    "cash_and_equivalents": "Cash and equivalents",
    "short_term_investments": "Short-term investments",
    "trade_receivables": "Trade receivables",
    "inventories": "Inventories",
    "trade_payables": "Trade payables",
    "short_term_borrowings": "Short-term borrowings",
    "current_portion_long_term_debt": "Current portion of long-term debt",
    "long_term_borrowings": "Long-term borrowings",
    "lease_liabilities_current": "Current lease liabilities",
    "lease_liabilities_non_current": "Non-current lease liabilities",
    "current_ratio": "Current ratio",
    "quick_ratio": "Quick ratio",
    "cash_ratio": "Cash ratio",
    "roa": "ROA",
    "roe": "ROE",
    "dso": "DSO",
    "dio": "Days inventory outstanding",
    "dpo": "Days payable outstanding",
    "total_debt": "Total debt",
    "debt_to_equity": "Debt to equity",
    "net_debt_to_ebitda": "Net debt to EBITDA",
    "interest_coverage": "Interest coverage",
    "cash_conversion_cycle": "Cash conversion cycle",
    "revenue_growth": "Revenue growth",
    "net_income_growth": "Net income growth",
    "gross_margin": "Gross margin",
    "operating_margin": "Operating margin",
    "net_margin": "Net margin",
    "return_on_equity": "ROE",
    "return_on_assets": "ROA",
    "asset_turnover": "Asset turnover",
    "debt_ratio": "Debt ratio",
    "working_capital": "Working capital",
    "ebitda": "EBITDA",
    "net_debt": "Net debt",
}

FLAG_LABELS = {
    "average_fallback_to_closing": "Used closing balance because a prior balance was unavailable",
    "parentheses_negative": "Parentheses indicate a negative figure",
    "per_share": "Per-share value",
    "scale_assumed_units": "Scale not stated; figures treated as single units",
    "unmapped": "Row not mapped",
    "digit_suspect": "Some digits may have been read incorrectly",
    DUPLICATE_CELL: "More than one figure was read for this period",
    "low_selectivity": "The document contained many unrelated items, so extraction was limited",
    "range_not_converted": "Some statement ranges could not be converted",
    "identity_failed": (
        "The balance check (assets = liabilities + equity) did not hold, "
        "so the figures do not reconcile"
    ),
    "tie_failed": (
        "Net profit does not match between the income statement and the statement of "
        "comprehensive income"
    ),
    "eps_failed": "Earnings per share does not match net profit and the share count",
    "period_outlier": "A reporting period differs from the other periods",
    "fraction_among_whole": "A fractional value appeared among whole-number figures",
    "row_realigned": "Rows were rearranged to match their figures",
    "numeric_columns_split": "A numeric column was split while reading the table",
    "ocr_parenthesis_repaired": "A repaired bracket may change whether a figure is negative",
    "period_unbound": "A reporting period could not be matched to a statement column",
    "period_conflict": "Reporting-period evidence conflicts, so the period is uncertain",
    "scale_conflict": (
        "The figure scale is unclear because the document gives conflicting information"
    ),
    "currency_conflict": (
        "The currency is unclear because the document gives conflicting information"
    ),
    "currency_missing": "The report does not identify a currency",
    "scale_implausible": "The stated figure scale appears implausible",
    "value_without_box": "A figure has no matching box on the page",
    "likely_bank": "The document may be a bank report, so its figures need review",
    "likely_insurer": "The document may be an insurer report, so its figures need review",
    "likely_other_financial": (
        "The document may be from another kind of financial company, so its figures need review"
    ),
    "image_pages_not_read": "Some image pages could not be read",
    "ocr_failed_pages": "Text could not be read from some pages",
    "garbled_text_layers_not_read": "Some garbled page text could not be read",
    "no_statements_found": "No financial statements were found in the document",
    "convert_failed": "Some statement pages could not be processed",
    "convert_partial": "Some statement pages were only partly processed",
    "ocr_unavailable": "Text recognition was unavailable for some statement pages",
    "page_outside_range": "A statement page was outside the supported page range",
    "page_not_converted": "A statement page could not be processed",
    "page_image_missing": "An image for a statement page was unavailable",
    "memory_over_budget": "Processing stopped after reaching the memory limit",
    "period_evidence_unavailable": (
        "Reporting-period evidence was unavailable, so the period is uncertain"
    ),
    "words_reversed": "Some words may have been read in reverse order",
    "word_order_uncertain": "Word order is uncertain",
    "words_from_page": "Some words were read from the page outside the table",
    "digits_reversed": "Some digits may have been read in reverse order",
    "arabic_indic_digits": "Arabic-Indic digits were converted to standard digits",
    "dash_as_zero": "A printed dash was read as zero",
    "word_as_zero": "A word meaning nil was read as zero",
    "not_available": "The figure was printed as not available",
    "footnote_marker": "A footnote marker was removed from the figure",
    "trailing_sign": "A sign printed after the figure was applied to it",
    "european_separators": "A decimal comma with thousands dots was interpreted",
    "comma_decimal": "A decimal comma was interpreted",
    "period_not_a_duration": "This metric needs a period with a length, not a single date",
    "period_longer_than_year": "This metric needs a one-year period, but the period is longer",
    "interim_not_annualized": "Not computed: this ratio is not annualised for a part-year period",
    "blank_confirmed": "This cell is blank on the page",
    "letters_spaced": "Some letters may have been read with incorrect spacing",
    "year_joined": "A year may have been joined incorrectly",
    "label_words_from_page": "Some row labels were read outside the table",
    "label_merged": "A row label may have merged with another row",
    "spanned_cell": "A figure spans more than one table cell",
    "bbox_synthesized": "A figure's page location was estimated",
    "rows_realigned": "Rows were rearranged to match their figures",
    "needs_review": "This statement needs review",
    "currency_mismatch": ("Figures use different currencies, so they cannot be compared directly"),
    "scale_mismatch": "Figures use different scales, so they cannot be compared directly",
    "identity_totals_not_found": (
        "The balance check could not run because key totals were not found"
    ),
    "implausible_magnitude": "A figure has an implausible magnitude",
    "unparsed": "A figure could not be read as a number",
    "numbers_missing": "A figure was not found where expected",
    "unchecked": "Some figures were not confirmed by any arithmetic check",
    "row_misaligned": "A row may be shifted against its figures",
    "ambiguous_separator": "A figure uses a separator that can be read two ways (comma or dot)",
    "duplicate_input": "A metric input appears more than once",
    "missing_input": "A required value for this metric was not found",
    "undefined_zero_denominator": (
        "This metric could not be calculated because its denominator is zero"
    ),
    "undefined_negative_denominator": (
        "This metric could not be calculated with a negative denominator"
    ),
    "negative_base": "This metric uses a negative base value and needs review",
    "arithmetic_result": "The arithmetic result needs review",
    "unrepresentable_result": "The arithmetic result could not be represented",
    "sign_unexpected": "A figure's sign is unexpected and needs review",
    "peak_footprint_rss_fallback": "Peak memory use could not be measured accurately",
    "statement_not_found": "A financial statement was not found",
    "merged_or_wrapped": "A row may be merged with or wrapped around another row",
    "note_split": "A note may have been split from its related figure",
    "text_repaired": "Some extracted text was repaired and needs review",
    "reading_uncertain": "Some text could not be read with confidence",
    "error": "A processing error occurred",
    "all_ranges_failed": "No statement pages could be processed",
    "statement_not_extracted": "A financial statement could not be extracted",
    "scale_missing": "The report does not state a figure scale",
    "currency_inferred": "The currency was inferred and needs review",
    "currency_from_domicile": (
        "The currency was inferred from the company location and needs review"
    ),
    "row_alignment_unresolved": "Some rows could not be matched to their figures with confidence",
    "ocr_failed": "Text recognition failed on a page",
    "critical_item_unmapped": "A key statement line could not be identified",
}


def plain_label(identifier: str) -> str:
    """A short reader-facing label; the original identifier remains available in the detail."""
    if identifier in PLAIN_LABELS:
        return PLAIN_LABELS[identifier]
    words = identifier.replace("_", " ").replace(":", " · ").split()
    acronyms = {
        "ebitda": "EBITDA",
        "eps": "EPS",
        "dso": "DSO",
        "ebit": "EBIT",
        "roe": "ROE",
        "roa": "ROA",
    }
    rendered = [acronyms.get(word.lower(), word.lower()) for word in words]
    if rendered and rendered[0] not in acronyms.values():
        rendered[0] = rendered[0].capitalize()
    return " ".join(rendered)


def plain_formula(formula: str) -> str:
    """Replace canonical item identifiers in a formula with reader-facing labels."""
    function_names = {"abs", "avg"}
    return re.sub(
        r"\b[a-z][a-z0-9_]*\b",
        lambda match: (
            match.group() if match.group() in function_names else plain_label(match.group())
        ),
        formula,
    )


def flag_label(flag: str) -> str:
    """Explain common extraction and calculation flags without dropping their source code."""
    if flag.startswith("error:"):
        return FLAG_LABELS["error"]
    identifier = flag.partition(":")[0]
    label = FLAG_LABELS.get(identifier)
    if label is not None:
        match = re.fullmatch(
            r"(image_pages_not_read|ocr_failed_pages|garbled_text_layers_not_read|"
            r"page_not_converted|digit_suspect|period_outlier|fraction_among_whole|"
            r"row_realigned|numeric_columns_split):(\d+)",
            flag,
        )
        if match:
            count = int(match.group(2))
            counted_labels = {
                "image_pages_not_read": (
                    f"{count} image {'page could' if count == 1 else 'pages could'} not be read"
                ),
                "ocr_failed_pages": (
                    f"Text could not be read from {count} {'page' if count == 1 else 'pages'}"
                ),
                "garbled_text_layers_not_read": (
                    f"{count} {'page had' if count == 1 else 'pages had'} garbled text "
                    "that could not be read"
                ),
                "page_not_converted": (
                    f"{count} {'page could' if count == 1 else 'pages could'} not be processed"
                ),
                "digit_suspect": (
                    f"{count} {'figure may have' if count == 1 else 'figures may have'} "
                    "misread digits"
                ),
                "period_outlier": (
                    f"{count} "
                    f"{'reporting period differs' if count == 1 else 'reporting periods differ'} "
                    "from the other periods"
                ),
                "fraction_among_whole": (
                    f"{count} {('fractional value' if count == 1 else 'fractional values')} "
                    "appeared among whole-number figures"
                ),
                "row_realigned": (
                    f"{count} {'row was' if count == 1 else 'rows were'} "
                    "rearranged to match figures"
                ),
                "numeric_columns_split": (
                    f"{count} {'numeric column was' if count == 1 else 'numeric columns were'} "
                    "split while reading the table"
                ),
            }
            return counted_labels[match.group(1)]
        return label
    return plain_label(identifier)


def readable_code(code: str) -> str:
    """Make an unfamiliar machine identifier readable without exposing its syntax."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w -]", " ", code.replace("_", " "))).strip().capitalize()


def decline_label(reason: str, decline_code: str | None = None) -> str:
    """Describe a decline without exposing evidence text or an internal reason code."""
    match = re.match(r"declined:([^:]+):", reason)
    code = decline_code or (match.group(1) if match else reason)
    labels = {
        "bank": "This looks like a bank, which this tool does not analyse.",
        "insurer": "This looks like an insurer, which this tool does not analyse.",
        "unsupported_language": (
            "This report is mainly in Arabic, which this tool does not analyse."
        ),
    }
    return labels.get(code, "This document was not analysed.")


def hold_label(reason: str) -> str:
    """Describe an analysis hold while keeping industry scores and evidence private."""
    match = re.match(r"industry_uncertain:([^:]+)(?::\s*([^,]+))?", reason)
    if match:
        code, kind = match.groups()
        if kind is None:
            kind = "unknown"
        kind = kind.split("/", maxsplit=1)[0].replace("_", " ")
        suspected = {
            "bank": "a bank",
            "insurer": "an insurer",
            "other financial": "another kind of financial company",
            "unknown": "the company type",
            "corporate": "a company type",
        }.get(kind, "the company type")
        wording = {
            "no_verdict": "could not be identified",
            "other_financial": "could not be confirmed",
            "too_narrow": "could not be confirmed from enough evidence",
            "near_threshold": "could not be confirmed",
            "weak_verdict": "could not be confirmed",
        }.get(code, "could not be confirmed")
        if kind in {"bank", "insurer", "other financial"}:
            return (
                f"The company may be {suspected}, but that {wording}, "
                "so its statements are held for review."
            )
        return f"The company type {wording}, so its statements are held for review."
    return f"Review note — {readable_code(reason)}"


def reason_label(reason: str) -> str:
    """Turn review reason codes into short descriptions while retaining the exact reason."""
    if reason.startswith("industry_uncertain:"):
        return hold_label(reason)
    if reason == "subtotal_failed":
        return "A statement subtotal did not match, so the figures do not reconcile"
    if reason == "identity_failed":
        return FLAG_LABELS[reason]
    if reason == "tie_failed":
        return FLAG_LABELS[reason]
    if reason == "eps_failed":
        return FLAG_LABELS[reason]
    if reason == "duplicate_statement":
        return "A duplicate statement was found, so its figures may be repeated"
    match = re.fullmatch(
        r"(subtotal_not_checked|numbers_missing|unparsed|implausible_magnitude):(\d+)", reason
    )
    if match:
        count = int(match.group(2))
        kind = match.group(1)
        if kind == "subtotal_not_checked":
            return (
                "1 total could not be recalculated from the lines above it, so it is not confirmed"
                if count == 1
                else (
                    f"{count} totals could not be recalculated from the lines above them, "
                    "so they are not confirmed"
                )
            )
        if kind == "numbers_missing":
            return (
                "1 figure was not found where expected"
                if count == 1
                else f"{count} figures were not found where expected"
            )
        if kind == "unparsed":
            return (
                "1 figure could not be read as a number"
                if count == 1
                else f"{count} figures could not be read as numbers"
            )
        return (
            "1 figure has an implausible magnitude"
            if count == 1
            else f"{count} figures have implausible magnitudes"
        )
    if reason == "unchecked":
        return "Some figures were not confirmed by any arithmetic check"
    if reason == "row_alignment_unresolved":
        return "Some rows could not be matched to their figures with confidence"
    if reason.startswith("row_misaligned:"):
        count_text = reason.partition(":")[2]
        rows = "row" if count_text == "1" else "rows"
        return f"{count_text} {rows} may be shifted against their figures"
    if reason.startswith("ambiguous_separator:"):
        count_text = reason.partition(":")[2]
        figures = "figure" if count_text == "1" else "figures"
        return f"{count_text} {figures} use a separator that can be read two ways (comma or dot)"
    if reason == "identity_not_checked:missing_values":
        return (
            "The balance check (assets = liabilities + equity) could not be run because "
            "some figures are missing"
        )
    if reason == "identity_not_checked:identity_totals_not_found":
        return (
            "The balance check (assets = liabilities + equity) could not be run because "
            "key totals were not found"
        )
    # The flag table is the single label source for reasons that share producer codes.
    # Strip only the value-with-box coordinates before dispatch; they are trace data.
    if reason.startswith("critical_item_unmapped:"):
        return f"The line {plain_label(reason.partition(':')[2])} could not be identified"
    label = flag_label(reason)
    if reason.partition(":")[0] in FLAG_LABELS:
        return label
    return f"Review note — {readable_code(reason)}"


@dataclass(frozen=True)
class ReasonView:
    label: str


def review_reasons(reasons: tuple[str, ...]) -> tuple[ReasonView, ...]:
    """Group key-line omissions and order statement review issues by confirmation impact."""
    priority = {"block": 0, "read": 1, "mapping": 2, "note": 3}
    items: list[tuple[int, int, ReasonView]] = []
    critical = [reason for reason in reasons if reason.startswith("critical_item_unmapped:")]
    if critical:
        names = [plain_label(reason.partition(":")[2]) for reason in critical]
        names = [
            name if name.isupper() else name[0].lower() + name[1:] if name else name
            for name in names
        ]
        noun = "line" if len(names) == 1 else "lines"
        verb = "was" if len(names) == 1 else "were"
        label = f"{len(names)} key {noun} {verb} not identified ({', '.join(names)})."
        items.append((priority["mapping"], reasons.index(critical[0]), ReasonView(label)))
    for index, reason in enumerate(reasons):
        if reason.startswith("critical_item_unmapped:"):
            continue
        if reason.startswith(
            ("identity_not_checked:", "subtotal_failed", "subtotal_not_checked:")
        ) or reason in {"identity_failed", "tie_failed", "eps_failed"}:
            group = "block"
        elif reason.startswith(
            (
                "numbers_missing:",
                "unparsed:",
                "implausible_magnitude:",
                "row_misaligned:",
                "ambiguous_separator:",
                "row_alignment_unresolved",
            )
        ):
            group = "read"
        else:
            group = "note"
        items.append((priority[group], index, ReasonView(reason_label(reason))))
    return tuple(item for _, _, item in sorted(items, key=lambda item: (item[0], item[1])))


def source_url(sha256: str, provenance: Provenance, job_id: str | None = None) -> str:
    box: BBox = provenance.bbox
    params = {"box": f"{box.left:.1f},{box.top:.1f},{box.right:.1f},{box.bottom:.1f}"}
    if job_id is not None:
        params["analysis"] = job_id
    query = urlencode(params)
    return f"/documents/{sha256}/pages/{provenance.page_no}?{query}"


def amount(value: Decimal) -> str:
    return format_amount(value)


@dataclass(frozen=True)
class CaveatView:
    id: CaveatId
    currency: str
    evidence: dict[str, str]
    anchor: str
    language: str

    @property
    def text(self) -> str:
        if self.language not in ("en", "ar"):
            raise ValueError(f"unsupported caveat language: {self.language}")
        return caveat_text(self.id, self.currency, "ar" if self.language == "ar" else "en")


@dataclass(frozen=True)
class CellView:
    text: str
    printed: str
    flags: tuple[str, ...]
    source: str | None
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class RowView:
    label: str
    depth: int
    subtotal: bool
    mapped_to: str | None
    mapping_origin: str | None
    mapping_evidence: str | None
    mapping_flag: str | None
    cells: tuple[tuple[CellView, ...], ...]  # per period, newest first; () when none was read


@dataclass(frozen=True)
class StatementView:
    id: str
    title: str
    units: str
    rtl: bool
    language: str
    pages: tuple[int, ...]
    periods: tuple[str, ...]
    rows: tuple[RowView, ...]
    status: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    duplicate: bool
    caveats: tuple[CaveatView, ...]


@dataclass(frozen=True)
class StatementGroup:
    title: str
    statements: tuple[StatementView, ...]


@dataclass(frozen=True)
class InputView:
    canonical_id: str
    label: str
    period: str
    text: str
    source: str
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class MetricView:
    name: str
    anchor: str
    period: str
    value: str | None
    formula: str
    flags: tuple[str, ...]
    caveats: tuple[CaveatId, ...]
    inputs: tuple[InputView, ...] = field(default=())
    notes: tuple[CaveatView, ...] = ()


def statement_view(
    statement: Statement, review: StatementReview, job_id: str | None = None
) -> StatementView:
    """Periods newest first, whatever order the page printed them in, so the English and the
    Arabic edition of a report read the same way (in right-to-left, the newest sits beside the
    label, as Arabic statements print it)."""
    ordered = sorted(statement.periods, key=lambda p: (p.end_date, p.months or 0), reverse=True)
    keys = [p.key for p in ordered]
    caveats = tuple(
        CaveatView(
            id=c.id,
            currency=statement.currency,
            evidence=c.evidence,
            anchor=f"statement-{statement.id}-{c.id}",
            language=statement.language,
        )
        for c in statement.caveats
    )
    rows = []
    for item in statement.line_items:
        by_period: dict[str, list[CellView]] = {}
        for cell in item.cells:
            by_period.setdefault(cell.period_key, []).append(
                CellView(
                    text="" if cell.reported is None else amount(cell.reported),
                    printed=cell.raw_text,
                    flags=tuple(cell.flags),
                    source=source_url(statement.document_sha256, cell.provenance, job_id),
                    caveats=caveats
                    if cell.reported is not None and "percent" not in cell.flags
                    else (),
                )
            )
        # Two cells read for one period are both shown and flagged, never one silently kept.
        cells = tuple(
            tuple(
                replace(c, flags=(*c.flags, DUPLICATE_CELL)) if len(found) > 1 else c for c in found
            )
            for found in (by_period.get(key, []) for key in keys)
        )
        rows.append(
            RowView(
                label=item.raw_label,
                depth=item.depth,
                subtotal=item.is_subtotal,
                mapped_to=item.canonical_id,
                mapping_origin="Model proposed; evidence checked"
                if item.mapping_source == "model"
                else None,
                mapping_evidence=item.mapping_evidence,
                mapping_flag=item.mapping_flag,
                cells=cells,
            )
        )
    scale = SCALE_WORDS[statement.scale]
    return StatementView(
        id=statement.id,
        title=STATEMENT_TITLES[statement.type],
        units=f"{statement.currency} {scale}".strip(),
        rtl=statement.language in RTL_LANGUAGES,
        language=statement.language,
        pages=tuple(statement.source_pages),
        periods=tuple(keys),
        rows=tuple(rows),
        status=review.status,
        reasons=tuple(review.reasons),
        warnings=tuple(review.warnings),
        duplicate="duplicate_statement" in review.reasons,
        caveats=caveats,
    )


def metric_text(metric: MetricValue) -> str | None:
    return format_metric(metric)


def _input_view(
    sha256: str,
    cell: MetricInput,
    caveats: tuple[CaveatView, ...],
    job_id: str | None = None,
) -> InputView:
    scale = SCALE_WORDS[cell.scale]
    return InputView(
        canonical_id=cell.canonical_id,
        label=plain_label(cell.canonical_id),
        period=cell.period_key,
        text=f"{amount(cell.reported)} {cell.currency} {scale}".strip(),
        source=source_url(sha256, cell.provenance, job_id),
        caveats=caveats,
    )


def metric_anchor(metric_id: str, period_key: str) -> str:
    """The id of a metric's row, which a summary's claim links to."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"metric-{metric_id}-{period_key}")


@dataclass(frozen=True)
class SentencePart:
    text: str
    href: str | None
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class SectionView:
    heading: str
    sentences: tuple[tuple[SentencePart, ...], ...]


def sentence_parts(sentence: Sentence, anchors: frozenset[str]) -> tuple[SentencePart, ...]:
    """The sentence with each claimed span a link to the metric it states. Each claim takes
    the first occurrence of its span that no earlier claim took, so the same figure claimed
    twice links each occurrence in turn. A span with no free occurrence, or appearing more
    times in the text than claims name it (so which occurrence is meant is ambiguous), or
    naming a metric the analysis did not compute, is an error: a figure in the summary is
    shown only with its trace."""
    placed = place_claims(sentence)
    for item in placed:
        anchor = metric_anchor(item.claim.metric_id, item.claim.period_key)
        if anchor not in anchors:
            raise ValueError(f"claim {item.claim.text_span!r} names no computed metric ({anchor})")
    parts: list[SentencePart] = []
    cursor = 0
    for item in placed:
        start, end = item.start, item.end
        anchor = metric_anchor(item.claim.metric_id, item.claim.period_key)
        if start > cursor:
            parts.append(SentencePart(sentence.text[cursor:start], None))
        parts.append(SentencePart(sentence.text[start:end], f"#{anchor}"))
        cursor = end
    if cursor < len(sentence.text):
        parts.append(SentencePart(sentence.text[cursor:], None))
    return tuple(parts)


def narrative_view(
    narrative: Narrative,
    anchors: frozenset[str],
    notes: dict[str, tuple[CaveatView, ...]] | None = None,
) -> tuple[SectionView, ...]:
    notes = notes or {}

    def parts(sentence: Sentence) -> tuple[SentencePart, ...]:
        linked = sentence_parts(sentence, anchors)
        footnotes = {
            note.anchor: note
            for claim in sentence.claims
            for note in notes.get(metric_anchor(claim.metric_id, claim.period_key), ())
        }
        return (*linked, SentencePart("", None, tuple(footnotes.values()))) if footnotes else linked

    return tuple(
        SectionView(
            heading=(
                {
                    "overview": "نظرة عامة",
                    "liquidity": "السيولة",
                    "leverage": "المديونية",
                    "profitability": "الربحية",
                    "watch_items": "بنود للمتابعة",
                }[section.heading.value]
                if narrative.language == "ar"
                else section.heading.value.replace("_", " ")
            ),
            sentences=tuple(parts(s) for s in section.sentences),
        )
        for section in narrative.sections
    )


def metric_view(
    sha256: str,
    metric: MetricValue,
    statement_notes: dict[str, tuple[CaveatView, ...]],
    job_id: str | None = None,
) -> MetricView:
    anchor = metric_anchor(metric.metric_id, metric.period_key)
    relevant_notes = {
        (cell.statement_id, note.id): replace(note, anchor=f"{anchor}-caveat-{note.anchor}")
        for cells in metric.inputs.values()
        for cell in cells
        for note in statement_notes.get(cell.statement_id, ())
        if note.id in metric.caveats
    }
    inputs = tuple(
        _input_view(
            sha256,
            cell,
            tuple(
                relevant_notes.get((cell.statement_id, note.id), note)
                for note in statement_notes.get(cell.statement_id, ())
            ),
            job_id,
        )
        for cells in metric.inputs.values()
        for cell in cells
    )
    notes = {
        note.anchor: note for cell in inputs for note in cell.caveats if note.id in metric.caveats
    }
    missing = set(metric.caveats) - {note.id for note in notes.values()}
    if missing:
        raise ValueError(
            f"metric {metric.metric_id}: caveats without source statements: {sorted(missing)}"
        )
    return MetricView(
        name=plain_label(metric.metric_id),
        anchor=anchor,
        period=metric.period_key,
        value=metric_text(metric),
        formula=metric.formula,
        flags=tuple(metric.flags),
        caveats=tuple(metric.caveats),
        inputs=inputs,
        notes=tuple(notes.values()),
    )


@dataclass(frozen=True)
class AnalysisView:
    statements: tuple[StatementView, ...]
    statement_groups: tuple[StatementGroup, ...]
    metrics: tuple[MetricView, ...]
    headline_metrics: tuple[MetricView, ...]
    computed: int
    narrative: tuple[SectionView, ...] | None
    footnotes: tuple[CaveatView, ...]
    summary_notice: str | None
    origin_text: str


def analysis_view(analysis: Analysis) -> AnalysisView:
    """Statements in the order found, each with its review (an error when one is missing);
    metrics computed first, then the ones that could not be, each group in registry order."""
    reviews = {r.statement_id: r for r in analysis.reviews}
    missing = [s.id for s in analysis.statements if s.id not in reviews]
    if missing:
        raise ValueError(f"analysis {analysis.job_id}: statements without a review: {missing}")
    statements = tuple(
        statement_view(s, reviews[s.id], analysis.job_id) for s in analysis.statements
    )
    statement_notes = {s.id: s.caveats for s in statements}
    metrics = [
        metric_view(analysis.upload_sha256, m, statement_notes, analysis.job_id)
        for m in analysis.metrics
    ]
    artifact = analysis.narrative
    ar = analysis.language == "ar"
    summary_notice = None
    origin_text = ""
    if artifact is not None and (
        artifact.language not in ("en", "ar")
        or (analysis.narrative_partial and artifact.origin != "direct_template")
    ):
        artifact = None
        summary_notice = (
            "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
            if ar
            else "Summary withheld because its text or references could not be verified."
        )
    elif artifact is not None and artifact.origin == "direct_template":
        try:
            artifact = validate_summary(analysis, artifact, partial=analysis.narrative_partial)
            origin_text = (
                "قالب مباشر تم التحقق منه مقابل المقاييس المحسوبة."
                if ar
                else "Direct template checked against the computed metrics."
            )
        except GroundingError:
            artifact = None
            summary_notice = (
                "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
                if ar
                else "Summary withheld because its text or references could not be verified."
            )
    elif artifact is not None:
        origin_text = (
            "ملخص محفوظ؛ لم يتم التحقق منه كقالب مباشر."
            if ar
            else "Stored summary; not verified as a direct template."
        )
    elif analysis.narrative_suppression:
        held = analysis.status() is not JobStatus.COMPLETED
        if held:
            summary_notice = (
                "حُجب الملخص: التحليل قيد المراجعة."
                if ar
                else "Summary withheld because the analysis is held for review."
            )
        else:
            summary_notice = (
                "حُجب الملخص: لا تتوفر حقائق مؤهلة من أحدث فترة."
                if ar
                else "Summary withheld because no eligible latest-period facts are available."
            )
    summary_notes = (
        {
            m.anchor: tuple(
                replace(
                    note, anchor=f"summary-{note.id}-{note.currency}", language=artifact.language
                )
                for note in m.notes
            )
            for m in metrics
            if m.value is not None
        }
        if artifact is not None
        else {}
    )
    narrative = None
    if artifact is not None:
        try:
            narrative = narrative_view(
                artifact, frozenset(m.anchor for m in metrics if m.value is not None), summary_notes
            )
        except ValueError:
            summary_notice = (
                "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
                if ar
                else "Summary withheld because its text or references could not be verified."
            )
    footnotes = {
        note.anchor: note
        for section in narrative or ()
        for parts in section.sentences
        for part in parts
        for note in part.caveats
    }
    grouped: dict[str, list[StatementView]] = {}
    for statement in statements:
        grouped.setdefault(statement.title, []).append(statement)
    statement_groups = tuple(
        StatementGroup(title, tuple(group)) for title, group in grouped.items()
    )
    ordered_metrics = tuple(
        [m for m in metrics if m.value is not None] + [m for m in metrics if m.value is None]
    )
    return AnalysisView(
        statements=statements,
        statement_groups=statement_groups,
        metrics=ordered_metrics,
        headline_metrics=tuple(m for m in ordered_metrics if m.value is not None)[:8],
        computed=sum(1 for m in metrics if m.value is not None),
        narrative=narrative,
        footnotes=tuple(footnotes.values()),
        summary_notice=summary_notice,
        origin_text=origin_text,
    )
