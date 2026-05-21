"""FTS5 search over span I/O and error messages."""
from fastapi.testclient import TestClient

import clustertrace
from clustertrace.dashboard.app import app


def test_search_finds_text_in_input():
    @clustertrace.trace
    def go(text):
        return text

    go("the quick brown fox")

    client = TestClient(app)
    hits = client.get("/api/search?q=fox").json()["results"]
    assert len(hits) >= 1
    assert "fox" in hits[0]["snippet"].lower()


def test_search_finds_error_messages():
    @clustertrace.trace
    def boom():
        raise ValueError("unique-magic-string-xyzzy")

    try: boom()
    except ValueError: pass

    client = TestClient(app)
    hits = client.get("/api/search?q=xyzzy").json()["results"]
    assert len(hits) >= 1


def test_empty_query_returns_no_results():
    client = TestClient(app)
    r = client.get("/api/search?q=")
    assert r.json()["results"] == []


def test_bad_fts_query_does_not_500():
    """Malformed FTS5 queries return empty results, not 500."""
    @clustertrace.trace
    def go(): pass
    go()

    client = TestClient(app)
    # FTS5 chokes on unbalanced parens
    r = client.get("/api/search?q=(((")
    assert r.status_code == 200
    assert "results" in r.json()


def test_phrase_search():
    @clustertrace.trace
    def go(text): return text
    go("rate limit exceeded for the api")
    go("everything went fine today")

    client = TestClient(app)
    # Phrase match — should hit only the first
    hits = client.get('/api/search?q="rate limit"').json()["results"]
    assert len(hits) == 1
