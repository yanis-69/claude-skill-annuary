"""Base SQLite : schéma, migrations et requêtes (filtres, tris, FTS5)."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

# --------------------------------------------------------------------------
# Schéma et migrations
# --------------------------------------------------------------------------

_V1 = """
CREATE TABLE repos (
    id              TEXT PRIMARY KEY,            -- node id GitHub
    full_name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url             TEXT,
    owner           TEXT COLLATE NOCASE,
    owner_type      TEXT,                        -- user | org
    is_official     INTEGER NOT NULL DEFAULT 0,  -- organisation anthropics
    description     TEXT,
    stars           INTEGER NOT NULL DEFAULT 0,
    forks           INTEGER NOT NULL DEFAULT 0,
    open_issues     INTEGER NOT NULL DEFAULT 0,
    license         TEXT,
    topics          TEXT NOT NULL DEFAULT '[]',  -- JSON
    default_branch  TEXT,
    created_at      TEXT,
    pushed_at       TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    is_fork         INTEGER NOT NULL DEFAULT 0,
    gone            INTEGER NOT NULL DEFAULT 0,  -- dépôt supprimé ou devenu privé
    etag            TEXT,
    tree_sha        TEXT,
    last_checked_at TEXT,
    source          TEXT                         -- comment le dépôt a été découvert
);
CREATE INDEX idx_repos_checked ON repos(last_checked_at);
CREATE INDEX idx_repos_owner ON repos(owner);

-- Dépôts découverts mais pas encore crawlés (permet la reprise).
CREATE TABLE candidates (
    full_name     TEXT PRIMARY KEY COLLATE NOCASE,
    source        TEXT,
    discovered_at TEXT
);

CREATE TABLE skills (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id                 TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path                    TEXT NOT NULL,       -- dossier du skill ('' = racine)
    name                    TEXT,
    frontmatter_description TEXT,
    summary                 TEXT,                -- FR, 2 à 4 phrases
    advantages              TEXT NOT NULL DEFAULT '[]',  -- JSON, 3 à 5 puces
    category                TEXT,
    skill_md_sha            TEXT,                -- blob SHA : clé de détection de changement
    skill_md_size           INTEGER,             -- octets (≈ 4 octets par token)
    has_scripts             INTEGER NOT NULL DEFAULT 0,
    has_references          INTEGER NOT NULL DEFAULT 0,
    file_count              INTEGER NOT NULL DEFAULT 1,
    install_type            TEXT,                -- plugin | standalone | collection
    compat                  TEXT NOT NULL DEFAULT '[]',  -- JSON
    language                TEXT,
    enriched_by             TEXT,                -- claude | codex | heuristic
    enriched_at             TEXT,
    first_seen_at           TEXT,
    updated_at              TEXT,
    removed                 INTEGER NOT NULL DEFAULT 0,
    UNIQUE (repo_id, path)
);
CREATE INDEX idx_skills_repo ON skills(repo_id);
CREATE INDEX idx_skills_category ON skills(category);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE skill_tags (
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (skill_id, tag_id)
);
CREATE INDEX idx_skill_tags_tag ON skill_tags(tag_id);

CREATE TABLE star_snapshots (
    repo_id TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    date    TEXT NOT NULL,                       -- AAAA-MM-JJ
    stars   INTEGER NOT NULL,
    PRIMARY KEY (repo_id, date)
);

CREATE TABLE crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT,
    backend         TEXT,
    repos_seen      INTEGER NOT NULL DEFAULT 0,
    repos_unchanged INTEGER NOT NULL DEFAULT 0,
    repos_changed   INTEGER NOT NULL DEFAULT 0,
    repos_added     INTEGER NOT NULL DEFAULT 0,
    skills_added    INTEGER NOT NULL DEFAULT 0,
    skills_updated  INTEGER NOT NULL DEFAULT 0,
    skills_removed  INTEGER NOT NULL DEFAULT 0,
    enriched        INTEGER NOT NULL DEFAULT 0,
    errors          TEXT NOT NULL DEFAULT '[]',  -- JSON
    rate_remaining  INTEGER
);

CREATE VIRTUAL TABLE skills_fts USING fts5(
    name, description, summary, advantages, tags,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Synchronisation skills -> skills_fts (rowid = skills.id).
CREATE TRIGGER skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts (rowid, name, description, summary, advantages, tags)
    VALUES (new.id, coalesce(new.name, ''), coalesce(new.frontmatter_description, ''),
            coalesce(new.summary, ''), coalesce(new.advantages, ''), '');
END;
CREATE TRIGGER skills_au
AFTER UPDATE OF name, frontmatter_description, summary, advantages ON skills BEGIN
    UPDATE skills_fts
       SET name = coalesce(new.name, ''),
           description = coalesce(new.frontmatter_description, ''),
           summary = coalesce(new.summary, ''),
           advantages = coalesce(new.advantages, '')
     WHERE rowid = new.id;
END;
CREATE TRIGGER skills_ad AFTER DELETE ON skills BEGIN
    DELETE FROM skills_fts WHERE rowid = old.id;
END;
CREATE TRIGGER skill_tags_ai AFTER INSERT ON skill_tags BEGIN
    UPDATE skills_fts
       SET tags = (SELECT coalesce(group_concat(t.name, ' '), '')
                     FROM skill_tags st JOIN tags t ON t.id = st.tag_id
                    WHERE st.skill_id = new.skill_id)
     WHERE rowid = new.skill_id;
END;
CREATE TRIGGER skill_tags_ad AFTER DELETE ON skill_tags BEGIN
    UPDATE skills_fts
       SET tags = (SELECT coalesce(group_concat(t.name, ' '), '')
                     FROM skill_tags st JOIN tags t ON t.id = st.tag_id
                    WHERE st.skill_id = old.skill_id)
     WHERE rowid = old.skill_id;
END;
"""

# v2 : mode d'emploi et exemple par skill ; table meta (curseurs de reprise).
_V2 = """
ALTER TABLE skills ADD COLUMN usage TEXT;      -- comment utiliser le skill (une ligne par étape)
ALTER TABLE skills ADD COLUMN example TEXT;    -- exemple d'utilisation, si disponible
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Une entrée par version de schéma ; PRAGMA user_version mémorise la dernière appliquée.
MIGRATIONS: list[str] = [_V1, _V2]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Ouvre la base (la crée et la migre au besoin)."""
    path = str(path if path is not None else config.DB_PATH)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")


# --------------------------------------------------------------------------
# Écritures
# --------------------------------------------------------------------------

REPO_FIELDS = (
    "full_name", "url", "owner", "owner_type", "is_official", "description", "stars",
    "forks", "open_issues", "license", "topics", "default_branch", "created_at",
    "pushed_at", "archived", "is_fork", "gone", "etag", "tree_sha", "last_checked_at",
    "source",
)


def upsert_repo(conn: sqlite3.Connection, repo: dict[str, Any]) -> None:
    """Insère ou met à jour un dépôt, identifié par son node id."""
    data = {k: repo.get(k) for k in REPO_FIELDS}
    if isinstance(data["topics"], (list, tuple)):
        data["topics"] = json.dumps(list(data["topics"]), ensure_ascii=False)
    data["topics"] = data["topics"] or "[]"
    for k in ("is_official", "stars", "forks", "open_issues", "archived", "is_fork", "gone"):
        data[k] = int(data[k] or 0)
    # Un autre dépôt peut avoir repris ce nom après un renommage : l'ancien est retiré.
    conn.execute(
        "DELETE FROM repos WHERE full_name = ? AND id <> ?", (data["full_name"], repo["id"])
    )
    cols = ", ".join(REPO_FIELDS)
    marks = ", ".join(f":{k}" for k in REPO_FIELDS)
    # La source d'origine est conservée lors des mises à jour.
    updates = ", ".join(
        f"{k} = coalesce(repos.source, excluded.source)" if k == "source" else f"{k} = excluded.{k}"
        for k in REPO_FIELDS
    )
    conn.execute(
        f"INSERT INTO repos (id, {cols}) VALUES (:id, {marks}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        {"id": repo["id"], **data},
    )


def add_star_snapshot(conn: sqlite3.Connection, repo_id: str, stars: int,
                      date: str | None = None) -> None:
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, date, stars) VALUES (?, ?, ?) "
        "ON CONFLICT(repo_id, date) DO UPDATE SET stars = excluded.stars",
        (repo_id, date or today(), int(stars or 0)),
    )


def add_candidates(conn: sqlite3.Connection, found: dict[str, str]) -> int:
    """Enregistre les dépôts découverts qui ne sont pas encore connus. Retourne le nombre ajouté."""
    added = 0
    ts = now_iso()
    for full_name, source in found.items():
        if conn.execute("SELECT 1 FROM repos WHERE full_name = ?", (full_name,)).fetchone():
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO candidates (full_name, source, discovered_at) VALUES (?, ?, ?)",
            (full_name, source, ts),
        )
        added += cur.rowcount
    return added


SKILL_FIELDS = (
    "name", "frontmatter_description", "summary", "advantages", "category", "skill_md_sha",
    "skill_md_size", "has_scripts", "has_references", "file_count", "install_type", "compat",
    "language", "enriched_by", "enriched_at", "usage", "example",
)


def upsert_skill(conn: sqlite3.Connection, repo_id: str, path: str,
                 skill: dict[str, Any]) -> tuple[int, bool]:
    """Insère ou met à jour un skill. Retourne (id, créé)."""
    data = {k: skill.get(k) for k in SKILL_FIELDS}
    for k in ("advantages", "compat"):
        if not isinstance(data[k], str):
            data[k] = json.dumps(list(data[k] or []), ensure_ascii=False)
    for k in ("has_scripts", "has_references"):
        data[k] = int(bool(data[k]))
    data["file_count"] = int(data["file_count"] or 1)
    ts = now_iso()
    row = conn.execute(
        "SELECT id FROM skills WHERE repo_id = ? AND path = ?", (repo_id, path)
    ).fetchone()
    if row:
        sets = ", ".join(f"{k} = :{k}" for k in SKILL_FIELDS)
        conn.execute(
            f"UPDATE skills SET {sets}, updated_at = :ts, removed = 0 WHERE id = :id",
            {**data, "ts": ts, "id": row["id"]},
        )
        skill_id, created = row["id"], False
    else:
        cols = ", ".join(SKILL_FIELDS)
        marks = ", ".join(f":{k}" for k in SKILL_FIELDS)
        cur = conn.execute(
            f"INSERT INTO skills (repo_id, path, {cols}, first_seen_at, updated_at) "
            f"VALUES (:repo_id, :path, {marks}, :ts, :ts)",
            {**data, "repo_id": repo_id, "path": path, "ts": ts},
        )
        skill_id, created = cur.lastrowid, True
    if skill.get("tags") is not None:
        set_skill_tags(conn, skill_id, skill["tags"])
    return skill_id, created


def set_skill_tags(conn: sqlite3.Connection, skill_id: int, tags: Iterable[str]) -> None:
    conn.execute("DELETE FROM skill_tags WHERE skill_id = ?", (skill_id,))
    for tag in dict.fromkeys(t.strip().lower() for t in tags if t and t.strip()):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO skill_tags (skill_id, tag_id) VALUES (?, ?)", (skill_id, tag_id)
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)),
        )
    conn.commit()


def start_run(conn: sqlite3.Connection, mode: str, backend: str) -> int:
    # Un crawl tué brutalement n'a pas pu se clôturer : on le marque ici.
    conn.execute(
        "UPDATE crawl_runs SET finished_at = ?, errors = ? WHERE finished_at IS NULL",
        (now_iso(), json.dumps(["crawl interrompu avant sa fin"], ensure_ascii=False)),
    )
    cur = conn.execute(
        "INSERT INTO crawl_runs (started_at, mode, backend) VALUES (?, ?, ?)",
        (now_iso(), mode, backend),
    )
    conn.commit()
    return cur.lastrowid


RUN_COUNTERS = (
    "repos_seen", "repos_unchanged", "repos_changed", "repos_added", "skills_added",
    "skills_updated", "skills_removed", "enriched",
)


def finish_run(conn: sqlite3.Connection, run_id: int, counters: dict[str, int],
               errors: list[str], rate_remaining: int | None) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in RUN_COUNTERS)
    conn.execute(
        f"UPDATE crawl_runs SET finished_at = :finished_at, {sets}, errors = :errors, "
        f"rate_remaining = :rate_remaining WHERE id = :id",
        {
            **{k: int(counters.get(k, 0)) for k in RUN_COUNTERS},
            "finished_at": now_iso(),
            "errors": json.dumps(errors[:200], ensure_ascii=False),
            "rate_remaining": rate_remaining,
            "id": run_id,
        },
    )
    conn.commit()


# --------------------------------------------------------------------------
# Lecture : recherche, filtres, tris
# --------------------------------------------------------------------------

LIGHT_MAX_BYTES = 8000  # « léger » : moins de 2000 tokens environ

_TREND = (
    "(r.stars - coalesce("
    "(SELECT ss.stars FROM star_snapshots ss WHERE ss.repo_id = r.id "
    "   AND ss.date <= date('now', '-30 day') ORDER BY ss.date DESC LIMIT 1), "
    "(SELECT ss.stars FROM star_snapshots ss WHERE ss.repo_id = r.id "
    "   ORDER BY ss.date ASC LIMIT 1), "
    "r.stars))"
)

SORTS: dict[str, tuple[str, str]] = {
    "stars": ("Étoiles", "r.stars DESC, s.name COLLATE NOCASE"),
    "newest": ("Plus récents (détection)", "s.first_seen_at DESC, r.stars DESC, s.id DESC"),
    "created": ("Plus récents (création du dépôt)", "r.created_at DESC, s.id DESC"),
    "updated": ("Dernière mise à jour", "r.pushed_at DESC, s.id DESC"),
    "trending": ("Tendance (30 j)", "trend DESC, r.stars DESC"),
    "forks": ("Forks", "r.forks DESC, r.stars DESC"),
    "name": ("Nom", "s.name COLLATE NOCASE ASC, r.stars DESC"),
    "relevance": ("Pertinence", "bm25(skills_fts, 10.0, 4.0, 3.0, 2.0, 6.0), r.stars DESC"),
}
DEFAULT_SORT = "stars"

SINCE_CHOICES = {7: "7 jours", 30: "30 jours", 90: "90 jours", 365: "1 an"}


def fts_query(text: str | None) -> str:
    """Transforme une saisie libre en requête FTS5 sûre (tous les mots, en préfixe)."""
    tokens = re.findall(r"[\w][\w\-\.]*", text or "", flags=re.UNICODE)
    return " ".join('"' + t.replace('"', "") + '"*' for t in tokens[:12])


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _where(f: dict[str, Any]) -> tuple[list[str], list[str], list[Any], bool]:
    joins: list[str] = []
    where = ["s.removed = 0", "r.gone = 0"]
    params: list[Any] = []

    match = fts_query(f.get("q"))
    if match:
        joins.append("JOIN skills_fts ON skills_fts.rowid = s.id")
        where.append("skills_fts MATCH ?")
        params.append(match)

    if f.get("category"):
        where.append("s.category = ?")
        params.append(f["category"])
    for tag in _as_list(f.get("tags")):
        where.append(
            "EXISTS (SELECT 1 FROM skill_tags st JOIN tags t ON t.id = st.tag_id "
            "WHERE st.skill_id = s.id AND t.name = ?)"
        )
        params.append(tag.lower())
    if f.get("min_stars"):
        where.append("r.stars >= ?")
        params.append(int(f["min_stars"]))
    if f.get("since"):
        limit = datetime.now(timezone.utc) - timedelta(days=int(f["since"]))
        where.append("r.pushed_at >= ?")
        params.append(limit.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if f.get("official"):
        where.append("r.is_official = 1")
    for compat in _as_list(f.get("compat")):
        where.append("EXISTS (SELECT 1 FROM json_each(s.compat) WHERE value = ?)")
        params.append(compat)
    if f.get("install_type"):
        where.append("s.install_type = ?")
        params.append(f["install_type"])
    if f.get("scripts"):
        where.append("s.has_scripts = 1")
    if f.get("license"):
        where.append("r.license = ?")
        params.append(f["license"])
    if f.get("language"):
        where.append("s.language = ?")
        params.append(f["language"])
    if f.get("light"):
        where.append("s.skill_md_size < ?")
        params.append(LIGHT_MAX_BYTES)
    if f.get("hide_archived"):
        where.append("r.archived = 0")
    if f.get("author"):
        where.append("r.owner = ?")
        params.append(f["author"])
    return joins, where, params, bool(match)


_SELECT = (
    "SELECT s.*, r.full_name, r.url AS repo_url, r.owner, r.owner_type, r.is_official, "
    "r.description AS repo_description, r.stars, r.forks, r.open_issues, r.license, "
    "r.topics, r.default_branch, r.created_at AS repo_created_at, r.pushed_at, r.archived, "
    f"{_TREND} AS trend, "
    "(SELECT group_concat(t.name, ',') FROM skill_tags st JOIN tags t ON t.id = st.tag_id "
    " WHERE st.skill_id = s.id) AS tag_list "
    "FROM skills s JOIN repos r ON r.id = s.repo_id "
)


def row_to_skill(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("advantages", "compat", "topics"):
        if k in d:
            try:
                d[k] = json.loads(d[k] or "[]")
            except (TypeError, ValueError):
                d[k] = []
    d["tags"] = sorted(t for t in (d.pop("tag_list", None) or "").split(",") if t)
    d["tokens"] = int((d.get("skill_md_size") or 0) / 4)
    return d


def search_skills(conn: sqlite3.Connection, filters: dict[str, Any] | None = None,
                  sort: str | None = None, page: int = 1,
                  per_page: int = 30) -> tuple[list[dict[str, Any]], int]:
    """Recherche paginée. Retourne (skills, total)."""
    f = filters or {}
    joins, where, params, has_match = _where(f)
    if sort not in SORTS or (sort == "relevance" and not has_match):
        sort = "relevance" if has_match and not sort else DEFAULT_SORT
    join_sql = " ".join(joins)
    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT count(*) FROM skills s JOIN repos r ON r.id = s.repo_id {join_sql} "
        f"WHERE {where_sql}",
        params,
    ).fetchone()[0]
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))
    rows = conn.execute(
        f"{_SELECT} {join_sql} WHERE {where_sql} ORDER BY {SORTS[sort][1]} LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return [row_to_skill(r) for r in rows], total


def get_skill(conn: sqlite3.Connection, skill_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"{_SELECT} WHERE s.id = ?", (skill_id,)).fetchone()
    return row_to_skill(row) if row else None


def get_repo(conn: sqlite3.Connection, full_name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["topics"] = json.loads(d["topics"] or "[]")
    except ValueError:
        d["topics"] = []
    return d


def repo_skills(conn: sqlite3.Connection, repo_id: str,
                exclude_id: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"{_SELECT} WHERE s.repo_id = ? AND s.removed = 0"
    params: list[Any] = [repo_id]
    if exclude_id is not None:
        sql += " AND s.id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY s.name COLLATE NOCASE"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [row_to_skill(r) for r in conn.execute(sql, params).fetchall()]


_ACTIVE = "FROM skills s JOIN repos r ON r.id = s.repo_id WHERE s.removed = 0 AND r.gone = 0"


def facets(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Valeurs disponibles pour les filtres, avec le nombre de skills."""

    def pairs(sql: str) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in conn.execute(sql).fetchall() if r[0]]

    return {
        "categories": pairs(
            f"SELECT s.category, count(*) {_ACTIVE} GROUP BY s.category ORDER BY 2 DESC"
        ),
        "tags": pairs(
            "SELECT t.name, count(*) FROM skill_tags st JOIN tags t ON t.id = st.tag_id "
            "JOIN skills s ON s.id = st.skill_id JOIN repos r ON r.id = s.repo_id "
            "WHERE s.removed = 0 AND r.gone = 0 GROUP BY t.name ORDER BY 2 DESC"
        ),
        "licenses": pairs(
            f"SELECT r.license, count(*) {_ACTIVE} GROUP BY r.license ORDER BY 2 DESC LIMIT 25"
        ),
        "languages": pairs(
            f"SELECT s.language, count(*) {_ACTIVE} GROUP BY s.language ORDER BY 2 DESC"
        ),
        "compat": pairs(
            f"SELECT j.value, count(*) FROM skills s JOIN repos r ON r.id = s.repo_id, "
            f"json_each(s.compat) j WHERE s.removed = 0 AND r.gone = 0 "
            f"GROUP BY j.value ORDER BY 2 DESC"
        ),
        "install_types": pairs(
            f"SELECT s.install_type, count(*) {_ACTIVE} GROUP BY s.install_type ORDER BY 2 DESC"
        ),
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    fc = facets(conn)
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    last = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_run = dict(last) if last else None
    if last_run:
        try:
            last_run["errors"] = json.loads(last_run["errors"] or "[]")
        except ValueError:
            last_run["errors"] = []
    return {
        "skills": one(f"SELECT count(*) {_ACTIVE}"),
        "repos": one("SELECT count(*) FROM repos WHERE gone = 0"),
        "repos_with_skills": one(f"SELECT count(DISTINCT s.repo_id) {_ACTIVE}"),
        "candidates": one("SELECT count(*) FROM candidates"),
        "official": one(f"SELECT count(*) {_ACTIVE} AND r.is_official = 1"),
        "by_category": fc["categories"],
        "by_tag": fc["tags"],
        "by_language": fc["languages"],
        "by_install_type": fc["install_types"],
        "by_enricher": [
            (r[0], r[1]) for r in conn.execute(
                f"SELECT coalesce(s.enriched_by, '?'), count(*) {_ACTIVE} GROUP BY 1 ORDER BY 2 DESC"
            )
        ],
        "top_authors": [
            dict(r) for r in conn.execute(
                f"SELECT r.owner AS owner, count(*) AS skills, count(DISTINCT r.id) AS repos, "
                f"max(r.stars) AS best_stars {_ACTIVE} GROUP BY r.owner "
                f"ORDER BY skills DESC, best_stars DESC LIMIT 20"
            )
        ],
        "last_run": last_run,
    }
