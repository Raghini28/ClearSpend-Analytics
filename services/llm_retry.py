"""Retry with backoff for transient LLM API failures (rate limits, overload)."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], *, retries: int = 5, base_delay: float = 0.6) -> T:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            retryable = any(
                x in msg
                for x in (
                    "429",
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
            time.sleep(base_delay * (2**attempt) + random.random() * 0.15)
    assert last is not None
    raise last
