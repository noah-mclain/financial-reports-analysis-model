"""Token counts of chat-templated training sequences from a tokenizer on local disk."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from dataset_contracts import DatasetError, canonical_json

REQUIRED_FILES = ("tokenizer.json", "tokenizer_config.json")
# Every file that can change how text becomes tokens; those present are fingerprinted.
IDENTITY_FILES = (
    *REQUIRED_FILES,
    "chat_template.jinja",
    "chat_template.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
)
# AutoTokenizer's local constructor also consults the model config. Keep this set
# separate from the historical identity so existing dataset and model pins stay exact.
CONSTRUCTOR_FILES = ("config.json", *IDENTITY_FILES)


def tokenizer_fingerprints(tokenizer_dir: Path) -> dict[str, str]:
    """Fingerprint the local tokenizer inputs without constructing a tokenizer."""
    if not tokenizer_dir.is_dir():
        raise DatasetError(f"tokenizer directory is missing: {tokenizer_dir}")
    missing = [name for name in REQUIRED_FILES if not (tokenizer_dir / name).is_file()]
    if missing:
        raise DatasetError(f"{tokenizer_dir}: tokenizer files missing: {', '.join(missing)}")
    return {
        name: hashlib.sha256((tokenizer_dir / name).read_bytes()).hexdigest()
        for name in IDENTITY_FILES
        if (tokenizer_dir / name).is_file()
    }


def constructor_fingerprints(tokenizer_dir: Path) -> dict[str, str]:
    """Fingerprint every pinned local file AutoTokenizer may consult at construction."""
    if not tokenizer_dir.is_dir():
        raise DatasetError(f"tokenizer directory is missing: {tokenizer_dir}")
    # Named templates override tokenizer_config.json, including a named default.
    # Retain relative names: renaming a template changes template selection.
    names = [*CONSTRUCTOR_FILES]
    for folder in ("chat_templates", "additional_chat_templates"):
        names.extend(
            path.relative_to(tokenizer_dir).as_posix()
            for path in sorted((tokenizer_dir / folder).glob("*.jinja"))
        )
    return {
        name: hashlib.sha256((tokenizer_dir / name).read_bytes()).hexdigest()
        for name in names
        if (tokenizer_dir / name).is_file()
    }


class ChatTokenizerContext:
    """One tokenizer bound to one exact local snapshot for a verification operation."""

    def __init__(self, tokenizer_dir: Path) -> None:
        self.fingerprints = tokenizer_fingerprints(tokenizer_dir)
        self.constructor_fingerprints = constructor_fingerprints(tokenizer_dir)
        self.directory = tokenizer_dir.resolve(strict=True)
        # Keep construction lazy so controller imports never load Transformers or Torch.
        from transformers import AutoTokenizer

        self.tokenizer: Any = AutoTokenizer.from_pretrained(
            self.directory, local_files_only=True, trust_remote_code=False
        )
        self.assert_current(tokenizer_dir)
        if not self.tokenizer.chat_template:
            raise DatasetError(f"{self.directory}: tokenizer carries no chat template")

    @property
    def identity(self) -> str:
        return hashlib.sha256(canonical_json(self.fingerprints).encode("utf-8")).hexdigest()

    def assert_current(self, tokenizer_dir: Path) -> None:
        if tokenizer_dir.resolve(strict=True) != self.directory:
            raise DatasetError("tokenizer context directory differs from requested snapshot")
        if (
            tokenizer_fingerprints(self.directory) != self.fingerprints
            or constructor_fingerprints(self.directory) != self.constructor_fingerprints
        ):
            raise DatasetError(f"{self.directory}: tokenizer snapshot changed during verification")

    def count(self, messages: Sequence[Mapping[str, str]]) -> int:
        if self.tokenizer is None:
            raise DatasetError("tokenizer context released")
        ids = self.tokenizer.apply_chat_template(
            [dict(message) for message in messages],
            tokenize=True,
            add_generation_prompt=False,
            return_dict=False,
        )
        return len(ids)

    def encode(self, messages: Sequence[Mapping[str, str]]) -> tuple[list[int], int]:
        if self.tokenizer is None:
            raise DatasetError("tokenizer context released")
        full = self.tokenizer.apply_chat_template(
            [dict(message) for message in messages],
            tokenize=True,
            add_generation_prompt=False,
            return_dict=False,
        )
        prefix = self.tokenizer.apply_chat_template(
            [dict(message) for message in messages[:-1]],
            tokenize=True,
            add_generation_prompt=True,
            return_dict=False,
        )
        return list(full), len(prefix)

    def release(self) -> None:
        self.tokenizer = None


def open_chat_tokenizer(tokenizer_dir: Path) -> ChatTokenizerContext:
    return ChatTokenizerContext(tokenizer_dir)


def load_chat_counter(
    tokenizer_dir: Path,
) -> tuple[Callable[[Sequence[Mapping[str, str]]], int], str]:
    """A counter for the chat-templated sequence and its local tokenizer identity."""
    context = open_chat_tokenizer(tokenizer_dir)
    return context.count, context.identity
