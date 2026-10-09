from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from cg_extract.client import CentralGestError
from cg_extract.extractors import base as base_module
from cg_extract.extractors.base import Window
from cg_extract.extractors.invoices import InvoicesExtractor, date_filter
from tests.conftest import API, mint_token

# 03:00Z on 10-08 to 03:00Z on 10-09 -> local days 10-08 and 10-09.
WINDOW = Window(since=datetime(2026, 10, 8, 3, 0, tzinfo=UTC), until=datetime(2026, 10, 9, 3, 0, tzinfo=UTC))


def row(faccb_id: int, data_doc: str = "2026-10-08T00:00:00") -> dict:
    return {"faccbId": faccb_id, "dataDoc": data_doc}


def list_params(call) -> dict[str, list[str]]:
    return parse_qs(urlparse(call.request.url).query)


def test_date_filter_matches_what_cg_web_app_sends():
    # Local (default Europe/Lisbon) midnight rendered as Date.toISOString(): 23:00Z the day before while on summer time.
    assert date_filter(date(2026, 10, 8), date(2026, 10, 9)) == (
        "dataDoc>=2026-10-07T23:00:00.000Z&&dataDoc<=2026-10-09T22:59:59.999Z"
    )
    # Winter time: Europe/Lisbon is UTC+0.
    assert date_filter(date(2026, 12, 1), date(2026, 12, 1)) == (
        "dataDoc>=2026-12-01T00:00:00.000Z&&dataDoc<=2026-12-01T23:59:59.999Z"
    )


@responses.activate
def test_filters_on_cg_side_and_lands_each_list_row(client):
    mint_token(responses)
    first = {**row(1), "stampUpdate": "2026-10-08T10:00:00.000+01:00", "totalDocumento": 92.25}
    responses.get(f"{API}/docscomerciais", json={"list": [first, row(3, "2026-10-09T00:00:00")], "total": 2})

    records = list(InvoicesExtractor(client, 42).extract(WINDOW, extracted_at="2026-10-09T03:00:00+00:00"))

    assert [r["entity_id"] for r in records] == ["1", "3"]
    assert records[0]["payload"] == first
    assert records[0]["source"] == "/docscomerciais"
    assert records[0]["stamp_update"] == "2026-10-08T10:00:00.000+01:00"
    assert records[1]["stamp_update"] is None
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 1  # one page, no per-document reads
    assert list_params(gets[0])["pesquisa"] == [date_filter(date(2026, 10, 8), date(2026, 10, 9))]


@responses.activate
def test_an_ignored_filter_aborts_the_run(client):
    mint_token(responses)
    responses.get(f"{API}/docscomerciais", json={"list": [row(1), row(2, "2025-01-15T00:00:00")], "total": 2})

    with pytest.raises(CentralGestError, match="ignored the date filter"):
        list(InvoicesExtractor(client, 42).extract(WINDOW, extracted_at="x"))


@responses.activate
def test_a_row_without_a_date_fails_loudly(client):
    mint_token(responses)
    responses.get(f"{API}/docscomerciais", json={"list": [{"faccbId": 1}], "total": 1})

    with pytest.raises(CentralGestError, match="no readable dataDoc"):
        list(InvoicesExtractor(client, 42).extract(WINDOW, extracted_at="x"))


@responses.activate
def test_pages_with_skip_and_take_keeping_the_filter(client, monkeypatch):
    monkeypatch.setattr(base_module, "PAGE_SIZE", 2)
    mint_token(responses)
    responses.get(f"{API}/docscomerciais", json={"list": [row(1), row(2)], "total": 3})
    responses.get(f"{API}/docscomerciais", json={"list": [row(3)], "total": 3})

    assert len(list(InvoicesExtractor(client, 42).extract(WINDOW, extracted_at="x"))) == 3
    pages = [list_params(c) for c in responses.calls if c.request.url.split("?")[0].endswith("/docscomerciais")]
    assert [(p["skip"], p["take"]) for p in pages] == [(["0"], ["2"]), (["2"], ["2"])]
    assert all("pesquisa" in p for p in pages)


@responses.activate
def test_a_pager_that_ignores_skip_aborts_instead_of_looping(client, monkeypatch):
    monkeypatch.setattr(base_module, "PAGE_SIZE", 2)
    mint_token(responses)
    responses.get(f"{API}/docscomerciais", json={"list": [row(1), row(2)]})

    with pytest.raises(CentralGestError, match="pager not honoured"):
        list(InvoicesExtractor(client, 42).extract(WINDOW, extracted_at="x"))


def test_a_backfill_month_does_not_spill_into_the_next_month():
    from cg_extract.cli import month_window
    from cg_extract.extractors.invoices import local_days

    assert local_days(month_window(date(2026, 2, 1))) == (date(2026, 2, 1), date(2026, 2, 28))
    assert local_days(month_window(date(2026, 12, 1))) == (date(2026, 12, 1), date(2026, 12, 31))
