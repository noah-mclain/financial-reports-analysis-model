from pathlib import Path
from unittest.mock import Mock

from harness.model_mapping import evaluation_structure
from pytest import MonkeyPatch

from fra_core.normalization import load_normalization_settings
from fra_core.paths import REPO_ROOT
from fra_ingest.config import load_config
from fra_ingest.results import StructureResult


def test_cached_structure_still_calls_mapping(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    baseline = StructureResult(version="v", sha256="a" * 64, convert_version="v", settings_hash="h")
    structure = Mock(return_value=baseline)
    mapping = Mock(return_value=baseline)
    monkeypatch.setattr("harness.model_mapping.structure_pdf", structure)
    monkeypatch.setattr("harness.model_mapping.map_structure", mapping)
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "1")
    monkeypatch.setenv("FRA_PROFILE", "native")
    monkeypatch.setenv("FRA_LLM_PORT", "8123")
    evaluation_structure(tmp_path / "input.pdf", load_config(), None)
    evaluation_structure(tmp_path / "input.pdf", load_config(), None)
    assert mapping.call_count == 2
    assert structure.call_count == 2


def test_off_does_not_map(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    baseline = StructureResult(version="v", sha256="a" * 64, convert_version="v", settings_hash="h")
    mapping = Mock(side_effect=AssertionError("must not map"))
    monkeypatch.setattr("harness.model_mapping.structure_pdf", Mock(return_value=baseline))
    monkeypatch.setattr("harness.model_mapping.map_structure", mapping)
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "0")
    assert evaluation_structure(tmp_path / "input.pdf", load_config(), None) == baseline
    assert not mapping.called
    assert not load_normalization_settings({}, REPO_ROOT).enabled
