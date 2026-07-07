"""Phase 1: build the resolution plan for pending staging rows.

A cluster is a write-isolation unit; resolution INSIDE a cluster is an in-order
replay of the sequential engine (crm/commands/dedup.py), reusing crm.matching
thresholds and the SAME exact-key set/method names as find_candidates. No writes.

Each plan item: {id, source, source_external_id, cluster_id, identity, match_status,
  match_method, staged, and one of: matched_ref (attach/review target),
  create_key+contact_fields}.
A 'ref' is an existing contact uuid OR a create_key (a member created earlier in
THIS cluster). Phase 2 dereferences create_key→uuid after the atomic create RPC.

Key constraint (non-negotiable):
  EXACT_KEYS = ("email", "linkedin_url", "phone") — mirrors find_candidates, NO handle.
  clustering.py's CLUSTER_KEYS keeps handle for isolation grouping only.
  Review-band rows (0.55–0.92 name sim) and nameless rows are NOT resolved members.
"""
from crm.clustering import cluster_rows, similarity
from crm.matching import AUTO_MERGE, CONFLICT_SCORE, _is_role_email, classify

EXACT_KEYS = ("email", "linkedin_url", "phone")   # mirrors find_candidates (NO handle)
METHOD = {"email": "exact_email", "linkedin_url": "exact_linkedin", "phone": "exact_phone"}
IDENTITY_FIELDS = ("source", "source_external_id", "email", "phone",
                   "linkedin_url", "handle", "raw_json")


def _existing(client, rows):
    """Return per-row exact hits and fuzzy hits from existing contacts.

    Returns:
        exact: row id -> [(contact_id, method), ...]  (all existing exact hits)
        fuzzy: row id -> (contact_id, score)           (best fuzzy hit, if any)
    """
    exact = {r["id"]: [] for r in rows}

    # Exact key lookups: batch by key to minimise DB round-trips
    for key in EXACT_KEYS:
        vals = sorted({
            r[key] for r in rows
            if r.get(key) and not (key == "email" and _is_role_email(r[key]))
        })
        found: dict[str, str] = {}   # value -> contact_id (first hit wins)
        for i in range(0, len(vals), 100):
            chunk_vals = vals[i:i + 100]
            for it in (client.table("contact_identities")
                       .select(f"contact_id,{key}")
                       .in_(key, chunk_vals)
                       .execute().data):
                found.setdefault(it[key], it["contact_id"])
        for r in rows:
            v = r.get(key)
            if v and v in found and not (key == "email" and _is_role_email(v)):
                cid = found[v]
                # Only add if this contact_id isn't already in the hit list
                if all(c != cid for c, _ in exact[r["id"]]):
                    exact[r["id"]].append((cid, METHOD[key]))

    # Fuzzy (name) lookups: batch via the bulk RPC added in migration 0005
    named = [r for r in rows if r.get("full_name")]
    fuzzy: dict[str, tuple[str, float]] = {}
    for i in range(0, len(named), 200):
        chunk = named[i:i + 200]
        results = (client.rpc("match_contacts_by_names",
                              {"names": [r["full_name"] for r in chunk], "lim": 1})
                   .execute().data)
        for row in results:
            r = chunk[row["idx"] - 1]   # idx is 1-based ordinality from unnest
            fuzzy[r["id"]] = (row["contact_id"], row["score"])

    return exact, fuzzy


def _union_by_existing_contact(clusters, rows, exact, fuzzy):
    """§3.1 step 4: union clusters whose rows resolve to the SAME existing contact.

    This closes the concurrent-attach race: any two rows targeting the same
    existing contact land in ONE cluster, so ONE thread does all writes to that
    contact (fill-null, identity insert, enrichment_log). Only single-target rows
    count — conflicting-keys rows (≥2 distinct existing hits) go to review and
    touch no contact, so they're excluded from the union.

    A row's single existing target for union purposes is:
      - the contact, if it has exactly ONE exact-key hit (exact path); ELSE
      - the fuzzy contact_id, if it has ZERO exact hits AND a fuzzy hit that
        classifies as auto (score >= AUTO_MERGE).
    Rows with ≥2 distinct exact hits (conflicting keys → review, touch no contact)
    or a sub-auto fuzzy hit (<0.92 → not attached) contribute no union edge.

    NOTE: fuzzy co-targeting of a COMMON EXISTING contact (two rows each scoring
    ≥0.92 name-sim to one existing contact, while dissimilar to EACH OTHER so no
    clustering name edge joins them) IS handled HERE: a single fuzzy-auto target
    unions exactly like an exact target, so one thread does all writes to that
    contact. This closes a cross-thread fill race that the clustering edge alone
    does NOT cover. Mutual fuzzy similarity between two NEW rows (similar to each
    other, no shared existing target) is the separate case handled by the
    name-sim ≥ REVIEW_BAND clustering edge in clustering.py.
    """
    rep = {cid: cid for cid in clusters}

    def find(c):
        while rep[c] != c:
            rep[c] = rep[rep[c]]
            c = rep[c]
        return c

    cluster_of = {r["id"]: cid for cid, ms in clusters.items() for r in ms}

    by_contact: dict[str, list[str]] = {}
    for r in rows:
        hits = exact[r["id"]]
        target: str | None = None
        if len(hits) == 1:            # single existing exact target — not conflicting
            target = hits[0][0]
        elif len(hits) == 0:          # no exact hit — fuzzy-auto target counts too
            fz = fuzzy.get(r["id"])
            if fz and fz[1] >= AUTO_MERGE:
                target = fz[0]
        # else: ≥2 exact hits → conflicting → review → no union edge
        if target is not None:
            by_contact.setdefault(target, []).append(cluster_of[r["id"]])

    for cids in by_contact.values():
        for other in cids[1:]:
            rep[find(cids[0])] = find(other)

    merged: dict[str, list[dict]] = {}
    for cid, ms in clusters.items():
        merged.setdefault(find(cid), []).extend(ms)
    return merged


def build_plan(client, rows):
    """Build the resolution plan for a list of pending staging rows.

    Returns a list of plan items, one per row. Each item has at minimum:
        id, source, source_external_id, cluster_id, identity, staged,
        match_status, match_method
    Plus one of:
        matched_ref           — for auto_matched / needs_review
        create_key + contact_fields — for merged (new contact)

    No DB writes are performed.
    """
    exact, fuzzy = _existing(client, rows)
    clusters = _union_by_existing_contact(cluster_rows(rows), rows, exact, fuzzy)

    plan = []
    for cluster_id, members in clusters.items():
        # In-order replay: process rows in imported_at order (stable: tie-break on id)
        members = sorted(members, key=lambda r: (r.get("imported_at") or "", r["id"]))

        # resolved: list of {ref, name, keys} for rows already resolved in THIS cluster
        # CRITICAL: review-band rows are NOT added here — exactly as in sequential engine
        resolved = []

        for r in members:
            base = {
                "id": r["id"],
                "source": r["source"],
                "cluster_id": cluster_id,
                "source_external_id": r["source_external_id"],
                "identity": {f: r.get(f) for f in IDENTITY_FIELDS},
                "staged": {
                    "full_name": r.get("full_name"),
                    "role": r.get("role"),
                    "company": r.get("company"),
                    "location": r.get("location"),
                    "twitter_username": r.get("twitter_username"),
                    "github_username": r.get("github_username"),
                    "website_url": r.get("website_url"),
                },
            }

            # Nameless rows: reject immediately (mirrors sequential engine)
            if not r.get("full_name"):
                plan.append({**base, "match_status": "rejected", "match_method": "no_name"})
                continue

            # --- Exact key matching ---
            # Collect targets in FIELD ORDER (email → linkedin_url → phone), mirroring
            # find_candidates' hits-dict insertion order exactly.  For each field we
            # check BOTH existing hits (from the `exact` map) AND resolved cluster
            # members carrying that key value, deduplicating by ref with setdefault
            # semantics — first occurrence of a ref wins its method.  This ensures that
            # for a conflicting_keys row the matched_ref is the FIRST ref encountered in
            # field order, which is identical to what find_candidates' next(iter(hits))
            # returns in the sequential engine.
            ordered: dict[str, str] = {}   # ref -> method, insertion-ordered (Python 3.7+)

            # Reconstruct a per-field lookup from the exact map (cid, method) pairs.
            # The exact map already deduplicates by cid within each field (see _existing),
            # so the first pair per method is the authoritative existing hit for that field.
            existing_by_method: dict[str, str] = {}  # method -> cid (first hit per method)
            for cid, meth in exact[r["id"]]:
                existing_by_method.setdefault(meth, cid)

            for key in EXACT_KEYS:
                v = r.get(key)
                if not v:
                    continue
                if key == "email" and _is_role_email(v):
                    continue
                meth = METHOD[key]
                # Existing contact hit for this field?
                if meth in existing_by_method:
                    ordered.setdefault(existing_by_method[meth], meth)
                # Resolved cluster-member hit for this field?
                for m in resolved:
                    if m["keys"].get(key) == v:
                        ordered.setdefault(m["ref"], meth)
                        break   # only first matching member per field

            targets = list(ordered.items())   # [(ref, method)] in field order
            refs = set(ordered)

            if len(refs) >= 2:
                # Conflicting keys — different contacts targeted by different keys
                plan.append({
                    **base,
                    "match_status": "needs_review",
                    "matched_ref": targets[0][0],
                    "match_confidence": CONFLICT_SCORE,
                    "match_method": "conflicting_keys",
                })
                continue

            if len(refs) == 1:
                ref, method = targets[0]
                _attach(plan, base, ref, 1.0, method, resolved, r)
                continue

            # --- Fuzzy name matching ---
            # Best of: existing-contact fuzzy hit OR in-cluster resolved member similarity
            best_ref: str | None
            best_score: float
            if r["id"] in fuzzy:
                best_ref, best_score = fuzzy[r["id"]]
            else:
                best_ref, best_score = None, 0.0

            for m in resolved:
                s = similarity(r["full_name"], m["name"])
                # Strict `>` is correct here: existing-contact fuzzy hits (from the DB
                # RPC) win ties at the same score.  A resolved cluster member tying an
                # existing contact at 1.0 is unreachable — if they shared the same name
                # AND all exact keys differed, the member's own creating row would itself
                # have already auto-matched the existing contact (score=1.0 ≥ AUTO_MERGE).
                # Do NOT change to `>=`.
                if s > best_score:
                    best_ref, best_score = m["ref"], s

            verdict = classify(best_score) if best_ref else "none"

            if verdict == "auto":
                _attach(plan, base, best_ref, best_score, "fuzzy_name", resolved, r)
            elif verdict == "review":
                plan.append({
                    **base,
                    "match_status": "needs_review",
                    "matched_ref": best_ref,
                    "match_confidence": best_score,
                    "match_method": "fuzzy_name",
                })
                # CRITICAL: review rows do NOT become resolved members
                # (matches sequential engine — review rows never create a contact)
            else:
                # No match at all — create a new contact
                ck = f"{cluster_id}:{r['id']}"
                plan.append({
                    **base,
                    "match_status": "merged",
                    "create_key": ck,
                    "match_method": "new_contact",
                    "contact_fields": {
                        "full_name": r["full_name"],
                        "current_role": r.get("role"),
                        "current_company": r.get("company"),
                        "location": r.get("location"),
                        "twitter_username": r.get("twitter_username"),
                        "github_username": r.get("github_username"),
                        "website_url": r.get("website_url"),
                    },
                })
                resolved.append({
                    "ref": ck,
                    "name": r["full_name"],
                    "keys": {k: r.get(k) for k in EXACT_KEYS},
                })

    return plan


def _attach(plan, base, ref, score, method, resolved, r):
    """Add an auto_matched item and update the resolved member's key set."""
    plan.append({
        **base,
        "match_status": "auto_matched",
        "matched_ref": ref,
        "match_confidence": score,
        "match_method": method,
    })
    # If this ref already exists in resolved, merge in any new keys from this row
    for m in resolved:
        if m["ref"] == ref:
            for k in EXACT_KEYS:
                if r.get(k) and not m["keys"].get(k):
                    m["keys"][k] = r[k]
            break
