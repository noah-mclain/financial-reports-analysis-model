"""Draft SEC crosswalk and abstention policies from tag vocabularies and a curated mapping.

Only the tag vocabulary (``tag.txt``) of a Financial Statement Data Sets archive is read; it
holds tag definitions and no filer rows. Nothing here approves anything: every artifact is a draft.

Interpretations fixed here, in one place:

- A vocabulary ``version`` such as ``us-gaap/2024`` is ``<namespace>/<tag taxonomy version>``;
  the part before the slash is the namespace and the four digits after it the tag taxonomy
  version. Filer-defined tags (``custom`` = 1) are not in this vocabulary and share the single
  scope ``custom`` / ``extension``; the decoder that reads filer rows must map them there.
- The statement codes of ``pre.txt`` are listed in ``STATEMENT_CODES``. The decoder drops, with a
  counted reason and before resolution, any other code (``unsupported_statement_code``), any
  standard namespace other than the mapping's (``unsupported_namespace``) and any tag under the
  mapping's ``excluded`` section without its required presentation companion
  (``ambiguous_semantics``). It never turns a drop into a null.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from dataset_contracts import Contract, DatasetError, Sha256, Text, canonical_json, digest
from pydantic import Field, ValidationError
from sec_crosswalk import (
    AbstentionPolicy,
    Crosswalk,
    CrosswalkEntry,
    ReviewEvidence,
    TagKey,
    TagScope,
    taxonomy_sha256,
)

from fra_core.schemas.statement import StatementType
from fra_core.taxonomy import Taxonomy

TAG_MEMBER = "tag.txt"
TAG_COLUMNS = ("tag", "version", "custom", "abstract", "datatype", "iord", "crdr", "tlabel", "doc")
VERSION = re.compile(r"([a-z][a-z0-9-]*)/(\d{4})")
STATEMENT_CODES: Mapping[str, StatementType] = {
    "BS": StatementType.BALANCE,
    "IS": StatementType.INCOME,
    "CI": StatementType.COMPREHENSIVE_INCOME,
    "CF": StatementType.CASH_FLOW,
    "EQ": StatementType.EQUITY,
}
CUSTOM_NAMESPACE = "custom"
CUSTOM_VERSION = "extension"
DRAFT_PRODUCER_ID = "crosswalk-draft-1"
MAPPING_PATH = Path(__file__).parent / "data/sec_crosswalk/us_gaap_mapping.yaml"
SCHEMA_VERSION = 1


class TagDefinition(Contract):
    """One standard tag as one vocabulary version defines it."""

    tag: str
    namespace: str
    tag_taxonomy_version: str
    tlabel: str
    doc: str

    @property
    def version(self) -> str:
        return f"{self.namespace}/{self.tag_taxonomy_version}"


Vocabulary = Mapping[str, Mapping[str, TagDefinition]]


def read_archive_member(archive: Path, member: str) -> str:
    """The text of one archive member; only the tag vocabulary may be read."""
    if member != TAG_MEMBER:
        raise DatasetError(f"{archive}: only {TAG_MEMBER} may be read, not {member!r}")
    try:
        with zipfile.ZipFile(archive) as zf:
            try:
                data = zf.read(TAG_MEMBER)
            except KeyError:
                raise DatasetError(f"{archive}: {TAG_MEMBER} is missing") from None
    except (OSError, zipfile.BadZipFile) as exc:
        raise DatasetError(f"{archive}: cannot read archive: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{archive}:{TAG_MEMBER}: not UTF-8: {exc}") from exc


def read_tag_vocabulary(archive: Path) -> tuple[TagDefinition, ...]:
    """Standard (not custom, not abstract) tags of one archive, sorted by raw version, then tag."""
    where = f"{archive}:{TAG_MEMBER}"
    lines = read_archive_member(archive, TAG_MEMBER).split("\n")
    if tuple(lines[0].rstrip("\r").split("\t")) != TAG_COLUMNS:
        raise DatasetError(f"{where}:1: header is not {'/'.join(TAG_COLUMNS)}")
    found: dict[tuple[str, str], TagDefinition] = {}
    for number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.rstrip("\r").split("\t")
        if len(fields) != len(TAG_COLUMNS):
            raise DatasetError(f"{where}:{number}: expected {len(TAG_COLUMNS)} columns")
        tag, version, custom, abstract, *_, tlabel, doc = fields
        for name, flag in (("custom", custom), ("abstract", abstract)):
            if flag not in ("0", "1"):
                raise DatasetError(f"{where}:{number}: {name} flag {flag!r} is not 0 or 1")
        if custom == "1" or abstract == "1":
            continue
        parsed = VERSION.fullmatch(version)
        if parsed is None:
            raise DatasetError(f"{where}:{number}: version {version!r} is not <namespace>/<yyyy>")
        current = TagDefinition(
            tag=tag,
            namespace=parsed.group(1),
            tag_taxonomy_version=parsed.group(2),
            tlabel=tlabel,
            doc=doc,
        )
        previous = found.setdefault((current.version, tag), current)
        if previous != current:
            raise DatasetError(f"{where}:{number}: {tag} {version} is defined twice differently")
    return tuple(found[key] for key in sorted(found))


def merge_vocabularies(definitions: Iterable[TagDefinition]) -> dict[str, dict[str, TagDefinition]]:
    """Definitions grouped by raw version, then tag; a conflicting redefinition is refused."""
    merged: dict[str, dict[str, TagDefinition]] = defaultdict(dict)
    for current in definitions:
        previous = merged[current.version].setdefault(current.tag, current)
        if previous != current:
            raise DatasetError(f"{current.tag} {current.version} is defined differently twice")
    return dict(merged)


class MappingRow(Contract):
    statement: Literal["BS", "IS"]
    tag: str
    canonical_id: str | None
    doc_phrases: tuple[str, ...] = Field(min_length=1)
    reason: str


class CompanionRule(Contract):
    """A conditional target when a standard total line shares the presentation report."""

    tag: Text
    canonical_id: Text
    doc_phrases: tuple[Text, ...] = Field(min_length=1)


class MappingExclusion(Contract):
    """A tag dropped unless its optional presentation companion disambiguates it."""

    statement: Literal["BS", "IS"]
    tag: str
    reason: str
    companion: CompanionRule | None = None


class CuratedMapping(Contract):
    version: Literal[1]
    namespace: str
    unmapped_items: Mapping[str, str]
    excluded: tuple[MappingExclusion, ...]
    rows: tuple[MappingRow, ...]
    sha256: Sha256
    source_name: str

    @property
    def entry_rows(self) -> tuple[MappingRow, ...]:
        """Unconditional and conditional entries share target and vocabulary validation."""
        return self.rows + tuple(
            MappingRow(
                statement=x.statement,
                tag=x.tag,
                canonical_id=x.companion.canonical_id,
                doc_phrases=x.companion.doc_phrases,
                reason=x.reason,
            )
            for x in self.excluded
            if x.companion is not None
        )


def _check_targets(mapping: CuratedMapping, taxonomy: Taxonomy) -> None:
    items = {item.id: item for item in taxonomy.items}
    seen: set[tuple[str, str]] = set()
    dropped = [(x.statement, x.tag) for x in mapping.excluded]
    if len(set(dropped)) != len(dropped):
        raise DatasetError("an excluded tag is listed twice")
    for statement, tag in dropped:
        if any((row.statement, row.tag) == (statement, tag) for row in mapping.rows):
            raise DatasetError(f"{statement} {tag}: both a mapping row and excluded")
    for exclusion in mapping.excluded:
        rule = exclusion.companion
        if rule is None:
            continue
        if rule.tag == exclusion.tag:
            raise DatasetError(f"{exclusion.tag}: companion is self-referencing")
        if not any(
            row.statement == exclusion.statement
            and row.tag == rule.tag
            and row.canonical_id is not None
            for row in mapping.rows
        ):
            raise DatasetError(f"{exclusion.tag}: companion {rule.tag!r} missing positive mapping")
    for row in mapping.entry_rows:
        if (row.statement, row.tag) in seen:
            raise DatasetError(f"{row.statement} {row.tag}: mapped twice")
        seen.add((row.statement, row.tag))
        if row.canonical_id is None:
            continue
        item = items.get(row.canonical_id)
        if item is None or item.statement != STATEMENT_CODES[row.statement]:
            raise DatasetError(
                f"{row.statement} {row.tag}: {row.canonical_id!r} is not an item of that statement"
            )


def load_mapping(path: Path, taxonomy: Taxonomy) -> CuratedMapping:
    """The curated mapping, checked against the taxonomy: every item is mapped or accounted for."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read mapping: {exc.strerror}") from exc
    try:
        loaded = yaml.safe_load(data)
        if not isinstance(loaded, dict):
            raise DatasetError(f"{path}: expected a mapping at the top level")
        mapping = CuratedMapping.model_validate(
            {**loaded, "sha256": hashlib.sha256(data).hexdigest(), "source_name": path.name},
            strict=False,
        )
    except yaml.YAMLError as exc:
        raise DatasetError(f"{path}: {exc}") from exc
    except ValidationError as exc:
        raise DatasetError(f"{path}: {exc}") from exc
    try:
        _check_targets(mapping, taxonomy)
    except DatasetError as exc:
        raise DatasetError(f"{path}: {exc}") from exc
    mapped = {row.canonical_id for row in mapping.entry_rows if row.canonical_id}
    ids = {item.id for item in taxonomy.items}
    both = mapped & set(mapping.unmapped_items)
    if both or set(mapping.unmapped_items) - ids:
        raise DatasetError(f"{path}: unmapped_items must be taxonomy items without rows: {both}")
    missing = sorted(ids - mapped - set(mapping.unmapped_items))
    if missing:
        raise DatasetError(f"{path}: items neither mapped nor listed as unmapped: {missing}")
    return mapping


def _evidence_sha256(definition: TagDefinition) -> str:
    return digest({"tlabel": definition.tlabel, "doc": definition.doc})


def _review(mapping: CuratedMapping) -> ReviewEvidence:
    return ReviewEvidence(
        producer_id=DRAFT_PRODUCER_ID,
        status="draft",
        reference=f"curated mapping {mapping.source_name} with the {TAG_MEMBER} vocabularies",
        sha256=mapping.sha256,
    )


def unmatched_rows(vocabulary: Vocabulary, mapping: CuratedMapping) -> list[str]:
    """Mapping rows and excluded tags whose tag is in no supplied vocabulary version."""
    known = {
        tag
        for version, tags in vocabulary.items()
        if version.startswith(f"{mapping.namespace}/")
        for tag in tags
    }
    return [f"{row.statement} {row.tag}" for row in mapping.rows if row.tag not in known] + [
        f"{x.statement} {x.tag} (excluded)" for x in mapping.excluded if x.tag not in known
    ]


def build_draft(vocabulary: Vocabulary, mapping: CuratedMapping, taxonomy: Taxonomy) -> Crosswalk:
    """One entry per vocabulary version in which a mapped tag is defined; absent means none."""
    _check_targets(mapping, taxonomy)
    versions = sorted(v for v in vocabulary if v.startswith(f"{mapping.namespace}/"))
    if not versions:
        raise DatasetError(f"no {mapping.namespace} vocabulary version was supplied")
    entries: list[CrosswalkEntry] = []
    for version in versions:
        for row in mapping.entry_rows:
            definition = vocabulary[version].get(row.tag)
            if definition is None:
                continue
            if not any(phrase in definition.doc for phrase in row.doc_phrases):
                raise DatasetError(
                    f"{row.tag} {version}: none of the mapping phrases {row.doc_phrases} "
                    "is in its definition"
                )
            entries.append(
                CrosswalkEntry(
                    key=TagKey(
                        namespace=definition.namespace,
                        tag_taxonomy_version=definition.tag_taxonomy_version,
                        statement=STATEMENT_CODES[row.statement],
                        tag=row.tag,
                    ),
                    canonical_id=row.canonical_id,
                    abstention_reason=None if row.canonical_id else row.reason,
                    semantic_reference=f"{TAG_MEMBER} {version} {row.tag}",
                    semantic_evidence_sha256=_evidence_sha256(definition),
                    requires_companion=next(
                        (
                            exclusion.companion.tag
                            for exclusion in mapping.excluded
                            if exclusion.statement == row.statement
                            and exclusion.tag == row.tag
                            and exclusion.companion is not None
                        ),
                        None,
                    ),
                )
            )
    return Crosswalk(
        review=_review(mapping),
        canonical_taxonomy_version=taxonomy.version,
        canonical_taxonomy_sha256=taxonomy_sha256(taxonomy),
        schema_version=SCHEMA_VERSION,
        entries=tuple(sorted(entries, key=lambda e: canonical_json(e.key.model_dump(mode="json")))),
    )


def build_policies(
    vocabulary: Vocabulary, mapping: CuratedMapping, taxonomy: Taxonomy
) -> tuple[AbstentionPolicy, ...]:
    """Draft exclusions: outside the crosswalk and for negative controls, per scope group."""
    versions = sorted(v for v in vocabulary if v.startswith(f"{mapping.namespace}/"))
    if not versions:
        raise DatasetError(f"no {mapping.namespace} vocabulary version was supplied")
    covered = {STATEMENT_CODES[code] for code in ("BS", "IS")}
    uncovered = set(STATEMENT_CODES.values()) - covered

    def scopes(
        version_list: list[tuple[str, str]], statements: Iterable[StatementType]
    ) -> tuple[TagScope, ...]:
        return tuple(
            TagScope(namespace=ns, tag_taxonomy_version=v, statement=s)
            for ns, v in version_list
            for s in sorted(statements, key=lambda s: s.value)
        )

    standard = [(mapping.namespace, v.partition("/")[2]) for v in versions]
    custom = [(CUSTOM_NAMESPACE, CUSTOM_VERSION)]
    anchor = {
        "canonical_taxonomy_version": taxonomy.version,
        "canonical_taxonomy_sha256": taxonomy_sha256(taxonomy),
        "review": _review(mapping),
    }
    specs: list[
        tuple[str, Literal["outside_crosswalk", "negative_control"], tuple[TagScope, ...], str]
    ] = [
        (
            "outside-crosswalk-mapped-statements",
            "outside_crosswalk",
            scopes(standard, covered),
            "Standard tag on a balance sheet or income statement line the curated mapping omits.",
        ),
        (
            "outside-crosswalk-unmodeled-statements",
            "outside_crosswalk",
            scopes(standard, uncovered),
            "The taxonomy has no items for comprehensive income, cash flow or equity statements.",
        ),
        (
            "outside-crosswalk-custom",
            "outside_crosswalk",
            scopes(custom, STATEMENT_CODES.values()),
            "Filer-defined extension tag, which has no standard definition to map from.",
        ),
        (
            "negative-control-standard",
            "negative_control",
            scopes(standard, STATEMENT_CODES.values()),
            "Negative control filer, such as a bank or insurer: every line abstains.",
        ),
        (
            "negative-control-custom",
            "negative_control",
            scopes(custom, STATEMENT_CODES.values()),
            "Negative control filer's extension tag: every line abstains.",
        ),
    ]
    return tuple(
        AbstentionPolicy(policy_id=pid, kind=kind, scopes=sc, reason=reason, **anchor)  # type: ignore[arg-type]
        for pid, kind, sc, reason in specs
    )
