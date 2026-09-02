"""Pipeline stages: fetch -> pipeline (propose) -> review -> apply.

The stages are deliberately separate processes:

  fetch     needs the network, writes data/raw + data/snapshot
  pipeline  pure function of the snapshot -> proposals in SQLite (no network)
  review    human decisions against SQLite (no network)
  apply     needs the network, executes only approved proposals

That split is what makes the run reproducible: `pipeline` can be re-run over a
saved snapshot to reproduce a decision exactly, and `apply` can never invent a
write that a reviewer did not approve.
"""
from __future__ import annotations

import json

from . import config, crm, match, propose, scraper, store


def stage_fetch(live: bool = True) -> dict:
    site = scraper.fetch_site(live=live)
    scraper.write_snapshot(site)
    if live:
        client = crm.CrmClient()
        accounts = client.list_accounts()
        contacts = client.list_contacts()
        crm.write_snapshot(accounts, contacts)
    else:
        accounts = crm.load_snapshot()
    return {"locations": len(site["locations"]), "accounts": len(accounts),
            "announcements": len(site.get("announcements", [])),
            "acquisitions": len(site.get("acquisitions", []))}


def stage_pipeline(st: store.Store) -> dict:
    site = scraper.load_snapshot()
    accounts = crm.load_snapshot()
    run_id = st.start_run()

    result = match.build(site["locations"], accounts,
                         confident_at=config.CONFIDENT_AT,
                         review_at=config.REVIEW_AT,
                         vetoes=st.vetoes())
    proposer = propose.Proposer(accounts, site, run_id)
    proposals = proposer.run(result)

    counts = {"generated": len(proposals), "new": 0, "already_decided": 0}
    by_kind: dict[str, int] = {}
    for p in proposals:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
        status = st.upsert_proposal(p, run_id)
        counts["new" if status == "pending" else "already_decided"] += 1
    counts["obsoleted"] = st.obsolete_stale(run_id)
    counts["by_kind"] = by_kind
    counts["run_id"] = run_id
    st.finish_run(run_id, counts)
    return counts


def _resolve(value, refs: dict[str, str]):
    if isinstance(value, str) and value.startswith("$ref:"):
        key = value[5:]
        if key not in refs:
            raise KeyError(f"unresolved reference {value}")
        return refs[key]
    return value


def stage_apply(st: store.Store, client: crm.CrmClient | None = None,
                dry_run: bool = False, plan_out: str | None = None) -> dict:
    """Execute every approved-but-not-applied proposal.

    Idempotent: a create is skipped if its idempotency key already produced an
    account, and applied proposals move to status 'applied' so a second run is a
    no-op. `plan_out` writes the resolved API calls to a JSON file instead of
    sending them, for environments where the reviewer and the network live in
    different places.
    """
    client = client or (None if (dry_run or plan_out) else crm.CrmClient())
    approved = st.list_proposals("approved")
    summary = {"approved": len(approved), "applied": 0, "skipped": 0, "failed": 0,
               "calls": []}

    for p in approved:
        actions = json.loads(p["actions_json"])
        refs: dict[str, str] = {}
        results = []
        ok = True
        for action in actions:
            fields = {k: _resolve(v, refs) for k, v in action["fields"].items()} \
                if action["op"] in ("create", "patch") else {}
            if action["op"] == "create":
                key = action.get("idempotency_key") or p["fingerprint"]
                existing = st.created_account(key)
                if existing:
                    refs[action.get("ref", "new")] = existing
                    results.append({"op": "create", "skipped": True,
                                    "account_id": existing,
                                    "reason": "idempotency key already used"})
                    summary["skipped"] += 1
                    continue
                call = {"method": "POST", "path": "/accounts", "body": fields,
                        "idempotency_key": key, "fingerprint": p["fingerprint"],
                        "ref": action.get("ref", "new")}
                summary["calls"].append(call)
                if dry_run or plan_out:
                    refs[action.get("ref", "new")] = f"<new:{key}>"
                    results.append({"op": "create", "planned": True})
                    continue
                try:
                    created = client.create_account(fields)
                    new_id = created.get("account_id") or created.get("id", "")
                    if not new_id:
                        raise RuntimeError(f"create returned no id: {created}")
                    st.record_created(key, new_id, p["fingerprint"])
                    refs[action.get("ref", "new")] = new_id
                    results.append({"op": "create", "account_id": new_id})
                except Exception as e:  # noqa: BLE001 - recorded, not swallowed
                    ok = False
                    results.append({"op": "create", "error": str(e)})
                    break
            elif action["op"] == "patch":
                call = {"method": "PATCH", "path": f"/accounts/{action['account_id']}",
                        "body": fields, "fingerprint": p["fingerprint"]}
                summary["calls"].append(call)
                if dry_run or plan_out:
                    results.append({"op": "patch", "planned": True})
                    continue
                try:
                    client.update_account(action["account_id"], fields)
                    results.append({"op": "patch", "account_id": action["account_id"]})
                except Exception as e:  # noqa: BLE001
                    ok = False
                    results.append({"op": "patch", "account_id": action["account_id"],
                                    "error": str(e)})
                    break
            else:
                ok = False
                results.append({"error": f"unknown op {action['op']}"})
                break

        if dry_run or plan_out:
            continue
        st.mark_applied(p["fingerprint"], results, ok)
        summary["applied" if ok else "failed"] += 1

    if plan_out:
        with open(plan_out, "w", encoding="utf-8") as fh:
            json.dump(summary["calls"], fh, indent=2)
    return summary


def ingest_apply_results(st: store.Store, results_path: str) -> dict:
    """Record the outcome of a plan that was executed elsewhere.

    Expects a JSON list of {fingerprint, ref?, ok, account_id?, error?} in the
    order the calls were made.
    """
    with open(results_path, encoding="utf-8") as fh:
        rows = json.load(fh)
    per_fp: dict[str, list[dict]] = {}
    for r in rows:
        per_fp.setdefault(r["fingerprint"], []).append(r)
        if r.get("ok") and r.get("idempotency_key") and r.get("account_id"):
            st.record_created(r["idempotency_key"], r["account_id"], r["fingerprint"])
    out = {"applied": 0, "failed": 0}
    for fp, rs in per_fp.items():
        ok = all(r.get("ok") for r in rs)
        st.mark_applied(fp, rs, ok)
        out["applied" if ok else "failed"] += 1
    return out
