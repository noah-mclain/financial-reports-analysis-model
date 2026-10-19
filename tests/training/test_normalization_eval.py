"""The comparison must penalize invalid/null replies and preserve issuer-level evidence."""

import json

import pytest
from dataset_contracts import DatasetError
from normalization_eval import (
    EvalRow,
    Prediction,
    compare,
    error_upper95,
    inference_messages,
    parse_answer,
    score,
    select_rows,
)

from fra_core.schemas import StatementType
from fra_core.taxonomy import load_taxonomy


def sample(ident: str, target: str | None, issuer: str = "cik:1") -> EvalRow:
    return EvalRow(
        example_id=ident,
        issuer=issuer,
        statement=StatementType.INCOME,
        label="Sales",
        canonical_id=target,
        source="sec",
        language="en",
        period="annual",
    )


def prediction(ident: str, target: str | None) -> Prediction:
    return Prediction(example_id=ident, response=json.dumps({"canonical_id": target}))


@pytest.mark.parametrize(
    "reply",
    [
        "null",
        "{}",
        '{"canonical_id": false}',
        '{"canonical_id": "invented"}',
        '{"canonical_id": "total_assets"}',
        '{"canonical_id": null, "extra": 1}',
        '{"canonical_id": null, "canonical_id": "revenue"}',
        "```json\n{}\n```",
    ],
)
def test_bad_answers_are_invalid_instead_of_abstentions(reply: str) -> None:
    assert parse_answer(reply, StatementType.INCOME, load_taxonomy())[0] == "invalid"


def test_null_is_a_valid_abstention() -> None:
    assert parse_answer('{"canonical_id": null}', StatementType.INCOME, load_taxonomy()) == (
        "valid",
        None,
    )


def test_critical_bound_requires_evidence_beyond_perfect_accuracy() -> None:
    assert error_upper95(0, 100) == pytest.approx(0.029513049607)
    assert error_upper95(0, 298) > 0.01
    assert error_upper95(0, 299) < 0.01
    assert error_upper95(1, 473) < 0.01
    assert error_upper95(1, 472) > 0.01
    assert error_upper95(10, 10) == 1
    assert error_upper95(0, 0) is None


def test_invalid_output_on_null_target_is_an_error() -> None:
    result = score((sample("a", None),), (Prediction(example_id="a", response="bad"),))
    assert result["accuracy"] == 0
    assert result["invalid_outputs"] == 1
    assert result["correct_abstentions"] == 0


def test_critical_accuracy_with_too_few_examples_is_explicitly_not_shown() -> None:
    result = score((sample("a", "revenue"),), (prediction("a", "revenue"),))
    assert result["plan_critical"]["accuracy"] == 1
    assert result["plan_critical"]["issuers"] == 1
    assert result["plan_critical"]["disposition"] == "not shown"


def test_many_critical_rows_from_one_issuer_still_have_insufficient_support() -> None:
    rows = tuple(sample(str(i), "revenue") for i in range(300))
    replies = tuple(prediction(r.example_id, "revenue") for r in rows)
    assert score(rows, replies)["plan_critical"]["disposition"] == "not shown"


def test_metrics_count_false_mappings_abstentions_and_missing_classes() -> None:
    rows = (sample("a", "revenue"), sample("b", "revenue"), sample("c", None))
    replies = (prediction("a", "revenue"), prediction("b", None), prediction("c", "revenue"))
    result = score(rows, replies)
    # Revenue TP=1, FP=1, FN=1 -> F1=0.5; every other canonical class has F1=0.
    assert result["macro_f1"] == pytest.approx(0.5 / 47)
    assert result["macro_class_count"] == 47
    assert result["accuracy"] == pytest.approx(1 / 3)
    assert result["null_only_accuracy"] == pytest.approx(1 / 3)
    assert result["false_mappings_on_abstain"] == 1
    assert result["plan_critical"]["errors"] == 1
    revenue = next(c for c in result["per_class"] if c["canonical_id"] == "revenue")
    assert revenue["precision"] == revenue["recall"] == revenue["f1"] == 0.5
    assert result["issuers"] == 1
    assert result["by_period"]["annual"]["rows"] == 3
    assert result["by_source_language"]["sec|en"]["rows"] == 3


@pytest.mark.parametrize(
    "replies",
    [
        (),
        (prediction("z", None),),
        (prediction("a", None), prediction("a", None)),
        (prediction("a", None), prediction("z", None)),
    ],
)
def test_prediction_ids_must_match_the_selection_exactly(replies: tuple[Prediction, ...]) -> None:
    with pytest.raises(DatasetError, match="prediction"):
        score((sample("a", None),), replies)


def test_duplicate_selection_ids_are_refused() -> None:
    with pytest.raises(DatasetError, match="duplicate"):
        score((sample("a", None), sample("a", None)), (prediction("a", None),))


def test_prompt_uses_only_label_and_statement() -> None:
    messages = inference_messages(sample("secret-id", "revenue", "secret-issuer"))
    assert [m["role"] for m in messages] == ["system", "user"]
    assert json.loads(messages[1]["content"]) == {"label": "Sales", "statement": "income"}
    assert "secret" not in json.dumps(messages)
    assert messages == inference_messages(sample("different-id", None, "different-issuer"))


def test_selection_is_order_independent_and_does_not_use_gold() -> None:
    rows = tuple(sample(str(i), None, f"cik:{i}") for i in range(20))
    selected = select_rows(rows, limit=7, seed=42)
    assert selected == select_rows(tuple(reversed(rows)), limit=7, seed=42)
    changed = tuple(r.model_copy(update={"canonical_id": "revenue"}) for r in rows)
    assert [r.example_id for r in selected] == [
        r.example_id for r in select_rows(changed, limit=7, seed=42)
    ]
    assert len(selected) == 7
    assert selected != select_rows(rows, limit=7, seed=3)


def test_bootstrap_is_paired_by_issuer_and_too_few_issuers_cannot_pass() -> None:
    rows = tuple(sample(str(i), "revenue", f"cik:{i // 2}") for i in range(8))
    base = tuple(prediction(r.example_id, None) for r in rows)
    adapter = tuple(prediction(r.example_id, "revenue") for r in rows)
    result = compare(rows, base, adapter, seed=4, resamples=1000)
    # All four issuers have the same paired difference: only revenue F1 changes.
    assert result["macro_f1_delta_ci95"] == pytest.approx([1 / 47, 1 / 47])
    assert result["disposition"] == "not shown"
    assert result == compare(rows, base, adapter, seed=4, resamples=1000)
    with pytest.raises(DatasetError, match="1000"):
        compare(rows, base, adapter, seed=4, resamples=999)


def test_identical_predictors_have_zero_paired_spread_on_heterogeneous_issuers() -> None:
    rows = (sample("a", "revenue", "cik:1"), sample("b", "revenue", "cik:2"))
    replies = (prediction("a", "revenue"), prediction("b", None))
    result = compare(rows, replies, replies, seed=7)
    assert result["macro_f1_delta_ci95"] == [0, 0]


def test_source_language_comparisons_are_never_pooled() -> None:
    sec = sample("a", "revenue")
    corpus = sample("b", "revenue").model_copy(update={"source": "corpus"})
    base = (prediction("a", None), prediction("b", None))
    adapter = (prediction("a", "revenue"), prediction("b", None))
    result = compare((sec, corpus), base, adapter, seed=0)
    assert result["macro_f1_delta_ci95"] is None
    assert result["by_source_language"]["corpus|en"]["macro_f1_delta_ci95"] == [0, 0]
    assert result["by_source_language"]["sec|en"]["macro_f1_delta_ci95"] == pytest.approx(
        [1 / 47, 1 / 47]
    )
