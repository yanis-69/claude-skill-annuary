"""Lecture de SKILL.md (frontmatter YAML + corps), de marketplace.json et de l'arbre Git."""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Iterable

import yaml

_FRONTMATTER = re.compile(r"\A﻿?\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_KEY_LINE = re.compile(r"^(name|description)\s*:\s*(.*)$", re.IGNORECASE)

SCRIPT_EXT = (".py", ".sh", ".js", ".ts", ".mjs", ".rb", ".ps1", ".go")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _loose_frontmatter(raw: str) -> dict[str, Any]:
    """Repli quand le YAML est invalide (par exemple un « : » non échappé dans la description)."""
    out: dict[str, Any] = {}
    key = None
    for line in raw.splitlines():
        m = _KEY_LINE.match(line)
        if m:
            key = m.group(1).lower()
            out[key] = m.group(2).strip().strip("\"'")
            if out[key] in ("|", ">", "|-", ">-"):
                out[key] = ""
        elif key and line[:1] in (" ", "\t") and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
        elif re.match(r"^[\w-]+\s*:", line):
            key = None
    return out


def parse_skill_md(text: str, fallback_name: str = "") -> dict[str, Any]:
    """Extrait `name`, `description`, le frontmatter complet et le corps d'un SKILL.md."""
    text = text or ""
    frontmatter: dict[str, Any] = {}
    body = text
    m = _FRONTMATTER.match(text)
    if m:
        body = text[m.end():]
        try:
            loaded = yaml.safe_load(m.group(1))
            frontmatter = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            frontmatter = {}
        if not frontmatter:
            frontmatter = _loose_frontmatter(m.group(1))
    frontmatter = {str(k).lower(): v for k, v in frontmatter.items()}

    name = _clean(frontmatter.get("name"))
    description = _clean(frontmatter.get("description"))
    if not name:
        h1 = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        name = _clean(h1.group(1)) if h1 else fallback_name
    if not description:
        description = first_paragraph(body)
    return {
        "name": name[:200] or fallback_name or "skill",
        "description": description[:2000],
        "frontmatter": frontmatter,
        "body": body.strip(),
    }


def first_paragraph(body: str) -> str:
    """Premier paragraphe de texte du corps (hors titres, blocs de code et listes)."""
    in_code = False
    lines: list[str] = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not s or s.startswith(("#", "|", "<", "---", "![")):
            if lines:
                break
            continue
        lines.append(s)
    return _clean(" ".join(lines))[:600]


# --------------------------------------------------------------------------
# Arbre Git
# --------------------------------------------------------------------------

def find_skills_in_tree(tree: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repère chaque SKILL.md de l'arbre et décrit son dossier.

    Retourne des dictionnaires : path (dossier, '' pour la racine), file (chemin du SKILL.md),
    sha (blob SHA), size, has_scripts, has_references, file_count.
    """
    blobs = [e for e in tree if e.get("type") == "blob" and e.get("path")]
    skills: dict[str, dict[str, Any]] = {}
    for e in blobs:
        if posixpath.basename(e["path"]) == "SKILL.md":
            d = posixpath.dirname(e["path"])
            skills[d] = {
                "path": d, "file": e["path"], "sha": e.get("sha"), "size": e.get("size") or 0,
                "has_scripts": False, "has_references": False, "file_count": 0,
            }
    if not skills:
        return []
    for e in blobs:
        # Chaque fichier est rattaché au dossier de skill ancêtre le plus proche.
        d = posixpath.dirname(e["path"])
        while True:
            if d in skills:
                break
            if not d:
                d = None
                break
            d = posixpath.dirname(d)
        if d is None:
            continue
        sk = skills[d]
        sk["file_count"] += 1
        rel = e["path"][len(d) + 1:] if d else e["path"]
        low = rel.lower()
        # Pour un skill à la racine, seuls les dossiers conventionnels comptent
        # (sinon tout le code du dépôt serait pris pour des scripts du skill).
        if low.startswith("scripts/") or (d and low.endswith(SCRIPT_EXT)):
            sk["has_scripts"] = True
        if low.startswith(("references/", "reference/", "resources/", "assets/", "templates/")) \
                or (d and low.endswith(".md") and rel != "SKILL.md"):
            sk["has_references"] = True
    return sorted(skills.values(), key=lambda s: s["path"])


def is_plugin_repo(tree: Iterable[dict[str, Any]]) -> bool:
    """Vrai si le dépôt expose un plugin ou une marketplace Claude Code."""
    for e in tree:
        p = e.get("path") or ""
        if p.endswith(".claude-plugin/marketplace.json") or p.endswith(".claude-plugin/plugin.json"):
            return True
    return False


def install_type(plugin: bool, skill_count: int) -> str:
    if plugin:
        return "plugin"
    return "collection" if skill_count > 1 else "standalone"


# --------------------------------------------------------------------------
# marketplace.json et listes « awesome »
# --------------------------------------------------------------------------

_GH_LINK = re.compile(
    r"github\.com[/:]([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})", re.IGNORECASE
)
_RESERVED_OWNERS = {
    "topics", "orgs", "sponsors", "features", "marketplace", "apps", "settings", "about",
    "collections", "search", "login", "site", "user-attachments", "contact", "pricing",
    "issues", "pulls", "explore", "notifications", "security", "enterprise", "readme",
}


def normalize_full_name(owner: str, name: str) -> str | None:
    name = name.rstrip(".")
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not name or name in (".", "..") or owner.lower() in _RESERVED_OWNERS:
        return None
    return f"{owner}/{name}"


def extract_github_repos(text: str) -> list[str]:
    """Tous les dépôts GitHub cités dans un texte (README, liste « awesome »…), dédoublonnés."""
    seen: dict[str, str] = {}
    for owner, name in _GH_LINK.findall(text or ""):
        full = normalize_full_name(owner, name)
        if full:
            seen.setdefault(full.lower(), full)
    return list(seen.values())


def parse_marketplace(text: str) -> list[str]:
    """Dépôts GitHub référencés par un `.claude-plugin/marketplace.json`."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    found: dict[str, str] = {}

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        value = value.strip()
        if "github.com" in value:
            for full in extract_github_repos(value):
                found.setdefault(full.lower(), full)
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", value):
            full = normalize_full_name(*value.split("/"))
            if full:
                found.setdefault(full.lower(), full)

    for plugin in data.get("plugins") or []:
        if not isinstance(plugin, dict):
            continue
        source = plugin.get("source")
        if isinstance(source, dict):
            add(source.get("repo"))
            add(source.get("url"))
        elif isinstance(source, str) and not source.startswith((".", "/")):
            add(source)
        add(plugin.get("repository"))
        add(plugin.get("homepage"))
    return list(found.values())
