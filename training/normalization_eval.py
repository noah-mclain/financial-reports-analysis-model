"""Validation-only normalization records and scores, independent of an inference backend."""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any, Literal

from dataset_contracts import Contract, DatasetError, Text, digest
from dataset_exclusions import FrozenExclusions, checked, target_name

from fra_core.normalization_prompt import normalization_messages
from fra_core.schemas import StatementType
from fra_core.schemas.normalization import parse_normalization_answer
from fra_core.taxonomy import Taxonomy, load_taxonomy
from fra_core.taxonomy.loader import PLAN_CRITICAL_IDS

INVALID = "!invalid"
Counts = Counter[tuple[str | None, str | None]]


class EvalRow(Contract):
    example_id: Text
    issuer: Text
    statement: StatementType
    label: Text
    canonical_id: str | None
    source: Text
    language: Literal["en", "ar"]
    period: Literal["annual", "interim"]


class Prediction(Contract):
    example_id: Text
    response: str


def inference_messages(row: EvalRow) -> list[dict[str, str]]:
    """The exact dataset prompt, without its assistant target or evaluation metadata."""
    return normalization_messages(row.label, row.statement.value)


def parse_answer(
    response: str, statement: StatementType, taxonomy: Taxonomy
) -> tuple[Literal["valid", "invalid"], str | None]:
    return parse_normalization_answer(response, statement, taxonomy)


def select_rows(rows: Sequence[EvalRow], *, limit: int | None, seed: int) -> tuple[EvalRow, ...]:
    """Lowest seeded hashes of example ids; never stratify using target labels."""
    _unique_rows(rows)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise DatasetError("selection limit must be positive")
    ordered = sorted(
        rows,
        key=lambda r: (hashlib.sha256(f"{seed}:{r.example_id}".encode()).hexdigest(), r.example_id),
    )
    return tuple(sorted(ordered[:limit], key=lambda r: r.example_id))


def _unique_rows(rows: Sequence[EvalRow]) -> None:
    if not rows:
        raise DatasetError("empty evaluation selection")
    if len({r.example_id for r in rows}) != len(rows):
        raise DatasetError("duplicate evaluation example ids")


def _evaluated(
    rows: Sequence[EvalRow], predictions: Sequence[Prediction], taxonomy: Taxonomy
) -> dict[str, str | None]:
    _unique_rows(rows)
    if len({p.example_id for p in predictions}) != len(predictions):
        raise DatasetError("duplicate prediction ids")
    if {p.example_id for p in predictions} != {r.example_id for r in rows}:
        raise DatasetError("prediction ids differ from selection")
    replies = {p.example_id: p.response for p in predictions}
    result: dict[str, str | None] = {}
    for row in rows:
        if row.canonical_id is not None:
            item = taxonomy.by_id(row.canonical_id)
            if item is None or item.statement != row.statement:
                raise DatasetError(f"{row.example_id}: invalid evaluation target")
        status, cid = parse_answer(replies[row.example_id], row.statement, taxonomy)
        result[row.example_id] = cid if status == "valid" else INVALID
    return result


def _f1(counts: Counts, ids: Sequence[str | None]) -> tuple[list[dict[str, Any]], float]:
    actual: Counter[str | None] = Counter()
    predicted: Counter[str | None] = Counter()
    for (truth, prediction), count in counts.items():
        actual[truth] += count
        predicted[prediction] += count
    classes: list[dict[str, Any]] = []
    for cid in ids:
        tp, support, emitted = counts[cid, cid], actual[cid], predicted[cid]
        classes.append(
            {
                "canonical_id": cid,
                "support": support,
                "predicted": emitted,
                "correct": tp,
                "precision": tp / emitted if emitted else 0.0,
                "recall": tp / support if support else 0.0,
                "f1": 2 * tp / (support + emitted) if support + emitted else 0.0,
            }
        )
    return classes, sum(c["f1"] for c in classes) / len(ids)


def error_upper95(errors: int, n: int) -> float | None:
    """One-sided exact binomial upper bound; issuer dependence is reported separately."""
    if not n:
        return None
    if errors == n:
        return 1.0
    if errors == 0:
        return -math.expm1(math.log(0.05) / n)
    low, high = errors / n, 1.0
    coefficients = [
        math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) for k in range(errors + 1)
    ]
    for _ in range(55):
        p = (low + high) / 2
        terms = [c + k * math.log(p) + (n - k) * math.log1p(-p) for k, c in enumerate(coefficients)]
        peak = max(terms)
        cdf = math.exp(peak) * sum(math.exp(v - peak) for v in terms)
        if cdf > 0.05:
            low = p
        else:
            high = p
    return (low + high) / 2


def _critical_sets(taxonomy: Taxonomy) -> dict[str, set[str]]:
    return {
        "plan_critical": set(PLAN_CRITICAL_IDS),
        "taxonomy_critical": {i.id for i in taxonomy.items if i.critical},
    }


def _summary(counts: Counts, taxonomy: Taxonomy) -> dict[str, Any]:
    ids = tuple(item.id for item in taxonomy.items)
    per_class, macro = _f1(counts, ids)
    _, with_null = _f1(counts, (*ids, None))
    n = sum(counts.values())
    nulls = sum(v for (t, _), v in counts.items() if t is None)
    correct = sum(v for (t, p), v in counts.items() if t == p)
    critical = []
    for name, items in _critical_sets(taxonomy).items():
        support = sum(v for (t, _), v in counts.items() if t in items)
        ok = sum(v for (t, p), v in counts.items() if t in items and t == p)
        critical.append(
            (
                name,
                {
                    "support": support,
                    "errors": support - ok,
                    "accuracy": ok / support if support else None,
                    "error_upper95": error_upper95(support - ok, support),
                },
            )
        )
    return {
        "rows": n,
        "accuracy": correct / n,
        "macro_f1": macro,
        "macro_class_count": len(ids),
        "macro_f1_with_abstention": with_null,
        "supported_classes": sum(c["support"] > 0 for c in per_class),
        "invalid_outputs": sum(v for (_, p), v in counts.items() if p == INVALID),
        "abstain_support": nulls,
        "correct_abstentions": counts[None, None],
        "false_mappings_on_abstain": sum(
            v for (t, p), v in counts.items() if t is None and p not in (None, INVALID)
        ),
        "null_only_accuracy": nulls / n,
        "per_class": per_class,
        **dict(critical),
    }


def score(
    rows: Sequence[EvalRow], predictions: Sequence[Prediction], taxonomy: Taxonomy | None = None
) -> dict[str, Any]:
    taxonomy = taxonomy or load_taxonomy()
    evaluated = _evaluated(rows, predictions, taxonomy)

    def summarize_group(group: Sequence[EvalRow]) -> dict[str, Any]:
        counts: Counts = Counter((r.canonical_id, evaluated[r.example_id]) for r in group)
        summary = _summary(counts, taxonomy)
        summary["issuers"] = len({r.issuer for r in group})
        for name, ids in _critical_sets(taxonomy).items():
            critical = summary[name]
            critical["issuers"] = len({r.issuer for r in group if r.canonical_id in ids})
            critical["disposition"] = (
                "not shown"
                if critical["support"] < 300 or critical["issuers"] < 30
                else "validation diagnostic, not Gate C"
            )
        return summary

    result = summarize_group(rows)
    for name, keys in (
        ("by_period", {r.period for r in rows}),
        ("by_source_language", {f"{r.source}|{r.language}" for r in rows}),
    ):
        result[name] = {}
        for key in sorted(keys):
            selected = [
                r
                for r in rows
                if (r.period if name == "by_period" else f"{r.source}|{r.language}") == key
            ]
            result[name][key] = summarize_group(selected)
    by_issuer: dict[str, list[EvalRow]] = defaultdict(list)
    for row in rows:
        by_issuer[row.issuer].append(row)
    result["per_issuer"] = []
    for issuer, part in sorted(by_issuer.items()):
        summary = summarize_group(part)
        result["per_issuer"].append(
            {
                "issuer": issuer,
                **{
                    k: summary[k]
                    for k in (
                        "rows",
                        "accuracy",
                        "macro_f1",
                        "invalid_outputs",
                        "plan_critical",
                        "taxonomy_critical",
                    )
                },
            }
        )
    result["disposition"] = "validation diagnostic, not Gate C"
    result["notes"] = [
        "Macro-F1 uses every canonical id, including unsupported ids; null is reported separately.",
        "Binomial bounds assume independent examples; rows from one issuer are dependent.",
        "SEC supervision comes from approved crosswalks, not independent PDF gold labels.",
    ]
    return result


def population_report(
    rows: Sequence[EvalRow], predictions: Sequence[Prediction], artifact: FrozenExclusions
) -> dict[str, Any]:
    """Continuity score and two separately labelled diagnostic populations."""
    checked(artifact)
    full = score(rows, predictions)
    keys = frozenset(
        artifact.keys
        if artifact.version == 1
        else (r.input_key for r in artifact.removals or () if r.reason == "ambiguous")
    )
    ambiguous: dict[str, list[EvalRow]] = defaultdict(list)
    retained = []
    for row in rows:
        key = inference_messages(row)[1]["content"]
        if key in keys:
            ambiguous[key].append(row)
        else:
            retained.append(row)
    replies = {p.example_id: p for p in predictions}

    def population(part: Sequence[EvalRow], label: str) -> dict[str, Any]:
        return {
            "label": label,
            "rows": len(part),
            "score": score(part, tuple(replies[r.example_id] for r in part)) if part else None,
        }

    diagnostic = population(
        [r for group in ambiguous.values() for r in group],
        "Ambiguity-only validation diagnostic; not an acceptance population",
    )
    diagnostic["distinct_inputs"] = len(ambiguous)
    diagnostic["inputs"] = [
        {
            "input": key,
            "rows": len(group),
            "target_breakdown": dict(
                sorted(Counter(target_name(r.canonical_id) for r in group).items())
            ),
            "predictions": [
                {**replies[r.example_id].model_dump(mode="json"), "target": r.canonical_id}
                for r in group
            ],
        }
        for key, group in sorted(ambiguous.items())
    ]
    return {
        "exclusion_keys_sha256": digest(artifact.keys),
        **(
            {"exclusion_artifact_content_sha256": artifact.keys_sha256}
            if artifact.version == 2
            else {}
        ),
        "full_validation": {
            "label": "Full validation, continuity population",
            "rows": len(rows),
            "score": full,
        },
        "validation_excluding_ambiguity": population(
            retained,
            "Validation excluding ambiguity; different population, "
            "not comparable to full validation",
        ),
        "ambiguity_only": diagnostic,
    }


def compare(
    rows: Sequence[EvalRow],
    base: Sequence[Prediction],
    adapter: Sequence[Prediction],
    *,
    seed: int,
    resamples: int = 1000,
    taxonomy: Taxonomy | None = None,
) -> dict[str, Any]:
    """Paired resampling of issuers, separately for each source/language stratum."""
    if resamples < 1000:
        raise DatasetError("at least 1000 bootstrap resamples are required")
    taxonomy = taxonomy or load_taxonomy()
    a, b = _evaluated(rows, base, taxonomy), _evaluated(rows, adapter, taxonomy)
    ids = tuple(i.id for i in taxonomy.items)
    strata = sorted({f"{r.source}|{r.language}" for r in rows})
    results: dict[str, dict[str, Any]] = {}
    for stratum in strata:
        parts: dict[str, tuple[Counts, Counts]] = {}
        selected = [r for r in rows if f"{r.source}|{r.language}" == stratum]
        for row in selected:
            ca, cb = parts.setdefault(row.issuer, (Counter(), Counter()))
            ca[row.canonical_id, a[row.example_id]] += 1
            cb[row.canonical_id, b[row.example_id]] += 1
        issuers = sorted(parts)
        rng, deltas = random.Random(seed), []
        for _ in range(resamples):
            ca, cb = Counter(), Counter()
            for issuer in rng.choices(issuers, k=len(issuers)):
                ca.update(parts[issuer][0])
                cb.update(parts[issuer][1])
            deltas.append(_f1(cb, ids)[1] - _f1(ca, ids)[1])
        deltas.sort()
        interval = [deltas[int(0.025 * (resamples - 1))], deltas[int(0.975 * (resamples - 1))]]
        results[stratum] = {
            "issuers": len(issuers),
            "rows": len(selected),
            "macro_f1_delta_ci95": interval,
            "disposition": "validation diagnostic, not Gate C"
            if len(issuers) >= 30
            else "not shown",
        }
    return {
        "seed": seed,
        "resamples": resamples,
        "by_source_language": results,
        "macro_f1_delta_ci95": next(iter(results.values()))["macro_f1_delta_ci95"]
        if len(results) == 1
        else None,
        "disposition": "not shown"
        if any(v["issuers"] < 30 for v in results.values())
        else "validation diagnostic, not Gate C",
    }
