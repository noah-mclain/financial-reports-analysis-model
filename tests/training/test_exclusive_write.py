"""The one exclusive-write helper: all files or none, and nothing is ever overwritten."""

from pathlib import Path

import pytest
from dataset_contracts import DatasetError, text_lines
from exclusive_write import write_new


def test_writes_text_and_bytes_and_creates_directories(tmp_path: Path) -> None:
    write_new({tmp_path / "a" / "one.txt": "héllo\n", tmp_path / "a" / "two.bin": b"\x00\x01"})
    assert (tmp_path / "a" / "one.txt").read_text(encoding="utf-8") == "héllo\n"
    assert (tmp_path / "a" / "two.bin").read_bytes() == b"\x00\x01"


def test_an_existing_file_blocks_all_and_leaves_no_new_file(tmp_path: Path) -> None:
    (tmp_path / "second").write_text("keep", encoding="utf-8")
    with pytest.raises(DatasetError, match="already exists"):
        write_new({tmp_path / "first": "x", tmp_path / "second": "y"})
    assert not (tmp_path / "first").exists()
    assert (tmp_path / "second").read_text(encoding="utf-8") == "keep"


def test_a_dangling_symlink_is_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (tmp_path / "link").symlink_to(victim)
    with pytest.raises(DatasetError, match="already exists"):
        write_new({tmp_path / "first": "x", tmp_path / "link": "y"})
    assert not victim.exists() and not (tmp_path / "first").exists()


def test_any_failure_while_writing_rolls_back(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        write_new({tmp_path / "one": "ok", tmp_path / "two": 7})  # type: ignore[dict-item]
    assert list(tmp_path.iterdir()) == []


def test_lines_split_on_newline_only() -> None:
    ls, nel, ps = chr(0x2028), chr(0x85), chr(0x2029)
    text = f"a{ls}b\n\nc{nel}d{ps}e\r\n  \nlast"
    assert list(text_lines(text)) == [(1, f"a{ls}b"), (3, f"c{nel}d{ps}e\r"), (5, "last")]
