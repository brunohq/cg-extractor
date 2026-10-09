"""cg-extract: read-only, rate-limited extraction of one CentralGest Cloud company into NDJSON.

  daily — what the nightly cron runs. Dated extractors read the window, snapshot extractors
          read their whole list:

    cg-extract daily                                  # every extractor, last 24h
    cg-extract daily invoices --hours 72
    cg-extract daily clients articles

  backfill — history for dated extractors, one local calendar month per file, newest first.
             A month whose file exists is done and skipped, so re-running resumes:

    cg-extract backfill invoices --from 2021-01 --to 2026-09
    cg-extract backfill invoices --from 2026-09 --to 2026-09 --force   # redo one month

  companies — list every company the key reaches (cgID, name, NIF, training mode), to find
              the values for CG_COMPANY_NIF / CG_COMPANY_CGID:

    cg-extract companies

Output: out/<extractor>/<run>.jsonl (daily) and out/<extractor>/backfill-YYYY-MM.jsonl.

Environment:
  CG_BASE_URL       https://<tenant>.centralgestcloud.com (required)
  CG_API_KEY        office API key (required; never printed)
  CG_COMPANY_NIF    NIF of the one company to read (required)
  CG_COMPANY_CGID   that company's cgID, to skip scanning every reachable company (optional)
  CG_ALLOW_TRAINING_MODE  set to 1 to read a training-mode company (sandbox keys)
  CG_TIMEZONE       calendar for business dates and backfill months (default Europe/Lisbon)
  CG_INTERVAL_S     min seconds between requests (default 1.0, floor 0.5)
  CG_MAX_REQUESTS   request budget per invocation (default 1000)

Exit codes: 0 ok, 1 CentralGest error / budget hit (the failing file is not written), 2 usage.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from cg_extract.client import CentralGestClient, CentralGestError
from cg_extract.company import Company, list_companies, resolve_company
from cg_extract.extractors import EXTRACTORS, Window
from cg_extract.extractors.base import LOCAL_TZ
from cg_extract.sinks import JsonlSink
from cg_extract.throttle import DEFAULT_INTERVAL_S, DEFAULT_MAX_REQUESTS, RequestBudgetExceeded, Throttle

logger = logging.getLogger("cg_extract")

DATED = sorted(name for name, cls in EXTRACTORS.items() if cls.kind == "dated")
STOP = (CentralGestError, RequestBudgetExceeded)


def _month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cg-extract", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=Path("out"), help="output folder (default ./out)")
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="nightly run: dated extractors read a window, snapshots read everything")
    daily.add_argument("extractors", nargs="*", choices=sorted(EXTRACTORS), help="default: all")
    window = daily.add_mutually_exclusive_group()
    window.add_argument("--hours", type=int, default=24, help="window length ending now (default 24)")
    window.add_argument("--since", type=datetime.fromisoformat, help="window start, ISO 8601 with offset")

    sub.add_parser("companies", help="list the companies this key reaches (needs no CG_COMPANY_NIF)")

    backfill = sub.add_parser("backfill", help="history for dated extractors, month by month, resumable")
    backfill.add_argument("extractors", nargs="+", choices=DATED)
    backfill.add_argument("--from", dest="first", type=_month, required=True, help="first month, YYYY-MM")
    backfill.add_argument("--to", dest="last", type=_month, required=True, help="last month, YYYY-MM")
    backfill.add_argument("--force", action="store_true", help="redo months that already have a file")
    return parser.parse_args(argv)


def _client() -> CentralGestClient:
    throttle = Throttle(
        interval_s=float(os.environ.get("CG_INTERVAL_S", DEFAULT_INTERVAL_S)),
        max_requests=int(os.environ.get("CG_MAX_REQUESTS", DEFAULT_MAX_REQUESTS)),
    )
    return CentralGestClient(
        api_key=os.environ.get("CG_API_KEY", ""),
        throttle=throttle,
        base_url=os.environ.get("CG_BASE_URL", ""),
    )


def _connect() -> tuple[CentralGestClient, Company]:
    client = _client()
    cg_id = os.environ.get("CG_COMPANY_CGID")
    allow_training = os.environ.get("CG_ALLOW_TRAINING_MODE") == "1"
    company = resolve_company(
        client, os.environ.get("CG_COMPANY_NIF", ""), int(cg_id) if cg_id else None, allow_training=allow_training
    )
    if allow_training:
        logger.warning("CG_ALLOW_TRAINING_MODE=1: training-mode companies are accepted")
    logger.info("Company cgID %s (%s)", company.cg_id, company.nome)
    return client, company


def months_newest_first(first: date, last: date) -> list[date]:
    months = []
    current = date(last.year, last.month, 1)
    while current >= first:
        months.append(current)
        current = date(current.year - (current.month == 1), (current.month - 2) % 12 + 1, 1)
    return months


def month_window(month: date) -> Window:
    start = datetime.combine(month, time.min, tzinfo=LOCAL_TZ)
    following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return Window(since=start, until=datetime.combine(following, time.min, tzinfo=LOCAL_TZ))


def _daily(args: argparse.Namespace, client: CentralGestClient, company: Company, sink: JsonlSink) -> bool:
    now = datetime.now(UTC)
    since = args.since if args.since else now - timedelta(hours=args.hours)
    window = Window(since=since, until=now)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    logger.info("Daily run %s, dated window %s -> %s", run_id, window.since, window.until)

    for name in args.extractors or sorted(EXTRACTORS):
        extractor = EXTRACTORS[name](client, company.cg_id)
        try:
            path, count = sink.write(name, run_id, extractor.extract(window, extracted_at=now.isoformat()))
        except STOP as exc:
            # Stop the whole run: the next extractor would hit the same unhealthy tenant.
            logger.error("%s failed after %d requests: %s", name, client.requests_made, exc)
            return False
        logger.info("%s (%s): %d records -> %s", name, extractor.kind, count, path)
    return True


def _backfill(args: argparse.Namespace, client: CentralGestClient, company: Company, sink: JsonlSink) -> bool:
    months = months_newest_first(args.first, args.last)
    for name in args.extractors:
        extractor = EXTRACTORS[name](client, company.cg_id)
        for i, month in enumerate(months, 1):
            run_id = f"backfill-{month:%Y-%m}"
            if sink.exists(name, run_id) and not args.force:
                logger.info("%s %s: done earlier, skipping (%d of %d)", name, f"{month:%Y-%m}", i, len(months))
                continue
            extracted_at = datetime.now(UTC).isoformat()
            try:
                path, count = sink.write(
                    name, run_id, extractor.extract(month_window(month), extracted_at=extracted_at)
                )
            except STOP as exc:
                logger.error(
                    "%s %s failed after %d requests: %s. Re-run the same command to resume from this month.",
                    name,
                    f"{month:%Y-%m}",
                    client.requests_made,
                    exc,
                )
                return False
            logger.info("%s %s: %d records -> %s (%d of %d)", name, f"{month:%Y-%m}", count, path, i, len(months))
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)

    if args.command == "daily" and args.since and args.since.tzinfo is None:
        print("--since needs a UTC offset, e.g. 2026-10-01T00:00:00+01:00", file=sys.stderr)
        return 2
    if args.command == "backfill" and args.first > args.last:
        print("--from must not be after --to", file=sys.stderr)
        return 2

    if args.command == "companies":
        try:
            client = _client()
            companies = list_companies(client)
        except STOP as exc:
            logger.error("Could not list companies: %s", exc)
            return 1
        print(f"{'cgID':>6}  {'nEmpresa':<9} {'NIF':<10} {'training':<8} name")
        for c in companies:
            training = "yes" if c["training_mode"] else "no"
            print(f"{c['cg_id']:>6}  {c['n_empresa']:<9} {c['nif']:<10} {training:<8} {c['nome']}")
        logger.info("Done: %d CentralGest requests", client.requests_made)
        return 0

    try:
        client, company = _connect()
    except STOP as exc:
        logger.error("Could not connect to the company: %s", exc)
        return 1

    sink = JsonlSink(args.out)
    ok = (_daily if args.command == "daily" else _backfill)(args, client, company, sink)
    logger.info("Done: %d CentralGest requests", client.requests_made)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
