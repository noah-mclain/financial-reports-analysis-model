import hashlib
import json

import pytest
from dataset_builder import SYSTEM_PROMPT as TRAIN_PROMPT
from dataset_builder import render_input as train_input
from normalization_eval import EvalRow, inference_messages

from fra_core.normalization_prompt import SYSTEM_PROMPT, normalization_messages, render_input
from fra_core.schemas import StatementType


@pytest.mark.parametrize("label", ['  الإيرادات\n\t"x"  ', "Lease liabilities", "é\u0301"])
def test_exact_training_contract(label: str) -> None:
    assert (
        hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
        == "bba5f9cdf6ea3e553c9b899a0d4419128e84f6255f73870dba2090ded1bc0606"
    )
    assert SYSTEM_PROMPT == TRAIN_PROMPT
    assert render_input(label, "balance") == train_input(label, "balance")
    row = EvalRow(
        example_id="x",
        issuer="x",
        statement=StatementType.BALANCE,
        label=label,
        canonical_id=None,
        source="pdf",
        language="en",
        period="annual",
    )
    assert normalization_messages(label, "balance") == inference_messages(row)
    assert json.loads(normalization_messages(label, "balance")[1]["content"]) == {
        "label": label,
        "statement": "balance",
    }
