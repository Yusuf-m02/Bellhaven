"""Scrapes the Bellhaven public website into structured Location records.

Two sources, one parser:
  * live   -- fetch pages over HTTP and write them to data/raw/ as a cache
  * cached -- parse whatever is already in data/raw/

The cache exists so the parser can be re-run (and unit-tested) without hitting
the site, and so a failed pipeline run can be replayed against exactly the bytes
that produced it.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, asdict, field

from . import config, httpc

# ------------------------------------------------------------------- model ---


@dataclass
class Location:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""
    source_url: str = ""
    # 'directory' = listed on /communities; 'announcement' = only mentioned in
    # site copy (e.g. a brand-new community on the home page).
    source: str = "directory"

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ parsing ---

_TAG = re.compile(r"<[^>]+>")
_SLUG_RE = re.compile(r'href="/communities/([a-z0-9\-]+)"')
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_DD_RE = re.compile(r"<dt>\s*(.*?)\s*</dt>\s*<dd>(.*?)</dd>", re.S)
_BADGE_RE = re.compile(r'<span class="badge">(.*?)</span>', re.S)
_CITY_ST_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s*(\d{5})(?:-\d{4})?$")
_TOTAL_RE = re.compile(r"(\d+)\s+communities listed")


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub(" ", fragment or "")).replace("\xa0", " ").strip()


def parse_directory_slugs(page_html: str) -> list[str]:
    """Slugs linked from a /communities listing page, in document order."""
    seen, out = set(), []
    for m in _SLUG_RE.finditer(page_html):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def parse_directory_total(page_html: str) -> int | None:
    m = _TOTAL_RE.search(page_html)
    return int(m.group(1)) if m else None


def parse_community(slug: str, page_html: str) -> Location:
    h1 = _H1_RE.search(page_html)
    name = _text(h1.group(1)) if h1 else slug.replace("-", " ").title()

    fields: dict[str, str] = {}
    badges: list[str] = []
    for m in _DD_RE.finditer(page_html):
        label = _text(m.group(1)).lower()
        raw = m.group(2)
        if label.startswith("care"):
            badges = [_text(b) for b in _BADGE_RE.findall(raw)] or [_text(raw)]
        fields[label] = _text(raw.replace("<br>", "\n").replace("<br/>", "\n"))

    street = city = state = zipc = ""
    addr = fields.get("address", "")
    # <dd>210 Orchard Lane<br>Maplewood, OH 44280</dd>
    parts = [p.strip() for p in re.split(r"\n|(?<=[a-z0-9])\s{2,}", addr) if p.strip()]
    if len(parts) == 1:  # fall back: split on the last comma-state-zip group
        m = re.search(r"^(.*?)\s+([A-Za-z .'-]+),\s*([A-Za-z]{2})\s*(\d{5})$", addr)
        if m:
            parts = [m.group(1), f"{m.group(2)}, {m.group(3)} {m.group(4)}"]
    if parts:
        street = parts[0]
    if len(parts) > 1:
        m = _CITY_ST_ZIP.match(parts[-1])
        if m:
            city, state, zipc = m.group(1).strip(), m.group(2).upper(), m.group(3)
        else:
            city = parts[-1]

    return Location(
        slug=slug,
        name=name,
        street=street,
        city=city,
        state=state,
        zip=zipc,
        care_offerings=badges,
        administrator=fields.get("administrator", ""),
        phone=fields.get("phone", ""),
        source_url=f"{config.SITE_BASE}/communities/{slug}",
    )


# Communities the site talks about in prose but has not added to the directory
# yet ("we're delighted to welcome X to the Bellhaven family"). Worth catching:
# a brand-new community is the one most likely to be missing from the CRM.
_BRANDED_NAME_RE = re.compile(
    r"\bBellhaven(?:\s+(?:of|at|the|and)|\s+[A-Z][\w'&-]+)+")

# Phrases that are the operator itself or its corporate address, not a community.
_NOT_A_COMMUNITY = {
    "bellhaven senior living", "bellhaven senior", "bellhaven way",
    "bellhaven family", "bellhaven care network",
}


_EVENT_RE = re.compile(r"welcom|join|acquir|new this year|expand", re.I)


def _sentences(page_html: str) -> list[str]:
    flat = " ".join(_text(page_html).split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]


def parse_announcements(page_html: str, known: set[str]) -> list[str]:
    """Community names mentioned in site copy that are not in the directory.

    Only sentences that describe an ownership/expansion event are considered, so
    navigation chrome and boilerplate cannot leak in.
    """
    found: list[str] = []
    for sent in _sentences(page_html):
        if not _EVENT_RE.search(sent):
            continue
        for m in _BRANDED_NAME_RE.finditer(sent):
            words = m.group(0).split()
            if len(words) > 5:  # a run that long is nav text, not a name
                continue
            cand = re.sub(r"\s+(?:of|at|the|and)$", "", " ".join(words)).strip()
            low = cand.lower()
            if low in _NOT_A_COMMUNITY or low in known or cand in found:
                continue
            if len(cand.split()) < 3:  # "Bellhaven Meadows" alone is too thin
                continue
            found.append(cand)
    return found


# Ownership events described in site copy. These are the evidence that justifies
# re-parenting: if the About page says Bellhaven absorbed Harborview, then a CRM
# facility still sitting under Harborview is a broken link, not a competitor.
_ACQ_PATTERNS = [
    re.compile(r"welcomed the ([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})\s+family"),
    re.compile(r"joining us from ([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})"),
    re.compile(r"acquired ([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,3})"),
]


def parse_acquisitions(page_html: str) -> list[dict]:
    out, seen = [], set()
    for sent in _sentences(page_html):
        for pat in _ACQ_PATTERNS:
            for m in pat.finditer(sent):
                org = m.group(1).strip().rstrip(".,")
                if org.lower() in seen or org.lower().startswith("bellhaven"):
                    continue
                seen.add(org.lower())
                out.append({"organization": org, "quote": sent})
    return out


# ------------------------------------------------------------------- driver ---


def _raw_path(fname: str):
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    return config.RAW_DIR / fname


def fetch_site(live: bool = True) -> dict:
    """Return {'locations': [...], 'directory_total': int, 'announcements': [...]}"""
    pages: dict[str, str] = {}

    def load(fname: str, url: str) -> str:
        p = _raw_path(fname)
        if live:
            text = httpc.get_text(url)
            p.write_text(text, encoding="utf-8")
            return text
        if not p.exists():
            raise FileNotFoundError(f"{p} missing - run `python run.py fetch` with network first")
        return p.read_text(encoding="utf-8")

    slugs: list[str] = []
    total = None
    for page_no in range(1, 21):
        fname = f"communities_page{page_no}.html"
        if not live and not _raw_path(fname).exists():
            break
        try:
            text = load(fname, f"{config.SITE_BASE}/communities?page={page_no}")
        except httpc.HttpError:
            break
        pages[fname] = text
        found = parse_directory_slugs(text)
        total = total or parse_directory_total(text)
        new = [s for s in found if s not in slugs]
        if not new:
            break
        slugs.extend(new)
        if total and len(slugs) >= total:
            break

    locations = []
    for slug in slugs:
        text = load(f"community_{slug}.html", f"{config.SITE_BASE}/communities/{slug}")
        locations.append(parse_community(slug, text))

    known = {loc.name.lower() for loc in locations}
    announcements: list[str] = []
    acquisitions: list[dict] = []
    for fname, path in (("home.html", "/"), ("about.html", "/about")):
        text = load(fname, config.SITE_BASE + path)
        for nm in parse_announcements(text, known):
            if nm.lower() not in known and nm not in announcements:
                announcements.append(nm)
        for acq in parse_acquisitions(text):
            if acq["organization"].lower() not in {a["organization"].lower() for a in acquisitions}:
                acq["source_url"] = config.SITE_BASE + path
                acquisitions.append(acq)

    return {
        "locations": [loc.to_dict() for loc in locations],
        "directory_total": total,
        "announcements": announcements,
        "acquisitions": acquisitions,
    }


def write_snapshot(payload: dict) -> None:
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (config.SNAPSHOT_DIR / "website.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def load_snapshot() -> dict:
    return json.loads((config.SNAPSHOT_DIR / "website.json").read_text(encoding="utf-8"))
