"""The shipped reply contract remains strict and byte compatible."""

import hashlib
import json

import pytest

from fra_core.schemas import StatementType
from fra_core.schemas.normalization import NormalizationAnswer, parse_normalization_answer
from fra_core.taxonomy import load_taxonomy


def test_shipped_schema_digest() -> None:
    encoded = json.dumps(
        NormalizationAnswer.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "9b169a03e9d7d29ec063bdbd0739b8d98b54f9975dcb62b61a4bff1da3dd8552"
    )


@pytest.mark.parametrize(
    "response",
    [
        "null",
        "{}",
        "bad",
        "[]",
        '{"canonical_id":false}',
        '{"canonical_id":"unknown"}',
        '{"canonical_id":"total_assets"}',
        '{"canonical_id":null,"extra":1}',
        '{"canonical_id":null,"canonical_id":null}',
    ],
)
def test_invalid_is_not_semantic_null(response: str) -> None:
    assert parse_normalization_answer(response, StatementType.INCOME, load_taxonomy()) == (
        "invalid",
        None,
    )


@pytest.mark.parametrize("canonical", [None, "revenue"])
def test_valid_reply(canonical: str | None) -> None:
    assert parse_normalization_answer(
        json.dumps({"canonical_id": canonical}), StatementType.INCOME, load_taxonomy()
    ) == ("valid", canonical)
