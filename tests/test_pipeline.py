"""Unit tests: `python -m unittest discover -s tests -v` (stdlib only)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bellhaven import match, normalize as N, propose, scraper, store  # noqa: E402

BELL = "PARENT_BELL"
CEDAR = "PARENT_CEDAR"


def acct(aid, name, street, city, state, zipc, parent=BELL, rev=0, ar=0, **kw):
    d = {"account_id": aid, "name": name, "billing_street": street, "billing_city": city,
         "billing_state": state, "billing_zip": zipc, "parent_id": parent,
         "parent_name": "", "care_type": "Skilled Nursing", "status": "Active",
         "phone": "", "lifetime_revenue": rev, "outstanding_ar": ar,
         "chow_current_account": "", "duplicate_of_account": "", "note": ""}
    d.update(kw)
    return d


def loc(slug, name, street, city, state, zipc, care=("Assisted Living",)):
    return {"slug": slug, "name": name, "street": street, "city": city, "state": state,
            "zip": zipc, "care_offerings": list(care), "phone": "", "administrator": "",
            "source_url": f"http://site/communities/{slug}", "source": "directory"}


class TestNormalize(unittest.TestCase):
    def test_street_abbreviations(self):
        self.assertEqual(N.street("4930 West Lake Road"), N.street("4930 W Lake Rd"))
        self.assertEqual(N.street("1420 Harbor Point Drive"), N.street("1420 Harbor Point Dr"))
        self.assertEqual(N.street("1125 Logan Boulevard"), N.street("1125 Logan Blvd"))

    def test_loose_key_survives_suffix_noise(self):
        a = N.loose_address_key("3313 Wilmington Pike", "45429", "Kettering", "OH")
        b = N.loose_address_key("3313 Wilmington Pk", "45429", "Kettering", "OH")
        self.assertEqual(a, b)

    def test_po_box_has_no_address_key(self):
        self.assertEqual(N.address_key("PO Box 517", "44004", "Ashtabula", "OH"), "")

    def test_geo_core_strips_brand_and_care_words(self):
        self.assertEqual(N.geo_core("Harborview Nursing & Rehab of Port Clinton"),
                         N.geo_core("Bellhaven of Port Clinton"))
        self.assertEqual(N.geo_core("Cedar Trail of Zanesville"),
                         N.geo_core("Bellhaven of Zanesville"))

    def test_name_similarity_is_symmetric_and_bounded(self):
        s = N.name_similarity("Bellhaven of Marion", "Bellhaven of Marion")
        self.assertEqual(s, 1.0)
        self.assertEqual(N.name_similarity("a b", "b a"), N.name_similarity("b a", "a b"))


class TestScraper(unittest.TestCase):
    HTML = """<html><body><div class="wrap"><h1>Bellhaven of Maplewood</h1>
    <dl class="detail">
    <dt>Address</dt><dd>210 Orchard Lane<br>Maplewood, OH 44280</dd>
    <dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span>
      <span class="badge">Memory Support</span></dd>
    <dt>Administrator</dt><dd>Nadia Duval</dd>
    <dt>Phone</dt><dd>(614) 250-9447</dd></dl></div></body></html>"""

    def test_parse_community(self):
        l = scraper.parse_community("bellhaven-of-maplewood", self.HTML)
        self.assertEqual(l.name, "Bellhaven of Maplewood")
        self.assertEqual(l.street, "210 Orchard Lane")
        self.assertEqual((l.city, l.state, l.zip), ("Maplewood", "OH", "44280"))
        self.assertEqual(l.care_offerings, ["Assisted Living", "Memory Support"])
        self.assertEqual(l.phone, "(614) 250-9447")

    def test_announcement_only_from_event_sentences(self):
        page = ("<p>Bellhaven Senior Living Home Our Communities About Us</p>"
                "<p>New this year: we're delighted to welcome Bellhaven Meadows of Findlay "
                "to the Bellhaven family.</p>")
        self.assertEqual(scraper.parse_announcements(page, set()),
                         ["Bellhaven Meadows of Findlay"])

    def test_acquisitions(self):
        page = "<p>In 2025 we welcomed the Harborview Care Group family of communities.</p>"
        got = scraper.parse_acquisitions(page)
        self.assertEqual([g["organization"] for g in got], ["Harborview Care Group"])

    def test_care_mapping(self):
        self.assertEqual(match.map_care(["Short-Term Rehabilitation & Nursing"]),
                         ["Skilled Nursing"])
        self.assertEqual(match.map_care(["Memory Support"]), ["Memory Care"])


class TestMatching(unittest.TestCase):
    def test_address_beats_rebrand(self):
        l = loc("erie", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505")
        a = acct("A", "Harborview Shores of Erie", "4930 West Lake Road", "Erie", "PA", "16505")
        self.assertEqual(match.score_pair(l, a).score, 100)

    def test_same_town_different_building_is_not_a_duplicate(self):
        l = loc("mw", "Bellhaven of Maplewood", "210 Orchard Ln", "Maplewood", "OH", "44280")
        good = acct("A", "Bellhaven of Maplewood", "210 Orchard Lane", "Maplewood", "OH", "44280")
        rival = acct("B", "Maplewood Senior Care Center", "9 Other St", "Maplewood", "OH", "44280")
        r = match.build([l], [good, rival], confident_at=90, review_at=62)
        self.assertEqual([c.account_id for c in r["clusters"]["mw"]], ["A"])
        self.assertEqual([c.account_id for c in r["considered"]["mw"]], ["B"])

    def test_state_mismatch_is_capped(self):
        l = loc("am", "Amberly Manor", "4390 Darrow Rd", "Hudson", "OH", "44236")
        a = acct("A", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "CO", "80903")
        self.assertLess(match.score_pair(l, a).score, 62)

    def test_veto_removes_a_pairing(self):
        l = loc("us", "Bellhaven at Union Square", "118 Union Square Dr", "New Albany", "OH", "43054")
        a = acct("A", "Union Square Senior Living", "240 Market St", "New Albany", "OH", "43054")
        r = match.build([l], [a], confident_at=90, review_at=62)
        self.assertTrue(r["clusters"]["us"])
        r2 = match.build([l], [a], confident_at=90, review_at=62, vetoes={("us", "A")})
        self.assertFalse(r2["clusters"]["us"])

    def test_resolved_accounts_leave_the_pool(self):
        self.assertTrue(match.is_resolved(acct("A", "x", "", "", "", "", chow_current_account="B")))
        self.assertTrue(match.is_resolved(
            acct("A", "x", "", "", "", "", duplicate_of_account="B", status="Inactive")))
        self.assertFalse(match.is_resolved(
            acct("A", "x", "", "", "", "", duplicate_of_account="B", status="Active")))

    def test_survivor_prefers_billing_history(self):
        l = loc("k", "Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", "OH", "45429")
        rich = acct("B", "Kettering Care Centre", "3313 Wilmington Pike", "Kettering", "OH",
                    "45429", parent=CEDAR, rev=1000)
        poor = acct("A", "Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", "OH", "45429")
        cluster = [match.score_pair(l, rich), match.score_pair(l, poor)]
        by = {"A": poor, "B": rich}
        self.assertEqual(match.pick_survivor(cluster, by, l, BELL, {CEDAR}), "B")


SITE = {"locations": [], "announcements": [], "acquisitions": [
    {"organization": "Cedar Trail", "quote": "communities joining us from Cedar Trail."}]}


def proposer(accounts, locations=(), **site):
    s = dict(SITE, locations=list(locations), **site)
    base = [acct("PARENT_BELL", "Bellhaven Senior Living (Parent Account)", "", "", "", "", parent=""),
            acct("PARENT_CEDAR", "Cedar Trail Communities (Parent Account)", "", "", "", "", parent="")]
    return propose.Proposer(base + list(accounts), s, "runX")


class TestSOP(unittest.TestCase):
    """The one hard rule: revenue history AND open AR means the parent must not move."""

    def _one(self, rev, ar):
        l = loc("t", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
        a = acct("A", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883",
                 parent=CEDAR, rev=rev, ar=ar)
        pr = proposer([a], [l])
        r = match.build([l], pr.accounts, confident_at=90, review_at=62)
        return pr.run(r)

    def test_revenue_and_ar_triggers_chow_and_never_touches_parent(self):
        ps = self._one(84000, 12400)
        chow = [p for p in ps if p["kind"] == "chow"]
        self.assertEqual(len(chow), 1)
        ops = chow[0]["actions"]
        self.assertEqual(ops[0]["op"], "create")
        self.assertEqual(ops[0]["fields"]["parent_id"], BELL)
        patch = ops[1]
        self.assertEqual(patch["account_id"], "A")
        self.assertEqual(patch["fields"]["chow_current_account"], "$ref:new")
        self.assertNotIn("parent_id", patch["fields"])   # the SOP's whole point
        self.assertNotIn("status", patch["fields"])

    def test_revenue_but_no_ar_reparents_directly(self):
        ps = self._one(47000, 0)
        rp = [p for p in ps if p["kind"] == "reparent"]
        self.assertEqual(len(rp), 1)
        self.assertEqual(rp[0]["actions"][0]["fields"]["parent_id"], BELL)

    def test_ar_but_no_revenue_reparents_directly(self):
        ps = self._one(0, 500)
        self.assertTrue(any(p["kind"] == "reparent" for p in ps))

    def test_chow_links_to_an_existing_correct_account_instead_of_creating(self):
        l = loc("s", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870")
        old = acct("OLD", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH",
                   "44870", parent=CEDAR, rev=130000, ar=5200)
        good = acct("NEW", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870")
        pr = proposer([old, good], [l])
        r = match.build([l], pr.accounts, confident_at=90, review_at=62)
        ps = pr.run(r)
        links = [p for p in ps if p["kind"] == "chow_link"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["actions"][0]["fields"]["chow_current_account"], "NEW")
        self.assertEqual(len(links[0]["actions"]), 1)  # nothing else is touched


class TestProposals(unittest.TestCase):
    def test_missing_location_creates_under_bellhaven(self):
        l = loc("b", "Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", "OH", "45103")
        pr = proposer([], [l])
        r = match.build([l], pr.accounts, confident_at=90, review_at=62)
        ps = pr.run(r)
        self.assertEqual([p["kind"] for p in ps], ["create_account"])
        self.assertEqual(ps[0]["actions"][0]["fields"]["parent_id"], BELL)

    def test_cosmetic_address_difference_is_not_a_change(self):
        a = acct("A", "Bellhaven of Maplewood", "210 Orchard Ln", "Maplewood", "OH", "44280")
        l = loc("m", "Bellhaven of Maplewood", "210 Orchard Lane", "Maplewood", "OH", "44280")
        self.assertEqual(propose.desired_fields(l, a), {"care_type": "Assisted Living"})

    def test_offsite_only_flags_needs_review(self):
        a = acct("A", "Bellhaven of Coldwater", "90 N Michigan Ave", "Coldwater", "MI", "49036")
        pr = proposer([a], [])
        r = match.build([], pr.accounts, confident_at=90, review_at=62)
        ps = pr.run(r)
        self.assertEqual([p["kind"] for p in ps], ["offsite"])
        fields = ps[0]["actions"][0]["fields"]
        self.assertEqual(fields["status"], "Needs Review")
        self.assertEqual(set(fields) - {"status", "note"}, set())


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.st = store.Store(self.tmp.name)

    def tearDown(self):
        self.st.close()
        os.unlink(self.tmp.name)

    def _p(self, fp="fp1", kind="reparent"):
        return {"fingerprint": fp, "kind": kind, "title": "t", "rationale": "r",
                "severity": "confident", "actions": [{"op": "patch", "account_id": "A",
                                                      "fields": {"parent_id": "P"}}],
                "evidence": [], "location_slug": "s", "account_id": "A"}

    def test_fingerprint_is_content_addressed(self):
        a = store.fingerprint("reparent", "s", "A", [{"op": "patch", "fields": {"x": 1}}])
        b = store.fingerprint("reparent", "s", "A", [{"op": "patch", "fields": {"x": 1}}])
        c = store.fingerprint("reparent", "s", "A", [{"op": "patch", "fields": {"x": 2}}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_decided_proposals_are_not_resurrected(self):
        run = self.st.start_run()
        self.st.upsert_proposal(self._p(), run)
        self.st.decide("fp1", "rejected")
        self.assertEqual(self.st.upsert_proposal(self._p(), self.st.start_run()), "rejected")

    def test_applied_cannot_be_re_decided(self):
        self.st.upsert_proposal(self._p(), self.st.start_run())
        self.st.mark_applied("fp1", {}, True)
        with self.assertRaises(ValueError):
            self.st.decide("fp1", "approved")

    def test_rejecting_a_match_records_a_veto(self):
        self.st.upsert_proposal(self._p("fp2", "ambiguous_match"), self.st.start_run())
        self.st.decide("fp2", "rejected")
        self.assertIn(("s", "A"), self.st.vetoes())

    def test_rejecting_a_field_fix_does_not_veto_the_match(self):
        self.st.upsert_proposal(self._p("fp3", "update_fields"), self.st.start_run())
        self.st.decide("fp3", "rejected")
        self.assertNotIn(("s", "A"), self.st.vetoes())

    def test_stale_pending_proposals_are_retired(self):
        r1 = self.st.start_run()
        self.st.upsert_proposal(self._p(), r1)
        self.st.obsolete_stale(self.st.start_run())
        self.assertEqual(self.st.get_proposal("fp1")["status"], "obsolete")

    def test_created_accounts_are_idempotent(self):
        self.assertIsNone(self.st.created_account("k"))
        self.st.record_created("k", "001NEW", "fp1")
        self.assertEqual(self.st.created_account("k"), "001NEW")


if __name__ == "__main__":
    unittest.main()
