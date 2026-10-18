"""The local chat tokenizer adapter, against a tiny tokenizer built offline."""

import json
from pathlib import Path

import pytest
from chat_tokenizer import load_chat_counter
from dataset_contracts import DatasetError
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast

TEMPLATE = (
    "{% for m in messages %}{{ m.role }} {{ m.content }} {% endfor %}"
    "{% if add_generation_prompt %}assistant {% endif %}"
)
VOCAB = {"[UNK]": 0, "user": 1, "net": 2, "sales": 3, "assistant": 4, "revenue": 5}


def make_tokenizer(directory: Path, template: str | None = TEMPLATE) -> Path:
    tokenizer = Tokenizer(models.WordLevel(VOCAB, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    fast = PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token="[UNK]")
    if template is not None:
        fast.chat_template = template
    fast.save_pretrained(directory)
    return directory


def test_counts_the_templated_sequence_including_the_assistant_turn(tmp_path: Path) -> None:
    count, _identity = load_chat_counter(make_tokenizer(tmp_path))
    messages = [
        {"role": "user", "content": "net sales"},
        {"role": "assistant", "content": "revenue"},
    ]
    # "user net sales assistant revenue " is five word-level tokens; a generation prompt would
    # add a sixth, so this also shows the counter asks for none.
    assert count(messages) == 5
    assert type(count(messages)) is int


def test_identity_is_stable_and_changes_with_the_template(tmp_path: Path) -> None:
    _, first = load_chat_counter(make_tokenizer(tmp_path / "a"))
    _, again = load_chat_counter(make_tokenizer(tmp_path / "b"))
    _, changed = load_chat_counter(make_tokenizer(tmp_path / "c", TEMPLATE + "!"))
    assert first == again
    assert first != changed
    assert str(tmp_path) not in first


def test_missing_template_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="chat template"):
        load_chat_counter(make_tokenizer(tmp_path, template=None))


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="missing"):
        load_chat_counter(tmp_path / "absent")


def test_missing_tokenizer_file_is_named(tmp_path: Path) -> None:
    make_tokenizer(tmp_path)
    (tmp_path / "tokenizer.json").unlink()
    with pytest.raises(DatasetError, match=r"tokenizer\.json"):
        load_chat_counter(tmp_path)


def test_identity_covers_chat_template_json(tmp_path: Path) -> None:
    make_tokenizer(tmp_path)
    _, before = load_chat_counter(tmp_path)
    (tmp_path / "chat_template.json").write_text(
        json.dumps({"chat_template": TEMPLATE}), encoding="utf-8"
    )
    _, after = load_chat_counter(tmp_path)
    assert before != after
