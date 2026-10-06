"""The file format of the moves record and the scoring log (eval/harness/holdout_records.py).
The rules over the parsed values are tested in packages/core/tests/test_split.py."""

import subprocess
import sys
from pathlib import Path

import pytest
from harness.development import development_set
from harness.holdout_records import (
    LOG_HEADER,
    log_look,
    read_looks,
    read_moves,
    read_validation_uses,
)
from harness.paths import HOLDOUT_MOVES, ROOT, SCORING_LOG, VALIDATION_USES

from fra_core.split import Look, Part, SplitError

AL_DAWAA = "Al Dawaa Medical Services"


def write_moves(path: Path, *entries: str) -> Path:
    body = "".join(entries) if entries else " []"
    path.write_text(f"# test record\nmoves:{body}\n", encoding="utf-8")
    return path


def move(
    issuer: str = AL_DAWAA,
    kind: str = "override",
    looks_before: str = "0",
    date: str = "2026-10-02",
    source: str = "holdout",
    target: str = "fit",
) -> str:
    return (
        f"\n  - issuer: {issuer}\n    kind: {kind}\n    from: {source}\n    to: {target}\n"
        f"    date: '{date}'\n    looks_before: {looks_before}\n    reason: a page was read\n"
    )


def empty_log(path: Path) -> Path:
    path.write_text(LOG_HEADER, encoding="utf-8")
    return path


def a_look(date: str = "2026-10-05", kind: str = "dry_run") -> Look:
    return Look(
        date=date, kind=kind, parts=("holdout",), strata=("annual", "interim"), candidate="abc1234"
    )


# ---- the committed records ------------------------------------------------------------------


def test_the_committed_records_read_together_and_start_with_the_first_dry_run() -> None:
    looks = read_looks(SCORING_LOG)
    assert (looks[0].date, looks[0].kind, looks[0].parts) == ("2026-10-04", "dry_run", ("holdout",))
    assert [m.kind for m in read_moves(HOLDOUT_MOVES, looks)][:4] == ["override"] * 4


# ---- the moves record -----------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["issuer", "kind", "from", "to", "date", "looks_before"])
def test_a_move_missing_a_field_names_the_field_and_the_file(tmp_path: Path, missing: str) -> None:
    lines = [
        ln for ln in move().splitlines() if not ln.strip().lstrip("- ").startswith(f"{missing}:")
    ]
    if missing == "issuer":
        lines[1] = lines[1].replace("    kind", "  - kind")
    path = write_moves(tmp_path / "m.yaml", "\n".join(lines) + "\n")
    with pytest.raises(SplitError, match=rf"m\.yaml.*'{missing}'"):
        read_moves(path, [])


@pytest.mark.parametrize("looks_before", ["'0'", "0.5", "true", "[]", "null"])
def test_a_looks_before_that_is_not_an_integer_names_the_file(
    tmp_path: Path, looks_before: str
) -> None:
    path = write_moves(tmp_path / "m.yaml", move(looks_before=looks_before))
    with pytest.raises(SplitError, match=r"m\.yaml.*looks_before.*not an integer"):
        read_moves(path, [])


@pytest.mark.parametrize("kind", ["[a]", "5"])
def test_a_kind_that_is_not_text_names_the_file(tmp_path: Path, kind: str) -> None:
    with pytest.raises(SplitError, match=r"m\.yaml.*kind.*not text"):
        read_moves(write_moves(tmp_path / "m.yaml", move(kind=kind)), [])


def test_an_unknown_kind_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(SplitError, match=r"m\.yaml.*kind 'exception'"):
        read_moves(write_moves(tmp_path / "m.yaml", move(kind="exception")), [])


def test_a_rule_broken_in_the_record_names_the_file(tmp_path: Path) -> None:
    path = write_moves(tmp_path / "m.yaml", move(source="fit", target="holdout"))
    with pytest.raises(SplitError, match=r"m\.yaml.*holdout to fit"):
        read_moves(path, [])


def test_a_record_that_is_not_a_moves_list_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text("moves: 3\n", encoding="utf-8")
    with pytest.raises(SplitError, match=r"m\.yaml.*'moves' list"):
        read_moves(path, [])
    path.write_text("moves:\n  - just text\n", encoding="utf-8")
    with pytest.raises(SplitError, match=r"m\.yaml.*move 1 is not a mapping"):
        read_moves(path, [])


def test_moves_are_checked_against_the_log_that_was_read(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    log_look(log, a_look("2026-10-05"))
    looks = read_looks(log)
    assert read_moves(write_moves(tmp_path / "m.yaml", move(date="2026-10-02")), looks)
    with pytest.raises(SplitError, match="before the first look"):
        read_moves(write_moves(tmp_path / "m.yaml", move(date="2026-10-05")), looks)
    spent = move(kind="spent_look", looks_before="1", date="2026-10-06")
    assert read_moves(write_moves(tmp_path / "m.yaml", spent), looks)[0].kind == "spent_look"


# ---- the scoring log ------------------------------------------------------------------------


def test_log_starts_empty_and_appends_rows_in_order(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    assert read_looks(log) == []
    log_look(log, a_look("2026-10-05"))
    log_look(log, a_look("2026-10-07", kind="model_scoring"))
    looks = read_looks(log)
    assert [(lk.date, lk.kind) for lk in looks] == [
        ("2026-10-05", "dry_run"),
        ("2026-10-07", "model_scoring"),
    ]
    assert looks[0].parts == ("holdout",) and looks[0].strata == ("annual", "interim")
    text = log.read_text(encoding="utf-8")
    assert text.startswith(LOG_HEADER)
    assert text.endswith("2026-10-07\tmodel_scoring\tholdout\tannual,interim\tabc1234\n")


def test_log_refuses_a_row_dated_before_the_last(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    log_look(log, a_look("2026-10-05"))
    with pytest.raises(SplitError, match=r"log\.tsv.*earlier"):
        log_look(log, a_look("2026-10-04"))
    assert len(read_looks(log)) == 1


def test_log_refuses_an_incomplete_row_and_writes_nothing(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with pytest.raises(SplitError):
        log_look(log, Look("2026-10-05", "peek", ("holdout",), ("annual",), "abc"))
    assert read_looks(log) == []


def test_log_with_a_changed_header_is_refused(tmp_path: Path) -> None:
    log = tmp_path / "log.tsv"
    log.write_text("date\tkind\n", encoding="utf-8")
    with pytest.raises(SplitError, match="header"):
        read_looks(log)


def test_a_row_with_the_wrong_number_of_fields_is_refused(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-05\tdry_run\tholdout\n")
    with pytest.raises(SplitError, match=r"log\.tsv.*row 1 has 3 fields"):
        read_looks(log)


def test_a_log_row_that_breaks_a_look_rule_names_the_file(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-05\tpeek\tholdout\tannual\tabc\n")
    with pytest.raises(SplitError, match=r"log\.tsv.*peek"):
        read_looks(log)


def test_a_log_that_does_not_end_in_a_newline_is_refused(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-05\tdry_run\tholdout\tannual\tabc")
    with pytest.raises(SplitError, match=r"log\.tsv.*newline"):
        read_looks(log)
    with pytest.raises(SplitError, match=r"log\.tsv.*newline"):
        log_look(log, a_look("2026-10-06"))


def test_a_row_is_split_on_newlines_only(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-05\tdry_run\tholdout\tannual\tab\x0bc\n")
    with pytest.raises(SplitError, match=r"log\.tsv.*control"):
        read_looks(log)


@pytest.mark.parametrize("bad", ["a\x0bb", "a\x0cb", "a\x85b", "a\u2028b", "a\u2029b", "a\x00b"])
def test_a_look_that_could_not_be_read_back_is_refused(tmp_path: Path, bad: str) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with pytest.raises(SplitError, match="control"):
        log_look(log, Look("2026-10-05", "dry_run", ("holdout",), ("annual",), bad))
    assert read_looks(log) == []


def test_a_failure_to_read_the_log_names_the_path_once(tmp_path: Path) -> None:
    log = tmp_path / "log.tsv"
    log.write_text("date\tkind\n", encoding="utf-8")
    with pytest.raises(SplitError) as caught:
        log_look(log, a_look())
    assert str(caught.value).count(str(log)) == 1


def test_the_corpus_tools_import_without_the_harness_on_the_path() -> None:
    """scripts/argaam_listing.py imports corpus; only `split` needs the harness."""
    code = (
        "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import corpus; "
        "assert 'harness' not in sys.modules"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "scripts"), str(ROOT / "packages/core/src")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin"},
        check=False,
    )
    assert done.returncode == 0, done.stderr


# ---- the validation documents used for ingest development ------------------------------------


def write_uses(path: Path, body: str) -> Path:
    path.write_text(f"# test record\n{body}", encoding="utf-8")
    return path


def use(**fields: str) -> str:
    """A uses record with one entry; a field given as an empty string is left out."""
    entry = {"date": "'2026-10-05'", "purpose": "a rule", "documents": "[a-1, b-2]", **fields}
    lines = [f"{key}: {value}" for key, value in entry.items() if value]
    return "uses:\n  - " + "\n    ".join(lines) + "\n"


def test_the_committed_validation_uses_name_validation_documents_only() -> None:
    uses = read_validation_uses(VALIDATION_USES)
    assert uses
    validation = {d["id"] for d in development_set({Part.VALIDATION})}
    for item in uses:
        assert set(item.documents) <= validation, item


def test_a_validation_use_is_read_with_its_date_purpose_and_documents(tmp_path: Path) -> None:
    (item,) = read_validation_uses(write_uses(tmp_path / "u.yaml", use()))
    assert (item.date, item.purpose, item.documents) == ("2026-10-05", "a rule", ("a-1", "b-2"))


@pytest.mark.parametrize("missing", ["date", "purpose", "documents"])
def test_a_use_missing_a_field_names_the_field_and_the_file(tmp_path: Path, missing: str) -> None:
    path = write_uses(tmp_path / "u.yaml", use(**{missing: ""}))
    with pytest.raises(SplitError, match=rf"u\.yaml.*'{missing}'"):
        read_validation_uses(path)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"date": "'5 October'"}, "date"),
        ({"documents": "[]"}, "documents"),
        ({"documents": "a-1"}, "documents"),
        ({"documents": "[a-1, a-1]"}, "twice"),
    ],
)
def test_a_use_with_a_bad_value_names_the_file_and_the_field(
    tmp_path: Path, fields: dict[str, str], message: str
) -> None:
    with pytest.raises(SplitError, match=rf"u\.yaml.*{message}"):
        read_validation_uses(write_uses(tmp_path / "u.yaml", use(**fields)))


def test_a_record_that_is_not_a_uses_list_is_refused(tmp_path: Path) -> None:
    path = write_uses(tmp_path / "u.yaml", "uses: 3\n")
    with pytest.raises(SplitError, match=r"u\.yaml.*'uses' list"):
        read_validation_uses(path)
    path = write_uses(tmp_path / "u.yaml", "uses:\n  - just text\n")
    with pytest.raises(SplitError, match=r"u\.yaml.*use 1 is not a mapping"):
        read_validation_uses(path)
