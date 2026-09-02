"""Normalisation helpers.

Matching quality in this problem is almost entirely a normalisation problem:
"1420 Harbor Point Drive" and "1420 Harbor Point Dr" are the same building, and
"Bellhaven Healthcare Centre of Ashland" and "Bellhaven Health Care Center of
Ashland" are the same facility. Everything here is deterministic and unit-tested;
no LLM calls, no network.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# --------------------------------------------------------------- primitives ---

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def basic(s: str) -> str:
    """lowercase, strip accents/punctuation, collapse whitespace."""
    s = _ascii(s).lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


# ------------------------------------------------------------------ address ---

# USPS-style street suffix + directional normalisation.
STREET_ABBR = {
    "street": "st", "st": "st",
    "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr",
    "avenue": "ave", "av": "ave", "ave": "ave",
    "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "circle": "cir", "cir": "cir",
    "place": "pl", "pl": "pl",
    "parkway": "pkwy", "pkwy": "pkwy",
    "highway": "hwy", "hwy": "hwy",
    "terrace": "ter", "ter": "ter",
    "trail": "trl", "trl": "trl",
    "square": "sq", "sq": "sq",
    "pike": "pike", "pk": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne",
    "southwest": "sw", "southeast": "se",
    "suite": "ste", "ste": "ste",
    "post": "po",
    "box": "box",
}

_UNIT = re.compile(r"\b(ste|apt|unit|bldg|fl)\b.*$")


def street(s: str) -> str:
    """Canonical street line: '4930 West Lake Road' -> '4930 w lake rd'."""
    toks = basic(s).split()
    toks = [STREET_ABBR.get(t, t) for t in toks]
    out = _WS.sub(" ", " ".join(toks)).strip()
    out = _UNIT.sub("", out).strip()
    return out


def is_po_box(s: str) -> bool:
    return bool(re.match(r"^\s*(po|p o|p\.o\.?)\s*box\b", basic(s)))


def house_number(s: str) -> str:
    m = re.match(r"\s*(\d+)", basic(s))
    return m.group(1) if m else ""


def zip5(s: str) -> str:
    m = re.search(r"(\d{5})", s or "")
    return m.group(1) if m else ""


def city(s: str) -> str:
    return basic(s)


def state(s: str) -> str:
    return (s or "").strip().upper()[:2]


# --------------------------------------------------------------------- name ---

# Words that describe *what kind of building it is*, not *which building it is*.
CARE_WORDS = {
    "care", "cares", "center", "centre", "centers", "health", "healthcare",
    "nursing", "rehab", "rehabilitation", "senior", "seniors", "living",
    "retirement", "home", "house", "community", "communities", "campus",
    "commons", "village", "manor", "estates", "gardens", "terrace", "woods",
    "shores", "court", "crossings", "meadows", "place", "point", "ridge",
    "acres", "arbors", "assisted", "independent", "memory", "skilled",
    "short", "term", "and", "of", "at", "the", "inc", "llc", "lp", "group",
    "partners", "eldercare", "senior living",
}

# Operator brand names seen in this dataset. Stripping them is what lets
# "Cedar Trail of Zanesville" match "Bellhaven of Zanesville".
BRAND_WORDS = {
    "bellhaven", "harborview", "cedartrail", "cedar", "trail", "millstone",
    "juniper", "stonebridge", "eldercare",
}

_STOP_FOR_CORE = CARE_WORDS | BRAND_WORDS


def name(s: str) -> str:
    """Full normalised name (brand kept)."""
    return basic(s)


def name_tokens(s: str) -> set[str]:
    return set(name(s).split())


def geo_core(s: str) -> str:
    """The part of a facility name that identifies *where* it is.

    'Harborview Nursing & Rehab of Port Clinton' -> 'port clinton'
    'Bellhaven of Port Clinton'                  -> 'port clinton'
    'Sunny Acres Retirement Home'                -> 'sunny'
    """
    toks = [t for t in name(s).split() if t not in _STOP_FOR_CORE]
    return " ".join(toks)


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def name_similarity(a: str, b: str) -> float:
    """Blend of whole-string, token-set and geo-core similarity."""
    na, nb = name(a), name(b)
    if na == nb:
        return 1.0
    seq = ratio(na, nb)
    jac = jaccard(name_tokens(a), name_tokens(b))
    core_a, core_b = geo_core(a), geo_core(b)
    core = ratio(core_a, core_b) if (core_a and core_b) else 0.0
    return round(max(0.55 * seq + 0.45 * jac, 0.35 * seq + 0.65 * core), 4)


# ------------------------------------------------------------------- keying ---

def address_key(street_line: str, zipcode: str, city_line: str, state_line: str) -> str:
    """Strongest identity key we have for a physical building."""
    st = street(street_line)
    if not st or is_po_box(street_line):
        return ""
    z = zip5(zipcode)
    if z:
        return f"{st}|{z}"
    return f"{st}|{city(city_line)}|{state(state_line)}"


def loose_address_key(street_line: str, zipcode: str, city_line: str, state_line: str) -> str:
    """House number + first street word + locality. Survives '3313 Wilmington Pike'
    vs '3313 Wilmington Pk' style suffix noise that STREET_ABBR misses."""
    st = street(street_line)
    if not st or is_po_box(street_line):
        return ""
    parts = st.split()
    num = parts[0] if parts and parts[0].isdigit() else ""
    words = [p for p in parts[1:] if not p.isdigit()]
    stem = words[0][:6] if words else ""
    z = zip5(zipcode) or f"{city(city_line)}-{state(state_line)}"
    return f"{num}|{stem}|{z}"
