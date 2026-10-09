"""Extractor registry. Adding an entity = one class + one line in EXTRACTORS."""

from __future__ import annotations

from cg_extract.extractors.base import Extractor, SnapshotExtractor, Window
from cg_extract.extractors.invoices import InvoicesExtractor
from cg_extract.extractors.masterdata import ArticlesExtractor, ClientsExtractor

EXTRACTORS: dict[str, type[Extractor]] = {
    cls.name: cls for cls in (InvoicesExtractor, ClientsExtractor, ArticlesExtractor)
}

__all__ = ["EXTRACTORS", "Extractor", "SnapshotExtractor", "Window"]
