"""The in-container entrypoint, run on the host against a fake agent.

`entrypoint.run` is the only code that turns an agent's final state into the
files the harness reads, and the only place that decides whether a failure was
the agent's or Meridian's. These tests exercise that decision without Docker.
"""

from __future__ import annotations

import importlib
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from meridian.adapters.entrypoint import (
    EXIT_ENTRYPOINT_FAILED,
    EXIT_OK,
    HARNESS_ERROR_FILE,
    proxy_is_unreachable,
    run,
)

pytestmark = pytest.mark.unit

FAKE_AGENT = """
import json
import os


class _Graph:
    def __init__(self, final_state=None, error=None):
        self.final_state = final_state
        self.error = error

    async def ainvoke(self, payload, config):
        if self.error is not None:
            raise self.error
        path = os.path.join(os.path.dirname(__file__), "last_payload.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return self.final_state


succeeding = _Graph(
    final_state={
        "completed": True,
        "turns": 2,
        "tool_calls": [{"name": "write_file", "ok": True}],
        "input_tokens": 7,
        "output_tokens": 3,
    }
)
exploding = _Graph(error=RuntimeError("the agent blew up"))
not_a_graph = object()
"""


@pytest.fixture(autouse=True)
def local_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx parses NO_PROXY; `[::1]` makes it reject every URL on 0.28."""
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")


@pytest.fixture
def fake_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Each test gets its own copy; drop the previous import so `__file__` (which
    # records where the payload is written) points at this test's directory.
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("fake_entrypoint_agent", None)
    importlib.invalidate_caches()
    (tmp_path / "fake_entrypoint_agent.py").write_text(FAKE_AGENT, encoding="utf-8")
    return tmp_path


def write_context(
    payload_dir: Path,
    *,
    spec: str = "langgraph:fake_entrypoint_agent:succeeding",
    model_base_url: str = "",
) -> Path:
    context_path = payload_dir / "context.json"
    context_path.write_text(
        (
            "{"
            f'"adapter_spec": "{spec}", '
            '"run_id": "run-1", '
            '"task_slug": "happy-path", '
            '"trial_index": 0, '
            '"seed": 7, '
            '"workdir": "/work", '
            '"prompt": "do the task", '
            f'"model_base_url": "{model_base_url}", '
            '"max_tokens": 1000, '
            '"deadline_unix_ms": 0'
            "}"
        ),
        encoding="utf-8",
    )
    return context_path


async def test_a_successful_run_writes_the_result(fake_agent: Path) -> None:
    context_path = write_context(fake_agent)

    assert await run(context_path) == EXIT_OK

    payload = (fake_agent / "result.json").read_text(encoding="utf-8")
    assert '"completed": true' in payload
    assert '"turns": 2' in payload
    assert (fake_agent / "transcript.json").exists()
    assert not (fake_agent / HARNESS_ERROR_FILE).exists()


async def test_the_context_is_handed_to_the_graph_unchanged(fake_agent: Path) -> None:
    context_path = write_context(fake_agent)

    assert await run(context_path) == EXIT_OK

    payload = (fake_agent / "last_payload.json").read_text(encoding="utf-8")
    assert '"run_id": "run-1"' in payload
    assert '"task_slug": "happy-path"' in payload


async def test_a_missing_target_writes_the_harness_error(fake_agent: Path) -> None:
    context_path = write_context(fake_agent, spec="langgraph:fake_entrypoint_agent:no_such_graph")

    assert await run(context_path) == EXIT_ENTRYPOINT_FAILED

    error = (fake_agent / HARNESS_ERROR_FILE).read_text(encoding="utf-8")
    assert "AdapterSpecError" in error
    assert "no_such_graph" in error
    assert not (fake_agent / "result.json").exists()


async def test_an_agent_exception_is_a_result_not_a_harness_error(fake_agent: Path) -> None:
    context_path = write_context(fake_agent, spec="langgraph:fake_entrypoint_agent:exploding")

    assert await run(context_path) == EXIT_OK

    result = (fake_agent / "result.json").read_text(encoding="utf-8")
    assert '"completed": false' in result
    assert "RuntimeError: the agent blew up" in result
    assert (fake_agent / "traceback.txt").exists()
    assert not (fake_agent / HARNESS_ERROR_FILE).exists()


async def test_a_dead_proxy_turns_an_agent_error_into_a_harness_error(fake_agent: Path) -> None:
    context_path = write_context(
        fake_agent,
        spec="langgraph:fake_entrypoint_agent:exploding",
        model_base_url=unused_local_url(),
    )

    assert await run(context_path) == EXIT_ENTRYPOINT_FAILED

    error = (fake_agent / HARNESS_ERROR_FILE).read_text(encoding="utf-8")
    assert "did not answer its health check" in error
    assert "The agent reported: RuntimeError" in error
    assert not (fake_agent / "result.json").exists()


def test_proxy_is_unreachable_for_a_dead_url() -> None:
    assert proxy_is_unreachable(unused_local_url())


def test_proxy_is_reachable_for_a_live_server() -> None:
    with local_http_server(200) as base_url:
        assert not proxy_is_unreachable(base_url)


def test_a_failing_proxy_is_unreachable() -> None:
    with local_http_server(503) as base_url:
        assert proxy_is_unreachable(base_url)


def test_an_empty_base_url_is_not_probed() -> None:
    assert not proxy_is_unreachable("")


def unused_local_url() -> str:
    """A loopback URL whose port was just released, so nothing is listening."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class _HealthHandler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def local_http_server(status: int) -> Iterator[str]:
    handler = type("HealthHandler", (_HealthHandler,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
