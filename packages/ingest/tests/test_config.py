"""Settings for the ingest stages, read from configs/ingest.toml."""

from pathlib import Path

import pytest

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config


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
