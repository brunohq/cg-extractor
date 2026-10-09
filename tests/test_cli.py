from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from cg_extract import cli
from cg_extract.extractors.base import LOCAL_TZ
from tests.conftest import API, mint_token

NIF = "500000000"  # same NIF on both: the training-mode company must be skipped


@pytest.fixture
def cg(monkeypatch):
    """A fake CG tenant: the target company (cgID 42) and a training-mode test company (cgID 6)."""
    monkeypatch.setenv("CG_API_KEY", "office-key")
    monkeypatch.setenv("CG_BASE_URL", "https://cg.test")
    monkeypatch.setenv("CG_COMPANY_NIF", NIF)
    monkeypatch.setenv("CG_INTERVAL_S", "0.5")
    monkeypatch.setattr("cg_extract.throttle.time.sleep", lambda _s: None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        erps = [
            {"cgID": 6, "nEmpresa": "001", "nomeEmpresa": "EMPRESA DEMO"},
            {"cgID": 42, "nEmpresa": "002", "nomeEmpresa": "ACME, LDA"},
        ]
        rsps.get(f"{API}/auth/me", json={"status": 0, "data": {"erps": erps}})
        mint_token(rsps)
        rsps.get(f"{API}/empresas/001", json={"status": 0, "data": {"nif": NIF, "demonstracao": True}})
        rsps.get(f"{API}/empresas/002", json={"status": 0, "data": {"nif": NIF, "nome": "ACME, LDA"}})
        yield rsps


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def doc_list_calls(rsps):
    return [c for c in rsps.calls if urlparse(c.request.url).path.endswith("/docscomerciais")]


def test_months_newest_first_cross_year_boundaries():
    assert cli.months_newest_first(date(2025, 11, 1), date(2026, 2, 1)) == [
        date(2026, 2, 1),
        date(2026, 1, 1),
        date(2025, 12, 1),
        date(2025, 11, 1),
    ]


def test_daily_runs_every_extractor_by_default(cg, tmp_path):
    today = datetime.now(LOCAL_TZ).date().isoformat()
    cg.get(f"{API}/docscomerciais", json={"list": [{"faccbId": 7, "dataDoc": f"{today}T00:00:00"}], "total": 1})
    cg.get(f"{API}/clientes", json={"list": [{"nConta": "211110001"}], "total": 1})
    cg.get(f"{API}/artigos", json={"list": [{"nArtigo": "0056"}], "total": 1})

    assert cli.main(["--out", str(tmp_path), "daily"]) == 0

    for name, entity_id in [("invoices", "7"), ("clients", "211110001"), ("articles", "0056")]:
        [out] = list((tmp_path / name).glob("*.jsonl"))
        [record] = read(out)
        assert record["cg_id"] == 42 and record["entity_id"] == entity_id
    assert not list(tmp_path.rglob(".*partial"))


def test_backfill_writes_one_file_per_month_newest_first(cg, tmp_path):
    cg.get(f"{API}/docscomerciais", json={"list": [], "total": 0})

    assert cli.main(["--out", str(tmp_path), "backfill", "invoices", "--from", "2026-07", "--to", "2026-09"]) == 0

    names = sorted(p.name for p in (tmp_path / "invoices").iterdir())
    assert names == ["backfill-2026-07.jsonl", "backfill-2026-08.jsonl", "backfill-2026-09.jsonl"]
    filters = [parse_qs(urlparse(c.request.url).query)["pesquisa"][0] for c in doc_list_calls(cg)]
    assert filters[0].startswith("dataDoc>=2026-08-31T23:00:00.000Z")  # September first, local midnight
    assert filters[-1].startswith("dataDoc>=2026-06-30T23:00:00.000Z")


def test_backfill_resumes_and_skips_finished_months(cg, tmp_path):
    (tmp_path / "invoices").mkdir()
    (tmp_path / "invoices" / "backfill-2026-09.jsonl").write_text("")
    cg.get(f"{API}/docscomerciais", json={"list": [], "total": 0})

    assert cli.main(["--out", str(tmp_path), "backfill", "invoices", "--from", "2026-08", "--to", "2026-09"]) == 0
    assert len(doc_list_calls(cg)) == 1  # only August was read


def test_a_failed_month_is_left_undone_for_the_next_run(cg, tmp_path):
    cg.get(f"{API}/docscomerciais", json={"list": [], "total": 0})
    cg.get(f"{API}/docscomerciais", status=500, json={"exception": {"message": "Access violation"}})

    assert cli.main(["--out", str(tmp_path), "backfill", "invoices", "--from", "2026-08", "--to", "2026-09"]) == 1
    assert sorted(p.name for p in (tmp_path / "invoices").iterdir()) == ["backfill-2026-09.jsonl"]


def test_snapshot_extractors_cannot_be_backfilled(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "clients", "--from", "2026-01", "--to", "2026-02"])
    assert exc.value.code == 2


def test_a_known_cgid_skips_scanning_other_companies(cg, tmp_path, monkeypatch):
    monkeypatch.setenv("CG_COMPANY_CGID", "42")
    cg.get(f"{API}/clientes", json={"list": [], "total": 0})

    assert cli.main(["--out", str(tmp_path), "daily", "clients"]) == 0
    assert not any(urlparse(c.request.url).path.endswith("/empresas/001") for c in cg.calls)


def test_missing_nif_refuses_to_run(cg, tmp_path, monkeypatch):
    monkeypatch.delenv("CG_COMPANY_NIF")
    assert cli.main(["--out", str(tmp_path), "daily"]) == 1


def test_companies_lists_every_reachable_company_without_a_nif(cg, monkeypatch, capsys):
    monkeypatch.delenv("CG_COMPANY_NIF")
    assert cli.main(["companies"]) == 0
    out = capsys.readouterr().out
    lines = [line.split() for line in out.splitlines()[1:]]
    assert [(line[0], line[3]) for line in lines] == [("6", "yes"), ("42", "no")]


def test_training_mode_company_is_refused_unless_allowed(cg, tmp_path, monkeypatch):
    monkeypatch.setenv("CG_COMPANY_CGID", "6")
    assert cli.main(["--out", str(tmp_path), "daily", "clients"]) == 1

    monkeypatch.setenv("CG_ALLOW_TRAINING_MODE", "1")
    cg.get(f"{API}/clientes", json={"list": [], "total": 0})
    assert cli.main(["--out", str(tmp_path), "daily", "clients"]) == 0
