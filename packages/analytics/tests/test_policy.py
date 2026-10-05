"""The policy boundary refuses implicit or malformed settings, and reads one flat file."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fra_analytics.policy import Policy, load_policy

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "analytics.toml"
DEFAULTS = (
    'negative_margin_denominator = "compute_and_flag"\n'
    "include_lease_liabilities = true\n"
    "day_count_basis = 365\n"
)


def policy(**overrides: object) -> Policy:
    settings: dict[str, object] = {
        "negative_margin_denominator": "compute_and_flag",
        "include_lease_liabilities": True,
        "day_count_basis": 365,
    }
    settings.update(overrides)
    return Policy(**settings)  # type: ignore[arg-type]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "analytics.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("option", ["compute_and_flag", "null"])
def test_load_policy(tmp_path: Path, option: str) -> None:
    path = write(tmp_path, DEFAULTS.replace("compute_and_flag", option))
    assert load_policy(path).negative_margin_denominator == option


def test_the_shipped_settings_are_the_blueprint_defaults() -> None:
    # D1 (provisional): a negative revenue is computed and flagged. D3: leases are in total
    # debt. D4: 365 days.
    assert load_policy(CONFIG) == policy()


@pytest.mark.parametrize(
    "text",
    [
        "",
        'unknown = "null"',
        DEFAULTS + "unknown = 1",
        DEFAULTS.replace("compute_and_flag", "compute"),
        DEFAULTS.replace('"compute_and_flag"', "true"),
        DEFAULTS.replace('"compute_and_flag"', '["null"]'),
        DEFAULTS.replace("= 365", "= 364"),
        DEFAULTS.replace("= 365", "= true"),
        DEFAULTS.replace("= true", "= 1"),
        "[analytics]\n" + DEFAULTS,
        "[metrics]\n" + DEFAULTS,
    ],
)
def test_invalid_policy_file(tmp_path: Path, text: str) -> None:
    with pytest.raises(
        ValueError, match=r"analytics\.toml|negative_margin_denominator|include_lease|day_count"
    ):
        load_policy(write(tmp_path, text))


@pytest.mark.parametrize("missing", ["negative_margin_denominator", "include_lease_liabilities"])
def test_a_setting_that_is_missing_is_an_error_naming_the_file_and_the_setting(
    tmp_path: Path, missing: str
) -> None:
    text = "".join(line for line in DEFAULTS.splitlines(True) if not line.startswith(missing))
    with pytest.raises(ValueError, match=rf"analytics\.toml.*missing \['{missing}'\]"):
        load_policy(write(tmp_path, text))


@pytest.mark.parametrize(
    ("setting", "bad"),
    [
        ("negative_margin_denominator", "compute"),
        ("negative_margin_denominator", True),
        ("negative_margin_denominator", None),
        ("negative_margin_denominator", ["null"]),
        ("include_lease_liabilities", 1),
        ("include_lease_liabilities", "true"),
        ("day_count_basis", 364),
        ("day_count_basis", True),
        ("day_count_basis", 365.0),
    ],
)
def test_invalid_direct_policy(setting: str, bad: object) -> None:
    with pytest.raises(ValueError, match=setting):
        policy(**{setting: bad})


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "absent.toml")


def test_no_default_and_frozen() -> None:
    with pytest.raises(TypeError):
        Policy()  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        policy().negative_margin_denominator = "null"  # type: ignore[misc]
