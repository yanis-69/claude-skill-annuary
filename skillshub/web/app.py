"""Application web : FastAPI + Jinja2 + HTMX."""

from __future__ import annotations

import math
import posixpath
import sqlite3
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from .. import db
from ..enrich import CATEGORY_LABELS

HERE = Path(__file__).resolve().parent
PER_PAGE = 30

COMPAT_LABELS = {
    "claude-code": "Claude Code", "claude.ai": "claude.ai", "api": "API",
    "codex": "Codex", "other-agents": "Autres agents",
}
INSTALL_LABELS = {"plugin": "Plugin (marketplace)", "standalone": "Standalone",
                  "collection": "Collection"}
LANG_LABELS = {"en": "Anglais", "fr": "Français", "zh": "Chinois", "ja": "Japonais",
               "ko": "Coréen", "es": "Espagnol", "de": "Allemand", "pt": "Portugais",
               "ru": "Russe"}

app = FastAPI(title="Skills Hub", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


templates.env.filters["num"] = _num
templates.env.filters["day"] = lambda v: (v or "")[:10] or "—"
templates.env.globals.update(
    category_labels=CATEGORY_LABELS, compat_labels=COMPAT_LABELS,
    install_labels=INSTALL_LABELS, lang_labels=LANG_LABELS,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_filters(qp: QueryParams) -> dict[str, Any]:
    since = _int(qp.get("since"))
    return {
        "q": (qp.get("q") or "").strip(),
        "category": qp.get("category") or "",
        "tags": [t for t in qp.getlist("tags") if t],
        "min_stars": max(0, _int(qp.get("min_stars")) or 0),
        "since": since if since in db.SINCE_CHOICES else None,
        "official": bool(qp.get("official")),
        "compat": [c for c in qp.getlist("compat") if c],
        "install_type": qp.get("install_type") or "",
        "scripts": bool(qp.get("scripts")),
        "license": qp.get("license") or "",
        "language": qp.get("language") or "",
        "light": bool(qp.get("light")),
        "hide_archived": bool(qp.get("hide_archived")),
        "author": (qp.get("author") or "").strip(),
    }


def run_search(conn: sqlite3.Connection, qp: QueryParams, per_page: int = PER_PAGE
               ) -> dict[str, Any]:
    filters = parse_filters(qp)
    sort_param = sort = qp.get("sort") or ""
    if sort not in db.SORTS or (sort == "relevance" and not filters["q"]):
        sort = "relevance" if filters["q"] else db.DEFAULT_SORT
    page = max(1, _int(qp.get("page")) or 1)
    skills, total = db.search_skills(conn, filters, sort, page, per_page)
    pages = max(1, math.ceil(total / per_page))

    base = [(k, v) for k, v in qp.multi_items() if k != "page" and v != ""]

    def page_url(n: int) -> str:
        return "/?" + urlencode(base + ([("page", n)] if n > 1 else []))

    return {
        "skills": skills, "total": total, "page": page, "pages": pages,
        "per_page": per_page, "filters": filters, "sort": sort, "page_url": page_url,
        "sort_param": sort_param if sort_param in db.SORTS else "",
        "active_filters": sum(
            1 for k, v in filters.items() if k != "q" and v not in ("", None, False, 0, [])
        ),
    }


def install_commands(skill: dict[str, Any]) -> list[str]:
    """Commandes d'installation suggérées."""
    if skill["install_type"] == "plugin":
        return [f"/plugin marketplace add {skill['full_name']}"]
    repo_dir = skill["full_name"].split("/")[1]
    folder = posixpath.basename(skill["path"]) or repo_dir
    source = f"{repo_dir}/{skill['path']}" if skill["path"] else repo_dir
    return [
        f"git clone --depth 1 https://github.com/{skill['full_name']}",
        f"cp -r {source} ~/.claude/skills/{folder}",
    ]


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ctx = run_search(conn, request.query_params)
    ctx.update(sorts=db.SORTS, since_choices=db.SINCE_CHOICES)
    h = request.headers
    partial = (h.get("HX-Request") == "true" and h.get("HX-Target") == "results"
               and h.get("HX-History-Restore-Request") != "true")
    if partial:
        resp = templates.TemplateResponse(request, "_results.html", ctx)
    else:
        ctx["facets"] = db.facets(conn)
        resp = templates.TemplateResponse(request, "index.html", ctx)
    resp.headers["Vary"] = "HX-Request, HX-Target"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/skill/{skill_id}", response_class=HTMLResponse)
def skill_page(request: Request, skill_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    skill = db.get_skill(conn, skill_id)
    if not skill:
        raise HTTPException(404, "Skill introuvable")
    siblings = db.repo_skills(conn, skill["repo_id"], exclude_id=skill_id, limit=24)
    folder_url = f"{skill['repo_url']}/tree/{skill['default_branch']}"
    if skill["path"]:
        folder_url += f"/{skill['path']}"
    return templates.TemplateResponse(request, "skill.html", {
        "skill": skill, "siblings": siblings, "folder_url": folder_url,
        "install": install_commands(skill),
    })


@app.get("/repo/{owner}/{name}", response_class=HTMLResponse)
def repo_page(request: Request, owner: str, name: str,
              conn: sqlite3.Connection = Depends(get_conn)):
    repo = db.get_repo(conn, f"{owner}/{name}")
    if not repo:
        raise HTTPException(404, "Dépôt introuvable")
    return templates.TemplateResponse(request, "repo.html", {
        "repo": repo, "skills": db.repo_skills(conn, repo["id"]),
    })


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(request, "stats.html", {"stats": db.stats(conn)})


API_FIELDS = (
    "id", "name", "path", "full_name", "repo_url", "owner", "is_official", "category", "tags",
    "summary", "advantages", "frontmatter_description", "compat", "language", "install_type",
    "has_scripts", "has_references", "file_count", "skill_md_size", "tokens", "stars", "forks",
    "trend", "license", "archived", "pushed_at", "first_seen_at", "updated_at", "enriched_by",
)


@app.get("/api/skills")
def api_skills(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """La même recherche que la page d'accueil, en JSON (mêmes paramètres, plus `per_page`)."""
    per_page = max(1, min(100, _int(request.query_params.get("per_page")) or PER_PAGE))
    r = run_search(conn, request.query_params, per_page)
    return {
        "total": r["total"], "page": r["page"], "pages": r["pages"], "per_page": per_page,
        "sort": r["sort"],
        "items": [{k: s.get(k) for k in API_FIELDS} | {"url": f"/skill/{s['id']}"}
                  for s in r["skills"]],
    }
