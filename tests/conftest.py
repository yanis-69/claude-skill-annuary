"""Fixtures partagées : base en mémoire et faux serveur GitHub."""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest

from skillshub import db
from skillshub.github import GitHubClient

SKILL_MD = """---
name: {name}
description: {description}
---

# {name}

Instructions du skill.
"""


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


class FakeGitHub:
    """Simule api.github.com et raw.githubusercontent.com pour quelques dépôts."""

    def __init__(self):
        self.repos: dict[str, dict] = {}
        self.etags: dict[str, str] = {}
        self.trees: dict[str, dict] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.calls: Counter = Counter()

    def add_repo(self, full_name: str, skills: dict[str, tuple[str, str]], *, stars: int = 10,
                 pushed_at: str = "2026-01-01T00:00:00Z", fork: bool = False,
                 created_at: str = "2025-01-01T00:00:00Z", extra_files: tuple[str, ...] = ()):
        """`skills` : dossier -> (blob sha, description)."""
        owner = full_name.split("/")[0]
        self.repos[full_name] = {
            "node_id": f"R_{full_name}", "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "owner": {"login": owner, "type": "Organization" if owner == "anthropics" else "User"},
            "description": f"Dépôt {full_name}", "stargazers_count": stars, "forks_count": 1,
            "open_issues_count": 0, "license": {"spdx_id": "MIT"}, "topics": ["claude-skills"],
            "default_branch": "main", "created_at": created_at, "pushed_at": pushed_at,
            "archived": False, "fork": fork,
        }
        self.etags[full_name] = 'W/"etag-1"'
        self.set_tree(full_name, skills, extra_files)

    def set_tree(self, full_name: str, skills: dict[str, tuple[str, str]],
                 extra_files: tuple[str, ...] = (), tree_sha: str | None = None):
        entries = []
        for folder, (sha, description) in skills.items():
            path = f"{folder}/SKILL.md" if folder else "SKILL.md"
            entries.append({"path": path, "type": "blob", "sha": sha, "size": 400})
            name = folder.split("/")[-1] or "root"
            self.files[(full_name, path)] = SKILL_MD.format(name=name, description=description)
        for path in extra_files:
            entries.append({"path": path, "type": "blob", "sha": "x" + path, "size": 10})
        sha = tree_sha or "tree-" + "-".join(sorted(e["sha"] for e in entries))
        self.trees[full_name] = {"sha": sha, "tree": entries, "truncated": False}

    def touch(self, full_name: str, pushed_at: str | None = None, **changes):
        """Modifie le dépôt : son ETag change."""
        if pushed_at:
            self.repos[full_name]["pushed_at"] = pushed_at
        self.repos[full_name].update(changes)
        n = int(self.etags[full_name].split("-")[1].rstrip('"')) + 1
        self.etags[full_name] = f'W/"etag-{n}"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}
        if request.url.host == "raw.githubusercontent.com":
            full, path = "/".join(parts[:2]), "/".join(parts[3:])
            self.calls["file"] += 1
            if (full, path) in self.files:
                return httpx.Response(200, text=self.files[(full, path)])
            return httpx.Response(404, text="not found")
        if parts[0] == "repos" and len(parts) == 3:
            full = "/".join(parts[1:3])
            if full not in self.repos:
                self.calls["repo-404"] += 1
                return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
            if request.headers.get("if-none-match") == self.etags[full]:
                self.calls["repo-304"] += 1
                return httpx.Response(304, headers=headers)
            self.calls["repo-200"] += 1
            return httpx.Response(200, content=json.dumps(self.repos[full]),
                                  headers={**headers, "etag": self.etags[full],
                                           "content-type": "application/json"})
        if parts[0] == "repos" and parts[3:5] == ["git", "trees"]:
            self.calls["tree"] += 1
            return httpx.Response(200, json=self.trees["/".join(parts[1:3])], headers=headers)
        return httpx.Response(404, json={"message": f"route inconnue {request.url}"})


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def gh(fake):
    client = GitHubClient("test-token", transport=httpx.MockTransport(fake.handler),
                          sleep=lambda _s: None)
    yield client
    client.close()


def seed(conn, full_name="acme/tools", *, repo_id=None, stars=10, forks=1, skills=(), **repo):
    """Insère un dépôt et ses skills directement en base. `skills` : liste de dictionnaires."""
    repo_id = repo_id or f"R_{full_name}"
    owner = full_name.split("/")[0]
    db.upsert_repo(conn, {
        "id": repo_id, "full_name": full_name, "url": f"https://github.com/{full_name}",
        "owner": owner, "owner_type": "user", "is_official": int(owner == "anthropics"),
        "description": "desc", "stars": stars, "forks": forks, "license": "MIT",
        "topics": [], "default_branch": "main", "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-01-01T00:00:00Z", **repo,
    })
    ids = []
    for s in skills:
        data = {"name": s["path"].split("/")[-1], "frontmatter_description": "", "summary": "",
                "advantages": [], "category": "other", "tags": [], "compat": ["claude-code"],
                "language": "en", "skill_md_sha": "sha", "skill_md_size": 4000,
                "install_type": "standalone", "enriched_by": "heuristic", **s}
        ids.append(db.upsert_skill(conn, repo_id, s["path"], data)[0])
    conn.commit()
    return ids
