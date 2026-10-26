"""Optional post-baseline mapping. Deterministic evidence, never a proposal, grants support."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from fra_core.normalization import NormalizationSettings
from fra_core.normalization_prompt import SYSTEM_PROMPT_SHA256
from fra_core.schemas import CheckResult, MappingSource, StatementType
from fra_core.taxonomy import load_taxonomy
from fra_ingest.eps_checks import check_eps
from fra_ingest.figure_checks import check_net_profit_tie
from fra_ingest.label_match import LabelIndex
from fra_ingest.normalization_context import build_normalization_request
from fra_ingest.normalization_decision import assess_proposal
from fra_ingest.results import StructureResult
from fra_ingest.review import review_statement
from fra_ingest.table_checks import run_checks
from fra_model.runtime import LlamaServerRuntime, ModelRuntime


def _merge_checks(prior: Sequence[CheckResult], fresh: Sequence[CheckResult]) -> list[CheckResult]:
    # Rechecking does not erase an earlier failed or unsettled check, or duplicate its ID.
    rank = {"pass": 0, "skipped": 1, "fail": 2}
    merged = {c.id: c for c in prior}
    for check in fresh:
        previous = merged.get(check.id)
        if previous is None or rank[check.status] > rank[previous.status]:
            merged[check.id] = check
    return list(merged.values())


def apply_normalization(
    result: StructureResult, checks: Sequence[CheckResult], runtime: ModelRuntime
) -> tuple[StructureResult, list[CheckResult], list[dict[str, object]]]:
    """Run every eligible unmapped label, preserving baseline mappings and uncertainty.

    Numeric checks are reused unchanged when nothing is accepted. After acceptance the same
    numeric, EPS and cross-statement tie checkers run again; prior review holds remain.
    No proposal is supplied to an arithmetic checker as source evidence.
    """
    runtime.validate()
    taxonomy = load_taxonomy()
    statements = []
    audits: list[dict[str, object]] = []
    accepted = False
    for statement in result.statements:
        items = []
        own = [c for c in checks if c.statement_id == statement.id]
        for item in statement.line_items:
            if (
                statement.type not in (StatementType.BALANCE, StatementType.INCOME)
                or item.canonical_id is not None
                or not item.cells
                or not item.raw_label.strip()
            ):
                items.append(item)
                continue
            request = build_normalization_request(statement, item.id)
            response = runtime.normalize(request.payload.label, request.payload.statement)
            decision = assess_proposal(statement, item.id, taxonomy, own, response)
            audits.append(
                {
                    "line_item_id": item.id,
                    "statement_id": statement.id,
                    "system_prompt_sha256": SYSTEM_PROMPT_SHA256,
                    "response": response,
                    "baseline_mapping": {
                        "flag": item.mapping_flag,
                        "evidence": item.mapping_evidence,
                    },
                    "request_audit": request.audit.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                }
            )
            update: dict[str, object]
            if decision.state == "supported":
                accepted = True
                update = {
                    "canonical_id": decision.canonical_id,
                    "mapping_source": MappingSource.MODEL,
                    "mapping_flag": None,
                    "mapping_evidence": f"model_proposed:{decision.mapper_evidence}",
                }
            else:
                reason = (
                    "model_abstained"
                    if decision.state == "model_abstention"
                    else f"model_rejected:{decision.reason.split(':', 1)[0]}"
                )
                update = {
                    "mapping_flag": item.mapping_flag or "unmapped",
                    "mapping_evidence": reason,
                }
            items.append(item.model_copy(update=update))
        statements.append(statement.model_copy(update={"line_items": items}))
    updated_checks = list(checks)
    if accepted:
        index = LabelIndex(taxonomy)
        rechecked = [run_checks(s, index) for s in statements]
        statements = [s for s, _ in rechecked]
        eps = [
            c
            for s in statements
            for c in check_eps(s, result.eps_notes, result.statement_headers.get(s.id, ""))
        ]
        # Preserve every earlier failure/skip as a hold even if a later check passes.
        updated_checks = _merge_checks(
            checks, [c for _, numeric in rechecked for c in numeric] + eps
        )
        first = {
            kind: next((s for s in statements if s.type is kind), None) for kind in StatementType
        }
        income, comprehensive = (
            first[StatementType.INCOME],
            first[StatementType.COMPREHENSIVE_INCOME],
        )
        if income is not None and comprehensive is not None:
            updated_checks = _merge_checks(
                updated_checks, check_net_profit_tie(income, comprehensive)
            )
    old_reviews = {r.statement_id: r for r in result.reviews}
    reviews = []
    seen: set[StatementType] = set()
    for statement in statements:
        own = [c for c in updated_checks if c.statement_id == statement.id]
        if statement.type is StatementType.INCOME and statement.type not in seen:
            own += [c for c in updated_checks if c.kind == "net_profit_tie"]
        review = review_statement(statement, own, primary=statement.type not in seen)
        seen.add(statement.type)
        prior = old_reviews.get(statement.id)
        if prior is not None:
            reasons = list(dict.fromkeys([*prior.reasons, *review.reasons]))
            review = review.model_copy(
                update={
                    "status": "needs_review"
                    if prior.status == "needs_review" or reasons
                    else review.status,
                    "reasons": reasons,
                    "warnings": list(dict.fromkeys([*prior.warnings, *review.warnings])),
                }
            )
        if review.status == "needs_review" and "needs_review" not in statement.flags:
            statement.flags = [*statement.flags, "needs_review"]
        reviews.append(review)
    return (
        result.model_copy(update={"statements": statements, "reviews": reviews}),
        updated_checks,
        audits,
    )


def map_structure(
    result: StructureResult, artifact_root: Path, settings: NormalizationSettings
) -> StructureResult:
    """Separate artifacts, never stored under the deterministic structure cache's keys."""
    if not settings.enabled or (
        result.industry is not None and result.industry.outcome == "declined"
    ):
        return result
    root = artifact_root / result.sha256
    checks = [
        CheckResult.model_validate(c) for c in json.loads((root / "table_checks.json").read_text())
    ]
    mapped, checked, audit = apply_normalization(result, checks, LlamaServerRuntime(settings))
    directory = root / "normalization"
    directory.mkdir(exist_ok=True)
    (directory / "statements.json").write_text(mapped.model_dump_json(indent=2))
    (directory / "checks.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in checked], indent=2)
    )
    (directory / "audit.json").write_text(
        json.dumps(
            {"model": settings.model_dump(mode="json"), "rows": audit}, ensure_ascii=False, indent=2
        )
    )
    return mapped
