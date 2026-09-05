"""Regression checks for failures found in the September 2026 repository review."""
import asyncio
import gzip
import json
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from transport_lit import apis, config, embeddings, fulltext, graph, harvest, snapshot
from transport_lit.store import Store


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'transport-lit.sqlite')
    monkeypatch.setattr(config, 'RAW_DIR', tmp_path / 'raw')
    monkeypatch.setattr(config, 'PDF_DIR', tmp_path / 'pdf')
    monkeypatch.setattr(embeddings, 'VEC_DIR', tmp_path / 'vectors')
    config.ensure_dirs()
    s = Store(config.DB_PATH)
    yield s
    s.close()


def rec(rid='openalex:W1', title='Pedestrian safety', **kw):
    return {'id': rid, 'oai_identifier': 'api:' + rid, 'title': title,
            'authors': [], 'subjects': [], 'collections': [], 'raw': {}, **kw}


def fake_api(records):
    def pages(client, since, say):
        yield 'p00000', json.dumps(records).encode()
    return apis.ApiSource('openalex', 'Test', 'Test', pages, json.loads)


def test_fresh_cache_ids_and_reindex(local):
    first = harvest.harvest_api(local, fake_api([rec()]), mode='full')
    old_raw = config.RAW_DIR / f'run{first.run_id}-p00000.json.gz'
    before = old_raw.read_bytes()
    first_seen = local.get_record('openalex:W1')['first_seen_at']
    res = harvest.harvest_fresh_api(local, fake_api([rec(title='Updated pedestrian safety')]))
    assert res.status == 'complete' and res.run_id != first.run_id
    assert old_raw.read_bytes() == before
    assert local.get_record('openalex:W1')['first_seen_at'] == first_seen
    assert local.last_complete_run('openalex')['id'] == res.run_id
    assert harvest.reindex_api(local, fake_api([]))['pages'] == 1
    assert local.get_record('openalex:W1')['title'] == 'Updated pedestrian safety'


def test_fresh_refuses_silent_drop(local):
    local.upsert_records([rec('openalex:W' + str(i)) for i in range(20)])
    res = harvest.harvest_fresh_api(local, fake_api([rec()]))
    assert res.status == 'failed' and local.count() == 20
    assert local.get_run(res.run_id)['status'] == 'failed'


def test_missing_cache_does_not_modify_index(local):
    res = harvest.harvest_api(local, fake_api([rec()]), mode='full')
    local.upsert_records([rec('openalex:W2')])
    local.update_run(res.run_id, pages=2)
    with pytest.raises(RuntimeError, match='cannot reindex'):
        harvest.reindex_api(local, fake_api([]))
    assert local.count() == 2


def test_status_exposes_latest_failed_source(local):
    good = local.start_run('openalex', 'full', None, None)
    local.update_run(good, status='complete')
    bad = local.start_run('openalex', 'incremental', None, None)
    local.update_run(bad, status='failed')
    s = harvest.status(local)['sources']['openalex']
    assert s['latest_run']['status'] == 'failed'
    assert s['last_complete_run']['id'] == good


def test_columns_exact_url_and_empty_lookup(local):
    local.upsert_records([rec(title='Pedestrian safety', authors=['Lynn, A'], report_numbers=['813 097'], landing_url='https://example.org/report'),
                          rec('openalex:W2', title='Other report', abstract='pedestrian')])
    assert [r['id'] for r in local.search('title:pedestrian')] == ['openalex:W1']
    assert local.search('authors:lynn')[0]['id'] == 'openalex:W1'
    assert local.search('report_numbers:"813 097"')[0]['id'] == 'openalex:W1'
    assert local.lookup('https://example.org/report')[0]['id'] == 'openalex:W1'
    assert local.lookup('') == []


def test_doctor_same_day_and_external_content(local):
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    run = local.start_run('openalex', 'full', None, None)
    local.update_run(run, started_at=old)
    local.upsert_records([rec()])
    local.conn.execute("INSERT INTO records_fts(records_fts) VALUES ('delete-all')")
    local.conn.commit()
    d = local.doctor()
    assert len(d['runs_still_running']) == 1
    assert d['records_fts'].startswith('inconsistent:')
    local.repair()
    assert local.search('pedestrian')


def test_pdf_failed_download_is_not_cached(local, monkeypatch):
    monkeypatch.setattr(fulltext._limiter, 'wait', lambda: None)
    monkeypatch.setattr(config, 'MAX_PDF_BYTES', 3)
    dest = config.PDF_DIR / 'test.pdf'
    dest.write_bytes(b'old')
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'too large'))) as c:
        with pytest.raises(ValueError, match='exceeds'):
            fulltext._download('https://example.org/a.pdf', dest, c)
    assert dest.read_bytes() == b'old'
    assert list(config.PDF_DIR.glob('*.part')) == []
    assert fulltext.get_fulltext(local, 'dot:missing')['status'] == 'not_found'
    assert fulltext.direct_pdf_urls(rec(other_urls=['https://example.org/r.pdf?download=1'])) == ['https://example.org/r.pdf?download=1']


def test_pubmed_doi_does_not_come_from_reference_list():
    raw = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>A</ArticleTitle></Article></MedlineCitation>
    <PubmedData><ReferenceList><Reference><ArticleIdList><ArticleId IdType="doi">10.1234/other</ArticleId></ArticleIdList></Reference></ReferenceList></PubmedData>
    </PubmedArticle></PubmedArticleSet>'''
    assert apis.pubmed_parse(raw)[0]['doi'] == ''
    with pytest.raises(ValueError):
        apis.pubmed_parse(b'<eFetchResult><ERROR>History expired</ERROR></eFetchResult>')


def test_openalex_incremental_free_and_key(monkeypatch):
    monkeypatch.delenv('TRANSPORT_LIT_OPENALEX_USE_UPDATED_FILTER', raising=False)
    monkeypatch.delenv('DOT_LIT_OPENALEX_USE_UPDATED_FILTER', raising=False)
    monkeypatch.setenv('TRANSPORT_LIT_OPENALEX_API_KEY', 'test-secret')
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(200, json={'meta': {'count': 0, 'next_cursor': None}, 'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        list(apis.openalex_pages(c, '2026-09-01T00:00:00Z', lambda s: None))
    assert 'from_updated_date' not in calls[0].url.params['filter']
    assert calls[0].url.params['api_key'] == 'test-secret'
    assert calls[0].url.params['per-page'] == '100'


def test_http_errors_do_not_leak_key_or_retry_400():
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(400)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(RuntimeError) as err:
            apis._get(c, apis.RateLimiter(0), 'https://example.org/api', {'api_key': 'test-secret'})
    assert 'test-secret' not in str(err.value) and len(calls) == 1


def test_semantic_falls_back_when_no_vectors(local):
    local.upsert_records([rec()])
    hits, used = embeddings.hybrid_search(local, 'pedestrian', mode='semantic')
    assert used == 'keyword' and hits[0]['id'] == 'openalex:W1'


def test_graph_releases_write_lock_between_requests(local, monkeypatch):
    local.upsert_records([rec()])
    g = graph.Graph(local)
    g._upsert_work({'openalex_id': 'W1', 'title': 'Pedestrian safety'}, record_id='openalex:W1')
    local.conn.commit()
    def fetch(path, params=None):
        assert not local.conn.in_transaction
        if path == '/works/W1':
            return {'id': 'https://openalex.org/W1', 'referenced_works': ['https://openalex.org/W2']}
        return {'results': [{'id': 'https://openalex.org/W2', 'display_name': 'Cited work'}]}
    monkeypatch.setattr(graph, 'fetch', fetch)
    g.prefetch_edges()
    assert not local.conn.in_transaction
    assert g.references('openalex:W1')['n'] == 1
    assert graph._norm_title('交通安全') == '交通安全'


def test_snapshot_invalid_leaves_live_database(local, tmp_path):
    local.upsert_records([rec()])
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz'):
        pass
    with pytest.raises(FileNotFoundError):
        snapshot.install(str(archive), force=True, progress=lambda x: None)
    assert local.count() == 1


def test_snapshot_roundtrip_with_open_connection(local, tmp_path):
    local.upsert_records([rec()])
    archive = tmp_path / 'snapshot.tar.gz'
    snapshot.build(local, archive, include_vectors=False, progress=lambda x: None)
    local.upsert_records([rec('openalex:W2')])
    result = snapshot.install(str(archive), force=True, progress=lambda x: None)
    assert result['records_now'] == 1
    assert local.count() == 1


def test_mcp_stdio_protocol(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        env = {**os.environ, 'TRANSPORT_LIT_DATA_DIR': str(tmp_path), 'TRANSPORT_LIT_ENV_FILE': '/dev/null'}
        params = StdioServerParameters(command=sys.executable, args=['-m', 'transport_lit.server'], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                assert len(result.tools) == 12
                for name, args in [('search_reports', {'query': 'pedestrian', 'mode': 'keyword'}), ('harvest_status', {}), ('get_report', {'id': 'dot:1'})]:
                    result = await session.call_tool(name, args)
                    payload = result.model_dump(by_alias=True)
                    assert not payload.get('isError', False), payload
    asyncio.run(asyncio.wait_for(run(), timeout=30))


def test_mcp_annotations_and_http_startup(monkeypatch):
    from types import SimpleNamespace
    from transport_lit import server
    assert server.RO.model_dump(by_alias=True)['readOnlyHint'] is True
    assert server.RO_NET.model_dump(by_alias=True)['openWorldHint'] is True
    for major in (1, 2):
        calls = []
        fake = SimpleNamespace(run=lambda **kw: calls.append(kw))
        if major == 1:
            fake.settings = SimpleNamespace(host=None, port=None)
        monkeypatch.setattr(server, 'mcp', fake)
        monkeypatch.setattr(sys, 'argv', ['transport-lit-mcp', '--transport', 'streamable-http', '--port', '8888'])
        server.main()
        if major == 1:
            assert fake.settings.port == 8888 and calls == [{'transport': 'streamable-http'}]
        else:
            assert calls == [{'transport': 'streamable-http', 'host': '127.0.0.1', 'port': 8888}]


def test_snapshot_excludes_vectors_and_linked_graph(local, tmp_path):
    import numpy as np
    local.upsert_records([rec(), rec('cinii:1')])
    idx = embeddings.VectorIndex(config.DATA_DIR / 'vectors' / 'test')
    idx.add(['openalex:W1', 'cinii:1'], np.array([[1., 0.], [0., 1.]]), {'backend': 'fake'})
    local.set_meta('embeddings.active', 'test')
    g = graph.Graph(local)
    g._upsert_work({'openalex_id': 'W2', 'title': 'Excluded metadata'}, record_id='cinii:1')
    local.conn.commit()
    archive = tmp_path / 'public.tar.gz'
    snapshot.build(local, archive, progress=lambda x: None)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert len(names) == len(set(names))
        assert json.load(tar.extractfile('vectors/test/ids.json')) == ['openalex:W1']
        extract = tmp_path / 'inspection'
        tar.extractall(extract, filter='data')
    with sqlite3.connect(extract / 'transport-lit.sqlite') as c:
        assert c.execute('SELECT count(*) FROM works').fetchone()[0] == 0


def test_semantic_filters_precede_top_k(local, monkeypatch):
    import numpy as np
    local.upsert_records([rec('dot:1')] + [rec(f'cinii:{i}') for i in range(350)])
    idx = embeddings.VectorIndex(config.DATA_DIR / 'vectors' / 'filter-test')
    idx.add(['dot:1'] + [f'cinii:{i}' for i in range(350)], np.array([[0., 1.]] + [[1., 0.]] * 350), {})
    local.set_meta('embeddings.active', 'filter-test')
    class Backend:
        def embed_query(self, query):
            return np.array([1., 0.])
    monkeypatch.setattr(embeddings, 'backend_for', lambda index: Backend())
    hits, used = embeddings.hybrid_search(local, 'safety', mode='semantic', limit=1, sources=['dot'])
    assert used == 'semantic' and [h['id'] for h in hits] == ['dot:1']


def test_oai_fresh_roundtrip(local, monkeypatch):
    from transport_lit.oai import OAIClient, TruncatedList
    raw = b'''<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><record>
    <header><identifier>oai:dot.stacks:dot:1</identifier><datestamp>2026-01-01T00:00:00Z</datestamp></header>
    <metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Pedestrian safety</dc:title></oai_dc:dc></metadata></record></ListRecords></OAI-PMH>'''
    monkeypatch.setattr(OAIClient, '_get', lambda self, params: b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><Identify/></OAI-PMH>' if params['verb'] == 'Identify' else raw)
    harvest.harvest(local, mode='full')
    res = harvest.harvest_fresh(local)
    assert res.status == 'complete' and res.run_id == 2
    assert harvest.reindex(local)['pages'] == 1
    assert local.get_record('dot:1')['title'] == 'Pedestrian safety'
    with pytest.raises(TruncatedList):
        OAIClient._parse(b'<html><body>Maintenance</body></html>')


def test_reindex_accepts_cached_no_change_envelope(local):
    local.upsert_records([rec('dot:1')])
    run = local.start_run('rosap', 'full', None, None)
    local.update_run(run, status='complete', pages=0)
    (config.RAW_DIR / f'run{run}-p00000.xml.gz').write_bytes(gzip.compress(b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"/>'))
    assert harvest.reindex(local)['pages'] == 0
    assert local.count() == 1
