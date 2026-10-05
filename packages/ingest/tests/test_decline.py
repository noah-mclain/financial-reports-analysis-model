"""Banks and insurers are declined with a reason; an uncertain verdict is held for review."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from support import located
from test_review import ROWS, statement
from test_structure import document
from test_structure import inputs as structure_inputs

from fra_core.schemas import PageMode
from fra_ingest import cli, structure
from fra_ingest.config import IngestConfig, load_config
from fra_ingest.errors import IngestError
from fra_ingest.industry import (
    UNCERTAIN_MARGIN,
    VERDICT_THRESHOLD,
    WHOLE_DOCUMENT_MIN_CUES,
    industry_decision,
)
from fra_ingest.pages import sha256_file
from fra_ingest.results import (
    ConvertResult,
    IndustryKind,
    IndustrySignal,
    LocateResult,
    StructureResult,
)
from fra_ingest.review import StatementReview, hold_for_industry
from fra_ingest.review_report import write_review_report

SHA = "d" * 64
EVIDENCE = [(3, "total deposits"), (4, "loans and advances to customers")]
STRONG = VERDICT_THRESHOLD + UNCERTAIN_MARGIN  # the lowest score a decline can rest on


def signal(
    kind: IndustryKind, score: float, cues: int = WHOLE_DOCUMENT_MIN_CUES, **extra: object
) -> IndustrySignal:
    return IndustrySignal.model_validate(
        {"kind": kind, "score": score, "distinct_cues": cues, "evidence": EVIDENCE, **extra}
    )


def with_industry(industry: IndustrySignal) -> LocateResult:
    return located([PageMode.TEXT] * 3, [(2, 3)], sha256=SHA).model_copy(
        update={"industry": industry}
    )


@pytest.mark.parametrize("kind", ["bank", "insurer"])
def test_a_strong_bank_or_insurer_is_declined_with_kind_score_and_evidence(
    kind: IndustryKind,
) -> None:
    decision = industry_decision(signal(kind, 14.0, cues=5))
    assert decision is not None
    assert (decision.outcome, decision.code) == ("declined", kind)
    assert decision.signal == signal(kind, 14.0, cues=5)
    assert f"declined:{kind}: {kind}, score 14.0" in decision.reason
    assert "page 3 'total deposits'" in decision.reason
    assert "page 4 'loans and advances to customers'" in decision.reason


@pytest.mark.parametrize("kind", ["bank", "insurer"])
def test_a_verdict_at_the_lowest_strong_score_and_cue_count_is_still_declined(
    kind: IndustryKind,
) -> None:
    decision = industry_decision(signal(kind, STRONG, cues=WHOLE_DOCUMENT_MIN_CUES))
    assert decision is not None and decision.outcome == "declined"


@pytest.mark.parametrize("kind", ["bank", "insurer"])
@pytest.mark.parametrize(
    ("score", "cues"),
    [
        (VERDICT_THRESHOLD, 1),  # one cue on two pages: a utility's "deposits from customers"
        (STRONG - 0.5, WHOLE_DOCUMENT_MIN_CUES),  # enough cues, too low a score
        (20.0, WHOLE_DOCUMENT_MIN_CUES - 1),  # a high score on a narrow vocabulary
    ],
)
def test_a_weak_bank_or_insurer_verdict_is_held_not_declined(
    kind: IndustryKind, score: float, cues: int
) -> None:
    decision = industry_decision(signal(kind, score, cues))
    assert decision is not None
    assert (decision.outcome, decision.code) == ("needs_review", "weak_verdict")
    assert f"industry_uncertain:weak_verdict: {kind}" in decision.reason


def test_a_corporate_document_is_not_declined_or_held() -> None:
    assert industry_decision(signal("corporate", 0.0, evidence=[])) is None
    assert industry_decision(IndustrySignal(kind="corporate")) is None


@pytest.mark.parametrize(
    ("industry", "code"),
    [
        (IndustrySignal(kind="unknown"), "no_verdict"),
        (signal("other_financial", 9.0, subkind="brokerage"), "other_financial"),
        (signal("corporate", VERDICT_THRESHOLD - UNCERTAIN_MARGIN), "near_threshold"),
        (signal("corporate", VERDICT_THRESHOLD - 0.5), "near_threshold"),
        (signal("corporate", VERDICT_THRESHOLD + 3.0), "too_narrow"),
    ],
)
def test_an_uncertain_verdict_is_held_for_review_not_declined(
    industry: IndustrySignal, code: str
) -> None:
    decision = industry_decision(industry)
    assert decision is not None
    assert decision.outcome == "needs_review"
    assert decision.code == code
    assert f"industry_uncertain:{code}: " in decision.reason


def test_the_reason_names_the_sub_kind() -> None:
    decision = industry_decision(signal("other_financial", 9.0, subkind="brokerage"))
    assert decision is not None
    assert "other_financial/brokerage, score 9.0" in decision.reason


def test_a_score_just_below_the_margin_is_corporate() -> None:
    assert (
        industry_decision(signal("corporate", VERDICT_THRESHOLD - UNCERTAIN_MARGIN - 0.5)) is None
    )


def refuse(*_args: object, **_kwargs: object) -> ConvertResult:
    raise AssertionError("convert stage reached for a declined document")


def _stub_locate(monkeypatch: pytest.MonkeyPatch, industry: IndustrySignal) -> None:
    monkeypatch.setattr(structure, "load_or_locate", lambda *_a: with_industry(industry))
    monkeypatch.setattr(structure, "current_convert", refuse)
    monkeypatch.setattr(structure, "read_pages", lambda *_a, **_k: [])


@pytest.mark.parametrize("kind", ["bank", "insurer"])
def test_a_declined_document_ends_without_statements_and_never_converts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: IndustryKind
) -> None:
    _stub_locate(monkeypatch, signal(kind, 14.0, cues=5))
    config = IngestConfig(artifact_root=tmp_path)
    result = structure.structure_pdf(Path("doc.pdf"), config, None, convert=refuse)
    assert result.statements == [] and result.tables == [] and result.reviews == []
    assert result.industry is not None
    assert result.industry.outcome == "declined"
    assert result.industry.signal.kind == kind
    assert result.flags == [f"declined:{kind}"]
    out = tmp_path / SHA
    stored = StructureResult.model_validate_json((out / "statements.raw.json").read_text("utf-8"))
    assert stored == result
    assert json.loads((out / "table_checks.json").read_text("utf-8")) == []


def test_a_decline_replaces_stored_statements_from_an_earlier_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / SHA
    out.mkdir()
    (out / "statements.raw.json").write_text("stale", encoding="utf-8")
    _stub_locate(monkeypatch, signal("bank", 14.0, cues=5))
    structure.structure_pdf(Path("doc.pdf"), IngestConfig(artifact_root=tmp_path), None)
    assert "declined:bank" in (out / "statements.raw.json").read_text("utf-8")


def test_a_corporate_document_goes_on_to_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        structure, "load_or_locate", lambda *_a: with_industry(signal("corporate", 0.0))
    )
    reached: list[str] = []

    def stop(*_args: object, **_kwargs: object) -> ConvertResult:
        reached.append("convert")
        raise RuntimeError("stop here")

    monkeypatch.setattr(structure, "current_convert", stop)
    with pytest.raises(RuntimeError):
        structure.structure_pdf(Path("doc.pdf"), IngestConfig(artifact_root=tmp_path), None)
    assert reached == ["convert"]


def test_an_uncertain_document_goes_on_to_convert_and_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_locate(monkeypatch, IndustrySignal(kind="unknown"))
    empty = ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="2",
        docling_version="1",
        device="cpu",
        settings_hash="h",
        ranges=[],
    )
    monkeypatch.setattr(structure, "current_convert", lambda *_a: empty)
    result = structure.structure_pdf(Path("doc.pdf"), IngestConfig(artifact_root=tmp_path), None)
    assert result.industry is not None
    assert result.industry.outcome == "needs_review"
    assert result.industry.code == "no_verdict"


def test_an_uncertain_verdict_holds_every_statement_with_its_reason() -> None:
    held, review = hold_for_industry(
        statement(ROWS),
        StatementReview(
            statement_id="s", status="passed", numeric_cells=4, checked_cells=4, flagged_cells=0
        ),
        "near_threshold",
    )
    assert "needs_review" in held.flags
    assert review.status == "needs_review"
    assert review.reasons == ["industry_uncertain:near_threshold"]


def test_a_statement_already_held_keeps_its_reasons_and_flag_once() -> None:
    base = statement(ROWS, flags=("needs_review",))
    review = StatementReview(
        statement_id="s",
        status="needs_review",
        reasons=["unchecked"],
        numeric_cells=4,
        checked_cells=0,
        flagged_cells=0,
    )
    held, held_review = hold_for_industry(base, review, "no_verdict")
    assert held.flags.count("needs_review") == 1
    assert held_review.reasons == ["unchecked", "industry_uncertain:no_verdict"]


def test_the_cli_reports_a_decline_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    decision = industry_decision(signal("bank", 14.0, cues=5))
    assert decision is not None

    def fake(*_args: object, **_kwargs: object) -> StructureResult:
        return StructureResult(
            version="1",
            sha256="c" * 64,
            convert_version="",
            settings_hash="",
            flags=["declined:bank"],
            industry=decision,
        )

    monkeypatch.setattr(cli, "structure_pdf", fake)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"x")
    assert main_structure(pdf, tmp_path) == cli.EXIT_DECLINED
    captured = capsys.readouterr()
    assert "declined:bank" in captured.err
    assert "page 3 'total deposits'" in captured.err
    assert "Traceback" not in captured.err


def main_structure(pdf: Path, tmp_path: Path) -> int:
    return cli.main(["structure", str(pdf), "--no-ocr", "--artifacts", str(tmp_path / "a")])


@pytest.mark.golden
def test_no_golden_document_is_declined_or_held(golden: Callable[[str], Path]) -> None:
    config = load_config()
    documents = sorted(Path(golden("almarai-2025-en-annualreport.pdf")).parent.glob("*.pdf"))
    assert documents
    seen = 0
    for pdf in documents:
        stored = config.artifact_root / sha256_file(pdf) / "locate.json"
        if not stored.is_file():
            continue  # run make eval-locate first
        verdict = LocateResult.model_validate_json(stored.read_text("utf-8")).industry
        assert industry_decision(verdict) is None, pdf.name
        seen += 1
    if not seen:
        pytest.skip("run make eval-locate first")


def _structure_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, industry: IndustrySignal
) -> StructureResult:
    """structure_pdf over a converted two-page balance sheet, with the locator's verdict stubbed."""
    _stub_locate(monkeypatch, industry)
    base = structure_inputs([("docling/p1-2.json", document())])
    monkeypatch.setattr(structure, "current_convert", lambda *_a: base.convert)
    monkeypatch.setattr(structure, "load_docling_json", lambda _path: document())
    return structure.structure_pdf(Path("doc.pdf"), IngestConfig(artifact_root=tmp_path), None)


def test_structure_pdf_holds_every_statement_of_a_near_threshold_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _structure_held(tmp_path, monkeypatch, signal("corporate", VERDICT_THRESHOLD - 0.5))
    assert result.statements and len(result.reviews) == len(result.statements)
    assert all(
        r.status == "needs_review" and "industry_uncertain:near_threshold" in r.reasons
        for r in result.reviews
    )
    assert "industry_uncertain:near_threshold" in result.flags
    assert result.industry is not None and result.industry.code == "near_threshold"


def test_structure_pdf_leaves_a_corporate_document_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _structure_held(tmp_path, monkeypatch, signal("corporate", 0.0, evidence=[]))
    assert result.statements
    assert not any("industry_uncertain" in reason for r in result.reviews for reason in r.reasons)
    assert not any(flag.startswith("industry_uncertain") for flag in result.flags)
    assert result.industry is None


def _declined_artifacts(root: Path) -> Path:
    """A declined document's artifacts beside a convert.json left by an earlier run."""
    decision = industry_decision(signal("bank", 14.0, cues=5))
    assert decision is not None
    out = root / SHA
    out.mkdir()
    declined = StructureResult(
        version="1",
        sha256=SHA,
        convert_version="",
        settings_hash="",
        flags=["declined:bank"],
        industry=decision,
    )
    (out / "statements.raw.json").write_text(declined.model_dump_json(), encoding="utf-8")
    (out / "table_checks.json").write_text("[]", encoding="utf-8")
    (out / "convert.json").write_text("stale", encoding="utf-8")
    return out


def test_review_report_of_a_declined_document_gives_the_decline_and_its_reason(
    tmp_path: Path,
) -> None:
    _declined_artifacts(tmp_path)
    with pytest.raises(IngestError) as declined:
        write_review_report(SHA, IngestConfig(artifact_root=tmp_path))
    assert declined.value.reason == "declined"
    assert "declined:bank" in declined.value.detail
    assert "page 3 'total deposits'" in declined.value.detail
    assert not (tmp_path / SHA / "review.html").exists()


def test_the_cli_review_report_of_a_declined_document_exits_as_structure_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _declined_artifacts(tmp_path)
    assert cli.main(["review-report", SHA[:8], "--artifacts", str(tmp_path)]) == cli.EXIT_DECLINED
    err = capsys.readouterr().err
    assert "declined:bank" in err and "Traceback" not in err
