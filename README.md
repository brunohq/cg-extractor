# cg-extractor

A read-only, rate-limited extractor for [CentralGest Cloud](https://www.centralgest.com) ERP data. It reads one company over the CentralGest REST API and writes newline-delimited JSON (NDJSON), which most warehouses and query engines load natively.

```
cron (off-peak) ─► cg-extract daily ─────────────► out/<entity>/<run>.jsonl ──────────► your warehouse
one-off         ─► cg-extract backfill invoices ─► out/invoices/backfill-YYYY-MM.jsonl ─┘
```

## Extractors

| Extractor | Kind | Endpoint | `daily` | `backfill` |
|---|---|---|---|---|
| `invoices` | dated | `/docscomerciais` | Documents dated in the window | Month by month, newest first, resumable |
| `clients` | snapshot | `/clientes` | Whole list | — (the first daily run is the history) |
| `articles` | snapshot | `/artigos` | Whole list | — |

## Run

```bash
uv sync
cp .env.example .env    # fill it in, then export it: set -a; . ./.env; set +a
uv run cg-extract companies                                     # find your company's NIF / cgID
uv run cg-extract daily                                         # every extractor, last 24h
uv run cg-extract daily invoices --hours 72
uv run cg-extract backfill invoices --from 2021-01 --to 2026-09
uv run pytest
```

| Env | Default | |
|---|---|---|
| `CG_BASE_URL` | (required) | `https://<tenant>.centralgestcloud.com`. This is live production; CentralGest has no sandbox |
| `CG_API_KEY` | (required) | Office API key. Never printed or logged |
| `CG_COMPANY_NIF` | (required) | NIF of the one company to read |
| `CG_COMPANY_CGID` | — | That company's cgID. It skips scanning every company the key reaches; the NIF is still checked |
| `CG_ALLOW_TRAINING_MODE` | — | `1` accepts a training-mode company. Useful for sandbox keys; off by default |
| `CG_TIMEZONE` | `Europe/Lisbon` | Calendar for business dates and backfill months |
| `CG_INTERVAL_S` | `1.0` | Minimum gap between requests. Cannot go below `0.5` |
| `CG_MAX_REQUESTS` | `1000` | Request budget per run. The run aborts when it's hit |

Exit code `0` means ok. `1` means a CentralGest error or the budget was hit; the failing file is not written. `2` means bad usage.

## Rules: why the code is shaped this way

1. **Read-only, always.** The transport refuses anything but `GET`. The only exception is the token handshake `POST /auth/changeempresa`, which creates no business record. A write raises `ReadOnlyViolation` before reaching the network.
2. **Be gentle with the tenant.** CentralGest has no rate limiting of its own, and an abused API can take down the tenant a company's accounting runs on. So the client:
   - sends requests strictly in series: 1 req/s plus jitter, through one shared `Throttle`;
   - has a per-run request budget;
   - never retries a `500` (CentralGest answers some bad input with a server memory fault), so the run stops instead;
   - retries `502/503/504` or a timeout once, after 10s;
   - never loops over parameters to "discover" behaviour.
3. **One company, by NIF.** It must be outside training mode, and is never picked by list position or name.
4. **Raw payload, model downstream.** Most CentralGest list endpoints have no documented response schema. Each record carries the API's row untouched in `payload`, so a field change upstream can't break a load.

Record (one line, one row):

```json
{"cg_id": 42, "entity_id": "1977", "stamp_update": "2026-10-09T00:13:07.000+01:00",
 "extracted_at": "2026-10-10T02:30:00+00:00", "source": "/docscomerciais", "payload": {"faccbId": 1977, "nDocFa": 1, "totalDocumento": 92.25}}
```

Suggested table: `payload` as a JSON column, partitioned on `extracted_at`. Rows are append-only: a document changed twice shows up twice, so the reporting view keeps the latest `stamp_update` (or `extracted_at`) per `entity_id`.

## Adding an extractor

1. **Master data:** subclass `SnapshotExtractor` and set `name`, `path` and `id_field`, as in `extractors/masterdata.py`.
   **Dated:** subclass `Extractor` with `kind = "dated"` and filter on the server through `pesquisa`, as in `extractors/invoices.py`. It becomes backfillable automatically.
2. Register it in `EXTRACTORS` in `cg_extract/extractors/__init__.py`.
3. Add a test with `responses` fixtures. No test ever calls a real tenant.

The endpoint catalogue is in each tenant's OpenAPI spec at `https://<tenant>.centralgestcloud.com/api/v1/api-doc/`.

## Filtering with `pesquisa`

The spec documents list parameters (`pesquisa`, `skip`/`take`, `pagina`/`porpagina`, `ordena`, `campospesq`) by name only. `pesquisa` takes a filter expression, the same one the CentralGest web app sends from its own search screens:

- comparisons `=`, `<>`, `>=`, `<=`; `&&` (and), `||` (or), parentheses;
- dates as UTC ISO strings with milliseconds, e.g. `dataDoc>=2026-10-07T23:00:00.000Z&&dataDoc<=2026-10-09T22:59:59.999Z`;
- example: `nconta>=211&&(ndocfa=1||ndocfa=3)`.

## Invoices (`/docscomerciais`)

- **Filtered on the server** by `dataDoc`. Each bound is a local midnight (`CG_TIMEZONE`) converted to UTC.
- **Whole local days**, because `dataDoc` is a date. A daily run reads yesterday and today. Overlapping runs produce duplicates, which the latest-row view removes.
- **The filter is checked on every run.** Each returned row's date is compared with the requested days. If the server ignores the filter, the run aborts instead of loading the whole list.
- **Header level only.** Each list row is one record: the full document header, about 120 fields, at one request per 100 documents. It covers every commercial document type; use `payload.nDocFa` to tell them apart.
- **Not included:** lines (`linhas`), the VAT summary (`resumoIva`) and the client snapshot (`clifo`). They exist only in `GET /docscomerciais/{faccbId}`; add a separate extractor if you need them.
- **Gap:** annulling an old document changes it without changing its `dataDoc`, and a `stampUpdate` filter is ignored by the server. Re-run recent months with `--force` periodically.

## Backfill

See [docs/backfill.md](docs/backfill.md) for rules, sizing, resume and validation.

## Verified against a live tenant

| Check | Result |
|---|---|
| Auth, company resolution, read-only calls | ✅ |
| `pesquisa` filter `dataDoc>=…&&dataDoc<=…` over the API | ✅ honoured. Months outside the data returned 0 rows, where an ignored filter would have tripped the row check |
| `/docscomerciais` list row | ✅ the full document header (~120 fields incl. `stampUpdate`, `dataVenc`, AT status, net/VAT/total). Lines are detail-only |
| `/clientes`, `/artigos` list rows | ✅ full records (~65 fields each, incl. `stampUpdate`) |
| `stampUpdate>=…` in `pesquisa` | ❌ **silently ignored**: all rows come back. Don't build incremental loads on it; re-run recent months with `--force` instead |
| Backfill resume | ✅ re-running skips finished months, costing only the setup calls |
| Paging beyond 100 rows (`skip`/`take`) | ⏳ not exercised yet (test data was smaller than one page). The pager aborts rather than loops if `skip` is ignored |

The ignored `stampUpdate` filter is why every dated extractor re-checks returned rows: the server ignores unknown filters without an error.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with CentralGest.
