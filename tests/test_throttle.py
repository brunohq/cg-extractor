from __future__ import annotations

import pytest

from cg_extract.throttle import RequestBudgetExceeded, Throttle


def test_spaces_requests_by_the_interval(fake_clock):
    throttle = Throttle(interval_s=1.0, jitter_s=0.0, clock=fake_clock.clock, sleep=fake_clock.sleep)
    throttle.wait()
    throttle.wait()
    fake_clock.now += 0.3  # some work happened between calls
    throttle.wait()
    assert fake_clock.slept == [1.0, pytest.approx(0.7)]


def test_jitter_only_ever_adds_delay(fake_clock):
    throttle = Throttle(interval_s=1.0, jitter_s=0.25, clock=fake_clock.clock, sleep=fake_clock.sleep, rand=lambda: 1.0)
    throttle.wait()
    throttle.wait()
    assert fake_clock.slept == [1.25]


def test_interval_below_the_floor_is_refused():
    with pytest.raises(ValueError):
        Throttle(interval_s=0.1)


def test_budget_aborts_the_run(fake_clock):
    throttle = Throttle(interval_s=1.0, max_requests=2, clock=fake_clock.clock, sleep=fake_clock.sleep)
    throttle.wait()
    throttle.wait()
    with pytest.raises(RequestBudgetExceeded):
        throttle.wait()
