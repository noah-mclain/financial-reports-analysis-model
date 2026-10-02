"""The run profile: where the platform is running, read from ``FRA_PROFILE`` in one place.

``native`` is the Mac (MLX, Vision OCR). ``docker`` is the container (llama.cpp, no GPU). The
set is closed and there is no default: a missing or misspelled value fails at startup, so no
service runs under a profile nobody chose.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum

PROFILE_ENV_VAR = "FRA_PROFILE"


class Profile(StrEnum):
    NATIVE = "native"
    DOCKER = "docker"


def read_profile(environ: Mapping[str, str]) -> Profile:
    """The profile named by ``FRA_PROFILE``; ``ValueError`` when unset or not a known profile."""
    allowed = ", ".join(profile.value for profile in Profile)
    if PROFILE_ENV_VAR not in environ:
        raise ValueError(f"{PROFILE_ENV_VAR} is not set; it must be one of: {allowed}")
    value = environ[PROFILE_ENV_VAR]
    try:
        return Profile(value)
    except ValueError:
        raise ValueError(
            f"{PROFILE_ENV_VAR}={value!r} is not a profile; use one of: {allowed}"
        ) from None


if __name__ == "__main__":
    print(read_profile(os.environ).value)
