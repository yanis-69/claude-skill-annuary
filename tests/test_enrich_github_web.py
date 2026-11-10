"""Enrichissement, client GitHub, découverte et application web."""

import httpx
import pytest
from fastapi.testclient import TestClient

from skillshub import config, db, discover, enrich
from skillshub.github import GitHubClient, GitHubError, NotFound

from .conftest import seed

# ---------------------------------------------------------------- enrichissement

SEO = {"name": "shopify-seo", "path": "skills/shopify-seo", "size": 3000, "has_scripts": True,
       "description": "Audit SEO for Shopify stores: keyword research and meta description fixes.",
       "body": "Use this skill to improve search engine ranking of a storefront."}


def test_heuristique():
    r = enrich.enrich_heuristic(SEO)
    assert r["enriched_by"] == "heuristic" and r["language"] == "en"
    assert {"seo", "ecom"} <= set(r["tags"]) and r["category"] in ("ecommerce", "marketing")
    assert r["summary_fr"] == SEO["description"]
    assert 3 <= len(r["advantages"]) <= 5
    assert "Inclut des scripts prêts à l'emploi" in r["advantages"]
    assert set(r["tags"]) <= set(enrich.TAGS) and r["category"] in enrich.CATEGORIES
    assert r["compat"] == ["claude-code"]


def test_heuristique_skill_vide_et_langue():
    r = enrich.enrich_heuristic({"name": "x"})
    assert r["category"] == "other" and r["tags"] == [] and r["advantages"] and r["summary_fr"]
    assert enrich.detect_language("Utiliser ce skill pour les fichiers dans le dossier") == "fr"
    assert enrich.detect_language("这个技能用于处理文档和表格的自动化工作流程") == "zh"
    assert "codex" in enrich.detect_compat({"body": "Works with Claude Code and Codex CLI"})


def test_validate_et_extract_json():
    raw = ('```json\n{"summary_fr": "Un résumé suffisamment long pour être valide.", '
           '"advantages": ["a", "b", "c", "d", "e", "f"], "category": "Testing", '
           '"tags": ["testing", "dev", "bidon", "DEV"], "compat": [], "language": "EN-us"}\n```')
    r = enrich.validate(enrich.extract_json(raw))
    assert r["category"] == "testing" and r["tags"] == ["testing", "dev"]
    assert len(r["advantages"]) == 5 and r["compat"] == ["claude-code"] and r["language"] == "en"
    for bad in ('{"summary_fr": "court"}', "aucun json", '{"summary_fr": "' + "x" * 30 + '", '
                '"advantages": ["a", "b"], "category": "inventée"}', "[1]"):
        with pytest.raises(enrich.InvalidEnrichment):
            enrich.validate(enrich.extract_json(bad))


def test_json_invalide_un_retry_puis_repli(monkeypatch):
    calls = []
    monkeypatch.setitem(enrich.CALLERS, "claude", lambda p: calls.append(p) or "pas du json")
    r = enrich.enrich(SEO, "claude")
    assert len(calls) == 2 and r["enriched_by"] == "heuristic"
    assert "<skill>" in calls[0] and "shopify-seo" in calls[0]

    def missing(_prompt):
        raise FileNotFoundError("claude")

    monkeypatch.setitem(enrich.CALLERS, "codex", missing)
    assert enrich.enrich(SEO, "codex")["enriched_by"] == "heuristic"


def test_resolve_backend(monkeypatch):
    monkeypatch.setattr(enrich.shutil, "which", lambda name: None)
    assert enrich.resolve_backend("auto") == "heuristic"
    monkeypatch.setattr(enrich.shutil, "which", lambda name: "x" if name == "codex" else None)
    assert enrich.resolve_backend("auto") == "codex"
    assert enrich.resolve_backend("heuristic") == "heuristic"
    with pytest.raises(ValueError):
        enrich.resolve_backend("gpt")


# ---------------------------------------------------------------- client GitHub

def _client(handler, **kw):
    sleeps = []
    return GitHubClient("t", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw), sleeps


def test_pause_quota_puis_reprise():
    n = {"i": 0}

    def handler(request):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(403, json={"message": "API rate limit exceeded"},
                                  headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"})
        return httpx.Response(200, json={"ok": True}, headers={
            "etag": '"e"', "x-ratelimit-remaining": "41", "x-ratelimit-reset": "1"})

    gh, sleeps = _client(handler)
    data, etag = gh.get_repo("a/b")
    assert data == {"ok": True} and etag == '"e"' and len(sleeps) == 1 and gh.remaining == 41


def test_retry_5xx_404_et_etag():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={})
        if request.url.path.endswith("/down"):
            return httpx.Response(502)
        if request.headers.get("if-none-match") == '"e1"':
            return httpx.Response(304)
        return httpx.Response(500) if len(seen) == 1 else httpx.Response(200, json={"id": 1})

    gh, sleeps = _client(handler, max_retries=2)
    assert gh.get_repo("a/b")[0] == {"id": 1} and sleeps == [2.0]
    assert gh.get_repo("a/b", etag='"e1"') == (None, '"e1"')
    assert seen[-1].headers["authorization"] == "Bearer t"
    with pytest.raises(NotFound):
        gh.get_repo("a/missing")
    with pytest.raises(GitHubError):
        gh.get_repo("a/down")


def test_quota_trop_long_abandonne():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "5000"})

    gh, sleeps = _client(handler, max_wait=90)
    with pytest.raises(GitHubError, match="quota"):
        gh.get_repo("a/b")
    assert sleeps == []


# ---------------------------------------------------------------- découverte

def test_decoupage_de_la_recherche_de_code(monkeypatch):
    queries = []

    def handler(request):
        q = request.url.params["q"]
        page = int(request.url.params["page"])
        queries.append((q, page))
        if "size:0..500" in q:                       # tranche trop grosse : à redécouper
            return httpx.Response(200, json={"total_count": 1500, "items": []})
        if "size:0..250" in q:
            items = [{"repository": {"full_name": f"o/r{page}-{i}"}} for i in range(100 if page == 1 else 20)]
            return httpx.Response(200, json={"total_count": 120, "items": items})
        if "size:251..500" in q:
            return httpx.Response(200, json={"total_count": 1, "items": [
                {"repository": {"full_name": "O/R1-0"}}, {"repository": {"full_name": "new/one"}}]})
        return httpx.Response(200, json={"total_count": 0, "items": []})

    gh, _ = _client(handler)
    monkeypatch.setattr(discover, "SIZE_MAX", 500)
    found = discover.Found()
    discover.discover_code(gh, found, lambda _m: None)
    assert len(found) == 121                         # 120 + new/one, doublon de casse ignoré
    assert ("filename:SKILL.md size:0..250", 2) in queries
    assert queries[-1][0] == "filename:SKILL.md size:>=501"


def test_seeds_et_limite(tmp_path, monkeypatch):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("# commentaire\nacme/one\nhttps://github.com/acme/two/  # note\n\n", "utf-8")
    monkeypatch.setattr(config, "SEEDS_PATH", seeds)
    assert discover.read_seeds() == ["anthropics/skills", "acme/one", "acme/two"]
    found = discover.Found(limit=2)
    discover.discover_seeds(found, lambda _m: None)
    assert found.full and found["acme/one"] == "seed"


# ---------------------------------------------------------------- application web

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "web.db")
    conn = db.connect()
    seed(conn, "anthropics/skills", stars=5000, skills=[
        {"path": "skills/pdf", "name": "pdf", "summary": "Remplit des formulaires PDF.",
         "category": "documents", "tags": ["docs"], "advantages": ["Gain de temps"],
         "install_type": "plugin", "has_scripts": 1},
        {"path": "skills/xlsx", "name": "xlsx", "summary": "Tableurs.", "category": "data",
         "tags": ["data"], "install_type": "plugin"}])
    seed(conn, "acme/shop", stars=5, skills=[
        {"path": "seo-audit", "name": "seo-audit", "summary": "Audit SEO <b>Shopify</b>.",
         "category": "marketing", "tags": ["seo", "ecom"]}])
    db.finish_run(conn, db.start_run(conn, "incremental", "heuristic"), {"repos_seen": 2}, [], 4990)
    conn.close()
    from skillshub.web.app import app
    return TestClient(app)


def test_accueil_et_partiel_htmx(client):
    page = client.get("/")
    assert page.status_code == 200 and "<form" in page.text and "seo-audit" in page.text
    assert "3</strong> skills" in page.text
    assert "&lt;b&gt;Shopify" in page.text                       # contenu échappé
    partial = client.get("/?q=pdf", headers={"HX-Request": "true", "HX-Target": "results"})
    assert "<form" not in partial.text and "<html" not in partial.text
    assert 'href="/skill/1"' in partial.text and "seo-audit" not in partial.text
    restore = client.get("/?q=pdf", headers={"HX-Request": "true", "HX-Target": "results",
                                             "HX-History-Restore-Request": "true"})
    assert "<html" in restore.text


def test_filtres_tri_et_pagination_par_url(client):
    r = client.get("/?tags=seo&tags=ecom&sort=name&min_stars=abc&since=999")
    assert r.status_code == 200 and "seo-audit" in r.text and 'href="/skill/1"' not in r.text
    assert 'value="seo" checked' in r.text
    assert client.get("/?official=1&category=data").text.count('class="card"') == 1
    assert "Aucun skill ne correspond" in client.get("/?q=introuvable").text
    assert client.get("/?page=99").status_code == 200


def test_pages_skill_repo_stats(client):
    skill = client.get("/skill/1")
    assert skill.status_code == 200
    for expected in ("Remplit des formulaires PDF.", "Gain de temps", "Officiel Anthropic",
                     "/plugin marketplace add anthropics/skills", "xlsx",
                     "https://github.com/anthropics/skills/tree/main/skills/pdf"):
        assert expected in skill.text
    other = client.get("/skill/3").text
    assert "git clone --depth 1 https://github.com/acme/shop" in other
    assert "cp -r shop/seo-audit ~/.claude/skills/seo-audit" in other
    assert client.get("/skill/999").status_code == 404

    repo = client.get("/repo/anthropics/skills")
    assert repo.status_code == 200 and "2 skills" in repo.text
    assert client.get("/repo/nobody/nothing").status_code == 404

    stats = client.get("/stats")
    assert stats.status_code == 200 and "Dernier crawl" in stats.text and "4990" in stats.text
    assert "anthropics" in stats.text and "Documents" in stats.text


def test_api_json(client):
    data = client.get("/api/skills?q=seo&per_page=1").json()
    assert data["total"] == 1 and data["sort"] == "relevance" and data["per_page"] == 1
    item = data["items"][0]
    assert item["name"] == "seo-audit" and item["tags"] == ["ecom", "seo"] and item["url"] == "/skill/3"
    everything = client.get("/api/skills").json()
    assert everything["total"] == 3 and everything["items"][0]["stars"] == 5000
    assert client.get("/static/htmx.min.js").status_code == 200
