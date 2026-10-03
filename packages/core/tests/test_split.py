"""The issuer split of the train pool: fit, validation and holdout (04-execution-phases.md 2.4)."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from fra_core.split import (
    LOG_HEADER,
    Look,
    Part,
    PoolRefused,
    SplitError,
    bucket,
    cik_key,
    document_key,
    hashed_part,
    holdout_for_scoring,
    issuer_key,
    log_look,
    place,
    read_looks,
    read_moves,
)

# Hand-checked: sha256(b"holdout:al dawaa medical services") mod 100 is 14, the last holdout
# bucket; sha256(b"holdout:cik:320193") mod 100 is 29, the last validation bucket; "almarai" is 99.
AL_DAWAA = "Al Dawaa Medical Services"


def doc(
    issuer: str, pool: str = "train", *, cik: int | None = None, n: int = 0, **extra: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {"id": f"{issuer_key(issuer)}-{n}", "issuer": issuer, "pool": pool}
    if cik is not None:
        record["cik"] = cik
    return record | extra


def write_moves(path: Path, *entries: str) -> Path:
    body = "".join(entries) if entries else " []"
    path.write_text(f"# test record\nmoves:{body}\n", encoding="utf-8")
    return path


def move(
    issuer: str = AL_DAWAA,
    kind: str = "override",
    looks_before: int = 0,
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


# ---- hashing --------------------------------------------------------------------------------


def test_bucket_is_sha256_of_salted_key_mod_100() -> None:
    key = issuer_key(AL_DAWAA)
    assert key == "al dawaa medical services"
    by_hand = int(hashlib.sha256(b"holdout:al dawaa medical services").hexdigest(), 16) % 100
    assert by_hand == 14
    assert bucket(key) == 14
    assert bucket("cik:320193") == 29
    assert bucket(issuer_key("Almarai Company")) == 99


def test_bucket_boundaries_give_15_holdout_15_validation_70_fit() -> None:
    assert hashed_part(issuer_key(AL_DAWAA)) is Part.HOLDOUT  # bucket 14
    assert hashed_part("cik:320193") is Part.VALIDATION  # bucket 29
    assert hashed_part(issuer_key("Almarai Company")) is Part.FIT  # bucket 99
    shares = {p: 0 for p in Part}
    for n in range(4000):
        shares[hashed_part(f"issuer {n}")] += 1
    assert 0.12 < shares[Part.HOLDOUT] / 4000 < 0.18
    assert 0.12 < shares[Part.VALIDATION] / 4000 < 0.18


def test_hash_is_stable_across_calls_and_name_spellings() -> None:
    assert hashed_part(issuer_key("ALMARAI CO.")) is hashed_part(issuer_key("Almarai Company"))
    assert bucket("x") == bucket("x")


# ---- corpus and SEC agree on a shared CIK ---------------------------------------------------


def test_corpus_issuer_with_cik_is_keyed_like_its_sec_rows() -> None:
    corpus_doc = doc("Apple Inc.", cik=320193)
    assert document_key(corpus_doc) == cik_key(320193) == "cik:320193"
    assert hashed_part(document_key(corpus_doc)) is hashed_part(cik_key(320193))
    # Without the cik the name would decide, and the two would not be the same issuer.
    assert document_key(doc("Apple Inc.")) == "apple"
    assert hashed_part("apple") is not hashed_part(cik_key(320193))


def test_an_issuer_with_a_cik_on_only_some_documents_is_refused() -> None:
    documents = [doc("Apple Inc.", cik=320193, n=1), doc("Apple Inc.", n=2)]
    with pytest.raises(SplitError, match="every document"):
        place(documents, [])


def test_cik_given_as_string_is_the_same_key() -> None:
    assert document_key(doc("X", cik="0000320193")) == "cik:320193"  # type: ignore[arg-type]


# ---- refusing the pools the split may not touch ---------------------------------------------


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_place_refuses_model_test_and_blind(pool: str) -> None:
    with pytest.raises(PoolRefused, match=pool):
        place([doc("Some Company", pool)], [])


def test_place_refuses_dev_as_not_split() -> None:
    with pytest.raises(SplitError, match="dev"):
        place([doc("Almarai Company", "dev")], [])


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_holdout_selection_refuses_model_test_and_blind_and_logs_nothing(
    tmp_path: Path, pool: str
) -> None:
    log = empty_log(tmp_path / "log.tsv")
    moves = write_moves(tmp_path / "moves.yaml")
    with pytest.raises(PoolRefused, match=pool):
        holdout_for_scoring([doc(AL_DAWAA), doc("Other", pool)], moves, log, a_look())
    assert read_looks(log) == []


# ---- placement and overrides ----------------------------------------------------------------


def test_place_as_hashed_and_with_an_override(tmp_path: Path) -> None:
    documents = [doc(AL_DAWAA, n=1), doc(AL_DAWAA, n=2), doc("Almarai Company")]
    hashed = place(documents, [])
    assert [d["id"] for d in hashed[Part.HOLDOUT]] == [d["id"] for d in documents[:2]]
    moves = read_moves(write_moves(tmp_path / "m.yaml", move()), [])
    after = place(documents, moves)
    assert after[Part.HOLDOUT] == []
    assert len(after[Part.FIT]) == 3


@pytest.mark.parametrize(("source", "target"), [("holdout", "validation"), ("fit", "holdout")])
def test_a_move_only_goes_from_holdout_to_fit(tmp_path: Path, source: str, target: str) -> None:
    path = write_moves(tmp_path / "m.yaml", move(source=source, target=target))
    with pytest.raises(SplitError, match="holdout to fit"):
        read_moves(path, [])


def test_a_move_of_an_issuer_the_hash_does_not_hold_out_is_refused(tmp_path: Path) -> None:
    moves = read_moves(write_moves(tmp_path / "m.yaml", move(issuer="Almarai Company")), [])
    with pytest.raises(SplitError, match="not in the holdout"):
        place([doc("Almarai Company")], moves)


def test_a_move_of_an_issuer_not_in_train_is_refused(tmp_path: Path) -> None:
    moves = read_moves(write_moves(tmp_path / "m.yaml", move()), [])
    with pytest.raises(SplitError, match="no train document"):
        place([doc("Almarai Company")], moves)


def test_an_issuer_moved_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SplitError, match="more than once"):
        read_moves(write_moves(tmp_path / "m.yaml", move(), move()), [])


def test_override_before_the_first_look_is_allowed(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    log_look(log, a_look("2026-10-05"))
    moves = read_moves(write_moves(tmp_path / "m.yaml", move(date="2026-10-02")), read_looks(log))
    assert [m.kind for m in moves] == ["override"]


def test_override_after_a_look_is_refused(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    log_look(log, a_look("2026-10-05"))
    looks = read_looks(log)
    with pytest.raises(SplitError, match="before the first look"):
        read_moves(write_moves(tmp_path / "m.yaml", move(looks_before=1)), looks)
    with pytest.raises(SplitError, match="before the first look"):
        read_moves(write_moves(tmp_path / "m.yaml", move(date="2026-10-06")), looks)


def test_spent_look_needs_a_look_in_the_log(tmp_path: Path) -> None:
    path = write_moves(tmp_path / "m.yaml", move(kind="spent_look", looks_before=1))
    with pytest.raises(SplitError, match="1 look"):
        read_moves(path, [])
    with pytest.raises(SplitError, match="after a look"):
        read_moves(write_moves(tmp_path / "m.yaml", move(kind="spent_look")), [])
    log = empty_log(tmp_path / "log.tsv")
    log_look(log, a_look("2026-10-05"))
    spent = read_moves(
        write_moves(
            tmp_path / "m.yaml", move(kind="spent_look", looks_before=1, date="2026-10-06")
        ),
        read_looks(log),
    )
    assert spent[0].kind == "spent_look"


def test_unknown_move_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SplitError, match="kind"):
        read_moves(write_moves(tmp_path / "m.yaml", move(kind="exception")), [])


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
    with pytest.raises(SplitError, match="earlier"):
        log_look(log, a_look("2026-10-04"))


@pytest.mark.parametrize(
    "bad",
    [
        Look("2026-10-05", "peek", ("holdout",), ("annual",), "abc"),
        Look("2026-10-05", "dry_run", ("validation",), ("annual",), "abc"),
        Look("2026-10-05", "dry_run", ("holdout",), (), "abc"),
        Look("2026-10-05", "dry_run", ("holdout",), ("annual",), ""),
        Look("2026-10-05", "dry_run", ("holdout",), ("annual",), "a\tb"),
        Look("5 October", "dry_run", ("holdout",), ("annual",), "abc"),
        Look("20261005", "dry_run", ("holdout",), ("annual",), "abc"),
    ],
)
def test_log_refuses_an_incomplete_row(tmp_path: Path, bad: Look) -> None:
    log = empty_log(tmp_path / "log.tsv")
    with pytest.raises(SplitError):
        log_look(log, bad)
    assert read_looks(log) == []


def test_log_with_a_changed_header_is_refused(tmp_path: Path) -> None:
    log = tmp_path / "log.tsv"
    log.write_text("date\tkind\n", encoding="utf-8")
    with pytest.raises(SplitError, match="header"):
        read_looks(log)


def test_holdout_scoring_is_logged_before_documents_are_returned(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    moves = write_moves(tmp_path / "m.yaml")
    documents = [doc(AL_DAWAA), doc("Almarai Company")]
    selected = holdout_for_scoring(documents, moves, log, a_look())
    assert [d["issuer"] for d in selected] == [AL_DAWAA]
    assert read_looks(log) == [a_look()]


def test_holdout_scoring_without_holdout_in_the_look_is_refused(tmp_path: Path) -> None:
    log = empty_log(tmp_path / "log.tsv")
    moves = write_moves(tmp_path / "m.yaml")
    look = Look("2026-10-05", "dry_run", ("validation",), ("annual",), "abc")
    with pytest.raises(SplitError, match="holdout"):
        holdout_for_scoring([doc(AL_DAWAA)], moves, log, look)
    assert read_looks(log) == []
