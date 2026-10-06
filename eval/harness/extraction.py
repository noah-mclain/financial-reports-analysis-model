"""Score the extraction against the expected files (spec 12, Scoring; 04, Gate G1).

    PYTHONPATH=eval uv run python -m harness.extraction [--ocr ocrmac|tesseract|none]
        [--label NAME] [--fresh]

For each expected statement, its rows are aligned in order with the extracted rows, and every
confirmed expected figure is scored: right when the aligned row holds the same value in that
period and does not sit under another row's label. Where nothing is printed, nothing must be
read. Only files with status ``checked`` count towards G1. A draft starts as a copy of the
extraction, so only the figures confirmed against the page are scored, in a block headed
provisional. Writes var/eval/extraction-golden.json.

``--ocr`` (or FRA_OCR_ENGINE) and ``--label`` try one OCR engine, or one setting of it, without
editing the settings. The run then keeps its artifacts in ``var/artifacts-<label>``, writes
var/eval/extraction-golden-<label>.json (the label is the engine's name unless given), and
adds the bake-off table: per language of the scanned statements, accuracy with its interval,
how many statements were found, and the time per OCR call. ``--fresh`` first deletes that
artifact root, so the times are real work.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from importlib import metadata
from pathlib import Path
from typing import Any, get_args

import yaml

from fra_core.schemas import LineItem, Period, Statement, StatementType
from fra_ingest.config import (
    OCR_ENGINE_ENV,
    REPO_ROOT,
    IngestConfig,
    OcrEngineName,
    load_config,
)
from fra_ingest.converter import docling_version
from fra_ingest.label_match import squash
from fra_ingest.ocr import TESSERACT_COMMAND, make_engine
from fra_ingest.pages import PAGES_STAGE_VERSION, recorded_ocr_calls, sha256_file
from fra_ingest.results import ConvertResult
from fra_ingest.structure import structure_pdf
from harness.expected import (
    EXPECTED_DIR,
    MANIFEST,
    ExpectedFile,
    ExpectedRow,
    ExpectedStatement,
    load_expected,
)
from harness.paths import STRATA
from harness.reproducibility import report_evidence

OUT = REPO_ROOT / "var" / "eval"
# Gate G1 (04-execution-phases.md).
DIGITAL = 0.995
SCANNED = 0.98
PERIODS = 1.0
SIGN = 0.999
METADATA = 1.0
# How alike an expected label and a stretch of the label read must be to count as found.
LABEL_MATCH = 0.85
# Below this, a label read is not this row's label even allowing for OCR noise.
LABEL_READABLE = 0.6
# Characters by which two stretches of a label may differ or overlap and still count as apart.
_SLOP = 2


@dataclass
class Score:
    cells: int = 0
    right: int = 0
    sign_cells: int = 0
    sign_right: int = 0
    periods: int = 0
    periods_right: int = 0
    metadata: int = 0
    metadata_right: int = 0
    unconfirmed: int = 0
    extra_rows: int = 0
    mislabelled: int = 0
    statements: int = 0
    statements_found: int = 0
    cells_missing: int = 0
    wrong: list[dict[str, str]] = field(default_factory=list)

    def __add__(self, other: Score) -> Score:
        return Score(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self.__dataclass_fields__
            }
        )


@dataclass(frozen=True)
class OcrTiming:
    """What one document cost. None means the run did not measure it: the stage came from the
    cache, which holds the cost of an earlier run."""

    read_calls: tuple[float, ...] | None
    convert_seconds: float | None
    convert_pages: int


# The conversion time per page, docling's model load left out, is not OCR time: docling also
# runs layout and table recognition on those pages.
CONVERT_LABEL = "convert, including layout and tables"
WILSON_Z = 1.96


def wilson_interval(right: int, total: int) -> tuple[float, float]:
    """The 95% Wilson score interval of a share; (0, 1) when nothing was scored."""
    if total == 0:
        return 0.0, 1.0
    p = right / total
    z2 = WILSON_Z**2
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = WILSON_Z * math.sqrt(p * (1 - p) / total + z2 / (4 * total**2)) / (1 + z2 / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def convert_timing(converted: ConvertResult) -> tuple[float, int]:
    """Docling's seconds with its model load excluded, and the pages it converted."""
    pages = sum(r.last_page - r.first_page + 1 for r in converted.ranges if r.ocr != "skipped")
    return converted.timings["convert"], pages


def split_cold_start(
    order: Sequence[str], timings: Mapping[str, OcrTiming]
) -> tuple[float | None, dict[str, OcrTiming]]:
    """The first OCR call of the run, which pays for loading the engine, and the timings
    without it. None when no document's calls were measured."""
    rest = dict(timings)
    for document in order:
        timing = rest.get(document)
        if timing is not None and timing.read_calls:
            first, *others = timing.read_calls
            rest[document] = OcrTiming(tuple(others), timing.convert_seconds, timing.convert_pages)
            return first, rest
    return None, rest


def engine_root(artifact_root: Path, label: str) -> Path:
    """Each run keeps its own artifact root, so none reuses another's pages."""
    return artifact_root.with_name(f"{artifact_root.name}-{label}")


def engine_report(label: str) -> Path:
    return OUT / f"extraction-golden-{label}.json"


def remove_artifacts(root: Path) -> None:
    """Delete a run's own artifact root, and nothing else: it must be ``var/artifacts-<label>``."""
    if root.parent != REPO_ROOT / "var" or not root.name.startswith("artifacts-"):
        msg = f"refusing to delete {root}: only {REPO_ROOT / 'var'}/artifacts-<label> may be"
        raise ValueError(msg)
    if root.exists():
        shutil.rmtree(root)


def scanned_breakdown(
    files: Sequence[tuple[ExpectedFile, Mapping[StatementType, Statement]]],
    documents: Mapping[str, Mapping[str, str]],
    timings: Mapping[str, OcrTiming],
) -> list[dict[str, Any]]:
    """The scanned statements per file status and document language: cell accuracy with its
    interval, a value-only accuracy (the same alignment without the label check), how many
    statements were found and how many wrong cells that explains, the documents and issuers
    behind the row, and the time per OCR call and per converted page (None unless every
    document of the row was measured). ``documents`` holds each id's manifest entry."""
    scores: dict[tuple[str, str], Score] = {}
    by_type: dict[tuple[str, str], dict[str, int]] = {}
    members: dict[tuple[str, str], list[str]] = {}
    for expected, statements in files:
        if expected.id not in documents:
            msg = f"{expected.id}: expected file has no entry in the manifest"
            raise ValueError(msg)
        key: tuple[str, str] = (expected.status, documents[expected.id]["language"])
        scanned = [s for s in expected.statements if s.page_mode == "scanned"]
        if scanned:
            members.setdefault(key, []).append(expected.id)
        for s in scanned:
            score = score_statement(s, statements.get(s.type))
            scores[key] = scores.get(key, Score()) + score
            types = by_type.setdefault(key, {})
            types[s.type.value] = types.get(s.type.value, 0) + score.cells
    rows = []
    for key, score in sorted(scores.items()):
        status, language = key
        ids = members[key]
        mine = [timings.get(i) for i in ids]
        calls = [t.read_calls for t in mine if t is not None]
        converted = [t for t in mine if t is not None and t.convert_seconds is not None]
        read_measured = len(calls) == len(ids) and all(c is not None for c in calls)
        n_calls = sum(len(c) for c in calls if c is not None) if read_measured else None
        low, high = wilson_interval(score.right, score.cells)
        rows.append(
            {
                "status": status,
                "language": language,
                "documents": len(ids),
                "issuers": len({documents[i]["issuer"] for i in ids}),
                "not_fully_scanned": [i for i in ids if documents[i]["text_layer"] != "scanned"],
                "cells": score.cells,
                "right": score.right,
                "accuracy": _share(score.right, score.cells),
                "wilson_low": low,
                "wilson_high": high,
                "value_only_accuracy": _share(score.right + score.mislabelled, score.cells),
                "unconfirmed": score.unconfirmed,
                "cells_by_type": by_type[key],
                "statements": score.statements,
                "statements_found": score.statements_found,
                "cells_wrong_statement_missing": score.cells_missing,
                "cells_wrong_other": score.cells - score.right - score.cells_missing,
                "ocr_calls": n_calls,
                "read_seconds_per_call": (
                    _mean([x for c in calls if c is not None for x in c]) if read_measured else None
                ),
                "convert_seconds_per_page": (
                    _per_page(
                        sum(t.convert_seconds or 0.0 for t in converted),
                        sum(t.convert_pages for t in converted),
                    )
                    if len(converted) == len(ids)
                    else None
                ),
            }
        )
    return rows


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _per_page(seconds: float, pages: int) -> float | None:
    return seconds / pages if pages else None


def breakdown_lines(rows: Sequence[Mapping[str, Any]], cold_start: float | None) -> list[str]:
    lines = [
        f"first OCR call of the run (engine load, left out of the times below): "
        f"{_seconds(cold_start)} s"
    ]
    for status in ("checked", "draft"):
        mine = [r for r in rows if r["status"] == status]
        if not mine:
            continue
        lines.append(f"scanned statements by language ({status})")
        for r in mine:
            types = ", ".join(f"{t} {n}" for t, n in sorted(r["cells_by_type"].items()))
            lines += [
                f"  {r['language']:<3} {r['documents']} documents, {r['issuers']} issuers  "
                f"right {r['right']} of {r['cells']}  {100 * r['accuracy']:.2f}% "
                f"(95% interval {100 * r['wilson_low']:.1f} to {100 * r['wilson_high']:.1f})  "
                f"value only {100 * r['value_only_accuracy']:.2f}%  "
                f"unconfirmed, not scored {r['unconfirmed']}",
                f"      cells by statement: {types}",
                f"      statements located {r['statements_found']} of {r['statements']}; "
                f"wrong cells: statement missing {r['cells_wrong_statement_missing']}, "
                f"other {r['cells_wrong_other']}",
                f"      page read: {_count(r['ocr_calls'])} calls, "
                f"{_seconds(r['read_seconds_per_call'])} s per call;  "
                f"{CONVERT_LABEL}: {_seconds(r['convert_seconds_per_page'])} s per page "
                "(docling model load excluded)",
            ]
        loose = sorted({i for r in mine for i in r["not_fully_scanned"]})
        lines.append(
            "  every scanned document is fully scanned: "
            + ("yes" if not loose else "NO, " + ", ".join(loose))
        )
    return lines


def _count(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def tessdata_digests(command: str, languages: Sequence[str]) -> dict[str, str]:
    """The sha256 of each language's traineddata file in the directory Tesseract lists."""
    listing = subprocess.run(
        [command, "--list-langs"], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    found = re.search(r'in "([^"]+)"', listing)
    if found is None:
        msg = f"cannot find the tessdata directory in: {listing.splitlines()[:1]}"
        raise ValueError(msg)
    directory = Path(found.group(1))
    return {code: sha256_file(directory / f"{code}.traineddata") for code in languages}


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def environment_record(config: IngestConfig) -> list[str]:
    """What the numbers were measured with."""
    lines = [
        f"platform: {platform.platform()}",
        f"docling {docling_version()}, ocrmac {_version('ocrmac')}",
    ]
    if config.convert_ocr == "tesseract":
        first = subprocess.run(
            [TESSERACT_COMMAND, "--version"], capture_output=True, text=True, check=True, timeout=30
        )
        version = (first.stdout or first.stderr).splitlines()[0]
        digests = tessdata_digests(TESSERACT_COMMAND, ("ara", "eng"))
        lines += [
            f"tesseract: {version}",
            *(f"  {code}.traineddata sha256 {digest}" for code, digest in digests.items()),
            f"tesseract settings: psm {config.tesseract_psm}, Arabic language "
            f"{config.tesseract_arabic_language}, ocr_dpi {config.ocr_dpi}, "
            f"ocr_scale {config.ocr_scale}",
        ]
    return lines


def _place(label: str, read: str) -> tuple[float, int, int] | None:
    """Where an expected label sits in the label read, both without spaces: how alike the label
    and that stretch are, and the stretch's start and end. None when they share no two
    characters in a row."""
    blocks = [
        b
        for b in SequenceMatcher(None, label, read, autojunk=False).get_matching_blocks()
        if b.size >= 2
    ]
    if not blocks:
        return None
    first, last = blocks[0], blocks[-1]
    start = max(0, first.b - first.a)
    end = min(len(read), last.b + last.size + len(label) - (last.a + last.size))
    alike = SequenceMatcher(None, label, read[start:end], autojunk=False).ratio()
    return alike, start, end


def mislabelled(row: ExpectedRow, read: str, expected: Sequence[ExpectedRow]) -> bool:
    """Whether a figure sits under another row's label.

    Every expected label is looked for inside the label read. Those found (``LABEL_MATCH``
    alike or more) compete, and the one that accounts for the longest stretch of the label read
    is the label it is: "Cash and cash equivalents" read with a typo is that label, not "Cash".
    The figure is mislabelled when the row read has no label, when the label read is another
    row's and the row's own label is not in it, or when its own label is only found inside the
    stretch the other accounts for. A cell holding two rows' labels side by side serves both,
    and so does one holding a heading before the row's label. A label garbled but still
    readable as the row's own (``LABEL_READABLE``) does not make its figure wrong; one that
    reads as nothing of the row's, such as a section heading alone, does.
    """
    own, got = squash(row.label), squash(read)
    if not own:
        return False
    if not got:
        return True
    places = {}
    for label in {squash(r.label) for r in expected} | {own}:
        place = _place(label, got) if label else None
        if place is not None and place[0] >= LABEL_MATCH:
            places[label] = place
    mine = places.get(own) or _place(own, got)
    if mine is None or mine[0] < LABEL_READABLE:
        # Not this row's label by any reading: another row's, or a heading, or nothing.
        return True

    def overlaps(other: tuple[float, int, int]) -> bool:
        return min(mine[2], other[2]) - max(mine[1], other[1]) > _SLOP

    if own not in places:
        # Readable as this row's through noise, unless that stretch is really another's.
        return any(overlaps(place) for place in places.values())
    # The longest stretch wins; between stretches of about one length, the closer match.
    longest = max(end - start for _, start, end in places.values())
    winner = max(
        (label for label, (_, start, end) in places.items() if longest - (end - start) <= _SLOP),
        key=lambda label: (places[label][0], label == own),
    )
    return winner != own and overlaps(places[winner])


def _matches(row: ExpectedRow, item: LineItem) -> bool:
    label = squash(row.label)
    if label and label == squash(item.raw_label):
        return True
    return any(value and item.value_for(key) == value for key, value in row.values.items())


def align_rows(expected: Sequence[ExpectedRow], items: Sequence[LineItem]) -> list[int | None]:
    """For each expected row, the index of its extracted row or None: the longest alignment
    that keeps both in order, two rows matching by label or by a shared non-zero value."""
    rows, cols = len(expected), len(items)
    best = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            if _matches(expected[i], items[j]):
                best[i][j] = best[i + 1][j + 1] + 1
            else:
                best[i][j] = max(best[i + 1][j], best[i][j + 1])
    aligned: list[int | None] = [None] * rows
    i = j = 0
    while i < rows and j < cols:
        if _matches(expected[i], items[j]) and best[i][j] == best[i + 1][j + 1] + 1:
            aligned[i] = j
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return aligned


def _period(period: Period) -> tuple[object, ...]:
    return (period.key, period.end_date, period.kind, period.months)


def score_statement(expected: ExpectedStatement, statement: Statement | None) -> Score:
    score = Score(
        periods=len(expected.periods),
        metadata=2,
        statements=1,
        statements_found=int(statement is not None),
    )
    items = statement.line_items if statement is not None else []
    if statement is not None:
        found = {_period(p) for p in statement.periods}
        score.periods_right = sum(1 for p in expected.periods if _period(p) in found)
        score.metadata_right = (statement.scale == expected.scale) + (
            statement.currency == expected.currency
        )
    aligned = align_rows(expected.rows, items)
    for row, position in zip(expected.rows, aligned, strict=True):
        item = items[position] if position is not None else None
        labelled = item is not None and not mislabelled(row, item.raw_label, expected.rows)
        for key, value in row.values.items():
            if key in row.unconfirmed:
                score.unconfirmed += 1
                continue
            read = item.value_for(key) if item is not None else None
            score.cells += 1
            score.cells_missing += int(statement is None)
            # Nothing printed must read as nothing; a figure must be read, on its own label.
            same = statement is not None and read == value
            if same and (value is None or labelled):
                score.right += 1
            else:
                score.mislabelled += same
                score.wrong.append(
                    {
                        "statement": expected.type.value,
                        "label": row.label,
                        "period": key,
                        "expected": "" if value is None else str(value),
                        "read": "" if read is None else str(read),
                        "label_read": item.raw_label if item is not None else "",
                    }
                )
            if value is not None and read is not None and abs(read) == abs(value):
                score.sign_cells += 1
                score.sign_right += read == value
    used = {p for p in aligned if p is not None}
    score.extra_rows = sum(
        1
        for position, item in enumerate(items)
        if position not in used and any(c.reported is not None for c in item.cells)
    )
    return score


def _share(right: int, total: int) -> float:
    return right / total if total else 1.0


def _mode_line(mode: str, score: Score) -> str:
    return (
        f"  {mode:<7}  cells {score.cells}  right {score.right}  "
        f"{100 * _share(score.right, score.cells):6.2f}%"
    )


def _gate(name: str, right: int, total: int, threshold: float) -> tuple[str, bool]:
    met = _share(right, total) >= threshold
    return (
        f"  {name:<7}  {right} of {total}  {'meets' if met else 'below'} {100 * threshold:.2f}%",
        met,
    )


def report(
    files: Sequence[tuple[ExpectedFile, Mapping[StatementType, Statement]]],
) -> tuple[list[str], int]:
    """The lines to print and the exit code: 1 only when a checked file misses a G1 threshold."""
    by_status: dict[str, dict[str, Score]] = {"checked": {}, "draft": {}}
    for expected, statements in files:
        for s in expected.statements:
            modes = by_status[expected.status]
            modes[s.page_mode] = modes.get(s.page_mode, Score()) + score_statement(
                s, statements.get(s.type)
            )
    checked = sum(1 for f, _ in files if f.status == "checked")
    lines = [f"checked files: {checked}"]
    failed = False
    if not checked:
        lines.append("G1 not measured: no expected file is checked yet")
    else:
        total = Score()
        for mode, threshold in (("digital", DIGITAL), ("scanned", SCANNED)):
            score = by_status["checked"].get(mode)
            if score is None or not score.cells:
                lines.append(f"  {mode:<7}  no cells")
                continue
            total = total + score
            met = _share(score.right, score.cells) >= threshold
            failed = failed or not met
            lines.append(
                _mode_line(mode, score) + f"  {'meets' if met else 'below'} {100 * threshold:.2f}%"
            )
        for name, right, count, threshold in (
            ("periods", total.periods_right, total.periods, PERIODS),
            ("sign", total.sign_right, total.sign_cells, SIGN),
            ("units", total.metadata_right, total.metadata, METADATA),
        ):
            line, met = _gate(name, right, count, threshold)
            failed = failed or not met
            lines.append(line)
        lines.append(f"  unconfirmed {total.unconfirmed}  extra rows {total.extra_rows}")
    if by_status["draft"]:
        lines.append("provisional (drafts, confirmed cells only; not a gate)")
        for mode in ("digital", "scanned"):
            score = by_status["draft"].get(mode)
            if score is not None:
                lines.append(
                    _mode_line(mode, score)
                    + f"  unconfirmed {score.unconfirmed}  extra rows {score.extra_rows}"
                )
    return lines, 1 if failed else 0


def _stamps(out_dir: Path) -> tuple[int | None, int | None]:
    """When the page cache and convert.json were last written, None when absent."""
    pages = out_dir / f"pages.v{PAGES_STAGE_VERSION}.json"
    convert = out_dir / "convert.json"
    return (
        pages.stat().st_mtime_ns if pages.is_file() else None,
        convert.stat().st_mtime_ns if convert.is_file() else None,
    )


def _document_timing(out_dir: Path, before: tuple[int | None, int | None]) -> OcrTiming:
    """Each stage's cost when this run wrote its file, None when it came from the cache."""
    pages_before, convert_before = before
    pages_after, convert_after = _stamps(out_dir)
    calls = recorded_ocr_calls(out_dir) if pages_after != pages_before else None
    if convert_after is None or convert_after == convert_before:
        return OcrTiming(tuple(calls) if calls is not None else None, None, 0)
    converted = ConvertResult.model_validate_json(
        (out_dir / "convert.json").read_text(encoding="utf-8")
    )
    seconds, pages = convert_timing(converted)
    return OcrTiming(tuple(calls) if calls is not None else None, seconds, pages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.extraction")
    parser.add_argument(
        "--ocr",
        choices=get_args(OcrEngineName),
        default=None,
        help="OCR engine for this run, instead of convert.ocr_engine in the settings",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="name of this run: its artifacts and report carry it (default: the engine's name)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="delete this run's own artifact root first, so the times are real work",
    )
    args = parser.parse_args(argv)
    config = load_config(ocr_engine=args.ocr)
    tagged = args.ocr is not None or args.label is not None or bool(os.environ.get(OCR_ENGINE_ENV))
    if args.fresh and not tagged:
        parser.error("--fresh needs --ocr or --label: the default artifacts are never deleted")
    label = args.label or config.convert_ocr
    if tagged:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", label):
            parser.error(f"--label {label!r}: lower-case letters, digits and hyphens only")
        config = config.model_copy(
            update={"artifact_root": engine_root(config.artifact_root, label)}
        )
        if args.fresh:
            remove_artifacts(config.artifact_root)
        engine = make_engine(config)
    else:
        engine = None  # the plain run reads no image pages, as it always has
    entries = {
        d["id"]: d for d in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    }
    files: list[tuple[ExpectedFile, dict[StatementType, Statement]]] = []
    timings: dict[str, OcrTiming] = {}
    rows = []
    document_hashes: dict[str, str] = {}
    expected_hashes: dict[str, str] = {}
    period_kinds: dict[str, str] = {}
    for path in sorted(EXPECTED_DIR.glob("*.json")):
        expected = load_expected(path)
        pdf = MANIFEST.parent / entries[expected.id]["file"]
        document_hashes[expected.id] = sha256_file(pdf)
        expected_hashes[expected.id] = sha256_file(path)
        periods = [p for s in expected.statements for p in s.periods]
        period_kinds[expected.id] = (
            "annual"
            if any(p.is_annual for p in periods)
            else "interim"
            if any(p.months is not None and p.months < 12 for p in periods)
            else "unknown"
        )
        out_dir = config.artifact_root / document_hashes[expected.id]
        before = _stamps(out_dir)
        result = structure_pdf(pdf, config, engine)
        found: dict[StatementType, Statement] = {}
        for extracted in result.statements:
            found.setdefault(extracted.type, extracted)
        files.append((expected, found))
        if tagged:
            timings[expected.id] = _document_timing(out_dir, before)
        for s in expected.statements:
            score = score_statement(s, found.get(s.type))
            rows.append(
                {
                    "id": expected.id,
                    "status": expected.status,
                    "type": s.type.value,
                    "page_mode": s.page_mode,
                    "period_kind": period_kinds[expected.id],
                    **asdict(score),
                }
            )
            print(
                f"{expected.id:34} {expected.status:8} {s.type.value:22} {s.page_mode:8} "
                f"cells {score.cells:3}  right {score.right:3}  unconfirmed {score.unconfirmed:3}"
            )
    lines, code = report(files)
    strata = {}
    for kind in (*STRATA, "unknown", "total"):
        subset = [(f, found) for f, found in files if kind == "total" or period_kinds[f.id] == kind]
        stratum_lines, stratum_code = report(subset)
        strata[kind] = {
            "documents": len(subset),
            "report": stratum_lines,
            "exit_code": stratum_code,
        }
    payload: dict[str, Any] = {
        "statements": rows,
        "run_options": {
            "label": label,
            "tagged": tagged,
            "fresh": args.fresh,
            "page_ocr_enabled": engine is not None,
        },
        "strata": strata,
        "reproducibility": report_evidence(
            config,
            documents=document_hashes,
            expected=expected_hashes,
            inputs={"manifest": sha256_file(MANIFEST)},
        ),
    }
    destination = OUT / "extraction-golden.json"
    if tagged:
        cold, timings = split_cold_start([f.id for f, _ in files], timings)
        breakdown = scanned_breakdown(files, entries, timings)
        lines = [
            f"OCR engine: {config.convert_ocr}, run {label}",
            *environment_record(config),
            *lines,
            *breakdown_lines(breakdown, cold),
        ]
        payload = {
            "engine": config.convert_ocr,
            "label": label,
            "first_ocr_call_seconds": cold,
            **payload,
            "scanned": breakdown,
        }
        destination = engine_report(label)
    print("\n".join(lines))
    OUT.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({**payload, "report": lines}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
