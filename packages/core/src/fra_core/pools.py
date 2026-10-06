"""Authoritative top-level issuer pools; train subdivision remains in ``split``.

Recorded assignments win over either source's legacy default. Names and CIKs are
linked only by a supplied identity, never fuzzy matched. Conflicts require review.
The local metadata file is written before SEC output, without reading label datasets.
This module holds values only; reading, writing and locking that file is ``scripts/pool_store.py``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from fra_core.split import issuer_key


class PoolError(ValueError):
    """Missing identity, invalid metadata or conflicting recorded assignments."""


class Pool(StrEnum):
    DEV = "dev"
    TRAIN = "train"
    MODEL_TEST = "model_test"
    BLIND = "blind"


class Source(StrEnum):
    PDF = "pdf"
    SEC = "sec"


@dataclass(frozen=True)
class Identity:
    name: str | None = None
    cik: int | str | None = None

    def __post_init__(self) -> None:
        if self.name is not None:
            if not isinstance(self.name, str) or not issuer_key(self.name):
                raise PoolError(f"invalid issuer name {self.name!r}")
            object.__setattr__(self, "name", issuer_key(self.name))
        if self.cik is not None:
            if (
                isinstance(self.cik, bool)
                or not isinstance(self.cik, (int, str))
                or not re.fullmatch(r"[0-9]{1,10}", str(self.cik))
                or int(self.cik) <= 0
            ):
                raise PoolError(f"invalid CIK {self.cik!r}")
            object.__setattr__(self, "cik", int(self.cik))
        if self.name is None and self.cik is None:
            raise PoolError("issuer identity needs a name or CIK")

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Identity:
        name, cik = document.get("issuer"), document.get("cik")
        if not isinstance(name, str):
            raise PoolError(f"invalid or missing issuer name {name!r}")
        if "cik" in document and cik is None:
            raise PoolError("explicit CIK cannot be null")
        if cik is not None and not isinstance(cik, (int, str)):
            raise PoolError(f"invalid CIK {cik!r}")
        return cls(name, cik)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in (
                f"name:{self.name}" if self.name else "",
                f"cik:{self.cik}" if self.cik else "",
            )
            if key
        )


@dataclass(eq=False)
class _Assignment:
    pool: Pool
    names: set[str] = field(default_factory=set)
    cik: int | None = None


class PoolRegistry:
    """One assignment per connected name/CIK identity, preserving all recorded aliases."""

    def __init__(self, *, ambiguous_names: Iterable[str] = ()) -> None:
        self._by_key: dict[str, _Assignment] = {}
        self.sec_outputs: set[str] = set()
        self._ambiguous_names: set[str] = set()
        for name in ambiguous_names:
            if (
                not isinstance(name, str)
                or not name
                or issuer_key(name) != name
                or name in self._ambiguous_names
            ):
                raise PoolError(f"invalid or duplicate normalized ambiguous_names entry {name!r}")
            self._ambiguous_names.add(name)

    def _keys(self, identity: Identity) -> tuple[str, ...]:
        if identity.name in self._ambiguous_names:
            if identity.cik is None:
                raise PoolError(
                    f"{identity}: ambiguous name; supply a reviewed CIK; existing name-only "
                    "records require reviewed CIK identities before conversion"
                )
            return (f"cik:{identity.cik}",)
        return identity.keys

    def _matches(self, identity: Identity) -> set[_Assignment]:
        matches = {self._by_key[key] for key in self._keys(identity) if key in self._by_key}
        ciks = {a.cik for a in matches if a.cik is not None}
        if identity.cik is not None:
            ciks.add(int(identity.cik))
        if len(ciks) > 1:
            raise PoolError(
                f"{identity}: one issuer name links different CIKs {sorted(ciks)}; review "
                "both identities and preserved pools before declaring ambiguous_names in "
                "registry metadata; name-only PDF records need reviewed CIKs"
            )
        if len({a.pool for a in matches}) > 1:
            raise PoolError(
                f"{identity}: issuer spans pools {sorted(str(a.pool) for a in matches)}"
            )
        return matches

    def register(self, identity: Identity, pool: str) -> Pool:
        try:
            chosen = Pool(pool)
        except (ValueError, TypeError) as exc:
            raise PoolError(f"{identity}: invalid pool {pool!r}") from exc
        matches = self._matches(identity)
        if any(a.pool != chosen for a in matches):
            pools = sorted({str(chosen), *(str(a.pool) for a in matches)})
            raise PoolError(f"{identity}: issuer spans pools {pools}")
        assignment = next(iter(matches), _Assignment(chosen))
        if identity.name:
            assignment.names.add(identity.name)
        if identity.cik is not None:
            assignment.cik = int(identity.cik)
        for other in matches:
            assignment.names.update(other.names)
            assignment.cik = assignment.cik or other.cik
        for name in assignment.names:
            if name not in self._ambiguous_names:
                self._by_key[f"name:{name}"] = assignment
        if assignment.cik is not None:
            self._by_key[f"cik:{assignment.cik}"] = assignment
        return chosen

    def assign(self, identity: Identity, source: Source) -> Pool:
        if source not in (Source.PDF, Source.SEC):
            raise PoolError(f"invalid source {source!r}")
        matches = self._matches(identity)
        if matches:
            return self.register(identity, next(iter(matches)).pool)
        if source == Source.SEC:
            if identity.cik is None:
                raise PoolError(f"{identity}: new SEC allocation needs a CIK")
            key = f"cik:{identity.cik}"
            bucket = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 100
            pool = Pool.MODEL_TEST if bucket < 15 else Pool.TRAIN
        elif source == Source.PDF:
            if identity.name is None:
                raise PoolError(f"{identity}: new PDF allocation needs an issuer name")
            bucket = int(hashlib.sha256(identity.name.encode()).hexdigest(), 16) % 100
            pool = Pool.TRAIN if bucket < 65 else Pool.MODEL_TEST if bucket < 85 else Pool.BLIND
        else:
            raise PoolError(f"invalid source {source!r}")
        return self.register(identity, pool)

    def record_documents(self, documents: Iterable[Mapping[str, object]]) -> None:
        for document in documents:
            pool = document.get("pool")
            if not isinstance(pool, str):
                raise PoolError(f"{document.get('issuer')!r}: missing or invalid pool {pool!r}")
            self.register(Identity.from_document(document), pool)

    def cik_for(self, identity: Identity) -> int | None:
        matches = self._matches(identity)
        return next(iter(matches)).cik if matches else None

    def alias(self, name: str, target: Identity) -> None:
        """Link a caller's explicitly reviewed rename or exact URL match."""
        matches = self._matches(target)
        if not matches:
            raise PoolError(f"alias {name!r}: target {target} has no recorded assignment")
        group = next(iter(matches))
        alias = Identity(name, group.cik)
        self.register(alias, group.pool)
        other = next(iter(self._matches(alias)))
        group.names.update(other.names)
        group.cik = group.cik or other.cik
        for key in (
            *[f"name:{n}" for n in group.names if n not in self._ambiguous_names],
            *([f"cik:{group.cik}"] if group.cik else []),
        ):
            self._by_key[key] = group

    def cik_pools(self) -> dict[int, str]:
        return {a.cik: str(a.pool) for a in set(self._by_key.values()) if a.cik is not None}

    def metadata(self) -> dict[str, object]:
        assignments = [
            {"names": sorted(a.names), "cik": a.cik, "pool": a.pool}
            for a in set(self._by_key.values())
        ]
        assignments.sort(key=lambda a: (str(a["cik"]), str(a["names"])))
        return {
            "version": 1,
            "assignments": assignments,
            "sec_outputs": sorted(self.sec_outputs),
            "ambiguous_names": sorted(self._ambiguous_names),
        }

    @classmethod
    def from_metadata(cls, data: object, source: str) -> PoolRegistry:
        """Rebuild a registry from a ``metadata()`` dict; ``source`` names it in errors."""
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise PoolError(f"{source}: expected pool metadata version 1")
        entries, outputs = data.get("assignments"), data.get("sec_outputs")
        if not isinstance(entries, list) or not isinstance(outputs, list):
            raise PoolError(f"{source}: expected assignments and sec_outputs lists")
        ambiguous_names = data.get("ambiguous_names", [])
        if not isinstance(ambiguous_names, list):
            raise PoolError(f"{source}: expected ambiguous_names list")
        registry = cls(ambiguous_names=ambiguous_names)
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("names"), list):
                raise PoolError(f"{source}: invalid assignment {entry!r}")
            names, cik, pool = entry["names"], entry.get("cik"), entry.get("pool")
            if not isinstance(pool, str) or (cik is not None and not isinstance(cik, (str, int))):
                raise PoolError(f"{source}: invalid pool/CIK in assignment")
            if not names:
                registry.register(Identity(cik=cik), pool)
            first: Identity | None = None
            for name in names:
                if not isinstance(name, str):
                    raise PoolError(f"{source}: invalid issuer name {name!r}")
                identity = Identity(name, cik)
                registry.register(identity, pool)
                if first is None:
                    first = identity
                else:
                    registry.alias(name, first)
        for output in outputs:
            if not isinstance(output, str) or not re.fullmatch(
                r"labels-[0-9]{4}q[1-4]\.jsonl\.gz", output
            ):
                raise PoolError(f"{source}: invalid SEC output name {output!r}")
            registry.sec_outputs.add(output)
        return registry

    def require_sec_outputs(self, names: Iterable[str], source: str) -> None:
        """Every historical SEC output must already have recorded assignments."""
        missing = sorted(name for name in names if name not in self.sec_outputs)
        if missing:
            raise PoolError(
                f"{source}: historical SEC outputs lack recorded assignments: "
                f"{', '.join(missing)}; "
                "recover reviewed identity/pool metadata before proceeding; do not reallocate"
            )

    def record_golden(self, rows: Iterable[Mapping[str, object]]) -> None:
        """Golden identities are always dev."""
        for row in rows:
            self.register(Identity.from_document(row), Pool.DEV)

    def record_fetched(
        self, data: object, source: str, recorded: Iterable[Mapping[str, object]]
    ) -> None:
        """Verify fetched pools by document id against the recorded candidate rows."""
        rows = data.get("documents") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            raise PoolError(f"{source}: expected documents mapping")
        by_id = {doc.get("id"): doc for doc in recorded}
        for doc_id, row in rows.items():
            if doc_id not in by_id or not isinstance(row, dict):
                raise PoolError(f"{source}: {doc_id!r} lacks issuer identity metadata")
            self.record_documents([{**by_id[doc_id], "pool": row.get("pool")}])


def document_rows(
    data: object, source: str, *, optional: bool = False
) -> list[Mapping[str, object]]:
    """The ``documents`` list of an already loaded metadata mapping, validated."""
    if not isinstance(data, dict):
        raise PoolError(f"{source}: expected document metadata mapping")
    rows = data.get("documents")
    if rows is None and optional:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PoolError(f"{source}: expected documents list")
    return rows
