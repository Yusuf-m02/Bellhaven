"""Bellhaven CRM API client + account snapshot handling."""
from __future__ import annotations

import json
from typing import Any, Iterator

from . import config, httpc

WRITABLE_FIELDS = {
    "name", "parent_id", "billing_street", "billing_city", "billing_state",
    "billing_zip", "care_type", "status", "phone", "note",
    "chow_current_account", "duplicate_of_account",
}

VALID_STATUS = {"Active", "Inactive", "Needs Review"}


class CrmClient:
    def __init__(self, base: str | None = None, token: str | None = None):
        self.base = (base or config.API_BASE).rstrip("/")
        self.token = token or config.API_TOKEN
        if not self.token:
            raise RuntimeError(
                "No CRM token found.\n"
                "  set BELLHAVEN_TOKEN=bh_...        (Windows)\n"
                "  export BELLHAVEN_TOKEN=bh_...     (macOS/Linux)\n"
                "or write the token to token.txt in the project root (gitignored).")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ------------------------------------------------------------- reading ---
    def iter_accounts(self, page_size: int = 200, **params) -> Iterator[dict]:
        page = 1
        while True:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
            url = f"{self.base}/accounts?page={page}&page_size={page_size}" + (f"&{qs}" if qs else "")
            payload = httpc.get_json(url, headers=self._headers)
            rows = payload.get("data", [])
            for r in rows:
                yield r
            total = payload.get("total", 0)
            page += 1
            if not rows or (page - 1) * page_size >= total:
                return

    def list_accounts(self, **params) -> list[dict]:
        return list(self.iter_accounts(**params))

    def get_account(self, account_id: str) -> dict:
        return httpc.get_json(f"{self.base}/accounts/{account_id}", headers=self._headers)

    def list_contacts(self, page_size: int = 200) -> list[dict]:
        out, page = [], 1
        while True:
            payload = httpc.get_json(
                f"{self.base}/contacts?page={page}&page_size={page_size}", headers=self._headers)
            rows = payload.get("data", [])
            out.extend(rows)
            if not rows or len(out) >= payload.get("total", 0):
                return out
            page += 1

    # ------------------------------------------------------------- writing ---
    def update_account(self, account_id: str, fields: dict[str, Any]) -> dict:
        bad = set(fields) - WRITABLE_FIELDS
        if bad:
            raise ValueError(f"refusing to write unknown fields: {sorted(bad)}")
        if "status" in fields and fields["status"] not in VALID_STATUS:
            raise ValueError(f"invalid status {fields['status']!r}")
        status, text = httpc.request(
            "PATCH", f"{self.base}/accounts/{account_id}",
            headers=self._headers, body=fields)
        return json.loads(text) if text.strip() else {"status": status}

    def create_account(self, fields: dict[str, Any]) -> dict:
        bad = set(fields) - WRITABLE_FIELDS
        if bad:
            raise ValueError(f"refusing to write unknown fields: {sorted(bad)}")
        status, text = httpc.request(
            "POST", f"{self.base}/accounts", headers=self._headers, body=fields)
        return json.loads(text) if text.strip() else {"status": status}


# ----------------------------------------------------------------- snapshot ---

def write_snapshot(accounts: list[dict], contacts: list[dict] | None = None) -> None:
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (config.SNAPSHOT_DIR / "crm_accounts.json").write_text(
        json.dumps({"total": len(accounts), "data": accounts}, indent=2), encoding="utf-8")
    if contacts is not None:
        (config.SNAPSHOT_DIR / "crm_contacts.json").write_text(
            json.dumps({"total": len(contacts), "data": contacts}, indent=2), encoding="utf-8")


def load_snapshot() -> list[dict]:
    p = config.SNAPSHOT_DIR / "crm_accounts.json"
    if not p.exists():  # fall back to the raw cache captured by `fetch`
        p = config.RAW_DIR / "crm_accounts.json"
    return json.loads(p.read_text(encoding="utf-8"))["data"]
