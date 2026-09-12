"""Load and validate the canonical taxonomy.

Validated at load time so a bad file fails fast instead of mis-mapping labels mid-run.

Aliases are keyed per statement: "Non-controlling interests" is a profit line in the income
statement and an equity line in the balance sheet.
"""

from __future__ import annotations

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

__all__ = ["CanonicalItem", "Taxonomy", "load_taxonomy"]

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

    def aliases_for(self, language: str) -> tuple[str, ...]:
        return self.aliases.get(language, ())


@dataclass(frozen=True)
class Taxonomy:
    """The loaded taxonomy, with lookup by alias."""

    version: int
    items: tuple[CanonicalItem, ...]
    _by_id: Mapping[str, CanonicalItem]
    _by_alias: Mapping[tuple[StatementType, str], CanonicalItem]

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
        """Exact match on the normalized label, within one statement.

        This is the first rung of the mapping ladder. Labels it does not resolve go on to the
        embedding matcher and then, if still unresolved, to the model.
        """
        return self._by_alias.get((statement, normalize_label(label)))


@lru_cache(maxsize=4)
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load the taxonomy, from the packaged resource unless a path is given."""
    raw = _read(path)
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
    by_alias = _index_by_alias(items)
    return Taxonomy(version=version, items=items, _by_id=by_id, _by_alias=by_alias)


def _read(path: Path | None) -> dict[str, Any]:
    if path is not None:
        text = path.read_text(encoding="utf-8")
    else:
        text = resources.files(__package__).joinpath(_RESOURCE).read_text(encoding="utf-8")
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

    return CanonicalItem(
        id=item_id,
        statement=StatementType(entry["statement"]),
        natural_sign=sign,
        critical=bool(entry.get("critical", False)),
        subtotal=bool(entry.get("subtotal", False)),
        per_share=bool(entry.get("per_share", False)),
        aliases=MappingProxyType(aliases),
    )


def _index_by_id(items: tuple[CanonicalItem, ...]) -> Mapping[str, CanonicalItem]:
    index: dict[str, CanonicalItem] = {}
    for item in items:
        if item.id in index:
            msg = f"duplicate canonical id: {item.id}"
            raise ValueError(msg)
        index[item.id] = item
    return MappingProxyType(index)


def _index_by_alias(
    items: tuple[CanonicalItem, ...],
) -> Mapping[tuple[StatementType, str], CanonicalItem]:
    index: dict[tuple[StatementType, str], CanonicalItem] = {}
    for item in items:
        for language in _REQUIRED_LANGUAGES:
            for alias in item.aliases_for(language):
                key = (item.statement, normalize_label(alias))
                existing = index.get(key)
                if existing is not None and existing.id != item.id:
                    msg = (
                        f"alias {alias!r} maps to both {existing.id!r} and {item.id!r} "
                        f"within the {item.statement.value} statement"
                    )
                    raise ValueError(msg)
                index[key] = item
    return MappingProxyType(index)
