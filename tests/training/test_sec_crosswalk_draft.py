"""Tag vocabulary parsing, the curated mapping and the draft crosswalk generator."""

import hashlib
import zipfile
from pathlib import Path

import pytest
import yaml
from dataset_contracts import DatasetError, canonical_json
from pydantic import ValidationError
from sec_crosswalk import AbstentionPolicy, TagKey, taxonomy_sha256
from sec_crosswalk_draft import (
    DRAFT_PRODUCER_ID,
    MAPPING_PATH,
    STATEMENT_CODES,
    CuratedMapping,
    MappingExclusion,
    MappingRow,
    TagDefinition,
    build_draft,
    build_policies,
    load_mapping,
    merge_vocabularies,
    read_archive_member,
    read_tag_vocabulary,
    unmatched_rows,
)
from sec_crosswalk_fixtures import (
    committed_mapping,
    synthetic_vocabulary,
    tag_line,
    taxonomy,
    write_archive,
)

from fra_core.schemas.statement import StatementType


def definition(tag: str, version: str = "us-gaap/2024", doc: str = "A definition") -> TagDefinition:
    namespace, _, number = version.partition("/")
    return TagDefinition(
        tag=tag, namespace=namespace, tag_taxonomy_version=number, tlabel="A label", doc=doc
    )


def vocab(*entries: TagDefinition) -> dict[str, dict[str, TagDefinition]]:
    return merge_vocabularies(entries)


def small_mapping(*rows: MappingRow, excluded: tuple[MappingExclusion, ...] = ()) -> CuratedMapping:
    return CuratedMapping(
        version=1,
        namespace="us-gaap",
        unmapped_items={},
        excluded=excluded,
        rows=rows,
        sha256="c" * 64,
        source_name="m.yaml",
    )


ASSETS = MappingRow(
    statement="BS",
    tag="Assets",
    canonical_id="total_assets",
    doc_phrases=("Sum of",),
    reason="total",
)
AMBIGUOUS = MappingRow(
    statement="IS",
    tag="NetIncomeLoss",
    canonical_id=None,
    doc_phrases=("portion of",),
    reason="two items",
)


@pytest.mark.parametrize("companion", ["NetIncomeLoss", "MissingTag"])
def test_mapping_refuses_self_or_missing_companion(tmp_path: Path, companion: str) -> None:
    data = yaml.safe_load(MAPPING_PATH.read_text())
    data["excluded"][0]["companion"] = {
        "tag": companion,
        "canonical_id": "net_income_attributable_parent",
        "doc_phrases": ["parent"],
    }
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(DatasetError, match=r"companion.*(self|missing)"):
        load_mapping(path, taxonomy())


def test_reads_standard_tags_and_parses_namespace_and_version(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "a.zip",
        [
            tag_line("Assets", doc="Sum of assets"),
            tag_line("CustomThing", "0001234567-24-000001", custom="1"),
            tag_line("AbstractHeading", abstract="1"),
            tag_line("Leases", "ifrs/2025"),
        ],
    )
    tags = read_tag_vocabulary(archive)
    assert [(t.tag, t.namespace, t.tag_taxonomy_version) for t in tags] == [
        ("Leases", "ifrs", "2025"),
        ("Assets", "us-gaap", "2024"),
    ]
    assert tags[1].doc == "Sum of assets"


def test_only_the_tag_vocabulary_member_can_be_read(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "a.zip", [tag_line("Assets")])
    for member in ("pre.txt", "num.txt", "sub.txt", "../tag.txt"):
        with pytest.raises(DatasetError, match=r"tag\.txt"):
            read_archive_member(archive, member)
    assert "Assets" in read_archive_member(archive, "tag.txt")


def test_missing_archive_and_missing_member_are_named(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match=r"absent\.zip"):
        read_tag_vocabulary(tmp_path / "absent.zip")
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("readme.htm", "x")
    with pytest.raises(DatasetError, match=r"tag\.txt"):
        read_tag_vocabulary(empty)


@pytest.mark.parametrize(
    ("lines", "header", "match"),
    [
        ([tag_line("Assets") + "\textra"], None, r"tag\.txt:2"),
        ([tag_line("Assets"), "Short\tline"], None, r"tag\.txt:3"),
        ([tag_line("Assets", "us-gaap")], None, r"tag\.txt:2.*version"),
        ([tag_line("Assets", "US-GAAP/2024")], None, r"tag\.txt:2.*version"),
        ([tag_line("Assets", custom="2")], None, r"tag\.txt:2.*custom"),
        ([tag_line("Assets", abstract="x")], None, r"tag\.txt:2.*abstract"),
        ([tag_line("Assets")], "tag\tversion", r"tag\.txt:1.*header"),
    ],
)
def test_malformed_lines_are_named_by_member_and_line(
    tmp_path: Path, lines: list[str], header: str | None, match: str
) -> None:
    archive = write_archive(
        tmp_path / "a.zip",
        lines,
        header=header
        or "\t".join(
            ["tag", "version", "custom", "abstract", "datatype", "iord", "crdr", "tlabel", "doc"]
        ),
    )
    with pytest.raises(DatasetError, match=match):
        read_tag_vocabulary(archive)


def test_conflicting_duplicate_definitions_are_refused(tmp_path: Path) -> None:
    archive = write_archive(
        tmp_path / "a.zip", [tag_line("Assets", doc="one"), tag_line("Assets", doc="two")]
    )
    with pytest.raises(DatasetError, match="Assets"):
        read_tag_vocabulary(archive)
    identical = write_archive(tmp_path / "b.zip", [tag_line("Assets"), tag_line("Assets")])
    assert len(read_tag_vocabulary(identical)) == 1
    with pytest.raises(DatasetError, match="Assets"):
        merge_vocabularies([definition("Assets", doc="one"), definition("Assets", doc="two")])


def test_committed_mapping_is_valid_against_the_taxonomy() -> None:
    mapping = committed_mapping()
    items = {item.id: item for item in taxonomy().items}
    assert (
        mapping.sha256
        == hashlib.sha256(
            (
                Path(__file__).parents[2] / "training/data/sec_crosswalk/us_gaap_mapping.yaml"
            ).read_bytes()
        ).hexdigest()
    )
    mapped = {row.canonical_id for row in mapping.entry_rows if row.canonical_id}
    # Every taxonomy item is either mapped by some tag or recorded as having none.
    assert mapped | set(mapping.unmapped_items) == set(items)
    assert not mapped & set(mapping.unmapped_items)
    for row in mapping.rows:
        if row.canonical_id:
            assert items[row.canonical_id].statement == STATEMENT_CODES[row.statement]
    critical = {
        "revenue",
        "net_income",
        "total_assets",
        "total_equity",
        "total_current_assets",
        "total_current_liabilities",
    }
    assert critical <= mapped


def committed_text() -> str:
    return MAPPING_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("canonical_id: total_assets", "canonical_id: no_such_item"),
        ("  tag: Assets\n", "  tag: Liabilities\n"),
        ("version: 1\n", "version: 2\n"),
    ],
)
def test_mapping_loader_refuses_invalid_rows(tmp_path: Path, old: str, new: str) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(committed_text().replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DatasetError, match=r"m\.yaml"):
        load_mapping(path, taxonomy())


def test_mapping_loader_refuses_a_row_listed_twice(tmp_path: Path) -> None:
    text = committed_text()
    path = tmp_path / "m.yaml"
    path.write_text(
        text
        + "- statement: BS\n  tag: Assets\n  canonical_id: null\n  doc_phrases: [x]\n  reason: y\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="mapped twice"):
        load_mapping(path, taxonomy())


def test_mapping_loader_refuses_uncovered_items_and_bad_files(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(
        "version: 1\nnamespace: us-gaap\nunmapped_items: {}\nexcluded: []\nrows: []\n",
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="revenue"):
        load_mapping(path, taxonomy())
    path.write_text("rows: [unclosed\n", encoding="utf-8")
    with pytest.raises(DatasetError, match=r"m\.yaml"):
        load_mapping(path, taxonomy())
    with pytest.raises(DatasetError, match="absent"):
        load_mapping(tmp_path / "absent.yaml", taxonomy())


def test_draft_has_one_entry_per_version_statement_and_tag_present() -> None:
    mapping = small_mapping(ASSETS, AMBIGUOUS)
    entries = build_draft(
        vocab(
            definition("Assets", "us-gaap/2023", "Sum of 2023"),
            definition("Assets", "us-gaap/2024", "Sum of 2024"),
            definition("NetIncomeLoss", "us-gaap/2024", "portion of profit"),
            definition("Unmapped", "us-gaap/2024"),
            definition("Assets", "ifrs/2024", "Sum of ifrs"),
        ),
        mapping,
        taxonomy(),
    ).entries
    keys = sorted((e.key.tag_taxonomy_version, e.key.tag, e.key.statement.value) for e in entries)
    # NetIncomeLoss is missing from 2023, so it has no 2023 entry; the ifrs tag is not mapped.
    assert keys == [
        ("2023", "Assets", "balance"),
        ("2024", "Assets", "balance"),
        ("2024", "NetIncomeLoss", "income"),
    ]


def test_draft_entry_fields_and_review_metadata() -> None:
    mapping = small_mapping(ASSETS, AMBIGUOUS)
    tax = taxonomy()
    draft = build_draft(
        vocab(
            definition("Assets", doc="Sum of assets"),
            definition("NetIncomeLoss", doc="portion of profit"),
        ),
        mapping,
        tax,
    )
    by_tag = {e.key.tag: e for e in draft.entries}
    assets = by_tag["Assets"]
    assert assets.key == TagKey(
        namespace="us-gaap",
        tag_taxonomy_version="2024",
        statement=StatementType.BALANCE,
        tag="Assets",
    )
    assert assets.canonical_id == "total_assets"
    assert assets.abstention_reason is None
    assert assets.semantic_reference == "tag.txt us-gaap/2024 Assets"
    assert (
        assets.semantic_evidence_sha256
        == hashlib.sha256(
            canonical_json({"tlabel": "A label", "doc": "Sum of assets"}).encode("utf-8")
        ).hexdigest()
    )
    assert by_tag["NetIncomeLoss"].canonical_id is None
    assert by_tag["NetIncomeLoss"].abstention_reason == "two items"
    assert draft.review.status == "draft"
    assert draft.review.producer_id == DRAFT_PRODUCER_ID
    assert draft.review.sha256 == mapping.sha256
    assert draft.canonical_taxonomy_version == tax.version
    assert draft.canonical_taxonomy_sha256 == taxonomy_sha256(tax)


def test_draft_is_independent_of_input_order() -> None:
    mapping = small_mapping(ASSETS, AMBIGUOUS)
    entries = [
        definition("Assets", "us-gaap/2023", "Sum of x"),
        definition("Assets", "us-gaap/2024", "Sum of x"),
        definition("NetIncomeLoss", "us-gaap/2024", "portion of x"),
    ]
    forward = build_draft(vocab(*entries), mapping, taxonomy())
    backward = build_draft(vocab(*reversed(entries)), small_mapping(AMBIGUOUS, ASSETS), taxonomy())
    assert forward.content_sha256() == backward.content_sha256()


def test_only_companion_exclusions_add_requirements_to_draft_entries() -> None:
    mapping = committed_mapping()
    draft = build_draft(synthetic_vocabulary(mapping), mapping, taxonomy())
    conditional = {
        entry.key.tag: entry.requires_companion
        for entry in draft.entries
        if entry.requires_companion is not None
    }
    assert conditional == {
        "NetIncomeLoss": "ProfitLoss",
        "StockholdersEquity": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    }


def test_draft_refuses_a_phrase_missing_from_the_definition() -> None:
    with pytest.raises(DatasetError, match=r"Assets.*us-gaap/2024"):
        build_draft(
            vocab(definition("Assets", doc="Something else entirely")),
            small_mapping(ASSETS),
            taxonomy(),
        )


def test_any_listed_phrase_may_match_a_reworded_definition() -> None:
    row = ASSETS.model_copy(update={"doc_phrases": ("Sum of", "Amount of asset")})
    draft = build_draft(
        vocab(
            definition("Assets", "us-gaap/2023", "Sum of carrying amounts"),
            definition("Assets", "us-gaap/2024", "Amount of asset recognized"),
        ),
        small_mapping(row),
        taxonomy(),
    )
    assert len(draft.entries) == 2


def test_draft_refuses_invalid_targets_and_empty_vocabulary() -> None:
    wrong = ASSETS.model_copy(update={"canonical_id": "revenue"})
    with pytest.raises(DatasetError, match="Assets"):
        build_draft(vocab(definition("Assets", doc="Sum of")), small_mapping(wrong), taxonomy())
    with pytest.raises(DatasetError, match="us-gaap"):
        build_draft(vocab(definition("Assets", "ifrs/2024")), small_mapping(ASSETS), taxonomy())


def test_mapping_rows_reject_unknown_statement_codes() -> None:
    with pytest.raises(ValidationError):
        MappingRow(statement="CF", tag="X", canonical_id=None, doc_phrases=("x",), reason="y")


def policies_for(*versions: str) -> tuple[AbstentionPolicy, ...]:
    return build_policies({f"us-gaap/{v}": {} for v in versions}, small_mapping(ASSETS), taxonomy())


def test_policies_cover_every_statement_and_version_once_per_kind() -> None:
    policies = policies_for("2023", "2024")
    scopes: dict[str, list[tuple[str, str, str]]] = {
        "outside_crosswalk": [],
        "negative_control": [],
    }
    for policy in policies:
        assert policy.review.status == "draft"
        assert policy.review.producer_id == DRAFT_PRODUCER_ID
        scopes[policy.kind] += [
            (s.namespace, s.tag_taxonomy_version, s.statement.value) for s in policy.scopes
        ]
    expected = {
        (ns, v, st.value)
        for ns, v in (("us-gaap", "2023"), ("us-gaap", "2024"), ("custom", "extension"))
        for st in STATEMENT_CODES.values()
    }
    for kind, found in scopes.items():
        assert len(found) == len(set(found)), kind
        assert set(found) == expected, kind
    assert len({p.policy_id for p in policies}) == len(policies)
    assert all(p.reason for p in policies)


def test_policies_need_a_vocabulary_for_the_mapping_namespace() -> None:
    with pytest.raises(DatasetError, match="us-gaap"):
        build_policies({"ifrs/2024": {}}, small_mapping(ASSETS), taxonomy())


def test_committed_mapping_excludes_the_parent_share_tags_instead_of_nulling_them() -> None:
    mapping = committed_mapping()
    excluded = {(x.statement, x.tag) for x in mapping.excluded}
    assert excluded >= {("IS", "NetIncomeLoss"), ("BS", "StockholdersEquity")}
    # Excluded tags are not unconditional mapping rows.
    assert not excluded & {(r.statement, r.tag) for r in mapping.rows}
    assert all(x.reason for x in mapping.excluded)


def test_a_tag_cannot_be_both_mapped_and_excluded() -> None:
    both = MappingExclusion(statement="BS", tag="Assets", reason="ambiguous")
    with pytest.raises(DatasetError, match="Assets"):
        build_draft(
            vocab(definition("Assets", doc="Sum of")),
            small_mapping(ASSETS, excluded=(both,)),
            taxonomy(),
        )


def test_excluded_tags_get_no_entry() -> None:
    gone = MappingExclusion(statement="IS", tag="NetIncomeLoss", reason="ambiguous")
    draft = build_draft(
        vocab(definition("Assets", doc="Sum of"), definition("NetIncomeLoss")),
        small_mapping(ASSETS, excluded=(gone,)),
        taxonomy(),
    )
    assert [e.key.tag for e in draft.entries] == ["Assets"]


def test_mapping_loader_refuses_an_excluded_tag_that_is_also_a_row(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(
        committed_text().replace(
            "excluded:\n", "excluded:\n- statement: BS\n  tag: Assets\n  reason: clash\n", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="Assets"):
        load_mapping(path, taxonomy())


def test_review_reference_names_the_mapping_file_given(tmp_path: Path) -> None:
    path = tmp_path / "custom_name.yaml"
    path.write_text(committed_text(), encoding="utf-8")
    mapping = load_mapping(path, taxonomy())
    assert mapping.source_name == "custom_name.yaml"
    draft = build_draft(synthetic_vocabulary(mapping), mapping, taxonomy())
    assert "custom_name.yaml" in draft.review.reference
    assert MAPPING_PATH.name not in draft.review.reference
    policies = build_policies(synthetic_vocabulary(mapping), mapping, taxonomy())
    assert all("custom_name.yaml" in p.review.reference for p in policies)


def test_rows_and_exclusions_without_any_vocabulary_match_are_listed() -> None:
    gone = MappingExclusion(statement="IS", tag="NetIncomeLoss", reason="ambiguous")
    mapping = small_mapping(ASSETS, AMBIGUOUS, excluded=(gone,))
    present = vocab(definition("Assets", "us-gaap/2023", "Sum of"), definition("Other"))
    assert unmatched_rows(present, mapping) == ["IS NetIncomeLoss", "IS NetIncomeLoss (excluded)"]
    complete = vocab(definition("Assets"), definition("NetIncomeLoss", "us-gaap/2025"))
    assert unmatched_rows(complete, mapping) == []


def committed_targets() -> dict[tuple[str, str], str | None]:
    return {(r.statement, r.tag): r.canonical_id for r in committed_mapping().rows}


@pytest.mark.parametrize(
    ("statement", "tag", "target"),
    [
        ("BS", "AccountsReceivableNet", "trade_receivables"),
        ("BS", "AccountsReceivableNetCurrent", "trade_receivables"),
        ("BS", "NotesPayableCurrent", None),
        ("BS", "LoansPayableCurrent", "current_portion_long_term_debt"),
        ("IS", "RevenueFromContractWithCustomerIncludingAssessedTax", None),
        ("IS", "RevenueFromContractWithCustomerExcludingAssessedTax", "revenue"),
        ("BS", "OperatingLeaseLiability", None),
        ("IS", "InvestmentIncomeInterestAndDividend", None),
        ("BS", "OtherIntangibleAssetsNet", "intangible_assets"),
        ("BS", "AccountsAndOtherReceivablesNetCurrent", "trade_receivables"),
        ("BS", "OtherAssetsMiscellaneousNoncurrent", "other_non_current_assets"),
        ("BS", "LongTermLoansFromBank", None),
        ("BS", "LongTermNotesAndLoans", "long_term_borrowings"),
        ("BS", "LongTermNotesPayable", None),
        ("BS", "LoansPayableToBankCurrent", None),
        ("BS", "NotesPayableToBankCurrent", None),
        ("BS", "ShortTermBankLoansAndNotesPayable", None),
        ("BS", "NotesAndLoansPayableCurrent", "current_portion_long_term_debt"),
        (
            "BS",
            "PensionAndOtherPostretirementAndPostemploymentBenefitPlansLiabilitiesNoncurrent",
            "other_non_current_liabilities",
        ),
        (
            "BS",
            "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
            "total_equity",
        ),
        ("BS", "InterestAndDebtExpense", "absent"),
        ("BS", "OtherAccruedLiabilitiesCurrent", "absent"),
        ("BS", "OtherAccruedLiabilitiesNoncurrent", "absent"),
    ],
)
def test_reviewed_tag_decisions(statement: str, tag: str, target: str | None) -> None:
    targets = committed_targets()
    if target == "absent":
        # Borderline tags are left unlisted on purpose.
        assert (statement, tag) not in targets
    else:
        assert targets[(statement, tag)] == target


def test_single_instrument_borrowing_tags_are_null_with_the_stated_reason() -> None:
    rows = {(r.statement, r.tag): r for r in committed_mapping().rows}
    for tag in (
        "NotesPayableCurrent",
        "ShortTermBankLoansAndNotesPayable",
        "LoansPayableToBankCurrent",
        "NotesPayableToBankCurrent",
        "LongTermLoansFromBank",
        "LongTermNotesPayable",
    ):
        assert rows[("BS", tag)].canonical_id is None
        assert rows[("BS", tag)].reason == "single borrowing instrument; narrower than the item"
    assert rows[("BS", "LoansPayableCurrent")].canonical_id == "current_portion_long_term_debt"


def test_equity_tags_that_do_not_say_whether_a_noncontrolling_share_is_included_are_excluded() -> (
    None
):
    mapping = committed_mapping()
    assert {(x.statement, x.tag) for x in mapping.excluded} == {
        ("IS", "NetIncomeLoss"),
        ("BS", "StockholdersEquity"),
        ("BS", "MembersEquity"),
        ("BS", "PartnersCapital"),
        ("BS", "MembersCapital"),
    }
    listed = {(r.statement, r.tag) for r in mapping.rows}
    assert ("BS", "PartnersCapital") not in listed and ("BS", "MembersEquity") not in listed
    # The partnership tag that says it includes the noncontrolling share stays mapped.
    assert ("BS", "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest") in listed


def test_tags_that_land_on_one_item_are_the_documented_collisions() -> None:
    by_item: dict[str, list[str]] = {}
    for row in committed_mapping().rows:
        if row.canonical_id:
            by_item.setdefault(row.canonical_id, []).append(row.tag)
    assert sorted(by_item["revenue"]) == [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ]
    assert sorted(by_item["current_portion_long_term_debt"]) == [
        "LoansPayableCurrent",
        "LongTermDebtCurrent",
        "NotesAndLoansPayableCurrent",
    ]
    assert sorted(by_item["long_term_borrowings"]) == [
        "LongTermDebtNoncurrent",
        "LongTermLoansPayable",
        "LongTermNotesAndLoans",
    ]
    assert sorted(by_item["short_term_borrowings"]) == ["BankOverdrafts", "ShortTermBorrowings"]


def test_a_mapped_reason_never_leans_on_a_tag_that_is_null_or_excluded() -> None:
    import re

    mapping = committed_mapping()
    status = {r.tag: r.canonical_id for r in mapping.rows}
    status.update({x.tag: "excluded" for x in mapping.excluded})
    for row in mapping.rows:
        if row.canonical_id is None:
            continue
        for tag, target in status.items():
            if tag != row.tag and re.search(rf"\b{tag}\b", row.reason):
                assert target not in (None, "excluded"), f"{row.tag} cites {tag}"


def test_reasons_that_were_stale_after_the_review_are_corrected() -> None:
    mapping = committed_mapping()
    rows = {r.tag: r for r in mapping.rows}
    assert "siblings" not in rows["NotesAndLoansPayableCurrent"].reason
    assert "NotesPayableCurrent" not in rows["LoansPayableCurrent"].reason
    members = next(x for x in mapping.excluded if x.tag == "MembersEquity")
    assert "parent entity's share" in members.reason and "2026" in members.reason
    assert "does not say" not in members.reason and "without saying" not in members.reason
