# cg-extractor

Read-only, rate-limited CentralGest Cloud extractor. It writes NDJSON per entity. README.md covers usage, `docs/backfill.md` the backfill procedure.

## Hard rules

- **Read-only, always.** Never add a non-GET call. The only POST is the token handshake `/auth/changeempresa`, allowlisted by exact path in `CentralGestClient._send`. Don't widen that allowlist, and don't give extractors a way to send a body.
- **Every request goes through the one `Throttle`.** No concurrency, no threads, no async fan-out. Never lower `MIN_INTERVAL_FLOOR_S` (0.5s) or the 1.0s default.
- **Never retry a 500.** CentralGest answers some malformed input with a server memory fault. Stop the run instead.
- **Never probe the live API.** No loops over parameters to discover behaviour, and no ad-hoc scripts against a tenant. Learn from the OpenAPI spec (`/api/v1/api-doc/`) or the web app's own requests. Any live check is a single, deliberate, human-run command.
- **One company, by NIF.** It must be outside training mode (`demonstracao`). Never pick by list position or name.
- **No secrets or real identifiers in the repo.** No API keys, tenant URLs, NIFs, cgIDs or company names in code, tests or docs. Configuration comes from env (`CG_*`), and tests use `https://cg.test` and fake NIFs.

## Shape

- `cg_extract/client.py`: transport, auth (office key → company token), error mapping, read-only guard.
- `cg_extract/throttle.py`: serial spacing plus a per-run request budget.
- `cg_extract/company.py`: resolves the target company by NIF.
- `cg_extract/extractors/`:
  - `base.py`: `Extractor` (`kind` is `dated` or `snapshot`), `SnapshotExtractor`, `paged()` (skip/take, aborts if the server ignores `skip`), `LOCAL_TZ`.
  - `invoices.py`: dated, with a server-side `pesquisa` filter on `dataDoc` and a re-check of every row against the window.
  - `masterdata.py`: snapshot subclasses.
- `cg_extract/sinks.py`: atomic NDJSON writes (`.partial` then rename). File existence means the unit is done, which is how backfill resumes.
- `cg_extract/cli.py`: the `daily` and `backfill` subcommands.

## Adding an extractor

- **Master data:** a `SnapshotExtractor` subclass with `name`, `path` and `id_field`.
- **Dated:** an `Extractor` with `kind = "dated"` that filters on the server through `pesquisa`. The grammar is in the README. Re-check returned rows so a silently ignored filter fails loudly.
- Register it in `extractors/__init__.py`, then add `responses`-based tests. Tests never hit the network.

Records keep the API row untouched in `payload`. Don't reshape data here; modelling happens downstream.

## Dev loop

```bash
uv sync
uv run pytest -q
uv run ruff format . && uv run ruff check .
```

Python ≥ 3.12, line length 120. Fail loud: unexpected shapes raise `CentralGestError` with the keys seen. Never guess.
