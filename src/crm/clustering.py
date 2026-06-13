"""Pure in-memory clustering for dedup Phase 1.

trigrams/similarity reproduce pg_trgm EXACTLY (parity-tested vs
extensions.similarity) so Python edge-building and the DB fuzzy RPC agree.
A cluster is a WRITE-ISOLATION unit, not a 'one human' claim — see the spec.
Exact-key edges use email/phone/linkedin_url/handle (handle included for
isolation grouping ONLY — the PLANNER, not this module, restricts match keys
to email/linkedin/phone to mirror find_candidates).
"""
import re
import unicodedata

from crm.matching import REVIEW_BAND

CLUSTER_KEYS = ("email", "phone", "linkedin_url", "handle")


# stroked/barred Latin letters have no NFKD decomposition; Postgres unaccent
# maps them via lookup. Mirror that so Python trigrams match the DB exactly.
_STROKE_MAP = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T", "ŀ": "l", "Ŀ": "L",
    "ı": "i", "ȷ": "j", "ƒ": "f", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
})


def _unaccent_lower(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s.lower().translate(_STROKE_MAP))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def trigrams(value: str | None) -> set[str]:
    if not value:
        return set()
    out: set[str] = set()
    for word in re.split(r"[^a-z0-9]+", _unaccent_lower(value)):
        if not word:
            continue
        padded = "  " + word + " "
        for i in range(len(padded) - 2):
            out.add(padded[i:i + 3])
    return out


def similarity(a: str | None, b: str | None) -> float:
    ta, tb = trigrams(a), trigrams(b)
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


class _UF:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def cluster_rows(rows: list[dict]) -> dict[str, list[dict]]:
    """Union-find over exact-key + name-sim(>=REVIEW_BAND) edges, trigram-blocked.
    Returns cluster_id -> rows; cluster_id is the representative row id (stable)."""
    uf = _UF([r["id"] for r in rows])
    for key in CLUSTER_KEYS:
        buckets: dict[str, list[str]] = {}
        for r in rows:
            if r.get(key):
                buckets.setdefault(r[key], []).append(r["id"])
        for ids in buckets.values():
            for other in ids[1:]:
                uf.union(ids[0], other)

    tri_index: dict[str, list[str]] = {}
    tri_of: dict[str, set[str]] = {}
    for r in rows:
        if not r.get("full_name"):
            continue
        t = trigrams(r["full_name"]); tri_of[r["id"]] = t
        for g in t:
            tri_index.setdefault(g, []).append(r["id"])

    checked: set[tuple[str, str]] = set()
    for ids in tri_index.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                if (a, b) in checked or uf.find(a) == uf.find(b):
                    continue
                checked.add((a, b))
                ta, tb = tri_of[a], tri_of[b]
                if len(ta & tb) / len(ta | tb) >= REVIEW_BAND:
                    uf.union(a, b)

    clusters: dict[str, list[dict]] = {}
    for r in rows:
        clusters.setdefault(uf.find(r["id"]), []).append(r)
    return clusters
