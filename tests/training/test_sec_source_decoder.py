"""Decoding prepared payloads and the coverage report, on synthetic rows."""

import gzip
import json
from pathlib import Path

import pytest
from dataset_contracts import DatasetError
from sec_crosswalk_draft import TagDefinition, build_draft, merge_vocabularies
from sec_crosswalk_fixtures import committed_mapping, synthetic_vocabulary, taxonomy
from sec_source_access import AccessContext, SecIndex, SourceManifest, iter_payloads
from sec_source_decoder import (
    DROP_REASONS,
    Dropped,
    Resolved,
    classify,
    coverage_curve,
    coverage_report,
    decode_payload,
    outside_split,
    top_unlisted,
    unlisted_jsonl,
)
from sec_source_pass import PayloadRow, directory_opener, encode_payload
from test_sec_source_pass import World, adsh, pre_line, sub_line, write_archive

from pool_store import load_registry


def row(
    stmt: str = "BS",
    tag: str = "Assets",
    version: str = "us-gaap/2024",
    plabel: str = "Total assets",
    *,
    inpth: bool = False,
    n: int = 1,
) -> PayloadRow:
    return PayloadRow(
        adsh=adsh(1),
        stmt=stmt,
        report=1,
        line=n,
        tag=tag,
        version=version,
        plabel=plabel,
        negating=False,
        inpth=inpth,
    )


def crosswalk():  # type: ignore[no-untyped-def]
    mapping = committed_mapping()
    return mapping, build_draft(synthetic_vocabulary(mapping), mapping, taxonomy())


def kind_of(r: PayloadRow) -> str:
    mapping, cw = crosswalk()
    result = classify(r, mapping, cw)
    return result.reason if isinstance(result, Dropped) else result.kind


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "mapped"),
        ({"stmt": "IS", "tag": "Revenues", "plabel": "Net sales"}, "mapped"),
        (
            {"stmt": "IS", "tag": "RevenueFromContractWithCustomerIncludingAssessedTax"},
            "null_entry",
        ),
        ({"tag": "NotInTheCrosswalk"}, "outside_crosswalk"),
        ({"version": "us-gaap/2019"}, "outside_crosswalk"),
        ({"version": adsh(1), "tag": "FilerTag"}, "custom_extension"),
        ({"stmt": "SI"}, "unsupported_statement_code"),
        ({"stmt": "UN"}, "unsupported_statement_code"),
        ({"stmt": ""}, "unsupported_statement_code"),
        ({"stmt": "CP"}, "unsupported_statement_code"),
        ({"version": "ifrs/2024"}, "unsupported_namespace"),
        ({"version": "us-gaap-ebp/2024"}, "unsupported_namespace"),
        ({"stmt": "IS", "tag": "NetIncomeLoss"}, "ambiguous_semantics"),
        ({"stmt": "BS", "tag": "StockholdersEquity"}, "ambiguous_semantics"),
        ({"inpth": True}, "parenthetical"),
        ({"plabel": "   "}, "empty_label"),
        ({"stmt": "CF"}, "outside_crosswalk"),
    ],
)
def test_classification(changes: dict[str, object], expected: str) -> None:
    assert kind_of(row(**changes)) == expected  # type: ignore[arg-type]


def test_a_mapped_row_carries_its_key_and_target() -> None:
    mapping, cw = crosswalk()
    result = classify(row(), mapping, cw)
    assert isinstance(result, Resolved)
    assert result.key.namespace == "us-gaap" and result.key.tag_taxonomy_version == "2024"
    assert result.key.statement.value == "balance"
    assert result.canonical_id == "total_assets"


def test_a_custom_tag_gets_the_extension_scope() -> None:
    mapping, cw = crosswalk()
    result = classify(row(version=adsh(1), tag="FilerTag"), mapping, cw)
    assert isinstance(result, Resolved)
    assert (result.key.namespace, result.key.tag_taxonomy_version) == ("custom", "extension")


def test_the_drop_reasons_are_the_documented_ones() -> None:
    assert set(DROP_REASONS) == {
        "unsupported_statement_code",
        "unsupported_namespace",
        "ambiguous_semantics",
        "parenthetical",
        "empty_label",
    }


def prepared(tmp_path: Path, rows: list[bytes]) -> tuple[World, list]:  # type: ignore[type-arg]
    world = World(tmp_path)
    write_archive(
        world.archive,
        [
            sub_line(1, world.fit, "Fit Co"),
            sub_line(2, world.valid, "Valid Co", "10-Q"),
        ],
        rows,
    )
    world.run()
    base = tmp_path / "prepared" / "2024q3"
    index = SecIndex.model_validate_json((base / "index.json").read_bytes())
    manifest = SourceManifest.model_validate_json((base / "manifest.json").read_bytes())
    context = AccessContext.model_validate_json((base / "context.json").read_bytes())
    verified = list(
        iter_payloads(
            [a.artifact_id for a in manifest.artifacts],
            index=index,
            manifest=manifest,
            registry=load_registry(world.registry_path),
            context=context,
            moves=[],
            looks=[],
            opener=directory_opener(tmp_path / "prepared"),
        )
    )
    return world, verified


def coverage_rows() -> list[bytes]:
    custom = f"{adsh(1)}\t3\t1\tBS\t0\tH\tFilerTag\t{adsh(1)}\tFiler line\t0\n".encode()
    return [
        pre_line(1, 1, "Total assets"),
        pre_line(1, 2, "Net sales", stmt="IS", tag="Revenues"),
        pre_line(1, 3, "Net income", stmt="IS", tag="NetIncomeLoss"),
        pre_line(1, 4, "(parenthetical)", inpth="1"),
        pre_line(1, 5, "Schedule", stmt="SI"),
        pre_line(1, 6, "Fresh A", tag="NewTagA"),
        pre_line(1, 7, "Fresh A again", tag="NewTagA"),
        pre_line(2, 1, "Fresh B", tag="NewTagB"),
        pre_line(2, 2, "Fresh C", stmt="IS", tag="NewTagC"),
        pre_line(2, 3, "Cash flow", stmt="CF", tag="NewTagD"),
        custom,
        pre_line(2, 4, "Total assets"),
    ]


def test_decode_validates_the_payload_against_its_descriptor(tmp_path: Path) -> None:
    _, verified = prepared(tmp_path, coverage_rows())
    first = next(v for v in verified if v.descriptor.filing.source_record.source_id == adsh(1))
    decoded = decode_payload(first)
    assert len(decoded) == 8
    assert all(r.adsh == adsh(1) for r in decoded)

    def with_payload(data: bytes):  # type: ignore[no-untyped-def]
        return first.model_copy(update={"payload": data})

    with pytest.raises(DatasetError, match="declared"):
        decode_payload(with_payload(encode_payload(decoded[:3])))
    twice = [*decoded[:-1], decoded[0]]
    with pytest.raises(DatasetError, match="twice"):
        decode_payload(with_payload(encode_payload(twice)))
    other = PayloadRow(**{**decoded[0].model_dump(), "adsh": adsh(9)})
    with pytest.raises(DatasetError, match="filing"):
        decode_payload(with_payload(encode_payload([other, *decoded[1:]])))
    with pytest.raises(DatasetError, match="not gzip"):
        decode_payload(with_payload(b"plain"))
    with pytest.raises(DatasetError, match="line 1"):
        decode_payload(with_payload(gzip.compress(b'{"adsh": 1}\n')))


def test_coverage_counts_rows_by_resolution_kind_and_drop_reason(tmp_path: Path) -> None:
    _, verified = prepared(tmp_path, coverage_rows())
    mapping, cw = crosswalk()
    report = coverage_report(verified, mapping, cw)
    assert report.rows == 12
    assert report.by_kind == {
        "mapped": 3,
        "outside_crosswalk": 5,
        "custom_extension": 1,
    }
    assert report.drops == {
        "ambiguous_semantics": 1,
        "parenthetical": 1,
        "unsupported_statement_code": 1,
    }
    assert sum(report.by_kind.values()) + sum(report.drops.values()) == report.rows


def test_top_unlisted_counts_standard_tags_in_bs_and_is_only(tmp_path: Path) -> None:
    _, verified = prepared(tmp_path, coverage_rows())
    mapping, cw = crosswalk()
    report = coverage_report(verified, mapping, cw)
    vocabulary = synthetic_vocabulary(mapping)
    top = top_unlisted(report, 10, vocabulary)
    assert [(r["tag"], r["rows"]) for r in top["BS"]] == [("NewTagA", 2), ("NewTagB", 1)]
    assert [(r["tag"], r["rows"]) for r in top["IS"]] == [("NewTagC", 1)]
    assert top_unlisted(report, 1, vocabulary)["BS"][0]["tag"] == "NewTagA"
    assert json.dumps(top)  # label free and serialisable
    assert "Fresh" not in json.dumps(top)


def test_outside_crosswalk_rows_are_split_by_where_the_gap_is(tmp_path: Path) -> None:
    rows = [
        *coverage_rows(),
        # A listed tag in a tag taxonomy version the crosswalk has no entry for.
        pre_line(2, 5, "Total assets 2019").replace(b"us-gaap/2024", b"us-gaap/2019"),
    ]
    _, verified = prepared(tmp_path, rows)
    mapping, cw = crosswalk()
    report = coverage_report(verified, mapping, cw)
    assert outside_split(report) == {
        "bs_is_unlisted_tag": 4,
        "bs_is_listed_tag_without_entry": 1,
        "unmodeled_statements": 1,
    }
    assert sum(outside_split(report).values()) == report.by_kind["outside_crosswalk"]


def test_distinct_unlisted_tags_and_the_cumulative_curve(tmp_path: Path) -> None:
    _, verified = prepared(tmp_path, coverage_rows())
    mapping, cw = crosswalk()
    report = coverage_report(verified, mapping, cw)
    assert {s: len(c) for s, c in report.unlisted.items()} == {"BS": 2, "IS": 1}
    # Unlisted rows are 2, 1 and 1 by tag: one tag covers half, all three cover 80% and more.
    assert coverage_curve(report, (50, 80, 90, 95, 99)) == {50: 1, 80: 3, 90: 3, 95: 3, 99: 3}


def test_the_full_ranking_is_written_with_vocabulary_text_only(tmp_path: Path) -> None:
    _, verified = prepared(tmp_path, coverage_rows())
    mapping, cw = crosswalk()
    report = coverage_report(verified, mapping, cw)

    def define(version: str) -> TagDefinition:
        namespace, _, number = version.partition("/")
        return TagDefinition(
            tag="NewTagA",
            namespace=namespace,
            tag_taxonomy_version=number,
            tlabel=f"label {number}",
            doc=f"definition {number}",
        )

    vocabulary = merge_vocabularies([define("us-gaap/2023"), define("us-gaap/2024")])
    lines = [json.loads(x) for x in unlisted_jsonl(report, vocabulary).splitlines()]
    assert [(r["statement"], r["tag"], r["rows"]) for r in lines] == [
        ("BS", "NewTagA", 2),
        ("BS", "NewTagB", 1),
        ("IS", "NewTagC", 1),
    ]
    assert lines[0]["versions"] == {
        "us-gaap/2023": {"tlabel": "label 2023", "doc": "definition 2023"},
        "us-gaap/2024": {"tlabel": "label 2024", "doc": "definition 2024"},
    }
    assert lines[1]["versions"] == {}
    assert "Fresh" not in unlisted_jsonl(report, vocabulary)


def test_labels_with_unicode_line_separators_survive_the_payload(tmp_path: Path) -> None:
    label = "Net\u2028sales\u0085and\u2029other"
    _, verified = prepared(tmp_path, [pre_line(1, 1, label), pre_line(2, 1, "Total assets")])
    first = next(v for v in verified if v.descriptor.filing.source_record.source_id == adsh(1))
    assert [r.plabel for r in decode_payload(first)] == [label]
