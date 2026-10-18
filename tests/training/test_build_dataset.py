"""The dataset command end to end, on synthetic files and a tiny offline tokenizer."""

import json
from pathlib import Path

import pytest
from build_dataset import main
from dataset_contracts import NormalizationExample, SourceRecord
from harness.holdout_records import LOG_HEADER
from test_chat_tokenizer import make_tokenizer
from test_dataset_builder import fit_source, row, valid_source
from test_dataset_contracts import cik_in, example, registry_for, source

from fra_core.split import Part
from pool_store import save_registry


class Files:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.examples = root / "examples.jsonl"
        self.sources = root / "sources.jsonl"
        self.approved = root / "approved.txt"
        self.registry = root / "registry.json"
        self.moves = root / "moves.yaml"
        self.looks = root / "looks.tsv"
        self.tokenizer = make_tokenizer(root / "tokenizer")
        self.out = root / "out"
        self.moves.write_text("moves: []\n", encoding="utf-8")
        self.looks.write_text(LOG_HEADER, encoding="utf-8")

    def write(
        self,
        examples: tuple[NormalizationExample, ...],
        *,
        approve: bool = True,
        records: tuple[SourceRecord, ...] | None = None,
    ) -> None:
        roster = records or tuple(
            {e.source_record.source_id: e.source_record for e in examples}.values()
        )
        self.examples.write_text(
            "".join(e.model_dump_json() + "\n" for e in examples), encoding="utf-8"
        )
        self.sources.write_text(
            "".join(r.model_dump_json() + "\n" for r in roster), encoding="utf-8"
        )
        hashes = [e.content_sha256() for e in examples] if approve else []
        self.approved.write_text("# approvals\n\n" + "\n".join(hashes) + "\n", encoding="utf-8")
        save_registry(registry_for(*roster), self.registry)

    def args(self) -> list[str]:
        return [
            "--examples", str(self.examples),
            "--sources", str(self.sources),
            "--approved", str(self.approved),
            "--registry", str(self.registry),
            "--moves", str(self.moves),
            "--looks", str(self.looks),
            "--tokenizer-dir", str(self.tokenizer),
            "--out-dir", str(self.out),
        ]  # fmt: skip


@pytest.fixture
def files(tmp_path: Path) -> Files:
    return Files(tmp_path)


def pair() -> tuple[NormalizationExample, ...]:
    return (row(fit_source(), "a-1"), row(valid_source(), "b-1"))


def test_builds_the_three_files(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    files.write(pair())
    assert main(files.args()) == 0
    assert sorted(p.name for p in files.out.iterdir()) == [
        "manifest.json",
        "train.jsonl",
        "valid.jsonl",
    ]
    manifest = json.loads((files.out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fit_rows_after"] == 1
    assert manifest["token_limit"] == 2048
    assert str(files.out / "manifest.json") in capsys.readouterr().out


def test_unapproved_example_is_refused(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    files.write(pair(), approve=False)
    assert main(files.args()) == 1
    assert "a-1" in capsys.readouterr().err
    assert not files.out.exists()


def test_bad_json_line_is_named_by_file_and_line(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files.write(pair())
    lines = files.examples.read_text(encoding="utf-8").splitlines()
    files.examples.write_text(lines[0] + "\n{not json\n", encoding="utf-8")
    assert main(files.args()) == 1
    assert f"{files.examples}:2" in capsys.readouterr().err


def test_bad_approval_line_is_named(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    files.write(pair())
    files.approved.write_text("# ok\nnot-a-hash\n", encoding="utf-8")
    assert main(files.args()) == 1
    assert f"{files.approved}:2" in capsys.readouterr().err


def test_existing_output_is_refused(files: Files, capsys: pytest.CaptureFixture[str]) -> None:
    files.write(pair())
    files.out.mkdir()
    (files.out / "train.jsonl").write_text("keep", encoding="utf-8")
    assert main(files.args()) == 1
    assert "already exists" in capsys.readouterr().err
    assert (files.out / "train.jsonl").read_text(encoding="utf-8") == "keep"


def test_holdout_issuer_example_is_refused(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    held = source(cik_in(Part.HOLDOUT))
    files.write((*pair(), example(held)))
    assert main(files.args()) == 1
    assert "holdout" in capsys.readouterr().err
    assert not files.out.exists()


def test_missing_moves_file_is_a_reported_error(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files.write(pair())
    files.moves.unlink()
    assert main(files.args()) == 1
    assert str(files.moves) in capsys.readouterr().err


def test_malformed_moves_yaml_is_a_reported_error(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files.write(pair())
    files.moves.write_text("moves: [unclosed\n", encoding="utf-8")
    assert main(files.args()) == 1
    assert str(files.moves) in capsys.readouterr().err


def test_malformed_registry_json_is_a_reported_error(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files.write(pair())
    files.registry.write_text("{not json", encoding="utf-8")
    assert main(files.args()) == 1
    assert str(files.registry) in capsys.readouterr().err


def test_missing_registry_is_refused_not_treated_as_empty(
    files: Files, capsys: pytest.CaptureFixture[str]
) -> None:
    files.write(pair())
    files.registry.unlink()
    assert main(files.args()) == 1
    assert str(files.registry) in capsys.readouterr().err
    assert not files.out.exists()


def test_max_per_issuer_must_be_positive(files: Files) -> None:
    files.write(pair())
    with pytest.raises(SystemExit) as stop:
        main([*files.args(), "--max-per-issuer", "0"])
    assert stop.value.code == 2


def test_a_label_with_unicode_line_separators_is_one_line(files: Files) -> None:
    from build_dataset import read_jsonl

    record = fit_source()
    odd = row(record, "a-1", label="Net\u2028sales\u0085and\u2029other")
    files.examples.write_text(odd.model_dump_json() + "\n" + odd.model_dump_json() + "\n", "utf-8")
    assert [e.label for e in read_jsonl(files.examples, NormalizationExample)] == [odd.label] * 2
