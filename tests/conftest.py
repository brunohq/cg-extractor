from __future__ import annotations

import pytest

from cg_extract import client as client_module
from cg_extract.client import CentralGestClient
from cg_extract.throttle import Throttle

BASE = "https://cg.test"
API = f"{BASE}/api/v1"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)


@pytest.fixture
def client(fake_clock: FakeClock) -> CentralGestClient:
    throttle = Throttle(interval_s=1.0, jitter_s=0.0, max_requests=50, clock=fake_clock.clock, sleep=fake_clock.sleep)
    return CentralGestClient(api_key="office-key", throttle=throttle, base_url=BASE)


def mint_token(responses_mock, token: str = "company-token") -> None:
    responses_mock.post(f"{API}/auth/changeempresa", json={"status": 0, "message": "", "data": {"token": token}})
