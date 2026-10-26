import os
from pathlib import Path
from unittest.mock import Mock

from pytest import MonkeyPatch

from fra_core.normalization import load_normalization_settings
from fra_core.paths import REPO_ROOT
from fra_model.serve import main


def test_native_server_uses_pinned_absolute_path_and_port(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("FRA_PROFILE", "native")
    monkeypatch.setenv("FRA_LLM_PORT", "8123")
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "1")
    execute = Mock()
    monkeypatch.setattr("fra_model.serve.os.execvp", execute)
    main()
    config = load_normalization_settings(os.environ, REPO_ROOT)
    args = execute.call_args.args[1]
    assert args[args.index("-m") + 1] == str(config.model_path)
    assert args[args.index("--alias") + 1] == str(config.model_path)
    assert args[args.index("--port") + 1] == "8123"
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("-ngl") + 1] == "99"
    assert Path(args[args.index("-m") + 1]).is_absolute()
    assert args[args.index("-c") + 1] == "2048"
    assert args[args.index("-np") + 1] == "1"
    assert args[args.index("--cache-ram") + 1] == "0"
