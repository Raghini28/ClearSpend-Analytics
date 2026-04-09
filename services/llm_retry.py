"""Retry with backoff for transient LLM API failures (rate limits, overload)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], *, retries: int = 8, base_delay: float = 0.6) -> T:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            hard_rl = "429" in msg or "rate_limit" in msg
            retryable = hard_rl or any(
                x in msg
                for x in (
                    "rate",
                    "too many requests",
                    "overloaded",
                    "capacity",
                    "timeout",
                    "temporarily",
                    "503",
                    "502",
                )
            )
            if not retryable or attempt == retries - 1:
                raise
            # Org TPM limits: wait longer so the next minute/sliding window clears.
            if hard_rl:
                wait = min(75.0, 12.0 * (2**attempt) + random.random() * 5.0)
            else:
                wait = base_delay * (2**attempt) + random.random() * 0.15
            time.sleep(wait)
    assert last is not None
    raise last
