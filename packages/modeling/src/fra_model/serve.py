"""Start native llama-server using the same pinned settings as the worker."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fra_core.normalization import load_normalization_settings
from fra_core.normalization_config import server_command
from fra_core.paths import REPO_ROOT
from fra_core.profile import Profile, read_profile


def main() -> None:
    settings = load_normalization_settings(os.environ, REPO_ROOT)
    if not settings.enabled:
        raise ValueError("FRA_NORMALIZATION_ENABLED=1 is required to serve")
    profile = read_profile(os.environ)
    port = urlsplit(settings.url).port
    assert port is not None
    args = server_command(
        settings.model_path,
        executable="llama-server",
        host="127.0.0.1",
        port=port,
        gpu_layers=99 if profile is Profile.NATIVE else 0,
    )
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
