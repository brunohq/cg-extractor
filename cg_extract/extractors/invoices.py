"""Invoices: CentralGest commercial documents (``/docscomerciais``) dated inside the window, header level.

``/docscomerciais`` holds every commercial document type — invoices, credit notes,
invoice-receipts. All of them are extracted; ``payload.nDocFa`` tells them apart in
the warehouse.

Filtering happens on CG's side, through ``pesquisa``. The spec doesn't document its
grammar, so this uses exactly what CG's own web app sends from its document search
screen: ``dataDoc>=<iso>&&dataDoc<=<iso>``, each bound a local calendar day's midnight
rendered as UTC ``toISOString()``. (The same grammar has ``<>``, ``=``, ``||`` and
parentheses — e.g. ``&&(ndocfa=1||ndocfa=3)`` to restrict document types.)

``dataDoc`` is a calendar date, so the window is widened to whole local days: a daily
run reads yesterday and today. Overlapping runs return the same document twice; downstream
keeps the latest per ``entity_id``.

Not covered: an annulment changes an old document without changing its ``dataDoc``.
Filtering on ``stampUpdate`` would catch that, but CG's UI never does it, so it is
unproven — verify before relying on it.

Every returned row is re-checked against the requested days. If CG ever ignores the
filter and hands back the whole list, the run fails instead of silently loading it.

List rows only — one request per page, no per-document read. A row is the document's
header (number, type, client account, status, totals). Lines (``linhas``), the VAT summary
(``resumoIva``) and the client snapshot (``clifo``) live only in
``GET /docscomerciais/{faccbId}``; add that as a separate extractor if BI needs line level.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from cg_extract.client import CentralGestError
from cg_extract.extractors.base import LOCAL_TZ, Extractor, Window, paged

LIST_PATH = "/docscomerciais"
ID_FIELD = "faccbId"


def _iso_utc(moment: datetime) -> str:
    """The format CG's web app sends: JavaScript's Date.toISOString(), e.g. 2026-10-07T23:00:00.000Z."""
    utc = moment.astimezone(ZoneInfo("UTC"))
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def local_days(window: Window) -> tuple[date, date]:
    """Inclusive local calendar days covering [since, until). ``until`` is exclusive, so a
    backfill month ending at the next month's midnight does not spill into its first day."""
    last = (window.until - timedelta(microseconds=1)).astimezone(LOCAL_TZ).date()
    return window.since.astimezone(LOCAL_TZ).date(), last


def date_filter(first_day: date, last_day: date) -> str:
    """``pesquisa`` for documents dated first_day..last_day inclusive (local calendar days)."""
    start = datetime.combine(first_day, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ) - timedelta(milliseconds=1)
    return f"dataDoc>={_iso_utc(start)}&&dataDoc<={_iso_utc(end)}"


def check_in_days(row: dict[str, Any], first_day: date, last_day: date) -> None:
    raw = row.get("dataDoc")
    try:
        day = date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise CentralGestError(f"{LIST_PATH} row {row.get(ID_FIELD)!r} has no readable dataDoc ({raw!r})") from None
    if not first_day <= day <= last_day:
        raise CentralGestError(
            f"{LIST_PATH} returned document {row.get(ID_FIELD)!r} dated {day}, outside {first_day}..{last_day}: "
            "CG ignored the date filter. Aborting rather than loading the whole list."
        )


class InvoicesExtractor(Extractor):
    name = "invoices"
    kind = "dated"

    def extract(self, window: Window, *, extracted_at: str) -> Iterator[dict[str, Any]]:
        first_day, last_day = local_days(window)
        rows = list(
            paged(self.client, LIST_PATH, self.cg_id, id_field=ID_FIELD, pesquisa=date_filter(first_day, last_day))
        )
        for row in rows:
            check_in_days(row, first_day, last_day)
        for row in rows:
            faccb_id = row.get(ID_FIELD)
            if faccb_id is None:
                raise CentralGestError(f"{LIST_PATH} row without {ID_FIELD}: keys {sorted(row)}")
            yield self.record(
                entity_id=faccb_id,
                stamp_update=row.get("stampUpdate"),
                source=LIST_PATH,
                payload=row,
                extracted_at=extracted_at,
            )
