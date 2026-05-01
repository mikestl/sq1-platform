"""SQ1 Platform — QuickBooks Online OAuth helper with rotation safety.

QBO refresh tokens rotate on every use: each call to ``/oauth2/v1/tokens/bearer``
issues a new refresh_token and invalidates the old one. With multiple consumers
(local MCP + cloud Function), naïve sharing breaks: whoever refreshes second
gets ``invalid_grant`` because the token they read is dead.

This module solves that by funneling **all** refreshes through ``QBOClient`` and
writing the new refresh_token back to Key Vault before returning. Local tools
also import this helper — same code path → same rotation safety.

Usage:
    from sq1_platform import QBOClient
    qbo = QBOClient()
    company = qbo.get(f"companyinfo/{qbo.realm_id()}")
    time_activity = qbo.query("SELECT * FROM TimeActivity STARTPOSITION 1 MAXRESULTS 1000")
"""
from __future__ import annotations

import base64
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

import requests

from .secrets import Secrets

QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API_BASE = "https://quickbooks.api.intuit.com/v3/company"

log = logging.getLogger("sq1_platform.qbo")


class QBOClient:
    """QBO API client with auto-rotating refresh token persistence."""

    def __init__(
        self,
        secrets: Optional[Secrets] = None,
        minor_version: str = "70",
    ) -> None:
        self.secrets = secrets or Secrets()
        self.minor_version = minor_version
        self._access_token: Optional[str] = None
        self._access_expiry: float = 0.0

    # ── Token management ───────────────────────────────────────────────────────

    def _refresh(self) -> str:
        """Exchange refresh_token for new access_token. Persists rotated value."""
        client_id = self.secrets.get("qbo-client-id")
        client_secret = self.secrets.get("qbo-client-secret")
        # ALWAYS read refresh fresh — another consumer may have rotated it
        refresh_token = self.secrets.get("qbo-refresh-token", fresh=True)

        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

        log.info("Requesting new QBO access_token (rotating refresh_token)")
        resp = requests.post(
            QBO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=body,
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(
                f"QBO token refresh failed: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        new_refresh = data["refresh_token"]
        new_access = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))

        # ATOMIC: persist new refresh token IMMEDIATELY, before any other code runs
        self.secrets.set("qbo-refresh-token", new_refresh)

        self._access_token = new_access
        self._access_expiry = time.time() + max(60, expires_in - 60)
        return new_access

    def access_token(self) -> str:
        """Cached access token; auto-refreshes when within 60s of expiry."""
        if self._access_token and time.time() < self._access_expiry:
            return self._access_token
        return self._refresh()

    def realm_id(self) -> str:
        return self.secrets.get("qbo-realm-id")

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{QBO_API_BASE}/{self.realm_id()}/{path.lstrip('/')}"

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        params = dict(params or {})
        params.setdefault("minorversion", self.minor_version)
        resp = requests.get(
            self._url(path),
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
            },
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def query(self, sql: str) -> dict:
        """Run a QBO SQL-like query (e.g. ``SELECT * FROM TimeActivity``)."""
        return self.get("query", params={"query": sql})

    def post(self, path: str, body: dict, params: Optional[Dict[str, Any]] = None) -> dict:
        params = dict(params or {})
        params.setdefault("minorversion", self.minor_version)
        resp = requests.post(
            self._url(path),
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params=params,
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    # ── TimeActivity convenience (used by hours_sync) ──────────────────────────

    def list_time_activity(self, max_per_page: int = 1000) -> list[dict]:
        """Paginate through ALL TimeActivity rows. Returns flat list of dicts."""
        all_entries: list[dict] = []
        start = 1
        while True:
            sql = (
                f"SELECT * FROM TimeActivity "
                f"STARTPOSITION {start} MAXRESULTS {max_per_page}"
            )
            data = self.query(sql)
            chunk = data.get("QueryResponse", {}).get("TimeActivity", [])
            all_entries.extend(chunk)
            if len(chunk) < max_per_page:
                break
            start += max_per_page
        log.info("Pulled %d TimeActivity entries", len(all_entries))
        return all_entries
