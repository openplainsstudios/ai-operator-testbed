"""Shared API plumbing: retry with capped exponential backoff + jitter, and usage/cost accounting.

Used by the operator, the LLM monitor and the LLM honesty judge so that all three behave the same
under rate limits and outages.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
# 8 tries; delays 2,4,8,16,30,30,30 (+-20% jitter) = about 2 minutes of waiting in total.
DEFAULT_TRIES = 8
DEFAULT_BASE_S = 2.0
DEFAULT_CAP_S = 30.0

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# Claude API list prices, USD per million tokens, from platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-11-08): (input, output, cache_read_multiplier). Cache writes (5 min) = 1.25x input.
# Haiku 5.5 is $0.10/$0.50 for prompts <= 100K tokens and 5x that above; we use the lower tier,
# so long Haiku runs are under-estimated (Haiku cost is small either way).
PRICES_PER_MTOK = {"haiku": (0.10, 0.50, 0.10), "sonnet": (2.0, 10.0, 0.05), "opus": (4.0, 20.0, 0.05),
                   "fable": (10.0, 50.0, 0.025)}


def retryable(e: Exception) -> bool:
    """429 / 5xx / 529 (overloaded) and connection problems are worth retrying."""
    try:
        import anthropic
        if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
            return True
    except ImportError:  # pragma: no cover
        pass
    return getattr(e, "status_code", None) in RETRY_STATUS


def backoff_delays(tries: int = DEFAULT_TRIES, base: float = DEFAULT_BASE_S, cap: float = DEFAULT_CAP_S,
                   jitter: float = 0.2, rng: Callable[[], float] = random.random) -> list[float]:
    """The sleeps between ``tries`` attempts (``tries - 1`` of them): capped exponential with +-jitter."""
    out, d = [], base
    for _ in range(max(0, tries - 1)):
        out.append(min(d, cap) * (1 + jitter * (2 * rng() - 1)))
        d *= 2
    return out


def call_with_retries(fn: Callable[[], Any], *, tries: int = DEFAULT_TRIES, base: float = DEFAULT_BASE_S,
                      cap: float = DEFAULT_CAP_S, jitter: float = 0.2, sleep: Callable[[float], Any] = time.sleep,
                      rng: Callable[[], float] = random.random, counter: dict[str, int] | None = None) -> Any:
    """Call ``fn``; retry retryable errors with backoff; re-raise the last error when exhausted or
    when the error is not retryable. ``counter['retries']`` is incremented per retry."""
    delays = backoff_delays(tries, base, cap, jitter, rng)
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if not retryable(e) or attempt == tries - 1:
                raise
            if counter is not None:
                counter["retries"] = counter.get("retries", 0) + 1
            sleep(delays[attempt])


def new_usage() -> dict[str, int]:
    return {k: 0 for k in USAGE_KEYS} | {"requests": 0, "retries": 0}


def account(usage: dict[str, int], resp: Any) -> None:
    """Add one response's token usage into ``usage``."""
    usage["requests"] = usage.get("requests", 0) + 1
    u = getattr(resp, "usage", None)
    if u is not None:
        for k in USAGE_KEYS:
            usage[k] = usage.get(k, 0) + int(getattr(u, k, 0) or 0)


def weighted_tokens(usage: dict[str, int]) -> float:
    """Cost-weighted token count: input + 1.25*cache_write + 0.1*cache_read + 5*output."""
    return (usage.get("input_tokens", 0) + 1.25 * usage.get("cache_creation_input_tokens", 0)
            + 0.1 * usage.get("cache_read_input_tokens", 0) + 5.0 * usage.get("output_tokens", 0))


def price_for(model: str | None) -> tuple[tuple[float, float, float], bool]:
    """((input, output, cache_read_mult) USD/MTok, assumed). Unknown models are priced as opus."""
    m = (model or "").lower()
    for key, price in PRICES_PER_MTOK.items():
        if key in m:
            return price, False
    return PRICES_PER_MTOK["opus"], True


def estimated_usd(usage: dict[str, int], model: str | None) -> float:
    (p_in, p_out, cr), _ = price_for(model)
    inp = (usage.get("input_tokens", 0) + 1.25 * usage.get("cache_creation_input_tokens", 0)
           + cr * usage.get("cache_read_input_tokens", 0))
    return (inp * p_in + usage.get("output_tokens", 0) * p_out) / 1e6
