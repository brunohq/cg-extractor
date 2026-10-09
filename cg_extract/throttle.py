"""Client-side rate limiting for CentralGest.

CentralGest has no rate limiting of its own, and an abused API can take the tenant down —
the same tenant the company's day-to-day accounting runs on. So every call this tool makes
goes through one Throttle:

- Serial only. One request in flight at a time; there is no concurrency anywhere.
- A minimum gap between the *start* of consecutive requests (default 1.0s), plus random
  jitter so a scheduled run never lands as a perfectly regular burst.
- A floor (MIN_INTERVAL_FLOOR_S) that configuration cannot go below.
- A per-run request budget. Hitting it aborts the run: a runaway pager or an extractor
  bug fails loudly instead of hammering CG all night.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

MIN_INTERVAL_FLOOR_S = 0.5
DEFAULT_INTERVAL_S = 1.0
DEFAULT_JITTER_S = 0.25
DEFAULT_MAX_REQUESTS = 1000


class RequestBudgetExceeded(RuntimeError):
    """The run used up its request budget. Raise the budget deliberately, never in a loop."""


class Throttle:
    def __init__(
        self,
        interval_s: float = DEFAULT_INTERVAL_S,
        jitter_s: float = DEFAULT_JITTER_S,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if interval_s < MIN_INTERVAL_FLOOR_S:
            raise ValueError(f"interval_s={interval_s} is below the {MIN_INTERVAL_FLOOR_S}s floor CentralGest needs")
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self.interval_s = interval_s
        self.jitter_s = max(jitter_s, 0.0)
        self.max_requests = max_requests
        self.requests_made = 0
        self._clock = clock
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._rand = rand
        self._last_start: float | None = None

    def wait(self) -> None:
        """Block until the next request may start, and count it against the budget."""
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(
                f"Run reached its budget of {self.max_requests} CentralGest requests; aborting."
            )
        if self._last_start is not None:
            gap = self.interval_s + self._rand() * self.jitter_s
            remaining = gap - (self._clock() - self._last_start)
            if remaining > 0:
                self._sleep(remaining)
        self._last_start = self._clock()
        self.requests_made += 1
