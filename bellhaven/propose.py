"""Turn a match result into reviewable, applyable proposals.

Proposal kinds
--------------
create_account   website community with no CRM account at all
reparent         re-hang an account under the Bellhaven parent (SOP-safe path)
chow             SOP change-of-ownership: preserve the old account, create a new
                 one under the correct parent, point the old at the new
chow_link        same SOP, but a correctly parented account already exists, so
                 point at that instead of creating a redundant record
update_fields    name / address / care-type corrections on a matched account
mark_duplicate   a second record for a building we already have
ambiguous_match  a plausible but unproven match a human must rule on
offsite          a Bellhaven-parented account the website no longer lists

Every proposal carries `actions`: an ordered list of API calls. `$ref:new` in a
field value is resolved at apply time to the id of the account created earlier in
the same proposal.
"""
from __future__ import annotations

from . import config, match, normalize as N, store

FIELD_LABELS = {
    "name": "Name", "parent_id": "Parent", "billing_street": "Street",
    "billing_city": "City", "billing_state": "State", "billing_zip": "ZIP",
    "care_type": "Care type", "status": "Status", "phone": "Phone",
    "chow_current_account": "CHOW pointer", "duplicate_of_account": "Duplicate of",
}

NOTE_MAX = 900


def _note(*parts: str) -> str:
    txt = " ".join(p.strip() for p in parts if p and p.strip())
    return txt[:NOTE_MAX]


def _stamp(run_id: str) -> str:
    """Deterministic note prefix.

    Deliberately excludes the run id: a note is part of a proposal's content
    fingerprint, so a timestamped note would make every proposal look new on
    every run. The CRM's own updated_at records when the write happened.
    """
    return "[crm-sync]"


def desired_fields(loc: dict, acct: dict) -> dict:
    """Field corrections implied by the website listing. Only material
    differences: '210 Orchard Ln' vs '210 Orchard Lane' is not a change."""
    out: dict[str, str] = {}
    if loc.get("name") and N.name(loc["name"]) != N.name(acct.get("name", "")):
        out["name"] = loc["name"]
    if loc.get("street") and N.street(loc["street"]) != N.street(acct.get("billing_street", "")):
        out["billing_street"] = loc["street"]
    if loc.get("city") and N.city(loc["city"]) != N.city(acct.get("billing_city", "")):
        out["billing_city"] = loc["city"]
    if loc.get("state") and N.state(loc["state"]) != N.state(acct.get("billing_state", "")):
        out["billing_state"] = loc["state"]
    if loc.get("zip") and N.zip5(loc["zip"]) != N.zip5(acct.get("billing_zip", "")):
        out["billing_zip"] = loc["zip"]
    # Care type: only correct an outright contradiction. The site lists offerings,
    # the CRM holds one picklist value, so "site offers more than the CRM says"
    # is not an error worth churning the record for.
    site_care = match.map_care(loc.get("care_offerings", []))
    if site_care and acct.get("care_type") and acct["care_type"] not in site_care:
        out["care_type"] = site_care[0]
    elif site_care and not acct.get("care_type"):
        out["care_type"] = site_care[0]
    return out


def new_account_fields(loc: dict, parent_id: str) -> dict:
    care = match.map_care(loc.get("care_offerings", []))
    return {
        "name": loc["name"],
        "parent_id": parent_id,
        "billing_street": loc.get("street", ""),
        "billing_city": loc.get("city", ""),
        "billing_state": loc.get("state", ""),
        "billing_zip": loc.get("zip", ""),
        "care_type": care[0] if care else "",
        "status": "Active",
        "phone": loc.get("phone", ""),
    }


def _diff_text(acct: dict, fields: dict) -> str:
    bits = []
    for k, v in fields.items():
        before = acct.get(k, "")
        bits.append(f"{FIELD_LABELS.get(k, k)}: '{before}' -> '{v}'")
    return "; ".join(bits)


class Proposer:
    def __init__(self, accounts: list[dict], site: dict, run_id: str):
        self.accounts = accounts
        self.by_id = {a["account_id"]: a for a in accounts}
        self.site = site
        self.run_id = run_id
        self.locs = {l["slug"]: l for l in site["locations"]}

        parents = {a["name"]: a["account_id"] for a in accounts if "(Parent Account)" in a["name"]}
        self.parent_ids = parents
        self.bellhaven_id = parents.get(config.BELLHAVEN_PARENT_NAME, "")
        if not self.bellhaven_id:
            raise RuntimeError("Bellhaven parent account not found in CRM snapshot")

        # Operators the website says Bellhaven absorbed -> their parent accounts.
        self.acquired: dict[str, str] = {}
        for acq in site.get("acquisitions", []):
            org = N.basic(acq["organization"])
            for pname, pid in parents.items():
                if N.basic(pname).startswith(org):
                    self.acquired[pid] = acq["quote"]
        self.acquired_ids = set(self.acquired)

        self.address_index: dict[str, list[dict]] = {}
        for a in accounts:
            key = N.address_key(a.get("billing_street", ""), a.get("billing_zip", ""),
                                a.get("billing_city", ""), a.get("billing_state", ""))
            if key:
                self.address_index.setdefault(key, []).append(a)

        self.proposals: list[dict] = []
        # Accounts corroborated by a site announcement rather than the
        # directory. They are legitimately absent from /communities, so the
        # off-site pass must not flag them.
        self.announced_ids: set[str] = set()

    # ------------------------------------------------------------- helpers ---
    def _parent_label(self, pid: str) -> str:
        a = self.by_id.get(pid)
        return a["name"] if a else ("(none)" if not pid else pid)

    def _add(self, kind, title, rationale, severity, actions, evidence,
             location_slug=None, account_id=None) -> dict:
        loc = self.locs.get(location_slug or "", {})
        acct = self.by_id.get(account_id or "", {})
        p = {
            "kind": kind, "title": title, "rationale": rationale, "severity": severity,
            "actions": actions, "evidence": evidence,
            "location_slug": location_slug, "location_name": loc.get("name"),
            "account_id": account_id, "account_name": acct.get("name"),
        }
        p["fingerprint"] = store.fingerprint(kind, location_slug or "", account_id or "", actions)
        self.proposals.append(p)
        return p

    def _acquisition_evidence(self, acct: dict) -> list[str]:
        pid = acct.get("parent_id") or ""
        if pid in self.acquired:
            return [f"About page: \"{self.acquired[pid]}\""]
        if pid and pid != self.bellhaven_id:
            return [f"No acquisition of {self._parent_label(pid)} is described on the "
                    f"Bellhaven site - re-parenting rests on the address/name match alone."]
        return []

    # ------------------------------------------------------------ SOP logic ---
    def sop_requires_chow(self, acct: dict) -> bool:
        """The one hard rule: revenue history AND open AR means billing keeps the
        old account, so its parent must not move."""
        return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0

    def _move_parent(self, loc: dict, acct: dict, target_parent_id: str,
                     cluster_ids: list[str], base_evidence: list[str]) -> None:
        """Emit the right proposal for moving `acct` under `target_parent_id`,
        honouring the CHOW SOP."""
        aid = acct["account_id"]
        target_label = self._parent_label(target_parent_id)
        from_label = self._parent_label(acct.get("parent_id") or "")
        money = (f"lifetime_revenue ${acct.get('lifetime_revenue', 0):,} / "
                 f"outstanding_ar ${acct.get('outstanding_ar', 0):,}")

        if not self.sop_requires_chow(acct):
            why = ("no revenue history" if not (acct.get("lifetime_revenue") or 0)
                   else "no outstanding AR")
            fields = {"parent_id": target_parent_id}
            fields.update(desired_fields(loc, acct) if loc else {})
            fields["note"] = _note(
                _stamp(self.run_id), f"Re-parented from {from_label} to {target_label}.",
                f"SOP check: {money} - {why}, so the existing account moves directly.")
            self._add(
                "reparent",
                f"Re-parent '{acct['name']}' to {target_label}",
                f"The website lists this building as a Bellhaven community, but the CRM "
                f"still hangs it off {from_label}. SOP check passes ({money}: {why}), so "
                f"the existing account can move.",
                "confident",
                [{"op": "patch", "account_id": aid, "fields": fields}],
                base_evidence + [f"SOP: {money} -> direct re-parent allowed"],
                location_slug=loc.get("slug") if loc else None, account_id=aid)
            return

        # --- SOP: preserve the old account -----------------------------------
        # Is there already a correctly parented account for this same building?
        existing = [self.by_id[i] for i in cluster_ids
                    if i != aid and self.by_id[i].get("parent_id") == target_parent_id]
        if not existing and loc.get("name"):
            for cand in self.address_index.get(
                    N.address_key(loc.get("street", ""), loc.get("zip", ""),
                                  loc.get("city", ""), loc.get("state", "")), []):
                if cand["account_id"] != aid and cand.get("parent_id") == target_parent_id:
                    existing.append(cand)

        if existing:
            survivor = existing[0]
            note = _note(
                _stamp(self.run_id),
                f"CHOW: this facility now sits under {target_label}.",
                f"SOP: {money} - revenue history and open AR, so this account is preserved "
                f"as-is for billing and its parent is NOT changed.",
                f"Current relationship lives on account {survivor['account_id']} "
                f"({survivor['name']}).")
            self._add(
                "chow_link",
                f"CHOW pointer: '{acct['name']}' -> existing '{survivor['name']}'",
                f"This building has moved to {target_label}. The SOP forbids re-parenting "
                f"because {money}, and a correctly parented account already exists at the "
                f"same address, so we point at it rather than creating a redundant record. "
                f"Nothing else on the old account changes.",
                "review",
                [{"op": "patch", "account_id": aid,
                  "fields": {"chow_current_account": survivor["account_id"], "note": note}}],
                base_evidence + [
                    f"SOP: {money} -> revenue AND AR both positive: parent must NOT change",
                    f"Existing account under {target_label} at the same address: "
                    f"{survivor['account_id']} ({survivor['name']})"],
                location_slug=loc.get("slug") if loc else None, account_id=aid)
            return

        if not loc.get("name"):
            # No website listing to build a new record from (this is the
            # divestiture path). Nothing safe to do automatically.
            self._add(
                "offsite",
                f"Manual CHOW needed for '{acct['name']}'",
                f"This account should move to {target_label}, the SOP forbids re-parenting "
                f"({money}), and there is no correctly parented account to point at and no "
                f"website listing to build one from. Flagging for a human.",
                "review",
                [{"op": "patch", "account_id": aid,
                  "fields": {"status": "Needs Review",
                             "note": _note(_stamp(self.run_id),
                                           f"Should move to {target_label}; SOP blocks "
                                           f"re-parenting ({money}). Needs a manual CHOW.")}}],
                base_evidence, account_id=aid)
            return

        # Create a fresh account under the correct parent and point the old at it.
        fields = new_account_fields(loc, target_parent_id)
        fields["note"] = _note(
            _stamp(self.run_id),
            f"Created by CHOW from account {aid} ({acct['name']}, previously under "
            f"{from_label}). The prior account is preserved for billing "
            f"({money}) per SOP.")
        old_note = _note(
            _stamp(self.run_id),
            f"CHOW: facility moved from {from_label} to {target_label}.",
            f"SOP: {money} - revenue history and open AR, so this account is left exactly "
            f"as-is (parent unchanged) and chow_current_account points to the new account.")
        self._add(
            "chow",
            f"CHOW: preserve '{acct['name']}', create new account under {target_label}",
            f"The website lists this building as a Bellhaven community but the CRM has it "
            f"under {from_label}. It has {money} - revenue history AND open AR - so the SOP "
            f"forbids moving it. Instead: create a new account under {target_label} and set "
            f"chow_current_account on the old one. The old account is otherwise untouched.",
            "confident",
            [
                {"op": "create", "ref": "new", "fields": fields,
                 "idempotency_key": f"chow:{aid}:{target_parent_id}"},
                {"op": "patch", "account_id": aid,
                 "fields": {"chow_current_account": "$ref:new", "note": old_note}},
            ],
            base_evidence + [
                f"SOP: {money} -> revenue AND AR both positive: parent must NOT change",
                "No correctly parented account exists at this address, so a new one is created"],
            location_slug=loc.get("slug") if loc else None, account_id=aid)

    # -------------------------------------------------------------- passes ---
    def run(self, result: dict) -> list[dict]:
        self._locations_pass(result)
        self._announcements_pass(result)
        self._offsite_pass(result)
        return self.proposals

    def _locations_pass(self, result: dict) -> None:
        for slug, cluster in result["clusters"].items():
            loc = self.locs[slug]
            kind = match.classify(cluster, result["confident_at"])
            considered = result["considered"].get(slug, [])

            if kind == "no_match":
                near = [f"{self.by_id[c.account_id]['name']} (score {c.score})"
                        for c in considered[:3]]
                fields = new_account_fields(loc, self.bellhaven_id)
                fields["note"] = _note(
                    _stamp(self.run_id),
                    f"Created from the Bellhaven website listing {loc['source_url']}; "
                    f"no CRM account matched this address.")
                self._add(
                    "create_account",
                    f"Create account for '{loc['name']}'",
                    f"{loc['name']} is listed on the Bellhaven website at "
                    f"{loc.get('street','')}, {loc.get('city','')} {loc.get('state','')} "
                    f"{loc.get('zip','')}, and no CRM account matches that address"
                    + (f". Closest candidates were rejected: {', '.join(near)}." if near else "."),
                    "confident",
                    [{"op": "create", "ref": "new", "fields": fields,
                      "idempotency_key": f"site:{slug}"}],
                    [f"Website listing: {loc['source_url']}",
                     "No CRM account within the match threshold at this address"]
                    + ([f"Considered and rejected: {n}" for n in near]),
                    location_slug=slug)
                continue

            if kind == "needs_review":
                c = cluster[0]
                acct = self.by_id[c.account_id]
                fields = desired_fields(loc, acct)
                if acct.get("parent_id") != self.bellhaven_id:
                    fields["parent_id"] = self.bellhaven_id
                fields["note"] = _note(
                    _stamp(self.run_id),
                    f"Reconciled against website listing {loc['source_url']} "
                    f"(match score {c.score}, below the auto-confident threshold).")
                self._add(
                    "ambiguous_match",
                    f"Probable match: '{acct['name']}' = '{loc['name']}' (score {c.score})",
                    f"Scored {c.score}, under the confident threshold of "
                    f"{result['confident_at']}, so a human decides. Approving applies "
                    f"{_diff_text(acct, {k: v for k, v in fields.items() if k != 'note'})}. "
                    f"Rejecting records a veto, and the next run will propose creating a "
                    f"new account for this community instead.",
                    "review",
                    [{"op": "patch", "account_id": c.account_id, "fields": fields}],
                    c.evidence + [f"Website listing: {loc['source_url']}"]
                    + self._acquisition_evidence(acct),
                    location_slug=slug, account_id=c.account_id)
                continue

            # confident or duplicates
            survivor_id = (cluster[0].account_id if len(cluster) == 1 else
                           match.pick_survivor(cluster, self.by_id, loc,
                                               self.bellhaven_id, self.acquired_ids))
            survivor = self.by_id[survivor_id]
            cluster_ids = [c.account_id for c in cluster]
            ev = {c.account_id: c.evidence for c in cluster}

            # 1. duplicates lose
            for c in cluster:
                if c.account_id == survivor_id:
                    continue
                loser = self.by_id[c.account_id]
                note = _note(
                    _stamp(self.run_id),
                    f"Duplicate of {survivor_id} ({survivor['name']}) - same building at "
                    f"{loser.get('billing_street','')}, {loser.get('billing_city','')} "
                    f"{loser.get('billing_state','')}. Marked Inactive; this API has no "
                    f"merge, so history stays readable on this record.",
                    (f"Carried lifetime_revenue ${loser.get('lifetime_revenue',0):,} / "
                     f"outstanding_ar ${loser.get('outstanding_ar',0):,}."
                     if (loser.get('lifetime_revenue') or loser.get('outstanding_ar')) else ""))
                self._add(
                    "mark_duplicate",
                    (f"Mark '{loser['name']}' ({c.account_id}) a duplicate of "
                     f"'{survivor['name']}' ({survivor_id})"),
                    f"Both records point at {loser.get('billing_street','')}, "
                    f"{loser.get('billing_city','')} {loser.get('billing_state','')} "
                    f"{loser.get('billing_zip','')}, which the website lists once as "
                    f"'{loc['name']}'. Survivor {survivor_id} was chosen because it "
                    f"{self._survivor_reason(survivor, loser)}.",
                    "confident" if c.score >= match.ADDRESS_AGREEMENT else "review",
                    [{"op": "patch", "account_id": c.account_id,
                      "fields": {"duplicate_of_account": survivor_id,
                                 "status": "Inactive", "note": note}}],
                    ev[c.account_id] + [
                        f"Website lists this address once: {loc['source_url']}",
                        f"Survivor: {survivor_id} ({survivor['name']}, parent "
                        f"{self._parent_label(survivor.get('parent_id',''))})"]
                    + self._acquisition_evidence(loser),
                    location_slug=slug, account_id=c.account_id)

            # 2. survivor: parent move (SOP-aware) and/or field corrections
            base_ev = ev[survivor_id] + [f"Website listing: {loc['source_url']}"] \
                + self._acquisition_evidence(survivor)
            if survivor.get("parent_id") != self.bellhaven_id:
                self._move_parent(loc, survivor, self.bellhaven_id, cluster_ids, base_ev)
            else:
                fields = desired_fields(loc, survivor)
                if fields:
                    fields["note"] = _note(
                        _stamp(self.run_id),
                        f"Field corrections from website listing {loc['source_url']}.")
                    self._add(
                        "update_fields",
                        f"Correct '{survivor['name']}' from the website listing",
                        f"The website is the source of truth for name and address. "
                        f"{_diff_text(survivor, {k: v for k, v in fields.items() if k != 'note'})}",
                        "confident",
                        [{"op": "patch", "account_id": survivor_id, "fields": fields}],
                        base_ev, location_slug=slug, account_id=survivor_id)

    def _survivor_reason(self, survivor: dict, loser: dict) -> str:
        s_money = (survivor.get("lifetime_revenue") or 0) + (survivor.get("outstanding_ar") or 0)
        l_money = (loser.get("lifetime_revenue") or 0) + (loser.get("outstanding_ar") or 0)
        if s_money > l_money:
            return f"carries the billing history (${s_money:,} vs ${l_money:,})"
        s_tier = match.parent_tier(survivor, self.bellhaven_id, self.acquired_ids)
        l_tier = match.parent_tier(loser, self.bellhaven_id, self.acquired_ids)
        if s_tier > l_tier:
            return f"sits under {self._parent_label(survivor.get('parent_id',''))}"
        return "matches the website listing most closely (ties broken on account id)"

    def _announcements_pass(self, result: dict) -> None:
        """Communities the site mentions in prose but has not added to the
        directory. No address to match on, so name identity must be exact."""
        matched_ids = set(result["taken"])
        for name in self.site.get("announcements", []):
            best, best_sim = None, 0.0
            for a in self.accounts:
                if "(Parent Account)" in a["name"] or a["account_id"] in matched_ids:
                    continue
                sim = N.name_similarity(name, a["name"])
                if sim > best_sim:
                    best, best_sim = a, sim
            if not best or best_sim < 0.95:
                continue
            # Record the corroboration first: on later runs this account is
            # already correctly parented and returns early, but it must still be
            # exempt from the off-site sweep below.
            self.announced_ids.add(best["account_id"])
            if best.get("parent_id") == self.bellhaven_id:
                continue
            ev = [f"Home page announces this community as newly part of Bellhaven",
                  f"Exact name match to CRM account {best['account_id']} "
                  f"(similarity {best_sim:.2f})",
                  "Not yet in the /communities directory, so there is no address to "
                  "cross-check - name identity only"]
            self._move_parent({}, best, self.bellhaven_id, [best["account_id"]], ev)
            # Downgrade: no address corroboration, so a human should look.
            self.proposals[-1]["severity"] = "review"
            self.proposals[-1]["location_name"] = name

    def _offsite_pass(self, result: dict) -> None:
        """Accounts under the Bellhaven parent that the website no longer lists."""
        for aid in result["unmatched_accounts"]:
            acct = self.by_id[aid]
            if acct.get("parent_id") != self.bellhaven_id:
                continue
            if acct.get("status") in ("Inactive", "Needs Review"):
                continue  # already retired or already flagged by an earlier run
            if match.is_resolved(acct):
                continue
            if aid in self.announced_ids:
                continue  # the site announces it, it just is not in the directory yet

            key = N.address_key(acct.get("billing_street", ""), acct.get("billing_zip", ""),
                                acct.get("billing_city", ""), acct.get("billing_state", ""))
            rivals = [x for x in self.address_index.get(key, [])
                      if x["account_id"] != aid and x.get("parent_id")
                      and x.get("parent_id") != self.bellhaven_id]

            if rivals:
                rival = rivals[0]
                ev = [f"Not present in the website directory ({len(self.locs)} communities "
                      f"scraped)",
                      f"Another operator has an account at the same address: "
                      f"{rival['account_id']} ({rival['name']}) under "
                      f"{self._parent_label(rival.get('parent_id',''))}",
                      "Read as a divestiture: the building left the Bellhaven portfolio"]
                self._move_parent({}, acct, rival["parent_id"], [aid, rival["account_id"]], ev)
                self.proposals[-1]["severity"] = "review"
                continue

            note = _note(
                _stamp(self.run_id),
                f"Not listed on the Bellhaven website as of this run "
                f"({self.site.get('directory_total')} communities in the directory). "
                f"Flagged for a human to confirm whether it was sold, closed or simply "
                f"never published. Left under the Bellhaven parent pending that decision.",
                (f"Note: carries lifetime_revenue ${acct.get('lifetime_revenue',0):,} / "
                 f"outstanding_ar ${acct.get('outstanding_ar',0):,}."
                 if (acct.get('lifetime_revenue') or acct.get('outstanding_ar')) else ""))
            self._add(
                "offsite",
                f"Flag '{acct['name']}' - under Bellhaven but not on the website",
                f"This account hangs off the Bellhaven parent but no community on the "
                f"website matches it. That is a signal, not proof - it could be sold, "
                f"closed, or just unpublished - so this only sets status to 'Needs Review' "
                f"and records why. Nothing about the parent or the billing fields changes.",
                "review",
                [{"op": "patch", "account_id": aid,
                  "fields": {"status": "Needs Review", "note": note}}],
                [f"No website community matched this address or name",
                 f"CRM address: {acct.get('billing_street','')}, "
                 f"{acct.get('billing_city','')} {acct.get('billing_state','')} "
                 f"{acct.get('billing_zip','')}",
                 f"lifetime_revenue ${acct.get('lifetime_revenue',0):,} / "
                 f"outstanding_ar ${acct.get('outstanding_ar',0):,}"],
                account_id=aid)
