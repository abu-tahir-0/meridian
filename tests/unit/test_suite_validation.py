"""Every validation rule in HANDOFF §7.1 has a fixture that trips exactly it.

The tests assert the *error code*, not merely that something raised. A test that
only asserts `pytest.raises(SuiteValidationError)` passes when the loader rejects
a suite for entirely the wrong reason, which is how a validator quietly stops
enforcing the rule you care about.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.suites.loader import load_suite
from meridian.suites.validate import SuiteValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invalid-suites"
REAL_SUITE = Path(__file__).resolve().parents[2] / "suites" / "checkout-agent"

# fixture directory -> the rule code it must trip
RULES = [
    ("no-outcome-assertion", "no_outcome_assertion"),
    ("unpinned-snapshot", "unpinned_snapshot"),
    ("provenance-required", "provenance_required"),
    ("invalid-slug", "invalid_slug"),
    ("duplicate-slug", "duplicate_slug"),
    ("missing-input-file", "missing_input_file"),
    ("timeout-out-of-range", "timeout_out_of_range"),
    ("extra-fields", "extra_fields"),
    ("unknown-assertion-kind", "unknown_assertion_kind"),
]


@pytest.mark.unit
def test_control_fixture_loads() -> None:
    """The control suite differs from every invalid fixture by exactly one break."""
    suite = load_suite(FIXTURES / "_valid-control")
    assert suite.slug == "fixture-suite"
    assert len(suite.tasks) == 1


@pytest.mark.unit
@pytest.mark.parametrize(("directory", "code"), RULES, ids=[r[0] for r in RULES])
def test_rule_is_enforced(directory: str, code: str) -> None:
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(FIXTURES / directory)
    assert code in excinfo.value.codes(), (
        f"{directory} was rejected, but for {excinfo.value.codes()} rather than {code!r}"
    )


@pytest.mark.unit
def test_issues_name_the_file_and_the_task() -> None:
    """A rejection a developer cannot act on is barely better than a silent one."""
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(FIXTURES / "unpinned-snapshot")
    issue = next(i for i in excinfo.value.issues if i.code == "unpinned_snapshot")
    assert issue.file.endswith(".yaml")
    assert issue.task_slug == "valid-task"
    assert "snapshot" in (issue.field or "")


@pytest.mark.unit
def test_every_issue_is_reported_not_just_the_first() -> None:
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(FIXTURES / "duplicate-slug")
    assert len(excinfo.value.issues) >= 1
    assert all(i.message for i in excinfo.value.issues)


@pytest.mark.unit
def test_missing_suite_file_is_its_own_code(tmp_path: Path) -> None:
    (tmp_path / "tasks").mkdir()
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(tmp_path)
    assert excinfo.value.codes() == ["missing_suite_file"]


@pytest.mark.unit
def test_a_harbor_dataset_suggests_importing_it_first(tmp_path: Path) -> None:
    """Pointing `run` at a Harbor dataset should name the command that converts it."""
    task = tmp_path / "some-harbor-task"
    task.mkdir()
    (task / "task.toml").write_text("", encoding="utf-8")
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(tmp_path)
    assert excinfo.value.codes() == ["missing_suite_file"]
    assert "import-harbor" in str(excinfo.value)


@pytest.mark.unit
def test_a_plain_directory_gets_no_harbor_hint(tmp_path: Path) -> None:
    (tmp_path / "tasks").mkdir()
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(tmp_path)
    assert "import-harbor" not in str(excinfo.value)


@pytest.mark.unit
def test_malformed_yaml_is_reported_as_such(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "_valid-control", tmp_path / "suite")
    (tmp_path / "suite" / "tasks" / "broken.yaml").write_text("slug: [unclosed\n")
    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(tmp_path / "suite")
    assert "malformed_yaml" in excinfo.value.codes()


@pytest.mark.unit
def test_reference_suite_loads_and_separates_probes() -> None:
    suite = load_suite(REAL_SUITE)
    assert suite.slug == "checkout-agent"
    assert {t.slug for t in suite.scored_tasks} == {
        "happy-path",
        "expired-coupon",
        "stale-coupon",
        "expired-on-bulk-order",
        "expiry-boundary",
        "unknown-coupon",
        "missing-field",
    }
    # The contamination pair measures Meridian, not the agent, and must never
    # contribute to a reported score.
    assert {t.slug for t in suite.excluded_tasks} == {
        "contamination-writer",
        "contamination-probe",
    }


@pytest.mark.unit
def test_content_hash_is_stable_across_loads() -> None:
    first = load_suite(REAL_SUITE).content_hash()
    second = load_suite(REAL_SUITE).content_hash()
    assert first == second
    assert first.startswith("sha256:")


@pytest.mark.unit
def test_content_hash_changes_when_a_task_changes(tmp_path: Path) -> None:
    copied = tmp_path / "checkout-agent"
    shutil.copytree(REAL_SUITE, copied)
    before = load_suite(copied).content_hash()

    task_file = copied / "tasks" / "happy-path.yaml"
    task_file.write_text(task_file.read_text().replace("expected: 4500", "expected: 4501"))

    assert load_suite(copied).content_hash() != before


@pytest.mark.unit
def test_definitions_are_frozen() -> None:
    task = load_suite(REAL_SUITE).task("happy-path")
    with pytest.raises(ValidationError):
        task.slug = "mutated"  # type: ignore[misc]
