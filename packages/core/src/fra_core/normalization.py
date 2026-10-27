"""The single normalization settings boundary; serving is opt-in and local only."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fra_core.normalization_config import load_values
from fra_core.profile import Profile, read_profile


class NormalizationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    url: str
    model_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


def load_normalization_settings(
    environ: Mapping[str, str], repo_root: Path
) -> NormalizationSettings:
    flag = environ.get("FRA_NORMALIZATION_ENABLED", "0")
    if flag not in ("0", "1"):
        raise ValueError("FRA_NORMALIZATION_ENABLED must be 0 or 1")
    values = load_values(repo_root / "configs" / "normalization.toml", repo_root)
    values["enabled"] = flag == "1"
    # Disabled analysis requires neither a server nor an inference profile/port.
    values["url"] = ""
    if flag == "1":
        profile = read_profile(environ)
        raw_port = environ.get("FRA_LLM_PORT", "")
        if not raw_port.isascii() or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ValueError("FRA_LLM_PORT must be a port from 1 to 65535")
        host = "127.0.0.1" if profile is Profile.NATIVE else "llm"
        values["url"] = f"http://{host}:{int(raw_port)}"
    return NormalizationSettings.model_validate(values)
