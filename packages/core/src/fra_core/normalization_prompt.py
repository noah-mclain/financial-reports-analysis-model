"""Exact label-only prompt contract shared by training, scoring and serving."""

from __future__ import annotations

import json

from fra_core.schemas.normalization import NormalizationAnswer
from fra_core.taxonomy import Taxonomy, load_taxonomy


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def normalization_prompt(taxonomy: Taxonomy) -> str:
    """One instruction, closed catalog and response schema for training and inference."""
    catalog: dict[str, list[str]] = {}
    for item in sorted(taxonomy.items, key=lambda i: i.id):
        catalog.setdefault(item.statement.value, []).append(item.id)
    return "\n".join(
        (
            "Map the financial statement line item to an id listed for its statement in the "
            "catalog below. If the statement has no listed ids or none matches, use null. "
            "Return only a JSON object matching the schema below. No explanation or markdown.",
            _canonical_json(catalog),
            _canonical_json(NormalizationAnswer.model_json_schema()),
        )
    )


SYSTEM_PROMPT = normalization_prompt(load_taxonomy())
SYSTEM_PROMPT_SHA256 = "bba5f9cdf6ea3e553c9b899a0d4419128e84f6255f73870dba2090ded1bc0606"


def render_input(label: str, statement: str) -> str:
    """Preserve literal Unicode and whitespace; context and audit are never sent."""
    return _canonical_json({"label": label, "statement": statement})


def normalization_messages(label: str, statement: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_input(label, statement)},
    ]
