"""Tests for trigram bucket-size cap in cluster_rows (Finding 6).

The cap guards against O(k²) blowup when a very common trigram (e.g. "  s"
from every name starting with "S") lands 200+ contacts in one bucket.

Invariants:
  - NAME-SIMILARITY buckets >MAX_BUCKET are skipped (the pairwise loop is O(k²)).
  - A genuine duplicate pair that shares a RARER trigram (small bucket) still
    clusters — the rare-trigram edge survives even when the fat bucket is skipped.
  - EXACT-KEY buckets (email/phone/...) are NEVER capped — >200 rows sharing an
    email must all land in one cluster.
  - When buckets are skipped, `err` is called with the exact notice text.
  - When NO bucket exceeds 200, NO notice is emitted.
"""
import sys
import io

import pytest

from crm.clustering import cluster_rows, MAX_BUCKET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cid(clusters):
    """Map row id -> cluster representative id."""
    return {r["id"]: c for c, members in clusters.items() for r in members}


def _make_row(id_, full_name=None, email=None, phone=None):
    return {"id": id_, "full_name": full_name, "email": email, "phone": phone}


# ---------------------------------------------------------------------------
# 1. Oversized name bucket is skipped BUT rare-trigram edge still clusters
# ---------------------------------------------------------------------------

def test_oversized_bucket_skipped_rare_trigram_survives():
    """Fat bucket (>MAX_BUCKET rows sharing a common trigram) is skipped.

    The genuine duplicate pair also shares a rarer trigram that lives in a
    small bucket — that edge must still fire so the pair ends up in the same
    cluster.

    Strategy:
      - Names "Zzz Alpha One", "Zzz Alpha Two" share the rare trigrams
        "  z", " zz", "zzz", "zz " … none of the filler names have "Zzz".
      - All 201 filler names start with "Sam" → they share the very common
        trigram "  s", " sa", "sam" etc., making at least one bucket >200.
      - "Zzz Alpha One" and "Zzz Alpha Two" are similar (Jaccard >= REVIEW_BAND)
        and their "zzz"-family trigrams form a tiny bucket (size 2).
    """
    rows = []
    # 201 filler rows → at least one "  s"/"sam" bucket hits 201 entries
    for i in range(MAX_BUCKET + 1):
        rows.append(_make_row(f"filler-{i}", full_name=f"Sam Filler{i:04d}"))

    # The genuine duplicate pair (rare prefix "Zzz" → small bucket)
    rows.append(_make_row("dup-A", full_name="Zzz Alpha One"))
    rows.append(_make_row("dup-B", full_name="Zzz Alpha Two"))

    clusters = cluster_rows(rows)
    cid = _cid(clusters)

    # The pair must cluster together
    assert cid["dup-A"] == cid["dup-B"], (
        "Genuine duplicate pair must still cluster via rare-trigram edge "
        "even when fat buckets are skipped"
    )

    # Sanity: total cluster count is less than total rows (filler rows are all separate)
    assert len(clusters) < len(rows)


# ---------------------------------------------------------------------------
# 2. Exact-key buckets are NOT capped
# ---------------------------------------------------------------------------

def test_exact_key_buckets_not_capped():
    """More than MAX_BUCKET rows sharing the same email must all land in one cluster."""
    shared_email = "shared@example.com"
    rows = [
        _make_row(f"e-{i}", full_name=f"Person {i}", email=shared_email)
        for i in range(MAX_BUCKET + 1)
    ]

    clusters = cluster_rows(rows)

    assert len(clusters) == 1, (
        f"All {MAX_BUCKET + 1} rows sharing an email must be one cluster; "
        f"got {len(clusters)} clusters"
    )


# ---------------------------------------------------------------------------
# 3. Notice is emitted with exact text when buckets are skipped
# ---------------------------------------------------------------------------

def test_notice_emitted_when_bucket_skipped(monkeypatch):
    """err() must be called with the exact notice text when >=1 bucket is skipped."""
    captured = []

    import crm.clustering as _mod
    monkeypatch.setattr(_mod, "err", lambda msg: captured.append(msg))

    # Build rows whose names share a very common trigram many times over
    rows = [
        _make_row(f"n-{i}", full_name=f"Sam Name{i:04d}")
        for i in range(MAX_BUCKET + 1)
    ]

    cluster_rows(rows)

    assert len(captured) == 1, f"Expected exactly one err() call, got: {captured}"
    msg = captured[0]
    # Count of skipped buckets is embedded in the message; extract N
    import re
    m = re.match(
        r"clustering: skipped (\d+) oversized trigram bucket\(s\) \(>200\); "
        r"rare-trigram edges still applied",
        msg,
    )
    assert m is not None, f"Notice text did not match expected pattern. Got: {msg!r}"
    n = int(m.group(1))
    assert n >= 1, f"Expected N>=1 skipped buckets, got N={n}"


# ---------------------------------------------------------------------------
# 4. No notice when no bucket exceeds MAX_BUCKET
# ---------------------------------------------------------------------------

def test_no_notice_when_no_bucket_oversized(monkeypatch):
    """err() must NOT be called when all buckets are within the cap."""
    captured = []

    import crm.clustering as _mod
    monkeypatch.setattr(_mod, "err", lambda msg: captured.append(msg))

    # Only 3 rows — no bucket can exceed MAX_BUCKET
    rows = [
        _make_row("a", full_name="Robert Smith"),
        _make_row("b", full_name="Robart Smith"),
        _make_row("c", full_name="Zelda Far"),
    ]

    cluster_rows(rows)

    assert captured == [], f"No notice should be emitted; got: {captured}"
