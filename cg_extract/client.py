"""Read-only HTTP client for a CentralGest Cloud tenant.

The tenant (``https://<tenant>.centralgestcloud.com``) is live production: CentralGest
offers no sandbox, so everything this client calls is real.

Auth, as CG does it:
  1. The office API key goes in ``x-auth-token``.
  2. ``GET /auth/me`` lists the companies (``data.erps[]``) the key reaches.
  3. ``POST /auth/changeempresa?cgId=<n>`` mints a company token (``data.token``).
     That POST is a session handshake — it writes no business record.
  4. Company-scoped calls send the company token in ``x-auth-token``.

Read-only, always:
  The transport refuses every method except GET, with one exception — the handshake
  POST in step 3, matched by exact path. Anything else raises ReadOnlyViolation before
  a byte leaves the machine. Extractors never get a way to send a body.

Envelopes:
  Most reads wrap the payload as {"status": 0, "message": "", "data": {...}}. Paged lists
  (/docscomerciais, /clientes, /artigos, /recibos, ...) answer {"list": [...], "total": N}
  with no wrapper. ``get()`` returns whichever shape came back; ``paged_rows`` and
  ``wrapped_data`` unwrap.

Retry policy:
  One retry, after a backoff, on a connection error, a timeout, or a 502/503/504 — those
  come from the infrastructure in front of CG. A 500 is never retried: CG answers some
  malformed input with a server memory fault, and a second call hits a tenant that is
  already unhealthy. The run stops instead. A 401 on a company token re-mints it once.

Never logged: the office API key and any company token.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn

import requests

from cg_extract.throttle import Throttle

logger = logging.getLogger(__name__)

JSONBody = dict[str, Any] | list[Any]

API_PREFIX = "/api/v1"
HANDSHAKE_PATH = f"{API_PREFIX}/auth/changeempresa"

_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 60.0
_RETRY_BACKOFF_S = 10.0
_RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
# CG does not document a company token's lifetime; re-mint well before any plausible limit.
_TOKEN_TTL_S = 30 * 60


class CentralGestError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CentralGestAuthError(CentralGestError):
    """CG rejected the token (HTTP 401)."""


class CentralGestTransientError(CentralGestError):
    """Connection error, timeout, or 5xx. Never retried by the caller within the same run."""


class ReadOnlyViolation(RuntimeError):
    """Something tried to send a non-read request. This is a bug, never a condition to handle."""


def _decode(resp: requests.Response) -> JSONBody:
    if not resp.content:
        return {}
    try:
        body = resp.json()
    except ValueError:
        raise CentralGestError(f"CentralGest returned a non-JSON body (status {resp.status_code})") from None
    if not isinstance(body, dict | list):
        raise CentralGestError(f"CentralGest returned a JSON {type(body).__name__}, expected an object or array")
    return body


def _raise_for_error(resp: requests.Response) -> NoReturn:
    status = resp.status_code
    if status == 401:
        raise CentralGestAuthError("CentralGest rejected the auth token (401)", status_code=status)
    try:
        body: Any = resp.json()
    except ValueError:
        # 403 (unlicensed module) comes back empty; a 502 from CG's nginx comes back as HTML.
        body = None
    message = ""
    if isinstance(body, dict) and isinstance(body.get("exception"), dict):
        message = body["exception"].get("message") or ""
    detail = message or (resp.text[:300] if resp.text else "no body")
    if status >= 500:
        raise CentralGestTransientError(f"CentralGest error {status}: {detail}", status_code=status)
    raise CentralGestError(f"CentralGest error {status}: {detail}", status_code=status)


def wrapped_data(body: JSONBody, *, endpoint: str) -> Any:
    """Unwrap {"status", "message", "data"}. A null ``data`` is an error dressed as a 200."""
    if not isinstance(body, dict) or body.get("data") is None:
        message = body.get("message") if isinstance(body, dict) else ""
        raise CentralGestError(f"CentralGest returned no usable data for {endpoint}: {message or 'empty envelope'}")
    return body["data"]


def paged_rows(body: JSONBody, *, endpoint: str) -> tuple[list[dict[str, Any]], int | None]:
    """Unwrap {"list": [...], "total": N} into (rows, total)."""
    rows = body.get("list") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise CentralGestError(f"CentralGest returned an unexpected paged-list shape for {endpoint}")
    total = body.get("total") if isinstance(body, dict) else None
    return rows, total if isinstance(total, int) else None


class CentralGestClient:
    """One instance per run: it owns the HTTP session, the company-token cache and — via the
    shared Throttle — the run's request budget."""

    def __init__(self, api_key: str, throttle: Throttle, base_url: str) -> None:
        if not base_url:
            raise CentralGestError("No CentralGest base URL (https://<tenant>.centralgestcloud.com).")
        if not api_key:
            # CG answers a malformed token with a server-side fault, not a 401.
            raise CentralGestError("No CentralGest API key. Refusing to call CentralGest with an empty token.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._throttle = throttle
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._company_tokens: dict[int, tuple[str, float]] = {}

    @property
    def requests_made(self) -> int:
        return self._throttle.requests_made

    def _send(self, method: str, path: str, *, token: str, params: dict[str, Any] | None = None) -> requests.Response:
        method = method.upper()
        if method != "GET" and not (method == "POST" and path == HANDSHAKE_PATH):
            raise ReadOnlyViolation(f"Refusing {method} {path}: this tool is read-only.")

        url = f"{self._base_url}{path}"
        for attempt in range(2):
            self._throttle.wait()
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    headers={"x-auth-token": token},
                    timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == 0:
                    logger.warning("CentralGest unreachable on %s %s, retrying once: %s", method, path, exc)
                    time.sleep(_RETRY_BACKOFF_S)
                    continue
                raise CentralGestTransientError(f"CentralGest unreachable: {exc}") from None

            if resp.status_code < 300:
                return resp
            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt == 0:
                logger.warning("CentralGest %s on %s %s, retrying once", resp.status_code, method, path)
                time.sleep(_RETRY_BACKOFF_S)
                continue
            _raise_for_error(resp)
        raise CentralGestTransientError("CentralGest retry loop ended without a response")

    # -- auth ----------------------------------------------------------------

    def companies(self) -> list[dict[str, Any]]:
        """GET /auth/me -> data.erps[]: every company the office key reaches."""
        body = _decode(self._send("GET", f"{API_PREFIX}/auth/me", token=self._api_key))
        erps = wrapped_data(body, endpoint="/auth/me").get("erps") or []
        if not isinstance(erps, list):
            raise CentralGestError("CentralGest returned a non-list data.erps")
        return erps

    def company_token(self, cg_id: int, *, force_refresh: bool = False) -> str:
        cached = self._company_tokens.get(cg_id)
        if cached and not force_refresh and time.monotonic() - cached[1] < _TOKEN_TTL_S:
            return cached[0]
        resp = self._send("POST", HANDSHAKE_PATH, token=self._api_key, params={"cgId": cg_id})
        token = wrapped_data(_decode(resp), endpoint="/auth/changeempresa").get("token")
        if not isinstance(token, str) or not token:
            raise CentralGestError(f"CentralGest returned no company token for cgID {cg_id}")
        self._company_tokens[cg_id] = (token, time.monotonic())
        return token

    # -- reads ---------------------------------------------------------------

    def get(self, path: str, cg_id: int, *, params: dict[str, Any] | None = None) -> JSONBody:
        """Company-scoped GET. ``path`` is relative to /api/v1, e.g. "/docscomerciais"."""
        full = f"{API_PREFIX}{path}"
        try:
            resp = self._send("GET", full, token=self.company_token(cg_id), params=params)
        except CentralGestAuthError:
            # A 401 is decided at CG's gate, so re-minting once and resending is safe.
            resp = self._send("GET", full, token=self.company_token(cg_id, force_refresh=True), params=params)
        return _decode(resp)
