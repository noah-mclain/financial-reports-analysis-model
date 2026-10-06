"""The issuer split of the train pool: fit, validation and holdout (04-execution-phases.md 2.4)."""

import hashlib
import inspect
from typing import Any

import pytest

from fra_core import split
from fra_core.split import (
    HoldoutAvailability,
    Look,
    Move,
    Part,
    PartCounts,
    PoolRefused,
    SplitError,
    bucket,
    check_looks,
    check_moves,
    cik_key,
    development_documents,
    document_key,
    hashed_part,
    holdout_availability,
    holdout_for_scoring,
    issuer_key,
    part_counts,
)

# Hand-checked: sha256(b"holdout:al dawaa medical services") mod 100 is 14, the last holdout
# bucket; sha256(b"holdout:cik:320193") mod 100 is 29, the last validation bucket; "almarai" is 99.
AL_DAWAA = "Al Dawaa Medical Services"
PUBLIC_API = [
    "TRAIN",
    "HoldoutAvailability",
    "Look",
    "Move",
    "Part",
    "PartCounts",
    "PoolRefused",
    "SplitError",
    "bucket",
    "check_looks",
    "check_moves",
    "cik_key",
    "development_documents",
    "document_key",
    "hashed_part",
    "holdout_availability",
    "holdout_for_scoring",
    "issuer_key",
    "part_counts",
]


def doc(
    issuer: str, pool: str = "train", *, cik: int | None = None, n: int = 0, **extra: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {"id": f"{issuer_key(issuer)}-{n}", "issuer": issuer, "pool": pool}
    if cik is not None:
        record["cik"] = cik
    return record | extra


def mv(
    issuer: str = AL_DAWAA,
    kind: str = "override",
    looks_before: int = 0,
    date: str = "2026-10-02",
    source: str = "holdout",
    target: str = "fit",
    reason: str = "a page was read",
) -> Move:
    return Move(issuer, issuer_key(issuer), kind, source, target, date, looks_before, reason)


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
        part_counts(documents, [])


def test_cik_given_as_string_is_the_same_key() -> None:
    assert document_key(doc("X", cik="0000320193")) == "cik:320193"  # type: ignore[arg-type]


# ---- refusing the pools the split may not touch ---------------------------------------------


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_counting_refuses_model_test_and_blind(pool: str) -> None:
    with pytest.raises(PoolRefused, match=pool):
        part_counts([doc("Some Company", pool)], [])


def test_counting_refuses_dev_as_not_split() -> None:
    with pytest.raises(SplitError, match="dev"):
        part_counts([doc("Almarai Company", "dev")], [])


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_holdout_selection_refuses_model_test_and_blind_and_logs_nothing(pool: str) -> None:
    recorded: list[Look] = []
    with pytest.raises(PoolRefused, match=pool):
        holdout_for_scoring([doc(AL_DAWAA), doc("Other", pool)], [], [], a_look(), recorded.append)
    assert recorded == []


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_availability_refuses_model_test_and_blind(pool: str) -> None:
    with pytest.raises(PoolRefused, match=pool):
        holdout_availability([doc("Other", pool)], [], set())


# ---- placement and overrides ----------------------------------------------------------------


def test_counts_as_hashed_and_with_an_override() -> None:
    documents = [
        doc(AL_DAWAA, n=1, language="ar", period="interim"),
        doc(AL_DAWAA, n=2, language="en", period="annual"),
        doc("Almarai Company", language="ar", period="annual"),
    ]
    hashed = part_counts(documents, [])
    assert hashed[Part.HOLDOUT] == PartCounts(
        issuers=1, documents=2, arabic=1, english=1, both=1, annual=1, interim=1
    )
    assert hashed[Part.VALIDATION] == PartCounts(0, 0, 0, 0, 0, 0, 0)
    after = part_counts(documents, [mv()])
    assert after[Part.HOLDOUT].documents == 0
    assert (after[Part.FIT].issuers, after[Part.FIT].documents) == (2, 3)


def test_public_api_returns_holdout_documents_only_through_a_logged_scoring() -> None:
    assert sorted(split.__all__) == sorted(PUBLIC_API)
    defined = {
        name
        for name, value in vars(split).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or inspect.isclass(value))
        and value.__module__ == split.__name__
    }
    assert defined <= set(split.__all__)
    assert not hasattr(split, "place")


def test_core_does_no_file_io() -> None:
    assert not hasattr(split, "yaml") and not hasattr(split, "Path")


def test_override_dated_on_the_day_of_the_first_look_is_refused() -> None:
    with pytest.raises(SplitError, match="before the first look"):
        check_moves([mv(date="2026-10-05")], [a_look("2026-10-05")])


@pytest.mark.parametrize(("source", "target"), [("holdout", "validation"), ("fit", "holdout")])
def test_a_move_only_goes_from_holdout_to_fit(source: str, target: str) -> None:
    with pytest.raises(SplitError, match="holdout to fit"):
        check_moves([mv(source=source, target=target)], [])


def test_a_move_of_an_issuer_the_hash_does_not_hold_out_is_refused() -> None:
    with pytest.raises(SplitError, match="not in the holdout"):
        part_counts([doc("Almarai Company")], [mv(issuer="Almarai Company")])


def test_a_move_of_an_issuer_not_in_train_is_refused() -> None:
    with pytest.raises(SplitError, match="no train document"):
        part_counts([doc("Almarai Company")], [mv()])


def test_an_issuer_moved_twice_is_refused() -> None:
    with pytest.raises(SplitError, match="more than once"):
        check_moves([mv(), mv()], [])


def test_a_move_needs_its_reason_and_a_real_date() -> None:
    with pytest.raises(SplitError, match="reason"):
        check_moves([mv(reason=" ")], [])
    with pytest.raises(SplitError, match="YYYY-MM-DD"):
        check_moves([mv(date="2 October")], [])


def test_override_before_the_first_look_is_allowed() -> None:
    check_moves([mv(date="2026-10-02")], [a_look("2026-10-05")])


def test_override_after_a_look_is_refused() -> None:
    looks = [a_look("2026-10-05")]
    with pytest.raises(SplitError, match="before the first look"):
        check_moves([mv(looks_before=1)], looks)
    with pytest.raises(SplitError, match="before the first look"):
        check_moves([mv(date="2026-10-06")], looks)


def test_spent_look_needs_a_look_in_the_log() -> None:
    with pytest.raises(SplitError, match="1 look"):
        check_moves([mv(kind="spent_look", looks_before=1)], [])
    with pytest.raises(SplitError, match="after a look"):
        check_moves([mv(kind="spent_look")], [])
    spent = mv(kind="spent_look", looks_before=1, date="2026-10-06")
    check_moves([spent], [a_look("2026-10-05")])
    with pytest.raises(SplitError, match="before the look it follows"):
        check_moves([mv(kind="spent_look", looks_before=1, date="2026-10-04")], [a_look()])


def test_unknown_move_kind_is_refused() -> None:
    with pytest.raises(SplitError, match="kind"):
        check_moves([mv(kind="exception")], [])


# ---- the scoring log's rules ----------------------------------------------------------------


def test_looks_must_be_in_date_order() -> None:
    check_looks([a_look("2026-10-05"), a_look("2026-10-07", kind="model_scoring")])
    with pytest.raises(SplitError, match="earlier"):
        check_looks([a_look("2026-10-05"), a_look("2026-10-04")])


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
def test_an_incomplete_look_is_refused(bad: Look) -> None:
    with pytest.raises(SplitError):
        check_looks([bad])


# ---- the holdout leaves only after the look is recorded -------------------------------------


def test_holdout_scoring_records_the_look_before_documents_are_returned() -> None:
    events: list[str] = []

    def record(look: Look) -> None:
        events.append(f"recorded {look.date}")

    documents = [doc(AL_DAWAA), doc("Almarai Company")]
    selected = holdout_for_scoring(documents, [], [], a_look(), record)
    assert [d["issuer"] for d in selected] == [AL_DAWAA]
    assert events == ["recorded 2026-10-05"]


def test_a_record_that_raises_returns_no_documents() -> None:
    def record(look: Look) -> None:
        raise OSError("log is read-only")

    with pytest.raises(OSError, match="read-only"):
        holdout_for_scoring([doc(AL_DAWAA)], [], [], a_look(), record)


@pytest.mark.parametrize("parts", [("validation",), ("holdout", "validation"), ("fit", "holdout")])
def test_holdout_scoring_logs_only_the_holdout_part(parts: tuple[str, ...]) -> None:
    recorded: list[Look] = []
    look = Look("2026-10-05", "dry_run", parts, ("annual",), "abc")
    with pytest.raises(SplitError, match="holdout"):
        holdout_for_scoring([doc(AL_DAWAA)], [], [], look, recorded.append)
    assert recorded == []


def test_holdout_scoring_refuses_a_look_dated_before_the_log_or_broken_moves() -> None:
    recorded: list[Look] = []
    with pytest.raises(SplitError, match="earlier"):
        holdout_for_scoring(
            [doc(AL_DAWAA)], [], [a_look("2026-10-06")], a_look("2026-10-05"), recorded.append
        )
    with pytest.raises(SplitError, match="before the first look"):
        holdout_for_scoring(
            [doc(AL_DAWAA)], [mv(looks_before=1)], [a_look()], a_look("2026-10-06"), recorded.append
        )
    assert recorded == []


# ---- availability: counts, never documents --------------------------------------------------


def test_availability_counts_the_holdout_documents_that_are_not_ready() -> None:
    documents = [doc(AL_DAWAA, n=1), doc(AL_DAWAA, n=2), doc("Almarai Company")]
    ready = {documents[0]["id"]}
    result = holdout_availability(documents, [], ready)
    assert result == HoldoutAvailability(documents=2, missing=1)
    after_move = holdout_availability(documents, [mv()], set())
    assert after_move == HoldoutAvailability(documents=0, missing=0)


# ---- development documents ------------------------------------------------------------------

VALIDATION_ISSUER = next(
    name
    for name in (f"Issuer {n}" for n in range(300))
    if hashed_part(issuer_key(name)) is Part.VALIDATION
)
FIT_ISSUER = next(
    name
    for name in (f"Issuer {n}" for n in range(300))
    if hashed_part(issuer_key(name)) is Part.FIT
)


def development_set() -> list[dict[str, Any]]:
    return [
        doc(FIT_ISSUER),
        doc(VALIDATION_ISSUER),
        doc(AL_DAWAA, n=1),  # in the holdout as hashed, in fit after the override
    ]


def test_development_documents_refuse_the_holdout_and_point_to_the_logged_scoring() -> None:
    with pytest.raises(SplitError, match="holdout_for_scoring"):
        development_documents(development_set(), [], {Part.HOLDOUT})
    with pytest.raises(SplitError, match="holdout_for_scoring"):
        development_documents(development_set(), [], {Part.FIT, Part.HOLDOUT})


def test_development_documents_need_a_part() -> None:
    with pytest.raises(SplitError, match="no part"):
        development_documents(development_set(), [], set())


@pytest.mark.parametrize("pool", ["model_test", "blind"])
def test_development_documents_refuse_model_test_and_blind(pool: str) -> None:
    with pytest.raises(PoolRefused):
        development_documents([*development_set(), doc("Other", pool=pool)], [], {Part.FIT})


def test_development_documents_return_fit_and_validation_with_the_moves_applied() -> None:
    documents = development_set()
    fit_only = development_documents(documents, [], {Part.FIT})
    assert [d["issuer"] for d in fit_only] == [FIT_ISSUER]
    both = development_documents(documents, [mv()], {Part.FIT, Part.VALIDATION})
    assert [d["issuer"] for d in both] == [FIT_ISSUER, VALIDATION_ISSUER, AL_DAWAA]
    validation = development_documents(documents, [mv()], {Part.VALIDATION})
    assert [d["issuer"] for d in validation] == [VALIDATION_ISSUER]


def test_development_documents_never_hold_a_holdout_issuer() -> None:
    documents = development_set()
    returned = development_documents(documents, [], {Part.FIT, Part.VALIDATION})
    assert AL_DAWAA not in {d["issuer"] for d in returned}
