"""Ask a running API whether it is healthy: ``python -m fra_api.healthcheck PORT --profile P``.

Compose runs it inside the container and ``make docker-health`` runs it from the host, so the
health path and the pass rule live only in this package.
"""

from __future__ import annotations

import argparse
import http.client
import json
import urllib.request

from fra_api.app import HEALTH_PATH
from fra_core.profile import Profile


class HealthCheckError(Exception):
    """The API did not answer, or answered with something other than a healthy reply."""


def check(port: int, expect_profile: Profile, host: str = "127.0.0.1") -> dict[str, str]:
    """The health body if the API is up and reports ``expect_profile``, else HealthCheckError."""
    url = f"http://{host}:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise HealthCheckError(f"{url} is unreachable or did not return JSON: {error}") from error
    if not isinstance(body, dict):
        raise HealthCheckError(f"{url} replied with {type(body).__name__}, not a JSON object")
    if body.get("status") != "ok":
        raise HealthCheckError(f"{url} reports status {body.get('status')!r}, expected 'ok'")
    if body.get("profile") != expect_profile.value:
        raise HealthCheckError(
            f"{url} reports profile {body.get('profile')!r}, expected {expect_profile.value!r}"
        )
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("--profile", type=Profile, required=True, choices=list(Profile))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        body = check(args.port, args.profile, args.host)
    except HealthCheckError as error:
        print(f"unhealthy: {error}")
        return 1
    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
