"""SQLite persistence: runs, proposals, decisions, and applied writes.

The store is what makes the pipeline safe to run daily:

* a proposal is identified by a content fingerprint, so re-running produces the
  same id for the same suggested change;
* a fingerprint that already carries a decision (approved / rejected / applied)
  is never re-proposed;
* rejecting a *match* records a veto so the matcher stops pairing that location
  with that account on later runs;
* every account this pipeline creates is recorded against an idempotency key, so
  a crash between "create" and "record" cannot produce two accounts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    counts_json TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    fingerprint      TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL,
    rationale        TEXT NOT NULL,
    location_slug    TEXT,
    location_name    TEXT,
    account_id       TEXT,
    account_name     TEXT,
    severity         TEXT NOT NULL,
    actions_json     TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    first_seen_run   TEXT,
    last_seen_run    TEXT,
    first_seen_at    TEXT,
    last_seen_at     TEXT,
    decided_at       TEXT,
    decided_by       TEXT,
    decision_note    TEXT,
    applied_at       TEXT,
    apply_result_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);
CREATE TABLE IF NOT EXISTS vetoes (
    location_slug TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (location_slug, account_id)
);
CREATE TABLE IF NOT EXISTS created_accounts (
    idempotency_key TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    fingerprint     TEXT
);
"""

TERMINAL = ("applied", "rejected")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(kind: str, location_slug: str, account_id: str, actions: list[dict]) -> str:
    payload = canonical([kind, location_slug or "", account_id or "", actions])
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


class Store:
    def __init__(self, path=None):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path or config.DB_PATH)
        # check_same_thread=False so the threaded review server can share one
        # connection; every mutation goes through self.lock.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _exec(self, sql: str, args: tuple = ()):
        """Every write goes through here: one connection, serialised by a lock,
        committed immediately. The review server is threaded, so this is what
        keeps decisions from interleaving."""
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- runs ---
    def start_run(self) -> str:
        # Microseconds matter: two runs in the same second would share an id, and
        # obsolete_stale() compares on run id to find proposals this run did not
        # regenerate. A collision there would silently retire nothing.
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self._exec("INSERT OR REPLACE INTO runs(run_id, started_at) VALUES (?,?)",
                        (run_id, now()))
        return run_id

    def finish_run(self, run_id: str, counts: dict) -> None:
        self._exec("UPDATE runs SET finished_at=?, counts_json=? WHERE run_id=?",
                        (now(), canonical(counts), run_id))

    # ----------------------------------------------------------- proposals ---
    def upsert_proposal(self, p: dict, run_id: str) -> str:
        """Insert a freshly generated proposal, or refresh last_seen on an
        existing one. Never resurrects or mutates a decided proposal."""
        fp = p["fingerprint"]
        row = (self._query("SELECT status FROM proposals WHERE fingerprint=?", (fp,)) or [None])[0]
        if row:
            self._exec("UPDATE proposals SET last_seen_run=?, last_seen_at=? WHERE fingerprint=?",
                (run_id, now(), fp))
            return row["status"]
        self._exec("""INSERT INTO proposals
               (fingerprint, kind, title, rationale, location_slug, location_name,
                account_id, account_name, severity, actions_json, evidence_json,
                status, first_seen_run, last_seen_run, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
            (fp, p["kind"], p["title"], p["rationale"], p.get("location_slug"),
             p.get("location_name"), p.get("account_id"), p.get("account_name"),
             p["severity"], canonical(p["actions"]), canonical(p["evidence"]),
             run_id, run_id, now(), now()))
        return "pending"

    def obsolete_stale(self, run_id: str) -> int:
        """Pending proposals not regenerated by this run no longer reflect the
        CRM (someone fixed it by hand, or the site changed). Retire them."""
        cur = self._exec("UPDATE proposals SET status='obsolete', decided_at=?, decided_by='pipeline',"
            " decision_note='not regenerated by run ' || ?"
            " WHERE status='pending' AND last_seen_run != ?", (now(), run_id, run_id))
        return cur.rowcount

    def list_proposals(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM proposals"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += (" ORDER BY CASE severity WHEN 'confident' THEN 0 ELSE 1 END,"
              " kind, location_name, account_name")
        return [dict(r) for r in self._query(q, args)]

    def get_proposal(self, fp: str) -> dict | None:
        r = (self._query("SELECT * FROM proposals WHERE fingerprint=?", (fp,)) or [None])[0]
        return dict(r) if r else None

    def decide(self, fp: str, decision: str, by: str = "reviewer", note: str = "") -> None:
        if decision not in ("approved", "rejected", "pending"):
            raise ValueError(decision)
        p = self.get_proposal(fp)
        if not p:
            raise KeyError(fp)
        if p["status"] in TERMINAL and decision != "pending":
            raise ValueError(f"proposal {fp} is already {p['status']}")
        self._exec("UPDATE proposals SET status=?, decided_at=?, decided_by=?, decision_note=?"
            " WHERE fingerprint=?", (decision, now(), by, note, fp))
        if decision == "rejected" and p["kind"] in MATCH_KINDS and p["location_slug"] and p["account_id"]:
            self.add_veto(p["location_slug"], p["account_id"])

    def mark_applied(self, fp: str, result: Any, ok: bool) -> None:
        self._exec("UPDATE proposals SET status=?, applied_at=?, apply_result_json=? WHERE fingerprint=?",
            ("applied" if ok else "failed", now(), canonical(result), fp))

    # -------------------------------------------------------------- vetoes ---
    def add_veto(self, slug: str, account_id: str) -> None:
        self._exec("INSERT OR IGNORE INTO vetoes(location_slug, account_id, created_at) VALUES (?,?,?)",
            (slug, account_id, now()))

    def vetoes(self) -> set[tuple[str, str]]:
        return {(r["location_slug"], r["account_id"])
                for r in self._query("SELECT * FROM vetoes")}

    # ---------------------------------------------------- created accounts ---
    def created_account(self, key: str) -> str | None:
        rows = self._query(
            "SELECT account_id FROM created_accounts WHERE idempotency_key=?", (key,))
        return rows[0]["account_id"] if rows else None

    def record_created(self, key: str, account_id: str, fingerprint: str) -> None:
        self._exec("INSERT OR REPLACE INTO created_accounts(idempotency_key, account_id, created_at,"
            " fingerprint) VALUES (?,?,?,?)", (key, account_id, now(), fingerprint))

    # --------------------------------------------------------------- stats ---
    def counts(self) -> dict[str, int]:
        rows = self._query("SELECT status, COUNT(*) n FROM proposals GROUP BY status")
        return {r["status"]: r["n"] for r in rows}


# Kinds whose central claim is an *identity* claim ("this account IS this
# building"). Rejecting one of these records a veto so the matcher stops pairing
# them, which is what lets a rejected match turn into a "create account"
# proposal on the next run instead of coming back unchanged.
MATCH_KINDS = {"ambiguous_match", "mark_duplicate"}
