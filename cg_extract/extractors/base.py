"""What every extractor shares: the time window, the record shape, and a safe pager.

Two kinds of extractor:
  dated     — transactions with a business date (invoices). Read a window; backfillable
              month by month. Daily runs read the last few days.
  snapshot  — master data (clients, articles). Small and changed in place, so every run
              reads the whole list; the window is ignored and there is nothing to backfill.

Record shape (one NDJSON line, one warehouse row):
  {
    "cg_id": 42,                      # company the row came from
    "entity_id": "1977",              # the entity's own id in CG
    "stamp_update": "2026-10-09T...", # CG's last-modified stamp, when the entity has one
    "extracted_at": "2026-10-10T...", # this run, UTC
    "source": "/docscomerciais",      # the endpoint the payload is from
    "payload": {...}                  # CG's response, untouched
  }

The payload lands raw (a JSON column) on purpose: most CG list endpoints have no
documented schema, so modelling happens in the warehouse, where a CG field change cannot
break the nightly load.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from cg_extract.client import CentralGestClient, CentralGestError, paged_rows

# Unverified against the live tenant: CG documents neither the max nor the default page size.
# paged() fails loudly if CG ignores it, rather than silently missing rows.
PAGE_SIZE = 100

#: Calendar used for business dates (``dataDoc``) and backfill months. CentralGest is
#: Portuguese software, so mainland Portugal is the default.
LOCAL_TZ = ZoneInfo(os.environ.get("CG_TIMEZONE", "Europe/Lisbon"))


@dataclass(frozen=True)
class Window:
    """[since, until) in absolute time. Daily runs end now; backfill months end at the next month's start."""

    since: datetime
    until: datetime


def paged(
    client: CentralGestClient, path: str, cg_id: int, *, id_field: str, pesquisa: str | None = None
) -> Iterator[dict[str, Any]]:
    """Walk a CG paged list with skip/take, serially, one throttled request per page.

    Stops at a short page or once ``total`` rows are seen. Aborts if CG hands back a page
    that starts with an id already seen — the sign it ignored ``skip`` — so a misread
    pager can never turn into an endless loop against production.

    ``pesquisa`` is CG's filter expression, e.g. ``dataDoc>=<iso>&&dataDoc<=<iso>``.
    """
    seen: set[Any] = set()
    skip = 0
    while True:
        params: dict[str, Any] = {"skip": skip, "take": PAGE_SIZE}
        if pesquisa:
            params["pesquisa"] = pesquisa
        rows, total = paged_rows(client.get(path, cg_id, params=params), endpoint=path)
        if not rows:
            return
        first = rows[0].get(id_field)
        if first in seen:
            raise CentralGestError(
                f"{path} returned a page starting at an id already seen ({first}); pager not honoured"
            )
        for row in rows:
            seen.add(row.get(id_field))
            yield row
        skip += len(rows)
        if len(rows) < PAGE_SIZE or (total is not None and skip >= total):
            return


class Extractor(ABC):
    #: CLI name and output folder / table suffix.
    name: ClassVar[str]
    kind: ClassVar[Literal["dated", "snapshot"]]

    def __init__(self, client: CentralGestClient, cg_id: int) -> None:
        self.client = client
        self.cg_id = cg_id

    @abstractmethod
    def extract(self, window: Window, *, extracted_at: str) -> Iterator[dict[str, Any]]:
        """Yield one record (see module docstring) per entity created or changed in the window."""

    def record(self, *, entity_id: Any, stamp_update: Any, source: str, payload: Any, extracted_at: str) -> dict:
        return {
            "cg_id": self.cg_id,
            "entity_id": str(entity_id),
            "stamp_update": stamp_update if isinstance(stamp_update, str) else None,
            "extracted_at": extracted_at,
            "source": source,
            "payload": payload,
        }


class SnapshotExtractor(Extractor):
    """Reads a whole master-data list every run. Subclasses set ``name``, ``path`` and ``id_field``."""

    kind = "snapshot"
    path: ClassVar[str]
    id_field: ClassVar[str]

    def extract(self, window: Window, *, extracted_at: str) -> Iterator[dict[str, Any]]:
        for row in paged(self.client, self.path, self.cg_id, id_field=self.id_field):
            entity_id = row.get(self.id_field)
            if entity_id in (None, ""):
                raise CentralGestError(f"{self.path} row without {self.id_field}: keys {sorted(row)}")
            yield self.record(
                entity_id=entity_id,
                stamp_update=row.get("stampUpdate"),
                source=self.path,
                payload=row,
                extracted_at=extracted_at,
            )
