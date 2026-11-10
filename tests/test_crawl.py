"""Détection de changement : 304, pushed_at, tree_sha et blob SHA."""

import pytest

from skillshub import db, enrich
from skillshub.crawl import Crawler, run

REPO = "acme/tools"


@pytest.fixture
def enriched(monkeypatch):
    """Compte les appels d'enrichissement."""
    calls = []
    original = enrich.enrich

    def spy(skill, backend="heuristic", log=None):
        calls.append(skill["path"])
        return original(skill, "heuristic", log)

    monkeypatch.setattr(enrich, "enrich", spy)
    return calls


@pytest.fixture
def crawler(conn, gh, fake):
    fake.add_repo(REPO, {"skills/pdf": ("sha-pdf-1", "Remplit des PDF"),
                         "skills/seo": ("sha-seo-1", "Audit SEO pour Shopify")},
                  extra_files=("skills/pdf/scripts/fill.py",))
    return Crawler(conn, gh, log=lambda _m: None)


def skills(conn):
    return {r["path"]: r for r in conn.execute("SELECT * FROM skills")}


def test_premier_crawl_ajoute_les_skills(crawler, conn, fake, enriched):
    assert crawler.process_repo(REPO, "seed") == "added"
    rows = skills(conn)
    assert set(rows) == {"skills/pdf", "skills/seo"}
    pdf = rows["skills/pdf"]
    assert pdf["skill_md_sha"] == "sha-pdf-1" and pdf["has_scripts"] == 1
    assert pdf["install_type"] == "collection" and pdf["enriched_by"] == "heuristic"
    assert pdf["frontmatter_description"] == "Remplit des PDF"
    repo = conn.execute("SELECT * FROM repos").fetchone()
    assert repo["etag"] == 'W/"etag-1"' and repo["tree_sha"] and repo["source"] == "seed"
    assert conn.execute("SELECT stars FROM star_snapshots").fetchone()[0] == 10
    assert sorted(enriched) == ["skills/pdf", "skills/seo"]
    assert crawler.counters["skills_added"] == 2 and crawler.counters["repos_added"] == 1


def test_304_aucun_retraitement(crawler, conn, fake, enriched):
    crawler.process_repo(REPO)
    enriched.clear()
    fake.calls.clear()
    assert crawler.process_repo(REPO) == "unchanged"
    assert fake.calls == {"repo-304": 1}          # ni arbre, ni fichier
    assert enriched == []
    assert crawler.counters["repos_unchanged"] == 1


def test_metadonnees_modifiees_sans_push(crawler, conn, fake, enriched):
    crawler.process_repo(REPO)
    enriched.clear()
    fake.calls.clear()
    fake.touch(REPO, stargazers_count=42)         # l'ETag change, pas pushed_at
    assert crawler.process_repo(REPO) == "unchanged"
    assert fake.calls == {"repo-200": 1}
    assert conn.execute("SELECT stars, etag FROM repos").fetchone()[:] == (42, 'W/"etag-2"')
    assert enriched == []


def test_push_sans_changement_d_arbre(crawler, conn, fake, enriched):
    crawler.process_repo(REPO)
    enriched.clear()
    fake.calls.clear()
    fake.touch(REPO, pushed_at="2026-02-01T00:00:00Z")   # push sur une autre branche
    assert crawler.process_repo(REPO) == "unchanged"
    assert fake.calls == {"repo-200": 1, "tree": 1}
    assert enriched == []


def test_seul_le_skill_modifie_est_retraite(crawler, conn, fake, enriched):
    crawler.process_repo(REPO)
    before = skills(conn)
    enriched.clear()
    fake.calls.clear()
    fake.set_tree(REPO, {"skills/pdf": ("sha-pdf-2", "Remplit et fusionne des PDF"),
                         "skills/seo": ("sha-seo-1", "Audit SEO pour Shopify")})
    fake.touch(REPO, pushed_at="2026-02-01T00:00:00Z")
    assert crawler.process_repo(REPO) == "changed"
    assert enriched == ["skills/pdf"]
    assert fake.calls["file"] == 1
    after = skills(conn)
    assert after["skills/pdf"]["skill_md_sha"] == "sha-pdf-2"
    assert after["skills/pdf"]["frontmatter_description"] == "Remplit et fusionne des PDF"
    assert after["skills/pdf"]["id"] == before["skills/pdf"]["id"]
    assert after["skills/pdf"]["first_seen_at"] == before["skills/pdf"]["first_seen_at"]
    assert dict(after["skills/seo"]) == dict(before["skills/seo"])
    assert crawler.counters["skills_updated"] == 1 and crawler.counters["skills_added"] == 2


def test_nouveau_skill_et_skill_disparu(crawler, conn, fake, enriched):
    crawler.process_repo(REPO)
    enriched.clear()
    fake.set_tree(REPO, {"skills/pdf": ("sha-pdf-1", "Remplit des PDF"),
                         "skills/git": ("sha-git-1", "Aide aux commits git")})
    fake.touch(REPO, pushed_at="2026-02-01T00:00:00Z")
    crawler.process_repo(REPO)
    rows = skills(conn)
    assert enriched == ["skills/git"]
    assert rows["skills/seo"]["removed"] == 1          # marqué, pas supprimé
    assert rows["skills/git"]["removed"] == 0
    assert crawler.counters["skills_removed"] == 1
    assert [s["name"] for s in db.search_skills(conn, sort="name")[0]] == ["git", "pdf"]

    # Le skill réapparaît à l'identique : restauré sans ré-enrichissement.
    enriched.clear()
    fake.set_tree(REPO, {"skills/pdf": ("sha-pdf-1", "x"), "skills/git": ("sha-git-1", "x"),
                         "skills/seo": ("sha-seo-1", "x")})
    fake.touch(REPO, pushed_at="2026-03-01T00:00:00Z")
    crawler.process_repo(REPO)
    assert skills(conn)["skills/seo"]["removed"] == 0 and enriched == []


def test_full_retraite_tout(conn, gh, fake, crawler, enriched):
    crawler.process_repo(REPO)
    enriched.clear()
    fake.calls.clear()
    full = Crawler(conn, gh, full=True, log=lambda _m: None)
    assert full.process_repo(REPO) == "changed"
    assert fake.calls["repo-304"] == 0 and fake.calls["tree"] == 1
    assert sorted(enriched) == ["skills/pdf", "skills/seo"]
    assert len(skills(conn)) == 2


def test_fichier_inaccessible_retente_au_prochain_crawl(crawler, conn, fake, enriched):
    del fake.files[(REPO, "skills/seo/SKILL.md")]
    crawler.process_repo(REPO)
    assert set(skills(conn)) == {"skills/pdf"}
    repo = conn.execute("SELECT etag, tree_sha FROM repos").fetchone()
    assert repo["etag"] is None and repo["tree_sha"] is None   # incomplet : pas mémorisé
    assert len(crawler.errors) == 1
    fake.files[(REPO, "skills/seo/SKILL.md")] = "---\nname: seo\ndescription: d\n---\n"
    enriched.clear()
    assert crawler.process_repo(REPO) == "changed"
    assert enriched == ["skills/seo"] and set(skills(conn)) == {"skills/pdf", "skills/seo"}


def test_depot_disparu_et_fork_sans_commit(crawler, conn, fake):
    crawler.process_repo(REPO)
    del fake.repos[REPO]
    assert crawler.process_repo(REPO) == "gone"
    assert conn.execute("SELECT gone FROM repos").fetchone()[0] == 1
    assert db.search_skills(conn)[1] == 0

    fake.add_repo("someone/fork", {"s": ("sha", "d")}, fork=True,
                  created_at="2026-01-10T00:00:00Z", pushed_at="2026-01-01T00:00:00Z")
    db.add_candidates(conn, {"someone/fork": "topic", "ghost/none": "awesome"})
    assert crawler.process_repo("someone/fork") == "skipped-fork"
    assert crawler.process_repo("ghost/none") == "gone"
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 1


def test_plugin_marketplace(conn, gh, fake):
    fake.add_repo("acme/plugins", {"plugins/a/skills/x": ("sha-x", "Skill X")},
                  extra_files=(".claude-plugin/marketplace.json",))
    Crawler(conn, gh, log=lambda _m: None).process_repo("acme/plugins")
    assert skills(conn)["plugins/a/skills/x"]["install_type"] == "plugin"


def test_run_journalise_et_relance_incrementale(conn, gh, fake, enriched):
    fake.add_repo(REPO, {"skills/pdf": ("sha-pdf-1", "Remplit des PDF")})
    fake.add_repo("anthropics/skills", {"skills/docx": ("sha-docx", "Documents Word")}, stars=999)
    db.add_candidates(conn, {REPO: "seed", "anthropics/skills": "seed"})
    log = lambda _m: None  # noqa: E731

    first = run(conn, gh, no_discover=True, log=log)
    assert first["counters"]["repos_added"] == 2 and first["counters"]["skills_added"] == 2
    assert conn.execute("SELECT is_official FROM repos WHERE owner = 'anthropics'").fetchone()[0] == 1

    enriched.clear()
    second = run(conn, gh, no_discover=True, log=log)
    assert second["counters"]["repos_unchanged"] == 2 and second["counters"]["enriched"] == 0
    assert enriched == []

    runs = conn.execute("SELECT * FROM crawl_runs ORDER BY id").fetchall()
    assert len(runs) == 2
    assert runs[0]["repos_seen"] == 2 and runs[0]["finished_at"] and runs[0]["errors"] == "[]"
    assert runs[1]["repos_unchanged"] == 2 and runs[1]["rate_remaining"] == 4999
    assert runs[0]["mode"] == "incremental"


def test_limit_et_ordre_de_reprise(conn, gh, fake):
    for name in ("a/one", "b/two", "c/three"):
        fake.add_repo(name, {"s": (f"sha-{name}", "d")})
    db.add_candidates(conn, {"a/one": "seed", "b/two": "seed", "c/three": "seed"})
    log = lambda _m: None  # noqa: E731
    run(conn, gh, no_discover=True, limit=2, log=log)
    assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 2
    # Le candidat restant passe avant les dépôts déjà vérifiés.
    run(conn, gh, no_discover=True, limit=1, log=log)
    assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0


def test_enrich_only_remplace_l_heuristique(conn, gh, fake, monkeypatch):
    fake.add_repo(REPO, {"skills/pdf": ("sha-pdf-1", "Remplit des PDF")})
    Crawler(conn, gh, log=lambda _m: None).process_repo(REPO)
    answer = ('{"summary_fr": "Ce skill remplit des formulaires PDF automatiquement.", '
              '"advantages": ["Gain de temps", "Moins d\'erreurs", "Scripts inclus"], '
              '"category": "documents", "tags": ["docs", "automation", "inconnu"], '
              '"compat": ["claude-code", "api"], "language": "fr"}')
    monkeypatch.setitem(enrich.CALLERS, "claude", lambda prompt: "Voici :\n" + answer)
    result = run(conn, gh, enrich_only=True, limit=5, backend="claude", log=lambda _m: None)
    assert result["counters"]["enriched"] == 1
    skill = db.search_skills(conn)[0][0]
    assert skill["enriched_by"] == "claude" and skill["category"] == "documents"
    assert skill["tags"] == ["automation", "docs"] and skill["compat"] == ["claude-code", "api"]
    assert skill["summary"].startswith("Ce skill remplit")
    assert [s["name"] for s in db.search_skills(conn, {"q": "formulaires"})[0]] == ["pdf"]
    # Plus rien à ré-enrichir.
    assert run(conn, gh, enrich_only=True, backend="claude", log=lambda _m: None)["counters"]["enriched"] == 0
