"""The local chat tokenizer adapter, against a tiny tokenizer built offline."""

import json
from pathlib import Path

import chat_tokenizer
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


def test_context_constructs_once_and_explicitly_encodes_full_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_tokenizer(tmp_path)
    calls = []

    class Tokenizer:
        chat_template = TEMPLATE

        def apply_chat_template(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return [1, 2, 3] if not kwargs["add_generation_prompt"] else [1, 2]

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls.append((path, kwargs))
            return Tokenizer()

    monkeypatch.setitem(
        __import__("sys").modules,
        "transformers",
        type("Transformers", (), {"AutoTokenizer": AutoTokenizer}),
    )
    context = chat_tokenizer.open_chat_tokenizer(directory)
    assert context.encode(
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    ) == ([1, 2, 3], 2)
    assert context.count([{"role": "assistant", "content": "y"}]) == 3
    assert calls[0][1] == {"local_files_only": True, "trust_remote_code": False}
    assert calls[1][1] == {
        "tokenize": True,
        "add_generation_prompt": False,
        "return_dict": False,
    }
    assert calls[2][1] == {
        "tokenize": True,
        "add_generation_prompt": True,
        "return_dict": False,
    }
    assert calls[3][1] == {
        "tokenize": True,
        "add_generation_prompt": False,
        "return_dict": False,
    }
    assert sum(isinstance(call[0], Path) for call in calls) == 1


def test_count_and_encode_do_not_rehash_snapshot_per_row(tmp_path: Path, monkeypatch) -> None:
    directory = make_tokenizer(tmp_path)
    context = chat_tokenizer.open_chat_tokenizer(directory)
    monkeypatch.setattr(
        chat_tokenizer,
        "tokenizer_fingerprints",
        lambda _directory: pytest.fail("tokenizer was fingerprinted during row processing"),
    )
    monkeypatch.setattr(
        chat_tokenizer,
        "constructor_fingerprints",
        lambda _directory: pytest.fail("constructor inputs were fingerprinted per row"),
    )
    messages = [
        {"role": "user", "content": "net sales"},
        {"role": "assistant", "content": "revenue"},
    ]
    for _ in range(5):
        context.count(messages)
        context.encode(messages)


def test_real_tokenizer_is_constructed_once_for_repeated_counts(tmp_path: Path, monkeypatch):
    directory = make_tokenizer(tmp_path)
    from transformers import AutoTokenizer

    original = AutoTokenizer.from_pretrained
    constructions = []

    def counted(*args, **kwargs):
        constructions.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(AutoTokenizer, "from_pretrained", counted)
    context = chat_tokenizer.open_chat_tokenizer(directory)
    messages = [
        {"role": "user", "content": "net sales"},
        {"role": "assistant", "content": "revenue"},
    ]
    for _ in range(5):
        context.count(messages)
    assert constructions == [directory.resolve()]


def test_context_rejects_changed_or_different_snapshot(tmp_path: Path, monkeypatch) -> None:
    directory = make_tokenizer(tmp_path / "snapshot")
    other = make_tokenizer(tmp_path / "other")

    class Tokenizer:
        chat_template = TEMPLATE

        def apply_chat_template(self, messages, **kwargs):
            return [1, 2, 3]

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            return Tokenizer()

    monkeypatch.setitem(
        __import__("sys").modules,
        "transformers",
        type("Transformers", (), {"AutoTokenizer": AutoTokenizer}),
    )
    context = chat_tokenizer.open_chat_tokenizer(directory)
    with pytest.raises(DatasetError, match="differs"):
        context.assert_current(other)
    (directory / "tokenizer.json").write_text("changed")
    with pytest.raises(DatasetError, match="changed"):
        context.assert_current(directory)


@pytest.mark.parametrize("mutation", ["change", "add", "delete"])
def test_context_rejects_constructor_config_mutations(tmp_path, monkeypatch, mutation):
    directory = make_tokenizer(tmp_path)
    (directory / "config.json").write_text('{"model_type":"old"}')

    class Tokenizer:
        chat_template = TEMPLATE

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            return Tokenizer()

    monkeypatch.setitem(
        __import__("sys").modules,
        "transformers",
        type("Transformers", (), {"AutoTokenizer": AutoTokenizer}),
    )
    context = chat_tokenizer.open_chat_tokenizer(directory)
    config = directory / "config.json"
    if mutation == "change":
        config.write_text('{"model_type":"new"}')
    elif mutation == "add":
        (directory / "added_tokens.json").write_text("{}")
    else:
        config.unlink()
    with pytest.raises(DatasetError, match="changed"):
        context.assert_current(directory)


def test_context_instances_do_not_share_tokenizers(tmp_path: Path, monkeypatch) -> None:
    directory = make_tokenizer(tmp_path)
    instances = []

    class Tokenizer:
        chat_template = TEMPLATE

        def apply_chat_template(self, messages, **kwargs):
            return [1]

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            tokenizer = Tokenizer()
            instances.append(tokenizer)
            return tokenizer

    monkeypatch.setitem(
        __import__("sys").modules,
        "transformers",
        type("Transformers", (), {"AutoTokenizer": AutoTokenizer}),
    )
    first = chat_tokenizer.open_chat_tokenizer(directory)
    second = chat_tokenizer.open_chat_tokenizer(directory)
    assert first.tokenizer is not second.tokenizer
    assert instances == [first.tokenizer, second.tokenizer]


def test_released_context_fails_with_dataset_error(tmp_path: Path) -> None:
    context = chat_tokenizer.open_chat_tokenizer(make_tokenizer(tmp_path))
    context.release()
    messages = [{"role": "user", "content": "net sales"}]
    with pytest.raises(DatasetError, match="tokenizer context released"):
        context.count(messages)
    with pytest.raises(DatasetError, match="tokenizer context released"):
        context.encode(messages)


def test_module_import_does_not_import_transformers() -> None:
    import subprocess
    import sys

    training = str(Path(__file__).resolve().parents[2] / "training")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import chat_tokenizer; "
            "assert 'transformers' not in sys.modules",
            training,
        ],
        check=True,
    )


@pytest.mark.parametrize("folder", ["chat_templates", "additional_chat_templates"])
@pytest.mark.parametrize("mutation", ["add", "change", "delete", "rename"])
def test_context_binds_named_template_content_and_relative_names(tmp_path, folder, mutation):
    directory = make_tokenizer(tmp_path)
    templates = directory / folder
    templates.mkdir()
    named = templates / "default.jinja"
    if mutation != "add":
        named.write_text(TEMPLATE)
    context = chat_tokenizer.open_chat_tokenizer(directory)
    legacy = chat_tokenizer.tokenizer_fingerprints(directory)
    identity = context.identity
    if mutation in ("add", "change"):
        named.write_text(TEMPLATE + "revenue ")
    elif mutation == "delete":
        named.unlink()
    else:
        named.rename(templates / "other.jinja")
    assert chat_tokenizer.tokenizer_fingerprints(directory) == legacy
    assert context.identity == identity
    with pytest.raises(DatasetError, match="snapshot changed"):
        context.assert_current(directory)
    with pytest.raises(DatasetError, match="snapshot changed"):
        context.assert_current(directory)


def test_discovered_default_template_overrides_config_and_retained_context_refuses(tmp_path):
    from transformers.utils.hub import CHAT_TEMPLATE_DIR

    directory = make_tokenizer(tmp_path)
    # Put the default only in config, as in the pinned production snapshot.
    (directory / "chat_template.jinja").unlink()
    config = directory / "tokenizer_config.json"
    config.write_text(json.dumps({**json.loads(config.read_bytes()), "chat_template": TEMPLATE}))
    context = chat_tokenizer.open_chat_tokenizer(directory)
    messages = [{"role": "user", "content": "net sales"}]
    before = context.count(messages)
    templates = directory / CHAT_TEMPLATE_DIR
    templates.mkdir()
    (templates / "default.jinja").write_text(TEMPLATE + "revenue ")
    fresh = chat_tokenizer.open_chat_tokenizer(directory)
    assert fresh.count(messages) == before + 1
    assert fresh.identity == context.identity
    with pytest.raises(DatasetError, match="snapshot changed"):
        context.assert_current(directory)
