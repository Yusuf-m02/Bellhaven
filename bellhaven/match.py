"""Match website locations to CRM accounts.

Design notes
------------
*   Address is the only identity that survives a rebrand, so it carries the most
    weight. Name similarity is a supporting signal, never a sufficient one under
    the conservative profile.
*   Assignment is greedy and global: every account belongs to at most one
    website location, but a location may collect several accounts -- that is how
    duplicates surface.
*   Every score carries a human-readable `evidence` list. Nothing reaches the
    review queue that a reviewer cannot audit in ten seconds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import normalize as N

# Website care-offering vocabulary -> CRM care_type picklist.
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
    "rehabilitation": "Skilled Nursing",
}


def map_care(offerings: list[str]) -> list[str]:
    out = []
    for o in offerings or []:
        v = CARE_MAP.get(N.basic(o).replace(" and ", " & "))
        if v is None:
            v = CARE_MAP.get(N.basic(o))
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class Candidate:
    account_id: str
    score: int
    evidence: list[str] = field(default_factory=list)

    def add(self, score: int, why: str) -> None:
        if score > self.score:
            self.score = score
        self.evidence.append(why)


def _acct_addr_keys(a: dict) -> tuple[str, str]:
    return (
        N.address_key(a.get("billing_street", ""), a.get("billing_zip", ""),
                      a.get("billing_city", ""), a.get("billing_state", "")),
        N.loose_address_key(a.get("billing_street", ""), a.get("billing_zip", ""),
                            a.get("billing_city", ""), a.get("billing_state", "")),
    )


def _loc_addr_keys(loc: dict) -> tuple[str, str]:
    return (
        N.address_key(loc.get("street", ""), loc.get("zip", ""),
                      loc.get("city", ""), loc.get("state", "")),
        N.loose_address_key(loc.get("street", ""), loc.get("zip", ""),
                            loc.get("city", ""), loc.get("state", "")),
    )


def score_pair(loc: dict, acct: dict) -> Candidate:
    """Score one (website location, CRM account) pair."""
    c = Candidate(acct["account_id"], 0)

    l_exact, l_loose = _loc_addr_keys(loc)
    a_exact, a_loose = _acct_addr_keys(acct)

    same_state = N.state(loc.get("state", "")) == N.state(acct.get("billing_state", ""))
    same_city = N.city(loc.get("city", "")) == N.city(acct.get("billing_city", ""))
    sim = N.name_similarity(loc.get("name", ""), acct.get("name", ""))
    core_eq = (N.geo_core(loc.get("name", "")) != ""
               and N.geo_core(loc.get("name", "")) == N.geo_core(acct.get("name", "")))

    if l_exact and l_exact == a_exact:
        c.add(100, f"street+ZIP identical after normalisation ({l_exact.replace('|', ' / ')})")
    elif l_loose and l_loose == a_loose:
        c.add(92, f"same street number and street on the same ZIP "
                  f"({acct.get('billing_street','')} vs {loc.get('street','')})")

    if same_state and same_city:
        if N.name(loc.get("name", "")) == N.name(acct.get("name", "")):
            c.add(80, "exact name match in the same city")
        elif core_eq:
            c.add(74, f"same city and the same location core in the name "
                      f"('{N.geo_core(loc.get('name',''))}')")
        elif sim >= 0.86:
            c.add(70, f"same city, name similarity {sim:.2f}")
        elif sim >= 0.62:
            c.add(int(50 + 25 * sim), f"same city, weak name similarity {sim:.2f}")

    if not same_state and c.score > 40:
        c.score = 40
        c.evidence.append(f"state differs ({acct.get('billing_state','?')} vs "
                          f"{loc.get('state','?')}) - score capped")

    # Supporting-only signals: recorded for the reviewer, never scored on their own.
    crm_care = acct.get("care_type", "")
    site_care = map_care(loc.get("care_offerings", []))
    if crm_care and site_care:
        c.evidence.append(
            f"care type {'agrees' if crm_care in site_care else 'DISAGREES'} "
            f"(CRM '{crm_care}' vs site {site_care})")
    if acct.get("phone") and loc.get("phone"):
        digits = lambda s: "".join(ch for ch in s if ch.isdigit())
        if digits(acct["phone"]) == digits(loc["phone"]):
            c.evidence.append("phone number matches the site listing")
    return c


# Two accounts are only ever called duplicates of each other when they agree at
# *address* level. Two facilities in the same town with similar names are a
# coincidence, not a duplicate -- that distinction is what keeps "Maplewood
# Senior Care Center" (a competitor's building) out of Bellhaven's roll-up.
ADDRESS_AGREEMENT = 92


def is_resolved(acct: dict) -> bool:
    """True when a previous decision already retired this record.

    A record that has been pointed at a successor (chow_current_account) or
    marked an inactive duplicate is no longer a live candidate. Excluding these
    is what keeps daily runs stable: without it, the account a CHOW *created*
    and the account it preserved would look like a fresh duplicate pair on the
    next run, and the pipeline would propose undoing its own work.
    """
    if (acct.get("chow_current_account") or "").strip():
        return True
    if (acct.get("duplicate_of_account") or "").strip() and acct.get("status") == "Inactive":
        return True
    return False


def build(locations: list[dict], accounts: list[dict], *, confident_at: int,
          review_at: int, vetoes: set[tuple[str, str]] | None = None) -> dict:
    """Return the full assignment: clusters per location, plus leftovers."""
    vetoes = vetoes or set()
    by_id = {a["account_id"]: a for a in accounts}
    # Parent (roll-up) accounts have no address and are never facilities.
    facilities = [a for a in accounts
                  if "(Parent Account)" not in a.get("name", "") and not is_resolved(a)]

    pairs = []
    for loc in locations:
        for acct in facilities:
            if (loc["slug"], acct["account_id"]) in vetoes:
                continue
            cand = score_pair(loc, acct)
            if cand.score >= review_at:
                pairs.append((cand.score, loc["slug"], cand))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2].account_id))

    taken: dict[str, str] = {}          # account_id -> slug
    clusters: dict[str, list[Candidate]] = {loc["slug"]: [] for loc in locations}
    considered: dict[str, list[Candidate]] = {loc["slug"]: [] for loc in locations}
    for score, slug, cand in pairs:
        if cand.account_id in taken:
            continue
        if score >= ADDRESS_AGREEMENT or not clusters[slug]:
            # Address agreement joins the cluster; a sub-address candidate is only
            # taken when nothing better exists for this location.
            taken[cand.account_id] = slug
            clusters[slug].append(cand)
        else:
            considered[slug].append(cand)

    for slug in clusters:
        clusters[slug].sort(key=lambda c: -c.score)

    unmatched_accounts = [a["account_id"] for a in facilities if a["account_id"] not in taken]

    return {
        "clusters": clusters,
        "considered": considered,
        "taken": taken,
        "unmatched_accounts": unmatched_accounts,
        "by_id": by_id,
        "confident_at": confident_at,
        "review_at": review_at,
    }


def classify(cluster: list[Candidate], confident_at: int) -> str:
    if not cluster:
        return "no_match"
    if len(cluster) > 1:
        return "duplicates"
    return "confident" if cluster[0].score >= confident_at else "needs_review"


def parent_tier(acct: dict, bellhaven_parent_id: str, acquired_parent_ids: set[str]) -> int:
    """How well an account's current parent fits the Bellhaven ownership story."""
    pid = acct.get("parent_id") or ""
    if pid == bellhaven_parent_id:
        return 3            # already correct
    if pid in acquired_parent_ids:
        return 2            # an operator the site says Bellhaven absorbed
    if not pid:
        return 1            # orphan - no ownership claim either way
    return 0                # sits under an unrelated operator


def pick_survivor(cluster: list[Candidate], by_id: dict, loc: dict,
                  bellhaven_parent_id: str, acquired_parent_ids: set[str]) -> str:
    """Which of several accounts for one building should stay Active?

    Priority, highest first:
      1. carries billing history (revenue or AR) - invoices and collections live
         there, and this API has no merge, so the losing copy's history is lost
         to reporting the moment it goes Inactive
      2. the most defensible parent (Bellhaven > an acquired operator > orphan)
      3. strongest match score, then closest name to the website listing
      4. account_id, so the choice is stable across runs
    """
    def key(c: Candidate):
        a = by_id[c.account_id]
        billing = (a.get("lifetime_revenue") or 0) + (a.get("outstanding_ar") or 0)
        return (
            1 if billing > 0 else 0,
            parent_tier(a, bellhaven_parent_id, acquired_parent_ids),
            c.score,
            N.name_similarity(loc.get("name", ""), a.get("name", "")),
            # invert id so that max() prefers the lexicographically smallest
            tuple(-ord(ch) for ch in a["account_id"]),
        )
    return max(cluster, key=key).account_id
