"""What the proxy container learns about a run — pure, no Docker."""

from __future__ import annotations

import pytest

from meridian.config import ProxyMode
from meridian.runtime.proxy.session import ProxyStartError, proxy_environment
from meridian.runtime.secrets import ProviderCredential

pytestmark = pytest.mark.unit

CREDENTIAL = ProviderCredential(env_var="GROQ_API_KEY", value="gsk_test_value")


def test_replay_mode_gets_no_credential() -> None:
    environment = proxy_environment(mode=ProxyMode.REPLAY, tasks=[], credential=CREDENTIAL)
    assert environment["MERIDIAN_PROXY_MODE"] == "replay"
    assert "GROQ_API_KEY" not in environment
    assert CREDENTIAL.value not in environment.values()


def test_the_stub_provider_gets_no_upstream_settings() -> None:
    environment = proxy_environment(
        mode=ProxyMode.RECORD,
        tasks=[],
        use_stub_provider=True,
        upstream_base_url="http://meridian-provider-run-1:9000",
        upstream_api="openai",
        upstream_model="llama",
        credential=CREDENTIAL,
    )
    assert environment["MERIDIAN_UPSTREAM_BASE_URL"] == "http://meridian-provider-run-1:9000"
    assert "MERIDIAN_UPSTREAM_API" not in environment
    assert "MERIDIAN_UPSTREAM_MODEL" not in environment
    assert "GROQ_API_KEY" not in environment
    assert CREDENTIAL.value not in environment.values()


def test_a_live_run_gets_exactly_one_credential() -> None:
    environment = proxy_environment(mode=ProxyMode.RECORD, tasks=[], credential=CREDENTIAL)
    assert {key: value for key, value in environment.items() if value == CREDENTIAL.value} == {
        "GROQ_API_KEY": "gsk_test_value"
    }


def test_a_live_run_without_a_credential_is_refused() -> None:
    with pytest.raises(ProxyStartError):
        proxy_environment(mode=ProxyMode.RECORD, tasks=[])


def test_the_run_budget_is_forwarded() -> None:
    environment = proxy_environment(mode=ProxyMode.REPLAY, tasks=[], run_budget_cents=250)
    assert environment["MERIDIAN_RUN_BUDGET_CENTS"] == "250"
