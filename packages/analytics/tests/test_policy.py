"""The policy boundary refuses implicit or malformed settings."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fra_analytics.policy import Policy, load_policy


@pytest.mark.parametrize("option", ["compute_and_flag", "null"])
def test_load_policy(tmp_path: Path, option: str) -> None:
    path = tmp_path / "analytics.toml"
    path.write_text(f'negative_margin_denominator = "{option}"\n')
    assert load_policy(path).negative_margin_denominator == option


def test_tracked_policy() -> None:
    path = Path(__file__).resolve().parents[3] / "configs" / "analytics.toml"
    assert load_policy(path) == Policy(negative_margin_denominator="compute_and_flag")


@pytest.mark.parametrize(
    "text",
    [
        "",
        'unknown = "null"',
        'negative_margin_denominator = "null"\nunknown = 1',
        'negative_margin_denominator = "compute"',
        "negative_margin_denominator = true",
        "negative_margin_denominator = 1",
        'negative_margin_denominator = ["null"]',
        '[analytics]\nnegative_margin_denominator = "null"',
    ],
)
def test_invalid_policy_file(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=r"negative_margin_denominator|unknown"):
        load_policy(path)


@pytest.mark.parametrize("option", ["compute", True, 1, None, ["null"]])
def test_invalid_direct_policy(option: object) -> None:
    with pytest.raises(ValueError, match="negative_margin_denominator"):
        Policy(negative_margin_denominator=option)  # type: ignore[arg-type]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "absent.toml")


def test_no_default_and_frozen() -> None:
    with pytest.raises(TypeError):
        Policy()  # type: ignore[call-arg]
    policy = Policy(negative_margin_denominator="null")
    with pytest.raises(FrozenInstanceError):
        policy.negative_margin_denominator = "compute_and_flag"  # type: ignore[misc]
