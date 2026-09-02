# Bellhaven CRM reconciliation — writeup

**Time spent:** ~2h10m end to end (analysis, build, run, review, this document).

**End state:** 30 changes applied to the CRM across two runs. 29 proposals
approved, 1 rejected, 1 auto-retired. The review queue is empty and a third run
produces zero proposals.

---

## What the data actually contained

Before writing matching code I read the CRM and the site. The shape of the mess
determined the design, so it is worth stating:

* The website lists **34 communities** in its directory and claims 35. The 35th,
  **Bellhaven Meadows of Findlay**, appears only in a home-page sentence — never
  in the directory. Its CRM account existed with **no parent at all**.
* The About page carries the ownership narrative: *"In 2025 we welcomed the
  Harborview Care Group family of communities, and in 2026 we expanded further
  with select communities joining us from Cedar Trail."* Both operators exist as
  parent accounts in the CRM with children still hanging off them. That sentence
  is the evidence that justifies re-parenting, and the pipeline scrapes and
  quotes it rather than hard-coding the operator names.
* **Six buildings had more than one CRM record.** 750 Stewart Rd, Monroe had
  three (Bellhaven, Harborview and Cedar Trail each had a record for it). 3313
  Wilmington Pike, Kettering had three. Owosso had two records with identical
  addresses and identical names, differing only in "West Main Street" vs
  "W Main St".
* **47 accounts have revenue history; only 11 have open AR.** That gap is what
  the SOP turns on, and it is why the number of true CHOW cases is small.

## How matching works

Three layers, in decreasing weight.

**1. Address.** The only identity that survives a rebrand. Street lines are
normalised USPS-style (`4930 West Lake Road` → `4930 w lake rd`), and matched on
`street + ZIP`. A looser key — house number + first six characters of the street
name + ZIP — catches `Wilmington Pike` vs `Wilmington Pk`, which suffix
normalisation alone misses. Exact address agreement scores 100, loose 92.

**2. Name.** Names are decomposed into a *geo core*: strip operator brands
(bellhaven, harborview, cedar trail, …) and care-type nouns (nursing, rehab,
gardens, terrace, care center, …) and what remains is the thing that identifies
the location. `Harborview Nursing & Rehab of Port Clinton` and `Bellhaven of
Port Clinton` both reduce to `port clinton`. Name agreement in the same city
scores 74–80 — enough to reach the queue, never enough to auto-apply.

**3. Everything else is evidence, not score.** Care type and phone are recorded
on the proposal for the reviewer but never move the number. A care-type
disagreement is printed in capitals so it is impossible to miss.

**The conservative profile** sets the auto-confident bar at 90, so only
address-level agreement is ever "confident". Everything else lands in the queue
with its evidence. Under this profile 26 of 34 locations matched confidently.

**Clustering.** Two accounts are called duplicates only when they agree at
*address* level (≥92). This matters: `Maplewood Senior Care Center` (a
Stonebridge facility) scores 74 against `Bellhaven of Maplewood` — same town,
same geo core — and a naive clusterer would mark a competitor's building as a
Bellhaven duplicate. It is recorded as "considered and rejected" instead.

**Survivor selection** when a building has several records, in priority order:
billing history first (invoices and AR live there, and this API has no merge, so
whatever goes Inactive is effectively lost to reporting), then the most
defensible parent (Bellhaven > an acquired operator > orphan > unrelated), then
match score, then name closeness, then account id so the choice is stable across
runs.

## The CHOW SOP

The rule: *revenue history **AND** outstanding AR > 0 → do not change the parent.*

Three accounts needed to change parent while carrying money. Two of them had both
revenue and AR:

| Account | Revenue | AR | Handling |
|---|---|---|---|
| Bellhaven of Marietta (under Cedar Trail) | $51,250 | $3,800 | **CHOW** |
| Bellhaven of Tiffin (under Cedar Trail) | $84,000 | $12,400 | **CHOW** |
| Bellhaven Crossings of Lima (under Harborview) | $47,000 | **$0** | direct re-parent |

Lima is the case that proves the rule is read correctly: it has real revenue, but
AR is zero, so the SOP does **not** apply and the existing account moves. The
proposal states the check and its result in words, so a reviewer can verify the
reasoning without reading code.

For Marietta and Tiffin the pipeline creates a new account under the Bellhaven
parent, then sets `chow_current_account` on the old one. The old record's PATCH
contains **only** `chow_current_account` and `note` — no parent, no status, no
address. That is enforced and unit-tested (`test_revenue_and_ar_triggers_chow_and_never_touches_parent`).

### The judgment call: Sandusky

`Bellhaven of Sandusky` (rev $130,000, AR $5,200) is **not** on the website, and
`Millstone Care of Sandusky` sits at the identical address under a different
parent. Read together those two facts say Bellhaven sold the building to
Millstone.

So this account needs to move to a different parent, which puts it squarely under
the SOP — and it has both revenue and AR, so its parent must not move. But the
SOP's remedy ("create a new account under the correct parent") would create a
*second* Millstone record for a building Millstone already has. I pointed
`chow_current_account` at the existing Millstone account instead, and changed
nothing else on the old record: same parent, same status, same balances. The SOP's
purpose is to preserve the billing account and leave a pointer to where the live
relationship went; creating a redundant record would satisfy its letter and
damage the data. The pipeline implements this as its own proposal kind
(`chow_link`) so the reviewer sees the difference and can say no.

I marked this one **review**, not confident. It is an inference from an absence.

## Duplicates

Seven records marked `duplicate_of_account` + Inactive, with a note naming the
survivor and the shared address:

| Loser | Survivor | Why |
|---|---|---|
| Cedar Trail of Monroe | Bellhaven Gardens of Monroe | survivor already under Bellhaven |
| Monroe Gardens Care Center | Bellhaven Gardens of Monroe | same |
| Harborview Shores of Erie | Bellhaven Shores of Erie | same |
| Harborview Nursing & Rehab of Port Clinton | Bellhaven of Port Clinton | same |
| Kettering Nursing & Rehabilitation | Kettering Care Centre → renamed *Bellhaven of Kettering* | orphan loses to an acquired-operator record |
| Kettering Senior Campus | same | tie on parent tier, broken on account id |
| Bellhaven of Owosso (`001QU1…`) | Bellhaven of Owosso (`001EGU…`) | identical; address formatting broke the tie |

None of the losers carried revenue or AR, so nothing was lost by inactivating
them. If one had, the survivor rule would have flipped to keep it — and the note
records the balances either way, since there is no merge in this API and the
history has to stay readable somewhere.

## The rejected match

`Bellhaven at Union Square` (118 Union Square Dr, New Albany OH) scored 74
against `Union Square Senior Living` (240 Market St, New Albany OH, Juniper Point
Healthcare). Same town, same geo core, **different street** and a competitor's
parent. Same town plus a similar name is not evidence of the same building, so I
rejected it.

This is the case that demonstrates the re-run behaviour. Rejecting a match
records a **veto** on that (location, account) pair. On the next run the matcher
skips the pair, the location has no candidate at all, and the pipeline proposes
creating a new account — which I then approved. The system converged on the right
answer through a human "no" rather than needing me to edit the matcher.

## Off-site accounts

Two accounts sit under Bellhaven with no website match and no rival at their
address: **Bellhaven Care Center of Alliance** and **Bellhaven of Coldwater**.
Absence from a website is weak evidence — it could be a sale, a closure, or a
marketing oversight. Both were set to `Needs Review` with a note explaining what
was and was not observed. Parent, status of the billing fields, and balances were
left alone. That is the honest action: flag for a human, don't guess.

Findlay is deliberately exempt: the home page announces it, so it is legitimately
absent from the directory. The first version of the pipeline flagged it as
off-site right after re-parenting it — a real bug, caught by re-running, and fixed
by having the announcement pass record its corroboration before the off-site
sweep runs.

## Re-run safety

Four mechanisms, all tested:

1. **Content fingerprints.** A proposal's id is a hash of `(kind, location,
   account, actions)`. The same suggested change always produces the same id, and
   a fingerprint that already carries a decision is never re-raised. Notes are
   deliberately *not* timestamped — a run id in a note would change the
   fingerprint every day and re-propose everything.
2. **Resolved records leave the pool.** Any account with `chow_current_account`
   set, or `duplicate_of_account` + Inactive, is excluded from matching. Without
   this the account a CHOW *created* and the account it preserved would look like
   a fresh duplicate pair the next morning, and the pipeline would propose undoing
   its own work.
3. **Idempotency keys on creates.** Every create carries a stable key
   (`site:<slug>`, `chow:<old-id>:<parent-id>`) recorded against the resulting
   account id, so a crash between "create" and "record" cannot produce two
   accounts.
4. **Stale proposals retire themselves.** A pending proposal not regenerated by
   the current run is marked `obsolete` — someone fixed it by hand, or the site
   changed. It does not linger in the queue pretending to be actionable.

Observed convergence: run 1 → 29 proposals; run 2 → 2 (the vetoed Union Square
create, plus the Findlay bug); run 3, after the fix → **0**.

## Verified end state

Read back from the live CRM after applying:

* **36 active accounts under the Bellhaven parent** = 34 directory communities +
  Findlay + Sandusky (preserved under Bellhaven by the SOP).
* **Harborview Care Group: 0 active children.** Fully absorbed.
* **Cedar Trail: 2 active children** — the preserved Marietta and Tiffin billing
  records, exactly as the SOP requires.
* **3 CHOW pointers**, all resolving to an account under the correct parent.
* **7 inactive duplicates**, each pointing at its survivor.
* **6 accounts created** (4 website communities with no CRM record, 2 CHOW
  successors).
* **31 accounts carry a `[crm-sync]` note** explaining what changed and why.

## What I would do next

* **Contacts are untouched.** 67 exist and the API exposes them, but when a
  duplicate goes Inactive its contacts go with it. Real deployment needs to
  re-point contacts at the survivor before inactivating.
* **Street-level geocoding** would replace my hand-rolled normalisation and make
  the Union Square judgment call automatic rather than human.
* **Change history on the website.** The scraper sees one day's snapshot. Keeping
  a history of listings would let the system distinguish "delisted last night"
  from "never listed", which is exactly the ambiguity that made Alliance and
  Coldwater un-decidable.
* **Alerting on queue age**, not just queue depth. A proposal pending for a week
  is a process failure, and the schedule currently only reports counts.

## How this was run

The build environment could not reach the Railway host (egress-restricted), so
the writes were executed through a browser on the operator's machine using
`run.py apply --plan-out`, and the outcomes recorded back with `run.py
ingest-results`. Same approved proposals, same resolved API calls, same
idempotency keys — only the transport differed. On a machine with direct network
access, `python run.py review` → "Apply approved to CRM" does the whole thing in
one click, and `run.py apply` does it from the shell.

That split turned out to be worth keeping. Approvals and production network
access frequently live in different places in real sales-ops environments, and a
reviewable, replayable plan file is a better artifact than a process that must
hold both at once.
