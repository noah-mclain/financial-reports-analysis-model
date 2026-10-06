"""Settings for the ingest stages, read from configs/ingest.toml."""

from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_convert_defaults() -> None:
    config = IngestConfig()
    assert config.device == "mps"
    assert config.convert_ocr == "ocrmac"
    assert config.images_scale == 2.0
    assert config.batch_size == 2
    assert config.do_cell_matching is True
    assert config.document_timeout_s == 600.0
    assert config.child_timeout_s == 900.0
    assert config.memory_budget_gb == 3.5


def test_convert_settings_load(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            '[convert]\ndevice = "cpu"\nocr_engine = "none"\nimages_scale = 1.5\n'
            "child_timeout_s = 60\n",
        )
    )
    assert config.device == "cpu"
    assert config.convert_ocr == "none"
    assert config.images_scale == 1.5
    assert config.child_timeout_s == 60.0


def test_an_unknown_convert_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"unknown setting convert\.dpi"):
        load_config(write(tmp_path, "[convert]\ndpi = 2\n"))


def test_an_unknown_device_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, '[convert]\ndevice = "cuda"\n'))


def test_ocr_scale_defaults_to_doclings_own(tmp_path: Path) -> None:
    assert IngestConfig().ocr_scale == 3.0
    assert load_config(write(tmp_path, "[convert]\nocr_scale = 4.0\n")).ocr_scale == 4.0


def test_structure_confidence_setting(tmp_path: Path) -> None:
    assert IngestConfig().min_confidence == 0.5
    assert load_config(write(tmp_path, "[structure]\nmin_confidence = 0.7\n")).min_confidence == 0.7


@pytest.mark.parametrize("engine", ["ocrmac", "tesseract", "none"])
def test_every_ocr_engine_name_is_accepted(tmp_path: Path, engine: str) -> None:
    config = load_config(write(tmp_path, f'[convert]\nocr_engine = "{engine}"\n'))
    assert config.convert_ocr == engine


def test_an_unknown_ocr_engine_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="convert_ocr"):
        load_config(write(tmp_path, '[convert]\nocr_engine = "paddle"\n'))


def test_an_ocr_engine_argument_overrides_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[convert]\nocr_engine = "ocrmac"\n')
    assert load_config(path, ocr_engine="tesseract").convert_ocr == "tesseract"


def test_the_ocr_engine_can_be_named_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, '[convert]\nocr_engine = "ocrmac"\n')
    monkeypatch.setenv("FRA_OCR_ENGINE", "tesseract")
    assert load_config(path).convert_ocr == "tesseract"
    assert load_config(path, ocr_engine="none").convert_ocr == "none"


def test_an_unknown_ocr_engine_in_the_environment_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRA_OCR_ENGINE", "paddle")
    with pytest.raises(ValidationError, match="convert_ocr"):
        load_config(write(tmp_path, ""))


def test_tesseract_defaults_are_the_bake_off_choice(tmp_path: Path) -> None:
    config = IngestConfig()
    assert (config.tesseract_psm, config.tesseract_arabic_language) == (3, "ara+eng")
    path = write(tmp_path, '[tesseract]\npsm = 6\narabic_language = "ara"\n')
    config = load_config(path)
    assert (config.tesseract_psm, config.tesseract_arabic_language) == (6, "ara")


def test_an_arabic_language_string_tesseract_does_not_know_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="tesseract_arabic_language"):
        load_config(write(tmp_path, '[tesseract]\narabic_language = "fra"\n'))


def test_shipped_docker_profile_resolves_cpu_and_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRA_PROFILE", "docker")
    monkeypatch.delenv("FRA_OCR_ENGINE", raising=False)
    config = load_config()
    assert (config.device, config.convert_ocr) == ("cpu", "tesseract")
    assert load_config(ocr_engine="none").convert_ocr == "none"


def test_native_tesseract_keeps_native_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRA_PROFILE", "native")
    assert load_config(ocr_engine="tesseract").device == "mps"


def test_unknown_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRA_PROFILE", "typo")
    with pytest.raises(ValueError, match="FRA_PROFILE"):
        load_config()
