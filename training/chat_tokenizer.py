"""Token counts of chat-templated training sequences from a tokenizer on local disk."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from dataset_contracts import DatasetError, canonical_json
from transformers import AutoTokenizer

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


def load_chat_counter(
    tokenizer_dir: Path,
) -> tuple[Callable[[Sequence[Mapping[str, str]]], int], str]:
    """A counter for the chat-templated sequence, and the tokenizer's identity for the manifest.

    The directory is read with no network. The identity hashes the tokenizer files' contents,
    never their location, so the same files give the same identity anywhere.
    """
    if not tokenizer_dir.is_dir():
        raise DatasetError(f"tokenizer directory is missing: {tokenizer_dir}")
    missing = [name for name in REQUIRED_FILES if not (tokenizer_dir / name).is_file()]
    if missing:
        raise DatasetError(f"{tokenizer_dir}: tokenizer files missing: {', '.join(missing)}")
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
    if not tokenizer.chat_template:
        raise DatasetError(f"{tokenizer_dir}: tokenizer carries no chat template")

    def count(messages: Sequence[Mapping[str, str]]) -> int:
        # The training sequence ends with the assistant turn, so no generation prompt is added.
        ids = tokenizer.apply_chat_template(
            [dict(m) for m in messages],
            tokenize=True,
            add_generation_prompt=False,
            return_dict=False,
        )
        return len(ids)

    files = {
        name: hashlib.sha256((tokenizer_dir / name).read_bytes()).hexdigest()
        for name in IDENTITY_FILES
        if (tokenizer_dir / name).is_file()
    }
    return count, hashlib.sha256(canonical_json(files).encode("utf-8")).hexdigest()
