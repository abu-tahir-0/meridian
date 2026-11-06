"""JSON assertions over produced files.

Comparison is **structural, after parsing**. Key order and whitespace must not
matter: an agent that emits the right invoice with its keys in a different order
has produced the right invoice, and failing it would be grading formatting rather
than outcome.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonpath_ng.parser import parse as parse_jsonpath

from meridian.grading.graders.base import GraderError, failed, passed, resolve
from meridian.models.run import AssertionResult
from meridian.models.task import JsonPathEquals, JsonPathMatches

MAX_RENDER = 200


def _load(state_dir: Path, path: str, workdir: str) -> Any:
    target = resolve(state_dir, path, workdir)
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # The agent wrote something, and it is not JSON. That is the agent's
        # failure to report, not a broken assertion.
        raise _AgentWroteInvalidJson(f"{path} is not valid JSON: {exc}") from exc


class _AgentWroteInvalidJson(Exception):
    pass


def _select(document: Any, json_path: str) -> list[Any]:
    try:
        expression = parse_jsonpath(json_path)
    except Exception as exc:
        # A malformed JSONPath is a broken assertion, and must never look like an
        # agent failure.
        raise GraderError(f"invalid json_path {json_path!r}: {exc}") from exc
    return [match.value for match in expression.find(document)]


def _render(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= MAX_RENDER else text[:MAX_RENDER] + "…"


def grade_json_path_equals(
    assertion: JsonPathEquals, state_dir: Path, workdir: str
) -> AssertionResult:
    try:
        document = _load(state_dir, assertion.path, workdir)
    except _AgentWroteInvalidJson as exc:
        return failed(assertion.kind, str(exc))
    if document is None:
        return failed(assertion.kind, f"{assertion.path} was never written")

    found = _select(document, assertion.json_path)
    if not found:
        return failed(
            assertion.kind,
            f"{assertion.path} has nothing at {assertion.json_path}; "
            f"the document is {_render(document)}",
        )
    actual = found[0]
    if actual == assertion.expected:
        return passed(assertion.kind, f"{assertion.json_path} == {_render(assertion.expected)}")
    return failed(
        assertion.kind,
        f"{assertion.path} {assertion.json_path} is {_render(actual)}, "
        f"expected {_render(assertion.expected)}",
    )


def grade_json_path_matches(
    assertion: JsonPathMatches, state_dir: Path, workdir: str
) -> AssertionResult:
    try:
        document = _load(state_dir, assertion.path, workdir)
    except _AgentWroteInvalidJson as exc:
        return failed(assertion.kind, str(exc))
    if document is None:
        return failed(assertion.kind, f"{assertion.path} was never written")

    try:
        pattern = re.compile(assertion.pattern)
    except re.error as exc:
        raise GraderError(f"invalid pattern {assertion.pattern!r}: {exc}") from exc

    found = _select(document, assertion.json_path)
    if not found:
        return failed(
            assertion.kind,
            f"{assertion.path} has nothing at {assertion.json_path}; "
            f"the document is {_render(document)}",
        )
    actual = found[0]
    text = actual if isinstance(actual, str) else json.dumps(actual, sort_keys=True, default=str)
    if pattern.search(text):
        return passed(assertion.kind, f"{assertion.json_path} matches /{assertion.pattern}/")
    return failed(
        assertion.kind,
        f"{assertion.path} {assertion.json_path} is {_render(actual)}, "
        f"which does not match /{assertion.pattern}/",
    )
