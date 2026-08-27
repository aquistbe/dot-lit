from pathlib import Path

from transport_lit.importers import import_ris, parse_ris, ris_to_record
from transport_lit.store import Store

RIS = """TY  - RPRT
TI  - Evaluation of the Oregon DMV Driver Improvement Program
AU  - Strathman, James G
AU  - Kimpel, Thomas J
PY  - 2007
AB  - This report provides an evaluation of the Oregon DIP,
  which was substantially changed in 2002.
KW  - Driver improvement
KW  - Point systems
PB  - Oregon Department of Transportation
M3  - FHWA-OR-RD-07-08
UR  - https://trid.trb.org/view/813520
UR  - https://www.oregon.gov/odot/Programs/ResearchDocuments/DIP.pdf
ER  - 

TY  - JOUR
TI  - Negligent operator programs: a review
AU  - Doe, Jane
PY  - 1999/01/01
JO  - Journal of Safety Research
DO  - 10.1016/S0022-4375(99)00001-X
AN  - 12345
ER  - 
"""


def test_parse_and_map():
    recs = list(parse_ris(RIS))
    assert len(recs) == 2
    r = ris_to_record(recs[0], source="import", collection="test")
    assert r["id"] == "trid:813520" and r["collections"] == ["TRID", "test"]
    assert r["year"] == 2007 and r["authors"] == ["Strathman, James G", "Kimpel, Thomas J"]
    assert "changed in 2002" in r["abstract"]  # continuation line joined
    assert r["report_numbers"] == ["FHWA-OR-RD-07-08"] and r["doc_type"] == "Tech Report"
    assert r["other_urls"] == ["https://www.oregon.gov/odot/Programs/ResearchDocuments/DIP.pdf"]
    j = ris_to_record(recs[1], source="import", collection="test")
    assert j["id"] == "import:12345" and j["doi"] == "10.1016/S0022-4375(99)00001-X" and j["year"] == 1999
    assert j["landing_url"] == "https://doi.org/10.1016/S0022-4375(99)00001-X"


def test_import_is_idempotent(tmp_path: Path):
    f = tmp_path / "trid-export.ris"
    f.write_text(RIS)
    s = Store(tmp_path / "t.sqlite")
    out1 = import_ris(s, f)
    out2 = import_ris(s, f)
    assert out1["records"] == 2 and out2["total_in_store"] == 2
    assert s.source_distribution() == {"trid": 1, "import": 1}
    hits = s.search("negligent operator")
    assert hits[0]["id"] == "import:12345"
    assert [h["id"] for h in s.search("driver improvement", collection="TRID")] == ["trid:813520"]
    s.close()


def test_replace_source_keeps_imports(tmp_path: Path):
    live = Store(tmp_path / "live.sqlite")
    fresh = Store(tmp_path / "fresh.sqlite")
    base = {"oai_identifier": "x", "datestamp": "", "authors": [], "subjects": [], "report_numbers": [], "raw": {},
            "abstract": "", "year": 2000, "landing_url": ""}
    live.upsert_records([{**base, "id": "dot:1", "title": "old one", "collections": ["A"]},
                         {**base, "id": "dot:2", "title": "gone", "collections": []},
                         {**base, "id": "trid:9", "title": "kept import", "collections": ["TRID"]}])
    fresh.upsert_records([{**base, "id": "dot:1", "title": "new one", "collections": ["B"]},
                          {**base, "id": "dot:3", "title": "added", "collections": []}])
    fresh.close()
    n = live.replace_source("dot", Store(tmp_path / "fresh.sqlite"))
    assert n == 2
    ids = {r["id"] for r in [live.get_record(i) for i in ("dot:1", "dot:2", "dot:3", "trid:9")] if r}
    assert ids == {"dot:1", "dot:3", "trid:9"}
    assert live.get_record("dot:1")["title"] == "new one" and live.get_record("dot:1")["collections"] == ["B"]
    assert live.search("new one")[0]["id"] == "dot:1" and not live.search("gone")
    live.close()
