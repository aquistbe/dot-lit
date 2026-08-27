import xml.etree.ElementTree as ET

from dot_lit.dc import parse_record, local_id_from_oai

SAMPLE = """<record xmlns="http://www.openarchives.org/OAI/2.0/">
<header><identifier>oai:dot.stacks:dot:93144</identifier><datestamp>2026-08-25T17:13:17Z</datestamp></header>
<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier>https://rosap.ntl.bts.gov/view/dot/93144</dc:identifier>
<dc:title>Exploring Driver Adaptation</dc:title>
<dc:subject>Driver behavior</dc:subject><dc:subject>Speeding</dc:subject>
<dc:date>2026</dc:date>
<dc:publisher>NHTSA</dc:publisher><dc:type>Tech Report</dc:type>
<dc:identifier.uri>https://doi.org/10.21949/hcf8-kh48 </dc:identifier.uri>
<dc:identifier.uri>DOT HS 813 827</dc:identifier.uri>
<dc:relation.isPartOf>NHTSA-Vehicle Safety Research;US Transportation Collection</dc:relation.isPartOf>
<dc:contributor.author>Klauer, S. G.</dc:contributor.author>
<dc:contributor.creator>Virginia Tech Transportation Institute</dc:contributor.creator>
<dc:description.abstract>Behavioral adaptation.</dc:description.abstract>
</oai_dc:dc></metadata></record>"""


def test_parse_record_derives_fields():
    d = parse_record(ET.fromstring(SAMPLE))
    assert d["id"] == "dot:93144"
    assert d["year"] == 2026
    assert d["doi"] == "10.21949/hcf8-kh48"
    assert d["report_numbers"] == ["DOT HS 813 827"]
    assert d["collections"] == ["NHTSA-Vehicle Safety Research", "US Transportation Collection"]
    assert d["authors"] == ["Klauer, S. G."]
    assert d["corporate_authors"] == ["Virginia Tech Transportation Institute"]
    assert d["abstract"] == "Behavioral adaptation."
    assert d["landing_url"] == "https://rosap.ntl.bts.gov/view/dot/93144"
    assert "identifier.uri" in d["raw"]


def test_local_id():
    assert local_id_from_oai("oai:dot.stacks:dot:1") == "dot:1"


LEGACY = """<record xmlns="http://www.openarchives.org/OAI/2.0/">
<header><identifier>oai:dot.stacks:dot:21848</identifier><datestamp>2017-08-24T17:06:22Z</datestamp></header>
<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier>https://rosap.ntl.bts.gov/view/dot/21848</dc:identifier>
<dc:title>Evaluation of the Oregon DMV driver improvement program.</dc:title>
<dc:creator>Strathman, James G.</dc:creator><dc:creator>Portland State University. Center for Urban Studies</dc:creator>
<dc:description>SPR 634</dc:description>
<dc:description>This report provides an evaluation of the DIP, which changed in 2002.</dc:description>
<dc:description>Final report; June 2007</dc:description>
<dc:type>Tech Report</dc:type>
</oai_dc:dc></metadata></record>"""


def test_parse_legacy_profile():
    d = parse_record(ET.fromstring(LEGACY))
    assert d["authors"] == ["Strathman, James G."]
    assert d["corporate_authors"] == ["Portland State University. Center for Urban Studies"]
    assert d["report_numbers"] == ["SPR 634"]
    assert d["abstract"].startswith("This report provides") and "Final report" in d["abstract"]
    assert d["year"] == 2007 and d["year_source"] == "description"
    assert d["raw"]["description"][0] == "SPR 634"


def test_year_from_title_when_no_date():
    x = LEGACY.replace("<dc:description>Final report; June 2007</dc:description>", "").replace(
        "driver improvement program.", "driver improvement program, Sixth Edition, 2011")
    d = parse_record(ET.fromstring(x))
    assert d["year"] == 2011 and d["year_source"] == "title"


def test_year_fallback_rejects_report_numbers_and_implausible_years():
    x = LEGACY.replace("<dc:description>Final report; June 2007</dc:description>",
                       "<dc:description>RC-1600</dc:description>")
    d = parse_record(ET.fromstring(x))
    assert d["year"] is None and d["year_source"] is None
    assert "RC-1600" in d["report_numbers"]
    y = LEGACY.replace("driver improvement program.", "roads of Virginia, 1607-1840.").replace(
        "<dc:description>Final report; June 2007</dc:description>", "<dc:description>2009</dc:description>")
    d = parse_record(ET.fromstring(y))
    assert d["year"] == 2009 and d["year_source"] == "description"
