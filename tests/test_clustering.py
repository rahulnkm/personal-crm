from crm.clustering import cluster_rows, trigrams, similarity


def test_trigrams_match_pg_trgm_tokenization():
    # pg_trgm: lowercase, split non-alphanumerics, pad word "  <w> ", sliding 3-grams.
    assert trigrams("Cat") == {"  c", " ca", "cat", "at "}
    assert trigrams("") == set()


def test_similarity_is_jaccard():
    assert 0.4 < similarity("Jonathan", "Jonathon") < 0.95
    assert similarity("abc", "abc") == 1.0
    assert similarity("abc", "xyz") == 0.0


def test_cluster_exact_key_edge():
    rows = [
        {"id": "1", "full_name": "Ada L", "email": "a@b.co"},
        {"id": "2", "full_name": "Completely Different", "email": "a@b.co"},
        {"id": "3", "full_name": "Nobody", "email": "z@z.co"},
    ]
    cid = _cid(cluster_rows(rows))
    assert cid["1"] == cid["2"] and cid["1"] != cid["3"]


def test_cluster_review_band_and_transitive():
    rows = [
        {"id": "1", "full_name": "Robert Smith", "email": "r@x.com"},
        {"id": "2", "full_name": "Robart Smith", "email": "b@y.com"},
        {"id": "3", "full_name": "Zelda Far", "email": "r@x.com"},
    ]
    cid = _cid(cluster_rows(rows))
    assert cid["1"] == cid["2"] == cid["3"]   # 1~2 name, 1~3 email


def test_cluster_no_edge_separate():
    rows = [{"id": "1", "full_name": "Alpha One", "email": "a@a.co"},
            {"id": "2", "full_name": "Beta Two", "email": "b@b.co"}]
    assert len(cluster_rows(rows)) == 2


def _cid(clusters):
    return {r["id"]: c for c, members in clusters.items() for r in members}


PARITY_PAIRS = [
    ("Jonathan Smithers", "Jonathon Smithers"),
    ("José García", "Jose Garcia"),
    ("Robert Smith", "Robart Smith"),
    ("Ada Lovelace", "Ada Lovelace"),
    # stroked/barred Latin letters — Postgres unaccent maps via lookup table,
    # not NFKD; _STROKE_MAP must mirror this so Python trigrams match the DB.
    ("Łukasz Kowalski", "Lukasz Kowalski"),
    ("Søren Aaberg", "Soren Aaberg"),
    ("Đức Nguyen", "Duc Nguyen"),
]

def test_similarity_parity_with_pg_trgm(db):
    for a, b in PARITY_PAIRS:
        c = db.table("contacts").insert({"full_name": b}).execute().data[0]
        rows = db.rpc("match_contacts_by_names", {"names": [a], "lim": 1}).execute().data
        py = similarity(a, b)
        if py >= 0.55:
            assert rows and abs(rows[0]["score"] - py) < 0.001, f"{a!r}/{b!r} py={py} db={rows}"
        else:
            assert not rows
        db.table("contacts").delete().eq("id", c["id"]).execute()
