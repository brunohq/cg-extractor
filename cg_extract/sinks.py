"""Where records go. Today: newline-delimited JSON files, one per extractor per run.

NDJSON is a native bulk-load format for most warehouses and query engines, so loading is
one command on the destination side. A sink that writes straight to a warehouse can
replace this class; extractors don't change.
Files are written to a temp name and renamed, so a crashed run never leaves a half file
that a loader might pick up.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class JsonlSink:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def exists(self, extractor: str, run_id: str) -> bool:
        """A final file only appears after a complete write, so existing means done."""
        return (self.out_dir / extractor / f"{run_id}.jsonl").exists()

    def write(self, extractor: str, run_id: str, records: Iterable[dict[str, Any]]) -> tuple[Path, int]:
        folder = self.out_dir / extractor
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"{run_id}.jsonl"
        tmp = folder / f".{run_id}.jsonl.partial"
        count = 0
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    count += 1
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, final)
        return final, count
