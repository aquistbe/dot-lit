from transport_lit.store import Store, normalize_id, tokenize_query


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


def test_lookup_similar_whatsnew_citations(tmp_path):
    from transport_lit.citations import to_bibtex, to_ris
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([
        {**_rec(1, "Driver improvement program evaluation", "negligent operator", 2007, ["Oregon DOT"]),
         "doi": "10.1000/abc", "report_numbers": ["SPR 634"], "authors": ["Strathman, James G."]},
        _rec(2, "Driver improvement schools and recidivism", "", 1982),
        _rec(3, "Bridge deck overlays", "", 2019),
    ])
    assert [r["id"] for r in s.lookup("https://doi.org/10.1000/abc")] == ["dot:1"]
    assert [r["id"] for r in s.lookup("SPR 634")] == ["dot:1"]
    assert [r["id"] for r in s.lookup("dot:3")] == ["dot:3"]
    assert s.similar("dot:1")[0]["id"] == "dot:2"
    new = s.whats_new(1)
    assert new["counts_by_source"] == {"dot": 3}
    ris = to_ris([s.get_record("dot:1")])
    assert "TY  - RPRT" in ris and "AU  - Strathman, James G." in ris and "M3  - SPR 634" in ris
    bib = to_bibtex([s.get_record("dot:1")])
    assert bib.startswith("@techreport{strathman2007dot1") and "doi = {10.1000/abc}" in bib
    assert [h["id"] for h in s.search("driver", sources=["dot"], limit=1, offset=1)] == [s.search("driver", limit=2)[1]["id"]]
    s.close()


def test_fulltext_fts(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([_rec(1, "A report", "", 2000)])
    s.put_fulltext("dot:1", pdf_url="x", status="ok", n_pages=1, n_chars=10, text="negligent operator points", error=None)
    hits = s.search_fulltext("negligent operator")
    assert hits and hits[0]["record_id"] == "dot:1" and "[negligent]" in hits[0]["snippet"]
    s.close()


def test_cjk_substring_fallback(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([{**_rec(1, "交通事故死者数の推移", "", 2010), "id": "cinii:1"},
                      {**_rec(2, "Road safety", "", 2010), "id": "cinii:2"}])
    hits = s.search("交通事故")
    assert [h["id"] for h in hits] == ["cinii:1"] and hits[0]["match_mode"] == "cjk_substring"
    s.close()


def test_lookup_text_fallback_and_doctor(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([{**_rec(1, "Countermeasures That Work [Traffic Tech]", "NHTSA has published Report No. DOT HS 813 097, a guide.", 2021)}])
    hits = s.lookup("DOT HS 813 097")
    assert hits and hits[0]["id"] == "dot:1" and hits[0].get("lookup_match") == "text"
    s.conn.execute("INSERT INTO harvest_runs(source, kind, started_at, finished_at, status) VALUES ('x','full','2026-01-02T00:00:00Z','2026-01-01T00:00:00Z','complete')")
    s.conn.execute("INSERT INTO harvest_runs(source, kind, started_at, status) VALUES ('y','full','2020-01-01T00:00:00Z','running')")
    s.conn.commit()
    rep = s.doctor()
    assert rep["integrity"] == "ok" and len(rep["runs_with_impossible_times"]) == 1 and len(rep["runs_still_running"]) == 1
    done = s.repair()
    assert any("finish time cleared" in d for d in done) and any("marked failed" in d for d in done)
    after = s.doctor()
    assert not after["runs_with_impossible_times"] and not after["runs_still_running"]
    s.close()
