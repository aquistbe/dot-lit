from dot_lit.store import Store, normalize_id, tokenize_query


def _rec(i, title, abstract="", year=2000, collections=()):
    return {"id": f"dot:{i}", "oai_identifier": f"oai:dot.stacks:dot:{i}", "datestamp": "2026-01-01T00:00:00Z",
            "title": title, "abstract": abstract, "year": year, "collections": list(collections),
            "authors": [], "subjects": [], "report_numbers": [], "raw": {},
            "landing_url": f"https://rosap.ntl.bts.gov/view/dot/{i}"}


def test_search_and_filters(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([
        _rec(1, "Driver improvement program evaluation", "negligent operator", 2007, ["Oregon DOT"]),
        _rec(2, "Countermeasures that work", "highway safety", 2023, ["NHTSA"]),
        _rec(3, "Driver licensing", "improvement of licensing", 1982),
    ])
    hits = s.search("driver improvement program")
    assert hits[0]["id"] == "dot:1" and hits[0]["match_mode"] == "all_terms"
    assert any(h["id"] == "dot:3" and h["match_mode"] == "any_terms" for h in hits)
    assert [h["id"] for h in s.search("driver", year_max=1990)] == ["dot:3"]
    assert [h["id"] for h in s.search("countermeasures", collection="nhtsa")] == ["dot:2"]
    # upsert is idempotent and updates the FTS index
    s.upsert_records([_rec(2, "Countermeasures that work, 11th edition", "", 2023, ["NHTSA"])])
    assert s.count() == 3
    assert s.search("edition")[0]["id"] == "dot:2"
    assert s.collections()[0]["records"] == 1
    s.close()


def test_tokenize_query():
    assert tokenize_query('driver "improvement program" oper*') == ['"driver"', '"improvement program"', '"oper"*']
    assert tokenize_query("a AND (b) OR") == ['"a"', '"(b)"']
    assert tokenize_query("") == []


def test_normalize_id():
    for s in ("dot:5", "5", "oai:dot.stacks:dot:5", "https://rosap.ntl.bts.gov/view/dot/5"):
        assert normalize_id(s) == "dot:5"
