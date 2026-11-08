"""Cassette, budget, secret, and config rules — no daemon, no network."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from meridian.config import ProxyMode, RunMode, load_config
from meridian.runtime.proxy.budget import (
    BudgetExceeded,
    BudgetLedger,
    PriceBook,
    TrialLimits,
    cost_microcents,
    load_prices,
)
from meridian.runtime.proxy.cassette import (
    VOLATILE_REQUEST_FIELDS,
    Cassette,
    CassetteMiss,
    CassetteStore,
    request_key,
)
from meridian.runtime.proxy.server import ProxyConfig, create_app
from meridian.runtime.secrets import (
    MissingCredentialError,
    assert_absent_from,
    has_credential,
    load_credential,
    redact,
)

pytestmark = pytest.mark.unit

REQUEST = {
    "model": "claude-sonnet-5",
    "max_tokens": 128,
    "messages": [{"role": "user", "content": "hi"}],
}


def response_body(
    text: str = "ok", *, in_tokens: int = 10, out_tokens: int = 5
) -> dict[str, object]:
    return {
        "type": "message",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


# -- cassette keys -------------------------------------------------------------


def test_volatile_fields_do_not_change_the_key() -> None:
    """Leaving one of these in means every replay misses."""
    noisy = dict(REQUEST)
    for field in VOLATILE_REQUEST_FIELDS:
        noisy[field] = "varies-between-identical-calls"
    assert request_key(noisy) == request_key(REQUEST)


def test_a_real_change_does_change_the_key() -> None:
    changed = dict(REQUEST) | {"max_tokens": 129}
    assert request_key(changed) != request_key(REQUEST)


def test_key_ignores_dict_ordering() -> None:
    reordered = {k: REQUEST[k] for k in reversed(list(REQUEST))}
    assert request_key(reordered) == request_key(REQUEST)


# -- cassette tapes ------------------------------------------------------------


def test_each_trial_replays_its_own_recording() -> None:
    """The reason a cassette is a tape per trial and not a request→response map.

    A map would serve both trials the same response, every recorded run would be
    deterministic, and pass^k could only ever be 0 or 1.
    """
    cassette = Cassette("tape-task")
    key = request_key(REQUEST)
    cassette.record(0, key=key, response=response_body("first"), input_tokens=1, output_tokens=1)
    cassette.record(1, key=key, response=response_body("second"), input_tokens=1, output_tokens=1)

    assert cassette.replay(0, key)["response"]["content"][0]["text"] == "first"
    assert cassette.replay(1, key)["response"]["content"][0]["text"] == "second"


def test_replay_is_ordered_within_a_trial() -> None:
    cassette = Cassette("tape-task")
    first, second = request_key(REQUEST), request_key(dict(REQUEST) | {"max_tokens": 256})
    cassette.record(0, key=first, response=response_body("a"), input_tokens=1, output_tokens=1)
    cassette.record(0, key=second, response=response_body("b"), input_tokens=1, output_tokens=1)

    assert cassette.replay(0, first)["response"]["content"][0]["text"] == "a"
    assert cassette.replay(0, second)["response"]["content"][0]["text"] == "b"


def test_an_unrecorded_trial_misses() -> None:
    cassette = Cassette("tape-task")
    cassette.record(
        0, key=request_key(REQUEST), response=response_body(), input_tokens=1, output_tokens=1
    )
    with pytest.raises(CassetteMiss, match="no recording"):
        cassette.replay(7, request_key(REQUEST))


def test_running_off_the_end_of_the_tape_misses() -> None:
    cassette = Cassette("tape-task")
    cassette.record(
        0, key=request_key(REQUEST), response=response_body(), input_tokens=1, output_tokens=1
    )
    cassette.replay(0, request_key(REQUEST))
    with pytest.raises(CassetteMiss, match="behaviour changed"):
        cassette.replay(0, request_key(REQUEST))


def test_a_changed_request_misses_rather_than_serving_the_wrong_response() -> None:
    cassette = Cassette("tape-task")
    cassette.record(
        0, key=request_key(REQUEST), response=response_body(), input_tokens=1, output_tokens=1
    )
    with pytest.raises(CassetteMiss, match="changed"):
        cassette.replay(0, request_key(dict(REQUEST) | {"model": "other"}))


def test_cassettes_round_trip_through_disk(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    cassette = store.get("expired-coupon")
    cassette.record(
        0, key=request_key(REQUEST), response=response_body(), input_tokens=3, output_tokens=4
    )
    store.save(cassette)

    reloaded = CassetteStore(tmp_path).get("expired-coupon")
    assert reloaded.content_hash() == cassette.content_hash()
    assert reloaded.replay(0, request_key(REQUEST))["input_tokens"] == 3


def test_re_recording_a_trial_replaces_its_tape(tmp_path: Path) -> None:
    """Recording twice must not leave the first take in front of the second.

    An appended tape replays the *previous* agent's calls and never reaches the
    ones just recorded — a cassette that silently answers for code that no
    longer exists.
    """
    store = CassetteStore(tmp_path)
    first = store.get("tape-task")
    first.record(
        0, key=request_key(REQUEST), response=response_body("old"), input_tokens=1, output_tokens=1
    )
    store.save(first)

    second = CassetteStore(tmp_path).get("tape-task")
    second.record(
        0, key=request_key(REQUEST), response=response_body("new"), input_tokens=1, output_tokens=1
    )

    assert len(second.trials["0"]) == 1
    assert second.replay(0, request_key(REQUEST))["response"]["content"][0]["text"] == "new"


def test_re_recording_one_trial_leaves_the_others_alone(tmp_path: Path) -> None:
    """A run that records fewer trials must not truncate the trials it skipped."""
    store = CassetteStore(tmp_path)
    cassette = store.get("tape-task")
    for trial in (0, 1):
        cassette.record(
            trial,
            key=request_key(REQUEST),
            response=response_body(f"take-{trial}"),
            input_tokens=1,
            output_tokens=1,
        )
    store.save(cassette)

    reloaded = CassetteStore(tmp_path).get("tape-task")
    reloaded.record(
        0,
        key=request_key(REQUEST),
        response=response_body("fresh"),
        input_tokens=1,
        output_tokens=1,
    )

    assert reloaded.replay(0, request_key(REQUEST))["response"]["content"][0]["text"] == "fresh"
    assert reloaded.replay(1, request_key(REQUEST))["response"]["content"][0]["text"] == "take-1"


def test_a_second_call_within_one_recording_still_appends() -> None:
    """Replacement is per recording session, not per call."""
    cassette = Cassette("tape-task")
    first, second = request_key(REQUEST), request_key(dict(REQUEST) | {"max_tokens": 256})
    cassette.record(0, key=first, response=response_body("a"), input_tokens=1, output_tokens=1)
    cassette.record(0, key=second, response=response_body("b"), input_tokens=1, output_tokens=1)

    assert len(cassette.trials["0"]) == 2


def test_an_unknown_cassette_version_is_refused() -> None:
    with pytest.raises(ValueError, match="re-record"):
        Cassette.from_dict({"version": 99, "task": "tape-task", "trials": {}})


# -- budget --------------------------------------------------------------------


def test_costs_are_integers() -> None:
    assert isinstance(cost_microcents("claude-sonnet-5", 1_000_000, 0), int)


def test_an_unknown_model_still_costs_something() -> None:
    assert cost_microcents("some-new-model", 1_000_000, 0) > 0


def test_prices_come_from_a_data_file() -> None:
    prices = load_prices()
    assert prices.price_for("claude-sonnet-5") == (300_000_000, 1_500_000_000)
    assert prices.price_for("some-new-model") == prices.default


def test_a_new_price_needs_only_a_data_change(tmp_path: Path) -> None:
    path = tmp_path / "model_prices.toml"
    path.write_text(
        '[default]\ninput = 100\noutput = 200\n\n[models]\n"brand-new-model" = [5, 25]\n',
        encoding="utf-8",
    )
    prices = PriceBook.from_toml(path)
    assert prices.price_for("brand-new-model") == (5, 25)
    assert prices.price_for("unlisted-model") == (100, 200)


def test_a_malformed_price_entry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "model_prices.toml"
    path.write_text(
        '[default]\ninput = 100\noutput = 200\n\n[models]\n"half-a-price" = [5]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"half-a-price"):
        PriceBook.from_toml(path)


def test_an_unpriced_model_is_reported_once_per_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = BudgetLedger()
    with caplog.at_level(logging.WARNING, logger="meridian.runtime.proxy.budget"):
        ledger.charge("t", 0, model="mystery-model", input_tokens=1, output_tokens=1)
        ledger.charge("t", 0, model="mystery-model", input_tokens=1, output_tokens=1)
    assert sum("mystery-model" in record.getMessage() for record in caplog.records) == 1

    # A new run has its own ledger, so it says it again rather than staying quiet.
    with caplog.at_level(logging.WARNING, logger="meridian.runtime.proxy.budget"):
        BudgetLedger().charge("t", 0, model="mystery-model", input_tokens=1, output_tokens=1)
    assert sum("mystery-model" in record.getMessage() for record in caplog.records) == 2


def test_token_ceiling_stops_the_next_call() -> None:
    ledger = BudgetLedger(limits={"t": TrialLimits(max_tokens=100, budget_cents=1000)})
    ledger.charge("t", 0, model="claude-sonnet-5", input_tokens=60, output_tokens=50)
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.check_before("t", 0)
    assert excinfo.value.limit == "max_tokens"


def test_one_trials_spend_does_not_charge_another() -> None:
    ledger = BudgetLedger(limits={"t": TrialLimits(max_tokens=100, budget_cents=1000)})
    ledger.charge("t", 0, model="claude-sonnet-5", input_tokens=200, output_tokens=0)
    ledger.check_before("t", 1)  # must not raise


def test_run_budget_is_checked_independently() -> None:
    ledger = BudgetLedger(run_budget_cents=1)
    ledger.charge("t", 0, model="claude-opus-5", input_tokens=10_000_000, output_tokens=0)
    assert ledger.run_budget_exhausted()
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.check_before("t", 0)
    assert excinfo.value.scope == "run"


def test_reported_cost_rounds_up() -> None:
    """Reporting less than was actually spent is the one direction that matters."""
    ledger = BudgetLedger()
    ledger.usage_for("t", 0).microcents = 1
    assert ledger.run_cents == 1


# -- secrets -------------------------------------------------------------------


def test_credentials_never_render_themselves() -> None:
    credential = load_credential({"ANTHROPIC_API_KEY": "sk-ant-abcdefghijklmnop"})
    assert "sk-ant" not in repr(credential)
    assert "sk-ant" not in str(credential)


def test_redaction_removes_key_shaped_tokens() -> None:
    assert "sk-ant-abcdefghijklmnop" not in redact("failed with key sk-ant-abcdefghijklmnop here")


def test_missing_credential_explains_that_replay_needs_none() -> None:
    with pytest.raises(MissingCredentialError, match="replay does not"):
        load_credential({})
    assert not has_credential({})


def test_a_container_environment_carrying_a_key_is_rejected() -> None:
    with pytest.raises(AssertionError, match="ANTHROPIC_API_KEY"):
        assert_absent_from(["ANTHROPIC_API_KEY=sk-ant-x"], context="trial")


def test_a_key_hidden_under_another_name_is_still_caught() -> None:
    with pytest.raises(AssertionError, match="credential-shaped"):
        assert_absent_from(["INNOCENT=sk-ant-abcdefghijklmnop"], context="trial")


# -- config --------------------------------------------------------------------


def test_passthrough_rejected_in_gate_mode() -> None:
    config = load_config(overrides={"execution": {"proxy_mode": "passthrough"}})
    config.validate_for(RunMode.EXPLORATORY)  # allowed for local exploration
    with pytest.raises(ValueError, match="cannot be replayed"):
        config.validate_for(RunMode.GATE)


def test_shared_env_is_rejected_in_gate_mode() -> None:
    config = load_config(overrides={"execution": {"unsafe_shared_env": True}})
    with pytest.raises(ValueError, match="correlated failures"):
        config.validate_for(RunMode.GATE)


def test_k_cannot_exceed_n() -> None:
    with pytest.raises(ValueError, match="pass\\^k"):
        load_config(overrides={"execution": {"n_trials": 3, "k": 5}})


def test_config_hash_changes_with_the_configuration() -> None:
    base = load_config()
    other = load_config(overrides={"execution": {"n_trials": 7}})
    assert base.config_hash() != other.config_hash()
    assert base.config_hash() == load_config().config_hash()


def test_default_proxy_mode_is_replay() -> None:
    """Replay is the safe default: offline, reproducible, and needs no key."""
    assert load_config().execution.proxy_mode is ProxyMode.REPLAY


# -- the app itself ------------------------------------------------------------


async def test_replay_serves_the_recording_and_charges_for_it(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    cassette = store.get("happy-path")
    cassette.record(
        0,
        key=request_key(REQUEST),
        response=response_body("recorded"),
        input_tokens=11,
        output_tokens=7,
    )
    store.save(cassette)

    app = create_app(
        ProxyConfig(mode=ProxyMode.REPLAY, cassette_dir=tmp_path, limits={}, credential=None)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post(
            "/v1/messages",
            json=REQUEST,
            headers={"x-meridian-task": "happy-path", "x-meridian-trial": "0"},
        )
        assert response.status_code == 200
        assert response.json()["content"][0]["text"] == "recorded"

        usage = (await client.get("/v1/usage")).json()
        assert usage["trials"]["happy-path:0"]["input_tokens"] == 11


async def test_replay_fails_closed_on_a_miss(tmp_path: Path) -> None:
    """A cassette that falls through to the network guarantees nothing."""
    app = create_app(
        ProxyConfig(mode=ProxyMode.REPLAY, cassette_dir=tmp_path, limits={}, credential=None)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post(
            "/v1/messages",
            json=REQUEST,
            headers={"x-meridian-task": "never-recorded", "x-meridian-trial": "0"},
        )
    assert response.status_code == 424
    assert response.json()["error"]["type"] == "cassette_miss"


async def test_budget_exhaustion_returns_a_structured_429(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path)
    cassette = store.get("happy-path")
    for _ in range(3):
        cassette.record(
            0, key=request_key(REQUEST), response=response_body(), input_tokens=40, output_tokens=40
        )
    store.save(cassette)

    app = create_app(
        ProxyConfig(
            mode=ProxyMode.REPLAY,
            cassette_dir=tmp_path,
            limits={"happy-path": TrialLimits(max_tokens=100, budget_cents=1000)},
        )
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"x-meridian-task": "happy-path", "x-meridian-trial": "0"}
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        assert (await client.post("/v1/messages", json=REQUEST, headers=headers)).status_code == 200
        assert (await client.post("/v1/messages", json=REQUEST, headers=headers)).status_code == 200
        exhausted = await client.post("/v1/messages", json=REQUEST, headers=headers)

    assert exhausted.status_code == 429
    body = exhausted.json()
    assert body["error"]["type"] == "budget_exceeded"
    assert body["error"]["limit"] == "max_tokens"


async def test_recording_writes_a_cassette(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from meridian.runtime.proxy import server as server_module

    async def fake_upstream(
        state: object, body: dict[str, object], **_: object
    ) -> dict[str, object]:
        return response_body("live", in_tokens=4, out_tokens=6)

    monkeypatch.setattr(server_module, "_call_upstream", fake_upstream)
    app = create_app(ProxyConfig(mode=ProxyMode.RECORD, cassette_dir=tmp_path, limits={}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post(
            "/v1/messages",
            json=REQUEST,
            headers={"x-meridian-task": "happy-path", "x-meridian-trial": "2"},
        )
    assert response.status_code == 200

    saved = json.loads((tmp_path / "happy-path.json").read_text())
    assert saved["trials"]["2"][0]["input_tokens"] == 4
    assert saved["trials"]["2"][0]["request_hash"] == request_key(REQUEST)
