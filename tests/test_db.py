from datetime import datetime, timedelta, timezone

import pytest

from skillshub import db

from .conftest import seed


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def data(conn):
    seed(conn, "anthropics/skills", stars=5000, forks=300, pushed_at=_iso(2),
         created_at="2025-10-01T00:00:00Z", license="Apache-2.0", skills=[
             {"path": "skills/pdf", "name": "pdf", "summary": "Remplit des formulaires PDF.",
              "category": "documents", "tags": ["docs", "automation"], "has_scripts": 1,
              "compat": ["claude-code", "claude.ai", "api"], "skill_md_size": 12000,
              "install_type": "plugin"},
             {"path": "skills/xlsx", "name": "xlsx", "summary": "Tableurs et formules.",
              "category": "data", "tags": ["data", "docs"], "skill_md_size": 3000,
              "install_type": "plugin"},
         ])
    seed(conn, "acme/shop", stars=120, forks=4, pushed_at=_iso(45),
         created_at="2026-02-01T00:00:00Z", skills=[
             {"path": "seo-audit", "name": "seo-audit", "category": "marketing",
              "summary": "Audit de référencement pour boutiques Shopify.",
              "tags": ["seo", "ecom"], "language": "fr", "skill_md_size": 2000,
              "advantages": ["Améliore le classement Google"]},
         ])
    seed(conn, "old/archive", stars=900, forks=50, pushed_at=_iso(400), archived=1,
         created_at="2024-01-01T00:00:00Z", license=None, skills=[
             {"path": "", "name": "commit-helper", "category": "development",
              "summary": "Rédige des messages de commit.", "tags": ["git", "dev"],
              "compat": ["claude-code", "codex"]},
         ])
    return conn


def names(conn, filters=None, sort=None):
    return [s["name"] for s in db.search_skills(conn, filters or {}, sort)[0]]


def test_sans_filtre_tri_par_etoiles(data):
    skills, total = db.search_skills(data)
    assert total == 4
    assert [s["stars"] for s in skills] == [5000, 5000, 900, 120]
    assert skills[0]["tags"] == ["automation", "docs"] and skills[0]["tokens"] == 3000


@pytest.mark.parametrize("filters, expected", [
    ({"category": "marketing"}, {"seo-audit"}),
    ({"tags": ["docs"]}, {"pdf", "xlsx"}),
    ({"tags": ["docs", "data"]}, {"xlsx"}),          # multi-sélection : tous les tags
    ({"tags": "seo"}, {"seo-audit"}),
    ({"min_stars": 500}, {"pdf", "xlsx", "commit-helper"}),
    ({"since": 7}, {"pdf", "xlsx"}),
    ({"since": 90}, {"pdf", "xlsx", "seo-audit"}),
    ({"official": True}, {"pdf", "xlsx"}),
    ({"compat": ["codex"]}, {"commit-helper"}),
    ({"compat": ["claude.ai", "api"]}, {"pdf"}),
    ({"install_type": "plugin"}, {"pdf", "xlsx"}),
    ({"scripts": True}, {"pdf"}),
    ({"license": "Apache-2.0"}, {"pdf", "xlsx"}),
    ({"language": "fr"}, {"seo-audit"}),
    ({"light": True}, {"xlsx", "seo-audit", "commit-helper"}),
    ({"hide_archived": True}, {"pdf", "xlsx", "seo-audit"}),
    ({"author": "ACME"}, {"seo-audit"}),
    ({"category": "data", "official": True, "light": True}, {"xlsx"}),
    ({"category": "finance"}, set()),
])
def test_filtres(data, filters, expected):
    assert set(names(data, filters)) == expected


def test_tris(data):
    assert names(data, sort="name") == ["commit-helper", "pdf", "seo-audit", "xlsx"]
    assert names(data, sort="forks")[-1] == "seo-audit"
    assert names(data, sort="updated")[-1] == "commit-helper"
    assert names(data, sort="created")[0] == "seo-audit"
    assert names(data, sort="inconnu")[0] in ("pdf", "xlsx")  # repli sur les étoiles


def test_tri_tendance(data):
    acme = data.execute("SELECT id FROM repos WHERE full_name = 'acme/shop'").fetchone()[0]
    old = data.execute("SELECT id FROM repos WHERE full_name = 'old/archive'").fetchone()[0]
    past = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    db.add_star_snapshot(data, acme, 20, past)     # +100 en 40 jours
    db.add_star_snapshot(data, old, 890, past)     # +10
    skills, _ = db.search_skills(data, sort="trending")
    assert [s["name"] for s in skills[:2]] == ["seo-audit", "commit-helper"]
    assert skills[0]["trend"] == 100


def test_recherche_plein_texte(data):
    assert names(data, {"q": "formulaires"}) == ["pdf"]
    assert names(data, {"q": "shopify"}) == ["seo-audit"]          # résumé
    assert names(data, {"q": "ecom"}) == ["seo-audit"]             # tag
    assert names(data, {"q": "google"}) == ["seo-audit"]           # avantages
    assert names(data, {"q": "referencement"}) == ["seo-audit"]    # sans accent
    assert names(data, {"q": "formulair"}) == ["pdf"]                 # préfixe
    assert set(names(data, {"q": "docs"})) == {"pdf", "xlsx"}
    assert names(data, {"q": "docs", "category": "data"}) == ["xlsx"]
    assert names(data, {"q": 'pdf" OR (x'}) == []                  # saisie hostile : pas d'erreur
    assert names(data, {"q": "commit"}, sort="relevance") == ["commit-helper"]
    assert len(names(data, {"q": ""}, sort="relevance")) == 4      # pertinence sans recherche


def test_fts_suit_les_modifications(data):
    sid = data.execute("SELECT id FROM skills WHERE name = 'xlsx'").fetchone()[0]
    assert names(data, {"q": "budget"}) == []
    data.execute("UPDATE skills SET summary = 'Budget prévisionnel' WHERE id = ?", (sid,))
    assert names(data, {"q": "budget"}) == ["xlsx"]
    db.set_skill_tags(data, sid, ["finance"])
    assert names(data, {"q": "finance"}) == ["xlsx"]
    assert names(data, {"tags": ["data"]}) == []
    data.execute("DELETE FROM skills WHERE id = ?", (sid,))
    assert names(data, {"q": "budget"}) == []


def test_skills_retires_et_depots_disparus_masques(data):
    data.execute("UPDATE skills SET removed = 1 WHERE name = 'pdf'")
    data.execute("UPDATE repos SET gone = 1 WHERE full_name = 'old/archive'")
    assert set(names(data)) == {"xlsx", "seo-audit"}
    assert db.stats(data)["skills"] == 2


def test_pagination(data):
    page1, total = db.search_skills(data, sort="name", page=1, per_page=3)
    page2, _ = db.search_skills(data, sort="name", page=2, per_page=3)
    assert total == 4 and len(page1) == 3 and [s["name"] for s in page2] == ["xlsx"]


def test_upsert_skill_met_a_jour_sans_dupliquer(data):
    repo_id = "R_acme/shop"
    sid, created = db.upsert_skill(data, repo_id, "seo-audit", {"name": "seo-audit-v2", "tags": ["seo"]})
    assert not created
    assert data.execute("SELECT count(*) FROM skills WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1
    assert db.get_skill(data, sid)["tags"] == ["seo"]


def test_facettes_et_stats(data):
    f = db.facets(data)
    assert dict(f["categories"])["documents"] == 1
    assert dict(f["tags"])["docs"] == 2
    assert dict(f["compat"])["claude-code"] == 4
    st = db.stats(data)
    assert st["skills"] == 4 and st["repos"] == 3 and st["official"] == 2
    assert st["top_authors"][0]["owner"] == "anthropics"
    assert st["last_run"] is None


def test_migration_idempotente(tmp_path):
    path = tmp_path / "x.db"
    db.connect(path).close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    c.close()
