"""Token and cost ceilings, enforced where the spend happens.

The ledger lives in the proxy rather than in the agent because a limit the agent
enforces on itself is not a limit. The trial container never sees these numbers
and cannot raise them.

Costs are integers throughout — micro-cents internally, whole cents at the
boundary. Money in a float is a rounding argument waiting to happen, and any
value that reaches a hashed structure must not be a float at all.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

MICROCENTS_PER_CENT = 1_000_000

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceBook:
    """What one call costs, read from `model_prices.toml`.

    Pairs are (input, output) micro-cents per million tokens. A model the book
    does not list is charged the default price, and the ledger reports it.
    """

    default: tuple[int, int]
    models: dict[str, tuple[int, int]]

    @classmethod
    def from_toml(cls, path: Path) -> PriceBook:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        return _parse_prices(document, str(path))

    def price_for(self, model: str) -> tuple[int, int]:
        return self.models.get(model, self.default)


def _amount(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer number of micro-cents")
    return value


def _price_pair(value: object, where: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{where} must be [input, output] micro-cents per million tokens")
    return _amount(value[0], f"{where} input"), _amount(value[1], f"{where} output")


def _parse_prices(document: object, source: str) -> PriceBook:
    if not isinstance(document, dict):
        raise ValueError(f"{source}: expected a table with [default] and [models]")
    default_raw = document.get("default")
    if not isinstance(default_raw, dict):
        raise ValueError(f"{source}: [default] must be a table with input and output")
    default = (
        _amount(default_raw.get("input"), f"{source}: default.input"),
        _amount(default_raw.get("output"), f"{source}: default.output"),
    )
    models_raw = document.get("models", {})
    if not isinstance(models_raw, dict):
        raise ValueError(f"{source}: [models] must be a table of model = [input, output]")
    models = {
        name: _price_pair(value, f"{source}: models.{name}") for name, value in models_raw.items()
    }
    return PriceBook(default=default, models=models)


@lru_cache(maxsize=1)
def load_prices() -> PriceBook:
    """The packaged price list, parsed once per process."""
    resource = files("meridian.runtime.proxy").joinpath("model_prices.toml")
    document = tomllib.loads(resource.read_text(encoding="utf-8"))
    return _parse_prices(document, "model_prices.toml")


def cost_microcents(model: str, input_tokens: int, output_tokens: int) -> int:
    """Integer micro-cents for one call."""
    per_in, per_out = load_prices().price_for(model)
    return (input_tokens * per_in + output_tokens * per_out) // 1_000_000


class BudgetExceeded(Exception):
    """A ceiling was crossed.

    Surfaced to the agent as a 429 with a structured body. The agent sees a
    failed call, so the trial is a `fail` — the agent did not achieve the
    required state within its budget — and never a `harness_error`.
    """

    def __init__(self, scope: str, limit: str, used: int, allowed: int) -> None:
        super().__init__(
            f"{scope} exceeded its {limit} budget: {used} used against a limit of {allowed}"
        )
        self.scope = scope
        self.limit = limit
        self.used = used
        self.allowed = allowed

    def as_payload(self) -> dict[str, object]:
        return {
            "type": "error",
            "error": {
                "type": "budget_exceeded",
                "scope": self.scope,
                "limit": self.limit,
                "used": self.used,
                "allowed": self.allowed,
                "message": str(self),
            },
        }


@dataclass
class TrialLimits:
    """Per-trial ceilings, taken from the task definition, not from the agent."""

    max_tokens: int
    budget_cents: int


@dataclass
class TrialUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    microcents: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class UpstreamHealth:
    """How a trial's calls to the real provider went, after the proxy's retries.

    Lets the harness tell "the agent failed" from "the provider failed under the
    agent": a trial whose last upstream call failed did not get a fair attempt.
    """

    failures: int = 0
    last_failed: bool = False
    # "provider" (rate limit, 5xx, transport) or "proxy" (a bug on our side).
    last_kind: str | None = None
    # Time the proxy spent backing off before retrying the provider, in total.
    retries: int = 0
    waited_ms: int = 0
    # The provider's own explanation for the most recent retry (e.g. which rate limit).
    last_retry_reason: str = ""


@dataclass
class BudgetLedger:
    """Usage for a whole run, keyed by trial."""

    limits: dict[str, TrialLimits] = field(default_factory=dict)
    run_budget_cents: int | None = None
    usage: dict[str, TrialUsage] = field(default_factory=dict)
    upstream: dict[str, UpstreamHealth] = field(default_factory=dict)
    # Which models were actually called. Recorded because the manifest must pin
    # the model, and the harness never chooses it -- the agent does.
    models: set[str] = field(default_factory=set)
    # Models charged the default price this run, so each is reported once per
    # run rather than once per call. A module global would silence the next run.
    _unpriced: set[str] = field(default_factory=set, repr=False)

    def _key(self, task_slug: str, trial_index: int) -> str:
        return f"{task_slug}:{trial_index}"

    def usage_for(self, task_slug: str, trial_index: int) -> TrialUsage:
        return self.usage.setdefault(self._key(task_slug, trial_index), TrialUsage())

    def record_upstream_failure(self, task_slug: str, trial_index: int, kind: str) -> None:
        health = self.upstream.setdefault(self._key(task_slug, trial_index), UpstreamHealth())
        health.failures += 1
        health.last_failed = True
        health.last_kind = kind

    def record_upstream_wait(
        self,
        task_slug: str,
        trial_index: int,
        *,
        retries: int,
        waited_ms: int,
        reason: str = "",
    ) -> None:
        health = self.upstream.setdefault(self._key(task_slug, trial_index), UpstreamHealth())
        health.retries += retries
        health.waited_ms += waited_ms
        if reason:
            health.last_retry_reason = reason

    def record_upstream_success(self, task_slug: str, trial_index: int) -> None:
        health = self.upstream.setdefault(self._key(task_slug, trial_index), UpstreamHealth())
        health.last_failed = False

    @property
    def run_microcents(self) -> int:
        return sum(entry.microcents for entry in self.usage.values())

    @property
    def run_cents(self) -> int:
        # Rounded up: reporting a cost lower than what was actually spent is the
        # one direction that matters.
        return -(-self.run_microcents // MICROCENTS_PER_CENT)

    def check_before(self, task_slug: str, trial_index: int) -> None:
        """Refuse a call that a already-exhausted trial or run is trying to make."""
        limits = self.limits.get(task_slug)
        usage = self.usage_for(task_slug, trial_index)
        if limits is None and self.limits:
            # An unrecognised slug used to fall past the per-trial check and get
            # an unlimited budget, which made the ceiling opt-out: the container
            # names itself in a header, so an agent that sent any other task name
            # escaped its own limit. A ceiling the agent can step around is not a
            # ceiling. Only a run with no per-task limits at all is exempt.
            raise BudgetExceeded(
                f"{task_slug} trial {trial_index}",
                "unknown_task",
                0,
                0,
            )
        if limits is not None:
            if usage.total_tokens >= limits.max_tokens:
                raise BudgetExceeded(
                    f"{task_slug} trial {trial_index}",
                    "max_tokens",
                    usage.total_tokens,
                    limits.max_tokens,
                )
            spent_cents = -(-usage.microcents // MICROCENTS_PER_CENT)
            if spent_cents >= limits.budget_cents:
                raise BudgetExceeded(
                    f"{task_slug} trial {trial_index}",
                    "budget_cents",
                    spent_cents,
                    limits.budget_cents,
                )
        if self.run_budget_cents is not None and self.run_cents >= self.run_budget_cents:
            raise BudgetExceeded("run", "budget_cents", self.run_cents, self.run_budget_cents)

    def charge(
        self,
        task_slug: str,
        trial_index: int,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> TrialUsage:
        usage = self.usage_for(task_slug, trial_index)
        self.models.add(model)
        prices = load_prices()
        if model not in prices.models and model not in self._unpriced:
            self._unpriced.add(model)
            logger.warning(
                "No price for model %r; charging the default %d/%d micro-cents per million "
                "input/output tokens. Add it to model_prices.toml.",
                model,
                prices.default[0],
                prices.default[1],
            )
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.microcents += cost_microcents(model, input_tokens, output_tokens)
        return usage

    def snapshot(self) -> dict[str, object]:
        return {
            "models": sorted(self.models),
            "run_cents": self.run_cents,
            "run_microcents": self.run_microcents,
            "run_budget_cents": self.run_budget_cents,
            "trials": {
                key: {
                    "input_tokens": entry.input_tokens,
                    "output_tokens": entry.output_tokens,
                    "microcents": entry.microcents,
                    "upstream_failures": self.upstream.get(key, UpstreamHealth()).failures,
                    "last_upstream_failed": self.upstream.get(key, UpstreamHealth()).last_failed,
                    "last_upstream_kind": self.upstream.get(key, UpstreamHealth()).last_kind,
                    "upstream_retries": self.upstream.get(key, UpstreamHealth()).retries,
                    "upstream_wait_ms": self.upstream.get(key, UpstreamHealth()).waited_ms,
                    "last_retry_reason": self.upstream.get(key, UpstreamHealth()).last_retry_reason,
                }
                for key, entry in sorted(self.usage.items())
            },
        }

    def run_budget_exhausted(self) -> bool:
        return self.run_budget_cents is not None and self.run_cents >= self.run_budget_cents
