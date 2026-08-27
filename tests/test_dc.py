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
