"""Parse a suite directory into frozen, validated models.

The loader collects every issue it can find rather than aborting on the first.
A suite with four broken tasks should tell you about four broken tasks in one
run; fixing them one build at a time is how people stop running the validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from meridian.models.suite import Suite, SuiteHeader
from meridian.models.task import TaskDefinition
from meridian.suites.validate import (
    SuiteValidationError,
    ValidationIssue,
    issues_from_pydantic,
)

SUITE_FILE = "suite.yaml"
TASKS_DIR = "tasks"
HARBOR_TASK_FILE = "task.toml"


def _looks_like_harbor_tasks(root: Path) -> bool:
    """Harbor keeps task.toml at the dataset root, one level down, or two."""
    return any(
        candidate.is_file()
        for pattern in (HARBOR_TASK_FILE, f"*/{HARBOR_TASK_FILE}", f"*/*/{HARBOR_TASK_FILE}")
        for candidate in root.glob(pattern)
    )


def _read_yaml(
    path: Path, relative_to: Path
) -> tuple[dict[str, Any] | None, list[ValidationIssue]]:
    display = str(path.relative_to(relative_to))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [
            ValidationIssue(
                code="malformed_yaml",
                file=display,
                message=str(exc).replace("\n", " "),
            )
        ]
    if raw is None:
        return None, [ValidationIssue(code="malformed_yaml", file=display, message="file is empty")]
    if not isinstance(raw, dict):
        return None, [
            ValidationIssue(
                code="malformed_yaml",
                file=display,
                message=f"expected a mapping at the top level, got {type(raw).__name__}",
            )
        ]
    return raw, []


def load_suite(path: str | Path) -> Suite:
    """Load and fully validate the suite rooted at `path`.

    Raises `SuiteValidationError` carrying every issue found.
    """
    root = Path(path).resolve()
    issues: list[ValidationIssue] = []

    if not root.is_dir():
        raise SuiteValidationError(
            [
                ValidationIssue(
                    code="missing_suite_directory",
                    file=str(root),
                    message="suite directory does not exist",
                )
            ]
        )

    suite_file = root / SUITE_FILE
    if not suite_file.is_file():
        message = f"no {SUITE_FILE} in {root}"
        if _looks_like_harbor_tasks(root):
            message += "; this looks like Harbor tasks — run `meridian import-harbor` first"
        raise SuiteValidationError(
            [
                ValidationIssue(
                    code="missing_suite_file",
                    file=SUITE_FILE,
                    message=message,
                )
            ]
        )

    raw_header, header_issues = _read_yaml(suite_file, root)
    issues.extend(header_issues)

    header: SuiteHeader | None = None
    if raw_header is not None:
        try:
            header = SuiteHeader.model_validate(raw_header)
        except ValidationError as exc:
            issues.extend(issues_from_pydantic(exc, file=SUITE_FILE))

    tasks_dir = root / TASKS_DIR
    task_files = sorted(tasks_dir.glob("*.yaml")) if tasks_dir.is_dir() else []
    if not task_files:
        issues.append(
            ValidationIssue(
                code="empty_suite",
                file=TASKS_DIR,
                message=f"no task files found in {tasks_dir}",
            )
        )

    tasks: list[TaskDefinition] = []
    seen_slugs: dict[str, str] = {}

    for task_file in task_files:
        display = str(task_file.relative_to(root))
        raw_task, task_issues = _read_yaml(task_file, root)
        issues.extend(task_issues)
        if raw_task is None:
            continue

        declared_slug = raw_task.get("slug") if isinstance(raw_task.get("slug"), str) else None
        try:
            task = TaskDefinition.model_validate(raw_task)
        except ValidationError as exc:
            issues.extend(issues_from_pydantic(exc, file=display, task_slug=declared_slug))
            continue

        if task.slug in seen_slugs:
            issues.append(
                ValidationIssue(
                    code="duplicate_slug",
                    file=display,
                    task_slug=task.slug,
                    field="slug",
                    message=f"slug already defined in {seen_slugs[task.slug]}",
                )
            )
            continue
        seen_slugs[task.slug] = display

        for reference in task.referenced_files():
            # Containment before existence, and both before the file is ever
            # read. `root / reference` happily resolves `../../.git/config` and
            # absolute paths, and `build_payload` later reads whatever this
            # names and ships it into the trial container. In the case Meridian
            # exists for, the task file is attacker-controlled: a pull request
            # edits it and CI runs the gate on the result.
            if not (root / reference).resolve().is_relative_to(root.resolve()):
                issues.append(
                    ValidationIssue(
                        code="input_file_outside_suite",
                        file=display,
                        task_slug=task.slug,
                        field=reference,
                        message=(
                            f"{reference} resolves outside the suite directory; a task "
                            f"may only ship files that live beside it"
                        ),
                    )
                )
                continue
            if not (root / reference).is_file():
                issues.append(
                    ValidationIssue(
                        code="missing_input_file",
                        file=display,
                        task_slug=task.slug,
                        field=reference,
                        message=f"{reference} does not exist relative to the suite directory",
                    )
                )

        tasks.append(task)

    if issues or header is None:
        raise SuiteValidationError(issues)

    return Suite(header=header, tasks=tuple(tasks), root=root)
