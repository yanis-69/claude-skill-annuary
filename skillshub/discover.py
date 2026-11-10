"""Sources de découverte des dépôts GitHub qui contiennent des skills Claude."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import config
from .github import GitHubClient, GitHubError
from .parse import extract_github_repos, parse_marketplace

DEFAULT_SEEDS = ["anthropics/skills"]

TOPICS = [
    "claude-skills", "claude-code-skills", "agent-skills", "anthropic-skills",
    "claude-code-plugin", "claude-skill", "skills",
]
# Le topic générique « skills » est trop large : il est restreint par mots-clés.
TOPIC_QUERIES = [f"topic:{t}" for t in TOPICS if t != "skills"] + [
    "topic:skills claude",
    '"SKILL.md" claude in:readme,description',
    "claude skills in:name,description",
]

AWESOME_LISTS = [
    "travisvn/awesome-claude-skills",
    "ComposioHQ/awesome-claude-skills",
    "VoltAgent/awesome-claude-skills",
    "hesreallyhim/awesome-claude-code",
]
AWESOME_QUERY = "awesome claude skills in:name"

# Tranches de taille (octets) pour contourner le plafond de 1000 résultats par recherche.
SIZE_STEP = 500
SIZE_MAX = 20000
SEARCH_CAP = 1000

Log = Callable[[str], None]


class Found(dict):
    """full_name -> source, dédoublonné sans tenir compte de la casse."""

    def __init__(self, limit: int | None = None):
        super().__init__()
        self._keys: set[str] = set()
        self.limit = limit

    def add(self, full_name: str | None, source: str) -> None:
        if not full_name or not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name):
            return
        if full_name.lower() not in self._keys:
            self._keys.add(full_name.lower())
            self[full_name] = source

    @property
    def full(self) -> bool:
        return self.limit is not None and len(self) >= self.limit


def read_seeds(path: Path | None = None) -> list[str]:
    path = path or config.SEEDS_PATH
    seeds = list(DEFAULT_SEEDS)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                seeds.append(line.removeprefix("https://github.com/").strip("/"))
    return list(dict.fromkeys(seeds))


def _keep(repo: dict) -> bool:
    """Écarte les forks : la recherche GitHub n'indexe que ceux qui ont leur propre activité,
    et le crawl refait le contrôle « fork sans commit propre » dépôt par dépôt."""
    return bool(repo) and not repo.get("private")


def discover_seeds(found: Found, log: Log) -> None:
    for full in read_seeds():
        found.add(full, "seed")
    log(f"  seeds : {len(found)} dépôt(s)")


def discover_topics(gh: GitHubClient, found: Found, log: Log, max_pages: int = 10) -> None:
    for query in TOPIC_QUERIES:
        before = len(found)
        for page in range(1, max_pages + 1):
            if found.full:
                break
            data = gh.search("repositories", query, page=page, sort="stars")
            items = data.get("items") or []
            for repo in items:
                if _keep(repo):
                    found.add(repo.get("full_name"), f"topic:{query}")
            if len(items) < 100:
                break
        log(f"  recherche de dépôts « {query} » : +{len(found) - before}")
        if found.full:
            return


def _raw_url(html_url: str) -> str | None:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/(.+)", html_url or "")
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}" if m else None


def discover_marketplaces(gh: GitHubClient, found: Found, log: Log, max_pages: int = 10) -> None:
    before = len(found)
    for page in range(1, max_pages + 1):
        if found.full:
            break
        data = gh.search("code", "filename:marketplace.json path:.claude-plugin", page=page)
        items = data.get("items") or []
        for item in items:
            repo = item.get("repository") or {}
            found.add(repo.get("full_name"), "marketplace")
            raw = _raw_url(item.get("html_url", ""))
            if not raw or found.full:
                continue
            try:  # on suit les dépôts référencés par la marketplace
                for full in parse_marketplace(gh.request(raw).text):
                    found.add(full, f"marketplace:{repo.get('full_name')}")
            except GitHubError:
                pass
        if len(items) < 100:
            break
    log(f"  marketplaces de plugins : +{len(found) - before}")


def discover_awesome(gh: GitHubClient, found: Found, log: Log) -> None:
    lists = list(AWESOME_LISTS)
    try:
        data = gh.search("repositories", AWESOME_QUERY, sort="stars", per_page=30)
        lists += [r["full_name"] for r in data.get("items") or [] if r.get("full_name")]
    except GitHubError as exc:
        log(f"  recherche des listes awesome impossible : {exc}")
    before = len(found)
    for full in dict.fromkeys(lists):
        if found.full:
            break
        try:
            readme = gh.get_readme(full)
        except GitHubError:
            continue
        found.add(full, "awesome")
        for linked in extract_github_repos(readme):
            found.add(linked, f"awesome:{full}")
    log(f"  listes awesome : +{len(found) - before}")


def _code_search_slice(gh: GitHubClient, found: Found, log: Log, lo: int, hi: int | None) -> None:
    """Recherche `filename:SKILL.md` sur une tranche de taille ; redécoupe si elle dépasse le plafond."""
    if found.full:
        return
    size = f"size:>={lo}" if hi is None else f"size:{lo}..{hi}"
    query = f"filename:SKILL.md {size}"
    data = gh.search("code", query, page=1)
    total = data.get("total_count") or 0
    if total > SEARCH_CAP and hi is not None and hi > lo:
        mid = (lo + hi) // 2
        _code_search_slice(gh, found, log, lo, mid)
        _code_search_slice(gh, found, log, mid + 1, hi)
        return
    before = len(found)
    page = 1
    while True:
        for item in data.get("items") or []:
            repo = item.get("repository") or {}
            if _keep(repo):
                found.add(repo.get("full_name"), "code-search")
        if found.full or page * 100 >= min(total, SEARCH_CAP) or not data.get("items"):
            break
        page += 1
        data = gh.search("code", query, page=page)
    capped = " (plafonné à 1000)" if total > SEARCH_CAP else ""
    log(f"  recherche de code {size} : {total} fichier(s){capped}, +{len(found) - before} dépôt(s)")


def discover_code(gh: GitHubClient, found: Found, log: Log) -> None:
    lo = 0
    while lo <= SIZE_MAX and not found.full:
        hi = lo + SIZE_STEP if lo == 0 else lo + SIZE_STEP - 1
        _code_search_slice(gh, found, log, lo, hi)
        lo = hi + 1
    _code_search_slice(gh, found, log, lo, None)


def discover_all(gh: GitHubClient, limit: int | None = None, log: Log = print) -> Found:
    """Lance toutes les sources. Avec `limit`, s'arrête dès que assez de dépôts sont trouvés.

    Sans jeton GitHub, la recherche de code est indisponible : seules les sources
    seeds, topics et listes awesome sont utilisées.
    """
    found = Found(limit)
    discover_seeds(found, log)
    steps: list[tuple[str, Callable[[], None]]] = [
        ("topics", lambda: discover_topics(gh, found, log)),
        ("awesome", lambda: discover_awesome(gh, found, log)),
    ]
    if gh.token:
        steps += [
            ("marketplaces", lambda: discover_marketplaces(gh, found, log)),
            ("code", lambda: discover_code(gh, found, log)),
        ]
    else:
        log("  GITHUB_TOKEN absent : recherche de code et marketplaces ignorées")
    for name, step in steps:
        if found.full:
            break
        try:
            step()
        except GitHubError as exc:
            log(f"  source « {name} » interrompue : {exc}")
    return found
