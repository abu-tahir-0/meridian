"""The terminal agent's one tool: run a shell command.

Standard library only, so it can be tested on the host without LangGraph.

The command runs inside the trial container, which is already the sandbox: a
read-only root filesystem, an unprivileged user, no capabilities and no network.
This module adds only what any shell tool needs -- a time limit and bounded output.
"""

from __future__ import annotations

import math
import os
import subprocess
from typing import Any

#: Defaults. A trial's context can change each limit per task; the environment
#: can change it for every task in a run.
MAX_TURNS = 30
REQUEST_TIMEOUT_SECONDS = 180.0
MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300

#: context key -> (environment variable, default, cast)
_LIMITS: dict[str, tuple[str, int | float, type]] = {
    "max_turns": ("MERIDIAN_AGENT_MAX_TURNS", MAX_TURNS, int),
    "request_timeout_seconds": (
        "MERIDIAN_AGENT_REQUEST_TIMEOUT",
        REQUEST_TIMEOUT_SECONDS,
        float,
    ),
    "max_output_chars": ("MERIDIAN_AGENT_MAX_OUTPUT_CHARS", MAX_OUTPUT_CHARS, int),
    "default_command_timeout_seconds": (
        "MERIDIAN_AGENT_DEFAULT_COMMAND_TIMEOUT",
        DEFAULT_TIMEOUT_SECONDS,
        int,
    ),
    "max_command_timeout_seconds": (
        "MERIDIAN_AGENT_MAX_COMMAND_TIMEOUT",
        MAX_TIMEOUT_SECONDS,
        int,
    ),
}


def _positive(raw: object, cast: type) -> int | float | None:
    if raw is None:
        return None
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def resolve_limit(
    context: dict[str, Any],
    key: str,
    env: str,
    default: int | float,
    cast: type = int,
) -> Any:
    """A positive limit from the trial context, else the environment, else the default.

    A bad value falls through to the next source, not to a crash: a typo in an
    optional knob must not turn a trial into a harness error.
    """
    value = _positive(context.get(key), cast)
    if value is not None:
        return value
    value = _positive(os.environ.get(env), cast)
    if value is not None:
        return value
    return default


def agent_limits(context: dict[str, Any]) -> dict[str, Any]:
    """Every per-trial limit, resolved once before the graph starts."""
    return {
        key: resolve_limit(context, key, env, default, cast)
        for key, (env, default, cast) in _LIMITS.items()
    }


def tool_schemas(default_timeout: int, max_timeout: int) -> list[dict[str, Any]]:
    """The bash tool's schema, naming the limits this trial actually uses."""
    return [
        {
            "name": "bash",
            "description": (
                "Run a shell command in the working directory. Returns the combined "
                "stdout/stderr and the exit code. Commands that run longer than the "
                "timeout are killed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run."},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": f"Time limit, at most {max_timeout} (default "
                        f"{default_timeout}).",
                    },
                },
                "required": ["command"],
            },
        }
    ]


TOOL_SCHEMAS = tool_schemas(DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of long output; the middle is rarely the point."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


def run_bash(
    command: str,
    *,
    workdir: str,
    timeout_seconds: int | None = None,
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Run `command` with bash in `workdir`. Never raises; failures are results."""
    timeout = default_timeout_seconds if timeout_seconds is None else timeout_seconds
    timeout = max(1, min(int(timeout), max_timeout_seconds))
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"") + (exc.stderr or b"")
        output = partial.decode("utf-8", errors="replace")
        return {
            "exit_code": None,
            "output": truncate(output, limit=max_output_chars),
            "error": f"timed out after {timeout}s",
        }
    except OSError as exc:
        return {"exit_code": None, "output": "", "error": f"{type(exc).__name__}: {exc}"}
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    return {
        "exit_code": completed.returncode,
        "output": truncate(output, limit=max_output_chars),
        "error": None,
    }


def format_result(result: dict[str, Any]) -> str:
    """The text the model sees for one command."""
    lines = []
    if result["error"]:
        lines.append(f"[{result['error']}]")
    elif result["exit_code"] != 0:
        lines.append(f"[exit code {result['exit_code']}]")
    if result["output"]:
        lines.append(result["output"])
    return "\n".join(lines) if lines else "[no output]"
