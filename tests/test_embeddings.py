import hashlib

import numpy as np

from dot_lit import embeddings as E
from dot_lit.store import Store


class FakeBackend:
    name = "fake"
    model = "fake-1"
    slug = "fake-fake-1"
    dim = 8

    def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.lower().encode()).digest()
            v = np.frombuffer(h[:32], dtype=np.uint8).astype(np.float32)[:8]
            # make "pedestrian"-ish texts cluster: add a shared component
            if "pedestrian" in t.lower() or "walker" in t.lower():
                v = v + 200.0
            out.append(v)
        return E._norm(np.stack(out))

    def embed_query(self, text):
        return self.embed([text])[0]


def _rec(i, title, abstract="", year=2000):
    return {"id": f"dot:{i}", "oai_identifier": "x", "datestamp": "", "title": title, "abstract": abstract,
            "year": year, "collections": [], "authors": [], "subjects": [], "report_numbers": [], "raw": {},
            "landing_url": ""}


def test_vector_index_add_search_append(tmp_path):
    idx = E.VectorIndex(tmp_path / "v")
    b = FakeBackend()
    idx.add(["a", "b"], b.embed(["pedestrian crash", "bridge overlay"]), {"backend": "fake", "model": "fake-1"})
    idx.add(["c"], b.embed(["walker safety"]), {"backend": "fake", "model": "fake-1"})
    assert len(idx) == 3 and idx.has("c") and idx.meta["n"] == 3 and idx.meta["dim"] == 8
    res = idx.search(b.embed_query("pedestrian injuries"), k=2)
    assert {r for r, _ in res} == {"a", "c"}
    reopened = E.VectorIndex(tmp_path / "v")
    assert len(reopened) == 3 and reopened.search(b.embed_query("walker"), k=1)[0][0] in ("a", "c")


def test_hybrid_search_and_fallback(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    s.upsert_records([_rec(1, "Pedestrian crash countermeasures", "walker safety", 2010),
                      _rec(2, "Bridge deck overlays", "", 2019),
                      _rec(3, "Sidewalk design for walkers", "", 1999)])
    hits, used = E.hybrid_search(s, "pedestrian", mode="hybrid", limit=5)
    assert used == "keyword" and [h["id"] for h in hits] == ["dot:1"]      # no vectors yet -> keyword
    monkeypatch.setattr(E, "VEC_DIR", tmp_path / "vectors")
    b = FakeBackend()
    res = E.embed_records(s, b, batch=2)
    assert res["index_size"] == 3 and s.get_meta("embeddings.active") == "fake-fake-1"
    monkeypatch.setattr(E, "backend_for", lambda idx: b)
    hits, used = E.hybrid_search(s, "pedestrian", mode="hybrid", limit=5)
    assert used == "hybrid" and hits[0]["id"] == "dot:1"
    assert any(h["id"] == "dot:3" and h["match_mode"] == "semantic" for h in hits)   # found by meaning only
    hits, used = E.hybrid_search(s, "pedestrian", mode="semantic", limit=5, year_min=1990, year_max=2005)
    assert used == "semantic" and [h["id"] for h in hits] == ["dot:3"]
    res2 = E.embed_records(s, b)
    assert res2["embedded_now"] == 0
    s.close()


def test_rrf():
    f = E.rrf([["a", "b"], ["b", "c"]])
    assert f["b"] > f["a"] > f["c"]
