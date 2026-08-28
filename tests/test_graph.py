import transport_lit.graph as G
from transport_lit.store import Store


def _rec(i, title, year=2000, doi="", pref="dot"):
    return {"id": f"{pref}:{i}", "oai_identifier": "x", "datestamp": "", "title": title, "abstract": "", "year": year,
            "collections": [], "authors": [], "subjects": [], "report_numbers": [], "raw": {}, "landing_url": "", "doi": doi}


FAKE = {
    "/works/https://doi.org/10.1/a": {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a", "display_name": "Report A",
                                       "publication_year": 2007, "cited_by_count": 2, "referenced_works": ["https://openalex.org/W2", "https://openalex.org/W9"]},
    "/works/W1": {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a", "display_name": "Report A", "publication_year": 2007,
                  "cited_by_count": 2, "referenced_works": ["https://openalex.org/W2", "https://openalex.org/W9"]},
    "/works/pmid:77": {"id": "https://openalex.org/W3", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/77"}, "display_name": "Paper C",
                       "publication_year": 2015, "cited_by_count": 0, "referenced_works": ["https://openalex.org/W1"]},
}


def fake_fetch(path, params=None):
    if path in FAKE:
        return FAKE[path]
    if path == "/works" and params and params.get("filter", "").startswith("openalex:"):
        ids = params["filter"].split(":", 1)[1].split("|")
        return {"results": [{"id": f"https://openalex.org/{w}", "display_name": f"Work {w}", "publication_year": 1999,
                             "doi": "https://doi.org/10.1/b" if w == "W2" else None, "cited_by_count": 5} for w in ids]}
    if path == "/works" and params and params.get("filter", "").startswith("cites:W1"):
        return {"results": [{"id": "https://openalex.org/W3", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/77"}, "display_name": "Paper C",
                             "publication_year": 2015, "cited_by_count": 0}], "meta": {"next_cursor": None}}
    if path == "/works" and params and params.get("filter", "").startswith("title.search:"):
        return {"results": [{"id": "https://openalex.org/W5", "display_name": "Evaluation of the Oregon DMV driver improvement program", "publication_year": 2007}]}
    return None


def test_graph_resolution_edges_and_index_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "fetch", fake_fetch)
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([_rec(1, "Report A", 2007, doi="10.1/a"), _rec(2, "Report B", 1999, doi="10.1/b"),
                      _rec(77, "Paper C", 2015, pref="pubmed"), _rec(3, "Evaluation of the Oregon DMV driver improvement program.", 2007)])
    g = G.Graph(s)
    refs = g.references("dot:1")
    assert refs["resolved"] and refs["match"] == "doi" and refs["n"] == 2 and refs["in_index"] == 1
    assert {r["openalex_id"]: r["record_id"] for r in refs["references"]} == {"W2": "dot:2", "W9": None}
    cites = g.citations("dot:1")
    assert cites["n"] == 1 and cites["citations"][0]["record_id"] == "pubmed:77" and cites["cited_by_count_openalex"] == 2
    assert g.citations("dot:1", only_in_index=True)["n"] == 1
    # PMID and exact-title matching
    assert g.resolve("pubmed:77")["match"] == "pmid"
    assert g.resolve("dot:3")["match"] == "title" and g.resolve("dot:3")["openalex_id"] == "W5"
    # cached: second call must not need the network
    monkeypatch.setattr(G, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    assert g.references("dot:1")["n"] == 2 and g.citations("dot:1")["n"] == 1
    assert g.cited_by_counts(["dot:1", "dot:2"]) == {"dot:1": 2, "dot:2": 5}
    st = g.stats(); assert st["edges"] == 3 and st["works_in_index"] == 4
    s.close()


def test_titles_match():
    from transport_lit.graph import _titles_match, _norm_title
    a = _norm_title("Countermeasures That Work: A Highway Safety Countermeasure Guide for State Highway Safety Offices, 11th Edition, 2023")
    b = _norm_title("Countermeasures That Work: A Highway Safety Countermeasure Guide For State Highway Safety Offices, Eleventh Edition")
    assert _titles_match(a, b)
    assert not _titles_match(_norm_title("Road safety"), _norm_title("Road safety in Peru"))  # too short to trust
    assert not _titles_match(_norm_title("Evaluation of the Oregon DMV driver improvement program"), _norm_title("Evaluation of the Iowa driver improvement program by gender"))


def test_title_variants():
    from transport_lit.graph import _title_variants
    v = _title_variants("Countermeasures That Work: A Highway Safety Countermeasure Guide for State Highway Safety Offices [Second Edition, 2007]")
    assert v[1] == "Countermeasures That Work: A Highway Safety Countermeasure Guide for State Highway Safety Offices"
    assert len(v) == 3 and v[2].split()[:3] == ["Countermeasures", "That", "Work:"]
    assert _title_variants("Short title") == ["Short title"]
