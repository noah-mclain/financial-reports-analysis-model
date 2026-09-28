"""Settings for the ingest stages, read from configs/ingest.toml."""

from pathlib import Path

import pytest

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, IngestConfig, find_repo_root, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ingest.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_config_matches_the_defaults() -> None:
    assert load_config() == IngestConfig()


def test_defaults_convert_balance_income_and_comprehensive_income() -> None:
    config = IngestConfig()
    assert config.enabled_types == (
        StatementType.BALANCE,
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
    )
    assert config.optional_types == (StatementType.CASH_FLOW, StatementType.EQUITY)


def test_enabling_cash_flow_is_a_config_change(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            '[statements]\nenabled = ["balance", "income", "cash_flow"]\noptional = ["equity"]\n',
        )
    )
    assert StatementType.CASH_FLOW in config.enabled_types
    assert config.optional_types == (StatementType.EQUITY,)


def test_a_type_cannot_be_both_enabled_and_optional(tmp_path: Path) -> None:
    path = write(tmp_path, '[statements]\nenabled = ["balance"]\noptional = ["balance"]\n')
    with pytest.raises(ValueError, match="both enabled and optional"):
        load_config(path)


def test_unknown_settings_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"unknown setting ocr\.mode"):
        load_config(write(tmp_path, '[ocr]\nmode = "fast"\n'))


def test_relative_artifact_root_resolves_against_the_repository(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, '[artifacts]\nroot = "var/elsewhere"\n'))
    assert config.artifact_root == REPO_ROOT / "var" / "elsewhere"


def test_at_least_one_ocr_language_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ocr_languages"):
        load_config(write(tmp_path, "[ocr]\nlanguages = []\n"))


def test_the_repository_root_can_be_set_for_installed_copies(tmp_path: Path) -> None:
    source = tmp_path / "site-packages"  # an installed copy: no configs/ next to the code
    assert find_repo_root({"FRA_ROOT": str(tmp_path / "app")}, source, tmp_path) == tmp_path / "app"
    assert find_repo_root({}, source, tmp_path / "cwd") == tmp_path / "cwd"


def test_the_source_checkout_is_the_root_when_it_has_the_config(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ingest.toml").write_text("", encoding="utf-8")
    assert find_repo_root({}, tmp_path, tmp_path / "elsewhere") == tmp_path


def test_the_config_file_can_be_named_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRA_INGEST_CONFIG", str(write(tmp_path, "[locate]\npad_pages = 2\n")))
    assert load_config().pad_pages == 2
