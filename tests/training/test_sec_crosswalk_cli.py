"""The SEC crosswalk command line, end to end on a synthetic archive."""

import json
import re
from pathlib import Path

import pytest
from sec_crosswalk_cli import main, write_new
from sec_crosswalk_draft import MAPPING_PATH
from sec_crosswalk_fixtures import committed_mapping, synthetic_vocabulary, tag_line, write_archive


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    vocabulary = synthetic_vocabulary(committed_mapping())
    lines = [
        tag_line(d.tag, d.version, tlabel=d.tlabel, doc=d.doc)
        for tags in vocabulary.values()
        for d in tags.values()
    ]
    return write_archive(tmp_path / "2024q1.zip", lines)


def verdict_for(row: dict[str, object]) -> dict[str, object]:
    """The reviewer's line for a packet row: the header echoed, an approval for the rest."""
    if row["kind"] == "subjects":
        return row
    return {"key": row["key"], "verdict": "approve", "note": ""}


def draft(archive: Path, out: Path) -> int:
    return main(["draft", "--archives", str(archive), "--out-dir", str(out)])


def test_draft_writes_the_three_files_and_prints_hashes(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "crosswalk.json",
        "policies.json",
        "review_packet.jsonl",
    ]
    shown = capsys.readouterr().out
    crosswalk = json.loads((out / "crosswalk.json").read_text(encoding="utf-8"))
    assert crosswalk["review"]["status"] == "draft"
    assert len(json.loads((out / "policies.json").read_text(encoding="utf-8"))) == 5
    packet = [json.loads(x) for x in (out / "review_packet.jsonl").read_text("utf-8").splitlines()]
    assert len(packet) == len(crosswalk["entries"]) + 8
    assert "crosswalk sha256" in shown
    assert "mapping sha256" in shown


def test_draft_refuses_to_overwrite_and_names_missing_archives(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    capsys.readouterr()
    assert draft(archive, out) == 1
    assert "already exists" in capsys.readouterr().err
    assert draft(tmp_path / "absent.zip", tmp_path / "other") == 1
    assert "absent.zip" in capsys.readouterr().err
    assert not (tmp_path / "other").exists()


def test_sample_then_approve_round_trip(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    sample = tmp_path / "sample.jsonl"
    assert (
        main(
            [
                "sample",
                "--packet",
                str(out / "review_packet.jsonl"),
                "--n",
                "20",
                "--seed",
                "3",
                "--out",
                str(sample),
            ]
        )
        == 0
    )
    assert sample.read_text(encoding="utf-8").strip()
    packet = [json.loads(x) for x in (out / "review_packet.jsonl").read_text("utf-8").splitlines()]
    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text(
        "".join(json.dumps(verdict_for(r)) + "\n" for r in packet),
        encoding="utf-8",
    )
    approved = tmp_path / "approved"
    argv = [
        "approve",
        "--crosswalk",
        str(out / "crosswalk.json"),
        "--policies",
        str(out / "policies.json"),
        "--verdicts",
        str(verdicts),
        "--reviewer-id",
        "reviewer-agent-1",
        "--authority-id",
        "owner-sample-check",
        "--evidence-reference",
        "verdicts.jsonl",
        "--out-dir",
        str(approved),
    ]
    assert main(argv) == 0
    records = json.loads((approved / "approvals.json").read_text(encoding="utf-8"))
    assert len(records) == 6
    assert {r["status"] for r in records} == {"approved"}
    assert (
        json.loads((approved / "reviewed_crosswalk.json").read_text("utf-8"))["review"]["status"]
        == "reviewed"
    )
    capsys.readouterr()
    assert main(argv) == 1
    assert "already exists" in capsys.readouterr().err


def test_approve_refuses_incomplete_verdicts(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    verdicts = tmp_path / "verdicts.jsonl"
    header = (out / "review_packet.jsonl").read_text(encoding="utf-8").splitlines()[0]
    verdicts.write_text(
        header
        + "\n"
        + json.dumps({"key": "policy:outside-crosswalk-custom", "verdict": "approve", "note": ""})
        + "\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    code = main(
        [
            "approve",
            "--crosswalk",
            str(out / "crosswalk.json"),
            "--policies",
            str(out / "policies.json"),
            "--verdicts",
            str(verdicts),
            "--reviewer-id",
            "reviewer-agent-1",
            "--authority-id",
            "owner-sample-check",
            "--evidence-reference",
            "verdicts.jsonl",
            "--out-dir",
            str(tmp_path / "approved"),
        ]
    )
    assert code == 1
    assert "no verdict" in capsys.readouterr().err
    assert not (tmp_path / "approved").exists()


def test_approve_refuses_a_mapping_that_is_not_the_one_drafted_from(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    packet = [json.loads(x) for x in (out / "review_packet.jsonl").read_text("utf-8").splitlines()]
    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text(
        "".join(json.dumps(verdict_for(r)) + "\n" for r in packet),
        encoding="utf-8",
    )
    changed = tmp_path / "mapping.yaml"
    changed.write_text(MAPPING_PATH.read_text("utf-8") + "# edited after the draft\n", "utf-8")
    capsys.readouterr()
    code = main(
        [
            "approve",
            "--crosswalk",
            str(out / "crosswalk.json"),
            "--policies",
            str(out / "policies.json"),
            "--mapping",
            str(changed),
            "--verdicts",
            str(verdicts),
            "--reviewer-id",
            "reviewer-agent-1",
            "--authority-id",
            "owner-sample-check",
            "--evidence-reference",
            "verdicts.jsonl",
            "--out-dir",
            str(tmp_path / "approved"),
        ]
    )
    assert code == 1
    assert "mapping" in capsys.readouterr().err
    assert not (tmp_path / "approved").exists()


def test_draft_lists_every_mapping_row_with_no_vocabulary_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vocabulary = synthetic_vocabulary(committed_mapping())
    lines = [
        tag_line(d.tag, d.version, tlabel=d.tlabel, doc=d.doc)
        for tags in vocabulary.values()
        for d in tags.values()
        if d.tag not in ("Assets", "NetIncomeLoss")
    ]
    archive = write_archive(tmp_path / "partial.zip", lines)
    assert draft(archive, tmp_path / "out") == 1
    err = capsys.readouterr().err
    assert "BS Assets" in err
    assert "IS NetIncomeLoss (excluded)" in err
    assert not (tmp_path / "out").exists()


def packet_file(tmp_path: Path, text: bytes) -> Path:
    path = tmp_path / "packet.jsonl"
    path.write_bytes(text)
    return path


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"\xff\xfe not utf-8\n", "packet.jsonl"),
        (b'{"kind": "subjects"}\n{oops\n', "packet.jsonl:2"),
        (b'{"kind": "subjects"}\n{"kind": "entry", "key": "k"}\n', "packet.jsonl:2.*canonical_id"),
        (b'{"kind": "subjects"}\n[1]\n', "packet.jsonl:2"),
    ],
)
def test_sample_reports_a_bad_packet_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, expected: str
) -> None:
    code = main(
        [
            "sample",
            "--packet",
            str(packet_file(tmp_path, content)),
            "--n",
            "5",
            "--seed",
            "1",
            "--out",
            str(tmp_path / "sample.jsonl"),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert re.search(expected, err)
    assert err.strip().count("\n") == 0
    assert "Traceback" not in err


def test_a_dangling_symlink_blocks_the_write_and_nothing_is_left_behind(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    (out / "policies.json").symlink_to(victim)
    assert draft(archive, out) == 1
    assert "already exists" in capsys.readouterr().err
    assert not victim.exists()
    assert not (out / "crosswalk.json").exists()


def test_approve_refuses_a_crosswalk_edited_after_the_packet(
    archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "draft"
    assert draft(archive, out) == 0
    packet = [json.loads(x) for x in (out / "review_packet.jsonl").read_text("utf-8").splitlines()]
    verdicts = tmp_path / "verdicts.jsonl"
    verdicts.write_text(
        "".join(json.dumps(verdict_for(r)) + "\n" for r in packet),
        encoding="utf-8",
    )
    document = json.loads((out / "crosswalk.json").read_text(encoding="utf-8"))
    document["entries"][0]["canonical_id"] = "total_liabilities"
    document["entries"][0]["abstention_reason"] = None
    (out / "crosswalk.json").write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()
    code = main(
        [
            "approve",
            "--crosswalk",
            str(out / "crosswalk.json"),
            "--policies",
            str(out / "policies.json"),
            "--verdicts",
            str(verdicts),
            "--reviewer-id",
            "reviewer-agent-1",
            "--authority-id",
            "owner-sample-check",
            "--evidence-reference",
            "verdicts.jsonl",
            "--out-dir",
            str(tmp_path / "approved"),
        ]
    )
    assert code == 1
    assert "header" in capsys.readouterr().err
    assert not (tmp_path / "approved").exists()


def test_a_failed_write_removes_the_files_it_created(tmp_path: Path) -> None:
    files = {tmp_path / "one.json": "ok", tmp_path / "two.json": 7}
    with pytest.raises(TypeError):
        write_new(files)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []
