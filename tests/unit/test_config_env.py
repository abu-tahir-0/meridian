"""Configuration loading: every environment mapping and the override order."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.config import ProxyMode, load_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("variable", "value", "expected_path", "expected"),
    [
        ("MERIDIAN_N_TRIALS", "7", ("execution", "n_trials"), 7),
        ("MERIDIAN_K", "2", ("execution", "k"), 2),
        ("MERIDIAN_MAX_CONCURRENT_TRIALS", "8", ("execution", "max_concurrent_trials"), 8),
        ("MERIDIAN_PROXY_MODE", "passthrough", ("execution", "proxy_mode"), ProxyMode.PASSTHROUGH),
        ("MERIDIAN_BUDGET_CENTS", "250", ("execution", "budget_cents"), 250),
        ("MERIDIAN_TOLERANCE", "0.1", ("gate", "tolerance"), "0.1"),
        (
            "MERIDIAN_DATABASE_URL",
            "sqlite:///runs.db",
            ("store", "database_url"),
            "sqlite:///runs.db",
        ),
    ],
)
def test_each_environment_variable_maps_to_its_field(
    variable: str, value: str, expected_path: tuple[str, str], expected: object
) -> None:
    config = load_config(env={variable: value})
    section, field_name = expected_path
    assert getattr(getattr(config, section), field_name) == expected


def test_a_null_budget_clears_the_value() -> None:
    assert load_config(env={"MERIDIAN_BUDGET_CENTS": "null"}).execution.budget_cents is None


def test_an_invalid_tolerance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="is not a decimal"):
        load_config(env={"MERIDIAN_TOLERANCE": "not-a-number"})


def test_the_environment_overrides_the_file(tmp_path: Path) -> None:
    path = tmp_path / "meridian.yaml"
    path.write_text(
        "gate:\n  tolerance: '0.5'\nexecution:\n  n_trials: 9\n",
        encoding="utf-8",
    )

    config = load_config(path, env={"MERIDIAN_TOLERANCE": "0.1", "MERIDIAN_N_TRIALS": "4"})

    assert config.gate.tolerance == "0.1"
    assert config.execution.n_trials == 4


def test_explicit_overrides_win_over_the_environment() -> None:
    config = load_config(
        env={"MERIDIAN_TOLERANCE": "0.1"},
        overrides={"gate": {"tolerance": "0.25"}},
    )
    assert config.gate.tolerance == "0.25"
