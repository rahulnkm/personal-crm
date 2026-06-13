# tests/test_dedup_rpcs.py
def _contact(db, name, **kw):
    return db.table("contacts").insert({"full_name": name, **kw}).execute().data[0]


def test_match_contacts_by_names_best_per_input(db):
    a = _contact(db, "Jonathan Smithers")
    _contact(db, "Totally Different")
    res = db.rpc("match_contacts_by_names",
                 {"names": ["Jonathon Smithers", "Nobody Here At All"], "lim": 1}
                 ).execute().data
    by_idx = {r["idx"]: r for r in res}
    assert by_idx[1]["contact_id"] == a["id"]      # 1-based ordinality
    assert by_idx[1]["score"] >= 0.55
    assert 2 not in by_idx                          # sub-threshold → absent


def test_create_contacts_with_identities_atomic(db):
    payload = [
        {"create_key": "k1",
         "contact": {"full_name": "Ada Lovelace", "current_company": "Analytical"},
         "identity": {"source": "s1", "source_external_id": "x1", "email": "ada@example.com"}},
        {"create_key": "k2",
         "contact": {"full_name": "Grace Hopper"},
         "identity": {"source": "s1", "source_external_id": "x2", "phone": "+15551234567"}},
    ]
    res = db.rpc("create_contacts_with_identities", {"payload": payload}).execute().data
    keymap = {r["create_key"]: r["contact_id"] for r in res}
    assert set(keymap) == {"k1", "k2"}
    idents = db.table("contact_identities").select("contact_id,email,phone").execute().data
    assert any(i["contact_id"] == keymap["k1"] and i["email"] == "ada@example.com" for i in idents)
    assert any(i["contact_id"] == keymap["k2"] and i["phone"] == "+15551234567" for i in idents)
    ada = db.table("contacts").select("current_company").eq(
        "id", keymap["k1"]).single().execute().data
    assert ada["current_company"] == "Analytical"   # "current_role" sibling column writes fine


def test_staging_dedup_cluster_column(db):
    row = db.table("staging").insert(
        {"source": "s", "source_external_id": "c1", "full_name": "X",
         "dedup_cluster": "cluster-7"}).execute().data[0]
    assert row["dedup_cluster"] == "cluster-7"
