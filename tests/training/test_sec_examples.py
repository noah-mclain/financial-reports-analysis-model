"""Prepared payloads to approved training examples to dataset files, end to end and synthetic."""

import json
import shutil
from pathlib import Path

import pytest
from build_dataset import main as dataset_main
from sec_crosswalk_cli import main as crosswalk_main
from sec_crosswalk_fixtures import committed_mapping, synthetic_vocabulary, tag_line
from sec_crosswalk_fixtures import write_archive as write_tag_archive
from sec_source_cli import main
from test_chat_tokenizer import make_tokenizer
from test_sec_source_pass import (
    CANARY,
    World,
    adsh,
    pre_line,
    sub_line,
    write_archive,
)

REVIEWER = "reviewer-agent-1"
AUTHORITY = "owner-sample-check"


def scenario_rows() -> list[bytes]:
    custom = f"{adsh(1)}\t3\t1\tBS\t0\tH\tFilerTag\t{adsh(1)}\tFiler line\t0\n".encode()
    return [
        pre_line(1, 1, "Total assets"),
        pre_line(1, 2, "Net sales", stmt="IS", tag="Revenues"),
        pre_line(1, 3, "Net income", stmt="IS", tag="NetIncomeLoss"),
        pre_line(1, 4, "(parenthetical)", inpth="1"),
        pre_line(1, 5, "Schedule", stmt="SI"),
        pre_line(1, 6, "Fresh A", tag="NewTagA"),
        custom,
        pre_line(5, 1, "Total assets"),
        pre_line(5, 2, "NET   SALES", stmt="IS", tag="Revenues"),
        pre_line(2, 1, "Total assets"),
        pre_line(2, 2, "Fresh C", stmt="IS", tag="NewTagC"),
        pre_line(2, 3, "Cash flow", stmt="CF", tag="NewTagD"),
        pre_line(3, 1, CANARY),
        pre_line(4, 1, CANARY),
    ]


def verdict_for(row: dict[str, object]) -> dict[str, object]:
    if row["kind"] in ("subjects", "roles-subjects"):
        return row
    return {"key": row["key"], "verdict": "approve", "note": ""}


class Pipeline:
    """Runs prepare, the crosswalk and role approvals, and builds the trust directory."""

    def __init__(self, root: Path, rows: list[bytes] | None = None) -> None:
        self.root = root
        self.world = World(root)
        w = self.world
        write_archive(
            w.archive,
            [
                sub_line(1, w.fit, "Fit Co"),
                sub_line(2, w.valid, "Valid Co", "10-Q"),
                sub_line(3, w.hold, "Hold Co"),
                sub_line(4, w.test, "Test Co"),
                sub_line(5, w.fit, "Fit Co", "10-Q"),
            ],
            rows if rows is not None else scenario_rows(),
        )
        self.prepared = root / "prepared"
        assert main(self.prepare_args()) == 0
        self.draft = root / "draft"
        mapping = committed_mapping()
        vocabulary = synthetic_vocabulary(mapping)
        self.tags = write_tag_archive(
            root / "tags.zip",
            [
                tag_line(d.tag, d.version, tlabel=d.tlabel, doc=d.doc)
                for t in vocabulary.values()
                for d in t.values()
            ],
        )
        assert (
            crosswalk_main(["draft", "--archives", str(self.tags), "--out-dir", str(self.draft)])
            == 0
        )
        self.approved = self.approve_crosswalk()
        self.roles = self.roles_flow()
        self.trust = root / "trust"
        self.trust.mkdir()
        for source, target in (
            (self.approved / "reviewed_crosswalk.json", "reviewed_crosswalk.json"),
            (self.approved / "reviewed_policies.json", "reviewed_policies.json"),
            (self.approved / "approvals.json", "approvals.json"),
            (self.roles / "reviewed_role_assessments.json", "reviewed_role_assessments.json"),
            (self.roles / "role_approvals.json", "role_approvals.json"),
        ):
            shutil.copy(source, self.trust / target)
        (self.trust / "authorities.json").write_text(json.dumps([AUTHORITY]), encoding="utf-8")

    def prepare_args(self) -> list[str]:
        w = self.world
        return [
            "prepare",
            "--archives",
            str(w.archive),
            "--out-dir",
            str(self.prepared),
            "--authorization",
            "owner message, test",
            "--registry",
            str(w.registry_path),
            "--moves",
            str(w.moves_path),
            "--looks",
            str(w.looks_path),
        ]

    def approve_crosswalk(self) -> Path:
        packet = [
            json.loads(x)
            for x in (self.draft / "review_packet.jsonl").read_text("utf-8").splitlines()
        ]
        verdicts = self.root / "verdicts.jsonl"
        verdicts.write_text(
            "".join(json.dumps(verdict_for(r)) + "\n" for r in packet), encoding="utf-8"
        )
        out = self.root / "approved"
        assert (
            crosswalk_main(
                [
                    "approve",
                    "--crosswalk",
                    str(self.draft / "crosswalk.json"),
                    "--policies",
                    str(self.draft / "policies.json"),
                    "--verdicts",
                    str(verdicts),
                    "--reviewer-id",
                    REVIEWER,
                    "--authority-id",
                    AUTHORITY,
                    "--evidence-reference",
                    "verdicts.jsonl",
                    "--out-dir",
                    str(out),
                ]
            )
            == 0
        )
        return out

    def roles_flow(self) -> Path:
        w = self.world
        draft = self.root / "roles-draft"
        common = ["--prepared", str(self.prepared), "--archives", str(w.archive)]
        assert main(["roles-draft", *common, "--out-dir", str(draft)]) == 0
        packet = [
            json.loads(x) for x in (draft / "role_packet.jsonl").read_text("utf-8").splitlines()
        ]
        verdicts = self.root / "role-verdicts.jsonl"
        verdict = {
            "key": f"rule-table:{packet[0]['rule_table_sha256']}",
            "verdict": "approve",
            "note": "",
        }
        verdicts.write_text(
            json.dumps(packet[0]) + "\n" + json.dumps(verdict) + "\n", encoding="utf-8"
        )
        out = self.root / "roles-approved"
        code = main(
            [
                "roles-approve",
                *common,
                "--assessments",
                str(draft / "role_assessments.json"),
                "--verdicts",
                str(verdicts),
                "--reviewer-id",
                "role-reviewer-1",
                "--authority-id",
                AUTHORITY,
                "--evidence-reference",
                "role-verdicts.jsonl",
                "--out-dir",
                str(out),
            ]
        )
        assert code == 0
        return out

    def examples_args(self, out: Path, trust: Path | None = None) -> list[str]:
        w = self.world
        return [
            "examples",
            "--prepared",
            str(self.prepared),
            "--trust-dir",
            str(trust or self.trust),
            "--out-dir",
            str(out),
            "--registry",
            str(w.registry_path),
            "--moves",
            str(w.moves_path),
            "--looks",
            str(w.looks_path),
        ]


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    return Pipeline(tmp_path_factory.mktemp("sec-examples"))


def test_roles_are_assigned_from_the_sic_rule(pipeline: Pipeline) -> None:
    roles = json.loads((pipeline.trust / "reviewed_role_assessments.json").read_text("utf-8"))
    assert {r["source_id"]: r["role"] for r in roles} == {
        adsh(1): "corporate",
        adsh(2): "corporate",
        adsh(5): "corporate",
    }
    assert {r["review"]["status"] for r in roles} == {"reviewed"}


def test_examples_are_resolved_deduplicated_and_counted(
    pipeline: Pipeline, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "examples"
    assert main(pipeline.examples_args(out)) == 0
    captured = capsys.readouterr()
    assert "sec-examples started" in captured.err
    assert "sec-examples issuer batch 1/1\n" in captured.err
    assert "sec-examples" not in captured.out
    assert sorted(p.name for p in out.iterdir()) == [
        "approved.txt",
        "counts.json",
        "examples.jsonl",
        "sources.jsonl",
    ]
    counts = json.loads((out / "counts.json").read_text(encoding="utf-8"))
    assert counts["rows_read"] == 12
    assert counts["dropped"] == {
        "ambiguous_semantics": 1,
        "parenthetical": 1,
        "unsupported_statement_code": 1,
    }
    assert counts["resolved"] == {"abstain": 4, "mapped": 5}
    assert counts["rows_before_dedupe"] == 9 and counts["rows_after_dedupe"] == 7
    assert counts["conflicting_groups"] == 0
    assert counts["examples"] == 7 and counts["filings"] == 3 and counts["issuers"] == 2
    assert counts["peak_rss_bytes"] > 0
    examples = [json.loads(x) for x in (out / "examples.jsonl").read_text("utf-8").splitlines()]
    by_id = {e["example_id"]: e for e in examples}
    assert by_id[f"{adsh(1)}:2:1"]["canonical_id"] == "total_assets"
    assert by_id[f"{adsh(1)}:2:1"]["months"] is None
    assert by_id[f"{adsh(1)}:2:1"]["tag"] == "us-gaap:Assets"
    assert by_id[f"{adsh(1)}:2:1"]["verification"]["method"] == "reviewed_crosswalk"
    assert by_id[f"{adsh(1)}:2:1"]["verification"]["reviewer_id"] == REVIEWER
    abstained = by_id[f"{adsh(2)}:2:3"]
    assert abstained["canonical_id"] is None and abstained["statement"] == "cash_flow"
    assert abstained["abstention_reason"]
    custom = next(e for e in examples if e["tag"] == "custom:FilerTag")
    assert custom["canonical_id"] is None
    assert not [e for e in examples if e["source_record"]["source_id"] == adsh(5)]
    approved = (out / "approved.txt").read_text(encoding="utf-8").split()
    assert len(approved) == 7 == len(set(approved))
    sources = [json.loads(x) for x in (out / "sources.jsonl").read_text("utf-8").splitlines()]
    assert sorted(s["source_id"] for s in sources) == [adsh(1), adsh(2), adsh(5)]
    assert all(len(s["source_sha256"]) == 64 for s in sources)
    text = "".join(p.read_text("utf-8") for p in out.iterdir())
    assert CANARY not in text and "Hold Co" not in text and "Test Co" not in text


def test_the_batch_size_changes_nothing(pipeline: Pipeline, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "many"
    assert main([*pipeline.examples_args(first), "--batch-issuers", "1"]) == 0
    assert main([*pipeline.examples_args(second), "--batch-issuers", "100"]) == 0
    for name in ("examples.jsonl", "sources.jsonl", "approved.txt"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_the_output_directory_is_never_reused(pipeline: Pipeline, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "examples"
    assert main(pipeline.examples_args(out)) == 0
    capsys.readouterr()
    assert main(pipeline.examples_args(out)) == 1
    assert "already exists" in capsys.readouterr().err


def test_dataset_build_reads_what_the_adapter_wrote(pipeline: Pipeline, tmp_path: Path) -> None:
    out = tmp_path / "examples"
    assert main(pipeline.examples_args(out)) == 0
    tokenizer = make_tokenizer(tmp_path / "tokenizer")
    dataset = tmp_path / "dataset"
    w = pipeline.world
    code = dataset_main(
        [
            "--examples",
            str(out / "examples.jsonl"),
            "--sources",
            str(out / "sources.jsonl"),
            "--approved",
            str(out / "approved.txt"),
            "--registry",
            str(w.registry_path),
            "--moves",
            str(w.moves_path),
            "--looks",
            str(w.looks_path),
            "--tokenizer-dir",
            str(tokenizer),
            "--out-dir",
            str(dataset),
        ]
    )
    assert code == 0
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fit_rows_after"] == 4 and manifest["validation_rows_after"] == 3
    train = (dataset / "train.jsonl").read_text(encoding="utf-8")
    assert CANARY not in train and "Hold Co" not in train
    # The holdout and model_test filers were never prepared, so they cannot appear.
    assert adsh(3) not in train + (dataset / "valid.jsonl").read_text(encoding="utf-8")


def test_draft_artifacts_are_refused(
    pipeline: Pipeline, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trust = tmp_path / "draft-trust"
    shutil.copytree(pipeline.trust, trust)
    shutil.copy(pipeline.draft / "crosswalk.json", trust / "reviewed_crosswalk.json")
    shutil.copy(pipeline.draft / "policies.json", trust / "reviewed_policies.json")
    assert main(pipeline.examples_args(tmp_path / "out", trust)) == 1
    assert "draft" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        ("approvals.json", "approval"),
        ("role_approvals.json", "approval"),
        ("authorities.json", "authorities"),
        ("reviewed_role_assessments.json", "reviewed_role_assessments"),
    ],
)
def test_missing_or_incomplete_trust_is_refused(
    pipeline: Pipeline, tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str, match: str
) -> None:
    trust = tmp_path / "trust"
    shutil.copytree(pipeline.trust, trust)
    (trust / missing).unlink()
    assert main(pipeline.examples_args(tmp_path / "out", trust)) == 1
    assert match in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_role_whose_approval_is_absent_is_refused(
    pipeline: Pipeline, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trust = tmp_path / "trust"
    shutil.copytree(pipeline.trust, trust)
    records = json.loads((trust / "role_approvals.json").read_text(encoding="utf-8"))
    (trust / "role_approvals.json").write_text(json.dumps(records[:-1]), encoding="utf-8")
    assert main(pipeline.examples_args(tmp_path / "out", trust)) == 1
    assert "no trusted approval" in capsys.readouterr().err


def conflict_rows(unique_labels: int) -> list[bytes]:
    # The same label under a mapped tag and under an unlisted tag cannot supervise either.
    return [
        pre_line(1, 1, "Other items", tag="Assets"),
        pre_line(5, 1, "Other items", tag="NewTagA"),
        *(pre_line(2, n, f"Distinct label {n}") for n in range(1, unique_labels + 1)),
    ]


def test_many_conflicts_stop_the_run_unless_a_number_is_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pipe = Pipeline(tmp_path, conflict_rows(3))
    capsys.readouterr()
    assert main(pipe.examples_args(tmp_path / "refused")) == 1
    err = capsys.readouterr().err
    assert "conflicting" in err and "allow-conflicts-up-to" in err
    assert not (tmp_path / "refused").exists()
    assert main([*pipe.examples_args(tmp_path / "short"), "--allow-conflicts-up-to", "1"]) == 1
    assert not (tmp_path / "short").exists()
    out = tmp_path / "allowed"
    assert main([*pipe.examples_args(out), "--allow-conflicts-up-to", "2"]) == 0
    shown = capsys.readouterr().out
    assert "conflicting_groups 1" in shown and "conflicting_rows 2" in shown
    counts = json.loads((out / "counts.json").read_text(encoding="utf-8"))
    assert counts["conflicting_groups"] == 1 and counts["conflicting_rows"] == 2
    assert counts["examples"] == 3


def test_a_small_share_of_conflicts_passes_and_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pipe = Pipeline(tmp_path, conflict_rows(60))
    capsys.readouterr()
    assert main(pipe.examples_args(tmp_path / "examples")) == 0
    shown = capsys.readouterr().out
    assert "conflicting_groups 1" in shown and "conflicting_rows 2" in shown


def test_a_failed_publish_leaves_no_mixed_directory(
    pipeline: Pipeline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.rename
    calls: list[Path] = []

    def flaky(self: Path, target: Path) -> Path:
        calls.append(self)
        if len(calls) == 3:
            raise OSError("injected rename failure")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    assert main(pipeline.examples_args(tmp_path / "out")) == 1
    assert len(calls) >= 3
    assert not (tmp_path / "out").exists()


def test_roles_draft_leaves_no_first_file_when_the_second_exists(
    pipeline: Pipeline, tmp_path: Path
) -> None:
    out = tmp_path / "roles"
    out.mkdir()
    (out / "role_packet.jsonl").write_text("keep", encoding="utf-8")
    common = ["--prepared", str(pipeline.prepared), "--archives", str(pipeline.world.archive)]
    assert main(["roles-draft", *common, "--out-dir", str(out)]) == 1
    assert [p.name for p in out.iterdir()] == ["role_packet.jsonl"]


def test_coverage_leaves_no_report_when_the_ranking_file_exists(
    pipeline: Pipeline, tmp_path: Path
) -> None:
    w = pipeline.world
    ranking = tmp_path / "unlisted.jsonl"
    ranking.write_text("keep", encoding="utf-8")
    report = tmp_path / "report.json"
    code = main(
        [
            "coverage",
            "--prepared",
            str(pipeline.prepared),
            "--crosswalk",
            str(pipeline.draft / "crosswalk.json"),
            "--archives",
            str(pipeline.tags),
            "--registry",
            str(w.registry_path),
            "--moves",
            str(w.moves_path),
            "--looks",
            str(w.looks_path),
            "--out",
            str(report),
            "--unlisted-out",
            str(ranking),
        ]
    )
    assert code == 1
    assert not report.exists() and ranking.read_text(encoding="utf-8") == "keep"
