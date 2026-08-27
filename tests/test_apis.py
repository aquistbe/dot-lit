import json

from transport_lit.apis import cinii_parse, openalex_parse, pubmed_parse, pubmed_term


def test_openalex_parse():
    raw = json.dumps({"results": [{
        "id": "https://openalex.org/W123", "doi": "https://doi.org/10.1/x", "display_name": "Road Safety Report",
        "publication_year": 2020, "type": "report", "language": "en",
        "authorships": [{"author": {"display_name": "A. Author"}, "institutions": [{"display_name": "Uni"}]}],
        "primary_location": {"landing_page_url": "https://ex.org/r", "pdf_url": None, "source": {"display_name": "Ex Press"}},
        "best_oa_location": {"pdf_url": "https://ex.org/r.pdf"},
        "abstract_inverted_index": {"safety": [1], "Road": [0]},
        "topics": [{"display_name": "Traffic and Road Safety"}], "keywords": [{"display_name": "pedestrians"}]}]}).encode()
    r = openalex_parse(raw)[0]
    assert r["id"] == "openalex:W123" and r["doi"] == "10.1/x" and r["abstract"] == "Road safety"
    assert r["other_urls"] == ["https://ex.org/r.pdf"] and r["collections"] == ["OpenAlex reports", "OpenAlex: Traffic and Road Safety"]
    assert r["authors"] == ["A. Author"] and r["corporate_authors"] == ["Uni"] and r["subjects"] == ["pedestrians", "Traffic and Road Safety"]


def test_cinii_parse():
    raw = json.dumps({"items": [{"@id": "https://cir.nii.ac.jp/crid/139", "title": "交通安全の研究", "dc:creator": "山田太郎",
                                  "prism:publicationDate": "1983-08", "dc:type": "Article",
                                  "dc:identifier": [{"@type": "cir:DOI", "@value": "10.11501/2833233"}]}]}, ensure_ascii=False).encode()
    r = cinii_parse(raw)[0]
    assert r["id"] == "cinii:139" and r["year"] == 1983 and r["authors"] == ["山田太郎"] and r["doi"] == "10.11501/2833233"


def test_pubmed_parse_and_term():
    xml = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
    <Journal><Title>Accident Analysis and Prevention</Title><JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue></Journal>
    <ArticleTitle>Pedestrian crashes</ArticleTitle><Abstract><AbstractText>Text.</AbstractText></Abstract>
    <AuthorList><Author><LastName>Quistberg</LastName><ForeName>D A</ForeName></Author></AuthorList><Language>eng</Language>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article>
    <MeshHeadingList><MeshHeading><DescriptorName>Accidents, Traffic</DescriptorName></MeshHeading></MeshHeadingList></MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1016/j.aap.2019.1</ArticleId><ArticleId IdType="pmc">PMC99</ArticleId></ArticleIdList></PubmedData>
    </PubmedArticle></PubmedArticleSet>"""
    r = pubmed_parse(xml)[0]
    assert r["id"] == "pubmed:1" and r["year"] == 2019 and r["doi"] == "10.1016/j.aap.2019.1"
    assert r["authors"] == ["Quistberg, D A"] and r["subjects"] == ["Accidents, Traffic"]
    assert r["other_urls"] == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC99/"]
    t = pubmed_term()
    assert t.startswith("(((") and '"Accid Anal Prev"[Journal]' in t
