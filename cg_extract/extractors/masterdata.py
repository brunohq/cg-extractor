"""Master data: full snapshots every run (see ``SnapshotExtractor``).

The list rows are the records — no per-entity reads. Field sets come from CG's untyped list
responses, so check the first output before modelling downstream.
"""

from __future__ import annotations

from cg_extract.extractors.base import SnapshotExtractor


class ClientsExtractor(SnapshotExtractor):
    """``GET /clientes`` — customer accounts (``nConta`` 211…). Suppliers are ``/fornecedores``."""

    name = "clients"
    path = "/clientes"
    id_field = "nConta"


class ArticlesExtractor(SnapshotExtractor):
    """``GET /artigos`` — the article (product/service) catalogue."""

    name = "articles"
    path = "/artigos"
    id_field = "nArtigo"
