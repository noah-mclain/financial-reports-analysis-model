"""Role assessments for SEC filings from one reviewed rule table.

The reviewer approves the rule table. One ``RoleAssessment`` per filing is derived from the
table and the submission roster, and approval refuses any assessment that does not equal the
one recomputed from the roster. The per-filing approval records are issued under that single
verdict; they authenticate nothing beyond the verdict they cite.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from dataset_contracts import Contract, DatasetError, Sha256, digest, text_lines
from pydantic import ValidationError
from sec_crosswalk import ApprovalRecord, ReviewEvidence, RoleAssessment
from sec_source_preparation import FilingRow

RULES_PATH = Path(__file__).parent / "data/sec_crosswalk/role_rules.yaml"
ROLE_PRODUCER_ID = "role-rules-1"
Role = Literal["corporate", "negative_control", "unknown"]


class RoleRules(Contract):
    version: Literal[1]
    negative_control_min: int
    negative_control_max: int
    sha256: Sha256
    source_name: str


class ReviewedRoles(Contract):
    assessments: tuple[RoleAssessment, ...]
    approvals: tuple[ApprovalRecord, ...]


def load_rules(path: Path) -> RoleRules:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read rule table: {exc.strerror}") from exc
    try:
        loaded = yaml.safe_load(data)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("negative_control_sic"), dict):
            raise DatasetError(f"{path}: expected version and negative_control_sic")
        band = loaded["negative_control_sic"]
        rules = RoleRules.model_validate(
            {
                "version": loaded.get("version"),
                "negative_control_min": band.get("min"),
                "negative_control_max": band.get("max"),
                "sha256": hashlib.sha256(data).hexdigest(),
                "source_name": path.name,
            }
        )
    except (yaml.YAMLError, ValidationError) as exc:
        raise DatasetError(f"{path}: {exc}") from exc
    if not 0 <= rules.negative_control_min <= rules.negative_control_max <= 9999:
        raise DatasetError(f"{path}: the negative control band is not a SIC range")
    return rules


def role_for_sic(rules: RoleRules, sic: str) -> Role:
    if len(sic) != 4 or not sic.isascii() or not sic.isdigit():
        return "unknown"
    if rules.negative_control_min <= int(sic) <= rules.negative_control_max:
        return "negative_control"
    return "corporate"


def draft_assessments(filings: Sequence[FilingRow], rules: RoleRules) -> tuple[RoleAssessment, ...]:
    """One draft assessment per filing, sorted by accession."""
    return tuple(
        RoleAssessment(
            review=ReviewEvidence(
                producer_id=ROLE_PRODUCER_ID,
                status="draft",
                reference=f"role rule table {rules.source_name} {rules.sha256} for filing {f.adsh}",
                sha256=rules.sha256,
            ),
            source_id=f.adsh,
            role=role_for_sic(rules, f.sic),
        )
        for f in sorted(filings, key=lambda f: f.adsh)
    )


def roles_header(rules: RoleRules, assessments: Sequence[RoleAssessment]) -> dict[str, object]:
    """What the reviewer's verdict covers: the rule table and the derived assessment list."""
    hashes = sorted(a.content_sha256() for a in assessments)
    return {
        "kind": "roles-subjects",
        "rule_table_sha256": rules.sha256,
        "assessments_sha256": digest(hashes),
        "count": len(hashes),
        "roles": dict(sorted(Counter(a.role for a in assessments).items())),
    }


def _verdict_key(rules: RoleRules) -> str:
    return f"rule-table:{rules.sha256}"


def approve_roles(
    assessments: Sequence[RoleAssessment],
    filings: Sequence[FilingRow],
    rules: RoleRules,
    verdicts: bytes,
    *,
    reviewer_id: str,
    authority_id: str,
    evidence_reference: str,
) -> ReviewedRoles:
    """Reviewed copies and one approval record each, if the single verdict approves the table."""
    given = tuple(RoleAssessment.model_validate(a) for a in assessments)
    ids = [a.source_id for a in given]
    if len(set(ids)) != len(ids):
        raise DatasetError("a role assessment appears twice")
    derived = draft_assessments(filings, rules)
    if sorted(a.content_sha256() for a in given) != sorted(a.content_sha256() for a in derived):
        raise DatasetError(
            "the assessments differ from those recomputed from the roster and the rule table"
        )
    try:
        rows = [json.loads(line) for _, line in text_lines(verdicts.decode("utf-8"))]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"the verdict file is not JSON lines: {exc}") from exc
    if not rows:
        raise DatasetError("the verdict file holds no verdict")
    if rows[0] != roles_header(rules, derived):
        raise DatasetError(
            "the verdict file's header differs from the rule table and assessments given"
        )
    if len(rows) != 2 or not isinstance(rows[1], dict) or rows[1].get("key") != _verdict_key(rules):
        raise DatasetError(
            f"expected exactly one verdict, for the rule table {_verdict_key(rules)}"
        )
    if rows[1].get("verdict") != "approve":
        raise DatasetError(f"the rule table was not approved: {rows[1].get('verdict')!r} (reject)")
    reviewed = tuple(
        a.model_copy(update={"review": a.review.model_copy(update={"status": "reviewed"})})
        for a in derived
    )
    # The evidence binds the rule table, the derived list and the verdict file itself.
    evidence = digest(
        {
            "rule_table": rules.sha256,
            "assessments": sorted(a.content_sha256() for a in derived),
            "verdicts": hashlib.sha256(verdicts).hexdigest(),
        }
    )
    approvals = tuple(
        ApprovalRecord(
            subject_kind="role_assessment",
            subject_sha256=a.content_sha256(),
            producer_id=a.review.producer_id,
            reviewer_id=reviewer_id,
            authority_id=authority_id,
            status="approved",
            evidence_reference=evidence_reference,
            evidence_sha256=evidence,
        )
        for a in reviewed
    )
    return ReviewedRoles(
        assessments=tuple(RoleAssessment.model_validate(a) for a in reviewed), approvals=approvals
    )
