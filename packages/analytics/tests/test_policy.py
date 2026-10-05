"""The policy: D3 and D4 are settings, read from configs/analytics.toml and nowhere else."""

from pathlib import Path

import pytest

from fra_analytics.policy import Policy, load_policy

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "analytics.toml"


def test_the_shipped_settings_are_the_blueprint_defaults() -> None:
    # D3: leases are in total debt. D4: 365 days.
    assert load_policy(CONFIG) == Policy(include_lease_liabilities=True, day_count_basis=365)


def test_a_setting_that_is_missing_is_an_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "analytics.toml"
    path.write_text("[metrics]\ninclude_lease_liabilities = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"analytics\.toml.*day_count_basis"):
        load_policy(path)


def test_an_unknown_setting_is_an_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "analytics.toml"
    path.write_text(
        "[metrics]\ninclude_lease_liabilities = true\nday_count_basis = 365\nrounding = 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"unknown setting metrics\.rounding"):
        load_policy(path)


def test_a_day_count_basis_other_than_360_or_365_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "analytics.toml"
    path.write_text(
        "[metrics]\ninclude_lease_liabilities = true\nday_count_basis = 364\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="day_count_basis"):
        load_policy(path)
