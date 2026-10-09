from __future__ import annotations

from datetime import UTC, datetime

import pytest
import responses

from cg_extract.client import CentralGestError
from cg_extract.extractors.base import Window
from cg_extract.extractors.masterdata import ArticlesExtractor, ClientsExtractor
from tests.conftest import API, mint_token

ANY_WINDOW = Window(since=datetime(2026, 10, 8, tzinfo=UTC), until=datetime(2026, 10, 9, tzinfo=UTC))


@responses.activate
def test_clients_snapshot_reads_the_whole_list_without_a_filter(client):
    mint_token(responses)
    rows = [
        {"nConta": "211110001", "nome": "A", "stampUpdate": "2020-01-01T00:00:00.000+00:00"},
        {"nConta": "211110002"},
    ]
    responses.get(f"{API}/clientes", json={"list": rows, "total": 2})

    records = list(ClientsExtractor(client, 42).extract(ANY_WINDOW, extracted_at="x"))

    assert [r["entity_id"] for r in records] == ["211110001", "211110002"]
    assert records[0]["payload"] == rows[0] and records[0]["source"] == "/clientes"
    assert records[0]["stamp_update"] == "2020-01-01T00:00:00.000+00:00"  # old rows are kept: it's a snapshot
    list_call = next(c for c in responses.calls if c.request.method == "GET")
    assert "pesquisa" not in list_call.request.url


@responses.activate
def test_articles_snapshot_keys_on_the_article_code(client):
    mint_token(responses)
    responses.get(f"{API}/artigos", json={"list": [{"nArtigo": "0056", "nome": "Subscription"}], "total": 1})

    [record] = list(ArticlesExtractor(client, 42).extract(ANY_WINDOW, extracted_at="x"))
    assert record["entity_id"] == "0056"


@responses.activate
def test_a_row_without_its_id_fails_loudly(client):
    mint_token(responses)
    responses.get(f"{API}/artigos", json={"list": [{"nome": "?"}], "total": 1})

    with pytest.raises(CentralGestError, match="without nArtigo"):
        list(ArticlesExtractor(client, 42).extract(ANY_WINDOW, extracted_at="x"))
