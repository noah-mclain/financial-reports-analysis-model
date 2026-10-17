"""Shared synthetic inputs for the SEC crosswalk package tests: a tiny tag.txt archive."""

import zipfile
from pathlib import Path

from sec_crosswalk_draft import (
    MAPPING_PATH,
    TAG_COLUMNS,
    CuratedMapping,
    TagDefinition,
    load_mapping,
    merge_vocabularies,
)

from fra_core.taxonomy import Taxonomy, load_taxonomy

HEADER = "\t".join(TAG_COLUMNS)


def tag_line(
    tag: str,
    version: str = "us-gaap/2024",
    *,
    custom: str = "0",
    abstract: str = "0",
    tlabel: str = "A label",
    doc: str = "A definition",
) -> str:
    return "\t".join([tag, version, custom, abstract, "monetary", "I", "D", tlabel, doc])


def write_archive(path: Path, lines: list[str], *, header: str = HEADER) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tag.txt", "\n".join([header, *lines]) + "\n")
        archive.writestr("pre.txt", "THIS MEMBER MUST NEVER BE READ\n")
    return path


def taxonomy() -> Taxonomy:
    return load_taxonomy()


def committed_mapping() -> CuratedMapping:
    return load_mapping(MAPPING_PATH, taxonomy())


def synthetic_vocabulary(
    mapping: CuratedMapping, versions: tuple[str, ...] = ("2023", "2024")
) -> dict[str, dict[str, TagDefinition]]:
    """Every mapped tag in each version, defined by its own first mapping phrase.

    Excluded tags are defined too, as the real vocabulary defines them.
    """
    mapped = [(row.tag, f"{row.doc_phrases[0]} (synthetic)") for row in mapping.rows] + [
        (x.tag, f"{x.tag} excluded definition") for x in mapping.excluded
    ]
    return merge_vocabularies(
        TagDefinition(
            tag=tag,
            namespace=mapping.namespace,
            tag_taxonomy_version=version,
            tlabel=f"{tag} label",
            doc=doc,
        )
        for version in versions
        for tag, doc in mapped
    )
