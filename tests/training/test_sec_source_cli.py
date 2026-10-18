"""The prepare and coverage commands on a synthetic world."""

import json
from pathlib import Path

import pytest
from sec_crosswalk_cli import main as crosswalk_main
from sec_crosswalk_fixtures import committed_mapping, synthetic_vocabulary, tag_line
from sec_crosswalk_fixtures import write_archive as write_tag_archive
from sec_source_cli import main
from test_sec_source_decoder import coverage_rows
from test_sec_source_pass import World, sub_line, write_archive


def prepare_args(world: World, out: Path) -> list[str]:
    return [
        "prepare",
        "--archives", str(world.archive),
        "--out-dir", str(out),
        "--authorization", "owner message, test",
        "--registry", str(world.registry_path),
        "--moves", str(world.moves_path),
        "--looks", str(world.looks_path),
    ]  # fmt: skip


def test_prepare_runs_the_pass_and_prints_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    out = tmp_path / "prepared"
    assert main(prepare_args(world, out)) == 0
    captured = capsys.readouterr()
    shown = captured.out
    assert "sec-prepare 2024q3 started" in captured.err
    assert "sec-prepare 2024q3 finished" in captured.err
    assert "sec-prepare" not in shown
    assert "routed fit 1, validation 1" in shown
    assert str(out / "pass-log.json") in shown
    assert (
        json.loads((out / "pass-log.json").read_text(encoding="utf-8"))["totals"]["kept_rows"] == 3
    )


def test_prepare_errors_are_one_line_and_exit_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    out = tmp_path / "prepared"
    assert main(prepare_args(world, out)) == 0
    capsys.readouterr()
    assert main(prepare_args(world, out)) == 1
    err = capsys.readouterr().err
    assert "already exists" in err and "Traceback" not in err
    missing = [*prepare_args(world, tmp_path / "other")]
    missing[missing.index("--registry") + 1] = str(tmp_path / "absent.json")
    assert main(missing) == 1
    assert "absent.json" in capsys.readouterr().err


def test_coverage_reports_counts_and_unlisted_tags_from_prepared_payloads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    write_archive(
        world.archive,
        [sub_line(1, world.fit, "Fit Co"), sub_line(2, world.valid, "Valid Co", "10-Q")],
        coverage_rows(),
    )
    out = tmp_path / "prepared"
    assert main(prepare_args(world, out)) == 0
    mapping = committed_mapping()
    vocabulary = synthetic_vocabulary(mapping)
    tags = write_tag_archive(
        tmp_path / "tags.zip",
        [
            tag_line(d.tag, d.version, tlabel=d.tlabel, doc=d.doc)
            for t in vocabulary.values()
            for d in t.values()
        ],
    )
    draft = tmp_path / "draft"
    assert crosswalk_main(["draft", "--archives", str(tags), "--out-dir", str(draft)]) == 0
    capsys.readouterr()
    report = tmp_path / "coverage.json"
    argv = [
        "coverage",
        "--prepared", str(out),
        "--crosswalk", str(draft / "crosswalk.json"),
        "--archives", str(tags),
        "--registry", str(world.registry_path),
        "--moves", str(world.moves_path),
        "--looks", str(world.looks_path),
        "--top", "5",
        "--out", str(report),
        "--unlisted-out", str(tmp_path / "unlisted.jsonl"),
    ]  # fmt: skip
    assert main(argv) == 0
    assert "sec-coverage started" in capsys.readouterr().err
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["rows"] == 12
    assert data["by_kind"] == {"custom_extension": 1, "mapped": 3, "outside_crosswalk": 5}
    assert data["drops"]["parenthetical"] == 1
    assert [r["tag"] for r in data["top_unlisted"]["BS"]] == ["NewTagA", "NewTagB"]
    assert "Fresh" not in report.read_text(encoding="utf-8")
    assert data["outside_split"] == {
        "bs_is_unlisted_tag": 4,
        "bs_is_listed_tag_without_entry": 0,
        "unmodeled_statements": 1,
    }
    assert data["distinct_unlisted_tags"] == {"BS": 2, "IS": 1}
    assert data["unlisted_tags_for_percent_of_rows"]["50"] == 1
    ranked = (tmp_path / "unlisted.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["tag"] for x in ranked] == ["NewTagA", "NewTagB", "NewTagC"]
    assert main(argv) == 1  # the report file is never overwritten
    assert "already exists" in capsys.readouterr().err
