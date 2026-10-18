"""Synthetic dataset rendering, deduplication, length audit and writer checks."""

import hashlib
import json
from pathlib import Path

import pytest
from dataset_builder import (
    SYSTEM_PROMPT,
    BuiltDataset,
    build_dataset,
    dedupe_per_issuer,
    normalize_label,
    render_row,
    write_dataset,
)
from dataset_contracts import (
    DatasetError,
    NormalizationExample,
    PartitionedExamples,
    SourceRecord,
    example_digest,
    partition_examples,
)
from test_dataset_contracts import cik_in, config, example, registry_for, source

from fra_core.pools import Identity, Source
from fra_core.schemas.statement import BBox, Provenance, StatementType
from fra_core.split import Part, cik_key, hashed_part
from fra_core.taxonomy import load_taxonomy

TOKENIZER = "synthetic-tokenizer"


def fit_source(index: int = 0, **changes: object) -> SourceRecord:
    ciks = [c for c in range(1, 1000) if cik_in_part(c, Part.FIT)]
    cik = ciks[index]
    return source(cik, name=f"Fit Widgets {index}", **changes)


def valid_source(index: int = 0) -> SourceRecord:
    ciks = [c for c in range(1, 1000) if cik_in_part(c, Part.VALIDATION)]
    return source(ciks[index], name=f"Valid Widgets {index}")


def cik_in_part(cik: int, part: Part) -> bool:
    return hashed_part(cik_key(cik)) == part


def pdf_provenance() -> Provenance:
    return Provenance(
        page_no=1, bbox=BBox(left=0, top=0, right=10, bottom=10), table_ref="table-1", row=1, col=0
    )


def row(record: SourceRecord, example_id: str, **changes: object) -> NormalizationExample:
    return example(record, example_id=example_id, **changes)


def abstain(record: SourceRecord, example_id: str, label: str = "Other") -> NormalizationExample:
    return row(
        record,
        example_id,
        label=label,
        canonical_id=None,
        abstention_reason="no taxonomy item",
    )


def partitioned(
    fit: tuple[NormalizationExample, ...], validation: tuple[NormalizationExample, ...]
) -> PartitionedExamples:
    records = {e.source_record.source_id: e.source_record for e in (*fit, *validation)}
    rows = (*fit, *validation)
    return partition_examples(
        rows,
        sources=tuple(records.values()),
        registry=registry_for(*records.values()),
        config=config(*rows),
        taxonomy=load_taxonomy(),
        moves=(),
        looks=(),
    )


def default_parts() -> PartitionedExamples:
    return partitioned((row(fit_source(), "a-1"),), (row(valid_source(), "b-1"),))


def test_render_english_row_exact_bytes() -> None:
    rendered = render_row(row(fit_source(), "a-1"))
    assert rendered == {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": '{"label":"Net sales","statement":"income"}'},
            {"role": "assistant", "content": '{"canonical_id":"revenue"}'},
        ]
    }


def test_render_arabic_label_is_not_escaped() -> None:
    record = fit_source(source=Source.PDF, language="ar")
    pdf_row = row(
        record,
        "a-ar",
        label="الإيرادات",
        tag=None,
        provenance=pdf_provenance(),
    )
    user = render_row(pdf_row)["messages"][1]["content"]
    assert user == '{"label":"الإيرادات","statement":"income"}'


def test_render_abstention_is_null() -> None:
    rendered = render_row(abstain(fit_source(), "a-1"))
    assert rendered["messages"][2]["content"] == '{"canonical_id":null}'


def test_normalize_label_folds_case_whitespace_and_presentation_forms() -> None:
    assert normalize_label("  Net \t SALES ") == "net sales"
    # Arabic presentation-form letters fold to the base letters under NFKC.
    assert normalize_label("ﺍﻟﻣﺎﻟ") == normalize_label("المال")


def test_dedupe_collapses_same_issuer_variants_keeping_lowest_id() -> None:
    record = fit_source()
    rows = (
        row(record, "z-3", label="NET SALES"),
        row(record, "a-1", label="Net sales"),
        row(record, "m-2", label="net   sales"),
    )
    assert [r.example_id for r in dedupe_per_issuer(rows)] == ["a-1"]


def test_dedupe_keeps_different_issuers_separate() -> None:
    rows = (row(fit_source(0), "a-1"), row(fit_source(1), "a-2"))
    assert [r.example_id for r in dedupe_per_issuer(rows)] == ["a-1", "a-2"]


def test_dedupe_does_not_run_across_statements() -> None:
    record = fit_source()
    rows = (
        row(record, "a-1", statement=StatementType.INCOME),
        row(
            record,
            "a-2",
            statement=StatementType.COMPREHENSIVE_INCOME,
            canonical_id="revenue",
        ),
    )
    # Different statements are different keys even with the same label and target text.
    assert len(dedupe_per_issuer(rows)) == 2


def test_dedupe_conflict_raises_naming_example_ids() -> None:
    record = fit_source()
    rows = (row(record, "a-1"), abstain(record, "a-2", label="net SALES"))
    with pytest.raises(DatasetError, match=r"a-1.*a-2"):
        dedupe_per_issuer(rows)


def test_dedupe_is_deterministic_under_permutation() -> None:
    record = fit_source()
    rows = (
        row(record, "a-1"),
        row(record, "a-2", label="Cost of sales", canonical_id="cost_of_revenue"),
        row(record, "a-3", label="NET SALES"),
    )
    assert dedupe_per_issuer(rows) == dedupe_per_issuer(tuple(reversed(rows)))


def test_length_audit_passes_at_limit_and_fails_above() -> None:
    parts = default_parts()
    built = build_dataset(parts, count_tokens=lambda _m: 10, token_limit=10, tokenizer_id=TOKENIZER)
    assert built.manifest.fit_lengths.max == 10
    with pytest.raises(DatasetError, match=r"a-1.*11"):
        build_dataset(parts, count_tokens=lambda _m: 11, token_limit=10, tokenizer_id=TOKENIZER)


def test_length_audit_names_every_offender_in_validation_too() -> None:
    parts = default_parts()
    with pytest.raises(DatasetError, match=r"a-1.*b-1"):
        build_dataset(parts, count_tokens=lambda _m: 99, token_limit=10, tokenizer_id=TOKENIZER)


def test_empty_parts_are_refused() -> None:
    only_fit = partitioned((row(fit_source(), "a-1"),), ())
    with pytest.raises(DatasetError, match="validation"):
        build_dataset(only_fit, count_tokens=lambda _m: 1, tokenizer_id=TOKENIZER)
    only_valid = partitioned((), (row(valid_source(), "b-1"),))
    with pytest.raises(DatasetError, match="fit"):
        build_dataset(only_valid, count_tokens=lambda _m: 1, tokenizer_id=TOKENIZER)


def test_manifest_records_counts_strata_and_prompt_hash() -> None:
    record = fit_source()
    parts = partitioned(
        (row(record, "a-1"), row(record, "a-2", label="NET sales"), abstain(record, "a-3")),
        (row(valid_source(), "b-1"),),
    )
    built = build_dataset(
        parts, count_tokens=lambda m: len(m), token_limit=2048, tokenizer_id=TOKENIZER
    )
    manifest = built.manifest
    assert manifest.dataset == parts.manifest
    assert manifest.system_prompt_sha256 == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert manifest.tokenizer_id == TOKENIZER
    assert manifest.token_limit == 2048
    assert (manifest.fit_rows_before, manifest.fit_rows_after) == (3, 2)
    assert (manifest.validation_rows_before, manifest.validation_rows_after) == (1, 1)
    assert manifest.fit_lengths.count == 2
    assert manifest.fit_strata == {"sec|en|annual": 2}
    assert manifest.validation_strata == {"sec|en|annual": 1}
    assert len(built.fit_rows) == 2


def test_write_round_trips_hashes(tmp_path: Path) -> None:
    built = build_dataset(default_parts(), count_tokens=lambda _m: 5, tokenizer_id=TOKENIZER)
    out = tmp_path / "out"
    write_dataset(built, out)
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "train.jsonl", "valid.jsonl"]
    train = (out / "train.jsonl").read_bytes()
    valid = (out / "valid.jsonl").read_bytes()
    assert train.endswith(b"\n") and valid.endswith(b"\n")
    assert hashlib.sha256(train).hexdigest() == built.manifest.train_sha256
    assert hashlib.sha256(valid).hexdigest() == built.manifest.valid_sha256
    assert [json.loads(line) for line in train.decode("utf-8").splitlines()] == list(built.fit_rows)
    stored = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert stored["train_sha256"] == built.manifest.train_sha256
    assert stored == json.loads(built.manifest.model_dump_json())


def test_write_keeps_arabic_unescaped(tmp_path: Path) -> None:
    record = fit_source(source=Source.PDF, language="ar")
    pdf_row = row(
        record,
        "a-ar",
        label="الإيرادات",
        tag=None,
        provenance=pdf_provenance(),
    )
    built = build_dataset(
        partitioned((pdf_row,), (row(valid_source(), "b-1"),)),
        count_tokens=lambda _m: 5,
        tokenizer_id=TOKENIZER,
    )
    write_dataset(built, tmp_path / "out")
    assert "الإيرادات" in (tmp_path / "out" / "train.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("existing", ["train.jsonl", "valid.jsonl", "manifest.json", "test.jsonl"])
def test_write_refuses_existing_files(tmp_path: Path, existing: str) -> None:
    built = build_dataset(default_parts(), count_tokens=lambda _m: 5, tokenizer_id=TOKENIZER)
    (tmp_path / existing).write_text("keep", encoding="utf-8")
    with pytest.raises(DatasetError, match=existing):
        write_dataset(built, tmp_path)
    assert (tmp_path / existing).read_text(encoding="utf-8") == "keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == [existing]


def test_holdout_example_is_refused_upstream_and_builder_never_writes_test(
    tmp_path: Path,
) -> None:
    holdout = source(cik_in(Part.HOLDOUT))
    held = example(holdout)
    with pytest.raises(DatasetError, match="holdout"):
        partition_examples(
            (held,),
            sources=(holdout,),
            registry=registry_for(holdout),
            config=config(held),
            taxonomy=load_taxonomy(),
            moves=(),
            looks=(),
        )
    built: BuiltDataset = build_dataset(
        default_parts(), count_tokens=lambda _m: 5, tokenizer_id=TOKENIZER
    )
    write_dataset(built, tmp_path / "out")
    assert not (tmp_path / "out" / "test.jsonl").exists()


def test_swapped_or_extra_rows_do_not_match_the_upstream_manifest() -> None:
    parts = default_parts()
    extra = row(fit_source(1), "a-9")
    with pytest.raises(DatasetError, match="fit"):
        build_dataset(
            PartitionedExamples(
                fit=(*parts.fit, extra), validation=parts.validation, manifest=parts.manifest
            ),
            count_tokens=lambda _m: 1,
            tokenizer_id=TOKENIZER,
        )
    with pytest.raises(DatasetError, match="validation"):
        build_dataset(
            PartitionedExamples(fit=parts.fit, validation=parts.fit, manifest=parts.manifest),
            count_tokens=lambda _m: 1,
            tokenizer_id=TOKENIZER,
        )


def test_issuer_without_cik_is_a_dataset_error() -> None:
    parts = default_parts()
    nameless = row(source(1, issuer=Identity("No Cik Widgets", None)), "a-0")
    fit = (nameless,)
    manifest = parts.manifest.model_copy(update={"fit_sha256": example_digest(fit)})
    with pytest.raises(DatasetError, match=r"a-0.*CIK"):
        build_dataset(
            PartitionedExamples(fit=fit, validation=parts.validation, manifest=manifest),
            count_tokens=lambda _m: 1,
            tokenizer_id=TOKENIZER,
        )


def test_writer_refuses_rows_that_differ_from_manifest_hashes(tmp_path: Path) -> None:
    built = build_dataset(default_parts(), count_tokens=lambda _m: 5, tokenizer_id=TOKENIZER)
    altered = render_row(abstain(fit_source(), "a-1"))
    changed_train = BuiltDataset(
        fit_rows=(altered,), validation_rows=built.validation_rows, manifest=built.manifest
    )
    with pytest.raises(DatasetError, match="train"):
        write_dataset(changed_train, tmp_path / "out")
    changed_valid = BuiltDataset(
        fit_rows=built.fit_rows, validation_rows=(altered,), manifest=built.manifest
    )
    with pytest.raises(DatasetError, match="valid"):
        write_dataset(changed_valid, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("limit", [0, -1, True, 2.5])
def test_token_limit_must_be_a_positive_int(limit: object) -> None:
    with pytest.raises(DatasetError, match="token_limit"):
        build_dataset(
            default_parts(),
            count_tokens=lambda _m: 1,
            token_limit=limit,  # type: ignore[arg-type]
            tokenizer_id=TOKENIZER,
        )


@pytest.mark.parametrize("bad", [-1, True, 3.0, "3"])
def test_counter_must_return_a_non_negative_int(bad: object) -> None:
    with pytest.raises(DatasetError, match="count_tokens"):
        build_dataset(
            default_parts(),
            count_tokens=lambda _m: bad,  # type: ignore[arg-type,return-value]
            tokenizer_id=TOKENIZER,
        )


def test_length_stats_and_arabic_interim_strata_are_computed_per_row() -> None:
    record = fit_source()
    ar = fit_source(1, source=Source.PDF, language="ar", period="interim")
    ar_row = row(ar, "a-ar", label="الإيرادات", tag=None, provenance=pdf_provenance(), months=6)
    fit = (*(row(record, f"a-{i:02d}", label=f"Item {i}") for i in range(1, 21)), ar_row)
    # One length per row in call order (fit, then validation): fit gets 1..21.
    lengths = iter(range(1, 23))

    built = build_dataset(
        partitioned(fit, (row(valid_source(), "b-1"),)),
        count_tokens=lambda _m: next(lengths),
        tokenizer_id=TOKENIZER,
    )
    stats = built.manifest.fit_lengths
    assert (stats.count, stats.max, stats.mean, stats.p95) == (21, 21, "11.000", 20)
    assert built.manifest.fit_strata == {"pdf|ar|interim": 1, "sec|en|annual": 20}
