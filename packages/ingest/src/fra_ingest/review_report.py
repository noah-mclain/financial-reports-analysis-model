"""Every extracted cell drawn on its page image, with the checks beside it (spec 12).

``fra-ingest review-report <sha256>`` writes ``review.html`` next to the artifacts it reads:
``statements.raw.json``, ``table_checks.json``, ``convert.json`` and the page images. The page
is one file with no dependencies. Boxes are placed by percentages of the page size, so the
report does not depend on the scale the images were rendered at.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from html import escape
from pathlib import Path

from pydantic import TypeAdapter

from fra_core.schemas import Cell, CheckResult, LineItem, Statement
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import load_docling_json
from fra_ingest.errors import IngestError
from fra_ingest.results import ConvertResult, StructureResult
from fra_ingest.review import StatementReview, is_critical

_SHA = re.compile(r"[0-9a-f]{64}")
# Flags that say how a figure was written, not that anything is wrong with it.
_QUIET_FLAGS = frozenset(
    {"arabic_indic_digits", "digits_reversed", "parentheses_negative", "dash_as_zero"}
)
_STYLE = """
body{font:14px/1.4 -apple-system,"Segoe UI",sans-serif;margin:24px;color:#1b1f24;background:#fff}
h1{font-size:20px} h2{font-size:16px;margin-top:32px;border-top:1px solid #d0d7de;padding-top:16px}
table{border-collapse:collapse;font-size:12px} th,td{border:1px solid #d0d7de;padding:3px 6px;
vertical-align:top;text-align:left} td.num{text-align:right;font-variant-numeric:tabular-nums;
white-space:nowrap} .statement{display:grid;grid-template-columns:minmax(360px,1fr) minmax(480px,
1fr);gap:16px;align-items:start} .page{position:relative;border:1px solid #d0d7de;
margin-bottom:12px} .page img{display:block;width:100%} .box{position:absolute;
box-sizing:border-box;border:1.5px solid;border-radius:2px} .checked{border-color:#1a7f37;
background:rgba(26,127,55,.12)} .unchecked{border-color:#6e7781;background:rgba(110,119,129,.10)}
.flagged{border-color:#bf8700;background:rgba(191,135,0,.20)} .failed{border-color:#cf222e;
background:rgba(207,34,46,.20)} .box.synthesized{border-style:dashed} .box.moved{outline:2px
solid #8250df;outline-offset:1px} td.checked{background:rgba(26,127,55,.10)}
td.flagged{background:rgba(191,135,0,.18)} td.failed{background:rgba(207,34,46,.16)}
.hot{box-shadow:0 0 0 3px #0969da} tr.hot td{box-shadow:inset 0 0 0 9999px rgba(9,105,218,.10)}
.flags{color:#6e7781;font-size:10px;display:block} .passed{color:#1a7f37;font-weight:600}
.needs_review{color:#cf222e;font-weight:600} .legend span{display:inline-block;padding:1px 8px;
margin-right:8px;border:1.5px solid} .missing{color:#6e7781;font-style:italic}
"""
_SCRIPT = """
document.querySelectorAll('[data-row]').forEach(function (element) {
  function mark(on) {
    document.querySelectorAll('[data-row="' + element.dataset.row + '"]').forEach(function (e) {
      e.classList.toggle('hot', on);
    });
  }
  element.addEventListener('mouseenter', function () { mark(true); });
  element.addEventListener('mouseleave', function () { mark(false); });
});
"""


def _total_id(check: CheckResult) -> str | None:
    if not check.line_item_ids:
        return None
    return check.line_item_ids[0] if check.kind == "balance_identity" else check.line_item_ids[-1]


def cell_class(cell: Cell, item_id: str, checks: Sequence[CheckResult]) -> str:
    """failed, flagged, checked or unchecked: the first that applies."""
    mine = [c for c in checks if item_id in c.line_item_ids and c.period_key == cell.period_key]
    if any(c.status == "fail" for c in mine):
        return "failed"
    if is_critical(cell):
        return "flagged"
    return "checked" if any(c.status == "pass" for c in mine) else "unchecked"


def _percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def _box(
    cell: Cell, item: LineItem, size: tuple[float, float], checks: Sequence[CheckResult]
) -> str:
    width, height = size
    box = cell.provenance.bbox
    left = _percent(100 * box.left / width)
    top = _percent(100 * box.top / height)
    wide = min(100.0 - left, _percent(100 * box.width / width))
    tall = min(100.0 - top, _percent(100 * box.height / height))
    classes = [cell_class(cell, item.id, checks)]
    if "bbox_synthesized" in cell.flags:
        classes.append("synthesized")
    if "row_realigned" in cell.flags:
        classes.append("moved")
    title = " | ".join(
        [item.id, item.raw_label, cell.period_key, cell.raw_text, ", ".join(cell.flags)]
    )
    return (
        f'<div class="box {" ".join(classes)}" data-row="{escape(item.id)}" '
        f'title="{escape(title)}" '
        f'style="left:{left:.2f}%;top:{top:.2f}%;width:{wide:.2f}%;height:{tall:.2f}%"></div>'
    )


def _number(value: Decimal | None) -> str:
    return "&empty;" if value is None else f"{value:,}"


def _value_cell(cell: Cell | None, item: LineItem, checks: Sequence[CheckResult]) -> str:
    if cell is None:
        return "<td></td>"
    flags = [f for f in cell.flags if f not in _QUIET_FLAGS]
    shown = f'<span class="flags">{escape(", ".join(flags))}</span>' if flags else ""
    return (
        f'<td class="num {cell_class(cell, item.id, checks)}" title="{escape(cell.raw_text)}">'
        f"{_number(cell.reported)}{shown}</td>"
    )


def _row_checks(item: LineItem, checks: Sequence[CheckResult]) -> str:
    lines = []
    for check in checks:
        if _total_id(check) != item.id:
            continue
        parts = [check.kind, check.period_key, check.status]
        if check.expected is not None and check.actual is not None:
            parts.append(f"expected {check.expected:,}, read {check.actual:,}")
        if check.difference:
            parts.append(f"difference {check.difference:,}")
        if check.detail:
            parts.append(check.detail)
        if len(check.line_item_ids) > 1:
            parts.append("rows " + " ".join(i.rsplit("-", 1)[-1] for i in check.line_item_ids))
        state = {"pass": "checked", "fail": "failed"}.get(check.status, "unchecked")
        lines.append(f'<div class="{state}">{escape(" · ".join(parts))}</div>')
    return "".join(lines)


def _rows_table(statement: Statement, checks: Sequence[CheckResult]) -> str:
    head = "".join(f"<th>{escape(p.key)}</th>" for p in statement.periods)
    rows = []
    for item in statement.line_items:
        cells = {c.period_key: c for c in item.cells}
        values = "".join(_value_cell(cells.get(p.key), item, checks) for p in statement.periods)
        mark = "total" if item.is_subtotal else ""
        rows.append(
            f'<tr data-row="{escape(item.id)}"><td>{escape(item.id)}</td><td>{mark}</td>'
            f'<td dir="auto" style="padding-left:{6 + 12 * item.depth}px">'
            f"{escape(item.raw_label)}</td><td>{escape(item.note_ref or '')}</td>{values}"
            f"<td>{_row_checks(item, checks)}</td></tr>"
        )
    return (
        f"<table><tr><th>row</th><th></th><th>label</th><th>note</th>{head}<th>checks</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _pages(
    statement: Statement,
    checks: Sequence[CheckResult],
    page_sizes: Mapping[int, tuple[float, float]],
    page_images: Mapping[int, str],
) -> str:
    parts = []
    for page_no in statement.source_pages:
        size, image = page_sizes.get(page_no), page_images.get(page_no)
        if size is None or image is None:
            parts.append(f'<p class="missing">page {page_no}: page image missing</p>')
            continue
        boxes = "".join(
            _box(cell, item, size, checks)
            for item in statement.line_items
            for cell in item.cells
            if cell.provenance.page_no == page_no
        )
        parts.append(
            f'<div class="page"><img src="{escape(image)}" alt="page {page_no}"/>{boxes}</div>'
        )
    return "".join(parts)


def _review_line(review: StatementReview | None) -> str:
    if review is None:
        return ""
    reasons = escape(", ".join(review.reasons))
    warnings = escape(", ".join(review.warnings))
    return (
        f'<span class="{review.status}">{review.status}</span> {reasons} '
        f"&middot; {review.checked_cells} of {review.numeric_cells} cells in a passing check"
        + (f" &middot; warnings: {warnings}" if warnings else "")
    )


def _caveats(statement: Statement) -> str:
    """The assumptions the statement's amounts rest on, with their evidence."""
    lines = [
        f"{c.id} ({', '.join(f'{k} {v}' for k, v in sorted(c.evidence.items()))})"
        for c in statement.caveats
    ]
    return f'<p class="caveats">caveats: {escape("; ".join(lines))}</p>' if lines else ""


def _summary(result: StructureResult, reviews: Mapping[str, StatementReview]) -> str:
    rows = []
    for s in result.statements:
        review = reviews.get(s.id)
        pages = f"{s.source_pages[0]}-{s.source_pages[-1]}" if s.source_pages else ""
        rows.append(
            f'<tr><td><a href="#{escape(s.id)}">{escape(s.type.value)}</a></td><td>{pages}</td>'
            f"<td>{escape(', '.join(p.key for p in s.periods))}</td>"
            f"<td>{escape(s.currency)} x{s.scale}</td>"
            f"<td>{_review_line(review)}</td><td>{escape(', '.join(s.flags))}</td></tr>"
        )
    return (
        "<table><tr><th>statement</th><th>pages</th><th>periods</th><th>unit</th>"
        f"<th>review</th><th>flags</th></tr>{''.join(rows)}</table>"
    )


def _tables(result: StructureResult) -> str:
    rows = "".join(
        f"<tr><td>{t.page_no}</td><td>{escape(t.table_ref)}</td>"
        f"<td>{escape(t.type.value if t.type else 'not a statement')}</td>"
        f"<td>{t.confidence:.2f}</td><td>{escape(t.statement_id or '')}</td>"
        f"<td>{escape(', '.join(t.evidence))}</td></tr>"
        for t in result.tables
    )
    return (
        "<table><tr><th>page</th><th>table</th><th>decision</th><th>confidence</th>"
        f"<th>statement</th><th>evidence</th></tr>{rows}</table>"
    )


def render_report(
    result: StructureResult,
    checks: Sequence[CheckResult],
    page_sizes: Mapping[int, tuple[float, float]],
    page_images: Mapping[int, str],
    title: str,
) -> str:
    reviews = {r.statement_id: r for r in result.reviews}
    sections = []
    for s in result.statements:
        own = [c for c in checks if c.statement_id == s.id]
        ids = {i.id for i in s.line_items}
        # A tie is recorded on one statement and names a row of the other.
        own += [c for c in checks if c.statement_id != s.id and ids & set(c.line_item_ids)]
        sections.append(
            f'<h2 id="{escape(s.id)}">{escape(s.type.value)} &middot; {escape(s.id)}</h2>'
            f"<p>{_review_line(reviews.get(s.id))}</p>{_caveats(s)}"
            f'<div class="statement"><div>{_pages(s, own, page_sizes, page_images)}</div>'
            f"<div>{_rows_table(s, own)}</div></div>"
        )
    flags = escape(", ".join(result.flags)) or "none"
    return (
        '<!doctype html><html><head><meta charset="utf-8"/>'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>"
        f"<h1>{escape(title)}</h1>"
        f"<p>{escape(result.sha256)} &middot; structure {escape(result.version)} "
        f"&middot; document flags: {flags}</p>"
        '<p class="legend"><span class="checked">in a passing check</span>'
        '<span class="unchecked">read, no check</span><span class="flagged">flagged</span>'
        '<span class="failed">in a failed check</span>dashed: no figure was read here '
        "&middot; purple outline: moved onto its label</p>"
        f"{_summary(result, reviews)}{''.join(sections)}"
        f"<h2>Tables</h2>{_tables(result)}<script>{_SCRIPT}</script></body></html>"
    )


def resolve_artifacts(root: Path, sha256: str) -> Path:
    """The artifact directory for a full hash or a unique prefix of one."""
    found = (
        sorted(
            p
            for p in root.iterdir()
            if p.is_dir() and _SHA.fullmatch(p.name) and p.name.startswith(sha256)
        )
        if sha256 and root.is_dir()
        else []
    )
    if not found:
        raise IngestError("unknown_document", sha256)
    if len(found) > 1:
        raise IngestError("ambiguous_document", ", ".join(p.name[:16] for p in found))
    return found[0]


def write_review_report(sha256: str, config: IngestConfig) -> Path:
    out = resolve_artifacts(config.artifact_root, sha256)

    def read(name: str) -> str:
        path = out / name
        if not path.is_file():
            raise IngestError("artifact_missing", name)
        return path.read_text(encoding="utf-8")

    result = StructureResult.model_validate_json(read("statements.raw.json"))
    checks = TypeAdapter(list[CheckResult]).validate_json(read("table_checks.json"))
    convert = ConvertResult.model_validate_json(read("convert.json"))

    pages = {page for s in result.statements for page in s.source_pages}
    paths = {t.docling_path for t in result.tables}
    paths |= {r.docling_path for r in convert.ranges if r.docling_path}
    sizes: dict[int, tuple[float, float]] = {}
    for relative in sorted(paths):
        if not (out / relative).is_file():
            continue
        document = load_docling_json(out / relative)
        for page in pages:
            if str(page) in document.pages:
                size = document.page_size(page)
                sizes[page] = (size.width, size.height)
    images = {n: path for n, path in convert.page_images.items() if (out / path).is_file()}

    entity = next((s.entity_name for s in result.statements if s.entity_name), None)
    title = f"Review: {entity or result.sha256[:12]}"
    target = out / "review.html"
    temporary = target.with_suffix(".html.tmp")
    temporary.write_text(render_report(result, checks, sizes, images, title), encoding="utf-8")
    os.replace(temporary, target)
    return target
