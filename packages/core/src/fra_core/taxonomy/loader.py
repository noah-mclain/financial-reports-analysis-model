"""Load and validate the canonical taxonomy.

Validated at load time so a bad file fails fast instead of mis-mapping labels mid-run.

Aliases are keyed per statement: "Non-controlling interests" is a profit line in the income
statement and an equity line in the balance sheet. Within one statement an alias names one item,
except where every item sharing it says which total it sits ``within`` and those totals differ:
"Lease liabilities" is printed under current and under non-current liabilities alike, and only
the section a row sums into tells the two apart.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from fra_core.labels import normalize_label
from fra_core.schemas.statement import StatementType

__all__ = ["PLAN_CRITICAL_IDS", "CanonicalItem", "Taxonomy", "load_taxonomy"]

_RESOURCE = "canonical_items.yaml"
_SUPPORTED_VERSION = 1
_REQUIRED_LANGUAGES = ("en", "ar")


@dataclass(frozen=True)
class CanonicalItem:
    """One canonical line item and the ways statements spell it."""

    id: str
    statement: StatementType
    natural_sign: Literal["+", "-"]
    critical: bool = False
    subtotal: bool = False
    per_share: bool = False
    # A mappingproxy cannot be a dataclass default: it is unhashable, and dataclasses reject
    # unhashable defaults. The built instances below are wrapped in MappingProxyType instead.
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # How statements print the heading of the section this total closes ("Current assets").
    # Not aliases: a heading never names the total, it only tells which total sits under it.
    headings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The total this item sums into, for an item whose alias another item shares.
    within: str | None = None

    def aliases_for(self, language: str) -> tuple[str, ...]:
        return self.aliases.get(language, ())

    def headings_for(self, language: str) -> tuple[str, ...]:
        return self.headings.get(language, ())


# The six items the plan names for Gate B (docs/blueprint/08-revised-plan.md, Gates): revenue, net
# profit, total assets, total equity, total current assets and total current liabilities. The
# taxonomy's `critical` set is wider (it adds the subtotals the identities need), so a report that
# speaks of the plan's six counts these and no others.
PLAN_CRITICAL_IDS = (
    "revenue",
    "net_income",
    "total_assets",
    "total_equity",
    "total_current_assets",
    "total_current_liabilities",
)


@dataclass(frozen=True)
class Taxonomy:
    """The loaded taxonomy, with lookup by alias."""

    version: int
    items: tuple[CanonicalItem, ...]
    _by_id: Mapping[str, CanonicalItem]
    _by_alias: Mapping[tuple[StatementType, str], CanonicalItem]
    # Headings a statement prints over its sections ("Assets", "Equity and liabilities"); a text
    # layer can run them into the first row below, like a section heading.
    statement_headings: Mapping[StatementType, tuple[str, ...]] = field(default_factory=dict)
    # sha256 of the file as read: what a cache of mapped statements must be keyed on, since an
    # alias can change without the version changing.
    digest: str = ""

    def by_id(self, canonical_id: str) -> CanonicalItem | None:
        return self._by_id.get(canonical_id)

    def for_statement(self, statement: StatementType) -> tuple[CanonicalItem, ...]:
        return tuple(i for i in self.items if i.statement is statement)

    def critical_ids(self, statement: StatementType | None = None) -> tuple[str, ...]:
        return tuple(
            i.id
            for i in self.items
            if i.critical and (statement is None or i.statement is statement)
        )

    def lookup(self, label: str, statement: StatementType) -> CanonicalItem | None:
        """Exact match on the normalized label, within one statement; None for an alias items
        share by section (``within``), which only a row's section settles.

        This is the first rung of the mapping ladder. Labels it does not resolve go on to the
        embedding matcher and then, if still unresolved, to the model.
        """
        return self._by_alias.get((statement, normalize_label(label)))


@lru_cache(maxsize=4)
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load the taxonomy, from the packaged resource unless a path is given."""
    text = _read_text(path)
    raw = _parse(text)
    version = raw.get("version")
    if version != _SUPPORTED_VERSION:
        msg = f"taxonomy version {version!r} is not supported (expected {_SUPPORTED_VERSION})"
        raise ValueError(msg)

    entries = raw.get("items")
    if not isinstance(entries, list) or not entries:
        msg = "taxonomy has no items"
        raise ValueError(msg)

    items = tuple(_build_item(entry) for entry in entries)
    by_id = _index_by_id(items)
    _check_within(items, by_id)
    by_alias = _index_by_alias(items)
    _check_headings(items, by_alias)
    return Taxonomy(
        version=version,
        items=items,
        _by_id=by_id,
        _by_alias=by_alias,
        statement_headings=_build_statement_headings(raw.get("statement_headings", {})),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _read_text(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    return resources.files(__package__).joinpath(_RESOURCE).read_text(encoding="utf-8")


def _parse(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        msg = "taxonomy file does not contain a mapping"
        raise ValueError(msg)
    return loaded


def _build_item(entry: Any) -> CanonicalItem:
    if not isinstance(entry, dict):
        msg = f"taxonomy item is not a mapping: {entry!r}"
        raise ValueError(msg)

    item_id = entry.get("id")
    if not isinstance(item_id, str) or not item_id:
        msg = f"taxonomy item has no id: {entry!r}"
        raise ValueError(msg)

    sign = entry.get("natural_sign", "+")
    if sign not in ("+", "-"):
        msg = f"{item_id}: natural_sign must be '+' or '-', got {sign!r}"
        raise ValueError(msg)

    raw_aliases = entry.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        msg = f"{item_id}: aliases must be a mapping of language to list"
        raise ValueError(msg)

    aliases: dict[str, tuple[str, ...]] = {}
    for language in _REQUIRED_LANGUAGES:
        values = raw_aliases.get(language)
        if not isinstance(values, list) or not values:
            msg = f"{item_id}: needs at least one {language} alias"
            raise ValueError(msg)
        aliases[language] = tuple(str(v) for v in values)

    subtotal = bool(entry.get("subtotal", False))
    headings = _build_headings(item_id, subtotal, entry.get("headings", {}))
    within = entry.get("within")
    if within is not None and (not isinstance(within, str) or not within):
        msg = f"{item_id}: within must name a total"
        raise ValueError(msg)

    return CanonicalItem(
        id=item_id,
        statement=StatementType(entry["statement"]),
        natural_sign=sign,
        critical=bool(entry.get("critical", False)),
        subtotal=subtotal,
        per_share=bool(entry.get("per_share", False)),
        aliases=MappingProxyType(aliases),
        headings=MappingProxyType(headings),
        within=within,
    )


def _build_headings(item_id: str, subtotal: bool, raw: Any) -> dict[str, tuple[str, ...]]:
    if not raw:
        return {}
    if not subtotal:
        msg = f"{item_id}: headings are only for a subtotal item"
        raise ValueError(msg)
    if not isinstance(raw, dict):
        msg = f"{item_id}: headings must be a mapping of language to list"
        raise ValueError(msg)
    headings: dict[str, tuple[str, ...]] = {}
    for language, values in raw.items():
        if language not in _REQUIRED_LANGUAGES or not isinstance(values, list):
            msg = f"{item_id}: headings need a list for each of {_REQUIRED_LANGUAGES}"
            raise ValueError(msg)
        headings[language] = tuple(str(v) for v in values)
    return headings


def _index_by_id(items: tuple[CanonicalItem, ...]) -> Mapping[str, CanonicalItem]:
    index: dict[str, CanonicalItem] = {}
    for item in items:
        if item.id in index:
            msg = f"duplicate canonical id: {item.id}"
            raise ValueError(msg)
        index[item.id] = item
    return MappingProxyType(index)


def _shared_by_section(a: CanonicalItem, b: CanonicalItem) -> bool:
    return a.within is not None and b.within is not None and a.within != b.within


def _index_by_alias(
    items: tuple[CanonicalItem, ...],
) -> Mapping[tuple[StatementType, str], CanonicalItem]:
    """Alias to its one item. An alias items share by section is left out: a lookup of it is
    None, and the mapper settles it by the row's section."""
    owners: dict[tuple[StatementType, str], list[CanonicalItem]] = {}
    for item in items:
        for language in _REQUIRED_LANGUAGES:
            for alias in item.aliases_for(language):
                key = (item.statement, normalize_label(alias))
                found = owners.setdefault(key, [])
                for existing in found:
                    if existing.id != item.id and not _shared_by_section(existing, item):
                        msg = (
                            f"alias {alias!r} maps to both {existing.id!r} and {item.id!r} "
                            f"within the {item.statement.value} statement"
                        )
                        raise ValueError(msg)
                if all(existing.id != item.id for existing in found):
                    found.append(item)
    return MappingProxyType({key: found[0] for key, found in owners.items() if len(found) == 1})


def _check_within(items: tuple[CanonicalItem, ...], by_id: Mapping[str, CanonicalItem]) -> None:
    for item in items:
        if item.within is None:
            continue
        total = by_id.get(item.within)
        if total is None or not total.subtotal or total.statement is not item.statement:
            msg = f"{item.id}: within {item.within!r} is not a total of the same statement"
            raise ValueError(msg)


def _build_statement_headings(raw: Any) -> Mapping[StatementType, tuple[str, ...]]:
    if not isinstance(raw, dict):
        msg = "statement_headings must be a mapping of statement to languages"
        raise ValueError(msg)
    built: dict[StatementType, tuple[str, ...]] = {}
    for statement, languages in raw.items():
        need = f"statement_headings.{statement}: needs a list for each of {_REQUIRED_LANGUAGES}"
        if not isinstance(languages, dict) or set(languages) != set(_REQUIRED_LANGUAGES):
            raise ValueError(need)
        for language in _REQUIRED_LANGUAGES:
            values = languages[language]
            # A bare string would iterate as one-letter headings and load without complaint.
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise ValueError(f"{need}; {language} is {values!r}")
        built[StatementType(statement)] = tuple(
            v for language in _REQUIRED_LANGUAGES for v in languages[language]
        )
    return MappingProxyType(built)


def _check_headings(
    items: tuple[CanonicalItem, ...], by_alias: Mapping[tuple[StatementType, str], CanonicalItem]
) -> None:
    for item in items:
        for language in _REQUIRED_LANGUAGES:
            for heading in item.headings_for(language):
                owner = by_alias.get((item.statement, normalize_label(heading)))
                if owner is not None and owner.id != item.id:
                    msg = (
                        f"heading {heading!r} of {item.id!r} is an alias of {owner.id!r} "
                        f"within the {item.statement.value} statement"
                    )
                    raise ValueError(msg)
