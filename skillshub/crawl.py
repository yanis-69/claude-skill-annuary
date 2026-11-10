"""Orchestration du crawl incrémental (CLI).

Un skill déjà connu n'est retraité que si le blob SHA de son SKILL.md a changé.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import config, db, enrich
from .discover import discover_all
from .github import GitHubClient, GitHubError, NotFound
from .parse import find_skills_in_tree, install_type, is_plugin_repo, parse_skill_md

Log = Callable[[str], None]

# Téléchargements simultanés de SKILL.md (raw.githubusercontent.com, hors quota API).
DOWNLOAD_WORKERS = 8


def _print(msg: str) -> None:
    print(msg, flush=True)


def repo_from_api(data: dict[str, Any]) -> dict[str, Any]:
    """Convertit la réponse de `GET /repos/{full_name}` en ligne de la table repos."""
    owner = data.get("owner") or {}
    lic = data.get("license") or {}
    spdx = lic.get("spdx_id")
    if not spdx or spdx == "NOASSERTION":
        spdx = lic.get("name")
    login = owner.get("login") or data["full_name"].split("/")[0]
    return {
        "id": data["node_id"],
        "full_name": data["full_name"],
        "url": data.get("html_url") or f"https://github.com/{data['full_name']}",
        "owner": login,
        "owner_type": "org" if owner.get("type") == "Organization" else "user",
        "is_official": int(login.lower() in config.OFFICIAL_OWNERS),
        "description": data.get("description"),
        "stars": data.get("stargazers_count") or 0,
        "forks": data.get("forks_count") or 0,
        "open_issues": data.get("open_issues_count") or 0,
        "license": spdx,
        "topics": data.get("topics") or [],
        "default_branch": data.get("default_branch") or "main",
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "archived": int(bool(data.get("archived"))),
        "is_fork": int(bool(data.get("fork"))),
        "gone": 0,
    }


def fork_without_own_commits(data: dict[str, Any]) -> bool:
    """Un fork jamais poussé garde le `pushed_at` du dépôt parent, antérieur à sa création."""
    if not data.get("fork"):
        return False
    pushed, created = data.get("pushed_at"), data.get("created_at")
    return not pushed or not created or pushed <= created


class Crawler:
    def __init__(self, conn: sqlite3.Connection, gh: GitHubClient, backend: str = "heuristic",
                 full: bool = False, log: Log = _print):
        self.conn = conn
        self.gh = gh
        self.backend = backend
        self.full = full
        self.log = log
        self.counters = {k: 0 for k in db.RUN_COUNTERS}
        self.errors: list[str] = []

    # ------------------------------------------------------------------
    # Un dépôt
    # ------------------------------------------------------------------

    def _drop_candidate(self, *names: str) -> None:
        for name in names:
            self.conn.execute("DELETE FROM candidates WHERE full_name = ?", (name,))

    def _save_unchanged(self, repo: dict[str, Any]) -> str:
        db.upsert_repo(self.conn, repo)
        db.add_star_snapshot(self.conn, repo["id"], repo["stars"])
        self.conn.commit()
        self.counters["repos_unchanged"] += 1
        return "unchanged"

    def process_repo(self, full_name: str, source: str | None = None) -> str:
        """Traite un dépôt. Retourne : added, changed, unchanged, gone ou skipped-fork."""
        conn = self.conn
        self.counters["repos_seen"] += 1
        known = conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name,)).fetchone()

        # 1. Métadonnées du dépôt, avec ETag : un 304 ne consomme pas de quota.
        etag = known["etag"] if known and not self.full else None
        try:
            data, new_etag = self.gh.get_repo(full_name, etag)
        except NotFound:
            if known:
                conn.execute(
                    "UPDATE repos SET gone = 1, last_checked_at = ? WHERE id = ?",
                    (db.now_iso(), known["id"]),
                )
            self._drop_candidate(full_name)
            conn.commit()
            return "gone"

        if data is None:
            conn.execute(
                "UPDATE repos SET last_checked_at = ?, gone = 0 WHERE id = ?",
                (db.now_iso(), known["id"]),
            )
            db.add_star_snapshot(conn, known["id"], known["stars"])
            conn.commit()
            self.counters["repos_unchanged"] += 1
            return "unchanged"

        if not known:  # dépôt renommé : il est déjà connu sous un autre nom
            known = conn.execute("SELECT * FROM repos WHERE id = ?", (data["node_id"],)).fetchone()
        if not known and fork_without_own_commits(data):
            self._drop_candidate(full_name, data["full_name"])
            conn.commit()
            return "skipped-fork"

        repo = repo_from_api(data)
        repo.update(
            etag=new_etag, last_checked_at=db.now_iso(),
            tree_sha=known["tree_sha"] if known else None,
            source=(known["source"] if known else None) or source,
        )
        self._drop_candidate(full_name, repo["full_name"])

        # 2. Aucun push depuis le dernier passage : rien à retraiter.
        if known and not self.full and known["tree_sha"] and known["pushed_at"] == repo["pushed_at"]:
            return self._save_unchanged(repo)

        # 3. Arbre Git complet.
        try:
            tree = self.gh.get_tree(repo["full_name"], repo["default_branch"])
        except GitHubError as exc:
            if exc.status not in (404, 409):  # 409 : dépôt vide
                raise
            tree = {"sha": "empty", "tree": []}
        if known and not self.full and known["tree_sha"] == tree.get("sha"):
            return self._save_unchanged(repo)

        entries = tree.get("tree") or []
        truncated = bool(tree.get("truncated"))
        found = find_skills_in_tree(entries)
        kind = install_type(is_plugin_repo(entries), len(found))
        existing = {
            r["path"]: r
            for r in conn.execute("SELECT * FROM skills WHERE repo_id = ?", (repo["id"],))
        } if known else {}

        todo = [s for s in found
                if self.full or s["path"] not in existing
                or existing[s["path"]]["skill_md_sha"] != s["sha"]]
        complete = not truncated
        if len(todo) > config.MAX_SKILLS_PER_REPO:
            self.log(f"    {len(todo)} skills à traiter : limité à {config.MAX_SKILLS_PER_REPO} "
                     f"pour ce passage, la suite au prochain crawl")
            todo = todo[:config.MAX_SKILLS_PER_REPO]
            complete = False
        if truncated:
            self.errors.append(f"{repo['full_name']} : arbre tronqué par GitHub (dépôt trop gros)")

        # Téléchargement et parsing des SKILL.md nouveaux ou modifiés uniquement.
        def fetch(s: dict[str, Any]) -> str | GitHubError:
            try:
                return self.gh.get_file(repo["full_name"], repo["default_branch"], s["file"], s["sha"])
            except GitHubError as exc:
                return exc

        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(todo))) as pool:
                texts = list(pool.map(fetch, todo))
        else:
            texts = [fetch(s) for s in todo]

        infos: list[dict[str, Any]] = []
        for s, text in zip(todo, texts):
            if isinstance(text, GitHubError):
                self.errors.append(f"{repo['full_name']}/{s['file']} : {text}")
                complete = False
                continue
            fallback = posixpath.basename(s["path"]) or repo["full_name"].split("/")[1]
            parsed = parse_skill_md(text, fallback_name=fallback)
            infos.append({
                **s, "name": parsed["name"], "description": parsed["description"],
                "body": parsed["body"], "full_name": repo["full_name"],
                "repo_description": repo["description"], "topics": repo["topics"],
            })
        results = enrich.enrich_many(infos, self.backend, log=self.log)
        self.counters["enriched"] += len(results)

        # Écriture en une seule transaction. L'ETag et le tree_sha ne sont mémorisés que si
        # tout a été traité : sinon le prochain crawl reprend ce dépôt.
        if complete:
            repo["tree_sha"] = tree.get("sha")
        else:
            repo["tree_sha"] = None
            repo["etag"] = None
        db.upsert_repo(conn, repo)
        db.add_star_snapshot(conn, repo["id"], repo["stars"])
        now = db.now_iso()
        for info, res in zip(infos, results):
            _, created = db.upsert_skill(conn, repo["id"], info["path"], {
                "name": info["name"],
                "frontmatter_description": info["description"],
                "summary": res["summary_fr"],
                "advantages": res["advantages"],
                "category": res["category"],
                "tags": res["tags"],
                "compat": res["compat"],
                "language": res["language"],
                "enriched_by": res["enriched_by"],
                "enriched_at": now,
                "skill_md_sha": info["sha"],
                "skill_md_size": info["size"],
                "has_scripts": info["has_scripts"],
                "has_references": info["has_references"],
                "file_count": info["file_count"],
                "install_type": kind,
            })
            self.counters["skills_added" if created else "skills_updated"] += 1

        found_paths = {s["path"] for s in found}
        for path, row in existing.items():
            if path in found_paths:
                if row["removed"]:  # skill réapparu à l'identique
                    conn.execute("UPDATE skills SET removed = 0 WHERE id = ?", (row["id"],))
            elif not row["removed"] and not truncated:
                # Skill disparu : marqué, jamais supprimé.
                conn.execute(
                    "UPDATE skills SET removed = 1, updated_at = ? WHERE id = ?", (now, row["id"])
                )
                self.counters["skills_removed"] += 1
        conn.execute(
            "UPDATE skills SET install_type = ? WHERE repo_id = ? AND removed = 0 "
            "AND coalesce(install_type, '') <> ?",
            (kind, repo["id"], kind),
        )
        conn.commit()
        status = "changed" if known else "added"
        self.counters["repos_changed" if known else "repos_added"] += 1
        self.log(f"    {len(found)} skill(s), {len(infos)} traité(s)")
        return status

    # ------------------------------------------------------------------
    # Passes
    # ------------------------------------------------------------------

    def crawl(self, limit: int | None = None, only: list[str] | None = None) -> None:
        conn = self.conn
        # Candidats dans l'ordre de découverte : les seeds d'abord.
        queue: list[tuple[str, str | None]] = [
            (r["full_name"], r["source"])
            for r in conn.execute("SELECT * FROM candidates ORDER BY rowid")
        ]
        # Reprise possible : les dépôts vérifiés il y a le plus longtemps passent d'abord.
        queue += [
            (r["full_name"], None)
            for r in conn.execute(
                "SELECT full_name FROM repos WHERE gone = 0 "
                "ORDER BY last_checked_at IS NOT NULL, last_checked_at, full_name"
            )
        ]
        if only:  # dépôts désignés explicitement (--repo)
            queue = [(name, "manual") for name in only]
        if limit:
            queue = queue[:limit]
        self.log(f"Crawl de {len(queue)} dépôt(s), backend d'enrichissement : {self.backend}")
        for i, (full_name, source) in enumerate(queue, 1):
            try:
                status = self.process_repo(full_name, source)
                self.log(f"[{i}/{len(queue)}] {full_name} : {status}")
            except GitHubError as exc:
                conn.rollback()
                self.errors.append(f"{full_name} : {exc}")
                self.log(f"[{i}/{len(queue)}] {full_name} : ERREUR {exc}")
                if "quota" in str(exc):
                    self.log("Quota GitHub épuisé : arrêt. Relancer plus tard pour reprendre.")
                    break
            except Exception as exc:  # un dépôt défectueux ne doit pas arrêter le crawl
                conn.rollback()
                self.errors.append(f"{full_name} : {type(exc).__name__} : {exc}")
                self.log(f"[{i}/{len(queue)}] {full_name} : ERREUR {type(exc).__name__} : {exc}")

    def enrich_only(self, limit: int | None = None) -> None:
        """Ré-enrichit avec un CLI les skills restés en `heuristic`."""
        conn = self.conn
        sql = (
            "SELECT s.*, r.full_name, r.default_branch, r.description AS repo_description, "
            "r.topics FROM skills s JOIN repos r ON r.id = s.repo_id "
            "WHERE s.removed = 0 AND r.gone = 0 AND s.enriched_by = 'heuristic' "
            "ORDER BY r.stars DESC, s.id"
        )
        rows = conn.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
        self.log(f"Ré-enrichissement de {len(rows)} skill(s), backend : {self.backend}")
        if self.backend == "heuristic":
            self.log("  attention : aucun CLI (claude/codex) sélectionné, l'heuristique est rejouée")
        def fetch(row: sqlite3.Row) -> str | GitHubError:
            try:
                return self.gh.get_file(row["full_name"], row["default_branch"],
                                        posixpath.join(row["path"], "SKILL.md"), row["skill_md_sha"])
            except GitHubError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            texts = list(pool.map(fetch, rows))
        infos = []
        for row, text in zip(rows, texts):
            if isinstance(text, GitHubError):
                self.errors.append(f"{row['full_name']}/{row['path']} : {text}")
                continue
            parsed = parse_skill_md(text, fallback_name=row["name"] or "")
            infos.append({
                "id": row["id"], "path": row["path"], "name": parsed["name"],
                "description": parsed["description"], "body": parsed["body"],
                "full_name": row["full_name"], "repo_description": row["repo_description"],
                "topics": db.row_to_skill(row)["topics"], "has_scripts": row["has_scripts"],
                "has_references": row["has_references"], "size": row["skill_md_size"],
            })
        results = enrich.enrich_many(infos, self.backend, log=self.log)
        now = db.now_iso()
        for info, res in zip(infos, results):
            conn.execute(
                "UPDATE skills SET summary = ?, advantages = ?, category = ?, compat = ?, "
                "language = ?, enriched_by = ?, enriched_at = ? WHERE id = ?",
                (res["summary_fr"], json.dumps(res["advantages"], ensure_ascii=False),
                 res["category"], json.dumps(res["compat"]), res["language"],
                 res["enriched_by"], now, info["id"]),
            )
            db.set_skill_tags(conn, info["id"], res["tags"])
            self.counters["enriched"] += 1
            if res["enriched_by"] != "heuristic":
                self.counters["skills_updated"] += 1
            if len(infos) <= 50:
                self.log(f"  {info['full_name']}/{info['path']} : {res['enriched_by']}")
        conn.commit()


def run(conn: sqlite3.Connection, gh: GitHubClient, *, full: bool = False,
        limit: int | None = None, discover_only: bool = False, enrich_only: bool = False,
        no_discover: bool = False, repos: list[str] | None = None,
        backend: str = "heuristic", log: Log = _print) -> dict[str, Any]:
    """Lance un crawl complet et journalise le résultat dans crawl_runs."""
    mode = ("enrich-only" if enrich_only else "discover-only" if discover_only
            else "full" if full else "incremental")
    run_id = db.start_run(conn, mode, backend)
    crawler = Crawler(conn, gh, backend=backend, full=full, log=log)
    try:
        if enrich_only:
            crawler.enrich_only(limit)
        else:
            if not no_discover and not repos:
                log("Découverte des dépôts…")
                found = discover_all(gh, limit=limit, log=log)
                added = db.add_candidates(conn, found)
                conn.commit()
                log(f"Découverte : {len(found)} dépôt(s) trouvé(s), {added} nouveau(x)")
            if not discover_only:
                crawler.crawl(limit, only=repos)
    except KeyboardInterrupt:
        conn.rollback()
        crawler.errors.append("interrompu par l'utilisateur")
        log("Interrompu : le prochain crawl reprendra là où celui-ci s'est arrêté.")
    finally:
        db.finish_run(conn, run_id, crawler.counters, crawler.errors, gh.remaining)

    c = crawler.counters
    log("")
    log(f"Crawl n°{run_id} ({mode}) terminé")
    log(f"  dépôts  : {c['repos_seen']} vus, {c['repos_added']} ajoutés, "
        f"{c['repos_changed']} modifiés, {c['repos_unchanged']} inchangés")
    log(f"  skills  : {c['skills_added']} ajoutés, {c['skills_updated']} mis à jour, "
        f"{c['skills_removed']} retirés, {c['enriched']} enrichis")
    log(f"  erreurs : {len(crawler.errors)}")
    for err in crawler.errors[:10]:
        log(f"    - {err}")
    quota = "inconnu" if gh.remaining is None else gh.remaining
    log(f"  quota GitHub restant : {quota} ({gh.requests} requêtes envoyées)")
    return {"run_id": run_id, "counters": c, "errors": crawler.errors}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # console Windows : éviter les erreurs d'encodage
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Crawl incrémental des skills Claude sur GitHub")
    p.add_argument("--full", action="store_true", help="ignore les ETag et les SHA : retraite tout")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="nombre maximal de dépôts (ou de skills avec --enrich-only)")
    p.add_argument("--discover-only", action="store_true",
                   help="découvre les dépôts sans les crawler")
    p.add_argument("--enrich-only", action="store_true",
                   help="ré-enrichit les skills `heuristic` avec un CLI")
    p.add_argument("--no-discover", action="store_true",
                   help="saute la découverte : crawle seulement les dépôts déjà connus")
    p.add_argument("--repo", action="append", metavar="OWNER/NOM", default=None,
                   help="crawle uniquement ce dépôt (option répétable), sans découverte")
    p.add_argument("--backend", choices=["auto", *enrich.BACKENDS], default=None,
                   help="backend d'enrichissement (défaut : ENRICH_BACKEND du .env, sinon auto)")
    p.add_argument("--db", default=None, help="chemin de la base SQLite")
    args = p.parse_args(argv)

    backend = enrich.resolve_backend(args.backend)
    if not config.GITHUB_TOKEN:
        _print("ATTENTION : GITHUB_TOKEN absent. Mode dégradé : 60 requêtes/heure, "
               "pas de recherche de code. Renseigner .env pour un crawl complet.")
    conn = db.connect(args.db)
    # Sans jeton, on n'attend pas la remise à zéro du quota (jusqu'à 1 h) : on s'arrête.
    gh = GitHubClient(config.GITHUB_TOKEN, log=_print,
                      max_wait=3700.0 if config.GITHUB_TOKEN else 90.0)
    try:
        result = run(conn, gh, full=args.full, limit=args.limit,
                     discover_only=args.discover_only, enrich_only=args.enrich_only,
                     no_discover=args.no_discover, repos=args.repo, backend=backend)
    finally:
        gh.close()
        conn.close()
    return 1 if result["errors"] and not any(result["counters"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
