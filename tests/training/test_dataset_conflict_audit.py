"""Fit-only conflict attribution and projection checks use synthetic rows only."""

import hashlib
import json
from pathlib import Path

import dataset_conflict_audit as audit_module
import pytest
from dataset_conflict_audit import DatasetError, audit_rows, main, write_report
from harness.holdout_records import LOG_HEADER
from test_dataset_contracts import cik_in, source
from test_dataset_contracts import example as contract_example

from fra_core.pools import Pool
from fra_core.split import Part


def train(label: str, answer: str | None, statement: str = "income") -> dict[str, object]:
    return {
        "messages": [
            {"role": "system", "content": audit_module.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"label": label, "statement": statement}, separators=(",", ":")
                ),
            },
            {
                "role": "assistant",
                "content": json.dumps({"canonical_id": answer}, separators=(",", ":")),
            },
        ]
    }


def example(source: str, label: str, answer: str | None, **fields: object) -> dict[str, object]:
    return {
        "source_id": source,
        "pool": "train",
        "statement": "income",
        "label": label,
        "canonical_id": answer,
        "abstention_reason": "unmapped" if answer is None else None,
        "role": "corporate",
        "tag": "us-gaap:Other",
        **fields,
    }


def test_mixed_group_has_disjoint_null_buckets_and_exact_keys() -> None:
    rows = [train("A", "revenue"), train("A", None), train("A", None), train("A", None)]
    examples_ = [
        example("s1", "A", "revenue"),
        example("s2", "A", None, role="negative_control"),
        example("s3", "A", None, tag="custom:Other"),
        example("s4", "A", None, abstention_reason="unsupported_tag"),
    ]
    report = audit_rows(rows, examples_, fit_source_ids={"s1", "s2", "s3", "s4"})
    key = report["conflicts"][0]
    assert key["mapped_by_canonical_id"] == {"revenue": 1}
    assert key["buckets"] == {
        "mapped": 1,
        "negative_control_role": 1,
        "custom_tag": 1,
        "standard_tag_omission": 1,
    }


def test_unmatched_train_target_is_unresolved_provenance() -> None:
    report = audit_rows(
        [train("A", "revenue"), train("A", None)],
        [example("s1", "A", "revenue")],
        fit_source_ids={"s1"},
    )
    assert report["conflicts"][0]["buckets"]["unresolved_provenance"] == 1
    assert report["upstream_row_counts_reconcile"] is False
    assert report["unattributed_train_rows"] == 1


def test_partial_mixed_provenance_is_unresolved_and_order_independent() -> None:
    rows = [train("A", None), train("A", "revenue")]
    upstream = [
        example("s1", "A", None, tag="us-gaap:Cash"),
        example("s2", "A", None, tag="custom:Other"),
        example("s3", "A", "revenue"),
    ]
    first = audit_rows(rows, upstream, fit_source_ids={"s1", "s2", "s3"})
    second = audit_rows(rows, list(reversed(upstream)), fit_source_ids={"s1", "s2", "s3"})

    assert first == second
    assert first["conflicts"][0]["buckets"]["unresolved_provenance"] == 1
    assert first["projections"]["R1"]["removed_rows"] == 0
    assert first["ambiguous_provenance_train_rows"] == 1


def test_fully_matched_mixed_provenance_and_partial_uniform_provenance() -> None:
    full = audit_rows(
        [train("A", None), train("A", None), train("A", "revenue")],
        [
            example("s1", "A", None, tag="custom:One"),
            example("s2", "A", None, role="negative_control"),
            example("s3", "A", "revenue"),
        ],
        fit_source_ids={"s1", "s2", "s3"},
    )
    uniform = audit_rows(
        [train("B", None), train("B", "revenue")],
        [
            example("s4", "B", None, tag="custom:One"),
            example("s5", "B", None, tag="custom:Two"),
            example("s6", "B", "revenue"),
        ],
        fit_source_ids={"s4", "s5", "s6"},
    )
    assert full["conflicts"][0]["buckets"] == {
        "mapped": 1,
        "custom_tag": 1,
        "negative_control_role": 1,
    }
    assert full["ambiguous_provenance_train_rows"] == 0
    assert uniform["conflicts"][0]["buckets"]["custom_tag"] == 1
    assert uniform["ambiguous_provenance_train_rows"] == 0


def test_system_message_must_match_current_prompt() -> None:
    row = train("A", "revenue")
    row["messages"][0]["content"] = "stale prompt"
    with pytest.raises(DatasetError, match="system prompt"):
        audit_rows([row], [], fit_source_ids=set())

    row["messages"][0]["content"] = audit_module.SYSTEM_PROMPT
    row["messages"][0]["role"] = "user"
    with pytest.raises(DatasetError, match="system prompt"):
        audit_rows([row], [], fit_source_ids=set())

    row = train("A", "revenue")
    row["messages"].pop(0)
    with pytest.raises(DatasetError, match="system prompt"):
        audit_rows([row], [], fit_source_ids=set())


def test_main_filters_nonfit_labels_before_model_validation(tmp_path: Path, capsys) -> None:
    record = source(cik_in(Part.FIT))
    foreign = source(cik_in(Part.FIT))
    foreign = foreign.model_copy(update={"source_id": "foreign-source"})
    train_path = tmp_path / "train.jsonl"
    examples_path = tmp_path / "examples.jsonl"
    sources_path = tmp_path / "sources.jsonl"
    moves_path = tmp_path / "moves.yaml"
    looks_path = tmp_path / "looks.tsv"
    report_path = tmp_path / "report.json"
    train_path.write_text(json.dumps(train("Net sales", "revenue")) + "\n", encoding="utf-8")
    valid_example = contract_example(record).model_dump(mode="json")
    invalid_foreign = contract_example(foreign).model_dump(mode="json")
    invalid_foreign["canonical_id"] = 42
    examples_path.write_text(
        json.dumps(valid_example) + "\n" + json.dumps(invalid_foreign) + "\n", encoding="utf-8"
    )
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path.write_text(LOG_HEADER, encoding="utf-8")

    result = main(
        [
            "--train",
            str(train_path),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(report_path),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "42" not in error
    assert "upstream fit example" not in error
    assert json.loads(report_path.read_text(encoding="utf-8"))["fit_source_count"] == 1


def test_main_rejects_malformed_example_json_without_echoing_line(tmp_path: Path, capsys) -> None:
    record = source(cik_in(Part.FIT))
    examples_path = tmp_path / "examples.jsonl"
    examples_path.write_text("{malformed secret-label\n", encoding="utf-8")
    sources_path = tmp_path / "sources.jsonl"
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    moves_path = tmp_path / "moves.yaml"
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path = tmp_path / "looks.tsv"
    looks_path.write_text(LOG_HEADER, encoding="utf-8")
    train_path = tmp_path / "train.jsonl"
    train_path.write_text(json.dumps(train("Net sales", "revenue")) + "\n", encoding="utf-8")

    result = main(
        [
            "--train",
            str(train_path),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(tmp_path / "out.json"),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "line 1" in error
    assert "secret-label" not in error


def test_main_reports_invalid_authorized_label_with_line_only(tmp_path: Path, capsys) -> None:
    record = source(cik_in(Part.FIT))
    bad_example = contract_example(record).model_dump(mode="json")
    bad_example["canonical_id"] = 42
    examples_path = tmp_path / "examples.jsonl"
    examples_path.write_text(json.dumps(bad_example) + "\n", encoding="utf-8")
    sources_path = tmp_path / "sources.jsonl"
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    moves_path = tmp_path / "moves.yaml"
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path = tmp_path / "looks.tsv"
    looks_path.write_text(LOG_HEADER, encoding="utf-8")
    train_path = tmp_path / "train.jsonl"
    train_path.write_text(json.dumps(train("Net sales", "revenue")) + "\n", encoding="utf-8")

    result = main(
        [
            "--train",
            str(train_path),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(tmp_path / "out.json"),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "line 1" in error
    assert "42" not in error
    assert "canonical_id" not in error


def test_main_rejects_nontrain_fit_roster_before_reading_examples(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    record = source(cik_in(Part.FIT), pool=Pool.DEV)
    sources_path = tmp_path / "sources.jsonl"
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    examples_path = tmp_path / "examples.jsonl"
    examples_path.write_text("malformed\n", encoding="utf-8")
    moves_path = tmp_path / "moves.yaml"
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path = tmp_path / "looks.tsv"
    looks_path.write_text(LOG_HEADER, encoding="utf-8")
    monkeypatch.setattr(audit_module, "development_documents", lambda roster, *_: roster)

    result = main(
        [
            "--train",
            str(tmp_path / "unused-train.jsonl"),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(tmp_path / "out.json"),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert "non-train-pool" in error
    assert "examples.jsonl" not in error


def test_r1_removes_only_eligible_conflicting_nulls() -> None:
    rows = [
        train("A", "revenue"),
        train("A", None),
        train("B", "revenue"),
        train("C", "revenue", "cash_flow"),
        train("C", None, "cash_flow"),
        train("D", "revenue"),
        train("D", None),
        train("E", None),
    ]
    examples_ = [
        example("s1", "A", "revenue"),
        example("s2", "A", None, role="negative_control"),
        example("s3", "B", "revenue"),
        example("s4", "C", "revenue", statement="cash_flow"),
        example("s5", "C", None, role="negative_control", statement="cash_flow"),
        example("s6", "D", "revenue"),
        example("s7", "D", None, abstention_reason="unsupported_tag"),
        example("s8", "E", None),
    ]
    report = audit_rows(rows, examples_, fit_source_ids={f"s{i}" for i in range(1, 9)})
    assert report["projections"]["R1"]["removed_rows"] == 1
    assert report["projections"]["R1"]["remaining_conflicting_keys"] == 2
    assert report["projections"]["R1"]["unsupported_statement_negatives"]["retained_rows"] == 1
    assert report["projections"]["R1"]["retained_null_rows"] == 3


def test_r2_quarantines_remaining_conflicts_and_keeps_resolved_key() -> None:
    rows = [train("A", "revenue"), train("A", None), train("B", "revenue"), train("B", None)]
    examples_ = [
        example("s1", "A", "revenue"),
        example("s2", "A", None, role="negative_control"),
        example("s3", "B", "revenue"),
        example("s4", "B", None, abstention_reason="other"),
    ]
    report = audit_rows(rows, examples_, fit_source_ids={f"s{i}" for i in range(1, 5)})
    assert report["projections"]["R1"]["retained_rows"] == 3
    assert report["projections"]["R2"]["retained_rows"] == 1
    assert report["projections"]["R2"]["remaining_conflicting_keys"] == 0


def test_class_retention_reports_zero_supervision() -> None:
    rows = [train("A", "rare"), train("A", None)]
    examples_ = [example("s1", "A", "rare"), example("s2", "A", None, abstention_reason="other")]
    report = audit_rows(rows, examples_, fit_source_ids={"s1", "s2"})
    assert "rare" in report["projections"]["R2"]["classes_losing_all_supervision"]


def test_non_null_disagreement_uses_pending_source_review_status() -> None:
    report = audit_rows(
        [train("A", "revenue"), train("A", "other_revenue")],
        [example("s1", "A", "revenue"), example("s2", "A", "other_revenue")],
        fit_source_ids={"s1", "s2"},
    )
    assert report["conflicts"][0]["multi_non_null_status"] == (
        "suspected_supervision_error_pending_source_review"
    )


def test_multi_target_counts_distinguish_groups_with_and_without_nulls() -> None:
    rows = [
        train("A", None),
        train("A", "revenue"),
        train("A", "other_revenue"),
        train("B", "revenue"),
        train("B", "other_revenue"),
    ]
    examples_ = [
        example(f"s{i}", label, answer, abstention_reason="other" if answer is None else None)
        for i, (label, answer) in enumerate(
            [
                ("A", None),
                ("A", "revenue"),
                ("A", "other_revenue"),
                ("B", "revenue"),
                ("B", "other_revenue"),
            ],
            start=1,
        )
    ]

    report = audit_rows(rows, examples_, fit_source_ids={f"s{i}" for i in range(1, 6)})

    assert report["multi_non_null_conflict_keys"] == 2
    assert report["nonnull_only_multi_target_conflict_keys"] == 1


def test_non_fit_source_raises_and_exclusive_output_is_deterministic(tmp_path) -> None:
    with pytest.raises(DatasetError, match="non-fit"):
        audit_rows([train("A", "revenue")], [example("s1", "A", "revenue")], fit_source_ids=set())
    with pytest.raises(DatasetError, match="non-train"):
        audit_rows(
            [train("A", "revenue")],
            [example("s1", "A", "revenue", pool="valid")],
            fit_source_ids={"s1"},
        )
    report = audit_rows(
        [train("A", "revenue")], [example("s1", "A", "revenue")], fit_source_ids={"s1"}
    )
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    write_report(first, report)
    write_report(second, report)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(DatasetError, match="already exists"):
        write_report(first, report)


def test_key_is_exact_rendered_user_content() -> None:
    row = train("Net sales", "revenue")
    report = audit_rows([row], [example("s1", "Net sales", "revenue")], fit_source_ids={"s1"})
    assert report["row_count"] == 1
    assert report["input_key_definition"] == "train.jsonl messages[1].content exact string"


def test_main_writes_report_with_count_and_full_hash_mismatches(tmp_path: Path, capsys) -> None:
    record = source(cik_in(Part.FIT))
    upstream = contract_example(record)
    train_path = tmp_path / "train.jsonl"
    examples_path = tmp_path / "examples.jsonl"
    sources_path = tmp_path / "sources.jsonl"
    moves_path = tmp_path / "moves.yaml"
    looks_path = tmp_path / "looks.tsv"
    report_path = tmp_path / "report.json"
    train_path.write_text(json.dumps(train("Net sales", "revenue")) + "\n", encoding="utf-8")
    examples_path.write_text(upstream.model_dump_json() + "\n", encoding="utf-8")
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path.write_text(LOG_HEADER, encoding="utf-8")

    result = main(
        [
            "--train",
            str(train_path),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(report_path),
        ]
    )

    assert result == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["fit_source_count"] == 1
    assert report["expected_count_checks"]["mismatches"]["row_count"]["actual"] == 1
    assert report["train_sha256_matches_expected"] is False
    assert report["expected_count_checks"]["mismatches"]["train_sha256"]["actual"] == (
        hashlib.sha256(train_path.read_bytes()).hexdigest()
    )
    assert "reported counts differ" in capsys.readouterr().err


def test_main_succeeds_when_expected_counts_and_hash_match(tmp_path: Path, monkeypatch) -> None:
    record = source(cik_in(Part.FIT))
    train_path = tmp_path / "train.jsonl"
    examples_path = tmp_path / "examples.jsonl"
    sources_path = tmp_path / "sources.jsonl"
    moves_path = tmp_path / "moves.yaml"
    looks_path = tmp_path / "looks.tsv"
    report_path = tmp_path / "report.json"
    train_path.write_text(json.dumps(train("Net sales", "revenue")) + "\n", encoding="utf-8")
    examples_path.write_text(contract_example(record).model_dump_json() + "\n", encoding="utf-8")
    sources_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    moves_path.write_text("moves: []\n", encoding="utf-8")
    looks_path.write_text(LOG_HEADER, encoding="utf-8")
    monkeypatch.setattr(
        audit_module,
        "PINNED_DATASET_EXPECTED_COUNTS",
        {
            "row_count": 1,
            "conflicting_keys": 0,
            "conflicting_rows": 0,
            "mixed_groups": 0,
            "null_rows_in_mixed_groups": 0,
            "fit_source_count": 1,
            "multi_non_null_conflict_keys": 0,
            "nonnull_only_multi_target_conflict_keys": 0,
        },
    )
    monkeypatch.setattr(
        audit_module, "PINNED_TRAIN_SHA256", hashlib.sha256(train_path.read_bytes()).hexdigest()
    )

    result = main(
        [
            "--train",
            str(train_path),
            "--examples",
            str(examples_path),
            "--sources",
            str(sources_path),
            "--moves",
            str(moves_path),
            "--looks",
            str(looks_path),
            "--out",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert report["expected_count_checks"]["mismatches"] == {}
