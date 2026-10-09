from __future__ import annotations

import pytest
import responses

from cg_extract.client import (
    CentralGestError,
    CentralGestTransientError,
    ReadOnlyViolation,
)
from tests.conftest import API, mint_token


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_are_refused_before_any_network_call(client, method):
    with responses.RequestsMock() as rsps:  # any HTTP call would fail as unregistered
        with pytest.raises(ReadOnlyViolation):
            client._send(method, "/api/v1/docscomerciais", token="t")
        assert len(rsps.calls) == 0


def test_the_handshake_is_the_only_post_allowed(client):
    with pytest.raises(ReadOnlyViolation):
        client._send("POST", "/api/v1/auth/changeempresa/../../docscomerciais", token="t")


@responses.activate
def test_company_get_mints_once_and_sends_the_company_token(client):
    mint_token(responses)
    responses.get(f"{API}/clientes", json={"list": [], "total": 0})
    client.get("/clientes", 9)
    client.get("/clientes", 9)
    assert [c.request.method for c in responses.calls] == ["POST", "GET", "GET"]
    assert responses.calls[1].request.headers["x-auth-token"] == "company-token"


@responses.activate
def test_401_remints_once(client):
    mint_token(responses, "stale")
    responses.get(f"{API}/clientes", status=401)
    responses.get(f"{API}/clientes", json={"list": [], "total": 0})
    mint_token(responses, "fresh")
    assert client.get("/clientes", 9) == {"list": [], "total": 0}
    assert responses.calls[-1].request.headers["x-auth-token"] == "fresh"


@responses.activate
def test_502_is_retried_once(client):
    mint_token(responses)
    responses.get(f"{API}/clientes", status=502)
    responses.get(f"{API}/clientes", json={"list": [], "total": 0})
    assert client.get("/clientes", 9) == {"list": [], "total": 0}


@responses.activate
def test_500_is_never_retried(client):
    mint_token(responses)
    responses.get(f"{API}/clientes", status=500, json={"exception": {"message": "Access violation"}})
    with pytest.raises(CentralGestTransientError, match="Access violation"):
        client.get("/clientes", 9)
    assert sum(1 for c in responses.calls if c.request.method == "GET") == 1


def test_empty_key_is_refused(fake_clock):
    from cg_extract.client import CentralGestClient
    from cg_extract.throttle import Throttle

    with pytest.raises(CentralGestError):
        CentralGestClient(api_key="", throttle=Throttle(), base_url="https://cg.test")


@responses.activate
def test_every_request_goes_through_the_throttle(client, fake_clock):
    mint_token(responses)
    responses.get(f"{API}/clientes", json={"list": [], "total": 0})
    client.get("/clientes", 9)
    client.get("/clientes", 9)
    assert client.requests_made == 3
    assert fake_clock.slept == [1.0, 1.0]


def test_empty_base_url_is_refused():
    from cg_extract.client import CentralGestClient
    from cg_extract.throttle import Throttle

    with pytest.raises(CentralGestError, match="base URL"):
        CentralGestClient(api_key="k", throttle=Throttle(), base_url="")
