"""Synthetic pair removals, equity-first policy and existing builder/loader contracts."""

import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pytest
from dataset_builder import Row, build_dataset, render_input, write_dataset
from dataset_contracts import DatasetError, PartitionedExamples, digest
from dataset_exclusions import (
    CONSISTENCY_CRITERION,
    CRITERION,
    ConsistencyPolicy,
    FrozenExclusions,
    freeze_exclusions,
    load_exclusions,
    retained_indices,
    select_consistency_removals,
    verify_source,
)
from dataset_exclusions import main as freeze_main
from test_dataset_builder import fit_source, partitioned, row, valid_source

from fra_core.schemas import StatementType


def policy(dominance: str = "0.95", null_dominance: str = "0.90") -> ConsistencyPolicy:
    return ConsistencyPolicy(
        statements=("balance", "income"),
        dominance=Decimal(dominance),
        null_dominance=Decimal(null_dominance),
    )


def fixture(tmp_path: Path) -> tuple[PartitionedExamples, Path, Path]:
    # Every row has a distinct synthetic issuer; no within-issuer conflict/dedupe.
    specs: list[tuple[str, StatementType, str | None]] = [
        ("Equity", StatementType.BALANCE, "equity_attributable_parent"),
        ("Equity", StatementType.BALANCE, "total_equity"),
        ("Equity", StatementType.BALANCE, None),
        ("Sales", StatementType.INCOME, "revenue"),
        ("Sales", StatementType.INCOME, "revenue"),
        ("Sales", StatementType.INCOME, "cost_of_revenue"),
        ("Sales", StatementType.INCOME, None),
        ("Other", StatementType.BALANCE, None),
        ("Other", StatementType.BALANCE, None),
        ("Flows", StatementType.CASH_FLOW, None),
    ]
    fit = tuple(
        row(
            fit_source(i, period="annual" if i % 2 else "interim"),
            f"fit-{i:03}",
            label=label,
            statement=statement,
            canonical_id=target,
            abstention_reason="synthetic unmapped" if target is None else None,
        )
        for i, (label, statement, target) in enumerate(specs)
    )
    parts = partitioned(fit, (row(valid_source(), "valid"),))
    source = tmp_path / "source"
    write_dataset(build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture"), source)
    equity = tmp_path / "equity.json"
    equity.write_text(freeze_exclusions(source, provenance="synthetic equity").model_dump_json())
    return parts, source, equity


def freeze_v2(source: Path, equity: Path, **changes: object) -> FrozenExclusions:
    kwargs = {
        "criterion": CONSISTENCY_CRITERION,
        "statements": ("balance", "income"),
        "dominance": Decimal("0.60"),
        "null_dominance": Decimal("0.90"),
        "equity_exclusions": equity,
        "provenance": "synthetic training policy only",
    }
    kwargs.update(changes)
    return freeze_exclusions(source, **kwargs)


@pytest.mark.parametrize(
    "targets,dominance,null_dominance,kept,reason",
    [
        ([None, None], "0.95", "0.90", [None, None], None),
        (["revenue"] * 19 + ["cost_of_revenue", None], "0.95", "0.90", ["revenue"] * 19, "outlier"),
        (["revenue"] + [None] * 9, "0.95", "0.90", [None] * 9, "outlier"),
        (["revenue"] + [None] * 8, "0.95", "0.90", ["revenue"], "outlier"),
        (["revenue"] * 18 + ["cost_of_revenue"] * 2 + [None], "0.95", "0.90", [], "ambiguous"),
        (["revenue", "cost_of_revenue", None], "0.50", "0.90", [], "ambiguous"),
        (["revenue", "cost_of_revenue"] + [None] * 98, "0.95", "0.90", [], "ambiguous"),
        (
            ["revenue"] * 19 + ["cost_of_revenue"],
            "0.950000000000000000000000000001",
            "0.90",
            [],
            "ambiguous",
        ),
    ],
)
def test_policy_boundaries_and_no_relabeling(
    targets: list[str | None],
    dominance: str,
    null_dominance: str,
    kept: list[str | None],
    reason: str | None,
) -> None:
    key = render_input("Sales", "income")
    removals = select_consistency_removals(
        {key: Counter(targets)}, policy(dominance, null_dominance)
    )
    removed = {r.target for r in removals}
    assert [target for target in targets if target not in removed] == kept
    assert {r.reason for r in removals} == ({reason} if reason else set())
    assert sum(r.rows for r in removals) == len(targets) - len(kept)


def test_exact_complete_input_identity_and_statement_filter() -> None:
    a = render_input("Sales", "income")
    # Identical parsed payload, different complete string: these must never merge.
    b = '{ "statement": "income", "label": "Sales" }'
    outside = render_input("Flows", "cash_flow")
    removals = select_consistency_removals(
        {
            a: Counter({"revenue": 1}),
            b: Counter({"cost_of_revenue": 1}),
            outside: Counter({None: 2}),
        },
        policy(),
    )
    assert [(r.input_key, r.target, r.reason, r.rows) for r in removals] == [
        (outside, None, "statement", 2),
    ]


def test_equity_first_builder_statistics_single_audit_and_loader_binding(tmp_path: Path) -> None:
    parts, source, equity_path = fixture(tmp_path)
    artifact = freeze_v2(source, equity_path)
    assert artifact.version == 2
    assert artifact.selection_criterion == CONSISTENCY_CRITERION
    assert artifact.policy == policy("0.60")
    assert artifact.equity_artifact_sha256 == hashlib.sha256(equity_path.read_bytes()).hexdigest()
    assert artifact.removed_reasons == {"equity": 3, "outlier": 2, "statement": 1}
    assert artifact.removed_targets == {
        "equity_attributable_parent": 1,
        "total_equity": 1,
        "null": 3,
        "cost_of_revenue": 1,
    }
    assert (artifact.rows_before, artifact.removed_rows, artifact.rows_after) == (10, 6, 4)

    def count(messages: list[dict[str, str]]) -> int:
        return 7 if '"canonical_id":null' in messages[2]["content"] else 3

    original = build_dataset(parts, count_tokens=count, tokenizer_id="fixture")
    equity = load_exclusions(equity_path)
    v3 = build_dataset(parts, count_tokens=count, tokenizer_id="fixture", exclusions=equity)
    groups: defaultdict[str, Counter[str | None]] = defaultdict(Counter)
    for r in v3.fit_rows:
        groups[r["messages"][1]["content"]][
            json.loads(r["messages"][2]["content"])["canonical_id"]
        ] += 1
    policy_pairs = {
        (r.input_key, r.target) for r in select_consistency_removals(groups, artifact.policy)
    }
    expected = tuple(
        r
        for r in v3.fit_rows
        if (r["messages"][1]["content"], json.loads(r["messages"][2]["content"])["canonical_id"])
        not in policy_pairs
    )
    built = build_dataset(parts, count_tokens=count, tokenizer_id="fixture", exclusions=artifact)
    assert built.fit_rows == expected
    assert built.manifest.fit_lengths.model_dump() == {
        "count": 4,
        "max": 7,
        "mean": "5.000",
        "p95": 7,
    }
    assert built.manifest.fit_strata == {"sec|en|annual": 2, "sec|en|interim": 2}
    assert built.manifest.fit_rows_before == 10
    assert built.manifest.fit_rows_after == 4
    assert sum(built.manifest.fit_strata.values()) == 4
    assert built.manifest.exclusion == artifact.audit()
    assert set(built.manifest.exclusion.model_dump()) == {
        "keys_sha256",
        "source_train_sha256",
        "rows_before",
        "rows_after",
        "removed_rows",
        "removed_targets",
    }
    assert built.validation_rows == original.validation_rows
    assert built.manifest.valid_sha256 == original.manifest.valid_sha256
    verify_source(artifact, original.manifest)
    verify_source(artifact, built.manifest, allow_applied=True)
    with pytest.raises(DatasetError, match="already applied"):
        verify_source(artifact, built.manifest)
    write_dataset(built, tmp_path / "filtered")
    assert (
        hashlib.sha256((tmp_path / "filtered/train.jsonl").read_bytes()).hexdigest()
        == built.manifest.train_sha256
    )
    assert (tmp_path / "filtered/valid.jsonl").read_bytes() == (source / "valid.jsonl").read_bytes()


def test_freeze_is_deterministic_and_reads_only_train_manifest_equity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, equity = fixture(tmp_path)
    original_open = Path.open
    allowed = {source / "train.jsonl", source / "manifest.json", equity}

    def guard(path: Path, *args: object, **kwargs: object):
        assert path in allowed, f"unexpected freeze read: {path}"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard)
    a = freeze_v2(source, equity)
    b = freeze_v2(source, equity)
    assert a.model_dump_json() == b.model_dump_json()


def cli(source: Path, equity: Path, out: Path) -> list[str]:
    return [
        "--dataset-dir",
        str(source),
        "--out",
        str(out),
        "--provenance",
        "synthetic",
        "--criterion",
        CONSISTENCY_CRITERION,
        "--equity-exclusions",
        str(equity),
        "--statements",
        "income,balance",
        "--dominance",
        "0.60",
        "--null-dominance",
        "0.90",
    ]


def test_v2_cli_roundtrip_refuses_existing_output(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    out = tmp_path / "v2.json"
    command = cli(source, equity, out)
    assert freeze_main(command) == 0
    original = out.read_bytes()
    assert load_exclusions(out) == freeze_v2(source, equity, provenance="synthetic")
    assert freeze_main(command) == 1
    assert out.read_bytes() == original


@pytest.mark.parametrize(
    "missing", ["--statements", "--dominance", "--null-dominance", "--equity-exclusions"]
)
def test_cli_requires_explicit_v2_parameters(tmp_path: Path, missing: str) -> None:
    command = cli(tmp_path / "missing", tmp_path / "missing-equity", tmp_path / "out")
    index = command.index(missing)
    del command[index : index + 2]
    with pytest.raises(SystemExit) as exc:
        freeze_main(command)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"statements": ("unknown",)},
        {"statements": ()},
        {"statements": ("income", "income")},
        {"dominance": Decimal("0")},
        {"dominance": Decimal("NaN")},
        {"null_dominance": Decimal("1.01")},
    ],
)
def test_invalid_policy_refused_before_source_access(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(DatasetError):
        freeze_v2(tmp_path / "absent", tmp_path / "absent-equity", **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("selection_criterion", "invented"),
        ("source_train_sha256", "f" * 64),
        ("source_valid_sha256", "f" * 64),
        ("keys_sha256", "f" * 64),
        ("equity_artifact_sha256", "f" * 64),
        ("keys", ("tampered",)),
        ("policy", policy("0.70")),
        ("removed_reasons", {"equity": 6}),
        ("removed_targets", {"null": 6}),
        ("rows_after", 3),
    ],
)
def test_v2_tampering_is_refused(tmp_path: Path, field: str, value: object) -> None:
    parts, source, equity = fixture(tmp_path)
    artifact = freeze_v2(source, equity).model_copy(update={field: value})
    with pytest.raises(DatasetError):
        build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=artifact)


def test_pair_coverage_and_actual_removal_counts_are_checked(tmp_path: Path) -> None:
    parts, source, equity = fixture(tmp_path)
    artifact = freeze_v2(source, equity)
    built = build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture")
    rows = list(built.fit_rows)
    # Same total, but duplicate one removal pair and omit another.
    rows[1] = rows[0]
    with pytest.raises(DatasetError, match="removal count, target breakdown"):
        retained_indices(rows, artifact)
    with pytest.raises(DatasetError):
        retained_indices(rows[:-1], artifact)


def rewrite_train(source: Path, rows: list[Row]) -> None:
    data = b"".join((json.dumps(r) + "\n").encode() for r in rows)
    (source / "train.jsonl").write_bytes(data)
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["train_sha256"] = hashlib.sha256(data).hexdigest()
    (source / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "mutation",
    [
        "messages",
        "role",
        "prompt",
        "target",
        "target_extra",
        "statement",
        "target_statement",
        "label",
        "input_extra",
        "input_duplicate",
    ],
)
def test_strict_train_contracts(tmp_path: Path, mutation: str) -> None:
    _, source, equity = fixture(tmp_path)
    rows = [json.loads(line) for line in (source / "train.jsonl").read_text().splitlines()]
    messages = rows[-1]["messages"]
    if mutation == "messages":
        messages.pop()
    elif mutation == "role":
        messages[1]["role"] = "assistant"
    elif mutation == "prompt":
        messages[0]["content"] = "wrong"
    elif mutation == "target":
        messages[2]["content"] = '{"canonical_id":"unknown"}'
    elif mutation == "target_extra":
        messages[2]["content"] = '{"canonical_id":null,"extra":1}'
    elif mutation == "statement":
        messages[1]["content"] = '{"label":"Flows","statement":"unknown"}'
    elif mutation == "target_statement":
        messages[2]["content"] = '{"canonical_id":"revenue"}'
    elif mutation == "label":
        messages[1]["content"] = '{"label":" ","statement":"income"}'
    elif mutation == "input_extra":
        messages[1]["content"] = '{"label":"Flows","statement":"cash_flow","extra":1}'
    else:
        messages[1]["content"] = '{"label":"A","label":"B","statement":"cash_flow"}'
    rewrite_train(source, rows)
    # Bind equity to this synthetic source so row validation, not stale source hashes, rejects it.
    raw = json.loads(equity.read_text())
    raw["source_train_sha256"] = hashlib.sha256((source / "train.jsonl").read_bytes()).hexdigest()
    equity.write_text(json.dumps(raw))
    with pytest.raises(DatasetError, match=r"train.jsonl:10:"):
        freeze_v2(source, equity)


def test_equity_source_mismatch_and_train_hash_mismatch(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    original = equity.read_text()
    raw = json.loads(original)
    raw["source_train_sha256"] = "f" * 64
    equity.write_text(json.dumps(raw))
    with pytest.raises(DatasetError, match="source train sha256"):
        freeze_v2(source, equity)
    equity.write_text(original)
    with (source / "train.jsonl").open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(DatasetError, match="sha256"):
        freeze_v2(source, equity)


def test_v1_contract_remains_whole_key_and_constant_criterion(tmp_path: Path) -> None:
    parts, _, equity = fixture(tmp_path)
    artifact = load_exclusions(equity)
    assert artifact.version == 1 and artifact.selection_criterion == CRITERION
    assert "policy" not in artifact.model_dump(mode="json")
    assert "removals" not in artifact.model_dump(mode="json")
    assert artifact.keys_sha256 == digest(artifact.keys)
    built = build_dataset(
        parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=artifact
    )
    assert len(built.fit_rows) == 7
    assert all(
        r["messages"][1]["content"] != render_input("Equity", "balance") for r in built.fit_rows
    )
    with pytest.raises(ValueError):
        FrozenExclusions.model_validate(
            artifact.model_copy(update={"selection_criterion": CONSISTENCY_CRITERION})
        )


def test_loader_uses_unchanged_exclusion_contract(tmp_path: Path) -> None:
    from dataset_builder import DatasetBuildManifest

    parts, source, equity = fixture(tmp_path)
    artifact = freeze_v2(source, equity)
    built = build_dataset(
        parts, count_tokens=lambda _: 1, tokenizer_id="fixture", exclusions=artifact
    )
    # The loader consumes a saved artifact and already-applied manifest, without rebuilding.
    saved = tmp_path / "loader-exclusions.json"
    saved.write_text(artifact.model_dump_json())
    manifest = DatasetBuildManifest.model_validate_json(built.manifest.model_dump_json())
    loaded = load_exclusions(saved)
    verify_source(loaded, manifest, allow_applied=True)
    assert loaded.audit() == manifest.exclusion
    assert manifest.train_sha256 != loaded.source_train_sha256
    with pytest.raises(DatasetError, match="already applied"):
        verify_source(loaded, manifest)
    with pytest.raises(DatasetError, match="already applied or differ"):
        verify_source(
            loaded,
            manifest.model_copy(
                update={"exclusion": loaded.audit().model_copy(update={"keys_sha256": "f" * 64})}
            ),
            allow_applied=True,
        )


@pytest.mark.parametrize(
    "option,value",
    [
        ("--dominance", "bad"),
        ("--null-dominance", "bad"),
    ],
)
def test_cli_malformed_threshold_is_a_usage_error(tmp_path: Path, option: str, value: str) -> None:
    command = cli(tmp_path / "absent", tmp_path / "absent-equity", tmp_path / "out")
    command[command.index(option) + 1] = value
    with pytest.raises(SystemExit) as exc:
        freeze_main(command)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "field",
    [
        "policy",
        "removals",
        "equity_exclusions",
        "equity_artifact_sha256",
        "removed_reasons",
    ],
)
def test_v1_still_rejects_unknown_v2_fields_even_null(tmp_path: Path, field: str) -> None:
    _, _, equity = fixture(tmp_path)
    raw = json.loads(equity.read_bytes())
    raw[field] = None
    equity.write_text(json.dumps(raw))
    with pytest.raises(DatasetError):
        load_exclusions(equity)


def test_entry_tampering_duplicate_pairs_and_missing_keys_fail(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    artifact = freeze_v2(source, equity)
    assert artifact.removals is not None
    for removals in (
        artifact.removals[:-1],
        (*artifact.removals, artifact.removals[0]),
        (*artifact.removals[:-1], artifact.removals[-1].model_copy(update={"reason": "equity"})),
        (*artifact.removals[:-1], artifact.removals[-1].model_copy(update={"rows": 99})),
    ):
        bad = artifact.model_copy(update={"removals": removals})
        # Rehashing is not enough to bypass the structural reconciliation checks.
        bad = bad.model_copy(
            update={"keys_sha256": digest(bad.model_dump(mode="json", exclude={"keys_sha256"}))}
        )
        with pytest.raises(ValueError):
            FrozenExclusions.model_validate(bad)


def test_reason_precedence_and_deterministic_pair_order() -> None:
    a, b = render_input("A", "income"), render_input("B", "balance")
    groups = {a: Counter({None: 10, "revenue": 1, "cost_of_revenue": 1}), b: Counter({None: 2})}
    first = select_consistency_removals(groups, policy())
    second = select_consistency_removals(dict(reversed(list(groups.items()))), policy())
    assert first == second
    assert all(r.input_key == a and r.reason == "ambiguous" for r in first)


def test_v1_synthetic_artifact_and_retained_bytes_match_pinned_contract(tmp_path: Path) -> None:
    parts, _, equity = fixture(tmp_path)
    contract = json.loads(
        (Path(__file__).resolve().parent / "fixtures/exclusions-v1-contract.json").read_bytes()
    )
    current = load_exclusions(equity)
    assert current.model_dump_json() == contract["artifact_json"]
    built = build_dataset(parts, count_tokens=lambda _: 1, tokenizer_id="fixture")
    indices = retained_indices(built.fit_rows, current)
    assert list(indices) == contract["retained_indices"]
    data = b"".join((json.dumps(built.fit_rows[i]) + "\n").encode() for i in indices)
    assert hashlib.sha256(data).hexdigest() == contract["retained_bytes_sha256"]


def test_original_v1_cli_needs_no_new_flags(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    out = tmp_path / "v1-cli.json"
    assert (
        freeze_main(
            [
                "--dataset-dir",
                str(source),
                "--out",
                str(out),
                "--provenance",
                "synthetic equity",
            ]
        )
        == 0
    )
    assert load_exclusions(out).model_dump_json() == load_exclusions(equity).model_dump_json()


def test_v2_reads_train_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, equity = fixture(tmp_path)
    original_open = Path.open
    opens = 0

    def counted(path: Path, *args: object, **kwargs: object):
        nonlocal opens
        if path == source / "train.jsonl":
            opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    freeze_v2(source, equity)
    assert opens == 1


def test_v2_rejects_changed_valid_bytes_at_eof(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    train = source / "train.jsonl"
    # Valid JSON and unchanged rows; only the complete byte hash exposes this mutation.
    train.write_bytes(train.read_bytes().rstrip(b"\n") + b" \n")
    with pytest.raises(DatasetError, match=r"train\.jsonl: source sha256"):
        freeze_v2(source, equity)


def test_v2_cannot_bind_removals_to_bytes_reopened_after_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source, equity = fixture(tmp_path)
    original_digest = hashlib.file_digest
    original = (source / "train.jsonl").read_bytes()
    mutated = original.replace(b"Sales", b"Trade")
    assert mutated != original

    def mutate_after_hash(handle, name):
        value = original_digest(handle, name)
        (source / "train.jsonl").write_bytes(mutated)
        return value

    monkeypatch.setattr(hashlib, "file_digest", mutate_after_hash)
    artifact = freeze_v2(source, equity)
    assert any(r.input_key == render_input("Sales", "income") for r in artifact.removals)
    assert all(r.input_key != render_input("Trade", "income") for r in artifact.removals)


def test_missing_equity_key_is_a_coverage_error(tmp_path: Path) -> None:
    _, source, equity = fixture(tmp_path)
    raw = json.loads(equity.read_bytes())
    raw["keys"] = [render_input("Absent equity", "balance")]
    raw["keys_sha256"] = digest(raw["keys"])
    equity.write_text(json.dumps(raw))
    with pytest.raises(DatasetError, match=r"equity.*key coverage"):
        freeze_v2(source, equity)


@pytest.mark.parametrize("dominance, ambiguous_rows", [("0.60", 0), ("0.95", 3)])
def test_v2_report_uses_only_ambiguity_reasons(
    tmp_path: Path, dominance: str, ambiguous_rows: int
) -> None:
    from normalization_eval import population_report, score
    from test_normalization_eval import prediction, sample

    _, source, equity = fixture(tmp_path)
    artifact = freeze_v2(source, equity, dominance=Decimal(dominance))
    specs = [
        ("Equity", StatementType.BALANCE, "total_equity"),
        ("Sales", StatementType.INCOME, "revenue"),
        ("Sales", StatementType.INCOME, "cost_of_revenue"),
        ("Sales", StatementType.INCOME, None),
        ("Flows", StatementType.CASH_FLOW, None),
        ("Other", StatementType.BALANCE, None),
    ]
    rows = tuple(
        sample(str(i), target).model_copy(update={"statement": statement, "label": label})
        for i, (label, statement, target) in enumerate(specs)
    )
    predictions = tuple(prediction(r.example_id, None) for r in rows)
    report = population_report(rows, predictions, artifact)
    assert report["full_validation"]["score"] == score(rows, predictions)
    assert report["full_validation"]["rows"] == len(rows)
    assert report["ambiguity_only"]["rows"] == ambiguous_rows
    assert report["validation_excluding_ambiguity"]["rows"] == len(rows) - ambiguous_rows
    inputs = report["ambiguity_only"]["inputs"]
    assert [i["input"] for i in inputs] == (
        [render_input("Sales", "income")] if ambiguous_rows else []
    )


@pytest.mark.parametrize("version", [1, 2])
def test_report_names_key_list_and_content_digests(tmp_path: Path, version: int) -> None:
    from normalization_eval import population_report
    from test_normalization_eval import prediction, sample

    _, source, equity = fixture(tmp_path)
    artifact = load_exclusions(equity) if version == 1 else freeze_v2(source, equity)
    report = population_report((sample("a", None),), (prediction("a", None),), artifact)
    assert report["exclusion_keys_sha256"] == digest(artifact.keys)
    if version == 2:
        assert report["exclusion_artifact_content_sha256"] == artifact.keys_sha256
    else:
        assert "exclusion_artifact_content_sha256" not in report
    assert artifact.audit().keys_sha256 == artifact.keys_sha256


@pytest.mark.parametrize("failure", ["partial_write", "sync", "link", "race", "success"])
def test_cli_publishes_complete_bytes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import os

    _, source, equity = fixture(tmp_path)
    out = tmp_path / "published/v2.json"
    expected = (
        freeze_v2(source, equity, provenance="synthetic").model_dump_json(indent=2) + "\n"
    ).encode()
    original_link = os.link
    links = []

    def publish(src, dst):
        src, dst = Path(src), Path(dst)
        links.append(src)
        assert src.parent == out.parent and src != out
        assert src.read_bytes() == expected
        assert not dst.exists()
        if failure == "race":
            dst.write_bytes(b"competing artifact")
        if failure == "link":
            raise OSError("injected link failure")
        original_link(src, dst)

    monkeypatch.setattr(os, "link", publish)
    if failure == "sync":

        def fail_sync(descriptor):
            assert not out.exists()
            raise OSError("injected sync failure")

        monkeypatch.setattr(os, "fsync", fail_sync)
    if failure == "partial_write":
        original_fdopen = os.fdopen

        class PartialWrite:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[:8])
                self.handle.flush()
                assert not out.exists()
                raise OSError("injected partial write")

        monkeypatch.setattr(os, "fdopen", lambda *a, **k: PartialWrite(original_fdopen(*a, **k)))
    result = freeze_main(cli(source, equity, out))
    assert result == (0 if failure == "success" else 1)
    assert list(out.parent.iterdir()) == ([out] if failure in {"race", "success"} else [])
    if failure == "success":
        assert links
        assert out.read_bytes() == expected
    elif failure == "race":
        assert out.read_bytes() == b"competing artifact"
    else:
        assert not out.exists()
