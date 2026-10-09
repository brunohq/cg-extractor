# Backfill

How to load a company's CentralGest history without hurting the live tenant its accounting runs on.

## Rules

1. **Coordinate with CentralGest before a large backfill.** The API has no rate limiting of its own. Tell them the window and the expected request count.
2. **One process at a time.** A backfill never runs alongside the daily cron or another backfill. They share one request budget.
3. **Off-peak only:** nights and weekends.
4. **Same throttle, never faster.** `CG_INTERVAL_S` stays at 1.0. If a backfill is slow, raise `CG_MAX_REQUESTS` to make the night longer, not denser.
5. **A 500 ends the night.** Resume the next night with the same command. Repeated failures mean talking to CentralGest before trying again.

## Unit of work, by entity kind

| Kind | Example | Unit | Done when | Resume |
|---|---|---|---|---|
| **Dated** (server-side date filter) | invoices | one entity × one local month | `out/<entity>/backfill-YYYY-MM.jsonl` exists | Re-run the same command; finished months are skipped |
| **Snapshot** (master data) | clients, articles | the whole list | The first `daily` run. It *is* the history | Re-run `daily <entity>` |
| **Undated, large** (no filter, too big for one run) | — | page ranges (`skip`) | Not built yet. Add a checkpoint on `skip` when needed | — |

Months run **newest first**. The most useful data lands first, and recent months are the easiest to check against the CentralGest UI.

## Steps

1. **Size it** (about 1 request per entity).
   - Read `total` from one list call per entity with `take=1`.
   - Requests ≈ `total / 100` plus one per month (empty months still cost a call), plus 3 for setup.
   - Example: 10k documents over 6 years ≈ 100 + 72 + 3 = ~3 minutes at 1 req/s.
2. **Start the daily cron first.** From then on, yesterday and today are always covered, so the backfill only needs complete past months.
3. **Run the backfill**, for as many months as one night's budget allows:
   ```bash
   CG_MAX_REQUESTS=5000 cg-extract backfill invoices --from 2021-01 --to 2026-09
   ```
   Logs read `invoices 2026-07: 41 records -> … (3 of 69)`. On failure the log names the month, and re-running the identical command resumes there.
4. **Load every file**, backfill and daily alike, into an append-only table. A view keeping the latest row per `entity_id` makes overlaps and re-runs harmless.
5. **Validate** 3 sample months (newest, one mid-range, oldest) against the same period in the CentralGest UI:
   - document count;
   - sum of `totalDocumento`.

   A mismatch means re-running that month with `--force` after finding the cause. Don't patch it downstream.

## Re-running and corrections

- `--force` redoes chosen months (`--from 2025-03 --to 2025-03 --force`). The table is append-only and the view takes the latest row, so a redo replaces nothing destructively.
- **Annulments on old documents:** a `dataDoc` filter doesn't see an old invoice annulled today. The server ignores a `stampUpdate` filter, so re-run recent months with `--force` periodically.
