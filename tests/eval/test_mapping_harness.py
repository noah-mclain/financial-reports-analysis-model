"""The label mapping report: slot states, and what an expected file can and cannot confirm."""

from datetime import date
from decimal import Decimal

from harness.expected import ExpectedFile, ExpectedRow, ExpectedStatement
from harness.mapping import Verdict, document_period_kind, slot_state, verdict

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    MappingSource,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex

TAXONOMY = load_taxonomy()
INDEX = LabelIndex(TAXONOMY)
P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(
    n: int,
    label: str,
    value: str,
    canonical: str | None = None,
    source: MappingSource = MappingSource.LEXICON,
    flag: str | None = None,
    evidence: str | None = None,
) -> LineItem:
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value),
        raw_text=value,
        provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
    )
    return LineItem(
        id=f"r{n}",
        raw_label=label,
        cells=[cell],
        canonical_id=canonical,
        mapping_source=source if canonical else None,
        mapping_flag=flag,  # type: ignore[arg-type]
        mapping_evidence=evidence,
    )


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1,
        periods=[P],
        line_items=items,
    )


def expected(*rows: tuple[str, str], kind: StatementType = StatementType.BALANCE) -> ExpectedFile:
    return ExpectedFile(
        id="d",
        sha256="b" * 64,
        status="draft",
        statements=[
            ExpectedStatement(
                type=kind,
                pages=[1],
                page_mode="digital",
                scale=1,
                currency="SAR",
                periods=[P],
                rows=[ExpectedRow(label=label, values={P.key: Decimal(v)}) for label, v in rows],
            )
        ],
    )


def test_a_slot_is_mapped_flagged_or_missing() -> None:
    s = statement(
        [
            item(1, "Total assets", "30", "total_assets"),
            item(2, "x", "5", flag="ambiguous", evidence="alias_total_unconfirmed:total_equity"),
        ]
    )
    assert slot_state(s, "total_assets", TAXONOMY) == "mapped"
    assert slot_state(s, "total_equity", TAXONOMY) == "ambiguous"
    assert slot_state(s, "total_liabilities", TAXONOMY) == "unmapped"
    assert slot_state(None, "total_assets", TAXONOMY) == "statement_not_found"


def test_a_flag_names_an_item_by_its_exact_id_not_by_a_substring() -> None:
    flagged = item(
        1,
        "x",
        "5",
        flag="ambiguous",
        evidence="alias_multiple:net_income_attributable_parent|revenue",
    )
    s = statement([flagged])
    assert slot_state(s, "net_income", TAXONOMY) == "unmapped"
    assert slot_state(s, "net_income_attributable_parent", TAXONOMY) == "ambiguous"


def test_a_mapped_row_the_expected_label_confirms_through_the_lexicon_is_only_consistent() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    found = verdict(s, "total_assets", expected(("Total assets", "30")), INDEX)
    assert found == Verdict("consistent", "")


def test_an_anchored_row_the_expected_label_confirms_is_verified() -> None:
    s = statement([item(1, "", "15", "total_current_assets", MappingSource.ANCHOR)])
    found = verdict(s, "total_current_assets", expected(("Total current assets", "15")), INDEX)
    assert found.kind == "verified"


def test_an_expected_row_of_another_item_is_a_disagreement() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    found = verdict(s, "total_assets", expected(("Total liabilities", "30")), INDEX)
    assert found.kind == "disagrees"
    assert "total_liabilities" in found.reason


def test_an_item_whose_id_extends_another_is_not_confirmed_by_it() -> None:
    s = statement([item(1, "Net profit", "9", "net_income")], StatementType.INCOME)
    found = verdict(
        s,
        "net_income",
        expected(
            ("Profit attributable to shareholders of the parent", "9"), kind=StatementType.INCOME
        ),
        INDEX,
    )
    assert found.kind == "disagrees"


def test_total_assets_and_the_closing_total_are_told_apart_by_position() -> None:
    both = [
        item(1, "Total assets", "30", "total_assets"),
        item(2, "Total equity and liabilities", "30", "total_liabilities_and_equity"),
    ]
    s = statement(both)
    exp = expected(("Total assets", "30"), ("Total equity and liabilities", "30"))
    assert verdict(s, "total_assets", exp, INDEX).kind == "consistent"
    assert verdict(s, "total_liabilities_and_equity", exp, INDEX).kind == "consistent"
    swapped = statement(
        [
            item(1, "Total assets", "30", "total_liabilities_and_equity"),
            item(2, "Total equity and liabilities", "30", "total_assets"),
        ]
    )
    assert verdict(swapped, "total_assets", exp, INDEX).kind == "disagrees"
    assert verdict(swapped, "total_liabilities_and_equity", exp, INDEX).kind == "disagrees"


def test_rows_sharing_figures_that_the_expected_file_does_not_share_give_no_verdict() -> None:
    s = statement(
        [
            item(1, "Total assets", "30", "total_assets"),
            item(2, "Total equity and liabilities", "30", "total_liabilities_and_equity"),
        ]
    )
    found = verdict(s, "total_assets", expected(("Total assets", "30")), INDEX)
    assert found.kind == "no_verdict" and "shared" in found.reason


def test_no_verdict_without_an_expected_file_a_label_or_a_row() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    assert verdict(s, "total_assets", None, INDEX).reason == "no expected file"
    assert verdict(s, "total_assets", expected((" ", "30")), INDEX).reason == "blank expected label"
    nothing = verdict(s, "total_assets", expected(("Total assets", "31")), INDEX)
    assert nothing.kind == "no_verdict" and nothing.reason == "figures on no expected row"
    unknown = verdict(s, "total_assets", expected(("Chairman", "30")), INDEX)
    assert unknown.kind == "no_verdict" and unknown.reason == "expected label not in the lexicon"


def test_the_period_kind_is_read_from_the_statements() -> None:
    annual = Period(key="FY", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
    half = Period(key="H1", end_date=date(2025, 6, 30), kind=PeriodKind.DURATION, months=6)
    base = statement([item(1, "x", "1")])
    assert document_period_kind([base]) == "unknown"
    assert document_period_kind([base.model_copy(update={"periods": [annual]})]) == "annual"
    assert document_period_kind([base.model_copy(update={"periods": [half]})]) == "interim"


# ---- the fit mode: unmapped labels by failure class ------------------------------------------

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from harness.mapping import fit_report, main  # noqa: E402

from fra_ingest.errors import IngestError  # noqa: E402
from fra_ingest.results import StructureResult  # noqa: E402


def structured(*items_: LineItem) -> StructureResult:
    return StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[statement(list(items_))],
    )


def entry(
    doc_id: str, period: str = "annual", language: str = "en", **extra: str
) -> dict[str, str]:
    return {"id": doc_id, "period": period, "language": language, **extra}


def test_fit_report_lists_flagged_rows_by_class_per_language_and_period_kind(
    tmp_path: Path,
) -> None:
    for name in ("a", "b"):
        (tmp_path / f"{name}.pdf").write_bytes(b"x")
    results = {
        "a": structured(
            item(1, "Mystery", "1", flag="unmapped", evidence="no_alias_no_anchor"),
            item(2, "Total assets", "9", canonical="total_assets", evidence="alias"),
        ),
        "b": structured(item(1, "Mystery", "1", flag="unmapped", evidence="no_alias_no_anchor")),
    }
    report = fit_report(
        [entry("a"), entry("b", "interim", "ar")],
        lambda doc_id: tmp_path / f"{doc_id}.pdf",
        lambda path: results[path.stem],
        INDEX,
        TAXONOMY,
    )
    unknown = report["failure_classes"]["classes"]["no_alias_no_anchor"]
    assert unknown["count"] == 2
    assert unknown["by_language"] == {"en": 1, "ar": 1}
    assert unknown["by_period_kind"] == {"annual": 1, "interim": 1}
    assert unknown["top_labels"] == [["mystery", 2]]
    assert report["documents_run"] == 2 and report["missing"] == []
    assert set(report["critical_slots"]) == {"annual", "interim", "total"}


def test_a_document_with_no_pdf_is_counted_and_listed_not_fetched(tmp_path: Path) -> None:
    report = fit_report(
        [entry("gone")],
        lambda doc_id: tmp_path / f"{doc_id}.pdf",
        lambda path: pytest.fail("a missing PDF must not be structured"),
        INDEX,
        TAXONOMY,
    )
    assert report["missing"] == ["gone"] and report["documents_run"] == 0


def test_an_unreadable_document_is_listed_with_its_reason(tmp_path: Path) -> None:
    (tmp_path / "bad.pdf").write_bytes(b"x")

    def fail(path: Path) -> StructureResult:
        raise IngestError("unreadable_pdf", "detail")

    report = fit_report([entry("bad")], lambda d: tmp_path / f"{d}.pdf", fail, INDEX, TAXONOMY)
    assert report["errored"] == [{"id": "bad", "reason": "unreadable_pdf"}]


def test_negative_controls_are_not_mapped_and_are_counted(tmp_path: Path) -> None:
    (tmp_path / "bank.pdf").write_bytes(b"x")
    report = fit_report(
        [entry("bank", role="negative_control")],
        lambda d: tmp_path / f"{d}.pdf",
        lambda path: pytest.fail("a negative control is not mapped"),
        INDEX,
        TAXONOMY,
    )
    assert report["negative_controls_skipped"] == 1


def test_the_limit_takes_the_first_documents_in_id_order(tmp_path: Path) -> None:
    seen: list[str] = []
    for name in ("c", "a", "b"):
        (tmp_path / f"{name}.pdf").write_bytes(b"x")

    def structure(path: Path) -> StructureResult:
        seen.append(path.stem)
        return structured()

    fit_report(
        [entry("c"), entry("a"), entry("b")],
        lambda d: tmp_path / f"{d}.pdf",
        structure,
        INDEX,
        TAXONOMY,
        limit=2,
    )
    assert seen == ["a", "b"]


def test_an_unknown_target_is_refused() -> None:
    with pytest.raises(SystemExit):
        main(["holdout"])


def test_the_fit_run_maps_the_fit_part_only_not_validation_or_the_holdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.mapping as mapping_module
    import yaml
    from harness.holdout_records import LOG_HEADER

    from fra_core.split import Part, hashed_part, issuer_key

    def issuer(part: Part) -> str:
        return next(
            n for n in (f"Issuer {k}" for k in range(300)) if hashed_part(issuer_key(n)) is part
        )

    records = [
        {**entry(i), "issuer": issuer(part), "pool": "train"}
        for i, part in (("f1", Part.FIT), ("v1", Part.VALIDATION), ("h1", Part.HOLDOUT))
    ]
    store = tmp_path / "store"
    (store / "train").mkdir(parents=True)
    for r in records:
        (store / "train" / f"{r['id']}.pdf").write_bytes(b"x")
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"documents": records}), encoding="utf-8")
    (tmp_path / "m.yaml").write_text(yaml.safe_dump({"moves": []}), encoding="utf-8")
    (tmp_path / "l.tsv").write_text(LOG_HEADER, encoding="utf-8")
    seen: list[str] = []

    def fake_structure(pdf: Path, *args: object, **kwargs: object) -> StructureResult:
        seen.append(pdf.stem)
        return structured()

    monkeypatch.setattr(mapping_module, "structure_pdf", fake_structure)
    monkeypatch.setattr(mapping_module, "make_engine", lambda config: None)
    monkeypatch.setattr(mapping_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mapping_module, "OUT", tmp_path / "out")
    mapping_module.run_fit(
        None,
        candidates=tmp_path / "c.yaml",
        moves=tmp_path / "m.yaml",
        log=tmp_path / "l.tsv",
        store=store,
    )
    assert seen == ["f1"]


def test_golden_mapping_run_builds_one_configured_engine_and_reuses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.mapping as mapping_module

    from fra_ingest.config import IngestConfig

    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "documents:\n  - {id: a, file: a.pdf, language: en}\n  - {id: b, file: b.pdf, language: ar}\n",
        encoding="utf-8",
    )
    engine = object()
    factory_configs: list[IngestConfig] = []
    calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []

    def make_engine(actual_config: IngestConfig) -> object:
        factory_configs.append(actual_config)
        return engine

    def structure_pdf(pdf: Path, *args: object, **kwargs: object) -> StructureResult:
        calls.append((pdf, args, kwargs))
        from fra_ingest.errors import IngestError

        raise IngestError("unreadable_pdf", "test")

    expected_dir = tmp_path / "expected"
    expected_dir.mkdir()
    monkeypatch.setattr(mapping_module, "load_config", lambda: config)
    monkeypatch.setattr(mapping_module, "make_engine", make_engine)
    monkeypatch.setattr(mapping_module, "structure_pdf", structure_pdf)
    monkeypatch.setattr(mapping_module, "MANIFEST", manifest)
    monkeypatch.setattr(mapping_module, "EXPECTED_DIR", expected_dir)
    monkeypatch.setattr(mapping_module, "OUT", tmp_path / "out")
    monkeypatch.setattr(mapping_module, "REPO_ROOT", tmp_path)

    assert mapping_module.run_golden() == 1
    assert factory_configs == [config]
    assert [call[1][1] for call in calls] == [engine, engine]
    assert all(call[1][0] is config and call[2] == {"use_cache": False} for call in calls)


@pytest.mark.parametrize("limit", ["0", "-1", "x"])
def test_a_limit_that_is_not_a_positive_number_is_refused(limit: str) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["fit", "--limit", limit])
    assert caught.value.code == 2


def test_genuine_item_finding_is_explicitly_flagged_without_becoming_mapped() -> None:
    from fra_ingest.label_mapping import map_statement

    result = map_statement(statement([]), INDEX, [])
    assert slot_state(result, "total_assets", TAXONOMY) == "explicitly_flagged"
    assert slot_state(result, "inventories", TAXONOMY) == "unmapped"
    assert slot_state(None, "total_assets", TAXONOMY) == "statement_not_found"


def test_mapped_and_named_ambiguous_slots_win_over_item_findings() -> None:
    from fra_ingest.label_mapping import map_statement

    ambiguous = map_statement(statement([item(1, "Total assets", "0")]), INDEX, [])
    assert "total_assets" in {f.item_id for f in ambiguous.mapping_findings}
    assert slot_state(ambiguous, "total_assets", TAXONOMY) == "ambiguous"
    stale = ambiguous.model_copy(
        update={"line_items": [item(1, "Total assets", "0", "total_assets")]}
    )
    assert slot_state(stale, "total_assets", TAXONOMY) == "mapped"


def test_invalid_findings_cannot_make_a_slot_explicitly_flagged() -> None:
    from fra_core.schemas import MappingFinding

    valid = MappingFinding(item_id="total_assets", observed_period_keys=(P.key,))
    bad = [
        {
            "item_id": "total_assets",
            "reason": "critical_item_unmapped",
            "observed_period_keys": [P.key],
        },
        "needs_review",
        valid.model_copy(update={"item_id": "total_equity"}),
        valid.model_copy(update={"observed_period_keys": ()}),
        valid.model_copy(update={"observed_period_keys": ("wrong",)}),
        valid.model_copy(update={"observed_period_keys": (P.key, P.key)}),
        valid.model_copy(update={"reason": "needs_review"}),
    ]
    for finding in bad:
        forged = statement([]).model_copy(update={"mapping_findings": (finding,)})
        assert slot_state(forged, "total_assets", TAXONOMY) == "unmapped"
    duplicates = statement([]).model_copy(update={"mapping_findings": (valid, valid)})
    assert slot_state(duplicates, "total_assets", TAXONOMY) == "unmapped"
    wrong_type = statement([], StatementType.INCOME).model_copy(
        update={"mapping_findings": (valid,)}
    )
    assert slot_state(wrong_type, "total_assets", TAXONOMY) == "unmapped"
    noncritical = valid.model_copy(update={"item_id": "inventories"})
    forged = statement([]).model_copy(update={"mapping_findings": (noncritical,)})
    assert slot_state(forged, "inventories", TAXONOMY) == "unmapped"
    generic = statement([]).model_copy(
        update={"flags": ["needs_review", "critical_item_unmapped:total_assets"]}
    )
    assert slot_state(generic, "total_assets", TAXONOMY) == "unmapped"


def test_a_finding_must_cover_all_observed_periods_but_counts_as_one_slot() -> None:
    from harness.mapping import critical_slots

    from fra_ingest.label_mapping import map_statement

    second = P.model_copy(update={"key": "2024-12-31", "end_date": date(2024, 12, 31)})
    two = statement([]).model_copy(update={"periods": [P, second]})
    result = map_statement(two, INDEX, [])
    counts, slots, disagreements = critical_slots({result.type: result}, None, INDEX, TAXONOMY)
    assert counts["explicitly_flagged"] == len(TAXONOMY.critical_ids(result.type))
    assert len(slots) == sum(
        len(TAXONOMY.critical_ids(k)) for k in (StatementType.BALANCE, StatementType.INCOME)
    )
    assert counts["mapped"] == counts["verified"] == counts["no_verdict"] == 0
    assert not disagreements
    partial = result.mapping_findings[0].model_copy(update={"observed_period_keys": (P.key,)})
    stale = result.model_copy(update={"mapping_findings": (partial,)})
    assert slot_state(stale, partial.item_id, TAXONOMY) == "unmapped"
