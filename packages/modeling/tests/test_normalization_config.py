"""The CPU image can parse and launch from the same stdlib-only boundary."""

import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

from fra_core.normalization import load_normalization_settings


@pytest.mark.parametrize("absolute", [False, True])
def test_shared_config_accepts_valid_toml_and_resolves_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool
) -> None:
    from fra_core.normalization_config import load_values, server_command

    real = tmp_path / "real models"
    real.mkdir()
    (real / "model.gguf").write_bytes(b"synthetic")
    (tmp_path / "models").symlink_to(real, target_is_directory=True)
    path = real / "model.gguf" if absolute else Path("models/model.gguf")
    configs = tmp_path / "configs"
    configs.mkdir()
    pin = configs / "normalization.toml"
    pin.write_text(
        f"model_path   =   '{path}'\nsha256 = '{'a' * 64}'\nsize_bytes=9\ntimeout_seconds=2\n"
    )
    values = load_values(pin, tmp_path)
    settings = load_normalization_settings({}, tmp_path)
    assert values["model_path"] == settings.model_path == real / "model.gguf"
    assert not settings.enabled
    args = server_command(
        settings.model_path, executable="/app/llama-server", host="0.0.0.0", port=8123, gpu_layers=0
    )
    assert args[0] == "/app/llama-server"
    assert args[args.index("-m") + 1] == str(real / "model.gguf")
    assert args[args.index("--alias") + 1] == str(real / "model.gguf")
    assert args[args.index("-ngl") + 1] == "0"
    assert args[args.index("-c") + 1] == "2048"
    assert args[args.index("-np") + 1] == "1"
    assert args[args.index("--cache-ram") + 1] == "0"
    execute = Mock()
    monkeypatch.setattr("os.execvp", execute)
    monkeypatch.setattr(
        "sys.argv",
        [
            "normalization_config.py",
            "--config",
            str(pin),
            "--root",
            str(tmp_path),
            "--port",
            "8123",
        ],
    )
    # Direct script execution is the Compose entrypoint; no package/Pydantic install required.
    runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "core/src/fra_core/normalization_config.py"),
        run_name="__main__",
    )
    assert execute.call_args.args == (args[0], args)


def test_off_keeps_missing_config_loud_but_needs_no_model(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"normalization\.toml"):
        load_normalization_settings({}, tmp_path)
    (tmp_path / "configs").mkdir()
    source = Path(__file__).resolve().parents[3] / "configs/normalization.toml"
    (tmp_path / "configs/normalization.toml").write_bytes(source.read_bytes())
    settings = load_normalization_settings({}, tmp_path)
    assert not settings.enabled and not settings.model_path.exists()
