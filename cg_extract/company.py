"""Resolve the one company this tool may read, confirmed by its NIF.

An office API key can reach several companies (test companies, client companies of an
accounting office). The extractor reads exactly one, chosen by NIF and required to be out
of training mode — never by its position in the list, never by name.

``allow_training`` (``CG_ALLOW_TRAINING_MODE=1``) accepts a training-mode company — for
sandbox keys, whose only company is often a demo. Off by default.

``cg_id`` (``CG_COMPANY_CGID``) skips the scan when the company's cgID is known: one token
mint and one read instead of one per reachable company. The NIF is still checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cg_extract.client import CentralGestClient, CentralGestError


@dataclass(frozen=True)
class Company:
    cg_id: int
    n_empresa: str
    nome: str
    nif: str


def _unwrap(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body and "status" in body:
        return body["data"]
    return body


def list_companies(client: CentralGestClient) -> list[dict[str, Any]]:
    """Every company the key reaches, with its NIF and training-mode flag (1 + 2 calls per company)."""
    found = []
    for entry in client.companies():
        cg_id = int(entry["cgID"])
        n_empresa = str(entry.get("nEmpresa", ""))
        info = _unwrap(client.get(f"/empresas/{n_empresa}", cg_id)) or {}
        found.append(
            {
                "cg_id": cg_id,
                "n_empresa": n_empresa,
                "nome": str(info.get("nome") or entry.get("nomeEmpresa", "")),
                "nif": str(info.get("nif", "")).strip(),
                "training_mode": bool(info.get("demonstracao")),
            }
        )
    return found


def resolve_company(
    client: CentralGestClient, nif: str, cg_id: int | None = None, *, allow_training: bool = False
) -> Company:
    if not nif:
        raise CentralGestError("No company NIF configured (CG_COMPANY_NIF).")
    erps = client.companies()
    candidates = [e for e in erps if cg_id is None or int(e.get("cgID", -1)) == cg_id]
    for entry in candidates:
        entry_cg_id = int(entry["cgID"])
        n_empresa = str(entry.get("nEmpresa", ""))
        info = _unwrap(client.get(f"/empresas/{n_empresa}", entry_cg_id)) or {}
        if str(info.get("nif", "")).strip() == nif and (allow_training or not info.get("demonstracao")):
            return Company(cg_id=entry_cg_id, n_empresa=n_empresa, nome=str(info.get("nome", "")), nif=nif)
    scope = f"cgID {cg_id}" if cg_id is not None else "No company reachable by this key"
    mode = "" if allow_training else " outside training mode"
    raise CentralGestError(
        f"{scope} does not carry NIF {nif}{mode}. `cg-extract companies` lists what the key reaches."
    )
